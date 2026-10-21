# -*- coding: utf-8 -*-
"""v2 中文时间解析：前缀语法 + 最长匹配。

设计约束（v1.6 §3.3）：
- 必须最长匹配（"大后儿" 不能被 "后儿" 先命中）
- 不解析模糊意图（赶明儿/回头/改天/哪天/找个时间）→ intent_future=True
- 锚点词（上回/那会儿/前阵子）→ anchor=True，交给锚点队列
- 返回的时间是「事件发生区间」，不是「何时说的」；后者由 caller 用 mentioned_at 记
- 时区由 caller 传入 tzinfo；本模块只用 datetime

对外只暴露 parse_time()。
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any

DAY = 86400.0

# ---- 模糊意图：明确不解析 ----
_VAGUE = ("赶明儿", "赶明", "回头", "改天", "哪天", "找个时间", "找时间", "有空", "改日", "下次再说")

# ---- 锚点词：指向过去但说不清具体时间 ----
_ANCHOR = ("上回", "上上次", "那会儿", "那阵子", "前阵子", "之前", "以前", "当时", "那次")

# 星期几
_WD = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}

# 日的偏移词（相对今天）
_DAY_PREFIX = {"大前": -3, "大前天": -3, "前儿": -2, "前天": -2, "前": -2, "昨": -1, "昨天": -1, "昨儿": -1,
               "今": 0, "今天": 0, "今儿": 0,
               "明": 1, "明天": 1, "明儿": 1,
               "后儿": 2, "后天": 2, "后": 2,
               "大后": 3, "大后天": 3, "大后儿": 3}

# 周的偏移
_WEEK_PREFIX = {"上上": -2, "上": -1, "这": 0, "本": 0, "下": 1, "下下": 2}

# 月的偏移
_MONTH_PREFIX = {"上上": -2, "上": -1, "这": 0, "本": 0, "下": 1, "下下": 2}

# 年的偏移
_YEAR_WORD = {"前年": -2, "去年": -1, "头年": -1, "今年": 0, "明年": 1, "后年": 2}

# 季节 → 月份区间（北半球）
_SEASON = {"春天": (3, 5), "春季": (3, 5), "夏天": (6, 8), "夏季": (6, 8),
           "秋天": (9, 11), "秋季": (9, 11), "冬天": (12, 2), "冬季": (12, 2)}

# 补词（固定语义）
_PATCH = {
    "刚才": ("exact", -10 * 60),      # now - 10min
    "刚刚": ("exact", -10 * 60),
    "年头": ("month", 1),             # 年初 = 1 月
    "年初": ("month", 1),
    "年底": ("month", 12),
    "月底": ("month_end", 0),
    "月初": ("month_start", 0),
    "前两年": ("year", -2),
    "头两年": ("year", -2),
    "头年冬天": ("season_last_winter", 0),
}

_NUM = "0-9零一二三四五六七八九十两"
_CN_NUM = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
           "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def _cn_int(s: str) -> int | None:
    """解析 1-31 的中文/阿拉伯数字（够用于日期、星期）。"""
    s = s.strip()
    if not s:
        return None
    if s.isdigit():
        return int(s)
    if s == "十":
        return 10
    if s.startswith("十"):
        rest = _CN_NUM.get(s[1:], None)
        return 10 + rest if rest is not None else None
    if "十" in s:
        a, _, b = s.partition("十")
        hi = _CN_NUM.get(a, None)
        lo = _CN_NUM.get(b, None) if b else 0
        if hi is None or lo is None:
            return None
        return hi * 10 + lo
    return _CN_NUM.get(s)


def _day_bounds(d: datetime) -> tuple[float, float]:
    start = d.replace(hour=0, minute=0, second=0, microsecond=0)
    return start.timestamp(), (start + timedelta(days=1)).timestamp() - 1


def _shift_month(d: datetime, months: int) -> datetime:
    y, m = d.year, d.month + months
    while m <= 0:
        y -= 1
        m += 12
    while m > 12:
        y += 1
        m -= 12
    return d.replace(year=y, month=m, day=1)


def _safe_date(year: int, month: int, day: int, tz) -> datetime | None:
    try:
        return datetime(year, month, day, tzinfo=tz)
    except ValueError:
        return None


def _month_bounds(year: int, month: int, tz) -> tuple[float, float]:
    start = datetime(year, month, 1, tzinfo=tz)
    end = _shift_month(start, 1) - timedelta(seconds=1)
    return start.timestamp(), end.timestamp()


def _result(phrase: str, gran: str, start: float | None, end: float | None,
            confidence: float = 0.9, *, intent_future: bool = False,
            anchor: bool = False, note: str = "") -> dict[str, Any]:
    return {
        "matched": bool(phrase),
        "time_phrase": phrase,
        "granularity": gran,
        "occurred_start": start,
        "occurred_end": end,
        "confidence": confidence,
        "intent_future": intent_future,
        "anchor": anchor,
        "note": note,
    }


def parse_time(text: str, now: datetime | None = None, tz=None) -> dict[str, Any]:
    """从一句话里解析时间短语 → 事件发生区间。

    只取「第一个最强的时间短语」；找不到返回 matched=False。
    now 必须带 tz（默认取 now 自带的 tzinfo）。
    """
    now = now or datetime.now(tz)
    tz = tz or now.tzinfo
    raw = str(text or "")
    if not raw.strip():
        return _result("", "", None, None, 0.0)

    # 0) 模糊意图优先（它们本身可能含"明"等字，先拦）
    for w in _VAGUE:
        if w in raw:
            return _result(w, "none", None, None, 0.2, intent_future=True, note="vague")

    # 1) 具体月日 + 区间："3月21号到23号" / "21号到23号"
    m = re.search(rf"([{_NUM}]{{1,3}})月([{_NUM}]{{1,3}})[号日]?\s*(?:到|至|~|-|—)\s*([{_NUM}]{{1,3}})[号日]", raw)
    if m:
        mon, d1, d2 = _cn_int(m.group(1)), _cn_int(m.group(2)), _cn_int(m.group(3))
        if mon and d1 and d2:
            year = now.year
            s = _safe_date(year, mon, d1, tz)
            e = _safe_date(year, mon, d2, tz)
            if s and e:
                st, _ = _day_bounds(s)
                _, en = _day_bounds(e)
                return _result(m.group(0), "day", st, en, 0.95)

    # 2) 月日单点："3月21号"
    m = re.search(rf"([{_NUM}]{{1,3}})月([{_NUM}]{{1,3}})[号日]", raw)
    if m:
        mon, d = _cn_int(m.group(1)), _cn_int(m.group(2))
        if mon and d:
            s = _safe_date(now.year, mon, d, tz)
            if s:
                st, en = _day_bounds(s)
                return _result(m.group(0), "day", st, en, 0.9)

    # 3) 单独日期 + 区间："21号到23号" / "21-23号"
    m = re.search(rf"([{_NUM}]{{1,3}})[号日]\s*(?:到|至|~|-|—)\s*([{_NUM}]{{1,3}})[号日]", raw)
    if m:
        d1, d2 = _cn_int(m.group(1)), _cn_int(m.group(2))
        if d1 and d2:
            base = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            s = _safe_date(base.year, base.month, d1, tz)
            e = _safe_date(base.year, base.month, d2, tz)
            if s and e:
                st, _ = _day_bounds(s)
                _, en = _day_bounds(e)
                return _result(m.group(0), "day", st, en, 0.85)

    # 4) 单独日期："21号"
    m = re.search(rf"([{_NUM}]{{1,3}})[号日]", raw)
    if m:
        d = _cn_int(m.group(1))
        if d:
            base = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            s = _safe_date(base.year, base.month, d, tz)
            if s:
                st, en = _day_bounds(s)
                return _result(m.group(0), "day", st, en, 0.8)

    # 5) 星期几：「上礼拜三 / 这周六 / 下周三」
    m = re.search(rf"(上上|上|这|本|下下|下)(?:个)?(?:周|星期|礼拜)([{''.join(_WD)}])", raw)
    if m:
        off = _WEEK_PREFIX.get(m.group(1), 0)
        target = _WD.get(m.group(2))
        if target is not None:
            monday = (now - timedelta(days=now.weekday())).replace(
                hour=0, minute=0, second=0, microsecond=0)
            d = monday + timedelta(days=off * 7 + target)
            st, en = _day_bounds(d)
            return _result(m.group(0), "day", st, en, 0.9)

    # 6) 整周：「上周 / 这周 / 下周」
    m = re.search(r"(上上|上|这|本|下下|下)(?:个)?(?:周|星期|礼拜)", raw)
    if m:
        off = _WEEK_PREFIX.get(m.group(1), 0)
        monday = (now - timedelta(days=now.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0) + timedelta(days=off * 7)
        st = monday.timestamp()
        en = (monday + timedelta(days=7)).timestamp() - 1
        return _result(m.group(0), "week", st, en, 0.85)

    # 7) 月份：「下个月 / 上上个月 / 这月」
    m = re.search(r"(上上|上|这|本|下下|下)(?:个)?月", raw)
    if m:
        off = _MONTH_PREFIX.get(m.group(1), 0)
        base = _shift_month(now.replace(day=1, hour=0, minute=0, second=0, microsecond=0), off)
        st, en = _month_bounds(base.year, base.month, tz)
        return _result(m.group(0), "month", st, en, 0.85)

    # 8a) 含"前"的复合词优先于单字"前"（否则 "前两年"/"前阵子" 会被解析成前天）
    for word in ("前两年", "头两年"):
        if word in raw:
            y = now.year - 2
            st = datetime(y, 1, 1, tzinfo=tz).timestamp()
            en = datetime(y, 12, 31, 23, 59, 59, tzinfo=tz).timestamp()
            return _result(word, "year", st, en, 0.9)
    for word in sorted(_ANCHOR, key=len, reverse=True):
        if word in raw:
            return _result(word, "vague", None, None, 0.4, anchor=True, note="anchor")

    # 8) 日偏移：「昨天 / 大后天 / 前儿」
    #    按长度降序尝试，避免 "后儿" 抢先于 "大后儿"
    for word in sorted(_DAY_PREFIX, key=len, reverse=True):
        idx = raw.find(word)
        if idx < 0:
            continue
        after = raw[idx + len(word): idx + len(word) + 1]
        if after in ("个", "月", "年", "周", "星期", "儿"):
            continue
        off = _DAY_PREFIX[word]
        d = now + timedelta(days=off)
        st, en = _day_bounds(d)
        return _result(word, "day", st, en, 0.9)

    # 9) 年 + 季组合（必须先于单独年份，否则 "去年冬天" 会被解析成整年）
    m = re.search(r"(前年|去年|头年|今年|明年|后年)(春天|春季|夏天|夏季|秋天|秋季|冬天|冬季)", raw)
    if m:
        y = now.year + _YEAR_WORD.get(m.group(1), 0)
        s_word = m.group(2).replace("春季", "春天").replace("夏季", "夏天").replace("秋季", "秋天").replace("冬季", "冬天")
        m1, m2 = _SEASON.get(s_word, (1, 12))
        if m1 <= m2:
            st = datetime(y, m1, 1, tzinfo=tz).timestamp()
            en = _shift_month(datetime(y, m2, 1, tzinfo=tz), 1).timestamp() - 1
        else:
            st = datetime(y, 12, 1, tzinfo=tz).timestamp()
            en = datetime(y + 1, 3, 1, tzinfo=tz).timestamp() - 1
        return _result(m.group(0), "season", st, en, 0.9)

    # 9b) 单独年份：「前年 / 去年 / 今年 / 明年 / 后年」
    for word in sorted(_YEAR_WORD, key=len, reverse=True):
        if word in raw:
            off = _YEAR_WORD[word]
            y = now.year + off
            st = datetime(y, 1, 1, tzinfo=tz).timestamp()
            en = datetime(y, 12, 31, 23, 59, 59, tzinfo=tz).timestamp()
            return _result(word, "year", st, en, 0.85)

    # 10) 季节
    for word in sorted(_SEASON, key=len, reverse=True):
        if word in raw:
            y = now.year
            if word == "头年冬天":
                y -= 1
            m1, m2 = _SEASON[word]
            if m1 <= m2:
                st = datetime(y, m1, 1, tzinfo=tz).timestamp()
                en = datetime(y, m2, 1, tzinfo=tz)
                en = _shift_month(en, 1).timestamp() - 1
            else:                       # 冬天跨年：12 月 → 次年 2 月
                st = datetime(y, 12, 1, tzinfo=tz).timestamp()
                en = datetime(y + 1, 3, 1, tzinfo=tz).timestamp() - 1
            return _result(word, "season", st, en, 0.8)

    # 11) 补词
    for word in sorted(_PATCH, key=len, reverse=True):
        if word in raw:
            kind, val = _PATCH[word]
            if kind == "exact":
                t = (now + timedelta(seconds=val)).timestamp()
                return _result(word, "exact", t, t, 0.9)
            if kind == "month":
                st, en = _month_bounds(now.year, int(val), tz)
                return _result(word, "month", st, en, 0.85)
            if kind == "month_end":
                st, en = _month_bounds(now.year, now.month, tz)
                return _result(word, "month", st, en, 0.8)
            if kind == "month_start":
                st, en = _month_bounds(now.year, now.month, tz)
                return _result(word, "month", st, en, 0.8)
            if kind == "year":
                y = now.year + int(val)
                st = datetime(y, 1, 1, tzinfo=tz).timestamp()
                en = datetime(y, 12, 31, 23, 59, 59, tzinfo=tz).timestamp()
                return _result(word, "year", st, en, 0.9)
            if kind == "season_last_winter":
                y = now.year - 1
                st = datetime(y, 12, 1, tzinfo=tz).timestamp()
                en = datetime(now.year, 3, 1, tzinfo=tz).timestamp() - 1
                return _result(word, "season", st, en, 0.8)

    # 12) 锚点词
    for word in sorted(_ANCHOR, key=len, reverse=True):
        if word in raw:
            return _result(word, "vague", None, None, 0.4, anchor=True, note="anchor")

    return _result("", "", None, None, 0.0)

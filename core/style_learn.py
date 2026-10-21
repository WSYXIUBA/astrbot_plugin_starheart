# -*- coding: utf-8 -*-
"""语言学习 MVP：从真实语料里蒸馏「原话样本 + 词表」。

流程（v1.3 §5）：
  ① 取语料 → canon → 私聊配对（≤300s）→ 水位过滤
  ② 一次 LLM：挑值得学的对子 + 词条
  ③ 代码按下标取原话落库（模型不复述原文）
  ④ 写水位（只在新语料够量时才跑）

纪律：
- 模型只回下标，正文由代码从 hf_corpus 取（逐字可查是结构保证）。
- 只做私聊配对；群聊没有 wake 标记，MVP 跳过。
- 失败不写水位，可重试。
"""
from __future__ import annotations

import json
import time
from typing import Any

from astrbot.api import logger

from . import style_canon as canon_mod

PAIR_GAP = 300.0          # 私聊「回复前最后一条用户消息」允许的最大间隔
MIN_FRESH = 30            # 新语料少于这个数不跑
MAX_PAIRS = 60            # 一次喂给模型的最大对数

STYLE_LEARN_SYSTEM = """你在帮一个聊天机器人学「人怎么说话」。只输出 JSON，不要解释。

输入是从真实聊天里抽出来的问答对，编号后每行一组。
任务是挑出值得让机器人学的，最多 12 组。

值得学：
- 短、口语、像人随口说的（一问一答各 ≤40 字）
- 有接话方式或个性：短答、反问、敷衍、撒娇、接梗、嘴硬
- 能看出「什么场合怎么接」

不要：
- 寒暄（在吗/早/晚安/吃了吗）
- 命令、指令、工具调用、链接、代码
- 吵架、辱骂、刷屏、复读
- 问 AI 能力/自我介绍/客服腔
- 纯表情、纯数字、纯语气词

另外可选出词条：只在对话里出现「圈内才懂」的词或明显口癖时给。
词条格式 {"term":"典","kind":"tic|slang","usage":"什么时候用","meaning":"黑话才填"}。

输出（严格 JSON，不要 markdown）：
{"picks":[{"i":1,"tags":["短句","接梗"]}],"terms":[{"term":"典","kind":"tic","usage":"对方发言很典型时"}]}

i 是行首编号。没有值得学的就 {"picks":[],"terms":[]}。
"""


def norm(text: str) -> str:
    return canon_mod.norm(text)


GROUP_GAP = 120.0   # 群聊干扰多：只认 2 分钟内的紧邻回复（有 @/唤醒标记后再放宽）


def pair_rows(rows: list[dict[str, Any]], *, since_id: int = 0,
              gap: float = PAIR_GAP) -> list[dict[str, Any]]:
    """把语料行配成 (trigger, reply) 对。

    - 私聊：bot 回复前最近一条非 bot 消息，间隔 ≤ gap（默认 300s），conf 0.9
    - 群聊：同样规则但只认 ≤120s（群聊交错多，宁缺毋滥），conf 0.7
    - 只保留 reply_id > since_id 的对（新回复）
    """
    by_sess: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        by_sess.setdefault(str(r.get("session_id") or ""), []).append(r)

    out: list[dict[str, Any]] = []
    for sid, items in by_sess.items():
        items.sort(key=lambda x: float(x.get("ts") or 0))
        if not items:
            continue
        is_group = "群" in str(items[0].get("chat_type") or "")
        limit_gap = GROUP_GAP if is_group else float(gap)
        for i, r in enumerate(items):
            if not int(r.get("is_bot") or 0):
                continue
            rid = int(r.get("id") or 0)
            if rid <= int(since_id):
                continue
            j = i - 1
            while j >= 0 and int(items[j].get("is_bot") or 0):
                j -= 1
            if j < 0:
                continue
            gap_s = float(r.get("ts") or 0) - float(items[j].get("ts") or 0)
            if gap_s < 0 or gap_s > limit_gap:
                continue
            out.append({
                "trigger": str(items[j].get("text") or ""),
                "reply": str(r.get("text") or ""),
                "trigger_id": int(items[j].get("id") or 0),
                "reply_id": rid,
                "session_id": sid,
                "ts": float(r.get("ts") or 0),
                "conf": 0.7 if is_group else 0.9,
                "register": "group" if is_group else "private",
            })
    out.sort(key=lambda x: x["ts"])
    return out


def build_prompt(pairs: list[dict[str, Any]]) -> str:
    lines = []
    for i, p in enumerate(pairs, 1):
        t = canon_mod.canon(p["trigger"])
        r = canon_mod.canon(p["reply"])
        if not t or not r:
            continue
        lines.append(f"{i}. 他：{t[0][:40]} / 她：{r[0][:40]}")
    return "\n".join(lines[:MAX_PAIRS])


def parse_learn(raw: str) -> dict[str, Any]:
    """解析模型输出：容忍 ```json 包裹与前后废话。"""
    s = str(raw or "").strip()
    if not s:
        return {"picks": [], "terms": []}
    s = s.strip("`")
    if s.lower().startswith("json"):
        s = s[3:].strip()
    i, j = s.find("{"), s.rfind("}")
    if i < 0 or j <= i:
        return {"picks": [], "terms": []}
    try:
        data = json.loads(s[i:j + 1])
    except Exception:                                       # noqa: BLE001
        return {"picks": [], "terms": []}
    picks = data.get("picks") or []
    terms = data.get("terms") or []
    out = {"picks": [], "terms": []}
    for p in picks:
        try:
            out["picks"].append({"i": int(p.get("i")), "tags": [str(x)[:8] for x in (p.get("tags") or [])][:3]})
        except (TypeError, ValueError, AttributeError):
            continue
    for t in terms:
        term = str(t.get("term") or "").strip()[:16]
        # 单字词条会误命中（「非」会打中「非常」「非要说」），至少两字
        if len(term) < 2:
            continue
        out["terms"].append({
            "term": term,
            "kind": str(t.get("kind") or "tic") if str(t.get("kind") or "tic") in ("tic", "slang", "address") else "tic",
            "usage": str(t.get("usage") or "")[:60],
            "meaning": str(t.get("meaning") or "")[:60],
        })
    return out


async def _get_wm(store) -> int:
    try:
        row = await (await store.db.execute(
            "SELECT value FROM hf_config WHERE key='style_watermark'")).fetchone()
        return int(float(dict(row)["value"])) if row else 0
    except Exception:                                       # noqa: BLE001
        return 0


async def _set_wm(store, value: int) -> None:
    try:
        await store.db.execute(
            "INSERT INTO hf_config(key,value,updated_at) VALUES('style_watermark',?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (str(int(value)), time.time()),
        )
        await store.db.commit()
    except Exception as exc:                                # noqa: BLE001
        logger.debug(f"[StarHeart] style 水位写入跳过：{exc}")


async def run_distill(main, cfg: dict[str, Any], persona_id: str = "",
                      *, max_pairs: int = MAX_PAIRS,
                      min_fresh: int = MIN_FRESH) -> dict[str, Any]:
    """跑一轮蒸馏。返回统计；失败不写水位。"""
    store = main.store
    svc = main.service
    stat: dict[str, Any] = {"ok": False, "fresh": 0, "pairs": 0, "picked": 0,
                            "saved_bot": 0, "saved_human": 0, "terms": 0, "reason": ""}

    rows = await store.list_corpus(limit=1500)
    if not rows:
        stat["reason"] = "corpus_empty"
        return stat
    wm = await _get_wm(store)
    max_id = max(int(r.get("id") or 0) for r in rows)
    fresh = [r for r in rows if int(r.get("id") or 0) > wm]
    stat["fresh"] = len(fresh)
    if len(fresh) < int(min_fresh):
        stat["reason"] = f"fresh<{int(min_fresh)}"
        return stat

    pairs = pair_rows(rows, since_id=wm)
    # 取最新 N 对：配对结果按时间升序，[:N] 会挑到最旧的（实测学成 10-03 的对话）
    pairs = pairs[-int(max_pairs):]
    stat["pairs"] = len(pairs)
    if not pairs:
        stat["reason"] = "no_pairs"
        return stat

    prompt = "对话对：\n" + build_prompt(pairs)
    gen = svc.system.get("llm_generate")
    if not callable(gen):
        stat["reason"] = "no_llm"
        return stat
    try:
        raw = await gen(prompt, "style_learn", STYLE_LEARN_SYSTEM)
    except Exception as exc:                                # noqa: BLE001
        stat["reason"] = f"llm_error:{exc}"[:120]
        return stat

    parsed = parse_learn(raw)
    stat["picked"] = len(parsed["picks"])

    # 词条
    for t in parsed["terms"]:
        if await store.save_style_term({**t, "persona_id": persona_id}):
            stat["terms"] += 1

    # 样本：下标 → 原话（模型不复述）
    # 先把「人说过的话」当口吻样本收一批（群聊/私聊都收，最近 80 条人话）
    said = [r for r in rows if not int(r.get("is_bot") or 0)]
    said.sort(key=lambda x: float(x.get("ts") or 0))
    for r in said[-60:]:
        reg = "group" if "群" in str(r.get("chat_type") or "") else "private"
        for b in canon_mod.canon(r.get("text")):
            # 8 字起：滤掉「你还是算了吧，」「astrbot插件」这类半句/碎片
            b = b.strip("，。、；：,.;: 　")
            if 8 <= len(b) <= 80 and not canon_mod.looks_like_bot_meta(b):
                if await store.save_style_sample({
                    "persona_id": persona_id, "kind": "human", "register": reg,
                    "trigger_text": "", "reply_text": b,
                    "source_ids": [int(r.get("id") or 0)],
                    "sensitivity": "normal",
                }):
                    stat["saved_human"] += 1

    seen: set[str] = set()
    # 同前缀去重：同主题的口癖句只留一条（实测「改三遍还说不行…」出现两个变体）
    seen_prefix: set[str] = set()
    try:
        _existing = await store.list_style_samples(persona_id, "bot", "active", 300)
        seen_prefix = {str(r.get("reply_text") or "")[:6] for r in _existing}
    except Exception:                                       # noqa: BLE001
        pass
    for p in parsed["picks"]:
        idx = p["i"] - 1
        if idx < 0 or idx >= len(pairs):
            continue
        pair = pairs[idx]
        trig = canon_mod.canon(pair["trigger"])
        rep = canon_mod.canon(pair["reply"])
        if not trig or not rep:
            continue
        rep0 = rep[0]
        if canon_mod.looks_like_bot_meta(rep0):
            continue
        key = canon_mod.norm_key(trig[0], rep0)
        if key in seen:
            continue
        seen.add(key)

        # 人侧样本：用户自己怎么说话（私聊里没有「群友互答」，只有「他说 + 我回」）
        # 注入时用来对齐他的口吻，不是教 bot 模仿别人。
        # 门槛 5 字：滤掉「就是」「怎么说呢」这类纯口头碎片。
        if 8 <= len(trig[0].strip("，。、；：,.;: 　")) <= 80 and not canon_mod.looks_like_bot_meta(trig[0]):
            if await store.save_style_sample({
                "persona_id": persona_id, "kind": "human",
                "register": str(pair.get("register") or "private"),
                "trigger_text": "", "reply_text": trig[0],
                "source_ids": [pair["trigger_id"]],
                "sensitivity": "normal",
            }):
                stat["saved_human"] += 1
        # bot 侧样本：同一轮多气泡只取最长的一条（避免同主题刷屏，实测 3 条「改三遍」变体）
        cand = [b for b in rep[:4] if not canon_mod.looks_like_bot_meta(b)]
        if cand:
            best = max(cand, key=len)
            if best[:6] in seen_prefix:
                continue
            seen_prefix.add(best[:6])
            if await store.save_style_sample({
                "persona_id": persona_id, "kind": "bot",
                "register": str(pair.get("register") or "private"),
                "trigger_text": trig[0], "reply_text": best,
                "source_ids": [pair["reply_id"]],
                "sensitivity": "normal",
            }):
                stat["saved_bot"] += 1

    stat["ok"] = bool(stat["picked"])
    if not stat["ok"]:
        stat["reason"] = "model_picked_nothing"
        return stat
    await _set_wm(store, max_id)
    logger.info(f"[StarHeart] 语言学习：{stat}")
    return stat


async def style_block(store, cfg: dict[str, Any], persona_id: str,
                      register: str, current_msg: str) -> str:
    """渲染注入块（≤300 字）。无样本/无词条返回空串。"""
    if not bool(cfg.get("expression_style_enabled", False)):
        return ""
    parts: list[str] = []

    bot_rows = await store.pick_style_samples(persona_id, "bot", register, 1)
    human_rows = await store.pick_style_samples(persona_id, "human", register, 1)
    terms = await store.list_style_terms(persona_id, scope="", limit=20)

    if bot_rows:
        b = bot_rows[0]
        parts.append(f"【你自己这么说过】「{str(b.get('reply_text') or '')[:40]}」\n（学语气，不要复述这句话）")
    if human_rows:
        h = human_rows[0]
        rep = str(h.get("reply_text") or "")[:40]
        # 与当前消息太像就不投：他说过的同一句不要再摆在面前
        if rep and canon_mod.similar(rep, current_msg) < 0.6:
            parts.append(f"【他平时这么说话】「{rep}」\n（顺着他的口吻接，别用客服腔）")
        else:
            human_rows = []

    hit_terms = []
    for t in terms:
        term = str(t.get("term") or "")
        if term and term in str(current_msg or ""):
            extra = str(t.get("meaning") or t.get("usage") or "")[:24]
            hit_terms.append(f"「{term}」{('= ' + extra) if extra else ''}")
    if not hit_terms:
        hit_terms = [f"「{str(t.get('term'))}」{('= ' + str(t.get('meaning') or t.get('usage'))[:20]) if (t.get('meaning') or t.get('usage')) else ''}"
                     for t in terms[:3] if t.get("term")]
    if hit_terms:
        parts.append("【口头禅】" + "；".join(hit_terms[:4]))

    if not parts:
        return ""
    block = "【说话方式】（学语气和句式，不要逐字复刻下面的句子）\n" + "\n".join(parts)
    if len(block) > 300:
        block = block[:299] + "…"
    # 记账：本轮真投了哪几条
    for row in (bot_rows[:1] + human_rows[:1]):
        try:
            await store.touch_style_sample(int(row["id"]))
        except Exception:                                   # noqa: BLE001
            pass
    return block

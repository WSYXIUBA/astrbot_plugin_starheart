"""StarHeart 引擎核心：纯函数，不依赖 astrbot，便于单测与面板试算复用。

门控不是让 LLM 判断要不要开口，而是本地公式打分（零 LLM 成本）：
    score = (base × state × relation) - penalty
base 由内容特征与相关性组成，state/relation 是乘性系数，penalty 是短反应/存在感惩罚。
"""

from __future__ import annotations

import difflib
import re
import time
from typing import Any

# 内容特征权重（可被面板配置覆盖的部分留在 service 层）
AT_WEIGHT = 100
PRIVATE_BASE = 40
QUESTION_WEIGHT = 15
REQUEST_WEIGHT = 20
MENTION_WEIGHT = 35
SHORT_REPLY_PENALTY = -25
PRESENCE_PENALTY = -8
PRESSURE_BONUS = 12

# 记忆衰减的类型系数（乘在 half_life 上）：>1 掉得慢，<1 掉得快。
# 关系/偏好/承诺是长期陪伴的骨架，不能和一次性的琐事用同一条半衰期。
_DECAY_TYPE_MULT = {
    "relational": 1.8,
    "preference": 1.6,
    "planned": 1.5,
    "opinion": 1.2,
    "routine": 1.1,
    "factual": 1.0,
    "episodic": 0.8,
}

QUESTION_PAT = re.compile(r"[?？]|(吗|呢|怎么|为什么|如何|是不是|能不能|可不可以)\s*$")
REQUEST_PAT = re.compile(r"(帮我|麻烦|请|来个|写个|看下|查一下|推荐|教我)")
SHORT_PAT = re.compile(r"^[\s\S]{0,3}$")
FAVOUR_TAG_PAT = re.compile(r"\[\s*(?:好感度?|favou?r)\s*[:：]?\s*(?:持平|没有?变化|unchanged|[+-]?\d+(?:\.\d+)?)\s*\]", re.I)


def zone_of(score: float) -> str:
    if score >= 80:
        return "DEVOTED"
    if score >= 60:
        return "CLOSE"
    if score >= 30:
        return "FAMILIAR"
    if score >= 0:
        return "NEUTRAL"
    if score >= -30:
        return "COLD"
    return "HOSTILE"


def relation_coefficient(favour_daily: float) -> float:
    """好感 → 关系系数：0.7 ~ 1.4，越亲近越愿意开口。

    阈值必须与 zone_of() 的档位边界一一对应。以前 CLOSE(60-79) 与 DEVOTED(≥80)
    共用 `>= 60 → 1.4` 同一档，导致「亲密」和「挚爱」对门控完全等价，档位白分。
    """
    if favour_daily >= 80:          # DEVOTED
        return 1.4
    if favour_daily >= 60:          # CLOSE
        return 1.3
    if favour_daily >= 30:          # FAMILIAR
        return 1.15
    if favour_daily >= 0:           # NEUTRAL
        return 1.0
    if favour_daily >= -30:         # COLD
        return 0.85
    return 0.7                      # HOSTILE


def score_message(
    text: str,
    *,
    is_at: bool = False,
    is_private: bool = False,
    window_count: int = 0,
    pressure: float = 50.0,
    relation_coef: float = 1.0,
    state_coef: float = 1.0,
    in_backoff: bool = False,
    threshold: float = 80.0,
) -> dict[str, Any]:
    """对一条消息打分。返回总分与各因子明细，供面板展示与落库。"""
    t = (text or "").strip()
    if in_backoff:
        return {
            "score": 0.0,
            "decision": "backoff",
            "factors": {"backoff": True, "window_count": window_count},
            "reason": "退避中，跳过本轮评估",
        }
    factors: dict[str, float] = {}
    if is_at:
        factors["at_me"] = float(AT_WEIGHT)
    if is_private:
        factors["private"] = float(PRIVATE_BASE)
    if QUESTION_PAT.search(t):
        factors["question"] = float(QUESTION_WEIGHT)
    if REQUEST_PAT.search(t):
        factors["request"] = float(REQUEST_WEIGHT)
    if not is_private and not is_at and window_count >= 2:
        factors["presence"] = float(PRESENCE_PENALTY) * min(window_count - 1, 3)
    if SHORT_PAT.match(t) and not is_private:
        factors["short_reply"] = float(SHORT_REPLY_PENALTY)
    if pressure >= 70:
        factors["pressure_bonus"] = float(PRESSURE_BONUS)
    base = sum(factors.values())
    score = base * max(0.2, state_coef) * max(0.2, relation_coef)
    factors["state_coef"] = round(state_coef, 3)
    factors["relation_coef"] = round(relation_coef, 3)
    score = round(max(0.0, min(100.0, score)), 1)
    return {
        "score": score,
        "decision": decide(score, threshold),
        "factors": factors,
        "reason": "本地公式评分（未经过 LLM）",
    }


def decide(score: float, threshold: float) -> str:
    """阈值判据：达到阈值才回。原始分（可为负）低于 0 直接沉默。"""
    return "reply" if score >= max(1.0, threshold) else "silent"


def extract_favour_tag(text: str) -> tuple[str, float | None]:
    """把主 LLM 尾行的好感自报标签剥离出来，返回（干净文本, 增减值）。持平 = 0。"""
    if not text:
        return text, None
    match = FAVOUR_TAG_PAT.search(text)
    if match is None:
        return text, None
    inner = match.group(0)
    if "持平" in inner or "没有变化" in inner or "无变化" in inner or "unchanged" in inner.lower():
        delta = 0.0
    else:
        num = re.search(r"[+-]?\d+(?:\.\d+)?", inner)
        delta = float(num.group(0)) if num else None
    cleaned = FAVOUR_TAG_PAT.sub("", text).rstrip()
    return cleaned, delta


TONE_LINES = {
    "DEVOTED": "最亲的那一档：可以主动找他说话，可以撒娇，也可以直接说想他，被冷落会直说。",
    "CLOSE": "聊得挺来：可以主动接话、开玩笑、叫昵称，深夜也回。",
    "FAMILIAR": "算熟人：随口聊就行，有梗就接，不当外人也不端着。",
    "NEUTRAL": "普通网友：随口聊，不用刻意热络，也不用装客气。",
    "COLD": "不太熟：话少点，不追问，不给建议。",
    "HOSTILE": "不待见：能一个字就一个字，不接话。",
}


def tone_line(zone: str, overrides: dict[str, str] | None = None) -> str:
    """语气线文案：面板可改（overrides），没改就用内置默认。"""
    if overrides and str(overrides.get(zone) or "").strip():
        return str(overrides[zone]).strip()
    return TONE_LINES.get(zone, TONE_LINES["NEUTRAL"])


def ago_label(ts: float | Any, now: float | None = None) -> str:
    """时间戳 → 「刚刚 / 12 分钟前 / 3 天前 / 5 个月前」。

    刻意只给**相对**时间：绝对日期要时区，而这里是注入路径上的无状态纯函数；
    相对量已经够模型判断「刚发生的还是陈年旧事」，也不会因为容器时区配错而说谎
    （真机踩过：容器 TZ=UTC，naive 时间整整差 8 小时）。
    """
    try:
        t = float(ts or 0)
    except (TypeError, ValueError):
        return ""
    if t <= 0:
        return ""
    delta = max(0.0, float(now if now is not None else time.time()) - t)
    if delta < 120:
        return "刚刚"
    if delta < 3600:
        return f"{int(delta // 60)} 分钟前"
    if delta < 86400:
        return f"{int(delta // 3600)} 小时前"
    if delta < 30 * 86400:
        return f"{int(delta // 86400)} 天前"
    if delta < 365 * 86400:
        return f"{int(delta // (30 * 86400))} 个月前"
    return "很久以前"


def build_memory_context(memories: list[dict[str, Any]], budget_chars: int = 900, *,
                         now: float | None = None, extra_note: str = "") -> str:
    """把召回结果拼成给模型的自然语言片段（不含任何分数）。

    每条记忆带一个「大约多久以前」的方括号标记：模型本来对记忆完全没有时间感，
    分不清「这是三天前的事还是刚才的事」—— 时间锚是它能接住「上次」「前几天」这类话的前提。
    extra_note：附加在块尾的一句提醒（主动开口链路用它标注「都是旧事/注意主语」等）。
    """
    if not memories:
        return ""
    lines: list[str] = []
    used = 0
    for mem in memories:
        summary = (mem.get("summary") or "").strip()
        if not summary:
            continue
        when = ago_label(mem.get("occurred_at") or mem.get("created_at"), now)
        line = f"- [{when}] {summary}" if when else f"- [时间未知] {summary}"
        if used + len(line) > budget_chars:
            # 超预算：跳过这条继续装下一条（旧实现 break —— 第一条超长就把整块变成空，
            # 等于「有记忆但一条都不给模型看」）。第一条就超长时截断也要给出一条。
            if lines:
                continue
            line = line[:max(20, int(budget_chars))]
        lines.append(line)
        used += len(line)
    if not lines:
        return ""
    tail = f"\n{extra_note}" if extra_note else ""
    return (
        "（你记得这些事（方括号里是「大约多久以前」，是我记忆里的粗略时间 —— "
        "它是参考不是证据，不要拿它去质疑对方「刚才在哪说过什么」）：\n"
        + "\n".join(lines)
        + tail
        + "\n）"
    )


def _norm_for_similarity(text: str) -> str:
    """相似度比对前的归一化：小写、去空白/标点/emoji，只留文字与数字。"""
    t = str(text or "").lower()
    t = re.sub(r"[\s\u3000]+", "", t)
    t = re.sub(r"[^\w\u4e00-\u9fff]+", "", t)
    return t


def text_similarity(a: str, b: str) -> float:
    """两条短文本的相似度（0~1）：取 字符包含度 / 二元组包含度 / difflib 比值 的最大值。

    为什么不是简单相等：重播往往只改几个字（「周五会我找人替了」→
    「周五会找人替了 省得再挨骂」），必须能抓住「同一件事换个说法再说一遍」。
    包含度用 min() 作分母，专治「一长一短」的改写；任一侧太短（<4 字）不比对，避免误伤。
    """
    na, nb = _norm_for_similarity(a), _norm_for_similarity(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0            # 一字不差就是复读，短句也认（先于长度护栏）
    if len(na) < 4 or len(nb) < 4:
        return 0.0            # 近似判断太短的文本没意义，防误伤
    # 长度差太大时只可能是「短句被长句包含」，不是同一件事的改写；
    # 用 min 作分母会把「TA养了只猫」和「TA养了只猫叫花花」判成 1.0，进而吞掉新信息。
    len_ratio = min(len(na), len(nb)) / max(len(na), len(nb))
    sa, sb = set(na), set(nb)
    char_contain = len(sa & sb) / max(1, max(len(sa), len(sb)))
    ba = {na[i:i + 2] for i in range(len(na) - 1)}
    bb = {nb[i:i + 2] for i in range(len(nb) - 1)}
    bigram_contain = (len(ba & bb) / max(1, max(len(ba), len(bb)))) if (ba and bb) else 0.0
    ratio = difflib.SequenceMatcher(None, na, nb).ratio()
    score = max(char_contain, bigram_contain, ratio)
    if len_ratio < 0.75:
        score = min(score, 0.6)   # 只允许「整句包含」在调用方单独处理，不在这里判成复读
    return round(score, 4)


def cosine_vec(a: list[float] | None, b: list[float] | None) -> float:
    """两个稠密向量的余弦相似度（0~1）；维度不一致/空向量一律 0。

    主动消息的语义防复读用：字符相似度抓不住「同一个梗换个说法」
    （同一件事换一种说法时，字符相似度往往只有 0.3~0.4），
    向量能抓住，但必须失败即回退，不能让嵌入服务拖住开口。
    """
    import math

    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(float(x) * float(y) for x, y in zip(a, b))
    na = math.sqrt(sum(float(x) * float(x) for x in a))
    nb = math.sqrt(sum(float(y) * float(y) for y in b))
    if na <= 0 or nb <= 0:
        return 0.0
    return max(0.0, min(1.0, dot / (na * nb)))


def tokenize(text: str) -> list[str]:
    """轻量分词：英文按词，中文按 2-gram（长句不再被当成一个整 token）。"""
    raw = re.findall(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]+", (text or "").lower())
    out: list[str] = []
    for w in raw:
        if re.fullmatch(r"[A-Za-z0-9_]+", w):
            if len(w) >= 2:
                out.append(w)
        elif len(w) <= 2:
            out.append(w)
        else:
            out.extend(w[i:i + 2] for i in range(len(w) - 1))
    # 去重保序，限制数量，避免超长文本把 SQL/Python 拖慢
    seen: set[str] = set()
    deduped: list[str] = []
    for w in out:
        if w not in seen:
            seen.add(w)
            deduped.append(w)
    return deduped[:64]


# 可召回状态：只把「删除 / 遗忘」当终点，其余一律可召回 —— 状态只做排序乘子，不做过滤器。
# 旧实现把 stale / archived 挡在召回外，于是「没被召回 → 归档 → 更不可能被召回 → 遗忘」
# 变成一条自毁链。乘子让旧记忆变淡，但仍有机会被一句话提起。
RECALL_STATUSES = ("active", "decayed", "stale", "archived")
STATUS_WEIGHT = {"active": 1.0, "decayed": 0.85, "stale": 0.7, "archived": 0.4}


def status_weight(status: str) -> float:
    """状态乘子（未知状态按 0.5，宁可弱一点也不丢掉）。"""
    return STATUS_WEIGHT.get(str(status or "").lower(), 0.5)


async def retrieve_memories(
    store, query: str, *, top_k: int = 5, session_id: str = "", persona_id: str = "",
    include_archived: bool = True, subject_id: str = "",
) -> list[dict[str, Any]]:
    """关键词 × 强度 × 状态 的本地召回（不上向量，先保证零依赖可用）。

    状态不做过滤器（archived 也进候选，只乘 0.4）；命中后由调用方 touch_memory 拉回。
    返回的每条带 `_hit_kind`（keyword / fallback）与 `_recall_score`：兜底（最近 N 条）
    不算「被提及」，调用方不能再给它加固 —— 旧实现两者共用出口，最新几条会永久霸榜。
    """
    tokens = tokenize(query)
    base = "SELECT * FROM hf_memories WHERE status NOT IN ('deleted','forgotten')"
    if not include_archived:
        base += " AND status!='archived'"

    def _persona_sql(untagged: bool = False) -> tuple[str, list[Any]]:
        if untagged:
            return " AND (persona_id IS NULL OR persona_id = '')", []
        if persona_id == "__none__":
            return " AND (persona_id IS NULL OR persona_id = '')", []
        if persona_id:
            return " AND persona_id=?", [persona_id]
        return "", []

    async def _fetch(limit: int, *, untagged: bool = False, sess: str = "") -> list[Any]:
        sql, args = base, []
        p_sql, p_args = _persona_sql(untagged)
        sql += p_sql
        args.extend(p_args)
        if sess:
            sql += " AND session_id=?"
            args.append(sess)
        sql += " ORDER BY strength DESC LIMIT ?"
        args.append(max(1, int(limit)))
        return await (await store.db.execute(sql, args)).fetchall()

    async def _latest(limit: int, *, untagged: bool = False, sess: str = "") -> list[Any]:
        sql, args = base, []
        p_sql, p_args = _persona_sql(untagged)
        sql += p_sql
        args.extend(p_args)
        if sess:
            sql += " AND session_id=?"
            args.append(sess)
        sql += " ORDER BY COALESCE(NULLIF(occurred_at,0), created_at) DESC LIMIT ?"
        args.append(max(1, int(limit)))
        return await (await store.db.execute(sql, args)).fetchall()

    # 1) 本会话 200 条 + 全库 200 条（并集，去重）
    rows_all: list[Any] = []
    if session_id:
        rows_all.extend(await _fetch(200, sess=session_id))
    rows_all.extend(await _fetch(200))
    seen_ids: set[str] = set()
    rows: list[Any] = []
    for r in rows_all:
        rid = str(dict(r).get("id") or "")
        if rid and rid not in seen_ids:
            seen_ids.add(rid)
            rows.append(r)
    # 2) 人格过滤后一条都没有 → 只退回「未打人格标签」的记忆，绝不拿别的人格
    if not rows and persona_id not in ("", "__none__"):
        rows = await _fetch(200, untagged=True)
        if session_id:
            rows = list(rows) + list(await _fetch(200, untagged=True, sess=session_id))

    scored: list[tuple[float, dict[str, Any]]] = []
    for row in rows:
        mem = store._mem_row(row)
        haystack = f"{mem.get('summary','')} {mem.get('content','')} {' '.join(mem.get('tags') or [])} {' '.join(mem.get('cues') or [])}".lower()
        hits = sum(1 for tk in tokens if tk in haystack)
        if hits == 0:
            continue
        # 归一到 0~1：长问句不该靠「命中词数多」天然压过短问句
        cover = hits / max(1, len(tokens))
        score = (cover * 0.7 + float(mem.get("strength") or 0) * 0.3) * status_weight(mem.get("status"))
        if session_id and mem.get("session_id") == session_id:
            score *= 1.15
        # 同一人格下的「别人的私聊」：降权但不排除（旧实现完全没有这维度，A 的记忆会注入给 B）
        if subject_id and str(mem.get("subject_id") or ""):
            score *= 1.10 if str(mem.get("subject_id")) == str(subject_id) else 0.6
        scored.append((score, mem))
    scored.sort(key=lambda x: x[0], reverse=True)
    picked: list[dict[str, Any]] = []
    for s, m in scored[: max(1, top_k)]:
        m["_hit_kind"] = "keyword"
        m["_recall_score"] = round(float(s), 4)
        picked.append(m)
    if not picked:
        # 3) 关键词 0 命中（含纯标点/emoji）时的兜底：先本会话最近，再全库最近；不要一上来就跨会话
        for sess in ([session_id] if session_id else []) + [""]:
            try:
                rows2 = await _latest(top_k, sess=sess)
            except Exception:
                rows2 = []
            if rows2:
                picked = [store._mem_row(r) for r in rows2]
                break
        if not picked and persona_id not in ("", "__none__"):
            try:
                rows2 = await _latest(top_k, untagged=True, sess=session_id)
            except Exception:
                rows2 = []
            picked = [store._mem_row(r) for r in rows2]
        for m in picked:
            m["_hit_kind"] = "fallback"     # 兜底 ≠ 被提及：调用方不得加固（见 P0-5）
            m["_recall_score"] = 0.0
    # 计数交给调用方 touch_memory（真实命中才算一次），这里不再重复 +1
    return picked


def decay_strength(strength: float, age_days: float, half_life_days: float = 21.0,
                  atom_type: str = "") -> float:
    """指数衰减：strength × 0.5^(age/half_life)；半衰期按记忆类型分化。

    关系/偏好/承诺类（称呼、雷点、答应过的事）掉得最慢 —— 这些正是长期陪伴最该留住的，
    用一条 21 天公式把所有类型一起杀掉是旧实现最大的语义错误。
    """
    mult = _DECAY_TYPE_MULT.get(str(atom_type or "").lower(), 1.0)
    half_life_days = float(half_life_days) * mult
    if half_life_days <= 0:
        return strength
    return round(max(0.0, strength * (0.5 ** (age_days / half_life_days))), 4)


def age_days(ts: float | None) -> float:
    if not ts:
        return 0.0
    return max(0.0, (time.time() - float(ts)) / 86400.0)


def strip_code_blocks(text: str) -> tuple[str, str]:
    """把代码块摘出来（代码块整段发，不拆），返回（正文, 代码块拼接）。"""
    blocks = _CODE_BLOCK_PAT.findall(text or "")
    body = _CODE_BLOCK_PAT.sub("", text or "")
    return body, "\n".join(blocks)


def should_split(text: str, *, min_len: int = 40, max_seg: int = 4) -> bool:
    """短句不拆（真人发「6」就是一条）；长句且有多个语义边界才拆。"""
    body, code = strip_code_blocks(text)
    if code and len(body) < min_len:
        return False
    if len(body) < min_len:
        return False
    return sum(body.count(c) for c in _SPLIT_END) >= 2


def tidy_output(text: str) -> str:
    """发出去前收拾一下：模型爱用空行当分段符，会变成真的空行；分段交给官方机制。"""
    if not text:
        return text
    if _CODE_BLOCK_PAT.search(text):
        # 有代码块就只压紧正文里的空行，不动代码块所在位置（避免把代码块搬到末尾）
        out: list[str] = []
        for piece in re.split(r"(`{3}.*?`{3})", text, flags=re.S):
            if piece.startswith("```"):
                out.append(piece)
            else:
                out.append(re.sub(r"\n{2,}", "\n", re.sub(r"[ \t]+\n", "\n", piece)))
        joined = "".join(out)
        return re.sub(r"\n{3,}", "\n", joined).strip()
    body = re.sub(r"[ \t]+\n", "\n", re.sub(r"\n{2,}", "\n", text.strip()))
    return body


# 代码块整段识别（```…```）：分条 / 压空行 / 错字注入都要跳过它，绝不拆代码。
_CODE_BLOCK_PAT = re.compile(r"```.*?```", re.S)
# 语义边界（真人发消息就是按这些断句）。注意：这两个常量曾被"清死代码"误删，
# 导致 strip_code_blocks / should_split / split_message / tidy_output 一调用就 NameError。
_SPLIT_END = "。！？!?…～~"


# ---------------- 错字拟人（output_typo_mode 的真实实现） ----------------
# 安全约束：只动中文段落；跳过代码块、URL、命令、@提及、长数字（QQ号/金额）。
# 档位必须与面板 select 及 _conf_schema.json 的 options 三选项一致：
#   light  = 整条最多 1 处，且一半概率完全不犯错
#   medium = 整条最多 2 处
#   wild   = 整条最多 3 处（仅作兼容别名保留，面板不暴露）
_CJK_PAT = re.compile(r"[\u4e00-\u9fff]{2,}")
_SKIP_PAT = re.compile(r"(https?://|www\.|/|\d{5,}|@|\w+\.\w{2,})", re.I)


def apply_typo(text: str, mode: str = "off", rate: float | None = None) -> str:
    """按档位给中文文本注入轻微错字（相邻字交换）。

    代码块原样保留；一行里含 URL/命令/长数字的不动；没有中文的整条不动。
    rate =「错字率」（output_typo_rate）：整条消息进入错字流程的概率（0–1）；
    传了它就由它当数值闸（rate=0 等于完全不动），不传则沿用各档内置概率。
    """
    if not text:
        return text
    mode = str(mode or "off").strip().lower()
    # 以前只认 light / wild —— 面板与 schema 给的是 off/light/medium，
    # 于是用户选 medium 被静默当成 off（控件看起来生效、实际关掉功能），
    # 而真正的 wild 又没有入口。这里以面板三选项为准，wild 降级为兼容别名。
    if mode not in ("light", "medium", "wild"):
        return text
    import random

    if rate is not None:
        try:
            _rate = float(rate)
        except (TypeError, ValueError):
            _rate = None
        if _rate is not None:
            if _rate <= 0:
                return text
            if random.random() >= min(_rate, 1.0):
                return text
    elif mode == "light" and random.random() < 0.5:
        return text  # light 档一半概率整条不犯错，更像真人状态波动
    max_swaps = {"light": 1, "medium": 2, "wild": 3}[mode]
    swaps = 0
    out_lines: list[str] = []
    # 代码块内部原样保留，只处理块外的行
    in_code = False
    for line in text.split("\n"):
        if line.strip().startswith("```"):
            in_code = not in_code
            out_lines.append(line)
            continue
        if in_code or swaps >= max_swaps or _SKIP_PAT.search(line):
            out_lines.append(line)
            continue
        candidates = [m for m in _CJK_PAT.finditer(line) if m.end() - m.start() >= 2]
        random.shuffle(candidates)
        for m in candidates:
            span = m.end() - m.start()
            start = m.start() + random.randint(0, span - 2)
            if line[start] == line[start + 1]:
                continue
            line = line[:start] + line[start + 1] + line[start] + line[start + 2:]
            swaps += 1
            break
        out_lines.append(line)
    return "\n".join(out_lines)


def split_message(text: str, *, max_seg: int = 6, min_len: int = 12) -> list[str]:
    """把一段回复拆成多条发出。

    **协议（让 bot 自己控制分段）**：bot 在两条之间**空一行** = 它想分一条发。
    所以：
      ① 先按空行切 —— 这是首选，bot 写得清楚就完全按它的意思来；
      ② 没有空行（或只有一段）时，才退回按语义边界（。！？…）兜底切，短片段并入相邻；
      ③ **绝不丢字**：超过 max_seg 的部分并入最后一条一起发（原实现是 `[:max_seg]` 直接截断，
         长回复第 5 段起的内容会被静默丢掉 —— 这是真 bug，不是风格问题）；
      ④ 绝不产生空白片段（空白片段在客户端就是"空一行/空气泡"）。
    代码块整段不拆，永远单独成条。
    """
    body, code = strip_code_blocks(text)
    segs: list[str] = []

    # ① 首选：bot 自己写的空行分段（\n\n / \n + 空白 + \n 都算）
    blocks = [b.strip() for b in re.split(r"\n\s*\n", body or "") if b.strip()]
    if len(blocks) > 1:
        segs = blocks
    else:
        # ② 兜底：按语义边界切（没有空行时的旧行为，保留）
        buf = ""
        for ch in body or "":
            buf += ch
            if ch in _SPLIT_END:
                piece = buf.strip()
                if len(piece) >= min_len:
                    segs.append(piece)
                    buf = ""
                elif segs:
                    segs[-1] += piece
                    buf = ""                                   # ⚠️ 必须清空：否则这句既并入上一条、
                                                               # 收尾时又被当成 tail 再拼一次（重复一句）
                else:
                    buf = piece
        tail = buf.strip()
        if tail:
            if segs and len(tail) < min_len:
                segs[-1] += tail
            else:
                segs.append(tail)

    if code:
        segs.append(code)

    # ③④ 收口：去掉空白片段；超出 max_seg 的合并进最后一条（不丢字）
    segs = [s for s in segs if s and s.strip()]
    if not segs:
        return [text] if text and text.strip() else []
    if len(segs) > max_seg:
        merged = segs[: max_seg - 1] + ["\n".join(segs[max_seg - 1:])]
        segs = merged
    return segs


def split_for_send(text: str, *, max_line: int = 12, max_seg: int = 6) -> tuple[list[str], str]:
    """发送层切段（主动通道用）：不依赖 AstrBot 的 result 管线，纯文本判定。

    主动消息走 `context.send_message()`，没有 event/result，`on_decorating_result`
    钩子永远收不到它 —— 所以「空行 = 分一条」的协议必须在这里自己兑现。

    三条协议，优先级从上到下（与回复路径同口径）：
      ① 空行协议：bot 自己写的空行，每段发什么、发多长全由它决定；
      ② 一行一条：整段没有空行、但每行都很短（1/2/3… 一条一个数字）时按单换行切；
      ③ 语义切分：前两条都不成立、且长度与断点够时才按标点边界兜底切。

    返回 (段列表, 依据)；不拆时返回 ([原文], "")，调用方照旧整条发。
    错字/收尾（apply_typo / tidy_output）由调用方逐段处理 —— 本函数只做切分，
    保证「先切后收拾」，不会因为 tidy_output 压掉空行而让协议失效。
    """
    raw = str(text or "")
    if not raw.strip():
        return [raw], ""
    # 代码块：整段交给 split_message —— 它先摘代码块再切正文，保证代码块不被空行切碎、绝不丢字
    if _CODE_BLOCK_PAT.search(raw):
        segs = split_message(raw)
        if len(segs) <= 1:
            return [raw], ""
        body, _code = strip_code_blocks(raw)
        body_blocks = [b.strip() for b in re.split(r"\n\s*\n", body) if b.strip()]
        return segs, ("空行协议" if len(body_blocks) >= 2 else "代码块")
    blocks = [b.strip() for b in re.split(r"\n\s*\n", raw) if b.strip()]
    if len(blocks) >= 2:
        # 无人值守通道的防洪：超过 max_seg 段就把余下的并进最后一条（合并用空行，不丢字）
        if len(blocks) > max_seg:
            blocks = blocks[: max_seg - 1] + ["\n\n".join(blocks[max_seg - 1:])]
        return blocks, "空行协议"
    lines = [ln.strip() for ln in raw.split("\n") if ln.strip()]
    if len(lines) >= 2 and max(len(ln) for ln in lines) <= max_line and "\n\n" not in raw:
        if len(lines) > max_seg:
            lines = lines[: max_seg - 1] + ["\n".join(lines[max_seg - 1:])]
        return lines, "一行一条"
    if should_split(raw):
        segs = split_message(raw)
        if len(segs) > 1:
            return segs, "语义切分"
    return [raw], ""

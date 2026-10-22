# -*- coding: utf-8 -*-
"""语言学习 v2：从记忆的原对话里学「对方这个人怎么说话」，注入他的话当口吻样本。

口径（用户 2026-10-21）：
- 对话先落成记忆，每条记忆带原对话（hf_memories.evidence：`用户：… / 助手：…`）。
- 学习 = 读这些原对话，把**对方**说过的句子收成样本，按「说话方式」归组。
- 学的是人说话，不是 bot 自己说过的话（自我模仿只会越学越像 AI）。
- 变旧不淘汰：只按 相关度 × 准确率 × 新近度 加权。
- 注入只投逐字原话（附记忆出处），不投「情景→说法」这类要模型解释的规则。
"""
from __future__ import annotations

import re
import time
from typing import Any

from astrbot.api import logger

from . import style_canon as canon_mod

MIN_MEMORIES = 1          # 至少有几条新记忆才跑（手动可传 1）
MAX_MEMORIES = 12         # 一次最多读多少条记忆（同对话记忆会先去重）
DROP_ACC_BELOW = 0.34     # 准确率低于这个且纠正 ≥2 才丢弃原话

CORRECTION_PAT = re.compile(
    r"(太长|话太多|话唠|话痨|啰嗦|啰唆|发一堆|一条一条|分开发|短点|短一点|"
    r"重复|又说|老说|别用句号|别加括号|不像你|机器人|客服|说人话|正常点)"
)
USER_LINE = re.compile(r"^\s*用户\s*[:：]\s*(.*)$")
BOT_LINE = re.compile(r"^\s*(?:助手|bot|BOT|Bot)\s*[:：]\s*(.*)$")

LEARN_SYSTEM = """你在帮一个聊天机器人学会「对面这个人平时怎么说话」。只输出 JSON。

输入是这个人自己发过的短句，每行 `编号 | 原话`。
给每一条起一个「说话方式」的名字（2–8 个汉字），描述他这句话是怎么说的，例如：
  短句直给、反问怼人、敷衍嗯哦、撒娇拖音、先骂后哄、认真讲道理、报点式、口头禅式。
实在没有复用价值的（命令、链接、报错、复述别人、纯表情、纯数字）写「跳过」。

输出（严格 JSON，不要解释、不要 markdown）：
{"1":"短句直给","2":"跳过","3":"反问怼人"}
"""


def _split_role_lines(evidence: str) -> list[dict[str, Any]]:
    """把 evidence 拆成角色段落：用户 / 助手，各自按空行分气泡。"""
    segs: list[dict[str, Any]] = []
    cur_role, cur_lines = "", []
    for raw in str(evidence or "").replace("\r", "").split("\n"):
        m_u, m_b = USER_LINE.match(raw), BOT_LINE.match(raw)
        if m_u or m_b:
            if cur_role and cur_lines:
                segs.append({"role": cur_role, "text": "\n".join(cur_lines).strip()})
            cur_role = "user" if m_u else "bot"
            cur_lines = [(m_u or m_b).group(1)]
        elif cur_role:
            cur_lines.append(raw)
    if cur_role and cur_lines:
        segs.append({"role": cur_role, "text": "\n".join(cur_lines).strip()})
    out = []
    for s in segs:
        bubbles = [b.strip() for b in re.split(r"\n\s*\n", s["text"]) if b.strip()]
        if bubbles:
            out.append({"role": s["role"], "bubbles": bubbles})
    return out


def human_lines_from_memory(evidence: str) -> list[str]:
    """一条记忆里「对方这个人」说过的句子（学的是人说话，不是 bot 自己）。"""
    out: list[str] = []
    for seg in _split_role_lines(evidence):
        if seg["role"] != "user":
            continue
        for b in seg["bubbles"]:
            t = canon_mod.clean(b).strip()
            if t:
                out.append(t)
    return out


def turns_from_memory(evidence: str) -> list[dict[str, Any]]:
    """一条记忆 → 若干回合：触发（用户）→ 助手气泡 → 下一句用户（可能没有）。"""
    segs = _split_role_lines(evidence)
    turns = []
    for i, s in enumerate(segs):
        if s["role"] != "bot":
            continue
        trigger = ""
        for j in range(i - 1, -1, -1):
            if segs[j]["role"] == "user":
                trigger = segs[j]["bubbles"][-1]
                break
        nxt = ""
        for j in range(i + 1, len(segs)):
            if segs[j]["role"] == "user":
                nxt = segs[j]["bubbles"][0]
                break
        fixed = bool(nxt and len(nxt) <= 40 and CORRECTION_PAT.search(nxt))
        turns.append({
            "line": len(turns),
            "trigger": trigger[:200],
            "bubbles": [b[:120] for b in s["bubbles"]][:6],
            "next_user": nxt[:200],
            "fixed": fixed,
        })
    return turns


def _user_name(mem: dict[str, Any]) -> str:
    try:
        ents = str(mem.get("entities") or "[]")
        import json
        arr = json.loads(ents)
        if isinstance(arr, list) and arr:
            return str(arr[0])[:24]
    except Exception:                                       # noqa: BLE001
        pass
    return ""


def build_prompt(mems: list[dict[str, Any]]) -> str:
    lines = []
    for idx, m in enumerate(mems, 1):
        turns = turns_from_memory(m.get("evidence") or "")
        if not turns:
            continue
        flat = []
        for t in turns:
            flat.append(" | ".join(t["bubbles"]))
        body = f"#{idx} | 用户：{turns[0]['trigger']} | 助手：{' / '.join(flat)}"
        lines.append(body[:400])
    return "\n".join(lines)


def parse_learn(raw: str) -> dict[str, Any]:
    s = str(raw or "").strip().strip("`")
    if s.lower().startswith("json"):
        s = s[3:].strip()
    i, j = s.find("{"), s.rfind("}")
    if i < 0 or j <= i:
        return {"picks": [], "terms": []}
    try:
        import json
        data = json.loads(s[i:j + 1])
    except Exception:                                       # noqa: BLE001
        return {"picks": [], "terms": []}
    picks = []
    for p in data.get("picks") or []:
        try:
            picks.append({
                "i": int(p.get("i")),
                "line": int(p.get("line") or 0),
                "style": str(p.get("style") or "接话")[:12],
                "fixed": bool(p.get("fixed")),
            })
        except (TypeError, ValueError):
            continue
    terms = []
    for t in data.get("terms") or []:
        term = str(t.get("term") or "").strip()[:16]
        if len(term) < 2:
            continue
        terms.append({
            "term": term,
            "kind": str(t.get("kind") or "tic") if str(t.get("kind") or "tic") in ("tic", "slang", "address") else "tic",
            "usage": str(t.get("usage") or "")[:60],
            "meaning": str(t.get("meaning") or "")[:60],
        })
    return {"picks": picks, "terms": terms}


def _register(session_id: str) -> str:
    return "group" if "Group" in str(session_id) else "private"


async def run_learn(main, cfg: dict[str, Any], persona_id: str = "") -> dict[str, Any]:
    """新记忆 → 代码挑出对方说过的话 → 模型只起「说话方式」名字 → 入库。

    为什么不让模型挑下标：真机上模型连续返回 `{"picks":[]}`（它不愿意按行号挑），
    而「哪些话是她说的、多长算短」其实代码能判。模型只做它擅长的：给这些原话归类命名。
    """
    store, svc = main.store, main.service
    stat = {"ok": False, "fresh": 0, "cands": 0, "saved": 0, "styles": 0,
            "terms": 0, "fixed": 0, "reason": ""}
    try:
        wm = int(float(await store.get_config("style_mem_wm", 0) or 0))
    except (TypeError, ValueError):
        wm = 0
    new_mems = await store.list_memories_with_evidence(wm, limit=MAX_MEMORIES, persona_id=persona_id)
    mems = list(new_mems)
    backfill = False
    # 新记忆不够一批（常见：只有一条画像/总结）就往前回填一页旧对话
    if len(mems) < 4:
        try:
            back = int(float(await store.get_config("style_mem_back", 0) or 0))
        except (TypeError, ValueError):
            back = 0
        if not back:
            back = await store.max_memory_id() + 1
        more = await store.list_memories_before(back, limit=MAX_MEMORIES, persona_id=persona_id)
        if more:
            seen_ids = {int(m["id"]) for m in mems}
            mems += [m for m in more if int(m["id"]) not in seen_ids]
            backfill = True
        else:
            await store.set_config("style_mem_back", 0)
    if not mems:
        stat["reason"] = "no_new_memory"
        return stat
    # 同一次对话会落好几条记忆（evidence 完全相同）→ 按原对话去重
    seen_ev: set[str] = set()
    uniq = []
    for m in mems:
        key = str(m.get("evidence") or "").strip()
        if not key or key in seen_ev:
            continue
        seen_ev.add(key)
        uniq.append(m)
    mems = uniq
    stat["fresh"] = len(mems)
    if not mems:
        stat["reason"] = "no_new_memory"
        return stat

    # 学的是「对方这个人」怎么说话，不是 bot 自己怎么说话——拿 bot 的旧回复回来喂它，
    # 只会自我模仿、越学越像 AI。每个记忆里取出对方说过的每一句当候选。
    cands: list[dict[str, Any]] = []
    for m in mems:
        for line in human_lines_from_memory(m.get("evidence") or ""):
            t = line.strip()
            if not (4 <= len(t) <= 120):
                continue
            if not re.search(r"[\u4e00-\u9fff]", t):
                continue
            if canon_mod.looks_like_bot_meta(t):
                continue
            if t.startswith("/") or "http://" in t or "https://" in t:
                continue
            cands.append({"mem": m, "quote": t})
    # 同一条回复只留一次；库里已有的也不再收（跨批去重）
    seen_q: set[str] = {str(q.get("quote") or "").strip() for q in await store.list_quotes(persona_id)}
    cands = [c for c in cands if not (c["quote"] in seen_q or seen_q.add(c["quote"]))]
    stat["cands"] = len(cands)
    if not cands:
        stat["reason"] = "no_candidate"
        if new_mems:
            await store.set_config("style_mem_wm", max(int(m["id"]) for m in new_mems))
        if backfill:
            await store.set_config("style_mem_back", min(int(m["id"]) for m in mems))
        return stat

    listing = "\n".join(
        f"{i}. 记忆#{int(c['mem'].get('id') or 0)} | {c['quote'].replace(chr(10), ' / ')}" for i, c in enumerate(cands, 1)
    )
    existing = [str(x.get("name") or "") for x in await store.list_styles(persona_id)]
    existing = [x for x in existing if x]
    prompt = (
        ("现有的说话方式（优先复用，不要造近义词）：" + "、".join(existing[:20]) + "\n"
         "都不合适才新建，整批最多新建 3 种。名字太泛（随口说 / 其他 / 日常）就把里面最特别的那几条单独起个更具体的名字，不要全塞进一个。\n\n") if existing else
        "还没有说话方式的分类，给这批句子起名（整批最多 3 种）。\n\n"
    ) + "他说过的话：\n" + listing
    gen = svc.system.get("llm_generate")
    if not callable(gen):
        stat["reason"] = "no_llm"
        return stat
    try:
        raw = await gen(prompt, "expression_extract", LEARN_SYSTEM,
                        umo=str(mems[0].get("session_id") or ""), timeout=180)
    except Exception as exc:                                # noqa: BLE001
        stat["reason"] = f"llm_error:{exc}"[:120]
        return stat
    names = parse_names(raw)
    stat["picked"] = len(names)
    if not names:
        stat["reason"] = "empty_llm_output" if not str(raw or "").strip() else "model_named_nothing"
        return stat

    used_styles: set[str] = set()
    new_names: list[str] = []
    for i, c in enumerate(cands, 1):
        name = _match_name(str(names.get(str(i)) or "").strip(), existing)
        if not name or name in ("跳过", "skip", "-"):
            continue
        if name not in existing and name not in new_names:
            if len(new_names) >= 3:
                name = _closest_name(name, existing) or "其他"
            else:
                new_names.append(name)
        sid = await store.upsert_style(persona_id, name)
        if not sid:
            continue
        if name not in used_styles:
            used_styles.add(name)
            stat["styles"] += 1
        qid = await store.upsert_quote({
            "style_id": sid, "persona_id": persona_id, "quote": c["quote"],
            "trigger_text": "", "memory_id": int(c["mem"].get("id") or 0),
            "session_id": str(c["mem"].get("session_id") or ""),
            "user_id": str(c["mem"].get("subject_id") or ""),
            "user_name": _user_name(c["mem"]),
            "register": _register(str(c["mem"].get("session_id") or "")),
            "corrections": 0,
        })
        if qid:
            stat["saved"] += 1
    stat["ok"] = stat["saved"] > 0
    if not stat["ok"]:
        stat["reason"] = "model_named_nothing"
        # 这批没新增也要往前走，否则会卡在同一页
        if new_mems:
            await store.set_config("style_mem_wm", max(int(m["id"]) for m in new_mems))
        if backfill:
            await store.set_config("style_mem_back", min(int(m["id"]) for m in mems))
        return stat
    await store.prune_empty_styles(persona_id)
    if new_mems:
        await store.set_config("style_mem_wm", max(int(m["id"]) for m in new_mems))
    if backfill:
        await store.set_config("style_mem_back", min(int(m["id"]) for m in mems))
    logger.info(f"[StarHeart] 语言学习：{stat}")
    return stat


def _canon_name(name: str) -> str:
    return re.sub(r"(一句|一下|的|了|说法)$", "", str(name or "").strip())


def _match_name(name: str, existing: list[str]) -> str:
    cn = _canon_name(name)
    for e in existing:
        ce = _canon_name(e)
        if cn and ce and (cn == ce or cn in ce or ce in cn):
            return e
    return name


def _closest_name(name: str, existing: list[str]) -> str:
    best, score = "", 0.0
    for e in existing:
        sc = canon_mod.similar(name, e)
        if sc > score:
            best, score = e, sc
    return best if score >= 0.34 else ""


def parse_names(raw: str) -> dict[str, str]:
    """解析模型给的 `{"1":"嘴硬一句", ...}`（容忍 md/json 包裹）。"""
    t = str(raw or "").strip().strip("`")
    if t.lower().startswith("json"):
        t = t[3:].strip()
    i, j = t.find("{"), t.rfind("}")
    if i < 0 or j <= i:
        return {}
    try:
        import json
        data = json.loads(t[i:j + 1])
    except Exception:                                       # noqa: BLE001
        return {}
    out = {}
    for k, v in (data or {}).items():
        name = str(v or "").strip()[:12]
        if name and str(k).isdigit():
            out[str(k)] = name
    return out


def _accuracy(hits: int, corrections: int) -> float:
    return (float(hits) + 1.0) / (float(hits) + float(corrections) + 2.0)


# 人格里搬出来的那套说话方式（默认表达块）。
# 学不到东西时用它；一旦学到原话，整块换成学到的内容（用户定的规则）。
# 表达块的底线：长度与敷衍（任何模式都带 —— 学到的原话只补语气，不接管长度）。
# 旧文案写「一条尽量二十字以内」，模型会理解成「每条 20 字、可以很多条」，
# 于是每轮都排好几拍短句（真机 reasoning 原文：短句 空格 不用逗号 … 注意别太长，每条20字内）。
BREVITY_RULE = (
    "默认一句就够 十几字以内 说完就停 别一口气说好几件事 也别把一句话拆成几条\n"
    "能敷衍就敷衍 嗯 哦 6 行 随便 都行 不解释 不追问 不总结 不写旁白 不写括号动作"
)

# 默认的表达块（没学到原话时用）。
DEFAULT_RULES = BREVITY_RULE + "\n" + "段内不用逗号 用空格隔开 偶尔用～拖音"


async def expression_block(store, cfg: dict[str, Any], persona_id: str,
                           register: str, current_msg: str, dry: bool = False) -> str:
    """人格之后、system 最末尾的表达块：默认规则，或整个换成学到的原话 + 黑话。

    dry=True 只编译、不记账（面板预览用）：预览不能改变命中与冷却。
    """
    learned = bool(cfg.get("expression_style_enabled", False))
    picked: list[dict[str, Any]] = []
    part_names: list[str] = []
    parts: list[str] = []
    if learned:
        quotes = await store.list_quotes(persona_id, status="active")
        if quotes:
            pool = [q for q in quotes if str(q.get("register") or "private") == register] or quotes
            for q in sorted(pool, key=lambda x: float(x.get("created_at") or 0), reverse=True):
                text = str(q.get("quote") or "").strip()
                if not text:
                    continue
                # 对方刚说的这句不要再原样摆回去
                if current_msg and canon_mod.similar(text, str(current_msg or "")) >= 0.72:
                    continue
                picked.append(q)
                if len(picked) >= 2:
                    break
            # 「说话方式」就是学来的每句原话的提炼结果，注入时带上
            try:
                names = {int(s["id"]): str(s.get("name") or "") for s in await store.list_styles(persona_id)}
                for q in picked:
                    nm = names.get(int(q.get("style_id") or 0)) or ""
                    if nm and nm not in part_names:
                        part_names.append(nm)
            except Exception:                                   # noqa: BLE001
                part_names = part_names[:0]
    if picked:
        lines = "\n".join(
            f"- 「{str(q.get('quote') or '').replace(chr(10), ' / ')[:60]}」" for q in picked
        )
        head = "【他平时的说话口吻】"
        if part_names:
            head += "他的几种习惯：" + "、".join(part_names[:4]) + "。"
        parts.append(BREVITY_RULE)
        parts.append(
            head + "下面是**他过去**说过的句子，只学他的语气、句式和用词；"
            "不是他现在说的话，也不要把这些句子回给他（别逐字复刻，别用客服腔）：\n" + lines
        )
    else:
        parts.append(DEFAULT_RULES)
    terms = await store.list_style_terms(persona_id, scope="", limit=20)
    hits = []
    for t in terms:
        term = str(t.get("term") or "")
        if term and term in str(current_msg or ""):
            extra = str(t.get("meaning") or t.get("usage") or "")[:24]
            hits.append(f"「{term}」{('= ' + extra) if extra else ''}")
    if hits:
        parts.append("【黑话】" + "；".join(hits[:4]))
    block = "\n".join(parts)
    if len(block) > 460:
        block = block[:459] + "…"
    if picked and not dry:
        try:
            for q in picked:
                await store.bump_quote(int(q["id"]), hits=1)
                await store.bump_style(int(q.get("style_id") or 0), hits=1)
        except Exception:                                   # noqa: BLE001
            pass
    return block


async def style_block(store, cfg: dict[str, Any], persona_id: str,
                      register: str, current_msg: str) -> str:
    """兼容旧调用名。"""
    return await expression_block(store, cfg, persona_id, register, current_msg)

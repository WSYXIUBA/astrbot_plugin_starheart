# -*- coding: utf-8 -*-
"""「人格与表达」读模型 / 动作适配层。

设计原则：不新造学习引擎，也不复制一份数据。这里只做三件事：
1. 把现有几套学习产物（hf_expr / hf_style_* / hf_jargon / hf_persona_proposals）
   收成面板四个页签需要的形状；
2. 给每条产物补「来处」：原对话按 memory_id / 命中日志回到 hf_messages，
   回不到就明说「已清理」，不拿记忆正文冒充原话；
3. 写动作全部转发到已有 service 方法，保持原状态机与审计不变。
"""
from __future__ import annotations

import asyncio
import difflib
import hashlib
import json
import re
import time
from typing import Any

from astrbot.api import logger

from . import style_memory as style_mod


def _json_list(raw: Any) -> list[Any]:
    if isinstance(raw, list):
        return raw
    try:
        val = json.loads(str(raw or "[]"))
        return val if isinstance(val, list) else []
    except Exception:                                       # noqa: BLE001
        return []


def _clip(text: Any, n: int = 200) -> str:
    s = str(text or "").replace("\r", "").strip()
    return s if len(s) <= n else s[: max(0, n - 1)] + "…"


def _hash(text: str) -> str:
    return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()[:12]


def _diff(base: str, proposal: str, limit: int = 80) -> list[str]:
    try:
        lines = list(difflib.unified_diff(
            str(base or "").splitlines(), str(proposal or "").splitlines(),
            fromfile="当前", tofile="提案", lineterm="", n=1,
        ))
    except Exception:                                       # noqa: BLE001
        return []
    return lines[:limit]


def _sbs(base: str, proposal: str, limit: int = 240) -> list[dict[str, Any]]:
    """左右并排 diff：左=当前人格，右=更新后。

    每行给两侧各自的状态，前端才好把左边删掉的画红、右边新增的画绿：
    lt/rt ∈ same | del | ins | none（none = 这一侧没有对应行）。
    """
    def _lines(t: Any) -> list[str]:
        return str(t or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")

    a, b = _lines(base), _lines(proposal)
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    rows: list[dict[str, Any]] = []

    def _push(lt: str, l: str, ln: int, rt: str, r: str, rn: int) -> None:
        rows.append({"lt": lt, "l": l, "ln": ln, "rt": rt, "r": r, "rn": rn})

    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                _push("same", a[i1 + k], i1 + k + 1, "same", b[j1 + k], j1 + k + 1)
        elif tag == "delete":
            for k in range(i2 - i1):
                _push("del", a[i1 + k], i1 + k + 1, "none", "", 0)
        elif tag == "insert":
            for k in range(j2 - j1):
                _push("none", "", 0, "ins", b[j1 + k], j1 + k + 1)
        else:                                               # replace：逐行配对，多出的那侧留空
            n = max(i2 - i1, j2 - j1)
            for k in range(n):
                li = i1 + k if i1 + k < i2 else -1
                rj = j1 + k if j1 + k < j2 else -1
                _push(
                    "del" if li >= 0 else "none", a[li] if li >= 0 else "", li + 1 if li >= 0 else 0,
                    "ins" if rj >= 0 else "none", b[rj] if rj >= 0 else "", rj + 1 if rj >= 0 else 0,
                )
    return rows[: max(1, int(limit))]


_EVID_RE = re.compile(r"^\s*(用户|助手|bot|Bot|BOT)\s*[:：]\s*(.*)$")


def _messages_from_text(text: str, bot_name: str = "助手") -> list[dict[str, Any]]:
    """把 `用户：… / 助手：…` 的 evidence 拆成气泡。

    多气泡回复在 evidence 里是空行分隔的续行（`助手：在\\n\\n昨天问一遍…`），
    续行必须并回同一条，否则「学到的是这句」只显示第一行。
    """
    out: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None
    for line in str(text or "").replace("\r", "").splitlines():
        m = _EVID_RE.match(line)
        if m:
            is_bot = m.group(1) in ("助手", "bot", "Bot", "BOT")
            cur = {
                "who": bot_name if is_bot else "对方",
                "side": "bot" if is_bot else "user",
                "text": m.group(2).strip(),
                "ts": 0,
                "id": "",
            }
            out.append(cur)
        elif cur is not None and line.strip():
            cur["text"] = (str(cur["text"]) + "\n" + line.strip()).strip()
    for m in out:
        m["text"] = re.sub(r"\[好感[:：][^\]]*\]", "", str(m.get("text") or "")).strip()
    return [m for m in out if m["text"]]


class ActorView:
    def __init__(self, service) -> None:
        self.svc = service
        self.store = service.store

    # ---------------- 公共 ----------------
    async def _pid(self, pid: str = "") -> str:
        if pid:
            return str(pid)
        try:
            people = await self.svc.personas()
            return str(people.get("active") or "")
        except Exception:                                   # noqa: BLE001
            return ""

    async def _persona(self, pid: str = "") -> dict[str, Any]:
        people = await self.svc.personas()
        items = people.get("items") or []
        active = str(people.get("active") or "")
        want = str(pid or active)
        cur = next((x for x in items if str(x.get("persona_id") or "") == want), None)
        if cur is None and items:
            cur = items[0]
        prompt = str((cur or {}).get("system_prompt") or "")
        return {
            "id": str((cur or {}).get("persona_id") or want or ""),
            "chars": len(prompt),
            "hash": _hash(prompt) if prompt else "",
            "prompt": prompt,
            "items": items,
            "active": active,
            "isolated": bool(people.get("isolated")),
        }

    async def _bot_name(self) -> str:
        """当前人格名（面板里代替写死的「小洛」）。"""
        try:
            people = await self.svc.personas()
            return str(people.get("active") or "助手") or "助手"
        except Exception:                                   # noqa: BLE001
            return "助手"

    # ---------------- 总览 ----------------
    async def overview(self) -> dict[str, Any]:
        cfg = await self.svc.config()
        persona = await self._persona()
        expr = await self.svc.expr_review_queue(1)
        jar = await self.svc.jargon_list()
        props = await self.store.list_proposals(100)
        styles = await self.store.list_styles(persona["id"])
        quotes = await self.store.list_quotes(persona["id"])
        terms = await self.store.list_style_terms(persona["id"], limit=200)
        pending_props = [x for x in props if str(x.get("status") or "") == "pending"]
        last_ts = 0.0
        for s in styles:
            last_ts = max(last_ts, float(s.get("updated_at") or s.get("created_at") or 0))
        for q in quotes:
            last_ts = max(last_ts, float(q.get("created_at") or 0))
        used_today = 0
        try:
            start_ts = self.svc._day_start_ts(cfg)
            row = await (await self.store._db.execute(
                "SELECT COUNT(*) AS n FROM hf_expr_use_log WHERE used_at>=?", (start_ts,)
            )).fetchone()
            used_today = int(row["n"] or 0) if row else 0
        except Exception:                                   # noqa: BLE001
            used_today = 0
        return {
            "ok": True,
            "persona": {
                "id": persona["id"], "chars": persona["chars"], "hash": persona["hash"],
                "active": persona["active"], "isolated": persona["isolated"],
            },
            "counts": {
                "pending": int((expr.get("counts") or {}).get("pending") or 0)
                + int((jar.get("counts") or {}).get("pending") or 0) + len(pending_props),
                "pending_expr": int((expr.get("counts") or {}).get("pending") or 0),
                "pending_jargon": int((jar.get("counts") or {}).get("pending") or 0),
                "pending_persona": len(pending_props),
                "styles": len(styles), "quotes": len(quotes), "terms": len(terms),
            },
            "quota": {
                "inject_cap": int(cfg.get("output_expr_max_inject") or 5),
                "used_today": used_today,
            },
            "learning": {
                "enabled": bool(cfg.get("expression_style_enabled")),
                "mode": str(cfg.get("expression_style_inject_mode") or "legacy"),
                "last_at": last_ts,
            },
        }

    # ---------------- 审核队列 ----------------
    async def review(self, limit: int = 100) -> dict[str, Any]:
        expr = await self.svc.expr_review_queue(limit)
        jar = await self.svc.jargon_list(status="pending")
        props = await self.store.list_proposals(limit)
        items: list[dict[str, Any]] = []
        for r in expr.get("items") or []:
            cues = [str(x) for x in _json_list(r.get("cues"))][:4]
            preview = []
            for x in _json_list(r.get("evidence_snap"))[:3]:
                if isinstance(x, dict):
                    preview.append({
                        "who": str(x.get("speaker") or x.get("role") or "对方"),
                        "side": "bot" if str(x.get("role") or "") in ("assistant", "bot") else "user",
                        "text": _clip(x.get("text") or x.get("content"), 160),
                    })
                elif isinstance(x, str):
                    preview.extend(_messages_from_text(x)[:2])
            items.append({
                "kind": "expr", "id": int(r.get("id") or 0), "type": "表达",
                "title": str(r.get("way") or ""), "subtitle": str(r.get("scene") or ""),
                "body": " · ".join(cues)[:240], "scope": str(r.get("scope") or ""),
                "created_at": float(r.get("created_at") or 0),
                "status": str(r.get("checked_state") or "ai_pending"),
                "reason": _clip(r.get("ai_reason"), 200),
                "preview": preview[:2],
            })
        for r in jar.get("items") or []:
            if str(r.get("status") or "") != "pending":
                continue
            items.append({
                "kind": "jargon", "id": int(r.get("id") or 0), "type": "群内用语",
                "title": str(r.get("term") or ""),
                "subtitle": str(r.get("meaning") or r.get("usage") or ""),
                "body": _clip(r.get("raw_context") or r.get("usage"), 200),
                "scope": str(r.get("scope") or ""),
                "created_at": float(r.get("created_at") or r.get("first_seen_at") or 0),
                "status": "pending", "reason": "",
            })
        pending_props = [r for r in props if str(r.get("status") or "") == "pending"]
        items.sort(key=lambda x: float(x.get("created_at") or 0), reverse=True)
        counts = {
            "total": len(items),
            "expr": sum(1 for x in items if x["kind"] == "expr"),
            "jargon": sum(1 for x in items if x["kind"] == "jargon"),
            # 人格提案不在这一栏处理（在「人格」页，带左右对比），这里只给待审数当入口提示
            "persona": len(pending_props),
        }
        return {"ok": True, "items": items[: max(1, min(int(limit), 500))], "counts": counts}

    # ---------------- 表达库 ----------------
    async def voice(self, persona_id: str = "") -> dict[str, Any]:
        pid = await self._pid(persona_id)
        styles = await self.store.list_styles(pid)
        quotes = await self.store.list_quotes(pid)
        rows = await (await self.store._db.execute(
            "SELECT * FROM hf_style_terms WHERE persona_id=? OR persona_id='' ORDER BY hits DESC, id DESC",
            (pid,),
        )).fetchall()
        terms = [dict(r) for r in rows]
        # 群内用语（hf_jargon，人工闸门过的黑话）：和「口头禅（hf_style_terms）」是两套东西，
        # 面板分开显示，避免同名两义。
        jargon: list[dict[str, Any]] = []
        try:
            jar = await self.svc.jargon_list(status="confirmed")
            for it in jar.get("items") or []:
                jargon.append({
                    "id": int(it.get("id") or 0),
                    "term": str(it.get("term") or ""),
                    "meaning": str(it.get("meaning") or ""),
                    "usage": str(it.get("usage") or ""),
                    "scope": str(it.get("scope") or ""),
                    "status": str(it.get("status") or ""),
                    "enabled": bool(it.get("enabled")),
                    "hit_count": int(it.get("hit_count") or 0),
                })
        except Exception as exc:                            # noqa: BLE001 群内用语读失败不影响其它段
            logger.debug(f"[StarHeart] 群内用语读取跳过：{exc}")
        by_style: dict[int, list[dict[str, Any]]] = {}
        for q in quotes:
            by_style.setdefault(int(q.get("style_id") or 0), []).append({
                "id": int(q.get("id") or 0),
                "quote": str(q.get("quote") or ""),
                "trigger_text": str(q.get("trigger_text") or ""),
                "register": str(q.get("register") or "private"),
                "memory_id": int(q.get("memory_id") or 0),
                "user_name": str(q.get("user_name") or ""),
                "user_id": str(q.get("user_id") or ""),
                "session_id": str(q.get("session_id") or ""),
                "hits": int(q.get("hits") or 0),
                "corrections": int(q.get("corrections") or 0),
                "status": str(q.get("status") or "active"),
                "created_at": float(q.get("created_at") or 0),
            })
        out_styles = []
        for s in styles:
            qs = by_style.get(int(s.get("id") or 0), [])
            out_styles.append({
                "id": int(s.get("id") or 0), "name": str(s.get("name") or ""),
                "status": str(s.get("status") or "active"),
                "hits": int(s.get("hits") or 0),
                "corrections": int(s.get("corrections") or 0),
                "quotes": qs,
                "active": sum(1 for q in qs if q["status"] == "active"),
            })
        unattributed = await (await self.store._db.execute(
            "SELECT COUNT(*) AS n FROM hf_style_quotes WHERE COALESCE(persona_id,'')=''"
        )).fetchone()
        return {
            "ok": True,
            "persona_id": pid,
            "styles": out_styles,
            "terms": terms,
            "jargon": jargon,
            "counts": {
                "styles": len(out_styles),
                "quotes": sum(len(v) for v in by_style.values()),
                "terms": len(terms),
                "jargon": len(jargon),
                "unattributed": int(unattributed["n"] or 0) if unattributed else 0,
            },
        }

    # ---------------- 人格 ----------------
    async def personas(self) -> dict[str, Any]:
        people = await self.svc.personas()
        items = []
        for x in people.get("items") or []:
            prompt = str(x.get("system_prompt") or "")
            items.append({
                "id": str(x.get("persona_id") or ""),
                "prompt": prompt,
                "chars": len(prompt),
                "hash": _hash(prompt) if prompt else "",
                "active": str(x.get("persona_id") or "") == str(people.get("active") or ""),
                "begin_dialogs": x.get("begin_dialogs") or [],
            })
        return {
            "ok": True, "items": items, "active": str(people.get("active") or ""),
            "isolated": bool(people.get("isolated")),
        }

    async def revisions(self, persona_id: str = "") -> dict[str, Any]:
        props = await self.store.list_proposals(100)
        live_pid, live_prompt = "", ""
        try:
            people = await self.svc.personas()
            want = str(persona_id or people.get("active") or "")
            cur = next((x for x in people.get("items") or []
                        if str(x.get("persona_id") or "") == want), None)
            live_pid = str((cur or {}).get("persona_id") or want)
            live_prompt = str((cur or {}).get("system_prompt") or "")
        except Exception:                                   # noqa: BLE001 读不到就退回提案基线
            pass
        items = []
        for x in props:
            pid = str(x.get("persona_id") or "")
            if persona_id and pid != str(persona_id):
                continue
            base = str(x.get("base") or "")
            prop = str(x.get("proposal") or "")
            left = live_prompt if (live_prompt and pid == live_pid) else base
            items.append({
                "id": int(x.get("id") or 0), "persona_id": pid,
                "ts": float(x.get("ts") or 0), "status": str(x.get("status") or ""),
                "rationale": _clip(x.get("rationale"), 600),
                "base_len": len(left), "proposal_len": len(prop),
                "diff": _diff(left, prop, 60),
                "sbs": _sbs(left, prop, 40),
                "base_stale": bool(live_prompt and pid == live_pid and base and base != live_prompt),
                "added": max(0, len(prop) - len(left)),
            })
        return {"ok": True, "items": items}

    # ---------------- 来处 ----------------
    async def provenance(self, kind: str, ident: int) -> dict[str, Any]:
        k = str(kind or "").lower()
        if k == "quote":
            return await self._prov_quote(ident)
        if k == "style":
            return await self._prov_style(ident)
        if k == "expr":
            return await self._prov_expr(ident)
        if k == "jargon":
            return await self._prov_jargon(ident)
        if k == "term":
            return await self._prov_term(ident)
        if k == "persona":
            return await self._prov_persona(ident)
        return {"ok": False, "error": f"unknown kind: {kind}"}

    async def _prov_quote(self, qid: int) -> dict[str, Any]:
        q = await self.store.get_style_quote(int(qid))
        if not q:
            return {"ok": False, "error": "quote not found"}
        mid = int(q.get("memory_id") or 0)
        bot = await self._bot_name()
        ev = await self.svc.memory_evidence(str(mid)) if mid else {"ok": False}
        around = (ev or {}).get("around") or []
        # 记忆内原对话（带 bot 侧气泡）优先：hf_messages 只存用户消息，
        # 只拿 around 会把「学到的是哪句」弄丢。around 只补前后文。
        ev_msgs = _messages_from_text((ev or {}).get("dialogue") or (ev or {}).get("source_text") or "", bot)
        around_msgs = [m for m in (self._msg(r, bot) for r in around) if str(m.get("text") or "").strip()]
        # 人名统一：evidence 里只有「对方」，around 里有真实昵称，用它对齐
        uname = next((str(m.get("who") or "") for m in around_msgs
                      if m.get("side") == "user" and str(m.get("who") or "") not in ("", "对方")), "")
        if uname:
            for m in ev_msgs:
                if m.get("side") == "user":
                    m["who"] = uname
        # 主对话只用记忆里那一段（学到的就是它）；会话其余消息另放 context，
        # 混在同一个列表里会看成「她在我回过之后又说了一遍」，对不上。
        msgs = ev_msgs
        seen_text = {str(m.get("text") or "").strip() for m in ev_msgs}
        context = [m for m in around_msgs if str(m.get("text") or "").strip() not in seen_text]
        picked = None
        quote = str(q.get("quote") or "")
        style_row = await self.store.get_style_row(int(q.get("style_id") or 0)) if q.get("style_id") else None
        style_name = str((style_row or {}).get("name") or "")
        if not msgs:
            msgs, context = context, []
        for m in msgs:
            if m["side"] == "user" and quote and quote in m["text"]:
                m["picked"] = True
                picked = m
                break
        session = str(q.get("session_id") or (ev or {}).get("session_id") or "")
        conv_count = await self.store.message_count(session) if session else 0
        note = str((ev or {}).get("note") or "")
        if not msgs:
            note = note or ("原对话已清理：这条记忆只留下摘要，回不到逐句对话" if mid else "没有记录原对话")
        return {
            "ok": True, "kind": "quote", "id": int(qid), "type": "原话",
            "title": quote, "subtitle": f"{q.get('register') or 'private'} · 记忆 #{mid}",
            "state": "生效中" if str(q.get("status") or "") == "active" else "已停用",
            "meta": f"命中 {int(q.get('hits') or 0)} · 纠正 {int(q.get('corrections') or 0)}",
            "steps": [
                {"ts": int(float(q.get("created_at") or 0)), "label": "落库",
                 "note": f"归入「{style_name or '说话方式'}」；记忆 #{mid}"},
                # 只有 created_at 是真时间戳；抽取/过闸/状态没有独立时间，留空不编时间
                {"ts": 0, "label": "抽取",
                 "note": f"触发：{_clip(q.get('trigger_text'), 60) or '—'}"},
                {"ts": 0, "label": "过闸",
                 "note": "对方说的原话 · 跨批去重 · 人格隔离"},
                {"ts": 0, "label": "状态",
                 "note": str(q.get("status") or "active")},
            ],
            "messages": msgs,
            "context": context,
            "conv": f"会话 {session or '—'} · {conv_count} 条" if session else "",
            "note": note,
            "why": [
                f"只收对方说过的句子；长度 {len(quote)} 字。",
                f"来源记忆 #{mid}；触发原话已保留。" if mid else "没有来源记忆。",
                f"命中 {int(q.get('hits') or 0)} / 纠正 {int(q.get('corrections') or 0)}。",
            ],
            "source": {"memory_id": mid, "session_id": session,
                       "conversation_id": str((ev or {}).get("id") or ""),
                       "pruned": not bool(msgs)},
        }

    async def _prov_style(self, sid: int) -> dict[str, Any]:
        style = await self.store.get_style_row(int(sid))
        if not style:
            return {"ok": False, "error": "style not found"}
        quotes = await self.store.list_quotes(style_id=int(sid))
        bot = await self._bot_name()
        sessions: set[str] = set()
        threads: list[dict[str, Any]] = []
        seen_mem: set[int] = set()
        for q in quotes:
            session = str(q.get("session_id") or "")
            if session:
                sessions.add(session)
            mid = int(q.get("memory_id") or 0)
            if not mid or mid in seen_mem or len(threads) >= 6:
                continue
            seen_mem.add(mid)
            mem = await self.store.get_memory(mid) or {}
            ev = await self.svc.memory_evidence(str(mid))
            diet = _messages_from_text((ev or {}).get("dialogue") or "", bot)
            if not diet:
                continue
            mine = [str(x.get("quote") or "").split("\n")[0] for x in quotes
                    if int(x.get("memory_id") or 0) == mid and str(x.get("quote") or "").strip()]
            for m in diet:
                if m["side"] == "user" and any(s and s in m["text"] for s in mine):
                    m["picked"] = True
            threads.append({
                "memory_id": mid,
                "session_id": session,
                "ts": float(mem.get("occurred_at") or mem.get("created_at") or 0),
                "msgs": diet,
            })
        return {
            "ok": True, "kind": "style", "id": int(sid), "type": "说话方式",
            "title": str(style.get("name") or ""),
            "subtitle": f"{len(quotes)} 条原话 · {len(sessions)} 个会话",
            "state": "生效中" if str(style.get("status") or "") == "active" else "已停用",
            "meta": f"命中 {int(style.get('hits') or 0)} · 纠正 {int(style.get('corrections') or 0)}",
            "steps": [
                {"ts": int(float(style.get("created_at") or 0)), "label": "归纳",
                 "note": f"{len(quotes)} 句他说过的话被归到同一说话方式「{style.get('name') or ''}」"},
                {"ts": int(float(style.get("updated_at") or 0)), "label": "状态",
                 "note": str(style.get("status") or "active")},
            ],
            "messages": [],
            "threads": threads,
            "conv": f"{len(sessions)} 个会话",
            "note": "" if threads else "这组还没有可回查的原对话",
            "why": [
                f"同一说话方式下 {len(quotes)} 句他说过的话，来自 {len(sessions)} 个会话。",
                "下面按记忆分段给出他说这些话时的原对话。",
                f"命中 {int(style.get('hits') or 0)} / 纠正 {int(style.get('corrections') or 0)}。",
            ],
        }

    async def _prov_expr(self, eid: int) -> dict[str, Any]:
        row = await self.store.get_expr(int(eid))
        if not row:
            return {"ok": False, "error": "expr not found"}
        cues = [str(x) for x in _json_list(row.get("cues"))]
        ids = [int(x) for x in _json_list(row.get("source_ids")) if str(x).isdigit()]
        bot = await self._bot_name()
        ev = await self.svc.memory_evidence(str(ids[0])) if ids else {"ok": False}
        around = (ev or {}).get("around") or []
        # 同 quote：记忆内原对话带 bot 侧气泡，优先用它（hf_messages 只有用户消息）
        msgs = _messages_from_text((ev or {}).get("dialogue") or (ev or {}).get("source_text") or "", bot)
        if not msgs:
            snap = _json_list(row.get("evidence_snap"))
            for x in snap:
                if isinstance(x, dict):
                    msgs.append({
                        "who": str(x.get("speaker") or x.get("role") or "对方"),
                        "side": "bot" if str(x.get("role") or "") in ("assistant", "bot") else "user",
                        "text": str(x.get("text") or x.get("content") or ""),
                        "ts": float(x.get("ts") or 0), "id": "",
                    })
                elif isinstance(x, str):
                    msgs.extend(_messages_from_text(x, bot))
        if not msgs:
            msgs = [self._msg(r, bot) for r in around]
        session = str((ev or {}).get("session_id") or "")
        return {
            "ok": True, "kind": "expr", "id": int(eid), "type": "表达",
            "title": str(row.get("way") or ""), "subtitle": str(row.get("scene") or ""),
            "state": "待审" if str(row.get("checked_state") or "") == "ai_pending" else "已处理",
            "meta": f"scope {row.get('scope') or '—'} · {len(ids)} 条来源记忆",
            "steps": [
                {"ts": int(float(row.get("created_at") or 0)), "label": "抽取",
                 "note": f"场景：{row.get('scene') or '—'}；做法：{row.get('way') or '—'}"},
                {"ts": int(float(row.get("updated_at") or 0)), "label": "审核",
                 "note": str(row.get("checked_state") or "ai_pending")},
            ],
            "messages": msgs,
            "conv": f"会话 {session} · {await self.store.message_count(session)} 条" if session else "",
            "note": "" if msgs else (str((ev or {}).get("note") or "") or "来源记忆没有留下逐句原对话"),
            "why": ([f"线索：{c}" for c in cues[:4]] or ["没有记录线索"]) +
                    ([f"模型理由：{_clip(row.get('ai_reason'), 160)}"] if row.get("ai_reason") else []),
        }

    async def _prov_jargon(self, jid: int) -> dict[str, Any]:
        row = await self.store.jargon_get(int(jid))
        if not row:
            return {"ok": False, "error": "jargon not found"}
        hits = await self.store.jargon_hits_list(int(jid), 20)
        msgs = [
            {"who": "群友", "side": "user", "text": str(h.get("msg") or ""),
             "ts": float(h.get("ts") or 0), "id": f"hit {h.get('id') or ''}"}
            for h in hits if str(h.get("msg") or "").strip()
        ]
        return {
            "ok": True, "kind": "jargon", "id": int(jid), "type": "群内用语",
            "title": str(row.get("term") or ""),
            "subtitle": str(row.get("meaning") or row.get("usage") or ""),
            "state": {"confirmed": "生效中", "pending": "待审", "denied": "已否定"}.get(
                str(row.get("status") or ""), str(row.get("status") or "")),
            "meta": f"{row.get('scope') or '全局'} · 命中 {int(row.get('hit_count') or 0)} 次",
            "steps": [
                {"ts": int(float(row.get("created_at") or 0)), "label": "首次出现",
                 "note": f"在 {row.get('scope') or '全局'} 捕获"},
                {"ts": int(float(row.get("last_hit_at") or 0)), "label": "最近命中",
                 "note": f"累计 {int(row.get('hit_count') or 0)} 次"},
                {"ts": int(float(row.get("updated_at") or 0)), "label": "人工闸门",
                 "note": str(row.get("status") or "pending")},
            ],
            "messages": msgs,
            "conv": f"命中日志 {len(msgs)} 条",
            "note": "" if msgs else "还没有命中日志；只有词条本身",
            "why": [
                f"释义：{row.get('meaning') or '—'}",
                f"用法：{row.get('usage') or '—'}",
                "只用于听懂，不照搬进回复。",
            ],
        }

    async def _prov_term(self, tid: int) -> dict[str, Any]:
        row = await (await self.store._db.execute(
            "SELECT * FROM hf_style_terms WHERE id=?", (int(tid),)
        )).fetchone()
        if not row:
            return {"ok": False, "error": "term not found"}
        t = dict(row)
        return {
            "ok": True, "kind": "term", "id": int(tid), "type": "群内用语",
            "title": str(t.get("term") or ""),
            "subtitle": str(t.get("meaning") or t.get("usage") or ""),
            "state": "生效中" if int(t.get("enabled") or 0) else "已停用",
            "meta": f"{t.get('scope') or '全局'} · 命中 {int(t.get('hits') or 0)} 次",
            "steps": [
                {"ts": int(float(t.get("created_at") or 0)), "label": "落库",
                 "note": f"范围：{t.get('scope') or '全局'}"},
                {"ts": int(float(t.get("updated_at") or 0)), "label": "状态",
                 "note": str(t.get("status") or "confirmed")},
            ],
            "messages": [],
            "conv": "",
            "note": "这个词条没有逐条命中日志；命中统计在 hits 里。",
            "why": [f"释义：{t.get('meaning') or '—'}", f"用法：{t.get('usage') or '—'}"],
        }

    async def _prov_persona(self, pid: int) -> dict[str, Any]:
        row = await self.store.get_proposal(int(pid))
        if not row:
            return {"ok": False, "error": "proposal not found"}
        persona = str(row.get("persona_id") or "")
        base = str(row.get("base") or "")
        prop = str(row.get("proposal") or "")
        live = ""
        try:
            people = await self.svc.personas()
            cur = next((x for x in people.get("items") or []
                        if str(x.get("persona_id") or "") == persona), None)
            live = str((cur or {}).get("system_prompt") or "")
        except Exception:                                   # noqa: BLE001
            live = ""
        left = live or base
        return {
            "ok": True, "kind": "persona", "id": int(pid), "type": "人格提案",
            "title": f"提案 #{int(pid)}", "subtitle": persona or "",
            "state": {"pending": "待审", "approved": "已写入", "rejected": "已驳回",
                      "stale": "已过期", "expired": "超时作废"}.get(
                str(row.get("status") or ""), str(row.get("status") or "")),
            "meta": f"当前 {len(left)} 字 → 提案 {len(prop)} 字",
            "steps": [
                {"ts": int(float(row.get("ts") or 0)), "label": "生成提案",
                 "note": "只提改动，不直接写人格"},
                {"ts": int(float(row.get("decided_at") or 0)), "label": "状态",
                 "note": str(row.get("status") or "pending")},
            ],
            "messages": [],
            "conv": "",
            "note": ("提案生成后人格已被改过：左边显示的是现在的真身，和提案基线不一致，写入前请看清楚。"
                     if (live and base and live != base) else
                     "人格提案没有逐句证据；这里给的是模型理由与左右对比，写入前请自己看一遍。"),
            "why": [_clip(row.get("rationale"), 300)] if row.get("rationale") else ["没有记录模型理由"],
            "diff": _diff(left, prop, 400),
            "sbs": _sbs(left, prop, 600),
            "left_label": "当前人格",
            "right_label": "更新后",
        }

    @staticmethod
    def _msg(r: dict[str, Any], bot_name: str = "助手") -> dict[str, Any]:
        return {
            "who": str(r.get("sender_name") or (bot_name if int(r.get("is_bot") or 0) else "对方")),
            "side": "bot" if int(r.get("is_bot") or 0) else "user",
            "text": str(r.get("content") or ""),
            "ts": float(r.get("ts") or 0),
            "id": str(r.get("ext_id") or r.get("id") or ""),
        }

    # ---------------- 注入预览 ----------------
    async def preview(self, persona_id: str = "", register: str = "private",
                      message: str = "") -> dict[str, Any]:
        pid = await self._pid(persona_id)
        cfg = await self.svc.config()
        learned_on = bool(cfg.get("expression_style_enabled"))
        sys_block = ""
        try:
            # 按当前真实开关编译：学习关着时注入的就是默认表达规则，
            # 预览不能拿「打开学习」的假结果糊弄人（原来这里强制 True，两张皮）。
            sys_block = await style_mod.expression_block(
                self.store, cfg,
                pid, "group" if register == "group" else "private", str(message or ""), dry=True,
            )
        except Exception as exc:                            # noqa: BLE001
            logger.warning(f"[StarHeart] 注入预览编译失败：{exc}")
        pool = await self.store.jargon_match_pool(["全局"])
        matched = self.svc._jargon_mechanical_matches(str(message or ""), pool)
        lines, dropped = [], []
        for m in matched:
            meaning = str(m.get("meaning") or "").strip()
            if meaning:
                lines.append(f"- {m.get('term')}：{meaning}")
            else:
                dropped.append({"term": str(m.get("term") or ""), "reason": "没有释义"})
        user_block = ""
        if lines:
            user_block = ("【黑话理解参考】群友用的这些词/梗，只用于听懂他们在说什么，"
                          "**不要照搬进你自己的回复**：\n" + "\n".join(lines))
        return {
            "ok": True, "persona_id": pid, "register": register,
            "system_block": sys_block, "user_block": user_block,
            "chars": len(sys_block) + len(user_block),
            "token_est": max(1, (len(sys_block) + len(user_block)) // 2),
            "dropped": dropped,
            "learning": {"enabled": learned_on,
                         "mode": str(cfg.get("expression_style_inject_mode") or "legacy")},
            "note": "" if learned_on else "表达学习开关是关的：这一轮注入的是默认表达规则，学到的原话不会进提示词。",
        }

    # ---------------- 写动作 ----------------
    async def decide(self, kind: str, ident: int, action: str, reason: str = "") -> dict[str, Any]:
        k = str(kind or "").lower()
        act = str(action or "").lower()
        if k == "expr":
            return await self.svc.expr_review([int(ident)], "approve" if act == "approve" else "reject", reason)
        if k == "jargon":
            return await self.svc.jargon_decide(int(ident), "confirm" if act == "approve" else "deny")
        if k == "persona":
            return await self.svc.persona_decide(int(ident), act == "approve")
        return {"ok": False, "error": f"unknown kind: {kind}"}

    async def voice_save(self, kind: str, ident: int, fields: dict[str, Any]) -> dict[str, Any]:
        k = str(kind or "").lower()
        if k == "style":
            name = str((fields or {}).get("name") or "").strip()
            if not name:
                return {"ok": False, "error": "name required"}
            n = await self.store.rename_style(int(ident), name)
            return {"ok": bool(n)}
        if k == "quote":
            quote = str((fields or {}).get("quote") or "").strip()
            if not quote:
                return {"ok": False, "error": "quote required"}
            n = await self.store.update_quote(int(ident), quote)
            return {"ok": bool(n)}
        if k == "term":
            kwargs = {x: str((fields or {}).get(x) or "") for x in ("term", "meaning", "usage", "scope")
                      if x in (fields or {})}
            if "enabled" in (fields or {}):
                kwargs["enabled"] = bool((fields or {}).get("enabled"))
            n = await self.store.update_style_term(int(ident), **kwargs)
            return {"ok": bool(n)}
        return {"ok": False, "error": f"unknown kind: {kind}"}

    async def voice_status(self, kind: str, ident: int, status: str) -> dict[str, Any]:
        k = str(kind or "").lower()
        if k == "style":
            n = await self.store.set_style_status(int(ident), "paused" if status == "paused" else "active")
            return {"ok": bool(n)}
        if k == "quote":
            n = await self.store.set_quote_status(int(ident), str(status) if status else "active")
            return {"ok": bool(n)}
        if k == "term":
            n = await self.store.update_style_term(int(ident), enabled=bool(status != "paused"))
            return {"ok": bool(n)}
        return {"ok": False, "error": f"unknown kind: {kind}"}

    async def voice_delete(self, kind: str, ident: int) -> dict[str, Any]:
        k = str(kind or "").lower()
        if k == "style":
            n = await self.store.delete_style(int(ident))
            return {"ok": bool(n)}
        if k == "quote":
            n = await self.store.delete_quote(int(ident))
            return {"ok": bool(n)}
        if k == "term":
            n = await self.store.delete_style_term(int(ident))
            return {"ok": bool(n)}
        return {"ok": False, "error": f"unknown kind: {kind}"}

    async def persona_save(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self.svc.persona_save(payload)

    async def revision_decide(self, ident: int, approve: bool) -> dict[str, Any]:
        return await self.svc.persona_decide(int(ident), bool(approve))

    async def revision_rollback(self, ident: int) -> dict[str, Any]:
        return await self.svc.persona_rollback(int(ident))

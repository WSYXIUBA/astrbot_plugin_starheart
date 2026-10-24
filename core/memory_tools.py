# -*- coding: utf-8 -*-
"""bot 主动检索自己记忆的 LLM 函数工具（P1-1）。

设计口径：
- 平时每轮自动注入的记忆照旧（被动召回）；这三个工具给模型「深挖」用——
  本轮没召回、但模型觉得「我应该记得」的时候，自己动手查。
- 全部只读：查了不会写库、不会改强度（touch_memory 只在自动召回路径做）。
- 隔离守恒：全部走 storage 现成方法，人格/会话过滤与召回路径同源，不新开 SQL 后门。
"""
from __future__ import annotations

import json
import time
from typing import Any

from astrbot.api import logger

from .service import StarHeartService

# 单工具返回上限（字符）：工具结果会进下一轮 prompt，太长会挤爆上下文
RESULT_LIMIT = 2400
# 每工具最多带几条记忆
HIT_LIMIT = 8


def _clip(text: Any, n: int = 120) -> str:
    s = str(text or "").replace("\r", "").strip()
    return s if len(s) <= n else s[: max(0, n - 1)] + "…"


def _stamp(ts: Any) -> str:
    try:
        v = float(ts or 0)
    except (TypeError, ValueError):
        return "?"
    if v <= 0:
        return "?"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(v))


def _is_group(m: dict[str, Any]) -> bool:
    return "GroupMessage" in str(m.get("session_id") or "")


def _render_memory(m: dict[str, Any]) -> str:
    """一条记忆 → 一行紧凑文本（id / 时间 / 归属 / 摘要 / 原话引文）。"""
    parts = [f"#{m.get('id')}"]
    when = _stamp(m.get("occurred_at") or m.get("created_at"))
    if when != "?":
        parts.append(when)
    sess = str(m.get("session_id") or "")
    if _is_group(m):
        parts.append("群聊")
    elif sess:
        parts.append("私聊" if ("FriendMessage" in sess or "private" in sess.lower()) else "会话")
    if str(m.get("said_by") or "").strip() == "bot":
        parts.append("我自己说的")
    head = " ".join(parts)
    summary = _clip(m.get("summary"), 80)
    evi = _clip(m.get("evidence") or m.get("source_text"), 100)
    line = f"- {head}: {summary}"
    if evi and evi != summary:
        line += f"｜原话: {evi}"
    return line


async def _ready(plugin: Any) -> StarHeartService | None:
    """库没开/没就绪时返回 None（工具层各自短路）。"""
    service = getattr(plugin, "service", None)
    if service is None:
        return None
    store = getattr(service, "store", None)
    if store is None or getattr(store, "db", None) is None:
        return None
    return service


async def _search(service: StarHeartService, kw: str, limit: int,
                  persona_id: str = "", days: float = 0) -> list[dict[str, Any]]:
    try:
        # P1-10：多关键词 AND + 相关性排序 + 天数下推 SQL（旧实现整串 LIKE、按时间排、
        # days 在取回后才过滤——工具示例「猫 出差」本身就失效）
        res = await service.store.search_memories(
            q=kw, persona_id=persona_id, limit=limit, days=days, sort="relevance")
        return [dict(r) for r in ((res or {}).get("items") or [])]
    except Exception as exc:                              # noqa: BLE001
        logger.debug(f"[StarHeart] 记忆工具检索失败：{exc}")
        return []


async def _gate(plugin: Any, event: Any, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """工具结果的统一闸门（P0-1）：与自动召回同一套 _scope_allows。

    工具是记忆的第二个出口，不能绕过隐私边界——旧实现直接 search_memories 全库返回，
    群里一句「查查他跟我说过什么」就能把别人的私聊记忆念出来。
    scope 解析失败时返回空集（fail-closed）。
    """
    try:
        from . import memory_v2 as v2mem

        cfg = await plugin.service.config()
        scope = v2mem._recall_scope(plugin, event, cfg)
    except Exception as exc:                              # noqa: BLE001
        logger.warning(f"[StarHeart] 记忆工具闸门解析失败，本次不返回记忆：{exc}")
        return []
    out: list[dict[str, Any]] = []
    for r in rows:
        try:
            ok, _cross = v2mem._scope_allows(scope, r)
        except Exception:                                 # noqa: BLE001
            ok = False
        if ok:
            out.append(r)
    return out


async def _tool_persona(plugin: Any, event: Any) -> str:
    """当前会话的人格 id（工具检索必须按人格收敛，P0-1）。"""
    try:
        return await plugin._inject_persona_id(
            str(getattr(event, "unified_msg_origin", "") or plugin._sid(event)),
            use_cache=True,
        ) or ""
    except Exception:                                     # noqa: BLE001
        return ""


async def search_memory(
    plugin: Any, event: Any, query: str, days: float = 0, scope: str = "auto",
) -> str:
    """搜索我自己的记忆库。当对话涉及过去的事、而系统自动带出的记忆不够或没带时调用；聊天内容我平时会自动记住，这个工具用于主动回忆细节。

    Args:
        query (string): 搜索词：人名/事/话题关键词，例如「猫 出差」「团子」
        days (number): 只搜最近多少天内（默认 0 = 不限时间）
        scope (string): 范围：auto=不限（默认）、group=只群聊记忆、private=只私聊记忆
    Returns:
        str: 命中的记忆列表
    """
    try:
        service = await _ready(plugin)
        kw = str(query or "").strip()[:60]
        if service is None or not kw:
            return "（记忆库还没就绪或搜索词为空，先别查）"
        persona_id = await _tool_persona(plugin, event)
        rows = await _search(service, kw, HIT_LIMIT * 4, persona_id=persona_id,
                             days=float(days or 0))
        # P0-1：先过隐私闸门（与自动召回同口径），再谈时间/渠道
        rows = await _gate(plugin, event, rows)
        if not rows:
            return f"没搜到跟「{kw}」相关的记忆。"
        sc = str(scope or "auto").strip().lower()
        # scope=auto 改为硬过滤当前渠道（旧实现只排序不过滤 = 群里能搜到私聊记忆）
        sess_ch = ""
        try:
            sess_ch, _gid = service.session_kind(plugin._sid(event))
        except Exception:                                 # noqa: BLE001
            sess_ch = ""
        want = sc if sc in ("group", "private") else sess_ch
        if want in ("group", "private"):
            rows = [r for r in rows if _is_group(r) == (want == "group")]
        lines = [_render_memory(r) for r in rows[:HIT_LIMIT]]
        more = len(rows) - len(lines)
        out = f"搜「{kw}」命中 {len(rows)} 条：\n" + "\n".join(lines)
        if more > 0:
            out += f"\n（还有 {more} 条未列出）"
        return out[:RESULT_LIMIT]
    except Exception as exc:                              # noqa: BLE001 工具失败不许炸对话
        logger.warning(f"[StarHeart] search_memory 工具失败：{exc}")
        return "（这次没查到，稍后再试）"


async def recall_about_person(
    plugin: Any, event: Any, name: str, question: str = "",
) -> str:
    """查我和某个人（或某个群）的画像与往来记忆。想不起某个人是谁、他说过什么、我们关系如何时调用。

    Args:
        name (string): 人名/昵称/QQ号/群号，例如「团子」「小洛」「123456789」
        question (string): 可选：只关心哪方面，例如「他家的猫」「他什么时候出差」
    Returns:
        str: 这个人的画像与相关记忆
    """
    try:
        service = await _ready(plugin)
        who = str(name or "").strip()[:40]
        if service is None or not who:
            return "（记忆库还没就绪或名字为空，先别查）"
        # P0-1：人格收敛（旧实现不传 persona = 跨人格取最近一份画像）
        persona_id = await _tool_persona(plugin, event)
        # 昵称反解：名字 → QQ 号（画像与记忆都按号存）
        ident = who
        try:
            names_map = await service.store.person_names_map() or {}
            if not who.isdigit():
                for key, val in names_map.items():
                    nm = str((val or {}).get("name") if isinstance(val, dict) else val or "")
                    if nm and nm == who and str(key).startswith("user:"):
                        ident = str(key).split(":", 1)[1]
                        break
        except Exception as exc:                          # noqa: BLE001
            logger.debug(f"[StarHeart] 昵称反解跳过：{exc}")
        blocks: list[str] = []
        # ① 画像（profile_get 零 LLM 直读；先按人查，查不到再按群查）
        try:
            prof = await service.store.profile_get(ident, "user", persona_id)
            if not prof:
                prof = await service.store.profile_get(ident, "group", persona_id)
            if prof:
                body = prof.get("profile") or {}
                if isinstance(body, str):
                    try:
                        body = json.loads(body)
                    except Exception:                     # noqa: BLE001
                        body = {"one_line": str(body)}
                lines = [f"## {who} 的画像"]
                if isinstance(body, dict):
                    for k, v in body.items():
                        vs = _clip(v, 120)
                        if vs:
                            lines.append(f"- {k}：{vs}")
                else:
                    lines.append(f"- {_clip(body, 200)}")
                lines.append(f"（基于 {int(prof.get('mem_count') or 0)} 条记忆，"
                             f"更新于 {_stamp(prof.get('updated_at'))}）")
                blocks.append("\n".join(lines))
        except Exception as exc:                          # noqa: BLE001
            logger.debug(f"[StarHeart] 画像查询失败：{exc}")
        # ② 相关记忆（按人格 + 闸门过滤）
        rows = [r for r in await _search(service, who, HIT_LIMIT * 2, persona_id=persona_id) if (
            who in str(r.get("summary") or "")
            or who in str(r.get("content") or "")
            or who in str(r.get("source_text") or "")
            or who in str(r.get("entities") or "")
        )]
        rows = await _gate(plugin, event, rows)
        if question:
            q = str(question).strip()[:60]
            qrows = [r for r in await _search(service, q, HIT_LIMIT, persona_id=persona_id)]
            qrows = await _gate(plugin, event, qrows)
            ids = {r.get("id") for r in rows}
            rows += [r for r in qrows if r.get("id") not in ids]
        if rows:
            lines = [f"## 与「{who}」相关的记忆（{len(rows)} 条）"]
            lines.extend(_render_memory(r) for r in rows[:HIT_LIMIT])
            blocks.append("\n".join(lines))
        if not blocks:
            return f"我对「{who}」还没有记忆——可能没聊过，或者名字记法不同（可以问对方 QQ 号再查）。"
        return "\n\n".join(blocks)[:RESULT_LIMIT]
    except Exception as exc:                              # noqa: BLE001
        logger.warning(f"[StarHeart] recall_about_person 工具失败：{exc}")
        return "（这次没查到，稍后再试）"


async def browse_history(
    plugin: Any, event: Any, channel: str = "auto", query: str = "", hours: float = 72,
) -> str:
    """翻看最近的聊天原文。想确认「刚才/昨天具体说了什么」、自动记忆没覆盖到的细节时调用。

    Args:
        channel (string): 翻哪个会话：auto=当前会话（默认）、group=最近活跃的群、private=最近私聊
        query (string): 可选过滤词，只看含这个词的消息
        hours (number): 往回翻多少小时（默认 72，最大 720）
    Returns:
        str: 最近的聊天记录摘录
    """
    try:
        service = await _ready(plugin)
        if service is None:
            return "（记忆库还没就绪，先别查）"
        sid = ""
        try:
            sid = plugin._sid(event)
        except Exception:                                 # noqa: BLE001
            sid = ""
        hours = max(1.0, min(720.0, float(hours or 72)))
        ch = str(channel or "auto").strip().lower()
        target = ""
        if ch == "auto":
            target = sid
        else:
            # P0-1：跨会话翻记录必须过闸门——旧实现「私聊里能翻任意群、群里能翻别人私聊」。
            # 现在只允许翻「本人参与」的会话：私聊=本人；群=本群，或本人/当前说话人在场的群。
            try:
                from . import memory_v2 as v2mem

                cfg = await service.config()
                scope = v2mem._recall_scope(plugin, event, cfg)
                cur_gid = str(scope.get("group_id") or "")
                me = str(service.session_kind(sid)[1] or "") if sid else ""
                cast = set((scope.get("cast") or {}).keys())
            except Exception as exc:                      # noqa: BLE001 fail-closed
                logger.warning(f"[StarHeart] 翻记录范围解析失败，只允许当前会话：{exc}")
                scope, cur_gid, me, cast = {}, "", "", set()
            # 最近活跃会话：消息表按会话聚合（与面板消息浏览同源）
            try:
                srows = await (await service.store.db.execute(
                    "SELECT session_id, MAX(ts) AS last_ts FROM hf_messages "
                    "GROUP BY session_id ORDER BY last_ts DESC LIMIT 30"
                )).fetchall()
                for r in srows:
                    s2 = str(r["session_id"] or "")
                    if not s2:
                        continue
                    try:
                        sk, sk_ident = service.session_kind(s2)
                    except Exception:                     # noqa: BLE001
                        sk, sk_ident = "", ""
                    if sk != ch:
                        continue
                    # 私聊：只能翻本人的会话（会话尾部 = 本人号）
                    if sk == "private" and sk_ident != me:
                        continue
                    # 群聊：只能翻本群（其它群要 g2g 开关，默认关）
                    if sk == "group" and sk_ident != cur_gid and not bool(scope.get("g2g")):
                        continue
                    target = s2
                    break
            except Exception as exc:                      # noqa: BLE001
                logger.debug(f"[StarHeart] 会话定位失败：{exc}")
        if not target:
            return "没找到要翻的会话（最近没有这个渠道的聊天）。"
        try:
            res = await service.store.messages_for_session(
                session_id=target, limit=60, offset=0, q=str(query or "").strip())
            rows = [dict(r) for r in ((res or {}).get("items") or [])]
        except Exception as exc:                          # noqa: BLE001
            logger.debug(f"[StarHeart] 翻消息失败：{exc}")
            rows = []
        cutoff = time.time() - hours * 3600
        rows = [r for r in rows if float(r.get("ts") or 0) >= cutoff]
        if not rows:
            return f"最近 {int(hours)} 小时没有可翻的聊天记录。"
        lines: list[str] = []
        for r in rows[-20:]:
            who = str(r.get("sender_name") or r.get("sender_id") or ("我" if r.get("is_bot") else "?"))
            lines.append(f"{_stamp(r.get('ts'))} {who}: {_clip(r.get('content'), 90)}")
        label = "当前会话" if target == sid else target
        out = f"## {label} 最近 {int(hours)}h 聊天（{len(rows)} 条，显示末 20 条）\n" + "\n".join(lines)
        return out[:RESULT_LIMIT]
    except Exception as exc:                              # noqa: BLE001
        logger.warning(f"[StarHeart] browse_history 工具失败：{exc}")
        return "（这次没查到，稍后再试）"


# 统一登记（main.py initialize/terminate 按这个元组驱动启停，防新增漏关）
TOOL_NAMES: tuple[str, ...] = (
    "starheart_search_memory",
    "starheart_recall_about_person",
    "starheart_browse_history",
)

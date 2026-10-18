"""跨会话动作留痕：认出「bot 借工具在别的会话里发了东西」，并把它记成可查的台账。

为什么需要它（实测现场，2026-10-13）：
    群里 @ 它「私聊给我发一句消息」→ 它借第三方工具插件（qzone_tools 的
    run_wyc_tool → send_message）**直连协议端**把消息发到了私聊 →
    随后在私聊里问「知道这句是谁让你发的吗」，它答「没人让我发……就是我自己抽的」。

    根因不是它笨，而是信息根本不存在：这条动作的**起因**只留在「发起会话（群聊）」的
    上下文里，而目标会话（私聊）只看得见自己那一侧的对话历史。AstrBot 的群聊上下文感知
    也只往群会话注入，不会把群里的东西送进私聊请求。

这个模块做的是「认出来 + 记下来」，三点设计约束：
    1. **结构性判据**，不做意图判定：只看工具名/参数/返回值里有没有「内容 + 目标」这两件
       事实（意图、情绪照旧交给模型，见仓库铁律）；
    2. **认不出来不猜**：参数形状对不上就返回 None，由调用方记一条 debug 日志。宁可漏记，
       不可记错（记错等于给 bot 喂假记忆）；
    3. **失败不落痕**：返回里出现失败标记、或读不到明确的成功标记，一律不记（fail-close）。

台账本体存在 main.py 的运行内存里（带 TTL、带上限），需要长期可回忆时再由 main.py 写一条
`source_mode="action"` 的记忆 —— 本模块只放纯函数，方便单测。
"""

from __future__ import annotations

import json
import time
from typing import Any

# ---------------- 结构性判据 ----------------

# 「内容」字段：要发的话放在哪个键上（第三方工具没有统一 schema，只能按常见键收）
_TEXT_KEYS = ("message", "msg", "text", "content", "caption", "plain_text", "messages")

# 「目标」字段：发给谁 / 发到哪个会话（顺序 = 优先级，越靠前越像「明确的目标」）
_TARGET_KEYS = ("target_id", "target", "user_id", "group_id", "qq", "to", "umo", "session", "peer")

# 「会话类型」字段
_CHAT_KEYS = ("chat_type", "session_type", "message_type", "msg_type")

# 会话类型取值归一（OneBot 的 message_type 用 1/2 表示私聊/群聊）
_CHAT_MAP = {
    "1": "private", "2": "group",
    "private": "private", "friend": "private", "friendmessage": "private",
    "group": "group", "groupmessage": "group",
    "私聊": "private", "好友": "private", "小窗": "private",
    "群聊": "group", "群": "group",
}

# 只读工具的名字特征：先排除「查历史 / 列清单 / 搜索」这类工具，避免把「读」认成「发」
_READ_HINTS = (
    "history", "list", "search", "query", "get_", "read", "info", "status", "check",
    "dump", "export", "help", "view", "show",
    "历史", "列表", "查询", "查看", "搜索", "记录查询",
)

# 动作类型：定时 / 发布（其余按「即时发送」处理）
_SCHEDULE_HINTS = ("schedule", "timer", "delay", "remind", "定时", "预约", "延时", "提醒", "稍后发")
_PUBLISH_HINTS = ("publish", "moments", "qzone", "feed", "post_", "发布", "空间", "说说", "动态", "朋友圈")

# 返回值里的成功 / 失败标记（顺序：先排失败，再要求成功 —— 读不到成功标记就不记）
_FAIL_MARKS = ("error", "fail", "false", "❌", "失败", "错误", "无法", "不存在", "拒绝", "超时", "timeout", "invalid")
_OK_MARKS = ("已发送", "发送成功", "已发出", "已发布", "已创建", "success", "sent", "✅", "成功")

RESULT_TEXT_LIMIT = 400          # 返回值摘要长度上限（只用于判成功/失败，不需要全文）
ACTION_TEXT_LIMIT = 120          # 台账里留存的内容长度上限
NOTE_ITEM_LIMIT = 3              # 一次最多知会几条
NOTE_TEXT_LIMIT = 60             # 知会里引用内容的长度上限


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _text_of(value: Any) -> str:
    """把「内容」字段的任意形状压成一段文本（消息段数组 / {text:...} / 纯字符串）。"""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        for key in ("text", "content", "message", "plain_text", "data"):
            inner = _text_of(value.get(key))
            if inner:
                return inner
        return ""
    if isinstance(value, (list, tuple)):
        parts = [p for p in (_text_of(item) for item in value) if p]
        return " ".join(parts).strip()
    return ""


def _ident_of(value: Any) -> str:
    """把「目标」字段的值压成一个标识串（数字 id / 号码串 / 完整会话串）。"""
    if isinstance(value, bool) or value is None:
        return ""
    if isinstance(value, (int, float)):
        return str(int(value))
    if isinstance(value, str):
        raw = value.strip()
        if not raw or len(raw) > 96:
            return ""
        return raw
    if isinstance(value, (list, tuple)):
        for item in value:
            ident = _ident_of(item)
            if ident:
                return ident
        return ""
    if isinstance(value, dict):
        for key in ("id", "session_id", "user_id", "group_id", "target_id", "umo", "session"):
            ident = _ident_of(value.get(key))
            if ident:
                return ident
    return ""


def split_umo(value: str) -> tuple[str, str]:
    """拆 `platform:MessageType:id` → (group|private|other, id)。

    与 service.session_kind 同口径（这里不 import service，避免 main↔core 出现环）。
    """
    parts = str(value or "").split(":")
    if len(parts) >= 3:
        mtype, ident = parts[-2].strip().lower(), parts[-1].strip()
        if "group" in mtype:
            return "group", ident
        if "friend" in mtype or "private" in mtype:
            return "private", ident
        return "other", ident
    return "other", str(value or "").strip()


def unwrap(tool_name: str, tool_args: Any, depth: int = 0) -> tuple[str, dict[str, Any], str]:
    """拆掉「工具工具箱」那一层：`{tool_name:…, tool_args:…}` → 内层工具名与参数。

    工具类插件（如 qzone_tools 的 run_wyc_tool）把上百个工具收在一个入口后面，
    LLM 实际喊的是内层工具名 —— 不拆就认不出 `send_message`。

    返回 (内层工具名, 内层参数, 链路名「外层/内层」)。只在**同时**出现「名字键 + 参数键」时
    才当成工具箱，避免把 `{"action": "list"}` 这类普通参数误拆成工具名。
    """
    name = str(tool_name or "")
    args = _as_dict(tool_args)
    if depth >= 2:
        return name, args, name
    inner = args.get("tool_name") or args.get("tool")
    payload = args.get("tool_args") if "tool_args" in args else args.get("args")
    if isinstance(inner, str) and inner.strip() and payload is not None:
        inner_args: Any = payload
        if isinstance(inner_args, str):
            try:
                inner_args = json.loads(inner_args)
            except (TypeError, ValueError):
                inner_args = {}
        sub_name, sub_args, chain = unwrap(inner, _as_dict(inner_args), depth + 1)
        return sub_name, sub_args, f"{name}/{chain}"
    return name, args, name


def _pick_text(args: dict[str, Any]) -> str:
    for key in _TEXT_KEYS:
        if key in args:
            text = _text_of(args.get(key))
            if text:
                return text
    return ""


def _pick_chat(args: dict[str, Any]) -> str:
    for key in _CHAT_KEYS:
        if key not in args:
            continue
        value = args.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            value = str(int(value))
        if isinstance(value, str):
            chat = _CHAT_MAP.get(value.strip().lower(), "")
            if chat:
                return chat
    return ""


def _pick_target(args: dict[str, Any], chat: str) -> tuple[str, str]:
    """返回 (目标标识, 会话类型)。目标是完整会话串时，顺带把类型解出来。"""
    for key in _TARGET_KEYS:
        if key not in args:
            continue
        ident = _ident_of(args.get(key))
        if not ident:
            continue
        if ident.count(":") >= 2:                 # platform:MessageType:id
            kind, tail = split_umo(ident)
            return tail, chat or (kind if kind in ("group", "private") else "")
        return ident, chat
    return "", chat


def classify(name: str, args: dict[str, Any]) -> dict[str, Any] | None:
    """判断这次调用是不是「外发动作」；是则给出 kind / 目标 / 内容。

    判据 = 内容字段非空 + 目标字段（或会话类型）存在。两条缺一不可：
    只读工具（查历史、列清单）天然没有内容字段，会被这条件挡掉；
    同会话回复（没有目标）也不会记进来，避免把正常回复刷成台账。
    """
    low = str(name or "").lower()
    if any(hint in low for hint in _READ_HINTS):
        return None
    text = _pick_text(args)
    chat = _pick_chat(args)
    target, chat = _pick_target(args, chat)
    if any(hint in low for hint in _PUBLISH_HINTS):
        if not text:
            return None
        return {"kind": "publish", "text": text, "target": target, "chat_type": chat}
    if not text or not (target or chat):
        return None
    kind = "schedule" if any(hint in low for hint in _SCHEDULE_HINTS) else "send"
    return {"kind": kind, "text": text, "target": target, "chat_type": chat}


def result_text(tool_result: Any) -> str:
    """把工具返回值压成一段文本（用于判成功/失败）；认不出的形状返回空串。

    AstrBot 交给 on_llm_tool_respond 的是 mcp 的 CallToolResult（`.content` 里是
    TextContent 等块），但插件直接返回 dict 的路径也存在 —— 两种都要吃得下。
    """
    if tool_result is None:
        return ""
    if isinstance(tool_result, str):
        return tool_result.strip()[:RESULT_TEXT_LIMIT]
    if isinstance(tool_result, (dict, list, tuple)):
        try:
            return json.dumps(tool_result, ensure_ascii=False, default=str)[:RESULT_TEXT_LIMIT]
        except Exception:                                     # noqa: BLE001 兜底序列化失败
            return str(tool_result)[:RESULT_TEXT_LIMIT]
    chunks: list[str] = []
    content = getattr(tool_result, "content", None)
    if isinstance(content, str):
        chunks.append(content)
    elif isinstance(content, (list, tuple)):
        for item in content:
            if isinstance(item, str):
                chunks.append(item)
                continue
            inner = getattr(item, "text", None)
            chunks.append(inner if isinstance(inner, str) else str(item))
    if not chunks:
        for attr in ("text", "message", "data", "result", "output"):
            value = getattr(tool_result, attr, None)
            if isinstance(value, str) and value.strip():
                chunks.append(value)
                break
    if not chunks:
        try:
            chunks.append(json.dumps(tool_result, ensure_ascii=False, default=str))
        except Exception:                                     # noqa: BLE001
            chunks.append(repr(tool_result))
    return "\n".join(chunks).strip()[:RESULT_TEXT_LIMIT]


def action_ok(tool_result: Any) -> bool:
    """这次调用是否**确实发出去了**。

    fail-close：读不到明确成功标记（或带了 isError / 失败字样）→ False。
    漏记一条只是少一次知会；把「失败」记成「干过」则会让 bot 说假话，后者严重得多。
    """
    if getattr(tool_result, "isError", False):
        return False
    text = result_text(tool_result).lower()
    if not text:
        return False
    if any(mark in text for mark in _FAIL_MARKS):
        return False
    return any(mark in text for mark in _OK_MARKS)


def looks_outbound(name: str) -> bool:
    """工具名看起来像「往外发东西」吗（只用于 debug 日志：方便发现没认出来的工具）。"""
    low = str(name or "").lower()
    if any(hint in low for hint in _READ_HINTS):
        return False
    keys = ("send", "post", "push", "notify", "broadcast", "deliver", "发", "通知", "转告", "发布", "群发", "私聊")
    return any(key in low for key in keys)


def build_entry(found: dict[str, Any], *, chain: str, session_id: str, kind: str, ident: str,
                actor_id: str = "", actor_name: str = "", now: float | None = None) -> dict[str, Any] | None:
    """把 classify() 的结果组装成台账条目。

    返回 None = 不该记：**即时发送但没目标**（例如核心工具不带 session，就是发给当前会话）、
    或目标就是**本会话自己**（正常回复，记下来只会把台账刷成噪音）。
    定时 / 发布类动作没有这个问题：它们本来就是「出了这个会话」的行为。
    """
    act_kind = str(found.get("kind") or "send")
    target = str(found.get("target") or "")
    chat_type = str(found.get("chat_type") or "")
    if target.count(":") >= 2:                    # 目标是完整会话串 → 类型从串里解
        target_kind, target = split_umo(target)
        chat_type = chat_type or (target_kind if target_kind in ("group", "private") else "")
    target_kind = chat_type if chat_type in ("group", "private") else ""
    if act_kind == "send":
        if not target:
            return None
        if target == ident and (not target_kind or target_kind == kind):
            return None
    return {
        "ts": float(now if now is not None else time.time()),
        "kind": act_kind,
        "tool": str(chain or ""),
        "text": str(found.get("text") or "")[:ACTION_TEXT_LIMIT],
        "target": target,
        "target_kind": target_kind,
        "src_session": str(session_id or ""),
        "src_kind": str(kind or ""),
        "src_ident": str(ident or ""),
        "actor_id": str(actor_id or ""),
        "actor_name": str(actor_name or ""),
    }


def duration_text(seconds: float) -> str:
    """一段时长说成人话：刚刚 / 12 分钟 / 3 小时 / 2 天。"""
    try:
        delta = max(0.0, float(seconds))
    except (TypeError, ValueError):
        return ""
    if delta < 60:
        return "刚刚"
    if delta < 3600:
        return f"{int(delta // 60)} 分钟"
    if delta < 86400:
        return f"{int(delta // 3600)} 小时"
    if delta < 30 * 86400:
        return f"{int(delta // 86400)} 天"
    if delta < 365 * 86400:
        return f"{int(delta // (30 * 86400))} 个月"
    return "很久"


def ago_text(ts: float, now: float | None = None) -> str:
    """「刚刚 / N 分钟前 / N 小时前」——知会里用自然话，不写时间戳。"""
    t = float(now if now is not None else time.time())
    delta = max(0.0, t - float(ts or 0))
    label = duration_text(delta)
    return label if label in ("刚刚", "很久") else f"{label}前"


def where_text(kind: str, ident: str) -> str:
    """会话的人类可读说法：群 {群号} / 私聊 {QQ}。"""
    if kind == "group":
        return f"群 {ident}" if ident else "某个群"
    if kind == "private":
        return f"私聊 {ident}" if ident else "私聊"
    return ident or "别的会话"


def collect_for(items: list[dict[str, Any]], *, kind: str, ident: str, session_id: str,
                ttl_sec: float, now: float | None = None) -> list[dict[str, Any]]:
    """挑出与本会话相关的台账条目（新的在前）。

    两个方向都算相关：
      · 目标是本会话（别人让 bot 发给「你」）→ 你在私聊里问「谁让你发的」要能答上；
      · 来源是本会话（bot 在这个会话里被要求做的事）→ 在群里问「刚给我发什么了」也要能答上。
    """
    t = float(now if now is not None else time.time())
    keep: list[dict[str, Any]] = []
    for item in items:
        if t - float(item.get("ts") or 0) > ttl_sec:
            continue
        src_session = str(item.get("src_session") or "")
        same_source = bool(session_id) and src_session == session_id
        to_here = bool(ident) and str(item.get("target") or "") == ident and (
            not item.get("chat_type") or not kind or item.get("chat_type") == kind
        )
        if same_source or to_here:
            keep.append(item)
    keep.sort(key=lambda x: float(x.get("ts") or 0), reverse=True)
    return keep


def render_line(item: dict[str, Any], *, kind: str, ident: str, session_id: str,
                now: float | None = None) -> str:
    """把一条台账渲染成人话（供注入）。"""
    when = ago_text(float(item.get("ts") or 0), now)
    text = str(item.get("text") or "").replace("\n", " ").strip()
    if len(text) > NOTE_TEXT_LIMIT:
        text = text[:NOTE_TEXT_LIMIT] + "…"
    quoted = f"「{text}」" if text else ""
    verb = {
        "send": f"发了一条消息{quoted}",
        "schedule": f"排了一条定时消息{quoted}",
        "publish": f"发了一条动态{quoted}",
    }.get(str(item.get("kind") or "send"), f"发了一条消息{quoted}")
    actor_name = str(item.get("actor_name") or "").strip()
    actor_id = str(item.get("actor_id") or "").strip()
    actor = f"{actor_name}({actor_id})" if actor_name and actor_id else (actor_name or actor_id or "有人")
    src_where = where_text(str(item.get("src_kind") or ""), str(item.get("src_ident") or ""))
    tgt_where = where_text(str(item.get("target_kind") or ""), str(item.get("target") or ""))
    from_here = bool(session_id) and str(item.get("src_session") or "") == session_id
    if from_here:
        dest = tgt_where if item.get("target") else "别的地方"
        return f"{when}：我按你的要求在{dest}{verb}" if kind == "group" else f"{when}：我按你的要求给{dest}{verb}"
    if item.get("target") and str(item.get("target")) == ident:
        return f"{when}：{actor}在{src_where}里让我给你{verb}"
    return f"{when}：{actor}在{src_where}里让我{verb}"


def render_note(items: list[dict[str, Any]], *, kind: str, ident: str, session_id: str,
                now: float | None = None) -> str:
    """台账 → 注入文本（空列表 = 不注入）。"""
    if not items:
        return ""
    lines = [render_line(item, kind=kind, ident=ident, session_id=session_id, now=now)
             for item in items[:NOTE_ITEM_LIMIT]]
    header = (
        "【跨会话动作】下面这些是我在**别的会话**里做的事（本会话看不到它们，"
        "别人问起时照实说，不要装作不知道）："
    )
    return header + "\n" + "\n".join(f"- {line}" for line in lines if line)


def summarize(item: dict[str, Any], *, text_limit: int = ACTION_TEXT_LIMIT) -> tuple[str, str]:
    """台账 → 一条记忆的 (summary, content)。"""
    text = str(item.get("text") or "").replace("\n", " ").strip()
    if len(text) > text_limit:
        text = text[:text_limit] + "…"
    actor_name = str(item.get("actor_name") or "").strip()
    actor_id = str(item.get("actor_id") or "").strip()
    actor = f"{actor_name}({actor_id})" if actor_name and actor_id else (actor_name or actor_id or "有人")
    src_where = where_text(str(item.get("src_kind") or ""), str(item.get("src_ident") or ""))
    tgt_where = where_text(str(item.get("target_kind") or ""), str(item.get("target") or ""))
    verb = {"send": "发了一条消息", "schedule": "排了一条定时消息", "publish": "发了一条动态"}.get(
        str(item.get("kind") or "send"), "发了一条消息"
    )
    summary = f"{actor}在{src_where}里让我{verb}给{tgt_where}：{text}" if text else \
        f"{actor}在{src_where}里让我给{tgt_where}{verb}"
    content = f"{summary}（工具：{item.get('tool') or '未知'}）"
    return summary, content

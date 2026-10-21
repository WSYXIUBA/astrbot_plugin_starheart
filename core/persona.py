"""人格解析：把 AstrBot 原生 `resolve_selected_persona` 的正确用法收在一处。

坑（真机实测，P1-1）：AstrBot 4.28.2 的签名是 **keyword-only + 三个必填**——

    async def resolve_selected_persona(
        self, *, umo, conversation_persona_id, platform_name, provider_settings=None
    ) -> tuple[persona_id, persona, force_applied_persona_id, use_webchat_special_default]

插件早先只传 `umo=...` → TypeError → 被 `except TypeError: continue` 吞掉 → 静默回退全局
默认人格。表现是 `memory_persona_isolated` 形同虚设：会话切了人格，记忆还写全局那份。

这里做三件事：
1. 按官方四参数调用（umo / conversation_persona_id / platform_name / provider_settings）；
2. 签名不匹配时**一次性 warning**，不再静默降级；
3. 给一个按 UMO 的 TTL 缓存 —— 捕获路径（observe/settle/静默补总结）手上没有 `req`，
   只能复用注入时算出来的结果；缓存键必须是**原始 UMO**，不是归一化过的 session_id
   （webchat 会被折成 `webchat!user`，拿它查 scope 永远查空）。
"""

from __future__ import annotations

import inspect
import time
from typing import Any

from astrbot.api import logger

_WARNED: set[str] = set()


def reset_warnings() -> None:
    """测试用：清掉「已警告过」的标记，让下一次签名错误能再打一次。"""
    _WARNED.clear()


def warned_keys() -> set[str]:
    return set(_WARNED)


def umo_platform(umo: str) -> str:
    """从 UMO 取平台名。格式为 `platform_name:message_type:session_id`（AstrBot 官方约定）。"""
    return str(umo or "").split(":", 1)[0].strip()


async def resolve_persona_id(
    manager: Any,
    *,
    umo: str,
    conversation_persona_id: str | None = None,
    platform_name: str = "",
    provider_settings: dict[str, Any] | None = None,
    fallback: str = "",
) -> str:
    """解析会话最终生效的人格 id；任何异常都回退 `fallback`（通常是全局默认人格）。

    `manager` 是 AstrBot 的 PersonaManager（或同签名的假对象）。没有 UMO 时不查会话级
    —— 会话级覆盖本身就按 UMO 存（`sp.get_async(scope="umo", scope_id=umo)`）。
    """
    fn = getattr(manager, "resolve_selected_persona", None) if manager is not None else None
    if not callable(fn) or not str(umo or "").strip():
        return fallback
    plat = str(platform_name or "").strip() or umo_platform(umo)
    try:
        res = fn(
            umo=umo,
            conversation_persona_id=conversation_persona_id,
            platform_name=plat,
            provider_settings=provider_settings,
        )
        res = await res if inspect.isawaitable(res) else res
    except TypeError as exc:
        # 签名漂移是最难查的一类：旧实现 continue 掉，于是「会话人格」永远不生效还没日志。
        if "resolve_selected_persona" not in _WARNED:
            _WARNED.add("resolve_selected_persona")
            logger.warning(
                f"[StarHeart] AstrBot 人格解析签名不匹配，已回退全局默认人格（本条只提示一次）：{exc}"
            )
        return fallback
    except Exception as exc:                                # noqa: BLE001 解析失败不该打断对话
        logger.debug(f"[StarHeart] 人格解析失败，回退全局默认人格：{exc}")
        return fallback
    pid = res[0] if isinstance(res, (tuple, list)) and res else res
    return str(pid) if pid else fallback


class PersonaCache:
    """按原始 UMO 缓存「这个会话当前的人格」，默认 10 分钟。

    捕获路径没有 `req.conversation.persona_id`，只能吃注入时算出来的值；
    注入一轮算一次，捕获直接命中缓存，不再重复查存储。
    """

    def __init__(self, ttl: float = 600.0) -> None:
        self.ttl = float(ttl)
        self._data: dict[str, tuple[str, float]] = {}

    def put(self, umo: str, persona_id: str, now: float | None = None) -> None:
        key = str(umo or "").strip()
        if not key or not persona_id:
            return
        self._data[key] = (str(persona_id), float(time.time() if now is None else now))
        if len(self._data) > 512:                           # 防无界增长：丢最旧的一批
            for k, _ in sorted(self._data.items(), key=lambda kv: kv[1][1])[:128]:
                self._data.pop(k, None)

    def get(self, umo: str, now: float | None = None) -> str:
        key = str(umo or "").strip()
        if not key:
            return ""
        hit = self._data.get(key)
        if not hit:
            return ""
        pid, ts = hit
        if float(time.time() if now is None else now) - ts > self.ttl:
            self._data.pop(key, None)
            return ""
        return pid

    def clear(self) -> None:
        self._data.clear()

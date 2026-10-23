"""StarHeart：AstrBot 记忆增强插件主入口。

职责：
1. 生命周期：建库、注册面板页面 API、装载配置。
2. 旁听全部消息 → 本地门控打分落库（影子模式只记录不拦截）。
3. LLM 请求前注入记忆与关系语气；响应后结算好感；发送前做输出拟人。
4. 面板数据全部来自真实库，不放假数据。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import random
import re
import time
import urllib.parse
from collections import deque
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.event.filter import EventMessageType, PermissionType
from astrbot.api.message_components import Image, Plain, Record
from astrbot.api.provider import LLMResponse, ProviderRequest
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core.agent.message import TextPart
from astrbot.core.message.message_event_result import MessageChain, ResultContentType

from .core import actions, engine
from .core import persona as persona_rt
from .core.service import DEFAULT_CONFIG, StarHeartService
from .core.storage import Store, session_norm


def _local_path_from_url(url: str) -> str:
    """把消息组件里的图片引用还原成本地文件路径（三种形态都要认）。

    - `file:///AstrBot/data/x.jpg` → `/AstrBot/data/x.jpg`（Linux 真机形态）
    - `file:///C:/Users/x.jpg`     → `C:/Users/x.jpg`（Windows：AstrBot 用 Path.as_uri()，会多一道前导斜杠）
    - 普通路径原样返回（顺手做一次 %XX 解码）

    为什么不能用 `path.startswith("/")` 判本地文件：Windows 上剥掉 `file://` 得到的是 `/C:/x.jpg`，
    直接喂给 open() 是打不开的；而纯 `C:/x.jpg` 又不以 `/` 开头，会被当成「非本地」整张丢掉。
    """
    u = str(url or "").strip()
    if u.lower().startswith("file://"):
        u = u[7:]
        if len(u) >= 3 and u[0] == "/" and u[2] == ":":   # /C:/… → C:/…
            u = u[1:]
    try:
        return urllib.parse.unquote(u)
    except Exception:                                     # noqa: BLE001 解码失败就用原串
        return u

PLUGIN_NAME = "astrbot_plugin_starheart"

# 输出拟人：模型在回复里打这个标记 = 想配一张表情包（标记本身不会发出去）
STICKER_MARK = re.compile(r"\[\s*(?:表情|贴纸|sticker)\s*\]", re.I)

# 出站流水 kind：发送入口的 tag → 流水类型（每轮回复的首条由框架管线发出，kind='first_seg'）。
_SENT_KIND_BY_TAG = {
    "主动消息": "proactive",
    "撤回反应": "recall_react",
    "手动试发": "proactive",
    "分段补发": "resend",
}

FAVOUR_HINT = (
    "\n\n[系统] 若这轮对话让你对对方的好感或关系温度发生变化，请在回复的最末尾另起一行输出"
    "形如 [好感:+3] 或 [好感:-2] 的标记（整数，没有变化就不要输出）。该标记不会被用户看到。"
)

# 平台联系人结构体的键名：不是会话 id，任何写名单的路径都不得收
# （2026-10-11 真机 id_whitelist 里躺着这 9 个键名 —— 黑名单写回把 contacts() 的 dict 当 list 迭代了）
_CONTACT_SHAPE_KEYS = {
    "platforms", "groups", "friends", "admins", "bots", "errors",
    "member_names", "admin_in_friends", "admin_not_in_friends",
}


@register(
    "StarHeart",
    "肆一纸心",
    "记忆增强：把对话记成长期记忆，聊天时自动想起相关的；附带人物画像、关系与表达习惯，自带可视化面板。",
    "1.0.5",
    "https://github.com/WSYXIUBA/astrbot_plugin_starheart",
)
class StarHeartPlugin(Star):
    def __init__(self, context: Context, config: Any = None):
        super().__init__(context)
        self.context = context
        # 保留 AstrBot 注入的 AstrBotConfig 本体：它自带 save_config_async()，落盘的就是
        # WebUI 插件配置页读写的那一个 data/config/<插件>_config.json。
        # 以前这里 `self.plugin_config = config or {}` 把它当成普通 dict → 从此再也写不回原生配置。
        self.bot_config = config
        self.plugin_config = config or {}
        self.data_dir = Path(str(StarTools.get_data_dir(PLUGIN_NAME)))
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.store = Store(self._resolve_db_path(self.data_dir))
        self.service = StarHeartService(
            self.store,
            self._astrbot_data_root(),
            persona_provider={
                "list": self._persona_list,
                "update": self._persona_update,
            },
            system_provider={
                "providers": self._system_providers,
                "active_persona": self._active_persona_id,
                "activate_persona": self._activate_persona,
                "model_test": self._model_test,
                "llm_generate": self._llm_generate,
                # 面板保存配置 / 保存系统提示词都要写回原生插件配置；漏注册这里 = 写回恒失败（静默）
                "save_native_config": self.save_native_config,
                # 名单/可见性统一走 AstrBot 原生那份（platform_settings.enable_id_white_list / id_whitelist）
                "native_visibility_read": self._native_visibility_read,
                "native_visibility_write": self._native_visibility_write,
                # 主动开口要吃到 AstrBot 那份完整提示词与上下文：见 _agent_system_prompt()
                "agent_prompt": self._agent_system_prompt,
                "send_proactive": self._send_proactive,
                # 主动消息语义防复读：复用记忆的嵌入通道（失败自动回退字符相似度）
                "embed": self._embed,
                "contacts": self._platform_contacts_rich,   # 包装：返回前补私聊昵称（名单页/判定共用一个源头）
                # 私聊昵称真源：平台接口常只给号码，这里直连协议端 get_stranger_info
                "resolve_user_names": self._resolve_user_names,
                "persona_text": self._persona_text,
                "timezone": self._timezone,
            },
        )
        self.page_api = None
        self._ready = False
        self._init_task: asyncio.Task | None = None
        self._loop_task: asyncio.Task | None = None
        # 聊天记忆总结：会话缓冲 + 上次触发时间 + 后台任务句柄
        self._capture_bufs: dict[str, list[dict[str, str]]] = {}
        # 主动消息写回失败的会话：下一轮用「你最近主动发过」知会块兜底
        self._writeback_failed: set[str] = set()
        # 连发合并：key = (会话, 发送者)，value = 该人最近几条原话 + 计时器
        self._burst: dict[str, dict[str, Any]] = {}
        # 会话滑动窗口：key = session_id，value = 时间戳 deque（存在感惩罚用）
        self._session_windows: dict[str, deque] = {}
        # 会话最近两条消息的时间（「距上一条消息多久」用；AstrBot 的对话历史里没有时间）
        self._session_touch: dict[str, dict[str, float]] = {}
        # 门控退避：key = session_id，value = {"n": 指数档, "until": 退避到什么时候, "last": 最近活动}
        self._backoff: dict[str, dict[str, Any]] = {}
        # AstrBot 拼好的完整系统提示词 + 上下文尾（按会话缓存）：主动开口本质是一次对话回复，
        # 必须吃到同一份提示词，而不是插件自己拼一个人格 —— 见 inject_context 的写入处。
        self._agent_prompt_cache: dict[str, dict[str, Any]] = {}
        # 缓存一版门控配置给同步的窗口计数用（_initialize 时刷新）
        self._window_cfg: dict[str, Any] = {}
        self._capture_last: dict[str, float] = {}
        # 会话 → 原始 UMO：捕获路径（observe/settle/静默补总结）手上只有归一化 session_id，
        # 而人格的会话级覆盖按**原始 UMO** 存，拿归一化 id 去查永远查空（P1-1）。
        self._capture_umo: dict[str, str] = {}
        # 按原始 UMO 缓存「这个会话的人格」：注入时算一次，捕获直接吃缓存
        self._persona_cache = persona_rt.PersonaCache(ttl=600.0)
        # 跨会话动作台账：bot 借工具（含第三方工具插件）在**别的会话**里干了什么。
        # 条目只是运行期台账（带 TTL），长期可回忆的部分由 _write_action_memory 落成记忆。
        self._action_ledger: list[dict[str, Any]] = []
        # 认不出来的「疑似外发工具」只报一次 debug，避免每轮刷屏
        self._action_unknown_seen: set[str] = set()
        self._bg_tasks: set[asyncio.Task] = set()
        self._register_page_api()
        self._init_task = asyncio.create_task(self._initialize_boot())

    # ---------------- 路径与初始化 ----------------
    @staticmethod
    def _resolve_db_path(data_dir: Path) -> Path:
        """数据文件路径。

        旧版本的文件叫 `heartflow.db`：改名 StarHeart 后第一次启动会自动把它改过来，
        免得老用户升级上来记忆全空。改名失败就继续用旧文件，绝不因此启动不了。
        """
        new_db = data_dir / "starheart.db"
        old_db = data_dir / "heartflow.db"
        if not new_db.exists() and old_db.exists():
            try:
                for suf in ("", "-wal", "-shm"):
                    src = Path(str(old_db) + suf)
                    if src.exists():
                        src.rename(Path(str(new_db) + suf))
                logger.info("[StarHeart] 数据文件 heartflow.db → starheart.db")
            except Exception as exc:                          # noqa: BLE001 改名失败接着用旧文件
                logger.warning(f"[StarHeart] 数据文件改名失败，继续使用旧文件：{exc}")
                return old_db
        return new_db

    @staticmethod
    def _astrbot_data_root() -> Path:
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_data_path

            return Path(get_astrbot_data_path())
        except Exception:  # 老版本没有这个工具函数时退回到插件数据目录的上级
            return Path(str(StarTools.get_data_dir(PLUGIN_NAME))).parent.parent

    def _register_page_api(self) -> None:
        if not hasattr(self.context, "register_web_api"):
            logger.warning("[StarHeart] 当前 AstrBot 不支持插件页面 API，面板不可用")
            return
        try:
            from .core.page_api import PageApi

            self.page_api = PageApi(self)
            self.page_api.register_routes()
        except Exception as exc:
            logger.error(f"[StarHeart] 面板 API 注册失败：{exc}")
            self.page_api = None

    async def _initialize_boot(self) -> None:
        """带重试的启动初始化。

        以前 _initialize 的 except 只打一行日志：一旦 store.connect() 失败（数据库被占用、
        data 目录还没挂载好），_ready 就永远是 False，所有钩子都会走 _ensure_ready 等超时，
        等于插件静默失效到下次重启 —— 而日志里只有一行「初始化失败」。
        现在有限次重试；全部失败才放弃，并把原因说清楚。
        """
        for attempt in range(1, self.INIT_MAX_ATTEMPTS + 1):
            try:
                await self._initialize()
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:                        # noqa: BLE001
                if attempt >= self.INIT_MAX_ATTEMPTS:
                    logger.error(
                        f"[StarHeart] 初始化连续 {self.INIT_MAX_ATTEMPTS} 次失败，已放弃：{exc}"
                        "（插件保持未就绪；重载插件可再试）"
                    )
                    return
                logger.warning(
                    f"[StarHeart] 初始化第 {attempt}/{self.INIT_MAX_ATTEMPTS} 次失败：{exc}"
                    f"，{self.INIT_RETRY_SECONDS:.0f}s 后重试"
                )
                await asyncio.sleep(self.INIT_RETRY_SECONDS)

    async def _sync_db_values_to_native(self) -> list[str]:
        """把「库（面板）里的值」补写回原生配置页 —— 只补该补的，绝不覆盖用户刚设的。

        两类要补：
          ① 原生是空（None / "" / [] / {}）而库里有值 —— 面板是权威入口，历史数据没人补过；
          ② 原生**非空但只是 schema 默认值**（AstrBot 物化出来的 false / 0 / []）且与库不同 ——
             判据是「原生值 == 上次启动时记的快照」（= 这次启动后没人动过它）。
             原生值 ≠ 快照 ⇒ 用户刚在原生页改过 ⇒ 一律不动，交给紧随其后的「变更采纳」（谁最后动谁赢）。
        """
        # 上次启动时的原生快照（双层 JSON 编码的坑也顺手兼容）
        raw_snap = await self.store.get_config("native_cfg_snapshot", "")
        prev: Any = {}
        try:
            prev = json.loads(raw_snap) if isinstance(raw_snap, str) else raw_snap
            if isinstance(prev, str):
                prev = json.loads(prev)
        except Exception:                                        # noqa: BLE001
            prev = {}
        if not isinstance(prev, dict):
            prev = {}

        pending: dict[str, Any] = {}
        for key in self._schema_keys():
            try:
                nat = self.plugin_config.get(key, None)
            except Exception:                                    # noqa: BLE001
                nat = None
            try:
                dbv = await self.store.get_config(key, None)
            except Exception:                                    # noqa: BLE001
                continue
            if dbv in (None, "", [], {}):
                continue                                         # 库里也是空 → 无事可做
            # 「空」的判定要严：只有 None / 空字符串 / 空列表 / 空 dict 才算没设过。
            # ⚠️ 不能用 `str(nat or "")` —— 那会把 **false / 0** 也当成空，
            # 于是用户刚在原生页关掉的开关（false）会被库里的 true 覆盖回去（0.50.3 的隐患）。
            _is_empty = (nat is None
                         or (isinstance(nat, str) and not nat.strip())
                         or (isinstance(nat, (list, dict)) and not nat))
            if not _is_empty and not (key in prev and prev.get(key) == nat and nat != dbv):
                continue                                         # 原生有值且不是"没动过的默认值" → 尊重原生页
            pending[key] = dbv
        if not pending:
            return []
        try:
            r = await self.save_native_config(pending)
        except Exception as exc:                                 # noqa: BLE001
            logger.warning(f"[StarHeart] 配置补写原生页失败：{exc}")
            return []
        logger.info(
            "[StarHeart] 配置补写原生页：%d 项（%s）ok=%s"
            % (len(pending), ", ".join(sorted(pending)), bool((r or {}).get("ok")))
        )
        return sorted(pending)

    async def _adopt_native_config_changes(self) -> list[str]:
        """原生配置页的改动采纳：原生值**变了**就以原生为准，否则以库（面板）为准。

        为什么需要它：_conf_schema.json 里的键在「AstrBot 原生插件配置页」和「本插件面板」两处都能改，
        而读配置的唯一权威是库里的 hf_config。原实现只在库里没有该键时（首启）把原生值 seed 一次 ——
        于是首启之后在原生页改任何键都**静默不生效**，用户面对的是一个假控件。

        为什么用「变更检测」而不是二选一：
          · 原生永远优先 → 面板里每次修改都会被下次启动时的原生旧值覆盖回去；
          · 库永远优先   → 就是现在这个 bug；
          · 变更检测     → 谁最后动就以谁为准，两处都能真用，且不会互相覆盖。

        升级安全：库里还没有快照时**不采纳任何值**（只把当前原生值记成基线），
        避免第一次升级到本版本时，把用户早已在面板里改过的值冲回原生旧值。
        """
        native = self.plugin_config or {}
        keys = [k for k in self._schema_keys() if k in native]
        if not keys:
            return []
        cur = {k: native.get(k) for k in keys}
        raw = await self.store.get_config("native_cfg_snapshot", "")
        prev: Any = {}
        if raw:
            try:
                prev = json.loads(raw) if isinstance(raw, str) else raw
            except (TypeError, ValueError):
                prev = {}
        adopted: list[str] = []
        if isinstance(prev, dict) and prev:
            for k, v in cur.items():
                if k in prev and prev[k] != v:
                    await self.store.set_config(k, v)
                    adopted.append(k)
        if adopted:
            logger.info(f"[StarHeart] 采纳原生配置页的改动：{', '.join(sorted(adopted))}")
        await self.store.set_config(
            "native_cfg_snapshot", json.dumps(cur, ensure_ascii=False, sort_keys=True)
        )
        return adopted

    async def _migrate_r1_defaults(self) -> None:
        """一次性把 R1 前的旧默认值迁到通用默认值（只迁「仍是旧默认」的键）。"""
        try:
            ver = int(await self.store.get_config("config_version", 0) or 0)
        except (TypeError, ValueError):
            ver = 0
        if ver >= 2:
            return
        changed: dict[str, Any] = {}

        async def _set(key: str, value: Any) -> None:
            old = await self.store.get_config(key, None)
            if old != value:
                await self.store.set_config(key, value)
                changed[key] = {"old": old, "new": value}

        for kind in ("group", "private"):
            mode = str(await self.store.get_config(f"proactive_mode_{kind}", "") or "").lower()
            wl = await self.store.get_config(f"proactive_wl_{kind}", []) or []
            if mode == "white" and not wl:
                await _set(f"proactive_mode_{kind}", "black")
            cmode = str(await self.store.get_config(f"conn_mode_{kind}", "") or "").lower()
            cwhite = await self.store.get_config(f"conn_white_{kind}", []) or []
            cblack = await self.store.get_config(f"conn_black_{kind}", []) or []
            if cmode == "white" and not cwhite and not cblack:
                await _set(f"conn_mode_{kind}", "black")
        if str(await self.store.get_config("proactive_min_zone", "") or "") == "FAMILIAR":
            await _set("proactive_min_zone", "")
        if bool(await self.store.get_config("proactive_shadow", False)):
            await _set("proactive_shadow", False)
        if not bool(await self.store.get_config("proactive_writeback", True)):
            await _set("proactive_writeback", True)
        if not bool(await self.store.get_config("proactive_note_enabled", True)):
            await _set("proactive_note_enabled", True)
        old_style = "自然地开一句话题，短、不追问、不解释自己为什么开口"
        if str(await self.store.get_config("proactive_style", "") or "") == old_style:
            await _set("proactive_style", "用你平时说话的方式，想说什么就说什么")
        await self.store.set_config("config_version", 2)
        if changed:
            logger.info(f"[StarHeart] 配置默认值迁移（R1）：{', '.join(sorted(changed))}")
            await self.store.audit("config.migrate_r1", json.dumps(changed, ensure_ascii=False)[:1000])

    async def _initialize(self) -> None:
        try:
            await self.store.connect()
            seed = dict(DEFAULT_CONFIG)
            seed.update({k: v for k, v in self.plugin_config.items() if k in DEFAULT_CONFIG})
            for key, value in seed.items():
                if await self.store.get_config(key, None) is None:
                    await self.store.set_config(key, value)
            # 旧默认值一次性迁移（白名单空/好感门槛/影子/写回/风格）
            await self._migrate_r1_defaults()
            # 系统提示词：面板是权威入口，但用户在 AstrBot 原生插件配置页改的那几个键也要能生效 ——
            # 规则：本库为空（从没在面板改过）而原生有值 → 采用原生值；本库有值 → 以本库为准（面板权威）。
            # 不这样，原生页那几个框就是"改了不生效"的假控件。
            for key in list(DEFAULT_CONFIG):
                if not key.startswith("sys_prompt_"):
                    continue
                native_val = str(self.plugin_config.get(key) or "")
                if native_val.strip() and not str(await self.store.get_config(key, "") or "").strip():
                    await self.store.set_config(key, native_val)
                    logger.info(f"[StarHeart] 采纳原生配置页里的系统提示词：{key}")
            # 面板设过、原生页还是空的键 → 写回原生配置页（0.50.0 把 39 个键补进 schema 后，
            # AstrBot 用 schema 默认值把它们物化成空串，而面板里早就填过值：不补写回去，
            # 原生页就永远显示「未选择」，两边说的不一样）
            try:
                await self._sync_db_values_to_native()
            except Exception as exc:                             # noqa: BLE001
                logger.warning(f"[StarHeart] 配置补写原生页跳过：{exc}")
            # 原生配置页的**后续改动**采纳（首启之后在原生页改键不再静默失效）——见方法注释
            try:
                await self._adopt_native_config_changes()
            except Exception as exc:                             # noqa: BLE001 采纳失败不该挡住启动
                logger.warning(f"[StarHeart] 原生配置变更采纳跳过：{exc}")
            # 配置登记自检（无开关）：默认值 / 配置域 / 护栏 / schema / 面板字段五处不一致时自动补。
            # 每次启动都跑；插件代码更新后 AstrBot 重载 → 这里自动触发，不依赖用户点按钮。
            try:
                audit = await self.service.config_registry_audit()
                if audit.get("added_schema") or audit.get("added_domain"):
                    logger.info(
                        f"[StarHeart] 配置自检已自动补登记：schema={audit['added_schema']} 域={audit['added_domain']}"
                    )
            except Exception as exc:                             # noqa: BLE001 自检失败不该挡住启动
                logger.warning(f"[StarHeart] 配置登记自检跳过：{exc}")
            self._ready = True
            self._window_cfg = dict(seed)
            # 掉线宽限要跨重启：把以前认过的 OneBot 实例预载进内存（store.known_platforms 此前定义了从没被读过）
            try:
                known = await self.store.known_platforms()
                if known:
                    self._known_platforms = set(known)
            except Exception as exc:                         # noqa: BLE001 预载失败不影响启动
                logger.debug(f"[StarHeart] 平台名单预载跳过：{exc}")
            # 时间基一次性修复（幂等）：真机上 last_used_at 全 0、部分 occurred_at 在未来，
            # 会让这些记忆永久免疫衰减 —— 启动时顺手修掉，并补齐衰减基线 strength_updated_at。
            # 人格隔离修复（幂等）：跨人格的边置为 inactive —— 隔离默认开启，跨人格边是污染
            try:
                leak = await self.store.repair_persona_leaks()
                try:      # 画像也按人格回填（旧库的画像本来没有归属，属"未归属"）
                    fixed = await self.store.repair_profile_personas()
                    if fixed:
                        logger.info(f"[StarHeart] 画像人格回填：{fixed} 份")
                except Exception as exc:      # noqa: BLE001 回填失败不许挡住启动
                    logger.warning(f"[StarHeart] 画像人格回填失败：{exc}")
                if leak.get("cross_persona"):
                    logger.warning(f"[StarHeart] 人格隔离修复：{leak['cross_persona']} 条跨人格边已停用")
            except Exception as exc:                             # noqa: BLE001
                logger.warning(f"[StarHeart] 人格隔离修复跳过：{exc}")
            try:
                fixed = await self.store.repair_memory_timebase()
                if any(fixed.values()):
                    logger.info(f"[StarHeart] 记忆时间基已修复：{fixed}")
            except Exception as exc:                             # noqa: BLE001
                logger.warning(f"[StarHeart] 时间基修复跳过：{exc}")
            if self._loop_task is None or self._loop_task.done():
                self._loop_task = asyncio.create_task(self._background_loop())
            # 一次性补跑：把 pending 的 v2 episode 抽完（默认关，只用于切读前验证/补课）
            try:
                _cfg0 = await self.service.config()
                if _cfg0.get("v2_extract_on_boot", False):
                    self._spawn(self._v2_drain_pending(), label="v2-drain")
                    logger.info("[StarHeart] v2 启动补跑已派发")
            except Exception as exc:                             # noqa: BLE001
                logger.debug(f"[StarHeart] v2 启动补跑跳过：{exc}")
            stats = await self.store.stats()
            logger.info(
                f"[StarHeart] 初始化完成：记忆 {stats['memories']} 条 / 实体 {stats['entities']} / 边 {stats['edges']}"
            )
        except Exception as exc:
            logger.error(f"[StarHeart] 初始化失败：{exc}")
            raise

    def _spawn(self, coro: Any, *, label: str = "") -> asyncio.Task | None:
        """登记一个后台小任务：统一进 _bg_tasks，跑完自动摘除。

        为什么必须统一登记：terminate() 只取消 _init_task / _loop_task / _bg_tasks
        里登记过的任务，然后立刻 store.close()。漏登记的 create_task 会在关库之后
        继续跑（写库、补发消息），表现为「卸载/重载插件后报库已关闭」或停用后消息还在冒。
        另外未持引用的 task 还可能被 GC 提前回收。
        """
        try:
            task = asyncio.create_task(coro)
        except Exception as exc:                            # noqa: BLE001 建任务失败不该冒泡
            logger.warning(f"[StarHeart] 后台任务创建失败{'（' + label + '）' if label else ''}：{exc}")
            return None
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)
        return task

    async def _ensure_ready(self, timeout: float = 8.0) -> bool:
        if self._ready:
            return True
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._ready:
                return True
            await asyncio.sleep(0.2)
        return self._ready

    async def terminate(self) -> None:
        # service 的导入任务也会写库：漏掉它就会「库已关、任务还在写」
        svc_tasks = [t for t in (getattr(self.service, "_import_task", None),) if t]
        all_tasks = [self._init_task, self._loop_task, *list(self._bg_tasks), *svc_tasks]
        for task in all_tasks:
            if task and not task.done():
                task.cancel()
        # 等后台任务真正退出再关库，避免「任务还在写、连接已关闭」的竞态
        pending = [t for t in all_tasks if t and not t.done()]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._capture_bufs.clear()
        self._capture_last.clear()
        self._burst.clear()
        self._session_windows.clear()
        self._backoff.clear()
        self._agent_prompt_cache.clear()
        await self.store.close()
        logger.info("[StarHeart] 已卸载，数据库连接已关闭")





    # ---------------- 向量：嵌入生成 + 余弦召回 ----------------
    def _first_tool_provider(self, kind: str):
        """AstrBot 已配置的「嵌入 / 重排」提供商里的第一个。

        为什么需要它：这两个槽位**留空 = 跟随 AstrBot 已配置的那个**，而**不是**"跟随会话当前模型" ——
        对话模型没有 get_embedding/rerank，按"跟随会话"选等于把功能静默关掉（面板过去正是这么标的）。
        """
        try:
            if kind == "embedding":
                fn = getattr(self.context, "get_all_embedding_providers", None)
                items = fn() if callable(fn) else []
            else:
                items = list(getattr(getattr(self.context, "provider_manager", None), "rerank_provider_insts", []) or [])
            for item in items or []:
                return item
        except Exception as exc:                            # noqa: BLE001
            logger.warning(f"[StarHeart] 取 AstrBot 已配置的{kind}提供商失败：{exc}")
        return None

    @staticmethod
    def _gen_name(active: Any) -> str:
        """vector_gens() 的 active 是整条记录（dict），不是代名字符串。"""
        if isinstance(active, dict):
            return str(active.get("gen") or active.get("gen_key") or "")
        return str(active or "")

    def _embed_target(self, cfg: dict[str, Any]) -> tuple[str, int, Any]:
        """当前 embedding 模型的真实身份： (模型名, 维度, provider)。

        维度必须从 provider 拿（不是猜），因为 gen_key 是「模型|维度」；
        只有模型名没有维度时，回填层会先用一条短文本探一次再定代。
        """
        pid = str(cfg.get("model_embedding") or "")
        provider = self.context.get_provider_by_id(pid) if pid else self._first_tool_provider("embedding")
        if provider is None:
            return "", 0, None
        model = ""
        try:
            meta = provider.meta()
            model = str(getattr(meta, "model", "") or getattr(meta, "id", "") or "")
        except Exception:                                  # noqa: BLE001
            model = ""
        if not model:
            model = pid
        dim = 0
        try:
            get_dim = getattr(provider, "get_dim", None)
            if callable(get_dim):
                dim = int(get_dim() or 0)
        except Exception:                                  # noqa: BLE001
            dim = 0
        return model, dim, provider

    def _embed_model_name(self, cfg: dict[str, Any]) -> str:
        """取当前**实际会用**的嵌入模型名（槽位锁定就用它，留空就用跟随到的那个 provider）。"""
        return self._embed_target(cfg)[0]

    async def _embed(self, text: str) -> list[float] | None:
        """用「模型分配 → 向量嵌入」指定的模型算向量。

        槽位留空 → 跟随 AstrBot 已配置的嵌入提供商；一个都没配或调用失败 → None（关键词召回兜底）。
        """
        try:
            cfg = await self.service.config()
        except Exception:
            cfg = {}
        pid = str(cfg.get("model_embedding") or "")
        provider = self.context.get_provider_by_id(pid) if pid else self._first_tool_provider("embedding")
        if provider is None:
            return None
        last_exc: Exception | None = None
        for attempt in range(2):                       # 真机踩过：上游 503 抖动 + client closed，重试一次能救回
            try:
                vec = await provider.get_embedding(text[:512])
                return [float(x) for x in vec] if vec else None
            except Exception as exc:                  # noqa: BLE001
                last_exc = exc
                if attempt == 0:
                    await asyncio.sleep(1.0)
                    continue
        logger.warning(f"[StarHeart] 嵌入失败（{pid}）：{last_exc}")
        return None

    @staticmethod
    def _cosine(a: list[float], b_blob: bytes) -> float:
        import array
        import math
        b = array.array("f")
        b.frombytes(b_blob)
        if len(b) != len(a) or not len(a):
            return 0.0
        dot = sum(x * y for x, y in zip(a, b))
        na = math.sqrt(sum(x * x for x in a))
        nb = math.sqrt(sum(y * y for y in b))
        return dot / (na * nb) if na > 0 and nb > 0 else 0.0

    async def _inject_persona_id(
        self,
        umo: str = "",
        *,
        conversation_persona_id: str | None = None,
        platform_name: str = "",
        use_cache: bool = False,
    ) -> str:
        """注入/召回/捕获用的当前人格 id（按会话解析；隔离开关关掉时返回空 = 不过滤）。

        旧实现只读全局默认人格（`agent_runner.config.persona.persona_id`），会话切到
        别的 persona 后记忆仍写全局人格、注入又从全局找 —— `memory_persona_isolated`
        形同虚设。

        `use_cache=True` 走按 **原始 UMO** 的缓存：捕获路径没有 `req`，只能复用注入时
        算出来的结果（缓存为空则现算，算完回填）。
        """
        try:
            cfg = await self.service.config()
        except Exception:
            cfg = {}
        if not cfg.get("memory_persona_isolated", True):
            return ""
        key = str(umo or "").strip()
        if use_cache and key:
            hit = self._persona_cache.get(key)
            if hit:
                return hit
        pid = await self._active_persona_id(
            key,
            conversation_persona_id=conversation_persona_id,
            platform_name=platform_name,
        )
        if pid and key:
            self._persona_cache.put(key, pid)
        return pid

    def _persona_umo_for_session(self, session_id: str) -> str:
        """会话归一化 id → 原始 UMO（捕获路径用）。没记录过就返回空串。"""
        sid = str(session_id or "")
        if not sid:
            return ""
        return str(self._capture_umo.get(sid) or "")

    # ---------------- 语料：学群友/好友怎么说话 ----------------
    async def _record_corpus(self, event: AstrMessageEvent, text: str, *, is_bot: bool) -> None:
        """把一条消息落进语料库。命令/空消息/超长不入库；bot 自己的话也入库用于自省。"""
        body = (text or "").strip()
        if not body or body.startswith("/") or len(body) > 300:
            return
        low = body.lower()
        # 命令残骸（/reset 走 wake 后 message_str 只剩 reset）与常见指令词不入库
        if low in {"reset", "help", "状态", "菜单", "help.", "?"} or body.startswith("!"):
            return
        try:
            is_private = bool(event.is_private_chat())
        except Exception:
            is_private = False
        persona_id = self._active_persona_id_sync() if is_bot else ""
        if is_bot:
            name = persona_id
        else:
            name_fn = getattr(event, "get_sender_name", None)
            try:
                name = (name_fn() if callable(name_fn) else "") or ""
            except Exception:
                name = ""
        await self.store.add_corpus(
            {
                "session_id": self._sid(event),
                "chat_type": "私聊" if is_private else "群聊",
                "user_id": self._sender_id(event),
                "user_name": name,
                "persona_id": persona_id,
                "text": body,
                "is_bot": is_bot,
                "message_id": self._msg_id_of(event),   # 出处侧车（E3）：学到这条原话时能对回哪条消息
            }
        )

    # ---------------- 连发合并（同一人短时间连发多条 → 一次回复） ----------------
    def _burst_key(self, event: AstrMessageEvent) -> str:
        sender = self._sender_id(event) or "?"
        return f"{self._sid(event)}|{sender}"

    async def _burst_push(self, event: AstrMessageEvent, text: str) -> list[str]:
        """记录一条并返回「本轮之前同一人已发的几条」（不含当前这条）。

        只旁路记录，不改 event、不阻塞：真正的合并靠 on_llm_request 把这些原话
        交给 bot，由它自己判断该合并回一条还是逐条回。
        """
        body = (text or "").strip()
        if not body:
            return []
        cfg = await self.service.config()
        if not cfg.get("master_switch", True) or not cfg.get("burst_merge_enabled", True):
            return []
        try:
            is_private = bool(event.is_private_chat())
        except Exception:
            is_private = False
        # 群聊默认不按人合并（多人交错时逐条独立回复更自然），可配置开启
        if not is_private and not cfg.get("burst_merge_group", False):
            return []
        key = self._burst_key(event)
        now = time.time()
        window = max(0.5, float(cfg.get("burst_merge_window", 3.0) or 3.0))
        slot = self._burst.get(key)
        if slot is None or now - float(slot.get("ts") or 0.0) > window:
            slot = {"msgs": [], "ts": now}
            self._burst[key] = slot
        previous = [m for m in slot["msgs"] if isinstance(m, str)]
        slot["msgs"].append(body)
        slot["ts"] = now
        # 只留窗口内的量，防止长连发把 prompt 撑爆
        if len(slot["msgs"]) > 6:
            slot["msgs"] = slot["msgs"][-6:]
        return previous

    async def _burst_merge_into(self, event: AstrMessageEvent, req: Any) -> str:
        """把窗口内同一人连发的几条拼成一条，直接顶替本轮 LLM prompt。

        官方管线在 build_main_agent 阶段就把 req.prompt 从 event.message_str
        固化了下来（on_llm_request 钩子时 message_str 的修改早已无效），
        所以这里必须改 req.prompt 本身。
        """
        key = self._burst_key(event)
        slot = self._burst.get(key)
        if not slot:
            return ""
        msgs: list[str] = [m for m in slot.get("msgs", []) if isinstance(m, str)]
        if len(msgs) < 2:
            return ""
        merged = " ".join(m.strip() for m in msgs if m.strip())
        if not merged or len(merged) > 600:
            return ""
        try:
            # 当前这条消息已在 on_llm_request 前 append 进了 slot（observe 阶段），
            # 最后一晚收到的 prompt = 全部连发内容
            if hasattr(req, "prompt"):
                req.prompt = merged
        except Exception:
            return ""
        return merged

    async def _burst_clear(self, event: AstrMessageEvent) -> None:
        self._burst.pop(self._burst_key(event), None)

    # ---------------- 会话消息窗口（存在感惩罚的判据） ----------------
    async def _session_window_count(self, session_id: str) -> int:
        """最近 gate_window_seconds 秒内本会话的消息条数（含当前这条）。"""
        cfg_window = 20.0
        try:
            # 读实时配置：以前读的是初始化时的快照，改了 gate_window_seconds 不重启不生效
            live = await self.service.config()
            cfg_window = float(live.get("gate_window_seconds", 20) or 20)
        except Exception:
            try:
                cfg_window = float(self._window_cfg.get("gate_window_seconds", 20) or 20)
            except Exception:
                pass
        now_ts = time.time()
        dq = self._session_windows.setdefault(session_id, deque())
        while dq and now_ts - dq[0] > cfg_window:
            dq.popleft()
        dq.append(now_ts)
        if len(self._session_windows) > 256:
            # 以前这里是 `if not v`（只删空 deque）—— 而任何发过言的会话，deque 里必有时间戳，
            # 条件恒假，等于完全没有清理。改成按「最后一次活动时间」淘汰，
            # 并顺手把其它几个只增不减的会话级字典一起修剪。
            self._prune_memory_maps(now_ts, aggressive=True)
        return len(dq)

    def _prune_memory_maps(self, now_ts: float | None = None, *, aggressive: bool = False) -> None:
        """修剪会话级内存字典，防止长期运行无界增长。

        这些字典都以会话/发送者为键、只增不减：
          _session_windows / _backoff / _burst / _capture_bufs / _capture_last / _session_touch
        （_agent_prompt_cache 另有 64 条上限，其余此前完全没有清理。）
        群聊里 bot 常常不回，槽位就一直挂着 —— 跑几个月就是稳定的内存泄漏。
        """
        t = float(now_ts or time.time())
        idle = self.MEM_MAP_IDLE_SECONDS
        # 会话滑动窗口：deque 里存的是时间戳
        for key in [k for k, dq in self._session_windows.items() if not dq or t - float(dq[-1]) > idle]:
            self._session_windows.pop(key, None)
        # 门控退避状态
        for key in [k for k, v in self._backoff.items() if t - float(v.get("last") or 0) > idle]:
            self._backoff.pop(key, None)
        # 连发合并槽位：沉默 30 分钟就丢（远长于合并窗口，不会误伤正在连发的人）
        for key in [k for k, slot in self._burst.items() if t - float(slot.get("ts") or 0) > 1800.0]:
            self._burst.pop(key, None)
        # 记忆捕获缓冲：2 小时没动过的会话整槽丢掉（缓冲本来就是给「马上要总结」用的）
        for key in [k for k, ts in self._capture_last.items() if t - float(ts or 0) > 7200.0]:
            self._capture_last.pop(key, None)
            self._capture_bufs.pop(key, None)
        # 会话最近两条消息的时间：闲置就丢（下次说话自然重建，只是那一轮不说「距上一条多久」）
        for key in [k for k, v in self._session_touch.items() if t - float(v.get("last") or 0) > idle]:
            self._session_touch.pop(key, None)
        # 跨会话动作台账：过期丢（只服务短期知会，长期记忆另有落库路径）
        if self._action_ledger:
            fresh = [x for x in self._action_ledger
                     if t - float(x.get("ts") or 0) <= self.ACTION_LEDGER_KEEP_SECONDS]
            if len(fresh) != len(self._action_ledger):
                self._action_ledger[:] = fresh
        if aggressive:
            # 没有 _capture_last 打点的残槽（历史版本留下的）与最旧的提示词缓存
            for key in [k for k in self._capture_bufs if k not in self._capture_last]:
                self._capture_bufs.pop(key, None)
            for k, _ in sorted(
                self._agent_prompt_cache.items(), key=lambda kv: float(kv[1].get("ts") or 0)
            )[:16]:
                self._agent_prompt_cache.pop(k, None)

    # ---------------- 门控退避（连发到一定密度就安静一会儿） ----------------
    # 为什么要有它：engine.score_message() 里早就有 in_backoff 分支与 gate_backoff_base/max 参数，
    # 但主流程从不进入退避（永远传 False），所以面板「消息事件」里的「退避 backoff」统计永远是 0。
    # 这里补上状态机：窗口内连发够密 → 退避 base 秒起步、每触发一次翻倍、上限 max 秒；
    # 静默 10 分钟自动复位指数。base <= 0 = 完全关闭退避。
    BACKOFF_TRIGGER = 4          # 窗口内连发达到这个条数就进入退避
    BACKOFF_RESET_AFTER = 600.0  # 静默这么久，指数档位归零

    # 单次后台 LLM 调用的上限。以前是裸 await provider.text_chat(...)：没有超时，
    # 而 proactive_scan / 总结 / 画像都在 _background_loop 里 await 它 ——
    # 一次网络挂起就会把整条后台链（衰减、遗忘、画像、主动扫描）全部堵死到进程重启。
    LLM_TIMEOUT_SECONDS = 90.0

    # 初始化重试：一次失败（数据库被占用、目录还没挂载好）不该让插件永久失活。
    INIT_MAX_ATTEMPTS = 3
    INIT_RETRY_SECONDS = 20.0

    # 会话级内存字典的保留上限（进 _prune_memory_maps）
    MEM_MAP_IDLE_SECONDS = 3600.0

    # 跨会话动作台账：条数上限 + 最长保留。台账只服务「刚刚/最近」的知会，
    # 长期可回忆的部分由 _write_action_memory 写成记忆条目，所以这里丢得起。
    ACTION_LEDGER_MAX = 200
    ACTION_LEDGER_KEEP_SECONDS = 6 * 3600.0

    def _backoff_tick(self, session_id: str, window_count: int, cfg: dict[str, Any]) -> bool:
        try:
            base = float(cfg.get("gate_backoff_base") or 0)
            cap = float(cfg.get("gate_backoff_max") or 300)
        except Exception:
            return False
        if base <= 0 or not session_id:
            return False
        now_ts = time.time()
        slot = self._backoff.get(session_id)
        if slot and now_ts - float(slot.get("last") or 0) > self.BACKOFF_RESET_AFTER:
            slot = None
        active = bool(slot and now_ts < float(slot.get("until") or 0))
        if window_count >= self.BACKOFF_TRIGGER:
            n = int((slot or {}).get("n") or 0) + 1
            self._backoff[session_id] = {
                "n": n, "until": now_ts + min(cap, base * (2 ** (n - 1))), "last": now_ts,
            }
            return True
        if slot:
            slot["last"] = now_ts
            self._backoff[session_id] = slot
        if len(self._backoff) > 256:      # 防字典无界增长
            for k in [k for k, v in self._backoff.items() if now_ts > float(v.get("until") or 0)][:64]:
                self._backoff.pop(k, None)
        return active

    # ---------------- 聊天记忆自动总结（攒批 + 后台蒸馏） ----------------
    def _capture_buffer(self, session_id: str) -> list[dict[str, str]]:
        buf = self._capture_bufs.get(session_id)
        if buf is None:
            buf = []
            self._capture_bufs[session_id] = buf
        return buf

    async def _capture_user_only(self, event: AstrMessageEvent, user_text: str,
                                images: list[str] | None = None) -> None:
        """用户发言就入缓冲（bot 还没回或根本不会回，例如群聊里没 @ 它）。

        纯图片消息 message_str 为空但图本身是记忆来源：只拦「无文字且无图」。
        """
        if not user_text.strip() and not images:
            return
        await self._capture_push(event, user_text.strip(), "", images=images)

    async def _capture_with_reply(self, event: AstrMessageEvent, user_text: str, bot_text: str,
                                 images: list[str] | None = None) -> None:
        """bot 有回应时入缓冲：同一条用户消息只留一条（补上 bot 文本，不重复入）。"""
        if not user_text.strip() and not images:
            return
        await self._capture_push(event, user_text.strip(), bot_text or "", images=images)

    # ————— 撤回取证的图片留存上限 —————
    _IMG_THUMB_EDGE = 512          # 缩略图最长边：撤回板块列表里"看一眼撤了什么"
    _IMG_EMBED_MAX = 512 * 1024    # 没有 Pillow 时，原图超过这个大小就不嵌（别把流水撑爆）
    _IMG_FULL_MAX = 1024 * 1024    # 「可下载」版本：原图 ≤1MB 就留原图，否则留 1600px 高清版
    _IMG_FULL_EDGE = 1600

    def _image_full_data_url(self, path: str) -> tuple[str, bool]:
        """撤回取证的「可下载」版本 → (data_url, 是否原图)。

        面板上缩略图只够看；下载要的是能用的大图。原图 ≤1MB 就直接留原图（保留原始格式/字节），
        超过就重编成 1600px JPEG(q88)，第二个返回值告诉面板现在下载的是原图还是高清版 —— 如实标注，
        不把高清版谎称原图。
        """
        try:
            raw = Path(path).read_bytes()
        except Exception:                                      # noqa: BLE001
            return "", False
        if not raw:
            return "", False
        if len(raw) <= self._IMG_FULL_MAX:
            try:
                from PIL import Image as _PILImage
                with _PILImage.open(io.BytesIO(raw)) as im:
                    fmt = str(im.format or "JPEG").lower()
                mime = "image/jpeg" if fmt in ("jpg", "jpeg") else f"image/{fmt}"
            except Exception:                                  # noqa: BLE001 没装 Pillow/认不出格式 → 按二进制给
                mime = "application/octet-stream"
            return f"data:{mime};base64," + base64.b64encode(raw).decode("ascii"), True
        try:
            from PIL import Image as _PILImage
            im = _PILImage.open(io.BytesIO(raw))
            if im.mode not in ("RGB", "L"):
                im = im.convert("RGB")
            im.thumbnail((self._IMG_FULL_EDGE, self._IMG_FULL_EDGE))
            buf = io.BytesIO()
            im.save(buf, format="JPEG", quality=88)
            out = buf.getvalue()
            if out:
                return "data:image/jpeg;base64," + base64.b64encode(out).decode("ascii"), False
        except Exception as exc:                               # noqa: BLE001
            logger.debug(f"[StarHeart] 高清版生成失败：{exc}")
        return "", False

    def _image_to_data_url(self, path: str) -> str:
        """把本地图片读成 data URL（优先缩略图）。失败返回空串。

        ⚠️ 必须在**收到消息那一刻**读：AstrBot 把 QQ 图片落成临时文件
        （/AstrBot/data/temp/media_image_*.jpg），事后会被清理，撤回时再想看就永远取不回来。
        真机实证（2026-10-09）：图片消息被撤回后，流水里 `imgs` 是空的、只显示"内容未留存"，
        根因就是这里把本地路径当成"不可直接读取"跳过了 —— 文本能取回、图片取不回。
        """
        try:
            raw = Path(path).read_bytes()
        except Exception:                                      # noqa: BLE001 文件没了/没权限
            return ""
        if not raw:
            return ""
        try:
            import io

            from PIL import Image as _PILImage  # 容器自带 Pillow；没有就走原图分支
            im = _PILImage.open(io.BytesIO(raw))
            if im.mode not in ("RGB", "L"):
                im = im.convert("RGB")
            im.thumbnail((self._IMG_THUMB_EDGE, self._IMG_THUMB_EDGE))
            buf = io.BytesIO()
            im.save(buf, format="JPEG", quality=82)
            out = buf.getvalue()
            if out:
                return "data:image/jpeg;base64," + base64.b64encode(out).decode("ascii")
        except Exception as exc:                               # noqa: BLE001 解码失败 → 退回原图/跳过
            logger.debug(f"[StarHeart] 图片缩略失败：{exc}")
        if len(raw) <= self._IMG_EMBED_MAX:
            return "data:image/jpeg;base64," + base64.b64encode(raw).decode("ascii")
        return ""

    # ---------------- 原生白名单桥（名单/可见性统一走 AstrBot 原生那份） ----------------
    def _native_visibility_read(self) -> dict:
        """读 AstrBot 全局配置 platform_settings 的原生白名单。

        原生语义（AstrBot 自己的提示词也这么写）：**未启用 / 名单为空 = 全部放行（等于不触发）**。
        """
        try:
            cfg = self.context.get_config()
            ps = cfg.get("platform_settings", {}) if hasattr(cfg, "get") else {}
            ids = ps.get("id_whitelist") or []
            return {"enable": bool(ps.get("enable_id_white_list")), "ids": [str(x) for x in ids]}
        except Exception as exc:                                # noqa: BLE001
            logger.warning(f"[StarHeart] 读原生白名单失败：{exc}")
            return {"enable": False, "ids": [], "err": str(exc)[:120]}

    async def _native_visibility_write(self, enable: bool, ids: list) -> dict:
        """写回 AstrBot 全局配置（平台配置 → 白名单）并调原生 save_config 落盘。

        插件只做筛选/优化，**不另存第二份名单**：勾选的进白名单、没勾的不进。
        """
        try:
            cfg = self.context.get_config()
            ps = cfg.get("platform_settings")
            if not isinstance(ps, dict):
                return {"ok": False, "error": "原生 platform_settings 不可写"}
            # 最后一道闸：联系人结构体的键名永远不是会话 id，不许进白名单
            _clean, _dropped = [], []
            for _x in (ids or []):
                _s = str(_x).strip()
                if not _s:
                    continue
                if _s in _CONTACT_SHAPE_KEYS:
                    _dropped.append(_s)
                    continue
                if _s not in _clean:
                    _clean.append(_s)
            if _dropped:
                logger.warning(f"[StarHeart] 写原生白名单时丢弃结构体键名：{', '.join(_dropped)}")
            ps["enable_id_white_list"] = bool(enable)
            ps["id_whitelist"] = _clean
            fn = getattr(cfg, "save_config_async", None) or getattr(cfg, "save_config", None)
            if not callable(fn):
                return {"ok": False, "error": "原生配置对象没有 save_config"}
            r = fn()
            if asyncio.iscoroutine(r):
                await r
            logger.info(f"[StarHeart] 原生白名单已写回：enable={bool(enable)} count={len(ids or [])}")
            return {"ok": True, "enable": bool(enable), "count": len(ids or [])}
        except Exception as exc:                                # noqa: BLE001
            logger.warning(f"[StarHeart] 写原生白名单失败：{exc}")
            return {"ok": False, "error": str(exc)[:160]}

    async def _platform_contacts_rich(self, force: bool = False) -> dict:
        """包一层 `_platform_contacts`：返回前把**只给号码**的私聊补上真实昵称。

        为什么要包在源头：名单页渲染、可见性判定、导出的联系人表都取 `system["contacts"]`，
        在这一层补 = 所有调用方一次到位（之前补在 contacts() 里，名单页用的是 contact_filter()，
        所以那两段一次都没执行到 —— 真机日志可证）。
        昵称真源 = 协议端 get_stranger_info（与 AstrBot 适配器自己用的动作一致）。
        """
        out = await self._platform_contacts(force) or {}
        try:
            miss: list[str] = []
            for f in (out.get("friends") or []):
                if not isinstance(f, dict):
                    continue
                fid = str(f.get("user_id") or f.get("id") or "").strip()
                nm = str(f.get("name") or f.get("nickname") or f.get("remark") or "").strip()
                if fid and (not nm or nm == fid):
                    miss.append(fid)
            if miss:
                live = await self._resolve_user_names(miss[:60]) or {}
                got = 0
                for f in (out.get("friends") or []):
                    if not isinstance(f, dict):
                        continue
                    fid = str(f.get("user_id") or f.get("id") or "").strip()
                    hit = live.get(fid)
                    if hit:
                        f["nickname"] = str(hit)
                        f["name"] = str(hit)
                        got += 1
                logger.info(f"[StarHeart] 名单联系人昵称补全 {got} 条（get_stranger_info，缺名 {len(miss)} 个）")
        except Exception as exc:                                # noqa: BLE001
            logger.warning(f"[StarHeart] 联系人昵称补全失败（不影响名单）：{exc}")
        return out

    async def _resolve_user_names(self, ids: list) -> dict:
        """用协议端接口实时补昵称：get_stranger_info(user_id) → {nickname}。

        平台 get_friend_list 只给号码、或走的是「自己聊过的会话」兜底时，
        这是唯一可靠的真源（与面板里 get_login_info 那条链路同源）。
        """
        out: dict[str, str] = {}
        want = [str(x).strip() for x in (ids or []) if str(x).strip()][:60]
        if not want:
            return out
        try:
            from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_platform_adapter import AiocqhttpAdapter
        except Exception as exc:                                # noqa: BLE001
            logger.debug(f"[StarHeart] aiocqhttp 适配器不可用：{exc}")
            return out
        for inst in list(getattr(self.context.platform_manager, "platform_insts", []) or []):
            try:
                if not isinstance(inst, AiocqhttpAdapter):
                    continue
                client = inst.get_client()
                for uid in want:
                    if uid in out:
                        continue
                    try:
                        info = await asyncio.wait_for(
                            client.call_action("get_stranger_info", user_id=int(uid)), timeout=6)
                    except Exception:                           # noqa: BLE001 单个失败不影响其它
                        continue
                    nm = str((info or {}).get("nickname") or (info or {}).get("remark") or "").strip()
                    if nm:
                        out[uid] = nm
                if out:
                    break
            except Exception as exc:                            # noqa: BLE001
                logger.debug(f"[StarHeart] 取昵称失败：{exc}")
                continue
        if out:
            logger.info(f"[StarHeart] 协议端实时补昵称 {len(out)} 条（get_stranger_info）")
        return out

    def _collect_image_urls_ex(self, event: AstrMessageEvent) -> tuple[list[str], list[str], list[bool], int]:
        """收集消息里的图片 → (展示用缩略图列表, 可下载版本列表, 各自是否原图, 读不出来被跳过的张数)。

        认四种形式：http(s) 外链 · `data:` · `base64://` · **本地文件**（`file://` 或绝对路径）。
        最后一种才是 QQ 图片的真实形态（AstrBot 下载到 data/temp 下的本地文件）——
        原实现只认前三种，于是图片消息的撤回内容永远是空的。
        AstrBot 的 Image 组件字段名各版本略有差异（url / file），两种都认。
        """
        out: list[str] = []
        fulls: list[str] = []
        full_orig: list[bool] = []
        skipped = 0
        try:
            msg = getattr(event, "message_obj", None)
            parts = list(getattr(msg, "message", None) or [])
        except Exception:                                      # noqa: BLE001
            parts = []
        for comp in parts:
            try:
                ctype = str(getattr(comp, "type", "") or type(comp).__name__)
            except Exception:                                  # noqa: BLE001
                ctype = ""
            if "image" not in ctype.lower():
                continue
            url = str(getattr(comp, "url", "") or getattr(comp, "file", "") or "").strip()
            if not url:
                continue
            if url.startswith("http://") or url.startswith("https://") or url.startswith("data:image"):
                out.append(url)
                fulls.append(url)                              # 外链本身就是原图，直接给下载用
                full_orig.append(True)
            elif url.startswith("base64://"):
                d = "data:image/png;base64," + url[len("base64://"):]
                out.append(d)
                fulls.append(d)
                full_orig.append(True)
            else:
                path = _local_path_from_url(url)
                embedded = self._image_to_data_url(path) if path and Path(path).is_file() else ""
                if embedded:
                    out.append(embedded)
                    full, is_orig = self._image_full_data_url(path)
                    fulls.append(full or embedded)
                    full_orig.append(bool(is_orig) if full else False)
                else:
                    skipped += 1
                    logger.info(f"[StarHeart] 撤回取证：这张图没能留存（{path[:80]}）")
        return out, fulls, full_orig, skipped

    def _collect_image_urls(self, event: AstrMessageEvent) -> list[str]:
        """兼容旧调用点（记忆捕获只关心 URL 列表，不带跳过计数）。"""
        return self._collect_image_urls_ex(event)[0]

    async def _capture_push(
        self, event: AstrMessageEvent, user_text: str, bot_text: str, images: list[str] | None = None
    ) -> None:
        """缓冲一条并判断是否触发总结。bot 文本后补时替换最后一条的 bot 字段。"""
        try:
            cfg = await self.service.config()
            if not cfg.get("master_switch", True) or not cfg.get("memory_capture_enabled", True):
                return
            session_id = self._sid(event)
            if not session_id:
                return
            # 原始 UMO（未归一化）：会话人格覆盖按它存；归一化 id 去查 scope 会查空
            raw_umo = str(getattr(event, "unified_msg_origin", "") or "")
            if raw_umo:
                self._capture_umo[session_id] = raw_umo
                if len(self._capture_umo) > 512:
                    for k, _ in list(self._capture_umo.items())[:128]:
                        self._capture_umo.pop(k, None)
            buf = self._capture_buffer(session_id)
            uid = self._sender_id(event)
            name_fn = getattr(event, "get_sender_name", None)
            name = (name_fn() if callable(name_fn) else "") or ""
            # 每轮都带 uid / 昵称 / 时间：群里 A/B/C 的发言不能全记成「最后说话的人」，
            # 也不能一批 5 条共用一个时间戳（ago_label 全显示「刚刚」）。
            # 同一人 + 同一文本才合并 bot 回复：纯图片轮 user 文本都是空串，
            # 不校验 uid 会把群里 A、B 两张图合成一个人说的话。
            if (buf and (buf[-1].get("user") or "") == (user_text or "")
                    and buf[-1].get("uid") == uid
                    and not buf[-1].get("bot") and bot_text):
                buf[-1]["bot"] = bot_text
                buf[-1]["bot_ts"] = time.time()
                if images:
                    buf[-1]["imgs"] = list(images)      # 后补的 bot 文本也把图带上
            else:
                buf.append({"user": user_text, "bot": bot_text, "imgs": list(images or []),
                            "uid": uid, "name": name, "ts": time.time(),
                            # 消息 id：撤回命中记忆要靠它反查（P1-3 侧车表）
                            "mid": self._msg_id_of(event)})
            # 0 是合法值（= 每轮都总结），不能被 `or` 吞回默认值
            need_cfg = cfg.get("memory_capture_turns")
            need = max(1, int(4 if need_cfg is None else need_cfg))
            gap = time.time() - float(self._capture_last.get(session_id, 0.0))
            min_gap_cfg = cfg.get("memory_capture_min_interval")
            min_gap = 120.0 if min_gap_cfg is None else float(min_gap_cfg)
            # 留痕：攒批的每一步都可查（不然「聊了半天没记忆」只能靠猜 —— 实测就踩过这个坑）
            logger.debug(
                f"[StarHeart] 捕获缓冲：{session_id} len={len(buf)}/{need} gap={int(gap)}s"
            )
            if len(buf) < need:
                return
            if gap < min_gap:
                return
            self._capture_last[session_id] = time.time()
            turns = list(buf)
            buf.clear()
            logger.info(f"[StarHeart] 记忆捕获触发：{session_id} 共 {len(turns)} 轮 → 入队总结")
            # v2 落库：影子写只存原文；v2_enabled 时再异步抽取成 memories
            if cfg.get("v2_shadow_write", False) or cfg.get("v2_enabled", False):
                try:
                    self._spawn(
                        self._v2_ingest_episode(turns, session_id, uid, name, cfg),
                        label="v2-episode",
                    )
                except Exception as exc:                    # noqa: BLE001 影子写失败不影响主链路
                    logger.debug(f"[StarHeart] v2 落库派发跳过：{exc}")
            persona_id = await self._inject_persona_id(
                self._persona_umo_for_session(session_id) or session_id, use_cache=True
            )
            await self._enqueue_capture(turns, session_id, persona_id, uid, name)
        except Exception as exc:
            logger.debug(f"[StarHeart] 记忆捕获跳过：{exc}")

    async def _v2_ingest_episode(
        self, turns: list[dict[str, Any]], session_id: str, uid: str, name: str,
        cfg: dict[str, Any] | None = None,
    ) -> None:
        """v2 落库：原文写 episodes；v2_enabled 时紧接着抽成 memories。

        影子写（v2_shadow_write）只存原文，不调模型、不写 memories，读路径完全不动。
        """
        try:
            msgs: list[dict[str, Any]] = []
            mids: list[str] = []
            for t in turns or []:
                if t.get("user"):
                    msgs.append({
                        "role": "user", "content": str(t["user"])[:2000],
                        "ts": float(t.get("ts") or 0), "message_id": str(t.get("mid") or ""),
                        "name": str(t.get("name") or name or ""),
                    })
                    if t.get("mid"):
                        mids.append(str(t["mid"]))
                if t.get("bot"):
                    msgs.append({
                        "role": "assistant", "content": str(t["bot"])[:2000],
                        "ts": float(t.get("bot_ts") or t.get("ts") or 0), "message_id": "",
                    })
                for img in (t.get("imgs") or []):
                    msgs.append({"role": "system", "content": "[图片]", "image": str(img)[:500]})
            if not msgs:
                return
            raw_json = json.dumps(msgs, ensure_ascii=False)
            msg_hash = hashlib.sha1(
                ("|".join(mids) if mids else raw_json).encode("utf-8")
            ).hexdigest()[:24]
            try:
                kind, _ident = self.service.session_kind(session_id)
            except Exception:                               # noqa: BLE001
                kind = "private"
            ts0 = float(msgs[0].get("ts") or time.time())
            ts1 = float(msgs[-1].get("ts") or ts0)
            ep_id = await self.store.upsert_episode({
                "session_id": session_id,
                "persona_id": "",
                "subject_id": str(uid or "") if kind == "private" else "",
                "started_at": ts0,
                "ended_at": ts1,
                "raw_json": raw_json,
                "message_ids": mids,
                "msg_hash": msg_hash,
                "summary": "",
            })
            logger.debug(f"[StarHeart] v2 episode：{session_id} {len(msgs)} 条消息 → {ep_id}")
            if ep_id and (cfg or {}).get("v2_enabled", False):
                from .core import memory_v2 as v2mem

                await v2mem.write_episode(self, int(ep_id), cfg)
        except Exception as exc:                            # noqa: BLE001 落库失败不影响主链路
            logger.debug(f"[StarHeart] v2 落库跳过：{exc}")

    async def _v2_drain_pending(self, limit: int = 10) -> None:
        """把 pending/failed 的 v2 episode 抽完（启动补跑用；/hf v2 extract 同源）。"""
        try:
            from .core import memory_v2 as v2mem

            # 启动早期 provider 还没就绪，等它挂好再抽（否则 _session_provider 返回 None）
            await asyncio.sleep(15)
            rows = await (await self.store.db.execute(
                "SELECT id FROM episodes WHERE extract_state IN ('pending','failed') "
                "ORDER BY id LIMIT ?", (int(limit),))).fetchall()
            if not rows:
                logger.info("[StarHeart] v2 补跑：没有待抽取的 episode")
                return
            cfg = dict(await self.service.config())
            cfg["v2_enabled"] = True
            for r in rows:
                eid = int(r["id"])
                try:
                    s = await v2mem.write_episode(self, eid, cfg)
                    logger.info(f"[StarHeart] v2 补跑 ep{eid} → {s}")
                except Exception as exc:                    # noqa: BLE001 单条失败不影响其它
                    logger.warning(f"[StarHeart] v2 补跑失败 ep{eid}：{exc}")
        except Exception as exc:                            # noqa: BLE001
            logger.warning(f"[StarHeart] v2 补跑跳过：{exc}")

    @staticmethod
    def _capture_outcome(res: dict[str, Any]) -> str:
        """统一捕获结果口径：新结果用 outcome（done/skipped/retry），老结果退回 ok。"""
        out = str((res or {}).get("outcome") or "")
        if out:
            return out
        return "done" if (res or {}).get("ok") else "retry"

    async def _summarize_capture(
        self, turns: list[dict[str, Any]], session_id: str, user_id: str, name: str, persona_id: str
    ) -> dict[str, Any]:
        """跑一次总结并给新记忆补向量，返回 summarize_dialogue 的结果（失败也返回，不抛）。

        图片只有一条路：**原生多模态** —— image_urls 直接交给做总结的模型自己看。
        为什么删掉「先用独立图像模型识别、再把描述喂给文本模型」那条路：
        那个独立模型吃不到 AstrBot 的提示词，属于「吃不到提示词的模型参与对话链路」——
        用户明确禁止（自审模型就是这么坏的）。对话本身用到的图片由 AstrBot 自己的管线处理
        （prepare_request_images 在 on_llm_request 之后执行），插件不介入，也不会因图片改变回复。
        """
        try:
            cfg = await self.service.config()
            urls: list[str] = []
            if cfg.get("vision_enabled", True):
                cap = int(cfg.get("vision_max_images", 4) or 4)
                for t in turns:
                    for u in (t.get("imgs") or []):
                        if u not in urls:
                            urls.append(u)
                urls = urls[:cap]
            res = await self.service.summarize_dialogue(
                turns, session_id=session_id, user_id=user_id, user_name=name, persona_id=persona_id,
                image_urls=urls or None,
            )
            # 带图失败 → 去掉图片立刻重跑一次纯文本。旧实现这里只打一行 warning
            # （注释写「上层会降级重跑纯文本」，但下面没有降级代码）：总结模型不支持图片时，
            # 含图批次失败 5 次（约 8 分钟）后连同该批纯文本对话一起停摆一年（P0-3）。
            # 只在失败时降级，成功路径不会多跑一次模型。
            if (urls and self._capture_outcome(res) == "retry"
                    and self.service.image_related_failure(res)):
                logger.warning(
                    f"[StarHeart] 带图总结失败，降级纯文本重跑一次：{str(res.get('error') or res)[:120]}"
                )
                res_text = await self.service.summarize_dialogue(
                    turns, session_id=session_id, user_id=user_id, user_name=name, persona_id=persona_id,
                    image_urls=None,
                )
                if self._capture_outcome(res_text) in ("done", "skipped"):
                    res_text["degraded"] = "image_to_text"
                    await self.service.store.audit(
                        "memory.capture",
                        f"image_degraded session={session_id} saved={res_text.get('saved')}",
                    )
                    logger.info("[StarHeart] 图片降级生效：本批按纯文本总结（图片未进记忆）")
                else:
                    logger.warning(
                        f"[StarHeart] 纯文本降级仍失败：{str(res_text.get('error') or res_text)[:120]}"
                    )
                res = res_text
            if self._capture_outcome(res) not in ("done", "skipped"):
                return res
            if not res.get("saved"):
                return res
            logger.info(
                f"[StarHeart] 记忆总结：写入 {res.get('saved')} 条（扫 {res.get('scanned')} 轮）"
            )
            cfg_emb = await self.service.config()
            model_name, emb_dim, emb_provider = self._embed_target(cfg_emb)
            if not model_name or emb_provider is None:
                logger.warning("[StarHeart] 没有可用的嵌入提供商（槽位留空且 AstrBot 里也没配）→ 跳过补向量")
                return res          # 必须回结果：调用方按 res["ok"] 决定是否删除 pending
            if emb_dim <= 0:
                logger.warning("[StarHeart] 嵌入模型没有返回维度 → 跳过本轮补向量")
                return res
            target_gen = self.store.gen_key(model_name, emb_dim)
            # 按「当前模型的目标代」补，而不是旧列 NULL：换模型时也能继续补新代
            for row in await self.store.memories_missing_gen(target_gen, limit=5, persona_id=persona_id):
                text = f"{row['summary']} {row['content'] or ''}".strip()[:512]
                if not text:
                    continue
                vec = await self._embed(text)
                if vec:
                    actual_gen = self.store.gen_key(model_name, len(vec))
                    await self.store.set_embedding_gen(row["id"], vec, model_name, gen_key=actual_gen)
            return res
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(f"[StarHeart] 记忆总结失败：{exc}")
            return {"ok": False, "error": str(exc)[:200]}

    async def _enqueue_capture(self, turns: list[dict[str, Any]], session_id: str,
                               persona_id: str, user_id: str, name: str) -> None:
        """先落 pending 再总结：模型超时/空响应不会让整批对话蒸发（P0-6）。"""
        if not turns:
            return
        try:
            row = await self.store.capture_enqueue_row(
                turns, session_id=session_id, persona_id=persona_id,
                user_id=user_id, user_name=name,
            )
        except Exception as exc:                            # noqa: BLE001 落库失败退回直接跑
            logger.error(
                f"[StarHeart] 记忆捕获入队失败（改为直接总结：本批不进队列、失败没有退避）：{exc}")
            await self._capture_worker({"id": 0, "turns": list(turns), "session_id": session_id,
                                        "persona_id": persona_id, "user_id": user_id, "user_name": name})
            return
        if not row:
            return
        # 入队即占位（capture_enqueue_row 在 INSERT 里写 locked_by + 租约）：不再有
        # 「INSERT 完成 → capture_claim 之前」的空窗，retry tick 抢不走这一批。
        self._spawn(
            self._capture_worker(row),
            label="memory-capture",
        )

    async def _capture_worker(self, row: dict[str, Any]) -> None:
        """跑一条待处理批：成功删行；失败退避重试（30s→60→120→240→480）。"""
        pid = int(row.get("id") or 0)
        try:
            res = await self._summarize_capture(
                list(row.get("turns") or []), str(row.get("session_id") or ""),
                str(row.get("user_id") or ""), str(row.get("user_name") or ""),
                str(row.get("persona_id") or ""),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:                            # noqa: BLE001
            res = {"ok": False, "error": str(exc)[:200]}
        outcome = self._capture_outcome(res)
        token = str(row.get("locked_by") or "")
        if outcome == "hold":
            # 捕获开关关掉 / 主动暂停：行留着，到期时间推后，开关恢复后自动续跑。
            if pid:
                await self.store.capture_hold(pid, str(res.get("reason") or "hold"), token=token)
                logger.info("[StarHeart] 记忆捕获已暂停：本批留在队列（未删除），开关恢复后自动续跑")
            return
        if outcome in ("done", "skipped"):
            if pid:
                await self.store.capture_done(pid, token)
            return
        if not pid:
            return
        tries = await self.store.capture_fail(pid, str(res.get("error") or res), token=token)
        if not tries:
            # 锁已被租约回收（同一批可能已有另一个 worker 在跑）：不覆盖别人的状态，也不再重复记账。
            logger.debug(f"[StarHeart] 捕获批 {pid} 锁已失效，跳过本次失败记账")
            return
        logger.warning(
            f"[StarHeart] 记忆总结失败，已留档待重试（第 {tries} 次）：{str(res.get('error') or res)[:160]}"
        )

    async def _capture_retry_tick(self) -> None:
        """到期重试：每次最多 2 批，避免模型故障时反复打爆（数据一直留着）。"""
        # 停摆回暖：退避链只有 ~7.5 分钟，模型/网关故障一久队列会整批 parked。
        # 每 6h 给 5 批一次机会，修好之后能自己恢复，不必人工翻库。
        now_t = time.time()
        if now_t - float(getattr(self, "_capture_parked_last", 0.0) or 0.0) >= 6 * 3600:
            self._capture_parked_last = now_t
            try:
                n = await self.store.capture_parked_for_retry(limit=5)
                if n:
                    logger.warning(f"[StarHeart] 停摆队列回暖：{n} 批重新进入重试")
            except Exception as exc:                        # noqa: BLE001
                logger.debug(f"[StarHeart] 停摆队列回暖跳过：{exc}")
        try:
            rows = await self.store.capture_take(limit=2)
        except Exception as exc:                            # noqa: BLE001
            logger.debug(f"[StarHeart] 记忆捕获重试跳过：{exc}")
            return
        for row in rows:
            self._spawn(self._capture_worker(row), label="memory-capture-retry")

    async def _capture_idle_flush(self) -> None:
        """会话静默 10 分钟还没攒满 → 直接总结。

        旧行为必须攒满 memory_capture_turns（默认 6，且 2 小时整槽丢弃）：
        聊 3–5 轮然后离开 = 一个字都不进记忆（P0-7）。
        """
        try:
            cfg = await self.service.config()
            if not cfg.get("master_switch", True) or not cfg.get("memory_capture_enabled", True):
                return
            now_t = time.time()
            idle_sec = 600.0
            for sid, buf in list(self._capture_bufs.items()):
                if not buf:
                    continue
                last = float(buf[-1].get("ts") or self._capture_last.get(sid, 0.0) or 0.0)
                if now_t - last < idle_sec:
                    continue
                turns = list(buf)
                buf.clear()
                self._capture_last[sid] = now_t
                uid = str(turns[-1].get("uid") or "")
                name = str(turns[-1].get("name") or "")
                logger.info(f"[StarHeart] 记忆捕获（静默补总结）：{sid} 共 {len(turns)} 轮")
                await self._enqueue_capture(
                    turns, sid,
                    await self._inject_persona_id(self._persona_umo_for_session(sid) or sid, use_cache=True),
                    uid, name,
                )
        except Exception as exc:                            # noqa: BLE001
            logger.debug(f"[StarHeart] 静默补总结跳过：{exc}")

    async def _recall_picked(self, event: AstrMessageEvent, top_k: int, persona_id: str) -> list[dict[str, Any]]:
        """两臂并行 + RRF 融合：语义臂命中不再短路关键词臂（P0-3）。

        旧实现要 `if not picked:` 才走关键词 —— 一条余弦 0.36 的沾边命中就能顶掉本轮
        全部关键词结果，而 embedding IS NULL 的记忆彻底出局。
        """
        query = event.message_str or ""
        sid = self._sid(event)
        # 主体加权只对私聊有意义：群聊里 sender 是「谁在说话」而不是「记忆属于谁」
        subject_id = self._sender_id(event) if "group" not in str(sid).lower() else ""
        cand = max(12, int(top_k) * 4)
        semantic: list[dict[str, Any]] = []
        qvec = None
        try:
            qvec = await self._embed(query)
        except Exception as exc:                        # noqa: BLE001 嵌入失败只影响语义臂
            logger.debug(f"[StarHeart] 语义臂嵌入跳过：{exc}")
        if qvec:
            try:
                for mid, score in await self._vector_recall(qvec, cand, persona_id=persona_id):
                    mem = await self.store.get_memory(mid)
                    # 归档/变淡的记忆也能被召回命中，命中即由 touch_memory 拉回
                    if mem and str(mem.get("status")) not in ("deleted", "forgotten"):
                        mem["score"] = float(score)      # 兼容旧字段（面板/日志会读）
                        mem["_vec_score"] = float(score)
                        semantic.append(mem)
            except Exception as exc:                    # noqa: BLE001
                logger.debug(f"[StarHeart] 语义召回跳过：{exc}")
        keyword: list[dict[str, Any]] = []
        fallback: list[dict[str, Any]] = []
        try:
            kw = await engine.retrieve_memories(
                self.store, query, top_k=cand, session_id=sid,
                persona_id=persona_id, subject_id=subject_id,
            )
            for m in kw:
                (fallback if m.get("_hit_kind") == "fallback" else keyword).append(m)
        except Exception as exc:                        # noqa: BLE001
            logger.debug(f"[StarHeart] 关键词召回跳过：{exc}")

        def _boost(mem: dict[str, Any]) -> float:
            """同会话 / 同主体加权，跨主体降权（不做硬过滤，避免误伤同一个人换会话）。"""
            boost = 1.0
            subj = str(mem.get("subject_id") or "")
            if subject_id and subj:
                boost *= 1.15 if subj == str(subject_id) else 0.55
            if sid and str(mem.get("session_id") or "") == sid:
                boost *= 1.10
            return boost

        fused: dict[str, dict[str, Any]] = {}
        for arm, items, weight in (("semantic", semantic, 1.0), ("keyword", keyword, 0.9)):
            for rank, mem in enumerate(items, 1):
                slot = fused.setdefault(str(mem.get("id")), {"mem": mem, "score": 0.0, "arms": set()})
                slot["score"] += weight / (60.0 + rank)
                slot["arms"].add(arm)
        def _poi_factor(mem: dict[str, Any]) -> float:
            """重要度只做 0.8~1.0 的轻推。

            真正决定排序的必须是「这句话和当前话题的相关性」，不能是「这条被注入过几次」——
            旧实现里 strength 占 30%，一句被反复注入的测试话会压过真正相关的偏好。
            """
            try:
                poi = float(mem.get("poignancy") if mem.get("poignancy") is not None else 5)
            except (TypeError, ValueError):
                poi = 5.0
            return 0.8 + 0.2 * max(0.0, min(1.0, poi / 10.0))

        ordered = sorted(
            fused.values(),
            key=lambda sl: sl["score"] * _boost(sl["mem"]) * _poi_factor(sl["mem"]),
            reverse=True,
        )
        out: list[dict[str, Any]] = []
        seen_texts: list[str] = []
        for slot in ordered:
            if len(out) >= cand:
                break
            mem = slot["mem"]
            summary = str(mem.get("summary") or "")
            # 同主题去重：一轮注入里「小猫娘」这类同义句最多留一条。
            # 真机现场：一次 8 条里 6 条都在说猫，模型自然只会抓着猫不放。
            if summary and any(engine.text_similarity(summary, prev) >= 0.42 for prev in seen_texts):
                continue
            mem["_hit_kind"] = "semantic" if "semantic" in slot["arms"] else "keyword"
            mem["_recall_score"] = round(float(slot["score"] * _boost(mem) * _poi_factor(mem)), 5)
            out.append(mem)
            seen_texts.append(summary)
        if out:
            return out
        # 两臂都空（纯标点 / 库里一条都没匹配）→ 最近兜底，标 fallback：不算被提及
        return fallback[: max(1, int(top_k))]

    async def _vector_recall(self, query_vec: list[float], top_k: int, persona_id: str = "") -> list[tuple[str, float]]:
        """语义臂：按分数取满 top_k，**不再用 0.35 硬阈值**。

        旧行为是双重伤害：余弦 0.36 的沾边命中会顶掉整条关键词臂（`if not picked` 短路），
        而 0.34 的真命中连候选都进不来。阈值交给融合与重排，召回本身不做生死判断。
        """
        pairs: list[tuple[str, bytes]] = []
        try:
            gen = await self.store.active_gen()
        except Exception:                                   # noqa: BLE001
            gen = ""
        if gen:
            # 多代向量库：只读当前代，天然不会混维度；归档记忆也带上（被提及要能拉回）
            pairs = await self.store.all_embeddings_gen(
                gen, persona_id=persona_id, include_stale=True, include_archived=True
            )
        if not pairs:
            # 老库/还没建代时的兜底：读旧列（只会在没有当前代时走到）
            pairs = await self.store.all_active_embeddings(persona_id=persona_id)
        if not pairs:
            return []
        scored = [(mid, self._cosine(query_vec, blob)) for mid, blob in pairs]
        scored.sort(key=lambda x: x[1], reverse=True)
        return [(mid, float(s)) for mid, s in scored[: max(1, int(top_k))]]

    def _agent_system_prompt(self, session_id: str, max_age: float = 7200.0) -> dict[str, Any]:
        """取该会话最近一次「AstrBot 拼好的系统提示词 + 上下文尾」。

        超过 max_age（默认 2 小时）就当没有 —— 宁可退回只取人格，也不用一份过期提示词。
        """
        item = self._agent_prompt_cache.get(str(session_id or ""))
        if not item:
            return {}
        if max_age > 0 and time.time() - float(item.get("ts") or 0) > max_age:
            return {}
        return {"system": str(item.get("system") or ""), "contexts": list(item.get("contexts") or [])}

    async def _rerank_provider(self):
        """读「结果重排」槽位对应的 provider（没配 / 不叫这名字 → None）。"""
        try:
            cfg = await self.service.config()
        except Exception:
            cfg = {}
        pid = str(cfg.get("model_rerank") or "")
        provider = self.context.get_provider_by_id(pid) if pid else self._first_tool_provider("rerank")
        if provider is None or not hasattr(provider, "rerank"):
            return None
        return provider

    async def _rerank_texts(self, query: str, docs: list[str]) -> list[int]:
        """给面板召回测试用的重排回调：返回按相关性排序的候选下标（空 = 不重排）。"""
        provider = await self._rerank_provider()
        if provider is None or len(docs) < 2 or not query.strip():
            return []
        res = await provider.rerank(query[:200], docs)
        return [int(r.index) for r in (res or []) if 0 <= int(r.index) < len(docs)]

    async def _rerank_memories(self, query: str, items: list[dict[str, Any]], top_k: int) -> list[dict[str, Any]]:
        """注入路径的重排：失败/未配一律原样返回，绝不影响注入与回复。"""
        if len(items) < 2 or not query.strip():
            return items
        try:
            idxs = await self._rerank_texts(
                query, [f"{it.get('summary') or ''} {it.get('content') or ''}".strip()[:300] for it in items]
            )
        except Exception as exc:                            # noqa: BLE001 重排不许影响注入
            logger.debug(f"[StarHeart] 结果重排跳过：{exc}")
            return items
        if not idxs:
            return items
        seen: set[int] = set()
        ordered: list[dict[str, Any]] = []
        for i in idxs:
            if i not in seen:
                seen.add(i)
                ordered.append(items[i])
        ordered += [it for i, it in enumerate(items) if i not in seen]
        return ordered[: max(1, top_k)]

    async def _backfill_embeddings(self) -> None:
        """后台补向量：**按当前 embedding 模型的目标代回填**，建满后自动切换，旧代不删。

        旧实现只看旧列 `embedding IS NULL`：换模型后旧列全有值 → 新代永远补不到，
        而且 `set_embedding` 会无条件写旧列，导致旧列混维度、召回静默归零。
        """
        lock = getattr(self, "_embed_lock", None)
        if lock is None:
            lock = self._embed_lock = asyncio.Lock()
        if lock.locked():                                  # 上一轮还没跑完，不叠加
            return
        async with lock:
            await self._backfill_embeddings_locked()

    async def _backfill_embeddings_locked(self) -> None:
        cfg = await self.service.config()
        model, dim, provider = self._embed_target(cfg)
        if not model or provider is None:
            logger.warning("[StarHeart] 没有可用的嵌入提供商 → 跳过后台补向量")
            return
        if dim <= 0:
            try:
                probe = await provider.get_embedding("dimension probe")
                dim = len(probe or [])
            except Exception as exc:                       # noqa: BLE001
                logger.warning(f"[StarHeart] 嵌入维度探测失败，跳过本轮补向量：{exc}")
                return
        if dim <= 0:
            logger.warning("[StarHeart] 嵌入模型没有返回维度，跳过本轮补向量")
            return

        active = await self.store.active_gen()
        target = self.store.gen_key(model, dim)

        # 目标代已经建满 → 直接切过去（旧代保留）；没建满就继续补
        if target != active:
            missing = await self.store.gen_missing_count(target)
            if missing == 0:
                await self.store.set_active_gen(target)
                try:
                    await self.store.audit("vector.autoswitch", target, f"model={model} dim={dim}")
                except Exception:                          # noqa: BLE001
                    pass
                logger.info(f"[StarHeart] 向量库自动切换 → {target}（旧代 {active or '无'} 保留）")
                return

        rows = await self.store.memories_missing_gen(target, limit=40)
        if not rows:
            return
        done = 0
        for row in rows:
            text = f"{row['summary']} {row['content'] or ''}".strip()[:512]
            if not text:
                continue
            vec = await self._embed(text)
            if not vec:
                continue
            actual = self.store.gen_key(model, len(vec))
            if actual != target:                           # provider 实际维度与 get_dim 不一致：以真实为准
                logger.warning(f"[StarHeart] 嵌入维度与声明不一致（声明 {dim}，实际 {len(vec)}），改用 {actual}")
                target = actual
            # 写目标代；set_embedding_gen 只在目标代==当前代时同步旧列
            await self.store.set_embedding_gen(row["id"], vec, model, gen_key=target)
            done += 1
            await asyncio.sleep(0.3)                       # 限速：别把 embedding 上游打满

        if target != active:
            missing = await self.store.gen_missing_count(target)
            if missing == 0:
                await self.store.set_active_gen(target)
                try:
                    await self.store.audit("vector.autoswitch", target, f"model={model} dim={dim}")
                except Exception:                          # noqa: BLE001
                    pass
                logger.info(f"[StarHeart] 向量库建满并切换 → {target}（旧代 {active or '无'} 保留）")
            else:
                logger.info(f"[StarHeart] 向量库回填 {target}：本批 +{done}，还缺 {missing} 条")
        else:
            logger.debug(f"[StarHeart] 向量库回填 {target}：本批 +{done}")

    def _timezone(self) -> str:
        """AstrBot 配置里的时区（面板的静默时段按这个算）。"""
        try:
            cfg = self.context.get_config()
            return str(cfg.get("timezone") or "") if hasattr(cfg, "get") else ""
        except Exception:
            return ""

    async def _persona_text(self) -> str:
        """主动消息要用的人格文本：一律用 AstrBot 当前启用的人格（只用原生人格）。"""
        try:
            cfg = await self.service.config()
        except Exception:
            cfg = {}
        items = await self._persona_list()
        # AstrBot 原生当前人格优先，读不到才退回插件记录的 id（例如插件先于平台就绪）
        want = self._active_persona_id_sync() or str(cfg.get("persona_active_id") or "")
        for p in items:
            if p.get("persona_id") == want:
                return str(p.get("system_prompt") or "")
        return str((items[0] or {}).get("system_prompt") or "") if items else ""

    # ---------------- 平台边界与昵称 ----------------
    #
    # 平台边界由 _is_trackable_session 统一判定：可记录 = OneBot（QQ）实例 + AstrBot 自带
    # WebChat（ChatUI，用户名就是「人」）。其它平台（Telegram/Discord…）默认不记录；要加就
    # 写进 track_platforms。排除名单**全部来自配置** person_exclude_platforms，不再写死。
    # （历史：曾把 webchat/webui 写死排除，因为不拦会让「同一个我」在画像里变成两个点。）

    def _sid(self, event: AstrMessageEvent) -> str:
        """从事件取会话 id，并做统一归一（WebChat 的 `webchat!user!uuid` → `webchat!user`）。

        为什么在唯一入口归一：会话 id 是「这个人是谁」的载体（图谱人节点、画像主体、成员边
        全靠它），归一处、全链路一致；不然会出现「消息表里是这个 uuid、记忆表里是那个 uuid」。
        """
        return session_norm(str(getattr(event, "unified_msg_origin", "") or ""))

    def _session_head(self, session_id: str) -> str:
        return str(session_id or "").split(":", 1)[0].strip().lower()

    def _trackable_platform_ids(self, cfg: dict[str, Any] | None = None) -> set[str]:
        """允许记录的平台实例 id：自动识别 OneBot 适配器的实例 + 配置里的手工追加。"""
        cached = getattr(self, "_track_ids_cache", None)
        if cached and time.time() - cached[0] < 60:
            return cached[1]
        ids: set[str] = set()
        for inst in list(getattr(self.context.platform_manager, "platform_insts", []) or []):
            try:
                cls = type(inst)
                mod, cname = str(cls.__module__).lower(), cls.__name__
                # 两类适配器自动认得：OneBot（QQ）、AstrBot 自带 WebChat（ChatUI）
                if ("aiocqhttp" not in mod and cname != "AiocqhttpAdapter"
                        and "webchat" not in mod and cname != "WebChatAdapter"):
                    continue
                pid = str(getattr(inst.meta(), "id", "") or "").strip()
                if pid:
                    ids.add(pid)
            except Exception:
                continue
        try:
            for x in ((cfg or {}).get("track_platforms") or []):
                if str(x).strip():
                    ids.add(str(x).strip())
        except Exception:
            pass
        self._track_ids_cache = (time.time(), ids)
        return ids

    def _is_trackable_session(self, session_id: str, cfg: dict[str, Any] | None = None) -> bool:
        """这个会话该不该记录：只认 OneBot（QQ）与 AstrBot WebChat（ChatUI）实例。

        排除名单**只来自配置** person_exclude_platforms（默认排除 webui；把 webchat 加进去
        就等于让 ChatUI 会话对插件不可见），代码里不再写死任何平台。
        fail-closed：**认不出任何已知实例时一律不记**（红队实测：旧实现 ids 为空即放行，
        别的平台没起来的那 60 秒里 TG/Discord 的会话会被记进来 —— 跨平台串号会复现）。
        但「以前见过、这次没认出来」的实例要放行（多实例里某个掉线时不该丢它的数据）。
        """
        head = self._session_head(session_id)
        if not head:
            return False
        ignore: set[str] = set()
        try:
            ignore.update(str(x).strip().lower() for x in ((cfg or {}).get("person_exclude_platforms") or []) if str(x).strip())
        except Exception:
            pass
        if head in ignore:
            return False
        ids = {x.lower() for x in self._trackable_platform_ids(cfg)}
        if ids:
            for pid in ids:
                try:
                    self._remember_platform_soon(pid)
                except Exception:
                    pass
            return head in ids
        known = getattr(self, "_known_platforms", None)
        if known and head in known:
            return True           # 掉线宽限：以前认过这个 OneBot 实例
        return False              # 一个 OneBot 都没认出来 → 一律不记（宁缺勿滥）

    def _remember_platform_soon(self, pid: str) -> None:
        """把见过的 OneBot 实例 id 落库（每进程一次，异步、不阻塞消息链）。"""
        if pid in getattr(self, "_platform_remembered", set()):
            return
        self._platform_remembered = getattr(self, "_platform_remembered", set()) | {pid}
        self._known_platforms = getattr(self, "_known_platforms", set()) | {str(pid).lower()}
        if self.store is None:
            return
        async def _bg() -> None:
            try:
                await self.store.remember_platform(pid)
            except Exception:
                pass
        self._spawn(_bg(), label="remember-platform")

    def _sender_name(self, event: AstrMessageEvent) -> str:
        """发送者昵称（AstrBot 原生接口；取不到再退回 message_obj.sender）。"""
        fn = getattr(event, "get_sender_name", None)
        nm = ""
        try:
            nm = str(fn() or "").strip() if callable(fn) else ""
        except Exception:
            nm = ""
        if not nm:
            try:
                nm = str(getattr(getattr(event, "message_obj", None), "sender", None).nickname or "").strip()
            except Exception:
                nm = ""
        return nm

    async def _remember_person_name(self, event: AstrMessageEvent) -> None:
        """把「这个号叫什么」落库：界面只认昵称，QQ 号只出现在详情页。"""
        try:
            uid = str(self._sender_id(event) or "").strip()
            nm = self._sender_name(event)
            if not uid or not nm:
                return
            sid = self._sid(event)
            parts = sid.split(":")
            gid = parts[-1] if len(parts) >= 3 and "group" in parts[-2].lower() else ""
            await self.store.upsert_person_name(
                uid, nm, source="msg", platform=self._session_head(sid), group_id=gid)
        except Exception as exc:                            # noqa: BLE001
            logger.debug(f"[StarHeart] 记昵称跳过：{exc}")

    # ---------------- 平台联系人：群聊 / 好友 / 自身 / 管理员 ----------------
    async def _platform_contacts(self, force: bool = False) -> dict[str, Any]:
        """读机器人真实的群聊与好友（OneBot 接口）+ AstrBot 配置里的管理员名单。"""
        cached = getattr(self, "_contacts_cache", None)
        if cached and not force and time.time() - cached[0] < 120:
            return cached[1]
        out: dict[str, Any] = {"admins": [], "groups": [], "friends": [], "bots": [], "platforms": [], "errors": []}
        try:
            cfg = self.context.get_config()
            out["admins"] = [str(x) for x in ((cfg.get("admins_id") or []) if hasattr(cfg, "get") else [])]
        except Exception as exc:
            out["errors"].append(f"admins: {exc}")
        try:
            from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_platform_adapter import AiocqhttpAdapter
        except Exception as exc:
            out["errors"].append(f"adapter import: {exc}")
            return out
        for inst in list(getattr(self.context.platform_manager, "platform_insts", []) or []):
            try:
                meta = inst.meta()
                platform_id = str(getattr(meta, "id", "") or "")
            except Exception:
                platform_id = ""
            out["platforms"].append({"id": platform_id, "type": type(inst).__name__})
            if not isinstance(inst, AiocqhttpAdapter):
                continue
            try:
                client = inst.get_client()
            except Exception as exc:
                out["errors"].append(f"client[{platform_id}]: {exc}")
                continue
            if client is None:
                continue
            try:
                login = await client.call_action("get_login_info") or {}
                out["bots"].append({
                    "platform": platform_id,
                    "user_id": str(login.get("user_id") or ""),
                    "nickname": str(login.get("nickname") or ""),
                })
            except Exception as exc:
                out["errors"].append(f"login[{platform_id}]: {exc}")
            try:
                for g in await client.call_action("get_group_list") or []:
                    out["groups"].append({
                        "platform": platform_id,
                        "group_id": str(g.get("group_id") or ""),
                        "name": str(g.get("group_name") or ""),
                        "members": g.get("member_count"),
                        "max_members": g.get("max_member_count"),
                    })
            except Exception as exc:
                out["errors"].append(f"groups[{platform_id}]: {exc}")
            try:
                for f in await client.call_action("get_friend_list") or []:
                    out["friends"].append({
                        "platform": platform_id,
                        "user_id": str(f.get("user_id") or ""),
                        "nickname": str(f.get("nickname") or ""),
                        "remark": str(f.get("remark") or ""),
                    })
            except Exception as exc:
                out["errors"].append(f"friends[{platform_id}]: {exc}")
            # 群成员名片：只在群里出现（不在好友列表）的群友，名字只能从这里拿到。
            # 限量抓（最多 8 群 × 300 人），避免把 OneBot 接口打爆。
            try:
                glist = await client.call_action("get_group_list") or []
            except Exception as exc:
                glist = []
                out["errors"].append(f"groups2[{platform_id}]: {exc}")
            # 节流 + 超时 + 限量：红队实测 8 群×300 人串行且无超时，会触发 QQ/NapCat 风控，
        # 单次 call_action 挂起就能把整个面板请求拖死。这里一次只抓 5 群 × 200 人、群间 sleep。
            for gi, g in enumerate(list(glist)[:5]):
                gid = str((g or {}).get("group_id") or "")
                if not gid:
                    continue
                if gi:
                    await asyncio.sleep(0.3)                      # 群间节流，避免风控
                try:
                    members = await asyncio.wait_for(
                        client.call_action("get_group_member_list", group_id=int(gid)), timeout=8)
                except Exception as exc:
                    out["errors"].append(f"members[{gid}]: {exc}")
                    continue
                for m in list(members)[:200]:
                    uid = str((m or {}).get("user_id") or "")
                    card = str((m or {}).get("card") or "").strip()
                    nick = str((m or {}).get("nickname") or "").strip()
                    if not uid:
                        continue
                    if card:
                        out.setdefault("member_names", []).append({
                            "ident": uid, "name": card, "source": "group_card",
                            "platform": platform_id, "group_id": gid})
                    if nick:
                        out.setdefault("member_names", []).append({
                            "ident": uid, "name": nick, "source": "nickname",
                            "platform": platform_id, "group_id": gid})
        # 昵称/群名落库：OneBot 能给的全拿（好友备注名 > 昵称；群里用群名片），
        # 这样面板显示的是名字，QQ 号只在详情页出现 —— 以前一个都不存，只能显示裸号。
        try:
            bulk: list[dict[str, Any]] = []
            for f in out["friends"]:
                # 昵称与备注分开存：显示按「群名片 > 昵称 > 备注」挑，避免拿备注名当本名
                uid = str(f.get("user_id") or "")
                if not uid:
                    continue
                for src, key in (("nickname", "nickname"), ("remark", "remark")):
                    nm = str(f.get(key) or "").strip()
                    if nm:
                        bulk.append({"ident": uid, "name": nm, "source": src,
                                     "platform": str(f.get("platform") or "")})
            for g in out["groups"]:
                if g.get("group_id") and g.get("name"):
                    bulk.append({"kind": "group", "ident": str(g["group_id"]), "name": str(g["name"]),
                                 "source": "nickname", "platform": str(g.get("platform") or "")})
            for mm in (out.get("member_names") or []):
                if mm.get("ident") and mm.get("name"):
                    bulk.append({"ident": str(mm["ident"]), "name": str(mm["name"]),
                                 "source": str(mm.get("source") or "group_card"),
                                 "platform": str(mm.get("platform") or ""),
                                 "group_id": str(mm.get("group_id") or "")})
            changed = await self.store.upsert_person_names_bulk(bulk)   # 一次 commit，别逐条占写锁
            # 降噪：这批是周期性刷新，没变化就不记日志（以前每 2 分钟一条「昵称批量落库 49 条」把日志区刷满）
            if changed:
                logger.debug(f"[StarHeart] 昵称批量落库 {len(bulk)} 条（变更 {changed}）")
        except Exception as exc:                            # noqa: BLE001
            logger.debug(f"[StarHeart] 联系人群名落库跳过：{exc}")
        admins = set(out["admins"])
        for f in out["friends"]:
            f["is_admin"] = f["user_id"] in admins
        out["friends"].sort(key=lambda x: (0 if x.get("is_admin") else 1, x.get("nickname") or x.get("user_id") or ""))
        out["groups"].sort(key=lambda x: x.get("name") or x.get("group_id") or "")
        out["admin_in_friends"] = sum(1 for f in out["friends"] if f.get("is_admin"))
        out["admin_not_in_friends"] = [a for a in out["admins"] if not any(f["user_id"] == a for f in out["friends"])]
        self._contacts_cache = (time.time(), out)
        return out

    # ---------------- 消息留档（撤回内容取证） ----------------
    def _msg_id_of(self, event: AstrMessageEvent) -> str:
        """取这条消息的平台消息 id（AstrBot 的 message_obj 上有；取不到就返回空）。"""
        for src in (getattr(event, "message_obj", None), event):
            try:
                mid = str(getattr(src, "message_id", "") or "")
                if mid:
                    return mid
            except Exception:                                  # noqa: BLE001
                continue
        return ""

    async def _keep_live_message(self, event: AstrMessageEvent, text: str) -> None:
        """收到消息时就留档：环形缓冲（最近 200 条/会话）＋ hf_messages（ext_id 去重）。

        为什么必须自己留：OneBot 撤回通知只给 message_id；AstrBot 原生也不会把「那条被撤回的消息正文」
        交给插件。要让面板显示完整内容（含图片），只能在收到时存下来。
        """
        mid = self._msg_id_of(event)
        if not mid:
            return
        if not hasattr(self, "_recent_msgs"):
            self._recent_msgs = {}
        try:
            imgs, imgs_full, imgs_full_orig, imgs_skipped = self._collect_image_urls_ex(event)
        except Exception:                                      # noqa: BLE001
            imgs, imgs_full, imgs_full_orig, imgs_skipped = [], [], [], 0
        session_id = self._sid(event)
        sender = self._sender_id(event)
        try:
            sender_name = str(event.get_sender_name() or "")
        except Exception:                                      # noqa: BLE001
            sender_name = ""
        # imgs 存的是**可直接展示的 data-url / 外链**（图片在收到这一刻就缩略嵌好，
        # 因为 AstrBot 的媒体是临时文件，撤回时再去读就没了）；imgs_skipped 记录
        # 「有几张图确实存在但没能留存」，面板据此如实显示，而不是笼统地说"内容未留存"。
        # imgs_full 是面板「下载」用的版本（原图 ≤1MB 留原图，否则 1600px 高清版），
        # imgs_full_orig 标明每张到底是不是原图 —— 面板按它决定按钮文案，不谎称。
        rec = {"mid": mid, "ts": time.time(), "session_id": session_id, "sender": sender,
               "sender_name": sender_name, "text": text, "imgs": imgs,
               "imgs_full": imgs_full, "imgs_full_orig": imgs_full_orig, "imgs_skipped": imgs_skipped}
        try:
            self._recent_msgs.setdefault(session_id, deque(maxlen=200)).append(rec)
        except Exception:                                      # noqa: BLE001
            pass
        try:
            await self.store.add_messages([{
                "session_id": session_id, "role": "user", "content": text,
                "sender_id": sender, "sender_name": sender_name,
                "platform": str(self.service.session_kind(session_id)[0] or ""),
                "ts": rec["ts"], "is_bot": 0, "ext_id": f"qq:{mid}",
            }])
        except Exception as exc:                               # noqa: BLE001
            logger.debug(f"[StarHeart] 消息落库跳过：{exc}")

    async def _recalled_by_mid(self, session_id: str, mid: str) -> dict[str, Any]:
        """按 message_id 找回被撤回的那条消息（先本会话，再全量兜底）。找不到就返回空 dict。"""
        if not mid:
            return {}
        try:
            rings = getattr(self, "_recent_msgs", {}) or {}
            for sid in ([session_id] + [k for k in rings.keys() if k != session_id]):
                for rec in reversed(list(rings.get(sid) or [])):
                    if rec.get("mid") == mid:
                        return dict(rec)
            # 内存环里没有（插件重启 / 该会话超过 200 条被挤出）→ 查 hf_messages 兜底。
            # 审查实证：原先这里从不查库，所以「重启后对方才撤回」必定显示无内容，库里的正文白存。
            found = await self.store.find_message_by_ext_id(f"qq:{mid}")
            if found:
                logger.info(f"[StarHeart] 撤回内容由库兜底取回（mid={mid}）")
                return found
        except Exception:                                      # noqa: BLE001
            return {}
        return {}

    # ---------------- 平台通知（撤回等） ----------------
    async def _handle_notice(self, event: AstrMessageEvent, raw: dict[str, Any]) -> None:
        """OneBot 通知事件：撤回 → 降权命中记忆并落流水。"""
        notice_type = str(raw.get("notice_type") or "")
        if "recall" not in notice_type:
            return
        try:
            session_id = self._sid(event)
        except Exception:
            session_id = ""
        cfg = await self.service.config()
        if not cfg.get("master_switch", True):
            return
        if not self._is_trackable_session(session_id, cfg):
            return
        vis_kind, vis_ident = self.service.session_kind(session_id)
        if not self.service.visible(vis_kind, vis_ident, cfg,
                                    platform=self._session_head(session_id))[0]:
            return
        try:
            mid = str(raw.get("message_id") or "")
            recalled = await self._recalled_by_mid(session_id, mid)
            out = await self.service.handle_recall(
                {
                    "session_id": session_id,
                    "user_id": str(raw.get("user_id") or ""),
                    "operator_id": str(raw.get("operator_id") or ""),
                    "notice_type": notice_type,
                    "message_id": mid,
                    # 撤回正文取证：谁 + 撤回了什么（含图片网址）。取不到就是空，面板如实显示"内容未留存"
                    "recalled": recalled,
                }
            )
            _rimgs = len(recalled.get("imgs") or []) if recalled else 0
            _rskip = int(recalled.get("imgs_skipped") or 0) if recalled else 0
            # 让 **bot 自己知道**：把这次撤回记成一条"待知会"，下一轮请求时注入给它。
            # 依据：AstrBot 的撤回通知不会进入对话历史，bot 本来永远不知道"那句话被撤回了"，
            # 还会拿被撤回的内容当有效上下文继续聊。参考 astrbot_plugin_private_companion 的
            # 做法（它在消息里插 `<!-- private_companion_recall -->` 标记），这里等价但不改聊天记录。
            try:
                if not hasattr(self, "_recall_notes"):
                    self._recall_notes = {}
                _who = str(recalled.get("sender_name") or recalled.get("sender") or "")
                _txt = str(recalled.get("text") or "").strip()
                _note = {"ts": time.time(), "who": _who, "text": _txt[:120],
                         "imgs": _rimgs, "notice": notice_type}
                self._recall_notes.setdefault(session_id, deque(maxlen=5)).append(_note)
                logger.info(f"[StarHeart] 撤回知会已挂起（{_who or '未知'}：{(_txt[:24] + '…') if len(_txt) > 24 else (_txt or ('图片 ' + str(_rimgs) + ' 张' if _rimgs else '无正文'))}）")
            except Exception as exc:                            # noqa: BLE001 知会失败不影响撤回处理
                logger.debug(f"[StarHeart] 撤回知会登记跳过：{exc}")
            # 「撤回打断」：这条消息可能正被 bot 用来生成回复 —— 打断在途的那一轮，
            # 免得它基于"已经被撤回的内容"继续说下去。官方 API（/stop 用的同一个）。
            # ⚠️ 这里**必须自己取配置**：本函数里 `cfg` 要到下面才赋值，早引用会
            #    UnboundLocalError 把整段撤回处理打断（真机日志实证：每次撤回都在这里崩）。
            try:
                _cfgw: dict[str, Any] = {}
                try:
                    _cfgw = await self.service.config()
                except Exception:                               # noqa: BLE001 取不到配置就用默认（打断开）
                    _cfgw = {}
                if bool((_cfgw or {}).get("recall_interrupt_reply", True)):
                    from astrbot.core.utils.active_event_registry import active_event_registry
                    _n = active_event_registry.request_agent_stop_all(session_id, exclude=event)
                    if _n:
                        logger.info(f"[StarHeart] 撤回打断：已请求停止该会话在途回复 {_n} 条")
            except Exception as exc:                            # noqa: BLE001 老版本没这个 API 就算了
                logger.debug(f"[StarHeart] 撤回打断跳过：{exc}")
            logger.info(
                f"[StarHeart] 收到撤回（{notice_type}）：命中记忆 {out.get('matched')} 条，动作 {out.get('action')}"
                + ("；撤回内容已留档（"
                   + (f"含图 {_rimgs} 张" if _rimgs else "纯文本")
                   + (f"，另有 {_rskip} 张图未能留存" if _rskip else "")
                   + "）"
                   if recalled else "；撤回内容未留存（这条消息在插件启动前/未观察过）")
            )
            try:
                cfg = await self.service.config()
            except Exception:
                cfg = {}
            if bool(cfg.get("recall_withdraw_reply", False)):
                # 「撤回连回复一起撤」：本地一定做得到的那部分 —— 撤掉该会话最近落库的 bot 语料
                # （语料是表达学习的数据源）并丢弃待总结轮次，别让这轮对话被总结成记忆。
                try:
                    n = await self.service.withdraw_reply_trace(session_id)
                    self._capture_bufs.pop(session_id, None)
                    logger.info(f"[StarHeart] 撤回连回复：清掉该会话 bot 语料 {n} 条，并丢弃待总结轮次")
                except Exception as exc:                    # noqa: BLE001
                    logger.warning(f"[StarHeart] 撤回连回复失败：{exc}")
                logger.info(
                    "[StarHeart] 说明：平台侧删除 bot 回复需要那条回复的消息 id，"
                    "AstrBot 未把回复 id 交给插件，所以只做本地撤销（不做静默假装）"
                )
            # 「撤回反应」：用户撤回了一条消息 —— **当场**走主动通道起一轮，让模型自己决定
            # 说什么 / 装没看见。用户拍板：不看主动名单、不受免打扰限制（它是"用户触发的"），
            # 输出就是一条普通消息、不写对话历史；可见性名单照常管（不破原生限制）。
            try:
                self._spawn(
                    self.service.recall_react({
                        "session_id": session_id,
                        "message_id": mid,
                        "notice_type": notice_type,
                        "recalled": recalled,
                    }),
                    label="recall-react",
                )
            except Exception as exc:                            # noqa: BLE001
                logger.debug(f"[StarHeart] 撤回反应调度跳过：{exc}")
        except Exception as exc:
            logger.warning(f"[StarHeart] 撤回处理失败：{exc}")

    # ---------------- 后台：调用模型（走「模型分配」里指定的模型） ----------------
    async def _session_provider(self, umo: str = ""):
        """取**该会话**当前使用的对话模型（按 UMO 解析）。

        为什么不用 get_using_provider()：AstrBot 已把它标记 deprecated，且不带 umo 时取全局默认模型 ——
        会话被 /provider 或自定义规则切过模型时，这里就是错的（"跟随会话"名不副实）。
        """
        for attempt in (
            (lambda: self.context.get_using_provider_async(umo=umo)) if umo else None,
            lambda: self.context.get_using_provider_async(),
            lambda: self.context.get_using_provider(),
        ):
            if attempt is None:
                continue
            try:
                got = attempt()
                if got is None:
                    continue
                return await got if asyncio.iscoroutine(got) else got
            except Exception:
                continue
        return None

    async def _llm_generate(self, prompt: str, task: str = "", system_prompt: str | None = None,
                            image_urls: list[str] | None = None, contexts: list[Any] | None = None,
                            umo: str = "", timeout: float | None = None) -> str:
        try:
            cfg = await self.service.config()
        except Exception:
            cfg = {}
        # 只登记**插件自己的活儿**。没有槽位的任务一律用会话当前模型（见下面 provider_id）：
        # 权威来源单一 —— 对话模型、人格进化都只由 AstrBot 自己的设置决定，插件不复制一个「对话回复」槽位。
        key_map = {
            "memory_summary": "model_memory_summary",
            "expression_extract": "model_expression_extract",
            "person_profile": "model_person_profile",
            "group_profile": "model_person_profile",   # 画像 = 人物+群聊同一个槽（用户要求不拆）
            "persona_evolve": "model_persona_evolve",
            "forget": "model_forget",
        }
        # 没登记槽位的任务（主动开口、以及任何未指定任务）→ provider_id 为空 = 用会话当前模型。
        # 为什么：① 吃不到提示词的模型不许参与对话链路（主动开口吃 AstrBot 那份系统提示词）；
        #         ② 权威来源单一 —— 对话模型只有 AstrBot 自己的设置说了算，插件不另造那个槽位（也换不动它）。
        # 人格进化**登记了槽位**：它是纯后台活（产物只是提案，还要人工确认才写回），不参与对话回复，
        # 所以用户可以自己锁一个模型（比如便宜模型），不影响对话链路。
        provider_id = str(cfg.get(key_map[task]) or "") if task in key_map else ""
        provider = self.context.get_provider_by_id(provider_id) if provider_id else None
        if provider is None and provider_id:
            # 槽位填了模型却取不到（改名/删除）→ 必须留痕，不许静默换个模型跑
            logger.warning(f"[StarHeart] 任务 {task} 锁定的模型取不到（{provider_id}），本次回退到会话/全局模型")
        if provider is None:
            # 没登记槽位的任务（主动开口 / 未指定）→ 用**该会话**的模型（按 UMO 解析）
            provider = await self._session_provider(umo)
        if provider is None:
            logger.warning(
                f"[StarHeart] 任务 {task or 'chat'} 取不到可用模型（umo={umo or '空'}、槽位={provider_id or '空'}），本次跳过")
            return ""
        # 系统提示词：面板里配的覆盖优先 → 调用方显式给的 → 插件内置默认（service.TASK_PROMPTS）。
        # 留空 = 内置默认；任务不在清单里（例如主动开口，它吃 AstrBot 那份系统提示词）就原样传。
        _eff = str(system_prompt or "")
        try:
            _info = (getattr(self.service, "TASK_PROMPTS", None) or {}).get(task) or {}
            _ov = str(cfg.get(f"sys_prompt_{task}") or "").strip()
            # 白名单：只有登记在 TASK_PROMPTS 的插件自管任务才允许被覆盖 ——
            # 对话回复/主动开口（吃 AstrBot 原生提示词）永远不可能被 sys_prompt_* 碰到。
            if _ov and task in (getattr(self.service, "TASK_PROMPTS", None) or {}):
                _eff = _ov
            elif not _eff.strip() and _info.get("system"):
                _eff = str(_info["system"])
        except Exception:
            pass
        kwargs: dict[str, Any] = {"prompt": prompt, "session_id": "starheart:bg", "system_prompt": _eff or None}
        if contexts:
            kwargs["contexts"] = list(contexts)
        if image_urls:
            # 原生多模态：图片直接给模型（AstrBot 的 text_chat 原生支持 image_urls）
            kwargs["image_urls"] = list(image_urls)
        # 必须带超时：调用方有 _background_loop 里的 proactive_scan / 总结 / 画像，
        # 裸 await 一旦网络挂起会把整条后台链堵死到进程重启。
        _to = float(timeout or self.LLM_TIMEOUT_SECONDS)
        attempts = 2 if task == "memory_extract" else 1   # 抽取偶发 60s 超时（真机 6 次），给它一次重试
        resp = None
        for attempt in range(attempts):
            try:
                resp = await asyncio.wait_for(provider.text_chat(**kwargs), timeout=_to)
                break
            except TimeoutError:
                if attempt + 1 < attempts:
                    logger.warning(f"[StarHeart] 模型调用超时（{_to:.0f}s，task={task}）：重试一次")
                    continue
                logger.warning(
                    f"[StarHeart] 模型调用超时（{_to:.0f}s，task={task or 'chat'}）"
                    "：放弃本轮结果（返回空串），不让它拖住后台循环"
                )
                return ""
        if resp is None:
            return ""
        chain = getattr(getattr(resp, "result_chain", None), "chain", None) or []
        text = "".join(getattr(part, "text", "") or "" for part in chain)
        raw = getattr(resp, "raw_completion", None)
        usage = getattr(raw, "usage", None)
        if usage is not None:
            await self.store.log_tokens(
                {
                    "ts": time.time(),
                    "task": f"bg:{task or 'chat'}",
                    "model": str(getattr(raw, "model", "") or provider_id),
                    "in_tokens": int(getattr(usage, "prompt_tokens", 0) or 0),
                    "out_tokens": int(getattr(usage, "completion_tokens", 0) or 0),
                }
            )
        return text.strip()

    # ---------------- 后台：真发一条主动消息 ----------------
    async def _proactive_note(self, event: AstrMessageEvent, cfg: dict[str, Any]) -> str:
        """把「最近真发出去的主动消息」压成一小块知会，注入下一轮系统提示词。

        写回开启时这是兜底（写回失败/没会话 id）；写回关闭时靠它让主模型知道自己说过什么。
        """
        if not bool(cfg.get("proactive_note_enabled", True)):
            return ""
        _sid = self._sid(event)
        # 写回成功时不要再注入知会块：同一个事实说两遍，模型会当成两条不同消息。
        # 只有这个会话确实写回失败/历史为空时才兜底。
        if bool(cfg.get("proactive_writeback", True)) and _sid not in self._writeback_failed:
            return ""
        try:
            hours = max(1.0, float(cfg.get("proactive_note_hours") or 24))
            limit = int(cfg.get("proactive_note_max") or 3)
        except (TypeError, ValueError):
            hours, limit = 24.0, 3
        rows = await self.store.recent_sent_proactive(
            _sid, time.time() - hours * 3600.0, limit
        )
        if not rows:
            return ""
        lines: list[str] = []
        for r in rows:
            text = str(r.get("text") or "").strip().replace("\n", " ")
            if not text:
                continue
            ts = time.strftime("%m-%d %H:%M", time.localtime(float(r.get("ts") or 0)))
            lines.append(f"- [{ts}] 「{text[:80]}」")
        if not lines:
            return ""
        # 已注入一轮就清标记：避免同一批主动消息每轮都重复提醒
        self._writeback_failed.discard(_sid)
        return (
            "【你最近主动发过（这些是你自己发的，不是对方说的）】\n"
            + "\n".join(lines)
            + "\n对方问起时按原话承认；不要否认，也不要把它们当成对方的话。"
            "只在相关时参考，不要主动重复。"
        )

    async def _remember_sent(self, session: str, text: str) -> None:
        """把主动发出的消息写回 AstrBot 的对话历史。

        末尾是 assistant 时并入上一条 content parts，避免连续两条 assistant；
        历史为空/写回失败时由 _proactive_note() 兜底知会。
        """
        cm = getattr(self.context, "conversation_manager", None)
        if cm is None or not session or not text:
            if session:
                self._writeback_failed.add(str(session))
            return
        try:
            cid = await cm.get_curr_conversation_id(session)
            if not cid:
                self._writeback_failed.add(str(session))
                return                       # 没会话就不硬造，避免给没聊过的人凭空生上下文
            conv = await cm.get_conversation(session, cid)
            # 字段名是 history（JSON 字符串），不是 content —— conversation_manager 给的是
            # 兼容 v1 的模型。早期版本这里读 content 永远是 None，等于把「空历史」写回去，
            # 一次主动消息就能把整段会话压成一条（真机事故：41 条 / 932KB 被覆盖）。
            raw_hist = getattr(conv, "history", None)
            if isinstance(raw_hist, list):
                hist = list(raw_hist)
            elif isinstance(raw_hist, str):
                try:
                    parsed = json.loads(raw_hist) if raw_hist.strip() else []
                except ValueError:
                    parsed = None
                if not isinstance(parsed, list):
                    self._writeback_failed.add(str(session))
                    logger.warning("[StarHeart] 读不到会话历史（格式不认识），本次不写回，避免覆盖")
                    return
                hist = parsed
            else:
                self._writeback_failed.add(str(session))
                logger.warning("[StarHeart] 读不到会话历史（字段缺失），本次不写回，避免覆盖")
                return
            tail = hist[-1] if hist else None
            if isinstance(tail, dict) and tail.get("role") == "assistant":
                _tc = tail.get("content")
                if isinstance(_tc, list):
                    _tail_text = "".join(
                        str(p.get("text") or "") for p in _tc if isinstance(p, dict)
                    )
                else:
                    _tail_text = str(_tc or "")
                if _tail_text.strip() == text.strip():
                    self._writeback_failed.discard(str(session))
                    return                   # 幂等：同一条不重复写
                # 末尾已经是 assistant：不要再追加一条（模型会把两条并成一条）。
                # 把主动消息并入上一轮 assistant 的 content parts，历史长度不变。
                if isinstance(_tc, list):
                    _tc.append({"type": "text", "text": "\n" + text})
                else:
                    tail["content"] = [
                        {"type": "text", "text": str(_tc or "")},
                        {"type": "text", "text": "\n" + text},
                    ]
                await cm.update_conversation(session, conversation_id=cid, history=hist)
                self._writeback_failed.discard(str(session))
                logger.info(f"[StarHeart] 主动消息并入上一条 assistant（{len(hist)} 条）← {session}")
                return
            if not hist:
                self._writeback_failed.add(str(session))
                logger.info("[StarHeart] 主动写回跳过：历史为空（留给知会块兜底）")
                return
            before = len(hist)
            # 用 AstrBot 原生 content parts（不是裸字符串）；即使开了写回，也不要再制造
            # 「assistant 的 content 是 str」这种和正常记录不一致的条目。
            hist.append({"role": "assistant", "content": [{"type": "text", "text": text}]})
            if len(hist) <= before:          # 防呆：只增不减，任何情况下都不许把历史写短
                return
            await cm.update_conversation(session, conversation_id=cid, history=hist)
            self._writeback_failed.discard(str(session))
            logger.info(f"[StarHeart] 已写回对话历史（{len(hist)} 条）← {session}")
        except Exception as exc:             # noqa: BLE001
            self._writeback_failed.add(str(session))
            logger.warning(f"[StarHeart] 写回对话历史失败（不影响已发出的消息）：{exc}")

    def _split_cap(self, cfg: dict[str, Any]) -> int:
        """一轮最多拆几条（面板键 output_split_max，2–8）；脏值退回 4。

        为什么必须有上限：模型会把「想多说」写成十几行短句，空行协议就照单全拆 ——
        真机出现过一轮 9 条、32 秒内连发（用户原话「话唠，一发发一堆」）。
        """
        try:
            v = int(float(cfg.get("output_split_max", 4) or 4))
        except (TypeError, ValueError):
            v = 4
        return max(2, min(8, v))

    def _split_for_send(self, text: str, cfg: dict[str, Any], typo_mode: str = "off",
                        max_seg: int = 6) -> tuple[list[str], str]:
        """把一条待发文本切段（主动通道用）：判定交给 engine.split_for_send，这里只逐段收尾。

        主动消息不走 AstrBot 的 result_decorate 管线，`humanize_output` 永远收不到它，
        所以「空行 = 分一条」的协议必须在发送层自己兑现，否则提示词教模型分段等于白教。
        不拆时返回 ([原文], "")，调用方照旧整条发，避免意外改动未开分段时的观感。
        """
        segs, how = engine.split_for_send(str(text or ""), max_seg=max_seg)
        if len(segs) <= 1:
            return [text], ""
        cleaned = [engine.apply_typo(engine.tidy_output(s), typo_mode, cfg.get("output_typo_rate")) for s in segs]
        cleaned = [s for s in cleaned if s.strip()]
        if len(cleaned) <= 1:
            return [text], ""
        return cleaned, how

    async def _send_proactive(self, session: str, text: str, tag: str = "主动消息", note: str = "",
                              *, delay_ms: int = 0, kind: str = "", seg_index: int = 0,
                              seg_total: int = 1) -> bool:
        """主动通道发送（主动开口 / 撤回反应 / 面板试发 / 分段补发都走这里）。

        发送后落一笔 hf_sent_log；记账失败只记 WARNING，不改发送结果。
        开了「分段发送」时，主动消息也按空行协议拆成多条（首条同步发，其余后台补发）——
        这条通道没有 event/result，框架的 on_decorating_result 钩子照不到它。
        """
        if not session or not text:
            return False
        cfg: dict[str, Any] = {}
        segs: list[str] = [text]
        how = ""
        # 「分段补发」是补发通道自己，不能再拆（否则递归）；其余主动入口一律按同一协议切
        if tag != "分段补发":
            try:
                cfg = await self.service.config()
            except Exception:                            # noqa: BLE001 读不到配置就按不拆发，不许影响发送
                cfg = {}
            if bool(cfg.get("output_enabled", True)) and bool(cfg.get("output_split_messages", False)):
                segs, how = self._split_for_send(
                    text, cfg, str(cfg.get("output_typo_mode") or "off"), max_seg=self._split_cap(cfg)
                )
        first = segs[0] if len(segs) > 1 else text
        ok = False
        try:
            chain = MessageChain([Plain(first)])
            ok = await asyncio.wait_for(self.context.send_message(session, chain), timeout=20)
            # 先判结果再记日志：原实现先打「已发送」再 return bool(ok)，
            # 发送失败时日志照样写着成功 —— 排查时会被这条假日志带偏。
            if ok:
                suffix = f"（{note}）" if note else ""
                logger.info(f"[StarHeart] {tag}已发送 → {session}: {first[:40]}{suffix}")
                if len(segs) > 1:
                    logger.info(f"[StarHeart] {tag}分段：首条 + 补发 {len(segs) - 1} 条（{how}）")
                # 只有「bot 自己开的口」才考虑写回；撤回反应按设计不入历史，
                # 分段补发的全文已由 AstrBot 管线记账。写回时并入上一条 assistant，
                # 避免连续两条 assistant 被模型并成一句。写回用**全文**，不是首段。
                if tag == "主动消息":
                    if cfg.get("proactive_writeback", False):
                        await self._remember_sent(session, text)
            else:
                logger.warning(f"[StarHeart] {tag}未发出（send_message 返回假值）→ {session}")
        except Exception as exc:
            logger.warning(f"[StarHeart] {tag}发送失败：{exc}")
            ok = False
        try:
            await self.store.log_sent({
                "session_id": session,
                "kind": kind or _SENT_KIND_BY_TAG.get(tag, "proactive"),
                # 一条流水只记一条真实消息：拆段后首条记首段，不能拿全文冒充（length/hash 会对不上）
                "text": first,
                "delay_ms": int(delay_ms or 0),
                "seg_index": int(seg_index or 0),
                "seg_total": len(segs) if len(segs) > 1 else int(seg_total or 1),
                "emit_ok": 1 if ok else 0,
                "trigger": tag,
            })
        except Exception as exc:                            # noqa: BLE001 记账不许影响发送结果
            logger.warning(f"[StarHeart] 发送记账失败（不影响已发出的消息）：{exc}")
        try:
            # 出站对账（S4-3）：拿**出站原文**就近判「way/scene 子串命中」—— hf_sent_log 不存原文，
            # 所以只能在这里判；漏挂一处 = 那条路径永远不计入 use_count。失败只 WARNING。
            await self.store.reconcile_expr_use(str(session or ""), str(first if len(segs) > 1 else text) or "",
                                                sent_ok=bool(ok))
        except Exception as exc:                            # noqa: BLE001 对账不许影响发送结果
            logger.warning(f"[StarHeart] 表达出站对账失败（不影响已发出的消息）：{exc}")
        # 首条已交给平台，剩下的段按打字节奏后台补发（失败不影响首条，也不回收 ok）
        if ok and len(segs) > 1:
            try:
                self._spawn(self._send_followups(session, [("text", s) for s in segs[1:]], cfg,
                                                 seg_offset=1, seg_total=len(segs)),
                            label="proactive-followups")
            except Exception as exc:                        # noqa: BLE001 调度失败只降级成单条
                logger.warning(f"[StarHeart] {tag}分段补发调度失败（首条已发出）：{exc}")

        return bool(ok)

    # ---------------- 后台循环：主动扫描 + 记忆衰减维护 ----------------

    # ---------------- 自动遗忘 / 关系链自维护 / 自动导入 ----------------

    async def _forget_tick(self) -> None:
        """归档 + 遗忘一轮。重要记忆（强度高 / 情绪强）受保护，不会被遗忘。"""
        try:
            res = await self.service.lifecycle_tick()
            if res.get("archived") or res.get("forgotten"):
                logger.info(f"[StarHeart] 记忆生命周期：归档 {res.get('archived')} / 遗忘 {res.get('forgotten')}")
        except Exception as exc:                             # noqa: BLE001 后台任务不许把主循环带崩
            logger.warning(f"[StarHeart] 自动遗忘失败：{exc}")

    async def _link_autobuild(self) -> None:
        """关系链自动维护：边数明显少于节点数才重建（避免无意义重算）。"""
        try:
            stats = await self.store.graph_stats("")
            n, e = int(stats.get("nodes") or 0), int(stats.get("edges") or 0)
            if n > 50 and e < n * 0.5:
                out = await self.store.rebuild_links(3000)
                logger.info(f"[StarHeart] 关系链自动重建：节点 {n} / 边 {e} → {out.get('edges')}")
        except Exception as exc:                             # noqa: BLE001
            logger.debug(f"[StarHeart] 关系链自维护跳过：{exc}")


    async def _auto_import_watch(self) -> None:
        """自动识别 livingmemory 里还没转换过来的记忆并导入（幂等：external_id 去重，重复跑不翻倍）。"""
        try:
            cfg = await self.service.config()
            if not cfg.get("auto_import_external", True):
                return
            info = await self.service.import_detect()
            pending = int(info.get("documents") or 0) + int(info.get("atoms") or 0) - int(info.get("already_imported") or 0)
            if info.get("db_exists") and pending > 0:
                logger.info(f"[StarHeart] 检测到 {pending} 条未转换的外部记忆，自动开始导入")
                await self.service.import_start(layers=["documents", "atoms"])
                await self.service.import_extras()          # 顺带补齐对话/来源/完整原文
        except Exception as exc:                             # noqa: BLE001
            logger.debug(f"[StarHeart] 自动导入检查跳过：{exc}")

    async def _background_loop(self) -> None:
        last_decay = 0.0
        last_capture_retry = 0.0
        last_recall_cleanup = 0.0
        last_import_watch = 0.0
        last_forget_tick = 0.0
        last_link_check = 0.0
        last_auto_import = 0.0
        last_profile_tick = 0.0
        last_evolve_tick = 0.0
        last_habit_tick = 0.0
        last_jargon_tick = 0.0
        last_style_tick = 0.0
        while True:
            try:
                now_t = time.time()
                # 每轮顺手修剪会话级内存字典（_session_windows / _burst / _capture_* 等）
                self._prune_memory_maps(now_t)
                # 总开关必须最先判：以前 _forget_tick / 关系链 / 自动导入都在它之前，
                # 用户「停用插件」后插件仍在归档 + 遗忘（P0-11）。
                cfg = await self.service.config()
                if not cfg.get("master_switch", True):
                    await asyncio.sleep(60)
                    continue
                # 记忆捕获：会话静默 10 分钟还没攒满 → 直接总结
                await self._capture_idle_flush()
                # 到期重试的捕获批（失败不丢，退避再来）
                if now_t - last_capture_retry > 120:
                    last_capture_retry = now_t
                    await self._capture_retry_tick()
                # 自动遗忘：每 2 小时一轮（原每 30 分钟；没候选时不调模型，降频省 token，2026-10-15）
                if now_t - last_forget_tick > 7200:
                    last_forget_tick = now_t
                    await self._forget_tick()
                # 关系链自维护：边数明显偏少才重建（用户要求：这是自动的，不要按钮）
                if now_t - last_link_check > 3600:
                    last_link_check = now_t
                    await self._link_autobuild()
                # 自动识别未转换的外部记忆（livingmemory）并转换导入：幂等，靠 external_id 去重
                if now_t - last_auto_import > 900:
                    last_auto_import = now_t
                    await self._auto_import_watch()
                await asyncio.sleep(60)
                if cfg.get("proactive_shadow", True) or cfg.get("proactive_enabled", False):
                    try:
                        await self.service.proactive_scan()
                    except Exception as exc:                # noqa: BLE001 主动失败不许拖垮后续维护
                        logger.warning(f"[StarHeart] 主动扫描失败（本轮跳过）：{exc}")
                if time.time() - last_decay > 3600:
                    last_decay = time.time()
                    if cfg.get("memory_decay_half_life_days"):
                        await self.service.memory_maintenance()
                    await self.service.favour_decay_tick()   # 「好感每日半衰期」（键为 0 时内部直接返回）
                # 表达学习自动提取：未学语料攒够就学一次（不依赖面板手动触发）
                if time.time() - last_habit_tick > 1800:
                    last_habit_tick = time.time()
                    await self._habit_auto_tick(cfg)
                # 黑话含义推断（S5-1）：每日 ≤1 批（拍-16）—— 门槛/配额在 service 里按配置判定
                if time.time() - last_jargon_tick > 1800:
                    last_jargon_tick = time.time()
                    await self._jargon_auto_tick(cfg)
                # 语言学习：每 6 小时检查一次（新语料够量才真跑，事件触发）
                if time.time() - last_style_tick > 21600:
                    last_style_tick = time.time()
                    await self._style_auto_tick(cfg)
                if time.time() - last_recall_cleanup > 86400:
                    last_recall_cleanup = time.time()
                    removed = await self.service.recall_log_cleanup()
                    # 直播留档（hf_messages 里 qq: 行）也要有保留期：不清理会无限长 + 撤回内容被长期留存
                    await self.store.purge_live_messages(int(cfg.get("recall_keep_days", 30) or 30))
                    if removed:
                        logger.info(f"[StarHeart] 撤回流水清理：删除 {removed} 条过期记录")
                    # 流水保留期（30/30/90/30 天）；失败只记 WARNING，不拖垮后台循环
                    # 回收站真删：软删超过 30 天的才物理清掉（保留可恢复窗口，不让 deleted 行永久堆积）
                    try:
                        purged = await self.store.purge_deleted_memories(30)
                        if purged:
                            logger.info(f"[StarHeart] 回收站清理：物理删除 {purged} 条（软删已满 30 天）")
                    except Exception as exc:                # noqa: BLE001 清理失败不影响其它维护任务
                        logger.warning(f"[StarHeart] 回收站清理失败：{exc}")
                    try:
                        flow = await self.store.purge_flow_logs()
                        if any(int(v or 0) for v in flow.values()):
                            logger.info(f"[StarHeart] 流水保留期清理：{flow}")
                    except Exception as exc:                # noqa: BLE001 清理失败不影响其它维护任务
                        logger.warning(f"[StarHeart] 流水保留期清理失败：{exc}")
                # 门禁改判「实际可用的嵌入模型」：model_embedding 默认空串 = 跟随 AstrBot，
                # 按它判会让每小时补向量任务永不执行（825 条记忆只有 47 条有向量）。
                if self._embed_model_name(cfg):
                    await self._backfill_embeddings()
                if self.service._import_watch and time.time() - last_import_watch > 300:
                    last_import_watch = time.time()
                    await self.service.import_watch_tick()
                # 人格自动进化：按策略出提案（默认需人工确认，不会自己写人格）
                if time.time() - last_evolve_tick > 3600:
                    last_evolve_tick = time.time()
                    try:
                        res = await self.service.evolve_tick()
                        if res.get("proposed") or res.get("auto_applied"):
                            logger.info(f"[StarHeart] 人格进化：{str(res)[:200]}")
                    except Exception as exc:
                        logger.warning(f"[StarHeart] 人格进化失败：{exc}")
                # 人物画像：按策略自动补齐/刷新（默认开，配置里可关）
                if time.time() - last_profile_tick > 1800:
                    last_profile_tick = time.time()
                    try:
                        res = await self.service.auto_profile_tick()
                        if res.get("built"):
                            logger.info(f"[StarHeart] 自动画像：{res}")
                    except Exception as exc:
                        logger.warning(f"[StarHeart] 自动画像失败：{exc}")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(f"[StarHeart] 后台循环异常：{exc}")

    # ---------------- 人格（复用 AstrBot 原生 PersonaManager） ----------------
    def _schema_keys(self) -> list[str]:
        """本插件 _conf_schema.json 声明过的键（只有这些能写进原生配置，其它会被完整性检查清掉）。"""
        try:
            p = Path(__file__).with_name("_conf_schema.json")
            return list(json.loads(p.read_text(encoding="utf-8-sig")).keys())
        except Exception:
            return []

    async def save_native_config(self, patch: dict[str, Any]) -> dict[str, Any]:
        """把开关写回 AstrBot 原生插件配置（= WebUI 插件配置页那一个文件）。

        为什么必须走原生：面板私库与原生页是两份配置（红队审计），只写私库会出现
        「原生页改了不生效 / 面板改了原生页看不见」。这里只写 schema 声明过的键。
        """
        cfg = self.bot_config
        if cfg is None or not hasattr(cfg, "save_config_async"):
            return {"ok": False, "error": "没拿到 AstrBotConfig（插件未以原生方式注入配置）"}
        keys = set(self._schema_keys())
        applied: list[str] = []
        skipped: list[str] = []
        for k, v in (patch or {}).items():
            if k in keys:
                try:
                    cfg[k] = v
                    applied.append(k)
                except Exception:
                    skipped.append(k)
            else:
                skipped.append(k)
        if applied:
            try:
                await cfg.save_config_async()
            except Exception as exc:                            # noqa: BLE001
                return {"ok": False, "error": f"写原生配置失败：{exc}", "applied": sorted(applied)}
        return {"ok": True, "applied": sorted(applied), "not_in_schema": sorted(skipped)}

    def _persona_manager(self) -> Any:
        """AstrBot 原生 PersonaManager（context 上暴露）。取不到时返回 None（老版本兼容）。"""
        pm = getattr(self.context, "persona_manager", None)
        return pm

    async def _persona_list(self) -> list[dict[str, Any]]:
        """列人格：优先走原生 PersonaManager（带内存态与内置 default），DB 只作兜底。"""
        rows: list[Any] = []
        pm = self._persona_manager()
        if pm is not None:
            for fn in ("get_all_personas", "get_personas"):
                f = getattr(pm, fn, None)
                if callable(f):
                    try:
                        rows = list(await f() or [])
                    except Exception:
                        rows = []
                    break
        if not rows:
            try:
                db = self.context.get_db()
                rows = await db.get_personas()
            except Exception as exc:
                logger.warning(f"[StarHeart] 读取人格失败：{exc}")
                return []
        out: list[dict[str, Any]] = []
        for p in rows:
            out.append(
                {
                    "persona_id": getattr(p, "persona_id", ""),
                    "system_prompt": getattr(p, "system_prompt", "") or "",
                    "begin_dialogs": getattr(p, "begin_dialogs", None) or [],
                    "tools": getattr(p, "tools", None),
                    "skills": getattr(p, "skills", None),
                    "prompt_len": len(getattr(p, "system_prompt", "") or ""),
                }
            )
        return out

    def _active_persona_id_sync(self) -> str:
        """读 AstrBot 当前实际采用的人格（agent_runner.config.persona.persona_id）。"""
        try:
            node: Any = self.context.get_config()
            for key in ("agent_runner", "config", "persona", "persona_id"):
                if not hasattr(node, "get"):
                    return ""
                node = node.get(key)
                if node is None:
                    return ""
            return str(node)
        except Exception:
            return ""

    async def _active_persona_id(
        self,
        umo: str = "",
        *,
        conversation_persona_id: str | None = None,
        platform_name: str = "",
    ) -> str:
        """当前人格：按**原生优先级**解析（会话规则 > 会话级 > 全局默认）。

        旧实现只读全局默认那一条 → 多会话下会把「被会话规则强制指定的人格」「会话自选人格」
        都当成全局默认，导致记忆按错人格归档、主动消息用错语气（审计实测）。

        调用方式必须对齐 AstrBot 原生签名（`umo` / `conversation_persona_id` /
        `platform_name` / `provider_settings` 全是 keyword-only，前三个必填），
        少传就是 TypeError → 静默回退全局。具体规则见 core/persona.py。
        """
        return await persona_rt.resolve_persona_id(
            self._persona_manager(),
            umo=str(umo or ""),
            conversation_persona_id=conversation_persona_id,
            platform_name=platform_name,
            fallback=self._active_persona_id_sync(),
        )

    async def _activate_persona(self, pid: str) -> dict[str, Any]:
        """切换 AstrBot 全局人格：写官方配置路径 agent_runner.config.persona.persona_id 并保存。"""
        if not pid:
            return {"ok": False, "error": "persona_id 为空"}
        if pid != "default":          # 'default' 是原生内置人格（DEFAULT_PERSONALITY），DB 里通常没有
            items = await self._persona_list()
            if not any(p.get("persona_id") == pid for p in items):
                return {"ok": False, "error": f"人格 {pid} 不存在"}
        try:
            cfg = self.context.get_config()
            ar = cfg.get("agent_runner") or {}
            runner_type = str(ar.get("runner_type") or "").lower()
            conf = ar.get("config") or {}
            # 两条路径都写：local 运行器读嵌套 config.persona.persona_id，其它运行器读扁平
            # config.persona_id（审计实测：只写嵌套时非 local 会「返回成功但静默不生效」）。
            conf.setdefault("persona", {})["persona_id"] = pid
            conf["persona_id"] = pid
            ar["config"] = conf
            cfg["agent_runner"] = ar
            cfg.save_config()
            return {"ok": True, "active": pid, "scope": "全局默认",
                    "runner_type": runner_type or "default",
                    "note": "改的是全局默认人格；单个会话的临时人格由 AstrBot 的会话规则管"}
        except Exception as exc:
            logger.warning(f"[StarHeart] 切换人格失败：{exc}")
            return {"ok": False, "error": str(exc)}

    async def _system_providers(self) -> dict[str, Any]:
        """读 AstrBot 真实模型配置：对话 / TTS / 嵌入 / STT / 重排（面板「模型分配」用）。"""
        ctx = self.context
        out: dict[str, Any] = {"llm": [], "tts": [], "embedding": [], "stt": [], "rerank": [], "errors": []}

        def pack(provider: Any) -> dict[str, Any]:
            meta = provider.meta()
            return {
                "id": getattr(meta, "id", ""),
                "model": getattr(meta, "model", "") or "",
                "adapter": getattr(meta, "type", ""),
                "capability": str(getattr(meta, "provider_type", "")),
            }

        for key, name in (
            ("llm", "get_all_providers"),
            ("tts", "get_all_tts_providers"),
            ("embedding", "get_all_embedding_providers"),
            ("stt", "get_all_stt_providers"),
        ):
            fn = getattr(ctx, name, None)
            if not callable(fn):
                continue
            try:
                for provider in fn() or []:
                    try:
                        out[key].append(pack(provider))
                    except Exception as exc:
                        out["errors"].append(f"{key}:{exc}")
            except Exception as exc:
                out["errors"].append(f"{key}:{exc}")
        # Context 没有 get_all_rerank_providers，重排走 provider_manager
        try:
            for provider in list(getattr(ctx.provider_manager, "rerank_provider_insts", []) or []):
                try:
                    out["rerank"].append(pack(provider))
                except Exception as exc:
                    out["errors"].append(f"rerank:{exc}")
        except Exception as exc:
            out["errors"].append(f"rerank:{exc}")
        # 当前会话在用的模型（面板显示「跟随会话当前模型」是谁）
        try:
            using = ctx.get_using_provider()
            if using is not None:
                meta = using.meta()
                out["current"] = {"id": getattr(meta, "id", ""), "model": getattr(meta, "model", "") or ""}
        except Exception:
            pass
        return out

    async def _model_test(self, task: str, provider_id: str) -> dict[str, Any]:
        """真调一次模型：LLM 走 text_chat，嵌入/重排走 get_embedding（用户点「试跑」才触发）。"""
        # 注意：这里必须把**所有走 LLM 的槽位**列全 —— 漏掉的会掉进下面的 else 去调 get_embedding，
        # 于是"试跑"必然失败（人物画像 就曾因此点了报错）。已下线的槽位保留名字，兼容老面板缓存。
        chat_tasks = {
            "memory_summary", "expression_extract", "forget", "person_profile", "group_profile",
            "chat", "proactive", "vision", "persona_evolve",
        }
        provider = None
        if provider_id:
            provider = self.context.get_provider_by_id(provider_id)
        elif task in ("embedding", "rerank"):
            # 工具槽位留空 = 跟随 AstrBot 已配置的那个（与真实调用路径一致，不能拿对话模型来"试跑"）
            provider = self._first_tool_provider(task)
        else:
            provider = await self._session_provider()
        if provider is None:
            return {"ok": False, "error": "找不到该提供商（可能已被改名或删除，或 AstrBot 里还没配这类模型）"}
        started = time.time()
        try:
            if task in chat_tasks:
                resp = await provider.text_chat(prompt="只回复两个字：心跳", session_id="starheart:model-test")
                chain = getattr(getattr(resp, "result_chain", None), "chain", None) or []
                text = "".join(getattr(part, "text", "") or "" for part in chain)
                raw = getattr(resp, "raw_completion", None)
                usage = getattr(raw, "usage", None)
                if usage is not None:
                    await self.store.log_tokens(
                        {
                            "ts": time.time(),
                            "task": "model_test",
                            "model": str(getattr(raw, "model", "") or provider_id),
                            "in_tokens": int(getattr(usage, "prompt_tokens", 0) or 0),
                            "out_tokens": int(getattr(usage, "completion_tokens", 0) or 0),
                        }
                    )
            elif task == "rerank":
                if not hasattr(provider, "rerank"):
                    return {"ok": False, "error": "这个槽位需要「重排」类型的提供商，当前选的不是"}
                res = await provider.rerank("心跳", ["心跳", "今天吃什么", "睡觉"])
                text = f"重排 {len(res)} 条，最相关=第 {res[0].index + 1} 条" if res else "重排返回空"
            else:
                # 嵌入槽位：选错类型要说人话，而不是抛 AttributeError 给用户看
                if not hasattr(provider, "get_embedding"):
                    return {"ok": False, "error": "这个槽位需要「嵌入」类型的提供商，当前选的不是"}
                vec = await provider.get_embedding("心跳")
                text = f"返回向量维度 {len(vec)}"
            return {"ok": True, "ms": int((time.time() - started) * 1000), "reply": str(text).strip()[:80] or "（空回复）"}
        except Exception as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:200]}

    async def _persona_update(self, persona_id: str, fields: dict[str, Any]) -> None:
        """写人格：**必须走原生 PersonaManager**（它同时刷 DB 与内存 personas_v3）。

        直写 DB 的两个坑（审计实测）：①不触发 get_v3_persona_data() → 运行中的 bot 继续吃
        旧人格，必须重启才生效；②update_persona 的 tools=None 是「有效值」不是「不修改」，
        部分更新会把该人格的工具白名单静默重置成「全部工具」。
        """
        kwargs: dict[str, Any] = {}
        for k in ("system_prompt", "begin_dialogs", "tools"):
            if k in fields and fields.get(k) is not None:     # 没提供就不传，绝不传 None
                kwargs[k] = fields[k]
        pm = self._persona_manager()
        if pm is not None and hasattr(pm, "update_persona"):
            try:
                await pm.update_persona(persona_id, **kwargs)
                return
            except Exception as exc:                          # noqa: BLE001
                logger.warning(f"[StarHeart] 原生 PersonaManager 写人格失败，退回 DB：{exc}")
        await self.context.get_db().update_persona(persona_id, **kwargs)

    @staticmethod
    def _gate_config_ver(cfg: dict[str, Any]) -> str:
        """门控关键配置的快照指纹（hf_decision_log.config_ver）：影响判定的配置一变，指纹跟着变，便于按版本对账。"""
        keys = ("gate_enabled", "gate_shadow", "gate_threshold", "gate_window_seconds",
                "gate_backoff_base", "gate_backoff_max")
        raw = json.dumps([cfg.get(k) for k in keys], ensure_ascii=False, default=str)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]

    # ---------------- 门控：旁听全部消息 ----------------
    # priority=-100 让本处理器尽量排后：其他插件的指令/监听先跑，这里只旁观。
    # 注意：priority 只影响同类型 handler 的排序，不保证绝对时序；
    # 门控是纯记录+影子判定，不依赖与其他插件的先后。
    @filter.event_message_type(EventMessageType.ALL, priority=-100)
    async def observe_messages(self, event: AstrMessageEvent):
        # 顶层兜底：这里的异常会冒到 AstrBot 的 star_request 异常分支，变成用户可见的
        # 「:( 调用插件 StarHeart...」并且 stop_event() 吃掉整轮对话 —— 绝不能发生。
        try:
            await self._observe_messages_inner(event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:                            # noqa: BLE001
            logger.warning(f"[StarHeart] 旁听处理异常（已吞掉，不影响本轮对话）：{exc}")

    async def _observe_messages_inner(self, event: AstrMessageEvent):
        if not await self._ensure_ready(timeout=3):
            return
        # OneBot 通知事件（撤回等）的 message_str 为空，必须在文本检查之前处理，
        # 否则这个分支永远不会执行
        raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
        if isinstance(raw, dict) and str(raw.get("post_type") or "") == "notice":
            await self._handle_notice(event, raw)
            return
        text = (event.message_str or "").strip()
        cfg = await self.service.config()
        # master_switch=False = StarHeart整体不介入（面板文案：「关掉后StarHeart整体不介入」）
        if not cfg.get("master_switch", True):
            return
        gate_on = bool(cfg.get("gate_enabled", True))
        # 平台边界统一判定（OneBot + WebChat；排除名单来自 person_exclude_platforms）
        if not self._is_trackable_session(self._sid(event), cfg):
            return
        session_id = self._sid(event)
        # 基础名单（可见性）：黑名单里的群/人对StarHeart「完全不存在」——
        # 门控不记、语料不落、记忆不写、好感不算、主动候选不进。
        vis_kind, vis_ident = self.service.session_kind(session_id)
        vis_ok, vis_why = self.service.visible(vis_kind, vis_ident, cfg,
                                               platform=self._session_head(session_id))
        if not vis_ok:
            logger.debug(f"[StarHeart] 名单拦截（{vis_why}）：{session_id}")
            if not cfg.get("gate_shadow", True):
                event.stop_event()
            return
        # 留档放在总开关/平台/可见性判定之后：拉黑的群、关掉的会话不再落明文。
        # 纯图片消息 message_str 为空，但撤回时同样要能给出「谁撤回了什么」；命令也留。
        await self._keep_live_message(event, text)
        await self._remember_person_name(event)
        # 纯图片消息 message_str 为空：图片是记忆来源，不能在这里被拦掉。
        # 旧实现 `if not text` 让「群里发一张图、bot 没回」永远进不了记忆（P1-1）。
        images = self._collect_image_urls(event)
        if text.startswith("/"):
            return
        if not text and not images:
            return
        try:
            is_private = event.is_private_chat()
        except Exception:
            is_private = False
        is_at = False
        try:
            is_at = bool(event.is_at_or_wake_command)
        except Exception:
            is_at = False
        if gate_on and text:
            fav = {f["user_id"]: f for f in await self.store.list_favours()}
            sender = self._sender_id(event)
            relation = engine.relation_coefficient(float(fav.get(sender, {}).get("daily", 0)))
            threshold = float(cfg.get("gate_threshold", 80))
            window_count = await self._session_window_count(session_id)
            # 退避对"私聊"和"被 @ "不生效：那两种情况本来就该回
            in_backoff = self._backoff_tick(session_id, window_count, cfg) and not is_at and not is_private
            result = engine.score_message(
                text,
                is_at=is_at,
                is_private=is_private,
                window_count=window_count,
                relation_coef=relation,
                threshold=threshold,
                in_backoff=in_backoff,
            )
            decision = result["decision"]
            shadow = bool(cfg.get("gate_shadow", True))
            final = decision
            # 非影子模式：沉默与退避都真的把事件停掉（影子模式只记录，不动行为）
            if decision in ("silent", "backoff") and not shadow and not is_at and not is_private:
                final = "blocked"
                event.stop_event()
            await self.store.log_gate(
                {
                    "ts": time.time(),
                    "session_id": session_id,
                    "user_id": sender,
                    "score": result["score"],
                    "factors": result["factors"],
                    "decision": final,
                    "threshold": threshold,
                    "note": "shadow" if shadow else "",
                }
            )
            # 决策流水（hf_decision_log）：因子快照 + 判定理由 + 消息文本（截断 ≤500 字 + hash）。
            # 与 hf_gate_log 并存；失败只记 WARNING，不影响本轮判定。
            try:
                await self.store.log_decision(
                    {
                        "event_id": self._msg_id_of(event),
                        "session_id": session_id,
                        "user_id": sender,
                        "eligible": int(not is_at and not is_private),   # 具备门控拦截条件（非私聊且未被 @）
                        "decision": final,
                        "score": result.get("score"),
                        "factors": result.get("factors") or {},
                        "reason": result.get("reason") or "",
                        "config_ver": self._gate_config_ver(cfg),
                        "gate_shadow": shadow,
                        "text": text,
                    }
                )
            except Exception as exc:                        # noqa: BLE001 记账不许影响主流程
                logger.warning(f"[StarHeart] 决策记账失败（不影响本轮判定）：{exc}")
            # 给同一进程里的其它插件留一个只读抓手（AstrBot 的 set_extra 就是跨插件通道）。
            # 本插件内部不读它们 —— 这就是为什么静态扫描会把它们报成「死数据」：
            # 它们是**刻意的对外集成点**，不是遗留物，别删。
            event.set_extra("_hf_gate_score", result["score"])
            event.set_extra("_hf_gate_decision", final)
        # 群友/好友原话落语料：表达自学习与人格自进化的唯一数据源（不落就没法学说话）
        try:
            await self._record_corpus(event, text, is_bot=False)
        except Exception as exc:
            logger.debug(f"[StarHeart] 语料落库跳过：{exc}")
        # 记下本会话这条/上一条的时间：注入里的「距上一条消息多久」靠它（AstrBot 历史里没有时间）
        try:
            self._touch_session(session_id)
        except Exception as exc:                            # noqa: BLE001 记账失败不许影响本轮
            logger.debug(f"[StarHeart] 会话时间记账跳过：{exc}")
        # 消息留档不在这里做：上面（早退之前）已经对**所有**消息调用过一次 _keep_live_message，
        # 覆盖了图片-only 与 / 开头命令；这里再调一次是纯重复的 DB 往返（审查实证：每条消息被写两遍）。
        # 同一人连发多条时记一笔，供 on_llm_request 合并（bot 自己决定合并回还是逐条回）
        try:
            await self._burst_push(event, text)
        except Exception as exc:
            logger.debug(f"[StarHeart] 连发合并记录跳过：{exc}")
        # 群聊里 bot 常常沉默（没 @ 就不回），但用户在群里的发言同样是记忆来源 →
        # 这里先记一条「用户单说」的缓冲，bot 若有回应会在 on_llm_response 里补上 bot 文本。
        try:
            # 图片必须在这里收进缓冲：不传 images 的话「图像识别」开关整条链路空转
            # （_run_capture 里读的是 turns[*]["imgs"]，恒空 = 图片永远进不了记忆）
            await self._capture_user_only(event, text, images)
        except Exception as exc:
            logger.debug(f"[StarHeart] 记忆捕获（用户侧）跳过：{exc}")

    @staticmethod
    def _sender_id(event: AstrMessageEvent) -> str:
        for getter in ("get_sender_id",):
            fn = getattr(event, getter, None)
            if callable(fn):
                try:
                    value = fn()
                    if value:
                        return str(value)
                except Exception:
                    pass
        return ""

    def _jargon_scopes(self, event: AstrMessageEvent) -> list[str]:
        """黑话匹配的 scope 取值域（拍-17）：全局 + 群·{gid} + 人·{uid}（拿不到的段不加）。

        这几个 scope 会作为参数送进 SQL（jargon_match_pool）做过滤下推 —— 会话看不到的词条
        根本不会进入匹配，不会串群、不会串人。
        """
        out = ["全局"]
        try:
            gid = str(getattr(event.message_obj, "group_id", "") or "")
        except Exception:
            gid = ""
        if gid:
            out.append(f"群·{gid}")
        try:
            uid = self._sender_id(event)
        except Exception:
            uid = ""
        if uid:
            out.append(f"人·{uid}")
        return out

    def _session_facts(self, event: AstrMessageEvent, cfg: dict[str, Any] | None = None) -> str:
        """把「现在几点、在跟谁、在哪种场合、距上一条多久」拼成一句自然话。

        时间这块为什么要插件补：AstrBot 只在开了「现实世界时间感知」时给一个绝对时刻，
        而对话历史（req.contexts）里**一条时间都没有** —— 模型无从判断「对方隔了多久才说话」。
        时区统一走 AstrBot 配置（容器时区常见是 UTC，直接用容器本地时间会差 8 小时）。
        """
        try:
            is_private = bool(event.is_private_chat())
        except Exception:
            is_private = False
        sender = ""
        fn = getattr(event, "get_sender_id", None)
        if callable(fn):
            try:
                sender = str(fn() or "")
            except Exception:
                sender = ""
        name_fn = getattr(event, "get_sender_name", None)
        try:
            name = (name_fn() if callable(name_fn) else "") or ""
        except Exception:
            name = ""
        who = "、".join(x for x in (f"昵称「{name}」" if name else "", f"QQ号 {sender}" if sender else "") if x)
        clock = self._clock_hint(event, cfg)
        if is_private:
            return f"【当前会话】私聊 · 对方是{who or '未知用户'}。{clock}"
        gid = ""
        try:
            gid = str(getattr(event.message_obj, "group_id", "") or "")
        except Exception:
            gid = ""
        try:
            gname = str(getattr(event.message_obj, "group_name", "") or "")
        except Exception:
            gname = ""
        place = f"群「{gname}」" if gname else (f"群 {gid}" if gid else "某个群")
        return f"【当前会话】群聊 · 在{place}里，消息来自{who or '未知用户'}。{clock}"

    def _clock_hint(self, event: AstrMessageEvent, cfg: dict[str, Any] | None = None) -> str:
        """「现在是 …（周X），距这个会话上一条消息 12 分钟」——纯本地计算，不发 DB 请求。"""
        from datetime import datetime

        tz = None
        try:
            tz = self.service.timezone(dict(cfg or {}))
        except Exception:                                   # noqa: BLE001 取不到时区就退回本机
            tz = None
        try:
            now_dt = datetime.now(tz) if tz is not None else datetime.now()
            clock = f"现在是 {now_dt.strftime('%Y-%m-%d %H:%M')}（周{'一二三四五六日'[now_dt.weekday()]}）"
        except Exception:                                   # noqa: BLE001 时间算不出来就不注入这段
            return ""
        gap = self._session_gap_text(self._sid(event), cfg)
        if gap:
            clock += f"，距这个会话上一条消息 {gap}"
        return clock + "。"

    def _session_gap_text(self, session_id: str, cfg: dict[str, Any] | None = None) -> str:
        """距本会话上一条消息多久（内存记账；不到 1 分钟就不说，免得变成噪音）。

        间隔超过 6 小时时补一个绝对时刻：只写「11 小时前」，模型会把昨晚的对话
        当成「今天早些时候」甚至「刚才」（真机痛点：分不清昨天和今天）。
        """
        slot = self._session_touch.get(str(session_id or "")) or {}
        last_ts = float(slot.get("last") or 0)
        prev_ts = float(slot.get("prev") or 0)
        if last_ts <= 0 or prev_ts <= 0:
            return ""
        gap = max(0.0, last_ts - prev_ts)
        if gap < 60:
            return ""
        text = actions.duration_text(gap)
        if gap >= 6 * 3600:
            try:
                from datetime import datetime

                tz = None
                try:
                    tz = self.service.timezone(dict(cfg or {}))
                except Exception:                       # noqa: BLE001 取不到时区就退回本机
                    tz = None
                d = datetime.fromtimestamp(prev_ts, tz) if tz is not None else datetime.fromtimestamp(prev_ts)
                text += f"（上一次是 {d.strftime('%m-%d %H:%M')}）"
            except Exception:                           # noqa: BLE001 算不出来就只给相对量
                pass
        return text

    def _touch_session(self, session_id: str) -> None:
        """记下本会话「这一条 / 上一条」的时间：注入里的「距上一条多久」靠它。"""
        sid = str(session_id or "")
        if not sid:
            return
        now_ts = time.time()
        slot = self._session_touch.get(sid)
        self._session_touch[sid] = {
            "last": now_ts,
            "prev": float((slot or {}).get("last") or 0),
        }
        if len(self._session_touch) > 512:                  # 防无界增长：丢最旧的 128 个会话
            for key, _ in sorted(
                self._session_touch.items(), key=lambda kv: float(kv[1].get("last") or 0)
            )[:128]:
                self._session_touch.pop(key, None)

    # ---------------- 工具返回后：跨会话动作留痕 ----------------
    @filter.on_llm_tool_respond()
    async def note_tool_actions(
        self, event: AstrMessageEvent, tool: Any, tool_args: dict | None, tool_result: Any
    ) -> None:
        """LLM 借工具在**别的会话**里干了什么，这里统一留痕（与具体插件无关）。

        为什么挂这个钩子：AstrBot 在每次工具调用返回后触发它（astr_agent_hooks.py:64-70，
        EventType.OnLLMToolRespondEvent），参数就是「工具 + 参数 + 返回值」，而且是**全局**的 ——
        第三方工具类插件（qzone_tools 的 run_wyc_tool → send_message）或 AstrBot 自带的
        send_message_to_user 代 bot 往外发消息时，本插件照样看得见。这是「不绑定任何具体插件」
        的关键：谁发的不重要，动作本身才是要记的东西。

        实测现场（2026-10-13）：群里 @ 它让它私聊发一句 → 紧接着在私聊里问「知道这句是谁让你发的吗」，
        它答「没人让我发」—— 因为那条动作只存在于群会话的上下文里，私聊那边什么都不知道。
        留痕之后，相关会话的下一轮请求会看到一行知会（见 _inject_context_inner）。

        边界：任何一步失败都只记日志，绝不抛回管线（工具调用是别人的回合，不能因本插件出错）。
        """
        try:
            await self._note_tool_actions_inner(event, tool, tool_args, tool_result)
        except asyncio.CancelledError:
            raise
        except Exception as exc:                            # noqa: BLE001
            logger.debug(f"[StarHeart] 跨会话动作留痕跳过：{exc}")

    async def _note_tool_actions_inner(
        self, event: AstrMessageEvent, tool: Any, tool_args: dict | None, tool_result: Any
    ) -> None:
        if not await self._ensure_ready(timeout=3):
            return
        cfg = await self.service.config()
        if not cfg.get("master_switch", True) or not cfg.get("cross_session_enabled", True):
            return
        session_id = self._sid(event)
        if not session_id or not self._is_trackable_session(session_id, cfg):
            return
        vis_kind, vis_ident = self.service.session_kind(session_id)
        vis_ok, _vis_why = self.service.visible(
            vis_kind, vis_ident, cfg, platform=self._session_head(session_id)
        )
        if not vis_ok:
            return                      # 名单外的会话 = 对 StarHeart 完全不存在（与记忆/语料同一道门）
        raw_name = str(getattr(tool, "name", "") or "")
        if not raw_name:
            return
        # 工具箱（qzone_tools 的 run_wyc_tool）要拆一层才看得到真正干活的工具名
        name, args, chain = actions.unwrap(raw_name, tool_args)
        found = actions.classify(name, args)
        if not found:
            if actions.looks_outbound(raw_name) or actions.looks_outbound(name):
                self._note_unknown_tool(chain or name, args)
            return
        ok = actions.action_ok(tool_result)
        if not ok:
            logger.debug(
                f"[StarHeart] 外发动作未确认成功（不记台账）：{chain} → {actions.result_text(tool_result)[:120]}"
            )
            return
        kind, ident = self.service.session_kind(session_id)
        name_fn = getattr(event, "get_sender_name", None)
        try:
            actor_name = str((name_fn() if callable(name_fn) else "") or "")
        except Exception:                                   # noqa: BLE001
            actor_name = ""
        entry = actions.build_entry(
            found, chain=chain, session_id=session_id, kind=kind, ident=ident,
            actor_id=self._sender_id(event), actor_name=actor_name,
        )
        if not entry:
            return                  # 没有目标 / 就是发给本会话自己 → 不记（正常回复不刷台账）
        self._action_ledger.append(entry)
        if len(self._action_ledger) > self.ACTION_LEDGER_MAX:
            del self._action_ledger[:64]
        logger.info(
            f"[StarHeart] 跨会话动作留痕：{chain} → {entry['kind']} {entry['target_kind'] or '?'} "
            f"{entry['target'] or '?'}（来源 {session_id}）"
        )
        if cfg.get("cross_session_memory", True):
            self._spawn(self._write_action_memory(entry), label="action-memory")

    def _note_unknown_tool(self, name: str, args: dict[str, Any]) -> None:
        """疑似外发工具但没认出来：每种只报一次 debug（方便发现新插件的发送工具，不刷屏）。"""
        key = str(name or "")
        if not key or key in self._action_unknown_seen:
            return
        if len(self._action_unknown_seen) > 64:
            self._action_unknown_seen.clear()
        self._action_unknown_seen.add(key)
        logger.debug(f"[StarHeart] 疑似外发工具未识别（未记台账）：{key} args={str(args)[:200]}")

    def _cross_session_note(self, event: AstrMessageEvent, cfg: dict[str, Any]) -> str:
        """本会话相关的跨会话动作 → 注入文本（空 = 不注入）。"""
        session_id = self._sid(event)
        if not session_id or not self._action_ledger:
            return ""
        kind, ident = self.service.session_kind(session_id)
        ttl_min = float(cfg.get("cross_session_ttl_min", 120) or 120)
        items = actions.collect_for(
            self._action_ledger, kind=kind, ident=ident, session_id=session_id,
            ttl_sec=max(60.0, ttl_min * 60.0),
        )
        if not items:
            return ""
        logger.info(f"[StarHeart] 跨会话知会已注入本轮请求（{len(items)} 条）")
        return actions.render_note(items, kind=kind, ident=ident, session_id=session_id)

    async def _write_action_memory(self, entry: dict[str, Any]) -> None:
        """把一条跨会话动作写成记忆：知会只在 TTL 内有效，长期回忆要靠记忆库。"""
        try:
            summary, content = actions.summarize(entry)
            if not summary:
                return
            persona_id = await self._inject_persona_id(
                self._persona_umo_for_session(str(entry.get("src_session") or ""))
                or str(entry.get("src_session") or ""),
                use_cache=True,
            )
            entities = [x for x in (str(entry.get("actor_name") or "").strip(),
                                    str(entry.get("actor_id") or "").strip()) if x]
            cues = [content[:24]]
            mid = await self.store.insert_memory(
                {
                    "summary": summary,
                    "content": content,
                    "atom_type": "episodic",
                    "strength": 0.6,
                    "poignancy": 5,
                    "source_mode": "action",
                    "source_ref": f"action:{entry.get('tool') or ''}",
                    "session_id": str(entry.get("src_session") or ""),
                    "persona_id": persona_id or "",
                    "tags": ["跨会话", "动作留痕"],
                    "entities": entities,
                    "cues": cues,
                    "evidence": f"{entry.get('tool') or ''} → {entry.get('target') or ''}",
                    "recalls": 0,
                    "status": "active",
                }
            )
            if not mid:
                return
            try:
                await self.store.link_memory(
                    mid, entities=entities or None, session_id=str(entry.get("src_session") or ""),
                    persona_id=persona_id or "", cues=cues,
                )
            except Exception as exc:                        # noqa: BLE001 建链失败不影响记忆本身
                logger.debug(f"[StarHeart] 动作记忆建链跳过：{exc}")
            cfg_emb = await self.service.config()
            model_name = self._embed_model_name(cfg_emb)
            vec = await self._embed(f"{summary} {content}".strip()[:512]) if model_name else None
            if vec and model_name:
                await self.store.set_embedding(mid, vec, model_name)
            logger.info(f"[StarHeart] 跨会话动作已写入记忆 {mid}：{summary[:60]}")
        except asyncio.CancelledError:
            raise
        except Exception as exc:                            # noqa: BLE001
            logger.debug(f"[StarHeart] 动作记忆写入跳过：{exc}")

    # ---------------- LLM 请求前：注入记忆与语气 ----------------
    @filter.on_llm_request()
    async def inject_context(self, event: AstrMessageEvent, req: ProviderRequest):
        """注入记忆 / 关系语气 / 表达习惯。

        整段包一层 try：任何一步失败都只是「本轮不注入」，绝不能把异常抛回 AstrBot 管线 ——
        否则用户会看到一条报错，甚至整轮对话被打断。observe_messages 一直有这个顶层兜底，
        这个钩子以前没有（只有末端 append 与表达习惯各自包了一层）。
        """
        try:
            await self._inject_context_inner(event, req)
        except Exception as exc:                            # noqa: BLE001
            logger.warning(f"[StarHeart] 注入失败（本轮不注入，不影响对话）：{exc}")

    async def _inject_context_inner(self, event: AstrMessageEvent, req: ProviderRequest):
        # 「参与对话的调用必须吃到 AstrBot 的提示词」：这个钩子在 AstrBot 组装完系统提示词之后触发
        # （internal.py: build_main_agent → OnLLMRequestEvent），所以这一刻 req.system_prompt 就是
        # 人格 + 技能 + 安全 + 全局前缀的完整版本。按会话留一份，主动开口直接复用，绝不自己拼人格。
        sess_cache = ""                                        # 取不到就空串：后面表达使用记账要用，不许因此中断注入
        try:
            sess_cache = self._sid(event)
            if sess_cache:
                # 只留最近 4 条（约 2 轮）：主动开口复用这段历史，留太长模型会接着自己
                # 上一轮的输出往下编（真机：把 8:27 那条主动消息原样复读到 10:16 的回复里）。
                self._agent_prompt_cache[sess_cache] = {
                    "ts": time.time(),
                    "system": str(getattr(req, "system_prompt", "") or ""),
                    "contexts": list(getattr(req, "contexts", []) or [])[-4:],
                }
                if len(self._agent_prompt_cache) > 64:      # 防无界增长：丢掉最旧的 16 个会话
                    for k, _ in sorted(
                        self._agent_prompt_cache.items(), key=lambda kv: float(kv[1].get("ts") or 0)
                    )[:16]:
                        self._agent_prompt_cache.pop(k, None)
        except Exception as exc:                            # noqa: BLE001 缓存失败绝不影响本轮对话
            logger.debug(f"[StarHeart] 缓存 AstrBot 系统提示词跳过：{exc}")
        if not await self._ensure_ready(timeout=3):
            return
        cfg = await self.service.config()
        if not cfg.get("master_switch", True):
            return
        # 人格解析的会话上下文：原始 UMO（不是归一化 sid）+ 本轮会话级人格 + 平台名。
        # 注入路径手上就有 req，照 AstrBot 官方调用点取全四个参数；捕获路径吃这里的缓存。
        raw_umo = str(getattr(event, "unified_msg_origin", "") or "")
        try:
            conv_persona_id: str | None = getattr(getattr(req, "conversation", None), "persona_id", None)
        except Exception:                                   # noqa: BLE001 老版本没有 conversation
            conv_persona_id = None
        try:
            plat_name = str(event.get_platform_name() or "")
        except Exception:                                   # noqa: BLE001
            plat_name = ""
        if raw_umo and sess_cache:
            self._capture_umo[sess_cache] = raw_umo
        parts: list[str] = []
        # 连发合并：把同一人窗口内连发的几条顶替成本轮 prompt（改 req.prompt 才有效）
        if cfg.get("burst_merge_enabled", True):
            try:
                await self._burst_merge_into(event, req)
            except Exception as exc:
                logger.debug(f"[StarHeart] 连发合并跳过：{exc}")
        # 会话上下文永远注入（群聊/私聊 + 对方昵称与 ID + 现在几点/距上一条多久），
        # 这不花钱且是 bot 知情的基础
        if cfg.get("session_context_enabled", True):
            parts.append(self._session_facts(event, cfg))
        # 主动消息知会：主动消息不再写回 AstrBot 历史，主模型靠这块知道自己说过什么。
        # 只带最近 N 条真发出去的（默认 24h/3 条）；对方问起时按原话承认，不要当成对方的话。
        if cfg.get("proactive_note_enabled", True):
            try:
                _pnote = await self._proactive_note(event, cfg)
                if _pnote:
                    parts.append(_pnote)
            except Exception as exc:                        # noqa: BLE001 知会失败不影响本轮
                logger.debug(f"[StarHeart] 主动消息知会跳过：{exc}")
        # 跨会话动作知会：别人让它在**别的会话**里发了什么（或它在本会话里被要求做过什么）——
        # 这些都不在本会话的对话历史里，不告知就会出现「问它谁让你发的，它答不知道」。
        if cfg.get("cross_session_enabled", True):
            try:
                note = self._cross_session_note(event, cfg)
                if note:
                    parts.append(note)
            except Exception as exc:                        # noqa: BLE001 知会失败不影响本轮注入
                logger.debug(f"[StarHeart] 跨会话知会跳过：{exc}")
        if cfg.get("memory_inject_enabled", True):
            top_k_cfg = cfg.get("memory_inject_top_k")
            top_k = int(8 if top_k_cfg is None else top_k_cfg)
            budget_cfg = cfg.get("memory_inject_budget_tokens")
            budget = int(1200 if budget_cfg is None else budget_cfg)
            persona_id = await self._inject_persona_id(
                raw_umo or sess_cache or self._sid(event),
                conversation_persona_id=conv_persona_id,
                platform_name=plat_name,
            )
            v2_ok = False
            if bool(cfg.get("v2_enabled", False)):
                # v2 召回：5 路候选 + 间隙法 + RRF + MMR（旧链路完全跳过，含重排）
                try:
                    from .core import memory_v2 as v2mem

                    turn_id = hashlib.md5(
                        f"{self._sid(event)}|{time.time()}".encode("utf-8")).hexdigest()[:16]
                    if not hasattr(self, "_v2_turn"):
                        self._v2_turn = {}
                    self._v2_turn[self._sid(event)] = turn_id
                    picked = await v2mem.recall_v2(self, event, cfg, top_k=top_k, persona_id=persona_id)
                    addresses = await v2mem.load_addresses(self.store, self._sender_id(event), persona_id)
                    block = v2mem.render_block(
                        picked, cfg, now_ts=time.time(), tz=self.service._tzinfo(cfg),
                        addresses=addresses, today_label=self.service.now_text(cfg),
                    )
                    await v2mem.log_shown(
                        self, turn_id, self._sid(event),
                        "" if "group" in str(self._sid(event)).lower() else self._sender_id(event),
                        picked, "recall",
                    )
                    if block:
                        parts.append(block)
                    v2_ok = True
                except Exception as exc:                    # noqa: BLE001 v2 失败必须退回旧链路
                    logger.warning(f"[StarHeart] v2 召回失败，本轮回退旧链路：{exc}")
            if not v2_ok:
                picked = await self._recall_picked(event, top_k, persona_id)
                # 「结果重排」：配了重排模型就先按它重新定序（失败/未配 → 原序）
                picked = await self._rerank_memories(event.message_str or "", picked, top_k)
                # 排障用：把本轮真正注入的 id 记下来（权重调完要能核对「那条到底进没进」）
                logger.debug(
                    f"[StarHeart] 记忆召回：{len(picked)} 条 → {[str(m.get('id')) for m in picked[:max(1, int(top_k))]]}"
                )
                # 被提及 → 刷新遗忘计时；归档中的当场拉回正常记忆并补回强度。
                # 只认真实命中：兜底（最近 N 条）不算被提及，否则最新几条永远霸榜（P0-5）。
                for m in picked[: max(1, int(top_k))]:
                    if str(m.get("_hit_kind") or "") == "fallback":
                        continue
                    try:
                        if await self.store.touch_memory(m.get("id")):
                            logger.info(f"[StarHeart] 记忆 {m.get('id')} 被提及，已自动拉回正常记忆")
                    except Exception as exc:                 # noqa: BLE001 计时失败不许影响注入
                        logger.debug(f"[StarHeart] 记忆计时刷新跳过：{exc}")
                block = engine.build_memory_context(
                    picked, budget_chars=budget,
                    stamp_fn=self.service.abs_stamp(cfg), today_label=self.service.now_text(cfg),
                )
                if block:
                    parts.append(block)
        if cfg.get("favour_enabled", True):
            fav = {f["user_id"]: f for f in await self.store.list_favours()}
            sender = self._sender_id(event)
            zone = fav.get(sender, {}).get("zone", "NEUTRAL")
            tone_overrides: dict = {}   # 关系区间语气线不可配（原来那个键从没有写入方，已删）
            parts.append(f"（当前关系温度：{engine.tone_line(zone, tone_overrides)}）")
            if cfg.get("favour_settle_mode", "tag") == "tag":
                parts.append(FAVOUR_HINT)
        # 记忆由本插件自动处理：明确告诉模型不要自己去找记忆工具。
        # 背景：qzone_tools 的记忆功能被用户关掉后，模型仍会反复调 run_wyc_tool 搜「记忆」，
        # 结果全是空转 —— 白烧 token、偶发 `run_wyc_tool() missing 'tool_args'` TypeError、刷 WARN。
        # 只在本来就有注入内容时才追加，避免"没内容也硬塞一行"破坏空注入的早退。
        if parts:
            parts.append("（记忆由系统自动处理，不用调用任何记忆类工具去搜或存）")
        # 表达块：挂在人格之后、system 最末尾（不是 user content）。
        # 学到东西就整块换成学到的原话；没学到就是人格里搬出来的默认规则。
        sys_tail: list[str] = []
        try:
            from .core import style_memory as style_mod

            _reg = "group" if "group" in str(self._sid(event)).lower() else "private"
            _pid_style = await self._inject_persona_id(
                raw_umo or sess_cache or self._sid(event),
                conversation_persona_id=conv_persona_id,
                platform_name=plat_name,
                use_cache=True,
            )
            _sblock = await style_mod.expression_block(
                self.store, cfg, _pid_style or "", _reg, event.message_str or ""
            )
            if _sblock:
                sys_tail.append(_sblock)
        except Exception as exc:                            # noqa: BLE001 表达块失败不影响回复
            logger.debug(f"[StarHeart] 表达块注入跳过：{exc}")
        if not parts and not sys_tail:
            return
        # 表达学习真生效：把学到的说话习惯注入（原实现只提取不使用 = 假功能）
        if cfg.get("expression_enabled", True):
            try:
                # 注入名额（勘误 A1：output_expr_max_inject 已建键但从未接线；0 = 不注入）
                _cap = int(cfg.get("output_expr_max_inject", 5) or 0)
            except (TypeError, ValueError):
                _cap = 5
            if _cap > 0:
                try:
                    _pid = await self._inject_persona_id(
                        raw_umo or sess_cache or self._sid(event),
                        conversation_persona_id=conv_persona_id,
                        platform_name=plat_name,
                        use_cache=True,
                    )
                    hints, expr_ids = await self.service.habit_hints_detail(
                        persona_id=_pid, limit=_cap
                    )
                    if hints:
                        parts.append(hints)
                        self._spawn(self._habit_used_bg(expr_ids, sess_cache, _pid), label="habit-used")
                except Exception as exc:
                    logger.debug(f"[StarHeart] 表达习惯注入跳过：{exc}")
        # 黑话（S5-1/S5-2）：只做理解、不进回复 —— 同一机械匹配器负责命中记账 + 理解注入
        if cfg.get("jargon_enabled", True):
            try:
                _jmsg = str(getattr(event, "message_str", "") or "")
                if _jmsg:
                    _jblock, _jmatched = await self.service.jargon_context_block(
                        _jmsg, self._jargon_scopes(event), sess_cache
                    )
                    if _jblock:
                        parts.append(_jblock)
            except Exception as exc:                        # noqa: BLE001 黑话失败只影响本轮不注入
                logger.debug(f"[StarHeart] 黑话理解注入跳过：{exc}")
        # 输出分段协议：只有开了插件的分段发送才教 —— 否则空行没有意义、白占提示词。
        # 这条是「让 bot 自己控制分段」的关键：bot 空一行 = 它想分一条发，split_message() 就照办。
        if bool(cfg.get("output_split_messages", False)):
            sys_tail.append(
                "【输出方式】默认一条发完，一句话能说完就别拆。真的有两件事要分开说，才空一行，"
                "一次最多两三条；短回复不用分。空行只表示「换一条发」，不是让你列备选。"
            )
        # 撤回知会：把对方最近撤回过的消息**一次**告诉 bot（下一轮它就知道"那条已经不算数"了），
        # 然后清掉。依据：AstrBot 的撤回通知不进对话历史，bot 本来永远不知情。
        try:
            _rsid = self._sid(event)
            _pending = (getattr(self, "_recall_notes", {}) or {}).pop(_rsid, None) if _rsid else None
            if _pending:
                _lines = []
                for _n in list(_pending)[-3:]:
                    _what = _n.get("text") or (("图片 %s 张" % _n.get("imgs")) if _n.get("imgs") else "（内容未留存）")
                    _lines.append("- %s 撤回了一条消息：「%s」" % (_n.get("who") or "对方", _what))
                parts.append(
                    "【撤回知会】刚刚有人撤回了消息 —— 那些内容**已经不算数**，不要再当有效上下文继续聊：\n"
                    + "\n".join(_lines)
                )
                # 打日志留痕：这条日志出现 = 知会真的进了本轮请求（排查"bot 为什么不知道"时看它）
                logger.info(f"[StarHeart] 撤回知会已注入本轮请求（{len(_lines)} 条）")
        except Exception as exc:                                # noqa: BLE001 知会失败不影响注入
            logger.debug(f"[StarHeart] 撤回知会注入跳过：{exc}")
        if sys_tail:
            _tail = "\n".join(sys_tail).strip()
            if _tail:
                try:
                    _old_sys = str(getattr(req, "system_prompt", "") or "")
                    req.system_prompt = (_old_sys.rstrip() + "\n\n【表达】\n" + _tail + "\n【/表达】").lstrip()
                    logger.debug(f"[StarHeart] 表达块已挂到人格末尾（{len(_tail)} 字）")
                except Exception as exc:                        # noqa: BLE001 挂不上不影响回复
                    logger.warning(f"[StarHeart] 表达块挂 system 失败：{exc}")
        if not parts:
            return
        try:
            text = "\n".join(parts)
            target = getattr(req, "extra_user_content_parts", None)
            if target is None:
                logger.debug("[StarHeart] 当前请求没有 extra_user_content_parts，跳过注入")
                return
            target.append(TextPart(text=text).mark_as_temp())
            await self.store.log_tokens(
                {"ts": time.time(), "task": "est:inject", "model": "", "in_tokens": len(text) // 4, "out_tokens": 0}
            )
        except Exception as exc:
            logger.warning(f"[StarHeart] 上下文注入失败：{exc}")

    # ---------------- LLM 响应后：好感结算 + 记忆捕获 ----------------
    @filter.on_llm_response()
    async def settle_favour(self, event: AstrMessageEvent, resp: LLMResponse):
        if not await self._ensure_ready(timeout=3):
            return
        # 平台边界统一判定：名单外/未知平台的会话不结算好感、不落语料、不补记忆
        if not self._is_trackable_session(self._sid(event),
                                         await self.service.config()):
            return
        # token 消耗统计无条件先记（原实现只在出现好感标签时才记，导致面板消耗偏低）
        await self._log_usage(resp, event)
        chain = getattr(resp, "result_chain", None)
        text = ""
        if chain is not None and getattr(chain, "chain", None):
            text = "".join(getattr(c, "text", "") for c in chain.chain if isinstance(c, Plain))
        # 记忆捕获不依赖好感：bot 回应时把 bot 文本补进缓冲（用户侧已在 observe 入过缓冲）
        if text:
            try:
                await self._record_corpus(event, text, is_bot=True)
            except Exception as exc:
                logger.debug(f"[StarHeart] bot 语料落库跳过：{exc}")
            try:
                await self._burst_clear(event)  # 这一轮回应完了，下次连发重新计
            except Exception as exc:
                logger.debug(f"[StarHeart] 连发缓冲清理跳过：{exc}")
            try:
                await self._capture_with_reply(
                    event, (event.message_str or "").strip(), text, self._collect_image_urls(event)
                )
            except Exception as exc:
                logger.debug(f"[StarHeart] 记忆捕获未触发：{exc}")
            # v2 归因：这轮回复到底用没用注入的记忆（shown 与 cited 分开记）
            try:
                _turn = (getattr(self, "_v2_turn", {}) or {}).pop(self._sid(event), None)
                if _turn:
                    from .core import memory_v2 as v2mem

                    await v2mem.attribute_reply(self, _turn, text, "recall")
            except Exception as exc:                        # noqa: BLE001 归因失败不影响回复
                logger.debug(f"[StarHeart] v2 归因跳过：{exc}")
        cfg = await self.service.config()
        if not cfg.get("master_switch", True) or not cfg.get("favour_enabled", True):
            return
        if not text:
            return
        cleaned, delta = engine.extract_favour_tag(text)
        if delta is None:
            # 「需要标签才结算」关掉时：没有好感标签也按"主动回应"给 +1（仍受好感日上限约束）
            if bool(cfg.get("favour_tag_required", True)):
                return
            delta = 1.0
        sender = self._sender_id(event)
        if not sender:
            return
        name = (
            getattr(event, "get_sender_name", lambda: "")()
            if callable(getattr(event, "get_sender_name", None))
            else ""
        )
        if delta == 0:
            # 无变化也建档：好感面板要有档案可见（只建一次，不重复结算）
            await self.service.favour_adjust(
                sender, 0, reason="好感持平（建档）", scope="both", name=name
            )
        else:
            await self.service.favour_adjust(
                sender, delta, reason="主模型尾行自报", scope="both", name=name
            )

    async def _log_usage(self, resp: LLMResponse, event: AstrMessageEvent) -> None:
        raw = getattr(resp, "raw_completion", None)
        usage = getattr(raw, "usage", None)
        if usage is None:
            return
        try:
            await self.store.log_tokens(
                {
                    "ts": time.time(),
                    "task": "chat",
                    "model": str(getattr(raw, "model", "") or ""),
                    "in_tokens": int(getattr(usage, "prompt_tokens", 0) or 0),
                    "out_tokens": int(getattr(usage, "completion_tokens", 0) or 0),
                    "cached": int(getattr(usage, "prompt_cache_hit_tokens", 0) or 0),
                }
            )
        except Exception:
            pass

    # ---------------- 发送前：输出拟人 ----------------
    async def _habit_auto_tick(self, cfg: dict[str, Any]) -> None:
        """表达学习自动提取：未学语料攒够 expression_auto_min_corpus 条就学一次。

        关掉 expression_enabled 或把门槛设 0，就退回纯手动触发（面板按钮）。
        """
        try:
            if not bool(cfg.get("expression_enabled", True)):
                return
            need = int(cfg.get("expression_auto_min_corpus", 20) or 0)
            if need <= 0:
                return
            store = getattr(self.service, "store", None)
            if store is None:
                return
            rows = await store.list_corpus(limit=need, is_bot=0, unused_only=True)
            if len(rows) < need:
                return
            res = await self.service.habit_extract()
            logger.info(
                f"[StarHeart] 表达学习自动提取：待学语料 {len(rows)} 条 → 新学习惯 {int(res.get('saved', 0))} 条"
            )
        except Exception as exc:                            # noqa: BLE001 后台任务异常不允许拖垮循环
            logger.warning(f"[StarHeart] 表达学习自动提取失败（不影响其它任务）：{exc}")

    async def _style_auto_tick(self, cfg: dict[str, Any]) -> None:
        """语言学习：新记忆够量就跑一轮（从记忆原对话学，事件触发）。"""
        logger.info(f"[StarHeart] 语言学习 tick（enabled={cfg.get('expression_style_enabled')}）")
        if not bool(cfg.get("expression_style_enabled", False)):
            return
        try:
            from .core import style_memory as style_mod

            pid = await self.service._active_pid()
            res = await style_mod.run_learn(self, cfg, pid or "")
            if res.get("ok"):
                logger.info(f"[StarHeart] 语言学习自动学习：{res}")
            else:
                logger.info(f"[StarHeart] 语言学习跳过：{res.get('reason')} / 新记忆={res.get('fresh')}")
        except Exception as exc:                            # noqa: BLE001
            logger.warning(f"[StarHeart] 语言学习自动学习失败：{exc}")

    async def _jargon_auto_tick(self, cfg: dict[str, Any]) -> None:
        """黑话含义推断的自动敲钟（S5-1）：到点敲一次，配额/门槛全部由 service 按配置判定。

        每日 ≤jargon_infer_daily_batches 批（默认 1，拍-16）；关 jargon_enabled 或把批数设 0
        即退回纯手动（面板仍可确认/否定/改释义）。
        """
        try:
            if not bool(cfg.get("jargon_enabled", True)):
                return
            res = await self.service.jargon_infer_batch()
            if int(res.get("created") or 0) > 0 or res.get("skipped_reason") in ("model_error",):
                logger.info(f"[StarHeart] 黑话推断：{str(res)[:200]}")
        except Exception as exc:                            # noqa: BLE001 后台任务异常不允许拖垮循环
            logger.warning(f"[StarHeart] 黑话推断失败（不影响其它任务）：{exc}")

    async def _habit_used_bg(
        self, expr_ids: list[int] | None = None, session_id: str = "", persona_id: str = ""
    ) -> None:
        """习惯注入后的计数（后台跑，不阻塞本轮请求）+ 表达使用流水（hf_expr_use_log）。"""
        try:
            await self.service.habit_mark_used(persona_id=persona_id, ids=expr_ids)
        except Exception as exc:
            logger.debug(f"[StarHeart] 习惯计数失败：{exc}")
        for eid in (expr_ids or []):
            try:
                await self.store.log_expr_use({"expr_id": int(eid), "session_id": session_id})
            except Exception as exc:                        # noqa: BLE001 记账不许影响本轮对话
                logger.warning(f"[StarHeart] 表达使用记账失败（不影响本轮）：{exc}")

    async def _log_first_seg(self, event: AstrMessageEvent, chain: list[Any], seg_total: int = 1) -> None:
        """首条（框架管线直接发出）记账：内容此刻已确定，但发送结果不在本插件手里。

        因此落 kind='first_seg'、emit_ok=NULL（未确认）—— 不冒充「已确认发出」，也不当成失败。
        seg_total 取本轮实际总段数（含首条），否则流水里 seg_index/seg_total 对不上真机气泡数。
        """
        try:
            text = "\n".join(str(getattr(c, "text", "") or "") for c in chain if isinstance(c, Plain)).strip()
            if not text:
                return
            await self.store.log_sent({
                "session_id": self._sid(event),
                "kind": "first_seg",
                "text": text,
                "seg_index": 0,
                "seg_total": max(1, int(seg_total or 1)),
                "emit_ok": None,
                "trigger": "首条",
            })
            try:
                # first_seg = 发出但拿不到回执（emit_ok=NULL）—— 照样参与子串命中判定（拍-12）
                await self.store.reconcile_expr_use(str(self._sid(event)), text, sent_ok=None)
            except Exception as exc:                        # noqa: BLE001 对账不许影响发送
                logger.warning(f"[StarHeart] 表达出站对账失败（首条）：{exc}")

        except Exception as exc:                            # noqa: BLE001 记账不许影响发送
            logger.warning(f"[StarHeart] 首条记账失败（不影响发送）：{exc}")

    @filter.on_decorating_result()
    async def humanize_output(self, event: AstrMessageEvent):
        if not await self._ensure_ready(timeout=3):
            return
        cfg = await self.service.config()
        result = event.get_result()
        chain = getattr(result, "chain", None)
        try:
            logger.info(f"[StarHeart] 拟人钩子：chain={'空' if not chain else len(chain)} 组件={[type(c).__name__ for c in (chain or [])][:4]}")
        except Exception:                                   # noqa: BLE001
            pass
        if not chain:
            return
        # 只收拾 LLM 自己产出的文本：/hf 命令、报错、其他插件的回复不动
        ctype = getattr(result, "result_content_type", None)
        if ctype not in (ResultContentType.LLM_RESULT, ResultContentType.STREAMING_RESULT,
                         ResultContentType.STREAMING_FINISH):
            return
        if not cfg.get("master_switch", True):
            return
        if not cfg.get("output_enabled", True):
            # 关掉输出拟人 ≠ 可以把好感标记发给用户：只剥离标记，其它修饰全跳过
            for comp in chain:
                if isinstance(comp, Plain) and comp.text:
                    comp.text = engine.extract_favour_tag(comp.text)[0]
            await self._log_first_seg(event, chain)
            return
        # 只收拾自己产出的 LLM 文本：不动用户/其他插件的消息（图片、文件等非文本组件天然跳过）
        typo_mode = str(cfg.get("output_typo_mode") or "off")
        # 分词前原文：tidy_output 会把空行压掉，而「空行 = 这里分一条」是 bot 自己控制分段的协议，
        # 必须在压空行之前留一份原文下来给分段判定用（否则协议形同虚设，只有标点兜底还可能不触发）。
        raw_parts: list[str] = []
        for comp in chain:
            if isinstance(comp, Plain) and comp.text:
                cleaned = engine.extract_favour_tag(comp.text)[0]  # 兜底：不让好感标记漏出去
                raw_parts.append(cleaned)
                comp.text = engine.apply_typo(engine.tidy_output(cleaned), typo_mode, cfg.get("output_typo_rate"))
        raw_body = "\n".join(raw_parts).strip()
        # ---- 输出拟人（分段 / 表情包 / 语音）----
        # 顺序说明：AstrBot 没有"发送之后"的钩子，本钩子在**发之前**；所以首条仍交给框架正常发出，
        # 其余内容由后台任务按"打字时长"依次补发 —— 正好等价于真人「先说一句 → 接着打字」。
        comps = list(chain)
        # 只对**文本组件**做拟人；链里带引用(Reply)/At/图片等非文本组件时不再一票否决。
        # 原先写 all(isinstance(c, Plain)...)：用户开着「回复时引用发送人消息」时链首会多一个
        # Reply 组件 ⇒ 恒为假 ⇒ 分段与打字延迟被整段跳过（真机日志：一条「输出分段」都没有）。
        plains = [c for c in comps if isinstance(c, Plain)]
        if plains:
            body = "\n".join((c.text or "") for c in plains).strip()
            if body:
                _total = self._plan_humanized_followups(event, plains, body, cfg,
                                                        raw_body=raw_body, typo_mode=typo_mode)
                # 计划落定后记首条：此时 comps[0] 已去掉表情标记/只留第一段，正是框架将要发出的内容
                await self._log_first_seg(event, chain, seg_total=_total or 1)

    # ---------------- 输出拟人：把"首条之后要补发什么"计划出来 ----------------
    def _plan_humanized_followups(self, event: AstrMessageEvent, comps: list[Any], body: str,
                                  cfg: dict[str, Any], raw_body: str = "", typo_mode: str = "off") -> None:
        """判定补发内容（分段的其余部分 / 表情包 / 语音）并丢给后台。

        任何一步失败都只记日志：首条已经交给框架了，绝不允许这里把它搞没。
        """
        try:
            session = self._sid(event)
            if not session:
                return
            try:
                _has_blank = bool(re.search(r"\n\s*\n", raw_body or body or ""))
                logger.info(
                    f"[StarHeart] 拟人计划：session={'有' if session else '空'} 正文长={len(body or '')} "
                    f"分段开关={bool(cfg.get('output_split_messages', False))} 空行={'有' if _has_blank else '无'} "
                    f"延迟开关={bool(cfg.get('output_typing_delay', True))}"
                )
            except Exception:                               # noqa: BLE001 打点失败不影响拟人
                pass
            plan: list[tuple[str, str]] = []
            text = body
            # ① 表情包：回复里带 [表情] 标记 → 从「表情包槽位」指定的目录随机发一张
            sticker_dir = str(cfg.get("sticker_slot") or "off").strip()
            if STICKER_MARK.search(text) and sticker_dir.lower() not in ("off", "none", ""):
                text = STICKER_MARK.sub("", text).strip()
                comps[0].text = text
                pic = self._pick_sticker(sticker_dir)
                if pic:
                    plan.append(("sticker", pic))
                else:
                    logger.warning(f"[StarHeart] 表情包槽位没取到图片（目录：{sticker_dir}）")
            # ② 语音：把回复文本交给 TTS 合成后补发一条语音
            voice_slot = str(cfg.get("voice_slot") or "off").strip()
            if text and voice_slot.lower() not in ("off", "none", ""):
                plan.append(("voice", text))
            # ③ 分段：分不分条由 bot 自己决定（engine.plan_segments，含「整条 <24 字不拆」的克制闸）。
            #    以前还有一条「语义切分」兜底 —— 模型没打算分条，机器也按标点硬切成两条，
            #    这就是「一句话敷衍也被拆」的来源；已删除，没有空行就整条发。
            _cap = self._split_cap(cfg)
            _segs, _how = engine.plan_segments(raw_body, text, cap=_cap)
            if _how == "空行协议":
                _raw_blocks = [b for b in re.split(r"\n\s*\n", raw_body or "") if b.strip()]
                if len(_raw_blocks) > _cap:
                    logger.info(f"[StarHeart] 输出分段收口：空行协议超上限，合并到 {_cap} 段")
            if bool(cfg.get("output_split_messages", False)) and len(_segs) > 1:
                _segs = [engine.apply_typo(engine.tidy_output(s), typo_mode, cfg.get("output_typo_rate")) for s in _segs]
                comps[0].text = _segs[0]
                for c in comps[1:]:
                    c.text = ""
                plan = [("text", seg) for seg in _segs[1:]] + plan
                logger.info(f"[StarHeart] 输出分段：首条 + 补发 {len(_segs) - 1} 条（{_how}；上限 {_cap}）")
            if plan:
                # 记账口径：首条已由管线发出（index 0），补发从 1 起；
                # 总段数 = 首条 + 文本补发 + 表情/语音（原实现漏了首条，seg_index 还会重复记 0）。
                _extras = sum(1 for _k, _v in plan if _k != "text")
                _total = (len(_segs) if len(_segs) > 1 else 1) + _extras
                self._spawn(self._send_followups(session, plan, cfg, seg_offset=1, seg_total=_total),
                            label="output-followups")
                return _total
        except Exception as exc:                            # noqa: BLE001 首条不受影响
            logger.warning(f"[StarHeart] 输出拟人计划失败（首条不受影响）：{exc}")
        return 1

    def _typing_delay(self, text: str, cfg: dict[str, Any]) -> float:
        """打字时长 = 每字秒数 × 字数，封顶 output_typing_max；关掉就是 0（立刻发）。"""
        if not bool(cfg.get("output_typing_delay", True)):
            return 0.0
        try:
            per = float(cfg.get("output_typing_per_char", 0.3) or 0)
            cap = float(cfg.get("output_typing_max", 6.0) or 0)
        except Exception:
            return 0.0
        return max(0.0, min(cap if cap > 0 else 0.0, per * len(text or "")))

    def _pick_sticker(self, folder: str) -> str:
        """从「表情包槽位」目录里随机取一张图（png/jpg/jpeg/gif/webp）。取不到就返回空串。"""
        try:
            pic_dir = Path(str(folder)).expanduser()
            if not pic_dir.is_dir():
                logger.warning(f"[StarHeart] 表情包目录不存在：{folder}")
                return ""
            imgs = [x for x in pic_dir.iterdir() if x.suffix.lower() in (".png", ".jpg", ".jpeg", ".gif", ".webp")]
            if not imgs:
                logger.warning(f"[StarHeart] 表情包目录里没有图片：{folder}")
                return ""
            return str(random.choice(imgs))
        except Exception as exc:                            # noqa: BLE001
            logger.warning(f"[StarHeart] 挑表情包失败：{exc}")
            return ""

    async def _send_voice(self, session: str, text: str, cfg: dict[str, Any]) -> bool:
        """「语音槽位」：把文本交给 TTS 合成后补发一条语音。槽位值 = TTS provider id，或 session（跟随会话）。"""
        slot = str(cfg.get("voice_slot") or "off").strip()
        provider = None
        try:
            if slot.lower() in ("session", "auto", "跟随会话"):
                provider = await self.context.get_using_tts_provider_async()
            else:
                provider = self.context.get_provider_by_id(slot)
        except Exception as exc:                            # noqa: BLE001
            logger.warning(f"[StarHeart] 语音槽位取 provider 失败（{slot}）：{exc}")
            return False
        if provider is None or not hasattr(provider, "get_audio"):
            logger.warning(f"[StarHeart] 语音槽位不可用（{slot}）：找不到 TTS provider")
            return False
        audio = await provider.get_audio(text[:300])
        if not audio:
            logger.warning("[StarHeart] TTS 返回空音频，跳过语音补发")
            return False
        ok = await self.context.send_message(session, MessageChain([Record(file=str(audio))]))
        logger.debug(f"[StarHeart] 语音已补发 → {session}（{bool(ok)}）")
        return bool(ok)

    async def _send_followups(self, session: str, plan: list[tuple[str, str]], cfg: dict[str, Any],
                              *, seg_offset: int = 0, seg_total: int = 0) -> None:
        """后台按打字节奏补发（分段文本 / 表情包 / 语音）。单条失败只记日志，不影响其它条目。

        seg_offset / seg_total：回复路径从 0 起算「补发第几条」；主动通道首条已单独发出，
        传 seg_offset=1、seg_total=总段数，hf_sent_log 的 seg_index/seg_total 才能和真机对齐。
        """
        total = int(seg_total or max(1, len(plan)))
        for i, (kind, payload) in enumerate(plan):
            seg_i = seg_offset + i
            delay = self._typing_delay(payload, cfg) if kind == "text" else 0.5
            if delay > 0:
                await asyncio.sleep(delay)
            if kind == "text":
                # 分段文本走 _send_proactive（它内部统一落 hf_sent_log），这里不重复记账
                try:
                    await self._send_proactive(session, payload, tag="分段补发", delay_ms=int(delay * 1000),
                                               kind="resend", seg_index=seg_i, seg_total=total)
                except Exception as exc:                    # noqa: BLE001
                    logger.warning(f"[StarHeart] 补发失败（{kind}）：{exc}")
                continue
            ok = False
            try:
                if kind == "sticker":
                    ok = bool(await self.context.send_message(session, MessageChain([Image(file=payload)])))
                    logger.debug(f"[StarHeart] 表情包已补发 → {session}（{bool(ok)}）")
                elif kind == "voice":
                    ok = await self._send_voice(session, payload, cfg)
            except Exception as exc:                        # noqa: BLE001
                logger.warning(f"[StarHeart] 补发失败（{kind}）：{exc}")
            # 出站记账（表情包 / 语音）：失败只记 WARNING，不影响其它条目
            try:
                await self.store.log_sent({
                    "session_id": session,
                    "kind": "sticker" if kind == "sticker" else "voice",
                    "text": payload if kind == "voice" else "",
                    "delay_ms": int(delay * 1000),
                    "seg_index": seg_i,
                    "seg_total": total,
                    "emit_ok": 1 if ok else 0,
                    "trigger": "表情包" if kind == "sticker" else "语音",
                })
            except Exception as exc:                        # noqa: BLE001
                logger.warning(f"[StarHeart] 补发记账失败（{kind}）：{exc}")
            # 出站对账（S4-3）：这条路线也要判命中（文本段走 _send_proactive，已自带一次）
            try:
                await self.store.reconcile_expr_use(
                    str(session or ""), str(payload if kind == "voice" else "") or "",
                    sent_ok=bool(ok) if kind == "voice" else True,
                )
            except Exception as exc:                        # noqa: BLE001
                logger.warning(f"[StarHeart] 表达出站对账失败（补发）：{exc}")


    # ---------------- 指令组 ----------------
    #
    # 为什么这么写（真机踩过的坑）：AstrBot 插件页只列**子指令名**（status / import / link），
    # 不显示组名前缀，于是用户很自然地发 `/status` —— 日志里就真的发生过：
    # 那条消息被当成普通聊天走了 LLM，插件毫无反应。所以这里做三件事：
    #   1) 提供 /hf help，一屏给全用法（含「必须带 /hf」这句），且它不依赖任何服务，永远能用；
    #   2) 每个子指令整体包 try/except：出错必须回一句人话，静默 = 用户以为坏掉了；
    #   3) 装饰器顺序按 AstrBot 官方文档：permission_type 放在最外层。

    USAGE: tuple[tuple[str, str], ...] = (
        ("/hf help", "看这一屏用法"),
        ("/hf status", "运行概览（记忆 / 门控 / 导入）"),
        ("/hf health", "自检（初始化、向量覆盖、缺失模型）"),
        ("/hf sid", "当前会话 ID、用户 ID、人格、是否管理员"),
        ("/hf on | off", "总开关（关掉后StarHeart彻底不介入）"),
        ("/hf recall 关键词", "混合召回测试（关键词 + 向量 → RRF）"),
        ("/hf mem <记忆ID>", "看某条记忆的详情"),
        ("/hf mute <记忆ID> [off]", "别再提这条记忆（冷处理：不进召回与主动开口；off 恢复）"),
        ("/hf gate [条数]", "最近的门控决策"),
        ("/hf fav [用户ID]", "好感概览；加 /hf fav set <用户ID> <增减> [理由] 可手调"),
        ("/hf expr", "表达习惯概览（学来的说法 + 最近轨迹）"),
        ("/hf msg", "消息事件汇总（开口 / 跳过 / 关系档位）"),
        ("/hf vec", "向量库代数与覆盖率"),
        ("/hf graph", "关系图谱统计"),
        ("/hf profile", "画像概览（人物 / 群聊）"),
        ("/hf persona", "人格列表与当前人格"),
        ("/hf forget [run]", "遗忘评估预览（加 run 真正执行一次）"),
        ("/hf conf", "关键配置概览"),
        ("/hf import", "导入 astrbot_plugin_livingmemory 的记忆"),
        ("/hf link", "重建记忆关系链（图谱只有孤点时用）"),
    )

    @filter.command_group("hf")
    def hf(self):
        """StarHeart · 管理指令组（子指令必须带 hf 前缀，例如 /hf status；全部用法：/hf help）"""
        pass

    def _cmd_arg(self, event: AstrMessageEvent, n: int = 2) -> str:
        """取指令第 n 段之后的原文。

        为什么不用 AstrBot 的类型化参数：关键词/理由里带空格很常见，直接读原文最稳。
        """
        parts = str(getattr(event, "message_str", "") or "").strip().split()
        return " ".join(parts[n:]).strip()

    async def _cmd_ready(self) -> bool:
        return await self._ensure_ready()

    # help 故意不限管理员：它是唯一的用法发现入口，卡权限就又变成"用户不知道怎么用"
    @hf.command("help")
    async def hf_help(self, event: AstrMessageEvent):
        """查看 StarHeart 全部指令用法"""
        lines = ["StarHeart · 指令用法", "注意：子指令要带 hf 前缀，直接发 /status 是不生效的。"]
        for usage, desc in self.USAGE:
            lines.append(f"{usage} — {desc}")
        lines.append("图形面板：AstrBot WebUI → 插件 → StarHeart → StarHeart面板")
        yield event.plain_result("\n".join(lines))

    @filter.permission_type(PermissionType.ADMIN)
    @hf.command("status")
    async def hf_status(self, event: AstrMessageEvent):
        """查看StarHeart运行概览"""
        try:
            if not await self._cmd_ready():
                yield event.plain_result("StarHeart尚未初始化完成，请稍后重试（面板「消息与事件」里能看到初始化进度）")
                return
            stats = await self.store.stats()
            cfg = await self.service.config()
            detect = await self.service.import_detect()
            lines = [
                "StarHeart 状态",
                f"总开关：{'开' if cfg.get('master_switch', True) else '关'}",
                f"记忆 {stats['memories']} 条 · 实体 {stats['entities']} · 关系边 {stats['edges']}",
                f"门控：{'开' if cfg['gate_enabled'] else '关'}{'（影子模式）' if cfg['gate_shadow'] else ''} · 阈值 {cfg['gate_threshold']}",
                f"已评估 {stats['gate_total']} 次 · 触发 {stats['gate_reply']} 次",
                f"LivingMemory：{'已装' if detect['plugin_dir_exists'] else '未装'} · 可导入 {detect['documents'] + detect['atoms']} 条 · 已导入 {detect['already_imported']}",
                "更多：/hf health · 面板：AstrBot WebUI → 插件 → StarHeart",
            ]
            yield event.plain_result("\n".join(lines))
        except Exception as exc:                            # noqa: BLE001 指令不能因为内部异常变哑巴
            logger.warning(f"[StarHeart] /hf status 失败：{exc}")
            yield event.plain_result(f"/hf status 执行出错：{str(exc)[:120]}")

    @filter.permission_type(PermissionType.ADMIN)
    @hf.command("health")
    async def hf_health(self, event: AstrMessageEvent):
        """自检：初始化、向量覆盖、缺失模型"""
        try:
            if not await self._cmd_ready():
                yield event.plain_result("StarHeart尚未初始化完成，请稍后重试")
                return
            h = await self.service.health()
            cov = await self.service.embedding_coverage()   # store 版没有 gen，恒显示"未知"
            cfg = await self.service.config()
            lines = ["StarHeart 自检"]
            # 不猜字段名：service.health() 返回什么就列什么，改后端也不会让这条指令变空洞
            for key, val in list(h.items())[:10]:
                shown = val if isinstance(val, (int, float, bool)) else json.dumps(val, ensure_ascii=False)
                lines.append(f"{key}: {str(shown)[:160]}")
            lines.append(f"向量覆盖：{cov.get('embedded', cov.get('has', 0))}/{cov.get('total', 0)}（当前代 {cov.get('gen', '未知')}）")
            missing = [k for k in ("model_person_profile", "model_embedding", "model_forget") if not cfg.get(k)]
            if missing:
                lines.append("未分配模型：" + "、".join(missing) + "（面板 → 模型分配）")
            yield event.plain_result("\n".join(lines))
        except Exception as exc:                            # noqa: BLE001
            logger.warning(f"[StarHeart] /hf health 失败：{exc}")
            yield event.plain_result(f"/hf health 执行出错：{str(exc)[:120]}")

    @filter.permission_type(PermissionType.ADMIN)
    @hf.command("sid")
    async def hf_sid(self, event: AstrMessageEvent):
        """查看当前会话 ID / 用户 ID / 人格"""
        try:
            try:
                persona = await self._active_persona_id(
                    str(getattr(event, "unified_msg_origin", "") or ""),
                    platform_name=str(event.get_platform_name() or ""),
                )
            except Exception:                               # noqa: BLE001 人格拿不到不该让整条指令失败
                persona = "（读取失败）"
            lines = [
                "当前上下文",
                f"会话 ID：{self._sid(event)}",
                f"平台：{event.get_platform_name()}",
                f"用户 ID：{event.get_sender_id()}",
                f"昵称：{event.get_sender_name() or '（无）'}",
                f"场景：{'群聊' if event.get_group_id() else '私聊'}",
                f"人格：{persona or '（跟随 AstrBot 原生）'}",
            ]
            yield event.plain_result("\n".join(lines))
        except Exception as exc:                            # noqa: BLE001
            logger.warning(f"[StarHeart] /hf sid 失败：{exc}")
            yield event.plain_result(f"/hf sid 执行出错：{str(exc)[:120]}")

    @filter.permission_type(PermissionType.ADMIN)
    @hf.command("on")
    async def hf_on(self, event: AstrMessageEvent):
        """打开StarHeart总开关"""
        try:
            await self.service.save_config({"master_switch": True})
            await self.service.config_save_sync()
            yield event.plain_result("StarHeart总开关：已打开")
        except Exception as exc:                            # noqa: BLE001
            logger.warning(f"[StarHeart] /hf on 失败：{exc}")
            yield event.plain_result(f"/hf on 执行出错：{str(exc)[:120]}")

    @filter.permission_type(PermissionType.ADMIN)
    @hf.command("off")
    async def hf_off(self, event: AstrMessageEvent):
        """关闭StarHeart总开关（彻底不介入）"""
        try:
            await self.service.save_config({"master_switch": False})
            await self.service.config_save_sync()
            yield event.plain_result("StarHeart总开关：已关闭（记忆与面板数据仍在，随时 /hf on 回来）")
        except Exception as exc:                            # noqa: BLE001
            logger.warning(f"[StarHeart] /hf off 失败：{exc}")
            yield event.plain_result(f"/hf off 执行出错：{str(exc)[:120]}")

    @filter.permission_type(PermissionType.ADMIN)
    @hf.command("recall")
    async def hf_recall(self, event: AstrMessageEvent):
        """混合召回测试：/hf recall 关键词"""
        try:
            if not await self._cmd_ready():
                yield event.plain_result("StarHeart尚未初始化完成，请稍后重试")
                return
            query = self._cmd_arg(event, 2)
            if not query:
                yield event.plain_result("用法：/hf recall 关键词（例：/hf recall 生日）")
                return
            out = await self.hybrid_recall(query, topk=6)
            route = out.get("route") or {}
            lines = [f"召回「{query}」· {out.get('took_ms', 0)}ms · 候选 {out.get('candidates', 0)}",
                     f"关键词命中 {route.get('keyword_hits', 0)} · 向量命中 {route.get('vector_hits', 0)}"]
            if route.get("vector_note"):
                lines.append(f"向量：{route['vector_note']}")
            items = out.get("items") or []
            if not items:
                lines.append("没有召回结果")
            for it in items[:6]:
                text = (it.get("summary") or it.get("content") or "").replace("\n", " ")
                lines.append(f"[{it.get('id')}] {it.get('score', 0):.3f} {text[:60]}")
            yield event.plain_result("\n".join(lines))
        except Exception as exc:                            # noqa: BLE001
            logger.warning(f"[StarHeart] /hf recall 失败：{exc}")
            yield event.plain_result(f"/hf recall 执行出错：{str(exc)[:120]}")

    @filter.permission_type(PermissionType.ADMIN)
    @hf.command("mem")
    async def hf_mem(self, event: AstrMessageEvent):
        """看某条记忆详情：/hf mem <记忆ID>"""
        try:
            if not await self._cmd_ready():
                yield event.plain_result("StarHeart尚未初始化完成，请稍后重试")
                return
            mid = self._cmd_arg(event, 2)
            if not mid:
                yield event.plain_result("用法：/hf mem <记忆ID>（ID 形如 m870，可先用 /hf recall 关键词 找到）")
                return
            row = await self.store.get_memory(mid)
            if not row:
                yield event.plain_result(f"没找到记忆 {mid}")
                return
            vec = row.get("vector") or {}
            body = (row.get("content") or row.get("summary") or "").replace("\n", " ")
            lines = [
                f"记忆 {mid}",
                f"摘要：{row.get('summary') or '（无）'}",
                f"状态：{row.get('status') or '?'} · 类型：{row.get('atom_type') or '?'} · 重要度 {row.get('poignancy', '?')}",
                f"强度：{row.get('strength', '?')} · 提及 {row.get('recalls', 0)} 次",
                f"向量：{('已有（%s）' % vec.get('gen')) if vec.get('has_embedding') else '未生成'}",
                f"内容：{body[:400]}",
                "改 / 删 / 加权：面板 → 记忆库",
            ]
            yield event.plain_result("\n".join(lines))
        except Exception as exc:                            # noqa: BLE001
            logger.warning(f"[StarHeart] /hf mem 失败：{exc}")
            yield event.plain_result(f"/hf mem 执行出错：{str(exc)[:120]}")

    @filter.permission_type(PermissionType.ADMIN)
    @hf.command("mute")
    async def hf_mute(self, event: AstrMessageEvent):
        """别再提这条记忆：/hf mute <记忆ID> [off]"""
        try:
            if not await self._cmd_ready():
                yield event.plain_result("StarHeart尚未初始化完成，请稍后重试")
                return
            parts = self._cmd_arg(event, 2).split()
            if not parts:
                yield event.plain_result(
                    "用法：/hf mute <记忆ID>（恢复：/hf mute <记忆ID> off）\n先 /hf recall 关键词 找到 ID")
                return
            mid = parts[0]
            off = len(parts) > 1 and parts[1].lower() in ("off", "0", "false", "取消", "恢复")
            if off:
                res = await self.service.memory_mute(mid, False)
            else:
                # 顺手标成 test 来源：这条多半是当初拿测试句喂进来的，
                # 留个标签，面板筛选时一眼能认出来（真正的开关仍是 muted）。
                res = await self.service.memory_mute(mid, True, source_mode="test")
            if not res.get("ok"):
                yield event.plain_result(f"没找到记忆 {mid}")
                return
            if off:
                yield event.plain_result(f"{mid} 已恢复：重新参与召回与主动开口")
            else:
                yield event.plain_result(f"{mid} 已冷处理：不再进召回与主动开口（/hf mute {mid} off 可恢复）")
        except Exception as exc:                            # noqa: BLE001
            logger.warning(f"[StarHeart] /hf mute 失败：{exc}")
            yield event.plain_result(f"/hf mute 执行出错：{str(exc)[:120]}")

    @filter.permission_type(PermissionType.ADMIN)
    @hf.command("v2")
    async def hf_v2(self, event: AstrMessageEvent):
        """v2 记忆：status / migrate / shadow on|off / on|off"""
        try:
            if not await self._cmd_ready():
                yield event.plain_result("StarHeart尚未初始化完成，请稍后重试")
                return
            parts = self._cmd_arg(event, 2).split()
            sub = (parts[0] if parts else "status").lower()
            if sub == "migrate":
                res = await self.store.migrate_v1_to_v2()
                yield event.plain_result(
                    "v2 迁移完成："
                    f"源 {res['total']} / 新增 {res['inserted']} / 跳过 {res['skipped']} / "
                    f"向量 {res['vectors']} / 实体边 {res['edges']} / 错误 {res['errors']}"
                )
                return
            if sub in ("extract", "run"):
                # 手动跑抽取：不依赖全局开关，先把 pending 的 episode 消化掉
                from .core import memory_v2 as v2mem

                rows = await (await self.store.db.execute(
                    "SELECT id FROM episodes WHERE extract_state IN ('pending','failed') "
                    "ORDER BY id LIMIT 5")).fetchall()
                if not rows:
                    yield event.plain_result(
                        "没有待抽取的 episode（先聊几轮攒原文；影子写开着就会自动攒）")
                    return
                _cfg = dict(await self.service.config())
                _cfg["v2_enabled"] = True
                out = []
                for r in rows:
                    eid = int(r["id"])
                    try:
                        s = await v2mem.write_episode(self, eid, _cfg)
                        out.append(
                            f"· ep{eid}: 抽出 {s.get('extracted', 0)} / 写入 {s.get('written', 0)} / "
                            f"合并 {s.get('merged', 0)} / 跳过 {s.get('noop', 0)} / "
                            f"错误 {s.get('errors', 0)}"
                            + (f"（{s.get('reason')}）" if s.get("reason") else ""))
                    except Exception as exc:                # noqa: BLE001
                        out.append(f"· ep{eid}: 失败 {str(exc)[:80]}")
                yield event.plain_result("v2 抽取完成：\n" + "\n".join(out))
                return
            if sub == "shadow":
                val = (parts[1] if len(parts) > 1 else "").lower()
                if val not in ("on", "off"):
                    yield event.plain_result("用法：/hf v2 shadow on|off")
                    return
                await self.service.save_config({"v2_shadow_write": val == "on"})
                await self.service.config_save_sync()
                yield event.plain_result(
                    "v2 影子写：" + ("已开启（只落 episode 原文，不抽取、不影响主链路）" if val == "on" else "已关闭")
                )
                return
            if sub in ("on", "off"):
                await self.service.save_config({"v2_enabled": sub == "on"})
                await self.service.config_save_sync()
                yield event.plain_result("v2 召回：" + ("已开启" if sub == "on" else "已关闭（旧链路照旧）"))
                return
            cfg = await self.service.config()
            counts = await self.store.v2_counts()
            lines = [
                "v2 状态：",
                f"· 召回开关 v2_enabled={bool(cfg.get('v2_enabled'))}",
                f"· 影子写 v2_shadow_write={bool(cfg.get('v2_shadow_write'))}",
                f"· 巩固 v2_consolidate_enabled={bool(cfg.get('v2_consolidate_enabled'))}",
            ]
            lines += [f"· {k}：{v}" for k, v in counts.items()]
            lines.append("命令：/hf v2 migrate | shadow on|off | on|off")
            yield event.plain_result("\n".join(lines))
        except Exception as exc:                            # noqa: BLE001
            logger.warning(f"[StarHeart] /hf v2 失败：{exc}")
            yield event.plain_result(f"/hf v2 执行出错：{str(exc)[:160]}")

    @filter.permission_type(PermissionType.ADMIN)
    @hf.command("style")
    async def hf_style(self, event: AstrMessageEvent):
        """语言学习：status / learn / list / show / on|off / mode legacy|new"""
        try:
            if not await self._cmd_ready():
                yield event.plain_result("StarHeart尚未初始化完成，请稍后重试")
                return
            parts = self._cmd_arg(event, 2).split()
            sub = (parts[0] if parts else "status").lower()
            cfg = await self.service.config()
            if sub == "learn":
                from .core import style_memory as style_mod

                pid = await self.service._active_pid()
                res = await style_mod.run_learn(self, cfg, pid or "")
                if not res.get("ok"):
                    yield event.plain_result(
                        f"这轮没学到：{res.get('reason')}（新记忆 {res.get('fresh')} 条）")
                    return
                yield event.plain_result(
                    f"学完了：{res['saved']} 条原话 / {res['styles']} 种回复方式 / "
                    f"词条 +{res['terms']} / 其中被纠正 {res['fixed']} 条")
                return
            if sub == "list":
                styles = await self.store.list_styles("")
                if not styles:
                    yield event.plain_result("还没有回复方式，先 /hf style learn")
                    return
                lines = []
                for s in styles[:6]:
                    quotes = await self.store.list_quotes("", int(s["id"]), status="active")
                    first = str(quotes[0].get("quote"))[:20] if quotes else "（空）"
                    lines.append(
                        f"· {s['name']}（{len(quotes)} 条 · 命中 {s['hits']} / 纠正 {s['corrections']}）"
                        f" 例：{first}"
                    )
                yield event.plain_result("回复方式：\n" + "\n".join(lines))
                return
            if sub == "show":
                from .core import style_memory as style_mod

                pid = await self._inject_persona_id(self._sid(event), use_cache=True)
                _reg = "group" if "group" in str(self._sid(event)).lower() else "private"
                blk = await style_mod.style_block(
                    self.store,
                    {**cfg, "expression_style_enabled": True},
                    pid or "", _reg, event.message_str or "",
                )
                yield event.plain_result(blk or "（当前没有可注入的样本/词条）")
                return
            if sub in ("on", "off"):
                await self.service.save_config({"expression_style_enabled": sub == "on"})
                await self.service.config_save_sync()
                yield event.plain_result(
                    "新表达系统：" + ("已开启（记得 mode new）" if sub == "on" else "已关闭（旧规则照旧）"))
                return
            if sub == "mode":
                val = (parts[1] if len(parts) > 1 else "").lower()
                if val not in ("legacy", "new"):
                    yield event.plain_result("用法：/hf style mode legacy|new")
                    return
                await self.service.save_config({"expression_style_inject_mode": val})
                await self.service.config_save_sync()
                yield event.plain_result(
                    "注入模式：" + ("新·原话样本" if val == "new" else "旧·30 条规则"))
                return
            counts = await self.store.style_counts()
            yield event.plain_result(
                "语言学习：\n"
                f"· 开关 {bool(cfg.get('expression_style_enabled'))} / "
                f"模式 {cfg.get('expression_style_inject_mode')}\n"
                f"· bot 样本 {counts.get('bot')} / 人样本 {counts.get('human')} / "
                f"词条 {counts.get('terms')}\n"
                "命令：learn | list | show | on|off | mode legacy|new")
        except Exception as exc:                            # noqa: BLE001
            logger.warning(f"[StarHeart] /hf style 失败：{exc}")
            yield event.plain_result(f"/hf style 出错：{str(exc)[:160]}")

    @filter.permission_type(PermissionType.ADMIN)
    @hf.command("gate")
    async def hf_gate(self, event: AstrMessageEvent):
        """最近的门控决策：/hf gate [条数]"""
        try:
            if not await self._cmd_ready():
                yield event.plain_result("StarHeart尚未初始化完成，请稍后重试")
                return
            arg = self._cmd_arg(event, 2)
            limit = min(int(arg), 30) if arg.isdigit() else 8
            out = await self.service.gate_log(limit=limit)
            rows = out.get("items") or out.get("rows") or []
            lines = [f"门控决策（最近 {len(rows)} 条）"]
            if not rows:
                lines.append("暂无记录")
            for r in rows:
                lines.append("{ts} {d} 分数 {s} · {reason}".format(
                    ts=str(r.get("created_at") or r.get("ts") or "")[:16],
                    # 库里的字段是 decision（reply/silent/backoff），没有 should_reply/reply → 以前恒显示"沉默"
                    d={"reply": "回复", "silent": "沉默", "backoff": "退避"}.get(
                        str(r.get("decision") or ""), str(r.get("decision") or "沉默")),
                    s=r.get("score", r.get("total", "?")),
                    reason=str(r.get("reason") or r.get("note") or "")[:40],
                ))
            yield event.plain_result("\n".join(lines))
        except Exception as exc:                            # noqa: BLE001
            logger.warning(f"[StarHeart] /hf gate 失败：{exc}")
            yield event.plain_result(f"/hf gate 执行出错：{str(exc)[:120]}")

    @filter.permission_type(PermissionType.ADMIN)
    @hf.command("fav")
    async def hf_fav(self, event: AstrMessageEvent):
        """好感概览；也可 /hf fav set <用户ID> <增减> [理由] 手调"""
        try:
            if not await self._cmd_ready():
                yield event.plain_result("StarHeart尚未初始化完成，请稍后重试")
                return
            arg = self._cmd_arg(event, 2)
            if arg.startswith("set "):
                parts = arg.split()
                if len(parts) < 3:
                    yield event.plain_result("用法：/hf fav set <用户ID> <增减> [理由]（例：/hf fav set 100000000 2 帮忙找资料）")
                    return
                out = await self.service.favour_adjust(parts[1], float(parts[2]), " ".join(parts[3:]))
                # 不写嵌套同引号 f-string：Python < 3.12 下是语法错误（PEP 701 才允许），
                # 一旦 AstrBot 环境回退到 3.11 就是整个插件加载失败，零收益的隐患。
                _fav_line = f"今日 {out.get('daily', '?')} / 累计 {out.get('lifetime', '?')}（{out.get('zone_text') or out.get('zone') or '无档位'}）"
                yield event.plain_result(f"好感已调整：{out.get('user_id', parts[1])} → {_fav_line}")
                return
            out = await self.service.favours()
            rows = out.get("items") or out.get("rows") or []
            if arg:
                rows = [r for r in rows if str(r.get("user_id")) == arg]
            lines = ["好感概览"]
            if not rows:
                lines.append("暂无数据")
            for r in rows[:10]:
                lines.append(f"{r.get('name') or r.get('user_id')}：今日 {r.get('daily', '?')} / 累计 {r.get('lifetime', '?')}（{r.get('zone') or '无档位'}）")
            ins = await self.service.favour_insight()
            if isinstance(ins, dict) and (ins.get("avg_daily") is not None or ins.get("events")):
                lines.append(f"洞察：{str(ins['summary'])[:120]}")
            yield event.plain_result("\n".join(lines))
        except Exception as exc:                            # noqa: BLE001
            logger.warning(f"[StarHeart] /hf fav 失败：{exc}")
            yield event.plain_result(f"/hf fav 执行出错：{str(exc)[:120]}")

    @filter.permission_type(PermissionType.ADMIN)
    @hf.command("expr")
    async def hf_expr(self, event: AstrMessageEvent):
        """表达习惯概览"""
        try:
            if not await self._cmd_ready():
                yield event.plain_result("StarHeart尚未初始化完成，请稍后重试")
                return
            out = await self.service.habits()
            rows = out.get("items") or out.get("habits") or []
            lines = [f"表达习惯 {len(rows)} 条"]
            for r in rows[:8]:
                quote = str(r.get("way") or r.get("phrase") or "")[:40]
                lines.append(f"{r.get('scene') or r.get('context') or '通用'}：「{quote}」"
                             f"{'（已启用）' if r.get('enabled', True) else '（已停用）'}")
            log = await self.service.habit_log(limit=5)
            # 真事件在 habit_log()["events"]（hf_audit）；"items" 是习惯行，没有 created_at/action → 恒空
            for r in (log.get("events") or log.get("items") or [])[:5]:
                lines.append(f"· {str(r.get('created_at') or '')[:16]} {str(r.get('action') or r.get('note') or '')[:40]}")
            yield event.plain_result("\n".join(lines))
        except Exception as exc:                            # noqa: BLE001
            logger.warning(f"[StarHeart] /hf expr 失败：{exc}")
            yield event.plain_result(f"/hf expr 执行出错：{str(exc)[:120]}")

    @filter.permission_type(PermissionType.ADMIN)
    @hf.command("msg")
    async def hf_msg(self, event: AstrMessageEvent):
        """消息事件汇总"""
        try:
            if not await self._cmd_ready():
                yield event.plain_result("StarHeart尚未初始化完成，请稍后重试")
                return
            hub = await self.service.message_hub(limit=5)
            kpi = hub.get("kpi") or {}
            lines = ["消息事件汇总"]
            # kpi 是 dict（{key: value}）不是数组；事件流的键叫 stream —— 两处以前都读错，命令必然抛错
            for k, v in (list(kpi.items())[:8] if isinstance(kpi, dict) else []):
                lines.append(f"{k}：{v if not isinstance(v, dict) else (v.get('value') or v)}")
            for r in (hub.get("stream") or hub.get("events") or [])[:5]:
                lines.append(f"· {str(r.get('ts') or '')[:16]} [{r.get('source', '')}] {str(r.get('text') or r.get('summary') or '')[:50]}")
            lines.append("完整图形版：面板 → 消息事件")
            yield event.plain_result("\n".join(lines))
        except Exception as exc:                            # noqa: BLE001
            logger.warning(f"[StarHeart] /hf msg 失败：{exc}")
            yield event.plain_result(f"/hf msg 执行出错：{str(exc)[:120]}")

    @filter.permission_type(PermissionType.ADMIN)
    @hf.command("vec")
    async def hf_vec(self, event: AstrMessageEvent):
        """向量库代数与覆盖率"""
        try:
            if not await self._cmd_ready():
                yield event.plain_result("StarHeart尚未初始化完成，请稍后重试")
                return
            gens = await self.service.vector_gens()
            cov = await self.store.embedding_coverage()
            lines = [f"向量库当前代：{self._gen_name(gens.get('active')) or cov.get('gen') or '未知'}"]
            for g in (gens.get("items") or gens.get("gens") or []):
                lines.append(f"· {g.get('gen') or g.get('gen_key')}：{g.get('covered', g.get('count', '?'))} 条"
                             f"{'（当前使用）' if g.get('active') else ''}")
            lines.append(f"覆盖率：{cov.get('embedded', cov.get('has', 0))}/{cov.get('total', 0)}")
            lines.append("换模型会新建一代、不删旧代；补向量：面板 → 记忆库 → 补全向量")
            yield event.plain_result("\n".join(lines))
        except Exception as exc:                            # noqa: BLE001
            logger.warning(f"[StarHeart] /hf vec 失败：{exc}")
            yield event.plain_result(f"/hf vec 执行出错：{str(exc)[:120]}")

    @filter.permission_type(PermissionType.ADMIN)
    @hf.command("graph")
    async def hf_graph(self, event: AstrMessageEvent):
        """关系图谱统计"""
        try:
            if not await self._cmd_ready():
                yield event.plain_result("StarHeart尚未初始化完成，请稍后重试")
                return
            st = await self.service.graph_stats()
            lines = ["关系图谱统计"]
            for k, v in list(st.items())[:8]:
                lines.append(f"{k}：{v}")
            lines.append("图形版：面板 → 记忆图谱（3D 可交互）")
            yield event.plain_result("\n".join(lines))
        except Exception as exc:                            # noqa: BLE001
            logger.warning(f"[StarHeart] /hf graph 失败：{exc}")
            yield event.plain_result(f"/hf graph 执行出错：{str(exc)[:120]}")

    @filter.permission_type(PermissionType.ADMIN)
    @hf.command("profile")
    async def hf_profile(self, event: AstrMessageEvent):
        """画像概览（人物 L0 / 群聊 L1）"""
        try:
            if not await self._cmd_ready():
                yield event.plain_result("StarHeart尚未初始化完成，请稍后重试")
                return
            ps = await self.service.personas()
            pid = str(ps.get("active") or "")
            # 以前这里把「人格名」当成「人物 id」传进画像接口 → 这条指令一直是空转的。
            # 现在改成读列表（列表本身带当前人格下的画像状态）：人物 + 群聊都覆盖。
            data = await self.service.persons(6, pid)
            items = data.get("items") or []
            groups = data.get("groups") or []
            have_p = [x for x in items if x.get("profile")]
            have_g = [x for x in groups if x.get("profile")]
            lines = [f"画像（当前人格：{pid or '未指定'}）",
                     f"已生成：人物 {len(have_p)}/{len(items)} · 群聊 {len(have_g)}/{len(groups)}（各取最近活跃的几个）"]

            def _line(x, tag: str) -> str:
                pr = x.get("profile") or {}
                who = x.get("display") or x.get("user_id") or x.get("group_id") or ""
                return f"{tag} {who}：{str(pr.get('one_line') or '（没有一句话概括）')[:60]}（记忆 {pr.get('mem_count', 0)} 条）"

            for x in have_p[:3]:
                lines.append(_line(x, "人物"))
            for x in have_g[:2]:
                lines.append(_line(x, "群聊"))
            if not have_p and not have_g:
                lines.append("当前人格下还没有画像 —— 材料不足时不会生成（避免让模型瞎猜）；"
                             "可在面板「画像」页点生成，或等后台自动补齐")
            st = await self.service.evolve_state()
            lines.append(f"人格进化：待审核 {st.get('pending', 0)} 条 · 上次 {str(st.get('last_evolve_at') or '—')[:19]}")
            if st.get("skipped"):
                lines.append(f"进化跳过原因：{str(st.get('skipped'))[:80]}")
            lines.append("详情：面板 → 记忆系统 → 画像 / 人格管理 → 状态与进化")
            yield event.plain_result("\n".join(lines))
        except Exception as exc:                            # noqa: BLE001
            logger.warning(f"[StarHeart] /hf profile 失败：{exc}")
            yield event.plain_result(f"/hf profile 执行出错：{str(exc)[:120]}")

    @filter.permission_type(PermissionType.ADMIN)
    @hf.command("persona")
    async def hf_persona(self, event: AstrMessageEvent):
        """人格列表与当前人格"""
        try:
            ps = await self.service.personas()
            items, act, cnts = ps.get("items") or [], str(ps.get("active") or ""), ps.get("counts") or {}
            lines = [f"人格 {len(items)} 个" + ("" if ps.get("available", True) else "（原生人格接口读不到）")]
            for p in items[:12]:
                pid = str(p.get("id") or p.get("persona_id") or "")
                lines.append(f"{'★' if pid and pid == act else '·'} {p.get('name') or pid or '?'}"
                             f"（记忆 {cnts.get(pid, 0)} 条）")
            lines.append("StarHeart只用 AstrBot 原生人格，不接管人格")
            yield event.plain_result("\n".join(lines))
        except Exception as exc:                            # noqa: BLE001
            logger.warning(f"[StarHeart] /hf persona 失败：{exc}")
            yield event.plain_result(f"/hf persona 执行出错：{str(exc)[:120]}")

    @filter.permission_type(PermissionType.ADMIN)
    @hf.command("forget")
    async def hf_forget(self, event: AstrMessageEvent):
        """遗忘评估：/hf forget [run]（run 会真正执行一次）"""
        try:
            if not await self._cmd_ready():
                yield event.plain_result("StarHeart尚未初始化完成，请稍后重试")
                return
            if self._cmd_arg(event, 2).lower() == "run":
                await self._forget_tick()
                yield event.plain_result("已执行一次遗忘评估（结果见面板 → 记忆库 / 审计）")
                return
            prev = await self.service.lifecycle_preview()
            lines = ["遗忘评估预览（未改动任何数据；要执行加 run）"]
            for k, v in list(prev.items())[:8]:
                lines.append(f"{k}：{str(v)[:120] if not isinstance(v, (int, float)) else v}")
            lines.append("当前策略：面板 → 设置 → 衰减与遗忘")
            yield event.plain_result("\n".join(lines))
        except Exception as exc:                            # noqa: BLE001
            logger.warning(f"[StarHeart] /hf forget 失败：{exc}")
            yield event.plain_result(f"/hf forget 执行出错：{str(exc)[:120]}")

    @filter.permission_type(PermissionType.ADMIN)
    @hf.command("conf")
    async def hf_conf(self, event: AstrMessageEvent):
        """关键配置概览"""
        try:
            dom = await self.service.config_domains()
            values = dom.get("values") or {}
            keys = ("master_switch", "gate_enabled", "gate_threshold", "gate_shadow",
                    "memory_decay_half_life_days", "forget_enabled", "archive_after_days", "forget_after_days",
                    "auto_import_external", "model_embedding", "model_forget", "model_rerank",
                                              "output_split_messages", "output_typing_per_char", "favour_daily_cap",
                                              "sticker_slot", "voice_slot")
            lines = ["关键配置"]
            for k in keys:
                if k in values:
                    lines.append(f"{k} = {values[k]}")
            lines.append("完整配置：面板 → 设置（改完自动保存）")
            yield event.plain_result("\n".join(lines))
        except Exception as exc:                            # noqa: BLE001
            logger.warning(f"[StarHeart] /hf conf 失败：{exc}")
            yield event.plain_result(f"/hf conf 执行出错：{str(exc)[:120]}")

    @filter.permission_type(PermissionType.ADMIN)
    @hf.command("import")
    async def hf_import(self, event: AstrMessageEvent):
        """导入 astrbot_plugin_livingmemory 的记忆"""
        try:
            if not await self._cmd_ready():
                yield event.plain_result("StarHeart尚未初始化完成，请稍后重试")
                return
            out = await self.service.import_start()
            if out.get("started"):
                yield event.plain_result("已开始导入 LivingMemory 记忆，进度请到面板「设置 → 记忆导入」查看")
            else:
                yield event.plain_result(f"未开始：{out.get('message')}")
        except Exception as exc:                            # noqa: BLE001
            logger.warning(f"[StarHeart] /hf import 失败：{exc}")
            yield event.plain_result(f"/hf import 执行出错：{str(exc)[:120]}")

    @filter.permission_type(PermissionType.ADMIN)
    @hf.command("link")
    async def hf_link(self, event: AstrMessageEvent):
        """重建记忆关系链（图谱只有孤点时用）"""
        try:
            if not await self._cmd_ready():
                yield event.plain_result("StarHeart尚未初始化完成，请稍后重试")
                return
            stat = await self.service.graph_build()
            yield event.plain_result(
                "关系链重建完成\n"
                f"扫描记忆 {stat.get('memories', 0)} 条\n"
                f"实体边 +{stat.get('entity', 0)} · 时序边 +{stat.get('temporal', 0)} · 语义边 +{stat.get('semantic', 0)}\n"
                f"当前孤立记忆 {stat.get('stats', {}).get('isolated', 0)} 条 · 平均度数 {stat.get('stats', {}).get('avg_degree', 0)}"
            )
        except Exception as exc:                            # noqa: BLE001
            logger.warning(f"[StarHeart] /hf link 失败：{exc}")
            yield event.plain_result(f"/hf link 执行出错：{str(exc)[:120]}")

    # ===================== 记忆库管理表 / 混合召回 =====================

    def _recall_engine(self):
        """混合召回引擎（关键词路 + 向量路 → RRF 融合）。懒加载 + 复用向量缓存。"""
        if getattr(self, "_rec_engine", None) is None:
            from .core.recall import HybridRecall  # 延迟导入：插件加载期不额外吃时间
            self._rec_engine = HybridRecall(self.store, self._embed, rerank=self._rerank_texts)
        return self._rec_engine

    async def hybrid_recall(self, query: str, *, persona_id: str = "", session_id: str = "", topk: int = 8) -> dict:
        try:
            return await self._recall_engine().search(
                query, persona_id=persona_id, session_id=session_id, topk=topk
            )
        except Exception as exc:                            # noqa: BLE001 召回测试不能把面板打崩
            logger.warning(f"[StarHeart] 召回失败：{exc}")
            return {"query": query, "items": [], "route": {"keyword_hits": 0, "vector_hits": 0, "vector_note": f"召回异常：{str(exc)[:80]}"}, "took_ms": 0, "candidates": 0}

    async def rewrite_summaries(self, persona_id: str = "", limit: int = 12) -> dict:
        """把已有的记忆简写（summary）重写成自然句式 —— 去掉「用户…」这种机器口吻。

        为什么需要它：内置总结提示词曾硬性要求「每条以用户为主语」，于是所有简写都长成
        「用户偏好…」「用户计划…」，面板上像一本机器台账（用户 2026-10-11 反馈）。
        提示词已改成「像人给自己记的笔记」，本方法负责把**存量**重写一遍。

        接口按「每次调用处理 limit 条」设计（面板像「补全向量」那样循环调用），这样：
        单次请求短、不会超时；进度能在按钮上实时显示；中途停下也不丢已完成的部分。
        旧简写先备份进 hf_summary_backup，写完重算向量（简写变了，向量要跟着变）。
        """
        cfg = await self.service.config()
        pid = str(persona_id or "").strip()
        if not pid:
            return {"ok": False, "error": "先在上面筛选里选一个人格再重写（避免把导入的记忆一起改写）"}
        rows = await self.store.list_memories(persona_id=pid, size=2000, page=1)
        items = list((rows or {}).get("items") or [])
        if not items:
            return {"ok": True, "scanned": 0, "rewritten": 0, "remaining": 0, "model": ""}
        def _num(mid: Any) -> int | None:
            """记忆 id 库里是整数，但接口给的是 "m853" 这种带前缀的字符串 —— 统一取数字部分。"""
            s = str(mid).strip()
            if s.lower().startswith("m"):
                s = s[1:]
            return int(s) if s.isdigit() else None

        done: set[int] = getattr(self, "_rewrite_done", set())
        tries: dict[int, int] = getattr(self, "_rewrite_tries", {})
        if not done:                                   # 新一轮：清掉上一轮的状态
            self._rewrite_done, self._rewrite_tries = done, tries
        pending = [r for r in items if _num(r["id"]) is not None and _num(r["id"]) not in done]
        chunk = pending[: max(1, int(limit or 12))]
        if not chunk:
            self._rewrite_done, self._rewrite_tries = set(), {}
            return {"ok": True, "scanned": len(items), "rewritten": 0, "remaining": 0, "model": ""}
        model = self._embed_model_name(cfg)
        system = (
            "你是记忆整理器。把给定的「记忆简写」改写成更自然、具体、可复述的短句——只改说法，不改事实。\n"
            "1) 不要以「用户」开头，直接说事；涉及两个人时写清谁对谁即可，不用强行统一人称；\n"
            "2) 每条 ≤ 60 字，像人给自己记的笔记；\n"
            "3) 不得新增、删改或合并信息；\n"
            "4) 每条输出一行，格式 `id|新简写`，条数与输入一致，不要解释。"
        )
        lines = [f"{_num(r['id'])}|{str(r.get('summary') or '').strip()}" for r in chunk]
        prompt = "把下面每条记忆简写改写得更自然（原样保留 id）：\n" + "\n".join(lines)
        out = await self._llm_generate(prompt, "memory_summary", system)
        mapping: dict[int, str] = {}
        for line in str(out or "").splitlines():
            line = line.strip().lstrip("-* ").strip()
            if "|" not in line:
                continue
            k, v = line.split("|", 1)
            k, v = k.strip().lstrip("m").strip(), v.strip()
            if k.isdigit() and v:
                mapping[int(k)] = v[:120]
        try:
            await self.store.db.execute(
                "CREATE TABLE IF NOT EXISTS hf_summary_backup "
                "(memory_id INTEGER, old_summary TEXT, new_summary TEXT, persona_id TEXT, at REAL)"
            )
        except Exception:                              # noqa: BLE001 表已存在
            pass
        rewritten = failed = 0
        for r in chunk:
            mid_key = _num(r["id"])
            if mid_key is None:
                continue
            new = mapping.get(mid_key)
            old = str(r.get("summary") or "")
            if not new or new == old:
                tries[mid_key] = tries.get(mid_key, 0) + 1
                if tries.get(mid_key, 0) >= 2:         # 两次都没改出来 → 放弃它，别让循环卡死
                    done.add(mid_key)
                failed += 1
                continue
            try:
                await self.store.db.execute(
                    "INSERT INTO hf_summary_backup(memory_id, old_summary, new_summary, persona_id, at) VALUES(?,?,?,?,?)",
                    (mid_key, old, new, pid, time.time()),
                )
                # summary/content/cues 三处必须一起改：只改 summary 会让召回线索和正文停在旧说法。
                await self.store.update_memory(r["id"], {"summary": new, "content": new, "cues": [new[:24]]})
                text = str(new)[:512]
                if model:
                    vec = await self._embed(text)
                    if vec:
                        await self.store.set_embedding(r["id"], vec, model)
                done.add(mid_key)
                rewritten += 1
            except Exception as exc:                   # noqa: BLE001 单条失败不影响整批
                logger.warning(f"[StarHeart] 重写简写失败（{r['id']}）：{exc}")
                failed += 1
        remaining = len([r for r in items if _num(r["id"]) is not None and _num(r["id"]) not in done])
        if remaining == 0:
            self._rewrite_done, self._rewrite_tries = set(), {}
        else:
            self._rewrite_done, self._rewrite_tries = done, tries
        logger.info(
            f"[StarHeart] 重写简写：persona={pid} 本批 {len(chunk)} 条 → 成功 {rewritten} 失败 {failed}，剩余 {remaining}"
        )
        if remaining == 0:
            await self.store.audit("memory.rewrite_summaries", f"persona={pid} total={len(items)} 完成")
        return {"ok": True, "persona": pid, "scanned": len(items), "rewritten": rewritten,
                "failed": failed, "remaining": remaining, "model": model, "batch": len(chunk)}

    async def embed_backfill(self, limit: int = 16) -> dict:
        """批量补向量（面板按钮）。

        多代语义：按当前嵌入模型算出目标代，只补这一代缺的；建满后自动切过去。
        旧代一条不删，换回旧模型时直接切回、不重算。
        """
        lock = getattr(self, "_embed_lock", None)
        if lock is None:
            lock = self._embed_lock = asyncio.Lock()
        if lock.locked():
            return {"ok": False, "error": "后台正在补向量，稍后再点（不会重复叠加）"}
        async with lock:
            cfg = await self.service.config()
            model, dim, provider = self._embed_target(cfg)
            if not model or provider is None:
                return {"ok": False, "error": "没配「向量嵌入」模型：设置 → 模型分配 → 向量嵌入"}
            if dim <= 0:
                try:
                    probe = await provider.get_embedding("dimension probe")
                    dim = len(probe or [])
                except Exception as exc:                   # noqa: BLE001
                    return {"ok": False, "error": f"嵌入维度探测失败：{exc}"}
            if dim <= 0:
                return {"ok": False, "error": "嵌入模型没有返回维度"}

            active = await self.store.active_gen()
            target = self.store.gen_key(model, dim)
            rows = await self.store.memories_missing_gen(target, limit=limit)
            done = failed = 0
            for row in rows:
                text = (row.get("summary") or "") + " " + (row.get("content") or "")
                vec = await self._embed(text.strip() or row.get("summary") or "")
                if not vec:
                    failed += 1
                else:
                    actual = self.store.gen_key(model, len(vec))
                    if actual != target:
                        logger.warning(f"[StarHeart] 嵌入维度与声明不一致（声明 {dim}，实际 {len(vec)}），改用 {actual}")
                        target = actual
                    await self.store.set_embedding_gen(row["id"], vec, model, gen_key=target)
                    done += 1
                await asyncio.sleep(0.25)                   # 限速：保护 embedding 提供商与事件循环

            if target != active:
                missing = await self.store.gen_missing_count(target)
                if missing == 0:
                    await self.store.set_active_gen(target)
                    try:
                        await self.store.audit("vector.autoswitch", target, f"model={model} dim={dim} manual")
                    except Exception:                      # noqa: BLE001
                        pass
                    logger.info(f"[StarHeart] 向量库手动回填后自动切换 → {target}（旧代 {active or '无'} 保留）")
            target_missing = await self.store.gen_missing_count(target)
            return {
                "ok": True, "embedded": done, "failed": failed, "model": model,
                "target": target, "active": await self.store.active_gen(),
                "target_missing": target_missing,
                "items": await self.store.gen_stats(),
                "coverage": await self.service.embedding_coverage(),
            }

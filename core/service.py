"""StarHeart 业务服务层：所有路由逻辑都在这里，且**除 logger 外不依赖 astrbot**。

好处是能在本地直接跑真库做端到端测试（page_api 只做请求解析与响应包装）。
人格相关操作通过注入的 persona provider 完成，方便在 AstrBot 里接原生 PersonaManager。
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import random
import re
import shutil
import time
import unicodedata
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

# 审核规范（硬性）：logger 必须从 astrbot.api 导入，不得保留标准库 logging 的 fallback。
# 独立脚本环境（devtools 直接 import core）由 devtools/_astrbot_shim.py 在导入前注入桩，不进发布代码。
from astrbot.api import logger

from . import engine
from .importer import SOURCE_NAME, LivingMemoryImporter
from .storage import (  # noqa: F401
    CONFIG_SNAPSHOT_KEEP, EXPR_COOLDOWN_SECONDS, EXPR_WINDOW_MAX_INJECT,
    JARGON_CANDIDATE_BATCH, JARGON_CORPUS_SCAN, JARGON_MATCH_LIMIT, JARGON_MEANING_MAX_CHARS,
    JARGON_STATUSES, JARGON_TERM_MAX_CHARS, JARGON_TRY_MAX_TOKENS,    Store, expr_norm_key,
    session_kind, session_platform,
)

# 表达块是插件每轮临时挂上去的，不属于人格本体。
# 人格进化读人格、写人格时都必须先剥掉它，否则会把插件塞的话当成角色设定写进人格。
_EXPR_BLOCK_RE = re.compile(r"\n*【表达】.*?【/表达】\n*", re.S)
_LEGACY_SPLIT_RE = re.compile(r"想多说就接着往下说[^\n]*")


def strip_expression_block(text: str) -> str:
    t = _EXPR_BLOCK_RE.sub("\n", str(text or ""))
    t = _LEGACY_SPLIT_RE.sub("", t)
    return re.sub(r"\n{3,}", "\n\n", t).strip()



async def _maybe_await(value: Any) -> Any:
    """统一兼容 system provider 的同步/异步返回。

    坑（2026-10-15 真机实锤）：main 里 agent_prompt 提供方是**同步函数**，调用侧却直接
    `await fn(...)` —— await 一个 dict 当场抛 TypeError，被兜底 except 吞掉后 got 恒为 {}，
    于是「主动开口吃 AstrBot 完整提示词」这条路径**从未真正生效**（100% 静默降级成只吃人格，
    所有开口的输入 token 恒 ~1043 就是佐证）。这里两种写法都接住，杜绝再次静默失败。
    """
    if inspect.isawaitable(value):
        return await value
    return value


_ATOM_TYPES = frozenset(
    {"episodic", "factual", "relational", "preference", "planned", "routine", "opinion"}
)

def _salvage_fields(cand: str) -> dict[str, Any] | None:
    """最后一招：JSON 语法坏了也把常见字段抠出来（宁可要半成品，也不要"画像没生成"）。

    真机踩过：模型返回的字符串里有真实换行，json.loads 直接失败。
    """
    out: dict[str, Any] = {}
    keys = ["one_line", "identity", "traits", "likes", "dislikes", "topics", "relation", "events", "notes"]
    for k in keys:
        m = re.search(chr(34) + k + chr(34) + r"\s*[:：]\s*(\[[^\]]*\]|\{[^}]*\}|" + chr(34) + r"[^" + chr(34) + r"]*" + chr(34) + r"|[^,}\n]+)", cand, re.S)
        if not m:
            continue
        raw = m.group(1).strip()
        if raw.startswith("["):
            items = [x.strip().strip(chr(34)).strip() for x in raw.strip("[]").split(",")]
            out[k] = [x for x in items if x]
        elif raw.startswith("{"):
            inner = _salvage_fields(raw) or {}
            out[k] = [inner] if inner else []
        else:
            out[k] = raw.strip(chr(34)).strip()
    return out or None


def _repair_json(cand: str) -> str:
    """修模型常犯的几种 JSON 毛病：字符串里的真实换行/制表、全角标点、尾逗号。"""
    t = "".join((ch if ch >= " " or ch == chr(10) else " ") for ch in cand)
    t = t.replace(chr(10), " ").replace(chr(13), " ").replace(chr(9), " ")   # 字符串里的换行 → 空格
    t = t.replace(chr(0x201c), chr(34)).replace(chr(0x201d), chr(34))                 # 全角引号
    t = t.replace(chr(0xff1a), ":").replace(chr(0xff0c), ",")               # 全角冒号/逗号
    t = re.sub(r",\s*}", "}", t)
    t = re.sub(r",\s*]", "]", t)
    return t


_BAD_FIELD = ""


def _clean_text(v: Any, limit: int = 400) -> str:
    """字段级清洗：丢了坏值/坏转义（真机上出现过只剩一个反斜杠的字段），不改变含义。"""
    s = str(v if v is not None else "").strip()
    if not s:
        return ""
    # 只剩标点/反斜杠/空白 → 判为坏值（模型坏转义留下的残渣）
    # 坏值只判「没有任何实义字符（字母/数字/符号）」：纯标点/反斜杠才算坏。
    # 这样 emoji、♥ 这类合法昵称字符不会被误杀（红队实测过），坏转义残渣仍会被丢弃。
    if not any(unicodedata.category(ch)[0] in ("L", "N", "S") for ch in s):
        return ""
    if s.count(chr(92)) >= max(4, len(s) // 3):     # 反斜杠密度过高才算坏转义
        s = s.replace(chr(92), "")
    s = s.strip()
    return s[:limit]


def _clean_list(v: Any, limit: int = 30) -> list[str]:
    out: list[str] = []
    for x in (v if isinstance(v, list) else ([v] if v else [])):
        s = _clean_text(x, 120)
        if s and s not in out:
            out.append(s)
    return out[:limit]


def _clean_events(v: Any, limit: int = 24) -> list[dict[str, str]]:
    """events 必须是对象数组；模型常给「字符串化的 JSON 对象」或纯字符串（真机实测）。"""
    out: list[dict[str, str]] = []
    for x in (v if isinstance(v, list) else ([v] if v else [])):
        if isinstance(x, dict):
            when, what = _clean_text(x.get("when"), 60), _clean_text(x.get("what"), 240)
        elif isinstance(x, str):
            s = x.strip()
            when = what = ""
            if s.startswith("{"):
                try:
                    d = json.loads(s)
                    if isinstance(d, dict):
                        when, what = _clean_text(d.get("when"), 60), _clean_text(d.get("what"), 240)
                except Exception:
                    what = ""
            what = what or _clean_text(s, 240)
        else:
            continue
        if what:
            out.append({"when": when, "what": what})
    return out[:limit]


def _sanitize_profile(body: Any, pid: str = "", name: str = "") -> dict[str, Any]:
    """画像落库前的最后一道闸：字段级清洗 + 不泄漏内部 id（人物 id 不该出现在给人看的正文里）。"""
    b = body if isinstance(body, dict) else {}
    out: dict[str, Any] = {
        "one_line": _clean_text(b.get("one_line"), 120),
        "identity": _clean_text(b.get("identity"), 200),
        "traits": _clean_list(b.get("traits")),
        "likes": _clean_list(b.get("likes")),
        "dislikes": _clean_list(b.get("dislikes")),
        "topics": _clean_list(b.get("topics")),
        "relation": _clean_text(b.get("relation"), 500),
        "events": _clean_events(b.get("events")),
        "notes": _clean_text(b.get("notes"), 500),
    }
    pid = str(pid or "")
    if pid:
        # 只替换「人物 id xxx」这类整词；别把正文里恰好相同的短子串一起改，
        # 也绝不臆造性别代词（name 为空就用中性的「该联系人」）。
        safe = name or "该联系人"
        for k in ("one_line", "identity", "relation", "notes"):
            v = str(out.get(k) or "")
            if pid and pid in v:
                v = re.sub(r"(人物[ \t]*id|ID|id)[ \t:：]*" + re.escape(pid), safe, v)
                v = re.sub(r"(?<![0-9A-Za-z_])" + re.escape(pid) + r"(?![0-9A-Za-z_])", safe, v)
                out[k] = v
    return out


def _sanitize_group_profile(body: Any, gid: str = "", name: str = "") -> dict[str, Any]:
    """群聊画像落库前的字段级清洗（与人物画像同思路：只留白名单字段，号不进正文）。"""
    b = body if isinstance(body, dict) else {}
    members: list[dict[str, Any]] = []
    for m in (b.get("members") or [])[:40]:
        if not isinstance(m, dict):
            continue
        who = _clean_text(m.get("who"), 60)
        if not who:
            continue
        members.append({"who": who, "role": _clean_text(m.get("role"), 160),
                        "n": _clean_text(m.get("n"), 20)})
    out: dict[str, Any] = {
        "one_line": _clean_text(b.get("one_line"), 120),
        "kind": _clean_text(b.get("kind"), 80),
        "atmosphere": _clean_text(b.get("atmosphere"), 400),
        "topics": _clean_list(b.get("topics")),
        "members": members,
        "norms": _clean_list(b.get("norms")),
        "relation": _clean_text(b.get("relation"), 500),
        "notes": _clean_text(b.get("notes"), 500),
    }
    gid = str(gid or "")
    if gid:
        safe = name or "该群"
        for kk in ("one_line", "kind", "atmosphere", "relation", "notes"):
            v = str(out.get(kk) or "")
            if gid and gid in v:
                v = re.sub(r"(群[ \t]*号|群[ \t]*id|ID|id)[ \t:：]*" + re.escape(gid), safe, v)
                v = re.sub(r"(?<![0-9A-Za-z_])" + re.escape(gid) + r"(?![0-9A-Za-z_])", safe, v)
                out[kk] = v
    return out


def _json_block(text: str) -> dict[str, Any] | None:
    """从模型输出里抠出 JSON 对象，三级容错：严格 → 修复 → 正则抠字段。

    不要用 rfind("}")：模型常在 JSON 后面又补一句带花括号的话，那样切出来的不是合法 JSON。
    """
    t = str(text or "").strip()
    t = t.replace("```json", "").replace("```JSON", "").replace("```", "").strip()
    start = t.find("{")
    if start < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    cand = ""
    for i in range(start, len(t)):
        ch = t[i]
        if in_str:
            if esc:
                esc = False
            elif ch == chr(92):
                esc = True
            elif ch == chr(34):
                in_str = False
            continue
        if ch == chr(34):
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                cand = t[start:i + 1]
                break
    if not cand:
        return None
    for attempt in (cand, _repair_json(cand)):
        try:
            out = json.loads(attempt)
            if isinstance(out, dict):
                return out
        except Exception:
            continue
    return _salvage_fields(cand)


DEFAULT_CONFIG: dict[str, Any] = {
    "master_switch": True,
    # 3D 图谱控制台的界面配置（布局/镜头/配色/参数…）：前端每改一次自动写回这里，刷新不丢
    "graph_ui": {},
    "gate_enabled": True,
    "gate_shadow": True,
    "gate_threshold": 80,
    "gate_window_seconds": 20,
    "memory_inject_enabled": True,
    "memory_inject_top_k": 8,
    "memory_inject_budget_tokens": 1200,   # 实为字符数（键名沿用旧版，改键名会丢老配置）
    "memory_decay_half_life_days": 45,
    "memory_evict_below": 0.05,
    "memory_persona_isolated": True,
    "session_context_enabled": True,
    "memory_capture_enabled": True,
    "memory_capture_turns": 4,
    "memory_capture_min_interval": 120,
    "memory_capture_max_per_run": 5,
    # 跨会话动作留痕：bot 借工具（含第三方工具类插件）在别的会话里发了什么，这里留台账 +
    # 在相关会话注入知会，并在记忆库留一条（避免「问它谁让你发的，它答不知道」）。
    "cross_session_enabled": True,
    "cross_session_ttl_min": 120,
    "cross_session_memory": True,
    "memory_summary_prompt": "",     # 记忆总结自定义提示词（空 = 用内置；支持 {transcript} 占位）
    # 插件自管任务的系统提示词覆盖（留空 = 用内置默认；面板「模型分配」可展开编辑）
    "sys_prompt_memory_summary": "",
    "sys_prompt_expression_extract": "",
    "sys_prompt_person_profile": "",
    # 群聊画像：与 service.TASK_PROMPTS["group_profile"] 一一对应。
    # 以前只在 _conf_schema.json 里登记、这里漏了 → main.py 的「采纳原生配置」循环
    # 遍历的是 DEFAULT_CONFIG，于是跳过它，AstrBot 原生插件配置页里填的值永远不生效
    # （只有面板 prompt/save 旁路能写，双源行为不一致 = 假控件）。
    "sys_prompt_group_profile": "",
    "sys_prompt_persona_evolve": "",
    "sys_prompt_forget": "",
    "favour_enabled": True,
    "favour_settle_mode": "tag",
    "favour_daily_cap": 15,              # 好感日上限：当天正增量累计到此封顶（0 = 不限）
    "favour_daily_half_life_days": 0,    # 好感 daily 的半衰期天数（0 = 关闭衰减；库里配了值就以库里的为准）
    "favour_tag_required": True,         # 必须带好感标签才结算；关掉则无标签也按 +1
    "expression_enabled": True,
    "expression_min_uses": 3,            # 表达习惯至少生效过 N 次才算"已验证"（没达标的只在名额没用满时补位）
    "expression_auto_min_corpus": 20,    # 未学语料攒够 N 条就自动提取一次（0 = 关自动，只留面板手动）
    # 「先审后用」开关（拍-3；默认关 = 自动通过，学到就能用）
    "expression_review_first": False,

    # ---- S5 黑话（只做理解、不进回复；阈值/配额拍-16/19 —— 全部进参数中心可调，R4 登记先行） ----
    "jargon_enabled": True,                # 黑话总开关（理解注入 + 命中记账）
    "jargon_name_filter": True,            # 洞② 人名过滤：候选挖掘时滤掉昵称/群名（接 hf_person_names）
    "jargon_candidate_min_sessions": 2,    # 机械候选门槛：跨 ≥N 会话（拍-16 默认 2）
    "jargon_candidate_min_hits": 4,        # 机械候选门槛：出现 ≥N 次（拍-16 默认 4）
    "jargon_infer_daily_batches": 1,       # 含义推断每日批数（拍-16 ≤1；0 = 关自动推断）
    "jargon_try_daily_quota": 20,          # 试解读每日配额（拍-19 默认 20；0 = 不限）
    # 运行时计数（面板不暴露；跨天自动归零，写入走 store.set_config）
    "jargon_infer_day": "",
    "jargon_infer_day_count": 0,
    "jargon_try_day": "",
    "jargon_try_day_count": 0,

    # 单轮最多注入几条表达（勘误 A1：真·待新增键，0–10）
    "output_expr_max_inject": 5,
    # 输出拟人（分段发送 + 打字延迟）：默认全关，开了才动行为
    # 下面这四个键以前没进 DEFAULT_CONFIG：原生页能改、但面板保存会被静默丢弃、新装也没有默认值。
    "output_enabled": True,              # 输出收拾总开关（压缩空行 / 剥离好感标记）
    "output_typo_mode": "off",            # 错字拟人档位：off / light / medium
    # 错字率（错字机制的数值闸）：2026-10-16 勘误冻结为"真·待新增"键，范围 0–0.03
    "output_typo_rate": 0.01,
    "output_split_messages": True,      # 长回复拆成多条发（按语义边界切，短句不拆）
    "output_split_max": 4,              # 一轮最多拆几条（空行/一行一条/语义切分共用；超出并进最后一条）
    # 打字延迟：2026-10-16 裁决 —— 打字参数一律不动、**新装默认关**（库里已有值的一字不改）
    "output_typing_delay": False,        # 补发的后续条目按"打字时长"延迟（首条立即发）
    "output_typing_per_char": 0.3,       # 每个字的打字秒数
    "output_typing_max": 6,            # 单条最多等这么多秒
    "sticker_slot": "off",                # 表情包目录（预留槽位：回复出现 [表情] 时随机发一张；off=关闭）
    "voice_slot": "off",                  # 语音槽位：TTS provider id 或 session=跟随会话；off=关闭
    "persona_active_id": "",
    "proactive_enabled": False,
    "proactive_shadow": False,
    "proactive_idle_min": 120,
    "proactive_daily_cap": 3,             # 每个会话每天最多主动几条（多会话按会话数放大）
    "proactive_min_interval_min": 90,
    "proactive_quiet_start": "01:30",
    "proactive_quiet_end": "07:30",
    "proactive_min_zone": "",             # 空 = 不按好感档位设门槛
    "proactive_last_only": False,
    "proactive_style": "用你平时说话的方式，想说什么就说什么",
    # 主动名单：white=只对名单内主动 / black=只对名单内不主动 / off=不主动
    "proactive_mode_group": "black",
    "proactive_mode_private": "black",
    "proactive_auto_wl": "off",
    "proactive_wl_private": [],
    "proactive_bl_private": [],
    "proactive_wl_group": [],
    "proactive_bl_group": [],
    "proactive_use_persona": True,
    "proactive_context_memories": 6,     # 主动开口参考几条（3 太少，总用最新几条）
    "proactive_writeback": True,
    "proactive_note_enabled": True,
    "proactive_note_hours": 24,
    "proactive_note_max": 3,
    # 主动消息「第 2 层」提示词（本轮情境）：留空 = 用插件内置（PROACTIVE_LAYER2_TEMPLATE）。
    # 面板「主动 → 参数」页底部可展开编辑；支持 {zone}/{tone}/{idle} 占位符。
    "proactive_instruction": "",
    # 主动开口复用 AstrBot 完整提示词的缓存时效（分钟）：超过就降级为只吃人格，
    # 并在主动流水里标注 prompt_source=persona（防「静默降级」不可观测）。
    "proactive_prompt_max_age_min": 720,
    # ---- 基础名单（可见性）：黑名单里的群/人 = 对 bot 完全不存在 ----
    # 这是「主动消息名单」的地基：主动只能在这个可见集合里再收窄，不会给它扩容。
    # 可见性名单只有两种：black（名单内对 bot 不存在）/ white（只有名单内可见）
    # 默认 black + 空名单 = 全部可见；只有显式加入黑名单才隐藏。
    "conn_mode_group": "black",
    "conn_mode_private": "black",
    "conn_black_group": [],
    "conn_white_group": [],
    "conn_black_private": [],
    "conn_white_private": [],
    # 人物画像里要排除的 id（bot 自己会自动识别并落库，这里留着手工兜底）
    "person_exclude_ids": [],
    # 平台边界：可记录 = OneBot（QQ）实例 + AstrBot 自带 WebChat（ChatUI），两者都自动认得；
    # track_platforms 用来额外放行别的平台实例 id。
    # person_exclude_platforms 是排除名单（默认只排除 webui）；想让 ChatUI 也进记忆就把它从名单里删掉，
    # 想反过来让 ChatUI 对插件不可见，就把 "webchat" 加回来 —— 代码里不再写死任何平台。
    "track_platforms": [],
    "person_exclude_platforms": ["webui"],
    "recall_handling": "log_only",
    "recall_weaken_factor": 0.3,
    "recall_keep_days": 30,
    "recall_withdraw_reply": False,      # 撤回时连 bot 那份回复的本地痕迹一起撤（语料/待总结轮次）
    "recall_interrupt_reply": True,      # 撤回时打断"还在生成中"的那条回复（内容已作废，说完也白说）
    # 撤回反应（用户撤回一条消息 → **当场**走主动通道起一轮，让模型自己决定说什么/装没看见）
    "recall_react_enabled": False,        # 开关（面板「撤回」板块）
    "recall_react_group": "related",      # 群聊范围：related=只对与 bot 有互动的消息 / all=群里任何人都触发 / off=群聊不触发
    "recall_react_images": True,          # 把被撤回的图片一并交给模型看（原生多模态，最多 2 张）
    "recall_react_extra": "",             # 追加在撤回通知后面的自定义指示（留空=不加）
    "import_layers": ["documents", "atoms"],
    # 六个任务的模型分配：空字符串 = 跟随会话当前模型，只有显式选择才锁定
    # 人格自动进化（详细说明在 _conf_schema.json；面板也能改）
    "evolve_auto": True,
    "evolve_min_hours": 24,
    "evolve_min_memories": 6,
    "evolve_scope": "desc_style",
    "evolve_require_confirm": True,
    "evolve_pending_ttl_hours": 72,      # 待审提案超过这么多小时自动作废（不再永久阻塞进化）
    # 运行时时间戳（面板显示用，不在配置界面里暴露）
    "persona_last_evolve_at": 0.0,
    "persona_last_save_at": 0.0,
    # 任务 × 模型分配：空字符串 = 跟随会话当前模型，只有显式选择才锁定
    "model_memory_summary": "",
    "model_expression_extract": "",
    "model_person_profile": "",   # 画像生成（人物 L0 / 群聊 L1 共用；留空 = 跟随会话当前模型）
    "model_persona_evolve": "",   # 人格进化提案：留空 = 跟随会话当前模型（可锁便宜模型，不影响对话链路）
    "model_embedding": "",
    # 自动遗忘：久未被提及 → 归档；归档后再久未被提及且不重要 → 遗忘
    "forget_enabled": True,
    "archive_after_days": 90,
    "forget_after_days": 180,
    "forget_min_strength": 0.2,
    "forget_keep_poignancy": 6,
    "forget_batch_per_tick": 20,
    "model_forget": "",            # 记忆遗忘评估：配了就让模型判断"哪条真该忘"（见 lifecycle_tick）
    "auto_import_external": True,
    "model_rerank": "",            # 召回结果重排：配了就把召回候选交给它重排
    # ---- v2 记忆（批 A：只写不读；默认全关）----
    # 这三个键刻意不进 _conf_schema.json（走 CONFIG_INTERNAL_KEYS）：
    # 面板一改就会触发 AstrBot 重载插件，v2 开关需要热生效、只改 DB。
    "v2_enabled": False,             # v2 召回总开关（批 B 才开）
    "v2_shadow_write": False,        # v2 影子写（批 A 验证：episode 落库，不抽取）
    "v2_consolidate_enabled": False, # 批 C 巩固开关
    "v2_extract_on_boot": False,     # 启动时补跑 pending episode（一次性验证/补课用）
    # ---- 语言学习 MVP（原话样本 + 词表；设计见 _refs/语言学习蒸馏-20261020/方案-v1.3-MVP.md）----
    "expression_style_enabled": False,       # 新表达系统总开关（默认关，先看蒸馏产物）
    "expression_style_inject_mode": "legacy",  # legacy=旧 30 条规则 / new=原话样本
    "expression_style_samples": 1,           # 每轮注入条数（bot/human 各取 1）
    # 门控退避：连发到一定密度就安静一会儿（base 秒起步、指数增长、上限 max 秒；base=0 关闭）
    "gate_backoff_base": 15,
    "gate_backoff_max": 300,
    # ---- 补齐 9 个「只在面板生效」的开关 ----
    # 这些键原先没登记进默认配置表，而 save_config/seed 都以本表为准，
    # 于是面板/配置页改完也不生效（真·死开关），这里补全。
    "vision_enabled": True,          # 聊天里的图片也进记忆
    "vision_max_images": 4,          # 单条消息最多处理几张图（控成本）
    "profile_auto": True,            # 人物画像自动补齐
    "profile_min_memories": 5,       # 至少积累多少条记忆才自动生成
    "profile_refresh_hours": 72,     # 多久刷新一次
    "profile_batch_per_tick": 2,     # 每轮后台最多生成几个
    "burst_merge_enabled": True,     # 同一人连发多条时合并成一次回复
    "burst_merge_window": 3.0,       # 连发合并窗口（秒）
    "burst_merge_group": False,      # 群聊也按人合并（默认只私聊合并）
}

# ---------------- 参数护栏（勘误冻结：15 键 min/max + 8 条「一次改坏」硬拦 + 关系 / 格式校验） ----------------
# 这里是**保存路径的唯一校验源**：save_config() 先跑 validate_config_patch()，越界或踩到硬拦的
# 整批拒绝落库（原子），把原因返回给面板；面板不再自己抄一份范围（两份数字必然会漂移）。
# hard=True 的条目是「一次改坏」：改过去会让某个机制静默失效（打字节奏关掉 / 门控永久沉默 /
# 撤回档案当场删光 / 记忆再也学不进去 …），所以除了范围还要给一句人话原因。
PARAM_GUARDS: dict[str, dict[str, Any]] = {
    "output_typing_per_char": {"label": "每字秒数", "min": 0.2, "max": 2.0, "hard": True,
                               "why": "低于 0.2 秒等于关掉打字节奏（变成秒回）"},
    "output_typing_max": {"label": "单条延迟上限(秒)", "min": 3, "max": 30, "hard": True,
                          "why": "低于 3 秒等于关掉打字延迟（分段会立刻连发）"},
    "gate_threshold": {"label": "触发阈值", "min": 1, "max": 100, "hard": True,
                       "why": "高于 100 永远够不到 = 门控永久沉默"},
    "gate_backoff_base": {"label": "退避起步(秒)", "min": 1, "max": 60, "hard": True,
                          "why": "低于 1 秒等于退避失效（连发不再被拦）"},
    "gate_backoff_max": {"label": "退避上限(秒)", "min": 15, "max": 600,
                         "why": "上限要 ≥ 退避起步，否则起步值无处生效"},
    "recall_keep_days": {"label": "撤回流水保留(天)", "min": 1, "max": 365, "hard": True,
                         "why": "低于 1 天等于撤回档案当场删干净"},
    "memory_capture_max_per_run": {"label": "单次总结上限(条)", "min": 1, "max": 20, "hard": True,
                                   "why": "0 条等于记忆再也学不进去"},
    "memory_capture_min_interval": {"label": "总结最小间隔(秒)", "min": 0, "max": 3600},
    "forget_after_days": {"label": "遗忘周期(天)", "min": 1, "max": 365, "hard": True,
                          "why": "低于 1 天等于归档就遗忘，记忆当场清空"},
    "archive_after_days": {"label": "归档周期(天)", "min": 1, "max": 3650, "hard": True,
                           "why": "低于 1 天等于一次 tick 归档全库（旧版无护栏，填 0/负数会当场扫空）"},
    "forget_min_strength": {"label": "遗忘强度下限", "min": 0, "max": 1},
    "forget_keep_poignancy": {"label": "情绪保护线", "min": 0, "max": 10},
    "favour_daily_cap": {"label": "好感日上限", "min": 0, "max": 100},
    "favour_daily_half_life_days": {"label": "好感半衰期(天)", "min": 0, "max": 60, "hard": True,
                                    "why": "0 = 关闭衰减（合法）；负数会把好感算成指数增长"},
    "output_expr_max_inject": {"label": "表达注入条数", "min": 0, "max": 10},
    "output_typo_rate": {"label": "错字率", "min": 0, "max": 0.03},
    "output_split_max": {"label": "最多分几条", "min": 2, "max": 8,
                         "why": "少于 2 等于关掉分段；高于 8 会一轮刷屏"},
    # S5 黑话四洞之④：阈值/配额可调（拍-16/19）。R4 登记先行 —— 上页面之前先在护栏这里登记 min/max。
    "jargon_candidate_min_sessions": {"label": "黑话候选·最少会话数", "min": 1, "max": 50},
    "jargon_candidate_min_hits": {"label": "黑话候选·最少出现次数", "min": 1, "max": 500},
    "jargon_infer_daily_batches": {"label": "黑话推断·每日批数", "min": 0, "max": 20,
                                   "why": "0 = 关自动推断（候选仍照常记账；人工可随时改释义）"},
    "jargon_try_daily_quota": {"label": "试解读·每日配额", "min": 0, "max": 500},
    "proactive_quiet_start": {"label": "静默开始", "kind": "time",
                              "why": "格式必须是 HH:MM（解析不出来 = 静默窗失效）"},
    "proactive_quiet_end": {"label": "静默结束", "kind": "time",
                            "why": "格式必须是 HH:MM（解析不出来 = 静默窗失效）"},
    "proactive_note_hours": {"label": "主动知会回看(小时)", "min": 1, "max": 168},
    "proactive_note_max": {"label": "主动知会最多几条", "min": 1, "max": 10},
    # 自检补登记：这些数值键原先只有 schema 没有护栏，面板输入框没有上下界
    "cross_session_ttl_min": {"label": "跨会话上下文(分钟)", "min": 0, "max": 1440},
    "expression_auto_min_corpus": {"label": "自动提取语料阈值", "min": 0, "max": 200,
                                   "why": "0 = 关闭自动提取（只留面板手动）"},
    "expression_min_uses": {"label": "表达验证次数", "min": 1, "max": 50},
    "gate_window_seconds": {"label": "门控统计窗口(秒)", "min": 5, "max": 600},
    "memory_capture_turns": {"label": "每次总结轮数", "min": 1, "max": 50},
    "memory_decay_half_life_days": {"label": "记忆半衰期(天)", "min": 1, "max": 3650},
    "memory_evict_below": {"label": "记忆淘汰阈值", "min": 0, "max": 1},
    "memory_inject_budget_tokens": {"label": "注入预算(tokens)", "min": 100, "max": 8000},
    "memory_inject_top_k": {"label": "注入条数", "min": 1, "max": 50},
    "proactive_context_memories": {"label": "主动参考记忆条数", "min": 0, "max": 20},
    "proactive_daily_cap": {"label": "每日主动上限", "min": 0, "max": 50},
    "proactive_idle_min": {"label": "主动静默时长(分钟)", "min": 0, "max": 1440},
    "proactive_min_interval_min": {"label": "主动最小间隔(分钟)", "min": 1, "max": 1440},
    "proactive_prompt_max_age_min": {"label": "提示词最长缓存(分钟)", "min": 1, "max": 1440},
    "recall_weaken_factor": {"label": "撤回降权系数", "min": 0, "max": 1},
}
# 跨键关系校验：退避上限不得小于起步值（只带其中一个键时，另一个取库里的现值）。
PARAM_GUARD_RELATIONS: tuple[dict[str, Any], ...] = (
    {"keys": ("gate_backoff_max", "gate_backoff_base"), "hard": True},
)
# 门控改判的取值（行内两个按钮 + 撤销）→ 面板文案
GATE_VERDICTS: dict[str, str] = {
    "should_reply": "该回",
    "correct": "判定正确",
    "clear": "撤销改判",
}

# 学习诊断的跳过原因枚举（S4-4 / 拍-13：采用草案 7 值）。
# size_capped = reserved：现行提取没有「批次超上限整批丢弃」这条，先占位（对齐 v8 示例行），
# 一旦真加上限，写入侧直接用这个值，不必再改前端文案表。
EXPR_SKIP_REASONS: tuple[str, ...] = (
    "corpus_insufficient",   # 语料不足（还没攒到能学）
    "all_duplicate",         # 这批全是学过的说法（合并了但没新增）
    "model_error",           # 模型调用抛错
    "model_unavailable",     # 模型通道不可用
    "empty_parse",           # 模型输出解析出 0 条
    "banned_filtered",       # 被「不再学」名单拦下
    "size_capped",           # reserved：批次超上限整批丢弃（现行无此上限）
)
EXPR_SKIP_REASON_RESERVED: tuple[str, ...] = ("size_capped",)
EXPR_SKIP_REASON_TEXT: dict[str, str] = {
    "corpus_insufficient": "语料不够：群友/好友的原话还没攒够，先多聊几句",
    "all_duplicate": "这批全是已经学过的说法，没有新东西可学",
    "model_error": "模型调用失败（看「设置 · 模型分配」里的表达提取模型）",
    "model_unavailable": "模型通道不可用：没有可用的表达提取模型",
    "empty_parse": "模型没给出可用的表达（输出解析出 0 条）",
    "banned_filtered": "学到的东西在「不再学」名单里，按你的选择跳过了",
    "size_capped": "批次超过单次上限，整批丢弃（reserved，现行不会出现）",
}


def _skip_reason_enum() -> list[dict[str, Any]]:
    """跳过原因枚举 → 面板（带人话与 reserved 标记）。"""
    return [{"value": v, "reserved": v in EXPR_SKIP_REASON_RESERVED,
             "text": EXPR_SKIP_REASON_TEXT.get(v, v)} for v in EXPR_SKIP_REASONS]


# ---------------- S5 黑话：提示词与机械候选（拍-16/18/19） ----------------
_ANN_NO = {"no", "否", "不是", "none", "n", "无", "普通词", "不是黑话"}

# 含义推断（每日 ≤1 批，走「表达提取」离线槽位）：只产 pending，确认/否定全人工（洞③）。
JARGON_INFER_PROMPT_HEAD = (
    "你是「群内特殊用语」判别助手。下面是机器人从最近群聊里**机械统计**出的候选片段"
    "（已按出现次数排序，括号里是出现次数与涉及会话数）。\n"
    "请判断其中哪些是群友之间常用的特殊用语 / 黑话 / 梗，哪些只是普通词汇、语气词或人名。\n"
    "硬性要求：\n"
    "1. 每条候选输出一行，格式：候选词|简短释义（≤30 字）\n"
    "2. 判断为「不是黑话」的写：候选词|no（普通词、语气词、人名、地名一律写 no）\n"
    "3. 释义只写它在群里的实际含义；拿不准的一律写 no（宁缺勿滥）\n"
    "4. 只输出上述行：不要解释、不要编号、不要 markdown、不要代码块\n\n【候选】\n"
)

# 试解读（拍-19：默认只手动触发；单次 ≤1 条短请求、输出 ≤200 tokens；硬约束 = 不进回复库）。
JARGON_TRY_PROMPT = (
    "你在帮主人核对「黑话理解」功能。下面是一句群聊里的话，以及机器人此前学到的词条释义。\n"
    "请用一段人话说明「如果机器人在群里看到这句话，它会把这些词理解成什么」"
    "——重点解释特殊用语的含义与语气。\n"
    "硬性约束：\n"
    "1. 只解读、不回复：不要写成对这句话的应答，也不要建议怎么回\n"
    "2. 单次只输出一段解读，**上限 {cap} tokens**（约 150 字）；不要分点、不要 JSON、不要 markdown\n"
    "3. 词条没覆盖到的部分，一句「其余是普通表达」带过\n\n"
    "【这句话】\n{text}\n\n【学到的词条】\n{matched}\n"
)


def _jargon_scope_guess(session_ids: list[str], sample: dict[str, Any]) -> str:
    """候选词条归属（拍-17 取值域）：只有单会话出处时才细化到群/人，跨会话一律全局。

    单会话：群聊（session 形如 `平台:GroupMessage:群号`）→ '群·{gid}'；私聊 → '人·{uid}'。
    """
    uniq = sorted({str(s) for s in (session_ids or []) if str(s or "")})
    if len(uniq) == 1:
        sid = uniq[0]
        if ":GroupMessage:" in sid:
            return f"群·{sid.rsplit(':', 1)[-1]}"
        uid = str((sample or {}).get("user_id") or "").strip()
        return f"人·{uid}" if uid else "全局"
    return "全局"


_JARGON_NGRAM_MAX = 8   # 机械候选最长的连续子串（再长就是句子，不是词）


def _mine_jargon_candidates(
    rows: list[dict[str, Any]], *, min_sessions: int = 2, min_hits: int = 4,
    exclude_terms: set[str] | None = None, name_terms: set[str] | None = None,
    cap: int = JARGON_CANDIDATE_BATCH,
) -> list[dict[str, Any]]:
    """机械候选（拍-16 门槛 = 跨 ≥min_sessions 会话、出现 ≥min_hits 次）。

    机制（不引分词器，纯机械、可解释）：
      · 每条入站语料按「连续词字符」切段：ASCII 段整段算一个词；CJK 段取 2–8 字连续子串；
      · 纯数字段跳过（防电话号 / QQ 号噪声），单字不入候选；
      · **可延长片段剔除**：若某个候选总出现在一个更长的同样次数的片段里（例如整句被原样复读时
        切出来的窗口），它就不是独立的词 —— 直接丢，少送噪声给模型；
      · 同理：命中的短片段若与某个更长候选出现次数完全相同且被其包含 → 只留长的。
    人名过滤（洞②）：过滤开时，与已知昵称 / 群名（hf_person_names）相同的候选直接丢弃。
    """
    word_re = re.compile(r"\w+")
    excl = {str(x).strip().lower() for x in (exclude_terms or set()) if str(x).strip()}
    names = {str(x).strip().lower() for x in (name_terms or set()) if str(x).strip()}
    cnt: dict[str, int] = {}
    sess: dict[str, set[str]] = {}
    samples: dict[str, list[dict[str, Any]]] = {}
    run_cnt: dict[str, int] = {}
    all_runs: list[str] = []
    for r in rows or []:
        text = str(r.get("text") or "")
        if not text:
            continue
        sid = str(r.get("session_id") or "")
        snap = {"text": text, "ts": float(r.get("ts") or 0), "session_id": sid,
                "user_id": str(r.get("user_id") or ""), "user_name": str(r.get("user_name") or "")}
        for seg in word_re.findall(text):
            if seg.isdigit():
                continue                                  # 纯数字（号/编号/验证码）不当候选
            if seg.isascii():
                if 2 <= len(seg) <= 24:
                    cnt[seg.lower()] = cnt.get(seg.lower(), 0) + 1
                    sess.setdefault(seg.lower(), set()).add(sid)
                    smp = samples.setdefault(seg.lower(), [])
                    if len(smp) < 3:
                        smp.append(snap)
                continue
            run = seg
            if len(run) > _JARGON_NGRAM_MAX:
                run_cnt[run] = run_cnt.get(run, 0) + 1
                if len(all_runs) < 2000:
                    all_runs.append(run)
            for n in range(2, _JARGON_NGRAM_MAX + 1):
                if n > len(run):
                    break
                for i in range(0, len(run) - n + 1):
                    t = run[i:i + n]
                    cnt[t] = cnt.get(t, 0) + 1
                    sess.setdefault(t, set()).add(sid)
                    smp = samples.setdefault(t, [])
                    if len(smp) < 3:
                        smp.append(snap)

    def _is_extendable(t: str) -> bool:
        """该片段是否总被一个「同样次数、更长」的片段包着（= 不是独立词）。样本内近似判定。"""
        c_t = cnt.get(t, 0)
        for snap in samples.get(t) or []:
            body = str(snap.get("text") or "")
            start = body.find(t)
            while start >= 0:
                for L in range(len(t) + 1, _JARGON_NGRAM_MAX + 1):
                    for off in range(0, L - len(t) + 1):
                        s2 = start - off
                        if s2 < 0 or s2 + L > len(body):
                            continue
                        u = body[s2:s2 + L]
                        if u != t and t in u and cnt.get(u, 0) == c_t:
                            return True
                start = body.find(t, start + 1)
        # 整段被原样复读时（长于 n-gram 上限的句子）：该片段被更长整段包着的也要丢
        for run in all_runs:
            if len(run) > len(t) and t in run and run_cnt.get(run, 0) == c_t:
                return True
        return False

    out: list[dict[str, Any]] = []
    for term, n in cnt.items():
        if n < max(1, int(min_hits)) or len(sess.get(term, ())) < max(1, int(min_sessions)):
            continue
        if len(term) < 2 or term.isdigit():
            continue
        low = term.lower()
        if low in excl or low in names:                   # 洞②：人名/群名过滤（接上的能力）
            continue
        if _is_extendable(term):
            continue
        out.append({"term": term, "hits": n, "sessions": len(sess[term]),
                    "session_ids": sorted(sess[term])[:10], "samples": samples[term]})
    # 再去重：短片段是某个更长候选的子串、且两者次数完全相同 → 丢短的（留「整词」）
    out.sort(key=lambda c: (-len(c["term"]), -c["hits"]))
    kept: list[dict[str, Any]] = []
    for c in out:
        if any((c["term"] in k["term"]) and k["hits"] == c["hits"] and k["term"] != c["term"] for k in kept):
            continue
        kept.append(c)
    kept.sort(key=lambda c: (-c["hits"], -c["sessions"], -len(c["term"]), c["term"]))
    picked = kept[: max(1, int(cap))]
    for c in picked:
        c["scope"] = _jargon_scope_guess(c.get("session_ids") or [], (c.get("samples") or [{}])[0])
    return picked


def _as_bool(v: Any) -> bool:
    """宽松布尔：面板来的 checkbox 是 bool，配置库里也可能存成 "true"/1 —— 只有明确的真才算真。"""
    if isinstance(v, bool):
        return v
    return str(v if v is not None else "").strip().lower() in ("1", "true", "yes", "on")

_HM_RE = re.compile(r"^(\d{1,2}):(\d{2})$")


def _hm_to_minutes(v: Any) -> int | None:
    """HH:MM → 当天分钟数（H:MM 也认；与主动静默窗的解析口径一致）。认不出来给 None。"""
    m = _HM_RE.match(str(v if v is not None else "").strip())
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    return h * 60 + mi if 0 <= h < 24 and 0 <= mi < 60 else None


def _as_number(v: Any) -> float | None:
    """参数值 → float；bool / None / 非数字都不算（面板手滑清空输入框会送来空串）。"""
    if isinstance(v, bool) or v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f and f not in (float("inf"), float("-inf")) else None


def _num_text(v: Any) -> str:
    """给人看的数字：去掉多余的 .0。"""
    f = _as_number(v)
    if f is None:
        return str(v)
    return str(int(f)) if f == int(f) else str(f)


def validate_config_patch(patch: dict[str, Any] | None, current: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """保存前护栏校验：返回违规清单（空列表 = 全部通过）。纯函数，不改任何状态。

    current = 当前生效配置，用于跨键关系校验（本次没带的键按现值算）。
    """
    patch = patch or {}
    cur = current or {}
    out: list[dict[str, Any]] = []
    for key in patch:
        rule = PARAM_GUARDS.get(key)
        if not rule:
            continue
        value = patch[key]
        label = str(rule.get("label") or key)
        if rule.get("kind") == "time":
            if _hm_to_minutes(value) is None:
                out.append({"key": key, "label": label, "value": value, "hard": False,
                            "reason": f"{label}（{key}）只接受 HH:MM 格式，收到 {value!r}；{rule.get('why', '')}"})
            continue
        num = _as_number(value)
        if num is None:
            out.append({"key": key, "label": label, "value": value, "hard": bool(rule.get("hard")),
                        "reason": f"{label}（{key}）需要一个数字，收到 {value!r}"})
            continue
        lo, hi = rule.get("min"), rule.get("max")
        if (lo is not None and num < float(lo)) or (hi is not None and num > float(hi)):
            why = str(rule.get("why") or "").strip()
            out.append({
                "key": key, "label": label, "value": value, "min": lo, "max": hi,
                "hard": bool(rule.get("hard")),
                "reason": f"{label}（{key}）的允许范围是 {_num_text(lo)}–{_num_text(hi)}，收到 {_num_text(num)}"
                          + (f"；{why}" if why else ""),
            })
    # 跨键关系校验放在范围校验之后：同一个键已经因为范围/格式被拦过，就不再重复报一条
    # （面板 toast 只给前几条，重复条目会把真正的原因挤掉）。
    flagged = {str(v.get("key") or "") for v in out}
    for rel in PARAM_GUARD_RELATIONS:
        hi_key, lo_key = rel["keys"]
        if hi_key not in patch and lo_key not in patch:
            continue
        if hi_key in flagged:
            continue
        hi_v = _as_number(patch.get(hi_key, cur.get(hi_key)))
        lo_v = _as_number(patch.get(lo_key, cur.get(lo_key)))
        if hi_v is None or lo_v is None or hi_v >= lo_v:
            continue
        out.append({
            "key": hi_key, "label": str(PARAM_GUARDS[hi_key].get("label") or hi_key), "value": hi_v,
            "min": lo_v, "max": PARAM_GUARDS[hi_key].get("max"), "hard": bool(rel.get("hard")),
            "reason": f"{PARAM_GUARDS[hi_key]['label']}（{_num_text(hi_v)}）小于 {lo_key}（{_num_text(lo_v)}）："
                      f"退避上限必须 ≥ 起步值，否则起步值无处生效",
        })
    return out


def guard_reject_message(violations: list[dict[str, Any]]) -> str:
    """违规清单 → 面板可见的一句话（写着哪几个键、为什么）。"""
    if not violations:
        return ""
    parts = [str(v.get("reason") or v.get("key") or "") for v in violations[:4]]
    if len(violations) > 4:
        parts.append(f"等共 {len(violations)} 项")
    return "参数被护栏拦下（本批一个字都没写入）：" + "；".join(parts)


def guards_payload() -> dict[str, Any]:
    """护栏清单（面板据此设输入框上下界、标「已拦下」）—— 后端是唯一真源。"""
    return {
        "keys": {k: dict(v) for k, v in PARAM_GUARDS.items()},
        "relations": [{"keys": list(r["keys"]), "hard": bool(r.get("hard"))} for r in PARAM_GUARD_RELATIONS],
        "hard_count": sum(1 for v in PARAM_GUARDS.values() if v.get("hard")),
    }


ZONE_TEXT = {
    "DEVOTED": "可以撒娇、可以主动、可以占话题",
    "CLOSE": "可以亲昵、可以未 @ 接话、能开玩笑、深夜也秒回",
    "FAMILIAR": "正常参与、有梗就接、不主动私聊",
    "NEUTRAL": "被点名才答、礼貌但不热络",
    "COLD": "字数极简、不主动追问",
    "HOSTILE": "字数极简、被点名才开口、不带情绪波动",
}

PersonaProvider = Callable[..., Awaitable[Any]]



# 各域面板只暴露引擎真正读取的配置键（避免出现假开关）
CONFIG_REMOVED_KEYS = frozenset({
    # 已删除的键：自检/补登记永远不要再把它们复活
    "proactive_memory_cooldown_hours", "proactive_exclude_relational",
    "proactive_memory_types", "proactive_min_chars", "proactive_retry_invalid",
    "proactive_require_full_prompt", "proactive_dedup_enabled",
    "proactive_dedup_hours", "proactive_dedup_threshold",
    "proactive_dedup_semantic", "proactive_dedup_semantic_threshold",
    "proactive_lists_enabled",
})

CONFIG_INTERNAL_KEYS = frozenset({
    # 运行时状态：不面向用户，不进 schema / 面板
    "config_version", "graph_ui", "import_layers", "proactive_plan",
    "persona_last_evolve_at", "persona_last_save_at",
    "jargon_infer_day", "jargon_infer_day_count",
    "jargon_try_day", "jargon_try_day_count",
    # v2 开关：只走 DB（面板改会触发插件重载），不登记 schema/域/面板
    "v2_enabled", "v2_shadow_write", "v2_consolidate_enabled", "v2_extract_on_boot",
    "expression_style_enabled", "expression_style_inject_mode", "expression_style_samples",
})


def _is_internal_config_key(key: str) -> bool:
    """运行时状态键（不要求 schema / 面板 / 护栏登记）。"""
    k = str(key or "")
    if k in CONFIG_INTERNAL_KEYS:
        return True
    return bool(re.search(r"(?:_day|_day_count)$", k))


# 只在 AstrBot 原生配置页暴露、刻意不放进面板「参数」页的键。
# 新增键如果不在这个集合里，自检会按前缀自动补进面板域（防「加了键但面板看不见」）。
CONFIG_NATIVE_ONLY_KEYS = frozenset({
    "archive_after_days", "auto_import_external",
    "burst_merge_enabled", "burst_merge_group", "burst_merge_window",
    "evolve_auto", "evolve_min_hours", "evolve_min_memories",
    "evolve_pending_ttl_hours", "evolve_require_confirm", "evolve_scope",
    "forget_after_days", "forget_batch_per_tick", "forget_enabled",
    "forget_keep_poignancy", "forget_min_strength",
    "memory_persona_isolated",
    "model_embedding", "model_expression_extract", "model_forget",
    "model_memory_summary", "model_person_profile", "model_persona_evolve", "model_rerank",
    "output_enabled", "output_typo_mode",
    "profile_auto", "profile_batch_per_tick", "profile_min_memories", "profile_refresh_hours",
    "sticker_slot",
    "sys_prompt_expression_extract", "sys_prompt_forget", "sys_prompt_group_profile",
    "sys_prompt_memory_summary", "sys_prompt_person_profile", "sys_prompt_persona_evolve",
    "vision_enabled", "vision_max_images", "voice_slot",
})

# 新键按前缀自动归域（只用于自检补登记，不影响已有原生专属键）
CONFIG_PANEL_PREFIXES = (
    ("proactive_", "proactive"),
    ("memory_", "memory"),
    ("output_", "output"),
    ("recall_", "recall"),
    ("favour_", "favour"),
    ("gate_", "gate"),
    ("expression_", "expression"),
    ("jargon_", "expression"),
    ("persona_", "persona"),
)


def _domain_for_key(key: str) -> str:
    for prefix, domain in CONFIG_PANEL_PREFIXES:
        if str(key).startswith(prefix):
            return domain
    return ""


CONFIG_DOMAINS: dict[str, list[str]] = {
    "gate": ["gate_enabled", "gate_shadow", "gate_threshold", "gate_window_seconds",
             "gate_backoff_base", "gate_backoff_max"],
    "memory": ["memory_summary_prompt", "memory_inject_enabled", "memory_inject_top_k", "memory_inject_budget_tokens",
               "memory_decay_half_life_days", "memory_evict_below",
               "memory_capture_enabled", "memory_capture_turns",
               "memory_capture_min_interval", "memory_capture_max_per_run",
               "cross_session_enabled", "cross_session_ttl_min", "cross_session_memory"],
    "expression": ["expression_enabled", "expression_min_uses", "expression_auto_min_corpus",
                   "output_expr_max_inject", "expression_review_first",
                   # S5 黑话（同一「语言学习」页的块② 与同域参数；阈值/配额拍-16/19 可调）
                   "jargon_enabled", "jargon_name_filter", "jargon_candidate_min_sessions",
                   "jargon_candidate_min_hits", "jargon_infer_daily_batches", "jargon_try_daily_quota"],
    "favour": ["favour_enabled", "favour_settle_mode", "favour_daily_cap", "favour_daily_half_life_days", "favour_tag_required"],
    "recall": ["recall_handling", "recall_weaken_factor", "recall_keep_days", "recall_withdraw_reply",
               "recall_interrupt_reply",
               "recall_react_enabled", "recall_react_group", "recall_react_images", "recall_react_extra"],
    "proactive": ["proactive_enabled", "proactive_shadow", "proactive_idle_min", "proactive_daily_cap",
                  "proactive_min_interval_min", "proactive_quiet_start", "proactive_quiet_end",
                  "proactive_min_zone", "proactive_last_only", "proactive_style",
                  "proactive_instruction", "proactive_prompt_max_age_min",
                  "proactive_mode_group", "proactive_mode_private",
                  "proactive_auto_wl", "proactive_wl_private",
                  "proactive_bl_private", "proactive_wl_group", "proactive_bl_group",
                  "proactive_use_persona", "proactive_context_memories",
                  "proactive_writeback", "proactive_note_enabled",
                  "proactive_note_hours", "proactive_note_max"],
    "output": ["output_split_messages", "output_split_max", "output_typing_delay", "output_typing_per_char",
               "output_typing_max", "output_typo_rate"],
    "persona": ["persona_active_id"],
    "contacts": ["conn_mode_group", "conn_black_group", "conn_white_group",
                 "track_platforms", "person_exclude_platforms",
                 "conn_mode_private", "conn_black_private", "conn_white_private",
                 "person_exclude_ids"],
    "master": ["master_switch", "session_context_enabled"],
}
ZONE_RANK = {"HOSTILE": 0, "COLD": 1, "NEUTRAL": 2, "FAMILIAR": 3, "CLOSE": 4, "DEVOTED": 5}

# ---- 主动消息「第 2 层」提示词（本轮情境）----
# 用户可以在面板「主动 → 参数」页底部展开自定义（配置键 proactive_instruction，留空 = 用这份内置）。
# 支持占位符：{zone}=关系区间 / {tone}=语气线 / {idle}=距对方上次说话的分钟数。
# 改动这里 = 改所有人开箱默认；用户自定义优先。
PROACTIVE_LAYER2_TEMPLATE = chr(10).join([
    "【本轮情境】你要主动给对方发一条消息，不是回复某条消息。",
    "现在是 {now}。关系：{zone} · 语气：{tone} · 距对方上次说话：{idle}。",
    "可以是一句问候、一件你刚想到的事、一个提问；要提旧事就说得出来历和时间，想不起来就不提。",
    "也可以分成两三条发（空行分段）。",
    "用你平时说话的方式；想写动作就写在括号里；不要解释你为什么突然开口，也不要提到“记忆”这两个字。",
    "真的不想说，就只输出 [沉默]。",
])
# 主动开口注入记忆块时追加的提醒：防「旧事当新闻」「对方的事说成你的事」「偏好被当指令」。
PROACTIVE_MEM_NOTE = (
    "（这些是你的长期记忆，方括号里是「大约多久以前 · 发生时间」。记忆里的「TA/对方」= 现在这个对话对象，"
    "「我」= 你自己，按这个对应理解别反过来。只能引用这里明确写过的东西："
    "没写的地点、计划、约定、细节就是没有，不许补；拿不准就不提。"
    "引用时用自己的话，不要整句照抄。）"
)

# ---------------- 原生白名单快照（名单/可见性统一来源） ----------------
_NATIVE_SYS: dict[str, Any] = {"provider": None}


def _native_vis() -> dict[str, Any] | None:
    """取 AstrBot 原生白名单；桥未注册/读取失败 → None（调用方退回旧逻辑，不放大权限）。"""
    fn = (_NATIVE_SYS.get("provider") or {}).get("native_visibility_read")
    if not callable(fn):
        return None
    try:
        nat = fn() or {}
        if nat.get("err"):
            return None
        return {"enable": bool(nat.get("enable")), "ids": [str(x) for x in (nat.get("ids") or [])]}
    except Exception:                                           # noqa: BLE001
        return None


def contact_ids(contacts: Any) -> list[str]:
    """把平台联系人结构摊平成 id 列表。

    ⚠️ 真机踩过的坑（2026-10-11）：``system["contacts"]`` 返回的是 **dict**
    ``{platforms, groups, friends, admins, bots, errors, member_names,
       admin_in_friends, admin_not_in_friends}``。
    黑名单写回分支曾直接 ``for row in contacts`` 迭代 —— dict 迭代拿到的是 **键名**，
    于是这 9 个键名被当成"联系人"写进了 AstrBot 原生白名单（真机 id_whitelist 里就躺着它们）。
    """
    out: list[str] = []

    def push(value: Any) -> None:
        s = str(value or "").strip()
        if s and s not in out:
            out.append(s)

    if isinstance(contacts, dict):
        for key in ("friends", "groups", "admins", "bots"):
            for row in (contacts.get(key) or []):
                if isinstance(row, str):
                    push(row)
                elif isinstance(row, dict):
                    push(row.get("user_id") or row.get("group_id") or row.get("id") or row.get("ident"))
    elif isinstance(contacts, (list, tuple, set)):
        for row in contacts:
            if isinstance(row, str):
                push(row)
            elif isinstance(row, dict):
                push(row.get("id") or row.get("ident") or row.get("user_id") or row.get("group_id"))
    return out


# 原生白名单 id 匹配（裸号 / 完整会话 id 两种写法都认）
def _native_id_match(ident: str, platform: str, kind: str, ids: list[str] | set[str]) -> bool:
    """原生白名单匹配：既认裸号，也认 `平台:GroupMessage:号` / `平台:FriendMessage:号`。

    用户按 AstrBot `/sid` 提示填的是完整会话 id，旧实现只拿裸号比，会把整个群误判成
    「不在原生白名单里」——消息在入口就被丢掉，面板还看不到这个群。
    """
    ident = str(ident or "").strip()
    if not ident:
        return False
    cands = {ident}
    plat = str(platform or "").strip()
    if plat:
        mtype = "GroupMessage" if kind == "group" else "FriendMessage"
        cands.add(f"{plat}:{mtype}:{ident}")
    for raw in ids or []:
        s = str(raw or "").strip()
        if not s:
            continue
        if s in cands:
            return True
        # 完整会话 id 的尾段就是裸号；反向也认（名单里可能只存了裸号）
        if ":" in s and s.rsplit(":", 1)[-1] == ident:
            return True
        if ":" in ident and ident.rsplit(":", 1)[-1] == s:
            return True
    return False


# 名单归一化：旧库可能把列表存成逗号字符串
def _as_id_list(value: Any) -> list[str]:
    """把配置里的名单统一成 list[str]：兼容旧库把列表存成 '123,456' 字符串。"""
    if value is None:
        return []
    if isinstance(value, str):
        return [x for x in re.split(r"[,，\s]+", value.strip()) if x]
    if isinstance(value, (list, tuple, set)):
        out: list[str] = []
        for x in value:
            s = str(x).strip()
            if s and s not in out:
                out.append(s)
        return out
    return [str(value).strip()] if str(value).strip() else []


def _cfg_num(cfg: dict[str, Any], key: str, default: float, *, as_int: bool = False) -> float:
    """读配置数字：空/非法值回退默认，不让一次脏配置把整条主动链路炸掉。"""
    v = cfg.get(key)
    if v is None or (isinstance(v, str) and not v.strip()):
        return default
    try:
        return int(float(v)) if as_int else float(v)
    except (TypeError, ValueError):
        logger.warning(f"[StarHeart] 配置 {key}={v!r} 不是数字，按默认 {default} 处理")
        return default


def _cfg_int(cfg: dict[str, Any], key: str, default: int) -> int:
    return int(_cfg_num(cfg, key, float(default), as_int=True))


def _cfg_float(cfg: dict[str, Any], key: str, default: float) -> float:
    return float(_cfg_num(cfg, key, float(default)))


# 联系人结构体的键名：它们**不是**会话 id，任何写名单的路径都不该收
CONTACT_SHAPE_KEYS = {
    "platforms", "groups", "friends", "admins", "bots", "errors",
    "member_names", "admin_in_friends", "admin_not_in_friends",
}


class StarHeartService:
    def __init__(
        self,
        store: Store,
        data_root: str | Path,
        persona_provider: dict[str, PersonaProvider] | None = None,
        system_provider: dict[str, PersonaProvider] | None = None,
    ) -> None:
        self.store = store
        self.importer = LivingMemoryImporter(store, data_root)
        self.persona = persona_provider or {}
        self.system = system_provider or {}
        _NATIVE_SYS["provider"] = self.system      # 供 classmethod visible() 读原生白名单
        self._import_task: asyncio.Task | None = None
        self._import_state: dict[str, Any] = {"phase": "idle"}
        self._import_subs: list[asyncio.Queue] = []
        self._import_watch = False

    # ---------------- 配置 ----------------

    # ---------------- 撤回 ----------------
    async def recalls(self, limit: int = 200) -> dict[str, Any]:
        return {"items": await self.store.list_recalls(limit), "config": await self.config()}

    async def handle_recall(self, info: dict[str, Any]) -> dict[str, Any]:
        """用户撤回了消息：命中记忆就降权标 stale，否则只记录事件。"""
        cfg = await self.config()
        ref = str(info.get("message_id") or "")
        mode = str(cfg.get("recall_handling") or "log_only")
        if mode not in ("log_only", "weaken", "archive", "delete"):
            mode = "log_only"                               # 非法值不静默变形，按最保守的 log_only
        if mode == "delete":                                # 兼容旧值：真删 → 归档
            mode = "archive"
        matched = 0
        action = "log_only"
        if ref:
            sid_recall = str(info.get("session_id") or "")
            hits = await self.store.memories_by_source_ref(ref, sid_recall)
            ids = [h["id"] for h in hits]
            # 自动总结的记忆：source_ref 是 capture:{session}，按 message_id 精确查不到。
            # 侧车表 hf_memory_msg 是唯一反查路径；两路结果合并去重。
            for sid_x in await self.store.memory_ids_by_message_id(ref):
                if sid_x not in ids:
                    ids.append(sid_x)
            if ids and mode == "weaken":
                weaken_cfg = cfg.get("recall_weaken_factor")
                matched = await self.store.mark_memories_stale(
                    ids, 0.3 if weaken_cfg is None else float(weaken_cfg)
                )
                action = "weaken_memories"
            elif ids and mode == "archive":      # 注意：上面已把旧值 delete 归一成 archive，
                                                 # 这里再判 delete 会让归档分支变成死代码（已踩过）

                # 用户定：不物理删除，改成归档（可在面板「复活历史记忆」找回，被提及也会自动拉回）
                matched = await self.store.archive_memories(ids, float(cfg.get("recall_weaken_factor") or 0.3))
                action = "archive_memories"
            else:
                matched = len(ids)                         # log_only：命中数照报，一条都不动
        entry = {
            "session_id": info.get("session_id") or "",
            "user_id": info.get("user_id") or "",
            "notice_type": info.get("notice_type") or "",
            "message_id": ref,
            "operator_id": info.get("operator_id") or "",
            "matched": matched,
            "action": action,
            "detail": {
                "handling": mode,
                "weaken_factor": cfg.get("recall_weaken_factor"),
                # 撤回正文（谁 + 撤回了什么 + 图片网址）：面板要显示完整内容，不是只记一条流水
                "recalled": info.get("recalled") or {},
            },
        }
        await self.store.log_recall(entry)
        await self.store.audit("recall.handle", f"{entry['notice_type']} matched={matched}")
        return {"matched": matched, "action": action}

    async def recall_log_cleanup(self) -> int:
        """撤回流水按 recall_keep_days 滚动清理（原配置存了但从未执行）。"""
        cfg = await self.config()
        days = float(cfg.get("recall_keep_days") or 30)
        if days <= 0:
            return 0
        cutoff = time.time() - days * 86400
        cur = await self.store.db.execute("DELETE FROM hf_recall_log WHERE ts < ?", (cutoff,))
        await self.store.db.commit()
        return int(cur.rowcount or 0)

    # ---------------- 主动消息名单（私聊/群聊各自独立） ----------------
    @staticmethod
    def session_kind(session_id: str) -> tuple[str, str]:
        """会话字符串 platform:MessageType:id → ("group"|"private", id)。

        WebChat 尾段 `webchat!<用户名>!<会话id>` → 归一化成用户名（账户名才是「人」）。
        """
        parts = str(session_id or "").split(":")
        if len(parts) >= 3:
            mtype, ident = parts[-2].lower(), parts[-1]
            if ident.lower().startswith("webchat!") and ident.count("!") >= 2:
                ident = ident.split("!")[1].strip() or ident
            if "group" in mtype:
                return "group", ident
            if "friend" in mtype or "private" in mtype:
                return "private", ident
            return "other", ident
        return "other", str(session_id or "")

    # ---------------- 基础名单（可见性）：黑名单里的群/人对 bot 完全不存在 ----------------
    @staticmethod
    def _conn_keys(kind: str, mode: str) -> str:
        """kind: group|private，mode: black|white → 配置键名。"""
        k = "group" if kind == "group" else "private"
        m = "black" if mode == "black" else "white"
        return f"conn_{m}_{k}"

    @classmethod
    def visible(cls, kind: str, ident: str, cfg: dict[str, Any], platform: str = "") -> tuple[bool, str]:
        """这个群/这个人对 bot 是否「存在」。

        ``platform``：会话的平台实例 id。**webchat（ChatUI）不受名单约束** —— 它是操作者自己的
        控制台，而白名单是按 QQ 号/群号组织的（里面根本没有它的账户名，面板也选不出来）。
        ChatUI 进不进插件由平台级开关 person_exclude_platforms 决定（见 main._is_trackable_session）。

        判定来源：群聊只读尊重 AstrBot 原生白名单；私聊只看插件本地名单。
        原生语义：**未启用 / 名单为空 = 全部放行（等于不触发）**；启用且非空 = 仅名单内可见。
        本地名单永远优先：黑名单内直接不可见；白名单为空 = 谁都不回（fail-closed）。

        黑名单：名单内 → 不存在（不论当前是 black 还是 white 模式）；
        白名单：名单外 → 不存在（名单为空 = 谁都不回，fail-closed）；
        off：全都存在。
        """
        if kind not in ("group", "private"):
            return True, ""
        if not ident:
            return True, ""
        if str(platform or "").strip().lower() == "webchat":
            return True, "webchat（ChatUI 控制台）不受名单约束"
        mode = str(cfg.get(f"conn_mode_{kind}") or "black").lower()
        if mode not in ("black", "white"):
            mode = "black"
        # 黑名单永远优先：切到 white 模式也不该把曾经拉黑的人放出来
        black_ids = _as_id_list(cfg.get(cls._conn_keys(kind, "black")))
        if ident in black_ids:
            return False, "在可见性黑名单里"
        ids = _as_id_list(cfg.get(cls._conn_keys(kind, mode)))
        # 本地白名单为空 = 谁都不回：这条要压过原生白名单（用户在本插件里的显式选择）
        if mode == "white" and not ids:
            return False, "可见性白名单为空（谁都不回）"
        nat = _native_vis()
        # 只有群聊用原生白名单：原生名单是「群号+QQ 号」一份平表，
        # 私聊若也套它，只填了群号时会把所有私聊判成名单外。私聊一律走本地名单。
        if nat is not None and kind == "group":
            if not nat["enable"] or not nat["ids"]:
                return True, "原生白名单未启用/为空（全部放行）"
            if _native_id_match(ident, platform, kind, nat["ids"]):
                return True, "在原生白名单里"
            return False, "不在原生白名单里"
        # 原生桥不可用：按本地名单退回
        if mode == "white":
            if not ids:
                return False, "可见性白名单为空（谁都不回）"
            if ident in ids:
                return True, ""
            return False, "不在可见性白名单里"
        return True, ""

    async def contact_filter(self) -> dict[str, Any]:
        """基础名单现状：模式 + 两份名单 + 每个平台联系人的「可见/不可见」判定。"""
        cfg = await self.config()
        out: dict[str, Any] = {"modes": {}, "black": {}, "white": {}, "counts": {}, "hidden": {}}
        for kind in ("group", "private"):
            mode = str(cfg.get(f"conn_mode_{kind}") or "black").lower()
            if mode not in ("black", "white"):
                mode = "black"
            black = _as_id_list(cfg.get(self._conn_keys(kind, "black")))
            white = _as_id_list(cfg.get(self._conn_keys(kind, "white")))
            out["modes"][kind] = mode
            out["black"][kind] = black
            out["white"][kind] = white
            out["counts"][kind] = {"black": len(black), "white": len(white)}
            # 白名单为空 = 谁都不回：面板要能一眼看见这个「静默」状态
            out.setdefault("silent", {})[kind] = bool(mode == "white" and not white)
        try:
            hidden = await self.store.hidden_contacts()
            out["hidden"] = {"group": hidden.get("group", []), "private": hidden.get("private", [])}
        except Exception:
            out["hidden"] = {"group": [], "private": []}
        # 给平台名单打分（面板直接显示「可见/不可见」），并把昵称/群名一并带上
        verdicts: dict[str, Any] = {}
        names: dict[str, str] = {}
        try:
            data = await self.contacts(False)
            for kind, rows in (("group", data.get("groups") or []), ("private", data.get("friends") or [])):
                for row in rows:
                    ident = str(row.get("id") or row.get("group_id") or row.get("user_id") or "")
                    if not ident:
                        continue
                    # 好友的名称字段是 nickname / remark，群里是 name —— 只认 name 就会满屏号码
                    label = str(row.get("name") or row.get("nickname") or row.get("remark") or "").strip()
                    if label:
                        names[f"{kind}:{ident}"] = label
                    vis, why = self.visible(kind, ident, cfg, platform=str(row.get("platform") or ""))
                    verdicts[f"{kind}:{ident}"] = {"visible": vis, "why": why, "name": label}
        except Exception:
            pass
        out["verdicts"] = verdicts
        out["names"] = names
        return out

    async def set_contact_filter(self, kind: str, mode: str, ids: list[str]) -> dict[str, Any]:
        """整表写入某一边的名单（面板就是「多选 → 确定」这一步）。

        只写插件本地：AstrBot 原生白名单是全局的（群号 + QQ 号共用一份，还会
        影响其他插件与私聊），从面板替用户改它太容易把别的会话一起挡掉。
        原生名单仍会被只读尊重（visible 里对群聊生效），但插件不再写它。
        """
        if kind not in ("group", "private"):
            raise ValueError(f"unknown kind: {kind}")
        mode = str(mode or "black").lower()
        if mode not in ("black", "white"):
            raise ValueError(f"unknown mode: {mode}")
        clean: list[str] = []
        _junk: list[str] = []
        for x in ids or []:
            x = str(x).strip()
            if not x:
                continue
            if x in CONTACT_SHAPE_KEYS:               # ← 结构体键名不是会话 id，永远不收
                _junk.append(x)
                continue
            if x not in clean:
                clean.append(x)
        if _junk:
            logger.warning(
                f"[StarHeart] 名单里混进了联系人结构体键名，已丢弃：{', '.join(_junk)}"
            )
        if mode == "white" and not clean:
            # fail-closed 之后「白名单+空」= 该类全局静默；这不该由一次误点写入。
            raise ValueError("可见性白名单不能为空；想静音请用「不主动」或关掉总开关")
        await self.store.set_config(f"conn_mode_{kind}", mode)
        await self.store.set_config(self._conn_keys(kind, mode), clean)
        await self.store.audit(f"contacts.set_{kind}_{mode}", f"{len(clean)} 项（本地）")
        return await self.contact_filter()


    def _list_key(self, kind: str, mode: str) -> str:
        return f"proactive_{'wl' if mode == 'allow' else 'bl'}_{'group' if kind == 'group' else 'private'}"

    @staticmethod
    def _auto_wl_allows(auto_mode: str, kind: str, zone: str) -> bool:
        """「自动白名单」策略判定 —— 让面板三选项 off / close / familiar 都能走到分支。

        close    = 只自动放行已经亲近的人（好感档 CLOSE 及以上）
        familiar = 熟人就放行（FAMILIAR 及以上）
        both / group / private = 旧配置的兼容值，按会话类型放行
        """
        if auto_mode in ("", "off"):
            return False
        if auto_mode == "close":
            return ZONE_RANK.get(zone, 2) >= ZONE_RANK["CLOSE"]
        if auto_mode == "familiar":
            return ZONE_RANK.get(zone, 2) >= ZONE_RANK["FAMILIAR"]
        return auto_mode in ("both", kind)

    @staticmethod
    def proactive_mode(cfg: dict[str, Any], kind: str) -> str:
        """主动名单模式：white（只对名单内主动）/ black（只对名单内不主动）/ off（不主动）。"""
        k = "group" if kind == "group" else "private"
        mode = str(cfg.get(f"proactive_mode_{k}") or "black").lower()
        if mode in ("black", "white", "off"):
            return mode
        return "black"

    @classmethod
    def _proactive_rule_tables(cls, cfg: dict[str, Any]) -> dict[str, Any]:
        """扫描用的轻量名单（纯配置，不打平台接口）。"""
        return {
            "modes": {k: cls.proactive_mode(cfg, k) for k in ("group", "private")},
            "wl_private": _as_id_list(cfg.get("proactive_wl_private")),
            "bl_private": _as_id_list(cfg.get("proactive_bl_private")),
            "wl_group": _as_id_list(cfg.get("proactive_wl_group")),
            "bl_group": _as_id_list(cfg.get("proactive_bl_group")),
        }

    async def _whitelist_sessions(self, cfg: dict[str, Any],
                                  sessions: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
        """白名单里显式点过的会话（哪怕没聊过）也纳入候选——否则名单没意义。

        sessions：baseline 里的真实会话，用来推断每个群/人属于哪个 OneBot 实例；
        多实例时不能拿 bots[0] 的 platform 硬拼，否则会发到错误实例。
        """
        if self.proactive_mode(cfg, "group") != "white" and self.proactive_mode(cfg, "private") != "white":
            return []
        platform = ""
        try:
            fn = self.system.get("contacts")
            if callable(fn):
                data = await fn(False)
                bots = data.get("bots") or []
                if bots:
                    platform = str(bots[0].get("platform") or "")
        except Exception:
            platform = ""
        platform = platform or "aiocqhttp"
        prefix_by_id: dict[str, str] = {}
        for _s in sessions or []:
            _sid = str(_s.get("session_id") or "")
            if not _sid:
                continue
            _k, _ident = self.session_kind(_sid)
            if _ident:
                prefix_by_id.setdefault(f"{_k}:{_ident}", _sid.split(":", 1)[0])
        out: list[dict[str, Any]] = []
        for kind, key in (("group", "proactive_wl_group"), ("private", "proactive_wl_private")):
            if self.proactive_mode(cfg, kind) != "white":
                continue
            for ident in _as_id_list(cfg.get(key)):
                ident = str(ident).strip()
                if not ident:
                    continue
                mtype = "GroupMessage" if kind == "group" else "FriendMessage"
                _plat = prefix_by_id.get(f"{kind}:{ident}") or platform
                out.append({
                    "session_id": f"{_plat}:{mtype}:{ident}",
                    "user_id": "" if kind == "group" else ident,
                    "last_user_ts": 0.0, "msgs": 0, "sent_today": 0, "last_proactive_ts": 0.0,
                    "from_whitelist": True,
                })
        return out

    async def contact_names(self) -> dict[str, str]:
        """把平台联系人整理成 {"group:群号": 群名, "private:QQ": 昵称}。

        为什么要它：面板上到处只显示 QQ 号/群号，一屏数字谁也认不出是谁 ——
        昵称和群名才是「一眼辨别」的关键（好友的字段是 nickname/remark，群是 name，别只认 name）。
        """
        names: dict[str, str] = {}
        try:
            data = await self.contacts(False)
            for kind, rows, key in (("group", data.get("groups") or [], "group_id"), ("private", data.get("friends") or [], "user_id")):
                for row in rows:
                    ident = str(row.get(key) or row.get("id") or "").strip()
                    if not ident:
                        continue
                    label = str(row.get("name") or row.get("nickname") or row.get("remark") or "").strip()
                    if label:
                        names[f"{kind}:{ident}"] = label
        except Exception:
            pass
        try:
            for fav in await self.store.list_favours():
                uid = str(fav.get("user_id") or "").strip()
                if uid and fav.get("name") and f"private:{uid}" not in names:
                    names[f"private:{uid}"] = str(fav["name"])
        except Exception:
            pass
        return names

    async def proactive_rules(self) -> dict[str, Any]:
        cfg = await self.config()
        rules = {
            "modes": {k: self.proactive_mode(cfg, k) for k in ("group", "private")},
            "base_modes": {k: (str(cfg.get(f"conn_mode_{k}") or "black") if str(cfg.get(f"conn_mode_{k}") or "black") in ("black", "white") else "black") for k in ("group", "private")},
            "base_black": {k: _as_id_list(cfg.get(self._conn_keys(k, "black"))) for k in ("group", "private")},
            "base_white": {k: _as_id_list(cfg.get(self._conn_keys(k, "white"))) for k in ("group", "private")},
            "auto_wl": str(cfg.get("proactive_auto_wl") or "off"),
            "wl_private": _as_id_list(cfg.get("proactive_wl_private")),
            "bl_private": _as_id_list(cfg.get("proactive_bl_private")),
            "wl_group": _as_id_list(cfg.get("proactive_wl_group")),
            "bl_group": _as_id_list(cfg.get("proactive_bl_group")),
        }
        base = await self.store.proactive_baseline(self._day_start_ts(cfg))
        names = await self.contact_names()
        rules["names"] = names
        # —— 候选宇宙：聊过的会话 ∪ 平台联系人（好友 + 群）——
        # 用户口径（2026-10-11）：「原名单默认是空，那么主动消息黑白名单会显示所有用户和群聊」。
        # 原名单（可见性）空 = 谁都在 → 名单页就该把所有人都摆出来让你挑，
        # 而不是只显示「已开启主动」的几个、其余藏进按钮里（旧行为，用户反馈显示不正常）。
        peers: dict[tuple[str, str], dict[str, Any]] = {}
        try:
            ct = await self.contacts()
        except Exception as exc:                            # noqa: BLE001 联系人拿不到不影响名单页
            logger.debug(f"[StarHeart] 主动名单取联系人失败：{exc}")
            ct = {}
        # 原生白名单非空 → 候选只保留名单内的（空 = 全部放行，下面照旧全显示）
        _nat = _native_vis()
        _allow_ids: set[str] = set()
        if _nat and _nat.get("enable") and _nat.get("ids"):
            _allow_ids = {str(x) for x in _nat["ids"]}
        for kind, rows, id_keys in (
            ("group", ct.get("groups") or [], ("group_id", "id")),
            ("private", ct.get("friends") or [], ("user_id", "id")),
        ):
            for row in rows:
                ident = ""
                for k in id_keys:
                    ident = str(row.get(k) or "").strip()
                    if ident:
                        break
                if not ident:
                    continue
                if _allow_ids and kind == "group" and not _native_id_match(
                    ident, str(row.get("platform") or ""), kind, _allow_ids
                ):
                    continue                                     # 群聊原生名单外：不显示（私聊不走原生名单）
                label = str(row.get("name") or row.get("nickname") or row.get("remark") or "").strip()
                if label:
                    names.setdefault(f"{kind}:{ident}", label)
                peers[(kind, ident)] = row
        judged = []
        for item in base.get("sessions", []):
            sid = str(item.get("session_id") or "")
            # ChatUI（webchat）是操作者自己的控制台 / 测试通道，不是「会被主动打扰的人」——
            # 名单里不显示、也不参与主动（用户原话：正经人谁拿这个聊天，都是用来测功能的）。
            if sid.split(":", 1)[0].strip().lower() == "webchat":
                continue
            kind, ident = self.session_kind(sid)
            if _allow_ids and kind == "group" and ident and not _native_id_match(
                ident, sid.split(":", 1)[0], kind, _allow_ids
            ):
                continue                                         # 群聊原生名单外的会话不进候选（已失效）
            verdict, why = self._judge(kind, ident, rules, cfg, sid.split(":", 1)[0])
            judged.append({"session_id": item.get("session_id"), "kind": kind, "id": ident,
                           "name": names.get(f"{kind}:{ident}", ""),
                           "verdict": verdict, "why": why,
                           "msgs": item.get("msgs", 0), "idle_min": item.get("idle_min", 0),
                           "sent_today": item.get("sent_today", 0), "user_id": item.get("user_id", "")})
        # 联系人里「还没聊过」的也要出现在名单上（勾上保存就能进白名单）
        _seen = {(x["kind"], x["id"]) for x in judged}
        for (kind, ident), _row in peers.items():
            if (kind, ident) in _seen:
                continue
            _seen.add((kind, ident))
            verdict, why = self._judge(kind, ident, rules, cfg)
            judged.append({
                "session_id": "", "kind": kind, "id": ident,
                "name": names.get(f"{kind}:{ident}", ""), "verdict": verdict, "why": why,
                "msgs": 0, "idle_min": 0, "sent_today": 0, "user_id": "", "no_history": True,
            })
        # 名单里保存过、但既不在会话里也不在联系人里的（典型：原生名单一收窄，它就掉出去了）
        # 必须**由后端给出真实判定**再展示 —— 前端旧实现对这些行硬编码 verdict=allow（"名单里显式添加"），
        # 于是「已失效」的条目在界面上仍显示成「可主动」（面板说能发、实际发不出）。
        # 展示成「被拦 · 已失效」而不是隐藏：① 一眼看懂发生了什么；② 行还在，才能取消勾选保存来清理。
        for _key, _kind in (("wl_private", "private"), ("bl_private", "private"),
                            ("wl_group", "group"), ("bl_group", "group")):
            for _ident in (rules.get(_key) or []):
                _ident = str(_ident).strip()
                if not _ident or (_kind, _ident) in _seen:
                    continue
                _seen.add((_kind, _ident))
                _verdict, _why = self._judge(_kind, _ident, rules, cfg)
                judged.append({
                    "session_id": "", "kind": _kind, "id": _ident,
                    "name": names.get(f"{_kind}:{_ident}", ""), "verdict": _verdict, "why": _why,
                    "msgs": 0, "idle_min": 0, "sent_today": 0, "user_id": "", "from_list": True,
                })
        rules["sessions"] = judged
        rules["allowed"] = sum(1 for x in judged if x["verdict"] == "allow")
        rules["blocked"] = sum(1 for x in judged if x["verdict"] == "deny")
        return rules

    def _judge(self, kind: str, ident: str, rules: dict[str, Any],
               cfg: dict[str, Any] | None = None, platform: str = "") -> tuple[str, str]:
        """判定一个会话在**主动名单**下的结论。

        用户口径（2026-10-11，两次拍板后的最终版）：
        · 主动名单自己回答「能不能主动开口」（白名单外 / 黑名单内 = 拦）；
        · **AstrBot 原生白名单是地基**（实时只读，不缓存、不镜像）：
          - 原生未启用 / 名单为空 = 全部放行（等于不触发）→ 主动名单说了算；
          - 原生名单非空 = 只在名单内收窄 → 名单外的会话在主动这里一并**自动失效**
            （哪怕它之前被保存进过主动名单：用户口径「插件会自动读到并进行失效」）。
        为什么必须实时读：名单页与心跳都走这一个函数 —— 原生名单一动，两边同时跟着变，不会各说各话。
        """
        if cfg is not None and ident:
            try:
                _ok, _why = self.visible(kind, ident, cfg, platform=platform)
            except Exception:                                    # noqa: BLE001 原生读失败不放大权限
                _ok, _why = True, ""
            if not _ok:
                return "deny", f"{_why}（主动名单已自动失效）"
        k = "group" if kind == "group" else "private"
        bl = [str(x) for x in (rules.get(f"bl_{k}") or [])]
        wl = [str(x) for x in (rules.get(f"wl_{k}") or [])]
        mode = str((rules.get("modes") or {}).get(k) or "black").lower()
        if mode not in ("black", "white", "off"):
            mode = "black"
        if mode == "off":
            return "deny", "主动名单：off（不主动）"
        if mode == "black":
            if ident and ident in bl:
                return "deny", "在主动黑名单里"
            return "allow", ""
        # 白名单模式：名单为空 = 谁都不主动（避免误扰）
        # ⚠️ 下面两句拒绝文案是 auto_wl（自动加白）的判据，改文案前先搜 proactive_scan 里的 why 条件
        if ident not in wl:
            return "deny", ("主动白名单为空（谁都不主动）" if not wl else "不在白名单里")
        return "allow", ""

    async def proactive_set(self, kind: str, mode: str, ids: list[str]) -> dict[str, Any]:
        """面板「多选 + 保存」：一次写模式 + 该模式的名单（另一份名单留着，切回来不丢）。"""
        if kind not in ("group", "private"):
            raise ValueError(f"unknown kind: {kind}")
        mode = str(mode or "black").lower()
        if mode not in ("black", "white", "off"):
            raise ValueError(f"unknown mode: {mode}")
        clean: list[str] = []
        for x in ids or []:
            x = str(x).strip()
            if x and x not in clean:
                clean.append(x)
        await self.store.set_config(f"proactive_mode_{kind}", mode)
        if mode == "off":
            await self.store.audit(f"proactive.set_{kind}_off", "不主动")
            return await self.proactive_rules()
        key = "proactive_wl_" + kind if mode == "white" else "proactive_bl_" + kind
        await self.store.set_config(key, clean)
        await self.store.audit(f"proactive.set_{kind}_{mode}", f"{len(clean)} 项")
        return await self.proactive_rules()

    async def rule_bulk(self, kind: str, action: str, ids: list[str]) -> dict[str, Any]:
        """多选确认：整表替换该名单。action: allow | deny。"""
        if action not in ("allow", "deny"):
            raise ValueError(f"unknown action: {action}")
        if kind not in ("group", "private"):
            raise ValueError(f"unknown kind: {kind}")
        wl_key, bl_key = self._list_key(kind, "allow"), self._list_key(kind, "deny")
        clean = []
        for x in ids or []:
            x = str(x).strip()
            if x and x not in clean:
                clean.append(x)
        cfg = await self.config()
        if action == "allow":
            await self.store.set_config(wl_key, clean)
            # 二选一：进了白名单的自动从黑名单移除
            await self.store.set_config(bl_key, [x for x in (cfg.get(bl_key) or []) if str(x) not in clean])
        else:
            await self.store.set_config(bl_key, clean)
            await self.store.set_config(wl_key, [x for x in (cfg.get(wl_key) or []) if str(x) not in clean])
        await self.store.audit(f"proactive.bulk_{action}", f"{kind}:{len(clean)}")
        return await self.proactive_rules()

    async def rule_set(self, kind: str, ident: str, action: str) -> dict[str, Any]:
        """action: allow（进白名单，并从黑名单移除）| deny（进黑名单，并从白名单移除）| clear（两个都清）。"""
        if not ident:
            raise ValueError("missing id")
        cfg = await self.config()
        wl_key, bl_key = self._list_key(kind, "allow"), self._list_key(kind, "deny")
        wl = [str(x) for x in (cfg.get(wl_key) or [])]
        bl = [str(x) for x in (cfg.get(bl_key) or [])]
        wl = [x for x in wl if x != ident]
        bl = [x for x in bl if x != ident]
        if action == "allow":
            wl.append(ident)
        elif action == "deny":
            bl.append(ident)
        elif action != "clear":
            raise ValueError(f"unknown action: {action}")
        await self.store.set_config(wl_key, wl)
        await self.store.set_config(bl_key, bl)
        await self.store.audit(f"proactive.{action}", f"{kind}:{ident}")
        return await self.proactive_rules()

    # ---------------- 主动消息 ----------------
    async def proactive_view(self, limit: int = 200) -> dict[str, Any]:
        cfg = await self.config()
        day_start = self._day_start_ts(cfg)
        baseline = await self.store.proactive_baseline(day_start)
        plan = await self.store.get_config("proactive_plan", {}) or {}
        date = self._day_str(cfg)
        raw = plan.get("slots") or []
        slots = ([float(x) for x in raw if isinstance(x, (int, float)) and float(x) > 0]
                 if str(plan.get("date") or "") == date else [])
        return {
            "items": await self.store.list_proactive(limit),
            "sessions": baseline["sessions"],
            "candidates": baseline["candidates"],
            "config": {k: cfg.get(k) for k in CONFIG_DOMAINS["proactive"]},
            "plan": {"date": str(plan.get("date") or ""), "today": date, "cap": plan.get("cap"),
                     "slots": slots, "day_start": day_start, "day_end": day_start + 86400.0},
        }

    def _tzinfo(self, cfg: dict[str, Any]):
        """解析时区：AstrBot 全局配置 → 插件配置 → None（退回本机）。静默时段与「一天」边界共用。"""
        tz_name = ""
        fn = self.system.get("timezone")
        if callable(fn):
            try:
                tz_name = str(fn() or "")
            except Exception:
                tz_name = ""
        tz_name = tz_name or str(cfg.get("timezone") or "")
        if tz_name:
            try:
                from zoneinfo import ZoneInfo

                return ZoneInfo(tz_name)
            except Exception:
                pass
        return None

    def timezone(self, cfg: dict[str, Any]):
        """对外别名：插件各处要「按 AstrBot 时区」算时间时统一走它（容器时区常见是 UTC）。"""
        return self._tzinfo(cfg)

    def _day_start_ts(self, cfg: dict[str, Any], now_ts: float | None = None) -> float:
        """「今天」从哪一刻算：本地（AstrBot 时区）自然日 00:00。
        用户口径（2026-10-12）：一天按 0:00–23:59 计，不是滚动 24 小时。"""
        from datetime import datetime

        ts = time.time() if now_ts is None else now_ts
        tz = self._tzinfo(cfg)
        d = datetime.fromtimestamp(ts, tz) if tz is not None else datetime.fromtimestamp(ts)
        return d.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()

    def _day_str(self, cfg: dict[str, Any], now_ts: float | None = None) -> str:
        """本地自然日的 YYYY-MM-DD（好感日上限的记账键，必须与 _day_start_ts 同一时区）。"""
        from datetime import datetime

        ts = time.time() if now_ts is None else now_ts
        tz = self._tzinfo(cfg)
        d = datetime.fromtimestamp(ts, tz) if tz is not None else datetime.fromtimestamp(ts)
        return d.strftime("%Y-%m-%d")

    def _now_hhmm(self, cfg: dict[str, Any]) -> str:
        """当前钟点：优先用 AstrBot 配置的时区（容器常见是 UTC，直接用会误判静默时段）。"""
        from datetime import datetime

        tz = self._tzinfo(cfg)
        if tz is not None:
            return datetime.now(tz).strftime("%H:%M")
        return time.strftime("%H:%M", time.localtime())

    def now_text(self, cfg: dict[str, Any]) -> str:
        """「2026-10-19 16:37（周一）」—— 主动开口必须知道绝对时间。

        真机事故：16:37 发出「这么晚了 还不睡」。主动提示词当时只有「距上次说话 232 分钟」，
        没有钟；模型拿上一段深夜对话当现在，自己编了个半夜。回复链路早就有这行（_clock_hint），
        主动链路一直没有。
        """
        from datetime import datetime

        tz = self._tzinfo(cfg)
        d = datetime.now(tz) if tz is not None else datetime.now()
        return f"{d.strftime('%Y-%m-%d %H:%M')}（周{'一二三四五六日'[d.weekday()]}）"

    def abs_stamp(self, cfg: dict[str, Any]) -> Any:
        """epoch → 「10-17 18:13」（AstrBot 时区）。

        记忆块每条都要带绝对日期：只有「2 天前」这种相对量时，模型会把它说成「昨儿」
        （真机：10-17 排的定时提醒被 10-20 主动开口说成「昨儿不是说去杭州了」）。
        """
        from datetime import datetime

        tz = self._tzinfo(cfg)

        def _fmt(ts: float) -> str:
            try:
                num = float(ts or 0)
            except (TypeError, ValueError):
                return ""
            if num <= 0:
                return ""
            try:
                d = datetime.fromtimestamp(num, tz) if tz is not None else datetime.fromtimestamp(num)
            except (OSError, OverflowError, ValueError):
                return ""
            return d.strftime("%m-%d %H:%M")

        return _fmt

    def _quiet_now(self, cfg: dict[str, Any], now_hhmm: str) -> bool:
        start = str(cfg.get("proactive_quiet_start") or "")
        end = str(cfg.get("proactive_quiet_end") or "")

        def mins(t: str) -> int | None:
            try:
                h, m = t.split(":")
                return int(h) * 60 + int(m)
            except (ValueError, AttributeError):
                return None

        s_m, e_m = mins(start), mins(end)
        cur = mins(now_hhmm)
        if s_m is None or e_m is None or cur is None:
            return False
        if s_m <= e_m:
            return s_m <= cur < e_m
        return cur >= s_m or cur < e_m  # 跨午夜

    # ---------------- 主动开口排布（当天随机时间点） ----------------
    # 机制：不再「最小间隔一到就发」——每天先把 N 个开口时间点排好（N=每日上限，
    # 落在当天 0:00–23:59 里随机分布、避开静默时段），扫描只在「到点了」才尝试开口；
    # 改上限 / 跨天自动重排；被更晚时间点追上的旧时间点按「错过」作废。
    @staticmethod
    def _hm_to_min(v: Any) -> int | None:
        m = re.match(r"^(\d{1,2}):(\d{2})$", str(v or "").strip())
        if not m:
            return None
        h, mi = int(m.group(1)), int(m.group(2))
        return h * 60 + mi if 0 <= h < 24 and 0 <= mi < 60 else None

    def _day_segments(self, cfg: dict[str, Any], day_start: float) -> list[tuple[float, float]]:
        """当天可开口时段 = 全天 − 静默时段（静默可为跨午夜区间）。"""
        day_end = day_start + 86400.0
        segs: list[tuple[float, float]] = [(day_start, day_end)]
        s_m = self._hm_to_min(cfg.get("proactive_quiet_start"))
        e_m = self._hm_to_min(cfg.get("proactive_quiet_end"))
        if s_m is not None and e_m is not None and s_m != e_m:
            qsegs = ([(day_start + s_m * 60.0, day_start + e_m * 60.0)] if s_m < e_m
                     else [(day_start, day_start + e_m * 60.0), (day_start + s_m * 60.0, day_end)])
            for q0, q1 in qsegs:
                nxt: list[tuple[float, float]] = []
                for a, b in segs:
                    if q1 <= a or q0 >= b:
                        nxt.append((a, b))
                        continue
                    if a < q0:
                        nxt.append((a, q0))
                    if q1 < b:
                        nxt.append((q1, b))
                segs = nxt
        return [(a, b) for a, b in segs if b - a > 60]

    def _gen_slots(self, cfg: dict[str, Any], n: int, lo: float, hi: float, day_start: float) -> list[float]:
        """在 [lo, hi] ∩ 可开口时段里随机排 n 个时间点：分层采样，整段分散、不扎堆。"""
        avail = [(max(a, lo), min(b, hi)) for a, b in self._day_segments(cfg, day_start)]
        avail = [(a, b) for a, b in avail if b - a > 30]
        total = sum(b - a for a, b in avail)
        if n <= 0 or total <= 60:
            return []
        n = min(n, max(1, int(total // 300)))  # 两位至少隔 5 分钟，排不下就少排
        slots: list[float] = []
        for i in range(n):
            t0, t1 = total * i / n, total * (i + 1) / n
            w = t1 - t0
            target = random.uniform(t0 + w * 0.15, t1 - w * 0.15) if w > 120 else random.uniform(t0, t1)
            acc = 0.0
            ts = avail[-1][1] - 1.0
            for a, b in avail:
                if target <= acc + (b - a):
                    ts = a + (target - acc)
                    break
                acc += b - a
            slots.append(round(ts, 1))
        slots.sort()
        return slots

    async def _plan_sent_today(self, day_start: float) -> int:
        """今天已真发出几条（不含手动试发）——排布余量的记数源。"""
        row = await (await self.store.db.execute(
            "SELECT COUNT(*) FROM hf_proactive_log WHERE status='sent' AND ts >= ? "
            "AND guard NOT LIKE '%\"manual\"%'", (day_start,)
        )).fetchone()
        return int(row[0] or 0) if row else 0

    async def _plan_ensure(self, cfg: dict[str, Any], now_ts: float, session_count: int = 1) -> dict[str, Any]:
        """取/刷新今天的开口排布。只在「新的一天 / 每日上限变化」时重排。

        注意：session_count 会随活跃会话数波动，不能每轮都拿它重算 cap —— 否则
        时间条每分钟被重新洗牌，永远等不到 slots[0]，表现就是“主动消息不发”。
        """
        day_start = self._day_start_ts(cfg, now_ts)
        date = self._day_str(cfg, now_ts)
        daily_cap = max(0, _cfg_int(cfg, "proactive_daily_cap", 3))
        cap = daily_cap * max(1, int(session_count))
        sent = await self._plan_sent_today(day_start)
        plan = await self.store.get_config("proactive_plan", {}) or {}
        raw = plan.get("slots") or []
        slots = [float(x) for x in raw if isinstance(x, (int, float)) and float(x) > 0]
        changed = False
        same_day = str(plan.get("date") or "") == date
        cap_changed = int(plan.get("daily_cap") or -1) != daily_cap
        if not same_day or cap_changed:
            # 新的一天 / 用户改了每日上限：按「当前使用之后的剩余量」重排
            need = max(0, cap - sent)
            slots = self._gen_slots(cfg, need, max(now_ts, day_start), day_start + 86400.0, day_start)
            changed = True
        else:
            # 同一天：沿用当天已经定下的 cap，活跃会话数波动不重排
            cap = int(plan.get("cap") or cap)
            need = max(0, cap - sent)
            while len(slots) >= 2 and now_ts >= slots[1]:
                slots.pop(0)  # 被更晚时间点追上的：按「错过」作废
                changed = True
            if len(slots) > need:
                slots = slots[:need]
                changed = True
        if changed:
            plan = {"date": date, "cap": cap, "daily_cap": daily_cap, "slots": slots, "gen_ts": now_ts}
            await self.store.set_config("proactive_plan", plan)
        return {"date": date, "cap": cap, "slots": slots}

    async def _plan_consume(self) -> dict[str, Any]:
        """一个开口真的发出去了：消耗最早的那个（也就是刚到的）时间点。"""
        plan = await self.store.get_config("proactive_plan", {}) or {}
        slots = [float(x) for x in (plan.get("slots") or []) if isinstance(x, (int, float))]
        if slots:
            slots.pop(0)
        plan["slots"] = slots
        await self.store.set_config("proactive_plan", plan)
        return plan

    def _ts_hhmm(self, cfg: dict[str, Any], ts: float) -> str:
        from datetime import datetime

        tz = self._tzinfo(cfg)
        d = datetime.fromtimestamp(ts, tz) if tz is not None else datetime.fromtimestamp(ts)
        return d.strftime("%H:%M")

    async def proactive_scan(self) -> dict[str, Any]:
        """真扫一遍：够格的说一句（影子模式只落候选）。"""
        cfg = await self.config()
        # 语义：总开关关掉 = 不扫；主动关掉但还在影子模式 = 仍旧扫（方便在面板看候选）。
        if not cfg.get("master_switch") or (
            cfg.get("proactive_enabled") is False and not cfg.get("proactive_shadow")
        ):
            return {"scanned": 0, "candidates": 0, "sent": 0, "skipped": 0}
        # skip 流水按天滚动：扫描每分钟跑一次，不清理会把库撑爆
        cutoff = time.time() - 7 * 86400
        await self.store.db.execute(
            "DELETE FROM hf_proactive_log WHERE status IN ('skipped','candidate') AND ts < ?", (cutoff,)
        )
        await self.store.db.commit()
        day_start = self._day_start_ts(cfg)
        baseline = await self.store.proactive_baseline(day_start)
        active_sessions = sum(
            1 for x in (baseline.get("sessions") or [])
            if float(x.get("last_user_ts") or 0) >= day_start
        )
        extras = await self._whitelist_sessions(cfg, baseline.get("sessions") or [])
        # 只按「今天活跃 + 显式白名单」放大额度，且加硬上限，避免历史会话把 cap 撑到几百
        session_count = min(50, max(1, active_sessions + len(extras)))
        favours = {str(f.get("user_id") or ""): f for f in await self.store.list_favours()}
        now_ts = time.time()
        plan = await self._plan_ensure(cfg, now_ts, session_count=session_count)
        due_ts: float | None = float(plan["slots"][0]) if plan.get("slots") else None
        hhmm = self._now_hhmm(cfg)
        quiet = self._quiet_now(cfg, hhmm)
        min_rank = ZONE_RANK.get(str(cfg.get("proactive_min_zone") or "").strip().upper(), 0)
        if not cfg.get("favour_enabled", True):
            min_rank = 0
        sent = skipped = cands = 0
        max_idle = _cfg_float(cfg, "proactive_idle_min", 120.0)
        # 只保留「真的有过用户发言」或「显式白名单」的会话：
        # hf_proactive_log 里只有 failed/candidate、从没聊过的会话不该被主动打扰。
        sessions = [
            x for x in (baseline["sessions"] or [])
            if float(x.get("last_user_ts") or 0) > 0 or x.get("from_whitelist")
        ]
        known = {str(x.get("session_id") or "") for x in sessions}
        for extra in extras:
            if extra["session_id"] not in known:
                sessions.append(extra)
                known.add(extra["session_id"])
        if cfg.get("proactive_last_only"):
            # last_only 只裁剪「历史会话」，白名单是手工点过的显式授权，不能被裁掉
            history = [x for x in sessions if not x.get("from_whitelist")]
            if history:
                history = sorted(history, key=lambda x: x.get("last_user_ts") or 0, reverse=True)[:1]
            sessions = history + [x for x in sessions if x.get("from_whitelist")]
        rules = self._proactive_rule_tables(cfg)
        backoff_map = getattr(self, "_proactive_backoff", None)
        if backoff_map is None:
            backoff_map = self._proactive_backoff = {}
        auto_mode = str(cfg.get("proactive_auto_wl") or "off")
        for item in sessions:
            _loop_sid = str(item.get("session_id") or "")
            if now_ts < float(backoff_map.get(_loop_sid) or 0):
                skipped += 1
                continue
            _last_user = float(item.get("last_user_ts") or 0)
            idle_min = ((now_ts - _last_user) / 60.0) if _last_user > 0 else -1.0
            _sid2 = str(item.get("session_id") or "")
            kind, ident = self.session_kind(_sid2)
            verdict, why = self._judge(kind, ident, rules, cfg, _sid2.split(":", 1)[0])
            # 群聊不是一个人：不要用「最后一个发言者」的好感档决定整个群的语气/门槛
            if kind == "group":
                zone = "NEUTRAL"
            else:
                fav = favours.get(str(item.get("user_id") or "")) or {}
                zone = str(fav.get("zone") or "NEUTRAL")
            # 自动白名单：互动过的会话（有过发言）自动放进白名单，省得手工加。
            # 老代码只认 auto_mode in ("both", kind)，而面板 select 给的是 off/close/familiar
            # → 选 close 或 familiar 全都等于没开（静默失效），真正有效的 both 又没有入口。
            # 现在按好感档位实现这两档，both/group/private 保留为旧配置兼容值。
            if (
                verdict == "deny"
                and why in ("不在白名单里", "主动白名单为空（谁都不主动）")
                and int(item.get("msgs") or 0) >= 1
                and self._auto_wl_allows(auto_mode, kind, zone)
            ):
                await self.rule_set(kind, ident, "allow")
                verdict, why = "allow", "自动加入白名单"
            guard: dict[str, Any] = {
                "idle_min": round(idle_min, 1), "quiet": quiet, "kind": kind, "id": ident,
                "sent_today": item.get("sent_today", 0), "cap": cfg.get("proactive_daily_cap"),
                "verdict": verdict, "why": why,
            }
            if verdict == "deny":
                skipped += 1
                if cfg.get("proactive_shadow"):
                    await self.store.log_proactive({
                        "session_id": item["session_id"], "user_id": item.get("user_id") or "",
                        "zone": zone, "idle_min": idle_min, "score": 0.0,
                        "status": "skipped", "reason": f"名单拦截：{why}", "guard": guard,
                    })
                continue
            guard["zone"] = zone
            guard["due"] = round(due_ts) if due_ts else None
            reason = ""
            if due_ts is None:
                reason = "今日预定开口已排完"
            elif now_ts < due_ts:
                reason = f"未到预定开口时间（下次 {self._ts_hhmm(cfg, due_ts)}）"
            elif idle_min >= 0 and idle_min < max_idle:
                reason = f"距上次发言 {idle_min:.0f} 分钟 < {max_idle:.0f}"
            elif quiet:
                reason = "静默时段"
            elif int(item.get("sent_today") or 0) >= _cfg_int(cfg, "proactive_daily_cap", 3):
                reason = "今日已达上限"
            elif (now_ts - float(item.get("last_proactive_ts") or 0)) / 60.0 < _cfg_float(cfg, "proactive_min_interval_min", 90.0):
                reason = "距上次主动未到最小间隔"
            elif ZONE_RANK.get(zone, 2) < min_rank:
                reason = f"关系区间 {zone} 低于 {cfg.get('proactive_min_zone')}"
            if reason:
                skipped += 1
                # 同一会话同一原因 30 分钟内只留一条：扫描每分钟一次，不节流会把
                # hf_proactive_log 写成每分钟 3 条的流水，真正有用的记录被淹掉。
                _skip_log = getattr(self, "_proactive_skip_log", None)
                if _skip_log is None:
                    self._proactive_skip_log = {}
                    _skip_log = self._proactive_skip_log
                _skip_key = (str(item["session_id"]), re.sub(r"\d+", "#", reason))
                if now_ts - float(_skip_log.get(_skip_key) or 0) >= 1800:
                    _skip_log[_skip_key] = now_ts
                    await self.store.log_proactive({
                        "session_id": item["session_id"], "user_id": item.get("user_id") or "",
                        "zone": zone, "idle_min": idle_min, "score": 0.0,
                        "status": "skipped", "reason": reason, "guard": guard,
                    })
                continue
            score = round(min(100.0, max(0.0, idle_min) / max(1.0, max_idle) * 30 + ZONE_RANK.get(zone, 2) * 12), 1)
            cands += 1
            if cfg.get("proactive_shadow") or not cfg.get("proactive_enabled"):
                await self.store.log_proactive({
                    "session_id": item["session_id"], "user_id": item.get("user_id") or "",
                    "zone": zone, "idle_min": idle_min, "score": score,
                    "status": "candidate", "reason": "影子模式：够格但未发送", "guard": guard,
                })
                continue
            # P0 修复（2026-10-15）：真正静默时长要塞回 item —— _build_proactive_prompt 读的是
            # item["idle_min"]，旧实现没塞，提示词里「距对方上次说话约 X 分钟」恒为 0
            # （实测 312/785 分钟都显示 0，模型据此把旧记忆当成「刚刚发生」）。
            item["idle_min"] = idle_min
            text = await self._proactive_text(item, zone, cfg)
            meta = getattr(self, "_last_prompt", {}) or {}
            prompt_source = str(meta.get("prompt_source") or "")
            memory_ids = [str(x) for x in (meta.get("memory_ids") or [])]
            invalid_reason = str(meta.get("invalid_reason") or "")
            if not text:
                skipped += 1
                why = invalid_reason or "模型未返回内容"
                await self.store.log_proactive({
                    "session_id": item["session_id"], "user_id": item.get("user_id") or "",
                    "zone": zone, "idle_min": idle_min, "score": score, "status": "skipped",
                    "reason": f"这次没开口：{why}", "guard": guard, "text": "",
                    "prompt_source": prompt_source, "memory_ids": memory_ids,
                })
                logger.info(f"[StarHeart] 这次没开口（{why}）→ {item['session_id']}")
                # 模型自己选择沉默 / 空正文：消耗这个时间点 + 5 分钟退避，避免每分钟重打模型
                backoff_map[str(item["session_id"])] = now_ts + 300
                plan = await self._plan_consume()
                _left = [float(x) for x in (plan.get("slots") or []) if isinstance(x, (int, float))]
                due_ts = _left[0] if _left else None
                continue
            ok = False
            send = self.system.get("send_proactive")
            if text and callable(send):
                note = f"提示词:{prompt_source or '?'}" + (f" 记忆:{','.join(memory_ids)}" if memory_ids else "")
                ok = bool(await send(item["session_id"], text, note=note))
            await self.store.log_proactive({
                "session_id": item["session_id"], "user_id": item.get("user_id") or "",
                "zone": zone, "idle_min": idle_min, "score": score,
                "status": "sent" if ok else "failed", "reason": "" if ok else "发送失败",
                "guard": guard, "text": text, "prompt_source": prompt_source, "memory_ids": memory_ids,
            })
            sent += 1 if ok else 0
            if ok:
                plan = await self._plan_consume()
                _left = [float(x) for x in (plan.get("slots") or []) if isinstance(x, (int, float))]
                due_ts = _left[0] if _left else None
            else:
                # 发送失败也要消耗时间点 + 短退避：否则同一会话每分钟重打一次模型
                backoff_map[str(item["session_id"])] = now_ts + 900
                plan = await self._plan_consume()
                _left = [float(x) for x in (plan.get("slots") or []) if isinstance(x, (int, float))]
                due_ts = _left[0] if _left else None
        return {"scanned": len(sessions), "candidates": cands, "sent": sent, "skipped": skipped, "quiet": quiet, "next_open": due_ts}

    async def _build_proactive_prompt(self, item: dict[str, Any], zone: str, cfg: dict[str, Any]) -> dict[str, Any]:
        """拼出真正发给模型的东西：人格（可选）+ 情境（第 2 层）+ 记忆。

        记忆不做类型 / 关系 / 冷却过滤：本会话 active 记忆全在池子里，按「新近度 × 强度」
        加权随机抽样。用过只降权，不封存；记忆为空也照常开口。
        """
        sid = str(item.get("session_id") or "")
        kind, ident = self.session_kind(sid)
        limit = max(0, _cfg_int(cfg, "proactive_context_memories", 3))
        memories: list[dict[str, Any]] = []
        recent_ids: set[str] = set()
        pool_size = 0
        if limit:
            persona_id = ""
            fn_p = self.system.get("active_persona")
            if callable(fn_p):
                try:
                    persona_id = str(await fn_p(sid) or "")
                except Exception:                                # noqa: BLE001 取不到人格就不过滤
                    persona_id = ""
            if not cfg.get("memory_persona_isolated", True):
                persona_id = ""
            # 最近主动用过的记忆只降权，不排除
            try:
                rows = await (await self.store.db.execute(
                    "SELECT memory_ids FROM hf_proactive_log "
                    "WHERE session_id=? AND status='sent' AND ts>=? AND memory_ids!='' "
                    "ORDER BY ts DESC LIMIT 20",
                    (sid, time.time() - 7 * 86400),
                )).fetchall()
                for r in rows:
                    recent_ids.update(self._split_memory_ids(r["memory_ids"]))
            except Exception as exc:                             # noqa: BLE001 查询失败按未用过处理
                logger.debug(f"[StarHeart] 主动记忆近期使用查询失败：{exc}")
            # 池子范围：本会话 + （私聊）同一主体在其他会话的记忆。
            # 旧实现只取 session_id=? AND status='active' LIMIT 80 —— 线上 active 239 / archived 723，
            # 71% 的记忆对主动开口永远不可见（P1-5）。archived 不再排除，只按 0.4 降权。
            subject_id = str(ident or "") if kind == "private" else ""
            base_sql = (
                "SELECT id, summary, occurred_at, created_at, atom_type, strength, status, "
                "poignancy, tags, source_mode "
                "FROM hf_memories WHERE status NOT IN ('deleted','forgotten') AND COALESCE(muted,0)=0 AND summary!='' "
                "AND (session_id=?"
            )
            base_args: list[Any] = [sid]
            if subject_id:
                base_sql += " OR subject_id=?"
                base_args.append(subject_id)
            base_sql += ")"
            order = (" ORDER BY (status='archived') ASC, "
                     "COALESCE(NULLIF(occurred_at,0), created_at) DESC LIMIT 160")

            async def _fetch(persona_filter: bool) -> list[Any]:
                where, args = base_sql, list(base_args)
                if persona_filter and persona_id:
                    where += " AND (persona_id=? OR persona_id IS NULL OR persona_id='')"
                    args.append(persona_id)
                return await (await self.store.db.execute(where + order, tuple(args))).fetchall()

            rows = await _fetch(True)
            if persona_id and not rows:
                # 人格过滤后一条不剩 → 只退回「未打人格标签」的记忆，绝不拿别的人格记忆
                rows = await (await self.store.db.execute(
                    base_sql + " AND (persona_id IS NULL OR persona_id='')" + order, tuple(base_args)
                )).fetchall()
            pool: list[dict[str, Any]] = []
            for r in rows:
                m = dict(r)
                if str(m.get("summary") or "").strip():
                    pool.append(m)
            pool_size = len(pool)
            now = time.time()

            def _is_action_mem(m: dict[str, Any]) -> bool:
                """动作留痕（跨会话排消息等）：summary 里带目标消息原文，容易被当成对方说过的话。

                保留在池里（用户要能想起「我替你记过事」），但抽样降权 + 注入时加主语标注。
                """
                tags = m.get("tags") or []
                if isinstance(tags, str):
                    try:
                        tags = json.loads(tags)
                    except (TypeError, ValueError):
                        tags = [tags]
                if not isinstance(tags, (list, tuple)):
                    tags = []
                return (any(str(t) in ("跨会话", "动作留痕") for t in tags)
                        or str(m.get("source_mode") or "") == "action")

            def _weight(m: dict[str, Any]) -> float:
                ts = float(m.get("occurred_at") or m.get("created_at") or now)
                age_days = max(0.0, (now - ts) / 86400.0)
                # 7 天半衰：主动开口是「刚想到的事」，不是回忆录。旧值 30 天让 4 天前的
                # 琐事权重还有 0.91，等于每天都在翻同一批陈年旧账（真机 12:24 那批：6 条里
                # 4 条 4 天前、2 条 2 天前，没有一条是昨天）。
                recency = 0.5 ** (age_days / 7.0)
                if age_days > 3:
                    recency *= 0.55
                if age_days > 10:
                    recency *= 0.3
                strength = 0.5 + float(m.get("strength") or 0.5)
                reuse = 0.35 if str(m.get("id")) in recent_ids else 1.0
                # 归档记忆仍可被主动想起，但权重压到 0.4：不让它们挤掉当前关系线的内容
                live = 0.4 if str(m.get("status") or "active") == "archived" else 1.0
                action = 0.5 if _is_action_mem(m) else 1.0
                # 重要度参与抽样：poi 3 → 0.79，poi 10 → 1.0。
                # 小权重就够 —— 抽签本来就该有随机性，但不能让测试残留和真实偏好同权。
                try:
                    poi = float(m.get("poignancy") if m.get("poignancy") is not None else 5)
                except (TypeError, ValueError):
                    poi = 5.0
                importance = 0.7 + 0.3 * max(0.0, min(1.0, poi / 10.0))
                return max(0.01, recency * strength * reuse * live * action * importance)

            # 加权不放回抽样：记忆越多越不容易连续撞同一组，但不封存任何一条
            for _ in range(min(limit, len(pool))):
                total = sum(_weight(m) for m in pool)
                pick = random.uniform(0.0, total)
                acc = 0.0
                for i, m in enumerate(pool):
                    acc += _weight(m)
                    if pick <= acc:
                        memories.append(pool.pop(i))
                        break
                else:
                    memories.append(pool.pop())
        persona_text = ""
        agent_system = ""
        contexts: list[Any] = []
        if cfg.get("proactive_use_persona", True):
            # 首选：AstrBot 那份拼好的完整系统提示词（人格 + 技能 + 安全 + 全局前缀）。
            fn_agent = self.system.get("agent_prompt")
            if callable(fn_agent):
                try:
                    max_age = _cfg_float(cfg, "proactive_prompt_max_age_min", 720.0) * 60.0
                    got = await _maybe_await(fn_agent(sid, max_age=max_age)) or {}
                except Exception:                                # noqa: BLE001 取不到就降级
                    got = {}
                agent_system = str(got.get("system") or "")
                contexts = list(got.get("contexts") or [])
            if not agent_system.strip():
                # 该会话还没说过话 / 缓存超时 → 退回只取人格，但不再因此拒发
                fn = self.system.get("persona_text")
                if callable(fn):
                    try:
                        persona_text = str(await fn() or "")
                    except Exception:                            # noqa: BLE001
                        persona_text = ""
        style = str(cfg.get("proactive_style") or "").strip()
        base_sys = agent_system.strip() or persona_text.strip()
        system = ((base_sys + "\n\n") if base_sys else "")
        # 第 2 层（本轮情境）：面板可自定义（proactive_instruction），留空 = 内置模板
        tpl = str(cfg.get("proactive_instruction") or "").strip() or PROACTIVE_LAYER2_TEMPLATE
        idle_raw = item.get("idle_min")
        idle_min = round(float(idle_raw)) if idle_raw not in (None, "") else -1
        idle_text = "未知（还没在这个会话说过话）" if idle_min < 0 else f"约 {idle_min} 分钟"
        now_text = self.now_text(cfg)
        layer2 = (tpl.replace("{zone}", str(zone))
                     .replace("{tone}", engine.tone_line(zone))
                     .replace("{now}", now_text)
                     .replace("{idle}", idle_text))
        # 用户自定义模板没写 {now} 也照样给钟 —— 主动开口最常见的错就是「不知道现在几点」
        if "{now}" not in tpl:
            layer2 = f"现在是 {now_text}。\n" + layer2
        # 历史对话只是背景：主动开口复用 AstrBot 上次请求的 contexts，里面带着它自己上一轮
        # 说过的话（包括幻觉）。不写死这条，模型会接着自己的旧输出往下编 —— 真机：10-19
        # 23:05 编的「昨天说陪我看星星」被 08:27、12:24 两轮主动开口原样照抄。
        system += (layer2 + "\n"
                   "如果下面有历史对话，那只是背景：不要接着你上一句话往下说，也不要复述历史里出现过的句子；"
                   "引用记忆里超过一天的事，必须说出大概时间（比如「前几天」「上周」），"
                   "不许说成「今天/刚才/昨天」。")
        block = (engine.build_memory_context(
                     memories, budget_chars=560, extra_note=PROACTIVE_MEM_NOTE,
                     stamp_fn=self.abs_stamp(cfg), today_label=now_text)
                 if memories else "")
        head = f"（风格参考：{style}；不合适就忽略）" if style else ""
        if block:
            user = (head + "\n" + block).strip()
        else:
            user = (head + "\n这个会话还没有可参考的旧记忆，就按当前情境自然说一句。").strip()
        source = "astrbot" if agent_system.strip() else ("persona" if persona_text.strip() else "none")
        return {
            "system": system, "user": user, "memories": memories,
            "memory_ids": [str(m.get("id")) for m in memories], "contexts": contexts,
            "persona_used": bool(base_sys), "prompt_source": source, "kind": kind,
            "idle_min": idle_min, "layer2": layer2, "memory_pool": pool_size,
            "memory_recent": len(recent_ids),
            "layer2_custom": bool(str(cfg.get("proactive_instruction") or "").strip()),
        }

    @staticmethod
    def _split_memory_ids(raw: Any) -> list[str]:
        """把 hf_proactive_log.memory_ids 的逗号串/列表统一成 id 列表。"""
        if isinstance(raw, (list, tuple, set)):
            return [str(x).strip() for x in raw if str(x).strip()]
        return [x.strip() for x in str(raw or "").split(",") if x.strip()]

    async def _proactive_text(self, item: dict[str, Any], zone: str, cfg: dict[str, Any]) -> str:
        gen = self.system.get("llm_generate")
        if not callable(gen):
            return ""
        built = await self._build_proactive_prompt(item, zone, cfg)
        self._last_prompt = built
        try:
            out = await gen(built["user"], "proactive", built["system"],
                            contexts=built.get("contexts"), umo=str(item.get("session_id") or ""))
        except Exception as exc:                             # noqa: BLE001
            built["invalid_reason"] = "llm_error"
            logger.warning(f"[StarHeart] 主动生成失败：{exc}")
            return ""
        text = str(out or "").strip().strip('"“”')
        # 只去掉评分/表情标签，保留正文；短句、括号动作、昵称都照发
        text = re.sub(r"\[(?:好感|表情)[^\]]*\]", "", text).strip()
        if not text:
            built["invalid_reason"] = "empty"
            return ""
        if text in ("[沉默]", "沉默", "……", "..."):
            built["invalid_reason"] = "model_silent"
            return ""
        built["invalid_reason"] = ""
        return text[:500]

    async def proactive_preview(self, session_id: str, zone: str = "") -> dict[str, Any]:
        """只生成不发送：把「实际发给模型的东西」也一并返回，面板可展开看。

        zone 留空 = 按会话真实关系档试写（旧前端写死 FAMILIAR，导致试看与真发口径不一致）。
        """
        cfg = await self.config()
        idle = -1.0
        if not str(zone or "").strip():
            zone = "NEUTRAL"
            try:
                fav = {str(f.get("user_id") or ""): f for f in await self.store.list_favours()}
                _, id2 = self.session_kind(session_id)
                zone = str((fav.get(id2) or {}).get("zone") or "NEUTRAL")
            except Exception:
                pass
        try:
            base = await self.store.proactive_baseline(self._day_start_ts(cfg))
            for item in base.get("sessions", []):
                if item.get("session_id") == session_id:
                    _last = float(item.get("last_user_ts") or 0)
                    if _last > 0:
                        idle = (time.time() - _last) / 60.0
        except Exception:
            pass
        gen = self.system.get("llm_generate")
        if not callable(gen):
            return {"ok": False, "error": "模型通道不可用"}
        started = time.time()
        text = await self._proactive_text({"session_id": session_id, "idle_min": idle}, zone, cfg)
        built = getattr(self, "_last_prompt", {}) or {}
        invalid_reason = str(built.get("invalid_reason") or "")
        return {
            "ok": bool(text), "text": text, "zone": zone, "idle_min": round(idle, 1),
            "ms": int((time.time() - started) * 1000),
            "system_prompt": str(built.get("system") or "")[:1500],
            "user_prompt": str(built.get("user") or "")[:800],
            "memories_used": len(built.get("memories") or []),
            "persona_used": built.get("persona_used"), "kind": built.get("kind"),
            "prompt_source": built.get("prompt_source", "none"),
            "memory_ids": built.get("memory_ids") or [],
            "layer2_custom": bool(built.get("layer2_custom")),
            "contexts_used": len(built.get("contexts") or []),
            "valid": bool(text), "invalid_reason": invalid_reason,
            "error": "" if text else ("模型选择沉默" if invalid_reason == "model_silent" else "模型没有返回内容"),
        }

    # ---------------- 撤回反应（用户撤回一条消息 → 当场走主动通道起一轮） ----------------
    #
    # 用户拍板（2026-10-11）：走主动消息通道、用户在聊天里**看不出区别**（就是一条普通消息）、
    # 无视主动消息名单（proactive_wl_*/bl_*/mode_*）—— 因为这是「用户刚发了一条消息」的等价物，
    # 而不是主动搭话；因此也不受免打扰/日上限限制。可见性名单（原生白名单）照常管，不破原生限制。
    RECALL_REACT_SILENT = "[沉默]"          # 模型用它表示"这轮不说话"
    RECALL_REACT_MAX_IMGS = 2

    async def _recall_neighbors(self, session_id: str, ts: float) -> tuple[bool, bool]:
        """被撤回消息的前/后一条是不是 bot 发的（群聊判「与 bot 有互动」用）。"""
        prev_bot = next_bot = False
        try:
            row = await (await self.store.db.execute(
                "SELECT is_bot FROM hf_messages WHERE session_id=? AND ts<? ORDER BY ts DESC LIMIT 1",
                (session_id, float(ts or 0)),
            )).fetchone()
            prev_bot = bool(row and int(row["is_bot"] or 0))
            row = await (await self.store.db.execute(
                "SELECT is_bot FROM hf_messages WHERE session_id=? AND ts>? ORDER BY ts ASC LIMIT 1",
                (session_id, float(ts or 0)),
            )).fetchone()
            next_bot = bool(row and int(row["is_bot"] or 0))
        except Exception:                                       # noqa: BLE001
            return False, False
        return prev_bot, next_bot

    async def recall_react(self, info: dict[str, Any]) -> dict[str, Any]:
        """撤回反应入口：跑一轮，然后把「模型的决定 + 说的话」写回那条撤回流水（面板显示）。"""
        res = await self._recall_react_impl(info)
        if not (isinstance(res, dict) and res.get("skipped") == "dup"):
            mid = str(info.get("message_id") or "")
            if mid:
                try:
                    await self.store.attach_recall_react(mid, {
                        "at": time.time(),
                        "said": bool(res.get("sent")),
                        "silent": bool(res.get("silent")),
                        "text": str(res.get("text") or "")[:300],
                        "ms": int(res.get("ms") or 0),
                        "skip": str(res.get("skipped") or ""),
                        "error": str(res.get("error") or "")[:120],
                    })
                except Exception as exc:                        # noqa: BLE001
                    logger.debug(f"[StarHeart] 撤回反应落流水跳过：{exc}")
        return res

    async def _recall_react_impl(self, info: dict[str, Any]) -> dict[str, Any]:
        """撤回反应：把「这条消息已被撤回（含正文/图片）」**当场**交给模型，由它决定说什么。

        链路：撤回事件 → 可见性/范围判定 → 组包（AstrBot 完整 agent 提示词 + 本轮原始消息=撤回通知）
              → 会话当前模型生成（原生多模态带上被撤回的图）→ 模型说就说、[沉默] 就不发 → 记流水。
        """
        try:
            cfg = await self.config()
        except Exception:                                       # noqa: BLE001
            cfg = {}
        if not cfg.get("master_switch", True):
            return {"ok": False, "skipped": "master-off"}
        if not cfg.get("recall_react_enabled", False):
            return {"ok": False, "skipped": "disabled"}
        sid = str(info.get("session_id") or "")
        mid = str(info.get("message_id") or "")
        if not sid or not mid:
            return {"ok": False, "skipped": "no-session"}
        # 同一条只反应一次（防重复触发，不是频率限制——用户要求不限频）
        seen = getattr(self, "_recall_reacted", None)
        if seen is None:
            seen = self._recall_reacted = []
        if mid in seen:
            return {"ok": False, "skipped": "dup"}
        seen.append(mid)
        del seen[:-64]
        kind, ident = self.session_kind(sid)
        # 可见性：原生的名单判定照常走（webchat 例外在 visible 内部处理）—— 不破原生限制
        try:
            vis_ok, why = self.visible(kind, ident, cfg, platform=session_platform(sid))
        except Exception:                                       # noqa: BLE001
            vis_ok, why = True, ""
        if not vis_ok:
            logger.info(f"[StarHeart] 撤回反应跳过：该目标{why}")
            return {"ok": False, "skipped": "invisible"}
        # 群聊范围：related = 只对与 bot 有互动的消息反应（前后一条是 bot 发言）
        if kind == "group":
            mode = str(cfg.get("recall_react_group") or "related").lower()
            if mode in ("off", "none", "false"):
                return {"ok": False, "skipped": "group-off"}
            if mode != "all":
                recalled0 = info.get("recalled") or {}
                ts = float(recalled0.get("ts") or 0)
                if not ts:
                    found = await self.store.find_message_by_ext_id(f"qq:{mid}")
                    ts = float((found or {}).get("ts") or 0)
                prev_bot, next_bot = await self._recall_neighbors(sid, ts)
                if not ts or not (prev_bot or next_bot):
                    logger.info("[StarHeart] 撤回反应跳过：群消息与 bot 无互动")
                    return {"ok": False, "skipped": "group-not-related"}
        # 组包：AstrBot 那份拼好的完整系统提示词（人格 + 技能 + 上下文）
        recalled = info.get("recalled") or {}
        who = str(recalled.get("sender_name") or recalled.get("sender") or "").strip()
        text = str(recalled.get("text") or "").strip()
        img_all = [str(u) for u in (recalled.get("imgs") or []) if str(u).strip()]
        imgs = img_all[: self.RECALL_REACT_MAX_IMGS] if cfg.get("recall_react_images", True) else []
        agent_system = ""
        contexts: list[Any] = []
        fn_agent = self.system.get("agent_prompt")
        if callable(fn_agent):
            try:
                max_age = _cfg_float(cfg, "proactive_prompt_max_age_min", 720.0) * 60.0
                got = await _maybe_await(fn_agent(sid, max_age=max_age)) or {}
                agent_system = str(got.get("system") or "")
                contexts = list(got.get("contexts") or [])
            except Exception:                                   # noqa: BLE001
                pass
        if not agent_system.strip():
            fn = self.system.get("persona_text")
            if callable(fn):
                try:
                    agent_system = str(await fn() or "")
                except Exception:                               # noqa: BLE001
                    agent_system = ""
        if not agent_system.strip():
            # 吃不到 AstrBot 提示词的调用不许参与对话链路（用户红线）→ 宁可不动
            logger.warning("[StarHeart] 撤回反应跳过：取不到 AstrBot 提示词（会话还没说过话？）")
            return {"ok": False, "skipped": "no-prompt"}
        what = text or (f"（图片 {len(img_all)} 张）" if img_all else "（内容未留存）")
        extra = str(cfg.get("recall_react_extra") or "").strip()
        notice = (
            "【系统通知】当前消息已被用户撤回 —— 它已经不算数了，不要再当成有效上下文。\n"
            f"撤回的人：{who or '对方'}\n"
            f"被撤回的内容：{what}\n"
            + (f"（随本轮附上被撤回的图片 {len(imgs)} 张，你可以直接看）\n" if imgs else "")
            + (extra + "\n" if extra else "")
            + "要不要回应、怎么回应（装没看见 / 追问 / 调侃 / 认错 / 什么都不说）完全由你决定；"
              "想说话就直接输出要发的那句话（口语化、别解释）；不想说话就只输出 " + self.RECALL_REACT_SILENT + "。"
        )
        gen = self.system.get("llm_generate")
        if not callable(gen):
            return {"ok": False, "error": "模型通道不可用"}
        started = time.time()
        try:
            out = await gen(notice, "recall_react", agent_system,
                            image_urls=imgs or None, contexts=contexts, umo=sid)
        except Exception as exc:                                # noqa: BLE001
            logger.warning(f"[StarHeart] 撤回反应生成失败：{exc}")
            return {"ok": False, "error": str(exc)[:120]}
        said = str(out or "").strip()
        silent = (not said) or (self.RECALL_REACT_SILENT in said)
        if silent:
            logger.info("[StarHeart] 撤回反应：模型选择沉默（不发送）")
            await self.store.audit("recall.react", f"{mid} silent")
            return {"ok": True, "sent": False, "silent": True, "ms": int((time.time() - started) * 1000)}
        said = said.replace(self.RECALL_REACT_SILENT, "").strip()[:300]
        send = self.system.get("send_proactive")
        ok = False
        if callable(send) and said:
            try:
                ok = bool(await send(sid, said, "撤回反应"))
            except Exception as exc:                            # noqa: BLE001
                logger.warning(f"[StarHeart] 撤回反应发送失败：{exc}")
        await self.store.audit("recall.react", f"{mid} sent={bool(ok)} {said[:80]}")
        return {"ok": True, "sent": bool(ok), "text": said, "ms": int((time.time() - started) * 1000)}

    async def hidden_contacts(self) -> dict[str, Any]:
        out = await self.store.hidden_contacts()
        return {"group": out.get("group", []), "private": out.get("private", [])}

    async def set_hidden_contacts(self, kind: str, ids: list[str]) -> dict[str, Any]:
        await self.store.set_hidden_contacts(kind, ids)
        await self.store.audit(f"contacts.hide_{kind}", ",".join((ids or [])[:6]))
        return await self.hidden_contacts()

    async def contacts(self, force: bool = False) -> dict[str, Any]:
        """机器人真实的群聊/好友/自身 + AstrBot 管理员名单（来自平台接口）。

        平台接口拿不到时**不摆烂**：用我们自己聊过的会话兜底（标 derived=True），
        否则名单页是一片空白，用户连「选谁」都无从选起。
        """
        empty = {"available": False, "admins": [], "groups": [], "friends": [], "bots": [],
                 "platforms": [], "errors": [], "derived": False}
        data = dict(empty)
        fn = self.system.get("contacts")
        if callable(fn):
            try:
                data.update(await fn(force) or {})
            except Exception as exc:  # noqa: BLE001
                data["errors"] = [*data.get("errors", []), f"平台接口异常：{exc}"]
        else:
            data["errors"] = ["contacts 通道不可用"]
        if not data.get("groups") and not data.get("friends"):
            try:
                fallback = await self.store.recent_contacts()
                data["groups"] = fallback.get("groups") or []
                data["friends"] = fallback.get("friends") or []
                data["derived"] = bool(data["groups"] or data["friends"])
                if data["derived"]:
                    data["errors"] = [*data.get("errors", []), "平台名单为空，已用「你聊过的会话」兜底"]
            except Exception as exc:  # noqa: BLE001
                data["errors"] = [*data.get("errors", []), f"兜底名单失败：{exc}"]
        data["available"] = bool(data.get("groups") or data.get("friends") or data.get("bots"))
        # 私聊昵称回填：平台接口常见只给号码（name 与 id 相同 ⇒ 界面上就是「ID 和 ID」），
        # 这里用我们自己同步过的称呼（upsert_person_names_bulk 落的那份）补上显示名。
        try:
            _reader = None
            for _m in ("get_person_names", "person_names", "get_person_name_map",
                       "person_name_map", "all_person_names", "get_names"):
                _f = getattr(self.store, _m, None)
                if callable(_f):
                    _reader = _f
                    break
            if _reader is not None:
                _nm = _reader()
                if hasattr(_nm, "__await__"):
                    _nm = await _nm
                _map: dict[str, str] = {}
                if isinstance(_nm, dict):
                    _map = {str(k): str(v) for k, v in _nm.items() if v}
                elif isinstance(_nm, (list, tuple)):
                    for _row in _nm:
                        if isinstance(_row, dict):
                            _k = str(_row.get("ident") or _row.get("id") or "")
                            _v = _row.get("name") or _row.get("nickname")
                            if _k and _v:
                                _map[_k] = str(_v)
                _fixed = 0
                for _fr in (data.get("friends") or []):
                    if not isinstance(_fr, dict):
                        continue
                    _id = str(_fr.get("user_id") or _fr.get("id") or "")
                    _cur = str(_fr.get("name") or _fr.get("nickname") or "").strip()
                    _hit = _map.get(_id)
                    if _hit and (not _cur or _cur == _id):
                        _fr["nickname"] = _hit
                        _fr["name"] = _hit
                        _fixed += 1
                if _fixed:
                    logger.info(f"[StarHeart] 私聊昵称回填 {_fixed} 条（平台只给了号码）")
        except Exception as exc:                                # noqa: BLE001
            logger.debug(f"[StarHeart] 私聊昵称回填失败（不影响名单）：{exc}")
        # 兜底（更硬的来源）：平台接口只给号码时，直连协议端 get_stranger_info 实时取昵称。
        try:
            _miss: list[str] = []
            for _fr in (data.get("friends") or []):
                if not isinstance(_fr, dict):
                    continue
                _id = str(_fr.get("user_id") or _fr.get("id") or "").strip()
                _cur = str(_fr.get("name") or _fr.get("nickname") or "").strip()
                if _id and (not _cur or _cur == _id):
                    _miss.append(_id)
            _rfn = self.system.get("resolve_user_names")
            if _miss and callable(_rfn):
                _live = await _rfn(_miss[:60]) or {}
                _n2 = 0
                for _fr in (data.get("friends") or []):
                    if not isinstance(_fr, dict):
                        continue
                    _id = str(_fr.get("user_id") or _fr.get("id") or "").strip()
                    _hit = _live.get(_id) if isinstance(_live, dict) else None
                    if _hit:
                        _fr["nickname"] = str(_hit)
                        _fr["name"] = str(_hit)
                        _n2 += 1
                if _n2:
                    logger.info(f"[StarHeart] 名单页私聊昵称实时补全 {_n2} 条")
        except Exception as exc:                                # noqa: BLE001
            logger.debug(f"[StarHeart] 实时补昵称失败（不影响名单）：{exc}")
        return data

    async def _resolve_session(self, session_id: str, kind: str, ident: str) -> str:
        """面板可能只给「一个好友的 QQ」或「一个群号」，这里补成完整会话串。"""
        sid = str(session_id or "").strip()
        if sid:
            return sid
        kind = "group" if str(kind or "").lower() == "group" else "private"
        ident = str(ident or "").strip()
        if not ident:
            return ""
        platform = ""
        try:
            fn = self.system.get("contacts")
            if callable(fn):
                data = await fn(False)
                for x in (data.get("bots") or []):
                    platform = str(x.get("platform") or "")
                    if platform:
                        break
        except Exception:
            platform = ""
        platform = platform or "aiocqhttp"
        mtype = "GroupMessage" if kind == "group" else "FriendMessage"
        return f"{platform}:{mtype}:{ident}"

    async def proactive_test_send(self, session_id: str = "", text: str = "",
                                  kind: str = "", ident: str = "") -> dict[str, Any]:
        """面板「立即发一条」：以系统身份让 bot 直接给指定会话/人发一条。

        设计要点（用户明确要求）：
        - **一步到位**：不给文本就直接生成，生成完立刻发，不需要「先生成再点发送」；
        - **绝不卡死**：生成 30s、发送 20s 硬超时，超时就返回明确错误，前端不会一直转圈；
        - 无视空闲门槛/静默时段/额度（那是自动扫描的护栏），这是人工试发。
        """
        send = self.system.get("send_proactive")
        if not callable(send):
            return {"ok": False, "error": "发送通道不可用（插件未就绪或平台适配器不支持主动发送）"}
        sid = await self._resolve_session(session_id, kind, ident)
        if not sid:
            return {"ok": False, "error": "没给目标：会话号 / 群号 / QQ 至少要有一个"}
        cfg = await self.config()
        # 目标被基础名单挡着时给个告警（人工试发仍然执行，但要让用户知道自动扫描不会发它）
        warn = ""
        k2, id2 = self.session_kind(sid)
        vis, why = self.visible(k2, id2, cfg, platform=session_platform(sid))
        if not vis:
            warn = f"注意：这个目标{why}，自动主动扫描不会发它（本次手动试发仍然会发）"
        # 试发按「会话真实关系档 + 真实静默时长」拼提示词（旧实现写死 NEUTRAL/0 分钟，
        # 试看结果和真发不是一回事 —— 2026-10-15 修）
        zone = "NEUTRAL"
        idle_min = 0.0
        try:
            fav = {str(f.get("user_id") or ""): f for f in await self.store.list_favours()}
            zone = str((fav.get(id2) or {}).get("zone") or "NEUTRAL")
            base = await self.store.proactive_baseline(self._day_start_ts(cfg))
            for it in base.get("sessions", []):
                if str(it.get("session_id") or "") == sid:
                    idle_min = (time.time() - float(it.get("last_user_ts") or 0)) / 60.0
        except Exception:                                     # noqa: BLE001 试发不因读不到关系档而失败
            pass
        started = time.time()
        text = str(text or "").strip()
        generated = False
        if not text:
            try:
                text = await asyncio.wait_for(
                    self._proactive_text({"session_id": sid, "idle_min": idle_min}, zone, cfg), timeout=30
                )
            except TimeoutError:
                return {"ok": False, "error": "生成超时（30s）：模型通道没响应，检查「模型分配 → 对话回复」", "session_id": sid, "warn": warn}
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "error": f"生成失败：{exc}", "session_id": sid, "warn": warn}
            generated = True
            text = str(text or "").strip()
        if not text:
            return {"ok": False, "error": "模型没有返回内容（检查「模型分配 → 对话回复」）", "session_id": sid, "warn": warn}
        meta = getattr(self, "_last_prompt", {}) or {}
        prompt_source = str(meta.get("prompt_source") or "")
        memory_ids = [str(x) for x in (meta.get("memory_ids") or [])]
        gen_ms = int((time.time() - started) * 1000)
        send_ms = 0
        try:
            t2 = time.time()
            note = f"提示词:{prompt_source or '?'}" + (f" 记忆:{','.join(memory_ids)}" if memory_ids else "")
            ok = bool(await asyncio.wait_for(send(sid, text, "手动试发", note=note), timeout=20))
            send_ms = int((time.time() - t2) * 1000)
        except TimeoutError:
            ok = False
            await self.store.log_proactive({
                "session_id": sid, "status": "failed", "reason": "面板手动试发超时",
                "text": text, "guard": {"manual": True, "timeout": 20},
                "zone": zone, "idle_min": round(idle_min, 1),
                "prompt_source": prompt_source, "memory_ids": memory_ids,
            })
            return {"ok": False, "error": "发送超时（20s）：平台适配器没回执，检查 bot 是否在线",
                    "text": text, "session_id": sid, "warn": warn}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"发送异常：{exc}", "text": text, "session_id": sid, "warn": warn}
        await self.store.log_proactive({
            "session_id": sid, "status": "sent" if ok else "failed",
            "reason": "面板手动试发", "text": text,
            "guard": {"manual": True, "generated": generated},
            "zone": zone, "idle_min": round(idle_min, 1),
            "prompt_source": prompt_source, "memory_ids": memory_ids,
        })
        return {
            "ok": ok, "text": text, "session_id": sid, "warn": warn,
            "generated": generated, "gen_ms": gen_ms if generated else 0, "send_ms": send_ms,
            "ms": int((time.time() - started) * 1000),
            "zone": zone, "idle_min": round(idle_min, 1),
            "prompt_source": prompt_source, "memory_ids": memory_ids,
            "error": "" if ok else "发送接口返回 False（bot 掉线或该会话不可达）",
        }

    # ---------------- 人格进化提案 ----------------
    async def persona_proposals(self, limit: int = 20) -> dict[str, Any]:
        return {"items": await self.store.list_proposals(limit), "config": await self.config()}

    async def evolve_state(self) -> dict[str, Any]:
        """人格进化的参数与时间标记（面板「人格状态与进化」用）。"""
        cfg = await self.config()
        last = float(cfg.get("persona_last_evolve_at") or 0)
        save_at = float(cfg.get("persona_last_save_at") or 0)
        hours = max(1, int(cfg.get("evolve_min_hours", 24) or 24))
        need = max(1, int(cfg.get("evolve_min_memories", 6) or 6))
        # 当前人格：新记忆只数它的（未归属的算人人有份）
        cur_pid = str(cfg.get("persona_active_id") or "")
        try:
            people = await self.personas()
            cur_pid = str(people.get("active") or cur_pid)
        except Exception:                                     # noqa: BLE001 读不到就退回配置值
            pass
        props = await self.store.list_proposals(20)
        pending = [x for x in props if x.get("status") == "pending"]
        approved = [x for x in props if x.get("status") == "approved"]
        # 冷却基准 = max(上次通过, 上次生成提案)：否则「驳回 → 下一小时又生成」来回抖动
        last_proposal_at = float(props[0]["ts"]) if props else 0
        cool_base = max(last, last_proposal_at)
        new_mem = await self.store.memories_since(cool_base, cur_pid) if cool_base else await self.store.memories_since(0, cur_pid)
        return {
            "ok": True,
            "params": {
                "evolve_auto": bool(cfg.get("evolve_auto", True)),
                "evolve_min_hours": hours,
                "evolve_min_memories": need,
                "evolve_scope": str(cfg.get("evolve_scope") or "desc_style"),
                "evolve_require_confirm": bool(cfg.get("evolve_require_confirm", True)),
            },
            "last_evolve_at": last, "last_save_at": save_at,
            "cooldown_base": cool_base, "persona_id": cur_pid,
            "next_due_at": (cool_base + hours * 3600) if cool_base else 0,
            "new_memories": int(new_mem), "pending": len(pending), "approved": len(approved),
            "pending_ttl_hours": max(1, int(cfg.get("evolve_pending_ttl_hours", 72) or 72)),
            "last_proposal_at": last_proposal_at,
            "llm_ready": callable(self.system.get("llm_generate")),
        }

    def _evolve_scope_hint(self, scope: str) -> str:
        return {
            "desc_only": "只允许改「人格描述」",
            "all": "允许整段重写人格",
        }.get(scope, "只允许改「人格描述与说话风格」")

    async def evolve_tick(self) -> dict[str, Any]:
        """按策略自动出进化提案（默认不自动写入，等你点通过）。"""
        cfg = await self.config()
        if not cfg.get("evolve_auto", True):
            return {"skipped": "evolve_auto 关闭"}
        if not callable(self.system.get("llm_generate")):
            return {"skipped": "模型通道不可用"}
        st = await self.evolve_state()
        # ① 先做无条件清理：待审提案超 TTL 自动作废。
        #    「永不过期」会让忘点一次 = 再也不进化；而且清理必须在冷却判定之前，
        #    否则冷却先把本轮挡掉，过期提案会一直挂在面板上显示「待审核」。
        ttl = float(st.get("pending_ttl_hours") or 72) * 3600
        expired = 0
        for x in await self.store.list_proposals(50):
            if x.get("status") == "pending" and (time.time() - float(x.get("ts") or 0)) > ttl:
                await self.store.decide_proposal(int(x["id"]), "expired")
                expired += 1
        if expired:
            await self.store.audit("persona.proposal_expired", f"{expired} 条超时作废")
            st = await self.evolve_state()
        # ② 再判门控：冷却 / 新记忆 / 待审
        base_ts = float(st.get("cooldown_base") or 0)
        hours = int(st["params"]["evolve_min_hours"])
        if base_ts and (time.time() - base_ts) < hours * 3600:
            return {"skipped": f"距上次进化 / 上次提案不足 {hours} 小时"}
        if st["new_memories"] < int(st["params"]["evolve_min_memories"]):
            return {"skipped": f"新记忆不足（{st['new_memories']}/{st['params']['evolve_min_memories']}，只数当前人格）"}
        if st["pending"]:
            return {"skipped": f"已有 {st['pending']} 条待审核提案（超过 {int(ttl/3600)} 小时会自动作废）"}
        res = await self.persona_propose()
        if not res.get("ok"):
            return res
        if not st["params"]["evolve_require_confirm"]:
            pid = int(res.get("id") or 0)
            if pid:
                decided = await self.persona_decide(pid, True)
                return {"auto_applied": True, "proposal": res, "decide": decided}
        return {"proposed": True, "proposal": res}

    async def persona_propose(self) -> dict[str, Any]:
        gen = self.system.get("llm_generate")
        if not callable(gen):
            return {"ok": False, "error": "模型通道不可用"}
        people = await self.personas()
        cfg_active = str((await self.config()).get("persona_active_id") or "")
        items = people.get("items") or []
        # 严格取当前人格（AstrBot 原生那个），绝不能回退到 items[0]——
        # 那会进化成另一个人格（实测踩过：提案写成的是另一个人格，实际在用的却是当前人格）
        want = str(people.get("active") or cfg_active or "")
        cur = next((p for p in items if p.get("persona_id") == want), None) if want else None
        if cur is None and items:
            # 没有 active 信息时：取配置里指定的人格，仍无则明确报错让用户去面板选
            cfg_pid = str((await self.config()).get("persona_active_id") or "")
            cur = next((p for p in items if p.get("persona_id") == cfg_pid), None)
        if cur is None:
            return {"ok": False, "error": "读不到当前人格（面板「人格管理」里先确认哪个是当前使用）"}
        base = strip_expression_block(str(cur.get("system_prompt") or ""))
        if not base:
            return {"ok": False, "error": "该人格没有可进化的文本"}
        # 必须把**完整人格**喂给模型：以前只喂前 2000 字、却按整段写回 → 尾部设定被静默丢掉。
        # 超长人格不硬塞（上下文会爆），明确拒绝并说清怎么办。
        if len(base) > 12000:
            return {"ok": False, "error": f"人格文本 {len(base)} 字，超过单次进化上限 12000 字 —— "
                                          f"请先精简人格，或分段手动进化（避免静默截断丢设定）"}
        # 数据源：学到的说话习惯 + bot 自己最近说过的话（自省哪里不像自己）
        habits = await self.store.list_habits()
        my_scope = f"学习·{cur.get('persona_id') or ''}"
        learned = [h for h in habits if str(h.get("scope") or "") in (my_scope, "学习", "全局")]
        style = "；".join(f"{h.get('scene')}→{h.get('way')}" for h in learned[:10])
        bot_said = await self.store.list_corpus(limit=20, is_bot=1, persona_id=str(cur.get("persona_id") or ""))
        recent = "\n".join(f"- {str(r.get('text') or '')[:80]}" for r in bot_said[:15])
        prompt = (
            f"下面是「{cur.get('persona_id')}」这个角色现在的人格设定。请结合数据给它出一版更自然的改写。\n\n"
            + (f"它最近从群友/好友那里学到的说话习惯：{style}。\n" if style else "")
            + (f"它自己最近说过这些话（这些是它说过的话，**不要照抄进人格文本**，只用来判断哪里不像它）：\n{recent}\n" if recent else "")
            + "\n改写要求：\n"
            "1. 保留核心设定（身份、性格底色、与他人关系）不变，只让说话更像真人。\n"
            "2. 把学到的口头禅、句尾语气词、常用口语融进去——但只写**可复用的说法**，"
            "不要把它说过的具体句子原文搬进来（否则它会复读自己说过的话）。\n"
            "3. 砍掉它自己说过的话里那些不像它的地方（比如太工整、像客服、说教、列建议）。\n"
            "4. 说话言简意赅，除非对方特别要求。真人话少，别写长篇。\n"
            "5. 不要写成方法论或步骤清单，要写成这个人物自己的性格与说话习惯。\n"
            "6. **不要出现「你平时大概会这样说话」这类示例对话段落**——示例属于 few-shot，"
            "会诱发复读；这里只写性格与说话习惯本身。\n"
            "7. 只输出改写后的完整人格文本，不要解释、不要 markdown。\n"
            + f"8. 可改范围：{self._evolve_scope_hint(str((await self.config()).get('evolve_scope') or 'desc_style'))}"
            "——超出这个范围的内容一律原样保留。\n\n"
            "【原文】\n" + base
        )
        out = await gen(prompt, "persona_evolve")
        proposal = str(out or "").strip()
        if not proposal:
            return {"ok": False, "error": "模型没有返回内容"}
        pid = await self.store.add_proposal({
            "persona_id": str(cur.get("persona_id") or ""), "base": base, "proposal": proposal,
            "rationale": "由面板触发的一次进化提案（需人工审核后才会生效）",
        })
        await self.store.audit("persona.propose", str(pid))
        return {"ok": True, "id": pid}

    async def _persona_text(self, persona_id: str) -> str | None:
        """读某个人格当前的 system_prompt（原生数据）；读不到返回 None（不误判成"过期"）。"""
        try:
            people = await self.personas()
        except Exception:                                     # noqa: BLE001
            return None
        for p in people.get("items") or []:
            if str(p.get("persona_id") or "") == str(persona_id or ""):
                return str(p.get("system_prompt") or "")
        return None

    async def persona_rollback(self, pid: int) -> dict[str, Any]:
        """把某条提案里保存的「旧版本」写回该人格 —— 进化错了要有退路，不用去翻备份。"""
        item = await self.store.get_proposal(int(pid))
        if item is None:
            return {"ok": False, "error": "提案不存在"}
        if str(item.get("status") or "") != "approved":
            return {"ok": False, "error": f"只有「已采纳」的提案能回滚（当前状态：{item.get('status')}）"}
        base_text = str(item.get("base") or "").strip()
        if not base_text:
            return {"ok": False, "error": "这条提案没有保存旧版本，无法回滚"}
        save = self.persona.get("update")
        if not callable(save):
            return {"ok": False, "error": "人格写入通道不可用"}
        await save(str(item.get("persona_id") or ""), {"system_prompt": base_text})
        # 回滚也是一次"写入人格"，刷新时间标记（否则面板「人格上次保存」与实际不符）
        await self.store.set_config("persona_last_save_at", time.time())
        await self.store.audit("persona.rollback", f"id={pid} persona={item.get('persona_id')}")
        return {"ok": True, "id": int(pid), "persona_id": str(item.get("persona_id") or ""), "len": len(base_text)}

    async def persona_decide(self, pid: int, approve: bool) -> dict[str, Any]:
        """通过 / 驳回一条进化提案。

        通过前**必须**校验 base 新鲜度：人格在你审阅这段时间里可能被手改过，
        拿旧提案覆盖新人格 = 静默丢掉你的修改。写回失败也不许把状态留在 approved。
        """
        item = await self.store.get_proposal(int(pid))
        if item is None:
            return {"ok": False, "error": "提案不存在"}
        if approve:
            cur_text = await self._persona_text(str(item.get("persona_id") or ""))
            base_text = str(item.get("base") or "")
            if cur_text is not None and base_text.strip() and cur_text.strip() != base_text.strip():
                await self.store.decide_proposal(int(pid), "stale")
                return {"ok": False, "stale": True,
                        "error": "这条提案基于旧版人格（你之后改过人格），已标记为过期 —— 请重新生成提案再通过"}
            item = await self.store.decide_proposal(int(pid), "approved") or item
            # 记下"上次进化 / 上次写入人格"的时间：面板要显示进化时间标记
            await self.store.set_config("persona_last_evolve_at", time.time())
            await self.store.set_config("persona_last_save_at", time.time())
        else:
            item = await self.store.decide_proposal(int(pid), "rejected") or item
        if approve:
            save = self.persona.get("update")
            if not callable(save):
                await self.store.decide_proposal(int(pid), "pending")
                return {"ok": False, "error": "人格写入通道不可用，提案已退回待审"}
            try:
                await save(item.get("persona_id") or "",
                           {"system_prompt": strip_expression_block(item.get("proposal") or "")})
            except Exception as exc:                          # noqa: BLE001
                await self.store.decide_proposal(int(pid), "pending")
                return {"ok": False, "error": f"写回人格失败：{exc}（提案已退回待审）"}
        await self.store.audit(f"persona.{'approve' if approve else 'reject'}", str(pid))
        return {"ok": True, "id": pid, "status": item.get("status")}

    # ---------------- 插件自管模型任务的提示词 ----------------
    #
    # 为什么要有这几条：这些任务是**插件自己调模型**干的活（不参与对话回复），提示词完全由插件
    # 决定。以前只有「人物画像」带 system 提示词，其余 4 个把约束塞在 user 文本里 —— 用户既看不见
    # 也改不了。现在统一登记进 TASK_PROMPTS：面板「模型分配」可展开查看与修改（留空 = 用内置默认）。
    # 注意：工具类（function calling）提示词不在清单里，按用户要求不展开。
    MEMORY_SUMMARY_SYSTEM = (
        "你是记忆整理器。读一段对话，写下以后还用得上的事，像给未来的自己留便签。\n"
        "写什么：这个人的稳定偏好与雷区、正在发生的事与计划、你们之间定下的称呼与约定、"
        "一起经历过的事、对方教你的说法（原话加引号）、情绪触发点、关系的变化。\n"
        "两个人都写：对方做了什么、你怎么回、你答应了什么——关系是双向的，只写一半以后会记错。\n"
        "亲密/性相关互动照实记录双方意愿、边界、称呼与偏好；不做道德评判、不删减。\n"
        "主语可以不写；要写就用「TA」指对方、「我」指你自己。\n"
        "不写：逐字流水账、没有信息量的客套、你猜的（材料不够就少写，写「无」也行）。\n"
        "每条一行：摘要|类型|强度|情绪浓度；"
        "类型 ∈ planned/factual/preference/routine/episodic/relational/opinion；强度与情绪浓度 0-1。"
    )
    EXPRESSION_SYSTEM = (
        "你是语言风格分析器。从真实聊天记录里提取「可复用的说话方式」：口头禅、句尾语气词、"
        "常用句式、称呼习惯、回应风格。\n"
        "硬性要求：1) 只提取怎么说话，不要提取隐私内容、具体事件与人名；"
        "2) 场景必须是社交情境（例如「被夸」「被催」），不要写「群聊/私聊」这类场所词；"
        "3) 只输出调用方要求的格式行，不要解释。"
    )
    PERSONA_EVOLVE_SYSTEM = (
        "你是角色人格编辑。给定一个角色的现有人格文本与它的近期素材，输出一版更自然、更像真人的改写。\n"
        "硬性要求：1) 身份、性格底色、说话习惯、与用户的关系保持不变，只改表达；"
        "2) 绝对不得引入其它角色/其它人格的内容（人名、关系、设定都不许跨人格搬）；"
        "3) 不得添加或删改安全边界；4) 只输出新人格全文本身，不要解释、不要前言后语。"
    )
    FORGET_SYSTEM = (
        "你是记忆守门人。给定一批候选记忆，只挑出「确实该遗忘」的编号。\n"
        "硬性要求：1) 只能从候选里收窄，不得新增、不得扩大范围；2) 拿不准就少挑；"
        "3) 只输出编号列表，不要解释。"
    )

    # ---------------- 聚合页 ----------------
    # ---------------- 人物画像（补齐功能）----------------
    # 画像该回答的是"这个人是谁、和这个人怎么相处"，而不是几条计数。
    # 没数据就明说没有（面板会给出"生成画像"按钮），不许显示空壳当正常。
    PROFILE_SYSTEM = (
        "你在为一个真实的人写「人物画像」。只输出一个 JSON 对象，不要解释、不要 markdown 代码块、不要多余文字。\n"
        "硬规则：如果这个人就是 bot 的主人本人，identity 里禁止写成「bot 主人的熟人/朋友/暧昧对象」，"
        "要写「bot 的主人本人」；relation 用第二人称「你」来写你和他的关系。\n"
        "硬规则：这是关于「这个人」的画像，不是关于助手的；不要把助手的性格、行为、口头禅、承诺写进任何字段。\n"
        "字段（全部按给定材料写，材料不足就留空，严禁臆造）：\n"
        '{"one_line":"一句话这个人是谁（30字内，具体）",'
        '"identity":"身份/称呼/关系",'
        '"traits":["性格特点",...],'
        '"likes":["喜欢·在意的事",...],'
        '"dislikes":["忌讳·雷区",...],'
        '"topics":["常聊话题",...],'
        '"relation":"如果这个人是 bot 主人本人：写『你和他是什么关系、该怎么相处』；如果是别人：写『他和 bot 主人的关系，以及 bot 该怎么和他相处』。2-3 句，具体可执行",'
        '"events":[{"when":"时间","what":"关键事件"}],'
        '"notes":"其他要注意的：称呼习惯、语气偏好等"}'
    )
    # 群聊画像（L1）：群不是人，问的是"这是个什么群、氛围如何、谁在活跃、常聊什么、有什么默契"。
    # 与人物画像同一套口径：材料不足留空、严禁臆造、字段级清洗后再入库。
    GROUP_PROFILE_SYSTEM = (
        "你在为一个 QQ 群写「群聊画像」。只输出一个 JSON 对象，不要解释、不要 markdown 代码块、不要多余文字。\n"
        "字段（全部按给定材料写，材料不足就留空，严禁臆造）：\n"
        '{"one_line":"一句话这是个什么群（30字内，具体）",'
        '"kind":"群的性质（熟人闲聊群/游戏群/工作群/项目群…）",'
        '"atmosphere":"氛围与说话风格（2-3 句）",'
        '"topics":["常聊话题",...],'
        '"members":[{"who":"成员称呼","role":"在群里是什么角色·常做什么","n":"发言量或记忆数"}],'
        '"norms":["群里的默契与忌讳",...],'
        '"relation":"这个群和用户（bot 的主人）的关系、bot 在群里该注意什么：2-3 句，要具体可执行",'
        '"notes":"其他要注意的"}'
    )

    async def person_merge(self, alias_id: str, canonical_id: str, reason: str = "manual") -> dict[str, Any]:
        """把别名号并入主号（同一个人的两个 QQ 号场景）。"""
        return await self.store.merge_persons(alias_id, canonical_id, reason=reason)

    async def person_unmerge(self, alias_id: str) -> dict[str, Any]:
        """拆开别名（并错了要能退回来）。"""
        return await self.store.unmerge_person(alias_id)

    async def purge_platform_sessions(self, platforms: list[str]) -> dict[str, Any]:
        """清掉非 QQ 平台（webchat 等）在库里留下的痕迹。"""
        return await self.store.purge_platform_sessions(platforms)

    async def person_profile_reset(self, pid: str, subject_kind: str = "user", persona_id: str = "") -> dict[str, Any]:
        """清掉某个人的画像（垃圾画像清理）。"""
        ok = await self.store.profile_delete(pid, subject_kind, persona_id)
        await self.store.audit("person.profile_reset", str(pid))
        return {"ok": ok, "person_id": str(pid)}

    # 任务 → 面板展示与默认提示词（system 为空 = 该任务没有系统提示词）
    TASK_PROMPTS: dict[str, dict[str, str]] = {
        "memory_summary": {"label": "记忆总结", "system": MEMORY_SUMMARY_SYSTEM,
                           "desc": "把一段对话压成摘要 + 召回线索，供以后回忆",
                           "user_key": "memory_summary_prompt", "user_label": "总结用户模板"},
        "expression_extract": {"label": "学习", "system": EXPRESSION_SYSTEM,
                               "desc": "从记忆的原对话里学「怎么说话」（回复方式 / 口头禅 / 黑话），供拟人输出"},
        "person_profile": {"label": "人物画像", "system": PROFILE_SYSTEM,
                           "desc": "把某个人的记忆汇总成一句话画像（会自动刷新）"},
        "group_profile": {"label": "群聊画像", "system": GROUP_PROFILE_SYSTEM,
                          "desc": "把某个群的记忆与成员汇总成群聊画像（会自动刷新）"},
        "persona_evolve": {"label": "人格进化", "system": PERSONA_EVOLVE_SYSTEM,
                           "desc": "按近期素材改写当前人格的表达（默认要你确认才写回）"},
        "forget": {"label": "记忆遗忘评估", "system": FORGET_SYSTEM,
                   "desc": "筛出可以归档 / 遗忘的旧记忆（只收窄，不新增）"},
    }

    async def person_profile(self, pid: str, subject_kind: str = "user", persona_id: str = "") -> dict[str, Any]:
        """读一份画像（L0 人物 / L1 群聊）+ 它的数据规模（没有时明确说"还没生成"，不造假数据）。"""
        pid = str(pid or "").strip()
        k = "group" if str(subject_kind or "").lower() == "group" else "user"
        if not pid:
            return {"ok": False, "error": "缺少主体 id"}
        row = await self.store.profile_get(pid, k, persona_id)
        dossier = await self.store.subject_dossier(pid, k, limit=200, persona_id=persona_id)
        out: dict[str, Any] = {
            "ok": True, "person_id": pid, "subject_kind": k,
            "name": dossier.get("name") or pid,
            "members": dossier.get("members") or [],
            "profile_persona": str((row or {}).get("persona_id") or ""),
            "persona_ids": await self.store.profile_persona_ids(pid, k),
            "mem_count": dossier.get("mem_count") or 0, "msg_count": dossier.get("msgs") or 0,
            "sessions": len(dossier.get("sessions") or []),
            "has_profile": bool(row),
            "profile": (row or {}).get("profile") or {},
            "updated_at": (row or {}).get("updated_at") or 0,
            "memories": [
                {"id": m["id"], "summary": m["summary"][:80], "atom_type": m["atom_type"],
                 "strength": m["strength"], "occurred_at": m["occurred_at"]}
                for m in (dossier.get("memories") or [])[:12]
            ],
            "top_entities": (dossier.get("top_entities") or [])[:12],
            "top_cues": (dossier.get("top_cues") or [])[:12],
            "llm_ready": callable(self.system.get("llm_generate")),
        }
        if out["has_profile"]:
            # 脏画像识别：真机上出现过 mem_count=0 却入库的「身份未知，仅知为人物 id …」
            prof_text = json.dumps(out["profile"], ensure_ascii=False)
            bad = (int((row or {}).get("mem_count") or 0) <= 0
                   or "仅知为人物 id" in prof_text
                   or ("身份未知" in prof_text and not (out["profile"].get("traits") or out["profile"].get("topics"))))
            if bad:
                out["invalid"] = True
                out["invalid_reason"] = ("这份画像是在材料不足时生成的（当时记忆 "
                                        f"{int((row or {}).get('mem_count') or 0)} 条），"
                                        "内容没有依据，已在自动任务里标记为待重建 —— 也可以现在点下面的按钮重生成")
                out["has_profile"] = False
            else:
                out["invalid"] = False
        if not out["has_profile"]:
            # 缺陷要显式暴露：告诉面板"没有画像 + 为什么 + 怎么办"
            if out["mem_count"] == 0 and out["msg_count"] == 0:
                out["reason"] = "这个人还没有任何记忆和互动记录 —— 先让 bot 和这个人正常聊几次，或用「导入」把历史记录补进来"
            else:
                out["reason"] = "还没有生成过画像（有 %d 条记忆可用）" % out["mem_count"]
        return out

    # ---------------- 图像识别：整段移除（不再是独立模型调用） ----------------
    #
    # 原先两条路：① 与对话同模型时，把 image_urls 交给总结模型自己看；② 配了不同模型时，先用那个
    # 「图像识别」模型识别、再把描述并进文本。现在只剩 ①，而且不需要任何配置：
    #   · 对话本身用到的图片由 AstrBot 自己的管线处理（prepare_request_images 在 on_llm_request 之后执行），
    #     插件不介入 —— 既不会改变回复，也不会丢人格/提示词；
    #   · 记忆里要留图片内容时，image_urls 直接交给做总结的模型（原生多模态）。
    # 删掉 ② 的原因：那个独立模型吃不到 AstrBot 的提示词，属于「吃不到提示词的模型参与对话链路」，
    # 用户明确禁止（自审模型就是这么坏的）。同时 `model_vision` 槽位一并下线。

    async def _persona_id_list(self) -> list[str]:
        """当前有哪些人格。后台要**逐人格**生成画像（人格之间互不可见），所以必须拿到真实 id 列表。"""
        ids: list[str] = []
        try:
            data = await self.personas()
        except Exception:
            return []
        for it in data.get("items") or []:
            pid = str((it or {}).get("id") or (it or {}).get("persona_id") or "").strip()
            if pid:
                ids.append(pid)
        return list(dict.fromkeys(ids))

    async def auto_profile_tick(self) -> dict[str, Any]:
        """后台自动补齐/刷新画像：**逐个人格**分别生成 L0 人物画像与 L1 群聊画像。

        为什么逐人格：画像就是"这个人物眼里的世界"，人格隔离是硬约束 —— 人格 A 的画像
        绝不能拿人格 B 的记忆来生成（真机上两个人格曾共用一份画像，属于定性错误）。
        """
        cfg = await self.config()
        if not cfg.get("profile_auto", True):
            return {"skipped": "profile_auto 关闭"}
        if not callable(self.system.get("llm_generate")):
            return {"skipped": "模型通道不可用"}
        # 配置值可能被面板写成非数字（实测会把 int() 抛出去、整轮画像静默停摆）→ 逐个容错回落默认值
        def _int(key: str, dflt: int) -> int:
            try:
                return int(cfg.get(key, dflt) or dflt)
            except (TypeError, ValueError):
                logger.warning(f"[StarHeart] 画像参数 {key}={cfg.get(key)!r} 不是数字，本轮按默认 {dflt} 处理")
                return dflt

        limit = max(1, _int("profile_batch_per_tick", 2))
        min_mem = max(1, _int("profile_min_memories", 5))
        hours = max(1, _int("profile_refresh_hours", 72))
        personas = await self._persona_id_list()
        if not personas:
            # 读不到人格列表时**不能**退回"不筛人格"：那样生成的画像会落成无归属行，
            # 而"全部人格"读取路径会把它们当任何人格的画像用 —— 隔离直接被绕过。
            return {"skipped": "读不到人格列表（AstrBot 人格接口不可用）—— 已跳过本轮，"
                               "避免写入无归属画像；修复后会自动继续",
                    "personas": 0, "built": []}
        done: list[dict[str, Any]] = []
        for persona_id in personas:
            for kind in ("user", "group"):
                try:
                    todo = await self.store.subjects_needing_profile(
                        kind, min_memories=min_mem, refresh_hours=hours, limit=limit, persona_id=persona_id)
                except Exception as exc:                     # 单个人格出错不影响其余
                    done.append({"persona": persona_id, "kind": kind, "ok": False, "err": str(exc)})
                    continue
                for item in todo:
                    try:
                        r = await self.person_profile_build(item["person_id"], kind, persona_id)
                        done.append({"persona": persona_id, "kind": kind, "pid": item["person_id"],
                                     "ok": bool(r.get("ok")), "err": r.get("error", "")})
                    except Exception as exc:
                        done.append({"persona": persona_id, "kind": kind, "pid": item["person_id"],
                                     "ok": False, "err": str(exc)})
        return {"built": done, "personas": len(personas)}

    async def person_profile_build(self, pid: str, subject_kind: str = "user", persona_id: str = "") -> dict[str, Any]:
        """根据主体（人物 L0 / 群聊 L1）的记忆材料，让 LLM 生成/更新一份画像并落库。"""
        pid = str(pid or "").strip()
        k = "group" if str(subject_kind or "").lower() == "group" else "user"
        who = "这个群" if k == "group" else "这个人"
        if not pid:
            return {"ok": False, "error": "缺少主体 id"}
        # 人格必填：画像是"这个人物眼里的世界"，落成无归属行会被"全部人格"读取路径捞出来当
        # 任何人格的画像用（隔离被绕过）。宁可明确拒绝，让调用方先把当前人格解析出来。
        persona_id = "" if str(persona_id) == "__none__" else str(persona_id or "").strip()
        if not persona_id:
            return {"ok": False, "code": "persona_required",
                    "error": "没有确定当前人格，已拒绝生成画像 —— 画像按人格隔离，必须先指定人格再生成"}
        gen = self.system.get("llm_generate")
        if not callable(gen):
            return {"ok": False, "error": "模型通道不可用：先在「设置 → 模型」里配好一个可用的对话模型"}
        try:
            _cfg = await self.config()
            min_mem = int(_cfg.get("profile_min_memories") or 5)
        except Exception:
            _cfg = {}
            min_mem = 5
        min_mem = max(1, min_mem)
        # 记忆时间锚要用「AstrBot 时区」而不是容器本地时间（容器常见是 UTC，会整体差 8 小时）
        from datetime import datetime

        tz = self._tzinfo(_cfg)
        dossier = await self.store.subject_dossier(pid, k, limit=120, persona_id=persona_id)
        mems = dossier.get("memories") or []
        if not mems and not dossier.get("msgs"):
            tail = "先让 bot 在群里多聊几次，或用「导入」补历史记录" if k == "group" else "先让 bot 和对方聊几次，或用「导入」补历史记录"
            return {"ok": False, "error": f"{who}还没有任何记忆和互动记录，没法做画像 —— " + tail}
        # 门槛：材料太少的画像就是模型瞎猜（真机上就生成过「身份未知，仅知为人物 id …」这种垃圾）。
        # 宁可明确拒绝并说清缺什么，也不许把垃圾写进库。
        if len(mems) < min_mem:
            return {"ok": False, "code": "not_enough_material",
                    "error": f"材料不足：{who}的记忆只有 {len(mems)} 条（至少需要 {min_mem} 条）"
                             f"，互动消息 {int(dossier.get('msgs') or 0)} 条。"
                             f"先让它多聊几次，或用「设置 → 危险区 / 导入」补历史对话，再生成画像",
                    "mem_count": len(mems), "min_memories": min_mem}

        if k == "group":
            lines = ["【已知信息】",
                     f"群号：{pid}", f"群名（若已知）：{dossier.get('name') or '未知'}",
                     f"群消息数：{dossier.get('msgs')}，这个群的记忆条数：{len(mems)}",
                     f"涉及会话数：{len(dossier.get('sessions') or [])}"]
            if dossier.get("members"):
                lines.append("活跃成员（按发言量）：" + "、".join(
                    f"{m.get('name') or m.get('user_id')}({m.get('msgs')})" for m in dossier["members"][:16]))
        else:
            lines = ["【已知信息】",
                     f"人物 id：{pid}", f"称呼（若已知）：{dossier.get('name') or '未知'}",
                     f"互动消息数：{dossier.get('msgs')}，记忆条数：{len(mems)}",
                     f"出现在 {len(dossier.get('sessions') or [])} 个会话里"]
        if dossier.get("top_entities"):
            lines.append("常出现的实体：" + "、".join(f"{e['name']}({e['n']})" for e in dossier["top_entities"][:16]))
        if dossier.get("top_cues"):
            lines.append("常出现的线索：" + "、".join(f"{c['name']}({c['n']})" for c in dossier["top_cues"][:16]))
        lines.append("")
        lines.append("【这个群的记忆（按重要性排）】" if k == "group" else "【关于这个人的记忆（按重要性排）】")
        for i, m in enumerate(mems[:70], 1):
            when = ""
            if m.get("occurred_at"):
                try:
                    ts = float(m["occurred_at"])
                    when = (
                        datetime.fromtimestamp(ts, tz).strftime("%Y-%m-%d %H:%M")
                        if tz is not None
                        else time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))
                    )
                except Exception:
                    when = ""
            bits = [f"{i}. [{m.get('atom_type') or ''}{('/' + when) if when else ''}] {m.get('summary') or ''}"]
            if m.get("cues"):
                bits.append("   线索：" + "、".join(str(x) for x in m["cues"][:6]))
            if m.get("entities"):
                bits.append("   实体：" + "、".join(str(x) for x in m["entities"][:8]))
            if m.get("evidence"):
                bits.append("   原文：" + str(m["evidence"])[:160])
            lines.extend(bits)
        prompt = "\n".join(lines) + ("\n\n请据此输出群聊画像 JSON。" if k == "group" else "\n\n请据此输出人物画像 JSON。")

        started = time.time()
        try:
            if k == "group":
                raw = await gen(prompt, "group_profile", self.GROUP_PROFILE_SYSTEM)
            else:
                raw = await gen(prompt, "person_profile", self.PROFILE_SYSTEM)
        except Exception as exc:  # 模型报错也要能被面板看见
            return {"ok": False, "error": f"模型调用失败：{exc}"}
        text = str(raw or "").strip()
        if not text:
            return {"ok": False, "error": "模型没有返回内容（可能是模型通道限流或超时）"}
        body = _json_block(text)
        if body is None:
            # 模型返回数组/标量时，_json_block 拿不到对象 → 以前会把原文塞进 one_line 当画像存下来
            # （面板上就出现「一句话」是 ["a","b"] 这种垃圾）。这类输出明确拒绝，不当画像。
            if text.lstrip()[:1] in ("[", "\"") or text.strip() in ("true", "false", "null"):
                return {"ok": False, "code": "unparseable_output",
                        "error": "模型没有按要求返回 JSON 对象（返回的是数组/标量），已拒绝入库 —— 请重试或换模型"}
            body = {"one_line": text[:200], "notes": "（模型未按要求返回 JSON，这里原样保留输出）"}
        nm = str(dossier.get("name") or "")
        if k == "group":
            body = _sanitize_group_profile(body, gid=pid, name=nm)
            thin = not body.get("one_line") and not body.get("kind") and not body.get("notes")
        else:
            body = _sanitize_profile(body, pid=pid, name=nm)
            thin = not body.get("one_line") and not body.get("identity") and not body.get("notes")
        if thin:
            return {"ok": False, "code": "empty_profile",
                    "error": "模型返回的画像全是坏字段（清洗后为空），已拒绝入库 —— 请重试或换一个模型"}
        await self.store.profile_save(pid, body, mem_count=len(mems), msg_count=int(dossier.get("msgs") or 0),
                                      built_by="llm", subject_kind=k, persona_id=persona_id)
        try:      # 画像生成必须留审计（以前只 log；出问题时无法追溯是谁在什么人格下生成的）
            await self.store.audit("profile.build",
                                   f"{k}:{pid} @ {persona_id or '未归属'} · 记忆 {len(mems)} 条 · "
                                   f"模型 {int((time.time() - started) * 1000)}ms")
        except Exception:
            pass
        return {"ok": True, "person_id": pid, "subject_kind": k, "persona_id": persona_id,
                "profile": body, "mem_count": len(mems),
                "msgs": int(dossier.get("msgs") or 0), "ms": int((time.time() - started) * 1000),
                "updated_at": time.time()}

    async def persons(self, limit: int = 60, persona_id: str = "") -> dict[str, Any]:
        """按人聚合；群聊单独返回（群不是人，别把群号当 QQ 摆进去）。

        bot 自己的 id 只从可信来源取：平台 get_login_info + 配置里的手工排除，
        取到就落库（kind='self'），下次即使平台接口挂了也不会把自己当成「聊过的人」。
        """
        exclude: set[str] = set()
        try:
            cfg = await self.config()
            exclude.update(str(x) for x in (cfg.get("person_exclude_ids") or []) if str(x).strip())
        except Exception:
            pass
        try:
            data = await self.contacts(False)
            for b in data.get("bots") or []:
                uid = str(b.get("user_id") or b.get("id") or "").strip()
                if uid:
                    exclude.add(uid)
        except Exception:
            pass
        if exclude:
            try:
                await self.store.set_bot_self_ids(sorted(exclude))
            except Exception:
                pass
        try:
            ignore = list((await self.config()).get("person_exclude_platforms") or [])
        except Exception:
            ignore = []
        out = await self.store.person_overview(limit, exclude=exclude, ignore_platforms=ignore,
                                               persona_id=str(persona_id or ""))
        if isinstance(out, dict):
            out["bots"] = sorted(exclude)
            return out
        return {"items": out or [], "groups": [], "bots": sorted(exclude)}

    async def favour_insight(self) -> dict[str, Any]:
        return await self.store.favour_insight()

    async def habit_log(self, limit: int = 100) -> dict[str, Any]:
        log = await self.store.habit_log(limit)
        # 语料统计：让「自学习」有可见进度（学了多少条原话 / 用掉多少）
        corpus = {**(await self.store.corpus_stats()), "unused": await self.store.corpus_unused_count()}
        return {
            **log,
            "corpus": corpus,
            "runs": log.get("runs") or [],
            "health": await self._evidence_health(),
            "skipped_reason_enum": _skip_reason_enum(),
        }

    async def corpus_list(self, limit: int = 50, *, is_bot: int | None = None) -> dict[str, Any]:
        """看语料原文（面板「表达学习 → 语料」用）。"""
        return {
            "items": await self.store.list_corpus(limit=limit, is_bot=is_bot),
            "stats": await self.store.corpus_stats(),
        }

    async def prompt_registry(self) -> dict[str, Any]:
        """面板「模型分配」用：每个自管任务的系统提示词（内置/覆盖/生效）与用户模板。"""
        cfg = await self.config()
        items = []
        for task, info in self.TASK_PROMPTS.items():
            default = str(info.get("system") or "")
            override = str(cfg.get(f"sys_prompt_{task}") or "")
            user_key = str(info.get("user_key") or "")
            items.append({
                "task": task,
                "label": info.get("label") or task,
                "desc": info.get("desc") or "",
                "default": default,
                "override": override,
                "effective": override or default,
                "edited": bool(override.strip()),
                "user_key": user_key,
                "user_label": info.get("user_label") or "",
                "user_value": str(cfg.get(user_key) or "") if user_key else "",
                "editable": True,
            })
        return {"items": items,
                "note": "留空 = 使用内置默认。这里只列插件自己调模型的任务；"
                        "对话回复 / 主动开口 / 图像理解走 AstrBot 原生提示词，不在此处（也不该在此处改）。"}

    async def save_task_prompt(self, task: str, system_text: str | None = None) -> dict[str, Any]:
        """保存某任务的系统提示词覆盖（传空字符串 = 恢复内置默认）。"""
        task = str(task or "").strip()
        if task not in self.TASK_PROMPTS:
            return {"ok": False, "error": f"未知任务：{task}"}
        if system_text is None:
            return {"ok": False, "error": "缺少 system 字段"}
        key = f"sys_prompt_{task}"
        await self.store.set_config(key, str(system_text))
        await self.store.audit("prompt.save", f"{task} len={len(str(system_text))}")
        # 与「面板保存配置」走同一条路：写回原生插件配置。否则就是双源 ——
        # 面板改了原生页看不见、原生页改了不生效（原生页只读，权威源仍是插件配置）。
        native: dict[str, Any] = {"ok": False, "error": "未接入原生写回"}
        fn = self.system.get("save_native_config")
        if callable(fn):
            try:
                native = await fn({key: str(system_text)})
            except Exception as exc:                          # noqa: BLE001
                native = {"ok": False, "error": str(exc)[:120]}
        return {"ok": True, "task": task, "edited": bool(str(system_text).strip()), "_native_write": native}

    # ---------------- 配置登记自检（无开关，启动 / 保存后自动跑） ----------------
    def _schema_path(self) -> Path:
        return Path(__file__).resolve().parent.parent / "_conf_schema.json"

    def _load_schema(self, force: bool = False) -> dict[str, Any]:
        path = self._schema_path()
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = 0.0
        cache = getattr(self, "_schema_cache", None)
        if not force and cache and cache[0] == mtime:
            return cache[1]
        data: dict[str, Any] = {}
        try:
            raw = json.loads(path.read_text(encoding="utf-8-sig"))
            if isinstance(raw, dict):
                data = raw
        except Exception as exc:                            # noqa: BLE001
            logger.warning(f"[StarHeart] 读取 _conf_schema.json 失败：{exc}")
        self._schema_cache = (mtime, data)
        return data

    @staticmethod
    def _infer_schema_type(value: Any) -> str:
        if isinstance(value, bool):
            return "bool"
        if isinstance(value, int):
            return "int"
        if isinstance(value, float):
            return "float"
        if isinstance(value, (list, tuple)):
            return "list"
        return "string"

    @staticmethod
    def _field_type(value: Any, spec: dict[str, Any]) -> str:
        t = str(spec.get("type") or "").lower()
        if t in ("list", "array"):
            return "list"
        opts = spec.get("options") or spec.get("enum")
        if isinstance(opts, (list, tuple)) and 0 < len(opts) <= 20 and t in ("", "string", "str", "select", "enum"):
            return "select"
        mapping = {
            "bool": "bool", "boolean": "bool",
            "int": "num", "integer": "num", "float": "num", "number": "num",
            "list": "list", "array": "list",
            "select": "select", "enum": "select",
            "textarea": "textarea",
        }
        if t in mapping:
            return mapping[t]
        if isinstance(value, bool):
            return "bool"
        if isinstance(value, (int, float)):
            return "num"
        if isinstance(value, (list, tuple)):
            return "list"
        return "text"

    def config_fields(self) -> dict[str, list[Any]]:
        """给面板的字段兜底表：任何键都能渲染，不再依赖 views-conf.js 的静态表。"""
        schema = self._load_schema()
        out: dict[str, list[Any]] = {}
        domain_keys = {k for keys in CONFIG_DOMAINS.values() for k in keys}
        for key in sorted((set(schema) | domain_keys | set(DEFAULT_CONFIG)) - CONFIG_NATIVE_ONLY_KEYS):
            if _is_internal_config_key(key) or key in CONFIG_REMOVED_KEYS:
                continue
            spec = schema.get(key) if isinstance(schema.get(key), dict) else {}
            value = DEFAULT_CONFIG.get(key)
            desc = str(spec.get("description") or "").strip()
            name = (desc.split("：", 1)[0].strip()[:28] if desc else "") or str(key)
            ftype = self._field_type(value, spec)
            guard = PARAM_GUARDS.get(key) or {}
            mn = spec.get("min")
            mx = spec.get("max")
            if mn is None:
                mn = guard.get("min")
            if mx is None:
                mx = guard.get("max")
            step = spec.get("step")
            if step is None:
                step = 1 if ftype == "num" and isinstance(value, int) else (0.01 if ftype == "num" else None)
            options = spec.get("options") or spec.get("enum") or []
            if isinstance(options, dict):
                options = list(options.keys())
            if not isinstance(options, (list, tuple)):
                options = []
            out[key] = [name, desc or "（自动补登记）", ftype, mn, mx, step, list(options)]
        return out

    async def config_registry_audit(self, record: bool = True) -> dict[str, Any]:
        """五处登记一致性自检 + 自动补缺（无开关）。

        - 默认值 DEFAULT_CONFIG、配置域 CONFIG_DOMAINS、参数护栏 PARAM_GUARDS、
          原生 schema `_conf_schema.json`、面板字段 `config_fields()`。
        - 新增键只写 DEFAULT_CONFIG 时，自动补 schema；
        - 新增键带面板前缀（proactive_/memory_/…）但没进域时，自动补域；
        - 面板字段由 `config_fields()` 兜底，不再因为漏写 views-conf.js 而消失。
        - schema 写入前自动备份；写失败只告警，不阻塞启动。
        """
        schema = self._load_schema(force=True)
        defaults = DEFAULT_CONFIG
        if not schema:
            # 读不到 schema 时绝不能按默认值全量重建：那会把已有描述/_special 全冲掉。
            logger.error("[StarHeart] 配置自检：_conf_schema.json 为空或不可读，跳过自动补写（避免覆盖）")
            return {"ok": False, "error": "schema_empty", "added_schema": [], "added_domain": {}}
        internal = {k for k in defaults if _is_internal_config_key(k)}
        auto_domain: dict[str, str] = {}
        added_schema: dict[str, Any] = {}
        unclassified_native: list[str] = []

        def _ensure_domain(key: str, dom: str) -> None:
            if not dom:
                return
            keys = CONFIG_DOMAINS.setdefault(dom, [])
            if key not in keys:
                keys.append(key)
                auto_domain[key] = dom

        # 1) 只在 DEFAULT_CONFIG 里、既不在 schema 也不在域：默认补进 schema；带面板前缀的再补域
        domain_keys = {k for keys in CONFIG_DOMAINS.values() for k in keys}
        removed_present = sorted((set(schema) | domain_keys) & CONFIG_REMOVED_KEYS)
        for key in sorted(set(defaults) - domain_keys - set(schema) - internal - CONFIG_REMOVED_KEYS):
            value = defaults[key]
            safe_default = value if isinstance(value, (bool, int, float, str, list)) else ""
            schema[key] = {
                "type": self._infer_schema_type(value),
                "description": f"自动补登记：{key}",
                "default": safe_default,
            }
            added_schema[key] = schema[key]
            _ensure_domain(key, _domain_for_key(key))

        # 2) 在 schema、但不在任何域：已有原生专属键保持不动；新键按前缀补面板域
        domain_keys = {k for keys in CONFIG_DOMAINS.values() for k in keys}
        for key in sorted(set(schema) - domain_keys - internal - set(CONFIG_NATIVE_ONLY_KEYS) - CONFIG_REMOVED_KEYS):
            dom = _domain_for_key(key)
            if dom:
                _ensure_domain(key, dom)
            else:
                unclassified_native.append(key)

        # 3) schema 有、DEFAULT_CONFIG 没有：补运行时默认值（不改代码常量）
        missing_defaults: dict[str, Any] = {}
        for key, spec in schema.items():
            if key not in defaults and key not in CONFIG_REMOVED_KEYS and isinstance(spec, dict) and "default" in spec:
                defaults[key] = spec["default"]
                missing_defaults[key] = spec["default"]

        # 4) 写回 schema（仅新增项；先备份）
        schema_write = "skipped"
        if added_schema:
            path = self._schema_path()
            try:
                st = path.stat() if path.exists() else None
                if st is not None:
                    bak = path.with_name(path.name + ".bak-registry-" + time.strftime("%Y%m%d-%H%M%S"))
                    shutil.copy2(path, bak)
                    if hasattr(os, "chown"):
                        try:
                            os.chown(bak, st.st_uid, st.st_gid)
                        except OSError:
                            pass
                tmp = path.with_name(path.name + ".tmp")
                tmp.write_text(json.dumps(schema, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                tmp.replace(path)
                if st is not None:
                    # 容器里通常以 root 跑；把属主/权限还原，避免宿主用户以后改不了插件文件
                    if hasattr(os, "chown"):
                        try:
                            os.chown(path, st.st_uid, st.st_gid)
                            os.chmod(path, st.st_mode)
                        except OSError:
                            pass
                schema_write = "ok"
            except Exception as exc:                        # noqa: BLE001
                schema_write = f"failed: {exc}"
                logger.warning(f"[StarHeart] 配置自检：写回 _conf_schema.json 失败（下轮重试）：{exc}")
        self._schema_cache = (0.0, schema)

        # 5) 汇总
        domain_keys = {k for keys in CONFIG_DOMAINS.values() for k in keys}
        seen_domains: dict[str, str] = {}
        duplicate_domains: list[str] = []
        for dom, keys in CONFIG_DOMAINS.items():
            for key in keys:
                if key in seen_domains and seen_domains[key] != dom:
                    duplicate_domains.append(f"{key}:{seen_domains[key]}/{dom}")
                else:
                    seen_domains[key] = dom
        domain_missing = sorted(domain_keys - set(defaults))
        domain_missing_schema = sorted(domain_keys - set(schema) - CONFIG_REMOVED_KEYS)
        guards_missing = sorted(
            k for k in domain_keys
            if isinstance(defaults.get(k), (int, float)) and not isinstance(defaults.get(k), bool)
            and k not in PARAM_GUARDS
        )
        stale_guards = sorted(set(PARAM_GUARDS) - set(defaults))
        fields = self.config_fields()
        fields_missing = sorted(k for k in domain_keys if k not in fields)
        report = {
            "ok": not (domain_missing or domain_missing_schema or stale_guards or unclassified_native
                       or fields_missing or duplicate_domains or removed_present
                       or schema_write.startswith("failed")),
            "defaults": len(defaults), "domains": len(domain_keys), "schema": len(schema),
            "fields": len(fields),
            "added_schema": sorted(added_schema), "added_domain": auto_domain,
            "missing_defaults": sorted(missing_defaults),
            "domain_missing_default": domain_missing,
            "domain_missing_schema": domain_missing_schema,
            "duplicate_domains": duplicate_domains,
            "removed_present": removed_present,
            "missing_guards": guards_missing, "stale_guards": stale_guards,
            "unclassified_native": unclassified_native,
            "fields_missing": fields_missing, "schema_write": schema_write,
        }
        logger.info(
            f"[StarHeart] 配置自检：默认 {len(defaults)} / 域 {len(domain_keys)} / "
            f"schema {len(schema)} / 字段 {len(fields)}；补 schema {len(added_schema)}、补域 {len(auto_domain)}"
        )
        if added_schema:
            logger.info(f"[StarHeart] 配置自检：自动补 schema {sorted(added_schema)}（{schema_write}）")
        if auto_domain:
            logger.info(f"[StarHeart] 配置自检：自动补面板域 {auto_domain}")
        if (domain_missing or domain_missing_schema or stale_guards or unclassified_native
                or fields_missing or missing_defaults or duplicate_domains or removed_present):
            logger.warning(
                "[StarHeart] 配置自检需人工确认："
                f"域键缺默认值={domain_missing} 域键缺schema={domain_missing_schema} "
                f"schema键缺代码默认值={sorted(missing_defaults)} "
                f"重复域键={duplicate_domains} 已删除但仍登记={removed_present} "
                f"过期护栏={stale_guards} 未分类原生键={unclassified_native} 缺字段={fields_missing}"
            )
        if record:
            try:
                await self.store.audit("config.audit", json.dumps(report, ensure_ascii=False)[:1000], "")
            except Exception as exc:                        # noqa: BLE001
                logger.debug(f"[StarHeart] 配置自检审计写入失败：{exc}")
        return report

    async def config_domains(self) -> dict[str, Any]:
        cfg = await self.config()
        # builtin：面板要展示/「载入默认」的内置文本（第 2 层提示词模板；留空配置时实际生效的就是它）
        # guards：参数护栏清单（输入框上下界 / 「已拦下」判定都以它为准，面板不再自己抄一份）
        # fields：字段兜底表（任何 DEFAULT_CONFIG / schema 键都能渲染，漏写 views-conf.js 也不会消失）
        return {"domains": CONFIG_DOMAINS, "values": cfg, "guards": guards_payload(),
                "fields": self.config_fields(),
                "builtin": {"proactive_instruction": PROACTIVE_LAYER2_TEMPLATE}}

    # ---------------- 表达提取（真跑一次 LLM） ----------------
    async def _active_pid(self) -> str:
        """当前生效人格 id（表达归属 学习·{pid} / 手加·{pid} 用；拿不到就空串 → 退化写法）。"""
        fn = self.system.get("active_persona")
        if not callable(fn):
            return ""
        try:
            return str(await _maybe_await(fn()) or "")
        except Exception as exc:                            # noqa: BLE001
            logger.debug(f"[StarHeart] 取当前人格失败（表达归属退化）：{exc}")
            return ""

    def _expr_scopes(self, persona_id: str) -> set[str] | None:
        """当前人格可见的 scope 集合（E1）。

        pid 空 = 没开人格隔离 → None（不过滤 scope）；pid 有值 → **fail-closed**：
        只认 学习·{pid} / 全局 / 手加·{pid}，无匹配就是空 —— 原实现在「一条都没匹配上」时
        回退成「全部 enabled」，于是别的人格条目会串进当前人格（真机可复现）。
        """
        pid = str(persona_id or "")
        if not pid:
            return None
        return {f"学习·{pid}", "全局", f"手加·{pid}"}

    def _expr_availability(
        self, h: dict[str, Any], *, persona_id: str, review_first: bool, min_uses: int,
        banned: set[str], now_ts: float,
    ) -> tuple[str, bool]:
        """只读求值这条现在能不能进当前人格的注入池（S4-3 available_now + S4-5 隔离徽标）。

        返回 (人话标签, 当前人格可注入)。判定顺序与注入侧 habit_hints_detail 完全一致 ——
        面板显示的「可用」就是真注入会选的（不做第二套口径）。
        """
        scopes = self._expr_scopes(persona_id)
        state = str(h.get("checked_state") or "")
        norm_key = str(h.get("norm_key") or "")
        if str(h.get("status") or "active") != "active":
            return "已清空（软删，可恢复）", False
        if scopes is not None and str(h.get("scope") or "") not in scopes:
            return "不在当前人格", False
        if state == "user_reject" or (norm_key and norm_key in banned):
            return "已驳回（进了「不再学」名单）", False
        if review_first and state != "user_ok":
            return "待审核（先审后用开着）", False
        if not h.get("enabled"):
            return "已停用", False
        if float(h.get("cooldown_until") or 0) > now_ts:
            return "冷却中", False
        if int(h.get("injected_24h") or 0) >= EXPR_WINDOW_MAX_INJECT:
            return "24 小时内已注入满 2 次", False
        if int(h.get("use_count") or 0) < max(0, int(min_uses or 0)):
            return "未达最小次数（名额有余就补位）", True
        return "可注入", True

    async def habit_extract(self, limit: int = 60) -> dict[str, Any]:
        """从群友/好友的真实原话里学说话习惯（语料库是数据源，记忆摘要只做辅助）。

        S4 口径：每次运行都写一行 hf_expr_run_log（S4-4）—— 学 0 条也写，并且写清**为什么 0 条**；
        学到的行带出处（corpus_id / message_id / 原话 / 时间 / 会话 / 说话人，E3）；
        「不再学」名单里的说法一律跳过（S4-2：修「删了还会再学」）。
        """
        cfg = await self.config()
        pid = await self._active_pid()
        model = str(cfg.get("model_expression_extract") or "")
        review_first = _as_bool(cfg.get("expression_review_first", False))
        gen = self.system.get("llm_generate")
        if not callable(gen):
            await self.store.log_expr_run({"scanned": 0, "saved": 0, "skipped_reason": "model_unavailable",
                                           "model": model, "ok": 0, "persona_id": pid})
            return {"ok": False, "error": "模型通道不可用", "saved": 0, "scanned": 0,
                    "skipped_reason": "model_unavailable"}

        async def _finish(scanned: int, saved: int, reason: str, ok: int = 1,
                          extra: dict[str, Any] | None = None) -> dict[str, Any]:
            await self.store.log_expr_run({
                "scanned": int(scanned), "saved": int(saved), "skipped_reason": str(reason or ""),
                "model": model, "ok": ok, "persona_id": pid,
            })
            await self.store.audit(
                "habit.extract",
                f"corpus={int(scanned)} saved={int(saved)} reason={reason or '-'} persona={pid}",
            )
            out: dict[str, Any] = {"ok": bool(ok), "saved": int(saved), "scanned": int(scanned),
                                   "persona": pid, "skipped_reason": str(reason or "")}
            out.update(extra or {})
            return out

        # 优先取没学过的群友/好友原话
        rows = await self.store.list_corpus(limit=limit, is_bot=0, unused_only=True)
        used_ids = [int(r["id"]) for r in rows]
        if len(rows) < 10:  # 语料不够时拿已用过的补齐，保证能学
            rows += await self.store.list_corpus(limit=limit - len(rows), is_bot=0)
        if not rows:
            await self.store.log_expr_run({"scanned": 0, "saved": 0, "skipped_reason": "corpus_insufficient",
                                           "model": model, "ok": 0, "persona_id": pid})
            return {"ok": False, "saved": 0, "scanned": 0, "skipped_reason": "corpus_insufficient",
                    "error": "还没有群友/好友的语料——先在群里聊几句（面板「语言学习 → 立即学一批」）"}
        # 出处侧车（E3）：这些语料对应的原生 message_id
        refs = await self.store.corpus_ref_map([int(r["id"]) for r in rows])
        by_chat: dict[str, list[dict[str, Any]]] = {}
        for r in rows:
            body = str(r.get("text") or "").strip()[:120]
            if not body:
                continue
            who = r.get("user_name") or r.get("user_id") or "某人"
            by_chat.setdefault(f"{r.get('chat_type') or '群聊'}·{who}", []).append(dict(r))
        # 编号原话列表：prompt 要求模型按「行号」回填出处，所以必须先编号，再把编号写进样本
        numbered: list[tuple[int, str, str, dict[str, Any]]] = []      # (行号, 场景标签, 原话, 语料行)
        for tag, items in by_chat.items():
            for item in items[:12]:
                body = str(item.get("text") or "").strip()[:120]
                if not body:
                    continue
                numbered.append((len(numbered) + 1, tag, body, item))
        numbered = numbered[:60]
        # 逐行拼样本，超 3500 字就停：样本里出现的编号一定在 by_idx 里（模型不会引用到被截掉的行）
        sample_lines: list[str] = []
        used = 0
        for idx, tag, line, _item in numbered:
            item_txt = f"{idx}. [{tag}] {line}"
            if used + len(item_txt) > 3500:
                break
            sample_lines.append(item_txt)
            used += len(item_txt)
        sample = "\n".join(sample_lines)
        prompt = (
            "下面是我这个聊天机器人的群友和好友平时真实说的话（原文照抄，不要改）。\n"
            "请观察他们怎么说话，总结出这个机器人**可以直接套用的说话套路**。\n\n"
            "硬性要求：\n"
            "1. 每条一行，格式：什么时候用|怎么说|参考了第几行原话（可多个，逗号分隔；没参考就留空）\n"
            "2. **不要整句照抄原话**，要抽象成可复用的句式或词。比如原话里出现「我真的会谢」，"
            "写成「夸张地表达无语 → 说『我真的会谢』」；不要写成「私聊 → 我真的会谢 笑不活了」。\n"
            "3. 场景要是**社交情境**（被夸/被调侃/无语/同意/想炫耀/关心人/敷衍…），"
            "不要写「群聊」「私聊」这种聊天场所。\n"
            "4. 说法要具体、能直接说出口，带上原话里的口头禅、语气词、句尾拖音写法。\n"
            "5. 最多 6 条，宁少勿滥。没有值得学的就只输出一行「无」。\n"
            "6. 第三段只填你真的参考过的原话行号（上面编号列表里的号），别瞎填；不要编号、不要 markdown、不要解释。\n\n"
            f"【他们的原话】\n{sample}"
        )
        try:
            out = await gen(prompt, "expression_extract")
        except Exception as exc:                            # noqa: BLE001 模型调用失败也要留诊断行
            logger.warning(f"[StarHeart] 表达提取模型调用失败：{exc}")
            return await _finish(len(rows), 0, "model_error", ok=0,
                                 extra={"error": f"模型调用失败：{str(exc)[:120]}"})
        banned = await self.store.banned_keys()
        saved = merged = banned_hit = 0
        by_idx = {idx: (line, item) for idx, _tag, line, item in numbered}
        for raw in str(out or "").splitlines():
            line = raw.strip().lstrip("-*0123456789. ")
            if "|" not in line:
                continue
            parts = [x.strip() for x in line.split("|")]
            scene = parts[0] if parts else ""
            way = parts[1] if len(parts) > 1 else ""
            if not scene or not way or way in ("无", "none"):
                continue
            norm_key = expr_norm_key(scene, way)
            if norm_key and norm_key in banned:
                banned_hit += 1                             # 「不再学」名单先过（S4-2）
                continue
            # 出处：模型回填的行号 → 真实原话（最多 3 条）。回填不出来就留空，
            # 面板会明确标「没记出处」，不假装有出处。
            cues: list[str] = []
            src_ids: list[dict[str, Any]] = []
            evidence: list[dict[str, Any]] = []
            for tok in str(parts[2] if len(parts) > 2 else "").replace("，", ",").split(","):
                tok = tok.strip().lstrip("#")
                if not tok.isdigit():
                    continue
                hit = by_idx.get(int(tok))
                if not hit:
                    continue
                text, row = hit
                cid = int(row.get("id") or 0)
                who = str(row.get("user_name") or row.get("user_id") or "某人")
                ts_row = float(row.get("ts") or 0)
                if text[:60] not in cues:
                    cues.append(text[:60])
                ref = refs.get(cid) or {}
                mid = str(ref.get("message_id") or "")
                src_ids.append({"corpus_id": cid, "message_id": mid,
                                "session_id": str(row.get("session_id") or ""), "who": who, "ts": ts_row})
                evidence.append({"text": text, "ts": ts_row, "session_id": str(row.get("session_id") or ""),
                                 "who": who, "corpus_id": cid, "message_id": mid})
                if len(cues) >= 3:
                    break
            _eid, is_new = await self.store.merge_expr({
                "scene": scene[:40], "way": way[:120], "cues": cues,
                "scope": f"学习·{pid}" if pid else "学习",
                "enabled": 1, "norm_key": norm_key,
                # 默认自动通过（定稿口径 #2）：没开「先审后用」就直接 user_ok；
                # 合并路径（merge_expr）**不动**已有行的审核态 —— 学习回写绝不回退审核。
                "checked_state": "ai_pending" if review_first else "user_ok",
                "source_ids": src_ids, "evidence_snap": evidence,
            })
            if is_new:
                saved += 1
            else:
                merged += 1
        # 语料标记：只有真学到东西才算「学过」。学 0 条不标记 ——
        # 原实现无条件标记，一次失败就让这批语料永久沉底（S4-4 复核项）。
        if (saved or merged) and used_ids:
            await self.store.mark_corpus_used(used_ids)
        await self.store.prune_corpus(5000)                 # 被引用语料免删（E3）
        total = saved + merged
        if banned_hit and total == 0:
            reason = "banned_filtered"
        elif total == 0:
            reason = "empty_parse"
        elif saved == 0:
            reason = "all_duplicate"
        elif banned_hit:
            reason = "banned_filtered"
        else:
            reason = ""
        return await _finish(len(rows), total, reason,
                             extra={"new": saved, "merged": merged, "banned": banned_hit})

    # 只有「像图片相关的失败」才值得降级重跑纯文本；纯超时再跑一次只会多烧一份 prompt。
    _IMAGE_FAIL_HINTS = ("image", "图片", "视觉", "vision", "multimodal", "media", "base64",
                         "image_url", "unsupported", "invalid content", "content type",
                         "400", "415", "422")

    def image_related_failure(self, res: dict[str, Any]) -> bool:
        """判断这次失败是否冲着「模型看不了图」去的（决定要不要降级重跑纯文本）。"""
        err = str((res or {}).get("error") or "").lower()
        if not err:
            return False
        if err.startswith("unparsed_model_output"):
            # 模型把「看不了图」写成自然语言解释 → 有输出但解析不出条目
            return True
        return any(h in err for h in self._IMAGE_FAIL_HINTS)

    @staticmethod
    def _is_no_memory_reply(raw: str) -> bool:
        """模型**明确**表示「这批没有值得记的」→ 可以安全丢弃这一批。

        只认短回复里的明确标记：超时/空响应是空串，长篇解释与格式跑偏都不算「无」，
        要按失败重试而不是删批 —— 旧实现用 `if not items` 一把梭，把两者混成同一个结局。
        """
        body = str(raw or "").strip().strip("。.!！?？~～\"'“”").strip()
        if not body or len(body) > 40:
            return False
        low = body.lower()
        if low in ("无", "空", "none", "nothing", "no", "没有"):
            return True
        markers = ("没有值得记", "无值得记", "没有可记", "无需记录", "无需记忆",
                   "没有需要记", "没有值得长期", "nothing to remember", "no memory")
        return any(m in low for m in markers)

    async def summarize_dialogue(
        self,
        turns: list[dict[str, str]],
        *,
        session_id: str = "",
        user_id: str = "",
        user_name: str = "",
        persona_id: str = "",
        image_urls: list[str] | None = None,
    ) -> dict[str, Any]:
        """把一轮对话蒸馏成记忆条目（聊天记忆自动总结）。

        image_urls：图片直接交给模型自己看（原生多模态）。只有这一条路 —— 「先用独立图像模型识别、
        再喂给文本模型」已整段下线（那个模型吃不到 AstrBot 的提示词）。

        turns: [{"user": "...", "bot": "..."}]，由调用方按会话累积。
        走「模型分配」里的 memory_summary 模型；解析失败/无内容都不抛错，只记 audit。
        """
        gen = self.system.get("llm_generate")
        if not callable(gen):
            return {"ok": False, "outcome": "retry", "error": "模型通道不可用"}
        cfg = await self.config()
        if not cfg.get("memory_capture_enabled", True):
            # 用户关掉捕获 = 暂停，不是「这批不用记」：返回 hold，由 worker 保留行并推后重试，
            # 开关重新打开后自动续跑（旧实现这里 skipped → 直接删队列，等于关开关丢对话）。
            return {"ok": True, "outcome": "hold", "reason": "capture_disabled"}
        cleaned = [
            t for t in turns
            if ((t.get("user") or "").strip() or (t.get("bot") or "").strip()
                or (t.get("imgs") or []))
            # 管理指令不是对话内容：漏进缓冲也别写进长期记忆
            # （真机上抓到过一条「TA 发过 /hf mute m989 我没接住」的垃圾记忆）
            and not (t.get("user") or "").strip().startswith("/")
        ]
        if not cleaned:
            # 空批是「无内容」，不是「失败」：旧实现返回 ok=False → 被当失败重试 5 次再搁置一年。
            # 纯图片轮次（user/bot 文本都为空、只有 imgs）是**有内容**的批：放行后必须走带图调用，
            # 否则 P1-1 修了等于没修（纯图会被当成 empty_batch 直接删掉）。
            return {"ok": True, "outcome": "skipped", "reason": "empty_batch"}
        limit = cfg.get("memory_capture_max_per_run")
        limit = 5 if limit is None else int(limit)
        # 不再截断到「末 12 轮」：配置允许 50 轮，却只把末 12 轮喂给模型，前面白扫。
        # 改成按字符预算装（超预算从最早的一轮开始丢，保留最近），并如实回报 scanned。
        budget_chars = 12000
        kept: list[dict[str, Any]] = []
        used_chars = 0
        for t in reversed(cleaned):
            who_seg = (t.get("name") or "").strip()
            seg_len = (len(str(t.get("user") or "")) + len(str(t.get("bot") or "")) + len(who_seg) + 8)
            if kept and used_chars + seg_len > budget_chars:
                break
            kept.append(t)
            used_chars += seg_len
        convo_lines: list[str] = []
        for i, t in enumerate(reversed(kept), 1):
            who_s = (t.get("name") or "").strip() or "用户"
            u = (t.get("user") or "").strip().replace("\n", " ")[:200]
            b = (t.get("bot") or "").strip().replace("\n", " ")[:200]
            imgs = [x for x in (t.get("imgs") or []) if x]
            if u or b:
                convo_lines.append(f"{i}. {who_s}：{u}\n   我：{b}")
            elif imgs:
                # 纯图片轮：正文为空，但图会随 image_urls 交给模型看，转写里必须留位置。
                convo_lines.append(f"{i}. {who_s}：[发送了 {len(imgs)} 张图片，未附文字]")
        if not convo_lines:
            # cleaned 非空但全是空白：真·无内容（图片轮次已在上面保留）。
            return {"ok": True, "outcome": "skipped", "reason": "empty_batch"}
        who = f"对话对象：{user_name}（{user_id}）" if user_id else ""
        prompt = (
            f"下面是一段人机对话。请提取值得长期记住的信息，最多 {limit} 条。每条一行，格式：\n"
            "摘要|类型|强度|情绪浓度\n"
            "类型 ∈ planned/factual/preference/routine/episodic/relational/opinion；"
            "强度与情绪浓度 0-1。\n"
            "写法：\n"
            "1. 双方都写：用户做了什么、助手怎么回、助手答应了什么；关系是双向的。\n"
            "2. 尽量写清谁对谁；主语可省略，要写就用「TA」指对方、「我」指助手自己。\n"
            "3. 称呼、调情、亲密/性相关互动照实记：双方意愿、边界、称呼、偏好；不做道德评判、不删减。\n"
            "4. 不编造；材料不够就少写；没有值得记的就输出「无」。\n"
            "5. 不要编号、不要 markdown、不要解释；只写内容本身。\n\n"
            f"{who}\n对话内容：\n" + "\n".join(convo_lines)
        )
        custom_prompt = str(cfg.get("memory_summary_prompt") or "").strip()
        if custom_prompt:
            # 「记忆总结自定义提示词」：只替换"指令段"。{transcript} 占位 = 对话正文；
            # 没写占位就把自定义提示词接在内置提示词前面。输出格式行（摘要|类型|强度|情绪浓度）
            # 仍按内置规则解析 —— 自定义时务必保留格式行，否则解析不到记忆。
            transcript = f"{who}\n对话内容：\n" + "\n".join(convo_lines)
            prompt = (
                custom_prompt.replace("{transcript}", transcript)
                if "{transcript}" in custom_prompt
                else custom_prompt + "\n\n" + prompt
            )
        try:
            # 图片必须真的传给模型：收了 image_urls 却不传参 = 图片 100% 进不了记忆（P0-8）。
            # 失败时上层会降级重跑纯文本，绝不换一个「吃不到提示词」的图像模型去描述。
            out = await gen(prompt, "memory_summary", None, image_urls=image_urls or None)
        except Exception as exc:  # LLM 故障不阻塞对话
            await self.store.audit("memory.capture", f"llm_error {exc}")
            return {"ok": False, "outcome": "retry", "error": str(exc)}
        raw = str(out or "").strip()
        if not raw:
            # 空输出 ≠「这批没有值得记的」：超时 / 空响应 / 模型槽位指向已删除的模型都到这儿。
            # 旧实现把它并进 `if not items` 当 ok=True 删 pending → 整批对话永久丢失（P0-1）。
            await self.store.audit(
                "memory.capture", f"empty_model_output turns={len(kept)}/{len(cleaned)}")
            return {
                "ok": False, "outcome": "retry", "error": "empty_model_output",
                "scanned": len(kept), "turns_total": len(cleaned),
            }
        items: list[dict[str, Any]] = []
        for line in str(out or "").splitlines():
            line = line.strip().lstrip("-*0123456789. ").strip()
            if not line or "|" not in line:
                continue
            parts = [x.strip() for x in line.split("|")]
            summary = parts[0][:200]
            # 插件自己的好感标签不是记忆内容，别让它混进长期记忆
            summary = re.sub(r"[\[【(（]?\s*好感\s*[:：]?\s*[+＋-]?\s*\d+\s*[\]】)）]?", "", summary).strip()
            if not summary or summary in ("无", "none", "None"):
                continue
            atom = (parts[1] if len(parts) > 1 else "factual").lower()
            if atom not in _ATOM_TYPES:
                atom = "factual"
            try:
                strength = float(parts[2]) if len(parts) > 2 else 0.5
            except ValueError:
                strength = 0.5
            try:
                poignancy = float(parts[3]) if len(parts) > 3 else 5.0
            except ValueError:
                poignancy = 5.0
            strength = min(1.0, max(0.05, strength))
            if 0 < poignancy <= 1.0:      # 旧输出是 0-1 制：≤1 一律放大成 0-10，否则 0.7→1 让「情绪保护」永不生效
                poignancy *= 10.0
            poignancy = int(round(min(10.0, max(0.0, poignancy))))
            items.append(
                {
                    "summary": summary,
                    "atom_type": atom,
                    "strength": strength,
                    "poignancy": poignancy,
                }
            )
            if len(items) >= limit:
                break
        if not items:
            if self._is_no_memory_reply(raw):
                await self.store.audit(
                    "memory.capture", f"model_none turns={len(kept)}/{len(cleaned)}")
                return {
                    "ok": True, "outcome": "skipped", "reason": "model_none", "saved": 0,
                    "scanned": len(kept), "turns_total": len(cleaned),
                }
            # 有输出但一条都解析不出来（格式跑偏 / 模型在解释）同样是失败：重试，不能当「无内容」删批。
            await self.store.audit(
                "memory.capture", f"unparsed_model_output turns={len(kept)}/{len(cleaned)}")
            return {
                "ok": False, "outcome": "retry",
                "error": f"unparsed_model_output: {raw[:120]}",
                "scanned": len(kept), "turns_total": len(cleaned),
            }
        saved = 0
        deduped = 0
        linked = 0
        link_errors = 0
        evidence = "\n".join(
            f"用户：{(t.get('user') or '').strip()[:80]}\n助手：{(t.get('bot') or '').strip()[:80]}"
            if (t.get("user") or t.get("bot"))
            else f"用户：[{len(t.get('imgs') or [])} 张图片]"
            for t in cleaned[-3:]
            if (t.get("user") or t.get("bot") or t.get("imgs"))
        )[:500]
        # 撤回反查侧车：把这一批的 message_id 挂到每条新记忆上。
        # source_ref 是 `capture:{session}`，与 message_id 永不相等 —— 没有侧车表时
        # recall_handling=weaken/archive 对自动总结的记忆恒为 0 命中（P1-3）。
        msg_ids: list[str] = []
        for t in cleaned:
            mm = str(t.get("mid") or "").strip()
            if mm and mm not in msg_ids:
                msg_ids.append(mm)
        # 同人格、同会话的近期记忆：新条目如果只是换了个说法，就并回旧条目，
        # 不再新增一条。同一件事换三种说法在短时间写入多条，是实机出现过的退化路径。
        existing: list[tuple[str, str, str]] = []
        try:
            rows_ex = await (await self.store.db.execute(
                "SELECT id, summary, content FROM hf_memories WHERE status='active' "
                "AND COALESCE(persona_id,'')=? AND session_id=? "
                "ORDER BY COALESCE(occurred_at, created_at) DESC LIMIT 300",
                (persona_id or "", session_id or ""),
            )).fetchall()
            existing = [(str(r["id"]), str(r["summary"] or ""), str(r["content"] or "")) for r in rows_ex]
        except Exception as exc:                    # noqa: BLE001 去重查询失败就按原逻辑插入
            logger.debug(f"[StarHeart] 记忆去重查询失败（按不去重处理）：{exc}")
        for it in items:
            replace_id = None
            dup_id = None
            new_summary = it["summary"]
            new_norm = engine._norm_for_similarity(new_summary)
            for old_id, old_summary, old_content in existing:
                old_norm = engine._norm_for_similarity(old_summary)
                # 只替换「摘要就是全文」的简单记忆；导入记忆的 content 是长原文，
                # 用一句新摘要覆盖会把原文整段吃掉，宁可新增一条。
                simple_old = (not old_content.strip()) or old_content.strip() == old_summary.strip()
                if (simple_old and len(old_norm) >= 6 and old_norm and new_norm
                        and old_norm in new_norm and len(new_norm) > len(old_norm) + 2):
                    # 新记忆是旧记忆的补充/纠正（「怕黑」→「怕黑，睡觉要留灯」）：
                    # 用新内容替换旧行，保留原始发生时间，不把它顶成「刚刚」。
                    replace_id = old_id
                    break
                if old_summary and engine.text_similarity(new_summary, old_summary) >= 0.995:
                    dup_id = old_id
                    break
            if replace_id:
                try:
                    did = int(str(replace_id).lstrip("m"))
                    now = time.time()
                    await self.store.db.execute(
                        "UPDATE hf_memories SET summary=?, content=?, cues=?, "
                        "strength=MIN(1.0, COALESCE(strength,0)+0.15), "
                        "recalls=COALESCE(recalls,0)+1, updated_at=? WHERE id=?",
                        (new_summary, new_summary,
                         json.dumps([new_summary[:24]], ensure_ascii=False),
                         now, did),
                    )
                    await self.store.db.commit()
                except Exception as exc:            # noqa: BLE001
                    logger.debug(f"[StarHeart] 记忆补充失败：{exc}")
                if msg_ids:
                    await self.store.link_memory_messages(replace_id, msg_ids)
                deduped += 1
                logger.info(f"[StarHeart] 记忆补充：{new_summary[:40]} → 更新 #{replace_id}")
                continue
            if dup_id:
                try:
                    await self.store.touch_memory(dup_id)
                except Exception as exc:            # noqa: BLE001
                    logger.debug(f"[StarHeart] 记忆去重加固失败：{exc}")
                if msg_ids:
                    await self.store.link_memory_messages(dup_id, msg_ids)
                deduped += 1
                logger.info(f"[StarHeart] 记忆去重：{new_summary[:40]} → 刷新 #{dup_id}")
                continue
            mid = await self.store.insert_memory(
                {
                    "summary": it["summary"],
                    "content": it["summary"],
                    "atom_type": it["atom_type"],
                    "strength": it["strength"],
                    "poignancy": it["poignancy"],
                    "source_mode": "dialogue",
                    "source_ref": f"capture:{session_id}",
                    "session_id": session_id,
                    "persona_id": persona_id or "",
                    "tags": ["自动总结"],
                    "entities": [user_name] if user_name else [],
                    "cues": [it["summary"][:24]],
                    "evidence": evidence,
                    "recalls": 0,
                    "status": "active",
                    # 时间锚：这批对话第一轮的真实时间，而不是总结完成时刻（P0-9）
                    "occurred_at": float((kept[-1] or {}).get("ts") or 0) or None,
                    # 归属主体：同人格多个私聊时，A 的记忆不该原样注入给 B（P0-10）。
                    # 群聊不写：那里的「主体」是群，不是最后说话的那个人。
                    "subject_id": str(user_id or "") if session_kind(session_id)[0] == "private" else "",
                }
            )
            if mid:
                saved += 1
                if msg_ids:
                    await self.store.link_memory_messages(mid, msg_ids)
                existing.append((str(mid), it["summary"], it["summary"]))
                # 关键：新记忆立刻建关系链（实体边 + 同会话时序边 + 共享实体语义边）。
                # 不建链的话图谱就是一堆孤点，用户看到的就是「没有关系链」。
                try:
                    links = await self.store.link_memory(
                        mid,
                        entities=[user_name] if user_name else [],
                        session_id=session_id,
                        persona_id=persona_id or "",
                        cues=[it["summary"][:24]],
                    )
                    linked += sum(links.values())
                except Exception:  # 建链失败不影响记忆本身落库，但要能看见
                    link_errors += 1
        await self.store.audit(
            "memory.capture",
            f"saved={saved} deduped={deduped} turns={len(kept)}/{len(cleaned)} "
            f"session={session_id} links={linked} link_errors={link_errors}",
        )
        return {
            "ok": True,
            "outcome": "done",
            "saved": saved,
            "deduped": deduped,
            "scanned": len(kept),
            "turns_total": len(cleaned),
            "links": linked,
            "link_errors": link_errors,
            "ids": [],
        }

    async def purge_stale(self, persona_id: str | None = None) -> dict[str, Any]:
        """清理非活跃记忆（软删）。传 persona_id 只作用于该人格，不传 = 全部人格。"""
        n = await self.store.purge_stale_memories(persona_id)
        await self.store.audit("memory.purge_stale", f"{n} 条" + (f"（人格 {persona_id}）" if persona_id else "（全部人格）"))
        return {"removed": n, "persona_id": persona_id or "", "mode": "soft_delete"}

    async def memory_maintenance(self) -> dict[str, Any]:
        """记忆衰减 + 淘汰（设计里的「向量记忆与衰减」落地点）。

        2026-10-17：安全阀按 persona 分别算。旧实现拿全局 active 比例，
        历史人格 archived 很多时会把全局压到 15% 以下，导致当前人格的记忆也永远不衰减
        （当前人格 active 正常、历史人格全是 archived 的库会踩到）。
        """
        cfg = await self.config()
        half = 45.0 if cfg.get("memory_decay_half_life_days") is None else float(cfg["memory_decay_half_life_days"])
        floor = 0.05 if cfg.get("memory_evict_below") is None else float(cfg["memory_evict_below"])
        if half <= 0:
            return {"decayed": 0, "evicted": 0}
        await self.store.ensure_strength_updated_at()
        groups = await (await self.store.db.execute(
            "SELECT COALESCE(persona_id,'') AS pid, COUNT(*) AS total, "
            "       SUM(CASE WHEN status='active' THEN 1 ELSE 0 END) AS act "
            "FROM hf_memories WHERE status != 'deleted' GROUP BY COALESCE(persona_id,'')"
        )).fetchall()
        decayed = evicted = skipped = 0
        stamp = time.time()
        for g in groups:
            pid = str(g["pid"] or "")
            total = int(g["total"] or 0)
            act = int(g["act"] or 0)
            _ = (total, act)
            # 已删除「active 比例 <15% 就冻结衰减」的阀门：它让老库永远不衰减，
            # 且把「记忆多」当成异常状态 —— 记忆变多是正常事，不该反过来冻结维护。
            rows = await (await self.store.db.execute(
                "SELECT id, strength, atom_type, COALESCE(NULLIF(strength_updated_at,0), NULLIF(last_used_at,0), "
                "         occurred_at, created_at) AS ts FROM hf_memories "
                "WHERE status='active' AND COALESCE(persona_id,'')=?",
                (pid,),
            )).fetchall()
            for row in rows:
                cur = float(row["strength"] or 0)
                # 增量幂等：只补算「距上次衰减基线」的这段时间；旧实现拿累计年龄反复乘当前强度 = 复利崩塌
                # 半衰期按 atom_type 分化：称呼/雷点/承诺（relational/preference/planned）掉得最慢
                new_s = engine.decay_strength(cur, engine.age_days(row["ts"]), half,
                                             atom_type=str(row["atom_type"] or ""))
                if new_s >= cur:
                    continue
                decayed += 1
                if new_s < floor:
                    evicted += 1
                    await self.store.db.execute(
                        "UPDATE hf_memories SET strength=?, strength_updated_at=?, status='stale', updated_at=? WHERE id=?",
                        (new_s, stamp, stamp, row["id"]),
                    )
                else:
                    await self.store.db.execute(
                        "UPDATE hf_memories SET strength=?, strength_updated_at=?, updated_at=? WHERE id=?",
                        (new_s, stamp, stamp, row["id"]),
                    )
        await self.store.db.commit()
        if decayed:
            await self.store.audit("memory.maintenance", f"decayed={decayed} evicted={evicted} skipped_personas={skipped}")
        return {"decayed": decayed, "evicted": evicted, "skipped_personas": skipped}

    # ---------------- 表达习惯注入（把学到的说话方式带进 prompt） ----------------
    async def habit_hints_detail(self, persona_id: str = "", limit: int = 6) -> tuple[str, list[int]]:
        """把当前人格可用的表达拼成注入片段，并返回本次真注入的 id（表达使用记账用）。

        S4 口径：
          · 名额 = output_expr_max_inject（0 = 不注入；上限 10，与 PARAM_GUARDS 同界）；
          · scope 走 _expr_scopes()（E1：**fail-closed**，无匹配 = 空，不再回退「全部 enabled」）；
          · 冷却中 / 24h 内已注入满 2 次 / 已驳回 / 在「不再学」名单 / 已停用 —— 都不选；
          · 「先审后用」开着时只有 user_ok 能进池（默认自动通过 → ai_pending/学来的也能进）。
        """
        cfg = await self.config()
        try:
            cap = int(limit if limit is not None else cfg.get("output_expr_max_inject", 5))
        except (TypeError, ValueError):
            cap = 5
        cap = max(0, min(10, cap))
        if cap <= 0:
            return "", []
        try:
            min_uses = max(0, int(cfg.get("expression_min_uses") or 0))
        except (TypeError, ValueError):
            min_uses = 0
        review_first = _as_bool(cfg.get("expression_review_first", False))
        habits = await self.store.list_habits()
        banned = await self.store.banned_keys()
        now_ts = time.time()
        scopes = self._expr_scopes(persona_id)
        allowed = ("user_ok",) if review_first else ("user_ok", "ai_pending")
        usable: list[dict[str, Any]] = []
        for h in habits:
            if str(h.get("status") or "active") != "active" or not h.get("enabled"):
                continue
            if scopes is not None and str(h.get("scope") or "") not in scopes:
                continue
            if str(h.get("checked_state") or "") not in allowed:
                continue
            if str(h.get("norm_key") or "") and str(h.get("norm_key")) in banned:
                continue
            if float(h.get("cooldown_until") or 0) > now_ts:
                continue
            if int(h.get("injected_24h") or 0) >= EXPR_WINDOW_MAX_INJECT:
                continue
            usable.append(h)
        # 排序：用过的（出站命中）优先，其次最近更新的 —— 原实现按 weight 排，weight 已删（拍-11）
        usable.sort(key=lambda h: (int(h.get("use_count") or 0), float(h.get("updated_at") or 0)), reverse=True)
        # 「表达最少使用次数」：达到 N 次才算"已验证"，优先注入；没达标的只在名额没用满时补位
        verified = [h for h in usable if int(h.get("use_count") or 0) >= min_uses]
        fresh = [h for h in usable if int(h.get("use_count") or 0) < min_uses]
        picked = (verified + fresh)[:cap]
        lines = [f"- {h.get('scene')}→{h.get('way')}" for h in picked if h.get("scene") and h.get("way")]
        if not lines:
            return "", []
        text = "【表达习惯参考】像真人一样说话，可以参考这些习惯（不要逐字复读）：\n" + "\n".join(lines)
        ids: list[int] = []
        for h in picked:
            if not (h.get("scene") and h.get("way")):
                continue
            try:
                ids.append(int(h["id"]))
            except (KeyError, TypeError, ValueError) as exc:  # noqa: BLE001 缺 id 的行不该挡住注入
                logger.debug(f"[StarHeart] 表达使用记账跳过一条（无 id）：{exc}")
                continue
        return text, ids

    async def habit_hints(self, persona_id: str = "", limit: int = 6) -> str:
        """把启用的表达拼成注入片段（表达学习从「只提取不使用」变成真生效）。"""
        text, _ids = await self.habit_hints_detail(persona_id, limit)
        return text

    async def habit_mark_used(self, persona_id: str = "", ids: list[int] | None = None) -> int:
        """表达「用过了」的写侧（E1 一并修）：只盖**本次真注入**那几条的冷却戳，且按人格范围过滤。

        原实现是「给前 6 条 enabled 加 uses+1」——既不看人格（跨人格串号），也不看真注入了谁。
        S4 之后没有 uses 回写列（拍-5：use_count 走查询聚合），这里只维护冷却戳；
        不传 ids 直接返回 0（绝不瞎猜是哪些被注入了）。
        """
        picked: list[int] = []
        for x in ids or []:
            try:
                v = int(x)
            except (TypeError, ValueError):
                continue
            if v > 0:
                picked.append(v)
        if not picked:
            return 0
        scopes = self._expr_scopes(persona_id)
        marks = ",".join("?" for _ in picked)
        where = f" WHERE id IN ({marks})"
        args: list[Any] = list(picked)
        if scopes is not None:
            where += " AND scope IN (" + ",".join("?" for _ in scopes) + ")"
            args += sorted(scopes)
        cur = await self.store.db.execute(
            f"UPDATE hf_expr SET cooldown_until=?{where}", [time.time() + EXPR_COOLDOWN_SECONDS] + args
        )
        await self.store.db.commit()
        return int(cur.rowcount or 0)

    # ---------------- 关系边 ----------------
    async def memory_edges(self, mid: str | int) -> dict[str, Any]:
        return {"items": await self.store.edges_of(mid)}

    async def edge_save(self, items: list[dict[str, Any]]) -> dict[str, Any]:
        n = 0
        for it in items or []:
            if await self.store.update_edge(it.get("id"), it.get("weight"), it.get("status")):
                n += 1
        await self.store.audit("edge.save", f"n={n}")
        return {"saved": n}

    async def config(self) -> dict[str, Any]:
        saved = await self.store.all_config()
        merged = dict(DEFAULT_CONFIG)
        merged.update(saved)
        return merged

    async def save_config(self, patch: dict[str, Any]) -> dict[str, Any]:
        """保存配置：先过参数护栏，再写本插件库（per-session 开关的家），最后**写回原生插件配置**。

        护栏（勘误冻结）：越界或踩到「一次改坏」硬拦的整批拒绝 —— 一个字都不写，
        并把原因抛给调用方（面板直接显示）。校验只覆盖 PARAM_GUARDS 里的键，其余键照旧。

        为什么要写回原生：红队审计实测「面板私库 / 原生页 / hf_config」是两份源，只写一份
        就会出现「原生页改了不生效、面板改了原生页看不见」。插件只是调用原生 —— 不另造权威。
        """
        clean = {k: v for k, v in (patch or {}).items() if k in DEFAULT_CONFIG}
        # 面板/原生页都可能把数字当字符串发过来：按 schema 收口，避免 "90" 落库后
        # 又被 JSON 读出来参与比较（"90" != 90），或空串被 Number() 当成 0。
        _schema = self._load_schema()
        for _k, _v in list(clean.items()):
            _spec = _schema.get(_k) or {}
            _t = str(_spec.get("type") or "").lower()
            if _t in ("int", "integer", "number", "float") and isinstance(_v, str):
                _raw = _v.strip()
                if not _raw:
                    raise ValueError(f"「{_k}」不能为空")
                try:
                    clean[_k] = int(float(_raw)) if _t in ("int", "integer") else float(_raw)
                except ValueError as exc:
                    raise ValueError(f"「{_k}」需要数字，收到 {_v!r}") from exc
            elif _t in ("bool", "boolean") and isinstance(_v, str):
                clean[_k] = _v.strip().lower() in ("1", "true", "yes", "on", "是", "开")
            elif _t in ("list", "array") and isinstance(_v, str):
                clean[_k] = _as_id_list(_v)
        violations = validate_config_patch(clean, await self.config())
        if violations:
            msg = guard_reject_message(violations)
            try:
                await self.store.audit("config.reject",
                                       ",".join(str(v.get("key")) for v in violations)[:160], msg[:200])
            except Exception as exc:                          # noqa: BLE001 留痕失败不改变拒绝结论
                logger.warning(f"[StarHeart] 护栏拒绝留痕失败：{exc}")
            raise ValueError(msg)
        # 改动前自动留一份快照（可一键回滚到这次保存之前的整库配置）；快照失败不挡保存
        if clean:
            try:
                await self.config_snapshot_save(name="保存前自动快照", auto=True,
                                                note=",".join(sorted(clean))[:160])
            except Exception as exc:                          # noqa: BLE001
                logger.warning(f"[StarHeart] 保存前自动快照失败（不影响保存）：{exc}")
        written: dict[str, Any] = {}
        for k, v in clean.items():
            await self.store.set_config(k, v)
            written[k] = v
        await self.store.audit("config.save", ",".join(list((patch or {}).keys())[:8]))
        native: dict[str, Any] = {"ok": False, "error": "未接入原生写回"}
        fn = self.system.get("save_native_config")
        if callable(fn) and written:
            try:
                native = await fn(written)
            except Exception as exc:                          # noqa: BLE001
                native = {"ok": False, "error": str(exc)[:120]}
        out = await self.config()
        if isinstance(out, dict):
            out["_native_write"] = native                     # 面板可见：哪些键真的写进原生配置了
        # 保存后自动跑一次登记自检：新键如果只加在某一处，下一次保存就会补齐
        try:
            await self.config_registry_audit(record=False)
        except Exception as exc:                              # noqa: BLE001
            logger.warning(f"[StarHeart] 保存后配置自检失败（不影响保存）：{exc}")
        return out

    # ---------------- 参数快照 / 一键回滚 ----------------
    async def config_snapshots(self, limit: int = 25) -> dict[str, Any]:
        """快照列表（设置页「参数快照」卡）。只给元信息，配置本体不出库、不发前端。"""
        items = await self.store.list_config_snapshots(limit=max(1, min(int(limit or 25), 100)))
        return {"items": items, "total": await self.store.count_config_snapshots(),
                "keep": CONFIG_SNAPSHOT_KEEP}

    async def config_snapshot_save(self, name: str = "", auto: bool = False, note: str = "",
                                   actor: str = "panel") -> dict[str, Any]:
        """存一份快照：内容是 hf_config 里**真正存着的**键值（回滚就是把它原样写回）。"""
        cfg = await self.store.all_config()
        label = str(name or "").strip()[:60] or time.strftime("快照 %m-%d %H:%M")
        item = await self.store.save_config_snapshot(label, cfg, auto=auto, note=note, actor=actor)
        try:
            pruned = await self.store.prune_config_snapshots()
        except Exception as exc:                              # noqa: BLE001
            pruned = 0
            logger.warning(f"[StarHeart] 快照保留清理跳过：{exc}")
        if not auto:
            await self.store.audit("config.snapshot", str(item.get("id")), label)
        return dict(item, pruned=pruned)

    async def config_rollback(self, sid: int, actor: str = "panel") -> dict[str, Any]:
        """一键回滚：把快照里的键值写回 hf_config，并清掉「快照之后才出现」的配置键。

        回滚前先自动存一份当前值 —— 滚错了还能再滚回来。写回内容以快照为准（哪怕个别值
        超出今天的护栏范围：那是当时真在生效的值，回滚是用户的显式选择，只提示不拦）。
        """
        try:
            target = int(sid)
        except (TypeError, ValueError) as exc:
            raise ValueError("快照 id 不是数字") from exc
        snap = await self.store.get_config_snapshot(target)
        if not snap:
            raise ValueError(f"找不到这份快照（id={target}）")
        try:
            saved = json.loads(snap.get("config_json") or "{}")
        except (TypeError, ValueError) as exc:
            raise ValueError(f"快照内容读不出来：{exc}") from exc
        if not isinstance(saved, dict) or not saved:
            raise ValueError("这份快照里没有配置内容，回滚不了")
        cur = await self.store.all_config()
        await self.config_snapshot_save(name=f"回滚 #{target} 前的自动快照", auto=True, note="回滚前")
        restored = {k: v for k, v in saved.items() if k in DEFAULT_CONFIG}
        skipped = sorted(k for k in saved if k not in DEFAULT_CONFIG)
        for k, v in restored.items():
            await self.store.set_config(k, v)
        # 快照里没有、现在却有的配置键 = 快照之后新加的：一起清掉，值才算真的还原
        extra = sorted(k for k in cur if k not in saved and k in DEFAULT_CONFIG)
        for k in extra:
            await self.store.delete_config_key(k)
        native: dict[str, Any] = {"ok": False, "error": "未接入原生写回"}
        fn = self.system.get("save_native_config")
        if callable(fn) and restored:
            try:
                native = await fn(restored)
            except Exception as exc:                          # noqa: BLE001
                native = {"ok": False, "error": str(exc)[:120]}
        after = await self.config()
        bad = validate_config_patch(restored, after)
        if bad:
            logger.warning(f"[StarHeart] 回滚后的配置仍有 {len(bad)} 个键超出当前护栏（按快照原样写回）")
        await self.store.audit("config.rollback", f"snapshot#{target}",
                               f"写入 {len(restored)} / 清除 {len(extra)}")
        return {"ok": True, "id": target, "name": snap.get("name") or "", "ts": snap.get("ts") or 0,
                "restored": len(restored), "removed": extra, "skipped": skipped,
                "violations": bad, "native": native, "values": after}

    # ---------------- 总览 ----------------
    async def overview(self, persona_id: str = "") -> dict[str, Any]:
        stats = await self.store.stats(persona_id=persona_id)
        cfg = await self.config()
        batches = await self.store.list_batches(3)
        detect = await self.importer.detect_async()
        stats["config"] = {
            "master_switch": cfg["master_switch"],
            "gate_enabled": cfg["gate_enabled"],
            "gate_shadow": cfg["gate_shadow"],
            "output_enabled": cfg["output_enabled"],
            "proactive_enabled": cfg["proactive_enabled"],
        }
        stats["livingmemory"] = {
            "installed": detect["plugin_dir_exists"],
            "db_exists": detect["db_exists"],
            "documents": detect["documents"],
            "atoms": detect["atoms"],
            "imported": detect["already_imported"],
        }
        stats["recent_batches"] = batches
        # 捕获队列健康度：待重试 / 停摆（parked）行数，总览页直接显示，不用翻 audit 表。
        stats["capture"] = await self.store.capture_stats()
        return stats

    async def capture_queue(self, limit: int = 20) -> dict[str, Any]:
        """捕获队列只读视图（面板用）：只给状态列，不返回 payload（对话正文）。"""
        return {
            "stats": await self.store.capture_stats(),
            "rows": await self.store.capture_pending_list(limit),
        }

    async def capture_retry(self, limit: int = 20) -> dict[str, Any]:
        """手动给停摆（parked）的批一次机会，重新走退避链。"""
        n = await self.store.capture_parked_for_retry(limit=limit)
        if n:
            await self.store.audit("memory.capture.revive", f"{n} 批", "面板手动重试")
        return {"requeued": n, "stats": await self.store.capture_stats()}

    # ---------------- 记忆 ----------------
    async def memories(self, **kw: Any) -> dict[str, Any]:
        return await self.store.list_memories(
            q=kw.get("q", "") or "",
            tag=kw.get("tag", "") or "",
            source=kw.get("source", "") or "",
            page=int(kw.get("page", 1) or 1),
            size=min(int(kw.get("size", 50) or 50), 500),
            persona_id=kw.get("persona_id", "") or "",
        )

    async def memory(self, mid: str) -> dict[str, Any] | None:
        return await self.store.get_memory(mid)

    async def memory_update(self, mid: str, fields: dict[str, Any]) -> dict[str, Any]:
        ok = await self.store.update_memory(mid, fields or {})
        await self.store.audit("memory.update", str(mid), ",".join(list((fields or {}).keys())[:6]))
        return {"updated": ok}

    async def panel_stats(self, persona_id: str = "") -> dict[str, Any]:
        return await self.store.panel_stats(persona_id=persona_id)

    async def purge(self, scope: str, session_id: str = "") -> dict[str, Any]:
        """危险区操作。scope: gate | learning | expressions | session。

        两个学习类动作都**先备份再动**（拍-14：备份落数据目录 backups/，保留最近 5 份）：
        learning = 原语义（记忆+实体+关系+表达，好感保留）；expressions = 只清表达（软删 + 进名单）。
        返回值带 backup 文件名；"" = 备份没建成 —— 面板必须如实说出来，不假装有备份。
        """
        backup = ""
        if scope == "gate":
            removed: Any = await self.store.purge_gate_log()
        elif scope == "learning":
            out = await self.store.purge_learning()
            backup = str(out.pop("backup", "") or "")
            removed = out
        elif scope == "expressions":
            out = await self.store.purge_expressions()
            backup = str(out.get("backup") or "")
            removed = {"soft_deleted": int(out.get("soft_deleted") or 0),
                       "banned": int(out.get("banned") or 0)}
        elif scope == "session":
            sid = (session_id or "").strip()
            if not sid:
                raise ValueError("missing session_id")
            removed = await self.store.purge_session_memories(sid)
        else:
            raise ValueError(f"unknown scope: {scope}")
        await self.store.audit(f"purge.{scope}", str(removed), f"backup={backup or '-'}")
        return {"scope": scope, "removed": removed, "backup": backup}

    async def memory_delete(self, mid: str) -> dict[str, Any]:
        """单条删除 = 软删（可恢复）。面板文案必须跟着改：不是「永久删除」。"""
        ok = await self.store.delete_memory(mid)
        await self.store.audit("memory.delete", str(mid), "soft")
        return {"deleted": bool(ok), "soft": True}

    async def memory_restore(self, mid: str) -> dict[str, Any]:
        """撤销删除（回收站恢复）。"""
        ok = await self.store.restore_memory(mid)
        await self.store.audit("memory.restore", str(mid))
        return {"restored": bool(ok)}

    async def memory_mute(self, mid: str, muted: bool = True, source_mode: str = "") -> dict[str, Any]:
        """「别再提」：冷处理一条记忆（不进召回 / 不进主动池，但保留可恢复）。

        这是用户能给出的唯一负反馈：旧记忆反复被提起时，除了删掉没有别的动作，
        而删掉又舍不得。muted 只影响「会不会被自动提起」，不影响面板查看与手动恢复。
        """
        ok = await self.store.set_muted(mid, bool(muted), source_mode=str(source_mode or ""))
        await self.store.audit(
            "memory.mute" if muted else "memory.unmute", str(mid),
            "source=%s" % source_mode if source_mode else "")
        return {"ok": bool(ok), "muted": bool(muted)}

    async def graph(self, limit: int = 4000, persona_id: str = "") -> dict[str, Any]:
        return await self.store.graph(limit, persona_id=persona_id)

    async def graph_stats(self, persona_id: str = "") -> dict[str, Any]:
        """图谱关系链体检数据（孤立点 / 平均度数 / 连通分量 / 枢纽）。"""
        return await self.store.graph_stats(persona_id)

    async def graph_build(self, limit: int = 3000, persona_id: str = "") -> dict[str, Any]:
        """重建记忆之间的关系链（面板按钮与 hf 命令共用）。

        老库、或只导入过一半的库，记忆之间是没有边的 —— 图谱就只剩孤点。
        重建是幂等的（add_edge 是 upsert），所以可以放心反复点。
        """
        stat = await self.store.rebuild_links(limit, persona_id=persona_id)
        await self.store.audit(
            "graph.rebuild",
            f"persona={persona_id or 'all'} memories={stat.get('memories')} "
            f"entity={stat.get('entity')} temporal={stat.get('temporal')} semantic={stat.get('semantic')}",
        )
        return {"ok": True, **stat, "stats": await self.store.graph_stats(persona_id)}

    # ---------------- 好感 ----------------
    async def favours(self) -> dict[str, Any]:
        items = await self.store.list_favours()
        for it in items:
            it["zone_text"] = ZONE_TEXT.get(it.get("zone", "NEUTRAL"), "")
        return {"items": items}

    async def favour_adjust(self, user_id: str, delta: float, reason: str = "", scope: str = "both", name: str = "") -> dict[str, Any]:
        if not user_id:
            raise ValueError("user_id required")
        cfg = await self.config()
        daily_only = scope == "daily"
        lifetime_only = scope == "lifetime"
        cur = {f["user_id"]: f for f in await self.store.list_favours()}.get(user_id, {})
        ev = list(cur.get("evidence") or [])
        day = self._day_str(cfg)     # 自然日（AstrBot 时区）0:00–23:59，与主动额度同一口径
        # 「好感日上限」：当天正增量累计到 cap 就封顶（0 = 不限）。日记账写进 evidence（面板"依据"列可见）。
        try:
            cap = float(cfg.get("favour_daily_cap") or 0)
        except Exception:
            cap = 0.0
        if cap > 0 and delta > 0:
            used = 0.0
            for e in ev:
                m = re.match(r"^(\d{4}-\d{2}-\d{2})\s*([+-][\d.]+)$", str(e))
                if m and m.group(1) == day and float(m.group(2)) > 0:
                    used += float(m.group(2))
            delta = min(delta, max(0.0, cap - used))
        if delta:
            ev.append(f"{day} {delta:+g}")
        daily = float(cur.get("daily", 0)) + (0 if lifetime_only else delta)
        lifetime = float(cur.get("lifetime", 0)) + (0 if daily_only else delta)
        patch = {
            "name": name or cur.get("name", user_id),
            "session_id": cur.get("session_id", ""),
            "daily": daily,
            "lifetime": lifetime,
            "zone": self.store.zone_of(daily),
            "evidence": ev[-40:],
        }
        out = await self.store.upsert_favour(user_id, patch)
        await self.store.audit("favour.adjust", user_id, f"delta={delta} reason={reason} scope={scope}")
        return out

    async def favour_decay_tick(self) -> int:
        """「好感每日半衰期」：daily 按 half_life 天半衰，避免"很久没聊还满好感"。

        half_life <= 0 = 关闭（DEFAULT_CONFIG 里默认 0；库里配了值就以库里的为准）。
        lifetime 不衰减（那是历史累计，只增不减）。距上次更新不到半天的不动，避免频繁抖动。
        """
        cfg = await self.config()
        try:
            hl = float(cfg.get("favour_daily_half_life_days") or 0)
        except Exception:
            hl = 0.0
        if hl <= 0:
            return 0
        ts = time.time()
        n = 0
        for r in await self.store.list_favours():
            daily = float(r.get("daily") or 0)
            last = float(r.get("updated_at") or 0)
            if daily == 0 or last <= 0:
                continue
            days = max(0.0, (ts - last) / 86400.0)
            if days < 0.5:
                continue
            new = daily * (0.5 ** (days / hl))
            if abs(new - daily) < 0.01:
                continue
            await self.store.upsert_favour(
                r["user_id"], {**r, "daily": new, "zone": self.store.zone_of(new), "evidence": r.get("evidence") or []}
            )
            n += 1
        if n:
            await self.store.audit("favour.decay", str(n), f"half_life={hl}d")
        return n

    async def withdraw_reply_trace(self, session_id: str, minutes: int = 10) -> int:
        """「撤回连回复一起撤」：把该会话最近这段落库的 bot 语料撤掉（本地一定做得到的那部分）。

        平台侧删消息需要"bot 那条回复的消息 id"，AstrBot 没有把这个 id 交给插件，所以不在这项能力内；
        调用方会把这一限制写进日志，不做静默假装。
        """
        if not session_id:
            return 0
        since = time.time() - max(1, int(minutes)) * 60
        n = await self.store.drop_bot_corpus_since(session_id, since)
        if n:
            await self.store.audit("recall.withdraw_reply", session_id, f"清掉 bot 语料 {n} 条")
        return n

    # ---------------- 表达习惯（S4：hf_expr） ----------------
    async def _evidence_health(self, rows: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """出处回查健康度（E3 / 勘误 D5「证据可回查 ≥95%」）：证据快照逐条对语料库。

        旧库的条目只有 60 字快照、没有 corpus_id → 不计入分母（如实标 n=0，不编数字）。
        """
        items = rows if rows is not None else await self.store.list_habits()
        pairs: list[tuple[int, str]] = []
        for it in items:
            snap = it.get("evidence_snap")
            if not isinstance(snap, list):
                try:
                    snap = json.loads(snap or "[]")
                except (TypeError, ValueError):
                    snap = []
            for ev in snap or []:
                if not isinstance(ev, dict):
                    continue
                try:
                    cid = int(ev.get("corpus_id") or 0)
                except (TypeError, ValueError):
                    cid = 0
                if cid:
                    pairs.append((cid, str(ev.get("text") or "")))
        if not pairs:
            return {"value": None, "n": 0, "state": "INSUFFICIENT_DATA",
                    "reason": "还没有带出处的表达（旧条目只有 60 字快照，补不齐）"}
        live = await self.store.corpus_rows_by_ids([c for c, _t in pairs])
        recoverable = 0
        for cid, text in pairs:
            row = live.get(cid)
            if row and (not text or str(row.get("text") or "").strip() == text.strip()):
                recoverable += 1
        return {"value": round(recoverable / len(pairs), 4), "n": len(pairs),
                "recoverable": recoverable, "state": "ok" if len(pairs) >= 5 else "INSUFFICIENT_DATA"}

    async def habits(self, persona_id: str = "") -> dict[str, Any]:
        """表达库数据源：条目 + 当前可用性 + 出处回查 + 「不再学」名单 + 审核计数。

        E1（人格隔离 fail-closed + 手加纳入）/ E3（出处可回查）/ S4-2（审核与名单）/
        S4-3（冷却、24h≤2、use_count 出站命中）的读数全在这一处，面板与注入侧不会两套口径。
        """
        cfg = await self.config()
        pid = str(persona_id or "")
        review_first = _as_bool(cfg.get("expression_review_first", False))
        try:
            min_uses = max(0, int(cfg.get("expression_min_uses") or 0))
        except (TypeError, ValueError):
            min_uses = 0
        items = await self.store.list_habits()
        bans = await self.store.list_bans()
        banned = {str(b.get("norm_key") or "") for b in bans}
        # 出处回查（E3）：证据快照 → 语料行 + 消息 id 侧车（逐字相等才算可回查）
        cids: list[int] = []
        for h in items:
            for ev in h.get("evidence_snap") or []:
                try:
                    v = int(ev.get("corpus_id") or 0)
                except (TypeError, ValueError):
                    v = 0
                if v:
                    cids.append(v)
        live = await self.store.corpus_rows_by_ids(cids)
        refs = await self.store.corpus_ref_map(cids)
        now_ts = time.time()
        counts = {"total": len(items), "enabled": 0, "pending": 0, "ok": 0, "rejected": 0,
                  "banned": len(bans), "available": 0}
        for h in items:
            state = str(h.get("checked_state") or "")
            if h.get("enabled"):
                counts["enabled"] += 1
            if state in counts:
                counts[state] += 1
            label, can = self._expr_availability(
                h, persona_id=pid, review_first=review_first, min_uses=min_uses,
                banned=banned, now_ts=now_ts,
            )
            h["available_now"] = label
            h["current_persona_ok"] = bool(can)
            if can:
                counts["available"] += 1
            h["cooldown_active"] = float(h.get("cooldown_until") or 0) > now_ts
            h["used_24h"] = int(h.get("used_24h") or 0)
            h["injected_24h"] = int(h.get("injected_24h") or 0)
            ev_out: list[dict[str, Any]] = []
            for ev in h.get("evidence_snap") or []:
                try:
                    cid = int(ev.get("corpus_id") or 0)
                except (TypeError, ValueError):
                    cid = 0
                row = live.get(cid) if cid else None
                text = str(ev.get("text") or "")
                ev_out.append({
                    **ev, "corpus_id": cid,
                    "message_id": str((refs.get(cid) or {}).get("message_id") or ev.get("message_id") or ""),
                    "recoverable": bool(row) and (not text or str(row.get("text") or "").strip() == text.strip()),
                })
            h["evidence"] = ev_out
        return {"items": items, "banned": bans, "review_first": review_first,
                "counts": counts, "persona_id": pid}

    async def habit_save(self, habit: dict[str, Any]) -> dict[str, Any]:
        """新增/就地编辑一条表达（面板手加与「改说法」）。

        手加归属按拍-4 归一：'手加' → '手加·{pid}'（pid 拿不到就保持 '手加'）。
        手加是用户自己写的 → 建行即 user_ok（不受「先审后用」影响，否则用户手加的还要自己审自己）。
        """
        h = dict(habit or {})
        pid = await self._active_pid()
        scope = str(h.get("scope") or "")
        if not scope or scope.startswith("手加"):
            h["scope"] = f"手加·{pid}" if pid else "手加"
        else:
            h["scope"] = scope
        h["norm_key"] = str(h.get("norm_key") or "").strip() or expr_norm_key(h.get("scene", ""), h.get("way", ""))
        if not h.get("id"):
            h.setdefault("checked_state", "user_ok")
        hid = await self.store.upsert_expr(h)
        await self.store.audit("habit.save", str(hid), str(h.get("scene") or "")[:40])
        return {"id": hid, "scope": h["scope"], "norm_key": h["norm_key"]}

    async def habit_delete(self, hid: int) -> dict[str, Any]:
        """「删除」= 软删 + 写「不再学」名单（S4-2 / 拍-14）—— 修「删了还会再学」。

        真删归数据治理（设置 · 数据治理 →「只清表达」）。行还在，移出名单即恢复。
        """
        out = await self.store.archive_expr(int(hid), actor="panel")
        await self.store.audit("habit.delete", str(hid), f"banned={out.get('banned')}")
        return {"deleted": True, "archived": int(out.get("archived") or 0),
                "banned": int(out.get("banned") or 0)}

    # ---------------- 审核 / 名单 / 诊断（S4-2 / S4-4） ----------------
    async def expr_review(self, ids: list[int], action: str, reason: str = "") -> dict[str, Any]:
        """审核状态机（POST expr/review）：approve → user_ok；reject → user_reject + 进名单。"""
        act = "approve" if str(action) == "approve" else "reject"
        out = await self.store.set_expr_review(ids, act, reason=str(reason or ""), actor="panel")
        await self.store.audit(f"expr.review.{act}", str(out.get("ids") or []), f"reason={reason or '-'}")
        return {"ok": True, "action": act, **out}

    async def expr_review_queue(self, limit: int = 200) -> dict[str, Any]:
        """GET expr/review：待审行 + 四种计数（面板「待审 N」与菜单徽标的数据源）。"""
        return await self.store.expr_review_queue(limit)

    async def expr_bans(self, limit: int = 200) -> dict[str, Any]:
        items = await self.store.list_bans(limit)
        return {"items": items, "total": len(items)}

    async def expr_ban_remove(self, norm_key: str) -> dict[str, Any]:
        """移出「不再学」名单（v8「可移出」）：同键可再学，被软删的同键行一并恢复。"""
        out = await self.store.remove_ban(norm_key)
        await self.store.audit("expr.ban.remove", str(norm_key), f"removed={out.get('removed')}")
        return {"ok": bool(out.get("removed") or out.get("restored")), **out}

    async def expr_runs(self, limit: int = 20) -> dict[str, Any]:
        """GET expr/runs：运行行 + 语料存量 + 出处健康度 + 原因枚举（面板③学习诊断）。"""
        return {
            "items": await self.store.expr_runs(limit),
            "corpus": {**(await self.store.corpus_stats()), "unused": await self.store.corpus_unused_count()},
            "health": await self._evidence_health(),
            "skipped_reason_enum": _skip_reason_enum(),
        }

    # ---------------- S5 黑话（hf_jargon）：理解注入 / 命中记账 / 人工闸门 / 试解读 ----------------
    @staticmethod
    def _jargon_mechanical_matches(text: str, pool: list[dict[str, Any]],
                                   limit: int = JARGON_MATCH_LIMIT) -> list[dict[str, Any]]:
        """机械匹配器 —— 命中日志与理解注入共用（round1-C1 §4「写一次两用」）。

        朴素子串（小写化）、长词优先；命中的片段从比对串里抹掉，
        防「破防了 / 破防」这类同区域重复记账。上限 = JARGON_MATCH_LIMIT（上游 10）。
        """
        low = str(text or "").lower()
        if not low:
            return []
        out: list[dict[str, Any]] = []
        for row in sorted(pool, key=lambda r: -len(str(r.get("term") or ""))):
            if len(out) >= max(1, int(limit)):
                break
            t = str(row.get("term") or "").strip().lower()
            if not t or t not in low:
                continue
            out.append(row)
            low = low.replace(t, " ")
        return out

    async def jargon_context_block(self, text: str, scopes: list[str],
                                   session_id: str = "") -> tuple[str, list[dict[str, Any]]]:
        """入站消息的黑话处理：机械匹配 → 命中记账（hf_jargon_hit）→ 拼「理解参考」注入片段。

        硬约束（S5 全条）：这段只进 prompt 供听懂，**不进回复库、不产生任何回复**；
        命中日志与理解注入共用同一匹配器（写一次两用）。无命中原样返回空串。
        """
        try:
            pool = await self.store.jargon_match_pool(list(scopes or []))
        except Exception as exc:                            # noqa: BLE001 读不到候选池=本轮不注入
            logger.warning(f"[StarHeart] 黑话候选池读取失败（本轮不注入）：{exc}")
            return "", []
        matched = self._jargon_mechanical_matches(text, pool)
        if not matched:
            return "", []
        rows: list[dict[str, Any]] = []
        lines: list[str] = []
        for m in matched:
            meaning = str(m.get("meaning") or "").strip()
            rows.append({"term_id": int(m["id"]), "session_id": str(session_id or ""),
                         "msg": str(text or ""), "injected": 1 if meaning else 0})
            if meaning:
                lines.append(f"- {m['term']}：{meaning}")
        await self.store.jargon_log_hits(rows)
        if not lines:
            return "", matched                           # 全是没释义的待推断词：只记账，不占 prompt
        block = ("【黑话理解参考】群友用的这些词/梗，只用于听懂他们在说什么，"
                 "**不要照搬进你自己的回复**：\n" + "\n".join(lines))
        return block, matched

    async def jargon_list(self, status: str = "", q: str = "", scope: str = "") -> dict[str, Any]:
        """GET jargon：黑话清单 + 计数 + 命中聚合 + 「未进入回复库」标记 + 阈值/配额读数（块②）。"""
        st = str(status or "").strip()
        if st and st not in JARGON_STATUSES:
            st = ""
        items = await self.store.jargon_list(status=st, q=q, scope=scope)
        stats_map = await self.store.jargon_hit_stats()
        exprs = await self.store.jargon_expr_map()
        counts = {"total": len(items), "pending": 0, "confirmed": 0, "denied": 0, "enabled": 0, "hits": 0}
        for it in items:
            stt = str(it.get("status") or "")
            if stt in counts:
                counts[stt] += 1
            if it.get("enabled"):
                counts["enabled"] += 1
            agg = stats_map.get(int(it.get("id") or 0)) or {}
            it["sessions"] = int(agg.get("sessions") or 0)
            it["hits_logged"] = int(agg.get("hits") or 0)
            it["in_reply_lib"] = bool(exprs.get(str(it.get("term") or "")))
            counts["hits"] += int(it.get("hit_count") or 0)
        cfg = await self.config()
        return {
            "items": items,
            "counts": counts,
            "status_enum": list(JARGON_STATUSES),
            "config": {
                "jargon_enabled": _as_bool(cfg.get("jargon_enabled", True)),
                "jargon_name_filter": _as_bool(cfg.get("jargon_name_filter", True)),
                "candidate_min_sessions": int(cfg.get("jargon_candidate_min_sessions") or 2),
                "candidate_min_hits": int(cfg.get("jargon_candidate_min_hits") or 4),
                "infer_daily_batches": int(cfg.get("jargon_infer_daily_batches") or 0),
                "try_daily_quota": int(cfg.get("jargon_try_daily_quota") or 0),
                "try_used_today": await self._jargon_quota_peek("jargon_try_day", "jargon_try_day_count"),
            },
        }

    async def jargon_decide(self, jid: int, action: str) -> dict[str, Any]:
        """人工闸门（洞③）：confirm → confirmed（启用）；deny → denied（归档：不再推断、不再注入）。"""
        act = str(action or "").strip()
        if act not in ("confirm", "deny"):
            raise ValueError(f"不认识的动作：{action!r}（只接受 confirm / deny）")
        target = int(jid)
        row = await self.store.jargon_get(target)
        if not row:
            raise ValueError(f"没有这条黑话（id={target}）")
        if act == "confirm":
            await self.store.jargon_set_status(target, "confirmed", enabled=1)
        else:
            await self.store.jargon_set_status(target, "denied", enabled=0)
        await self.store.audit(f"jargon.{act}", f"#{target} {str(row.get('term') or '')[:40]}")
        return {"ok": True, "id": target, "status": "confirmed" if act == "confirm" else "denied"}

    async def jargon_save(self, jid: int, term: str = "", meaning: str = "",
                          enabled: Any = None) -> dict[str, Any]:
        """改词条 / 改释义 / 停用-启用（人工）。长度超限直接拒绝（不静默截断）。"""
        target = int(jid)
        t = str(term or "").strip()
        if not t:
            raise ValueError("词条不能为空")
        if len(t) > JARGON_TERM_MAX_CHARS:
            raise ValueError(f"词条最长 {JARGON_TERM_MAX_CHARS} 字")
        if len(str(meaning or "")) > JARGON_MEANING_MAX_CHARS:
            raise ValueError(f"释义最长 {JARGON_MEANING_MAX_CHARS} 字")
        if not await self.store.jargon_get(target):
            raise ValueError(f"没有这条黑话（id={target}）")
        n = await self.store.jargon_update(
            target, term=t, meaning=str(meaning or ""),
            enabled=(None if enabled is None else (1 if _as_bool(enabled) else 0)),
        )
        await self.store.audit("jargon.save", f"#{target}", t[:40])
        return {"ok": bool(n), "id": target, "changed": int(n)}

    async def jargon_delete(self, jid: int) -> dict[str, Any]:
        """真删（行 + 命中日志）。注意：「否定」≠ 删除 —— 否定是归档（denied），不动数据。"""
        target = int(jid)
        out = await self.store.jargon_delete(target)
        await self.store.audit("jargon.delete", f"#{target}", f"hits={out.get('hits_removed')}")
        return {"ok": bool(out.get("deleted")), **out}

    async def jargon_hits(self, term_id: int, limit: int = 20) -> dict[str, Any]:
        """GET jargon/hits：命中日志（验证物①）—— 哪一轮真的读到了这个词、原话是什么。"""
        target = int(term_id)
        items = await self.store.jargon_hits_list(target, limit)
        row = await self.store.jargon_get(target)
        return {"items": items, "term": str((row or {}).get("term") or ""), "total": len(items)}

    async def jargon_check(self) -> dict[str, Any]:
        """GET jargon/check：「未进入回复库 ✓」检查行（验证物②）——黑话词是否漏进表达库。"""
        exprs = await self.store.jargon_expr_map()
        violations = [v for group in exprs.values() for v in group]
        terms = await self.store.jargon_list()
        return {"violations": violations, "checked": len(terms), "ok": not violations}

    # ---- 试解读 / 推断（拍-19：手动触发 + 每日配额；hard 约束 = 不进回复库） ----
    @staticmethod
    def _jargon_day() -> str:
        return time.strftime("%Y-%m-%d", time.localtime())

    async def _jargon_quota_peek(self, day_key: str, cnt_key: str) -> int:
        cfg = await self.config()
        if str(cfg.get(day_key) or "") != self._jargon_day():
            return 0                                        # 跨天自动归零（读侧）
        try:
            return max(0, int(cfg.get(cnt_key) or 0))
        except (TypeError, ValueError):
            return 0

    async def _jargon_quota_take(self, day_key: str, cnt_key: str) -> int:
        """记一次今日用量（写侧同样跨天归零）。返回记完之后今日已用次数。"""
        used = (await self._jargon_quota_peek(day_key, cnt_key)) + 1
        await self.store.set_config(day_key, self._jargon_day())
        await self.store.set_config(cnt_key, used)
        return used

    async def jargon_try(self, text: str) -> dict[str, Any]:
        """POST jargon/try 试解读（验证物③，拍-19）：手动触发；走「表达提取」离线槽位。

        约束：单次 ≤1 条短请求、输出 ≤200 tokens（提示词写死）；每日配额默认 20（可调）。
        硬约束：只读理解 —— 不调用任何发送/回复路径，reply_side 恒为 "blocked"；
        不写命中日志（试解读不是入站消息，不污染「被观察到的命中次数」口径）。
        """
        raw = str(text or "").strip()
        if not raw:
            raise ValueError("先打一句话再试解读")
        cfg = await self.config()
        quota = max(0, int(cfg.get("jargon_try_daily_quota") or 0))
        used = await self._jargon_quota_peek("jargon_try_day", "jargon_try_day_count")
        if quota and used >= quota:
            return {"ok": False, "error": f"今日试解读配额已用完（{used}/{quota}；可在参数中心调大）",
                    "reading": "", "matched": [], "reply_side": "blocked",
                    "quota": {"limit": quota, "used": used}}
        pool = [r for r in await self.store.jargon_list()
                if int(r.get("enabled") or 0) and str(r.get("status")) in ("pending", "confirmed")]
        matched = self._jargon_mechanical_matches(raw, pool)
        gen = self.system.get("llm_generate")
        if callable(gen):
            used = await self._jargon_quota_take("jargon_try_day", "jargon_try_day_count")
            matched_txt = "\n".join(f"- {m['term']}：{str(m.get('meaning') or '').strip() or '（暂无释义）'}"
                                    for m in matched) or "（这句里没有已学到的词条）"
            prompt = JARGON_TRY_PROMPT.format(text=raw, matched=matched_txt, cap=JARGON_TRY_MAX_TOKENS)
            try:
                reading = str(await gen(prompt, "expression_extract") or "").strip()[:600]
            except Exception as exc:                        # noqa: BLE001 模型失败如实回报，不假装有解读
                logger.warning(f"[StarHeart] 试解读模型调用失败：{exc}")
                reading = f"（模型调用失败：{str(exc)[:120]}）"
        else:
            reading = "（模型通道不可用：先去「设置 · 模型分配」给「表达提取」选一个模型）"
        await self.store.audit("jargon.try", raw[:40], f"matched={len(matched)} used={used}/{quota or '∞'}")
        return {
            "ok": True,
            "reading": reading,
            "matched": [{"id": int(m["id"]), "term": str(m["term"]),
                         "meaning": str(m.get("meaning") or ""), "scope": str(m.get("scope") or "")}
                        for m in matched],
            "reply_side": "blocked",
            "quota": {"limit": quota, "used": used},
            "note": "试解读只做理解：结果不进回复库、不产生任何回复",
        }

    async def jargon_infer_batch(self) -> dict[str, Any]:
        """机械候选 → 一批 LLM 含义推断（拍-16：每日 ≤jargon_infer_daily_batches 批，默认 1）。

        · 候选门槛 = 跨 ≥min_sessions 会话、出现 ≥min_hits 次（参数中心可调）；
        · 人名过滤（洞②）在候选侧生效（接 hf_person_names）；
        · 只产 pending：确认/否定仍是全人工（洞③）；denied 的词不再参与推断（不再学）。
        """
        cfg = await self.config()
        if not _as_bool(cfg.get("jargon_enabled", True)):
            return {"ok": False, "skipped_reason": "disabled", "created": 0}
        try:
            daily = max(0, int(cfg.get("jargon_infer_daily_batches") or 0))
        except (TypeError, ValueError):
            daily = 0
        if daily <= 0:
            return {"ok": False, "skipped_reason": "disabled", "created": 0}
        used = await self._jargon_quota_peek("jargon_infer_day", "jargon_infer_day_count")
        if used >= daily:
            return {"ok": False, "skipped_reason": "daily_cap", "created": 0, "used": used, "limit": daily}
        gen = self.system.get("llm_generate")
        if not callable(gen):
            return {"ok": False, "skipped_reason": "model_unavailable", "created": 0}
        try:
            min_sessions = max(1, int(cfg.get("jargon_candidate_min_sessions") or 2))
            min_hits = max(1, int(cfg.get("jargon_candidate_min_hits") or 4))
        except (TypeError, ValueError):
            min_sessions, min_hits = 2, 4
        rows = await self.store.list_corpus(limit=JARGON_CORPUS_SCAN, is_bot=0)
        exclude = await self.store.jargon_existing_terms()
        names: set[str] = set()
        if _as_bool(cfg.get("jargon_name_filter", True)):
            names = set(await self.store.person_names_all())
        cands = _mine_jargon_candidates(rows, min_sessions=min_sessions, min_hits=min_hits,
                                        exclude_terms=exclude, name_terms=names)
        if not cands:
            return {"ok": True, "skipped_reason": "no_candidates", "created": 0, "scanned": len(rows)}
        prompt = JARGON_INFER_PROMPT_HEAD + "\n".join(
            f"{c['term']}（出现 {c['hits']} 次 / {c['sessions']} 个会话）" for c in cands
        )
        try:
            out = await gen(prompt, "expression_extract")
        except Exception as exc:                            # noqa: BLE001 推断失败不下配额（可重试）
            logger.warning(f"[StarHeart] 黑话含义推断失败：{exc}")
            return {"ok": False, "skipped_reason": "model_error", "created": 0, "error": str(exc)[:160]}
        used = await self._jargon_quota_take("jargon_infer_day", "jargon_infer_day_count")
        by_term = {str(c["term"]).lower(): c for c in cands}
        created = 0
        got: list[dict[str, Any]] = []
        for line in str(out or "").splitlines():
            raw = line.strip().lstrip("-*0123456789. ")
            if "|" not in raw:
                continue
            term, _, meaning = raw.partition("|")
            term, meaning = term.strip(), meaning.strip()
            cand = by_term.get(term.lower())
            if not cand or not meaning or meaning.lower().strip("。 .") in _ANN_NO:
                continue
            evidence = [{"text": str(s.get("text") or "")[:200], "ts": float(s.get("ts") or 0),
                         "session": str(s.get("session_id") or ""),
                         "who": str(s.get("user_name") or s.get("user_id") or "")}
                        for s in (cand.get("samples") or [])]
            jid, is_new = await self.store.jargon_upsert({
                "term": term, "meaning": meaning[:JARGON_MEANING_MAX_CHARS], "status": "pending",
                "scope": str(cand.get("scope") or "全局"), "evidence_json": evidence,
            })
            if is_new:
                created += 1
                got.append({"id": jid, "term": term, "meaning": meaning[:80]})
        await self.store.audit("jargon.infer", f"candidates={len(cands)}",
                               f"created={created} batch={used}/{daily}")
        return {"ok": True, "scanned": len(rows), "candidates": len(cands), "created": created,
                "used": used, "limit": daily, "items": got}

    # ---------------- 门控 / token ----------------
    async def gate_log(self, limit: int = 100, offset: int = 0) -> dict[str, Any]:
        """门控页数据源：老的逐条流水 + 决策流水（带人工改判列，供「逐条改判」用）。

        决策流水读取失败只记 WARNING：它是新增的账，不该拖垮门控页原有的那半张表。
        """
        out: dict[str, Any] = {
            "items": await self.store.list_gate(max(1, min(limit, 200)), max(0, offset)),
            "total": await self.store.count_gate(),
        }
        try:
            out["decisions"] = {
                "items": await self.store.list_decisions(limit=max(5, min(int(limit or 30), 60)), offset=0),
                "total": await self.store.count_decisions(),
            }
        except Exception as exc:                              # noqa: BLE001
            logger.warning(f"[StarHeart] 决策流水读取失败（门控页只少这一块）：{exc}")
            out["decisions"] = {"items": [], "total": 0, "error": str(exc)[:160]}
        return out

    async def gate_verdict(self, did: int, verdict: str, actor: str = "panel") -> dict[str, Any]:
        """门控行内改判：把人工判定写回决策流水（hf_decision_log.user_verdict）并留审计。

        只记账：不改判定结果、不写任何训练样本（样本表还没建）。重复提交同一判定是幂等的。
        """
        v = str(verdict or "").strip()
        if v not in GATE_VERDICTS:
            raise ValueError(f"不认识的判定值：{verdict!r}（只接受 {'/'.join(GATE_VERDICTS)}）")
        try:
            target = int(did)
        except (TypeError, ValueError) as exc:
            raise ValueError("决策记录 id 不是数字") from exc
        if target <= 0:
            raise ValueError("缺少决策记录 id")
        ok = await self.store.set_decision_verdict(target, "" if v == "clear" else v, actor)
        if not ok:
            raise ValueError(f"没有这条决策记录（id={target}）")
        row = await self.store.get_decision(target)
        await self.store.audit("gate.verdict", f"decision#{target}", GATE_VERDICTS[v])
        return {"id": target, "verdict": v, "label": GATE_VERDICTS[v],
                "user_verdict": (row or {}).get("user_verdict", ""),
                "verdict_at": (row or {}).get("verdict_at", 0) or 0}

    async def tokens(self, days: int = 7) -> dict[str, Any]:
        return await self.store.tokens_summary(days)

    # ---------------- 人格（走注入的原生 provider） ----------------
    async def personas(self) -> dict[str, Any]:
        if not self.persona.get("list"):
            return {"available": False, "items": [], "active": ""}
        items = await self.persona["list"]()
        cfg = await self.config()
        # 只用 AstrBot 原生人格：当前人格一律问 AstrBot，
        # 插件的 persona_active_id 只在原生接口读不到时当兜底（例如插件先启动了）。
        active = ""
        if self.system.get("active_persona"):
            try:
                active = str(await self.system["active_persona"]() or "")
            except Exception:
                active = ""
        if not active:
            active = str(cfg.get("persona_active_id", "") or "")
        counts = await self.store.persona_memory_counts()
        return {
            "available": True,
            "items": items,
            "active": active,
            "counts": counts,
            "isolated": bool(cfg.get("memory_persona_isolated", True)),
        }

    async def persona_save(self, payload: dict[str, Any]) -> dict[str, Any]:
        # 面板里改人格 = 一次"保存"，记下时间给「人格状态与进化」显示
        await self.store.set_config("persona_last_save_at", time.time())
        if not self.persona.get("update"):
            raise ValueError("persona provider unavailable")
        pid = (payload or {}).get("persona_id") or ""
        if not pid:
            raise ValueError("persona_id required")
        fields = {k: payload[k] for k in ("system_prompt", "begin_dialogs", "tools") if k in (payload or {})}
        await self.persona["update"](pid, fields)
        await self.store.audit("persona.update", pid, ",".join(fields.keys()))
        return {"updated": True}

    async def persona_set_active(self, pid: str) -> dict[str, Any]:
        """切换人格：走 AstrBot 官方配置路径真切换，并同步插件库 persona_active_id。"""
        result: dict[str, Any] = {}
        if self.system.get("activate_persona"):
            result = await self.system["activate_persona"](pid) or {}
        await self.store.set_config("persona_active_id", pid)
        await self.store.audit("persona.active", pid)
        return {"active": pid, "applied": bool(result.get("ok")), "error": result.get("error", "")}

    # ---------------- 系统读取（模型 / 人格等真实配置） ----------------
    async def providers(self) -> dict[str, Any]:
        """读取 AstrBot 真实的模型提供商配置（面板「模型分配」用）。"""
        if not self.system.get("providers"):
            return {"available": False, "llm": [], "tts": [], "embedding": [], "stt": [], "note": "当前 AstrBot 未提供模型列表接口"}
        try:
            data = await self.system["providers"]()
        except Exception as exc:
            return {"available": False, "llm": [], "tts": [], "embedding": [], "stt": [], "note": f"读取失败：{exc}"}
        data["available"] = True
        return data

    async def model_test(self, task: str, provider_id: str) -> dict[str, Any]:
        """试跑：真调一次所选提供商（LLM 走 text_chat，嵌入/重排走 get_embedding）。"""
        if not self.system.get("model_test"):
            return {"ok": False, "error": "当前 AstrBot 未提供模型调用接口"}
        try:
            return await self.system["model_test"](task, provider_id)
        except Exception as exc:
            return {"ok": False, "error": str(exc)[:200]}

    # ---------------- livingmemory 导入 ----------------
    async def import_detect(self) -> dict[str, Any]:
        info = await self.importer.detect_async()
        info["running"] = self._import_state.get("phase") not in ("idle", "done", "error")
        info["state"] = dict(self._import_state)
        info["batches"] = await self.store.list_batches(10)
        return info

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._import_subs.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        if q in self._import_subs:
            self._import_subs.remove(q)

    async def _broadcast(self, payload: dict[str, Any]) -> None:
        self._import_state = dict(payload)
        for q in list(self._import_subs):
            try:
                q.put_nowait(dict(payload))
            except asyncio.QueueFull:
                pass

    async def import_start(self, layers: list[str] | None = None, limit: int | None = None) -> dict[str, Any]:
        if self._import_task and not self._import_task.done():
            return {"started": False, "message": "已有导入任务在运行"}
        detect = await self.importer.detect_async()
        if not detect["db_exists"]:
            return {"started": False, "message": "未检测到 astrbot_plugin_livingmemory 数据库"}
        self._import_watch = True
        self._import_task = asyncio.create_task(self._run_import(layers, limit))
        return {"started": True, "total": detect["documents"] + detect["atoms"], "watch": True}

    async def _run_import(self, layers: list[str] | None, limit: int | None) -> None:
        try:
            await self.importer.run(self._broadcast, layers=layers, limit=limit)
            if self._import_watch:
                await self._broadcast(
                    {"phase": "watch", "message": "后台监控中：每 60s 检查源库新增并自动增量导入，直到追平"}
                )
        except Exception as exc:  # 导入失败要显式暴露给面板
            await self._broadcast({"phase": "error", "message": f"导入失败：{exc}"})
            await self.store.audit("import.error", SOURCE_NAME, str(exc))

    async def import_watch_tick(self) -> None:
        """后台循环调用：源库有新增就增量导入（幂等），追平后自动停止监控。"""
        if not self._import_watch or (self._import_task and not self._import_task.done()):
            return
        try:
            detect = await self.importer.detect_async()
        except Exception as exc:
            await self.store.audit("import.watch.error", SOURCE_NAME, f"对账失败：{exc}")
            return
        if not detect["db_exists"]:
            self._import_watch = False
            await self.store.audit("import.watch.stop", SOURCE_NAME, "源库不存在，停止监控")
            return
        source_total = detect["documents"] + detect["atoms"]
        imported = detect["already_imported"]
        if imported >= source_total:
            self._import_watch = False
            await self.store.audit("import.watch.done", SOURCE_NAME, f"已追平 {imported}/{source_total}，停止监控")
            await self._broadcast({"phase": "done", "done": source_total, "total": source_total, "message": "已追平源库，监控结束"})
            return
        await self.store.audit("import.watch.tick", SOURCE_NAME, f"新增 {source_total - imported} 条，开始增量导入")
        await self._broadcast(
            {"phase": "watch", "done": imported, "total": source_total, "message": f"发现新增 {source_total - imported} 条，自动增量导入"}
        )
        self._import_task = asyncio.create_task(self._run_watch_import())

    async def _run_watch_import(self) -> None:
        try:
            await self.importer.run(self._broadcast, layers=["documents", "atoms"], limit=None)
            if self._import_watch:
                await self._broadcast({"phase": "watch", "message": "增量完成，继续监控源库"})
        except Exception as exc:
            await self._broadcast({"phase": "error", "message": f"增量导入失败：{exc}"})
            await self.store.audit("import.error", SOURCE_NAME, str(exc))

    async def import_events(self):
        """SSE 生成器：先推当前状态，再实时推进度。"""
        q = self.subscribe()
        try:
            yield {"phase": self._import_state.get("phase", "idle"), "message": "已连接导入进度"}
            while True:
                try:
                    item = await asyncio.wait_for(q.get(), timeout=20)
                except TimeoutError:
                    yield {"phase": "ping", "message": "keepalive"}
                    continue
                yield item
                if item.get("phase") in ("done", "error"):
                    break
        finally:
            self.unsubscribe(q)

    # ---------------- 其他 ----------------
    async def audit(self, limit: int = 50) -> dict[str, Any]:
        return {"items": await self.store.list_audit(limit)}

    async def persona_zone_preview(self) -> dict[str, Any]:
        favs = await self.store.list_favours()
        return {
            "zones": [
                {"zone": z, "text": t, "users": [f["user_id"] for f in favs if f.get("zone") == z]}
                for z, t in ZONE_TEXT.items()
            ]
        }

    async def health(self) -> dict[str, Any]:
        return {"ok": True, "ts": time.time(), "source": SOURCE_NAME}

    # ===================== 记忆库（管理表）/ 召回 / 原文补齐 =====================

    async def memory_search(
        self,
        q: str = "",
        session_id: str = "",
        persona_id: str = "",
        status: str = "",
        atom_type: str = "",
        source: str = "",
        sort: str = "recent",
        limit: int = 20,
        offset: int = 0,
    ) -> dict[str, Any]:
        """记忆库管理表：关键词（含 ID/内容）+ 会话 + 状态 + 类型 + 来源 筛选，可排序分页。"""
        return await self.store.search_memories(
            q=q, session_id=session_id, persona_id=persona_id, status=status,
            atom_type=atom_type, source=source, sort=sort, limit=limit, offset=offset,
        )

    async def memory_sessions(self) -> dict[str, Any]:
        """会话清单 + 人格清单（记忆库按人格分类）。"""
        return {
            "items": await self.store.memory_sessions(),
            "personas": await self.store.memory_personas(),
            "msg": await self.store.msg_stats(),
        }

    async def vector_gens(self) -> dict[str, Any]:
        """多代向量库：每一代的模型/维度/覆盖率 + 当前在用哪一代。"""
        await self.store.ensure_vector_migration()
        gens = await self.store.gen_stats()
        active = next((g for g in gens if g["active"]), None)
        src = (await self.config()).get("model_embedding") or ""
        return {"ok": True, "items": gens, "active": active, "current_model": src,
                "note": "换模型/维度会新建一代，旧代保留（暂停使用），新代建好再切换；换回来立刻可用，不用重算"}

    async def vector_switch(self, gen_key: str) -> dict[str, Any]:
        gens = await self.store.gen_stats()
        if not any(g["gen_key"] == gen_key for g in gens):
            return {"ok": False, "error": "没有这一代向量库"}
        await self.store.set_active_gen(gen_key)
        await self.store.audit("vector.switch", gen_key, "")
        return {"ok": True, "active": gen_key, "items": await self.store.gen_stats()}

    async def vector_drop(self, gen_key: str) -> dict[str, Any]:
        n = await self.store.drop_gen(gen_key)
        await self.store.audit("vector.drop", gen_key, f"{n} 条")
        return {"ok": True, "removed": n, "items": await self.store.gen_stats()}

    async def embedding_coverage(self) -> dict[str, Any]:
        """向量覆盖按"当前代"算（旧列 embedding 已经不代表真相：换模型后旧向量会被暂停）。"""
        cov = await self.store.embedding_coverage()
        cfg = await self.config()
        model = str(cfg.get("model_embedding") or "")
        await self.store.ensure_vector_migration()
        gen = await self.store.active_gen()
        total = (await self.store.gen_stats()) or []
        act = next((g for g in total if g["gen_key"] == gen), None)
        if act:
            cov["total"] = act["total"]
            cov["embedded"] = act["covered"]
            cov["missing"] = max(0, act["total"] - act["covered"])
            cov["gen"] = act["gen_key"]
            cov["dim"] = act["dim"]
        cov["model"] = model
        cov["status"] = await self.store.status_counts()
        return cov

    async def sync_active_gen(self) -> dict[str, Any]:
        """模型即切换按钮：配置里选了哪个向量模型，就切到"那一代"向量库（维度不同也不影响）。

        逻辑：按模型名找已有的代 → 找到就用它（换回旧模型 = 秒切回旧向量库）；
        没找到就留空，等第一次回填时按真实维度建新代（旧代保持不动）。
        """
        cfg = await self.config()
        model = str(cfg.get("model_embedding") or "").strip()
        if not model:
            return {"ok": False, "error": "未配置向量嵌入模型"}
        await self.store.ensure_vector_migration()
        gens = await self.store.gen_stats()
        want = self.store.norm_model(model)
        same = [g for g in gens if self.store.norm_model(str(g["model"])) == want]
        if not same:
            return {"ok": True, "switched": False, "note": f"还没有「{model}」这一代向量，回填时会自动建立", "items": gens}
        # 同模型可能有多代（历史维度不同）：优先覆盖率高的一代
        best = sorted(same, key=lambda g: g["covered"], reverse=True)[0]
        if not best["active"]:
            await self.store.set_active_gen(best["gen_key"])
            await self.store.audit("vector.autoswitch", best["gen_key"], f"model={model}")
        return {"ok": True, "switched": True, "active": best["gen_key"], "dim": best["dim"],
                "coverage": best["coverage"], "items": await self.store.gen_stats()}

    async def model_dims(self) -> dict[str, Any]:
        """每个向量模型已知的维度（从已有代推出来的；面板模型分配里显示用）。"""
        await self.store.ensure_vector_migration()
        out: dict[str, int] = {}
        for g in await self.store.gen_stats():
            m = str(g["model"] or "").strip()
            if m and int(g["dim"] or 0) > 0:
                out[m] = max(out.get(m, 0), int(g["dim"]))
        return {"ok": True, "dims": out, "active_gen": await self.store.active_gen()}

    async def config_save_sync(self) -> dict[str, Any]:
        """配置保存后的副作用：向量模型变了就自动切到对应代。"""
        try:
            return await self.sync_active_gen()
        except Exception as exc:                             # noqa: BLE001
            return {"ok": False, "error": str(exc)[:120]}

    async def memory_reactivate(self, ids: list[int] | None = None, floor: float = 0.45) -> dict[str, Any]:
        """批量激活（把 stale/decayed 拉回召回范围）。"""
        n = await self.store.reactivate(ids=ids, floor=float(floor or 0.45))
        await self.store.audit("memory.reactivate", ",".join(str(i) for i in (ids or [])[:8]) or "all-stale", f"{n} 条")
        cov = await self.store.embedding_coverage()
        return {"ok": True, "reactivated": n, "coverage": cov}

    async def messages_list(self, session_id: str, q: str = "", limit: int = 200, offset: int = 0) -> dict[str, Any]:
        return await self.store.messages_for_session(session_id=session_id, q=q, limit=limit, offset=offset)

    async def memory_evidence(self, mid: str) -> dict[str, Any]:
        """详情页的"详细对话"：① 记忆学自哪几句（精确）② 原文（不截断）③ 那一刻前后的真实对话。"""
        raw = str(mid).lstrip("m")
        mem = await self.store.get_memory(raw)
        if mem is None:
            return {"ok": False, "error": "memory not found"}
        sources = await self.store.memory_sources(raw)
        text = mem.get("source_text") or ""
        sid = mem.get("session_id") or ""
        ts = float(mem.get("occurred_at") or mem.get("created_at") or 0)
        around = await self.store.messages_around(sid, ts, before=8, after=8) if sid else []
        have_msgs = len((await self.messages_list(sid, limit=1))["items"]) > 0 if sid else False
        return {
            "ok": True,
            "id": raw,
            "sources": sources,
            "source_text": text,
            "around": around,
            "session_id": sid,
            "session_has_messages": have_msgs,
            "note": "" if (sources or around) else (
                "这条记忆是导入来的旧数据，来源库没留下对应对话" if mem.get("external_source") else
                "这条记忆写入时没带上下文（旧版本行为），新记忆都会带"
            ),
        }

    async def import_extras(self) -> dict[str, Any]:
        """补齐 livingmemory 的会话消息 / 记忆来源轮次 / 完整原文（幂等，可反复点）。"""
        if self._import_task and not self._import_task.done():
            return {"ok": False, "error": "已有导入任务在跑，等它跑完再点"}
        out = await self.importer.import_extras(self._broadcast)
        out["ok"] = True
        out["stats"] = await self.store.msg_stats()
        await self.store.audit("import.extras", "livingmemory", str(out)[:200])
        return out

    # ===================== 自动遗忘生命周期 =====================

    async def lifecycle_preview(self) -> dict[str, Any]:
        """面板要看的两条队列 + 现状统计（只读）。"""
        cfg = await self.config()
        arch = 90.0 if cfg.get("archive_after_days") is None else float(cfg["archive_after_days"])
        forg = 180.0 if cfg.get("forget_after_days") is None else float(cfg["forget_after_days"])
        queue = await self.store.memory_lifecycle_scan(archive_days=arch, forget_days=forg)
        nowt = __import__("time").time()
        arch_cut = nowt - arch * 86400
        return {
            "ok": True,
            "enabled": bool(cfg.get("forget_enabled", True)),
            "archive_after_days": arch,
            "forget_after_days": forg,
            "min_strength": 0.2 if cfg.get("forget_min_strength") is None else float(cfg["forget_min_strength"]),
            "keep_poignancy": 6.0 if cfg.get("forget_keep_poignancy") is None else float(cfg["forget_keep_poignancy"]),
            "counts": await self.store.lifecycle_counts(),
            "archive_queue": [x for x in queue if x.get("status") == "active" and float(x.get("last_touch") or 0) < arch_cut],
            "forget_queue": [x for x in queue if x.get("status") == "archived"],
        }

    async def lifecycle_tick(self, limit: int | None = None, dry: bool = False) -> dict[str, Any]:
        """跑一轮：归档 + 遗忘。被提及过的（last_used_at 新）自然不会被选中。"""
        cfg = await self.config()
        if not cfg.get("forget_enabled", True):
            return {"ok": True, "skipped": "自动遗忘已关闭"}
        # 0 是合法值（= 立刻归档），不能被 `or` 吞成默认值；只有 None 才取默认。
        # 默认值同步上调：30/30 太短，两个月的沉默不该等于死亡。
        arch = 90.0 if cfg.get("archive_after_days") is None else float(cfg["archive_after_days"])
        forg = 180.0 if cfg.get("forget_after_days") is None else float(cfg["forget_after_days"])
        min_s = 0.2 if cfg.get("forget_min_strength") is None else float(cfg["forget_min_strength"])
        keep_p = 6.0 if cfg.get("forget_keep_poignancy") is None else float(cfg["forget_keep_poignancy"])
        limit = int(limit or cfg.get("forget_batch_per_tick") or 20)
        pv = await self.lifecycle_preview()
        arch_ids = [int(x["id"]) for x in pv["archive_queue"][:limit]]
        # 遗忘只针对"不重要"的：强度低于阈值 且 情绪强度不高（重要记忆受保护）
        forget_ids = [
            int(x["id"])
            for x in pv["forget_queue"]
            if float(x.get("strength") or 0) < min_s and float(x.get("poignancy") or 0) < keep_p
        ][:limit]
        if dry:
            return {"ok": True, "dry": True, "would_archive": len(arch_ids), "would_forget": len(forget_ids)}
        # 「记忆遗忘评估」：配了模型就先让模型在这些候选里挑"哪条真该忘"。
        # 模型只能**收窄**候选（阈值已经先筛过一遍），失败就退回阈值判定 —— 永不因为模型乱来而多忘。
        if forget_ids and str(cfg.get("model_forget") or ""):
            picked = await self._model_pick_forget(forget_ids)
            if picked is not None:
                forget_ids = picked
        n_a = await self.store.archive_memories(arch_ids, why="idle")
        n_f = await self.store.forget_memories(forget_ids, why="unimportant")
        if n_a or n_f:
            await self.store.audit("memory.lifecycle", f"archive={n_a} forget={n_f}", f"arch={arch}d forget={forg}d")
        return {"ok": True, "archived": n_a, "forgotten": n_f, "counts": await self.store.lifecycle_counts()}

    async def _model_pick_forget(self, ids: list[int]) -> list[int] | None:
        """让「记忆遗忘评估」模型在这些候选里挑出真正可以忘掉的 id。

        返回 None = 模型不可用/输出没法解析 → 调用方退回阈值判定（保守：宁可少忘）。
        """
        gen = self.system.get("llm_generate")
        if not callable(gen) or not ids:
            return None
        sql = "SELECT id, summary, strength, poignancy, recalls FROM hf_memories WHERE id IN (%s)" % ",".join("?" * len(ids))
        rows = await (await self.store.db.execute(sql, ids)).fetchall()
        if not rows:
            return None
        lines = [
            "%s | 强度%.2f | 情绪%s | 被提及%s次 | %s"
            % (r["id"], float(r["strength"] or 0), r["poignancy"], r["recalls"], str(r["summary"] or "")[:60])
            for r in rows
        ]
        prompt = (
            "下面是长期没人提起的记忆候选，请判断哪些**可以安全忘掉**。\n\n"
            "判断标准：只留那些「事实性、可重新问到、对关系没有长期价值」的；"
            "凡是涉及人、偏好、约定、共同经历、称呼、雷区的，一律保留。\n\n"
            "只输出可以忘掉的 id，一行一个；一个都没有就只输出「无」。不要解释、不要编号、不要其它文字。\n\n"
            + "\n".join(lines)
        )
        try:
            out = await gen(prompt, "forget")
        except Exception as exc:                            # noqa: BLE001 模型挂了不许影响遗忘主流程
            logger.debug(f"[StarHeart] 遗忘评估失败，退回阈值判定：{exc}")
            return None
        picked: list[int] = []
        # 只接受「整行纯 id」或「逗号分隔的纯 id」。旧实现对整行 findall(r"\d+")：
        # 模型只要复述候选表（「12 | 强度0.30 | 情绪5 | 被提及3次」），12/0/30/5/3 全被当 id
        # → 一次忘光整批候选（P0-2）。含任何其它字符的行整行丢弃。
        id_line = re.compile(r"^\s*\d+(?:\s*[,，]\s*\d+)*\s*$")
        cap = min(len(ids), 20)                    # 单次遗忘上限：防模型整批输出
        for line in str(out or "").splitlines():
            line = line.strip()
            if not line or line in ("无", "none", "None", "没有"):
                continue
            if not id_line.match(line):
                continue
            for tok in re.findall(r"\d+", line):
                v = int(tok)
                if v in ids and v not in picked:
                    picked.append(v)
                if len(picked) >= cap:
                    break
            if len(picked) >= cap:
                break
        if picked and len(picked) == len(ids) and len(ids) >= 5:
            # 候选 ≥5 时「全选」是模型复述/失控的典型形态。遗忘是批量不可逆操作，
            # 宁可这轮一条不忘，等下一轮阈值判定再来。
            logger.warning(
                f"[StarHeart] 遗忘评估返回了全部 {len(ids)} 个候选，判为异常输出，本轮不遗忘")
            return []
        if not picked:
            return []                                        # 模型说"一条都不用忘" → 尊重它
        await self.store.audit("memory.forget.review", f"{len(ids)}→{len(picked)}", "模型筛过遗忘候选")
        return picked

    # ===================== 消息事件聚合（该页一次取全，前端不再自己拼） =====================

    async def message_hub(self, limit: int = 60, session_id: str = "") -> dict[str, Any]:
        """门控 / 主动 / 撤回三源合并：KPI + 24h 节律 + 漏斗原料 + 跳过原因 + 统一事件流。

        为什么在后端聚合：前端要发 6 次请求再对齐时间与分桶，既慢又容易前后不一致；
        数据量又很小（门控 230 / 主动 5974），一次算完最省事也最准。
        """
        import json as _json
        import time as _t

        nowt = _t.time()
        day = nowt - 86400
        week = nowt - 7 * 86400      # 注意：是"时间戳-7天"，不是 day*7（曾因此热力图全 0）
        month = nowt - 30 * 86400
        where_s = ""
        args: list[Any] = []
        if session_id:
            where_s = " AND session_id=?"
            args = [session_id]
        db = self.store.db
        # 三类各取最近 per 条再合并：门控/撤回数据量远小于主动，若统一 LIMIT，
        # 时间轴会被"主动"淹没（实测 60 条全是 proactive）→ 页面就没用了。
        per = max(10, int(limit) // 3)

        async def week_grid(table: str, extra: str = "") -> list[list[int]]:
            """7(周一..周日) × 24 小时 计数网格。SQLite 的 %w 是 0=周日，手动挪成周一开头。"""
            grid = [[0] * 24 for _ in range(7)]
            rows = await (await db.execute(
                "SELECT CAST(strftime('%w', ts, 'unixepoch') AS INT) AS w,"
                " CAST(strftime('%H', ts, 'unixepoch') AS INT) AS h, COUNT(*) AS n"
                f" FROM {table} WHERE ts>?{extra} GROUP BY w, h",
                # 占位符与参数必须配平：extra 里没有 ? 时不能把 args 全塞进去
                # （审查实证：带会话筛选时 1 个占位符配 2 个参数 → sqlite3.ProgrammingError → 整页 500）
                [week, *list(args)[: extra.count("?")]])).fetchall()
            for r in rows:
                w = (int(r["w"]) + 6) % 7
                h = int(r["h"])
                if 0 <= w <= 6 and 0 <= h <= 23:
                    grid[w][h] = int(r["n"])
            return grid

        # ---------- 门控 ----------
        gate_rows = await (await db.execute(
            f"SELECT * FROM hf_gate_log WHERE 1=1{where_s} ORDER BY ts DESC LIMIT ?", [*args, per]
        )).fetchall()
        gate_all = (await (await db.execute("SELECT COUNT(*) FROM hf_gate_log")).fetchone())[0]
        gate_24 = (await (await db.execute(
            f"SELECT COUNT(*) FROM hf_gate_log WHERE ts>?{where_s}", [day, *args])).fetchone())[0]
        gate_by_dec: dict[str, int] = {}
        for r in await (await db.execute(
                f"SELECT decision, COUNT(*) AS n FROM hf_gate_log WHERE 1=1{where_s} GROUP BY decision", args)).fetchall():
            gate_by_dec[str(r["decision"] or "")] = int(r["n"])
        gate_hours = [0] * 24
        for r in await (await db.execute(
                f"SELECT CAST(strftime('%H', ts, 'unixepoch') AS INT) AS h, COUNT(*) AS n"
                f" FROM hf_gate_log WHERE ts>?{where_s} GROUP BY h", [week, *args])).fetchall():
            if 0 <= int(r["h"]) <= 23:
                gate_hours[int(r["h"])] = int(r["n"])
        gate_bands: dict[str, int] = {}
        for r in await (await db.execute(
                f"SELECT CAST(score/20 AS INT)*20 AS b, COUNT(*) AS n FROM hf_gate_log"
                f" WHERE 1=1{where_s} GROUP BY b ORDER BY b", args)).fetchall():
            gate_bands[str(int(r["b"]))] = int(r["n"])

        # ---------- 主动 ----------
        pro_rows = await (await db.execute(
            f"SELECT * FROM hf_proactive_log WHERE 1=1{where_s} ORDER BY ts DESC LIMIT ?", [*args, per]
        )).fetchall()
        pro_24 = (await (await db.execute(
            f"SELECT COUNT(*) FROM hf_proactive_log WHERE ts>?{where_s}", [day, *args])).fetchone())[0]
        pro_by_status: dict[str, int] = {}
        for r in await (await db.execute(
                f"SELECT status, COUNT(*) AS n FROM hf_proactive_log WHERE 1=1{where_s} GROUP BY status", args)).fetchall():
            pro_by_status[str(r["status"] or "")] = int(r["n"])
        pro_reasons = [dict(r) for r in await (await db.execute(
            f"SELECT reason, COUNT(*) AS n FROM hf_proactive_log WHERE 1=1{where_s}"
            f" GROUP BY reason ORDER BY n DESC LIMIT 6", args)).fetchall()]
        pro_zones: dict[str, int] = {}
        for r in await (await db.execute(
                f"SELECT zone, COUNT(*) AS n FROM hf_proactive_log WHERE 1=1{where_s} GROUP BY zone", args)).fetchall():
            pro_zones[str(r["zone"] or "未标")] = int(r["n"])
        pro_hours = [0] * 24
        for r in await (await db.execute(
                f"SELECT CAST(strftime('%H', ts, 'unixepoch') AS INT) AS h, COUNT(*) AS n"
                f" FROM hf_proactive_log WHERE status='sent' AND ts>?{where_s} GROUP BY h",
                [month, *args])).fetchall():
            if 0 <= int(r["h"]) <= 23:
                pro_hours[int(r["h"])] = int(r["n"])

        # ---------- 撤回 ----------
        recall_rows = await (await db.execute(
            f"SELECT * FROM hf_recall_log WHERE 1=1{where_s} ORDER BY ts DESC LIMIT ?", [*args, per]
        )).fetchall()
        recall_total = (await (await db.execute(
            f"SELECT COUNT(*) FROM hf_recall_log WHERE 1=1{where_s}", args)).fetchone())[0]

        # ---------- 会话清单（筛选下拉用）----------
        sess: dict[str, int] = {}
        for r in await (await db.execute(
                "SELECT session_id, COUNT(*) AS n FROM ("
                " SELECT session_id FROM hf_gate_log UNION ALL SELECT session_id FROM hf_proactive_log"
                ") GROUP BY session_id ORDER BY n DESC LIMIT 30")).fetchall():
            sess[str(r["session_id"] or "")] = int(r["n"])

        # ---------- 三源合并成统一事件流 ----------
        stream: list[dict[str, Any]] = []
        for r in gate_rows:
            d = dict(r)
            try:
                d["factors"] = _json.loads(d.get("factors") or "{}")
            except (TypeError, ValueError):
                d["factors"] = {}
            stream.append({"kind": "gate", "ts": d.get("ts") or 0, "session_id": d.get("session_id") or "",
                           "user_id": d.get("user_id") or "", "data": d})
        for r in pro_rows:
            d = dict(r)
            try:
                d["guard"] = _json.loads(d.get("guard") or "{}")
            except (TypeError, ValueError):
                d["guard"] = {}
            stream.append({"kind": "proactive", "ts": d.get("ts") or 0, "session_id": d.get("session_id") or "",
                           "user_id": d.get("user_id") or "", "data": d})
        for r in recall_rows:
            d = dict(r)
            stream.append({"kind": "recall", "ts": d.get("ts") or 0, "session_id": d.get("session_id") or "",
                           "user_id": d.get("user_id") or "", "data": d})
        stream.sort(key=lambda x: -float(x.get("ts") or 0))

        sent = int(pro_by_status.get("sent", 0))
        skipped = int(pro_by_status.get("skipped", 0))
        cand = int(pro_by_status.get("candidate", 0))
        return {
            "ok": True,
            "kpi": {
                "gate_24h": int(gate_24), "gate_total": int(gate_all),
                "gate_reply": int(gate_by_dec.get("reply", 0)),
                "gate_silent": int(gate_by_dec.get("silent", 0)),
                "gate_backoff": int(gate_by_dec.get("backoff", 0)),
                "pro_24h": int(pro_24), "pro_sent": sent, "pro_skipped": skipped, "pro_candidate": cand,
                "recall_total": int(recall_total),
            },
            "rhythm": {"gate_hours": gate_hours, "pro_hours": pro_hours},
            "grid": {"gate": await week_grid("hf_gate_log"),
                     "pro": await week_grid("hf_proactive_log", " AND status='sent'")},
            "gate_bands": gate_bands,
            "gate_threshold": float((await self.config()).get("gate_threshold") or 80),
            "pro_reasons": pro_reasons,
            "pro_zones": pro_zones,
            "sessions": sess,
            "stream": stream[:limit],
            "per_kind": per,
            "counts": {"gate": int(gate_all), "pro": sent + skipped + cand, "recall": int(recall_total)},
        }

    # ===================== 拟人指标（只读聚合：六指标 + 长度分布 + 门控因子分布） =====================
    # 三态纪律：ok / INSUFFICIENT_DATA（样本不足）/ NOT_IMPLEMENTED（口径未接）/ ERROR（读失败）。
    # 非 ok 一律 value=null，不返 0 冒充。每项 definition 写清算法：口径变化只改这里，前端不重算。
    # 出站口径：只统计真发出（emit_ok=0 不计入；首条未确认单独记账并按已交发计入，见 main._log_first_seg）。
    N_MIN_ABS = 200              # 绝对类指标最小样本（长度/连发/回复间隔/因子分布）
    N_MIN_RATIO = 250            # 比例类指标最小样本（沉默率/表情率/表达命中率）
    BURST_GAP_SECONDS = 30.0     # 连发：同会话相邻两条发出间隔 ≤ 30s 算同一串
    REPLY_WINDOW_SECONDS = 600.0 # 回复间隔：入站消息后 10 分钟内跟上的第一条发出才算回复
    READ_LIMIT = 20000           # 每类流水最多读入的行数（取窗口内最近 N 行）

    @staticmethod
    def _pct(values: list[float], p: float) -> float | None:
        """最近秩分位（样本为空返回 None，不返 0 冒充）。"""
        if not values:
            return None
        xs = sorted(float(v) for v in values)
        idx = min(len(xs) - 1, max(0, int(round((len(xs) - 1) * p))))
        return round(xs[idx], 1)

    @staticmethod
    def _read_err(table: str, exc: Exception) -> str:
        """读失败的原因文案：与「样本不足/未实现」严格区分（随接口 reason 返回）。"""
        return f"读失败（{table}）：{str(exc)[:160]}"

    async def metrics(self, days: int = 30) -> dict[str, Any]:
        """拟人指标页数据源（GET /page/metrics）：每项带 n 与 state，空表/样本不足/读失败如实报态。"""
        window = max(1, int(days))
        since = time.time() - window * 86400
        db = self.store.db
        out: dict[str, Any] = {
            "ok": True,
            "window_days": window,
            "n_min": {"absolute": self.N_MIN_ABS, "ratio": self.N_MIN_RATIO},
            "metrics": {},
            "length_dist": {"state": "INSUFFICIENT_DATA", "n": 0, "bins": [],
                            "p50": None, "p90": None, "mean": None},
            "gate_factors": {"state": "INSUFFICIENT_DATA", "n": 0, "items": [],
                            "note": "只统计出现过该因子的记录；constant=窗口内该因子只有一个取值（低频因子的恒值未必代表退化）"},
            "notes": [
                "沉默率与因子分布取自决策记录；长度、连发、回复间隔、表情率取自发送记录；表达命中率等待出站对账接入。",
                f"统计窗口 = 最近 {window} 天；每类最多读取窗口内最近 20000 行；发送失败的记录不计入出站指标。",
                "每轮回复的首条由框架管线直接发出、拿不到发送回执：单独记账并以「未确认」计入出站指标。",
            ],
        }

        # ---------- 决策流水：沉默率 + 因子分布 ----------
        dec: list[dict[str, Any]] = []
        dec_err = ""
        try:
            rows = await (await db.execute(
                "SELECT ts,session_id,decision,factors_json FROM hf_decision_log"
                " WHERE ts>=? ORDER BY ts DESC LIMIT ?", (since, self.READ_LIMIT))).fetchall()
            dec = [dict(r) for r in rows]
        except Exception as exc:                            # noqa: BLE001 表不可读 → ERROR + reason
            dec_err = self._read_err("hf_decision_log", exc)
            out["decision_error"] = dec_err
        n_dec = len(dec)
        n_silent = sum(1 for d in dec if str(d.get("decision") or "") in ("silent", "backoff", "blocked"))
        sil_state = ("ok" if n_dec >= self.N_MIN_RATIO
                     else ("ERROR" if dec_err else "INSUFFICIENT_DATA"))
        out["metrics"]["silence_rate"] = {
            "value": (round(n_silent / n_dec, 4) if sil_state == "ok" else None),
            "n": n_dec,
            "n_min": self.N_MIN_RATIO,
            "state": sil_state,
            "definition": "沉默率 = 门控判定为沉默/退避/拦截的轮数 ÷ 有决策记录的轮数；影子模式下的判定同样计入",
            "reason": dec_err,
        }
        fac: dict[str, dict[str, int]] = {}
        for d in dec:
            try:
                f = json.loads(d.get("factors_json") or "{}")
            except (TypeError, ValueError):
                f = {}
            if not isinstance(f, dict):
                continue
            for k, v in f.items():
                vals = fac.setdefault(str(k), {})
                vals[str(v)] = vals.get(str(v), 0) + 1
        items: list[dict[str, Any]] = []
        for k, vals in sorted(fac.items(), key=lambda kv: -sum(kv[1].values())):
            top = sorted(vals.items(), key=lambda kv: (-kv[1], kv[0]))[:3]
            items.append({
                "factor": k,
                "n": sum(vals.values()),
                "distinct": len(vals),
                "constant": len(vals) <= 1,
                "top": [{"value": v, "n": c} for v, c in top],
            })
        fac_state = ("ok" if n_dec >= self.N_MIN_ABS
                     else ("ERROR" if dec_err else "INSUFFICIENT_DATA"))
        out["gate_factors"] = {
            "state": fac_state,
            "n": n_dec,
            "n_min": self.N_MIN_ABS,
            "items": items,
            "reason": dec_err,
            "note": "只统计出现过该因子的记录；constant=窗口内该因子只有一个取值（低频因子的恒值未必代表退化）",
        }

        # ---------- 出站流水：长度 / 连发 / 回复间隔 / 表情率 ----------
        # 口径 = 真发出：emit_ok=0（确认失败）不计入；NULL 是首条未确认（见 main._log_first_seg），按已交发计入。
        sent: list[dict[str, Any]] = []
        sent_err = ""
        try:
            rows = await (await db.execute(
                "SELECT ts,session_id,kind,length_chars FROM hf_sent_log"
                " WHERE ts>=? AND (emit_ok=1 OR emit_ok IS NULL) ORDER BY ts DESC LIMIT ?",
                (since, self.READ_LIMIT))).fetchall()
            sent = [dict(r) for r in rows]
        except Exception as exc:                            # noqa: BLE001
            sent_err = self._read_err("hf_sent_log", exc)
        n_sent = len(sent)
        sent_state = ("ok" if n_sent >= self.N_MIN_ABS
                      else ("ERROR" if sent_err else "INSUFFICIENT_DATA"))
        sent_by_sess: dict[str, list[float]] = {}
        for s in sent:
            sent_by_sess.setdefault(str(s.get("session_id") or ""), []).append(float(s.get("ts") or 0))
        for v in sent_by_sess.values():
            v.sort()

        # 长度 P50（去空白单条长度；hf_sent_log.length_chars 写入时即去空白）
        lengths = [int(s.get("length_chars") or 0) for s in sent if int(s.get("length_chars") or 0) > 0]
        len_state = ("ok" if len(lengths) >= self.N_MIN_ABS
                     else ("ERROR" if sent_err else "INSUFFICIENT_DATA"))
        out["metrics"]["length_p50"] = {
            "value": (self._pct(lengths, 0.5) if len_state == "ok" else None),
            "unit": "字",
            "n": len(lengths),
            "n_min": self.N_MIN_ABS,
            "state": len_state,
            "definition": "去空白单条长度（拆段后每条单独算）的 P50；发送失败不计入",
            "reason": sent_err,
        }

        # 连发条数（同会话相邻发出间隔 ≤30s 的连续段长度）
        bursts: list[int] = []
        for tss in sent_by_sess.values():
            run = 1 if tss else 0
            for a, b in zip(tss, tss[1:]):
                if (b - a) <= self.BURST_GAP_SECONDS:
                    run += 1
                else:
                    bursts.append(run)
                    run = 1
            if tss:
                bursts.append(run)
        out["metrics"]["burst_length"] = {
            "value": (self._pct(bursts, 0.5) if sent_state == "ok" else None),
            "unit": "条",
            "n": n_sent,
            "n_extra": {"bursts": len(bursts), "mean": (round(sum(bursts) / len(bursts), 2) if bursts else None)},
            "n_min": self.N_MIN_ABS,
            "state": sent_state,
            "definition": "连发条数 = 同会话内相邻两条发出间隔 ≤30 秒的连续段长度（报 P50，段数见 n_extra.bursts）；发送失败不计入",
            "reason": sent_err,
        }

        # 回复间隔 P50（入站消息 → 其后的第一条发出；同一条发出只计一次，避免被连续多条入站重复计入）
        dec_by_sess: dict[str, list[float]] = {}
        for d in dec:
            dec_by_sess.setdefault(str(d.get("session_id") or ""), []).append(float(d.get("ts") or 0))
        gaps: list[float] = []
        for sid, dts in dec_by_sess.items():
            stss = sent_by_sess.get(sid) or []
            if not stss:
                continue
            ptr = 0
            for t in sorted(dts):
                while ptr < len(stss) and stss[ptr] < t:
                    ptr += 1
                if ptr >= len(stss):
                    break
                if (stss[ptr] - t) <= self.REPLY_WINDOW_SECONDS:
                    gaps.append(stss[ptr] - t)
                    ptr += 1                                 # 这条发出已配对，下一条入站从再下一条发出找
        ri_state = ("ok" if len(gaps) >= self.N_MIN_ABS
                    else ("ERROR" if (sent_err or dec_err) else "INSUFFICIENT_DATA"))
        out["metrics"]["reply_interval_p50"] = {
            "value": (self._pct(gaps, 0.5) if ri_state == "ok" else None),
            "unit": "秒",
            "n": len(gaps),
            "n_min": self.N_MIN_ABS,
            "state": ri_state,
            "definition": "回复间隔 = 入站消息 → 其后的第一条发出（同一条发出只计一次）的时间差（报 P50）；发送失败不计入",
            "reason": sent_err or dec_err,
        }

        # 表情率（表情包发出 ÷ 全部发出）
        n_sticker = sum(1 for s in sent if str(s.get("kind") or "") == "sticker")
        sticker_state = ("ok" if n_sent >= self.N_MIN_RATIO
                         else ("ERROR" if sent_err else "INSUFFICIENT_DATA"))
        out["metrics"]["sticker_rate"] = {
            "value": (round(n_sticker / n_sent, 4) if sticker_state == "ok" else None),
            "n": n_sent,
            "n_min": self.N_MIN_RATIO,
            "state": sticker_state,
            "definition": "表情率 = 表情包（sticker）发出条数 ÷ 全部发出条数；发送失败不计入",
            "reason": sent_err,
        }

        # ---------- 表达命中率（分子待出站对账接入） ----------
        n_expr = n_recon = n_hit = 0
        expr_err = ""
        try:
            row = await (await db.execute(
                "SELECT COUNT(*) AS n,"
                " SUM(CASE WHEN emit_ok IS NOT NULL THEN 1 ELSE 0 END) AS recon,"
                " SUM(CASE WHEN emit_ok=1 THEN 1 ELSE 0 END) AS hits"
                " FROM hf_expr_use_log WHERE used_at>=?", (since,))).fetchone()
            n_expr = int(row["n"] or 0)
            n_recon = int(row["recon"] or 0)
            n_hit = int(row["hits"] or 0)
        except Exception as exc:                            # noqa: BLE001
            expr_err = self._read_err("hf_expr_use_log", exc)
        if expr_err:
            out["metrics"]["expr_hit_rate"] = {
                "value": None,
                "n": n_expr,
                "n_min": self.N_MIN_RATIO,
                "state": "ERROR",
                "definition": "表达命中率 = 出站文本真的含该表达的次数 ÷ 已对账的注入次数",
                "reason": expr_err,
            }
        elif n_recon > 0:
            expr_state = "ok" if n_recon >= self.N_MIN_RATIO else "INSUFFICIENT_DATA"
            out["metrics"]["expr_hit_rate"] = {
                "value": (round(n_hit / n_recon, 4) if expr_state == "ok" else None),
                "n": n_recon,
                "n_min": self.N_MIN_RATIO,
                "state": expr_state,
                "definition": "表达命中率 = 出站文本真的含该表达的次数 ÷ 已对账的注入次数",
                "reason": "",
            }
        else:
            out["metrics"]["expr_hit_rate"] = {
                "value": None,
                "n": n_expr,
                "n_min": self.N_MIN_RATIO,
                "state": "NOT_IMPLEMENTED",
                "definition": "表达命中率 = 出站文本真的含该表达的次数 ÷ 注入次数",
                "reason": "等待出站对账接入：当前只记录「注入即命中」，尚未回读实际发出内容",
            }

        # ---------- 长度分布（条形用） ----------
        bin_defs = [(0, 10), (11, 20), (21, 40), (41, 80), (81, 160), (161, 10 ** 9)]
        bins = []
        for lo, hi in bin_defs:
            label = f"{lo}-{hi}" if hi < 10 ** 9 else f"{lo}+"
            bins.append({"label": label, "n": sum(1 for v in lengths if lo <= v <= hi)})
        out["length_dist"] = {
            "state": len_state,
            "n": len(lengths),
            "n_min": self.N_MIN_ABS,
            "bins": bins,
            "p50": self._pct(lengths, 0.5),
            "p90": self._pct(lengths, 0.9),
            "mean": (round(sum(lengths) / len(lengths), 1) if lengths else None),
            "definition": "去空白单条长度分桶（字）；发送失败不计入",
            "reason": sent_err,
        }
        return out

"""StarHeart 存储层：单库多表，全部通过 aiosqlite 访问。

一张库同时承载记忆、关系链、好感、表达习惯、门控流水、token 消耗、导入批次与配置；
面板的每个页面都直接读这里的真实数据，不做假数据。
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import time
import uuid
from pathlib import Path
from typing import Any

import aiosqlite
from astrbot.api import logger

from . import engine



def _json_default(obj: Any) -> Any:
    """备份用 JSON 序列化：embedding 是 BLOB，必须转 base64，否则整份备份失败。"""
    if isinstance(obj, (bytes, bytearray)):
        return base64.b64encode(bytes(obj)).decode("ascii")
    if isinstance(obj, set):
        return list(obj)
    return str(obj)

def _jlist(v: Any) -> list[str]:
    """JSON 数组字段容错解析（历史数据里可能是空串/None/半截 JSON）。"""
    if isinstance(v, list):
        return [str(x) for x in v]
    try:
        out = json.loads(v or "[]")
        return [str(x) for x in out] if isinstance(out, list) else []
    except Exception:
        return []


def _clip(text: Any, limit: int = 500) -> str:
    """消息文本截断（流水只存前 limit 字，不存完整原文）。"""
    return str(text or "")[: int(limit)]


def _jobj_list(v: Any) -> list[dict[str, Any]]:
    """JSON 对象数组字段容错解析（出处快照 source_ids / evidence_snap）。"""
    if isinstance(v, list):
        src: Any = v
    else:
        try:
            src = json.loads(v or "[]")
        except Exception:
            return []
    return [dict(x) for x in src if isinstance(x, dict)] if isinstance(src, list) else []


def _hash16(text: Any) -> str:
    """文本摘要（sha256 前 16 位十六进制）：流水对账锚点；不可原文回读。"""
    return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()[:16]


def _len_nows(text: Any) -> int:
    """去空白后的字符数（长度口径：只算可见内容，空白与全角空格不计）。"""
    return len(re.sub(r"\s+", "", str(text or "")))


# 库结构版本档位。以前没有任何版本机制，全靠下面一长串 `if 列 not in cols: ALTER` 幂等探测 ——
# 能跑，但无法回答「这个库升到哪一档了」，也写不出「只跑一次」的迁移。
# 新库直接盖当前档；旧库原地升档（上面的 ALTER 都是幂等的，不需要重跑）。
SCHEMA_VERSION = 6

# 审查补的三条索引：高频查询列此前完全没索引 → 全表扫描。
# 必须**在列升级（ALTER TABLE）之后**执行，因为它们引用的 last_used_at 是由 ALTER 补上的列，
# 全新库在 SCHEMA 阶段还没有它（踩过一次：写进 SCHEMA 直接 `no such column: last_used_at`）。
#   idx_hf_mem_persona   —— 几乎每个面板查询都带 COALESCE(persona_id,'')=? 过滤
#   idx_hf_mem_lifecycle —— 衰减/遗忘按状态筛再按最近使用排序
#   idx_hf_edge_dst      —— 按 dst 反查「谁指向我」（实体反查 / 孤儿边清理）
_LATE_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_hf_mem_persona ON hf_memories(persona_id)",
    "CREATE INDEX IF NOT EXISTS idx_hf_mem_lifecycle ON hf_memories(status, last_used_at)",
    "CREATE INDEX IF NOT EXISTS idx_hf_edge_dst ON hf_edges(dst_type, dst_id)",
)

SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS hf_memories (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        summary TEXT NOT NULL,
        content TEXT DEFAULT '',
        atom_type TEXT NOT NULL DEFAULT 'factual',
        strength REAL NOT NULL DEFAULT 0.5,
        poignancy INTEGER NOT NULL DEFAULT 5,
        source_mode TEXT NOT NULL DEFAULT 'dialogue',
        source_ref TEXT DEFAULT '',
        session_id TEXT DEFAULT '',
        persona_id TEXT DEFAULT '',
        tags TEXT DEFAULT '[]',
        entities TEXT DEFAULT '[]',
        cues TEXT DEFAULT '[]',
        evidence TEXT DEFAULT '',
        recalls INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'active',
        occurred_at REAL,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        external_source TEXT DEFAULT '',
        external_id TEXT DEFAULT '',
        import_batch TEXT DEFAULT '',
        subject_id TEXT DEFAULT ''
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_hf_mem_ext ON hf_memories(external_source, external_id) "
    "WHERE external_id != ''",
    "CREATE INDEX IF NOT EXISTS idx_hf_mem_session ON hf_memories(session_id)",
    "CREATE INDEX IF NOT EXISTS idx_hf_mem_status ON hf_memories(status)",
    # 注意：下面这三条审查补的索引**不能写在这里**。
    # SCHEMA 是 connect() 里最先执行的一批语句，而 hf_memories.last_used_at 等列是靠
    # 后面的 ALTER TABLE 才补上的 —— 全新库在这个时点还没有这些列，
    # 索引语句会直接报 `no such column: last_used_at` 把整个建库流程打断。
    # 因此它们统一挪到「列升级完成之后」创建，见 connect() 末尾的 _LATE_INDEXES。
    """
    CREATE TABLE IF NOT EXISTS hf_entities (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        kind TEXT NOT NULL DEFAULT 'topic',
        name TEXT NOT NULL,
        canonical TEXT NOT NULL UNIQUE,
        meta TEXT DEFAULT '{}',
        weight INTEGER NOT NULL DEFAULT 0,
        last_seen REAL NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS hf_edges (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        src_type TEXT NOT NULL,
        src_id TEXT NOT NULL,
        dst_type TEXT NOT NULL,
        dst_id TEXT NOT NULL,
        etype TEXT NOT NULL DEFAULT 'semantic',
        weight REAL NOT NULL DEFAULT 1.0,
        status TEXT NOT NULL DEFAULT 'active',
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_hf_edge_uniq ON hf_edges(src_type, src_id, dst_type, dst_id, etype)",
    """
    CREATE TABLE IF NOT EXISTS hf_favours (
        user_id TEXT PRIMARY KEY,
        name TEXT DEFAULT '',
        session_id TEXT DEFAULT '',
        daily REAL NOT NULL DEFAULT 0,
        lifetime REAL NOT NULL DEFAULT 0,
        zone TEXT NOT NULL DEFAULT 'NEUTRAL',
        evidence TEXT DEFAULT '[]',
        updated_at REAL NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS hf_habits (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        scene TEXT NOT NULL,
        way TEXT NOT NULL,
        cues TEXT DEFAULT '[]',
        weight REAL NOT NULL DEFAULT 0.5,
        uses INTEGER NOT NULL DEFAULT 0,
        scope TEXT DEFAULT '全局',
        enabled INTEGER NOT NULL DEFAULT 1,
        updated_at REAL NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS hf_gate_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts REAL NOT NULL,
        session_id TEXT DEFAULT '',
        user_id TEXT DEFAULT '',
        score REAL NOT NULL DEFAULT 0,
        factors TEXT DEFAULT '{}',
        decision TEXT NOT NULL DEFAULT 'silent',
        threshold REAL NOT NULL DEFAULT 80,
        note TEXT DEFAULT ''
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_hf_gate_ts ON hf_gate_log(ts DESC)",
    # 群友/好友的原话语料：表达自学习与人格自进化的数据源
    """
    CREATE TABLE IF NOT EXISTS hf_corpus (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts REAL NOT NULL,
        session_id TEXT NOT NULL DEFAULT '',
        chat_type TEXT NOT NULL DEFAULT '',
        user_id TEXT NOT NULL DEFAULT '',
        user_name TEXT NOT NULL DEFAULT '',
        persona_id TEXT NOT NULL DEFAULT '',
        text TEXT NOT NULL,
        is_bot INTEGER NOT NULL DEFAULT 0,
        used INTEGER NOT NULL DEFAULT 0
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_hf_corpus_ts ON hf_corpus(ts DESC)",
    "CREATE INDEX IF NOT EXISTS idx_hf_corpus_persona ON hf_corpus(persona_id, is_bot)",
    """
    CREATE TABLE IF NOT EXISTS hf_tokens (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts REAL NOT NULL,
        task TEXT NOT NULL DEFAULT 'chat',
        model TEXT DEFAULT '',
        in_tokens INTEGER NOT NULL DEFAULT 0,
        out_tokens INTEGER NOT NULL DEFAULT 0,
        cached INTEGER NOT NULL DEFAULT 0,
        cost REAL NOT NULL DEFAULT 0
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_hf_tok_ts ON hf_tokens(ts DESC)",
    """
    CREATE TABLE IF NOT EXISTS hf_import_batches (
        batch_id TEXT PRIMARY KEY,
        source TEXT NOT NULL,
        started_at REAL NOT NULL,
        finished_at REAL,
        total INTEGER NOT NULL DEFAULT 0,
        imported INTEGER NOT NULL DEFAULT 0,
        skipped INTEGER NOT NULL DEFAULT 0,
        failed INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'running',
        note TEXT DEFAULT ''
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS hf_config (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        updated_at REAL NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS hf_audit (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts REAL NOT NULL,
        actor TEXT DEFAULT 'panel',
        action TEXT NOT NULL,
        target TEXT DEFAULT '',
        detail TEXT DEFAULT ''
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS hf_person_profile (
        subject_kind TEXT NOT NULL DEFAULT 'user',
        person_id TEXT NOT NULL,
        persona_id TEXT NOT NULL DEFAULT '',
        profile TEXT NOT NULL DEFAULT '{}',
        updated_at REAL NOT NULL,
        mem_count INTEGER NOT NULL DEFAULT 0,
        msg_count INTEGER NOT NULL DEFAULT 0,
        built_by TEXT DEFAULT '',
        PRIMARY KEY (subject_kind, person_id, persona_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_hf_pp_updated ON hf_person_profile(updated_at)",
    """
    CREATE TABLE IF NOT EXISTS hf_hidden_contacts (
        kind TEXT NOT NULL,
        ident TEXT NOT NULL,
        ts REAL NOT NULL,
        PRIMARY KEY (kind, ident)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS hf_recall_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts REAL NOT NULL,
        session_id TEXT DEFAULT '',
        user_id TEXT DEFAULT '',
        notice_type TEXT DEFAULT '',
        message_id TEXT DEFAULT '',
        operator_id TEXT DEFAULT '',
        matched INTEGER NOT NULL DEFAULT 0,
        action TEXT DEFAULT 'log_only',
        detail TEXT DEFAULT '{}'
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS hf_proactive_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts REAL NOT NULL,
        session_id TEXT DEFAULT '',
        user_id TEXT DEFAULT '',
        zone TEXT DEFAULT '',
        idle_min REAL NOT NULL DEFAULT 0,
        score REAL NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'candidate',
        reason TEXT DEFAULT '',
        guard TEXT DEFAULT '{}',
        text TEXT DEFAULT '',
        prompt_source TEXT NOT NULL DEFAULT '',
        memory_ids TEXT NOT NULL DEFAULT ''
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_hf_proactive_sess ON hf_proactive_log(session_id, ts DESC)",
    "CREATE INDEX IF NOT EXISTS idx_hf_proactive_status_ts ON hf_proactive_log(status, ts DESC)",
    """
    CREATE TABLE IF NOT EXISTS hf_persona_proposals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts REAL NOT NULL,
        persona_id TEXT NOT NULL DEFAULT '',
        base TEXT DEFAULT '',
        proposal TEXT DEFAULT '',
        rationale TEXT DEFAULT '',
        status TEXT NOT NULL DEFAULT 'pending',
        in_tokens INTEGER NOT NULL DEFAULT 0,
        out_tokens INTEGER NOT NULL DEFAULT 0,
        decided_at REAL
    )
    """,
    """CREATE TABLE IF NOT EXISTS hf_messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL DEFAULT '',
        role TEXT NOT NULL DEFAULT '',
        content TEXT NOT NULL DEFAULT '',
        sender_id TEXT NOT NULL DEFAULT '',
        sender_name TEXT NOT NULL DEFAULT '',
        platform TEXT NOT NULL DEFAULT '',
        ts REAL NOT NULL DEFAULT 0,
        is_bot INTEGER NOT NULL DEFAULT 0,
        ext_id TEXT UNIQUE
    )""",
    """CREATE INDEX IF NOT EXISTS idx_hf_msg_sess ON hf_messages(session_id, ts)""",
    """CREATE TABLE IF NOT EXISTS hf_vectors (
        memory_id INTEGER NOT NULL,
        gen_key TEXT NOT NULL DEFAULT '',
        model TEXT NOT NULL DEFAULT '',
        dim INTEGER NOT NULL DEFAULT 0,
        vec BLOB,
        created_at REAL NOT NULL DEFAULT 0,
        PRIMARY KEY (memory_id, gen_key)
    )""",
    """CREATE INDEX IF NOT EXISTS idx_hf_vec_gen ON hf_vectors(gen_key)""",
    # 记忆捕获「先落库再总结」：模型超时/空响应时整批对话不会蒸发，可退避重试。
    """CREATE TABLE IF NOT EXISTS hf_capture_pending (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL DEFAULT '',
        persona_id TEXT NOT NULL DEFAULT '',
        user_id TEXT NOT NULL DEFAULT '',
        user_name TEXT NOT NULL DEFAULT '',
        payload TEXT NOT NULL DEFAULT '[]',
        tries INTEGER NOT NULL DEFAULT 0,
        next_try_at REAL NOT NULL DEFAULT 0,
        last_error TEXT DEFAULT '',
        status TEXT NOT NULL DEFAULT 'retry',
        locked_by TEXT NOT NULL DEFAULT '',
        locked_until REAL NOT NULL DEFAULT 0,
        created_at REAL NOT NULL DEFAULT 0
    )""",
    """CREATE TABLE IF NOT EXISTS hf_memory_src (
        memory_id INTEGER NOT NULL,
        seq INTEGER NOT NULL DEFAULT 0,
        role TEXT NOT NULL DEFAULT '',
        speaker TEXT NOT NULL DEFAULT '',
        content TEXT NOT NULL DEFAULT '',
        ts REAL NOT NULL DEFAULT 0,
        PRIMARY KEY (memory_id, seq)
    )""",
    # 记忆 ↔ 消息 id 侧车：撤回精确命中「自动总结」的记忆只能靠它。
    # 自动总结写入的 source_ref 是 `capture:{session}`，永远不等于 message_id；
    # 捕获时把批内每条消息的 id 记在这里，撤回按 message_id 反查（P1-3）。
    # 老记忆没有这批数据，撤回只对新记忆生效 —— 这一点写进 CHANGELOG。
    """CREATE TABLE IF NOT EXISTS hf_memory_msg (
        memory_id INTEGER NOT NULL,
        message_id TEXT NOT NULL DEFAULT '',
        ts REAL NOT NULL DEFAULT 0,
        PRIMARY KEY (memory_id, message_id)
    )""",
    """CREATE INDEX IF NOT EXISTS idx_hf_memmsg_mid ON hf_memory_msg(message_id)""",
    # ---------------- 流水四表（只记不拦；全部 additive，不动任何已有表与列） ----------------
    # 建表纪律：仅 CREATE TABLE/INDEX IF NOT EXISTS，不动任何已有表与列。
    # 保留期：hf_sent_log 30 天 / hf_decision_log 30 天 / hf_expr_use_log 90 天 / hf_send_plan 30 天（终态行），
    # 由 purge_flow_logs() 按 FLOW_RETENTION_DAYS 清理。
    # downgrade 路径（回退只需丢这四张表，旧表零影响）：
    #   DROP TABLE IF EXISTS hf_sent_log; DROP TABLE IF EXISTS hf_decision_log;
    #   DROP TABLE IF EXISTS hf_expr_use_log; DROP TABLE IF EXISTS hf_send_plan;
    # 出站流水（msg_id = 对账锚点）：只存长度与哈希，不存原文。
    # emit_ok 三态：1 = 确认发出；0 = 确认失败；NULL = 未确认（首条经框架管线发出，拿不到发送回执）。
    """
    CREATE TABLE IF NOT EXISTS hf_sent_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        msg_id TEXT NOT NULL DEFAULT '',
        ts REAL NOT NULL,
        session_id TEXT NOT NULL DEFAULT '',
        kind TEXT NOT NULL DEFAULT 'reply',
        seg_index INTEGER NOT NULL DEFAULT 0,
        seg_total INTEGER NOT NULL DEFAULT 1,
        length_chars INTEGER NOT NULL DEFAULT 0,
        content_hash TEXT NOT NULL DEFAULT '',
        delay_ms INTEGER NOT NULL DEFAULT 0,
        emit_ok INTEGER,
        plan_id TEXT NOT NULL DEFAULT '',
        local_id TEXT NOT NULL DEFAULT '',
        trigger TEXT NOT NULL DEFAULT '',
        seed TEXT NOT NULL DEFAULT '',
        exempt INTEGER NOT NULL DEFAULT 0,
        confidence TEXT NOT NULL DEFAULT 'low',
        platform_id TEXT NOT NULL DEFAULT ''
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_hf_sent_ts ON hf_sent_log(ts DESC)",
    "CREATE INDEX IF NOT EXISTS idx_hf_sent_sess ON hf_sent_log(session_id, ts)",
    # 门控决策流水（只记不拦）：因子快照 + reason + 消息文本（截断 ≤500 字 + hash）。时间索引为建表前必修项。
    # eligible：1 = 该消息具备门控拦截条件（非私聊且未被 @）；0 = 豁免场景（私聊/被 @，永远不会被拦）。
    """
    CREATE TABLE IF NOT EXISTS hf_decision_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts REAL NOT NULL,
        event_id TEXT NOT NULL DEFAULT '',
        session_id TEXT NOT NULL DEFAULT '',
        user_id TEXT NOT NULL DEFAULT '',
        eligible INTEGER NOT NULL DEFAULT 1,
        decision TEXT NOT NULL DEFAULT '',
        score REAL NOT NULL DEFAULT 0,
        factors_json TEXT NOT NULL DEFAULT '{}',
        reason TEXT NOT NULL DEFAULT '',
        config_ver TEXT NOT NULL DEFAULT '',
        gate_shadow INTEGER NOT NULL DEFAULT 0,
        msg_text TEXT NOT NULL DEFAULT '',
        text_hash TEXT NOT NULL DEFAULT ''
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_hf_decision_ts ON hf_decision_log(ts DESC)",
    "CREATE INDEX IF NOT EXISTS idx_hf_decision_sess ON hf_decision_log(session_id, ts)",
    # 表达使用流水：只记「注入即命中」；emit_ok/outcome_score 留空（NULL）待出站对账回填，NULL ≠ 没发出。
    """
    CREATE TABLE IF NOT EXISTS hf_expr_use_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        expr_id INTEGER NOT NULL,
        used_at REAL NOT NULL,
        emit_ok INTEGER,
        outcome_score REAL,
        seed TEXT NOT NULL DEFAULT '',
        session_id TEXT NOT NULL DEFAULT ''
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_hf_expr_use ON hf_expr_use_log(expr_id, used_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_hf_expr_use_at ON hf_expr_use_log(used_at)",
    # 发送计划（建表先行，接线待后续）：五态状态机 pending→sending→partial→sent（failed/expired 为终态），
    # 幂等键 = PRIMARY KEY(plan_id, seg_index)；TTL = max(30min, seg_total × output_typing_max × 1.5)，超龄置 expired。
    # content_hash 可复算段文本；payload_text 仅截断 ≤500 字。
    """
    CREATE TABLE IF NOT EXISTS hf_send_plan (
        plan_id TEXT NOT NULL,
        seg_index INTEGER NOT NULL,
        session_id TEXT NOT NULL DEFAULT '',
        target_local TEXT NOT NULL DEFAULT '',
        seg_total INTEGER NOT NULL DEFAULT 1,
        state TEXT NOT NULL DEFAULT 'pending',
        attempts INTEGER NOT NULL DEFAULT 0,
        next_at REAL NOT NULL DEFAULT 0,
        last_error TEXT NOT NULL DEFAULT '',
        confidence TEXT NOT NULL DEFAULT 'low',
        kind TEXT NOT NULL DEFAULT 'reply',
        content_hash TEXT NOT NULL DEFAULT '',
        payload_text TEXT NOT NULL DEFAULT '',
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        PRIMARY KEY (plan_id, seg_index)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_hf_send_plan_state ON hf_send_plan(state, next_at)",
    "CREATE INDEX IF NOT EXISTS idx_hf_send_plan_upd ON hf_send_plan(updated_at)",
    # 配置快照（参数护栏配套）：保存配置前自动存一份，也可在设置页手动命名存一份。
    # 回滚 = 把快照里的键值原样写回 hf_config。保留份数见 CONFIG_SNAPSHOT_KEEP（超出的先删自动快照）。
    # 只存配置键值，不含任何消息/记忆内容；downgrade：DROP TABLE IF EXISTS hf_config_snapshot;
    """
    CREATE TABLE IF NOT EXISTS hf_config_snapshot (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL DEFAULT '',
        ts REAL NOT NULL,
        n_keys INTEGER NOT NULL DEFAULT 0,
        auto INTEGER NOT NULL DEFAULT 0,
        actor TEXT NOT NULL DEFAULT 'panel',
        note TEXT NOT NULL DEFAULT '',
        config_json TEXT NOT NULL DEFAULT '{}'
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_hf_cfg_snap_ts ON hf_config_snapshot(ts DESC)",
    # ---------------- S4 表达重建：hf_expr / 不再学名单 / 学习诊断 / 语料出处侧车 ----------------
    # 建表纪律同上：仅 CREATE TABLE/INDEX IF NOT EXISTS 追加，不动任何已有表与列。
    # 列名口径（拍-1）= 代码真列名 scene/way；无 weight（拍-11 死字段，不迁入）；
    # 无 use_count/last_used 回写列（拍-5：一律从 hf_expr_use_log 查询聚合，防双写漂移）。
    # scope：'学习·{pid}' / '全局' / '手加·{pid}'（pid 空退化 '学习' / '手加'，拍-4）。
    # checked_state：ai_pending / user_ok / user_reject（默认自动通过 → 提取写入即 user_ok，拍-2）；
    # status：active / archived（软删 = 'archived'，拍-14）；enabled：停用位（只关不删）；
    # cooldown_until：冷却按时间（拍-10）。downgrade：DROP TABLE IF EXISTS hf_expr;（旧表零影响）
    """
    CREATE TABLE IF NOT EXISTS hf_expr (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        scene          TEXT NOT NULL,
        way            TEXT NOT NULL,
        cues           TEXT NOT NULL DEFAULT '[]',
        scope          TEXT NOT NULL DEFAULT '全局',
        enabled        INTEGER NOT NULL DEFAULT 1,
        status         TEXT NOT NULL DEFAULT 'active',
        norm_key       TEXT NOT NULL DEFAULT '',
        duplicate_of   INTEGER,
        checked_state  TEXT NOT NULL DEFAULT 'ai_pending',
        ai_reason      TEXT NOT NULL DEFAULT '',
        source_ids     TEXT NOT NULL DEFAULT '[]',
        evidence_snap  TEXT NOT NULL DEFAULT '[]',
        cooldown_until REAL,
        outcome_score  REAL,
        gate_override  TEXT NOT NULL DEFAULT '',
        created_at     REAL NOT NULL,
        updated_at     REAL NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_hf_expr_scope ON hf_expr(scope, enabled)",
    "CREATE INDEX IF NOT EXISTS idx_hf_expr_state ON hf_expr(checked_state, enabled)",
    "CREATE INDEX IF NOT EXISTS idx_hf_expr_norm ON hf_expr(scope, norm_key)",
    "CREATE INDEX IF NOT EXISTS idx_hf_expr_upd ON hf_expr(updated_at DESC)",
    # 「不再学」名单（拍-6 独立表）：删除/驳回都写进来，提取路径先过这张表 → 修「删了还会再学」。
    # key = 当时写的原文（人看的）；norm_key = 规范化键（判重/移出的唯一键，主键）。
    """
    CREATE TABLE IF NOT EXISTS hf_expr_banned (
        key      TEXT NOT NULL DEFAULT '',
        norm_key TEXT PRIMARY KEY,
        reason   TEXT NOT NULL DEFAULT '',
        ts       REAL NOT NULL,
        actor    TEXT NOT NULL DEFAULT 'panel',
        note     TEXT NOT NULL DEFAULT ''
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_hf_expr_banned_ts ON hf_expr_banned(ts DESC)",
    # 学习诊断（S4-4）：每次提取运行一行 —— 自动（_habit_auto_tick）与手动（面板按钮）两条路都写。
    # skipped_reason 枚举见 service.EXPR_SKIP_REASONS（7 值；size_capped 为 reserved 占位）。
    """
    CREATE TABLE IF NOT EXISTS hf_expr_run_log (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        ts             REAL NOT NULL,
        scanned        INTEGER NOT NULL DEFAULT 0,
        saved          INTEGER NOT NULL DEFAULT 0,
        skipped_reason TEXT NOT NULL DEFAULT '',
        model          TEXT NOT NULL DEFAULT '',
        ok             INTEGER NOT NULL DEFAULT 1,
        persona_id     TEXT NOT NULL DEFAULT ''
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_hf_expr_run_ts ON hf_expr_run_log(ts DESC)",
    # 语料出处侧车（E3）：hf_corpus 的「消息 id」。本期纪律禁 ALTER 既有表（拍-15），
    # 所以不往 hf_corpus 加列，改用可 JOIN 的侧车表存 message_id/ext_id —— 出处回查与对树都靠它。
    """
    CREATE TABLE IF NOT EXISTS hf_corpus_ref (
        corpus_id  INTEGER PRIMARY KEY,
        message_id TEXT NOT NULL DEFAULT '',
        ext_id     TEXT NOT NULL DEFAULT '',
        session_id TEXT NOT NULL DEFAULT '',
        ts         REAL NOT NULL DEFAULT 0
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_hf_corpus_ref_mid ON hf_corpus_ref(message_id)",
    # ---------------- S5 黑话：hf_jargon + 命中日志（从零建，拍-18 列清单以 round1-C1 §5 块2 冻结） ----------------
    # 建表纪律同上：仅 CREATE TABLE/INDEX IF NOT EXISTS 追加，不动任何已有表与列。
    # 状态三态（洞③ 人工闸门）：pending 待确认 / confirmed 已确认 / denied 已否定（= 不再推断，留档）。
    # scope（拍-17）：'全局' / '群·{gid}' / '人·{uid}' —— 匹配时过滤**下推 SQL**，索引 (scope, term)。
    # hit_count 口径 = **被观察到的命中次数**（机械匹配日志聚合；不是上游「被再次抽取到」的次数 ——
    # maibot-deepdive 01 §7-A3 再抽取污染教训不照抄）。证据 evidence_json 长期保留（洞①）。
    # 唯一性：(term, scope) 逻辑唯一（应用层判重；上游「同词多行」属缺陷，不照抄）。
    # downgrade：DROP TABLE IF EXISTS hf_jargon_hit; DROP TABLE IF EXISTS hf_jargon;
    """
    CREATE TABLE IF NOT EXISTS hf_jargon (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        term          TEXT NOT NULL,
        meaning       TEXT NOT NULL DEFAULT '',
        evidence_json TEXT NOT NULL DEFAULT '[]',
        status        TEXT NOT NULL DEFAULT 'pending',
        scope         TEXT NOT NULL DEFAULT '全局',
        enabled       INTEGER NOT NULL DEFAULT 1,
        hit_count     INTEGER NOT NULL DEFAULT 0,
        last_hit_at   REAL,
        created_at    REAL NOT NULL,
        updated_at    REAL NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_hf_jargon_term ON hf_jargon(term)",
    "CREATE INDEX IF NOT EXISTS idx_hf_jargon_scope_term ON hf_jargon(scope, term)",
    "CREATE INDEX IF NOT EXISTS idx_hf_jargon_status ON hf_jargon(status, hit_count DESC)",
    # 命中日志（验证物①）：每次入站消息机械匹配命中已知词写一行（term_id / 会话 / 原话 / ts）。
    # 与理解注入共用同一匹配函数（round1-C1 §4「写一次两用」）；injected = 是否同轮进了理解注入。
    # msg 截断 ≤500 字（同 hf_decision_log.msg_text 口径）。
    """
    CREATE TABLE IF NOT EXISTS hf_jargon_hit (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        term_id    INTEGER NOT NULL,
        session_id TEXT NOT NULL DEFAULT '',
        msg        TEXT NOT NULL DEFAULT '',
        ts         REAL NOT NULL,
        injected   INTEGER NOT NULL DEFAULT 0
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_hf_jargon_hit ON hf_jargon_hit(term_id, ts DESC)",

    # ================= v2 记忆（批 A：只建表 + 只写不读） =================
    # 设计见 _refs/记忆系统调研-20261020/v2-完整方案-v1.6.md。
    # 纪律：全部 additive；不删不改任何 v1 表；v2_enabled 默认 false，读路径完全不变。
    """CREATE TABLE IF NOT EXISTS episodes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL DEFAULT '',
        persona_id TEXT NOT NULL DEFAULT '',
        subject_id TEXT NOT NULL DEFAULT '',
        started_at REAL NOT NULL DEFAULT 0,
        ended_at REAL NOT NULL DEFAULT 0,
        raw_json TEXT NOT NULL DEFAULT '[]',
        message_ids TEXT NOT NULL DEFAULT '[]',
        msg_hash TEXT NOT NULL DEFAULT '',
        summary TEXT DEFAULT '',
        extract_state TEXT NOT NULL DEFAULT 'pending',
        attempts INTEGER NOT NULL DEFAULT 0,
        lock_until REAL DEFAULT 0,
        retry_at REAL DEFAULT 0,
        created_at REAL NOT NULL DEFAULT 0
    )""",
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_ep_hash ON episodes(session_id, msg_hash)",
    "CREATE INDEX IF NOT EXISTS idx_ep_extract ON episodes(extract_state, retry_at)",

    """CREATE TABLE IF NOT EXISTS memories (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        legacy_id INTEGER,
        episode_id INTEGER,
        persona_id TEXT NOT NULL DEFAULT '',
        subject_id TEXT NOT NULL DEFAULT '',
        session_id TEXT NOT NULL DEFAULT '',
        kind TEXT NOT NULL DEFAULT 'fact',
        text TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        gist TEXT DEFAULT '',
        occurred_start REAL,
        occurred_end REAL,
        mentioned_at REAL NOT NULL DEFAULT 0,
        time_phrase TEXT DEFAULT '',
        time_granularity TEXT DEFAULT '',
        anchor_memory_id INTEGER,
        anchor_state TEXT DEFAULT '',
        valid_from REAL,
        valid_to REAL,
        is_current INTEGER NOT NULL DEFAULT 1,
        superseded_by INTEGER,
        source_msg_ids TEXT NOT NULL DEFAULT '[]',
        evidence_quote TEXT DEFAULT '',
        evidence_kind TEXT NOT NULL DEFAULT 'verbatim',
        stance TEXT DEFAULT 'unknown',
        origin TEXT NOT NULL DEFAULT 'user_explicit',
        confidence REAL NOT NULL DEFAULT 0.5,
        intimacy INTEGER NOT NULL DEFAULT 0,
        due_at REAL,
        importance INTEGER NOT NULL DEFAULT 3,
        arousal REAL NOT NULL DEFAULT 0.0,
        valence REAL NOT NULL DEFAULT 0.0,
        access_count INTEGER NOT NULL DEFAULT 0,
        last_access_at REAL,
        used_count INTEGER NOT NULL DEFAULT 0,
        mention_count INTEGER NOT NULL DEFAULT 0,
        last_mention_at REAL,
        muted INTEGER NOT NULL DEFAULT 0,
        proof_count INTEGER NOT NULL DEFAULT 0,
        source_ids TEXT NOT NULL DEFAULT '[]',
        consolidated INTEGER NOT NULL DEFAULT 0,
        migrated INTEGER NOT NULL DEFAULT 0,
        needs_review INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'active',
        tags TEXT NOT NULL DEFAULT '[]',
        entities TEXT NOT NULL DEFAULT '[]',
        created_at REAL NOT NULL DEFAULT 0,
        updated_at REAL NOT NULL DEFAULT 0
    )""",
    """CREATE UNIQUE INDEX IF NOT EXISTS ux_mem_hash ON memories(subject_id, content_hash)
        WHERE status='active' AND is_current=1""",
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_mem_legacy ON memories(legacy_id) WHERE legacy_id IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_mem_recall ON memories(persona_id, status, is_current, mentioned_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_mem_time ON memories(status, is_current, occurred_start)",
    "CREATE INDEX IF NOT EXISTS idx_mem_consol ON memories(consolidated, importance DESC) WHERE consolidated=0",
    "CREATE INDEX IF NOT EXISTS idx_mem_due ON memories(due_at) WHERE kind='commitment' AND status='active'",

    # FTS 必须在 memories/episodes 之后建（external content 要求宿主表存在）
    """CREATE VIRTUAL TABLE IF NOT EXISTS fts_mem USING fts5(
        text, gist, entities, content='memories', content_rowid='id', tokenize='trigram')""",
    """CREATE VIRTUAL TABLE IF NOT EXISTS fts_ep USING fts5(
        raw_json, content='episodes', content_rowid='id', tokenize='trigram')""",
    """CREATE TRIGGER IF NOT EXISTS trg_mem_ai AFTER INSERT ON memories BEGIN
        INSERT INTO fts_mem(rowid,text,gist,entities) VALUES (new.id,new.text,new.gist,new.entities);
    END""",
    """CREATE TRIGGER IF NOT EXISTS trg_mem_ad AFTER DELETE ON memories BEGIN
        INSERT INTO fts_mem(fts_mem,rowid,text,gist,entities) VALUES('delete',old.id,old.text,old.gist,old.entities);
    END""",
    """CREATE TRIGGER IF NOT EXISTS trg_mem_au AFTER UPDATE OF text,gist,entities ON memories BEGIN
        INSERT INTO fts_mem(fts_mem,rowid,text,gist,entities) VALUES('delete',old.id,old.text,old.gist,old.entities);
        INSERT INTO fts_mem(rowid,text,gist,entities) VALUES (new.id,new.text,new.gist,new.entities);
    END""",
    """CREATE TRIGGER IF NOT EXISTS trg_ep_ai AFTER INSERT ON episodes BEGIN
        INSERT INTO fts_ep(rowid,raw_json) VALUES (new.id,new.raw_json);
    END""",
    """CREATE TRIGGER IF NOT EXISTS trg_ep_ad AFTER DELETE ON episodes BEGIN
        INSERT INTO fts_ep(fts_ep,rowid,raw_json) VALUES('delete',old.id,old.raw_json);
    END""",
    """CREATE TRIGGER IF NOT EXISTS trg_ep_au AFTER UPDATE OF raw_json ON episodes BEGIN
        INSERT INTO fts_ep(fts_ep,rowid,raw_json) VALUES('delete',old.id,old.raw_json);
        INSERT INTO fts_ep(rowid,raw_json) VALUES (new.id,new.raw_json);
    END""",

    """CREATE TABLE IF NOT EXISTS mem_bigram (
        gram TEXT NOT NULL, memory_id INTEGER NOT NULL,
        PRIMARY KEY (gram, memory_id)
    ) WITHOUT ROWID""",

    """CREATE TABLE IF NOT EXISTS mem_vectors (
        owner_type TEXT NOT NULL, owner_id INTEGER NOT NULL, gen_key TEXT NOT NULL,
        model TEXT NOT NULL DEFAULT '', dim INTEGER NOT NULL DEFAULT 0, vec BLOB NOT NULL,
        PRIMARY KEY (owner_type, owner_id, gen_key)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_memvec_gen ON mem_vectors(gen_key, owner_id)",

    """CREATE TABLE IF NOT EXISTS access_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        turn_id TEXT NOT NULL DEFAULT '',
        session_id TEXT NOT NULL DEFAULT '',
        subject_id TEXT DEFAULT '',
        memory_id INTEGER NOT NULL,
        ts REAL NOT NULL DEFAULT 0,
        used_in_reply INTEGER NOT NULL DEFAULT 0,
        shown INTEGER NOT NULL DEFAULT 0,
        channel TEXT NOT NULL DEFAULT 'recall'
    )""",
    "CREATE INDEX IF NOT EXISTS idx_acc_turn ON access_log(turn_id)",
    "CREATE INDEX IF NOT EXISTS idx_acc_mem ON access_log(memory_id, ts, channel)",

    """CREATE TABLE IF NOT EXISTS mem_trace (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts REAL, session_id TEXT, zone TEXT, query TEXT,
        memory_id INTEGER, rank INTEGER,
        rrf_norm REAL, recency REAL, importance REAL, used REAL, mention REAL, entity REAL,
        final REAL, injected INTEGER, drop_reason TEXT DEFAULT '',
        rendered TEXT DEFAULT '', channel TEXT DEFAULT 'recall'
    )""",
    "CREATE INDEX IF NOT EXISTS idx_trace_ts ON mem_trace(ts DESC)",

    """CREATE TABLE IF NOT EXISTS recall_event (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts REAL, session_id TEXT, query TEXT,
        cand_lex INTEGER, cand_vec INTEGER, cand_ent INTEGER, cand_time INTEGER, cand_ep INTEGER,
        hit INTEGER, injected INTEGER, ms REAL
    )""",

    """CREATE TABLE IF NOT EXISTS quarantine (
        memory_id INTEGER PRIMARY KEY,
        reason TEXT,
        state TEXT NOT NULL DEFAULT 'pending',
        reviewed_at REAL, note TEXT, created_at REAL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_quar_state ON quarantine(state, created_at)",

    """CREATE TABLE IF NOT EXISTS consolidation_run (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        started_at REAL, finished_at REAL, batch INTEGER,
        created INTEGER, merged INTEGER, superseded INTEGER, error TEXT
    )""",

    """CREATE TABLE IF NOT EXISTS mem_write_ledger (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        new_id INTEGER, legacy_id INTEGER,
        old_ok INTEGER DEFAULT 0, new_ok INTEGER DEFAULT 0,
        err TEXT DEFAULT '', ts REAL
    )""",

    """CREATE TABLE IF NOT EXISTS person_address (
        person_id TEXT NOT NULL, persona_id TEXT NOT NULL DEFAULT '',
        term TEXT NOT NULL, priority INTEGER DEFAULT 0, zone_floor TEXT DEFAULT '',
        dropped_at REAL, created_at REAL,
        PRIMARY KEY (person_id, persona_id, term)
    )""",

    """CREATE TABLE IF NOT EXISTS anchor_queue (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        memory_id INTEGER NOT NULL, created_at REAL, resolved_at REAL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_anchor_open ON anchor_queue(resolved_at) WHERE resolved_at IS NULL",

    # ================= 语言学习（MVP：原话样本 + 词表） =================
    # 设计见 _refs/语言学习蒸馏-20261020/方案-v1.3-MVP.md。
    # 纪律：不删不改 hf_expr；旧链路原样保留，靠 expression_style_inject_mode 切换。
    """CREATE TABLE IF NOT EXISTS hf_style_samples (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        persona_id    TEXT NOT NULL DEFAULT '',
        kind          TEXT NOT NULL DEFAULT 'bot',      -- bot | human
        register      TEXT NOT NULL DEFAULT 'private',  -- group | private
        trigger_text  TEXT NOT NULL DEFAULT '',         -- human 也存（否则去重压成一行）
        reply_text    TEXT NOT NULL DEFAULT '',         -- 单气泡 canon 文本
        structure_json TEXT NOT NULL DEFAULT '{}',
        norm_key      TEXT NOT NULL DEFAULT '',
        source_ids    TEXT NOT NULL DEFAULT '[]',
        sensitivity   TEXT NOT NULL DEFAULT 'normal',   -- normal | intimate | pii
        shown         INTEGER NOT NULL DEFAULT 0,
        last_shown_at REAL NOT NULL DEFAULT 0,
        status        TEXT NOT NULL DEFAULT 'active',   -- active | retired | archived
        created_at    REAL NOT NULL DEFAULT 0,
        updated_at    REAL NOT NULL DEFAULT 0
    )""",
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_style_sample ON hf_style_samples(persona_id, kind, norm_key)",
    "CREATE INDEX IF NOT EXISTS idx_style_pick ON hf_style_samples(persona_id, status, kind, register, last_shown_at)",

    """CREATE TABLE IF NOT EXISTS hf_style_terms (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        persona_id    TEXT NOT NULL DEFAULT '',
        term          TEXT NOT NULL,
        term_norm     TEXT NOT NULL DEFAULT '',
        kind          TEXT NOT NULL DEFAULT 'tic',      -- tic | slang | address
        meaning       TEXT NOT NULL DEFAULT '',
        usage         TEXT NOT NULL DEFAULT '',
        scope         TEXT NOT NULL DEFAULT '全局',
        enabled       INTEGER NOT NULL DEFAULT 1,
        status        TEXT NOT NULL DEFAULT 'confirmed',-- pending | confirmed | denied
        hits          INTEGER NOT NULL DEFAULT 0,
        evidence_json TEXT NOT NULL DEFAULT '[]',
        source_ids    TEXT NOT NULL DEFAULT '[]',
        created_at    REAL NOT NULL DEFAULT 0,
        updated_at    REAL NOT NULL DEFAULT 0
    )""",
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_style_term ON hf_style_terms(persona_id, term_norm, scope)",
    "CREATE INDEX IF NOT EXISTS idx_style_term_match ON hf_style_terms(scope, enabled, status)",

    # ================= 语言学习 v2：回复方式 / 原话（带记忆出处） =================
    # 设计见 int/data/语言学习v2-设计稿-20261021/设计稿说明.md（v7）。
    # 口径：一种回复方式 = 一个组件；组件里存逐字原话，每条原话挂「来自哪条记忆」；
    # 学习输入 = hf_memories.evidence 里的原对话（用户：… / 助手：…），不靠模型复述。
    """CREATE TABLE IF NOT EXISTS hf_style_styles (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        persona_id  TEXT NOT NULL DEFAULT '',
        name        TEXT NOT NULL,
        status      TEXT NOT NULL DEFAULT 'active',   -- active | paused
        hits        INTEGER NOT NULL DEFAULT 0,
        corrections INTEGER NOT NULL DEFAULT 0,
        created_at  REAL NOT NULL DEFAULT 0,
        updated_at  REAL NOT NULL DEFAULT 0
    )""",
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_style_style ON hf_style_styles(persona_id, name)",

    """CREATE TABLE IF NOT EXISTS hf_style_quotes (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        style_id    INTEGER NOT NULL DEFAULT 0,
        persona_id  TEXT NOT NULL DEFAULT '',
        quote       TEXT NOT NULL,
        trigger_text TEXT NOT NULL DEFAULT '',
        memory_id   INTEGER NOT NULL DEFAULT 0,
        session_id  TEXT NOT NULL DEFAULT '',
        user_id     TEXT NOT NULL DEFAULT '',
        user_name   TEXT NOT NULL DEFAULT '',
        register    TEXT NOT NULL DEFAULT 'private',
        hits        INTEGER NOT NULL DEFAULT 0,
        corrections INTEGER NOT NULL DEFAULT 0,
        status      TEXT NOT NULL DEFAULT 'active',   -- active | dropped（不准的丢弃）
        last_used_at REAL NOT NULL DEFAULT 0,
        created_at  REAL NOT NULL DEFAULT 0,
        updated_at  REAL NOT NULL DEFAULT 0
    )""",
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_style_quote ON hf_style_quotes(memory_id, quote)",
    "CREATE INDEX IF NOT EXISTS idx_style_quote_pick ON hf_style_quotes(persona_id, status, style_id)",

)


# 流水表保留期（天）：出站 30 / 决策 30 / 表达使用 90 / 发送计划 30（只清终态行）。
FLOW_RETENTION_DAYS = {
    "hf_sent_log": 30,
    "hf_decision_log": 30,
    "hf_expr_use_log": 90,
    "hf_send_plan": 30,
}

# 配置快照保留份数：超出后先删最老的自动快照（手动命名的排最后）。
CONFIG_SNAPSHOT_KEEP = 50

# ---------------- S4 表达（hf_expr）运行时口径 ----------------
# 拍-5：use_count/last_used 一律查询聚合（无回写列），索引靠 hf_expr_use_log 既有的 idx_hf_expr_use。
# 拍-10：冷却按时间 —— 注入后同一条进 EXPR_COOLDOWN_SECONDS 冷却窗；「同表达 24h ≤ 2」用流水窗口计数。
EXPR_COOLDOWN_SECONDS = 1800
EXPR_WINDOW_SECONDS = 86400
EXPR_WINDOW_MAX_INJECT = 2
# 备份里允许整表导出的表名白名单（只给「只清表达 / 清全部学习」用；表名不来自外部输入）。
EXPR_BACKUP_TABLES: tuple[str, ...] = ("hf_expr", "hf_habits", "hf_expr_banned")
LEARNING_BACKUP_TABLES: tuple[str, ...] = ("hf_memories", "hf_edges", "hf_entities", "hf_expr", "hf_habits")
BACKUP_KEEP = 5          # 拍-14：备份落 <数据目录>/backups/，保留最近 5 份

# ---------------- S5 黑话（hf_jargon）运行时口径 ----------------
# 机械候选门槛（拍-16：跨 ≥2 会话、出现 ≥4 次）与推断/试解读配额走 DEFAULT_CONFIG 的
# jargon_* 键（参数中心可调）；这里只放不暴露的工程常量。
JARGON_STATUSES: tuple[str, ...] = ("pending", "confirmed", "denied")
JARGON_MATCH_LIMIT = 10            # 单轮理解注入最多几条（上游 MAX_JARGON_REFERENCE_MATCHES=10）
JARGON_CANDIDATE_BATCH = 20        # 单次推断最多送几个候选（控 prompt 长度/成本）
JARGON_TRY_MAX_TOKENS = 200        # 试解读单次输出上限（拍-19：单次 ≤1 条短请求、输出 ≤200 tokens）
JARGON_TERM_MAX_CHARS = 40         # 词条最长（写入校验：超了拒绝，不静默截断）
JARGON_MEANING_MAX_CHARS = 300     # 释义最长（同上）
JARGON_HIT_MSG_MAX_CHARS = 500     # 命中日志原话截断（同 hf_decision_log.msg_text 口径）
JARGON_CORPUS_SCAN = 800           # 机械候选只扫最近 N 条入站语料（性能闸，够单群日常量）
# 规范化键要丢掉的字符：中英常见标点 + 各种引号（空白由 isspace 单独判，避免正则里写引号）。
_EXPR_NORM_DROP = set(
    "，。！？、,.!?~～…·:：;；()（）[]【】<>《》-—_/|"
    + chr(34) + chr(39) + chr(92) + chr(0x201C) + chr(0x201D) + chr(0x2018) + chr(0x2019)
)


def expr_norm_key(scene: Any, way: Any) -> str:
    """表达规范化键（F2 去重/合并键 +「不再学」名单键）：小写 + 去空白与常见标点。

    只留「说法」骨架，所以「嘿嘿，谢谢夸奖～」与「嘿嘿谢谢夸奖」是同一个键。
    scene 不参与（场景是分类，说法才是可复用的那条）。
    """
    s = "".join(ch for ch in str(way or "") if not ch.isspace() and ch not in _EXPR_NORM_DROP)
    return s.lower()[:120]



def _query_memory_id(q: str) -> int | None:
    """把搜索框里的「记忆 ID」写法归一成 hf_memories.id；不是 ID 写法就回 None。

    面板「记忆库」的摘要列显示的是 `m{id}`（前端自己拼的前缀），关系/实体那边还会给出
    `e{id}`，用户会很自然地把整串粘进搜索框。老 SQL 是 `CAST(id AS TEXT)=?` 拿**原始**
    关键词去比：CAST(865)='865'，'m865' 永远不等于它，于是搜 ID 恒为 0 条。
    只在「剥掉前缀后是纯数字」时才认 ID，避免把 m 开头的普通中英文词（例如 maybe）当 ID 去精确匹配。
    """
    s = (q or "").strip()
    if not s:
        return None
    m = re.fullmatch(r"[mMeE]?([0-9]+)", s)
    if not m:
        return None
    try:
        mid = int(m.group(1))
    except ValueError:                      # 纯 [0-9]+ 理论上不会失败，兜一层免得炸查询
        return None
    return mid if 0 < mid <= 9223372036854775807 else None


def now() -> float:
    return time.time()


def _loads(raw: Any, default: Any = None) -> Any:
    """JSON 字段容错解析：旧库里可能存着半截 JSON，读不出来就给默认值而不是抛异常。"""
    if raw is None or raw == "":
        return default if default is not None else []
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default if default is not None else []


def session_platform(session_id: str) -> str:
    """会话前缀 = 平台实例 id（如 testbot / webchat）。"""
    return str(session_id or "").split(":", 1)[0].strip().lower()


def is_ignored_session(session_id: str, ignore_platforms: list[str] | tuple[str, ...] | None = None) -> bool:
    """该会话的平台是否在排除名单里（名单 = 配置里的 person_exclude_platforms）。

    不再写死 webchat/webui：ChatUI（WebChat）现在能像 QQ 一样被记录，排不排除由配置决定。
    """
    head = session_platform(session_id)
    if not head:
        return False
    ignore: set[str] = set()
    for x in (ignore_platforms or ()):
        x = str(x).strip().lower()
        if x:
            ignore.add(x)
    return head in ignore


def session_kind(session_id: str) -> tuple[str, str]:
    """会话字符串 platform:MessageType:id → ("group"|"private"|"other", id)。

    WebChat 尾段形如 `webchat!<用户名>!<会话id>`，用户名才是「人」→ 归一化成用户名，
    免得面板/画像里出现 `webchat!<用户名>!uuid` 这种丑 id（用户明确要求显示账户名）。
    """
    parts = str(session_id or "").split(":")
    if len(parts) >= 3:
        mtype, ident = parts[-2].lower(), parts[-1]
        # WebChat 尾段两种形态都要认：
        #   · `webchat!<用户名>!<uuid>`（旧数据 / 未归一时）
        #   · `webchat!<用户名>`（session_norm 归一后的标准形态，只有一个感叹号）
        # 判据写成「≥1 个感叹号 + 取第 2 段」，别再要求 ≥2 —— 不然归一后的 id 反而漏网，
        # 图上会冒出「用户 webchat!<用户名>」这种丑节点（实测踩过）。
        if ident.lower().startswith("webchat!") and ident.count("!") >= 1:
            seg = ident.split("!")
            ident = (seg[1].strip() if len(seg) > 1 and seg[1].strip() else ident)
        if "group" in mtype:
            return "group", ident
        if "friend" in mtype or "private" in mtype:
            return "private", ident
        return "other", ident
    return "other", str(session_id or "")


def session_norm(session_id: str) -> str:
    """把一个会话 id 归一成插件内部统一形态（目前只处理 WebChat 的平台差异）。

    为什么必须归一：ChatUI 的 umo 尾段是 `webchat!<用户名>!<uuid>`，而那个 uuid
    **每开一个新对话就换一个** —— 同一个人被拆成一条条互不相干的会话：
      · 记忆捕获按会话攒轮次，永远攒不够 → ChatUI 聊再多也存不下记忆（实测 5 段对话 13 条消息 = 3 轮）；
      · 库里/面板上全是 `webchat!<用户名>!<uuid>` 这种丑 id，「谁在说话」也没法对上人。
    归一把尾段收成 `webchat!<用户名>`，与 QQ 私聊「一个人 = 一条会话」的口径对齐。
    """
    s = str(session_id or "")
    parts = s.split(":")
    if len(parts) >= 2:
        tail = parts[-1]
        if tail.lower().startswith("webchat!") and tail.count("!") >= 2:
            name = tail.split("!")[1].strip()
            if name:
                parts[-1] = "webchat!" + name
                return ":".join(parts)
    return s


class Store:
    """异步 SQLite 存储。所有写操作都更新 updated_at，便于面板展示。"""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)
        self._db: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA foreign_keys=ON")
        # 单连接会被插件自身多个协程与面板请求并发使用：没有 busy_timeout 时，
        # 一旦有别的连接持锁就立刻抛 SQLITE_BUSY（表现为随机「写库失败」）。
        # synchronous=NORMAL 是 WAL 下的官方推荐档：掉电最多丢最近一个事务，换取大幅减少 fsync。
        await self._db.execute("PRAGMA busy_timeout=5000")
        await self._db.execute("PRAGMA synchronous=NORMAL")
        for stmt in SCHEMA:
            await self._db.execute(stmt)
        # 门控改判（S1）：决策流水补三列 —— 门控页行内「该回 / 判定正确」的落点。
        # PRAGMA 探测 + ALTER，幂等：老库升级与新建库走同一条路（新库此时还没有这三列）。
        try:
            dl_cols = {str(r[1]) for r in await (await self._db.execute("PRAGMA table_info(hf_decision_log)")).fetchall()}
            for col, ddl in (("user_verdict", "TEXT NOT NULL DEFAULT ''"),
                             ("verdict_at", "REAL NOT NULL DEFAULT 0"),
                             ("verdict_actor", "TEXT NOT NULL DEFAULT ''")):
                if dl_cols and col not in dl_cols:
                    await self._db.execute(f"ALTER TABLE hf_decision_log ADD COLUMN {col} {ddl}")
            await self._db.commit()
        except Exception as exc:                            # noqa: BLE001 列升级失败不该挡住启动
            logger.warning(f"[StarHeart] 决策流水改判列升级跳过：{exc}")
        # 捕获队列升级（库结构 3）：status / locked_by / locked_until 三列。
        # 为什么：① 三态 outcome 要区分「待重试」和「已停摆（parked）」；② capture_take 原来只
        # SELECT 不占位，同一批超过一个 tick（120s）会被第二个 worker 再总结一遍 —— 重复记忆 + 重复烧 token。
        # 纯 additive：PRAGMA 探测 + ALTER，老库升级与新建库走同一条路（新库此时已带这三列）。
        try:
            cp_cols = {str(r[1]) for r in await (await self._db.execute(
                "PRAGMA table_info(hf_capture_pending)")).fetchall()}
            for col, ddl in (("status", "TEXT NOT NULL DEFAULT 'retry'"),
                             ("locked_by", "TEXT NOT NULL DEFAULT ''"),
                             ("locked_until", "REAL NOT NULL DEFAULT 0")):
                if cp_cols and col not in cp_cols:
                    await self._db.execute(f"ALTER TABLE hf_capture_pending ADD COLUMN {col} {ddl}")
            # 老库里的行都是「待处理」：默认值已对上；这里只防呆空值，不猜测语义。
            await self._db.execute(
                "UPDATE hf_capture_pending SET status='retry' WHERE COALESCE(status,'')=''")
            await self._db.commit()
        except Exception as exc:                            # noqa: BLE001 列升级失败不该挡住启动
            logger.warning(f"[StarHeart] 捕获队列列升级跳过：{exc}")
        # 向量列（旧库平滑升级）
        cols = [r[1] for r in await (await self._db.execute("PRAGMA table_info(hf_memories)")).fetchall()]
        if "embedding" not in cols:
            await self._db.execute("ALTER TABLE hf_memories ADD COLUMN embedding BLOB")
        if "embedding_model" not in cols:
            await self._db.execute("ALTER TABLE hf_memories ADD COLUMN embedding_model TEXT DEFAULT ''")
        # 不截断的原文（旧库 evidence 被截到 250 字，原文另存一列，不动老数据）
        if "source_text" not in cols:
            await self._db.execute("ALTER TABLE hf_memories ADD COLUMN source_text TEXT DEFAULT ''")
        if "archived" not in cols:
            await self._db.execute("ALTER TABLE hf_memories ADD COLUMN archived INTEGER NOT NULL DEFAULT 0")
        # 最近被提及时间：遗忘周期靠它计时，被提及就刷新（召回/注入都会写）
        if "last_used_at" not in cols:
            await self._db.execute("ALTER TABLE hf_memories ADD COLUMN last_used_at REAL NOT NULL DEFAULT 0")
        if "forget_stage" not in cols:
            await self._db.execute("ALTER TABLE hf_memories ADD COLUMN forget_stage TEXT NOT NULL DEFAULT ''")
        # 记忆归属主体（谁的事）：同人格下多个私聊时，A 的记忆不该原样注入给 B。
        # 旧库一律空串 = 未标注，召回时按「不惩罚」处理（不做猜测回填）。
        if "subject_id" not in cols:
            await self._db.execute("ALTER TABLE hf_memories ADD COLUMN subject_id TEXT DEFAULT ''")
        # 「别再提」冷处理：muted=1 的记忆不进召回、不进主动池，但保留在库里可随时恢复。
        # 为什么要这一列：用户对某条旧记忆说「我从没提过这个」时，除了删掉没有别的动作 ——
        # 删就丢了，留着又会一直被注入。这是负反馈的唯一落点。
        if "muted" not in cols:
            await self._db.execute("ALTER TABLE hf_memories ADD COLUMN muted INTEGER NOT NULL DEFAULT 0")
        # 主动流水升级：记下这条开口当时的「提示词来源（astrbot=完整 / persona=降级只吃人格）」与
        # 「参考记忆编号」——以前只能翻 completion 原文才知道有没有降级，出问题无从核对。
        pl_cols = [r[1] for r in await (await self._db.execute("PRAGMA table_info(hf_proactive_log)")).fetchall()]
        if "prompt_source" not in pl_cols:
            await self._db.execute("ALTER TABLE hf_proactive_log ADD COLUMN prompt_source TEXT NOT NULL DEFAULT ''")
        if "memory_ids" not in pl_cols:
            await self._db.execute("ALTER TABLE hf_proactive_log ADD COLUMN memory_ids TEXT NOT NULL DEFAULT ''")
        # 画像表升级：加「主体类型 user/group」与「人格」两列，主键从 person_id 单列变成三列复合。
        # 为什么要重建：① 群聊画像（L1）原来无处安放；② 两个人格共用一份画像 —— 违反人格隔离硬约束。
        # 旧数据一律按「人物 + 未归属人格」迁入，随后由 repair_profile_personas() 按记忆归属回填人格。
        pp_cols = [r[1] for r in await (await self._db.execute("PRAGMA table_info(hf_person_profile)")).fetchall()]
        if pp_cols and ("subject_kind" not in pp_cols or "persona_id" not in pp_cols):
            # 起手先清掉历史残留 _new（半迁移态会把脏行带进新表 —— 审查实证）
            await self._db.execute("DROP TABLE IF EXISTS hf_person_profile_new")
            await self._db.execute(
                "CREATE TABLE IF NOT EXISTS hf_person_profile_new ("
                " subject_kind TEXT NOT NULL DEFAULT 'user', person_id TEXT NOT NULL,"
                " persona_id TEXT NOT NULL DEFAULT '', profile TEXT NOT NULL DEFAULT '{}',"
                " updated_at REAL NOT NULL, mem_count INTEGER NOT NULL DEFAULT 0,"
                " msg_count INTEGER NOT NULL DEFAULT 0, built_by TEXT DEFAULT '',"
                " PRIMARY KEY (subject_kind, person_id, persona_id))")
            await self._db.execute(
                "INSERT OR IGNORE INTO hf_person_profile_new"
                " (subject_kind, person_id, persona_id, profile, updated_at, mem_count, msg_count, built_by)"
                " SELECT 'user', person_id, '', profile, updated_at, mem_count, msg_count, built_by"
                " FROM hf_person_profile")
            # 安全网：备份旧表 → 行数校验 → 重建索引 → 丢行可见告警（审查要求四条）
            before = int((await (await self._db.execute("SELECT COUNT(*) FROM hf_person_profile")).fetchone())[0] or 0)
            await self._db.execute("DROP TABLE IF EXISTS hf_person_profile_backup")
            await self._db.execute("CREATE TABLE hf_person_profile_backup AS SELECT * FROM hf_person_profile")
            await self._db.execute("DROP TABLE hf_person_profile")
            await self._db.execute("ALTER TABLE hf_person_profile_new RENAME TO hf_person_profile")
            await self._db.execute("CREATE INDEX IF NOT EXISTS idx_hf_pp_updated ON hf_person_profile(updated_at)")
            after = int((await (await self._db.execute("SELECT COUNT(*) FROM hf_person_profile")).fetchone())[0] or 0)
            if after < before:
                logger.warning(f"[StarHeart] 画像表迁移丢行：{before} → {after}（多为 person_id 为 NULL 的旧行）；旧数据完整备份在 hf_person_profile_backup")
            else:
                logger.info(f"[StarHeart] 画像表已升级：+subject_kind+persona_id，{before} 行全数迁入（备份 hf_person_profile_backup）")
        await self._db.commit()
        # ---- 列升级已完成，这里才建那几条依赖新列的索引 ----
        for stmt in _LATE_INDEXES:
            try:
                await self._db.execute(stmt)
            except Exception as exc:                        # noqa: BLE001 老库异常不该挡住启动
                logger.warning(f"[StarHeart] 建索引跳过（{stmt.split(' ON ')[-1]}）：{exc}")
        await self._db.commit()
        # ---- 迁移版本戳 ----
        # 记录 PRAGMA user_version：新库直接盖当前档，旧库原地升档。
        # 有了它，以后的迁移可以写「只在 < N 时执行」，不必再靠逐列探测。
        try:
            row = await (await self._db.execute("PRAGMA user_version")).fetchone()
            cur_ver = int((row[0] if row else 0) or 0)
        except Exception:                                   # noqa: BLE001
            cur_ver = 0
        if cur_ver < SCHEMA_VERSION:
            # v5 数据迁移：强度上限由重要度（poignancy）决定。
            # 旧实现「注入一次 +0.06」把测试句一路推到 0.99（真机 #906 被注入 62 次），
            # 于是一句「早上八点半到岗」比真实偏好还重。这里把存量一次性夹回上限：
            # cap = 0.55 + 0.04 × poignancy（poi 3 → 0.67，poi 10 → 0.95）。
            if cur_ver < 5:
                try:
                    cur = await self._db.execute(
                        "UPDATE hf_memories SET strength = MIN(strength, 0.55 + 0.04 * COALESCE(poignancy, 5)) "
                        "WHERE strength > 0.55 + 0.04 * COALESCE(poignancy, 5)")
                    await self._db.commit()
                    clamped = int(cur.rowcount or 0)
                    if clamped:
                        logger.info(f"[StarHeart] 强度上限迁移：{clamped} 条按重要度夹回（0.55+0.04×poignancy）")
                        await self.audit("memory.strength_cap", f"clamped={clamped}")
                except Exception as exc:                    # noqa: BLE001 迁移失败不该挡住启动
                    logger.warning(f"[StarHeart] 强度上限迁移跳过：{exc}")
            await self._db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            await self._db.commit()
            if cur_ver:
                logger.info(f"[StarHeart] 库结构版本 {cur_ver} → {SCHEMA_VERSION}")

        # ---- S4 表达回填（幂等；真机存量 = 0 → no-op）：旧表 hf_habits 只读保留、不删 ----
        try:
            await self.backfill_expr_from_habits()
        except Exception as exc:                            # noqa: BLE001 回填失败不该挡住启动
            logger.warning(f"[StarHeart] 表达回填跳过：{exc}")
    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("store not connected")
        return self._db

    # ---------------- 记忆 ----------------
    async def insert_memory(self, mem: dict[str, Any]) -> int | None:
        """写入一条记忆；external_id 冲突时返回 None（表示已导入过）。"""
        ts = now()
        try:
            cur = await self.db.execute(
                """
                INSERT INTO hf_memories
                    (summary, content, atom_type, strength, poignancy, source_mode, source_ref,
                     session_id, persona_id, tags, entities, cues, evidence, recalls, status,
                     occurred_at, created_at, updated_at, external_source, external_id, import_batch,
                     last_used_at, strength_updated_at, subject_id)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    mem.get("summary", ""),
                    mem.get("content", ""),
                    mem.get("atom_type", "factual"),
                    float(mem.get("strength", 0.5)),
                    int(mem.get("poignancy", 5)),
                    mem.get("source_mode", "dialogue"),
                    mem.get("source_ref", ""),
                    mem.get("session_id", ""),
                    mem.get("persona_id", ""),
                    json.dumps(mem.get("tags", []), ensure_ascii=False),
                    json.dumps(mem.get("entities", []), ensure_ascii=False),
                    json.dumps(mem.get("cues", []), ensure_ascii=False),
                    mem.get("evidence", ""),
                    int(mem.get("recalls", 0)),
                    mem.get("status", "active"),
                    mem.get("occurred_at") or ts,
                    ts,
                    ts,
                    mem.get("external_source", ""),
                    mem.get("external_id", ""),
                    mem.get("import_batch", ""),
                    # 入库即视为「刚被提起」：导入的历史记忆若按原始发生日期算龄，
                    # 第一个生命周期 tick 就会被归档、再一轮被遗忘（P0-13）。
                    float(mem.get("last_used_at") or ts),
                    # 衰减基线必须入库时就写好：留 0 会让这条记忆「永不衰减」，
                    # 直到下一次启动 repair_memory_timebase 才补上（真机每次启动 base_filled>0）。
                    float(mem.get("strength_updated_at") or ts),
                    str(mem.get("subject_id") or ""),
                ),
            )
            await self.db.commit()
            return int(cur.lastrowid or 0)
        except aiosqlite.IntegrityError:
            return None

    # ================= v2 记忆（批 A：只写不读） =================

    @staticmethod
    def resolve_mid(value: Any) -> tuple[str, int] | None:
        """把 'm12' / 12 / 'v2m12' 解析成 (命名空间, id)。

        命名空间：legacy = hf_memories；v2 = memories。
        返回 None 表示非法。**调用方必须按 ns 分流**，不能只取 id
        （'v2m12' 取 12 会打到 hf_memories 第 12 行 —— 删错行）。
        """
        raw = str(value or "").strip()
        if not raw:
            return None
        if raw.startswith("v2m"):
            try:
                return ("v2", int(raw[3:]))
            except (TypeError, ValueError):
                return None
        if raw.startswith("m"):
            raw = raw[1:]
        try:
            return ("legacy", int(raw))
        except (TypeError, ValueError):
            return None

    @staticmethod
    def norm_text(text: Any) -> str:
        """content_hash 归一化：小写、去空白、去标点、全半角归一。"""
        import unicodedata

        t = unicodedata.normalize("NFKC", str(text or "")).lower()
        t = re.sub(r"[\s\u3000]+", "", t)
        t = re.sub(r"[^\w\u4e00-\u9fff]+", "", t)
        return t

    @classmethod
    def content_hash_v2(cls, subject_id: str, text: str) -> str:
        return hashlib.sha1(f"{subject_id}|{cls.norm_text(text)}".encode("utf-8")).hexdigest()[:32]

    async def upsert_episode(self, ep: dict[str, Any]) -> int | None:
        """影子写：按 (session_id, msg_hash) 幂等落 episode，返回 id。"""
        session_id = str(ep.get("session_id") or "")
        raw_json = str(ep.get("raw_json") or "[]")
        msg_hash = str(ep.get("msg_hash") or _hash16(raw_json))
        if not session_id or not raw_json or raw_json == "[]":
            return None
        ts = now()
        try:
            await self.db.execute(
                "INSERT INTO episodes (session_id, persona_id, subject_id, started_at, ended_at, "
                "raw_json, message_ids, msg_hash, summary, extract_state, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,'pending',?) "
                "ON CONFLICT(session_id, msg_hash) DO NOTHING",
                (
                    session_id,
                    str(ep.get("persona_id") or ""),
                    str(ep.get("subject_id") or ""),
                    float(ep.get("started_at") or ts),
                    float(ep.get("ended_at") or ts),
                    raw_json,
                    json.dumps(ep.get("message_ids") or [], ensure_ascii=False),
                    msg_hash,
                    str(ep.get("summary") or ""),
                    ts,
                ),
            )
            await self.db.commit()
        except Exception as exc:                            # noqa: BLE001 影子写失败不影响主链路
            logger.debug(f"[StarHeart] v2 episode 写入跳过：{exc}")
            return None
        row = await (await self.db.execute(
            "SELECT id FROM episodes WHERE session_id=? AND msg_hash=?", (session_id, msg_hash)
        )).fetchone()
        return int(row["id"]) if row else None

    async def claim_episode(self, ep_id: int, ttl: float = 300.0) -> bool:
        """认领一条 episode 做抽取；frozen 永不认领（回滚保护）。"""
        ts = now()
        cur = await self.db.execute(
            "UPDATE episodes SET extract_state='running', attempts=attempts+1, lock_until=? "
            "WHERE id=? AND extract_state NOT IN ('frozen','done') "
            "AND (lock_until IS NULL OR lock_until < ?)",
            (ts + ttl, int(ep_id), ts),
        )
        await self.db.commit()
        return int(cur.rowcount or 0) == 1

    async def insert_memory_v2(self, mem: dict[str, Any]) -> int | None:
        """写一条 v2 记忆；content_hash 冲突（同 subject 活跃行）返回 None。"""
        ts = now()
        text = str(mem.get("text") or "").strip()
        if not text:
            return None
        subject = str(mem.get("subject_id") or "")
        chash = str(mem.get("content_hash") or self.content_hash_v2(subject, text))
        try:
            cur = await self.db.execute(
                "INSERT INTO memories (episode_id, persona_id, subject_id, session_id, kind, text, "
                "content_hash, gist, occurred_start, occurred_end, mentioned_at, time_phrase, "
                "time_granularity, anchor_memory_id, anchor_state, valid_from, is_current, "
                "source_msg_ids, evidence_quote, evidence_kind, stance, origin, confidence, intimacy, due_at, "
                "importance, arousal, valence, proof_count, source_ids, consolidated, migrated, "
                "needs_review, status, tags, entities, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    mem.get("episode_id"),
                    str(mem.get("persona_id") or ""),
                    subject,
                    str(mem.get("session_id") or ""),
                    str(mem.get("kind") or "fact"),
                    text,
                    chash,
                    str(mem.get("gist") or ""),
                    mem.get("occurred_start"),
                    mem.get("occurred_end"),
                    float(mem.get("mentioned_at") or ts),
                    str(mem.get("time_phrase") or ""),
                    str(mem.get("time_granularity") or ""),
                    mem.get("anchor_memory_id"),
                    str(mem.get("anchor_state") or ""),
                    mem.get("valid_from"),
                    json.dumps(mem.get("source_msg_ids") or [], ensure_ascii=False),
                    str(mem.get("evidence_quote") or ""),
                    str(mem.get("evidence_kind") or "verbatim"),
                    str(mem.get("stance") or "unknown"),
                    str(mem.get("origin") or "user_explicit"),
                    float(mem.get("confidence") or 0.5),
                    int(mem.get("intimacy") or 0),
                    mem.get("due_at"),
                    int(mem.get("importance") or 3),
                    float(mem.get("arousal") or 0.0),
                    float(mem.get("valence") or 0.0),
                    int(mem.get("proof_count") or 0),
                    json.dumps(mem.get("source_ids") or [], ensure_ascii=False),
                    int(mem.get("consolidated") or 0),
                    int(mem.get("migrated") or 0),
                    int(mem.get("needs_review") or 0),
                    str(mem.get("status") or "active"),
                    json.dumps(mem.get("tags") or [], ensure_ascii=False),
                    json.dumps(mem.get("entities") or [], ensure_ascii=False),
                    ts,
                    ts,
                ),
            )
            await self.db.commit()
            return int(cur.lastrowid or 0)
        except aiosqlite.IntegrityError:
            await self.db.commit()
            return None

    async def migrate_v1_to_v2(self) -> dict[str, Any]:
        """批 A 迁移：hf_memories → memories（幂等，可重跑）。

        映射见 v1.6 §13.1：
          summary→text；content→gist（不同才写）；occurred_at→occurred_start；
          created_at→mentioned_at；evidence→evidence_quote（优先 source_text）；
          poignancy→importance；legacy_import 标记；consolidated=1 不占巩固队列。
        向量从 hf_vectors 复制并转 f16；实体边复制为 v2m{newid}（旧边保留）。
        """
        import array

        rows = await (await self.db.execute(
            "SELECT * FROM hf_memories WHERE status != 'deleted' ORDER BY id"
        )).fetchall()
        ts = now()
        stat = {"total": len(rows), "inserted": 0, "skipped": 0, "vectors": 0, "edges": 0, "errors": 0}
        for row in rows:
            d = dict(row)
            legacy_id = int(d["id"])
            summary = str(d.get("summary") or "").strip()
            if not summary:
                stat["skipped"] += 1
                continue
            exists = await (await self.db.execute(
                "SELECT id FROM memories WHERE legacy_id=?", (legacy_id,)
            )).fetchone()
            if exists:
                stat["skipped"] += 1
                continue
            content = str(d.get("content") or "").strip()
            gist = content if content and content != summary else ""
            subject = str(d.get("subject_id") or "")
            quote = str(d.get("source_text") or d.get("evidence") or "")[:250]
            try:
                poi = int(d.get("poignancy") or 5)
            except (TypeError, ValueError):
                poi = 5
            try:
                strength = float(d.get("strength") or 0.5)
            except (TypeError, ValueError):
                strength = 0.5
            importance = max(1, min(5, int(round(1 + strength * 4))))
            cur = await self.db.execute(
                "INSERT INTO memories (legacy_id, persona_id, subject_id, session_id, kind, text, "
                "content_hash, gist, occurred_start, mentioned_at, evidence_quote, evidence_kind, "
                "origin, confidence, time_granularity, importance, proof_count, source_ids, "
                "consolidated, migrated, status, tags, entities, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,'paraphrase','legacy_import',0.6,'month',?,?, "
                "'[]',1,1,?,?,?,?,?)",
                (
                    legacy_id,
                    str(d.get("persona_id") or ""),
                    subject,
                    str(d.get("session_id") or ""),
                    str(d.get("atom_type") or "fact"),
                    summary,
                    self.content_hash_v2(subject, summary),
                    gist,
                    d.get("occurred_at"),
                    float(d.get("created_at") or ts),
                    quote,
                    importance,
                    poi,
                    str(d.get("status") or "active"),
                    str(d.get("tags") or "[]"),
                    str(d.get("entities") or "[]"),
                    float(d.get("created_at") or ts),
                    ts,
                ),
            )
            new_id = int(cur.lastrowid)
            stat["inserted"] += 1
            await self.db.execute(
                "INSERT INTO mem_write_ledger (new_id, legacy_id, old_ok, new_ok, ts) VALUES (?,?,1,1,?)",
                (new_id, legacy_id, ts),
            )
            # 向量：hf_vectors → mem_vectors（f32 → f16）
            vr = await (await self.db.execute(
                "SELECT model, dim, vec FROM hf_vectors WHERE memory_id=? ORDER BY created_at DESC LIMIT 1",
                (legacy_id,),
            )).fetchone()
            if vr and vr["vec"]:
                try:
                    arr = array.array("f")
                    arr.frombytes(vr["vec"])
                    # Windows 的 array 不支持 'e'（半精度），用 struct 打包
                    import struct

                    half = struct.pack("<%de" % len(arr), *[float(x) for x in arr])
                    await self.db.execute(
                        "INSERT OR REPLACE INTO mem_vectors (owner_type, owner_id, gen_key, model, dim, vec) "
                        "VALUES ('memory',?,?,?,?,?)",
                        (new_id, self.gen_key(str(vr["model"] or ""), int(vr["dim"] or 0)),
                         str(vr["model"] or ""), int(vr["dim"] or 0), half),
                    )
                    stat["vectors"] += 1
                except Exception as exc:                    # noqa: BLE001 单条向量失败不影响迁移
                    logger.debug(f"[StarHeart] v2 迁移向量跳过 #{legacy_id}：{exc}")
            # 实体边：m{id} → v2m{new_id}（旧边保留，回滚需要）
            er = await (await self.db.execute(
                "SELECT dst_id, etype, weight FROM hf_edges "
                "WHERE src_type='memory' AND src_id=? AND dst_type='entity' AND status!='deleted'",
                (f"m{legacy_id}",),
            )).fetchall()
            for e in er:
                try:
                    await self.add_edge("memory", f"v2m{new_id}", "entity", str(e["dst_id"]),
                                        str(e["etype"] or "entity"), float(e["weight"] or 1.0))
                    stat["edges"] += 1
                except Exception:                           # noqa: BLE001
                    stat["errors"] += 1
        await self.db.commit()
        logger.info(f"[StarHeart] v2 迁移：{stat}")
        return stat

    async def v2_counts(self) -> dict[str, int]:
        """v2 各表行数（/hf v2 status 用）。"""
        out: dict[str, int] = {}
        for tbl in ("episodes", "memories", "mem_vectors", "mem_bigram", "access_log",
                    "mem_trace", "recall_event", "quarantine", "person_address", "anchor_queue"):
            try:
                row = await (await self.db.execute(f"SELECT COUNT(*) c FROM {tbl}")).fetchone()
                out[tbl] = int(row["c"] if row else 0)
            except Exception:                               # noqa: BLE001 表不存在时给 -1
                out[tbl] = -1
        return out

    @staticmethod
    def _mem_row(row: aiosqlite.Row) -> dict[str, Any]:
        d = dict(row)
        # embedding 是 float32 BLOB，走 JSON 序列化会被按 UTF-8 解码直接 400，且面板用不到
        d.pop("embedding", None)
        for k in ("tags", "entities", "cues"):
            try:
                d[k] = json.loads(d.get(k) or "[]")
            except (TypeError, ValueError):
                d[k] = []
        d["id"] = f"m{d['id']}"
        return d

    async def list_memories(
        self,
        q: str = "",
        tag: str = "",
        source: str = "",
        page: int = 1,
        size: int = 50,
        persona_id: str = "",
        status: str = "",
    ) -> dict[str, Any]:
        where: list[str] = []
        args: list[Any] = []
        if status:
            where.append("status = ?")
            args.append(status)
        else:
            # 与 search_memories 同口径：默认不列已软删的行
            where.append("status != 'deleted'")
        if q:
            where.append("(summary LIKE ? OR content LIKE ?)")
            args += [f"%{q}%", f"%{q}%"]
        if tag:
            where.append("tags LIKE ?")
            args.append(f"%{tag}%")
        if source:
            where.append("source_mode = ?")
            args.append(source)
        if persona_id == "__none__":
            where.append("(persona_id IS NULL OR persona_id = '')")
        elif persona_id:
            where.append("persona_id = ?")
            args.append(persona_id)
        clause = ("WHERE " + " AND ".join(where)) if where else ""
        total = (await (await self.db.execute(f"SELECT COUNT(*) FROM hf_memories {clause}", args)).fetchone())[0]
        rows = await (
            await self.db.execute(
                f"SELECT * FROM hf_memories {clause} ORDER BY strength DESC, updated_at DESC LIMIT ? OFFSET ?",
                args + [size, max(0, (page - 1) * size)],
            )
        ).fetchall()
        return {"total": total, "page": page, "size": size, "items": [self._mem_row(r) for r in rows]}

    async def get_memory(self, mid: str | int) -> dict[str, Any] | None:
        raw = str(mid)
        if raw.startswith("m"):
            raw = raw[1:]
        row = await (await self.db.execute("SELECT * FROM hf_memories WHERE id=?", (raw,))).fetchone()
        if row is None:
            return None
        mem = self._mem_row(row)
        src = f"m{raw}"
        edges = await (
            await self.db.execute(
                "SELECT * FROM hf_edges WHERE (src_type='memory' AND src_id=?) OR (dst_type='memory' AND dst_id=?)",
                (src, src),
            )
        ).fetchall()
        mem["links"] = [dict(e) for e in edges]
        # 向量状态（按"当前代"判断：旧列 embedding 不代表真相，换模型后旧代会被暂停）
        try:
            await self.ensure_vector_migration()
            gen = await self.active_gen()
            vrows = await (
                await self._db.execute("SELECT gen_key, dim, model FROM hf_vectors WHERE memory_id=?", (int(raw),))
            ).fetchall()
            cur = next((dict(r) for r in vrows if str(r["gen_key"]) == str(gen)), None)
            mem["vector"] = {
                "gen": str(gen or ""),
                "has_embedding": bool(cur and int(cur.get("dim") or 0) > 0),
                "dim": int((cur or {}).get("dim") or 0),
                "model": str((cur or {}).get("model") or ""),
                "other_gens": max(0, len(vrows) - (1 if cur else 0)),
                "gens": [dict(r) for r in vrows],
            }
        except Exception:                                    # noqa: BLE001 向量状态查不到不该让详情打不开
            mem["vector"] = {"gen": "", "has_embedding": False, "dim": 0, "model": "", "other_gens": 0, "gens": []}
        return mem

    async def update_memory(self, mid: str | int, fields: dict[str, Any]) -> bool:
        raw = str(mid).lstrip("m")
        allowed = {
            "summary",
            "content",
            "atom_type",
            "strength",
            "poignancy",
            "status",
            "evidence",
            "session_id",
            "persona_id",
        }
        sets: list[str] = []
        args: list[Any] = []
        for k, v in fields.items():
            if k in allowed:
                sets.append(f"{k}=?")
                args.append(v)
            elif k in ("tags", "entities", "cues"):
                sets.append(f"{k}=?")
                args.append(json.dumps(v if isinstance(v, list) else [v], ensure_ascii=False))
        if not sets:
            return False
        # 改了强度就同步刷新衰减基线：否则「+0.15 加权」会被按旧基线一次性复利扣回去
        # （衰减 = strength × 0.5^(距基线的龄期)，基线不动 = 补的强度等于白补）。
        if "strength" in fields:
            sets.append("strength_updated_at=?")
            args.append(now())
        sets.append("updated_at=?")
        args += [now(), raw]
        await self.db.execute(f"UPDATE hf_memories SET {', '.join(sets)} WHERE id=?", args)
        await self.db.commit()
        return True

    async def delete_memory(self, mid: str | int) -> bool:
        """单条删除 = **软删**（status='deleted'），关系边/向量/原文都留着，可恢复。

        为什么不再物理删：面板一次误点就把记忆 + 关系链 + 原文一起抹掉，无撤销、无备份；
        与本文件 purge_stale_memories 自己确立的「软删可回滚」原则相反。
        硬删改为显式语义：只有「清空回收站」才会真删（purge_deleted_memories）。
        """
        raw = str(mid).lstrip("m")
        cur = await self.db.execute(
            "UPDATE hf_memories SET status='deleted', updated_at=? WHERE id=? AND status!='deleted'",
            (now(), raw),
        )
        await self.db.commit()
        return bool(cur.rowcount)

    async def restore_memory(self, mid: str | int) -> bool:
        """把回收站里的记忆恢复成 active。

        覆盖两种终态：deleted（面板删除）与 forgotten（生命周期遗忘）。
        forgotten 仍然不可被召回命中 —— 只有这里的人工恢复能把它拉回来，
        与「遗忘是终点」的语义不冲突（这是显式的、一次一条的撤销）。
        """
        raw = str(mid).lstrip("m")
        stamp = now()
        cur = await self.db.execute(
            "UPDATE hf_memories SET status='active', last_used_at=?, strength_updated_at=?, "
            "strength=MAX(COALESCE(strength,0), 0.45), updated_at=? "
            "WHERE id=? AND status IN ('deleted','forgotten')",
            (stamp, stamp, stamp, raw),
        )
        await self.db.commit()
        return bool(cur.rowcount)

    async def purge_deleted_memories(self, older_than_days: float = 7) -> int:
        """真删「回收站」里超过保留期的记忆（连同边/向量/来源行）。

        这是唯一保留物理删除的路径，目的是不让 status='deleted' 的行永久堆积。
        """
        cut = now() - max(0.0, float(older_than_days)) * 86400
        rows = await (await self.db.execute(
            "SELECT id FROM hf_memories WHERE status='deleted' AND updated_at < ?", (cut,)
        )).fetchall()
        n = 0
        for r in rows:
            raw = str(r["id"])
            await self.db.execute(
                "DELETE FROM hf_edges WHERE (src_type='memory' AND src_id=?) OR (dst_type='memory' AND dst_id=?)",
                (f"m{raw}", f"m{raw}"),
            )
            for tbl in ("hf_vectors", "hf_memory_src", "hf_memory_msg"):
                try:
                    await self.db.execute(f"DELETE FROM {tbl} WHERE memory_id=?", (raw,))
                except Exception:                        # noqa: BLE001 老库可能没这张表
                    pass
            await self.db.execute("DELETE FROM hf_memories WHERE id=?", (raw,))
            n += 1
        await self.db.commit()
        return n

    # ---------------- 记忆捕获待处理队列（先落库，再总结） ----------------
    # 旧链路是 `turns=list(buf); buf.clear(); _spawn(总结)`：模型超时/空响应/格式不对
    # 就整批消失，只剩一行 log，还会白吃 300s 间隔。现在一律先写 pending，成功才删。
    async def capture_enqueue(self, turns: list[dict[str, Any]], *, session_id: str = "",
                              persona_id: str = "", user_id: str = "", user_name: str = "",
                              delay: float = 0.0) -> int | None:
        """入队（**不占位**）：只插入、返回 id。

        生产链路不要用这条 —— 「插入后再去 claim」中间有窗口（P2-1），用 capture_enqueue_row。
        保留它是因为它也是「手动放一批进队列」的最小原语（测试/工具用）。
        """
        return await self._capture_insert(
            turns, session_id=session_id, persona_id=persona_id, user_id=user_id,
            user_name=user_name, delay=delay, claim=False)

    async def capture_enqueue_row(self, turns: list[dict[str, Any]], *, session_id: str = "",
                                  persona_id: str = "", user_id: str = "", user_name: str = "",
                                  delay: float = 0.0, lease: float = 0.0) -> dict[str, Any] | None:
        """入队**即占位**：INSERT 里一次写死 locked_by + 租约，返回整行。

        旧路径是「INSERT 再 capture_claim」两条语句：两步之间 retry tick 的 capture_take 能把新行
        抢走（新行 status=retry、next_try_at<=now、locked_until=0 全满足）；被抢走后本次 worker
        拿到的是没有 token 的 row，done/fail 都变成无条件操作 —— 正好是本批要消灭的双跑（P2-1）。
        """
        pid = await self._capture_insert(
            turns, session_id=session_id, persona_id=persona_id, user_id=user_id,
            user_name=user_name, delay=delay, claim=True, lease=lease)
        if not pid:
            return None
        row = await (await self.db.execute(
            "SELECT * FROM hf_capture_pending WHERE id=?", (pid,))).fetchone()
        return self._capture_row(row) if row else None

    async def _capture_insert(self, turns: list[dict[str, Any]], *, session_id: str = "",
                              persona_id: str = "", user_id: str = "", user_name: str = "",
                              delay: float = 0.0, claim: bool = False,
                              lease: float = 0.0) -> int | None:
        if not turns:
            return None
        try:
            payload = json.dumps(list(turns), ensure_ascii=False)
        except Exception:
            return None
        t_now = now()
        due = t_now + max(0.0, float(delay or 0))
        if claim:
            token = uuid.uuid4().hex
            cur = await self.db.execute(
                "INSERT INTO hf_capture_pending "
                "(session_id, persona_id, user_id, user_name, payload, tries, next_try_at, "
                " status, locked_by, locked_until, created_at) "
                "VALUES (?,?,?,?,?,0,?,'retry',?,?,?)",
                (str(session_id or ""), str(persona_id or ""), str(user_id or ""), str(user_name or ""),
                 payload, due, token, t_now + float(lease or self.CAPTURE_LEASE_SECONDS), t_now),
            )
        else:
            cur = await self.db.execute(
                "INSERT INTO hf_capture_pending "
                "(session_id, persona_id, user_id, user_name, payload, tries, next_try_at, "
                " status, locked_by, locked_until, created_at) "
                "VALUES (?,?,?,?,?,0,?,'retry','',0,?)",
                (str(session_id or ""), str(persona_id or ""), str(user_id or ""), str(user_name or ""),
                 payload, due, t_now),
            )
        await self.db.commit()
        return int(cur.lastrowid or 0) or None

    # 租约 10 分钟：worker 被 kill / 容器重启时锁不会永久不还，到期自动可被再次领取。
    CAPTURE_LEASE_SECONDS = 600.0

    @staticmethod
    def _capture_row(r: Any) -> dict[str, Any]:
        """行 → dict，并把 payload 解成 turns（面板/worker 共用）。"""
        d = dict(r)
        try:
            d["turns"] = json.loads(d.get("payload") or "[]")
        except Exception:
            d["turns"] = []
        return d

    async def capture_take(self, limit: int = 3, *, lease: float = 0.0) -> list[dict[str, Any]]:
        """原子领取到期可跑的待处理批（按到期时间先进先出）。

        旧实现只 SELECT 不改状态：一批总结超过一个 tick（120s）就会被第二个 worker 再取一次，
        两个 worker 同时总结、同时写记忆 —— 重复记忆 + 重复烧 token（P1-4）。
        现在先 UPDATE 抢锁（locked_by 唯一 token + locked_until 租约），再按 token 回读自己的行。
        到期租约（locked_until <= now）自动可再领取，不需要额外回收任务。
        """
        n = max(1, int(limit))
        t_now = now()
        token = uuid.uuid4().hex
        lease_s = float(lease or self.CAPTURE_LEASE_SECONDS)
        await self.db.execute(
            "UPDATE hf_capture_pending SET locked_by=?, locked_until=? "
            "WHERE id IN ("
            "  SELECT id FROM hf_capture_pending "
            "   WHERE COALESCE(status,'retry')='retry' AND next_try_at <= ? "
            "     AND COALESCE(locked_until,0) <= ? "
            "   ORDER BY next_try_at ASC, id ASC LIMIT ?)",
            (token, t_now + lease_s, t_now, t_now, n),
        )
        await self.db.commit()
        rows = await (await self.db.execute(
            "SELECT * FROM hf_capture_pending WHERE locked_by=? ORDER BY next_try_at ASC, id ASC",
            (token,),
        )).fetchall()
        return [self._capture_row(r) for r in rows]

    async def capture_count(self) -> int:
        row = await (await self.db.execute("SELECT COUNT(*) AS n FROM hf_capture_pending")).fetchone()
        return int((row or {})["n"] or 0) if row else 0

    async def capture_stats(self) -> dict[str, int]:
        """按状态统计（retry / parked / …）：停摆的行要能被看见，不再靠 next_try_at 猜。"""
        rows = await (await self.db.execute(
            "SELECT COALESCE(status,'retry') AS st, COUNT(*) AS n FROM hf_capture_pending GROUP BY st"
        )).fetchall()
        return {str(r["st"] or "retry"): int(r["n"] or 0) for r in rows}

    async def capture_pending_list(self, limit: int = 20) -> list[dict[str, Any]]:
        """面板用的队列视图：**不返回 payload**（里面是对话正文），只给可判断状态需要的列。"""
        rows = await (await self.db.execute(
            "SELECT id, session_id, tries, next_try_at, last_error, status, created_at "
            "FROM hf_capture_pending ORDER BY id DESC LIMIT ?", (max(1, int(limit)),)
        )).fetchall()
        return [dict(r) for r in rows]

    async def capture_hold(self, pid: int, reason: str, *, token: str = "",
                           delay: float = 3600.0) -> bool:
        """暂停处理（典型场景：用户关掉了「记忆捕获」）——**不删行**，把到期时间推后。

        旧实现把「捕获已关闭」当成「这批无需处理」直接删行：用户只是想暂停，队列里已捕获的
        对话被静默丢掉。现在行留着，开关重新打开后由 retry tick 自动续跑。
        """
        sql = ("UPDATE hf_capture_pending SET status='retry', next_try_at=?, last_error=?, "
               "locked_by='', locked_until=0 WHERE id=?")
        args: tuple[Any, ...] = (now() + max(60.0, float(delay)), f"paused: {reason}"[:300], int(pid))
        if token:
            sql += " AND locked_by=?"
            args = args + (str(token),)
        cur = await self.db.execute(sql, args)
        await self.db.commit()
        return bool(getattr(cur, "rowcount", 0))

    async def capture_parked_for_retry(self, limit: int = 5) -> int:
        """给停摆的批一次机会：tries 归 1、到期时间归现在，重新进入退避链。

        退避链是 0→30→90→210→450s（约 7.5 分钟）：模型/网关故障超过这个时长，队列会整批
        parked。没有出口的话，模型修好了也永远不会自动恢复，只能人工翻库。
        """
        cur = await self.db.execute(
            "UPDATE hf_capture_pending SET status='retry', tries=1, next_try_at=?, "
            "locked_by='', locked_until=0 WHERE id IN "
            "(SELECT id FROM hf_capture_pending WHERE status='parked' ORDER BY id ASC LIMIT ?)",
            (now(), max(1, int(limit))),
        )
        await self.db.commit()
        return int(getattr(cur, "rowcount", 0) or 0)

    async def capture_done(self, pid: int, token: str = "") -> None:
        """成功 / 明确无需处理 → 删行。带 token 时只删自己锁住的那条（防误删别人正在跑的批）。"""
        if token:
            await self.db.execute(
                "DELETE FROM hf_capture_pending WHERE id=? AND locked_by=?", (int(pid), str(token)))
        else:
            await self.db.execute("DELETE FROM hf_capture_pending WHERE id=?", (int(pid),))
        await self.db.commit()

    async def capture_fail(self, pid: int, err: str, max_tries: int = 5, token: str = "") -> int:
        """失败退避：30s → 60 → 120 → 240；到上限转 parked（保留整行可查，不再自动重试）。

        旧实现第 5 次直接把 next_try_at 推到 365 天后：既不会真重试，又伪装成「还会跑」，
        面板和日志都看不出这行已经停摆。现在用 status 明说，next_try_at 归零。
        """
        sql = "SELECT tries FROM hf_capture_pending WHERE id=?"
        args: tuple[Any, ...] = (int(pid),)
        if token:
            sql += " AND locked_by=?"
            args = (int(pid), str(token))
        row = await (await self.db.execute(sql, args)).fetchone()
        if not row:                                   # 锁已被回收/行已被删 → 不覆盖别人的状态
            return 0
        tries = int((row or {"tries": 0})["tries"] or 0) + 1
        parked = tries >= max_tries
        nxt = 0.0 if parked else now() + min(3600.0, 30.0 * (2 ** (tries - 1)))
        status = "parked" if parked else "retry"
        await self.db.execute(
            "UPDATE hf_capture_pending SET tries=?, next_try_at=?, last_error=?, status=?, "
            "locked_by='', locked_until=0 WHERE id=?" + (" AND locked_by=?" if token else ""),
            (tries, nxt, str(err or "")[:300], status, int(pid)) + ((str(token),) if token else ()),
        )
        await self.db.commit()
        if parked:
            await self.audit("memory.capture.parked", f"id={pid} tries={tries}", str(err or "")[:200])
        return tries

    async def purge_stale_memories(self, persona_id: str | None = None) -> int:
        """把「非 active」的记忆**软删**（status='deleted'）并级联清理，返回受影响条数。

        为什么不再物理删（原实现是一句 `DELETE FROM hf_memories WHERE status != 'active'`）：
        ① 它连 archived 一起物理删掉，而面板与文档都把 archived 说成「可找回」→ 不可逆的数据丢失；
        ② 完全没有 persona 过滤 → 一次清理会跨所有人格动手，与「人格隔离」硬约束冲突；
        ③ 只清 hf_edges，不清 hf_vectors / hf_memory_src → 留下永远指向不存在记忆的孤儿行。

        现在：只打 status='deleted'（召回侧本来就都带 `status!='deleted'`，立刻不可见、但可回滚），
        按 persona 可选收窄，并把该批记忆的向量、来源行与关系边一并清掉。
        """
        pwhere, args = "", []
        if persona_id is not None:
            pwhere = " AND COALESCE(persona_id,'')=?"
            args = [str(persona_id)]
        rows = await (
            await self.db.execute(
                "SELECT id FROM hf_memories WHERE status NOT IN ('active','deleted')" + pwhere,
                tuple(args),
            )
        ).fetchall()
        ids = [int(r["id"]) for r in rows]
        if not ids:
            return 0
        marks = ",".join("?" for _ in ids)
        mids = [f"m{i}" for i in ids]
        mmarks = ",".join("?" for _ in mids)
        await self.db.execute(f"UPDATE hf_memories SET status='deleted' WHERE id IN ({marks})", tuple(ids))
        for tbl in ("hf_vectors", "hf_memory_src", "hf_memory_msg"):
            try:
                await self.db.execute(f"DELETE FROM {tbl} WHERE memory_id IN ({marks})", tuple(ids))
            except Exception as exc:                        # noqa: BLE001 老库可能没这张表
                logger.warning(f"[StarHeart] 清理 {tbl} 跳过：{exc}")
        await self.db.execute(
            f"DELETE FROM hf_edges WHERE (src_type='memory' AND src_id IN ({mmarks}))"
            f" OR (dst_type='memory' AND dst_id IN ({mmarks}))",
            tuple(mids + mids),
        )
        await self.db.commit()
        # 兜底：扫掉历史遗留的孤儿边（保留原实现这一步的语义）
        # v2 的边用 'v2m<id>' 命名空间，显式排除：它们不在 hf_memories 里，
        # 不排除会被这条清理整批误删（v2 方案 G20）。
        await self.db.execute(
            "DELETE FROM hf_edges WHERE src_type='memory' AND src_id NOT LIKE 'v2m%' "
            "AND src_id NOT IN (SELECT 'm'||id FROM hf_memories)"
        )
        await self.db.commit()
        logger.info(f"[StarHeart] 清理非活跃记忆：{len(ids)} 条软删（status='deleted'，可回滚）")
        return len(ids)

    async def edges_of(self, mid: str | int) -> list[dict[str, Any]]:
        raw = "m" + str(mid).lstrip("m")
        rows = await (await self.db.execute(
            "SELECT * FROM hf_edges WHERE (src_id=? OR dst_id=?) AND status != 'deleted' ORDER BY weight DESC LIMIT 60",
            (raw, raw),
        )).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            other = item["dst_id"] if item["src_id"] == raw else item["src_id"]
            item["other"] = other
            out.append(item)
        return out

    async def update_edge(self, edge_id: Any, weight: Any = None, status: Any = None) -> bool:
        if edge_id is None:
            return False
        sets, args = [], []
        if weight is not None:
            sets.append("weight=?")
            args.append(float(weight))
        if status:
            sets.append("status=?")
            args.append(str(status))
        if not sets:
            return False
        sets.append("updated_at=?")
        args.extend([now(), edge_id])
        cur = await self.db.execute(f"UPDATE hf_edges SET {', '.join(sets)} WHERE id=?", tuple(args))
        await self.db.commit()
        return bool(cur.rowcount)

    async def panel_stats(self, persona_id: str = "") -> dict[str, Any]:
        """面板用的真实计数（危险区/概览）。传 persona_id 则记忆数只算该人格的。"""

        async def one(sql: str, args: list[Any] | None = None) -> int:
            row = await (await self.db.execute(sql, args or [])).fetchone()
            return int(row[0] or 0) if row else 0

        # 口径与 memory_counts / persona_memory_counts 对齐：软删的行不算数
        # （否则「清理非活跃记忆」之后危险区/概览数字不变，像没生效）
        mem_where, mem_args = " WHERE status != 'deleted'", []
        if persona_id == "__none__":
            mem_where += " AND (persona_id IS NULL OR persona_id = '')"
        elif persona_id:
            mem_where += " AND persona_id = ?"
            mem_args = [persona_id]
        # 这里必须把 mem_args 传下去：persona_id 非空时 mem_where 带一个 ?，
        # 不传参数 → sqlite3 ProgrammingError「uses 1, and there are 0 supplied」→ 整个 /stats 500，
        # 面板只要选了人格视角（?persona_id=xxx）就整页空白（2026-10-11 真机复现）。
        by_type = await (await self.db.execute(
            "SELECT COALESCE(atom_type,'') AS t, COUNT(*) AS c FROM hf_memories" + mem_where + " GROUP BY t",
            tuple(mem_args),
        )).fetchall()
        return {
            "gate": await one("SELECT COUNT(*) FROM hf_gate_log"),
            # 面板要用的真值（以前 /stats 不返回，导致门控「当前触发率」「清空流水」恒 0、类型分布恒空）
            "gate_total": await one("SELECT COUNT(*) FROM hf_gate_log"),
            "gate_reply": await one("SELECT COUNT(*) FROM hf_gate_log WHERE decision='reply'"),
            "memory_by_type": {str(r["t"] or "未知"): int(r["c"]) for r in by_type},
            "memories": await one(f"SELECT COUNT(*) FROM hf_memories{mem_where}", mem_args),
            "habits": await one("SELECT COUNT(*) FROM hf_expr WHERE status!='archived'"),
            "favours": await one("SELECT COUNT(*) FROM hf_favours"),
            "entities": await one("SELECT COUNT(*) FROM hf_entities"),
            "edges": await one("SELECT COUNT(*) FROM hf_edges"),
            "tokens": await one("SELECT COUNT(*) FROM hf_tokens"),
            "scoped": bool(persona_id),
        }

    async def purge_gate_log(self) -> int:
        cur = await self.db.execute("DELETE FROM hf_gate_log")
        await self.db.commit()
        return int(cur.rowcount or 0)

    async def purge_learning(self) -> dict[str, Any]:
        """清记忆/实体/关系/表达（原语义；拍-14：先备份再动）。

        好感保留（关系是长期资产，单独由面板决定）。返回值多一个 backup 字段
        ——「备份没建成」要能让面板如实说出来，不能假装有备份。
        """
        backup = await self.backup_tables(LEARNING_BACKUP_TABLES, tag="learning")
        out: dict[str, Any] = {"backup": backup}
        if not backup:
            # 备份失败就绝不动库：宁可这次清空失效，也不能不可恢复地删数据
            out["error"] = "backup_failed"
            out["aborted"] = True
            logger.error("[StarHeart] 清空学习中止：备份失败，未删除任何表")
            return out
        for table in ("hf_edges", "hf_entities", "hf_memories", "hf_expr", "hf_habits"):
            try:
                cur = await self.db.execute(f"DELETE FROM {table}")
                out[table.replace("hf_", "")] = int(cur.rowcount or 0)
            except Exception as exc:                        # noqa: BLE001 单表缺/坏不挡其它表
                logger.warning(f"[StarHeart] 清空学习数据跳过 {table}：{exc}")
        await self.db.commit()
        return out

    async def purge_session_memories(self, session_id: str) -> int:
        cur = await self.db.execute("DELETE FROM hf_memories WHERE session_id=?", (session_id,))
        await self.db.commit()
        return int(cur.rowcount or 0)

    async def memory_counts(self, persona_id: str = "") -> dict[str, Any]:
        """面板用的真实记忆计数（一律排除 status='deleted' 软删行）。

        口径统一：hf_memories.status 是生命周期语义（active/decayed/archived/stale/deleted），
        召回侧每条查询都带 `status!='deleted'`，persona_memory_counts 也带 ——
        只有这里原是裸的 `SELECT COUNT(*)`，没有任何 status 过滤。
        后果：执行「清理非活跃记忆」之后面板数字纹丝不动，看起来像按钮没生效。
        """
        cur = self.db
        where = " WHERE status != 'deleted'"
        args: list[Any] = []
        if persona_id == "__none__":
            where += " AND (persona_id IS NULL OR persona_id = '')"
        elif persona_id:
            where += " AND persona_id = ?"
            args = [persona_id]
        total = (await (await cur.execute(f"SELECT COUNT(*) FROM hf_memories{where}", args)).fetchone())[0]
        by_type = {
            r["atom_type"]: r["c"]
            for r in await (
                await cur.execute(f"SELECT atom_type, COUNT(*) c FROM hf_memories{where} GROUP BY atom_type", args)
            ).fetchall()
        }
        by_status = {
            r["status"]: r["c"]
            for r in await (
                await cur.execute(f"SELECT status, COUNT(*) c FROM hf_memories{where} GROUP BY status", args)
            ).fetchall()
        }
        by_source = {
            (r["source_mode"] or "unknown"): r["c"]
            for r in await (
                await cur.execute(
                    f"SELECT source_mode, COUNT(*) c FROM hf_memories{where} GROUP BY source_mode", args
                )
            ).fetchall()
        }
        avg = (
            await (await cur.execute(f"SELECT AVG(strength) FROM hf_memories{where}", args)).fetchone()
        )[0] or 0
        return {"total": total, "by_type": by_type, "by_status": by_status, "by_source": by_source, "avg_strength": round(float(avg), 3)}

    # ---------------- 实体与关系边 ----------------
    async def ensure_entity(self, kind: str, name: str) -> str:
        canonical = f"{kind}:{name}"
        row = await (await self.db.execute("SELECT id, weight FROM hf_entities WHERE canonical=?", (canonical,))).fetchone()
        if row is None:
            cur = await self.db.execute(
                "INSERT INTO hf_entities(kind,name,canonical,meta,weight,last_seen) VALUES(?,?,?,'{}',1,?)",
                (kind, name, canonical, now()),
            )
            await self.db.commit()
            return f"e{cur.lastrowid}"
        await self.db.execute("UPDATE hf_entities SET weight=weight+1, last_seen=? WHERE id=?", (now(), row["id"]))
        await self.db.commit()
        return f"e{row['id']}"

    async def add_edge(self, src_type: str, src_id: str, dst_type: str, dst_id: str, etype: str, weight: float = 1.0) -> None:
        ts = now()
        cur = await self.db.execute(
            "SELECT id, weight FROM hf_edges WHERE src_type=? AND src_id=? AND dst_type=? AND dst_id=? AND etype=?",
            (src_type, src_id, dst_type, dst_id, etype),
        )
        row = await cur.fetchone()
        if row is None:
            await self.db.execute(
                "INSERT INTO hf_edges(src_type,src_id,dst_type,dst_id,etype,weight,status,created_at,updated_at) VALUES(?,?,?,?,?,?,'active',?,?)",
                (src_type, src_id, dst_type, dst_id, etype, weight, ts, ts),
            )
        else:
            await self.db.execute("UPDATE hf_edges SET weight=?, updated_at=? WHERE id=?", (max(weight, row["weight"]), ts, row["id"]))
        await self.db.commit()

    # ---------------- 关系链（图谱的命脉） ----------------
    @staticmethod
    def _persona_where(persona_id: str, column: str = "persona_id") -> tuple[str, list[Any]]:
        """人格过滤 SQL 片段：__none__ = 没有归属的旧数据；空串 = 全量（不筛）。"""
        if persona_id == "__none__":
            return f" AND ({column} IS NULL OR {column} = '')", []
        if persona_id:
            return f" AND {column} = ?", [persona_id]
        return "", []

    LIKE_ESC = "\\"      # SQLite 的 LIKE 转义字符（反斜杠）

    @staticmethod
    def _like_escape(value: str) -> str:
        """把 LIKE 元字符逐字转义（反斜杠 / % / _）。拆出来是因为群会话串要在中间插 GroupMessage。"""
        return (str(value or "").replace("\\", "\\\\")
                .replace("%", "\\%").replace("_", "\\_"))

    @staticmethod
    def _like_suffix(value: str) -> tuple[str, str]:
        """构造「会话串以 :<id> 结尾」的安全 LIKE，返回 (pattern, escape_char)。

        为什么必须转义：person_id / 群号里若出现 `%` 或 `_`，LIKE 会把它们当通配符 ——
        实测 `200_02` 会匹配到 `200X02`（下划线吃一个字符），把画像回填进**别人格**的域里，
        等于人格隔离被静默击穿。所以统一把「反斜杠 / % / _」逐字转义，并显式声明 ESCAPE。
        """
        return "%:" + Store._like_escape(value), Store.LIKE_ESC

    @staticmethod
    def _split_mid(value: Any) -> int | None:
        """把 'm12' / 12 / '12' 统一成 12；非法返回 None。"""
        raw = str(value).strip()
        if raw.startswith("m"):
            raw = raw[1:]
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None

    async def _entities_of_memory(self, mid: int) -> list[str]:
        """从边表取这条记忆挂着的实体 id（'e7' 这种）。"""
        rows = await (
            await self.db.execute(
                "SELECT dst_id FROM hf_edges WHERE status='active' AND src_type='memory' AND src_id=? AND etype='entity'",
                (f"m{mid}",),
            )
        ).fetchall()
        return [r["dst_id"] for r in rows]

    async def link_memory(
        self,
        mid: str | int,
        *,
        entities: list[str] | None = None,
        entity_kind: str = "topic",
        session_id: str = "",
        persona_id: str = "",
        occurred_at: float | None = None,
        cues: list[str] | None = None,
        window: int = 8,
    ) -> dict[str, int]:
        """给一条**刚写入**的记忆补关系链：实体边 + 同会话时序边 + 共享实体/线索的语义边。

        为什么必须在这里做：图谱的观感完全取决于记忆之间的关系密度。旧版只有导入路径
        （core/importer.py）会建边，运行时写入的记忆全是孤点 —— 面板上就只能看到一堆
        互不相连的点，也就是用户说的「没有关系链」。返回各类计数，便于面板/日志核对。
        """
        sid = self._split_mid(mid)
        if sid is None:
            return {"entity": 0, "temporal": 0, "semantic": 0}
        self_id = f"m{sid}"
        out = {"entity": 0, "temporal": 0, "semantic": 0}

        # 1) 记忆 → 实体：实体名先落库，再挂边
        ent_ids: list[str] = []
        for name in list(dict.fromkeys([str(x).strip() for x in (entities or []) if str(x).strip()]))[:8]:
            eid = await self.ensure_entity(entity_kind, name)
            ent_ids.append(eid)
            await self.add_edge("memory", self_id, "entity", eid, "entity", 1.4)
            out["entity"] += 1

        ts = float(occurred_at or now())
        mine = {str(x).strip().lower() for x in (cues or []) if str(x).strip()}

        # 2) 同会话前后邻居：时序边（只连时间上最近的 2 个，链条就不会变成毛线球）
        if session_id:
            rows = await (
                await self.db.execute(
                    # 人格隔离（硬约束）：只能在**同一个人格**内建边。
                    # 同一会话里切换过人格时，旧写法会把两个人格的记忆连起来（真机上已产生 17 条跨人格边）。
                    "SELECT id, COALESCE(occurred_at, created_at) AS ts, cues FROM hf_memories "
                    "WHERE session_id=? AND id!=? AND status!='deleted' AND COALESCE(persona_id,'')=? "
                    "ORDER BY ABS(COALESCE(occurred_at, created_at) - ?) ASC LIMIT ?",
                    (session_id, sid, str(persona_id or ""), ts, max(1, window)),
                )
            ).fetchall()
            for i, row in enumerate(rows[:2]):
                await self.add_edge("memory", self_id, "memory", f"m{row['id']}", "temporal", 0.9 - i * 0.1)
                out["temporal"] += 1
            # 3) 同会话里线索词重合的，补语义边（对话内同主题的连续讨论）
            for row in rows:
                try:
                    others = {str(x).strip().lower() for x in json.loads(row["cues"] or "[]")}
                except (TypeError, ValueError):
                    others = set()
                if not mine or not others:
                    continue
                overlap = len(mine & others) / max(1, len(mine | others))
                if overlap >= 0.34:
                    await self.add_edge("memory", self_id, "memory", f"m{row['id']}", "semantic", 0.9 + overlap)
                    out["semantic"] += 1

        # 4) 跨会话：共享实体的记忆互连（每个实体最多连 4 条，防止枢纽爆边）
        for eid in ent_ids:
            rows = await (
                await self.db.execute(
                    # 同上：共享实体的对手记忆必须同人格（实体本身目前是全局去重的，
                    # 人格域内的实体拆分在做图谱重做时一并处理；这里先保证**边**不跨人格）
                    "SELECT src_id FROM hf_edges WHERE status='active' AND etype='entity' AND dst_id=? "
                    "AND src_type='memory' AND src_id!=? "
                    "AND src_id IN (SELECT 'm'||id FROM hf_memories WHERE COALESCE(persona_id,'')=?) "
                    "ORDER BY weight DESC LIMIT 4",
                    (eid, self_id, str(persona_id or "")),
                )
            ).fetchall()
            for row in rows:
                await self.add_edge("memory", self_id, "memory", row["src_id"], "semantic", 1.0)
                out["semantic"] += 1
        return out

    async def repair_persona_leaks(self) -> dict[str, int]:
        """幂等清理人格隔离泄漏：把「两端人格不同」的记忆↔记忆边置为 inactive（不物理删除）。

        为什么需要：link_memory 早期版本只按会话建边，同一会话里切换人格会连出跨人格边
        （真机实测已产生 17 条）。人格隔离是默认开启的硬约束，跨人格边既是数据污染，
        也会让人格 B 的图上出现人格 A 的痕迹。启动时跑一次，可反复执行。
        """
        out = {"cross_persona": 0}
        try:
            rows = await (await self.db.execute(
                "SELECT e.id FROM hf_edges e "
                "JOIN hf_memories a ON e.src_id = 'm'||a.id "
                "JOIN hf_memories b ON e.dst_id = 'm'||b.id "
                "WHERE e.src_type='memory' AND e.dst_type='memory' AND e.status='active' "
                "AND COALESCE(a.persona_id,'') != COALESCE(b.persona_id,'')"
            )).fetchall()
            ids = [int(r["id"]) for r in rows]
            if ids:
                marks = ",".join("?" * len(ids))
                await self.db.execute(
                    f"UPDATE hf_edges SET status='inactive', updated_at=? WHERE id IN ({marks})",
                    (now(), *ids),
                )
                await self.db.commit()
            out["cross_persona"] = len(ids)
        except Exception as exc:                                  # noqa: BLE001 隔离修复失败不能拖垮启动
            out["error"] = str(exc)[:120]
        return out

    async def rebuild_links(self, limit: int = 3000, persona_id: str = "") -> dict[str, Any]:
        """对**已有**记忆做一次关系链重建（老库/导入残缺时用）。

        规则（确定性、可重复执行，不会把边越滚越大）：
        - 实体边：按 entities 字段补齐 memory → entity；
        - 时序边：同一会话内按发生时间排序，相邻两条相连，跨度最多 3 跳；
        - 语义边：共享实体（权重按重合度）、或 tags/cues 高度重合的记忆两两相连，每个记忆上限 10 条。
        """
        pwhere, pargs = self._persona_where(persona_id)
        rows = await (
            await self.db.execute(
                "SELECT id, atom_type, tags, entities, cues, session_id, COALESCE(occurred_at, created_at) AS ts "
                "FROM hf_memories WHERE status!='deleted'" + pwhere + " ORDER BY ts ASC LIMIT ?",
                (*pargs, max(1, limit)),
            )
        ).fetchall()
        stat = {"memories": len(rows), "entity": 0, "temporal": 0, "semantic": 0}
        if not rows:
            return stat

        ent_index: dict[str, list[int]] = {}
        tag_index: dict[str, list[int]] = {}
        by_session: dict[str, list[tuple[float, int]]] = {}
        seq: list[tuple[int, list[str], set[str], float, str]] = []
        for r in rows:
            mid = int(r["id"])
            try:
                ents = [str(x).strip() for x in json.loads(r["entities"] or "[]") if str(x).strip()]
            except (TypeError, ValueError):
                ents = []
            try:
                tags = {str(x).strip().lower() for x in json.loads(r["tags"] or "[]") if str(x).strip()}
            except (TypeError, ValueError):
                tags = set()
            cues_raw = r["cues"]
            try:
                cues = {str(x).strip().lower() for x in json.loads(cues_raw or "[]") if str(x).strip()}
            except (TypeError, ValueError):
                cues = set()
            seq.append((mid, ents, tags | cues, float(r["ts"] or 0), str(r["session_id"] or "")))
            for name in ents:
                ent_index.setdefault(name.lower(), []).append(mid)
            for t in tags:
                tag_index.setdefault(t, []).append(mid)
            if r["session_id"]:
                by_session.setdefault(str(r["session_id"]), []).append((float(r["ts"] or 0), mid))

        # 实体边
        ent_cache: dict[str, str] = {}
        for name in {n for _mid, ents, _tags, _ts, _sid in seq for n in ents}:
            canonical = f"topic:{name}"
            row = await (await self.db.execute("SELECT id FROM hf_entities WHERE canonical=?", (canonical,))).fetchone()
            eid = f"e{row['id']}" if row else await self.ensure_entity("topic", name)
            ent_cache[name.lower()] = eid
        for mid, ents, _tags, _ts, _sid in seq:
            for name in ents:
                eid = ent_cache.get(name.lower())
                if not eid:
                    continue
                await self.add_edge("memory", f"m{mid}", "entity", eid, "entity", 1.4)
                stat["entity"] += 1

        # 时序边（同会话，相邻 + 3 跳窗口）
        for _sid, items in by_session.items():
            items.sort(key=lambda x: x[0])
            for i in range(len(items)):
                for j in range(i + 1, min(len(items), i + 4)):
                    weight = 0.9 if j == i + 1 else 0.6
                    await self.add_edge("memory", f"m{items[i][1]}", "memory", f"m{items[j][1]}", "temporal", weight)
                    stat["temporal"] += 1

        # 语义边：共享实体 / 共享标签
        pair_weight: dict[tuple[int, int], float] = {}
        degree: dict[int, int] = {}
        for index, base in (("ent", ent_index), ("tag", tag_index)):
            for _key, mems in base.items():
                if len(mems) < 2:
                    continue
                mems = mems[:12]
                for i in range(len(mems)):
                    for j in range(i + 1, len(mems)):
                        pair = (min(mems[i], mems[j]), max(mems[i], mems[j]))
                        pair_weight[pair] = pair_weight.get(pair, 0.0) + (1.4 if index == "ent" else 0.8)
        ranked = sorted(pair_weight.items(), key=lambda kv: kv[1], reverse=True)
        for (a, b), w in ranked:
            if degree.get(a, 0) >= 10 and degree.get(b, 0) >= 10:
                continue
            await self.add_edge("memory", f"m{a}", "memory", f"m{b}", "semantic", round(min(w, 3.0), 2))
            degree[a] = degree.get(a, 0) + 1
            degree[b] = degree.get(b, 0) + 1
            stat["semantic"] += 1
        return stat

    async def graph(self, limit: int = 400, persona_id: str = "") -> dict[str, Any]:
        """面板图谱数据：记忆 + 实体 + 关系边（前端卡片渲染还要 cues/evidence/degree）。"""
        pwhere, pargs = self._persona_where(persona_id)
        mems = await (
            await self.db.execute(
                "SELECT id,summary,atom_type,strength,poignancy,tags,entities,cues,evidence,recalls,"
                "session_id,source_mode,occurred_at,updated_at FROM hf_memories "
                "WHERE status!='deleted'" + pwhere + " ORDER BY strength DESC LIMIT ?",
                (*pargs, limit),
            )
        ).fetchall()
        mem_ids = {f"m{r['id']}" for r in mems}
        if not mem_ids:
            return {"nodes": [], "links": [], "stats": {"memories": 0, "entities": 0, "links": 0}}
        placeholders = ",".join("?" * len(mem_ids))
        args = tuple(mem_ids)
        rows = list(
            await (
                await self.db.execute(
                    f"SELECT * FROM hf_edges WHERE status='active' AND src_type='memory' AND src_id IN ({placeholders})",
                    args,
                )
            ).fetchall()
        )
        rows += list(
            await (
                await self.db.execute(
                    f"SELECT * FROM hf_edges WHERE status='active' AND dst_type='memory' AND dst_id IN ({placeholders})",
                    args,
                )
            ).fetchall()
        )
        seen: set[int] = set()
        edge_rows: list[aiosqlite.Row] = []
        for r in rows:
            if r["id"] in seen:
                continue
            seen.add(r["id"])
            edge_rows.append(r)

        # 与这些记忆相连的实体（只取用到的，不把全库实体都画上来）
        # ⚠️ 实体可能在边的**任一侧**：写入端固定写 memory→entity，但老库 / 外部导入可能在
        #    entity→memory 方向；只收 dst 一侧会让那种边整条进不了图（方向不对称，埋雷）。
        ent_needed = sorted({
            (r["dst_id"] if r["dst_type"] == "entity" else r["src_id"])
            for r in edge_rows
            if r["dst_type"] == "entity" or r["src_type"] == "entity"
        })
        ent_ids: set[str] = set()
        ent_rows: list[aiosqlite.Row] = []
        ids = [int(x[1:]) for x in ent_needed if x[1:].isdigit()]
        if ids:
            q = ",".join("?" * len(ids))
            ent_rows = await (await self.db.execute(f"SELECT * FROM hf_entities WHERE id IN ({q})", tuple(ids))).fetchall()
            ent_ids = {f"e{r['id']}" for r in ent_rows}

        links: list[dict[str, Any]] = []
        seen_link: set[tuple[str, str, str]] = set()
        # ⚠️ 这里原来还有一段「实体↔实体」边的查询（人—群 / 话题—话题），已删除，原因：
        #    ① 全仓唯一的写边入口 add_edge() 的 src_type 恒为 'memory'，没有任何代码产出
        #       这类边 ⇒ 查询结果恒为空（真机实测 hf_edges 里 entity↔entity = 0 条），纯死路径；
        #    ② 它没有 persona 过滤，一旦将来真有这类边，任一人格的视图只要两端实体都出现
        #       就会被画出来 —— 等于给跨人格泄漏留了口子；
        #    ③ 「人在哪个群」这条横边由下面读时合成的 member_of 负责（三源并集、守隔离），
        #       不依赖这类边。
        for e in edge_rows:
            s, t, etype = e["src_id"], e["dst_id"], e["etype"]
            if s in mem_ids and t in ent_ids:
                pair = (s, t, "entity")
            elif t in mem_ids and s in ent_ids:
                pair = (t, s, "entity")
            elif s in mem_ids and t in mem_ids:
                pair = (s, t, etype)
            else:
                continue
            if pair in seen_link:
                continue
            seen_link.add(pair)
            links.append({"s": pair[0], "t": pair[1], "type": pair[2], "w": float(e["weight"] or 1.0)})

        degree: dict[str, int] = {}
        for link in links:
            degree[link["s"]] = degree.get(link["s"], 0) + 1
            degree[link["t"]] = degree.get(link["t"], 0) + 1

        # ————— 语义分层（代替旧的"连接度分位桶"） —————
        # 旧实现按度数分位把节点切成 L0..L4，层名没有任何含义，用户看不懂"为什么这些点在同一圈"
        # （业界调研：mem0 / Graphiti / GraphRAG / Neo4j / HippoRAG 等**没有一家**用分位桶当层级）。
        # 现在按**节点固有角色**分层，每一层一句话能解释：
        #   L0 = 人（跟你在说话的人）  L1 = 群 / 会话（话发生在哪）
        #   L2 = 话题 / 实体（聊到了什么）  L3 = 记忆（具体哪一条）
        ROLE_LEVEL = {"person": 0, "session": 1, "topic": 2, "cue": 2, "memory": 3}

        nodes: list[dict[str, Any]] = []
        for r in mems:
            nid = f"m{r['id']}"
            nodes.append(
                {
                    "id": nid,
                    "label": (r["summary"] or "")[:60],
                    "group": "memory",
                    "kind": r["atom_type"],
                    "strength": round(float(r["strength"] or 0), 3),
                    "poignancy": int(r["poignancy"] or 0),
                    "tags": _loads(r["tags"]),
                    "entities": _loads(r["entities"]),
                    "cues": _loads(r["cues"]),
                    "evidence": (r["evidence"] or "")[:300],
                    "recalls": int(r["recalls"] or 0),
                    "session": r["session_id"],
                    "source": r["source_mode"],
                    "occurred": r["occurred_at"],
                    "updated": r["updated_at"],
                    "degree": degree.get(nid, 0),
                    "role": "memory", "level": ROLE_LEVEL["memory"],
                }
            )
        for r in ent_rows:
            nid = f"e{r['id']}"
            nodes.append(
                {
                    "id": nid,
                    "label": r["name"],
                    "group": r["kind"],
                    "kind": r["kind"],
                    # 角色 → 层：person=人(0) / session=群(1) / topic=话题(2)
                    "role": "person" if r["kind"] == "person" else ("session" if r["kind"] == "session" else "topic"),
                    "level": ROLE_LEVEL.get(str(r["kind"]), 2),
                    # ⚠️ 不再用实体表的全局 weight：它跨人格累加，切到人格 B 也能间接看出人格 A 的次数（隔离泄漏）。
                    # 这里用**当前范围内**的度数，口径与人格子图一致。
                    "weight": degree.get(nid, 0),
                    "last": r["last_seen"],
                    "degree": degree.get(nid, 0),
                    # 编号：导入期实体名就是号（纯数字），这时它就是真号；
                    # 是昵称就留空，等下面「人/群派生」那一步按 session_id 回填。
                    "ident": str(r["name"] or "").strip() if str(r["name"] or "").strip().isdigit() else "",
                }
            )
        # ————— 派生「人 / 群」这一层（图里的"谁"）＋并入真实边 —————
        # 为什么要有：记忆里一直存着 session_id（形如 "示例群:FriendMessage:100000001"），这是
        # "这条记忆发生在跟谁 / 哪个群"的真实事实，却从来没建过节点 —— 图里因此完全没有"谁"这一层。
        # 派生出的边也是真关系（记忆 关于某人 / 发生在某群），不是编的。
        # 同名实体**合并**进来并按人 / 群就地定性：导入期把 QQ 号 / 昵称存成了 topic 实体，
        # 真机上度数最大的那个节点就是这么来的（在图上被显示成"话题"，纯属定性错误）。
        try:
            nm = await self.person_names_map()
        except Exception:                                     # noqa: BLE001 昵称读不到就用号，绝不编名字
            nm = {}

        def _nm(key: str, fallback: str) -> str:
            v = nm.get(key)
            if isinstance(v, dict):
                return str(v.get("name") or fallback)
            return str(v or fallback)

        ent_by_name = {str(n["label"]): n for n in nodes if str(n.get("id", "")).startswith("e")}
        seen_pair = {(lk["s"], lk["t"]) for lk in links}
        # (记忆, 节点) → 已存在的边对象：命中重复时用来**原地升级类型**，而不是简单丢弃。
        link_by_pair: dict[tuple[str, str], dict[str, Any]] = {(lk["s"], lk["t"]): lk for lk in links}
        peer_nodes: dict[str, dict[str, Any]] = {}
        peer_links: list[dict[str, Any]] = []
        for r in mems:
            # 用 session_kind()（而不是自己 split）——它认得 WebChat 的 `webchat!user!uuid`
            # 并把 id 归一成用户名；自己 split 的话会拿整段 `webchat!<用户名>!<uuid>` 去查昵称，
            # 查不到就退化成「用户 webchat!<用户名>!<uuid>」这种节点（人也没法跟昵称表对上）。
            _peer_kind, uid = session_kind(str(r["session_id"] or ""))
            if not uid or _peer_kind == "other":
                continue
            if _peer_kind == "group":
                role, label = "session", _nm(f"group:{uid}", f"群 {uid}")
            else:
                role, label = "person", _nm(f"user:{uid}", f"用户 {uid}")
            hit = ent_by_name.get(uid) or ent_by_name.get(label)
            if hit is not None:                               # 已有同名实体 → 就地改定性，不新建重复节点
                nid = hit["id"]
                hit["role"] = role
                hit["level"] = ROLE_LEVEL[role]
                hit["group"] = role
                # 记下这个节点的「真号」：昵称一旦覆盖 label，编号就再也取不出来了，
                # 而卡片要显示用户 ID / 群号、成员关系边也要靠它把人和群对上。
                if not hit.get("ident"):
                    hit["ident"] = uid
                if str(hit["label"]) == uid:
                    hit["label"] = label                      # 号 → 昵称（来自昵称表，不是猜的）
            else:
                nid = f"p:{uid}" if role == "person" else f"s:{uid}"
                peer_nodes.setdefault(nid, {
                    "id": nid, "label": label, "group": role, "kind": role, "role": role,
                    "level": ROLE_LEVEL[role], "degree": 0, "session": r["session_id"] or "",
                    "ident": uid,
                })
            mid = f"m{r['id']}"
            pair_type = "about" if role == "person" else "in_session"
            if (mid, nid) in seen_pair:
                # 这条记忆已经通过一条**实体边**连到同一个节点了。不新增重复边，但要把类型
                # **升级成语义更准的那个**：原来它被标成 'entity'（实体共指），而真实语义是
                # 「这条记忆**关于**这个人 / 发生在这个会话」—— 线色和图例都会跟着错。
                # 真机实测：几乎所有人—记忆线都被这么吞掉（全库 669 条 about 只显示出 11 条）。
                _up = link_by_pair.get((mid, nid))
                if _up is not None:
                    _up["type"] = pair_type
                continue
            seen_pair.add((mid, nid))
            _new = {"s": mid, "t": nid, "type": pair_type, "w": 1.0}
            peer_links.append(_new)
            link_by_pair[(mid, nid)] = _new

        links.extend(peer_links)
        nodes.extend(peer_nodes.values())

        # 同名消歧：真机上有两个不同的群都叫「测试」，不加短号在图上根本分不清。
        # ⚠️ 必须放在 `nodes.extend(peer_nodes.values())` **之后** ——
        #    派生出来的人/群（p:/s: 那批）恰恰是最容易撞名的一类
        #    （实测 e1209 与 s:100000003 两个节点都叫「测试」、都没后缀），
        #    放在前面等于把主干漏掉，等于没做消歧。
        # 后缀优先用**真号**（ident = QQ 号 / 群号）末 4 位，取不到才退回节点 id 末 4 位。
        lab: dict[str, list[dict[str, Any]]] = {}
        for n in nodes:
            if n.get("role") in ("person", "session"):
                lab.setdefault(str(n["label"]), []).append(n)
        for name, group in lab.items():
            if len(group) > 1:
                for n in group:
                    real = str(n.get("ident") or "")
                    if real:
                        suffix = real[-4:]
                    else:
                        nid = str(n["id"])
                        suffix = nid.split(":")[-1][-4:] if ":" in nid else nid[-4:]
                    n["label"] = f"{name} ·{suffix}"

        # ————— 人 ↔ 群：成员关系边（补上 L0 ↔ L1 这条横边）—————
        # 现状：人 / 群都只通过「记忆辐条」连到 L3，**人与他所在的群之间一条边都没有**，
        # 所以图上永远看不出「谁在哪个群」。业界做记忆/知识图谱都是显式关系边
        # （Graphiti 的 entity edge、mem0 的 connection），不靠同现去猜。
        # 数据源（三路并集，**只在两端节点都已经在本图里**时连边，绝不凭空造节点）：
        #   ① hf_corpus         —— 有 persona_id，跟着视角过滤走（最权威，但真机上群行 persona 为空）
        #   ② hf_messages       —— 逐条消息的「谁在本视图的哪个群发过言」，**按视图会话收窄**（守住隔离）
        #   ③ hf_person_names   —— 无 persona 列，只在「全库」范围并入
        # 只在**两端节点都已经在本图里**时连边 —— 绝不凭空造节点。
        ident_of: dict[tuple[str, str], str] = {}
        for nd in nodes:
            if nd.get("role") in ("person", "session") and nd.get("ident"):
                ident_of[(str(nd["role"]), str(nd["ident"]))] = str(nd["id"])
        member_links: list[dict[str, Any]] = []
        if ident_of:
            def _collect(user_key: str, group_key: str) -> None:
                p_node = ident_of.get(("person", str(user_key or "")))
                s_node = ident_of.get(("session", str(group_key or "")))
                if p_node and s_node and p_node != s_node:
                    member_pairs.add((p_node, s_node))

            member_pairs: set[tuple[str, str]] = set()
            pw, pa = self._persona_where(persona_id)
            try:
                mrows = await (
                    await self.db.execute(
                        "SELECT DISTINCT user_id, session_id FROM hf_corpus "
                        "WHERE is_bot = 0 AND user_id != '' AND session_id != ''" + pw,
                        tuple(pa),
                    )
                ).fetchall()
                for r in mrows:
                    parts = str(r["session_id"] or "").split(":")
                    if len(parts) < 3 or "group" not in parts[1].lower():
                        continue
                    _collect(r["user_id"], parts[2])
            except Exception as exc:                            # noqa: BLE001 成员边失败不该拖垮图谱
                logger.warning(f"[StarHeart] 成员关系边（hf_corpus）跳过：{exc}")
            # 逐条消息源：**只在「本视图已包含的群会话」里查**。这一条同时满足两件事 ——
            #   ① 依据是定义本身：「某人在这个群里发过言」⟹ 他在这个群里，不是按同现硬猜；
            #   ② 天然守着人格隔离：查询范围被收窄到本视图自己的会话，
            #      别的人格 / 别的机器人账号的会话根本不会被读进来
            #      （hf_messages 没有 persona 列，所以「按视图收窄」是它唯一安全的用法）。
            # 为什么必须有它：真机上「谁在哪个群」的事实只存在于全局表，
            # 而 hf_corpus 的群行 persona_id 全为空 → 人格视图里一条成员边都出不来
            # （实测：人格视图 0 条、全库 3 条，用户看到的仍是「人和群不连」）。
            scoped_sessions = sorted({
                str(r["session_id"])
                for r in mems
                if len(str(r["session_id"] or "").split(":")) >= 3
                and "group" in str(r["session_id"] or "").split(":")[1].lower()
            })
            for i in range(0, len(scoped_sessions), 400):       # 分批：避开 SQLite 变量数上限
                chunk = scoped_sessions[i:i + 400]
                try:
                    ph = ",".join("?" * len(chunk))
                    for r in (await (await self.db.execute(
                        "SELECT DISTINCT sender_id, session_id FROM hf_messages "
                        "WHERE is_bot = 0 AND sender_id IS NOT NULL AND sender_id != '' "
                        f"AND session_id IN ({ph})",
                        tuple(chunk),
                    )).fetchall()):
                        parts = str(r["session_id"] or "").split(":")
                        if len(parts) >= 3:
                            _collect(r["sender_id"], parts[2])
                except Exception as exc:                        # noqa: BLE001 老库可能没这张表
                    logger.debug(f"[StarHeart] 成员关系边（逐条消息）跳过：{exc}")
            if not persona_id:                                  # 次源无 persona 列 → 只在全库并
                try:
                    for r in (await (await self.db.execute(
                        "SELECT DISTINCT ident, group_id FROM hf_person_names "
                        "WHERE ident != '' AND group_id != ''"
                    )).fetchall()):
                        _collect(r["ident"], r["group_id"])
                except Exception as exc:                        # noqa: BLE001 老库可能没这张表
                    logger.debug(f"[StarHeart] 成员关系边（昵称表）跳过：{exc}")
            seen_pair2 = {(lk["s"], lk["t"]) for lk in links}
            for a, b in sorted(member_pairs):
                if (a, b) in seen_pair2:
                    continue
                seen_pair2.add((a, b))
                member_links.append({"s": a, "t": b, "type": "member_of", "w": 1.0})
        links.extend(member_links)

        for link in list(peer_links) + member_links:
            degree[link["s"]] = degree.get(link["s"], 0) + 1
            degree[link["t"]] = degree.get(link["t"], 0) + 1
        for n in nodes:                                       # 度数原本在节点构造前算过，这里统一收口
            n["degree"] = degree.get(n["id"], 0)
            if n.get("role") in ("person", "session"):
                n["weight"] = n["degree"]

        by_type: dict[str, int] = {}
        for link in links:
            by_type[link["type"]] = by_type.get(link["type"], 0) + 1
        isolated = [n["id"] for n in nodes if n["degree"] == 0 and n["role"] == "memory"]
        return {
            "nodes": nodes,
            "links": links,
            "stats": {
                "memories": len(mems),
                "entities": len(ent_rows),
                "links": len(links),
                "by_type": by_type,
                "isolated": len(isolated),
                "roles": {k: sum(1 for n in nodes if n.get("role") == k)
                          for k in ("person", "session", "topic", "memory")},
            },
        }

    async def _member_pair_count(self, mems: list[Any], persona_id: str) -> int:
        """人↔群成员关系的**原始对**计数（三源并集，与 graph() 同源同规则）。

        与 graph() 的唯一差别：graph() 还要求「两端节点都已经在本图里」才连线，
        这里不做节点存在性过滤 —— 体检要的是「数据里到底有多少这种关系」，
        图上画出来多少由 graph() 当时的节点集决定。两处数字用途不同，界面上分别标注
        （「落库关系边」vs「绘制关系」），避免同一页面两个同名字段打架。
        """
        pairs: set[tuple[str, str]] = set()
        pwhere, pargs = self._persona_where(persona_id)
        try:                                                    # 源① hf_corpus（有 persona 列）
            for r in (await (await self.db.execute(
                "SELECT DISTINCT user_id, session_id FROM hf_corpus "
                "WHERE is_bot = 0 AND user_id != '' AND session_id != ''" + pwhere,
                tuple(pargs),
            )).fetchall()):
                parts = str(r["session_id"] or "").split(":")
                if len(parts) >= 3 and "group" in parts[1].lower() and parts[2]:
                    pairs.add((str(r["user_id"]), parts[2]))
        except Exception as exc:                                # noqa: BLE001 体检不该因它失败
            logger.debug(f"[StarHeart] 成员对（hf_corpus）跳过：{exc}")
        scoped = sorted({                                       # 源② hf_messages（按本视图群会话收窄）
            str(r["session_id"]) for r in mems
            if len(str(r["session_id"] or "").split(":")) >= 3
            and "group" in str(r["session_id"] or "").split(":")[1].lower()
        })
        for i in range(0, len(scoped), 400):                    # 分批：避开 SQLite 变量数上限
            chunk = scoped[i:i + 400]
            try:
                ph = ",".join("?" * len(chunk))
                for r in (await (await self.db.execute(
                    "SELECT DISTINCT sender_id, session_id FROM hf_messages "
                    "WHERE is_bot = 0 AND sender_id IS NOT NULL AND sender_id != '' "
                    f"AND session_id IN ({ph})",
                    tuple(chunk),
                )).fetchall()):
                    parts = str(r["session_id"] or "").split(":")
                    if len(parts) >= 3 and parts[2] and r["sender_id"]:
                        pairs.add((str(r["sender_id"]), parts[2]))
            except Exception as exc:                            # noqa: BLE001 老库可能没这张表
                logger.debug(f"[StarHeart] 成员对（逐条消息）跳过：{exc}")
        if not persona_id:                                      # 源③ hf_person_names（无 persona 列，仅全库）
            try:
                for r in (await (await self.db.execute(
                    "SELECT DISTINCT ident, group_id FROM hf_person_names "
                    "WHERE ident != '' AND group_id != ''"
                )).fetchall()):
                    pairs.add((str(r["ident"]), str(r["group_id"])))
            except Exception as exc:                            # noqa: BLE001 老库可能没这张表
                logger.debug(f"[StarHeart] 成员对（昵称表）跳过：{exc}")
        return len(pairs)

    async def graph_stats(self, persona_id: str = "") -> dict[str, Any]:
        """关系链体检：面板要能一眼看出「图谱是不是又退化成一堆孤点」。"""
        pwhere, pargs = self._persona_where(persona_id)
        mems = await (
            await self.db.execute(
                "SELECT id, strength, session_id FROM hf_memories WHERE status!='deleted'" + pwhere,
                tuple(pargs),
            )
        ).fetchall()
        mem_ids = {f"m{r['id']}" for r in mems}
        out: dict[str, Any] = {
            "memories": len(mems),
            "entities": 0,
            "edges": 0,
            # ⚠️ 这里**不能**出现前端永远拿不到数据的类型：'causal'（因果）全仓没有任何
            #    写边路径，真机 hf_edges 里 0 条 —— 留着只会在图例/体检里显示一个永远为 0
            #    的分类，误导使用者以为存在因果边。已删。
            "by_type": {"entity": 0, "temporal": 0, "semantic": 0,
                        "about": 0, "in_session": 0, "member_of": 0},
            "isolated": 0,
            "avg_degree": 0.0,
            "components": 0,
            "largest_component": 0,
            "hubs": [],
        }
        # 注意：成员关系（人 ↔ 群）是**读时合成**的、不落 hf_edges（那张表没有 persona 列，
        # 落库会让人与群的关系在所有人格视图里互相可见，破坏人格隔离）。
        # 它的计数在函数末尾收口处再写进 by_type —— 这里不能写，
        # 因为下面会 `by_type = {k: 0 for k in out["by_type"]}` 重建一次把值清零。
        if not mem_ids:
            return out
        rows = await (await self.db.execute("SELECT * FROM hf_edges WHERE status='active'")).fetchall()
        degree: dict[str, int] = {}
        parent: dict[str, str] = {m: m for m in mem_ids}

        def find(x: str) -> str:
            while parent.get(x, x) != x:
                parent[x] = parent.get(parent[x], parent[x])
                x = parent[x]
            return x

        ent_ids: set[str] = set()
        by_type = {k: 0 for k in out["by_type"]}
        for r in rows:
            s, t, etype = r["src_id"], r["dst_id"], r["etype"]
            if s in mem_ids and t in mem_ids:
                by_type[etype] = by_type.get(etype, 0) + 1
                degree[s] = degree.get(s, 0) + 1
                degree[t] = degree.get(t, 0) + 1
                ra, rb = find(s), find(t)
                if ra != rb:
                    parent[rb] = ra
            elif s in mem_ids and r["dst_type"] == "entity":
                by_type["entity"] = by_type.get("entity", 0) + 1
                degree[s] = degree.get(s, 0) + 1
                ent_ids.add(t)
            elif t in mem_ids and r["src_type"] == "entity":
                by_type["entity"] = by_type.get("entity", 0) + 1
                degree[t] = degree.get(t, 0) + 1
                ent_ids.add(s)
        comps: dict[str, int] = {}
        for m in mem_ids:
            root = find(m)
            comps[root] = comps.get(root, 0) + 1
        isolated = sum(1 for m in mem_ids if degree.get(m, 0) == 0)
        hubs = sorted(
            ({"id": m, "degree": d} for m, d in degree.items() if m in mem_ids),
            key=lambda x: x["degree"],
            reverse=True,
        )[:5]
        labels = []
        if hubs:
            hub_ids = tuple(int(h["id"][1:]) for h in hubs)
            labels = await (
                await self.db.execute(
                    f"SELECT id, summary FROM hf_memories WHERE id IN ({','.join('?' * len(hub_ids))})",
                    hub_ids,
                )
            ).fetchall()
        by_id = {f"m{r['id']}": r["summary"] for r in labels}
        for h in hubs:
            h["label"] = (by_id.get(h["id"]) or "")[:40]
        # 读时合成、不落库的三种边（about / in_session / member_of）也要进体检 ——
        # 它们不在 hf_edges 里，不补的话界面上永远是 0（用户看到的「人和群不连」就有这一份）。
        # 口径与 graph() 一致：
        #   · about / in_session：每条 session_id 有效的记忆各贡献一条（群会话→in_session，否则 about）
        #   · member_of：三源并集（hf_corpus + 本视图群会话的逐条消息 + 昵称表仅全库）
        for r in mems:
            parts = str(r["session_id"] or "").split(":")
            if len(parts) >= 3 and parts[2]:
                by_type["in_session" if "group" in parts[1].lower() else "about"] += 1
        try:
            by_type["member_of"] = await self._member_pair_count(mems, persona_id)
        except Exception as exc:                                # noqa: BLE001 体检不该因它失败
            logger.debug(f"[StarHeart] 成员关系计数跳过：{exc}")
        out.update(
            {
                "entities": len(ent_ids),
                # 「落库关系边」：真正落在 hf_edges 里的三类（entity / temporal / semantic）。
                # about / in_session / member_of 是读时合成的派生量，只在 by_type 里体现，
                # 不混进这个数 —— 界面上这个数字的标签是「落库关系边」，
                # 跟图谱面板的「绘制关系」（当前视图实际画出的条数）区分开，避免同名对不上。
                "edges": by_type["entity"] + by_type["temporal"] + by_type["semantic"],
                "by_type": by_type,
                "isolated": isolated,
                "avg_degree": round(sum(degree.get(m, 0) for m in mem_ids) / max(1, len(mem_ids)), 2),
                "components": len(comps),
                "largest_component": max(comps.values()) if comps else 0,
                "hubs": hubs,
            }
        )
        return out

    # ---------------- 好感 ----------------
    async def list_favours(self) -> list[dict[str, Any]]:
        rows = await (await self.db.execute("SELECT * FROM hf_favours ORDER BY daily DESC")).fetchall()
        out = [dict(r) for r in rows]
        for d in out:
            try:
                d["evidence"] = json.loads(d.get("evidence") or "[]")
            except (TypeError, ValueError):
                d["evidence"] = []
        return out

    async def upsert_favour(self, user_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        ts = now()
        row = await (await self.db.execute("SELECT * FROM hf_favours WHERE user_id=?", (user_id,))).fetchone()
        if row is None:
            await self.db.execute(
                "INSERT INTO hf_favours(user_id,name,session_id,daily,lifetime,zone,evidence,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    user_id,
                    patch.get("name", user_id),
                    patch.get("session_id", ""),
                    float(patch.get("daily", 0)),
                    float(patch.get("lifetime", 0)),
                    patch.get("zone", "NEUTRAL"),
                    json.dumps(patch.get("evidence", []), ensure_ascii=False),
                    ts,
                ),
            )
        else:
            daily = float(patch.get("daily", row["daily"]))
            lifetime = float(patch.get("lifetime", row["lifetime"]))
            zone = patch.get("zone") or engine.zone_of(daily)
            # evidence 也要写：它是「好感日上限」的日记账（[\"YYYY-MM-DD +N\"]），
            # 只在 INSERT 时写会导致日上限从第三条起就算错（前两条是"第一条+本次"，见 CHANGELOG 0.19.1）。
            patch_ev = patch.get("evidence")
            await self.db.execute(
                "UPDATE hf_favours SET daily=?, lifetime=?, zone=?, updated_at=?,"
                " name=COALESCE(NULLIF(?,''),name), evidence=COALESCE(?, evidence) WHERE user_id=?",
                (
                    daily,
                    lifetime,
                    zone,
                    ts,
                    patch.get("name", ""),
                    (json.dumps(patch_ev, ensure_ascii=False) if patch_ev is not None else None),
                    user_id,
                ),
            )
        await self.db.commit()
        fresh = await (await self.db.execute("SELECT * FROM hf_favours WHERE user_id=?", (user_id,))).fetchone()
        out = dict(fresh) if fresh else {}
        # evidence 与 list_favours() 保持一致：那儿解析成数组，这儿也必须是数组，
        # 否则同一字段在"写回值"和"列表值"里类型不同（前端.slice() 会报错）。
        if "evidence" in out:
            try:
                out["evidence"] = json.loads(out.get("evidence") or "[]")
            except (TypeError, ValueError):
                out["evidence"] = []
        return out

    @staticmethod
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

    # ---------------- 表达（S4：hf_expr 新表；hf_habits 只读保留、不再写） ----------------
    async def list_habits(self, include_archived: bool = False) -> list[dict[str, Any]]:
        """表达库全量（注入侧与面板共用）。

        use_count / last_used / used_24h / injected_24h 全部**查询聚合**（拍-5：不设回写列，
        真源 = hf_expr_use_log）→ 不会出现「回写列与流水漂移」。uses = use_count 的兼容别名。
        """
        cut = now() - EXPR_WINDOW_SECONDS
        where = "" if include_archived else " WHERE e.status!='archived'"
        rows = await (
            await self.db.execute(
                "SELECT e.*,"
                " (SELECT COUNT(*) FROM hf_expr_use_log u WHERE u.expr_id=e.id AND u.emit_ok=1) AS use_count,"
                " (SELECT MAX(u.used_at) FROM hf_expr_use_log u WHERE u.expr_id=e.id AND u.emit_ok=1) AS last_used,"
                " (SELECT COUNT(*) FROM hf_expr_use_log u WHERE u.expr_id=e.id AND u.emit_ok=1 AND u.used_at>=?) AS used_24h,"
                " (SELECT COUNT(*) FROM hf_expr_use_log u WHERE u.expr_id=e.id AND u.used_at>=?) AS injected_24h"
                " FROM hf_expr e" + where + " ORDER BY e.enabled DESC, e.id DESC",
                (cut, cut),
            )
        ).fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            d = dict(r)
            d["cues"] = _jlist(d.get("cues"))
            d["source_ids"] = _jobj_list(d.get("source_ids"))
            d["evidence_snap"] = _jobj_list(d.get("evidence_snap"))
            d["use_count"] = int(d.get("use_count") or 0)
            d["uses"] = d["use_count"]                      # 兼容旧字段名（面板曾读 uses）
            out.append(d)
        return out

    async def upsert_expr(self, expr: dict[str, Any]) -> int:
        """写入/更新一条表达（面板「手加」与就地编辑走这里）。id 在 = 就地改，否则新增。"""
        ts = now()
        eid = int(expr.get("id") or 0)
        norm_key = str(expr.get("norm_key") or "").strip() or expr_norm_key(expr.get("scene", ""), expr.get("way", ""))
        if eid:
            await self.db.execute(
                "UPDATE hf_expr SET scene=?, way=?, cues=?, scope=?, enabled=?, norm_key=?,"
                " checked_state=?, ai_reason=?, updated_at=? WHERE id=?",
                (
                    str(expr.get("scene") or "")[:40],
                    str(expr.get("way") or "")[:120],
                    json.dumps(expr.get("cues") or [], ensure_ascii=False),
                    str(expr.get("scope") or "全局"),
                    1 if expr.get("enabled", True) else 0,
                    norm_key,
                    str(expr.get("checked_state") or "ai_pending"),
                    str(expr.get("ai_reason") or ""),
                    ts,
                    eid,
                ),
            )
            await self.db.commit()
            return eid
        cur = await self.db.execute(
            "INSERT INTO hf_expr(scene,way,cues,scope,enabled,status,norm_key,duplicate_of,checked_state,"
            " ai_reason,source_ids,evidence_snap,cooldown_until,outcome_score,gate_override,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                str(expr.get("scene") or "")[:40],
                str(expr.get("way") or "")[:120],
                json.dumps(expr.get("cues") or [], ensure_ascii=False),
                str(expr.get("scope") or "全局"),
                1 if expr.get("enabled", True) else 0,
                str(expr.get("status") or "active"),
                norm_key,
                expr.get("duplicate_of"),
                str(expr.get("checked_state") or "ai_pending"),
                str(expr.get("ai_reason") or ""),
                json.dumps(expr.get("source_ids") or [], ensure_ascii=False),
                json.dumps(expr.get("evidence_snap") or [], ensure_ascii=False),
                expr.get("cooldown_until"),
                expr.get("outcome_score"),
                str(expr.get("gate_override") or ""),
                ts,
                ts,
            ),
        )
        await self.db.commit()
        return int(cur.lastrowid or 0)

    async def find_expr(self, scope: str, norm_key: str) -> dict[str, Any] | None:
        """按 (scope, norm_key) 找一条（提取合并用：命中就合并，不新增行）。"""
        row = await (
            await self.db.execute(
                "SELECT * FROM hf_expr WHERE scope=? AND norm_key=? ORDER BY id DESC LIMIT 1",
                (str(scope or ""), str(norm_key or "")),
            )
        ).fetchone()
        return dict(row) if row else None

    async def merge_expr(self, expr: dict[str, Any]) -> tuple[int, bool]:
        """提取路径的写入（S4-2）。返回 (id, 是否新增行)。

        命中同 (scope, norm_key) 时只更新说法并**累加**出处/证据：
        checked_state / enabled / status 一律保持原值 —— 学习回写绝不回退审核态（上游 G2 教训）。
        """
        norm_key = str(expr.get("norm_key") or "").strip() or expr_norm_key(expr.get("scene", ""), expr.get("way", ""))
        eid = int(expr.get("id") or 0)
        row: dict[str, Any] | None = None
        if eid:
            hit = await (await self.db.execute("SELECT * FROM hf_expr WHERE id=?", (eid,))).fetchone()
            row = dict(hit) if hit else None
        if row is None:
            row = await self.find_expr(str(expr.get("scope") or "全局"), norm_key)
        if row is None:
            return await self.upsert_expr({**expr, "norm_key": norm_key}), True
        cues = _jlist(row.get("cues"))
        for c in [str(x) for x in (expr.get("cues") or []) if str(x or "").strip()]:
            if c not in cues:
                cues.append(c)
        src = _jobj_list(row.get("source_ids"))
        seen = {(str(x.get("corpus_id") or ""), str(x.get("text") or "")) for x in src}
        for x in (expr.get("source_ids") or []):
            k = (str(x.get("corpus_id") or ""), str(x.get("text") or ""))
            if k not in seen:
                src.append(x)
                seen.add(k)
        ev = _jobj_list(row.get("evidence_snap"))
        seen_ev = {(str(x.get("text") or ""), str(x.get("corpus_id") or "")) for x in ev}
        for x in (expr.get("evidence_snap") or []):
            k = (str(x.get("text") or ""), str(x.get("corpus_id") or ""))
            if k not in seen_ev:
                ev.append(x)
                seen_ev.add(k)
        await self.db.execute(
            "UPDATE hf_expr SET scene=?, way=?, cues=?, source_ids=?, evidence_snap=?, updated_at=? WHERE id=?",
            (
                str(expr.get("scene") or row.get("scene") or "")[:40],
                str(expr.get("way") or row.get("way") or "")[:120],
                json.dumps(cues[:20], ensure_ascii=False),
                json.dumps(src[:20], ensure_ascii=False),
                json.dumps(ev[:20], ensure_ascii=False),
                now(),
                int(row["id"]),
            ),
        )
        await self.db.commit()
        return int(row["id"]), False

    # ---------------- 审核状态机 + 「不再学」名单（S4-2） ----------------
    async def set_expr_review(self, ids: list[int], action: str, reason: str = "", actor: str = "panel") -> dict[str, Any]:
        """approve → user_ok（并启用）；reject → user_reject（停用）+ 写「不再学」名单。

        驳回是软语义（可恢复）：行保留，移出名单 + status 回 active 就回来了。
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
            return {"changed": 0, "banned": 0, "ids": []}
        marks = ",".join("?" for _ in picked)
        rows = await (await self.db.execute(f"SELECT * FROM hf_expr WHERE id IN ({marks})", picked)).fetchall()
        stamp = now()
        banned = 0
        for r in rows:
            row = dict(r)
            if str(action) == "approve":
                await self.db.execute(
                    "UPDATE hf_expr SET checked_state='user_ok', enabled=1, ai_reason=?, updated_at=? WHERE id=?",
                    (str(reason or row.get("ai_reason") or ""), stamp, int(row["id"])),
                )
            else:
                await self.db.execute(
                    "UPDATE hf_expr SET checked_state='user_reject', enabled=0, ai_reason=?, updated_at=? WHERE id=?",
                    (str(reason or ""), stamp, int(row["id"])),
                )
                banned += await self.add_ban(
                    key=f"{row.get('scene') or ''}|{row.get('way') or ''}",
                    norm_key=str(row.get("norm_key") or ""),
                    reason=str(reason or "驳回"),
                    actor=actor,
                    note="review:reject",
                )
        await self.db.commit()
        return {"changed": len(rows), "banned": banned, "ids": [int(dict(r)["id"]) for r in rows]}

    async def add_ban(self, *, key: str, norm_key: str, reason: str = "", actor: str = "panel", note: str = "") -> int:
        """写「不再学」名单（同 norm_key 覆盖：最新理由/时间赢）。返回 1/0。"""
        nk = str(norm_key or "").strip() or expr_norm_key(key, key)
        if not nk:
            return 0
        await self.db.execute(
            "INSERT OR REPLACE INTO hf_expr_banned(key,norm_key,reason,ts,actor,note) VALUES(?,?,?,?,?,?)",
            (str(key or "")[:120], nk, str(reason or "")[:200], now(), str(actor or "panel"), str(note or "")[:200]),
        )
        await self.db.commit()
        return 1

    async def list_bans(self, limit: int = 200) -> list[dict[str, Any]]:
        rows = await (
            await self.db.execute(
                "SELECT * FROM hf_expr_banned ORDER BY ts DESC LIMIT ?", (max(1, min(int(limit), 500)),)
            )
        ).fetchall()
        return [dict(r) for r in rows]

    async def banned_keys(self) -> set[str]:
        """名单键集合（提取路径先过它 → 「删了还会再学」的洞在此封死）。"""
        try:
            rows = await (await self.db.execute("SELECT norm_key FROM hf_expr_banned")).fetchall()
        except Exception as exc:                            # noqa: BLE001
            logger.warning(f"[StarHeart] 「不再学」名单读取跳过：{exc}")
            return set()
        return {str(r["norm_key"] or "") for r in rows if str(r["norm_key"] or "")}

    async def remove_ban(self, norm_key: str) -> dict[str, Any]:
        """移出名单（v8「可移出」）：同键可再学；被软删的同键行一并恢复。"""
        nk = str(norm_key or "").strip()
        if not nk:
            return {"removed": 0, "restored": 0}
        cur = await self.db.execute("DELETE FROM hf_expr_banned WHERE norm_key=?", (nk,))
        back = await self.db.execute(
            "UPDATE hf_expr SET status='active', enabled=1, updated_at=? WHERE norm_key=? AND status='archived'",
            (now(), nk),
        )
        await self.db.commit()
        return {"removed": int(cur.rowcount or 0), "restored": int(back.rowcount or 0)}

    async def archive_expr(self, hid: int, actor: str = "panel") -> dict[str, Any]:
        """「删除」的真语义（S4-2 + 拍-14）：软删（status='archived' + 停用）+ 写「不再学」名单。

        真删归数据治理（「只清表达」）。行还在 → 移出名单即恢复。
        """
        row = await (await self.db.execute("SELECT * FROM hf_expr WHERE id=?", (int(hid),))).fetchone()
        if not row:
            return {"archived": 0, "banned": 0}
        d = dict(row)
        await self.db.execute(
            "UPDATE hf_expr SET status='archived', enabled=0, updated_at=? WHERE id=?", (now(), int(hid))
        )
        await self.db.commit()
        n = await self.add_ban(
            key=f"{d.get('scene') or ''}|{d.get('way') or ''}",
            norm_key=str(d.get("norm_key") or ""),
            reason="手动删除",
            actor=actor,
            note="delete",
        )
        return {"archived": 1, "banned": n}

    async def expr_review_queue(self, limit: int = 200) -> dict[str, Any]:
        """待审行 + 四种计数（GET expr/review）：审核队列的数据源。"""
        rows = await (
            await self.db.execute(
                "SELECT checked_state, COUNT(*) AS n FROM hf_expr WHERE status!='archived' GROUP BY checked_state"
            )
        ).fetchall()
        counts = {str(r["checked_state"] or ""): int(r["n"] or 0) for r in rows}
        pend = await (
            await self.db.execute(
                "SELECT id, scene, way, scope, checked_state, ai_reason, created_at, updated_at FROM hf_expr"
                " WHERE status!='archived' AND checked_state='ai_pending' ORDER BY id DESC LIMIT ?",
                (max(1, min(int(limit), 500)),),
            )
        ).fetchall()
        ban = await (await self.db.execute("SELECT COUNT(*) AS n FROM hf_expr_banned")).fetchone()
        return {
            "items": [dict(r) for r in pend],
            "counts": {
                "pending": counts.get("ai_pending", 0),
                "ok": counts.get("user_ok", 0),
                "rejected": counts.get("user_reject", 0),
                "banned": int(ban["n"] or 0) if ban else 0,
            },
        }

    # ---------------- 学习诊断（S4-4） ----------------
    async def log_expr_run(self, rec: dict[str, Any]) -> None:
        """每次提取运行落一行；写失败只 WARNING（绝不挡提取）。"""
        try:
            await self.db.execute(
                "INSERT INTO hf_expr_run_log(ts,scanned,saved,skipped_reason,model,ok,persona_id) VALUES(?,?,?,?,?,?,?)",
                (
                    float(rec.get("ts") or now()),
                    int(rec.get("scanned") or 0),
                    int(rec.get("saved") or 0),
                    str(rec.get("skipped_reason") or ""),
                    str(rec.get("model") or ""),
                    1 if rec.get("ok", True) else 0,
                    str(rec.get("persona_id") or ""),
                ),
            )
            await self.db.commit()
        except Exception as exc:                            # noqa: BLE001
            logger.warning(f"[StarHeart] 学习诊断写入跳过：{exc}")

    async def expr_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = await (
            await self.db.execute(
                "SELECT * FROM hf_expr_run_log ORDER BY ts DESC LIMIT ?", (max(1, min(int(limit), 200)),)
            )
        ).fetchall()
        return [dict(r) for r in rows]

    # ---------------- 出站对账（S4-3：use_count = 出站子串命中） ----------------
    async def reconcile_expr_use(
        self, session_id: str, text: str, *, sent_ok: bool | None = True, window: float = 1800.0
    ) -> dict[str, int]:
        """把本会话**待对账**的注入行按「出站子串命中」判掉。

        命中（出站原文里出现这条的 way 或 scene）→ emit_ok=1；没命中 → 0；发送失败（sent_ok=False）→
        全判 0（没发出去不算用过）；sent_ok=None = first_seg（发出但拿不到回执）照样参与判定
        （拍-12：NULL ≠ 没发出，排除它 = 系统性低估）。只注入、不产出站的行保持 NULL 且**不计数**。
        """
        body = str(text or "")
        if sent_ok is not False and not body.strip():
            # 没有出站原文可判（表情包 / 语音这类段）→ 留 NULL 不瞎判（NULL = 未对账、不计入 use_count）
            return {"reconciled": 0, "hit": 0, "miss": 0}
        rows = await (
            await self.db.execute(
                "SELECT u.id AS uid, u.expr_id AS eid, e.scene AS scene, e.way AS way"
                " FROM hf_expr_use_log u LEFT JOIN hf_expr e ON e.id=u.expr_id"
                " WHERE u.session_id=? AND u.emit_ok IS NULL AND u.used_at>=?",
                (str(session_id or ""), now() - max(60.0, float(window))),
            )
        ).fetchall()
        hit = miss = 0
        for r in rows:
            ok = bool(sent_ok is not False and body)
            if ok:
                way = str(r["way"] or "")
                scene = str(r["scene"] or "")
                ok = bool((way and way in body) or (scene and scene in body))
            try:
                await self.db.execute(
                    "UPDATE hf_expr_use_log SET emit_ok=? WHERE id=?", (1 if ok else 0, int(r["uid"]))
                )
            except Exception as exc:                        # noqa: BLE001
                logger.warning(f"[StarHeart] 表达出站对账写回跳过：{exc}")
            hit += 1 if ok else 0
            miss += 0 if ok else 1
        if rows:
            await self.db.commit()
        return {"reconciled": len(rows), "hit": hit, "miss": miss}

    # ---------------- 语料出处（E3） ----------------
    async def referenced_corpus_ids(self) -> set[int]:
        """被**有效表达**引用的语料 id 集合（拍-15：prune 用查询排除，不给 hf_corpus 加列）。"""
        out: set[int] = set()
        try:
            rows = await (
                await self.db.execute("SELECT source_ids, evidence_snap FROM hf_expr WHERE status!='archived'")
            ).fetchall()
        except Exception as exc:                            # noqa: BLE001
            logger.warning(f"[StarHeart] 出处引用读取跳过：{exc}")
            return out
        for r in rows:
            for item in _jobj_list(r["source_ids"]) + _jobj_list(r["evidence_snap"]):
                try:
                    cid = int(item.get("corpus_id") or 0)
                except (TypeError, ValueError):
                    cid = 0
                if cid:
                    out.add(cid)
        return out

    async def corpus_ref_map(self, ids: list[int]) -> dict[int, dict[str, Any]]:
        """语料 id → {message_id, ext_id, session_id}（出处对树用）。"""
        picked = [int(x) for x in ids if int(x or 0) > 0]
        if not picked:
            return {}
        marks = ",".join("?" for _ in picked)
        rows = await (
            await self.db.execute(f"SELECT * FROM hf_corpus_ref WHERE corpus_id IN ({marks})", picked)
        ).fetchall()
        return {int(r["corpus_id"]): dict(r) for r in rows}

    async def corpus_rows_by_ids(self, ids: list[int]) -> dict[int, dict[str, Any]]:
        """语料 id → 语料行（出处回查：还在不在、原话是否逐字相等）。"""
        picked = [int(x) for x in ids if int(x or 0) > 0]
        if not picked:
            return {}
        marks = ",".join("?" for _ in picked)
        rows = await (
            await self.db.execute(f"SELECT * FROM hf_corpus WHERE id IN ({marks})", picked)
        ).fetchall()
        return {int(r["id"]): dict(r) for r in rows}

    async def corpus_unused_count(self) -> int:
        """还没学过的群友/好友语料条数（诊断块「可学 X 条未学」）。"""
        row = await (
            await self.db.execute("SELECT COUNT(*) AS n FROM hf_corpus WHERE is_bot=0 AND used=0")
        ).fetchone()
        return int(row["n"] or 0) if row else 0

    # ---------------- S5 黑话（hf_jargon + 命中日志） ----------------
    async def jargon_upsert(self, rec: dict[str, Any]) -> tuple[int, bool]:
        """写一条黑话，返回 (id, 是否新建)。(term, scope) 逻辑唯一 —— 应用层判重（拍-18 冻结口径）。

        推断路径与手工路径共用；denied 的行也占位（= 不再推断），所以判重**不看 status**。
        """
        term = str(rec.get("term") or "").strip()[:JARGON_TERM_MAX_CHARS]
        if not term:
            return 0, False
        scope = str(rec.get("scope") or "全局").strip() or "全局"
        stamp = now()
        ev = rec.get("evidence_json", rec.get("evidence"))
        if isinstance(ev, (list, dict)):
            ev = json.dumps(ev, ensure_ascii=False)
        ev = str(ev or "[]")
        row = await (
            await self.db.execute("SELECT id FROM hf_jargon WHERE term=? AND scope=? LIMIT 1", (term, scope))
        ).fetchone()
        if row:
            jid = int(row["id"])
            sets = ["evidence_json=?", "updated_at=?"]
            args: list[Any] = [ev, stamp]
            if str(rec.get("meaning") or "").strip():
                sets.insert(0, "meaning=?")
                args.insert(0, str(rec["meaning"])[:JARGON_MEANING_MAX_CHARS])
            if rec.get("status"):
                sets.insert(0, "status=?")
                args.insert(0, str(rec["status"]))
            await self.db.execute(f"UPDATE hf_jargon SET {', '.join(sets)} WHERE id=?", args + [jid])
            await self.db.commit()
            return jid, False
        cur = await self.db.execute(
            "INSERT INTO hf_jargon(term,meaning,evidence_json,status,scope,enabled,hit_count,last_hit_at,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?,0,NULL,?,?)",
            (
                term,
                str(rec.get("meaning") or "")[:JARGON_MEANING_MAX_CHARS],
                ev,
                str(rec.get("status") or "pending"),
                scope,
                1 if rec.get("enabled", 1) else 0,
                stamp,
                stamp,
            ),
        )
        await self.db.commit()
        return int(cur.lastrowid or 0), True

    async def jargon_match_pool(self, scopes: list[str]) -> list[dict[str, Any]]:
        """当前会话可用的黑话候选池 —— scope 过滤**下推 SQL**（拍-17），走 (scope, term) 索引。

        只收 enabled 且非 denied 的行（denied = 已判定不是黑话：不再注入、不再重推断）。
        """
        picked = [str(s).strip() for s in (scopes or []) if str(s or "").strip()]
        if not picked:
            return []
        marks = ",".join("?" for _ in picked)
        rows = await (
            await self.db.execute(
                "SELECT id,term,meaning,status,scope,hit_count FROM hf_jargon"
                " WHERE enabled=1 AND status IN ('pending','confirmed')"
                f" AND scope IN ({marks})",
                picked,
            )
        ).fetchall()
        return [dict(r) for r in rows]

    async def jargon_log_hits(self, rows: list[dict[str, Any]]) -> int:
        """命中日志（验证物①）：每命中一词写一行 + 该词 hit_count/last_hit_at 递增。

        失败只 WARNING（记账不许影响注入）；返回写入行数。点一下 message = 每词一行（同轮多词多行）。
        """
        if not rows:
            return 0
        n = 0
        try:
            bump: dict[int, list[float]] = {}
            for r in rows:
                try:
                    tid = int(r.get("term_id") or 0)
                except (TypeError, ValueError):
                    continue
                if tid <= 0:
                    continue
                ts = float(r.get("ts") or now())
                await self.db.execute(
                    "INSERT INTO hf_jargon_hit(term_id,session_id,msg,ts,injected) VALUES(?,?,?,?,?)",
                    (
                        tid,
                        str(r.get("session_id") or ""),
                        str(r.get("msg") or "")[:JARGON_HIT_MSG_MAX_CHARS],
                        ts,
                        1 if r.get("injected") else 0,
                    ),
                )
                n += 1
                agg = bump.setdefault(tid, [0.0, ts])
                agg[0] += 1
                agg[1] = max(agg[1], ts)
            for tid, (cnt, last) in bump.items():
                await self.db.execute(
                    "UPDATE hf_jargon SET hit_count=hit_count+?, last_hit_at=? WHERE id=?",
                    (int(cnt), float(last), tid),
                )
            await self.db.commit()
        except Exception as exc:                            # noqa: BLE001 记账失败不算错
            logger.warning(f"[StarHeart] 黑话命中记账跳过：{exc}")
        return n

    async def jargon_list(self, *, status: str = "", q: str = "", scope: str = "") -> list[dict[str, Any]]:
        """黑话清单（面板块②数据源）：过滤全参数化；evidence_json 解析成 evidence 列表。"""
        where: list[str] = []
        args: list[Any] = []
        if str(status or "").strip():
            where.append("status=?")
            args.append(str(status).strip())
        if str(scope or "").strip():
            where.append("scope=?")
            args.append(str(scope).strip())
        if str(q or "").strip():
            where.append("(term LIKE ? OR meaning LIKE ?)")
            like = f"%{str(q).strip()}%"
            args += [like, like]
        clause = ("WHERE " + " AND ".join(where)) if where else ""
        rows = await (
            await self.db.execute(
                f"SELECT * FROM hf_jargon {clause} ORDER BY hit_count DESC, id DESC", args
            )
        ).fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            d = dict(r)
            d["evidence"] = _jobj_list(r["evidence_json"])
            out.append(d)
        return out

    async def jargon_hit_stats(self) -> dict[int, dict[str, Any]]:
        """每个词的命中聚合：总次数 / 涉及会话数 / 最近一次（面板「出现 / 最近命中」）。"""
        rows = await (
            await self.db.execute(
                "SELECT term_id, COUNT(*) AS n, COUNT(DISTINCT session_id) AS sess, MAX(ts) AS last_ts"
                " FROM hf_jargon_hit GROUP BY term_id"
            )
        ).fetchall()
        return {
            int(r["term_id"]): {"hits": int(r["n"] or 0), "sessions": int(r["sess"] or 0),
                                "last_ts": float(r["last_ts"] or 0)}
            for r in rows
        }

    async def jargon_expr_map(self) -> dict[str, list[dict[str, Any]]]:
        """「未进入回复库」检查：黑话词出现在表达库 way/scene 的行（instr 精确子串，无通配语义）。

        口径 = round1-C1 §4 的**唯一正向检查**：黑话词不得出现在 hf_expr 里（只做理解、不做表达）。
        """
        rows = await (
            await self.db.execute(
                "SELECT j.id AS term_id, j.term AS term, e.id AS expr_id, e.scene AS scene, e.way AS way"
                " FROM hf_jargon j JOIN hf_expr e"
                " ON (instr(e.way, j.term) > 0 OR instr(e.scene, j.term) > 0)"
                " WHERE e.status != 'archived'"
            )
        ).fetchall()
        out: dict[str, list[dict[str, Any]]] = {}
        for r in rows:
            out.setdefault(str(r["term"]), []).append(
                {"term_id": int(r["term_id"]), "term": str(r["term"]),
                 "expr_id": int(r["expr_id"]), "scene": str(r["scene"] or ""), "way": str(r["way"] or "")}
            )
        return out

    async def jargon_hits_list(self, term_id: int, limit: int = 20) -> list[dict[str, Any]]:
        """命中日志（看原话）：按时间倒序。term_id 参数化；越界收敛。"""
        rows = await (
            await self.db.execute(
                "SELECT * FROM hf_jargon_hit WHERE term_id=? ORDER BY ts DESC LIMIT ?",
                (int(term_id), max(1, min(int(limit), 200))),
            )
        ).fetchall()
        return [dict(r) for r in rows]

    async def jargon_get(self, jid: int) -> dict[str, Any] | None:
        row = await (await self.db.execute("SELECT * FROM hf_jargon WHERE id=?", (int(jid),))).fetchone()
        if not row:
            return None
        d = dict(row)
        d["evidence"] = _jobj_list(row["evidence_json"])
        return d

    async def jargon_set_status(self, jid: int, status: str, *, enabled: int | None = None) -> int:
        """人工闸门（洞③）：pending → confirmed / denied。denied 同时置 enabled=0（归档语义）。"""
        sets = ["status=?", "updated_at=?"]
        args: list[Any] = [str(status), now()]
        if enabled is not None:
            sets.append("enabled=?")
            args.append(int(enabled))
        cur = await self.db.execute(f"UPDATE hf_jargon SET {', '.join(sets)} WHERE id=?", args + [int(jid)])
        await self.db.commit()
        return int(cur.rowcount or 0)

    async def jargon_update(self, jid: int, *, term: str | None = None, meaning: str | None = None,
                            enabled: int | None = None) -> int:
        """改词条 / 改释义 / 停用-启用（都是人工动作）。term 改了要防它撞上现存 (term, scope) 唯一键。"""
        sets: list[str] = []
        args: list[Any] = []
        if term is not None:
            t = str(term).strip()[:JARGON_TERM_MAX_CHARS]
            if not t:
                return 0
            sets.append("term=?")
            args.append(t)
        if meaning is not None:
            sets.append("meaning=?")
            args.append(str(meaning)[:JARGON_MEANING_MAX_CHARS])
        if enabled is not None:
            sets.append("enabled=?")
            args.append(1 if enabled else 0)
        if not sets:
            return 0
        sets.append("updated_at=?")
        args.append(now())
        cur = await self.db.execute(f"UPDATE hf_jargon SET {', '.join(sets)} WHERE id=?", args + [int(jid)])
        await self.db.commit()
        return int(cur.rowcount or 0)

    async def jargon_delete(self, jid: int) -> dict[str, int]:
        """真删一条黑话（行 + 它的命中日志）。「否定」不等于删除：否定是归档（status=denied）。"""
        target = int(jid)
        cur = await self.db.execute("DELETE FROM hf_jargon WHERE id=?", (target,))
        hits = await self.db.execute("DELETE FROM hf_jargon_hit WHERE term_id=?", (target,))
        await self.db.commit()
        return {"deleted": int(cur.rowcount or 0), "hits_removed": int(hits.rowcount or 0)}

    async def jargon_existing_terms(self) -> set[str]:
        """所有已登记词条（任何状态，含 denied —— 否定 = 不再推断）。返回小写集合。"""
        rows = await (await self.db.execute("SELECT DISTINCT term FROM hf_jargon")).fetchall()
        return {str(r["term"]).strip().lower() for r in rows if str(r["term"]).strip()}

    async def person_names_all(self) -> list[str]:
        """已知人名/昵称/群名（h 洞② 人名过滤的接线数据源；表不存在就空列表，不放大错误）。"""
        out: list[str] = []
        try:
            await self._ensure_person_tables()
            rows = await (
                await self.db.execute("SELECT DISTINCT name FROM hf_person_names WHERE name != ''")
            ).fetchall()
            out = [str(r["name"]).strip() for r in rows if str(r["name"]).strip()]
        except Exception as exc:                            # noqa: BLE001 读不到名单不挡候选挖掘
            logger.debug(f"[StarHeart] 人名表读取跳过：{exc}")
        return out

    # ---------------- 备份（拍-14：落 backups/，保留最近 5 份） ----------------
    async def backup_tables(self, tables: tuple[str, ...], *, tag: str = "backup", keep: int = BACKUP_KEEP) -> str:
        """整表导出成 JSON 落到 <数据目录>/backups/。返回文件名；失败返回 ""（调用方必须如实回报）。

        表名来自本文件的常量元组（白名单），无外部输入面。危险操作**先备份再动**：
        返回空串 = 没备份成功 —— 调用方要把这句话原样告诉用户（不假装有备份）。
        """
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
        name = f"hf_{tag}-{stamp}.json"
        out_dir = Path(self.db_path).parent / "backups"
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
            payload: dict[str, Any] = {"ts": now(), "tag": tag, "db": Path(self.db_path).name, "tables": {}}
            for table in tables:
                rows = await (await self.db.execute(f"SELECT * FROM {table}")).fetchall()
                payload["tables"][table] = [dict(r) for r in rows]
            (out_dir / name).write_text(json.dumps(payload, ensure_ascii=False, default=_json_default), encoding="utf-8")
        except Exception as exc:                            # noqa: BLE001
            logger.warning(f"[StarHeart] 备份失败（{tag}）：{exc}")
            return ""
        try:
            olds = sorted(out_dir.glob(f"hf_{tag}-*.json"))
            for old in olds[: -max(1, int(keep))]:
                old.unlink(missing_ok=True)
        except Exception as exc:                            # noqa: BLE001
            logger.warning(f"[StarHeart] 备份清理跳过（{tag}）：{exc}")
        return name

    async def purge_expressions(self, actor: str = "panel") -> dict[str, Any]:
        """「只清表达」（S4-5 E2 动作一 / 拍-14）：先备份 → 软删 + 全部进「不再学」名单。

        记忆 / 关系 / 好感一律不动。软删 = status='archived'（行还在，可恢复）。
        """
        backup = await self.backup_tables(EXPR_BACKUP_TABLES, tag="expressions")
        if not backup:
            logger.error("[StarHeart] 清空表达中止：备份失败，未改动任何数据")
            return {"soft_deleted": 0, "banned": 0, "backup": "", "error": "backup_failed"}
        stamp = now()
        rows = await (
            await self.db.execute("SELECT id, scene, way, norm_key FROM hf_expr WHERE status!='archived'")
        ).fetchall()
        banned = 0
        for r in rows:
            banned += await self.add_ban(
                key=f"{r['scene'] or ''}|{r['way'] or ''}",
                norm_key=str(r["norm_key"] or ""),
                reason="清空表达",
                actor=actor,
                note="purge:expressions",
            )
        cur = await self.db.execute(
            "UPDATE hf_expr SET status='archived', enabled=0, updated_at=? WHERE status!='archived'", (stamp,)
        )
        await self.db.commit()
        return {"soft_deleted": int(cur.rowcount or 0), "banned": banned, "backup": backup}

    async def backfill_expr_from_habits(self) -> int:
        """旧表 hf_habits → hf_expr 一次性回填（幂等：同 (scope, norm_key) 已存在就跳过）。

        拍-2 / 存量口径：真机存量 = 0 → 天然 no-op；旧表**只读保留、不删**（回退零损失）。
        weight 是死字段（拍-11），不迁入。跑两遍行数不变（S4-1 验收 2）。
        """
        try:
            src = await (await self.db.execute("SELECT * FROM hf_habits")).fetchall()
        except Exception as exc:                            # noqa: BLE001
            logger.warning(f"[StarHeart] 表达回填读取旧表跳过：{exc}")
            return 0
        n = 0
        for r in src:
            d = dict(r)
            scene, way = str(d.get("scene") or ""), str(d.get("way") or "")
            if not scene or not way:
                continue
            scope = str(d.get("scope") or "全局")
            norm_key = expr_norm_key(scene, way)
            hit = await (
                await self.db.execute(
                    "SELECT id FROM hf_expr WHERE scope=? AND norm_key=? LIMIT 1", (scope, norm_key)
                )
            ).fetchone()
            if hit:
                continue
            stamp = float(d.get("updated_at") or now())
            await self.db.execute(
                "INSERT INTO hf_expr(scene,way,cues,scope,enabled,status,norm_key,checked_state,"
                " source_ids,evidence_snap,cooldown_until,outcome_score,gate_override,created_at,updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    scene[:40], way[:120],
                    json.dumps(_jlist(d.get("cues")), ensure_ascii=False),
                    scope,
                    1 if d.get("enabled", 1) else 0,
                    "active", norm_key, "user_ok", "[]", "[]", None, None, "", stamp, stamp,
                ),
            )
            n += 1
        if n:
            await self.db.commit()
            logger.info(f"[StarHeart] 表达回填：hf_habits → hf_expr 迁入 {n} 条（旧表保留、不写不动）")
        return n


    # ---------------- 门控流水 ----------------
    async def log_gate(self, rec: dict[str, Any]) -> None:
        await self.db.execute(
            "INSERT INTO hf_gate_log(ts,session_id,user_id,score,factors,decision,threshold,note) VALUES(?,?,?,?,?,?,?,?)",
            (
                rec.get("ts") or now(),
                rec.get("session_id", ""),
                rec.get("user_id", ""),
                float(rec.get("score", 0)),
                json.dumps(rec.get("factors", {}), ensure_ascii=False),
                rec.get("decision", "silent"),
                float(rec.get("threshold", 80)),
                rec.get("note", ""),
            ),
        )
        await self.db.commit()

    async def list_gate(self, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        rows = await (
            await self.db.execute(
                "SELECT * FROM hf_gate_log ORDER BY ts DESC LIMIT ? OFFSET ?",
                (limit, max(0, offset)),
            )
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["factors"] = json.loads(d.get("factors") or "{}")
            except (TypeError, ValueError):
                d["factors"] = {}
            out.append(d)
        return out

    async def count_gate(self) -> int:
        row = await (await self.db.execute("SELECT COUNT(*) AS n FROM hf_gate_log")).fetchone()
        return int(row["n"] if row else 0)

    # ---------------- 流水记账（只记不拦；写失败由调用方兜底，不允许影响主流程） ----------------
    async def log_sent(self, rec: dict[str, Any]) -> str:
        """出站流水：自赋 msg_id（对账锚点），只存长度与哈希、不存原文。

        emit_ok 三态：1=确认发出 / 0=确认失败 / None=未确认（无发送回执）。返回本次 msg_id。
        """
        text = str(rec.get("text") or "")
        msg_id = str(rec.get("msg_id") or uuid.uuid4().hex[:16])
        emit_raw = rec.get("emit_ok", 0)
        await self.db.execute(
            "INSERT INTO hf_sent_log(ts,msg_id,session_id,kind,seg_index,seg_total,length_chars,"
            "content_hash,delay_ms,emit_ok,plan_id,local_id,trigger,seed,exempt,confidence,platform_id) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                rec.get("ts") or now(),
                msg_id,
                rec.get("session_id", ""),
                rec.get("kind", "reply"),
                int(rec.get("seg_index", 0) or 0),
                max(1, int(rec.get("seg_total", 1) or 1)),
                int(rec.get("length_chars") if rec.get("length_chars") is not None else _len_nows(text)),
                rec.get("content_hash") or _hash16(text),
                int(rec.get("delay_ms", 0) or 0),
                None if emit_raw is None else int(emit_raw or 0),
                rec.get("plan_id", ""),
                rec.get("local_id") or msg_id,
                rec.get("trigger", ""),
                rec.get("seed", ""),
                int(rec.get("exempt", 0) or 0),
                rec.get("confidence") or "low",
                rec.get("platform_id", ""),
            ),
        )
        await self.db.commit()
        return msg_id

    async def log_decision(self, rec: dict[str, Any]) -> None:
        """门控决策流水（只记不拦）：因子快照 + reason + 消息文本（截断 ≤500 字 + hash）。"""
        text = str(rec.get("text") or "")
        await self.db.execute(
            "INSERT INTO hf_decision_log(ts,event_id,session_id,user_id,eligible,decision,score,"
            "factors_json,reason,config_ver,gate_shadow,msg_text,text_hash) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                rec.get("ts") or now(),
                rec.get("event_id", ""),
                rec.get("session_id", ""),
                rec.get("user_id", ""),
                int(rec.get("eligible", 1) or 0),
                rec.get("decision", ""),
                float(rec.get("score", 0) or 0),
                json.dumps(rec.get("factors") or {}, ensure_ascii=False),
                rec.get("reason", ""),
                rec.get("config_ver", ""),
                int(rec.get("gate_shadow", 0) or 0),
                _clip(text, 500),
                _hash16(text),
            ),
        )
        await self.db.commit()

    async def set_decision_verdict(self, did: int, verdict: str, actor: str = "panel") -> bool:
        """门控行内改判：人工判定写回决策流水（verdict 取值由 service 层白名单校验）。

        幂等：同一行重复写同一个判定只更新时间戳；verdict 为空串 = 撤销改判。
        """
        stamp = now() if str(verdict or "") else 0.0
        cur = await self.db.execute(
            "UPDATE hf_decision_log SET user_verdict=?, verdict_at=?, verdict_actor=? WHERE id=?",
            (str(verdict or ""), stamp, str(actor or "panel"), int(did)),
        )
        await self.db.commit()
        return bool(cur.rowcount)

    async def list_decisions(self, limit: int = 30, offset: int = 0) -> list[dict[str, Any]]:
        """决策流水（门控页「逐条改判」数据源）：含消息文本（截断列）与人工改判状态。"""
        rows = await (
            await self.db.execute(
                "SELECT id,ts,event_id,session_id,user_id,eligible,decision,score,factors_json,reason,"
                "config_ver,gate_shadow,msg_text,text_hash,user_verdict,verdict_at,verdict_actor"
                " FROM hf_decision_log ORDER BY ts DESC, id DESC LIMIT ? OFFSET ?",
                (max(1, int(limit or 30)), max(0, int(offset or 0))),
            )
        ).fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            d = dict(r)
            raw = d.pop("factors_json", "{}")
            try:
                d["factors"] = json.loads(raw or "{}")
            except (TypeError, ValueError):
                d["factors"] = {}
            d["gate_shadow"] = int(d.get("gate_shadow") or 0)
            d["user_verdict"] = str(d.get("user_verdict") or "")
            out.append(d)
        return out

    async def get_decision(self, did: int) -> dict[str, Any] | None:
        """单条决策记录（改判后回读状态用）。"""
        row = await (
            await self.db.execute(
                "SELECT id,decision,score,user_verdict,verdict_at,verdict_actor FROM hf_decision_log WHERE id=?",
                (int(did),),
            )
        ).fetchone()
        return dict(row) if row else None

    async def count_decisions(self) -> int:
        row = await (await self.db.execute("SELECT COUNT(*) AS n FROM hf_decision_log")).fetchone()
        return int((row["n"] if row else 0) or 0)

    async def log_expr_use(self, rec: dict[str, Any]) -> None:
        """表达使用流水：注入即记一笔（emit_ok=NULL 待出站对账，NULL ≠ 没发出）。

        同时盖冷却戳（拍-10：冷却按时间）—— use_count 不回写（拍-5：只做查询聚合）。
        """
        used_at = float(rec.get("used_at") or now())
        eid = int(rec.get("expr_id") or 0)
        await self.db.execute(
            "INSERT INTO hf_expr_use_log(expr_id,used_at,emit_ok,outcome_score,seed,session_id) VALUES(?,?,?,?,?,?)",
            (
                eid,
                used_at,
                rec.get("emit_ok"),
                rec.get("outcome_score"),
                rec.get("seed", ""),
                rec.get("session_id", ""),
            ),
        )
        await self.db.commit()
        try:
            await self.db.execute(
                "UPDATE hf_expr SET cooldown_until=? WHERE id=?", (used_at + EXPR_COOLDOWN_SECONDS, eid)
            )
            await self.db.commit()
        except Exception as exc:                            # noqa: BLE001 冷却戳写失败不该挡记账
            logger.warning(f"[StarHeart] 表达冷却戳写入跳过：{exc}")

    async def purge_flow_logs(self) -> dict[str, int]:
        """按 FLOW_RETENTION_DAYS 清理四张流水表；发送计划只清终态行（未完成行留给恢复扫描）。

        逐表独立 try/except + 提交：单表缺/坏只记 WARNING，不挡其他表当日清理。
        表名/列名来自本函数的常量元组（白名单），无外部输入面。
        """
        cut_now = now()
        out: dict[str, int] = {}
        for table, col in (("hf_sent_log", "ts"), ("hf_decision_log", "ts"), ("hf_expr_use_log", "used_at")):
            try:
                days = int(FLOW_RETENTION_DAYS.get(table) or 30)
                cur = await self.db.execute(f"DELETE FROM {table} WHERE {col} < ?", (cut_now - days * 86400,))
                n = int(cur.rowcount or 0)
                await self.db.commit()
                out[table] = n
            except Exception as exc:                        # noqa: BLE001 单表失败不挡其他表
                logger.warning(f"[StarHeart] 流水保留期清理跳过 {table}：{exc}")
        try:
            days = int(FLOW_RETENTION_DAYS.get("hf_send_plan") or 30)
            cur = await self.db.execute(
                "DELETE FROM hf_send_plan WHERE updated_at < ? AND state IN ('sent','failed','expired')",
                (cut_now - days * 86400,),
            )
            n = int(cur.rowcount or 0)
            await self.db.commit()
            out["hf_send_plan"] = n
        except Exception as exc:                            # noqa: BLE001
            logger.warning(f"[StarHeart] 流水保留期清理跳过 hf_send_plan：{exc}")
        return out

    # ---------------- token ----------------
    async def log_tokens(self, rec: dict[str, Any]) -> None:
        await self.db.execute(
            "INSERT INTO hf_tokens(ts,task,model,in_tokens,out_tokens,cached,cost) VALUES(?,?,?,?,?,?,?)",
            (
                rec.get("ts") or now(),
                rec.get("task", "chat"),
                rec.get("model", ""),
                int(rec.get("in_tokens", 0)),
                int(rec.get("out_tokens", 0)),
                int(rec.get("cached", 0)),
                float(rec.get("cost", 0)),
            ),
        )
        await self.db.commit()

    async def tokens_summary(self, days: int = 7) -> dict[str, Any]:
        since = now() - days * 86400
        rows = await (
            await self.db.execute(
                "SELECT task, model, SUM(in_tokens) i, SUM(out_tokens) o, SUM(cached) c, SUM(cost) cost, COUNT(*) n "
                "FROM hf_tokens WHERE ts>=? GROUP BY task, model",
                (since,),
            )
        ).fetchall()
        items = [dict(r) for r in rows]
        # est:inject 是「上下文注入」的估算行，不是模型真实消耗（混进 total_in 会抬高输入 tokens）
        est_in = sum(int(x["i"] or 0) for x in items if str(x.get("task") or "") == "est:inject")
        real = [x for x in items if str(x.get("task") or "") != "est:inject"]
        return {
            "days": days,
            "est_in": est_in,
            "has_pricing": any(float(x.get("cost") or 0) > 0 for x in items),
            "total_in": sum(x["i"] or 0 for x in real),
            "total_out": sum(x["o"] or 0 for x in items),
            "total_cached": sum(x["c"] or 0 for x in items),
            "total_cost": round(sum(x["cost"] or 0 for x in items), 4),
            "items": items,
        }

    # ---------------- 配置 ----------------
    async def get_config(self, key: str, default: Any = None) -> Any:
        row = await (await self.db.execute("SELECT value FROM hf_config WHERE key=?", (key,))).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except (TypeError, ValueError):
            return row["value"]

    async def all_config(self) -> dict[str, Any]:
        rows = await (await self.db.execute("SELECT key, value FROM hf_config")).fetchall()
        out: dict[str, Any] = {}
        for r in rows:
            try:
                out[r["key"]] = json.loads(r["value"])
            except (TypeError, ValueError):
                out[r["key"]] = r["value"]
        return out

    async def set_config(self, key: str, value: Any) -> None:
        await self.db.execute(
            "INSERT INTO hf_config(key,value,updated_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, json.dumps(value, ensure_ascii=False), now()),
        )
        await self.db.commit()

    # ---------------- 配置快照（参数护栏：保存前自动存 / 手动命名存 / 一键回滚） ----------------
    async def save_config_snapshot(self, name: str, config: dict[str, Any], *, auto: bool = False,
                                   actor: str = "panel", note: str = "") -> dict[str, Any]:
        """存一份配置快照：内容是 hf_config 里真正存着的键值（回滚即原样写回）。"""
        payload = json.dumps(config or {}, ensure_ascii=False)
        stamp = now()
        cur = await self.db.execute(
            "INSERT INTO hf_config_snapshot(name,ts,n_keys,auto,actor,note,config_json) VALUES(?,?,?,?,?,?,?)",
            (str(name or "")[:60], stamp, len(config or {}), 1 if auto else 0,
             str(actor or "panel")[:32], str(note or "")[:200], payload),
        )
        await self.db.commit()
        return {"id": int(cur.lastrowid or 0), "name": str(name or "")[:60], "ts": stamp,
                "n_keys": len(config or {}), "auto": bool(auto)}

    async def list_config_snapshots(self, limit: int = 25) -> list[dict[str, Any]]:
        rows = await (
            await self.db.execute(
                "SELECT id,name,ts,n_keys,auto,actor,note FROM hf_config_snapshot ORDER BY ts DESC, id DESC LIMIT ?",
                (max(1, int(limit or 25)),),
            )
        ).fetchall()
        return [dict(r) for r in rows]

    async def count_config_snapshots(self) -> int:
        row = await (await self.db.execute("SELECT COUNT(*) AS n FROM hf_config_snapshot")).fetchone()
        return int((row["n"] if row else 0) or 0)

    async def get_config_snapshot(self, sid: int) -> dict[str, Any] | None:
        row = await (
            await self.db.execute("SELECT * FROM hf_config_snapshot WHERE id=?", (int(sid),))
        ).fetchone()
        return dict(row) if row else None

    async def prune_config_snapshots(self, keep: int = CONFIG_SNAPSHOT_KEEP) -> int:
        """超出保留份数就从最老的开始删，自动快照先删（手动命名的尽量留到最后）。"""
        # 超出保留份数时先删**自动快照**（从最老的开始），自动快照全删光了才动手动命名的 ——
        # 手动命名的那几份是人特意留的存档点，不能被之后随手保存刷出来的自动快照挤掉。
        keep = max(1, int(keep or CONFIG_SNAPSHOT_KEEP))
        rows = await (
            await self.db.execute("SELECT id,auto FROM hf_config_snapshot ORDER BY ts DESC, id DESC")
        ).fetchall()
        over = len(rows) - keep
        if over <= 0:
            return 0
        autos_old_first = [r for r in reversed(rows) if int(r["auto"] or 0)]
        victims = autos_old_first[:over]
        if len(victims) < over:                                   # 自动快照不够删，才从最老的手动快照补
            manuals_old_first = [r for r in reversed(rows) if not int(r["auto"] or 0)]
            victims += manuals_old_first[: over - len(victims)]
        for r in victims:
            await self.db.execute("DELETE FROM hf_config_snapshot WHERE id=?", (int(r["id"]),))
        await self.db.commit()
        return len(victims)

    async def delete_config_key(self, key: str) -> None:
        """删掉一个配置键（回滚用：快照里没有、现在却存在的键 = 快照之后新加的）。"""
        await self.db.execute("DELETE FROM hf_config WHERE key=?", (str(key),))
        await self.db.commit()

    # ---------------- 审计 ----------------
    async def audit(self, action: str, target: str = "", detail: str = "", actor: str = "panel") -> None:
        await self.db.execute(
            "INSERT INTO hf_audit(ts,actor,action,target,detail) VALUES(?,?,?,?,?)",
            (now(), actor, action, target, detail),
        )
        await self.db.commit()

    async def list_audit(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = await (await self.db.execute("SELECT * FROM hf_audit ORDER BY ts DESC LIMIT ?", (limit,))).fetchall()
        return [dict(r) for r in rows]

    # ---------------- 导入批次 ----------------
    async def new_batch(self, source: str, total: int, note: str = "") -> str:
        batch_id = f"imp_{int(now())}_{uuid.uuid4().hex[:6]}"
        await self.db.execute(
            "INSERT INTO hf_import_batches(batch_id,source,started_at,total,status,note) VALUES(?,?,?,?,'running',?)",
            (batch_id, source, now(), total, note),
        )
        await self.db.commit()
        return batch_id

    async def update_batch(self, batch_id: str, **fields: Any) -> None:
        if not fields:
            return
        sets = ", ".join(f"{k}=?" for k in fields)
        await self.db.execute(f"UPDATE hf_import_batches SET {sets} WHERE batch_id=?", list(fields.values()) + [batch_id])
        await self.db.commit()

    async def list_batches(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = await (await self.db.execute("SELECT * FROM hf_import_batches ORDER BY started_at DESC LIMIT ?", (limit,))).fetchall()
        return [dict(r) for r in rows]

    async def imported_external_ids(self, source: str) -> set[str]:
        rows = await (
            await self.db.execute("SELECT external_id FROM hf_memories WHERE external_source=?", (source,))
        ).fetchall()
        return {r["external_id"] for r in rows}

    async def persona_memory_counts(self) -> dict[str, int]:
        """按人格聚合记忆数（含空人格归到 ''），顶栏人格切换器的计数用。"""
        rows = await (
            await self.db.execute(
                "SELECT COALESCE(persona_id, '') AS p, COUNT(*) AS n FROM hf_memories WHERE status!='deleted' GROUP BY p"
            )
        ).fetchall()
        return {str(r["p"]): int(r["n"]) for r in rows}


    # ---------------- 撤回 ----------------
    async def hidden_contacts(self) -> dict[str, list[str]]:
        rows = await (await self.db.execute("SELECT kind, ident FROM hf_hidden_contacts ORDER BY ts DESC")).fetchall()
        out = {"group": [], "private": []}
        for row in rows:
            out.setdefault(str(row["kind"]), []).append(str(row["ident"]))
        return out

    async def set_hidden_contacts(self, kind: str, ids: list[str]) -> None:
        if kind not in ("group", "private"):
            raise ValueError(f"unknown kind: {kind}")
        clean = []
        for x in ids or []:
            x = str(x).strip()
            if x and x not in clean:
                clean.append(x)
        await self.db.execute("DELETE FROM hf_hidden_contacts WHERE kind=?", (kind,))
        await self.db.executemany(
            "INSERT OR IGNORE INTO hf_hidden_contacts (kind, ident, ts) VALUES (?,?,?)",
            [(kind, x, now()) for x in clean],
        )
        await self.db.commit()

    async def set_embedding(self, mid: Any, vector: list[float], model: str) -> None:
        """写入向量：**旧列 + 当前代 hf_vectors 都要写**。

        为什么必须两处都写（这是条真踩过的坑）：
        注入路径读旧列 `hf_memories.embedding`，而面板的「召回测试」和「向量覆盖率」读
        `hf_vectors`。只写旧列的话，迁移之后新建的记忆在面板里永远显示"缺向量"、也永远
        进不了面板召回结果（点「补全向量」也没用，因为补的就是旧列）。写入端对齐后，
        两条读取路径才算一致。

        多代修正（2026-10-23）：旧列只是「当前代」的兼容镜像。写非当前代时必须
        **不动旧列**，否则换模型后旧列会混进不同维度的向量，召回会静默归零。
        """
        import array
        sid = self._split_mid(mid)
        if sid is None:
            return
        blob = array.array("f", [float(x) for x in vector]).tobytes()
        gk = self.gen_key(model, len(vector))
        await self.db.execute(
            "INSERT OR REPLACE INTO hf_vectors (memory_id, gen_key, model, dim, vec, created_at)"
            " VALUES (?,?,?,?,?,?)",
            (sid, gk, str(model or ""), len(vector), blob, now()),
        )
        active = await self.active_gen()
        if not active or gk == active:
            await self.db.execute(
                "UPDATE hf_memories SET embedding=?, embedding_model=?, updated_at=? WHERE id=?",
                (blob, model, now(), sid),
            )
        await self.db.commit()

    async def memories_without_embedding(self, limit: int = 200, persona_id: str = "") -> list[dict[str, Any]]:
        """还没建向量的可召回记忆（含 archived —— 归档记忆不进向量库就永远不可能被提起）。"""
        sql = ("SELECT id, summary, content FROM hf_memories "
               "WHERE status NOT IN ('deleted','forgotten') AND embedding IS NULL")
        args: list[Any] = []
        if persona_id == "__none__":
            sql += " AND (persona_id IS NULL OR persona_id = '')"
        elif persona_id:
            sql += " AND persona_id=?"
            args.append(persona_id)
        sql += " LIMIT ?"
        args.append(limit)
        rows = await (await self.db.execute(sql, args)).fetchall()
        return [dict(r) for r in rows]

    async def all_active_embeddings(self, persona_id: str = "", include_archived: bool = True) -> list[tuple[str, bytes]]:
        """可召回记忆的向量（deleted / forgotten 才是终点，archived 也在内）。

        名字沿用旧的，避免改散调用点；语义已改：存档记忆必须能参与召回，否则
        「归档被提及自动拉回」永远走不到 —— 先有召回才可能复活，而复活又是召回的前提。
        """
        sql = ("SELECT id, embedding FROM hf_memories "
               "WHERE status NOT IN ('deleted','forgotten') AND COALESCE(muted,0)=0 AND embedding IS NOT NULL")
        if not include_archived:
            sql += " AND status!='archived'"
        args: list[Any] = []
        if persona_id == "__none__":
            sql += " AND (persona_id IS NULL OR persona_id = '')"
        elif persona_id:
            sql += " AND persona_id=?"
            args.append(persona_id)
        rows = await (await self.db.execute(sql, args)).fetchall()
        return [(str(r["id"]), r["embedding"]) for r in rows]

    # ---------------- 语料（群友/好友原话，自学习的数据源） ----------------
    async def add_corpus(self, rec: dict[str, Any]) -> int | None:
        text = str(rec.get("text") or "").strip()[:500]
        if not text:
            return None
        cur = await self.db.execute(
            """INSERT INTO hf_corpus
                (ts, session_id, chat_type, user_id, user_name, persona_id, text, is_bot, used)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                float(rec.get("ts") or now()),
                str(rec.get("session_id") or ""),
                str(rec.get("chat_type") or ""),
                str(rec.get("user_id") or ""),
                str(rec.get("user_name") or ""),
                str(rec.get("persona_id") or ""),
                text,
                1 if rec.get("is_bot") else 0,
                int(rec.get("used") or 0),
            ),
        )
        cid = int(cur.lastrowid or 0)
        await self.db.commit()
        # 语料出处侧车（E3）：把这条消息的原生 message_id / ext_id 记下来 ——
        # 表达学到这条原话时，出处就能对回「哪条消息」，而不是只剩 60 字快照。
        mid = str(rec.get("message_id") or "")
        ext = str(rec.get("ext_id") or "")
        if cid and (mid or ext):
            try:
                await self.db.execute(
                    "INSERT OR REPLACE INTO hf_corpus_ref(corpus_id,message_id,ext_id,session_id,ts)"
                    " VALUES(?,?,?,?,?)",
                    (cid, mid or ext, ext or mid, str(rec.get("session_id") or ""), float(rec.get("ts") or now())),
                )
                await self.db.commit()
            except Exception as exc:                        # noqa: BLE001 侧车失败不该挡语料落库
                logger.warning(f"[StarHeart] 语料出处侧车写入跳过：{exc}")
        return cid

    async def corpus_stats(self) -> dict[str, Any]:
        row = await (
            await self.db.execute(
                "SELECT COUNT(*) AS n, SUM(CASE WHEN is_bot=0 THEN 1 ELSE 0 END) AS peer,"
                " SUM(CASE WHEN is_bot=1 THEN 1 ELSE 0 END) AS bot FROM hf_corpus"
            )
        ).fetchone()
        by_persona = {
            r["persona_id"] or "全局": r["c"]
            for r in await (
                await self.db.execute(
                    "SELECT persona_id, COUNT(*) c FROM hf_corpus WHERE is_bot=0 GROUP BY persona_id"
                )
            ).fetchall()
        }
        return {
            "total": int(row["n"] or 0),
            "peer": int(row["peer"] or 0),
            "bot": int(row["bot"] or 0),
            "by_persona": by_persona,
        }

    async def list_corpus(
        self, limit: int = 200, *, is_bot: int | None = None, persona_id: str = "", unused_only: bool = False
    ) -> list[dict[str, Any]]:
        where: list[str] = []
        args: list[Any] = []
        if is_bot is not None:
            where.append("is_bot = ?")
            args.append(is_bot)
        if persona_id:
            where.append("persona_id = ?")
            args.append(persona_id)
        if unused_only:
            where.append("used = 0")
        clause = ("WHERE " + " AND ".join(where)) if where else ""
        rows = await (
            await self.db.execute(
                f"SELECT * FROM hf_corpus {clause} ORDER BY ts DESC LIMIT ?",
                args + [max(1, min(limit, 1000))],
            )
        ).fetchall()
        return [dict(r) for r in rows]

    async def drop_bot_corpus_since(self, session_id: str, since: float) -> int:
        """删除某会话在 since 之后落库的 bot 语料（「撤回连回复一起撤」用），返回删除条数。"""
        cur = await self.db.execute(
            "DELETE FROM hf_corpus WHERE session_id=? AND is_bot=1 AND ts>=?", (session_id, float(since))
        )
        await self.db.commit()
        return int(cur.rowcount or 0)

    async def mark_corpus_used(self, ids: list[int]) -> int:
        if not ids:
            return 0
        marks = ",".join("?" for _ in ids)
        cur = await self.db.execute(f"UPDATE hf_corpus SET used = 1 WHERE id IN ({marks})", list(ids))
        await self.db.commit()
        return int(cur.rowcount or 0)

    async def prune_corpus(self, keep: int = 5000) -> int:
        """超出 keep 时删最旧；**被有效表达引用的行免删**（E3 + 拍-15：查询排除，不加列）。

        不这么写就会出现：刚学到的表达，出处原话在下一轮提取后被 prune 物理删掉 →
        「出处可回查 ≥95%」永远做不到。
        """
        row = await (await self.db.execute("SELECT COUNT(*) AS n FROM hf_corpus")).fetchone()
        if int(row["n"] or 0) <= keep:
            return 0
        refs = await self.referenced_corpus_ids()
        if refs:
            ids = sorted(refs)
            marks = ",".join("?" for _ in ids)
            cur = await self.db.execute(
                "DELETE FROM hf_corpus WHERE id IN ("
                f" SELECT id FROM hf_corpus WHERE id NOT IN ({marks}) ORDER BY ts DESC LIMIT -1 OFFSET ?)",
                ids + [max(0, keep)],
            )
        else:
            cur = await self.db.execute(
                "DELETE FROM hf_corpus WHERE id IN (SELECT id FROM hf_corpus ORDER BY ts DESC LIMIT -1 OFFSET ?)",
                (max(0, keep),),
            )
        await self.db.commit()
        return int(cur.rowcount or 0)

    async def log_recall(self, entry: dict[str, Any]) -> int:
        cur = await self.db.execute(
            "INSERT INTO hf_recall_log (ts, session_id, user_id, notice_type, message_id, operator_id, matched, action, detail) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (
                float(entry.get("ts") or now()), str(entry.get("session_id") or ""), str(entry.get("user_id") or ""),
                str(entry.get("notice_type") or ""), str(entry.get("message_id") or ""), str(entry.get("operator_id") or ""),
                int(entry.get("matched") or 0), str(entry.get("action") or "log_only"),
                json.dumps(entry.get("detail") or {}, ensure_ascii=False),
            ),
        )
        await self.db.commit()
        return int(cur.lastrowid or 0)

    async def list_recalls(self, limit: int = 200) -> list[dict[str, Any]]:
        rows = await (await self.db.execute("SELECT * FROM hf_recall_log ORDER BY ts DESC LIMIT ?", (limit,))).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            try:
                item["detail"] = json.loads(item.get("detail") or "{}")
            except (TypeError, ValueError):
                item["detail"] = {}
            out.append(item)
        return out

    async def link_memory_messages(self, memory_id: Any, message_ids: list[str]) -> int:
        """把一批消息 id 挂到一条记忆上（侧车表 hf_memory_msg）。返回新写入条数。"""
        raw = str(memory_id or "").lstrip("m")
        if not raw.isdigit():
            return 0
        ts_now = now()
        n = 0
        seen: set[str] = set()
        for mid in message_ids or []:
            mid = str(mid or "").strip()
            if not mid or mid in seen:
                continue
            seen.add(mid)
            try:
                cur = await self.db.execute(
                    "INSERT OR IGNORE INTO hf_memory_msg (memory_id, message_id, ts) VALUES (?, ?, ?)",
                    (int(raw), mid, ts_now),
                )
                n += int(cur.rowcount or 0)
            except Exception as exc:                        # noqa: BLE001 老库没有这张表时不影响记忆本身
                logger.debug(f"[StarHeart] 记忆-消息侧车写入跳过：{exc}")
                break
        if n:
            await self.db.commit()
        return n

    async def memory_ids_by_message_id(self, message_id: str) -> list[str]:
        """按消息 id 反查记忆（撤回命中），返回 `m<id>` 形式，与面板/服务层口径一致。

        只返回「还能被处置」的记忆：已遗忘/已删除的必须排除，否则撤回一条旧消息会把
        `forgotten` 翻成 `archived`（复活已遗忘的记忆，P2-A）。
        """
        ref = str(message_id or "").strip()
        if not ref:
            return []
        try:
            rows = await (await self.db.execute(
                "SELECT DISTINCT s.memory_id FROM hf_memory_msg s "
                "JOIN hf_memories m ON m.id = s.memory_id "
                "WHERE s.message_id=? AND m.status NOT IN ('forgotten','deleted')",
                (ref,),
            )).fetchall()
        except Exception as exc:                            # noqa: BLE001 老库没有这张表
            logger.debug(f"[StarHeart] 记忆-消息侧车查询跳过：{exc}")
            return []
        return [f"m{int(r['memory_id'])}" for r in rows if r["memory_id"] is not None]

    async def memories_by_source_ref(self, ref: str, session_id: str = "") -> list[dict[str, Any]]:
        """按 source_ref 精确匹配（不再 LIKE '%mid%'：message_id 是短数字，会命中无关会话）。"""
        ref = str(ref or "").strip()
        if not ref:
            return []
        sql = "SELECT * FROM hf_memories WHERE source_ref = ? AND status='active'"
        args: list[Any] = [ref]
        if session_id:
            sql += " AND session_id = ?"
            args.append(str(session_id))
        rows = await (await self.db.execute(sql, args)).fetchall()
        return [self._mem_row(r) for r in rows]

    async def attach_recall_react(self, message_id: str, react: dict[str, Any]) -> bool:
        """把「模型对这条撤回的反应」写回对应那条撤回流水的 detail.react（面板据此显示）。"""
        mid = str(message_id or "")
        if not mid or not isinstance(react, dict):
            return False
        try:
            row = await (await self.db.execute(
                "SELECT id, detail FROM hf_recall_log WHERE message_id=? ORDER BY ts DESC LIMIT 1", (mid,)
            )).fetchone()
            if not row:
                return False
            try:
                d = json.loads(row["detail"] or "{}")
                if not isinstance(d, dict):
                    d = {}
            except Exception:                                  # noqa: BLE001
                d = {}
            d["react"] = react
            await self.db.execute("UPDATE hf_recall_log SET detail=? WHERE id=?",
                                  (json.dumps(d, ensure_ascii=False), row["id"]))
            await self.db.commit()
            return True
        except Exception:                                      # noqa: BLE001 反应记录失败不影响主流程
            return False

    async def mark_memories_stale(self, ids: list[Any], factor: float = 0.3) -> int:
        """撤回降权：只改强度 + 留痕，**不动 status**（P1-6）。

        旧实现直接写 status='stale' —— 一次撤回 = 永久离开 active，
        而召回当时只查 active，等于「撤回一次就再也想不起来」。
        现在只降权；状态由生命周期统一裁决，命中还能被 touch_memory 拉回。
        """
        n = 0
        for mid in ids:
            raw = str(mid).lstrip("m")
            cur = await self.db.execute(
                "UPDATE hf_memories SET strength=MAX(0.0, strength*?), "
                "forget_stage=CASE WHEN forget_stage='' THEN 'recall' ELSE forget_stage END, "
                "updated_at=? WHERE id=?",
                (float(factor), now(), raw),
            )
            n += int(cur.rowcount or 0)
        await self.db.commit()
        return n

    # ---------------- 主动消息 ----------------
    async def log_proactive(self, entry: dict[str, Any]) -> int:
        cur = await self.db.execute(
            "INSERT INTO hf_proactive_log (ts, session_id, user_id, zone, idle_min, score, status, reason, guard, text,"
            " prompt_source, memory_ids) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                float(entry.get("ts") or now()), str(entry.get("session_id") or ""), str(entry.get("user_id") or ""),
                str(entry.get("zone") or ""), float(entry.get("idle_min") or 0), float(entry.get("score") or 0),
                str(entry.get("status") or "candidate"), str(entry.get("reason") or ""),
                json.dumps(entry.get("guard") or {}, ensure_ascii=False), str(entry.get("text") or ""),
                str(entry.get("prompt_source") or ""),
                entry.get("memory_ids") if isinstance(entry.get("memory_ids"), str)
                else ",".join(str(x) for x in (entry.get("memory_ids") or [])),
            ),
        )
        await self.db.commit()
        return int(cur.lastrowid or 0)

    async def list_proactive(self, limit: int = 200, status: str = "") -> list[dict[str, Any]]:
        if status:
            rows = await (await self.db.execute(
                "SELECT * FROM hf_proactive_log WHERE status=? ORDER BY ts DESC LIMIT ?", (status, limit)
            )).fetchall()
        else:
            rows = await (await self.db.execute(
                "SELECT * FROM hf_proactive_log ORDER BY ts DESC LIMIT ?", (limit,)
            )).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            try:
                item["guard"] = json.loads(item.get("guard") or "{}")
            except (TypeError, ValueError):
                item["guard"] = {}
            out.append(item)
        return out

    async def recent_sent_proactive(
        self, session_id: str, since: float, limit: int = 3
    ) -> list[dict[str, Any]]:
        """最近真发出去的主动消息（给 inject_context 的「你最近主动发过」知会块用）。

        主动消息不再写回 AstrBot 历史后，主模型要靠这个块知道自己说过什么；
        只取 status='sent' 的真发送，跳过/候选/失败都不算。
        """
        try:
            lim = max(1, min(20, int(limit or 3)))
        except (TypeError, ValueError):
            lim = 3
        rows = await (await self.db.execute(
            "SELECT ts, text, memory_ids FROM hf_proactive_log "
            "WHERE session_id=? AND status='sent' AND ts>=? AND text!='' "
            "ORDER BY ts DESC LIMIT ?",
            (str(session_id or ""), float(since or 0), lim),
        )).fetchall()
        return [dict(r) for r in rows]

    async def proactive_baseline(self, day_start: float | None = None) -> dict[str, Any]:
        """每个会话的最后活动时间、今日已发主动条数、上次主动时间——护栏判据的真源。

        day_start：本地自然日 0 点的 epoch（service 按 AstrBot 时区算好传入）；
        不传则退化为滚动 24h 窗口（仅旧调用兜底）。用户口径：一天 = 0:00–23:59，0 点重置。
        「已发」只统计真发出去的自动消息；手动试发（guard.manual）不吃额度、不占最小间隔，
        与面板文案「试发不占额度」保持一致。
        """
        sessions: dict[str, dict[str, Any]] = {}
        rows = await (await self.db.execute(
            "SELECT session_id, user_id, MAX(ts) AS last_ts, COUNT(*) AS msgs FROM hf_gate_log "
            "WHERE session_id != '' GROUP BY session_id"
        )).fetchall()
        for row in rows:
            sessions[str(row["session_id"])] = {
                "session_id": str(row["session_id"]), "user_id": str(row["user_id"] or ""),
                "last_user_ts": float(row["last_ts"] or 0), "msgs": int(row["msgs"] or 0),
                "sent_today": 0, "last_proactive_ts": 0.0,
            }

        def _merge(sid: str, uid: str, ts: float, msgs: int = 0) -> None:
            sid = str(sid or "")
            if not sid:
                return
            item = sessions.get(sid)
            if item is None:
                item = {"session_id": sid, "user_id": str(uid or ""), "last_user_ts": 0.0,
                        "msgs": 0, "sent_today": 0, "last_proactive_ts": 0.0}
                sessions[sid] = item
            if ts > float(item.get("last_user_ts") or 0):
                item["last_user_ts"] = float(ts or 0)
            if msgs > int(item.get("msgs") or 0):
                item["msgs"] = int(msgs)
            if uid and not item.get("user_id"):
                item["user_id"] = str(uid)

        # 候选集不能只依赖门控流水：关掉门控 / 只发图片的会话也必须能进主动候选。
        # hf_messages 是每个可见会话都留档的真源；hf_corpus 补早期/导入会话。
        try:
            mrows = await (await self.db.execute(
                "SELECT session_id, sender_id, MAX(ts) AS last_ts, COUNT(*) AS msgs "
                "FROM hf_messages WHERE session_id != '' AND is_bot=0 GROUP BY session_id"
            )).fetchall()
            for row in mrows:
                _merge(str(row["session_id"]), str(row["sender_id"] or ""),
                       float(row["last_ts"] or 0), int(row["msgs"] or 0))
        except Exception as exc:                            # noqa: BLE001 旧库缺表不影响
            logger.debug(f"[StarHeart] proactive_baseline 读 hf_messages 跳过：{exc}")
        try:
            crows = await (await self.db.execute(
                "SELECT session_id, user_id, MAX(ts) AS last_ts, COUNT(*) AS msgs "
                "FROM hf_corpus WHERE session_id != '' AND is_bot=0 GROUP BY session_id"
            )).fetchall()
            for row in crows:
                _merge(str(row["session_id"]), str(row["user_id"] or ""),
                       float(row["last_ts"] or 0), int(row["msgs"] or 0))
        except Exception as exc:                            # noqa: BLE001
            logger.debug(f"[StarHeart] proactive_baseline 读 hf_corpus 跳过：{exc}")
        if day_start is None:
            day_start = now() - 86400.0
        prows = await (await self.db.execute(
            "SELECT session_id, ts, status, guard FROM hf_proactive_log "
            "WHERE ts >= ? ORDER BY ts DESC",
            (day_start - 7 * 86400,),
        )).fetchall()
        for row in prows:
            sid = str(row["session_id"])
            item = sessions.get(sid)
            if item is None:
                # 主动日志里出现过、但没有聊天记录的会话（白名单点过的），也要纳入统计
                item = {"session_id": sid, "user_id": "", "last_user_ts": 0.0, "msgs": 0,
                        "sent_today": 0, "last_proactive_ts": 0.0}
                sessions[sid] = item
            ts = float(row["ts"] or 0)
            # 只有真发出去的才算"上次主动"和"今日已发"；candidate/skipped 是观察记录，
            # 否则每次扫描自己写一条 skip 就会把最小间隔永远顶住（死锁）
            if str(row["status"]) != "sent":
                continue
            # 面板手动试发是调试动作：不算额度、也不占最小间隔（护栏只管自动扫描）
            if '"manual"' in str(row["guard"] or ""):
                continue
            item["last_proactive_ts"] = max(item["last_proactive_ts"], ts)
            if ts >= day_start:
                item["sent_today"] += 1
        row = await (await self.db.execute(
            "SELECT COUNT(*) FROM hf_proactive_log WHERE status='candidate' AND ts >= ?",
            (day_start - 7 * 86400,),
        )).fetchone()
        return {"sessions": list(sessions.values()), "candidates": int(row[0] or 0) if row else 0}

    # ---------------- 人格进化提案 ----------------
    async def add_proposal(self, item: dict[str, Any]) -> int:
        cur = await self.db.execute(
            "INSERT INTO hf_persona_proposals (ts, persona_id, base, proposal, rationale, status, in_tokens, out_tokens) "
            "VALUES (?,?,?,?,?,'pending',?,?)",
            (
                float(item.get("ts") or now()), str(item.get("persona_id") or ""), str(item.get("base") or ""),
                str(item.get("proposal") or ""), str(item.get("rationale") or ""),
                int(item.get("in_tokens") or 0), int(item.get("out_tokens") or 0),
            ),
        )
        await self.db.commit()
        return int(cur.lastrowid or 0)

    async def list_proposals(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = await (await self.db.execute(
            "SELECT * FROM hf_persona_proposals ORDER BY ts DESC LIMIT ?", (limit,)
        )).fetchall()
        return [dict(r) for r in rows]

    async def decide_proposal(self, pid: int, status: str) -> dict[str, Any] | None:
        row = await (await self.db.execute("SELECT * FROM hf_persona_proposals WHERE id=?", (pid,))).fetchone()
        if row is None:
            return None
        await self.db.execute(
            "UPDATE hf_persona_proposals SET status=?, decided_at=? WHERE id=?", (status, now(), pid)
        )
        await self.db.commit()
        item = dict(row)
        item["status"] = status
        return item

    # ---------------- 面板聚合 ----------------
    async def set_bot_self_ids(self, ids: list[str]) -> None:
        """记住「机器人自己是谁」——人物画像据此把自己排除掉。"""
        clean = []
        for x in ids or []:
            x = str(x).strip()
            if x and x not in clean:
                clean.append(x)
        if not clean:
            return
        await self.db.executemany(
            "INSERT OR IGNORE INTO hf_hidden_contacts (kind, ident, ts) VALUES ('self', ?, ?)",
            [(x, now()) for x in clean],
        )
        await self.db.commit()

    # ---------------- 人物身份：昵称 + 跨平台归并 ----------------
    #
    # 为什么必须有这张表：人物点是从 hf_gate_log.user_id 聚合来的，而 user_id 是**平台号**
    # （QQ 号 / webchat 用户名）。插件以前只在好感结算时顺手记了个 name，于是：
    #   ① 面板只能显示裸号（"其他用户怎么也是显示QQ号"）；
    #   ② 同一个人在不同平台（QQ 100000000 / webchat example_user）算成两个人 → 图谱两个点。
    # 这里把「号 → 昵称」与「别名 → 主号」两件事都落库，显示只看昵称、内部只认主号。

    # 展示优先级：手工 > 群名片 > QQ 昵称 > 好友备注 > 好感表名字 > 消息里的昵称
    # （真机教训：同一个人 QQ 昵称是「昵称A」、备注是「备注B」，只按"谁先写"挑会挑错）
    NAME_RANK = {"manual": 6, "group_card": 5, "nickname": 4, "friend": 3, "remark": 2,
                 "favour": 2, "msg": 1}

    async def _ensure_person_tables(self) -> None:
        if getattr(self, "_pnames_ready", False):
            return                      # 只建一次：以前每次 upsert 都 executescript 4 条 DDL，写锁被反复占
        self._pnames_ready = True
        await self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS hf_person_names (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL DEFAULT 'user',
                ident TEXT NOT NULL,
                name TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT 'msg',
                platform TEXT NOT NULL DEFAULT '',
                group_id TEXT NOT NULL DEFAULT '',
                seen_count INTEGER NOT NULL DEFAULT 1,
                updated_at REAL NOT NULL DEFAULT 0
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_hf_pnames_uniq
                ON hf_person_names(kind, ident, source, platform, group_id);
            CREATE TABLE IF NOT EXISTS hf_person_merge (
                alias_id TEXT PRIMARY KEY,
                canonical_id TEXT NOT NULL,
                reason TEXT NOT NULL DEFAULT 'manual',
                ts REAL NOT NULL DEFAULT 0
            );
            """
        )
        await self.db.commit()

    async def upsert_person_name(self, ident: str, name: str, *, kind: str = "user",
                                 source: str = "msg", platform: str = "", group_id: str = "") -> None:
        """记下一次「这个号叫什么」。同名不同来源都留着，取值时按 NAME_RANK 挑最可信的。"""
        ident, name = str(ident or "").strip(), str(name or "").strip()
        if not ident or not name:
            return
        await self._ensure_person_tables()
        await self.db.execute(
            "INSERT INTO hf_person_names (kind, ident, name, source, platform, group_id, seen_count, updated_at) "
            "VALUES (?,?,?,?,?,?,1,?) "
            "ON CONFLICT(kind, ident, source, platform, group_id) DO UPDATE SET "
            "name=excluded.name, seen_count=hf_person_names.seen_count+1, updated_at=excluded.updated_at",
            (kind, ident, name, source, platform, group_id, now()),
        )
        await self.db.commit()

    async def upsert_person_names_bulk(self, rows: list[dict[str, Any]]) -> int:
        """批量落昵称（一次 commit）：群成员一次能拿上千条，逐条写会把 SQLite 写锁占死。

        返回「真正发生变更的行数」——姓名没变的行直接跳过（不动 seen_count、不刷 updated_at），
        调用方据此决定要不要记日志：联系人刷新是周期性动作，没变化时不该每 2 分钟刷一条日志。
        """
        if not rows:
            return 0
        await self._ensure_person_tables()
        t = now()
        data = []
        for r in rows or []:
            ident, name = str(r.get("ident") or "").strip(), str(r.get("name") or "").strip()
            if not ident or not name:
                continue
            data.append((str(r.get("kind") or "user"), ident, name, str(r.get("source") or "msg"),
                         str(r.get("platform") or ""), str(r.get("group_id") or ""), t))
        if not data:
            return 0
        cur = await self.db.executemany(
            "INSERT INTO hf_person_names (kind, ident, name, source, platform, group_id, seen_count, updated_at) "
            "VALUES (?,?,?,?,?,?,1,?) ON CONFLICT(kind, ident, source, platform, group_id) DO UPDATE SET "
            "name=excluded.name, seen_count=hf_person_names.seen_count+1, updated_at=excluded.updated_at "
            "WHERE hf_person_names.name IS NOT excluded.name",
            data)
        await self.db.commit()
        try:
            return int(cur.rowcount or 0)
        except Exception:                                   # noqa: BLE001 拿不到行数不算错
            return len(data)

    async def remember_platform(self, pid: str, kind: str = "onebot") -> None:
        """记住「见过的 OneBot 平台实例 id」：实例短暂掉线时不该把它的会话判成别的平台。"""
        pid = str(pid or "").strip().lower()
        if not pid:
            return
        try:
            await self.db.execute(
                "INSERT OR REPLACE INTO hf_hidden_contacts (kind, ident, ts) VALUES (?,?,?)",
                (f"platform_{kind}", pid, now()))
            await self.db.commit()
        except Exception:
            pass

    async def known_platforms(self, kind: str = "onebot") -> set[str]:
        try:
            rows = await (await self.db.execute(
                "SELECT ident FROM hf_hidden_contacts WHERE kind=?", (f"platform_{kind}",))).fetchall()
            return {str(r["ident"]).lower() for r in rows if str(r["ident"]).strip()}
        except Exception:
            return set()

    async def person_names_map(self) -> dict[str, str]:
        """号 → 最优昵称（来源可信度排序，其次看得见次数）。群名用 kind='group' 同一个表。"""
        await self._ensure_person_tables()
        out: dict[str, dict[str, Any]] = {}
        for row in (await (await self.db.execute(
            "SELECT kind, ident, name, source, seen_count FROM hf_person_names WHERE name != ''"
        )).fetchall()):
            key = f"{row['kind']}:{row['ident']}"
            rank = self.NAME_RANK.get(str(row["source"] or ""), 0)
            cur = out.get(key)
            if cur is None or (rank, int(row["seen_count"] or 0)) > (cur["rank"], cur["seen"]):
                out[key] = {"name": str(row["name"]), "rank": rank, "seen": int(row["seen_count"] or 0)}
        return {k: v["name"] for k, v in out.items()}

    async def person_name_detail(self, ident: str, kind: str = "user") -> list[dict[str, Any]]:
        """这个号的所有已知名字/来源（详情页展示「这个名字是哪来的」）。"""
        await self._ensure_person_tables()
        rows = await (await self.db.execute(
            "SELECT name, source, platform, group_id, seen_count, updated_at FROM hf_person_names "
            "WHERE kind=? AND ident=? ORDER BY updated_at DESC", (kind, str(ident)),
        )).fetchall()
        return [dict(r) for r in rows]

    async def person_merges_map(self) -> dict[str, str]:
        """别名 → 主号（跟链最多 4 跳，防脏数据成环）。"""
        await self._ensure_person_tables()
        raw = {str(r["alias_id"]): str(r["canonical_id"]) for r in
               (await (await self.db.execute("SELECT alias_id, canonical_id FROM hf_person_merge")).fetchall())}
        out: dict[str, str] = {}
        for alias in raw:
            cur, seen = alias, {alias}
            for _ in range(4):
                nxt = raw.get(cur)
                if not nxt or nxt in seen:
                    break
                seen.add(nxt)
                cur = nxt
            if cur != alias:
                out[alias] = cur
        return out

    async def merge_persons(self, alias_id: str, canonical_id: str, reason: str = "manual") -> dict[str, Any]:
        """把 alias 并入 canonical（幂等）。两边都记着，可拆分、可审计。"""
        alias_id, canonical_id = str(alias_id or "").strip(), str(canonical_id or "").strip()
        if not alias_id or not canonical_id or alias_id == canonical_id:
            return {"ok": False, "error": "别名与主号必须是两个不同的号"}
        await self._ensure_person_tables()
        merges = await self.person_merges_map()
        if merges.get(canonical_id) == alias_id:
            return {"ok": False, "error": "这两个号已经互为别名，会成环；请先拆分别名"}
        # 原主号若自己也被人并走了，改挂到最终主号上，避免 A→B→C 的长链
        final = merges.get(canonical_id) or canonical_id
        await self.db.execute(
            "INSERT INTO hf_person_merge (alias_id, canonical_id, reason, ts) VALUES (?,?,?,?) "
            "ON CONFLICT(alias_id) DO UPDATE SET canonical_id=excluded.canonical_id, "
            "reason=excluded.reason, ts=excluded.ts",
            (alias_id, final, str(reason or "manual"), now()),
        )
        # 顺手把别名号已知的昵称也搬给主号（否则主号只有 QQ 号没有名字）
        for row in await self.person_name_detail(alias_id):
            await self.upsert_person_name(final, row.get("name") or "", source="manual",
                                          platform=str(row.get("platform") or ""))
        await self.db.commit()
        await self.audit("person.merge", f"{alias_id} → {final} ({reason})")
        return {"ok": True, "alias_id": alias_id, "canonical_id": final}

    async def unmerge_person(self, alias_id: str) -> dict[str, Any]:
        await self._ensure_person_tables()
        cur = await (await self.db.execute(
            "SELECT canonical_id FROM hf_person_merge WHERE alias_id=?", (str(alias_id),))).fetchone()
        if not cur:
            return {"ok": False, "error": "这个号不是别名"}
        await self.db.execute("DELETE FROM hf_person_merge WHERE alias_id=?", (str(alias_id),))
        await self.db.commit()
        await self.audit("person.unmerge", f"{alias_id} 拆自 {cur['canonical_id']}")
        return {"ok": True, "alias_id": str(alias_id)}

    async def purge_platform_sessions(self, platforms: list[str] | tuple[str, ...]) -> dict[str, Any]:
        """清掉非 QQ 平台（webchat 等）曾经落下的数据：以前没拦平台边界，库里会有残留。"""
        # 平台名只允许 [a-z0-9_.:-]，防止 LIKE 的 %/_ 通配符把别的平台一并删掉
        heads = [str(p).strip().lower() for p in (platforms or []) if str(p).strip()]
        # 注意 '_' 在 LIKE 里是"吃一个字符"的通配符：放进来 web_chat 会匹配到 webXchat 并误删它
        heads = [h for h in heads if all(c.isalnum() or c in ".:-" for c in h)]
        if not heads:
            return {"ok": False, "error": "没有指定平台（只允许字母数字与 .:- ）"}
        await self._ensure_person_tables()
        where_lt = " OR ".join("LOWER(session_id) LIKE ?" for _ in heads)
        args_lt = [f"{h}:%" for h in heads]
        deleted: dict[str, int] = {}
        tables = (("hf_gate_log", "session_id"), ("hf_corpus", "session_id"),
                  ("hf_proactive_log", "session_id"), ("hf_recall_log", "session_id"),
                  ("hf_memories", "session_id"))
        existing = {str(r["name"]) for r in
                    (await (await self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")).fetchall())}
        for tbl, col in tables:
            if tbl not in existing:                         # 老库/裁剪过的库没这张表，跳过而不是报错
                continue
            try:
                cur = await self.db.execute(f"DELETE FROM {tbl} WHERE {where_lt.replace('session_id', col)}", tuple(args_lt))
                deleted[tbl] = int(cur.rowcount or 0)
            except Exception as exc:                        # noqa: BLE001
                deleted[tbl] = -1
                logger.warning(f"[StarHeart] 清理 {tbl} 失败：{exc}")
        # 用户维度（webchat 的 user_id 就是 WebUI 用户名）
        for ident in await self._person_idents_by_platform(heads):
            for tbl, col in (("hf_favours", "user_id"), ("hf_person_profile", "person_id")):
                if tbl not in existing:
                    continue
                try:
                    cur = await self.db.execute(f"DELETE FROM {tbl} WHERE {col} = ?", (ident,))
                    deleted[tbl] = deleted.get(tbl, 0) + int(cur.rowcount or 0)
                except Exception:
                    pass
            for h in heads:      # 只删「该平台自己」的昵称行，不碰 QQ 侧同名 id
                try:
                    cur = await self.db.execute(
                        "DELETE FROM hf_person_names WHERE ident = ? AND (platform = ? OR platform = '')",
                        (ident, h))
                    deleted["hf_person_names"] = deleted.get("hf_person_names", 0) + int(cur.rowcount or 0)
                except Exception:
                    pass
            try:
                await self.db.execute("DELETE FROM hf_person_merge WHERE alias_id = ? OR canonical_id = ?", (ident, ident))
            except Exception:
                pass
        await self.db.commit()
        await self.audit("platform.purge", f"{','.join(heads)} → " + ", ".join(f"{k}:{v}" for k, v in deleted.items()))
        return {"ok": True, "platforms": heads, "deleted": deleted}

    async def _person_idents_by_platform(self, heads: list[str]) -> list[str]:
        """从会话说里倒推这些平台的 user_id（webchat 的 user_id 不在 session 尾段，得单独找）。"""
        out: list[str] = []
        for h in heads:
            for row in (await (await self.db.execute(
                "SELECT DISTINCT user_id FROM hf_gate_log WHERE LOWER(session_id) LIKE ? AND user_id != ''",
                (f"{h}:%",))).fetchall()):
                uid = str(row["user_id"] or "").strip()
                if uid and uid not in out:
                    out.append(uid)
        return out

    async def recent_contacts(self, limit: int = 300,
                              ignore_platforms: list[str] | tuple[str, ...] | None = None) -> dict[str, Any]:
        """从我们自己的库里倒推「聊过的群 / 人」——平台接口不可用时的兜底。

        为什么必须有：非 OneBot 适配器、或适配器没实现 get_group_list / get_friend_list 时，
        平台名单是空的，名单页就是一片空白 —— 用户连「选谁」都无从选起（真机就出现过）。
        这些会话是我们确实见过的，拿来做名单一定选得中。
        """
        fav = {}
        for row in (await (await self.db.execute("SELECT * FROM hf_favours")).fetchall()):
            fav[str(row["user_id"])] = dict(row)
        groups: dict[str, dict[str, Any]] = {}
        people: dict[str, dict[str, Any]] = {}

        def touch(kind: str, ident: str, sid: str, msgs: int, last_ts: float) -> None:
            if is_ignored_session(sid, ignore_platforms):
                return
            platform = sid.split(":", 1)[0] if ":" in sid else ""
            if kind == "group":
                g = groups.setdefault(ident, {"platform": platform, "group_id": ident, "name": "",
                                              "msgs": 0, "last_ts": 0.0, "derived": True})
                g["msgs"] += msgs
                g["last_ts"] = max(g["last_ts"], last_ts)
            elif kind == "private" and ident:
                p = people.setdefault(ident, {"platform": platform, "user_id": ident, "nickname": "",
                                              "remark": "", "msgs": 0, "last_ts": 0.0, "derived": True})
                p["msgs"] += msgs
                p["last_ts"] = max(p["last_ts"], last_ts)
                f = fav.get(ident) or {}
                if f.get("name"):
                    p["nickname"] = str(f["name"])

        for row in (await (await self.db.execute(
            "SELECT session_id, user_id, MAX(ts) AS last_ts, COUNT(*) AS msgs FROM hf_gate_log "
            "WHERE session_id != '' GROUP BY session_id, user_id"
        )).fetchall()):
            sid = str(row["session_id"] or "")
            kind, ident = session_kind(sid)
            touch(kind, ident, sid, int(row["msgs"] or 0), float(row["last_ts"] or 0))
        # 门控关掉/影子模式下也还有记忆与语料，从那里补齐，名单才不会漏
        for row in (await (await self.db.execute(
            "SELECT session_id, COUNT(*) AS n, MAX(updated_at) AS last_ts FROM hf_memories "
            "WHERE session_id != '' GROUP BY session_id"
        )).fetchall()):
            sid = str(row["session_id"] or "")
            kind, ident = session_kind(sid)
            touch(kind, ident, sid, 0, float(row["last_ts"] or 0))
        for row in (await (await self.db.execute(
            "SELECT session_id, COUNT(*) AS n, MAX(ts) AS last_ts FROM hf_corpus "
            "WHERE session_id != '' AND is_bot=0 GROUP BY session_id"
        )).fetchall()):
            sid = str(row["session_id"] or "")
            kind, ident = session_kind(sid)
            touch(kind, ident, sid, 0, float(row["last_ts"] or 0))
        for uid, f in fav.items():
            if uid:
                p = people.setdefault(uid, {"platform": "", "user_id": uid, "nickname": str(f.get("name") or ""),
                                            "remark": "", "msgs": 0, "last_ts": 0.0, "derived": True})
                if f.get("name") and not p.get("nickname"):
                    p["nickname"] = str(f["name"])
        glist = sorted(groups.values(), key=lambda x: x.get("last_ts") or 0, reverse=True)[:limit]
        plist = sorted(people.values(), key=lambda x: x.get("last_ts") or 0, reverse=True)[:limit]
        return {"groups": glist, "friends": plist}

    # ---------------- 人物画像（persona profile）----------------
    async def memories_since(self, ts: float, persona_id: str = "") -> int:
        """ts 之后新增的记忆条数（人格进化、画像刷新的触发依据）。

        persona_id 非空时只数「该人格的 + 未归属的」新记忆 —— 别人格在聊不该触发本格进化；
        未归属（persona_id 为空）的记忆人人有份，避免关掉记忆隔离后进化永远不触发。
        """
        sql = "SELECT COUNT(*) AS n FROM hf_memories WHERE status='active' AND created_at > ?"
        args: list[Any] = [float(ts or 0)]
        if str(persona_id or "").strip():
            sql += " AND (persona_id = ? OR persona_id IS NULL OR persona_id = '')"
            args.append(str(persona_id).strip())
        row = await (await self.db.execute(sql, tuple(args))).fetchone()
        return int(row["n"] or 0) if row else 0

    async def get_proposal(self, pid: int) -> dict[str, Any] | None:
        """按 id 取一条人格提案（审阅前要用它校验 base 是否过期）。"""
        row = await (await self.db.execute("SELECT * FROM hf_persona_proposals WHERE id=?", (int(pid),))).fetchone()
        return dict(row) if row else None

    async def ensure_strength_updated_at(self) -> None:
        """补 strength_updated_at 列：衰减必须「只补算经过的时间」，否则同一段年龄会被反复乘（复利崩塌）。"""
        cols = {str(r[1]) for r in (await (await self.db.execute("PRAGMA table_info(hf_memories)")).fetchall())}
        if "strength_updated_at" not in cols:
            await self.db.execute("ALTER TABLE hf_memories ADD COLUMN strength_updated_at REAL NOT NULL DEFAULT 0")
            await self.db.commit()

    async def repair_memory_timebase(self) -> dict[str, int]:
        """一次性修时间基（幂等）：last_used_at 全 0 → 用发生时间回填；未来 occurred_at → 夹回 created_at。
        这两条真机实测都存在，会让「未来时间」的记忆永久免疫衰减。"""
        await self.ensure_strength_updated_at()
        t = now()
        c1 = (await self.db.execute(
            "UPDATE hf_memories SET last_used_at = COALESCE(NULLIF(last_used_at,0), occurred_at, created_at, ?) "
            "WHERE COALESCE(last_used_at,0) = 0", (t,))).rowcount
        c2 = (await self.db.execute(
            "UPDATE hf_memories SET occurred_at = created_at WHERE occurred_at > ?", (t + 86400,))).rowcount
        c3 = (await self.db.execute(
            "UPDATE hf_memories SET strength_updated_at = COALESCE(NULLIF(strength_updated_at,0), "
            "COALESCE(NULLIF(last_used_at,0), occurred_at, created_at, ?)) WHERE COALESCE(strength_updated_at,0)=0",
            (t,))).rowcount
        await self.db.commit()
        if c1 or c2 or c3:
            await self.audit("memory.timebase_repair", f"last_used={c1} future_occurred={c2} base={c3}")
        return {"last_used_filled": int(c1 or 0), "future_fixed": int(c2 or 0), "base_filled": int(c3 or 0)}

    async def reinforce_memories(self, ids: list[Any], delta: float = 0.06) -> int:
        """被召回/命中 → 加固：strength +Δ（上限 1.0）、recalls+1、刷新 last_used_at 与衰减基线。
        这是用户要的「只要频繁提起，权重就会被拉高」的落点。"""
        await self.ensure_strength_updated_at()
        n = 0
        t = now()
        for mid in ids or []:
            raw = str(mid).lstrip("m")
            try:
                cur = await self.db.execute(
                    "UPDATE hf_memories SET strength=MIN(1.0, COALESCE(strength,0) + ?), "
                    "recalls=COALESCE(recalls,0)+1, last_used_at=?, strength_updated_at=?, updated_at=? "
                    "WHERE id=? AND status='active'",
                    (float(delta), t, t, t, raw),
                )
                n += int(cur.rowcount or 0)
            except Exception:
                continue
        await self.db.commit()
        return n

    async def revive_stale(self, poignancy_min: float = 8.0, strength_floor: float = 0.45,
                           limit: int = 400) -> dict[str, int]:
        """存量救援（一次性）：把被复利衰减压死的 stale 记忆拉回 active。
        只救「情绪浓度高」或「本来就不弱」的，避免无差别复活噪音。"""
        rows = await (await self.db.execute(
            "SELECT id, poignancy, strength FROM hf_memories WHERE status IN ('stale','decayed') "
            "AND (poignancy >= ? OR strength >= 0.06) ORDER BY poignancy DESC, strength DESC LIMIT ?",
            (float(poignancy_min), int(limit)))).fetchall()
        ids = [int(r["id"]) for r in rows]
        if not ids:
            return {"revived": 0}
        marks = ",".join("?" * len(ids))
        t = now()
        cur = await self.db.execute(
            f"UPDATE hf_memories SET status='active', strength=MAX(COALESCE(strength,0), ?), "
            f"strength_updated_at=?, updated_at=? WHERE id IN ({marks})",
            [float(strength_floor), t, t, *ids])
        await self.db.commit()
        n = int(cur.rowcount or 0)
        await self.audit("memory.revive_stale", f"revived={n} limit={limit}")
        return {"revived": n, "scanned": len(ids)}

    # ---------------- 画像（L0 人物 / L1 群聊，按人格隔离）----------------
    # 表用三点定位：subject_kind('user'|'group') + person_id + persona_id。
    #   · subject_kind 对应图谱里的层：'user' = L0 人，'group' = L1 群聊（面板子菜单「画像」两层同此口径）
    #   · 同一个人在不同人格下是两份画像，互相看不见（人格隔离）。

    @staticmethod
    def _kind_of(subject_kind: str) -> str:
        return "group" if str(subject_kind or "").lower() == "group" else "user"

    async def profile_get(self, sid: str, subject_kind: str = "user", persona_id: str = "") -> dict[str, Any] | None:
        """读某份画像。persona_id 留空 = 不限人格（返回最近更新的一份，并把它的 persona_id 一起带出去）。"""
        k = self._kind_of(subject_kind)
        pwhere, pargs = self._persona_where(persona_id, column="persona_id")
        row = await (await self.db.execute(
            "SELECT subject_kind, person_id, persona_id, profile, updated_at, mem_count, msg_count, built_by "
            "FROM hf_person_profile WHERE subject_kind = ? AND person_id = ?" + pwhere +
            " ORDER BY updated_at DESC LIMIT 1",
            (k, str(sid), *pargs),
        )).fetchone()
        if not row:
            return None
        # 空 persona_id = 不限人格（面板"全部"视图）。这时**不能假装这是某个具体人格的画像**：
        # 把「这一主体都有哪些人格写过画像」一起带出去，由面板如实标注，避免 B 的画像被当成 A 的。
        ambiguous = False
        persona_ids: list[str] = []
        if not str(persona_id or ""):
            try:
                persona_ids = await self.profile_persona_ids(sid, k)
                ambiguous = len(persona_ids) > 1
            except Exception:
                persona_ids = []
        try:
            body = json.loads(row["profile"] or "{}")
        except Exception:
            body = {"one_line": str(row["profile"] or "")}
        if not isinstance(body, dict):
            body = {"one_line": str(body)}
        return {
            "subject_kind": k, "person_id": str(row["person_id"]), "persona_id": str(row["persona_id"] or ""),
            "ambiguous": ambiguous, "persona_ids": persona_ids,
            "profile": body, "updated_at": float(row["updated_at"] or 0),
            "mem_count": int(row["mem_count"] or 0), "msg_count": int(row["msg_count"] or 0),
            "built_by": str(row["built_by"] or ""),
        }

    async def profile_save(self, sid: str, profile: dict[str, Any], mem_count: int = 0, msg_count: int = 0,
                           built_by: str = "", subject_kind: str = "user", persona_id: str = "") -> None:
        k = self._kind_of(subject_kind)
        # 面板人格选择器里有 '__none__'（未归属）这一项；读/删把它当"未归属桶"，写入也必须归一，
        # 否则会落成一条字面 persona_id='__none__' 的行 —— 写了读不到、也删不掉（实测过）。
        if str(persona_id or "") == "__none__":
            persona_id = ""
        await self.db.execute(
            "INSERT INTO hf_person_profile"
            " (subject_kind, person_id, persona_id, profile, updated_at, mem_count, msg_count, built_by) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(subject_kind, person_id, persona_id) DO UPDATE SET "
            "profile = excluded.profile, updated_at = excluded.updated_at, "
            "mem_count = excluded.mem_count, msg_count = excluded.msg_count, built_by = excluded.built_by",
            (k, str(sid), str(persona_id or ""), json.dumps(profile, ensure_ascii=False), time.time(),
             int(mem_count), int(msg_count), str(built_by)),
        )
        await self.db.commit()

    async def profile_delete(self, sid: str, subject_kind: str = "user", persona_id: str = "") -> bool:
        k = self._kind_of(subject_kind)
        if str(persona_id or "") == "__none__":
            persona_id = ""
        # 空人格 = 不限人格 → 一定要走显式批量子句 '__all__'，否则这里会**跨人格删掉别人的画像**
        # （人格隔离是硬约束；删除不可恢复，宁可拒绝也不能猜）。
        if not str(persona_id or "") and str(persona_id) != "__all__":
            logger.warning("[StarHeart] 拒绝无指定人格的画像删除（避免跨人格误删）：kind=%s id=%s", k, sid)
            return False
        if str(persona_id) == "__all__":
            cur = await self.db.execute(
                "DELETE FROM hf_person_profile WHERE subject_kind = ? AND person_id = ?", (k, str(sid)))
            await self.db.commit()
            return bool(cur.rowcount)
        pwhere, pargs = self._persona_where(persona_id, column="persona_id")
        cur = await self.db.execute(
            "DELETE FROM hf_person_profile WHERE subject_kind = ? AND person_id = ?" + pwhere,
            (k, str(sid), *pargs))
        await self.db.commit()
        return bool(cur.rowcount)

    async def profile_persona_ids(self, sid: str, subject_kind: str = "user") -> list[str]:
        """这个主体都有哪些人格的画像（面板要如实显示"人格 A 有、人格 B 没有"）。"""
        k = self._kind_of(subject_kind)
        rows = await (await self.db.execute(
            "SELECT persona_id FROM hf_person_profile WHERE subject_kind = ? AND person_id = ?", (k, str(sid)))).fetchall()
        return sorted({str(r["persona_id"] or "") for r in rows})

    async def repair_profile_personas(self) -> int:
        """把「未归属人格」的画像回填到它真正所属的人格域（幂等，可反复跑）。

        依据：画像由某个主体的记忆生成，而记忆属于某个会话 → 会话属于某个人格。
        按「这个人的记忆落在哪个人格最多」投票；投不出来就保持"未归属"，面板如实显示，不猜。
        """
        try:
            rows = await (await self.db.execute(
                "SELECT subject_kind, person_id FROM hf_person_profile "
                "WHERE persona_id IS NULL OR persona_id = ''")).fetchall()
        except Exception:
            return 0
        fixed = 0
        failed = 0
        for r in rows:
            sid = str(r["person_id"] or "")
            kind = str(r["subject_kind"] or "user")
            if not sid:
                continue
            pat, esc = self._like_suffix(sid)
            pid = ""
            try:
                if kind == "group":
                    # 群：直接按「会话串以 :GroupMessage:<群号> 结尾」的记忆投票
                    vote = await (await self.db.execute(
                        "SELECT persona_id, COUNT(*) AS n FROM hf_memories "
                        "WHERE status != 'deleted' AND persona_id != '' AND session_id LIKE ? ESCAPE '" + esc + "' "
                        "GROUP BY persona_id ORDER BY n DESC LIMIT 1", (pat,))).fetchone()
                    pid = str(vote["persona_id"]) if vote else ""
                else:
                    # 人物：先按"以该用户号结尾的会话"（私聊）投票；
                    # 只在群里出现过的人，会话串尾部是群号、永远投不出来 —— 于是再用
                    # 「他在哪些群发过言 → 那些群会话的记忆属于哪个人格」投一次（真记录，不是猜）。
                    vote = await (await self.db.execute(
                        "SELECT persona_id, COUNT(*) AS n FROM hf_memories "
                        "WHERE status != 'deleted' AND persona_id != '' AND session_id LIKE ? ESCAPE '" + esc + "' "
                        "GROUP BY persona_id ORDER BY n DESC LIMIT 1", (pat,))).fetchone()
                    pid = str(vote["persona_id"]) if vote else ""
                    if not pid:
                        vote = await (await self.db.execute(
                            "SELECT m.persona_id AS persona_id, COUNT(*) AS n FROM hf_memories m "
                            "WHERE m.status != 'deleted' AND m.persona_id != '' "
                            "AND m.session_id LIKE '%:GroupMessage:%' "
                            "AND m.session_id IN (SELECT DISTINCT session_id FROM hf_messages WHERE sender_id = ?) "
                            "GROUP BY m.persona_id ORDER BY n DESC LIMIT 1", (sid,))).fetchone()
                        pid = str(vote["persona_id"]) if vote else ""
            except Exception:
                failed += 1
                continue
            if not pid:
                continue
            try:
                await self.db.execute(
                    "UPDATE hf_person_profile SET persona_id = ? "
                    "WHERE subject_kind = ? AND person_id = ? AND (persona_id IS NULL OR persona_id = '')",
                    (pid, str(r["subject_kind"] or "user"), sid))
                fixed += 1
            except Exception:
                continue
        if fixed or failed:
            await self.db.commit()
            if fixed:
                logger.info(f"[StarHeart] 画像人格回填：{fixed} 份画像已归入对应人格域"
                            + (f"（{failed} 份投票失败，保持未归属）" if failed else ""))
            try:      # 回填属于会改变数据归属的动作，必须留审计（以前只写日志）
                await self.audit("profile.persona_backfill", f"归位 {fixed} 份 / 投票失败 {failed} 份")
            except Exception:
                pass
        return fixed

    async def person_profile_delete(self, pid: str, subject_kind: str = "user", persona_id: str = "") -> bool:
        """删掉一份画像（垃圾画像清理 / 用户要求重来）。默认只删人物画像。"""
        return await self.profile_delete(pid, subject_kind, persona_id)

    async def archive_memories(self, ids: list[Any], factor: float | None = None, why: str = "") -> int:
        """把记忆归档（可被「复活历史记忆」找回、被提及也会自动拉回）—— 比物理删除稳。

        factor=None 时按 why 决定：生命周期归档（why="idle"）**不降权**，只翻状态；
        撤回路径（why 为空/其它）仍按 0.3 降权。

        为什么 idle 不再降权：旧默认 0.3 是「自动失忆链」的第一环 ——
        0.5 × 0.3 = 0.15 < forget_min_strength(0.35)，归档即等于判死，30 天后必然被遗忘。
        归档应当是「降低排序权重」，不是「加速死亡」。
        （历史教训：这里曾有两份同名定义互相覆盖，factor 版被 why 版盖掉 → 降权静默失效。）
        """
        if factor is None:
            factor = 1.0 if why == "idle" else 0.3
        n = 0
        for mid in ids or []:
            raw = str(mid).lstrip("m")
            try:
                cur = await self.db.execute(
                    "UPDATE hf_memories SET status='archived', archived=1, strength=MAX(0.0, strength*?), "
                    "forget_stage=CASE WHEN ? != '' THEN ? ELSE forget_stage END, updated_at=? WHERE id=?",
                    (float(factor), str(why or ""), str(why or ""), now(), raw),
                )
                n += int(cur.rowcount or 0)
            except Exception:
                continue
        await self.db.commit()
        return n

    async def person_profile_get(self, pid: str, persona_id: str = "") -> dict[str, Any] | None:
        """[兼容包装] 读人物画像（L0 人）。群聊画像请用 profile_get(sid, "group", persona_id)。"""
        return await self.profile_get(pid, "user", persona_id)

    async def person_profile_save(self, pid: str, profile: dict[str, Any], mem_count: int = 0,
                                  msg_count: int = 0, built_by: str = "", persona_id: str = "",
                                  subject_kind: str = "user") -> None:
        """[兼容包装] 写画像；带 subject_kind 即可写群聊画像（L1）。"""
        await self.profile_save(pid, profile, mem_count, msg_count, built_by, subject_kind, persona_id)

    async def subjects_needing_profile(self, subject_kind: str = "user", min_memories: int = 5,
                                       refresh_hours: int = 72, limit: int = 2,
                                       persona_id: str = "") -> list[dict[str, Any]]:
        """挑出"该生成 / 该刷新画像"的主体：L0 人物（默认）或 L1 群聊。

        为什么按"记忆数"卡阈值：材料太少的画像就是模型瞎猜，宁可先不生成（面板会说明原因）。
        人物材料来自门控流水（谁说了多少话）；群聊材料来自群会话里的记忆与语料（带人格过滤，天然隔离）。
        """
        import time as _t
        now = _t.time()
        stale_before = now - max(1, int(refresh_hours)) * 3600
        k = self._kind_of(subject_kind)
        cand: list[tuple[str, int, float]] = []
        if k == "group":
            pwhere, pargs = self._persona_where(persona_id)
            for row in (await (await self.db.execute(
                "SELECT session_id, COUNT(*) AS n, MAX(COALESCE(occurred_at, created_at)) AS last_ts "
                "FROM hf_memories WHERE status IN ('active','stale') AND session_id LIKE '%:GroupMessage:%'" + pwhere +
                " GROUP BY session_id ORDER BY last_ts DESC LIMIT 400", tuple(pargs),
            )).fetchall()):
                parts = str(row["session_id"] or "").split(":")
                if len(parts) < 3 or not parts[2]:
                    continue
                cand.append((parts[2], int(row["n"] or 0), float(row["last_ts"] or 0)))
        else:
            _ign = [str(x).strip().lower() for x in (await self.get_config("person_exclude_platforms", []) or []) if str(x).strip()]
            _nots = "".join(" AND LOWER(g.session_id) NOT LIKE ? " for _ in _ign)
            for row in (await (await self.db.execute(
                "SELECT g.user_id AS uid, COUNT(*) AS msgs, MAX(g.ts) AS last_ts "
                "FROM hf_gate_log g WHERE g.user_id != '' AND g.session_id != ''" + _nots +
                "GROUP BY g.user_id HAVING msgs > 0 ORDER BY last_ts DESC LIMIT 400",
                tuple("%s:%%" % p for p in _ign),
            )).fetchall()):
                cand.append((str(row["uid"] or ""), int(row["msgs"] or 0), float(row["last_ts"] or 0)))
        out: list[dict[str, Any]] = []
        for sid, msgs, last_ts in cand:
            if not sid:
                continue
            existing = await self.profile_get(sid, k, persona_id)
            updated = float((existing or {}).get("updated_at") or 0)
            p_mem = int((existing or {}).get("mem_count") or 0)
            # 从没生成过 或 过期（且有新互动）→ 需要生成。
            # 特别注意：p_mem=0 说明库里那份画像没有材料依据（真机出现过「身份未知，仅知为人物 id」），
            # 这种必须允许重建，否则"最近更新过"会把它永远钉死在库里。
            if updated and updated > stale_before and last_ts <= updated and p_mem > 0:
                continue
            dossier = await self.subject_dossier(sid, k, limit=40, persona_id=persona_id)
            mem_n = int(dossier.get("mem_count") or 0)
            if mem_n < int(min_memories):
                continue          # 材料不够，先不生成
            if updated and mem_n <= p_mem and last_ts <= updated:
                continue          # 没有新记忆，不浪费模型调用
            out.append({"person_id": sid, "subject_kind": k, "persona_id": persona_id,
                        "name": str(dossier.get("name") or ""), "mem_count": mem_n,
                        "msgs": int(dossier.get("msgs") or msgs or 0),
                        "last_ts": last_ts, "updated_at": updated})
            if len(out) >= max(1, int(limit)):
                break
        return out

    async def persons_needing_profile(self, min_memories: int = 5, refresh_hours: int = 72,
                                      limit: int = 2, persona_id: str = "") -> list[dict[str, Any]]:
        """[兼容包装] 人物画像（L0）的候选名单。"""
        return await self.subjects_needing_profile("user", min_memories, refresh_hours, limit, persona_id)

    async def subject_dossier(self, sid: str, subject_kind: str = "user", limit: int = 80,
                              persona_id: str = "") -> dict[str, Any]:
        """组一个主体档案（L0 人物 / L1 群聊）：会话、记忆、常出现的实体与话题、互动量。

        为什么按"会话"找记忆：记忆表里只有 session_id，没有 user_id；
        hf_gate_log 里才有 (session_id, user_id) 的对应关系 —— 这是唯一可信的归属来源。
        群聊反过来：它的会话串就是 `平台:GroupMessage:群号`，记忆直接按它归堆，成员从语料表取。
        persona_id 非空时只统计这个人格的记忆/语料 —— 画像因此天然按人格隔离。
        """
        sid = str(sid)
        k = self._kind_of(subject_kind)
        pwhere, pargs = self._persona_where(persona_id)
        # 称呼一律走昵称表（show 名字、不 show 号）——以前返回裸号，画像 prompt 里就变成了号码
        names: dict[str, Any] = {}
        try:
            names = await self.person_names_map() or {}
        except Exception:
            names = {}

        def _nm(key: str) -> str:
            v = names.get(key)
            if isinstance(v, dict):
                return str(v.get("name") or "")
            return str(v or "")

        sessions: list[str] = []
        members: list[dict[str, Any]] = []
        msgs = 0
        last_ts = 0.0
        if k == "group":
            # 群号里若含 % / _ 会被 LIKE 当通配符（实测 100_1 会吞并 100X1 的记忆）→ 转义 + 显式 ESCAPE
            _esc_sid = self._like_escape(sid)
            for row in (await (await self.db.execute(
                "SELECT session_id, COUNT(*) AS n, MAX(COALESCE(occurred_at, created_at)) AS last_ts "
                "FROM hf_memories WHERE session_id LIKE ? ESCAPE '" + self.LIKE_ESC + "'" + pwhere
                + " GROUP BY session_id",
                (f"%:GroupMessage:{_esc_sid}", *pargs),
            )).fetchall()):
                sessions.append(str(row["session_id"] or ""))
                last_ts = max(last_ts, float(row["last_ts"] or 0))
            sessions = [x for x in dict.fromkeys(sessions) if x]
            if sessions:
                # SQLite 变量上限（本机 32766 个 ?）：一旦撞上整段取料就炸（审查实证：4 万会话 → too many SQL variables）
                if len(sessions) > 800:
                    logger.warning(f"[StarHeart] 画像取料：会话数 {len(sessions)} 超过 800 上限，本轮只取前 800 个")
                    sessions = sessions[:800]
                ph = ",".join("?" for _ in sessions)
                # 成员与活跃度取自**聊天记录表**（真机实测：hf_messages 2768 行/群 100000004 有 53 个发言人，
                # 而 hf_corpus 只有 271 行）—— 语料表只当兜底。
                # 注意：聊天记录没有人格列（人格会随时间切换），所以这部分是「该群的整体活跃」，
                # 与人格无关；画像**正文**（记忆 / 话题 / 线索）仍然严格按人格过滤。
                for sql in (
                    f"SELECT sender_id AS uid, COUNT(*) AS n, MAX(ts) AS last_ts FROM hf_messages "
                    f"WHERE session_id IN ({ph}) AND sender_id != '' AND is_bot = 0 "
                    f"GROUP BY sender_id ORDER BY n DESC LIMIT 20",
                    f"SELECT user_id AS uid, COUNT(*) AS n, MAX(ts) AS last_ts FROM hf_corpus "
                    f"WHERE session_id IN ({ph}) AND user_id != '' AND is_bot = 0 "
                    f"GROUP BY user_id ORDER BY n DESC LIMIT 20",
                ):
                    members = []
                    try:
                        for row in (await (await self.db.execute(sql, tuple(sessions))).fetchall()):
                            members.append({"user_id": str(row["uid"]), "msgs": int(row["n"] or 0),
                                            "name": _nm(f"user:{row['uid']}")})
                            msgs += int(row["n"] or 0)
                            last_ts = max(last_ts, float(row["last_ts"] or 0))
                    except Exception:                          # 表可能还没建（老库）→ 试下一个来源
                        members = []
                    if members:
                        break
            pname = _nm(f"group:{sid}")
        else:
            pname = _nm(f"user:{sid}")
            for row in (await (await self.db.execute(
                "SELECT session_id, COUNT(*) AS n, MAX(ts) AS last_ts FROM hf_gate_log "
                "WHERE user_id = ? AND session_id != '' GROUP BY session_id", (sid,),
            )).fetchall()):
                sessions.append(str(row["session_id"] or ""))
                msgs += int(row["n"] or 0)
                last_ts = max(last_ts, float(row["last_ts"] or 0))
            # ⚠️ 不能只靠门控流水：面板有「清空流水」，门控关掉时也不写流水 —— 真机实测清空后
            # 画像会突然说「这个人还没有任何记忆」，而记忆其实都在。这里按「会话串以 :该用户号 结尾」
            # 直接从记忆表补全（私聊会话的尾部就是对方号）。群会话尾是群号，不会误并进来。
            try:
                _ppat, _pesc = self._like_suffix(sid)
                for row in (await (await self.db.execute(
                    "SELECT session_id, COUNT(*) AS n FROM hf_memories WHERE session_id LIKE ? ESCAPE '"
                    + _pesc + "'" + pwhere + " GROUP BY session_id", (_ppat, *pargs),
                )).fetchall()):
                    sessions.append(str(row["session_id"] or ""))
            except Exception:                                  # noqa: BLE001 补全失败不影响门控那一路
                pass
            # 私聊会话（有可能门控日志里还没记上）
            sessions.append(f"private:{sid}" if not str(sid).startswith("private:") else sid)
            sessions = [x for x in dict.fromkeys(sessions) if x]

        name = ""
        try:
            row = await (await self.db.execute(
                # hf_entities 没有 ident 列（只有 canonical = "kind:name"）。
                # 原先这条 SQL 必抛异常、又被 except 吞掉 → 人物档案里的称呼永远是空的。
                "SELECT name FROM hf_entities WHERE canonical IN (?, ?) LIMIT 1",
                (f"person:{sid}", f"session:{sid}"),
            )).fetchone()
            name = str(row["name"] or "") if row else ""
        except Exception:
            name = ""
        if pname:                      # 昵称表里的名字优先（号只留在详情里）
            name = pname
        elif name == sid:              # 实体名就是裸号 → 当没有称呼，别把号写进画像
            name = ""

        # SQLite 变量上限（本机 32766 个 ?）：一旦撞上整段取料就炸（审查实证：4 万会话 → too many SQL variables）
        if len(sessions) > 800:
            logger.warning(f"[StarHeart] 画像取料：会话数 {len(sessions)} 超过 800 上限，本轮只取前 800 个")
            sessions = sessions[:800]
        ph = ",".join("?" for _ in sessions)
        mems: list[dict[str, Any]] = []
        if sessions:
            for row in (await (await self.db.execute(
                f"SELECT id, summary, content, atom_type, strength, poignancy, entities, cues, evidence, "
                # 取料口径：active + stale 都是"这人确实说过"的内容（stale 只是被撤回降过权，
                # 已按 strength 排后）；archived（归档可找回）/forgotten 不算依据 —— 真机上 archived 有 283 条，
                # 全排除会让画像几乎没料可取。
                f"occurred_at, session_id, tags FROM hf_memories WHERE status IN ('active','stale') AND session_id IN ({ph}) "
                + pwhere + " ORDER BY strength DESC, COALESCE(occurred_at, created_at) DESC LIMIT ?",
                (*sessions, *pargs, int(limit)),
            )).fetchall()):
                mems.append({
                    "id": int(row["id"]), "summary": str(row["summary"] or ""),
                    "content": str(row["content"] or ""), "atom_type": str(row["atom_type"] or ""),
                    "strength": float(row["strength"] or 0), "poignancy": int(row["poignancy"] or 0),
                    "entities": _jlist(row["entities"]), "cues": _jlist(row["cues"]),
                    "evidence": str(row["evidence"] or ""), "occurred_at": float(row["occurred_at"] or 0),
                    "session_id": str(row["session_id"] or ""),
                })
        # 常出现的实体 / 话题（她的记忆里聚合出来）
        ent_count: dict[str, int] = {}
        cue_count: dict[str, int] = {}
        for m in mems:
            for e in m["entities"]:
                ent_count[str(e)] = ent_count.get(str(e), 0) + 1
            for c in m["cues"]:
                cue_count[str(c)] = cue_count.get(str(c), 0) + 1
        top_ent = sorted(ent_count.items(), key=lambda kv: -kv[1])[:24]
        top_cue = sorted(cue_count.items(), key=lambda kv: -kv[1])[:24]
        return {
            "person_id": sid, "subject_kind": k, "persona_id": str(persona_id or ""),
            "name": name, "sessions": sessions, "msgs": msgs, "last_ts": last_ts,
            "memories": mems, "mem_count": len(mems), "members": members,
            "top_entities": [{"name": kk, "n": v} for kk, v in top_ent],
            "top_cues": [{"name": kk, "n": v} for kk, v in top_cue],
        }

    async def person_dossier(self, pid: str, limit: int = 80, persona_id: str = "") -> dict[str, Any]:
        """[兼容包装] 人物档案（L0）。群聊档案请用 subject_dossier(gid, "group", ...)。"""
        return await self.subject_dossier(pid, "user", limit, persona_id)

    async def person_overview(self, limit: int = 60, exclude: set[str] | None = None,
                              ignore_platforms: list[str] | tuple[str, ...] | None = None,
                              persona_id: str = "") -> dict[str, Any]:
        """给人看的人物画像：**按人聚合**，群聊单独成列。

        为什么不再按会话聚合：一个真人在私聊 + N 个群里就是 N 条 session_id，
        按会话分组会把同一个人拆成好几行；群会话那一行还会把「群号」摆在人物列表里当 QQ
        （群里没有 user_id 的记忆行只能拿群号顶），看上去就是「人物读不准、QQ 各不相同」。
        这里以 user_id 当人物主键，群聊只作为「这人出现在哪些群」的维度，另外单独列出来。
        """
        def blank_person(uid: str) -> dict[str, Any]:
            return {"user_id": uid, "name": "", "name_known": False, "display": uid,
                    "aliases": [], "platforms": [], "msgs": 0, "private_msgs": 0, "group_msgs": 0,
                    "memories": 0, "recalls": 0, "last_ts": 0.0, "daily": None, "lifetime": None,
                    "zone": None, "groups": [], "sessions": [],
                    "level": 0, "profile": None, "profile_other_personas": []}      # L0 人物

        def blank_group(gid: str) -> dict[str, Any]:
            return {"group_id": gid, "name": "", "display": gid, "msgs": 0, "memories": 0,
                    "recalls": 0, "last_ts": 0.0, "people": 0,
                    "level": 1, "profile": None, "profile_other_personas": []}      # L1 群聊

        # bot 自己怎么认：**不猜**。语料表里 is_bot=1 的行存的是「当时那个会话的发送者」，
        # 拿它当机器人 id 会把真人整批排除掉（真机上就发生了：两个人全被当成 bot）。
        # 这里只用两个可信来源：上层传进来的（平台 get_login_info）+ 落库记下的 kind='self'。
        bots: set[str] = {str(x) for x in (exclude or set()) if str(x)}
        try:
            for row in (await (await self.db.execute("SELECT ident FROM hf_hidden_contacts WHERE kind='self'")).fetchall()):
                bots.add(str(row["ident"]))
        except Exception:
            pass

        people: dict[str, dict[str, Any]] = {}
        groups: dict[str, dict[str, Any]] = {}

        # 1) 门控流水：谁说了多少话、最近什么时候说
        for row in (await (await self.db.execute(
            "SELECT session_id, user_id, MAX(ts) AS last_ts, COUNT(*) AS msgs FROM hf_gate_log "
            "WHERE session_id != '' GROUP BY session_id, user_id"
        )).fetchall()):
            sid = str(row["session_id"] or "")
            if is_ignored_session(sid, ignore_platforms):     # 非 QQ 平台的会话不进人物/群列表
                continue
            uid = str(row["user_id"] or "")
            msgs = int(row["msgs"] or 0)
            last_ts = float(row["last_ts"] or 0)
            kind, ident = session_kind(sid)
            if kind == "group":
                g = groups.setdefault(ident, blank_group(ident))
                g["msgs"] += msgs
                g["last_ts"] = max(g["last_ts"], last_ts)
                if uid and uid not in bots:
                    person = people.setdefault(uid, blank_person(uid))
                    # 群里的发言也算这个人的活跃（私聊/群聊分开记，面板分开展示）
                    person["msgs"] += msgs
                    person["group_msgs"] += msgs
                    person["last_ts"] = max(person["last_ts"], last_ts)
                    if ident not in person["groups"]:
                        person["groups"].append(ident)
            elif kind == "private" and uid:
                person = people.setdefault(uid, blank_person(uid))
                person["msgs"] += msgs
                person["private_msgs"] += msgs
                person["last_ts"] = max(person["last_ts"], last_ts)

        # 2) 记忆：私聊记忆归本人，群记忆归群（群记忆没有 user_id，硬塞给人就是错）
        #    带 persona_id 时只数这个人格的记忆 —— 面板上两个人格的「记忆数」必须各算各的。
        mem_where, mem_args = self._persona_where(persona_id)
        for row in (await (await self.db.execute(
            "SELECT session_id, COUNT(*) AS n, SUM(recalls) AS r, MAX(updated_at) AS last_ts FROM hf_memories "
            "WHERE session_id != ''" + mem_where + " GROUP BY session_id", tuple(mem_args),
        )).fetchall()):
            sid = str(row["session_id"] or "")
            if is_ignored_session(sid, ignore_platforms):
                continue
            n = int(row["n"] or 0)
            r = int(row["r"] or 0)
            last_ts = float(row["last_ts"] or 0)
            kind, ident = session_kind(sid)
            if kind == "group":
                g = groups.setdefault(ident, blank_group(ident))
                g["memories"] += n
                g["recalls"] += r
                g["last_ts"] = max(g["last_ts"], last_ts)
            elif kind == "private" and ident:
                person = people.setdefault(ident, blank_person(ident))
                person["memories"] += n
                person["recalls"] += r
                person["last_ts"] = max(person["last_ts"], last_ts)

        # 3) 好感：名字 / 档位 / 好感分挂到人身上（被忽略平台的号不参与，否则 webchat 的人又会冒出来）
        ignored_idents: set[str] = set()
        try:
            ignored_idents = set(await self._person_idents_by_platform(
                [str(p).strip().lower() for p in (ignore_platforms or []) if str(p).strip()]))
        except Exception:
            ignored_idents = set()
        for row in (await (await self.db.execute("SELECT * FROM hf_favours")).fetchall()):
            fav = dict(row)
            uid = str(fav.get("user_id") or "")
            if not uid or uid in ignored_idents:
                continue
            person = people.setdefault(uid, blank_person(uid))
            # person_overview 里 sessions 从来没被填过（循环只写 msgs/groups）→ 这里直接回查，
            # 否则 platforms 恒为空（面板拿不到「这个人在哪些平台出现过」）。
            try:
                for r2 in (await (await self.db.execute(
                        "SELECT DISTINCT session_id FROM hf_gate_log WHERE user_id = ? LIMIT 20", (uid,))).fetchall()):
                    pf = session_platform(str(r2["session_id"]))
                    if pf and pf not in person["platforms"]:
                        person["platforms"].append(pf)
            except Exception:
                pass
            person["name"] = str(fav.get("name") or "")
            person["daily"] = fav.get("daily")
            person["lifetime"] = fav.get("lifetime")
            person["zone"] = fav.get("zone")

        # 4) 跨平台归并：同一个人的多个号（QQ 号 / webchat 用户名 / 其他平台）折叠成一个点。
        #    以前不归并 → 图谱上同一个人两个点；这里按 hf_person_merge 把别名折进主号。
        merges = await self.person_merges_map()
        # 先算最终归属链（A→B→C 一次性折到 C）再聚合：旧实现边 pop 边 setdefault，
        # 链式别名会把已 pop 的号重建成空白「幽灵人物」。
        homed: dict[str, str] = {}
        for alias in list(merges):
            cur, seen = alias, set()
            while cur in merges and cur not in seen:
                seen.add(cur)
                cur = merges[cur]
            homed[alias] = cur
        for alias, canon in homed.items():
            src = people.pop(alias, None)
            if src is None:
                continue
            dst = people.setdefault(canon, blank_person(canon))
            for k in ("msgs", "private_msgs", "group_msgs", "memories", "recalls"):
                dst[k] = int(dst.get(k) or 0) + int(src.get(k) or 0)
            dst["last_ts"] = max(float(dst.get("last_ts") or 0), float(src.get("last_ts") or 0))
            for k in ("groups", "sessions"):
                for v in (src.get(k) or []):
                    if v not in dst[k]:
                        dst[k].append(v)
            dst["aliases"] = sorted(set(dst.get("aliases") or []) | set(src.get("aliases") or []) | {alias})
            if not dst.get("name"):
                dst["name"] = src.get("name") or ""
            for pf in (src.get("platforms") or []):
                if pf and pf not in dst["platforms"]:
                    dst["platforms"].append(pf)
            # 好感分：同人多个号都有记录时「取更强的那个」，而不是只认先到的那行
            if src.get("daily") is not None:
                dst["daily"] = max(float(dst.get("daily") or 0), float(src["daily"]))
            if src.get("lifetime") is not None:
                dst["lifetime"] = max(float(dst.get("lifetime") or 0), float(src["lifetime"]))
            if not dst.get("zone") and src.get("zone"):
                dst["zone"] = src.get("zone")
            for k in ("daily", "lifetime", "zone"):
                if dst.get(k) is None and src.get(k) is not None:
                    dst[k] = src.get(k)

        # 5) 昵称：号是平台号，界面只认昵称（没有昵称才回退成号，并明确标"未记昵称"）
        names = await self.person_names_map()
        for p in people.values():
            uid = str(p["user_id"])
            nm = names.get(f"user:{uid}") or p.get("name") or ""
            if not nm:
                for a in (p.get("aliases") or []):
                    nm = names.get(f"user:{a}") or ""
                    if nm:
                        break
            p["name"] = str(nm)
            p["name_known"] = bool(nm)
            p["display"] = nm or f"未记昵称 · {uid}"
        for g in groups.values():
            nm = names.get(f"group:{g['group_id']}") or g.get("name") or ""
            g["name"] = str(nm)
            g["display"] = nm or f"未记群名 · {g['group_id']}"

        # 群名（能从联系人接口拿到就用，拿不到就只显示群号）
        for g in groups.values():
            g["people"] = sum(1 for p in people.values() if g["group_id"] in p["groups"])

        # 5.5) 画像状态挂到人和群上：面板一次请求就能画「L0 人物 / L1 群聊」两栏，
        #      不必逐个主体再发一次请求。带 persona_id 时只看这个人格的那一份 —— 人格隔离。
        prof_map: dict[tuple[str, str], dict[str, Any]] = {}
        try:
            pp_where, pp_args = self._persona_where(persona_id, column="persona_id")
            for row in (await (await self.db.execute(
                "SELECT subject_kind, person_id, persona_id, profile, updated_at, mem_count, msg_count "
                "FROM hf_person_profile WHERE 1=1" + pp_where, tuple(pp_args),
            )).fetchall()):
                try:
                    body = json.loads(row["profile"] or "{}")
                except Exception:
                    body = {}
                key2 = (str(row["subject_kind"] or "user"), str(row["person_id"]))
                cur2 = prof_map.get(key2)
                if cur2 and float(cur2.get("updated_at") or 0) >= float(row["updated_at"] or 0):
                    continue
                prof_map[key2] = {
                    "has": True, "persona_id": str(row["persona_id"] or ""),
                    "one_line": str((body or {}).get("one_line") or ""),
                    "updated_at": float(row["updated_at"] or 0),
                    "mem_count": int(row["mem_count"] or 0),
                }
        except Exception:
            prof_map = {}
        # 这个主体在**别的人格**下有画像吗？（当前人格没有时面板要如实说明，不能假装"从没生成过"）
        others: dict[tuple[str, str], list[str]] = {}
        try:
            for row in (await (await self.db.execute(
                "SELECT subject_kind, person_id, persona_id FROM hf_person_profile WHERE persona_id != ''")).fetchall()):
                others.setdefault((str(row["subject_kind"] or "user"), str(row["person_id"])), []).append(
                    str(row["persona_id"]))
        except Exception:
            others = {}
        for p in people.values():
            p["profile"] = prof_map.get(("user", str(p["user_id"])))
            if not p["profile"] and others.get(("user", str(p["user_id"]))):
                p["profile_other_personas"] = sorted(set(others[("user", str(p["user_id"]))]))
        for g in groups.values():
            g["profile"] = prof_map.get(("group", str(g["group_id"])))
            if not g["profile"] and others.get(("group", str(g["group_id"]))):
                g["profile_other_personas"] = sorted(set(others[("group", str(g["group_id"]))]))

        persons = [p for p in people.values() if p["user_id"] and p["user_id"] not in bots]
        persons.sort(key=lambda x: (x.get("last_ts") or 0, x.get("msgs") or 0), reverse=True)
        glist = sorted(groups.values(), key=lambda x: (x.get("last_ts") or 0, x.get("memories") or 0), reverse=True)
        return {"items": persons[:limit], "groups": glist[:limit], "bots": sorted(bots)}

    async def favour_insight(self) -> dict[str, Any]:
        rows = await (await self.db.execute("SELECT * FROM hf_favours ORDER BY lifetime DESC")).fetchall()
        favs = [dict(r) for r in rows]
        zones: dict[str, int] = {}
        for fav in favs:
            zones[str(fav.get("zone") or "NEUTRAL")] = zones.get(str(fav.get("zone") or "NEUTRAL"), 0) + 1
        events = await (await self.db.execute(
            "SELECT * FROM hf_audit WHERE action LIKE 'favour%' ORDER BY ts DESC LIMIT 40"
        )).fetchall()
        return {
            "total": len(favs),
            "zones": zones,
            "avg_daily": round(sum(float(f.get("daily") or 0) for f in favs) / len(favs), 2) if favs else 0.0,
            "top": favs[:8],
            "events": [dict(e) for e in events],
        }

    async def habit_log(self, limit: int = 100) -> dict[str, Any]:
        """学习轨迹（面板）：表达条目 + 学习/审核事件 + 最近的运行诊断。"""
        rows = await (
            await self.db.execute(
                "SELECT * FROM hf_expr WHERE status!='archived' ORDER BY updated_at DESC LIMIT ?", (limit,)
            )
        ).fetchall()
        events = await (
            await self.db.execute(
                "SELECT * FROM hf_audit WHERE action LIKE 'habit%' OR action LIKE 'expr%' ORDER BY ts DESC LIMIT 40"
            )
        ).fetchall()
        return {
            "items": [dict(r) for r in rows],
            "events": [dict(e) for e in events],
            "runs": await self.expr_runs(20),
        }

    async def stats(self, persona_id: str = "") -> dict[str, Any]:
        counts = await self.memory_counts(persona_id=persona_id)
        fav = await (await self.db.execute("SELECT COUNT(*) FROM hf_favours")).fetchone()
        habit = await (await self.db.execute("SELECT COUNT(*) FROM hf_expr WHERE status!='archived'")).fetchone()
        gate = await (await self.db.execute("SELECT COUNT(*) FROM hf_gate_log")).fetchone()
        edges = await (await self.db.execute("SELECT COUNT(*) FROM hf_edges WHERE status='active'")).fetchone()
        ents = await (await self.db.execute("SELECT COUNT(*) FROM hf_entities")).fetchone()
        trig = await (
            await self.db.execute("SELECT COUNT(*) FROM hf_gate_log WHERE decision='reply'")
        ).fetchone()
        gate_total = gate[0] or 0
        return {
            "memories": counts["total"],
            "memory_by_type": counts["by_type"],
            "memory_by_source": counts["by_source"],
            "memory_avg_strength": counts["avg_strength"],
            "entities": ents[0],
            "edges": edges[0],
            "favours": fav[0],
            "habits": habit[0],
            "gate_total": gate_total,
            "gate_reply": trig[0],
            "gate_rate": round(trig[0] / gate_total, 3) if gate_total else 0.0,
        }


    # ===================== 会话消息 / 原文保留 =====================
    # 设计取自 livingmemory：会话消息是"记忆条目"的上游，
    # 详情要能回答"这条记忆是从哪段对话总结出来的"，而不是只给一句摘要。

    async def find_message_by_ext_id(self, ext_id: str) -> dict[str, Any] | None:
        """按 ext_id 反查一条消息（撤回取证用：重启/环形缓冲失效后从库里兜底）。"""
        if not ext_id:
            return None
        try:
            row = await (await self.db.execute(
                "SELECT * FROM hf_messages WHERE ext_id=? LIMIT 1", (str(ext_id),)
            )).fetchone()
        except Exception:                                      # noqa: BLE001
            return None
        if not row:
            return None
        d = {k: row[k] for k in row.keys()}
        name = str(d.get("name") or d.get("sender_name") or d.get("user_id") or "")
        return {
            "mid": str(d.get("ext_id") or "").split(":", 1)[-1],
            "text": str(d.get("text") or d.get("content") or ""),
            "sender_name": name,
            "session_id": str(d.get("session_id") or ""),
            "ts": float(d.get("ts") or 0),
            "imgs": [],
            "from_db": True,
        }

    async def purge_live_messages(self, keep_days: int = 30) -> int:
        """直播留档的保留期清理：只清 qq:<mid>（导入的 lmmsg: 不属于本次留档）。

        不清理会无限增长：hf_messages 每条被观察到的消息都写一行正文，长期跑 = 磁盘 + 隐私双风险。
        """
        days = max(1, int(keep_days or 30))
        cutoff = now() - days * 86400.0
        try:
            cur = await self.db.execute(
                "DELETE FROM hf_messages WHERE ext_id LIKE 'qq:%' AND ts < ?", (cutoff,)
            )
            await self.db.commit()
            return int(cur.rowcount or 0)
        except Exception as exc:                               # noqa: BLE001
            logger.warning(f"[StarHeart] 直播留档清理失败：{exc}")
            return 0

    async def add_messages(self, rows: list[dict[str, Any]]) -> int:
        """批量写会话消息。ext_id 唯一 → 重复导入幂等，返回新增条数。"""
        if not rows:
            return 0
        before = (await (await self._db.execute("SELECT COUNT(*) FROM hf_messages")).fetchone())[0]
        await self._db.executemany(
            """INSERT OR IGNORE INTO hf_messages
               (session_id, role, content, sender_id, sender_name, platform, ts, is_bot, ext_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (
                    str(r.get("session_id") or ""),
                    str(r.get("role") or ""),
                    str(r.get("content") or ""),
                    str(r.get("sender_id") or ""),
                    str(r.get("sender_name") or ""),
                    str(r.get("platform") or ""),
                    float(r.get("ts") or 0),
                    1 if r.get("is_bot") else 0,
                    str(r.get("ext_id")) if r.get("ext_id") is not None else None,
                )
                for r in rows
            ],
        )
        await self._db.commit()
        after = (await (await self._db.execute("SELECT COUNT(*) FROM hf_messages")).fetchone())[0]
        return int(after - before)

    async def msg_stats(self) -> dict[str, Any]:
        total = (await (await self._db.execute("SELECT COUNT(*) FROM hf_messages")).fetchone())[0]
        sess = (await (await self._db.execute("SELECT COUNT(DISTINCT session_id) FROM hf_messages")).fetchone())[0]
        chars = (await (await self._db.execute("SELECT COALESCE(SUM(length(content)),0) FROM hf_messages")).fetchone())[0]
        linked = (await (await self._db.execute("SELECT COUNT(*) FROM hf_memory_src")).fetchone())[0]
        return {"messages": total, "sessions": sess, "chars": chars, "linked_turns": linked}

    async def messages_for_session(self, session_id: str, limit: int = 200, offset: int = 0, q: str = "") -> dict[str, Any]:
        """某会话的消息列表（记忆库管"对话"子视图；q 走 LIKE 关键词）。"""
        where = ["session_id=?"]
        args: list[Any] = [session_id]
        if q.strip():
            where.append("content LIKE ?")
            args.append("%" + q.strip() + "%")
        w = " AND ".join(where)
        total = (await (await self._db.execute(f"SELECT COUNT(*) FROM hf_messages WHERE {w}", args)).fetchone())[0]
        rows = await (
            await self._db.execute(
                f"SELECT * FROM hf_messages WHERE {w} ORDER BY ts ASC LIMIT ? OFFSET ?", [*args, limit, offset]
            )
        ).fetchall()
        return {"items": [dict(r) for r in rows], "total": int(total)}

    async def messages_around(self, session_id: str, ts: float, before: int = 6, after: int = 6) -> list[dict[str, Any]]:
        """某条记忆发生时刻前后的真实对话（详情页"原始对话"取这段）。"""
        pre = await (
            await self._db.execute(
                "SELECT * FROM hf_messages WHERE session_id=? AND ts<=? ORDER BY ts DESC LIMIT ?",
                (session_id, float(ts or 0), before),
            )
        ).fetchall()
        post = await (
            await self._db.execute(
                "SELECT * FROM hf_messages WHERE session_id=? AND ts>? ORDER BY ts ASC LIMIT ?",
                (session_id, float(ts or 0), after),
            )
        ).fetchall()
        return [dict(r) for r in list(reversed(pre)) + list(post)]

    async def save_memory_sources(self, mid: Any, rows: list[dict[str, Any]]) -> int:
        """写"这条记忆精确学自哪几句"（来自 livingmemory 的 memory_sources.source_json）。"""
        if not rows:
            return 0
        await self._db.execute("DELETE FROM hf_memory_src WHERE memory_id=?", (int(mid),))
        await self._db.executemany(
            "INSERT OR REPLACE INTO hf_memory_src (memory_id, seq, role, speaker, content, ts) VALUES (?, ?, ?, ?, ?, ?)",
            [
                (int(mid), i, str(r.get("role") or ""), str(r.get("speaker") or ""), str(r.get("content") or ""), float(r.get("ts") or 0))
                for i, r in enumerate(rows)
            ],
        )
        await self._db.commit()
        return len(rows)

    async def memory_sources(self, mid: Any) -> list[dict[str, Any]]:
        rows = await (await self._db.execute("SELECT * FROM hf_memory_src WHERE memory_id=? ORDER BY seq", (int(mid),))).fetchall()
        return [dict(r) for r in rows]

    async def sources_coverage(self) -> dict[str, Any]:
        n = (await (await self._db.execute("SELECT COUNT(DISTINCT memory_id) FROM hf_memory_src")).fetchone())[0]
        return {"memories_with_sources": int(n)}

    async def set_source_text(self, mid: Any, text: str) -> None:
        await self._db.execute("UPDATE hf_memories SET source_text=? WHERE id=?", (str(text or ""), int(mid)))
        await self._db.commit()

    async def external_id_map(self) -> dict[str, int]:
        """external_id → 本地记忆 id（导入映射用；livingmemory 的 external_id 形如 doc:<uuid>）。"""
        rows = await (await self._db.execute("SELECT id, external_id FROM hf_memories WHERE COALESCE(external_id,'')!=''")).fetchall()
        return {str(r["external_id"]): int(r["id"]) for r in rows}

    # ===================== 记忆库管理表：搜索 / 筛选 / 排序 / 分页 =====================

    async def active_gen_cached(self) -> str:
        """当前使用的向量库代（内存缓存 30 秒，避免列表查询每条都去读配置）。"""
        import time as _t
        at = getattr(self, "_gen_cache_at", 0.0)
        if _t.time() - at > 30:
            self._gen_cache = await self.active_gen()
            self._gen_cache_at = _t.time()
        return str(getattr(self, "_gen_cache", "") or "")

    async def search_memories(
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
        """管理表用。关键词覆盖 摘要/内容/原文/证据/线索（就是 livingmemory 的"关键字（支持 memory_id/内容搜索）"）。"""
        where: list[str] = ["1=1"]
        args: list[Any] = []
        if q.strip():
            kw = q.strip()
            like = "%" + kw + "%"
            # 关键词宽匹配照旧（摘要/内容/原文/证据/线索/实体/会话），另外单独认一次「记忆 ID」：
            # 面板摘要列显示的是 m{id}，用户会把整串粘进来，而 id 是 INTEGER —— 老写法
            # CAST(id AS TEXT)='m865' 永不成立，所以搜 ID 一条也搜不到。剥掉前缀后按数字精确匹配，
            # 用 OR 挂上去只可能多命中，不会把原来的关键词结果挤掉。
            conds = [
                "summary LIKE ?", "content LIKE ?", "source_text LIKE ?", "evidence LIKE ?",
                "cues LIKE ?", "entities LIKE ?", "session_id LIKE ?",
            ]
            args += [like] * len(conds)
            mid = _query_memory_id(kw)
            if mid is not None:
                conds.append("id=?")
                args.append(mid)
            where.append("(" + " OR ".join(conds) + ")")
        if session_id:
            where.append("session_id=?")
            args.append(session_id)
        if persona_id:
            where.append("persona_id=?")
            args.append(persona_id)
        if status:
            where.append("status=?")
            args.append(status)
        else:
            # 不指定状态 = 看所有「未软删」的（active/decayed/archived/stale）。
            # 以前这里不加任何过滤，于是执行「清理非活跃记忆」（打 status='deleted'）
            # 之后，记忆库表格与总数仍然列着那些已清理的行 —— 看起来像按钮没生效。
            # 想专门看已清理的，显式传 status=deleted 即可。
            where.append("status!='deleted'")
        if atom_type:
            where.append("atom_type=?")
            args.append(atom_type)
        if source == "imported":
            where.append("COALESCE(external_source,'')!=''")
        elif source == "local":
            where.append("COALESCE(external_source,'')=''")
        w = " AND ".join(where)
        order = {
            "recent": "COALESCE(occurred_at, created_at) DESC",
            "old": "COALESCE(occurred_at, created_at) ASC",
            "strong": "strength DESC",
            "weak": "strength ASC",
            "recalls": "recalls DESC",
            "poignancy": "poignancy DESC",
        }.get(sort, "COALESCE(occurred_at, created_at) DESC")
        gen = await self.active_gen_cached()
        total = (await (await self._db.execute(f"SELECT COUNT(*) FROM hf_memories WHERE {w}", args)).fetchone())[0]
        rows = await (
            await self._db.execute(
                f"""SELECT id, summary, content, source_text, atom_type, strength, poignancy, recalls,
                           session_id, persona_id, cues, entities, occurred_at, created_at, status,
                           external_source, external_id,
                           CASE WHEN EXISTS (SELECT 1 FROM hf_vectors v WHERE v.memory_id=hf_memories.id AND v.gen_key=?)
                                THEN 1 ELSE 0 END AS has_vec
                    FROM hf_memories WHERE {w} ORDER BY {order} LIMIT ? OFFSET ?""",
                [gen, *args, limit, offset],
            )
        ).fetchall()
        return {"items": [dict(r) for r in rows], "total": int(total), "limit": limit, "offset": offset, "gen": gen}

    async def memory_personas(self) -> list[dict[str, Any]]:
        """人格清单（记忆库按人格分类用）。"""
        rows = await (
            await self._db.execute(
                "SELECT persona_id, COUNT(*) AS memories,"
                " COALESCE(SUM(CASE WHEN embedding IS NULL THEN 0 ELSE 1 END),0) AS with_vec,"
                " MAX(COALESCE(occurred_at, created_at)) AS last_ts"
                " FROM hf_memories GROUP BY persona_id ORDER BY memories DESC"
            )
        ).fetchall()
        return [dict(r) for r in rows]

    async def memory_sessions(self) -> list[dict[str, Any]]:
        """记忆库顶部"会话"下拉的数据源（记忆数 + 消息数一起给，方便看出哪个会话有原文）。"""
        rows = await (
            await self._db.execute(
                """SELECT session_id, COUNT(*) AS memories,
                          COALESCE(SUM(CASE WHEN embedding IS NULL THEN 0 ELSE 1 END),0) AS with_vec,
                          MIN(COALESCE(occurred_at, created_at)) AS first_ts,
                          MAX(COALESCE(occurred_at, created_at)) AS last_ts
                   FROM hf_memories GROUP BY session_id ORDER BY memories DESC"""
            )
        ).fetchall()
        msg = {
            r["session_id"]: r["n"]
            for r in await (await self._db.execute("SELECT session_id, COUNT(*) AS n FROM hf_messages GROUP BY session_id")).fetchall()
        }
        out = []
        for r in rows:
            d = dict(r)
            d["messages"] = int(msg.get(d["session_id"], 0))
            out.append(d)
        return out

    async def embedding_coverage(self) -> dict[str, Any]:
        """向量覆盖：全部记忆都要算（旧写法只看 active，导致面板显示"47/47 全覆盖"、用户看到的却是 853 条没向量）。"""
        rows = await (
            await self._db.execute(
                """SELECT status,
                          COUNT(*) AS n,
                          COALESCE(SUM(CASE WHEN embedding IS NULL THEN 0 ELSE 1 END),0) AS vec
                   FROM hf_memories WHERE status!='archived' OR 1=1 GROUP BY status"""
            )
        ).fetchall()
        by_status = {r["status"]: {"total": int(r["n"]), "embedded": int(r["vec"]), "missing": int(r["n"] - r["vec"])} for r in rows}
        total = sum(v["total"] for v in by_status.values())
        done = sum(v["embedded"] for v in by_status.values())
        arch_missing = int(by_status.get("archived", {}).get("missing", 0))
        return {
            "total": total, "embedded": done, "missing": total - done,
            # 缺口必须拆开说：归档的记忆按设计**永远不会补**（那是用户主动归档的、不再参与召回），
            # 混进缺口里 → 面板永远显示「还缺 N」、按钮跑到最后归不了零（实测：265 条缺口 100% 是归档）。
            "missing_recallable": total - done - arch_missing,
            "archived_missing": arch_missing,
            "by_status": by_status,
            "recallable": sum(v["total"] for k, v in by_status.items() if k != "archived"),
        }

    async def all_embeddings(self, persona_id: str = "", include_stale: bool = True) -> list[tuple[str, bytes]]:
        """召回用的向量集。默认纳入 stale/decayed（它们只是分数低，不该直接失联），只排除 archived。"""
        sql = "SELECT id, embedding FROM hf_memories WHERE embedding IS NOT NULL"
        args: list[Any] = []
        if not include_stale:
            sql += " AND status='active'"
        else:
            sql += " AND status!='archived'"
        if persona_id:
            sql += " AND persona_id=?"
            args.append(persona_id)
        rows = await (await self._db.execute(sql, args)).fetchall()
        return [(str(r["id"]), r["embedding"]) for r in rows]

    async def memories_without_embedding_any(self, limit: int = 32, persona_id: str = "") -> list[dict[str, Any]]:
        """回填向量的取数（含 stale/decayed；archived 不动，那是用户主动归档的）。"""
        sql = ("SELECT id, summary, content FROM hf_memories"
               " WHERE embedding IS NULL AND status!='archived'")
        args: list[Any] = []
        if persona_id:
            sql += " AND persona_id=?"
            args.append(persona_id)
        sql += " ORDER BY strength DESC LIMIT ?"
        args.append(limit)
        rows = await (await self._db.execute(sql, args)).fetchall()
        return [dict(r) for r in rows]

    async def reactivate(self, ids: list[int] | None = None, *, floor: float = 0.45, only_stale: bool = True,
                         include_archived: bool = False) -> int:
        """批量"复活"：把 stale/decayed 拉回 active 并给一个强度下限。

        导入的历史记忆按原日期衰减会被判死，这不该是用户的默认结局。

        ⚠️ 必须同时把**衰减基线 `strength_updated_at` 设成现在**，否则这个动作是假的：
        衰减是「按距基线的时长」算的（`decay_strength(cur, age_days(strength_updated_at))`），
        只抬强度不动基线 → 下一轮维护仍按"几个月没动过"重算，0.45 一次乘到 0、直接又判死。
        真机实测（2026-10-11）：复活 382 条 → 3 分钟后 `decayed=419 evicted=346`，白复活。
        """
        stamp = now()
        if ids:
            marks = ",".join("?" * len(ids))
            cur = await self._db.execute(
                "UPDATE hf_memories SET status='active', strength=MAX(strength, ?), "
                f"strength_updated_at=?, updated_at=? WHERE id IN ({marks})",
                [floor, stamp, stamp, *ids],
            )
        else:
            if include_archived:
                cond = "status IN ('stale','decayed','archived','forgotten')"   # 「归档被提及即拉回」的批量版
            else:
                cond = "status IN ('stale','decayed')" if only_stale else "status!='archived'"
            cur = await self._db.execute(
                "UPDATE hf_memories SET status='active', strength=MAX(strength, ?), "
                f"strength_updated_at=?, updated_at=? WHERE {cond}",
                [floor, stamp, stamp],
            )
        await self._db.commit()
        return int(cur.rowcount or 0)

    async def status_counts(self) -> dict[str, int]:
        rows = await (await self._db.execute("SELECT status, COUNT(*) AS n FROM hf_memories GROUP BY status")).fetchall()
        return {str(r["status"]): int(r["n"]) for r in rows}

    # ===================== 自动遗忘生命周期 =====================
    # 规则（用户定的）：久未被提及 → 归档；归档后再久未被提及且不重要 → 遗忘；
    # 任何时候被提及（注入/召回命中）→ 刷新计时，归档中的直接拉回正常。

    async def memory_lifecycle_scan(self, archive_days: float = 30, forget_days: float = 30) -> list[dict[str, Any]]:
        """列出两条要处理的队列（只读，供 tick 与面板预览）。"""
        nowt = now()
        arch_cut = nowt - archive_days * 86400
        forget_cut = nowt - (archive_days + forget_days) * 86400
        stale = await (
            await self._db.execute(
                """SELECT id, summary, strength, poignancy, recalls, status,
                          MAX(COALESCE(last_used_at,0), COALESCE(occurred_at,0), created_at) AS last_touch
                   FROM hf_memories
                   WHERE status='active' AND MAX(COALESCE(last_used_at,0), COALESCE(occurred_at,0), created_at) < ?
                   ORDER BY last_touch ASC LIMIT 400""",
                (arch_cut,),
            )
        ).fetchall()
        old = await (
            await self._db.execute(
                """SELECT id, summary, strength, poignancy, recalls, status,
                          MAX(COALESCE(last_used_at,0), COALESCE(occurred_at,0), created_at) AS last_touch
                   FROM hf_memories
                   WHERE status='archived' AND MAX(COALESCE(last_used_at,0), COALESCE(occurred_at,0), created_at) < ?
                   ORDER BY last_touch ASC LIMIT 400""",
                (forget_cut,),
            )
        ).fetchall()
        return [dict(r) for r in stale] + [dict(r) for r in old]

    # （旧的第二份 archive_memories 已合并到上面那份：它覆盖掉了带 factor 的版本，
    #   导致「召回即降权」传进来的 0.3 变成了 why='0.3'、强度一点没动 —— 真机审计抓到）

    async def forget_memories(self, ids: list[int], why: str = "unimportant") -> int:
        if not ids:
            return 0
        marks = ",".join("?" * len(ids))
        cur = await self._db.execute(
            f"UPDATE hf_memories SET status='forgotten', forget_stage=?, updated_at=? WHERE id IN ({marks})",
            [why, now(), *ids],
        )
        await self._db.commit()
        return int(cur.rowcount or 0)

    async def touch_memory(self, mid: Any) -> bool:
        """被提及：刷新计时；归档/遗忘中的直接拉回正常（用户要求"归档里被提及就自动拉回"）。

        加固改成「递减 + 重要度封顶」：
          ① 增量 = 0.06 × (1 - strength/cap)，越接近上限涨得越慢（旧实现每次 +0.06，
             真机 7 次就锁死 1.0，#906 被注入 62 次）；
          ② 上限 cap = 0.55 + 0.04 × poignancy（poi 3 → 0.67，poi 10 → 0.95）——
             一句话值不值得被反复提起，由写入时模型给的重要度决定，不由「被注入过几次」决定。
        注意：调用方拿到的 id 可能是 "m123" 也可能是 123，统一走 _split_mid。
        """
        sid = self._split_mid(mid)
        if sid is None:
            return False
        row = await (await self._db.execute(
            "SELECT id, status, strength, poignancy FROM hf_memories WHERE id=?", (sid,))).fetchone()
        if row is None:
            return False
        back = str(row["status"]) in ("archived", "forgotten", "stale", "decayed")
        cap = 0.55 + 0.04 * float(row["poignancy"] if row["poignancy"] is not None else 5)
        cur = float(row["strength"] or 0.0)
        if back:
            new_strength = max(cur, min(0.45, cap))
        elif cur < cap:
            new_strength = min(cap, cur + 0.06 * (1.0 - cur / cap))
        else:
            new_strength = cur                      # 已在上限之上：不硬压，交给衰减
        await self.ensure_strength_updated_at()
        await self._db.execute(
            "UPDATE hf_memories SET last_used_at=?, strength_updated_at=?, recalls=COALESCE(recalls,0)+1,"
            + (" status='active'," if back else "")
            + " strength=?, updated_at=? WHERE id=?",
            (now(), now(), new_strength, now(), sid),
        )
        await self._db.commit()
        return back

    async def set_muted(self, mid: Any, muted: bool = True, source_mode: str = "") -> bool:
        """「别再提」：muted=1 的记忆不进召回、不进主动池（仍留在库里，可随时恢复）。

        source_mode 非空时顺手改来源标签（"test" = 测试残留），方便面板筛出来；
        它只是标签，真正的开关仍是 muted。
        """
        sid = self._split_mid(mid)
        if sid is None:
            return False
        if source_mode:
            cur = await self._db.execute(
                "UPDATE hf_memories SET muted=?, source_mode=?, updated_at=? WHERE id=?",
                (1 if muted else 0, str(source_mode), now(), sid))
        else:
            cur = await self._db.execute(
                "UPDATE hf_memories SET muted=?, updated_at=? WHERE id=?",
                (1 if muted else 0, now(), sid))
        await self._db.commit()
        return bool(cur.rowcount)

    async def lifecycle_counts(self) -> dict[str, Any]:
        rows = await (
            await self._db.execute(
                """SELECT status, COUNT(*) AS n,
                          SUM(CASE WHEN MAX(COALESCE(last_used_at,0), COALESCE(occurred_at,0), created_at) < ? THEN 1 ELSE 0 END) AS idle_arch
                   FROM hf_memories GROUP BY status""",
                (now() - 30 * 86400,),
            )
        ).fetchall()
        return {str(r["status"]): {"total": int(r["n"]), "idle_30d": int(r["idle_arch"] or 0)} for r in rows}

    # ===================== 多代向量库 =====================
    # 一个记忆在不同代里各存一份向量；召回只读"当前代"，所以永远不会混维度。
    # 换模型 = 新建一代（旧的保留、暂停使用），构建完成再切换；换回来立刻可用。

    @staticmethod
    def norm_model(model: str) -> str:
        """模型名规范化：BAAI/bge-m3 和 bge-m3 是同一个模型，不该分成两代向量库。"""
        m = str(model or "").strip().lower()
        if "/" in m:
            m = m.rsplit("/", 1)[-1]
        return m or "unknown"

    @classmethod
    def gen_key(cls, model: str, dim: int) -> str:
        return "%s|%d" % (cls.norm_model(model), int(dim or 0))

    async def normalize_vector_gens(self) -> int:
        """把 hf_vectors 里因模型名写法不同而被拆开的代合并（同模型同维度 = 同一代）。幂等。"""
        rows = await (
            await self._db.execute("SELECT memory_id, gen_key, model, dim, vec, created_at FROM hf_vectors")
        ).fetchall()
        if not rows:
            return 0
        need = any(str(r["gen_key"]) != self.gen_key(r["model"], r["dim"]) for r in rows)
        if not need:
            return 0
        best: dict[tuple[int, str], dict] = {}
        for r in rows:
            k = (int(r["memory_id"]), self.gen_key(r["model"], r["dim"]))
            if k not in best or float(r["created_at"] or 0) > float(best[k]["created_at"] or 0):
                best[k] = (int(r["memory_id"]), self.gen_key(r["model"], r["dim"]),
                           self.norm_model(r["model"]), int(r["dim"] or 0), r["vec"], float(r["created_at"] or 0))
        await self._db.execute("DELETE FROM hf_vectors")
        await self._db.executemany(
            "INSERT OR REPLACE INTO hf_vectors (memory_id, gen_key, model, dim, vec, created_at) VALUES (?,?,?,?,?,?)",
            list(best.values()),
        )
        await self._db.commit()
        return len(rows) - len(best)

    async def normalize_active_gen(self) -> None:
        """把配置里存的当前代号也规范化（BAAI/bge-m3|1024 → bge-m3|1024）。"""
        v = await self.get_config("vector_active_gen")
        v = str(v or "")
        if not v or "|" not in v:
            return
        m, d = v.split("|", 1)
        try:
            nk = self.gen_key(m, int(d))
        except ValueError:
            nk = self.norm_model(m) + "|0"
        if nk != v:
            await self.set_config("vector_active_gen", nk)

    async def ensure_vector_migration(self) -> dict[str, Any]:
        """把旧列 embedding 里的存量向量搬进 hf_vectors（按 embedding_model + BLOB 长度推维度）。幂等。"""
        await self.normalize_active_gen()
        await self.normalize_vector_gens()          # 副作用：合并因模型名写法不同被拆开的代
        exists = (await (await self._db.execute("SELECT COUNT(*) FROM hf_vectors")).fetchone())[0]
        if exists:
            return {"migrated": 0, "skipped": True}
        rows = await (
            await self._db.execute(
                "SELECT id, embedding, COALESCE(embedding_model,'') AS model FROM hf_memories WHERE embedding IS NOT NULL"
            )
        ).fetchall()
        n = 0
        for r in rows:
            blob = r["embedding"]
            if not blob:
                continue
            dim = len(blob) // 4
            model = str(r["model"] or "").strip() or "bge-m3"
            await self._db.execute(
                "INSERT OR REPLACE INTO hf_vectors (memory_id, gen_key, model, dim, vec, created_at) VALUES (?,?,?,?,?,?)",
                (int(r["id"]), self.gen_key(model, dim), model, dim, blob, now()),
            )
            n += 1
        await self._db.commit()
        if n:
            await self.set_config("vector_active_gen", self.gen_key(str(rows[0]["model"] or "bge-m3"), len(rows[0]["embedding"]) // 4))
        return {"migrated": n, "skipped": False}

    async def active_gen(self, fallback_model: str = "") -> str:
        v = await self.get_config("vector_active_gen")
        if v:
            return str(v)
        if fallback_model:
            return self.gen_key(fallback_model, 0)     # 0 维表示"尚未确定维度"
        return ""

    async def set_active_gen(self, gen_key: str) -> None:
        await self.set_config("vector_active_gen", str(gen_key or ""))

    async def gen_stats(self) -> list[dict[str, Any]]:
        """每一代：模型 / 维度 / 已覆盖多少条 / 总记忆数 / 是否当前在用。"""
        active = await self.active_gen()
        total = (await (await self._db.execute("SELECT COUNT(*) FROM hf_memories WHERE status!='deleted'")).fetchone())[0]
        rows = await (
            await self._db.execute(
                "SELECT gen_key, model, dim, COUNT(*) AS n, MAX(created_at) AS last_at FROM hf_vectors GROUP BY gen_key ORDER BY last_at DESC"
            )
        ).fetchall()
        out = []
        for r in rows:
            out.append({
                "gen_key": r["gen_key"], "model": r["model"], "dim": int(r["dim"] or 0),
                "covered": int(r["n"]), "total": int(total),
                "coverage": round(int(r["n"]) / total, 4) if total else 0.0,
                "active": str(r["gen_key"]) == str(active), "last_at": float(r["last_at"] or 0),
            })
        if not out and total:
            out.append({"gen_key": "", "model": "", "dim": 0, "covered": 0, "total": int(total),
                        "coverage": 0.0, "active": True, "last_at": 0})
        return out

    async def set_embedding_gen(self, mid: Any, vector: list[float], model: str, gen_key: str = "") -> None:
        """写入指定代（默认当前代）。旧代不受影响 —— 这就是"换模型不删旧向量"的关键。"""
        import array
        blob = array.array("f", [float(x) for x in vector]).tobytes()
        dim = len(vector)
        gk = gen_key or self.gen_key(model, dim)
        await self._db.execute(
            "INSERT OR REPLACE INTO hf_vectors (memory_id, gen_key, model, dim, vec, created_at) VALUES (?,?,?,?,?,?)",
            (int(str(mid).lstrip("m")), gk, str(model or ""), dim, blob, now()),
        )
        # 兼容旧列：当前代的向量同步一份，老代码/导出还能用
        if gk == await self.active_gen():
            await self._db.execute(
                "UPDATE hf_memories SET embedding=?, embedding_model=?, updated_at=? WHERE id=?",
                (blob, str(model or ""), now(), int(str(mid).lstrip("m"))),
            )
        await self._db.commit()

    async def all_embeddings_gen(self, gen_key: str, persona_id: str = "", include_stale: bool = True,
                                 *, include_archived: bool = False) -> list[tuple[str, bytes]]:
        """读取指定代的向量（同一维度，天然不会混代）。

        include_archived 只给注入召回用：归档记忆被提及要能拉回，所以聊天注入要带上；
        面板召回保持原口径（默认不带归档）。
        """
        sql = ("SELECT v.memory_id AS id, v.vec AS vec FROM hf_vectors v "
               "JOIN hf_memories m ON m.id = v.memory_id "
               "WHERE v.gen_key=? AND COALESCE(m.muted,0)=0")
        args: list[Any] = [str(gen_key)]
        if include_archived:
            sql += " AND m.status NOT IN ('forgotten','deleted')"
        elif include_stale:
            sql += " AND m.status NOT IN ('archived','forgotten','deleted')"
        else:
            sql += " AND m.status='active'"
        if persona_id:
            sql += " AND m.persona_id=?"
            args.append(persona_id)
        rows = await (await self._db.execute(sql, args)).fetchall()
        return [(str(r["id"]), r["vec"]) for r in rows if r["vec"]]

    async def memories_missing_gen(self, gen_key: str, limit: int = 32, persona_id: str = "") -> list[dict[str, Any]]:
        """某代还缺哪些记忆（回填取数；不含已归档/遗忘）。"""
        sql = ("SELECT m.id, m.summary, m.content FROM hf_memories m"
               " WHERE m.status NOT IN ('archived','forgotten','deleted')"
               " AND NOT EXISTS (SELECT 1 FROM hf_vectors v WHERE v.memory_id=m.id AND v.gen_key=?)")
        args: list[Any] = [str(gen_key)]
        if persona_id:
            sql += " AND m.persona_id=?"
            args.append(persona_id)
        sql += " ORDER BY m.strength DESC LIMIT ?"
        args.append(limit)
        rows = await (await self._db.execute(sql, args)).fetchall()
        return [dict(r) for r in rows]

    async def gen_missing_count(self, gen_key: str, persona_id: str = "") -> int:
        """某代还缺多少条可召回记忆（0 = 这一代已经建完，可以切过去）。"""
        sql = ("SELECT COUNT(*) FROM hf_memories m"
               " WHERE m.status NOT IN ('archived','forgotten','deleted')"
               " AND NOT EXISTS (SELECT 1 FROM hf_vectors v WHERE v.memory_id=m.id AND v.gen_key=?)")
        args: list[Any] = [str(gen_key)]
        if persona_id:
            sql += " AND m.persona_id=?"
            args.append(persona_id)
        row = await (await self._db.execute(sql, args)).fetchone()
        return int(row[0] or 0) if row else 0

    async def drop_gen(self, gen_key: str) -> int:
        cur = await self._db.execute("DELETE FROM hf_vectors WHERE gen_key=?", (str(gen_key),))
        await self._db.commit()
        return int(cur.rowcount or 0)

    # ---------------- 语言学习：原话样本 + 词表（MVP） ----------------

    async def save_style_sample(self, rec: dict[str, Any]) -> int | None:
        """写一条表达样本；同 (persona, kind, norm_key) 幂等更新，不回退 status。"""
        persona_id = str(rec.get("persona_id") or "")
        kind = str(rec.get("kind") or "bot")
        trigger = str(rec.get("trigger_text") or "").strip()
        reply = str(rec.get("reply_text") or "").strip()
        if not reply:
            return None
        from .style_canon import norm_key, structure

        key = str(rec.get("norm_key") or norm_key(trigger, reply))
        ts = now()
        try:
            await self._db.execute(
                "INSERT INTO hf_style_samples (persona_id, kind, register, trigger_text, reply_text,"
                " structure_json, norm_key, source_ids, sensitivity, status, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,'active',?,?)"
                " ON CONFLICT(persona_id, kind, norm_key) DO UPDATE SET"
                "  source_ids=excluded.source_ids, updated_at=excluded.updated_at",
                (
                    persona_id, kind, str(rec.get("register") or "private"), trigger, reply,
                    json.dumps(rec.get("structure") or structure(reply), ensure_ascii=False),
                    key, json.dumps(rec.get("source_ids") or [], ensure_ascii=False),
                    str(rec.get("sensitivity") or "normal"), ts, ts,
                ),
            )
            await self._db.commit()
        except Exception as exc:                                # noqa: BLE001
            logger.debug(f"[StarHeart] 表达样本写入跳过：{exc}")
            return None
        row = await (await self._db.execute(
            "SELECT id FROM hf_style_samples WHERE persona_id=? AND kind=? AND norm_key=?",
            (persona_id, kind, key),
        )).fetchone()
        return int(row["id"]) if row else None

    async def pick_style_samples(self, persona_id: str, kind: str, register: str,
                                 limit: int = 2, *, cooldown: float = 7200.0) -> list[dict[str, Any]]:
        """取 1-2 条可注入样本：太久没投的优先，冷却期内不投。"""
        ts = now()
        rows = await (await self._db.execute(
            "SELECT * FROM hf_style_samples WHERE persona_id=? AND kind=? AND status='active'"
            " AND sensitivity='normal' AND (register=? OR ?='')"
            " AND (last_shown_at=0 OR last_shown_at < ?)"
            " ORDER BY last_shown_at ASC, id DESC LIMIT ?",
            (str(persona_id), str(kind), str(register), str(register), ts - float(cooldown),
             max(1, int(limit))),
        )).fetchall()
        return [dict(r) for r in rows]

    async def touch_style_sample(self, sid: int) -> None:
        try:
            await self._db.execute(
                "UPDATE hf_style_samples SET shown=shown+1, last_shown_at=?, updated_at=? WHERE id=?",
                (now(), now(), int(sid)),
            )
            await self._db.commit()
        except Exception:                                       # noqa: BLE001
            pass

    async def list_style_samples(self, persona_id: str = "", kind: str = "",
                                 status: str = "active", limit: int = 50) -> list[dict[str, Any]]:
        where = ["1=1"]
        args: list[Any] = []
        if persona_id:
            where.append("persona_id=?")
            args.append(str(persona_id))
        if kind:
            where.append("kind=?")
            args.append(str(kind))
        if status:
            where.append("status=?")
            args.append(str(status))
        rows = await (await self._db.execute(
            "SELECT * FROM hf_style_samples WHERE " + " AND ".join(where)
            + " ORDER BY id DESC LIMIT ?", args + [max(1, min(limit, 500))],
        )).fetchall()
        return [dict(r) for r in rows]

    async def save_style_term(self, rec: dict[str, Any]) -> int | None:
        """写词条（口癖/黑话/称呼）；同 (persona, term_norm, scope) 幂等。"""
        from .style_canon import norm

        term = str(rec.get("term") or "").strip()
        tnorm = str(rec.get("term_norm") or norm(term))
        if not term or not tnorm:
            return None
        ts = now()
        try:
            await self._db.execute(
                "INSERT INTO hf_style_terms (persona_id, term, term_norm, kind, meaning, usage,"
                " scope, enabled, status, evidence_json, source_ids, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,1,?,?,?,?,?)"
                " ON CONFLICT(persona_id, term_norm, scope) DO UPDATE SET"
                "  meaning=CASE WHEN excluded.meaning<>'' THEN excluded.meaning ELSE hf_style_terms.meaning END,"
                "  usage=CASE WHEN excluded.usage<>'' THEN excluded.usage ELSE hf_style_terms.usage END,"
                "  updated_at=excluded.updated_at",
                (
                    str(rec.get("persona_id") or ""), term, tnorm,
                    str(rec.get("kind") or "tic"), str(rec.get("meaning") or ""),
                    str(rec.get("usage") or ""), str(rec.get("scope") or "全局"),
                    str(rec.get("status") or "confirmed"),
                    json.dumps(rec.get("evidence") or [], ensure_ascii=False),
                    json.dumps(rec.get("source_ids") or [], ensure_ascii=False),
                    ts, ts,
                ),
            )
            await self._db.commit()
        except Exception as exc:                                # noqa: BLE001
            logger.debug(f"[StarHeart] 词条写入跳过：{exc}")
            return None
        row = await (await self._db.execute(
            "SELECT id FROM hf_style_terms WHERE persona_id=? AND term_norm=? AND scope=?",
            (str(rec.get("persona_id") or ""), tnorm, str(rec.get("scope") or "全局")),
        )).fetchone()
        return int(row["id"]) if row else None

    async def list_style_terms(self, persona_id: str = "", scope: str = "",
                               limit: int = 50) -> list[dict[str, Any]]:
        where = ["enabled=1", "status!='denied'"]
        args: list[Any] = []
        if persona_id:
            where.append("(persona_id=? OR persona_id='')")
            args.append(str(persona_id))
        if scope:
            where.append("(scope=? OR scope='全局')")
            args.append(str(scope))
        rows = await (await self._db.execute(
            "SELECT * FROM hf_style_terms WHERE " + " AND ".join(where)
            + " ORDER BY hits DESC, id DESC LIMIT ?", args + [max(1, min(limit, 200))],
        )).fetchall()
        return [dict(r) for r in rows]

    async def style_counts(self, persona_id: str = "") -> dict[str, Any]:
        out: dict[str, Any] = {}
        for label, sql in (
            ("bot", "SELECT COUNT(*) c FROM hf_style_samples WHERE kind='bot' AND status='active'"),
            ("human", "SELECT COUNT(*) c FROM hf_style_samples WHERE kind='human' AND status='active'"),
            ("terms", "SELECT COUNT(*) c FROM hf_style_terms WHERE enabled=1 AND status!='denied'"),
        ):
            try:
                row = await (await self._db.execute(sql)).fetchone()
                out[label] = int(row["c"] if row else 0)
            except Exception:                                   # noqa: BLE001
                out[label] = -1
        return out

    async def hide_style_sample(self, sid: int) -> int:
        """面板「×」：不再用这条（软删，可查不可注入）。"""
        cur = await self._db.execute(
            "UPDATE hf_style_samples SET status='archived', updated_at=? WHERE id=?",
            (now(), int(sid)))
        await self._db.commit()
        return int(cur.rowcount or 0)

    # ---------------- 语言学习 v2：记忆 → 回复方式 / 原话 ----------------
    async def list_memories_with_evidence(self, since_id: int = 0, limit: int = 40,
                                          persona_id: str = "") -> list[dict[str, Any]]:
        """按 id 升序取带原对话的记忆（学习输入）。evidence 里是 `用户：… / 助手：…`。"""
        where = ["id > ?", "evidence <> ''", "status != 'deleted'"]
        args: list[Any] = [int(since_id)]
        if persona_id:
            where.append("persona_id=?")
            args.append(str(persona_id))
        rows = await (await self._db.execute(
            "SELECT id, session_id, persona_id, subject_id, entities, evidence, occurred_at, created_at"
            " FROM hf_memories WHERE " + " AND ".join(where) + " ORDER BY id DESC LIMIT ?",
            args + [max(1, min(int(limit), 200))],
        )).fetchall()
        return [dict(r) for r in rows]

    async def list_memories_before(self, before_id: int = 0, limit: int = 12,
                                   persona_id: str = "") -> list[dict[str, Any]]:
        """按 id 倒序取 before_id 之前带原对话的记忆（往前回填学习）。"""
        where = ["id < ?", "evidence <> ''", "status != 'deleted'"]
        args: list[Any] = [int(before_id or 0)]
        if persona_id:
            where.append("persona_id=?")
            args.append(str(persona_id))
        rows = await (await self._db.execute(
            "SELECT id, session_id, persona_id, subject_id, entities, evidence, occurred_at, created_at"
            " FROM hf_memories WHERE " + " AND ".join(where) + " ORDER BY id DESC LIMIT ?",
            args + [max(1, min(int(limit), 200))],
        )).fetchall()
        return [dict(r) for r in rows]

    async def max_memory_id(self) -> int:
        row = await (await self._db.execute(
            "SELECT MAX(id) AS m FROM hf_memories WHERE evidence <> '' AND status != 'deleted'"
        )).fetchone()
        return int(row["m"] or 0) if row else 0

    async def upsert_style(self, persona_id: str, name: str) -> int:
        name = str(name or "").strip()[:24]
        if not name:
            return 0
        ts = now()
        await self._db.execute(
            "INSERT INTO hf_style_styles (persona_id, name, status, created_at, updated_at)"
            " VALUES (?,?,'active',?,?)"
            " ON CONFLICT(persona_id, name) DO UPDATE SET updated_at=excluded.updated_at",
            (str(persona_id or ""), name, ts, ts),
        )
        await self._db.commit()
        row = await (await self._db.execute(
            "SELECT id FROM hf_style_styles WHERE persona_id=? AND name=?",
            (str(persona_id or ""), name),
        )).fetchone()
        return int(row["id"]) if row else 0

    async def upsert_quote(self, rec: dict[str, Any]) -> int | None:
        """写一条原话；同 (memory_id, quote) 幂等，重复学习不会重复入库。"""
        quote = str(rec.get("quote") or "").strip()[:300]
        if not quote:
            return None
        ts = now()
        cur = await self._db.execute(
            "INSERT OR IGNORE INTO hf_style_quotes (style_id, persona_id, quote, trigger_text,"
            " memory_id, session_id, user_id, user_name, register, hits, corrections, status,"
            " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                int(rec.get("style_id") or 0), str(rec.get("persona_id") or ""), quote,
                str(rec.get("trigger_text") or "")[:200], int(rec.get("memory_id") or 0),
                str(rec.get("session_id") or ""), str(rec.get("user_id") or ""),
                str(rec.get("user_name") or ""), str(rec.get("register") or "private"),
                int(rec.get("hits") or 0), int(rec.get("corrections") or 0),
                str(rec.get("status") or "active"), ts, ts,
            ),
        )
        await self._db.commit()
        if int(cur.rowcount or 0):
            return int(cur.lastrowid or 0)
        row = await (await self._db.execute(
            "SELECT id FROM hf_style_quotes WHERE memory_id=? AND quote=?",
            (int(rec.get("memory_id") or 0), quote),
        )).fetchone()
        return int(row["id"]) if row else None

    async def bump_style(self, style_id: int, *, hits: int = 0, corrections: int = 0) -> None:
        if not style_id:
            return
        await self._db.execute(
            "UPDATE hf_style_styles SET hits=hits+?, corrections=corrections+?, updated_at=?"
            " WHERE id=?",
            (int(hits), int(corrections), now(), int(style_id)),
        )
        await self._db.commit()

    async def list_styles(self, persona_id: str = "") -> list[dict[str, Any]]:
        where, args = ["1=1"], []
        if persona_id:
            where.append("persona_id=?")
            args.append(str(persona_id))
        rows = await (await self._db.execute(
            "SELECT * FROM hf_style_styles WHERE " + " AND ".join(where)
            + " ORDER BY (hits+corrections) DESC, id DESC", args,
        )).fetchall()
        return [dict(r) for r in rows]

    async def list_quotes(self, persona_id: str = "", style_id: int = 0,
                          status: str = "") -> list[dict[str, Any]]:
        where, args = ["1=1"], []
        if persona_id:
            where.append("persona_id=?")
            args.append(str(persona_id))
        if style_id:
            where.append("style_id=?")
            args.append(int(style_id))
        if status:
            where.append("status=?")
            args.append(str(status))
        rows = await (await self._db.execute(
            "SELECT * FROM hf_style_quotes WHERE " + " AND ".join(where)
            + " ORDER BY id DESC", args,
        )).fetchall()
        return [dict(r) for r in rows]

    async def get_style_row(self, sid: int) -> dict[str, Any] | None:
        row = await (await self._db.execute("SELECT * FROM hf_style_styles WHERE id=?", (int(sid),))).fetchone()
        return dict(row) if row else None

    async def get_style_quote(self, qid: int) -> dict[str, Any] | None:
        row = await (await self._db.execute("SELECT * FROM hf_style_quotes WHERE id=?", (int(qid),))).fetchone()
        return dict(row) if row else None

    async def get_expr(self, eid: int) -> dict[str, Any] | None:
        row = await (await self._db.execute("SELECT * FROM hf_expr WHERE id=?", (int(eid),))).fetchone()
        return dict(row) if row else None

    async def message_count(self, session_id: str) -> int:
        row = await (await self._db.execute(
            "SELECT COUNT(*) AS n FROM hf_messages WHERE session_id=?", (str(session_id or ""),)
        )).fetchone()
        return int(row["n"] or 0) if row else 0

    async def set_quote_status(self, qid: int, status: str) -> int:
        cur = await self._db.execute(
            "UPDATE hf_style_quotes SET status=?, updated_at=? WHERE id=?",
            (str(status or "active"), now(), int(qid)),
        )
        await self._db.commit()
        return int(cur.rowcount or 0)

    async def bump_quote(self, qid: int, *, hits: int = 0, corrections: int = 0) -> None:
        await self._db.execute(
            "UPDATE hf_style_quotes SET hits=hits+?, corrections=corrections+?,"
            " last_used_at=?, updated_at=? WHERE id=?",
            (int(hits), int(corrections), now(), now(), int(qid)),
        )
        await self._db.commit()

    async def prune_empty_styles(self, persona_id: str = "") -> int:
        """删掉没有任何原话的回复方式（学习中途失败会留下空壳）。"""
        where = " AND persona_id=?" if persona_id else ""
        args = (str(persona_id),) if persona_id else ()
        cur = await self._db.execute(
            "DELETE FROM hf_style_styles WHERE NOT EXISTS"
            " (SELECT 1 FROM hf_style_quotes q WHERE q.style_id = hf_style_styles.id)"
            + where, args,
        )
        await self._db.commit()
        return int(cur.rowcount or 0)

    async def set_style_status(self, sid: int, status: str) -> int:
        status = "paused" if str(status or "") == "paused" else "active"
        cur = await self._db.execute(
            "UPDATE hf_style_styles SET status=?, updated_at=? WHERE id=?",
            (status, now(), int(sid)),
        )
        await self._db.commit()
        return int(cur.rowcount or 0)

    async def delete_style(self, sid: int) -> int:
        """删掉一种回复方式，连同它的原话一起删（硬删，不可恢复）。"""
        await self._db.execute("DELETE FROM hf_style_quotes WHERE style_id=?", (int(sid),))
        cur = await self._db.execute("DELETE FROM hf_style_styles WHERE id=?", (int(sid),))
        await self._db.commit()
        return int(cur.rowcount or 0)

    async def update_quote(self, qid: int, quote: str) -> int:
        quote = str(quote or "").strip()[:300]
        if not quote:
            return 0
        cur = await self._db.execute(
            "UPDATE hf_style_quotes SET quote=?, updated_at=? WHERE id=?",
            (quote, now(), int(qid)),
        )
        await self._db.commit()
        return int(cur.rowcount or 0)

    async def delete_quote(self, qid: int) -> int:
        cur = await self._db.execute("DELETE FROM hf_style_quotes WHERE id=?", (int(qid),))
        await self._db.commit()
        return int(cur.rowcount or 0)

    async def update_style_term(self, tid: int, *, term: str | None = None,
                                meaning: str | None = None, usage: str | None = None,
                                scope: str | None = None, enabled: bool | None = None) -> int:
        sets, args = ["updated_at=?"], [now()]
        if term is not None:
            t = str(term or "").strip()[:24]
            if not t:
                return 0
            sets += ["term=?", "term_norm=?"]
            args += [t, t.lower()]
        if meaning is not None:
            sets.append("meaning=?")
            args.append(str(meaning or "").strip()[:200])
        if usage is not None:
            sets.append("usage=?")
            args.append(str(usage or "").strip()[:200])
        if scope is not None:
            sets.append("scope=?")
            args.append(str(scope or "全局")[:24])
        if enabled is not None:
            sets.append("enabled=?")
            args.append(1 if enabled else 0)
        if len(sets) == 1:
            return 0
        args.append(int(tid))
        cur = await self._db.execute(
            "UPDATE hf_style_terms SET " + ", ".join(sets) + " WHERE id=?", args,
        )
        await self._db.commit()
        return int(cur.rowcount or 0)

    async def delete_style_term(self, tid: int) -> int:
        cur = await self._db.execute("DELETE FROM hf_style_terms WHERE id=?", (int(tid),))
        await self._db.commit()
        return int(cur.rowcount or 0)

    async def rename_style(self, sid: int, name: str) -> int:
        name = str(name or "").strip()[:24]
        if not name:
            return 0
        cur = await self._db.execute(
            "UPDATE hf_style_styles SET name=?, updated_at=? WHERE id=?",
            (name, now(), int(sid)),
        )
        await self._db.commit()
        return int(cur.rowcount or 0)

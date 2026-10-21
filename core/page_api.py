"""StarHeart 插件页面 API：把 service 层挂到 AstrBot 的插件 Web API 上。

路由统一为 /{plugin_name}/page/...，页面侧 bridge 只需要写 "memories" 这样的相对路径。
这里只做三件事：读请求、调 service、包响应（含导入进度的 SSE）。
"""

from __future__ import annotations

import json
from typing import Any

from astrbot.api import logger
from astrbot.api.web import error_response, json_response, request, stream_response

from .service import StarHeartService

PLUGIN_NAME = "astrbot_plugin_starheart"
PAGE_PREFIX = f"/{PLUGIN_NAME}/page"

# query 里的整数取值 + 上下限收敛。
# 原实现是裸的 `request.query.get("limit", N, type=int) or N`，没有任何上限：
#   ?size=-1 / ?limit=-1 会一路传到 SQLite（`LIMIT -1` = 返回整表），
#   ?limit=999999 则把全表行读进内存再序列化 —— 面板一个手滑就能把服务拖死。
def _int_q(name: str, default: int, lo: int = 0, hi: int = 1000) -> int:
    try:
        v = int(request.query.get(name, default, type=int))
    except (TypeError, ValueError):
        v = default
    if v <= 0 and lo > 0:
        v = default
    return max(lo, min(hi, v))


class PageApi:
    def __init__(self, plugin) -> None:
        self.plugin = plugin
        self.service: StarHeartService = plugin.service

    # ---------------- 注册 ----------------
    def register_routes(self) -> None:
        register = self.plugin.context.register_web_api
        routes: list[tuple[str, Any, str]] = [
            ("health", self.health, "GET"),
            ("overview", self.overview, "GET"),
            ("capture/queue", self.capture_queue, "GET"),
            ("capture/retry", self.capture_retry, "POST"),
            ("memories", self.memories, "GET"),
            ("memory", self.memory_detail, "GET"),
            ("memory/update", self.memory_update, "POST"),
            ("memory/delete", self.memory_delete, "POST"),
            ("memory/restore", self.memory_restore, "POST"),
            ("memory/mute", self.memory_mute, "POST"),
            ("graph", self.graph, "GET"),
            ("graph/stats", self.graph_stats, "GET"),
            ("graph/build", self.graph_build, "POST"),
            ("favours", self.favours, "GET"),
            ("favour/adjust", self.favour_adjust, "POST"),
            ("habits", self.habits, "GET"),
            ("habit/save", self.habit_save, "POST"),
            ("habit/delete", self.habit_delete, "POST"),
            ("style/samples", self.style_samples, "GET"),
            ("style/learn", self.style_learn, "POST"),
            ("style/toggle", self.style_toggle, "POST"),
            ("style/hide", self.style_hide, "POST"),
            ("style/quote", self.style_quote, "POST"),
            ("style/rename", self.style_rename, "POST"),
            ("style/status", self.style_status, "POST"),
            ("style/delete", self.style_delete, "POST"),
            ("style/quote_edit", self.style_quote_edit, "POST"),
            ("style/quote_del", self.style_quote_del, "POST"),
            ("style/term_save", self.style_term_save, "POST"),
            ("style/term_del", self.style_term_del, "POST"),
            ("gate", self.gate, "GET"),
            ("message/hub", self.message_hub, "GET"),
            ("tokens", self.tokens, "GET"),
            ("personas", self.personas, "GET"),
            ("providers", self.providers, "GET"),
            ("model/test", self.model_test, "POST"),
            ("persona/save", self.persona_save, "POST"),
            ("persona/evolve", self.persona_evolve, "GET"),
            ("persona/active", self.persona_active, "POST"),
            ("config", self.config, "GET"),
            ("config/save", self.config_save, "POST"),
            ("import/detect", self.import_detect, "GET"),
            ("import/start", self.import_start, "POST"),
            ("import/progress", self.import_progress, "GET"),
            ("audit", self.audit, "GET"),
            ("recalls", self.recalls, "GET"),
            ("persons", self.persons, "GET"),
            ("person/profile", self.person_profile, "GET"),
            ("person/profile/build", self.person_profile_build, "POST"),
            ("prompts", self.prompts, "GET"),
            ("persona/rollback", self.persona_rollback, "POST"),
            ("prompt/save", self.prompt_save, "POST"),
            ("person/profile/reset", self.person_profile_reset, "POST"),
            ("person/merge", self.person_merge, "POST"),
            ("person/unmerge", self.person_unmerge, "POST"),
            ("platform/purge", self.platform_purge, "POST"),
            ("favour/insight", self.favour_insight, "GET"),
            ("habits/log", self.habit_log, "GET"),
            ("corpus", self.corpus_list, "GET"),
            ("proactive", self.proactive, "GET"),
            ("proactive/scan", self.proactive_scan, "POST"),
            ("proactive/send", self.proactive_send, "POST"),
            ("persona/proposals", self.persona_proposals, "GET"),
            ("persona/propose", self.persona_propose, "POST"),
            ("persona/decide", self.persona_decide, "POST"),
            ("config/domains", self.config_domains, "GET"),
            ("config/snapshots", self.config_snapshots, "GET"),
            ("config/snapshot", self.config_snapshot_save, "POST"),
            ("config/rollback", self.config_rollback, "POST"),
            ("gate/verdict", self.gate_verdict, "POST"),
            ("memory/maintenance", self.memory_maintenance, "POST"),
            ("expression/extract", self.expression_extract, "POST"),
            ("memory/purge_stale", self.memory_purge_stale, "POST"),
            ("memory/edges", self.memory_edges, "GET"),
            ("edge/save", self.edge_save, "POST"),
            ("contacts", self.contacts, "GET"),
            ("contacts/hidden", self.contacts_hidden, "GET"),
            ("contacts/filter", self.contacts_filter, "GET"),
            ("contacts/set", self.contacts_set, "POST"),
            ("contacts/hide", self.contacts_hide, "POST"),
            ("proactive/preview", self.proactive_preview, "POST"),
            ("proactive/rules", self.proactive_rules, "GET"),
            ("proactive/rule", self.proactive_rule_set, "POST"),
            ("proactive/rule/bulk", self.proactive_rule_bulk, "POST"),
            ("proactive/set", self.proactive_set, "POST"),
            ("stats", self.stats, "GET"),
            ("purge/gate", self.purge_gate, "POST"),
            ("purge/learning", self.purge_learning, "POST"),
            # S4 表达重建：审核队列（GET 待审 + POST 通过/驳回）、不再学名单、学习诊断、只清表达
            ("expr/review", self.expr_review_get, "GET"),
            ("expr/review", self.expr_review_post, "POST"),
            ("expr/bans", self.expr_bans, "GET"),
            ("expr/ban/remove", self.expr_ban_remove, "POST"),
            ("expr/runs", self.expr_runs, "GET"),
            ("purge/expressions", self.purge_expressions, "POST"),
            # S5 黑话（从零建）：清单 / 人工闸门 / 改释义 / 删除 / 命中日志 / 试解读 /「未进入回复库」检查行
            ("jargon", self.jargon_list, "GET"),
            ("jargon/decide", self.jargon_decide, "POST"),
            ("jargon/save", self.jargon_save, "POST"),
            ("jargon/delete", self.jargon_delete, "POST"),
            ("jargon/hits", self.jargon_hits, "GET"),
            ("jargon/try", self.jargon_try, "POST"),
            ("jargon/check", self.jargon_check, "GET"),

            ("purge/session", self.purge_session, "POST"),
            # 记忆库管理表 + 混合召回 + 详细对话（这一批是"后端早就该给界面"的东西）
            ("memory/search", self.memory_search, "GET"),
            ("memory/sessions", self.memory_sessions, "GET"),
            ("memory/evidence", self.memory_evidence, "GET"),
            ("messages", self.messages, "GET"),
            ("recall", self.recall, "POST"),
            ("embed/coverage", self.embed_coverage, "GET"),
            ("vector/gens", self.vector_gens, "GET"),
            ("model/dims", self.model_dims, "GET"),
            ("vector/switch", self.vector_switch, "POST"),
            ("vector/drop", self.vector_drop, "POST"),
            ("embed/backfill", self.embed_backfill, "POST"),
            ("memory/rewrite_summaries", self.memory_rewrite_summaries, "POST"),
            ("import/extras", self.import_extras, "POST"),
            ("memory/reactivate", self.memory_reactivate, "POST"),
            ("memory/lifecycle", self.memory_lifecycle, "GET"),
            ("memory/lifecycle/run", self.memory_lifecycle_run, "POST"),
            # 拟人指标（六指标 + 长度分布 + 门控因子分布；每项带 n 与 state）
            ("metrics", self.metrics, "GET"),
        ]
        for path, handler, method in routes:
            register(f"{PAGE_PREFIX}/{path}", self._guard(path, handler), [method], f"StarHeart page {path}")

    @staticmethod
    def _guard(path: str, handler):
        """统一异常兜底：任何未捕获异常 → 500 + 一句话，而不是把 traceback 甩给前端。

        84 条路由里原本只有 11 条自带 try/except，其余任何 service/store 的非预期异常
        都会直接变成 500 空响应（前端只能显示「接口失败」，排查全靠翻服务端日志）。
        与其逐条补 try（补了 73 处，下一条新路由还是会漏），不如在注册处统一包一层 ——
        新增路由自动获得同样保护。
        """
        async def wrapped(*args, **kwargs):
            try:
                return await handler(*args, **kwargs)
            except Exception as exc:                        # noqa: BLE001
                logger.error(f"[StarHeart] 页面接口 {path} 失败：{exc}", exc_info=True)
                return error_response(
                    f"{path} 执行失败：{type(exc).__name__}: {str(exc)[:200]}", status_code=500
                )

        wrapped.__name__ = getattr(handler, "__name__", "handler")
        return wrapped

    # ---------------- handler ----------------
    async def health(self):
        return json_response(await self.service.health())

    async def overview(self):
        persona_id = request.query.get("persona_id", "") or ""
        return json_response(await self.service.overview(persona_id))

    async def capture_queue(self):
        """捕获队列（待重试 / 停摆）——只读，不含对话正文。"""
        limit = _int_q("limit", 20, 1, 200)
        return json_response(await self.service.capture_queue(limit))

    async def capture_retry(self):
        """手动把 parked 的批放回重试链。"""
        limit = _int_q("limit", 20, 1, 200)
        return json_response(await self.service.capture_retry(limit))

    async def memories(self):
        q = request.query.get("q", "") or ""
        tag = request.query.get("tag", "") or ""
        source = request.query.get("source", "") or ""
        page = _int_q("page", 1, 1, 10000)
        size = _int_q("size", 50, 1, 500)
        persona_id = request.query.get("persona_id", "") or ""
        return json_response(await self.service.memories(q=q, tag=tag, source=source, page=page, size=size, persona_id=persona_id))

    async def memory_detail(self):
        mid = request.query.get("id", "")
        if not mid:
            return error_response("missing id")
        data = await self.service.memory(mid)
        if data is None:
            return error_response("memory not found", status_code=404)
        return json_response(data)

    async def memory_update(self):
        payload = await request.json(default={})
        mid = payload.get("id")
        if not mid:
            return error_response("missing id")
        return json_response(await self.service.memory_update(mid, payload.get("fields") or {}))

    async def memory_delete(self):
        payload = await request.json(default={})
        if not payload.get("id"):
            return error_response("missing id")
        return json_response(await self.service.memory_delete(payload["id"]))

    async def memory_restore(self):
        payload = await request.json(default={})
        if not payload.get("id"):
            return error_response("missing id")
        return json_response(await self.service.memory_restore(payload["id"]))

    async def memory_mute(self):
        """「别再提」开关：{"id": "985", "muted": true, "source_mode": "test"}"""
        payload = await request.json(default={})
        if not payload.get("id"):
            return error_response("missing id")
        muted = payload.get("muted")
        muted = True if muted is None else bool(muted)
        return json_response(await self.service.memory_mute(
            str(payload["id"]), muted, str(payload.get("source_mode") or "")))

    async def recalls(self):
        limit = _int_q("limit", 200, 1, 2000)
        return json_response(await self.service.recalls(limit))

    async def persons(self):
        limit = _int_q("limit", 60, 1, 2000)
        persona_id = request.query.get("persona_id", "") or ""
        return json_response(await self.service.persons(limit, persona_id))

    async def persona_evolve(self):
        return json_response(await self.service.evolve_state())

    async def person_profile(self):
        pid = request.query.get("pid", "")
        kind = request.query.get("kind", "user") or "user"
        persona_id = request.query.get("persona_id", "") or ""
        return json_response(await self.service.person_profile(pid, kind, persona_id))

    async def person_profile_build(self):
        payload = await request.json(default={})
        return json_response(await self.service.person_profile_build(
            str(payload.get("pid") or ""),
            str(payload.get("kind") or "user"),
            str(payload.get("persona_id") or "")))

    async def persona_rollback(self):
        payload = await request.json(default={})
        return json_response(await self.service.persona_rollback(int(payload.get("id") or 0)))

    async def prompts(self):
        return json_response(await self.service.prompt_registry())

    async def prompt_save(self):
        payload = await request.json(default={})
        return json_response(await self.service.save_task_prompt(
            str(payload.get("task") or ""), payload.get("system")))

    async def person_profile_reset(self):
        payload = await request.json(default={})
        # 重置必须带人格：空人格在存储层会被拒绝（避免跨人格误删），这里提前给出可读原因
        persona_id = str(payload.get("persona_id") or "")
        if not persona_id:
            return json_response({"ok": False, "code": "persona_required",
                                  "error": "未指定人格，已拒绝删除 —— 画像按人格隔离，请在面板选定人格后再重置"})
        return json_response(await self.service.person_profile_reset(
            str(payload.get("pid") or ""),
            str(payload.get("kind") or "user"),
            persona_id))

    async def person_merge(self):
        payload = await request.json(default={})
        return json_response(await self.service.person_merge(str(payload.get("alias") or ""),
                                                            str(payload.get("canonical") or "")))

    async def person_unmerge(self):
        payload = await request.json(default={})
        return json_response(await self.service.person_unmerge(str(payload.get("alias") or "")))

    async def platform_purge(self):
        """清理非 QQ 平台残留数据（危险操作，必须显式确认）。

        原实现：空 body 就默认去清 ["webchat","webui"] 两个平台 —— 面板上一个误点、
        或者任何能发 POST 的地方，都能在没有任何确认的情况下批量删数据。既没有
        二次确认、也没有 dry-run、还不幂等（跑两次删两遍）。
        现在要求请求体里带 confirm="PURGE" 才执行，并回传 dry_run 预览数量。
        """
        payload = await request.json(default={}) or {}
        plats = payload.get("platforms") or ["webchat", "webui"]
        if isinstance(plats, str):
            plats = [x.strip() for x in plats.split(",") if x.strip()]
        if str(payload.get("confirm") or "").strip() != "PURGE":
            return error_response(
                f"危险操作未确认：本接口会物理删除平台 {list(plats)} 在库里的全部痕迹"
                "（门控流水 / 语料 / 主动记录 / 召回流水 / 记忆 / 好感 / 画像 / 昵称）。"
                '若确认执行，请在请求体里带 confirm="PURGE"。',
                status_code=400,
            )
        return json_response(await self.service.purge_platform_sessions(list(plats)))

    async def favour_insight(self):
        return json_response(await self.service.favour_insight())

    async def habit_log(self):
        return json_response(await self.service.habit_log())

    async def proactive(self):
        limit = _int_q("limit", 200, 1, 2000)
        return json_response(await self.service.proactive_view(max(50, min(limit, 500))))

    async def proactive_scan(self):
        return json_response(await self.service.proactive_scan())

    async def proactive_send(self):
        payload = await request.json(default={})
        sid = str(payload.get("session_id") or "").strip()
        kind = str(payload.get("kind") or "").strip()
        ident = str(payload.get("ident") or "").strip()
        # 面板的「一键试发」在目标是纯 QQ 号 / 群号时走 kind+ident（前端不知道完整会话串），
        # 只有填了带冒号的会话串才给 session_id。这里两个入口都得认，否则填 QQ 号必回
        # 「missing session_id」——service 层本来就能解析 kind+ident，是这层把它挡掉了。
        if not sid and not (kind and ident):
            return error_response("missing session_id")
        return json_response(await self.service.proactive_test_send(
            sid, str(payload.get("text") or ""), kind=kind, ident=ident,
        ))

    async def persona_proposals(self):
        return json_response(await self.service.persona_proposals())

    async def persona_propose(self):
        return json_response(await self.service.persona_propose())

    async def persona_decide(self):
        payload = await request.json(default={})
        try:
            return json_response(await self.service.persona_decide(int(payload.get("id")), bool(payload.get("approve"))))
        except (TypeError, ValueError):
            return error_response("bad id")

    async def config_domains(self):
        return json_response(await self.service.config_domains())

    async def memory_maintenance(self):
        return json_response(await self.service.memory_maintenance())

    async def expression_extract(self):
        try:
            return json_response(await self.service.habit_extract())
        except Exception as exc:          # 提取链路出问题要回一句人话，别让面板吃 500
            logger.warning(f"[starheart] expression/extract failed: {exc}")
            return error_response(f"表达提取失败：{str(exc)[:160]}")

    async def corpus_list(self):
        limit = _int_q("limit", 50, 1, 2000)
        is_bot = request.query.get("is_bot", "")
        flag: int | None = None
        if str(is_bot) in ("0", "1"):
            flag = int(is_bot)
        return json_response(await self.service.corpus_list(limit, is_bot=flag))

    async def memory_purge_stale(self):
        # 可选 persona_id：不传 = 全部人格（与旧行为一致）；传了就只清该人格。
        payload = await request.json(default={}) or {}
        raw = str(payload.get("persona_id") or "").strip()
        return json_response(await self.service.purge_stale(raw or None))

    async def memory_edges(self):
        mid = request.query.get("id", "", type=str)
        if not mid:
            return error_response("missing id")
        return json_response(await self.service.memory_edges(mid))

    async def edge_save(self):
        payload = await request.json(default={})
        return json_response(await self.service.edge_save(payload.get("items") or []))

    async def contacts_hidden(self):
        return json_response(await self.service.hidden_contacts())

    async def contacts_filter(self):
        return json_response(await self.service.contact_filter())

    async def contacts_set(self):
        payload = await request.json(default={})
        return json_response(await self.service.set_contact_filter(
            str(payload.get("kind") or ""),
            str(payload.get("mode") or "off"),
            [str(x) for x in (payload.get("ids") or [])],
        ))

    async def contacts_hide(self):
        payload = await request.json(default={})
        kind = str(payload.get("kind") or "group")
        ids = [str(x) for x in (payload.get("ids") or [])]
        try:
            return json_response(await self.service.set_hidden_contacts(kind, ids))
        except ValueError as exc:
            return error_response(str(exc))

    async def contacts(self):
        force = str(request.query.get("force", "", type=str)).lower() in ("1", "true", "yes")
        return json_response(await self.service.contacts(force))

    async def proactive_preview(self):
        payload = await request.json(default={})
        # zone 缺省不要在这里填 "NEUTRAL"：service 收到空串会按会话真实关系档解析；
        # 在这层硬填一个默认值，等于把「真实档」挡在门外 —— 试看和真发就会两张皮
        # （真机实测：库里是 FAMILIAR，试看却按 NEUTRAL 出词）。
        return json_response(await self.service.proactive_preview(
            str(payload.get("session_id") or ""), str(payload.get("zone") or "")
        ))

    async def proactive_rule_bulk(self):
        payload = await request.json(default={})
        try:
            return json_response(await self.service.rule_bulk(
                str(payload.get("kind") or ""), str(payload.get("action") or ""),
                [str(x) for x in (payload.get("ids") or [])]
            ))
        except ValueError as exc:
            return error_response(str(exc))

    async def proactive_set(self):
        payload = await request.json(default={})
        return json_response(await self.service.proactive_set(
            str(payload.get("kind") or ""),
            str(payload.get("mode") or "white"),
            [str(x) for x in (payload.get("ids") or [])],
        ))

    async def proactive_rules(self):
        return json_response(await self.service.proactive_rules())

    async def proactive_rule_set(self):
        payload = await request.json(default={})
        try:
            return json_response(await self.service.rule_set(
                str(payload.get("kind") or "group"), str(payload.get("id") or ""), str(payload.get("action") or "clear")
            ))
        except ValueError as exc:
            return error_response(str(exc))

    async def stats(self):
        persona_id = request.query.get("persona_id", "") or ""
        return json_response(await self.service.panel_stats(persona_id))

    async def purge_gate(self):
        return json_response(await self.service.purge("gate"))

    async def purge_learning(self):
        return json_response(await self.service.purge("learning"))

    async def purge_session(self):
        payload = await request.json(default={})
        try:
            return json_response(await self.service.purge("session", str(payload.get("session_id") or "")))
        except ValueError as exc:
            return error_response(str(exc))

    async def graph(self):
        # 默认上限从 400 提到 4000：图谱原先是 ORDER BY strength DESC + LIMIT 400，
        # 被衰减过的导入记忆必然排在 400 名之外 → 用户看到的"外部导入定位不到"。
        limit = _int_q("limit", 4000, 1, 8000)
        persona_id = request.query.get("persona_id", "") or ""
        return json_response(await self.service.graph(max(20, min(limit, 6000)), persona_id=persona_id))

    async def graph_stats(self):
        persona_id = request.query.get("persona_id", "") or ""
        return json_response(await self.service.graph_stats(persona_id))

    async def graph_build(self):
        payload = await request.json(default={})
        persona_id = str(payload.get("persona_id") or request.query.get("persona_id", "") or "")
        try:
            limit = int(payload.get("limit") or 3000)
        except (TypeError, ValueError):
            limit = 3000
        return json_response(await self.service.graph_build(max(50, min(limit, 20000)), persona_id=persona_id))

    async def favours(self):
        return json_response(await self.service.favours())

    async def favour_adjust(self):
        payload = await request.json(default={})
        try:
            out = await self.service.favour_adjust(
                str(payload.get("user_id") or ""),
                float(payload.get("delta") or 0),
                str(payload.get("reason") or ""),
                str(payload.get("scope") or "both"),
                str(payload.get("name") or ""),
            )
        except (ValueError, TypeError) as exc:
            return error_response(str(exc))
        return json_response(out)

    async def habits(self):
        # persona_id 由页面带上（视角人格）：可用性求值 / 隔离徽标按它算（E1 fail-closed）
        persona_id = request.query.get("persona_id", "") or ""
        return json_response(await self.service.habits(persona_id))

    # ---------------- 语言学习 MVP：原话样本（新管线，只读 + 三个动作） ----------------
    async def _style_pid(self) -> str:
        pid = str(request.query.get("persona_id", "") or "")
        if pid:
            return pid
        try:
            return str(await self.plugin.service._active_pid() or "")
        except Exception:                                   # noqa: BLE001
            return ""

    async def style_samples(self):
        from . import style_memory as style_mod

        pid = await self._style_pid()
        terms = await self.plugin.store.list_style_terms(pid, limit=30)
        cfg = await self.plugin.service.config()
        preview = ""
        try:
            preview = await style_mod.style_block(
                self.plugin.store, {**cfg, "expression_style_enabled": True},
                pid, "private", "")
        except Exception as exc:                            # noqa: BLE001 预览失败不影响列表
            preview = f"（预览失败：{exc}）"
        styles = await self.plugin.store.list_styles(pid)
        quotes = await self.plugin.store.list_quotes(pid)
        by_style: dict[int, list[dict[str, Any]]] = {}
        for q in quotes:
            by_style.setdefault(int(q.get("style_id") or 0), []).append({
                "id": q.get("id"), "quote": q.get("quote"),
                "memory_id": q.get("memory_id"), "user_name": q.get("user_name"),
                "user_id": q.get("user_id"), "register": q.get("register"),
                "trigger_text": q.get("trigger_text"),
                "hits": q.get("hits"), "corrections": q.get("corrections"),
                "status": q.get("status"), "created_at": q.get("created_at"),
            })
        out_styles = []
        for s in styles:
            qs = by_style.get(int(s["id"]), [])
            out_styles.append({
                "id": s["id"], "name": s["name"], "status": s["status"],
                "hits": s["hits"], "corrections": s["corrections"],
                "quotes": qs,
                "active": sum(1 for q in qs if q["status"] == "active"),
            })
        return json_response({
            "enabled": bool(cfg.get("expression_style_enabled")),
            "mode": str(cfg.get("expression_style_inject_mode") or "legacy"),
            "counts": {
                "styles": len(out_styles),
                "quotes": sum(len(v) for v in by_style.values()),
                "terms": len(terms),
            },
            "styles": out_styles,
            "terms": terms,
            "preview": preview,
        })

    async def style_hide(self):
        payload = await request.json(default={})
        try:
            sid = int(payload.get("id") or 0)
        except (TypeError, ValueError):
            sid = 0
        if not sid:
            return error_response("id required")
        n = await self.plugin.store.hide_style_sample(sid)
        return json_response({"ok": bool(n), "hidden": n})

    async def style_learn(self):
        from . import style_memory as style_mod

        pid = await self._style_pid()
        res = await style_mod.run_learn(
            self.plugin, await self.plugin.service.config(), pid)
        return json_response(res)

    async def style_quote(self):
        payload = await request.json(default={})
        try:
            qid = int(payload.get("id") or 0)
        except (TypeError, ValueError):
            qid = 0
        if not qid:
            return error_response("id required")
        status = "active" if str(payload.get("action") or "") == "restore" else "dropped"
        n = await self.plugin.store.set_quote_status(qid, status)
        return json_response({"ok": bool(n), "id": qid, "status": status})

    async def style_rename(self):
        payload = await request.json(default={})
        try:
            sid = int(payload.get("id") or 0)
        except (TypeError, ValueError):
            sid = 0
        name = str(payload.get("name") or "").strip()
        if not sid or not name:
            return error_response("id and name required")
        n = await self.plugin.store.rename_style(sid, name)
        return json_response({"ok": bool(n), "id": sid, "name": name})

    async def style_status(self):
        payload = await request.json(default={})
        try:
            sid = int(payload.get("id") or 0)
        except (TypeError, ValueError):
            sid = 0
        if not sid:
            return error_response("id required")
        status = "paused" if str(payload.get("status") or "") == "paused" else "active"
        n = await self.plugin.store.set_style_status(sid, status)
        return json_response({"ok": bool(n), "id": sid, "status": status})

    async def style_delete(self):
        payload = await request.json(default={})
        try:
            sid = int(payload.get("id") or 0)
        except (TypeError, ValueError):
            sid = 0
        if not sid:
            return error_response("id required")
        n = await self.plugin.store.delete_style(sid)
        return json_response({"ok": bool(n), "id": sid})

    async def style_quote_edit(self):
        payload = await request.json(default={})
        try:
            qid = int(payload.get("id") or 0)
        except (TypeError, ValueError):
            qid = 0
        quote = str(payload.get("quote") or "").strip()
        if not qid or not quote:
            return error_response("id and quote required")
        n = await self.plugin.store.update_quote(qid, quote)
        return json_response({"ok": bool(n), "id": qid, "quote": quote})

    async def style_quote_del(self):
        payload = await request.json(default={})
        try:
            qid = int(payload.get("id") or 0)
        except (TypeError, ValueError):
            qid = 0
        if not qid:
            return error_response("id required")
        n = await self.plugin.store.delete_quote(qid)
        return json_response({"ok": bool(n), "id": qid})

    async def style_term_save(self):
        payload = await request.json(default={})
        try:
            tid = int(payload.get("id") or 0)
        except (TypeError, ValueError):
            tid = 0
        if not tid:
            return error_response("id required")
        kwargs: dict[str, Any] = {}
        for key in ("term", "meaning", "usage", "scope"):
            if key in payload:
                kwargs[key] = str(payload.get(key) or "")
        if "enabled" in payload:
            kwargs["enabled"] = bool(payload.get("enabled"))
        n = await self.plugin.store.update_style_term(tid, **kwargs)
        return json_response({"ok": bool(n), "id": tid})

    async def style_term_del(self):
        payload = await request.json(default={})
        try:
            tid = int(payload.get("id") or 0)
        except (TypeError, ValueError):
            tid = 0
        if not tid:
            return error_response("id required")
        n = await self.plugin.store.delete_style_term(tid)
        return json_response({"ok": bool(n), "id": tid})

    async def style_toggle(self):
        payload = await request.json(default={})
        patch: dict[str, Any] = {}
        if "enabled" in payload:
            patch["expression_style_enabled"] = bool(payload.get("enabled"))
        if "mode" in payload:
            m = str(payload.get("mode") or "").lower()
            if m not in ("legacy", "new"):
                return error_response("mode must be legacy|new")
            patch["expression_style_inject_mode"] = m
        if not patch:
            return error_response("nothing to update")
        try:
            await self.plugin.service.save_config(patch)
            await self.plugin.service.config_save_sync()
        except Exception as exc:                            # noqa: BLE001
            return error_response(str(exc)[:200])
        return json_response({"ok": True, **patch})

    async def expr_review_get(self):
        return json_response(await self.service.expr_review_queue(_int_q("limit", 200, 1, 500)))

    async def expr_review_post(self):
        payload = await request.json(default={})
        raw = payload.get("ids")
        ids = raw if isinstance(raw, list) else ([raw] if raw is not None else [])
        action = str(payload.get("action") or "").strip()
        if action not in ("approve", "reject"):
            return error_response("action must be approve|reject")
        return json_response(await self.service.expr_review(ids, action, str(payload.get("reason") or "")))

    async def expr_bans(self):
        return json_response(await self.service.expr_bans(_int_q("limit", 200, 1, 500)))

    async def expr_ban_remove(self):
        payload = await request.json(default={})
        nk = str(payload.get("norm_key") or "").strip()
        if not nk:
            return error_response("missing norm_key")
        return json_response(await self.service.expr_ban_remove(nk))

    async def expr_runs(self):
        return json_response(await self.service.expr_runs(_int_q("limit", 20, 1, 200)))

    async def purge_expressions(self):
        return json_response(await self.service.purge("expressions"))

    # ---------------- S5 黑话（hf_jargon） ----------------
    async def jargon_list(self):
        status = request.query.get("status", "") or ""
        q = request.query.get("q", "") or ""
        scope = request.query.get("scope", "") or ""
        return json_response(await self.service.jargon_list(status=status, q=q, scope=scope))

    async def jargon_decide(self):
        payload = await request.json(default={})
        try:
            return json_response(await self.service.jargon_decide(
                int(payload.get("id") or 0), str(payload.get("action") or "")))
        except (TypeError, ValueError) as exc:
            return error_response(str(exc))

    async def jargon_save(self):
        payload = await request.json(default={})
        try:
            return json_response(await self.service.jargon_save(
                int(payload.get("id") or 0),
                str(payload.get("term") or ""),
                str(payload.get("meaning") or ""),
                payload.get("enabled")))
        except (TypeError, ValueError) as exc:
            return error_response(str(exc))

    async def jargon_delete(self):
        payload = await request.json(default={})
        try:
            return json_response(await self.service.jargon_delete(int(payload.get("id") or 0)))
        except (TypeError, ValueError) as exc:
            return error_response(str(exc))

    async def jargon_hits(self):
        tid = _int_q("term_id", 0, 0, 10 ** 9)
        if tid <= 0:
            return error_response("missing term_id")
        return json_response(await self.service.jargon_hits(tid, _int_q("limit", 20, 1, 200)))

    async def jargon_try(self):
        payload = await request.json(default={})
        try:
            return json_response(await self.service.jargon_try(str(payload.get("text") or "")))
        except (TypeError, ValueError) as exc:
            return error_response(str(exc))

    async def jargon_check(self):
        return json_response(await self.service.jargon_check())

    async def habit_save(self):
        payload = await request.json(default={})
        return json_response(await self.service.habit_save(payload))

    async def habit_delete(self):
        payload = await request.json(default={})
        try:
            return json_response(await self.service.habit_delete(int(payload.get("id") or 0)))
        except (TypeError, ValueError):
            return error_response("invalid id")

    async def gate(self):
        limit = _int_q("limit", 100, 1, 2000)
        offset = _int_q("offset", 0, 0, 1000000)
        return json_response(await self.service.gate_log(limit, offset))

    async def tokens(self):
        days = _int_q("days", 7, 1, 3650)
        return json_response(await self.service.tokens(days))

    async def personas(self):
        return json_response(await self.service.personas())

    async def providers(self):
        return json_response(await self.service.providers())

    async def model_test(self):
        payload = await request.json(default={})
        return json_response(
            await self.service.model_test(str(payload.get("task") or ""), str(payload.get("provider_id") or ""))
        )

    async def persona_save(self):
        payload = await request.json(default={})
        try:
            return json_response(await self.service.persona_save(payload))
        except ValueError as exc:
            return error_response(str(exc))

    async def persona_active(self):
        payload = await request.json(default={})
        return json_response(await self.service.persona_set_active(str(payload.get("persona_id") or "")))

    async def config(self):
        return json_response(await self.service.config())

    async def config_save(self):
        payload = await request.json(default={})
        try:
            out = await self.service.save_config(payload.get("patch") or payload or {})
        except ValueError as exc:
            # 参数护栏拦下：整批一个字都没写，把原因原样交给面板（前端 toast 直接显示）
            return error_response(str(exc))
        # 换了向量模型 → 自动切到对应维度的向量库
        # （原先这行写在 return 之后，属不可达代码，等于从没执行过）
        sync = await self.service.config_save_sync()
        if isinstance(sync, dict) and sync.get("switched"):
            logger.info(f"[starheart] 向量库已自动切代：{sync.get('active')}")
        return json_response(out)


    async def config_snapshots(self):
        return json_response(await self.service.config_snapshots(_int_q("limit", 25, 1, 100)))

    async def config_snapshot_save(self):
        payload = await request.json(default={})
        return json_response(await self.service.config_snapshot_save(name=str(payload.get("name") or "")))

    async def config_rollback(self):
        payload = await request.json(default={})
        try:
            return json_response(await self.service.config_rollback(payload.get("id") or 0))
        except (TypeError, ValueError) as exc:
            return error_response(str(exc))

    async def gate_verdict(self):
        """门控行内改判（该回 / 判定正确 / 撤销）：只记账，写 hf_decision_log.user_verdict。"""
        payload = await request.json(default={})
        try:
            return json_response(await self.service.gate_verdict(
                payload.get("id") or 0, str(payload.get("verdict") or "")))
        except (TypeError, ValueError) as exc:
            return error_response(str(exc))

    async def import_detect(self):
        return json_response(await self.service.import_detect())

    async def import_start(self):
        payload = await request.json(default={})
        layers = payload.get("layers")
        if not isinstance(layers, list):
            cfg = await self.service.config()
            layers = cfg.get("import_layers") or ["documents", "atoms"]
        limit = payload.get("limit")
        out = await self.service.import_start(layers=[str(x) for x in layers], limit=int(limit) if limit else None)
        return json_response(out)

    async def import_progress(self):
        async def events():
            try:
                async for item in self.service.import_events():
                    yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"
            except Exception as exc:  # 进度流断了要说清楚
                logger.warning(f"[starheart] import progress stream error: {exc}")
                yield f"data: {json.dumps({'phase': 'error', 'message': str(exc)}, ensure_ascii=False)}\n\n"

        return stream_response(events())

    async def audit(self):
        limit = _int_q("limit", 50, 1, 2000)
        return json_response(await self.service.audit(limit))

    # ---------------- 记忆库管理表 / 混合召回 / 详细对话 ----------------

    async def memory_search(self):
        q = request.query.get("q", "") or ""
        session_id = request.query.get("session_id", "") or ""
        persona_id = request.query.get("persona_id", "") or ""
        status = request.query.get("status", "") or ""
        atom_type = request.query.get("atom_type", "") or ""
        source = request.query.get("source", "") or ""
        sort = request.query.get("sort", "recent") or "recent"
        limit = min(200, max(1, request.query.get("limit", 20, type=int) or 20))
        offset = max(0, request.query.get("offset", 0, type=int) or 0)
        return json_response(await self.service.memory_search(
            q=q, session_id=session_id, persona_id=persona_id, status=status,
            atom_type=atom_type, source=source, sort=sort, limit=limit, offset=offset,
        ))

    async def memory_sessions(self):
        return json_response(await self.service.memory_sessions())

    async def memory_evidence(self):
        mid = request.query.get("id", "") or ""
        if not mid:
            return error_response("missing id")
        return json_response(await self.service.memory_evidence(mid))

    async def messages(self):
        session_id = request.query.get("session_id", "") or ""
        if not session_id:
            return error_response("missing session_id")
        q = request.query.get("q", "") or ""
        limit = min(500, max(1, request.query.get("limit", 200, type=int) or 200))
        offset = max(0, request.query.get("offset", 0, type=int) or 0)
        return json_response(await self.service.messages_list(session_id=session_id, q=q, limit=limit, offset=offset))

    async def recall(self):
        """召回测试：关键词路 + 向量路 → RRF 融合。没有向量也能用（只跑关键词路并如实回报）。"""
        payload = await request.json(default={})
        query = str(payload.get("query") or "").strip()
        if not query:
            return error_response("empty query")
        topk = min(50, max(1, int(payload.get("topk") or 8)))
        out = await self.plugin.hybrid_recall(
            query,
            persona_id=str(payload.get("persona_id") or (request.query.get("persona_id", "") or "")),
            session_id=str(payload.get("session_id") or ""),
            topk=topk,
        )
        await self.service.store.audit("recall.test", query[:60], "topk=%d 命中=%d" % (topk, len(out.get("items") or [])))
        return json_response(out)

    async def embed_coverage(self):
        return json_response(await self.service.embedding_coverage())

    async def embed_backfill(self):
        """补向量：分批 + 限速。AstrBot 平台经验：1 秒内连打多次 embedding 会拖死 event loop，
        所以这里每批之间 sleep，宁可慢也不把整站卡住。"""
        payload = await request.json(default={})
        limit = min(64, max(1, int(payload.get("limit") or 16)))
        return json_response(await self.plugin.embed_backfill(limit=limit))

    async def memory_rewrite_summaries(self):
        """重写记忆简写（去掉「用户…」机器句式）：按人格分批改写，面板循环调用直到 remaining=0。"""
        payload = await request.json(default={})
        return json_response(await self.plugin.rewrite_summaries(
            persona_id=str(payload.get("persona_id") or ""),
            limit=min(24, max(1, int(payload.get("limit") or 12))),
        ))

    async def import_extras(self):
        return json_response(await self.service.import_extras())

    async def memory_reactivate(self):
        """把 stale/decayed 的记忆拉回召回范围（导入的历史记忆默认被衰减判死，这不该是默认结局）。"""
        payload = await request.json(default={})
        ids = payload.get("ids")
        ids = [int(x) for x in ids] if isinstance(ids, list) and ids else None
        floor = float(payload.get("floor") or 0.45)
        return json_response(await self.service.memory_reactivate(ids, floor))

    async def memory_lifecycle(self):
        """自动遗忘现状 + 两条待处理队列（面板展示用）。"""
        return json_response(await self.service.lifecycle_preview())

    async def memory_lifecycle_run(self):
        payload = await request.json(default={})
        limit = int(payload.get("limit") or 0) or None
        return json_response(await self.service.lifecycle_tick(limit=limit, dry=bool(payload.get("dry"))))

    async def metrics(self):
        """拟人指标：只读聚合；空表/样本不足/读失败如实报态（ok / INSUFFICIENT_DATA / NOT_IMPLEMENTED / ERROR）。"""
        days = _int_q("days", 30, 1, 3650)
        return json_response(await self.service.metrics(days))

    async def vector_gens(self):
        """多代向量库清单（换模型不删旧代、按当前模型选库）。"""
        return json_response(await self.service.vector_gens())

    async def vector_switch(self):
        payload = await request.json(default={})
        gen = str(payload.get("gen_key") or "")
        if not gen:
            return error_response("missing gen_key")
        return json_response(await self.service.vector_switch(gen))

    async def vector_drop(self):
        payload = await request.json(default={})
        gen = str(payload.get("gen_key") or "")
        if not gen:
            return error_response("missing gen_key")
        return json_response(await self.service.vector_drop(gen))

    async def model_dims(self):
        """每个向量模型已知的维度（从向量库各代推出来）+ 当前在用哪一代。"""
        return json_response(await self.service.model_dims())

    async def message_hub(self):
        """消息事件页聚合：KPI + 节律热力 + 漏斗/分布/原因 + 三源统一事件流。"""
        limit = min(200, max(10, request.query.get("limit", 60, type=int) or 60))
        session_id = request.query.get("session_id", "") or ""
        return json_response(await self.service.message_hub(limit=limit, session_id=session_id))

/**
 * StarHeart 面板 · 总览 / 语言学习 / 人格 / 设置
 *
 * 图形化取舍：
 * - 总览：模块之间是**依赖关系**（门控决定开口、记忆喂门控、好感调语气），用迷你网络图标出来比平铺卡片更能说明结构。
 * - 语言学习：本质是「情景 → 表达」的映射，用二分图（桑基）画。
 * - Token：按任务与模型聚合是后端给的真数据，不做按天趋势（后端没有这个口径，不编）。
 * - 设置：模块开关 + 数据治理（破坏性操作集中卡），模块卡带状态灯。
 */

import * as C from "./charts.js?v=1.0.4-3";
import {
  $, $$, api, apiSafe, apiPostSafe, esc, fmtNum, fmtPct, fmt1, fmtTime, timeAgo, toast, openDrawer, closeDrawer,
  confirmModal, formModal, pageHead, card, kpiCard, kpis, secTitle, mount, emptyBlock, skelBlock, jsStr, STATE, segmentHtml,
  syncSeg, bridge, EP, refreshCurrent, goPage, countUp, configTouchedAt, noteConfigTouched, recentFirstKeys, takeConfigDirty, nativeWriteError,
} from "./store.js?v=1.0.4-3";
import { renderDomainConf, domainRowsHtml, CONFIG_META } from "./views-conf.js?v=1.0.4-3";
// 收尾批：四个消息域的参数渲染器（原页底折叠区）——原样迁进本文件的「设置 · 模块与参数」页
import { renderGateConf, renderProactiveConf, renderRecallConf, renderFavourConf } from "./views-events.js?v=1.0.4-3";
import { ATOM_LABEL } from "./views-memory.js?v=1.0.4-3";
import { GRAPH_PALETTE } from "./graph.js?v=1.0.4-3";

// S4-2：「先审后用」开关挂语言学习域。CONFIG_META 由 views-conf.js 定义并被域页过滤读取，
// 新键必须登记在这里，否则参数页不渲染、保存也会被 CONFIG_META 过滤掉（静默丢键）。
CONFIG_META.expression_review_first = ["先审后用", "学到的新表达先等你过目（默认关 = 自动通过，学到就能用）", "bool"];

// 菜单徽标「待审 N」：进应用时拉一次（N>0 才显示）
setTimeout(() => { syncExpressionBadge().catch(() => {}); }, 1200);
const ATOM_COLOR = GRAPH_PALETTE.memory;
const MODULES = [
  { key: "memory", label: "记忆", icon: "◍", col: "#3c96ca" },
  { key: "gate", label: "门控", icon: "◉", col: "#03c9d7" },
  { key: "expression", label: "表达", icon: "✎", col: "#8b5cf6" },
  { key: "favour", label: "好感", icon: "♥", col: "#e0559a" },
  { key: "proactive", label: "主动", icon: "☎", col: "#f59e0b" },
  { key: "persona", label: "人格", icon: "☺", col: "#3aa76d" },
];

/* ==================================================================
   总览
   ================================================================== */
export async function renderOverview() {
  const [ov, gate, gstats, voice] = await Promise.all([
    api("overview"),
    apiSafe("gate", { limit: 40 }, { items: [], total: 0 }),
    apiSafe("graph/stats", {}, {}),
    apiSafe("actor/voice", {}, { styles: [], terms: [], jargon: [], counts: {} }),
  ]);
  const vc = (voice && voice.counts) || {};
  STATE.overview = ov;
  STATE.graphStats = gstats;
  const c = ov.config || {};
  const cap = ov.capture || {};
  const gateItems = gate.items || [];
  const trigger = gateItems.filter((g) => g.decision === "reply").length;

  // apiSafe 失败回落时返回的对象带 __err/__path。原实现直接读字段 → 接口一挂就显示
  // 「平均度数 0.0 · 孤立 0」，和这一页页头承诺的「拿不到就写「—」，不编造」正好相反
  // —— 用户看到的像是「关系链真的断了」，其实是接口失败了。
  const _failed = (x) => !!(x && typeof x === "object" && x.__err);
  const gstatsErr = _failed(gstats) ? String(gstats.__err).slice(0, 70) : "";
  const gateErr = _failed(gate) ? String(gate.__err).slice(0, 70) : "";
  const chainSub = gstatsErr
    ? `⚠ 读取失败：${gstatsErr}（数字不可用，不当作 0）`
    : `平均度数 ${fmt1(gstats.avg_degree)} · 孤立 ${fmtNum(gstats.isolated || 0)}`;

  const health = [
    { n: "记忆系统", ok: c.master_switch !== false, d: `${fmtNum(ov.memories)} 条 · 平均强度 ${fmt1(ov.memory_avg_strength)}` },
    { n: "捕获队列", ok: ov.capture ? !(cap.parked > 0) : true, d: ov.capture
        ? (cap.parked
            ? `停摆 ${fmtNum(cap.parked)} 批（可重试）· 待重试 ${fmtNum(cap.retry || 0)} 批`
            : `待重试 ${fmtNum(cap.retry || 0)} 批 · 无停摆`)
        : "—（后端未提供队列状态，不当作 0）" },
    { n: "门控" + (c.gate_shadow ? "（影子）" : ""), ok: !!c.gate_enabled, d: c.gate_shadow ? "只记录不拦截" : "低于阈值会沉默" },
    { n: "关系链", ok: !gstatsErr && (gstats.avg_degree || 0) > 0.3, d: chainSub },
    { n: "表达学习", ok: !!(vc.styles || vc.quotes), d: `${fmtNum(vc.styles || 0)} 种回复方式 · ${fmtNum(vc.quotes || 0)} 段原话` },
    { n: "主动消息", ok: !!c.proactive_enabled, d: c.proactive_enabled ? "已启用" : "未启用（默认关）" },
    { n: "好感结算", ok: !!c.favour_enabled, d: `跟踪 ${fmtNum(ov.favours)} 人` },
    // 门控流水读不到时也显式说出来，别让它静默变成「0 条流水」
    ...(gateErr ? [{ n: "门控流水", ok: false, d: `⚠ 读取失败：${gateErr}` }] : []),
  ];

  const html = `
  ${pageHead(
    "总览",
    "所有数字都来自插件自己的数据库（<span class='mono'>data/…/starheart.db</span>）",
    `<button class="btn s" onclick="hfRefreshOverview(this)">刷新</button>
     <button class="btn s" onclick="goPage('memory','graph')">看记忆图谱 →</button>`,
    "◉"
  )}
  <div class="grid g23 sec">
    ${card(`<div id="ovFlow"></div>`, {
      title: "它是怎么跑起来的",
      sub: "从左到右就是一条消息的旅程 · 数字是实时读数 · 悬停看上下游 · 点节点直接进那一页",
      actions: `<span class="mut" style="font-size:10.5px">光点=数据在往下一步走，越快说明量越大</span>`,
    })}
    ${card(
      `<div class="col" style="gap:7px">
        ${health
          .map(
            (h) => `<div class="row" style="gap:7px;align-items:flex-start">
          <span class="hlamp ${h.ok ? "g" : "y"}" style="margin-top:4px"></span>
          <div style="min-width:0">
            <b style="font-size:12px">${esc(h.n)}</b>
            <div class="mut" style="font-size:10.5px;line-height:1.5">${esc(h.d)}</div>
          </div></div>`
          )
          .join("")}
      </div>
      <div class="row bt wrap" style="gap:9px;margin-top:11px;padding-top:9px;border-top:1px solid var(--bd)">
        ${cap.parked ? `<button class="btn xs" onclick="hfCaptureRetry(this)">重试停摆队列（${fmtNum(cap.parked)}）</button>` : ""}
        <button class="btn xs" onclick="goPage('actor','review')">去人格与表达 →</button>
      </div>`,
      { title: "模块状态", sub: "按当前配置实时判定" }
    )}
  </div>
  <div class="grid g23 sec">
    ${card(`<div id="ovFunnel"></div>`, { title: "开口转化", sub: "评估 → 开口，逐层条形时不裁形状" })}
    ${card(`<div id="ovTypes"></div>`, { title: "记忆构成", sub: "按类型分布（当前查看视角）" })}
  </div>
  <div class="grid g23 sec">
    ${card(`<div id="ovFeed"></div>`, { title: "最近决策", sub: "倒序 · 点「决策流水」看完整因子分解", actions: `<button class="btn xs" onclick="goPage('messages','gate')">全部 →</button>` })}
    ${card(`<div id="ovVoice"></div>`, { title: "表达库构成", sub: "学到的说法按类型分；口头禅命中才进提示词，群内用语只用于听懂", actions: `<button class="btn xs" onclick="goPage('actor','voice')">打开 →</button>` })}
  </div>`;
  return {
    html,
    mount: (host) => {
      const byType = ov.memory_by_type || {};
      const on = (v) => (v ? "已开" : "未开");
      const flowNodes = [
        { id: "msg", stage: 0, label: "消息进来", icon: "✉", value: fmtNum(ov.gate_total), unit: "次评估", sub: "收到并被评估的消息", color: "#3c96ca", to: ["messages", "gate"] },
        { id: "gate", stage: 1, label: "门控决策", icon: "◐", value: fmtPct(ov.gate_rate, 1), unit: "开口率", sub: c.gate_shadow ? "影子模式：只记录不拦" : "低于阈值会沉默", color: "#03c9d7", to: ["messages", "gate"] },
        { id: "mem", stage: 2, label: "记忆库", icon: "◍", value: fmtNum(ov.memories), unit: "条", sub: `实体 ${fmtNum(ov.entities)} · 平均强度 ${fmt1(ov.memory_avg_strength)}`, color: "#4a90d9", to: ["memory", "graph"] },
        { id: "exp", stage: 2, label: "人格与表达", icon: "✎", value: fmtNum(vc.styles || 0), unit: "种说法", sub: `${fmtNum(vc.quotes || 0)} 段原话 · 下一轮按相关度挑一条`, color: "#8b5cf6", to: ["actor", "voice"] },
        { id: "link", stage: 3, label: "关系链", icon: "⬡", value: fmtNum(ov.edges), unit: "条边", sub: chainSub, color: "#00c853", to: ["memory", "graph"] },
        { id: "fav", stage: 3, label: "好感结算", icon: "♥", value: fmtNum(ov.favours), unit: "人", sub: "区间会变成语气描述", color: "#e0559a", to: ["messages", "favour"] },
        { id: "out", stage: 4, label: "回复输出", icon: "➤", value: on(c.output_enabled), unit: "", sub: "拟人化 / 分条 / 错别字", color: "#f59e0b", to: ["settings", "main"] },
        { id: "pro", stage: 4, label: "主动消息", icon: "☎", value: on(c.proactive_enabled), unit: "", sub: c.proactive_enabled ? "按护栏主动开口" : "默认关", color: "#9aa4b2", to: ["messages", "proactive"] },
      ];
      const flowLinks = [
        { from: "msg", to: "gate", value: ov.gate_total || 1 },
        { from: "gate", to: "mem", value: ov.memories || 1 },
        { from: "gate", to: "exp", value: vc.quotes || 1 },
        { from: "gate", to: "fav", value: ov.favours || 1 },
        { from: "mem", to: "link", value: ov.edges || 1 },
        { from: "mem", to: "pro", value: ov.memories || 1 },
        { from: "fav", to: "pro", value: ov.favours || 1 },
        { from: "gate", to: "out", value: ov.gate_total || 1 },
      ];
      mount(host, "#ovFlow", (el) =>
        C.flowGraph(el, {
          nodes: flowNodes,
          links: flowLinks,
          height: 300,
          onPick: (n) => {
            if (Array.isArray(n.to)) goPage(n.to[0], n.to[1]);
          },
        })
      );
      mount(host, "#ovFunnel", (el) =>
        C.stageBars(el, {
          stages: [
            { label: "评估过的消息", value: ov.gate_total || 0, color: "#3c96ca" },
            { label: "触发开口", value: ov.gate_reply || 0, color: "#03c9d7" },
          ],
          valueFmt: (v) => fmtNum(v),
          emptyText: "还没有门控评估记录",
        })
      );
      mount(host, "#ovTypes", (el) =>
        C.barSplit(el, {
          items: Object.keys(byType).map((k) => ({ label: ATOM_LABEL[k] || k, value: byType[k], color: ATOM_COLOR[k] })),
          height: 18,
        })
      );
      mount(host, "#ovVoice", (el) =>
        C.barSplit(el, {
          items: [
            { label: "说话方式", value: vc.styles || 0, color: "#8b5cf6" },
            { label: "原话", value: vc.quotes || 0, color: "#4a90d9" },
            { label: "口头禅", value: vc.terms || 0, color: "#00c853" },
            { label: "群内用语", value: vc.jargon || 0, color: "#f59e0b" },
          ],
          height: 18,
        })
      );
      mount(host, "#ovFeed", (el) =>
        C.timeline(el, {
          items: gateItems.slice(0, 8).map((g) => ({
            ts: g.ts,
            title: `${g.decision === "reply" ? "开口" : g.decision === "backoff" ? "退避" : "沉默"} · 分数 ${fmt1(g.score)} / 阈值 ${fmt1(g.threshold)}`,
            desc: `${(g.session_id || "未知会话").slice(0, 26)} · ${Object.keys(g.factors || {}).join(" / ") || "无因子"}`,
            color: g.decision === "reply" ? "#00c853" : "#9aa4b2",
          })),
          timeFmt: (ts) => fmtTime(ts),
        })
      );
      // 触发率与记忆数用滚动动画，让「刷新」有可见反馈
      countUp(host.querySelector("#ovGate"), ov.gate_total || 0);
    },
  };
}

export async function hfRefreshOverview(btn) {
  if (btn) btn.classList.add("loading");
  refreshCurrent();
  setTimeout(() => btn && btn.classList.remove("loading"), 400);
  toast("已刷新");
}

/** 把停摆（parked）的捕获批放回重试链——模型/网关修好后不用等下一轮 6h 回暖。 */
export async function hfCaptureRetry(btn) {
  if (btn) btn.classList.add("loading");
  const r = await apiPostSafe("capture/retry", {});
  if (btn) btn.classList.remove("loading");
  if (!r.ok) { toast("重试失败：" + r.error, "err"); return; }
  const n = (r.data && r.data.requeued) || 0;
  toast(n ? `已放回重试：${n} 批` : "没有需要重试的批", "ok");
  refreshCurrent();
}

/* ==================================================================
   语言学习
   ================================================================== */
let HB_FILTER = { q: "", kind: "all", state: "all", on: "all", sort: "new" };
let HB_EDIT = 0;     // 正在就地编辑哪一条（0 = 没有）
let HB_BANS = { items: [], total: 0 };
let HB_RUNS = { items: [], corpus: {}, health: {}, enum: [] };
/* —— ② 黑话（S5）：清单 / 命中日志 / 试解读；阈值与配额读后端真值（config/domains） —— */
let JG_FILTER = { q: "", status: "all" };
let JG_DATA = { items: [], counts: {}, config: {}, check: null };
let JG_EDIT = 0;                    // 正在改释义哪一条（0 = 没有）
const JG_HITS = {};                 // term_id → 命中日志（懒加载，每次整页重渲染后重新缓存）
const JG_STATUS = { pending: ["待确认", "warn"], confirmed: ["已确认", "ok"], denied: ["已否定", "gy"] };

/** 审核态归一（一律读后端的 checked_state / status，不在前端猜） */
function hbStateOf(h) {
  if (String(h.status || "active") === "archived") return "archived";
  const s = String(h.checked_state || "");
  return s === "user_reject" ? "rejected" : s === "user_ok" ? "ok" : s === "ai_pending" ? "pending" : "other";
}

/** 审核 tag（待审 / 已通过 / 已驳回 / 已清空） */
function hbStateTag(h) {
  const st = hbStateOf(h);
  if (st === "archived") return '<span class="tag gy" title="「只清表达」软删的行：移出「不再学」名单即可恢复">已清空</span>';
  if (st === "rejected") return '<span class="tag dg" title="驳回＝写进「不再学」名单，模型不会重新学（可恢复）">已驳回</span>';
  if (st === "ok") return '<span class="tag ok" title="已通过审核（默认自动通过）">已通过</span>';
  if (st === "pending") return '<span class="tag warn" title="等你看一眼：开着「先审后用」时它不会进注入池">待审</span>';
  return "";
}

/** 「当前人格可用」徽标：可用性只读后端求值（available_now / current_persona_ok），与注入侧同一判定 */
function hbAvailBadge(h) {
  if (h.current_persona_ok === true) {
    return '<span class="tag ok" title="当前人格可用 · 与注入侧同一判定（scope + 审核 + 冷却 + 24h 名额）">当前人格可用</span>';
  }
  const label = String(h.available_now || "");
  return label ? `<span class="chip gy" title="当前人格不可注入的原因（与注入侧同一判定）">✗ ${esc(label)}</span>` : "";
}

/** 菜单徽标：语言学习 →「待审 N」（N>0 才显示；窄轨下也可见 —— CSS 由本函数注入） */
function ensureHbBadgeCss() {
  if (typeof document === "undefined" || document.getElementById("hbBadgeCss")) return;
  const st = document.createElement("style");
  st.id = "hbBadgeCss";
  st.textContent =
    "#railNav .nav{position:relative}" +
    "#railNav .nav .hbpend{position:absolute;top:4px;right:7px;min-width:17px;height:17px;line-height:17px;" +
    "padding:0 5px;border-radius:999px;background:var(--gold,#c8a15a);color:#1a1a1a;font-size:10.5px;" +
    "font-weight:700;text-align:center;pointer-events:none}";
  document.head.appendChild(st);
}

/** 同步菜单徽标。传 n = 已知待审数（省一次请求）；不传就自己查一次。拿不到数字就不显示，绝不编数。 */
export async function syncExpressionBadge(n) {
  try {
    ensureHbBadgeCss();
    const nav = document.getElementById("railNav");
    if (!nav) return;
    const btn = nav.querySelector('[data-nav="actor"]');
    if (!btn) return;
    let num = n;
    if (num === undefined || num === null) {
      const r = await apiSafe("actor/overview", {}, { counts: {} });
      num = Number((((r || {}).counts) || {}).pending || 0);
    }
    num = Math.max(0, Number(num) || 0);
    let el = btn.querySelector(".hbpend");
    if (num <= 0) {                       // 默认自动通过 → 待审恒 0 → 徽标不显示
      if (el) el.remove();
      return;
    }
    if (!el) {
      el = document.createElement("span");
      el.className = "hbpend";
      btn.appendChild(el);
    }
    el.textContent = String(num);
    el.title = `待审 ${num} 条（表达 / 群内用语 / 人格提案）`;
  } catch (err) {
    /* 徽标拿不到就不显示 */
  }
}

/** 「不再学」名单（独立卡）：可查、可移出 */
function renderBansBlock() {
  const rows = HB_BANS.items.length
    ? HB_BANS.items
        .map(
          (b) => `<div class="card cp" style="padding:9px 12px">
        <div class="row bt" style="gap:9px">
          <div style="min-width:0">
            <div class="pn">${esc(String(b.key || b.norm_key || "—"))}</div>
            <div class="pd">理由：${esc(String(b.reason || "—"))} · ${esc(fmtTime(b.ts))} · 来源 ${esc(String(b.actor || "panel"))}${b.note ? "（" + esc(String(b.note)) + "）" : ""}</div>
          </div>
          <button class="btn xs" data-hbban="1" data-hbnk="${esc(String(b.norm_key || ""))}">移出名单</button>
        </div>
      </div>`
        )
        .join("")
    : emptyBlock("名单是空的 —— 驳回或删除的表达会出现在这里，模型不会把它们学回来", "✩");
  return `<div class="sec-t">「不再学」名单（${fmtNum(HB_BANS.total)}）</div>
    <div class="mut" style="font-size:11px;margin:-4px 0 8px">写进这里 = 提取路径遇到同一条说法直接跳过（修「删了还会再学」）。移出后可以重新学到，被清空的同键条目也会一并恢复。</div>
    <div class="col">${rows}</div>`;
}

/** ③ 学习诊断：每次提取的成败与跳过原因（S4-4）+ 语料存量 + 出处回查健康度（E3） */
function renderExprRunsBlock() {
  const enumList = HB_RUNS.enum || [];
  const reasonText = (v) => {
    const key = String(v || "");
    if (!key) return "—（有产出）";
    const e = enumList.find((x) => x.value === key);
    return e ? String(e.text) + (e.reserved ? "（reserved）" : "") : key;
  };
  const rows = (HB_RUNS.items || []).length
    ? HB_RUNS.items
        .map(
          (r) => `<tr>
        <td class="mono">${esc(fmtTime(r.ts))}</td>
        <td class="mono">${fmtNum(r.scanned)}</td>
        <td class="mono">${fmtNum(r.saved)}</td>
        <td>${r.skipped_reason ? `<span class="tag gy">${esc(String(r.skipped_reason))}</span>` : '<span class="tag ok">正常</span>'}
          <div class="mut" style="font-size:10.5px">${esc(reasonText(r.skipped_reason))}</div></td>
        <td class="mut">${esc(String(r.model || "—"))}</td>
        <td>${r.ok ? "✓" : "✗"}</td>
        <td class="mut">${esc(String(r.persona_id || "—"))}</td>
      </tr>`
        )
        .join("")
    : '<tr><td colspan="7" class="mut">还没有学习记录 —— 点右上角「立即学一批」跑一次就有了</td></tr>';
  const h = HB_RUNS.health || {};
  const healthText = h.value == null
    ? `样本不足（n=${fmtNum(h.n || 0)}）`
    : `${fmtPct(h.value)}（n=${fmtNum(h.n)}）`;
  const healthWhy = h.value == null && h.reason ? esc(String(h.reason)) : "目标 ≥95%（E3：出处可点到原话）";
  return card(
    `<div class="grid g3" style="gap:9px">
       <div class="kpi-mini"><div class="v">${fmtNum(HB_RUNS.corpus.unused)}</div><div class="l">可学 X 条未学</div></div>
       <div class="kpi-mini"><div class="v">${fmtNum(HB_RUNS.corpus.peer)}</div><div class="l">群友/好友语料（含已学）</div></div>
       <div class="kpi-mini"><div class="v">${healthText}</div><div class="l">出处可回查 · ${healthWhy}</div></div>
     </div>
     <div style="overflow-x:auto;margin-top:10px"><table><thead><tr>
       <th>时间</th><th>看了几条语料</th><th>学了几条</th><th>跳过原因</th><th>模型</th><th>成败</th><th>人格</th>
     </tr></thead><tbody>${rows}</tbody></table></div>
     <div class="mut" style="font-size:11px;margin-top:8px">学 0 条时**直接给原因**（不再只回一句「完成」）。原因枚举：${enumList
       .map((e) => `<span class="mono">${esc(String(e.value))}</span>${e.reserved ? "（reserved）" : ""}`)
       .join(" / ")}</div>`,
    { title: "学习诊断", sub: `最近 ${fmtNum((HB_RUNS.items || []).length)} 次提取 · 自动与手动两条路都记一行`, cls: "sec" }
  );
}

/** ② 黑话行：状态三态 / 出现与最近命中 / 未进入回复库标记 / 人工闸门 / 证据与命中日志 */
function jargonRow(it) {
  const st = String(it.status || "pending");
  const tag = JG_STATUS[st] || ["—", "gy"];
  const editing = JG_EDIT === Number(it.id);
  const meaning = String(it.meaning || "");
  return `<div class="card cp" style="padding:9px 12px">
    <div class="row bt" style="gap:9px">
      <div style="min-width:0;flex:1">
        <div class="row" style="gap:6px;flex-wrap:wrap;align-items:center">
          <span class="pn" style="font-size:13px">${esc(String(it.term || ""))}</span>
          <span class="tag ${tag[1]}" title="黑话状态三态（人工闸门）：待确认 / 已确认 / 已否定（否定 = 归档：不再推断、不再注入）">${esc(tag[0])}</span>
          <span class="chip" title="「出现」= 被观察到的命中次数（机械匹配日志聚合，不是「被再次抽取到」的次数）">出现 ${fmtNum(it.hit_count)}</span>
          <span class="chip" title="命中日志里出现过该词的会话数（跨 ≥2 会话是机械候选门槛之一）">${fmtNum(it.sessions)} 个会话</span>
          <span class="chip" title="最近一次被机械匹配到的时间">最近命中 ${it.last_hit_at ? esc(fmtTime(it.last_hit_at)) : "—"}</span>
          <span class="tag ${it.in_reply_lib ? "dg" : "ok"}" title="${it.in_reply_lib ? "该词出现在表达库里 —— 点「证据/命中」看漏在哪一条" : "唯一正向检查：黑话词没有进入表达库（只做理解、不进回复）"}">${it.in_reply_lib ? "✗ 漏进表达库" : "未进入回复库 ✓"}</span>
          ${it.enabled ? "" : '<span class="tag dg">已停用</span>'}
          <span class="chip gy" title="scope 取值域：全局 / 群·{gid} / 人·{uid}（过滤下推 SQL，不会串群串人）">${esc(String(it.scope || "全局"))}</span>
        </div>
        <div class="pd">释义：${meaning ? esc(meaning) : '<span class="mut">（待推断 / 待你补一句）</span>'}</div>
      </div>
      <div class="row" style="gap:5px;flex-shrink:0;flex-wrap:wrap;justify-content:flex-end">
        ${st === "pending" ? `<button class="btn xs p" data-jg="decide" data-id="${it.id}" data-act="confirm" title="确认：就按这条释义理解">确认</button>
          <button class="btn xs dg" data-jg="decide" data-id="${it.id}" data-act="deny" title="否定：判定不是黑话（归档；以后不再推断、不再注入）">否定</button>` : ""}
        ${st === "denied" ? `<button class="btn xs" data-jg="decide" data-id="${it.id}" data-act="confirm" title="改主意了：从「已否定」恢复为「已确认」">恢复</button>` : ""}
        <label class="sw" title="${it.enabled ? "停用（保留词条，只是不再参与理解注入）" : "启用"}"><input type="checkbox" data-jg="toggle" data-id="${it.id}" ${it.enabled ? "checked" : ""}><span class="tk"></span><span class="kb"></span></label>
        <button class="btn xs" data-jg="edit" data-id="${it.id}">${editing ? "取消" : "改"}</button>
        <button class="btn xs" data-jg="detail" data-id="${it.id}" title="证据长期保留（原话/时间/会话/说话人）+ 命中日志（哪一轮真的读到了它）">证据/命中</button>
        <button class="btn xs dg" data-jg="delete" data-id="${it.id}" title="真删（连命中日志）；只想归档请用「否定」">删</button>
      </div>
    </div>
    ${editing
      ? `<div class="hbedit">
           <input class="ipt" id="jgTerm_${it.id}" value="${esc(String(it.term || ""))}" placeholder="词条原文（≤40 字）">
           <input class="ipt" id="jgMean_${it.id}" value="${esc(meaning)}" placeholder="释义（≤300 字，只写它在群里的意思）">
           <div class="row" style="gap:6px;margin-top:6px"><button class="btn xs p" data-jg="save" data-id="${it.id}">保存</button>
             <span class="mut" style="font-size:10.5px">保存后下一轮理解就用新释义（不写任何回复）</span></div>
         </div>`
      : ""}
    <div id="jgx_${it.id}" style="display:none;margin-top:8px"></div>
  </div>`;
}

/** ② 黑话块（S5-1/S5-2）：清单 + 三件验证物（命中日志 / 未进入回复库检查行 / 试解读）+ 四洞控件 */
function renderJargonBlock() {
  const d = JG_DATA || {};
  const items = d.items || [];
  const c = d.config || {};
  const ck = d.check;
  const chips = [["all", "全部"], ["pending", "待确认"], ["confirmed", "已确认"], ["denied", "已否定"]]
    .map(([v, l]) => `<button class="hchip${JG_FILTER.status === v ? " on" : ""}" data-jg="fstatus" data-v="${v}">${l}</button>`)
    .join("");
  const shown = items
    .filter((it) => (JG_FILTER.status === "all" ? true : String(it.status) === JG_FILTER.status))
    .filter((it) => {
      const q = JG_FILTER.q.trim();
      if (!q) return true;
      return (String(it.term || "") + String(it.meaning || "")).includes(q);
    });
  const viol = (ck && Array.isArray(ck.violations)) ? ck.violations : [];
  const ckLine = ck == null
    ? '<span class="tag gy">检查行未取到</span><span class="mut" style="font-size:11px">jargon/check 读取失败 —— 不假装 ✓，刷新重试</span>'
    : ck.ok === true
      ? `<span class="tag ok">未进入回复库 ✓</span><span class="mut" style="font-size:11px">检查了 ${fmtNum(ck.checked)} 个黑话词：没有一个出现在表达库（唯一正向检查）</span>`
      : `<span class="tag dg">✗ 有黑话词漏进表达库</span><span class="mut" style="font-size:11px">${viol.slice(0, 3).map((v) => `「${esc(String(v.term || ""))}」出现在表达 #${fmtNum(v.expr_id)}`.replace(/</g, "&lt;")).join("；")}${viol.length > 3 ? ` 等 ${fmtNum(viol.length)} 处` : ""} —— 去①把含该词的说法改掉</span>`;
  return `<div class="sec" id="jgBlock">
    <div class="sec-t">② 黑话（听懂别人的词 · 只做理解，不进回复库）</div>
    <div class="card sec" style="padding:10px 13px">
      <div class="row bt wrap" style="gap:10px">
        <div class="row wrap" style="gap:6px;align-items:center">
          ${chips}
          <span class="mut" style="margin:0 4px">·</span>
          <input class="inp" id="jgQ" placeholder="搜词条 / 释义…" value="${esc(JG_FILTER.q)}" style="width:170px">
          <button class="btn xs" data-jg="search">搜索</button>
          ${JG_FILTER.q ? '<button class="btn xs" data-jg="qclear">清除</button>' : ""}
        </div>
        <div class="row wrap" style="gap:6px;align-items:center">
          <span class="mut" style="font-size:11px" title="洞② 人名过滤：开=候选挖掘时滤掉已知昵称/群名">人名过滤</span>
          <label class="sw"><input type="checkbox" data-jg="namefilter" ${c.jargon_name_filter ? "checked" : ""}><span class="tk"></span><span class="kb"></span></label>
          <span class="chip gy" title="机械候选门槛（拍-16；参数中心可调）">跨 ≥${fmtNum(c.candidate_min_sessions)} 会话 · 出现 ≥${fmtNum(c.candidate_min_hits)} 次</span>
          <span class="chip gy" title="含义推断每日批数上限（拍-16；0 = 关自动推断）">推断 ≤${fmtNum(c.infer_daily_batches)} 批/日</span>
          <span class="chip gy" title="试解读每日配额（拍-19；单次 ≤1 条短请求、输出 ≤200 tokens）">试解读 ${fmtNum(c.try_used_today)}/${fmtNum(c.try_daily_quota)}（今日）</span>
          <button class="btn xs" onclick="hfGoParams('expression')" title="阈值 / 配额 / 开关都在「设置 · 模块与参数」语言学习域（唯一编辑地）">去参数中心 →</button>
        </div>
      </div>
      <div class="row bt wrap" style="gap:8px;margin-top:8px;padding-top:8px;border-top:1px solid var(--bd)">
        <div style="min-width:0;flex:1">
          <div class="row" style="gap:6px;flex-wrap:wrap;align-items:center">${ckLine}</div>
          <div class="row wrap" style="gap:6px;margin-top:7px">
            <input class="inp" id="jgTryText" placeholder="试解读：打一句话，看它读懂什么（只理解、不产生回复）" style="flex:1;min-width:220px">
            <button class="btn p" data-jg="try">试解读</button>
          </div>
          <div id="jgTryOut" class="mut" style="font-size:11.5px;margin-top:6px;line-height:1.7">试解读默认只手动触发；模型走「表达提取」离线槽位（单次 ≤1 条短请求）；结果不进回复库、不产生任何回复。</div>
        </div>
      </div>
      <div class="mut" style="font-size:10.5px;margin-top:7px;line-height:1.7">
        「出现」= 被观察到的命中次数（机械匹配日志聚合）；命中日志与证据在每行「证据/命中」里。
        黑话与表达**不共享动作语义**：黑话审「对不对」（否定=归档），表达审「像不像」（驳回=降权进「不再学」）。
      </div>
    </div>
    <div class="col">${shown.length ? shown.map(jargonRow).join("") : emptyBlock(items.length ? "当前筛选下没有条目" : "还没有黑话词 —— 机械候选攒够门槛后由「黑话推断」每天自动过一批；结果先待确认，由你确认或否定", "☷")}</div>
  </div>`;
}

/** 证据 / 命中日志（懒加载，直接填行内容器，不整页重渲染） */
async function jgToggleDetail(id) {
  const box = document.getElementById("jgx_" + id);
  if (!box) return;
  if (box.style.display !== "none") {
    box.style.display = "none";
    return;
  }
  box.style.display = "block";
  box.innerHTML = '<div class="mut" style="font-size:11px">读命中日志…</div>';
  const it = (JG_DATA.items || []).find((x) => Number(x.id) === Number(id)) || {};
  let hits = JG_HITS[id];
  if (!hits) {
    const r = await apiSafe("jargon/hits", { term_id: id, limit: 20 }, { items: [], __err: "读取失败" });
    hits = r.items || [];
    if (!r.__err) JG_HITS[id] = hits;
  }
  const ev = Array.isArray(it.evidence) ? it.evidence : [];
  const evRows = ev.length
    ? ev.map((e) => `<div class="chip" style="white-space:pre-wrap;width:100%;line-height:1.7">「${esc(String(e.text || ""))}」 · ${esc(String(e.who || "?"))} · ${esc(fmtTime(e.ts))} · ${esc(String(e.session || "").slice(0, 30))}</div>`).join("")
    : '<div class="mut" style="font-size:11px">这条没有证据快照（旧数据，或由人工改写过）</div>';
  const hitRows = hits.length
    ? hits.map((h) => `<tr><td class="mono">${esc(fmtTime(h.ts))}</td><td class="mono">${esc(String(h.session_id || "").slice(0, 26))}</td>
        <td>${esc(String(h.msg || "").slice(0, 60))}</td><td>${h.injected ? "✓ 进了本轮理解" : "仅命中"}</td></tr>`).join("")
    : '<tr><td colspan="4" class="mut">还没有命中记录（等群里出现这个词就会记）</td></tr>';
  box.innerHTML = `
    <div class="pd" style="font-weight:600">证据（长期保留 —— 原话 / 时间 / 会话 / 说话人）</div>
    <div class="col" style="gap:4px">${evRows}</div>
    <div class="pd" style="font-weight:600;margin-top:9px">命中日志（最近 ${fmtNum(hits.length)} 条）</div>
    <div style="overflow-x:auto"><table><thead><tr><th>时间</th><th>会话</th><th>原话</th><th>理解注入</th></tr></thead><tbody>${hitRows}</tbody></table></div>`;
}

async function jgDecide(id, act) {
  const r = await apiPostSafe("jargon/decide", { id, action: act });
  if (!r.ok) { toast("操作失败：" + r.error, "err"); return; }
  toast(act === "confirm" ? "已确认（按这条释义理解）" : "已否定：归档，不再推断、不再注入", "ok");
  refreshCurrent();
}

async function jgSave(id) {
  const t = document.getElementById("jgTerm_" + id);
  const m = document.getElementById("jgMean_" + id);
  const r = await apiPostSafe("jargon/save", { id, term: t ? String(t.value || "") : "", meaning: m ? String(m.value || "") : "" });
  if (!r.ok) { toast("保存失败：" + r.error, "err"); return; }
  JG_EDIT = 0;
  toast("已保存", "ok");
  refreshCurrent();
}

async function jgToggleEnabled(id, on) {
  const it = (JG_DATA.items || []).find((x) => Number(x.id) === Number(id)) || {};
  const r = await apiPostSafe("jargon/save", { id, term: String(it.term || ""), meaning: String(it.meaning || ""), enabled: !!on });
  if (!r.ok) { toast("操作失败：" + r.error, "err"); refreshCurrent(); return; }
  toast(on ? "已启用" : "已停用（词条保留）", "ok");
  refreshCurrent();
}

async function jgSetNameFilter(on) {
  const r = await apiPostSafe("config/save", { patch: { jargon_name_filter: !!on } });
  if (!r.ok) { toast("保存失败：" + r.error, "err"); refreshCurrent(); return; }
  toast(on ? "人名过滤已开（候选里不再出现昵称/群名）" : "人名过滤已关", "ok");
  refreshCurrent();
}

function jgDelete(id) {
  const it = (JG_DATA.items || []).find((x) => Number(x.id) === Number(id)) || {};
  confirmModal(
    "删除这条黑话？",
    `「${String(it.term || "")}」的词条与它的命中日志都会删掉。只想归档（不再推断、不再注入）请用「否定」。`,
    async () => {
      const r = await apiPostSafe("jargon/delete", { id });
      if (!r.ok) { toast("删除失败：" + r.error, "err"); return; }
      toast("已删除", "ok");
      refreshCurrent();
    },
    "删除"
  );
}

async function jgTry(btn) {
  const inp = document.getElementById("jgTryText");
  const out = document.getElementById("jgTryOut");
  const text = inp ? String(inp.value || "").trim() : "";
  if (!text) { toast("先打一句话再试解读", "err"); return; }
  if (btn) { btn.classList.add("loading"); btn.disabled = true; }
  const r = await apiPostSafe("jargon/try", { text });
  if (btn) { btn.classList.remove("loading"); btn.disabled = false; }
  if (!out) return;
  if (!r.ok) {
    out.innerHTML = `<span style="color:var(--error,#e05555)">试解读失败：${esc(String(r.error))}</span>`;
    return;
  }
  const d = r.data || {};
  if (d.ok === false) {
    out.innerHTML = `<span class="tag warn">配额</span> ${esc(String(d.error || "今日配额已用完"))}`;
    return;
  }
  const matched = (d.matched || [])
    .map((x) => `<span class="chip">${esc(String(x.term || ""))}${x.meaning ? "：" + esc(String(x.meaning).slice(0, 40)) : ""}</span>`)
    .join(" ");
  out.innerHTML = `<div class="row" style="gap:6px;flex-wrap:wrap">
      <span class="tag ok">reply_side=${esc(String(d.reply_side || "blocked"))}</span>
      <span class="mut" style="font-size:10.5px">只理解、不产生任何回复 · 今日已用 ${fmtNum((d.quota || {}).used)}/${fmtNum((d.quota || {}).limit)}</span>
    </div>
    <div style="margin-top:5px">${esc(String(d.reading || "")) || "（模型没给出解读）"}</div>
    ${matched ? `<div class="row" style="gap:5px;flex-wrap:wrap;margin-top:5px">命中的词条：${matched}</div>` : ""}`;
}

export async function _renderExpressionLibOld() {
  const [res, log, dom, rev, bans, runs, jg, jgck] = await Promise.all([
    apiSafe("habits", {}, { items: [], counts: {}, banned: [] }),
    apiSafe("habits/log", {}, { items: [], events: [], corpus: {}, runs: [] }),
    apiSafe("config/domains", {}, { domains: {}, values: {} }),
    apiSafe("expr/review", {}, { items: [], counts: {} }),
    apiSafe("expr/bans", {}, { items: [], total: 0 }),
    apiSafe("expr/runs", { limit: 20 }, { items: [], corpus: {}, health: {}, skipped_reason_enum: [] }),
    // S5 黑话块②：清单（含命中聚合/未进回复库标记）+「未进入回复库」检查行
    apiSafe("jargon", {}, { items: [], counts: {}, config: {} }),
    apiSafe("jargon/check", {}, { ok: null, violations: [], checked: 0 }),
  ]);
  const items = res.items || [];
  STATE.habits = items;
  HB_BANS = { items: bans.items || [], total: Number(bans.total || (bans.items || []).length || 0) };
  HB_RUNS = {
    items: runs.items || [],
    corpus: runs.corpus || {},
    health: runs.health || {},
    enum: runs.skipped_reason_enum || [],
  };
  // 黑话（块②）：check 读取失败时保留 null（渲染层显式报「未取到」，不许假装 ✓）
  JG_DATA = {
    items: jg.items || [],
    counts: jg.counts || {},
    config: jg.config || {},
    check: (jgck && !jgck.__err) ? jgck : null,
  };
  const corpus = runs.corpus || log.corpus || {};
  const values = dom.values || {};
  const rc = rev.counts || {};
  const cc = res.counts || {};
  const pending = Number(rc.pending == null ? cc.pending || 0 : rc.pending);
  const bannedN = Number(rc.banned == null ? HB_BANS.total : rc.banned);
  const reviewFirst = res.review_first === true || !!values.expression_review_first;
  const availN = items.filter((h) => h.current_persona_ok === true).length;
  const usesOf = (h) => Number(h.use_count == null ? h.uses || 0 : h.use_count);
  const maxUses = Math.max(1, ...items.map(usesOf));
  syncExpressionBadge(pending);

  /** 来源判定（以后端写入值为准）：scope「学习·xxx」/「学习」/「全局」= 学来的；「手加·xxx」/「手加」= 我手加 */
  const kindOf = (h) => {
    const s = String(h.scope || "");
    return /^手加/.test(s) ? "manual" : /^学习/.test(s) || s === "全局" ? "learned" : "manual";
  };
  const lastUsed = (h) => Number(h.updated_at || 0);

  const shown = items
    .filter((h) => (HB_FILTER.kind === "all" ? true : kindOf(h) === HB_FILTER.kind))
    .filter((h) => (HB_FILTER.state === "all" ? true : hbStateOf(h) === HB_FILTER.state))
    .filter((h) => (HB_FILTER.on === "all" ? true : HB_FILTER.on === "on" ? !!h.enabled : !h.enabled))
    .filter((h) => {
      const q = HB_FILTER.q.trim();
      if (!q) return true;
      const ev = (h.evidence || []).map((e) => String(e.text || "")).join(" ");
      return (String(h.scene) + String(h.way) + (h.cues || []).join(" ") + ev).includes(q);
    })
    // 默认「新学置顶」= 按入库顺序（自增 id）倒序 —— 最新学到的排最前，不然新学的沉底看不见
    .sort((a, b) =>
      HB_FILTER.sort === "recent" ? lastUsed(b) - lastUsed(a)
        : HB_FILTER.sort === "new" ? Number(b.id || 0) - Number(a.id || 0)
          : usesOf(b) - usesOf(a));

  const chip = (label, active, onclick) => `<button class="hchip${active ? " on" : ""}" onclick="${onclick}">${esc(label)}</button>`;
  const conclusion =
    `共 ${fmtNum(items.length)} 条表达 · 当前人格可用 ${fmtNum(availN)} 条 · 待审 ${fmtNum(pending)} 条 · 「不再学」${fmtNum(bannedN)} 条 · `
    + (reviewFirst
      ? "「先审后用」已开：只有「已通过」进注入池（在参数中心关掉它／通过几条即可）"
      : "审核默认自动通过：学到就能用；驳回或删除会写进「不再学」名单");

  const html = `
  ${pageHead(
    "语言学习",
    "机器人从群友/好友的原话里学到的「什么时候说 → 具体怎么说」。每条都能核对出处（对到原话与消息 id）、改说法、通过或驳回（驳回＝写进「不再学」名单，不会重新学）",
    `<button class="btn p" onclick="hfExtractHabits(this)">立即学一批</button>
     <button class="btn" onclick="hfHabitAdd()">+ 手加一条</button>
     <button class="btn xs" data-hbrev="approve-all" title="把所有「待审」一次通过">待审全通过</button>`,
    "✎",
    conclusion
  )}
  ${kpis(
    kpiCard("hbAll", fmtNum(items.length), "表达条目")
    + kpiCard("hbOn", fmtNum(items.filter((h) => h.enabled).length), "启用中")
    + kpiCard("hbUses", fmtNum(items.reduce((s, h) => s + usesOf(h), 0)), "出站命中次数", '<span class="mut">真用过才算</span>')
    + kpiCard("hbPend", fmtNum(pending), "待审", reviewFirst ? '<span class="mut">先审后用已开</span>' : '<span class="mut">自动通过</span>')
    + kpiCard("hbPeer", fmtNum(corpus.peer), "可学素材", '<span class="mut">群友/好友原话</span>')
  )}
  <div class="card sec" style="padding:10px 13px">
    <div class="row bt wrap" style="gap:10px">
      <div class="row wrap" style="gap:6px">
        ${chip("全部状态", HB_FILTER.state === "all", "hfHbFilter('state','all')")}
        ${chip("待审", HB_FILTER.state === "pending", "hfHbFilter('state','pending')")}
        ${chip("已通过", HB_FILTER.state === "ok", "hfHbFilter('state','ok')")}
        ${chip("已驳回", HB_FILTER.state === "rejected", "hfHbFilter('state','rejected')")}
        ${chip("不再学", HB_FILTER.state === "banned", "hfHbFilter('state','banned')")}
        <span class="mut" style="margin:0 4px">·</span>
        ${chip("全部来源", HB_FILTER.kind === "all", "hfHbFilter('kind','all')")}
        ${chip("学来的", HB_FILTER.kind === "learned", "hfHbFilter('kind','learned')")}
        ${chip("我手加", HB_FILTER.kind === "manual", "hfHbFilter('kind','manual')")}
        <span class="mut" style="margin:0 4px">·</span>
        ${chip("不限启用", HB_FILTER.on === "all", "hfHbFilter('on','all')")}
        ${chip("只看启用", HB_FILTER.on === "on", "hfHbFilter('on','on')")}
        ${chip("只看停用", HB_FILTER.on === "off", "hfHbFilter('on','off')")}
        <span class="mut" style="margin:0 4px">·</span>
        ${chip("新学置顶", HB_FILTER.sort === "new", "hfHbFilter('sort','new')")}
        ${chip("按使用", HB_FILTER.sort === "uses", "hfHbFilter('sort','uses')")}
        ${chip("按最近更新", HB_FILTER.sort === "recent", "hfHbFilter('sort','recent')")}
      </div>
      <span class="searchbox" style="width:200px"><i class="ic">⌕</i><input id="hbQ2" placeholder="搜场景 / 说法 / 原话…" value="${esc(HB_FILTER.q)}" oninput="hfHbFilter('q', this.value)"></span>
    </div>
    <div class="mut" style="font-size:11px;margin-top:7px">
      共 ${fmtNum(shown.length)} 条 · 色条长度 = 出站命中次数（越常用越长）· 「新学置顶」= 按入库顺序（默认）
      ${items.filter((h) => !(h.cues || []).length).length ? ` · <b>${items.filter((h) => !(h.cues || []).length).length} 条还没记出处</b>（旧数据；再学一批就有了）` : ""}
    </div>
  </div>
  ${
    HB_FILTER.state === "banned"
      ? renderBansBlock()
      : `<div class="hbgrid sec">
    ${
      shown.length
        ? shown
            .map((h) => {
              const cues = Array.isArray(h.cues) ? h.cues : [];
              const evidence = Array.isArray(h.evidence) ? h.evidence : [];
              const learned = kindOf(h) === "learned";
              const heat = Math.max(6, Math.round((usesOf(h) / maxUses) * 100));
              const editing = HB_EDIT === h.id;
              const st = hbStateOf(h);
              const cool = h.cooldown_active ? ` · 冷却中（到 ${esc(fmtTime(h.cooldown_until))} 前不再注入）` : "";
              return `<div class="hbcard${h.enabled ? "" : " off"}" data-hb="${esc(String(h.scene + h.way + cues.join(" ")))}">
        <div class="hbheat" style="width:${heat}%"></div>
        <div class="row bt" style="gap:8px;position:relative">
          <div class="row" style="gap:6px;flex-wrap:wrap;min-width:0">
            <span class="tag info">${esc(h.scene)}</span>
            <span class="tag ${learned ? "gy" : "ok"}" title="${learned ? "学来的 —— 模型从原话里推断" : "我手加 —— 手动添加的声明式习惯"} · 后端来源标记 scope=${esc(String(h.scope || "—"))}">${learned ? "学来的" : "我手加"}</span>
            ${hbStateTag(h)}
            ${hbAvailBadge(h)}
            <span class="chip">用到 ${fmtNum(usesOf(h))} 次</span>
            ${h.used_24h ? `<span class="chip">24h 命中 ${fmtNum(h.used_24h)}</span>` : ""}
            ${h.enabled ? "" : '<span class="tag dg">已停用</span>'}
          </div>
          <div class="row" style="gap:5px;flex-shrink:0">
            ${
              st === "pending"
                ? `<button class="btn xs p" data-hbrev="approve" data-hbid="${h.id}" title="通过：立刻可用">通过</button>
                   <button class="btn xs dg" data-hbrev="reject" data-hbid="${h.id}" title="驳回：写进「不再学」名单">驳回</button>`
                : ""
            }
            <label class="sw" title="${h.enabled ? "停用（保留条目，只是不再参考）" : "启用"}"><input type="checkbox" ${h.enabled ? "checked" : ""} onchange="hfHabitToggle(${h.id}, this)"><span class="tk"></span><span class="kb"></span></label>
            <button class="btn xs" onclick="hfHabitEdit(${h.id})">${editing ? "取消" : "改说法"}</button>
            <button class="btn xs dg" onclick="hfHabitDelete(${h.id})">删</button>
          </div>
        </div>
        ${
          editing
            ? `<div class="hbedit">
                 <input class="ipt" id="hbScene_${h.id}" value="${esc(h.scene)}" placeholder="什么时候用（社交情境）">
                 <input class="ipt" id="hbWay_${h.id}" value="${esc(h.way)}" placeholder="具体怎么说">
                 <div class="row" style="gap:6px;margin-top:6px"><button class="btn xs p" onclick="hfHabitSaveEdit(${h.id})">保存</button>
                   <span class="mut" style="font-size:10.5px">保存后下一轮对话就用新说法（立即生效）</span></div>
               </div>`
            : `<div class="hway">"${esc(String(h.way))}"</div>`
        }
        <div class="row" style="gap:7px;margin-top:6px;flex-wrap:wrap;align-items:center">
          ${
            evidence.length || cues.length
              ? `<button class="btn xs" onclick="hfHabitCues(this, ${h.id})" data-n="${evidence.length || cues.length}">出处（${evidence.length || cues.length}）</button>`
              : '<span class="chip gy" title="这条是旧数据，提取时没记录来源">没记出处</span>'
          }
          <span class="mut" style="font-size:10.5px">最近更新 ${fmtTime(h.updated_at)}${esc(cool)}</span>
        </div>
        <div class="hcues" id="hcues_${h.id}" style="display:none"></div>
      </div>`;
            })
            .join("")
        : emptyBlock(
            items.length
              ? "当前筛选下没有条目 —— 换个筛选条件试试"
              : "还没有学到表达：点右上角「立即学一批」，或先让机器人多和人聊聊（也可以「+ 手加一条」）",
            "✎"
          )
    }
  </div>`
  }
  ${renderJargonBlock()}
  ${renderExprRunsBlock()}
  ${foldCard("foldConf", "学习开关与参数（只读摘要）", `${values.expression_enabled ? "语言学习：开" : "语言学习：关"} · 注入名额 ${cfgTxt(values.output_expr_max_inject, " 条")}`, `
    <div class="kv">语言学习总开关<span>${values.expression_enabled ? "开" : "关"}</span></div>
    <div class="kv">先审后用<span>${reviewFirst ? "开（只有「已通过」进注入池）" : "关（默认：学到就能用）"}</span></div>
    <div class="kv">已验证门限<span class="mono">${cfgTxt(values.expression_min_uses, " 次")}</span></div>
    <div class="kv">每轮注入名额<span class="mono">${cfgTxt(values.output_expr_max_inject, " 条")}</span></div>
    <div class="kv">自动提取门槛<span class="mono">${cfgTxt(values.expression_auto_min_corpus, " 条")}</span></div>
    <div class="kv">模型分配<span>提取只走「表达提取」一个模型，没有二次审校 · <button class="btn xs" onclick="goPage('settings','models')">去设置</button></span></div>
    <div class="row wrap" style="gap:8px;margin-top:10px">
      <button class="btn xs p" onclick="hfGoParams('expression')" title="去「设置 · 模块与参数」语言学习域，带域锚点">去参数中心 →</button>
      <span class="mut" style="font-size:10.5px">开关与参数都在「设置 · 模块与参数」的语言学习域里改（本页只读展示，参数只有一个编辑地）</span>
    </div>`, false)}`;
  return {
    html,
    mount: (host) => {
      // 审核 / 名单动作走 data 属性 + 监听器：内联 onclick 的 handler 必须挂在 window 上，
      // 而本页新函数不在 app.js 的 bindActions 清单里 —— 这里不挂 window、也不用内联 onclick。
      host.querySelectorAll("[data-hbrev]").forEach((b) => {
        b.addEventListener("click", () => {
          const act = String(b.dataset.hbrev || "");
          if (act === "approve-all") {
            hfExprApproveAll();
            return;
          }
          hfExprReview(Number(b.dataset.hbid || 0), act);
        });
      });
      host.querySelectorAll("[data-hbban]").forEach((b) => {
        b.addEventListener("click", () => hfExprBanRemove(String(b.dataset.hbnk || "")));
      });
      // —— ② 黑话块（S5）：行内动作走 data 属性 + 监听器（同审核按钮，不挂 window、不用内联 onclick） ——
      host.querySelectorAll("#jgBlock [data-jg]").forEach((el) => {
        const act = String(el.dataset.jg || "");
        const jid = Number(el.dataset.id || 0);
        if (act === "decide") el.addEventListener("click", () => jgDecide(jid, String(el.dataset.act || "")));
        else if (act === "edit") el.addEventListener("click", () => { JG_EDIT = JG_EDIT === jid ? 0 : jid; refreshCurrent(); });
        else if (act === "save") el.addEventListener("click", () => jgSave(jid));
        else if (act === "delete") el.addEventListener("click", () => jgDelete(jid));
        else if (act === "detail") el.addEventListener("click", () => jgToggleDetail(jid));
        else if (act === "try") el.addEventListener("click", () => jgTry(el));
        else if (act === "search") el.addEventListener("click", () => {
          const inp2 = host.querySelector("#jgQ");
          JG_FILTER.q = inp2 ? String(inp2.value || "").trim() : "";
          refreshCurrent();
        });
        else if (act === "qclear") el.addEventListener("click", () => { JG_FILTER.q = ""; refreshCurrent(); });
        else if (act === "fstatus") el.addEventListener("click", () => { JG_FILTER.status = String(el.dataset.v || "all"); JG_EDIT = 0; refreshCurrent(); });
        else if (act === "namefilter") el.addEventListener("change", () => jgSetNameFilter(el.checked));
        else if (act === "toggle") el.addEventListener("change", () => jgToggleEnabled(jid, el.checked));
      });
      const jgq = host.querySelector("#jgQ");
      if (jgq) jgq.addEventListener("keydown", (e) => {
        if (e.key === "Enter") { JG_FILTER.q = String(jgq.value || "").trim(); refreshCurrent(); }
      });
      const q = host.querySelector("#hbQ2");
      if (!q) return;
      // 同记忆库搜索框：每次重渲染都是新 input，组字监听在这里重绑。
      // refreshCurrent() 会把整页 DOM 换掉，组字期间触发 = 候选框消失、中文打不出来。
      _hbQcomposing = false;            // 新元素不可能还在组字：上一轮万一没收尾，旗标必须归零
      q.addEventListener("compositionstart", () => {
        _hbQcomposing = true;
        if (_hbQtimer) { clearTimeout(_hbQtimer); _hbQtimer = null; }
      });
      q.addEventListener("compositionend", () => {
        _hbQcomposing = false;
        _hbQcaret = q.selectionStart;
        _hbQSchedule();
      });
      q.addEventListener("blur", () => { _hbQcomposing = false; });     // 兜底，别让旗标卡住
      if (!HB_FILTER.q) return;
      q.focus();
      try {
        const pos = _hbQcaret == null ? q.value.length : Math.min(_hbQcaret, q.value.length);
        q.setSelectionRange(pos, pos);
      } catch (err) { /* 光标定位失败无所谓 */ }
      _hbQcaret = null;
    },
  };
}

/** 折叠卡（默认收起，标题右侧一个按钮） */
function foldCard(id, title, sub, inner, open) {
  return `<div class="card sec" style="padding:0">
    <div class="row bt" style="gap:8px;padding:11px 13px">
      <div style="min-width:0"><div class="pn">${esc(title)}</div><div class="pd">${sub}</div></div>
      <button class="btn xs" onclick="hfFold(this, '${id}')">${open ? "收起" : "展开"}</button>
    </div>
    <div id="${id}" style="display:${open ? "block" : "none"};padding:0 13px 13px">${inner}</div>
  </div>`;
}

/** 配置值展示：拿不到就写「—」，绝不拿 0 顶替（零假数据红线） */
function cfgTxt(v, unit) {
  if (v === undefined || v === null || v === "") return "—";
  return `${esc(String(v))}${unit || ""}`;
}

function renderLogSection(events, items) {
  const full = HF_LOG_MODE === "full";
  const rows = events.length
    ? events
        .map((e) => {
          const act = String(e.action || "");
          const kind = act.includes("extract") ? "提取" : act.includes("delete") ? "删除" : act.includes("save") ? "修改" : act.replace(/^habit[._]?/, "") || "事件";
          const cls = act.includes("delete") ? "dg" : act.includes("extract") ? "info" : "gy";
          return `<div class="card cp" style="padding:9px 12px">
        <div class="row bt" style="gap:9px">
          <div class="row" style="gap:8px;min-width:0">
            <span class="tag ${cls}">${esc(kind)}</span>
            <span class="mut mono" style="font-size:11px">${fmtTime(e.ts)}</span>
            <span class="mut" style="font-size:11.5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(String(e.target || "").slice(0, 60))}</span>
          </div>
          ${full ? `<button class="btn xs" onclick="hfLogToggle(${e.id})">展开</button>` : ""}
        </div>
        ${full
          ? `<div id="logd_${e.id}" style="display:none;margin-top:8px">
               <div class="mut" style="font-size:10.5px;margin-bottom:4px">完整记录（审计原文）</div>
               <div class="chip" style="white-space:pre-wrap;line-height:1.7;width:100%;max-height:240px;overflow:auto">${esc(String(e.detail || "—"))}</div>
             </div>`
          : `<div class="mut" style="font-size:11px;margin-top:4px">${esc(String(e.detail || "").slice(0, 90) || "—")}</div>`}
      </div>`;
        })
        .join("")
    : emptyBlock("还没有学习记录 —— 点右上角「立即学一批」跑一次就有了", "☰");
  return `<div class="row bt" style="gap:8px;margin-bottom:8px">
      <div class="mut" style="font-size:11px">${full ? "详细：每批都能展开看模型完整输出" : "简洁：只看每一批发生了什么"}</div>
      <button class="btn xs" onclick="hfLogMode('${full ? "brief" : "full"}')">${full ? "切到简洁" : "切到详细"}</button>
    </div>${rows}`;
}

function renderCorpusSection(corpus) {
  return `<div class="grid g3" style="gap:9px">
      <div class="kpi-mini"><div class="v">${fmtNum(corpus.peer)}</div><div class="l">可学素材（群友/好友原话）</div></div>
      <div class="kpi-mini"><div class="v">${fmtNum(corpus.bot)}</div><div class="l">机器人自己的发言（不计入素材）</div></div>
      <div class="kpi-mini"><div class="v">${fmtNum(corpus.unknown)}</div><div class="l">未判定来源</div></div>
    </div>
    <div class="mut" style="font-size:11px;margin-top:8px">学习链路：抓原话 → 让「表达提取」模型观察说话套路 → 去重后进上面的列表（全程只有这一次模型调用，没有二次审校）。只从别人的原话里学，不从机器人自己身上学。</div>`;
}

/** 筛选 / 排序（就地重渲染，不跳页） */
/** 搜索框重渲染后要恢复的光标位置（null = 没记录，落到末尾） */
let _hbQcaret = null;
let _hbQtimer = null;
/** 输入法正在组字（拼音还没上屏）。组字期间不排去抖，否则重渲染会把输入框换掉。 */
let _hbQcomposing = false;

/** 排一次「去抖 → 重查」。组字中直接跳过，等 compositionend 再排。 */
function _hbQSchedule() {
  if (_hbQtimer) clearTimeout(_hbQtimer);
  if (_hbQcomposing) { _hbQtimer = null; return; }
  _hbQtimer = setTimeout(() => {
    _hbQtimer = null;
    // 上屏与 input 事件的先后各浏览器不一致，落地前拿输入框现值兜一次
    const live = document.getElementById("hbQ2");
    if (live && String(live.value) !== HB_FILTER.q) HB_FILTER.q = String(live.value);
    refreshCurrent();
  }, 300);
}

export function hfHbFilter(key, value) {
  if (key === "q") HB_FILTER.q = String(value || "");
  else HB_FILTER[key] = value;
  if (key !== "q") {
    refreshCurrent();
    return;
  }
  // 同记忆库：搜索框去抖 300ms + 记住光标，别每敲一键就重渲染整页；
  // 中文/日文输入法组字期间连着触发的 input 一律不排去抖（见 _hbQSchedule），
  // 否则组字中途重渲染会把输入框和候选框一起换掉，字打不出来。
  const el = document.getElementById("hbQ2");
  _hbQcaret = el ? el.selectionStart : null;
  _hbQSchedule();
}

/** 折叠区开合 */
export function hfFold(btn, id) {
  const box = document.getElementById(id);
  if (!box) return;
  const open = box.style.display !== "none";
  box.style.display = open ? "none" : "block";
  if (btn) btn.textContent = open ? "展开" : "收起";
}

/** 就地编辑：点「改说法」进入输入态 */
export function hfHabitEdit(id) {
  HB_EDIT = HB_EDIT === id ? 0 : id;
  refreshCurrent();
}

/** 保存就地编辑（立即生效：下一轮对话就用新说法） */
export async function hfHabitSaveEdit(id) {
  const h = (STATE.habits || []).find((x) => x.id === id);
  if (!h) return;
  const sc = document.getElementById("hbScene_" + id);
  const wy = document.getElementById("hbWay_" + id);
  const scene = String((sc && sc.value) || "").trim();
  const way = String((wy && wy.value) || "").trim();
  if (!scene || !way) {
    toast("场景和说法都要填", "err");
    return;
  }
  const res = await apiPostSafe("habit/save", { ...h, scene, way });
  toast(res.ok ? "已保存，下一轮对话就用新说法" : "保存失败：" + (res.error || ""), res.ok ? "ok" : "err");
  if (res.ok) HB_EDIT = 0;
  refreshCurrent();
}

/** 手加一条（声明式：不会被当成模型推断的）；sandbox iframe 吞原生 prompt，走页面内弹窗 */
export function hfHabitAdd() {
  formModal("手加一条表达", [
    { key: "scene", label: "什么时候用？（社交情境，比如「被夸时」）", ph: "被夸时" },
    { key: "way", label: "具体怎么说？（越具体越好，可以带口头禅）", ph: "嘿嘿，谢谢夸奖～", area: true },
  ], async (v) => {
    const scene = String(v.scene || "").trim();
    const way = String(v.way || "").trim();
    if (!scene || !way) {
      toast("场景和说法都要填", "err");
      return false;
    }
    const res = await apiPostSafe("habit/save", {
      scene: scene.slice(0, 40),
      way: way.slice(0, 120),
      cues: [],
      scope: "手加",           // 归属由后端归一成 '手加·{pid}'（拍-4）
      enabled: 1,
    });
    toast(res.ok ? "已加入（标为「我手加」）" : "保存失败：" + (res.error || ""), res.ok ? "ok" : "err");
    if (res.ok) refreshCurrent();
    return res.ok;   // 失败时保持弹窗开着，改完可重试
  });
}

/** 出处核对：优先读条目自己的证据快照（E3：原话+时间+会话+说话人+message_id，逐字可回查） */
let HB_CORPUS = { at: 0, items: null, err: "" };

async function hbCorpusItems() {
  if (HB_CORPUS.items && Date.now() - HB_CORPUS.at < 30000) return HB_CORPUS;
  const res = await apiSafe("corpus", { limit: 500 }, { items: [] });
  HB_CORPUS = { at: Date.now(), items: (res && res.items) || [], err: (res && res.__err) || "" };
  return HB_CORPUS;
}

export async function hfHabitCues(btn, id) {
  const box = document.getElementById("hcues_" + id);
  if (!box) return;
  const n = btn ? String(btn.dataset.n || "") : "";
  const open = box.style.display !== "none";
  if (open) {
    box.style.display = "none";
    if (btn) btn.textContent = "出处（" + n + "）";
    return;
  }
  box.style.display = "block";
  if (btn) btn.textContent = "收起出处（" + n + "）";
  if (box.dataset.ready === "1") return;     // 已核对过：直接切换显示，不再重复拉
  const h = (STATE.habits || []).find((x) => String(x.id) === String(id));
  const evidence = h && Array.isArray(h.evidence) ? h.evidence : [];
  if (evidence.length) {
    // 新学到的条目：直接用落库时的证据快照（原话逐字 + 消息 id + 可回查判定）
    const rows = evidence
      .map((e) => {
        const mid = String(e.message_id || "");
        const ok = e.recoverable === true;
        return `<div class="cq">「${esc(String(e.text || ""))}」
        <span class="tag ${ok ? "ok" : "gy"}" style="font-size:10px">${ok ? "语料库在档（逐字相等）" : "语料已被清理，这是学习时的快照"}</span>
        <span class="mut" style="font-size:10.5px">${esc(fmtTime(e.ts))}${e.who ? " · " + esc(String(e.who)) : ""}${mid ? " · 消息 " + esc(mid) : ""}</span></div>`;
      })
      .join("");
    box.innerHTML = `<div class="mut" style="font-size:10.5px;margin:7px 0 4px">学自这些原话 · 出处随条目落库（E3），不依赖语料表还在不在</div>
      ${rows}
      <div class="mut" style="font-size:10px;margin-top:5px">「在档」＝语料库里还找得到逐字相同的原话；被清理掉的仍留有快照，不假装有出处。</div>`;
    box.dataset.ready = "1";
    return;
  }
  const cues = h && Array.isArray(h.cues) ? h.cues.map((c) => String(c)).filter(Boolean) : [];
  if (!cues.length) {
    box.innerHTML = '<div class="mut" style="font-size:10.5px;margin:7px 0 4px">这条没有记录出处（旧数据）</div>';
    box.dataset.ready = "1";
    return;
  }
  box.innerHTML = '<div class="mut" style="font-size:10.5px;margin:7px 0 4px">正在与语料库逐字核对…</div>';
  const corpus = await hbCorpusItems();
  const byText = new Map();
  (corpus.items || []).forEach((c) => {
    const t = String(c.text == null ? "" : c.text).trim();
    if (!t) return;
    const arr = byText.get(t) || [];
    arr.push(c);
    byText.set(t, arr);
  });
  const rows = cues
    .map((cue) => {
      const hits = corpus.err ? [] : byText.get(cue.trim()) || [];
      if (hits.length) {
        return hits
          .slice(0, 3)
          .map(
            (c) => `<div class="cq">「${esc(cue)}」 <span class="tag ok" style="font-size:10px">语料库在档</span>
        <span class="mut" style="font-size:10.5px">${esc(fmtTime(c.ts))}${c.user_name ? " · " + esc(String(c.user_name)) : ""}</span></div>`
          )
          .join("");
      }
      return `<div class="cq">「${esc(cue)}」 <span class="mut" style="font-size:10.5px">语料库已没有这条原话（可能被清理）—— 这是学习当时留下的 60 字快照</span></div>`;
    })
    .join("");
  box.innerHTML = `
    <div class="mut" style="font-size:10.5px;margin:7px 0 4px">学自这些原话 · 与语料库（hf_corpus）逐字核对${corpus.err ? "；本次语料库读取失败，以下均为快照" : ""}</div>
    ${rows}
    <div class="mut" style="font-size:10px;margin-top:5px">旧条目只有 60 字快照（当时没记 corpus_id）；新学的条目会带完整出处与消息 id。</div>
    ${corpus.err ? `<div class="mut" style="font-size:10px;margin-top:3px">语料库读取失败：${esc(corpus.err)}</div>` : ""}`;
  box.dataset.ready = "1";
}

/** 折叠辅助图表 */
export function hfChartToggle(btn, id) {
  const box = document.getElementById(id);
  if (!box) return;
  const open = box.style.display !== "none";
  box.style.display = open ? "none" : "block";
  if (btn) btn.textContent = open ? "图表" : "收起图表";
}

export function hfHabitFilter(v) {
  const q = String(v || "").trim();
  $$("#hbList .card").forEach((c) => {
    const key = c.getAttribute("data-habit") || "";
    c.style.display = !q || key.includes(q) ? "" : "none";
  });
}

export async function hfHabitToggle(id, el) {
  const h = (STATE.habits || []).find((x) => x.id === id);
  if (!h) return;
  const next = h.enabled ? 0 : 1;
  const res = await apiPostSafe("habit/save", { ...h, enabled: next });
  if (!res.ok) {
    if (el) el.checked = !!h.enabled;   // 失败：开关翻回原状
    toast("保存失败：" + (res.error || ""), "err");
    return;
  }
  h.enabled = next;
  // 就地更新这一张卡：不整页重渲染——卡片位置、滚动位置、其它卡片都不动，
  // 列表重排（按使用 / 按最近 / 只看启用…）留给用户自己刷新或切页时再发生。
  const card = (el && el.closest && el.closest(".hbcard")) || $$(".hbcard").find((c) => c.querySelector('input[onchange*="hfHabitToggle(' + id + '"]'));
  if (card) {
    card.classList.toggle("off", !next);
    const tagsRow = card.querySelector(".row.bt .row");
    const tag = card.querySelector(".tag.dg");
    if (!next && !tag && tagsRow) {
      const t = document.createElement("span");
      t.className = "tag dg pop";
      t.textContent = "已停用";
      tagsRow.appendChild(t);
    } else if (next && tag) {
      tag.remove();
    }
    const sw = card.querySelector("label.sw");
    if (sw) sw.title = next ? "停用（保留条目，只是不再参考）" : "启用";
  }
  const kEl = document.getElementById("hbOn");
  if (kEl) kEl.textContent = fmtNum((STATE.habits || []).filter((x) => x.enabled).length);
  toast(next ? "已启用" : "已停用", "ok");
}

export async function hfHabitDelete(id) {
  confirmModal(
    "删除这条表达",
    `会把它写进「不再学」名单：条目软删（可恢复），模型不会再把它学回来。原话快照保留，真删请到「设置 · 数据治理 → 只清表达」。`,
    async () => {
      const res = await apiPostSafe("habit/delete", { id });
      const d = (res.data || {});
      toast(res.ok ? `已删除并写进「不再学」名单（${d.banned ? "新记入名单" : "名单里已有"}）` : "删除失败：" + res.error, res.ok ? "ok" : "err");
      refreshCurrent();
    }
  );
}

/** 审核：通过 / 驳回（驳回可填理由，一并写进「不再学」名单） */
export async function hfExprReview(id, action) {
  const act = action === "reject" ? "reject" : "approve";
  if (act === "reject") {
    formModal("驳回这条表达", [{ key: "reason", label: "理由（可选，会写进「不再学」名单）", ph: "不像我会说的话" }], async (v) => {
      const res = await apiPostSafe("expr/review", { ids: [id], action: "reject", reason: String(v.reason || "").trim() });
      toast(res.ok ? "已驳回，并写进「不再学」名单" : "驳回失败：" + res.error, res.ok ? "ok" : "err");
      if (res.ok) refreshCurrent();
      return res.ok;
    });
    return;
  }
  const res = await apiPostSafe("expr/review", { ids: [id], action: "approve" });
  toast(res.ok ? "已通过（下一轮就能用）" : "通过失败：" + res.error, res.ok ? "ok" : "err");
  if (res.ok) refreshCurrent();
}

/** 批量：把当前所有「待审」一次通过 */
export async function hfExprApproveAll() {
  const pend = (STATE.habits || []).filter((h) => hbStateOf(h) === "pending");
  if (!pend.length) {
    toast("没有待审的条目", "ok");
    return;
  }
  confirmModal("一键通过待审", `把 ${pend.length} 条「待审」全部标记为已通过（立刻进注入池）。`, async () => {
    const res = await apiPostSafe("expr/review", { ids: pend.map((h) => h.id), action: "approve" });
    toast(res.ok ? `已通过 ${((res.data || {}).changed) || pend.length} 条` : "失败：" + res.error, res.ok ? "ok" : "err");
    if (res.ok) refreshCurrent();
  });
}

/** 移出「不再学」名单（同键可再学；被软删的同键条目一并恢复） */
export async function hfExprBanRemove(normKey) {
  const key = String(normKey || "");
  if (!key) return;
  confirmModal("移出「不再学」名单", "移出后模型可以重新学到这条说法，被清空的同键条目也会恢复成可用。", async () => {
    const res = await apiPostSafe("expr/ban/remove", { norm_key: key });
    const d = (res.data || {});
    toast(res.ok ? `已移出（恢复 ${fmtNum(d.restored || 0)} 条）` : "移出失败：" + res.error, res.ok ? "ok" : "err");
    if (res.ok) refreshCurrent();
  });
}

export async function hfExtractHabits(btn) {
  if (btn) {
    btn.classList.add("loading");
    btn.disabled = true;
  }
  const res = await apiPostSafe("expression/extract", {});
  if (btn) {
    btn.classList.remove("loading");
    btn.disabled = false;
  }
  const d = res.data || {};
  const ok = res.ok && d.ok !== false;
  const saved = Number(d.saved || 0);
  if (!ok && !d.skipped_reason) {
    toast("提取失败：" + (d.error || res.error || ""), "err");
  } else if (saved > 0) {
    toast(`提取完成：新增 ${saved} 条（看了 ${d.scanned || 0} 条语料）`, "ok");
  } else {
    // 学 0 条时直接给原因（不能只回一句「完成」）
    const e = (HB_RUNS.enum || []).find((x) => x.value === d.skipped_reason);
    toast(`这批没学到新表达（看了 ${d.scanned || 0} 条语料）：${e ? e.text : (d.skipped_reason || d.error || "没有可学的新说法")}`, ok ? "" : "err");
  }
  refreshCurrent();
}

/** 老 hash（学习日志）落到这里：不再保留「第三份日志」视图，统一回语言学习页的③学习诊断 */
export async function renderExpressionLog() {
  return renderExpressionLib();
}

/** 简洁 / 详细 切换（记住选择，刷新后不丢） */
export function hfLogMode(mode) {
  HF_LOG_MODE = mode === "full" ? "full" : "brief";
  refreshCurrent();
}

/** 详细模式下展开某条事件 */
export function hfLogToggle(id) {
  const box = document.getElementById("logd_" + id);
  if (!box) return;
  box.style.display = box.style.display === "none" ? "block" : "none";
}

export async function renderExpressionConf(opts = {}) {
  return renderDomainConf("expression", {
    // 收尾批：同一个渲染器嵌进「设置 · 模块与参数」（bare=用卡头代替整页页头）
    bare: !!opts.bare,
    recentFirst: !!opts.recentFirst,
    chartId: "dcChart_expression",
    side: card(
      `<div class="mut" style="font-size:11.5px;line-height:1.8">
        提取流程：取语料 → 让「表达提取」模型写出「情景 | 表达方式」→ 去重后入库（只有这一次模型调用）。<br>
        模型在「设置 → 模型分配 → 表达提取」里指定。</div>
       <div class="row" style="gap:6px;margin-top:10px">
         <button class="btn s" onclick="goPage('settings','models')">去配模型</button>
         <button class="btn s" onclick="goPage('actor','voice')">去表达库</button>
       </div>`,
      { title: "学习链路" }
    ),
  });
}

/* ==================================================================
   人格
   ================================================================== */
export async function renderPersonaList() {
  const p = await apiSafe("personas", {}, { available: false, items: [], active: "", counts: {} });
  STATE.personas = p;
  const items = p.items || [];
  const counts = p.counts || {};
  const html = `
  ${pageHead(
    "人格列表",
    "读的是 AstrBot 原生人格库（<span class='mono'>/personas</span>），改这里就是改原生人格 · 绿色徽标=当前正在用",
    `<button class="btn s" onclick="renderPersonaRefresh()">刷新</button>`,
    "☺"
  )}
  <div class="grid gauto sec">
    ${
      items.length
        ? items
            .map((x) => {
              const isActive = String(x.persona_id) === String(p.active);
              const memCount = counts[x.persona_id] || counts[""] || 0;
              return `<div class="card cp hov" style="${isActive ? "border-color:var(--primary);box-shadow:0 0 0 3px var(--primary-soft)" : ""}">
          <div class="row" style="gap:11px;margin-bottom:9px">
            <div id="psAv_${esc(x.persona_id)}" style="flex-shrink:0"></div>
            <div style="min-width:0">
              <div style="font-weight:650;font-size:13.5px;display:flex;align-items:center;gap:6px">
                ${esc(x.persona_id)} ${isActive ? `<span class="tag ok">当前</span>` : ""}</div>
              <div class="mut" style="font-size:10.5px">system_prompt ${fmtNum(x.prompt_len)} 字 · 开场白 ${(x.begin_dialogs || []).length} 条</div>
            </div>
          </div>
          <div class="mut" style="font-size:11.5px;line-height:1.6;max-height:56px;overflow:hidden">${esc(String(x.system_prompt || "").slice(0, 160))}…</div>
          <div class="row" style="gap:6px;margin-top:10px;flex-wrap:wrap">
            <button class="btn s ${isActive ? "" : "p"}" onclick="hfPersonaActive(${jsStr(x.persona_id)})" ${isActive ? "disabled" : ""}>设为当前</button>
            <button class="btn s" onclick="hfPersonaEdit(${jsStr(x.persona_id)})">编辑</button>
            <span class="spacer"></span>
            <span class="chip" title="这条人格下的记忆数">记忆 ${fmtNum(memCount)}</span>
          </div>
        </div>`;
            })
            .join("")
        : emptyBlock(p.available === false ? "读不到 AstrBot 人格库（可能是版本差异），可以只用原生人格" : "还没有人格，先去 AstrBot 里建一个", "☺")
    }
  </div>
  <div class="grid g2 sec">
    ${card(
      `<div class="mut" style="font-size:11.5px;line-height:1.8">
        人格只有一套：AstrBot 原生人格。<br>
        记忆按人格隔离：切换人格后，记忆库与图谱只显示当前人格的数据（顶栏可临时切换查看视角）。</div>
       <div class="row" style="gap:6px;margin-top:10px"><button class="btn s" onclick="goPage('actor','persona')">人格与表达 →</button></div>`,
      { title: "人格与记忆" }
    )}
    ${card(`<div id="psDist"></div>`, { title: "记忆的人格分布", sub: "各人格下沉淀了多少条记忆（空人格=未归属）" })}
  </div>`;
  return {
    html,
    mount: (host) => {
      items.forEach((x) =>
        C.avatar(host.querySelector(`#psAv_${CSS.escape(String(x.persona_id))}`), {
          name: x.persona_id,
          size: 44,
          color: "#3aa76d",
        })
      );
      mount(host, "#psDist", (el) =>
        C.bars(el, {
          items: Object.keys(counts).map((k) => ({ label: k || "（未归属）", value: counts[k], color: "#3aa76d" })),
        })
      );
    },
  };
}

export function renderPersonaRefresh() {
  refreshCurrent();
}

export async function hfPersonaActive(pid) {
  const res = await apiPostSafe("persona/active", { persona_id: pid });
  const d = res.data || {};
  if (res.ok && d.applied !== false) {
    STATE.personaViewTouched = false;
    toast(`已切到 ${pid}`, "ok");
  } else {
    toast("切换失败：" + (d.error || res.error || "原因未知"), "err");
  }
  refreshCurrent();
  refreshPersonaSwitch();
}

export async function hfPersonaEdit(pid) {
  const x = ((STATE.personas || {}).items || []).find((y) => String(y.persona_id) === String(pid));
  if (!x) return;
  openDrawer(
    "编辑人格 · " + pid,
    `<div class="mut" style="font-size:11.5px;margin-bottom:8px">直接改 AstrBot 原生人格（写回 /personas），不是另存一份</div>
     <div class="mut" style="font-size:11px;margin-bottom:3px">system_prompt（${fmtNum(x.prompt_len)} 字）</div>
     <textarea class="inp" rows="12" id="hfPersonaPrompt">${esc(x.system_prompt)}</textarea>
     <div class="mut" style="font-size:11px;margin:12px 0 3px">开场白（每行一条）</div>
     <textarea class="inp" rows="3" id="hfPersonaDialogs">${esc((x.begin_dialogs || []).join("\n"))}</textarea>
     <div class="row bt" style="margin-top:16px">
       <button class="btn" onclick="closeDrawer()">取消</button>
       <button class="btn p" onclick="hfPersonaSave(${jsStr(pid)})">保存到 AstrBot</button></div>`
  );
}

export async function hfPersonaSave(pid) {
  const prompt = (($("#hfPersonaPrompt") || {}).value || "").trim();
  const dialogs = (($("#hfPersonaDialogs") || {}).value || "").split("\n").map((x) => x.trim()).filter(Boolean);
  const res = await apiPostSafe("persona/save", { persona_id: pid, system_prompt: prompt, begin_dialogs: dialogs });
  toast(res.ok ? "人格已保存" : "保存失败：" + res.error, res.ok ? "ok" : "err");
  if (res.ok) {
    closeDrawer();
    refreshCurrent();
  }
}

export async function renderPersonaState() {
  const [p, prop, ev] = await Promise.all([
    apiSafe("personas", {}, { available: false, items: [], active: "" }),
    apiSafe("persona/proposals", {}, { items: [] }),
    apiSafe("persona/evolve", {}, { ok: false }),
  ]);
  const items = prop.items || [];
  const pend = items.filter((x) => x.status === "pending");                 // 主体只放待审核
  const hist = items.filter((x) => x.status !== "pending");                 // 处理过的进"文件列表"
  /** 提案状态 → 面板文案与颜色（stale/expired 是"自动作废"，不是"被你驳回"，别混成一种） */
  const statusView = (st) =>
    ({
      approved: ["已采纳", "ok"],
      rejected: ["已驳回", "gy"],
      stale: ["已过期（人格被改过）", "warn"],
      expired: ["超时作废", "warn"],
      pending: ["待审核", "warn"],
    }[String(st || "")] || [String(st || "未知"), "gy"]);
  /** 文件名用的日期戳：人格进化-2026-10-07-14-54.md */
  const fileStamp = (ms) => {
    const d = new Date((Number(ms) || 0) * 1000);
    const p2 = (n) => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${p2(d.getMonth() + 1)}-${p2(d.getDate())}-${p2(d.getHours())}-${p2(d.getMinutes())}`;
  };
  const E = ev.params || {};
  const scopeCn = { desc_style: "描述 + 说话风格", desc_only: "只改描述", all: "整段重写" }[E.evolve_scope] || String(E.evolve_scope || "—");
  const days = (ms) => (ms ? fmtTime(ms) : "—");
  const html = `
  ${pageHead("人格状态 · 自动进化", "模型可以提案改人格，但必须你点通过才会写入", `<button class="btn p" onclick="hfPersonaPropose(this)">立即生成提案</button>`, "✎")}
  ${kpis(
    kpiCard("pnActive", esc(p.active || "—"), "当前人格")
    + kpiCard("pnPend", fmtNum(pend.length), "待审核提案")
    + kpiCard("pnLast", days(ev.last_evolve_at), "上次进化")
    + kpiCard("pnNext", ev.next_due_at ? days(ev.next_due_at) : "—", `下次最早（间隔 ${E.evolve_min_hours || 24}h）`)
  )}
  ${card(
    `<div class="row bt" style="gap:8px;margin-bottom:0">
       <div style="min-width:0">
         <div class="pn">进化策略</div>
         <div class="pd">${E.evolve_auto ? "自动" : "手动"} · 自刷新（至少新增 <b>${fmtNum(E.evolve_min_memories || 6)}</b> 条记忆才动）·
           当前新增 <b>${fmtNum(ev.new_memories)}</b> 条 · 可改范围 <b>${esc(scopeCn)}</b> ·
           ${E.evolve_require_confirm ? "<b>需要你确认</b>" : "<b>自动写入</b>"}
           ${ev.llm_ready === false ? '<span class="tag dg">模型通道不可用</span>' : ""}</div>
       </div>
       <button class="btn xs" onclick="hfEvolveParams(this)">参数</button>
     </div>
     <div id="evParams" style="display:none;margin-top:9px">
       <div class="grid g2" style="gap:9px">
         <div class="prow"><div><div class="pn">自动进化</div><div class="pd">按周期自己出提案（关掉只能手点）</div></div>
           <div class="pc"><label class="sw"><input type="checkbox" id="evAuto" ${E.evolve_auto ? "checked" : ""} onchange="hfEvolveSave(this)"><span class="tk"></span><span class="kb"></span></label></div></div>
         <div class="prow"><div><div class="pn">最短间隔（小时）</div><div class="pd">自刷新：期间没有新记忆就不动</div></div>
           <div class="pc"><input class="ipt mono" id="evHours" style="width:90px" value="${esc(String(E.evolve_min_hours || 24))}" onchange="hfEvolveSave(this)"></div></div>
         <div class="prow"><div><div class="pn">至少新增记忆数</div><div class="pd">太少容易让人格漂移</div></div>
           <div class="pc"><input class="ipt mono" id="evMems" style="width:90px" value="${esc(String(E.evolve_min_memories || 6))}" onchange="hfEvolveSave(this)"></div></div>
         <div class="prow"><div><div class="pn">可改范围</div><div class="pd">建议只放开描述与说话风格</div></div>
           <div class="pc"><select class="ipt" id="evScope" onchange="hfEvolveSave(this)">
             <option value="desc_style" ${E.evolve_scope === "desc_style" ? "selected" : ""}>描述 + 说话风格</option>
             <option value="desc_only" ${E.evolve_scope === "desc_only" ? "selected" : ""}>只改描述</option>
             <option value="all" ${E.evolve_scope === "all" ? "selected" : ""}>整段重写</option></select></div></div>
         <div class="prow"><div><div class="pn">需要人工确认</div><div class="pd">关掉＝自动写回 AstrBot 原生人格</div></div>
           <div class="pc"><label class="sw"><input type="checkbox" id="evConfirm" ${E.evolve_require_confirm ? "checked" : ""} onchange="hfEvolveSave(this)"><span class="tk"></span><span class="kb"></span></label></div></div>
         <div class="prow"><div><div class="pn">人格上次保存</div><div class="pd">面板里改人格 / 通过提案都会刷新</div></div>
           <div class="pc mono" style="font-size:11.5px">${days(ev.last_save_at)}</div></div>
       </div>
     </div>`,
    { title: "参数（点右侧展开）", sub: "和插件配置 _conf_schema.json 同名同义，改哪边都行", cls: "sec" }
  )}
  ${card(
    `<div class="mut" style="font-size:11.5px;line-height:1.7;margin-bottom:9px">当前人格、system prompt、切换都在 AstrBot 原生人格库里（本页只读不改；「进化提案」经你审核后写回原生人格）。</div>
     <div class="prow"><div><div class="pn">记忆人格隔离</div><div class="pd">开启后记忆按人格隔离，切换人格只看到自己那份</div></div>
      <div class="pc"><label class="sw"><input type="checkbox" ${p.isolated ? "checked" : ""} onchange="hfIsolatedToggle(this.checked)"><span class="tk"></span><span class="kb"></span></label></div></div>`,
    { title: "人格来源：AstrBot 原生", cls: "sec" }
  )}
  <div class="sec-t">待审核提案</div>
  <div class="col">
    ${
      pend.length
        ? pend
            .map(
              (x) => `<div class="card cp">
        <div class="row bt" style="margin-bottom:8px">
          <div class="row" style="gap:7px">
            <span class="tag warn">待审核</span>
            <span class="chip">${esc(x.persona_id || "—")}</span>
            <span class="mut" style="font-size:10.5px">生成于 ${fmtTime(x.ts)} · token ${fmtNum((x.in_tokens || 0) + (x.out_tokens || 0))}</span>
          </div>
          <div class="row" style="gap:6px">
            <button class="btn xs p" onclick="hfPersonaDecide(${x.id}, true)">通过并写入</button>
            <button class="btn xs dg" onclick="hfPersonaDecide(${x.id}, false)">驳回</button>
          </div>
        </div>
        <div class="mut" style="font-size:11px;margin-bottom:5px">改动理由：${esc(x.rationale || "—")}</div>
        <div class="grid g2" style="gap:9px">
          <div><div class="mut" style="font-size:10.5px;margin-bottom:3px">当前人格片段 · 上次保存 ${days(ev.last_save_at)}</div>
            <div class="chip" style="white-space:pre-wrap;line-height:1.6;max-height:180px;overflow:auto;width:100%">${esc(String(x.base || ""))}</div></div>
          <div><div class="mut" style="font-size:10.5px;margin-bottom:3px">进化体（提案）· 生成于 ${fmtTime(x.ts)}${x.status === "approved" && x.decided_at ? " · 已采纳于 " + fmtTime(x.decided_at) : ""}</div>
            <div class="noteblock scroll" style="border-left-color:var(--primary)">${esc(String(x.proposal || ""))}</div></div>
        </div>
      </div>`
            )
            .join("")
        : emptyBlock("现在没有待审核的提案。点右上角「立即生成提案」让模型读语料写一版新人格给你对比", "✎")
    }
  </div>
  <div class="sec-t">历史记录</div>
  ${
    hist.length
      ? `<div class="col" style="gap:0">${hist.map((x) => {
          const stamp = fileStamp(x.decided_at || x.ts);
          const ok = x.status === "approved";
          return `<div class="fitem">
            <div class="frow-line" onclick="hfHistToggle(${x.id})">
              <span class="fic">📄</span>
              <span class="fname mono">人格进化-${esc(stamp)}.md</span>
              <span class="tag ${statusView(x.status)[1]}">${statusView(x.status)[0]}</span>
              <span class="mut" style="font-size:10.5px">${fmtTime(x.decided_at || x.ts)}</span>
              ${ok ? `<button class="btn xs" onclick="event.stopPropagation();hfPersonaRollback(${x.id})">回滚到此版本</button>` : ""}
              <button class="btn xs" onclick="event.stopPropagation();hfHistToggle(${x.id})">展开</button>
            </div>
            <div class="fbody" id="hist_${x.id}" style="display:none">
              <div class="mut" style="font-size:11px;margin:6px 0 4px">改动理由：${esc(x.rationale || "—")}</div>
              <div class="grid g2" style="gap:9px">
                <div><div class="mut" style="font-size:10.5px;margin-bottom:3px">当时的人格（base）</div>
                  <div class="chip" style="white-space:pre-wrap;line-height:1.6;max-height:260px;overflow:auto;width:100%">${esc(String(x.base || "—"))}</div></div>
                <div><div class="mut" style="font-size:10.5px;margin-bottom:3px">进化体（提案全文）</div>
                  <div class="noteblock scroll" style="border-left-color:var(--primary);max-height:260px">${esc(String(x.proposal || "—"))}</div></div>
              </div>
            </div>
          </div>`;
        }).join("")}</div>`
      : emptyBlock("还没有历史记录 —— 通过或驳回一次提案后，这里会出现带日期戳的文件", "🗂")
  }`;
  return { html };
}

/** 展开/收起进化参数（不再单独占一块） */
export function hfEvolveParams(btn) {
  const box = document.getElementById("evParams");
  if (!box) return;
  const open = box.style.display !== "none";
  box.style.display = open ? "none" : "block";
  if (btn) btn.textContent = open ? "参数" : "收起参数";
}

/** 历史记录：点文件名原地展开详细人格 */
export function hfHistToggle(id) {
  const box = document.getElementById("hist_" + id);
  if (!box) return;
  box.style.display = box.style.display === "none" ? "block" : "none";
}

/** 保存进化参数（和 _conf_schema.json 同名同义，改哪边都行） */
export async function hfEvolveSave() {
  const num = (id, d) => {
    const el = document.getElementById(id);
    const v = parseInt(String((el && el.value) || "").trim(), 10);
    return Number.isFinite(v) && v > 0 ? v : d;
  };
  const on = (id, d) => {
    const el = document.getElementById(id);
    return el ? !!el.checked : d;
  };
  const scopeEl = document.getElementById("evScope");
  const res = await apiPostSafe("config/save", {
    patch: {
      evolve_auto: on("evAuto", true),
      evolve_min_hours: num("evHours", 24),
      evolve_min_memories: num("evMems", 6),
      evolve_scope: scopeEl ? String(scopeEl.value) : "desc_style",
      evolve_require_confirm: on("evConfirm", true),
    },
  });
  toast(res.ok ? "进化参数已保存" : "保存失败：" + (res.error || ""), res.ok ? "ok" : "err");
  if (res.ok) refreshCurrent();
}

export async function hfIsolatedToggle(on) {
  const res = await apiPostSafe("config/save", { patch: { memory_persona_isolated: !!on } });
  toast(res.ok ? (on ? "记忆已按人格隔离" : "已改为全人格共享记忆") : "保存失败：" + res.error, res.ok ? "ok" : "err");
  refreshCurrent();
}

export async function hfPersonaPropose(btn) {
  if (btn) {
    btn.classList.add("loading");
    btn.disabled = true;
  }
  const res = await apiPostSafe("persona/propose", {});
  if (btn) {
    btn.classList.remove("loading");
    btn.disabled = false;
  }
  const d = res.data || {};
  toast(res.ok && d.ok !== false ? `已生成提案 #${d.id}，请对比后决定` : "生成失败：" + (d.error || res.error || ""), res.ok && d.ok !== false ? "ok" : "err");
  refreshCurrent();
}

export async function hfPersonaRollback(id) {
  confirmModal(
    "回滚人格？",
    "会把这条提案记录里保存的「旧版本人格」写回 AstrBot 原生人格 —— 当前人格会被覆盖（之后想再改回来也可以）。",
    async () => {
      const r = await apiPostSafe("persona/rollback", { id });
      if (r && r.ok) {
        toast("已回滚到该版本（人格已更新）");
        refreshCurrent();
      } else {
        toast("回滚失败：" + ((r && r.error) || "未知错误"));
      }
    }
  );
}

export async function hfPersonaDecide(id, approve) {
  const res = await apiPostSafe("persona/decide", { id, approve: !!approve });
  const d = res.data || {};
  toast(res.ok && d.ok !== false ? (approve ? "已写入人格" : "已驳回") : "失败：" + (d.error || res.error || ""), res.ok && d.ok !== false ? "ok" : "err");
  refreshCurrent();
}

/* ==================================================================
   设置：模块与参数 / 用户与群聊 / 模型分配 / Token
   ================================================================== */
/* ---------------- 参数中心：搜索 + 最近改过置顶（收尾批） ----------------
   搜索：纯前端过滤 .prow（键名 / 中文名），**不重渲染** —— 不丢光标、不打接口，输入法组字不受影响。
   最近改过置顶：本次会话里「改过并保存成功」的参数排到各自域分区最前（store.js 登记，保存成功才记）。 */
let PARAMS_QUERY = "";
let PARAMS_RECENT_FIRST = true;

/** 参数分区渲染失败时的兜底卡（单个域失败不影响整页；保留 data-pdom 让域深链仍有着落） */
function paramsSectionErr(domain, title, err) {
  return {
    html: `<div class="sec" data-pdom="${esc(domain)}">${card(
      `<div class="mut">「${esc(title)}」读取失败：<b>${esc(String((err && err.message) || err))}</b></div>`,
      { title }
    )}</div>`,
  };
}

/** 搜索够用就行：键名和中文名都按子串匹配（大小写不敏感） */
export function hfParamsSearch(v) {
  PARAMS_QUERY = String(v == null ? "" : v);
  applyParamsFilter();
}

export function hfParamsRecentToggle() {
  PARAMS_RECENT_FIRST = !PARAMS_RECENT_FIRST;
  refreshCurrent();
}

function applyParamsFilter() {
  const host = document.getElementById("pagehost");
  if (!host) return;
  const q = PARAMS_QUERY.trim().toLowerCase();
  let total = 0;
  let hit = 0;
  host.querySelectorAll("[data-pdom]").forEach((sec) => {
    let secHit = 0;
    sec.querySelectorAll(".prow[data-key]").forEach((row) => {
      total += 1;
      const key = String(row.getAttribute("data-key") || "").toLowerCase();
      const nameEl = row.querySelector(".pn");
      const name = String((nameEl && nameEl.textContent) || "").toLowerCase();
      const ok = !q || key.indexOf(q) >= 0 || name.indexOf(q) >= 0;
      row.style.display = ok ? "" : "none";
      if (ok) {
        secHit += 1;
        hit += 1;
      }
    });
    sec.style.display = secHit || !q ? "" : "none";
  });
  const info = document.getElementById("paramsSearchInfo");
  if (info) info.textContent = q ? `匹配 ${hit} / ${total} 项` : `共 ${total} 项参数`;
  const noMatch = document.getElementById("paramsNoMatch");
  if (noMatch) noMatch.style.display = q && !hit ? "" : "none";
}

function paramsToolbar() {
  return `<div class="card cp sec" style="padding:10px 13px">
    <div class="row bt wrap" style="gap:9px">
      <span class="searchbox" style="flex:1;min-width:220px"><i class="ic">⌕</i>
        <input id="paramsQ" placeholder="搜索参数（键名 / 中文名）…" value="${esc(PARAMS_QUERY)}" oninput="hfParamsSearch(this.value)"></span>
      <button class="btn xs" onclick="hfParamsRecentToggle()" style="flex-shrink:0" title="本次会话里「改过并保存成功」的参数，置顶显示">最近改过置顶：${PARAMS_RECENT_FIRST ? "开" : "关"}</button>
      <span class="mut" id="paramsSearchInfo" style="font-size:11px;flex-shrink:0"></span>
    </div>
    <div class="mut" id="paramsNoMatch" style="display:none;margin-top:7px">没有匹配的参数 —— 换个词试试（键名和中文名都能搜）</div>
  </div>`;
}

export async function renderSettingsMain() {
  // 域参数渲染器来自 views-events（与原页底折叠区**同一批函数**：控件 / 保存逻辑 / 接口原样，bare=嵌进本页）
  const [cfg, detect, stats, dom, pExp, pGate, pPro, pRec, pFav] = await Promise.all([
    apiSafe("config", {}, {}),
    apiSafe("import/detect", {}, {}),
    apiSafe("stats", { persona_id: "" }, {}),
    apiSafe("config/domains", {}, { domains: {} }),
    renderExpressionConf({ bare: true, recentFirst: PARAMS_RECENT_FIRST }).catch((err) => paramsSectionErr("expression", "语言学习参数", err)),
    renderGateConf({ bare: true, recentFirst: PARAMS_RECENT_FIRST }).catch((err) => paramsSectionErr("gate", "门控参数", err)),
    renderProactiveConf({ bare: true, recentFirst: PARAMS_RECENT_FIRST }).catch((err) => paramsSectionErr("proactive", "主动消息", err)),
    renderRecallConf({ bare: true, recentFirst: PARAMS_RECENT_FIRST }).catch((err) => paramsSectionErr("recall", "撤回处理参数", err)),
    renderFavourConf({ bare: true, recentFirst: PARAMS_RECENT_FIRST }).catch((err) => paramsSectionErr("favour", "结算参数", err)),
  ]);
  if (cfg && cfg.__err) {
    return {
      html: pageHead(
        "设置读取失败",
        `配置接口现在拿不到：${esc(String(cfg.__err))}`,
        `<button class="btn p" onclick="hfRefresh()">重试</button>`,
        "⚠"
      ) + card(
        `<div class="fnote">⚠ 为避免把空值/关闭状态写回，本次不渲染开关。请重试。</div>`,
        { title: "读取失败" }
      ),
    };
  }
  // 「输出拟人」域（分段 / 打字延迟 / 表情包 / 语音）此前在面板里没有任何卡片 ——
  // 只有 AstrBot 原生配置弹窗能看到，等于"控件有、面板点不到"。这里给它一张真卡片（能保存）。
  const outKeys = ((dom || {}).domains || {}).output || [];
  const orderKeys = (keys) => (PARAMS_RECENT_FIRST ? recentFirstKeys(keys) : keys.slice());
  STATE.config = cfg;
  STATE.stats = stats;
  const coreAll = [
    ["master_switch", "总开关", "关掉后StarHeart整体不介入，适合排查问题"],
    ["session_context_enabled", "会话上下文", "把同会话最近消息一起给模型"],
    ["memory_persona_isolated", "记忆人格隔离", "记忆按人格分开存放"],
  ];
  // 最近改过置顶：核心开关这几行也按同一口径排序
  const rows = orderKeys(coreAll.map((r) => r[0]))
    .map((k) => coreAll.find((r) => r[0] === k))
    .filter(Boolean);
  const lm = detect || {};
  const html = `
  ${pageHead("设置", "模块开关与全部域的护栏参数（门控 / 主动 / 撤回 / 好感 / 输出 / 语言学习）都收拢在这一页；搜索框可直接找键名或中文名", "", "⚙")}
  ${paramsToolbar()}
  <div data-pdom="core">${card(
    rows
      .map(([key, name, desc]) => {
        const v = cfg[key];
        const isBool = key !== "output_typo_mode";
        const touched = configTouchedAt(key);
        return `<div class="prow${touched ? " modified" : ""}" data-key="${key}"${touched ? ` title="本次会话里改过并保存成功 · ${esc(fmtTime(touched))}"` : ""}><div><div class="pn">${esc(name)}</div><div class="pd">${esc(desc)} · <span class="mono">${key}</span></div></div>
          <div class="pc">${
            isBool
              ? `<label class="sw"><input type="checkbox" data-hf-cfg="${key}" ${v ? "checked" : ""}><span class="tk"></span><span class="kb"></span></label>`
              : `<select class="inp" data-hf-cfg="${key}">${["off", "light", "medium"].map((o) => `<option ${String(v) === o ? "selected" : ""}>${o}</option>`).join("")}</select>`
          }</div></div>`;
      })
      .join("") + `<div class="row" style="justify-content:flex-end;margin-top:12px"><button class="btn p" onclick="hfSaveConfig(this)">保存</button></div>`,
    { title: "核心开关", cls: "sec" }
  )}</div>
  <div data-pdom="output">${outKeys.length
    ? card(
        domainRowsHtml("output", cfg, orderKeys(outKeys)) +
          `<div class="row" style="justify-content:flex-end;margin-top:12px"><button class="btn p" onclick="hfSaveDomain('output',this)">保存输出参数</button></div>`,
        {
          title: "输出拟人 · 分段 / 打字延迟 / 表情包 / 语音",
          sub: "对应后端 CONFIG_DOMAINS['output']：这些是「发出去之前」的拟人处理（改完点保存）",
          cls: "sec",
        }
      )
    : ""}</div>
  ${pExp.html}
  ${pGate.html}
  ${pPro.html}
  ${pRec.html}
  ${pFav.html}
  <div class="grid g2 sec">
    ${card(
      `<div class="grid g2" style="gap:9px">
        <div style="background:var(--surface2);border:1px solid var(--bd);border-radius:11px;padding:10px 12px">
          <div class="row" style="gap:6px"><span class="hlamp ${lm.plugin_dir_exists ? "g" : "y"}"></span><b style="font-size:12px">LivingMemory 插件</b></div>
          <div class="mut" style="font-size:10.5px;margin-top:3px">${lm.plugin_dir_exists ? "已检测到源库" : "未检测到（导入功能不可用）"}</div></div>
        <div style="background:var(--surface2);border:1px solid var(--bd);border-radius:11px;padding:10px 12px">
          <div class="row" style="gap:6px"><span class="hlamp ${lm.db_exists ? "g" : "y"}"></span><b style="font-size:12px">源数据库</b></div>
          <div class="mut" style="font-size:10.5px;margin-top:3px">文档 ${fmtNum(lm.documents)} · 原子 ${fmtNum(lm.atoms)} · 已导入 ${fmtNum(lm.already_imported)}</div></div>
      </div>
      <div class="row wrap" style="gap:8px;margin-top:12px">
        <label class="row" style="gap:6px;font-size:11.5px"><input type="checkbox" id="hfLayerDoc" checked> 文档层</label>
        <label class="row" style="gap:6px;font-size:11.5px"><input type="checkbox" id="hfLayerAtom" checked> 原子层</label>
        <button class="btn s" onclick="hfImportDetect()">重新检测</button>
        <button class="btn p" onclick="hfImportStart(false)">导入 5 条试跑</button>
        <button class="btn" onclick="hfImportStart(true)">全量导入</button>
      </div>
      <div style="margin-top:11px"><div class="bar"><span class="bf" id="hfImportBar" style="display:block;width:0%"></span></div>
        <div class="row bt" style="margin-top:5px"><span class="mut" style="font-size:11px" id="hfImportPhase">未开始</span><span class="mono" style="font-size:11px" id="hfImportCount"></span></div></div>`,
      { title: "记忆导入", sub: "从 astrbot_plugin_livingmemory 的源库导入历史记忆（导入会一并重建关系链）" }
    )}
    ${card(
      `<div style="overflow-x:auto"><table><thead><tr><th>开始时间</th><th>来源</th><th>导入</th><th>跳过</th><th>失败</th><th>状态</th></tr></thead>
        <tbody id="hfImportHistory">${
          ((detect || {}).batches || [])
            .map(
              (b) => `<tr><td class="mono">${fmtTime(b.started_at)}</td><td>${esc(b.source)}</td><td class="mono">${fmtNum(b.imported)}</td>
              <td class="mono">${fmtNum(b.skipped)}</td><td class="mono">${fmtNum(b.failed)}</td>
              <td><span class="tag ${b.status === "done" ? "ok" : "info"}">${esc(b.status)}</span></td></tr>`
            )
            .join("") || `<tr><td colspan="6" class="mut">还没有导入记录</td></tr>`
        }</tbody></table></div>`,
      { title: "导入历史", sub: "每一批的去重结果" }
    )}
  </div>
  ${card(
    `<div class="danger-zone">
      <div class="mut" style="font-size:11px;margin:0 0 7px">破坏性操作集中在这一张卡（原「危险区」已并入本卡）：每个入口只此一处，保留原有确认弹窗与后端接口，其它页面不再重复出现。</div>
      <div class="prow"><div><div class="pn">清空门控流水</div><div class="pd" id="hfDangerGateText">读取中…</div></div>
        <div class="pc"><button class="btn s dg" onclick="hfPurge('gate')">清空</button></div></div>
      <div class="prow"><div><div class="pn">清空表达（只清表达）</div><div class="pd" id="hfDangerExprText">读取中…</div></div>
        <div class="pc"><button class="btn s dg" onclick="hfPurge('expressions')">清空表达</button></div></div>
      <div class="prow"><div><div class="pn">重置学习数据（清全部学习）</div><div class="pd" id="hfDangerLearnText">读取中…</div></div>
        <div class="pc"><button class="btn s dg" onclick="hfPurge('learning')">重置</button></div></div>
      <div class="prow"><div><div class="pn">按会话删除记忆</div><div class="pd">填会话 id（如 aiocqhttp:GroupMessage:12345），删掉该会话全部记忆</div></div>
        <div class="pc"><input class="inp" id="hfDangerSession" placeholder="会话 id" style="width:230px"><button class="btn s dg" onclick="hfPurge('session')">删除</button></div></div>
      <div class="prow"><div><div class="pn">衰减 / 淘汰（立即运行）</div><div class="pd">按半衰期降低所有记忆强度；低于阈值的标成 stale（不在图谱与召回里出现）。原在「记忆设置」页头</div></div>
        <div class="pc"><button class="btn s" onclick="hfMemMaintain(this)">立即运行</button></div></div>
      <div class="prow"><div><div class="pn">清理冻结记忆</div><div class="pd">删掉没有任何关系链且已 stale 的记忆（不可恢复）。原在「记忆设置 · 维护」</div></div>
        <div class="pc"><button class="btn s dg" onclick="hfPurgeStale(this)">清理冻结记忆</button></div></div>
      <div class="prow"><div><div class="pn">重建关系链</div><div class="pd">把记忆之间的实体/时序/语义边重新算一遍（幂等，可反复点）</div></div>
        <div class="pc"><button class="btn s" onclick="hfGraphBuild(this)">重建</button></div></div>
    </div>`,
    { title: "数据治理", sub: "破坏性操作合并成一张卡 · 数据库直改，不可恢复；执行前的确认弹窗与原来一致", cls: "sec" }
  )}`;
  return {
    html,
    mount: (host) => {
      const st = stats || {};
      const gateEl = host.querySelector("#hfDangerGateText");
      if (gateEl) gateEl.textContent = `7 天自动滚动，当前 ${fmtNum(st.gate_total || 0)} 条评分记录`;
      const learnEl = host.querySelector("#hfDangerLearnText");
      if (learnEl) learnEl.textContent = `删除全部 ${fmtNum(st.memories || 0)} 条记忆、${fmtNum(st.habits || 0)} 条表达与全部关系链，好感保留。执行前自动备份到 backups/（保留最近 5 份），但仍属不可恢复操作。`;
      const exprEl = host.querySelector("#hfDangerExprText");
      if (exprEl) exprEl.textContent = `把 ${fmtNum(st.habits || 0)} 条表达软删（可恢复）并全部写进「不再学」名单，记忆/关系/好感一律不动。执行前自动备份。`;
      // 域参数分区：各自的小图 / 触发率条在本页挂载（与旧折叠区同一批渲染器）
      [pExp, pGate, pPro, pRec, pFav].forEach((o) => {
        try {
          if (o && typeof o.mount === "function") o.mount(host);
        } catch (err) {
          console.warn("[starheart] 参数分区挂载失败", err);
        }
      });
      // 软刷新（保存 / 切置顶）后还原搜索过滤；输入框 value 已在渲染时还原
      applyParamsFilter();
    },
  };
}

export async function hfSaveConfig(btn) {
  const root = btn && btn.closest("[data-pdom]");
  if (!root) {
    toast("拿不到本域容器，请刷新后重试（不会写入整页）", "err");
    return;
  }
  const patch = {};
  const empty = [];
  root.querySelectorAll("[data-hf-cfg]").forEach((el) => {
    const key = el.getAttribute("data-hf-cfg");
    if (!key) return;
    if (el.type === "checkbox") {
      patch[key] = el.checked;
    } else if (el.type === "number") {
      const raw = String(el.value || "").trim();
      if (!raw || !Number.isFinite(Number(raw))) {
        empty.push(key);
        return;
      }
      patch[key] = Number(raw);
    } else {
      patch[key] = el.value;
    }
  });
  if (empty.length) {
    empty.forEach((k) => {
      const el = root.querySelector(`[data-hf-cfg="${k}"]`);
      const row = el && el.closest(".prow");
      if (row) row.classList.add("guard-bad");
    });
    toast(`有 ${empty.length} 项为空/不是数字：${empty.join("、")}`, "err");
    return;
  }
  if (btn) {
    btn.classList.add("loading");
    btn.disabled = true;
  }
  const res = await apiPostSafe("config/save", { patch });
  if (btn) {
    btn.classList.remove("loading");
    btn.disabled = false;
  }
  if (res.ok) noteConfigTouched(takeConfigDirty(Object.keys(patch)));
  const nw = nativeWriteError(res);
  toast(nw ? `已写入插件库，但原生配置写回失败：${nw}`
           : (res.ok ? `已保存 ${Object.keys(patch).length} 项` : "保存失败：" + res.error),
        nw ? "err" : (res.ok ? "ok" : "err"));
  refreshCurrent();
}

export async function hfImportDetect() {
  const d = await apiSafe("import/detect", {}, {});
  toast(`检测完成：文档 ${d.documents || 0} · 原子 ${d.atoms || 0}`);
  refreshCurrent();
}

let importSub = null;

export async function hfImportStart(all) {
  const layers = [];
  if (($("#hfLayerDoc") || {}).checked) layers.push("documents");
  if (($("#hfLayerAtom") || {}).checked) layers.push("atoms");
  if (!layers.length) {
    toast("至少选一个导入层", "err");
    return;
  }
  const res = await apiPostSafe("import/start", { layers, limit: all ? null : 5 });
  const d = res.data || {};
  if (!res.ok || !d.started) {
    toast("未开始：" + (d.message || res.error || ""), "err");
    return;
  }
  toast("已开始导入，进度实时刷新", "ok");
  const phase = $("#hfImportPhase");
  if (phase) phase.textContent = "已启动…";
  if (importSub) {
    try {
      await bridge.unsubscribeSSE(importSub);
    } catch (err) {
      /* 订阅可能已断，忽略 */
    }
  }
  importSub = await bridge.subscribeSSE(
    EP("import/progress"),
    {
      onMessage(ev) {
        const p = ev.parsed || {};
        const bar = $("#hfImportBar");
        const pct = p.total ? Math.min(100, Math.round(((p.done || 0) / p.total) * 100)) : 0;
        if (bar) bar.style.width = pct + "%";
        const cnt = $("#hfImportCount");
        if (cnt) cnt.textContent = `${p.done || 0} / ${p.total || 0}`;
        const ph = $("#hfImportPhase");
        if (ph) ph.textContent = `${p.phase || ""} ${p.message || ""}${p.imported != null ? ` · 导入 ${p.imported} 跳过 ${p.skipped} 失败 ${p.failed}` : ""}`;
        if (p.phase === "done") {
          toast(`导入完成：新增 ${p.imported || 0} 条`, "ok");
          if (importSub) {
            const sid = importSub;
            importSub = null;
            Promise.resolve(bridge.unsubscribeSSE(sid)).catch(() => {});
          }
          refreshCurrent();
        }
      },
      onError() {
        console.warn("[starheart] 导入进度 SSE 断开");
      },
    },
    {}
  );
}

export async function hfPurge(scope) {
  if (scope === "session") {
    const sid = (($("#hfDangerSession") || {}).value || "").trim();
    if (!sid) {
      toast("先填会话 id", "err");
      return;
    }
    confirmModal("按会话删除记忆", `将删除会话 ${sid} 的全部记忆，不可恢复。`, async () => {
      const res = await apiPostSafe("purge/session", { session_id: sid });
      toast(res.ok ? `已删除 ${(res.data || {}).removed || 0} 条` : "失败：" + res.error, res.ok ? "ok" : "err");
      refreshCurrent();
    });
    return;
  }
  const bakNote = (d) => (d && d.backup ? `已备份：backups/${d.backup}（保留最近 5 份）` : "⚠ 备份没建成（不影响本次操作，但没有回滚副本）");
  if (scope === "expressions") {
    confirmModal(
      "清空表达（只清表达）",
      "会把所有表达软删（可恢复）并全部写进「不再学」名单；记忆、关系链、好感一律不动。执行前自动备份。确认继续？",
      async () => {
        const res = await apiPostSafe("purge/expressions", {});
        const d = res.data || {};
        toast(res.ok ? `已清空：软删 ${fmtNum((d.removed || {}).soft_deleted || 0)} 条 · ${bakNote(d)}` : "失败：" + res.error, res.ok ? "ok" : "err");
        refreshCurrent();
      }
    );
    return;
  }
  const label = scope === "gate" ? "门控流水" : "全部学习数据（记忆 + 表达 + 关系链）";
  const tip =
    scope === "gate"
      ? "不可恢复，确定继续？"
      : "会删除全部记忆、表达与关系链（好感保留）。执行前自动备份到 backups/。二次确认：确定继续？";
  confirmModal(`清空${label}`, tip, async () => {
    const res = await apiPostSafe(scope === "gate" ? "purge/gate" : "purge/learning", {});
    const d = res.data || {};
    toast(res.ok ? `已清空${scope === "gate" ? "" : " · " + bakNote(d)}` : "失败：" + res.error, res.ok ? "ok" : "err");
    refreshCurrent();
  });
}

const CONN_MODES = [
  { v: "white", label: "白名单", hint: "只有名单里的群/人能被StarHeart看到；其余全部当不存在。白名单为空 = 谁都不回。只写插件本地，不改 AstrBot 全局白名单。" },
  { v: "black", label: "黑名单", hint: "勾选 = 对 StarHeart 不存在：不记忆、不学说话、不算好感、不进主动候选。只写插件本地，不改 AstrBot 全局白名单。" },
];

/** 一对一的「可见性名单」编辑器：模式 + 多选 + 确定写入 */
function connCard(kind, title, list, filter) {
  const raw = String((filter.modes || {})[kind] || "black");
  const mode = raw === "black" ? "black" : "white";   // 只有黑白两种；历史 off 归一到白名单
  const meta = CONN_MODES.find((x) => x.v === mode) || CONN_MODES[0];
  const picked = (STATE.pick && STATE.pick[`${kind}:${mode}`]) || new Set();
  const verdicts = filter.verdicts || {};
  const rows = list
    .map((x) => {
      const id = String(x.id || x.group_id || x.user_id || "");
      if (!id) return "";
      const v = verdicts[`${kind}:${id}`] || {};
      // 名字候选：行自带 name / verdict.name / 全局 names 映射 —— **凡是等于号码的一律跳过**
      // （好友行的 name 常常就是号码本身，直接 || 会被短路，界面就成了「号码 号码」）
      const nm = [x.name, v.name, (filter.names || {})[`${kind}:${id}`]]
        .map((s) => String(s == null ? "" : s).trim())
        .find((s) => s && s !== id) || String(id);
      const on = picked.has(id);
      return `<label class="pickrow" data-name="${esc(nm.toLowerCase())}" style="cursor:pointer">
        <input type="checkbox" class="connbox" data-kind="${kind}" data-id="${esc(id)}" ${on ? "checked" : ""}
               onchange="hfConnToggle('${kind}', ${jsStr(id)}, this.checked)">
        <span class="nm">${esc(nm)}</span>
        <span class="mono mut" style="font-size:10.5px">${esc(id)}</span>
        <span class="tag ${v.visible === false ? "err" : v.visible ? "ok" : "gy"}">${v.visible === false ? "不可见" : v.visible ? "可见" : "未判"}</span>
      </label>`;
    })
    .join("");

  return card(
    `<div class="row bt wrap" style="gap:8px;margin-bottom:9px">
      ${segmentHtml(CONN_MODES.map((m) => ({ value: m.v, label: m.label, title: m.hint })), mode, `conn_${kind}`)}
      <span class="mut" style="font-size:11px">${esc(meta.hint)}</span>
      <span class="searchbox" style="width:140px"><i class="ic">⌕</i><input placeholder="过滤…" oninput="hfContactFilter(this.value, '${kind}')"></span>
    </div>
    <div class="row bt wrap" style="gap:6px;margin-bottom:8px">
      <span class="mut" style="font-size:11px">已选 <b id="connCnt_${kind}">${picked.size}</b> / 共 ${list.length} 项</span>
      <button class="btn xs" onclick="hfConnAll('${kind}', true)">全选</button>
      <button class="btn xs" onclick="hfConnAll('${kind}', false)">清空</button>
      <button class="btn p" onclick="hfConnSave('${kind}', this)">保存</button>
    </div>
    ${
      mode === "white" && !picked.size
        ? `<div class="noteblock" style="border-left-color:var(--error);margin-bottom:8px">
             ⚠ <b>白名单是空的</b>：<b>不能直接保存</b>（fail-closed，避免误点后整类静默）。
             想放行谁就勾上它，再点「保存」；也可以
             <button class="btn xs" onclick="hfConnSeedAll('${kind}', this)">全部放行</button>（把列出来的 ${list.length} 项一次加进白名单，之后慢慢删）。
           </div>`
        : ""
    }
    <div class="picklist" data-kind="${kind}" style="max-height:260px">${rows || emptyBlock("平台接口没返回名单（可能是适配器限制）", "☰")}</div>`,
    { title, sub: mode === "black" ? "名单内 = 对 bot 不存在" : mode === "white" ? "只有名单内可见" : "全部可见" }
  );
}

export async function renderContacts() {
  const [contacts, filter, rules] = await Promise.all([
    apiSafe("contacts", {}, { available: false, admins: [], groups: [], friends: [], bots: [], errors: [] }),
    apiSafe("contacts/filter", {}, { modes: {}, black: {}, white: {}, counts: {}, verdicts: {} }),
    apiSafe("proactive/rules", {}, { sessions: [] }),
  ]);
  if ((contacts && contacts.__err) || (filter && filter.__err)) {
    const why = (contacts && contacts.__err) || (filter && filter.__err);
    return {
      html: pageHead("联系人读取失败", `接口现在拿不到名单：${esc(String(why))}`,
        `<button class="btn p" onclick="hfRefresh()">重试</button>`, "⚠")
        + card(`<div class="fnote">⚠ 不渲染空名单，避免把「读取失败」误当成「没有联系人」再保存一次。</div>`,
               { title: "读取失败" }),
    };
  }
  STATE.contacts = contacts;
  STATE.filter = filter;
  STATE.rules = rules;
  // 多选状态：四份（群/私聊 × 黑/白）各自独立，切模式不会串味
  STATE.pick = STATE.pick || {};
  for (const kind of ["group", "private"]) {
    for (const mode of ["black", "white"]) {
      const key = `${kind}:${mode}`;
      if (!STATE.pick[key]) {
        STATE.pick[key] = new Set(((mode === "black" ? (filter.black || {}) : (filter.white || {}))[kind] || []).map(String));
      }
    }
  }
  const groups = contacts.groups || [];
  const friends = contacts.friends || [];
  const pk = { group: [], private: [] };
  (rules.sessions || []).forEach((x) => {
    if (x.verdict === "deny") pk[x.kind] = pk[x.kind] || [];
  });

  const html = `
  ${pageHead(
    "名单与可见性",
    "这里是「StarHeart能看到谁」的总闸门，只有<b>黑名单</b>和<b>白名单</b>两种：黑名单内 = 对 bot 不存在；白名单 = 只有名单内可见，<b>白名单为空 = 谁都不回</b>（fail-closed）。主动消息的黑白名单是在这之上的收窄；管理员名单读的是 AstrBot 原生配置；<b>这里的名单只写插件本地</b>，不改 AstrBot 全局「平台配置 → 白名单」，避免影响其他插件与私聊。",
    `<button class="btn" onclick="hfContactsRefresh(this)">重新拉取</button>
     <button class="btn s" onclick="goPage('messages','proactive')">主动设置 →</button>`,
    "☰"
  )}
  ${
    contacts.derived
      ? `<div class="noteblock" style="border-left-color:var(--warning);margin-bottom:12px">
           平台接口没返回群/好友列表（可能是适配器限制），下面用的是<b>你自己聊过的会话</b>兜底 —— 一样能勾选、能写入名单。
           ${(contacts.errors || []).length ? `<div class="mut" style="font-size:10.5px;margin-top:6px">${esc((contacts.errors || []).join(" / "))}</div>` : ""}
         </div>`
      : ""
  }
  ${kpis(
    kpiCard("ctG", fmtNum(groups.length), "群聊")
    + kpiCard("ctF", fmtNum(friends.length), "好友")
    + kpiCard("ctB", fmtNum((contacts.bots || []).length), "机器人账号")
    + kpiCard("ctA", fmtNum((contacts.admins || []).length), "管理员")
  )}
  <div class="grid g2 sec">
    ${connCard("group", "群聊可见性", groups, filter)}
    ${connCard("private", "私聊可见性", friends, filter)}
  </div>
  ${card(
    `<div class="chips">${(contacts.admins || []).map((a) => `<span class="chip">🛡 ${esc(a.name || a.id || a)}</span>`).join("") || '<span class="mut">没有读到管理员</span>'}</div>
     <div class="mut" style="font-size:11px;margin-top:9px">机器人自己：${(contacts.bots || []).map((b) => esc(b.name || b.id || b)).join("、") || "—"}（已自动排除出人物画像）</div>
     ${(contacts.errors || []).length ? `<div class="mut" style="font-size:11px;margin-top:8px;color:var(--warn-tx)">接口警告：${esc((contacts.errors || []).join(" / "))}</div>` : ""}`,
    { title: "管理员与机器人自己", sub: "只读" }
  )}`;
  return {
    html,
    mount: (host) => {
      // 模式选择是分段控件：点一下立刻落库（切换只看模式，名单本身留着）
      ["group", "private"].forEach((kind) => {
        const seg = host.querySelector(`[data-seg="conn_${kind}"]`);
        if (!seg) return;
        seg.querySelectorAll(".sg").forEach((b) =>
          b.addEventListener("click", () => {
            if (b.classList.contains("on")) return;
            hfConnMode(kind, b.dataset.v);
          })
        );
        syncSeg(seg);
      });
    },
  };
}

/** 勾选/取消一项（不动服务端，等「确定写入」） */
export function hfConnToggle(kind, id, on) {
  const mode = String((STATE.filter && STATE.filter.modes && STATE.filter.modes[kind]) || "off");
  const key = `${kind}:${mode}`;
  STATE.pick = STATE.pick || {};
  STATE.pick[key] = STATE.pick[key] || new Set();
  if (on) STATE.pick[key].add(String(id));
  else STATE.pick[key].delete(String(id));
  const cnt = document.querySelector(`#connCnt_${kind}`);
  if (cnt) cnt.textContent = String(STATE.pick[key].size);
}

/** 全选 / 清空（只改本地勾选，仍需「确定写入」） */
export function hfConnAll(kind, on) {
  const mode = String((STATE.filter && STATE.filter.modes && STATE.filter.modes[kind]) || "off");
  const key = `${kind}:${mode}`;
  const boxes = $$(`.connbox[data-kind="${kind}"]`);
  STATE.pick[key] = new Set();
  boxes.forEach((b) => {
    b.checked = !!on;
    if (on) STATE.pick[key].add(String(b.getAttribute("data-id")));
  });
  const cnt = document.querySelector(`#connCnt_${kind}`);
  if (cnt) cnt.textContent = String(STATE.pick[key].size);
}

/** 切换模式（服务端把两份名单都留着，切回来不丢） */
export async function hfConnMode(kind, mode) {
  const filter = STATE.filter || {};
  const m = mode === "black" ? "black" : "white";
  const ids = ((m === "black" ? (filter.black || {}) : (filter.white || {}))[kind] || []).map(String);
  const res = await apiPostSafe("contacts/set", { kind, mode: m, ids });
  if (!res.ok) {
    toast("切换失败：" + res.error, "err");
    return;
  }
  STATE.pick = STATE.pick || {};
  STATE.pick[`${kind}:${m}`] = new Set(ids);
  toast(m === "black" ? "已切到黑名单：名单内对 bot 不存在" : "已切到白名单：只有名单内可见", "ok");
  refreshCurrent();
}

/** 确定写入：多选的结果一次提交（不用一个一个点） */
export async function hfConnSave(kind, btn) {
  const raw = String((STATE.filter && STATE.filter.modes && STATE.filter.modes[kind]) || "black");
  const mode = raw === "black" ? "black" : "white";
  const ids = Array.from((STATE.pick && STATE.pick[`${kind}:${mode}`]) || []);
  if (btn) btn.classList.add("loading");
  const res = await apiPostSafe("contacts/set", { kind, mode, ids });
  if (btn) btn.classList.remove("loading");
  if (!res.ok) {
    toast("写入失败：" + res.error, "err");
    return;
  }
  STATE.filter = res.data || STATE.filter;
  const warn = mode === "white" && !ids.length ? "（白名单为空 = 谁都不回）" : "";
  toast(`已保存${kind === "group" ? "群聊" : "私聊"}${mode === "black" ? "黑名单" : "白名单"}：${ids.length} 项${warn}`, mode === "black" || ids.length ? "ok" : "err");
  refreshCurrent();
}

/** 白名单空选时的一键放行：把当前列出来的全部写进白名单（等于「先能用，再慢慢删」） */
export async function hfConnSeedAll(kind, btn) {
  const ids = $$(`.connbox[data-kind="${kind}"]`).map((b) => String(b.getAttribute("data-id") || "")).filter(Boolean);
  if (!ids.length) {
    toast("这个范围没有可放行的项", "err");
    return;
  }
  if (btn) btn.classList.add("loading");
  const res = await apiPostSafe("contacts/set", { kind, mode: "white", ids });
  if (btn) btn.classList.remove("loading");
  if (!res.ok) {
    toast("写入失败：" + res.error, "err");
    return;
  }
  STATE.filter = res.data || STATE.filter;
  STATE.pick = STATE.pick || {};
  STATE.pick[`${kind}:white`] = new Set(ids);
  toast(`已放行 ${ids.length} 项（${kind === "group" ? "群聊" : "私聊"}白名单）`, "ok");
  refreshCurrent();
}

export function hfContactFilter(v, kind) {
  const q = String(v || "").trim().toLowerCase();
  $$(`.picklist[data-kind="${kind}"] .pickrow`).forEach((row) => {
    const name = row.getAttribute("data-name") || "";
    const text = row.textContent.toLowerCase();
    row.style.display = !q || name.includes(q) || text.includes(q) ? "" : "none";
  });
}

export async function hfContactsRefresh(btn) {
  if (btn) btn.classList.add("loading");
  const res = await apiSafe("contacts", { force: "1" }, {});
  if (btn) btn.classList.remove("loading");
  toast(`已重新拉取：群 ${(res.groups || []).length} · 好友 ${(res.friends || []).length}`, "ok");
  refreshCurrent();
}

/* 面板跑在 AstrBot 的 iframe 里，URL 会带 ?locale=zh-CN。
   动态文案跟随 AstrBot 系统语言：这里读出 locale，中/英各一份标签。 */
const HF_LOCALE = (() => {
  try {
    const u = new URLSearchParams(location.search);
    return String(u.get("locale") || u.get("lang") || "zh-CN");
  } catch (err) {
    return "zh-CN";
  }
})();
export const HF_LANG = /^en/i.test(HF_LOCALE) ? "en" : "zh";
const L = (zh, en) => (HF_LANG === "en" ? en : zh);

/** Token 流水里的任务键 → 中文名（面板上再按语言取词） */
const TASK_LABEL = {
  "est:inject": "估算·注入上下文（非真实计费）",
  memory_summary: L("记忆总结", "Memory summary"),
  memory_extract: L("记忆提取", "Memory extract"),
  expression_extract: L("表达提取", "Expression extract"),
  person_profile: L("画像（人物）", "Profiles (person)"),
  group_profile: L("画像（群聊）", "Profiles (group)"),
  proactive: L("主动开口", "Proactive"),
  persona_evolve: L("人格进化", "Persona evolve"),
  forget: L("记忆遗忘评估", "Forget review"),
  rerank: L("结果重排", "Rerank"),
  embedding: L("向量嵌入", "Embedding"),
  import: L("导入解析", "Import parse"),
};
const taskCn = (k) => {
  const raw = String(k || "chat");
  const short = raw.includes(":") ? raw.split(":").pop() : raw;   // 兼容 bg:xxx 这种带前缀的写法
  return TASK_LABEL[short] || TASK_LABEL[raw] || short;
};

const MODEL_TASKS = [
  ["memory_summary", L("◍ 记忆总结", "◍ Memory summary"), L("对话 → 摘要 + 召回线索", "Dialogue → summary + cues"), "llm"],
  ["expression_extract", L("✎ 表达提取", "✎ Expression extract"), L("聊天 → 情景/表达方式", "Chat → scenes / expressions"), "llm"],
  // 画像 = 人物（L0）+ 群聊（L1）同一个模型槽；两段系统提示词都在这张卡里展开改（用户要求：不拆成两张卡）
  ["person_profile", L("☺ 画像", "☺ Profiles"), L("把一个人 / 一个群的记忆汇总成画像（含自动刷新）", "Turn a person's or a group's memories into a profile (auto-refresh)"), "llm", ["group_profile"]],
  ["persona_evolve", L("☺ 人格进化", "☺ Persona evolve"), L("按近期素材改写当前人格的表达（默认要你确认才写回；建议锁便宜模型）", "Rewrite the current persona (needs your approval by default; a cheaper model is recommended)"), "llm"],
  ["forget", L("⌛ 记忆遗忘评估", "⌛ Forget review"), L("定期评估哪些记忆该归档 / 该遗忘（配了就用模型筛候选，没配则纯阈值；建议锁便宜模型）", "Decide which memories to archive / forget (LLM narrows the threshold candidates; falls back to plain thresholds)"), "llm"],
  ["embedding", L("⬡ 向量嵌入", "⬡ Embedding"), L("记忆召回的 embedding", "Embedding for recall"), "embedding"],
  ["rerank", L("≡ 结果重排", "≡ Rerank"), L("召回候选交给它重新定序（配了才生效）", "Reorder recall candidates (only when configured)"), "rerank"],
];

let MODELS_CFG_READY = false;

export async function renderModels() {

  const [prov, cfg, prompts] = await Promise.all([
    apiSafe("providers", {}, {}),
    apiSafe("config", {}, {}),
    // 插件自管任务的系统提示词清单（对话/主动开口/图像理解不在此列 —— 它们走 AstrBot 原生提示词）
    apiSafe("prompts", {}, { items: [] }),
  ]);
  if (cfg && cfg.__err) {
    MODELS_CFG_READY = false;
    return {
      html: pageHead("模型配置读取失败", `接口现在拿不到已保存的模型绑定：${esc(String(cfg.__err))}`,
        `<button class="btn p" onclick="hfRefresh()">重试</button>`, "⚠")
        + card(`<div class="fnote">⚠ 为避免把 7 个模型绑定清空，本次不渲染下拉。请重试。</div>`,
               { title: "读取失败" }),
    };
  }
  MODELS_CFG_READY = true;
  const plist = prompts.items || [];
  const promptErrHtml = plist.length
    ? ""
    : '<div class="mut" style="font-size:11px;margin-bottom:8px">读不到系统提示词清单（后端接口异常），下面卡片里就不会显示提示词框。</div>';
  const promptMap = {};
  plist.forEach((x) => (promptMap[x.task] = x));
  const promptBlock = (key) => {
    const pr = promptMap[key];
    if (!pr) return "";
    return `<details class="hf-pr" style="margin-top:8px">
          <summary style="font-size:11.5px;display:flex;align-items:center;gap:7px">
            <span class="tag ${pr.edited ? "ok" : "gy"}">${esc(pr.label || pr.task)} · 系统提示词 ${pr.edited ? "已自定义" : "内置默认"}</span>
            <span class="mut">点这里展开看 / 改</span>
          </summary>
          ${pr.desc ? `<div class="mut" style="font-size:11px;margin-top:5px">${esc(pr.desc)}</div>` : ""}
          <textarea class="inp" rows="8" data-hf-prompt="${esc(key)}" data-hf-default="${esc(pr.default)}" style="margin-top:6px;font-size:11.5px">${esc(pr.effective)}</textarea>
          ${pr.user_key ? `<div class="mut" style="font-size:11px;margin-top:6px">用户模板（${esc(pr.user_label || pr.user_key)}）</div>
          <textarea class="inp" rows="4" data-hf-userprompt="${esc(pr.user_key)}" style="margin-top:4px;font-size:11.5px">${esc(pr.user_value)}</textarea>` : ""}
          <div class="row bt" style="margin-top:6px">
            <span class="mut" style="font-size:11px" id="hfPromptMsg_${esc(key)}"></span>
            <span class="row" style="gap:6px">
              <button class="btn xs" onclick="hfPromptReset(${jsStr(key)}, this)">恢复默认</button>
              <button class="btn xs p" onclick="hfPromptSave(${jsStr(key)}, this)">保存</button>
            </span>
          </div>
          <div class="mut" style="font-size:11px;margin-top:4px">留空 = 用内置默认，可自由改（想改成什么风格都随你）。只影响这个后台任务，<b>不影响对话回复</b>。</div>
        </details>`;
  };

  const cur = prov.current || {};
  const p = prov || {};
  const html = `
  ${pageHead(
    "模型分配",
    `每个任务各选各的模型 · 下拉读自 AstrBot 已配置的提供商 · 对话类任务留空 = 跟随会话当前模型${cur.id ? `（当前 ${esc(cur.id)}）` : ""}；嵌入/重排留空 = 跟随 AstrBot 已配置的那一个`,
    `${p.available ? "" : '<span class="tag warn">读不到模型列表</span>'}<button class="btn p" onclick="hfSaveModels(this)">保存分配</button>`,
    "⬡"
  )}
  <div class="grid g23 sec">
    ${card(`<div id="mdNet"></div>`, { title: "任务 → 提供商 绑定图", sub: "中间是任务，外围是你配的提供商；亮线=已锁定，虚线=跟随会话" })}
    ${card(
      `<div class="mut" style="font-size:11.5px;line-height:1.8">
        · 不锁定：跟着会话当前模型走，换模型不用回来改<br>
        · 记忆总结这类后台重活建议锁一个便宜模型，避免消耗对话模型额度<br>
        · 嵌入 / 重排留空会跟随 AstrBot 已配置的那一个；<b>锁死更稳</b>（换会话时向量维度不会变）</div>
       <div class="row" style="gap:6px;margin-top:10px"><button class="btn s" onclick="hfTestAllModels(this)">逐个试跑</button></div>`,
      { title: "怎么配" }
    )}
  </div>
  <div class="grid g2 sec">
    ${MODEL_TASKS.map(([key, title, desc, kind, extraPrompts]) => {
      const list = p[kind] || [];
      const value = cfg["model_" + key] || "";
      // 工具槽位（嵌入/重排）留空是「跟随 AstrBot 已配置的那个」，不是"跟随会话当前模型"——
      // 对话模型没有 get_embedding/rerank，按"跟随会话"选等于把功能静默关掉（以前正是这么标的）。
      const isTool = key === "embedding" || key === "rerank";
      const followLabel = isTool
        ? `跟随 AstrBot 已配置的${key === "embedding" ? "嵌入" : "重排"}提供商（取第一个）`
        : "跟随会话当前模型（推荐）";
      return `${key === "memory_summary" ? promptErrHtml : ""}<div class="card cp hov">
        <div class="row bt" style="margin-bottom:8px">
          <span style="font-weight:700;font-size:13px">${esc(title)}</span>
          <span class="tag ${list.length ? "ok" : "gy"}">${list.length ? `● ${list.length} 个可选` : "该类型未配置"}</span>
        </div>
        <div class="mut" style="font-size:11px;margin-bottom:8px">${esc(desc)}</div>
        <select class="inp" style="width:100%" data-hf-model="${key}" ${list.length ? "" : "disabled"}>
          <option value="" ${value ? "" : "selected"}>${followLabel}</option>
          ${list.map((x) => `<option value="${esc(x.id)}" ${x.id === value ? "selected" : ""}>${esc(x.id)}${x.model ? " · " + esc(x.model) : ""}</option>`).join("")}
        </select>
        <div class="row bt" style="margin-top:8px">
          <span class="mut" style="font-size:11px" id="hfModelMsg_${key}">${value ? "已锁定：" + esc(value) : "未锁定（跟随会话）"}</span>
          <button class="btn xs" onclick="hfTestModel(${jsStr(key)}, this)">试跑</button>
        </div>
        ${key === "embedding" ? `<div class="mut" style="font-size:11px;margin-top:6px" id="hfDim_embedding">向量维度：读取中…</div>` : ""}${promptBlock(key)}${(extraPrompts || []).map((x) => promptBlock(x)).join("")}</div>`;
    }).join("")}
  </div>`;
  return {
    html,
    mount: (host) => {
      const assigns = {};
      MODEL_TASKS.forEach(([key, , , kind]) => {
        const v = cfg["model_" + key] || "";
        if (v) assigns[key] = v;
      });
      // 当前向量模型的维度（来自向量库各代：换模型=换库，这里让用户看得见）
      (async () => {
        try {
          const d = await apiSafe("model/dims", {}, { dims: {}, active_gen: "" });
          const el = host.querySelector("#hfDim_embedding");
          if (!el) return;
          const cur = cfg["model_embedding"] || "";
          const norm = (x) => String(x || "").toLowerCase().split("/").pop();
          const dim = (d.dims || {})[norm(cur)];
          el.textContent = dim
            ? `当前向量维度：${dim} 维（用向量库「${d.active_gen || cur}」这一代）`
            : "当前向量维度：未知（还没有该模型的向量，回填时自动建立，旧代保留）";
        } catch (err) { /* 维度查不到不影响模型分配 */ }
      })();
      const providersUsed = Array.from(new Set(Object.values(assigns)));
      mount(host, "#mdNet", (el) =>
        C.miniNetwork(el, {
          nodes: [
            ...MODEL_TASKS.map(([key, title]) => ({ id: `T:${key}`, label: title.replace(/^\S+\s/, ""), color: "#3c96ca", group: "task", r: 10 })),
            ...providersUsed.map((pid) => ({ id: `P:${pid}`, label: pid, color: "#3aa76d", group: "provider", r: 13 })),
          ],
          links: Object.keys(assigns).map((k) => ({ s: `T:${k}`, t: `P:${assigns[k]}`, w: 2 })),
          size: 240,
          interactive: true,
        })
      );
    },
  };
}

export async function hfSaveModels(btn) {
  if (!MODELS_CFG_READY) {
    toast("模型配置没读到，本次不保存（避免把绑定清空）", "err");
    return;
  }
  const patch = {};
  // 只提交「真的能选」的下拉：providers 读取失败时下拉会是 disabled/空，这时提交会把已存绑定清空
  $$("[data-hf-model]").forEach((el) => {
    if (el.disabled) return;
    if (!el.options || el.options.length <= 1) return;   // 只剩「跟随…」那一项 = 该类型没读到模型，别拿它覆盖
    patch["model_" + el.getAttribute("data-hf-model")] = el.value || "";
  });
  if (!Object.keys(patch).length) {
    if (btn) btn.classList.remove("loading");
    toast("没读到可用模型列表，这次不保存（避免把已保存的绑定清空）", "err");
    return;
  }
  if (btn) btn.classList.add("loading");
  const res = await apiPostSafe("config/save", { patch });
  if (btn) btn.classList.remove("loading");
  toast(res.ok ? "模型分配已保存" : "保存失败：" + res.error, res.ok ? "ok" : "err");
  Object.keys(patch).forEach((k) => {
    const msg = document.getElementById("hfModelMsg_" + k.replace("model_", ""));
    if (msg) msg.textContent = patch[k] ? "已锁定：" + patch[k] : "未锁定（跟随会话）";
  });
}

export async function hfPromptSave(task, btn) {
  const ta = document.querySelector(`[data-hf-prompt="${task}"]`);
  const msg = document.getElementById("hfPromptMsg_" + task);
  if (!ta) return;
  if (btn) btn.classList.add("loading");
  const res = await apiPostSafe("prompt/save", { task, system: ta.value });
  // 用户模板（例如记忆总结的 {transcript} 模板）走同一套 config/save
  const patch = {};
  // 只提交「本任务卡片内」的用户模板（以前会把整页所有模板一起提交）
  const scope = ta.closest(".card") || document;
  scope.querySelectorAll("[data-hf-userprompt]").forEach((el) => (patch[el.getAttribute("data-hf-userprompt")] = el.value));
  const res2 = Object.keys(patch).length ? await apiPostSafe("config/save", { patch }) : { ok: true };
  if (btn) btn.classList.remove("loading");
  const ok = res.ok && res2.ok;
  const nw = ok ? nativeWriteError(res2) : "";
  if (msg) {
    msg.textContent = ok
      ? (nw ? "✓ 已写入插件库，但原生配置写回失败：" + nw
            : "✓ 已保存" + (ta.value.trim() ? "（自定义）" : "（用内置默认）"))
      : "✗ " + (res.error || res2.error || "保存失败");
    msg.style.color = (ok && !nw) ? "var(--success)" : "var(--error)";
  }
}

export async function hfPromptReset(task, btn) {
  const ta = document.querySelector(`[data-hf-prompt="${task}"]`);
  const msg = document.getElementById("hfPromptMsg_" + task);
  if (!ta) return;
  ta.value = ta.getAttribute("data-hf-default") || "";
  if (btn) btn.classList.add("loading");
  const res = await apiPostSafe("prompt/save", { task, system: "" });
  if (btn) btn.classList.remove("loading");
  if (msg) {
    msg.textContent = res.ok ? "✓ 已恢复内置默认" : "✗ " + (res.error || "失败");
    msg.style.color = res.ok ? "var(--success)" : "var(--error)";
  }
}

export async function hfTestAllModels(btn) {
  if (btn) {
    btn.classList.add("loading");
    btn.disabled = true;
  }
  for (const [key] of MODEL_TASKS) {
    await hfTestModel(key, null);
  }
  if (btn) {
    btn.classList.remove("loading");
    btn.disabled = false;
  }
}

export async function hfTestModel(key, btn) {
  const sel = document.querySelector(`[data-hf-model="${key}"]`);
  const pid = sel ? sel.value : "";
  const msg = document.getElementById("hfModelMsg_" + key);
  if (btn) btn.classList.add("loading");
  if (msg) msg.textContent = "试跑中…";
  const res = await apiPostSafe("model/test", { task: key, provider_id: pid });
  const d = res.data || {};
  if (btn) btn.classList.remove("loading");
  if (msg) {
    msg.textContent = res.ok && d.ok ? `✓ ${d.ms}ms · ${String(d.reply || "").slice(0, 40)}` : `✗ ${d.error || res.error || "失败"}`;
    msg.style.color = res.ok && d.ok ? "var(--success)" : "var(--error)";
  }
}

export async function renderTokens() {
  const data = await apiSafe("tokens", { days: 7 }, { items: [], days: 7, total_in: 0, total_out: 0, total_cached: 0, total_cost: 0 });
  const items = data.items || [];
  const byTask = {};
  items.forEach((x) => {
    const t = taskCn(x.task);            // 面板上显示中文任务名，不再露内部 key
    byTask[t] = (byTask[t] || 0) + (x.i || 0) + (x.o || 0);
  });
  const byModel = {};
  items.forEach((x) => {
    const m = x.model || "跟随会话";
    byModel[m] = (byModel[m] || 0) + (x.i || 0) + (x.o || 0);
  });
  const html = `
  ${pageHead("Token 消耗", `最近 ${data.days} 天的真实用量（来自 hf_tokens 流水）`, `<span class="tag gy">缓存命中 ${fmtNum(data.total_cached)}</span>`, "◧")}
  ${kpis(
    kpiCard("tkIn", fmtNum(data.total_in), "输入 tokens", data.est_in ? `<span class="mut">另有估算注入 ${fmtNum(data.est_in)}（单列，不计入）</span>` : "")
    + kpiCard("tkOut", fmtNum(data.total_out), "输出 tokens")
    + kpiCard("tkCache", fmtNum(data.total_cached), "命中缓存")
    + kpiCard("tkCost", data.has_pricing ? Number(data.total_cost).toFixed(4) : "未接入", "累计花费", data.has_pricing ? "" : `<span class="mut">插件侧没接计费，不是真的 0</span>`)
  )}
  <div class="grid g23 sec">
    ${card(`<div id="tkTask"></div>`, { title: "按任务", sub: "哪个任务在烧 token" })}
    ${card(`<div id="tkModel"></div>`, { title: "按模型", sub: "token 落在哪个模型上" })}
  </div>
  ${card(
    `<div style="overflow-x:auto"><table><thead><tr><th>任务</th><th>模型</th><th>输入</th><th>输出</th><th>缓存</th><th>花费</th><th>调用次数</th></tr></thead>
      <tbody>${
        items.length
          ? items
              .map(
                (x) => `<tr><td>${esc(taskCn(x.task))}</td><td class="mono">${esc(x.model || "跟随会话")}</td>
          <td class="mono">${fmtNum(x.i)}</td><td class="mono">${fmtNum(x.o)}</td><td class="mono">${fmtNum(x.c)}</td>
          <td class="mono">${!data.has_pricing || x.cost == null ? "—" : Number(x.cost).toFixed(4)}</td><td class="mono">${fmtNum(x.n)}</td></tr>`
              )
              .join("")
          : `<tr><td colspan="7" class="mut">还没有 token 记录</td></tr>`
      }</tbody></table></div>`,
    { title: "明细", sub: "按任务 + 模型聚合" }
  )}`;
  return {
    html,
    mount: (host) => {
      mount(host, "#tkTask", (el) => C.bars(el, { items: Object.keys(byTask).map((k) => ({ label: k, value: byTask[k], color: "#03c9d7" })) }));
      mount(host, "#tkModel", (el) =>
        C.barSplit(el, {
          items: Object.keys(byModel).map((k, i) => ({ label: k, value: byModel[k], color: ["#3c96ca", "#03c9d7", "#8b5cf6", "#e0559a", "#f59e0b"][i % 5] })),
          height: 18,
        })
      );
    },
  };
}

/* 顶栏人格查看视角：读真人格列表 + 记忆计数 */
export async function refreshPersonaSwitch() {
  const menu = $("#pswMenu");
  if (!menu) return;
  const p = await apiSafe("personas", {}, { available: false, items: [], active: "", counts: {} });
  STATE.personas = p;
  const counts = p.counts || {};
  const active = String(p.active || "");
  const label = $("#pswitchLabel");
  const dot = $("#pswitchDot");
  if (!STATE.personaViewTouched) STATE.personaView = active;
  if (label) label.textContent = STATE.personaView ? STATE.personaView : "全部人格";
  if (dot) dot.classList.toggle("on", !!STATE.personaView && STATE.personaView === active);
  const rows = [
    { id: "", name: "全部人格（不过滤）", n: Object.values(counts).reduce((s, v) => s + v, 0) },
    ...(p.items || []).map((x) => ({ id: x.persona_id, name: x.persona_id, n: counts[x.persona_id] || 0, isActive: x.persona_id === active })),
  ];
  if (Object.prototype.hasOwnProperty.call(counts, "")) rows.push({ id: "__none__", name: "（未归属人格的旧数据）", n: counts[""] || 0 });
  menu.innerHTML =
    `<div class="hd">记忆查看视角 · 只影响面板显示</div>` +
    rows
      .map(
        (r) => `<button type="button" class="psw-item ${String(STATE.personaView) === String(r.id) ? "sel" : ""}" onclick="hfPersonaPickView(${jsStr(r.id)})">
      <span class="pdot ${r.isActive ? "on" : ""}"></span><span class="n">${esc(r.name)}</span><span class="c">${fmtNum(r.n)}</span></button>`
      )
      .join("");
}

export function hfPersonaPickView(pid) {
  STATE.personaView = pid || "";
  STATE.personaViewTouched = true;
  const label = $("#pswitchLabel");
  if (label) label.textContent = STATE.personaView === "__none__" ? "未归属" : STATE.personaView || "全部人格";
  const det = $("#personaSwitch");
  if (det) det.removeAttribute("open");
  toast(`查看视角：${STATE.personaView === "__none__" ? "未归属" : STATE.personaView || "全部人格"}`);
  refreshCurrent();
  refreshPersonaSwitch();
}


/* ================= 语言学习：回复方式（带记忆出处） =================
   一个组件 = 一种回复方式；组件里是逐字原话，每条挂「记忆 #id · 时间」，点开看原对话。
   学习输入 = 记忆里的原对话；不准的原话可丢弃（掉出池子），变旧的方式仍然会被触发。 */

function _qRows(st) {
  const qs = (st.quotes || []).filter((q) => q.status === "active").slice(0, 6);
  if (!qs.length) return '<div class="pd" style="padding:10px 0">（这种方式还没有原话）</div>';
  return qs.map((q) => {
    const t = C.fmtTs(q.created_at);
    const who = q.user_name ? `${esc(q.user_name)}（${esc(String(q.user_id || ""))}）` : "用户";
    const dlg = (q.trigger_text || q.quote)
      ? `<div class="dlg"><div class="u">${esc(who)}：${esc(q.trigger_text || "（原话缺失）")}</div>
           <div class="a">bot：${esc(q.quote)}</div></div>`
      : "";
    return `<div class="smp" onclick="this.classList.toggle('open')">
      <div class="srow"><span class="qt" title="${esc(q.quote)}">「${esc(q.quote)}」</span>
        <span class="mem">记忆 #${fmtNum(q.memory_id)} · ${t}</span><span class="cv">▸</span></div>
      ${dlg}
      <div class="sact">
        <button class="btn xs" data-q="${esc(q.quote)}" onclick="event.stopPropagation();hfQuoteEdit(${Number(q.id)},this)">改</button>
        <button class="btn xs" onclick="event.stopPropagation();hfQuoteDel(${Number(q.id)})">删</button>
      </div>
    </div>`;
  }).join("");
}

function _styleCard(st) {
  const active = Number(st.active || 0);
  const paused = st.status === "paused";
  const tag = paused ? '<span class="tag">已停用</span>' : '<span class="tag ok">在用</span>';
  return `<div class="card sty">
    <div class="hd"><b>${esc(st.name)}</b><span style="flex:1"></span>${tag}</div>
    <div class="meta">${fmtNum(active)} 条原话 · 命中 ${fmtNum(st.hits || 0)} · 纠正 ${fmtNum(st.corrections || 0)}</div>
    <div class="smps">${_qRows(st)}</div>
    <div class="ft">
      <button class="btn xs" onclick="hfStyleRename(${Number(st.id)},this)">改名</button>
      <button class="btn xs" onclick="hfStyleStatus(${Number(st.id)},'${paused ? "active" : "paused"}')">${paused ? "启用" : "停用"}</button>
      <button class="btn xs" onclick="hfStyleDel(${Number(st.id)},this)">删除</button>
    </div>
  </div>`;
}

function _stylePage(sty) {
  const on = !!sty.enabled;
  const c = sty.counts || {};
  const styles = sty.styles || [];
  const terms = sty.terms || [];
  const chip = on ? '<span class="tag ok">生效中</span>' : '<span class="tag">已关闭</span>';
  return `<style>
.ec-wrap{display:flex;flex-direction:column;gap:12px;width:100%;margin:0}
.ec-styles{display:grid;grid-template-columns:repeat(auto-fill,minmax(330px,1fr));gap:12px;align-items:start}
.sty{padding:13px 15px}
.sty .hd{display:flex;align-items:center;gap:8px}
.sty .hd b{font-size:13.5px;font-weight:650}
.sty .meta{font-size:10.5px;color:var(--tx3);margin-top:3px}
.smps{margin:9px 0 2px;display:flex;flex-direction:column;gap:6px}
.smp{border:1px solid var(--bd);border-radius:8px;overflow:hidden;background:var(--surface2)}
.srow{display:flex;align-items:center;gap:8px;padding:7px 9px;cursor:pointer}
.srow .qt{flex:1;min-width:0;font-size:12.5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.srow .mem{font-size:10px;color:var(--tx3);white-space:nowrap}
.srow .cv{font-size:10px;color:var(--tx3);transition:transform .15s}
.smp.open .srow .cv{transform:rotate(90deg)}
.dlg{display:none;padding:0 9px 6px;font-size:11.5px;line-height:1.7}
.smp.open .dlg{display:block}
.dlg .u{color:var(--tx3)} .dlg .a{color:var(--tx1)}
.sact{display:none;padding:0 9px 8px;gap:6px}
.smp.open .sact{display:flex}
.sty .ft{display:flex;gap:6px;margin-top:8px;flex-wrap:wrap}
.ask-mask{position:fixed;inset:0;z-index:9999;display:flex;align-items:center;justify-content:center;background:rgba(0,0,0,.45)}
.ask-box{width:min(520px,90vw);background:var(--card);border:1px solid var(--bd);border-radius:12px;padding:16px;box-shadow:0 24px 60px rgba(0,0,0,.5)}
.ask-box .t{font-size:13px;font-weight:650;margin-bottom:10px}
.ask-box textarea{width:100%;box-sizing:border-box;min-height:40px;background:var(--surface2);color:var(--tx1);border:1px solid var(--bd);border-radius:8px;padding:8px 10px;font:inherit;font-size:13px;resize:vertical}
.ask-box .b{display:flex;gap:8px;justify-content:flex-end;margin-top:12px}
</style>
<div class="ec-wrap">
  <div class="card sec" style="padding:14px 16px">
    <div class="row bt wrap" style="gap:10px">
      <div style="min-width:0">
        <div class="pn" style="font-size:16px">语言学习</div>
        <div class="pd">对话先落成记忆，再从记忆里的原对话学怎么回；每条原话都能点回那条记忆</div>
      </div>
      <div class="row" style="gap:6px;flex-wrap:wrap">
        ${chip}
        <button class="btn xs" onclick="hfStyleLearn(this)">立即学一批</button>
        <button class="btn xs${on ? " on" : ""}" onclick="hfStyleToggle(${on ? "false" : "true"})">${on ? "学习已开启" : "学习已关闭"}</button>
      </div>
    </div>
    <div class="pd" style="margin-top:8px">${fmtNum(c.styles)} 种回复方式 · ${fmtNum(c.quotes)} 条原话 · 黑话 ${fmtNum(c.terms)} 条</div>
  </div>

  <div class="ec-styles">${styles.map(_styleCard).join("") || '<div class="card sec" style="padding:20px"><div class="pd">还没有学到的回复方式。点「立即学一批」从记忆里的原对话学。</div></div>'}</div>

  <div class="card sec" style="padding:14px 16px">
    <div class="row bt"><div><div class="pn">黑话</div><div class="pd">命中才进提示词；词、释义、范围随时能改，也能删</div></div></div>
    <div style="margin-top:6px">
      ${(terms || []).map((t) => `<div class="row" style="gap:8px;padding:6px 0;border-bottom:1px solid var(--bd)">
        <b style="min-width:80px">${esc(String(t.term || ""))}</b>
        <span class="pd" style="flex:1;min-width:0">${esc(String(t.usage || t.meaning || "—"))}</span>
        <span class="tag">${esc(String(t.scope || "全局"))}</span>
        <span class="mono mut" title="命中次数">${fmtNum(t.hits || 0)}</span>
        <button class="btn xs" data-term="${esc(String(t.term || ""))}" data-usage="${esc(String(t.usage || ""))}" onclick="hfTermEdit(${Number(t.id)},this)">改</button>
        <button class="btn xs" onclick="hfTermDel(${Number(t.id)})">删</button></div>`).join("") || '<div class="pd" style="padding:10px 0">还没有词条</div>'}
    </div>
  </div>

  ${sty.preview ? `<div class="card sec" style="padding:14px 16px">
    <div class="row bt"><div class="pn">这一轮会注入什么</div><span class="pd mono">${fmtNum(sty.preview.length)} / 300 字</span></div>
    <pre class="pd" style="white-space:pre-wrap;margin:8px 0 0;line-height:1.75">${esc(sty.preview)}</pre>
  </div>` : ""}
</div>`;
}

export async function renderExpressionLib() {
  try {
    const sty = await apiSafe("style/samples", {}, {
      enabled: false, counts: {}, styles: [], terms: [], preview: "",
    });
    return { html: _stylePage(sty) };
  } catch (e) {
    return {
      html: `<div class="card sec" style="padding:14px"><div class="pn">语言学习渲染失败</div>
        <pre class="pd" style="white-space:pre-wrap">${esc(String((e && (e.stack || e.message)) || e))}</pre></div>`,
    };
  }
}

/* 面板在沙箱 iframe 里，window.prompt/confirm 被浏览器屏蔽（返回 null），所以自己画一个。 */
function _askText(title, value, multiline) {
  return new Promise((resolve) => {
    const mask = document.createElement("div");
    mask.className = "ask-mask";
    mask.innerHTML = `<div class="ask-box"><div class="t"></div>
      <textarea></textarea>
      <div class="b"><button class="btn xs" data-k="c">取消</button><button class="btn xs on" data-k="o">确定</button></div></div>`;
    mask.querySelector(".t").textContent = title;
    const ta = mask.querySelector("textarea");
    ta.value = value || "";
    if (multiline) ta.style.minHeight = "92px";
    const done = (v) => { mask.remove(); resolve(v); };
    mask.querySelector('[data-k="c"]').onclick = () => done(null);
    mask.querySelector('[data-k="o"]').onclick = () => done(ta.value.trim() || null);
    mask.onclick = (ev) => { if (ev.target === mask) done(null); };
    ta.onkeydown = (ev) => { if (ev.key === "Enter" && !ev.shiftKey) { ev.preventDefault(); done(ta.value.trim() || null); } };
    document.body.appendChild(mask);
    ta.focus();
    ta.select();
  });
}

function _askConfirm(title) {
  return new Promise((resolve) => {
    const mask = document.createElement("div");
    mask.className = "ask-mask";
    mask.innerHTML = `<div class="ask-box"><div class="t"></div>
      <div class="pd">删掉就找不回来了。</div>
      <div class="b"><button class="btn xs" data-k="c">取消</button><button class="btn xs on" data-k="o">删除</button></div></div>`;
    mask.querySelector(".t").textContent = title;
    const done = (v) => { mask.remove(); resolve(v); };
    mask.querySelector('[data-k="c"]').onclick = () => done(false);
    mask.querySelector('[data-k="o"]').onclick = () => done(true);
    mask.onclick = (ev) => { if (ev.target === mask) done(false); };
    document.body.appendChild(mask);
  });
}

export async function hfStyleLearn(btn) {
  if (btn) { btn.classList.add("loading"); btn.disabled = true; }
  const res = await apiPostSafe("style/learn", {});
  if (btn) { btn.classList.remove("loading"); btn.disabled = false; }
  const d = res.data || {};
  if (res.ok && d.ok) {
    toast(`学完了：${fmtNum(d.saved || 0)} 条原话 / ${fmtNum(d.styles || 0)} 种回复方式`, "ok");
  } else {
    toast(`这轮没学到：${d.reason || res.error || "未知原因"}（新记忆 ${fmtNum(d.fresh || 0)} 条）`, "err");
  }
  refreshCurrent();
}

export async function hfStyleToggle(on) {
  const res = await apiPostSafe("style/toggle", { enabled: !!on });
  toast(res.ok ? (on ? "学习已开启" : "学习已关闭") : "操作失败：" + (res.error || ""), res.ok ? "ok" : "err");
  if (res.ok) refreshCurrent();
}

export async function hfStyleRename(id) {
  const name = await _askText("这种回复方式叫什么？", "");
  if (!name) return;
  const res = await apiPostSafe("style/rename", { id: Number(id), name });
  toast(res.ok ? "改好了" : "操作失败：" + (res.error || ""), res.ok ? "ok" : "err");
  if (res.ok) refreshCurrent();
}

export async function hfStyleStatus(id, status) {
  const res = await apiPostSafe("style/status", { id: Number(id), status });
  toast(res.ok ? (status === "paused" ? "已停用" : "已启用") : "操作失败：" + (res.error || ""), res.ok ? "ok" : "err");
  if (res.ok) refreshCurrent();
}

export async function hfStyleDel(id) {
  const yes = await _askConfirm("删掉这种回复方式？");
  if (!yes) return;
  const res = await apiPostSafe("style/delete", { id: Number(id) });
  toast(res.ok ? "已删除" : "操作失败：" + (res.error || ""), res.ok ? "ok" : "err");
  if (res.ok) refreshCurrent();
}

export async function hfQuoteEdit(id, btn) {
  const old = btn && btn.dataset ? (btn.dataset.q || "") : "";
  const quote = await _askText("改这句原话", old);
  if (!quote || quote === old) return;
  const res = await apiPostSafe("style/quote_edit", { id: Number(id), quote });
  toast(res.ok ? "改好了" : "操作失败：" + (res.error || ""), res.ok ? "ok" : "err");
  if (res.ok) refreshCurrent();
}

export async function hfQuoteDel(id) {
  const yes = await _askConfirm("删掉这句原话？");
  if (!yes) return;
  const res = await apiPostSafe("style/quote_del", { id: Number(id) });
  toast(res.ok ? "已删除" : "操作失败：" + (res.error || ""), res.ok ? "ok" : "err");
  if (res.ok) refreshCurrent();
}

export async function hfTermEdit(id, btn) {
  const cur = btn && btn.dataset ? (btn.dataset.usage || "") : "";
  const usage = await _askText("这条黑话什么情况下用？", cur, true);
  if (usage === null) return;
  const res = await apiPostSafe("style/term_save", { id: Number(id), usage });
  toast(res.ok ? "改好了" : "操作失败：" + (res.error || ""), res.ok ? "ok" : "err");
  if (res.ok) refreshCurrent();
}

export async function hfTermDel(id) {
  const yes = await _askConfirm("删掉这条黑话？");
  if (!yes) return;
  const res = await apiPostSafe("style/term_del", { id: Number(id) });
  toast(res.ok ? "已删除" : "操作失败：" + (res.error || ""), res.ok ? "ok" : "err");
  if (res.ok) refreshCurrent();
}

/* 悬停看全文：单行省略，鼠标靠近在下方弹一个小浮窗（fixed 定位，不被滚动区裁掉） */
let _styleTipEl = null;

export function hfStyleTip(el) {
  try {
    const text = el && el.getAttribute ? (el.getAttribute("data-full") || "") : "";
    if (!text) return;
    if (!_styleTipEl) {
      _styleTipEl = document.createElement("div");
      _styleTipEl.style.cssText = [
        "position:fixed", "z-index:9999", "display:none", "pointer-events:none",
        "background:var(--card,#1c1c1e)", "color:var(--fg,#e8e8ea)",
        "border:1px solid var(--bd,#3a3a3c)", "border-radius:9px",
        "padding:8px 10px", "font-size:12px", "line-height:1.6",
        "box-shadow:0 8px 24px rgba(0,0,0,.45)", "white-space:normal", "word-break:break-word",
      ].join(";");
      document.body.appendChild(_styleTipEl);
    }
    const maxW = Math.min(440, Math.max(200, window.innerWidth * 0.42));
    _styleTipEl.style.maxWidth = maxW + "px";
    _styleTipEl.style.width = "max-content";
    _styleTipEl.textContent = text;
    _styleTipEl.style.display = "block";
    const r = el.getBoundingClientRect();
    const th = _styleTipEl.offsetHeight || 40;
    let left = r.left;
    if (left + _styleTipEl.offsetWidth + 12 > window.innerWidth) {
      left = Math.max(8, window.innerWidth - _styleTipEl.offsetWidth - 12);
    }
    let top = r.bottom + 6;
    if (top + th + 8 > window.innerHeight) top = Math.max(8, r.top - th - 6);
    _styleTipEl.style.left = left + "px";
    _styleTipEl.style.top = top + "px";
  } catch (e) { /* 浮层失败不影响列表 */ }
}

export function hfStyleTipHide() {
  if (_styleTipEl) _styleTipEl.style.display = "none";
}

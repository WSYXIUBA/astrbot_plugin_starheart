/**
 * StarHeart 面板 · 消息事件视图（门控 / 撤回 / 主动 / 好感）
 *
 * 这一组页面的图形化取舍：
 * - 门控：核心是「分数怎么凑够阈值的」→ 用瀑布图把因子逐项加减画出来，比一行数字直观得多；
 *   再配分布直方图与因子命中率，能一眼看出门控是太严还是太松。
 * - 撤回：事件型数据 → 时间轴 + 降权系数条。
 * - 主动：护栏本质是「额度和时钟」→ 用圆环显示今日额度、静默时长与阈值的差距。
 * - 好感：关系是连续量 → 区间色带 + 数值环，而不是只写一个数字。
 */

import * as C from "./charts.js?v=1.0.3";
import {
  $, $$, api, apiSafe, contactLabel, apiPostSafe, esc, fmtNum, fmt1, fmtPct, fmtTime, timeAgo, toast, openDrawer, closeDrawer,
  pageHead, card, kpiCard, kpis, mount, emptyBlock, skelBlock, jsStr, STATE, segmentHtml, syncSeg, countUp,
  refreshCurrent, goPage, noteConfigTouched, takeConfigDirty, nativeWriteError,
} from "./store.js?v=1.0.3";
import { renderDomainConf } from "./views-conf.js?v=1.0.3";

const ZONE_RANK = { HOSTILE: 0, COLD: 1, NEUTRAL: 2, FAMILIAR: 3, CLOSE: 4, DEVOTED: 5 };
const ZONE_COLOR = { HOSTILE: "#f44336", COLD: "#90a4ae", NEUTRAL: "#8e8e93", FAMILIAR: "#03c9d7", CLOSE: "#3c96ca", DEVOTED: "#e0559a" };
const FACTOR_LABEL = {
  at_me: "@我", private: "私聊", question: "疑问句", request: "请求/祈使",
  presence: "存在感扣分", short_reply: "短反应扣分", pressure_bonus: "压力加成",
  state_coef: "状态系数", relation_coef: "关系系数", backoff: "退避", window_count: "窗口条数",
};
const COEF_KEYS = new Set(["state_coef", "relation_coef"]);

/* ————— 撤回图片：放大浮层 + 下载 —————
   缩略图只够"看一眼撤了什么"；要放大看和下载原图就得有一份大图。
   后端在**收到消息那一刻**就把两份都备好了（临时文件会被清）：
   imgs = 512px 缩略图（列表展示），imgs_full = 原图（≤1MB）或 1600px 高清版（下载用）。
   图片数据是 base64，几十 KB 起 —— 不塞进 HTML 属性里，放这个模块级数组按下标引用。 */
let RECALL_IMG_STORE = [];

/** 点撤回缩略图 → 全屏放大，可下载。内联 onclick 调用（面板其余地方同样用内联 onclick）。 */
export function hfRecallZoom(idx) {
  const it = RECALL_IMG_STORE[Number(idx)];
  if (!it) return;
  const old = document.getElementById("hfZoomBox");
  if (old) old.remove();
  const box = document.createElement("div");
  box.id = "hfZoomBox";
  box.style.cssText = "position:fixed;inset:0;z-index:99999;background:rgba(4,8,14,.9);display:flex;flex-direction:column;align-items:center;justify-content:center;gap:10px;padding:20px;cursor:zoom-out";
  const im = document.createElement("img");
  im.src = it.src;
  im.style.cssText = "max-width:94vw;max-height:80vh;border-radius:10px;box-shadow:0 12px 48px rgba(0,0,0,.65);cursor:default";
  const bar = document.createElement("div");
  bar.style.cssText = "display:flex;gap:10px;align-items:center;font-size:12px;color:#dbe6f2;flex-wrap:wrap;justify-content:center";
  const tip = document.createElement("span");
  tip.textContent = `${it.name} · ${it.orig ? "原图" : "高清版（原图超过 1MB，已压缩到 1600px）"}`;
  const dl = document.createElement("a");
  dl.href = it.full || it.src;
  dl.download = it.name;
  dl.textContent = it.orig ? "⬇ 下载原图" : "⬇ 下载高清版";
  dl.style.cssText = "padding:5px 12px;border-radius:8px;background:#3c96ca;color:#fff;text-decoration:none;font-weight:600";
  dl.addEventListener("click", (e) => e.stopPropagation());
  const close = document.createElement("button");
  close.textContent = "关闭 (Esc)";
  close.style.cssText = "padding:5px 12px;border-radius:8px;border:1px solid rgba(255,255,255,.28);background:transparent;color:#dbe6f2;cursor:pointer;font-size:12px";
  const shut = () => { box.remove(); document.removeEventListener("keydown", onKey); };
  const onKey = (e) => { if (e.key === "Escape") shut(); };
  box.addEventListener("click", (e) => { if (e.target === box || e.target === im) shut(); });
  close.addEventListener("click", shut);
  document.addEventListener("keydown", onKey);
  bar.appendChild(tip);
  bar.appendChild(dl);
  bar.appendChild(close);
  box.appendChild(im);
  box.appendChild(bar);
  document.body.appendChild(box);
}

/* ==================================================================
   消息事件 · D 版（矩阵密度）公共零件
   ------------------------------------------------------------------
   语言：行 = 实体（会话 / 人 / 模块），列 = 维度（因子 / 闸门 / 日期 / 小时），格 = 信号。
   红线不变：没有真数据就不画（灰格 = 无记录，不是 0）；颜色只加深印象，数字写在格子里；
            空态如实说明；不新增依赖、不动后端。
   ================================================================== */

/** 数值 → 矩阵格：正绿负红，按幅度分 3 档；0 = 空心（未命中 / 无记录） */
function mxNum(v, opt = {}) {
  const n = Number(v) || 0;
  const forceTone = opt.tone || "";
  const tip = opt.tip ? String(opt.tip) : "";
  if (!n) return { t: "zero", tip };
  const lvl = Math.abs(n) >= 15 ? 3 : Math.abs(n) >= 5 ? 2 : 1;
  return { t: forceTone || (n > 0 ? "pos" : "neg"), lvl, v: (n > 0 ? "+" : "") + fmt1(n), tip };
}

/** 计数 → 强度格（单一色系，按该行的最大值分 3 档） */
function mxHeat(n, max, tone = "pro", tip = "") {
  const v = Number(n) || 0;
  if (!v) return { t: "zero", tip };
  const r = v / Math.max(1, Number(max) || 1);
  return { t: tone, lvl: r > 0.66 ? 3 : r > 0.33 ? 2 : 1, v: String(v), tip };
}

/** 本地日期键 YYYY-MM-DD（格子里给人看的是月-日） */
function mxDayKey(ts) {
  const d = new Date((Number(ts) || 0) * 1000);
  const p = (x) => String(x).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}
function mxDayShort(key) {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(key || ""));
  return m ? `${m[2]}/${m[3]}` : String(key || "");
}
function mxDayKeys(n) {
  const out = [];
  const p = (x) => String(x).padStart(2, "0");
  for (let i = n - 1; i >= 0; i--) {
    const d = new Date(Date.now() - i * 86400000);
    out.push(`${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`);
  }
  return out;
}

/** 会话短名（复用记忆库那套：私聊 · 1001） */
function mxSessLabel(sid) {
  const t = String(sid || "");
  if (!t) return "—";
  return t.indexOf(":") >= 0 ? mhSess(t) : t;
}

/** 关系档 → 两字缩写（矩阵格放不下长英文；全称进 tooltip） */
const MX_ZONE_SHORT = { HOSTILE: "敌对", COLD: "冷淡", NEUTRAL: "中性", FAMILIAR: "熟识", CLOSE: "亲密", DEVOTED: "挚爱" };

/** 因子明细抽屉（门控页：点行、点格都到这里） */
function mxGateDrawer(g) {
  if (!g) return;
  const f = g.factors || {};
  const rows = Object.keys(f)
    .filter((x) => typeof f[x] === "number" && !COEF_KEYS.has(x))
    .sort((a, b) => Math.abs(Number(f[b])) - Math.abs(Number(f[a])))
    .map((x) => `<div class="kv">${esc(FACTOR_LABEL[x] || x)}<span class="mono">${Number(f[x]) >= 0 ? "+" : ""}${fmt1(f[x])}</span></div>`)
    .join("");
  openDrawer(
    `门控裁决 · ${g.decision === "reply" ? "开口" : g.decision === "backoff" ? "退避" : "沉默"}`,
    `<div class="mut" style="font-size:11.5px;margin-bottom:8px">${esc(fmtTime(g.ts, true))} · ${esc(mxSessLabel(g.session_id))}</div>
     <div class="kv">最终得分<span class="mono">${fmt1(g.score)}</span></div>
     <div class="kv">阈值<span class="mono">${fmt1(g.threshold)}</span></div>
     <div class="kv">状态系数 / 关系系数<span class="mono">${fmt1(f.state_coef)} / ${fmt1(f.relation_coef)}</span></div>
     <div class="sec" style="margin:10px 0 6px;font-size:12px"><b>因子明细</b>（按影响大小）</div>
     ${rows || '<div class="mut">这条没有留下因子明细</div>'}
     ${g.note ? `<div class="mut" style="font-size:11px;margin-top:8px">说明：${esc(String(g.note))}</div>` : ""}`
  );
}

/** 门控：逐条记录表（表头 + 行内展开，保留原来的交互，只是挪进折叠区） */
function mxGateTable(host, items, total) {
  const t = host.querySelector("#gateTable");
  const totEl = host.querySelector("#gateTotal");
  if (totEl) totEl.textContent = `第 ${gateState.offset + 1}-${gateState.offset + items.length} 条 / 共 ${fmtNum(total)}`;
  if (!t) return;
  t.innerHTML = items.length
    ? `<div style="overflow-x:auto"><table>
      <thead><tr><th>时间</th><th>会话</th><th>分数</th><th>阈值</th><th>结果</th><th>因子</th><th>说明</th></tr></thead>
      <tbody>${items.map((g) => {
        const tag = g.decision === "reply" ? "ok" : g.decision === "backoff" ? "gy" : "warn";
        const label = g.decision === "reply" ? "开口" : g.decision === "backoff" ? "退避" : "沉默";
        const fac = g.factors || {};
        const f = Object.keys(fac).filter((x) => !COEF_KEYS.has(x));
        const coefs = Object.keys(fac).filter((x) => COEF_KEYS.has(x));
        const fsum = f.reduce((a, x) => a + (Number(fac[x]) || 0), 0);
        const fprod = coefs.reduce((a, x) => a * (Number(fac[x]) || 1), 1);
        const expect = fsum * fprod;
        const same = Math.abs(expect - (Number(g.score) || 0)) < 0.6;
        const det = `<tr class="gf-det" style="display:none"><td colspan="7" style="background:rgba(127,127,127,.07);padding:9px 11px">
          <div class="mut" style="font-size:11.5px;line-height:1.95">
            <b>因子逐项</b>：${f.length ? f.map((x) => `<span class="chip" title="${esc(x)}">${esc(FACTOR_LABEL[x] || x)} <b>${fmt1(fac[x])}</b></span>`).join(" ") : "（这一条没有加减因子）"}
            ${coefs.length ? `<br><b>乘算系数</b>：${coefs.map((x) => `${esc(FACTOR_LABEL[x] || x)} × ${fmt1(fac[x])}`).join(" · ")}` : ""}
            <br><b>算式</b>：因子和 ${fmt1(fsum)}${coefs.length ? ` × ${fmt1(fprod)}` : ""} = ${fmt1(expect)}
            → 实际总分 <b>${fmt1(g.score)}</b>（阈值 ${fmt1(g.threshold)}）
            <span class="tag ${same ? "ok" : "warn"}">${same ? "与公式一致" : "⚠ 与引擎公式不一致，请核对"}</span>
            ${g.user_id ? `<br><b>触发者</b>：<span class="mono">${esc(String(g.user_id))}</span>` : ""}
            ${g.note ? `<br><b>说明</b>：${esc(String(g.note))}` : ""}
          </div></td></tr>`;
        return `<tr data-grow="1" style="cursor:pointer" title="点一下展开因子明细"><td class="mono">${fmtTime(g.ts, true)}</td>
        <td class="mono" style="max-width:220px;overflow:hidden;text-overflow:ellipsis">${esc(mxSessLabel(g.session_id))}</td>
        <td><div class="row" style="gap:6px"><span class="bar" style="width:54px"><span class="bf ${g.score >= (g.threshold || 0) ? "ok" : ""}" style="display:block;width:${Math.min(100, Number(g.score) || 0)}%"></span></span><span class="mono">${fmt1(g.score)}</span></div></td>
        <td class="mono">${fmt1(g.threshold)}</td>
        <td><span class="tag ${tag}">${label}</span></td>
        <td>${f.map((x) => `<span class="chip" title="${esc(FACTOR_LABEL[x] || x)}">${esc(FACTOR_LABEL[x] || x)}</span>`).join(" ")}</td>
        <td class="mut" style="max-width:260px;overflow:hidden;text-overflow:ellipsis">${esc(g.note || "")}</td></tr>${det}`;
      }).join("")}</tbody></table></div>`
    : emptyBlock("这一页没有记录（换个筛选条件试试）", "◉");
  t.querySelectorAll("tr[data-grow]").forEach((tr) => {
    tr.addEventListener("click", () => {
      const d = tr.nextElementSibling;
      if (!d || !d.classList.contains("gf-det")) return;
      const open = d.style.display !== "none";
      d.style.display = open ? "none" : "";
      tr.style.background = open ? "" : "rgba(127,127,127,.08)";
    });
  });
}

const zoneRank = (z) => ZONE_RANK[String(z || "NEUTRAL")] ?? 2;

/* ==================================================================
   门控：决策流水
   ================================================================== */
/** 门控流水的分页/筛选状态：app.js 的分页与筛选按钮直接改这里再重渲染 */
export const gateState = { offset: 0, limit: 30, result: "", since: "", session: "" };

/** 配置型布尔归一：true/false、1/0、"true"/"false" 都认；认不出（含拿不到配置）返回 null —— 调用方不猜 */
function cfgBool(v) {
  if (v === true || v === false) return v;
  if (v === 1 || v === 0) return v === 1;
  if (typeof v === "string") {
    const t = v.trim().toLowerCase();
    if (t === "true" || t === "1") return true;
    if (t === "false" || t === "0" || t === "") return false;
  }
  return null;
}

/** 门控「时间」筛选（纯前端）：只过滤本页已加载的记录，不触发后端查询 */
export function hfGateSince(v) {
  gateState.since = String(v || "");
  refreshCurrent();
}

/* ======================= D-PAGE: renderGateLive ======================= */
export async function renderGateLive() {
  const [page, ov, stats, dom] = await Promise.all([
    apiSafe("gate", { limit: gateState.limit, offset: gateState.offset }, { items: [], total: 0 }),
    apiSafe("overview", {}, {}),
    apiSafe("stats", {}, {}),
    // 影子模式横幅要从配置读（拿不到配置就不显示横幅 —— 不猜）
    apiSafe("config/domains", {}, { values: {} }),
  ]);
  STATE.gate = page;
  const all = page.items || [];
  // 结果 + 时间两档筛选都是纯前端：只作用于本页已加载的记录（不改后端查询）
  let sinceCut = 0;
  if (gateState.since === "today") {
    const d0 = new Date();
    d0.setHours(0, 0, 0, 0);
    sinceCut = d0.getTime() / 1000;
  } else {
    const span = { "1h": 3600, "6h": 6 * 3600, "24h": 24 * 3600, "3d": 3 * 86400 }[gateState.since] || 0;
    if (span) sinceCut = Date.now() / 1000 - span;
  }
  const items = all
    .filter((g) => (gateState.result ? g.decision === gateState.result : true))
    .filter((g) => !sinceCut || (Number(g.ts) || 0) >= sinceCut);
  const total = page.total ?? 0;
  const reply = Number(ov.gate_reply) || 0;
  const evals = Number(ov.gate_total) || stats.gate || total;

  // 行 = 会话（每个会话的「最近一次裁决」当代表；行尾副标写本页被评估次数）
  const bySess = new Map();
  items.forEach((g) => {
    const key = String(g.session_id || "—");
    if (!bySess.has(key)) bySess.set(key, { sid: key, latest: g, n: 0, replyN: 0 });
    const s = bySess.get(key);
    s.n += 1;
    if (g.decision === "reply") s.replyN += 1;
  });
  const rowsSrc = Array.from(bySess.values()).sort((a, b) => b.n - a.n).slice(0, 9);

  // 列 = 因子（按这些代表记录的「平均绝对影响」排前 6）；总分 / 结果固定在右侧
  const agg = {};
  rowsSrc.forEach((s) => Object.keys(s.latest.factors || {}).forEach((k) => {
    if (COEF_KEYS.has(k)) return;
    const slot = agg[k] || (agg[k] = { sum: 0, n: 0 });
    slot.sum += Math.abs(Number(s.latest.factors[k]) || 0);
    slot.n += 1;
  }));
  const cols = Object.keys(agg).sort((a, b) => agg[b].sum - agg[a].sum).slice(0, 6);

  // 谁在扣分：本页窗口里的扣分项合计（调参抓手）
  const negRank = {};
  items.forEach((g) => Object.keys(g.factors || {}).forEach((k) => {
    if (COEF_KEYS.has(k)) return;
    const v = Number(g.factors[k]) || 0;
    if (v < 0) negRank[k] = (negRank[k] || 0) + v;
  }));
  const topNeg = Object.keys(negRank).sort((a, b) => negRank[a] - negRank[b]).slice(0, 3);

  const newest = items[0];
  const replyPage = items.filter((g) => g.decision === "reply").length;
  const conclusion = newest
    ? `最近一条：<b>${fmt1(newest.score)} 分</b>（阈值 ${fmt1(newest.threshold)}）→ ${
        newest.decision === "reply"
          ? "<b>开口</b>"
          : newest.decision === "backoff"
            ? "退避（冷却）"
            : `<b>沉默</b> · 还差 ${fmt1(Math.max(0, (Number(newest.threshold) || 0) - (Number(newest.score) || 0)))} 分`
      }　·　本页窗口 ${fmtNum(items.length)} 条里开口 ${fmtNum(replyPage)} 条${topNeg.length ? ` · 最常扣分：<b>${esc(FACTOR_LABEL[topNeg[0]] || topNeg[0])}</b>` : ""}`
    : `还没有门控记录：开启门控后随便聊几句就会出现评分流水（全库累计评估 ${fmtNum(evals)} 次）`;

  // 影子开关的判读：配置可能是 true/false、1/0、"true"/"false" 任一种形态 —— 统一真值判断；
  // 拿不到配置就不显示横幅（不猜）。关掉影子后只有「群聊且未被 @」的会话会被真拦（main.py 语义）。
  const shadowOn = cfgBool(dom && dom.values ? dom.values.gate_shadow : undefined);

  const html = `
  ${shadowOn === true ? `<div class="noteblock" style="border-left-color:var(--warning,#e0a800);margin-bottom:11px">
    <span class="tag warn">影子模式</span> 当前是影子模式：标为「沉默」的记录实际并未被拦下，只是记了一笔。
    到「设置 · 模块与参数」的门控域（本页页头「参数 →」直达）里关掉影子模式，沉默与退避才会真正拦住事件 —— 且只拦「群聊里没被 @ 到」的那部分；私聊与被 @ 的会话照常回复（这两种情况，静默本来就不拦）。</div>` : ""}
  ${pageHead(
    "门控 · 裁决矩阵",
    "行 = 会话，列 = 因子：<b>蓝=加分、红=扣分、深浅=影响大小</b>，格子里写的就是原始分数。点行或点格 → 完整算式",
    `<button class="btn s" onclick="hfGateReload(this)">刷新</button>
     <button class="btn s" onclick="hfGoParams('gate')" title="去「设置 · 模块与参数」门控域（阈值 / 窗口 / 影子模式），带域锚点">参数 →</button>`,
    "◉",
    conclusion
  )}
  <div class="grid g23 sec">
    ${card(`<div id="gateMatrix"></div>`, {
      title: "会话 × 因子（每个会话的最近一次裁决）",
      sub: "行名后的 ×N = 本页窗口里这个会话被评估的次数",
    })}
    ${card(`<div id="gateSide"></div>`, { title: "现在什么状态" })}
  </div>
  ${card(`<div id="gateDecide"></div>`, {
    title: "逐条改判",
    sub: "决策流水：行内记下「该回 / 判定正确」→ 写进 hf_decision_log.user_verdict（只记账）",
    cls: "sec",
  })}
  <details style="margin-top:12px"><summary style="cursor:pointer;font-size:12.5px;padding:6px 0">
    <span class="tag gy">逐条记录（原始表格）</span><span class="mut">默认收起 —— 与上面矩阵同一批数据，需要抄数字时再展开</span></summary>
  ${card(
    `<div class="row bt wrap" style="margin-bottom:9px;gap:9px">
      <div class="row wrap" style="gap:8px">
        <select class="inp" id="gateResult" style="font-size:11.5px" onchange="hfGateFilter(this.value)">
          <option value="">全部结果</option><option value="reply" ${gateState.result === "reply" ? "selected" : ""}>仅触发</option><option value="silent" ${gateState.result === "silent" ? "selected" : ""}>仅沉默</option><option value="backoff" ${gateState.result === "backoff" ? "selected" : ""}>仅退避</option>
        </select>
        <select class="inp" id="gateSince" style="font-size:11.5px" title="时间筛选只过滤本页已加载的记录（不改后端查询）" onchange="hfGateSince(this.value)">
          <option value="">全部时间</option><option value="1h" ${gateState.since === "1h" ? "selected" : ""}>最近 1 小时</option><option value="6h" ${gateState.since === "6h" ? "selected" : ""}>最近 6 小时</option><option value="today" ${gateState.since === "today" ? "selected" : ""}>今天</option><option value="3d" ${gateState.since === "3d" ? "selected" : ""}>最近 3 天</option>
        </select>
        <span class="mut" id="gateTotal" style="font-size:11px">读取中…</span>
      </div>
      <div class="row" style="gap:6px">
        <button class="btn s" onclick="hfGatePage(-1)">上一页</button>
        <button class="btn s" onclick="hfGatePage(1)">下一页</button>
      </div>
    </div>
    <div id="gateTable">${skelBlock(4)}</div>`,
    { title: "逐条记录", sub: "点行展开因子明细（含「因子和 × 系数 = 总分」的当场校验）；结果 / 时间筛选只作用于本页已加载的记录" }
  )}</details>`;

  return {
    html,
    mount: (host) => {
      mount(host, "#gateMatrix", (el) =>
        C.matrix(el, {
          rows: rowsSrc.map((s) => ({
            label: mxSessLabel(s.sid),
            sub: `×${s.n}`,
            tip: `${s.sid} · 本页 ${s.n} 次评估，开口 ${s.replyN} 次 · 点开看最近一次的完整算式`,
          })),
          cols: cols.map((c) => ({ label: FACTOR_LABEL[c] || c, tip: c })).concat([{ label: "总分" }, { label: "结果" }]),
          cells: (r, c) => {
            const s = rowsSrc[r];
            if (!s) return { t: "zero" };
            const g = s.latest;
            const gs = g.factors || {};
            if (c < cols.length) {
              const k = cols[c];
              if (!(k in gs)) return { t: "zero", tip: `${fmtTime(g.ts, true)} 这次没有「${FACTOR_LABEL[k] || k}」这一项` };
              const v = Number(gs[k]) || 0;
              return mxNum(v, { tip: `${fmtTime(g.ts, true)} · ${FACTOR_LABEL[k] || k} ${v > 0 ? "+" : ""}${fmt1(v)}` });
            }
            if (c === cols.length) {
              const sc = Number(g.score) || 0;
              return { t: "num", v: fmt1(sc), tip: `最近一次总分 ${fmt1(sc)}（阈值 ${fmt1(g.threshold)}）` };
            }
            const d = g.decision;
            return d === "reply" ? { t: "tagok", v: "开口" } : d === "backoff" ? { t: "tagwarn", v: "退避" } : { t: "tag", v: "沉默" };
          },
          legend: [["acc", "加分"], ["neg", "扣分"], ["zero", "未命中 / 无记录"], ["tagok", "越阈值 → 开口"], ["tag", "沉默"]],
          note: "空格 = 该条记录里没有这个因子。想让它更容易开口，调的是<b>阈值</b>（「设置 · 模块与参数」的门控域，本页页头「参数 →」直达），不是改评分。",
          onPick: (r) => mxGateDrawer(rowsSrc[r] && rowsSrc[r].latest),
          onRow: (r) => mxGateDrawer(rowsSrc[r] && rowsSrc[r].latest),
          emptyText: "本页窗口里还没有门控记录",
        })
      );

      const sideEl = host.querySelector("#gateSide");
      if (sideEl) {
        const avg = items.length ? items.reduce((s, g) => s + (Number(g.score) || 0), 0) / items.length : 0;
        const neg = topNeg
          .map((k) => `<div class="kv">${esc(FACTOR_LABEL[k] || k)}<span class="mono" style="color:var(--error,#e05555)">${fmt1(negRank[k])}</span></div>`)
          .join("");
        sideEl.innerHTML =
          `<div class="kv">阈值<span class="mono">${newest ? fmt1(newest.threshold) : "—"}</span></div>
           <div class="kv">全库累计评估<span class="mono">${fmtNum(evals)}</span></div>
           <div class="kv">全库累计开口<span class="mono">${fmtNum(reply)}</span></div>
           <div class="kv">本页平均分<span class="mono">${fmt1(avg)}</span></div>
           <div class="sec" style="margin:11px 0 5px;font-size:12px"><b>谁在扣分</b>（本页窗口内合计）</div>
           ${neg || '<div class="mut" style="font-size:11.5px">本页窗口里没有扣分项</div>'}
           <div class="mut" style="font-size:11px;margin-top:11px;line-height:1.8">
             · 门控只管「该不该开口」，不管说什么
           </div>`;
      }

      mxGateTable(host, items, total);
      mount(host, "#gateDecide", (el) => gateVerdictTable(el, page.decisions || {}));
    },
  };
}




/* ==================================================================
   门控 · 逐条改判（决策流水 hf_decision_log）
   ------------------------------------------------------------------
   「该回」= 这次其实该开口；「判定正确」= 认同这次判定。两者都只写 user_verdict 记账，
   不改判定结果、不写训练样本（样本表还没建）。写库走 POST gate/verdict。
   ================================================================== */
const GATE_VERDICT_LABEL = { should_reply: "该回", correct: "判定正确" };

/** 改判状态单元格（写库后用返回值原地刷新，不整页重渲染） */
function verdictStateHtml(g) {
  const v = String((g && g.user_verdict) || "");
  if (!v) return '<span class="mut">未改判</span>';
  const at = Number((g && g.verdict_at) || 0);
  return `<span class="tag ok">${esc(GATE_VERDICT_LABEL[v] || v)}</span>${
    at ? `<span class="mut" style="font-size:10.5px"> · ${esc(fmtTime(at, true))}</span>` : ""
  }`;
}

function gateVerdictTable(el, pack) {
  const items = (pack && pack.items) || [];
  const total = Number((pack && pack.total) || 0) || items.length;
  if (!items.length) {
    el.innerHTML = emptyBlock(
      (pack && pack.error) || "还没有决策流水 —— 门控评估过一次就会出现（上面那张表的记录来自旧流水表，改判只对决策流水生效）",
      "◎"
    );
    return;
  }
  el.innerHTML = `<div style="overflow-x:auto"><table>
      <thead><tr><th>时间</th><th>会话</th><th>对方说</th><th>分数</th><th>判定</th><th>改判</th><th>当前状态</th></tr></thead>
      <tbody>${items
        .map((g) => {
          const dec = g.decision === "reply" ? "开口" : g.decision === "backoff" ? "退避" : "沉默";
          const shadow = Number(g.gate_shadow) ? ' <span class="tag gy" title="影子模式：只记录没真拦">影子</span>' : "";
          const did = Number(g.id);
          return `<tr>
            <td class="mono">${fmtTime(g.ts, true)}</td>
            <td class="mono" style="max-width:150px;overflow:hidden;text-overflow:ellipsis">${esc(mxSessLabel(g.session_id))}</td>
            <td style="max-width:320px;overflow:hidden;text-overflow:ellipsis" title="${esc(String(g.msg_text || ""))}">${
              g.msg_text ? esc(String(g.msg_text)) : '<span class="mut">（这条没留下原文）</span>'
            }</td>
            <td class="mono">${fmt1(g.score)}</td>
            <td>${esc(dec)}${shadow}</td>
            <td><div class="row" style="gap:5px">
              <button class="btn xs" data-hf-verdict="${did}" data-vd="should_reply" title="我认为这次应该开口">该回</button>
              <button class="btn xs" data-hf-verdict="${did}" data-vd="correct" title="这次判定是对的">判定正确</button>
            </div></td>
            <td data-vdstate="${did}">${verdictStateHtml(g)}</td></tr>`;
        })
        .join("")}</tbody></table></div>
    <div class="mut" style="font-size:10.5px;margin-top:6px">全库 ${fmtNum(total)} 条决策记录（这里列最近 ${items.length} 条）· 改判只记账：不改判定结果，也不写任何训练样本</div>`;
  el.querySelectorAll("button[data-hf-verdict]").forEach((b) => {
    b.addEventListener("click", async () => {
      const did = Number(b.getAttribute("data-hf-verdict"));
      const vd = String(b.getAttribute("data-vd") || "");
      b.disabled = true;
      const res = await apiPostSafe("gate/verdict", { id: did, verdict: vd });
      b.disabled = false;
      if (!res.ok) {
        toast("改判失败：" + res.error, "err");
        return;
      }
      const cell = el.querySelector(`[data-vdstate="${did}"]`);
      if (cell) cell.innerHTML = verdictStateHtml(res.data || {});
      toast(`已记下「${GATE_VERDICT_LABEL[vd] || vd}」`, "ok");
    });
  });
}

/* ==================================================================
   门控参数
   ================================================================== */
export async function renderGateConf(opts = {}) {
  const stats = await apiSafe("stats", {}, {});
  const statsErr = stats && stats.__err;
  const evals = statsErr ? 0 : (stats.gate_total || 0);
  const reply = statsErr ? 0 : (stats.gate_reply || 0);
  const out = await renderDomainConf("gate", {
    bare: !!opts.bare,
    recentFirst: !!opts.recentFirst,
    chartId: "dcChart_gate",
    actions: `<button class="btn s" onclick="hfGateReload(this)">看决策流水 →</button>`,
    side: card(
      `<div id="gcRate"></div>
       <div class="mut" style="font-size:11.5px;margin-top:8px;line-height:1.7">
         · 阈值越低越爱开口；影子模式打开时只记录不拦截，适合先观察<br>
         · 因子权重固化在 engine：@我 / 私聊 / 疑问句 / 请求 / 压力加成 为正，存在感与短反应为负
       </div>`,
      { title: "当前触发率", sub: statsErr ? `统计读取失败：${statsErr}` : `累计 ${fmtNum(evals)} 次评估，开口 ${fmtNum(reply)} 次` }
    ),
  });
  // ⚠️ 这个 id="gcRate" 以前**从来没被 mount 过** —— 卡片里就一直空着一块（空白组件）。
  // 现在把触发率画成环图（单值、有明确分母 0~100%），样本为 0 时给明确文案而不是空白。
  const inner = out.mount;
  out.mount = (host) => {
    if (typeof inner === "function") inner(host);
    // 触发开口：线性条 + 精确条数（用户否掉圆环与百分比 —— 仪表盘读不出精确值）
  mount(host, "#gcRate", (el) => {
    if (statsErr) {
      el.innerHTML = `<div class="fnote">⚠ 统计读取失败，不是 0：${esc(String(statsErr))}</div>`;
      return;
    }
    el.innerHTML = `<div class="hfline" style="border-bottom:0">
      <div class="row bt" style="font-size:12px;margin-bottom:5px"><span>触发开口</span>
        <span class="ok"><b class="mono">${fmtNum(reply)}</b> <span class="mut">/ 评估 ${fmtNum(evals)} 条</span></span></div>
      <div class="hfbar"><i style="width:${evals ? Math.min(100, (reply / evals) * 100).toFixed(1) : 0}%"></i></div>
      <div class="row bt mut" style="font-size:10.5px;margin-top:4px"><span>全库累计</span><span>${evals ? "其余都是「分数没过阈值 → 保持沉默」" : "门控还没跑满一次评估：收到几条消息后再看，不预置数字"}</span></div>
    </div>`;
  });
  };
  return out;
}

/* ==================================================================
   撤回流水 / 参数
   ================================================================== */
/* ======================= D-PAGE: renderRecallFeed ======================= */
export async function renderRecallFeed() {
  const data = await apiSafe("recalls", { limit: 200 }, { items: [], config: {} });
  const items = data.items || [];
  const cfg = data.config || {};
  const factor = Number(cfg.recall_weaken_factor) || 0.3;
  const hitItems = items.filter((x) => Number(x.matched) > 0);
  const hitMems = items.reduce((a, x) => a + (Number(x.matched) || 0), 0);
  const recoverable = items
    .filter((x) => Number(x.matched) > 0 && !/delete/i.test(String(x.action || "")))
    .reduce((a, x) => a + (Number(x.matched) || 0), 0);
  const loss = hitItems.reduce((a, x) => a + (Number(x.matched) || 0) * (1 - factor), 0);
  const actionLabel = { log_only: "只记录", weaken: "降权", weaken_memories: "降权", delete: "删除", delete_memories: "删除" };
  const days = mxDayKeys(7);
  // 撤回反应（模型自己决定说什么）—— 从每条流水的 detail.react 统计
  const reacts = items.map((x) => (x.detail || {}).react).filter(Boolean);
  const saidN = reacts.filter((r) => r.said).length;
  const silentN = reacts.filter((r) => r.silent).length;
  const reactN = saidN + silentN;

  // 行 = 会话；列 = 近 7 天（次数热力）+ 命中 / 处置 / 可恢复
  const bySess = new Map();
  items.forEach((x) => {
    const k = String(x.session_id || "—");
    if (!bySess.has(k)) bySess.set(k, { sid: k, n: 0, hit: 0, last: x, perDay: {}, acts: {} });
    const s = bySess.get(k);
    s.n += 1;
    s.hit += Number(x.matched) || 0;
    s.acts[String(x.action || "log_only")] = true;
    if (!s.latestHit && Number(x.matched) > 0) s.latestHit = x;
    const d = mxDayKey(x.ts);
    (s.perDay[d] = s.perDay[d] || []).push(x);
    if (Number(x.ts) > Number(s.last.ts)) s.last = x;
  });
  const rows = Array.from(bySess.values()).sort((a, b) => b.n - a.n).slice(0, 8);
  const maxDay = Math.max(1, ...rows.map((s) => Math.max(...days.map((d) => (s.perDay[d] || []).length), 0)));

  const concl = items.length
    ? `本页窗口 <b>${fmtNum(items.length)} 起</b>撤回 · 其中命中记忆 <b>${fmtNum(hitItems.length)} 起</b> · 累计降权 <b>${fmtNum(hitMems)} 条</b>（可恢复 ${fmtNum(recoverable)} 条，强度损失合计 ${fmt1(loss)}）${rows[0] ? ` · 撤得最多：<b>${esc(mxSessLabel(rows[0].sid))}</b>（${fmtNum(rows[0].n)} 起）` : ""}`
    : `还没有撤回事件：对方撤回一次消息，这里就会记下「谁撤回了什么」，以及命中了几条记忆`;

  const recallEventsHtml = (list, limit = 30) =>
    list.slice(0, limit).map((x) => {
      const r = ((x.detail || {}).recalled) || {};
      const who = r.sender_name || r.sender || x.user_id || x.operator_id || "未知";
      const imgs = Array.isArray(r.imgs) ? r.imgs.slice(0, 4) : [];
      const fulls = Array.isArray(r.imgs_full) ? r.imgs_full : [];
      const fullOrig = Array.isArray(r.imgs_full_orig) ? r.imgs_full_orig : [];
      const idx0 = RECALL_IMG_STORE.length;
      imgs.forEach((u, i) => RECALL_IMG_STORE.push({
        src: String(u),
        full: String(fulls[i] || u),
        orig: !!fullOrig[i],
        name: `撤回图片-${String(r.sender_name || r.sender || "未知")}-${String(fmtTime(x.ts, true)).replace(/[^\d]/g, "")}-${i + 1}.jpg`,
      }));
      return `<div class="fev" style="align-items:flex-start">
        <i class="mono">${esc(fmtTime(x.ts, true))}</i>
        <span style="flex:1;min-width:0">
          <b>${esc(String(who))}</b><span class="mut" style="font-size:10.5px"> · ${esc(mxSessLabel(x.session_id))} · ${esc(String(x.notice_type || ""))}</span>
          <div style="font-size:12px;margin:3px 0 2px">${r.text ? esc(String(r.text).slice(0, 200))
            : (imgs.length ? `<span class="mut">（撤回的是图片，见下）</span>`
              : (Number(r.imgs_skipped || 0) ? `<span class="mut">⚠ 有 ${Number(r.imgs_skipped)} 张图片没能留存（读取失败或超过嵌入上限）· 消息本身已观察</span>`
                : `<span class="mut">⚠ 内容未留存（这条消息在插件启动前发出，或未被观察）</span>`))}</div>
          ${imgs.length ? `<details style="margin:3px 0"><summary style="cursor:pointer;font-size:11px">显示 ${imgs.length} 张撤回的图片<span class="mut">（点图放大 · 可下载）</span></summary><div class="row wrap" style="gap:6px;margin-top:6px">${imgs.map((u, i) => `<img referrerpolicy="no-referrer" loading="lazy" decoding="async" data-idx="${idx0 + i}" src="${esc(String(u))}" alt="撤回的图片" title="点开放大 / 下载" onclick="hfRecallZoom(${idx0 + i})" style="max-height:120px;border-radius:7px;border:1px solid var(--bd,rgba(128,128,128,.2));cursor:zoom-in">`).join("")}</div></details>` : ""}
          <span class="tag ${x.matched ? "warn" : "gy"}">${x.matched ? `命中记忆 ${fmtNum(x.matched)} 条` : "无命中"} · ${esc(actionLabel[x.action] || String(x.action || "log_only"))}</span>
          ${(() => {
            const rt = (x.detail || {}).react;
            if (!rt) return "";
            const skipText = { disabled: "撤回反应开关没开", "master-off": "StarHeart总开关关着", invisible: "不在可见性名单里",
              "group-off": "群聊已设为不触发", "group-not-related": "群里这条与 bot 无互动", dup: "重复事件",
              "no-prompt": "取不到 AstrBot 提示词（该会话还没说过话）", "no-session": "会话/消息 id 缺失" }[String(rt.skip || "")] || String(rt.skip || "");
            if (rt.said) return `<div class="row" style="gap:6px;margin-top:3px;align-items:baseline"><span class="tag ok">🤖 已回应</span><span style="font-size:12px">${esc(String(rt.text || "").slice(0, 200))}</span>${rt.ms ? `<span class="mut" style="font-size:10.5px">· ${rt.ms}ms</span>` : ""}</div>`;
            if (rt.silent) return `<div class="row" style="gap:6px;margin-top:3px;align-items:baseline"><span class="tag gy">🤖 模型选择沉默</span><span class="mut" style="font-size:11px">看过了，决定不说${rt.ms ? ` · ${rt.ms}ms` : ""}</span></div>`;
            if (rt.error) return `<div class="row" style="gap:6px;margin-top:3px;align-items:baseline"><span class="tag warn">🤖 生成失败</span><span class="mut" style="font-size:11px">${esc(String(rt.error).slice(0, 90))}</span></div>`;
            if (rt.skip) return `<div class="row" style="gap:6px;margin-top:3px;align-items:baseline"><span class="tag gy">🤖 未触发</span><span class="mut" style="font-size:11px">${esc(skipText)}</span></div>`;
            return "";
          })()}
        </span></div>`;
    }).join("");

  RECALL_IMG_STORE = [];
  const html = `
  ${pageHead(
    "撤回 · 会话 × 近 7 天",
    "行 = 会话，列 = 最近 7 天：<b>格色越深 = 那天撤回次数越多</b>；点格子看那天撤回了什么（正文 + 图片），点行看这个会话的全部记录",
    `<button class="btn s" onclick="hfGoParams('recall')" title="去「设置 · 模块与参数」撤回域（三档处理 / 降权系数 / 保留天数），带域锚点">参数 →</button>`,
    "↩",
    concl
  )}
  ${kpis(
    kpiCard("rfMem", fmtNum(hitMems), "累计被降权记忆", `<span class="mut">条</span>`)
    + kpiCard("rfKeep", fmtNum(recoverable), "可恢复", `<span class="tag ok">未被删除</span>`)
    + kpiCard("rfLoss", fmt1(loss), "强度损失合计", `<span class="mut">系数 ×${factor}</span>`)
    + kpiCard("rfMode", esc(cfg.recall_handling || "—"), "当前处理方式", `<span class="mut">命中即${esc(actionLabel[cfg.recall_handling] || "按配置")}</span>`)
    + kpiCard("rfReact", `${fmtNum(saidN)} 说 / ${fmtNum(silentN)} 沉默`, "撤回反应（模型自己决定）",
        `<span class="mut">${cfg.recall_react_enabled ? `开关已开 · 触发 ${fmtNum(reactN)} 起` : "开关未开（到「撤回参数」里打开）"}</span>`)
  )}
  ${card(`<div id="rfMatrix"></div>`, {
    title: "撤回 × 会话 × 日期",
    sub: "空格 = 那天没有撤回（事实，不是 0）；灰色的「未留存」表示内容在插件启动前发出，取不回来",
  })}
  <details style="margin-top:12px"><summary style="cursor:pointer;font-size:12.5px;padding:6px 0">
    <span class="tag gy">取证清单 + 动作分布</span><span class="mut">默认收起：上面矩阵点开就是同一份内容；需要整页翻查时再展开</span></summary>
  <div class="grid g23 sec">
    ${card(`<div id="rfActions"></div>`, { title: "动作分布", sub: "撤回事件触发了什么处理" })}
    ${card(`<div id="rfFactorBar"></div>`, { title: "降权强度", sub: "命中记忆的强度会乘上这个系数（越小打击越狠）" })}
  </div>
  ${card(
    `<div class="mut" style="font-size:11px;margin-bottom:7px">收到消息时就留了档（环形缓冲 + 消息表），撤回时按 message_id 取回 —— 所以这里能看到<b>谁撤回了什么</b>；取不到就如实写「未留存」。</div>
     ${items.length ? recallEventsHtml(items, 40) : `<div class="mut">还没有撤回事件。</div>`}`,
    { title: "谁撤回了什么（完整内容 + 图片）", sub: "留档取证 · 撤回正文 + 图片" }
  )}</details>`;

  return {
    html,
    mount: (host) => {
      mount(host, "#rfMatrix", (el) =>
        C.matrix(el, {
          rows: rows.map((s) => ({
            label: mxSessLabel(s.sid),
            sub: `${s.n} 起`,
            tip: `${s.sid} · 本页窗口 ${s.n} 起撤回，命中记忆 ${s.hit} 条`,
          })),
          cols: days.map((d) => ({ label: mxDayShort(d), tip: d })).concat([{ label: "命中" }, { label: "处置" }, { label: "可恢复" }]),
          cells: (r, c) => {
            const s = rows[r];
            if (!s) return { t: "zero" };
            if (c < days.length) {
              const list = s.perDay[days[c]] || [];
              if (!list.length) return { t: "zero", tip: `${days[c]}：这天没有撤回` };
              const hitN = list.reduce((a, x) => a + (Number(x.matched) || 0), 0);
              const first = list[0];
              const r0 = ((first.detail || {}).recalled) || {};
              const who = r0.sender_name || r0.sender || first.user_id || first.operator_id || "未知";
              const r0imgs = Array.isArray(r0.imgs) ? r0.imgs : [];
              const txt = r0.text ? String(r0.text).slice(0, 40)
                : (r0imgs.length ? `（图片 ${r0imgs.length} 张）`
                  : (Number(r0.imgs_skipped || 0) ? "（有图片但没能留存）" : "（内容未留存）"));
              return {
                t: "rec",
                lvl: Math.max(1, Math.min(3, Math.ceil((list.length / maxDay) * 3))),
                v: String(list.length),
                tip: `${days[c]} · ${list.length} 起｜${who}：${txt}｜命中记忆 ${hitN} 条 —— 点开看完整内容`,
              };
            }
            if (c === days.length) return { t: "num", v: String(s.hit), dim: !s.hit, tip: `本页窗口内命中记忆 ${s.hit} 条` };
            if (c === days.length + 1) {
              const acts = Object.keys(s.acts);
              const a = acts.length === 1 ? acts[0] : "混合";
              return { t: a === "delete" || a === "delete_memories" ? "tagwarn" : "tag", v: actionLabel[a] || a, tip: acts.map((x) => actionLabel[x] || x).join(" / ") };
            }
            const canRecover = s.hit > 0 && !s.acts.delete && !s.acts.delete_memories;
            return canRecover
              ? { t: "tagok", v: "✓", tip: `命中 ${s.hit} 条按降权处理，记忆还在，可恢复` }
              : { t: "mute", v: "—", tip: s.hit ? "这批记忆按删除处理，不能恢复" : "没有命中记忆" };
          },
          legend: [["rec", "撤回次数（深=多）"], ["zero", "无撤回"], ["tagok", "可恢复（未被删除）"], ["tagwarn", "删除"]],
          note: "撤回是<b>对方的行为</b>，不是你的数据出错；降权只是「别把撤回过的话当成口头禅」。未留存的行照实写「未留存」，不补造。",
          onPick: (r, c) => {
            const s = rows[r];
            if (!s) return;
            if (c < days.length) {
              const list = s.perDay[days[c]] || [];
              if (!list.length) return;
              openDrawer(`${days[c]} · ${mxSessLabel(s.sid)}（${list.length} 起）`, recallEventsHtml(list, 20) || '<div class="mut">—</div>');
              return;
            }
            const all = items.filter((x) => String(x.session_id || "—") === s.sid);
            openDrawer(`${mxSessLabel(s.sid)} · 全部撤回记录`, recallEventsHtml(all, 30) || '<div class="mut">—</div>');
          },
          onRow: (r) => {
            const s = rows[r];
            if (!s) return;
            const all = items.filter((x) => String(x.session_id || "—") === s.sid);
            openDrawer(`${mxSessLabel(s.sid)} · 全部撤回记录（${all.length} 起）`, recallEventsHtml(all, 30) || '<div class="mut">—</div>');
          },
          emptyText: "还没有撤回事件 —— 对方撤回一次消息就会出现记录",
        })
      );

      mount(host, "#rfActions", (el) => {
        const actions = {};
        items.forEach((x) => (actions[x.action || "log_only"] = (actions[x.action || "log_only"] || 0) + 1));
        return C.barSplit(el, {
          items: Object.keys(actions).map((k) => ({
            label: actionLabel[k] || k,
            value: actions[k],
            color: { log_only: "#8e8e93", weaken: "#ffc107", weaken_memories: "#ffc107", delete: "#f44336", delete_memories: "#f44336" }[k] || "#3c96ca",
          })),
          height: 16,
        });
      });
      mount(host, "#rfFactorBar", (el) =>
        C.barGauge(el, {
          value: factor,
          max: 1,
          color: "#ffc107",
          label: "命中记忆的强度会乘上这个系数（越小打击越狠）",
          sub: `×${factor}`,
          height: 14,
        })
      );
    },
  };
}


export async function renderRecallConf(opts = {}) {
  return renderDomainConf("recall", {
    bare: !!opts.bare,
    recentFirst: !!opts.recentFirst,
    chartId: "dcChart_recall",
    side: card(
      `<div class="mut" style="font-size:11.5px;line-height:1.8">
        · <b>log_only</b>：只记录，不动记忆（最保守）<br>
        · <b>weaken</b>：命中记忆强度 × 降权系数<br>
        · <b>delete</b>：直接删掉命中记忆（最激进）</div>`,
      { title: "三种处理方式", sub: "拿不准就先 log_only 观察几天" }
    ),
  });
}

/* ==================================================================
   主动消息：候选池（名单与会话清单合并成一张表） / 投递审计 / 设置
   ================================================================== */

/**
 * 主动候选页。
 *
 * 设计要点（用户反馈驱动的三处改动）：
 * 1) **名单与会话清单合并**：原来「黑白名单」和「会话清单」是两块，功能重叠、还要两处维护；
 *    现在一张表既是名单（勾选 + 保存）又是清单（判定原因、额度、静默、试看、真发）。
 * 2) **显示名称**：只给 QQ 号/群号等于没法认人，行首一律显示昵称/群名，号码退成小字。
 * 3) **重排**：试发在最上（最常用），名单表占主区，投递状态/被挡原因收进右侧一栏。
 */
/* ==================================================================
   消息事件的「域枢纽」：一个域一个子菜单项，域内再用分段切「流水 / 名单 / 参数」
   —— 原来 10 个子页平铺在侧栏，找什么都得先数一遍；现在侧栏 4 项，
   每个域内部只保留「要看的」和「要调的」两层。
   ================================================================== */
const MSG_TABS = {
  gate: {
    hint: "门控决定「该不该开口」：评分 → 阈值 → 开口/沉默",
    tabs: [["live", "决策流水"], ["conf", "参数"]],
  },
  recall: {
    hint: "撤回后的处理：降权命中记忆",
    tabs: [["feed", "撤回流水"], ["conf", "参数"]],
  },
  proactive: {
    hint: "主动开口：名单决定「能对谁开口」，护栏决定「什么时候开口」",
    tabs: [["pool", "候选与名单"], ["audit", "投递审计"], ["conf", "参数"]],
  },
  favour: {
    hint: "好感结算：谁和我近，以及怎么算出来的",
    tabs: [["list", "好感列表"], ["insight", "关系洞察"], ["conf", "参数"]],
  },
};
const MSG_TAB_DEFAULT = { gate: "live", recall: "feed", proactive: "pool", favour: "list" };
const msgTabState = { ...MSG_TAB_DEFAULT };

/** 旧的子页 id → 新域 + 域内页（老书签/链接不失效） */
export const MSG_LEGACY = {
  gate_live: ["gate", "live"], gate_conf: ["gate", "conf"],
  recall_feed: ["recall", "feed"], recall_conf: ["recall", "conf"],
  proactive_pool: ["proactive", "pool"], proactive_audit: ["proactive", "audit"], proactive_conf: ["proactive", "conf"],
  favour_list: ["favour", "list"], favour_insight: ["favour", "insight"], favour_conf: ["favour", "conf"],
};

/** 当前域内页（给路由当 key 的一部分：同域换页也要重渲染） */
export function msgViewKey() {
  return Object.keys(MSG_TAB_DEFAULT).map((k) => `${k}:${msgTabState[k] || MSG_TAB_DEFAULT[k]}`).join("|");
}

export function msgTabSet(domain, tab) {
  if (MSG_TABS[domain] && MSG_TABS[domain].tabs.some(([v]) => v === tab)) msgTabState[domain] = tab;
}

/**
 * 页底折叠区跳转（原「同 hash 死按钮」整改）：不再 goPage 到当前页白点一下，
 * 而是原地展开对应折叠区并滚到位；key="top" 表示回到本页主内容顶部。
 */
export function hfFoldJump(key) {
  const host = document.getElementById("pagehost");
  if (!host) return;
  if (key === "top") {
    try { host.scrollIntoView({ block: "start", behavior: "smooth" }); } catch (err) { host.scrollIntoView(); }
    return;
  }
  const hit = Array.from(host.querySelectorAll("details")).find((d) => {
    const s = d.querySelector("summary");
    return s && String(s.textContent || "").includes(key);
  });
  if (!hit) { toast("没找到「" + key + "」折叠区（页面可能已更新）"); return; }
  hit.open = true;
  try { hit.scrollIntoView({ block: "start", behavior: "smooth" }); } catch (err) { hit.scrollIntoView(); }
  hit.style.outline = "2px solid var(--primary,#3c96ca)";
  setTimeout(() => { hit.style.outline = ""; }, 1600);
}

async function renderMessagesLegacy(domain) {
  // 旧书签里存在「有域、没页」的组合（如 recall_live）：以前会取到 undefined 渲染器 → 直接抛错白屏。
  const ALIAS = { gate_live: "gate", recall_live: "recall", proactive_pool: "proactive", proactive_audit: "proactive",
                  favour_list: "favour", favour_insight: "favour", recall_feed: "recall",
                  flow: "hub", control: "hub" };
  const dom = ALIAS[domain] || domain;
  const conf = MSG_TABS[dom] || MSG_TABS.gate;
  const cur = msgTabState[dom] || MSG_TAB_DEFAULT[dom];
  const renderers = {
    "gate:live": renderGateLive, "gate:conf": renderGateConf,
    "recall:feed": renderRecallFeed, "recall:conf": renderRecallConf,
    "proactive:pool": renderProactivePool, "proactive:audit": renderProactiveAudit, "proactive:conf": renderProactiveConf,
    "favour:list": renderFavourList, "favour:insight": renderFavourInsight, "favour:conf": renderFavourConf,
  };
  const fn = renderers[`${dom}:${cur}`] || renderers[`${dom}:${MSG_TAB_DEFAULT[dom]}`];
  if (typeof fn !== "function") {
    return { html: card(`<div class="mut">这个旧地址（#messages/${esc(String(domain))}）在当前版本里没有对应页面，请用上方页签选择。</div>`, { title: "地址已失效" }) };
  }
  const out = (await fn()) || { html: "" };
  // 不再渲染页内页签条：域页内部没有任何第二层导航（用户明确不要"顶上的子菜单"）
  return {
    html: out.html || "",
    mount: async (host) => {
      const seg = host.querySelector(`[data-seg="msgtab_${domain}"]`);
      if (seg) {
        seg.querySelectorAll(".sg").forEach((b) =>
          b.addEventListener("click", () => {
            if (b.classList.contains("on")) return;
            msgTabState[domain] = b.dataset.v;
            refreshCurrent();
          })
        );
        syncSeg(seg);
      }
      if (typeof out.mount === "function") await out.mount(host);
    },
  };
}

/** 主动名单：默认只列「已开启主动」的会话（用户要求：黑名单内 / 白名单外的不显示）；
 *  需要加白名单时用页内按钮显式展开被拦下的那批（不叫「假控件」，它真的会重渲染）。 */
let PRO_SHOW_BLOCKED = {};
export function hfProMore(kind, show) {
  PRO_SHOW_BLOCKED = Object.assign({}, PRO_SHOW_BLOCKED, { [kind]: !!show });
  refreshCurrent();
}

export async function renderProactivePool() {
  const [rules, log] = await Promise.all([
    apiSafe("proactive/rules", {}, { sessions: [], allowed: 0, blocked: 0, names: {}, modes: {} }),
    apiSafe("proactive", { limit: 200 }, { items: [], sessions: [], candidates: 0, config: {} }),
  ]);
  if ((rules && rules.__err) || (log && log.__err)) {
    const why = (rules && rules.__err) || (log && log.__err);
    return {
      html: pageHead("主动消息读取失败", `接口现在拿不到名单/日志：${esc(String(why))}`,
        `<button class="btn p" onclick="hfRefresh()">重试</button>`, "⚠")
        + card(`<div class="fnote">⚠ 不渲染空名单，避免把「读取失败」当成「名单为空」再保存一次。</div>`,
               { title: "读取失败" }),
    };
  }
  STATE.rules = rules;
  const cfg = log.config || {};
  const sessions = rules.sessions || [];
  const names = rules.names || {};
  const sent = (log.items || []).filter((x) => x.status === "sent");
  const todaySent = sessions.reduce((a, x) => a + (x.sent_today || 0), 0);
  const cap = (cfg.proactive_daily_cap === 0 ? 0 : (Number(cfg.proactive_daily_cap) || 3));
  const idleMin = (cfg.proactive_idle_min === 0 ? 0 : (Number(cfg.proactive_idle_min) || 120));
  const modes = rules.modes || {};

  // 勾选状态：群/私聊 × 白/黑 各自独立（切模式不串味）
  STATE.proPick = STATE.proPick || {};
  for (const kind of ["group", "private"]) {
    for (const mode of ["white", "black"]) {
      const key = `${kind}:${mode}`;
      if (!STATE.proPick[key]) STATE.proPick[key] = new Set((((mode === "white" ? rules[`wl_${kind}`] : rules[`bl_${kind}`]) || [])).map(String));
    }
  }

  // 一行数据：候选池里的会话 + 名单里显式加过但没聊过的（都要能看见、能取消）
  const rowsOf = (kind) => {
    const seen = new Map();
    sessions.forEach((s) => {
      if (String(s.kind || "") !== kind || !s.id) return;
      seen.set(String(s.id), {
        id: String(s.id), sid: s.session_id || "", name: s.name || names[`${kind}:${s.id}`] || "",
        verdict: s.verdict || "", why: s.why || "", msgs: s.msgs || 0,
        idle: s.idle_min || 0, sentToday: s.sent_today || 0,
      });
    });
    // 兜底：名单里存过、后端没给行的（正常情况后端都会给，含真实判定）——
    // 这里一律标「被拦 / 未判定」，绝不硬编码 allow（那会把"已失效"渲染成"可主动"）。
    ["white", "black"].forEach((m) => {
      ((rules[`${m === "white" ? "wl" : "bl"}_${kind}`] || [])).forEach((id) => {
        const key = String(id);
        if (seen.has(key)) return;
        seen.set(key, { id: key, sid: "", name: names[`${kind}:${key}`] || "", verdict: "deny",
                       why: "名单里保存过（待后端判定）", msgs: 0, idle: 0, sentToday: 0 });
      });
    });
    const list = Array.from(seen.values());
    list.sort((a, b) => (b.verdict === "allow" ? 1 : 0) - (a.verdict === "allow" ? 1 : 0) || (b.msgs || 0) - (a.msgs || 0));
    return list;
  };

  const rowHtml = (kind, r) => {
    const mode = String(modes[kind] || "black");
    const picked = (STATE.proPick[`${kind}:${mode}`]) || new Set();
    const nm = r.name || "";
    const label = nm && nm !== r.id ? nm : `${kind === "group" ? "群" : "QQ"} ${r.id}`;
    const last = sent.find((x) => x.session_id === r.sid);
    return `<div class="picklist-row" data-name="${esc((nm || "") + " " + r.id).toLowerCase()}" style="display:flex;align-items:center;gap:9px;padding:7px 9px;border-bottom:1px solid var(--bd)">
      <input type="checkbox" class="probox" data-kind="${kind}" data-id="${esc(r.id)}" ${picked.has(r.id) ? "checked" : ""}
             onchange="hfProToggle('${kind}', ${jsStr(r.id)}, this.checked)" style="flex-shrink:0">
      <div style="min-width:0;flex:1">
        <div class="row bt" style="gap:6px">
          <span style="font-weight:600;font-size:12.5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(label)}</span>
          <span class="tag ${r.verdict === "allow" ? "ok" : "gy"}">${r.verdict === "allow" ? "可主动" : "被拦"}</span>${r.sid ? "" : `<span class="tag gy">未聊过</span>`}
        </div>
        <div class="mut" style="font-size:10.5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">
          <span class="mono">${esc(r.id)}</span>${r.why ? ` · ${esc(r.why)}` : ""}${last ? ` · 上次主动 ${timeAgo(last.ts)}` : ""}
        </div>
      </div>
      <div style="width:132px;flex-shrink:0">
        <div class="row bt" style="font-size:10px;color:var(--tx3)"><span>静默 ${fmt1((r.idle || 0) / 60)}h</span><span>额度 ${r.sentToday || 0}/${cap}</span></div>
        <span class="bar" style="display:block;margin-top:3px"><span class="bf ${(r.idle || 0) >= idleMin ? "ok" : ""}" style="display:block;width:${Math.min(100, ((r.idle || 0) / Math.max(1, idleMin)) * 100)}%"></span></span>
      </div>
      <button class="btn xs" onclick="hfProactivePreview(this, ${jsStr(r.sid || "")})" ${r.sid ? "" : "disabled"}>试看</button>
      <button class="btn xs" onclick="hfProactiveSend(${jsStr(r.sid || "")}, this)" ${r.sid ? "" : "disabled"}>真发</button>
    </div>`;
  };

  const listBlock = (kind, title) => {
    // saved = 服务器上真正生效的；mode = 正在查看/编辑的（切段只改它，按保存才写服务器）
    const saved = String(modes[kind] || "black");
    const mode = String((STATE.proMode && STATE.proMode[kind]) || saved);
    const picked = (STATE.proPick[`${kind}:${mode}`]) || new Set();
    const rowsAll = rowsOf(kind);
    const allowed = rowsAll.filter((r) => r.verdict === "allow");
    // 用户口径（2026-10-11）：名单页把「所有用户和群聊」都摆出来 —— 原名单默认空 = 谁都在，
    // 不藏行、不搞「显示被拦下的」二段式（那套按钮还会让用户以为名单显示不全）。
    const rows = rowsAll;
    const hiddenN = 0;
    return `<div>
      <div class="row bt wrap" style="gap:7px;margin-bottom:8px">
        <b style="font-size:12.5px">${esc(title)}</b>
        ${segmentHtml([{ value: "white", label: "白名单", title: "只主动对名单内的会话开口" }, { value: "black", label: "黑名单", title: "名单内不主动，其余都可以" }, { value: "off", label: "不主动", title: "关闭这一类的主动消息" }], mode, `promode_${kind}`)}
        <span class="mut" style="font-size:11px">已选 <b id="proCnt_${kind}">${picked.size}</b> / 共 ${rows.length}（其中可主动 ${allowed.length}）</span>
        <button class="btn xs" onclick="hfProAll('${kind}', true)">全选</button>
        <button class="btn xs" onclick="hfProAll('${kind}', false)">清空</button>
        <button class="btn p" onclick="hfProSave('${kind}', this)">保存并生效</button>
        <span class="searchbox" style="width:130px"><i class="ic">⌕</i><input placeholder="过滤…" oninput="hfProFilter(this.value, '${kind}')"></span>
      </div>
      <div class="noteblock" id="proDirty_${kind}" style="border-left-color:var(--warning);margin-bottom:8px;display:${mode === saved ? "none" : ""}">
        <b>未保存</b>：你正在查看 / 编辑的是<b>${mode === "white" ? "白名单" : mode === "off" ? "不主动" : "黑名单"}</b>；当前生效的仍是<b>${saved === "white" ? "白名单" : saved === "off" ? "不主动" : "黑名单"}</b> —— 点「保存并生效」才会切换。
      </div>
      <div class="noteblock" id="proNote_${kind}" style="border-left-color:var(--warning);margin-bottom:8px;display:${mode === "white" && !picked.size ? "" : "none"}">白名单为空 = <b>谁都不主动</b>（不会打扰任何人）。想主动找谁就勾上它再点「保存并生效」。</div>
      <div class="mut" style="font-size:10.5px;margin-bottom:5px">群聊遵循 AstrBot 原生白名单：<b>为空 = 全部放行</b>（下面列出全部）；<b>非空 = 只列名单内的群</b>；私聊只看本插件名单。此前保存过、但已不在原生名单里的会显示成「被拦 · 已失效」—— 取消勾选并保存即可清理。</div>
      <div class="picklist" data-kind="${kind}" style="max-height:min(56vh,520px);border:1px solid var(--bd);border-radius:6px;overflow-y:auto;overflow-x:hidden;scrollbar-gutter:stable">
        ${rows.length ? rows.map((r) => rowHtml(kind, r)).join("") : emptyBlock("还没有会话：先在群里聊几句，或把要主动的人加进白名单", "☎")}
      </div>
    </div>`;
  };

  const st = {};
  (log.items || []).forEach((x) => (st[x.status || "candidate"] = (st[x.status || "candidate"] || 0) + 1));
  const blocked = sessions.filter((x) => x.verdict !== "allow");

  const html = `
  ${pageHead(
    "主动消息",
    `静默满 <b>${idleMin} 分钟</b>才有资格；每天最多 <b>${cap}</b> 条开口（当天随机排布时间点）；判定顺序：可见性名单 → 主动名单 → 开口时间点 → 额度/时钟/关系档`,
    `<button class="btn p" onclick="hfProactiveScan(this)">立即扫描一轮</button>
     <button class="btn s" onclick="hfFoldJump('投递审计')">投递审计 ↓</button>`,
    "☎"
  )}
  ${kpis(
    kpiCard("ppSent", fmtNum(todaySent), "今日已发", `<span class="mut">上限 ${cap}/会话</span>`)
    + kpiCard("ppAllow", fmtNum(rules.allowed || 0), "可主动的会话", `<span class="tag ok">可主动</span>`)
    + kpiCard("ppDeny", fmtNum(rules.blocked || 0), "被拦下的会话", `<span class="tag gy">不会打扰</span>`)
    + kpiCard("ppCand", fmtNum(log.candidates || 0), "历史候选记录")
  )}
  ${card(
    `<div class="row wrap" style="gap:8px;align-items:flex-end">
       <label style="font-size:11.5px" class="mut">发给
         <input id="proTestTarget" class="inp" placeholder="QQ 号 / 群号 / 完整会话串" style="width:220px;margin-left:6px">
       </label>
       <label style="font-size:11.5px" class="mut">说点
         <input id="proTestText" class="inp" placeholder="留空 = 让模型现写" style="width:240px;margin-left:6px">
       </label>
       <button class="btn p" onclick="hfProTest(this)">立即发一条</button>
     </div>
     <div class="mut" style="font-size:11px;margin-top:8px">让 bot 直接发：不看静默、不占额度；生成 30s / 发送 20s 硬超时，不会卡死。</div>
     <div id="proTestResult" style="margin-top:9px">${proTestResultHtml()}</div>`,
    { title: "一键试发", sub: "人工试发不受护栏限制" }
  )}
  <div class="grid g32 sec">
    ${card(`<div class="col" style="gap:14px">${listBlock("private", "私聊")}${listBlock("group", "群聊")}</div>`,
      { title: "主动名单 · 会话总表", sub: "勾选即名单（白名单=只对这些主动）；同一张表就是会话清单：判定原因、额度、静默、试看、真发都在这里" })}
    <div class="col">
      ${card(`<div id="ppStatusChart"></div>`, { title: "投递状态", sub: "候选 → 发送 / 跳过 / 失败" })}
      ${card(
        `<div class="col" style="gap:7px">${
          blocked.length
            ? blocked.slice(0, 6).map((s) => `<div class="row" style="gap:7px;font-size:11.5px">
                <span class="mono mut" style="min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(s.name || s.id)}</span>
                <span class="spacer"></span><span class="mut">${esc(s.why || "")}</span></div>`).join("")
            : `<div class="mut" style="font-size:11.5px">没有会话被拦下</div>`
        }</div>`,
        { title: "被挡下的原因", sub: "前 6 条" }
      )}
      ${card(
        `<div class="mut" style="font-size:11.5px;line-height:1.8">自动白名单：<b>${esc(rules.auto_wl || "off")}</b>（互动过的会话自动进白名单）<br>
          可见性名单是地基：被它挡掉的会话，这里放行也不会发。</div>
         <div class="row" style="gap:6px;margin-top:9px;flex-wrap:wrap">
           <button class="btn s" onclick="goPage('settings','contacts')">可见性名单 →</button>
           <button class="btn s" onclick="hfProactivePreview(this)">生成一条试看</button>
         </div>`,
        { title: "自动策略", sub: "与可见性的关系" }
      )}
    </div>
  </div>`;
  return {
    html,
    mount: (host) => {
      ["group", "private"].forEach((kind) => {
        const seg = host.querySelector(`[data-seg="promode_${kind}"]`);
        if (!seg) return;
        seg.querySelectorAll(".sg").forEach((b) =>
          b.addEventListener("click", () => {
            if (b.classList.contains("on")) return;
            hfProMode(kind, b.dataset.v);
          })
        );
        syncSeg(seg);
      });
      mount(host, "#ppStatusChart", (el) =>
        C.barSplit(el, {
          items: Object.keys(st).map((k) => ({
            label: { sent: "窗口内已发送", skipped: "被跳过", failed: "发送失败", candidate: "仅候选" }[k] || k,
            value: st[k],
            color: { sent: "#00c853", skipped: "#ffc107", failed: "#f44336", candidate: "#3c96ca" }[k] || "#8e8e93",
          })),
          height: 16,
        })
      );
    },
  };
}

/** 勾选（本地，等「保存」一起提交） */
export function hfProToggle(kind, id, on) {
  // 勾的是「正在查看」的那份名单（切段只改视图；保存才写服务器）
  const mode = String((STATE.proMode && STATE.proMode[kind])
    || (STATE.rules && STATE.rules.modes && STATE.rules.modes[kind]) || "black");
  const key = `${kind}:${mode}`;
  STATE.proPick = STATE.proPick || {};
  STATE.proPick[key] = STATE.proPick[key] || new Set();
  if (on) STATE.proPick[key].add(String(id));
  else STATE.proPick[key].delete(String(id));
  const el = document.querySelector(`#proCnt_${kind}`);
  if (el) el.textContent = String(STATE.proPick[key].size);
}

export function hfProAll(kind, on) {
  const mode = String((STATE.proMode && STATE.proMode[kind])
    || (STATE.rules && STATE.rules.modes && STATE.rules.modes[kind]) || "black");
  const key = `${kind}:${mode}`;
  STATE.proPick = STATE.proPick || {};
  STATE.proPick[key] = new Set();
  $$(`.probox[data-kind="${kind}"]`).forEach((b) => {
    b.checked = !!on;
    if (on) STATE.proPick[key].add(String(b.getAttribute("data-id")));
  });
  const el = document.querySelector(`#proCnt_${kind}`);
  if (el) el.textContent = String(STATE.proPick[key].size);
}

/** 切换「正在查看 / 编辑哪份名单」——**纯视图切换，绝不写服务器**。
 *
 *  用户反馈（2026-10-11）：「我切换到黑名单，只是查看，没有保存，但他也生效了，
 *  导致所有没选中的都进入主动消息列表」。旧实现把「切段」当成了「保存」——现已改正：
 *  切段 = 只看 / 只编辑这份名单；**真正生效必须按「保存」**（保存时把「正在查看的模式 +
 *  该模式的勾选」一起写下去）。两份名单各自留着，来回切不丢内容。
 *  另外：这里就地更新段选 / 勾选框 / 计数 / 提示，**不整页重渲染** ——
 *  既不会收起任何折叠卡，也不会因连点段选堆积渲染（旧版「卡住」的来源之一）。
 */
export function hfProMode(kind, mode) {
  const m = mode === "off" ? "off" : (mode === "black" ? "black" : "white");
  STATE.proMode = STATE.proMode || {};
  STATE.proMode[String(kind)] = m;
  const seg = document.querySelector(`[data-seg="promode_${kind}"]`);
  if (seg) {
    seg.querySelectorAll(".sg").forEach((b) => b.classList.toggle("on", b.dataset.v === m));
    try { syncSeg(seg); } catch (err) { /* 指示器失败不影响功能 */ }
  }
  const picked = (STATE.proPick && STATE.proPick[`${kind}:${m}`]) || new Set();
  $$(`.picklist[data-kind="${kind}"] .probox`).forEach((b) => {
    b.checked = picked.has(String(b.getAttribute("data-id")));
  });
  const cnt = document.querySelector(`#proCnt_${kind}`);
  if (cnt) cnt.textContent = String(picked.size);
  const note = document.querySelector(`#proNote_${kind}`);
  if (note) note.style.display = m === "white" && !picked.size ? "" : "none";
  const dirtyEl = document.querySelector(`#proDirty_${kind}`);
  if (dirtyEl) {
    const saved = String((STATE.rules && STATE.rules.modes && STATE.rules.modes[kind]) || "black");
    dirtyEl.style.display = m === saved ? "none" : "";
    dirtyEl.innerHTML = `<b>未保存</b>：你正在查看 / 编辑的是<b>${m === "white" ? "白名单" : m === "off" ? "不主动" : "黑名单"}</b>；`
      + `当前生效的仍是<b>${saved === "white" ? "白名单" : saved === "off" ? "不主动" : "黑名单"}</b> —— 点「保存并生效」才会切换。`;
  }
}

export async function hfProSave(kind, btn) {
  // 保存 = 把「正在查看的模式 + 该模式的勾选」一起写下去 —— 这是切换生效的**唯一**入口
  const mode = String((STATE.proMode && STATE.proMode[kind])
    || (STATE.rules && STATE.rules.modes && STATE.rules.modes[kind]) || "black");
  const ids = mode === "off" ? [] : Array.from((STATE.proPick && STATE.proPick[`${kind}:${mode}`]) || []);
  if (btn) btn.classList.add("loading");
  const res = await apiPostSafe("proactive/set", { kind, mode, ids });
  if (btn) btn.classList.remove("loading");
  if (!res.ok) {
    toast("保存失败：" + res.error, "err");
    return;
  }
  STATE.rules = res.data || STATE.rules;
  const warn = mode === "white" && !ids.length ? "（白名单为空：不会主动打扰任何人）" : "";
  toast(`已保存主动${kind === "group" ? "群聊" : "私聊"}${mode === "white" ? "白名单" : mode === "off" ? "不主动" : "黑名单"}：${ids.length} 项${warn}`, "ok");
  refreshCurrent();
}

/** 名单行内过滤（本地，不打服务端） */
export function hfProFilter(v, kind) {
  const q = String(v || "").trim().toLowerCase();
  $$(`.picklist[data-kind="${kind}"] .picklist-row`).forEach((row) => {
    const name = row.getAttribute("data-name") || "";
    const text = row.textContent.toLowerCase();
    row.style.display = !q || name.includes(q) || text.includes(q) ? "" : "none";
  });
}

export async function hfProactiveScan(btn) {
  if (btn) {
    btn.classList.add("loading");
    btn.disabled = true;
  }
  const res = await apiPostSafe("proactive/scan", {});
  if (btn) {
    btn.classList.remove("loading");
    btn.disabled = false;
  }
  toast(res.ok ? `扫描完成：${JSON.stringify(res.data).slice(0, 120)}` : "扫描失败：" + res.error, res.ok ? "ok" : "err");
  refreshCurrent();
}

export async function hfProactivePreview(btn, sessionId) {
  const sid = sessionId || (STATE.rules && STATE.rules.sessions && STATE.rules.sessions[0] && STATE.rules.sessions[0].session_id) || "";
  if (!sid) {
    toast("先在会话清单里选一个会话", "err");
    return;
  }
  if (btn) btn.classList.add("loading");
  // 不再写死 zone：后端会按这个会话的真实关系档 + 真实静默时长试写（2026-10-15 修；旧版写死 FAMILIAR，试看与真发两张皮）
  const res = await apiPostSafe("proactive/preview", { session_id: sid });
  if (btn) btn.classList.remove("loading");
  const d = res.data || {};
  const src = { astrbot: "完整（AstrBot 拼好的那份）", persona: "降级：仅人格（缓存超时 / 该会话还没聊过）", none: "没取到（会吃不到人格）" }[String(d.prompt_source || "")] || String(d.prompt_source || "—");
  const memIds = (d.memory_ids || []).map((x) => "#" + x).join(" ");
  openDrawer(
    "主动消息试看 · " + sid,
    d.ok
      ? `<div class="chip" style="font-size:12.5px;white-space:normal;line-height:1.7">${esc(d.text || "")}</div>
        <div class="dkv" style="margin-top:10px"><div class="k">关系档 / 静默</div><div class="v mono">${esc(d.zone || "—")} · ${fmt1(d.idle_min)} 分钟</div></div>
        <div class="dkv"><div class="k">提示词来源</div><div class="v">${esc(src)}</div></div>
        <div class="dkv"><div class="k">耗时</div><div class="v mono">${d.ms || "—"} ms</div></div>
        <div class="dkv"><div class="k">带入记忆</div><div class="v mono">${d.memories_used || 0} 条${memIds ? " · " + esc(memIds) : ""}</div></div>
        <div class="dkv"><div class="k">使用人格</div><div class="v">${d.persona_used ? "是" : "否"}</div></div>
        <div class="mut" style="font-size:11px;margin:12px 0 4px">实际交给模型的 system prompt${d.layer2_custom ? "（含你自己写的第 2 层）" : "（含内置第 2 层）"}</div>
        <div class="chip" style="white-space:pre-wrap;line-height:1.6;max-height:240px;overflow:auto">${esc(d.system_prompt || "")}</div>
        <div class="mut" style="font-size:11px;margin:10px 0 4px">user prompt（含参考记忆与时间锚）</div>
        <div class="chip" style="white-space:pre-wrap;line-height:1.6;max-height:200px;overflow:auto">${esc(d.user_prompt || "")}</div>`
      : `<div class="mut">生成失败：${esc(d.error || res.error || "原因未知")}</div>`
  );
}

export async function hfProactiveSend(sessionId, btn) {
  if (btn) btn.classList.add("loading");
  const res = await apiPostSafe("proactive/send", { session_id: sessionId, text: "" });
  if (btn) btn.classList.remove("loading");
  toast(res.ok && res.data && res.data.ok ? `已发送：${String(res.data.text || "").slice(0, 30)}` : "发送失败：" + ((res.data && res.data.error) || res.error || ""), res.ok ? "ok" : "err");
  refreshCurrent();
}

/** 用最近有记录的会话把目标填上，省得手抄 QQ 号 */
/** 把最近一次试发的结果画进 #proTestResult（重渲染后依然在） */
function proTestResultHtml() {
  const d = STATE.proTest;
  if (!d) return "";
  const ok = d.ok;
  return ok
    ? `<div class="noteblock" style="border-left-color:var(--success)"><b>已发出</b> → <span class="mono">${esc(d.session_id || "")}</span>
         <div style="margin-top:6px">${esc(d.text || "")}</div>
         <div class="mut" style="font-size:10.5px;margin-top:6px">${d.generated ? "模型现写 " + (d.gen_ms || 0) + "ms · " : "用你给的文本 · "}发送 ${d.send_ms || 0}ms · 合计 ${d.ms || 0}ms</div>
         ${d.warn ? `<div style="font-size:10.5px;margin-top:6px;color:var(--warn-tx)">${esc(d.warn)}</div>` : ""}
       </div>`
    : `<div class="noteblock" style="border-left-color:var(--error)"><b>没发出去</b>
         <div style="margin-top:6px">${esc(d.error || "原因未知")}</div>
         ${d.session_id ? `<div class="mut mono" style="font-size:10.5px;margin-top:6px">目标：${esc(d.session_id)}</div>` : ""}
       </div>`;
}

export async function hfProTestFill() {
  const el = document.querySelector("#proTestTarget");
  if (!el) return;
  const data = STATE.rules && STATE.rules.sessions ? STATE.rules.sessions : [];
  const first = (data || []).find((x) => x && x.session_id);
  if (!first) {
    toast("还没有会话记录，直接填 QQ 号或群号也行", "err");
    return;
  }
  el.value = String(first.session_id);
  toast("已填入：" + first.session_id, "ok");
}

/**
 * 一键试发：填了文本就直接发；留空就让模型现写后**立刻发**。
 * 目标容错：纯数字按 QQ 号（私聊）处理，想发群就填群号；带冒号的当完整会话串。
 * 结果写进 #proTestResult（不只一个 toast），失败原因、耗时、告警都摆出来。
 */
export async function hfProTest(btn) {
  const target = String((document.querySelector("#proTestTarget") || {}).value || "").trim();
  const text = String((document.querySelector("#proTestText") || {}).value || "").trim();
  const box = document.querySelector("#proTestResult");
  if (!target) {
    toast("先填目标：QQ 号 / 群号 / 完整会话串", "err");
    return;
  }
  if (btn) {
    btn.classList.add("loading");
    btn.disabled = true;
  }
  if (box) box.innerHTML = `<div class="mut" style="font-size:11.5px">正在发送…（留空文本时需要模型现写，最多等 30 秒）</div>`;
  const payload = { text };
  if (target.includes(":")) payload.session_id = target;
  else {
    payload.kind = "private";
    payload.ident = target;
  }
  const res = await apiPostSafe("proactive/send", payload);
  if (btn) {
    btn.classList.remove("loading");
    btn.disabled = false;
  }
  const d = res.data || {};
  const ok = res.ok && d.ok;
  // 结果存进 STATE：刷新当前页/切页回来都还看得见刚才发了什么（只弹 toast 会一闪就没）
  STATE.proTest = {
    ok, text: d.text || text, session_id: d.session_id || target,
    gen_ms: d.gen_ms || 0, send_ms: d.send_ms || 0, ms: d.ms || 0,
    generated: !!d.generated, warn: d.warn || "",
    error: ok ? "" : (d.error || res.error || "原因未知"),
  };
  if (box) box.innerHTML = proTestResultHtml();
  toast(ok ? "已发送（" + (d.send_ms || 0) + "ms）" : "发送失败：" + (d.error || res.error || ""), ok ? "ok" : "err");
}

/* ======================= D-PAGE: renderProactiveMatrix ======================= */
export async function renderProactiveMatrix() {
  const [rules, log] = await Promise.all([
    apiSafe("proactive/rules", {}, { sessions: [], allowed: 0, blocked: 0, names: {}, modes: {} }),
    apiSafe("proactive", { limit: 200 }, { items: [], sessions: [], candidates: 0, config: {} }),
  ]);
  if ((rules && rules.__err) || (log && log.__err)) {
    const why = (rules && rules.__err) || (log && log.__err);
    return {
      html: pageHead("主动消息读取失败", `接口现在拿不到名单/日志：${esc(String(why))}`,
        `<button class="btn p" onclick="hfRefresh()">重试</button>`, "⚠")
        + card(`<div class="fnote">⚠ 不渲染空名单，避免把「读取失败」当成「名单为空」再保存一次。</div>`,
               { title: "读取失败" }),
    };
  }
  STATE.rules = rules;
  const cfg = log.config || {};
  const sessions = (rules.sessions || []).slice();
  const logSess = {};
  (log.sessions || []).forEach((s) => { logSess[String(s.session_id || "")] = s; });
  const cap = (cfg.proactive_daily_cap === 0 ? 0 : (Number(cfg.proactive_daily_cap) || 0));
  const gapMin = Number(cfg.proactive_min_interval_min) || 0;
  const idleMin = (cfg.proactive_idle_min === 0 ? 0 : (Number(cfg.proactive_idle_min) || 0));
  const qsTxt = String(cfg.proactive_quiet_start || "");
  const qeTxt = String(cfg.proactive_quiet_end || "");
  const hm = (v) => { const m = /^(\d{1,2}):(\d{2})$/.exec(String(v || "").trim()); return m && Number(m[1]) < 24 && Number(m[2]) < 60 ? Number(m[1]) * 60 + Number(m[2]) : null; };
  const qs = hm(qsTxt), qe = hm(qeTxt);
  const nowMin = new Date().getHours() * 60 + new Date().getMinutes();
  let inQuiet = null;
  if (qs !== null && qe !== null && qs !== qe) inQuiet = qs < qe ? nowMin >= qs && nowMin < qe : nowMin >= qs || nowMin < qe;
  const nowS = Date.now() / 1000;

  // 只列「名单内（可主动）」的会话 —— 不在主动名单里的不在这里显示（用户口径 2026-10-11）；
  // 不再截断到 12 行：外层容器有滑条，向下滑可以看全（用户要求：避免显示不全）。
  const list = sessions
    .filter((s) => s.verdict === "allow")
    .sort((a, b) => (b.msgs || 0) - (a.msgs || 0))
    .slice(0, 80);
  const todaySent = sessions.reduce((a, x) => a + (Number(x.sent_today) || 0), 0);
  const allowed = Number(rules.allowed) || 0;
  const items = log.items || [];
  const reasons = {};
  items.filter((x) => x.status === "skipped").forEach((x) => {
    const r = String(x.reason || "（未记原因）");
    reasons[r] = (reasons[r] || 0) + 1;
  });
  const reasonRows = Object.keys(reasons).sort((a, b) => reasons[b] - reasons[a]).slice(0, 4);
  const topReason = reasonRows[0] || "";
  const hasZone = list.some((s) => s.zone);

  // 「开口时钟 · 额度 · 静默」卡片（自投递审计搬上来）要用的一组真值：config + proactive_baseline + 排布 plan
  const sessAll = Array.isArray(log.sessions) ? log.sessions : [];
  const baseLast = sessAll.reduce((m, x) => Math.max(m, Number(x.last_proactive_ts) || 0), 0);
  const winSent = items.filter((x) => x.status === "sent");
  const lastSent = baseLast || (winSent.length ? Math.max.apply(null, winSent.map((x) => Number(x.ts) || 0)) : 0);
  const gapLeftS = lastSent && gapMin ? Math.max(0, gapMin * 60 - (nowS - lastSent)) : 0;
  const cfgStr = (v) => (v == null ? "" : String(v));

  const gatesTip = inQuiet === null ? (qs !== null && qs === qe ? "起止相同视为不启用静默" : "未配置静默时段（不拦截）") : inQuiet ? "当前在静默期内" : "当前不在静默期";
  const unlockTxt = inQuiet && qe !== null ? `${qeTxt} 解禁` : inQuiet === null ? "不受静默限制" : "现在就可以发（受额度 / 间隔约束）";
  const conclusion = inQuiet === null
    ? `静默时段未启用（${qs !== null ? "起止相同" : "没配置"}）· 今日已发 <b>${fmtNum(todaySent)}</b> 条 · 名单内 <b>${fmtNum(allowed)}</b> 个会话${topReason ? ` · 近期最多被拦原因：<b>${esc(topReason)}</b>` : ""}`
    : inQuiet
      ? `此刻<b>不可发</b>：卡在静默时段（${esc(qsTxt)}–${esc(qeTxt)}），<b>${esc(unlockTxt)}</b> · 今日已发 ${fmtNum(todaySent)} 条 · 剩余额度 ${fmtNum(Math.max(0, cap - todaySent))} 条${topReason ? ` · 近期最多被拦原因：<b>${esc(topReason)}</b>` : ""}`
      : `此刻<b>可发</b>（不在静默期）· 今日已发 <b>${fmtNum(todaySent)}</b> / 上限 ${fmtNum(cap)}（每会话）· 名单内 ${fmtNum(allowed)} 个会话${topReason ? ` · 近期最多被拦原因：<b>${esc(topReason)}</b>` : ""}`;

  const html = `
  ${pageHead(
    "主动 · 闸门矩阵",
    "行 = 会话，列 = 五道闸门：<b>绿=通过、琥珀=拦下</b>。判定顺序是固定的：名单 → 开口时间点 → 静默 → 额度 → 间隔 → 关系档",
    `<button class="btn p" onclick="hfProactiveScan(this)">立即扫描一轮</button>`,
    "☎",
    conclusion
  )}
  <div class="grid g23 sec">
    ${card(`<div id="pmClock"></div><div id="pmClockRows" style="margin-top:10px"></div>`, { title: "开口时钟 · 额度 · 静默" })}
    ${card(`<div style="max-height:min(60vh,560px);overflow-y:auto;overflow-x:auto;scrollbar-gutter:stable"><div id="proMatrix"></div></div>`, {
      title: "会话 × 闸门（此刻的判定）",
      sub: "只列<b>名单内</b>的会话（不在主动名单里的不显示）；行多时滚动看全。点格子看这道闸的当前值，点行看完整判定与快捷试发",
    })}
  </div>
  ${card(
    `<div class="kv">静默窗（当前生效）<span class="mono">${qs !== null && qe !== null ? `${esc(qsTxt)} – ${esc(qeTxt)}` : "未配置（不拦截）"}</span></div>
     <div class="kv">此刻判定<span>${inQuiet === null ? "静默未启用（不会拦主动）" : inQuiet ? `<b>在静默期内</b>（暂不主动开口）· ${esc(qeTxt)} 解禁` : "不在静默期（可以主动）"}</span></div>
     <div class="kv">作用范围<span>仅主动 + 学习任务 · <b>回复侧永不静默</b>（有人说话该回就回，静默不拦任何回复）</span></div>
     <div class="kv">编辑<span><button class="btn xs" onclick="hfGoParams('proactive')" title="去「设置 · 模块与参数」主动域（静默窗 / 额度 / 间隔），带域锚点">去参数中心 · 主动域 →</button></span></div>`,
    { title: "作息时段", sub: "静默窗只掐「主动开口」与学习类后台任务；静默不会拦回复", cls: "sec",
      actions: '<span class="tag ok" title="S5-3 收口（拍-21）：本卡读 config 真值（静默窗/额度/间隔）；不建独立作息表（无冻结 DDL，且与 config 双源）">已上线</span>' }
  )}
  <div class="mut" style="font-size:11.5px;margin-top:10px;line-height:1.8">
    · 被挡住的记录都留在「投递审计」里。<br>
    · 想改谁能收到 → 页底折叠卡「候选人名单」；想改额度 / 静默 / 间隔 → 「设置 · 模块与参数」主动域（上面「作息时段」卡里的编辑入口直达）。
  </div>`;

  return {
    html,
    mount: (host) => {
      const colDefs = [
        { key: "wl", label: "名单", tip: "可见性名单 + 主动名单的判定结果" },
        { key: "quiet", label: "静默", tip: `${qsTxt || "—"} – ${qeTxt || "—"}（${gatesTip}）` },
        { key: "cap", label: "额度", tip: cap ? `每天最多 ${cap} 条（当天随机排布时间点 · 0 点重置）` : "未配置上限" },
        { key: "gap", label: "间隔", tip: gapMin ? `距上次主动需满 ${gapMin} 分钟` : "未配置最小间隔" },
      ];
      if (hasZone) colDefs.push({ key: "zone", label: "关系档", tip: "关系档低于门槛时会拦下" });
      colDefs.push({ key: "today", label: "今日" }, { key: "last", label: "上次主动" });

      mount(host, "#proMatrix", (el) =>
        C.matrix(el, {
          rows: list.map((s) => ({
            label: s.name || `（未命名）${s.id}`,
            sub: s.kind === "group" ? "群" : "私聊",
            tip: `${s.session_id || s.id}${s.why ? " · " + s.why : ""}`,
          })),
          cols: colDefs.map((c) => ({ label: c.label, tip: c.tip })),
          cells: (r, c) => {
            const s = list[r];
            if (!s) return { t: "zero" };
            const def = colDefs[c];
            if (!def) return { t: "zero" };
            const last = logSess[String(s.session_id || "")] || {};
            const lastTs = Number(last.last_proactive_ts) || 0;
            if (def.key === "wl") {
              return s.verdict === "allow"
                ? { t: "pos", lvl: 1, v: "✓", tip: "可主动" + (s.why ? `（${s.why}）` : "") }
                : { t: "warn", lvl: 2, v: "✕", tip: "被拦：" + (s.why || "不在名单内") };
            }
            if (def.key === "quiet") {
              if (inQuiet === null) return { t: "mute", tip: gatesTip };
              return inQuiet
                ? { t: "warn", lvl: 2, v: "✕", tip: `现在 ${String(new Date().getHours()).padStart(2, "0")}:${String(new Date().getMinutes()).padStart(2, "0")} 在静默期内，${unlockTxt}` }
                : { t: "pos", lvl: 1, v: "✓", tip: "当前不在静默期" };
            }
            if (def.key === "cap") {
              if (!cap) return { t: "mute", tip: "未配置每日上限" };
              const n = Number(s.sent_today) || 0;
              return n >= cap
                ? { t: "warn", lvl: 2, v: `${n}/${cap}`, tip: `今日已用完（${n}/${cap}），明天 0 点重置` }
                : { t: "pos", lvl: 1, v: `${n}/${cap}`, tip: `今日还剩 ${cap - n} 条` };
            }
            if (def.key === "gap") {
              if (!gapMin) return { t: "mute", tip: "未配置最小间隔" };
              if (!lastTs) return { t: "mute", tip: "这个会话还没主动过（不受间隔限制）" };
              const left = gapMin * 60 - (nowS - lastTs);
              return left > 0
                ? { t: "warn", lvl: 2, v: `+${Math.ceil(left / 60)}m`, tip: `距上次主动 ${timeAgo(lastTs)}，还需等约 ${Math.ceil(left / 60)} 分钟` }
                : { t: "pos", lvl: 1, v: "✓", tip: `距上次主动 ${timeAgo(lastTs)}，已过 ${gapMin} 分钟间隔` };
            }
            if (def.key === "zone") {
              if (!s.zone) return { t: "mute", tip: "这个会话没有关系档记录" };
              return { t: "fav", lvl: 1, v: MX_ZONE_SHORT[s.zone] || String(s.zone), tip: `关系档 ${s.zone}` };
            }
            if (def.key === "today") {
              return { t: "num", v: String(Number(s.sent_today) || 0), dim: !(Number(s.sent_today) || 0), tip: `今日已主动 ${Number(s.sent_today) || 0} 条` };
            }
            return { t: "num", v: lastTs ? timeAgo(lastTs) : "—", dim: true, tip: lastTs ? `上次主动：${fmtTime(lastTs, true)}` : "没有主动记录" };
          },
          legend: [["pos", "通过"], ["warn", "被拦下"], ["mute", "不适用（未配置 / 无记录）"]],
          note: "静默是一段时间窗、额度是一个计数器、间隔是一个计时器。空格 = 这一列对这个会话不适用。",
          onPick: (r, c) => {
            const s = list[r];
            const def = colDefs[c] || {};
            if (!s || !def.key) return;
            const last = logSess[String(s.session_id || "")] || {};
            const rows = {
              wl: `判定：<b>${s.verdict === "allow" ? "可主动" : "被拦"}</b>${s.why ? `（${esc(s.why)}）` : ""}`,
              quiet: `配置：<b>${esc(qsTxt || "—")} – ${esc(qeTxt || "—")}</b><br>现在：${esc(gatesTip)}<br>${inQuiet ? esc(unlockTxt) : ""}`,
              cap: `配置：每会话每天 <b>${cap || "不限"}</b> 条<br>今日已发：<b>${Number(s.sent_today) || 0}</b> 条`,
              gap: `配置：最小间隔 <b>${gapMin || "不限"}</b> 分钟<br>上次主动：<b>${Number(last.last_proactive_ts) ? esc(timeAgo(last.last_proactive_ts)) : "无记录"}</b>`,
              zone: `关系档：<b>${esc(String(s.zone || "未结算"))}</b>`,
              today: `今日已主动 <b>${Number(s.sent_today) || 0}</b> 条`,
              last: Number(last.last_proactive_ts) ? `上次主动：<b>${esc(timeAgo(last.last_proactive_ts))}</b>（${esc(fmtTime(last.last_proactive_ts, true))}）` : "还没有主动过",
            }[def.key];
            openDrawer(`闸门 · ${def.label} · ${s.name || s.id}`, `<div style="font-size:12.5px;line-height:2">${rows || "—"}</div>`);
          },
          onRow: (r) => {
            const s = list[r];
            if (!s) return;
            const last = logSess[String(s.session_id || "")] || {};
            const sid = s.session_id || "";
            openDrawer(
              `会话判定 · ${s.name || s.id}`,
              `<div class="kv">会话<span class="mono">${esc(sid || s.id)}</span></div>
               <div class="kv">名单判定<span>${s.verdict === "allow" ? "可主动" : "被拦"}${s.why ? `（${esc(s.why)}）` : ""}</span></div>
               <div class="kv">消息量<span class="mono">${fmtNum(s.msgs || 0)}</span></div>
               <div class="kv">会话静默<span class="mono">${fmt1((Number(s.idle_min) || 0) / 60)} 小时</span></div>
               <div class="kv">今日已发<span class="mono">${Number(s.sent_today) || 0} / ${cap || "不限"}</span></div>
               <div class="kv">上次主动<span>${Number(last.last_proactive_ts) ? esc(timeAgo(last.last_proactive_ts)) : "—"}</span></div>
               <div class="mut" style="font-size:11px;margin-top:9px">静默满 ${fmtNum(idleMin)} 分钟才有资格；判定顺序：可见性名单 → 主动名单 → 开口时间点 → 额度 / 时钟 / 关系档。</div>
               <div class="row" style="gap:6px;margin-top:12px">
                 <button class="btn s" onclick="hfProactivePreview(this, ${jsStr(sid)})" ${sid ? "" : "disabled"}>试看一条</button>
                 <button class="btn s" onclick="hfProactiveSend(${jsStr(sid)}, this)" ${sid ? "" : "disabled"}>真发一条</button>
               </div>`
            );
          },
          emptyText: "还没有会话记录：先在群里聊几句，或把要主动的人加进折叠卡里的名单",
        })
      );

      const clockEl = host.querySelector("#pmClock");
      if (clockEl) {
        const line = (label, body, right) => `<div class="hfline">
        <div class="row bt" style="margin-bottom:5px;font-size:12px">
          <span>${label}</span><span class="${right.cls || ""}">${right.text}</span></div>
        ${body}
        <div class="row bt mut" style="font-size:10.5px;margin-top:4px"><span>${right.l}</span><span>${right.r}</span></div>
      </div>`;
        const parts = [];
        // 合并条（全天 0:00–23:59）：白线 = 现在；绿线 = 预定开口（上方标注时刻）；斜纹 = 静默区间；小灰杠 = 已开口
        const plan = log.plan || {};
        const slotList = (Array.isArray(plan.slots) ? plan.slots : []).map(Number).filter((tt) => tt > 0);
        const dayS = Number(plan.day_start) || 0;
        const dayE = Number(plan.day_end) || dayS + 86400;
        const planFresh = !!dayS && String(plan.date || "") === String(plan.today || "");
        const hmOf = (ts) => { const d = new Date(ts * 1000); return String(d.getHours()).padStart(2, "0") + ":" + String(d.getMinutes()).padStart(2, "0"); };
        const pctOf = (ts) => (dayE > dayS ? Math.max(0, Math.min(100, ((ts - dayS) / (dayE - dayS)) * 100)) : 0);
        const sentTicks = winSent.filter((x) => !(x.guard && x.guard.manual) && Number(x.ts) >= dayS);
        let barMarks = `<span class="otrack"></span>`;
        if (qs !== null && qe !== null && qs !== qe) {
          const qsegs = qs < qe ? [[qs, qe]] : [[qs, 1440], [0, qe]];
          barMarks += qsegs.map(([a, b]) => `<span class="oq" style="left:${(a / 14.4).toFixed(2)}%;width:${((b - a) / 14.4).toFixed(2)}%"></span>`).join("");
        }
        barMarks += sentTicks.map((x) => `<span class="otick" style="left:${pctOf(Number(x.ts)).toFixed(2)}%"></span>`).join("");
        if (planFresh) {
          barMarks += slotList.map((ts) => {
            const p = pctOf(ts);
            const align = p < 5 ? "transform:translateX(0)" : p > 95 ? "transform:translateX(-100%)" : "transform:translateX(-50%)";
            return `<span class="gopen" style="left:${p.toFixed(2)}%"></span><span class="golbl" style="left:${p.toFixed(2)}%;${align}">${hmOf(ts)}</span>`;
          }).join("");
        }
        barMarks += `<span class="hfnow" style="left:${pctOf(nowS).toFixed(2)}%"></span>`;
        const nextTs = slotList.length ? slotList[0] : 0;
        let cdTxt = "未排布", cdCls = "mut";
        if (planFresh) {
          if (!nextTs) {
            cdTxt = "已排完";
          } else if (nextTs <= nowS) {
            cdTxt = "已到点"; cdCls = "ok";
          } else {
            const leftS = nextTs - nowS;
            cdTxt = "还需 " + (leftS >= 3600 ? `${Math.floor(leftS / 3600)} 小时 ${Math.round((leftS % 3600) / 60)} 分` : `${Math.max(1, Math.round(leftS / 60))} 分钟`);
            cdCls = "";
          }
        }
        const rightTxt = !planFresh ? "今日开口未排布"
          : !nextTs ? "今日预定开口已排完"
          : nextTs <= nowS ? `已到预定开口时间（${hmOf(nextTs)}）`
          : `下次开口 ${hmOf(nextTs)}`;
        parts.push(line("开口时钟",
          `<div class="row" style="gap:10px;align-items:flex-end">
            <div class="${cdCls}" style="min-width:92px;font-size:12px;white-space:nowrap">${cdTxt}</div>
            <div class="hfopen" style="flex:1">${barMarks}</div>
          </div>`,
          { text: rightTxt, cls: cdCls === "ok" ? "ok" : cdCls === "mut" ? "mut" : "",
            l: !planFresh ? "未排布（主动扫描开启后自动生成）" : lastSent ? `上次主动 ${timeAgo(lastSent)}` : "还没主动过",
            r: "白线 = 现在 · 绿线 = 预定开口 · 斜纹 = 静默区间" }));
        if (cap) {
          const n = Math.min(cap, 24);
          const dots = Array.from({ length: n }).map((_, k) => `<i class="${k < todaySent ? "on" : ""}"></i>`).join("");
          const midnight = new Date(); midnight.setHours(24, 0, 0, 0);
          const hoursLeft = Math.max(0, (midnight.getTime() / 1000 - nowS) / 3600);
          const nextRel = `每天 0 点重置（还有约 ${hoursLeft < 1 ? Math.max(1, Math.round(hoursLeft * 60)) + " 分钟" : Math.round(hoursLeft) + " 小时"}）`;
          parts.push(line("今日额度（自然日）", `<div class="hfdots">${dots}</div>`,
            { text: `${todaySent} / ${cap} 条${todaySent >= cap ? "（已达上限）" : ""}`, cls: todaySent >= cap ? "warn" : "ok",
              l: nextRel, r: "0:00–24:00 · 手动试发不占额度" }));
        } else {
          parts.push(line("今日额度（自然日）", `<div class="hfdots"><i></i></div>`, { text: `${todaySent} 条（未配置上限）`, cls: "mut", l: "没设上限", r: "不会因额度被拦" }));
        }
        clockEl.innerHTML = parts.join("");
      }
      const clockRows = host.querySelector("#pmClockRows");
      if (clockRows) {
        const row = (k, v, extra) => `<div class="kv">${k}<span>${v}${extra ? ` <span class="mut">${extra}</span>` : ""}</span></div>`;
        clockRows.innerHTML =
          row("距上次主动", lastSent ? esc(timeAgo(lastSent)) : "全表没有发送记录", gapMin ? (gapLeftS > 0 ? "还需等约 " + Math.ceil(gapLeftS / 60) + " 分钟（最小间隔仅对同一会话生效）" : "已过最小间隔（会话级护栏）") : "未配置最小间隔") +
          row("今日额度（自然日）", cap ? `${fmtNum(todaySent)} / ${fmtNum(cap)} 条` : `${fmtNum(todaySent)} 条（无上限）`, "自然日 0:00–24:00（按 AstrBot 时区）· 每天 0 点重置 · 手动试发不占额度") +
          row("静默时段", qs !== null && qe !== null ? `${esc(cfgStr(cfg.proactive_quiet_start))} – ${esc(cfgStr(cfg.proactive_quiet_end))}` : "未配置", inQuiet === null ? "（不拦截）" : inQuiet ? "现在在静默期内" : "现在不在静默期") +
          row("静默多久才考虑", cfg.proactive_idle_min != null ? `${fmtNum(cfg.proactive_idle_min)} 分钟` : "未配置") +
          row("影子模式", cfg.proactive_shadow ? "已开启（只挑选、不真发）" : "关闭（会真发）");
      }
    },
  };
}

export async function renderProactiveAudit() {
  const data = await apiSafe("proactive", { limit: 300 }, { items: [], sessions: [], candidates: 0, config: {} });
  const items = data.items || [];
  const st = {};
  items.forEach((x) => (st[x.status || "candidate"] = (st[x.status || "candidate"] || 0) + 1));
  const html = `
  ${pageHead("投递审计", "每一次「想开口」都被记下来了：候选、跳过、发送、失败，以及当时的护栏判据", `<button class="btn s" onclick="hfFoldJump('候选人名单')">候选池 ↓</button>`, "☰")}
  ${kpis(
    kpiCard("paAll", fmtNum(items.length), "本页窗口记录")
    + kpiCard("paSent", fmtNum(st.sent || 0), "窗口内已发送")
    + kpiCard("paSkip", fmtNum(st.skipped || 0), "窗口内被拦下")
    + kpiCard("paFail", fmtNum(st.failed || 0), "发送失败")
  )}
  ${card(`<div id="paHeat"></div>`, { title: "投递节律（24 小时）", sub: "本页流水按小时聚合：颜色越深 = 这个小时里「想开口」的次数越多；灰 = 一次都没有" })}
  <div class="grid g23 sec">
    ${card(`<div id="paChart"></div>`, { title: "状态构成", sub: "被拦下的多半是静默时长/额度/名单，reason 里写了原因" })}
    ${card(`<div id="paZones"></div>`, { title: "主动目标的关系档", sub: "关系越近越容易放行" })}
  </div>
  ${card(`<div id="paTimeline"></div>`, { title: "时间轴", sub: "倒序，最多 60 条" })}`;
  return {
    html,
    mount: (host) => {
      const P = C.palette();   // 主题色板（深/浅色都能适配）
  // 24 小时投递节律热力条（本页流水）
  mount(host, "#paHeat", (el) => {
    const hours = new Array(24).fill(0);
    items.forEach((x) => {
      const ts = Number(x.ts) || 0;
      if (!ts) return;
      hours[new Date(ts * 1000).getHours()] += 1;
    });
    const peak = Math.max.apply(null, hours.concat([1]));
    return C.heatstrip(el, {
      height: 44,
      ticks: true,
      items: hours.map((v, h) => ({
        label: (h % 3 === 0 ? String(h).padStart(2, "0") : ""),
        value: h,
        weight: v / peak,
        color: v ? null : P.line,
      })),
    });
  });

  mount(host, "#paChart", (el) =>
        C.barSplit(el, {
          items: Object.keys(st).map((k) => ({
            label: { sent: "窗口内已发送", skipped: "被跳过", failed: "失败", candidate: "仅候选" }[k] || k,
            value: st[k],
            color: { sent: "#00c853", skipped: "#ffc107", failed: "#f44336", candidate: "#3c96ca" }[k] || "#8e8e93",
          })),
          height: 16,
        })
      );
      const zones = {};
      items.forEach((x) => (zones[x.zone || "未结算"] = (zones[x.zone || "未结算"] || 0) + 1));
      mount(host, "#paZones", (el) =>
        C.bars(el, { items: Object.keys(zones).map((z) => ({ label: z, value: zones[z], color: ZONE_COLOR[z] || "#8e8e93" })) })
      );
      mount(host, "#paTimeline", (el) =>
        C.timeline(el, {
          items: items.slice(0, 60).map((x) => ({
            ts: x.ts,
            title: `${x.status || "candidate"} · ${x.session_id || "—"}`,
            desc: `${x.reason || ""}${x.text ? " · " + String(x.text).slice(0, 60) : ""}${x.prompt_source ? " · 提示词:" + ({ astrbot: "完整", persona: "仅人格", none: "无" }[x.prompt_source] || x.prompt_source) : ""}${x.memory_ids ? " · 记忆:" + String(x.memory_ids).slice(0, 40) : ""}`,
            color: { sent: "#00c853", skipped: "#ffc107", failed: "#f44336" }[x.status] || "#3c96ca",
            tag: x.zone || "",
          })),
          timeFmt: (ts) => fmtTime(ts, true),
        })
      );
    },
  };
}

export async function renderProactiveConf(opts = {}) {
  const [rules, filter] = await Promise.all([
    apiSafe("proactive/rules", {}, { sessions: [], allowed: 0, blocked: 0 }),
    apiSafe("contacts/filter", {}, { modes: {}, counts: {} }),
  ]);
  if ((rules && rules.__err) || (filter && filter.__err)) {
    const why = (rules && rules.__err) || (filter && filter.__err);
    return {
      html: pageHead("主动参数读取失败", `接口现在拿不到名单现状：${esc(String(why))}`,
        `<button class="btn p" onclick="hfRefresh()">重试</button>`, "⚠")
        + card(`<div class="fnote">⚠ 不渲染「放行 0 / 拦截 0 / 不过滤」这种假状态，避免你按错误数字去改名单。</div>`,
               { title: "读取失败" }),
    };
  }
  const base = (kind) => {
    const mode = String((filter.modes || {})[kind] || "off");
    const n = ((filter.counts || {})[kind] || {})[mode === "black" ? "black" : "white"] || 0;
    return mode === "off" ? "不过滤" : `${mode === "black" ? "黑名单" : "白名单"} ${n} 项`;
  };
  return renderDomainConf("proactive", {
    bare: !!opts.bare,
    recentFirst: !!opts.recentFirst,
    chartId: "dcChart_proactive",
    // 名单写入（私聊/群聊 白/黑）**不迁**：它有专属编辑面（主动页「候选人名单」折叠卡走
    // proactive/set 接口 + 「设置 · 名单与可见性」，与 AstrBot 原生名单语义耦合），
    // 在参数中心再渲染一份裸 id 输入 = 第二写入面。这 4 个键仍在那两处照常编辑。
    exclude: ["proactive_wl_private", "proactive_bl_private", "proactive_wl_group", "proactive_bl_group"],
    actions: `<button class="btn s" onclick="hfProactiveScan(this)">扫描一轮</button>
              <button class="btn s" onclick="goPage('settings','contacts')">名单与可见性 →</button>`,
    side: card(
      `<div class="mut" style="font-size:11.5px;line-height:1.8">
        <b>主动名单</b>：放行 <b>${rules.allowed || 0}</b> 个会话，拦截 <b>${rules.blocked || 0}</b> 个。<br>
        原生白名单是地基（实时只读）：为空 = 全部放行，主动名单说了算；非空 = 只在名单内收窄，名单外的会话自动失效。<br>
        <span class="mut">（参考：可见性名单当前 群聊 ${esc(base("group"))} · 私聊 ${esc(base("private"))}）</span><br>
        <span class="mut">名单的编辑保持在原处：主动页「候选人名单」折叠卡 / 「设置 · 名单与可见性」—— 参数中心不重复渲染裸 id 输入。</span></div>
       <div class="row" style="gap:6px;margin-top:10px;flex-wrap:wrap">
         <button class="btn s" onclick="goPage('settings','contacts')">改名名单</button>
         <button class="btn s" onclick="goPage('messages','proactive')">回主动页（审计 ↓ / 候选池 ↓）</button>
       </div>`,
      { title: "名单现状" }
    ),
    // 第 2 层提示词编辑器：沉在参数页最底下，可展开（用户 2026-10-15 指定位置：主动最底下那栏）
    bottom: (values, dom) => layer2EditorHtml(values, ((dom || {}).builtin || {}).proactive_instruction || ""),
  });
}

/* ---------------- 主动消息 · 第 2 层提示词（本轮情境） ---------------- */

/** 折叠段落式编辑器；「内置默认」全文由后端 config/domains.builtin 带过来（不再前端复制一份） */
export function layer2EditorHtml(values, builtin) {
  const cur = String(((values || {}).proactive_instruction) || "");
  const def = String(builtin || "");
  const state = cur.trim() ? "当前：自定义" : "当前：插件内置";
  return `<details class="hf-l2">
    <summary style="cursor:pointer"><b>✎ 主动消息 · 第 2 层提示词（本轮情境）</b>
      <span class="mut" style="font-size:11.5px;margin-left:8px">${state} · 点这里展开看 / 改</span></summary>
    <div class="mut" style="font-size:11.5px;line-height:1.85;margin:8px 0 6px">
      这是主动开口时贴给模型的「系统级任务指令」，接在 AstrBot 完整提示词之后（撤回重发那条通道不吃这份，只走自己的通知块）。
      留空 = 用插件内置。占位符：<span class="mono">{zone}</span> 关系区间 · <span class="mono">{tone}</span> 语气线 · <span class="mono">{idle}</span> 距对方上次说话的分钟数。<br>
      ⚠ 建议至少保留「只输出一句话、不加引号 / 括号动作」这类硬要求，否则模型可能把思考过程也发出去。
    </div>
    <textarea id="hfL2ta" data-hf-cfg="proactive_instruction" rows="9"
      style="width:100%;font-family:ui-monospace,Consolas,monospace;font-size:12px;line-height:1.7;box-sizing:border-box"
      placeholder="留空 = 用插件内置（点下面「载入内置默认」可以看看它长什么样）">${esc(cur)}</textarea>
    <div class="row" style="gap:6px;margin-top:8px;flex-wrap:wrap;align-items:center">
      <button class="btn xs" onclick="hfL2LoadDefault()">载入内置默认</button>
      <button class="btn xs" onclick="hfL2Reset()">恢复默认（清空）</button>
      <button class="btn xs p" onclick="hfL2Save(this)">保存第 2 层</button>
      <span class="mut" style="font-size:11px" id="hfL2msg"></span>
    </div>
    <details style="margin-top:8px"><summary class="mut" style="cursor:pointer;font-size:11px">看一眼内置默认全文（只读；改了这里不会保存）</summary>
      <div class="chip" style="white-space:pre-wrap;font-size:11px;line-height:1.7;margin-top:6px;opacity:.8" id="hfL2def">${esc(def)}</div>
    </details>
  </details>`;
}

export function hfL2LoadDefault() {
  const ta = document.getElementById("hfL2ta");
  const defEl = document.getElementById("hfL2def");
  if (!ta) return;
  ta.value = defEl ? (defEl.textContent || "") : "";
  toast("已载入内置默认，改完记得点「保存第 2 层」", "ok");
}

export function hfL2Reset() {
  const ta = document.getElementById("hfL2ta");
  if (!ta) return;
  ta.value = "";
  toast("已清空：点「保存第 2 层」后生效（回到插件内置）", "ok");
}

export async function hfL2Save(btn) {
  const ta = document.getElementById("hfL2ta");
  const msg = document.getElementById("hfL2msg");
  if (!ta) return;
  if (btn) { btn.classList.add("loading"); btn.disabled = true; }
  const res = await apiPostSafe("config/save", { patch: { proactive_instruction: String(ta.value || "") } });
  if (btn) { btn.classList.remove("loading"); btn.disabled = false; }
  const ok = !!(res && res.ok);
  const errTxt = (res && res.error) || "保存失败";
  const nw = ok ? nativeWriteError(res) : "";
  if (ok) noteConfigTouched(takeConfigDirty(["proactive_instruction"]));   // 参数中心「最近改过置顶」：只记手改过的
  if (msg) msg.textContent = ok ? (nw ? "已写入插件库，但原生配置写回失败：" + nw : "已保存 ✓（留空 = 内置默认）") : "保存失败：" + errTxt;
  toast(nw ? "已写入插件库，但原生配置写回失败：" + nw : (ok ? "第 2 层提示词已保存" : "保存失败：" + errTxt),
        (ok && !nw) ? "ok" : "err");
}

/* ==================================================================
   好感：列表 / 洞察 / 参数
   ================================================================== */
let favSort = "daily";

/* ======================= D-PAGE: renderFavourList ======================= */
export async function renderFavourList() {
  const res = await apiSafe("favours", {}, { items: [] });
  const items = (res.items || []).slice();
  items.sort(favSort === "lifetime" ? (a, b) => (b.lifetime || 0) - (a.lifetime || 0) : (a, b) => (b.daily || 0) - (a.daily || 0));
  STATE.favours = items;
  const days = mxDayKeys(14);
  const daily = items.map((f) => Number(f.daily) || 0);
  const warm = items.filter((f) => (Number(f.daily) || 0) > 0).length;
  const cold = items.filter((f) => (Number(f.daily) || 0) < 0).length;
  const worst = items.slice().sort((a, b) => (Number(a.daily) || 0) - (Number(b.daily) || 0))[0];
  const best = items.slice().sort((a, b) => (Number(b.daily) || 0) - (Number(a.daily) || 0))[0];

  /** 每人每天的 delta：来自 evidence 里的「YYYY-MM-DD ±N」记账行（人工校准 / 日结算都写在这里） */
  const seriesOf = (f) => {
    const map = {};
    (f.evidence || []).forEach((e) => {
      const m = /^(\d{4}-\d{2}-\d{2})\s*([+-][\d.]+)/.exec(String(e));
      if (m) map[m[1]] = (map[m[1]] || 0) + (Number(m[2]) || 0);
    });
    return map;
  };
  const rows = items.slice(0, 12);
  const seriesAll = rows.map(seriesOf);
  const maxAbs = Math.max(1, ...seriesAll.map((m) => Math.max(1, ...days.map((d) => Math.abs(m[d] || 0)))));

  const concl = items.length
    ? `跟踪 <b>${fmtNum(items.length)}</b> 人 · 今天变暖 <b>${fmtNum(warm)}</b> 人 / 变冷 <b>${fmtNum(cold)}</b> 人` +
      (best && Number(best.daily) ? ` · 今天最暖：<b>${esc(best.name || best.user_id)}</b>（+${fmt1(best.daily)}）` : "") +
      (worst && Number(worst.daily) < 0 ? ` · 降温最快：<b>${esc(worst.name || worst.user_id)}</b>（${fmt1(worst.daily)}）` : "")
    : `还没有结算过好感：聊上几轮、或跑一轮好感结算就会出现关系记录`;

  const html = `
  ${pageHead(
    "好感 · 人 × 14 天",
    "行 = 人，列 = 最近 14 天的每日增减：<b>绿=当天变暖、红=当天变冷、灰=那天没有记账</b>；点行看这个人的结算依据与人工校准",
    `${segmentHtml([{ value: "daily", label: "按今日" }, { value: "lifetime", label: "按累计" }], favSort, "favsort")}
     <button class="btn s" onclick="hfGoParams('favour')" title="去「设置 · 模块与参数」好感域（结算模式 / 上限 / 半衰期），带域锚点">参数 →</button>`,
    "♥",
    concl
  )}
  <div class="grid g23 sec">
    ${card(`<div id="fvMatrix"></div>`, {
      title: "关系变化日历",
      sub: "逐日格来自结算记账（evidence 里的日期行）；没有记账的当天是灰格 —— 不是 0 分",
    })}
    ${card(`<div id="fvSide"></div>`, { title: "今天谁在变" })}
  </div>
  <div class="mut" style="font-size:11.5px;margin-top:10px;line-height:1.8">
    · 分数是<b>连续量</b>：daily 会冷却，lifetime 只增不减；分数本身<b>永不进 prompt</b>，只有区间会变成语气描述。<br>
    · 想手工调某个人 → 点行，在抽屉里「人工校准」；档位分布与结算事件流在页底折叠卡「关系洞察」。
  </div>`;

  return {
    html,
    mount: (host) => {
      host.querySelectorAll('[data-seg="favsort"] .sg').forEach((b) =>
        b.addEventListener("click", () => {
          favSort = b.dataset.v;
          b.parentElement.querySelectorAll(".sg").forEach((x) => x.classList.toggle("on", x === b));
          syncSeg(b.parentElement);
          refreshCurrent();
        })
      );

      mount(host, "#fvMatrix", (el) =>
        C.matrix(el, {
          rows: rows.map((f) => ({
            label: f.name || f.user_id,
            sub: esc(String(f.zone || "")),
            tip: `${f.session_id || f.user_id} · 档位 ${f.zone || "未结算"} · 今日 ${fmt1(f.daily)} · 累计 ${fmt1(f.lifetime)}`,
          })),
          cols: days.map((d) => ({ label: mxDayShort(d), tip: d })).concat([{ label: "累计" }, { label: "今日" }]),
          cells: (r, c) => {
            const f = rows[r];
            if (!f) return { t: "zero" };
            const map = seriesAll[r] || {};
            if (c < days.length) {
              const v = map[days[c]];
              if (v == null) return { t: "zero", tip: `${days[c]}：这天没有结算记账` };
              return {
                t: v > 0 ? "pos" : "neg",
                lvl: Math.max(1, Math.min(3, Math.ceil((Math.abs(v) / maxAbs) * 3))),
                v: (v > 0 ? "+" : "") + fmt1(v),
                tip: `${days[c]} 记账 ${v > 0 ? "+" : ""}${fmt1(v)}`,
              };
            }
            if (c === days.length) return { t: "num", v: fmt1(f.lifetime || 0), tip: `累计 ${fmt1(f.lifetime || 0)}（只增不减）` };
            const d0 = Number(f.daily) || 0;
            return { t: "num", v: (d0 > 0 ? "+" : "") + fmt1(d0), dim: !d0, tip: `今日 ${fmt1(d0)}（会随时间冷却）` };
          },
          legend: [["pos", "当天变暖"], ["neg", "当天变冷"], ["zero", "没有记账"]],
          note: "排序看段控「按今日 / 按累计」。灰色 ≠ 0 分，只是那天没有结算记账。",
          onRow: (r) => {
            const f = rows[r];
            if (!f) return;
            const ev = (f.evidence || []).slice(-12).reverse();
            openDrawer(
              `好感详情 · ${f.name || f.user_id}`,
              `<div class="kv">档位<span>${esc(f.zone || "未结算")}</span></div>
               <div class="kv">今日 / 累计<span class="mono">${fmt1(f.daily)} / ${fmt1(f.lifetime)}</span></div>
               <div class="kv">会话<span class="mono">${esc(f.session_id || f.user_id)}</span></div>
               <div class="sec" style="margin:10px 0 6px;font-size:12px"><b>结算依据</b>（最近 12 条）</div>
               ${ev.length ? ev.map((e) => `<div class="kv"><span style="min-width:0;overflow:hidden;text-overflow:ellipsis">${esc(String(e).slice(0, 80))}</span></div>`).join("") : '<div class="mut">没有留下依据记录</div>'}
               <div class="row" style="gap:6px;margin-top:12px">
                 <button class="btn s" onclick="hfFavourAdjustOpen(${jsStr(f.user_id)})">人工校准</button>
               </div>`
            );
          },
          emptyText: "还没有好感记录：等人和机器人聊上几轮就会自动结算",
        })
      );

      const sideEl = host.querySelector("#fvSide");
      if (sideEl) {
        const delta = items
          .slice()
          .sort((a, b) => Math.abs(Number(b.daily) || 0) - Math.abs(Number(a.daily) || 0))
          .slice(0, 6);
        const maxD = Math.max(1, ...delta.map((f) => Math.abs(Number(f.daily) || 0)));
        const bars = delta.map((f) => {
          const v = Number(f.daily) || 0;
          const w = Math.max(2, Math.round((Math.abs(v) / maxD) * 100));
          const col = v >= 0 ? "var(--success,#22a06b)" : "var(--error,#e05555)";
          return `<div class="hfline" style="border-bottom:0;padding:7px 0">
            <div class="row bt" style="font-size:12px;margin-bottom:4px">
              <span>${esc(f.name || f.user_id)}</span>
              <span class="mono" style="color:${v >= 0 ? "inherit" : "var(--error,#e05555)"}">${v > 0 ? "+" : ""}${fmt1(v)}</span></div>
            <div class="hfbar" style="height:8px"><i style="width:${w}%;background:${col}"></i></div>
          </div>`;
        }).join("");
        sideEl.innerHTML =
          `${bars || '<div class="mut" style="font-size:11.5px">还没有结算记录</div>'}
           <div class="kv" style="margin-top:10px">跟踪中<span class="mono">${fmtNum(items.length)}</span></div>
           <div class="kv">亲密及以上<span class="mono">${fmtNum(items.filter((f) => zoneRank(f.zone) >= 4).length)}</span></div>
           <div class="kv">冷淡 / 敌对<span class="mono">${fmtNum(items.filter((f) => zoneRank(f.zone) <= 1).length)}</span></div>
           <div class="kv">平均今日分<span class="mono">${fmt1(daily.length ? daily.reduce((s, x) => s + x, 0) / daily.length : 0)}</span></div>`;
      }
    },
  };
}


export function hfFavourAdjustOpen(userId) {
  const f = (STATE.favours || []).find((x) => String(x.user_id) === String(userId)) || {};
  openDrawer(
    "人工校准 · " + userId,
    `<div class="dkv"><div class="k">当前 daily / lifetime</div><div class="v mono">${Math.round(f.daily || 0)} / ${f.lifetime == null ? "—" : Math.round(f.lifetime)}</div></div>
     <div style="margin:12px 0 4px" class="mut" style="font-size:11px">调整量（可为负）</div>
     <input class="inp" id="hfAdjDelta" value="+5" style="width:100%">
     <div style="margin:12px 0 4px" class="mut" style="font-size:11px">原因（写进审计流水）</div>
     <input class="inp" id="hfAdjReason" value="人工校准" style="width:100%">
     <div style="margin:12px 0 4px" class="mut" style="font-size:11px">生效范围</div>
     <select class="inp" id="hfAdjScope" style="width:100%"><option value="both">daily 与 lifetime 同时</option><option value="daily">只改 daily</option><option value="lifetime">只改 lifetime</option></select>
     <div class="row bt" style="margin-top:16px">
       <button class="btn" onclick="closeDrawer()">取消</button>
       <button class="btn p" onclick="hfFavourAdjustSubmit(${jsStr(userId)})">确认调整</button>
     </div>`
  );
}

export async function hfFavourAdjustSubmit(userId) {
  const delta = parseFloat(($("#hfAdjDelta") || {}).value);
  if (!delta) {
    toast("调整量无效", "err");
    return;
  }
  const res = await apiPostSafe("favour/adjust", {
    user_id: userId,
    delta,
    reason: ($("#hfAdjReason") || {}).value || "人工校准",
    scope: ($("#hfAdjScope") || {}).value || "both",
  });
  toast(res.ok ? "好感已调整" : "调整失败：" + res.error, res.ok ? "ok" : "err");
  if (res.ok) {
    closeDrawer();
    refreshCurrent();
  }
}

export async function renderFavourInsight() {
  const ins = await apiSafe("favour/insight", {}, { total: 0, zones: {}, avg_daily: 0, top: [], events: [] });
  const zones = ins.zones || {};
  const html = `
  ${pageHead("关系洞察", "谁在你这里最亲、关系都在什么档、结算系统最近改过什么", `<button class="btn s" onclick="hfFoldJump('top')">回好感列表 ↑</button>`, "◎")}
  ${kpis(
    kpiCard("fiAll", fmtNum(ins.total), "跟踪用户")
    + kpiCard("fiAvg", fmt1(ins.avg_daily), "平均今日分")
    + kpiCard("fiClose", fmtNum((zones.CLOSE || 0) + (zones.DEVOTED || 0)), "亲密及以上")
    + kpiCard("fiHostile", fmtNum(zones.HOSTILE || 0), "敌对档", `<span class="tag ${zones.HOSTILE ? "err" : "gy"}">${zones.HOSTILE ? "需要关注" : "无"}</span>`)
  )}
  <div class="grid g23 sec">
    ${card(`<div id="fiZones"></div>`, { title: "关系档位分布", sub: "区间由 daily 结算得出" })}
    ${card(`<div id="fiTop"></div>`, { title: "累计好感 Top8", sub: "lifetime 只增不减，代表长期关系厚度" })}
  </div>
  ${card(`<div id="fiEvents"></div>`, { title: "结算事件流", sub: "来自审计表的 favour* 记录（人工校准、自动结算）" })}`;
  return {
    html,
    mount: (host) => {
      mount(host, "#fiZones", (el) =>
        C.barSplit(el, {
          items: Object.keys(zones).map((z) => ({ label: z, value: zones[z], color: ZONE_COLOR[z] || "#8e8e93" })),
          height: 18,
        })
      );
      mount(host, "#fiTop", (el) =>
        C.bars(el, {
          items: (ins.top || []).map((f) => ({ label: f.name || f.user_id, value: Number(f.lifetime) || 0, sub: `今日 ${Math.round(f.daily || 0)}`, color: ZONE_COLOR[f.zone] || "#3c96ca" })),
          valueFmt: (v) => fmt1(v),
        })
      );
      mount(host, "#fiEvents", (el) =>
        C.timeline(el, {
          items: (ins.events || []).slice(0, 40).map((e) => ({
            ts: e.ts,
            title: e.action || "favour",
            desc: `${e.target || ""} ${e.detail || ""}`,
            color: "#e0559a",
          })),
          timeFmt: (ts) => fmtTime(ts, true),
        })
      );
    },
  };
}

export async function renderFavourConf(opts = {}) {
  return renderDomainConf("favour", {
    bare: !!opts.bare,
    recentFirst: !!opts.recentFirst,
    chartId: "dcChart_favour",
    side: card(
      `<div class="mut" style="font-size:11.5px;line-height:1.8">
        · 结算模式 <b>tag</b>：由模型在回复里打标（close/neutral/cold…）—— 目前唯一实现的模式<br>
        · 人工校准可在「好感列表」里对单个人加/减分，审计流水会记下来源</div>
       <div class="row" style="gap:6px;margin-top:10px"><button class="btn s" onclick="goPage('messages','favour')">去好感列表 →</button></div>`,
      { title: "结算模式怎么选" }
    ),
  });
}

/* ==================================================================
   消息事件 = 一页（研究文档 + 内容盘点后重写）

   排版依据（每条都有出处，不是随手）：
   · 真机 skipped 5948 / sent 25 / candidate 1 → 本页主角是「为什么被拦下」，不是「发了什么」；
     饼/环在此零信息量（一条线里的一粒沙）→ 原因排行 + 文字数值冗余。
   · 顶部 KPI 状态带：长度是最快的前注意通道，先回答"现在什么状态"。
   · 节律热力图当主角：24×7 = 168 格，一图看一天/一周。
   · 开口转化用 stageBars，且"沉默/退避"标 noDrop —— 它们是"开口"的**兄弟**不是下游，
     写成"少了 93%"会彻底误导。
   · 事件流：三类各取最近 N 条再按时间倒序（否则被主动日志淹没）；点一行**原地**展开，
     不重渲染（不刷新、不丢滚动）。颜色 + 图标 + 文字三路冗余，不只靠颜色。
   · 参数与名单仍走旧 4 个子页：MSG_LEGACY 是承重墙（跨页按钮还在用它），
     所以只把菜单收敛成一个入口，不删旧页。
   ================================================================== */

const MH = { kind: "", session: "", view: "gate", data: null };

const MH_KIND = {
  gate: { label: "门控", icon: "◎", color: "#6ea8fe" },
  proactive: { label: "主动", icon: "↗", color: "#a78bfa" },
  recall: { label: "撤回", icon: "↩", color: "#ff6b6b" },
};

const MH_STYLE = `
  <style>
    /* 近黑底 + 石墨卡；强调色只用 蓝 / 蓝紫 / 青（用户明确反对藏青深蓝与发白） */
    .mh-kpis { display: grid; grid-template-columns: repeat(6, minmax(0, 1fr)); gap: 9px; }
    .mh-kpi { position: relative; border: 1px solid var(--bd); border-radius: 11px; padding: 9px 11px; background: var(--surface2); overflow: hidden; }
    .mh-kpi .bar { position: absolute; left: 0; top: 0; height: 2px; width: 100%; }
    .mh-kpi .v { font-size: 20px; font-weight: 650; line-height: 1.15; }
    .mh-kpi .l { font-size: 10.5px; color: var(--tx2); margin-top: 2px; }
    .mh-kpi .n { font-size: 10px; color: var(--tx2); opacity: .78; margin-top: 3px; line-height: 1.45; }
    .mh-grid2 { display: grid; grid-template-columns: minmax(0, 1.05fr) minmax(0, .95fr); gap: 10px; }
    .mh-grid3 { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr) minmax(0, 1fr); gap: 10px; }
    .mh-rank { display: grid; gap: 6px; }
    .mh-rank .r { display: grid; grid-template-columns: 1fr auto; gap: 8px; align-items: center; }
    .mh-rank .lab { font-size: 11.5px; color: var(--tx); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .mh-rank .num { font-size: 11.5px; font-family: var(--mono, monospace); color: var(--tx2); }
    .mh-rank .track { height: 7px; border-radius: 4px; background: var(--surface3, var(--surface2)); overflow: hidden; margin-top: 3px; }
    .mh-rank .fill { height: 100%; border-radius: 4px; }
    .mh-flow { max-height: 430px; overflow: auto; }
    .mh-ev { display: grid; grid-template-columns: 74px 1fr; gap: 9px; padding: 7px 4px; border-bottom: 1px solid var(--bd); cursor: pointer; }
    .mh-ev:hover { background: var(--surface2); }
    .mh-ev .t { font-size: 10.5px; font-family: var(--mono, monospace); color: var(--tx2); opacity: .78; padding-top: 2px; }
    .mh-ev .badge { display: inline-flex; align-items: center; gap: 4px; font-size: 10.5px; padding: 1px 7px; border-radius: 999px; border: 1px solid var(--bd); }
    .mh-ev .sess { font-size: 11px; color: var(--tx2); }
    .mh-ev .sum { font-size: 12px; margin-top: 3px; line-height: 1.6; }
    .mh-evdet { grid-column: 2; background: var(--surface2); border-radius: 8px; padding: 8px 10px; margin-top: 6px; font-size: 11.5px; line-height: 1.75; }
    .mh-heatgrid { display: grid; gap: 3px; }
    .mh-heatrow { display: grid; grid-template-columns: 34px repeat(24, minmax(0, 1fr)); gap: 3px; align-items: center; }
    .mh-heat { height: 15px; border-radius: 3px; border: 1px solid rgba(255,255,255,.05); }
    .mh-heat:hover { outline: 1px solid rgba(255,255,255,.45); }
    .mh-heatday { font-size: 10px; color: var(--tx2); text-align: right; }
    .mh-heath { font-size: 9px; color: var(--tx2); opacity: .6; text-align: center; }
    .mh-heatlegend { display: flex; align-items: center; gap: 4px; margin-top: 9px; font-size: 10.5px; color: var(--tx2); }
    .mh-heatlegend i { width: 16px; height: 9px; border-radius: 2px; display: inline-block; }
    .mh-chip { border: 1px solid var(--bd); background: transparent; color: var(--tx2); font-size: 11.5px; padding: 3px 10px; border-radius: 999px; cursor: pointer; }
    .mh-chip.on { background: var(--primary); border-color: var(--primary); color: #fff; }
  </style>`;

/** 折叠卡（本页自带：views-memory 里那个没导出） */
function mhFold(id, title, sub, inner) {
  return `<div class="card sec" style="padding:0">
    <div class="row bt" style="gap:8px;padding:11px 13px">
      <div style="min-width:0"><div class="pn">${esc(title)}</div><div class="pd">${sub}</div></div>
      <button class="btn xs" onclick="hfFold(this,'${id}')">展开</button>
    </div>
    <div id="${id}" style="display:none;padding:0 13px 13px">${inner}</div>
  </div>`;
}

/** 会话可读名（和记忆库同一套：私聊 · 100000000，完整 id 进 title） */
function mhSess(sid) {
  const t = String(sid || "");
  const m = t.match(/^(.*?):([A-Za-z]+Message):(.+)$/);
  if (!m) return t || "—";
  const kind = /group/i.test(m[2]) ? "群聊" : /friend|private|direct/i.test(m[2]) ? "私聊" : m[2];
  return kind + " · " + m[3];
}

function mhChip(label, on, onclick) {
  return `<button class="mh-chip${on ? " on" : ""}" onclick="${onclick}">${esc(label)}</button>`;
}

/** 横向排行：条与数字同排，数字永远可读（不靠悬停） */
function mhRank(rows, color) {
  const max = Math.max(1, ...rows.map((r) => Number(r[1]) || 0));
  return `<div class="mh-rank">${rows.map((r) => {
    const label = r[0];
    const n = r[1];
    const w = Math.max(2, Math.round((Number(n) / max) * 100));
    return '<div><div class="r"><span class="lab" title="' + esc(String(label)) + '">' + esc(String(label)) + '</span>'
      + '<span class="num">' + fmtNum(n) + '</span></div>'
      + '<div class="track"><div class="fill" style="width:' + w + "%;background:" + color + '"></div></div></div>';
  }).join("")}</div>`;
}

/** 一句话结论：把一条事件压成人的话（可扫读） */
function mhSummary(ev) {
  const d = ev.data || {};
  if (ev.kind === "gate") {
    const dec = d.decision === "reply" ? "开口" : d.decision === "backoff" ? "退避" : "沉默";
    return "分数 " + fmt1(d.score) + "（阈值 " + fmt1(d.threshold) + "）→ " + dec;
  }
  if (ev.kind === "proactive") {
    if (d.status === "sent") return "已发出：" + (String(d.text || "").slice(0, 46) || "（无内容）");
    return (d.status === "candidate" ? "待发候选" : "未发送") + "：" + String(d.reason || "—")
      + "（闲置 " + fmt1(d.idle_min) + " 分钟）";
  }
  if (ev.kind === "recall") {
    return String(d.action || "处理") + (d.matched ? " · 命中记忆" : "") + "：" + String(d.detail || "").slice(0, 40);
  }
  return "";
}

/** 事件明细（点开才渲染 = 渐进披露） */
function mhDetail(ev) {
  const d = ev.data || {};
  if (ev.kind === "gate") {
    const fs = Object.keys(d.factors || {});
    const chips = fs.length
      ? fs.map((k) => '<span class="chip">' + esc(k) + " " + (Number(d.factors[k]) >= 0 ? "+" : "") + fmt1(d.factors[k]) + "</span>").join("")
      : '<span class="mut">这条没记因子明细</span>';
    return '<div class="row wrap" style="gap:6px">' + chips + "</div>"
      + '<div class="mut" style="margin-top:5px">会话 ' + esc(String(d.session_id || "—")) + " · 用户 "
      + esc(String(d.user_id || "—")) + " · note " + esc(String(d.note || "—")) + "</div>";
  }
  if (ev.kind === "proactive") {
    const g = d.guard || {};
    const chips = Object.keys(g).slice(0, 10)
      .map((k) => '<span class="chip">' + esc(k) + "=" + esc(String(g[k])) + "</span>").join("");
    return '<div class="row wrap" style="gap:6px">' + chips + "</div>"
      + (d.text ? '<div class="mut" style="margin-top:5px">内容：' + esc(String(d.text).slice(0, 200)) + "</div>" : "")
      + '<div class="mut" style="margin-top:5px">会话 ' + esc(String(d.session_id || "—")) + " · 档位 "
      + esc(String(d.zone || "—")) + " · 闲置 " + fmt1(d.idle_min) + " 分</div>";
  }
  return '<div class="mut">会话 ' + esc(String(d.session_id || "—")) + " · 操作人 "
    + esc(String(d.operator_id || "—")) + " · 消息 " + esc(String(d.message_id || "—")) + "</div>";
}

/** 旧子页 id：这些页面还能进（菜单不再暴露，但按钮与跨页跳转要用） */
/* ==========================================================================
   消息事件：**5 个平级子页签，域页内部没有任何第二层导航**
   --------------------------------------------------------------------------
   用户反复强调两条：①不要把子菜单藏在子菜单里（也不要在页顶再挂一条 chips/锚点栏）
   ②界面别太复杂。所以这里：
     · 主导航 = rail 的「消息事件」→ 5 个平级子页签（总览 / 门控 / 主动 / 撤回 / 好感）
     · 域页内部**没有**第二层页签、没有分段栏；「名单 / 审计 / 洞察」收进**页底一张默认折叠的卡**
       （点开才见，不占首屏、不产生导航层级）；**参数已收口到「设置 · 模块与参数」**，
       这里只留「参数 →」跳转（带域锚点 #settings/params/<域>）
   ========================================================================== */

/** 域 → 主体渲染函数（域页的主体内容） */
const DOMAIN_RENDERERS = {
  gate: () => renderGateLive(),
  proactive: () => renderProactiveMatrix(),
  recall: () => renderRecallFeed(),
  favour: () => renderFavourList(),
};

/** 域 → 页底折叠区（名单 / 审计 / 洞察都在这里，按顺序折叠，不占首屏）
 *  收尾批：四个域的参数折叠区已收口到「设置 · 模块与参数」——这里不再渲染参数，
 *  功能页只留页头/卡片里的「参数 →」跳转（带域锚点 #settings/params/<域>）。 */
const DOMAIN_EXTRAS = {
  gate: [],
  proactive: [["候选人名单（谁会收到主动消息）", renderProactivePool],
              ["投递审计（节律 / 时间轴）", renderProactiveAudit]],
  recall: [],
  favour: [["关系洞察", renderFavourInsight]],
};

export function msgDomainKey(sub) {
  return "dom:" + String(sub || "hub");
}

/** 门控/主动/撤回/好感这四张表（hf_gate_log / hf_proactive_log / hf_recall_log / hf_favours）
 *  本身没有 persona 列，做不到按人格过滤 —— 那就必须**在页面上说清楚**，而不是让用户以为
 *  切换「查看视角」会让这些数字跟着变（审查实证：切人格后这四类数字完全相同）。 */
const PERSONA_SCOPE_NOTE = `<div class="mut" style="font-size:11px;margin:-2px 0 10px;padding:6px 9px;border-radius:8px;background:rgba(255,193,7,.10);border:1px solid rgba(255,193,7,.30)">⚠ 本页是<b>全局</b>统计（未按人格过滤）：门控 / 主动 / 撤回 / 好感四张流水表没有 persona 列，所以切换上方「查看视角」<b>不会</b>改变这里的数字 —— 按人格隔离的是<b>记忆与画像</b>。</div>`;

export async function renderMsgDomain(sub) {
  const key = String(sub || "hub");
  if (key === "hub" || !DOMAIN_RENDERERS[key]) return renderMessagesHub("hub");
  let out = null;
  try {
    out = (await DOMAIN_RENDERERS[key]()) || { html: "" };
  } catch (err) {
    return { html: card(`<div class="mut">这一页读取失败：<b>${esc(String((err && err.message) || err))}</b></div>`, { title: "读取失败" }) };
  }
  const extras = [];
  for (const [label, fn] of DOMAIN_EXTRAS[key] || []) {
    let o = null;
    try {
      o = await fn();
    } catch (err) {
      o = { html: `<div class="mut">${esc(label)} 读取失败：<b>${esc(String((err && err.message) || err))}</b></div>` };
    }
    extras.push([label, o || { html: "" }]);
  }
  // 折叠区：<details> 原生折叠，默认收起 —— 不占首屏、不需要任何 JS，也不构成导航层级
  const folded = extras
    .map(([label, o]) => `<details style="margin-top:12px">
        <summary style="cursor:pointer;font-size:12.5px;padding:6px 0"><span class="tag gy">${esc(label)}</span>
          <span class="mut">点开查看 / 修改（默认收起，不占首屏）</span></summary>
        <div style="margin-top:8px">${o.html}</div></details>`)
    .join("");
  return {
    html: PERSONA_SCOPE_NOTE + out.html + folded,
    mount: (host) => {
      try {
        if (typeof out.mount === "function") out.mount(host);
      } catch (err) {
        console.warn("[starheart] 域页主体挂载失败", err);
      }
      extras.forEach(([label, o]) => {
        try {
          if (typeof o.mount === "function") o.mount(host);
        } catch (err) {
          console.warn("[starheart] 折叠区挂载失败：" + label, err);
        }
      });
    },
  };
}

const MH_LEGACY_SUBS = ["gate", "gate_live", "gate_conf", "recall", "recall_live", "recall_conf",
  "proactive", "proactive_pool", "proactive_audit", "proactive_conf",
  "favour", "favour_list", "favour_insight", "favour_conf"];

/** 消息事件页（一页版）。sub 属于旧子页 → 交给 legacy 实现。 */
/* ======================= D-PAGE: renderMessagesHub ======================= */
export async function renderMessagesHub(sub) {
  if (sub && sub !== "hub" && MH_LEGACY_SUBS.indexOf(sub) >= 0) return renderMessagesLegacy(sub);

  let data;
  try {
    data = await api("message/hub", { limit: 60, session_id: MH.session });
  } catch (err) {
    const msg = (err && err.message) || String(err);
    return { html: card(
      `<div class="mut" style="line-height:1.8">读不到事件数据：<b>${esc(msg)}</b><br>
       可点右上「刷新当前页数据」重试。</div>`,
      { title: "事件总览读取失败" }) };
  }
  MH.data = data;
  const k = data.kpi || {};
  const stream = (data.stream || []).filter((x) => (MH.kind ? x.kind === MH.kind : true));

  // 追加两路（撤回 / 好感的小时分布）+ 域开关：拿不到就不画那一行，不编
  const [rc, ins, dom] = await Promise.all([
    apiSafe("recalls", { limit: 200 }, { items: [], config: {} }),
    apiSafe("favour/insight", {}, { events: [] }),
    apiSafe("config/domains", {}, { values: {} }),
  ]);
  const vals = (dom && dom.values) || {};
  const weekAgo = Date.now() / 1000 - 7 * 86400;
  const sumGrid = (grid) => {
    const out = new Array(24).fill(0);
    (grid || []).forEach((line) => (line || []).forEach((v, h) => { out[h] += Number(v) || 0; }));
    return out;
  };
  const gateH = sumGrid((data.grid || {}).gate);
  const proH = sumGrid((data.grid || {}).pro);
  const recH = new Array(24).fill(0);
  const favH = new Array(24).fill(0);
  (rc.items || []).forEach((x) => { const ts = Number(x.ts) || 0; if (ts > weekAgo) recH[new Date(ts * 1000).getHours()] += 1; });
  (ins.events || []).forEach((e) => { const ts = Number(e.ts) || 0; if (ts > weekAgo) favH[new Date(ts * 1000).getHours()] += 1; });

  // 「现在」：静默窗口 / 额度 —— 状态灯与"下一步"要靠它
  const hm = (v) => { const m = /^(\d{1,2}):(\d{2})$/.exec(String(v || "").trim()); return m && Number(m[1]) < 24 && Number(m[2]) < 60 ? Number(m[1]) * 60 + Number(m[2]) : null; };
  const qs = hm(vals.proactive_quiet_start), qe = hm(vals.proactive_quiet_end);
  const nowMin = new Date().getHours() * 60 + new Date().getMinutes();
  let inQuiet = null;
  if (qs !== null && qe !== null && qs !== qe) inQuiet = qs < qe ? nowMin >= qs && nowMin < qe : nowMin >= qs || nowMin < qe;
  const cap = Number(vals.proactive_daily_cap) || 0;
  const last2h = stream.filter((x) => Number(x.ts) > Date.now() / 1000 - 7200).length;

  const lamp = (name, color, line1, line2) => `<div style="background:var(--surface2);border:1px solid var(--bd);border-radius:10px;padding:9px 11px">
    <div class="row" style="gap:7px;align-items:center;font-size:12.5px"><i style="width:7px;height:7px;border-radius:50%;background:${color};display:inline-block"></i><b>${esc(name)}</b></div>
    <div class="mut" style="font-size:10.5px;margin-top:3px;line-height:1.6">${line1}</div>
    <div style="font-size:11.5px;margin-top:6px">${line2}</div></div>`;

  const lamps =
    lamp("门控", vals.gate_enabled === false ? "var(--error,#e05555)" : "var(--success,#22a06b)",
      `阈值 ${fmt1(vals.gate_threshold)} · 影子模式${vals.gate_shadow ? "开" : "关"} · 累计评估 ${fmtNum(k.gate_total)}`,
      vals.gate_enabled === false ? "<b>已停用</b> → 去门控页开启" : "运行中")
    + lamp("主动", inQuiet === null ? "var(--tx3,#8e8e99)" : inQuiet ? "var(--warning,#e0a800)" : "var(--success,#22a06b)",
      `静默 ${esc(String(vals.proactive_quiet_start || "—"))}–${esc(String(vals.proactive_quiet_end || "—"))} · 额度 ${cap || "不限"}/会话`,
      inQuiet === null ? "静默未启用" : inQuiet ? `<b>被静默挡着</b> · ${esc(String(vals.proactive_quiet_end || ""))} 解禁` : "<b>可以发</b>")
    + lamp("撤回处理", "var(--success,#22a06b)",
      `处理方式 ${esc(String(vals.recall_handling || "—"))} · 系数 ×${fmt1(Number(vals.recall_weaken_factor) || 0.3)} · 保留 ${fmtNum(vals.recall_keep_days)} 天`,
      "命中即按配置处理")
    + lamp("好感结算", vals.favour_enabled === false ? "var(--error,#e05555)" : "var(--success,#22a06b)",
      `跟踪 ${fmtNum((ins.top || []).length ? Math.max(Number(ins.total) || 0, (ins.top || []).length) : Number(ins.total) || 0)} 人 · 平均今日分 ${fmt1(ins.avg_daily || 0)}`,
      vals.favour_enabled === false ? "<b>已停用</b>" : "正常结算");

  const conclusion = `门控 ${vals.gate_enabled === false ? "已停用" : "在跑"}（累计评估 ${fmtNum(k.gate_total)} · 开口 ${fmtNum(k.gate_reply)}）· 主动 ${
    inQuiet === null ? "静默未启用" : inQuiet ? `被静默挡着（${esc(String(vals.proactive_quiet_end || ""))} 解禁）` : "可以发"
  } · 撤回命中累计 ${fmtNum(k.recall_total)} · 最近 2 小时 <b>${fmtNum(last2h)}</b> 条事件`;

  const html = MH_STYLE + pageHead(
    "消息事件 · 指挥台",
    "先看「现在」：四个模块能不能动作；再看「最近」：四类事件在 24 小时里的分布；明细在各域页（这里不再重复画一遍它们的主图）",
    '<select class="inp" style="width:190px" onchange="hfMhSet(\'session\', this.value)">'
    + '<option value="">全部会话</option>'
    + Object.keys(data.sessions || {}).map((sid) => '<option value="' + esc(sid) + '"' + (MH.session === sid ? " selected" : "") + ">"
      + esc(mhSess(sid)) + "（" + fmtNum(data.sessions[sid]) + "）</option>").join("")
    + "</select>"
    + '<button class="btn s" onclick="hfMhExport()">导出 CSV</button>',
    "✉",
    conclusion
  )
  + '<div class="sec" style="display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:9px">' + lamps + "</div>"
  + '<div class="card sec" style="padding:12px 13px"><div class="pn">事件密度 · 模块 × 24 小时</div>'
  + '<div class="pd">近 7 天各小时累计（门控 / 主动来自节律网格，撤回 / 好感来自各自流水）；颜色越深＝这个小时里越多，<b>数字写在格子里</b></div>'
  + '<div id="mhMatrix" style="margin-top:9px"></div></div>'
  + '<div class="card sec" style="padding:12px 13px">'
  + '<div class="row bt wrap" style="gap:8px"><div style="min-width:0"><div class="pn">统一事件流</div>'
  + '<div class="pd">门控 / 主动 / 撤回 三类各取最近 ' + fmtNum(data.per_kind || 20) + ' 条再按时间倒序；点一行原地展开明细（不刷新页面）</div></div>'
  + '<div class="row wrap" style="gap:6px">' + mhChip("全部类型", !MH.kind, "hfMhSet('kind','')")
  + mhChip("◎ 门控", MH.kind === "gate", "hfMhSet('kind','gate')")
  + mhChip("↗ 主动", MH.kind === "proactive", "hfMhSet('kind','proactive')")
  + mhChip("↩ 撤回", MH.kind === "recall", "hfMhSet('kind','recall')") + "</div></div>"
  + '<div class="mh-flow" id="mhFlow" style="margin-top:8px">'
  + (stream.length ? stream.map((ev) => {
    const K = MH_KIND[ev.kind] || MH_KIND.gate;
    return '<div class="mh-ev" data-ts="' + Number(ev.ts) + '" onclick="hfMhOpen(' + Number(ev.ts) + ')">'
      + '<div class="t">' + esc(fmtTime(ev.ts)) + "</div><div>"
      + '<div class="row wrap" style="gap:7px;align-items:center">'
      + '<span class="badge" style="color:' + K.color + ";border-color:" + K.color + '55"><b>' + K.icon + "</b> " + esc(K.label) + "</span>"
      + '<span class="sess" title="' + esc(String(ev.session_id || "")) + '">' + esc(mhSess(ev.session_id)) + "</span></div>"
      + '<div class="sum">' + esc(mhSummary(ev)) + "</div></div></div>";
  }).join("") : emptyBlock("没有事件 —— 换个会话或类型试试", "✉"))
  + "</div></div>";

  return {
    html: html,
    mount: (host) => {
      const rows = [
        { label: "门控裁决", tone: "acc", data: gateH, tip: "门控评估（按小时；近 7 天累计）" },
        { label: "主动投递", tone: "pro", data: proH, tip: "主动消息记录（候选 / 跳过 / 发送）" },
        { label: "撤回事件", tone: "rec", data: recH, tip: "撤回事件（来自撤回流水，近 7 天）" },
        { label: "好感结算", tone: "fav", data: favH, tip: "好感结算事件（来自结算审计，近 7 天）" },
      ].filter((r) => r.data.some((v) => v > 0));
      const cols = Array.from({ length: 24 }).map((_, h) => ({ label: h % 3 === 0 ? String(h).padStart(2, "0") : "", tip: `${h}:00 – ${h}:59` }));
      mount(host, "#mhMatrix", (el) =>
        C.matrix(el, {
          rows: rows.map((r) => ({ label: r.label, tip: r.tip })),
          cols: cols,
          cells: (r, c) => {
            const row = rows[r];
            if (!row) return { t: "zero" };
            const v = row.data[c] || 0;
            if (!v) return { t: "zero", tip: `${row.label} · ${c}:00 这个小时没有记录` };
            const max = Math.max(1, ...row.data);
            return mxHeat(v, max, row.tone, `${row.label} · ${c}:00–${c}:59：${v} 条`);
          },
          legend: [["acc", "门控"], ["pro", "主动"], ["rec", "撤回"], ["fav", "好感"], ["zero", "无记录"]],
          note: "每一行按<b>自己</b>的最大值分深浅（行间量级差得远），所以纵向比的是时间分布，横向比时间分布要看格子里的数字。",
          onPick: (r, c) => {
            const row = rows[r];
            if (!row) return;
            toast(`${row.label} · ${c}:00–${c}:59：${row.data[c] || 0} 条`, "");
          },
          emptyText: "近 7 天没有事件记录",
        })
      );
    },
  };
}


export function hfMhSet(key, value) {
  MH[key] = String(value == null ? "" : value);
  refreshCurrent();
}

export function hfMhView(v) {
  MH.view = v === "pro" ? "pro" : "gate";
  refreshCurrent();
}

/** 点一行事件：纯 DOM 展开/收起（不重渲染 → 不刷新、不丢滚动） */
export function hfMhOpen(ts) {
  const flow = document.getElementById("mhFlow");
  if (!flow || !MH.data) return;
  const list = MH.data.stream || [];
  let ev = null;
  for (let i = 0; i < list.length; i++) {
    if (Number(list[i].ts) === Number(ts)) { ev = list[i]; break; }
  }
  if (!ev) return;
  const row = flow.querySelector('.mh-ev[data-ts="' + String(ts) + '"]');
  if (!row) return;
  const old = row.querySelector(".mh-evdet");
  const all = flow.querySelectorAll(".mh-evdet");
  for (let i = 0; i < all.length; i++) all[i].remove();
  if (old) return;
  const box = document.createElement("div");
  box.className = "mh-evdet";
  box.innerHTML = mhDetail(ev);
  row.appendChild(box);
}

/** 导出事件流 CSV */
export function hfMhExport() {
  const rows = (MH.data && MH.data.stream) || [];
  const head = "ts,kind,session_id,user_id,summary,raw\n";
  const body = rows.map((ev) => [fmtTime(ev.ts), ev.kind, ev.session_id || "", ev.user_id || "",
    mhSummary(ev), JSON.stringify(ev.data || {})]
    .map((x) => '"' + String(x).replace(/"/g, '""') + '"').join(",")).join("\n");
  const csv = "\ufeff" + head + body;
  const blob = new Blob([csv], { type: "text/csv;charset=utf-8" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "starheart-message-events-" + new Date().toISOString().slice(0, 10) + ".csv";
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  setTimeout(() => URL.revokeObjectURL(a.href), 4000);
  toast("已导出 " + rows.length + " 条事件", "ok");
}

/**
 * StarHeart 面板 · 拟人指标（一级页）
 *
 * 行为质量只在这一页看聚合：六张 KPI 卡 + 单条长度分布 + 门控因子分布 + 长度豁免打标。
 * 数据全部来自 GET /page/metrics —— 本页不做任何本地估算，接口怎么报就怎么显示：
 *   ok 出数；样本不足标出 n / 需要 N；未实现说明等表 / 机制；读取失败把接口给的原因贴出来；
 *   连字段都没读到就显式报「接口未返回」。绝不静默吞成「待建」，也不拿 0 顶替。
 * 明细不在这里重复列表，卡面上的跳转去对应页看。
 *
 * 接口契约（字段名以契约为准；旧别名仅作兼容，未命中即显式报错，不吞）：
 *   {
 *     window_days: <int>,
 *     metrics: {
 *       silence_rate:        {value, n, n_min, state, definition, reason?},
 *       burst_length:        {value, unit:"条", n, n_min, state, …},   // 连发段长 P50
 *       length_p50:          {value, unit:"字", …},
 *       reply_interval_p50:  {value, unit:"秒", …},
 *       sticker_rate:        {…},
 *       expr_hit_rate:       {…}
 *     },
 *     length_dist:  {state, n, n_min, bins: [{label, n}], p50, p90, mean, reason?},
 *     gate_factors: {state, n, n_min, items: [{factor, n, distinct, constant, top: [{value, n}]}], reason?},
 *     length_exempt: {…}            // 打标机制上线后才有
 *   }
 * state ∈ ok / INSUFFICIENT_DATA / NOT_IMPLEMENTED / ERROR；state != ok 时接口不给 value。
 */

import * as C from "./charts.js?v=1.0.5-3";
import {
  api, esc, fmt1, fmtNum, fmtPct, card, kpis, mount, emptyBlock, pageHead,
} from "./store.js?v=1.0.5-3";

/* 六张 KPI 卡（卡序：长度 / 连发 / 间隔 / 表情 / 沉默 / 命中，与设计稿一致）。
   keys = 接口字段别名：契约字段名在前，历史别名在后（比对忽略大小写与下划线）。
   nMin 仅在接口没给 n_min 时兜底；单位也优先读接口给的 unit。 */
const CARDS = [
  { id: "mtLen", keys: ["length_p50", "len_p50", "p50_len", "single_len_p50", "p50_length"], label: "单条长度 P50", unit: "字", nMin: 200,
    jump: { page: "expression", sub: "lib", label: "表达库" } },
  { id: "mtBurst", keys: ["burst_length", "burst_avg", "burst", "burst_count", "burst_per_round"], label: "连发条数", unit: "条", nMin: 200,
    jump: { page: "messages", sub: "gate", label: "门控" } },
  { id: "mtGap", keys: ["reply_interval_p50", "gap_p50", "interval_p50", "reply_gap_p50", "delay_p50"], label: "回复间隔 P50", unit: "秒", nMin: 200,
    jump: null, pending: "输出参数（目标页还没上线）" },
  { id: "mtEmoji", keys: ["sticker_rate", "emoji_rate", "emoji"], label: "表情率", unit: "%", rate: true, nMin: 250,
    jump: null, pending: "输出参数（目标页还没上线）" },
  { id: "mtSil", keys: ["silence_rate", "silent_rate", "rate_silent"], label: "沉默率", unit: "%", rate: true, nMin: 250,
    jump: { page: "messages", sub: "gate", label: "门控" } },
  { id: "mtExpr", keys: ["expr_hit_rate", "expression_hit_rate", "hit_rate", "expr_hit"], label: "表达命中率", unit: "%", rate: true, nMin: 250,
    jump: { page: "expression", sub: "lib", label: "表达库" } },
];

/* ---------------- 接口取值（对字段名宽容，对「没有」严格） ---------------- */

const norm = (s) => String(s == null ? "" : s).toLowerCase().replace(/[^a-z0-9]/g, "");
const numOf = (v) => (v == null || v === "" || (typeof v === "string" && !v.trim()) ? null : (Number.isFinite(Number(v)) ? Number(v) : null));
const clamp = (s, n) => (String(s).length > n ? String(s).slice(0, n - 1) + "…" : String(s));

/** 后端 reason 可能自带「读失败(x)」外壳 —— 去重，卡面不出现「读取失败（读失败(x)）」 */
function reasonText(r) {
  const t = String(r == null ? "" : r).trim();
  const m = t.match(/^读(?:取)?失败\s*[（(]\s*(.*?)\s*[)）]\s*$/);
  return m ? m[1] : t;
}

/** 在一层对象里按别名找字段；找不到返回 undefined（= 显式报错，不猜） */
function pickRaw(data, aliases) {
  if (!data || typeof data !== "object") return undefined;
  const want = aliases.map(norm);
  const boxes = [data.metrics, data.kpis, data.items, data.kpi].filter((x) => x && typeof x === "object" && !Array.isArray(x));
  boxes.push(data);
  for (const box of boxes) {
    for (const k of Object.keys(box)) {
      if (want.indexOf(norm(k)) >= 0) return box[k];
    }
  }
  const arr = [data.metrics, data.kpis, data.items].find((x) => Array.isArray(x));
  if (arr) {
    for (const it of arr) {
      if (!it || typeof it !== "object") continue;
      if (want.indexOf(norm(it.key || it.name || it.metric || it.id)) >= 0) return it;
    }
  }
  return undefined;
}

/** 归一一项指标：{state, value, n, nMin, unit, reason}；state ∈ ok / INSUFFICIENT_DATA / NOT_IMPLEMENTED / ERROR / MISSING */
function readMetric(raw) {
  if (raw == null) return { state: "MISSING" };
  if (typeof raw === "number" || typeof raw === "string") {
    const v = numOf(raw);
    return v == null ? { state: "MISSING" } : { state: "ok", value: v };
  }
  if (typeof raw !== "object") return { state: "MISSING" };
  const st = String(raw.state || raw.status || "").trim().toUpperCase();
  let value = null;
  for (const k of ["value", "v", "val", "rate", "ratio", "p50", "avg", "mean"]) {
    if (k in raw) { value = numOf(raw[k]); if (value != null) break; }
  }
  let n = null;
  for (const k of ["n", "samples", "sample", "count", "rows"]) {
    if (k in raw) { n = numOf(raw[k]); if (n != null) break; }
  }
  const nMin = numOf(raw.n_min);
  const unit = raw.unit != null ? String(raw.unit) : "";
  const reason = raw.reason != null ? String(raw.reason) : "";
  if (st === "ERROR" || st === "READ_FAILED" || st === "FAILED") return { state: "ERROR", n, nMin, unit, reason };
  if (st === "NOT_IMPLEMENTED" || st === "TODO" || st === "PENDING") return { state: "NOT_IMPLEMENTED", n, nMin, unit, reason };
  if (st === "INSUFFICIENT_DATA" || st === "INSUFFICIENT" || st === "NO_DATA") return { state: "INSUFFICIENT_DATA", n, nMin, unit, reason };
  // 非 ok 不给数；ok 却缺数值 = 契约没对齐 → 显式报错（不猜成待建）
  if (value == null) {
    return { state: "ERROR", n, nMin, unit, reason: reason || (st === "OK" ? "接口标了 ok 但没给出数值" : "接口没有给出状态，也没有可读的数值") };
  }
  return { state: "ok", value, n, nMin, unit, reason };
}

/* ---------------- 卡片渲染 ---------------- */

/** 比值 → 百分比文案：≤1 按比值解释，>1 视为接口已经给了百分数 */
const fmtRate = (v) => (Number(v) <= 1 ? fmtPct(Number(v), 1) : `${fmt1(v)} %`);

const jumpBtn = (j) => (j ? `<button class="btn xs" onclick="goPage('${esc(j.page)}','${esc(j.sub)}')">${esc(j.label)} →</button>` : "");

/** 一张 KPI 卡：出数就出数，没数就把「为什么没有」写在脸上 */
function metricCard(def, item) {
  const st = item.state;
  const unit = item.unit ? String(item.unit) : String(def.unit || "").trim();
  const nMin = item.nMin != null ? item.nMin : def.nMin;
  const nBit = item.n != null ? ` · n=${fmtNum(item.n)}` : "";
  let value = `<span class="mut" style="font-size:15px;font-weight:500">—</span>`;
  let extra = "";
  let stateLine = "";
  if (st === "ok") {
    value = def.rate ? fmtRate(item.value) : `${fmt1(item.value)}<span style="font-size:12px;font-weight:500"> ${esc(unit)}</span>`;
    if (item.n != null) extra = `<span class="mut" style="font-size:10.5px">n=${fmtNum(item.n)}</span>`;
  } else if (st === "INSUFFICIENT_DATA") {
    stateLine = `<span class="tag warn">样本不足</span><span class="mut" style="font-size:10.5px">（${item.n != null ? `n=${fmtNum(item.n)}` : "样本数未返回"}${nMin ? ` / 需要 ${fmtNum(nMin)}` : ""}）</span>`;
  } else if (st === "NOT_IMPLEMENTED") {
    stateLine = `<span class="tag gy">未实现</span><span class="mut" style="font-size:10.5px">（等表/机制建好${nBit}）</span>`;
  } else if (st === "ERROR") {
    const r = clamp(reasonText(item.reason) || "接口没给原因", 60);
    stateLine = `<span class="tag err">读取失败</span><span class="mut" style="font-size:10.5px">（${esc(r)}）${nBit}</span>`;
  } else {
    // MISSING：契约里的字段没读到 —— 必须显式报出来，不许静默吞成「待建」
    stateLine = `<span class="tag err">接口未返回</span><span class="mut" style="font-size:10.5px">（这个指标字段没读到，不显示 0 顶替）</span>`;
  }
  const foot = def.jump ? jumpBtn(def.jump) : (def.pending ? `<span class="tag gy">${esc(def.pending)}</span>` : "");
  return `<div class="kpi"><b id="${esc(def.id)}">${value}</b>
    <div class="lb">${esc(def.label)}${extra ? `<span class="spacer"></span>${extra}` : ""}</div>
    ${stateLine || foot ? `<div class="row wrap" style="gap:6px;align-items:center;margin-top:5px">${stateLine}${foot}</div>` : ""}</div>`;
}

/* ---------------- 分布：长度 / 门控因子 / 长度豁免 ---------------- */

/** 失败态：红边说明条（含接口给的原因）—— 与「样本不足 / 未实现」的浅色空态区分开 */
const failNote = (msg) => `<div class="noteblock" style="margin-top:8px;border-left-color:var(--error,#e05555)">${esc(msg)}</div>`;

/* 长度分布：普通桶统一中性色，仅「41 字起」的尾桶标红（图例=「40 字以上」） */
const NEUTRAL_BAR = "var(--tx3,#8e8e93)";
const HOT_BAR = "var(--error,#e05555)";
const isHotBucket = (label) => {
  const m = String(label).match(/\d+/);   // 桶标签里的第一个数 = 桶下界
  return !!m && Number(m[0]) >= 41;
};

/** 契约形态 {state,n,n_min,bins}；旧形态（顶层数组 / 文本）兼容；读不到一律显式回报 */
function lengthDistRead(data) {
  const raw = pickRaw(data, ["length_dist", "length_distribution", "len_dist", "lengths"]);
  if (typeof raw === "string") return raw.trim() ? { text: raw } : { fail: "单条长度分布：接口返回了空文本" };
  if (raw == null) return { fail: "单条长度分布：接口没有返回这个字段（不摆空柱、不拿 0 顶替）" };
  if (typeof raw !== "object") return { fail: "单条长度分布：接口返回的形状看不懂" };
  let bins = null;
  let state = "";
  let n = null;
  let nMin = null;
  let reason = "";
  if (Array.isArray(raw)) {
    bins = raw;   // 旧形态：顶层数组
  } else {
    state = String(raw.state || raw.status || "").trim().toUpperCase();
    n = numOf(raw.n);
    nMin = numOf(raw.n_min);
    reason = raw.reason != null ? String(raw.reason) : "";
    if (Array.isArray(raw.bins)) bins = raw.bins;
  }
  if (state === "INSUFFICIENT_DATA" || state === "INSUFFICIENT" || state === "NO_DATA") {
    return { empty: `单条长度分布样本不足（${n != null ? `n=${fmtNum(n)}` : "样本数未返回"}${nMin ? ` / 需要 ${fmtNum(nMin)}` : ""}）：先不摆空柱` };
  }
  if (state === "NOT_IMPLEMENTED" || state === "TODO" || state === "PENDING") {
    return { empty: "单条长度分布还没实现：等记账表建好后再摆柱" };
  }
  if (state === "ERROR") return { fail: `单条长度分布读取失败（${reasonText(reason) || "接口没给原因"}）` };
  if (!bins || !bins.length) return { fail: "单条长度分布：接口没有给出分桶数据" };
  const items = [];
  bins.forEach((x) => {
    if (Array.isArray(x)) {
      const v = numOf(x[1]);
      if (v != null) items.push({ label: String(x[0] == null ? "" : x[0]), value: v });
      return;
    }
    if (!x || typeof x !== "object") return;
    const label = x.label != null ? x.label : x.bucket != null ? x.bucket : x.range != null ? x.range : x.name != null ? x.name : "";
    let v = null;
    for (const k of ["n", "count", "value", "total"]) { if (k in x) { v = numOf(x[k]); if (v != null) break; } }
    if (v != null) items.push({ label: String(label), value: v });
  });
  if (!items.length) return { fail: "单条长度分布：分桶里没有可读的数值" };
  return { items };
}

function lengthDistBody(rd) {
  if (rd.text) return `<div class="noteblock" style="margin-top:8px">${esc(rd.text)}</div>`;
  if (rd.fail) return failNote(rd.fail);
  if (rd.empty) return emptyBlock(rd.empty, "◔");
  return `<div id="mtLenBars"></div>
    <div class="row wrap mut" style="gap:12px;margin-top:6px">
      <span><i style="display:inline-block;width:9px;height:9px;border-radius:2px;background:${NEUTRAL_BAR}"></i> 常规</span>
      <span><i style="display:inline-block;width:9px;height:9px;border-radius:2px;background:${HOT_BAR}"></i> 40 字以上（可打标豁免）</span>
      <span>柱高 = 该区间的条数 · 口径：拆段后的单条（不是整轮合并长度）</span>
    </div>`;
}

/** 门控因子：契约形态 {state,n,n_min,items:[{factor,n,distinct,constant,top}]}；表体直接拼这些字段 */
function gateFactorBody(data) {
  const raw = pickRaw(data, ["gate_factors", "gate_factor_dist", "factor_dist", "factor_hist"]);
  if (typeof raw === "string") return raw.trim() ? `<div class="noteblock" style="margin-top:8px">${esc(raw)}</div>` : failNote("门控因子分布：接口返回了空文本");
  if (raw == null) return failNote("门控因子分布：接口没有返回这个字段（不摆空表）");
  if (typeof raw !== "object") return failNote("门控因子分布：接口返回的形状看不懂");
  let arr = null;
  let state = "";
  let n = null;
  let nMin = null;
  let reason = "";
  if (Array.isArray(raw)) {
    arr = raw;   // 旧形态：顶层数组
  } else {
    state = String(raw.state || raw.status || "").trim().toUpperCase();
    n = numOf(raw.n);
    nMin = numOf(raw.n_min);
    reason = raw.reason != null ? String(raw.reason) : "";
    if (Array.isArray(raw.items)) arr = raw.items;
  }
  if (state === "INSUFFICIENT_DATA" || state === "INSUFFICIENT" || state === "NO_DATA") {
    return emptyBlock(`门控因子分布样本不足（${n != null ? `n=${fmtNum(n)}` : "样本数未返回"}${nMin ? ` / 需要 ${fmtNum(nMin)}` : ""}）：样本够了再出表`, "◔");
  }
  if (state === "NOT_IMPLEMENTED" || state === "TODO" || state === "PENDING") {
    return emptyBlock("门控因子分布还没实现：等决策流水带上因子快照后，这里出「哪些因子退化成恒值」", "◔");
  }
  if (state === "ERROR") return failNote(`门控因子分布读取失败（${reasonText(reason) || "接口没给原因"}）`);
  if (!arr || !arr.length) return failNote("门控因子分布：接口没有给出因子条目");
  const rows = arr.map((x) => {
    if (!x || typeof x !== "object") return "";
    const name = x.factor != null ? x.factor : x.name != null ? x.name : x.label != null ? x.label : x.key != null ? x.key : "";
    if (name === "") return "";   // 连名字都没有的行不出（不许摆空单元格占位）
    let dist = "";
    if (Array.isArray(x.top)) {
      dist = x.top.map((t) => `${t && t.value != null ? String(t.value) : "—"}×${t && t.n != null ? fmtNum(t.n) : "?"}`).join(" / ");
    } else {
      dist = x.dist != null ? x.dist : x.values != null ? x.values : x.desc != null ? x.desc : x.detail != null ? x.detail : "";
    }
    const bits = [];
    if (x.n != null) bits.push(`n=${fmtNum(x.n)}`);
    if (x.constant) bits.push("常量（退化）");
    else if (x.distinct != null) bits.push(`取值 ${fmtNum(x.distinct)} 种`);
    if (x.state != null) bits.push(String(x.state));
    return `<tr><td>${esc(name)}</td><td class="mono">${esc(dist)}</td><td class="mut">${esc(bits.join(" · "))}</td></tr>`;
  }).filter(Boolean);
  if (!rows.length) return failNote("门控因子分布：条目里没有可读的字段");
  return `<div style="overflow-x:auto"><table><thead><tr><th>量</th><th>取值分布</th><th>来源 / 状态</th></tr></thead>
    <tbody>${rows.join("")}</tbody></table></div>`;
}

/** 长度豁免打标：接口给了数就出数；没给 = 打标机制还没上线（如实说明，不拿 0 顶、不报成故障） */
function lengthExemptBody(data) {
  const lex = pickRaw(data, ["length_exempt", "length_exemption", "length_exemptions", "exempt"]);
  const desc = `超过 <b>40 字</b>的回复会被标记，你可以给它「豁免」标签（比如讲正经事时本来就该长）。`;
  if (lex && typeof lex === "object") {
    const over = numOf(lex.over_n != null ? lex.over_n : lex.marked != null ? lex.marked : lex.n_marked);
    const ex = numOf(lex.exempted != null ? lex.exempted : lex.exempt_n != null ? lex.exempt_n : lex.n_exempt);
    const cells = [];
    if (over != null) cells.push(["超过 40 字的回复", fmtNum(over)]);
    if (ex != null) cells.push(["已豁免", fmtNum(ex)]);
    if (cells.length) {
      return `<div class="cs">${desc}</div>
        <div class="row wrap" style="gap:9px;margin-top:9px">${cells
          .map(([l, v]) => `<div class="kpi-mini" style="min-width:132px"><div class="v">${esc(v)}</div><div class="l">${esc(l)}</div></div>`)
          .join("")}</div>`;
    }
  }
  return `${emptyBlock("长度豁免打标还没上线：等长回复开始带标记记账后，这里才有数字", "◔")}
    <div class="mut" style="font-size:11px;margin-top:2px;line-height:1.8">${desc}</div>`;
}

/* ---------------- 页面 ---------------- */

export async function renderMetrics() {
  let data = null;
  let errMsg = "";
  try {
    data = await api("metrics");
  } catch (err) {
    errMsg = (err && err.message) || String(err);
    // 接口没就绪时页面按「读取失败」渲染，控制台也留一笔
    console.warn("[starheart] /page/metrics 读取失败，按读取失败态渲染", errMsg);
  }

  const read = CARDS.map((def) => ({
    def,
    item: errMsg ? { state: "ERROR", reason: errMsg } : readMetric(pickRaw(data, def.keys)),
  }));
  const nOk = read.filter((x) => x.item.state === "ok").length;
  const nInsuff = read.filter((x) => x.item.state === "INSUFFICIENT_DATA").length;
  const nTodo = read.filter((x) => x.item.state === "NOT_IMPLEMENTED").length;
  const nFail = 6 - nOk - nInsuff - nTodo;   // ERROR + MISSING：读不到的都要被看见

  const parts = [];
  if (nOk) parts.push(`出数 <b>${fmtNum(nOk)}</b> 项`);
  if (nInsuff) parts.push(`样本不足 ${fmtNum(nInsuff)} 项`);
  if (nTodo) parts.push(`待建 ${fmtNum(nTodo)} 项`);
  if (nFail) parts.push(`读取失败 <b>${fmtNum(nFail)}</b> 项`);
  let conclusion;
  if (errMsg) {
    conclusion = `接口 <span class="mono">/page/metrics</span> 读取失败（${esc(errMsg)}）：六张卡按读取失败显示，不预置任何数字`;
  } else if (!parts.length) {
    conclusion = "接口已接通，但一个指标字段都没读到：六张卡按「接口未返回」显示，不预置数字";
  } else if (nFail === 6 && !nOk && !nInsuff && !nTodo) {
    conclusion = "接口已接通，但没有返回任何指标字段：六张卡按「接口未返回」显示（不预置数字、也不拿 0 顶替）";
  } else {
    conclusion = `已接通 <span class="mono">/page/metrics</span>：${parts.join(" · ")}`;
  }

  const rd = lengthDistRead(data);
  const distItems = rd.items
    ? rd.items.map((it) => {
        const hot = isHotBucket(it.label);
        return { label: it.label, value: it.value, sub: hot ? "40 字以上（可打标豁免）" : "", color: hot ? HOT_BAR : NEUTRAL_BAR };
      })
    : null;

  const win = data ? numOf(data.window_days) : null;
  const nminBits = read
    .map(({ def, item }) => `${def.label} N_min=${fmtNum(item.nMin != null ? item.nMin : def.nMin)}`)
    .join(" · ");

  const html = `
  ${pageHead(
    "拟人指标",
    "行为质量只在这一页看聚合：六张 KPI 卡 + 单条长度分布 + 门控因子分布。指标在这儿看，明细去对应页看 —— 本页不重复列表",
    `<button class="btn s" onclick="hfRefresh(this)">刷新</button>`,
    "◔",
    conclusion
  )}
  ${kpis(read.map((x) => metricCard(x.def, x.item)).join(""))}
  ${card(lengthDistBody(rd), { title: "单条长度分布", sub: "拆段后的单条口径；柱高 = 该区间的条数", cls: "sec" })}
  <div>
    ${card(gateFactorBody(data), { title: "门控因子分布（校准看得见）", sub: "门控页已有因子矩阵与扣分榜，这里补的是「校准」：哪些因子退化成恒值一眼看出", cls: "sec" })}
    ${card(lengthExemptBody(data), { title: "长度豁免打标", sub: "超过 40 字的回复可打「豁免」标签", cls: "sec" })}
  </div>
  <div class="noteblock" style="margin-top:12px">口径：数据全部来自 <span class="mono">/page/metrics</span>，本页不做任何本地估算，也不拿 0 顶替缺的数。
· 范围：本页是<b>全局</b>统计（不随上方「查看视角」切换）—— 指标按全库窗口计算，未按人格过滤。
${win != null ? `· 窗口：最近 <b>${fmtNum(win)}</b> 天（接口给出）。\n` : ""}· 样本目标 N_min（逐项）：${nminBits} —— 不够就不出数，卡面显示「样本不足」并标出当前 n。
· 状态：<b>出数</b> / <b>样本不足</b>（样本不够）/ <b>未实现</b>（等表或机制建好）/ <b>读取失败</b>（接口报错的原因会写在卡面上）。
· 指标只放聚合，明细跳对应页：沉默率与连发 → 门控；长度与表达命中 → 语言学习；回复间隔与表情率的目标参数页还没上线。</div>`;

  return {
    html,
    mount: (host) => {
      if (distItems && distItems.length) {
        mount(host, "#mtLenBars", (el) => C.bars(el, { items: distItems, valueFmt: (v) => fmtNum(v) + " 条" }));
      }
    },
  };
}

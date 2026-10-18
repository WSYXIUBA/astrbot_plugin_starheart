/**
 * StarHeart 面板 · 数据层与 UI 基础件
 *
 * 约定（沿用插件既有铁律）：
 * 1. 页面里不放任何假数据 —— 拿不到就显示明确的空状态/错误，绝不编造数字。
 * 2. 所有后端调用都走 bridge（受限 iframe 里没有 cookie，直连必然 401）。
 * 3. 端点规则：后端注册在 /{plugin}/page/<path>，宿主只拼 /{plugin}/，
 *    所以前端必须自己带上 page/ 前缀（与 livingmemory 面板一致）。
 */

const bridge = window.AstrBotPluginPage;

/** 初始上下文（bridge.ready() 的返回值） */
export let CTX = null;

/** 全局状态：跨页签共享的只读缓存 + 当前查看视角 */
export const STATE = {
  page: "",
  sub: "",
  personaView: "",
  personaViewTouched: false,
  overview: null,
  config: {},
  domains: {},
  stats: null,
  memories: [],
  graph: null,
  graphStats: null,
  favours: [],
  habits: [],
  persons: [],
  personas: null,
  contacts: null,
  rules: null,
  hidden: {},
  tokens: null,
  theme: "light",
  ready: false,
  /** 参数中心域深链锚点（#settings/params/<domain>）：app.js 的 parseHash 投递，settings 页消费 */
  paramsAnchor: "",
};

export const $ = (sel) => document.querySelector(sel);
export const $$ = (sel) => Array.from(document.querySelectorAll(sel));

/** 后端路由前缀：宿主不补 page/，前端自己带 */
export const EP = (path) => {
  const clean = String(path).replace(/^\/+/, "");
  return clean.startsWith("page/") ? clean : "page/" + clean;
};

/* ---------------- 请求 ---------------- */

function withTimeout(promise, ms, label) {
  return Promise.race([
    promise,
    new Promise((_, rej) => setTimeout(() => rej(new Error((label || "请求") + "超时（" + ms / 1000 + "s）")), ms)),
  ]);
}

/**
 * 回包体检：宿主的桥在 HTTP 4xx/5xx 时**也会 resolve**（不是 reject），
 * 直接把错误页文本当数据返回。没有这层判断，写操作失败会被界面当成成功 ——
 * 真机上就出现过「HTTP 501 却弹出『已生成提案 #undefined』」。
 * strict=true（POST/写操作）额外把 {"ok":false,"error":...} 视为失败。
 */
function badResponse(res, label, strict) {
  if (res == null) return label + "：没有返回数据";
  if (typeof res === "string") {
    const t = res.replace(/<[^>]*>/g, " ").replace(/\s+/g, " ").trim();
    return label + "：返回异常（" + (t.slice(0, 120) || "空内容") + "）";
  }
  if (typeof res !== "object") return label + "：返回类型异常";
  if (res.status === "error") return res.message || res.error || label + "：失败";
  if (res.code != null && Number(res.code) >= 400) return res.message || label + "：失败（" + res.code + "）";
  if (strict && (res.ok === false || res.success === false)) return res.error || res.message || label + "：失败";
  return "";
}

export async function api(path, params) {
  const p = { ...(params || {}) };
  // 显式传 persona_id:"" 表示「要全量」，别被查看视角覆盖（数据治理的计数需要全库数）
  if (STATE.personaView && p.persona_id === undefined) p.persona_id = STATE.personaView;
  const res = await withTimeout(bridge.apiGet(EP(path), p), 30000, path);
  const bad = badResponse(res, path, false);
  if (bad) throw new Error(bad);
  return res;
}

export async function apiPost(path, body) {
  const res = await withTimeout(bridge.apiPost(EP(path), body || {}), 120000, path);
  const bad = badResponse(res, path, true);
  if (bad) throw new Error(bad);
  return res;
}

/** 不抛错的 GET：单个数据源失败不该让整页白屏 */
export async function apiSafe(path, params, fallback = null) {
  try {
    return await api(path, params);
  } catch (err) {
    // ⚠️ 静默回落会把「接口失败」渲染成 0/空 —— 那是假数据（零假数据红线）。
    // 这里仍然回落（单源失败不该白屏），但**必须留痕并打标记**：调用方用 res.__err 判定，
    // 关键页面（如消息事件总览）直接用 api() 走错误卡，不许把失败显示成 0。
    const msg = (err && err.message) || String(err);
    console.warn("[starheart] GET 失败（已回落，调用方应按 __err 判定）", path, msg);
    if (fallback && typeof fallback === "object" && !Array.isArray(fallback)) {
      return { ...fallback, __err: msg, __path: path };
    }
    return fallback;
  }
}

export async function apiPostSafe(path, body) {
  try {
    return { ok: true, data: await apiPost(path, body) };
  } catch (err) {
    return { ok: false, error: (err && err.message) || String(err) };
  }
}

/** 保存成功但原生配置写回失败时的提示文案；没有失败返回空串。 */
export function nativeWriteError(res) {
  const nw = res && res.ok && res.data && res.data._native_write;
  if (nw && nw.ok === false) return String(nw.error || "未知原因");
  return "";
}

/**
 * 从平台联系人行里取一个「能一眼认出」的名字。
 * 好友的字段是 nickname/remark，群是 name，还可能只有 id —— 只认 name 会满屏号码。
 */
export function contactLabel(x) {
  if (x == null) return "";
  if (typeof x === "string") return x;
  const id = String(x.id || x.group_id || x.user_id || "");
  const label = String(x.name || x.nickname || x.remark || "").trim();
  if (!label) return id;
  return label === id ? id : label;
}

/** 会话串 → 人类可读：「群 100000001（测试）」/「张三 (100000000)」 */
export function sessionLabel(sessionId, kind, name, ident) {
  const k = kind || (String(sessionId || "").toLowerCase().includes("group") ? "group" : "private");
  const id = String(ident || (sessionId || "").split(":").pop() || "");
  const nm = String(name || "").trim();
  const tag = k === "group" ? "群" : "私聊";
  return nm && nm !== id ? `${tag} ${nm}（${id}）` : `${tag} ${id}`;
}

/* ---------------- 格式化 ---------------- */

export const esc = (s) =>
  String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

export const fmtNum = (n) => (Number(n) || 0).toLocaleString("en-US");

export const fmtPct = (v, digits = 0) => `${((Number(v) || 0) * 100).toFixed(digits)}%`;

export const fmt1 = (n) => (Math.round((Number(n) || 0) * 10) / 10).toFixed(1);

export function fmtTime(ts, withSec = false) {
  if (!ts) return "—";
  const d = new Date(Number(ts) * 1000);
  const p = (n) => String(n).padStart(2, "0");
  const day = `${p(d.getMonth() + 1)}-${p(d.getDate())}`;
  const time = `${p(d.getHours())}:${p(d.getMinutes())}${withSec ? ":" + p(d.getSeconds()) : ""}`;
  return `${day} ${time}`;
}

/** 相对时间：面板上「3 分钟前」比时间戳更好读 */
export function timeAgo(ts) {
  const t = Number(ts) || 0;
  if (!t) return "从未";
  if (t > Date.now() / 1000 + 60) return "时间戳异常";
  const s = Math.max(0, Date.now() / 1000 - t);
  if (s < 60) return "刚刚";
  if (s < 3600) return `${Math.floor(s / 60)} 分钟前`;
  if (s < 86400) return `${Math.floor(s / 3600)} 小时前`;
  if (s < 86400 * 30) return `${Math.floor(s / 86400)} 天前`;
  return fmtTime(t);
}

export const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));

export function normList(value) {
  if (Array.isArray(value)) return value.map((x) => String(x).trim()).filter(Boolean);
  return String(value || "")
    .split(/[,，\s]+/)
    .map((x) => x.trim())
    .filter(Boolean);
}

/* ---------------- 参数中心：「最近改过」跟踪（收尾批） ----------------
   面板跑在 sandbox iframe 里（没有 localStorage/sessionStorage），只能记在这次会话的内存里。
   只在**服务端确认保存成功**后登记 —— 输入框里改了还没保存的不算「改过」，那会是假信息。
   「改过」= 用户手改过（input 事件）且这次保存真的写进去的键；保存会把所有行原样回写，
   所以**不能**把整批 patch 都算成改过（那样每个域一保存、全表都置顶，等于没置顶）。 */
const CONFIG_TOUCHED = new Map(); // key -> 时间戳（秒）
const CONFIG_DIRTY = new Set();   // 用户手改过、还没保存成功的键

export function noteConfigTouched(keys) {
  const now = Date.now() / 1000;
  (Array.isArray(keys) ? keys : [keys]).forEach((k) => {
    if (k) CONFIG_TOUCHED.set(String(k), now);
  });
}

/** 用户手改了某个键（由 #pagehost 上的委托 input 监听调用；程序性赋值不算） */
export function noteConfigDirty(key) {
  if (key) CONFIG_DIRTY.add(String(key));
}

/** 保存成功后：把这批键里**真的被手改过**的取出来登记为「最近改过」，并清掉它们的脏标记 */
export function takeConfigDirty(keys) {
  const out = [];
  (Array.isArray(keys) ? keys : [keys]).forEach((k) => {
    if (CONFIG_DIRTY.has(k)) {
      out.push(k);
      CONFIG_DIRTY.delete(k);
    }
  });
  return out;
}

export function configTouchedAt(key) {
  return CONFIG_TOUCHED.get(String(key)) || 0;
}

/** 「最近改过置顶」：本次会话改过的键按改动时间倒序排到最前，其余保持原来的顺序 */
export function recentFirstKeys(keys) {
  const arr = Array.from(keys || []);
  const touched = arr
    .filter((k) => CONFIG_TOUCHED.has(k))
    .sort((a, b) => CONFIG_TOUCHED.get(b) - CONFIG_TOUCHED.get(a));
  return touched.length ? touched.concat(arr.filter((k) => !CONFIG_TOUCHED.has(k))) : arr;
}

/* ---------------- 主题 ---------------- */

export function currentTheme() {
  return document.documentElement.getAttribute("data-theme") === "dark" ? "dark" : "light";
}

export function applyTheme(mode) {
  STATE.theme = mode === "dark" ? "dark" : "light";
  document.documentElement.setAttribute("data-theme", STATE.theme);
  const btn = $("#themeBtn");
  if (btn) btn.textContent = STATE.theme === "dark" ? "☀" : "🌙";
}

/** 面板主题：宿主说了算；用户手动切换只影响本次会话（sandbox 里没有 localStorage） */
export function toggleTheme() {
  const next = currentTheme() === "dark" ? "light" : "dark";
  applyTheme(next);
  toast(`已切到${next === "dark" ? "暗色" : "亮色"}（仅本次会话，宿主主题不会被改）`);
}

/* ---------------- 提示 / 抽屉 / 弹窗 ---------------- */

export function toast(msg, kind = "") {
  const host = $("#toasts");
  if (!host) return;
  const el = document.createElement("div");
  el.className = "toast" + (kind ? " " + kind : "");
  el.textContent = String(msg);
  host.appendChild(el);
  setTimeout(() => {
    el.classList.add("out");
    setTimeout(() => el.remove(), 260);
  }, kind === "err" ? 5200 : 3000);
}

export function openDrawer(title, html) {
  const d = $("#drawer");
  const mask = $("#drawerMask");
  if (!d || !mask) return;
  d.innerHTML = `<div class="dh"><b>${esc(title)}</b><button class="iconbtn" id="drawerClose" title="关闭">✕</button></div>
    <div class="db">${html}</div>`;
  d.classList.add("show");
  mask.classList.add("show");
  const btn = $("#drawerClose");
  if (btn) btn.onclick = closeDrawer;
  // 点遮罩 / 按 Esc 也要能关：抽屉盖在右半边，只留一个小叉的话用户得先找到它
  mask.onclick = closeDrawer;
  if (!openDrawer._esc) {
    openDrawer._esc = true;
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") closeDrawer();
    });
  }
}

export function closeDrawer() {
  const d = $("#drawer");
  const mask = $("#drawerMask");
  if (d) d.classList.remove("show");
  if (mask) mask.classList.remove("show");
}

export function confirmModal(title, text, onOk, okText = "确认") {
  const mask = $("#modalMask");
  const box = $("#modal");
  if (!mask || !box) return;
  box.innerHTML = `<h4>${esc(title)}</h4><p>${esc(text)}</p>
    <div class="acts"><button class="btn" id="modalCancel">取消</button><button class="btn dg" id="modalOk">${esc(okText)}</button></div>`;
  mask.classList.add("show");
  const hide = () => { mask.classList.remove("show"); box.onkeydown = null; };
  box.onkeydown = null;
  $("#modalCancel").onclick = hide;
  let busy = false;
  $("#modalOk").onclick = async () => {
    if (busy) return;   // 连点/回车双触发守卫（删除、批量重写这类操作不能跑两遍）
    busy = true;
    hide();
    try {
      await onOk();
    } catch (err) {
      toast("操作失败：" + ((err && err.message) || err), "err");
    }
  };
}

/**
 * 表单弹窗：和 confirmModal 共用同一套弹窗外壳，但带输入字段。
 * 面板跑在 sandbox iframe 里（无 allow-modals），原生 prompt()/confirm() 会被静默吞掉——
 * 所有需要用户输入的交互都必须走这里，别再碰 window.prompt。
 * fields: [{key, label, value?, ph?, area?}]；onSubmit(values) 返回 false 则保持弹窗打开。
 */
export function formModal(title, fields, onSubmit, okText = "保存") {
  const mask = $("#modalMask");
  const box = $("#modal");
  if (!mask || !box) return;
  box.innerHTML = `<h4>${esc(title)}</h4>
    ${fields
      .map((f) => `<label class="fml"><span>${esc(f.label)}</span>${f.area
        ? `<textarea class="ipt" id="fm_${esc(f.key)}" rows="3" placeholder="${esc(f.ph || "")}">${esc(f.value || "")}</textarea>`
        : `<input class="ipt" id="fm_${esc(f.key)}" placeholder="${esc(f.ph || "")}" value="${esc(f.value || "")}">`}</label>`)
      .join("")}
    <div class="acts"><button class="btn" id="modalCancel">取消</button><button class="btn p" id="modalOk">${esc(okText)}</button></div>`;
  mask.classList.add("show");
  const hide = () => { mask.classList.remove("show"); box.onkeydown = null; };
  const read = () => {
    const out = {};
    fields.forEach((f) => {
      const el = $("#fm_" + f.key);
      out[f.key] = el ? el.value : "";
    });
    return out;
  };
  $("#modalCancel").onclick = hide;
  box.onkeydown = (e) => {
    if (e.key === "Escape") hide();
    else if (e.key === "Enter" && e.target && e.target.tagName === "INPUT") {
      e.preventDefault();
      const ok = $("#modalOk");
      if (ok) ok.click();
    }
  };
  let busy = false;
  $("#modalOk").onclick = async () => {
    if (busy) return;   // 连点/回车双触发守卫
    busy = true;
    try {
      if ((await onSubmit(read())) !== false) hide();
      else busy = false;
    } catch (err) {
      busy = false;
      toast("操作失败：" + ((err && err.message) || err), "err");
    }
  };
  const first = box.querySelector("input,textarea");
  if (first) setTimeout(() => { try { first.focus(); } catch (e) {} }, 40);
}

/** 顶栏状态灯：把后端可用性直白地写在脸上，而不是默默空白 */
export function setLive(text, kind = "") {
  const pill = $("#livePill");
  const t = $("#liveText");
  if (t) t.textContent = text;
  if (pill) pill.className = "pill" + (kind ? " " + kind : "");
}

/* ---------------- 小 UI 构件 ---------------- */

/** 分段控件：带滑动指示器（CSS 变量驱动，点击后自己挪） */
export function segmentHtml(items, active, action) {
  return `<div class="seg" data-seg="${esc(action)}">${items
    .map((it) => `<button class="sg${it.value === active ? " on" : ""}" data-v="${esc(it.value)}" title="${esc(it.title || it.label)}">${esc(it.label)}</button>`)
    .join("")}<i class="ind"></i></div>`;
}

/** 让指示器滑到当前选中项（渲染后调用；容器尺寸变化也要调） */
export function syncSeg(root = document) {
  const scopes = root === document ? $$(".seg") : Array.from(root.querySelectorAll ? root.querySelectorAll(".seg") : []);
  scopes.forEach((seg) => {
    const on = seg.querySelector(".sg.on");
    const ind = seg.querySelector(".ind");
    if (!on || !ind) return;
    ind.style.left = on.offsetLeft + "px";
    ind.style.width = on.offsetWidth + "px";
  });
}

export function emptyBlock(text, ico = "◌") {
  return `<div class="empty"><div class="eico">${ico}</div><div>${esc(text)}</div></div>`;
}

export function skelBlock(lines = 3) {
  return `<div class="col" style="gap:8px">${Array.from({ length: lines })
    .map(() => `<div class="sk" style="height:14px;width:${60 + Math.round(38 * Math.random())}%"></div>`)
    .join("")}</div>`;
}

export function kvRows(pairs, cls = "dkv") {
  return pairs.filter(Boolean).map(([k, v]) => `<div class="${cls}"><div class="k">${esc(k)}</div><div class="v">${v}</div></div>`).join("");
}

export function table(head, rows, emptyText = "暂无数据") {
  if (!rows.length) return emptyBlock(emptyText);
  return `<div style="overflow-x:auto"><table><thead><tr>${head.map((h) => `<th>${h}</th>`).join("")}</tr></thead>
    <tbody>${rows.join("")}</tbody></table></div>`;
}

/** 数值滚动动画：让 KPI 数字「长」出来，动效存在感来自这里而不是花哨的装饰 */
export function countUp(el, target, ms = 620) {
  if (!el) return;
  const to = Number(target) || 0;
  const from = Number(el.textContent.replace(/[^\d.-]/g, "")) || 0;
  if (Math.abs(to - from) < 0.001) {
    el.textContent = String(target);
    return;
  }
  const t0 = performance.now();
  const step = (t) => {
    const k = clamp((t - t0) / ms, 0, 1);
    const eased = 1 - Math.pow(1 - k, 3);
    const val = from + (to - from) * eased;
    el.textContent = Number.isInteger(to) ? Math.round(val).toLocaleString("en-US") : String(Math.round(val * 100) / 100);
    if (k < 1) requestAnimationFrame(step);
    else el.textContent = String(target);
  };
  requestAnimationFrame(step);
}

export function cssVar(name, fallback) {
  const v = getComputedStyle(document.documentElement).getPropertyValue(name);
  return (v && v.trim()) || fallback;
}

export function copyText(text) {
  const ta = document.createElement("textarea");
  ta.value = String(text);
  ta.style.cssText = "position:fixed;opacity:0";
  document.body.appendChild(ta);
  ta.select();
  let ok = false;
  try {
    ok = document.execCommand("copy");
  } catch (err) {
    ok = false;
  }
  ta.remove();
  toast(ok ? "已复制" : "复制失败（沙箱限制）", ok ? "ok" : "err");
}

/* ---------------- 启动桥接 ---------------- */

export async function initBridge() {
  if (!bridge) {
    setLive("bridge 未注入", "err");
    $("#pagehost").innerHTML = `<div class="card cp"><h3>面板没能连上 AstrBot</h3>
      <div class="cs">页面里读不到 <span class="mono">window.AstrBotPluginPage</span>：通常是页面被单独打开、或 bridge SDK 没注入。
      请从 WebUI「插件 → StarHeart → StarHeart面板」进入。</div></div>`;
    return false;
  }
  try {
    CTX = await bridge.ready();
  } catch (err) {
    CTX = bridge.getContext ? bridge.getContext() : null;
    console.warn("[starheart] bridge.ready 失败", err);
  }
  applyTheme(CTX && CTX.isDark ? "dark" : currentTheme());
  if (bridge.onContext) {
    bridge.onContext((ctx) => {
      applyTheme(ctx && ctx.isDark ? "dark" : "light");
    });
  }
  STATE.ready = true;
  return true;
}

/* ---------------- 页面骨架构件（各视图文件共用） ---------------- */

/** 页头：标题 + 说明 + 右侧动作区（+ 可选「结论句」第二行：这一页现在是什么状态） */
export function pageHead(title, desc, actions = "", icon = "", conclusion = "") {
  return `<div class="ph"><div><h1>${icon ? `<span class="hi">${icon}</span>` : ""}${esc(title)}</h1>
    ${desc ? `<div class="d">${desc}</div>` : ""}
    ${conclusion ? `<div class="concl">${conclusion}</div>` : ""}</div>
    <div class="acts">${actions}</div></div>`;
}

/** 页内子页签（同一个一级页里横排切换） */
export function tabBar(page, subs, active) {
  return `<div class="seg" data-seg="sub" style="margin-bottom:13px">${subs
    .map(
      (s) =>
        `<button class="sg${s.id === active ? " on" : ""}" data-sub="${esc(page)}/${esc(s.id)}" title="${esc(s.hint || s.label)}">${esc(s.label)}</button>`
    )
    .join("")}<i class="ind"></i></div>`;
}

/** 通用卡片 */
export function card(body, { title = "", sub = "", actions = "", cls = "", pad = true } = {}) {
  const head =
    title || actions
      ? `<div class="row bt" style="margin-bottom:${sub ? 2 : 8}px"><h3>${esc(title)}</h3><div class="row" style="gap:6px">${actions}</div></div>`
      : "";
  return `<div class="card ${cls} ${pad ? "cp" : ""}">
    ${head}${sub ? `<div class="cs">${sub}</div>` : ""}${body}</div>`;
}

/** KPI 卡：主数字 + 副标 + 迷你图形槽（图形由调用方 charts.* 后再填） */
export function kpiCard(id, value, label, extra = "", slotId = "") {
  return `<div class="kpi"><b id="${id}">${value}</b>
    <div class="lb">${label}${extra ? `<span class="spacer"></span>${extra}` : ""}</div>
    ${slotId ? `<div class="barslot" id="${slotId}"></div>` : ""}</div>`;
}

export function kpis(html) {
  return `<div class="kpis">${html}</div>`;
}

export function secTitle(text) {
  return `<div class="sec-t">${esc(text)}</div>`;
}

/** 把 charts 模块的返回值塞进容器；charts 内部已处理空数据，这里只兜底 DOM 不存在 */
export function mount(host, sel, fn) {
  const el = typeof sel === "string" ? host.querySelector(sel) : sel;
  if (!el) return null;
  try {
    return fn(el);
  } catch (err) {
    console.warn("[starheart] 图表渲染失败", sel, err);
    el.innerHTML = emptyBlock("图表渲染失败：" + ((err && err.message) || err));
    return null;
  }
}

/**
 * 内联事件安全取值：所有 onclick="fn(${jsStr(x)})" 的参数都要走它。
 * 先 JSON.stringify 保证是合法 JS 字符串字面量，再做 HTML 属性转义 —— 少了这步，
 * 属性里的引号会把 handler 截断（浏览器报 "Unexpected end of input"）。
 */
export const jsStr = (s) =>
  JSON.stringify(String(s == null ? "" : s))
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");

/* ---------------- 页面跳转与刷新钩子（避免视图模块与路由模块循环依赖） ---------------- */

/** 跳页：只改 hash，路由在 app.js 里统一响应（哈希路由也方便用户直接分享链接） */
export function goPage(page, sub) {
  const next = `#${page}${sub ? "/" + sub : ""}`;
  if (location.hash === next) return;
  location.hash = next;
}

/* ---------------- 参数中心域深链（收尾批） ----------------
   #settings/params/<domain>：跳到「设置 · 模块与参数」的对应域分区（带锚点滚动 + 高亮）。
   已在参数中心时直接滚动，不整页重渲染 —— 不会把用户输入到一半的值冲掉。 */

export function hfGoParams(domain) {
  const d = String(domain || "").replace(/[^a-z_]/g, "");
  if (!d) return;
  if (STATE.page === "settings" && STATE.sub === "main") {
    scrollParamsDomain(d);
    try {
      const target = "#settings/params/" + d;
      if (location.hash !== target) history.replaceState(null, "", target);
    } catch (err) { /* 沙箱里可能不允许改 URL；锚点已经滚到位，不影响使用 */ }
    return;
  }
  location.hash = "#settings/params/" + d;
}

/** 滚到某个参数域分区并短暂高亮描边（找不到就如实说，不假装跳了） */
export function scrollParamsDomain(domain) {
  const d = String(domain || "").replace(/[^a-z_]/g, "");
  const el = document.querySelector(`[data-pdom="${d}"]`);
  if (!el) {
    toast("没找到「" + d + "」参数区（页面可能还在加载）");
    return false;
  }
  // 顶栏是 sticky（约 48px 高）——给锚点留出这一段的让位，别把分区卡头压在顶栏底下
  el.style.scrollMarginTop = "58px";
  try { el.scrollIntoView({ block: "start", behavior: "smooth" }); } catch (err) { el.scrollIntoView(); }
  el.style.outline = "2px solid var(--primary,#3c96ca)";
  setTimeout(() => { el.style.outline = ""; }, 1600);
  return true;
}

/** 消费 app.js 路由投递过来的域锚点；同一次投放只滚一次（软刷新不会反复跳）。
 *  返回是否真的滚了 —— 路由的「还原滚动位置」逻辑据此让位，不把这次跳转顶掉。 */
export function consumeParamsAnchor() {
  const dom = STATE.paramsAnchor;
  if (!dom) return false;
  STATE.paramsAnchor = "";
  return scrollParamsDomain(dom);
}

let _refreshHook = null;

export function setRefreshHook(fn) {
  _refreshHook = fn;
}

/** 重新拉取当前页（数据变更后用，避免整页重载丢滚动位置） */
export function refreshCurrent() {
  if (typeof _refreshHook === "function") _refreshHook();
}

export { bridge };

/**
 * StarHeart 面板 · 入口（路由 + 壳层交互 + 全局动作绑定）
 *
 * 路由用 hash（#page/sub）：AstrBot 的 Pages 是受限 iframe，hash 路由刷新页面也能回到原处，
 * 而且不会触发宿主的路由。
 * 所有页内容都由视图模块渲染成 HTML 字符串再挂载，图表在 mount() 里画 —— 这样切页永远是
 * 「先读到数据再画」，不会出现图表画在空容器上的空白页。
 */

import * as charts from "./charts.js?v=1.0.2";
import { MemoryGraph } from "./graph.js?v=1.0.2";
import { Graph3D } from "./graph3d.js?v=1.0.2";
import {
  $, $$, STATE, initBridge, setLive, setRefreshHook, goPage, toggleTheme, toast, openDrawer, closeDrawer, esc,
   syncSeg, jsStr, emptyBlock, refreshCurrent, hfGoParams, consumeParamsAnchor, noteConfigDirty,
} from "./store.js?v=1.0.2";
import * as mem from "./views-memory.js?v=1.0.2";
import * as evt from "./views-events.js?v=1.0.2";
import * as sys from "./views-system.js?v=1.0.2";
import * as mt from "./views-metrics.js?v=1.0.2";
import { hfSaveDomain } from "./views-conf.js?v=1.0.2";

/* ---------------- 页面表 ---------------- */
const PAGES = {
  overview: { title: "总览", icon: "◉", nav: "overview", render: () => sys.renderOverview() },
  memory: {
    title: "记忆系统",
    icon: "◍",
    nav: "memory",
    subs: [
      { id: "graph", label: "记忆图谱", hint: "关系链可视化：可拖动单点、缩放后卡片化" },
      { id: "list", label: "记忆库", hint: "记忆管理表格：搜索/筛选/排序/分页 + 召回测试；点一行原地展开详细" },
      { id: "person", label: "画像", hint: "L0 人物画像 + L1 群聊画像（同一个人在不同人格下是两份，互不可见）" },
      { id: "conf", label: "记忆设置", hint: "记忆注入 / 抓取 / 衰减 / 整理" },
    ],
    render: (sub) =>
      sub === "list" ? mem.renderMemoryList() : sub === "person" ? mem.renderPersons() : sub === "conf" ? mem.renderMemoryConf() : mem.renderGraph(),
  },
  expression: {
    title: "语言学习",
    icon: "✎",
    nav: "expression",
    // 一页到底：主体是"学到的东西"，日志/参数/语料收进默认折叠区（不再拆子页）
    // 只有一页（没有真正的二级页）→ 不摆子页签按钮，免得出现"点不动的假子菜单"
    subs: [],
    render: (sub) => (sub === "log" ? sys.renderExpressionLog() : sub === "conf" ? sys.renderExpressionConf() : sys.renderExpressionLib()),
  },
  messages: {
    title: "消息事件",
    icon: "✉",
    nav: "messages",
    // 3 个可直接点的平级子页签（一键切换）。原来是 5 个页签、每个域再套 2~3 个内部页签 = 三层级联，
    // 属用户点名的"子菜单里藏子菜单"：现在同一实体只用「平级页签 + 页内分段」两种手段。
    // 5 个平级子页签，**页内不再有任何第二层导航**（用户不要"子菜单里藏子菜单"，也不要页顶 chips 栏）；
    // 「名单 / 洞察 / 参数」都收进对应域页**页底默认折叠的卡**里，点开才见。
    subs: [
      { id: "hub", label: "事件总览", hint: "现象：KPI / 节律热力 / 开口转化 / 为什么没发" },
      { id: "gate", label: "门控", hint: "该不该开口：决策流水（行内展开因子）+ 页底折叠参数" },
      { id: "proactive", label: "主动", hint: "主动投递：倒计时/额度 + 审计 + 页底折叠名单与参数" },
      { id: "recall", label: "撤回", hint: "谁撤回了什么（含图片）+ 页底折叠参数" },
      { id: "favour", label: "好感", hint: "关系结算列表 + 页底折叠洞察与参数" },
    ],
    render: (sub) => evt.renderMsgDomain(sub || "hub"),
  },
  metrics: {
    title: "拟人指标",
    icon: "◔",
    nav: "metrics",
    // 一页到底的聚合页（六张 KPI 卡 + 两组分布），没有子页 → subs 留空，页头下不摆子页签
    subs: [],
    render: () => mt.renderMetrics(),
  },
  persona: {
    title: "人格管理",
    icon: "☺",
    nav: "persona",
    subs: [
      { id: "list", label: "人格列表" },
      { id: "state", label: "状态与进化" },
    ],
    render: (sub) => (sub === "state" ? sys.renderPersonaState() : sys.renderPersonaList()),
  },
  settings: {
    title: "设置",
    icon: "⚙",
    nav: "settings",
    subs: [
      { id: "main", label: "模块与参数" },
      { id: "contacts", label: "名单与可见性" },
      { id: "models", label: "模型分配" },
      { id: "tokens", label: "Token 消耗" },
    ],
    render: (sub) =>
      sub === "contacts" ? sys.renderContacts() : sub === "models" ? sys.renderModels() : sub === "tokens" ? sys.renderTokens() : sys.renderSettingsMain(),
  },
};

const DEFAULT_SUB = { memory: "graph", expression: "lib", messages: "hub", persona: "list", settings: "main" };

/* ---------------- 路由 ---------------- */
/** 上一次解析到的原始 hash：用来判断「这次是导航还是同 hash 的软刷新」
 *  （#settings/params/<域> 的锚点只在真正导航时投放，软刷新不反复滚动） */
let lastRawHash = "";

function parseHash() {
  const raw = String(location.hash || "").replace(/^#/, "").replace(/^\/+/, "");
  const [page, sub, extra] = raw.split("/");
  const p = PAGES[page] ? page : "overview";
  let s = sub || DEFAULT_SUB[p] || "";
  // 老书签兼容：旧的子页 id 映射到新的域 + 域内页
  if (p === "messages" && evt.MSG_LEGACY[s]) {
    const [dom, tab] = evt.MSG_LEGACY[s];
    evt.msgTabSet(dom, tab);
    s = dom;
    // 把地址栏也改成新 hash：否则「同域换子页」时路由会因 key 未变而提前 return，
    // 页面停在上一页（实测踩过：点投递审计仍显示候选池）
    // 用 replaceState 而不是改 location.hash：后者会再触发一次 hashchange → 页面渲染两遍
    // （实测会把后面的断言拖到超时）。改不动也无所谓，路由键里带了域内页，一样会重渲染。
    try {
      if (location.hash !== `#${p}/${s}`) history.replaceState(null, "", `#${p}/${s}`);
    } catch (err) { /* 沙箱里可能不允许改 URL，忽略 */ }
  }
  // 设置 · 参数中心域深链：#settings/params/<domain>（收尾批）
  // 真导航（hash 变了）才投放锚点 →「设置 · 模块与参数」的对应域分区会被滚动 + 高亮；
  // 同 hash 的软刷新（保存 / 切置顶 / 点刷新）不会反复跳位置。
  if (p === "settings" && s === "params") {
    if (raw !== lastRawHash) STATE.paramsAnchor = String(extra || "").trim();
    s = "main";
  }
  lastRawHash = raw;
  return { page: p, sub: PAGES[p] && PAGES[p].subs ? s : "" };
}

/** 把子页签插进页头之后：页头是渲染器给的第一块，深度扫一遍找它的收尾 div，稳且不挑结构 */
function insertAfterPageHead(html, snippet) {
  const start = html.indexOf('<div class="ph"');
  if (start < 0) return snippet + html;
  let i = start;
  let depth = 0;
  while (i < html.length) {
    const open = html.indexOf("<div", i);
    const close = html.indexOf("</div>", i);
    if (close < 0) break;
    if (open >= 0 && open < close) {
      depth += 1;
      i = open + 4;
    } else {
      depth -= 1;
      i = close + 6;
      if (depth === 0) return html.slice(0, i) + snippet + html.slice(i);
    }
  }
  return snippet + html;
}

let currentKey = "";
let rendering = false;
/** 渲染期间又发生的导航（渲染结束后重放一次，避免导航被静默吞掉） */
let pendingKey = null;

// 视图层需要「重新查一次当前页」时调它。之前各处调用的 refreshCurrent() 并不存在，
// 导致记忆库 / 语言学习的交互点了没反应（写入路径直接 ReferenceError）。
window.hfRerender = () => route(true);

async function route(force = false) {
  // 离开记忆图谱页 → 恢复中心轴（用户反馈：从记忆详细点过去会锁定，退出后不恢复）
  try {
    if (!String(location.hash || "").startsWith("#memory/graph")) mem.clearGraphFocus && mem.clearGraphFocus();
  } catch (err) { /* 恢复中心轴失败不该阻断路由 */ }
  const { page, sub } = parseHash();
  // 域内页也算进 key：否则「同一个域、换域内页」会被当成没变而提前 return
  const viewKey = page === "messages" && typeof evt.msgViewKey === "function" ? "#" + evt.msgViewKey() : "";
  const key = `${page}/${sub}${viewKey}`;
  if (!force && key === currentKey) {
    // 同页的 hash 跳转（如 #settings/main → #settings/params/gate）：不重渲染，
    // 但锚点要消费 —— 直接滚到对应参数域分区
    if (page === "settings") {
      try { consumeParamsAnchor(); } catch (err) { /* 锚点消费失败不阻断路由 */ }
    }
    return;
  }
  // 同页强制重查（按钮 / 开关 / 筛选 / 保存后的 hfRerender）＝软刷新：不铺骨架、不播进场动画，
  // 旧内容原地等新内容，到了再换 —— 不能再让「点一下 ＝ 整页刷新」（用户反馈）。
  // 跨页切换 ＝ 硬路由：数据先读，200ms 还没到齐才铺骨架（快时不闪，慢仍有明确加载态）。
  const soft = force && key === currentKey;
  // 正在渲染时不能「先推进 currentKey 再 return」：
  // 原实现是先把 currentKey 写成新键、把骨架屏铺到页面上，之后才 `if (rendering) return`，
  // 于是「渲染中点另一个菜单」= 界面卡在骨架屏/旧页；更糟的是 currentKey 已经变了，
  // 再点同一个菜单会被上面的 key===currentKey 提前 return —— 只能点别的菜单才恢复。
  // 现在把这次导航记成 pending，等当前渲染结束自动重放一次。
  if (rendering) {
    pendingKey = key;
    return;
  }
  currentKey = key;
  STATE.page = page;
  STATE.sub = sub;
  const def = PAGES[page];
  const crumb = $("#crumb");
  const crumbSub = $("#crumbSub");
  if (crumb) crumb.textContent = def.title;
  const subDef = (def.subs || []).find((x) => x.id === sub);
  if (crumbSub) crumbSub.textContent = subDef ? subDef.label : "";
  $$("#railNav .nav").forEach((b) => b.classList.toggle("on", b.dataset.nav === def.nav));

  const host = $("#pagehost");
  if (!host) return;
  // —— 刷新保命符：记住「展开了哪些折叠块 + 滚到哪」——
  // 面板里很多按钮（切黑白名单 / 显示被拦下的 / 保存…）都是 refreshCurrent() 整页重渲染，
  // 不保的话每点一次都要重新展开、重新滚回来（用户反馈原话：会导致重新刷新，要重新展开）。
  const scroller = document.querySelector(".main") || document.scrollingElement;
  const prevTop = scroller ? scroller.scrollTop : 0;
  const foldKey = (d) => {
    const s = d.querySelector("summary");
    return s ? (s.textContent || "").replace(/\s+/g, " ").trim().slice(0, 48) : "";
  };
  const openFolds = Array.from(host.querySelectorAll("details")).filter((d) => d.open).map(foldKey).filter(Boolean);
  rendering = true;
  let settled = false;
  let skTimer = 0;
  let dimTimer = 0;
  if (soft) {
    // 软刷新：旧内容原地保留；只有确实慢（>150ms）才亮一个很轻的忙碌降透明，快时完全不闪
    dimTimer = setTimeout(() => { if (!settled) host.classList.add("soft-busy"); }, 150);
  } else {
    // 硬路由：200ms 内渲染完成就完全不铺骨架（LAN 上多数页面到不了这个阈值）
    skTimer = setTimeout(() => {
      if (!settled) host.innerHTML = `<div class="col" style="gap:12px"><div class="sk" style="height:18px;width:220px"></div>
    <div class="sk" style="height:96px"></div><div class="sk" style="height:220px"></div></div>`;
    }, 200);
  }
  try {
    const out = (await def.render(sub)) || { html: "" };
    let html = out.html || "";
    if (def.subs && def.subs.length) {
      const tabs = `<div class="seg" data-seg="sub" style="margin:2px 0 13px">${def.subs
        .map(
          (s) =>
            `<button class="sg${s.id === sub ? " on" : ""}" data-sub="${esc(page)}/${esc(s.id)}" title="${esc(s.hint || s.label)}">${esc(s.label)}</button>`
        )
        .join("")}<i class="ind"></i></div>`;
      html = insertAfterPageHead(html, tabs);
    }
    settled = true;
    clearTimeout(skTimer);
    clearTimeout(dimTimer);
    host.classList.remove("soft-busy");
    host.innerHTML = soft ? `<div>${html}</div>` : `<div class="enter">${html}</div>`;
    syncSeg(host);
    // 打印进度条 / 分段指示器依赖布局完成，下一帧再对齐一次
    requestAnimationFrame(() => syncSeg(host));
    if (typeof out.mount === "function") {
      try {
        await out.mount(host);
      } catch (err) {
        console.error("[starheart] mount 失败", page, sub, err);
        host.insertAdjacentHTML(
          "beforeend",
          `<div class="card cp" style="border-color:var(--error)"><h3>这个子页渲染出错</h3>
            <div class="cs">${esc((err && err.message) || String(err))} · 其它页面不受影响，可以切走</div></div>`
        );
      }
    }
    bindSubTabs(host);
    // 参数中心域深链（#settings/params/<domain>）：渲染完成后滚到对应域分区 + 高亮；
    // 真滚了就让下面的「还原滚动位置」让位，别把它顶回去
    let anchored = false;
    if (page === "settings" && sub === "main") {
      try { anchored = consumeParamsAnchor(); } catch (err) { /* 锚点消费失败不阻断渲染 */ }
    }
    // 还原：① 折叠块展开状态（按 summary 文本匹配，对不上就保持默认收起）② 滚动位置（等一帧/两拍，防止被后落地内容顶走）
    if (openFolds.length) {
      host.querySelectorAll("details").forEach((d) => {
        const k = foldKey(d);
        if (k && openFolds.includes(k)) d.open = true;
      });
    }
    if (prevTop > 0 && !anchored) {
      const sc = document.querySelector(".main") || document.scrollingElement;
      const put = () => { if (sc) sc.scrollTop = prevTop; };
      put();
      requestAnimationFrame(put);
      setTimeout(put, 80);
    }
    setLive("数据已更新", "");
  } catch (err) {
    settled = true;
    clearTimeout(skTimer);
    clearTimeout(dimTimer);
    host.classList.remove("soft-busy");
    console.error("[starheart] 渲染失败", page, sub, err);
    host.innerHTML = `<div class="card cp"><h3>页面加载失败</h3>
      <div class="cs">${esc((err && err.message) || String(err))}</div>
      <div class="row" style="gap:6px;margin-top:10px"><button class="btn s" onclick="hfRefresh()">重试</button></div></div>`;
    setLive("后端读取失败", "err");
  } finally {
    rendering = false;
    // 渲染期间又切过页 → 重放最后一次导航（用 setTimeout 让当前调用栈先退出，避免递归）
    const pending = pendingKey;
    pendingKey = null;
    // 渲染期间又来的导航 / 刷新：重放一次；同页刷新也要重放（连点两下时第二次不能被吞）
    if (pending != null) setTimeout(() => route(pending === key), 0);
  }
}

function bindSubTabs(host) {
  host.querySelectorAll("[data-sub]").forEach((b) =>
    b.addEventListener("click", () => {
      goPage(b.getAttribute("data-sub").split("/")[0], b.getAttribute("data-sub").split("/")[1]);
      setTimeout(() => syncSeg(host), 0);
    })
  );
}

/* ---------------- 壳层 ---------------- */
function bindShell() {
  const nav = $("#railNav");
  if (nav) {
    nav.addEventListener("click", (e) => {
      const b = e.target.closest(".nav");
      if (!b) return;
      goPage(b.dataset.nav, DEFAULT_SUB[b.dataset.nav] || "");
    });
  }
  const theme = $("#themeBtn");
  if (theme) theme.onclick = toggleTheme;
  const refresh = $("#refreshBtn");
  if (refresh) refresh.onclick = () => hfRefresh(refresh);
  const help = $("#helpBtn");
  if (help) help.onclick = openHelp;
  const pswitch = $("#pswitchLabel");
  if (pswitch) {
    // 打开下拉时刷新计数（人格可能刚被改过）
    const det = $("#personaSwitch");
    if (det) det.addEventListener("toggle", () => det.open && sys.refreshPersonaSwitch());
  }
  window.addEventListener("hashchange", () => route());
  window.addEventListener("resize", () => {
    try {
      syncSeg($("#pagehost"));
    } catch (err) {
      /* 尺寸变化时指示器对齐失败无所谓，下次渲染会修正 */
    }
  });
}

function hfRefresh(btn) {
  if (btn) btn.classList.add("loading");
  const { page, sub } = parseHash();
  // 图谱页：刷新只重取数据，不销毁画布（用户拖过的钉住位置不该被刷新按钮抹掉）
  if (page === "memory" && sub === "graph" && typeof mem.hfGraphReload === "function" && mem.hfGraphReload()) {
    setTimeout(() => btn && btn.classList.remove("loading"), 400);
    return;
  }
  route(true).finally(() => setTimeout(() => btn && btn.classList.remove("loading"), 300));
}

function openHelp() {
  openDrawer(
    "这个面板怎么看",
    `<div style="font-size:12.5px;line-height:1.9">
      <b>总览</b>：模块拓扑 + 开口转化（分级条形）+ 记忆构成条；数字全部来自插件自己的库。<br>
      <b>记忆图谱</b>：力导向关系链。拖动单个点只动它自己，双击解除钉住；滚轮缩放、拖空白平移；
      放大到 1.15× 后记忆变成卡片，鼠标悬停卡片会展开。<br>
      <b>记忆库</b>：管理表格。关键词（含记忆 ID）/ 会话 / 人格 / 状态 / 类型筛选，可排序分页；点任意一行原地展开详细（原始对话 + 来源轮次 + ID + 向量），右上角还有<b>召回测试</b>。<br>
      <b>门控</b>：看「最新一条：分数是怎么凑出来的」，就知道为什么它没回你。<br>
      <b>好感</b>：daily 会冷却、lifetime 只增不减；分数不进 prompt，只有区间会变成语气。<br>
      <b>主动</b>：静默时长、每日上限、名单三重护栏，先开影子模式观察它想说什么；「主动设置」页里可以直接给某个人试发一条。<br>
      <b>设置</b>：模型分配、名单勾选、导入与数据治理（破坏性操作集中卡）。<br><br>
      <span class="mut">顶栏的人格下拉只切换「记忆查看视角」，不会改 bot 实际人格；改实际人格请到「人格管理」。</span>
    </div>`
  );
}

/* ---------------- 全局动作（内联 onclick 用） ---------------- */
function bindActions() {
  Object.assign(window, {
    // 导航 / 壳层（抽屉里的「取消」按钮是内联 onclick，必须挂到 window 上）
    goPage,
    hfGoParams,          // 参数中心域深链：去「设置 · 模块与参数」的对应域（带锚点）
    closeDrawer,
    hfRefresh,
    hfCopyText: (t) => {
      const ta = document.createElement("textarea");
      ta.value = String(t);
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
    },
    // 记忆 / 图谱
    hfGraphBuild: mem.hfGraphBuild,
    hfPersonFocus: mem.hfPersonFocus,
    hfGraphFit: mem.hfGraphFit,
    hfGraphResetView: mem.hfGraphResetView,
    hfGraphCards: mem.hfGraphCards,
    hfGraphResetPhysics: mem.hfGraphResetPhysics,
    hfGraphShuffle: mem.hfGraphShuffle,
    hfGraphFull: mem.hfGraphFull,
    hfGraphExport: mem.hfGraphExport,
    hfGraphSearch: mem.hfGraphSearch,
    hfGraphMotion: mem.hfGraphMotion,
    hfGraphSelect: mem.hfGraphSelect,
    hfGraphFocus: mem.hfGraphFocus,
    clearGraphFocus: mem.clearGraphFocus,   // 路由切页时用它还原轴心，也给自检用
    hfGraphOpen: mem.hfGraphOpen,
    hfGraphReload: mem.hfGraphReload,
    hfMemOpen: mem.hfMemOpen,
    hfMemEdit: mem.hfMemEdit,
    hfVecSwitch: mem.hfVecSwitch,
    hfVecRefresh: mem.hfVecRefresh,
    hfMemSet: mem.hfMemSet,
    hfMemPage: mem.hfMemPage,
    hfMemOpenRow: mem.hfMemOpenRow,
    hfRecallToggle: mem.hfRecallToggle,
    hfRecallRun: mem.hfRecallRun,
    hfEmbedBackfill: mem.hfEmbedBackfill,
    hfRewriteSummaries: mem.hfRewriteSummaries,
    hfImportExtras: mem.hfImportExtras,
    hfMemReactivate: mem.hfMemReactivate,
    hfMemExport: mem.hfMemExport,
    hfCardDet: mem.hfCardDet,
    hfProfileBuild: mem.hfProfileBuild,
    hfToggleMore: mem.hfToggleMore,
    hfMemDetail: mem.hfMemDetail,
    hfMemBoost: mem.hfMemBoost,
    hfMemDemote: mem.hfMemDemote,
    hfPersonsRefresh: mem.hfPersonsRefresh,
    hfMemDelete: mem.hfMemDelete,
    hfMemRestore: mem.hfMemRestore,
    hfMemFilter: mem.hfMemFilter,
    hfMemMaintain: mem.hfMemMaintain,
    hfPurgeStale: mem.hfPurgeStale,
    // 门控 / 撤回 / 主动 / 好感
    hfGateReload: (btn) => {
      const { page } = parseHash();
      // 消息事件现在有 5 个平级域页；无论在哪一页都直接强制重渲染当前页
      // （旧判据写死 sub==="gate_live"，重构后永远是 false → 点了只闪 loading 什么都不刷）
      if (page === "messages") route(true);
      else {
        if (btn) btn.classList.add("loading");
        goPage("messages", "gate");
        if (btn) setTimeout(() => btn.classList.remove("loading"), 500);
      }
    },
    hfMhSet: evt.hfMhSet,
    hfMhView: evt.hfMhView,
    hfMhOpen: evt.hfMhOpen,
    hfMhExport: evt.hfMhExport,
    hfGateFilter: (v) => {
      evt.gateState && (evt.gateState.result = v);
      route(true);
    },
    hfGateSince: evt.hfGateSince,     // 门控时间筛选（纯前端过滤本页已加载记录）
    hfFoldJump: evt.hfFoldJump,       // 页底折叠区跳转（原 8 个同 hash 死按钮的替代）
    hfGatePage: (delta) => {
      if (!evt.gateState) return;
      evt.gateState.offset = Math.max(0, evt.gateState.offset + delta * evt.gateState.limit);
      route(true);
    },
    hfRecallReload: () => route(true),
    hfRecallZoom: evt.hfRecallZoom,
    hfProactiveScan: evt.hfProactiveScan,
    hfProMode: evt.hfProMode,
    hfProToggle: evt.hfProToggle,
    hfProAll: evt.hfProAll,
    hfProSave: evt.hfProSave,
    hfProFilter: evt.hfProFilter,
    hfProMore: evt.hfProMore,
    hfProTest: evt.hfProTest,
    hfProTestFill: evt.hfProTestFill,
    hfProactivePreview: evt.hfProactivePreview,
    hfProactiveSend: evt.hfProactiveSend,
    hfL2LoadDefault: evt.hfL2LoadDefault,
    hfL2Reset: evt.hfL2Reset,
    hfL2Save: evt.hfL2Save,
    hfFavourAdjustOpen: evt.hfFavourAdjustOpen,
    hfFavourAdjustSubmit: evt.hfFavourAdjustSubmit,
    // 表达 / 人格 / 设置
    hfHabitToggle: sys.hfHabitToggle,
    hfHabitDelete: sys.hfHabitDelete,
    hfHabitFilter: sys.hfHabitFilter,
    hfExtractHabits: sys.hfExtractHabits,
    hfHabitAdd: sys.hfHabitAdd,
    hfHabitSaveEdit: sys.hfHabitSaveEdit,
    hfHabitEdit: sys.hfHabitEdit,
    hfFold: sys.hfFold,
    hfHbFilter: sys.hfHbFilter,
    hfLogToggle: sys.hfLogToggle,
    hfLogMode: sys.hfLogMode,
    hfChartToggle: sys.hfChartToggle,
    hfHabitCues: sys.hfHabitCues,
    hfPersonaActive: sys.hfPersonaActive,
    hfPersonaEdit: sys.hfPersonaEdit,
    hfPersonaSave: sys.hfPersonaSave,
    hfPersonaPropose: sys.hfPersonaPropose,
    hfEvolveSave: sys.hfEvolveSave,
    hfEvolveParams: sys.hfEvolveParams,
    hfHistToggle: sys.hfHistToggle,
    hfPersonaDecide: sys.hfPersonaDecide,
    hfPersonaPickView: sys.hfPersonaPickView,
    hfIsolatedToggle: sys.hfIsolatedToggle,
    renderPersonaRefresh: sys.renderPersonaRefresh,
    hfSaveConfig: sys.hfSaveConfig,
    hfSaveDomain,
    hfParamsSearch: sys.hfParamsSearch,             // 参数中心搜索（纯前端过滤行，不重渲染）
    hfParamsRecentToggle: sys.hfParamsRecentToggle, // 参数中心「最近改过置顶」开关
    hfSaveModels: sys.hfSaveModels,
    hfTestModel: sys.hfTestModel,
    hfTestAllModels: sys.hfTestAllModels,
    hfPersonaRollback: sys.hfPersonaRollback,
    hfPromptSave: sys.hfPromptSave,
    hfPromptReset: sys.hfPromptReset,
    hfImportDetect: sys.hfImportDetect,
    hfImportStart: sys.hfImportStart,
    hfPurge: sys.hfPurge,
    hfContactFilter: sys.hfContactFilter,
    hfConnMode: sys.hfConnMode,
    hfConnAll: sys.hfConnAll,
    hfConnToggle: sys.hfConnToggle,
    hfConnSave: sys.hfConnSave,
    hfConnSeedAll: sys.hfConnSeedAll,
    hfContactsRefresh: sys.hfContactsRefresh,
    hfRefreshOverview: sys.hfRefreshOverview,
    // 调试口（自动化断言用，不影响界面）
    __hf: {
      graphEngine: () => mem.graphEngine(),
      scope: () => mem.graphScope(),   // 图谱引擎（自检居中/轴心还原用）
      charts,
      MemoryGraph,
      Graph3D,
      state: () => ({ ...STATE, page: STATE.page, sub: STATE.sub }),
      graph: () => (mem.__graph ? mem.__graph() : null),
      engine: () => (mem.__graphEngine ? mem.__graphEngine() : null),
      engine3d: () => (mem.__graphEngine3d ? mem.__graphEngine3d() : null),
      bank3d: () => (mem.__bankEngine3d ? mem.__bankEngine3d() : null),
      person3d: () => (mem.__personEngine3d ? mem.__personEngine3d() : null),
      selfTest: () => ({ charts: charts.selfTest ? charts.selfTest() : "no-selftest" }),
    },
  });
}

/* ---------------- 启动 ---------------- */
async function boot() {
  // 画布自适应兜底：窗口尺寸/浏览器缩放变化后重新量图谱画布
  // （参考 upstream/astrbot_plugin_livingmemory 的做法：容器 ResizeObserver + 全局 resize 双保险）
  if (!window.__hfResizeHooked) {
    window.__hfResizeHooked = true;
    let t = 0;
    const rerun = () => {
      clearTimeout(t);
      t = setTimeout(() => {
        try { window.__hfGraphEngine?.resize?.(); } catch (err) { /* 引擎未就绪 */ }
        try { if (typeof window.__hfGraphResize === "function") window.__hfGraphResize(); } catch (err) { /* 忽略 */ }
      }, 110);
    };
    window.addEventListener("resize", rerun);
    window.addEventListener("orientationchange", rerun);
    if (window.visualViewport && window.visualViewport.addEventListener) window.visualViewport.addEventListener("resize", rerun);
  }
  applyInitialTheme();
  bindShell();
  bindActions();
  // 参数中心「最近改过」：只有**用户手改过**的键才登记（委托监听挂在常驻的 #pagehost 上，
  // 重渲染产生的新控件自动覆盖；程序性赋值不触发 input 事件，不会被误记）
  const ph = document.getElementById("pagehost");
  if (ph && !ph.__hfDirtyHooked) {
    ph.__hfDirtyHooked = true;
    ph.addEventListener(
      "input",
      (e) => {
        const t = e.target;
        const el = t && typeof t.closest === "function" ? t.closest("[data-hf-cfg]") : null;
        if (el) noteConfigDirty(el.getAttribute("data-hf-cfg"));
      },
      true
    );
  }
  const ok = await initBridge();
  if (!ok) return;
  setRefreshHook(() => route(true));
  await sys.refreshPersonaSwitch().catch(() => {});
  await route(true);
  setLive("面板就绪", "");
  // 主题变化（宿主切换 / 系统跟随）时让图表重绘
  if (typeof MutationObserver !== "undefined") {
    new MutationObserver(() => {
      try {
        syncSeg($("#pagehost"));
      } catch (err) {
        /* 指示器对齐失败无碍 */
      }
    }).observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
  }
}

/** bridge 还没就绪时先按宿主注入的 data-theme 显示，避免闪一下白 */
function applyInitialTheme() {
  const t = document.documentElement.getAttribute("data-theme");
  if (t !== "dark" && t !== "light") document.documentElement.setAttribute("data-theme", "light");
  STATE.theme = document.documentElement.getAttribute("data-theme");
  const btn = $("#themeBtn");
  if (btn) btn.textContent = STATE.theme === "dark" ? "☀" : "🌙";
}

if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
else boot();

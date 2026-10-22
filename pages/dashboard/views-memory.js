/**
 * StarHeart 面板 · 记忆系统视图（图谱 / 记忆库 / 画像[L0 人物·L1 群聊] / 记忆设置）
 *
 * 图谱页是这次重写的重点：旧的「星座视图」把成组节点用虚线凸包圈起来、节点不能单独拖、
 * 关系链几乎为空。现在改成力导向 + 单点拖拽 + 卡片化的信息呈现，并且后端补齐了
 * memory↔memory 的关系链（实体/时序/语义）。
 */

import * as C from "./charts.js?v=1.0.4";
import { GRAPH_PALETTE } from "./graph.js?v=1.0.4";
import {
  createGraph3D, webglAvailable, LAYOUT_DEFS, LAYOUT_HOTKEYS, CAM_DEFS, PALETTE_DEFS, TIER_DEFS,
  PARA_DEFS, LEVEL_DEFS, ATOM_LABEL as ATOM_LABEL_3D,
} from "./graph3d.js?v=1.0.4";
import {
  $, api, apiPost, apiPostSafe, apiSafe, esc, fmtNum, fmtPct, fmt1, fmtTime, timeAgo, toast, openDrawer, closeDrawer,
  confirmModal, formModal, pageHead, tabBar, card, kpiCard, kpis, secTitle, mount, emptyBlock, skelBlock, jsStr, STATE,
  segmentHtml, syncSeg, countUp, goPage, refreshCurrent,
} from "./store.js?v=1.0.4";

export const ATOM_LABEL = {
  factual: "事实", episodic: "经历", relational: "关系", preference: "偏好",
  planned: "待办", routine: "惯例", opinion: "看法",
};
export const GROUP_LABEL = { memory: "记忆", person: "人物", topic: "话题", session: "会话" };
const ATOM_COLOR = GRAPH_PALETTE.memory;
const LINK_LABEL = { semantic: "语义同义", temporal: "时序相邻", entity: "实体共指",
  // 这三种是 store.graph() 真实会产出的边类型，原来没有中文名 → 图例直接显示生 key
  about: "关于某人", in_session: "发生在该会话", member_of: "成员关系（人在这个群）" };
const atomColor = (k) => ATOM_COLOR[k] || "#8e8e93";

const GRAPH_VIEWS = [
  { id: "chain", label: "✦ 关系链", hint: "力导向：谁和谁有关系，线越亮关系越强" },
  { id: "cluster", label: "◎ 类型聚类", hint: "同类型记忆自然成团，仍可单独拖拽任意节点" },
  { id: "timeline", label: "◷ 时间脉络", hint: "按发生时间从左到右铺开，弧线=关系" },
  { id: "entity", label: "✧ 实体星系", hint: "人物/话题居中枢，关联记忆在卫星轨道" },
];

/* 模块级单例：切页不销毁，钉子与布局都能保留（用户拖过的位置不该白拖） */
let G = null;
let G_HOST = null;
// 3D 引擎单例（同样是「切页回来保留钉住的位置」）
let G3 = null;
let G3_MOUNT = null;
let G3_READY = null;
/** 3D 外壳元素（#gPanel / #gHealth 这些浮层挂在它下面，刷新时要用它去找节点） */
let G3_SHELL = null;
// 图谱范围：view=跟随顶栏「查看视角」（默认）；all=全库不过滤
let G_SCOPE = "view";
let B_SCOPE = "view";
// 画布真正挂在哪（#pagehost 换页时只重建子节点，靠它才能判断画布是否还在文档里）
let G_MOUNT = null;
let G_VIEW = "chain";

/** 调试口：自动化断言里想拿引擎状态时用（不影响界面） */
export function __graph() {
  const eng = G3 || G;
  if (!eng) return null;
  try {
    return typeof eng.getState === "function" ? eng.getState() : null;
  } catch (err) {
    return null;
  }
}

/** 调试口：需要直接读引擎内部（节点坐标/钉住状态）时用 —— 只读，不改渲染 */
export function __graphEngine() {
  // 原实现只返回 G，而 G 从未被赋值（恒 null）→ 这个调试口一直拿不到任何东西。
  return G3 || G;
}

/** 刷新按钮在图谱页的专用路径：只换数据，不销毁画布。
 *
 * 原实现只认 2D 引擎 G，而 G 从来没有被赋值过（唯一的 `new MemoryGraph` 在 graph.js
 * 自己的 selfTest 里，运行期根本不可达），G_HOST 同样恒 null → 这里**永远返回 false**，
 * 于是 app.js 只能走「整页重建」，用户拖过的钉住位置被抹掉 —— 正好与这个函数的承诺相反。
 * 现在作用于真正在跑的引擎：3D 引擎有 setData()，支持原地换数据并保留布局/钉住状态。
 */
export function hfGraphReload() {
  const eng = G3 || G;
  const shell = G3_SHELL || G_HOST || G3_MOUNT;
  if (!eng || typeof eng.setData !== "function" || !shell) return false;
  try { window.__hfLastRender = "hfGraphReload(limit700)"; } catch (err) { /* 非浏览器 */ }
  Promise.all([api("graph", { limit: 700 }), apiSafe("graph/stats", {}, {})])
    .then(([payload, stats]) => {
      STATE.graph = payload;
      STATE.graphStats = stats;
      eng.setData({ nodes: payload.nodes || [], links: payload.links || [] });
      renderGraphHealth(shell, payload, stats);
      toast("图谱数据已刷新");
    })
    .catch((err) => toast("刷新失败：" + ((err && err.message) || err), "err"));
  return true;
}



/* ==================================================================
   记忆图谱
   ================================================================== */
/** 着色维度（同一张图换角度看问题：类型 / 强度 / 时间 / 关系数） */
/* ==================================================================
   3D 图谱的 HUD / 控制台（视觉语言照参考实现：深蓝玻璃 + 青色高光）
   类名一律加 f 前缀（fbtn/fchip/fgrp…），避免和面板全局的 .chip/.btn 打架。
   ================================================================== */
const HF3D_CSS = `
/* 浮层配色：深色液态玻璃（高透 + 强模糊 + 镜面高光边）。
   底色用中性石墨（不带藏青/深蓝——用户明确否掉），透明度压到 .38~.55，
   让后面的星空透过来，才有"液态玻璃"而不是"奶白塑料板"的观感。 */
.hf3d-ui{position:relative}
.hf3d-ui .hud{position:absolute;z-index:6;font-size:12px;letter-spacing:.2px;color:rgba(235,235,245,.66)}
.hf3d-ui .hud b{color:#f5f5f7;font-weight:600}
.hf3d-ui .fdot{display:inline-block;width:6px;height:6px;border-radius:50%;background:#64d2ff;
  box-shadow:0 0 9px rgba(100,210,255,.9),0 0 3px #fff inset;margin-right:6px;vertical-align:1px;animation:hf3dbp 1.8s infinite}
@keyframes hf3dbp{0%,100%{opacity:1}50%{opacity:.35}}
/* 液态玻璃的公共写法：半透明石墨 + 背景模糊提饱和 + 上缘镜面高光 + 外投影 */
.hf3d-ui .fgstat{left:14px;bottom:12px;line-height:1.85;padding:6px 12px;border-radius:14px;
  background:linear-gradient(165deg,rgba(255,255,255,.05),rgba(255,255,255,.016) 42%,rgba(13,14,18,.46));
  border:1px solid rgba(0,0,0,.26);
  box-shadow:0 14px 34px rgba(0,0,0,.42);
  backdrop-filter:blur(20px) saturate(180%) brightness(1.05)}
.hf3d-ui .flegend{left:14px;bottom:74px;text-align:left;line-height:1.75;max-width:286px;padding:9px 12px;border-radius:14px;
  background:linear-gradient(165deg,rgba(255,255,255,.05),rgba(255,255,255,.016) 42%,rgba(13,14,18,.48));
  border:1px solid rgba(0,0,0,.26);
  box-shadow:0 14px 34px rgba(0,0,0,.42);
  backdrop-filter:blur(20px) saturate(180%) brightness(1.05)}
.hf3d-ui .flegend i{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:7px;vertical-align:-1px;box-shadow:0 0 9px currentColor;background:currentColor}
.hf3d-ui .fmetrics{left:50%;transform:translateX(-50%);top:10px;display:flex;gap:16px;flex-wrap:wrap;justify-content:center;
  padding:7px 16px;font-size:11px;color:rgba(235,235,245,.62);border-radius:15px;max-width:min(760px,64%);pointer-events:none;
  background:linear-gradient(165deg,rgba(255,255,255,.05),rgba(255,255,255,.016) 42%,rgba(13,14,18,.45));
  border:1px solid rgba(0,0,0,.26);
  box-shadow:0 14px 34px rgba(0,0,0,.4);
  backdrop-filter:blur(20px) saturate(180%) brightness(1.05)}
.hf3d-ui .fmetrics b{color:#f5f5f7;font-family:var(--mono,monospace);font-size:12.5px;margin-right:5px;font-weight:600}
.hf3d-ui .fmetrics .fm{white-space:nowrap}
.hf3d-ui .fmetrics .fm.hot b{color:#ff6b81}
.hf3d-ui .fmetricsB{top:auto;bottom:12px;font-size:10.5px;gap:14px}
.hf3d-ui .fmetricsB b{font-size:11.5px}
.hf3d-ui .fpanel{position:absolute;right:12px;top:12px;width:248px;z-index:8;font-size:12px;border-radius:18px;overflow:hidden;
  background:linear-gradient(160deg,rgba(255,255,255,.055),rgba(255,255,255,.018) 42%,rgba(12,13,17,.5));
  border:1px solid rgba(0,0,0,.30);
  box-shadow:0 20px 52px rgba(0,0,0,.5);
  backdrop-filter:blur(26px) saturate(185%) brightness(1.06)}
.hf3d-ui .fpanel::after{content:"";position:absolute;inset:0;pointer-events:none;border-radius:inherit;
  background:radial-gradient(130% 70% at 10% -8%,rgba(120,205,255,.07),rgba(255,255,255,0) 58%)}
.hf3d-ui .fpanel.folded .fbd{display:none}
.hf3d-ui .fpanel.folded{box-shadow:0 10px 26px rgba(0,0,0,.42)}
.hf3d-ui .fph{height:33px;line-height:33px;padding:0 13px;font-size:12.5px;letter-spacing:2px;color:#f5f5f7;font-weight:600;
  border-bottom:1px solid rgba(0,0,0,.24);position:relative;z-index:1}
.hf3d-ui .fph b{float:right;color:#64d2ff;font-weight:500;cursor:pointer;letter-spacing:0;text-shadow:0 0 10px rgba(100,210,255,.45)}
.hf3d-ui .fph b:hover{color:#9ee7ff}
.hf3d-ui .fbd{padding:9px 11px 11px;max-height:min(72vh,620px);overflow:auto;position:relative;z-index:1}
.hf3d-ui .fbd::-webkit-scrollbar{width:5px}
.hf3d-ui .fbd::-webkit-scrollbar-thumb{background:rgba(150,180,215,.24);border-radius:3px}
.hf3d-ui .fgrp{margin-bottom:9px}
.hf3d-ui .fgrp>label{display:block;color:rgba(235,235,245,.58);letter-spacing:.6px;margin-bottom:5px;font-size:10.5px;font-weight:600}
.hf3d-ui .fbtns{display:grid;grid-template-columns:repeat(4,1fr);gap:5px}
.hf3d-ui .fbtn,.hf3d-ui .fchip{height:21px;border:1px solid rgba(0,0,0,.26);background:rgba(255,255,255,.08);color:rgba(245,245,247,.88);
  font-size:10.5px;cursor:pointer;letter-spacing:0;transition:.16s;font-family:inherit;border-radius:9px;
  box-shadow:none}
.hf3d-ui .fchip{padding:0 9px}
.hf3d-ui .fbtn:hover,.hf3d-ui .fchip:hover{background:rgba(110,200,255,.16);color:#eaf7ff;border-color:rgba(110,200,255,.4)}
.hf3d-ui .fbtn.on,.hf3d-ui .fchip.on{background:linear-gradient(180deg,#4da3ff,#0a6fe0);color:#fff;border-color:rgba(255,255,255,.34);
  box-shadow:0 5px 18px rgba(10,111,224,.5),0 0 22px rgba(77,163,255,.3)}
.hf3d-ui .fchips{display:flex;flex-wrap:wrap;gap:5px}
.hf3d-ui .frow{display:flex;align-items:center;gap:7px;margin:8px 0 0;color:rgba(235,235,245,.66);font-size:11.5px}
.hf3d-ui .frow span{min-width:52px}
.hf3d-ui .frow input.frng{flex:1}
.hf3d-ui .frow var{font-style:normal;color:#f5f5f7;width:46px;text-align:right;font-family:var(--mono,monospace)}
.hf3d-ui input.frng{-webkit-appearance:none;appearance:none;width:100%;height:3px;background:rgba(255,255,255,.2);outline:none;margin:6px 0 0;border-radius:3px}
.hf3d-ui input.frng::-webkit-slider-thumb{-webkit-appearance:none;width:13px;height:13px;border-radius:50%;border:0;cursor:pointer;
  background:radial-gradient(circle at 35% 30%,#ffffff,#9ed8ff 55%,#4da3ff);
  box-shadow:0 0 12px rgba(100,210,255,.75),0 2px 6px rgba(0,0,0,.5)}
.hf3d-ui input.frng::-moz-range-thumb{width:13px;height:13px;border:0;border-radius:50%;background:#9ed8ff;cursor:pointer}
.hf3d-ui .fsearch{width:100%;height:27px;background:rgba(255,255,255,.09);border:1px solid rgba(0,0,0,.26);color:#f5f5f7;
  font-size:11.5px;padding:0 9px;outline:none;font-family:inherit;border-radius:10px;box-shadow:none}
.hf3d-ui .fsearch::placeholder{color:rgba(235,235,245,.42)}
.hf3d-ui .fsearch:focus{border-color:rgba(100,210,255,.6);box-shadow:0 0 0 3px rgba(100,210,255,.2)}
.hf3d-ui .fhint{color:rgba(235,235,245,.42);font-size:10.5px;line-height:1.7;margin-top:6px}
/* 记忆点卡片：同一套液态玻璃 */
.hf3d-ui .fcard{position:absolute;left:12px;top:74px;width:266px;z-index:7;display:none;border-radius:18px;padding:13px 15px;
  background:linear-gradient(160deg,rgba(255,255,255,.06),rgba(255,255,255,.02) 42%,rgba(12,13,17,.52));
  border:1px solid rgba(0,0,0,.30);
  box-shadow:0 22px 56px rgba(0,0,0,.52);
  backdrop-filter:blur(26px) saturate(185%) brightness(1.06)}
.hf3d-ui .fcard::after{content:"";position:absolute;inset:0;pointer-events:none;border-radius:inherit;
  background:radial-gradient(130% 70% at 8% -10%,rgba(120,205,255,.07),rgba(255,255,255,0) 56%)}
.hf3d-ui .fcard.on{display:block}
.hf3d-ui .fcard h3{position:relative;z-index:1;font-size:14px;color:#f5f5f7;letter-spacing:.2px;margin-bottom:6px;word-break:break-all;line-height:1.45;font-weight:600}
.hf3d-ui .fcard .kv{position:relative;z-index:1;font-size:11.5px;color:rgba(235,235,245,.6);line-height:1.95;display:flex;justify-content:space-between;gap:8px}
.hf3d-ui .fcard .kv span{color:#f5f5f7;max-width:152px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.hf3d-ui .fcard .bar{position:relative;z-index:1;height:3px;margin:10px 0;border-radius:3px;
  background:linear-gradient(90deg,#64d2ff,rgba(100,210,255,0));box-shadow:0 0 12px rgba(100,210,255,.4)}
.hf3d-ui .fcard .nb{position:relative;z-index:1;font-size:11px;color:rgba(235,235,245,.78);line-height:1.7;max-height:118px;overflow:auto}
.hf3d-ui .fcard .nb em{font-style:normal;color:#64d2ff}
.hf3d-ui .fcard .nb u{text-decoration:none;cursor:pointer;color:rgba(245,245,247,.9);border-radius:6px;padding:1px 3px}
.hf3d-ui .fcard .nb u:hover{color:#64d2ff;background:rgba(100,210,255,.14)}
.hf3d-ui .fcard .tag{border-radius:6px}
.hf3d-ui .fcard .btn.xs{border-radius:9px}
/* 展开态：卡片原地撑大，把画布空间用起来（详细内容就在卡里，不再从右侧拉抽屉） */
.hf3d-ui .fcard.expanded{width:min(560px,44%);max-height:78vh;overflow:auto;
  background:linear-gradient(160deg,rgba(255,255,255,.085),rgba(255,255,255,.03) 42%,rgba(12,13,17,.66));
  border-color:rgba(0,0,0,.34);box-shadow:0 26px 70px rgba(0,0,0,.6)}
.hf3d-ui .fcard.expanded::-webkit-scrollbar{width:6px}
.hf3d-ui .fcard.expanded::-webkit-scrollbar-thumb{background:rgba(150,180,215,.28);border-radius:3px}
.hf3d-ui .fdet{margin-top:10px}
.hf3d-ui .fdet-h{margin:11px 0 5px;font-size:10.5px;letter-spacing:1px;color:rgba(235,235,245,.5);font-weight:600}
.hf3d-ui .fdet-tx{font-size:12.5px;line-height:1.8;color:#f5f5f7;white-space:pre-wrap;word-break:break-word}
.hf3d-ui .fdet-q{font-size:11.5px;line-height:1.75;color:rgba(235,235,245,.78);padding:8px 10px;border-radius:9px;
  background:rgba(255,255,255,.05);border-left:2px solid rgba(100,210,255,.55)}
.hf3d-ui .btn.xs.p{background:linear-gradient(180deg,#4da3ff,#0a6fe0);color:#fff;border-color:rgba(255,255,255,.22)}
/* 折叠块：次要内容默认收起（关系链、她的记忆列表） */
.hf3d-ui .fmore{display:block;width:100%;text-align:left;margin:2px 0 4px;padding:6px 9px;font-size:11px;cursor:pointer;
  color:rgba(235,235,245,.72);background:rgba(255,255,255,.05);border:1px solid rgba(0,0,0,.24);border-radius:9px;font-family:inherit}
.hf3d-ui .fmore:hover{background:rgba(110,200,255,.14);color:#eaf7ff}
.hf3d-ui .fmore-b{display:none;padding:2px 2px 4px}
.hf3d-ui .fmore-b.open{display:block}
/* 画像 */
.hf3d-ui .fprof-one{font-size:14px;line-height:1.7;color:#fff;font-weight:600;padding:2px 0 4px}
.hf3d-ui .frow2{display:flex;gap:12px;flex-wrap:wrap;margin:6px 0 4px;font-size:11px;color:rgba(235,235,245,.55)}
.hf3d-ui .frow2 b{color:#f5f5f7;font-family:var(--mono,monospace);font-size:11.5px}
.hf3d-ui .fev{display:flex;gap:8px;font-size:11.5px;line-height:1.7;padding:2px 0}
.hf3d-ui .fev i{font-style:normal;color:rgba(100,210,255,.9);min-width:74px;font-family:var(--mono,monospace);font-size:10.5px}
.hf3d-ui .fev span{color:rgba(245,245,247,.9)}
.hf3d-ui .fnote{margin:6px 0;padding:7px 9px;font-size:11px;line-height:1.7;border-radius:9px;color:#ffe0c2;
  background:rgba(255,170,80,.10);border:1px solid rgba(255,170,80,.28)}
.hf3d-ui .chip.warn{background:rgba(255,120,140,.14);color:#ffb3c0;border-color:rgba(255,120,140,.3)}
.hf3d-ui .fmem{display:block;padding:3px 2px;font-size:11.5px;color:rgba(245,245,247,.85);cursor:pointer;border-bottom:1px dashed rgba(255,255,255,.08)}
.hf3d-ui .fmem:hover{color:#64d2ff}
.hf3d-ui .fpanelAlt{left:12px;top:12px;width:min(680px,72%);z-index:7;max-height:86%;overflow:auto}
.hf3d-ui .fpanelAlt .fbd{padding:12px}
.hf3d-ui .fpanelH{left:50%;transform:translateX(-50%);top:auto;bottom:46px;width:min(720px,70%);z-index:7}
.hf3d-ui .fpanelH.hide{display:none}
.hf3d-ui .fpanelH .fbd,.hf3d-ui .fpanelH{padding:0}
.hf3d-ui .fpanelH .bd{padding:11px 12px}
.hf3d-ui .fpanelH .kpi,.hf3d-ui .fpanelH .card{background:rgba(255,255,255,.045)!important;border-color:rgba(0,0,0,.26)!important;color:#f5f5f7!important}
.hf3d-ui .fpanelH .kpi .lb,.hf3d-ui .fpanelH .mut,.hf3d-ui .fpanelH .card .sub{color:rgba(235,235,245,.6)!important}
.hf3d-ui .fpanelH .kpi .vl{color:#f5f5f7!important}
.hf3d-ui .fpanelH .sec-t,.hf3d-ui .fpanelH .card .t{color:#f5f5f7!important}
.hf3d-ui .ftip{position:absolute;z-index:9;pointer-events:none;display:none;padding:6px 10px;font-size:11.5px;color:#f5f5f7;
  border-radius:11px;background:rgba(18,19,24,.72);border:1px solid rgba(0,0,0,.3);
  box-shadow:0 10px 28px rgba(0,0,0,.5);white-space:nowrap;line-height:1.6;
  backdrop-filter:blur(18px) saturate(180%)}
.hf3d-ui .ftip u{text-decoration:none;color:#64d2ff;font-weight:600}
.hf3d-ui .ftip s{text-decoration:none;color:rgba(235,235,245,.6);display:block;font-size:10.5px}
`;

/**
 * 画布右上的唯一控制台（右上/右下两个面板合并成一个）
 * 头部「收起」= 把内容向上折叠（只留标题条），**收起后标题条还在，随时能再展开**
 * （之前是整块往右滑出并 pointer-events:none，收起就再也点不到了）
 */
function graphPanelHtml(sid, opts = {}) {
  const layouts = LAYOUT_DEFS.map((L, i) =>
    `<button class="fbtn" data-layout="${L.key}" title="${esc(L.label)} / ${esc(L.en)}（快捷键 ${LAYOUT_HOTKEYS[i]}）">${esc(L.short)}</button>`
  ).join("");
  const cams = CAM_DEFS.map((c) => `<button class="fchip" data-cam="${c.key}">${esc(c.label)}</button>`).join("");
  const tiers = TIER_DEFS.map((c) => `<button class="fchip" data-tier="${c.key}" title="${esc(c.hint || c.label)}">${esc(c.label)}</button>`).join("");
  const paras = PARA_DEFS.map(
    (q) =>
      `<div class="frow"><span>${esc(q.label)}</span>
        <input class="frng" type="range" id="${sid}P_${q.key}" min="${q.min}" max="${q.max}" step="${q.step}" value="${q.value}">
        <var id="${sid}V_${q.key}">${q.value}</var></div>`
  ).join("");
  const views = (opts.views || []).length
    ? `<div class="fgrp"><label>页面内容</label><div class="fchips">${opts.views
        .map((v) => `<button class="fchip${v.on ? " on" : ""}" data-view="${v.value}" title="${esc(v.title || v.label)}">${esc(v.label)}</button>`)
        .join("")}</div></div>`
    : "";
  const scope = opts.scope
    ? `<div class="fgrp" style="margin:9px 0 0"><label>图谱范围</label><div class="fchips">
        <button class="fchip${opts.scope === "view" ? " on" : ""}" data-scope="view" title="跟随顶栏的查看视角过滤">按当前视角</button>
        <button class="fchip${opts.scope === "all" ? " on" : ""}" data-scope="all" title="不过滤人格，看整个记忆库">全库</button>
      </div></div>`
    : "";
  return `
  <div class="fpanel" id="${sid}Panel">
    <div class="fph">图谱控制台<b data-act="toggle-panel">收起</b></div>
    <div class="fbd">
      ${views}
      <div class="fgrp"><label>显示样式（3 种布局）</label><div class="fbtns" data-list="layout">${layouts}</div></div>
      <div class="fgrp"><label>镜头</label><div class="fchips" data-list="cam">${cams}</div></div>
      <div class="fgrp"><label>分层依据（换层，不只是换色）</label><div class="fchips" data-list="tier">${tiers}</div></div>
      <div class="fgrp"><label>显示开关</label><div class="fchips">
        <button class="fchip on" data-tgl="links">连线</button>
        <button class="fchip on" data-tgl="labels">标签</button>
        <button class="fchip" data-tgl="tour">自动巡演</button>
      </div></div>
      <div class="fgrp"><label>参数</label>${paras}</div>
      <div class="fgrp" style="margin-bottom:0"><label>检索与复位</label>
        <input class="fsearch" id="${sid}Search" placeholder="输入记忆 / 实体名，回车定位">
        <div class="fchips" style="margin-top:6px">
          <button class="fchip" data-act="reset-all" title="相机、平移、挪动过的节点、选中状态全部回到初始（等于重新选一次当前显示样式）">复位视图</button>
          <button class="fchip" data-act="export-png">导出 PNG</button>
          ${opts.health ? `<button class="fchip" data-act="toggle-health" title="关系类型构成 / 枢纽节点（叠在画布上）">体检</button>` : ""}
        </div>
        ${scope}
      </div>
      <div class="fhint">拖拽旋转 · 滚轮以鼠标为中心缩放 · <b>中键拖动平移</b> · 悬停高亮邻居 · 单击锁定 · 双击聚焦<br>
        快捷键：1-9 / 0 / q / w 切布局 · 空格暂停巡演 · H 收起面板 · L 标签 · R 复位视角</div>
    </div>
  </div>`;
}

/** HUD + 卡片（浮在画布上的那几块） */
function graphHudHtml(sid, opts = {}) {
  return `
  <div class="hud fmetrics" id="${sid}Top"></div>
  <div class="hud fmetrics fmetricsB" id="${sid}Bottom"></div>
  <div class="hud fgstat" id="${sid}Status"><span class="fdot"></span>读取中…</div>
  <div class="hud flegend" id="${sid}Legend"></div>
  <div class="fcard" id="${sid}Card"></div>
  <div class="ftip" id="${sid}Tip"></div>
  ${opts.extra || ""}`;
}

/** 把控制台接到引擎上（布局/镜头/分层/滑条/开关/搜索/快捷键/卡片） */
function wireGraphConsole(host, sid, eng, opts = {}) {
  const q = (sel) => host.querySelector(sel);
  const cardHtml = opts.cardHtml || (() => "");
  const onSel = (n) => {
    const c = q(`#${sid}Card`);
    if (c) {
      if (!n) {
        c.classList.remove("on", "expanded");
        c.innerHTML = "";
        // 注意：不要清空 LAST_CARD —— 详情是异步拉的，清掉它会导致回来时无处重绘（空卡片）
      } else {
        // 走 paintCard：这样"展开详细"的状态在每次重绘后都还在
        paintCard(host, sid, n, eng, (nn, ee) => cardHtml(nn, ee));
      }
    }
    if (opts.onSelect) opts.onSelect(n);
  };
  // 事件全部重新挂（多页复用同一外壳时要避免重复触发）
  ["layout", "cam", "pal", "tier", "stats", "quality", "legend", "select", "hover"].forEach((ev) => eng.offAll && eng.offAll(ev));
  eng.on("select", onSel);
  eng.on("layout", (d) => {
    const m = q(`#${sid}Mode`);
    if (m) {
      m.innerHTML = `${esc(d.label)}<small>${esc(d.en)}</small>`;
      m.classList.remove("hide");
      clearTimeout(m._t);
      m._t = setTimeout(() => m.classList.add("hide"), 2600);
    }
    host.querySelectorAll(`[data-layout]`).forEach((b) => b.classList.toggle("on", b.dataset.layout === d.key));
  });
  eng.on("legend", () => paintLegend(host, sid, eng));
  eng.on("stats", (st) => {
    // 节点/关系/分层等"数据"统一在底部那条参数排里显示，这里只留运行态，避免同一数字出现两处
    fillBottomMetrics(host, sid, st);
    const el = q(`#${sid}Status`);
    if (!el) return;
    el.innerHTML = `<span class="fdot"></span>FPS <b>${st.fps}</b> &nbsp;|&nbsp; ` +
      `镜头 <b>${esc((CAM_DEFS.find((c) => c.key === st.cam) || {}).label || st.cam)}</b> &nbsp;|&nbsp; ` +
      `<b>${st.tour ? "自动巡演中" : st.spin > 0 ? "自转中" : "静止"}</b> &nbsp;|&nbsp; ` +
      `${esc(st.gpu)}${st.quality === "low" ? " · 流畅模式" : ""}`;
  });
  eng.on("hover", (n) => {
    const t = q(`#${sid}Tip`);
    if (!t) return;
    if (!n) {
      t.style.display = "none";
      return;
    }
    const it = (eng.nodes || []).find((x) => String(x.id) === String(n.id));
    t.style.display = "block";
    t.style.left = "12px";
    t.style.top = "12px";
    t.innerHTML = `<u>${esc(String(n.label || n.id || ""))}</u><s>${it ? esc(nodeKindText(it)) : ""}</s>`;
  });
  // 布局按钮
  host.querySelectorAll("[data-layout]").forEach((b) =>
    b.addEventListener("click", () => {
      eng.selectNode(-1);
      eng.focus = [0, 0, 0];
      eng.setLayout(b.dataset.layout, false);
      eng.tour.on = false;
      host.querySelectorAll('[data-tgl="tour"]').forEach((x) => x.classList.toggle("on", false));
      saveUiPref({ layout: b.dataset.layout, toggles: { tour: false } });
    })
  );
  // 镜头 / 配色 / 分层
  host.querySelectorAll("[data-cam]").forEach((b) =>
    b.addEventListener("click", () => {
      host.querySelectorAll("[data-cam]").forEach((x) => x.classList.toggle("on", x === b));
      eng.setCam(b.dataset.cam, false);
      saveUiPref({ cam: b.dataset.cam });
    })
  );
  host.querySelectorAll("[data-tier]").forEach((b) =>
    b.addEventListener("click", () => {
      host.querySelectorAll("[data-tier]").forEach((x) => x.classList.toggle("on", x === b));
      eng.setTierMode(b.dataset.tier);
      eng.setLayout(eng.mode, false);
      saveUiPref({ tier: b.dataset.tier });
    })
  );
  // 参数滑条
  PARA_DEFS.forEach((def) => {
    const el = q(`#${sid}P_${def.key}`);
    const out = q(`#${sid}V_${def.key}`);
    if (!el) return;
    const push = () => {
      if (out) out.textContent = def.key === "cap" ? String(Math.round(Number(el.value))) : Number(el.value).toFixed(2);
      eng.setPara(def.key, Number(el.value));
      const pv = (UI_PREF.value && UI_PREF.value.params) || {};
      pv[def.key] = Number(el.value);
      saveUiPref({ params: pv });
    };
    el.addEventListener("input", push);
    el.addEventListener("change", push);
  });
  // 开关
  host.querySelectorAll("[data-tgl]").forEach((b) =>
    b.addEventListener("click", () => {
      const on = !b.classList.contains("on");
      b.classList.toggle("on", on);
      eng.setToggle(b.dataset.tgl, on);
      const tv = (UI_PREF.value && UI_PREF.value.toggles) || {};
      tv[b.dataset.tgl] = on;
      saveUiPref({ toggles: tv });
    })
  );
  // 范围（按当前视角 / 全库）
  host.querySelectorAll("[data-scope]").forEach((b) =>
    b.addEventListener("click", () => {
      host.querySelectorAll("[data-scope]").forEach((x) => x.classList.toggle("on", x === b));
      if (opts.onScope) opts.onScope(b.dataset.scope);
    })
  );
  // 复位挪动 / 收起面板
  host.querySelectorAll('[data-act="reset-pins"]').forEach((b) =>
    b.addEventListener("click", () => {
      eng.clearPins();
      toast("已把挪动过的节点复位", "ok");
    })
  );
  host.querySelectorAll('[data-act="toggle-panel"]').forEach((b) =>
    b.addEventListener("click", () => {
      const pn = q(`#${sid}Panel`);
      if (!pn) return;
      // 向上折叠：只藏内容区，标题条留在原处，收起后点一下就能再展开
      const folded = pn.classList.toggle("folded");
      b.textContent = folded ? "展开" : "收起";
      saveUiPref({ folded });
    })
  );
  host.querySelectorAll("[data-view]").forEach((b) =>
    b.addEventListener("click", () => {
      host.querySelectorAll("[data-view]").forEach((x) => x.classList.toggle("on", x === b));
      if (opts.onView) opts.onView(b.dataset.view);
    })
  );
  host.querySelectorAll('[data-act="export-png"]').forEach((b) => b.addEventListener("click", () => hfGraphExport()));
  host.querySelectorAll('[data-act="reset-all"]').forEach((b) =>
    b.addEventListener("click", () => {
      if (eng.resetAll) eng.resetAll();
      else if (eng.fit) eng.fit();
      toast("视图已复位", "ok");
    })
  );
  host.querySelectorAll('[data-act="toggle-health"]').forEach((b) =>
    b.addEventListener("click", () => {
      const hp = q(`#${sid}Health`);
      if (!hp) return;
      const hidden = hp.classList.toggle("hide");
      b.classList.toggle("on", !hidden);
      if (!hidden && opts.onHealthOpen) opts.onHealthOpen(hp);
    })
  );
  // 搜索（回车定位）
  const se = q(`#${sid}Search`);
  if (se) {
    se.addEventListener("input", () => eng.setSearch(se.value));
    se.addEventListener("keydown", (e) => {
      if (e.key === "Enter") eng.focusNode(null);
    });
  }
  // 卡片里的邻居点击 + 动作按钮
  const cd = q(`#${sid}Card`);
  if (cd) {
    cd.addEventListener("click", (e) => {
      const t = e.target;
      if (!t) return;
      if (t.dataset && t.dataset.goto) eng.focusNode(String(t.dataset.goto));
    });
  }
  // 快捷键
  const hk = (e) => {
    if (!host.isConnected || !q(`#${sid}Panel`)) return;
    const tag = (e.target && e.target.tagName) || "";
    if (tag === "INPUT" || tag === "TEXTAREA") return;
    const i = LAYOUT_HOTKEYS.indexOf(String(e.key).toLowerCase());
    if (i >= 0) {
      eng.selectNode(-1);
      eng.setLayout(LAYOUT_DEFS[i].key, false);
      return;
    }
    if (e.key === " ") {
      e.preventDefault();
      eng.tour.on = !eng.tour.on;
      host.querySelectorAll('[data-tgl="tour"]').forEach((x) => x.classList.toggle("on", eng.tour.on));
    } else if (e.key === "h" || e.key === "H") {
      const pn = q(`#${sid}Panel`);
      const b = q(`[data-act="toggle-panel"]`);
      if (pn) {
        const folded = pn.classList.toggle("folded");
        if (b) b.textContent = folded ? "展开" : "收起";
      }
    } else if (e.key === "l" || e.key === "L") {
      eng.showLabels = !eng.showLabels;
      const b = host.querySelector('[data-tgl="labels"]');
      if (b) b.classList.toggle("on", eng.showLabels);
    } else if (e.key === "r" || e.key === "R") {
      eng.focus = [0, 0, 0];
      eng.setCam("orbit", false);
    }
  };
  if (eng._hk) window.removeEventListener("keydown", eng._hk);
  eng._hk = hk;
  window.addEventListener("keydown", hk);
  // 初始高亮
  host.querySelectorAll('[data-layout]').forEach((b) => b.classList.toggle("on", b.dataset.layout === eng.mode));
  host.querySelectorAll("[data-cam]").forEach((b) => b.classList.toggle("on", b.dataset.cam === eng.camKey));
  host.querySelectorAll("[data-tier]").forEach((b) => b.classList.toggle("on", b.dataset.tier === eng.tierMode));
  paintLegend(host, sid, eng);
  return eng;
}

/** 顶部一排：数据概览（来自接口的真数据；跟左下状态行/图例不重复） */
function fillTopMetrics(host, sid, items) {
  const box = host.querySelector("#" + sid + "Top");
  if (!box) return;
  box.innerHTML = (items || [])
    .filter((it) => it && it[1] !== undefined && it[1] !== null && it[1] !== "")
    .map((it) => `<span class="fm${it[2] ? " " + it[2] : ""}"><b>${esc(String(it[1]))}</b>${esc(it[0])}</span>`)
    .join("");
}

/** 底部一排：当前视图运行态（绘制规模/分层/挪动/选中） */
function fillBottomMetrics(host, sid, st) {
  const box = host.querySelector("#" + sid + "Bottom");
  if (!box || !st) return;
  const lv = (st.levels || []).join("/");
  const sel = st.selected ? `选中 ${st.selected}` : "未选中";
  const acts = LAYOUT_DEFS.find((x) => x.key === st.layout);
  box.innerHTML = [
    `<span class="fm"><b>${st.drawn}</b>绘制节点${st.drawn < st.nodes ? " / 共 " + st.nodes : ""}</span>`,
    `<span class="fm"><b>${st.drawnLinks}</b>绘制关系</span>`,
    `<span class="fm"><b>${lv}</b>分层</span>`,
    `<span class="fm"><b>${st.cap}</b>采样上限</span>`,
    `<span class="fm"><b>${st.pinned}</b>已挪动</span>`,
    `<span class="fm"><b>${esc(acts ? acts.label : st.layout)}</b>布局</span>`,
    `<span class="fm${st.selected ? " hot" : ""}">${esc(sel)}</span>`,
  ].join("");
}

/** 节点的一句话类型说明（tooltip / 卡片标题用） */
function nodeKindText(it) {
  if (!it) return "";
  const L = Number.isFinite(it.roleLevel) ? it.roleLevel : null;      // 语义层，不随"分层依据"漂
  const ln = L === null ? "—" : `L${L}${LEVEL_DEFS[L] ? " " + LEVEL_DEFS[L].name : ""}`;
  if (it.role === "person") return `人物（第 0 层 · ${ln}） · ${it.deg} 条关系`;
  if (it.role === "session") return `群 / 会话（第 1 层 · ${ln}） · ${it.deg} 条关系`;
  if (it.isEntity) return `话题 / 实体（第 2 层 · ${ln}） · ${it.deg} 条关系`;
  const atom = ATOM_LABEL_3D[it.atom] || it.atom || "记忆";
  return `${atom} · ${ln} · ${it.deg} 条关系`;
}

function _pct(v) {
  const t = Object.keys(ATOM_LABEL_3D).length;
  return t ? v : v;
}

/** 图例：优先用引擎给的 legendData（跟着配色/分层依据走），老引擎退回按类型统计 */
function legendHtml3d(g3) {
  if (!g3 || !g3.nodes || !g3.nodes.length) return `<div class="mut" style="font-size:10.5px">没有数据</div>`;
  const data = typeof g3.legendData === "function" ? g3.legendData() : null;
  if (data) {
    const rows = (data.items || [])
      .slice(0, 9)
      .map((it) => `<div><i style="color:${it.color}"></i>${esc(it.label)}${Number.isFinite(it.count) ? ` <b>${fmtNum(it.count)}</b>` : ""}</div>${it.def ? `<div style="font-size:10px;opacity:.62;margin:0 0 4px 15px">${esc(it.def)}</div>` : ""}`)
      .join("");
    return `<div><b>${esc(data.title)}</b></div>${rows}<div style="margin-top:4px">线越亮＝关系越强；每个节点只属于一层；点节点看细节</div>`;
  }
  const atoms = new Map();
  let entCount = 0;
  g3.nodes.forEach((n) => {
    if (n.isEntity || n.kind === "entity") {
      entCount++;
      return;
    }
    const k = n.atom || "";
    atoms.set(k, (atoms.get(k) || 0) + 1);
  });
  let html = `<div><b>颜色 = 记忆类型</b></div>`;
  Array.from(atoms.entries())
    .sort((a, b) => b[1] - a[1])
    .slice(0, 7)
    .forEach(([k, c]) => {
      html += `<div><i style="color:${atomColor(k)}"></i>${esc(ATOM_LABEL[k] || ATOM_LABEL_3D[k] || k || "其他")} <b>${fmtNum(c)}</b></div>`;
    });
  if (entCount) html += `<div><i style="color:#8b5cf6"></i>实体 / 话题 <b>${fmtNum(entCount)}</b></div>`;
  html += `<div style="margin-top:4px">线越亮＝关系越强；点节点看细节</div>`;
  return html;
}

function paintLegend(host, sid, g3) {
  const el = host.querySelector("#" + sid + "Legend");
  if (el) el.innerHTML = legendHtml3d(g3);
}

/* ==================================================================
   记忆点/人物卡片的"原地放大"：点「展开详细」把左侧这张概括卡撑大，
   在卡片里直接显示详细内容 —— 不再从右侧拉抽屉（用户明确要求）。
   ================================================================== */
let CARD_DET = { key: "", html: "", busy: false };
let LAST_CARD = null;   // { host, sid, node, eng, key }

/**
 * 把 id 归一到"卡片 key 用的那种写法"。
 * 人物的裸 QQ（100000002）和引擎节点 id（u100000002）是同一个人的两种写法，
 * 不归一就会出现"卡片 key 和按钮传来的 id 对不上、点了等于没点"（实测踩过）。
 */
function canonId(sid, id) {
  const t = String(id == null ? "" : id);
  return sid === "p" && /^[0-9]+$/.test(t) ? "u" + t : t;   // 用字符类，避免转义在不同层被吃掉
}

/** 卡片渲染：写 innerHTML + 挂展开状态（重绘也保持一致） */
function paintCard(host, sid, node, eng, render) {
  const card = host.querySelector("#" + sid + "Card");
  if (!card) return;
  card.dataset.key = sid + ":" + canonId(sid, node && node.id != null ? node.id : "");
  card.classList.add("on");
  // 每页可以有自己的卡片渲染函数（人物画像就是自己那套）
  card.innerHTML = render ? render(node, eng) : nodeCardHtml(host, sid, node, eng);
  LAST_CARD = { host, sid, node, eng, key: card.dataset.key, render };
  CARD_EL = LAST_CARD;
  const on = CARD_DET.key && CARD_DET.key === card.dataset.key;
  card.classList.toggle("expanded", !!on);
  const box = card.querySelector(".fdet");
  if (box) box.innerHTML = on ? (CARD_DET.html || "") : "";
  const btn = card.querySelector("[data-det-btn]");
  if (btn) btn.textContent = on ? "收起详细" : "展开详细";
}

/** 用上次的节点重绘（展开/收起后要把按钮文案也刷掉） */
let CARD_EL = null;   // 最近一次真正画过的卡片（引擎取消选中会把 LAST_CARD 清掉，重绘要靠它兜底）

function repaintCard() {
  const c = LAST_CARD || CARD_EL;
  if (!c) return;
  // 关键：按"当前文档里的卡片 id"重绘，而不是记忆里的宿主元素 ——
  // 页面重挂时宿主会换，往旧宿主里画就是"卡片撑开了却看不到内容"（实测踩过）。
  const card = document.getElementById(c.sid + "Card");
  if (!card) return;
  const host = card.closest(".hf3d-ui") || c.host;
  const key = c.sid + ":" + canonId(c.sid, c.node && c.node.id != null ? c.node.id : "");
  card.dataset.key = key;
  card.classList.add("on");
  card.innerHTML = c.render ? c.render(c.node, c.eng) : nodeCardHtml(host, c.sid, c.node, c.eng);
  const on = CARD_DET.key && CARD_DET.key === key;
  card.classList.toggle("expanded", !!on);
  const box = card.querySelector(".fdet");
  if (box) box.innerHTML = on ? (CARD_DET.html || "") : "";
  const btn = card.querySelector("[data-det-btn]");
  if (btn) btn.textContent = on ? "收起详细" : "展开详细";
}

/** 通用折叠：默认收起，点了才展开（关系链这类次要内容一律默认收着） */
export function hfToggleMore(btn) {
  const id = btn && btn.dataset ? btn.dataset.more : "";
  const box = id ? document.querySelector('[data-morebox="' + id + '"]') : null;
  if (!box) return;
  const open = box.classList.toggle("open");
  if (btn) btn.textContent = (open ? "收起" : "展开") + String(btn.dataset.label || "");
}

/** 折叠块（默认收起）：主内容先给，次要内容点开才看 */
function moreBlock(id, label, count, inner) {
  return `<button class="fmore" data-more="${esc(id)}" data-label="${esc(label)}"
      onclick="hfToggleMore(this)">展开${esc(label)}（${count}）</button>
    <div class="fmore-b" data-morebox="${esc(id)}">${inner}</div>`;
}

/**
 * 记忆详情：把后端已有的东西全摆出来。
 * 重点是"原始对话"——用户要的是：图谱里显示简写，展开看详细对话；顺带把 ID、向量状态也亮出来
 * （之前这些字段一个都没显示，等于把数据全浪费了）。
 */
async function memDetailHtml(m, edges, sid) {
  const mid = String(m.id);
  const rawId = mid.replace(/^m/, "");
  // 单条详情走后端 memory 接口：content/evidence/embedding/edges 都在那里
  const full = await apiSafe("memory", { id: rawId }, null);
  const M = Object.assign({}, m.raw || m, (full && full.memory) || full || {});
  const rels = (edges && edges.length ? edges : ((full && (full.edges || []))) || []);
  const rows = (rels || []).map((e) => {
    const other = String(e.src_id) === mid || String(e.src_id) === "m" + rawId ? e.dst_id : e.src_id;
    return `<div class="dkv"><div class="k">${esc(LINK_LABEL[e.etype] || e.etype || "关联")}</div>
      <div class="v">${esc(String(other))} · 权重 ${fmt1(e.weight)}</div></div>`;
  });
  const ents = M.entities || m.entities || [];
  const cues = M.cues || m.cues || [];
  const dialogue = String(M.evidence || "").trim();
  const content = String(M.content || "").trim();
  // 向量状态：只有"当前代"里有才算已生成；旧代有 = 待重生成（补全向量即生成，旧向量没丢）
  const vinfo = (M && M.vector) || (full && full.vector) || {};
  const hasVec = !!vinfo.has_embedding;
  const oldGens = (vinfo.gens || []).filter((g) => String(g.gen_key) !== String(vinfo.gen));
  const vecModel = (full && full.memory && full.memory.embedding_model) || M.embedding_model || "";
  return `
    <div class="fdet-h">原始对话（这条记忆是从这里来的）</div>
    ${dialogue
      ? `<div class="fdia">${esc(dialogue)}</div>`
      : `<div class="fnote">这条记忆没存下原始对话 —— 如果是「导入」进来的旧数据，源头就没带语境；<br>
           新对话产生的记忆都会带上原话（可核对）</div>`}
    <div class="fdet-h">简写（图谱上显示的那句）</div>
    <div class="fdet-tx">${esc(M.summary || m.summary || "(无摘要)")}</div>
    ${content && content !== M.summary ? `<div class="fdet-h">补充内容</div><div class="fdet-q">${esc(content)}</div>` : ""}
    <div class="fdet-h">关键信息</div>
    <div class="frow2">
      <span>ID <b>m${esc(rawId)}</b></span>
      <span>强度 <b>${fmt1(M.strength ?? m.strength)}</b></span>
      <span>情绪 <b>${M.poignancy ?? "—"}/10</b></span>
      <span>召回 <b>${fmtNum(M.recalls)}</b></span>
      <span>向量 ${
        hasVec
          ? `<b>已生成</b>${vinfo.dim ? ` · ${fmtNum(vinfo.dim)} 维` : ""} · <span class="mono">${esc(String(vinfo.gen || ""))}</span>`
          : oldGens.length
            ? `<b>旧代有</b> · ${esc(oldGens.map((g) => `${g.gen_key}${g.dim ? `（${g.dim} 维）` : ""}`).join("、"))}<span class="mut">（当前代还没生成，点记忆库的「补全向量」即生成，旧向量没丢）</span>`
            : "<b>未生成</b>"
      }</span>
    </div>
    <div class="frow2">
      <span>发生 <b>${esc(fmtTime(M.occurred_at || m.ts))}</b></span>
      <span>类型 <b>${esc(ATOM_LABEL[M.atom_type] || M.atom_type || "—")}</b></span>
      <span>会话 <b title="${esc(String(M.session_id || M.session || ""))}">${esc(sessLabel(M.session_id || M.session))}</b></span>
      <span>来源 <b>${esc(String(M.source_mode || "—"))}</b></span>
    </div>
    ${M.external_source ? `<div class="frow2"><span>导入自 <b>${esc(String(M.external_source))}</b>${M.external_id ? " · " + esc(String(M.external_id)) : ""}</span>${M.import_batch ? `<span>批次 <b>${esc(String(M.import_batch))}</b></span>` : ""}</div>` : ""}
    ${ents.length ? `<div class="fdet-h">涉及实体 / 人物 / 话题（${ents.length}）</div><div class="chips">${ents.map((x) => `<span class="chip">◍ ${esc(String(x))}</span>`).join("")}</div>` : ""}
    ${cues.length ? `<div class="fdet-h">召回线索</div><div class="chips">${cues.map((x) => `<span class="chip hot">${esc(String(x))}</span>`).join("")}</div>` : ""}
    <div class="fdet-h">关系链</div>
    ${rows.length
      ? moreBlock("rel-" + rawId, "关系链", rows.length, rows.join(""))
      : `<div class="mut" style="font-size:11px">这条记忆还没有关系链 · 试试图谱页的「重建关系链」</div>`}
    <div class="row" style="gap:6px;margin-top:10px;flex-wrap:wrap">
      <button class="btn xs" onclick="hfMemEdit(${jsStr(rawId)})">改</button>
      <button class="btn xs" onclick="hfMemBoost(${jsStr(mid)})">↑ 加权</button>
      <button class="btn xs" onclick="hfMemDemote(${jsStr(mid)})">↓ 降权</button>
      ${Number(M.muted || 0) === 1
        ? `<button class="btn xs" onclick="hfMemMute(${jsStr(rawId)}, 0)">恢复提及</button>`
        : `<button class="btn xs" onclick="hfMemMute(${jsStr(rawId)}, 1)">别再提</button>`}
      <button class="btn xs" onclick="hfGraphOpen(${jsStr(mid)})">在图谱里看</button>
      ${String(M.status || m.status || "") === "deleted" || String(M.status || m.status || "") === "forgotten"
        ? `<button class="btn xs" onclick="hfMemRestore(${jsStr(rawId)})">从回收站恢复</button>`
        : `<button class="btn xs dg" onclick="hfMemDelete(${jsStr(rawId)})">删除</button>`}
      <button class="btn xs" data-det-btn onclick="hfCardDet(${jsStr(sid || "g")}, ${jsStr(mid)})">收起详细</button>
    </div>`;
}

/** 编辑记忆（接后端 memory/update）；sandbox iframe 吞原生 prompt，改走页面内表单弹窗 */
export async function hfMemEdit(mid) {
  const full = await apiSafe("memory", { id: String(mid).replace(/^m/, "") }, null);
  const cur = (full && (full.memory || full)) || {};
  formModal("编辑记忆", [
    { key: "summary", label: "简写（图谱上显示的那句）", value: String(cur.summary || "") },
    { key: "content", label: "补充内容（可留空）", value: String(cur.content || ""), area: true },
  ], async (v) => {
    const res = await apiPostSafe("memory/update", {
      id: String(mid).replace(/^m/, ""),
      fields: { summary: String(v.summary || "").trim(), content: String(v.content || "").trim() },
    });
    toast(res.ok ? "已保存" : "保存失败：" + (res.error || ""), res.ok ? "ok" : "err");
    if (res.ok) hfRerenderHook();
    return res.ok;
  });
}

/** 删除记忆（软删：进回收站，可恢复；关系链与原文都保留） */
export function hfMemDelete(mid) {
  confirmModal("移入回收站", "模型不会再参考它；原始内容与关系链会保留，可在这里恢复（不会再点一次就永久消失）。", async () => {
    const res = await apiPostSafe("memory/delete", { id: String(mid).replace(/^m/, "") });
    toast(res.ok ? "已移入回收站（可恢复）" : "删除失败：" + (res.error || ""), res.ok ? "ok" : "err");
    if (res.ok) hfRerenderHook();
  });
}

/** 从回收站恢复一条记忆（对应后端 memory/restore） */
export async function hfMemRestore(mid) {
  const res = await apiPostSafe("memory/restore", { id: String(mid).replace(/^m/, "") });
  toast(res.ok ? "已恢复" : "恢复失败：" + (res.error || ""), res.ok ? "ok" : "err");
  if (res.ok) hfRerenderHook();
}

/** 实体/话题节点的详情 */
function entDetailHtml(node, eng) {
  const it = (eng.nodes || []).find((x) => String(x.id) === String(node.id)) || {};
  const nb = [];
  (eng.links || []).forEach((l) => {
    if (l.a === it.idx || l.b === it.idx) nb.push((eng.nodes[l.a === it.idx ? l.b : l.a] || {}).name || "");
  });
  return `
    <div class="fdet-h">这是什么</div>
    <div class="fdet-tx">实体 / 话题节点：它本身不是一条记忆，而是把相关记忆连起来的"枢纽"。</div>
    <div class="kv">关系度<span>${fmtNum(it.deg)}</span></div>
    <div class="kv">直接相连<span>${nb.length} 个节点</span></div>
    <div class="fdet-h">相连的节点</div>
    <div class="chips">${nb.slice(0, 40).map((x) => `<span class="chip">◍ ${esc(String(x))}</span>`).join("") || "—"}</div>`;
}

/** 画像字段渲染小工具 */
function chipList(arr, cls) {
  const a = Array.isArray(arr) ? arr.filter((x) => String(x || "").trim()) : [];
  if (!a.length) return `<span class="mut" style="font-size:11px">材料里没有写到</span>`;
  return a.map((x) => `<span class="chip${cls || ""}">${esc(String(x))}</span>`).join("");
}

/**
 * 人物画像：这是"这个人是谁、和她怎么相处"——画像正文优先。
 * 没有画像就是缺陷：明确提示 + 给「生成画像」按钮（而不是显示几个计数器当画像）。
 */
async function buildPersonDetail(id, kind = "user") {
  const isGroup = kind === "group";
  const pid = String(id).replace(/^[ug]/, "");
  const persona = STATE.personaView || "";
  const prof = await apiSafe("person/profile", { pid, kind, persona_id: persona }, null);
  if (!prof || prof.ok === false) {
    return `<div class="fdet-h">${isGroup ? "群聊画像" : "人物画像"}</div>
      <div class="fdet-tx">画像接口异常：${esc(String((prof && prof.error) || "没有响应"))}</div>`;
  }
  const P = prof.profile || {};
  const mems = Array.isArray(prof.memories) ? prof.memories : [];
  const head = `
    <div class="kv">${isGroup ? "群号" : "QQ / 号"}<span>${esc(pid)}</span></div>
    ${prof.name && String(prof.name) !== pid ? `<div class="kv">${isGroup ? "群名" : "称呼"}<span>${esc(String(prof.name))}</span></div>` : ""}
    <div class="kv">层 / 人格<span>L${isGroup ? 1 : 0} · ${esc(prof.profile_persona || persona || "未归属人格")}</span></div>
    <div class="frow2">
      <span>互动 <b>${fmtNum(prof.msg_count)}</b> 条</span>
      <span>记忆 <b>${fmtNum(prof.mem_count)}</b> 条</span>
      ${isGroup ? `<span>活跃成员 <b>${fmtNum((prof.members || []).length)}</b> 人</span>` : `<span>会话 <b>${fmtNum(prof.sessions)}</b> 个</span>`}
      ${prof.updated_at ? `<span>画像更新 <b>${esc(timeAgo(prof.updated_at))}</b></span>` : ""}
    </div>`;
  const otherPersonas = (prof.persona_ids || []).filter((x) => x && x !== (prof.profile_persona || persona));
  const body = !prof.has_profile
    ? `<div class="fdet-h">${isGroup ? "群聊画像" : "人物画像"}：还没有</div>
       <div class="fnote">${prof.invalid
         ? "⚠ 库里那份画像是在材料不足时生成的，没有依据，已标记为待重建：" + esc(String(prof.invalid_reason || ""))
         : "⚠ 还没生成（不是\"没内容\"）：" + esc(String(prof.reason || "尚未生成"))}</div>
       ${otherPersonas.length
         ? `<div class="fnote">ⓘ 画像按人格隔离：${esc(otherPersonas.join("、"))} 人格下有这位的画像，当前人格（${esc(persona || "未筛")}）下没有 —— 两边内容互不可见；要在这里用就现在生成。</div>`
         : ""}
       <div class="row" style="gap:6px;margin-top:9px;flex-wrap:wrap">
         <button class="btn xs p" onclick="hfProfileBuild(${jsStr(pid)}, this, ${jsStr(isGroup ? "group" : "user")}, ${jsStr(persona)})">${prof.llm_ready ? (isGroup ? "生成群聊画像" : "生成人物画像") : "生成（需先配置模型）"}</button>
         <button class="btn xs" onclick="hfPersonsRefresh(this)">重新聚合人物</button>
       </div>
       ${profProfileErr ? `<div class="fnote">上次生成失败：${esc(profProfileErr)}</div>` : ""}`
    : isGroup
    ? `<div class="fdet-h">一句话</div>
       <div class="fprof-one">${esc(String(P.one_line || "（模型没给出一句话概括）"))}</div>
       ${P.kind ? `<div class="fdet-h">群的性质</div><div class="fdet-tx">${esc(String(P.kind))}</div>` : ""}
       ${P.atmosphere ? `<div class="fdet-h">氛围</div><div class="fdet-tx">${esc(String(P.atmosphere))}</div>` : ""}
       ${(Array.isArray(P.topics) && P.topics.length) ? `<div class="fdet-h">常聊话题</div><div class="chips">${chipList(P.topics)}</div>` : ""}
       ${(Array.isArray(P.members) && P.members.length)
          ? `<div class="fdet-h">活跃成员</div>${P.members.slice(0, 12).map((m) => `<div class="fev"><i>${esc(String((m && m.n) || ""))}</i><span><b>${esc(String((m && m.who) || ""))}</b>${m && m.role ? " · " + esc(String(m.role)) : ""}</span></div>`).join("")}`
          : ""}
       ${(Array.isArray(P.norms) && P.norms.length) ? `<div class="fdet-h">默契与忌讳</div><div class="chips">${chipList(P.norms, " warn")}</div>` : ""}
       ${P.relation ? `<div class="fdet-h">这个群怎么处</div><div class="fdet-tx">${esc(String(P.relation))}</div>` : ""}
       ${P.notes ? `<div class="fdet-h">其他要注意的</div><div class="fdet-tx">${esc(String(P.notes))}</div>` : ""}
       <div class="row" style="gap:6px;margin-top:10px;flex-wrap:wrap">
         <button class="btn xs p" onclick="hfProfileBuild(${jsStr(pid)}, this, 'group', ${jsStr(persona)})">重新生成群聊画像</button>
         <button class="btn xs" onclick="hfCardDet('p', 'g${esc(pid)}')">收起详细</button>
       </div>
       ${profProfileErr ? `<div class="fnote">上次生成失败：${esc(profProfileErr)}</div>` : ""}`
    : `<div class="fdet-h">一句话</div>
       <div class="fprof-one">${esc(String(P.one_line || "（模型没给出一句话概括）"))}</div>
       ${P.identity ? `<div class="fdet-h">身份 / 称呼</div><div class="fdet-tx">${esc(String(P.identity))}</div>` : ""}
       ${(Array.isArray(P.traits) && P.traits.length) ? `<div class="fdet-h">性格</div><div class="chips">${chipList(P.traits)}</div>` : ""}
       ${(Array.isArray(P.likes) && P.likes.length) ? `<div class="fdet-h">喜欢 / 在意</div><div class="chips">${chipList(P.likes, " hot")}</div>` : ""}
       ${(Array.isArray(P.dislikes) && P.dislikes.length) ? `<div class="fdet-h">忌讳 / 雷区</div><div class="chips">${chipList(P.dislikes, " warn")}</div>` : ""}
       ${(Array.isArray(P.topics) && P.topics.length) ? `<div class="fdet-h">常聊话题</div><div class="chips">${chipList(P.topics)}</div>` : ""}
       ${P.relation ? `<div class="fdet-h">怎么和她相处</div><div class="fdet-tx">${esc(String(P.relation))}</div>` : ""}
       ${(Array.isArray(P.events) && P.events.length)
          ? `<div class="fdet-h">关键事件</div>${P.events.slice(0, 8).map((e) => `<div class="fev"><i>${esc(String((e && e.when) || ""))}</i><span>${esc(String((e && e.what) || e || ""))}</span></div>`).join("")}`
          : ""}
       ${P.notes ? `<div class="fdet-h">其他要注意的</div><div class="fdet-tx">${esc(String(P.notes))}</div>` : ""}
       <div class="row" style="gap:6px;margin-top:10px;flex-wrap:wrap">
         <button class="btn xs p" onclick="hfProfileBuild(${jsStr(pid)}, this, 'user', ${jsStr(persona)})">重新生成画像</button>
         <button class="btn xs" onclick="hfCardDet('p', 'u${esc(pid)}')">收起详细</button>
       </div>
       ${profProfileErr ? `<div class="fnote">上次生成失败：${esc(profProfileErr)}</div>` : ""}`;
  return `<div class="fdet-h">${isGroup ? "群聊画像（L1）" : "人物画像（L0）"}</div>` + head + body +
    (mems.length
      ? `<div class="fdet-h">画像依据：由 ${fmtNum(prof.mem_count)} 条记忆总结而来</div>` +
        `<div class="mut" style="font-size:11px;margin-bottom:5px">下面列出的是喂给模型的记忆（带 ID，点一条看它的原始对话）</div>` +
        mems.map((m) => `<div class="fmem" onclick="hfCardDet('p','m${esc(String(m.id))}')">
            <span class="fmem-id mono">m${esc(String(m.id))}</span>
            <span class="fmem-sum">${esc(String(m.summary || "").slice(0, 70))}</span>
            <span class="mut" style="font-size:10px">${esc(fmtTime(m.occurred_at))}</span></div>`).join("")
      : `<div class="fdet-h">画像依据</div><div class="fnote">这个人还没有可用的记忆，画像只能凭长相像猜 —— 先让 bot 多和她聊几次</div>`);
}

let profProfileErr = "";

/** 「生成 / 重新生成人物画像」（LLM）：失败必须显示出来，不许静默 */
export async function hfProfileBuild(pid, btn, kind = "user", persona = "") {
  const key = String(pid);
  const isGroup = kind === "group";
  const personaId = persona || STATE.personaView || "";
  profProfileErr = "";
  const old = btn ? btn.textContent : "";
  if (btn) {
    btn.disabled = true;
    btn.textContent = isGroup ? "生成中…（读群记忆 + 成员，几秒）" : "生成中…（要读她的记忆再让模型总结，几秒）";
  }
  try {
    const r = await apiPostSafe("person/profile/build", { pid: key, kind, persona_id: personaId });
    if (!r || r.ok === false) profProfileErr = String((r && r.error) || "生成失败（模型没返回）");
  } catch (err) {
    profProfileErr = String((err && err.message) || err);
  }
  if (btn) {
    btn.disabled = false;
    btn.textContent = old || (isGroup ? "生成群聊画像" : "生成人物画像");
  }
  // 生成完必须让用户看得见结果：成功/失败都 toast，并把画像页两栏状态刷一遍
  // （以前点完毫无反馈，行里仍显示"还没生成"，用户以为没生效）
  if (profProfileErr) toast((isGroup ? "群聊画像生成失败：" : "人物画像生成失败：") + profProfileErr, "err");
  else toast(isGroup ? "群聊画像已生成" : "人物画像已生成", "ok");
  CARD_DET.html = await buildPersonDetail(String(key).replace(/^u/, ""), isGroup ? "group" : "user");
  repaintCard();
  if (typeof hfRerenderHook === "function") hfRerenderHook();
}

/** 人物详情（旧版：只有计数，保留给没有画像接口的场景兜底） */
function personDetailHtml(node, eng) {
  const raw = node.raw || node;
  const priv = Number(raw.private_msgs) || 0;
  const grp = Number(raw.group_msgs) || 0;
  const zone = String(raw.zone || "");
  const asList = (v) => (Array.isArray(v) ? v : []);
  const mems = (asList(raw.recent).length ? asList(raw.recent) : asList(raw.memories_list)).slice(0, 12);
  const memCount = Array.isArray(raw.memories) ? raw.memories.length : (raw.mem_count ?? raw.memories_total ?? raw.memories ?? 0);
  return `
    <div class="fdet-h">这个人</div>
    <div class="fdet-tx">同一个 QQ 在私聊与多个群里的记录已合并到这一行（群聊单独列，群号不是人）。</div>
    <div class="kv">QQ / 号<span>${esc(String(raw.user_id || raw.id || "—"))}</span></div>
    ${asList(raw.aliases).length ? `<div class="kv">别名<span>${esc(asList(raw.aliases).join("、"))}</span></div>` : ""}
    <div class="kv">私聊消息<span>${fmtNum(priv)}</span></div>
    <div class="kv">群聊消息<span>${fmtNum(grp)}</span></div>
    <div class="kv">关系区间<span>${esc(zone || "未结算")}</span></div>
    <div class="kv">记忆条数<span>${fmtNum(memCount)}</span></div>
    <div class="kv">最近互动<span>${fmtTime(raw.last_ts || raw.last_at)}</span></div>
    ${mems.length ? `<div class="fdet-h">相关记忆</div><div class="chips">${mems.map((x) => `<span class="chip">${esc(String((x && (x.summary || x.name)) || x).slice(0, 26))}</span>`).join("")}</div>` : ""}`;
}

/** 群的详情（群号不是人，单独一套） */


/** 「展开详细 / 收起详细」：在左侧卡片里原地展开 */
export async function hfCardDet(sid, id) {
  const key = String(sid) + ":" + canonId(sid, id);
  if (CARD_DET.key === key) {
    CARD_DET = { key: "", html: "", busy: false };
    repaintCard();
    return;
  }
  CARD_DET = { key, html: "", busy: true };
  repaintCard();
  let html = "";
  try {
    if (sid === "p") {
      const key = String(id);
      const items = (PERSON_DATA.items || []).concat((STATE.persons && STATE.persons.items) || []);
      const one = items.find((x) => String(x.id || x.qq || x.user_id) === key || "u" + String(x.user_id) === key);
      const grp = (PERSON_DATA.groups || []).find((x) => "g" + String(x.group_id || x.session_id) === key);
      if (one) {
        html = await buildPersonDetail(key);          // 画像优先（异步拉后端）
      } else if (grp) {
        html = await buildPersonDetail(String(grp.group_id), "group");    // 群也有画像（L1）
      } else {
        html = personDetailHtml({ id, raw: { id }, name: id }, LAST_CARD ? LAST_CARD.eng : null);
      }
    } else {
      const node = LAST_CARD && LAST_CARD.node ? LAST_CARD.node : { id };
      const eng = LAST_CARD ? LAST_CARD.eng : null;
      const it = eng ? (eng.nodes || []).find((x) => String(x.id) === String(id)) : null;
      // ⚠️ 引擎节点**没有 kind 字段**（只有 role），且现在的节点 id 是 e1207 / p:100000001 这种，
      // 既不匹配 /^u\d+$/、也取不到 kind → 原判断恒为 false，
      // 于是「人物」节点点「展开详细」会掉进「记忆」分支（用户报的「L0 显示不详细」）。
      const looksPerson = String((it && it.role) || (node && node.raw && node.raw.role) || "") === "person"
        || /^u\d+$/.test(String(id));
      if (looksPerson) {
        html = await buildPersonDetail(String(id));   // 人物节点 → 人物画像
      } else if (it && it.isEntity) {
        html = entDetailHtml(node, eng);
      } else {
        const m = (STATE.memories || []).find((x) => String(x.id) === String(id)) || { id, summary: node.name };
        const edges = await apiSafe("memory/edges", { id: Number(String(id).replace(/^m/, "")) }, { items: [] });
        html = await memDetailHtml(m, edges.items || edges.links || [], sid);   // 现在是 async：要 await，否则写进去的是 Promise
      }
    }
  } catch (err) {
    html = `<div class="mut">详情读取失败：${esc(String((err && err.message) || err))}</div>`;
  }
  CARD_DET = { key, html, busy: false };
  repaintCard();
  // 再补一次：卡片有异步重渲染（邻居/画像），同帧的点击偶尔会被吞掉，
  // 补这一次让"点开详细"对真用户也 100% 生效。
  setTimeout(repaintCard, 260);
}

/* ==================================================================
   图谱控制台的界面配置：改了自动存、刷新自动还原
   —— 面板跑在受限 iframe 里，localStorage 用不了，所以存到插件配置
      （hf_config.graph_ui，走通用 config / config/save 接口）
   ================================================================== */
const UI_PREF = { value: null, timer: 0, loading: null };

/** 读一次并缓存（全页共用一份） */
async function loadUiPref() {
  if (UI_PREF.value) return UI_PREF.value;
  if (!UI_PREF.loading) {
    UI_PREF.loading = apiSafe("config", {}, {}).then((cfg) => {
      const v = (cfg && cfg.graph_ui) || {};
      UI_PREF.value = typeof v === "object" && v ? v : {};
      // 布局从 12 种收到 3 种：旧配置里存的 cone/rings… 一律回球壳，别让控制台显示一个点不出来的名字
      if (UI_PREF.value.layout && !LAYOUT_DEFS.some((x) => x.key === UI_PREF.value.layout)) {
        UI_PREF.value.layout = "shell";
      }
      return UI_PREF.value;
    });
  }
  return UI_PREF.loading;
}

/** 每改一次就自动保存（去抖 600ms；失败只在控制台提示，不打扰） */
function saveUiPref(patch) {
  const cur = UI_PREF.value || (UI_PREF.value = {});
  Object.assign(cur, patch || {});
  clearTimeout(UI_PREF.timer);
  UI_PREF.timer = setTimeout(() => {
    apiPost("config/save", { patch: { graph_ui: cur } }).catch(() => {
      console.warn("[starheart] 控制台配置保存失败（下次改动会重试）");
    });
  }, 600);
}

/** 把保存的配置套到引擎与控件上 */
function applyUiPref(host, sid, eng, pref) {
  const p = pref || {};
  const q = (sel) => host.querySelector(sel);
  const on = (sel, val) => {
    const b = q(sel);
    if (b) b.classList.toggle("on", !!val);
  };
  if (p.layout && LAYOUT_DEFS.some((x) => x.key === p.layout)) eng.setLayout(p.layout, true);
  if (p.cam && CAM_DEFS.some((x) => x.key === p.cam)) eng.setCam(p.cam, true);
  // 色板已从控制台移除（用户定：线色按关系类型固定，「配色」这个开关没有意义了）。
  // 这里保留读取：沿用该面板上一次生效的那套色板，视觉不跳变。要换固定色板说一声即可。
  if (p.palette && PALETTE_DEFS.some((x) => x.key === p.palette)) eng.setPalette(p.palette);
  if (p.tier) eng.setTierMode(p.tier);      // 旧值"按连接度"由 setTierMode 内部归一到"按角色"
  if (p.params && typeof p.params === "object") {
    Object.keys(p.params).forEach((k) => {
      const v = Number(p.params[k]);
      if (!Number.isFinite(v)) return;
      eng.setPara(k, v);
      const el = q(`#${sid}P_${k}`);
      const out = q(`#${sid}V_${k}`);
      if (el) el.value = String(v);
      if (out) out.textContent = k === "cap" ? String(Math.round(v)) : v.toFixed(2);
    });
  }
  if (p.toggles && typeof p.toggles === "object") {
    Object.keys(p.toggles).forEach((k) => {
      eng.setToggle(k, !!p.toggles[k]);
      on(`[data-tgl="${k}"]`, p.toggles[k]);
    });
  }
  // 控件高亮同步
  host.querySelectorAll("[data-layout]").forEach((b) => b.classList.toggle("on", b.dataset.layout === eng.mode));
  host.querySelectorAll("[data-cam]").forEach((b) => b.classList.toggle("on", b.dataset.cam === eng.camKey));
  host.querySelectorAll("[data-tier]").forEach((b) => b.classList.toggle("on", b.dataset.tier === eng.tierMode));
  if (p.folded) {
    const pn = q(`#${sid}Panel`);
    const b = q('[data-act="toggle-panel"]');
    if (pn) pn.classList.add("folded");
    if (b) b.textContent = "展开";
  }
}

/* ==================================================================
   3D 图谱外壳（记忆图谱 / 记忆库 / 人物画像 三页共用）
   ================================================================== */
const SHELLS = new Map();
const GLASS = "background:color-mix(in srgb,var(--surface) 84%,transparent);border:1px solid var(--bd);border-radius:11px;backdrop-filter:blur(10px);z-index:3";

/** 铺满卡片的暗色舞台：画布 + 浮层（控制台 / 记忆点卡片 / 状态 / 图例 / 体检 / 备用视图） */
function shell3dHtml(sid, opts = {}) {
  const h = opts.height || "min(84vh,940px)";
  return `
  <style>${HF3D_CSS}</style>
  <div class="hf3d-ui card cp sec" id="${sid}Wrap" style="position:relative;height:${h};padding:0;overflow:hidden;background:#030713;border-color:rgba(70,150,240,.3)">
    <div id="${sid}Mount" style="position:absolute;inset:0"></div>
    ${opts.topLeft || ""}
    ${graphPanelHtml(sid, opts)}
    ${graphHudHtml(sid, opts)}
    <div class="fpanel fpanelAlt hide" id="${sid}Alt">${opts.altHtml || ""}</div>
    ${opts.health ? `<div class="fpanel fpanelH hide" id="${sid}Health"></div>` : ""}
  </div>`;
}

/**
 * 挂载/复用某个外壳的 3D 引擎（WebGL 优先，取不到上下文自动回落 canvas2D）
 * @param {{nodes,links}} data
 * @param {{layout?:string, cardHtml?:Function, onSelect?:Function, extra?:string}} opts
 */
/**
 * 解析「活着的」外壳节点。
 * 为什么必须现场解析：面板换范围/换视角会**连续重渲染**，闭包里捕获的 host / #gMount
 * 在下一轮就被换掉了（引用还在、但已脱离文档）。拿旧引用去 appendChild，画布就挂到了
 * 废节点上 —— 于是文档里一块 canvas 都没有，只能刷新页面。真机实测指纹：
 * `DOM=0X0(0)`（一块 canvas 都没有）、引擎 `glCanvas.isConnected === false`。
 */
function resolveShellNodes(sid, host) {
  const liveById = (id) => {
    try { const el = document.getElementById(id); return el && el.isConnected ? el : null; } catch (err) { return null; }
  };
  const liveWrap = liveById(sid + "Wrap");
  const liveHost = (liveWrap && liveWrap.parentElement) || (host && host.isConnected ? host : null) || liveWrap || host || null;
  let mount = null;
  if (liveHost && typeof liveHost.querySelector === "function") mount = liveHost.querySelector("#" + sid + "Mount");
  if (!mount || !mount.isConnected) mount = liveById(sid + "Mount");
  if (!mount && liveWrap) mount = liveWrap.querySelector('[id$="Mount"]');
  return { host: liveHost, mount: (mount && mount.isConnected) ? mount : null, wrap: liveWrap };
}

async function mountShell3d(host, sid, data, opts = {}) {
  // 一律挂到「文档里活着的」挂载点，而不是闭包里的旧引用（见 resolveShellNodes 注释）
  const live = resolveShellNodes(sid, host);
  if (live.host) host = live.host;
  const mountEl = live.mount || (host && host.querySelector ? host.querySelector("#" + sid + "Mount") : null);
  if (!mountEl) return null;
  let rec = SHELLS.get(sid);
  if (rec) {
    // ⚠️ 原实现只在 `rec.mount !== mountEl` 时搬运画布 —— 但换范围/换视角是**原地重渲染**
    //    （`#gMount` 这个节点没变、子节点被整个换掉），于是 rec.mount === mountEl、
    //    搬运分支被跳过、引擎复用，而**画布已经随旧子树被摘出文档** ⇒
    //    整个图谱一个 canvas 都不剩，引擎在 `_tickInner` 的 detached 分支里停循环，
    //    用户看到的就是「切到全库再切回当前视角，线没了/图没了，只能刷新页面」。
    //    真机实测指纹：BRK=DETACHED、E4=…/OFF/STOP/DETACHED、DOM=0X0(0)。
    // 所以判定条件改成**「引擎的画布是否还在当前挂载点里」**，而不是两个节点是不是同一个。
    // ⚠️ 2D 回退引擎（Graph3DCanvas）的字段是 `canvas`、没有 `glCanvas`：
    //    只认 glCanvas 会让它**每次重渲染都被判「不可复用」→ destroy + 重建**，
    //    反复消耗 WebGL 上下文预算（destroy 里还会 host.textContent="" + loseContext）。
    const cur = rec.g3 && (rec.g3.glCanvas || rec.g3.canvas);
    const inNow = !!(cur && mountEl.contains(cur));
    if (inNow) {
      rec.mount = mountEl;                                   // 正常：只更新挂载点引用
    } else {
      const cvs = rec.mount && rec.mount.querySelectorAll ? rec.mount.querySelectorAll("canvas") : [];
      if (cvs.length) {
        cvs.forEach((c) => mountEl.appendChild(c));           // 旧 mount 里还找得到 → 搬进来
        rec.mount = mountEl;
        rec.g3.setHost(mountEl);
      } else if (cur) {
        // 画布还在（只是被摘出文档）→ 直接挂回当前 mount：保留引擎与 GL 上下文，最省、无闪烁
        mountEl.appendChild(cur);
        if (rec.g3.fxCanvas && rec.g3.fxCanvas !== cur) mountEl.appendChild(rec.g3.fxCanvas);
        rec.mount = mountEl;
        rec.g3.setHost(mountEl);
        try { rec.g3.resize(); } catch (err) { /* 引擎未就绪 */ }
      } else {
        try { rec.g3.destroy(); } catch (err) { /* ignore */ }
        rec = null;
        SHELLS.delete(sid);
      }
    }
  }
  // ── 挂载后强制校验：画布必须真的在当前挂载点里、且连着文档，否则整个引擎重建 ──
  // 这一步是「无论面板怎么重渲染都不怕」的兜底：宁可丢上下文重建，也不能让图谱彻底消失。
  if (rec) {
    const c2 = rec.g3 && (rec.g3.glCanvas || rec.g3.canvas);
    const ok = !!(c2 && mountEl.contains(c2) && c2.isConnected);
    if (!ok) {
      try { rec.g3.destroy(); } catch (err) { /* ignore */ }
      SHELLS.delete(sid);
      rec = null;
    }
  }
  if (!rec) {
    let eng = null;
    try {
      eng = createGraph3D(mountEl, {});
    } catch (err) {
      console.warn("[starheart] 3D 引擎创建失败", err);
      return null;
    }
    rec = { g3: eng, mount: mountEl, opts: opts };
    SHELLS.set(sid, rec);
    const badge = host.querySelector("#" + sid + "Status");
    if (badge && eng.stats) badge.title = "渲染后端：" + (eng.stats().gpu || "canvas2D");
  }
  const eng = rec.g3;
  // 记下「该引擎现在应该持有的数据」：看门狗用它核对引擎有没有真的吃到本次数据
  rec.data = { nodes: (data && data.nodes) || [], links: (data && data.links) || [], limit: 3200, linkLimit: 26000 };
  rec.opts = opts;                 // 看门狗重挂时要复用同一套回调（否则范围按钮会变哑巴）
  eng.setData(rec.data);
  // 画布刚被挂回（或整页重渲染）时，引擎可能还停在「已卸载」的停循环状态 → 立刻唤醒一次
  try { if (eng.ensureAlive) eng.ensureAlive(); } catch (err) { /* 引擎未就绪 */ }
  if (opts.layout) eng.setLayout(opts.layout, true);
  wireGraphConsole(host, sid, eng, opts);
  // 控制台配置：刷新后还原上次的设置（存后端，每改一次自动写回）
  // 让「重画」在全局可达：窗口缩放/全屏/主题切换时任何地方都能触发重新量尺寸
  try { window.__hfGraphEngine = eng; } catch (err) { /* 非浏览器环境 */ }

  // ── 画布高度按「可用空间」实测设定（参考 upstream/astrbot_plugin_livingmemory 的 flex 填满思路）
  // 根因：引擎 resize() 只写画布**位图**尺寸（clientWidth/Height × dpr），容器高度却由 CSS 猜（原先 660px 封顶）→
  // 全屏时容器矮于可用区（下方一大块空白）、小窗时高于可用区（底部被裁）。所以这里直接量可用高并写死给容器。
  const fitGraphHeight = () => {
    // 幕布容器有两个：记忆图谱 #gWrap（外壳 sid="g"）、画像 #pWrap（sid="p"）。
    // 同一个模板生成，都是 .hf3d-ui 且 id 以 Wrap 结尾 → 按这个规律统一抓，新增外壳自动覆盖。
    const hosts = Array.from(document.querySelectorAll('.hf3d-ui[id$="Wrap"]'));
    if (!hosts.length) {
      const w = document.querySelector("#gWrap");
      if (w) hosts.push(w);
    }
    if (!hosts.length) return;
    const sc = document.querySelector("main") || document.documentElement;
    const scTop = sc.getBoundingClientRect().top;
    hosts.forEach((wrapEl) => {
      if (!wrapEl.isConnected) return;
      const r = wrapEl.getBoundingClientRect();
      // 隐藏中的外壳跳过：display:none 时 rect 全是 0，算出来是错的（等它显示时再算）
      if (!r.height && !wrapEl.clientHeight) return;
      const avail = sc.clientHeight - Math.max(0, r.top - scTop) - 26;
      const target = Math.max(360, Math.round(avail));
      if (Math.abs((parseFloat(wrapEl.style.height) || 0) - target) > 2) {
        wrapEl.style.height = target + "px";
        wrapEl.style.minHeight = "360px";
      }
    });
    try { eng.resize(); } catch (err) { /* 引擎未就绪 */ }
  };
  fitGraphHeight();
  // ── 渲染引擎防冻死看门狗（面板侧 setInterval，不依赖引擎自身的 rAF）──
  // 真机实测：切范围后引擎会出现「_running=true 但 _raf=0」——循环自称在跑、实际没排帧，
  // 自身永远醒不过来（画布停在旧帧或全黑），用户只能刷新页面。这里每秒用面板的时钟兜一次：
  // 补排帧 + 放行卡住的烘焙 + 核对数据是否真的喂进引擎（不一致就重喂）。
  if (typeof setInterval === "function") {
    const alive = async () => {
      if (alive._busy) return;                 // 上一次还没跑完（比如正在重建）→ 跳过本轮
      alive._busy = true;
      try {
        const liveNow = resolveShellNodes(sid, host);
        if (!liveNow.mount) {
          if (mountGraph._aliveTimer) { clearInterval(mountGraph._aliveTimer); mountGraph._aliveTimer = 0; }
          return;
        }
        if (liveNow.host) host = liveNow.host;
        const rec2 = SHELLS.get(sid);
        const e = rec2 && rec2.g3;
        // ⚠️ 重挂必须带上**原来的 opts**：空 opts 会让 wireGraphConsole 用空回调重新注册范围按钮，
        //    于是 onScope/cardHtml/onSelect 全丢 —— 之后点「全库/当前视角」只剩高亮、不会重渲染。
        const reOpts = (rec2 && rec2.opts) || {};
        if (!e) {
          // 连引擎都没了（重渲染把外壳整个换掉过）→ 用当前挂载点重新挂一次，宁可重建也要有图
          if (rec2 && rec2.data) { try { await mountShell3d(host, sid, rec2.data, reOpts); } catch (err) { /* 忽略 */ } }
          return;
        }
        if (typeof e.ensureAlive === "function") e.ensureAlive();
        // 画布不在文档里（原地重渲染把子节点换掉了）→ 强行重挂一次（限频 + 限次，避免每秒造一个新上下文）
        const cv = e.glCanvas || e.canvas;
        if (!cv || !cv.isConnected) {
          const tNow = Date.now();
          if (!alive._lastHeal || tNow - alive._lastHeal > 5000) {
            alive._lastHeal = tNow;
            alive._healCount = (alive._healCount || 0) + 1;
            if (alive._healCount <= 5) {
              try { await mountShell3d(host, sid, rec2.data || { nodes: [], links: [] }, reOpts); } catch (err) { /* 忽略 */ }
            }
          }
          return;
        }
        const want = rec2.data;
        if (want && typeof e.setData === "function" &&
            ((e.nodes || []).length !== want.nodes.length || (e.links || []).length !== want.links.length)) {
          e.setData(want);              // 引擎没吃到本次数据（切换后偶发）→ 重新喂一次
        }
      } catch (err) { /* 看门狗自身绝不能抛 */ } finally {
        alive._busy = false;
      }
    };
    if (mountGraph._aliveTimer) clearInterval(mountGraph._aliveTimer);
    mountGraph._aliveTimer = setInterval(alive, 1000);
    alive();
  }
  // 打开图谱时不要自动锁定某个节点：用户反馈「每次打开都锁着一条记忆，得点空白才退出」。
  // 引擎挂载过程中可能带出默认选中/聚焦，这里在布局稳定后再清一次；URL 明确要聚焦时不动。
  if (!/mem\//.test(location.hash || "")) {
    setTimeout(() => {
      try {
        if (typeof eng.selectNode === "function") eng.selectNode(-1);
        if (typeof eng.toggleFocus === "function") eng.toggleFocus("", false);
        else if (typeof eng.focusNode === "function") eng.focusNode(null);
      } catch (err) { /* 引擎版本差异，忽略 */ }
    }, 340);
  }
  if (typeof requestAnimationFrame === "function") requestAnimationFrame(fitGraphHeight);
  setTimeout(fitGraphHeight, 220);          // 等字体/布局稳定后再量一次
  if (!window.__hfGraphFitHooked) {
    window.__hfGraphFitHooked = true;
    let _ft = 0;
    const onRz = () => { clearTimeout(_ft); _ft = setTimeout(fitGraphHeight, 90); };
    window.addEventListener("resize", onRz);
    if (window.visualViewport && window.visualViewport.addEventListener) window.visualViewport.addEventListener("resize", onRz);
  }
  const pref = await loadUiPref();
  if (pref && Object.keys(pref).length) applyUiPref(host, sid, eng, pref);
  const card = host.querySelector("#" + sid + "Card");
  if (card) card.classList.remove("on");
  requestAnimationFrame(() => {
    try {
      eng.resize();

  // 画布自适应：原先只有「全屏」按钮会 resize，所以拖窗口/浏览器缩放后幕布不跟着变（用户反馈）。
  // 这里挂 ResizeObserver（量容器）+ window resize（量视口）双保险，节流到一帧内只跑一次。
  try {
    const _re = () => {
      if (STATE.__hfGResizeRaf) return;
      STATE.__hfGResizeRaf = (typeof requestAnimationFrame === "function" ? requestAnimationFrame : setTimeout)(() => {
        STATE.__hfGResizeRaf = 0;
        try { eng.resize(); } catch (err) { /* 引擎已销毁时忽略 */ }
      }, 16);
    };
    if (typeof ResizeObserver !== "undefined") {
      if (STATE.__hfGraphRO) { try { STATE.__hfGraphRO.disconnect(); } catch (e) {} }
      STATE.__hfGraphRO = new ResizeObserver(_re);
      const _wrapEl = document.querySelector("#gWrap") || host;   // wrap 不在本作用域，按 id 取（带 host 兜底）
      STATE.__hfGraphRO.observe(_wrapEl);
    }
    if (!STATE.__hfWinResizeHooked) {
      STATE.__hfWinResizeHooked = true;
      window.addEventListener("resize", () => { try { STATE.__hfGResize?.(); } catch (e) {} });
    }
    STATE.__hfGResize = _re;
  } catch (err) { /* 无 ResizeObserver 的老环境降级：至少全屏按钮仍可用 */ }
    } catch (err) { /* 容器还没尺寸 */ }
  });
  return eng;
}

/** 记忆点菜单（卡片）：名称 / 类型 / 层级 / 关系度 / 上级 / 邻居（可点跳转）+ 动作 */
function nodeCardHtml(host, sid, node, eng) {
  if (!node) return "";
  const it = (eng.nodes || []).find((x) => String(x.id) === String(node.id));
  const nb = [];
  (eng.links || []).forEach((l) => {
    if (l.a === it.idx || l.b === it.idx) {
      const other = (eng.nodes[l.a === it.idx ? l.b : l.a] || {});
      nb.push({ id: other.id, name: other.name || other.id, w: l.w });
    }
  });
  nb.sort((a, b) => b.w - a.w);
  const kindTag = it.role === "person"
    ? `<span class="tag" style="background:#22c55e1f;color:#22c55e">人物</span>`
    : it.role === "session"
      ? `<span class="tag" style="background:#f59e0b1f;color:#f59e0b">群 · 会话</span>`
      : it.isEntity
        ? `<span class="tag" style="background:#8b5cf61f;color:#8b5cf6">实体 / 话题</span>`
        : `<span class="tag" style="background:${atomColor(it.atom)}1f;color:${atomColor(it.atom)}">${esc(ATOM_LABEL[it.atom] || ATOM_LABEL_3D[it.atom] || it.atom || "记忆")}</span>`;
  const parent = it.parent >= 0 && eng.nodes[it.parent] ? eng.nodes[it.parent] : null;
  const raw = node.raw || node;
  const isMem = it.role === "memory" || (!it.role && !it.isEntity);
  // 「层级 / 上级」都要按**当前分档模式**判断，所以在模板外先算一次（别在 IIFE 里各算各的）。
  const tm = (LAST_CARD && LAST_CARD.eng && LAST_CARD.eng.tierMode) || "role";
  const rl = Number.isFinite(it.roleLevel) ? it.roleLevel : null;
  // 上级：it.parent 是**布局生成树**的父节点（下垂/星芒排布要用它），不是业务上的上级。
  //  · role 分档：最顶层节点的 parent 恒为 -1 → 原实现无条件渲染这行，就会显示「上级 —」
  //  · strength/time/type 分档：一个人节点的 parent 甚至可能是一条记忆，当「上级」显示是错的
  // 所以只在「按角色分层」且本节点确实还有更上一层时，才把它当上级渲染出来。
  const upper = tm === "role" && rl !== null && rl > 0 && parent ? parent : null;
  // 真号（QQ 号 / 群号）：后端节点上带了 ident。只给昵称是认不出是谁的，卡片必须显式给出。
  const ident = String((raw && raw.ident) || it.ident || "").trim();
  return `
    <h3>${esc(it.name)}</h3>
    <div class="row" style="gap:6px;margin-bottom:6px">
      ${kindTag}
      <span class="tag gy">${(() => {
        if (tm === "role") {
          const L = rl === null ? null : LEVEL_DEFS[rl];
          return `层 L${rl === null ? "—" : rl}${L ? " · " + esc(L.name) : ""}`;
        }
        if (tm === "strength") return `强度分档 ${esc(String(it.strength ?? "—"))}`;
        if (tm === "time") return `时间分档 ${it.ts ? fmtTime(it.ts) : "—"}`;
        return `类型 ${esc(String(it.atom || it.etype || "—"))}`;
      })()}</span>
    </div>
    <div class="kv">关系度<span>${fmtNum(it.deg)}</span></div>
    ${isMem ? `<div class="kv">记忆 ID<span class="mono" title="${esc(it.id)}">${esc(it.id)}</span></div>` : ""}
    ${upper ? `<div class="kv">上级<span title="${esc(upper.name)}">${esc(upper.name)}</span></div>` : ""}
    ${(it.role === "person" || it.role === "session") && ident
      ? `<div class="kv">${it.role === "person" ? "用户 ID" : "群号"}<span title="${esc(ident)}">${esc(ident)}</span></div>`
      : ""}
    ${isMem ? `<div class="kv">强度<span>${fmt1(it.strength)}</span></div>` : ""}
    ${it.ts ? `<div class="kv">时间<span>${fmtTime(it.ts)}</span></div>` : ""}
    ${isMem && raw && raw.session ? `<div class="kv">会话<span title="${esc(String(raw.session))}">${esc(sessLabel(raw.session))}</span></div>` : ""}
    <div class="bar"></div>
    <div class="mut" style="font-size:10.5px;margin-bottom:4px">邻居（${nb.length}）· 点一下跳过去</div>
    <div class="nb">${
      nb.length
        ? nb.slice(0, 30).map((x) => `<u data-goto="${esc(x.id)}"><em>→</em> ${esc(String(x.name).slice(0, 22))}</u>`).join("<br>")
        : "没有相邻节点"
    }</div>
    <div class="row" style="gap:6px;margin-top:10px;flex-wrap:wrap">
      <button class="btn xs p" data-det-btn onclick="hfCardDet(${jsStr(sid)}, ${jsStr(it.id)})">展开详细</button>
      ${isMem ? `<button class="btn xs" onclick="hfMemBoost(${jsStr(it.id)})">↑ 加权</button>
      <button class="btn xs" onclick="hfGraphOpen(${jsStr(it.id)})">在图谱里看</button>` : ""}
      <button class="btn xs${focusOn(sid === "p" ? "p" : "g", it.id) ? " on" : ""}" data-focus-btn onclick="${sid === "p" ? "hfPersonFocus" : "hfGraphFocus"}(${jsStr(it.id)})">${focusOn(sid === "p" ? "p" : "g", it.id) ? "取消居中" : "居中"}</button>
    </div>
    <div class="fdet"></div>`;
}

/** 框架的「重查当前页」入口（app.js 注入 window.hfRerender）。缺失时退化成强制路由重跑，绝不留白。 */
function hfRerenderHook() {
  if (typeof window !== "undefined" && typeof window.hfRerender === "function") {
    window.hfRerender();
    return;
  }
  if (typeof location !== "undefined") {
    const h = location.hash;
    location.hash = "#";
    location.hash = h;
  }
}

let LAST_FOCUS_ID = "";

// 人物画像的数据与人形引擎句柄。上一轮重写时它们的声明被误删，
// 导致 mountPersonsGraph 一进入就 ReferenceError → 整个人物画像页空白。
let PERSON_DATA = { items: [], groups: [] };
let PERSON3 = null;
let FOCUSED = { g: "", p: "" };

/** 离开图谱页时恢复中心轴（app.js 路由里调用） */
export function clearGraphFocus() {
  try {
    const g = typeof G3 !== "undefined" ? G3 : null;
    if (g && typeof g.unfocus === "function") g.unfocus();          // 还原轴心（幂等）
    else if (g && typeof g.toggleFocus === "function" && LAST_FOCUS_ID) g.toggleFocus(LAST_FOCUS_ID, false);
  } catch (err) { /* 恢复失败不该阻断路由 */ }
  FOCUSED = { g: "", p: "" };
  LAST_FOCUS_ID = "";
}

// ---------- 以下函数在上一轮重写时被误删，但 app.js / 其它视图仍在引用 ----------

/** 重建关系链（图谱工具栏） */
export async function hfGraphBuild(btn) {
  if (btn) { btn.disabled = true; btn.textContent = "重建中…"; }
  const res = await apiPostSafe("graph/build", {});
  const ok = res && res.ok !== false;
  toast(ok ? "关系链已重建" : "重建失败：" + ((res && res.error) || ""), ok ? "ok" : "err");
  hfRerenderHook();
}

/** ↑ 加权：直接抬强度（走 memory/update，真实写库） */
export async function hfMemBoost(id) {
  const raw = String(id).replace(/^m/, "");
  const full = await apiSafe("memory", { id: raw }, null);
  const cur = (full && (full.memory || full)) || {};
  const before = Number(cur.strength || 0.5);
  const after = Math.min(1, Math.round((before + 0.15) * 100) / 100);
  const res = await apiPostSafe("memory/update", { id: raw, fields: { strength: after } });
  const ok = res && res.ok !== false;
  toast(ok ? "强度 " + fmt1(before) + " → " + fmt1(after) : "加权失败：" + ((res && res.error) || ""), ok ? "ok" : "err");
  if (ok) hfRerenderHook();
}

/** ↓ 降权：把强度压到低位（「被制止的话术」「误记的内容」一键退场；救回用 ↑ 加权）
 *  2026-10-15 加：记忆 #882 这类「别重复发」的旧话术被主动开口当指令演绎，需要个一键降权阀 */
export async function hfMemDemote(id) {
  const raw = String(id).replace(/^m/, "");
  const full = await apiSafe("memory", { id: raw }, null);
  const cur = (full && (full.memory || full)) || {};
  const before = Number(cur.strength || 0.5);
  const after = Math.max(0.03, Math.round(before * 0.15 * 100) / 100);
  const res = await apiPostSafe("memory/update", { id: raw, fields: { strength: after } });
  const ok = res && res.ok !== false;
  toast(ok ? "已降权：" + fmt1(before) + " → " + fmt1(after) + "（救回用 ↑ 加权）" : "降权失败：" + ((res && res.error) || ""), ok ? "ok" : "err");
  if (ok) hfRerenderHook();
}

/** 别再提 / 恢复提及：muted 只影响召回与主动开口，不进注入池；记忆本身保留在库里 */
export async function hfMemMute(mid, muted) {
  const raw = String(mid).replace(/^m/, "");
  const on = Number(muted) === 1;
  const res = await apiPostSafe("memory/mute", {
    id: raw,
    muted: on,
    source_mode: on ? "test" : "",
  });
  const ok = res && res.ok !== false;
  toast(ok ? (on ? "已冷处理：不再进召回与主动开口" : "已恢复：重新参与召回") : "操作失败：" + ((res && res.error) || ""), ok ? "ok" : "err");
  if (ok) hfRerenderHook();
}

/** 老入口：打开某条记忆的详细（等价 hfMemDetail，保留兼容） */
export async function hfMemOpen(id, sid) {
  return hfCardDet(sid || (String(id).startsWith("m") ? "g" : "p"), String(id));
}

/** 打开某条记忆/人物的详细 */
export async function hfMemDetail(id) {
  return hfCardDet(String(id).startsWith("m") ? "g" : "p", String(id));
}

/** 运行衰减 / 淘汰 */
export async function hfMemMaintain(btn) {
  if (btn) { btn.disabled = true; btn.textContent = "运行中…"; }
  const res = await apiPostSafe("memory/maintenance", {});
  const ok = res && res.ok !== false;
  toast(ok ? "衰减 / 淘汰已跑完" : "运行失败：" + ((res && res.error) || ""), ok ? "ok" : "err");
  hfRerenderHook();
}

/** 清理已淘汰记忆 */
export async function hfPurgeStale(btn) {
  if (btn) { btn.disabled = true; btn.textContent = "清理中…"; }
  const res = await apiPostSafe("memory/purge_stale", {});
  const d = (res && res.data) || {};          // apiPostSafe 统一包成 {ok,data}，字段在 data 里（以前读顶层恒 undefined）
  const ok = res && res.ok !== false;
  toast(ok ? "已清理 " + fmtNum(d.removed || 0) + " 条" : "清理失败", ok ? "ok" : "err");
  hfRerenderHook();
}

/** 关键词过滤（DOM 级，不重渲染 → 不丢滚动位置） */
export function hfMemFilter(v) {
  const q = String(v || "").trim().toLowerCase();
  let hit = 0;
  document.querySelectorAll(".mcard, .fcard").forEach((el) => {
    const t = (el.textContent || "").toLowerCase();
    const on = !q || t.indexOf(q) >= 0;
    el.style.display = on ? "" : "none";
    if (on) hit += 1;
  });
  const tip = document.getElementById("mbHit");
  if (tip) tip.textContent = q ? "匹配 " + hit + " 条" : "";
}

/** 复制文本（宿主 iframe 里 clipboard API 常被拒 → 退回 textarea） */
export function hfCopyText(text, btn) {
  const t = String(text == null ? "" : text);
  const done = () => {
    if (btn) {
      const old = btn.textContent;
      btn.textContent = "已复制";
      setTimeout(() => { btn.textContent = old; }, 1200);
    }
    toast("已复制", "ok");
  };
  const fallback = () => {
    const ta = document.createElement("textarea");
    ta.value = t;
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    let ok = false;
    try { ok = document.execCommand("copy"); } catch (err) { ok = false; }
    document.body.removeChild(ta);
    if (ok) done();
    else toast("复制失败：宿主限制，请手动选中", "err");
  };
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(t).then(done, fallback);
      return;
    }
  } catch (err) { /* 落到兜底 */ }
  fallback();
}

/** 人物卡「居中」（之前只有 onclick、没有函数 → 点了必报 hfPersonFocus is not defined） */
export function hfPersonFocus(id) {
  const key = canonId("p", id);
  const on = focusOn("p", key);
  LAST_FOCUS_ID = key;
  try {
    if (typeof PERSON3 !== "undefined" && PERSON3 && PERSON3.toggleFocus) PERSON3.toggleFocus(key, !on);
  } catch (err) { /* 引擎没就绪就算了 */ }
  hfRerenderHook();
}

/** 引擎调试句柄（app.js / 验收台用） */
/** 图谱引擎句柄（自检用） */
/** 当前图谱查看范围 view/all（自检用） */
export function graphScope() {
  return G_SCOPE;
}

export function graphEngine() {
  return typeof G3 !== "undefined" ? G3 : null;
}

export function __bankEngine3d() {
  const rec = SHELLS.get("b");
  return rec ? rec.g3 : null;
}

export function __personEngine3d() {
  return typeof PERSON3 !== "undefined" ? PERSON3 : null;
}

/** 配置域保存（views-system.js 旧入口仍在用） */
export async function renderGraph() {
  const gp = { limit: 3200 };
  if (G_SCOPE === "all") gp.persona_id = "";
  return {
    html: `${pageHead(
      "记忆图谱",
      "3D 星空拓扑（WebGL）：3 种布局 × 4 镜头。拖拽旋转 · 滚轮缩放 · 悬停高亮邻居 · 单击锁定 · 双击聚焦 · 拖单点可单独挪",
      `
       <button class="btn s" onclick="hfGraphFit()">适应视角</button>
       
       <button class="btn s" onclick="hfGraphExport()">⬇ 导出 PNG</button>`,
      "◍"
    )}${shell3dHtml("g", { scope: G_SCOPE, health: true })}`,
    mount: (host) => mountGraph(host, gp),
  };
}

async function mountGraph(host, gp) {
  // 现场解析「活着的外壳容器」：换范围/换视角时面板会连续重渲染，传进来的 host 可能已经脱离文档，
  // 此时往里写 HUD/卡片/挂画布，用户都看不到（真机实测：文档里一块 canvas 都没有）。
  try {
    const lv = resolveShellNodes("g", host);
    if (lv.host) host = lv.host;
  } catch (err) { /* 忽略 */ }
  const sp = {};
  if (G_SCOPE === "all") sp.persona_id = "";
  // ⚠️ 拉数据失败**绝不能让整个挂载中止**：原实现直接 `await Promise.all([...])`，一旦 reject，
  //    mountShell3d（建画布）与自愈看门狗都不会被安装 —— 表现就是「外壳在、文档里一块 canvas 都没有、
  //    只能刷新页面」（子智能体独立复核确认过的路径）。这里改成：失败也把外壳挂起来，
  //    并按「零假数据」红线把错误写进可见位置，绝不静默留白。
  let payload = null, stats = null, fetchErr = null;
  try {
    const both = await Promise.all([api("graph", gp), apiSafe("graph/stats", sp)]);
    payload = both[0]; stats = both[1];
  } catch (err) {
    fetchErr = (err && err.message) || String(err);
  }
  if (fetchErr) {
    try {
      const engErr = await mountShell3d(host, "g", { nodes: [], links: [] }, {});
      const hint = host.querySelector("#gHint");
      if (hint) hint.textContent = "⚠ 图谱数据读取失败：" + String(fetchErr).slice(0, 90) + "（点「复位视图」或刷新重试）";
      return engErr;
    } catch (err2) { /* 连空外壳都挂不上：交给上层错误卡 */ }
  }
  STATE.graph = payload;
  STATE.graphStats = stats;
  const nodes = (payload && payload.nodes) || [];
  const links = (payload && payload.links) || [];
  // 顶部一排：库级真数据（记忆总数/实体/关系边/平均度数/孤立点/连通分量）
  fillTopMetrics(host, "g", [
    ["记忆总数", (stats && stats.memories) != null ? fmtNum(stats.memories) : "—"],
    ["实体 / 话题", (stats && stats.entities) != null ? fmtNum(stats.entities) : "—"],
    // ⚠️ 标签必须与图谱面板那个数字区分开：这里是**落库**边（hf_edges 的 entity/temporal/semantic），
    //    不包含读时合成的 about / in_session / member_of。两个数字同名会让用户以为哪里算错了。
    ["落库关系边", (stats && stats.edges) != null ? fmtNum(stats.edges) : "—"],
    ["平均度数", stats && stats.avg_degree != null ? fmt1(stats.avg_degree) : "—"],
    ["孤立点", (stats && stats.isolated) != null ? fmtNum(stats.isolated) : "—", (stats && stats.isolated) > 0 ? "hot" : ""],
    ["连通分量", (stats && stats.components) != null ? fmtNum(stats.components) : "—"],
  ]);
  const eng = await mountShell3d(
    host,
    "g",
    { nodes, links },
    {
      onScope: (v) => {
        G_SCOPE = v;
        hfRerenderHook();
      },
      cardHtml: (n, e) => nodeCardHtml(host, "g", n, e),
      onSelect: (n) => {
        const hint = host.querySelector("#gHint");
        if (hint && n) hint.textContent = "已选中：" + String(n.label || n.id || "");
      },
    }
  );
  G3 = eng;
  G3_SHELL = host;
  G3_MOUNT = host.querySelector("#gMount");
  try {
    renderGraphHealth(host, payload, stats);
  } catch (err) {
    console.warn("[starheart] 关系链体检渲染失败", err);
  }
  const first = nodes.find((n) => String(n.group) === "memory") || nodes[0];
  if (first && eng) {
    // ⚠️ 这里**不能替用户选中节点**（原来写的是 `eng.selected = it.idx`）。
    // 选中会让引擎 _alphas() 把**所有非邻接线**压到 0.036 —— 只有正常基线
    // （0.22×0.36=0.0792）的 45%，也就是「线基本看不见」。
    // 为什么只在小图上暴露：大图上万条边即使每条都极淡，重叠起来仍像一张网；
    // 小图（几百条边）压到 0.036 就直接等于「连线消失」。
    // 用户报的「切到全库再切回当前视角，当前视角就没有连线了」正是这个 ——
    // 每次渲染都替他选中了第一个记忆节点，而大图的观感把小图的缺失衬托得很明显。
    // 现在的口径：**卡片照旧画**（信息不缺），但"选中/聚焦"这件事只由用户点击决定。
    if (typeof eng._alphas === "function") eng._alphas();
    if (typeof eng._prepareDraw === "function") eng._prepareDraw();
    paintCard(host, "g", first, eng);
    eng.emit("select", first);
  }
}

function renderGraphHealth(host, payload, stats) {
  const box = host.querySelector("#gHealth");
  if (!box) return;
  box.classList.add("fpanelH-in");
  const nodes = (payload && payload.nodes) || [];
  const links = (payload && payload.links) || [];
  if (!nodes.length) {
    box.innerHTML = emptyBlock("还没有关系链 · 点右上角「重建关系链」", "⚠");
    return;
  }
  const etypes = new Map();
  const deg = new Map();
  links.forEach((l) => {
    const t = String(l.type || l.etype || "相关");
    etypes.set(t, (etypes.get(t) || 0) + 1);
    const a = String(l.source ?? l.s ?? "");
    const b = String(l.target ?? l.t ?? "");
    if (a) deg.set(a, (deg.get(a) || 0) + 1);
    if (b) deg.set(b, (deg.get(b) || 0) + 1);
  });
  const orphan = nodes.filter((n) => !deg.get(String(n.id))).length;
  const avg = (links.length * 2) / Math.max(1, nodes.length);
  const hubs = nodes
    .map((n) => ({ n, d: deg.get(String(n.id)) || 0 }))
    .sort((a, b) => b.d - a.d)
    .slice(0, 7);
  const eColors = ["#3c96ca", "#03c9d7", "#8b5cf6", "#f59e0b", "#00c853", "#e0559a"];
  const items = Array.from(etypes.entries()).sort((a, b) => b[1] - a[1]).map(([k, v], i) => ({
    label: LINK_LABEL[k] || k,
    value: v,
    color: eColors[i % eColors.length],
  }));
  box.innerHTML = `
  ${kpis(
    kpiCard("ghNodes", fmtNum(nodes.length), "节点")
    + kpiCard("ghLinks", fmtNum(links.length), "绘制关系")
    + kpiCard("ghAvg", avg.toFixed(2), "平均度数")
    + kpiCard("ghOrphan", fmtNum(orphan), "孤立点")
  )}
  <div class="grid g23 sec">
    ${card(`<div id="ghEdgeChart"></div>`, { title: "关系类型构成", sub: "边按类型统计；颜色说明在图下方" })}
    ${card(`<div id="ghHubs"></div>`, { title: "枢纽节点", sub: "度最高 = 关系最密的记忆 / 实体" })}
  </div>`;
  mount(host, "#ghEdgeChart", (el) =>
    C.barSplit(el, {
      items,   // 无边就渲染空占位（零假数据红线：不伪造「相关=1」）
      height: 18,
    })
  );
  mount(host, "#ghHubs", (el) =>
    C.bars(el, {
      items: hubs.map((h) => ({ label: String(h.n.label || h.n.id || "").slice(0, 26), value: h.d, sub: "度数" })),
    })
  );
}

function bindPhysics(host) {
  const map = [
    ["#gRep", "#gRepV", "repulsion"],
    ["#gLink", "#gLinkV", "linkForce"],
    ["#gCen", "#gCenV", "centerForce"],
    ["#gDist", "#gDistV", "linkDistance"],
  ];
  map.forEach(([sel, lab, key]) => {
    const el = host.querySelector(sel);
    const out = host.querySelector(lab);
    if (!el) return;
    const push = () => {
      if (out) out.textContent = el.value;
      const patch = {};
      patch[key] = Number(el.value);
      if (G3) G3.setPhysics(patch);
    };
    el.addEventListener("input", push);
    el.addEventListener("change", push);
  });
}

function renderGraphDetail(host, node) {
  const el = host.querySelector("#gDetail");
  if (!el) return;
  const payload = STATE.graph || { nodes: [], links: [] };
  if (!node) {
    const mem = payload.nodes.filter((n) => n.group === "memory");
    const ent = payload.nodes.filter((n) => n.group !== "memory");
    el.innerHTML = `<h3>图谱概览</h3><div class="cs">点任意节点看它的完整档案与关系链；悬停会高亮它的邻域</div>
      ${["记忆 " + fmtNum(mem.length), "实体 " + fmtNum(ent.length), "关系 " + fmtNum(payload.links.length)]
        .map((t) => `<span class="chip">${esc(t)}</span>`)
        .join(" ")}
      <div class="col" style="gap:7px;margin-top:12px">
        <div class="mut" style="font-size:11px">怎么看这张图</div>
        <div class="mut" style="font-size:11.5px;line-height:1.7">
          · <b>拖单个点</b>：只有它跟着鼠标走，其余节点被物理牵引着避让<br>
          · <b>双击点</b>：解除钉住，让它回到力场<br>
          · <b>滚轮</b>：以鼠标为锚点缩放；<b>拖空白</b>：平移画布<br>
          · 放大到 1.15× 以上，记忆会变成卡片；悬停卡片会展开显示细节
        </div>
      </div>`;
    return;
  }
  const links = payload.links.filter((l) => l.s === node.id || l.t === node.id);
  const byId = {};
  payload.nodes.forEach((n) => (byId[n.id] = n));
  const isMem = node.group === "memory";
  const pairs = [];
  if (isMem) {
    pairs.push([
      "强度",
      `<span class="row" style="gap:6px"><span class="bar" style="width:80px"><span class="bf" style="display:block;width:${Math.round((node.strength || 0) * 100)}%"></span></span><span class="mono">${(node.strength || 0).toFixed(2)}</span></span>`,
    ]);
    pairs.push(["情绪浓度", `<span class="mono">${node.poignancy ?? "—"}/10</span>`]);
    pairs.push(["类型", `<span class="tag" style="background:${atomColor(node.kind)}22;color:${atomColor(node.kind)}">${ATOM_LABEL[node.kind] || node.kind || "—"}</span>`]);
    pairs.push(["发生时间", `<span class="mono">${fmtTime(node.occurred)}</span>`]);
    pairs.push(["被召回", `<span class="mono">${fmtNum(node.recalls)} 次</span>`]);
    pairs.push(["来源", esc(node.source || "—")]);
    // 记忆 ID 必须显式给出来：用户在记忆库里就是拿 `m865` 这种号去搜的，
    // 图谱里看到了却不知道号、还得回列表翻，属于「详情不完整」。
    pairs.push(["记忆 ID", `<span class="mono" title="${esc(String(node.id || ""))}">${esc(String(node.id || "—"))}</span>`]);
    pairs.push(["会话", `<span title="${esc(String(node.session || ""))}">${esc(node.session ? sessLabel(node.session) : "—")}</span>`]);
    pairs.push(["图谱度数", `<span class="mono">${node.degree || 0}</span>`]);
  } else {
    pairs.push(["类型", `<span class="tag info">${esc(String(GROUP_LABEL[node.group] || node.group || "—"))}</span>`]);
    // 真号（用户 ID / 群号）：只给昵称是认不出是谁的。后端节点已带 ident，
    // 原实现这一栏完全没有 → 用户报的「L0 显示不详细，用户 ID 没显示」。
    const _entIdent = String(node.ident || "").trim();
    if (_entIdent) {
      const _lab = node.group === "person" ? "用户 ID" : node.group === "session" ? "群号" : "编号";
      pairs.push([_lab, `<span class="mono" title="${esc(_entIdent)}">${esc(_entIdent)}</span>`]);
    }
    pairs.push(["提及次数", `<span class="mono">${fmtNum(node.weight)}</span>`]);
    pairs.push(["最近出现", `<span class="mono">${timeAgo(node.last)}</span>`]);
    pairs.push(["图谱度数", `<span class="mono">${node.degree || 0}</span>`]);
  }
  const nb = links.map((l) => {
    const other = byId[l.s === node.id ? l.t : l.s];
    return { label: (other && other.label) || "?", type: l.type, w: l.w, other };
  });
  el.innerHTML = `<div class="row bt" style="margin-bottom:5px"><h3 style="margin:0">${esc(node.label)}</h3>
      <button class="btn xs" onclick="hfGraphSelect(null)">✕</button></div>
    <div class="row" style="gap:5px;flex-wrap:wrap;margin-bottom:8px">
      <span class="tag gy">${esc(GROUP_LABEL[node.group] || node.group)}</span>
      ${isMem ? `<span class="tag" style="background:${atomColor(node.kind)}22;color:${atomColor(node.kind)}">${ATOM_LABEL[node.kind] || node.kind}</span>` : ""}
      ${node.degree ? `<span class="tag ok">${node.degree} 条关系</span>` : `<span class="tag warn">孤点</span>`}
    </div>
    ${pairs.map(([k, v]) => `<div class="dkv"><div class="k">${esc(k)}</div><div class="v">${v}</div></div>`).join("")}
    ${isMem && (node.entities || []).length ? `<div style="margin:10px 0 5px" class="mut"><span style="font-size:11px">实体</span></div><div class="chips">${node.entities.map((t) => `<span class="chip">${esc(t)}</span>`).join("")}</div>` : ""}
    ${isMem && (node.cues || []).length ? `<div style="margin:10px 0 5px" class="mut"><span style="font-size:11px">召回线索</span></div><div class="chips">${node.cues.map((t) => `<span class="chip hot">${esc(t)}</span>`).join("")}</div>` : ""}
    ${isMem && node.evidence ? `<div style="margin:10px 0 5px" class="mut"><span style="font-size:11px">证据引文</span></div>
      <div class="chip" style="white-space:normal;line-height:1.6">${esc(node.evidence)}</div>` : ""}
    <div style="margin:12px 0 5px" class="mut"><span style="font-size:11px">关系链（${nb.length}）</span></div>
    ${nb.length ? `<div class="col" style="gap:6px">${nb
        .map(
          (x) => `<div class="row" style="gap:7px;font-size:11.5px;cursor:pointer" onclick="hfGraphFocus(${jsStr((x.other || {}).id || "")})" title="点一下跳到这个节点">
            <i style="width:10px;height:3px;border-radius:2px;background:${GRAPH_PALETTE.link[x.type] || "#888"};flex-shrink:0"></i>
            <span class="mut nowrap">${LINK_LABEL[x.type] || x.type}</span>
            <span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(x.label)}</span>
            <span class="mono mut" style="margin-left:auto">${fmt1(x.w)}</span></div>`
        )
        .join("")}</div>`
      : emptyBlock("这条记忆还没有关系链 · 试试右上角「重建关系链」", "⚠")}
    ${isMem ? `<div class="row" style="margin-top:14px;gap:6px;flex-wrap:wrap">
      <button class="btn xs" onclick="hfMemOpen(${jsStr(node.id)})">在记忆库里打开</button>
      <button class="btn xs" onclick="hfCopyText(${jsStr(node.label)})">复制摘要</button></div>` : ""}`;
}

/* ==================================================================
   记忆库（管理表 + 召回测试）
   ================================================================== */
let memFilter = { src: "", q: "", sort: "strength" };

const STATUS_LABEL = { active: "活跃", stale: "待复活", decayed: "已衰减", archived: "已归档", deleted: "已删除（回收站）", forgotten: "已遗忘" };

/** 会话 id 的可读形式：testbot:FriendMessage:100000000 → 私聊 · 100000000（完整 id 放 title，不再被列宽裁掉） */
function sessLabel(sid) {
  const t = String(sid || "");
  // 平台前缀 + 消息类型 + 会话 id，各平台命名不一（FriendMessage / PrivateMessage / GroupMessage …）
  const m = t.match(/^(.*?):([A-Za-z]+Message|FriendMessage|GroupMessage):(.+)$/);
  if (!m) return t || "—";
  const kind = /group/i.test(m[2]) ? "群聊" : /friend|private|direct|personal/i.test(m[2]) ? "私聊" : m[2];
  return kind + " · " + m[3];
}

let MB = { q: "", session_id: "", persona_id: "", status: "", atom_type: "", source: "", sort: "recent", limit: 20, offset: 0, open: 0, recall: null, recall_q: "", recall_topk: 8 };
let MB_DATA = { items: [], total: 0 };
let MB_SESS = { items: [], msg: {} };
let MB_COV = {};
let MB_GENS = [];

/** 记忆库 = 记忆管理表（和「记忆图谱」是两张不同的东西：一个是管理，一个是关系网络）。 */
export async function renderMemoryList() {
  const [res, sess, cov, vg, lc] = await Promise.all([
    apiSafe("memory/search", { q: MB.q, session_id: MB.session_id, persona_id: MB.persona_id, status: MB.status, atom_type: MB.atom_type, source: MB.source, sort: MB.sort, limit: MB.limit, offset: MB.offset }, { items: [], total: 0 }),
    apiSafe("memory/sessions", {}, { items: [], msg: {} }),
    apiSafe("embed/coverage", {}, {}),
    apiSafe("vector/gens", {}, { items: [] }),
    // 记忆生命周期（接口早就有，之前前端 0 调用）：活跃 / 已归档 / 已遗忘
    apiSafe("memory/lifecycle", {}, {}),
  ]);
  MB_DATA = { items: res.items || [], total: res.total || 0 };
  MB_SESS = sess;
  MB_COV = cov || {};
  MB_GENS = (vg && vg.items) || [];
  STATE.memories = (res.items || []).map((m) => ({ id: "m" + m.id, raw: m }));
  const msg = sess.msg || {};
  const st = cov.status || {};
  // 展开状态改由 DOM 维护（见 hfMemOpenRow），这里不再重复渲染，避免整页刷新
  const open = null;
  const openHtml = "";

  const opt = (v, label, cur) => `<option value="${esc(v)}"${cur === v ? " selected" : ""}>${esc(label)}</option>`;
  const pages = Math.max(1, Math.ceil(MB_DATA.total / MB.limit));
  const page = Math.floor(MB.offset / MB.limit) + 1;

  // 记忆生命周期卡：三个数全部来自 /page/memory/lifecycle 的 counts（按 status 分组）。
  // 分组里缺某个状态键 = 库里就是 0 条（GROUP BY 语义）—— 如实显示 0；
  // 只有 counts 整块没返回、或接口报错，才显示「— / 暂不可用」，绝不拿 0 顶替未知。
  const countsOk = !!(lc && !lc.__err && lc.counts && typeof lc.counts === "object");
  const lifeNum = (k) => {
    const c = countsOk ? lc.counts[k] : null;
    if (c && c.total != null) return fmtNum(c.total);
    if (c) return "—";                    // 有键却没给数：不猜
    return countsOk ? "0" : "—";          // 缺键 = 该状态就是 0 条
  };
  const lifeNote = (() => {
    if (!lc || lc.__err) return "";
    const parts = [];
    if (countsOk && !Object.keys(lc.counts).length) parts.push("当前库里还没有记忆 —— 三个状态都是 0 条");
    if (lc.enabled === false) parts.push("自动遗忘当前<b>关闭</b>（不会自动归档 / 遗忘）");
    else if (lc.enabled === true) parts.push("自动遗忘在跑");
    const a = Number(lc.archive_after_days);
    const f = Number(lc.forget_after_days);
    if (Number.isFinite(a) && Number.isFinite(f)) {
      parts.push(`超过 <b>${fmtNum(a)}</b> 天没被用到的记忆转「归档」，归档再闲置 <b>${fmtNum(f)}</b> 天且强度低的才会「遗忘」；两种状态被提及都会自动拉回`);
    }
    return parts.join(" · ");
  })();
  const lifeCard = lc && lc.__err
    ? card(`<div class="mut" style="font-size:12px;line-height:1.8">记忆生命周期接口读取失败：<b>${esc(lc.__err)}</b><br>
        这是展示问题，不影响记忆数据本身；点顶栏「⟳」可重试。</div>`,
        { title: "记忆生命周期", sub: "活跃 / 已归档 / 已遗忘（自动归档与遗忘的现状）" })
    : countsOk
      ? card(
          `<div class="row wrap" style="gap:9px">${[
            ["活跃", lifeNum("active"), "参与召回"],
            ["已归档", lifeNum("archived"), "不参与召回 · 被提及会拉回"],
            ["已遗忘", lifeNum("forgotten"), "软删 · 库里仍可查"],
          ].map(([l, v, d]) => `<div class="kpi-mini" style="min-width:150px;flex:1"><div class="v">${v}</div>
            <div class="l">${esc(l)} · ${esc(d)}</div></div>`).join("")}</div>
          ${lifeNote ? `<div class="mut" style="font-size:11px;margin-top:9px;line-height:1.8">${lifeNote}</div>` : ""}`,
          { title: "记忆生命周期", sub: "自动归档 / 遗忘的现状（阈值与开关在「记忆设置」里改）" })
      : card(`<div class="mut" style="font-size:12px;line-height:1.8">生命周期计数暂不可用：接口没有返回 <span class="mono">counts</span>（这里不显示 0 顶替）。</div>`,
          { title: "记忆生命周期", sub: "活跃 / 已归档 / 已遗忘（自动归档与遗忘的现状）" });

  const html = `
  <style>
    .mbrow { cursor: pointer; }
    .mbrow:hover td { background: var(--surface2); }
    .mbrow.on td { background: color-mix(in srgb, var(--primary) 10%, transparent); }
    .mbt { width: 100%; border-collapse: collapse; font-size: 12px; }
    .mbt th { text-align: left; font-weight: 600; font-size: 11px; color: var(--tx2);
              padding: 7px 8px; border-bottom: 1px solid var(--bd); white-space: nowrap; }
    .mbt td { padding: 8px; border-bottom: 1px solid var(--bd); vertical-align: top; }
    .mb-sum { line-height: 1.6; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
    .mb-bar { height: 5px; border-radius: 3px; background: var(--surface2); width: 54px; overflow: hidden; display: inline-block; vertical-align: middle; }
    .mb-bar i { display: block; height: 100%; background: linear-gradient(90deg, var(--primary), #54a0ff); }
    .mb-det td { background: var(--surface2); padding: 0; }
    .mb-det .fdet { padding: 12px 14px; }
    .mb-rc { border: 1px solid var(--bd); border-radius: 9px; padding: 8px 10px; margin-bottom: 6px; cursor: pointer; }
    .mb-rc:hover { border-color: var(--primary); }
    .mb-sel { width: 14px; height: 14px; }
    select.ipt, .ipt { background: var(--surface2) !important; color: var(--tx) !important;
                         border: 1px solid var(--bd) !important; border-radius: 7px; }
    select.ipt option { background: var(--surface); color: var(--tx); }
    .btn[disabled] { opacity: .75; }
    .mb-pager { display: flex; align-items: center; gap: 8px; justify-content: flex-end; padding: 10px 2px; font-size: 12px; }
  </style>
  ${pageHead(
    "记忆库 · 记忆管理",
    "找得到 / 看得全 / 改得动：关键词（含记忆 ID）、会话、状态、类型、来源筛选，可排序分页；点任意一行原地展开详细（原始对话 + 来源轮次 + ID + 向量）",
    `<button class="btn p" onclick="hfRecallToggle()">${MB.recall ? "收起召回测试" : "召回测试"}</button>`,
    "❑"
  )}
  ${lifeCard}
  ${kpis(
    kpiCard("mbAll", fmtNum(MB_DATA.total), "当前筛选命中", `<span class="mut">共 ${fmtNum(st.active || 0)} 活跃 / ${fmtNum(cov.total || 0)} 条</span>`)
    + kpiCard("mbVec", `${fmtNum(cov.embedded || 0)}<span class="mut" style="font-size:12px">/${fmtNum(cov.total || 0)}</span>`, "已生成向量",
      `<span class="mut">${(cov.missing_recallable ?? cov.missing) ? `还缺 ${fmtNum(cov.missing_recallable ?? cov.missing)}${cov.model ? " · " + esc(String(cov.model)) : ""}` : (cov.missing ? `覆盖完整（另有 ${fmtNum(cov.missing)} 条归档不参与召回）` : "覆盖完整")}</span>`)
    + kpiCard("mbMsg", fmtNum(msg.messages), "原始对话", `<span class="mut">${fmtNum(msg.sessions)} 个会话${msg.linked_turns ? ` · ${fmtNum(msg.linked_turns)} 条来源轮次` : ""}</span>`)
    + kpiCard("mbSt", `${fmtNum(st.active || 0)}<span class="mut" style="font-size:12px">/${fmtNum((st.stale || 0) + (st.decayed || 0))}</span>`, "活跃/待复活",
      `<span class="mut">归档 ${fmtNum(st.archived || 0)} 不参与召回</span>`)
  )}

  ${MB_GENS.length ? `
  <div class="card sec" style="padding:10px 13px">
    <div class="row bt" style="gap:9px">
      <div style="min-width:0">
        <div class="pn">向量库（多代）</div>
        <div class="pd">当前用哪一代 = 当前模型的维度。换模型不会删除旧向量（旧代暂停），新代建好再切换；换回来立刻可用，不用重算</div>
      </div>
      <button class="btn xs" onclick="hfVecRefresh()">刷新</button>
    </div>
    <div class="row wrap" style="gap:7px;margin-top:8px">
      ${MB_GENS.map((g) => `<span class="chip${g.active ? " hot" : ""}" title="${esc(g.gen_key)}">
          ${esc(String(g.model || "未知模型"))} · <b>${fmtNum(g.dim)} 维</b> · ${fmtNum(g.covered)}/${fmtNum(g.total)}（${(g.coverage * 100).toFixed(1)}%）
          ${g.active ? " · 使用中" : `<button class="btn xs" style="margin-left:6px" onclick="hfVecSwitch(${jsStr(String(g.gen_key))})">切到这代</button>`}
        </span>`).join("")}
    </div>
  </div>` : ""}
  ${MB.recall ? `
  <div class="card sec" style="padding:12px 13px">
    <div class="row bt" style="gap:9px;align-items:flex-end">
      <div style="flex:1;min-width:0">
        <div class="pn">召回测试</div>
        <div class="pd">打一句话，看模型实际会想起哪几条（关键词路 + 向量路 → RRF 融合）。这是检验"记忆有没有用"的唯一办法</div>
      </div>
      <button class="btn xs" onclick="hfRecallRun()">开始召回</button>
    </div>
    <div class="row" style="gap:8px;margin-top:9px">
      <span class="searchbox" style="flex:1"><i class="ic">⌕</i><input id="rcQ" placeholder="比如：她生日那天发生了什么 / 教案 / 汤面" value="${esc(MB.recall_q)}" onkeydown="if(event.key==='Enter')hfRecallRun()"></span>
      <select class="ipt" id="rcK" style="width:96px">
        ${[5, 8, 12, 20].map((k) => `<option value="${k}"${MB.recall_topk === k ? " selected" : ""}>${k} 条</option>`).join("")}
      </select>
      <select class="ipt" id="rcS" style="width:190px">
        <option value="">不限会话</option>
        ${(MB_SESS.items || []).map((x) => `<option value="${esc(x.session_id)}">${esc(x.session_id)}（${fmtNum(x.memories)}）</option>`).join("")}
      </select>
    </div>
    ${MB.recall
      ? `<div class="mut" style="font-size:11px;margin:10px 0 6px">
           关键词命中 <b>${fmtNum(MB.recall.route.keyword_hits)}</b> 条 · 向量命中 <b>${fmtNum(MB.recall.route.vector_hits)}</b> 条 ·
           候选 ${fmtNum(MB.recall.candidates)} 条 · 耗时 ${fmtNum(MB.recall.took_ms)}ms
           ${MB.recall.route.vector_note ? ` · <b style="color:var(--warn, #b8860b)">${esc(MB.recall.route.vector_note)}</b>` : ""}
           · 切词 ${esc((MB.recall.tokens || []).join(" "))}
         </div>
         ${(MB.recall.items || []).length
            ? MB.recall.items.map((r, idx) => `<div class="mb-rc" onclick="hfMemOpenRow(${r.id})">
                <div class="row bt" style="gap:8px">
                  <div class="row" style="gap:7px;min-width:0">
                    <span class="chip">#${idx + 1}</span>
                    <span class="chip">${(Number(r.score) || 0).toFixed(4)}</span>
                    <span class="tag ${r.in_vector && r.in_keyword ? "ok" : "gy"}">${r.in_vector && r.in_keyword ? "两路都中" : r.in_vector ? "向量" : "关键词"}</span>
                    <span class="mut mono" style="font-size:10.5px">m${r.id}</span>
                    ${r.external_source ? `<span class="chip gy">导入</span>` : ""}
                  </div>
                  <span class="mut" style="font-size:10.5px">${esc(fmtTime(r.occurred_at))}</span>
                </div>
                <div style="font-size:12.5px;line-height:1.7;margin-top:5px">${esc(String(r.summary || "").slice(0, 150))}</div>
                ${(r.matched_terms || []).length ? `<div class="mut" style="font-size:10.5px;margin-top:4px">命中词：${esc(r.matched_terms.join(" / "))}</div>` : ""}
              </div>`).join("")
            : `<div class="mut" style="font-size:12px">没召回任何记忆 —— 要么库里确实没有相关内容，要么需要先「补全向量」</div>`}`
      : `<div class="mut" style="font-size:11.5px;margin-top:9px">填一句话再点「开始召回」</div>`}
  </div>` : ""}

  <div class="card sec" style="padding:11px 13px">
    <div class="row bt wrap" style="gap:8px">
      <span class="searchbox" style="width:240px"><i class="ic">⌕</i><input id="mbQ" placeholder="摘要 / 内容 / 原文 / 记忆 ID…" value="${esc(MB.q)}" oninput="hfMemSet('q', this.value)"></span>
      <select class="ipt" style="width:190px" onchange="hfMemSet('session_id', this.value)">
        <option value="">全部会话</option>
        ${(MB_SESS.items || []).map((x) => `<option value="${esc(x.session_id)}"${MB.session_id === x.session_id ? " selected" : ""}>${esc(sessLabel(x.session_id))}（记忆 ${fmtNum(x.memories)} / 对话 ${fmtNum(x.messages)}）</option>`).join("")}
      </select>
      <select class="ipt" style="width:170px" onchange="hfMemSet('persona_id', this.value)">
        <option value="">全部人格</option>
        ${(MB_SESS.personas || []).map((x) => `<option value="${esc(x.persona_id)}"${MB.persona_id === x.persona_id ? " selected" : ""}>${esc(String(x.persona_id || "（未标人格）"))}（${fmtNum(x.memories)}）</option>`).join("")}
      </select>
      <select class="ipt" style="width:110px" onchange="hfMemSet('status', this.value)">
        ${opt("", "全部状态", MB.status)}${opt("active", "活跃", MB.status)}${opt("stale", "待复活", MB.status)}${opt("decayed", "已衰减", MB.status)}${opt("archived", "已归档", MB.status)}${opt("deleted", "回收站", MB.status)}${opt("forgotten", "已遗忘", MB.status)}
      </select>
      <select class="ipt" style="width:110px" onchange="hfMemSet('atom_type', this.value)">
        ${opt("", "全部类型", MB.atom_type)}${opt("factual", "事实", MB.atom_type)}${opt("episodic", "事件", MB.atom_type)}${opt("preference", "偏好", MB.atom_type)}${opt("planned", "计划", MB.atom_type)}${opt("relational", "关系", MB.atom_type)}
      </select>
      <select class="ipt" style="width:110px" onchange="hfMemSet('source', this.value)">
        ${opt("", "全部来源", MB.source)}${opt("local", "本机产生", MB.source)}${opt("imported", "外部导入", MB.source)}
      </select>
      <select class="ipt" style="width:120px" onchange="hfMemSet('sort', this.value)">
        ${opt("recent", "最新优先", MB.sort)}${opt("old", "最旧优先", MB.sort)}${opt("strong", "强度高优先", MB.sort)}${opt("weak", "强度低优先", MB.sort)}${opt("recalls", "召回多优先", MB.sort)}${opt("poignancy", "情绪强优先", MB.sort)}
      </select>
      <select class="ipt" style="width:96px" onchange="hfMemSet('limit', this.value)">
        ${[20, 50, 100, 200].map((k) => opt(String(k), k + " 条/页", String(MB.limit))).join("")}
      </select>
      <button class="btn xs" onclick="hfMemExport()">导出当前页 CSV</button>
      <button class="btn xs" onclick="hfMemSet('q','');hfMemSet('session_id','');hfMemSet('status','');hfMemSet('atom_type','');hfMemSet('source','')">清空筛选</button>
    </div>
  </div>

  <div class="card sec" style="padding:0;overflow:hidden">
    <table class="mbt">
      <thead><tr>
        <th style="width:54px">ID</th><th>记忆（点一行展开详细）</th><th style="width:74px">类型</th>
        <th style="width:92px">强度</th><th style="width:70px">状态</th><th style="width:58px">向量</th>
        <th style="width:150px">人格</th><th style="width:132px">发生时间</th><th style="width:190px">会话</th>
      </tr></thead>
      <tbody>
      ${MB_DATA.items.length
        ? MB_DATA.items.map((m) => {
            const isOpen = MB.open === m.id;
            return `<tr class="mbrow${isOpen ? " on" : ""}" data-id="${esc(String(m.id).replace(/^m/, ""))}" onclick="hfMemOpenRow(${jsStr(String(m.id))})">
              <td><span class="mono" style="font-size:11px;color:var(--primary)">m${m.id}</span></td>
              <td><div class="mb-sum">${esc(String(m.summary || "(无摘要)"))}</div>
                  ${String(m.source_text || "").length > String(m.summary || "").length + 20 ? `<div class="mut" style="font-size:10.5px;margin-top:2px">有完整原文 ${fmtNum(String(m.source_text).length)} 字</div>` : ""}</td>
              <td><span class="chip gy">${esc(ATOM_LABEL[m.atom_type] || m.atom_type || "—")}</span></td>
              <td><span class="mb-bar"><i style="width:${Math.max(3, Math.min(100, Math.round((m.strength || 0) * 100)))}%"></i></span>
                  <span class="mut" style="font-size:10.5px;margin-left:5px">${fmt1(m.strength)}</span></td>
              <td><span class="tag ${m.status === "active" ? "ok" : (m.status === "archived" || m.status === "deleted") ? "dg" : "gy"}">${esc(STATUS_LABEL[m.status] || m.status || "—")}</span>
                  ${Number(m.muted || 0) === 1 ? `<span class="tag gy" style="margin-left:4px" title="不进召回与主动开口（可恢复）">冷处理</span>` : ""}
                  ${m.status === "deleted" ? `<button class="btn xs" style="margin-left:4px" onclick="event.stopPropagation();hfMemRestore(${jsStr(String(m.id).replace(/^m/, ""))})">恢复</button>` : ""}
                  ${m.status === "forgotten" ? `<button class="btn xs" style="margin-left:4px" onclick="event.stopPropagation();hfMemRestore(${jsStr(String(m.id).replace(/^m/, ""))})">恢复</button>` : ""}</td>
              <td>${m.has_vec ? '<span class="tag ok">有</span>' : '<span class="tag gy">无</span>'}</td>
              <td>${m.persona_id ? `<span class="chip" style="font-size:10.5px">${esc(String(m.persona_id))}</span>` : `<span class="mut" style="font-size:11px">未标</span>`}</td>
              <td class="mut" style="font-size:11px">${esc(fmtTime(m.occurred_at || m.created_at))}</td>
              <td class="mut" style="font-size:10.5px" title="${esc(String(m.session_id || ""))}">${esc(sessLabel(m.session_id))}${m.external_source ? `<div class="chip gy" style="margin-top:3px">导入</div>` : ""}</td>
            </tr>
            ${isOpen ? `<tr class="mb-det"><td colspan="9"><div class="fdet">${openHtml}</div></td></tr>` : ""}`;
          })
          .join("")
        : `<tr><td colspan="9" style="padding:26px;text-align:center">
             <div class="mut" style="font-size:12.5px">${MB.q || MB.session_id || MB.status || MB.atom_type || MB.source
               ? "当前筛选下没有记忆 —— 放宽条件试试"
               : "库里还没有记忆：让 bot 多聊几次，或去设置页导入 livingmemory 的历史记忆"}</div>
           </td></tr>`}
      </tbody>
    </table>
    <div class="mb-pager">
      <span class="mut">共 ${fmtNum(MB_DATA.total)} 条 · 第 ${page}/${pages} 页</span>
      <button class="btn xs" onclick="hfMemPage(-1)" ${page <= 1 ? "disabled" : ""}>上一页</button>
      <button class="btn xs" onclick="hfMemPage(1)" ${page >= pages ? "disabled" : ""}>下一页</button>
    </div>
  </div>`;
  return {
    html,
    mount: (host) => {
      const q = host.querySelector("#mbQ");
      if (!q) return;
      // 每次重渲染都是全新的 input，所以组字监听必须在这里重绑。
      // 为什么要有它：中文/日文输入法组字期间会连续触发 input 事件（拼音字母逐个进来），
      // 300ms 去抖一旦在组字中途到期，route(true) 会把 #pagehost 整块 innerHTML 换掉 ——
      // 输入框连同候选框一起消失，字就打不出来。组字期间掐掉去抖并不排新的，结束再查。
      _mbQcomposing = false;            // 新元素不可能还在组字：上一轮万一没收尾，旗标必须归零
      q.addEventListener("compositionstart", () => {
        _mbQcomposing = true;
        if (_mbQtimer) { clearTimeout(_mbQtimer); _mbQtimer = null; }   // 已经在路上的那次重渲染也要掐掉
      });
      q.addEventListener("compositionend", () => {
        _mbQcomposing = false;
        // 上屏后浏览器一般还会补一次 input（再走 hfMemSet 排定）；个别输入法不补，
        // 所以这里主动排一次 —— 300ms 到期时还会从输入框现值兜一次，保证上屏的字一定进查询条件。
        _mbQcaret = q.selectionStart;
        _mbQSchedule();
      });
      q.addEventListener("blur", () => { _mbQcomposing = false; });     // 兜底，别让旗标卡住
      if (!MB.q) return;
      q.focus();
      try {
        // 优先回到打字时的位置；没有记录（首次进入 / 从别处跳来）才落到末尾
        const pos = _mbQcaret == null ? q.value.length : Math.min(_mbQcaret, q.value.length);
        q.setSelectionRange(pos, pos);
      } catch (err) { /* 光标定位失败无所谓 */ }
      _mbQcaret = null;
    },
  };
}

/** 搜索框重渲染后要恢复的光标位置（null = 没记录，落到末尾） */
let _mbQcaret = null;
let _mbQtimer = null;
/** 输入法正在组字（拼音还没上屏）。组字期间一律不重渲染，否则输入框被重建、候选框消失。 */
let _mbQcomposing = false;

/** 排一次「去抖 → 重查」。组字中直接跳过（等 compositionend 再排），这样中途到期的去抖
 *  不会把正在组字的输入框从 DOM 里换掉。 */
function _mbQSchedule() {
  if (_mbQtimer) clearTimeout(_mbQtimer);
  if (_mbQcomposing) { _mbQtimer = null; return; }
  _mbQtimer = setTimeout(() => {
    _mbQtimer = null;
    // 输入法上屏与 input 事件的先后各浏览器不一致，落地前再拿输入框现值兜一次，
    // 免得最后上屏的几个字没进查询条件（用户看到的现象就是"搜不到刚打的词"）。
    const live = document.getElementById("mbQ");
    if (live && String(live.value) !== MB.q) MB.q = String(live.value);
    hfRerenderHook();
  }, 300);
}

/** 改筛选条件 → 回到第一页重查。
 *
 * 搜索框走 300ms 去抖：oninput 每次都触发「整页重渲染 + 重发全部接口」，
 * 敲 10 个字就是 10 轮（记忆库一轮 4 个 GET），既卡手又打服务端。
 * 下拉 / 翻页 / 排序这些一次性动作不下游去抖，立即生效。
 *
 * 光去抖还不够：中文/日文输入法组字期间会连着触发 input 事件，只要去抖在组字中途到期，
 * 重渲染就会把输入框整块换掉、候选框消失，用户表现为「打不出中文」。
 * 组字期间由 _mbQSchedule 直接跳过，等 compositionend 再排一次。
 */
export function hfMemSet(key, value) {
  if (key === "limit") MB.limit = Math.max(1, Number(value) || 20);
  else MB[key] = String(value == null ? "" : value);
  MB.offset = 0;
  if (key !== "q") {
    hfRerenderHook();
    return;
  }
  const el = document.getElementById("mbQ");
  _mbQcaret = el ? el.selectionStart : null;
  _mbQSchedule();
}

export function hfMemPage(dir) {
  MB.offset = Math.max(0, MB.offset + dir * MB.limit);
  hfRerenderHook();
}

/**
 * 点一行：原地展开详细（纯 DOM 操作）。
 * 之前是"整页重渲染"，结果每展开一次页面就刷新、滚动条弹回顶部——用户明确抱怨过。
 */
export async function hfMemOpenRow(id) {
  const mid = String(id == null ? "" : id).replace(/^m/, "");   // 表格/卡片两种入口都可能带 m 前缀
  id = mid;
  const tbody = document.querySelector(".mbt tbody");
  if (!tbody) { MB.open = id; hfRerenderHook(); return; }
  const dets = Array.from(tbody.querySelectorAll("tr.mb-det"));
  const existed = dets.find((r) => r.getAttribute("data-for") === String(id));
  dets.forEach((r) => r.remove());
  tbody.querySelectorAll("tr.mbrow.on").forEach((r) => r.classList.remove("on"));
  if (existed) { MB.open = 0; return; }                      // 再点一次 = 收起，位置不动
  const row = tbody.querySelector('tr.mbrow[data-id="' + String(id) + '"]');
  if (!row) { MB.open = id; hfRerenderHook(); return; }
  row.classList.add("on");
  MB.open = id;
  const det = document.createElement("tr");
  det.className = "mb-det";
  det.setAttribute("data-for", String(id));
  const td = document.createElement("td");
  td.colSpan = 9;
  const box = document.createElement("div");
  box.className = "fdet";
  box.textContent = "加载详细…";                             // 先给反馈，别留白
  td.appendChild(box);
  det.appendChild(td);
  row.parentNode.insertBefore(det, row.nextSibling);
  const m = (MB_DATA.items || []).find((x) => String(x.id) === String(id)) || { id: Number(id) };
  try {
    const edges = await apiSafe("memory/edges", { id: Number(id) }, { items: [] });
    const html = await memDetailHtml({ id: "m" + id, raw: m }, edges.items || edges.links || [], "l");
    const target = det.querySelector(".fdet");
    if (target) target.innerHTML = html;
  } catch (err) {
    const target = det.querySelector(".fdet");
    if (target) target.textContent = "详细加载失败：" + String(err && err.message ? err.message : err);
  }
}

/** 召回测试 */
/** 切换使用哪一代向量库（换回来立刻可用，不用重算） */
export async function hfVecSwitch(genKey) {
  const res = await apiPostSafe("vector/switch", { gen_key: genKey });
  toast(res && res.ok ? "已切换到 " + genKey : "切换失败：" + ((res && res.error) || ""), res && res.ok ? "ok" : "err");
  hfRerenderHook();
}

export function hfVecRefresh() {
  hfRerenderHook();
}

export function hfRecallToggle() {
  MB.recall = MB.recall ? null : { route: {}, items: [] };
  hfRerenderHook();
}

export async function hfRecallRun() {
  const q = document.getElementById("rcQ");
  const k = document.getElementById("rcK");
  const s = document.getElementById("rcS");
  const query = String((q && q.value) || "").trim();
  if (!query) {
    toast("先填一句要召回的话", "err");
    return;
  }
  MB.recall_q = query;
  MB.recall_topk = Number((k && k.value) || 8);
  const res = await apiPostSafe("recall", { query, topk: MB.recall_topk, session_id: String((s && s.value) || "") });
  const d = (res && res.data) || {};          // 字段在 data 里（以前判 res.items 恒 undefined → 成功也报"召回失败"）
  if (res && res.ok !== false && (d.items || d.route)) {
    MB.recall = d;
  } else {
    toast("召回失败：" + ((res && res.error) || "接口没返回数据"), "err");
  }
  hfRerenderHook();
}

/** 补向量：分批 + 显示进度（后端每批限速，避免拖死 embedding 提供商） */
export async function hfEmbedBackfill(btn) {
  if (btn) { btn.disabled = true; btn.textContent = "补全中…"; }
  let done = 0;
  for (let i = 0; i < 40; i++) {
    const res = await apiPostSafe("embed/backfill", { limit: 32 });
    if (!res || res.ok === false) {
      toast(res && res.error ? res.error : "补向量失败", "err");
      break;
    }
    const d = (res && res.data) || {};        // 同上：embedded/coverage 在 data 里
    done += d.embedded || 0;
    const miss = ((d.coverage || {}).missing_recallable ?? (d.coverage || {}).missing) || 0;
    if (btn) btn.textContent = `补全中… ${done}（还缺 ${miss}）`;
    if (miss === 0 || (d.embedded || 0) === 0) break;
  }
  toast(done ? `已补 ${done} 条向量` : "没有需要补的向量（或嵌入提供商在限速，稍后再试）", done ? "ok" : "err");
  if (btn) { btn.disabled = false; btn.textContent = "补全向量"; }     // 不恢复就会一直灰着（用户反馈"按钮发灰"）
  hfRerenderHook();
}

/** 补齐 livingmemory 的会话对话 / 来源轮次 / 完整原文 */
/** 重写记忆简写：把「用户…」这种机器句式改成自然说法（按当前筛选的人格；旧值有备份，写完重算向量） */
export async function hfRewriteSummaries(btn) {
  // 这个按钮现在挂在「记忆设置」页，够不到记忆库的筛选框 → 用顶栏的查看视角（记忆库筛过就用它兜底）
  const pid = STATE.personaView || MB.persona_id || "";
  if (!pid) { toast("先在上面筛选里选一个人格（避免把导入的记忆一起改写）", "err"); return; }
  // sandbox iframe 里 window.confirm 会被吞（返回 false，静默不执行）→ 用页面内确认弹窗
  confirmModal("重写记忆简写", `把「${pid}」的记忆简写重写得更自然？\n\n· 用「记忆总结」模型逐批改写（会产生少量模型费用）\n· 旧简写存进 hf_summary_backup 备份表，可回退\n· 简写变了会顺带重算这些记忆的向量`, async () => {
    if (btn) { btn.disabled = true; btn.textContent = "重写中…"; }
    let total = 0, failed = 0, scanned = 0, remain = -1;
    for (let i = 0; i < 80; i++) {
      const res = await apiPostSafe("memory/rewrite_summaries", { persona_id: pid, limit: 12 });
      const d = (res && res.data) || {};
      if (!res || res.ok === false || d.ok === false) {
        toast("重写失败：" + ((res && res.error) || d.error || "未知错误"), "err");
        break;
      }
      total += d.rewritten || 0;
      failed += d.failed || 0;
      scanned = d.scanned || scanned;
      remain = d.remaining == null ? 0 : d.remaining;
      if (btn) btn.textContent = `重写中… ${total}${scanned ? "/" + scanned : ""}（剩 ${remain}）`;
      if (remain <= 0) break;
    }
    toast(total ? `已重写 ${total} 条简写${failed ? `（${failed} 条没改出来，已跳过）` : ""}` : "没有需要重写的（或模型没返回）",
          total ? "ok" : "err");
    if (btn) { btn.disabled = false; btn.textContent = "重写简写"; }
    hfRerenderHook();
  }, "开始重写");
}

export async function hfImportExtras(btn) {
  if (btn) { btn.disabled = true; btn.textContent = "补齐中…"; }
  const res = await apiPostSafe("import/extras", {});
  const d = (res && res.data) || {};
  if (res && res.ok) {
    const errs = (d.errors || []).slice(0, 2).join(" / ");
    toast(`补齐完成：对话 ${fmtNum(d.messages || 0)} 条 · 会话 ${fmtNum(d.sessions || 0)} 个 · `
      + `来源 ${fmtNum(d.sources || 0)} 条 · 原文 ${fmtNum(d.texts || 0)} 篇` + (errs ? `（部分失败：${errs}）` : ""),
      errs ? "err" : "ok");
  } else {
    toast("补齐失败：" + ((res && res.error) || "未知错误"), "err");
  }
  if (btn) { btn.disabled = false; btn.textContent = "补齐对话与原文"; }
  hfRerenderHook();
}

/** 复活被衰减判死的历史记忆（默认把 stale/decayed 拉回 active） */
export async function hfMemReactivate(btn) {
  if (btn) { btn.disabled = true; btn.textContent = "复活中…"; }
  const res = await apiPostSafe("memory/reactivate", {});
  const d = (res && res.data) || {};
  toast(res && res.ok !== false ? `已复活 ${fmtNum(d.reactivated || 0)} 条（状态回到活跃，强度提到 0.45 以上）` : "复活失败",
        res && res.ok !== false ? "ok" : "err");
  if (btn) { btn.disabled = false; btn.textContent = "复活历史记忆"; }
  hfRerenderHook();
}

/** 导出当前页为 CSV（面板内可用，不依赖下载权限） */
export function hfMemExport() {
  const head = ["id", "summary", "content", "atom_type", "strength", "status", "has_vec", "session_id", "occurred_at"];
  const rows = (MB_DATA.items || []).map((m) => head.map((k) => `"${String(m[k] == null ? "" : m[k]).replace(/"/g, '""')}"`).join(","));
  const csv = "\ufeff" + head.join(",") + "\n" + rows.join("\n");
  const blob = new Blob([csv], { type: "text/csv;charset=utf-8" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = `starheart-memories-${new Date().toISOString().slice(0, 10)}.csv`;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  setTimeout(() => URL.revokeObjectURL(a.href), 4000);
  toast(`已导出 ${rows.length} 条（当前页）`, "ok");
}

export async function renderPersons() {
  const [persons, insight] = await Promise.all([
    // 后端已改成「按人聚合」：items 是人，groups 是群（群不是人，别把群号当 QQ 摆进人物卡）
    apiSafe("persons", { persona_id: STATE.personaView || "" }, { items: [], groups: [], bots: [] }),
    apiSafe("favour/insight", {}, {}),
  ]);
  const items = persons.items || [];
  const groups = persons.groups || [];
  PERSON_DATA = { items: persons.items || [], groups: groups };   // 群分支/详情要用
  const bots = persons.bots || [];
  STATE.persons = items;
  const zoneCount = (insight && insight.zones) || {};
  const ZR = { HOSTILE: 0, COLD: 1, NEUTRAL: 2, FAMILIAR: 3, CLOSE: 4, DEVOTED: 5 };
  const zoneColor = (z) => ({ HOSTILE: "#f44336", COLD: "#90a4ae", NEUTRAL: "#8e8e93", FAMILIAR: "#03c9d7", CLOSE: "#3c96ca", DEVOTED: "#e0559a" })[z] || "#8e8e93";
  // 名字在前、号在后：display 由后端算好（有昵称就是昵称，没有才回退成"未记昵称 · 号"）
  const label = (p) => p.display || p.name || String(p.user_id || "");
  const totalPrivMem = items.reduce((a, x) => a + (x.memories || 0), 0);
  const totalGroupMem = groups.reduce((a, x) => a + (x.memories || 0), 0);
  const settled = items.filter((x) => x.zone).length;

  const personView = STATE.personView || "graph";
  // L0 人物 / L1 群聊 两栏画像总览：数据来自一次 /persons（后端已按当前人格带上画像状态），
  // 所以「这个人格下有没有画像」「别的人格下有」都能如实显示，绝不假装"从没生成过"。
  const html = `
  ${pageHead(
    "画像",
    "本页只有视图：在画布上点某个人或某个群，卡片菜单里就能看到该人格下的画像状态并生成/重新生成（画像按人格隔离，同一个人在不同人格下是两份）",
    `<button class="btn" onclick="hfPersonsRefresh(this)">重新聚合</button>
     <button class="btn s" onclick="goPage('settings','contacts')">名单与可见性 →</button>`,
    "☺"
  )}
  ${shell3dHtml("p", {
    // 只保留 3D 图谱：分布总览/人物卡片已按用户要求移除（详情改成左侧卡片原地放大）
    views: [{ value: "graph", label: "3D 关系图谱", on: true }],
    altHtml: `
      ${kpis(
        kpiCard("ppTotal", fmtNum(items.length), "有过互动的人")
        + kpiCard("ppPrivMem", fmtNum(totalPrivMem), "私聊记忆（记在本人名下）")
        + kpiCard("ppGroupMem", fmtNum(totalGroupMem), "群聊记忆（记在群名下）")
        + kpiCard("ppSettled", fmtNum(settled), "已结算关系档位")
      )}  <div class="grid g23 sec" id="ppOverview" ${personView === "overview" ? "" : 'style="display:none"'}>
    ${card(`<div id="ppBars"></div>`, { title: "谁聊得最多（按人）", sub: "这个人的所有会话（私聊+群聊）加起来" })}
    ${card(`<div id="ppZones"></div>`, { title: "关系区间分布", sub: "按好感结算出来的区间" })}
  </div>
  ${
    bots.length
      ? `<div class="mut" style="font-size:11px;margin:6px 0 0">已自动排除机器人自己：${esc(bots.join("、"))}</div>`
      : ""
  }
  <div class="sec-t" ${personView === "cards" ? "" : 'style="display:none"'}>人物卡片（${items.length}）</div>
  <div class="grid gauto" id="ppCards" ${personView === "cards" ? "" : 'style="display:none"'}>
    ${
      items.length
        ? items
            .map((p) => {
              const zone = String(p.zone || "");
              const rank = ZR[zone] ?? 2;
              const priv = Number(p.private_msgs) || 0;
              const grp = Number(p.group_msgs) || 0;
              return `<div class="card cp hov">
          <div class="row" style="gap:11px;margin-bottom:9px">
            <div id="ppAv_${esc(p.user_id)}" style="flex-shrink:0"></div>
            <div style="min-width:0">
              <div style="font-weight:650;font-size:13px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(label(p))}</div>
              <div class="mut mono" style="font-size:10.5px">QQ ${esc(p.user_id)}</div>
            </div>
          </div>
          <div class="row wrap" style="gap:6px">
            <span class="tag gy">私聊 ${fmtNum(priv)}</span>
            <span class="tag gy">群聊 ${fmtNum(grp)}</span>
            <span class="tag info">记忆 ${fmtNum(p.memories)}</span>
            <span class="tag gy">召回 ${fmtNum(p.recalls)}</span>
            <span class="tag ${zone ? (rank >= 4 ? "ok" : rank >= 3 ? "info" : "gy") : "gy"}">${esc(zone || "未结算")}</span>
          </div>
          <div class="mut" style="font-size:10.5px;margin-top:7px">最近活动 ${timeAgo(p.last_ts)}</div>
          <div class="row" style="gap:5px;margin-top:5px">
            <span class="mut" style="font-size:10.5px">关系进度</span>
            <span class="bar" style="flex:1"><span class="bf ${rank >= 4 ? "ok" : ""}" style="display:block;width:${((rank + 1) / 6) * 100}%"></span></span>
            <span class="mono mut" style="font-size:10px">${rank + 1}/6</span>
          </div>
          ${
            (p.groups || []).length
              ? `<div class="mut" style="font-size:10.5px;margin-top:7px">出现在 <b>${(p.groups || []).length}</b> 个群：${(p.groups || []).map((g) => `<span class="mono">${esc(g)}</span>`).join("、")}</div>`
              : `<div class="mut" style="font-size:10.5px;margin-top:7px">只在私聊出现过</div>`
          }
        </div>`;
            })
            .join("")
        : emptyBlock("还没有可画像的人：等机器人和人聊过几轮后再回来", "☺")
    }
  </div>
  <div class="sec-t" ${personView === "graph" ? 'style="display:none"' : ""}>群聊（${groups.length}）· 这些是群号，不是人</div>
  <div class="grid gauto" id="ppGroups" ${personView === "cards" ? "" : 'style="display:none"'}>
    ${
      groups.length
        ? groups
            .map(
              (g) => `<div class="card cp">
          <div class="row" style="gap:9px;margin-bottom:8px">
            <span class="tag info">群</span>
            <span class="mono" style="font-weight:650;font-size:12.5px">${esc(g.group_id)}</span>
          </div>
          <div class="row wrap" style="gap:6px">
            <span class="tag gy">群记忆 ${fmtNum(g.memories)}</span>
            <span class="tag gy">发言 ${fmtNum(g.msgs)}</span>
            <span class="tag gy">活跃 ${fmtNum(g.people)} 人</span>
            ${g.recalls ? `<span class="tag gy">召回 ${fmtNum(g.recalls)}</span>` : ""}
          </div>
          <div class="mut" style="font-size:10.5px;margin-top:7px">最近活动 ${timeAgo(g.last_ts)}</div>
        </div>`
            )
            .join("")
        : `<div class="mut" style="font-size:11.5px;padding:6px 2px">还没有群聊记录</div>`
    }
  </div>`,
  })}`;

  return {
    html,
    mount: async (host) => {
      fillTopMetrics(host, "p", [
        ["有过互动的人", fmtNum(items.length)],
        ["私聊记忆", fmtNum(totalPrivMem)],
        ["群聊记忆", fmtNum(totalGroupMem)],
        ["已结算档位", fmtNum(settled)],
      ]);
      const alt = host.querySelector("#pAlt");
      const chartsOn = () => {
        mount(host, "#ppBars", (el) =>
          C.bars(el, {
            items: items.slice(0, 8).map((p) => ({ label: label(p), value: (p.memories || 0) + 0, sub: `${p.msgs || 0} 条发言` })),
          })
        );
        mount(host, "#ppZones", (el) =>
          C.barSplit(el, {
            items: Object.keys(zoneCount).map((z) => ({ label: z, value: zoneCount[z], color: zoneColor(z) })),
            height: 18,
          })
        );
        items.forEach((p) => {
          const rank = ZR[String(p.zone || "")] ?? 2;
          const box = host.querySelector(`#ppAv_${CSS.escape(String(p.user_id))}`);
          if (!box) return;
          C.avatar(box, { name: label(p), size: 42, color: rank >= 4 ? "#e0559a" : "#3c96ca" });
        });
      };
      const showAlt = (v) => {
        if (alt) alt.classList.toggle("hide", v === "graph");
        const g = host.querySelector("#pMount");
        if (g) g.style.opacity = v === "graph" ? "1" : "0.25";
        if (v !== "graph") setTimeout(chartsOn, 60);
      };
      showAlt(personView);
      if (alt && personView !== "graph") alt.scrollTop = 0;
      const eng = await mountPersonsGraph(host, { items, groups, label, zoneColor, onView: (v) => { STATE.personView = v; showAlt(v); } });
      if (!eng) return;
      if (personView === "graph") return;

    },
  };
}

/** 重新聚合：后端按人重算一遍（记忆边也顺手重建，避免画像里的关系数停在旧值） */
export async function hfPersonsRefresh(btn) {
  if (btn) btn.classList.add("loading");
  const res = await apiPostSafe("graph/build", { persona_id: STATE.personaView || "" });
  if (btn) btn.classList.remove("loading");
  if (!res.ok) toast("重新聚合失败：" + res.error, "err");
  else toast("已重新聚合人物画像", "ok");
  hfRerenderHook();
}

/* ==================================================================
   图谱 / 记忆库 操作（挂到 window 给内联 onclick 用）
   ================================================================== */

/** 引擎是否支持程序化选中是版本相关的事：有就用，没有就退化成只更新右侧档案 */
function engineSelect(id) {
  if (!G) return;
  try {
    if (typeof G.selectNode === "function") G.selectNode(id || null);
  } catch (err) {
    console.warn("[starheart] 引擎选中失败", err);
  }
}

export function hfGraphSelect(id) {
  engineSelect(id);
  const host = G_HOST;
  if (!host) return;
  const node = id ? (STATE.graph.nodes || []).find((n) => n.id === id) : null;
  renderGraphDetail(host, node || null);
}

/** 卡片渲染用：这个节点当前是不是正被当轴心居中（决定按钮是「居中」还是「取消居中」） */
function focusOn(which, id) {
  const eng = which === "p" ? PERSON3 : G3;
  if (!eng || typeof eng.focusedId !== "function") return false;
  const cur = eng.focusedId();
  return cur !== null && cur !== undefined && String(cur) === String(id);
}

/**
 * 把某个节点设为旋转轴心（居中）。
 * 关键点：节点可能压根不在当前数据里（别的人格/别的查看范围）→ 先切到"全部"再重试，
 * 否则 toggleFocus 找不到节点会直接 return，用户看到的就是"点过去什么也没发生"。
 */
let FOCUS_RETRY = 0;

/**
 * 把某个节点设为旋转轴心（居中）。
 *
 * 踩过的坑：之前用 STATE.graph.nodes 判断"节点在不在"——从记忆库跳过来时，
 * STATE.graph 还是上一页（人物画像）的载荷，于是"节点不存在"→ 直接报错，
 * 而且只有"查看范围=全部"时才会走到这条（不是全部时会先切范围再重试，反而能成功）。
 * 现在问引擎自己（setData 同步写入 nodes），并且数据没加载完就等一等再试。
 */
export function hfGraphFocus(id) {
  const mid = String(id == null ? "" : id).replace(/^m/, "");
  const nodeId = "m" + mid;
  const eng = typeof G3 !== "undefined" ? G3 : null;
  if (!eng) return;
  const nodes = eng.nodes || [];
  const exists = nodes.some((n) => String(n.id) === nodeId);
  if (!exists) {
    // 数据可能还在加载（跳页后 600ms 未必够），给几次机会，别过早报错
    if (FOCUS_RETRY < 6) {
      FOCUS_RETRY += 1;
      setTimeout(() => hfGraphFocus(id), 500);
      return;
    }
    FOCUS_RETRY = 0;
    if (G_SCOPE !== "all") {
      G_SCOPE = "all";                                    // 换成"全部"，把这条记忆包含进来
      toast("这条记忆不在当前查看范围，已把图谱切到全部", "ok");
      hfRerenderHook();
      setTimeout(() => hfGraphFocus(id), 900);
    } else {
      toast("图谱数据里没有这条记忆（可能已归档或未向量化），可试「复活历史记忆 / 补全向量」", "err");
    }
    return;
  }
  FOCUS_RETRY = 0;
  const focused = String(eng.focusedId ? eng.focusedId() : "") === nodeId;
  LAST_FOCUS_ID = nodeId;
  if (typeof eng.toggleFocus === "function") eng.toggleFocus(nodeId, !focused);
  else eng.focusNode(nodeId);
}

export function hfGraphSearch(v) {
  if (G3) G3.setSearch(v || "");
  if (G) G.setSearch(v || "");
}

export function hfGraphCards(on) {
  // 3D 视图下没有「卡片」这个概念（节点本身就是发光点），保留开关只为兼容老链接
  if (G) G.setCards(!!on);
}

export function hfGraphMotion(on) {
  if (G3) G3.setMotion(!!on);
  if (G) G.setMotion(!!on);
}

/** 调试口：给自动化断言/性能剖析用 */
export function __graphEngine3d() {
  return G3;
}

export function hfGraphFit() {
  if (G3) G3.fit();
  else if (G) G.fitView();
}

/** 重置视角：回到默认俯仰 + 适应视图（3D 才有的操作） */
export function hfGraphResetView() {
  if (G3) {
    G3.yaw = -0.6;
    G3.pitch = 0.42;
    G3.fit();
    toast("视角已重置", "ok");
  } else if (G) {
    G.fitView();
  }
}

export function hfGraphResetPhysics() {
  const defs = { repulsion: 1400, linkForce: 0.055, centerForce: 0.008, linkDistance: 46 };
  const put = (sel, v) => {
    const el = document.querySelector(sel);
    if (el) el.value = String(v);
    const lab = document.querySelector(sel + "V");
    if (lab) lab.textContent = String(v);
  };
  put("#gRep", defs.repulsion);
  put("#gLink", defs.linkForce);
  put("#gCen", defs.centerForce);
  put("#gDist", defs.linkDistance);
  if (G3) G3.setPhysics(defs);
  if (G) G.setPhysics && G.setPhysics(defs);
  toast("物理参数已恢复默认", "ok");
}

export function hfGraphShuffle() {
  if (G3) {
    G3.setMode(G_VIEW || "chain");
    toast("已重新排列（重新播种 + 重新收敛）", "ok");
  } else if (G) {
    G.setView(G_VIEW || "chain");
    toast("已重新排列", "ok");
  }
}

export function hfGraphFull(btn) {
  const wrap = $("#gWrap");
  if (!wrap) return;
  wrap.classList.toggle("full");
  const on = wrap.classList.contains("full");
  if (btn) btn.textContent = on ? "⛶ 退出全屏" : "⛶ 全屏";
  if (G) setTimeout(() => { try { G.resize(); } catch (err) { /* 全屏尺寸变化后重算 */ } }, 60);
}

export function hfGraphExport() {
  // 3D：直接导画布；2D：走引擎自带的导出
  if (G3) {
    const url = G3.snapshot();
    if (!url) {
      toast("导出失败：画布不可读", "err");
      return;
    }
    const a = document.createElement("a");
    a.href = url;
    a.download = `starheart-graph3d-${Date.now()}.png`;
    a.click();
    toast("已导出 PNG", "ok");
    return;
  }
  if (!G || typeof G.exportPng !== "function") {
    toast("当前引擎不支持导出", "err");
    return;
  }
  try {
    G.exportPng(`starheart-graph-${Date.now()}.png`);
    toast("已导出 PNG", "ok");
  } catch (err) {
    toast("导出失败：" + err.message, "err");
  }
}

/**
 * 在图谱里看这条记忆。用户反馈的两件事都在这里修：
 *  ① 之前点过去会锁定中心轴，退出不恢复 → 先清掉上一次的锁定（离开图谱页时 app.js 也会清）。
 *  ② 其他人格的记忆点过去看不到（图谱还在原人格范围里）→ 自动把查看范围切到"全部"再锁定。
 */
export async function hfGraphOpen(id) {
  clearGraphFocus();                                        // 先清上一次的锁定，避免越点越乱
  const mid = String(id).replace(/^m/, "");
  let m = (MB_DATA.items || []).find((x) => String(x.id) === mid) || null;
  if (!m || !m.persona_id) {
    const full = await apiSafe("memory", { id: mid }, null);  // 表格是筛选过的，别依赖当前页数据
    m = Object.assign({}, m || {}, (full && (full.memory || full)) || {});
  }
  const cur = STATE.personaView || "";
  const pid = String((m && m.persona_id) || "");
  if (G_SCOPE !== "all" && pid && pid !== String(cur)) {
    G_SCOPE = "all";
    toast("这条记忆属于人格「" + pid + "」，已把图谱范围切到全部", "ok");
  }
  closeDrawer();
  goPage("memory", "graph");
  FOCUS_RETRY = 0;
  setTimeout(() => hfGraphFocus(id), 700);
}

/**
 * 人物画像 3D 图谱：人 = 大点（颜色 = 关系档位，大小 = 记忆/互动量），群 = 小点，
 * 连线 = 同群（来自后端按人聚合的 groups/sessions）或档位接近（弱连接）。
 */
async function mountPersonsGraph(host, ctx) {
  const { items, groups, label, zoneColor, onView } = ctx;
  const nodes = [];
  const links = [];
  const gIds = new Set(groups.map((g) => String(g.group_id || g.session_id || "")));
  items.forEach((p) => {
    const zone = String(p.zone || "") || "UNSET";
    nodes.push({
      id: "u" + p.user_id,
      label: label(p),
      kind: "person",
      group: zone,
      // ⚠️ 必须显式声明角色/层：setData 只按 group 认 topic/entity，认不出 kind="person"
      // → 之前图例把「人」显示成「记忆 3」、把「群」显示成「话题」（实测过）。
      role: "person",
      level: 0,
      atom_type: "",
      importance: 8 + (Number(p.memories) || 0) * 1.2,
      ts: Number(p.last_ts) || 0,
      strength: Math.min(1, (Number(p.memories) || 0) / 30),
    });
  });
  groups.forEach((g) => {
    nodes.push({
      id: "g" + (g.group_id || g.session_id),
      label: g.name || `群 ${g.group_id || g.session_id}`,
      kind: "entity",
      group: "topic",
      role: "session",     // 群 = L1（不是"话题"）
      level: 1,
      importance: 4 + (Number(g.memories) || 0) * 0.8,
      ts: Number(g.last_ts) || 0,
    });
  });
  items.forEach((p) => {
    const known = new Set([...(p.groups || []), ...(p.sessions || [])].map(String));
    let linked = 0;
    known.forEach((sid) => {
      if (!gIds.has(sid)) return;
      links.push({ source: "u" + p.user_id, target: "g" + sid, weight: 1.3, type: "在群里" });
      linked++;
    });
    // 兜底：知道他在群里说过话，但后端没给群号（老数据）→ 连到他最活跃的群，避免孤立点
    if (!linked && Number(p.group_msgs) > 0 && groups.length) {
      const g = groups.slice().sort((a, b) => (b.memories || 0) - (a.memories || 0))[0];
      links.push({ source: "u" + p.user_id, target: "g" + (g.group_id || g.session_id), weight: 0.5, type: "疑似同群" });
    }
  });
  // 同档位的人弱相连：让「圈子」显出来（档位相同 ≠ 一定认识，所以权重压很低）
  for (let i = 0; i < items.length; i++) {
    for (let j = i + 1; j < items.length; j++) {
      const za = String(items[i].zone || "UNSET");
      const zb = String(items[j].zone || "UNSET");
      if (za && za === zb) {
        links.push({ source: "u" + items[i].user_id, target: "u" + items[j].user_id, weight: 0.35, type: "关系接近" });
      }
    }
  }
  const zoneLegend = () => {
    const zs = Array.from(new Set(items.map((p) => String(p.zone || "") || "UNSET")));
    return (
      `<div><b>颜色 = 关系档位</b></div>` +
      zs
        .map(
          (z) =>
            `<div><i style="display:inline-block;width:8px;height:8px;border-radius:50%;background:${zoneColor(z === "UNSET" ? "" : z)};margin-right:5px"></i>${esc(z === "UNSET" ? "未结算" : z)}</div>`
        )
        .join("") +
      `<div style="margin-top:4px"><i style="display:inline-block;width:8px;height:8px;border-radius:2px;background:#8b5cf6;margin-right:5px"></i>群（方形点）</div>`
    );
  };
  PERSON_DATA = { items, groups };
  PERSON3 = await mountShell3d(host, "p", { nodes, links }, {
    mode: "chain",
    onView: onView,
    hintOf: (n) => "已选中：" + String(n.label || n.id),
    cardHtml: (n, e) => {
      if (!n) return "";
      const it = (e.nodes || []).find((x) => String(x.id) === String(n.id));
      const p = items.find((x) => "u" + x.user_id === n.id);
      const g = groups.find((x) => "g" + (x.group_id || x.session_id) === n.id);
      const nb = [];
      (e.links || []).forEach((l) => {
        if (!it) return;
        if (l.a === it.idx || l.b === it.idx) {
          const other = e.nodes[l.a === it.idx ? l.b : l.a] || {};
          nb.push({ id: other.id, name: other.name || other.id });
        }
      });
      const head = `<h3>${esc(String(n.label || n.id))}</h3>`;
      const nbBlock = `<div class="bar"></div>
        <div class="mut" style="font-size:10.5px;margin-bottom:4px">邻居（${nb.length}）· 点一下跳过去</div>
        <div class="nb">${nb.length ? nb.slice(0, 24).map((x) => `<u data-goto="${esc(x.id)}"><em>→</em> ${esc(String(x.name).slice(0, 22))}</u>`).join("<br>") : "没有相邻节点"}</div>`;
      if (p) {
        return `${head}
          <div class="row" style="gap:6px;margin-bottom:6px"><span class="tag gy">${esc(String(p.zone || "未结算"))}</span>
            <span class="tag info">第 0 层 · 人物</span></div>
          ${(() => {
            // 真号优先：昵称认不出是谁。后端节点已带 ident；老数据退到 profile 的 user_id。
            const _uid = String(n.ident || (it && it.ident) || p.user_id || "").trim();
            return _uid ? `<div class="kv">用户 ID<span title="${esc(_uid)}">${esc(_uid)}</span></div>` : "";
          })()}
          <div class="kv">互动<span>${fmtNum(p.msgs)} 条（私聊 ${fmtNum(p.private_msgs)} · 群聊 ${fmtNum(p.group_msgs)}）</span></div>
          <div class="kv">记忆<span>${fmtNum(p.memories)}</span></div>
          <div class="kv">关系度<span>${fmtNum(it ? it.deg : 0)}</span></div>
          <div class="kv">最近<span>${timeAgo(p.last_ts)}</span></div>
          ${(p.groups || []).length ? `<div class="kv">在群里<span>${esc((p.groups || []).join("、"))}</span></div>` : ""}
          ${nbBlock}
          <div class="row" style="gap:6px;margin-top:10px;flex-wrap:wrap">
            <button class="btn xs p" data-det-btn onclick="hfCardDet('p', ${jsStr(String(n.id))})">展开详细</button>
            <button class="btn xs" onclick="goPage('settings','contacts')">名单与可见性</button>
            <button class="btn xs${focusOn("p", n.id) ? " on" : ""}" data-focus-btn onclick="hfPersonFocus(${jsStr(String(n.id))})">${focusOn("p", n.id) ? "取消居中" : "居中"}</button>
          </div>
          <div class="fdet"></div>`;
      }
      if (g) {
        return `${head}
          <div class="row" style="gap:6px;margin-bottom:6px"><span class="tag gy">群</span><span class="tag info">第 1 层 · 群聊</span></div>
          <div class="kv">群号<span title="${esc(String(g.group_id || n.ident || ""))}">${esc(String(g.group_id || n.ident || "—"))}</span></div>
          <div class="kv">记忆<span>${fmtNum(g.memories)}</span></div>
          <div class="kv">发言<span>${fmtNum(g.msgs)}</span></div>
          <div class="kv">活跃<span>${fmtNum(g.people)} 人</span></div>
          <div class="kv">关系度<span>${fmtNum(it ? it.deg : 0)}</span></div>
          ${nbBlock}
          <div class="row" style="gap:6px;margin-top:10px;flex-wrap:wrap">
            <button class="btn xs p" data-det-btn onclick="hfCardDet('p', ${jsStr(String(n.id))})">展开详细</button>
            <button class="btn xs${focusOn("p", n.id) ? " on" : ""}" data-focus-btn onclick="hfPersonFocus(${jsStr(String(n.id))})">${focusOn("p", n.id) ? "取消居中" : "居中"}</button>
          </div>
          <div class="fdet"></div>`;
      }
      return `${head}
        <div class="row" style="gap:6px;margin-top:10px">
          <button class="btn xs p" data-det-btn onclick="hfCardDet('p', ${jsStr(String(n.id))})">展开详细</button>
        </div>
        <div class="fdet"></div>`;
    },
    legend: zoneLegend,
  });
  return PERSON3;
}

export async function renderMemoryConf() {
  const dom = await apiSafe("config/domains", {}, { domains: {}, values: {} });
  if (dom && dom.__err) {
    return {
      html: pageHead(
        "记忆设置读取失败",
        `配置接口现在拿不到：${esc(String(dom.__err))}`,
        `<button class="btn p" onclick="hfRefresh()">重试</button>`,
        "⚠"
      ) + card(`<div class="fnote">⚠ 为避免把空值/关闭状态写回，本次不渲染表单。请重试。</div>`,
               { title: "读取失败" }),
    };
  }
  const values = dom.values || {};
  const G = (dom.guards && dom.guards.keys) || {};
  const half = Number(values.memory_decay_half_life_days) || 21;
  const rows = [
    ["memory_summary_prompt", "总结自定义提示词", "空=用内置；支持 {transcript} 占位；请保留输出格式行（摘要|类型|强度|情绪浓度）", "area"],
    ["memory_inject_enabled", "记忆注入", "开关：把召回到的记忆拼进 prompt", "bool"],
    ["memory_inject_top_k", "注入条数", "每轮最多注入几条记忆", "num", 1, 20],
    ["memory_inject_budget_tokens", "注入预算", "记忆部分的 token 上限（超了会截断）", "num", 100, 6000],
    ["memory_capture_enabled", "对话自动总结", "聊完自动蒸馏出「关于用户的记忆」", "bool"],
    ["memory_capture_turns", "触发轮数", "攒够几轮对话触发一次总结", "num", 1, 60],
    ["memory_capture_min_interval", "最小间隔(秒)", "两次总结之间至少隔多久", "num", 30, 7200],
    ["memory_capture_max_per_run", "单次上限", "一次总结最多产出几条记忆", "num", 1, 20],
    ["cross_session_enabled", "跨会话动作留痕", "bot 借工具（自带或第三方工具类插件）在别的会话里发了消息时，留台账并在相关会话注入知会", "bool"],
    ["cross_session_ttl_min", "知会有效期(分钟)", "超过这个时间的动作不再注入；长期回忆走记忆库", "num", 10, 1440],
    ["cross_session_memory", "跨会话动作写入记忆", "关掉则只在有效期内注入知会，不写记忆条目", "bool"],
    ["memory_decay_half_life_days", "衰减半衰期(天)", "记忆强度按指数衰减，这是半衰期", "num", 1, 365],
    ["memory_evict_below", "淘汰阈值", "强度低于此值的记忆会被标记 stale", "num", 0.01, 0.5, 0.01],
  ];
  const html = `
  ${pageHead("记忆设置", "这些是写进数据库的真实配置，改完点保存即生效", `<button class="btn p" onclick="hfSaveDomain('memory',this)">保存</button>`, "⚙")}
  <div class="grid g32 sec">
    ${card(
      rows
        .map(([key, name, desc, type, min, max, step]) => {
          const g = G[key] || {};
          if (g.min != null) min = g.min;
          if (g.max != null) max = g.max;
          const v = values[key];
          return `<div class="prow" data-key="${key}"><div><div class="pn">${esc(name)}</div><div class="pd">${esc(desc)} · <span class="mono">${key}</span></div></div>
        <div class="pc">${
          type === "bool"
            ? `<label class="sw"><input type="checkbox" data-hf-cfg="${key}" ${v ? "checked" : ""}><span class="tk"></span><span class="kb"></span></label>`
            : type === "area"
            ? `<textarea class="inp" rows="3" data-hf-cfg="${key}" placeholder="留空 = 用内置提示词">${esc(v ?? "")}</textarea>`
            : type === "text"
            ? `<input class="inp" type="text" data-hf-cfg="${key}" value="${esc(v ?? "")}">`
            : `<input class="inp" type="number" data-hf-cfg="${key}" value="${esc(String(v ?? ""))}" ${min != null ? `min="${min}"` : ""} ${max != null ? `max="${max}"` : ""} ${step ? `step="${step}"` : ""}>`
        }</div></div>`;
        })
        .join(""),
      { title: "记忆域参数", sub: "记忆注入 / 抓取 / 衰减 · 对应后端 CONFIG_DOMAINS['memory']" }
    )}
    <div class="col">
      ${card(
        `<div class="mut" style="font-size:11.5px;line-height:1.7">一次性 / 可重跑任务：重复点只处理新增，每次运行都写审计（消息事件 → 审计可查）。</div>
        <div class="col" style="gap:2px;margin-top:10px">
          ${[
            ["补齐对话与原文", "从 livingmemory 补原始对话 / 来源轮次 / 完整原文（不再 250 字截断）", "hfImportExtras", "开始补齐"],
            ["补全向量", "给缺向量的记忆生成嵌入（归档的不补，分批限速）", "hfEmbedBackfill", "开始补全"],
            ["复活历史记忆", "把被衰减判死的 stale / decayed 拉回活跃（强度下限 0.45）", "hfMemReactivate", "开始复活"],
            ["重写简写", "用「记忆总结」模型把简写改写成自然句式（旧值进 hf_summary_backup 可回退）· 按顶栏查看视角的人格", "hfRewriteSummaries", "开始重写"],
          ].map(([name, desc, fn, label]) => `<div class="prow" data-key="act_${fn}" style="align-items:center">
            <div><div class="pn">${name}</div><div class="pd">${desc}</div></div>
            <div class="pc"><button class="btn s" onclick="${fn}(this)">${label}</button></div>
          </div>`).join("")}
        </div>`,
        { title: "维护动作" }
      )}
      ${card(`<div id="mcCurve"></div>`, { title: "衰减曲线（模型）", sub: `按当前半衰期 ${half} 天：第 30 天剩 ${(Math.pow(0.5, 30 / half) * 100).toFixed(1)}%，第 60 天剩 ${(Math.pow(0.5, 60 / half) * 100).toFixed(1)}%` })}
      ${card(`<div id="mcTypes"></div>`, { title: "类型分布", sub: "当前人格视角下的记忆构成" })}
      ${card(
        `<div class="mut" style="font-size:11.5px;line-height:1.7">破坏性维护（<b>衰减 / 淘汰</b> · <b>清理冻结记忆</b>）已经收进「设置 → 数据治理」集中的一处，本页不再重复放置入口。<br>
        · 衰减：按半衰期降低所有记忆强度<br>
        · 淘汰：把低于阈值的记忆标成 stale（不在图谱与召回里出现）<br>
        · 清理冻结：删掉没有任何关系链且已 stale 的记忆</div>
        <div class="row" style="gap:6px;margin-top:10px;flex-wrap:wrap">
          <button class="btn s" onclick="goPage('settings','main')">去数据治理 →</button>
        </div>`,
        { title: "维护" }
      )}
    </div>
  </div>`;
  return {
    html,
    mount: async (host) => {
      const stats = await apiSafe("stats", {}, {});
      STATE.stats = stats;
      const pts = [];
      for (let d = 0; d <= 60; d += 1) pts.push(Math.pow(0.5, d / half));
      mount(host, "#mcCurve", (el) =>
        C.area(el, {
          series: [{ name: "剩余强度", color: "#3c96ca", points: pts }],
          xLabels: pts.map((_, i) => (i % 15 === 0 ? `第${i}天` : "")),
          height: 150,
          valueFmt: (v) => v.toFixed(2),
        })
      );
      const byType = stats.memory_by_type || {};
      mount(host, "#mcTypes", (el) =>
        C.barSplit(el, {
          items: Object.keys(byType).map((k) => ({ label: ATOM_LABEL[k] || k, value: byType[k], color: atomColor(k) })),
          height: 18,
        })
      );
    },
  };
}

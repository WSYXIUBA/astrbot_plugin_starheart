/**
 * StarHeart 面板 · 3D 图谱引擎（WebGL 主渲染 + canvas2D 叠加层）
 *
 * 为什么是两块画布（和参考实现同构）：
 * - 底层 canvas（WebGL）交给 GPU：程序化星云/星场背景、发光点精灵、叠加混合的关系线。
 *   上万顶点一次性上传，CPU 不再逐个下发绘制命令 —— canvas2D 逐点逐线在真机上只有 15fps，
 *   而 GPU 侧 3000+ 点仍稳 60fps。
 * - 上层 canvas（canvas2D）画标签、悬停双环准星、选中旋转弧线：文字排版和可读性留在 2D 里最省事。
 *
 * 拿不到 WebGL 上下文时不在这里做第二套实现：createGraph3D() 会回落 canvas2D 引擎
 * （graph3d-fallback.js），页面代码一行都不用改。
 *
 * 世界尺度与参考实现一致（约 ±10，相机距离 ~21，FOV 48°），12 种布局的公式可以照着搬。
 */

import { Graph3D as Graph3DCanvas } from "./graph3d-fallback.js?v=1.0.3";

const TAU = Math.PI * 2;
const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
const lerp = (a, b, t) => a + (b - a) * t;
const easeIO = (t) => (t < 0.5 ? 2 * t * t : 1 - Math.pow(-2 * t + 2, 2) / 2);

/** 确定性哈希：同一 id 每次进页面得到同一套位置，图不会"每次长得不一样" */
function hash01(str) {
  let h = 2166136261;
  const s = String(str == null ? "" : str);
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return ((h >>> 0) % 100000) / 100000;
}

/** 记忆原子类型中文名（图例/卡片用） */
export const ATOM_LABEL = {
  factual: "事实", fact: "事实", episodic: "经历", summary: "经历", preference: "偏好",
  planned: "待办", routine: "惯例", relational: "关系", opinion: "看法", event: "事件", emotion: "情绪",
};
const ENT_KINDS = { entity: 1, topic: 1, person: 1, org: 1, place: 1, thing: 1, other: 1 };

/** 关系类型 → 线的颜色（与 graph.js 的 GRAPH_PALETTE.link 同表同值，2D/3D 表达对齐）。
 *  以前 3D 引擎把边的类型存进 kind 却从不读，线色取的是**两端节点的颜色** —— 于是
 *  semantic/temporal/entity/about/in_session/member_of 六种关系在 3D 里长得一模一样，
 *  用户没法从线的外观区分关系类型（2D 那边反而是有类型配色的，属于两个视图不一致）。 */
const LINK_TINT_HEX = {
  semantic: "#0074d9", temporal: "#009296", entity: "#f59e0b",
  about: "#22c55e", in_session: "#e11d48", member_of: "#14b8a6",
};
const LINK_TINT = (() => {
  const out = {};
  for (const k of Object.keys(LINK_TINT_HEX)) {
    const h = LINK_TINT_HEX[k];
    out[k] = [parseInt(h.slice(1, 3), 16) / 255, parseInt(h.slice(3, 5), 16) / 255, parseInt(h.slice(5, 7), 16) / 255];
  }
  return out;
})();
const ATOM_KINDS = {
  factual: 1, episodic: 1, preference: 1, planned: 1, routine: 1, relational: 1,
  opinion: 1, event: 1, emotion: 1, fact: 1, summary: 1,
};

/* ==================================================================
   元数据：页面的「显示样式菜单」直接读这些生成 UI
   ================================================================== */

/** 语义分层：层 = 节点在这张图里的**角色**（后端 graph() 直接给 role/level，前端不猜、更不随机）。
 *  L0 人 → L1 群/会话 → L2 话题/实体 → L3 记忆，每一层都能一句话说清"它为什么在这一圈"。 */
export const LEVEL_DEFS = [
  { name: "人", en: "PEOPLE", size: 0.78, def: "跟当前人格说过话的人（私聊对象）" },
  { name: "群 · 会话", en: "GROUPS", size: 0.52, def: "这段对话发生在哪个群 / 哪个会话" },
  { name: "话题 · 实体", en: "TOPICS", size: 0.30, def: "聊天里反复出现的话题、事物、事件" },
  { name: "记忆", en: "MEMORIES", size: 0.185, def: "具体某一条记忆（点开看原文证据）" },
];

/** 层号 → 半径尺寸（非「角色」分层依据下也可能有 5~6 层，这里给一条完整尺寸梯度） */
const LAYER_SIZES = [0.78, 0.52, 0.30, 0.185, 0.155, 0.135];

/** 角色 → 层号（后端没给 level 时的兜底） */
const ROLE_LVL = { person: 0, session: 1, topic: 2, memory: 3 };

export const LAYOUT_DEFS = [
  { key: "cone", label: "锥形分层", en: "CONTOURED LAYER TOPOLOGY", short: "锥形", spinLevel: 0, view: [0.55, 22, -1.0] },
  { key: "rings", label: "环塔相扣", en: "STACKED RING TOWERS", short: "环塔", spinLevel: 0.28, view: [0.95, 25, -3.4] },
  { key: "helix", label: "螺旋上升", en: "HELICAL LAYER SPIRAL", short: "螺旋", spinLevel: 0.1, view: [0.1, 26, -2.4] },
  { key: "shell", label: "同心球壳", en: "CONCENTRIC SHELL SPHERES", short: "球壳", spinLevel: 0.06, view: [0.2, 22, 0] },
  { key: "planes", label: "分层平面", en: "LAYERED DATA PLANES", short: "平面", spinLevel: 0, view: [0.16, 24, -1.4] },
  { key: "force", label: "力导向云", en: "3D FORCE-DIRECTED CLOUD", short: "力导", spinLevel: 0.03, view: [0.22, 22, 0] },
  { key: "hang", label: "垂吊立柱", en: "HANGING COLUMN TREE", short: "垂吊", spinLevel: 0, view: [0.1, 22, -0.4] },
  { key: "galaxy", label: "银河旋臂", en: "GALACTIC SPIRAL ARMS", short: "旋臂", spinLevel: 0, view: [0.55, 20, 2.0] },
  { key: "dna", label: "双螺旋链", en: "DOUBLE HELIX CHAINS", short: "双螺", spinLevel: 0.12, view: [0.08, 24, -2.2] },
  { key: "cube", label: "立方点阵", en: "CUBIC LATTICE STACK", short: "点阵", spinLevel: 0.04, view: [0.3, 20, 0] },
  { key: "burst", label: "放射星芒", en: "RADIAL BURST SPOKES", short: "星芒", spinLevel: 0.07, view: [0.24, 21, -1.2] },
  { key: "wave", label: "波浪曲面", en: "WAVE SURFACE FIELD", short: "波面", spinLevel: 0.05, view: [0.22, 22, 0.4] },
];
export const LAYOUT_HOTKEYS = ["1", "2", "3", "4", "5", "6", "7", "8", "9", "0", "q", "w"];

export const CAM_DEFS = [
  { key: "orbit", label: "环绕", pitch: 0.2, dist: 21, ty: -1.0 },
  { key: "top", label: "俯视", pitch: 1.28, dist: 19.5, ty: -1.4 },
  { key: "side", label: "侧视", pitch: 0.03, dist: 24, ty: -0.6 },
  { key: "near", label: "近观", pitch: 0.16, dist: 11.5, ty: -0.2 },
];

export const PALETTE_DEFS = [
  { key: "blue_gold", label: "蓝金", cols: ["#fff4d6", "#ffd166", "#ff9a4d", "#ff87a5", "#9fd0ff", "#b9ef8a"] },
  { key: "neon", label: "霓虹", cols: ["#eafffb", "#7dfcd0", "#39c6ff", "#9a7bff", "#ff6ad5", "#ffd166"] },
  { key: "cold", label: "冷银", cols: ["#ffffff", "#dcebff", "#a9c8ff", "#7ba4f0", "#5480d6", "#3a5fae"] },
  { key: "fire", label: "熔岩", cols: ["#fff6cf", "#ffd24a", "#ff9a2e", "#ff5f3c", "#d63a5f", "#8a2450"] },
  { key: "semantic", label: "语义", cols: [] },
];

export const TIER_DEFS = [
  { key: "role", label: "按角色", hint: "人 → 群/会话 → 话题/实体 → 记忆" },
  { key: "strength", label: "按强度", hint: "记忆强度分位：强 → 弱；人/群/话题·实体沉底" },
  { key: "time", label: "按时间", hint: "最近 7 天 / 1 月 / 3 月 / 半年 / 超过半年与无时间沉底" },
  { key: "type", label: "按记忆类型", hint: "事实/经历/偏好/待办/其他/话题实体" },
];

/** 旧值 "degree"（按连接度分位桶，已被判定无意义）统一归到 "role" */
export const normTier = (k) => (k === "degree" || !TIER_DEFS.some((x) => x.key === k) ? "role" : k);

/** 参数默认值（页面滑条读它初始化） */
export const PARA_DEFS = [
  { key: "gap", label: "层间距", min: 0.45, max: 2.2, step: 0.01, value: 1 },
  { key: "rad", label: "展开度", min: 0.5, max: 1.9, step: 0.01, value: 1 },
  { key: "glow", label: "光晕", min: 0.35, max: 2.2, step: 0.01, value: 1 },
  { key: "spin", label: "自转", min: 0, max: 0.6, step: 0.01, value: 0.02 },
  { key: "cap", label: "节点规模", min: 200, max: 3200, step: 50, value: 1600 },
  { key: "nebula", label: "星云亮度", min: 0, max: 2.5, step: 0.05, value: 1 },
];

/** 语义色板（记忆类型 / 实体），和面板其它图保持同一套色 */
const ATOM_COLORS = {
  factual: "#4a90d9", episodic: "#00a3a3", preference: "#8b5cf6", planned: "#f59e0b",
  routine: "#9aa4b2", relational: "#e0559a", opinion: "#03c9d7", event: "#e0559a",
  emotion: "#e0559a", fact: "#4a90d9", summary: "#00a3a3",
};

export function webglAvailable() {
  try {
    const c = document.createElement("canvas");
    return !!(c.getContext("webgl2") || c.getContext("webgl"));
  } catch (err) {
    return false;
  }
}

/** 工厂：能上 GPU 用 WebGL 引擎，否则回落 canvas2D 引擎 */
export function createGraph3D(host, opts) {
  return webglAvailable() ? new Graph3D(host, opts) : new Graph3DCanvas(host, opts);
}

/* ==================================================================
   主引擎
   ================================================================== */

export class Graph3D {
  constructor(host, opts = {}) {
    this.host = host;
    this.opts = opts || {};
    this.mode = opts.layout || "cone";
    this.paletteKey = opts.palette || "blue_gold";
    this.tierMode = normTier(opts.tier);
    this.P = { gap: 1, rad: 1, glow: 1, spin: 0.02, cap: 1600, nebula: 1 };
    this.cam = { yaw: 0.6, pitch: 0.2, dist: 21, tPitch: 0.2, tDist: 21, ty: -1.0, tTy: -1.0 };
    this.camKey = "orbit";
    this.showLinks = true;
    this.showLabels = true;
    this.motion = true;
    this.tour = { on: false, i: 0, t: 0 };
    this.selected = -1;
    this.hover = -1;
    this.focus = [0, 0, 0];
    // 屏幕空间平移（像素）。以前靠改 focus 来做平移/锚点缩放，结果转轴跟着跑，
    // 一转就像整张图在乱飘；现在旋转永远绕 focus，平移只推画面，二者彻底分开。
    this.pan = { x: 0, y: 0 };
    this.searchHits = null;
    this.searchSet = new Set();
    this.nodes = [];
    this.links = [];
    this._subs = {};
    this._morph = 1;
    this._spinPhase = 0;
    this._raf = 0;
    this._running = false;
    this._fps = 0;
    this._frames = 0;
    this._fpsT = 0;
    this._wdT = 0;
    this._bgRefixes = 0;
    this._last = 0;
    this._quality = "high";
    this._lowCount = 0;
    this._lineBoost = 1;          // 线可见度补偿系数，setData 里按线数重算（见那里的注释）
    this._pins = new Map();
    // 烘焙纹理尺寸：1024×640 够细腻，且分 3 片烤，每片只占几毫秒
    this.BAKE_W = 1024;
    this.BAKE_H = 640;
    this._bakeStep = 0;
    this._built = false;
    this._build();
    this._bind();
    this._alphas();
    this._kick();
  }

  /* ---------------- 结构 ---------------- */

  _build() {
    const h = this.host;
    h.textContent = "";
    try {
      if (getComputedStyle(h).position === "static") h.style.position = "relative";
    } catch (err) {
      h.style.position = "relative";
    }
    h.style.overflow = "hidden";
    const gl = document.createElement("canvas");
    gl.style.cssText = "position:absolute;inset:0;width:100%;height:100%;display:block;cursor:grab;touch-action:none";
    const fx = document.createElement("canvas");
    fx.style.cssText = "position:absolute;inset:0;width:100%;height:100%;display:block;pointer-events:none";
    h.appendChild(gl);
    h.appendChild(fx);
    this.glCanvas = gl;
    this.fxCanvas = fx;
    this.fx = fx.getContext("2d", { alpha: true });
    this._initGL();
    this.resize();
  }

  _initGL() {
    const attrs = { alpha: false, antialias: true, depth: false, premultipliedAlpha: false, preserveDrawingBuffer: true, powerPreference: "high-performance" };
    let gl = this.glCanvas.getContext("webgl2", attrs);
    this.isGL2 = !!gl;
    if (!gl) gl = this.glCanvas.getContext("webgl", attrs);
    this.gl = gl;
    if (!gl) throw new Error("WebGL 不可用");
    gl.disable(gl.DEPTH_TEST);
    gl.clearColor(0.004, 0.008, 0.02, 1);

    const sh = (type, src) => {
      const s = gl.createShader(type);
      gl.shaderSource(s, src);
      gl.compileShader(s);
      if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) console.warn("[hf3d] shader 编译失败", gl.getShaderInfoLog(s));
      return s;
    };
    const prog = (vs, fs, list) => {
      const p = gl.createProgram();
      gl.attachShader(p, sh(gl.VERTEX_SHADER, vs));
      gl.attachShader(p, sh(gl.FRAGMENT_SHADER, fs));
      list.forEach((a, i) => gl.bindAttribLocation(p, i, a));
      gl.linkProgram(p);
      if (!gl.getProgramParameter(p, gl.LINK_STATUS)) console.warn("[hf3d] program 链接失败", gl.getProgramInfoLog(p));
      const u = {};
      const nu = gl.getProgramParameter(p, gl.ACTIVE_UNIFORMS);
      for (let i = 0; i < nu; i++) {
        const inf = gl.getActiveUniform(p, i);
        u[inf.name] = gl.getUniformLocation(p, inf.name);
      }
      return { p, u };
    };

    /*
     * 背景星云：三套程序 + 两张烘焙纹理
     *   BAKE  —— 昂贵的那部分（三层域扭曲 fbm、多发射线配色、尘埃带、核心电离区）
     *           只在启动时按水平切片烤 3 帧，烤进一张纹理里；
     *   STAR  —— 另一张纹理：四层星场 + 亮星衍射星芒（跟气体分层，形成视差）
     *   BG    —— 每帧只做两次纹理采样 + 暗角 + 抖动，成本几乎为零（细节却不受限）
     * 依据：iq 的域扭曲（fbm(p+4·fbm(p+4·fbm(p)))）才长得出星云的丝状结构；
     *       配色按真实发射线走（Hα 品红/红、OIII 青、紫罗兰、金核、尘埃吸收带），
     *       只用一个蓝色梯度永远不像星云。
     */
    this.BAKE = prog(
      `attribute vec2 aP;varying vec2 vUv;void main(){vUv=aP*0.5+0.5;gl_Position=vec4(aP,0.0,1.0);}`,
      `precision highp float;varying vec2 vUv;uniform vec2 uRes;
float h21(vec2 p){p=fract(p*vec2(123.34,456.21));p+=dot(p,p+45.32);return fract(p.x*p.y);}
float vn(vec2 p){vec2 i=floor(p),f=fract(p);f=f*f*(3.0-2.0*f);
 return mix(mix(h21(i),h21(i+vec2(1,0)),f.x),mix(h21(i+vec2(0,1)),h21(i+vec2(1,1)),f.x),f.y);}
float fbm(vec2 p){float s=0.0,a=0.5;for(int i=0;i<7;i++){s+=a*vn(p);p=p*2.06+vec2(11.3,7.7);a*=0.5;}return s;}
// 三相配色：借参考文件（星云/index.html）的色彩逻辑，但这里是 2D 烘焙、不算体积，
// 每帧依旧只采一次纹理 → 观感上去了、性能几乎不变。
vec3 warmPhase(float t){                       // 暖发射：暗绯红 → 橙 → 金（稀薄处必须暗，否则整屏糊成橙雾）
 vec3 c=mix(vec3(0.20,0.022,0.075),vec3(0.98,0.40,0.15),t*t);
 return mix(c,vec3(1.00,0.84,0.42),clamp((t-0.62)*2.4,0.0,1.0)*0.9);}
vec3 coldPhase(float t){                       // 冷反射：暗靛 → 青（只当点缀，否则整屏泛蓝灰）
 vec3 c=mix(vec3(0.016,0.030,0.10),vec3(0.20,0.62,0.72),t*t);
 return mix(c,vec3(0.62,0.82,0.90),clamp((t-0.62)*2.0,0.0,1.0)*0.3);}
void main(){
 float asp=uRes.x/max(1.0,uRes.y);
 // 铺满整张纹理（原来缩在中心一小块 → 转个镜头那片就跑出画面）
 vec2 p=vec2((vUv.x-0.5)*asp,(vUv.y-0.5))*2.05+vec2(0.63,0.31);
 vec2 q=vec2(fbm(p),fbm(p+vec2(5.2,1.3)));
 vec2 r=vec2(fbm(p+3.4*q+vec2(1.7,9.2)),fbm(p+3.4*q+vec2(8.3,2.8)));
 float f=fbm(p+3.0*r);
 float ridge=1.0-abs(2.0*f-1.0);                       // 脊线（激波丝）
 float f2=fbm(p*2.55+2.0*r+vec2(31.3,12.7));
 float ridge2=1.0-abs(2.0*f2-1.0);
 // 低频疏密：永远留一层薄纱（任何取景方向都是星云场），厚薄交给它
 float mask=fbm(p*0.26+vec2(3.1,7.7));
 float veil=0.55+0.45*smoothstep(0.18,0.90,mask);
 // 软对比：不要把一半像素直接切成 0（那半屏就是纯黑）；最低也留 0.25 的气体
 float shape=0.10+0.90*smoothstep(0.40,0.88,f);   // 别叫 struct —— GLSL 保留字，会整个编译失败
 float cloud=shape*(0.35+0.65*veil);
 float filament=pow(ridge,4.0)*smoothstep(0.38,0.86,f);
 float thread=pow(ridge2,5.0)*smoothstep(0.44,0.88,f2);
 // 相位场：决定这一块是"暖发射"还是"冷反射"（参考文件的 key 手法）
 float phase=0.5+0.5*sin(1.2*(r.x*1.7+r.y*2.3-ridge*1.5)+q.x*0.7+p.x*0.55+p.y*0.35);
 float ps=smoothstep(0.46,0.74,phase);   // 暖区占多数
 // 三颗电离核心：任何方向都能撞见 1~2 颗，而不是只有一颗会飘走的"太阳"
 vec2 c1=vec2(0.86,0.30), c2=vec2(-0.76,0.56), c3=vec2(0.12,-0.82);
 float g1=exp(-length((p-c1)*vec2(0.62,0.95))*1.55);
 float g2=exp(-length((p-c2)*vec2(0.85,0.72))*1.35);
 float g3=exp(-length((p-c3)*vec2(0.70,1.05))*1.45);
 float glow=max(max(g1,g2),g3);
 float dust=fbm(p*1.70+3.5*r+vec2(21.7,9.3));
 float lane=smoothstep(0.42,0.86,dust);
 float band=exp(-pow((p.y*0.50-p.x*0.28+0.03)*2.2,2.0));   // 银河带
 // 三相着色
 float dn=clamp(cloud*1.9,0.0,1.0);
 vec3 col=mix(warmPhase(dn),coldPhase(dn),ps);
 col=mix(col,vec3(1.00,0.96,0.88),clamp(ridge*ridge*1.7-0.95,0.0,1.0)*0.50);  // 激波白丝（要稀疏）
 col+=vec3(0.30,0.10,0.34)*thread*(0.4+0.6*veil)*0.45;                        // 紫罗兰细丝
 col+=vec3(0.32,0.20,0.07)*band*veil*0.30;                                    // 银河带暖尘
 col+=vec3(1.00,0.72,0.30)*pow(glow,2.4)*0.80;                                // 金核
 // 稀薄处回到"中性近黑"而不是蓝（原来 r<g<b，那层蓝就是藏青）
 col=mix(vec3(0.0118,0.0100,0.0096),col,clamp(cloud*2.2,0.0,1.0));   // 中性底略偏暖（不能偏蓝，偏蓝就是藏青）
 col*=1.0-0.60*lane*cloud;                                                    // 尘埃吸收带
 col*=0.42+0.78*veil;                                                         // 大尺度明暗（暗带 + 亮脊）
 col=1.0-exp(-col*1.05);
 col*=0.62;                                                                   // 整体压一档：星云是背景，节点必须最亮
 float l=dot(col,vec3(0.2126,0.7152,0.0722));
 col=mix(vec3(l),col,1.22);                                                   // 提饱和：星云要"有色"
 gl_FragColor=vec4(col,1.0);}`,
      ["aP"]
    );

    this.STAR = prog(
      `attribute vec2 aP;varying vec2 vUv;void main(){vUv=aP*0.5+0.5;gl_Position=vec4(aP,0.0,1.0);}`,
      `precision highp float;varying vec2 vUv;uniform vec2 uRes;
float h21(vec2 p){p=fract(p*vec2(123.34,456.21));p+=dot(p,p+45.32);return fract(p.x*p.y);}
vec3 layer(vec2 p,float scale,float dens,float mag){
 vec2 s=p*scale;vec2 id=floor(s),fr=fract(s);
 float h=h21(id);if(h<dens)return vec3(0.0);
 vec2 cc=vec2(0.15+0.7*h21(id+1.7),0.15+0.7*h21(id+9.3));
 float d=length(fr-cc);
 float b=pow(max(0.0,1.0-d*3.4),7.0);
 vec3 tc=mix(vec3(0.66,0.79,1.0),vec3(1.0,0.86,0.62),h21(id+5.5));
 tc=mix(tc,vec3(1.0,0.62,0.42),step(0.93,h21(id+3.1)));
 return tc*b*mag*(0.35+0.9*h21(id+2.2));}
vec3 spikes(vec2 p){
 vec2 s=p*15.0;vec2 id=floor(s),fr=fract(s);
 float h=h21(id+13.7);if(h<0.9962)return vec3(0.0);
 vec2 d=fr-vec2(0.15+0.7*h21(id+1.7),0.15+0.7*h21(id+9.3));
 float core=exp(-dot(d,d)*180.0);
 float k=90.0+50.0*h21(id+4.4);
 float sp=exp(-abs(d.x)*k)*pow(max(0.0,1.0-abs(d.y)*2.6),3.0)
        + exp(-abs(d.y)*k)*pow(max(0.0,1.0-abs(d.x)*2.6),3.0);
 vec3 tc=mix(vec3(0.72,0.84,1.0),vec3(1.0,0.90,0.70),h21(id+8.1));
 return tc*(core*1.5+sp*0.5);}
void main(){
 float asp=uRes.x/max(1.0,uRes.y);
 vec2 p=vec2((vUv.x-0.5)*asp,vUv.y-0.5);
 vec3 c=layer(p,124.0,0.9755,0.72)+layer(p,62.0,0.9885,1.00)+layer(p,28.0,0.9948,1.35)+layer(p,16.0,0.9978,1.75);
 c+=spikes(p);
 c*=1.0-0.45*pow(length(p*vec2(0.85,1.10)),2.2);
 gl_FragColor=vec4(c,1.0);}`,
      ["aP"]
    );

    // 每帧的成本：两张纹理 + 暗角 + 抖动
    this.BG = prog(
      `attribute vec2 aP;varying vec2 vUv;void main(){vUv=aP*0.5+0.5;gl_Position=vec4(aP,0.0,1.0);}`,
      `precision highp float;varying vec2 vUv;uniform sampler2D uGas;uniform sampler2D uStar;
uniform vec2 uPar;uniform vec2 uRes1;uniform float uTime;uniform float uStars;uniform float uSimple;uniform float uNeb;
float h21(vec2 p){p=fract(p*vec2(123.34,456.21));p+=dot(p,p+45.32);return fract(p.x*p.y);}
void main(){
 vec2 base=0.06+vUv*0.88;
 // 程序化星云（uNeb1）已经在半分辨率里按相机方向算好了，这里只做放大 + 补细节
 vec3 gas=texture2D(uGas,base).rgb*uNeb;
 vec3 col=gas;
 // 全分辨率细丝：半分辨率放大后会糊，用两阶 2D 噪声补回"絮状纤维"，成本极低
 float lumGas=dot(gas,vec3(0.2126,0.7152,0.0722));
 // 原来这里加了一层全分辨率细丝当"细节"，实际看是**满屏噪点颗粒**（用户明确否掉）。
 // 现在只留极弱的一点点，且只在星云较亮处出现。
 float n1=fract(sin(dot(floor(vUv*260.0),vec2(127.1,311.7)))*43758.5453123);
 float n2=fract(sin(dot(floor(vUv*113.0+7.3),vec2(269.5,183.3)))*43758.5453123);
 float fix=(n1*0.6+n2*0.4-0.5)*0.30;
 col+=vec3(1.00,0.72,0.55)*clamp(lumGas*5.0-0.5,0.0,1.0)*max(fix,0.0)*0.13;   // 极弱亮丝
 col-=vec3(0.50,0.40,0.35)*clamp(lumGas*5.0-0.5,0.0,1.0)*max(-fix,0.0)*0.10;  // 极弱暗带
 if(uSimple<0.5&&uStars>0.0){
   vec3 st=texture2D(uStar,base+vec2(sin(uPar.x*0.9)*0.12,sin(uPar.y*1.2)*0.09)).rgb;
   // 闪烁：按屏幕位置错开相位，避免整屏一起亮暗
   float tw=0.80+0.20*sin(uTime*1.6+(vUv.x*37.0+vUv.y*53.0));
   col+=st*tw*uStars;
 }
 col*=1.0-0.34*pow(length((vUv-0.5)*vec2(0.92,1.16)),2.2);   // 暗角（收一档：原来四角被压得发空）
 float l=dot(col,vec3(0.2126,0.7152,0.0722));
 col=mix(vec3(l),col,1.14);                                  // 轻微提饱和
 col=1.0-exp(-col*1.12);
 col+=(h21(vUv*uRes1+vUv.y*uTime)-0.5)*0.010;                 // 抖动去色带
 gl_FragColor=vec4(col,1.0);}`,
      ["aP"]
    );

    // 点精灵：halo + core 双峰叠加 = 发光球手感
    // 真·程序化星云：一个无限的三维噪声场，镜头一转就是在里面穿行。
    // 所以永远不会"飘出去回不来"，也不存在边界。代价用半分辨率 + 限帧压住（见 _renderNeb）。
    this.NEB = prog(
      `attribute vec2 aP;void main(){gl_Position=vec4(aP,0.0,1.0);}`,
      `precision highp float;
uniform vec2 uRes;uniform float uTime;uniform vec2 uPar;uniform float uNeb;
float h13(vec3 p){return fract(sin(dot(p,vec3(127.1,311.7,74.7)))*43758.5453123);}
float n3(vec3 p){vec3 i=floor(p),f=fract(p);f=f*f*(3.0-2.0*f);
 return mix(mix(mix(h13(i),h13(i+vec3(1,0,0)),f.x),mix(h13(i+vec3(0,1,0)),h13(i+vec3(1,1,0)),f.x),f.y),
            mix(mix(h13(i+vec3(0,0,1)),h13(i+vec3(1,0,1)),f.x),mix(h13(i+vec3(0,1,1)),h13(i+vec3(1,1,1)),f.x),f.y),f.z);}
float fb(vec3 p,int oct){float s=0.0,a=0.5;for(int i=0;i<5;i++){if(i>=oct)break;s+=a*n3(p);p=p*2.07+vec3(11.3,7.7,3.1);a*=0.5;}return s;}
/* 三相配色：暖发射(绯红→橙→金) / 冷反射(靛→青) / 激波白丝 */
vec3 ramp(float d,float phase,float ridge){
 vec3 warm=mix(vec3(0.40,0.035,0.15),vec3(1.00,0.42,0.16),d*d);
 warm=mix(warm,vec3(1.00,0.83,0.45),pow(d,3.0)*0.95);
 vec3 cold=mix(vec3(0.035,0.055,0.22),vec3(0.20,0.60,0.72),d*d);
 vec3 c=mix(warm,cold,smoothstep(0.52,0.86,phase)*0.75);   // 暖区为主，青只当点缀（参考图就是绯红/橙/紫打底）
 return mix(c,vec3(1.00,0.97,0.92),pow(ridge,7.0)*0.60);}
void main(){
 // 以画面高度归一 → 任意宽高比都不变形
 vec2 uv=(gl_FragCoord.xy-0.5*uRes)/uRes.y;
 vec3 dir=normalize(vec3(uv,1.75));
 float cy=cos(uPar.x),sy=sin(uPar.x),cp=cos(uPar.y),sp=sin(uPar.y);
 vec3 rd=vec3(dir.x*cy+dir.z*sy,dir.y,-dir.x*sy+dir.z*cy);      // yaw
 rd=vec3(rd.x,rd.y*cp-rd.z*sp,rd.y*sp+rd.z*cp);                // pitch
 float t=uTime*0.012; vec3 acc=vec3(0.0); float T=1.0;
 for(int i=0;i<5;i++){                       // 5 层切片：够体量又不至于满屏噪点（用户反馈 8 层太吵）
  float fi=float(i);
  vec3 p=rd*(1.7+fi*1.25)+vec3(0.0,0.0,t)+vec3(3.1,7.7,1.3);   // 尺度放大 → 大团块，才够'浓密'
  float w1=fb(p,2), w2=fb(p+vec3(5.2,1.3,2.8),2);
  vec3 q=vec3(w1,w2,w1*0.7+w2*0.5);
  float f=fb(p+2.5*q,3);
  float ridge=1.0-abs(2.0*f-1.0);
  float mask=0.30+0.70*smoothstep(0.30,0.95,fb(p*0.40+vec3(2.3,1.9,5.1),2));
  float d=smoothstep(0.30,0.84,f)*mask;
  float phase=0.5+0.5*sin(1.1*(q.x*1.7+q.y*2.1-ridge*1.4)+fi*0.8);
  vec3 c=ramp(d,phase,ridge);
  float a=d*exp(-fi*0.26)*(1.0-fi*0.055);
  acc+=c*a*T; T*=1.0-a*0.50;
 }
 acc*=0.38;   // 柔和但看得见：真正的噪点感来自上面那层全屏细丝，已单独大幅削弱
 gl_FragColor=vec4(acc*uNeb,1.0);}`,
      ["aP"]
    );

    this.PT = prog(
      `attribute vec3 aPos;attribute vec3 aCol;attribute float aSize;attribute float aA;
uniform mat4 uMVP;uniform float uPS;uniform float uGlow;varying vec3 vC;varying float vA;
void main(){vec4 c=uMVP*vec4(aPos,1.0);gl_Position=c;
 gl_PointSize=clamp(aSize*uPS/max(c.w,0.35)*uGlow,1.0,42.0);vC=aCol;vA=aA;}`,
      `precision mediump float;varying vec3 vC;varying float vA;
void main(){float d=length(gl_PointCoord-vec2(0.5))*2.0;if(d>1.0){discard;}
 float halo=pow(1.0-d,2.3);float core=smoothstep(0.62,0.06,d);
 vec3 c=vC*(halo*1.35+core*0.8)+vec3(core)*0.12;gl_FragColor=vec4(c*vA,1.0);}`,
      ["aPos", "aCol", "aSize", "aA"]
    );

    // 关系线：顶点色 × alpha，加色混合
    this.LN = prog(
      `attribute vec3 aPos;attribute vec3 aCol;attribute float aA;
uniform mat4 uMVP;varying vec3 vC;varying float vA;
void main(){vec4 c=uMVP*vec4(aPos,1.0);gl_Position=c;vC=aCol;vA=aA;}`,
      `precision mediump float;varying vec3 vC;varying float vA;
void main(){gl_FragColor=vec4(vC*vA,1.0);}`,
      ["aPos", "aCol", "aA"]
    );

    const B = () => gl.createBuffer();
    this.buf = { quad: B(), nPos: B(), nCol: B(), nSize: B(), nA: B(), ePos: B(), eCol: B(), eA: B() };

    // 烘焙目标：两张纹理（气体 / 星空）+ 一个 FBO。开 REPEAT 不行（边缘会撕），
    // 所以 BG 里采样时留 7% 边距再叠视差偏移。
    const mkTex = () => {
      const t = gl.createTexture();
      gl.bindTexture(gl.TEXTURE_2D, t);
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, this.BAKE_W, this.BAKE_H, 0, gl.RGBA, gl.UNSIGNED_BYTE, null);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
      return t;
    };
    this.texGas = mkTex();
    this.texStar = mkTex();
    this.fbo = gl.createFramebuffer();
    // 程序化星云渲染目标（半分辨率：软云放大看不出来，省一半以上算力）
    this.texNeb = gl.createTexture();
    this._texNebSize = [0, 0];
    this.nebFbo = gl.createFramebuffer();
    this._bakeQueue = [];
    gl.bindBuffer(gl.ARRAY_BUFFER, this.buf.quad);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 3, -1, -1, 3]), gl.STATIC_DRAW);
    this._mat = { proj: new Float32Array(16), view: new Float32Array(16), mvp: new Float32Array(16) };
  }

  /**
   * 程序化星云：半分辨率渲染 + 限帧（相机不动就复用上一帧）。
   * 真·程序化，不是贴图：转镜头 = 在三维噪声场里穿行，永远不会有边界或"飘走"。
   */
  _renderNeb(tms) {
    const gl = this.gl;
    const size = this._nebSize || [
      Math.max(64, Math.round(this.glCanvas.width * 0.5)),
      Math.max(64, Math.round(this.glCanvas.height * 0.5)),
    ];
    const W = size[0], H = size[1];
    if (this._texNebSize[0] !== W || this._texNebSize[1] !== H) {
      gl.bindTexture(gl.TEXTURE_2D, this.texNeb);
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, W, H, 0, gl.RGBA, gl.UNSIGNED_BYTE, null);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
      this._texNebSize = [W, H];
      this._nebT = -1e9;
    }
    // 约 20fps 重算；相机一转立刻重算（拖视角绝不能糊）
    const moved = Math.abs(this.cam.yaw - (this._nebYaw || 0)) + Math.abs(this.cam.pitch - (this._nebPitch || 0)) > 0.003;
    if (!moved && this._nebT && tms - this._nebT < 48) return;
    this._nebT = tms;
    this._nebYaw = this.cam.yaw;
    this._nebPitch = this.cam.pitch;
    gl.bindFramebuffer(gl.FRAMEBUFFER, this.nebFbo);
    gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, this.texNeb, 0);
    gl.viewport(0, 0, W, H);
    gl.disable(gl.BLEND);
    gl.useProgram(this.NEB.p);
    gl.bindBuffer(gl.ARRAY_BUFFER, this.buf.quad);
    gl.enableVertexAttribArray(0);
    gl.vertexAttribPointer(0, 2, gl.FLOAT, false, 0, 0);
    gl.uniform2f(this.NEB.u.uRes, W, H);
    gl.uniform1f(this.NEB.u.uTime, tms * 0.001);
    gl.uniform2f(this.NEB.u.uPar, this.cam.yaw, this.cam.pitch);
    gl.uniform1f(this.NEB.u.uNeb, this.P.nebula == null ? 1 : this.P.nebula);
    gl.drawArrays(gl.TRIANGLES, 0, 3);
    gl.bindFramebuffer(gl.FRAMEBUFFER, null);
    gl.viewport(0, 0, this.glCanvas.width, this.glCanvas.height);
  }

  /** 上下文恢复后重建一切（贴图/程序/缓冲区都没了，必须重烤星云，否则背景永远黑） */
  _rebuildGL() {
    // ⚠️ 上下文确实丢失时，**不能只在原画布上再 getContext**：拿回来的还是同一个已丢失的
    //    上下文，后续 createProgram/attachShader 会抛错 → 这里 catch → _ctxLost 永远清不掉
    //    → _tickInner 永远卡在 lost 早退分支（_upload/_draw 一次都不执行）
    //    → 画布冻在旧帧，用户只能刷新页面。唯一可靠的复活方式：换一块全新画布 + 全新上下文。
    //    （_bind 可安全重复调用：同一函数引用的 addEventListener 会被浏览器去重。）
    let needFresh = false;
    try { needFresh = !this.gl || (this.gl.isContextLost && this.gl.isContextLost()); } catch (err) { needFresh = true; }
    if (needFresh) {
      try {
        this._build();
        this._bind();
      } catch (err) {
        if (typeof console !== "undefined") console.warn("[hf3d] 重建画布失败", err);
      }
    }
    try {
      this._initGL();
      this._bakeStep = 0;
      this._built = false;
      this._bakeFailed = null;
      this._bakeAll();
      this._prepareDraw();
      this._running = true;
      this._kick();
      this._ctxFail = 0;
      if (typeof console !== "undefined") console.warn("[hf3d] WebGL 上下文已恢复，星云重新烘焙完成");
    } catch (err) {
      this._fatal = "上下文恢复失败：" + String((err && err.message) || err);
      if (typeof console !== "undefined") console.warn("[hf3d] 上下文恢复失败", err);
      throw err;                  // 交给调用方决定（_tickInner 里有连续失败兜底）
    }
  }

  /**
   * 星云自愈看门狗：偶尔整块背景变黑（贴图被驱动/显存回收），刷新网页才好。
   * 每 ~4 秒采一次缩略画面，若整片纯黑且没丢上下文、星云亮度也不是 0，就重烤一次。
   */
  _bgWatchdog() {
    if (this._ctxLost || this._bakeFailed) return;
    if ((this.P.nebula == null ? 1 : this.P.nebula) < 0.05) return;
    if (this._buildStep === undefined) this._buildStep = 0;
    const w = 64, h = 40;
    const px = this._bgProbeBuf || (this._bgProbeBuf = new Uint8Array(w * h * 4));
    try {
      this.gl.bindFramebuffer(this.gl.FRAMEBUFFER, null);
      this.gl.readPixels(0, 0, w, h, this.gl.RGBA, this.gl.UNSIGNED_BYTE, px);
    } catch (err) {
      return;
    }
    let lit = 0;
    for (let i = 0; i < px.length; i += 4) if (px[i] + px[i + 1] + px[i + 2] > 26) lit++;
    if (lit === 0) {
      // 注意：只采画布左下角 64×40 一小块，命中率低所以只在"全黑"时才判定
      this._bgDarkHits = (this._bgDarkHits || 0) + 1;
      if (this._bgDarkHits >= 3) {
        this._bgDarkHits = 0;
        this._bgRefixes = (this._bgRefixes || 0) + 1;
        this._bakeStep = 0;
        this._built = false;
        this._bakeAll();
        this._prepareDraw();
        if (typeof console !== "undefined") console.warn("[hf3d] 背景疑似失效，已自动重新烘焙星云");
      }
    } else {
      this._bgDarkHits = 0;
    }
  }

  resize() {
    const h = this.host;
    const w = Math.max(160, h.clientWidth || 640);
    const ht = Math.max(160, h.clientHeight || 420);
    const dprCap = this._quality === "low" ? 1.25 : 2;
    const dpr = Math.min(dprCap, (typeof devicePixelRatio === "number" && devicePixelRatio) || 1);
    this.W = w;
    this.H = ht;
    this.DPR = dpr;
    this._nebSize = [Math.max(64, Math.round(w * dpr * 0.5)), Math.max(64, Math.round(ht * dpr * 0.5))];
    this.glCanvas.width = Math.round(w * dpr);
    this.glCanvas.height = Math.round(ht * dpr);
    this.fxCanvas.width = Math.round(w * dpr);
    this.fxCanvas.height = Math.round(ht * dpr);
    this.fx.setTransform(dpr, 0, 0, dpr, 0, 0);
    if (this.gl) this.gl.viewport(0, 0, this.glCanvas.width, this.glCanvas.height);
    this.FOV = (48 * Math.PI) / 180;
    this._kick();
  }

  setHost(el) {
    if (!el) return this;
    this.host = el;
    this.resize();
    return this;
  }

  /* ---------------- 矩阵 ---------------- */

  _mPersp(fovy, asp, near, far, out) {
    const f = 1 / Math.tan(fovy / 2);
    const nf = 1 / (near - far);
    out[0] = f / asp; out[1] = 0; out[2] = 0; out[3] = 0;
    out[4] = 0; out[5] = f; out[6] = 0; out[7] = 0;
    out[8] = 0; out[9] = 0; out[10] = (far + near) * nf; out[11] = -1;
    out[12] = 0; out[13] = 0; out[14] = 2 * far * near * nf; out[15] = 0;
    return out;
  }

  _mLook(eye, ctr, up, out) {
    let zx = eye[0] - ctr[0], zy = eye[1] - ctr[1], zz = eye[2] - ctr[2];
    const zl = Math.hypot(zx, zy, zz) || 1;
    zx /= zl; zy /= zl; zz /= zl;
    let xx = up[1] * zz - up[2] * zy, xy = up[2] * zx - up[0] * zz, xz = up[0] * zy - up[1] * zx;
    const xl = Math.hypot(xx, xy, xz) || 1;
    xx /= xl; xy /= xl; xz /= xl;
    const yx = zy * xz - zz * xy, yy = zz * xx - zx * xz, yz = zx * xy - zy * xx;
    out[0] = xx; out[1] = yx; out[2] = zx; out[3] = 0;
    out[4] = xy; out[5] = yy; out[6] = zy; out[7] = 0;
    out[8] = xz; out[9] = yz; out[10] = zz; out[11] = 0;
    out[12] = -(xx * eye[0] + xy * eye[1] + xz * eye[2]);
    out[13] = -(yx * eye[0] + yy * eye[1] + yz * eye[2]);
    out[14] = -(zx * eye[0] + zy * eye[1] + zz * eye[2]);
    out[15] = 1;
    return out;
  }

  _mMul(a, b, out) {
    for (let i = 0; i < 4; i++) {
      for (let j = 0; j < 4; j++) {
        out[i * 4 + j] =
          a[0 * 4 + j] * b[i * 4 + 0] + a[1 * 4 + j] * b[i * 4 + 1] +
          a[2 * 4 + j] * b[i * 4 + 2] + a[3 * 4 + j] * b[i * 4 + 3];
      }
    }
    return out;
  }

  /* ---------------- 数据 ---------------- */

  setData(payload) {
    const p = payload || {};
    const raw = Array.isArray(p.nodes) ? p.nodes : [];
    const rl = Array.isArray(p.links) ? p.links : [];
    // ⚠️ 不能对 payload 做「无条件取前 N 个」的截断：后端组装顺序是
    //   记忆 → 实体 → 派生「人/群」，而记忆量最大。一旦记忆数接近上限，
    //   排在后面的**人 / 群 / 话题三层会被整层切掉**；紧接着下面
    //   「端点不在 nodes 里的 link 直接 continue」会把全部 entity/about/in_session 线一起丢掉。
    //   表现就是用户报的「切到全库再切回来，线没了」。
    // 现在：语义骨架（非 memory 的节点）无条件保留，剩余预算才用来装记忆。
    const hardCap = clamp(Number(p.limit) || 12000, 100, 20000);
    let kept = raw;
    if (raw.length > hardCap) {
      const skel = [];
      const mem = [];
      for (let i = 0; i < raw.length; i++) {
        const cls = String((raw[i] && (raw[i].group || raw[i].klass)) || "memory");
        (cls === "memory" ? mem : skel).push(raw[i]);
      }
      const budget = Math.max(0, hardCap - skel.length);
      kept = skel.length >= hardCap ? skel.slice(0, hardCap) : skel.concat(mem.slice(0, budget));
    }
    const n = kept.length;
    this.n = n;

    const nodes = new Array(n);
    for (let i = 0; i < n; i++) {
      const r = kept[i];
      // 后端 graph 节点：group=类别(memory/topic)，kind=记忆原子类型 —— 别把 kind 当类别用
      const cls = String(r.group || r.klass || "");
      const rawKind = String(r.kind || "");
      // ⚠️ 实体不只 topic/entity：后端实体节点的 group 取 hf_entities.kind ∈ {person, session, topic}，
      //    派生出来的人/群节点 group 直接就是 "person"/"session"。原来的判定只认 topic/entity，
      //    于是**人物/群聊被判成「记忆」** —— 在「按记忆类型」分层里落到「其他」而不是「话题·实体」、
      //    语义配色里被涂成记忆蓝、图例计数也算错。这里补齐 person/session。
      const isEntity = cls === "topic" || cls === "entity" || cls === "person" || cls === "session"
        || (!cls && (ENT_KINDS[rawKind] || rawKind === "person" || rawKind === "session"));
      // 后端 graph() 直接给语义角色（person/session/topic/memory）与层号 —— 分层用它，不按度数猜
      const role = String(r.role || (isEntity ? "topic" : "memory"));
      const atom = String(r.atom_type || r.atomType || (ATOM_KINDS[rawKind] ? rawKind : ""));
      const st = Number(r.strength);
      nodes[i] = {
        idx: i,                       // ⚠️ links 用的是数组下标（{a,b}），节点卡片算邻居数靠 it.idx ——
                                      // 以前没这个字段，于是「邻居(N)」永远是 0、选中高亮也点不出来
        raw: r,
        id: String(r.id),
        name: String(r.label || r.summary || r.name || r.id || ""),
        isEntity,
        role,
        atom,
        etype: isEntity ? rawKind || cls : "",
        strength: Number.isFinite(st) ? st : 0,
        hasStrength: !isEntity,     // 只有记忆有「强度」；人/群/话题/实体没有 → 分层时沉底
        ts: Number(r.occurred || r.occurred_at || r.ts || 0) || 0,
        session: String(r.session || r.session_id || ""),
        deg: 0,
        seed: hash01(String(r.id)),
        // ⚠️ level 会被 _buildStructure 按当前「分层依据」重写，所以语义层号另存 roleLevel，
        // 否则用户切到"按强度/时间/类型"再切回"按角色"，角色层就被上一模式的值污染了（实测踩过）。
        roleLevel: Number.isFinite(Number(r.level)) ? clamp(Number(r.level), 0, 3) : (ROLE_LVL[role] == null ? 3 : ROLE_LVL[role]),
        level: 0,
        parent: -1,
        ring: 0,
        ord: 0,
      };
    }
    const idxOf = new Map();
    for (let i = 0; i < n; i++) idxOf.set(nodes[i].id, i);

    const E = [];
    const linkCap = clamp(Number(p.linkLimit) || 26000, 100, 40000);
    for (let i = 0; i < rl.length && E.length < linkCap; i++) {
      const l = rl[i];
      if (!l) continue;
      const a = idxOf.get(String(l.source != null ? l.source : l.s));
      const b = idxOf.get(String(l.target != null ? l.target : l.t));
      if (a == null || b == null || a === b) continue;
      const w = clamp(Number(l.weight != null ? l.weight : l.w != null ? l.w : 1) || 1, 0.05, 5);
      E.push({ a, b, w, k: E.length, kind: String(l.type || l.etype || "") });
      nodes[a].deg++;
      nodes[b].deg++;
    }
    this.nodes = nodes;
    this.links = E;
    // —— 线的「可见度补偿」系数 ——
    // 关系线是**加法混合**（gl.blendFunc(ONE, ONE)）：单条线的贡献 = 顶点色 × alpha，
    // 而 alpha 正常只有 0.22×0.36 ≈ 0.079 —— 换算到画布上大约 +6/255，**肉眼几乎为 0**。
    // 大图（上万条线）靠彼此叠加才看起来像一张网；稀疏图（几百条线）就什么都看不到，
    // 这正是用户报的「切到全库再切回当前视角，当前视角就没有连线了」的物理原因
    // （不是没画：底部 HUD 一直显示「N 绘制关系」）。
    // 所以按线数做一次补偿：线越少单条越亮（稀疏图才能看见），
    // 线越多越暗回去（**保持用户认可的"全库"观感**，否则上万条线会糊成一片白）。
    // 参照：用户认为"没问题"的全库约 11361 条 → 系数 0.5（α≈0.0396）；
    //       只有 228 条的小图 → 系数 2.29（α≈0.1817，肉眼可见）。
    this._lineBoost = clamp(Math.sqrt(1200 / Math.max(1, E.length)), 0.5, 2.6);
    // ⚠️ 换数据 = 从头开始：把会让 _tickInner **每帧早退**的开关一并复位。
    //    _ctxLost 残留 / 星云烘焙未完成(_built=false) / 循环已停(_running=false) ——
    //    任何一个没清，_upload/_draw 就一次都不执行、画布永远停在旧帧上，
    //    用户看到的就是「切到全库再切回当前视角，线全没了，只能刷新页面」
    //    （真机逐项测量：UP=0、EP0/NP0=0,0,0、CTXLOST=0、循环仍在跑）。
    this._ctxLost = false;
    this._ctxPollT = 0;
    this._ctxFail = 0;
    if (!this._built) {
      try { this._bakeAll(); } catch (err) { /* 烘焙失败也要放行 */ }
      this._built = true;
    }
    if (!this._running && (!this._everConnected || (this.glCanvas && this.glCanvas.isConnected))) {
      this._kick();
    }
    this._pins.clear();
    this.selected = -1;
    this.hover = -1;
    this._tsRange = null;
    // 换数据 = 从头开始。不复位的话，上一次「全库大图把 FPS 压低」触发的自动降级
    // 会把 _quality 永久锁成 low（绘制上限 ×0.6、只剩度数最高的一小撮节点与线），
    // 切回小范围也仍然停在那个残缺态 —— 这正是「切回当前视角后线消失」的第二个成因。
    // 注意 _simple 是用户自己按的「简版」开关，不能碰。
    this._quality = "high";
    this._lowCount = 0;
    this._fps = 0;
    this._frames = 0;
    this._fpsT = 0;
    this._buildStructure();
    this._layoutInit();
    this._forceReady = false;
    this._forceRun = 0;
    this._forcePos = new Float32Array(Math.max(1, n * 3));
    for (let i = 0; i < n; i++) {
      const u = nodes[i].seed * 2 - 1;
      const s = Math.cbrt(Math.max(0.0001, nodes[i].seed)) * 8;
      this._forcePos[i * 3] = u * s;
      this._forcePos[i * 3 + 1] = Math.sqrt(Math.max(0, 1 - u * u)) * s;
      this._forcePos[i * 3 + 2] = s;
    }
    this.applyPalette();
    this._prepareDraw();
    // 必须重算一次可见度：_prepareDraw 刚换了 _vis / _visLinks，
    // 而 ALP（每个节点/每条线的目标透明度）是按可见集合算的。
    // 原实现漏了这一步 → 换数据后 _visEa() 读到陈旧值、回落到 0.03/0.02（正常基线 0.079），
    // 「线画了但几乎看不见」，与「线消失」在用户眼里是同一件事。
    this._alphas();
    this.setLayout(this.mode, true);
    this.setCam(this.camKey, true);
    this._searchApply();
    this.emit("stats", this.stats());
    return this;
  }

  /** 分层 / 父子 / 环角：层 = 语义角色；强度、时间、类型也**真的重排几何**（旧版这仨只换色不换层，属于假选项） */
  _buildStructure() {
    const n = this.nodes.length;
    const SEM = { factual: 0, fact: 0, episodic: 1, summary: 1, event: 1, emotion: 1, preference: 2, relational: 2, opinion: 2, planned: 3, routine: 3 };
    const lvl = new Int8Array(n);
    const mode = this.tierMode;
    let K = 4;
    if (mode === "type") {
      for (let i = 0; i < n; i++) {
        const it = this.nodes[i];
        lvl[i] = it.isEntity ? 5 : (SEM[it.atom] == null ? 4 : SEM[it.atom]);
      }
      K = 6;
    } else if (mode === "strength") {
      // 记忆强度按**本视图分位数**切 4 档（等量），人 / 群 / 话题 / 实体这些没有强度的节点沉到第 5 层。
      // 为什么不用固定阈值：真机 853 条记忆里 571 条（67%）强度 < 0.2，固定阈值会把三分之二
      // 全堆在最后一层，看不出任何强弱梯度。
      const vals = [];
      for (let i = 0; i < n; i++) if (this.nodes[i].hasStrength) vals.push(this.nodes[i].strength);
      vals.sort((a, b) => a - b);
      const cut = [];
      for (let q = 1; q <= 3; q++) {
        cut.push(vals.length ? vals[Math.min(vals.length - 1, Math.floor((vals.length * q) / 4))] : 0.5);
      }
      this._stCuts = cut;                         // _tierOf（取色/图例）用同一套切点，别各算各的
      for (let i = 0; i < n; i++) {
        const it = this.nodes[i];
        if (!it.hasStrength) { lvl[i] = 4; continue; }        // 没有强度属性 → 一律沉底
        lvl[i] = it.strength >= cut[0] ? 0 : it.strength >= cut[1] ? 1 : it.strength >= cut[2] ? 2 : 3;
      }
      K = 5;
    } else if (mode === "time") {
      // 绝对时间窗，不按 min/max 归一化：7 天 / 1 个月 / 3 个月 / 半年 / 超过半年与无时间。
      // 归一化的问题：加一条新记忆就会让所有层重新洗牌，而且「刚发生的」和「三个月前的」
      // 会被塞进同一层。occurred_at 的单位是**秒**（后端 service.py 用 time.localtime 解析）。
      const DAY = 86400000;
      const nowMs = Date.now();
      const W = [7 * DAY, 30 * DAY, 90 * DAY, 180 * DAY];
      this._timeW = W;
      for (let i = 0; i < n; i++) {
        const t0 = this.nodes[i].ts || 0;
        const age = t0 > 0 ? nowMs - t0 * 1000 : Infinity;
        lvl[i] = age <= W[0] ? 0 : age <= W[1] ? 1 : age <= W[2] ? 2 : age <= W[3] ? 3 : 4;
      }
      K = 5;
    } else {
      // 默认：语义角色（L0 人 → L1 群/会话 → L2 话题/实体 → L3 记忆），层号来自后端 role/level
      for (let i = 0; i < n; i++) lvl[i] = clamp(this.nodes[i].roleLevel | 0, 0, 3);
      K = 4;
    }
    this.layerK = K;
    this.layerCut = [];
    const cnt = new Array(6).fill(0);
    for (let i = 0; i < n; i++) cnt[lvl[i]]++;
    const order = Array.from({ length: n }, (_, i) => i).sort((a, b) => lvl[a] - lvl[b] || this.nodes[b].deg - this.nodes[a].deg);
    const start = new Array(6).fill(0);
    let acc = 0;
    for (let l = 0; l < K; l++) {
      start[l] = acc;
      acc += cnt[l];
    }
    for (let k = 0; k < order.length; k++) {
      const i = order[k];
      this.nodes[i].level = lvl[i];
      this.nodes[i].ordInLevel = k - start[lvl[i]];
    }
    // 父节点：连到「层级更靠近 0 的邻居」里权重最大的那个（下垂/星芒要用）
    const parent = new Int32Array(n).fill(-1);
    const best = new Float32Array(n);
    for (let k = 0; k < this.links.length; k++) {
      const l = this.links[k];
      if (lvl[l.b] < lvl[l.a] && l.w > best[l.a]) { best[l.a] = l.w; parent[l.a] = l.b; }
      if (lvl[l.a] < lvl[l.b] && l.w > best[l.b]) { best[l.b] = l.w; parent[l.b] = l.a; }
    }
    // 环角：同层按「父节点角度均值」聚拢，再均匀铺到环上 —— 有关系的东西在视觉上挨着
    const kids = new Map();
    for (let i = 0; i < n; i++) {
      const p = parent[i];
      if (p >= 0) {
        if (!kids.has(p)) kids.set(p, []);
        kids.get(p).push(i);
      }
    }
    const ang = new Float32Array(n);
    let leafIdx = 0;
    const leafBase = Math.max(1, cnt[K - 1] + cnt[Math.max(0, K - 2)]);
    for (let l = K - 1; l >= 0; l--) {
      for (let k = 0; k < order.length; k++) {
        const i = order[k];
        if (lvl[i] !== l) continue;
        const ch = kids.get(i) || [];
        if (!ch.length) {
          ang[i] = (leafIdx++ / leafBase) * TAU;
        } else {
          let s = 0;
          for (let m = 0; m < ch.length; m++) s += ang[ch[m]];
          ang[i] = s / ch.length;
        }
      }
    }
    const ring = new Float32Array(n);
    for (let l = 0; l < (this.layerK || 4); l++) {
      const ids = [];
      for (let k = 0; k < n; k++) if (lvl[k] === l) ids.push(k);
      ids.sort((a, b) => ang[a] - ang[b]);
      for (let k = 0; k < ids.length; k++) {
        ring[ids[k]] = ((k + 0.5) / Math.max(1, ids.length)) * TAU;
        this.nodes[ids[k]].ord = k;
      }
    }
    for (let i = 0; i < n; i++) {
      this.nodes[i].ring = ring[i];
      this.nodes[i].ang = ang[i];
      this.nodes[i].parent = parent[i];
    }
    this.cnt = cnt;
    this.start = start;
    this._kids = kids;
  }

  _layoutInit() {
    const n = this.nodes.length;
    this.POS = new Float32Array(n * 3);
    this.FROM = new Float32Array(n * 3);
    this.TGT = new Float32Array(n * 3);
    this.COL = new Float32Array(n * 3);
    this.SIZ = new Float32Array(n);
    this.ALP = new Float32Array(n);
    this._morph = 1;
    this._spinPhase = 0;
  }

  /* ---------------- 配色 ---------------- */

  _hex2rgb(hex) {
    const h = String(hex).replace("#", "");
    const v = h.length === 3 ? h.split("").map((c) => c + c).join("") : h;
    return [parseInt(v.slice(0, 2), 16) / 255, parseInt(v.slice(2, 4), 16) / 255, parseInt(v.slice(4, 6), 16) / 255];
  }

  /** 分层依据 → 色序（语义配色时用节点自己的色） */
  _tierOf(i) {
    const it = this.nodes[i];
    switch (this.tierMode) {
      case "role":
        return clamp(it.roleLevel | 0, 0, 3);        // 0 人 / 1 群·会话 / 2 话题·实体 / 3 记忆
      case "type":
        return it.isEntity ? 5
          : it.atom === "factual" || it.atom === "fact" ? 0
          : it.atom === "episodic" || it.atom === "summary" || it.atom === "event" || it.atom === "emotion" ? 1
          : it.atom === "preference" || it.atom === "relational" || it.atom === "opinion" ? 2
          : it.atom === "planned" || it.atom === "routine" ? 3 : 4;
      case "strength": {
        const it2 = this.nodes[i];
        if (!it2.hasStrength) return 4;                     // 没有强度属性 → 与 _buildStructure 同规则沉底
        const c = this._stCuts || [];
        const c0 = c.length > 0 ? c[0] : 0.8;
        const c1 = c.length > 1 ? c[1] : 0.6;
        const c2 = c.length > 2 ? c[2] : 0.4;
        return it2.strength >= c0 ? 0 : it2.strength >= c1 ? 1 : it2.strength >= c2 ? 2 : 3;
      }
      case "time": {
        const it3 = this.nodes[i];
        const W = this._timeW || [7 * 86400000, 30 * 86400000, 90 * 86400000, 180 * 86400000];
        const age = it3.ts > 0 ? Date.now() - it3.ts * 1000 : Infinity;
        return age <= W[0] ? 0 : age <= W[1] ? 1 : age <= W[2] ? 2 : age <= W[3] ? 3 : 4;
      }
      default:
        return it.level;
    }
  }

  applyPalette() {
    if (!this._tsRange) {
      const times = this.nodes.map((x) => x.ts).filter((x) => x > 0);
      this._tsRange = times.length ? [Math.min.apply(null, times), Math.max.apply(null, times)] : [0, 1];
    }
    const pd = PALETTE_DEFS.find((x) => x.key === this.paletteKey) || PALETTE_DEFS[0];
    const cs = pd.cols.map((c) => this._hex2rgb(c));
    for (let i = 0; i < this.nodes.length; i++) {
      const it = this.nodes[i];
      let c;
      if (!cs.length) {
        c = this._hex2rgb(it.isEntity ? "#8b5cf6" : ATOM_COLORS[it.atom] || "#4a90d9");
      } else {
        const t = ((this._tierOf(i) % cs.length) + cs.length) % cs.length;
        c = cs[t];
      }
      const k = 1.05 + it.seed * 0.35;
      this.COL[i * 3] = clamp(c[0] * k, 0, 1);
      this.COL[i * 3 + 1] = clamp(c[1] * k, 0, 1);
      this.COL[i * 3 + 2] = clamp(c[2] * k, 0, 1);
      const base = LAYER_SIZES[clamp(it.level | 0, 0, LAYER_SIZES.length - 1)];
      this.SIZ[i] = base * (0.72 + (0.34 * Math.sqrt(Math.min(it.deg, 60))) / 7.7) * (0.8 + it.seed * 0.45);
      it.rgb = c;
    }
    this._alphas();
    this.emit("legend", this.legendData());
  }

  legendData() {
    const pd = PALETTE_DEFS.find((x) => x.key === this.paletteKey) || PALETTE_DEFS[0];
    if (!pd.cols.length) {
      const counts = new Map();
      this.nodes.forEach((x) => {
        const k = x.isEntity ? "实体/话题" : x.atom || "其他";
        counts.set(k, (counts.get(k) || 0) + 1);
      });
      const order = ["factual", "episodic", "planned", "preference", "routine", "relational", "opinion", "实体/话题"];
      const items = Array.from(counts.entries())
        .sort((a, b) => order.indexOf(a[0]) - order.indexOf(b[0]) || b[1] - a[1])
        .slice(0, 9)
        .map(([k, v]) => ({
          color: k === "实体/话题" ? "#8b5cf6" : ATOM_COLORS[k] || "#9aa4b2",
          label: k === "实体/话题" ? k : ATOM_LABEL[k] || k,
          count: v,
        }));
      return { title: "颜色 = 记忆类型", items };
    }
    // 层名一律跟着**当前分层依据**走；计数用真实分桶结果，并附一句"这一层是什么"。
    const titles = {
      role: "层 = 角色：人 → 群/会话 → 话题/实体 → 记忆",
      strength: "层 = 记忆强度（按本视图分位切档，无强度节点沉底）",
      time: "层 = 记忆时间（7 天 / 1 个月 / 3 个月 / 半年 / 更早）",
      type: "层 = 记忆类型 / 话题实体",
    };
    const items = [];
    const c0 = this.cnt || [];
    const push = (label, idx, count, def) => items.push({ color: pd.cols[idx % pd.cols.length], label, count: count || 0, def: def || "" });
    if (this.tierMode === "role") LEVEL_DEFS.forEach((L, i) => push(`${L.name} ${L.en}`, i, c0[i], L.def));
    else if (this.tierMode === "type") {
      [["事实", 0], ["经历", 1], ["偏好", 2], ["待办", 3], ["其他", 4], ["话题 / 实体", 5]]
        .forEach(([t, i]) => push(t, i, c0[i]));
    } else if (this.tierMode === "strength") {
      // 分位分档：切点由 _buildStructure 按本视图算（_stCuts），这里只把它们显示出来。
      const c = this._stCuts || [];
      const f = (v) => (v == null ? "—" : (+v).toFixed(2));
      push("最强一档", 0, c0[0], c.length > 0 ? `记忆强度 ≥ ${f(c[0])}` : "");
      push("较强", 1, c0[1], c.length > 1 ? `强度 ${f(c[1])} ~ ${f(c[0])}` : "");
      push("中等", 2, c0[2], c.length > 2 ? `强度 ${f(c[2])} ~ ${f(c[1])}` : "");
      push("较弱", 3, c0[3], c.length > 2 ? `记忆强度 < ${f(c[2])}` : "");
      push("无强度（人 / 群 / 话题·实体）", 4, c0[4], "没有强度属性的节点一律沉底");
    } else {
      // 绝对时间窗（不是相对分位）：阈值写死在 _buildStructure，与本图数据无关，稳定可解释。
      push("最近 7 天", 0, c0[0], "");
      push("1 个月以内", 1, c0[1], "");
      push("3 个月以内", 2, c0[2], "");
      push("半年以内", 3, c0[3], "");
      push("超过半年 / 无时间", 4, c0[4], "超过 6 个月与没有时间戳的一律沉底");
    }
    return { title: titles[this.tierMode] || "颜色", items };
  }

  setPalette(key) {
    if (!PALETTE_DEFS.some((x) => x.key === key)) return this;
    this.paletteKey = key;
    this.applyPalette();
    this._kick();
    this.emit("palette", key);
    return this;
  }

  setTierMode(key) {
    const k = normTier(key);
    if (!TIER_DEFS.some((x) => x.key === k)) return this;
    this.tierMode = k;
    this._buildStructure();
    this.applyPalette();
    this.setLayout(this.mode, false);
    this._kick();
    this.emit("tier", key);
    return this;
  }

  setColorMode(mode) {
    // 兼容旧接口：类型 / 强度 / 时间 / 连接度
    const map = { type: ["semantic", "type"], strength: ["fire", "strength"], time: ["cold", "time"], degree: ["blue_gold", "role"] };
    const m = map[mode];
    if (!m) return this;
    this.paletteKey = m[0];
    this.tierMode = m[1];
    this._buildStructure();
    this.applyPalette();
    this.setLayout(this.mode, false);
    this._kick();
    return this;
  }

  /* ---------------- 布局 ---------------- */

  _setTgt(i, x, y, z) {
    const pin = this._pins.get(i);
    this.TGT[i * 3] = x + (pin ? pin.x : 0);
    this.TGT[i * 3 + 1] = y + (pin ? pin.y : 0);
    this.TGT[i * 3 + 2] = z + (pin ? pin.z : 0);
  }

  setLayout(key, instant) {
    const def = LAYOUT_DEFS.find((x) => x.key === key);
    const n = this.nodes.length;
    if (!def || !n) return this;
    const P = this.P;
    const topY = 5.4;
    const L = this;
    const ND = this.nodes;

    const runners = {
      // ⚠️ 这些是对象字面量方法：里面的 this 指向 runners 对象，**不是引擎** —— 要引擎就用闭包里的 L。
      // （曾因此把 4 个布局改成 this.layerK → 一点就 TypeError，那几个"显示样式"看着就是不响应。）
      cone() {
        for (let i = 0; i < n; i++) {
          const it = ND[i];
          const r = (0.95 + it.level * 2.0) * P.rad * (1 + (it.seed - 0.5) * 0.06);
          const a = it.ring + it.level * 0.22;
          L._setTgt(i, Math.cos(a) * r, topY - it.level * 2.35 * P.gap + (it.seed - 0.5) * 0.5, Math.sin(a) * r);
        }
      },
      rings() {
        for (let i = 0; i < n; i++) {
          const it = ND[i];
          const r = (1.15 + Math.pow(it.level, 1.22) * 1.55) * P.rad;
          const a = it.ring + it.level * 0.55;
          L._setTgt(i, Math.cos(a) * r, topY - it.level * 3.15 * P.gap + Math.sin(it.ring * 3) * 0.18, Math.sin(a) * r);
        }
      },
      helix() {
        for (let i = 0; i < n; i++) {
          const t = i / n;
          const a = i * 0.165;
          const r = (0.9 + t * 7.4) * P.rad * (0.86 + 0.28 * Math.sin(i * 0.09));
          L._setTgt(i, Math.cos(a) * r, topY - t * 15.5 * P.gap, Math.sin(a) * r);
        }
      },
      shell() {
        for (let i = 0; i < n; i++) {
          const it = ND[i];
          const c = Math.max(1, L.cnt[it.level]);
          const R = (1.05 + it.level * 2.0) * P.rad;
          const th = Math.acos(clamp(1 - (2 * (it.ord + 0.5)) / c, -1, 1));
          const ph = it.ord * 2.399963 + it.level * 0.7;
          L._setTgt(i, R * Math.sin(th) * Math.cos(ph), R * Math.cos(th), R * Math.sin(th) * Math.sin(ph));
        }
      },
      planes() {
        for (let i = 0; i < n; i++) {
          const it = ND[i];
          const c = Math.max(1, L.cnt[it.level]);
          const a = it.ring * 3.4 + it.seed * 6.2;
          const rr = Math.sqrt((it.ord + 1) / c) * (2.2 + it.level * 2.6) * P.rad;
          L._setTgt(i, Math.cos(a) * rr, topY - it.level * 3.4 * P.gap + (it.seed - 0.5) * 0.35, Math.sin(a) * rr);
        }
      },
      force() {
        L._forceStep(26);
        const s = P.rad * 0.92;
        for (let i = 0; i < n; i++) L._setTgt(i, L._forcePos[i * 3] * s, L._forcePos[i * 3 + 1] * s * 0.95, L._forcePos[i * 3 + 2] * s);
      },
      hang() {
        for (let l = 0; l < (L.layerK || 4); l++) {
          const step = (2.05 - l * 0.12) * P.rad;
          for (let i = 0; i < n; i++) {
            const it = ND[i];
            if (it.level !== l) continue;
            if (l === 0) { L._setTgt(i, 0, topY, 0); continue; }
            const a = it.ring + it.seed * 0.8;
            const p = it.parent;
            if (p < 0) { L._setTgt(i, Math.cos(a) * step, topY - l * 2.9 * P.gap, Math.sin(a) * step); continue; }
            L._setTgt(i, L.TGT[p * 3] + Math.cos(a) * step * 0.95, topY - l * 2.9 * P.gap, L.TGT[p * 3 + 2] + Math.sin(a) * step * 0.95);
          }
        }
      },
      galaxy() {
        const ARMS = 4;
        for (let i = 0; i < n; i++) {
          const it = ND[i];
          const c = Math.max(1, L.cnt[it.level]);
          const R = (0.7 + it.level * 1.98 + (it.ord / c) * 1.5) * P.rad;
          const arm = (it.ord % ARMS) * (TAU / ARMS);
          const a = arm + R * 0.62 + (it.seed - 0.5) * (0.16 + 0.42 / (1 + R * 0.45));
          const th = (it.seed - 0.5) * (it.level <= 1 ? 3.1 : 0.85) * P.gap * (1 + 1.5 / (1 + R * 0.55));
          L._setTgt(i, Math.cos(a) * R, topY - 3.2 - Math.sin(th) * 0.95, Math.sin(a) * R);
        }
      },
      dna() {
        const HH = 15.2 * P.gap;
        for (let i = 0; i < n; i++) {
          const t = i / n;
          const a = t * TAU * 3.2 + (i % 2) * Math.PI;
          const R = (1.5 + t * 3.4) * P.rad;
          L._setTgt(i, Math.cos(a) * R, topY - t * HH, Math.sin(a) * R);
        }
      },
      cube() {
        for (let l = 0; l < (L.layerK || 4); l++) {
          const ids = [];
          for (let i = 0; i < n; i++) if (ND[i].level === l) ids.push(i);
          const c = ids.length;
          if (!c) continue;
          const nn = Math.ceil(Math.sqrt(c));
          const mm = Math.ceil(c / nn);
          const sp = (11.6 * P.rad) / Math.max(nn, 1);
          for (let k = 0; k < c; k++) {
            L._setTgt(ids[k], (k % nn - (nn - 1) / 2) * sp, topY - l * 2.7 * P.gap, (Math.floor(k / nn) - (mm - 1) / 2) * sp);
          }
        }
      },
      burst() {
        const dx = new Float32Array(n), dy = new Float32Array(n), dz = new Float32Array(n);
        for (let l = 0; l < (L.layerK || 4); l++) {
          for (let i = 0; i < n; i++) {
            const it = ND[i];
            if (it.level !== l) continue;
            const c = Math.max(1, L.cnt[l]);
            const k = it.ord;
            if (l === 0) { dx[i] = 0; dy[i] = 1; dz[i] = 0; }
            else if (l <= 2) {
              const ph = k * 2.399963;
              const th = Math.acos(clamp(1 - (2 * (k + 0.5)) / c, -1, 1));
              dx[i] = Math.sin(th) * Math.cos(ph); dy[i] = Math.cos(th); dz[i] = Math.sin(th) * Math.sin(ph);
            } else {
              const j = (it.seed - 0.5) * 0.17;
              const q = it.parent >= 0 ? it.parent : i;
              dx[i] = dx[q] + j; dy[i] = dy[q] + j * 0.8; dz[i] = dz[q] - j * 0.9;
              const len = Math.hypot(dx[i], dy[i], dz[i]) || 1;
              dx[i] /= len; dy[i] /= len; dz[i] /= len;
            }
            const R = (0.45 + l * 2.2) * P.rad;
            L._setTgt(i, dx[i] * R, dy[i] * R * 0.9 - 1.2, dz[i] * R);
          }
        }
      },
      wave() {
        for (let l = 0; l < (L.layerK || 4); l++) {
          const ids = [];
          for (let i = 0; i < n; i++) if (ND[i].level === l) ids.push(i);
          const c = ids.length;
          if (!c) continue;
          const nn = Math.ceil(Math.sqrt(c));
          const mm = Math.ceil(c / nn);
          const sp = (13.4 * P.rad) / Math.max(nn, 1);
          for (let k = 0; k < c; k++) {
            const x = (k % nn - (nn - 1) / 2) * sp;
            const z = (Math.floor(k / nn) - (mm - 1) / 2) * sp;
            const d = Math.hypot(x, z);
            L._setTgt(ids[k], x, topY - l * 2.5 * P.gap + Math.sin(d * 1.15 - l * 0.95) * 1.25 * Math.exp(-d * 0.05), z);
          }
        }
      },
    };

    const run = runners[key];
    if (!run) return this;
    run();
    this._spinPhase = 0;
    if (instant) {
      this.POS.set(this.TGT);
      this.FROM.set(this.TGT);
      this._morph = 1;
    } else {
      this.FROM.set(this.POS);
      this._morph = 0;
    }
    this.mode = key;
    this.pan.x = 0;
    this.pan.y = 0;
    if (def.view) {
      this.cam.tPitch = def.view[0];
      this.cam.tDist = def.view[1];
      this.cam.tTy = def.view[2];
    }
    this._kick();
    this.emit("layout", { key, label: def.label, en: def.en });
    return this;
  }

  /** 力导向布局：分块迭代（网格近似斥力），跑够步数就冻结复用 */
  _forceStep(iters) {
    if (this._forceReady) return;
    const n = this.nodes.length;
    if (!n) return;
    const fp = this._forcePos;
    const tmp = new Float32Array(n * 3);
    const cell = 2.6;
    const inv = 1 / cell;
    const gkey = (a, b, c) => (a + 96) + ((b + 96) << 8) + ((c + 96) << 16);
    for (let it = 0; it < iters; it++) {
      const grid = new Map();
      for (let i = 0; i < n; i++) {
        const key = gkey(Math.floor(fp[i * 3] * inv), Math.floor(fp[i * 3 + 1] * inv), Math.floor(fp[i * 3 + 2] * inv));
        let a = grid.get(key);
        if (!a) grid.set(key, (a = []));
        a.push(i);
      }
      for (let i = 0; i < n; i++) {
        const x = fp[i * 3], y = fp[i * 3 + 1], z = fp[i * 3 + 2];
        const gx = Math.floor(x * inv), gy = Math.floor(y * inv), gz = Math.floor(z * inv);
        let fx = 0, fy = 0, fz = 0, cnt = 0;
        for (let dx = -1; dx <= 1; dx++) {
          for (let dy = -1; dy <= 1; dy++) {
            for (let dz = -1; dz <= 1; dz++) {
              const a = grid.get(gkey(gx + dx, gy + dy, gz + dz));
              if (!a) continue;
              for (let m = 0; m < a.length; m++) {
                const j = a[m];
                if (j === i || cnt > 26) continue;
                cnt++;
                let ddx = x - fp[j * 3], ddy = y - fp[j * 3 + 1], ddz = z - fp[j * 3 + 2];
                let d2 = ddx * ddx + ddy * ddy + ddz * ddz;
                if (d2 < 0.04) { ddx = (this.nodes[i].seed - 0.5) * 0.2; ddy = 0.1; ddz = 0.03; d2 = 0.05; }
                const f = 1.15 / d2;
                const d = Math.sqrt(d2);
                fx += (ddx / d) * f; fy += (ddy / d) * f; fz += (ddz / d) * f;
              }
            }
          }
        }
        tmp[i * 3] = fx; tmp[i * 3 + 1] = fy; tmp[i * 3 + 2] = fz;
      }
      for (let k = 0; k < this.links.length; k++) {
        const l = this.links[k];
        const a = l.a, b = l.b;
        const dx = fp[b * 3] - fp[a * 3], dy = fp[b * 3 + 1] - fp[a * 3 + 1], dz = fp[b * 3 + 2] - fp[a * 3 + 2];
        const d = Math.hypot(dx, dy, dz) || 0.001;
        const rest = this.nodes[a].level === this.nodes[b].level ? 3.4 : 2.5;
        const f = ((d - rest) * 0.035) / d;
        tmp[a * 3] += dx * f; tmp[a * 3 + 1] += dy * f; tmp[a * 3 + 2] += dz * f;
        tmp[b * 3] -= dx * f; tmp[b * 3 + 1] -= dy * f; tmp[b * 3 + 2] -= dz * f;
      }
      const damp = 0.86 - it * 0.0016;
      for (let i = 0; i < n; i++) {
        let vx = tmp[i * 3] - fp[i * 3] * 0.012;
        let vy = tmp[i * 3 + 1] - fp[i * 3 + 1] * 0.012;
        let vz = tmp[i * 3 + 2] - fp[i * 3 + 2] * 0.012;
        const sp = Math.hypot(vx, vy, vz), max = 0.55;
        if (sp > max) { vx = (vx / sp) * max; vy = (vy / sp) * max; vz = (vz / sp) * max; }
        fp[i * 3] += vx * damp; fp[i * 3 + 1] += vy * damp; fp[i * 3 + 2] += vz * damp;
      }
    }
    let mx = 0;
    for (let i = 0; i < n; i++) mx = Math.max(mx, Math.abs(fp[i * 3]), Math.abs(fp[i * 3 + 1]), Math.abs(fp[i * 3 + 2]));
    const k = 9.6 / (mx || 1);
    for (let i = 0; i < fp.length; i++) fp[i] *= k;
    this._forceRun += iters;
    if (this._forceRun > 260) this._forceReady = true;
  }

  setPara(key, value) {
    if (!(key in this.P)) return this;
    this.P[key] = Number(value);
    if (key === "cap") this._prepareDraw();
    if (key === "gap" || key === "rad") this.setLayout(this.mode, false);
    this._kick();
    return this;
  }

  setCam(key, instant) {
    const v = CAM_DEFS.find((x) => x.key === key);
    if (!v) return this;
    this.camKey = key;
    this.cam.tPitch = v.pitch;
    this.cam.tDist = v.dist;
    this.cam.tTy = v.ty;
    if (instant) {
      this.cam.pitch = v.pitch;
      this.cam.dist = v.dist;
      this.cam.ty = v.ty;
    }
    this.emit("cam", key);
    this._kick();
    return this;
  }

  setToggle(key, on) {
    if (key === "simple") {
      this._simple = !!on;
      this._kick();
      this.emit("toggle", { key, on: !!on });
      return this;
    }
    if (key === "links") this.showLinks = !!on;
    if (key === "labels") this.showLabels = !!on;
    if (key === "tour") {
      this.tour.on = !!on;
      this.tour.t = 0;
    }
    this._kick();
    this.emit("toggle", { key, on: !!on });
    return this;
  }

  /* ---------------- 搜索 / 选中 / 统计 ---------------- */

  _searchApply() {
    const q = String(this._q || "").trim().toLowerCase();
    this.searchHits = q ? [] : null;
    this.searchSet.clear();
    if (q) {
      for (let i = 0; i < this.nodes.length; i++) {
        const x = this.nodes[i];
        if (x.name.toLowerCase().includes(q) || x.id.toLowerCase().includes(q)) {
          this.searchHits.push(i);
          this.searchSet.add(i);
          if (this.searchHits.length > 400) break;
        }
      }
    }
    this._alphas();
  }

  setSearch(v) {
    this._q = v || "";
    this._searchApply();
    this._prepareDraw();
    this._kick();
    return this;
  }

  /**
   * 取消居中：把居中前的轴心、相机、平移原样还回去（幂等，没在居中就什么都不做）。
   * 返回"是否真的还原过"。用户反馈"点空白退出、或用完退出，轴心回不来"——
   * 根因是退出路径把 _focusSaved 直接清掉而不还原，这里集中成一个入口。
   */
  unfocus(silent) {
    if (!this._focusSaved || !this._focusNodeId) return false;
    const b = this._focusSaved;
    const id = this._focusNodeId;
    this.focus = b.focus.slice();
    this.cam.yaw = b.yaw;
    this.cam.pitch = b.pitch; this.cam.tPitch = b.tPitch;
    this.cam.dist = b.dist; this.cam.tDist = b.tDist;
    this.cam.ty = b.ty; this.cam.tTy = b.tTy;
    this.pan.x = b.px; this.pan.y = b.py;
    this._focusSaved = null;
    this._focusNodeId = null;
    this._alphas();
    this._prepareDraw();
    this.emit("focusmode", { id, centered: false });
    if (!silent) this._kick();
    return true;
  }

  /**
   * 居中开关。支持显式第二参数 on：
   *   toggleFocus(id)        → 切换
   *   toggleFocus(id, true)  → 一定居中
   *   toggleFocus(id, false) → 一定取消居中（并还原轴心）
   * 只有 toggle 语义时，"清焦点"会把没居中的节点反而居中——这是之前定位错乱的元凶之一。
   */
  toggleFocus(idOrIdx, on) {
    const id = String(idOrIdx);
    const i = this.nodes.findIndex((x) => x.id === id);
    if (i < 0) return this;
    const focused = !!(this._focusSaved && this._focusNodeId === id);
    const want = on === undefined ? !focused : !!on;
    if (!want) {
      if (this._focusSaved) {
        this.unfocus();
        if (focused) this.emit("select", this.nodes[i].raw);   // 卡片靠 select 重渲染，按钮文案要翻回「居中」
      }
      return this;
    }
    if (focused) return this;
    if (this._focusSaved) this.unfocus(true);   // 先还原上一个，再记新的，免得原轴心被覆盖丢掉
    this._focusSaved = {
      focus: this.focus.slice(), yaw: this.cam.yaw, pitch: this.cam.pitch, tPitch: this.cam.tPitch,
      dist: this.cam.dist, tDist: this.cam.tDist, ty: this.cam.ty, tTy: this.cam.tTy,
      px: this.pan.x, py: this.pan.y,
    };
    this._focusNodeId = id;
    this.focusNode(i);
    this.emit("focusmode", { id, centered: true });
    return this;
  }

  /** 当前是否正把某个节点当轴心居中 */
  focusedId() {
    return this._focusSaved ? this._focusNodeId : null;
  }

  focusNode(idOrIdx) {
    let i = typeof idOrIdx === "number" ? idOrIdx : this.nodes.findIndex((x) => x.id === String(idOrIdx));
    if (i < 0) {
      if (this.searchHits && this.searchHits.length) i = this.searchHits[0];
      else return this;
    }
    if (i >= this.nodes.length) return this;
    this.selected = i;
    this.focus = [this.POS[i * 3], this.POS[i * 3 + 1], this.POS[i * 3 + 2]];
    this.cam.tDist = 15;
    this._alphas();
    this._prepareDraw();
    this.emit("select", this.nodes[i].raw);
    this._kick();
    return this;
  }

  /** 选中/取消选中（控制台清空选中要调） */
  selectNode(i) {
    const idx = typeof i === "number" ? i : -1;
    this.selected = idx >= 0 && idx < this.nodes.length ? idx : -1;
    if (this.selected < 0) this.searchSet = new Set();     // 取消选中后别让旧的搜索压暗残留
    this._alphas();
    this._prepareDraw();
    this.emit("select", this.selected >= 0 ? this.nodes[this.selected].raw : null);
    this._kick();
    return this;
  }

  /** 彻底复位：相机/平移/挪动/选中/布局全部回到初始（等价于重新选一次当前显示样式） */
  resetAll() {
    this._pins.clear();
    this.pan.x = 0;
    this.pan.y = 0;
    this.focus = [0, 0, 0];
    this.selected = -1;
    this._focusSaved = null;
    this._focusNodeId = null;
    this._alphas();
    this.setCam(this.camKey, true);
    this.setLayout(this.mode, true);
    this._prepareDraw();
    this.emit("select", null);
    this.emit("stats", this.stats());
    this._kick();
    return this;
  }

  clearPins() {
    if (!this._pins.size) return this;
    this._pins.clear();
    this.setLayout(this.mode, false);
    this.emit("stats", this.stats());
    return this;
  }

  /** 选中/搜索 → 点与边的透明度（其余压暗，这是"看清关系"的关键） */
  _alphas() {
    const n = this.nodes.length;
    const near = new Set();
    if (this.selected >= 0) {
      near.add(this.selected);
      for (let k = 0; k < this.links.length; k++) {
        const l = this.links[k];
        if (l.a === this.selected) near.add(l.b);
        else if (l.b === this.selected) near.add(l.a);
      }
    }
    this._near = this.selected >= 0 ? near : null;
    for (let i = 0; i < n; i++) {
      let a = 1;
      if (this.selected >= 0) a = near.has(i) ? 1.5 : 0.12;
      else if (this.searchHits && this.searchHits.length) a = this.searchSet.has(i) ? 1.6 : 0.22;
      this.ALP[i] = a;
    }
    if (!this._ea || this._ea.length < this.links.length * 2) this._ea = new Float32Array(Math.max(2, this.links.length * 2));
    for (let k = 0; k < this.links.length; k++) {
      const l = this.links[k];
      // 压暗的下限**必须仍然看得见**（这一段被两次踩过）：
      // 最早 0.015/0.012、后来 0.1/0.09，乘 0.36 之后分别是 ~0.005 与 0.036，
      // 都远低于正常基线 0.0792 —— 大图上万条边靠重叠还能看成一张网，
      // 小图（几百条边）就直接表现为「连线消失」（用户报的第 1 条）。
      // 现在的口径：**强调邻域只做加亮**（邻接 0.85 / 搜索命中 0.36），
      // 其余连线保持与正常基线同一水平（0.22），不再被压到看不见。
      let v = 0.22;
      if (this.selected >= 0) v = l.a === this.selected || l.b === this.selected ? 0.85 : 0.22;
      else if (this.searchHits && this.searchHits.length) v = this.searchSet.has(l.a) || this.searchSet.has(l.b) ? 0.36 : 0.22;
      // ⑦ 关系强度（hf_edges.weight）真的参与线亮度 —— 界面上写的是「线越亮＝关系越强」，
      //    之前 weight 只用于物理弹簧与低缩放裁剪，线的亮度完全不看它，文案是假的。
      //    权重实际值域：entity 1.4 / about·in_session·member_of 1.0 / temporal 0.2~0.9 /
      //    semantic 0.9~3.0 ⇒ 映射到 0.8~1.85 倍，基线仍远高于「看得见」的下限（这段被踩过两次）。
      const wf = 0.8 + 0.35 * clamp(Number(l.w) || 1, 0.2, 3);
      const a0 = Math.min(1, v * 0.36 * this._lineBoost * wf);
      const a1 = Math.min(1, v * 0.24 * this._lineBoost * wf);
      this._ea[k * 2] = a0;
      this._ea[k * 2 + 1] = a1;
    }
  }

  stats() {
    return {
      nodes: this.nodes.length,
      links: this.links.length,
      showLinks: this.showLinks,
      nebW: this._texNebSize ? this._texNebSize[0] : 0,
      fatal: this._fatal || null,
      drawn: this._visList ? this._visList.length : 0,
      drawnLinks: this._visLinks ? this._visLinks.length : 0,
      levels: this.cnt ? this.cnt.slice() : [0, 0, 0, 0, 0],
      fps: this._fps,
      layout: this.mode,
      layoutLabel: (LAYOUT_DEFS.find((x) => x.key === this.mode) || {}).label || "",
      cam: this.camKey,
      palette: this.paletteKey,
      tier: this.tierMode,
      quality: this._simple ? "low" : this._quality,
      simple: !!this._simple,
      gpu: this.isGL2 ? "WebGL2" : "WebGL1",
      selected: this.selected >= 0 ? this.nodes[this.selected].id : null,
      hover: this.hover >= 0 ? this.nodes[this.hover].id : null,
      pinned: this._pins.size,
      morph: Math.round(this._morph * 100) / 100,
      tour: this.tour.on,
      spin: this.P.spin,
      cap: this.P.cap,
      labels: this.showLabels,
    };
  }

  /* ---------------- 事件 ---------------- */

  on(ev, fn) {
    if (!this._subs[ev]) this._subs[ev] = [];
    this._subs[ev].push(fn);
    return this;
  }

  offAll(ev) {
    if (ev) this._subs[ev] = [];
    else this._subs = {};
    return this;
  }

  emit(ev, payload) {
    const arr = this._subs[ev];
    if (!arr || !arr.length) return;
    for (let i = 0; i < arr.length; i++) {
      try {
        arr[i](payload);
      } catch (err) {
        console.warn("[hf3d] 回调异常", err);
      }
    }
  }

  /* ---------------- 交互 ---------------- */

  _bind() {
    const cv = this.glCanvas;
    let lastX = 0, lastY = 0, dragging = false, moved = 0, panning = false, orbiting = false;
    let dragNode = -1;
    const pos = (e) => {
      const r = cv.getBoundingClientRect();
      return { x: e.clientX - r.left, y: e.clientY - r.top };
    };
    cv.addEventListener("pointerdown", (e) => {
      try { cv.setPointerCapture(e.pointerId); } catch (err) { /* 忽略 */ }
      dragging = true;
      moved = 0;
      lastX = e.clientX;
      lastY = e.clientY;
      const hit = this._pickAt(pos(e));
      // 中键（也兼容右键）平移视图；左键优先给"拖单点"，空白处才是旋转
      if (e.button === 1 || e.button === 2) panning = true;
      else if (hit >= 0) dragNode = hit;
      else orbiting = true;
      cv.style.cursor = panning ? "move" : "grabbing";
    });
    cv.addEventListener("pointermove", (e) => {
      const p = pos(e);
      if (!dragging) {
        const i = this._pickAt(p);
        if (i !== this.hover) {
          this.hover = i;
          cv.style.cursor = i >= 0 ? "pointer" : "grab";
          this.emit("hover", i >= 0 ? this.nodes[i].raw : null);
          this._kick();
        }
        return;
      }
      const dx = e.clientX - lastX, dy = e.clientY - lastY;
      moved += Math.abs(dx) + Math.abs(dy);
      lastX = e.clientX;
      lastY = e.clientY;
      if (dragNode >= 0) {
        // 单点挪动：位移写进 pin 偏移（TGT 与 POS 同时推），松手不会被布局拽回去
        const k = (2 * this.cam.dist * Math.tan(this.FOV / 2)) / this.H;
        const cy = Math.cos(this.cam.yaw), sy = Math.sin(this.cam.yaw);
        const cp = Math.cos(this.cam.pitch), sp = Math.sin(this.cam.pitch);
        const rx = cy, ry = 0, rz = -sy;
        const ux = -sy * sp, uy = cp, uz = -cy * sp;
        const wx = (rx * dx + ux * -dy) * k;
        const wy = (ry * dx + uy * -dy) * k;
        const wz = (rz * dx + uz * -dy) * k;
        const pin = this._pins.get(dragNode) || { x: 0, y: 0, z: 0 };
        pin.x += wx; pin.y += wy; pin.z += wz;
        this._pins.set(dragNode, pin);
        this.POS[dragNode * 3] += wx;
        this.POS[dragNode * 3 + 1] += wy;
        this.POS[dragNode * 3 + 2] += wz;
        this.TGT[dragNode * 3] += wx;
        this.TGT[dragNode * 3 + 1] += wy;
        this.TGT[dragNode * 3 + 2] += wz;
      } else if (panning) {
        // 二维平移：直接推画面（不再动 focus，转轴才不会被带跑）
        this.pan.x += dx;
        this.pan.y += dy;
      } else if (orbiting) {
        this.cam.yaw -= dx * 0.007;
        this.cam.tPitch = clamp(this.cam.tPitch + dy * 0.005, -1.45, 1.45);
        this._userHold = true;
      }
      this._kick();
    });
    const up = (e) => {
      dragging = false;
      orbiting = false;
      panning = false;
      cv.style.cursor = "grab";
      if (moved < 4) {
        const i = this._pickAt(pos(e));
        this.selected = i === this.selected ? -1 : i;
        // 点空白 = 退出居中：把居中前的轴心/相机还回去（用户明确要求这个行为）
        if (i < 0 && this._focusSaved) this.unfocus();
        this._alphas();
        this._prepareDraw();
        this.emit("select", this.selected >= 0 ? this.nodes[this.selected].raw : null);
      } else if (dragNode >= 0) {
        this.emit("stats", this.stats());
      }
      dragNode = -1;
      setTimeout(() => { this._userHold = false; }, 900);
      this._kick();
    };
    cv.addEventListener("pointerup", up);
    cv.addEventListener("pointercancel", up);
    cv.addEventListener("pointerleave", (e) => { if (dragging) up(e); });
    // 中键平移到视口之外时别触发浏览器自动滚动
    cv.addEventListener("mousedown", (e) => { if (e.button === 1) e.preventDefault(); });
    cv.addEventListener("auxclick", (e) => e.preventDefault());
    cv.addEventListener("wheel", (e) => {
      e.preventDefault();
      this.zoomAt(e);
    }, { passive: false });
    cv.addEventListener("contextmenu", (e) => e.preventDefault());
    // 上下文丢失/恢复：真机上偶发（GPU 压力/后台标签），表现为"星云突然没了、只能刷新网页"。
    // 丢失时停帧防止刷错误，恢复时重建 GL 资源并**重新烘焙星云**，用户无感自愈。
    cv.addEventListener("webglcontextlost", (e) => {
      e.preventDefault();
      this._ctxLost = true;
      // ⚠️ 这里**绝不能**把循环停死（原实现 _running=false）：webglcontextrestored 在真机上
      //    经常根本不触发，循环一停就再没人调 _rebuildGL()，画布永久冻在旧帧上 ——
      //    用户看到的就是「切到全库再切回当前视角，线全没了，只能刷新页面」。
      //    保持循环活着：_tickInner 的 lost 分支会每秒重试重建，成功即无感自愈。
      this._running = false;
      this._ctxPollT = 0;
      this._kick();               // _kick 见 _running 为假 → 重新置真并排下一帧
    }, false);
    cv.addEventListener("webglcontextrestored", () => {
      this._ctxLost = false;
      this._rebuildGL();
    }, false);
    cv.addEventListener("dblclick", (e) => {
      const i = this._pickAt(pos(e));
      if (i >= 0) {
        this.selected = i;
        this.focus = [this.POS[i * 3], this.POS[i * 3 + 1], this.POS[i * 3 + 2]];
        this.cam.tDist = 15;
        this._alphas();
        this._prepareDraw();
        this.emit("select", this.nodes[i].raw);
      } else {
        // 双击空白：如果正居中，先把轴心还回去（原来这里直接清 _focusSaved，轴心就永久丢了）
        if (!this.unfocus()) {
          this.focus = [0, 0, 0];
          this.cam.yaw = 0.6;
          this.setCam("orbit");
        }
        this.selected = -1;
        this._alphas();
        this._prepareDraw();
        this.emit("select", null);
      }
      this._kick();
    });
    let tD = 0;
    cv.addEventListener("touchstart", (e) => {
      if (e.touches.length === 1) {
        lastX = e.touches[0].clientX;
        lastY = e.touches[0].clientY;
        orbiting = true;
      } else if (e.touches.length === 2) {
        tD = Math.hypot(e.touches[0].clientX - e.touches[1].clientX, e.touches[0].clientY - e.touches[1].clientY);
      }
    }, { passive: true });
    cv.addEventListener("touchmove", (e) => {
      if (e.touches.length === 1 && orbiting) {
        this.cam.yaw -= (e.touches[0].clientX - lastX) * 0.007;
        this.cam.tPitch = clamp(this.cam.tPitch + (e.touches[0].clientY - lastY) * 0.005, -1.45, 1.45);
        lastX = e.touches[0].clientX;
        lastY = e.touches[0].clientY;
        this._userHold = true;
        this._kick();
      } else if (e.touches.length === 2) {
        const d = Math.hypot(e.touches[0].clientX - e.touches[1].clientX, e.touches[0].clientY - e.touches[1].clientY);
        this.cam.tDist = clamp(this.cam.tDist * (tD / (d || 1)), 2.5, 260);
        tD = d;
        this._kick();
      }
    }, { passive: true });
    cv.addEventListener("touchend", () => {
      orbiting = false;
      setTimeout(() => { this._userHold = false; }, 600);
    });

    this._onResize = () => this.resize();
    window.addEventListener("resize", this._onResize);
    if (typeof ResizeObserver !== "undefined") {
      try {
        this._ro = new ResizeObserver(() => this.resize());
        this._ro.observe(this.host);
      } catch (err) {
        this._ro = null;
      }
    }
  }

  /**
   * 以鼠标位置为锚点缩放：光标下的世界点保持不动。
   * 推导：聚焦平面（过 target、垂直于视线）上任一点 P = T + right*xOff + up*yOff，
   * 其中 xOff/yOff 与相机距离 d 成正比；距离变成 d'=k*d 后要让 P 仍在光标下，
   * 只需把 target 平移 (1-k) 倍的那个偏移量。
   */
  zoomAt(e) {
    const r = this.glCanvas.getBoundingClientRect();
    const px = (e && e.clientX != null ? e.clientX - r.left : this.W / 2);
    const py = (e && e.clientY != null ? e.clientY - r.top : this.H / 2);
    const step = 0.22; // 每格 22%：原来的 10% 太小，滚半天看不出变化
    const k = 1 + Math.sign(e ? e.deltaY : -1) * step;
    const d = this.cam.tDist;
    const nd = clamp(d * k, 2.5, 260);
    const realK = nd / d;
    // 光标到画面中心（含已有平移）的像素差 A：缩放后让世界点钉在光标下 → pan += (1-k)*A
    const ax = px - (this.W / 2 + this.pan.x);
    const ay = py - (this.H / 2 + this.pan.y);
    this.pan.x += (1 - realK) * ax;
    this.pan.y += (1 - realK) * ay;
    this.cam.tDist = nd;
    this._kick();
    return this;
  }

  _project(x, y, z, o) {
    const m = this._mat.mvp;
    const w = m[3] * x + m[7] * y + m[11] * z + m[15];
    if (w <= 0.01) { o[2] = -1; return o; }
    // 注意：平移量已经通过 _camUpdate 进到 mvp 矩阵里了，这里**不能**再加一次 pan，
    // 否则标签/拾取会算出两倍偏移 —— 表现就是「一挪画布，文字就从记忆点上飘走」（实测）。
    o[0] = (((m[0] * x + m[4] * y + m[8] * z + m[12]) / w) * 0.5 + 0.5) * this.W;
    o[1] = (1 - ((m[1] * x + m[5] * y + m[9] * z + m[13]) / w)) * 0.5 * this.H;
    o[2] = w;
    return o;
  }

  _pickAt(p) {
    if (!this._scrX) return -1;
    let best = -1, bd = 22;
    for (let i = 0; i < this.nodes.length; i++) {
      if (this._scrW[i] <= 0.5) continue;
      const d = Math.hypot(this._scrX[i] - p.x, this._scrY[i] - p.y);
      const lim = Math.max(7, this._scrR[i] * this.P.glow + 5);
      if (d < lim && d < bd) { bd = d; best = i; }
    }
    return best;
  }

  /* ---------------- 渲染 ---------------- */

  _prepareDraw() {
    const n = this.nodes.length;
    const lowK = this._quality === "low" || this._simple ? 0.6 : 1;
    const cap = clamp(Math.round(this.P.cap * lowK), 50, Math.max(50, n));
    const keep = new Set();
    if (n > cap) {
      // ⚠️ 不能只「按度数取前 cap 个」：记忆节点度数天然最低（1~3），
      //    真机 853 记忆 + 1209 实体 ≈ 2062 > cap(1600) 时，被丢掉的几乎全是记忆，
      //    连带它们的 about / in_session / member_of 线一起消失 —— 用户看到的
      //    「线没了」有这一份。语义骨架（人 / 群 / 话题）必须无条件保留，
      //    剩余预算才按度数补给记忆（保留总数仍 ≤ cap，不增加绘制开销）。
      const skel = [];
      const rest = [];
      for (let i = 0; i < n; i++) {
        const r = String(this.nodes[i].role || "");
        if (r === "person" || r === "session" || r === "topic") skel.push(i);
        else rest.push(i);
      }
      if (skel.length >= cap) {
        skel.sort((a, b) => this.nodes[b].deg - this.nodes[a].deg);   // 骨架自己超预算：骨架内按度数取舍
        for (let i = 0; i < cap; i++) keep.add(skel[i]);
      } else {
        for (let i = 0; i < skel.length; i++) keep.add(skel[i]);
        rest.sort((a, b) => this.nodes[b].deg - this.nodes[a].deg);
        const room = cap - skel.length;
        for (let i = 0; i < room && i < rest.length; i++) keep.add(rest[i]);
      }
      if (this.selected >= 0) keep.add(this.selected);
      for (let i = 0; i < n; i++) if (this.ALP[i] > 1) keep.add(i);
    } else {
      for (let i = 0; i < n; i++) keep.add(i);
    }
    this._vis = keep;
    const vis = [];
    for (let i = 0; i < n; i++) if (keep.has(i)) vis.push(i);
    this._visList = vis;
    const idx = new Map();
    vis.forEach((v, k) => idx.set(v, k));
    this._visIdx = idx;
    const lk = [];
    for (let k = 0; k < this.links.length; k++) {
      const l = this.links[k];
      if (keep.has(l.a) && keep.has(l.b)) lk.push(l);
    }
    this._visLinks = lk;
    this._nPos = new Float32Array(Math.max(3, vis.length * 3));
    this._nCol = new Float32Array(Math.max(3, vis.length * 3));
    this._nSize = new Float32Array(Math.max(1, vis.length));
    this._nA = new Float32Array(Math.max(1, vis.length));
    this._ePos = new Float32Array(Math.max(6, lk.length * 6));
    this._eCol = new Float32Array(Math.max(6, lk.length * 6));
    this._scrX = new Float32Array(Math.max(1, n));
    this._scrY = new Float32Array(Math.max(1, n));
    this._scrW = new Float32Array(Math.max(1, n));
    this._scrR = new Float32Array(Math.max(1, n));
    const gl = this.gl;
    const grow = (buf, arr) => {
      gl.bindBuffer(gl.ARRAY_BUFFER, buf);
      gl.bufferData(gl.ARRAY_BUFFER, arr.byteLength, gl.DYNAMIC_DRAW);
    };
    grow(this.buf.nPos, this._nPos);
    grow(this.buf.nCol, this._nCol);
    grow(this.buf.nSize, this._nSize);
    grow(this.buf.nA, this._nA);
    grow(this.buf.ePos, this._ePos);
    grow(this.buf.eCol, this._eCol);
    grow(this.buf.eA, new Float32Array(Math.max(2, lk.length * 2)));
    this._alphas();
  }

  _framePositions(dt) {
    const n = this.nodes.length;
    const L = LAYOUT_DEFS.find((x) => x.key === this.mode) || LAYOUT_DEFS[0];
    if (this._morph < 1) this._morph = Math.min(1, this._morph + 0.0125 * Math.max(0.4, dt / 16.7));
    const t = performance.now() / 1000;
    for (let i = 0; i < n; i++) {
      const it = this.nodes[i];
      const k = easeIO(clamp((this._morph - it.level * 0.055) / 0.8, 0, 1));
      let x = lerp(this.FROM[i * 3], this.TGT[i * 3], k);
      let y = lerp(this.FROM[i * 3 + 1], this.TGT[i * 3 + 1], k);
      let z = lerp(this.FROM[i * 3 + 2], this.TGT[i * 3 + 2], k);
      const br = 1 + Math.sin(t * 1.6 + it.seed * 9) * 0.012;
      if (L.spinLevel && this._morph >= 1) {
        const a = this._spinPhase * L.spinLevel * (it.level % 2 ? 1 : -1) * (1 + it.level * 0.22);
        const c = Math.cos(a), s = Math.sin(a);
        const nx = x * c - z * s;
        const nz = x * s + z * c;
        x = nx;
        z = nz;
      }
      this.POS[i * 3] = x * br;
      this.POS[i * 3 + 1] = y;
      this.POS[i * 3 + 2] = z * br;
    }
    if (this._morph >= 1) this._spinPhase += 0.016 * Math.max(0.4, dt / 16.7);
  }

  _upload() {
    const vis = this._visList;
    for (let k = 0; k < vis.length; k++) {
      const i = vis[k];
      this._nPos[k * 3] = this.POS[i * 3];
      this._nPos[k * 3 + 1] = this.POS[i * 3 + 1];
      this._nPos[k * 3 + 2] = this.POS[i * 3 + 2];
      this._nCol[k * 3] = this.COL[i * 3];
      this._nCol[k * 3 + 1] = this.COL[i * 3 + 1];
      this._nCol[k * 3 + 2] = this.COL[i * 3 + 2];
      this._nSize[k] = this.SIZ[i];
      this._nA[k] = this.ALP[i];
    }
    const lk = this._visLinks;
    const idx = this._visIdx;
    for (let k = 0; k < lk.length; k++) {
      const l = lk[k];
      const ka = idx.get(l.a), kb = idx.get(l.b);
      const o = k * 6;
      this._ePos[o] = this._nPos[ka * 3];
      this._ePos[o + 1] = this._nPos[ka * 3 + 1];
      this._ePos[o + 2] = this._nPos[ka * 3 + 2];
      this._ePos[o + 3] = this._nPos[kb * 3];
      this._ePos[o + 4] = this._nPos[kb * 3 + 1];
      this._ePos[o + 5] = this._nPos[kb * 3 + 2];
      // 线色 = **关系类型**的颜色（与 2D、图例同表 LINK_TINT）：端点色会让六种关系长得一模一样，
      // 用户没法从线的外观区分「语义 / 时序 / 实体共指 / 关于某人 / 发生在该会话 / 成员关系」。
      // 未知类型（后端将来新增）沿用端点色兜底，绝不因为没配色就画不出线。
      const tint = LINK_TINT[l.kind];
      if (tint) {
        this._eCol[o] = tint[0];
        this._eCol[o + 1] = tint[1];
        this._eCol[o + 2] = tint[2];
        this._eCol[o + 3] = tint[0];
        this._eCol[o + 4] = tint[1];
        this._eCol[o + 5] = tint[2];
      } else {
        this._eCol[o] = this._nCol[ka * 3];
        this._eCol[o + 1] = this._nCol[ka * 3 + 1];
        this._eCol[o + 2] = this._nCol[ka * 3 + 2];
        this._eCol[o + 3] = this._nCol[kb * 3];
        this._eCol[o + 4] = this._nCol[kb * 3 + 1];
        this._eCol[o + 5] = this._nCol[kb * 3 + 2];
      }
    }
    const gl = this.gl;
    // ⚠️ 必须**按当前可见规模**上传：`_visEa()` 返回的 `_visEaBuf` 只增不减（换回小图后仍是
    //    上一张大图的长度），而 `_prepareDraw()` 是按当前规模 `bufferData` 分配 GL 缓冲的。
    //    整块上传会「小缓冲接大数组」→ bufferSubData 溢出被 GL 拒绝 → 线 alpha 缓冲停在
    //    全 0 → 线颜色乘 0 → 加法混合加了个黑 → **屏幕上一条线都没有**（点还正常，因为
    //    节点那几个 CPU 数组每次 _prepareDraw 都重分配、长度永远正确）。
    //    真机表现：首次加载有线 → 切全库有线 → 切回当前视角线消失且只能刷新。
    const up = (buf, arr, n) => {
      gl.bindBuffer(gl.ARRAY_BUFFER, buf);
      gl.bufferSubData(gl.ARRAY_BUFFER, 0, n != null && n < arr.length ? arr.subarray(0, n) : arr);
    };
    up(this.buf.nPos, this._nPos, vis.length * 3);
    up(this.buf.nCol, this._nCol, vis.length * 3);
    up(this.buf.nSize, this._nSize, vis.length);
    up(this.buf.nA, this._nA, vis.length);
    if (this.showLinks && lk.length) {
      up(this.buf.ePos, this._ePos, lk.length * 6);
      up(this.buf.eCol, this._eCol, lk.length * 6);
      up(this.buf.eA, this._visEa(), lk.length * 2);
    }
  }

  /** 可见边的 alpha 数组（按可见序重排，别直接用全量数组） */
  _visEa() {
    const lk = this._visLinks;
    if (!this._visEaBuf || this._visEaBuf.length < Math.max(2, lk.length * 2)) {
      this._visEaBuf = new Float32Array(Math.max(2, lk.length * 2));
    }
    for (let k = 0; k < lk.length; k++) {
      const l = lk[k];
      this._visEaBuf[k * 2] = this._ea[l.k * 2] || 0.03;
      this._visEaBuf[k * 2 + 1] = this._ea[l.k * 2 + 1] || 0.02;
    }
    return this._visEaBuf;
  }

  _camUpdate(dt) {
    if (!this._dragNode && !this._userHold && this.motion) this.cam.yaw += dt * 0.001 * this.P.spin * 26;
    const k = Math.min(1, dt * 3);
    this.cam.pitch += (this.cam.tPitch - this.cam.pitch) * k;
    this.cam.dist += (this.cam.tDist - this.cam.dist) * k;
    this.cam.ty += (this.cam.tTy - this.cam.ty) * k;
    const cp = Math.cos(this.cam.pitch), sp = Math.sin(this.cam.pitch);
    const eye = [Math.sin(this.cam.yaw) * cp * this.cam.dist, sp * this.cam.dist + this.cam.ty, Math.cos(this.cam.yaw) * cp * this.cam.dist];
    const ctr = [this.focus[0], this.cam.ty + this.focus[1], this.focus[2]];
    this._mPersp(this.FOV, this.W / Math.max(1, this.H), 0.1, 400, this._mat.proj);
    this._mLook(eye, ctr, [0, 1, 0], this._mat.view);
    this._mMul(this._mat.proj, this._mat.view, this._mat.mvp);
    // 平移量进矩阵：NDC_x = (X + dx*w)/w，所以把 dx 倍的第四行加到第一行；y 同理
    const m = this._mat.mvp;
    const dx = (2 * this.pan.x) / Math.max(1, this.W);
    const dy = (-2 * this.pan.y) / Math.max(1, this.H);
    if (dx || dy) {
      for (let c = 0; c < 4; c++) {
        const w = m[c * 4 + 3];
        m[c * 4 + 0] += dx * w;
        m[c * 4 + 1] += dy * w;
      }
    }
  }

  _draw(t) {
    this._renderNeb(t * 1000);
    const gl = this.gl;
    if (gl.isContextLost && gl.isContextLost()) return;
    const W = this.glCanvas.width, H = this.glCanvas.height;
    gl.disable(gl.BLEND);
    gl.useProgram(this.BG.p);
    gl.bindBuffer(gl.ARRAY_BUFFER, this.buf.quad);
    gl.enableVertexAttribArray(0);
    gl.vertexAttribPointer(0, 2, gl.FLOAT, false, 0, 0);
    gl.activeTexture(gl.TEXTURE0);
    gl.bindTexture(gl.TEXTURE_2D, this.texNeb);   // uGas 这个采样器现在指程序化星云
    gl.activeTexture(gl.TEXTURE1);
    gl.bindTexture(gl.TEXTURE_2D, this.texStar);
    gl.uniform1i(this.BG.u.uGas, 0);
    gl.uniform1i(this.BG.u.uStar, 1);
    gl.uniform2f(this.BG.u.uPar, this.cam.yaw, this.cam.pitch);
    gl.uniform2f(this.BG.u.uRes1, W, H);
    gl.uniform1f(this.BG.u.uTime, t);
    gl.uniform1f(this.BG.u.uStars, this._simple ? 0.0 : 1.0);
    gl.uniform1f(this.BG.u.uSimple, this._simple ? 1.0 : 0.0);
    gl.uniform1f(this.BG.u.uNeb, this.P.nebula == null ? 1.0 : this.P.nebula);
    gl.drawArrays(gl.TRIANGLES, 0, 3);

    gl.enable(gl.BLEND);
    gl.blendFunc(gl.ONE, gl.ONE);
    if (this.showLinks && this._visLinks.length) {
      gl.useProgram(this.LN.p);
      gl.uniformMatrix4fv(this.LN.u.uMVP, false, this._mat.mvp);
      gl.bindBuffer(gl.ARRAY_BUFFER, this.buf.ePos);
      gl.enableVertexAttribArray(0);
      gl.vertexAttribPointer(0, 3, gl.FLOAT, false, 0, 0);
      gl.bindBuffer(gl.ARRAY_BUFFER, this.buf.eCol);
      gl.enableVertexAttribArray(1);
      gl.vertexAttribPointer(1, 3, gl.FLOAT, false, 0, 0);
      gl.bindBuffer(gl.ARRAY_BUFFER, this.buf.eA);
      gl.enableVertexAttribArray(2);
      gl.vertexAttribPointer(2, 1, gl.FLOAT, false, 0, 0);
      gl.drawArrays(gl.LINES, 0, this._visLinks.length * 2);
    }

    gl.useProgram(this.PT.p);
    gl.uniformMatrix4fv(this.PT.u.uMVP, false, this._mat.mvp);
    gl.uniform1f(this.PT.u.uPS, H / 2 / Math.tan(this.FOV / 2));
    gl.uniform1f(this.PT.u.uGlow, this.P.glow);
    gl.bindBuffer(gl.ARRAY_BUFFER, this.buf.nPos);
    gl.enableVertexAttribArray(0);
    gl.vertexAttribPointer(0, 3, gl.FLOAT, false, 0, 0);
    gl.bindBuffer(gl.ARRAY_BUFFER, this.buf.nCol);
    gl.enableVertexAttribArray(1);
    gl.vertexAttribPointer(1, 3, gl.FLOAT, false, 0, 0);
    gl.bindBuffer(gl.ARRAY_BUFFER, this.buf.nSize);
    gl.enableVertexAttribArray(2);
    gl.vertexAttribPointer(2, 1, gl.FLOAT, false, 0, 0);
    gl.bindBuffer(gl.ARRAY_BUFFER, this.buf.nA);
    gl.enableVertexAttribArray(3);
    gl.vertexAttribPointer(3, 1, gl.FLOAT, false, 0, 0);
    gl.drawArrays(gl.POINTS, 0, this._visList.length);
  }

  /**
   * 烘焙星云/星空到纹理：一帧烤一片（scissor 限定行范围），
   * 这样启动时不会卡住几十毫秒，也能把昂贵的三层域扭曲烤到 1024×640。
   */
  /** 一次把 8 片全烤完（真机 GPU 上几十毫秒；后台标签 rAF 被节流也不受影响） */
  _bakeAll() {
    const guard = this._bakeStep;
    for (let i = 0; i < 32 && !this._built; i++) this._bakeSlice();
    if (!this._built) {
      // 理论上不会到这里；真到了也要放行，不能把画布一直黑着
      this._built = true;
      this._bakeFailed = this._bakeFailed || "烘焙未完成就放行";
    }
    void guard;
    return this._built;
  }

  _bakeSlice() {
    const gl = this.gl;
    const W = this.BAKE_W, H = this.BAKE_H;
    const SLICES = 4;
    const step = this._bakeStep;
    if (step >= SLICES) return true; // 只烤星空：星云已改为每帧程序化生成（不是贴图）
    const isGas = false;
    const i = step;
    const h = Math.ceil(H / SLICES);
    const y0 = i * h;
    const hh = Math.min(h, H - y0);
    if (hh <= 0) { this._bakeStep++; return false; }
    const prog = isGas ? this.BAKE : this.STAR;
    gl.bindFramebuffer(gl.FRAMEBUFFER, this.fbo);
    gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, isGas ? this.texGas : this.texStar, 0);
    gl.viewport(0, 0, W, H);
    gl.enable(gl.SCISSOR_TEST);
    gl.scissor(0, y0, W, hh);
    gl.clearColor(0, 0, 0, 1);
    gl.clear(gl.COLOR_BUFFER_BIT);
    gl.disable(gl.BLEND);
    gl.useProgram(prog.p);
    gl.bindBuffer(gl.ARRAY_BUFFER, this.buf.quad);
    gl.enableVertexAttribArray(0);
    gl.vertexAttribPointer(0, 2, gl.FLOAT, false, 0, 0);
    gl.uniform2f(prog.u.uRes, W, H);
    gl.drawArrays(gl.TRIANGLES, 0, 3);
    gl.disable(gl.SCISSOR_TEST);
    gl.bindFramebuffer(gl.FRAMEBUFFER, null);
    gl.viewport(0, 0, this.glCanvas.width, this.glCanvas.height);
    this._bakeStep++;
    if (this._bakeStep >= SLICES * 2) this._built = true;
    return this._built;
  }

  /** 叠加层：标签 + 悬停双环 + 选中旋转弧线 */
  _drawOverlay() {
    const ctx = this.fx;
    const W = this.W, H = this.H;
    ctx.clearRect(0, 0, W, H);
    if (!this._scrX) return;
    const scr = [0, 0, 0];
    const labels = [];
    const vis = this._visList;
    for (let i = 0; i < this.nodes.length; i++) this._scrW[i] = -1;
    for (let k = 0; k < vis.length; k++) {
      const i = vis[k];
      this._project(this.POS[i * 3], this.POS[i * 3 + 1], this.POS[i * 3 + 2], scr);
      if (scr[2] < 0.5 || scr[2] > 120) continue;
      this._scrX[i] = scr[0];
      this._scrY[i] = scr[1];
      this._scrW[i] = scr[2];
      const px = (this.SIZ[i] * (H / 2)) / Math.tan(this.FOV / 2) / scr[2];
      this._scrR[i] = px;
      const gp = px * this.P.glow;
      const dense = this._visList.length > 600;
      const need = dense ? 5.6 : 3.4;
      if (this.showLabels && this.ALP[i] > 0.3 && (i === this.selected || i === this.hover || gp > need)) {
        labels.push(i);
      }
    }
    labels.sort((a, b) => this._scrW[a] - this._scrW[b]);
    const cap = Math.min(labels.length, 70);
    ctx.textAlign = "center";
    ctx.font = '10.5px Consolas,"Courier New",monospace';
    for (let k = 0; k < cap; k++) {
      const i = labels[k];
      const a = clamp((6.5 - this._scrW[i] / 12) * 0.16, 0.12, 0.92) * (i === this.selected || i === this.hover ? 1.4 : 1);
      ctx.fillStyle = "rgba(196,224,255," + a + ")";
      const nm = this.nodes[i].name;
      ctx.fillText(nm.length > 18 ? nm.slice(0, 17) + "…" : nm, this._scrX[i], this._scrY[i] + this._scrR[i] * this.P.glow + 11);
    }
    if (this.hover >= 0 && this._scrW[this.hover] > 0) {
      const i = this.hover;
      ctx.strokeStyle = "rgba(120,225,255,.95)";
      ctx.lineWidth = 1.2;
      ctx.beginPath();
      ctx.arc(this._scrX[i], this._scrY[i], Math.max(9, this._scrR[i] * this.P.glow + 6), 0, TAU);
      ctx.stroke();
      ctx.strokeStyle = "rgba(120,225,255,.28)";
      ctx.beginPath();
      ctx.arc(this._scrX[i], this._scrY[i], Math.max(15, this._scrR[i] * this.P.glow + 13), 0, TAU);
      ctx.stroke();
    }
    if (this.selected >= 0 && this._scrW[this.selected] > 0) {
      const i = this.selected;
      ctx.strokeStyle = "rgba(255,214,102,.95)";
      ctx.lineWidth = 1.4;
      const r = Math.max(13, this._scrR[i] * this.P.glow + 9);
      const tt = performance.now() / 700;
      for (let q = 0; q < 4; q++) {
        ctx.beginPath();
        ctx.arc(this._scrX[i], this._scrY[i], r, tt + (q * TAU) / 4, tt + (q * TAU) / 4 + 0.5);
        ctx.stroke();
      }
    }
  }

  _tick(tms) {
    // 渲染循环里任何异常都不许静默：以前会直接"停帧"，画布变黑却看不出为什么。
    // 现在把错误写到叠加层上（真机截图/肉眼都能看到），并广播给页面。
    // ⚠️⚠️ 关键：**异常绝不能让循环断链**。原实现 catch 里 `_running = false` 且不再排帧，
    //   于是「某一帧抛异常」= 整个引擎永久死亡：画面冻在最后一帧（preserveDrawingBuffer 让它
    //   看起来像"还活着"），用户只能刷新页面。真机实测指纹：_brk 从未被写过（BRK=-）、
    //   帧计数不增长（FR=0）、而 _running 仍为 true、_raf 还挂着旧句柄。
    const now0 = tms || (typeof performance !== "undefined" ? performance.now() : Date.now());
    this._beat = now0;                                  // 心跳：看门狗据此判断循环是否真的在出帧
    this._raf = 0;                                      // 先清零：用「_raf 有没有被重新赋值」判断本帧是否排上了下一帧
    try {
      this._tickInner(tms);
    } catch (err) {
      this._errCount = (this._errCount || 0) + 1;
      this._fatal = String((err && err.message) || err);
      // 只在前几次/每 120 帧打一次日志，避免每帧刷屏
      if (this._errCount <= 3 || this._errCount % 120 === 0) {
        console.warn("[hf3d] 渲染帧异常（循环保活，第 " + this._errCount + " 次）", err);
      }
      try {
        this._drawFatal(this._fatal);
      } catch (err2) { /* 叠加层也画不出来就算了 */ }
      if (this._errCount === 1 || this._errCount % 120 === 0) this.emit("fatal", this._fatal);
    } finally {
      // 无论如何都要把下一帧排上（异常打断路径下 _tickInner 来不及排帧）
      if (this._running && !this._raf) {
        this._raf = requestAnimationFrame((tt) => this._tick(tt));
      }
    }
  }

  /** 把致命错误画在画布上（含提示），避免"黑屏无解释" */
  _drawFatal(msg) {
    const ctx = this.fx;
    const W = this.W, H = this.H;
    ctx.clearRect(0, 0, W, H);
    ctx.fillStyle = "rgba(6,14,34,.86)";
    ctx.fillRect(16, 16, Math.min(W - 32, 620), 76);
    ctx.strokeStyle = "rgba(255,120,140,.9)";
    ctx.lineWidth = 2;
    ctx.strokeRect(16, 16, Math.min(W - 32, 620), 76);
    ctx.fillStyle = "#ffd6de";
    ctx.font = '13px Consolas,"Courier New",monospace';
    ctx.textAlign = "left";
    ctx.fillText("3D 渲染异常：" + String(msg).slice(0, 90), 30, 46);
    ctx.fillStyle = "#8dbdf0";
    ctx.font = '11.5px Consolas,"Courier New",monospace';
    ctx.fillText("已停帧；点右上「复位视图」或刷新页面可重试", 30, 70);
  }

  _tickInner(tms) {
    // 画布曾经挂进过文档 → 之后再离开（换页卸载 / 引擎销毁）就允许停循环；
    // 但**首次挂载阶段还不能停**：这一刻 isConnected 可能还是 false，
    // 而上下文丢失的自愈轮询正是靠循环一直跑着才有机会恢复。
    // 原实现把「上下文丢失」放在前面、isConnected 判定放后面，于是
    // 「上下文丢失 + 画布已卸载」时会每秒重建、**永久 rAF**（destroy() 之外的路径停不下来）。
    if (this.glCanvas && this.glCanvas.isConnected) this._everConnected = true;
    if (this._everConnected && (!this.glCanvas || !this.glCanvas.isConnected)) {
      this._brk = "detached";                   // 【诊断】循环停在哪条早退分支
      this._running = false;
      return;
    }
    // 上下文丢失时**绝不能停循环**：后台标签丢了 WebGL 上下文后，浏览器常常不发
    // webglcontextrestored 事件；循环一停就永远醒不过来（用户看到的就是"星云突然没了、
    // 只能刷新网页"）。这里每秒主动重试重建一次，恢复就自愈，不依赖那个事件。
    const lost = this._ctxLost || (this.gl && this.gl.isContextLost && this.gl.isContextLost());
    if (lost) {
      this._brk = "ctxLost";                    // 【诊断】
      if (!this._ctxPollT || tms - this._ctxPollT > 1000) {
        this._ctxPollT = tms;
        try {
          this._rebuildGL();               // 上下文已恢复时这次就会成功
          this._ctxLost = false;
          this._ctxFail = 0;
        } catch (err) {
          this._ctxFail = (this._ctxFail || 0) + 1;
          // 兜底：连着重试多次都失败、而上下文其实**没**丢（说明是重建过程里某步抛错）
          // → 不再无限等待，直接清旗子放行渲染，避免画面永久停在旧帧、只能刷新页面。
          const reallyLost = this.gl && this.gl.isContextLost && this.gl.isContextLost();
          if (this._ctxFail >= 3 && !reallyLost) {
            this._ctxLost = false;
            this._fatal = null;
            this._ctxFail = 0;
            this._built = true;
            this._kick();
          }
        }
      }
      this._raf = requestAnimationFrame((tt) => this._tick(tt));
      return;
    }
    const now = tms || performance.now();
    const dt = this._last ? clamp(now - this._last, 1, 60) : 16.7;
    this._last = now;
    // 启动时把星云/星空分片烤进纹理（一帧一片），烤完再进入正常渲染节奏
    if (!this._built) {
      this._brk = "bake";                       // 【诊断】
      try {
        this._bakeAll();
      } catch (err) {
        // 烘焙失败（老驱动/上下文异常）：放弃烘焙，直接走正常渲染，别让画布一直黑着
        this._built = true;
        this._bakeFailed = String((err && err.message) || err);
        console.warn("[hf3d] 星云烘焙失败，改用兜底背景", err);
      }
      this._raf = requestAnimationFrame((tt) => this._tick(tt));
      return;
    }
    this._brk = "draw";                         // 【诊断】真正进入渲染
    this._camUpdate(dt);
    this._framePositions(dt);
    this._upload();
    this._draw(now / 1000);
    this._drawOverlay();

    // 星云看门狗：每 12 秒查一次背景是不是整片失效；最多自愈 3 次（避免反复无意义重烤）
    if (now - (this._wdT || 0) > 12000) {
      this._wdT = now;
      if ((this._bgRefixes || 0) < 3) this._bgWatchdog();
    }

    this._frames++;
    if (now - this._fpsT > 500) {
      this._fps = Math.round((this._frames * 1000) / (now - this._fpsT));
      this._frames = 0;
      this._fpsT = now;
      if (this._fps && this._fps < 24 && this._quality === "high" && this.nodes.length > 300) {
        this._lowCount++;
        if (this._lowCount >= 4) {
          this._quality = "low";
          this.resize();
          this.emit("quality", "low");
        }
      } else if (this._fps > 45) {
        this._lowCount = 0;
      }
      if (this.tour.on) {
        this.tour.t += 0.5;
        if (this.tour.t > 9) {
          this.tour.t = 0;
          this.tour.i = (this.tour.i + 1) % LAYOUT_DEFS.length;
          this.setLayout(LAYOUT_DEFS[this.tour.i].key, false);
        }
      }
      this.emit("stats", this.stats());
    }
    this._raf = requestAnimationFrame((tt) => this._tick(tt));
  }

  /**
   * 面板侧定时调用的「防冻死」健康检查（外部时钟兜底，不靠自身 rAF）。
   * 为什么必须这样：渲染循环一旦没排上帧（_running=true 但 _raf=0），自身就**没有任何机会**
   * 再恢复 —— 没有下一帧来救自己。真机实测就是这个状态：RUN=1 / UP=0 / BUILT=0，
   * 画布永久停在旧帧或全黑，用户只能刷新页面。
   */
  ensureAlive() {
    try {
      const attached = !this.glCanvas || this.glCanvas.isConnected;
      if (!attached) return false;                        // 已卸载：不要复活（省电、别刷错误）
      // 1) 焙烤卡住 → 直接放行（背景退化为兜底，别把整幅图一起拖黑）
      if (!this._built) {
        try { this._bakeAll(); } catch (err) { /* 烘焙失败也放行 */ }
        this._built = true;
      }
      // 2) 上下文其实没丢、旗子却还立着 → 放行
      if (this._ctxLost && !(this.gl && this.gl.isContextLost && this.gl.isContextLost())) {
        this._ctxLost = false;
        this._ctxFail = 0;
      }
      // 3) 按**心跳**判断循环是否真的在出帧：距上一帧 > 1.5s ⇒ 循环已死
      //    （哪怕 _running=true、_raf 还挂着旧句柄 —— 异常打断 rAF 链时就是这副样子）→ 强制重启。
      //    先 cancel 掉可能挂着的旧句柄，保证重启后只有一条 rAF 链。
      const now = (typeof performance !== "undefined" ? performance.now() : Date.now());
      const stale = !this._beat || (now - this._beat) > 1500;
      if (stale || !this._running || !this._raf) {
        try { if (this._raf) cancelAnimationFrame(this._raf); } catch (err) { /* 忽略 */ }
        this._raf = 0;
        this._running = false;
        this._kick();
        return true;
      }
      return true;
    } catch (err) {
      return false;
    }
  }

  _kick() {
    // 循环「自以为在跑」但实际没有排帧时（_raf=0）也要能拉起来，避免永久停帧
    if (this._running && this._raf) return;
    this._running = true;
    this._last = 0;
    this._raf = requestAnimationFrame((tt) => this._tick(tt));
  }

  /* ---------------- 与 canvas2D 引擎同名的方法（切换引擎时页面代码不用改） ---------------- */

  setMode(key) { return this.setLayout(key, false); }
  setMotion(on) { this.motion = !!on; this._kick(); return this; }
  setPhysics() { return this; }
  setQuality(q) {
    this._quality = String(q) === "low" ? "low" : "high";
    this.resize();
    return this;
  }
  fit() {
    this.focus = [0, 0, 0];
    this.setCam("orbit");
    return this;
  }
  snapshot() {
    try {
      const c = document.createElement("canvas");
      c.width = this.glCanvas.width;
      c.height = this.glCanvas.height;
      const g = c.getContext("2d");
      g.drawImage(this.glCanvas, 0, 0);
      g.drawImage(this.fxCanvas, 0, 0);
      return c.toDataURL("image/png");
    } catch (err) {
      return "";
    }
  }
  destroy() {
    this._running = false;
    if (this._raf) cancelAnimationFrame(this._raf);
    if (this._onResize) window.removeEventListener("resize", this._onResize);
    if (this._ro) {
      try { this._ro.disconnect(); } catch (err) { /* ignore */ }
    }
    try {
      const ext = this.gl && this.gl.getExtension("WEBGL_lose_context");
      if (ext) ext.loseContext();
    } catch (err) { /* ignore */ }
    try { this.host.textContent = ""; } catch (err) { /* 容器没了就算了 */ }
  }
}

export default Graph3D;

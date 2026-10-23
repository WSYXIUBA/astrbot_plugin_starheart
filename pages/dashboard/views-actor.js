/**
 * 人格与表达 · 合并页
 *
 * 数据来自 actor/*（core/actor.py 的读模型）：审核队列 / 表达库 / 人格与 revision /
 * 注入预览。所有「看原对话」都走同一个来处接口，不在前端拼假证据。
 */
import {
  $, esc, jsStr, fmtNum, fmtTime, timeAgo, apiSafe, apiPostSafe,
  pageHead, card, kpis, kpiCard, secTitle, toast,
  openDrawer, closeDrawer, refreshCurrent, goPage,
} from "./store.js?v=1.0.5-3";

// 页签 id 必须与 app.js 的 actor.subs 一致；这里只用来校验子路由，显示文案以 app.js 为准。
const SUBS = ["review", "voice", "persona", "inject"];

const A = {
  personaId: "",
  filter: "all",
  msg: "你还记得我上次说的那件事吗？",
  reg: "private",
};

const CSS = `
.actor-wrap{display:flex;flex-direction:column;gap:12px}
.actor-review .prov-src{margin-top:7px}
.actor-path{font-size:10.5px;color:var(--tx3);line-height:1.7;margin-top:4px}
.actor-path .arr{color:var(--gold);margin:0 5px}
.actor-src{border-left:2px solid var(--hair);padding:2px 0 2px 9px;margin-top:7px}
.actor-line{font-size:11px;line-height:1.65;color:var(--tx2)}
.actor-line .who{color:var(--tx1);margin-right:5px}
.actor-line.bot{color:var(--tx1)}
.actor-line .pick{color:var(--gold);font-size:9px;letter-spacing:.08em;margin-left:6px}
.prov-q{font-family:Georgia,'Songti SC',serif;font-size:19px;line-height:1.5;margin:10px 0 4px;color:var(--ink)}
.prov-steps{border-left:1px solid var(--hair);padding-left:14px;margin:4px 0 2px 5px}
.prov-step{position:relative;padding-bottom:11px}
.prov-step:last-child{padding-bottom:0}
.prov-step:before{content:"";position:absolute;left:-18px;top:5px;width:5px;height:5px;border-radius:50%;background:var(--gold)}
.prov-step b{font-family:var(--mono);font-size:9.5px;color:var(--tx3);font-weight:400;margin-right:8px}
.prov-step span{font-size:11.5px;color:var(--tx1)}
.prov-step p{margin:2px 0 0;font-size:10.5px;color:var(--tx2);line-height:1.65}
.prov-chat{display:flex;flex-direction:column;gap:8px;margin-top:6px}
.prov-msg{max-width:90%}
.prov-msg.user{align-self:flex-start}
.prov-msg.bot{align-self:flex-end}
.prov-msg .who{font-size:9.5px;color:var(--tx3);margin-bottom:3px}
.prov-msg.bot .who{text-align:right}
.prov-msg .who span{font-family:var(--mono);margin-left:5px}
.prov-msg .bub{border:1px solid var(--hair);background:var(--surface2);padding:7px 10px;font-size:11.5px;line-height:1.7;text-align:left;white-space:pre-wrap}
.prov-msg.picked .bub{border-color:var(--gold);background:rgba(201,169,110,.12)}
.prov-msg .pick{font-size:9px;color:var(--gold);letter-spacing:.08em;margin-top:3px;text-align:right}
.prov-why{margin:4px 0 0;padding-left:16px;font-size:11px;color:var(--tx2);line-height:1.8}
.prov-why li::marker{color:var(--gold)}
.prov-usage{border-top:1px solid var(--hair);margin-top:12px;padding-top:8px}
.drawer.wide{width:560px;max-width:96vw}
.drawer.wide:not(.show){right:-580px}
.src-link{background:none;border:0;padding:0;margin-left:4px;color:var(--gold);font:inherit;font-size:10.5px;cursor:pointer;border-bottom:1px solid rgba(201,169,110,.35)}
.ask-mask{position:fixed;inset:0;z-index:9999;display:flex;align-items:center;justify-content:center;background:rgba(0,0,0,.45)}
.ask-box{width:min(520px,90vw);background:var(--card);border:1px solid var(--bd);border-radius:12px;padding:16px;box-shadow:0 24px 60px rgba(0,0,0,.5)}
.ask-box .t{font-size:13px;font-weight:650;margin-bottom:10px}
.ask-box textarea{width:100%;box-sizing:border-box;min-height:40px;background:var(--surface2);color:var(--tx1);border:1px solid var(--bd);border-radius:8px;padding:8px 10px;font:inherit;font-size:13px;resize:vertical}
.ask-box .b{display:flex;gap:8px;justify-content:flex-end;margin-top:12px}
@media (max-width:640px){.drawer.wide{width:100vw}}
/* 人格左右对比：左=当前（红色删除），右=更新后（绿色新增） */
.sbs{border:1px solid var(--bd);border-radius:8px;overflow:hidden;margin-top:8px;font-family:ui-monospace,SFMono-Regular,Consolas,monospace;background:var(--surface2)}
.sbs-hd,.sbs-row{display:grid;grid-template-columns:1fr 1fr}
.sbs-hd{font-size:10px;color:var(--tx3);border-bottom:1px solid var(--bd);background:var(--surface)}
.sbs-hd span{padding:5px 9px}
.sbs-hd span+span{border-left:1px solid var(--bd)}
.sbs-body{max-height:300px;overflow:auto}
.sbs-cell{display:flex;gap:7px;padding:0 9px;font-size:11px;line-height:1.75;white-space:pre-wrap;word-break:break-word;align-items:flex-start}
.sbs-cell+.sbs-cell{border-left:1px solid var(--bd)}
.sbs-row+.sbs-row .sbs-cell{border-top:1px solid rgba(127,127,127,.10)}
.sbs-no{color:var(--tx3);font-size:9.5px;min-width:18px;text-align:right;flex:none;line-height:1.9}
.sbs-tx{min-width:0}
.sbs-del{background:rgba(224,72,72,.20);box-shadow:inset 3px 0 0 #d9534f}
.sbs-del .sbs-no{color:#e0685f}
.sbs-del .sbs-mk{color:#e0685f}
.sbs-ins{background:rgba(46,196,124,.20);box-shadow:inset 3px 0 0 #2fb472}
.sbs-ins .sbs-no{color:#3fbf84}
.sbs-ins .sbs-mk{color:#3fbf84}
.sbs-none{background:rgba(127,127,127,.05)}
.sbs-mk{width:11px;flex:none;font-weight:700;line-height:1.9}
.sbs-foot{padding:6px 9px;font-size:10px;color:var(--tx3);border-top:1px solid var(--bd)}
.sbs-lg{display:flex;gap:14px;padding:4px 9px;font-size:10px;border-bottom:1px solid var(--bd);background:var(--surface)}
.sbs-lg .del{color:#e0685f}
.sbs-lg .ins{color:#3fbf84}
.prov-thread{border-top:1px dashed var(--bd);padding-top:4px;margin-top:8px}
.prov-thread:first-child{border-top:0;margin-top:0;padding-top:0}
.prov-thread-h{font-size:10px;color:var(--tx3);margin-bottom:4px}
.prov-ctx{margin-top:10px;font-size:10.5px;color:var(--tx3)}
.prov-ctx summary{cursor:pointer;color:var(--tx3);padding:4px 0}
.prov-ctx .prov-chat{margin-top:4px}
`;

const TYPE_CN = { expr: "表达", jargon: "群内用语", persona: "人格提案", quote: "原话", style: "说话方式", term: "口头禅" };
const STATUS_CN = {
  pending: ["待审", "warn"], active: ["生效中", "ok"], approved: ["已写入", "ok"],
  rejected: ["已驳回", "err"], stale: ["已过期", "warn"], expired: ["超时作废", "warn"],
  paused: ["已停用", "err"], dropped: ["已停用", "err"], ai_pending: ["待审", "warn"],
  confirmed: ["生效中", "ok"], denied: ["已否定", "err"],
};

function stateTag(s) {
  const [txt, cls] = STATUS_CN[String(s || "")] || [String(s || "—"), ""];
  return `<span class="tag ${cls}">${esc(txt)}</span>`;
}

/* 左右并排 diff：左=当前人格（红=删），右=更新后（绿=新增） */
function sbsHtml(rows, opts = {}) {
  const list = Array.isArray(rows) ? rows : [];
  if (!list.length) return "";
  const max = Number(opts.max) || 0;
  const shown = max > 0 ? list.slice(0, max) : list;
  const cls = (t) => (t === "del" ? "sbs-del" : t === "ins" ? "sbs-ins" : t === "none" ? "sbs-none" : "");
  const mark = (t) => (t === "del" ? "−" : t === "ins" ? "+" : "");
  const cell = (t, no, text) => `<div class="sbs-cell ${cls(t)}"><span class="sbs-no">${no ? fmtNum(no) : ""}</span><span class="sbs-mk">${mark(t)}</span><span class="sbs-tx">${esc(text || "")}</span></div>`;
  const more = list.length > shown.length
    ? `<div class="sbs-foot">还有 ${fmtNum(list.length - shown.length)} 行未展开，点「看完整对比」看全部</div>` : "";
  return `<div class="sbs">
    <div class="sbs-hd"><span>${esc(opts.left || "当前人格")}</span><span>${esc(opts.right || "更新后")}</span></div>
    <div class="sbs-lg"><span class="del">− 左边：被删掉的行</span><span class="ins">+ 右边：新增的行</span></div>
    <div class="sbs-body">${shown.map((r) => `<div class="sbs-row">${cell(r.lt, r.ln, r.l)}${cell(r.rt, r.rn, r.r)}</div>`).join("")}</div>
    ${more}</div>`;
}

function reviewPreview(it, botName) {
  const rows = it.preview || [];
  if (!rows.length) return "";
  return `<div class="actor-src">${rows.map((m) => {
    const bot = m.side === "bot";
    return `<div class="actor-line ${bot ? "bot" : ""}"><span class="who">${esc(m.who || (bot ? (botName || "助手") : "对方"))}</span>${esc(m.text || "")}${bot ? '<span class="pick">学到的是这句</span>' : ""}</div>`;
  }).join("")}</div>`;
}

/* ---------------- 审核 ---------------- */
async function reviewView(ov) {
  const rv = await apiSafe("actor/review", { limit: 100 }, { items: [], counts: {}, __err: "" });
  if (rv.__err) return card(`<div class="pd">审核队列读取失败：${esc(rv.__err)}</div>`);
  const c = rv.counts || {};
  const items = rv.items || [];
  const botName = String((ov.persona || {}).id || "");
  const chips = [
    ["all", "全部", items.length],
    ["expr", "表达", c.expr || 0],
    ["jargon", "群内用语", c.jargon || 0],
  ];
  const personaPend = Number(c.persona || 0);
  const personaPtr = personaPend
    ? `<div class="noteblock" style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
        <b>还有 ${fmtNum(personaPend)} 条人格提案待审</b><span class="mut">在「人格」页处理（带左右对比），这里不重复列</span>
        <button class="btn xs" style="margin-left:auto" onclick="goPage('actor','persona')">去人格页 →</button></div>`
    : "";
  const head = kpis(
    kpiCard("actPersona", esc((ov.persona || {}).id || "—"), "当前人格")
    + kpiCard("actPend", fmtNum(Number(c.expr || 0) + Number(c.jargon || 0)), "本页待审")
    + kpiCard("actCap", fmtNum((ov.quota || {}).inject_cap || 0), "单轮注入上限")
  );
  const list = items.length
    ? items.map((it) => {
      const kind = String(it.kind || "");
      const title = kind === "persona" ? "人格提案" : (it.title || "");
      const sub = kind === "persona" ? `#${it.id} · ${it.subtitle || ""}` : (it.subtitle || "");
      const body = it.body ? `<div class="pd" style="margin-top:4px">${esc(it.body)}</div>` : "";
      const reason = it.reason ? `<div class="pd" style="margin-top:5px">模型理由：${esc(it.reason)}</div>` : "";
      return `<div class="card cp actor-review" data-kind="${esc(kind)}" data-id="${Number(it.id) || 0}">
        <div class="row bt">
          <div class="row" style="gap:7px"><span class="tag">${esc(it.type || TYPE_CN[kind] || kind)}</span>
            <span class="chip">${esc(it.scope || "—")}</span>
            <span class="mut" style="font-size:10.5px">${esc(timeAgo(it.created_at))}</span></div>
          <div class="row" style="gap:8px"><span class="mono mut" style="font-size:10.5px">#${Number(it.id) || 0}</span>
            <button class="btn xs" onclick="hfActorProv('${esc(kind)}',${Number(it.id) || 0})">${kind === "persona" ? "看完整对比" : "看原对话"}</button></div>
        </div>
        <div style="font-size:15px;margin:9px 0 2px">${esc(title)}</div>
        <div class="pd">${esc(sub)}</div>
        ${reviewPreview(it, botName)}${body}
        ${kind === "persona" ? `${it.base_stale ? '<div class="noteblock" style="margin-top:8px">提案基线已与当前人格不同：左边显示的是现在的真身。</div>' : ""}${sbsHtml(it.sbs, { max: 16 })}` : ""}
        ${reason}
        <div class="row" style="gap:6px;margin-top:11px;padding-top:10px;border-top:1px solid var(--bd)">
          <button class="btn xs p" onclick="hfActorDecide('${esc(kind)}',${Number(it.id) || 0},'approve',this)">${kind === "persona" ? "通过并写入" : "通过"}</button>
          <button class="btn xs" onclick="hfActorProv('${esc(kind)}',${Number(it.id) || 0})">${kind === "persona" ? "看完整对比" : "看原对话"}</button>
          <button class="btn xs" onclick="hfActorDecide('${esc(kind)}',${Number(it.id) || 0},'reject',this)">驳回</button>
          <span class="mut" style="font-size:10.5px;margin-left:auto">${kind === "persona" ? "写前快照 · 写后回读" : "只影响当前人格"}</span>
        </div>
      </div>`;
    }).join("")
    : card(`<div class="pd">队列是空的。学到的候选会先出现在这里；通过之后才进「表达」并在下一轮编译。</div>`);
  return `<div class="actor-wrap">
    ${head}
    ${personaPtr}
    <div class="chips" id="actFilters">${chips.map(([k, label, n]) =>
      `<span class="chip${A.filter === k ? " hot" : ""}" data-f="${k}" onclick="hfActorFilter('${k}')">${label} ${fmtNum(n)}</span>`).join("")}
      <span class="spacer"></span><span class="mut" style="font-size:10.5px;align-self:center">按创建时间倒序</span></div>
    <div class="col" id="actReviewList" style="gap:10px">${list}</div>
  </div>`;
}

/* ---------------- 表达 ---------------- */
async function voiceView() {
  const v = await apiSafe("actor/voice", {}, { styles: [], terms: [], counts: {}, __err: "" });
  if (v.__err) return card(`<div class="pd">表达库读取失败：${esc(v.__err)}</div>`);
  const styles = v.styles || [];
  const terms = v.terms || [];
  const jargon = v.jargon || [];
  const styleCards = styles.length ? `<div class="grid g2">${styles.map((s) => {
    const qs = s.quotes || [];
    const first = qs.slice(0, 2).map((q) => `「${esc(String(q.quote || "").split("\n").join(" / "))}」`).join(" ");
    return `<div class="card cp">
      <div class="row bt"><h3>${esc(s.name || "未命名")}</h3>${stateTag(s.status)}</div>
      <div class="pd" style="margin-top:4px">${fmtNum(s.active)} 条生效原话 · 命中 ${fmtNum(s.hits)} · 纠正 ${fmtNum(s.corrections)}</div>
      <div class="actor-path">${fmtNum(qs.length)} 句他说过的话<span class="arr">→</span>同一个说话方式<span class="arr">→</span>回复时照着口吻来</div>
      <div class="pd" style="margin-top:5px;font-size:11px">${first || "他还没说过什么"}</div>
      <div class="row" style="gap:6px;margin-top:9px">
        <button class="btn xs" onclick="hfActorProv('style',${Number(s.id) || 0})">看学习过程</button>
        <button class="btn xs" onclick="hfActorRename(${Number(s.id) || 0},${jsStr(s.name || "")})">改名</button>
        <button class="btn xs" onclick="hfActorStyleStatus(${Number(s.id) || 0},${jsStr(s.status === "active" ? "paused" : "active")})">${s.status === "active" ? "停用" : "启用"}</button>
      </div>
    </div>`;
  }).join("")}</div>` : card(`<div class="pd">还没有学到他的说话方式。点右上角「立即学一批」，从记忆里他说过的话学。</div>`);

  const quoteRows = styles.flatMap((s) => (s.quotes || []).map((q) => ({ ...q, style: s.name })));
  const quoteList = quoteRows.length ? quoteRows.map((q) => `<div class="prow" style="padding:9px 14px">
    <div><div class="pn">「${esc(String(q.quote || "").split("\n").join(" / "))}」</div>
      <div class="pd">${esc(q.style || "")} · ${esc(q.register === "group" ? "群聊" : "私聊")} · 记忆 #${Number(q.memory_id) || 0}
        <button class="src-link" onclick="hfActorProv('quote',${Number(q.id) || 0})">看原对话</button></div></div>
    <div class="pc">${stateTag(q.status)}<span class="mono mut" style="font-size:11px">${fmtNum(q.hits)}</span>
      <button class="btn xs" onclick="hfActorEditQuote(${Number(q.id) || 0},${jsStr(q.quote || "")})">改</button>
      <button class="btn xs" onclick="hfActorQuoteStatus(${Number(q.id) || 0},${jsStr(q.status === "active" ? "dropped" : "active")})">${q.status === "active" ? "停用" : "恢复"}</button></div>
  </div>`).join("") : `<div class="pd" style="padding:10px 14px">他还没说过什么</div>`;

  const termList = terms.length ? terms.map((t) => `<div class="prow" style="padding:9px 14px">
    <div><div class="pn">${esc(t.term || "")}</div>
      <div class="pd">${esc(t.meaning || t.usage || "—")} · ${esc(t.scope || "全局")} · 命中 ${fmtNum(t.hits)}
        <button class="src-link" onclick="hfActorProv('term',${Number(t.id) || 0})">看出处</button></div></div>
    <div class="pc">${stateTag(t.enabled ? "active" : "paused")}
      <button class="btn xs" onclick="hfActorEditTerm(${Number(t.id) || 0},${jsStr(t.meaning || "")},${jsStr(t.usage || "")})">改</button></div>
  </div>`).join("") : `<div class="pd" style="padding:10px 14px">还没有口头禅。学到的口癖词会落在这一栏。</div>`;

  const jargonList = jargon.length ? jargon.map((j) => `<div class="prow" style="padding:9px 14px">
    <div><div class="pn">${esc(j.term || "")}</div>
      <div class="pd">${esc(j.meaning || j.usage || "—")} · ${esc(j.scope || "全局")} · 命中 ${fmtNum(j.hit_count)}
        <button class="src-link" onclick="hfActorProv('jargon',${Number(j.id) || 0})">看出处</button></div></div>
    <div class="pc">${stateTag(j.status)}</div>
  </div>`).join("") : `<div class="pd" style="padding:10px 14px">还没有过审的群内用语。待审的在上一个页签「审核」。</div>`;

  return `<div class="actor-wrap">
    ${secTitle(`说话方式 · ${styles.length} 种`)}
    ${styleCards}
    ${secTitle(`他说过的话 · ${quoteRows.length} 条 · 每条都能回到原对话`)}
    <div class="card">${quoteList}</div>
    ${secTitle(`口头禅 · ${terms.length} 条 · 只在消息里命中时才进提示词`)}
    <div class="card">${termList}</div>
    ${secTitle(`群内用语 · ${jargon.length} 条 · 只用于理解，不进回复`)}
    <div class="card">${jargonList}</div>
    ${Number((v.counts || {}).unattributed || 0) ? `<div class="noteblock"><b>还有 ${fmtNum(v.counts.unattributed)} 条未归属</b>：人格隔离关闭时学的，或旧数据没有 persona_id；它们不会参与编译。</div>` : ""}
  </div>`;
}

/* ---------------- 人格 ---------------- */
async function personaView(ov) {
  const p = await apiSafe("actor/personas", {}, { items: [], active: "", __err: "" });
  if (p.__err) return card(`<div class="pd">人格列表读取失败：${esc(p.__err)}</div>`);
  const items = p.items || [];
  const active = String(p.active || (ov.persona || {}).id || "");
  const want = String(A.personaId || active);
  const cur = items.find((x) => x.id === want) || items.find((x) => x.id === active) || items[0] || { id: "", prompt: "", chars: 0, hash: "" };
  A.personaId = String(cur.id || "");
  const rev = await apiSafe("actor/revisions", { persona_id: cur.id }, { items: [], __err: "" });
  const left = `<div class="card cp">
    <div class="sec-t" style="margin-top:0">人格列表</div>
    ${items.map((x) => `<div class="prow"><div><div class="pn">${esc(x.id || "—")}</div>
      <div class="pd">${fmtNum(x.chars)} 字 · ${esc(x.hash || "")}</div></div>
      <div class="pc">${x.active && cur.id === x.id
        ? '<span class="tag ok">当前</span>'
        : `<button class="btn xs${x.active ? " p" : ""}" onclick="hfActorPersonaUse(${jsStr(x.id)})">${x.active ? "回到当前" : "查看"}</button>`}</div></div>`).join("") || '<div class="pd">读不到人格</div>'}
    <div class="noteblock" style="margin-top:12px;font-size:10.5px;line-height:1.7">人格本体存在 AstrBot 原生人格库：下面的编辑框直接写回原生；提案路径另留 diff，批准后可回滚。</div>
    <div class="prow" style="padding:9px 0 0"><div><div class="pn">记忆人格隔离</div><div class="pd">开启后记忆与学习按人格分开</div></div>
      <div class="pc">${p.isolated ? '<span class="tag ok">已开启</span>' : '<span class="tag warn">关闭</span>'}</div></div>
  </div>`;
  const editor = `<div class="card cp">
    <div class="row bt"><div class="row" style="gap:8px"><h3>${esc(cur.id || "—")}</h3>${cur.id === active ? '<span class="tag ok">当前使用</span>' : ""}</div>
      <span class="mono mut" style="font-size:10.5px">hash ${esc(cur.hash || "—")}</span></div>
    <textarea class="inp" id="actorPersonaPrompt" style="min-height:300px;line-height:1.8;font-size:12.5px">${esc(cur.prompt || "")}</textarea>
    <div class="row bt" style="margin-top:10px"><span class="mut" style="font-size:11px">${fmtNum(cur.chars)} 字 · 保存写回原生人格库并记审计</span>
      <button class="btn xs p" onclick="hfActorPersonaSave(this)">保存到 AstrBot</button></div>
  </div>`;
  const props = (rev.items || []).filter((x) => !x.persona_id || x.persona_id === cur.id);
  const revisions = props.length ? props.map((x) => `<div class="card cp">
    <div class="row bt"><div class="row" style="gap:7px">${stateTag(x.status)}<b style="font-size:12.5px">提案 #${Number(x.id) || 0}</b></div>
      <span class="mut" style="font-size:10.5px">${esc(timeAgo(x.ts))} · +${fmtNum(x.added)} 字</span></div>
    <div class="pd" style="margin-top:5px">${esc(x.rationale || "")}</div>
    ${x.base_stale ? `<div class="noteblock" style="margin-top:8px">这条提案的基线已与当前人格不同：左边显示的是现在的真身。</div>` : ""}
    ${sbsHtml(x.sbs, { max: 22 })}
    <div class="row" style="gap:6px;margin-top:9px">
      ${x.status === "pending" ? `<button class="btn xs p" onclick="hfActorRevisionDecide(${Number(x.id) || 0},true,this)">通过并写入</button>
        <button class="btn xs" onclick="hfActorRevisionDecide(${Number(x.id) || 0},false,this)">驳回</button>` : ""}
      <button class="btn xs" onclick="hfActorProv('persona',${Number(x.id) || 0})">看完整对比</button>
      ${x.status === "approved" ? `<button class="btn xs" onclick="hfActorRollback(${Number(x.id) || 0},this)">回滚</button>` : ""}
    </div>
  </div>`).join("") : card(`<div class="pd">还没有人格提案。自动进化出提案后会出现在这里。</div>`);
  return `<div style="display:grid;grid-template-columns:280px minmax(0,1fr);gap:12px;align-items:start">
    ${left}
    <div class="actor-wrap">${editor}${secTitle("修订时间线")}${revisions}</div>
  </div>`;
}

/* ---------------- 注入 ---------------- */
async function injectView(ov) {
  const res = await apiPostSafe("actor/preview", {
    persona_id: (ov.persona || {}).id || "", register: A.reg, message: A.msg,
  });
  const pv = (res.ok && res.data) || { system_block: "", user_block: "", chars: 0, token_est: 0, dropped: [], __err: res.error };
  const dropped = (pv.dropped || []).length
    ? pv.dropped.map((d) => `<div class="prow" style="padding:7px 0"><div><div class="pn">${esc(d.term || "")}</div><div class="pd">${esc(d.reason || "")}</div></div></div>`).join("")
    : `<div class="pd">这一轮没有丢弃项。</div>`;
  return `<div style="display:grid;grid-template-columns:300px minmax(0,1fr);gap:12px;align-items:start">
    <div class="card cp">
      <div class="sec-t" style="margin-top:0">编译条件</div>
      <div class="pd">人格</div>
      <select class="inp" id="actorInjP" style="width:100%">${((ov.persona || {}).id ? [ov.persona.id] : []).map((x) => `<option>${esc(x)}</option>`).join("") || "<option>—</option>"}</select>
      <div class="pd" style="margin:10px 0 4px">场景</div>
      <div class="seg" id="actorReg"><button class="sg${A.reg === "private" ? " on" : ""}" onclick="hfActorReg('private')">私聊</button><button class="sg${A.reg === "group" ? " on" : ""}" onclick="hfActorReg('group')">群聊</button></div>
      <div class="pd" style="margin:10px 0 4px">测试消息</div>
      <textarea class="inp" id="actorMsg" style="min-height:70px">${esc(A.msg)}</textarea>
      <button class="btn xs p" style="width:100%;margin-top:12px" onclick="hfActorPreview(this)">编译这一轮</button>
      <div class="mut" style="font-size:10.5px;margin-top:9px;line-height:1.7">只读编译，不写学习库；不会改变命中与冷却。</div>
    </div>
    <div class="actor-wrap">
      ${pv.__err ? card(`<div class="pd">预览失败：${esc(pv.__err)}</div>`) : ""}
      ${pv.note ? `<div class="noteblock">${esc(pv.note)}</div>` : ""}
      <div class="card cp"><div class="row bt"><h3>system 尾部 ·【表达】块</h3><span class="mono mut" style="font-size:10.5px">${fmtNum((pv.system_block || "").length)} 字</span></div>
        <pre class="noteblock" style="white-space:pre-wrap;margin-top:8px">${esc(pv.system_block || "（这一轮没有表达块）")}</pre></div>
      <div class="card cp"><div class="row bt"><h3>user 侧 · 群内用语</h3><span class="mono mut" style="font-size:10.5px">${fmtNum((pv.user_block || "").length)} 字</span></div>
        <pre class="noteblock" style="white-space:pre-wrap;margin-top:8px">${esc(pv.user_block || "（这条消息没有命中词条）")}</pre></div>
      <div class="kpis">
        ${kpiCard("actInjChars", fmtNum(pv.chars || 0), "总字数")}
        ${kpiCard("actInjTok", fmtNum(pv.token_est || 0), "估算 token")}
        ${kpiCard("actInjDrop", fmtNum((pv.dropped || []).length), "丢弃条目")}
      </div>
      ${card(dropped, { title: "丢弃清单", sub: "默认折叠在结果里，不占提示词" })}
    </div>
  </div>`;
}

/* ---------------- 主入口 ---------------- */
export async function renderActor(sub) {
  const tab = SUBS.includes(sub) ? sub : "review";
  const ov = await apiSafe("actor/overview", {}, { ok: false, persona: {}, counts: {}, quota: {}, __err: "" });
  let body = "";
  if (tab === "review") body = await reviewView(ov);
  else if (tab === "voice") body = await voiceView();
  else if (tab === "persona") body = await personaView(ov);
  else body = await injectView(ov);
  return {
    html: `<style>${CSS}</style>${pageHead(
      "人格与表达",
      "学到的说话方式、原话和群内用语，和人格本体放在一页管。每条都能点回原对话；学习只出候选，人格改动必须你批准。",
      `<button class="btn xs" onclick="hfActorLearn(this)">立即学一批</button>`,
      "☺",
    )}${body}`,
  };
}

/* ---------------- 来处抽屉 ---------------- */
function bubbles(msgs) {
  return `<div class="prov-chat">${(msgs || []).map((m) => `<div class="prov-msg ${m.side === "bot" ? "bot" : "user"} ${m.picked ? "picked" : ""}">
      <div class="who">${esc(m.who || (m.side === "bot" ? "助手" : "对方"))}<span>${m.ts ? esc(fmtTime(m.ts)) : ""} ${esc(m.id || "")}</span></div>
      <div class="bub">${esc(m.text || "")}</div>
      ${m.picked ? '<div class="pick">学到的是这段</div>' : ""}
    </div>`).join("")}</div>`;
}

function provChat(res) {
  /* 回复方式：按记忆分段给原对话，每段就是一次完整回复 */
  if (Array.isArray(res.threads) && res.threads.length) {
    return res.threads.map((t) => `<div class="prov-thread">
      <div class="prov-thread-h">记忆 #${Number(t.memory_id) || 0}${t.ts ? ` · ${esc(fmtTime(t.ts))}` : ""}</div>
      ${bubbles(t.msgs || [])}</div>`).join("");
  }
  const msgs = res.messages || [];
  if (!msgs.length) return `<div class="noteblock" style="margin-top:6px">${esc(res.note || "没有可回查的原对话")}</div>`;
  const ctx = res.context || [];
  const ctxHtml = ctx.length
    ? `<details class="prov-ctx"><summary>同会话其它消息 ${fmtNum(ctx.length)} 条（不在这次学习里）</summary>${bubbles(ctx)}</details>`
    : "";
  return `<div class="row" style="justify-content:space-between;margin:6px 0 0">
      <span class="mut" style="font-size:9.5px">${esc(res.conv || "同一会话")}</span></div>
    ${bubbles(msgs)}${ctxHtml}`;
}

function provHtml(res) {
  const steps = (res.steps || []).map((x) => `<div class="prov-step"><b>${x.ts ? esc(fmtTime(x.ts)) : "—"}</b><span>${esc(x.label || "")}</span><p>${esc(x.note || "")}</p></div>`).join("");
  const why = (res.why || []).map((x) => `<li>${esc(x)}</li>`).join("");
  const sbs = (res.sbs || []).length
    ? `<div class="sec-t">左右对比</div>${sbsHtml(res.sbs, { left: res.left_label || "当前人格", right: res.right_label || "更新后" })}`
    : ((res.diff || []).length ? `<div class="sec-t">diff</div><pre class="noteblock" style="white-space:pre-wrap;border-left-color:var(--success);max-height:240px;overflow:auto;font-size:10.5px">${esc(res.diff.join("\n"))}</pre>` : "");
  const acts = [];
  if (["expr", "jargon", "persona"].includes(res.kind) && res.state === "待审") {
    acts.push(`<button class="btn xs p" onclick="hfActorDecide('${esc(res.kind)}',${Number(res.id)},'approve',this)">通过</button>`);
    acts.push(`<button class="btn xs" onclick="hfActorDecide('${esc(res.kind)}',${Number(res.id)},'reject',this)">驳回</button>`);
  }
  acts.push('<button class="btn xs" onclick="closeDrawer()">关闭</button>');
  return `<div class="row" style="gap:7px"><span class="tag">${esc(res.type || "")}</span>${stateTag(res.state)}</div>
    <div class="prov-q">${esc(res.title || "")}</div>
    <div class="mut" style="font-size:10.5px">${esc(res.subtitle || "")}</div>
    ${res.meta ? `<div class="mut" style="font-size:10px;margin-top:2px">${esc(res.meta)}</div>` : ""}
    ${res.note ? `<div class="noteblock" style="margin-top:8px">${esc(res.note)}</div>` : ""}
    <div class="sec-t">怎么来的</div><div class="prov-steps">${steps}</div>
    ${res.kind === "persona" ? "" : `<div class="sec-t">原始对话</div>${provChat(res)}`}
    <div class="sec-t">为什么是它</div><ul class="prov-why">${why}</ul>
    ${sbs}
    <div class="row" style="gap:6px;margin-top:14px;flex-wrap:wrap">${acts.join("")}</div>
    <div class="mut" style="font-size:10px;margin-top:9px;line-height:1.6">原对话按 memory_id / 命中日志取；取不到时写「已清理」，不拿记忆正文冒充原话。</div>`;
}

export async function hfActorProv(kind, id) {
  const res = await apiSafe("actor/provenance", { kind, id }, { ok: false, error: "读取失败" });
  if (!res || res.ok === false) {
    toast("来处读取失败：" + ((res && res.error) || "未知原因"), "err");
    return;
  }
  openDrawer(`来处 · ${res.type || TYPE_CN[kind] || kind}`, provHtml(res));
  const dr = document.getElementById("drawer");
  if (dr) dr.classList.add("wide");
}

/* ---------------- 动作 ---------------- */
export async function hfActorLearn(btn) {
  if (btn) { btn.classList.add("loading"); btn.disabled = true; }
  const res = await apiPostSafe("actor/learn", {});
  if (btn) { btn.classList.remove("loading"); btn.disabled = false; }
  const d = (res.ok && res.data) || {};
  if (res.ok && d.ok) toast(`学完了：${fmtNum(d.saved || 0)} 条原话 / ${fmtNum(d.styles || 0)} 种回复方式`, "ok");
  else toast(`这轮没学到：${d.reason || res.error || "未知原因"}（新记忆 ${fmtNum(d.fresh || 0)} 条）`, "err");
  if (res.ok) refreshCurrent();
}

export async function hfActorDecide(kind, id, action, btn) {
  if (btn) btn.disabled = true;
  const res = await apiPostSafe("actor/review/decide", { kind, id, action });
  if (btn) btn.disabled = false;
  const d = (res.ok && res.data) || {};
  if (res.ok && d.ok !== false) {
    toast(action === "approve" ? (kind === "persona" ? "已写入人格：写前快照、写后回读" : "已通过") : "已驳回", "ok");
    closeDrawer();
    refreshCurrent();
  } else {
    toast("操作失败：" + (d.error || res.error || "未知原因"), "err");
  }
}

export function hfActorFilter(kind) {
  A.filter = kind;
  document.querySelectorAll("#actFilters .chip").forEach((c) => c.classList.toggle("hot", c.dataset.f === kind));
  document.querySelectorAll("#actReviewList .actor-review").forEach((c) => {
    c.style.display = (kind === "all" || c.dataset.kind === kind) ? "" : "none";
  });
}

export function hfActorReg(reg) {
  A.reg = reg === "group" ? "group" : "private";
  refreshCurrent();
}

export async function hfActorPreview(btn) {
  const ta = $("#actorMsg");
  if (ta) A.msg = String(ta.value || "");
  if (btn) btn.disabled = true;
  refreshCurrent();
  setTimeout(() => { if (btn) btn.disabled = false; }, 500);
}

export async function hfActorPersonaUse(pid) {
  A.personaId = String(pid || "");
  toast("查看人格：" + pid + "（保存仍写原生人格库）", "");
  refreshCurrent();
}

export async function hfActorPersonaSave(btn) {
  const prompt = String((($("#actorPersonaPrompt") || {}).value) || "");
  if (!prompt.trim()) { toast("人格不能为空", "err"); return; }
  if (btn) btn.disabled = true;
  const res = await apiPostSafe("actor/persona/save", { persona_id: A.personaId, system_prompt: prompt });
  if (btn) btn.disabled = false;
  toast(res.ok ? "人格已保存" : "保存失败：" + (res.error || ""), res.ok ? "ok" : "err");
  if (res.ok) refreshCurrent();
}

export async function hfActorRevisionDecide(id, approve, btn) {
  if (btn) btn.disabled = true;
  const res = await apiPostSafe("actor/revision/decide", { id, approve: !!approve });
  if (btn) btn.disabled = false;
  const d = (res.ok && res.data) || {};
  toast(res.ok && d.ok !== false ? (approve ? "已写入人格" : "已驳回") : "操作失败：" + (d.error || res.error || ""), res.ok && d.ok !== false ? "ok" : "err");
  if (res.ok && d.ok !== false) refreshCurrent();
}

export async function hfActorRollback(id, btn) {
  if (btn) btn.disabled = true;
  const res = await apiPostSafe("actor/revision/rollback", { id });
  if (btn) btn.disabled = false;
  toast(res.ok ? "已回滚" : "回滚失败：" + (res.error || ""), res.ok ? "ok" : "err");
  if (res.ok) refreshCurrent();
}

export async function hfActorRename(id, old) {
  const name = await askText("这种回复方式叫什么？", old || "");
  if (!name || name === old) return;
  const res = await apiPostSafe("actor/voice/save", { kind: "style", id, fields: { name } });
  toast(res.ok ? "改好了" : "操作失败：" + (res.error || ""), res.ok ? "ok" : "err");
  if (res.ok) refreshCurrent();
}

export async function hfActorEditQuote(id, old) {
  const quote = await askText("改这句原话", old || "");
  if (!quote || quote === old) return;
  const res = await apiPostSafe("actor/voice/save", { kind: "quote", id, fields: { quote } });
  toast(res.ok ? "改好了" : "操作失败：" + (res.error || ""), res.ok ? "ok" : "err");
  if (res.ok) refreshCurrent();
}

export async function hfActorEditTerm(id, meaning, usage) {
  const val = await askText("改释义", meaning || usage || "");
  if (!val) return;
  const res = await apiPostSafe("actor/voice/save", { kind: "term", id, fields: { meaning: val } });
  toast(res.ok ? "改好了" : "操作失败：" + (res.error || ""), res.ok ? "ok" : "err");
  if (res.ok) refreshCurrent();
}

export async function hfActorStyleStatus(id, status) {
  const res = await apiPostSafe("actor/voice/status", { kind: "style", id, status });
  toast(res.ok ? (status === "paused" ? "已停用" : "已启用") : "操作失败：" + (res.error || ""), res.ok ? "ok" : "err");
  if (res.ok) refreshCurrent();
}

export async function hfActorQuoteStatus(id, status) {
  const res = await apiPostSafe("actor/voice/status", { kind: "quote", id, status });
  toast(res.ok ? (status === "active" ? "已恢复" : "已停用") : "操作失败：" + (res.error || ""), res.ok ? "ok" : "err");
  if (res.ok) refreshCurrent();
}

/* ---------------- 沙箱里的输入框 ---------------- */
function askText(title, value) {
  return new Promise((resolve) => {
    const mask = document.createElement("div");
    mask.className = "ask-mask";
    mask.innerHTML = `<div class="ask-box"><div class="t"></div>
      <textarea></textarea>
      <div class="b"><button class="btn xs" data-k="c">取消</button><button class="btn xs on" data-k="o">确定</button></div></div>`;
    mask.querySelector(".t").textContent = title;
    const ta = mask.querySelector("textarea");
    ta.value = value || "";
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

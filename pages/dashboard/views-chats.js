/**
 * StarHeart 面板 · 消息浏览视图（P2-2：像 QQ 一样看所有会话与聊天记录）
 *
 * 左栏 = 会话清单（按最近说话排序，含群/私标记、消息数、bot 占比、末条预览）；
 * 右栏 = 该会话的消息流（双方气泡 + 时间），带关键词过滤与翻页。
 * 数据源 hf_messages 是双侧的（2026-10-24 起 bot 侧也落档），不会再是半边对话。
 */
import {
  apiSafe, esc, fmtTime, timeAgo, pageHead, emptyBlock, openDrawer,
} from "./store.js?v=1.0.6";

const CHATS = { sessions: [], picked: "", q: "", page: 0, total: 0, items: [], loading: false };
const PAGE_SIZE = 200;

function sessLabel(sid) {
  const t = String(sid || "");
  const m = t.match(/^(.*?):([A-Za-z]+Message):(.+)$/);
  if (!m) return t || "—";
  const kind = /group/i.test(m[2]) ? "群聊" : /friend|private|direct/i.test(m[2]) ? "私聊" : m[2];
  return kind + " · " + m[3];
}
function isGroup(sid) { return /GroupMessage/i.test(String(sid || "")); }
function sessIcon(sid) {
  return isGroup(sid)
    ? '<span class="hf-ch-ico g" title="群聊">群</span>'
    : '<span class="hf-ch-ico p" title="私聊">私</span>';
}

async function loadSessions() {
  const res = await apiSafe("messages/sessions", { limit: 100 }, null);
  CHATS.sessions = (res && res.items) || [];
}

async function loadMessages() {
  if (!CHATS.picked) return;
  CHATS.loading = true;
  const res = await apiSafe("messages", {
    session_id: CHATS.picked, q: CHATS.q, limit: PAGE_SIZE, offset: CHATS.page * PAGE_SIZE,
  }, null);
  CHATS.loading = false;
  CHATS.items = (res && res.items) || [];
  CHATS.total = (res && res.total) || 0;
}

function listHtml() {
  const rows = CHATS.sessions.map((s) => {
    const on = s.session_id === CHATS.picked;
    const botPct = s.msgs ? Math.round((s.bot_msgs / s.msgs) * 100) : 0;
    return `<button class="hf-ch-row${on ? " on" : ""}" data-sess="${esc(s.session_id)}">
      ${sessIcon(s.session_id)}
      <span class="hf-ch-main">
        <span class="hf-ch-name">${esc(s.peer_name || sessLabel(s.session_id))}</span>
        <span class="hf-ch-prev">${s.last_from_bot ? "我：" : ""}${esc(s.last_preview || "（无文本）")}</span>
      </span>
      <span class="hf-ch-side">
        <span class="hf-ch-ago">${s.last_ts ? esc(timeAgo(s.last_ts)) : "—"}</span>
        <span class="hf-ch-n" title="共 ${s.msgs} 条（我说 ${s.bot_msgs} 条 / ${botPct}%）">${s.msgs} 条</span>
      </span>
    </button>`;
  }).join("");
  return `<div class="hf-ch-list">${rows || emptyBlock("还没有任何会话记录", "bot 收到/发出的消息会自动出现在这里")}</div>`;
}

function bubble(m) {
  const mine = m.is_bot === 1;
  const who = mine ? "我" : (m.sender_name || m.sender_id || "?");
  const t = m.ts ? fmtTime(m.ts) : "";
  // 已撤回：灰化 + 删除线 + 徽标（内容仍可见——这是 bot 视角的档案，不是藏）
  const recalled = Number(m.recalled || 0) === 1;
  const tag = recalled ? '<span class="hf-msg-recall" title="这条消息已被发送者撤回">已撤回</span>' : "";
  // 图片：后端 imgs 是数组（缩略 data-url / 外链），点开放大
  const imgs = Array.isArray(m.imgs) ? m.imgs.filter((x) => x) : [];
  const imgHtml = imgs.length
    ? `<div class="hf-msg-imgs">${imgs.map((u, i) =>
        `<img class="hf-msg-img" src="${esc(u)}" loading="lazy" data-i="${i}" alt="图${i + 1}">`).join("")}</div>`
    : "";
  const text = m.content ? `<div class="hf-msg-body${recalled ? " rec" : ""}">${esc(m.content)}</div>` : "";
  if (!imgHtml && !text) {
    return `<div class="hf-msg${mine ? " me" : ""}${recalled ? " recd" : ""}">
      <div class="hf-msg-head"><b>${esc(who)}</b>${tag}<span class="mut">${esc(t)}</span></div>
      <div class="hf-msg-body mut">（无文本）</div>
    </div>`;
  }
  return `<div class="hf-msg${mine ? " me" : ""}${recalled ? " recd" : ""}">
    <div class="hf-msg-head"><b>${esc(who)}</b>${tag}<span class="mut">${esc(t)}</span></div>
    ${text}${imgHtml}
  </div>`;
}

function streamHtml() {
  if (!CHATS.picked) {
    return emptyBlock("选一个会话", "左栏点一个会话，这里显示完整聊天记录（双方）");
  }
  const pages = Math.max(1, Math.ceil(CHATS.total / PAGE_SIZE));
  const nav = CHATS.total > PAGE_SIZE
    ? `<div class="row" style="gap:8px;align-items:center;padding:8px 0">
        <button class="btn xs ch-prev" ${CHATS.page <= 0 ? "disabled" : ""}>← 更早</button>
        <span class="mut">${CHATS.page + 1} / ${pages} 页 · ${CHATS.total} 条</span>
        <button class="btn xs ch-next" ${CHATS.page + 1 >= pages ? "disabled" : ""}>更晚 →</button>
       </div>` : "";
  const items = CHATS.items.length
    ? CHATS.items.map(bubble).join("")
    : emptyBlock("这个会话还没有消息", "或关键词没命中");
  return `${nav}<div class="hf-msg-flow">${items}</div>${nav}`;
}

function html() {
  const sess = CHATS.sessions.find((s) => s.session_id === CHATS.picked);
  return `
    <div class="hf-chats">
      <div class="hf-ch-left">
        <div class="hf-ch-search">
          <input id="hfChQ" placeholder="过滤当前会话的消息…" value="${esc(CHATS.q)}">
          <button class="btn xs ch-search">搜</button>
        </div>
        ${listHtml()}
      </div>
      <div class="hf-ch-right">
        ${sess ? `<div class="hf-ch-head">
          ${sessIcon(sess.session_id)}
          <b>${esc(sessLabel(sess.session_id))}${sess.peer_name ? " · " + esc(sess.peer_name) : ""}</b>
          <span class="mut">${sess.msgs} 条 · 我说 ${sess.bot_msgs} 条 · 最近 ${esc(timeAgo(sess.last_ts))}</span>
        </div>` : ""}
        ${streamHtml()}
      </div>
    </div>`;
}

export async function renderChats() {
  await loadSessions();
  if (!CHATS.picked && CHATS.sessions.length) CHATS.picked = CHATS.sessions[0].session_id;
  if (CHATS.picked) await loadMessages();
  return {
    html: `${pageHead("消息浏览", "所有会话的完整聊天记录（双方）——像 QQ 一样按最近聊天排序 · "
      + CHATS.sessions.length + " 个会话")}${html()}`,
    mount: bindChats,
  };
}

/** 事件绑定（沙箱 iframe 里 onclick 内联不可靠，统一 addEventListener） */
function bindChats(host) {
  const rerender = () => {
    const box = host.querySelector(".hf-chats");
    if (box) box.outerHTML = html();
    bindChats(host);
    const flow = host.querySelector(".hf-msg-flow");
    if (flow) flow.scrollTop = flow.scrollHeight;
  };
  host.querySelectorAll(".hf-ch-row").forEach((b) => {
    b.addEventListener("click", async () => {
      CHATS.picked = b.dataset.sess || "";
      CHATS.page = 0; CHATS.q = "";
      await loadMessages();
      rerender();
    });
  });
  const input = host.querySelector("#hfChQ");
  const doSearch = async () => {
    CHATS.q = (input && input.value) || "";
    CHATS.page = 0;
    await loadMessages();
    rerender();
    const q2 = host.querySelector("#hfChQ");
    if (q2) { q2.value = CHATS.q; q2.focus(); }
  };
  if (input) {
    input.addEventListener("keydown", (e) => { if (e.key === "Enter") doSearch(); });
  }
  const sb = host.querySelector(".ch-search");
  if (sb) sb.addEventListener("click", doSearch);
  const pv = host.querySelector(".ch-prev");
  if (pv) pv.addEventListener("click", async () => {
    CHATS.page = Math.max(0, CHATS.page - 1);
    await loadMessages(); rerender();
  });
  const nx = host.querySelector(".ch-next");
  if (nx) nx.addEventListener("click", async () => {
    CHATS.page += 1;
    await loadMessages(); rerender();
  });
  const flow = host.querySelector(".hf-msg-flow");
  if (flow) flow.scrollTop = flow.scrollHeight;
  // 图片点开放大（同一消息里的多张可在抽屉里连看）
  host.querySelectorAll(".hf-msg-img").forEach((img) => {
    img.addEventListener("click", () => {
      const box = img.closest(".hf-msg-imgs");
      const urls = box ? Array.from(box.querySelectorAll(".hf-msg-img")).map((x) => x.src) : [img.src];
      const cur = Number(img.dataset.i || 0);
      openDrawer("图片", `<div class="hf-img-view">${
        urls.map((u, i) => `<img src="${esc(u)}" class="${i === cur ? "" : "hid"}" data-vi="${i}" alt="图${i + 1}">`).join("")
      }</div>${
        urls.length > 1
          ? `<div class="row" style="gap:8px;justify-content:center;padding:10px 0">
              <button class="btn xs ch-img-prev">← 上一张</button>
              <span class="mut ch-img-n">${cur + 1} / ${urls.length}</span>
              <button class="btn xs ch-img-next">下一张 →</button>
            </div>`
          : ""
      }`);
      if (urls.length > 1) {
        let idx = cur;
        const paint = () => {
          host.querySelectorAll && document.querySelectorAll("#drawer .hf-img-view img").forEach((im) => {
            im.classList.toggle("hid", Number(im.dataset.vi || 0) !== idx);
          });
          const n = document.querySelector(".ch-img-n");
          if (n) n.textContent = `${idx + 1} / ${urls.length}`;
        };
        const pv = document.querySelector(".ch-img-prev");
        const nx = document.querySelector(".ch-img-next");
        if (pv) pv.onclick = () => { idx = (idx - 1 + urls.length) % urls.length; paint(); };
        if (nx) nx.onclick = () => { idx = (idx + 1) % urls.length; paint(); };
      }
    });
  });
}

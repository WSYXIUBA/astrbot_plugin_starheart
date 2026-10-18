/**
 * StarHeart 面板 · 配置域渲染器（门控 / 记忆 / 表达 / 好感 / 撤回 / 主动 / 输出 / 人格 / 主开关）
 *
 * 这些是**写进数据库的真配置**（CONFIG_DOMAINS 白名单），不是设计稿上的假开关：
 * 保存时只提交本页出现过的键，避免误改别的域。
 */

import * as C from "./charts.js?v=1.0.2";
import {
  $, $$, api, apiSafe, apiPostSafe, esc, fmtNum, fmtTime, toast, confirmModal, pageHead, card, mount, emptyBlock,
  skelBlock, refreshCurrent, normList, jsStr, configTouchedAt, noteConfigTouched, recentFirstKeys, takeConfigDirty, nativeWriteError,
} from "./store.js?v=1.0.2";

/** key -> [标签, 说明, 类型, min, max, step, 选项] */
export const CONFIG_META = {
  master_switch: ["总开关", "关掉后StarHeart整体不介入（适合临时排查）", "bool"],
  session_context_enabled: ["会话上下文", "把同会话最近的消息一起交给模型", "bool"],

  // 平台边界：本插件只服务 QQ（OneBot/aiocqhttp），别的平台的会话不进库
  track_platforms: ["允许记录的平台", "留空=自动只认 OneBot(QQ) 实例；也可手写平台实例名", "text"],
  person_exclude_platforms: ["永不记录的平台", "webchat/webui 已写死拦截，这里可再加", "text"],
  // 记忆域（views-memory.js 的「记忆设置」页渲染这些键，保存走 hfSaveDomain('memory')）——
  // 以前这里没有 memory_* 键，导致那一页的「保存」被 CONFIG_META 过滤成空 patch，10 个参数一个也存不了。
  memory_inject_enabled: ["记忆注入", "把召回到的记忆拼进 prompt", "bool"],
  memory_inject_top_k: ["注入条数", "每轮最多注入几条记忆", "num", 1, 20, 1],
  memory_inject_budget_tokens: ["注入预算", "记忆部分的 token 上限（超了会截断）", "num", 100, 6000, 100],
  memory_capture_enabled: ["对话自动总结", "聊完自动蒸馏出「关于用户的记忆」", "bool"],
  memory_capture_turns: ["触发轮数", "攒够几轮对话触发一次总结", "num", 1, 60, 1],
  memory_capture_min_interval: ["最小间隔(秒)", "两次总结之间至少隔多久（0 = 不设限）", "num", 0, 3600, 30],
  memory_capture_max_per_run: ["单次上限", "一次总结最多产出几条记忆", "num", 1, 20, 1],
  cross_session_enabled: ["跨会话动作留痕", "bot 借工具（自带或第三方工具类插件）在别的会话里发了消息时，留台账并在相关会话注入知会（被问「谁让你发的」时答得上来）", "bool"],
  cross_session_ttl_min: ["知会有效期(分钟)", "超过这个时间的动作不再注入；长期回忆走记忆库", "num", 10, 1440, 10],
  cross_session_memory: ["跨会话动作写入记忆", "关掉则只在有效期内注入知会，不写记忆条目", "bool"],
  memory_decay_half_life_days: ["衰减半衰期(天)", "记忆强度按指数衰减，这是半衰期", "num", 1, 365, 1],
  memory_evict_below: ["淘汰阈值", "强度低于此值的记忆会被标记 stale", "num", 0.01, 0.5, 0.01],
  memory_summary_prompt: ["总结自定义提示词", "空=内置；支持 {transcript} 占位", "text"],
  memory_persona_isolated: ["记忆人格隔离", "记忆按人格分开存放", "bool"],
  gate_enabled: ["门控开关", "决定机器人「该不该开口」", "bool"],
  gate_shadow: ["影子模式", "只记录评分不真的拦，用来观察门控准不准", "bool"],
  gate_threshold: ["触发阈值", "总分 ≥ 阈值才开口（分数永不进 prompt）；超过 100 会永久沉默，保存时被拦下", "num", 1, 100, 1],
  gate_window_seconds: ["连发窗口(秒)", "窗口内的多条消息合并成一次评估", "num", 3, 300, 1],
  gate_backoff_base: ["退避起步(秒)", "被拦下后最小静默间隔；低于 1 秒等于退避失效（保存时被拦下）", "num", 1, 60, 1],
  gate_backoff_max: ["退避上限(秒)", "被拦下后最大静默间隔；必须 ≥ 退避起步（保存时由后端校验）", "num", 15, 600, 5],

  expression_enabled: ["语言学习", "从真实对话里学「情景 → 表达方式」", "bool"],
  expression_min_uses: ["已验证门限(次)", "习惯至少生效过 N 次才算已验证；没达标的只在名额没用满时补位（避免冷启动死锁）", "num", 0, 50, 1],
  expression_auto_min_corpus: ["自动提取门槛(条)", "未学语料攒够 N 条就自动学一次；0 = 只留手动按钮", "num", 0, 200, 1],
  output_expr_max_inject: ["注入表达条数", "单轮最多注入几条表达习惯（0 = 不注入；随语言学习链路生效）", "num", 0, 10, 1],
  favour_enabled: ["好感结算", "按对话表现结算关系分数与区间", "bool"],
  favour_settle_mode: ["结算方式", "tag=由模型尾行自报结算（目前唯一实现）", "select", 0, 0, 0, ["tag"]],
  favour_daily_cap: ["好感日上限", "当天正增量累计到这个值就封顶（0=不限）", "num", 0, 100, 1],
  favour_daily_half_life_days: ["每日半衰期(天)", "daily 按这个天数半衰；0=关闭衰减（lifetime 永不衰减）", "num", 0, 60, 0.5],
  favour_tag_required: ["需要标签才结算", "开=只有模型给出好感标签才结算；关=无标签也按 +1（仍受日上限约束）", "bool"],
  recall_handling: ["撤回处理", "对方撤回消息时怎么处理相关记忆", "select", 0, 0, 0, ["log_only", "weaken", "delete"]],
  recall_weaken_factor: ["降权系数", "撤回命中记忆后强度乘这个系数", "num", 0.05, 1, 0.05],
  recall_keep_days: ["流水保留(天)", "撤回事件流水保留多久", "num", 1, 365, 1],
  recall_withdraw_reply: ["撤回连回复一起撤", "撤回时把该会话最近落库的 bot 语料一并撤掉并丢弃待总结轮次（平台删消息需要回复 id，AstrBot 未提供，故只做本地撤销）", "bool"],
  recall_react_enabled: ["撤回反应（当场走主动通道）", "用户撤回一条消息后，当场让模型自己决定说什么（装没看见/追问/调侃都行）；走 AstrBot 完整提示词、不看主动名单、不受免打扰限制，用户看到的是一条普通消息", "bool"],
  recall_react_group: ["撤回反应·群聊范围", "related=只对与 bot 有互动的消息（前/后一条是 bot 发言）/ all=群里任何人都触发 / off=群聊不触发", "select", 0, 0, 0, ["related", "all", "off"]],
  recall_react_images: ["撤回反应·带上图片", "被撤回的图片一并交给模型看（原生多模态，最多 2 张）", "bool"],
  recall_react_extra: ["撤回反应·附加指示", "追加在撤回通知后面的自定义指示（留空=不加）", "text"],
  proactive_enabled: ["主动消息开关", "机器人能不能主动开口找人聊天", "bool"],
  proactive_shadow: ["影子模式", "只挑选候选不真发，先看它想说什么", "bool"],
  proactive_idle_min: ["静默多久(分钟)", "会话静默超过这么久才考虑主动（0=不用等）", "num", 0, 1440, 10],
  proactive_daily_cap: ["每日上限", "每天最多主动开口几次（当天随机排布时间点；0=不主动）", "num", 0, 50, 1],
  proactive_min_interval_min: ["最小间隔(分钟)", "两次主动之间至少隔多久", "num", 1, 1440, 1],
  proactive_quiet_start: ["静默开始", "HH:MM，这段时间内不主动", "text"],
  proactive_quiet_end: ["静默结束", "HH:MM", "text"],
  proactive_min_zone: ["最低关系档", "留空 = 不设门槛；也可以选一个最低档", "select", 0, 0, 0, ["", "HOSTILE", "COLD", "NEUTRAL", "FAMILIAR", "CLOSE", "DEVOTED"]],
  proactive_last_only: ["只发最后一个会话", "一次扫描最多发一条，避免到处打招呼", "bool"],
  proactive_style: ["开口风格", "写进 prompt 的自然语言，描述主动消息该怎么说", "text"],
  proactive_auto_wl: ["自动白名单", "把人自动加进白名单的策略", "select", 0, 0, 0, ["off", "close", "familiar"]],
  proactive_mode_group: ["群聊主动模式", "white=只对名单内主动；black=名单内不主动；off=不主动", "select", 0, 0, 0, ["black", "white", "off"]],
  proactive_mode_private: ["私聊主动模式", "white=只对名单内主动；black=名单内不主动；off=不主动", "select", 0, 0, 0, ["black", "white", "off"]],
  proactive_wl_private: ["私聊白名单", "逗号分隔的 user_id", "list"],
  proactive_bl_private: ["私聊黑名单", "逗号分隔的 user_id", "list"],
  proactive_wl_group: ["群聊白名单", "逗号分隔的群号", "list"],
  proactive_bl_group: ["群聊黑名单", "逗号分隔的群号", "list"],
  proactive_use_persona: ["主动开口吃 AstrBot 提示词", "开启=主动开口复用 AstrBot 那份完整系统提示词（人格+技能+安全）；关掉会让主动消息吃不到人格，不建议关", "bool"],
  proactive_context_memories: ["带几条记忆", "主动开口时参考几条记忆", "num", 0, 20, 1],
  proactive_instruction: ["主动消息·第2层（本轮情境）", "主动开口的系统级任务指令；留空=用插件内置；支持 {zone}/{tone}/{idle} 占位符（本页底部展开编辑）", "textarea"],
  proactive_prompt_max_age_min: ["提示词缓存时效(分钟)", "主动开口复用 AstrBot 完整提示词的时效；超时降级为只吃人格（主动流水会标注）", "num", 30, 2880, 30],
  proactive_writeback: ["主动消息写回对话历史", "用原生 content parts 写回，bot 能记得自己说过什么；默认开", "bool"],
  proactive_note_enabled: ["主动消息知会块", "写回失败/空历史时的兜底：下一轮注入「你最近主动发过」", "bool"],
  proactive_note_hours: ["知会回看(小时)", "主动消息知会回看窗口", "num", 1, 168, 1],
  proactive_note_max: ["知会最多几条", "主动消息知会最多带几条", "num", 1, 10, 1],

  output_split_messages: ["分段发送", "长回复按语义边界拆成多条发（短句不拆）", "bool"],
  output_typing_delay: ["打字延迟", "补发的后续条目按「每字秒数 × 字数」延迟（首条立即发）", "bool"],
  output_typing_per_char: ["每字秒数", "0.3 ≈ 一字 0.3 秒；低于 0.2 等于关掉打字节奏（保存时被拦下）", "num", 0.2, 2, 0.05],
  output_typing_max: ["单条延迟上限(秒)", "打字时长封顶，别等太久；低于 3 秒等于关掉打字延迟（保存时被拦下）", "num", 3, 30, 0.5],
  output_typo_rate: ["错字率", "错字机制的目标比例（档位见「核心开关」的错字模式）", "num", 0, 0.03, 0.005],
  // S5 黑话（R4 登记先行：min/max 与后端 PARAM_GUARDS 保持一致；块② 的阈值行与试解读配额读这些键）
  jargon_enabled: ["黑话理解", "听懂群友的特殊用语/梗（只做理解，不进回复库）", "bool"],
  jargon_name_filter: ["黑话人名过滤", "候选挖掘时滤掉已知昵称/群名（洞②：防止把某人昵称学成黑话）", "bool"],
  jargon_candidate_min_sessions: ["黑话候选·最少会话数", "机械候选门槛：一个片段要跨几个会话才送推断（拍-16 默认 2）", "num", 1, 50, 1],
  jargon_candidate_min_hits: ["黑话候选·最少出现次数", "机械候选门槛：一个片段要出现几次才送推断（拍-16 默认 4）", "num", 1, 500, 1],
  jargon_infer_daily_batches: ["黑话推断·每日批数", "含义推断每天最多跑几批（默认 1；0 = 关自动推断，候选仍照常记账）", "num", 0, 20, 1],
  jargon_try_daily_quota: ["试解读·每日配额", "「试解读」每天最多几次（默认 20；0 = 不限；单次 1 条短请求、≤200 tokens）", "num", 0, 500, 1],
  persona_active_id: ["当前人格 id", "留空=跟随 AstrBot 原生当前人格", "text"],
};

/**
 * 后端 config/domains 下发的字段兜底表（`fields`）。
 * 静态 CONFIG_META 只负责好看的名字/说明；某个键漏写在这里时，用后端生成的 meta 渲染，
 * 不再整行消失、也不会因为过滤而保存不进去。
 */
let SERVER_META = {};

function metaFor(key, values) {
  const m = CONFIG_META[key] || SERVER_META[key];
  if (m) return m;
  const v = values ? values[key] : undefined;
  const t = Array.isArray(v) ? "list" : typeof v === "boolean" ? "bool" : typeof v === "number" ? "num" : "text";
  return [key, "（后端自检自动补登记；说明来自 _conf_schema.json）", t];
}

const DOMAIN_META = {
  gate: { title: "门控参数", icon: "◉", desc: "机器人「该不该开口」的判定：总分 = 各因子加权和，超过阈值才回复" },
  memory: { title: "记忆设置", icon: "◍", desc: "记忆注入 / 抓取 / 衰减 / 整理" },
  expression: { title: "学习参数", icon: "✎", desc: "表达习惯的学习开关" },
  favour: { title: "结算参数", icon: "♥", desc: "好感分怎么结算、按什么模式打标" },
  recall: { title: "撤回处理参数", icon: "↩", desc: "对方撤回消息时，命中的记忆怎么处理" },
  proactive: { title: "主动消息", icon: "☎", desc: "它想找你聊天时的节奏与口味（都是偏好，可随时改）" },
  output: { title: "输出拟人参数", icon: "✎", desc: "回复的拟人化处理" },
  persona: { title: "人格", icon: "☺", desc: "当前人格 id（人格本体在 AstrBot 原生人格库里改，本插件只用不改）" },
  master: { title: "总开关", icon: "⏻", desc: "StarHeart整体的开关与会话上下文" },
};

/**
 * 参数护栏与配置快照（设置 · 模块与参数页顶两张卡）
 *
 * 护栏的真源是后端：config/domains 会带下来 PARAM_GUARDS（15 键界值 + 「一次改坏」硬拦）。
 * 本文件不再自己抄一份范围 —— 只把后端下发的界值画出来、把被后端拒绝的那一批标出来。
 * 快照写进后端表 hf_config_snapshot：保存任何参数时都会自动存一份，回滚就是把它写回。
 */
let GUARDS = { keys: {}, relations: [], hard_count: 0 };
/** 上一次保存被后端拦下的那一批（护栏卡如实显示；没有就是没有） */
let LAST_REJECT = null;

/** 用后端下发的界值，把这一批里越界的键挑出来（最终裁决仍在后端） */
function guardCheckPatch(patch) {
  const bad = [];
  Object.keys(patch || {}).forEach((k) => {
    const g = GUARDS.keys[k];
    if (!g) return;
    const v = patch[k];
    if (g.kind === "time") {
      if (!/^\d{1,2}:\d{2}$/.test(String(v == null ? "" : v).trim())) {
        bad.push({ key: k, reason: `${g.label || k} 只接受 HH:MM 格式` });
      }
      return;
    }
    const n = Number(v);
    if (!isFinite(n)) {
      bad.push({ key: k, reason: `${g.label || k} 需要一个数字` });
      return;
    }
    if ((g.min != null && n < Number(g.min)) || (g.max != null && n > Number(g.max))) {
      bad.push({ key: k, reason: `${g.label || k} 的允许范围是 ${g.min}–${g.max}${g.why ? "；" + g.why : ""}` });
    }
  });
  return bad;
}

function guardRangeText(g) {
  return g && g.kind === "time" ? "HH:MM" : `${g.min}–${g.max}`;
}

/** 护栏卡：后端下发的界值 + 上一次被拦下的那一批（真数据；没拦过就写没拦过） */
function guardCard() {
  const keys = Object.keys(GUARDS.keys || {});
  const rows = keys
    .map((k) => {
      const g = GUARDS.keys[k];
      const bad = LAST_REJECT && LAST_REJECT.keys.indexOf(k) >= 0;
      return `<div class="kv" style="${bad ? "color:var(--error,#e05555)" : ""}">${esc(g.label || k)}<span><span class="mono">${esc(guardRangeText(g))}</span>${g.hard ? ' <span class="tag warn">一次改坏</span>' : ""}</span></div>`;
    })
    .join("");
  const rej = LAST_REJECT
    ? `<div class="noteblock" style="border-left-color:var(--error,#e05555);margin-top:10px"><b>已拦下</b>（后端一个字都没写入）：${esc(
        LAST_REJECT.keys.join("、") || "未指名键"
      )}<br><span class="mut" style="font-size:11px">${esc(LAST_REJECT.msg)}</span></div>`
    : `<div class="mut" style="font-size:11px;margin-top:9px">本次会话里还没有保存被拦下 —— 越界的改动会在保存时被后端整批拒绝（一个字都不写）。</div>`;
  return card(
    `<div class="grid g2" style="gap:4px 16px">${rows || '<div class="mut">界值清单没取到（config/domains 读取失败）</div>'}</div>${rej}
     <div class="mut" style="font-size:10.5px;margin-top:9px;line-height:1.7">界值由后端下发，共 ${keys.length} 项${
      Number(GUARDS.hard_count) ? `，其中 <b>${Number(GUARDS.hard_count)}</b> 项标「一次改坏」` : ""
    }；退避上限 ≥ 起步值这类跨键关系由后端校验。</div>`,
    { title: "参数护栏", sub: "越界保存会被拒绝；这里列的是当前生效的界值", cls: "sec" }
  );
}

/** 参数快照卡：存一份 / 列最近几份 / 一键回滚（回滚前会自动存当前值） */
function snapshotCard() {
  return card(
    `<div class="row wrap" style="gap:8px">
       <input class="inp" id="hfSnapName" placeholder="快照名（可留空，会自动带时间）" style="flex:1;min-width:200px">
       <button class="btn p" id="hfSnapSave">存一份快照</button>
       <button class="btn s" id="hfSnapRefresh">刷新</button>
     </div>
     <div class="mut" style="font-size:11px;margin-top:7px;line-height:1.7">改动前会自动存一份（可回滚到那次保存之前）；回滚时也会先自动存一份当前值 —— 滚错了还能滚回来。</div>
     <div id="hfSnapList" style="margin-top:10px">${skelBlock(2)}</div>`,
    { title: "参数快照 / 一键回滚", sub: "整库参数的存档点（后端表 hf_config_snapshot）", cls: "sec" }
  );
}

async function fillSnapshots() {
  const list = document.getElementById("hfSnapList");
  if (!list) return;
  const res = await apiSafe("config/snapshots", {}, { items: [], total: 0, keep: 50 });
  if (res && res.__err) {
    list.innerHTML = `<div class="mut" style="font-size:11.5px">快照列表读取失败：${esc(res.__err)}</div>`;
    return;
  }
  const items = (res.items || []).slice(0, 8);
  if (!items.length) {
    list.innerHTML = `<div class="mut" style="font-size:11.5px">还没有快照 —— 存一份，或改一次参数（保存时会自动存）。</div>`;
    return;
  }
  list.innerHTML = `<div style="overflow-x:auto"><table>
      <thead><tr><th>时间</th><th>名称</th><th>键数</th><th>来源</th><th></th></tr></thead>
      <tbody>${items
        .map((s) => {
          const tag = Number(s.auto) ? ["自动", "gy"] : ["手动", "ok"];
          return `<tr><td class="mono">${esc(fmtTime(s.ts, true))}</td><td>${esc(s.name || "")}</td>
            <td class="mono">${fmtNum(s.n_keys)}</td><td><span class="tag ${tag[1]}">${tag[0]}</span></td>
            <td style="text-align:right"><button class="btn xs" data-hf-rollback="${Number(s.id)}">回滚到这份</button></td></tr>`;
        })
        .join("")}</tbody></table></div>
    <div class="mut" style="font-size:10.5px;margin-top:6px">共 ${fmtNum(res.total || items.length)} 份 · 这里列最近 ${items.length} 份（保留上限 ${fmtNum(res.keep || 50)} 份，超出先清自动快照）</div>`;
  list.querySelectorAll("[data-hf-rollback]").forEach((b) => {
    b.addEventListener("click", () => hfSnapshotRollback(Number(b.getAttribute("data-hf-rollback"))));
  });
}

async function hfSnapshotSave(btn) {
  const inp = $("#hfSnapName");
  const name = inp ? String(inp.value || "").trim() : "";
  if (btn) {
    btn.classList.add("loading");
    btn.disabled = true;
  }
  const res = await apiPostSafe("config/snapshot", { name });
  if (btn) {
    btn.classList.remove("loading");
    btn.disabled = false;
  }
  if (!res.ok) {
    toast("存快照失败：" + res.error, "err");
    return;
  }
  const d = res.data || {};
  toast(`已存快照（${fmtNum(d.n_keys)} 个键）`, "ok");
  if (inp) inp.value = "";
  fillSnapshots();
}

function hfSnapshotRollback(sid) {
  confirmModal(
    "回滚参数",
    `把配置写回快照 #${sid} 里的值；回滚前会先自动存一份当前值。确定继续？`,
    async () => {
      const res = await apiPostSafe("config/rollback", { id: sid });
      if (!res.ok) {
        toast("回滚失败：" + res.error, "err");
        return;
      }
      const d = res.data || {};
      const extra = Array.isArray(d.removed) && d.removed.length ? `，并清除快照后才出现的 ${d.removed.length} 个键` : "";
      toast(`已回滚 ${fmtNum(d.restored)} 项${extra}`, "ok");
      refreshCurrent();
    },
    "回滚"
  );
}

/** 把两张卡插进「设置 · 模块与参数」页顶（每页只插一次；只在参数中心 bare 分区里做） */
function injectParamsCards(host) {
  if (!host || host.querySelector("#hfParamsCards")) return;
  const box = document.createElement("div");
  box.id = "hfParamsCards";
  box.className = "sec";
  box.innerHTML = guardCard() + snapshotCard();
  const first = host.querySelector("[data-pdom]");
  if (first) host.insertBefore(box, first);
  else host.appendChild(box);
  const save = box.querySelector("#hfSnapSave");
  if (save) save.addEventListener("click", () => hfSnapshotSave(save));
  const refresh = box.querySelector("#hfSnapRefresh");
  if (refresh) refresh.addEventListener("click", fillSnapshots);
  fillSnapshots();
}

/** 一门域的参数行；返回 {html, values} */
export function domainRowsHtml(domain, values, keys) {
  const list = Array.isArray(keys) ? keys : [];
  return list
    .map((key) => {
      const meta = metaFor(key, values);
      // textarea 类型（如主动第2层提示词）不进通用行——它由页面底部专门的可展开编辑器渲染
      if (meta[2] === "textarea") return "";
      const [name, desc, type, min, max, step, options] = meta;
      const g = GUARDS.keys[key] || null;
      const gmin = g && g.min != null ? g.min : min;
      const gmax = g && g.max != null ? g.max : max;
      const rejected = !!(LAST_REJECT && LAST_REJECT.keys.indexOf(key) >= 0);
      const v = values[key];
      let control = "";
      if (type === "bool") {
        control = `<label class="sw"><input type="checkbox" data-hf-cfg="${key}" ${v ? "checked" : ""}><span class="tk"></span><span class="kb"></span></label>`;
      } else if (type === "num") {
        control = `<input class="inp" type="number" data-hf-cfg="${key}" value="${esc(v ?? "")}" ${gmin != null ? `min="${gmin}"` : ""} ${gmax != null ? `max="${gmax}"` : ""} ${step ? `step="${step}"` : ""}>`;
      } else if (type === "select") {
        control = `<select class="inp" data-hf-cfg="${key}">${(options || [])
          .map((o) => `<option value="${esc(o)}" ${String(v) === o ? "selected" : ""}>${esc(o)}</option>`)
          .join("")}</select>`;
      } else if (type === "list") {
        control = `<input class="inp" style="min-width:220px" data-hf-cfg="${key}" data-hf-list="1" value="${esc((Array.isArray(v) ? v : []).join(", "))}" placeholder="逗号分隔">`;
      } else {
        control = `<input class="inp" style="min-width:240px" data-hf-cfg="${key}" value="${esc(v ?? "")}">`;
      }
      const range = g ? ` · <span class="mono">允许 ${esc(guardRangeText(g))}</span>` : "";
      const why = rejected
        ? `<div class="pd" style="color:var(--error,#e05555)">已拦下：这次保存没写进去（${esc(
            g && g.why ? g.why : "越界"
          )}）</div>`
        : "";
      return `<div class="prow${configTouchedAt(key) ? " modified" : ""}${rejected ? " guard-bad" : ""}" data-key="${key}"${
        configTouchedAt(key) ? ` title="本次会话里改过并保存成功 · ${esc(fmtTime(configTouchedAt(key)))}"` : ""
      }><div><div class="pn">${esc(name)}${g && g.hard ? ' <span class="tag warn" title="一次改坏：越界会让对应机制静默失效">一次改坏</span>' : ""}</div>
        <div class="pd">${esc(desc)} · <span class="mono">${key}</span>${range}</div>${why}</div>
        <div class="pc">${control}</div></div>`;
    })
    .filter(Boolean)
    .join("");
}

/**
 * 通用域参数页：右侧还塞一张「域相关小图」，让参数页不只是表单。
 * 传 keys 可只渲染某几个键（例如主动域只渲染主动相关键）。
 */
/** 最近一次 config/domains 拿到的「域 → 键白名单」。保存时用它收窄 patch。 */
let DOMAIN_KEYS = {};

export async function renderDomainConf(domain, opts = {}) {
  const dom = await apiSafe("config/domains", {}, { domains: {}, values: {} });
  // config/domains 失败时必须显式报错，不能退化成「全部参数 + 空值」——
  // 那样看起来像「配置被清空了」，而且一保存就会把全量键写回去。
  if (dom && typeof dom === "object" && dom.__err) {
    return {
      html: pageHead(
        "参数读取失败",
        `这一页的参数清单来自后端 <span class="mono">config/domains</span>，现在拿不到`,
        `<button class="btn p" onclick="hfRefresh()">重试</button>`,
        "⚠"
      ) + card(
        `<div class="fnote">⚠ 读取失败：${esc(String(dom.__err))}</div>
         <div class="cs">为避免「看起来像配置被清空」以及误写全量配置，这一页不再渲染空表单。请重试。</div>`,
        { title: "读取失败" }
      ),
    };
  }
  DOMAIN_KEYS = dom.domains && typeof dom.domains === "object" ? dom.domains : {};
  SERVER_META = dom.fields && typeof dom.fields === "object" ? dom.fields : {};
  if (dom.guards && typeof dom.guards === "object" && dom.guards.keys) GUARDS = dom.guards;
  const values = dom.values || {};
  const baseKeys = Array.isArray(dom.domains && dom.domains[domain]) ? dom.domains[domain] : [];
  // 收尾批：可选排除某些键（如主动名单写入 —— 它有专属编辑面，不在这里渲染第二份），
  // 可选「最近改过置顶」（本次会话保存成功的键排最前）。
  const excluded = Array.isArray(opts.exclude) ? opts.exclude : [];
  const keyList = baseKeys.filter((k) => excluded.indexOf(k) < 0);
  const keys = opts.recentFirst ? recentFirstKeys(keyList) : keyList;
  const shown = keys.filter((k) => metaFor(k, values)[2] !== "textarea");
  const meta = DOMAIN_META[domain] || { title: domain, icon: "⚙", desc: "" };
  const extra = opts.extra || "";
  // top：整宽卡片，放在参数网格之前（「一键试发」这类需要显眼的东西放这儿，
  // 塞进右侧 side 列会被参数卡挡得看不着 —— 用户抱怨过「太隐蔽」）
  const top = opts.top || "";
  // bottom：整宽区块，放在参数网格之后（主动「第 2 层提示词」这类要沉底的编辑器放这儿）；
  // 传函数时签名 (values, dom)——dom 里带着 builtin 默认文本（本页要用它当「载入内置默认」的来源）
  const bottom = typeof opts.bottom === "function" ? opts.bottom(values, dom) : (opts.bottom || "");
  // 同一页可能并排多个域（设置·模块与参数）→ 图表容器 id 每域一个，别互相抢
  const chartId = String(opts.chartId || "dcChart");
  const saveBtn = `<button class="btn p" onclick="hfSaveDomain(${jsStr(domain)},this)">保存本域参数</button>`;
  const rowsCard = card(domainRowsHtml(domain, values, shown), {
    // bare：嵌进「设置 · 模块与参数」的域分区时，用卡头代替整页页头（页面不能有一堆 h1）
    title: opts.bare ? `${meta.icon} ${meta.title}` : "参数",
    sub: opts.bare
      ? `${esc(meta.desc)} · 共 ${shown.length} 项 · 由后端 <span class="mono">CONFIG_DOMAINS['${esc(domain)}']</span> 白名单管理，改动写进数据库`
      : `共 ${shown.length} 项 · 改动会写进数据库`,
    actions: opts.bare ? saveBtn + (opts.actions || "") : "",
  });
  const html = opts.bare
    ? `<div class="sec" data-pdom="${esc(domain)}">
  ${top ? `<div class="sec">${top}</div>` : ""}
  <div class="grid g32">
    ${rowsCard}
    <div class="col">${opts.side || ""}<div id="${chartId}"></div>${extra}</div>
  </div>
  ${bottom ? `<div class="sec">${bottom}</div>` : ""}</div>`
    : `
  ${pageHead(
    meta.title,
    `${esc(meta.desc)} · 这些键由后端 <span class="mono">CONFIG_DOMAINS['${esc(domain)}']</span> 白名单管理`,
    `${saveBtn}
     ${opts.actions || ""}`,
    meta.icon
  )}
  ${top ? `<div class="sec">${top}</div>` : ""}
  <div class="grid g32 sec">
    ${rowsCard}
    <div class="col">${opts.side || ""}<div id="${chartId}"></div>${extra}</div>
  </div>
  ${bottom ? `<div class="sec">${bottom}</div>` : ""}`;
  return {
    html,
    mount: (host) => {
      // 参数中心（设置页的 bare 分区）页顶带两张卡：参数护栏 + 参数快照；整页只插一次
      if (opts.bare) injectParamsCards(host);
      mount(host, "#" + chartId, (el) =>
        C.bars(el, {
          items: keys
            .filter((k) => {
              const t = metaFor(k, values)[2];
              return t === "num" || t === "bool";
            })
            .slice(0, 10)
            .map((k) => {
              const m = metaFor(k, values);
              return {
                label: m[0],
                value: typeof values[k] === "number" ? values[k] : values[k] ? 1 : 0,
                color: values[k] ? "var(--primary,#3c96ca)" : "#9aa4b2",
                sub: m[2] === "bool" ? (values[k] ? "已开启" : "已关闭") : "当前值",
              };
            }),
          valueFmt: (v) => (v === 1 || v === 0 ? String(v) : String(v)),
        })
      );
    },
  };
}

/** 保存当前页里的本域参数（只提交本域白名单里的、且本页出现过的 data-hf-cfg） */
export async function hfSaveDomain(domain, btn) {
  const root = (btn && btn.closest("[data-pdom]")) || null;
  let whitelist = Array.isArray(DOMAIN_KEYS[domain]) ? DOMAIN_KEYS[domain] : [];
  if (!whitelist.length && !root) {
    // 冷启动直接进记忆设置页时，DOMAIN_KEYS 还没被任何 renderDomainConf 填过。
    // 这里自愈拉一次后端域清单，不能把「没逛过设置页」变成「整域存不了」。
    const meta = await apiSafe("config/domains", {}, null);
    if (meta && !meta.__err && meta.domains && Array.isArray(meta.domains[domain])) {
      DOMAIN_KEYS = meta.domains;
      SERVER_META = meta.fields || SERVER_META;
      whitelist = meta.domains[domain];
    }
  }
  if (!whitelist.length) {
    // 白名单没拿到时，只从「按钮所在的域子树」推导；绝不退化成整页/全文档。
    if (!root) {
      toast("拿不到本域白名单，请刷新后重试（不会写入别的域）", "err");
      return;
    }
    whitelist = Array.from(root.querySelectorAll("[data-hf-cfg]"))
      .map((el) => (el ? el.getAttribute("data-hf-cfg") : ""))
      .filter((k) => k);
    if (!whitelist.length) {
      toast("这一页没有可保存的参数", "err");
      return;
    }
    console.warn("[starheart] config/domains 不可用，已按按钮所在域推导白名单");

  }
  const allowed = new Set(whitelist);
  const patch = {};
  const bad = [];
  const scope = root || document;
  scope.querySelectorAll("[data-hf-cfg]").forEach((el) => {
    const key = el.getAttribute("data-hf-cfg");
    if (!key) return;
    if (!allowed.has(key)) return;
    if (el.type === "checkbox") {
      patch[key] = el.checked;
    } else if (el.dataset.hfList) {
      patch[key] = normList(el.value);
    } else if (el.type === "number") {
      const raw = String(el.value || "").trim();
      if (!raw || !Number.isFinite(Number(raw))) {
        bad.push(key);
        return;
      }
      patch[key] = Number(raw);
    } else {
      patch[key] = el.value;
    }
  });
  if (bad.length) {
    bad.forEach((k) => {
      const el = scope.querySelector(`[data-hf-cfg="${k}"]`);
      const row = el && el.closest(".prow");
      if (row) row.classList.add("guard-bad");
    });
    toast(`有 ${bad.length} 项为空/不是数字：${bad.join("、")}`, "err");
    return;
  }
  if (!Object.keys(patch).length) {
    toast("这一页没有可保存的参数", "err");
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
  if (!res.ok) {
    const bad = guardCheckPatch(patch);
    LAST_REJECT = { msg: String(res.error || ""), keys: bad.map((b) => b.key), detail: bad, at: Date.now() / 1000 };
    toast("保存失败：" + res.error, "err");
    refreshCurrent();                 // 让「参数护栏」卡与参数行标出这一批被哪些键拦下
    return;
  }
  LAST_REJECT = null;
  // 服务端确认保存成功后才登记「最近改过」（参数中心置顶用）——
  // 只登记**用户真的手改过**的键（保存会把整批行原样回写，全批都算改过就没意义了）
  noteConfigTouched(takeConfigDirty(Object.keys(patch)));
  const nw = nativeWriteError(res);
  toast(nw ? `已写入插件库，但原生配置写回失败：${nw}` : `已保存 ${Object.keys(patch).length} 项配置`, nw ? "err" : "ok");
  refreshCurrent();
}

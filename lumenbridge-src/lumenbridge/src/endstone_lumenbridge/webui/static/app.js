"use strict";

let TOKEN = localStorage.getItem("lumen_token") || "";
let configMode = "form"; // form | json
let configData = null;
let configLabels = {};
let logSource = null;
let logGeneration = 0;
let configGeneration = 0;
let currentPage = "";
let editingPlugin = "";
let editingPluginConfig = "";
let editingPluginSchema = null;
let editingPluginPendingFiles = {}; // key → File 对象（选了但尚未上传，点保存才提交，点取消丢弃）
let pluginConfigNames = new Set();
let rulesMode = "gui"; // gui | json
let rulesData = [];
let editingRuleIndex = -1;
let bgConfigured = null;
let configLabelsLoading = false;
let configEditorBound = false;

let metricsTimer = null;
let metricsGeneration = 0;
const GAUGE_CIRC = 2 * Math.PI * 52; // 环形仪表盘周长（r=52）
const MORE_ICON_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" style="width:19px;height:19px;flex-shrink:0"><circle cx="12" cy="12" r="9"/><path d="M3.6 9h16.8M3.6 15h16.8M12 3a15 15 0 0 1 0 18 15 15 0 0 1 0-18"/></svg>';

let pipTaskState = null;       // { taskId, done, success, subpluginName, status, msg, doneHandled, reloadShown }
let pipPollTimer = null;
let reloadPromptState = null;  // { subpluginName, isConfig }
let subpluginDepsCache = {};   // 子插件名 -> 缺失依赖列表
let subpluginErrorCache = {};  // 子插件名 -> 错误信息全文
let subpluginMarketCache = {}; // 子插件名 -> { market, version, hasError, hasMissing }，更新流程复用
let editingDepsPlugin = "";
let configNavObserver = null;
let spMoreMenuState = null;    // { name, btn } 当前打开的 ⋯ 菜单
let marketUpdateState = null;  // { name, marketId, detail, currentVersion, selectedVersion } 市场更新流程状态
const CONFIG_RELOAD_PREFIXES = ["connection.", "pip.", "commands."];
const CONFIG_RELOAD_EXACT = ["webui.enable", "webui.host", "webui.port", "language"];

let I18N = {};
let CURRENT_LANG = localStorage.getItem("lumen_lang") || "auto";  // "auto" 或具体语言
let i18nReady = false;

async function loadI18n(lang) {
  let targetLang = lang;
  if (lang === "auto" || !lang) {
    try {
      const res = await api("GET", "/api/i18n/current");
      targetLang = (res.data && res.data.language) || "zh_CN";
    } catch (e) {
      targetLang = "zh_CN";
    }
  }
  try {
    const res = await api("GET", "/api/i18n/" + targetLang);
    I18N = res.data || {};
    CURRENT_LANG = targetLang;
    localStorage.setItem("lumen_lang", lang || "auto");
    document.documentElement.lang = targetLang.replace("_", "-");
    mergeConfigLabelsFromI18n();
    i18nReady = true;
    applyI18n();
    updateLangButtons();
    if (currentPage === "config" && configData) renderConfigForm();
  } catch (e) {
    console.error("i18n load failed:", e);
  }
}

function t(key, params) {
  if (!I18N || !key) return key;
  const parts = key.split(".");
  let val = I18N;
  for (const p of parts) {
    if (val && typeof val === "object" && p in val) {
      val = val[p];
    } else {
      return key;
    }
  }
  if (typeof val !== "string") return key;
  if (params) {
    for (const [k, v] of Object.entries(params)) {
      const kEsc = k.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
      val = val.replace(new RegExp("\\{" + kEsc + "(?::[^}]*)?\\}", "g"), () => String(v));
    }
  }
  return val;
}

function applyI18n() {
  document.querySelectorAll("[data-i18n]").forEach((el) => {
    const key = el.getAttribute("data-i18n");
    const text = t(key);
    if (text && text !== key) el.textContent = text;
  });
  document.querySelectorAll("[data-i18n-placeholder]").forEach((el) => {
    const key = el.getAttribute("data-i18n-placeholder");
    const text = t(key);
    if (text && text !== key) el.placeholder = text;
  });
  document.querySelectorAll("[data-i18n-value]").forEach((el) => {
    const key = el.getAttribute("data-i18n-value");
    const text = t(key);
    if (text && text !== key) el.value = text;
  });
}

function updateLangButtons() {
  const stored = localStorage.getItem("lumen_lang") || "auto";
  document.querySelectorAll(".lang-btn").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.lang === stored);
  });
}

function mergeConfigLabelsFromI18n() {
  const labels = I18N && I18N.config_labels;
  if (!labels || typeof labels !== "object") return;

  // 配置 schema 包含 commands.status.allow_player 等三层以上路径。旧实现
  // 只合并两层，导致页面末尾的命令权限字段在切换语言后退回英文键名。
  const mergeNode = (node, path) => {
    if (!node || typeof node !== "object" || !path) return;
    const current = configLabels[path] || {};
    if (typeof node.section === "string") current._ = node.section;
    if (typeof node.label === "string") {
      current.label = node.label;
      current.desc = typeof node.desc === "string" ? node.desc : "";
    }
    if (Object.keys(current).length) configLabels[path] = current;

    for (const [key, value] of Object.entries(node)) {
      if (key === "section" || key === "label" || key === "desc") continue;
      if (value && typeof value === "object") {
        mergeNode(value, path + "." + key);
      }
    }
  };

  for (const [key, value] of Object.entries(labels)) mergeNode(value, key);
}

document.addEventListener("click", async (e) => {
  const btn = e.target.closest(".lang-btn");
  if (!btn) return;
  const lang = btn.dataset.lang;
  await loadI18n(lang);
});

function customConfirm(message, title) {
  return new Promise((resolve) => {
    const modal = document.getElementById("confirm-modal");
    const msgEl = document.getElementById("confirm-modal-message");
    const titleEl = document.getElementById("confirm-modal-title");
    const okBtn = document.getElementById("confirm-modal-ok");
    const cancelBtn = document.getElementById("confirm-modal-cancel");
    if (!modal || !okBtn || !cancelBtn) {
      // DOM 未就绪时返回 false，绝不降级到浏览器原生 confirm()
      resolve(false);
      return;
    }
    msgEl.textContent = message;
    titleEl.textContent = title || t("modal.confirm_title");
    modal.classList.add("show");

    const cleanup = () => {
      modal.classList.remove("show");
      okBtn.removeEventListener("click", onOk);
      cancelBtn.removeEventListener("click", onCancel);
      modal.removeEventListener("click", onMask);
    };
    const onOk = () => { cleanup(); resolve(true); };
    const onCancel = () => { cleanup(); resolve(false); };
    const onMask = (e) => { if (e.target === modal) { cleanup(); resolve(false); } };

    okBtn.addEventListener("click", onOk);
    cancelBtn.addEventListener("click", onCancel);
    modal.addEventListener("click", onMask);
  });
}

/** 自定义信息弹窗（单“确定”按钮，不使用浏览器原生 alert） */
function customAlert(message, title) {
  return new Promise((resolve) => {
    const modal = document.getElementById("alert-modal");
    const msgEl = document.getElementById("alert-modal-message");
    const titleEl = document.getElementById("alert-modal-title");
    const okBtn = document.getElementById("alert-modal-ok");
    if (!modal || !okBtn || !msgEl || !titleEl) {
      // DOM 未就绪时直接放行，绝不降级到浏览器原生 alert()
      resolve();
      return;
    }
    msgEl.textContent = message;
    titleEl.textContent = title || t("modal.alert_title");
    modal.classList.add("show");

    const cleanup = () => {
      modal.classList.remove("show");
      okBtn.removeEventListener("click", onOk);
      modal.removeEventListener("click", onMask);
    };
    const onOk = () => { cleanup(); resolve(); };
    const onMask = (e) => { if (e.target === modal) { cleanup(); resolve(); } };

    okBtn.addEventListener("click", onOk);
    modal.addEventListener("click", onMask);
  });
}

function toast(msg, isErr) {
  const el = document.getElementById("toast");
  document.getElementById("toast-text").textContent = msg;
  el.className = isErr ? "err show" : "show";
  clearTimeout(el._t);
  el._t = setTimeout(() => el.classList.remove("show"), 4000);
}

function dismissToast() {
  const el = document.getElementById("toast");
  el.classList.remove("show");
  clearTimeout(el._t);
}

function maskClick(ev, id) {
  if (ev.target === ev.currentTarget) closeModal(id);
}

async function api(method, path, body) {
  const opts = {
    method,
    headers: { "Content-Type": "application/json", Authorization: "Bearer " + TOKEN },
  };
  if (body !== undefined) opts.body = JSON.stringify(body);
  const res = await fetch(path, opts);
  const data = await res.json().catch(() => ({}));
  if (res.status === 401) {
    logout(true);
    throw new Error(data.msg || t("errors.login_expired"));
  }
  if (data.code && data.code !== 200) throw new Error(data.msg || t("errors.request_failed"));
  return data;
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function cssEscape(s) {
  return window.CSS && CSS.escape ? CSS.escape(String(s ?? "")) : String(s ?? "").replace(/[^a-zA-Z0-9_-]/g, "");
}

function textAction(label, onclick, extraClass = "ghost", symbol = "") {
  const icon = symbol ? `<span class="action-icon" aria-hidden="true">${symbol}</span>` : "";
  return `<button class="btn small ${extraClass}" onclick="${onclick}">${icon}<span>${esc(label)}</span></button>`;
}

function spMenuItem(label, onclick, cls = "") {
  return `<button class="btn small ${cls}" onclick="${onclick}">${esc(label)}</button>`;
}

document.addEventListener("click", (e) => {
  if (!e.target.closest(".custom-select")) closeAllCustomSelects();
});

function toggleCustomSelect(btn) {
  const list = btn.nextElementSibling;
  const isOpen = list && list.classList.contains("show");
  closeAllCustomSelects();
  if (!isOpen && list) {
    list.classList.add("show");
    btn.classList.add("open");
  }
}

function closeAllCustomSelects() {
  document.querySelectorAll(".custom-select-list.show").forEach((l) => l.classList.remove("show"));
  document.querySelectorAll(".custom-select-btn.open").forEach((b) => b.classList.remove("open"));
}

function selectMarketplaceSort(el, value) {
  const wrap = el.closest(".custom-select");
  if (!wrap) return;
  wrap.querySelectorAll(".custom-select-option").forEach((o) => o.classList.remove("selected"));
  el.classList.add("selected");
  const label = wrap.querySelector(".cs-label");
  if (label) label.textContent = el.textContent;
  closeAllCustomSelects();
  wrap.dataset.value = value;
  loadMarketplace();
}

function fmtUptime(sec) {
  const d = Math.floor(sec / 86400), h = Math.floor((sec % 86400) / 3600),
        m = Math.floor((sec % 3600) / 60);
  return (d ? d + " " : "") + h + ":" + String(m).padStart(2, "0");
}

function fmtSize(bytes) {
  if (bytes < 1024) return bytes + " B";
  if (bytes < 1048576) return (bytes / 1024).toFixed(1) + " KB";
  if (bytes < 1073741824) return (bytes / 1048576).toFixed(1) + " MB";
  return (bytes / 1073741824).toFixed(2) + " GB";
}

function closeModal(id) {
  // 关闭子插件配置弹窗时丢弃暂存文件（取消 = 不上传）
  if (id === "plugin-config-modal") {
    editingPluginPendingFiles = {};
  }
  const el = document.getElementById(id);
  if (el) el.classList.remove("show");
}

function copyToClipboard(text) {
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).catch(() => fallbackCopy(text));
  } else {
    fallbackCopy(text);
  }
}

function fallbackCopy(text) {
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.style.position = "fixed";
  ta.style.opacity = "0";
  document.body.appendChild(ta);
  ta.select();
  try { document.execCommand("copy"); } catch (e) {}
  document.body.removeChild(ta);
}

function closeLogStream() {
  logGeneration += 1;
  if (logSource) {
    logSource.onmessage = null;
    logSource.onerror = null;
    logSource.close();
    logSource = null;
  }
}


async function login() {
  const pwd = document.getElementById("login-password").value;
  const errEl = document.getElementById("login-err");
  errEl.textContent = "";
  try {
    const res = await fetch("/api/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ password: pwd }),
    });
    const data = await res.json();
    if (data.code !== 200) throw new Error(data.msg || t("login.failed"));
    TOKEN = data.data.token;
    localStorage.setItem("lumen_token", TOKEN);
    await loadI18n(localStorage.getItem("lumen_lang") || "auto");
    showApp();
    if (localStorage.getItem("lumen_pending_reload")) {
      localStorage.removeItem("lumen_pending_reload");
      showReloadPrompt("reload_prompt.message_config", "");
    }
  } catch (e) {
    errEl.textContent = e.message;
  }
}

function logout(silent) {
  TOKEN = "";
  localStorage.removeItem("lumen_token");
  closeLogStream();
  stopMetricsPolling();
  if (pipPollTimer) { clearInterval(pipPollTimer); pipPollTimer = null; }
  pipTaskState = null;
  if (typeof configNavObserver !== "undefined" && configNavObserver) { configNavObserver.disconnect(); configNavObserver = null; }
  if (marketTaskTimer) { clearInterval(marketTaskTimer); marketTaskTimer = null; }
  if (frameworkUpdateTimer) { clearInterval(frameworkUpdateTimer); frameworkUpdateTimer = null; }
  closeTaskLogModal();
  if (dashboardRefreshTimer) { clearInterval(dashboardRefreshTimer); dashboardRefreshTimer = null; }
  if (typeof stopQrBindPoll === "function") stopQrBindPoll();
  document.getElementById("app-view").style.display = "none";
  document.getElementById("login-view").style.display = "flex";
  if (!silent) toast(t("login.logged_out"));
}

function showApp() {
  document.getElementById("login-view").style.display = "none";
  document.getElementById("app-view").style.display = "block";
  startDashboardRefresh();
  nav("dashboard");
  loadCustomPages();
  loadBackground();
}


async function loadBackground() {
  try {
    const res = await fetch("/api/public/background");
    const data = await res.json();
    if (data.code === 200) {
      bgConfigured = data.data;
      applyBackground(bgConfigured);
      return;
    }
  } catch (e) {}

  if (TOKEN) {
    try {
      const { data } = await api("GET", "/api/config");
      const bg = (data && data.background) || {};
      bgConfigured = bg;
      applyBackground(bg);
    } catch (e) {
      bgConfigured = null;
      applyBackground(null);
    }
  }
}

function applyBackground(bg) {
  const layer = document.getElementById("bg-layer");
  const body = document.body;
  if (!layer) return;
  if (!bg || !bg.enable || !bg.api_url) {
    layer.classList.remove("show");
    body.classList.remove("has-bg");
    layer.style.backgroundImage = "";
    return;
  }
  const blur = parseInt(bg.blur_strength, 10);
  if (!isNaN(blur)) {
    document.documentElement.style.setProperty("--bg-blur", blur + "px");
  } else {
    document.documentElement.style.setProperty("--bg-blur", "18px");
  }
  // 加 cache_seconds 避免浏览器无限缓存导致切换不及时
  const sep = bg.api_url.includes("?") ? "&" : "?";
  const url = bg.api_url + sep + "_t=" + Math.floor(Date.now() / ((bg.cache_seconds || 600) * 1000));
  const probe = new Image();
  probe.onload = () => {
    layer.style.backgroundImage = `url("${url}")`;
    layer.classList.add("show");
    body.classList.add("has-bg");
  };
  probe.onerror = () => {
    if (bg.fallback_to_default !== false) {
      layer.classList.remove("show");
      body.classList.remove("has-bg");
      layer.style.backgroundImage = "";
    }
  };
  probe.src = url;
}


/* ─── 移动端可滚动 tab 条：激活 tab 平滑滚动居中（scrollIntoView 居中变体，
   参考 Android TabLayout scrollable / Chrome 标签条交互）─── */
function centerActiveTab(page) {
  const bar = document.getElementById("tabbar");
  const scroller = document.getElementById("tabbar-scroll");
  // 桌面端 tabbar 隐藏（display:none）时无需滚动；不能用 offsetParent
  // 判断——position:fixed 元素的 offsetParent 恒为 null
  if (!bar || !scroller || getComputedStyle(bar).display === "none") return;
  const el = scroller.querySelector(`.tab-item[data-page="${cssEscape(page)}"]`);
  if (!el) return;
  const target = el.offsetLeft - (scroller.clientWidth - el.offsetWidth) / 2;
  scroller.scrollTo({ left: Math.max(0, target), behavior: "smooth" });
}

/* 两侧渐隐提示：仅该侧还有未滚入视野的 tab 时亮起 */
function syncTabbarEdges() {
  const bar = document.getElementById("tabbar");
  const scroller = document.getElementById("tabbar-scroll");
  if (!bar || !scroller) return;
  const max = scroller.scrollWidth - scroller.clientWidth;
  bar.classList.toggle("edge-left", scroller.scrollLeft > 1);
  bar.classList.toggle("edge-right", scroller.scrollLeft < max - 1);
}

/* 子插件 iframe 全宽铺开：负 margin 抵消 layout 的 padding/max-width 限制，
   内容区横向铺满整个视口（不超屏幕），去掉“方框”束缚感。
   sidebar 已设 z-index:2，导航仍在 iframe 内容之上可正常点击。 */
function expandCustomFrame() {
  const wrap = document.querySelector("#page-custom .iframe-wrap");
  if (!wrap) return;
  const r = wrap.getBoundingClientRect();
  const ml = -Math.round(r.left);
  const mr = -Math.round(window.innerWidth - r.right);
  if (ml >= 0 && mr >= 0 && ml < window.innerWidth && mr < window.innerWidth) {
    wrap.style.marginLeft = ml + "px";
    wrap.style.marginRight = mr + "px";
  }
}
window.addEventListener("resize", () => {
  if (document.getElementById("page-custom").style.display !== "none") expandCustomFrame();
});

/* 子插件（iframe 内）委托主面板顶层显示确认弹窗：
   iframe 内部的 fixed 弹窗无法超出 iframe 边界，转发到父页面才能真正置顶。 */
window.addEventListener("message", async (e) => {
  const d = e.data;
  if (!d || typeof d !== "object") return;
  if (e.origin !== window.location.origin) return;
  if (d.type === "lumen-confirm") {
    const ok = await customConfirm(String(d.message || ""), d.title ? String(d.title) : undefined);
    if (e.source) e.source.postMessage({ type: "lumen-confirm-result", id: String(d.id || ""), ok: !!ok }, e.origin);
  }
  /* 子插件页面内容高度上报：iframe 高度随内容自适应，
     页面随主面板一起滚动，不再是被固定高度方框框住的“嵌入式网页” */
  if (d.type === "lumen-page-height") {
    const frame = document.getElementById("custom-frame");
    const h = Math.ceil(Number(d.height) || 0);
    if (frame && h > 0) frame.style.height = h + "px";
  }
  /* 子插件页面（iframe 内）的 toast 委托：iframe 自适应高度后其内部
     fixed 定位的 toast 会漂到文档底部，统一由主面板顶部 toast 呈现。
     处理后回执（lumen-toast-ack）：子插件据此判断主面板已显示，
     否则 350ms 后回退本地顶部显示（兼容未更新的旧主面板） */
  if (d.type === "lumen-toast") {
    toast(String(d.message || ""), !!d.err);
    try {
      if (e.source) e.source.postMessage({ type: "lumen-toast-ack", id: String(d.id || "") }, e.origin);
    } catch (err) { /* ignore */ }
  }
  /* 子插件弹窗（iframe 内）打开/关闭：
     - 打开：iframe 外围（含底部导航）盖同色遮罩条，与 iframe 内部遮罩拼成
       视觉无缝的全屏遮罩（消除“只有 iframe 矩形变暗”的方形边界）；
       同时把 iframe 顶端滚到视口顶部附近，让弹窗卡片尽量上移。
     - 关闭：撤遮罩并恢复打开前的滚动位置。 */
  if (d.type === "lumen-modal-open") subpluginModalOpen();
  if (d.type === "lumen-modal-close") subpluginModalClose();
});

/* ---- 子插件弹窗的外围遮罩与滚动配合 ---- */
let _subModalOpen = false;
function _subDimLayout() {
  const wrap = document.getElementById("subplugin-dim");
  const frame = document.getElementById("custom-frame");
  if (!wrap || !frame) return;
  const r = frame.getBoundingClientRect();
  const W = window.innerWidth;
  const H = window.innerHeight;
  const y1 = Math.max(0, r.top);
  const y2 = Math.min(H, r.bottom);
  const set = (pos, l, t, w, h) => {
    const el = wrap.querySelector('[data-pos="' + pos + '"]');
    if (!el) return;
    el.style.left = l + "px";
    el.style.top = t + "px";
    el.style.width = Math.max(0, w) + "px";
    el.style.height = Math.max(0, h) + "px";
  };
  set("top", 0, 0, W, r.top);
  set("bottom", 0, r.bottom, W, H - r.bottom);
  set("left", 0, y1, r.left, y2 - y1);
  set("right", r.right, y1, W - r.right, y2 - y1);
}
function subpluginModalOpen() {
  const frame = document.getElementById("custom-frame");
  if (!frame) return;
  _subModalOpen = true;
  const dim = document.getElementById("subplugin-dim");
  if (dim) {
    dim.classList.add("show");
    _subDimLayout();
    window.addEventListener("scroll", _subDimLayout, { passive: true });
    window.addEventListener("resize", _subDimLayout);
  }
  /* 弹窗卡片尽量上移：仅当 iframe 顶端在视口内偏下（页面顶部标题区域）时
     瞬时滚动把它带到视口顶部附近；用户已滚进页面深处时（iframe 顶端在视口外）
     不滚动——卡片由子插件 rAF 钉在可视区顶部，避免任何滚动突跳 */
  const t = frame.getBoundingClientRect().top;
  if (t > 10 && t < 400) window.scrollBy(0, t - 10);
  /* 锁定背景滚动（标准模态行为）：
     1. 弹窗内滚动不再链式带动父页面（此前滚到底会拖着整页滚）；
     2. 卡片钉在可视区完全静止——此前 rAF 每帧跟随父页面滚动重写位置，
        按钮在 mousedown→mouseup 之间移位会导致 click 丢失（保存按钮"点了没反应"）。
     桌面端锁定后滚动条消失会引起 ~15px 横移，用等宽 padding 补偿。 */
  const doc = document.documentElement;
  const sbw = window.innerWidth - doc.clientWidth;
  doc.style.overflow = "hidden";
  doc.style.paddingRight = sbw > 0 ? sbw + "px" : "";
}
function subpluginModalClose() {
  const dim = document.getElementById("subplugin-dim");
  if (dim) dim.classList.remove("show");
  window.removeEventListener("scroll", _subDimLayout);
  window.removeEventListener("resize", _subDimLayout);
  const doc = document.documentElement;
  doc.style.overflow = "";
  doc.style.paddingRight = "";
  _subModalOpen = false;
}
/* 点击 iframe 外的遮罩条 = 点击弹窗外区域：转发关闭指令给 iframe 内的弹窗 */
(function initSubpluginDim() {
  document.addEventListener("DOMContentLoaded", () => {
    const dim = document.getElementById("subplugin-dim");
    if (!dim) return;
    dim.addEventListener("click", () => {
      if (!_subModalOpen) return;
      const frame = document.getElementById("custom-frame");
      try {
        if (frame && frame.contentWindow) {
          frame.contentWindow.postMessage({ type: "lumen-modal-dismiss" }, window.location.origin);
        }
      } catch (e) { /* 跨域等异常：忽略 */ }
    });
  });
})();

/* 注入到子插件页面（同源 iframe）的自适应高度上报脚本：
   内容尺寸变化（ResizeObserver）+ 结构变化兜底轮询，实时把文档高度
   postMessage 给主面板；脚本自身幂等，重复注入无副作用。 */
const CUSTOM_AUTOHEIGHT_SCRIPT =
  "(function(){if(window.__lumenAutoHeight)return;window.__lumenAutoHeight=1;" +
  "var last=0;function report(){try{var b=document.body,d=document.documentElement;if(!b)return;" +
  "var h=Math.max(b.scrollHeight,b.offsetHeight||0,d.scrollHeight,d.offsetHeight||0);" +
  "if(Math.abs(h-last)<1)return;last=h;" +
  "parent.postMessage({type:'lumen-page-height',height:h},location.origin);}catch(e){}}" +
  "window.addEventListener('load',report);window.addEventListener('resize',report);" +
  "if(window.ResizeObserver){try{var ro=new ResizeObserver(report);" +
  "ro.observe(document.body);ro.observe(document.documentElement);}catch(e){}}" +
  "setInterval(report,800);})();";

/* 子插件页面加载完成后注入自适应高度脚本（同源可写 contentDocument；
   万一注入失败则保持 CSS 兜底高度，行为回退为原固定高度内滚动）。 */
(function initCustomFrameAutoHeight() {
  document.addEventListener("DOMContentLoaded", () => {
    const frame = document.getElementById("custom-frame");
    if (!frame) return;
    frame.addEventListener("load", function () {
      expandCustomFrame();
      /* 兜底：子插件页面重载（如保存后刷新）时弹窗已不复存在，撤掉外围遮罩 */
      if (_subModalOpen) subpluginModalClose();
      try {
        const doc = this.contentDocument;
        if (!doc || !doc.body) return;
        const s = doc.createElement("script");
        s.textContent = CUSTOM_AUTOHEIGHT_SCRIPT;
        (doc.head || doc.documentElement).appendChild(s);
      } catch (e) { /* 跨域等异常：维持 CSS 兜底固定高度 */ }
    });
  });
})();

function nav(page, customUrl, customTitle) {
  const target = customUrl ? "custom" : page;
  if (currentPage === target && !customUrl) return;
  const pageEl = document.getElementById("page-" + target);
  if (!pageEl) {
    toast(t("errors.page_not_found", { target }), true);
    return;
  }

  if (currentPage === "logs" && page !== "logs") closeLogStream();
  if (currentPage === "dashboard" && target !== "dashboard") stopMetricsPolling();
  if (currentPage === "marketplace" && target !== "marketplace") {
    if (marketTaskTimer) { clearInterval(marketTaskTimer); marketTaskTimer = null; }
    if (frameworkUpdateTimer) { clearInterval(frameworkUpdateTimer); frameworkUpdateTimer = null; }
    closeTaskLogModal();
  }
  // 离开任意页面时停掉扫码绑定轮询：模态框固定定位不随导航消失，
  // 不清理会一直轮询 /api/qqofficial/qr/poll 并在成功后弹出编辑框
  if (typeof stopQrBindPoll === "function") stopQrBindPoll();
  currentPage = target;
  document.querySelectorAll(".page").forEach((p) => (p.style.display = "none"));
  document.querySelectorAll(".nav-item, .tab-item").forEach((n) => n.classList.remove("active"));
  pageEl.style.display = "block";
  document.querySelectorAll(`.nav-item[data-page="${page}"], .tab-item[data-page="${page}"]`)
    .forEach((el) => el.classList.add("active"));
  centerActiveTab(page);
  requestAnimationFrame(syncTabbarEdges);

  if (customUrl) {
    document.getElementById("custom-title").textContent = customTitle || t("subplugins.custom_page_default_title");
    const frame = document.getElementById("custom-frame");
    frame.style.height = "";  // 清除旧页面的自适应高度，加载前先用 CSS 兜底高度
    frame.src = customUrl + (customUrl.includes("?") ? "&" : "?") + "token=" + encodeURIComponent(TOKEN);
    expandCustomFrame();
    return;
  }
  if (page === "dashboard") { loadDashboard(); startMetricsPolling(); }
  if (page === "config") {
    loadConfig();
    if (configPane === "connections") loadConnections();
  }
  if (page === "rules") loadRules();
  if (page === "whitelist") loadWhitelist();
  if (page === "subplugins") loadSubplugins();
  if (page === "marketplace") loadMarketplacePage();
  if (page === "packages") loadPipPackages();
  if (page === "logs") initLogs();
  window.scrollTo({ top: 0 });
}


async function loadDashboard() {
  try {
    const { data: rawData } = await api("GET", "/api/overview");
    const d = rawData || {};
    document.getElementById("dash-version").textContent = t("dashboard.subtitle", { version: d.version });
    // 多账号：每个启用的适配器一条小资料行（最多显示 3 行，超出省略）
    const profiles = Array.isArray(d.bot_profiles) && d.bot_profiles.length
      ? d.bot_profiles
      : (d.bot_profile ? [d.bot_profile] : []);
    const profileRow = (p) => {
      const qq = p.qq || 0;
      const name = p.nickname || (p.connected || d.connected
        ? t("dashboard.bot_nickname_loading")
        : t("dashboard.bot_nickname_offline"));
      const avatar = p.avatar_url
        ? `<img class="bot-avatar sm" src="${esc(p.avatar_url)}" alt="${esc(t("dashboard.qq_avatar_alt"))}" referrerpolicy="no-referrer" onerror="this.hidden=true;this.nextElementSibling.hidden=false"><span class="bot-avatar fallback sm" hidden>QQ</span>`
        : `<span class="bot-avatar fallback sm">QQ</span>`;
      const tag = p.adapter_name ? `<i class="bot-profile-tag" title="${esc(p.adapter_name)}">${esc(p.adapter_name)}</i>` : "";
      return `<span class="bot-profile-inline">${avatar}<span><b>${esc(name)} ${tag}</b><small>${esc(t("dashboard.qq_prefix"))}${esc(qq || t("common.not_set"))}</small></span></span>`;
    };
    const botProfile = profiles.length
      ? `<span class="bot-profile-list">${profiles.map(profileRow).join("")}</span>`
      : "";
    const mainGroups = Array.isArray(d.main_groups) ? d.main_groups : [];
    const groupsText = mainGroups.length
      ? mainGroups.join("、")
      : (d.main_group ? String(d.main_group) : t("common.not_set"));
    const modeLabel = d.mode_name || (d.mode === 0 || d.mode === "0"
      ? t("dashboard.forward_ws")
      : d.mode === 1 || d.mode === "1"
        ? t("dashboard.reverse_ws")
        : t("common.not_set"));
    const onlinePlayers = Array.isArray(d.online_players) ? d.online_players : [];
    // OneBot 连接卡片：按启用的适配器类型分别显示（QQ个人号 / QQ官方bot / AstrBot）
    const adapters = Array.isArray(d.adapters) ? d.adapters : [];
    const connSeg = (label, ok) =>
      `<span class="conn-seg"><span class="status-dot ${ok ? "ok" : "bad"}"></span>${esc(label)} ${ok ? esc(t("dashboard.connected")) : esc(t("dashboard.disconnected"))}</span>`;
    const typeOf = (a) => String(a.type || "websocket");
    const enabledList = adapters.filter((a) => a.enabled);
    const segs = [];
    const qqList = enabledList.filter((a) => typeOf(a) !== "qqofficial" && typeOf(a) !== "astrbot");
    const officialList = enabledList.filter((a) => typeOf(a) === "qqofficial");
    const astrList = enabledList.filter((a) => typeOf(a) === "astrbot");
    if (qqList.length) segs.push(connSeg(t("dashboard.conn_type_onebot"), qqList.some((a) => a.connected)));
    if (officialList.length) segs.push(connSeg(t("dashboard.conn_type_qqofficial"), officialList.some((a) => a.connected)));
    if (astrList.length) segs.push(connSeg(t("dashboard.conn_type_astrbot"), astrList.some((a) => a.connected)));
    const connValue = segs.length
      ? segs.join("")
      : `<span class="status-dot ${d.connected ? "ok" : "bad"}"></span>${d.connected ? t("dashboard.connected") : t("dashboard.disconnected")}`;
    const cards = [
      [t("dashboard.onebot_connection"), connValue],
      [t("dashboard.connection_mode"), esc(modeLabel)],
      [t("dashboard.main_group"), esc(groupsText)],
      [t("dashboard.bot_profile"), botProfile],
      [t("dashboard.online_players"), onlinePlayers.length + " " + t("dashboard.players_unit")],
      [t("dashboard.whitelist_count"), d.whitelist_count + " " + t("dashboard.count_unit")],
      [t("dashboard.rules_count"), d.rules_count + " " + t("dashboard.count_unit")],
      [t("dashboard.subplugin_count"), d.subplugin_count + " " + t("dashboard.count_unit")],
    ];
    document.getElementById("dash-cards").innerHTML = cards
      .map(([k, v]) => `<div class="card glass interactive"><div class="k">${k}</div><div class="v small">${v}</div></div>`)
      .join("");
    document.getElementById("dash-players").textContent =
      onlinePlayers.length ? onlinePlayers.join("、") : t("dashboard.no_players_online");
    document.getElementById("dash-env").innerHTML = `
      <tr><td style="color:var(--muted)">${esc(t("dashboard.uptime"))}</td><td>${fmtUptime(d.uptime)}</td></tr>
      <tr><td style="color:var(--muted)">${esc(t("dashboard.python_version"))}</td><td>${esc(d.python_version)}</td></tr>
      <tr><td style="color:var(--muted)">${esc(t("dashboard.os_version"))}</td><td>${esc(d.os_version)} (${esc(d.arch)})</td></tr>
      <tr><td style="color:var(--muted)">${esc(t("dashboard.pid"))}</td><td>${d.pid}</td></tr>`;
  } catch (e) { toast(t("dashboard.load_failed", { error: e.message }), true); }
}


function setGauge(ringId, valId, subId, footId, percent, sub, foot) {
  const ring = document.getElementById(ringId);
  const val = document.getElementById(valId);
  const subEl = document.getElementById(subId);
  const footEl = document.getElementById(footId);
  const p = Math.max(0, Math.min(100, Number(percent) || 0));
  if (ring) {
    ring.setAttribute("stroke-dasharray", GAUGE_CIRC.toFixed(2));
    ring.setAttribute("stroke-dashoffset", (GAUGE_CIRC * (1 - p / 100)).toFixed(2));
    ring.classList.remove("warn", "danger");
    if (p >= 90) ring.classList.add("danger");
    else if (p >= 70) ring.classList.add("warn");
  }
  if (val) val.textContent = p.toFixed(1);
  if (subEl && sub !== undefined) subEl.textContent = sub;
  if (footEl && foot !== undefined) footEl.innerHTML = foot;
}

function showMetricsUnavailable(reason) {
  setGauge("gauge-cpu-ring", "gauge-cpu-val", "gauge-cpu-sub", "gauge-cpu-foot", 0, t("common.unavailable"), "—");
  setGauge("gauge-mem-ring", "gauge-mem-val", "gauge-mem-sub", "gauge-mem-foot", 0, t("common.unavailable"), "—");
  document.getElementById("metric-cpu-card").classList.add("unavailable");
  document.getElementById("metric-mem-card").classList.add("unavailable");
  const val = document.getElementById("gauge-cpu-val");
  if (val) val.textContent = t("common.unavailable");
  const mval = document.getElementById("gauge-mem-val");
  if (mval) mval.textContent = t("common.unavailable");
  if (reason) {
    const foot = document.getElementById("gauge-cpu-foot");
    if (foot) foot.textContent = t("dashboard.reason_prefix", { reason });
  }
}

async function loadServerMetrics() {
  const gen = metricsGeneration;
  try {
    const { data: m } = await api("GET", "/api/server/metrics");
    if (gen !== metricsGeneration) return;
    const cpuCard = document.getElementById("metric-cpu-card");
    const memCard = document.getElementById("metric-mem-card");
    if (!m || !m.available) {
      showMetricsUnavailable(m && m.reason ? m.reason : "");
      return;
    }
    if (cpuCard) cpuCard.classList.remove("unavailable");
    if (memCard) memCard.classList.remove("unavailable");
    const cpuSub = m.cpu_count ? t("dashboard.cpu_cores", { n: m.cpu_count }) : "";
    setGauge(
      "gauge-cpu-ring", "gauge-cpu-val", "gauge-cpu-sub", "gauge-cpu-foot",
      m.cpu_percent, cpuSub, t("dashboard.cpu_realtime")
    );
    const memSub = m.mem_total ? fmtSize(m.mem_used) + " / " + fmtSize(m.mem_total) : "";
    setGauge(
      "gauge-mem-ring", "gauge-mem-val", "gauge-mem-sub", "gauge-mem-foot",
      m.mem_percent, memSub, t("dashboard.mem_used_total")
    );
  } catch (e) {
    if (gen !== metricsGeneration) return;
  }
}

function startMetricsPolling() {
  stopMetricsPolling();
  metricsGeneration += 1;
  loadServerMetrics();
  metricsTimer = setInterval(() => {
    const dash = document.getElementById("page-dashboard");
    if (!TOKEN || !dash || dash.style.display === "none" ||
        document.getElementById("app-view").style.display === "none" ||
        document.hidden) return;
    loadServerMetrics();
  }, 3000);
}

function stopMetricsPolling() {
  metricsGeneration += 1;
  if (metricsTimer) {
    clearInterval(metricsTimer);
    metricsTimer = null;
  }
}


async function loadConfig() {
  const generation = ++configGeneration;
  const form = document.getElementById("config-form");
  if (form) form.innerHTML = '<div class="empty-state">' + esc(t("common.loading_config")) + '</div>';
  try {
    if (!Object.keys(configLabels).length && !configLabelsLoading) {
      configLabelsLoading = true;
      try {
        const { data: labels } = await api("GET", "/api/config/labels");
        if (generation !== configGeneration) { configLabelsLoading = false; return; }
        configLabels = labels || {};
        configLabelsLoading = false;
      } catch (e) {
        configLabelsLoading = false;
        if (generation !== configGeneration) return;
        configLabels = {};
      }
    }
    if (i18nReady) mergeConfigLabelsFromI18n();
    const { data } = await api("GET", "/api/config");
    if (generation !== configGeneration || currentPage !== "config") return;
    configData = data && typeof data === "object" ? data : {};
    renderConfigForm();
    const ta = document.getElementById("config-json");
    const text = JSON.stringify(configData, null, 4);
    ta.value = text;
    const code = document.getElementById("config-code");
    if (code) code.innerHTML = highlightCode(text, "json");
  } catch (e) {
    if (generation !== configGeneration) return;
    if (form) form.innerHTML = `<div class="empty-state error">${esc(t("config.load_failed", { error: e.message }))}</div>`;
    toast(t("config.load_failed", { error: e.message }), true);
  }
}

function labelOf(path, key) {
  const info = configLabels[path];
  if (info && info.label) return { label: info.label, desc: info.desc || "" };
  return { label: key, desc: "" };
}

function sectionTitleOf(key) {
  const info = configLabels[key];
  return (info && info._) ? info._ : key;
}

function renderConfigForm() {
  const container = document.getElementById("config-form");
  if (!container || !configData || typeof configData !== "object") return;
  const TOP_ORDER = ["connection", "admin_qq", "main_group", "debug", "sync", "whitelist", "regex_engine", "webui", "background", "language", "pip", "commands", "marketplace", "updates"];
  const ordered = {};
  for (const k of TOP_ORDER) {
    if (k in configData) ordered[k] = configData[k];
  }
  for (const [k, v] of Object.entries(configData)) {
    if (!(k in ordered)) ordered[k] = v;
  }
  const walk = (obj, prefix) => {
    let html = "";
    for (const [key, val] of Object.entries(obj)) {
      const path = prefix ? prefix + "." + key : key;
      if (val !== null && typeof val === "object" && !Array.isArray(val)) {
        const sectionId = prefix ? "" : ` id="config-section-${esc(path)}"`;
        html += `<div class="section"${sectionId}><h3>${esc(sectionTitleOf(path))}</h3>${walk(val, path)}</div>`;
      } else {
        const { label, desc } = labelOf(path, key);
        const descHtml = desc ? `<span class="desc">${esc(desc)}</span>` : "";
        let ctrl;
        if (path === "connection.ws_type") {
          const isForward = val === 0 || val === "0";
          ctrl = `<div class="ws-mode-selector" data-path="${path}">
            <div class="ws-mode-card ${isForward ? "selected" : ""}" data-value="0" onclick="selectWsMode(this)">
              <div class="ws-mode-icon">→</div>
              <div class="ws-mode-info">
                <div class="ws-mode-title">${esc(t("config.ws_mode_forward"))}</div>
                <div class="ws-mode-desc">${esc(t("config.ws_mode_forward_desc"))}</div>
              </div>
            </div>
            <div class="ws-mode-card ${!isForward ? "selected" : ""}" data-value="1" onclick="selectWsMode(this)">
              <div class="ws-mode-icon">←</div>
              <div class="ws-mode-info">
                <div class="ws-mode-title">${esc(t("config.ws_mode_reverse"))}</div>
                <div class="ws-mode-desc">${esc(t("config.ws_mode_reverse_desc"))}</div>
              </div>
            </div>
          </div>`;
        } else if (path === "language") {
          const langOptions = [
            { value: "auto", label: t("config.language_options.auto") },
            { value: "en", label: t("config.language_options.en") },
            { value: "zh_CN", label: t("config.language_options.zh_CN") },
            { value: "zh_TW", label: t("config.language_options.zh_TW") },
          ];
          ctrl = `<div class="ctrl">${buildSelect("cf-" + path, langOptions, String(val), t("config.select_language"))}</div>`;
        } else if (typeof val === "boolean") {
          ctrl = `<div class="switch"><input type="checkbox" id="cf-${path}" ${val ? "checked" : ""}>
                  <label class="track" for="cf-${path}"></label></div>`;
        } else if (typeof val === "number" && path !== "main_group") {
          ctrl = `<div class="ctrl"><input type="number" id="cf-${path}" value="${esc(val)}"></div>`;
        } else if (Array.isArray(val)) {
          ctrl = `<div class="ctrl"><input type="text" id="cf-${path}" value="${esc(val.join(", "))}" data-array="1"></div>`;
        } else {
          ctrl = `<div class="ctrl"><input type="text" id="cf-${path}" value="${esc(val)}"></div>`;
        }
        let rowHtml = `<div class="form-row"><label>${esc(label)}${descHtml}</label>${ctrl}</div>`;
        const hasSection = !prefix && configLabels[key] && configLabels[key]._;
        if (hasSection && val !== null && typeof val !== "object") {
          html += `<div class="section" id="config-section-${esc(key)}"><h3>${esc(sectionTitleOf(key))}</h3>${rowHtml}</div>`;
        } else {
          html += rowHtml;
        }
      }
    }
    return html;
  };
  container.innerHTML = walk(ordered, "");
  initConfigNavSpy();
}

function selectWsMode(card) {
  const selector = card.closest(".ws-mode-selector");
  if (!selector) return;
  selector.querySelectorAll(".ws-mode-card").forEach((c) => c.classList.remove("selected"));
  card.classList.add("selected");
}

function collectConfigForm() {
  const result = JSON.parse(JSON.stringify(configData));
  const walk = (obj, prefix) => {
    for (const [key, val] of Object.entries(obj)) {
      const path = prefix ? prefix + "." + key : key;
      if (val !== null && typeof val === "object" && !Array.isArray(val)) { walk(val, path); continue; }
      if (path === "connection.ws_type") {
        const selected = document.querySelector('.ws-mode-selector[data-path="connection.ws_type"] .ws-mode-card.selected');
        if (selected) { obj[key] = Number(selected.dataset.value); }
        continue;
      }
      const el = document.getElementById("cf-" + path);
      if (!el) continue;
      if (typeof val === "boolean") obj[key] = el.checked;
      else if (typeof val === "number" && path !== "main_group") obj[key] = Number(el.value) || 0;
      else if (Array.isArray(val) || (path === "main_group" && el.value.includes(",")))
        obj[key] = el.value.split(",").map((s) => s.trim()).filter(Boolean)
          .map((s) => (/^\d+$/.test(s) ? Number(s) : s));
      else if (path === "main_group" && typeof val === "number") obj[key] = Number(el.value) || 0;
      else obj[key] = el.value;
    }
  };
  walk(result, "");
  return result;
}

async function saveConfig() {
  try {
    const oldConfig = configData ? JSON.parse(JSON.stringify(configData)) : null;
    let body;
    if (configMode === "form") {
      if (!configData || typeof configData !== "object") {
        toast(t("config.no_data"), true);
        return;
      }
      body = collectConfigForm();
    } else {
      body = JSON.parse(document.getElementById("config-json").value);
    }
    const res = await api("POST", "/api/config", body);
    toast(res.msg || t("config.save_success"));
    if (oldConfig && configNeedsReload(oldConfig, body)) {
      showReloadPrompt("reload_prompt.message_config", "");
    }
    loadConfig();
    loadBackground();
  } catch (e) { toast(t("config.save_failed", { error: e.message }), true); }
}

function setConfigMode(mode) {
  configMode = mode;
  document.getElementById("seg-form").classList.toggle("active", mode === "form");
  document.getElementById("seg-json").classList.toggle("active", mode === "json");
  document.getElementById("config-form").style.display = mode === "form" ? "block" : "none";
  document.getElementById("config-json-wrap").style.display = mode === "json" ? "block" : "none";
  const configNav = document.getElementById("config-nav");
  if (configNav) configNav.style.display = mode === "form" ? "flex" : "none";
  if (mode === "json") {
    const ta = document.getElementById("config-json");
    const code = document.getElementById("config-code");
    if (ta && code) code.innerHTML = highlightCode(ta.value, "json");
    if (!configEditorBound) {
      bindCodeEditor(
        "config-code-host", "config-code", "config-json", "json",
        () => document.getElementById("config-json").value,
        () => {},
      );
      configEditorBound = true;
    }
  }
  if (mode === "form") {
    try {
      const ta = document.getElementById("config-json");
      if (ta && ta.value.trim()) {
        const parsed = JSON.parse(ta.value);
        if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
          configData = parsed;
          renderConfigForm();
        }
      }
    } catch (e) {
      // JSON 解析失败时保留旧 configData，表单仍显示旧数据，用户切回 JSON 可继续修复
      toast(t("config.json_parse_error"), true);
    }
  }
}


function highlightCode(text, language) {
  if (typeof Prism === "undefined" || !Prism.languages[language]) {
    return esc(text);
  }
  try {
    return Prism.highlight(text, Prism.languages[language], language);
  } catch (e) {
    return esc(text);
  }
}

function detectLanguage(filename) {
  const ext = (filename || "").split(".").pop().toLowerCase();
  if (ext === "json") return "json";
  if (ext === "py") return "python";
  if (ext === "js") return "javascript";
  if (ext === "yml" || ext === "yaml") return "yaml";
  if (ext === "html" || ext === "htm" || ext === "xml" || ext === "svg") return "markup";
  if (ext === "css") return "css";
  return "none";
}

function bindCodeEditor(hostId, codeId, textareaId, language, getValue, setValue) {
  const host = document.getElementById(hostId);
  const code = document.getElementById(codeId);
  const ta = document.getElementById(textareaId);
  if (!host || !code || !ta) return;
  host.classList.add("editable");
  ta.style.display = "block";
  code.className = "language-" + language;

  const sync = () => {
    code.innerHTML = highlightCode(getValue(), language);
  };
  ta.value = getValue();
  sync();

  // 防抖高亮：输入时立即同步 value，但高亮重绘延迟 200ms 合并，
  // 避免每次按键都全量跑 Prism 正则 + innerHTML 替换导致输入卡顿。
  // timer 挂在宿主元素上，重绑定时 openFileEditor 可清理旧任务避免竞态
  host._lumenHlTimer = null;
  let rafScheduled = false;
  const scheduleHighlight = () => {
    if (host._lumenHlTimer) clearTimeout(host._lumenHlTimer);
    if (rafScheduled) return;
    rafScheduled = true;
    requestAnimationFrame(() => {
      rafScheduled = false;
      host._lumenHlTimer = setTimeout(() => {
        host._lumenHlTimer = null;
        sync();
      }, 200);
    });
  };

  ta.addEventListener("input", () => {
    setValue(ta.value);
    scheduleHighlight();
  });
  ta.addEventListener("scroll", () => {
    const pre = host.querySelector("pre");
    if (pre) {
      pre.scrollTop = ta.scrollTop;
      pre.scrollLeft = ta.scrollLeft;
    }
  });
  return { refresh: sync };
}


/* ─── 子插件配置面板（AstrBot 配置页风格复刻） ─── */
/* 布局：左信息右控件的两列行式 + 分组嵌套卡片；特性：搜索过滤、单项恢复默认、
   secret 密码遮罩、数字滑块、chips 列表编辑器（批量导入）、多选复选框组、
   textarea 全屏编辑、未保存拦截、Ctrl/Cmd+S 快捷保存 */
let pcDirty = false;            // 未保存更改标记
let pcInitialValues = null;     // 打开配置时的初始值快照（key → value），用于还原检测
let pcEditorSourceId = "";      // 全屏编辑器回写目标控件 id

const PC_RESTORE_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/><path d="M3 3v5h5"/></svg>';
const PC_EYE_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>';
const PC_EYE_OFF_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94"/><path d="M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19"/><path d="M14.12 14.12a3 3 0 1 1-4.24-4.24"/><line x1="1" y1="1" x2="23" y2="23"/></svg>';
const PC_EXPAND_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M8 3H5a2 2 0 0 0-2 2v3m18 0V5a2 2 0 0 0-2-2h-3m0 18h3a2 2 0 0 0 2-2v-3M3 16v3a2 2 0 0 0 2 2h3"/></svg>';
const PC_SECTION_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>';

function renderPluginConfig(schema) {
  const box = document.getElementById("plugin-config-body");
  const items = Array.isArray(schema && schema.items) ? schema.items : [];
  const countEl = document.getElementById("plugin-config-count");
  const fields = items.filter((it) => it.type !== "section");
  if (countEl) countEl.textContent = String(fields.length);
  if (!items.length) {
    box.innerHTML = '<div class="empty-state">' + esc(t("subplugins.config_empty")) + '</div>';
    return;
  }
  // 分组渲染：section 标记开启分组卡片，后续字段归属该组
  let html = "";
  let open = false;
  items.forEach((item, index) => {
    if (item.type === "section") {
      if (open) html += "</div></div>";
      html += `<div class="pc-section"><div class="pc-section-title">${PC_SECTION_SVG}<span>${esc(item.title || item.label || "")}</span></div>` +
        (item.desc ? `<div class="pc-section-desc">${esc(item.desc)}</div>` : "") + `<div class="pc-section-body">`;
      open = true;
    } else {
      html += pcRenderRow(item, index);
    }
  });
  if (open) html += "</div></div>";
  box.innerHTML = html;
  // 数组字段渲染 chips
  items.forEach((item, index) => {
    if (item.type === "array") pcRenderArrayChips(index);
  });
  // 初始值快照：脏检测改为「当前值 vs 快照」对比，改回去（4→8→4）能正确清除标记
  pcInitialValues = pcSnapshotValues();
  // 事件委托：任意输入后按值对比刷新脏标记 + 恢复默认按钮可见性
  box.oninput = () => { pcRefreshDirty(); pcUpdateRestoreButtons(); };
  box.onchange = () => { pcRefreshDirty(); pcUpdateRestoreButtons(); };
  pcSetDirty(false);
  pcUpdateRestoreButtons();
}

/* 收集全部控件当前值（section/file 跳过）作为快照 */
function pcSnapshotValues() {
  const schema = editingPluginSchema;
  const out = {};
  if (!schema || !Array.isArray(schema.items)) return out;
  schema.items.forEach((item, index) => {
    if (item.type === "file" || item.type === "section") return;
    const v = pcControlValue(item, index);
    if (v !== undefined) out[item.key] = JSON.stringify(v);
  });
  return out;
}

/* 按值对比刷新脏标记：有任一字段 ≠ 初始快照，或存在待上传文件 → 脏 */
function pcRefreshDirty() {
  if (!pcInitialValues) return;
  const cur = pcSnapshotValues();
  let dirty = Object.keys(editingPluginPendingFiles || {}).length > 0;
  for (const k in pcInitialValues) {
    if (pcInitialValues[k] !== cur[k]) { dirty = true; break; }
  }
  pcSetDirty(dirty);
}

function pcRenderRow(item, index) {
  const id = `plugin-config-${index}`;
  const label = item.label || item.key;
  const searchText = `${label} ${item.key} ${item.desc || ""}`.toLowerCase();
  const keyTag = item.show_key ? `<span class="pc-key">(${esc(item.key)})</span>` : "";
  const obvious = item.obvious_hint ? `<span class="pc-obvious" title="${esc(t("subplugins.config_obvious_hint"))}">‼️</span>` : "";
  let ctrl = "";
  if (item.type === "switch") {
    ctrl = `<div class="switch"><input type="checkbox" id="${id}" ${item.val ? "checked" : ""}>` +
      `<label class="track" for="${id}"></label></div>`;
  } else if (item.type === "number") {
    if (item.min != null && item.max != null) {
      // 滑块 + 数字框联动（AstrBot slider 风格）
      ctrl = `<div class="pc-slider">` +
        `<input type="range" id="${id}-range" min="${esc(item.min)}" max="${esc(item.max)}" step="${esc(item.step || 1)}" value="${esc(item.val)}" oninput="pcSliderSync(this)">` +
        `<input type="number" id="${id}" value="${esc(item.val)}" onchange="pcSliderSync(this)">` +
        `</div>`;
    } else {
      ctrl = `<input type="number" id="${id}" value="${esc(item.val)}">`;
    }
  } else if (item.type === "select") {
    /* select 字段使用自定义下拉组件 .lumen-select 替代原生 <select>。
       options 已被后端 configform.py 规范化为 [{value, label}]；
       兼容旧 schema 里可能残留的纯值列表 [1, 2, 3]。 */
    const rawOpts = Array.isArray(item.options) ? item.options : [];
    const opts = rawOpts.map((o) => {
      if (o !== null && typeof o === "object" && "value" in o) {
        return { value: String(o.value), label: String(o.label != null ? o.label : o.value) };
      }
      return { value: String(o), label: String(o) };
    });
    ctrl = buildSelect(id, opts, String(item.val), t("common.search"));
  } else if (item.type === "multiselect") {
    // 多选复选框组：data-value 存 JSON 序列化的原始值（保留类型）
    const opts = Array.isArray(item.options) ? item.options : [];
    ctrl = `<div class="pc-multi" id="${id}">` + opts.map((o) => {
      const v = o !== null && typeof o === "object" && "value" in o ? o.value : o;
      const lb = o !== null && typeof o === "object" && "label" in o ? o.label : String(o);
      const checked = Array.isArray(item.val) && item.val.some((x) => String(x) === String(v));
      return `<label class="pc-multi-item"><input type="checkbox" data-value="${esc(JSON.stringify(v))}" ${checked ? "checked" : ""}>${esc(String(lb))}</label>`;
    }).join("") + `</div>`;
  } else if (item.type === "array") {
    // chips 列表编辑器：逐项增删 + 批量导入（按行解析）
    const val = Array.isArray(item.val) ? item.val : [];
    ctrl = `<div class="pc-array">` +
      `<div class="pc-array-chips" id="${id}-chips"></div>` +
      `<div class="pc-array-add">` +
      `<input type="text" id="${id}-add" placeholder="${esc(t("subplugins.config_array_add_ph"))}" onkeydown="if(event.key==='Enter'){event.preventDefault();pcArrayAdd(${index})}">` +
      `<button class="btn small ghost" onclick="pcArrayAdd(${index})">＋</button>` +
      `<button class="btn small ghost" onclick="pcArrayBatchToggle(${index})">${esc(t("subplugins.config_batch_import"))}</button>` +
      `</div>` +
      `<div class="pc-array-batch-wrap" id="${id}-batch-wrap">` +
      `<textarea id="${id}-batch" class="pc-array-batch" placeholder="${esc(t("subplugins.config_batch_ph"))}"></textarea>` +
      `<div style="margin-top:8px;display:flex;gap:8px">` +
      `<button type="button" class="btn small" onclick="pcArrayBatchApply(${index})">${esc(t("subplugins.config_batch_apply"))}</button>` +
      `</div></div>` +
      `<input type="hidden" id="${id}" value="${esc(JSON.stringify(val))}">` +
      `</div>`;
  } else if (item.type === "textarea") {
    ctrl = `<div class="pc-textarea-wrap">` +
      `<textarea id="${id}" rows="4">${esc(item.val)}</textarea>` +
      `<button type="button" class="pc-fullscreen-btn" onclick="pcOpenEditor(${index})" title="${esc(t("subplugins.config_editor_title"))}">${PC_EXPAND_SVG}</button>` +
      `</div>`;
  } else if (item.type === "file") {
    const accept = esc(item.accept || "image/*");
    const uploadUrl = esc(item.upload_url || "");
    const key = esc(item.key);
    const hasVal = !!(item.val && String(item.val).trim());
    const hint = hasVal ? esc(String(item.val)) : "点击选择图片上传";
    ctrl =
      `<div id="${id}-preview" class="file-upload-preview" ` +
      `style="min-height:56px;display:flex;align-items:center;justify-content:center;gap:8px;padding:10px 14px;` +
      `border:1px dashed var(--divider);border-radius:var(--radius-sm);background:var(--input-bg);cursor:pointer;` +
      `font-size:.85rem;color:var(--muted);transition:border-color .18s,background .18s" ` +
      `onclick="document.getElementById('${id}').click()" ` +
      `onmouseover="this.style.borderColor='var(--accent)'" ` +
      `onmouseout="this.style.borderColor='var(--divider)'">` +
      `<span class="file-upload-hint">${hint}</span></div>` +
      `<input type="file" id="${id}" accept="${accept}" data-upload-url="${uploadUrl}" data-key="${key}" ` +
      `data-preview-id="${id}-preview" style="display:none">`;
  } else {
    // text：secret=True 渲染为密码框 + 明文切换
    const inputType = item.secret ? "password" : "text";
    if (item.secret) {
      ctrl = `<div class="pc-secret">` +
        `<input type="${inputType}" id="${id}" value="${esc(item.val)}" autocomplete="new-password">` +
        `<button type="button" class="pc-eye" onclick="pcToggleSecret(this)" title="${esc(t("subplugins.config_secret_toggle"))}">${PC_EYE_SVG}</button>` +
        `</div>`;
    } else {
      ctrl = `<input type="${inputType}" id="${id}" value="${esc(item.val)}">`;
    }
  }
  const restore = `<button type="button" class="pc-restore" id="${id}-restore" onclick="pcRestoreDefault(${index})" title="${esc(t("subplugins.config_restore_default"))}">${PC_RESTORE_SVG}</button>`;
  return `<div class="pc-row" data-search="${esc(searchText)}" data-pc-index="${index}">` +
    `<div class="pc-info"><div class="pc-name">${esc(label)}${keyTag}${obvious}</div>` +
    (item.desc ? `<div class="pc-hint">${esc(item.desc)}</div>` : "") + `</div>` +
    `<div class="pc-ctrl"><div class="pc-ctrl-inner">${ctrl}</div>${restore}</div>` +
    `</div>`;
}

/* 按 schema 类型读取控件当前值（保留原始类型：select/multiselect 映射回 option 原值） */
function pcControlValue(item, index) {
  const el = document.getElementById(`plugin-config-${index}`);
  if (!el) return undefined;
  if (item.type === "switch") return el.checked;
  if (item.type === "number") return Number(el.value) || 0;
  if (item.type === "select") {
    const raw = el.value;
    const opts = Array.isArray(item.options) ? item.options : [];
    const found = opts.find((o) =>
      o !== null && typeof o === "object" ? String(o.value) === String(raw) : String(o) === String(raw));
    if (found === undefined) return raw;
    return typeof found === "object" ? found.value : found;
  }
  if (item.type === "multiselect") {
    return Array.from(el.querySelectorAll("input:checked")).map((c) => {
      try { return JSON.parse(c.dataset.value); } catch (e) { return c.dataset.value; }
    });
  }
  if (item.type === "array") return pcArrayGet(index);
  return el.value;
}

/* 按 schema 类型把控件值设为指定值（用于恢复默认） */
function pcSetControlValue(item, index, value) {
  const el = document.getElementById(`plugin-config-${index}`);
  if (!el) return;
  if (item.type === "switch") {
    el.checked = !!value;
  } else if (item.type === "number") {
    el.value = value;
    const range = document.getElementById(`plugin-config-${index}-range`);
    if (range) range.value = value;
  } else if (item.type === "select") {
    const host = el.closest(".lumen-select");
    if (host) {
      const opts = Array.isArray(item.options) ? item.options : [];
      const found = opts.find((o) =>
        o !== null && typeof o === "object" ? String(o.value) === String(value) : String(o) === String(value));
      const label = found === undefined ? String(value)
        : typeof found === "object" ? String(found.label != null ? found.label : found.value) : String(found);
      selectOption(host, String(value), label);
    }
  } else if (item.type === "multiselect") {
    el.querySelectorAll("input").forEach((c) => {
      let v;
      try { v = JSON.parse(c.dataset.value); } catch (e) { v = c.dataset.value; }
      c.checked = Array.isArray(value) && value.some((x) => String(x) === String(v));
    });
  } else if (item.type === "array") {
    pcArraySet(index, Array.isArray(value) ? value : []);
  } else {
    el.value = value == null ? "" : String(value);
  }
}

function pcValueEquals(a, b) {
  return JSON.stringify(a) === JSON.stringify(b);
}

/* 刷新全部"恢复默认"按钮可见性：当前值 ≠ 默认值时显示 */
function pcUpdateRestoreButtons() {
  const schema = editingPluginSchema;
  if (!schema || !Array.isArray(schema.items)) return;
  schema.items.forEach((item, index) => {
    if (item.type === "section" || item.type === "file" || item.default === undefined) return;
    const btn = document.getElementById(`plugin-config-${index}-restore`);
    if (!btn) return;
    const cur = pcControlValue(item, index);
    btn.classList.toggle("show", cur !== undefined && !pcValueEquals(cur, item.default));
  });
}

function pcRestoreDefault(index) {
  const schema = editingPluginSchema;
  if (!schema || !schema.items[index]) return;
  const item = schema.items[index];
  pcSetControlValue(item, index, item.default);
  pcRefreshDirty();
  pcUpdateRestoreButtons();
}

async function pcResetAll() {
  const schema = editingPluginSchema;
  if (!schema || !Array.isArray(schema.items)) return;
  if (!await customConfirm(t("subplugins.config_reset_all_confirm"))) return;
  schema.items.forEach((item, index) => {
    if (item.type === "section" || item.type === "file" || item.default === undefined) return;
    pcSetControlValue(item, index, item.default);
  });
  pcRefreshDirty();
  pcUpdateRestoreButtons();
}

function pcSetDirty(dirty) {
  pcDirty = dirty;
  const badge = document.getElementById("pc-dirty-badge");
  if (badge) badge.style.display = dirty ? "" : "none";
}

/* 搜索过滤：按 key/label/desc 模糊匹配；无命中行的分组整组隐藏 */
function pcFilterItems() {
  const box = document.getElementById("plugin-config-body");
  const input = document.getElementById("pc-search");
  if (!box || !input) return;
  const q = (input.value || "").toLowerCase().trim();
  box.querySelectorAll(".pc-row").forEach((row) => {
    row.style.display = !q || (row.dataset.search || "").includes(q) ? "" : "none";
  });
  box.querySelectorAll(".pc-section").forEach((sec) => {
    const visible = Array.from(sec.querySelectorAll(".pc-row")).some((r) => r.style.display !== "none");
    sec.style.display = visible ? "" : "none";
  });
  const any = Array.from(box.querySelectorAll(".pc-row")).some((r) => r.style.display !== "none");
  let emptyEl = box.querySelector(".pc-no-results");
  if (q && !any) {
    if (!emptyEl) {
      emptyEl = document.createElement("div");
      emptyEl.className = "empty-state pc-no-results";
      box.appendChild(emptyEl);
    }
    emptyEl.textContent = t("subplugins.config_no_results");
  } else if (emptyEl) {
    emptyEl.remove();
  }
}

/* 带未保存拦截的关闭 */
async function pcRequestClose() {
  if (pcDirty && !await customConfirm(t("subplugins.config_close_confirm"))) return;
  closeModal("plugin-config-modal");
}

function pcMaskClick(ev) {
  if (ev.target === ev.currentTarget) pcRequestClose();
}

/* secret 明文/密文切换 */
function pcToggleSecret(btn) {
  const input = btn.parentElement.querySelector("input");
  if (!input) return;
  const show = input.type === "password";
  input.type = show ? "text" : "password";
  btn.innerHTML = show ? PC_EYE_OFF_SVG : PC_EYE_SVG;
}

/* 滑块 ↔ 数字框联动 */
function pcSliderSync(el) {
  const index = el.id.replace("plugin-config-", "").replace("-range", "");
  const num = document.getElementById(`plugin-config-${index}`);
  const range = document.getElementById(`plugin-config-${index}-range`);
  if (el.type === "range" && num) num.value = el.value;
  if (el.type === "number" && range) range.value = el.value;
}

/* 数组字段：chips 读写 */
function pcArrayGet(index) {
  const el = document.getElementById(`plugin-config-${index}`);
  if (!el) return [];
  try { return JSON.parse(el.value || "[]"); } catch (e) { return []; }
}

function pcArraySet(index, arr) {
  const el = document.getElementById(`plugin-config-${index}`);
  if (el) el.value = JSON.stringify(arr);
  pcRenderArrayChips(index);
  pcRefreshDirty();
  pcUpdateRestoreButtons();
}

function pcRenderArrayChips(index) {
  const chipsEl = document.getElementById(`plugin-config-${index}-chips`);
  if (!chipsEl) return;
  const arr = pcArrayGet(index);
  chipsEl.innerHTML = arr.map((v, i) =>
    `<span class="pc-array-chip"><span title="${esc(String(v))}">${esc(String(v))}</span>` +
    `<button type="button" onclick="pcArrayRemove(${index},${i})">✕</button></span>`
  ).join("");
}

function pcArrayAdd(index) {
  const input = document.getElementById(`plugin-config-${index}-add`);
  if (!input) return;
  const v = (input.value || "").trim();
  if (!v) return;
  const arr = pcArrayGet(index);
  if (!arr.some((x) => String(x) === v)) arr.push(v);
  input.value = "";
  pcArraySet(index, arr);
}

function pcArrayRemove(index, i) {
  const arr = pcArrayGet(index);
  arr.splice(i, 1);
  pcArraySet(index, arr);
}

function pcArrayBatchToggle(index) {
  const wrap = document.getElementById(`plugin-config-${index}-batch-wrap`);
  if (wrap) {
    wrap.classList.toggle("show");
    const ta = document.getElementById(`plugin-config-${index}-batch`);
    if (wrap.classList.contains("show") && ta) ta.focus();
  }
}

function pcArrayBatchApply(index) {
  const ta = document.getElementById(`plugin-config-${index}-batch`);
  const wrap = document.getElementById(`plugin-config-${index}-batch-wrap`);
  if (!ta) return;
  const lines = (ta.value || "").split("\n").map((s) => s.trim()).filter(Boolean);
  if (wrap) wrap.classList.remove("show");
  if (!lines.length) return;
  const arr = pcArrayGet(index);
  lines.forEach((ln) => { if (!arr.some((x) => String(x) === ln)) arr.push(ln); });
  ta.value = "";
  pcArraySet(index, arr);
}

/* textarea 全屏编辑 */
function pcOpenEditor(index) {
  const schema = editingPluginSchema;
  const item = schema && schema.items ? schema.items[index] : null;
  const el = document.getElementById(`plugin-config-${index}`);
  if (!item || !el) return;
  pcEditorSourceId = `plugin-config-${index}`;
  document.getElementById("pc-editor-title").textContent =
    t("subplugins.config_editor_title") + " · " + (item.label || item.key);
  document.getElementById("pc-editor-textarea").value = el.value;
  document.getElementById("pc-editor-modal").classList.add("show");
}

function pcEditorApply() {
  const el = document.getElementById(pcEditorSourceId);
  if (el) el.value = document.getElementById("pc-editor-textarea").value;
  closeModal("pc-editor-modal");
  pcRefreshDirty();
  pcUpdateRestoreButtons();
}

/* Ctrl/Cmd+S 快捷保存（配置弹窗打开时） */
document.addEventListener("keydown", (e) => {
  if ((e.ctrlKey || e.metaKey) && (e.key === "s" || e.key === "S")) {
    const modal = document.getElementById("plugin-config-modal");
    if (modal && modal.classList.contains("show")) {
      e.preventDefault();
      savePluginConfig();
    }
  }
});

async function openPluginConfig(name) {
  editingPluginConfig = name;
  editingPluginSchema = null;
  editingPluginPendingFiles = {}; // 清空暂存文件
  pcInitialValues = null;         // 清空旧快照（加载失败时不误用上一个插件的快照）
  pcSetDirty(false);
  const searchEl = document.getElementById("pc-search");
  if (searchEl) searchEl.value = "";
  document.getElementById("plugin-config-title").textContent = name + t("subplugins.config_title_suffix");
  document.getElementById("plugin-config-body").innerHTML = '<div class="empty-state">' + esc(t("subplugins.config_loading")) + '</div>';
  document.getElementById("plugin-config-modal").classList.add("show");
  try {
    const { data: schema } = await api("GET", "/api/plugins/config/" + encodeURIComponent(name));
    if (editingPluginConfig !== name) return;
    editingPluginSchema = schema;
    renderPluginConfig(schema);
  } catch (e) {
    document.getElementById("plugin-config-body").innerHTML =
      `<div class="empty-state error">${esc(t("subplugins.config_load_failed", { error: e.message }))}</div>`;
  }
}

async function savePluginConfig() {
  const name = editingPluginConfig;
  const schema = editingPluginSchema;
  if (!name || !schema || !Array.isArray(schema.items)) return;
  try {
    // 1) 先保存普通配置字段（file/section 类型跳过，file 由独立端点处理）
    const body = {};
    schema.items.forEach((item, index) => {
      if (item.type === "file" || item.type === "section") return;
      const value = pcControlValue(item, index);
      if (value !== undefined) body[item.key] = value;
    });
    const res = await api("POST", "/api/plugins/config/" + encodeURIComponent(name), body);

    // 2) 上传暂存的文件（用户点了保存才真正提交，取消则丢弃）
    const pendingKeys = Object.keys(editingPluginPendingFiles);
    if (pendingKeys.length) {
      toast(t("subplugins.uploading_files") || "正在上传文件…");
      for (const itemKey of pendingKeys) {
        const file = editingPluginPendingFiles[itemKey];
        // 从 schema 找到该 key 对应的 upload_url
        const fileItem = schema.items.find((it) => it.key === itemKey && it.type === "file");
        const uploadUrl = fileItem ? fileItem.upload_url : "";
        if (!uploadUrl) continue;
        const form = new FormData();
        form.append("file", file);
        const token = localStorage.getItem("lumen_token") || "";
        const resp = await fetch(uploadUrl, {
          method: "POST",
          headers: token ? { Authorization: "Bearer " + token } : {},
          body: form,
        });
        const data = await resp.json();
        if (data.code !== 200 || (data.data && data.data.ok === false)) {
          const msg = (data.data && data.data.msg) || data.msg || "文件上传失败";
          toast(msg, true);
          return; // 上传失败不关闭弹窗，让用户重试
        }
      }
      editingPluginPendingFiles = {};
    }

    toast(res.msg || t("subplugins.config_save_success"));
    pcSetDirty(false);
    closeModal("plugin-config-modal");
  } catch (e) { toast(t("subplugins.config_save_failed", { error: e.message }), true); }
}

// 文件配置项：选择文件后只做本地预览，暂存 File 对象，等保存按钮才上传
function handlePluginConfigFileChange(inputEl) {
  const file = inputEl.files && inputEl.files[0];
  if (!file) return;
  const previewEl = document.getElementById(inputEl.dataset.previewId);
  const key = inputEl.dataset.key;
  // 暂存 File 对象：点保存才上传，点取消则丢弃
  editingPluginPendingFiles[key] = file;
  pcRefreshDirty(); // 选择文件即视为有未保存更改
  // 本地预览（不上传）
  if (previewEl) {
    if (file.type.startsWith("image/")) {
      const url = URL.createObjectURL(file);
      previewEl.innerHTML = `<img src="${url}" style="max-width:100%;max-height:80px;border-radius:6px;object-fit:contain">`;
    } else {
      previewEl.innerHTML = `<span class="file-upload-hint">${esc(file.name)}</span>`;
    }
  }
  inputEl.value = ""; // 重置以允许重复选择同一文件
}

// 绑定 file input 的 change 事件（使用事件委托）
document.addEventListener("change", (e) => {
  if (e.target && e.target.type === "file" && e.target.dataset.uploadUrl !== undefined && e.target.dataset.key) {
    handlePluginConfigFileChange(e.target);
  }
});


const RULE_EVENT_TYPES = [
  { value: "group.member_join", labelKey: "rules.event_types.group_join" },
  { value: "group.member_leave", labelKey: "rules.event_types.group_leave" },
  { value: "server.player_join", labelKey: "rules.event_types.player_join" },
  { value: "server.player_left", labelKey: "rules.event_types.player_left" },
  { value: "server.player_chat", labelKey: "rules.event_types.player_chat" },
];
const RULE_ACTION_TYPES = [
  { value: "replyText", labelKey: "rules.action_types.reply_text" },
  { value: "replyAtText", labelKey: "rules.action_types.reply_at_text" },
  { value: "replyImage", labelKey: "rules.action_types.reply_image" },
  { value: "deleteMessage", labelKey: "rules.action_types.recall" },
  { value: "muteUser", labelKey: "rules.action_types.mute" },
  { value: "executeCommand", labelKey: "rules.action_types.runcmd" },
  { value: "callPluginCommand", labelKey: "rules.action_types.plugin" },
];
/* 每种动作的说明与参数输入提示：小白不看文档也能明白参数填什么 */
const RULE_ACTION_META = {
  replyText: { descKey: "rules.action_descs.reply_text", phKey: "rules.action_params_placeholders.reply_text" },
  replyAtText: { descKey: "rules.action_descs.reply_at_text", phKey: "rules.action_params_placeholders.reply_at_text" },
  replyImage: { descKey: "rules.action_descs.reply_image", phKey: "rules.action_params_placeholders.reply_image" },
  deleteMessage: { descKey: "rules.action_descs.delete_message", phKey: "rules.action_params_placeholders.delete_message" },
  muteUser: { descKey: "rules.action_descs.mute_user", phKey: "rules.action_params_placeholders.mute_user" },
  executeCommand: { descKey: "rules.action_descs.execute_command", phKey: "rules.action_params_placeholders.execute_command" },
  callPluginCommand: { descKey: "rules.action_descs.call_plugin_command", phKey: "rules.action_params_placeholders.call_plugin_command" },
};
/* userRole 条件的可选身份（后端比较值） */
const RULE_ROLE_OPTIONS = [
  { value: "owner", labelKey: "rules.condition_roles.owner" },
  { value: "admin", labelKey: "rules.condition_roles.admin" },
  { value: "member", labelKey: "rules.condition_roles.member" },
];
/* 各条件字段的一行说明 + 比较值输入提示 */
const RULE_CONDITION_META = {
  userRole: { hintKey: "rules.condition_field_hints.user_role", phKey: "rules.condition_value_placeholders.user_role" },
  userId: { hintKey: "rules.condition_field_hints.user_id", phKey: "rules.condition_value_placeholders.user_id" },
  groupId: { hintKey: "rules.condition_field_hints.group_id", phKey: "rules.condition_value_placeholders.group_id" },
};
const RULE_CONDITION_FIELDS = [
  { value: "userRole", labelKey: "rules.condition_fields.user_id" },
  { value: "userId", labelKey: "rules.condition_fields.qq" },
  { value: "groupId", labelKey: "rules.condition_fields.group_id" },
];
const RULE_CONDITION_OPERATORS = [
  { value: "==", labelKey: "rules.condition_operators.eq" },
  { value: "!=", labelKey: "rules.condition_operators.ne" },
  { value: "includes", labelKey: "rules.condition_operators.contains" },
  { value: "matches", labelKey: "rules.condition_operators.regex" },
];
const RULE_PATTERN_PRESETS = [
  { labelKey: "rules.pattern_presets.exact_query", value: "^查服$" },
  { labelKey: "rules.pattern_presets.exact_word", value: "^关键词$" },
  { labelKey: "rules.pattern_presets.contains_word", value: "关键词" },
  { labelKey: "rules.pattern_presets.starts_with", value: "^你好" },
  { labelKey: "rules.pattern_presets.bind_whitelist", value: "^绑定白名单(.+)$" },
  { labelKey: "rules.pattern_presets.number_cmd", value: "^#(\\d+)$" },
];
/* 完整规则模板：选择后自动填入整条规则（触发方式/关键词/条件/动作），
   文案（规则名/动作参数）经 i18n key 渲染，随界面语言变化 */
const RULE_TEMPLATES = [
  { value: "keyword_reply", labelKey: "rules.templates.keyword_reply" },
  { value: "welcome", labelKey: "rules.templates.welcome" },
  { value: "leave_notice", labelKey: "rules.templates.leave_notice" },
  { value: "player_join", labelKey: "rules.templates.player_join" },
  { value: "admin_cmd", labelKey: "rules.templates.admin_cmd" },
  { value: "server_status", labelKey: "rules.templates.server_status" },
  { value: "banned_word", labelKey: "rules.templates.banned_word" },
];
const RULE_TEMPLATE_DATA = {
  keyword_reply: {
    nameKey: "rules.templates.keyword_reply",
    triggerType: "message", pattern: "^你好$", flags: "i", eventType: "",
    conditions: [],
    actions: [{ type: "replyText", paramsKey: "rules.templates.keyword_reply_params" }],
    block: true,
  },
  welcome: {
    nameKey: "rules.templates.welcome",
    triggerType: "event", pattern: "", flags: "", eventType: "group.member_join",
    conditions: [],
    actions: [{ type: "replyAtText", paramsKey: "rules.templates.welcome_params" }],
    block: false,
  },
  leave_notice: {
    nameKey: "rules.templates.leave_notice",
    triggerType: "event", pattern: "", flags: "", eventType: "group.member_leave",
    conditions: [],
    actions: [{ type: "replyText", paramsKey: "rules.templates.leave_notice_params" }],
    block: false,
  },
  player_join: {
    nameKey: "rules.templates.player_join",
    triggerType: "event", pattern: "", flags: "", eventType: "server.player_join",
    conditions: [],
    actions: [{ type: "replyText", paramsKey: "rules.templates.player_join_params" }],
    block: false,
  },
  admin_cmd: {
    nameKey: "rules.templates.admin_cmd",
    triggerType: "message", pattern: "^执行(.+)$", flags: "", eventType: "",
    conditions: [{ field: "userRole", operator: "==", value: "admin" }],
    actions: [
      { type: "executeCommand", paramsKey: "rules.templates.admin_cmd_params" },
      { type: "replyText", paramsKey: "rules.templates.admin_cmd_reply_params" },
    ],
    block: true,
  },
  server_status: {
    nameKey: "rules.templates.server_status",
    triggerType: "message", pattern: "^查服$", flags: "i", eventType: "",
    conditions: [],
    actions: [
      { type: "executeCommand", params: "list" },
      { type: "replyText", paramsKey: "rules.templates.server_status_params" },
    ],
    block: true,
  },
  banned_word: {
    nameKey: "rules.templates.banned_word",
    triggerType: "message", pattern: "违禁词", flags: "i", eventType: "",
    conditions: [],
    actions: [
      { type: "deleteMessage", params: "" },
      { type: "muteUser", params: "600" },
      { type: "replyText", paramsKey: "rules.templates.banned_word_params" },
    ],
    block: true,
  },
};

function ruleOptions(arr) {
  return arr.map((e) => ({ value: e.value, label: t(e.labelKey) }));
}

let rulesEditorBound = false;

function buildSelect(id, options, current, placeholder) {
  const opts = options.map((o) => {
    const sel = o.value === current ? " selected" : "";
    const emptyCls = o.value === "" ? " empty" : "";
    return `<div class="ls-option${sel}${emptyCls}" data-value="${esc(o.value)}" data-label="${esc(o.label)}" onclick="onSelectOption(this)">${esc(o.label)}</div>`;
  }).join("");
  const cur = options.find((o) => o.value === current);
  const label = cur ? cur.label : (placeholder || t("common.search"));
  return `<div class="lumen-select" data-select="${id}">
    <div class="ls-trigger" onclick="toggleSelect(this.parentElement)">
      <span class="ls-label">${esc(label)}</span>
      <svg class="ls-arrow" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="m6 9 6 6 6-6"/></svg>
    </div>
    <div class="ls-list">${opts}</div>
    <input type="hidden" id="${esc(id)}" value="${esc(current)}">
  </div>`;
}

function onSelectOption(optEl) {
  const host = optEl.closest(".lumen-select");
  const id = host.getAttribute("data-select");
  const value = optEl.dataset.value;
  const label = optEl.dataset.label;
  selectOption(host, value, label);
  // 子插件配置弹窗内的下拉：隐藏 input 改值不发原生 change 事件，需手动刷新脏标记
  if (id && id.indexOf("plugin-config-") === 0) {
    pcRefreshDirty();
    pcUpdateRestoreButtons();
  }
  if (id === "rule-preset" && value) {
    const input = document.getElementById("rule-pattern");
    if (input) input.value = value;
    setTimeout(() => {
      host.querySelector(".ls-label").textContent = t("rules.form.template_placeholder");
      host.querySelector('input[type="hidden"]').value = "";
      host.querySelectorAll(".ls-option").forEach((o) =>
        o.classList.toggle("selected", o.dataset.value === ""));
    }, 0);
    return;
  }
  if (id === "rule-template" && value) {
    // 完整规则模板：自动填入整条规则（名称/触发方式/关键词/条件/动作）
    applyRuleTemplate(value);
    setTimeout(() => {
      host.querySelector(".ls-label").textContent = t("rules.form.template_row_placeholder");
      host.querySelector('input[type="hidden"]').value = "";
      host.querySelectorAll(".ls-option").forEach((o) =>
        o.classList.toggle("selected", o.dataset.value === ""));
    }, 0);
    return;
  }
  // 动作类型切换：说明文字与输入区随类型变化（如回复图片出现上传按钮）
  if (id && id.startsWith("rule-action-type-")) {
    rerenderEditorLists({ conditions: collectEditorConditions(), actions: collectEditorActions() });
    return;
  }
  // 条件字段切换：userRole 显示身份下拉，其他字段显示文本输入
  if (id && id.startsWith("rule-cond-field-")) {
    rerenderEditorLists({ conditions: collectEditorConditions(), actions: collectEditorActions() });
  }
}

function toggleSelect(host) {
  const isOpen = host.classList.contains("open");
  document.querySelectorAll(".lumen-select.open").forEach((el) => el.classList.remove("open"));
  if (!isOpen) host.classList.add("open");
}

function selectOption(host, value, label) {
  host.querySelector(".ls-label").textContent = label;
  host.querySelector('input[type="hidden"]').value = value;
  host.querySelectorAll(".ls-option").forEach((o) => {
    o.classList.toggle("selected", o.dataset.value === value);
    o.classList.toggle("empty", o.dataset.value === "" );
  });
  host.classList.remove("open");
}

document.addEventListener("click", (e) => {
  if (!e.target.closest(".lumen-select")) {
    document.querySelectorAll(".lumen-select.open").forEach((el) => el.classList.remove("open"));
  }
});

/* 事件委托：处理原本以 esc 字符串拼接进 onclick 的按钮，避免 XSS */
document.addEventListener("click", (e) => {
  /* 子插件 ⋯ 更多菜单项：先于其它处理（菜单在卡片外层，不命中卡片选择器） */
  const spMenuItem = e.target.closest(".sp-menu-item[data-action]");
  if (spMenuItem) {
    const action = spMenuItem.dataset.action || "";
    const name = spMenuItem.dataset.name || "";
    closeSpMoreMenu();
    dispatchSpAction(action, name);
    return;
  }
  /* ⋯ 按钮本身：切换菜单开合；点击后立即 return，避免下方"点外部关闭"逻辑误关 */
  const spMoreBtn = e.target.closest(".sp-more-btn[data-name]");
  if (spMoreBtn) {
    toggleSpMoreMenu(spMoreBtn);
    return;
  }
  /* 子插件描述「展开」按钮：滑出浮层显示全文（不撑大卡片） */
  const spDescBtn = e.target.closest(".sp-desc-expand[data-sp-name]");
  if (spDescBtn) {
    toggleSpDescPopover(spDescBtn);
    return;
  }
  /* 点击菜单与按钮之外的任意位置：收起菜单 */
  if (spMoreMenuState) closeSpMoreMenu();
  if (spDescPopoverState) closeSpDescPopover();
  const pipBtn = e.target.closest(".pip-uninstall-btn");
  if (pipBtn) { pipUninstall(pipBtn.dataset.name || ""); return; }
  const wlBtn = e.target.closest(".wl-unbind-btn");
  if (wlBtn) { unbind(wlBtn.dataset.qid || ""); return; }
  const fileBtn = e.target.closest(".file-edit-btn");
  if (fileBtn) { openFileEditor(fileBtn.dataset.path || ""); return; }
  const customNavBtn = e.target.closest(".custom-nav-btn");
  if (customNavBtn) {
    nav(customNavBtn.dataset.page || "",
      customNavBtn.dataset.customUrl || "",
      customNavBtn.dataset.customTitle || "");
    return;
  }
  /* 子插件注册到底栏的自定义页面 tab：与侧栏按钮同构（data-* 属性驱动） */
  const customTabBtn = e.target.closest(".custom-tab-btn");
  if (customTabBtn) {
    nav(customTabBtn.dataset.page || "",
      customTabBtn.dataset.customUrl || "",
      customTabBtn.dataset.customTitle || "");
    return;
  }
  const customMoreBtn = e.target.closest(".custom-more-btn");
  if (customMoreBtn) {
    closeMoreSheet();
    nav(customMoreBtn.dataset.page || "",
      customMoreBtn.dataset.customUrl || "",
      customMoreBtn.dataset.customTitle || "");
    return;
  }
  /* 适配器卡片/弹窗：data-* 属性替代 onclick 字符串拼接 */
  const adapterGearBtn = e.target.closest(".adapter-gear[data-id]");
  if (adapterGearBtn) { openEditAdapterModal(adapterGearBtn.dataset.id || ""); return; }
  const adapterQrBtn = e.target.closest(".ae-qr-btn[data-id]");
  if (adapterQrBtn) { openQrBindModal(adapterQrBtn.dataset.id || ""); return; }
  const adapterDelBtn = e.target.closest(".adapter-delete-btn[data-id]");
  if (adapterDelBtn) { deleteAdapter(adapterDelBtn.dataset.id || ""); return; }
  const secretEyeBtn = e.target.closest(".secret-eye[data-input-id]");
  if (secretEyeBtn) {
    toggleSecretReveal(secretEyeBtn,
      secretEyeBtn.dataset.inputId || "",
      secretEyeBtn.dataset.adapterId || "",
      secretEyeBtn.dataset.key || "");
    return;
  }
  /* 子插件卡片操作按钮：data-action + data-name 分发到原处理函数 */
  const spActionBtn = e.target.closest(".sp-action-btn[data-action]");
  if (spActionBtn) {
    dispatchSpAction(spActionBtn.dataset.action || "", spActionBtn.dataset.name || "");
    return;
  }
  /* 更新中心卡片：单独更新按钮 */
  const spuUpdateBtn = e.target.closest("[data-spu-update]");
  if (spuUpdateBtn) { updateSpuFromCard(spuUpdateBtn); return; }
  /* 市场卡片：安装按钮优先于卡片整体点击（原 stopPropagation 语义） */
  const marketInstallBtn = e.target.closest(".market-install-btn[data-id]");
  if (marketInstallBtn) { installMarketPlugin(marketInstallBtn.dataset.id || ""); return; }
  const marketVerBtn = e.target.closest(".market-ver-install-btn[data-id]");
  if (marketVerBtn) {
    installMarketPlugin(marketVerBtn.dataset.id || "", marketVerBtn.dataset.version || "");
    return;
  }
  /* 点赞按钮优先于卡片/弹窗整体点击 */
  const marketLikeBtn = e.target.closest(".market-like-btn[data-id]");
  if (marketLikeBtn) { toggleMarketLike(marketLikeBtn); return; }
  /* 市场更新弹窗：版本条目点击切换选中 */
  const marketVerItem = e.target.closest("#market-update-content .market-ver-item[data-version]");
  if (marketVerItem) { selectMarketVersion(marketVerItem.dataset.version || ""); return; }
  const marketCard = e.target.closest(".market-card[data-id]");
  if (marketCard) { openMarketDetail(marketCard.dataset.id || ""); return; }
});

/* 子插件启用开关：change 事件委托（原 onchange 拼接） */
document.addEventListener("change", (e) => {
  const spToggle = e.target.closest(".sp-toggle[data-name]");
  if (spToggle) toggleSubplugin(spToggle.dataset.name || "", spToggle.checked);
});

async function loadRules() {
  try {
    const { data } = await api("GET", "/api/rules");
    rulesData = Array.isArray(data) ? data : [];
    renderRulesList();
    syncRulesJson();
  } catch (e) { toast(t("rules.load_failed", { error: e.message }), true); }
}

function syncRulesJson() {
  const ta = document.getElementById("rules-json");
  if (!ta) return;
  const text = JSON.stringify(rulesData, null, 4);
  ta.value = text;
  const code = document.getElementById("rules-code");
  if (code) code.innerHTML = highlightCode(text, "json");
}

function setRulesMode(mode) {
  rulesMode = mode;
  document.getElementById("seg-rules-gui").classList.toggle("active", mode === "gui");
  document.getElementById("seg-rules-json").classList.toggle("active", mode === "json");
  document.getElementById("rules-gui-wrap").style.display = mode === "gui" ? "block" : "none";
  document.getElementById("rules-json-wrap").style.display = mode === "json" ? "block" : "none";
  if (mode === "json") {
    syncRulesJson();
    if (!rulesEditorBound) {
      bindCodeEditor(
        "rules-code-host", "rules-code", "rules-json", "json",
        () => document.getElementById("rules-json").value,
        () => {},
      );
      rulesEditorBound = true;
    }
  }
  if (mode === "gui") {
    try {
      const ta = document.getElementById("rules-json");
      if (ta && ta.value.trim()) {
        const parsed = JSON.parse(ta.value);
        if (Array.isArray(parsed)) {
          rulesData = parsed;
          renderRulesList();
        }
      }
    } catch (e) {
      // JSON 解析失败时保留旧 rulesData，GUI 仍显示旧数据，用户切回 JSON 可继续修复
      toast(t("rules.json_parse_error"), true);
    }
  }
}

async function saveRules() {
  try {
    let rules;
    if (rulesMode === "json") {
      rules = JSON.parse(document.getElementById("rules-json").value);
      if (!Array.isArray(rules)) throw new Error(t("rules.must_be_array"));
      rulesData = rules;
    } else {
      rules = rulesData;
    }
    const res = await api("POST", "/api/rules", rules);
    toast(res.msg || t("rules.save_success"));
    rulesData = Array.isArray(res.data) ? res.data : rules;
    renderRulesList();
    syncRulesJson();
  } catch (e) { toast(t("rules.save_failed", { error: e.message }), true); }
}


function renderRulesList() {
  const box = document.getElementById("rules-list");
  if (!box) return;
  if (!rulesData.length) {
    box.innerHTML = '<div class="empty-state">' + esc(t("rules.empty")) + '</div>';
    return;
  }
  box.innerHTML = rulesData.map((rule, idx) => {
    const enabled = rule.enabled !== false;
    const tag = enabled
      ? '<span class="tag green">' + esc(t("rules.status_enabled")) + '</span>'
      : '<span class="tag gray">' + esc(t("rules.status_disabled")) + '</span>';
    const isEvt = rule.triggerType === "event";
    const tt = isEvt ? t("rules.trigger_event") : t("rules.trigger_message");
    const patternText = rule.pattern
      ? `<code>${esc(rule.pattern)}</code>` + (rule.flags ? ` <span style="color:var(--muted);font-size:11px">/${rule.flags}/</span>` : "")
      : '<span style="color:var(--muted)">' + esc(t("rules.match_all")) + '</span>';
    const evtMatch = RULE_EVENT_TYPES.find((e) => e.value === rule.eventType);
    const evtLabel = isEvt
      ? (evtMatch ? t(evtMatch.labelKey) : (rule.eventType || t("rules.event_unspecified")))
      : "";
    const evt = isEvt ? `<span class="tag blue">${esc(evtLabel)}</span>` : "";
    const condCount = Array.isArray(rule.conditions) ? rule.conditions.length : 0;
    const actionCount = Array.isArray(rule.actions) ? rule.actions.length : 0;
    const blockTag = rule.block
      ? '<span class="tag red">' + esc(t("rules.block_next")) + '</span>' : '';
    return `<div class="rule-card glass interactive" onclick="if(event.target===this||event.target.closest('.rule-head'))highlightRule(this)">
      <div class="rule-head">
        <span class="name">${esc(rule.name || t("rules.unnamed"))}</span>
        <span class="rule-badges">
          ${tag}
          <span class="tag gray">${esc(tt)}</span>
          ${evt}
          ${blockTag}
          <span class="id">#${esc(rule.id || idx)}</span>
        </span>
      </div>
      <div class="rule-meta">${patternText}<br>
        ${esc(t("rules.conditions_count"))} ${condCount} ${esc(t("rules.count_suffix"))} · ${esc(t("rules.actions_count"))} ${actionCount} ${esc(t("rules.count_suffix"))}
      </div>
      <div class="rule-actions">
        <button class="btn small ghost" onclick="openRuleEditor(${idx})">${esc(t("common.edit"))}</button>
        <button class="btn small ghost" onclick="moveRule(${idx}, -1)">${esc(t("common.move_up"))}</button>
        <button class="btn small ghost" onclick="moveRule(${idx}, 1)">${esc(t("common.move_down"))}</button>
        <button class="btn small ghost" onclick="duplicateRule(${idx})">${esc(t("common.copy"))}</button>
        <button class="btn small danger" onclick="deleteRule(${idx})">${esc(t("common.delete"))}</button>
      </div>
    </div>`;
  }).join("");
}

function highlightRule(el) {
  const wasActive = el.classList.contains("is-active");
  document.querySelectorAll(".rule-card.is-active").forEach((n) => n.classList.remove("is-active"));
  if (!wasActive) {
    el.classList.add("is-active");
    setTimeout(() => el.classList.remove("is-active"), 1600);
  }
}

function moveRule(idx, dir) {
  const newIdx = idx + dir;
  if (newIdx < 0 || newIdx >= rulesData.length) return;
  const tmp = rulesData[idx];
  rulesData[idx] = rulesData[newIdx];
  rulesData[newIdx] = tmp;
  renderRulesList();
  syncRulesJson();
}

function duplicateRule(idx) {
  const copy = JSON.parse(JSON.stringify(rulesData[idx]));
  copy.id = (copy.id || "rule") + "_copy_" + Date.now();
  copy.name = (copy.name || t("rules.new_rule")) + t("rules.rule_copy_suffix");
  rulesData.splice(idx + 1, 0, copy);
  renderRulesList();
  syncRulesJson();
  toast(t("rules.copied_hint"));
}

async function deleteRule(idx) {
  if (!await customConfirm(t("rules.delete_confirm", { name: rulesData[idx].name || idx }))) return;
  rulesData.splice(idx, 1);
  renderRulesList();
  syncRulesJson();
}

function addRuleTemplate() {
  openRuleEditor(-1);
}


function newRuleTemplate() {
  return {
    id: "rule_" + Date.now(),
    name: t("rules.new_rule"),
    enabled: true,
    triggerType: "message",
    pattern: "^关键字$",
    flags: "i",
    eventType: "",
    conditions: [],
    actions: [{ type: "replyText", params: t("rules.reply_trigger_default") }],
    block: true,
  };
}

function openRuleEditor(idx) {
  editingRuleIndex = idx;
  const rule = idx < 0 ? newRuleTemplate() : JSON.parse(JSON.stringify(rulesData[idx]));
  document.getElementById("rule-editor-title").textContent =
    idx < 0 ? t("rules.editor_new_title") : t("rules.editor_edit_title", { name: rule.name || idx });
  document.getElementById("rule-editor-body").innerHTML = renderRuleForm(rule);
  document.getElementById("rule-editor-modal").classList.add("show");
}

function renderRuleForm(rule) {
  const isMsg = rule.triggerType !== "event";

  const presetOpts = [
    { value: "", label: t("rules.form.template_first_option") },
    ...RULE_PATTERN_PRESETS.map((p) => ({ value: p.value, label: t(p.labelKey) })),
  ];
  const presetSelect = buildSelect("rule-preset", presetOpts, "", t("rules.form.template_placeholder"));
  const templateOpts = [
    { value: "", label: t("rules.form.template_row_first_option") },
    ...RULE_TEMPLATES.map((p) => ({ value: p.value, label: t(p.labelKey) })),
  ];
  const templateSelect = buildSelect("rule-template", templateOpts, "", t("rules.form.template_row_placeholder"));

  return `<div class="form-row">
    <label>${esc(t("rules.form.template_row"))}<span class="desc">${esc(t("rules.form.template_row_desc"))}</span></label>
    <div class="ctrl">${templateSelect}</div>
  </div>
  <div class="form-row">
    <label>${esc(t("rules.form.rule_name"))}<span class="desc">${esc(t("rules.form.rule_name_desc"))}</span></label>
    <div class="ctrl"><input type="text" id="rule-name" value="${esc(rule.name || "")}" placeholder="${esc(t("rules.form.rule_name_placeholder"))}"></div>
  </div>
  <div class="form-row">
    <label>${esc(t("rules.form.enabled"))}<span class="desc">${esc(t("rules.form.enabled_desc"))}</span></label>
    <div class="switch"><input type="checkbox" id="rule-enabled" ${rule.enabled !== false ? "checked" : ""}>
      <label class="track" for="rule-enabled"></label></div>
  </div>
  <div class="form-row">
    <label>${esc(t("rules.form.trigger_mode"))}<span class="desc">${esc(t("rules.form.trigger_mode_desc"))}</span></label>
    <div class="ctrl">
      <div class="segment">
        <button type="button" id="rule-tt-msg" class="${isMsg ? "active" : ""}" onclick="setRuleTriggerType('message')">${esc(t("rules.form.message_trigger"))}</button>
        <button type="button" id="rule-tt-evt" class="${!isMsg ? "active" : ""}" onclick="setRuleTriggerType('event')">${esc(t("rules.form.event_trigger"))}</button>
      </div>
    </div>
  </div>
  <div class="form-row" id="rule-pattern-row" style="${isMsg ? "" : "display:none"}">
    <label>${esc(t("rules.form.keyword"))}<span class="desc">${esc(t("rules.form.keyword_desc"))}</span></label>
    <div class="ctrl">
      <input type="text" id="rule-pattern" value="${esc(rule.pattern || "")}" placeholder="${esc(t("rules.form.keyword_placeholder"))}">
      <div style="margin-top:8px">${presetSelect}</div>
    </div>
  </div>
  <div class="form-row" id="rule-flags-row" style="${isMsg ? "" : "display:none"}">
    <label>${esc(t("rules.form.case_sensitive"))}<span class="desc">${esc(t("rules.form.case_sensitive_desc"))}</span></label>
    <div class="switch"><input type="checkbox" id="rule-flag-ignorecase" ${(rule.flags || "i").includes("i") ? "checked" : ""}>
      <label class="track" for="rule-flag-ignorecase"></label>
      <span style="margin-left:8px;font-size:13px;color:var(--muted)">${esc(t("rules.form.ignore_case"))}</span></div>
  </div>
  <div class="form-row" id="rule-event-row" style="${!isMsg ? "" : "display:none"}">
    <label>${esc(t("rules.form.event_type"))}<span class="desc">${esc(t("rules.form.event_type_desc"))}</span></label>
    <div class="ctrl">${buildSelect("rule-eventType", ruleOptions(RULE_EVENT_TYPES), rule.eventType || "", t("rules.form.event_type_placeholder"))}</div>
  </div>
  <div class="form-row">
    <label>${esc(t("rules.form.block_next"))}<span class="desc">${esc(t("rules.form.block_next_desc"))}</span></label>
    <div class="switch"><input type="checkbox" id="rule-block" ${rule.block ? "checked" : ""}>
      <label class="track" for="rule-block"></label></div>
  </div>
  <div class="section">
    <h3 style="display:flex;justify-content:space-between;align-items:center">
      <span>${esc(t("rules.form.conditions"))}</span>
      <button type="button" class="btn small ghost" onclick="addEditorCondition()">${esc(t("rules.form.add_condition"))}</button>
    </h3>
    <p class="desc" style="margin:0 0 10px">${esc(t("rules.form.conditions_hint"))}</p>
    <div id="rule-conditions-list">${renderConditionsList(rule.conditions)}</div>
  </div>
  <div class="section">
    <h3 style="display:flex;justify-content:space-between;align-items:center">
      <span>${esc(t("rules.form.actions"))}</span>
      <button type="button" class="btn small ghost" onclick="addEditorAction()">${esc(t("rules.form.add_action"))}</button>
    </h3>
    <p class="desc" style="margin:0 0 10px">${esc(t("rules.form.actions_hint"))}</p>
    <div id="rule-actions-list">${renderActionsList(rule.actions)}</div>
  </div>`;
}

function setRuleTriggerType(type) {
  const isMsg = type === "message";
  document.getElementById("rule-tt-msg").classList.toggle("active", isMsg);
  document.getElementById("rule-tt-evt").classList.toggle("active", !isMsg);
  document.getElementById("rule-pattern-row").style.display = isMsg ? "" : "none";
  document.getElementById("rule-flags-row").style.display = isMsg ? "" : "none";
  document.getElementById("rule-event-row").style.display = isMsg ? "none" : "";
}

function collectEditorConditions() {
  const list = document.getElementById("rule-conditions-list");
  if (!list) return [];
  const items = list.querySelectorAll(".file-item");
  const out = [];
  items.forEach((item) => {
    const field = item.querySelector('input[id^="rule-cond-field-"]');
    const op = item.querySelector('input[id^="rule-cond-op-"]');
    const val = item.querySelector('input[id^="rule-cond-val-"]');
    if (field && op && val) out.push({ field: field.value, operator: op.value, value: val.value });
  });
  return out;
}

function collectEditorActions() {
  const list = document.getElementById("rule-actions-list");
  if (!list) return [];
  const items = list.querySelectorAll(".file-item");
  const out = [];
  items.forEach((item) => {
    const type = item.querySelector('input[id^="rule-action-type-"]');
    const params = item.querySelector("textarea");
    if (type && params) out.push({ type: type.value, params: params.value });
  });
  return out;
}

function addEditorCondition() {
  const conds = collectEditorConditions();
  // 默认一条可用的身份条件（管理员），用户添加后无需手填即可理解用法
  conds.push({ field: "userRole", operator: "==", value: "admin" });
  rerenderEditorLists({ conditions: conds, actions: collectEditorActions() });
}

function addEditorAction() {
  const acts = collectEditorActions();
  acts.push({ type: "replyText", params: "" });
  rerenderEditorLists({ conditions: collectEditorConditions(), actions: acts });
}

function removeEditorItem(kind, idx) {
  if (kind === "cond") {
    const conds = collectEditorConditions();
    conds.splice(idx, 1);
    rerenderEditorLists({ conditions: conds, actions: collectEditorActions() });
  } else {
    const acts = collectEditorActions();
    acts.splice(idx, 1);
    rerenderEditorLists({ conditions: collectEditorConditions(), actions: acts });
  }
}

function rerenderEditorLists({ conditions, actions }) {
  const condBox = document.getElementById("rule-conditions-list");
  const actBox = document.getElementById("rule-actions-list");
  if (condBox) condBox.innerHTML = renderConditionsList(conditions);
  if (actBox) actBox.innerHTML = renderActionsList(actions);
}

function renderConditionsList(conditions) {
  if (!conditions || !conditions.length)
    return '<div class="empty-state" style="padding:14px">' + esc(t("rules.form.no_conditions")) + '</div>';
  return conditions.map((c, i) => {
    const meta = RULE_CONDITION_META[c.field] || {};
    const hint = meta.hintKey ? t(meta.hintKey) : "";
    const ph = meta.phKey ? t(meta.phKey) : t("rules.form.value_placeholder");
    // userRole 用身份下拉选择，普通用户无需手填英文身份值
    const valueHtml = c.field === "userRole"
      ? buildSelect("rule-cond-val-" + i, ruleOptions(RULE_ROLE_OPTIONS), c.value, t("rules.form.value_placeholder"))
      : `<input type="text" id="rule-cond-val-${i}" value="${esc(c.value)}" placeholder="${esc(ph)}" style="flex:2;min-width:140px">`;
    // userRole 后端仅支持等于/不等于，其余运算符恒不匹配——下拉里直接隐藏
    const ops = c.field === "userRole"
      ? RULE_CONDITION_OPERATORS.filter((o) => o.value === "==" || o.value === "!=")
      : RULE_CONDITION_OPERATORS;
    return `<div class="file-item" style="flex-direction:column;align-items:stretch;gap:8px">
      <div style="display:flex;justify-content:space-between;align-items:center">
        <strong style="font-size:13px">${esc(t("rules.form.condition_title", { n: i + 1 }))}</strong>
        <button class="btn small danger" onclick="removeEditorItem('cond', ${i})">${esc(t("common.remove"))}</button>
      </div>
      ${hint ? `<div class="desc" style="font-size:12px;line-height:1.5">${esc(hint)}</div>` : ""}
      <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">
        <div style="flex:1;min-width:120px">${buildSelect("rule-cond-field-" + i, ruleOptions(RULE_CONDITION_FIELDS), c.field, t("rules.form.field_placeholder"))}</div>
        <div style="flex:1;min-width:100px">${buildSelect("rule-cond-op-" + i, ruleOptions(ops), c.operator, t("rules.form.operator_placeholder"))}</div>
        <div style="flex:2;min-width:140px">${valueHtml}</div>
      </div>
    </div>`;
  }).join("");
}

function renderActionsList(actions) {
  if (!actions || !actions.length)
    return '<div class="empty-state" style="padding:14px">' + esc(t("rules.form.no_actions")) + '</div>';
  return actions.map((a, i) => {
    const meta = RULE_ACTION_META[a.type] || {};
    const desc = meta.descKey ? t(meta.descKey) : "";
    const ph = meta.phKey ? t(meta.phKey) : t("rules.form.action_params_placeholder");
    let paramsHtml = "";
    if (a.type === "deleteMessage") {
      // 撤回消息无需参数：只读说明，不误导用户填内容
      paramsHtml = `<textarea id="rule-action-params-${i}" rows="1" readonly placeholder="${esc(ph)}" style="width:100%;opacity:.75"></textarea>`;
    } else if (a.type === "replyImage") {
      // 回复图片：可直接上传本地图片（自动填入路径），也可手填图片链接
      paramsHtml = `<textarea id="rule-action-params-${i}" rows="2" placeholder="${esc(ph)}" style="width:100%">${esc(a.params ?? "")}</textarea>
      <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
        <button type="button" class="btn small ghost" onclick="document.getElementById('rule-image-file-${i}').click()">${esc(t("rules.form.image_upload_button"))}</button>
        <input type="file" id="rule-image-file-${i}" accept="image/png,image/jpeg,image/gif,image/webp" style="display:none" onchange="uploadRuleImage(this, ${i})">
        <span id="rule-image-name-${i}" class="desc" style="font-size:12px"></span>
      </div>`;
    } else {
      paramsHtml = `<textarea id="rule-action-params-${i}" rows="2" placeholder="${esc(ph)}" style="width:100%">${esc(a.params ?? "")}</textarea>`;
    }
    return `<div class="file-item" style="flex-direction:column;align-items:stretch;gap:8px">
      <div style="display:flex;justify-content:space-between;align-items:center">
        <strong style="font-size:13px">${esc(t("rules.form.action_title", { n: i + 1 }))}</strong>
        <button class="btn small danger" onclick="removeEditorItem('action', ${i})">${esc(t("common.remove"))}</button>
      </div>
      <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">
        <div style="flex:1;min-width:140px">${buildSelect("rule-action-type-" + i, ruleOptions(RULE_ACTION_TYPES), a.type, t("rules.form.action_type_placeholder"))}</div>
      </div>
      ${desc ? `<div class="desc" style="font-size:12px;line-height:1.5">${esc(desc)}</div>` : ""}
      ${paramsHtml}
    </div>`;
  }).join("");
}

function applyRuleTemplate(value) {
  const tpl = RULE_TEMPLATE_DATA[value];
  if (!tpl) return;
  const rule = {
    id: editingRuleIndex >= 0 && rulesData[editingRuleIndex]
      ? rulesData[editingRuleIndex].id
      : "rule_" + Date.now(),
    name: t(tpl.nameKey),
    enabled: true,
    triggerType: tpl.triggerType,
    pattern: tpl.pattern,
    flags: tpl.flags,
    eventType: tpl.eventType || "",
    conditions: (tpl.conditions || []).map((c) => ({ field: c.field, operator: c.operator, value: c.value })),
    actions: (tpl.actions || []).map((a) => ({
      type: a.type,
      params: a.paramsKey ? t(a.paramsKey) : (a.params || ""),
    })),
    block: tpl.block !== false,
  };
  document.getElementById("rule-editor-body").innerHTML = renderRuleForm(rule);
  toast(t("rules.form.template_applied", { name: rule.name }));
}

async function uploadRuleImage(input, idx) {
  const file = input.files && input.files[0];
  if (!file) return;
  const form = new FormData();
  form.append("file", file);
  try {
    const res = await fetch("/api/rules/image", {
      method: "POST",
      headers: { Authorization: "Bearer " + TOKEN },
      body: form,
    });
    if (res.status === 401) { logout(true); return; }
    const data = await res.json();
    if (data.code !== 200) { toast(data.msg || t("rules.form.image_upload_failed", { error: "" }), true); return; }
    const ta = document.getElementById("rule-action-params-" + idx);
    if (ta) ta.value = data.data.path;
    const nameEl = document.getElementById("rule-image-name-" + idx);
    if (nameEl) nameEl.textContent = data.data.name || "";
    toast(t("rules.form.image_uploaded"));
  } catch (e) {
    toast(t("rules.form.image_upload_failed", { error: e.message }), true);
  }
  input.value = "";
}

function saveRuleEditor() {
  const id = "rule_" + Date.now();
  const name = document.getElementById("rule-name").value.trim() || t("rules.unnamed_save");
  const enabled = document.getElementById("rule-enabled").checked;
  const triggerType = document.getElementById("rule-tt-evt").classList.contains("active") ? "event" : "message";
  const pattern = document.getElementById("rule-pattern") ? document.getElementById("rule-pattern").value : "";
  const ignoreCase = document.getElementById("rule-flag-ignorecase") ? document.getElementById("rule-flag-ignorecase").checked : true;
  const flags = ignoreCase ? "i" : "";
  const eventTypeEl = document.getElementById("rule-eventType");
  const eventType = eventTypeEl ? eventTypeEl.value : "";
  const block = document.getElementById("rule-block").checked;
  const conditions = collectEditorConditions();
  const actions = collectEditorActions();
  const rule = { id, name, enabled, triggerType, pattern, flags, eventType, conditions, actions, block };

  if (editingRuleIndex < 0) {
    rulesData.push(rule);
  } else {
    rule.id = rulesData[editingRuleIndex].id || id;
    rulesData[editingRuleIndex] = rule;
  }
  renderRulesList();
  syncRulesJson();
  closeModal("rule-editor-modal");
  toast(t("rules.saved_toast", { action: t(editingRuleIndex < 0 ? "rules.saved_new" : "rules.saved_updated") }));
}


let wlDomain = "qq";          // 当前白名单展示域：qq（个人号）/ official（QQ 官方 bot）
let wlDomainInit = false;     // 是否已按适配器连接状态初始化默认域


function _renderWhitelistDomainSwitch(meta) {
  const wrap = document.getElementById("wl-domain-switch");
  if (!wrap) return;
  const showSwitch = !!(meta && (meta.has_qq && meta.has_official));
  wrap.style.display = showSwitch ? "" : "none";
  const btnQq = document.getElementById("wl-domain-qq");
  const btnOfficial = document.getElementById("wl-domain-official");
  if (btnQq) btnQq.classList.toggle("active", wlDomain === "qq");
  if (btnOfficial) btnOfficial.classList.toggle("active", wlDomain === "official");
  const th = document.querySelector("#wl-table thead th");
  if (th) th.textContent = t(wlDomain === "official" ? "whitelist.openid" : "whitelist.qq");
}


async function switchWhitelistDomain(domain) {
  if (domain !== "qq" && domain !== "official") return;
  wlDomain = domain;
  wlDomainInit = true; // 手动切换后不再自动跟随连接状态
  loadWhitelist();
}


async function loadWhitelist() {
  try {
    // 默认域：仅官方适配器 → official；仅个人号或双开 → qq（双开时即使官方已连接也默认 qq）
    if (!wlDomainInit) {
      try {
        const { data: meta } = await api("GET", "/api/whitelist/domains");
        if (meta && meta.default) wlDomain = meta.default;
      } catch (_) { /* 保持缺省 */ }
      wlDomainInit = true;
    }
    const { data: rawData } = await api("GET", "/api/whitelist?domain=" + wlDomain);
    const data = Array.isArray(rawData) ? rawData : [];
    const tbody = document.querySelector("#wl-table tbody");
    tbody.innerHTML = data.length
      ? data.map((b) => `<tr><td>${esc(b.qid)}</td><td>${esc(b.xbox)}</td>
          <td><button class="btn small danger wl-unbind-btn" data-qid="${esc(b.qid)}">${esc(t("whitelist.unbind_button"))}</button></td></tr>`).join("")
      : `<tr><td colspan="3" style="color:var(--muted)">${esc(t("whitelist.empty"))}</td></tr>`;
    api("GET", "/api/whitelist/domains").then(({ data: meta }) => _renderWhitelistDomainSwitch(meta)).catch(() => _renderWhitelistDomainSwitch(null));
  } catch (e) { toast(t("whitelist.load_failed", { error: e.message }), true); }
}

async function unbind(qq) {
  if (!await customConfirm(t("whitelist.unbind_confirm", { qq }))) return;
  try {
    const res = await api("DELETE", "/api/whitelist/" + encodeURIComponent(qq) + "?domain=" + wlDomain);
    toast(res.msg || t("whitelist.unbind_success"));
    loadWhitelist();
  } catch (e) { toast(t("whitelist.unbind_failed", { error: e.message }), true); }
}


/* ─── 子插件卡片操作分发：主按钮与 ⋯ 菜单项共用 ─── */
function dispatchSpAction(action, name) {
  switch (action) {
    case "reload": reloadSingleSubplugin(name); break;
    case "config": openPluginConfig(name); break;
    case "update": openUpdateFlow(name); break;
    case "update-deps": updateSubpluginDependencies(name); break;
    case "install-deps": installSubpluginDeps(name); break;
    case "install-requirements": installSubpluginRequirements(name); break;
    case "files": openFilesModal(name); break;
    case "copy-error": copySubpluginError(name); break;
    case "error-detail": toggleSubpluginErrorDetail(name); break;
    case "uninstall": uninstallPlugin(name); break;
  }
}

/* ─── 子插件 ⋯ 更多菜单（fixed 定位浮层，z-index:400 置于一切之上） ─── */
const SP_MENU_ICONS = {
  files: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>',
  "update-deps": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"/><polyline points="3.27 6.96 12 12.01 20.73 6.96"/><line x1="12" y1="22.08" x2="12" y2="12"/></svg>',
  "copy-error": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>',
  "error-detail": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M12 16v-4"/><path d="M12 8h.01"/></svg>',
  uninstall: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>',
};

function buildSpMoreMenuItems(name) {
  const cached = subpluginMarketCache[name] || {};
  const items = [];
  const item = (action, label, danger) =>
    `<button type="button" class="sp-menu-item menu-item${danger ? " danger" : ""}" data-action="${esc(action)}" data-name="${esc(name)}" role="menuitem">
      ${SP_MENU_ICONS[action] || ""}<span>${esc(label)}</span>
    </button>`;
  items.push(item("files", t("subplugins.files_button")));
  // 仅声明了依赖的子插件才提供「更新依赖」，无依赖不显示
  if ((cached.dependencies || []).length) items.push(item("update-deps", t("marketplace.update_deps_button")));
  if (cached.hasError || cached.hasMissing) items.push(item("copy-error", t("subplugins.copy_error")));
  if (cached.hasError) items.push(item("error-detail", t("subplugins.view_error_detail")));
  items.push('<div class="menu-sep"></div>');
  items.push(item("uninstall", t("subplugins.uninstall_button"), true));
  return items.join("");
}

function positionSpMoreMenu(menu, btn) {
  // fixed 定位跟随按钮；视口边缘自动翻转/收窄，滚动或缩放时直接收起
  const rect = btn.getBoundingClientRect();
  menu.style.visibility = "hidden";
  menu.classList.add("show");
  const mw = menu.offsetWidth;
  const mh = menu.offsetHeight;
  let left = Math.min(rect.right - mw, window.innerWidth - mw - 8);
  left = Math.max(8, left);
  let top = rect.bottom + 6;
  if (top + mh > window.innerHeight - 8) top = Math.max(8, rect.top - mh - 6);
  menu.style.left = left + "px";
  menu.style.top = top + "px";
  menu.style.visibility = "";
}

function toggleSpMoreMenu(btn) {
  const menu = document.getElementById("sp-more-menu");
  if (!menu || !btn) return;
  const name = btn.dataset.name || "";
  if (spMoreMenuState && spMoreMenuState.name === name) {
    closeSpMoreMenu();
    return;
  }
  closeSpMoreMenu();
  menu.innerHTML = buildSpMoreMenuItems(name);
  spMoreMenuState = { name, btn };
  btn.classList.add("active");
  positionSpMoreMenu(menu, btn);
}

function closeSpMoreMenu() {
  const menu = document.getElementById("sp-more-menu");
  if (menu) menu.classList.remove("show");
  if (spMoreMenuState && spMoreMenuState.btn) spMoreMenuState.btn.classList.remove("active");
  spMoreMenuState = null;
}

/* 滚动 / 缩放 / ESC 时收起 ⋯ 菜单（fixed 浮层不随内容滚动，留在原地会错位） */
window.addEventListener("scroll", closeSpMoreMenu, { passive: true, capture: true });
window.addEventListener("resize", closeSpMoreMenu);
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && spMoreMenuState) closeSpMoreMenu();
});

/* 移动端 tab 条：滚动 / 缩放时刷新两侧渐隐提示 */
(function initTabbarScrollHints() {
  document.addEventListener("DOMContentLoaded", () => {
    const scroller = document.getElementById("tabbar-scroll");
    if (!scroller) return;
    scroller.addEventListener("scroll", syncTabbarEdges, { passive: true });
    window.addEventListener("resize", syncTabbarEdges);
    // 初始（tabbar 随移动布局显示后）与字体/语言切换后都刷新一次
    requestAnimationFrame(syncTabbarEdges);
    setTimeout(syncTabbarEdges, 300);
  });
})();

/* ─── 子插件描述展开浮层：卡片内固定两行截断，全文以滑动浮层展示 ─── */
let spDescPopoverState = null;

function toggleSpDescPopover(btn) {
  const pop = document.getElementById("sp-desc-popover");
  if (!pop || !btn) return;
  const name = btn.dataset.spName || "";
  if (spDescPopoverState && spDescPopoverState.name === name) {
    closeSpDescPopover();
    return;
  }
  closeSpDescPopover();
  const desc = btn.closest(".sp-desc-row")?.querySelector(".sp-desc");
  pop.textContent = (desc && desc.textContent) || "";
  spDescPopoverState = { name, btn };
  btn.classList.add("active");
  // fixed 定位跟随按钮，视口边缘自动翻转
  pop.classList.add("show");
  const rect = btn.getBoundingClientRect();
  pop.style.visibility = "hidden";
  const pw = pop.offsetWidth;
  const ph = pop.offsetHeight;
  let left = Math.min(rect.left, window.innerWidth - pw - 8);
  left = Math.max(8, left);
  let top = rect.bottom + 6;
  if (top + ph > window.innerHeight - 8) top = Math.max(8, rect.top - ph - 6);
  pop.style.left = left + "px";
  pop.style.top = top + "px";
  pop.style.visibility = "";
}

function closeSpDescPopover() {
  const pop = document.getElementById("sp-desc-popover");
  if (pop) pop.classList.remove("show");
  if (spDescPopoverState && spDescPopoverState.btn) spDescPopoverState.btn.classList.remove("active");
  spDescPopoverState = null;
}

window.addEventListener("scroll", closeSpDescPopover, { passive: true, capture: true });
window.addEventListener("resize", closeSpDescPopover);
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && spDescPopoverState) closeSpDescPopover();
});

/* 渲染后检测描述是否被截断：溢出才显示「展开」小按钮 */
function markOverflowSpDescs() {
  document.querySelectorAll("#sp-list .sp-desc-row").forEach((row) => {
    const desc = row.querySelector(".sp-desc");
    if (!desc) return;
    row.classList.toggle("overflow", desc.scrollHeight > desc.clientHeight + 2);
  });
}

async function loadSubplugins(opts) {
  const feedback = !!(opts && opts.feedback);
  if (feedback) toast(t("subplugins.refreshing"));
  try {
    const [pluginsResult, configsResult] = await Promise.all([
      api("GET", "/api/subplugins"),
      api("GET", "/api/plugins/configs").catch(() => ({ data: [] })),
    ]);
    const data = Array.isArray(pluginsResult.data) ? pluginsResult.data : [];
    pluginConfigNames = new Set(Array.isArray(configsResult.data) ? configsResult.data : []);
    const box = document.getElementById("sp-list");
    if (!data.length) {
      box.innerHTML = `<div class="card glass empty-state">${esc(t("subplugins.empty"))}</div>`;
      return;
    }
    box.innerHTML = data.map((p) => {
      const status = p.loaded
        ? '<span class="tag green">' + esc(t("subplugins.loaded")) + '</span>'
        : p.error
          ? `<span class="tag red" title="${esc(p.error)}">${esc(t("subplugins.failed"))}</span>`
          : '<span class="tag gray">' + esc(t("subplugins.disabled")) + '</span>';
      const encodedName = encodeURIComponent(p.name);
      const hasConfig = pluginConfigNames.has(p.name);
      const marketOrigin = p.market && p.market.source === "marketplace" ? p.market : null;
      const marketUpdate = p.market_update || {};
      const marketBadge = marketOrigin
        ? `<span class="tag blue">${esc(t("marketplace.installed_badge"))}</span>`
        : "";
      const updateBadge = marketUpdate.available
        ? `<span class="tag orange">${esc(t("marketplace.update_available_badge", { version: marketUpdate.latest_version || "?" }))}</span>`
        : "";

      const missingDeps = Array.isArray(p.missing_deps) ? p.missing_deps : [];
      const missingModules = Array.isArray(p.missing_modules) ? p.missing_modules : [];
      const hasMissing = missingDeps.length > 0 || missingModules.length > 0;
      const depsList = missingDeps.length ? missingDeps : missingModules;
      // 插件级强制依赖（requires）：声明徽章 + 未满足时的补装入口
      const requires = p.requires || {};
      const reqSub = Array.isArray(requires.subplugins) ? requires.subplugins : [];
      const reqEs = Array.isArray(requires.endstone) ? requires.endstone : [];
      const missingRequirements = Array.isArray(p.missing_requirements) ? p.missing_requirements : [];
      const hasMissingRequirements = missingRequirements.length > 0;
      delete subpluginDepsCache[p.name];
      delete subpluginErrorCache[p.name];
      if (hasMissing) subpluginDepsCache[p.name] = depsList;
      if (p.error) subpluginErrorCache[p.name] = p.error;
      // 缓存市场来源与版本信息，更新流程（市场/手动选择）直接复用，避免再次请求列表
      subpluginMarketCache[p.name] = {
        market: marketOrigin,
        version: String(p.version || ""),
        dependencies: Array.isArray(p.dependencies) ? p.dependencies : [],
        requires: [].concat(reqSub, reqEs),
        hasError: !!p.error,
        hasMissing,
        hasMissingRequirements,
      };

      const missingBadge = hasMissing
        ? `<span class="tag red">${esc(t("subplugins.missing_deps_badge"))}</span>`
        : "";
      const requiresBadge = (reqSub.length || reqEs.length)
        ? `<span class="tag cyan" title="${esc([].concat(reqSub, reqEs).join("、"))}">${esc(t("subplugins.requires_badge"))}: ${reqSub.length + reqEs.length}</span>`
        : "";
      const missingReqBadge = hasMissingRequirements
        ? `<span class="tag red" title="${esc(missingRequirements.join("、"))}">${esc(t("subplugins.missing_requirements_badge"))}</span>`
        : "";

      const errorDetail = p.error
        ? `<div class="error-detail" style="display:none;margin-top:10px;color:var(--red);font-family:monospace;font-size:12px;white-space:pre-wrap;max-height:260px;overflow:auto">${esc(p.error)}</div>`
        : "";

      // 主操作只保留「重载 / 更新 / 配置 / 装依赖」，其余收进 ⋯ 菜单，避免按钮过载
      // data-* 属性 + 全局事件委托，避免把插件名拼进 onclick 导致 JS 字符串逃逸
      const actions = [];
      const spAction = (label, action, cls = "white") =>
        `<button class="btn small ${cls} sp-action-btn" data-action="${esc(action)}" data-name="${esc(p.name)}">${esc(label)}</button>`;
      actions.push(spAction(t("subplugins.reload_one_button"), "reload", ""));
      // 有新版本时给「更新」按钮右上角加小红点（CSS .has-update::after）
      actions.push(spAction(t("subplugins.update_button"), "update", marketUpdate.available ? "white has-update" : "white"));
      if (hasConfig) actions.push(spAction(t("subplugins.config_button"), "config"));
      if (hasMissing) actions.push(spAction(t("pip_page.subplugin_install_deps"), "install-deps"));
      if (hasMissingRequirements) actions.push(spAction(t("subplugins.install_requirements_button"), "install-requirements"));

      return `<div class="subplugin-card glass" data-sp-name="${esc(p.name)}">
        <div class="sp-head">
          <span class="name" title="${esc(p.name)}">${esc(p.name)}</span>
          <span class="tag blue">v${esc(p.version)}</span>
          <span class="tag gray">${esc(t("subplugins.priority"))}: ${esc(p.priority)}</span>
          ${status}
          ${marketBadge}
          ${updateBadge}
          ${missingBadge}
          ${requiresBadge}
          ${missingReqBadge}
        </div>
        <div class="sp-desc-row">
          <div class="sp-desc">${esc(p.description || t("subplugins.no_description"))}</div>
          <button class="sp-desc-expand" type="button" data-sp-name="${esc(p.name)}" title="${esc(t("subplugins.show_full_desc"))}" aria-label="${esc(t("subplugins.show_full_desc"))}">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="m6 9 6 6 6-6"/></svg>
          </button>
        </div>
        ${errorDetail}
        <div class="sp-actions">
          <span class="spacer"></span>
          ${actions.join("")}
          <button class="sp-more-btn" type="button" data-name="${esc(p.name)}" title="${esc(t("subplugins.more_actions"))}" aria-label="${esc(t("subplugins.more_actions"))}">
            <svg viewBox="0 0 24 24" fill="currentColor"><circle cx="5" cy="12" r="2"/><circle cx="12" cy="12" r="2"/><circle cx="19" cy="12" r="2"/></svg>
          </button>
          <div class="switch"><input type="checkbox" id="sp-${esc(encodedName)}" class="sp-toggle" data-name="${esc(p.name)}" ${p.load ? "checked" : ""}>
            <label class="track" for="sp-${esc(encodedName)}"></label></div>
        </div>
      </div>`;
    }).join("");
    requestAnimationFrame(markOverflowSpDescs);
    if (feedback) toast(t("subplugins.refresh_success"));
  } catch (e) { toast(t("subplugins.load_failed", { error: e.message }), true); }
}

async function reloadSingleSubplugin(name) {
  if (!await customConfirm(t("subplugins.reload_one_confirm", { name }))) return;
  try {
    const res = await api("POST", `/api/subplugins/${encodeURIComponent(name)}/reload`);
    toast(res.msg || t("subplugins.reload_one_success", { name }));
    loadSubplugins();
  } catch (e) {
    toast(t("subplugins.reload_one_failed", { name, error: e.message }), true);
    loadSubplugins();
  }
}

async function toggleSubplugin(name, enable) {
  try {
    const res = await api("POST", `/api/subplugins/${encodeURIComponent(name)}/toggle`, { enable });
    toast(res.msg || t("subplugins.op_success"));
    loadSubplugins();
  } catch (e) { toast(t("subplugins.op_failed", { error: e.message }), true); loadSubplugins(); }
}

async function reloadSubplugins() {
  try {
    const res = await api("POST", "/api/subplugins/reload");
    toast(res.msg || t("subplugins.reload_complete"));
    loadSubplugins();
    loadCustomPages();
  } catch (e) { toast(t("subplugins.reload_failed", { error: e.message }), true); }
}

async function uninstallPlugin(name) {
  // 预检依赖：有可一并卸载的依赖时，弹窗询问是否连同卸载（列出具体依赖名）；
  // 存在反向依赖（其它子插件 requires 本插件）时警告其将无法加载
  let withDeps = false;
  try {
    const res = await api("GET", `/api/subplugins/${encodeURIComponent(name)}/uninstall-preview`);
    const d = res.data || {};
    const deps = Array.isArray(d.deps) ? d.deps : [];
    const kept = Array.isArray(d.kept_deps) ? d.kept_deps : [];
    const dependents = Array.isArray(d.dependents) ? d.dependents : [];
    let msg = t("subplugins.uninstall_confirm", { name });
    if (dependents.length) {
      const names = dependents.map((x) => (x && x.name) || "").filter(Boolean);
      msg += "\n\n" + t("subplugins.uninstall_dependents_warning", { deps: names.join("、") });
    }
    if (kept.length) msg += "\n\n" + t("subplugins.uninstall_deps_kept", { deps: kept.join("、") });
    if (!await customConfirm(msg)) return;
    if (deps.length) {
      withDeps = await customConfirm(
        t("subplugins.uninstall_deps_question", { deps: deps.join("、") }),
        t("subplugins.uninstall_deps_title")
      );
    }
  } catch (e) {
    // 预检失败时退回普通确认，不阻塞卸载
    if (!await customConfirm(t("subplugins.uninstall_confirm", { name }))) return;
  }
  try {
    const res = await api("DELETE", "/api/subplugins/" + encodeURIComponent(name) + (withDeps ? "?with_deps=1" : ""));
    toast(res.msg || t("subplugins.uninstall_success"));
    loadSubplugins();
    loadCustomPages();
  } catch (e) { toast(t("subplugins.uninstall_failed", { error: e.message }), true); }
}


function openInstallModal() {
  document.getElementById("install-modal").classList.add("show");
}

function uploadZip(file) {
  if (!file) return;
  if (!file.name.toLowerCase().endsWith(".zip")) { toast(t("subplugins.install_zip_only"), true); return; }
  const track = document.getElementById("up-progress-track");
  const bar = document.getElementById("up-progress-bar");
  track.style.display = "block";
  bar.style.width = "0%";

  const form = new FormData();
  form.append("file", file);
  const xhr = new XMLHttpRequest();
  xhr.open("POST", "/api/subplugins/install/upload");
  xhr.setRequestHeader("Authorization", "Bearer " + TOKEN);
  xhr.upload.onprogress = (ev) => {
    if (ev.lengthComputable) bar.style.width = Math.round((ev.loaded / ev.total) * 100) + "%";
  };
  xhr.onload = () => {
    track.style.display = "none";
    document.getElementById("zip-input").value = "";
    if (xhr.status === 401) { logout(true); return; }
    try {
      const data = JSON.parse(xhr.responseText);
      if (data.code === 200) {
        const loaded = !data.data || data.data.loaded !== false;
        toast(data.msg || t("subplugins.install_success"), !loaded);
        closeModal("install-modal");
        loadSubplugins();
        loadCustomPages();
      } else {
        toast(data.msg || t("subplugins.install_failed"), true);
      }
    } catch (e) { toast(t("subplugins.install_failed_response"), true); }
  };
  xhr.onerror = () => { track.style.display = "none"; toast(t("subplugins.install_failed_network"), true); };
  xhr.send(form);
}

async function installFromUrl() {
  const url = document.getElementById("install-url").value.trim();
  if (!url) { toast(t("subplugins.install_url_required"), true); return; }
  toast(t("subplugins.install_downloading"));
  try {
    const res = await api("POST", "/api/subplugins/install/url", { url });
    const loaded = !res.data || res.data.loaded !== false;
    toast(res.msg || t("subplugins.install_success"), !loaded);
    closeModal("install-modal");
    document.getElementById("install-url").value = "";
    loadSubplugins();
    loadCustomPages();
  } catch (e) { toast(t("subplugins.install_failed", { error: e.message }), true); }
}

function openUpdateModal(name) {
  const nameEl = document.getElementById("update-plugin-name");
  if (nameEl) nameEl.textContent = name;
  const modal = document.getElementById("update-modal");
  if (modal) {
    modal.dataset.pluginName = name;
    modal.classList.add("show");
    const urlInput = document.getElementById("update-url");
    if (urlInput) urlInput.value = "";
    const track = document.getElementById("update-progress-track");
    if (track) track.style.display = "none";
  }
}

function updateModalPluginName() {
  const modal = document.getElementById("update-modal");
  return (modal && modal.dataset.pluginName) || "";
}

function uploadUpdateZip(file) {
  if (!file) return;
  if (!file.name.toLowerCase().endsWith(".zip")) { toast(t("subplugins.install_zip_only"), true); return; }
  const track = document.getElementById("update-progress-track");
  const bar = document.getElementById("update-progress-bar");
  track.style.display = "block";
  bar.style.width = "0%";
  const form = new FormData();
  form.append("file", file);
  const xhr = new XMLHttpRequest();
  xhr.open("POST", "/api/subplugins/install/upload");
  xhr.setRequestHeader("Authorization", "Bearer " + TOKEN);
  xhr.upload.onprogress = (ev) => {
    if (ev.lengthComputable) bar.style.width = Math.round((ev.loaded / ev.total) * 100) + "%";
  };
  xhr.onload = () => {
    track.style.display = "none";
    document.getElementById("update-zip-input").value = "";
    if (xhr.status === 401) { logout(true); return; }
    try {
      const data = JSON.parse(xhr.responseText);
      if (data.code === 200) {
        const loaded = !data.data || data.data.loaded !== false;
        // 模板含 {name} 占位符，必须传参，否则 toast 显示字面量 {name}
        toast(data.msg || t("subplugins.update_success", { name: updateModalPluginName() }), !loaded);
        closeModal("update-modal");
        loadSubplugins();
        loadCustomPages();
      } else {
        toast(data.msg || t("subplugins.update_failed", { name: updateModalPluginName() }), true);
      }
    } catch (e) { toast(t("subplugins.install_failed_response"), true); }
  };
  xhr.onerror = () => { track.style.display = "none"; toast(t("subplugins.install_failed_network"), true); };
  xhr.send(form);
}

async function updateFromUrl() {
  const url = document.getElementById("update-url").value.trim();
  if (!url) { toast(t("subplugins.install_url_required"), true); return; }
  toast(t("subplugins.update_downloading"));
  try {
    const res = await api("POST", "/api/subplugins/install/url", { url });
    const loaded = !res.data || res.data.loaded !== false;
    toast(res.msg || t("subplugins.update_success", { name: updateModalPluginName() }), !loaded);
    closeModal("update-modal");
    document.getElementById("update-url").value = "";
    loadSubplugins();
    loadCustomPages();
  } catch (e) { toast(t("subplugins.update_failed", { name: updateModalPluginName() }) + " (" + (e.message || "") + ")", true); }
}

(function initDropzone() {
  document.addEventListener("DOMContentLoaded", () => {
    const dz = document.getElementById("dropzone");
    if (dz) {
      ["dragenter", "dragover"].forEach((ev) =>
        dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.add("dragover"); }));
      ["dragleave", "drop"].forEach((ev) =>
        dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.remove("dragover"); }));
      dz.addEventListener("drop", (e) => {
        const file = e.dataTransfer.files && e.dataTransfer.files[0];
        if (file) uploadZip(file);
      });
    }
    const udz = document.getElementById("update-dropzone");
    if (udz) {
      ["dragenter", "dragover"].forEach((ev) =>
        udz.addEventListener(ev, (e) => { e.preventDefault(); udz.classList.add("dragover"); }));
      ["dragleave", "drop"].forEach((ev) =>
        udz.addEventListener(ev, (e) => { e.preventDefault(); udz.classList.remove("dragover"); }));
      udz.addEventListener("drop", (e) => {
        const file = e.dataTransfer.files && e.dataTransfer.files[0];
        if (file) uploadUpdateZip(file);
      });
    }
  });
})();


async function openFilesModal(name) {
  editingPlugin = name;
  document.getElementById("files-title").textContent = name + t("subplugins.files_title_suffix");
  document.getElementById("files-modal").classList.add("show");
  backToFileList();
  await refreshFileList();
}

async function refreshFileList() {
  const box = document.getElementById("file-browser");
  box.innerHTML = `<div style="color:var(--muted);padding:12px">${esc(t("subplugins.files_loading"))}</div>`;
  try {
    const { data: rawData } = await api("GET", `/api/subplugins/${encodeURIComponent(editingPlugin)}/files`);
    const data = Array.isArray(rawData) ? rawData : [];
    box.innerHTML = data.length
      ? data.map((f) => `<div class="file-item">
          <span class="fname">${esc(f.path)}</span>
          <span style="display:flex;align-items:center;gap:10px;flex-shrink:0">
            <span class="fsize">${fmtSize(f.size)}</span>
            ${f.editable
              ? `<button class="btn small ghost file-edit-btn" data-path="${esc(f.path)}">${esc(t("subplugins.files_edit_button"))}</button>`
              : `<span class="tag gray">${esc(t("subplugins.files_not_editable"))}</span>`}
          </span>
        </div>`).join("")
      : `<div style="color:var(--muted);padding:12px">${esc(t("subplugins.files_empty"))}</div>`;
  } catch (e) {
    box.innerHTML = `<div style="color:var(--red);padding:12px">${esc(e.message)}</div>`;
  }
}

function backToFileList() {
  document.getElementById("file-editor-wrap").style.display = "none";
  document.getElementById("file-browser").style.display = "flex";
}

async function openFileEditor(path) {
  try {
    const { data } = await api("GET",
      `/api/subplugins/${encodeURIComponent(editingPlugin)}/file?path=${encodeURIComponent(path)}`);
    document.getElementById("file-browser").style.display = "none";
    document.getElementById("file-editor-wrap").style.display = "block";
    document.getElementById("editing-path").textContent = data.path;
    const lang = detectLanguage(data.path);
    let cur = data.content;
    const editorHost = document.getElementById("file-editor-host");
    if (editorHost && editorHost._lumenHlTimer) {
      clearTimeout(editorHost._lumenHlTimer);
      editorHost._lumenHlTimer = null;
    }
    const oldTa = document.getElementById("file-editor");
    if (oldTa && oldTa.parentNode) {
      oldTa.parentNode.replaceChild(oldTa.cloneNode(false), oldTa);
    }
    bindCodeEditor(
      "file-editor-host", "file-editor-code", "file-editor", lang,
      () => cur, (v) => { cur = v; }
    );
    document.getElementById("file-editor").value = data.content;
  } catch (e) { toast(t("subplugins.files_read_failed", { error: e.message }), true); }
}

async function saveEditingFile() {
  const path = document.getElementById("editing-path").textContent;
  const content = document.getElementById("file-editor").value;
  try {
    const res = await api("POST",
      `/api/subplugins/${encodeURIComponent(editingPlugin)}/file?path=${encodeURIComponent(path)}`, { content });
    toast(res.msg || t("subplugins.files_save_success"));
  } catch (e) { toast(t("subplugins.files_save_failed", { error: e.message }), true); }
}


/* 子插件注册到底栏 tab 的自定义页面默认图标（registerPage 未传 icon 时使用） */
const CUSTOM_TAB_ICON_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><path d="M17.5 14v7M14 17.5h7"/></svg>';

/* 子插件 registerPage(icon=) 可用的命名 SVG 图标（与主面板 tab 同一套描边风格）；
   icon 值命中表内名称则渲染 SVG，否则按纯文本字符/emoji 处理 */
const CUSTOM_TAB_ICONS = {
  model: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><rect x="7" y="7" width="10" height="10" rx="2"/><path d="M12 2v3M12 19v3M2 12h3M19 12h3M5 5l2 2M17 17l2 2M19 5l-2 2M7 17l-2 2"/></svg>',
  bot: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><rect x="4" y="8" width="16" height="12" rx="3"/><path d="M12 8V4M9 4h6"/><path d="M9 13v2M15 13v2"/></svg>',
  chat: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>',
  shield: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg>',
  spark: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l1.9 5.1L19 10l-5.1 1.9L12 17l-1.9-5.1L5 10l5.1-1.9L12 3z"/><path d="M19 15l.9 2.1L22 18l-2.1.9L19 21l-.9-2.1L16 18l2.1-.9L19 15z"/></svg>',
  gear: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09a1.65 1.65 0 0 0-1-1.51 1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09a1.65 1.65 0 0 0 1.51-1 1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33h0a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51h0a1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82v0a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>',
  chart: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M3 3v18h18"/><path d="M7 15v-4M12 15V8M17 15v-6"/></svg>'
};

async function loadCustomPages() {
  let pages = [];
  try {
    const { data } = await api("GET", "/api/custom_pages");
    pages = Array.isArray(data) ? data : [];
  } catch (e) { pages = []; }

  const nav_ = document.getElementById("custom-nav");
  if (nav_) {
    // 桌面侧栏空间充足：无论注册到 tab 还是「其它」，全部展示
    nav_.innerHTML = pages.map((p) =>
      `<button class="nav-item custom-nav-btn" data-page="custom-${esc(p.id)}"
        data-custom-url="${esc(p.url)}" data-custom-title="${esc(p.title)}">
        ${MORE_ICON_SVG}
        ${esc(p.title)}</button>`).join("");
  }

  /* 「其它」按钮仅在该面板确实有页面时才显示，避免空入口撑稀底栏 */
  const tabMore = document.getElementById("tab-more");
  if (tabMore) tabMore.style.display = pages.some((p) => !p.tab) ? "" : "none";

  /* tab=True 的页面注册为底栏 tab（插到「其它」之前）；其余进「其它」面板。
     重新加载时先移除旧按钮，避免子插件热重载后重复堆积 */
  const tabBar = document.getElementById("tabbar-scroll");
  if (tabBar) {
    tabBar.querySelectorAll(".custom-tab-btn").forEach((b) => b.remove());
    pages.filter((p) => p.tab).forEach((p) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "tab-item custom-tab-btn";
      btn.dataset.page = "custom-" + p.id;
      btn.dataset.customUrl = p.url || "";
      btn.dataset.customTitle = p.title || "";
      btn.title = p.title || "";
      // 命名图标 → 同风格 SVG；否则默认 SVG；显式传入的其它短文本按字符图标渲染
      btn.innerHTML = CUSTOM_TAB_ICONS[p.icon] || CUSTOM_TAB_ICON_SVG;
      if (p.icon && !CUSTOM_TAB_ICONS[p.icon]) {
        const iconEl = document.createElement("span");
        iconEl.className = "tab-icon-text";
        iconEl.textContent = String(p.icon);
        btn.querySelector("svg")?.replaceWith(iconEl);
      }
      const lbl = document.createElement("span");
      lbl.textContent = p.title || "";
      btn.appendChild(lbl);
      tabBar.insertBefore(btn, tabMore);
    });
  }
  requestAnimationFrame(syncTabbarEdges);

  const moreList = document.getElementById("more-sheet-list");
  const sheetPages = pages.filter((p) => !p.tab);
  if (moreList) {
    moreList.innerHTML = sheetPages.length
      ? sheetPages.map((p) =>
          `<button class="more-item custom-more-btn" data-page="custom-${esc(p.id)}"
            data-custom-url="${esc(p.url)}" data-custom-title="${esc(p.title)}">
            ${MORE_ICON_SVG}<span>${esc(p.title)}</span></button>`).join("")
      : "";
  }
  const moreCustom = document.getElementById("more-sheet-custom");
  if (moreCustom) moreCustom.style.display = sheetPages.length ? "" : "none";
}

function openMoreSheet() {
  const mask = document.getElementById("more-sheet-mask");
  if (mask) mask.classList.add("show");
}

function closeMoreSheet() {
  const mask = document.getElementById("more-sheet-mask");
  if (mask) mask.classList.remove("show");
}


function appendLog(entry) {
  const box = document.getElementById("log-box");
  const line = document.createElement("div");
  line.className = "log-line";
  const lv = (entry.level || "info").toLowerCase();
  line.innerHTML = `<span class="t">${esc(entry.time)}</span>` +
    `<span class="lv lv-${lv}">${lv.toUpperCase()}</span>` +
    `<span style="color:#64d2ff">[${esc(entry.plugin)}]</span> ${esc(entry.msg)}`;
  box.appendChild(line);
  while (box.childNodes.length > 800) box.removeChild(box.firstChild);
  const cb = document.getElementById("log-autoscroll");
  if (cb && cb.checked) box.scrollTop = box.scrollHeight;
}

async function initLogs() {
  closeLogStream();
  const generation = logGeneration;
  const box = document.getElementById("log-box");
  const status = document.getElementById("log-status");
  box.innerHTML = "";
  if (status) status.textContent = t("logs.status_connecting");
  try {
    const { data } = await api("GET", "/api/logs");
    if (generation !== logGeneration || currentPage !== "logs") return;
    (data || []).forEach(appendLog);
  } catch (e) {
    if (generation !== logGeneration) return;
    if (status) status.textContent = t("logs.status_load_failed");
  }
  if (generation !== logGeneration || currentPage !== "logs" || document.hidden) return;
  const source = new EventSource("/api/logs/stream?token=" + encodeURIComponent(TOKEN));
  logSource = source;
  source.onopen = () => {
    if (logSource === source && status) status.textContent = t("logs.status_connected");
  };
  source.onmessage = (ev) => {
    if (logSource !== source || currentPage !== "logs") return;
    try { appendLog(JSON.parse(ev.data)); } catch (e) { /* 忽略无效事件 */ }
  };
  source.onerror = () => {
    if (logSource === source && status) status.textContent = t("logs.status_reconnecting");
  };
}

window.addEventListener("beforeunload", () => {
  closeLogStream();
  stopMetricsPolling();
});
document.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    closeLogStream();
    stopMetricsPolling();
  } else if (TOKEN) {
    if (currentPage === "logs") initLogs();
    if (currentPage === "dashboard") startMetricsPolling();
  }
});

function clearLogView() {
  document.getElementById("log-box").innerHTML = "";
}




let pipPackagesCache = [];
let pipPackageFilterTimer = null;
let pipLastRenderedQuery = null;

async function loadPipPackages() {
  const cfgBox = document.getElementById("pip-config-box");
  const grid = document.getElementById("pip-packages-grid");
  if (cfgBox) cfgBox.innerHTML = '<div class="empty-state">' + esc(t("common.loading_config")) + '</div>';
  if (grid) grid.innerHTML = '<div class="empty-state">' + esc(t("common.loading_config")) + '</div>';

  try {
    const { data: cfg } = await api("GET", "/api/pip/config");
    renderPipConfig(cfg);
  } catch (e) {
    if (cfgBox) cfgBox.innerHTML = '<div class="empty-state error">' + esc(t("pip_page.config_load_failed", { error: e.message })) + '</div>';
  }

  try {
    const { data: pkgs } = await api("GET", "/api/pip/list");
    pipPackagesCache = Array.isArray(pkgs) ? pkgs : [];
    pipLastRenderedQuery = null;
    filterPipPackages(true);
  } catch (e) {
    pipPackagesCache = [];
    renderPipTable([]);
  }
}

function renderPipConfig(cfg) {
  const box = document.getElementById("pip-config-box");
  if (!box) return;
  if (!cfg) { box.innerHTML = ""; return; }
  const enableTag = cfg.enable
    ? '<span class="tag green">' + esc(t("common.enabled")) + '</span>'
    : '<span class="tag gray">' + esc(t("common.disabled")) + '</span>';
  box.innerHTML =
    '<div class="form-row" style="border:none;padding:6px 0">' +
      '<label>' + esc(t("pip_page.index_url")) + '<span class="desc">' + esc(t("pip_page.config_tip")) + '</span></label>' +
      '<div class="ctrl"><div style="padding:10px 0;font-family:monospace;font-size:13px;word-break:break-all">' + esc(cfg.index_url || t("common.not_set")) + '</div></div>' +
    '</div>' +
    '<div class="form-row" style="border:none;padding:6px 0">' +
      '<label>' + esc(t("common.status")) + '</label>' +
      '<div class="ctrl"><div style="padding:10px 0">' + enableTag + '</div></div>' +
    '</div>';
}

function renderPipTable(pkgs) {
  const grid = document.getElementById("pip-packages-grid");
  if (!grid) return;
  if (!pkgs || !pkgs.length) {
    grid.innerHTML = '<div class="empty-state">' + esc(t("pip_page.no_packages")) + '</div>';
    return;
  }
  grid.innerHTML = pkgs.map((p) => {
    const protectedTag = p.protected
      ? ' <span class="tag gray">' + esc(t("pip_page.protected")) + '</span>'
      : "";
    const uninstallBtn = p.protected
      ? '<button class="btn small ghost" disabled>' + esc(t("pip_page.protected")) + '</button>'
      : '<button class="btn small danger pip-uninstall-btn" data-name="' + esc(p.name) + '">' + esc(t("pip_page.uninstall")) + '</button>';
    return '<div class="pip-pkg-card">' +
      '<span class="pkg-marker" aria-hidden="true"></span>' +
      '<div class="pkg-info">' +
        '<div class="pkg-name">' + esc(p.name) + protectedTag + '</div>' +
        '<div class="pkg-meta">' +
          '<span class="pkg-version">' + esc(p.version || "unknown") + '</span>' +
          uninstallBtn +
        '</div>' +
      '</div>' +
    '</div>';
  }).join("");
}

function queuePipPackageFilter() {
  if (pipPackageFilterTimer) clearTimeout(pipPackageFilterTimer);
  pipPackageFilterTimer = setTimeout(() => {
    pipPackageFilterTimer = null;
    filterPipPackages();
  }, 140);
}

function filterPipPackages(force = false) {
  const input = document.getElementById("pip-search-input");
  const query = (input ? input.value : "").trim().toLowerCase();
  if (!force && query === pipLastRenderedQuery) return;
  pipLastRenderedQuery = query;
  const filtered = !query ? pipPackagesCache : pipPackagesCache.filter((p) =>
    (p.name || "").toLowerCase().includes(query) ||
    (p.version || "").toLowerCase().includes(query)
  );
  requestAnimationFrame(() => {
    if (query === pipLastRenderedQuery) renderPipTable(filtered);
  });
}

async function pipInstall() {
  const input = document.getElementById("pip-install-input");
  const val = (input ? input.value : "").trim();
  if (!val) { toast(t("pip_page.input_empty"), true); return; }
  const packages = val.split(/[\s,]+/).filter(Boolean);
  if (!packages.length) { toast(t("pip_page.input_empty"), true); return; }
  if (input) input.value = "";
  try {
    const { data } = await api("POST", "/api/pip/install", { packages });
    if (data && data.task_id) {
      openPipLogModal(data.task_id);
    } else {
      toast(t("pip_page.install_started"));
      loadPipPackages();
    }
  } catch (e) { toast(t("pip_page.install_failed", { error: e.message }), true); }
}

async function pipUninstall(name) {
  if (!await customConfirm(t("pip_page.uninstall_confirm", { package: name }))) return;
  try {
    const res = await api("POST", "/api/pip/uninstall", { package: name });
    toast(res.msg || t("pip_page.uninstall_success"));
    loadPipPackages();
  } catch (e) { toast(t("pip_page.uninstall_failed", { error: e.message }), true); }
}


function openPipLogModal(taskId) {
  const pre = document.getElementById("pip-log-pre");
  if (pre) pre.textContent = "";
  document.getElementById("pip-log-modal").classList.add("show");
  hideBgRunningIndicator();
  startPipTaskPolling(taskId);
}

function startPipTaskPolling(taskId) {
  if (pipPollTimer) { clearInterval(pipPollTimer); pipPollTimer = null; }
  pipTaskState = {
    taskId, done: false, success: false, subpluginName: "",
    status: "running", msg: "", doneHandled: false, reloadShown: false,
    reloadRequired: false, failCount: 0,
  };
  updatePipLogStatus();
  pollPipTask();
  pipPollTimer = setInterval(pollPipTask, 800);
}

async function pollPipTask() {
  if (!pipTaskState) return;
  /* 入口捕获任务身份，await 期间若 pipTaskState 被新任务覆盖则放弃本次结果 */
  const myState = pipTaskState;
  const myTaskId = pipTaskState.taskId;
  try {
    const { data } = await api("GET", "/api/pip/task/" + myTaskId);
    if (!pipTaskState || pipTaskState !== myState) return;
    myState.failCount = 0;
    const lines = Array.isArray(data.log_lines) ? data.log_lines : [];
    const pre = document.getElementById("pip-log-pre");
    if (pre) {
      pre.textContent = lines.join("\n");
      const isNearBottom = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 50;
      if (isNearBottom) pre.scrollTop = pre.scrollHeight;
    }
    /* pip 任务可选进度条：后端返回 progress 字段时显示，否则隐藏 */
    const pwrap = document.getElementById("pip-progress-wrap");
    if (pwrap) {
      const pct = data.progress || 0;
      if (pct > 0 || data.progress_label) {
        pwrap.style.display = "";
        const bar = document.getElementById("pip-progress-bar");
        if (bar) bar.style.width = (pct || 0) + "%";
        const lbl = document.getElementById("pip-progress-label");
        if (lbl) lbl.textContent = data.progress_label || "";
        const pctEl = document.getElementById("pip-progress-pct");
        if (pctEl) pctEl.textContent = pct > 0 ? pct + "%" : "";
      }
    }
    myState.status = data.status || "running";
    myState.done = !!data.done;
    myState.success = !!data.success;
    myState.subpluginName = data.subplugin_name || "";
    myState.installationSuccess = !!data.installation_success;
    myState.reloadSuccess = data.reload_success;
    myState.reloadRequired = !!data.reload_required;
    myState.msg = data.msg || "";
    updatePipLogStatus();
    if (data.done && !myState.doneHandled) {
      myState.doneHandled = true;
      if (pipPollTimer) { clearInterval(pipPollTimer); pipPollTimer = null; }
      onPipTaskDone();
    }
  } catch (e) {
    if (!pipTaskState || pipTaskState !== myState) return;
    const emsg = String((e && e.message) || "");
    /* 鉴权失败：清理轮询，避免在 logout 后形成无限循环（TOKEN 已被 logout 清空即为鉴权失败） */
    if (!TOKEN || emsg.indexOf("login_expired") !== -1 || emsg.indexOf("401") !== -1) {
      if (pipPollTimer) { clearInterval(pipPollTimer); pipPollTimer = null; }
      return;
    }
    myState.failCount = (myState.failCount || 0) + 1;
    if (myState.failCount > 10) {
      if (pipPollTimer) { clearInterval(pipPollTimer); pipPollTimer = null; }
      toast(t("pip_log_modal.poll_failed"), true);
    }
  }
}

function updatePipLogStatus() {
  const statusEl = document.getElementById("pip-log-status");
  const bgBtn = document.getElementById("pip-log-bg-btn");
  if (!pipTaskState || !statusEl) return;
  if (!pipTaskState.done) {
    statusEl.textContent = t("pip_log_modal.installing");
    statusEl.className = "tag blue";
    if (bgBtn) bgBtn.style.display = "";
  } else if (pipTaskState.success) {
    statusEl.textContent = t("pip_log_modal.success");
    statusEl.className = "tag green";
    if (bgBtn) bgBtn.style.display = "none";
  } else {
    statusEl.textContent = t("pip_log_modal.failed");
    statusEl.className = "tag red";
    if (bgBtn) bgBtn.style.display = "none";
  }
}

function onPipTaskDone() {
  const modal = document.getElementById("pip-log-modal");
  const myState = pipTaskState;
  if (currentPage === "packages") loadPipPackages();
  if (modal.classList.contains("show")) {
    if (myState && myState.success) {
      setTimeout(() => {
        if (pipTaskState === myState && pipTaskState.done && pipTaskState.success &&
            document.getElementById("pip-log-modal").classList.contains("show")) {
          closePipLogModal();
        }
      }, 3000);
    }
  } else {
    if (pipTaskState) {
      toast(pipTaskState.success ? t("pip_log_modal.success") : t("pip_log_modal.failed"), !pipTaskState.success);
      hideBgRunningIndicator();
      if (pipTaskState.success && pipTaskState.subpluginName && pipTaskState.reloadRequired && !pipTaskState.reloadShown) {
        pipTaskState.reloadShown = true;
        showReloadPrompt("reload_prompt.dependencies_installed", pipTaskState.subpluginName);
      }
    }
  }
}

function closePipLogModal() {
  const modal = document.getElementById("pip-log-modal");
  modal.classList.remove("show");
  if (!pipTaskState) return;
  if (!pipTaskState.done) {
    showBgRunningIndicator();
  } else {
    hideBgRunningIndicator();
    if (pipTaskState.success && pipTaskState.subpluginName && pipTaskState.reloadRequired && !pipTaskState.reloadShown) {
      pipTaskState.reloadShown = true;
      showReloadPrompt("reload_prompt.dependencies_installed", pipTaskState.subpluginName);
    }
  }
}

function backgroundPipLog() {
  closePipLogModal();
}

function reopenPipLog() {
  if (!pipTaskState) return;
  const pre = document.getElementById("pip-log-pre");
  if (pre) pre.textContent = "";
  document.getElementById("pip-log-modal").classList.add("show");
  hideBgRunningIndicator();
  pollPipTask();
  if (pipTaskState && !pipTaskState.done && !pipPollTimer) {
    pipPollTimer = setInterval(pollPipTask, 800);
  }
}

function showBgRunningIndicator() {
  const btn = document.getElementById("bg-running-btn");
  if (btn) btn.style.display = "flex";
}

function hideBgRunningIndicator() {
  const btn = document.getElementById("bg-running-btn");
  if (btn) btn.style.display = "none";
}


function showReloadPrompt(messageKey, subpluginName) {
  reloadPromptState = {
    subpluginName: subpluginName || "",
    isConfig: messageKey === "reload_prompt.message_config",
  };
  const msgEl = document.getElementById("reload-prompt-message");
  if (subpluginName) {
    msgEl.textContent = t(messageKey, { name: subpluginName });
  } else {
    msgEl.textContent = t(messageKey);
  }
  const nowBtn = document.getElementById("reload-prompt-now");
  nowBtn.textContent = t("reload_prompt.execute_now");
  nowBtn.disabled = false;
  document.getElementById("reload-prompt-later").disabled = false;
  document.getElementById("reload-prompt-modal").classList.add("show");
}

function closeReloadPrompt() {
  document.getElementById("reload-prompt-modal").classList.remove("show");
}

function reloadPromptLater() {
  if (reloadPromptState && reloadPromptState.isConfig) {
    localStorage.setItem("lumen_pending_reload", "1");
  }
  closeReloadPrompt();
}

async function executeReloadNow() {
  if (!reloadPromptState) return;
  const nowBtn = document.getElementById("reload-prompt-now");
  const laterBtn = document.getElementById("reload-prompt-later");
  nowBtn.textContent = t("reload_prompt.reloading");
  nowBtn.disabled = true;
  laterBtn.disabled = true;
  try {
    let res;
    if (reloadPromptState.subpluginName) {
      res = await api("POST", "/api/subplugins/" + encodeURIComponent(reloadPromptState.subpluginName) + "/reload");
    } else {
      res = await api("POST", "/api/reload");
    }
    nowBtn.textContent = t("reload_prompt.reload_success");
    toast(res.msg || t("reload_prompt.reload_success"));
    localStorage.removeItem("lumen_pending_reload");
    setTimeout(() => {
      closeReloadPrompt();
      nowBtn.disabled = false;
      laterBtn.disabled = false;
      if (reloadPromptState && reloadPromptState.subpluginName && currentPage === "subplugins") loadSubplugins();
    }, 1200);
  } catch (e) {
    nowBtn.textContent = t("reload_prompt.reload_failed");
    toast(t("reload_prompt.reload_failed", { error: e.message }), true);
    setTimeout(() => {
      nowBtn.textContent = t("reload_prompt.execute_now");
      nowBtn.disabled = false;
      laterBtn.disabled = false;
    }, 1500);
  }
}


function installSubpluginDeps(name) {
  editingDepsPlugin = name;
  const deps = subpluginDepsCache[name] || [];
  const msgEl = document.getElementById("install-deps-message");
  msgEl.textContent = t("subplugins.install_deps_question", { name, deps: deps.join(" ") });
  document.getElementById("manual-install-wrap").style.display = "none";
  document.getElementById("install-deps-modal").classList.add("show");
}

async function autoInstallDeps() {
  const name = editingDepsPlugin;
  if (!name) return;
  closeModal("install-deps-modal");
  try {
    const { data } = await api("POST", "/api/subplugins/" + encodeURIComponent(name) + "/install-deps");
    if (data && data.task_id) {
      openPipLogModal(data.task_id);
    } else {
      toast(t("pip_page.install_started"));
      loadSubplugins();
    }
  } catch (e) { toast(t("pip_page.install_failed", { error: e.message }), true); }
}

function showManualInstall() {
  const name = editingDepsPlugin;
  const deps = subpluginDepsCache[name] || [];
  // Endstone Python 通常由 uv 管理；采用与自动安装相同的 PEP 668 兼容命令。
  const cmd = "uv pip install --system --break-system-packages -- " + deps.join(" ");
  document.getElementById("manual-install-desc").textContent = t("subplugins.manual_install_next_step", { name });
  document.getElementById("manual-install-cmd").textContent = cmd;
  document.getElementById("manual-install-wrap").style.display = "block";
}

function copyManualInstallCmd() {
  const cmd = document.getElementById("manual-install-cmd").textContent;
  copyToClipboard(cmd);
  toast(t("subplugins.manual_command_copied"));
}

function copySubpluginError(name) {
  const err = subpluginErrorCache[name] || "";
  copyToClipboard(err);
  toast(t("subplugins.error_copied"));
}

function toggleSubpluginErrorDetail(name) {
  // 通过 data-sp-name 定位卡片（⋯ 菜单在卡片外部，不能再用 closest）
  const card = document.querySelector(`.subplugin-card[data-sp-name="${cssEscape(name)}"]`);
  if (!card) return;
  const detail = card.querySelector(".error-detail");
  if (!detail) return;
  const isHidden = detail.style.display === "none";
  detail.style.display = isHidden ? "block" : "none";
}


function scrollToConfigSection(key) {
  const el = document.getElementById("config-section-" + key);
  if (el) el.scrollIntoView({ behavior: "smooth", block: "start" });
}

function initConfigNavSpy() {
  if (configNavObserver) { configNavObserver.disconnect(); configNavObserver = null; }
  const nav = document.getElementById("config-nav");
  if (!nav) return;
  const buttons = nav.querySelectorAll(".config-nav-btn");
  const keys = ["connection", "sync", "whitelist", "regex_engine", "webui", "background", "language", "pip", "commands", "marketplace", "updates"];
  const sections = keys
    .map((k) => document.getElementById("config-section-" + k))
    .filter(Boolean);
  if (!sections.length) return;
  configNavObserver = new IntersectionObserver((entries) => {
    entries.forEach((entry) => {
      if (entry.isIntersecting) {
        const id = entry.target.id.replace("config-section-", "");
        buttons.forEach((b) => {
          const isActive = b.dataset.target === id;
          b.classList.toggle("active", isActive);
          if (isActive) b.scrollIntoView({ behavior: "smooth", block: "nearest", inline: "center" });
        });
      }
    });
  }, { rootMargin: "-80px 0px -70% 0px", threshold: 0 });
  sections.forEach((s) => configNavObserver.observe(s));
}


function flattenConfig(obj, prefix, out) {
  out = out || {};
  for (const [k, v] of Object.entries(obj || {})) {
    const path = prefix ? prefix + "." + k : k;
    if (v !== null && typeof v === "object" && !Array.isArray(v)) {
      flattenConfig(v, path, out);
    } else {
      out[path] = v;
    }
  }
  return out;
}

function configNeedsReload(oldCfg, newCfg) {
  const oldFlat = flattenConfig(oldCfg);
  const newFlat = flattenConfig(newCfg);
  const allPaths = new Set([...Object.keys(oldFlat), ...Object.keys(newFlat)]);
  for (const path of allPaths) {
    if (JSON.stringify(oldFlat[path]) === JSON.stringify(newFlat[path])) continue;
    for (const prefix of CONFIG_RELOAD_PREFIXES) {
      if (path.startsWith(prefix)) return true;
    }
    if (CONFIG_RELOAD_EXACT.includes(path)) return true;
  }
  return false;
}


(async function init() {
  loadBackground();
  await loadI18n(localStorage.getItem("lumen_lang") || "auto");
  if (TOKEN) {
    try {
      await api("GET", "/api/overview");
      showApp();
      if (localStorage.getItem("lumen_pending_reload")) {
        localStorage.removeItem("lumen_pending_reload");
        showReloadPrompt("reload_prompt.message_config", "");
      }
      return;
    } catch (e) { /* token 失效，走登录 */ }
  }
  document.getElementById("login-view").style.display = "flex";
})();

let dashboardRefreshTimer = null;
function startDashboardRefresh() {
  if (dashboardRefreshTimer) return;
  dashboardRefreshTimer = setInterval(() => {
    const dash = document.getElementById("page-dashboard");
    if (TOKEN && dash && dash.style.display !== "none" &&
        document.getElementById("app-view").style.display !== "none" &&
        !document.hidden) {
      loadDashboard();
    }
  }, 10000);
}
startDashboardRefresh();

window.addEventListener("scroll", () => {
  const btn = document.getElementById("back-to-top");
  if (!btn) return;
  btn.classList.toggle("show", window.scrollY > 300);
}, { passive: true });


let marketTaskTimer = null;
let frameworkUpdateTimer = null;

async function loadMarketplacePage() {
  const status = document.getElementById("marketplace-status");
  const list = document.getElementById("marketplace-list");
  status.textContent = t("marketplace.loading_config");
  list.innerHTML = "";
  try {
    const res = await api("GET", "/api/market/config");
    const cfg = res.data || {};
    if (!cfg.enabled) {
      status.textContent = cfg.configured
        ? t("marketplace.disabled_hint")
        : t("marketplace.unconfigured_hint");
      return;
    }
    status.textContent = t("marketplace.secure_hint");
    await loadMarketplace();
  } catch (e) {
    status.textContent = t("marketplace.secure_hint");
    toast(e.message || t("marketplace.load_failed"), true);
  }
}

async function loadMarketplace() {
  const list = document.getElementById("marketplace-list");
  const status = document.getElementById("marketplace-status");
  const query = document.getElementById("marketplace-search").value.trim();
  const sortWrap = document.getElementById("marketplace-sort-wrap");
  const sort = (sortWrap && sortWrap.dataset.value) || "score";
  status.textContent = t("marketplace.loading");
  list.innerHTML = `<div style="color:var(--muted);padding:12px">${esc(t("marketplace.loading"))}</div>`;
  try {
    const res = await api("GET", `/api/market/plugins?limit=48&q=${encodeURIComponent(query)}&sort=${encodeURIComponent(sort)}`);
    const data = res.data || {};
    const items = Array.isArray(data.items) ? data.items : [];
    status.textContent = t("marketplace.results", { count: data.total || items.length });
    if (!items.length) {
      list.innerHTML = `<div style="color:var(--muted);padding:12px">${esc(t("marketplace.empty"))}</div>`;
      return;
    }
    list.innerHTML = items.map((item) => {
      const title = item.title || item.id || "";
      const initial = String(title).trim().charAt(0).toUpperCase() || "?";
      // 封面走后端代理：浏览器直连市场站点会因混合内容/防盗链 Cookie 失败
      const cover = item.cover_url
        ? `<img src="/api/market/cover?url=${encodeURIComponent(item.cover_url)}&token=${encodeURIComponent(TOKEN)}" alt="" loading="lazy" referrerpolicy="no-referrer" onerror="this.remove()">`
        : "";
      const tags = Array.isArray(item.tags) ? item.tags.slice(0, 4).map((tag) => `<span class="tag gray">${esc(tag)}</span>`).join("") : "";
      return `<div class="market-card glass" data-id="${esc(item.id || "")}" style="cursor:pointer">
        <div class="market-avatar">${cover || esc(initial)}</div>
        <div class="market-main">
          <div class="market-title-row">
            <div class="market-name" title="${esc(title)}">${esc(title)}</div>
          </div>
          <div class="market-meta">${esc(item.author || "")} · v${esc(item.latest_version || "?")}</div>
          <div class="market-desc">${esc(item.summary || "")}</div>
          ${tags ? `<div class="market-tags">${tags}</div>` : ""}
          <div class="market-footer">
            <div class="market-stats">
              <span class="stat">↓ ${esc(item.download_count || 0)}</span>
              <button class="stat market-like-btn${item.liked ? " liked" : ""}" data-id="${esc(item.id || "")}" data-liked="${item.liked ? "1" : "0"}" title="${esc(t("marketplace.like_button"))}">♥ <span class="like-count">${esc(item.like_count || 0)}</span></button>
              <span class="stat">${esc(t("marketplace.score_label"))} ${esc(item.score || 0)}</span>
            </div>
            <div class="market-actions">
              <button class="btn small ghost" data-id="${esc(item.id || "")}">${esc(t("marketplace.detail_button"))}</button>
              <button class="btn small market-install-btn" data-id="${esc(item.id || "")}">${esc(t("marketplace.install_button"))}</button>
            </div>
          </div>
        </div>
      </div>`;
    }).join("");
  } catch (e) {
    status.textContent = t("marketplace.secure_hint");
    list.innerHTML = "";
    toast(e.message || t("marketplace.load_failed"), true);
  }
}

/* ─── 统一任务日志弹窗（市场任务 / 框架更新共用） ─── */
let taskLogState = null;

function openTaskLogModal(taskId, title) {
  const pre = document.getElementById("task-log-pre");
  if (pre) pre.textContent = "";
  const bar = document.getElementById("task-progress-bar");
  if (bar) bar.style.width = "0%";
  const label = document.getElementById("task-progress-label");
  if (label) label.textContent = "";
  const pct = document.getElementById("task-progress-pct");
  if (pct) pct.textContent = "";
  const titleEl = document.getElementById("task-log-title");
  if (titleEl && title) titleEl.textContent = title;
  const status = document.getElementById("task-log-status");
  if (status) { status.textContent = t("task_log_modal.running"); status.className = "tag blue"; }
  document.getElementById("task-log-modal").classList.add("show");
  taskLogState = { taskId, done: false, success: false, msg: "", doneHandled: false };
}

function closeTaskLogModal() {
  document.getElementById("task-log-modal").classList.remove("show");
}

function updateTaskLogModal(task) {
  const pre = document.getElementById("task-log-pre");
  if (pre && Array.isArray(task.log_lines)) {
    pre.textContent = task.log_lines.join("\n");
    const isNearBottom = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 50;
    if (isNearBottom) pre.scrollTop = pre.scrollHeight;
  }
  const bar = document.getElementById("task-progress-bar");
  const label = document.getElementById("task-progress-label");
  const pct = document.getElementById("task-progress-pct");
  const progress = task.progress || 0;
  if (bar) bar.style.width = progress + "%";
  if (pct) pct.textContent = progress > 0 ? progress + "%" : "";
  if (label) label.textContent = task.progress_label || "";
  const status = document.getElementById("task-log-status");
  if (status) {
    if (task.done) {
      if (task.success) {
        status.textContent = t("task_log_modal.success");
        status.className = "tag green";
        if (bar) bar.style.width = "100%";
        if (pct) pct.textContent = "100%";
      } else {
        status.textContent = t("task_log_modal.failed");
        status.className = "tag red";
      }
    } else {
      status.textContent = t("task_log_modal.running");
      status.className = "tag blue";
    }
  }
}

function watchMarketTask(taskId, successMessage, onDone) {
  if (marketTaskTimer) clearInterval(marketTaskTimer);
  openTaskLogModal(taskId, t("task_log_modal.market_task"));
  let running = false;
  const poll = async () => {
    if (running) return;
    running = true;
    try {
      const res = await api("GET", "/api/market/task/" + encodeURIComponent(taskId));
      const task = res.data || {};
      updateTaskLogModal(task);
      if (!task.done) return;
      clearInterval(marketTaskTimer); marketTaskTimer = null;
      if (task.success) {
        toast(successMessage || task.msg || t("marketplace.task_success"));
        loadSubplugins(); loadCustomPages();
        setTimeout(() => { if (taskLogState && !taskLogState.doneHandled) closeTaskLogModal(); }, 2000);
      } else {
        toast(task.msg || t("marketplace.task_failed"), true);
      }
      if (taskLogState) taskLogState.doneHandled = true;
      if (typeof onDone === "function") { try { onDone(!!task.success, task); } catch (_) { /* 回调异常不影响任务流程 */ } }
    } catch (e) {
      clearInterval(marketTaskTimer); marketTaskTimer = null;
      toast(e.message || t("marketplace.task_failed"), true);
      if (typeof onDone === "function") { try { onDone(false, null); } catch (_) { /* ignore */ } }
    } finally { running = false; }
  };
  marketTaskTimer = setInterval(poll, 800);
  poll();
}

async function installMarketPlugin(marketId, version) {
  if (!await customConfirm(t("marketplace.install_confirm", { id: marketId }))) return;
  try {
    toast(t("marketplace.installing"));
    const payload = { id: marketId };
    if (version) payload.version = version;
    const res = await api("POST", "/api/market/install", payload);
    watchMarketTask(res.data.task_id, t("marketplace.install_success"));
  } catch (e) { toast(e.message || t("marketplace.install_failed"), true); }
}

let marketDetailCurrent = null;

function currentMarketDetailId() {
  return marketDetailCurrent ? marketDetailCurrent.id : "";
}

function installFromMarketDetail() {
  const detail = marketDetailCurrent;
  if (!detail) return;
  installMarketPlugin(detail.id, "");
}

async function openMarketDetail(marketId) {
  const modal = document.getElementById("market-detail-modal");
  const content = document.getElementById("market-detail-content");
  const installBtn = document.getElementById("market-detail-install-btn");
  if (!modal || !content) return;
  marketDetailCurrent = null;
  content.innerHTML = `<div style="color:var(--muted);padding:12px 0">${esc(t("marketplace.detail_loading"))}</div>`;
  if (installBtn) installBtn.disabled = true;
  modal.classList.add("show");
  try {
    const res = await api("GET", "/api/market/plugin/" + encodeURIComponent(marketId));
    const detail = res.data || {};
    marketDetailCurrent = detail;
    renderMarketDetail(detail);
    if (installBtn) installBtn.disabled = false;
  } catch (e) {
    content.innerHTML = `<div style="color:var(--muted);padding:12px 0">${esc(e.message || t("marketplace.detail_failed"))}</div>`;
  }
}

function formatRequiresMeta(v) {
  // 市场版本的插件级强制依赖：子插件依赖安装时自动装齐，Endstone 插件需自行安装
  const requires = (v && v.requires) || {};
  const sub = Array.isArray(requires.subplugins) ? requires.subplugins : [];
  const es = Array.isArray(requires.endstone) ? requires.endstone : [];
  if (!sub.length && !es.length) return "";
  const parts = [];
  if (sub.length) parts.push(`${esc(t("marketplace.detail_requires_subplugins"))}: ${esc(sub.join(", "))}`);
  if (es.length) parts.push(`${esc(t("marketplace.detail_requires_endstone"))}: ${esc(es.join(", "))}`);
  return `<div class="ver-meta">${parts.join(" · ")}</div>`;
}

function renderMarketDetail(detail) {
  const content = document.getElementById("market-detail-content");
  if (!content) return;
  const title = detail.title || detail.id || "";
  const initial = String(title).trim().charAt(0).toUpperCase() || "?";
  const cover = detail.cover_url
    ? `<img src="/api/market/cover?url=${encodeURIComponent(detail.cover_url)}&token=${encodeURIComponent(TOKEN)}" alt="" referrerpolicy="no-referrer" onerror="this.remove()">`
    : "";
  const tags = Array.isArray(detail.tags) && detail.tags.length
    ? `<div class="market-tags">${detail.tags.map((tag) => `<span class="tag gray">${esc(tag)}</span>`).join("")}</div>`
    : "";
  const versions = Array.isArray(detail.versions) ? detail.versions : [];
  const versionHtml = versions.map((v) => {
    const deps = Array.isArray(v.dependencies) && v.dependencies.length
      ? `<div class="ver-meta">${esc(t("marketplace.detail_dependencies"))}: ${esc(v.dependencies.join(", "))}</div>`
      : "";
    const reqs = formatRequiresMeta(v);
    const minLb = v.min_lumenbridge ? `<div class="ver-meta">${esc(t("marketplace.detail_min_lb"))}: ${esc(v.min_lumenbridge)}</div>` : "";
    const date = v.published_at ? String(v.published_at).replace("T", " ").slice(0, 16) : "";
    return `<div class="market-version-item">
      <div style="flex:1;min-width:0">
        <span class="ver">v${esc(v.version || "?")}</span>
        ${date ? ` <span class="ver-meta">${esc(date)}</span>` : ""}
        ${deps}${reqs}${minLb}
        ${v.changelog ? `<div class="ver-changelog">${esc(v.changelog)}</div>` : ""}
      </div>
      <div class="ver-install">
        <button class="btn small market-ver-install-btn" data-id="${esc(detail.id || "")}" data-version="${esc(v.version || "")}">${esc(t("marketplace.install_button"))}</button>
      </div>
    </div>`;
  }).join("");
  content.innerHTML = `
    <div class="market-detail-head">
      <div class="market-detail-cover">${cover || esc(initial)}</div>
      <div class="market-detail-info">
        <h4>${esc(title)}</h4>
        <div class="market-meta">${esc(detail.author || "")} · v${esc(detail.latest_version || "?")}${detail.category ? " · " + esc(detail.category) : ""}</div>
        <div class="market-stats">
          <span class="stat">↓ ${esc(detail.download_count || 0)}</span>
          <button class="stat market-like-btn${detail.liked ? " liked" : ""}" data-id="${esc(detail.id || "")}" data-liked="${detail.liked ? "1" : "0"}" title="${esc(t("marketplace.like_button"))}">♥ <span class="like-count">${esc(detail.like_count || 0)}</span></button>
          <span class="stat">${esc(t("marketplace.score_label"))} ${esc(detail.score || 0)}</span>
        </div>
        ${tags}
      </div>
    </div>
    <div class="market-detail-section">
      <h5>${esc(t("marketplace.detail_description"))}</h5>
      <div class="market-detail-desc">${esc(detail.description || detail.summary || "")}</div>
    </div>
    ${versions.length ? `<div class="market-detail-section">
      <h5>${esc(t("marketplace.detail_versions"))}</h5>
      <div class="market-version-list">${versionHtml}</div>
    </div>` : ""}`;
}

async function toggleMarketLike(btn) {
  const id = btn.dataset.id || "";
  if (!id || btn.disabled) return;
  const wanted = btn.dataset.liked !== "1";
  btn.disabled = true;
  try {
    const res = await api("POST", "/api/market/like", { id, liked: wanted });
    const data = res.data || {};
    updateMarketLikeButtons(id, data.liked === true || data.liked === "true", data.like_count);
  } catch (e) {
    toast(e.message || t("marketplace.like_failed"), true);
  } finally {
    btn.disabled = false;
  }
}

function updateMarketLikeButtons(id, liked, count) {
  if (!id) return;
  document.querySelectorAll(`.market-like-btn[data-id="${cssEscape(id)}"]`).forEach((b) => {
    b.dataset.liked = liked ? "1" : "0";
    b.classList.toggle("liked", liked);
    const c = b.querySelector(".like-count");
    if (c && count !== undefined && count !== null) c.textContent = count;
  });
  if (marketDetailCurrent && marketDetailCurrent.id === id) {
    marketDetailCurrent.liked = liked;
    if (count !== undefined && count !== null) marketDetailCurrent.like_count = count;
  }
}

async function reportMarketPlugin(marketId) {
  if (!marketId) return;
  const modal = document.getElementById("report-modal");
  const reasonEl = document.getElementById("report-reason");
  const contactEl = document.getElementById("report-contact");
  const submitBtn = document.getElementById("report-submit-btn");
  if (!modal || !reasonEl || !contactEl || !submitBtn) return;
  reasonEl.parentElement.parentElement.style.display = "";
  contactEl.parentElement.parentElement.style.display = "";
  reasonEl.value = "";
  contactEl.value = "";
  const labels = modal.querySelectorAll(".form-row label");
  if (labels[0]) labels[0].textContent = t("marketplace.report_reason_prompt");
  if (labels[1]) labels[1].textContent = t("marketplace.report_contact_prompt");
  modal.classList.add("show");
  reasonEl.focus();

  // close-x 与取消按钮走同一 cleanup 路径（内联 closeModal 保留，二者共存靠 removed 标志幂等），
  // 避免每次打开弹窗都给 submitBtn 累积新监听器导致重复提交
  const closeX = modal.querySelector(".close-x");
  const cancelBtn = modal.querySelector(".toolbar .btn.ghost");
  let removed = false;
  const onCancel = () => cleanup();
  const cleanup = () => {
    if (removed) return;
    removed = true;
    modal.classList.remove("show");
    submitBtn.removeEventListener("click", onOk);
    modal.removeEventListener("click", onMask);
    if (closeX) closeX.removeEventListener("click", onCancel);
    if (cancelBtn) cancelBtn.removeEventListener("click", onCancel);
  };
  const onOk = () => {
    const reason = reasonEl.value.trim();
    const contact = contactEl.value.trim();
    if (!reason) { reasonEl.focus(); return; }
    cleanup();
    submitReport(marketId, reason, contact);
  };
  const onMask = (e) => { if (e.target === modal) { cleanup(); } };
  submitBtn.addEventListener("click", onOk);
  modal.addEventListener("click", onMask);
  if (closeX) closeX.addEventListener("click", onCancel);
  if (cancelBtn) cancelBtn.addEventListener("click", onCancel);
}

async function submitReport(marketId, reason, contact) {
  try {
    await api("POST", "/api/market/report", { id: marketId, reason, contact });
    toast(t("marketplace.report_success"));
  } catch (e) { toast(e.message || t("marketplace.report_failed"), true); }
}

async function checkMarketUpdates() {
  try {
    toast(t("marketplace.checking"));
    const res = await api("POST", "/api/market/check", {});
    watchMarketCheckTask(res.data.task_id);
  } catch (e) { toast(e.message || t("marketplace.check_failed"), true); }
}

/* 检查更新任务：轮询进度 → 有更新弹"可更新子插件"卡片，无更新弹"均为最新版" */
let marketCheckTimer = null;

function watchMarketCheckTask(taskId) {
  if (marketCheckTimer) clearInterval(marketCheckTimer);
  openTaskLogModal(taskId, t("task_log_modal.market_task"));
  let running = false;
  const poll = async () => {
    if (running) return;
    running = true;
    try {
      const res = await api("GET", "/api/market/task/" + encodeURIComponent(taskId));
      const task = res.data || {};
      updateTaskLogModal(task);
      if (!task.done) return;
      clearInterval(marketCheckTimer); marketCheckTimer = null;
      if (taskLogState) taskLogState.doneHandled = true;
      closeTaskLogModal();
      if (!task.success) {
        toast(task.msg || t("marketplace.check_failed"), true);
        return;
      }
      const updates = (task.result && task.result.updates) || {};
      const pending = Object.entries(updates)
        .filter(([, info]) => info && info.available && info.market_id)
        .sort((a, b) => String(a[0]).localeCompare(String(b[0])));
      if (!pending.length) {
        showAllUpToDateModal(Object.keys(updates).length);
        return;
      }
      openUpdateCenter(pending);
    } catch (e) {
      clearInterval(marketCheckTimer); marketCheckTimer = null;
      toast(e.message || t("marketplace.check_failed"), true);
    } finally { running = false; }
  };
  marketCheckTimer = setInterval(poll, 800);
  poll();
}

/* 全部最新版提示：复用"已是最新版"弹窗（我知道了按钮居右） */
function showAllUpToDateModal(count) {
  const titleEl = document.querySelector("#latest-version-modal .latest-version-title");
  const desc = document.getElementById("latest-version-desc");
  const chip = document.getElementById("latest-version-chip");
  if (titleEl) titleEl.textContent = t("marketplace.all_up_to_date_title");
  if (desc) desc.textContent = t("marketplace.all_up_to_date_desc", { count: Number(count) || 0 });
  if (chip) chip.textContent = "✓ " + t("marketplace.all_up_to_date_chip");
  const modal = document.getElementById("latest-version-modal");
  if (modal) modal.classList.add("show");
}

/* 更新中心：可更新子插件卡片 + 单独更新 + 一键更新全部 */
let updateCenterState = null; // { pending: [[name, info], ...] }

function openUpdateCenter(pending) {
  updateCenterState = { pending: pending.map((entry) => [entry[0], entry[1]]) };
  const list = document.getElementById("update-center-list");
  const summary = document.getElementById("update-center-summary");
  if (summary) summary.textContent = t("marketplace.update_center_summary", { count: pending.length });
  refreshUpdateCenterApplyBtn();
  if (list) {
    list.innerHTML = pending.map(([name, info]) => {
      const current = String((info && info.local_version) || "?");
      const latest = String((info && info.latest_version) || "?");
      return `<div class="spu-card" data-spu-name="${esc(name)}">
        <div class="spu-head-row">
          <span class="spu-name" title="${esc(name)}">${esc(name)}</span>
          <span class="tag gray">v${esc(current)}</span>
          <span class="spu-arrow">→</span>
          <span class="tag blue">v${esc(latest)}</span>
        </div>
        <div class="spu-changelog">${esc(t("marketplace.update_loading_versions"))}</div>
        <div class="spu-actions-row">
          <div class="spu-version-select loading" data-spu-version>${buildSelect("spu-ver-" + esc(name), [{ value: latest, label: "v" + esc(latest) }], latest)}</div>
          <button class="btn small" data-spu-update data-name="${esc(name)}">${esc(t("marketplace.update_card_button"))}</button>
        </div>
      </div>`;
    }).join("");
    // 并行拉取每个插件的可更新版本列表（仅高于当前版本），填充下拉与更新日志
    pending.forEach(([name, info]) => loadSpuVersions(name, info));
  }
  const modal = document.getElementById("update-center-modal");
  if (modal) modal.classList.add("show");
}

function refreshUpdateCenterApplyBtn() {
  const applyBtn = document.getElementById("update-center-apply-btn");
  if (!applyBtn) return;
  const count = updateCenterState ? updateCenterState.pending.length : 0;
  applyBtn.textContent = t("marketplace.update_all_button") + (count ? " (" + count + ")" : "");
  applyBtn.disabled = !count;
}

async function loadSpuVersions(name, info) {
  const marketId = String((info && info.market_id) || "");
  const current = String((info && info.local_version) || "");
  let detail = null;
  try {
    const res = await api("GET", "/api/market/plugin/" + encodeURIComponent(marketId));
    detail = res.data || {};
  } catch (e) { detail = null; }
  const cards = document.querySelectorAll("#update-center-list .spu-card[data-spu-name]");
  const card = Array.from(cards).find((el) => el.dataset.spuName === name);
  if (!card || !updateCenterState) return;
  const changelogEl = card.querySelector(".spu-changelog");
  const select = card.querySelector("[data-spu-version]");
  const versions = detail && Array.isArray(detail.versions) ? detail.versions : [];
  // 只保留版本号大于当前版本的选项（与市场更新弹窗同规则）
  const newer = versions
    .filter((v) => v && typeof v === "object" && compareVersions(String(v.version || ""), current) > 0)
    .sort((a, b) => compareVersions(String(b.version || ""), String(a.version || "")));
  if (changelogEl) {
    const latestEntry = newer[0] || null;
    const text = latestEntry && latestEntry.changelog
      ? String(latestEntry.changelog)
      : (detail && (detail.summary || detail.description)) || "";
    changelogEl.textContent = text || t("marketplace.no_changelog");
  }
  if (select) {
    let opts;
    if (newer.length) {
      opts = newer.map((v) => ({ value: String(v.version || ""), label: "v" + String(v.version || "") }));
    } else {
      // 版本列表拉取失败：至少保留"更新到检查到的最新版"
      const fallback = String((info && info.latest_version) || "");
      opts = [{ value: fallback, label: "v" + (fallback || "?") }];
    }
    // 重建自定义下拉（.lumen-select），替代原生 <select>
    const inner = select.querySelector(".lumen-select");
    const selId = inner ? String(inner.getAttribute("data-select") || "") : "spu-ver";
    select.innerHTML = buildSelect(selId, opts, opts[0].value);
    select.classList.remove("loading");
  }
}

function removeUpdateCenterCard(name) {
  const list = document.getElementById("update-center-list");
  if (list) {
    const cards = list.querySelectorAll(".spu-card[data-spu-name]");
    Array.from(cards).forEach((el) => { if (el.dataset.spuName === name) el.remove(); });
  }
  if (updateCenterState) {
    updateCenterState.pending = updateCenterState.pending.filter(([n]) => n !== name);
    const summary = document.getElementById("update-center-summary");
    if (summary) summary.textContent = t("marketplace.update_center_summary", { count: updateCenterState.pending.length });
    if (!updateCenterState.pending.length) {
      closeModal("update-center-modal");
      updateCenterState = null;
      showAllUpToDateModal(0);
    } else {
      refreshUpdateCenterApplyBtn();
    }
  }
}

async function updateSpuFromCard(btn) {
  const card = btn.closest(".spu-card");
  if (!card) return;
  const name = card.dataset.spuName || "";
  const select = card.querySelector("[data-spu-version] input[type=hidden]");
  const version = select ? String(select.value || "") : "";
  if (!name) return;
  if (!await customConfirm(t("marketplace.update_confirm", { name }))) return;
  card.classList.add("updating");
  btn.disabled = true;
  try {
    const res = await api("POST", `/api/subplugins/${encodeURIComponent(name)}/market-update`, { version });
    watchMarketTask(res.data.task_id, t("marketplace.update_success", { name }), (ok) => {
      if (ok) {
        removeUpdateCenterCard(name);
      } else {
        card.classList.remove("updating");
        btn.disabled = false;
      }
    });
  } catch (e) {
    card.classList.remove("updating");
    btn.disabled = false;
    toast(e.message || t("marketplace.update_failed", { name }), true);
  }
}

async function updateAllSubplugins() {
  if (!updateCenterState || !updateCenterState.pending.length) return;
  const count = updateCenterState.pending.length;
  if (!await customConfirm(t("marketplace.update_all_confirm", { count }))) return;
  const applyBtn = document.getElementById("update-center-apply-btn");
  if (applyBtn) applyBtn.disabled = true;
  document.querySelectorAll("#update-center-list .spu-card").forEach((el) => el.classList.add("updating"));
  try {
    const res = await api("POST", "/api/market/update-all", {});
    watchMarketTask(res.data.task_id, t("marketplace.update_all_success"), () => {
      closeModal("update-center-modal");
      updateCenterState = null;
    });
  } catch (e) {
    if (applyBtn) applyBtn.disabled = false;
    document.querySelectorAll("#update-center-list .spu-card").forEach((el) => el.classList.remove("updating"));
    toast(e.message || t("marketplace.update_all_failed"), true);
  }
}

/* ─── 更新流程：单一更新按钮 → 市场更新 / 手动更新 ─── */
function compareVersions(a, b) {
  const pa = String(a || "").split(".").map((x) => parseInt(x, 10) || 0);
  const pb = String(b || "").split(".").map((x) => parseInt(x, 10) || 0);
  const len = Math.max(pa.length, pb.length);
  for (let i = 0; i < len; i++) {
    const da = pa[i] || 0;
    const db = pb[i] || 0;
    if (da !== db) return da - db;
  }
  return 0;
}

function openUpdateFlow(name) {
  const cached = subpluginMarketCache[name] || {};
  // 非市场来源没有市场更新可选：直接进入手动更新弹窗
  if (!cached.market) { openUpdateModal(name); return; }
  const modal = document.getElementById("update-choice-modal");
  if (modal) {
    modal.dataset.pluginName = name;
    modal.classList.add("show");
  }
}

function updateChoicePluginName() {
  const modal = document.getElementById("update-choice-modal");
  return (modal && modal.dataset.pluginName) || "";
}

function updateChoiceMarket() {
  const name = updateChoicePluginName();
  if (name) startMarketUpdateFlow(name);
}

function updateChoiceManual() {
  const name = updateChoicePluginName();
  closeModal("update-choice-modal");
  if (name) openUpdateModal(name);
}

async function startMarketUpdateFlow(name) {
  const cached = subpluginMarketCache[name] || {};
  const market = cached.market || {};
  const marketId = String(market.id || "");
  const currentVersion = String(cached.version || "");
  const content = document.getElementById("market-update-content");
  const applyBtn = document.getElementById("market-update-apply-btn");
  marketUpdateState = null;
  if (!marketId) { toast(t("marketplace.update_check_failed"), true); return; }
  if (applyBtn) applyBtn.disabled = true;
  if (content) content.innerHTML = `<div style="color:var(--muted);padding:12px 0">${esc(t("marketplace.update_checking"))}</div>`;
  closeModal("update-choice-modal");
  const modal = document.getElementById("market-update-modal");
  if (modal) modal.classList.add("show");
  try {
    const res = await api("GET", "/api/market/plugin/" + encodeURIComponent(marketId));
    const detail = res.data || {};
    const versions = Array.isArray(detail.versions) ? detail.versions : [];
    // 仅保留版本号大于当前版本的选项（可更新到最新或任一更高的历史版本）
    const newer = versions
      .filter((v) => v && typeof v === "object" && compareVersions(String(v.version || ""), currentVersion) > 0)
      .sort((a, b) => compareVersions(String(b.version || ""), String(a.version || "")));
    if (!newer.length) {
      closeModal("market-update-modal");
      showLatestVersionModal(name, currentVersion || String(detail.latest_version || "?"));
      return;
    }
    marketUpdateState = {
      name, marketId, detail, currentVersion,
      selectedVersion: String(newer[0].version || ""),
    };
    renderMarketUpdateContent(detail, currentVersion, newer);
  } catch (e) {
    closeModal("market-update-modal");
    toast(e.message || t("marketplace.update_check_failed"), true);
  }
}

function showLatestVersionModal(name, version) {
  const desc = document.getElementById("latest-version-desc");
  const chip = document.getElementById("latest-version-chip");
  if (desc) {
    // 插件名以占位符注入模板后统一转义，再替换为高亮 span（防 XSS 且保留样式）
    const placeholder = "\u0001";
    const text = t("marketplace.already_latest_desc", { name: placeholder, version: String(version || "?") });
    desc.innerHTML = esc(text).split(placeholder).join(`<span class="lv-name">${esc(name)}</span>`);
  }
  if (chip) chip.textContent = "v" + String(version || "?");
  const modal = document.getElementById("latest-version-modal");
  if (modal) modal.classList.add("show");
}

function renderMarketUpdateContent(detail, currentVersion, newerVersions) {
  const content = document.getElementById("market-update-content");
  const applyBtn = document.getElementById("market-update-apply-btn");
  if (!content) return;
  const title = detail.title || detail.id || "";
  const initial = String(title).trim().charAt(0).toUpperCase() || "?";
  const cover = detail.cover_url
    ? `<img src="/api/market/cover?url=${encodeURIComponent(detail.cover_url)}&token=${encodeURIComponent(TOKEN)}" alt="" referrerpolicy="no-referrer" onerror="this.remove()">`
    : "";
  const tags = Array.isArray(detail.tags) && detail.tags.length
    ? `<div class="market-tags">${detail.tags.map((tag) => `<span class="tag gray">${esc(tag)}</span>`).join("")}</div>`
    : "";
  const versionHtml = newerVersions.map((v, idx) => {
    const selected = idx === 0 ? " selected" : "";
    const deps = Array.isArray(v.dependencies) && v.dependencies.length
      ? `<div class="ver-meta">${esc(t("marketplace.detail_dependencies"))}: ${esc(v.dependencies.join(", "))}</div>`
      : "";
    const reqs = formatRequiresMeta(v);
    const minLb = v.min_lumenbridge ? `<div class="ver-meta">${esc(t("marketplace.detail_min_lb"))}: ${esc(v.min_lumenbridge)}</div>` : "";
    const date = v.published_at ? String(v.published_at).replace("T", " ").slice(0, 16) : "";
    return `<div class="market-ver-item${selected}" data-version="${esc(v.version || "")}">
      <span class="ver-radio"></span>
      <div class="ver-body">
        <span class="ver">v${esc(v.version || "?")}</span>
        ${date ? ` <span class="ver-meta">${esc(date)}</span>` : ""}
        ${deps}${reqs}${minLb}
        ${v.changelog ? `<div class="ver-changelog">${esc(v.changelog)}</div>` : ""}
      </div>
    </div>`;
  }).join("");
  content.innerHTML = `
    <div class="market-detail-head">
      <div class="market-detail-cover">${cover || esc(initial)}</div>
      <div class="market-detail-info">
        <h4>${esc(title)}</h4>
        <div class="market-update-current">
          <span>${esc(t("marketplace.update_current_version"))}</span>
          <span class="tag gray">v${esc(currentVersion || "?")}</span>
          <span>→</span>
          <span>${esc(t("marketplace.update_target_version"))}</span>
          <span class="tag blue" id="market-update-target-ver">v${esc(marketUpdateState ? marketUpdateState.selectedVersion : "")}</span>
        </div>
        <div class="market-stats">
          <span class="stat">↓ ${esc(detail.download_count || 0)}</span>
          <button class="stat market-like-btn${detail.liked ? " liked" : ""}" data-id="${esc(detail.id || "")}" data-liked="${detail.liked ? "1" : "0"}" title="${esc(t("marketplace.like_button"))}">♥ <span class="like-count">${esc(detail.like_count || 0)}</span></button>
          <span class="stat">${esc(t("marketplace.score_label"))} ${esc(detail.score || 0)}</span>
        </div>
        ${tags}
      </div>
    </div>
    <div class="market-detail-section">
      <h5>${esc(t("marketplace.detail_description"))}</h5>
      <div class="market-detail-desc">${esc(detail.description || detail.summary || "")}</div>
    </div>
    <div class="market-detail-section">
      <h5>${esc(t("marketplace.update_pick_version"))}</h5>
      <div class="market-version-list">${versionHtml}</div>
    </div>`;
  if (applyBtn) applyBtn.disabled = !marketUpdateState || !marketUpdateState.selectedVersion;
}

function selectMarketVersion(version) {
  if (!marketUpdateState) return;
  marketUpdateState.selectedVersion = version;
  document.querySelectorAll("#market-update-content .market-ver-item").forEach((el) => {
    el.classList.toggle("selected", el.dataset.version === version);
  });
  const target = document.getElementById("market-update-target-ver");
  if (target) target.textContent = "v" + version;
  const applyBtn = document.getElementById("market-update-apply-btn");
  if (applyBtn) applyBtn.disabled = !version;
}

async function applyMarketUpdate() {
  if (!marketUpdateState || !marketUpdateState.selectedVersion) return;
  const name = marketUpdateState.name;
  const version = marketUpdateState.selectedVersion;
  try {
    const res = await api("POST", `/api/subplugins/${encodeURIComponent(name)}/market-update`, { version });
    closeModal("market-update-modal");
    marketUpdateState = null;
    watchMarketTask(res.data.task_id, t("marketplace.update_success", { name }));
  } catch (e) { toast(e.message || t("marketplace.update_failed", { name }), true); }
}

async function updateSubpluginDependencies(name) {
  if (!await customConfirm(t("marketplace.deps_update_confirm", { name }))) return;
  try {
    const res = await api("POST", `/api/subplugins/${encodeURIComponent(name)}/update-deps`, {});
    watchMarketTask(res.data.task_id, t("marketplace.deps_update_success", { name }));
  } catch (e) { toast(e.message || t("marketplace.deps_update_failed", { name }), true); }
}

async function installSubpluginRequirements(name) {
  // 插件级强制依赖补装：子插件依赖会从市场自动安装（像 pip 依赖那样），
  // Endstone 插件依赖只能提示用户自行安装
  if (!await customConfirm(t("subplugins.install_requirements_confirm", { name }))) return;
  try {
    const res = await api("POST", `/api/subplugins/${encodeURIComponent(name)}/install-requirements`, {});
    watchMarketTask(res.data.task_id, t("subplugins.install_requirements_success", { name }));
  } catch (e) { toast(e.message || t("subplugins.install_requirements_failed", { name, error: e.message }), true); }
}


async function checkFrameworkUpdate() {
  const content = document.getElementById("framework-update-content");
  if (!content) return;
  content.textContent = t("marketplace.framework_checking");
  try {
    const res = await api("GET", "/api/updates/check");
    const data = res.data || {};
    if (!data.configured) {
      content.textContent = t("marketplace.framework_unconfigured");
      return;
    }
    if (!data.available) {
      content.textContent = t("marketplace.framework_latest", { version: data.current_version || "?" });
      return;
    }
    const latest = data.latest || {};
    const version = latest.version || "?";
    content.innerHTML = `${esc(t("marketplace.framework_available", { version }))} <button class="btn small" style="margin-left:8px" onclick="applyFrameworkUpdate()">${esc(t("marketplace.framework_apply_button"))}</button><div style="margin-top:7px;color:var(--muted)">${esc(t("marketplace.framework_reload_note"))}</div>`;
  } catch (e) {
    content.textContent = e.message || t("marketplace.framework_check_failed");
  }
}

async function applyFrameworkUpdate() {
  if (!await customConfirm(t("marketplace.framework_apply_confirm"))) return;
  const content = document.getElementById("framework-update-content");
  if (content) content.textContent = t("marketplace.framework_applying");
  try {
    const res = await api("POST", "/api/updates/apply", {});
    watchFrameworkApplyTask(res.data.task_id);
  } catch (e) {
    if (content) content.textContent = "";
    toast(e.message || t("marketplace.framework_apply_failed"), true);
  }
}

// 轮询热重载任务：任务完成 = 新 wheel 已就绪且热重载已调度。
// 阶段 1 通过 task-log-modal 展示下载/校验/热重载日志与进度条；
// 随后插件禁用自身、WebUI 短暂下线，新实例启动后自动恢复——
// 阶段 2 反复探测 /api/overview，恢复后刷新页面加载新版本面板。
function watchFrameworkApplyTask(taskId) {
  if (frameworkUpdateTimer) clearInterval(frameworkUpdateTimer);
  openTaskLogModal(taskId, t("task_log_modal.framework_update"));
  let phase = "task";
  let running = false;
  frameworkUpdateTimer = setInterval(async () => {
    if (running) return;
    running = true;
    try {
      if (phase === "task") {
        const res = await api("GET", "/api/market/task/" + encodeURIComponent(taskId));
        const task = res.data || {};
        updateTaskLogModal(task);
        if (!task.done) return;
        if (!task.success) {
          clearInterval(frameworkUpdateTimer); frameworkUpdateTimer = null;
          toast(task.msg || t("marketplace.framework_apply_failed"), true);
          checkFrameworkUpdate();
          return;
        }
        phase = "revive";
        const status = document.getElementById("task-log-status");
        if (status) { status.textContent = t("task_log_modal.reloading"); status.className = "tag blue"; }
        toast(t("marketplace.framework_reload_started"));
        return;
      }
      // 阶段 2：等待新实例的 WebUI 恢复（轮询会在下线期间持续报网络错误）
      await api("GET", "/api/overview");
      clearInterval(frameworkUpdateTimer); frameworkUpdateTimer = null;
      toast(t("marketplace.framework_reload_done"));
      setTimeout(() => location.reload(), 800);
    } catch (e) {
      if (phase === "task") {
        clearInterval(frameworkUpdateTimer); frameworkUpdateTimer = null;
        toast(e.message || t("marketplace.framework_apply_failed"), true);
      }
      // revive 阶段：面板尚未恢复属预期，继续等待
    } finally { running = false; }
  }, 1500);
  // 兜底：热重载异常卡死时 120 秒后停止轮询，避免定时器泄漏
  setTimeout(() => {
    if (frameworkUpdateTimer) { clearInterval(frameworkUpdateTimer); frameworkUpdateTimer = null; }
  }, 120000);
}

/* ================================ 连接配置：适配器卡片（v1.2.0） ================================ */

let connectionsData = [];   // /api/connections 返回的适配器快照
let connectionsStatus = []; // 各适配器运行状态
let editingAdapterId = "";  // 编辑弹窗当前适配器 id
let configPane = "basic";   // basic | connections | filter

const ADAPTER_WIFI_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12.55a11 11 0 0 1 14.08 0"/><path d="M1.42 9a16 16 0 0 1 21.16 0"/><path d="M8.53 16.11a6 6 0 0 1 6.95 0"/><circle cx="12" cy="20" r="1" fill="currentColor"/></svg>';
const ADAPTER_BOT_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="4" y="8" width="16" height="12" rx="3"/><path d="M12 8V4"/><circle cx="12" cy="3" r="1" fill="currentColor"/><circle cx="9" cy="14" r="1" fill="currentColor"/><circle cx="15" cy="14" r="1" fill="currentColor"/></svg>';
const ADAPTER_GEAR_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09a1.65 1.65 0 0 0-1-1.51 1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09a1.65 1.65 0 0 0 1.51-1 1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33h.01a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51h.01a1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82v.01a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>';
const ADAPTER_PEN_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17 3a2.85 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5Z"/></svg>';

function setConfigPane(pane) {
  configPane = pane;
  const panes = [
    ["basic", "cpane-basic", "config-basic-pane"],
    ["connections", "cpane-connections", "config-connections-pane"],
    ["filter", "cpane-filter", "config-filter-pane"],
  ];
  for (const [name, btnId, paneId] of panes) {
    const btn = document.getElementById(btnId);
    const paneEl = document.getElementById(paneId);
    if (!btn || !paneEl) continue;
    btn.classList.toggle("active", pane === name);
    paneEl.style.display = pane === name ? "" : "none";
  }
  if (pane === "connections") loadConnections();
  if (pane === "filter") loadChatFilter();
}

async function loadConnections() {
  try {
    const res = await api("GET", "/api/connections");
    const d = res.data || {};
    connectionsData = d.adapters || [];
    connectionsStatus = d.status || [];
    renderAdapterCards();
  } catch (e) {
    toast(e.message || t("connections.load_failed"), true);
  }
}

function adapterStatusOf(id) {
  return connectionsStatus.find((s) => String(s.id) === String(id)) || {};
}

/** 卡片右下角端点信息：qqofficial 显示 AppID，正向显示端口，反向显示监听 URL */
function adapterEndpointInfo(a) {
  if (a.type === "qqofficial") {
    const appId = String(a.app_id || "").trim();
    const secret = String(a.app_secret || "").trim();
    if (!appId || !secret) return { configured: false, text: t("connections.unconfigured") };
    return { configured: true, text: "AppID " + appId + (a.sandbox ? " (" + t("connections.qq_sandbox_short") + ")" : "") };
  }
  const forward = Number(a.ws_type || 0) === 0;
  const target = String(a.target || "").trim();
  const port = Number(a.listen_port || 0);
  if (forward) {
    if (!target) return { configured: false, text: t("connections.unconfigured") };
    try { return { configured: true, text: t("connections.port_label") + new URL(target).port }; }
    catch { return { configured: true, text: target }; }
  }
  if (!port) return { configured: false, text: t("connections.unconfigured") };
  return { configured: true, text: "URL: ws://" + (a.listen_host || "0.0.0.0") + ":" + port };
}

function renderAdapterCards() {
  const wrap = document.getElementById("adapter-cards");
  if (!wrap) return;
  if (!connectionsData.length) {
    wrap.innerHTML = `<div class="empty-state">${esc(t("connections.empty"))}</div>`;
    return;
  }
  wrap.innerHTML = connectionsData.map((a) => {
    const st = adapterStatusOf(a.id);
    const isBot = a.type === "astrbot";
    const isQQOfficial = a.type === "qqofficial";
    const forward = Number(a.ws_type || 0) === 0;
    const ep = adapterEndpointInfo(a);
    const typeLabel = isQQOfficial ? " · " + t("dashboard.conn_type_qqofficial") : (isBot ? " · " + t("dashboard.conn_type_astrbot") : "");
    const tagText = ep.configured
      ? (isQQOfficial ? t("connections.type_qqofficial") : (forward ? t("connections.ws_forward") : t("connections.ws_reverse")))
      : t("connections.unconfigured");
    let statusCls, statusText;
    if (!a.enabled) { statusCls = "off"; statusText = t("connections.disabled"); }
    else if (st.connected) { statusCls = "on"; statusText = t("connections.connected"); }
    else { statusCls = "on"; statusText = t("connections.enabled"); }
    const iconCls = isQQOfficial ? "qq" : (isBot ? "bot" : "wifi");
    return `
<div class="adapter-card ${a.enabled ? "" : "disabled"}">
  <div class="adapter-card-top">
    <span class="adapter-icon ${iconCls}">${isBot || isQQOfficial ? ADAPTER_BOT_SVG : ADAPTER_WIFI_SVG}</span>
    <span class="adapter-title">
      <span class="adapter-name" title="${esc(a.name)}">${esc(a.name)}</span>
      <span class="adapter-type-tag ${ep.configured ? "" : "unconfigured"}">${esc(tagText)}${esc(typeLabel)}</span>
    </span>
    <button class="adapter-gear" title="${esc(t("connections.settings"))}" data-id="${esc(a.id)}">${ADAPTER_GEAR_SVG}</button>
  </div>
  <div class="adapter-card-divider"></div>
  <div class="adapter-card-bottom">
    <span class="adapter-status ${statusCls}">${esc(statusText)}</span>
    <span class="adapter-endpoint ${ep.configured ? "" : "unconfigured"}" title="${esc(ep.text)}">${esc(ep.text)}</span>
  </div>
</div>`;
  }).join("");
}

function openAddAdapterModal() {
  document.getElementById("adapter-add-modal").classList.add("show");
}

async function createAdapter(type) {
  try {
    const res = await api("POST", "/api/connections", { type });
    const created = (res.data || {});
    closeModal("adapter-add-modal");
    toast(res.msg || t("connections.created"));
    await loadConnections();
    if (created.id) openEditAdapterModal(created.id);
  } catch (e) {
    toast(e.message || t("connections.create_failed"), true);
  }
}

/* ---------------- 编辑弹窗（v1.2.1 重构：标签上置 / 自定义下拉 / 折叠面板） ---------------- */

const AE_CARET_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="m6 9 6 6 6-6"/></svg>';
const AE_ARROW_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="m9 18 6-6-6-6"/></svg>';
const AE_CHECK_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6 9 17l-5-5"/></svg>';

/** 字段：小号加粗标签置于控件上方（不与输入框同行） */
function aeField(label, controlHtml, opts) {
  const o = opts || {};
  const optHtml = o.optional ? `<span class="ae-opt">${esc(o.optional)}</span>` : "";
  const hintHtml = o.hint ? `<div class="hint">${esc(o.hint)}</div>` : "";
  const cls = ["ae-field", o.cls, o.id].filter(Boolean).join(" ");
  return `<div class="${cls}"${o.id ? ` id="${esc(o.id)}"` : ""}>
  <label>${esc(label)}${optHtml}</label>${controlHtml}${hintHtml}
</div>`;
}

/* ----- 自定义下拉选择器（不使用浏览器原生 select） ----- */
const aeSelectRegistry = {};

function aeSelectHtml(id, options, selected) {
  aeSelectRegistry[id] = options;
  const cur = options.find((o) => String(o.value) === String(selected)) || options[0] || { value: "", name: "" };
  const items = options.map((o) => {
    const active = String(o.value) === String(cur.value);
    return `<div class="ae-select-item${active ? " active" : ""}" data-value="${esc(o.value)}" onclick="pickAeSelect('${esc(id)}','${esc(o.value)}')">
      <span class="ae-item-name">${esc(o.name)}${active ? `<span class="ae-item-check">${AE_CHECK_SVG}</span>` : ""}</span>
      ${o.desc ? `<span class="ae-item-desc">${esc(o.desc)}</span>` : ""}
    </div>`;
  }).join("");
  return `<div class="ae-select" id="${esc(id)}" data-value="${esc(cur.value)}">
  <button type="button" class="ae-select-btn" onclick="toggleAeSelect('${esc(id)}', event)">
    <span class="ae-select-value">${esc(cur.name)}</span>
    <span class="ae-select-caret">${AE_CARET_SVG}</span>
  </button>
  <div class="ae-select-menu">${items}</div>
</div>`;
}

function toggleAeSelect(id, ev) {
  if (ev) ev.stopPropagation();
  const el = document.getElementById(id);
  if (!el) return;
  document.querySelectorAll(".ae-select.open").forEach((s) => { if (s !== el) s.classList.remove("open"); });
  el.classList.toggle("open");
}

function closeAllAeSelects() {
  document.querySelectorAll(".ae-select.open").forEach((s) => s.classList.remove("open"));
}

function pickAeSelect(id, value) {
  const el = document.getElementById(id);
  if (!el) return;
  el.dataset.value = value;
  el.classList.remove("open");
  // 重渲染以更新按钮文案与选中态
  const options = aeSelectRegistry[id] || [];
  const cur = options.find((o) => String(o.value) === String(value));
  const valEl = el.querySelector(".ae-select-value");
  if (valEl && cur) valEl.textContent = cur.name;
  el.querySelectorAll(".ae-select-item").forEach((item) => {
    const active = item.dataset.value === String(value);
    item.classList.toggle("active", active);
    const name = item.querySelector(".ae-item-name");
    if (name) {
      let check = name.querySelector(".ae-item-check");
      if (active && !check) {
        check = document.createElement("span");
        check.className = "ae-item-check";
        check.innerHTML = AE_CHECK_SVG;
        name.appendChild(check);
      } else if (!active && check) check.remove();
    }
  });
  if (id === "ae-ws-type") onAdapterWsTypeChange();
  if (id === "ae-host") onAdapterHostChange();
}

// 点击空白处 / Esc 关闭全部自定义下拉
document.addEventListener("click", (e) => {
  if (!e.target.closest(".ae-select")) closeAllAeSelects();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") closeAllAeSelects();
});

/* ----- 表单行渲染 ----- */

function syncFieldRow(key, val) {
  const label = t("connections.sync_" + key);
  if (typeof val === "boolean") {
    return `<div class="ae-toggle-row">
  <span class="ae-switch-label">${esc(label)}</span>
  <div class="switch"><input type="checkbox" id="ae-sync-${esc(key)}" ${val ? "checked" : ""}>
  <label class="track" for="ae-sync-${esc(key)}"></label></div>
</div>`;
  }
  if (key === "max_message_length") {
    return aeField(label, `<input type="number" min="1" max="4096" id="ae-sync-${esc(key)}" value="${esc(val)}">`);
  }
  return aeField(label, `<input type="text" id="ae-sync-${esc(key)}" value="${esc(val)}">`);
}

function showAeField(id, show) {
  const el = document.getElementById(id);
  if (el) el.classList.toggle("show", !!show);
}

function onAdapterWsTypeChange() {
  const sel = document.getElementById("ae-ws-type");
  if (!sel) return;
  const forward = sel.dataset.value === "0";
  showAeField("ae-target-row", forward);
  showAeField("ae-host-row", !forward);
  showAeField("ae-port-row", !forward);
  const hostSel = document.getElementById("ae-host");
  showAeField("ae-host-custom-row", !forward && hostSel !== null && hostSel.dataset.value === "custom");
}

function onAdapterHostChange() {
  const sel = document.getElementById("ae-host");
  if (!sel) return;
  const custom = sel.dataset.value === "custom";
  showAeField("ae-host-custom-row", custom);
  if (custom) {
    const inp = document.getElementById("ae-host-custom");
    if (inp) inp.focus();
  }
}

function toggleAdapterCollapse(btn) {
  closeAllAeSelects();
  btn.closest(".adapter-collapse").classList.toggle("open");
}

function openEditAdapterModal(id) {
  const a = connectionsData.find((x) => String(x.id) === String(id));
  if (!a) { toast(t("connections.not_found", { id }), true); return; }
  editingAdapterId = String(id);
  const isBot = a.type === "astrbot";
  const isQQOfficial = a.type === "qqofficial";
  const forward = Number(a.ws_type || 0) === 0;
  const host = String(a.listen_host || "0.0.0.0");
  const hostPreset = (host === "0.0.0.0" || host === "::") ? host : (host === "127.0.0.1" ? host : "custom");
  // 后端返回等长 * 掩码（长度与真实密钥一致）；提交时后端识别掩码并保留原值
  const tokenMasked = a.access_token || "";
  const secretMasked = a.app_secret || "";
  const sync = a.sync || {};
  const syncRows = Object.keys(sync).map((k) => syncFieldRow(k, sync[k])).join("");
  const typeName = isQQOfficial ? t("connections.type_qqofficial")
    : (isBot ? t("connections.type_astrbot") : t("connections.type_websocket"));
  const typeDesc = isQQOfficial ? t("connections.type_qqofficial_desc")
    : (isBot ? t("connections.type_astrbot_desc") : t("connections.type_websocket_desc"));
  const modeBadge = isQQOfficial ? t("connections.qqofficial_ws_mode")
    : (forward ? t("connections.ws_forward") : t("connections.ws_reverse"));

  document.getElementById("adapter-edit-body").innerHTML = `
<div class="ae-head">
  <span class="adapter-icon ${isBot || isQQOfficial ? "bot" : "wifi"}">${isBot || isQQOfficial ? ADAPTER_BOT_SVG : ADAPTER_WIFI_SVG}</span>
  <div class="ae-head-main">
    <div class="ae-head-title">
      <span class="ae-head-name" id="ae-name-text" title="${esc(a.name)}">${esc(a.name)}</span>
      <button class="adapter-pen" title="${esc(t("connections.rename"))}" onclick="adapterNameEdit()">${ADAPTER_PEN_SVG}</button>
      <span class="ae-name-edit" id="ae-name-edit">
        <input type="text" id="ae-name-input" maxlength="64" placeholder="${esc(t("connections.name_placeholder"))}" value="${esc(a.name)}"
               onkeydown="if(event.key==='Enter')adapterNameConfirm();if(event.key==='Escape')adapterNameCancel()">
        <button class="btn small" onclick="adapterNameConfirm()">${esc(t("connections.name_ok"))}</button>
        <button class="btn small ghost" onclick="adapterNameCancel()">${esc(t("connections.name_cancel"))}</button>
      </span>
    </div>
    <div class="ae-badges">
      <span class="ae-badge plain">${esc(typeName)}</span>
      <span class="ae-badge">${esc(modeBadge)}</span>
      <span class="ae-badge ${a.enabled ? "green" : "red"}">${esc(a.enabled ? t("connections.enabled") : t("connections.disabled"))}</span>
    </div>
  </div>
</div>
<div class="hint" style="margin:10px 2px 0">${esc(typeDesc)}</div>

<div class="ae-section-title">${esc(t("connections.section_connection"))}</div>

<div class="ae-switch-row">
  <div class="ae-switch-info">
    <div class="ae-switch-label">${esc(t("connections.enable"))}</div>
    <div class="ae-switch-sub">${esc(t("connections.enable_hint"))}</div>
  </div>
  <div class="switch"><input type="checkbox" id="ae-enabled" ${a.enabled ? "checked" : ""}>
  <label class="track" for="ae-enabled"></label></div>
</div>

${isQQOfficial ? `
<div class="ae-qr-row">
  <button type="button" class="btn small ghost ae-qr-btn" data-id="${esc(a.id)}">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><path d="M14 14h3v3h-3zM21 14v.01M14 21v.01M17.5 17.5h.01M21 21h.01"/></svg>
    ${esc(t("connections.qr_login"))}
  </button>
  <span class="ae-qr-hint">${esc(t("connections.qr_login_hint"))}</span>
</div>
${aeField(t("connections.qq_app_id"), `<input type="text" id="ae-app-id" placeholder="102345678" value="${esc(a.app_id || "")}">`)}
${aeField(t("connections.qq_app_secret"), secretFieldHtml("ae-app-secret", t("connections.qq_app_secret_ph"), secretMasked, a.id, "app_secret"))}
${aeField(t("connections.qq_connect_interval"), `<input type="number" min="0" max="86400000" step="500" id="ae-connect-interval" value="${esc(a.connect_interval ?? 60000)}">`, { hint: t("connections.qq_connect_interval_hint") })}
${aeField(t("connections.qq_extra_intents"), `<input type="number" min="0" max="2147483647" step="1" id="ae-extra-intents" value="${esc(a.extra_intents ?? 0)}">`, { hint: t("connections.qq_extra_intents_hint") })}
<div class="ae-switch-row">
  <div class="ae-switch-info">
    <div class="ae-switch-label">${esc(t("connections.qq_sandbox"))}</div>
    <div class="ae-switch-sub">${esc(t("connections.qq_sandbox_hint"))}</div>
  </div>
  <div class="switch"><input type="checkbox" id="ae-sandbox" ${a.sandbox ? "checked" : ""}>
  <label class="track" for="ae-sandbox"></label></div>
</div>
<div class="ae-switch-row">
  <div class="ae-switch-info">
    <div class="ae-switch-label">${esc(t("connections.qq_suppress_log"))}</div>
    <div class="ae-switch-sub">${esc(t("connections.qq_suppress_log_hint"))}</div>
  </div>
  <div class="switch"><input type="checkbox" id="ae-suppress-log" ${(a.suppress_connection_log ?? true) ? "checked" : ""}>
  <label class="track" for="ae-suppress-log"></label></div>
</div>
<div class="hint">${esc(t("connections.qqofficial_hint"))}</div>
` : `
${aeField(t("connections.ws_type"), aeSelectHtml("ae-ws-type", [
    { value: "0", name: t("connections.ws_forward"), desc: t("connections.ws_forward_desc") },
    { value: "1", name: t("connections.ws_reverse"), desc: t("connections.ws_reverse_desc") },
  ], forward ? "0" : "1"), { id: "ae-wstype-field" })}

${aeField(t("connections.target"), `<input type="text" id="ae-target" placeholder="${isBot ? "ws://127.0.0.1:6200" : "ws://127.0.0.1:3001"}" value="${esc(a.target || "")}">`, { id: "ae-target-row", cls: "ae-field-cond" })}
${aeField(t("connections.listen_host"), aeSelectHtml("ae-host", [
    { value: "0.0.0.0", name: t("connections.host_all") },
    { value: "127.0.0.1", name: t("connections.host_local") },
    { value: "custom", name: t("connections.host_custom") },
  ], hostPreset), { id: "ae-host-row", cls: "ae-field-cond" })}
${aeField(t("connections.host_custom_ip"), `<input type="text" id="ae-host-custom" placeholder="192.168.1.10" value="${hostPreset === "custom" ? esc(host) : ""}">`, { id: "ae-host-custom-row", cls: "ae-field-cond" })}
${aeField(t("connections.listen_port"), `<input type="number" min="1" max="65535" id="ae-port" value="${esc(a.listen_port ?? 3002)}">`, { id: "ae-port-row", cls: "ae-field-cond" })}
${aeField(t("connections.access_token"), secretFieldHtml("ae-token", t("connections.token_optional"), tokenMasked, a.id, "access_token"))}
`}

<div class="ae-section-title">${esc(t("connections.section_advanced"))}</div>

<div class="adapter-collapse" id="ae-collapse-identity">
  <button class="adapter-collapse-header" type="button" onclick="toggleAdapterCollapse(this)">
    <span class="adapter-collapse-arrow">${AE_ARROW_SVG}</span>
    <span>${esc(t("connections.identity_section"))}</span>
    <span class="adapter-collapse-count">${esc(t("connections.item_count", { n: isQQOfficial ? 2 : (isBot ? 2 : 3) }))}</span>
  </button>
  <div class="adapter-collapse-body"><div class="adapter-collapse-inner">
    ${aeField(t("connections.bot_qq"), `<input type="number" id="ae-bot-qq" placeholder="${isQQOfficial ? "123456789" : ""}" value="${esc(a.bot_qq || 0)}">`,
      isQQOfficial ? { hint: t("connections.bot_qq_qqofficial_hint") } : {})}
    ${aeField(isQQOfficial ? t("connections.admin_openid") : t("connections.admin_qq"), `<input type="text" id="ae-admin-qq" placeholder="${isQQOfficial ? "OPENID1,OPENID2" : "10001,10002"}" value="${esc(Array.isArray(a.admin_qq) ? a.admin_qq.join(",") : (a.admin_qq || ""))}">`)}
    ${isBot
      ? `<div class="hint">${esc(t("connections.groups_on_astrbot"))}</div>`
      : aeField(isQQOfficial ? t("connections.main_group_openid") : t("connections.main_group"),
          `<input type="text" id="ae-main-group" placeholder="${isQQOfficial ? "OPENID1,OPENID2" : "111,222,333"}" value="${esc(Array.isArray(a.main_group) ? a.main_group.join(",") : (a.main_group || ""))}">`,
          isQQOfficial ? { hint: t("connections.main_group_openid_hint") } : {})}
  </div></div>
</div>

<div class="adapter-collapse" id="ae-collapse-sync">
  <button class="adapter-collapse-header" type="button" onclick="toggleAdapterCollapse(this)">
    <span class="adapter-collapse-arrow">${AE_ARROW_SVG}</span>
    <span>${esc(t("connections.sync_section"))}</span>
    <span class="adapter-collapse-count">${esc(t("connections.item_count", { n: Object.keys(sync).length }))}</span>
  </button>
  <div class="adapter-collapse-body"><div class="adapter-collapse-inner">${syncRows}</div></div>
</div>

<div class="adapter-edit-footer">
  <button class="btn small danger adapter-delete-btn" data-id="${esc(a.id)}">${esc(t("connections.delete"))}</button>
  <button class="btn small ghost" onclick="closeModal('adapter-edit-modal')">${esc(t("connections.cancel"))}</button>
  <button class="btn small" onclick="saveAdapter()">${esc(t("connections.save"))}</button>
</div>`;

  // 按当前连接方式显隐条件字段
  onAdapterWsTypeChange();
  document.getElementById("adapter-edit-modal").classList.add("show");
}

function adapterNameEdit() {
  document.getElementById("ae-name-text").style.display = "none";
  document.getElementById("ae-name-edit").classList.add("show");
  const input = document.getElementById("ae-name-input");
  input.focus();
  input.select();
}

function adapterNameConfirm() {
  const input = document.getElementById("ae-name-input");
  const name = (input.value || "").trim();
  if (name) document.getElementById("ae-name-text").textContent = name;
  document.getElementById("ae-name-edit").classList.remove("show");
  document.getElementById("ae-name-text").style.display = "";
}

function adapterNameCancel() {
  document.getElementById("ae-name-edit").classList.remove("show");
  document.getElementById("ae-name-text").style.display = "";
}

/* ---------------- 密钥二次查看（眼睛按钮） ---------------- */

const EYE_SHOW_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>';
const EYE_HIDE_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94"/><path d="M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19"/><path d="M14.12 14.12a3 3 0 1 1-4.24-4.24"/><line x1="1" y1="1" x2="23" y2="23"/></svg>';

/** 密钥输入框：带明文/掩码切换的眼睛按钮（明文经 /api/connections/reveal 获取） */
function secretFieldHtml(inputId, placeholder, maskedValue, adapterId, key) {
  return `<div class="secret-field">
  <input type="password" id="${esc(inputId)}" placeholder="${esc(placeholder)}" value="${esc(maskedValue)}"
         autocomplete="new-password" data-masked="${esc(maskedValue)}" spellcheck="false">
  <button type="button" class="secret-eye" data-shown="0"
          title="${esc(t("connections.reveal_secret"))}"
          data-input-id="${esc(inputId)}" data-adapter-id="${esc(adapterId)}" data-key="${esc(key)}">${EYE_SHOW_SVG}</button>
</div>`;
}

async function toggleSecretReveal(btn, inputId, adapterId, key) {
  const input = document.getElementById(inputId);
  if (!input || !btn) return;
  if (btn.dataset.shown === "1") {
    // 切回掩码
    input.type = "password";
    input.value = input.dataset.masked || "";
    btn.dataset.shown = "0";
    btn.innerHTML = EYE_SHOW_SVG;
    btn.title = t("connections.reveal_secret");
    return;
  }
  try {
    const res = await api("POST", "/api/connections/reveal", { id: adapterId, key });
    input.type = "text";
    input.value = (res.data && res.data.value) || "";
    input.dataset.masked = input.dataset.masked || "";
    btn.dataset.shown = "1";
    btn.innerHTML = EYE_HIDE_SVG;
    btn.title = t("connections.hide_secret");
  } catch (e) {
    toast(e.message || t("connections.reveal_failed"), true);
  }
}

/* ---------------- QQ 官方机器人扫码登录 ---------------- */

let qrBindTimer = null;

async function openQrBindModal(adapterId) {
  // 已登录（已配置 AppID + AppSecret）时先自定义确认弹窗，避免直接覆盖现有凭据
  const a = connectionsData.find((x) => String(x.id) === String(adapterId));
  const appId = String((a && a.app_id) || "").trim();
  const secret = String((a && a.app_secret) || "").trim();
  if (appId && secret) {
    const ok = await customConfirm(
      t("connections.qr_relogin_confirm"),
      t("connections.qr_relogin_title")
    );
    if (!ok) return;
  }
  const modal = document.getElementById("qr-bind-modal");
  const box = document.getElementById("qr-bind-code");
  const statusEl = document.getElementById("qr-bind-status");
  if (!modal || !box || !statusEl) return;
  box.innerHTML = `<div class="qr-loading">…</div>`;
  statusEl.textContent = t("connections.qr_creating");
  statusEl.className = "qr-bind-status";
  modal.classList.add("show");
  try {
    const res = await api("POST", "/api/qqofficial/qr/create", {});
    // 注意：解构变量必须重命名，否则局部字符串 qrcode 会遮蔽全局
    // qrcode() 渲染函数，调用即抛 TypeError，二维码退化为纯链接文本
    const { task_id: taskId, qrcode: qrUrl, interval } = res.data || {};
    // qrcode-generator (qrcode.min.js)：本地渲染，typeNumber 0 = 自动
    box.innerHTML = "";
    const holder = document.createElement("div");
    holder.className = "qr-bind-img";
    box.appendChild(holder);
    try {
      const qr = qrcode(0, "M");
      qr.addData(String(qrUrl || ""));
      qr.make();
      holder.innerHTML = qr.createSvgTag({ cellSize: 5, margin: 2, scalable: true });
    } catch (e) {
      holder.textContent = String(qrUrl || "");
    }
    statusEl.textContent = t("connections.qr_scan_hint");
    pollQrBind(String(taskId), String(adapterId), Math.max(1, Number(interval) || 3) * 1000);
  } catch (e) {
    box.innerHTML = "";
    statusEl.textContent = e.message || t("connections.qr_create_failed_short");
    statusEl.className = "qr-bind-status err";
  }
}

function pollQrBind(taskId, adapterId, intervalMs) {
  stopQrBindPoll();
  qrBindTimer = setInterval(async () => {
    let res;
    try {
      res = await api("POST", "/api/qqofficial/qr/poll", { task_id: taskId, adapter_id: adapterId });
    } catch (e) {
      stopQrBindPoll();
      const statusEl = document.getElementById("qr-bind-status");
      if (statusEl) { statusEl.textContent = e.message || t("connections.qr_poll_failed_short"); statusEl.className = "qr-bind-status err"; }
      return;
    }
    const data = res.data || {};
    const statusEl = document.getElementById("qr-bind-status");
    if (data.status === "created") {
      stopQrBindPoll();
      // 不再在二维码下方绘制绿色横框：直接关窗并弹自定义“登录成功”提示
      if (statusEl) { statusEl.textContent = ""; statusEl.className = "qr-bind-status"; }
      const appIdInput = document.getElementById("ae-app-id");
      if (appIdInput && data.appid) appIdInput.value = String(data.appid);
      const secretInput = document.getElementById("ae-app-secret");
      if (secretInput) {
        // Secret 由后端直接写入配置，前端刷新为最新等长掩码
        const len = Number(data.secret_len) || 32;
        secretInput.value = "*".repeat(len);
        secretInput.dataset.masked = "*".repeat(len);
        secretInput.type = "password";
      }
      closeModal("qr-bind-modal");
      await customAlert(
        t("connections.qr_bind_success", { appid: data.appid || "" }),
        t("connections.qr_bind_success_title")
      );
      await loadConnections();
      openEditAdapterModal(adapterId);
    } else if (data.status === "expired") {
      stopQrBindPoll();
      if (statusEl) { statusEl.textContent = t("connections.qr_expired"); statusEl.className = "qr-bind-status err"; }
    } else if (statusEl && data.message) {
      statusEl.textContent = String(data.message);
    }
  }, intervalMs);
}

function stopQrBindPoll() {
  if (qrBindTimer) { clearInterval(qrBindTimer); qrBindTimer = null; }
}

function closeQrBindModal() {
  stopQrBindPoll();
  closeModal("qr-bind-modal");
}

function collectAdapterForm() {
  const a = connectionsData.find((x) => String(x.id) === editingAdapterId);
  if (!a) return null;
  const isQQOfficial = a.type === "qqofficial";
  const name = (document.getElementById("ae-name-text").textContent || a.name || "").trim();
  const toIntList = (v) => String(v || "").split(",").map((s) => s.trim()).filter(Boolean)
    .map((s) => (/^\d+$/.test(s) ? Number(s) : s));
  const patch = {
    name,
    enabled: document.getElementById("ae-enabled").checked,
    admin_qq: toIntList(document.getElementById("ae-admin-qq").value),
    sync: {},
  };
  if (isQQOfficial) {
    const secret = document.getElementById("ae-app-secret").value || "";
    patch.app_id = (document.getElementById("ae-app-id").value || "").trim();
    if (secret && !/^\*+$/.test(secret)) patch.app_secret = secret;
    patch.sandbox = document.getElementById("ae-sandbox").checked;
    patch.suppress_connection_log = document.getElementById("ae-suppress-log").checked;
    const intervalRaw = Number(document.getElementById("ae-connect-interval").value);
    patch.connect_interval = Number.isFinite(intervalRaw)
      ? Math.min(86400000, Math.max(0, Math.floor(intervalRaw)))
      : 60000;
    const intentsRaw = Number(document.getElementById("ae-extra-intents").value);
    patch.extra_intents = Number.isFinite(intentsRaw)
      ? Math.min(2147483647, Math.max(0, Math.floor(intentsRaw)))
      : 0;
    patch.main_group = toIntList(document.getElementById("ae-main-group").value);
    patch.bot_qq = Number(document.getElementById("ae-bot-qq").value) || 0;
    // 官方机器人卡片不含 WebSocket 连接字段（ws_type/target 等）：
    // 后端按类型校验，官方域只走 AppID/Secret 网关鉴权
    return patch;
  }
  const forward = document.getElementById("ae-ws-type").dataset.value === "0";
  const hostSel = document.getElementById("ae-host").dataset.value;
  const host = hostSel === "custom"
    ? (document.getElementById("ae-host-custom").value || "").trim() || "0.0.0.0"
    : hostSel;
  patch.ws_type = forward ? 0 : 1;
  patch.listen_host = host;
  patch.listen_port = Number(document.getElementById("ae-port").value) || 0;
  patch.access_token = document.getElementById("ae-token").value || "";
  patch.bot_qq = Number(document.getElementById("ae-bot-qq").value) || 0;
  if (forward) patch.target = (document.getElementById("ae-target").value || "").trim();
  else patch.target = "";
  if (a.type !== "astrbot") patch.main_group = toIntList(document.getElementById("ae-main-group").value);
  else patch.main_group = a.main_group || [];
  const sync = a.sync || {};
  for (const key of Object.keys(sync)) {
    const el = document.getElementById("ae-sync-" + key);
    if (!el) continue;
    if (typeof sync[key] === "boolean") patch.sync[key] = el.checked;
    else if (key === "max_message_length") patch.sync[key] = Number(el.value) || 256;
    else patch.sync[key] = el.value;
  }
  return patch;
}

async function saveAdapter() {
  const patch = collectAdapterForm();
  if (!patch) return;
  try {
    const res = await api("PUT", "/api/connections/" + encodeURIComponent(editingAdapterId), patch);
    closeModal("adapter-edit-modal");
    toast(res.msg || t("connections.saved"));
    await loadConnections();
  } catch (e) {
    toast(e.message || t("connections.save_failed"), true);
  }
}

async function deleteAdapter(id) {
  const a = connectionsData.find((x) => String(x.id) === String(id));
  if (!await customConfirm(t("connections.delete_confirm", { name: (a && a.name) || id }))) return;
  try {
    const res = await api("DELETE", "/api/connections/" + encodeURIComponent(id));
    closeModal("adapter-edit-modal");
    toast(res.msg || t("connections.deleted"));
    await loadConnections();
  } catch (e) {
    toast(e.message || t("connections.delete_failed"), true);
  }
}

/* ─── 聊天屏蔽（双向敏感词过滤） ─── */
let chatFilterData = null;   // { config, words, wordbanks }
let chatFilterSaving = false;

async function loadChatFilter() {
  try {
    const res = await api("GET", "/api/chat_filter");
    chatFilterData = res.data || { config: {}, words: [], wordbanks: [] };
    renderChatFilter();
  } catch (e) {
    toast(e.message || t("chatfilter.load_failed"), true);
  }
}

function cfConfig() {
  return chatFilterData.config || {};
}

function renderChatFilter() {
  if (!chatFilterData) return;
  const cfg = cfConfig();
  const words = chatFilterData.words || [];
  const set = (id, val) => { const el = document.getElementById(id); if (el) el.value = val; };
  const check = (id, val) => { const el = document.getElementById(id); if (el) el.checked = !!val; };

  check("cf-master", cfg.enabled);
  check("cf-dir-g2q", cfg.game_to_qq);
  check("cf-dir-q2g", cfg.qq_to_game);
  set("cf-mask-input", cfg.mask_text || "*");
  renderCfMode(cfg.mode || "mask");

  const plainCount = words.filter((w) => w.type !== "regex").length;
  const regexCount = words.filter((w) => w.type === "regex").length;
  set("cf-st-words", String(plainCount));
  set("cf-st-regex", String(regexCount));
  set("cf-st-mode", (cfg.mode || "mask") === "mask" ? t("chatfilter.mode_mask") : t("chatfilter.mode_block"));

  renderCfBanks();
  renderCfWords();
  renderCfExempt("players", cfg.exempt_players || []);
  renderCfExempt("qq", cfg.exempt_qq || []);
}

function renderCfMode(mode) {
  const maskBtn = document.getElementById("cf-mode-mask");
  const blockBtn = document.getElementById("cf-mode-block");
  if (maskBtn) maskBtn.classList.toggle("active", mode === "mask");
  if (blockBtn) blockBtn.classList.toggle("active", mode === "block");
  const maskRow = document.getElementById("cf-mask-row");
  if (maskRow) maskRow.style.display = mode === "mask" ? "" : "none";
}

function renderCfBanks() {
  const host = document.getElementById("cf-banks");
  if (!host) return;
  const banks = (chatFilterData && chatFilterData.wordbanks) || [];
  if (!banks.length) {
    host.innerHTML = `<div class="empty-state">${esc(t("chatfilter.banks_empty"))}</div>`;
    return;
  }
  host.innerHTML = banks.map((b, i) => `
    <div class="cf-bank${b.imported ? " imported" : ""}">
      <div class="cf-bank-icon">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>
      </div>
      <div class="cf-bank-info">
        <div class="cf-bank-name">${esc(b.name || b.file)}${b.imported ? `<span class="tag green">${esc(t("chatfilter.bank_imported"))}</span>` : ""}</div>
        <div class="cf-bank-count">${esc(t("chatfilter.bank_count", { count: b.count || 0 }))}</div>
      </div>
      <button class="btn small ${b.imported ? "ghost" : ""}" onclick="cfImportBank(${i})">${esc(t("chatfilter.bank_import"))}</button>
    </div>
  `).join("");
}

function renderCfWords() {
  const host = document.getElementById("cf-words");
  const countEl = document.getElementById("cf-words-count");
  if (!host) return;
  const words = (chatFilterData && chatFilterData.words) || [];
  if (countEl) countEl.textContent = String(words.length);
  if (!words.length) {
    host.innerHTML = `<div class="empty-state" style="width:100%">${esc(t("chatfilter.words_empty"))}</div>`;
    return;
  }
  host.innerHTML = words.map((w, i) => `
    <span class="cf-word${w.type === "regex" ? " regex" : ""}" title="${esc(w.word || "")}">
      ${esc(w.word || "")}
      ${w.source && w.source !== "custom" ? `<span class="cf-word-src">${esc(w.source)}</span>` : ""}
      <button onclick="cfDelWord(${i})" title="${esc(t("chatfilter.word_delete"))}">✕</button>
    </span>
  `).join("");
}

function renderCfExempt(kind, list) {
  const host = document.getElementById("cf-exempt-" + kind);
  if (!host) return;
  const chips = list.map((v, i) => `
    <span class="cf-word">${esc(v)}<button onclick="cfRemoveExempt('${kind}', ${i})" title="${esc(t("chatfilter.word_delete"))}">✕</button></span>
  `).join("");
  host.innerHTML = chips + `
    <span class="cf-chip-add">
      <input type="text" id="cf-exempt-input-${kind}" placeholder="${esc(t("chatfilter.exempt_input_ph"))}" onkeydown="if(event.key==='Enter')cfAddExempt('${kind}')">
      <button class="btn small ghost" onclick="cfAddExempt('${kind}')">${esc(t("chatfilter.exempt_add"))}</button>
    </span>
  `;
}

/* 收集表单当前值 → PUT 保存（改动即时生效） */
async function cfSave() {
  if (!chatFilterData) return;
  const cfg = cfConfig();
  const read = (id) => { const el = document.getElementById(id); return el ? el.checked : undefined; };
  if (read("cf-master") !== undefined) cfg.enabled = read("cf-master");
  if (read("cf-dir-g2q") !== undefined) cfg.game_to_qq = read("cf-dir-g2q");
  if (read("cf-dir-q2g") !== undefined) cfg.qq_to_game = read("cf-dir-q2g");
  const maskInput = document.getElementById("cf-mask-input");
  if (maskInput) cfg.mask_text = (maskInput.value || "*").slice(0, 3);
  try {
    await api("PUT", "/api/chat_filter", { config: cfg, words: chatFilterData.words || [] });
    renderChatFilter();
  } catch (e) {
    toast(e.message || t("chatfilter.save_failed"), true);
  }
}

function cfSetMode(mode) {
  if (!chatFilterData) return;
  cfConfig().mode = mode;
  renderCfMode(mode);
  cfSave();
}

async function cfAddWord() {
  const input = document.getElementById("cf-word-input");
  const regexBox = document.getElementById("cf-word-regex");
  if (!input || !chatFilterData) return;
  const word = (input.value || "").trim();
  if (!word) return;
  chatFilterData.words = chatFilterData.words || [];
  chatFilterData.words.push({ word, type: regexBox && regexBox.checked ? "regex" : "plain", source: "custom" });
  input.value = "";
  if (regexBox) regexBox.checked = false;
  try {
    await api("PUT", "/api/chat_filter", { config: cfConfig(), words: chatFilterData.words });
    renderChatFilter();
  } catch (e) {
    toast(e.message || t("chatfilter.save_failed"), true);
  }
}

async function cfDelWord(index) {
  if (!chatFilterData || !chatFilterData.words) return;
  chatFilterData.words.splice(index, 1);
  try {
    await api("PUT", "/api/chat_filter", { config: cfConfig(), words: chatFilterData.words });
    renderChatFilter();
  } catch (e) {
    toast(e.message || t("chatfilter.save_failed"), true);
  }
}

async function cfImportBank(index) {
  const banks = (chatFilterData && chatFilterData.wordbanks) || [];
  const bank = banks[index];
  const file = bank && bank.file;
  if (!file) return;
  try {
    const res = await api("POST", "/api/chat_filter/import", { file });
    if (res.data) chatFilterData = res.data;
    renderChatFilter();
    toast(res.msg || t("chatfilter.bank_imported_ok"));
  } catch (e) {
    toast(e.message || t("chatfilter.save_failed"), true);
  }
}

/* 一键导入第三方 .txt 词库：前端读文件文本提交后端解析
   （兼容“每行一个”与“中英文逗号分隔”两种格式，自动去重） */
async function cfImportTextFile(input) {
  const file = input.files && input.files[0];
  input.value = "";  // 允许重复选择同一文件
  if (!file) return;
  if (file.size > 8 * 1024 * 1024) {
    toast(t("chatfilter.upload_too_large"), true);
    return;
  }
  let text = "";
  try {
    text = await file.text();
  } catch (e) {
    toast(t("chatfilter.upload_read_failed"), true);
    return;
  }
  try {
    const res = await api("POST", "/api/chat_filter/import_text", { text, name: file.name });
    if (res.data) chatFilterData = res.data;
    renderChatFilter();
    toast(res.msg || t("chatfilter.bank_imported_ok"));
  } catch (e) {
    toast(e.message || t("chatfilter.save_failed"), true);
  }
}

async function cfAddExempt(kind) {
  const input = document.getElementById("cf-exempt-input-" + kind);
  if (!input || !chatFilterData) return;
  const val = (input.value || "").trim();
  if (!val) return;
  const key = kind === "players" ? "exempt_players" : "exempt_qq";
  const cfg = cfConfig();
  const list = cfg[key] || [];
  if (list.map(String).some((x) => x.toLowerCase() === val.toLowerCase())) { input.value = ""; return; }
  list.push(val);
  cfg[key] = list;
  input.value = "";
  await cfSave();
}

async function cfRemoveExempt(kind, index) {
  if (!chatFilterData) return;
  const key = kind === "players" ? "exempt_players" : "exempt_qq";
  const cfg = cfConfig();
  const list = cfg[key] || [];
  list.splice(index, 1);
  await cfSave();
}

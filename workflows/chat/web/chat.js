const TOKEN_KEY = "mcpgate_chat_token";
const CONV_KEY_PREFIX = "mcpgate_chat_conv:";
const EXP_MARGIN_SECONDS = 30;

const ROLE_LABELS = {
  agent: "客服",
  supervisor: "主管",
};

const REGION_LABELS = {
  east: "华东",
  north: "华北",
  south: "华南",
};

const TOOL_LABELS = {
  get_order: "查询订单",
  list_orders: "查询订单列表",
  create_refund: "提交退款申请",
  get_approval: "查询审批进度",
  list_approvals: "查询审批记录",
  get_refund: "查询退款单",
  list_refunds: "查询退款记录",
};

const KIND_LABELS = {
  ok: "✓",
  pending: "待审批",
  denied: "无权限",
  auth_expired: "登录已过期",
  not_found: "未找到",
  conflict: "状态冲突",
  invalid: "参数无效",
  unavailable: "系统暂不可用",
  internal: "内部错误",
};

const state = {
  config: {
    dev_login_enabled: false,
    approval_console_url: "",
  },
  user: null,
  conversationId: "",
  isSending: false,
  countdownTimer: null,
  pendingRecovery: null, // { mode: "auto_send" | "manual_resend", query: string, expectedSub: string }
};

const els = {
  approvalLink: document.getElementById("approval-link"),
  userBadge: document.getElementById("user-badge"),
  tokenCountdown: document.getElementById("token-countdown"),
  newChatBtn: document.getElementById("new-chat-btn"),
  logoutBtn: document.getElementById("logout-btn"),
  messagesArea: document.getElementById("messages-area"),
  emptyState: document.getElementById("empty-state"),
  messagesList: document.getElementById("messages-list"),
  composerForm: document.getElementById("composer-form"),
  queryInput: document.getElementById("query-input"),
  sendBtn: document.getElementById("send-btn"),
  composerStatus: document.getElementById("composer-status"),
  charCount: document.getElementById("char-count"),
  loginModal: document.getElementById("login-modal"),
  loginAlert: document.getElementById("login-alert"),
  devLoginSection: document.getElementById("dev-login-section"),
  devLoginForm: document.getElementById("dev-login-form"),
  loginUsername: document.getElementById("login-username"),
  loginPassword: document.getElementById("login-password"),
  tokenForm: document.getElementById("token-form"),
  tokenInput: document.getElementById("token-input"),
};

function getToken() {
  return sessionStorage.getItem(TOKEN_KEY) || "";
}

function setToken(token) {
  sessionStorage.setItem(TOKEN_KEY, token.trim());
}

function clearToken() {
  sessionStorage.removeItem(TOKEN_KEY);
}

function convKey(sub) {
  return `${CONV_KEY_PREFIX}${sub}`;
}

function getSavedConversationId(sub) {
  if (!sub) return "";
  return sessionStorage.getItem(convKey(sub)) || "";
}

function saveConversationId(sub, convId) {
  if (!sub) return;
  if (convId) {
    sessionStorage.setItem(convKey(sub), convId);
  } else {
    sessionStorage.removeItem(convKey(sub));
  }
}

function decodeJwtPayload(token) {
  try {
    const parts = token.split(".");
    if (parts.length < 2) return null;
    const base64 = parts[1].replace(/-/g, "+").replace(/_/g, "/");
    const padded = base64 + "=".repeat((4 - (base64.length % 4)) % 4);
    const binary = atob(padded);
    const bytes = Uint8Array.from(binary, (c) => c.charCodeAt(0));
    const text = new TextDecoder("utf-8").decode(bytes);
    const obj = JSON.parse(text);
    return obj && typeof obj === "object" ? obj : null;
  } catch {
    return null;
  }
}

function getUserFromToken(token) {
  const payload = decodeJwtPayload(token);
  if (!payload || !payload.sub) return null;
  return {
    sub: String(payload.sub),
    username: String(payload.preferred_username || payload.sub),
    name: String(payload.name || payload.preferred_username || payload.sub),
    role: String(payload.role || ""),
    regions: payload.regions || "",
    exp: typeof payload.exp === "number" ? payload.exp : 0,
  };
}

function formatRegions(raw) {
  if (!raw) return "-";
  const items = Array.isArray(raw) ? raw : String(raw).split(",");
  const labels = items
    .map((item) => String(item).trim())
    .filter(Boolean)
    .map((item) => REGION_LABELS[item] || item);
  return labels.length > 0 ? labels.join("、") : "-";
}

function formatDuration(seconds) {
  const total = Math.max(0, Math.floor(seconds));
  const hrs = Math.floor(total / 3600);
  const mins = Math.floor((total % 3600) / 60);
  const secs = total % 60;
  const pad = (n) => String(n).padStart(2, "0");
  return `${pad(hrs)}:${pad(mins)}:${pad(secs)}`;
}

function secondsUntilExpiry(user) {
  if (!user || !user.exp) return 0;
  return user.exp - Date.now() / 1000;
}

function isTokenExpiringSoon(user) {
  return secondsUntilExpiry(user) <= EXP_MARGIN_SECONDS;
}

function clearNode(node) {
  while (node.firstChild) {
    node.removeChild(node.firstChild);
  }
}

function scrollToBottom() {
  els.messagesArea.scrollTop = els.messagesArea.scrollHeight;
}

function updateEmptyState() {
  const hasMessages = els.messagesList.children.length > 0;
  els.emptyState.classList.toggle("hidden", hasMessages);
}

function updateCharCount() {
  const len = els.queryInput.value.length;
  els.charCount.textContent = `${len} / 2000`;
}

function setComposerDisabled(disabled, statusText = "") {
  state.isSending = disabled;
  els.queryInput.disabled = disabled;
  els.sendBtn.disabled = disabled;
  els.newChatBtn.disabled = disabled;
  els.composerStatus.textContent = statusText;
}

function showLoginModal(message = "", type = "warn") {
  if (message) {
    els.loginAlert.textContent = message;
    els.loginAlert.className = `alert alert-${type}`;
  } else {
    els.loginAlert.textContent = "";
    els.loginAlert.className = "alert hidden";
  }
  els.loginModal.classList.remove("hidden");
  if (state.config.dev_login_enabled) {
    els.loginUsername.focus();
  } else {
    els.tokenInput.focus();
  }
}

function hideLoginModal() {
  els.loginModal.classList.add("hidden");
  els.loginAlert.textContent = "";
  els.loginAlert.className = "alert hidden";
}

function updateTopbar() {
  const user = state.user;
  if (!user) {
    els.userBadge.textContent = "未登录";
    els.tokenCountdown.classList.add("hidden");
    els.newChatBtn.classList.add("hidden");
    els.logoutBtn.classList.add("hidden");
    els.approvalLink.classList.add("hidden");
    return;
  }

  const roleText = ROLE_LABELS[user.role] || user.role || "-";
  const regionsText = formatRegions(user.regions);
  els.userBadge.textContent = `${user.name}（${roleText}｜${regionsText}）`;
  els.tokenCountdown.classList.remove("hidden");
  els.newChatBtn.classList.remove("hidden");
  els.logoutBtn.classList.remove("hidden");

  if (user.role === "supervisor" && state.config.approval_console_url) {
    els.approvalLink.setAttribute("href", state.config.approval_console_url);
    els.approvalLink.classList.remove("hidden");
  } else {
    els.approvalLink.classList.add("hidden");
  }

  updateCountdownDisplay();
}

function updateCountdownDisplay() {
  const user = state.user;
  if (!user) {
    els.tokenCountdown.classList.add("hidden");
    return;
  }
  const remaining = secondsUntilExpiry(user);
  els.tokenCountdown.classList.remove("countdown-warn", "countdown-expired");
  if (remaining <= 0) {
    els.tokenCountdown.textContent = "令牌已过期";
    els.tokenCountdown.classList.add("countdown-expired");
  } else if (remaining <= EXP_MARGIN_SECONDS) {
    els.tokenCountdown.textContent = `即将过期 ${Math.ceil(remaining)}s`;
    els.tokenCountdown.classList.add("countdown-expired");
  } else if (remaining <= 300) {
    els.tokenCountdown.textContent = `剩余 ${formatDuration(remaining)}`;
    els.tokenCountdown.classList.add("countdown-warn");
  } else {
    els.tokenCountdown.textContent = `剩余 ${formatDuration(remaining)}`;
  }
}

function startCountdownTimer() {
  if (state.countdownTimer) {
    clearInterval(state.countdownTimer);
  }
  state.countdownTimer = setInterval(updateCountdownDisplay, 1000);
}

function formatTraceLabel(item) {
  const toolName = TOOL_LABELS[item.tool] || item.tool || "工具调用";
  const kind = item.kind || "";
  if (kind === "ok") {
    return `${toolName} ✓`;
  }
  const kindText = KIND_LABELS[kind] || kind || "完成";
  if (kind === "pending" && item.approval_id) {
    return `${toolName} · ${kindText} ${item.approval_id}`;
  }
  return `${toolName} · ${kindText}`;
}

function buildTraceTooltip(item) {
  const parts = [];
  if (item.tool) parts.push(`工具: ${item.tool}`);
  if (item.kind) parts.push(`结果: ${item.kind}`);
  if (item.http_status != null) parts.push(`HTTP: ${item.http_status}`);
  if (item.error_code) parts.push(`错误码: ${item.error_code}`);
  if (item.approval_id) parts.push(`审批单: ${item.approval_id}`);
  if (item.request_id) parts.push(`请求ID: ${item.request_id}`);
  return parts.join(" | ");
}

function renderTraceBar(container, traces) {
  clearNode(container);
  if (!Array.isArray(traces) || traces.length === 0) {
    container.classList.add("hidden");
    return;
  }
  container.classList.remove("hidden");
  for (const item of traces) {
    const pill = document.createElement("span");
    const kindClass = item.kind ? `trace-${item.kind}` : "trace-not_found";
    pill.className = `trace-pill ${kindClass}`;
    pill.textContent = formatTraceLabel(item);
    const tooltip = buildTraceTooltip(item);
    if (tooltip) {
      pill.title = tooltip;
    }
    container.appendChild(pill);
  }
}

function renderCitations(container, citations) {
  clearNode(container);
  if (!Array.isArray(citations) || citations.length === 0) {
    container.classList.add("hidden");
    return;
  }
  container.classList.remove("hidden");

  const details = document.createElement("details");
  details.className = "citations-box";

  const summary = document.createElement("summary");
  summary.textContent = `制度出处（${citations.length} 条）`;
  details.appendChild(summary);

  const list = document.createElement("div");
  list.className = "citations-list";

  for (const cite of citations) {
    const item = document.createElement("div");
    item.className = "citation-item";

    const header = document.createElement("div");
    header.className = "citation-header";

    const headerParts = [];
    if (cite.document) headerParts.push(`《${cite.document}》`);
    if (cite.clause) headerParts.push(`第 ${cite.clause} 条`);
    if (cite.title) headerParts.push(cite.title);
    header.textContent = headerParts.join(" ") || "制度条款";

    if (typeof cite.score === "number" && !Number.isNaN(cite.score)) {
      const scoreSpan = document.createElement("span");
      scoreSpan.className = "citation-score";
      scoreSpan.textContent = `相关度 ${cite.score.toFixed(2)}`;
      header.appendChild(scoreSpan);
    }

    item.appendChild(header);

    if (cite.content) {
      const body = document.createElement("div");
      body.className = "citation-content";
      body.textContent = cite.content;
      item.appendChild(body);
    }

    list.appendChild(item);
  }

  details.appendChild(list);
  container.appendChild(details);
}

function appendUserMessage(text) {
  const row = document.createElement("div");
  row.className = "msg-row msg-row-user";

  const bubble = document.createElement("div");
  bubble.className = "msg-bubble msg-bubble-user";
  bubble.textContent = text;

  row.appendChild(bubble);
  els.messagesList.appendChild(row);
  updateEmptyState();
  scrollToBottom();
  return row;
}

function appendAssistantMessage({ answer = "", citations = [], trace = [], pending = false } = {}) {
  const row = document.createElement("div");
  row.className = "msg-row msg-row-assistant";

  const bubble = document.createElement("div");
  bubble.className = "msg-bubble msg-bubble-assistant";

  const textEl = document.createElement("div");
  textEl.className = pending ? "msg-text msg-pending" : "msg-text";
  textEl.textContent = pending ? "正在处理…" : answer;
  bubble.appendChild(textEl);

  const errorEl = document.createElement("div");
  errorEl.className = "msg-error hidden";
  bubble.appendChild(errorEl);

  const traceEl = document.createElement("div");
  traceEl.className = "trace-bar hidden";
  bubble.appendChild(traceEl);

  const citationsEl = document.createElement("div");
  citationsEl.className = "hidden";
  bubble.appendChild(citationsEl);

  row.appendChild(bubble);
  els.messagesList.appendChild(row);
  updateEmptyState();

  renderTraceBar(traceEl, trace);
  renderCitations(citationsEl, citations);
  scrollToBottom();

  return {
    row,
    textEl,
    errorEl,
    traceEl,
    citationsEl,
    setAnswer(fullText) {
      textEl.className = "msg-text";
      textEl.textContent = fullText;
      scrollToBottom();
    },
    setError(message) {
      if (textEl.classList.contains("msg-pending")) {
        textEl.textContent = "";
        textEl.className = "msg-text";
      }
      errorEl.textContent = message;
      errorEl.classList.remove("hidden");
      scrollToBottom();
    },
    setTrace(items) {
      renderTraceBar(traceEl, items);
      scrollToBottom();
    },
    setCitations(items) {
      renderCitations(citationsEl, items);
      scrollToBottom();
    },
  };
}

function resetConversationView({ clearSaved = true } = {}) {
  if (clearSaved && state.user) {
    saveConversationId(state.user.sub, "");
  }
  state.conversationId = "";
  clearNode(els.messagesList);
  updateEmptyState();
}

async function loadConversationHistory(conversationId) {
  if (!conversationId || !state.user) return;
  const token = getToken();
  if (!token) return;

  try {
    const resp = await fetch(
      `/chat/api/conversations/${encodeURIComponent(conversationId)}/messages`,
      {
        method: "GET",
        headers: {
          Accept: "application/json",
          Authorization: `Bearer ${token}`,
        },
      },
    );
    if (resp.status === 401) {
      clearToken();
      state.user = null;
      updateTopbar();
      showLoginModal("登录已过期，请重新登录。", "warn");
      return;
    }
    if (resp.status === 404 || resp.status === 422) {
      resetConversationView({ clearSaved: true });
      return;
    }
    if (!resp.ok) {
      return;
    }

    const data = await resp.json();
    const messages = Array.isArray(data?.messages) ? data.messages : [];
    clearNode(els.messagesList);
    for (const msg of messages) {
      if (msg.query) {
        appendUserMessage(msg.query);
      }
      appendAssistantMessage({
        answer: msg.answer || "",
        citations: Array.isArray(msg.citations) ? msg.citations : [],
      });
    }
    updateEmptyState();
    scrollToBottom();
  } catch {
    // Ignore transient network errors during history restore
  }
}

async function consumeSseStream(response, onEvent) {
  const reader = response.body.getReader();
  const decoder = new TextDecoder("utf-8");
  let buffer = "";

  function dispatchBlock(block) {
    const lines = block.split(/\r?\n/);
    let eventName = "message";
    const dataLines = [];
    for (const line of lines) {
      if (!line || line.startsWith(":")) continue;
      if (line.startsWith("event:")) {
        eventName = line.slice(6).trim();
      } else if (line.startsWith("data:")) {
        dataLines.push(line.slice(5).trimStart());
      }
    }
    if (dataLines.length === 0) return;
    const raw = dataLines.join("\n");
    try {
      const parsed = JSON.parse(raw);
      onEvent(eventName, parsed);
    } catch {
      // Ignore malformed chunk
    }
  }

  while (true) {
    const { done, value } = await reader.read();
    if (done) {
      buffer += decoder.decode();
      break;
    }
    buffer += decoder.decode(value, { stream: true });
    let splitIdx = buffer.search(/\r?\n\r?\n/);
    while (splitIdx !== -1) {
      const block = buffer.slice(0, splitIdx);
      const sepMatch = buffer.slice(splitIdx).match(/^\r?\n\r?\n/);
      buffer = buffer.slice(splitIdx + (sepMatch ? sepMatch[0].length : 2));
      if (block.trim()) {
        dispatchBlock(block);
      }
      splitIdx = buffer.search(/\r?\n\r?\n/);
    }
  }

  if (buffer.trim()) {
    dispatchBlock(buffer);
  }
}

async function sendQuery(rawQuery) {
  const query = rawQuery.trim();
  if (!query || state.isSending) return;

  const token = getToken();
  if (!token || !state.user) {
    state.pendingRecovery = {
      mode: "auto_send",
      query,
      expectedSub: "",
    };
    showLoginModal("请先登录后再发送消息。", "warn");
    return;
  }

  // Case 1: Pre-send expiry check (with 30s margin)
  if (isTokenExpiringSoon(state.user)) {
    const prevSub = state.user.sub;
    state.pendingRecovery = {
      mode: "auto_send",
      query,
      expectedSub: prevSub,
    };
    showLoginModal(
      "当前令牌已过期或即将过期，请重新登录。使用同一账号登录后将自动发送当前消息。",
      "warn",
    );
    return;
  }

  const senderSub = state.user.sub;
  els.queryInput.value = "";
  updateCharCount();
  setComposerDisabled(true, "助手正在回复…");

  const userRow = appendUserMessage(query);
  const assistantUi = appendAssistantMessage({ pending: true });

  const payload = {
    query,
    stream: true,
  };
  if (state.conversationId) {
    payload.conversation_id = state.conversationId;
  }

  let answerText = "";
  const traces = [];
  let sawAuthExpiredTrace = false;

  try {
    const resp = await fetch("/chat/api/messages", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Accept: "text/event-stream",
        Authorization: `Bearer ${token}`,
      },
      body: JSON.stringify(payload),
    });

    // Case 2: chat backend returns 401 (e.g. token_expired)
    if (resp.status === 401) {
      const errData = await resp.json().catch(() => null);
      userRow.remove();
      assistantUi.row.remove();
      updateEmptyState();

      clearToken();
      state.user = null;
      updateTopbar();

      els.queryInput.value = query;
      updateCharCount();
      state.pendingRecovery = {
        mode: "auto_send",
        query,
        expectedSub: senderSub,
      };
      showLoginModal(
        (errData?.message || "身份令牌已过期，请重新登录。") +
          " 使用同一账号登录后将自动发送当前消息。",
        "warn",
      );
      return;
    }

    if (!resp.ok) {
      const errData = await resp.json().catch(() => null);
      assistantUi.setError(errData?.message || `请求失败（HTTP ${resp.status}）`);
      return;
    }

    await consumeSseStream(resp, (eventName, data) => {
      if (eventName === "delta") {
        if (typeof data?.text === "string" && data.text.length > 0) {
          answerText += data.text;
          assistantUi.setAnswer(answerText);
        }
      } else if (eventName === "trace") {
        if (data && typeof data === "object") {
          traces.push(data);
          assistantUi.setTrace(traces);
          if (data.kind === "auth_expired") {
            sawAuthExpiredTrace = true;
          }
        }
      } else if (eventName === "done") {
        if (typeof data?.answer === "string") {
          answerText = data.answer;
          assistantUi.setAnswer(answerText);
        }
        if (Array.isArray(data?.trace)) {
          assistantUi.setTrace(data.trace);
          if (data.trace.some((t) => t && t.kind === "auth_expired")) {
            sawAuthExpiredTrace = true;
          }
        }
        if (Array.isArray(data?.citations)) {
          assistantUi.setCitations(data.citations);
        }
        if (typeof data?.conversation_id === "string" && data.conversation_id) {
          state.conversationId = data.conversation_id;
          saveConversationId(senderSub, data.conversation_id);
        }
      } else if (eventName === "error") {
        assistantUi.setError(data?.message || "助手暂时不可用，请稍后再试。");
      }
    });

    // Case 3: Token expired mid-workflow (Higress rejected, trace.kind === "auth_expired")
    if (sawAuthExpiredTrace) {
      clearToken();
      state.user = null;
      updateTopbar();

      if (!els.queryInput.value.trim()) {
        els.queryInput.value = query;
        updateCharCount();
      }
      state.pendingRecovery = {
        mode: "manual_resend",
        query,
        expectedSub: senderSub,
      };
      showLoginModal("登录已过期，请重新登录。使用同一账号登录后可直接重发该消息。", "warn");
    }
  } catch {
    assistantUi.setError("网络连接中断，请稍后重试。");
  } finally {
    setComposerDisabled(false, "");
  }
}

async function completeLogin(rawToken) {
  const cleaned = rawToken.replace(/^Bearer\s+/i, "").trim();
  const newUser = getUserFromToken(cleaned);
  if (!newUser) {
    showLoginModal("无效的 JWT 格式，请检查后重试。", "danger");
    return;
  }
  if (isTokenExpiringSoon(newUser)) {
    showLoginModal("该令牌已过期或即将过期，请获取新令牌后再登录。", "warn");
    return;
  }

  const prevSub = state.pendingRecovery?.expectedSub || state.user?.sub || "";
  const recovery = state.pendingRecovery;
  state.pendingRecovery = null;

  setToken(cleaned);
  state.user = newUser;
  updateTopbar();
  hideLoginModal();

  // Different account: automatically start a new conversation
  if (prevSub && newUser.sub !== prevSub) {
    resetConversationView({ clearSaved: true });
    els.queryInput.value = "";
    updateCharCount();
    els.queryInput.focus();
    return;
  }

  // Same account recovering from token expiry
  if (recovery && (!recovery.expectedSub || recovery.expectedSub === newUser.sub)) {
    if (recovery.mode === "auto_send" && recovery.query) {
      await sendQuery(recovery.query);
      return;
    }
    if (recovery.mode === "manual_resend") {
      els.queryInput.focus();
      return;
    }
  }

  // Initial login: restore saved conversation for this sub
  const savedConvId = getSavedConversationId(newUser.sub);
  state.conversationId = savedConvId;
  clearNode(els.messagesList);
  updateEmptyState();
  if (savedConvId) {
    await loadConversationHistory(savedConvId);
  }
  els.queryInput.focus();
}

function setupEventListeners() {
  els.queryInput.addEventListener("input", updateCharCount);

  els.queryInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      els.composerForm.requestSubmit();
    }
  });

  els.composerForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    await sendQuery(els.queryInput.value);
  });

  for (const chip of document.querySelectorAll(".prompt-chip")) {
    chip.addEventListener("click", () => {
      if (state.isSending) return;
      const text = chip.getAttribute("data-prompt") || "";
      els.queryInput.value = text;
      updateCharCount();
      els.queryInput.focus();
    });
  }

  els.newChatBtn.addEventListener("click", () => {
    if (state.isSending) return;
    resetConversationView({ clearSaved: true });
    els.queryInput.focus();
  });

  els.logoutBtn.addEventListener("click", () => {
    if (state.isSending) return;
    clearToken();
    state.user = null;
    state.pendingRecovery = null;
    resetConversationView({ clearSaved: false });
    els.queryInput.value = "";
    updateCharCount();
    updateTopbar();
    showLoginModal("已退出登录。", "info");
  });

  els.devLoginForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const username = els.loginUsername.value.trim();
    const password = els.loginPassword.value;
    if (!username || !password) return;

    const submitBtn = els.devLoginForm.querySelector("button[type=submit]");
    submitBtn.disabled = true;
    try {
      const resp = await fetch("/chat/api/login", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Accept: "application/json",
        },
        body: JSON.stringify({ username, password }),
      });
      const data = await resp.json().catch(() => null);
      if (!resp.ok || !data?.access_token) {
        showLoginModal(data?.message || `登录失败（HTTP ${resp.status}）`, "danger");
        return;
      }
      els.loginPassword.value = "";
      await completeLogin(data.access_token);
    } catch {
      showLoginModal("网络请求失败，请确认 chat 服务正在运行。", "danger");
    } finally {
      submitBtn.disabled = false;
    }
  });

  els.tokenForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const raw = els.tokenInput.value.trim();
    if (!raw) {
      showLoginModal("请粘贴有效的 JWT 令牌。", "warn");
      return;
    }
    els.tokenInput.value = "";
    await completeLogin(raw);
  });
}

async function init() {
  setupEventListeners();
  updateCharCount();
  startCountdownTimer();

  try {
    const resp = await fetch("/chat/api/config", { headers: { Accept: "application/json" } });
    if (resp.ok) {
      const cfg = await resp.json();
      state.config.dev_login_enabled = Boolean(cfg?.dev_login_enabled);
      state.config.approval_console_url = String(cfg?.approval_console_url || "");
    }
  } catch {
    // Fallback to defaults
  }

  if (!state.config.dev_login_enabled) {
    els.devLoginSection.classList.add("hidden");
  }

  const existingToken = getToken();
  if (existingToken) {
    const user = getUserFromToken(existingToken);
    if (user && !isTokenExpiringSoon(user)) {
      state.user = user;
      updateTopbar();
      const savedConvId = getSavedConversationId(user.sub);
      state.conversationId = savedConvId;
      if (savedConvId) {
        await loadConversationHistory(savedConvId);
      }
      return;
    }
    clearToken();
  }

  updateTopbar();
  showLoginModal();
}

init();

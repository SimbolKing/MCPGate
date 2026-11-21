import { DEV_LOGIN_ENABLED } from "./config.js";

const TOKEN_KEY = "mcpgate_guard_token";

export const REGION_LABELS = {
  east: "华东",
  north: "华北",
  south: "华南",
};

export const ROLE_LABELS = {
  agent: "客服",
  supervisor: "主管",
  system: "系统",
};

export const REASON_CATEGORY_LABELS = {
  quality_issue: "质量问题",
  no_reason: "无理由",
  wrong_item: "发错货",
  damaged_in_transit: "运输破损",
  not_received: "未收到货",
  other: "其他",
};

export const PRODUCT_CATEGORY_LABELS = {
  electronics: "数码家电",
  clothing: "服饰鞋包",
  home: "家居日用",
  fresh_food: "生鲜食品",
  custom_made: "定制商品",
};

export const ORDER_STATUS_LABELS = {
  pending_payment: "待支付",
  paid: "已支付未发货",
  shipped: "已发货未签收",
  delivered: "已签收",
  completed: "交易完成",
  cancelled: "已取消",
};

export const CUSTOMER_LEVEL_LABELS = {
  regular: "普通",
  vip: "会员",
};

export const TICKET_TYPE_LABELS = {
  return_refund: "退货退款",
  refund_only: "仅退款",
};

export const TICKET_STATUS_LABELS = {
  open: "待处理",
  resolved: "已处理",
  rejected: "已拒绝",
};

export const APPROVAL_STATUS_LABELS = {
  pending: "待审批",
  executing: "执行中（未确认）",
  executed: "已执行",
  execution_failed: "执行失败",
  rejected: "已拒绝",
  expired: "已过期",
};

export const EVENT_TYPE_LABELS = {
  tool_call: "工具调用",
  approval_approve: "审批同意",
  approval_reject: "审批拒绝",
  approval_execute: "执行退款",
  approval_expire: "自动过期",
};

export const CHANNEL_LABELS = {
  agent: "助手",
  console: "审批页",
  system: "系统",
};

export const OUTCOME_LABELS = {
  success: "成功",
  pending_approval: "待审批",
  access_denied: "越权拒绝",
  not_found: "不存在",
  conflict: "冲突",
  invalid: "参数错误",
  auth_failed: "鉴权失败",
  error: "系统错误",
};

export function labelOf(map, key) {
  if (key == null) return "-";
  return map[key] || String(key);
}

export function formatRegions(raw) {
  if (!raw) return "-";
  const arr = Array.isArray(raw) ? raw : String(raw).split(",");
  return arr
    .map((r) => r.trim())
    .filter(Boolean)
    .map((r) => labelOf(REGION_LABELS, r))
    .join("、");
}

export function formatMoney(val) {
  if (val == null || Number.isNaN(Number(val))) return "-";
  return `¥${Number(val).toFixed(2)}`;
}

export function formatTime(iso) {
  if (!iso) return "-";
  return String(iso).replace("T", " ").replace("+08:00", "");
}

export function formatRemaining(expiresIso, status) {
  if (status !== "pending" || !expiresIso) return "-";
  const diffMs = new Date(expiresIso).getTime() - Date.now();
  if (Number.isNaN(diffMs)) return "-";
  if (diffMs <= 0) return "已到期";
  const totalMinutes = Math.floor(diffMs / 60000);
  const hours = Math.floor(totalMinutes / 60);
  const mins = totalMinutes % 60;
  if (hours >= 24) {
    const days = Math.floor(hours / 24);
    const remHours = hours % 24;
    return `${days} 天 ${remHours} 小时`;
  }
  return `${hours} 小时 ${mins} 分`;
}

export function getToken() {
  return sessionStorage.getItem(TOKEN_KEY) || "";
}

export function setToken(token) {
  sessionStorage.setItem(TOKEN_KEY, token.trim());
}

export function clearToken() {
  sessionStorage.removeItem(TOKEN_KEY);
}

export function decodeJwtPayload(token) {
  try {
    const parts = token.split(".");
    if (parts.length < 2) return null;
    const base64 = parts[1].replace(/-/g, "+").replace(/_/g, "/");
    const padded = base64 + "=".repeat((4 - (base64.length % 4)) % 4);
    const binary = atob(padded);
    const bytes = Uint8Array.from(binary, (c) => c.charCodeAt(0));
    const jsonText = new TextDecoder("utf-8").decode(bytes);
    const obj = JSON.parse(jsonText);
    return obj && typeof obj === "object" ? obj : null;
  } catch {
    return null;
  }
}

export function getUserFromToken(token) {
  const payload = decodeJwtPayload(token);
  if (!payload) return null;
  return {
    sub: payload.sub || "",
    username: payload.preferred_username || payload.sub || "",
    name: payload.name || payload.preferred_username || payload.sub || "",
    role: payload.role || "",
    regions: payload.regions || "",
  };
}

export function el(tag, attrs = {}, children = []) {
  const node = document.createElement(tag);
  for (const [key, val] of Object.entries(attrs)) {
    if (val == null) continue;
    if (key === "className") {
      node.className = val;
    } else if (key === "text") {
      node.textContent = String(val);
    } else if (key.startsWith("on") && typeof val === "function") {
      node.addEventListener(key.slice(2).toLowerCase(), val);
    } else if (key === "disabled") {
      node.disabled = Boolean(val);
    } else {
      node.setAttribute(key, String(val));
    }
  }
  const items = Array.isArray(children) ? children : [children];
  for (const child of items) {
    if (child == null) continue;
    if (typeof child === "string" || typeof child === "number") {
      node.appendChild(document.createTextNode(String(child)));
    } else {
      node.appendChild(child);
    }
  }
  return node;
}

export function clearNode(node) {
  while (node.firstChild) {
    node.removeChild(node.firstChild);
  }
}

export function statusPill(status, labelMap = APPROVAL_STATUS_LABELS) {
  return el("span", {
    className: `pill pill-${status || "expired"}`,
    text: labelOf(labelMap, status),
  });
}

export class ApiError extends Error {
  constructor(status, code, message, requestId) {
    super(message || `请求失败（HTTP ${status}）`);
    this.status = status;
    this.code = code || "";
    this.requestId = requestId || "";
  }
}

export async function apiFetch(path, options = {}, { onUnauthorized } = {}) {
  const token = getToken();
  const headers = new Headers(options.headers || {});
  headers.set("Accept", "application/json");
  if (token) {
    headers.set("Authorization", `Bearer ${token}`);
  }
  if (options.body && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }

  const resp = await fetch(path, { ...options, headers });
  const requestId = resp.headers.get("X-Request-Id") || "";
  let data = null;
  const text = await resp.text();
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = null;
    }
  }

  if (resp.status === 401) {
    clearToken();
    if (typeof onUnauthorized === "function") {
      onUnauthorized((data && data.message) || "登录已失效，请重新登录。");
    }
    throw new ApiError(401, data?.code || "unauthorized", data?.message || "未认证或令牌已过期", requestId);
  }

  if (!resp.ok) {
    throw new ApiError(
      resp.status,
      data?.code || "",
      data?.message || `请求失败（HTTP ${resp.status}）`,
      requestId,
    );
  }
  return { status: resp.status, data, requestId };
}

export function setupAuthShell({ onLoggedIn, onLoggedOut }) {
  const userBadge = document.getElementById("user-badge");
  const logoutBtn = document.getElementById("logout-btn");
  const loginView = document.getElementById("login-view");
  const mainView = document.getElementById("main-view");
  const loginAlert = document.getElementById("login-alert");
  const devLoginSection = document.getElementById("dev-login-section");
  const devLoginForm = document.getElementById("dev-login-form");
  const tokenForm = document.getElementById("token-form");
  const roleBanner = document.getElementById("role-banner");

  if (!DEV_LOGIN_ENABLED && devLoginSection) {
    devLoginSection.classList.add("hidden");
  }

  function showLoginAlert(message, type = "danger") {
    if (!loginAlert) return;
    if (!message) {
      loginAlert.className = "alert hidden";
      loginAlert.textContent = "";
      return;
    }
    loginAlert.className = `alert alert-${type}`;
    loginAlert.textContent = message;
  }

  function showRoleBanner(message) {
    if (!roleBanner) return;
    if (!message) {
      roleBanner.className = "alert hidden";
      roleBanner.textContent = "";
      return;
    }
    roleBanner.className = "alert alert-danger";
    roleBanner.textContent = message;
  }

  function renderAuthState(noticeMessage = "") {
    const token = getToken();
    if (!token) {
      loginView.classList.remove("hidden");
      mainView.classList.add("hidden");
      logoutBtn.classList.add("hidden");
      userBadge.textContent = "未登录";
      showRoleBanner("");
      if (noticeMessage) showLoginAlert(noticeMessage, "warn");
      onLoggedOut?.();
      return;
    }

    const user = getUserFromToken(token);
    if (user) {
      const roleText = labelOf(ROLE_LABELS, user.role);
      const regionText = formatRegions(user.regions);
      userBadge.textContent = `${user.name}（${roleText}｜${regionText}）`;
    } else {
      userBadge.textContent = "已登录";
    }

    loginView.classList.add("hidden");
    mainView.classList.remove("hidden");
    logoutBtn.classList.remove("hidden");
    showLoginAlert("");
    showRoleBanner("");
    onLoggedIn?.({ user, showRoleBanner, handleUnauthorized });
  }

  function handleUnauthorized(msg) {
    renderAuthState(msg || "登录已过期，请重新登录。");
  }

  logoutBtn.addEventListener("click", () => {
    clearToken();
    renderAuthState("已退出登录。");
  });

  if (devLoginForm) {
    devLoginForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      showLoginAlert("");
      const username = document.getElementById("login-username").value.trim();
      const password = document.getElementById("login-password").value;
      const submitBtn = devLoginForm.querySelector("button[type=submit]");
      submitBtn.disabled = true;
      try {
        const resp = await fetch("/guard/v1/console/login", {
          method: "POST",
          headers: { "Content-Type": "application/json", Accept: "application/json" },
          body: JSON.stringify({ username, password }),
        });
        const data = await resp.json().catch(() => null);
        if (!resp.ok || !data?.access_token) {
          showLoginAlert(data?.message || `登录失败（HTTP ${resp.status}）`, "danger");
          return;
        }
        setToken(data.access_token);
        document.getElementById("login-password").value = "";
        renderAuthState();
      } catch {
        showLoginAlert("网络请求失败，请确认 guard 服务正在运行。", "danger");
      } finally {
        submitBtn.disabled = false;
      }
    });
  }

  if (tokenForm) {
    tokenForm.addEventListener("submit", (e) => {
      e.preventDefault();
      showLoginAlert("");
      const raw = document.getElementById("token-input").value.trim();
      const cleaned = raw.replace(/^Bearer\s+/i, "").trim();
      if (!cleaned) {
        showLoginAlert("请粘贴有效的 JWT 令牌。", "warn");
        return;
      }
      setToken(cleaned);
      document.getElementById("token-input").value = "";
      renderAuthState();
    });
  }

  renderAuthState();
  return { handleUnauthorized, showRoleBanner };
}

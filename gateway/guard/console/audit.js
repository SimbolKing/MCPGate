import {
  ApiError,
  CHANNEL_LABELS,
  EVENT_TYPE_LABELS,
  OUTCOME_LABELS,
  ROLE_LABELS,
  apiFetch,
  clearNode,
  el,
  formatRegions,
  formatTime,
  labelOf,
  setupAuthShell,
  statusPill,
} from "./common.js";

const state = {
  page: 1,
  pageSize: 50,
  total: 0,
  expandedIds: new Set(),
  showRoleBanner: null,
  handleUnauthorized: null,
};

const fields = {
  from: document.getElementById("f-from"),
  to: document.getElementById("f-to"),
  actor: document.getElementById("f-actor"),
  event_type: document.getElementById("f-event-type"),
  channel: document.getElementById("f-channel"),
  tool: document.getElementById("f-tool"),
  outcome: document.getElementById("f-outcome"),
  order_id: document.getElementById("f-order-id"),
  approval_id: document.getElementById("f-approval-id"),
  request_id: document.getElementById("f-request-id"),
};

const tbody = document.getElementById("audit-tbody");
const emptyEl = document.getElementById("audit-empty");
const errorEl = document.getElementById("audit-error");
const pageInfoEl = document.getElementById("audit-page-info");
const prevBtn = document.getElementById("audit-prev-btn");
const nextBtn = document.getElementById("audit-next-btn");

function initFromQueryString() {
  const qs = new URLSearchParams(window.location.search);
  for (const key of ["actor", "event_type", "channel", "tool", "outcome", "order_id", "approval_id", "request_id"]) {
    const val = qs.get(key);
    if (val && fields[key]) {
      fields[key].value = val;
    }
  }
}

function buildAuditUrl() {
  const params = new URLSearchParams();
  params.set("page", String(state.page));
  params.set("page_size", String(state.pageSize));
  if (fields.from.value) params.set("from", `${fields.from.value}:00+08:00`);
  if (fields.to.value) params.set("to", `${fields.to.value}:59+08:00`);
  for (const key of ["actor", "event_type", "channel", "tool", "outcome", "order_id", "approval_id", "request_id"]) {
    const val = fields[key].value.trim();
    if (val) params.set(key, val);
  }
  return `/guard/v1/console/audit-logs?${params.toString()}`;
}

async function loadAuditLogs() {
  errorEl.classList.add("hidden");
  errorEl.textContent = "";
  try {
    const { data } = await apiFetch(buildAuditUrl(), {}, { onUnauthorized: state.handleUnauthorized });
    state.showRoleBanner?.("");
    state.total = data.total || 0;
    renderAuditRows(data.items || []);
  } catch (err) {
    if (err instanceof ApiError && err.code === "insufficient_role") {
      clearNode(tbody);
      emptyEl.classList.add("hidden");
      state.showRoleBanner?.("当前账号角色不是主管，仅主管可用。请退出并使用主管账号登录。");
      return;
    }
    if (err instanceof ApiError && err.status === 401) return;
    errorEl.textContent = err.message || "查询审计记录失败";
    errorEl.classList.remove("hidden");
  }
}

function renderAuditRows(items) {
  clearNode(tbody);
  if (!items.length) {
    emptyEl.classList.remove("hidden");
  } else {
    emptyEl.classList.add("hidden");
  }

  for (const item of items) {
    const actorText = `${item.actor_name || item.actor_username || item.actor_sub} / ${labelOf(ROLE_LABELS, item.actor_role)} / ${formatRegions(item.actor_regions)}`;
    const approvalCell = item.approval_id
      ? el("span", { className: "inline-controls" }, [
          el("a", {
            href: `./index.html?approval_id=${encodeURIComponent(item.approval_id)}`,
            className: "mono",
            text: item.approval_id,
            onClick: (e) => e.stopPropagation(),
          }),
          el("button", {
            type: "button",
            className: "link-btn",
            text: "串联",
            onClick: (e) => {
              e.stopPropagation();
              fields.approval_id.value = item.approval_id;
              state.page = 1;
              loadAuditLogs();
            },
          }),
        ])
      : "-";

    const tr = el(
      "tr",
      {
        className: "clickable",
        onClick: () => {
          if (state.expandedIds.has(item.id)) {
            state.expandedIds.delete(item.id);
          } else {
            state.expandedIds.add(item.id);
          }
          renderAuditRows(items);
        },
      },
      [
        el("td", { className: "mono", text: String(item.id) }),
        el("td", { text: formatTime(item.ts) }),
        el("td", { text: actorText }),
        el("td", { text: labelOf(CHANNEL_LABELS, item.channel) }),
        el("td", { text: labelOf(EVENT_TYPE_LABELS, item.event_type) }),
        el("td", { className: "mono", text: item.tool }),
        el("td", { className: "mono", text: item.order_id || "-" }),
        el("td", {}, statusPill(item.outcome, OUTCOME_LABELS)),
        el("td", { className: "mono", text: item.status_code != null ? String(item.status_code) : "-" }),
        el("td", { text: `${item.duration_ms} ms` }),
        el("td", {}, approvalCell),
      ],
    );
    tbody.appendChild(tr);

    if (state.expandedIds.has(item.id)) {
      const detailTd = el("td", { colspan: "11" }, [
        el("div", { className: "kv-grid" }, [
          el("div", {}, [
            el("div", { className: "kv-label", text: "请求 ID" }),
            el("div", { className: "kv-value mono", text: item.request_id }),
          ]),
          el("div", {}, [
            el("div", { className: "kv-label", text: "REST 路径" }),
            el("div", {
              className: "kv-value mono",
              text: item.http_method && item.http_path ? `${item.http_method} ${item.http_path}` : "-",
            }),
          ]),
          el("div", {}, [
            el("div", { className: "kv-label", text: "错误码" }),
            el("div", { className: "kv-value mono", text: item.error_code || "-" }),
          ]),
          el("div", {}, [
            el("div", { className: "kv-label", text: "令牌 jti" }),
            el("div", { className: "kv-value mono", text: item.token_jti || "-" }),
          ]),
          el("div", {}, [
            el("div", { className: "kv-label", text: "被代表的申请人" }),
            el("div", { className: "kv-value mono", text: item.on_behalf_of_sub || "-" }),
          ]),
          el("div", {}, [
            el("div", { className: "kv-label", text: "数据区域" }),
            el("div", { className: "kv-value", text: item.resource_region ? formatRegions(item.resource_region) : "-" }),
          ]),
        ]),
        el("div", { className: "kv-label", style: "margin-top:8px;", text: "调用参数 JSON" }),
        el("pre", { className: "code-block", text: JSON.stringify(item.arguments || {}, null, 2) }),
        ...(item.result
          ? [
              el("div", { className: "kv-label", style: "margin-top:8px;", text: "结果摘要 JSON" }),
              el("pre", { className: "code-block", text: JSON.stringify(item.result, null, 2) }),
            ]
          : []),
      ]);
      tbody.appendChild(el("tr", { className: "detail-row" }, detailTd));
    }
  }

  const totalPages = Math.max(1, Math.ceil(state.total / state.pageSize));
  pageInfoEl.textContent = `共 ${state.total} 条｜第 ${state.page} / ${totalPages} 页`;
  prevBtn.disabled = state.page <= 1;
  nextBtn.disabled = state.page >= totalPages;
}

document.getElementById("audit-filter-form").addEventListener("submit", (e) => {
  e.preventDefault();
  state.page = 1;
  loadAuditLogs();
});

document.getElementById("denied-shortcut-btn").addEventListener("click", () => {
  fields.outcome.value = "access_denied";
  state.page = 1;
  loadAuditLogs();
});

document.getElementById("reset-filter-btn").addEventListener("click", () => {
  for (const input of Object.values(fields)) {
    input.value = "";
  }
  state.page = 1;
  loadAuditLogs();
});

prevBtn.addEventListener("click", () => {
  if (state.page > 1) {
    state.page -= 1;
    loadAuditLogs();
  }
});

nextBtn.addEventListener("click", () => {
  state.page += 1;
  loadAuditLogs();
});

initFromQueryString();

setupAuthShell({
  onLoggedIn: ({ showRoleBanner, handleUnauthorized }) => {
    state.showRoleBanner = showRoleBanner;
    state.handleUnauthorized = handleUnauthorized;
    loadAuditLogs();
  },
  onLoggedOut: () => {},
});

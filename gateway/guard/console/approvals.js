import {
  APPROVAL_STATUS_LABELS,
  ApiError,
  CUSTOMER_LEVEL_LABELS,
  EVENT_TYPE_LABELS,
  ORDER_STATUS_LABELS,
  OUTCOME_LABELS,
  PRODUCT_CATEGORY_LABELS,
  REASON_CATEGORY_LABELS,
  REGION_LABELS,
  ROLE_LABELS,
  TICKET_STATUS_LABELS,
  TICKET_TYPE_LABELS,
  apiFetch,
  clearNode,
  el,
  formatMoney,
  formatRemaining,
  formatTime,
  labelOf,
  setupAuthShell,
  statusPill,
} from "./common.js";

const TAB_STATUS_MAP = {
  active: ["pending", "executing"],
  done: ["executed", "execution_failed", "rejected", "expired"],
  all: [],
};

const state = {
  tab: "active",
  orderIdFilter: "",
  page: 1,
  pageSize: 20,
  total: 0,
  selectedId: new URLSearchParams(window.location.search).get("approval_id") || "",
  displayedDetailId: "",
  displayedDetailStatus: "",
  displayedDetailHasConflict: false,
  draftComments: {},
  refreshTimer: null,
  actionBusy: false,
  showRoleBanner: null,
  handleUnauthorized: null,
};

const tbody = document.getElementById("approvals-tbody");
const emptyEl = document.getElementById("approvals-empty");
const pageInfoEl = document.getElementById("approvals-page-info");
const prevBtn = document.getElementById("prev-page-btn");
const nextBtn = document.getElementById("next-page-btn");
const detailPanel = document.getElementById("detail-panel");
const orderSearchInput = document.getElementById("order-search-input");

function stopAutoRefresh() {
  if (state.refreshTimer) {
    clearInterval(state.refreshTimer);
    state.refreshTimer = null;
  }
}

function startAutoRefresh() {
  stopAutoRefresh();
  state.refreshTimer = setInterval(() => {
    if (!state.actionBusy) {
      loadApprovals({ silent: true });
    }
  }, 30000);
}

function buildListUrl() {
  const params = new URLSearchParams();
  params.set("page", String(state.page));
  params.set("page_size", String(state.pageSize));
  const statuses = TAB_STATUS_MAP[state.tab] || [];
  for (const s of statuses) {
    params.append("status", s);
  }
  if (state.orderIdFilter) {
    params.set("order_id", state.orderIdFilter);
  }
  return `/guard/v1/console/approvals?${params.toString()}`;
}

function showStaleNotice(approvalId) {
  const staleNotice = document.getElementById("detail-stale-notice");
  if (!staleNotice) return;
  clearNode(staleNotice);
  staleNotice.className = "alert alert-warn";
  const refreshLinkBtn = el("button", {
    type: "button",
    className: "btn btn-sm",
    style: "margin-left: 8px;",
    text: "点击刷新",
    onClick: () => loadDetail(approvalId, { preserveComment: true }),
  });
  staleNotice.appendChild(document.createTextNode("状态已变化，点击刷新查看最新详情。"));
  staleNotice.appendChild(refreshLinkBtn);
}

export function hasSelectedDetailChanged(item, detail) {
  if (!item || !detail) {
    return false;
  }
  return (
    item.status !== detail.status ||
    Boolean(item.has_late_execution_conflict) !== Boolean(detail.has_late_execution_conflict)
  );
}

function syncDetailStatusFromList(items) {
  if (!state.selectedId || state.displayedDetailId !== state.selectedId) {
    return;
  }
  const matched = items.find((item) => item.approval_id === state.selectedId);
  if (matched) {
    const displayedDetail = {
      status: state.displayedDetailStatus,
      has_late_execution_conflict: state.displayedDetailHasConflict,
    };
    if (hasSelectedDetailChanged(matched, displayedDetail)) {
      if (matched.status !== displayedDetail.status) {
        const statusSlot = document.getElementById("detail-status-slot");
        if (statusSlot) {
          clearNode(statusSlot);
          statusSlot.appendChild(statusPill(matched.status));
        }
      }
      showStaleNotice(state.selectedId);
    }
    return;
  }
  if (
    state.tab === "active" &&
    !state.orderIdFilter &&
    state.page === 1 &&
    state.total <= state.pageSize &&
    (state.displayedDetailStatus === "pending" || state.displayedDetailStatus === "executing")
  ) {
    showStaleNotice(state.selectedId);
  }
}

async function loadApprovals({
  silent = false,
  refreshDetail = false,
  preserveComment = true,
} = {}) {
  try {
    const { data } = await apiFetch(buildListUrl(), {}, { onUnauthorized: state.handleUnauthorized });
    state.showRoleBanner?.("");
    state.total = data.total || 0;
    const items = data.items || [];
    renderList(items);

    if (silent) {
      syncDetailStatusFromList(items);
    } else if (refreshDetail && state.selectedId) {
      await loadDetail(state.selectedId, {
        preserveComment: preserveComment && state.displayedDetailId === state.selectedId,
      });
    } else {
      syncDetailStatusFromList(items);
    }
  } catch (err) {
    if (err instanceof ApiError && err.code === "insufficient_role") {
      stopAutoRefresh();
      clearNode(tbody);
      emptyEl.classList.add("hidden");
      clearNode(detailPanel);
      state.displayedDetailId = "";
      state.displayedDetailStatus = "";
      state.displayedDetailHasConflict = false;
      state.showRoleBanner?.("当前账号角色不是主管，仅主管可用。请退出并使用主管账号登录。");
      return;
    }
    if (!silent && err instanceof ApiError && err.status !== 401) {
      clearNode(detailPanel);
      state.displayedDetailId = "";
      state.displayedDetailStatus = "";
      state.displayedDetailHasConflict = false;
      detailPanel.appendChild(el("div", { className: "alert alert-danger", text: err.message }));
    }
  }
}

function renderList(items) {
  clearNode(tbody);
  if (!items.length) {
    emptyEl.classList.remove("hidden");
  } else {
    emptyEl.classList.add("hidden");
  }

  for (const item of items) {
    const tr = el(
      "tr",
      {
        className: `clickable${item.approval_id === state.selectedId ? " selected" : ""}`,
        onClick: () => {
          const isSame = item.approval_id === state.displayedDetailId;
          state.selectedId = item.approval_id;
          renderList(items);
          loadDetail(item.approval_id, { preserveComment: isSame });
        },
      },
      [
        el("td", { className: "mono", text: item.approval_id }),
        el("td", { className: "mono", text: item.order_id }),
        el("td", { text: labelOf(REGION_LABELS, item.region) }),
        el("td", { text: formatMoney(item.amount) }),
        el("td", { text: labelOf(REASON_CATEGORY_LABELS, item.reason_category) }),
        el("td", { text: item.applicant_name || item.applicant_username || item.applicant_sub }),
        el("td", { text: formatTime(item.submitted_at) }),
        el("td", { text: formatRemaining(item.expires_at, item.status) }),
        el("td", {}, statusPill(item.status)),
      ],
    );
    tbody.appendChild(tr);
  }

  const totalPages = Math.max(1, Math.ceil(state.total / state.pageSize));
  pageInfoEl.textContent = `共 ${state.total} 条｜第 ${state.page} / ${totalPages} 页`;
  prevBtn.disabled = state.page <= 1;
  nextBtn.disabled = state.page >= totalPages;
}

function kv(label, valueNodeOrText) {
  const valueEl =
    typeof valueNodeOrText === "string" || typeof valueNodeOrText === "number"
      ? el("div", { className: "kv-value", text: String(valueNodeOrText) })
      : el("div", { className: "kv-value" }, valueNodeOrText);
  return el("div", { className: "kv-item" }, [
    el("div", { className: "kv-label", text: label }),
    valueEl,
  ]);
}

async function loadDetail(approvalId, { preserveComment = false } = {}) {
  let preservedComment = "";
  if (preserveComment && state.displayedDetailId === approvalId) {
    const currentInput = document.getElementById("decision-comment");
    if (currentInput) {
      preservedComment = currentInput.value;
      state.draftComments[approvalId] = preservedComment;
    } else if (typeof state.draftComments[approvalId] === "string") {
      preservedComment = state.draftComments[approvalId];
    }
  }

  clearNode(detailPanel);
  detailPanel.appendChild(el("div", { className: "muted", text: `正在加载 ${approvalId} 详情...` }));

  try {
    const { data } = await apiFetch(
      `/guard/v1/console/approvals/${encodeURIComponent(approvalId)}`,
      {},
      { onUnauthorized: state.handleUnauthorized },
    );
    renderDetail(data, { preservedComment });
  } catch (err) {
    if (err instanceof ApiError && err.status === 401) return;
    clearNode(detailPanel);
    state.displayedDetailId = "";
    state.displayedDetailStatus = "";
    state.displayedDetailHasConflict = false;
    detailPanel.appendChild(
      el("div", { className: "alert alert-danger", text: err.message || "加载详情失败" }),
    );
  }
}

function renderDetail(detail, { preservedComment = "" } = {}) {
  clearNode(detailPanel);
  state.displayedDetailId = detail.approval_id;
  state.displayedDetailStatus = detail.status;
  state.displayedDetailHasConflict = Boolean(detail.has_late_execution_conflict);

  // Header
  const statusSlot = el("span", { id: "detail-status-slot" }, statusPill(detail.status));
  const headerActions = el("span", { className: "inline-controls" }, [
    el("button", {
      type: "button",
      className: "btn btn-sm",
      text: "刷新详情",
      onClick: () => loadDetail(detail.approval_id, { preserveComment: true }),
    }),
    el("a", {
      href: `./audit.html?approval_id=${encodeURIComponent(detail.approval_id)}`,
      className: "btn btn-sm",
      text: "查看关联审计",
    }),
  ]);
  const header = el("div", { className: "section-title" }, [
    el("span", {}, [el("span", { className: "mono", text: detail.approval_id }), " ", statusSlot]),
    headerActions,
  ]);
  detailPanel.appendChild(header);

  const staleNotice = el("div", {
    id: "detail-stale-notice",
    className: "alert alert-warn hidden",
  });
  detailPanel.appendChild(staleNotice);

  // Status banner (3.6.5)
  if (detail.status === "execution_failed") {
    const failureText = `已同意，但订单系统拒绝执行：${detail.failure_message || "未知原因"}${
      detail.failure_code ? `（错误码：${detail.failure_code}）` : ""
    }`;
    detailPanel.appendChild(el("div", { className: "alert alert-danger", text: failureText }));
    const conflictText =
      detail.late_execution_notice ||
      (detail.late_executed_refund_id
        ? `订单系统另有一次执行成功，退款单号 ${detail.late_executed_refund_id}，请人工核对`
        : detail.has_late_execution_conflict
          ? "订单系统另有一次执行成功，退款单号未知，请到订单系统人工核对"
          : "");
    if (conflictText) {
      detailPanel.appendChild(el("div", { className: "alert alert-warn", text: conflictText }));
    }
  } else if (detail.status === "executing") {
    const execText = `执行结果未确认${
      detail.failure_message ? `：${detail.failure_message}` : ""
    }（已尝试 ${detail.attempts} 次），请确认后点击“重试执行”。`;
    detailPanel.appendChild(el("div", { className: "alert alert-warn", text: execText }));
  } else if (detail.status === "executed") {
    detailPanel.appendChild(
      el("div", {
        className: "alert alert-ok",
        text: `已执行完成，退款单号：${detail.refund_id || "-"}（执行时间：${formatTime(detail.executed_at)}）`,
      }),
    );
  } else if (detail.status === "rejected") {
    detailPanel.appendChild(
      el("div", {
        className: "alert alert-warn",
        text: `已拒绝：${detail.decision_comment || "-"}（审批人：${detail.decided_by_name || detail.decided_by_sub || "-"}）`,
      }),
    );
  } else if (detail.status === "expired") {
    detailPanel.appendChild(el("div", { className: "alert alert-info", text: detail.message }));
  }

  // Application section
  detailPanel.appendChild(el("div", { className: "section-title", text: "申请内容" }));
  const snapshot = detail.order_snapshot || {};
  const exceedsRefundable =
    typeof snapshot.refundable_amount === "number" && detail.amount > snapshot.refundable_amount;

  const amountDisplay = exceedsRefundable
    ? el("span", {
        className: "highlight-warn",
        text: `${formatMoney(detail.amount)}（超出快照可退金额 ${formatMoney(snapshot.refundable_amount)}）`,
      })
    : formatMoney(detail.amount);

  const applicantDisplay = `${detail.applicant_name || "-"} / ${detail.applicant_username || detail.applicant_sub} / ${labelOf(ROLE_LABELS, detail.applicant_role)}`;

  detailPanel.appendChild(
    el("div", { className: "kv-grid" }, [
      kv("订单号", detail.order_id),
      kv("所属区域", labelOf(REGION_LABELS, detail.region)),
      kv("申请金额", amountDisplay),
      kv("原因类别", labelOf(REASON_CATEGORY_LABELS, detail.reason_category)),
      kv("关联工单", detail.ticket_id || "无"),
      kv("申请人", applicantDisplay),
      kv("提交时间", formatTime(detail.submitted_at)),
      kv("截止时间", formatTime(detail.expires_at)),
    ]),
  );
  detailPanel.appendChild(
    el("div", { style: "margin-top: 8px;" }, [
      el("div", { className: "kv-label", text: "退款说明" }),
      el("div", { className: "kv-value", text: detail.reason }),
    ]),
  );

  // Order snapshot section
  detailPanel.appendChild(el("div", { className: "section-title", text: "提交时的订单快照" }));
  const customer = snapshot.customer || {};
  detailPanel.appendChild(
    el("div", { className: "kv-grid" }, [
      kv("订单状态", labelOf(ORDER_STATUS_LABELS, snapshot.status)),
      kv("客户姓名 / 等级", `${customer.name || "-"}（${labelOf(CUSTOMER_LEVEL_LABELS, customer.level)}）`),
      kv("实付金额", formatMoney(snapshot.total_amount)),
      kv("已退 / 可退", `${formatMoney(snapshot.refunded_amount)} / ${formatMoney(snapshot.refundable_amount)}`),
      kv("签收时间", formatTime(snapshot.delivered_at)),
      kv("已签收天数", snapshot.days_since_delivery != null ? `${snapshot.days_since_delivery} 天` : "-"),
    ]),
  );

  if (Array.isArray(snapshot.items) && snapshot.items.length) {
    const itemUl = el("ul", { className: "sub-list" });
    for (const it of snapshot.items) {
      itemUl.appendChild(
        el("li", {
          text: `${it.product_name || "-"}（${labelOf(PRODUCT_CATEGORY_LABELS, it.category)}）× ${it.quantity}｜单价 ${formatMoney(it.unit_price)}`,
        }),
      );
    }
    detailPanel.appendChild(el("div", { className: "kv-label", style: "margin-top:8px;", text: "商品明细" }));
    detailPanel.appendChild(itemUl);
  }

  if (Array.isArray(snapshot.after_sale_tickets) && snapshot.after_sale_tickets.length) {
    const ticketUl = el("ul", { className: "sub-list" });
    for (const tk of snapshot.after_sale_tickets) {
      ticketUl.appendChild(
        el("li", {
          text: `${tk.ticket_id}｜${labelOf(TICKET_TYPE_LABELS, tk.type)}｜${labelOf(REASON_CATEGORY_LABELS, tk.reason_category)}｜${labelOf(TICKET_STATUS_LABELS, tk.status)}：${tk.description || ""}`,
        }),
      );
    }
    detailPanel.appendChild(el("div", { className: "kv-label", style: "margin-top:8px;", text: "售后工单" }));
    detailPanel.appendChild(ticketUl);
  }

  // Live order section
  const liveContainer = el("div", { className: "muted", text: "正在实时查询订单现状..." });
  const reloadLiveBtn = el("button", {
    type: "button",
    className: "btn btn-sm",
    text: "刷新现状",
    onClick: () => fetchLiveOrder(detail.approval_id, liveContainer),
  });
  detailPanel.appendChild(
    el("div", { className: "section-title" }, [el("span", { text: "订单现状（实时查询）" }), reloadLiveBtn]),
  );
  detailPanel.appendChild(liveContainer);
  fetchLiveOrder(detail.approval_id, liveContainer);

  // Action section (pending / executing)
  if (detail.status === "pending" || detail.status === "executing") {
    const actionBox = el("div", { className: "action-box" });
    const actionFeedback = el("div", { className: "alert hidden" });
    actionBox.appendChild(actionFeedback);

    if (detail.status === "pending") {
      const commentInput = el("textarea", {
        id: "decision-comment",
        maxlength: "200",
        placeholder: "审批意见（同意时可不填，拒绝时必填，最多 200 字）",
      });
      if (preservedComment) {
        commentInput.value = preservedComment;
      }
      commentInput.addEventListener("input", () => {
        state.draftComments[detail.approval_id] = commentInput.value;
      });
      actionBox.appendChild(el("label", { for: "decision-comment", text: "审批意见" }));
      actionBox.appendChild(commentInput);

      const approveBtn = el("button", {
        type: "button",
        className: "btn btn-primary",
        text: "同意并执行",
      });
      const rejectBtn = el("button", {
        type: "button",
        className: "btn btn-danger",
        text: "拒绝",
      });

      approveBtn.addEventListener("click", async () => {
        const confirmed = window.confirm(
          `同意后将以你的身份立即在订单系统执行退款 ${formatMoney(detail.amount)}，确认继续吗？`,
        );
        if (!confirmed) return;
        await runDecisionAction({
          approvalId: detail.approval_id,
          action: "approve",
          body: { comment: commentInput.value.trim() || null },
          buttons: [approveBtn, rejectBtn],
          feedbackEl: actionFeedback,
        });
      });

      rejectBtn.addEventListener("click", async () => {
        const comment = commentInput.value.trim();
        if (!comment) {
          actionFeedback.className = "alert alert-warn";
          actionFeedback.textContent = "拒绝申请时必须填写审批意见。";
          return;
        }
        await runDecisionAction({
          approvalId: detail.approval_id,
          action: "reject",
          body: { comment },
          buttons: [approveBtn, rejectBtn],
          feedbackEl: actionFeedback,
        });
      });

      actionBox.appendChild(el("div", { className: "action-buttons" }, [approveBtn, rejectBtn]));
    } else if (detail.status === "executing") {
      const retryBtn = el("button", {
        type: "button",
        className: "btn btn-warn",
        text: "重试执行",
      });
      retryBtn.addEventListener("click", async () => {
        const confirmed = window.confirm(
          `将使用同一幂等键以你的身份重试执行退款 ${formatMoney(detail.amount)}，确认继续吗？`,
        );
        if (!confirmed) return;
        await runDecisionAction({
          approvalId: detail.approval_id,
          action: "retry",
          body: null,
          buttons: [retryBtn],
          feedbackEl: actionFeedback,
        });
      });
      actionBox.appendChild(el("div", { className: "action-buttons" }, [retryBtn]));
    }

    detailPanel.appendChild(actionBox);
  }

  // Timeline section
  detailPanel.appendChild(el("div", { className: "section-title", text: "处理记录（审计时间线）" }));
  const events = detail.events || [];
  if (!events.length) {
    detailPanel.appendChild(el("div", { className: "muted", text: "暂无处理记录" }));
  } else {
    const ul = el("ul", { className: "timeline" });
    for (const ev of events) {
      const isDenied = ev.outcome === "access_denied" || ev.outcome === "error" || ev.outcome === "conflict";
      const resultSummary = ev.result ? `｜${JSON.stringify(ev.result)}` : "";
      const errSummary = ev.error_code ? `｜错误：${ev.error_code}` : "";
      const commentSummary = ev.arguments?.comment ? `｜意见：${ev.arguments.comment}` : "";
      ul.appendChild(
        el("li", { className: isDenied ? "denied" : "" }, [
          el("div", {}, [
            el("strong", { text: labelOf(EVENT_TYPE_LABELS, ev.event_type) }),
            " ",
            statusPill(ev.outcome, OUTCOME_LABELS),
            el("span", { className: "muted", text: `  ${formatTime(ev.ts)}` }),
          ]),
          el("div", {
            className: "muted",
            text: `操作人：${ev.actor_name || ev.actor_username || ev.actor_sub}（${labelOf(ROLE_LABELS, ev.actor_role)}）${ev.status_code != null ? `｜HTTP ${ev.status_code}` : ""}${commentSummary}${errSummary}${resultSummary}`,
          }),
        ]),
      );
    }
    detailPanel.appendChild(ul);
  }
}

async function fetchLiveOrder(approvalId, container) {
  clearNode(container);
  container.appendChild(el("div", { className: "muted", text: "查询中..." }));
  try {
    const { data } = await apiFetch(
      `/guard/v1/console/approvals/${encodeURIComponent(approvalId)}/order`,
      {},
      { onUnauthorized: state.handleUnauthorized },
    );
    clearNode(container);
    container.appendChild(
      el("div", { className: "kv-grid" }, [
        kv("当前状态", labelOf(ORDER_STATUS_LABELS, data.status)),
        kv("实付金额", formatMoney(data.total_amount)),
        kv("当前已退", formatMoney(data.refunded_amount)),
        kv("当前可退", formatMoney(data.refundable_amount)),
      ]),
    );
    const refunds = data.refunds || [];
    if (refunds.length) {
      const ul = el("ul", { className: "sub-list" });
      for (const rf of refunds) {
        ul.appendChild(
          el("li", {
            text: `${rf.refund_id}｜${formatMoney(rf.amount)}｜${labelOf(REASON_CATEGORY_LABELS, rf.reason_category)}｜${rf.status}｜${formatTime(rf.created_at)}`,
          }),
        );
      }
      container.appendChild(el("div", { className: "kv-label", style: "margin-top:8px;", text: "已有退款单" }));
      container.appendChild(ul);
    } else {
      container.appendChild(el("div", { className: "muted", style: "margin-top:6px;", text: "暂无已有退款单" }));
    }
  } catch (err) {
    if (err instanceof ApiError && err.status === 401) return;
    clearNode(container);
    container.appendChild(
      el("div", { className: "alert alert-warn", text: `无法获取订单现状：${err.message}` }),
    );
  }
}

async function runDecisionAction({ approvalId, action, body, buttons, feedbackEl }) {
  state.actionBusy = true;
  buttons.forEach((b) => {
    b.disabled = true;
  });
  feedbackEl.className = "alert hidden";
  try {
    await apiFetch(
      `/guard/v1/console/approvals/${encodeURIComponent(approvalId)}/${action}`,
      {
        method: "POST",
        body: body != null ? JSON.stringify(body) : undefined,
      },
      { onUnauthorized: state.handleUnauthorized },
    );
    delete state.draftComments[approvalId];
    await loadApprovals({ refreshDetail: true, preserveComment: false });
  } catch (err) {
    if (err instanceof ApiError && err.status === 401) return;
    feedbackEl.className = "alert alert-danger";
    feedbackEl.textContent = err.message || "操作失败";
    buttons.forEach((b) => {
      b.disabled = false;
    });
  } finally {
    state.actionBusy = false;
  }
}

// Wire controls
document.getElementById("status-tabs").addEventListener("click", (e) => {
  const btn = e.target.closest("button[data-tab]");
  if (!btn) return;
  state.tab = btn.dataset.tab;
  state.page = 1;
  document.querySelectorAll("#status-tabs .tab").forEach((t) => {
    t.classList.toggle("active", t === btn);
  });
  loadApprovals();
});

document.getElementById("search-btn").addEventListener("click", () => {
  state.orderIdFilter = orderSearchInput.value.trim();
  state.page = 1;
  loadApprovals();
});

orderSearchInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter") {
    state.orderIdFilter = orderSearchInput.value.trim();
    state.page = 1;
    loadApprovals();
  }
});

document.getElementById("refresh-btn").addEventListener("click", () =>
  loadApprovals({ refreshDetail: Boolean(state.selectedId) }),
);

prevBtn.addEventListener("click", () => {
  if (state.page > 1) {
    state.page -= 1;
    loadApprovals();
  }
});

nextBtn.addEventListener("click", () => {
  state.page += 1;
  loadApprovals();
});

setupAuthShell({
  onLoggedIn: ({ showRoleBanner, handleUnauthorized }) => {
    state.showRoleBanner = showRoleBanner;
    state.handleUnauthorized = handleUnauthorized;
    loadApprovals({ refreshDetail: Boolean(state.selectedId) });
    startAutoRefresh();
  },
  onLoggedOut: () => {
    stopAutoRefresh();
    state.displayedDetailId = "";
    state.displayedDetailStatus = "";
    state.displayedDetailHasConflict = false;
    state.draftComments = {};
  },
});

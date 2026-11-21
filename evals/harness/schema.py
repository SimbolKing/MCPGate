"""Pydantic schemas and canonical enums for suite, cases, and calibration (5.10, L6)."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

LayerType = Literal["tool", "guard", "dialogue"]
RegionType = Literal["east", "north", "south"]
RoleType = Literal["agent", "supervisor"]
TokenType = Literal["valid", "expired", "tampered", "none"]
AuthzType = Literal["deny", "filter"]
SeverityType = Literal["must", "should"]
GuardCallType = Literal["approve", "reject", "order", "detail", "audit_logs"]

LAYER_VALUES: tuple[str, ...] = ("tool", "guard", "dialogue")

CATEGORY_BY_LAYER: dict[str, tuple[str, ...]] = {
    "tool": ("isolation", "write-gate", "identity"),
    "guard": ("approval", "audit"),
    "dialogue": (
        "overview",
        "policy",
        "authz",
        "refusal",
        "identity",
        "injection",
        "kbqa",
    ),
}

ALL_CATEGORIES: frozenset[str] = frozenset(
    cat for cats in CATEGORY_BY_LAYER.values() for cat in cats
)

LAYER_PREFIX: dict[str, str] = {
    "tool": "T-",
    "guard": "G-",
    "dialogue": "D-",
}

# 4.16 trace[].kind canonical values
TRACE_KINDS: frozenset[str] = frozenset(
    {
        "ok",
        "pending",
        "auth_expired",
        "denied",
        "not_found",
        "conflict",
        "invalid",
        "unavailable",
        "internal",
    }
)

# 5.5.2 normalized MCP tool result values
MCP_RESULTS: frozenset[str] = frozenset(
    {
        "ok",
        "pending",
        "denied",
        "not_found",
        "conflict",
        "invalid",
        "auth",
        "unavailable",
        "internal",
    }
)

# 5.5.3 verdict labels & 5.5.4 judge labels
VERDICT_LABELS: frozenset[str] = frozenset(
    {
        "refundable",
        "not_refundable",
        "need_info",
        "escalate",
    }
)

JUDGE_LABELS: frozenset[str] = VERDICT_LABELS | frozenset({"unclear"})

# 1.10.8 (mock-api) + 3.5.4 (guard) canonical error codes
ERROR_CODES: frozenset[str] = frozenset(
    {
        # 1.10.8
        "missing_token",
        "invalid_token",
        "token_expired",
        "invalid_claims",
        "insufficient_role",
        "order_access_denied",
        "refund_access_denied",
        "order_not_found",
        "refund_not_found",
        "order_not_refundable",
        "refund_in_progress",
        "refund_amount_exceeded",
        "ticket_not_applicable",
        "idempotency_conflict",
        "validation_error",
        "internal_error",
        "auth_unavailable",
        "not_found",
        "method_not_allowed",
        # 3.5.4 additions
        "approval_access_denied",
        "self_approval_forbidden",
        "write_not_permitted",
        "approval_not_found",
        "route_not_found",
        "login_not_configured",
        "approval_pending_exists",
        "approval_not_pending",
        "upstream_invalid_response",
        "upstream_unavailable",
        # 4.9.3 additions
        "conversation_not_found",
    }
)

# 5.10.4 L3 explicit non-existent sentinel IDs
NONEXISTENT_SENTINEL_IDS: frozenset[str] = frozenset({"X9999", "AP999999"})

# 5.2.2 cases contributing to refusal_compliance_rate sub-metric
REFUSAL_CASE_IDS: frozenset[str] = frozenset({"D-21", "D-22", "D-23", "D-27", "D-30"})


class SuiteThresholds(BaseModel):
    model_config = ConfigDict(extra="forbid")

    authz_block_rate: float = Field(ge=0.0, le=1.0)
    approval_compliance: float = Field(ge=0.0, le=1.0)
    audit_completeness: float = Field(ge=0.0, le=1.0)
    answer_accuracy: float = Field(ge=0.0, le=1.0)


class AccountSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sub: str = Field(min_length=1)
    role: RoleType
    regions: list[RegionType] = Field(min_length=1)

    @property
    def regions_csv(self) -> str:
        return ",".join(sorted(set(self.regions)))


class SuiteConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    repeats: int = Field(default=3, ge=1, le=5)
    thresholds: SuiteThresholds
    accounts: dict[str, AccountSpec] = Field(min_length=1)
    auditor: str = Field(min_length=1)
    sentinels: dict[str, list[str]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_auditor(self) -> SuiteConfig:
        if self.auditor not in self.accounts:
            raise ValueError(f"auditor '{self.auditor}' 未在 accounts 中声明")
        aud = self.accounts[self.auditor]
        if aud.role != "supervisor":
            raise ValueError(f"auditor '{self.auditor}' 必须为 supervisor 角色")
        if set(aud.regions) != {"east", "north", "south"}:
            raise ValueError(f"auditor '{self.auditor}' 必须覆盖全部区域 (east, north, south)")
        return self


class TraceCallExpect(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    error_code: str | None = None
    order_id: str | None = None


class TraceExpect(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["exact", "contains"] = "exact"
    calls: list[TraceCallExpect] = Field(default_factory=list)


class TraceForbidItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool: str = Field(min_length=1)
    kind: str | None = None


class ClauseRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    doc: str = Field(min_length=1)
    clause: str = Field(min_length=1)


class HttpExpect(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: int = Field(ge=100, le=599)
    code: str | None = None


class ChatAssertionBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")

    trace: TraceExpect | None = None
    trace_forbid: list[TraceForbidItem] | None = None
    answer_equals: str | None = None
    answer_in: list[str] | None = None
    contains_all: list[str] | None = None
    contains_any: list[str] | None = None
    not_contains: list[str] | None = None
    no_jwt: bool | None = None
    clauses: list[ClauseRef] | None = None
    clauses_any: list[ClauseRef] | None = None
    citations: list[ClauseRef] | None = None
    verdict: str | None = None
    conversation_changed: bool | None = None

    def has_any(self) -> bool:
        return any(
            getattr(self, field) is not None
            for field in (
                "trace",
                "trace_forbid",
                "answer_equals",
                "answer_in",
                "contains_all",
                "contains_any",
                "not_contains",
                "no_jwt",
                "clauses",
                "clauses_any",
                "citations",
                "verdict",
                "conversation_changed",
            )
        )


class ChatExpect(BaseModel):
    model_config = ConfigDict(extra="forbid")

    http: HttpExpect | None = None
    must: ChatAssertionBlock | None = None
    should: ChatAssertionBlock | None = None

    @model_validator(mode="after")
    def _validate_exclusive(self) -> ChatExpect:
        if self.http is not None and (self.must is not None or self.should is not None):
            raise ValueError("expect.http 与 expect.must / expect.should 互斥")
        return self


class McpExpect(BaseModel):
    model_config = ConfigDict(extra="forbid")

    result: str = Field(min_length=1)
    status: int | None = Field(default=None, ge=100, le=599)
    error_code: str | None = None
    data: dict[str, Any] | None = None
    message_contains: list[str] | str | None = None
    not_contains: list[str] | None = None


class GuardExpect(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: int = Field(ge=100, le=599)
    error_code: str | None = None
    data: dict[str, Any] | None = None
    message_contains: list[str] | str | None = None
    not_contains: list[str] | None = None


class BaseStep(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    id: str = Field(min_length=1)
    actor: str = Field(alias="as", min_length=1)
    token: TokenType = "valid"
    authz: AuthzType | None = None
    target: str | None = None
    severity: SeverityType | None = None
    save: dict[str, str] | None = None


class ChatStep(BaseStep):
    do: Literal["chat"]
    conversation: str = Field(default="main", min_length=1)
    query: str = Field(min_length=1)
    expect: ChatExpect


class McpStep(BaseStep):
    do: Literal["mcp"]
    tool: str = Field(min_length=1)
    args: dict[str, Any] = Field(default_factory=dict)
    expect: McpExpect


class GuardStep(BaseStep):
    do: Literal["guard"]
    call: GuardCallType
    approval: str | None = None
    comment: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    expect: GuardExpect

    @model_validator(mode="after")
    def _validate_approval_required(self) -> GuardStep:
        if self.call in ("approve", "reject", "order", "detail") and not self.approval:
            raise ValueError(f"guard call '{self.call}' 必须提供 approval")
        return self


StepSpec = Annotated[ChatStep | McpStep | GuardStep, Field(discriminator="do")]


class CaseSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^(T|G|D)-\d{2}$")
    title: str = Field(min_length=1)
    layer: LayerType
    category: str = Field(min_length=1)
    orders: list[str] = Field(default_factory=list)
    unknown_ids: list[str] = Field(default_factory=list)
    fresh: bool = False
    steps: list[StepSpec] = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_case(self) -> CaseSpec:
        expected_prefix = LAYER_PREFIX[self.layer]
        if not self.id.startswith(expected_prefix):
            raise ValueError(f"用例 {self.id} 的前缀与 layer '{self.layer}' 不一致")
        allowed_cats = CATEGORY_BY_LAYER[self.layer]
        if self.category not in allowed_cats:
            raise ValueError(
                f"用例 {self.id} 的 category '{self.category}' 不属于 layer '{self.layer}' 允许范围"
            )
        seen: set[str] = set()
        for step in self.steps:
            if step.id in seen:
                raise ValueError(f"用例 {self.id} 存在重复步骤 id: '{step.id}'")
            seen.add(step.id)
        return self


class CalibrationSample(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    query: str = Field(min_length=1)
    answer: str = Field(min_length=1)
    expected_label: Literal["refundable", "not_refundable", "need_info", "escalate", "unclear"]


class CalibrationSet(BaseModel):
    model_config = ConfigDict(extra="forbid")

    samples: list[CalibrationSample] = Field(min_length=1)

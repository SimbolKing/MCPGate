from __future__ import annotations

from fastapi import APIRouter, Depends, Path, Query, Request, Response
from sqlalchemy.orm import Session

from app import services
from app.auth import Principal, get_principal, get_settings, require_role
from app.clock import Clock, get_clock
from app.config import Settings
from app.db import get_session
from app.schemas import (
    CreateRefundRequest,
    ErrorResponse,
    RefundListResponse,
    RefundOut,
    RefundStatus,
)

router = APIRouter(
    prefix="/api/v1",
    tags=["refunds"],
    dependencies=[Depends(get_principal)],
)


@router.get(
    "/refunds/{refund_id}",
    operation_id="get_refund",
    summary="查询退款单详情",
    description=(
        "按退款单号查询一张退款单的金额、原因、状态和操作人。"
        "只能查询当前登录人负责区域内的退款单；不在负责区域返回 403，退款单号不存在返回 404。"
    ),
    response_model=RefundOut,
    response_model_exclude_none=True,
    responses={
        401: {"model": ErrorResponse, "description": "未认证或身份令牌无效"},
        403: {"model": ErrorResponse, "description": "令牌声明无效或无权访问该退款单"},
        404: {"model": ErrorResponse, "description": "退款单号不存在"},
    },
)
def get_refund(
    refund_id: str = Path(..., description="退款单号，如 R000105"),
    principal: Principal = Depends(get_principal),  # noqa: B008
    session: Session = Depends(get_session),  # noqa: B008
    settings: Settings = Depends(get_settings),  # noqa: B008
) -> RefundOut:
    return services.get_refund_detail(
        session,
        principal,
        refund_id,
        tz_name=settings.timezone,
    )


@router.get(
    "/refunds",
    operation_id="list_refunds",
    summary="查询退款记录（仅主管）",
    description=(
        "分页查询当前登录人负责区域内的退款单，可按订单号和退款状态筛选，按创建时间倒序返回。"
        "仅主管可用，客服调用返回 403。"
    ),
    response_model=RefundListResponse,
    response_model_exclude_none=True,
    responses={
        401: {"model": ErrorResponse, "description": "未认证或身份令牌无效"},
        403: {"model": ErrorResponse, "description": "令牌声明无效或当前角色无权调用"},
    },
)
def list_refunds(
    order_id: str | None = Query(default=None, description="只看某个订单的退款单"),
    status: RefundStatus | None = Query(default=None, description="退款状态"),
    page: int = Query(default=1, ge=1, description="页码，从 1 开始，默认 1"),
    page_size: int = Query(default=20, ge=1, le=50, description="每页条数，范围 1–50，默认 20"),
    principal: Principal = Depends(require_role("supervisor")),  # noqa: B008
    session: Session = Depends(get_session),  # noqa: B008
    settings: Settings = Depends(get_settings),  # noqa: B008
) -> RefundListResponse:
    return services.list_refund_records(
        session,
        principal,
        order_id=order_id,
        status=status,
        page=page,
        page_size=page_size,
        tz_name=settings.timezone,
    )


@router.post(
    "/refunds",
    operation_id="create_refund",
    summary="发起退款【写操作】",
    description=(
        "【写操作】为指定订单创建退款单并立即执行退款，会增加订单的已退金额，不可撤销。"
        "只能对当前登录人负责区域内的订单操作。订单未支付或已取消、已有处理中的退款、"
        "金额超过可退金额时会被拒绝。本接口只校验订单数据，不判断是否符合售后制度。"
    ),
    status_code=201,
    response_model=RefundOut,
    response_model_exclude_none=True,
    responses={
        401: {"model": ErrorResponse, "description": "未认证或身份令牌无效"},
        403: {"model": ErrorResponse, "description": "令牌声明无效或无权操作该订单"},
        404: {"model": ErrorResponse, "description": "订单号不存在"},
        409: {
            "model": ErrorResponse,
            "description": "业务规则冲突（不可退、超额、处理中、幂等冲突等）",
        },
    },
)
def create_refund(
    body: CreateRefundRequest,
    request: Request,
    response: Response,
    principal: Principal = Depends(get_principal),  # noqa: B008
    session: Session = Depends(get_session),  # noqa: B008
    clock: Clock = Depends(get_clock),  # noqa: B008
    settings: Settings = Depends(get_settings),  # noqa: B008
) -> RefundOut:
    request_id = getattr(request.state, "request_id", "")
    refund = services.create_refund(
        session,
        principal,
        body,
        now=clock(),
        tz_name=settings.timezone,
        request_id=request_id,
    )
    response.headers["Location"] = f"/api/v1/refunds/{refund.refund_id}"
    return refund

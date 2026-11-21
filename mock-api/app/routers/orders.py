from __future__ import annotations

from fastapi import APIRouter, Depends, Path, Query
from sqlalchemy.orm import Session

from app import services
from app.auth import Principal, get_principal, get_settings
from app.clock import Clock, get_clock
from app.config import Settings
from app.db import get_session
from app.schemas import ErrorResponse, OrderDetail, OrderListResponse, OrderStatus

router = APIRouter(
    prefix="/api/v1",
    tags=["orders"],
    dependencies=[Depends(get_principal)],
)


@router.get(
    "/orders/{order_id}",
    operation_id="get_order",
    summary="查询订单详情",
    description=(
        "按订单号查询一笔订单的完整信息：状态、金额、签收时间与已签收天数、商品及类目、"
        "客户等级、售后工单、已有退款。只返回事实数据，不判断是否符合售后制度。"
        "只能查询当前登录人负责区域内的订单；订单不在负责区域返回 403，订单号不存在返回 404。"
    ),
    response_model=OrderDetail,
    response_model_exclude_none=True,
    responses={
        401: {"model": ErrorResponse, "description": "未认证或身份令牌无效"},
        403: {"model": ErrorResponse, "description": "令牌声明无效或无权访问该订单"},
        404: {"model": ErrorResponse, "description": "订单号不存在"},
    },
)
def get_order(
    order_id: str = Path(..., description="订单号，如 A1023"),
    principal: Principal = Depends(get_principal),  # noqa: B008
    session: Session = Depends(get_session),  # noqa: B008
    clock: Clock = Depends(get_clock),  # noqa: B008
    settings: Settings = Depends(get_settings),  # noqa: B008
) -> OrderDetail:
    return services.get_order_detail(
        session,
        principal,
        order_id,
        now=clock(),
        tz_name=settings.timezone,
    )


@router.get(
    "/orders",
    operation_id="list_orders",
    summary="查询订单列表",
    description=(
        "按客户、订单状态、是否有待处理售后工单筛选订单，按下单时间倒序分页返回订单摘要。"
        "结果只包含当前登录人负责区域内的订单，其他区域的订单不会出现在结果中。"
        "需要完整信息时再用订单号查询订单详情。"
    ),
    response_model=OrderListResponse,
    response_model_exclude_none=True,
    responses={
        401: {"model": ErrorResponse, "description": "未认证或身份令牌无效"},
        403: {"model": ErrorResponse, "description": "令牌声明无效"},
    },
)
def list_orders(
    customer_id: str | None = Query(default=None, description="客户编号，如 C0001"),
    status: OrderStatus | None = Query(default=None, description="订单状态"),
    has_open_ticket: bool | None = Query(
        default=None,
        description="true 只返回有待处理售后工单的订单，false 只返回没有的",
    ),
    page: int = Query(default=1, ge=1, description="页码，从 1 开始，默认 1"),
    page_size: int = Query(default=20, ge=1, le=50, description="每页条数，范围 1–50，默认 20"),
    principal: Principal = Depends(get_principal),  # noqa: B008
    session: Session = Depends(get_session),  # noqa: B008
    clock: Clock = Depends(get_clock),  # noqa: B008
    settings: Settings = Depends(get_settings),  # noqa: B008
) -> OrderListResponse:
    return services.list_order_summaries(
        session,
        principal,
        customer_id=customer_id,
        status=status,
        has_open_ticket=has_open_ticket,
        page=page,
        page_size=page_size,
        now=clock(),
        tz_name=settings.timezone,
    )

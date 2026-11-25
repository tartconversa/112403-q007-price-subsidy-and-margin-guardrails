"""文本与 JSON 呈现。未确定部分显式标注，绝不用 0 或空白掩盖。"""
from __future__ import annotations

import json
from decimal import Decimal
from typing import Any, Optional

from .engine import (
    CampaignProjection,
    OrderAttribution,
    PriceConflict,
)
from .models import ConflictStatus, ProjectionStatus

PROJECTION_STATUS_LABEL = {
    ProjectionStatus.OK: "通过",
    ProjectionStatus.BREACH: "突破底线",
    ProjectionStatus.UNDETERMINED: "未确定",
}
CONFLICT_STATUS_LABEL = {
    ConflictStatus.CONFIRMED: "已确认冲突",
    ConflictStatus.PENDING: "待确认（不计违规）",
    ConflictStatus.UNDETERMINED: "未确定（缺少映射）",
}


def money(value: Optional[Decimal]) -> str:
    return "未确定" if value is None else f"{value:.2f}"


def _reasons_block(reasons: list[str], indent: str = "    ") -> str:
    if not reasons:
        return ""
    return "\n".join(f"{indent}- {reason}" for reason in reasons)


def render_order(attribution: OrderAttribution) -> str:
    lines = [
        f"订单 {attribution.order_id} / {attribution.sku} / {attribution.channel}"
        f"（{attribution.day.isoformat() if attribution.day else '日期未知'}）",
        f"  标价 {money(attribution.list_price)}  到手 {money(attribution.paid)}"
        f"  核销优惠合计 {money(attribution.discount_total)}",
        f"  平台补贴(已核实) {money(attribution.platform_subsidy)}  "
        f"品牌承担(已核实) {money(attribution.brand_burden)}  "
        f"未确定优惠 {money(attribution.undetermined_amount)}",
        f"  有效成本 {money(attribution.cost)}"
        f"（版本 {attribution.cost_version or '未确定'}）",
        f"  已确认贡献毛利 {money(attribution.confirmed_margin)}  "
        f"最坏情况毛利 {money(attribution.worst_case_margin)}  "
        f"毛利率 {money(attribution.margin_rate)}",
    ]
    if attribution.pending_settlements:
        lines.append(f"  待核实结算: {', '.join(attribution.pending_settlements)}")
    if attribution.unresolved_kinds:
        lines.append(f"  缺结算的优惠: {', '.join(attribution.unresolved_kinds)}")
    block = _reasons_block(attribution.reasons)
    if block:
        lines.append("  归因说明:")
        lines.append(block)
    return "\n".join(lines)


def render_campaign(projection: CampaignProjection) -> str:
    lines = [
        f"活动 {projection.campaign_id} / {projection.sku} / "
        f"{projection.channel} / {projection.spec}",
        f"  校验状态: {PROJECTION_STATUS_LABEL.get(projection.status, projection.status)}",
        f"  标价 {money(projection.list_price)}  "
        f"有效到手价 {money(projection.effective_paid)}",
        f"  平台补贴 {money(projection.platform_total)}  "
        f"品牌承担 {money(projection.brand_total)}  "
        f"未确定优惠 {money(projection.unknown_total)}",
        f"  品牌净收入 {money(projection.net_brand_revenue)}  "
        f"有效成本 {money(projection.cost)}（{projection.cost_version or '未确定'}）",
        f"  贡献毛利 {money(projection.margin)}  "
        f"最坏情况毛利 {money(projection.worst_case_margin)}  "
        f"底线 {money(projection.margin_floor)}  "
        f"底线差额 {money(projection.floor_gap)}",
        f"  预计量 {projection.expected_units} 件"
        + (
            "" if projection.floor_gap is None
            else f"  预计总影响 {projection.floor_gap * projection.expected_units:.2f}"
        ),
    ]
    block = _reasons_block(projection.reasons)
    if block:
        lines.append("  说明:")
        lines.append(block)
    return "\n".join(lines)


def render_conflict(conflict: PriceConflict) -> str:
    lines = [
        f"{conflict.conflict_id} [{CONFLICT_STATUS_LABEL.get(conflict.status, conflict.status)}]"
        f" 商品 {conflict.sku} 比较方式 {conflict.kind}",
        f"  {conflict.channel_a}（{conflict.ref_a}）价格 {money(conflict.price_a)}"
        + (
            "" if conflict.unit_price_a is None
            else f" 单位价 {conflict.unit_price_a}"
        ),
        f"  {conflict.channel_b}（{conflict.ref_b}）价格 {money(conflict.price_b)}"
        + (
            "" if conflict.unit_price_b is None
            else f" 单位价 {conflict.unit_price_b}"
        ),
        f"  差额 {money(conflict.delta)}",
    ]
    block = _reasons_block(conflict.reasons)
    if block:
        lines.append(block)
    return "\n".join(lines)


def to_json(payload: Any) -> str:
    def default(value: Any) -> Any:
        if isinstance(value, Decimal):
            return str(value)
        if hasattr(value, "isoformat"):
            return value.isoformat()
        if hasattr(value, "__dict__"):
            return value.__dict__
        raise TypeError(type(value).__name__)

    return json.dumps(payload, ensure_ascii=False, indent=2, default=default)

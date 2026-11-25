"""计算引擎：全部为纯函数，不写入任何状态。

核心口径（业务规则的代码化）：
1. 品牌结算记录（state=confirmed）是承担方的唯一权威来源；
   核销记录里的 owner 只是下单时的主张，结算缺失/待定时金额计入“未确定”，
   绝不默认归入品牌让利，也绝不把平台补贴误记为品牌承担。
2. 金额一律 Decimal；无法确认的部分用 None + reasons 表达，不用 0 代替。
3. 套组成本按组件有效成本加总；缺少规格映射时跨规格比较只能输出“未确定”。
4. 毛利 = 品牌净收入（到手价 − 品牌承担）− 有效成本；
   平台补贴不进品牌损益，叠加在同一订单上也不改变品牌承担口径。
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal, localcontext
from typing import Optional

from .models import Bearer, ConflictStatus, Dataset, ProjectionStatus, VerifyState

ZERO = Decimal("0")


@dataclass
class CostResult:
    cost: Optional[Decimal]
    version: Optional[str]
    reasons: list[str] = field(default_factory=list)


def effective_cost(
    dataset: Dataset, sku: str, day: dt.date
) -> CostResult:
    """取 sku 在 day 当天的有效成本版本。区间重叠时取最新启用版本并说明。"""
    covering = [
        version for version in dataset.costs
        if version.sku == sku
        and version.effective_from <= day
        and (version.effective_to is None or day <= version.effective_to)
    ]
    if not covering:
        return CostResult(None, None, [f"sku {sku} 在 {day.isoformat()} 无有效成本版本"])
    covering.sort(key=lambda version: version.effective_from, reverse=True)
    chosen = covering[0]
    reasons: list[str] = []
    if len(covering) > 1:
        others = ", ".join(version.version for version in covering[1:])
        reasons.append(
            f"sku {sku} 成本版本重叠，采用最新 {chosen.version}，忽略 {others}"
        )
    return CostResult(chosen.cost, chosen.version, reasons)


def cost_for_sku_spec(
    dataset: Dataset, sku: str, spec: str, day: dt.date
) -> CostResult:
    """按规格取成本：套组按组件有效成本加总，单品直接取有效版本。"""
    mapping = dataset.spec_maps.get(f"{sku}|{spec}")
    if mapping is None:
        result = effective_cost(dataset, sku, day)
        result.reasons.append(f"缺少 {sku}/{spec} 的规格映射，按单品成本处理")
        return result
    if not mapping.bundle_of:
        return effective_cost(dataset, sku, day)

    total = ZERO
    reasons: list[str] = []
    missing: list[str] = []
    for component, qty in mapping.bundle_of.items():
        component_cost = effective_cost(dataset, component, day)
        if component_cost.cost is None:
            missing.append(component)
            continue
        total += component_cost.cost * qty
        reasons.append(
            f"套组组件 {component} x{qty} @ {component_cost.cost}"
            f"（版本 {component_cost.version}）"
        )
    if missing:
        return CostResult(
            None, None, reasons + [f"套组组件缺少有效成本: {', '.join(missing)}"]
        )
    return CostResult(total, "bundle", reasons)


@dataclass
class OrderAttribution:
    order_id: str
    sku: str
    channel: str
    day: Optional[dt.date]
    list_price: Optional[Decimal]
    paid: Optional[Decimal]
    discount_total: Decimal
    platform_subsidy: Decimal       # 已核实由平台承担
    brand_burden: Decimal           # 已核实由品牌承担
    undetermined_amount: Decimal    # 结算缺失/待定的优惠金额
    reconciliation_gap: Optional[Decimal]  # 标价-到手-优惠核销 对不平的部分
    pending_settlements: list[str]
    unresolved_kinds: list[str]
    cost: Optional[Decimal]
    cost_version: Optional[str]
    confirmed_margin: Optional[Decimal]
    worst_case_margin: Optional[Decimal]
    margin_rate: Optional[Decimal]
    reasons: list[str] = field(default_factory=list)

    @property
    def has_undetermined(self) -> bool:
        return (
            self.undetermined_amount != ZERO
            or (self.reconciliation_gap not in (None, ZERO))
            or self.cost is None
        )


def _settlement_index(
    dataset: Dataset,
) -> dict[tuple[str, str, str], list]:
    index: dict[tuple[str, str, str], list] = {}
    for settlement in dataset.settlements:
        key = (settlement.order_id, settlement.kind, settlement.channel)
        index.setdefault(key, []).append(settlement)
    return index


def attribute_order(
    dataset: Dataset, order_id: str, sku: Optional[str] = None
) -> list[OrderAttribution]:
    """按订单（及订单内 sku）归因每条优惠的实际承担方。"""
    lines = [
        line for line in dataset.discounts
        if line.order_id == order_id and (sku is None or line.sku == sku)
    ]
    if not lines:
        raise KeyError(f"找不到订单 {order_id} 的核销记录")

    settlement_index = _settlement_index(dataset)
    result: list[OrderAttribution] = []
    grouped: dict[str, list] = {}
    for line in lines:
        grouped.setdefault(line.sku, []).append(line)

    for grouped_sku, sku_lines in grouped.items():
        channel = sku_lines[0].channel
        observations = [
            price for price in dataset.prices
            if price.sku == grouped_sku and price.channel == channel
        ]
        observation = max(observations, key=lambda item: item.day) if observations else None

        platform_total = ZERO
        brand_total = ZERO
        undetermined = ZERO
        discount_total = ZERO
        pending: list[str] = []
        unresolved: list[str] = []
        reasons: list[str] = []

        for line in sku_lines:
            discount_total += line.amount
            matches = settlement_index.get((order_id, line.kind, channel), [])
            if not matches:
                undetermined += line.amount
                unresolved.append(line.kind)
                reasons.append(
                    f"优惠 {line.kind} {line.amount} 缺少品牌结算记录，"
                    f"承担方未确定（核销登记 owner={line.owner}，不得直接采信）"
                )
                continue
            for settlement in matches:
                if settlement.state != VerifyState.CONFIRMED:
                    undetermined += settlement.amount
                    pending.append(settlement.settlement_id)
                    reasons.append(
                        f"优惠 {line.kind} 结算 {settlement.settlement_id} "
                        f"状态为 {settlement.state}，周期 {settlement.cycle}，承担方未核实"
                    )
                    continue
                if settlement.bearer == Bearer.PLATFORM:
                    platform_total += settlement.amount
                elif settlement.bearer == Bearer.BRAND:
                    brand_total += settlement.amount
                else:
                    undetermined += settlement.amount
                    reasons.append(
                        f"结算 {settlement.settlement_id} 承担方 {settlement.bearer} 无法识别"
                    )
                if settlement.bearer != line.owner:
                    reasons.append(
                        f"优惠 {line.kind} 核销登记 owner={line.owner} "
                        f"与财务核实承担方 {settlement.bearer} 不一致，以结算为准"
                    )

        list_price = observation.list_price if observation else None
        paid = observation.paid if observation else None
        gap: Optional[Decimal] = None
        if list_price is not None and paid is not None:
            gap = list_price - paid - discount_total
            if gap != ZERO:
                reasons.append(
                    f"标价 {list_price} − 到手 {paid} − 已核销优惠 {discount_total} "
                    f"= {gap}，存在未核销差额，计入未确定"
                )
                if gap > 0:
                    undetermined += gap

        day = observation.day if observation else None
        cost_result = (
            cost_for_sku_spec(dataset, grouped_sku, observation.spec, day)
            if observation else CostResult(None, None, ["缺少渠道价格记录，无法定位成本日期"])
        )
        reasons.extend(cost_result.reasons)

        confirmed_margin: Optional[Decimal] = None
        worst_case: Optional[Decimal] = None
        rate: Optional[Decimal] = None
        if paid is not None and cost_result.cost is not None:
            net_revenue = paid - brand_total
            confirmed_margin = net_revenue - cost_result.cost
            worst_case = (
                paid - brand_total - undetermined - cost_result.cost
            )
            if net_revenue > 0:
                with localcontext() as context:
                    context.prec = 28
                    rate = (confirmed_margin / net_revenue).quantize(
                        Decimal("0.0001")
                    )

        result.append(OrderAttribution(
            order_id=order_id,
            sku=grouped_sku,
            channel=channel,
            day=day,
            list_price=list_price,
            paid=paid,
            discount_total=discount_total,
            platform_subsidy=platform_total,
            brand_burden=brand_total,
            undetermined_amount=undetermined,
            reconciliation_gap=gap,
            pending_settlements=pending,
            unresolved_kinds=unresolved,
            cost=cost_result.cost,
            cost_version=cost_result.version,
            confirmed_margin=confirmed_margin,
            worst_case_margin=worst_case,
            margin_rate=rate,
            reasons=reasons,
        ))
    return result


def all_order_attributions(dataset: Dataset) -> list[OrderAttribution]:
    attributions: list[OrderAttribution] = []
    for order_id in sorted({line.order_id for line in dataset.discounts}):
        attributions.extend(attribute_order(dataset, order_id))
    return attributions


@dataclass
class CampaignProjection:
    campaign_id: str
    sku: str
    channel: str
    spec: str
    list_price: Decimal
    platform_total: Decimal
    brand_total: Decimal
    unknown_total: Decimal
    effective_paid: Decimal
    cost: Optional[Decimal]
    cost_version: Optional[str]
    net_brand_revenue: Optional[Decimal]
    margin: Optional[Decimal]
    worst_case_margin: Optional[Decimal]
    margin_rate: Optional[Decimal]
    margin_floor: Decimal
    floor_gap: Optional[Decimal]       # margin - floor，负数即突破
    expected_units: int
    status: str
    reasons: list[str] = field(default_factory=list)

    @property
    def is_undetermined(self) -> bool:
        return self.status == ProjectionStatus.UNDETERMINED

    @property
    def is_breach(self) -> bool:
        return self.status == ProjectionStatus.BREACH


def project_campaign(
    dataset: Dataset, campaign_id: str, day: Optional[dt.date] = None
) -> CampaignProjection:
    """活动发布前校验：有效到手价、品牌承担额、贡献毛利与底线状态。"""
    campaign = dataset.campaign(campaign_id)
    if campaign is None:
        raise KeyError(f"未知活动 {campaign_id}")
    day = day or campaign.start

    plan_lines = [
        line for line in dataset.campaign_discounts
        if line.campaign_id == campaign_id
    ]
    platform_total = ZERO
    brand_total = ZERO
    unknown_total = ZERO
    reasons: list[str] = []
    for line in plan_lines:
        if line.owner == Bearer.PLATFORM:
            platform_total += line.amount
        elif line.owner == Bearer.BRAND:
            brand_total += line.amount
        else:
            unknown_total += line.amount
            reasons.append(
                f"计划优惠 {line.kind} {line.amount} 承担方未落实，计入未确定，"
                f"不得假设为平台补贴或品牌让利"
            )

    total_discount = platform_total + brand_total + unknown_total
    effective_paid = campaign.list_price - total_discount
    if effective_paid < 0:
        reasons.append(f"优惠总额 {total_discount} 超过标价，到手价为负，请核对报名数据")

    cost_result = cost_for_sku_spec(dataset, campaign.sku, campaign.spec, day)
    reasons.extend(cost_result.reasons)

    net_revenue: Optional[Decimal] = None
    margin: Optional[Decimal] = None
    worst_case: Optional[Decimal] = None
    rate: Optional[Decimal] = None
    floor_gap: Optional[Decimal] = None

    if cost_result.cost is not None:
        net_revenue = effective_paid - brand_total
        margin = net_revenue - cost_result.cost
        worst_case = effective_paid - brand_total - unknown_total - cost_result.cost
        if net_revenue > 0:
            with localcontext() as context:
                context.prec = 28
                rate = (margin / net_revenue).quantize(Decimal("0.0001"))
        floor_gap = margin - campaign.margin_floor

    if unknown_total > 0 or cost_result.cost is None:
        status = ProjectionStatus.UNDETERMINED
    elif margin < campaign.margin_floor:
        status = ProjectionStatus.BREACH
        reasons.append(
            f"贡献毛利 {margin} 低于品牌毛利底线 {campaign.margin_floor}，"
            f"缺口 {campaign.margin_floor - margin}/件"
        )
    else:
        status = ProjectionStatus.OK

    return CampaignProjection(
        campaign_id=campaign_id,
        sku=campaign.sku,
        channel=campaign.channel,
        spec=campaign.spec,
        list_price=campaign.list_price,
        platform_total=platform_total,
        brand_total=brand_total,
        unknown_total=unknown_total,
        effective_paid=effective_paid,
        cost=cost_result.cost,
        cost_version=cost_result.version,
        net_brand_revenue=net_revenue,
        margin=margin,
        worst_case_margin=worst_case,
        margin_rate=rate,
        margin_floor=campaign.margin_floor,
        floor_gap=floor_gap,
        expected_units=campaign.expected_units,
        status=status,
        reasons=reasons,
    )


@dataclass
class PriceConflict:
    conflict_id: str
    sku: str
    kind: str                      # exact / normalized / unmapped
    channel_a: str
    ref_a: str
    price_a: Optional[Decimal]
    channel_b: str
    ref_b: str
    price_b: Optional[Decimal]
    unit_price_a: Optional[Decimal]
    unit_price_b: Optional[Decimal]
    delta: Optional[Decimal]
    status: str
    reasons: list[str] = field(default_factory=list)


@dataclass
class _ConflictItem:
    ref: str
    channel: str
    spec: str
    price: Decimal
    confirmed: bool
    reason: str


def _conflict_items(dataset: Dataset) -> list[_ConflictItem]:
    items: list[_ConflictItem] = []
    for price in dataset.prices:
        items.append(_ConflictItem(
            ref=f"price:{price.sku}:{price.channel}:{price.day.isoformat()}",
            channel=price.channel,
            spec=price.spec,
            price=price.paid,
            confirmed=True,
            reason="渠道价格巡检",
        ))
    for audit in dataset.audits:
        items.append(_ConflictItem(
            ref=f"audit:{audit.audit_id}",
            channel=f"store:{audit.store_id}@{audit.region}",
            spec=audit.spec,
            price=audit.observed_price,
            confirmed=audit.scope_confirmed,
            reason=(
                f"线下活动 {audit.activity_id or '未登记'}，"
                + ("区域范围已确认" if audit.scope_confirmed else "区域范围待区域运营确认")
            ),
        ))
    return items


def find_conflicts(dataset: Dataset) -> list[PriceConflict]:
    """同一商品跨渠道价格冲突。

    - exact：同 sku 同规格直接比较到手/巡检价；
    - normalized：规格不同但映射到同一标准单位，比较单位价格，禁止套组按单品价比；
    - unmapped：缺少规格映射无法归一化，只输出未确定，不下违规结论。
    未确认范围的门店巡检价只能产生 pending，不得计为已确认违规。
    """
    items = _conflict_items(dataset)
    conflicts: list[PriceConflict] = []
    seen: set[frozenset[str]] = set()
    serial = 0

    def add(item_a: _ConflictItem, item_b: _ConflictItem, kind: str,
            unit_a: Decimal | None, unit_b: Decimal | None,
            delta: Decimal | None, status: str, reasons: list[str]) -> None:
        nonlocal serial
        pair = frozenset({item_a.ref, item_b.ref})
        if pair in seen:
            return
        seen.add(pair)
        serial += 1
        conflicts.append(PriceConflict(
            conflict_id=f"C{serial:03d}",
            sku=sku,
            kind=kind,
            channel_a=item_a.channel,
            ref_a=item_a.ref,
            price_a=item_a.price,
            channel_b=item_b.channel,
            ref_b=item_b.ref,
            price_b=item_b.price,
            unit_price_a=unit_a,
            unit_price_b=unit_b,
            delta=delta,
            status=status,
            reasons=reasons + [item_a.reason, item_b.reason],
        ))

    skus: set[str] = set()
    for price in dataset.prices:
        skus.add(price.sku)
    for audit in dataset.audits:
        skus.add(audit.sku)
    for sku in sorted(skus):
        sku_items = [
            item for item in items
            if (item.ref.startswith("price:") and item.ref.split(":")[1] == sku)
            or (
                item.ref.startswith("audit:")
                and dataset.audit(item.ref.split(":", 1)[1]).sku == sku
            )
        ]

        # 第一轮：同规格直接比较
        by_spec: dict[str, list[_ConflictItem]] = {}
        for item in sku_items:
            by_spec.setdefault(item.spec, []).append(item)
        for spec, spec_items in by_spec.items():
            for i, item_a in enumerate(spec_items):
                for item_b in spec_items[i + 1:]:
                    if item_a.channel == item_b.channel:
                        continue
                    delta = item_a.price - item_b.price
                    if delta == 0:
                        continue
                    status = (
                        ConflictStatus.CONFIRMED
                        if item_a.confirmed and item_b.confirmed
                        else ConflictStatus.PENDING
                    )
                    reasons = [f"同规格 {spec} 到手价不一致，差额 {delta}"]
                    add(item_a, item_b, "exact", None, None, delta, status, reasons)

        # 第二轮：跨规格按标准单位归一化
        for i, item_a in enumerate(sku_items):
            for item_b in sku_items[i + 1:]:
                if item_a.spec == item_b.spec or item_a.channel == item_b.channel:
                    continue
                mapping_a = dataset.spec_maps.get(f"{sku}|{item_a.spec}")
                mapping_b = dataset.spec_maps.get(f"{sku}|{item_b.spec}")
                base_reasons = [
                    f"规格不同（{item_a.spec} vs {item_b.spec}），禁止直接按单品价比较"
                ]
                if mapping_a is None or mapping_b is None:
                    add(
                        item_a, item_b, "unmapped", None, None, None,
                        ConflictStatus.UNDETERMINED,
                        base_reasons + ["缺少规格映射，无法归一化，结论未确定"],
                    )
                    continue
                if (
                    not mapping_a.canonical_unit
                    or mapping_a.canonical_unit != mapping_b.canonical_unit
                    or mapping_a.units is None
                    or mapping_b.units is None
                ):
                    add(
                        item_a, item_b, "unmapped", None, None, None,
                        ConflictStatus.UNDETERMINED,
                        base_reasons + ["标准单位或容量缺失，无法归一化，结论未确定"],
                    )
                    continue
                with localcontext() as context:
                    context.prec = 28
                    unit_a = (item_a.price / mapping_a.units).quantize(
                        Decimal("0.0001")
                    )
                    unit_b = (item_b.price / mapping_b.units).quantize(
                        Decimal("0.0001")
                    )
                delta = (unit_a - unit_b).quantize(Decimal("0.0001"))
                if delta == 0:
                    continue
                status = (
                    ConflictStatus.CONFIRMED
                    if item_a.confirmed and item_b.confirmed
                    else ConflictStatus.PENDING
                )
                add(
                    item_a, item_b, "normalized", unit_a, unit_b, delta, status,
                    base_reasons + [
                        f"按 {mapping_a.canonical_unit} 归一后单位价 {unit_a} vs {unit_b}"
                    ],
                )
    return conflicts

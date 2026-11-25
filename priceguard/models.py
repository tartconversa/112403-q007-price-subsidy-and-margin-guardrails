"""领域模型。金额一律使用 Decimal，空单元格解析为 None。"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional


class Bearer:
    """优惠承担方。unknown 表示报名时未确定，不能直接归入品牌。"""

    PLATFORM = "platform"
    BRAND = "brand"
    UNKNOWN = "unknown"


class VerifyState:
    CONFIRMED = "confirmed"
    PENDING = "pending"


class ProjectionStatus:
    OK = "ok"
    BREACH = "breach"            # 已可确认地突破毛利底线
    UNDETERMINED = "undetermined"  # 结算/成本/规格缺失，无法形成结论


class ConflictStatus:
    CONFIRMED = "confirmed"   # 事实已确认的冲突
    PENDING = "pending"       # 待区域运营确认的线下价，不得计为违规
    UNDETERMINED = "undetermined"  # 缺少规格映射等，无法判断


@dataclass(frozen=True)
class PriceObservation:
    sku: str
    channel: str
    spec: str
    list_price: Decimal
    paid: Decimal
    day: dt.date


@dataclass(frozen=True)
class DiscountLine:
    """订单优惠核销记录，owner 是下单时登记的*主张*承担方。"""

    order_id: str
    sku: str
    channel: str
    kind: str
    amount: Decimal
    owner: str
    state: str


@dataclass(frozen=True)
class CostVersion:
    sku: str
    version: str
    cost: Decimal
    effective_from: dt.date
    effective_to: Optional[dt.date]


@dataclass(frozen=True)
class Settlement:
    """财务核实的品牌结算记录，是承担方与结算周期的权威来源。"""

    settlement_id: str
    order_id: str
    sku: str
    channel: str
    kind: str
    amount: Decimal
    bearer: str
    cycle: str
    state: str


@dataclass(frozen=True)
class Campaign:
    """平台/渠道活动报名。"""

    campaign_id: str
    sku: str
    channel: str
    spec: str
    list_price: Decimal
    start: dt.date
    end: dt.date
    margin_floor: Decimal
    expected_units: int
    owner: str


@dataclass(frozen=True)
class CampaignDiscount:
    """活动计划优惠行；owner=unknown 表示承担方尚未落实。"""

    campaign_id: str
    kind: str
    amount: Decimal
    owner: str


@dataclass(frozen=True)
class StoreAudit:
    audit_id: str
    sku: str
    spec: str
    region: str
    store_id: str
    observed_price: Decimal
    activity_id: str
    day: dt.date
    scope_confirmed: bool
    confirmed_by: Optional[str]


@dataclass(frozen=True)
class SpecMapping:
    """规格映射。bundle_of 非空表示套组，值为组件 sku -> 件数。"""

    sku: str
    spec: str
    canonical_unit: Optional[str]
    units: Optional[Decimal]
    bundle_of: dict[str, int] = field(default_factory=dict)


@dataclass
class Dataset:
    prices: list[PriceObservation] = field(default_factory=list)
    discounts: list[DiscountLine] = field(default_factory=list)
    costs: list[CostVersion] = field(default_factory=list)
    settlements: list[Settlement] = field(default_factory=list)
    campaigns: list[Campaign] = field(default_factory=list)
    campaign_discounts: list[CampaignDiscount] = field(default_factory=list)
    audits: list[StoreAudit] = field(default_factory=list)
    spec_maps: dict[str, SpecMapping] = field(default_factory=dict)

    def campaign(self, campaign_id: str) -> Optional[Campaign]:
        for campaign in self.campaigns:
            if campaign.campaign_id == campaign_id:
                return campaign
        return None

    def audit(self, audit_id: str) -> Optional[StoreAudit]:
        for audit in self.audits:
            if audit.audit_id == audit_id:
                return audit
        return None

    def settlement(self, settlement_id: str) -> Optional[Settlement]:
        for settlement in self.settlements:
            if settlement.settlement_id == settlement_id:
                return settlement
        return None

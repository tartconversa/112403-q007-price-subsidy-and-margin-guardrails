"""CSV 输入装载。

所有输入来自外部数据目录，属于不可信数据：
- 仅使用标准库 csv（strict 模式），空单元格转为 None；
- 必填列缺失或出现未知列时抛 LoaderError，避免静默错列；
- 金额解析为 Decimal（按字符串解析，禁止 float 中转）；
- 不执行输入目录中的任何代码。
"""
from __future__ import annotations

import csv
import datetime as dt
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .models import (
    Campaign,
    CampaignDiscount,
    CostVersion,
    Dataset,
    DiscountLine,
    PriceObservation,
    Settlement,
    SpecMapping,
    StoreAudit,
)

REQUIRED_HEADERS: dict[str, list[str]] = {
    "prices": ["sku", "channel", "spec", "list_price", "paid", "day"],
    "discounts": ["order_id", "sku", "channel", "kind", "amount", "owner", "state"],
    "costs": ["sku", "version", "cost", "from", "to"],
    "settlements": [
        "settlement_id", "order_id", "sku", "channel", "kind",
        "amount", "bearer", "cycle", "state",
    ],
    "campaigns": [
        "campaign_id", "sku", "channel", "spec", "list_price",
        "start", "end", "margin_floor", "expected_units", "owner",
    ],
    "campaign_discounts": ["campaign_id", "kind", "amount", "owner"],
    "audits": [
        "audit_id", "sku", "spec", "region", "store_id", "observed_price",
        "activity_id", "day", "scope_confirmed", "confirmed_by",
    ],
    "spec_map": ["sku", "spec", "canonical_unit", "units", "bundle_of"],
}


class LoaderError(ValueError):
    """输入结构或取值不合法。"""


def _money(value: str | None, context: str) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        amount = Decimal(value)
    except InvalidOperation as exc:
        raise LoaderError(f"{context}: 金额无法解析 {value!r}") from exc
    if amount < 0:
        raise LoaderError(f"{context}: 金额不允许为负 {value!r}")
    return amount


def _day(value: str | None, context: str) -> dt.date | None:
    if value is None or value == "":
        return None
    try:
        return dt.date.fromisoformat(value)
    except ValueError as exc:
        raise LoaderError(f"{context}: 日期无法解析 {value!r}") from exc


def _text(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


def _required(value: str | None, context: str) -> str:
    value = _text(value)
    if value is None:
        raise LoaderError(f"{context}: 必填单元格为空")
    return value


def _rows(path: Path) -> list[dict[str, str | None]]:
    if not path.exists():
        raise LoaderError(f"输入文件不存在: {path}")
    with path.open(encoding="utf-8", newline="") as source:
        reader = csv.DictReader(source, strict=True)
        expected = REQUIRED_HEADERS[path.stem]
        if reader.fieldnames != expected:
            raise LoaderError(
                f"{path.name}: 列头不匹配，期望 {expected}，实际 {reader.fieldnames}"
            )
        rows: list[dict[str, str | None]] = []
        for line, raw in enumerate(reader, start=2):
            if None in raw:
                raise LoaderError(f"{path.name}:{line} 存在多余列")
            rows.append({key: (_text(cell) if not isinstance(cell, list) else cell)
                         for key, cell in raw.items()})
        return rows


def load_dataset(data_dir: str | Path) -> Dataset:
    """从目录装载全部输入；目录缺失时抛 LoaderError。"""
    data_dir = Path(data_dir)
    if not data_dir.is_dir():
        raise LoaderError(f"数据目录不存在: {data_dir}")
    dataset = Dataset()

    for row in _rows(data_dir / "prices.csv"):
        context = "prices"
        dataset.prices.append(PriceObservation(
            sku=_required(row["sku"], context),
            channel=_required(row["channel"], context),
            spec=_required(row["spec"], context),
            list_price=_money(row["list_price"], context) or Decimal(),
            paid=_money(row["paid"], context) or Decimal(),
            day=_day(row["day"], context) or dt.date.min,
        ))

    for row in _rows(data_dir / "discounts.csv"):
        context = "discounts"
        dataset.discounts.append(DiscountLine(
            order_id=_required(row["order_id"], context),
            sku=_required(row["sku"], context),
            channel=_required(row["channel"], context),
            kind=_required(row["kind"], context),
            amount=_money(row["amount"], context) or Decimal(),
            owner=_required(row["owner"], context),
            state=_required(row["state"], context),
        ))

    for row in _rows(data_dir / "costs.csv"):
        context = "costs"
        dataset.costs.append(CostVersion(
            sku=_required(row["sku"], context),
            version=_required(row["version"], context),
            cost=_money(row["cost"], context) or Decimal(),
            effective_from=_day(row["from"], context) or dt.date.min,
            effective_to=_day(row["to"], context),
        ))

    for row in _rows(data_dir / "settlements.csv"):
        context = "settlements"
        dataset.settlements.append(Settlement(
            settlement_id=_required(row["settlement_id"], context),
            order_id=_required(row["order_id"], context),
            sku=_required(row["sku"], context),
            channel=_required(row["channel"], context),
            kind=_required(row["kind"], context),
            amount=_money(row["amount"], context) or Decimal(),
            bearer=_required(row["bearer"], context),
            cycle=_required(row["cycle"], context),
            state=_required(row["state"], context),
        ))

    for row in _rows(data_dir / "campaigns.csv"):
        context = "campaigns"
        dataset.campaigns.append(Campaign(
            campaign_id=_required(row["campaign_id"], context),
            sku=_required(row["sku"], context),
            channel=_required(row["channel"], context),
            spec=_required(row["spec"], context),
            list_price=_money(row["list_price"], context) or Decimal(),
            start=_day(row["start"], context) or dt.date.min,
            end=_day(row["end"], context) or dt.date.max,
            margin_floor=_money(row["margin_floor"], context) or Decimal(),
            expected_units=int(_required(row["expected_units"], context)),
            owner=_required(row["owner"], context),
        ))

    for row in _rows(data_dir / "campaign_discounts.csv"):
        context = "campaign_discounts"
        dataset.campaign_discounts.append(CampaignDiscount(
            campaign_id=_required(row["campaign_id"], context),
            kind=_required(row["kind"], context),
            amount=_money(row["amount"], context) or Decimal(),
            owner=_required(row["owner"], context),
        ))

    for row in _rows(data_dir / "audits.csv"):
        context = "audits"
        confirmed = _required(row["scope_confirmed"], context).lower()
        if confirmed not in {"true", "false"}:
            raise LoaderError(f"{context}: scope_confirmed 必须是 true/false")
        dataset.audits.append(StoreAudit(
            audit_id=_required(row["audit_id"], context),
            sku=_required(row["sku"], context),
            spec=_required(row["spec"], context),
            region=_required(row["region"], context),
            store_id=_required(row["store_id"], context),
            observed_price=_money(row["observed_price"], context) or Decimal(),
            activity_id=_text(row["activity_id"]) or "",
            day=_day(row["day"], context) or dt.date.min,
            scope_confirmed=confirmed == "true",
            confirmed_by=_text(row["confirmed_by"]),
        ))

    for row in _rows(data_dir / "spec_map.csv"):
        context = "spec_map"
        sku = _required(row["sku"], context)
        spec = _required(row["spec"], context)
        units = _money(row["units"], context)
        bundle_of: dict[str, int] = {}
        raw_bundle = _text(row["bundle_of"])
        if raw_bundle:
            for part in raw_bundle.split(";"):
                if ":" not in part:
                    raise LoaderError(f"{context}: bundle_of 片段格式错误 {part!r}")
                component, qty = part.split(":", 1)
                component, qty = component.strip(), qty.strip()
                if not component:
                    raise LoaderError(f"{context}: bundle_of 组件为空")
                try:
                    bundle_of[component] = int(qty)
                except ValueError as exc:
                    raise LoaderError(f"{context}: bundle_of 件数不是整数 {qty!r}") from exc
                if bundle_of[component] <= 0:
                    raise LoaderError(f"{context}: bundle_of 件数必须为正")
        if not bundle_of and units is None:
            raise LoaderError(f"{context}: 单品必须提供 units")
        mapping = SpecMapping(
            sku=sku,
            spec=spec,
            canonical_unit=_text(row["canonical_unit"]),
            units=units,
            bundle_of=bundle_of,
        )
        key = f"{sku}|{spec}"
        if key in dataset.spec_maps:
            raise LoaderError(f"{context}: 规格映射重复 {key}")
        dataset.spec_maps[key] = mapping

    _validate_references(dataset)
    return dataset


def _validate_references(dataset: Dataset) -> None:
    """外键式引用检查，防止孤儿记录在计算中被静默忽略。"""
    price_keys = {(p.sku, p.channel, p.spec) for p in dataset.prices}
    known_skus = {p.sku for p in dataset.prices} | {c.sku for c in dataset.costs}
    for line in dataset.discounts:
        if line.sku not in known_skus:
            raise LoaderError(f"discounts: 未知 sku {line.sku}")
    for settlement in dataset.settlements:
        if settlement.sku not in known_skus:
            raise LoaderError(f"settlements: 未知 sku {settlement.sku}")
    campaign_ids = {c.campaign_id for c in dataset.campaigns}
    for discount in dataset.campaign_discounts:
        if discount.campaign_id not in campaign_ids:
            raise LoaderError(
                f"campaign_discounts: 未知活动 {discount.campaign_id}"
            )
    for campaign in dataset.campaigns:
        if (campaign.sku, campaign.channel, campaign.spec) not in price_keys:
            raise LoaderError(
                f"campaigns: 活动 {campaign.campaign_id} 找不到对应渠道价格记录"
            )
    for audit in dataset.audits:
        if audit.sku not in known_skus:
            raise LoaderError(f"audits: 未知 sku {audit.sku}")
    for mapping in dataset.spec_maps.values():
        for component in mapping.bundle_of:
            if component not in known_skus:
                raise LoaderError(
                    f"spec_map: 套组 {mapping.sku} 引用未知组件 {component}"
                )

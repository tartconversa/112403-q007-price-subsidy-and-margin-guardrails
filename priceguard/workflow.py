"""审批工作流：提交、审批、上线、财务/区域确认、成本更正与重验。

规则要点：
- 未上线活动在成本/结算更正后重新校验，结果以独立重验事件追加；
- 已上线（已承诺）活动不回滚审批，只记录偏离底线影响与处理负责人；
- 未核实的区域价不产生违规，确认动作由区域运营显式完成；
- 事件日志只追加，审批历史完整保留。
"""
from __future__ import annotations

import copy
import datetime as dt
import json
from decimal import Decimal
from typing import Any, Optional

from . import engine
from .models import (
    Bearer,
    Campaign,
    CostVersion,
    Dataset,
    ProjectionStatus,
    Settlement,
    StoreAudit,
    VerifyState,
)
from .store import CampaignStage, EventStore, EventType, StoreError


class WorkflowError(ValueError):
    pass


# ---------------------------------------------------------------- 快照/指纹

def projection_snapshot(projection: engine.CampaignProjection) -> dict[str, Any]:
    """审批时点的计算快照，随事件永久保留，事后可核对当时依据。"""
    def money(value: Decimal | None) -> str | None:
        return None if value is None else str(value)

    return {
        "campaign_id": projection.campaign_id,
        "sku": projection.sku,
        "channel": projection.channel,
        "spec": projection.spec,
        "list_price": money(projection.list_price),
        "platform_subsidy": money(projection.platform_total),
        "brand_burden": money(projection.brand_total),
        "undetermined_discount": money(projection.unknown_total),
        "effective_paid": money(projection.effective_paid),
        "cost": money(projection.cost),
        "cost_version": projection.cost_version,
        "net_brand_revenue": money(projection.net_brand_revenue),
        "margin": money(projection.margin),
        "worst_case_margin": money(projection.worst_case_margin),
        "margin_rate": None if projection.margin_rate is None
        else str(projection.margin_rate),
        "margin_floor": money(projection.margin_floor),
        "floor_gap": money(projection.floor_gap),
        "expected_units": projection.expected_units,
        "status": projection.status,
        "reasons": list(projection.reasons),
    }


def _fingerprint(snapshot: dict[str, Any]) -> str:
    """输入口径指纹：只包含与计算口径相关的字段，审批人/备注不影响。"""
    keys = [
        "list_price", "platform_subsidy", "brand_burden",
        "undetermined_discount", "cost", "margin_floor", "expected_units",
        "spec", "sku", "channel",
    ]
    canonical = json.dumps(
        {key: snapshot[key] for key in keys},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    return f"sha256:{_sha256_hex(canonical)}"


def _sha256_hex(text: str) -> str:
    import hashlib
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def cost_signature(dataset: Dataset, campaign: Campaign) -> str:
    """活动相关成本口径签名：套组组件成本也纳入，成本更正必然改变签名。"""
    mapping = dataset.spec_maps.get(f"{campaign.sku}|{campaign.spec}")
    skus = list(mapping.bundle_of) if mapping and mapping.bundle_of else [campaign.sku]
    parts = []
    for sku in skus:
        versions = sorted(
            (version for version in dataset.costs if version.sku == sku),
            key=lambda version: (version.effective_from, version.version),
        )
        for version in versions:
            parts.append({
                "sku": sku,
                "version": version.version,
                "cost": str(version.cost),
                "from": version.effective_from.isoformat(),
                "to": None if version.effective_to is None
                else version.effective_to.isoformat(),
            })
    canonical = json.dumps(parts, ensure_ascii=False, sort_keys=True)
    return f"sha256:{_sha256_hex(canonical)}"


# ---------------------------------------------------------------- 状态应用

def apply_state(dataset: Dataset, state) -> Dataset:
    """把事件日志里的确认/更正叠加到只读输入之上，返回新数据集，不改原对象。"""
    effective = copy.deepcopy(dataset)

    # 财务确认/补充的结算记录：以 settlement_id 为准整体替换，否则新增
    for override in state.settlement_overrides.values():
        settlement = Settlement(
            settlement_id=override["settlement_id"],
            order_id=override["order_id"],
            sku=override["sku"],
            channel=override["channel"],
            kind=override["kind"],
            amount=Decimal(str(override["amount"])),
            bearer=override["bearer"],
            cycle=override["cycle"],
            state=override["state"],
        )
        effective.settlements = [
            item for item in effective.settlements
            if item.settlement_id != settlement.settlement_id
        ]
        effective.settlements.append(settlement)

    # 区域运营确认的线下活动范围
    for override in state.audit_confirmations.values():
        effective.audits = [
            StoreAudit(
                audit_id=item.audit_id,
                sku=item.sku,
                spec=item.spec,
                region=item.region,
                store_id=item.store_id,
                observed_price=item.observed_price,
                activity_id=item.activity_id,
                day=item.day,
                scope_confirmed=True,
                confirmed_by=override.get("confirmed_by"),
            ) if item.audit_id == override["audit_id"] else item
            for item in effective.audits
        ]

    # 成本更正：同 sku+version 整体替换，否则作为新版本追加
    for correction in state.cost_corrections:
        new_version = CostVersion(
            sku=correction["sku"],
            version=correction["version"],
            cost=Decimal(str(correction["cost"])),
            effective_from=dt.date.fromisoformat(correction["from"]),
            effective_to=(
                None if correction.get("to") in (None, "")
                else dt.date.fromisoformat(correction["to"])
            ),
        )
        effective.costs = [
            item for item in effective.costs
            if not (item.sku == new_version.sku and item.version == new_version.version)
        ]
        effective.costs.append(new_version)

    return effective


# ---------------------------------------------------------------- 工作流动作

class Workflow:
    def __init__(self, dataset: Dataset, store: EventStore):
        self.base_dataset = dataset
        self.store = store

    def _effective(self) -> tuple[Dataset, Any]:
        state = self.store.fold()
        return apply_state(self.base_dataset, state), state

    def _project(self, dataset: Dataset, campaign_id: str):
        campaign = dataset.campaign(campaign_id)
        if campaign is None:
            raise WorkflowError(f"未知活动 {campaign_id}")
        return campaign, engine.project_campaign(dataset, campaign_id, campaign.start)

    def refresh(self) -> list[dict[str, Any]]:
        """按当前输入与事件日志重算全部活动，追加必要的重验/偏差事件。"""
        dataset, state = self._effective()
        events: list[dict[str, Any]] = []
        for campaign in dataset.campaigns:
            record = state.records.get(campaign.campaign_id)
            if record is None or record.stage == CampaignStage.DRAFT:
                continue
            _, projection = self._project(dataset, campaign.campaign_id)
            snapshot = projection_snapshot(projection)
            fingerprint = _fingerprint(snapshot)
            signature = cost_signature(dataset, campaign)

            if record.fingerprint == fingerprint and record.cost_signature == signature:
                continue

            payload = {
                "campaign_id": campaign.campaign_id,
                "revision": record.revision,
                "stage": record.stage,
                "old_status": record.last_snapshot.get("status"),
                "new_status": projection.status,
                "fingerprint": fingerprint,
                "cost_signature": signature,
                "snapshot": snapshot,
                "reasons": snapshot["reasons"],
            }
            event = self.store.append(EventType.CAMPAIGN_REVALIDATED, payload)
            events.append(event)

            if record.stage == CampaignStage.LAUNCHED:
                # 已承诺活动：保留原审批，记录偏离底线影响与负责人
                if (
                    projection.status == ProjectionStatus.BREACH
                    or projection.status == ProjectionStatus.UNDETERMINED
                ):
                    impact = None
                    if projection.floor_gap is not None:
                        impact = str(
                            projection.floor_gap * projection.expected_units
                        )
                    deviation_event = self.store.append(
                        EventType.CAMPAIGN_DEVIATION_LOGGED,
                        {
                            "campaign_id": campaign.campaign_id,
                            "revision": record.revision,
                            "old_status": record.last_snapshot.get("status"),
                            "new_status": projection.status,
                            "margin": snapshot["margin"],
                            "worst_case_margin": snapshot["worst_case_margin"],
                            "floor": snapshot["margin_floor"],
                            "gap": snapshot["floor_gap"],
                            "expected_units": projection.expected_units,
                            "impact_amount": impact,
                            "owner": campaign.owner,
                            "note": (
                                "活动已上线并对外承诺，不回滚审批；"
                                "按更正后口径重验出现偏离，须由负责人处理"
                            ),
                        },
                    )
                    events.append(deviation_event)
        return events

    def submit(self, campaign_id: str, approver: str) -> dict[str, Any]:
        dataset, state = self._effective()
        campaign, projection = self._project(dataset, campaign_id)
        record = state.record(campaign_id)
        if record.stage not in (CampaignStage.DRAFT, CampaignStage.REJECTED):
            raise WorkflowError(
                f"活动 {campaign_id} 当前阶段 {record.stage}，不可重复提交"
            )
        snapshot = projection_snapshot(projection)
        return self.store.append(EventType.CAMPAIGN_SUBMITTED, {
            "campaign_id": campaign_id,
            "approver": approver,
            "fingerprint": _fingerprint(snapshot),
            "cost_signature": cost_signature(dataset, campaign),
            "snapshot": snapshot,
        })

    def approve(self, campaign_id: str, approver: str,
                force: bool = False, note: str = "") -> dict[str, Any]:
        dataset, state = self._effective()
        record = state.records.get(campaign_id)
        if record is None or record.stage != CampaignStage.SUBMITTED:
            raise WorkflowError(f"活动 {campaign_id} 未处于待审批阶段")
        _, projection = self._project(dataset, campaign_id)
        snapshot = projection_snapshot(projection)
        if projection.status != ProjectionStatus.OK and not force:
            raise WorkflowError(
                f"活动 {campaign_id} 校验状态为 {projection.status}，"
                "未确定或突破底线的活动不得直接批准（如需特批请显式 force 并记录原因）"
            )
        if force and projection.status != ProjectionStatus.OK:
            note = (note + " | ").strip(" |")
            note += f"特批：放行状态 {projection.status}，审批人承担例外责任"
        return self.store.append(EventType.CAMPAIGN_APPROVED, {
            "campaign_id": campaign_id,
            "approver": approver,
            "forced": force and projection.status != ProjectionStatus.OK,
            "fingerprint": _fingerprint(snapshot),
            "cost_signature": record.cost_signature,
            "snapshot": snapshot,
            "note": note,
        })

    def reject(self, campaign_id: str, approver: str, reason: str) -> dict[str, Any]:
        state = self.store.fold()
        record = state.records.get(campaign_id)
        if record is None or record.stage != CampaignStage.SUBMITTED:
            raise WorkflowError(f"活动 {campaign_id} 未处于待审批阶段")
        if not reason:
            raise WorkflowError("驳回必须填写原因")
        return self.store.append(EventType.CAMPAIGN_REJECTED, {
            "campaign_id": campaign_id,
            "approver": approver,
            "reason": reason,
        })

    def launch(self, campaign_id: str, operator: str) -> dict[str, Any]:
        dataset, state = self._effective()
        record = state.records.get(campaign_id)
        if record is None or record.stage != CampaignStage.APPROVED:
            raise WorkflowError(f"活动 {campaign_id} 未批准，不能上线")
        _, projection = self._project(dataset, campaign_id)
        snapshot = projection_snapshot(projection)
        if projection.status != ProjectionStatus.OK:
            raise WorkflowError(
                f"活动 {campaign_id} 当前口径状态 {projection.status}，"
                "批准后输入发生变化，请先查看重验结果再决定是否重新审批"
            )
        return self.store.append(EventType.CAMPAIGN_LAUNCHED, {
            "campaign_id": campaign_id,
            "operator": operator,
            "fingerprint": _fingerprint(snapshot),
            "cost_signature": record.cost_signature,
            "snapshot": snapshot,
        })

    def confirm_settlement(self, settlement_id: str, finance_user: str,
                           bearer: str, cycle: str,
                           state_value: str = VerifyState.CONFIRMED,
                           amount: Optional[str] = None,
                           order_id: Optional[str] = None,
                           sku: Optional[str] = None,
                           channel: Optional[str] = None,
                           kind: Optional[str] = None) -> dict[str, Any]:
        """财务核实承担方与结算周期；可补录缺失结算或更正待核实记录。"""
        if bearer not in (Bearer.PLATFORM, Bearer.BRAND):
            raise WorkflowError("承担方只能是 platform 或 brand")
        if state_value not in (VerifyState.CONFIRMED, VerifyState.PENDING):
            raise WorkflowError("结算状态只能是 confirmed 或 pending")

        existing = self.base_dataset.settlement(settlement_id)
        if existing is None:
            missing = [
                name for name, value in (
                    ("order_id", order_id), ("sku", sku),
                    ("channel", channel), ("kind", kind), ("amount", amount),
                ) if value is None
            ]
            if missing:
                raise WorkflowError(f"补录结算缺少字段: {', '.join(missing)}")
            payload_amount = str(amount)
            payload = {
                "settlement_id": settlement_id, "order_id": order_id, "sku": sku,
                "channel": channel, "kind": kind, "amount": payload_amount,
                "bearer": bearer, "cycle": cycle, "state": state_value,
                "finance_user": finance_user, "mode": "created",
            }
        else:
            payload = {
                "settlement_id": settlement_id,
                "order_id": existing.order_id,
                "sku": existing.sku,
                "channel": existing.channel,
                "kind": existing.kind,
                "amount": str(existing.amount if amount is None else amount),
                "bearer": bearer,
                "cycle": cycle,
                "state": state_value,
                "finance_user": finance_user,
                "mode": "updated",
            }
        return self.store.append(EventType.SETTLEMENT_CONFIRMED, payload)

    def confirm_audit_scope(self, audit_id: str, regional_user: str,
                            note: str = "") -> dict[str, Any]:
        """区域运营确认线下活动范围；确认前巡检价只能是 pending。"""
        if self.base_dataset.audit(audit_id) is None:
            raise WorkflowError(f"未知门店巡检记录 {audit_id}")
        return self.store.append(EventType.AUDIT_SCOPE_CONFIRMED, {
            "audit_id": audit_id,
            "confirmed_by": regional_user,
            "note": note,
        })

    def correct_cost(self, sku: str, version: str, cost: str,
                     effective_from: str, effective_to: Optional[str],
                     operator: str, reason: str) -> dict[str, Any]:
        """商品团队更正有效成本版本（新版本或修订既有版本），随后自动重验。"""
        if not reason:
            raise WorkflowError("成本更正必须填写原因")
        try:
            value = Decimal(cost)
        except Exception as exc:
            raise WorkflowError("成本金额无法解析") from exc
        if value < 0:
            raise WorkflowError("成本不允许为负")
        dt.date.fromisoformat(effective_from)
        if effective_to:
            dt.date.fromisoformat(effective_to)
        event = self.store.append(EventType.COST_CORRECTED, {
            "sku": sku,
            "version": version,
            "cost": str(value),
            "from": effective_from,
            "to": effective_to,
            "operator": operator,
            "reason": reason,
        })
        self.refresh()
        return event

    def resolve_deviation(self, campaign_id: str, deviation_event_id: str,
                          owner: str, note: str) -> dict[str, Any]:
        if not note:
            raise WorkflowError("偏差处理必须记录处理说明")
        state = self.store.fold()
        record = state.records.get(campaign_id)
        if record is None:
            raise WorkflowError(f"未知活动 {campaign_id}")
        if not any(
            deviation.event_id == deviation_event_id and not deviation.resolved
            for deviation in record.deviations
        ):
            raise WorkflowError("找不到该活动待处理的偏差事件")
        return self.store.append(EventType.CAMPAIGN_DEVIATION_RESOLVED, {
            "campaign_id": campaign_id,
            "deviation_event_id": deviation_event_id,
            "owner": owner,
            "note": note,
        })

    def revise_campaign(self, campaign_id: str, new_revision: int,
                        operator: str, change_note: str,
                        snapshot: dict[str, Any]) -> dict[str, Any]:
        """活动方案改版登记（输入文件改版后调用），旧审批保留为历史。"""
        state = self.store.fold()
        record = state.records.get(campaign_id)
        if record is None:
            raise WorkflowError(f"活动 {campaign_id} 尚无审批历史，直接提交即可")
        if new_revision <= record.revision:
            raise WorkflowError("新版本号必须大于当前版本，不能覆盖旧版本")
        return self.store.append(EventType.CAMPAIGN_REVISED, {
            "campaign_id": campaign_id,
            "old_revision": record.revision,
            "new_revision": new_revision,
            "operator": operator,
            "change_note": change_note,
            "fingerprint": _fingerprint(snapshot),
            "snapshot": snapshot,
        })

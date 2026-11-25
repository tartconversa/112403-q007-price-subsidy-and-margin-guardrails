"""只追加事件日志与状态折叠。

设计约束：
- 所有状态变化只通过 append(event) 追加，events.jsonl 永不就地改写；
- 价格/审批历史无法通过覆盖旧值隐藏，每次审批与重验都留下独立事件；
- 状态由事件折叠得到；同一事件 ID 重复追加会被拒绝。
"""
from __future__ import annotations

import datetime as dt
import json
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional


class StoreError(ValueError):
    pass


class EventType:
    CAMPAIGN_SUBMITTED = "campaign_submitted"
    CAMPAIGN_APPROVED = "campaign_approved"
    CAMPAIGN_REJECTED = "campaign_rejected"
    CAMPAIGN_LAUNCHED = "campaign_launched"
    CAMPAIGN_REVALIDATED = "campaign_revalidated"
    CAMPAIGN_DEVIATION_LOGGED = "campaign_deviation_logged"
    CAMPAIGN_DEVIATION_RESOLVED = "campaign_deviation_resolved"
    CAMPAIGN_REVISED = "campaign_revised"
    SETTLEMENT_CONFIRMED = "settlement_confirmed"
    AUDIT_SCOPE_CONFIRMED = "audit_scope_confirmed"
    COST_CORRECTED = "cost_corrected"


class CampaignStage:
    DRAFT = "draft"
    SUBMITTED = "submitted"
    APPROVED = "approved"
    REJECTED = "rejected"
    LAUNCHED = "launched"


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dt.date):
        return value.isoformat()
    raise TypeError(f"无法序列化 {type(value)!r}")


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


@dataclass
class Deviation:
    revision: int
    event_id: str
    recorded_at: str
    old_status: str
    new_status: str
    margin: Optional[str]
    floor: Optional[str]
    gap: Optional[str]
    impact_amount: Optional[str]
    owner: Optional[str]
    note: str
    resolved: bool = False
    resolution_note: Optional[str] = None
    resolved_at: Optional[str] = None


@dataclass
class CampaignRecord:
    campaign_id: str
    stage: str = CampaignStage.DRAFT
    revision: int = 0
    fingerprint: Optional[str] = None
    last_snapshot: dict[str, Any] = field(default_factory=dict)
    last_revalidation_event: Optional[str] = None
    deviations: list[Deviation] = field(default_factory=list)
    cost_signature: Optional[str] = None


@dataclass
class CampaignState:
    records: dict[str, CampaignRecord] = field(default_factory=dict)
    settlement_overrides: dict[str, dict[str, Any]] = field(default_factory=dict)
    audit_confirmations: dict[str, dict[str, Any]] = field(default_factory=dict)
    cost_corrections: list[dict[str, Any]] = field(default_factory=list)

    def record(self, campaign_id: str) -> CampaignRecord:
        if campaign_id not in self.records:
            self.records[campaign_id] = CampaignRecord(campaign_id)
        return self.records[campaign_id]


class EventStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, event_type: str, payload: dict[str, Any],
               event_id: Optional[str] = None) -> dict[str, Any]:
        event_id = event_id or uuid.uuid4().hex
        event = {
            "event_id": event_id,
            "type": event_type,
            "at": now_iso(),
            "payload": payload,
        }
        existing = self._raw_lines()
        if any(line.get("event_id") == event_id for line in existing):
            raise StoreError(f"事件 {event_id} 已存在，事件日志不允许重复追加")
        with self.path.open("a", encoding="utf-8") as sink:
            sink.write(json.dumps(event, ensure_ascii=False, default=_json_default))
            sink.write("\n")
        return event

    def events(self) -> list[dict[str, Any]]:
        return self._raw_lines()

    def _raw_lines(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        lines: list[dict[str, Any]] = []
        with self.path.open(encoding="utf-8") as source:
            for number, line in enumerate(source, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    lines.append(json.loads(line))
                except json.JSONDecodeError as exc:
                    raise StoreError(
                        f"{self.path}:{number} 事件日志损坏: {exc}"
                    ) from exc
        return lines

    def fold(self) -> CampaignState:
        state = CampaignState()
        for event in self.events():
            payload = event["payload"]
            kind = event["type"]
            if kind in (
                EventType.CAMPAIGN_SUBMITTED,
                EventType.CAMPAIGN_APPROVED,
                EventType.CAMPAIGN_REJECTED,
                EventType.CAMPAIGN_LAUNCHED,
                EventType.CAMPAIGN_REVALIDATED,
            ):
                record = state.record(payload["campaign_id"])
                record.fingerprint = payload.get("fingerprint", record.fingerprint)
                record.last_snapshot = payload.get("snapshot", record.last_snapshot)
                record.cost_signature = payload.get(
                    "cost_signature", record.cost_signature
                )
                stage_map = {
                    EventType.CAMPAIGN_SUBMITTED: CampaignStage.SUBMITTED,
                    EventType.CAMPAIGN_APPROVED: CampaignStage.APPROVED,
                    EventType.CAMPAIGN_REJECTED: CampaignStage.REJECTED,
                    EventType.CAMPAIGN_LAUNCHED: CampaignStage.LAUNCHED,
                }
                if kind in stage_map:
                    record.stage = stage_map[kind]
                if kind == EventType.CAMPAIGN_REVALIDATED:
                    record.last_revalidation_event = event["event_id"]
            elif kind == EventType.CAMPAIGN_REVISED:
                record = state.record(payload["campaign_id"])
                record.revision = int(payload["new_revision"])
                record.fingerprint = payload.get("fingerprint", record.fingerprint)
                record.stage = CampaignStage.SUBMITTED
            elif kind == EventType.CAMPAIGN_DEVIATION_LOGGED:
                record = state.record(payload["campaign_id"])
                record.deviations.append(Deviation(
                    revision=int(payload["revision"]),
                    event_id=event["event_id"],
                    recorded_at=event["at"],
                    old_status=payload["old_status"],
                    new_status=payload["new_status"],
                    margin=payload.get("margin"),
                    floor=payload.get("floor"),
                    gap=payload.get("gap"),
                    impact_amount=payload.get("impact_amount"),
                    owner=payload.get("owner"),
                    note=payload.get("note", ""),
                ))
            elif kind == EventType.CAMPAIGN_DEVIATION_RESOLVED:
                record = state.record(payload["campaign_id"])
                for deviation in reversed(record.deviations):
                    if (
                        deviation.event_id == payload["deviation_event_id"]
                        and not deviation.resolved
                    ):
                        deviation.resolved = True
                        deviation.resolution_note = payload.get("note")
                        deviation.resolved_at = event["at"]
                        break
                else:
                    raise StoreError(
                        f"找不到待处理的偏差事件 {payload.get('deviation_event_id')}"
                    )
            elif kind == EventType.SETTLEMENT_CONFIRMED:
                state.settlement_overrides[payload["settlement_id"]] = {
                    **payload, "at": event["at"],
                }
            elif kind == EventType.AUDIT_SCOPE_CONFIRMED:
                state.audit_confirmations[payload["audit_id"]] = {
                    **payload, "at": event["at"],
                }
            elif kind == EventType.COST_CORRECTED:
                state.cost_corrections.append({**payload, "at": event["at"]})
            else:
                raise StoreError(f"未知事件类型 {kind}")
        return state

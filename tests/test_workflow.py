"""工作流测试：审批闸门、更正重验、已上线偏差、只追加历史、不可覆盖。"""
from __future__ import annotations

from decimal import Decimal

from priceguard import engine
from priceguard.models import ProjectionStatus
from priceguard.store import CampaignStage, EventType, StoreError
from priceguard.workflow import WorkflowError
from tests._support import PriceGuardTestCase

D = Decimal


class ApprovalGateTests(PriceGuardTestCase):
    def test_ok_campaign_full_chain(self) -> None:
        self.workflow.submit("B1", "approver-qin")
        self.workflow.approve("B1", "approver-qin")
        self.workflow.launch("B1", "ops-lin")
        record = self.store.fold().record("B1")
        self.assertEqual(record.stage, CampaignStage.LAUNCHED)
        # 审批事件留存计算快照
        approved = [
            event for event in self.store.events()
            if event["type"] == EventType.CAMPAIGN_APPROVED
        ][0]
        self.assertEqual(approved["payload"]["snapshot"]["margin"], "85")
        self.assertTrue(approved["payload"]["fingerprint"].startswith("sha256:"))

    def test_breach_and_undetermined_cannot_be_approved(self) -> None:
        self.workflow.submit("B3", "approver-qin")
        with self.assertRaises(WorkflowError):
            self.workflow.approve("B3", "approver-qin")
        self.workflow.submit("B2", "approver-qin")
        with self.assertRaises(WorkflowError):
            self.workflow.approve("B2", "approver-qin")
        # 显式特批可放行但必须留痕
        event = self.workflow.approve("B3", "approver-qin", force=True)
        self.assertTrue(event["payload"]["forced"])
        self.assertIn("特批", event["payload"]["note"])

    def test_reject_requires_reason_and_blocks_launch(self) -> None:
        self.workflow.submit("B2", "approver-qin")
        with self.assertRaises(WorkflowError):
            self.workflow.reject("B2", "approver-qin", "")
        self.workflow.reject("B2", "approver-qin", "承担方未核实")
        with self.assertRaises(WorkflowError):
            self.workflow.launch("B2", "ops-lin")

    def test_duplicate_submission_rejected(self) -> None:
        self.workflow.submit("B1", "approver-qin")
        with self.assertRaises(WorkflowError):
            self.workflow.submit("B1", "approver-qin")


class RevalidationTests(PriceGuardTestCase):
    def test_cost_correction_revalidates_not_launched_without_deviation(self) -> None:
        self.workflow.submit("B5", "approver-qin")
        self.workflow.correct_cost(
            "S2", "v1", "260", "2026-01-01", None,
            "product-bai", "成本口径错误更正",
        )
        state = self.store.fold()
        record = state.record("B5")
        self.assertEqual(record.stage, CampaignStage.SUBMITTED)
        self.assertEqual(record.last_snapshot["status"], ProjectionStatus.BREACH)
        # 未上线活动只产生重验事件，不产生偏差事件
        self.assertFalse(any(
            event["type"] == EventType.CAMPAIGN_DEVIATION_LOGGED
            and event["payload"]["campaign_id"] == "B5"
            for event in self.store.events()
        ))
        # 重验后再审批必须按新口径被拦截
        with self.assertRaises(WorkflowError):
            self.workflow.approve("B5", "approver-qin")

    def test_launched_campaign_keeps_approval_and_logs_deviation(self) -> None:
        self.workflow.submit("B1", "approver-qin")
        self.workflow.approve("B1", "approver-qin")
        self.workflow.launch("B1", "ops-lin")
        self.workflow.correct_cost(
            "S1", "v1", "110", "2026-01-01", "2026-10-14",
            "product-bai", "包材涨价",
        )
        state = self.store.fold()
        record = state.record("B1")
        # 阶段仍为 launched，原审批未被覆盖
        self.assertEqual(record.stage, CampaignStage.LAUNCHED)
        self.assertEqual(len(record.deviations), 1)
        deviation = record.deviations[0]
        self.assertEqual(deviation.old_status, ProjectionStatus.OK)
        self.assertEqual(deviation.new_status, ProjectionStatus.BREACH)
        self.assertEqual(deviation.owner, "ops-lin")
        # 毛利 71，底线 80，缺口 -9/件 × 1000 件
        self.assertEqual(deviation.impact_amount, "-9000")
        self.assertFalse(deviation.resolved)

    def test_resolve_deviation_records_owner_and_note(self) -> None:
        self.workflow.submit("B1", "approver-qin")
        self.workflow.approve("B1", "approver-qin")
        self.workflow.launch("B1", "ops-lin")
        self.workflow.correct_cost(
            "S1", "v1", "110", "2026-01-01", "2026-10-14",
            "product-bai", "包材涨价",
        )
        deviation_id = self.store.fold().record("B1").deviations[0].event_id
        with self.assertRaises(WorkflowError):
            self.workflow.resolve_deviation("B1", deviation_id, "ops-lin", "")
        self.workflow.resolve_deviation(
            "B1", deviation_id, "ops-lin", "追加平台补贴，下版恢复"
        )
        deviation = self.store.fold().record("B1").deviations[0]
        self.assertTrue(deviation.resolved)
        self.assertEqual(deviation.resolution_note, "追加平台补贴，下版恢复")

    def test_settlement_confirmation_triggers_revalidation(self) -> None:
        self.workflow.submit("B2", "approver-qin")
        self.assertEqual(
            self.store.fold().record("B2").last_snapshot["status"],
            ProjectionStatus.UNDETERMINED,
        )
        # 财务把 B2 报名的 coupon 落实为平台承担（补录结算，按 order/kind/channel 匹配）
        self.workflow.confirm_settlement(
            "ST03", "finance-he", "platform", "2026-10M"
        )
        # 注意：B2 活动计划本身仍登记 unknown，未确定来自报名数据；
        # 订单侧归因则立即按结算确认更新
        dataset = self.effective_dataset()
        order = engine.attribute_order(dataset, "O2")[0]
        self.assertEqual(order.platform_subsidy, D("18"))

    def test_refresh_is_idempotent_without_input_changes(self) -> None:
        self.workflow.submit("B1", "approver-qin")
        first = len(self.store.events())
        self.workflow.refresh()
        self.workflow.refresh()
        self.assertEqual(len(self.store.events()), first)


class AppendOnlyTests(PriceGuardTestCase):
    def test_history_is_append_only_and_keeps_each_approval(self) -> None:
        self.workflow.submit("B1", "approver-qin")
        self.workflow.approve("B1", "approver-qin")
        self.workflow.launch("B1", "ops-lin")
        self.workflow.correct_cost(
            "S1", "v1", "110", "2026-01-01", "2026-10-14",
            "product-bai", "包材涨价",
        )
        events = self.store.events()
        types = [event["type"] for event in events]
        # 提交、批准、上线、更正、重验、偏差各至少一条，顺序保留
        self.assertIn(EventType.CAMPAIGN_APPROVED, types)
        self.assertIn(EventType.CAMPAIGN_REVALIDATED, types)
        self.assertIn(EventType.CAMPAIGN_DEVIATION_LOGGED, types)
        # 原始批准快照仍为成本更正前的毛利 85
        approved = next(
            event for event in events
            if event["type"] == EventType.CAMPAIGN_APPROVED
        )
        self.assertEqual(approved["payload"]["snapshot"]["margin"], "85")
        latest = next(
            event for event in reversed(events)
            if event["type"] == EventType.CAMPAIGN_REVALIDATED
        )
        self.assertEqual(latest["payload"]["snapshot"]["margin"], "71")

    def test_duplicate_event_id_rejected(self) -> None:
        self.workflow.submit("B1", "approver-qin", )
        with self.assertRaises(StoreError):
            self.store.append(
                EventType.CAMPAIGN_APPROVED,
                {"campaign_id": "B1", "snapshot": {}},
                event_id="fixed-id",
            )
            self.store.append(
                EventType.CAMPAIGN_LAUNCHED,
                {"campaign_id": "B1", "snapshot": {}},
                event_id="fixed-id",
            )

    def test_revision_cannot_overwrite_old_campaign(self) -> None:
        self.workflow.submit("B1", "approver-qin")
        snapshot = self.store.fold().record("B1").last_snapshot
        self.workflow.revise_campaign(
            "B1", 1, "ops-lin", "券面调整", snapshot
        )
        with self.assertRaises(WorkflowError):
            self.workflow.revise_campaign(
                "B1", 1, "ops-lin", "重复版本号", snapshot
            )
        record = self.store.fold().record("B1")
        self.assertEqual(record.revision, 1)
        self.assertEqual(record.stage, CampaignStage.SUBMITTED)
        # 旧事件仍在日志中
        self.assertTrue(any(
            event["type"] == EventType.CAMPAIGN_SUBMITTED
            for event in self.store.events()
        ))

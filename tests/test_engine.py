"""引擎规则测试：承担方归因、平台补贴隔离、未确定口径、套组成本与规格归一。"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

from priceguard import engine
from priceguard.models import Bearer, ConflictStatus, ProjectionStatus
from tests._support import PriceGuardTestCase

D = Decimal


class OrderAttributionTests(PriceGuardTestCase):
    def test_o1_fully_settled_platform_and_brand_split(self) -> None:
        result = engine.attribute_order(self.base, "O1")[0]
        self.assertEqual(result.platform_subsidy, D("25"))
        self.assertEqual(result.brand_burden, D("27"))
        self.assertEqual(result.undetermined_amount, D("0"))
        # 平台补贴 25 不进品牌损益：净收入 208-27=181，毛利 181-96=85
        self.assertEqual(result.confirmed_margin, D("85"))
        self.assertEqual(result.reconciliation_gap, D("0"))

    def test_pending_settlement_is_undetermined_not_brand_concession(self) -> None:
        result = engine.attribute_order(self.base, "O2")[0]
        self.assertEqual(result.platform_subsidy, D("0"))
        self.assertEqual(result.brand_burden, D("0"))
        # ST03 待核实的 18 + 未核销差额 22 都必须是未确定
        self.assertEqual(result.undetermined_amount, D("40"))
        self.assertIn("ST03", result.pending_settlements)
        # 已确认毛利不含未确定优惠；最坏情况把未确定视作品牌损失
        self.assertEqual(result.confirmed_margin, D("119"))
        self.assertEqual(result.worst_case_margin, D("79"))

    def test_missing_settlement_surfaces_unresolved(self) -> None:
        result = engine.attribute_order(self.base, "O4")[0]
        self.assertEqual(result.unresolved_kinds, ["coupon"])
        self.assertTrue(result.has_undetermined)
        self.assertEqual(result.brand_burden, D("0"))

    def test_finance_confirmation_moves_amount_to_platform(self) -> None:
        self.workflow.confirm_settlement(
            "ST03", "finance-he", Bearer.PLATFORM, "2026-10M"
        )
        dataset = self.effective_dataset()
        result = engine.attribute_order(dataset, "O2")[0]
        self.assertEqual(result.platform_subsidy, D("18"))
        self.assertEqual(result.brand_burden, D("0"))
        # 核销表 owner=unknown 与结算不一致时，以结算为准并留说明
        self.assertTrue(any("以结算为准" in reason for reason in result.reasons))
        # 仍有 22 的未核销差额，不能因为结算确认就抹平
        self.assertEqual(result.undetermined_amount, D("22"))

    def test_cost_version_switch_by_date(self) -> None:
        old = engine.effective_cost(self.base, "S1", dt.date(2026, 10, 1))
        new = engine.effective_cost(self.base, "S1", dt.date(2026, 10, 15))
        self.assertEqual((old.version, old.cost), ("v1", D("96")))
        self.assertEqual((new.version, new.cost), ("v2", D("102")))

    def test_missing_cost_is_undetermined_not_zero(self) -> None:
        from priceguard.models import CostVersion
        self.base.costs = [
            item for item in self.base.costs if item.sku != "S2"
        ]
        result = engine.attribute_order(self.base, "O4")[0]
        self.assertIsNone(result.cost)
        self.assertIsNone(result.confirmed_margin)
        self.assertTrue(any("无有效成本版本" in r for r in result.reasons))

    def test_bundle_cost_sums_components(self) -> None:
        result = engine.cost_for_sku_spec(
            self.base, "S3", "套组2x50ml", dt.date(2026, 10, 8)
        )
        self.assertEqual(result.version, "bundle")
        self.assertEqual(result.cost, D("240"))  # S2 v1 120 x2

    def test_bundle_with_corrected_component_cost(self) -> None:
        self.workflow.correct_cost(
            "S2", "v1", "135", "2026-01-01", None,
            "product-bai", "组件成本更正",
        )
        dataset = self.effective_dataset()
        result = engine.cost_for_sku_spec(
            dataset, "S3", "套组2x50ml", dt.date(2026, 10, 8)
        )
        self.assertEqual(result.cost, D("270"))


class CampaignProjectionTests(PriceGuardTestCase):
    def test_ok_campaign_keeps_platform_money_out_of_brand_margin(self) -> None:
        projection = engine.project_campaign(self.base, "B1")
        self.assertEqual(projection.status, ProjectionStatus.OK)
        self.assertEqual(projection.platform_total, D("25"))
        self.assertEqual(projection.brand_total, D("27"))
        self.assertEqual(projection.effective_paid, D("208"))
        self.assertEqual(projection.margin, D("85"))

    def test_unknown_owner_blocks_approval_as_undetermined(self) -> None:
        projection = engine.project_campaign(self.base, "B2")
        self.assertEqual(projection.status, ProjectionStatus.UNDETERMINED)
        self.assertEqual(projection.unknown_total, D("18"))
        self.assertEqual(projection.brand_total, D("0"))
        self.assertIsNotNone(projection.worst_case_margin)
        self.assertTrue(any("不得假设" in r for r in projection.reasons))

    def test_breach_is_detected_against_floor(self) -> None:
        projection = engine.project_campaign(self.base, "B3")
        self.assertEqual(projection.status, ProjectionStatus.BREACH)
        self.assertEqual(projection.margin, D("200"))
        self.assertEqual(projection.floor_gap, D("-20"))
        self.assertEqual(
            projection.floor_gap * projection.expected_units, D("-10000")
        )

    def test_bundle_campaign_uses_component_cost(self) -> None:
        projection = engine.project_campaign(self.base, "B4")
        self.assertEqual(projection.cost_version, "bundle")
        self.assertEqual(projection.cost, D("240"))
        self.assertEqual(projection.status, ProjectionStatus.OK)


class ConflictTests(PriceGuardTestCase):
    def test_confirmed_vs_pending_vs_undetermined(self) -> None:
        conflicts = engine.find_conflicts(self.base)
        statuses = {(c.conflict_id, c.status) for c in conflicts}
        # 已确认的线上渠道冲突
        self.assertIn(("C001", ConflictStatus.CONFIRMED), statuses)
        # 未确认区域范围的线下价只能 pending
        pending = [c for c in conflicts if c.status == ConflictStatus.PENDING]
        self.assertTrue(pending)
        self.assertTrue(any("SH01" in c.ref_b for c in pending))
        # 缺少规格映射 -> 未确定，不给违规结论
        unmapped = [c for c in conflicts if c.kind == "unmapped"]
        self.assertTrue(unmapped)
        self.assertTrue(all(
            c.status == ConflictStatus.UNDETERMINED and c.delta is None
            for c in unmapped
        ))

    def test_bundle_not_compared_as_single_unit_price(self) -> None:
        conflicts = engine.find_conflicts(self.base)
        # S3 套组与 S2 单品是不同 sku，不得直接比较
        self.assertFalse(any(
            (c.sku == "S3" and c.kind == "exact") for c in conflicts
        ))

    def test_normalized_conflict_uses_unit_price(self) -> None:
        conflicts = engine.find_conflicts(self.base)
        normalized = [c for c in conflicts if c.kind == "normalized"]
        self.assertTrue(normalized)
        for conflict in normalized:
            self.assertIsNotNone(conflict.unit_price_a)
            self.assertIsNotNone(conflict.unit_price_b)
            self.assertTrue(any("归一" in r for r in conflict.reasons))

    def test_region_confirmation_promotes_pending_only(self) -> None:
        before = {
            c.conflict_id: c.status for c in engine.find_conflicts(self.base)
        }
        self.workflow.confirm_audit_scope("SH01", "region-wang")
        dataset = self.effective_dataset()
        after = {
            c.conflict_id: c.status for c in engine.find_conflicts(dataset)
        }
        changed = [cid for cid, status in after.items()
                   if before[cid] != status]
        self.assertTrue(changed)
        for cid in changed:
            self.assertEqual(before[cid], ConflictStatus.PENDING)
            self.assertEqual(after[cid], ConflictStatus.CONFIRMED)
        # 缺映射的未确定项不会因为区域确认而变成违规
        self.assertTrue(all(
            after[cid] == ConflictStatus.UNDETERMINED
            for cid, status in before.items()
            if status == ConflictStatus.UNDETERMINED
        ))

"""装载器结构校验与 CLI 端到端测试。"""
from __future__ import annotations

import io
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from priceguard.cli import main
from priceguard.loader import LoaderError, load_dataset
from tests._support import PriceGuardTestCase, SAMPLES


class LoaderTests(unittest.TestCase):
    def test_samples_load(self) -> None:
        dataset = load_dataset(SAMPLES)
        self.assertTrue(dataset.prices)
        self.assertTrue(dataset.settlements)
        self.assertTrue(dataset.campaigns)

    def test_bad_header_rejected(self) -> None:
        import tempfile, shutil
        tmp = Path(tempfile.mkdtemp())
        try:
            shutil.copytree(SAMPLES, tmp / "data")
            (tmp / "data" / "costs.csv").write_text(
                "sku,version,cost,from\nS1,v1,96,2026-01-01\n", encoding="utf-8"
            )
            with self.assertRaises(LoaderError):
                load_dataset(tmp / "data")
        finally:
            shutil.rmtree(tmp)

    def test_broken_bundle_mapping_rejected(self) -> None:
        import tempfile, shutil
        tmp = Path(tempfile.mkdtemp())
        try:
            shutil.copytree(SAMPLES, tmp / "data")
            (tmp / "data" / "spec_map.csv").write_text(
                "sku,spec,canonical_unit,units,bundle_of\n"
                "S9,套装,ml,,S2:1;S1\n", encoding="utf-8"
            )
            with self.assertRaises(LoaderError):
                load_dataset(tmp / "data")
        finally:
            shutil.rmtree(tmp)

    def test_negative_money_rejected(self) -> None:
        import tempfile, shutil
        tmp = Path(tempfile.mkdtemp())
        try:
            shutil.copytree(SAMPLES, tmp / "data")
            (tmp / "data" / "prices.csv").write_text(
                "sku,channel,spec,list_price,paid,day\n"
                "S1,market-z,30ml,260,-1,2026-10-01\n", encoding="utf-8"
            )
            with self.assertRaises(LoaderError):
                load_dataset(tmp / "data")
        finally:
            shutil.rmtree(tmp)


class CliTests(PriceGuardTestCase):
    def run_cli(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        base = [
            "--data-dir", str(self.data_dir),
            "--event-log", str(self.event_log),
        ]
        with redirect_stdout(out), redirect_stderr(err):
            code = main(base + list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_orders_json(self) -> None:
        import json
        code, out, _ = self.run_cli("orders", "--json")
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual({row["order_id"] for row in payload},
                         {"O1", "O2", "O3", "O4"})

    def test_campaigns_exit_code_reflects_gate(self) -> None:
        code, _, _ = self.run_cli("campaigns")
        self.assertEqual(code, 1)  # 存在 breach / undetermined
        code, _, _ = self.run_cli("campaign", "B1")
        self.assertEqual(code, 0)
        code, _, _ = self.run_cli("campaign", "B3")
        self.assertEqual(code, 1)

    def test_conflicts_exit_code_and_pending_wording(self) -> None:
        code, out, _ = self.run_cli("conflicts")
        self.assertEqual(code, 1)  # 含已确认冲突
        self.assertIn("待确认（不计违规）", out)
        self.assertIn("未确定（缺少映射）", out)

    def test_approve_gate_and_full_chain(self) -> None:
        self.run_cli("submit", "B3", "--by", "q")
        code, _, err = self.run_cli("approve", "B3", "--by", "q")
        self.assertEqual(code, 2)
        self.assertIn("breach", err)
        self.run_cli("submit", "B1", "--by", "q")
        code, _, _ = self.run_cli("approve", "B1", "--by", "q")
        self.assertEqual(code, 0)
        code, _, _ = self.run_cli("launch", "B1", "--by", "lin")
        self.assertEqual(code, 0)

    def test_cost_correction_shows_deviation_in_status(self) -> None:
        self.run_cli("submit", "B1", "--by", "q")
        self.run_cli("approve", "B1", "--by", "q")
        self.run_cli("launch", "B1", "--by", "lin")
        self.run_cli(
            "cost-correct", "S1", "v1", "110",
            "--from", "2026-01-01", "--to", "2026-10-14",
            "--by", "bai", "--reason", "包材涨价",
        )
        code, out, _ = self.run_cli("status")
        self.assertEqual(code, 0)
        self.assertIn("launched", out)
        self.assertIn("待处理", out)
        self.assertIn("ops-lin", out)

    def test_history_lists_append_only_events(self) -> None:
        self.run_cli("submit", "B1", "--by", "q")
        code, out, _ = self.run_cli("history")
        self.assertEqual(code, 0)
        self.assertIn("campaign_submitted", out)


if __name__ == "__main__":
    unittest.main()

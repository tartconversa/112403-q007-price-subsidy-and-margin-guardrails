"""测试共用工具：装载仓库样例数据与临时事件日志。"""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from priceguard.loader import load_dataset
from priceguard.store import EventStore
from priceguard.workflow import Workflow

SAMPLES = Path(__file__).resolve().parent.parent / "samples"


class PriceGuardTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="priceguard-"))
        self.data_dir = self.tmp / "data"
        shutil.copytree(SAMPLES, self.data_dir)
        self.event_log = self.tmp / "events.jsonl"
        self.base = load_dataset(self.data_dir)
        self.store = EventStore(self.event_log)
        self.workflow = Workflow(self.base, self.store)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def effective_dataset(self):
        from priceguard.workflow import apply_state
        return apply_state(self.base, self.store.fold())

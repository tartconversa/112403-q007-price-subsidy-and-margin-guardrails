#!/usr/bin/env python3 -I
"""命令行启动器：保持隔离模式（-I），并把仓库目录显式加入 sys.path。

用法：
    ./run.py orders
    python3 -I run.py campaigns --json
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from priceguard.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())

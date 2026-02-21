"""메트릭 요약 출력 스크립트."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from modules.logging_config import setup_logging
from modules.metrics import MetricsStore


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="메트릭 요약 출력")
    parser.add_argument("--db", default="data/automation.db")
    parser.add_argument("--indent", type=int, default=2)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    setup_logging()
    metrics = MetricsStore(db_path=args.db)
    print(json.dumps(metrics.get_summary(), ensure_ascii=False, indent=args.indent))


if __name__ == "__main__":
    main()

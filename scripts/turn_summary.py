"""Print a human-readable timing summary of one turn.

Eurus writes a JSON dump of the recorded timing spans when both
``EURUS_TIMING=1`` and ``EURUS_TIMING_DUMP=<path>`` are set (the TUI dumps
after every turn). This script renders that dump.

Usage:
    python scripts/turn_summary.py [PATH]   # default: $EURUS_TIMING_DUMP
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def main() -> int:
    path = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("EURUS_TIMING_DUMP", "")
    if not path:
        print("usage: python scripts/turn_summary.py <timing.json>")
        return 2
    target = Path(path)
    if not target.is_file():
        print(f"timing dump not found: {target}")
        return 1
    payload = json.loads(target.read_text(encoding="utf-8"))

    spans = payload.get("spans", [])
    summary = payload.get("summary", {})
    turn_id = payload.get("turn_id", "")

    title = f"turn {turn_id}" if turn_id else "turn timing summary"
    print(f"{title} ({len(spans)} spans)")
    if not summary:
        print("  (no spans recorded; run with EURUS_TIMING=1)")
        return 0
    width = max(len(name) for name in summary)
    for name, bucket in sorted(summary.items(), key=lambda item: -item[1]["total_ms"]):
        print(
            f"  {name:<{width}}  count={int(bucket['count']):<5} "
            f"total={bucket['total_ms']:>9.1f}ms "
            f"avg={bucket['avg_ms']:>8.2f}ms "
            f"max={bucket['max_ms']:>9.1f}ms"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

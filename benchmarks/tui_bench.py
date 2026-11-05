from __future__ import annotations

import json
import statistics
import time
from io import StringIO

from rich.console import Console

from eurus.tui.renderer import StreamView, TerminalRenderer
from eurus.tui.theme import Theme


def measure(function, rounds: int = 1000) -> dict[str, float]:
    samples = []
    for _ in range(rounds):
        started = time.perf_counter_ns()
        function()
        samples.append((time.perf_counter_ns() - started) / 1_000_000)
    ordered = sorted(samples)
    return {
        "rounds": float(rounds),
        "median_ms": statistics.median(samples),
        "p95_ms": ordered[int((len(ordered) - 1) * 0.95)],
        "max_ms": max(samples),
    }


def main() -> None:
    console = Console(file=StringIO(), width=80, force_terminal=False)
    renderer = TerminalRenderer(console)
    view = StreamView(Theme.for_unicode(False))
    view.feed("token " * 600)
    results = {
        "stream_frame": measure(view.frame),
        "stream_tail": measure(lambda: view.tail(80)),
        "status_render": measure(lambda: renderer.status("ready")),
    }
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()

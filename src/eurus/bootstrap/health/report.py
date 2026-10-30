from __future__ import annotations

import json

from rich.console import Console
from rich.table import Table
from rich.text import Text

from eurus.bootstrap.models import HealthReport

STATUS_STYLES = {
    "READY": "green",
    "MISSING": "yellow",
    "BROKEN": "red",
    "OUTDATED": "yellow",
    "OPTIONAL": "bright_black",
    "BLOCKED": "red",
    "UNKNOWN": "bright_black",
}

SUMMARY_STYLES = {
    "READY": "bold green",
    "PARTIAL": "bold yellow",
    "DEGRADED": "bold yellow",
    "BLOCKED": "bold red",
}

SUMMARY_HINTS = {
    "READY": "EURUS is ready to use.",
    "PARTIAL": "Optional features are missing; run eurus bootstrap to configure them.",
    "DEGRADED": "Some components are broken; run eurus bootstrap --repair.",
    "BLOCKED": "Required components are missing or broken; run eurus bootstrap.",
}


def summary_style(summary: str) -> str:
    return SUMMARY_STYLES.get(summary, "bold")


def render_report(report: HealthReport, console: Console, *, title: str = "eurus doctor") -> None:
    table = Table(title=title, title_justify="left", box=None, pad_edge=False)
    table.add_column("Status", width=9)
    table.add_column("Check", style="bold")
    table.add_column("Detail")
    for result in report.results:
        style = STATUS_STYLES.get(result.status, "white")
        table.add_row(f"[{style}]{result.status}[/{style}]", result.name, result.detail or "")
    console.print(table)
    hint = report.hints_text()
    if hint:
        console.print()
        console.print(Text("Hints", style="bold"))
        for line in hint.splitlines():
            console.print(f"  {line}")
    console.print()
    style = summary_style(report.summary)
    hint = SUMMARY_HINTS.get(report.summary, "")
    console.print(f"[{style}]summary: {report.summary}[/{style}] - {hint}")


def report_text(report: HealthReport) -> str:
    lines = [f"summary: {report.summary}"]
    for result in report.results:
        lines.append(f"{result.status:<9} {result.name:<22} {result.detail}")
    return "\n".join(lines)


def report_json(report: HealthReport) -> str:
    return json.dumps(report.to_dict(), indent=2)

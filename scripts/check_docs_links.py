from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LINK_PATTERN = re.compile(r"\[[^\]]+\]\(((?!https?://|#)[^)\s]+)\)")

failures: list[str] = []


def check_file(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    for match in LINK_PATTERN.finditer(text):
        target = match.group(1).split("#")[0]
        if not target:
            continue
        resolved = (path.parent / target).resolve()
        if not resolved.exists():
            failures.append(f"{path.relative_to(ROOT)}: broken link {target}")


def main() -> int:
    markdown_files = [ROOT / "README.md", *sorted((ROOT / "docs").glob("*.md"))]
    for path in markdown_files:
        if path.exists():
            check_file(path)
    if failures:
        for item in failures:
            print(f"ERROR: {item}")
        return 1
    print(f"OK: all internal links resolve across {len(markdown_files)} files")
    return 0


if __name__ == "__main__":
    sys.exit(main())

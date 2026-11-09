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


def markdown_files() -> list[Path]:
    files = {ROOT / "README.md"}
    files.update(ROOT.glob("*.md"))
    files.update((ROOT / "docs").rglob("*.md"))
    files.update((ROOT / "benchmarks").rglob("*.md"))
    return sorted(files)


def main() -> int:
    markdown = [path for path in markdown_files() if path.exists()]
    for path in markdown:
        check_file(path)
    if failures:
        for item in failures:
            print(f"ERROR: {item}")
        return 1
    print(f"OK: all internal links resolve across {len(markdown)} files")
    return 0


if __name__ == "__main__":
    sys.exit(main())

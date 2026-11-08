from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from eurus.coding.validation import validate_command

MAX_TARGETED_FILES = 8
MAX_STATIC_FILES = 20
MAX_ARGV_LENGTH = 2_000
REGRESSION_ARGV: tuple[str, ...] = ("python", "-m", "pytest")
TEST_STEM_PREFIX = "test_"
TEST_STEM_SUFFIX = "_test.py"
TEST_DIR_CANDIDATES = ("tests", "test")


@dataclass(frozen=True, slots=True)
class TestCommand:
    argv: tuple[str, ...]
    category: str
    note: str = ""

    @property
    def display(self) -> str:
        return " ".join(self.argv)

    def to_dict(self) -> dict[str, object]:
        return {"argv": list(self.argv), "category": self.category, "note": self.note}


@dataclass(frozen=True, slots=True)
class TestPlan:
    root: str
    commands: tuple[TestCommand, ...] = ()
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def targeted(self) -> tuple[TestCommand, ...]:
        return tuple(item for item in self.commands if item.category == "targeted")

    @property
    def regression(self) -> tuple[TestCommand, ...]:
        return tuple(item for item in self.commands if item.category == "regression")

    @property
    def static(self) -> tuple[TestCommand, ...]:
        return tuple(item for item in self.commands if item.category == "static")

    def all_commands(self) -> tuple[TestCommand, ...]:
        ordered = (
            tuple(item for item in self.commands if item.category == "static")
            + self.targeted
            + self.regression
        )
        return ordered

    def to_dict(self) -> dict[str, object]:
        return {
            "root": self.root,
            "commands": [item.to_dict() for item in self.commands],
            "notes": list(self.notes),
        }


def build_test_strategy(
    root: Path | str,
    changed_paths: list[str] | tuple[str, ...] | None = None,
    *,
    include_regression: bool = True,
    include_static: bool = True,
    max_targeted: int = MAX_TARGETED_FILES,
) -> TestPlan:
    if not 1 <= max_targeted <= MAX_TARGETED_FILES:
        raise ValueError(f"max_targeted must be between 1 and {MAX_TARGETED_FILES}")
    base = Path(root).expanduser().resolve()
    if not base.is_dir():
        raise ValueError("workspace must be an existing directory")

    changed = _normalize(changed_paths or ())
    notes: list[str] = []
    commands: list[TestCommand] = []

    if include_static:
        commands.extend(_static_commands(base, changed, notes))
    commands.extend(_targeted_commands(base, changed, max_targeted, notes))
    if include_regression:
        commands.extend(_regression_commands(base, notes))

    validated: list[TestCommand] = []
    for command in commands:
        try:
            validate_command(command.argv)
        except ValueError as exc:
            notes.append(f"dropped {command.category} command ({exc}); it is not allowlisted")
            continue
        validated.append(command)
    return TestPlan(root=str(base), commands=tuple(validated), notes=tuple(notes))


def _normalize(changed_paths: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    normalized: list[str] = []
    for raw in changed_paths:
        text = str(raw).strip().replace("\\", "/")
        if text.startswith("./"):
            text = text[2:]
        text = text.lstrip("/")
        if text and text not in normalized:
            normalized.append(text)
    return tuple(normalized[:100])


def is_test_path(relative: str) -> bool:
    posix = relative.replace("\\", "/")
    name = Path(posix).name
    return (
        posix.split("/")[0] in TEST_DIR_CANDIDATES
        or name.startswith(TEST_STEM_PREFIX)
        or name.endswith(TEST_STEM_SUFFIX)
    )


def _mirror_candidates(relative: str) -> tuple[str, ...]:
    path = Path(relative)
    stem = path.stem
    if stem.startswith(TEST_STEM_PREFIX):
        stem = stem[len(TEST_STEM_PREFIX) :]
    if stem.endswith("_test"):
        stem = stem[: -len("_test")]
    parent = path.parent
    return (
        str(parent / f"test_{stem}.py"),
        str(Path(TEST_DIR_CANDIDATES[0]) / f"test_{stem}.py"),
        str(parent / f"{stem}_test.py"),
    )


def _resolve_test_targets(base: Path, changed: tuple[str, ...]) -> tuple[list[str], list[str]]:
    direct: list[str] = []
    unmapped: list[str] = []
    for relative in changed:
        if not relative.endswith(".py"):
            unmapped.append(relative)
            continue
        if is_test_path(relative):
            direct.append(relative)
            continue
        matched = False
        for candidate in _mirror_candidates(relative):
            if (base / candidate).is_file():
                direct.append(candidate)
                matched = True
                break
        if not matched:
            unmapped.append(relative)
    return direct, unmapped


def _targeted_commands(
    base: Path,
    changed: tuple[str, ...],
    max_targeted: int,
    notes: list[str],
) -> list[TestCommand]:
    direct, unmapped = _resolve_test_targets(base, changed)
    if unmapped:
        notes.append(
            f"no mirror test file found for {len(unmapped)} changed path(s); "
            "they are covered by the regression command"
        )
    selected: list[str] = []
    for candidate in direct:
        if candidate not in selected:
            selected.append(candidate)
        if len(selected) >= max_targeted:
            notes.append(
                f"targeted plan capped at {max_targeted} test file(s); "
                "the rest fall back to the regression command"
            )
            break
    if not selected:
        return []
    argv = ("python", "-m", "pytest", *selected)
    if len(" ".join(argv)) > MAX_ARGV_LENGTH:
        notes.append("targeted command exceeded the argv budget and was trimmed")
        argv = ("python", "-m", "pytest", *selected[:2])
    return [
        TestCommand(
            argv=argv,
            category="targeted",
            note=f"covers {len(selected)} changed test file(s)",
        )
    ]


def _regression_commands(base: Path, notes: list[str]) -> list[TestCommand]:
    tests_dir = base / TEST_DIR_CANDIDATES[0]
    if tests_dir.is_dir():
        return [
            TestCommand(
                argv=(*REGRESSION_ARGV, TEST_DIR_CANDIDATES[0]),
                category="regression",
                note="full suite over the tests/ directory",
            )
        ]
    notes.append("no tests/ directory detected; regression runs from the workspace root")
    return [
        TestCommand(
            argv=REGRESSION_ARGV,
            category="regression",
            note="full suite from the workspace root",
        )
    ]


def _static_commands(base: Path, changed: tuple[str, ...], notes: list[str]) -> list[TestCommand]:
    lint_targets = [item for item in changed if item.endswith(".py")][:MAX_STATIC_FILES]
    if not lint_targets:
        return []
    commands: list[TestCommand] = []
    ruff_argv = ("ruff", "check", *lint_targets)
    if len(" ".join(ruff_argv)) <= MAX_ARGV_LENGTH:
        commands.append(
            TestCommand(argv=ruff_argv, category="static", note="lint the changed files")
        )
    mypy_argv = ("python", "-m", "mypy", *lint_targets)
    if len(" ".join(mypy_argv)) <= MAX_ARGV_LENGTH and (base / "mypy.ini").is_file():
        commands.append(
            TestCommand(argv=mypy_argv, category="static", note="type check the changed files")
        )
    return commands

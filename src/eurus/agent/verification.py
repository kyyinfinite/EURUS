from __future__ import annotations

import json
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from eurus.models import Message, ToolResult

TranscriptProvider = Callable[[], Sequence[Message]]

MAX_OBJECTIVE_CHARS = 8000
MAX_JUDGE_INPUT_CHARS = 6000
MAX_STEP_OUTPUT_CHARS = 4000
MAX_EVIDENCE_ITEMS = 8
FILE_EXTENSION_PATTERN = re.compile(
    r"(?:[\w./-]*[./])?[\w-]+\.(?:py|js|ts|tsx|jsx|md|txt|json|toml|yaml|yml|csv|html|css|sh)"
)
QUOTED_PATH_PATTERN = re.compile(r"[`\"']([^`\"'\n]{1,200})[`\"']")
COMMAND_TOOL_NAMES = frozenset({"shell", "run_checks"})
RESEARCH_TOOL_NAMES = frozenset(
    {"web_search", "web_fetch", "deep_search", "browser_navigate", "browser_read"}
)
COMMAND_EXECUTION_INTENT = re.compile(
    r"\b(?:run|running|execute|executing|invoke|invoking|launch|launching)\s+"
    r"(?:the\s+|all\s+|any\s+|our\s+|your\s+|a\s+)?"
    r"(?:[a-z][\w-]*\s+)?"
    r"(?:tests?|test[- ]suites?|pytest|unittest|build|scripts?|checks?|lint(?:ing)?|"
    r"type[- ]?checks?|suites?)\b",
    re.IGNORECASE,
)
BUILD_INTENT = re.compile(
    r"\b(?:build|compile|make)\s+(?:the\s+|this\s+|our\s+|a\s+)?"
    r"(?:project|code|codebase|package|library|app|application|binary|executable|"
    r"artifact)\b",
    re.IGNORECASE,
)
TEST_EXPECTATION_INTENT = re.compile(
    r"\b(?:all\s+)?tests?\s+(?:must|should|need\s+to|have\s+to)\s+pass\b"
    r"|\bmake\s+sure\b[^.]{0,80}\btests?\s+pass\b",
    re.IGNORECASE,
)
TOOL_CHECK_INTENT = re.compile(r"\brun_checks\b", re.IGNORECASE)
AMBIGUOUS_COMMAND_INTENT = re.compile(
    r"\b(?:command|commands|terminal|shell|bash|sh|script|scripts|run|runs|running|"
    r"ran|execute|executes|executing|executed|check|checks|checking|verify|verifying|"
    r"validate|validating|test|tests|testing|build|compile|compiling|make|npm|pytest|"
    r"mypy|ruff|lint)\b",
    re.IGNORECASE,
)
EXIT_STATUS_PATTERN = re.compile(r"\bexit\s*_?\s*(?:code|status)\s*[:=]?\s*(\d+)", re.IGNORECASE)
FAILED_EXIT_PATTERN = re.compile(r"\bfailed\s*\(\s*exit\s+(\d+)\s*\)", re.IGNORECASE)
RESULT_SUMMARY_PATTERN = re.compile(r"^\s*result\s*:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)
CHECK_STATUS_PATTERN = re.compile(
    r"^\s*(?:\[\s*(?P<bracket>[A-Za-z][\w .+-]{0,30})\s*\]"
    r"|(?P<colon>[A-Za-z][\w .+-]{0,30})\s*:)\s*"
    r"(?P<status>passed|failed|timed\s+out|unavailable)\b",
    re.IGNORECASE | re.MULTILINE,
)
FAILED_TALLY_PATTERN = re.compile(r"\b(\d+)\s+(?:tests?\s+)?failed\b", re.IGNORECASE)
RENAME_VERB_PATTERN = re.compile(
    r"\b(?:rename|renames|renamed|renaming|move|moves|moved|moving)\b", re.IGNORECASE
)
RENAME_PAIR_PATTERN = re.compile(
    r"[`\"']?(?P<source>[A-Za-z0-9_][A-Za-z0-9_./+-]*\.[A-Za-z0-9]+)[`\"']?"
    r"\s*(?:->|→|\bto\b|\bas\b)\s*"
    r"[`\"']?(?P<target>[A-Za-z0-9_][A-Za-z0-9_./+-]*\.[A-Za-z0-9]+)[`\"']?",
    re.IGNORECASE,
)
RESEARCH_INTENT_PATTERN = re.compile(
    r"\b(?:research|find out|search|sources?|cite|cites|cited|compare|news)\b",
    re.IGNORECASE,
)
URL_PATTERN = re.compile(r"https?://[^\s<>\"')\]]+")
HARD_FAILURE_MARKERS = (
    "traceback (most recent call last)",
    "syntaxerror",
    "failed to import",
    "permission denied",
    "no such file or directory",
)


@dataclass(frozen=True, slots=True)
class VerificationResult:
    passed: bool
    summary: str
    evidence: tuple[str, ...] = ()
    applies: bool = True


class StepVerifier(Protocol):
    async def __call__(
        self, title: str, success_criteria: str, output: str
    ) -> VerificationResult: ...


def _transcript_tool_results(transcript: Sequence[Message], limit: int = 40) -> list[ToolResult]:
    results: list[ToolResult] = []
    for message in transcript:
        if message.role == "tool" and message.content:
            results.append(
                ToolResult(
                    call_id=message.tool_call_id or "",
                    name=message.name or "",
                    output=message.content,
                )
            )
    return results[-limit:]


def _objective_mentions_files(objective: str) -> list[str]:
    mentioned: list[str] = []
    for match in QUOTED_PATH_PATTERN.finditer(objective):
        value = match.group(1).strip()
        if value and Path(value).name and not value.startswith(("-", "http")):
            mentioned.append(value)
    for match in FILE_EXTENSION_PATTERN.finditer(objective):
        value = match.group(0).strip("`\"'.,;:()")
        if value and len(value) < 200:
            mentioned.append(value)
    seen: set[str] = set()
    unique: list[str] = []
    for item in mentioned:
        key = item.casefold()
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique[:6]


def _safe_relative_candidate(root: Path, raw: str) -> Path | None:
    cleaned = raw.strip().strip("`\"'")
    if not cleaned or cleaned.startswith(("-", "~")) or "\\" in cleaned:
        return None
    if URL_PATTERN.match(cleaned):
        return None
    candidate = Path(cleaned)
    try:
        root_resolved = root.resolve()
    except OSError:
        return None
    if candidate.is_absolute():
        try:
            resolved = candidate.resolve()
        except OSError:
            return None
    else:
        try:
            resolved = (root / candidate).resolve()
        except OSError:
            return None
    if resolved != root_resolved and not resolved.is_relative_to(root_resolved):
        return None
    return resolved


class RuntimeVerificationService:
    def __init__(
        self,
        *,
        workspace: Path,
        transcript: TranscriptProvider,
        auxiliary: Any | None = None,
        model_judge: bool = False,
        judge_max_chars: int = MAX_JUDGE_INPUT_CHARS,
    ) -> None:
        self.workspace = workspace
        self.transcript = transcript
        self.auxiliary = auxiliary
        self.model_judge = model_judge
        self.judge_max_chars = judge_max_chars

    def _recent_tool_results(self) -> list[ToolResult]:
        try:
            transcript = self.transcript() or ()
        except Exception:
            return []
        return _transcript_tool_results(list(transcript))

    async def verify_completion(self, objective: str, content: str) -> VerificationResult:
        results = self._recent_tool_results()
        checks: list[VerificationResult] = []
        checks.extend(self._check_workspace_files(objective))
        checks.extend(self._check_command_evidence(objective, results))
        checks.extend(self._check_research_evidence(objective, content, results))
        for check in checks:
            if not check.passed and check.applies:
                return check
        applicable = [check for check in checks if check.applies]
        passing = [check for check in applicable if check.passed]
        if passing:
            evidence: list[str] = []
            for check in passing:
                evidence.extend(check.evidence)
            return VerificationResult(
                True,
                "deterministic checks passed",
                tuple(evidence[:MAX_EVIDENCE_ITEMS]),
            )
        if self.model_judge and self.auxiliary is not None:
            judged = await self._judge(objective, content, results)
            if judged is not None:
                return judged
        return VerificationResult(
            False,
            "no deterministic evidence supports the claimed completion; the model "
            "assertion alone is not sufficient",
            ("objective_only_claim",),
            applies=bool(applicable),
        )

    async def verify_step(
        self, title: str, success_criteria: str, output: str
    ) -> VerificationResult:
        trimmed = output.strip()
        if not trimmed:
            return VerificationResult(
                False, "step produced no output to verify", ("empty_step_output",)
            )
        lowered = trimmed.lower()
        failures = [marker for marker in HARD_FAILURE_MARKERS if marker in lowered]
        if failures and not _has_success_indicator(trimmed):
            return VerificationResult(
                False,
                f"step output contains failure markers: {', '.join(failures[:3])}",
                tuple(f"failure_marker:{marker}" for marker in failures[:3]),
            )
        missing_requirements = _missing_step_requirements(success_criteria, trimmed)
        if missing_requirements:
            return VerificationResult(
                False,
                "step output does not mention artifacts required by the success criteria: "
                + ", ".join(missing_requirements[:3]),
                tuple(f"missing_step_requirement:{item}" for item in missing_requirements[:3]),
            )
        return VerificationResult(
            True,
            "step output contains no deterministic failure markers",
            ("no_failure_markers", f"output_chars:{len(trimmed)}"),
        )

    def _check_workspace_files(self, objective: str) -> list[VerificationResult]:
        bounded_objective = objective[:MAX_OBJECTIVE_CHARS]
        mentioned = _objective_mentions_files(bounded_objective)
        lowered = objective.lower()
        deleting = any(word in lowered for word in ("delete", "remove", "clean up"))
        pairs = (
            RENAME_PAIR_PATTERN.findall(bounded_objective)
            if RENAME_VERB_PATTERN.search(bounded_objective)
            else []
        )
        rename_sources = {_normalize_mention(source) for source, _ in pairs}
        checks: list[VerificationResult] = []
        for raw in mentioned:
            path = _safe_relative_candidate(self.workspace, raw)
            if path is None:
                continue
            if deleting and not path.exists():
                checks.append(
                    VerificationResult(
                        True,
                        f"{raw!r} is absent as the deletion objective requires",
                        (f"file_absent:{raw}",),
                    )
                )
                continue
            if _normalize_mention(raw) in rename_sources:
                if path.exists():
                    checks.append(
                        VerificationResult(
                            True,
                            f"{raw!r} rename source is still present after the rename",
                            (f"rename_source_present:{raw}",),
                        )
                    )
                else:
                    checks.append(
                        VerificationResult(
                            True,
                            f"{raw!r} is absent as expected after the rename",
                            (f"rename_source_absent:{raw}",),
                        )
                    )
                continue
            if not path.exists():
                checks.append(
                    VerificationResult(
                        False,
                        f"objective requires {raw!r} but it does not exist in the workspace",
                        (f"missing_file:{raw}",),
                    )
                )
            elif path.is_file() and path.stat().st_size == 0:
                checks.append(
                    VerificationResult(
                        False,
                        f"objective requires {raw!r} but the file is empty",
                        (f"empty_file:{raw}",),
                    )
                )
            else:
                try:
                    size = path.stat().st_size
                except OSError:
                    size = -1
                checks.append(
                    VerificationResult(
                        True,
                        f"{raw!r} exists in the workspace",
                        (f"file_exists:{raw}", f"size:{size}"),
                    )
                )
        return checks

    def _check_command_evidence(
        self, objective: str, results: Sequence[ToolResult]
    ) -> list[VerificationResult]:
        intent = _command_intent(objective)
        if intent == "none":
            return []
        if intent == "ambiguous":
            return [
                VerificationResult(
                    False,
                    "objective only ambiguously requests command execution; command "
                    "evidence is not verified for this run",
                    ("command_intent_ambiguous",),
                    applies=False,
                )
            ]
        command_results = [item for item in results if item.name in COMMAND_TOOL_NAMES]
        if not command_results:
            return [
                VerificationResult(
                    False,
                    "objective requires executing commands but no command tool ran in this run",
                    ("no_command_evidence",),
                )
            ]
        last = command_results[-1]
        if last.is_error:
            return [
                VerificationResult(
                    False,
                    f"command tool {last.name!r} ended with an error",
                    (f"command_error:{last.name}",),
                )
            ]
        failure = _command_output_failure(last.output)
        if failure is not None:
            summary, evidence = failure
            return [VerificationResult(False, summary, (evidence,))]
        return [
            VerificationResult(
                True,
                f"command tool {last.name!r} completed without an error status",
                (f"command_ok:{last.name}",),
            )
        ]

    def _check_research_evidence(
        self, objective: str, content: str, results: Sequence[ToolResult]
    ) -> list[VerificationResult]:
        researchy = RESEARCH_INTENT_PATTERN.search(objective) is not None
        if not researchy or _is_question(objective):
            return []
        research_tools_used = [item for item in results if item.name in RESEARCH_TOOL_NAMES]
        if not research_tools_used:
            return [
                VerificationResult(
                    False,
                    "research objective has no web or browser tool evidence in this run",
                    ("no_research_tool_evidence",),
                )
            ]
        citations = URL_PATTERN.findall(content)
        if not citations:
            return [
                VerificationResult(
                    False,
                    "research conclusions lack source citations in the final answer",
                    ("no_citations_in_answer",),
                )
            ]
        return [
            VerificationResult(
                True,
                "research answer cites sources and used web evidence tools",
                (
                    "research_tools:"
                    + ",".join(sorted({item.name for item in research_tools_used})[:3]),
                    f"citations:{len(citations)}",
                ),
            )
        ]

    async def _judge(
        self, objective: str, content: str, results: Sequence[ToolResult]
    ) -> VerificationResult | None:
        router = self.auxiliary
        if router is None:
            return None
        evidence = "\n".join(f"[{item.name}] {item.output[:400]}" for item in results[-6:])[
            : self.judge_max_chars // 2
        ]
        prompt = (
            "You are a strict completion judge for an autonomous agent. Decide whether "
            "the final answer actually satisfies the objective, using only the tool "
            "evidence provided. A claim without supporting evidence must fail. "
            'Reply with JSON only: {"passed": true|false, "summary": "..."}.\n\n'
            f"Objective: {objective[:1500]}\n\n"
            f"Final answer: {content[:2500]}\n\n"
            f"Recent tool evidence:\n{evidence or '(none)'}"
        )
        try:
            response = await router.chat([Message.user(prompt)])
            answer = response.message.content.strip()
        except Exception:
            return None
        parsed = _parse_judge_json(answer)
        if parsed is None:
            return None
        passed, summary = parsed
        return VerificationResult(
            passed,
            f"model judge (advisory evidence): {summary[:300]}",
            (f"model_judge:{'passed' if passed else 'failed'}",),
        )


def _parse_judge_json(answer: str) -> tuple[bool, str] | None:
    start = answer.find("{")
    end = answer.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(answer[start : end + 1])
    except ValueError:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("passed"), bool):
        return None
    summary = str(data.get("summary", ""))[:400]
    return bool(data["passed"]), summary


def _is_question(objective: str) -> bool:
    lowered = objective.strip().lower()
    if not lowered:
        return False
    if lowered.endswith("?"):
        return True
    prefixes = (
        "what ",
        "who ",
        "when ",
        "where ",
        "why ",
        "how ",
        "is ",
        "are ",
        "was ",
        "were ",
        "do ",
        "does ",
        "did ",
        "can ",
        "could ",
        "should ",
        "which ",
        "explain ",
        "describe ",
        "summarize ",
        "translate ",
    )
    return lowered.startswith(prefixes)


def _command_intent(objective: str) -> str:
    if _is_question(objective):
        return "none"
    if (
        COMMAND_EXECUTION_INTENT.search(objective)
        or BUILD_INTENT.search(objective)
        or TEST_EXPECTATION_INTENT.search(objective)
        or TOOL_CHECK_INTENT.search(objective)
    ):
        return "strong"
    if AMBIGUOUS_COMMAND_INTENT.search(objective):
        return "ambiguous"
    return "none"


def _result_summary_verdict(output: str) -> bool | None:
    matches = RESULT_SUMMARY_PATTERN.findall(output)
    if not matches:
        return None
    summary = matches[-1].strip().lower()
    tally = FAILED_TALLY_PATTERN.search(summary)
    if tally is not None:
        return int(tally.group(1)) == 0
    if re.search(r"\b(?:failed|timed\s+out|unavailable|error)\b", summary):
        return False
    if re.search(r"\b(?:passed|ok|success)\b", summary):
        return True
    return None


def _check_line_verdict(output: str) -> bool | None:
    matches = CHECK_STATUS_PATTERN.findall(output)
    if not matches:
        return None
    return all(status.strip().lower() == "passed" for _, _, status in matches)


def _command_output_failure(output: str) -> tuple[str, str] | None:
    for match in EXIT_STATUS_PATTERN.finditer(output):
        if int(match.group(1)) != 0:
            return (
                f"command exited with non-zero status {match.group(1)}",
                f"exit_status:{match.group(1)}",
            )
    for match in FAILED_EXIT_PATTERN.finditer(output):
        code = int(match.group(1))
        if code != 0:
            return (
                f"command reported a failed check with exit status {code}",
                f"exit_status:{code}",
            )
    summary = _result_summary_verdict(output)
    if summary is False:
        return ("command summary line reports failures", "command_summary_failed")
    if summary is True:
        return None
    if _check_line_verdict(output) is False:
        return ("one or more command checks failed", "command_check_failed")
    tally = FAILED_TALLY_PATTERN.search(output)
    if tally is not None and int(tally.group(1)) > 0:
        return (
            f"command output reports {tally.group(1)} failed tests",
            f"command_failed_count:{tally.group(1)}",
        )
    if re.search(r"\bfailed\b", output, re.IGNORECASE) and not _has_success_indicator(output):
        return (
            "command output reports failures without a passing summary",
            "command_failed_output",
        )
    return None


def _normalize_mention(value: str) -> str:
    return value.strip().strip("`\"'").casefold()


SUCCESS_INDICATOR_PATTERN = re.compile(
    r"\b(?:"
    r"\d+\s+passed|"
    r"passed|"
    r"succeeded|"
    r"0\s+failed|"
    r"no\s+errors?|"
    r"exit\s*_?\s*(?:code|status)\s*[:=]?\s*0"
    r")\b",
    re.IGNORECASE,
)


def _missing_step_requirements(success_criteria: str, output: str) -> list[str]:
    bounded = success_criteria[:600]
    quoted = [item.strip() for item in QUOTED_PATH_PATTERN.findall(bounded)]
    files = [item for item in FILE_EXTENSION_PATTERN.findall(bounded)]
    requirements = list(dict.fromkeys(item for item in quoted + files if 3 <= len(item) <= 200))
    if not requirements:
        return []
    lowered_output = output.lower()
    present = [item for item in requirements if item.lower() in lowered_output]
    if present:
        return []
    return requirements


def _has_success_indicator(output: str) -> bool:
    return SUCCESS_INDICATOR_PATTERN.search(output) is not None

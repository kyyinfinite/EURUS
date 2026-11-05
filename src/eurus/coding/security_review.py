from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from pathlib import Path

from eurus.multiagent.contracts import BLOCKING_SEVERITIES, ReviewFinding

MAX_FILES = 200
MAX_FILE_BYTES = 400_000
MAX_FINDINGS = 100

_SEVERITY_ORDER = {"blocker": 0, "critical": 1, "major": 2, "minor": 3, "info": 4}


@dataclass(frozen=True, slots=True)
class SecurityFinding:
    severity: str
    title: str
    location: str
    evidence: str
    remediation: str

    def to_review_finding(self) -> ReviewFinding:
        return ReviewFinding(
            finding_id=uuid.uuid4().hex,
            severity=self.severity,
            category="security",
            location=self.location[:500],
            evidence=self.evidence[:2000],
            suggested_remediation=self.remediation[:2000],
        )


@dataclass(frozen=True, slots=True)
class SecurityReviewReport:
    findings: tuple[SecurityFinding, ...] = ()
    files_scanned: int = 0
    bytes_scanned: int = 0
    truncated: bool = False

    @property
    def has_blocking(self) -> bool:
        return any(finding.severity in BLOCKING_SEVERITIES for finding in self.findings)

    @property
    def summary(self) -> str:
        counts = {level: 0 for level in _SEVERITY_ORDER}
        for finding in self.findings:
            counts[finding.severity] += 1
        rendered = ", ".join(
            f"{level}={counts[level]}" for level in _SEVERITY_ORDER if counts[level]
        )
        detail = rendered or "no findings"
        return f"scanned {self.files_scanned} file(s): {detail}"

    def to_review_findings(self) -> tuple[ReviewFinding, ...]:
        return tuple(finding.to_review_finding() for finding in self.findings)


@dataclass(frozen=True, slots=True)
class _Rule:
    pattern: re.Pattern[str]
    severity: str
    title: str
    remediation: str
    line_offset: int = 0


_RULES: tuple[_Rule, ...] = (
    _Rule(
        pattern=re.compile(
            r"\b(api[_-]?key|secret|password|passwd|token|access[_-]?key)\b"
            r"\s*[=:]\s*[\"'][^\"']{8,}[\"']",
            re.IGNORECASE,
        ),
        severity="blocker",
        title="possible hardcoded credential",
        remediation="move the value into an environment variable or secret store",
    ),
    _Rule(
        pattern=re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
        severity="blocker",
        title="AWS access key id in source",
        remediation="revoke the key and load it from the environment instead",
    ),
    _Rule(
        pattern=re.compile(r"-----BEGIN (RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----"),
        severity="blocker",
        title="private key material committed",
        remediation="remove the key from source and rotate it",
    ),
    _Rule(
        pattern=re.compile(r"verify\s*=\s*False"),
        severity="critical",
        title="TLS certificate verification disabled",
        remediation="keep certificate verification enabled or pin an explicit CA",
    ),
    _Rule(
        pattern=re.compile(r"shell\s*=\s*True"),
        severity="critical",
        title="subprocess with shell=True",
        remediation="pass an argument list with shell=False",
    ),
    _Rule(
        pattern=re.compile(r"\bos\.system\s*\(|\bos\.popen\s*\("),
        severity="critical",
        title="OS command execution via os.system/os.popen",
        remediation="use subprocess.run with an argument list",
    ),
    _Rule(
        pattern=re.compile(r"\b(eval|exec)\s*\(", re.ASCII),
        severity="major",
        title="dynamic code execution (eval/exec)",
        remediation="replace with explicit parsing or a safe evaluator",
    ),
    _Rule(
        pattern=re.compile(
            r"(?:SELECT|INSERT|UPDATE|DELETE)\b[^\n]*(?:%s|\{|\+|\.format\s*\()",
            re.IGNORECASE,
        ),
        severity="major",
        title="possible SQL built by string concatenation",
        remediation="use parameterized queries",
    ),
    _Rule(
        pattern=re.compile(r"yaml\.load\s*\((?![^)]*Loader\s*=)"),
        severity="major",
        title="yaml.load without an explicit safe Loader",
        remediation="use yaml.safe_load",
    ),
    _Rule(
        pattern=re.compile(r"\bpickle\.loads?\s*\("),
        severity="minor",
        title="pickle deserialization",
        remediation="prefer a data format with an explicit schema; unpickle only trusted data",
    ),
)


def review_text(content: str, *, location: str) -> SecurityReviewReport:
    findings: list[SecurityFinding] = []
    if not content.strip():
        return SecurityReviewReport()
    for rule in _RULES:
        match = rule.pattern.search(content)
        if match is None:
            continue
        line = content.count("\n", 0, match.start()) + 1
        snippet = content[match.start() : match.end()]
        if _looks_like_secret(rule.title) and _is_example_value(snippet):
            continue
        findings.append(
            SecurityFinding(
                severity=rule.severity,
                title=rule.title,
                location=f"{location}:{line}",
                evidence=snippet[:200],
                remediation=rule.remediation,
            )
        )
        if len(findings) >= MAX_FINDINGS:
            break
    return SecurityReviewReport(findings=tuple(_ordered(findings)))


def review_source_files(paths: list[Path] | tuple[Path, ...]) -> SecurityReviewReport:
    findings: list[SecurityFinding] = []
    scanned = 0
    total_bytes = 0
    truncated = False
    for path in paths[:MAX_FILES]:
        try:
            if not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
                truncated = truncated or path.is_file()
                continue
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        scanned += 1
        total_bytes += len(content)
        findings.extend(review_text(content, location=path.as_posix()).findings)
        if len(findings) >= MAX_FINDINGS:
            truncated = True
            break
    return SecurityReviewReport(
        findings=tuple(_ordered(findings[:MAX_FINDINGS])),
        files_scanned=scanned,
        bytes_scanned=total_bytes,
        truncated=truncated,
    )


def review_workspace(workspace: Path | str) -> SecurityReviewReport:
    base = Path(workspace).expanduser().resolve()
    if not base.is_dir():
        raise ValueError("workspace must be an existing directory")
    candidates: list[Path] = []
    for pattern in ("*.py", "*.json", "*.toml", "*.yaml", "*.yml", "*.cfg", "*.ini"):
        candidates.extend(path for path in base.rglob(pattern) if _inside(base, path))
    candidates.sort(key=lambda item: item.as_posix())
    return review_source_files(candidates)


def review_diff(diff: str, *, source_name: str = "patch") -> SecurityReviewReport:
    findings: list[SecurityFinding] = []
    current_file = source_name
    added_lines: list[tuple[int, str]] = []
    line_no = 0
    for raw in diff.splitlines():
        if raw.startswith("+++ "):
            current_file = raw[4:].strip() or current_file
            current_file = current_file.replace("b/", "", 1)
            line_no = 0
            continue
        if raw.startswith(("--- ", "diff ", "index ", "@@")):
            continue
        line_no += 1
        if raw.startswith("+"):
            added_lines.append((line_no, raw[1:]))
    content = "\n".join(text for _, text in added_lines)
    if content.strip():
        for finding in review_text(content, location=current_file).findings:
            added_line = _find_added_line(added_lines, finding.evidence)
            location = f"{current_file}:{added_line}" if added_line else current_file
            findings.append(
                SecurityFinding(
                    severity=finding.severity,
                    title=finding.title,
                    location=location,
                    evidence=finding.evidence,
                    remediation=finding.remediation,
                )
            )
    return SecurityReviewReport(findings=tuple(_ordered(findings[:MAX_FINDINGS])))


def _find_added_line(added_lines: list[tuple[int, str]], evidence: str) -> int:
    needle = evidence.strip()[:80]
    for line_no, text in added_lines:
        if needle and needle in text:
            return line_no
    return 0


def _inside(base: Path, path: Path) -> bool:
    try:
        path.resolve().relative_to(base)
    except (OSError, ValueError):
        return False
    return path.is_file()


def _ordered(findings: list[SecurityFinding]) -> list[SecurityFinding]:
    return sorted(findings, key=lambda item: (_SEVERITY_ORDER.get(item.severity, 9), item.location))


def _looks_like_secret(title: str) -> bool:
    return "credential" in title or "key" in title


def _is_example_value(snippet: str) -> bool:
    lowered = snippet.lower()
    placeholders = (
        "example",
        "dummy",
        "placeholder",
        "changeme",
        "change-me",
        "your-",
        "your_",
        "xxx",
        "test_key",
        "<",
        "${",
        "{{",
    )
    return any(marker in lowered for marker in placeholders)

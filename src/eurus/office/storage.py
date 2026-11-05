from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from eurus import paths
from eurus.office.errors import OfficeValidationError
from eurus.office.models.task import OfficeBrief, OfficeJobState, OfficeTask, validate_job_id

JOB_ID_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789"
MAX_JOB_AGE_DAYS = 30
MAX_STATE_BYTES = 2_000_000


def office_home(environ: dict[str, str] | None = None) -> Path:
    return paths.home_manager(environ).root / "office"


def jobs_root(environ: dict[str, str] | None = None) -> Path:
    return office_home(environ) / "jobs"


def templates_root(environ: dict[str, str] | None = None) -> Path:
    return office_home(environ) / "templates"


def create_job_id() -> str:
    import secrets

    stamp = time.strftime("%Y%m%d", time.gmtime())
    random_part = "".join(secrets.choice(JOB_ID_ALPHABET) for _ in range(10))
    return f"job-{stamp}-{random_part}"


def job_directory(job_id: str, environ: dict[str, str] | None = None) -> Path:
    validate_job_id(job_id)
    root = jobs_root(environ)
    candidate = root / job_id
    resolved = candidate.resolve()
    root_resolved = root.resolve()
    if root_resolved != resolved and root_resolved not in resolved.parents:
        raise OfficeValidationError("job directory escapes the office storage root")
    return candidate


@dataclass(frozen=True, slots=True)
class JobWorkspace:
    root: Path

    @classmethod
    def create(cls, job_id: str, environ: dict[str, str] | None = None) -> JobWorkspace:
        root = job_directory(job_id, environ)
        workspace = cls(root=root)
        workspace.ensure_layout()
        return workspace

    @classmethod
    def open(cls, job_id: str, environ: dict[str, str] | None = None) -> JobWorkspace:
        root = job_directory(job_id, environ)
        if not root.is_dir():
            raise OfficeValidationError(f"job workspace does not exist: {job_id}")
        return cls(root=root)

    def ensure_layout(self) -> None:
        for directory in (
            self.agents_dir,
            self.source_dir,
            self.staging_dir,
            self.renders_dir,
            self.reviews_dir,
            self.final_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)

    @property
    def agents_dir(self) -> Path:
        return self.root / "agents"

    @property
    def source_dir(self) -> Path:
        return self.root / "source"

    @property
    def staging_dir(self) -> Path:
        return self.root / "staging"

    @property
    def renders_dir(self) -> Path:
        return self.root / "renders"

    @property
    def reviews_dir(self) -> Path:
        return self.root / "reviews"

    @property
    def final_dir(self) -> Path:
        return self.root / "final"

    @property
    def task_file(self) -> Path:
        return self.root / "task.json"

    @property
    def plan_file(self) -> Path:
        return self.root / "plan.json"

    @property
    def state_file(self) -> Path:
        return self.root / "state.json"

    @property
    def events_file(self) -> Path:
        return self.root / "events.jsonl"

    def contains(self, target: Path) -> bool:
        resolved = target.resolve()
        root_resolved = self.root.resolve()
        return root_resolved == resolved or root_resolved in resolved.parents

    def staging_path(self, name: str) -> Path:
        candidate = (self.staging_dir / name).resolve()
        staging_resolved = self.staging_dir.resolve()
        if staging_resolved != candidate and staging_resolved not in candidate.parents:
            raise OfficeValidationError("staging path escapes the job staging area")
        candidate.parent.mkdir(parents=True, exist_ok=True)
        return candidate

    def final_path(self, name: str) -> Path:
        candidate = (self.final_dir / name).resolve()
        final_resolved = self.final_dir.resolve()
        if final_resolved != candidate and final_resolved not in candidate.parents:
            raise OfficeValidationError("final path escapes the job final area")
        candidate.parent.mkdir(parents=True, exist_ok=True)
        return candidate

    def save_task(self, task: OfficeTask) -> None:
        self.write_json(self.task_file, task.to_dict())

    def load_task(self) -> OfficeTask:
        return OfficeTask.from_dict(self.read_json(self.task_file))

    def save_plan(self, plan: dict[str, Any]) -> None:
        self.write_json(self.plan_file, plan)

    def load_plan(self) -> dict[str, Any]:
        return self.read_json(self.plan_file)

    def save_state(self, state: OfficeJobState) -> None:
        self.write_json(self.state_file, state.to_dict())

    def load_state(self) -> OfficeJobState:
        return OfficeJobState.from_dict(self.read_json(self.state_file))

    def append_event(self, event_type: str, payload: dict[str, Any]) -> None:
        record = {
            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "event": event_type,
            **payload,
        }
        line = json.dumps(record, ensure_ascii=False)
        self.events_file.parent.mkdir(parents=True, exist_ok=True)
        with self.events_file.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    def read_events(self) -> list[dict[str, Any]]:
        if not self.events_file.is_file():
            return []
        records: list[dict[str, Any]] = []
        for line in self.events_file.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return records

    def write_json(self, target: Path, payload: dict[str, Any]) -> None:
        if not self.contains(target):
            raise OfficeValidationError("write target escapes the job workspace")
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(target.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(target)

    def read_json(self, target: Path) -> dict[str, Any]:
        if not target.is_file():
            raise OfficeValidationError(f"job file does not exist: {target.name}")
        if target.stat().st_size > MAX_STATE_BYTES:
            raise OfficeValidationError(f"job file exceeds {MAX_STATE_BYTES} bytes")
        try:
            loaded: dict[str, Any] = json.loads(target.read_text(encoding="utf-8"))
            return loaded
        except json.JSONDecodeError as exc:
            raise OfficeValidationError(
                f"job file is not valid JSON: {target.name}: {exc}"
            ) from exc

    def write_text(self, target: Path, content: str) -> None:
        if not self.contains(target):
            raise OfficeValidationError("write target escapes the job workspace")
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(target.suffix + ".tmp")
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(target)

    def copy_source_files(self, source_paths: tuple[str, ...], workspace_root: Path) -> list[Path]:
        imported: list[Path] = []
        for raw in source_paths:
            candidate = Path(raw)
            resolved = candidate if candidate.is_absolute() else (workspace_root / candidate)
            try:
                resolved = resolved.resolve()
            except OSError:
                continue
            if not resolved.is_file():
                continue
            if resolved.is_relative_to(self.root.resolve()):
                continue
            target = self.source_dir / resolved.name
            counter = 1
            while target.exists():
                target = self.source_dir / f"{resolved.stem}-{counter}{resolved.suffix}"
                counter += 1
            try:
                target.write_bytes(resolved.read_bytes())
            except OSError:
                continue
            imported.append(target)
        return imported


def list_jobs(environ: dict[str, str] | None = None) -> list[str]:
    root = jobs_root(environ)
    if not root.is_dir():
        return []
    return sorted(
        entry.name
        for entry in root.iterdir()
        if entry.is_dir() and (entry / "state.json").is_file()
    )


def load_job_state(job_id: str, environ: dict[str, str] | None = None) -> OfficeJobState | None:
    try:
        workspace = JobWorkspace.open(job_id, environ)
    except OfficeValidationError:
        return None
    if not workspace.state_file.is_file():
        return None
    try:
        return workspace.load_state()
    except OfficeValidationError:
        return None


def prune_jobs(
    *,
    keep_days: int = MAX_JOB_AGE_DAYS,
    keep_failed_days: int = 7,
    environ: dict[str, str] | None = None,
) -> list[str]:
    removed: list[str] = []
    root = jobs_root(environ)
    if not root.is_dir():
        return removed
    now = time.time()
    for entry in root.iterdir():
        if not entry.is_dir():
            continue
        state_file = entry / "state.json"
        if not state_file.is_file():
            continue
        try:
            state = OfficeJobState.from_dict(json.loads(state_file.read_text(encoding="utf-8")))
        except (OSError, ValueError, KeyError):
            continue
        terminal = state.stage.value in {
            "completed",
            "failed",
            "cancelled",
            "quality_limit_reached",
        }
        if not terminal:
            continue
        try:
            age_days = (now - entry.stat().st_mtime) / 86_400
        except OSError:
            continue
        limit = keep_days if state.stage.value == "completed" else keep_failed_days
        if age_days > limit:
            import shutil

            shutil.rmtree(entry, ignore_errors=True)
            removed.append(entry.name)
    return removed


def brief_from_payload(payload: dict[str, Any]) -> OfficeBrief:
    from eurus.office.models.task import format_from_label

    return OfficeBrief(
        objective=str(payload.get("objective", "")),
        format=format_from_label(payload.get("format", "")),
        audience=str(payload.get("audience", "")),
        tone=str(payload.get("tone", "")),
        language=str(payload.get("language", "en")),
        style_hint=str(payload.get("style", "")),
        constraints=tuple(str(item) for item in payload.get("constraints", ())),
        source_paths=tuple(str(item) for item in payload.get("source_paths", ())),
        output_path=str(payload.get("output_path", "")),
        include_toc=bool(payload.get("include_toc", True)),
        external_research=bool(payload.get("external_research", False)),
        deadline_seconds=(
            float(payload["deadline_seconds"])
            if payload.get("deadline_seconds") is not None
            else None
        ),
    )

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from eurus.storage.jobs import Job, JobStore

GLOBAL_LIMIT_MIN = 1
GLOBAL_LIMIT_MAX = 256


@dataclass(frozen=True, slots=True)
class ConcurrencySnapshot:
    running_global: int
    running_by_owner: dict[str, int]
    global_limit: int
    per_owner_limit: int


class ConcurrencyGovernor:
    def __init__(
        self,
        store: JobStore,
        *,
        global_limit: int = 8,
        per_owner_limit: int = 4,
        owner_id: str | None = None,
    ) -> None:
        if not GLOBAL_LIMIT_MIN <= global_limit <= GLOBAL_LIMIT_MAX:
            raise ValueError("global_limit must be between 1 and 256")
        if not GLOBAL_LIMIT_MIN <= per_owner_limit <= GLOBAL_LIMIT_MAX:
            raise ValueError("per_owner_limit must be between 1 and 256")
        self.store = store
        self.global_limit = global_limit
        self.per_owner_limit = per_owner_limit
        self.owner_id = owner_id

    def snapshot(self, *, owner_id: str | None = None) -> ConcurrencySnapshot:
        running = self.store.list_jobs(statuses=("running",), owner_id=owner_id, limit=1000)
        by_owner: dict[str, int] = {}
        for job in running:
            by_owner[job.owner_id] = by_owner.get(job.owner_id, 0) + 1
        return ConcurrencySnapshot(
            running_global=len(running),
            running_by_owner=by_owner,
            global_limit=self.global_limit,
            per_owner_limit=self.per_owner_limit,
        )

    def within_limits(self, job: Job) -> bool:
        snapshot = self.snapshot()
        if snapshot.running_global >= self.global_limit:
            return False
        return snapshot.running_by_owner.get(job.owner_id, 0) < self.per_owner_limit

    def claim(
        self,
        worker_id: str,
        *,
        lease_seconds: int = 60,
        now: datetime | None = None,
    ) -> Job | None:
        return self.store.claim(
            worker_id,
            lease_seconds=lease_seconds,
            now=now,
            global_limit=self.global_limit,
            owner_limit=self.per_owner_limit,
            owner_id=self.owner_id,
        )

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from eurus.coding.worktree import IsolatedWorktree, WorktreeError, create_worktree

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class WorkspaceAllocation:
    assignment_id: str
    owner_id: str
    worktree: IsolatedWorktree

    @property
    def base_commit(self) -> str:
        return self.worktree.base_commit

    @property
    def parent_dirty(self) -> bool:
        return self.worktree.parent_dirty


class WorkspaceManager:
    def __init__(self, workspace_root: Path | str) -> None:
        self.root = Path(workspace_root).expanduser().resolve()
        self._allocations: dict[str, WorkspaceAllocation] = {}

    def allocate(self, assignment_id: str, *, owner_id: str) -> WorkspaceAllocation:
        if not assignment_id.strip() or not owner_id.strip():
            raise WorktreeError("assignment_id and owner_id are required")
        if assignment_id in self._allocations:
            return self._allocations[assignment_id]
        worktree = create_worktree(self.root, prefix=f"eurus-ws-{assignment_id[:8]}")
        allocation = WorkspaceAllocation(
            assignment_id=assignment_id, owner_id=owner_id, worktree=worktree
        )
        if worktree.parent_dirty:
            log.warning(
                "assignment %s: parent workspace has %d uncommitted change(s); the "
                "worktree is based on committed HEAD %s only",
                assignment_id,
                len(worktree.parent_dirty_paths),
                worktree.base_commit[:12],
            )
        self._allocations[assignment_id] = allocation
        return allocation

    def get(self, assignment_id: str) -> WorkspaceAllocation | None:
        return self._allocations.get(assignment_id)

    def release(self, assignment_id: str) -> str:
        allocation = self._allocations.pop(assignment_id, None)
        if allocation is None:
            return "not_allocated"
        return allocation.worktree.cleanup()

    def release_all(self) -> dict[str, str]:
        results: dict[str, str] = {}
        for assignment_id in list(self._allocations):
            results[assignment_id] = self.release(assignment_id)
        return results

    def active(self) -> tuple[str, ...]:
        return tuple(sorted(self._allocations))

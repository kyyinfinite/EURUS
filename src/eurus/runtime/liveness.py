from __future__ import annotations

import logging

from eurus.runtime.models import RunStatus
from eurus.runtime.state_machine import transition_run
from eurus.storage.runs import ConcurrentRunUpdate, RunStore

log = logging.getLogger(__name__)

DEFAULT_STALE_RUN_SECONDS = 900.0
HEARTBEAT_EXPIRED_STOP_REASON = "heartbeat_expired"


def reap_stale_runs(
    run_store: RunStore,
    *,
    older_than_seconds: float = DEFAULT_STALE_RUN_SECONDS,
    limit: int = 50,
) -> list[str]:
    if older_than_seconds <= 0:
        raise ValueError("older_than_seconds must be positive")
    reaped: list[str] = []
    for run in run_store.stale_running(older_than_seconds=older_than_seconds, limit=limit):
        try:
            transition_run(run, RunStatus.INTERRUPTED, stop_reason=HEARTBEAT_EXPIRED_STOP_REASON)
            run_store.save(run, expected_status_version=run.status_version - 1)
        except ConcurrentRunUpdate:
            log.info("run %s changed while reaping; skipping", run.run_id)
            continue
        except Exception:
            log.exception("failed to reap stale run %s", run.run_id)
            continue
        reaped.append(run.run_id)
        log.warning(
            "run %s interrupted: no heartbeat for more than %.0fs",
            run.run_id,
            older_than_seconds,
        )
    return reaped

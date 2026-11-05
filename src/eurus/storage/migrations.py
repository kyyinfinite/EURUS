from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

LATEST_SCHEMA_VERSION = 15

_MIGRATIONS: dict[int, tuple[str, ...]] = {
    1: (
        """CREATE TABLE eurus_runs (
            run_id TEXT PRIMARY KEY,
            owner_id TEXT NOT NULL,
            objective TEXT NOT NULL,
            parent_run_id TEXT,
            session_id TEXT,
            status TEXT NOT NULL,
            stop_reason TEXT,
            budget_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            started_at TEXT,
            finished_at TEXT,
            schema_version INTEGER NOT NULL,
            checkpoint_version INTEGER NOT NULL,
            result_ref TEXT,
            status_version INTEGER NOT NULL
        )""",
        "CREATE INDEX eurus_runs_owner_created ON eurus_runs(owner_id, created_at DESC)",
        "CREATE INDEX eurus_runs_status_updated ON eurus_runs(status, updated_at)",
        """CREATE TABLE eurus_run_checkpoints (
            run_id TEXT NOT NULL REFERENCES eurus_runs(run_id) ON DELETE CASCADE,
            checkpoint_version INTEGER NOT NULL,
            checkpoint_type TEXT NOT NULL,
            state_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (run_id, checkpoint_version)
        )""",
        """CREATE TABLE eurus_runtime_events (
            event_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL REFERENCES eurus_runs(run_id) ON DELETE CASCADE,
            sequence_no INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            payload_version INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            step_id TEXT,
            attempt_id TEXT,
            parent_run_id TEXT,
            UNIQUE (run_id, sequence_no)
        )""",
        "CREATE INDEX eurus_runtime_events_run_sequence "
        "ON eurus_runtime_events(run_id, sequence_no)",
        "CREATE INDEX eurus_runtime_events_type_created "
        "ON eurus_runtime_events(event_type, created_at)",
    ),
    2: (
        """CREATE TABLE eurus_jobs (
            job_id TEXT PRIMARY KEY,
            schedule_id TEXT,
            scheduled_at TEXT,
            owner_id TEXT NOT NULL,
            objective TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            status TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            max_attempts INTEGER NOT NULL DEFAULT 1,
            available_at TEXT NOT NULL,
            worker_id TEXT,
            lease_expires_at TEXT,
            lease_version INTEGER NOT NULL DEFAULT 0,
            side_effect_started INTEGER NOT NULL DEFAULT 0,
            cancel_requested INTEGER NOT NULL DEFAULT 0,
            result TEXT NOT NULL DEFAULT '',
            error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(schedule_id, scheduled_at)
        )""",
        "CREATE INDEX eurus_jobs_claimable ON eurus_jobs(status, available_at, created_at)",
        "CREATE INDEX eurus_jobs_lease ON eurus_jobs(status, lease_expires_at)",
        "CREATE INDEX eurus_jobs_schedule ON eurus_jobs(schedule_id, created_at DESC)",
    ),
    3: (
        """CREATE TABLE eurus_run_controls (
            run_id TEXT PRIMARY KEY REFERENCES eurus_runs(run_id) ON DELETE CASCADE,
            cancel_requested INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL
        )""",
    ),
    4: (
        """CREATE TABLE eurus_tool_attempts (
            attempt_id TEXT PRIMARY KEY,
            run_id TEXT,
            tool_name TEXT NOT NULL,
            call_key TEXT NOT NULL,
            status TEXT NOT NULL,
            is_error INTEGER NOT NULL DEFAULT 0,
            uncertain INTEGER NOT NULL DEFAULT 0,
            risk_level TEXT NOT NULL DEFAULT 'low',
            started_at TEXT,
            finished_at TEXT,
            detail TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        )""",
        "CREATE INDEX eurus_tool_attempts_run ON eurus_tool_attempts(run_id, created_at)",
        "CREATE INDEX eurus_tool_attempts_uncertain ON eurus_tool_attempts(uncertain, call_key)",
    ),
    5: (
        "ALTER TABLE eurus_run_checkpoints ADD COLUMN iteration INTEGER",
        "ALTER TABLE eurus_run_checkpoints ADD COLUMN side_effect_status TEXT",
        "CREATE INDEX eurus_run_checkpoints_lookup "
        "ON eurus_run_checkpoints(run_id, checkpoint_type, checkpoint_version)",
    ),
    6: (
        "ALTER TABLE eurus_jobs ADD COLUMN priority INTEGER NOT NULL DEFAULT 5",
        "ALTER TABLE eurus_jobs ADD COLUMN source TEXT NOT NULL DEFAULT 'schedule'",
        "ALTER TABLE eurus_jobs ADD COLUMN correlation_id TEXT NOT NULL DEFAULT ''",
        "CREATE INDEX eurus_jobs_priority ON eurus_jobs(status, priority, available_at)",
        """CREATE TABLE eurus_dead_letters (
            dead_letter_id TEXT PRIMARY KEY,
            job_id TEXT NOT NULL,
            owner_id TEXT NOT NULL,
            objective TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'schedule',
            reason TEXT NOT NULL,
            detail TEXT NOT NULL DEFAULT '',
            attempts INTEGER NOT NULL DEFAULT 0,
            payload_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            resolved_at TEXT,
            resolution TEXT
        )""",
        "CREATE INDEX eurus_dead_letters_owner ON eurus_dead_letters(owner_id, created_at DESC)",
        """CREATE TABLE eurus_goals (
            goal_id TEXT PRIMARY KEY,
            owner_id TEXT NOT NULL,
            objective TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            constraints_json TEXT NOT NULL DEFAULT '[]',
            priority INTEGER NOT NULL DEFAULT 5,
            deadline_at TEXT,
            status TEXT NOT NULL,
            status_version INTEGER NOT NULL DEFAULT 0,
            progress REAL NOT NULL DEFAULT 0.0,
            success_criteria_json TEXT NOT NULL DEFAULT '[]',
            plan_json TEXT NOT NULL DEFAULT '{}',
            parent_goal_id TEXT,
            source TEXT NOT NULL DEFAULT 'user',
            completion_evidence_json TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            completed_at TEXT
        )""",
        "CREATE INDEX eurus_goals_owner ON eurus_goals(owner_id, created_at DESC)",
        "CREATE INDEX eurus_goals_status ON eurus_goals(status, priority)",
        """CREATE TABLE eurus_goal_tasks (
            task_id TEXT PRIMARY KEY,
            goal_id TEXT NOT NULL REFERENCES eurus_goals(goal_id) ON DELETE CASCADE,
            title TEXT NOT NULL,
            detail TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending',
            depends_on_json TEXT NOT NULL DEFAULT '[]',
            success_criteria TEXT NOT NULL DEFAULT '',
            milestone_id TEXT,
            schedule_id TEXT,
            job_id TEXT,
            run_id TEXT,
            output TEXT NOT NULL DEFAULT '',
            error TEXT,
            attempts INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )""",
        "CREATE INDEX eurus_goal_tasks_goal ON eurus_goal_tasks(goal_id, created_at)",
        "CREATE INDEX eurus_goal_tasks_status ON eurus_goal_tasks(status)",
        """CREATE TABLE eurus_goal_milestones (
            milestone_id TEXT PRIMARY KEY,
            goal_id TEXT NOT NULL REFERENCES eurus_goals(goal_id) ON DELETE CASCADE,
            title TEXT NOT NULL,
            criteria TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending',
            target_progress REAL NOT NULL DEFAULT 1.0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )""",
        "CREATE INDEX eurus_goal_milestones_goal ON eurus_goal_milestones(goal_id)",
        """CREATE TABLE eurus_workflow_definitions (
            workflow_id TEXT NOT NULL,
            version INTEGER NOT NULL,
            name TEXT NOT NULL,
            definition_json TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            owner_id TEXT NOT NULL DEFAULT 'local',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (workflow_id, version)
        )""",
        """CREATE TABLE eurus_workflow_instances (
            instance_id TEXT PRIMARY KEY,
            workflow_id TEXT NOT NULL,
            version INTEGER NOT NULL,
            owner_id TEXT NOT NULL,
            status TEXT NOT NULL,
            context_json TEXT NOT NULL DEFAULT '{}',
            goal_id TEXT,
            parent_instance_id TEXT,
            error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            started_at TEXT,
            finished_at TEXT
        )""",
        "CREATE INDEX eurus_workflow_instances_status "
        "ON eurus_workflow_instances(status, updated_at)",
        "CREATE INDEX eurus_workflow_instances_workflow "
        "ON eurus_workflow_instances(workflow_id, created_at DESC)",
        """CREATE TABLE eurus_workflow_node_states (
            instance_id TEXT NOT NULL REFERENCES eurus_workflow_instances(instance_id)
                ON DELETE CASCADE,
            node_id TEXT NOT NULL,
            node_type TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0,
            output TEXT NOT NULL DEFAULT '',
            error TEXT,
            job_id TEXT,
            resume_at TEXT,
            started_at TEXT,
            finished_at TEXT,
            PRIMARY KEY (instance_id, node_id)
        )""",
        "CREATE INDEX eurus_workflow_node_states_status "
        "ON eurus_workflow_node_states(status, instance_id)",
        """CREATE TABLE eurus_workflow_approvals (
            approval_id TEXT PRIMARY KEY,
            instance_id TEXT NOT NULL,
            node_id TEXT NOT NULL,
            requested_at TEXT NOT NULL,
            decided_at TEXT,
            decided_by TEXT,
            decision TEXT,
            note TEXT NOT NULL DEFAULT ''
        )""",
        "CREATE INDEX eurus_workflow_approvals_instance "
        "ON eurus_workflow_approvals(instance_id, requested_at)",
        """CREATE TABLE eurus_agent_specialists (
            specialist_id TEXT PRIMARY KEY,
            role TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            capabilities_json TEXT NOT NULL DEFAULT '[]',
            allowed_tools_json TEXT NOT NULL DEFAULT '[]',
            model_policy TEXT NOT NULL DEFAULT 'default',
            max_concurrent INTEGER NOT NULL DEFAULT 2,
            delegation_allowed INTEGER NOT NULL DEFAULT 0,
            max_iterations INTEGER NOT NULL DEFAULT 20,
            max_runtime_seconds INTEGER NOT NULL DEFAULT 600,
            priority INTEGER NOT NULL DEFAULT 5,
            enabled INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(role)
        )""",
        """CREATE TABLE eurus_agent_assignments (
            assignment_id TEXT PRIMARY KEY,
            parent_run_id TEXT,
            goal_id TEXT,
            workflow_instance_id TEXT,
            node_id TEXT,
            specialist_id TEXT NOT NULL,
            role TEXT NOT NULL,
            objective TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            child_run_id TEXT,
            job_id TEXT,
            result TEXT NOT NULL DEFAULT '',
            error TEXT,
            attempts INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            started_at TEXT,
            finished_at TEXT,
            updated_at TEXT NOT NULL
        )""",
        "CREATE INDEX eurus_agent_assignments_parent "
        "ON eurus_agent_assignments(parent_run_id, created_at DESC)",
        "CREATE INDEX eurus_agent_assignments_status "
        "ON eurus_agent_assignments(status, updated_at)",
        """CREATE TABLE eurus_agent_heartbeats (
            assignment_id TEXT PRIMARY KEY,
            last_beat TEXT NOT NULL,
            detail TEXT NOT NULL DEFAULT ''
        )""",
        """CREATE TABLE eurus_domain_events (
            event_id TEXT PRIMARY KEY,
            event_type TEXT NOT NULL,
            subject_type TEXT NOT NULL DEFAULT '',
            subject_id TEXT NOT NULL DEFAULT '',
            payload_json TEXT NOT NULL DEFAULT '{}',
            correlation_id TEXT NOT NULL DEFAULT '',
            causation_id TEXT NOT NULL DEFAULT '',
            dedup_key TEXT,
            occurred_at TEXT NOT NULL,
            created_at TEXT NOT NULL
        )""",
        "CREATE UNIQUE INDEX eurus_domain_events_dedup "
        "ON eurus_domain_events(dedup_key) WHERE dedup_key IS NOT NULL",
        "CREATE INDEX eurus_domain_events_type ON eurus_domain_events(event_type, occurred_at)",
        "CREATE INDEX eurus_domain_events_subject "
        "ON eurus_domain_events(subject_type, subject_id, occurred_at)",
        """CREATE TABLE eurus_event_subscriptions (
            subscription_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            event_types_json TEXT NOT NULL DEFAULT '[]',
            subject_type TEXT NOT NULL DEFAULT '',
            subject_id TEXT NOT NULL DEFAULT '',
            handler_kind TEXT NOT NULL DEFAULT 'submit_job',
            handler_config_json TEXT NOT NULL DEFAULT '{}',
            enabled INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )""",
        """CREATE TABLE eurus_event_deliveries (
            delivery_id TEXT PRIMARY KEY,
            event_id TEXT NOT NULL REFERENCES eurus_domain_events(event_id) ON DELETE CASCADE,
            subscription_id TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0,
            next_attempt_at TEXT NOT NULL,
            last_error TEXT,
            delivered_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(event_id, subscription_id)
        )""",
        "CREATE INDEX eurus_event_deliveries_due "
        "ON eurus_event_deliveries(status, next_attempt_at)",
        """CREATE TABLE eurus_outbox (
            outbox_id TEXT PRIMARY KEY,
            event_id TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL,
            dispatched_at TEXT
        )""",
        "CREATE INDEX eurus_outbox_status ON eurus_outbox(status, created_at)",
        """CREATE TABLE eurus_evaluations (
            evaluation_id TEXT PRIMARY KEY,
            subject_type TEXT NOT NULL,
            subject_id TEXT NOT NULL,
            dimension TEXT NOT NULL,
            score REAL NOT NULL,
            passed INTEGER NOT NULL,
            evidence_json TEXT NOT NULL DEFAULT '[]',
            evaluator TEXT NOT NULL DEFAULT 'deterministic',
            created_at TEXT NOT NULL
        )""",
        "CREATE INDEX eurus_evaluations_subject "
        "ON eurus_evaluations(subject_type, subject_id, created_at DESC)",
        """CREATE TABLE eurus_learning_provenance (
            provenance_id TEXT PRIMARY KEY,
            artifact_type TEXT NOT NULL,
            artifact_id TEXT NOT NULL,
            source_run_id TEXT,
            evaluation_id TEXT,
            summary TEXT NOT NULL DEFAULT '',
            reversed_at TEXT,
            created_at TEXT NOT NULL
        )""",
        "CREATE INDEX eurus_learning_provenance_artifact "
        "ON eurus_learning_provenance(artifact_type, artifact_id)",
    ),
    7: (
        "ALTER TABLE eurus_workflow_instances ADD COLUMN child_instance_id TEXT",
        "ALTER TABLE eurus_workflow_node_states ADD COLUMN created_at TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE eurus_workflow_node_states ADD COLUMN updated_at TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE eurus_agent_assignments ADD COLUMN owner_id TEXT NOT NULL DEFAULT 'local'",
        "ALTER TABLE eurus_agent_assignments ADD COLUMN delegation_depth "
        "INTEGER NOT NULL DEFAULT 0",
        """CREATE TABLE eurus_agent_messages (
            message_id TEXT PRIMARY KEY,
            assignment_id TEXT NOT NULL,
            sender TEXT NOT NULL,
            message_type TEXT NOT NULL,
            body TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        )""",
        "CREATE INDEX eurus_agent_messages_assignment "
        "ON eurus_agent_messages(assignment_id, created_at)",
    ),
    8: (
        """DELETE FROM eurus_dead_letters WHERE resolved_at IS NULL AND dead_letter_id IN (
            SELECT a.dead_letter_id FROM eurus_dead_letters a, eurus_dead_letters b
            WHERE a.job_id = b.job_id AND a.resolved_at IS NULL AND b.resolved_at IS NULL
              AND (a.created_at < b.created_at
                   OR (a.created_at = b.created_at AND a.dead_letter_id < b.dead_letter_id))
        )""",
        """CREATE UNIQUE INDEX eurus_dead_letters_open_job
        ON eurus_dead_letters(job_id) WHERE resolved_at IS NULL""",
        """CREATE TABLE eurus_learning_tasks (
            task_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            job_id TEXT NOT NULL,
            owner_id TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0,
            error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )""",
        "CREATE UNIQUE INDEX eurus_learning_tasks_run ON eurus_learning_tasks(run_id)",
        "CREATE INDEX eurus_learning_tasks_status ON eurus_learning_tasks(status, updated_at)",
        "CREATE INDEX eurus_agent_assignments_owner "
        "ON eurus_agent_assignments(owner_id, updated_at)",
    ),
    9: (
        "ALTER TABLE eurus_domain_events ADD COLUMN owner_id TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE eurus_event_subscriptions ADD COLUMN owner_id TEXT NOT NULL DEFAULT ''",
        "DROP INDEX eurus_domain_events_dedup",
        """CREATE UNIQUE INDEX eurus_domain_events_dedup
        ON eurus_domain_events(owner_id, dedup_key) WHERE dedup_key IS NOT NULL""",
        "CREATE INDEX eurus_domain_events_owner ON eurus_domain_events(owner_id, occurred_at)",
        "CREATE INDEX eurus_event_subscriptions_owner "
        "ON eurus_event_subscriptions(owner_id, enabled)",
    ),
    10: (
        "UPDATE eurus_jobs SET owner_id='eurus' WHERE owner_id<>'eurus'",
        "UPDATE eurus_goals SET owner_id='eurus' WHERE owner_id<>'eurus'",
        "UPDATE eurus_workflow_instances SET owner_id='eurus' WHERE owner_id<>'eurus'",
        "UPDATE eurus_agent_assignments SET owner_id='eurus' WHERE owner_id<>'eurus'",
        "UPDATE eurus_dead_letters SET owner_id='eurus' WHERE owner_id<>'eurus'",
        "UPDATE eurus_domain_events SET owner_id='eurus' WHERE owner_id<>'eurus'",
        "UPDATE eurus_event_subscriptions SET owner_id='eurus' WHERE owner_id<>'eurus'",
    ),
    11: ("ALTER TABLE eurus_jobs ADD COLUMN delivery_status TEXT NOT NULL DEFAULT ''",),
    12: (
        "ALTER TABLE eurus_agent_specialists ADD COLUMN risk_tier TEXT NOT NULL DEFAULT 'medium'",
        "ALTER TABLE eurus_agent_specialists ADD COLUMN profile_version INTEGER NOT NULL DEFAULT 1",
        "ALTER TABLE eurus_agent_specialists ADD COLUMN tool_policy_version "
        "INTEGER NOT NULL DEFAULT 1",
        "ALTER TABLE eurus_agent_specialists ADD COLUMN max_delegation_depth "
        "INTEGER NOT NULL DEFAULT 3",
        "ALTER TABLE eurus_agent_specialists ADD COLUMN input_schema_json TEXT",
        "ALTER TABLE eurus_agent_specialists ADD COLUMN output_schema_json TEXT",
        "ALTER TABLE eurus_agent_specialists ADD COLUMN review_policy_json TEXT",
        "ALTER TABLE eurus_agent_assignments ADD COLUMN mission_id TEXT",
        "ALTER TABLE eurus_agent_assignments ADD COLUMN task_id TEXT",
        "CREATE INDEX eurus_agent_assignments_mission "
        "ON eurus_agent_assignments(mission_id, status, updated_at)",
        """CREATE TABLE eurus_agent_missions (
            mission_id TEXT PRIMARY KEY,
            owner_id TEXT NOT NULL,
            root_run_id TEXT,
            objective TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'created',
            plan_json TEXT NOT NULL DEFAULT '{}',
            acceptance_json TEXT NOT NULL DEFAULT '[]',
            budget_json TEXT NOT NULL DEFAULT '{}',
            version INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            completed_at TEXT
        )""",
        "CREATE INDEX eurus_agent_missions_owner "
        "ON eurus_agent_missions(owner_id, status, updated_at)",
        """CREATE TABLE eurus_agent_tasks (
            task_id TEXT PRIMARY KEY,
            mission_id TEXT NOT NULL,
            parent_task_id TEXT,
            task_key TEXT NOT NULL,
            title TEXT NOT NULL DEFAULT '',
            objective TEXT NOT NULL,
            required_capabilities_json TEXT NOT NULL DEFAULT '[]',
            preferred_role TEXT,
            input_contract_json TEXT NOT NULL DEFAULT '{}',
            output_contract_json TEXT NOT NULL DEFAULT '{}',
            acceptance_json TEXT NOT NULL DEFAULT '[]',
            status TEXT NOT NULL DEFAULT 'pending',
            priority INTEGER NOT NULL DEFAULT 5,
            attempt INTEGER NOT NULL DEFAULT 0,
            assignment_id TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(mission_id, task_key)
        )""",
        "CREATE INDEX eurus_agent_tasks_mission ON eurus_agent_tasks(mission_id, status, priority)",
        """CREATE TABLE eurus_agent_task_dependencies (
            mission_id TEXT NOT NULL,
            task_id TEXT NOT NULL,
            depends_on_task_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(task_id, depends_on_task_id)
        )""",
        "CREATE INDEX eurus_agent_task_deps_task ON eurus_agent_task_dependencies(task_id)",
        "CREATE INDEX eurus_agent_task_deps_depends "
        "ON eurus_agent_task_dependencies(depends_on_task_id)",
        """CREATE TABLE eurus_agent_artifacts (
            artifact_id TEXT PRIMARY KEY,
            owner_id TEXT NOT NULL,
            mission_id TEXT NOT NULL,
            task_id TEXT,
            assignment_id TEXT,
            artifact_type TEXT NOT NULL,
            media_type TEXT NOT NULL DEFAULT 'text/plain',
            storage_key TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            size_bytes INTEGER NOT NULL,
            version INTEGER NOT NULL DEFAULT 1,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            allowed_recipients_json TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL
        )""",
        "CREATE INDEX eurus_agent_artifacts_scope "
        "ON eurus_agent_artifacts(owner_id, mission_id, task_id, created_at)",
        """CREATE TABLE eurus_agent_reviews (
            review_id TEXT PRIMARY KEY,
            owner_id TEXT NOT NULL,
            mission_id TEXT NOT NULL,
            task_id TEXT NOT NULL,
            artifact_id TEXT,
            reviewer_assignment_id TEXT,
            cycle INTEGER NOT NULL DEFAULT 1,
            status TEXT NOT NULL DEFAULT 'open',
            findings_json TEXT NOT NULL DEFAULT '[]',
            summary TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            resolved_at TEXT
        )""",
        "CREATE INDEX eurus_agent_reviews_scope "
        "ON eurus_agent_reviews(mission_id, task_id, status)",
        """CREATE TABLE eurus_agent_verifications (
            verification_id TEXT PRIMARY KEY,
            owner_id TEXT NOT NULL,
            mission_id TEXT NOT NULL,
            task_id TEXT NOT NULL,
            artifact_id TEXT,
            check_name TEXT NOT NULL,
            category TEXT NOT NULL DEFAULT 'test',
            command_json TEXT NOT NULL DEFAULT '[]',
            working_directory TEXT,
            exit_code INTEGER,
            duration_ms INTEGER,
            status TEXT NOT NULL,
            evidence_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        )""",
        "CREATE INDEX eurus_agent_verifications_scope "
        "ON eurus_agent_verifications(mission_id, task_id, status)",
    ),
    13: (
        "ALTER TABLE eurus_runs ADD COLUMN heartbeat_at TEXT",
        "CREATE INDEX eurus_runs_stale_heartbeat ON eurus_runs(status, heartbeat_at)",
    ),
    14: ("ALTER TABLE eurus_event_deliveries ADD COLUMN claim_token TEXT NOT NULL DEFAULT ''",),
    15: (
        "ALTER TABLE eurus_agent_artifacts ADD COLUMN name TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE eurus_agent_artifacts ADD COLUMN source_tool TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE eurus_agent_artifacts ADD COLUMN run_id TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE eurus_agent_artifacts ADD COLUMN expires_at TEXT",
        "CREATE INDEX eurus_agent_artifacts_run ON eurus_agent_artifacts(owner_id, run_id)",
        "CREATE INDEX eurus_agent_artifacts_recent "
        "ON eurus_agent_artifacts(owner_id, created_at, artifact_id)",
    ),
}


def migrate_owner_once(connection: sqlite3.Connection, table: str) -> None:
    connection.execute(
        "CREATE TABLE IF NOT EXISTS eurus_owner_migrations ("
        "table_name TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    row = connection.execute(
        "SELECT 1 FROM eurus_owner_migrations WHERE table_name=?", (table,)
    ).fetchone()
    if row is not None:
        return
    connection.execute(f"UPDATE {table} SET owner_id='eurus' WHERE owner_id<>'eurus'")
    connection.execute(
        "INSERT OR IGNORE INTO eurus_owner_migrations VALUES (?, ?)",
        (table, datetime.now(UTC).isoformat()),
    )


def apply_migrations(connection: sqlite3.Connection) -> int:
    connection.execute(
        "CREATE TABLE IF NOT EXISTS eurus_schema_migrations "
        "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    connection.commit()
    applied = {
        int(row[0]) for row in connection.execute("SELECT version FROM eurus_schema_migrations")
    }
    if max(applied, default=0) > LATEST_SCHEMA_VERSION:
        raise RuntimeError("database schema is newer than this Eurus version")
    for version in sorted(_MIGRATIONS):
        if version in applied:
            continue
        connection.execute("BEGIN IMMEDIATE")
        try:
            already_applied = connection.execute(
                "SELECT 1 FROM eurus_schema_migrations WHERE version = ?", (version,)
            ).fetchone()
            if already_applied is not None:
                connection.commit()
                continue
            for statement in _MIGRATIONS[version]:
                connection.execute(statement)
            connection.execute(
                "INSERT INTO eurus_schema_migrations(version, applied_at) VALUES (?, ?)",
                (version, datetime.now(UTC).isoformat()),
            )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
    return max(applied | set(_MIGRATIONS), default=0)

from __future__ import annotations

import contextvars
from datetime import datetime
from typing import Any, cast
from zoneinfo import ZoneInfo

from eurus.errors import ToolError
from eurus.scheduler import (
    MISFIRE_POLICIES,
    ScheduleConcurrentUpdate,
    ScheduleStore,
    validate_deliver,
)
from eurus.tools.base import Tool

_schedule_context: contextvars.ContextVar[dict[str, str] | None] = contextvars.ContextVar(
    "eurus_schedule_context", default=None
)

MAX_SCHEDULES = 200


def set_schedule_context(
    owner_id: str, gateway: str, conversation_id: str
) -> contextvars.Token[dict[str, str] | None]:
    return _schedule_context.set(
        {"owner_id": owner_id, "gateway": gateway, "conversation_id": conversation_id}
    )


def reset_schedule_context(token: contextvars.Token[dict[str, str] | None]) -> None:
    _schedule_context.reset(token)


def _require_context() -> dict[str, str]:
    context = _schedule_context.get()
    if not context:
        raise ToolError("scheduling requires a gateway conversation context")
    return context


def _localize(value: str, timezone: str) -> str:
    try:
        return (
            datetime.fromisoformat(value)
            .astimezone(ZoneInfo(timezone))
            .isoformat(timespec="minutes")
        )
    except (ValueError, KeyError):
        return value


def _describe(schedule: Any) -> str:
    zone = schedule.timezone
    fields = [
        schedule.schedule_id[:12],
        schedule.name or "-",
        schedule.kind,
        schedule.deliver,
        f"{'paused' if not schedule.enabled else 'active'}",
        f"next={_localize(schedule.next_run_at, zone)} {zone}",
    ]
    if schedule.silent:
        fields.append("silent")
    if schedule.skills:
        fields.append(f"skills={','.join(schedule.skills)}")
    return f"{schedule.schedule_id[:12]} | " + " | ".join(fields[1:])


def schedule_tool(store: ScheduleStore) -> Tool:
    async def handler(arguments: dict[str, Any], context: Any) -> str:
        action = str(arguments.get("action", "")).strip().lower()
        actions = {
            "create": _create,
            "list": _list,
            "update": _update,
            "pause": _pause,
            "resume": _resume,
            "run": _run,
            "remove": _remove,
        }
        operation = actions.get(action)
        if operation is None:
            raise ToolError(
                "unknown action; use one of create, list, update, pause, resume, run, remove"
            )
        return operation(store, arguments)

    return Tool(
        name="schedule",
        description=(
            "Create and manage recurring or one-shot agent runs. Actions: create "
            "(objective plus kind once/interval/cron and its timing field), list, update "
            "(needs schedule_id), pause, resume, run (trigger immediately), remove. The run "
            "is a fresh agent execution; results are delivered to deliver=origin (the "
            "current conversation), local (file inbox), web, tui, or telegram:<chat_id>. "
            "Optionally attach skills (SKILL.md names), a pre_script shell command whose "
            "stdout is injected into the prompt, and silent to suppress delivery. Use this "
            "whenever the user wants something done periodically, at a future time, or "
            "recurring; convert requested times to explicit ISO-8601 timestamps."
        ),
        parameters={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["create", "list", "update", "pause", "resume", "run", "remove"],
                },
                "schedule_id": {"type": "string", "minLength": 6, "maxLength": 64},
                "objective": {"type": "string", "minLength": 1, "maxLength": 8000},
                "kind": {"type": "string", "enum": ["once", "interval", "cron"]},
                "run_at": {
                    "type": "string",
                    "description": "ISO-8601 datetime for once schedules.",
                },
                "interval_seconds": {"type": "integer", "minimum": 60, "maximum": 31536000},
                "cron_expr": {"type": "string", "minLength": 9, "maxLength": 64},
                "timezone": {"type": "string", "minLength": 3, "maxLength": 64},
                "deliver": {"type": "string", "enum": ["origin", "local", "web", "tui"]},
                "telegram_chat_id": {"type": "string", "pattern": "^[0-9-]{4,32}$"},
                "name": {"type": "string", "minLength": 1, "maxLength": 200},
                "skills": {
                    "type": "array",
                    "items": {"type": "string", "minLength": 1, "maxLength": 128},
                    "maxItems": 10,
                },
                "pre_script": {"type": "string", "maxLength": 8000},
                "silent": {"type": "boolean"},
                "misfire_policy": {"type": "string", "enum": list(MISFIRE_POLICIES)},
                "max_occurrences": {"type": "integer", "minimum": 1, "maximum": 100000},
                "limit": {"type": "integer", "minimum": 1, "maximum": 200},
            },
            "required": ["action"],
            "additionalProperties": False,
        },
        handler=handler,
    )


def _create(store: ScheduleStore, arguments: dict[str, Any]) -> str:
    ctx = _require_context()
    objective = str(arguments.get("objective", "")).strip()
    kind = str(arguments.get("kind", "")).strip()
    if not objective:
        raise ToolError("objective is required to create a schedule")
    if kind not in ("once", "interval", "cron"):
        raise ToolError("kind must be once, interval, or cron")
    telegram_chat_id = str(arguments.get("telegram_chat_id", "")).strip()
    if telegram_chat_id:
        deliver = f"telegram:{telegram_chat_id}"
    else:
        deliver = str(arguments.get("deliver", "origin") or "origin")
    try:
        deliver = validate_deliver(deliver)
        schedule = store.create(
            ctx["owner_id"],
            objective,
            kind=cast(Any, kind),
            run_at=arguments.get("run_at"),
            interval_seconds=arguments.get("interval_seconds"),
            cron_expr=arguments.get("cron_expr"),
            timezone=str(arguments.get("timezone") or "UTC"),
            misfire_policy=arguments.get("misfire_policy"),
            max_occurrences=arguments.get("max_occurrences"),
            name=str(arguments.get("name", "") or ""),
            deliver=deliver,
            origin_gateway=ctx["gateway"],
            origin_conversation=ctx["conversation_id"],
            skills=list(arguments.get("skills") or ()),
            pre_script=str(arguments.get("pre_script", "") or ""),
            silent=bool(arguments.get("silent", False)),
        )
    except ValueError as exc:
        raise ToolError(str(exc)) from exc
    next_run = _localize(schedule.next_run_at, schedule.timezone)
    return (
        f"Schedule {schedule.schedule_id[:12]} created ({schedule.kind}); "
        f"next run {next_run} {schedule.timezone}; deliver={schedule.deliver}."
    )


def _list(store: ScheduleStore, arguments: dict[str, Any]) -> str:
    ctx = _require_context()
    limit = int(arguments.get("limit", 50))
    schedules = store.list_owner(ctx["owner_id"], limit=min(max(limit, 1), MAX_SCHEDULES))
    if not schedules:
        return "No schedules found."
    return "\n".join(_describe(schedule) for schedule in schedules)


def _update(store: ScheduleStore, arguments: dict[str, Any]) -> str:
    ctx = _require_context()
    schedule_id = str(arguments.get("schedule_id", "")).strip()
    if not schedule_id:
        raise ToolError("schedule_id is required for update")
    telegram_chat_id = str(arguments.get("telegram_chat_id", "")).strip()
    deliver = f"telegram:{telegram_chat_id}" if telegram_chat_id else arguments.get("deliver")
    if deliver is not None:
        try:
            deliver = validate_deliver(str(deliver))
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
    kwargs: dict[str, Any] = {}
    if arguments.get("objective"):
        kwargs["objective"] = str(arguments["objective"]).strip()
    for key in ("interval_seconds", "cron_expr", "misfire_policy", "max_occurrences"):
        if arguments.get(key) is not None:
            kwargs[key] = arguments[key]
    if arguments.get("name") is not None:
        kwargs["name"] = str(arguments["name"])
    if deliver is not None:
        kwargs["deliver"] = deliver
    if arguments.get("skills") is not None:
        kwargs["skills"] = list(arguments["skills"])
    if arguments.get("pre_script") is not None:
        kwargs["pre_script"] = str(arguments["pre_script"])
    if arguments.get("silent") is not None:
        kwargs["silent"] = bool(arguments["silent"])
    if not kwargs:
        raise ToolError("nothing to update")
    try:
        updated = store.update(schedule_id, ctx["owner_id"], **kwargs)
    except ValueError as exc:
        raise ToolError(str(exc)) from exc
    except ScheduleConcurrentUpdate as exc:
        raise ToolError(f"concurrent update rejected: {exc}") from exc
    if updated is None:
        raise ToolError("schedule not found")
    return f"Schedule {updated.schedule_id[:12]} updated; next run {updated.next_run_at}."


def _pause(store: ScheduleStore, arguments: dict[str, Any]) -> str:
    ctx = _require_context()
    schedule_id = str(arguments.get("schedule_id", "")).strip()
    if not schedule_id:
        raise ToolError("schedule_id is required for pause")
    if not store.pause(schedule_id, ctx["owner_id"]):
        raise ToolError("schedule not found")
    return f"Schedule {schedule_id[:12]} paused."


def _resume(store: ScheduleStore, arguments: dict[str, Any]) -> str:
    ctx = _require_context()
    schedule_id = str(arguments.get("schedule_id", "")).strip()
    if not schedule_id:
        raise ToolError("schedule_id is required for resume")
    if not store.resume(schedule_id, ctx["owner_id"]):
        raise ToolError("schedule not found")
    return f"Schedule {schedule_id[:12]} resumed."


def _run(store: ScheduleStore, arguments: dict[str, Any]) -> str:
    ctx = _require_context()
    schedule_id = str(arguments.get("schedule_id", "")).strip()
    if not schedule_id:
        raise ToolError("schedule_id is required for run")
    job_id = store.trigger_now(schedule_id, ctx["owner_id"])
    if job_id is None:
        raise ToolError("schedule not found or already due")
    return f"Triggered schedule {schedule_id[:12]} as job {job_id[:12]}."


def _remove(store: ScheduleStore, arguments: dict[str, Any]) -> str:
    ctx = _require_context()
    schedule_id = str(arguments.get("schedule_id", "")).strip()
    if not schedule_id:
        raise ToolError("schedule_id is required for remove")
    if not store.remove(schedule_id, ctx["owner_id"]):
        raise ToolError("schedule not found")
    return f"Schedule {schedule_id[:12]} removed."


__all__ = [
    "reset_schedule_context",
    "schedule_tool",
    "set_schedule_context",
]

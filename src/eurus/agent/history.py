from __future__ import annotations

from eurus.models import Message

CANCELLED_RESULT = "error: cancelled before finishing"


def repair_history(history: list[Message]) -> bool:
    """Pair dangling tool calls with synthetic results and drop orphan results."""
    owned: dict[str, str] = {}
    for message in history:
        if message.role == "assistant":
            for call in message.tool_calls:
                owned[call.id] = call.name
    answered = {
        message.tool_call_id
        for message in history
        if message.role == "tool" and message.tool_call_id
    }
    keep = [
        message
        for message in history
        if not (
            message.role == "tool" and message.tool_call_id and message.tool_call_id not in owned
        )
    ]
    changed = len(keep) != len(history)
    missing = {call_id for call_id in owned if call_id not in answered}
    if not missing:
        if changed:
            history[:] = keep
        return changed
    repaired: list[Message] = []
    synth: list[Message] = []
    for message in keep:
        if message.role == "assistant":
            repaired.extend(synth)
            synth = [
                Message.tool(call.id, CANCELLED_RESULT, call.name)
                for call in message.tool_calls
                if call.id in missing
            ]
            if synth:
                changed = True
        repaired.append(message)
    repaired.extend(synth)
    history[:] = repaired
    return changed

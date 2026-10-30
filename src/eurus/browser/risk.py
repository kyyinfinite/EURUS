from __future__ import annotations

from dataclasses import dataclass

RISKY_ACTION_WORDS = (
    "submit",
    "pay",
    "payment",
    "purchase",
    "checkout",
    "buy",
    "order",
    "place order",
    "delete",
    "remove",
    "destroy",
    "confirm",
    "send",
    "post",
    "publish",
    "tweet",
    "share",
    "invite",
    "transfer",
    "withdraw",
    "authorize",
    "sign",
    "subscribe",
    "unsubscribe",
    "upgrade",
    "downgrade",
    "cancel",
    "agree",
    "accept",
    "approve",
    "verify identity",
    "reset password",
    "change password",
    "sign out",
    "sign in with",
    "login with",
)

SENSITIVE_INPUT_TYPES = frozenset({"password", "hidden", "card", "cc"})

SENSITIVE_AUTOCOMPLETE_PREFIXES = ("cc-",)


@dataclass(frozen=True, slots=True)
class ActionRisk:
    level: str
    reason: str = ""


def risky_name(name: str) -> bool:
    lowered = name.casefold()
    if not lowered:
        return False
    for word in RISKY_ACTION_WORDS:
        if lowered == word or lowered.startswith(word + " ") or f" {word} " in f" {lowered} ":
            return True
        if lowered.startswith(word) and len(word) >= 4:
            return True
    return False


def classify_click(role: str, name: str, tag: str, input_type: str, href: str) -> ActionRisk:
    if (
        role == "button"
        or tag in {"button", "summary"}
        or input_type in {"submit", "button", "reset"}
    ):
        if risky_name(name) or input_type == "submit":
            reason = f"this activates the button '{name}' which may submit or change data"
            return ActionRisk("consequential", reason)
        return ActionRisk("normal")
    if role == "link" or tag == "a":
        return ActionRisk("normal")
    if role in {"tab", "menuitem", "option", "radio", "checkbox", "switch"}:
        if risky_name(name):
            return ActionRisk("consequential", f"this toggles '{name}' which may change settings")
        return ActionRisk("normal")
    if not name:
        return ActionRisk("uncertain", "the target element has no accessible name")
    return ActionRisk("uncertain", "the effect of this click is not clearly readable")


def classify_type(role: str, name: str, input_type: str, autocomplete: str = "") -> ActionRisk:
    if input_type in SENSITIVE_INPUT_TYPES:
        return ActionRisk(
            "sensitive",
            f"this field is a sensitive input (type={input_type}); its value is kept secret",
        )
    lowered_autocomplete = autocomplete.lower()
    if any(lowered_autocomplete.startswith(prefix) for prefix in SENSITIVE_AUTOCOMPLETE_PREFIXES):
        return ActionRisk("sensitive", "this field holds payment data")
    if risky_name(name):
        return ActionRisk("uncertain", f"the field '{name}' may feed a consequential action")
    return ActionRisk("normal")


def describe_click(ref: str, role: str, name: str) -> str:
    label = name or role
    return f"click {role} '{label}' [{ref}]"


def describe_type(ref: str, role: str, name: str, text: str, secret: bool) -> str:
    label = name or role
    value = "••••••" if secret else (text[:60] + ("…" if len(text) > 60 else ""))
    return f"type into {role} '{label}' [{ref}]: {value}"

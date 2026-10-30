from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from eurus.browser.backend import ElementInfo

MAX_NAME_CHARS = 120
MAX_VALUE_CHARS = 160
MAX_OPTIONS = 24
MAX_ELEMENTS = 400
REF_PREFIX = "e"

INTERACTIVE_HINT = (
    "Interact using element references from the latest snapshot; references may change "
    "when the page changes."
)

_WALKER_TEMPLATE = """
() => {
  const MAX = __MAX__;
  const MAX_NAME = __MAX_NAME__;
  const MAX_VALUE = __MAX_VALUE__;
  const MAX_OPTIONS = __MAX_OPTIONS__;
  const install = () => {
    const visible = (el) => {
      for (let node = el; node && node.nodeType === 1; node = node.parentElement) {
        const style = window.getComputedStyle(node);
        if (style.display === 'none' || style.visibility === 'hidden') return false;
      }
      const rect = el.getBoundingClientRect();
      return rect.width > 0 || rect.height > 0;
    };
    const clean = (text, limit) => String(text || '').replace(/\\s+/g, ' ').trim().slice(0, limit);
    const nameOf = (el) => {
      const aria = el.getAttribute('aria-label');
      if (aria && aria.trim()) return clean(aria, MAX_NAME);
      const labelledby = el.getAttribute('aria-labelledby');
      if (labelledby) {
        const parts = labelledby
        .split(/\\s+/)
        .map((id) => document.getElementById(id))
        .filter(Boolean);
        const text = clean(parts.map((p) => p.textContent).join(' '), MAX_NAME);
        if (text) return text;
      }
      const tag = el.tagName.toLowerCase();
      if (tag === 'input' || tag === 'textarea' || tag === 'select') {
        if (el.id) {
          const label = document.querySelector('label[for="' + window.CSS.escape(el.id) + '"]');
          if (label) {
            const text = clean(label.textContent, MAX_NAME);
            if (text) return text;
          }
        }
        const wrap = el.closest('label');
        if (wrap) {
          const text = clean(wrap.textContent, MAX_NAME);
          if (text) return text;
        }
        const placeholder = el.getAttribute('placeholder');
        if (placeholder && placeholder.trim()) return clean(placeholder, MAX_NAME);
        if (tag === 'select') {
          if (el.name) return clean(el.name, MAX_NAME);
          const selected = el.selectedOptions && el.selectedOptions[0];
          return selected ? clean(selected.textContent, MAX_NAME) : '';
        }
        if (el.value && String(el.value).trim()) return clean(el.value, MAX_NAME);
        if (el.name) return clean(el.name, MAX_NAME);
        if (tag === 'input' && el.type) return clean(el.type, MAX_NAME);
      }
      const title = el.getAttribute('title');
      if (title && title.trim()) return clean(title, MAX_NAME);
      const alt = el.getAttribute('alt');
      if (alt && alt.trim()) return clean(alt, MAX_NAME);
      const own = clean(el.textContent, MAX_NAME);
      if (own) return own;
      return el.name ? clean(el.name, MAX_NAME) : '';
    };
    const roleOf = (el) => {
      const explicit = el.getAttribute('role');
      if (explicit && explicit !== 'presentation' && explicit !== 'none') return explicit;
      const tag = el.tagName.toLowerCase();
      const type = (el.type || '').toLowerCase();
      if (tag === 'a' && el.hasAttribute('href')) return 'link';
      if (tag === 'button') return 'button';
      if (tag === 'input' && ['submit', 'button', 'reset'].includes(type)) return 'button';
      if (tag === 'input' || tag === 'textarea') {
        if (type === 'checkbox') return 'checkbox';
        if (type === 'radio') return 'radio';
        if (type === 'file') return 'button';
        return 'textbox';
      }
      if (tag === 'select') return 'combobox';
      if (tag === 'summary') return 'button';
      if (/^h[1-6]$/.test(tag)) return 'heading';
      if (tag === 'img') return 'image';
      return tag;
    };
    const pathOf = (el) => {
      const parts = [];
      let node = el;
      while (node && node.nodeType === 1 && node !== document.documentElement) {
        let selector = node.tagName.toLowerCase();
        const parent = node.parentElement;
        if (parent) {
          const same = Array.from(parent.children).filter((c) => c.tagName === node.tagName);
          if (same.length > 1) selector += ':nth-of-type(' + (same.indexOf(node) + 1) + ')';
        }
        parts.unshift(selector);
        node = parent;
      }
      return parts.join(' > ');
    };
    const elements = [];
    const nodes = document.querySelectorAll(
      'a[href], button, input, textarea, select, summary, [role="button"], [role="link"], ' +
      '[role="checkbox"], [role="radio"], [role="textbox"], [role="combobox"], [role="switch"], ' +
      '[role="tab"], [role="menuitem"], [role="option"], [role="searchbox"], ' +
      '[contenteditable="true"], h1, h2, h3, h4, h5, h6'
    );
    const seen = new Set();
    for (const el of nodes) {
      if (elements.length >= MAX) break;
      if (seen.has(el)) continue;
      seen.add(el);
      if (!visible(el)) continue;
      if (el.disabled) continue;
      const tag = el.tagName.toLowerCase();
      const type = (el.type || '').toLowerCase();
      const role = roleOf(el);
      const name = nameOf(el);
      if (role !== 'heading' && !name && !el.getAttribute('aria-label')) {
        const inner = clean(el.textContent, 8);
        if (!inner && tag !== 'input' && tag !== 'textarea' && tag !== 'select') continue;
      }
      const item = { role: role, name: name, tag: tag };
      if (el.value !== undefined && ['textbox', 'searchbox'].includes(role))
        item.value = clean(el.value, MAX_VALUE);
      if (tag === 'textarea' && el.value) item.value = clean(el.value, MAX_VALUE);
      if (type && tag !== 'button') item.type = type;
      if (tag === 'a' && el.href) item.href = String(el.href).slice(0, 300);
      if (type === 'checkbox' || type === 'radio') item.checked = Boolean(el.checked);
      if (el.required) item.required = true;
      if (el.getAttribute('placeholder'))
        item.placeholder = clean(el.getAttribute('placeholder'), 80);
      if (/^h[1-6]$/.test(tag)) item.level = Number(tag.slice(1));
      if (tag === 'select') {
        const options = [];
        for (const option of Array.from(el.options || []).slice(0, MAX_OPTIONS)) {
          options.push(clean(option.textContent || option.value, 60));
        }
        if (el.multiple) item.multiple = true;
        item.options = options;
      }
      if (el.isContentEditable) item.contenteditable = true;
      elements.push(item);
    }
    return elements;
  };
  window.__eurusWalker = install;
  return install();
}
"""

WALKER_JS = (
    _WALKER_TEMPLATE.replace("__MAX__", str(MAX_ELEMENTS))
    .replace("__MAX_NAME__", str(MAX_NAME_CHARS))
    .replace("__MAX_VALUE__", str(MAX_VALUE_CHARS))
    .replace("__MAX_OPTIONS__", str(MAX_OPTIONS))
)

RESOLVE_JS = """
(index) => {
  if (typeof window.__eurusWalker !== 'function') return null;
  const elements = window.__eurusWalker();
  if (!elements || index < 0 || index >= elements.length) return null;
  return elements[index];
}
"""

_PICK_TEMPLATE = """
(index) => {
  if (typeof window.__eurusWalker !== 'function') return null;
  const elements = window.__eurusWalker();
  if (!elements || index < 0 || index >= elements.length) return null;
  const nodes = [];
  const selector = 'a[href], button, input, textarea, select, summary, [role="button"], ' +
    '[role="link"], [role="checkbox"], [role="radio"], [role="textbox"], [role="combobox"], ' +
    '[role="switch"], [role="tab"], [role="menuitem"], [role="option"], [role="searchbox"], ' +
    '[contenteditable="true"], h1, h2, h3, h4, h5, h6';
  for (const el of document.querySelectorAll(selector)) nodes.push(el);
  const visible = (el) => {
    for (let node = el; node && node.nodeType === 1; node = node.parentElement) {
      const style = window.getComputedStyle(node);
      if (style.display === 'none' || style.visibility === 'hidden') return false;
    }
    const rect = el.getBoundingClientRect();
    return rect.width > 0 || rect.height > 0;
  };
  const kept = [];
  for (const el of nodes) {
    if (el.disabled) continue;
    if (!visible(el)) continue;
    kept.push(el);
    if (kept.length > __MAX_ELEMENTS__) break;
  }
  return kept[index] || null;
}
"""

PICK_JS = _PICK_TEMPLATE.replace("__MAX_ELEMENTS__", str(MAX_ELEMENTS))


@dataclass(slots=True)
class SnapshotDiff:
    added: list[tuple[str, str]] = None  # type: ignore[assignment]
    removed: list[tuple[str, str]] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.added is None:
            self.added = []
        if self.removed is None:
            self.removed = []

    @property
    def has_changes(self) -> bool:
        return bool(self.added or self.removed)


def signature(role: str, name: str, tag: str) -> str:
    return f"{role}|{tag}|{name}"


def build_elements(raw_items: list[dict[str, Any]]) -> list[ElementInfo]:
    elements: list[ElementInfo] = []
    for index, item in enumerate(raw_items):
        if not isinstance(item, dict):
            continue
        role = str(item.get("role", ""))[:40]
        name = str(item.get("name", ""))
        tag = str(item.get("tag", ""))[:30]
        options = tuple(str(value) for value in item.get("options", []) or ())
        checked = item.get("checked")
        elements.append(
            ElementInfo(
                ref=f"{REF_PREFIX}{index + 1}",
                index=index,
                role=role,
                name=name,
                tag=tag,
                value=str(item.get("value", "")),
                href=str(item.get("href", "")),
                input_type=str(item.get("type", "")),
                checked=None if checked is None else bool(checked),
                required=bool(item.get("required", False)),
                disabled=bool(item.get("disabled", False)),
                placeholder=str(item.get("placeholder", "")),
                level=int(item.get("level", 0) or 0),
                options=options,
                signature=signature(role, name, tag),
            )
        )
    return elements


def ref_index(ref: str) -> int | None:
    cleaned = ref.strip().lower()
    if not cleaned.startswith(REF_PREFIX):
        return None
    digits = cleaned[1:]
    if not digits.isdigit():
        return None
    return int(digits) - 1


def format_element(element: ElementInfo) -> str:
    label = element.name or element.placeholder or element.value or element.tag
    prefix = f"[{element.ref}] {element.role}"
    if element.role == "heading" and element.level:
        prefix = f"[{element.ref}] heading{element.level}"
    quoted = f' "{label}"' if label else ""
    extras: list[str] = []
    if element.href:
        extras.append(element.href)
    if element.value and element.role in {"textbox", "searchbox"}:
        extras.append(f"value={element.value}")
    if element.checked is not None:
        extras.append("checked" if element.checked else "unchecked")
    if element.required:
        extras.append("required")
    if element.options:
        extras.append("options: " + ", ".join(element.options))
    suffix = f" ({'; '.join(extras)})" if extras else ""
    return f"{prefix}{quoted}{suffix}"


def format_snapshot(
    elements: list[ElementInfo],
    *,
    title: str,
    url: str,
    budget_chars: int,
    header: str = "",
) -> str:
    lines: list[str] = []
    if header:
        lines.append(header)
    lines.append(f'page "{title}" at {url}')
    lines.append(INTERACTIVE_HINT)
    body_budget = max(200, budget_chars - sum(len(line) + 1 for line in lines))
    used = 0
    for rendered, element in enumerate(elements):
        line = format_element(element)
        if used + len(line) + 1 > body_budget:
            remaining = len(elements) - rendered
            lines.append(
                f"... [{remaining} more elements omitted; take a new snapshot to see them]"
            )
            break
        lines.append(line)
        used += len(line) + 1
    return "\n".join(lines)


def snapshot_diff(previous: list[ElementInfo], current: list[ElementInfo]) -> SnapshotDiff:
    previous_keys = [item.signature for item in previous]
    current_keys = [item.signature for item in current]
    previous_set = set(previous_keys)
    current_set = set(current_keys)
    diff = SnapshotDiff()
    for item in current:
        if item.signature not in previous_set:
            diff.added.append((item.ref, item.signature))
    for item in previous:
        if item.signature not in current_set:
            diff.removed.append((item.ref, item.signature))
    return diff


def format_diff(diff: SnapshotDiff, current: list[ElementInfo], max_lines: int = 10) -> str:
    if not diff.has_changes:
        return ""
    by_signature = {item.signature: item for item in current}
    lines: list[str] = []
    for _ref, key in diff.added[:max_lines]:
        element = by_signature.get(key)
        lines.append(f"+ {format_element(element)}" if element else f"+ {key}")
    removed_slots = max(0, max_lines - len(lines))
    for _ref, key in diff.removed[:removed_slots]:
        lines.append(f"- {key}")
    return "\n".join(lines)


class RefResolver:
    def __init__(self) -> None:
        self._signatures: dict[str, str] = {}

    def remember(self, elements: list[ElementInfo]) -> None:
        self._signatures = {element.ref: element.signature for element in elements}

    def resolve(self, ref: str, current: list[ElementInfo]) -> tuple[ElementInfo, bool]:
        index = ref_index(ref)
        if index is None:
            raise ValueError(
                f"invalid element reference {ref!r}; use references like e1, e2 "
                "from the latest snapshot"
            )
        if not current:
            raise ValueError("no interactive elements found; take a snapshot first")
        expected = self._signatures.get(ref)
        if expected is None:
            raise ValueError(
                f"element reference {ref} is unknown; take a browser_snapshot and use "
                "references from it"
            )
        if index < len(current):
            candidate = current[index]
            if candidate.signature == expected:
                return candidate, False
        for element in current:
            if element.signature == expected:
                return element, True
        raise ValueError(
            f"element reference {ref} is stale: the page changed since the last snapshot; "
            "take a new browser_snapshot and use the updated references"
        )

    def reset(self) -> None:
        self._signatures = {}

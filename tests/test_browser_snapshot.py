from __future__ import annotations

import pytest

from eurus.browser.backend import ElementInfo, parse_key_combo, sanitized_filename
from eurus.browser.snapshot import (
    MAX_ELEMENTS,
    RefResolver,
    build_elements,
    format_diff,
    format_element,
    format_snapshot,
    ref_index,
    snapshot_diff,
)
from eurus.errors import ToolError


def _raw(**overrides) -> dict:
    item = {"role": "button", "name": "Submit", "tag": "button"}
    item.update(overrides)
    return item


def test_build_elements_assigns_ordered_refs() -> None:
    elements = build_elements([_raw(), _raw(role="link", name="Home", tag="a"), _raw()])
    assert [item.ref for item in elements] == ["e1", "e2", "e3"]
    assert elements[1].role == "link"
    assert elements[0].index == 0
    assert elements[0].signature == "button|button|Submit"


def test_build_elements_captures_attributes() -> None:
    elements = build_elements(
        [
            _raw(
                role="textbox",
                name="Email",
                tag="input",
                type="email",
                value="me@example.com",
                required=True,
                placeholder="you@example.com",
            ),
            _raw(role="combobox", name="Pick", tag="select", options=["A", "B"]),
            _raw(role="checkbox", name="Public", tag="input", type="checkbox", checked=True),
        ]
    )
    email, combo, checkbox = elements
    assert email.value == "me@example.com"
    assert email.required is True
    assert email.placeholder == "you@example.com"
    assert combo.options == ("A", "B")
    assert checkbox.checked is True


def test_build_elements_ignores_non_dicts() -> None:
    elements = build_elements([_raw(), "garbage", None])  # type: ignore[list-item]
    assert len(elements) == 1


def test_format_element_variants() -> None:
    button = build_elements([_raw()])[0]
    assert format_element(button) == '[e1] button "Submit"'
    heading = build_elements([_raw(role="heading", name="Title", tag="h2", level=2)])[0]
    assert format_element(heading) == '[e1] heading2 "Title"'
    link = build_elements([_raw(role="link", name="Docs", tag="a", href="https://x.dev")])[0]
    assert "https://x.dev" in format_element(link)
    checkbox = build_elements(
        [_raw(role="checkbox", name="Public", tag="input", type="checkbox", checked=False)]
    )[0]
    assert "unchecked" in format_element(checkbox)


def test_ref_index_validation() -> None:
    assert ref_index("e1") == 0
    assert ref_index("E12") == 11
    assert ref_index("e0") == -1
    assert ref_index("button") is None
    assert ref_index("e") is None
    assert ref_index("") is None


def test_format_snapshot_respects_budget() -> None:
    items = [{"role": "link", "name": f"Link number {i}", "tag": "a"} for i in range(60)]
    elements = build_elements(items)
    text = format_snapshot(elements, title="Big page", url="https://example.com", budget_chars=600)
    assert text.startswith('page "Big page" at https://example.com')
    assert "omitted" in text
    assert "[e60]" not in text


def test_format_snapshot_small_page_fully_rendered() -> None:
    elements = build_elements([_raw(name=f"B{i}") for i in range(5)])
    text = format_snapshot(elements, title="T", url="https://example.com", budget_chars=4000)
    for i in range(5):
        assert f"[e{i + 1}]" in text


def test_snapshot_diff_added_and_removed() -> None:
    before = build_elements([_raw(name="A"), _raw(name="B")])
    after = build_elements([_raw(name="A"), _raw(name="C"), _raw(name="B")])
    diff = snapshot_diff(before, after)
    assert diff.has_changes
    assert any("C" in key for _ref, key in diff.added)
    removed_text = format_diff(diff, after)
    assert "+" in removed_text


def test_snapshot_diff_no_changes() -> None:
    elements = build_elements([_raw(name="A")])
    diff = snapshot_diff(elements, build_elements([_raw(name="A")]))
    assert not diff.has_changes


class TestRefResolver:
    def test_resolve_same_position(self) -> None:
        resolver = RefResolver()
        first = build_elements([_raw(name="A"), _raw(name="B")])
        resolver.remember(first)
        element, recovered = resolver.resolve("e2", first)
        assert element.name == "B"
        assert recovered is False

    def test_resolve_rejects_invalid_format(self) -> None:
        resolver = RefResolver()
        with pytest.raises(ValueError, match="invalid element reference"):
            resolver.resolve("button", build_elements([_raw()]))

    def test_resolve_requires_prior_snapshot(self) -> None:
        resolver = RefResolver()
        with pytest.raises(ValueError, match="unknown"):
            resolver.resolve("e1", build_elements([_raw()]))

    def test_resolve_stale_when_element_changed(self) -> None:
        resolver = RefResolver()
        resolver.remember(build_elements([_raw(name="Old")]))
        with pytest.raises(ValueError, match="stale"):
            resolver.resolve("e1", build_elements([_raw(name="New")]))

    def test_resolve_recovers_moved_element(self) -> None:
        resolver = RefResolver()
        resolver.remember(build_elements([_raw(name="Target"), _raw(name="Other")]))
        moved = build_elements([_raw(name="Other"), _raw(name="Target")])
        element, recovered = resolver.resolve("e1", moved)
        assert element.ref == "e2"
        assert recovered is True

    def test_resolve_empty_page(self) -> None:
        resolver = RefResolver()
        resolver.remember(build_elements([_raw()]))
        with pytest.raises(ValueError, match="no interactive elements"):
            resolver.resolve("e1", [])


def test_element_cap_constant() -> None:
    assert MAX_ELEMENTS == 400


def test_parse_key_combo_named_keys() -> None:
    enter = parse_key_combo("Enter")
    assert enter["virtual"] == 13
    assert enter["text"] == "\r"
    combo = parse_key_combo("Control+a")
    assert combo["modifiers"] == 2
    assert combo["key"] == "a"
    shift = parse_key_combo("Shift+Tab")
    assert shift["modifiers"] == 8
    assert shift["virtual"] == 9


def test_parse_key_combo_rejects_unknown() -> None:
    with pytest.raises(ToolError, match="unsupported key"):
        parse_key_combo("Warp")
    with pytest.raises(ToolError, match="modifier"):
        parse_key_combo("Hyper+a")
    with pytest.raises(ToolError, match="empty"):
        parse_key_combo("+++")


def test_sanitized_filename() -> None:
    assert sanitized_filename("report.pdf") == "report.pdf"
    cleaned = sanitized_filename("../../etc/passwd")
    assert ".." not in cleaned
    assert "/" not in sanitized_filename("a/b/c.txt")
    cleaned = sanitized_filename("weird name with spaces!.zip")
    assert " " not in cleaned
    assert sanitized_filename("") == "download.bin"
    long_name = "x" * 300 + ".pdf"
    assert len(sanitized_filename(long_name)) <= 120


def test_element_info_defaults() -> None:
    info = ElementInfo(ref="e1", index=0, role="button", name="X", tag="button")
    assert info.href == ""
    assert info.checked is None
    assert info.options == ()

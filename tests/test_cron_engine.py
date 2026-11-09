from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from eurus.cron import CronExpression, CronParseError

UTC_ZONE = ZoneInfo("UTC")
NEW_YORK = ZoneInfo("America/New_York")
BERLIN = ZoneInfo("Europe/Berlin")
JAKARTA = ZoneInfo("Asia/Jakarta")


def parse(text: str) -> CronExpression:
    return CronExpression.parse(text)


def at(year, month, day, hour, minute, zone=UTC_ZONE):
    return datetime(year, month, day, hour, minute, tzinfo=zone)


@pytest.mark.parametrize(
    "text",
    [
        "* * * * *",
        "0 * * * *",
        "*/5 * * * *",
        "30 2 * * *",
        "0 0 1 1 *",
        "59 23 31 12 *",
        "1,2,3,4,5 0 * * *",
        "0 9-17 * * *",
        "0 8 * * mon-fri",
        "0 8 * * 1-5",
        "0 0 * * sun",
        "0 0 * * 7",
        "0 0 1 jan *",
        "0 0 1 jan,jul *",
        "0 0 */2 * *",
        "0 12 */3 * *",
        "15,45 8,18 * * 1,3",
        "0 0 29 2 *",
        "5 4 * * sat,sun",
        "0 0 1 */3 *",
    ],
)
def test_valid_expressions_parse(text):
    expr = parse(text)
    assert expr.text == " ".join(text.split())


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        "* * * *",
        "* * * * * *",
        "60 * * * *",
        "* 24 * * *",
        "* * 0 * *",
        "* * 32 * *",
        "* * * 0 *",
        "* * * 13 *",
        "* * * * 8",
        "*/0 * * * *",
        "a * * * *",
        "5-1 * * * *",
        "1,,2 * * * *",
        "1-2-3 * * * *",
        "0 0 * 4:0 *",
    ],
)
def test_invalid_expressions_rejected(text):
    with pytest.raises(CronParseError):
        parse(text)


def test_every_minute_steps():
    expr = parse("* * * * *")
    first = at(2026, 3, 1, 10, 30)
    assert expr.next_after(first, zone=UTC_ZONE) == at(2026, 3, 1, 10, 31)


def test_hourly_boundary():
    expr = parse("0 * * * *")
    first = at(2026, 3, 1, 10, 30)
    assert expr.next_after(first, zone=UTC_ZONE) == at(2026, 3, 1, 11, 0)


def test_step_minutes():
    expr = parse("*/15 * * * *")
    first = at(2026, 3, 1, 10, 31)
    assert expr.next_after(first, zone=UTC_ZONE) == at(2026, 3, 1, 10, 45)


def test_daily_at_time():
    expr = parse("30 2 * * *")
    first = at(2026, 3, 1, 3, 0)
    assert expr.next_after(first, zone=UTC_ZONE) == at(2026, 3, 2, 2, 30)


def test_month_and_dom():
    expr = parse("0 0 1 1 *")
    first = at(2026, 7, 15, 12, 0)
    assert expr.next_after(first, zone=UTC_ZONE) == at(2027, 1, 1, 0, 0)


def test_feb_29_leap_year():
    expr = parse("0 0 29 2 *")
    first = at(2026, 1, 1, 0, 0)
    assert expr.next_after(first, zone=UTC_ZONE) == at(2028, 2, 29, 0, 0)


def test_never_matching_expression_raises():
    expr = parse("0 0 30 2 *")
    first = at(2026, 1, 1, 0, 0)
    with pytest.raises(CronParseError):
        expr.next_after(first, zone=UTC_ZONE)


def test_dow_names_and_numbers():
    named = parse("0 8 * * mon-fri")
    numeric = parse("0 8 * * 1-5")
    first = at(2026, 3, 6, 12, 0)
    assert named.next_after(first, zone=UTC_ZONE) == numeric.next_after(first, zone=UTC_ZONE)
    assert named.next_after(first, zone=UTC_ZONE) == at(2026, 3, 9, 8, 0)


def test_sunday_as_seven():
    seven = parse("0 0 * * 7")
    zero = parse("0 0 * * sun")
    first = at(2026, 3, 4, 12, 0)
    assert seven.next_after(first, zone=UTC_ZONE) == zero.next_after(first, zone=UTC_ZONE)


def test_dom_and_dow_union_semantics():
    expr = parse("0 0 13 * fri")
    first = at(2026, 3, 1, 0, 0)
    nxt = expr.next_after(first, zone=UTC_ZONE)
    assert nxt == at(2026, 3, 6, 0, 0)
    following = expr.next_after(nxt, zone=UTC_ZONE)
    assert following == at(2026, 3, 13, 0, 0)
    after_both = expr.next_after(following, zone=UTC_ZONE)
    assert after_both == at(2026, 3, 20, 0, 0)


def test_dom_only_when_dow_wildcard():
    expr = parse("0 0 15 * *")
    first = at(2026, 3, 16, 0, 0)
    assert expr.next_after(first, zone=UTC_ZONE) == at(2026, 4, 15, 0, 0)


def test_wildcard_with_step_counts_as_restricted_for_dow():
    expr = parse("0 0 * * */2")
    first = at(2026, 3, 2, 12, 0)
    nxt = expr.next_after(first, zone=UTC_ZONE)
    assert nxt == at(2026, 3, 3, 0, 0)
    assert (nxt.weekday() + 1) % 7 == 2


def test_matches_method_agrees_with_next_after():
    expr = parse("*/10 3-5 * * *")
    moment = at(2026, 5, 5, 4, 20)
    assert expr.matches(moment)
    assert not expr.matches(at(2026, 5, 5, 4, 21))
    assert not expr.matches(at(2026, 5, 5, 6, 20))


def test_next_after_requires_timezone():
    expr = parse("* * * * *")
    with pytest.raises(ValueError):
        expr.next_after(datetime(2026, 1, 1))


def test_next_after_accepts_naive_zone_string():
    expr = parse("0 9 * * *")
    first = datetime(2026, 3, 1, 10, 0, tzinfo=UTC)
    nxt = expr.next_after(first, zone="Asia/Jakarta")
    assert nxt.tzinfo is not None
    assert nxt.astimezone(JAKARTA).hour == 9


def test_sequence_of_occurrences():
    expr = parse("*/7 * * * *")
    current = at(2026, 6, 1, 0, 0)
    seen = []
    for _ in range(5):
        current = expr.next_after(current, zone=UTC_ZONE)
        seen.append(current.minute)
    assert seen == [7, 14, 21, 28, 35]


def test_dst_spring_forward_gap_skipped():
    expr = parse("30 2 * * *")
    before = datetime(2026, 3, 7, 12, 0, tzinfo=NEW_YORK)
    nxt = expr.next_after(before, zone=NEW_YORK)
    assert nxt.astimezone(NEW_YORK).replace(tzinfo=None) == datetime(2026, 3, 9, 2, 30)


def test_dst_spring_forward_every_minute_fires_each_real_minute():
    expr = parse("* * * * *")
    before = datetime(2026, 3, 8, 1, 58, 30, tzinfo=NEW_YORK)
    nxt = expr.next_after(before, zone=NEW_YORK)
    assert nxt.astimezone(NEW_YORK).replace(tzinfo=None) == datetime(2026, 3, 8, 1, 59)
    following = expr.next_after(nxt, zone=NEW_YORK)
    assert following.astimezone(NEW_YORK).replace(tzinfo=None) == datetime(2026, 3, 8, 3, 0)


def test_dst_fall_back_ambiguous_time_fires_once():
    expr = parse("30 1 * * *")
    before = datetime(2026, 10, 31, 12, 0, tzinfo=NEW_YORK)
    nxt = expr.next_after(before, zone=NEW_YORK)
    local = nxt.astimezone(NEW_YORK)
    assert local.replace(tzinfo=None) == datetime(2026, 11, 1, 1, 30)
    assert local.fold == 0
    following = expr.next_after(nxt, zone=NEW_YORK)
    assert following.astimezone(NEW_YORK).replace(tzinfo=None) == datetime(2026, 11, 2, 1, 30)


def test_dst_berlin_transition():
    expr = parse("30 2 * * *")
    before = datetime(2026, 3, 28, 12, 0, tzinfo=BERLIN)
    nxt = expr.next_after(before, zone=BERLIN)
    assert nxt.astimezone(BERLIN).replace(tzinfo=None) == datetime(2026, 3, 30, 2, 30)
    gap_probe = expr.next_after(datetime(2026, 3, 29, 1, 0, tzinfo=BERLIN), zone=BERLIN)
    assert gap_probe.astimezone(BERLIN).replace(tzinfo=None) == datetime(2026, 3, 30, 2, 30)


def test_jakarta_no_dst():
    expr = parse("0 9 * * *")
    before = datetime(2026, 1, 1, 10, 0, tzinfo=JAKARTA)
    nxt = expr.next_after(before, zone=JAKARTA)
    assert nxt.astimezone(JAKARTA).replace(tzinfo=None) == datetime(2026, 1, 2, 9, 0)


def test_previous_before():
    expr = parse("0 12 * * *")
    before = at(2026, 3, 5, 15, 0)
    prev = expr.previous_before(before, zone=UTC_ZONE)
    assert prev == at(2026, 3, 5, 12, 0)
    assert expr.previous_before(at(2026, 3, 5, 12, 0), zone=UTC_ZONE) == at(2026, 3, 4, 12, 0)


def test_previous_before_none_when_exhausted():
    expr = parse("0 0 29 2 *")
    assert expr.previous_before(at(2028, 1, 1, 0, 0), zone=UTC_ZONE) == at(2024, 2, 29, 0, 0)
    assert expr.previous_before(at(2024, 1, 1, 0, 0), zone=UTC_ZONE) == at(2020, 2, 29, 0, 0)


def test_occurrences_between_helper():
    expr = parse("0 * * * *")
    start = at(2026, 4, 1, 10, 30)
    end = at(2026, 4, 1, 14, 0)
    collected = []
    current = start
    while True:
        nxt = expr.next_after(current, zone=UTC_ZONE)
        if nxt > end:
            break
        collected.append(nxt)
        current = nxt
    assert [item.hour for item in collected] == [11, 12, 13, 14]


def test_uppercase_names_rejected_or_lowered():
    expr = CronExpression.parse("0 8 * * MON-FRI")
    assert expr.day_of_week.matches(1)
    assert not expr.day_of_week.matches(6)


def test_parsing_is_not_string_splitting():
    expr = CronExpression.parse("  5   4   *   *   *  ")
    assert expr.text == "5 4 * * *"
    assert expr.next_after(at(2026, 1, 1, 4, 5), zone=UTC_ZONE) == at(2026, 1, 2, 4, 5)

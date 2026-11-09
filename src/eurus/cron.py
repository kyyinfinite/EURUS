from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

MONTH_NAMES = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}
DOW_NAMES = {"sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6}
FIELD_RANGES = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 6))
FIELD_ORDER = ("minute", "hour", "day_of_month", "month", "day_of_week")
MAX_SCAN_YEARS = 8
MAX_SEARCH_ITERATIONS = 500_000


class CronParseError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class CronField:
    values: frozenset[int]
    wildcard: bool

    def matches(self, value: int) -> bool:
        return value in self.values


@dataclass(frozen=True, slots=True)
class CronExpression:
    minute: CronField
    hour: CronField
    day_of_month: CronField
    month: CronField
    day_of_week: CronField
    text: str

    @classmethod
    def parse(cls, text: str) -> CronExpression:
        if not isinstance(text, str) or not text.strip():
            raise CronParseError("cron expression must be a non-empty string")
        cleaned = " ".join(text.split())
        parts = cleaned.split(" ")
        if len(parts) != 5:
            raise CronParseError("cron expression must have exactly 5 fields")
        return cls(
            minute=_parse_field(parts[0], 0),
            hour=_parse_field(parts[1], 1),
            day_of_month=_parse_field(parts[2], 2),
            month=_parse_field(parts[3], 3),
            day_of_week=_parse_field(parts[4], 4),
            text=cleaned,
        )

    def matches(self, moment: datetime) -> bool:
        if not self.month.matches(moment.month):
            return False
        if not self.matches_day(moment):
            return False
        return self.hour.matches(moment.hour) and self.minute.matches(moment.minute)

    def matches_day(self, moment: datetime) -> bool:
        cron_dow = (moment.weekday() + 1) % 7
        dom_restricted = not self.day_of_month.wildcard
        dow_restricted = not self.day_of_week.wildcard
        if dom_restricted and dow_restricted:
            return self.day_of_month.matches(moment.day) or self.day_of_week.matches(cron_dow)
        if dom_restricted:
            return self.day_of_month.matches(moment.day)
        if dow_restricted:
            return self.day_of_week.matches(cron_dow)
        return True

    def next_after(self, after: datetime, *, zone: str | ZoneInfo = "UTC") -> datetime:
        target = _resolve_zone(zone)
        if after.tzinfo is None:
            raise ValueError("after must include a timezone")
        return _next_occurrence(self, after.astimezone(target))

    def previous_before(self, before: datetime, *, zone: str | ZoneInfo = "UTC") -> datetime | None:
        target = _resolve_zone(zone)
        if before.tzinfo is None:
            raise ValueError("before must include a timezone")
        return _previous_occurrence(self, before.astimezone(target))


def _resolve_zone(zone: str | ZoneInfo) -> ZoneInfo:
    if isinstance(zone, ZoneInfo):
        return zone
    try:
        return ZoneInfo(zone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise CronParseError(f"unknown timezone: {zone}") from exc


def _parse_number(token: str, field_index: int) -> int:
    lowered = token.lower()
    if field_index == 3 and lowered in MONTH_NAMES:
        return MONTH_NAMES[lowered]
    if field_index == 4 and lowered in DOW_NAMES:
        return DOW_NAMES[lowered]
    if not token.isdigit():
        raise CronParseError(f"invalid value {token!r} in {FIELD_ORDER[field_index]} field")
    value = int(token)
    if field_index == 4 and value == 7:
        return 0
    return value


def _parse_field(raw: str, field_index: int) -> CronField:
    lowered = raw.lower()
    wildcard = lowered == "*"
    step = 1
    body = lowered
    if "/" in lowered:
        body, _, step_text = lowered.partition("/")
        if not step_text.isdigit() or int(step_text) < 1:
            raise CronParseError(f"invalid step in {FIELD_ORDER[field_index]} field")
        step = int(step_text)
    low, high = FIELD_RANGES[field_index]
    if body == "*":
        values = frozenset(range(low, high + 1, step))
        return CronField(values, wildcard=wildcard)
    collected: set[int] = set()
    for chunk in body.split(","):
        if not chunk:
            raise CronParseError(f"empty list element in {FIELD_ORDER[field_index]} field")
        if chunk.startswith("*"):
            collected.update(range(low, high + 1, step))
            continue
        if "-" in chunk:
            left, _, right = chunk.partition("-")
            start = _parse_number(left, field_index)
            end = _parse_number(right, field_index)
            if start > end:
                raise CronParseError(
                    f"descending range {chunk!r} in {FIELD_ORDER[field_index]} field"
                )
        else:
            start = _parse_number(chunk, field_index)
            end = start
        collected.update(range(start, end + 1, step))
    for value in collected:
        if not low <= value <= high:
            raise CronParseError(f"value {value} out of range in {FIELD_ORDER[field_index]} field")
    return CronField(frozenset(collected), wildcard=False)


def _valid_local(moment: datetime, zone: ZoneInfo | timezone) -> bool:
    if isinstance(zone, timezone) or zone.key in {"UTC", "Etc/UTC", "Z"}:
        return True
    normalized = moment.replace(fold=0)
    round_trip = normalized.astimezone(UTC).astimezone(zone)
    return round_trip.replace(tzinfo=None) == normalized.replace(tzinfo=None)


def _first_of_next_month(candidate: datetime) -> datetime:
    year, month = candidate.year, candidate.month
    if month == 12:
        year, month = year + 1, 1
    else:
        month += 1
    return candidate.replace(year=year, month=month, day=1, hour=0, minute=0)


def _start_of_next_day(candidate: datetime) -> datetime:
    next_day = candidate.replace(hour=0, minute=0) + timedelta(days=1)
    return next_day


def _next_matching_hour(candidate: datetime, hours: frozenset[int]) -> datetime | None:
    for hour in sorted(hours):
        if hour > candidate.hour:
            return candidate.replace(hour=hour, minute=0)
    return None


def _next_matching_minute(candidate: datetime, minutes: frozenset[int]) -> datetime | None:
    for minute in sorted(minutes):
        if minute > candidate.minute:
            return candidate.replace(minute=minute)
    return None


def _next_occurrence(expr: CronExpression, after: datetime) -> datetime:
    zone = after.tzinfo
    if not isinstance(zone, ZoneInfo) and zone is not UTC:
        zone = UTC
    candidate = (after + timedelta(minutes=1)).replace(second=0, microsecond=0)
    deadline_year = after.year + MAX_SCAN_YEARS
    iterations = 0
    while candidate.year <= deadline_year:
        iterations += 1
        if iterations > MAX_SEARCH_ITERATIONS:
            raise CronParseError("no occurrence found within the search window")
        if not expr.month.matches(candidate.month):
            candidate = _first_of_next_month(candidate)
            continue
        if not expr.matches_day(candidate):
            candidate = _start_of_next_day(candidate)
            continue
        if not expr.hour.matches(candidate.hour):
            jumped = _next_matching_hour(candidate, expr.hour.values)
            candidate = jumped if jumped is not None else _start_of_next_day(candidate)
            continue
        if not expr.minute.matches(candidate.minute):
            jumped = _next_matching_minute(candidate, expr.minute.values)
            if jumped is not None:
                candidate = jumped
            else:
                candidate = candidate.replace(minute=0) + timedelta(hours=1)
            continue
        if not _valid_local(candidate, zone):
            candidate = candidate + timedelta(minutes=1)
            continue
        return candidate
    raise CronParseError("no occurrence found within the search window")


def _previous_occurrence(expr: CronExpression, before: datetime) -> datetime | None:
    zone = before.tzinfo
    if not isinstance(zone, ZoneInfo) and zone is not UTC:
        zone = UTC
    candidate = before.replace(second=0, microsecond=0)
    if candidate == before:
        candidate = candidate - timedelta(minutes=1)
    earliest_year = before.year - MAX_SCAN_YEARS
    iterations = 0
    while candidate.year >= earliest_year:
        iterations += 1
        if iterations > MAX_SEARCH_ITERATIONS:
            return None
        if not expr.month.matches(candidate.month):
            candidate = _last_of_previous_month(candidate)
            continue
        if not expr.matches_day(candidate):
            candidate = candidate.replace(hour=23, minute=59) - timedelta(days=1)
            continue
        if not expr.hour.matches(candidate.hour):
            previous_hour = _previous_matching_hour(candidate, expr.hour.values)
            if previous_hour is not None:
                candidate = previous_hour
            else:
                candidate = candidate.replace(hour=0, minute=0) - timedelta(minutes=1)
            continue
        if not expr.minute.matches(candidate.minute):
            previous_minute = _previous_matching_minute(candidate, expr.minute.values)
            if previous_minute is not None:
                candidate = previous_minute
            else:
                candidate = candidate.replace(minute=0) - timedelta(minutes=1)
            continue
        if not _valid_local(candidate, zone):
            candidate = candidate - timedelta(minutes=1)
            continue
        return candidate
    return None


def _last_of_previous_month(candidate: datetime) -> datetime:
    first_of_month = candidate.replace(day=1, hour=0, minute=0)
    return first_of_month - timedelta(minutes=1)


def _previous_matching_hour(candidate: datetime, hours: frozenset[int]) -> datetime | None:
    for hour in sorted(hours, reverse=True):
        if hour < candidate.hour:
            return candidate.replace(hour=hour, minute=59)
    return None


def _previous_matching_minute(candidate: datetime, minutes: frozenset[int]) -> datetime | None:
    for minute in sorted(minutes, reverse=True):
        if minute < candidate.minute:
            return candidate.replace(minute=minute)
    return None


def matches_day(expr: CronExpression, moment: datetime) -> bool:
    return expr.matches_day(moment)


def cron_field_summary(expr: CronExpression) -> dict[str, Any]:
    return {
        "minute": sorted(expr.minute.values),
        "hour": sorted(expr.hour.values),
        "day_of_month": sorted(expr.day_of_month.values),
        "month": sorted(expr.month.values),
        "day_of_week": sorted(expr.day_of_week.values),
    }


def month_length(year: int, month: int) -> int:
    return calendar.monthrange(year, month)[1]

from __future__ import annotations

import asyncio

from eurus.browser.cdp_backend import CdpConnection


def make_connection() -> CdpConnection:
    return CdpConnection("ws://127.0.0.1:0/devtools/browser")


def append_event(connection: CdpConnection, method: str, **fields: object) -> int:
    connection._last_seq += 1
    connection._events.append(
        (connection._last_seq, {"method": method, "sessionId": "s1", **fields})
    )
    return connection._last_seq


async def test_position_counts_appended_events() -> None:
    connection = make_connection()
    assert connection.position() == 0
    append_event(connection, "Page.enable")
    append_event(connection, "Network.requestWillBeSent")
    assert connection.position() == 2


async def test_wait_event_cursor_finds_first_match_after_cursor() -> None:
    connection = make_connection()
    append_event(connection, "Network.requestWillBeSent")
    pause_seq = append_event(connection, "Fetch.requestPaused", params={"a": 1})
    append_event(connection, "Page.loadEventFired")

    event, cursor = await connection.wait_event_cursor(
        "Fetch.requestPaused", timeout=0.1, session_id="s1"
    )
    assert event is not None
    assert event["params"] == {"a": 1}
    assert cursor == pause_seq

    event, cursor = await connection.wait_event_cursor(
        "Fetch.requestPaused", timeout=0.1, session_id="s1", since=cursor
    )
    assert event is None


async def test_pump_style_scans_never_skip_late_fetch_pause() -> None:
    connection = make_connection()
    append_event(connection, "Page.frameStartedLoading")
    fetch_cursor = connection.position()
    dialog_cursor = connection.position()

    event, dialog_cursor = await connection.wait_event_cursor(
        "Page.javascriptDialogOpening", timeout=0.05, since=dialog_cursor
    )
    assert event is None

    append_event(connection, "Fetch.requestPaused", params={"url": "late"})

    event, fetch_cursor = await connection.wait_event_cursor(
        "Fetch.requestPaused", timeout=0.1, since=fetch_cursor
    )
    assert event is not None
    assert event["params"]["url"] == "late"

    event, dialog_cursor = await connection.wait_event_cursor(
        "Page.javascriptDialogOpening", timeout=0.05, since=dialog_cursor
    )
    assert event is None


async def test_fetch_and_dialog_events_do_not_starve_each_other() -> None:
    connection = make_connection()
    for _ in range(50):
        append_event(connection, "Network.dataReceived")
    append_event(connection, "Page.javascriptDialogOpening", params={"message": "hi"})
    append_event(connection, "Fetch.requestPaused", params={"url": "blocked"})

    event, fetch_cursor = await connection.wait_event_cursor(
        "Fetch.requestPaused", timeout=0.1, since=0
    )
    assert event is not None
    assert event["params"]["url"] == "blocked"

    event, dialog_cursor = await connection.wait_event_cursor(
        "Page.javascriptDialogOpening", timeout=0.1, since=0
    )
    assert event is not None
    assert event["params"]["message"] == "hi"

    again, _ = await connection.wait_event_cursor(
        "Fetch.requestPaused", timeout=0.05, since=fetch_cursor
    )
    assert again is None


async def test_cursor_survives_buffer_trimming() -> None:
    connection = make_connection()
    for _index in range(900):
        append_event(connection, "Noise.event")
    survivor = append_event(connection, "Fetch.requestPaused", params={"n": 1})
    assert len(connection._events) == 800
    assert connection.position() == survivor

    event, cursor = await connection.wait_event_cursor("Fetch.requestPaused", timeout=0.1, since=1)
    assert event is not None
    assert cursor == survivor


async def test_session_filter_and_prefix_filter() -> None:
    connection = make_connection()
    connection._last_seq += 1
    connection._events.append(
        (connection._last_seq, {"method": "Target.attachedToTarget", "sessionId": "other"})
    )
    event, cursor = await connection.next_event(timeout=0.05, session_id="s1", prefix="Target.")
    assert event is None
    assert cursor == 1

    event, cursor = await connection.next_event(timeout=0.05, session_id="other", prefix="Target.")
    assert event is not None
    assert cursor == 1


async def test_next_event_prefix_matching() -> None:
    connection = make_connection()
    append_event(connection, "Target.targetCreated")
    append_event(connection, "Target.targetInfoChanged")
    append_event(connection, "Page.loadEventFired")

    event, cursor = await connection.next_event(timeout=0.1, prefix="Target.")
    assert event is not None
    assert event["method"] == "Target.targetCreated"

    event, cursor = await connection.next_event(timeout=0.1, prefix="Target.", since=cursor)
    assert event is not None
    assert event["method"] == "Target.targetInfoChanged"

    event, _ = await connection.next_event(timeout=0.05, prefix="Target.", since=cursor)
    assert event is None


async def test_wait_notification_wakes_pending_scan() -> None:
    connection = make_connection()

    async def append_later() -> None:
        await asyncio.sleep(0.05)
        async with connection._condition:
            connection._last_seq += 1
            connection._events.append(
                (connection._last_seq, {"method": "Fetch.requestPaused", "sessionId": "s1"})
            )
            connection._condition.notify_all()

    task = asyncio.create_task(append_later())
    event, cursor = await connection.wait_event_cursor(
        "Fetch.requestPaused", timeout=2.0, session_id="s1"
    )
    await task
    assert event is not None
    assert cursor == 1

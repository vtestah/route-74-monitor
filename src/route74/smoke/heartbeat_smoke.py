from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from route74.models import NOVOSIBIRSK_TZ
from route74.storage import init_db
from route74.storage.heartbeat import (
    load_bot_update_offset,
    load_collector_heartbeat,
    save_bot_update_offset,
    update_collector_heartbeat,
)


def main() -> None:
    with sqlite3.connect(":memory:") as connection:
        connection.row_factory = sqlite3.Row
        init_db(connection)
        _assert_collector_heartbeat_requires_local_timestamp(connection)
        save_bot_update_offset(
            connection,
            name="web-runtime",
            update_offset=42,
            updated_at=datetime(2026, 6, 7, 8, 0, tzinfo=NOVOSIBIRSK_TZ),
        )
        _assert_equal(load_bot_update_offset(connection, "web-runtime"), 42)
        _assert_rejects(
            lambda: save_bot_update_offset(
                connection,
                name="web-runtime",
                update_offset=43,
                updated_at=datetime(2026, 6, 7, 8, 1),
            ),
            "bot update offset updated_at needs timezone-aware datetime",
        )
        _assert_equal(load_bot_update_offset(connection, "web-runtime"), 42)
        _assert_rejects(
            lambda: save_bot_update_offset(
                connection,
                name="web-runtime",
                update_offset=43,
                updated_at=datetime(2026, 6, 7, 1, 1, tzinfo=timezone.utc),
            ),
            "bot update offset updated_at needs Asia/Novosibirsk timezone",
        )
        _assert_rejects(
            lambda: save_bot_update_offset(
                connection,
                name="web-runtime",
                update_offset=43,
                updated_at=datetime(2026, 6, 7, 8, 1, tzinfo=timezone(timedelta(hours=7))),
            ),
            "bot update offset updated_at needs Asia/Novosibirsk timezone",
        )
        _assert_equal(load_bot_update_offset(connection, "web-runtime"), 42)
    print("OK | heartbeat smoke passed")


def _assert_collector_heartbeat_requires_local_timestamp(connection: sqlite3.Connection) -> None:
    update_collector_heartbeat(
        connection,
        name="yandex-collect",
        pid=123,
        profile_filter="all",
        last_status="ok",
        last_message="ok",
        updated_at=datetime(2026, 6, 7, 8, 0, tzinfo=NOVOSIBIRSK_TZ),
    )
    heartbeat = load_collector_heartbeat(connection, "yandex-collect")
    if heartbeat is None:
        raise AssertionError("expected collector heartbeat")
    expected_offset = datetime(2026, 6, 7, 8, 0, tzinfo=NOVOSIBIRSK_TZ).utcoffset()
    _assert_equal(heartbeat.updated_at.utcoffset(), expected_offset)

    connection.execute(
        "UPDATE collector_heartbeat SET updated_at = ? WHERE name = ?",
        ("2026-06-07T08:00:00", "yandex-collect"),
    )
    connection.commit()
    _assert_equal(load_collector_heartbeat(connection, "yandex-collect"), None)
    connection.execute(
        "UPDATE collector_heartbeat SET updated_at = ? WHERE name = ?",
        ("2026-06-07T08:00:00+00:00", "yandex-collect"),
    )
    connection.commit()
    _assert_equal(load_collector_heartbeat(connection, "yandex-collect"), None)
    _assert_rejects(
        lambda: update_collector_heartbeat(
            connection,
            name="yandex-collect",
            pid=123,
            profile_filter="all",
            last_status="ok",
            last_message="ok",
            updated_at=datetime(2026, 6, 7, 8, 1),
        ),
        "collector heartbeat updated_at needs timezone-aware datetime",
    )
    _assert_rejects(
        lambda: update_collector_heartbeat(
            connection,
            name="yandex-collect",
            pid=123,
            profile_filter="all",
            last_status="ok",
            last_message="ok",
            updated_at=datetime(2026, 6, 7, 1, 1, tzinfo=timezone.utc),
        ),
        "collector heartbeat updated_at needs Asia/Novosibirsk timezone",
    )
    _assert_rejects(
        lambda: update_collector_heartbeat(
            connection,
            name="yandex-collect",
            pid=123,
            profile_filter="all",
            last_status="ok",
            last_message="ok",
            updated_at=datetime(2026, 6, 7, 8, 1, tzinfo=timezone(timedelta(hours=7))),
        ),
        "collector heartbeat updated_at needs Asia/Novosibirsk timezone",
    )


def _assert_equal(actual: object, expected: object) -> None:
    if actual != expected:
        raise AssertionError(f"expected {expected!r}, got {actual!r}")


def _assert_rejects(call: Callable[[], object], expected_message: str) -> None:
    try:
        call()
    except ValueError as error:
        if expected_message not in str(error):
            raise AssertionError(f"expected {expected_message!r} in {error!s}") from error
        return
    raise AssertionError(f"expected validation error: {expected_message}")


if __name__ == "__main__":
    main()

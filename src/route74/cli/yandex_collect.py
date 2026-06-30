from __future__ import annotations

import argparse
import fcntl
import os
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from route74.cli.common import profiles_from_name
from route74.cli.formatting import format_yandex_collect_result
from route74.services.yandex_telemetry import YandexTelemetryCollector
from route74.sources.yandex.browser_client import ReusableChromium
from route74.sources.yandex.config import YandexSourceConfig
from route74.sources.yandex.models import YandexSourceMode
from route74.sources.yandex.route_traffic import YandexRouteTrafficSource
from route74.sources.yandex.transport import YandexTransportSource


def cmd_yandex_collect(args: argparse.Namespace) -> None:
    lock_file = args.lock_file or Path(f"{args.db}.lock")
    with _collector_lock(lock_file):
        browser_session = ReusableChromium() if args.mode != YandexSourceMode.OFF.value else None
        source = YandexTransportSource(
            YandexSourceConfig(
                mode=YandexSourceMode(args.mode),
                timeout_seconds=args.timeout,
                persistent_browser=True,
            ),
            browser_session=browser_session,
        )
        traffic_source = _traffic_source(args, browser_session=browser_session)
        collector = YandexTelemetryCollector(
            db_path=args.db,
            source=source,
            profiles=profiles_from_name(args.profile),
            heartbeat_name=args.heartbeat_name,
            profile_filter=args.profile,
            retention_days=args.retention_days,
            report_windows_only=args.report_windows_only,
            traffic_source=traffic_source,
        )
        try:
            _collect_until_done(args, collector)
        finally:
            _close_if_supported(source)
            _close_if_supported(traffic_source)
            _close_if_supported(browser_session)


def _collect_until_done(args: argparse.Namespace, collector: YandexTelemetryCollector) -> None:
    deadline = None if args.forever else time.monotonic() + args.minutes * 60
    while True:
        started = time.monotonic()
        for result in collector.collect_once():
            print(format_yandex_collect_result(result, args.db), flush=True)
        if args.once or (deadline is not None and time.monotonic() >= deadline):
            return
        sleep_for = args.interval - (time.monotonic() - started)
        if deadline is not None:
            sleep_for = min(sleep_for, max(0.0, deadline - time.monotonic()))
        time.sleep(max(0.0, sleep_for))


def _traffic_source(
    args: argparse.Namespace,
    *,
    browser_session: ReusableChromium | None = None,
) -> Callable[..., object] | None:
    if args.traffic_mode == "off" or args.mode == YandexSourceMode.OFF.value:
        return None
    return YandexRouteTrafficSource(
        timeout_seconds=args.timeout,
        persistent_browser=True,
        browser_session=browser_session,
    )


def _close_if_supported(value: object) -> None:
    close = getattr(value, "close", None)
    if callable(close):
        close()


@contextmanager
def _collector_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            handle.seek(0)
            pid = handle.read().strip()
            suffix = f" PID: {pid}." if pid else ""
            raise SystemExit(f"collector already running: {path}{suffix}") from exc
        handle.seek(0)
        handle.truncate()
        handle.write(f"{os.getpid()}\n")
        handle.flush()
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

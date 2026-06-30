from __future__ import annotations

import argparse

from route74.cli.common import profiles_from_name
from route74.diagnostics import sanitize_diagnostic_text
from route74.models import now_local
from route74.sources.yandex.browser_client import ReusableChromium
from route74.sources.yandex.config import YandexSourceConfig
from route74.sources.yandex.models import YandexSourceMode
from route74.sources.yandex.transport import YandexTransportSource
from route74.storage import connect, init_db, insert_yandex_canary_run
from route74.storage.yandex_canary import YandexCanaryRun


def cmd_yandex_canary(args: argparse.Namespace) -> None:
    checked_at = now_local()
    browser_session = ReusableChromium() if args.mode != YandexSourceMode.OFF.value else None
    source = YandexTransportSource(
        YandexSourceConfig(
            mode=YandexSourceMode(args.mode),
            timeout_seconds=args.timeout,
            persistent_browser=True,
        ),
        browser_session=browser_session,
    )
    runs: list[YandexCanaryRun] = []
    try:
        with connect(args.db) as connection:
            init_db(connection)
            for profile in profiles_from_name(args.profile):
                forecast = source.get_forecast(profile, checked_at)
                runs.append(
                    insert_yandex_canary_run(
                        connection,
                        profile=profile,
                        forecast=forecast,
                        checked_at=checked_at,
                    )
                )
            connection.commit()
    finally:
        source.close()
        if browser_session is not None:
            browser_session.close()
    canary_runs = tuple(runs)
    print(format_yandex_canary_runs(canary_runs, args.db))
    if args.strict and yandex_canary_has_warnings(canary_runs):
        raise SystemExit(strict_yandex_canary_message(canary_runs))


def yandex_canary_has_warnings(runs: tuple[YandexCanaryRun, ...]) -> bool:
    return not runs or any(run.status != "ok" for run in runs)


def strict_yandex_canary_message(runs: tuple[YandexCanaryRun, ...]) -> str:
    if not runs:
        return "yandex canary strict failed: no runs"
    warnings = (
        f"{_diagnostic_text(run.profile_key)}:{_diagnostic_text(run.status)}:{_diagnostic_text(run.risk_reason)}"
        for run in runs
        if run.status != "ok"
    )
    return "yandex canary strict failed: " + "; ".join(warnings)


def format_yandex_canary_runs(runs: tuple[YandexCanaryRun, ...], db_path: object) -> str:
    lines = [f"yandex canary runs={len(runs)} db={_diagnostic_text(db_path)}"]
    for run in runs:
        changed = ",".join(_diagnostic_text(key) for key in run.changed_keys) or "-"
        lines.append(
            f"- profile={_diagnostic_text(run.profile_key)} status={_diagnostic_text(run.status)} "
            f"method={_diagnostic_text(run.source_method)} schema={_diagnostic_text(run.schema_hash)} "
            f"changed={changed} reason={_diagnostic_text(run.risk_reason)} "
            f"checked={run.checked_at:%Y-%m-%d %H:%M}"
        )
    return "\n".join(lines)


def _diagnostic_text(value: object, *, fallback: str = "-", limit: int = 120) -> str:
    return sanitize_diagnostic_text(value, fallback=fallback, limit=limit)

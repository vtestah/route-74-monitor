from __future__ import annotations

import argparse
import json
from pathlib import Path

from route74.cli.common import positive_float, positive_int, profile_from_name
from route74.cli.formatting import format_line_topology
from route74.cli.yandex_canary import cmd_yandex_canary
from route74.cli.yandex_collect import cmd_yandex_collect
from route74.domain.profiles import ALL_PROFILES_KEY, PROFILE_KEYS, PROFILE_SELECTORS
from route74.sources.yandex.constants import (
    expected_thread_ids,
    prediction_stop_ids,
    route_map_url,
    stop_id,
    terminal_stop_id,
)
from route74.sources.yandex.dump import capture_masstransit_dump
from route74.sources.yandex.line import YandexLineTopology, parse_line_payload
from route74.sources.yandex.models import YandexSourceMode
from route74.storage import connect, init_db, upsert_route_geometry


def register_yandex_commands(subparsers: argparse._SubParsersAction) -> None:
    yandex_dump = subparsers.add_parser("yandex-dump", help="Capture Yandex masstransit AJAX responses.")
    yandex_source = yandex_dump.add_mutually_exclusive_group(required=True)
    yandex_source.add_argument("--profile", choices=PROFILE_KEYS, help="Use a Route74 Yandex route URL.")
    yandex_source.add_argument("--url", help="Exact Yandex Maps URL to inspect.")
    yandex_dump.add_argument("--output", type=Path, default=None, help="Output JSON path.")
    yandex_dump.add_argument("--timeout", type=positive_float, default=12.0, help="Browser timeout seconds.")
    yandex_dump.add_argument(
        "--no-click-vehicles",
        action="store_true",
        help="Do not click visible vehicle markers.",
    )
    yandex_dump.set_defaults(func=cmd_yandex_dump)

    yandex_line = subparsers.add_parser("yandex-line", help="Summarize getLine payloads from a Yandex dump.")
    yandex_line.add_argument("--dump", type=Path, required=True, help="JSON file created by route74 yandex-dump.")
    yandex_line.add_argument(
        "--save-profile",
        choices=PROFILE_KEYS,
        default=None,
        help="Persist route geometry for this profile.",
    )
    yandex_line.set_defaults(func=cmd_yandex_line)

    yandex_collect = subparsers.add_parser("yandex-collect", help="Collect Yandex telemetry snapshots.")
    yandex_collect.add_argument("--profile", choices=PROFILE_SELECTORS, default=ALL_PROFILES_KEY)
    yandex_collect.add_argument("--minutes", type=positive_float, default=60.0, help="Collection duration.")
    yandex_collect.add_argument("--interval", type=positive_float, default=30.0, help="Sampling interval in seconds.")
    yandex_collect.add_argument("--once", action="store_true", help="Store one snapshot and exit.")
    yandex_collect.add_argument("--forever", action="store_true", help="Run until interrupted.")
    yandex_collect.add_argument("--mode", choices=[item.value for item in YandexSourceMode], default="auto")
    yandex_collect.add_argument("--timeout", type=positive_float, default=8.0, help="Yandex request/browser timeout seconds.")
    yandex_collect.add_argument(
        "--traffic-mode",
        choices=["browser", "off"],
        default="browser",
        help="Collect Yandex route duration/distance for traffic fields.",
    )
    yandex_collect.add_argument("--lock-file", type=Path, default=None, help="Collector lock file path.")
    yandex_collect.add_argument("--heartbeat-name", default="yandex-collect", help="SQLite heartbeat name.")
    yandex_collect.add_argument("--retention-days", type=positive_int, default=30, help="Telemetry retention in days.")
    yandex_collect.add_argument(
        "--report-windows-only",
        action="store_true",
        help="Collect only on weekdays during report windows: morning 09-12 and evening 19-22.",
    )
    yandex_collect.set_defaults(func=cmd_yandex_collect)

    yandex_canary = subparsers.add_parser("yandex-canary", help="Run a Yandex parser/API contract canary.")
    yandex_canary.add_argument("--profile", choices=PROFILE_SELECTORS, default=ALL_PROFILES_KEY)
    yandex_canary.add_argument("--mode", choices=[item.value for item in YandexSourceMode], default="auto")
    yandex_canary.add_argument("--timeout", type=positive_float, default=8.0, help="Yandex request/browser timeout seconds.")
    yandex_canary.add_argument("--once", action="store_true", help="Compatibility flag; canary runs once.")
    yandex_canary.add_argument("--strict", action="store_true", help="Exit non-zero when any canary run is warning.")
    yandex_canary.set_defaults(func=cmd_yandex_canary)


def cmd_yandex_dump(args: argparse.Namespace) -> None:
    profile = profile_from_name(args.profile) if args.profile else None
    url = route_map_url(profile) if profile is not None else args.url
    result = capture_masstransit_dump(
        url,
        timeout_seconds=args.timeout,
        click_vehicles=not args.no_click_vehicles,
    )
    payload = result.to_jsonable()
    if args.output is None:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    methods = ", ".join(entry["method"] for entry in payload["entries"]) or "none"
    print(f"captured entries={len(payload['entries'])} methods={methods} output={args.output}")


def cmd_yandex_line(args: argparse.Namespace) -> None:
    payload = _load_dump_json(args.dump)
    topologies = _line_topologies_from_dump(payload)
    if not topologies:
        print("no getLine payloads found; run route74 yandex-dump with a route URL and a longer timeout")
        return
    for index, topology in enumerate(topologies, start=1):
        if len(topologies) > 1:
            print(f"getLine #{index}")
        print(format_line_topology(topology))
    if args.save_profile:
        profile = profile_from_name(args.save_profile)
        with connect(args.db) as connection:
            init_db(connection)
            upsert_route_geometry(
                connection,
                profile_key=profile.key,
                target_stop_id=stop_id(profile),
                topology=topologies[0],
                preferred_thread_ids=expected_thread_ids(profile),
                candidate_stop_ids=prediction_stop_ids(profile),
            )
        print(
            f"saved route_geometry profile={profile.key} target_stop={stop_id(profile)} "
            f"terminal_stop={terminal_stop_id(profile)} db={args.db}"
        )


def _load_dump_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SystemExit(f"Yandex dump not found: {path}") from None
    except OSError as exc:
        raise SystemExit(f"Yandex dump is not readable: {path}: {exc}") from None
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Yandex dump is not valid JSON: {path}: {exc.msg}") from None


def _line_topologies_from_dump(payload: object) -> list[YandexLineTopology]:
    if not isinstance(payload, dict):
        return []
    entries = payload.get("entries")
    if not isinstance(entries, list):
        return []
    topologies = []
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("method") != "getLine":
            continue
        entry_payload = entry.get("payload")
        if isinstance(entry_payload, dict):
            topologies.append(parse_line_payload(entry_payload))
    return topologies

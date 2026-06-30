from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path

from route74.cli.common import positive_int
from route74.domain.profiles import PROFILE_KEYS, profile_by_key
from route74.models import now_local
from route74.presenters.support_snapshot import format_support_snapshot
from route74.services.support_snapshot import SupportSnapshot, SupportSnapshotService
from route74.watch_state import DEFAULT_WATCH_STATE_PATH


def register_support_snapshot_command(subparsers: argparse._SubParsersAction) -> None:
    snapshot = subparsers.add_parser("support-snapshot", help="Print a compact operator snapshot.")
    snapshot.add_argument("--profile", choices=PROFILE_KEYS, required=True, help="Profile to diagnose.")
    snapshot.add_argument(
        "--hours",
        type=positive_int,
        default=24,
        help="Bot diagnostics window in hours.",
    )
    snapshot.add_argument(
        "--watch-state-path",
        type=Path,
        default=DEFAULT_WATCH_STATE_PATH,
        help="Persisted watch state JSON path.",
    )
    snapshot.set_defaults(func=cmd_support_snapshot)


def cmd_support_snapshot(args: argparse.Namespace) -> None:
    snapshot = build_support_snapshot(
        args.db,
        profile_key=args.profile,
        hours=args.hours,
        watch_state_path=args.watch_state_path,
    )
    print(format_support_snapshot(snapshot))


def build_support_snapshot(
    db_path: Path,
    *,
    profile_key: str,
    hours: int = 24,
    watch_state_path: Path = DEFAULT_WATCH_STATE_PATH,
    current_time: datetime | None = None,
) -> SupportSnapshot:
    current_time = current_time or now_local()
    service = SupportSnapshotService(
        db_path=db_path,
        watch_state_path=watch_state_path,
        hours=hours,
    )
    return service.build(profile_by_key(profile_key), current_time=current_time)

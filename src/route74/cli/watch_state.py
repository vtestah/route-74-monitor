from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path

from route74.models import now_local
from route74.watch_state import (
    DEFAULT_WATCH_STATE_PATH,
    format_watch_state_summary,
    summarize_watch_state,
)


def register_watch_state_command(subparsers: argparse._SubParsersAction) -> None:
    watch_state = subparsers.add_parser(
        "watch-state",
        help="Summarize persisted web watch state.",
    )
    watch_state.add_argument(
        "--path",
        type=Path,
        default=DEFAULT_WATCH_STATE_PATH,
        help="Watch state JSON path.",
    )
    watch_state.add_argument(
        "--at",
        type=_datetime_arg,
        default=None,
        help="Override the current time for diagnostics.",
    )
    watch_state.set_defaults(func=cmd_watch_state)


def cmd_watch_state(args: argparse.Namespace) -> None:
    current_time = args.at or now_local()
    summary = summarize_watch_state(args.path, current_time)
    print(format_watch_state_summary(summary, str(args.path)))


def _datetime_arg(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected ISO 8601 datetime") from exc
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("expected timezone-aware ISO 8601 datetime")
    return parsed

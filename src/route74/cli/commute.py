from __future__ import annotations

import argparse
from pathlib import Path

from route74.cli.common import profile_from_name, walk_minutes_arg
from route74.services.factory import commute_service
from route74.domain.profiles import EVENING, MORNING, PROFILE_KEYS
from route74.presenters.calculation import format_calculation_explanation
from route74.presenters.commute import format_action_message
from route74.presenters.stats import format_stats_message
from route74.services.stats import StatsService
from route74.watch_state import DEFAULT_WATCH_STATE_PATH


def register_commute_commands(subparsers: argparse._SubParsersAction) -> None:
    explain = subparsers.add_parser("explain", help="Explain how departure timing is calculated.")
    explain.add_argument("--morning-walk", type=walk_minutes_arg, default=MORNING.default_walk_minutes)
    explain.add_argument("--evening-walk", type=walk_minutes_arg, default=EVENING.default_walk_minutes)
    explain.set_defaults(func=cmd_explain)

    commute = subparsers.add_parser("commute", help="Preview the web commute response.")
    commute.add_argument("profile", choices=PROFILE_KEYS, help="Commute profile.")
    commute.add_argument("--walk", type=walk_minutes_arg, default=None, help="Walking minutes to the stop.")
    commute.set_defaults(func=cmd_commute)

    stats = subparsers.add_parser("stats", help="Print route 74 Yandex diagnostics.")
    stats.add_argument("profile", choices=PROFILE_KEYS, help="Commute profile.")
    stats.add_argument("--walk", type=walk_minutes_arg, default=None, help="Walking minutes to the stop.")
    stats.add_argument(
        "--watch-state-path",
        type=Path,
        default=DEFAULT_WATCH_STATE_PATH,
        help="Persisted watch state JSON path.",
    )
    stats.set_defaults(func=cmd_stats)

    predict = subparsers.add_parser("predict", help="Preview Yandex live/history departure decision.")
    predict.add_argument("profile", choices=PROFILE_KEYS, help="Commute profile.")
    predict.add_argument("--walk", type=walk_minutes_arg, default=None, help="Walking minutes to the stop.")
    predict.set_defaults(func=cmd_predict)


def cmd_explain(args: argparse.Namespace) -> None:
    print(format_calculation_explanation(args.morning_walk, args.evening_walk))


def cmd_commute(args: argparse.Namespace) -> None:
    profile = profile_from_name(args.profile)
    walk_minutes = args.walk if args.walk is not None else profile.default_walk_minutes
    service = commute_service(args.db)
    print(format_action_message(service.build_decision(profile, walk_minutes)))


def cmd_stats(args: argparse.Namespace) -> None:
    profile = profile_from_name(args.profile)
    walk_minutes = args.walk if args.walk is not None else profile.default_walk_minutes
    service = commute_service(args.db)
    stats_service = StatsService(service, db_path=args.db, watch_state_path=args.watch_state_path)
    print(format_stats_message(stats_service.build(profile, walk_minutes)))


def cmd_predict(args: argparse.Namespace) -> None:
    cmd_commute(args)

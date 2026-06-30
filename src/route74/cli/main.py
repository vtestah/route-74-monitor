from __future__ import annotations

import argparse
from collections.abc import Sequence

from route74.cli.common import sqlite_db_path
from route74.cli.commute import register_commute_commands
from route74.cli.database import register_database_commands
from route74.cli.stats import register_stats_commands
from route74.cli.watch_state import register_watch_state_command
from route74.cli.version import register_version_command
from route74.cli.yandex import register_yandex_commands
from route74.storage import DEFAULT_DB


def main(argv: Sequence[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    dispatch_command(parser, args)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="route74")
    parser.add_argument("--db", type=sqlite_db_path, default=DEFAULT_DB, help="SQLite database path.")
    subparsers = parser.add_subparsers(dest="command", metavar="command", required=True)
    register_commute_commands(subparsers)
    register_database_commands(subparsers)
    register_watch_state_command(subparsers)
    register_yandex_commands(subparsers)
    register_stats_commands(subparsers)
    register_version_command(subparsers)
    return parser


def dispatch_command(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    handler = getattr(args, "func", None)
    if not callable(handler):
        command = getattr(args, "command", "selected command")
        parser.error(f"command {command!r} is missing a handler")
    handler(args)

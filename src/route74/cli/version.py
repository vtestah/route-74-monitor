from __future__ import annotations

import argparse

from route74.build_info import BuildInfo, format_build_status, load_build_info


def register_version_command(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("version", help="Show package and deployed build version.")
    parser.add_argument("--plain", action="store_true", help="Print only the short build label.")
    parser.set_defaults(func=cmd_version)


def cmd_version(args: argparse.Namespace) -> None:
    info = load_build_info()
    if args.plain:
        print(info.label)
        return
    print(format_cli_version(info))


def format_cli_version(info: BuildInfo) -> str:
    parts = [
        "route74 version",
        f"package={info.package_version}",
        f"commit={info.display_commit or '-'}",
        f"branch={info.branch or '-'}",
        f"dirty={format_build_status(info)}",
        f"source={info.source}",
    ]
    if info.deployed_at:
        parts.append(f"deployed_at={info.deployed_at}")
    if info.built_at:
        parts.append(f"built_at={info.built_at}")
    return " ".join(parts)

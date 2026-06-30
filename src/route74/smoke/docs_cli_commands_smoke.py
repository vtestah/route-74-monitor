from __future__ import annotations

import argparse
import shlex
from collections.abc import Iterable, Iterator
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path

from route74.cli import build_parser


REPO_ROOT = Path(__file__).resolve().parents[3]
DOC_PATHS = (REPO_ROOT / "README.md", *sorted((REPO_ROOT / "docs").glob("*.md")))
GLOBAL_OPTIONS_WITH_VALUES = {"--db"}
GLOBAL_OPTIONS_WITHOUT_VALUES = {"-h", "--help"}
MIN_DOCUMENTED_ROUTE74_INVOCATIONS = 40


def main() -> None:
    _assert_command_extractor_examples()
    parser = build_parser()
    failures = []
    documented_commands: set[str] = set()
    checked = 0
    for path in DOC_PATHS:
        for invocation in _documented_route74_invocations(path):
            checked += 1
            documented_commands.add(invocation.command)
            parse_error = _route74_parse_error(parser, invocation)
            if parse_error is not None:
                failures.append(
                    f"{path.relative_to(REPO_ROOT)}:{invocation.line_number}: "
                    f"invalid route74 {invocation.command!r} arguments: {parse_error}"
                )
    if checked < MIN_DOCUMENTED_ROUTE74_INVOCATIONS:
        failures.append(
            "documented route74 command extractor found too few commands: "
            f"{checked}/{MIN_DOCUMENTED_ROUTE74_INVOCATIONS}"
        )
    missing_commands = sorted(_cli_commands(parser) - documented_commands)
    if missing_commands:
        failures.append(
            "route74 CLI commands need at least one documented fenced example: " + ", ".join(missing_commands)
        )
    if failures:
        details = "\n".join(failures)
        raise AssertionError(f"documented route74 commands must match the CLI parser:\n{details}")
    print(f"OK | docs CLI commands smoke passed invocations={checked}")


def _documented_route74_invocations(path: Path) -> tuple["Route74Invocation", ...]:
    invocations = []
    for block in _fenced_code_blocks(path):
        for line_number, command_line in _logical_shell_lines(block.lines):
            invocation = _route74_invocation(command_line)
            if invocation is not None:
                command, _command_args, route74_args = invocation
                invocations.append(Route74Invocation(line_number, command, route74_args))
    return tuple(invocations)


class Route74Invocation:
    def __init__(
        self,
        line_number: int,
        command: str,
        route74_args: tuple[str, ...],
    ) -> None:
        self.line_number = line_number
        self.command = command
        self.route74_args = route74_args


class FencedCodeBlock:
    def __init__(self, lines: tuple[tuple[int, str], ...]) -> None:
        self.lines = lines


def _fenced_code_blocks(path: Path) -> Iterator[FencedCodeBlock]:
    inside_block = False
    block_lines: list[tuple[int, str]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if line.startswith("```"):
            if inside_block:
                yield FencedCodeBlock(tuple(block_lines))
                block_lines = []
                inside_block = False
            else:
                inside_block = True
            continue
        if inside_block:
            block_lines.append((line_number, line))


def _logical_shell_lines(lines: Iterable[tuple[int, str]]) -> Iterator[tuple[int, str]]:
    start_line: int | None = None
    parts: list[str] = []
    for line_number, line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            if parts and start_line is not None:
                yield start_line, " ".join(parts)
            start_line = None
            parts = []
            continue
        if start_line is None:
            start_line = line_number
        if stripped.endswith("\\"):
            parts.append(stripped[:-1].rstrip())
            continue
        parts.append(stripped)
        yield start_line, " ".join(parts)
        start_line = None
        parts = []
    if parts and start_line is not None:
        yield start_line, " ".join(parts)


def _route74_invocation(command_line: str) -> tuple[str, tuple[str, ...], tuple[str, ...]] | None:
    try:
        tokens = shlex.split(command_line)
    except ValueError:
        return None
    invocation_index = _route74_invocation_index(tokens)
    if invocation_index is None:
        return None
    route74_args = tokens[invocation_index + 1 :]
    command_index = _first_subcommand_index(route74_args)
    if command_index is None:
        return None
    return (
        route74_args[command_index],
        tuple(route74_args[command_index + 1 :]),
        tuple(route74_args),
    )


def _route74_command_name(command_line: str) -> str | None:
    invocation = _route74_invocation(command_line)
    if invocation is None:
        return None
    command, _command_args, _route74_args = invocation
    return command


def _route74_invocation_index(tokens: list[str]) -> int | None:
    for index, token in enumerate(tokens):
        if index > 0 and tokens[index - 1] in {"-u", "--user"}:
            continue
        if _is_route74_executable_token(token):
            return index
    return None


def _is_route74_executable_token(token: str) -> bool:
    value = token.split("=", 1)[-1]
    return value == "route74" or value.endswith("/route74")


def _first_subcommand_index(tokens: list[str]) -> int | None:
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in GLOBAL_OPTIONS_WITH_VALUES:
            index += 2
            continue
        if any(token.startswith(f"{option}=") for option in GLOBAL_OPTIONS_WITH_VALUES):
            index += 1
            continue
        if token in GLOBAL_OPTIONS_WITHOUT_VALUES or token.startswith("-"):
            index += 1
            continue
        return index
    return None


def _route74_parse_error(
    parser: argparse.ArgumentParser,
    invocation: Route74Invocation,
) -> str | None:
    stderr = StringIO()
    try:
        with redirect_stderr(stderr):
            parser.parse_args(invocation.route74_args)
    except SystemExit as exc:
        if exc.code == 0:
            return None
        return " ".join(stderr.getvalue().split()) or f"exited with {exc.code!r}"
    return None


def _cli_commands(parser: argparse.ArgumentParser) -> frozenset[str]:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return frozenset(action.choices)
    raise AssertionError("missing command subparser")


def _assert_command_extractor_examples() -> None:
    _assert_equal(_route74_command_name("route74 --db data/route74.sqlite forecast-health"), "forecast-health")
    _assert_equal(
        _route74_invocation("route74 --db=data/route74.sqlite forecast-health --step-minutes 30"),
        (
            "forecast-health",
            ("--step-minutes", "30"),
            ("--db=data/route74.sqlite", "forecast-health", "--step-minutes", "30"),
        ),
    )
    _assert_equal(
        _route74_command_name("ExecStart=/opt/route74/.venv/bin/route74 --db data/route74.sqlite yandex-collect"),
        "yandex-collect",
    )
    parser = build_parser()
    bad_global_option = Route74Invocation(
        1,
        "forecast-health",
        ("--unknown-global", "forecast-health"),
    )
    _assert_contains(
        _route74_parse_error(parser, bad_global_option) or "",
        "unrecognized arguments: --unknown-global",
    )
    bad_db = Route74Invocation(
        1,
        "forecast-health",
        ("--db", "   ", "forecast-health"),
    )
    _assert_contains(
        _route74_parse_error(parser, bad_db) or "",
        "SQLite database path must not be blank",
    )
    _assert_equal(
        _route74_command_name("sudo -u route74 .venv/bin/python -m playwright install chromium"),
        None,
    )
    _assert_equal("commute" in _cli_commands(parser), True)


def _assert_equal(actual: object, expected: object) -> None:
    if actual != expected:
        raise AssertionError(f"expected {expected!r}, got {actual!r}")


def _assert_contains(actual: str, expected: str) -> None:
    if expected not in actual:
        raise AssertionError(f"expected {expected!r} in {actual!r}")


if __name__ == "__main__":
    main()

from __future__ import annotations

import ast
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
STDIN_READ_CALLS = frozenset({"sys.stdin.read", "sys.stdin.readline", "sys.stdin.readlines"})


def main() -> None:
    _assert_interactive_input_detector()
    failures = []
    for path in _production_module_paths():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        aliases = _import_aliases(tree)
        relative = path.relative_to(PACKAGE_ROOT)
        failures.extend(
            f"{relative.as_posix()}:{line_number} {label}"
            for line_number, label in _interactive_input_violations(tree, aliases)
        )
    if failures:
        details = "\n".join(failures)
        raise AssertionError(f"interactive input boundary failed:\n{details}")
    print("OK | interactive input boundary smoke passed")


def _production_module_paths() -> tuple[Path, ...]:
    paths = []
    for path in sorted(PACKAGE_ROOT.rglob("*.py")):
        parts = path.relative_to(PACKAGE_ROOT).with_suffix("").parts
        if any(part == "smoke" for part in parts) or parts[-1].endswith("_smoke"):
            continue
        paths.append(path)
    return tuple(paths)


def _interactive_input_violations(
    tree: ast.Module,
    aliases: dict[str, str],
) -> tuple[tuple[int, str], ...]:
    violations = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            label = _interactive_input_call_label(node, aliases)
            if label:
                violations.append((node.lineno, label))
        elif isinstance(node, ast.For | ast.AsyncFor):
            if _is_stdin_expression(node.iter, aliases):
                violations.append((node.lineno, "sys.stdin iteration must not block runtime modules"))
    return tuple(sorted(violations))


def _interactive_input_call_label(node: ast.Call, aliases: dict[str, str]) -> str | None:
    call_name = _call_name(node.func, aliases)
    if call_name == "input":
        return "input() must not block runtime modules"
    if call_name == "getpass.getpass":
        return "getpass.getpass() must not block runtime modules"
    if call_name in STDIN_READ_CALLS:
        return f"{call_name}() must not block runtime modules"
    return None


def _import_aliases(tree: ast.Module) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            aliases.update(_module_import_aliases(node))
        elif isinstance(node, ast.ImportFrom):
            aliases.update(_from_import_aliases(node))
    return aliases


def _module_import_aliases(node: ast.Import) -> dict[str, str]:
    aliases = {}
    for alias in node.names:
        if alias.name in {"builtins", "getpass", "sys"}:
            label = alias.asname or alias.name
            aliases[label] = alias.name
    return aliases


def _from_import_aliases(node: ast.ImportFrom) -> dict[str, str]:
    if node.module not in {"builtins", "getpass", "sys"}:
        return {}
    aliases = {}
    for alias in node.names:
        if alias.name == "*":
            continue
        label = alias.asname or alias.name
        aliases[label] = _normalize_call_name(f"{node.module}.{alias.name}")
    return aliases


def _call_name(node: ast.expr, aliases: dict[str, str]) -> str:
    if isinstance(node, ast.Name):
        return aliases.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        parent = _call_name(node.value, aliases)
        return _normalize_call_name(f"{parent}.{node.attr}" if parent else node.attr)
    return ""


def _is_stdin_expression(node: ast.expr, aliases: dict[str, str]) -> bool:
    return _call_name(node, aliases) == "sys.stdin"


def _normalize_call_name(name: str) -> str:
    if name == "builtins.input":
        return "input"
    return name


def _assert_interactive_input_detector() -> None:
    tree = ast.parse(
        "\n".join(
            [
                "import builtins as py_builtins",
                "import getpass as gp",
                "import sys as system",
                "from builtins import input as ask",
                "from getpass import getpass as secret",
                "from sys import stdin",
                "input('token: ')",
                "py_builtins.input('token: ')",
                "ask('token: ')",
                "gp.getpass('token: ')",
                "secret('token: ')",
                "system.stdin.read()",
                "stdin.readline()",
                "for line in system.stdin:",
                "    pass",
            ]
        )
    )
    _assert_equal(
        _labels(tree),
        (
            "input() must not block runtime modules",
            "input() must not block runtime modules",
            "input() must not block runtime modules",
            "getpass.getpass() must not block runtime modules",
            "getpass.getpass() must not block runtime modules",
            "sys.stdin.read() must not block runtime modules",
            "sys.stdin.readline() must not block runtime modules",
            "sys.stdin iteration must not block runtime modules",
        ),
    )


def _labels(tree: ast.Module) -> tuple[str, ...]:
    return tuple(label for _, label in _interactive_input_violations(tree, _import_aliases(tree)))


def _assert_equal(actual: object, expected: object) -> None:
    if actual != expected:
        raise AssertionError(f"expected {expected!r}, got {actual!r}")


if __name__ == "__main__":
    main()

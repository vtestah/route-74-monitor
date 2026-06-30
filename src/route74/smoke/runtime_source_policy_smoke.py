from __future__ import annotations

import argparse
import ast
from pathlib import Path

from route74.cli.main import build_parser


PACKAGE_ROOT = Path(__file__).resolve().parents[1]

REMOVED_RUNTIME_MODULES = (
    "route74.official_schedule",
    "route74.domain.schedule",
    "route74.services.planned_schedule",
)
REMOVED_RUNTIME_PATHS = (
    "official_schedule.py",
    "domain/schedule.py",
    "services/planned_schedule.py",
)
REMOVED_CLI_COMMANDS = frozenset({"official"})


def main() -> None:
    _assert_import_detector()
    _assert_removed_runtime_files_absent()
    _assert_removed_runtime_modules_not_imported()
    _assert_removed_cli_commands_absent()
    print("OK | runtime source policy smoke passed")


def _assert_removed_runtime_files_absent() -> None:
    existing = sorted(path for path in REMOVED_RUNTIME_PATHS if (PACKAGE_ROOT / path).exists())
    if existing:
        raise AssertionError(f"removed runtime source modules must stay absent: {', '.join(existing)}")


def _assert_removed_runtime_modules_not_imported() -> None:
    failures = []
    for path in _production_module_paths():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imports = _removed_runtime_imports(tree, package_name=_package_name(path))
        if imports:
            relative = path.relative_to(PACKAGE_ROOT)
            failures.append(f"{relative}: {', '.join(imports)}")
    if failures:
        details = "\n".join(failures)
        raise AssertionError(f"production code must not import removed runtime source modules:\n{details}")


def _assert_removed_cli_commands_absent() -> None:
    removed = sorted(REMOVED_CLI_COMMANDS & _cli_commands(build_parser()))
    if removed:
        raise AssertionError(f"removed CLI commands must stay absent: {', '.join(removed)}")


def _production_module_paths() -> tuple[Path, ...]:
    paths = []
    for path in sorted(PACKAGE_ROOT.rglob("*.py")):
        parts = path.relative_to(PACKAGE_ROOT).with_suffix("").parts
        if any(part == "smoke" for part in parts) or parts[-1].endswith("_smoke"):
            continue
        paths.append(path)
    return tuple(paths)


def _module_name(path: Path) -> str:
    parts = path.relative_to(PACKAGE_ROOT).with_suffix("").parts
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(("route74", *parts))


def _package_name(path: Path) -> str:
    module_name = _module_name(path)
    if path.name == "__init__.py":
        return module_name
    return module_name.rsplit(".", 1)[0]


def _removed_runtime_imports(tree: ast.Module, *, package_name: str) -> tuple[str, ...]:
    imports = []
    aliases = _import_aliases(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names if _is_removed_runtime_module(alias.name))
        elif isinstance(node, ast.ImportFrom):
            imports.extend(_removed_import_from_labels(node, package_name=package_name))
        elif isinstance(node, ast.Call):
            imports.extend(_removed_dynamic_import_labels(node, aliases))
    return tuple(imports)


def _removed_import_from_labels(node: ast.ImportFrom, *, package_name: str) -> tuple[str, ...]:
    base = _import_from_base(node, package_name=package_name)
    if _is_removed_runtime_module(base):
        return (base,)
    candidates = [base] if base else []
    candidates.extend(f"{base}.{alias.name}" for alias in node.names if base and alias.name != "*")
    return tuple(candidate for candidate in candidates if _is_removed_runtime_module(candidate))


def _import_from_base(node: ast.ImportFrom, *, package_name: str) -> str:
    if node.level == 0:
        return node.module or ""

    package_parts = package_name.split(".")
    parent_levels = node.level - 1
    if parent_levels >= len(package_parts):
        return ""
    parts = package_parts[: len(package_parts) - parent_levels]
    if node.module:
        parts.extend(node.module.split("."))
    return ".".join(parts)


def _is_removed_runtime_module(name: str) -> bool:
    return any(name == module or name.startswith(f"{module}.") for module in REMOVED_RUNTIME_MODULES)


def _removed_dynamic_import_labels(node: ast.Call, aliases: dict[str, str]) -> tuple[str, ...]:
    call_name = _call_name(node.func, aliases)
    if call_name not in {"__import__", "builtins.__import__", "importlib.import_module"}:
        return ()
    if not node.args:
        return ()
    module_name = _string_literal(node.args[0])
    if module_name is None or not _is_removed_runtime_module(module_name):
        return ()
    return (module_name,)


def _import_aliases(tree: ast.Module) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in {"builtins", "importlib"}:
                    aliases[alias.asname or alias.name] = alias.name
        elif isinstance(node, ast.ImportFrom) and node.module in {"builtins", "importlib"}:
            for alias in node.names:
                if alias.name == "*":
                    continue
                aliases[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    return aliases


def _call_name(node: ast.expr, aliases: dict[str, str]) -> str:
    if isinstance(node, ast.Name):
        return aliases.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        parent = _call_name(node.value, aliases)
        return f"{parent}.{node.attr}" if parent else node.attr
    return ""


def _string_literal(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _cli_commands(parser: argparse.ArgumentParser) -> frozenset[str]:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return frozenset(action.choices)
    raise AssertionError("missing command subparser")


def _assert_import_detector() -> None:
    tree = ast.parse(
        "\n".join(
            [
                "import importlib",
                "import importlib as module_loader",
                "from importlib import import_module as load_module",
                "from builtins import __import__ as import_any",
                "import route74.official_schedule",
                "from route74 import official_schedule",
                "from route74.domain import schedule",
                "from route74.services.planned_schedule import PlannedSchedule",
                "from . import schedule",
                "from ..services import planned_schedule",
                "from route74.sources.yandex.constants import YANDEX_LINE_ID",
                "importlib.import_module('route74.official_schedule')",
                "module_loader.import_module('route74.domain.schedule')",
                "load_module('route74.services.planned_schedule')",
                "__import__('route74.official_schedule')",
                "import_any('route74.domain.schedule')",
                "importlib.import_module('route74.sources.yandex.constants')",
            ]
        )
    )
    _assert_equal(
        _removed_runtime_imports(tree, package_name="route74.domain"),
        (
            "route74.official_schedule",
            "route74.official_schedule",
            "route74.domain.schedule",
            "route74.services.planned_schedule",
            "route74.domain.schedule",
            "route74.services.planned_schedule",
            "route74.official_schedule",
            "route74.domain.schedule",
            "route74.services.planned_schedule",
            "route74.official_schedule",
            "route74.domain.schedule",
        ),
    )


def _assert_equal(actual: object, expected: object) -> None:
    if actual != expected:
        raise AssertionError(f"expected {expected!r}, got {actual!r}")


if __name__ == "__main__":
    main()

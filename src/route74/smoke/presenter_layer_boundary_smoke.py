from __future__ import annotations

import ast
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PRESENTERS_ROOT = PACKAGE_ROOT / "presenters"
FORBIDDEN_PRESENTER_IMPORT_PREFIXES = (
    "route74.web",
    "route74.cli",
    "route74.dashboard",
    "route74.services",
    "route74.storage",
)
DYNAMIC_IMPORT_FUNCTIONS = frozenset(
    {
        "__import__",
        "builtins.__import__",
        "importlib.import_module",
    }
)


def main() -> None:
    _assert_presenter_import_detector()
    failures = []
    for path in _presenter_python_files():
        relative_path = path.relative_to(PACKAGE_ROOT)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        package_name = _package_name(path)
        failures.extend(
            f"{relative_path.as_posix()}:{line_number} {label}"
            for line_number, label in _presenter_import_violations(tree, package_name=package_name)
        )
    if failures:
        details = "\n".join(failures)
        raise AssertionError(f"presenter layer boundary failed:\n{details}")
    print("OK | presenter layer boundary smoke passed")


def _presenter_python_files() -> tuple[Path, ...]:
    return tuple(path for path in sorted(PRESENTERS_ROOT.rglob("*.py")) if not _is_smoke_module(path))


def _is_smoke_module(path: Path) -> bool:
    relative = path.relative_to(PACKAGE_ROOT)
    return any(part == "smoke" for part in relative.parts) or relative.name.endswith("_smoke.py")


def _presenter_import_violations(tree: ast.Module, *, package_name: str) -> tuple[tuple[int, str], ...]:
    aliases = _import_aliases(tree)
    violations = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _is_forbidden_presenter_import(alias.name):
                    violations.append((node.lineno, f"presenter imports runtime/data layer {alias.name}"))
        elif isinstance(node, ast.ImportFrom):
            for module_name in _import_from_module_names(node, package_name=package_name):
                if _is_forbidden_presenter_import(module_name):
                    violations.append((node.lineno, f"presenter imports runtime/data layer {module_name}"))
        elif isinstance(node, ast.Call):
            module_name = _dynamic_import_module_name(node, aliases)
            if module_name and _is_forbidden_presenter_import(module_name):
                violations.append((node.lineno, f"presenter dynamically imports runtime/data layer {module_name}"))
    return tuple(violations)


def _import_from_module_names(node: ast.ImportFrom, *, package_name: str) -> tuple[str, ...]:
    base = _import_from_base(node, package_name=package_name)
    if not base:
        return ()
    names = []
    for alias in node.names:
        if alias.name == "*":
            names.append(f"{base}.*")
        else:
            candidate = f"{base}.{alias.name}"
            names.append(candidate if _is_forbidden_presenter_import(candidate) else base)
    return tuple(names)


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


def _package_name(path: Path) -> str:
    relative = path.relative_to(PACKAGE_ROOT).with_suffix("")
    parts = ("route74", *relative.parts)
    if path.name == "__init__.py":
        return ".".join(parts[:-1])
    return ".".join(parts[:-1])


def _is_forbidden_presenter_import(module_name: str) -> bool:
    return any(
        module_name == prefix or module_name.startswith(f"{prefix}.") for prefix in FORBIDDEN_PRESENTER_IMPORT_PREFIXES
    )


def _dynamic_import_module_name(node: ast.Call, aliases: dict[str, str]) -> str:
    if _call_name(node.func, aliases) not in DYNAMIC_IMPORT_FUNCTIONS:
        return ""
    if not node.args:
        return ""
    module_name = node.args[0]
    if isinstance(module_name, ast.Constant) and isinstance(module_name.value, str):
        return module_name.value
    return ""


def _import_aliases(tree: ast.Module) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in {"builtins", "importlib"}:
                    aliases[alias.asname or alias.name] = alias.name
        elif isinstance(node, ast.ImportFrom):
            aliases.update(_import_from_aliases(node))
    return aliases


def _import_from_aliases(node: ast.ImportFrom) -> dict[str, str]:
    if node.module not in {"builtins", "importlib"}:
        return {}
    aliases = {}
    for alias in node.names:
        if alias.name == "*":
            continue
        label = alias.asname or alias.name
        if node.module == "builtins" and alias.name == "__import__":
            aliases[label] = "builtins.__import__"
        elif node.module == "importlib" and alias.name == "import_module":
            aliases[label] = "importlib.import_module"
    return aliases


def _call_name(node: ast.expr, aliases: dict[str, str]) -> str:
    if isinstance(node, ast.Name):
        return aliases.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        parent = _call_name(node.value, aliases)
        return f"{parent}.{node.attr}" if parent else node.attr
    return ""


def _assert_presenter_import_detector() -> None:
    tree = ast.parse(
        "\n".join(
            [
                "import importlib",
                "import importlib as imports",
                "import builtins",
                "import builtins as py_builtins",
                "from importlib import import_module as load_module",
                "from builtins import __import__ as import_builtin",
                "from route74.domain.commute import DepartureDecision",
                "from route74.presenters.commute_lines import headline",
                "from route74.sources.yandex.models import YandexSourceStatus",
                "from route74.services.stats import StatsSnapshot",
                "from route74.storage import connect",
                "from route74.web.app import create_app",
                "import route74.cli.main",
                "from route74.dashboard import app",
                "from .timeline import format_timeline_block",
                "from ..services import departure",
                "importlib.import_module('route74.domain.commute')",
                "load_module('route74.sources.yandex.models')",
                "load_module(module_name)",
                "importlib.import_module('route74.services.stats')",
                "imports.import_module('route74.storage')",
                "__import__('route74.web.app')",
                "builtins.__import__('route74.cli.main')",
                "py_builtins.__import__('route74.dashboard.app')",
                "import_builtin('route74.services.departure')",
            ]
        )
    )
    _assert_equal(
        _labels(tree, "route74.presenters"),
        (
            "presenter imports runtime/data layer route74.services.stats.StatsSnapshot",
            "presenter imports runtime/data layer route74.storage.connect",
            "presenter imports runtime/data layer route74.web.app.create_app",
            "presenter imports runtime/data layer route74.cli.main",
            "presenter imports runtime/data layer route74.dashboard.app",
            "presenter imports runtime/data layer route74.services.departure",
            "presenter dynamically imports runtime/data layer route74.services.stats",
            "presenter dynamically imports runtime/data layer route74.storage",
            "presenter dynamically imports runtime/data layer route74.web.app",
            "presenter dynamically imports runtime/data layer route74.cli.main",
            "presenter dynamically imports runtime/data layer route74.dashboard.app",
            "presenter dynamically imports runtime/data layer route74.services.departure",
        ),
    )


def _labels(tree: ast.Module, package_name: str) -> tuple[str, ...]:
    return tuple(label for _, label in _presenter_import_violations(tree, package_name=package_name))


def _assert_equal(actual: object, expected: object) -> None:
    if actual != expected:
        raise AssertionError(f"expected {expected!r}, got {actual!r}")


if __name__ == "__main__":
    main()

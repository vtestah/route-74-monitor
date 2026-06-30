from __future__ import annotations

import ast
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
SOURCES_ROOT = PACKAGE_ROOT / "sources"
FORBIDDEN_SOURCE_IMPORT_PREFIXES = (
    "route74.web",
    "route74.cli",
    "route74.dashboard",
    "route74.presenters",
    "route74.services",
)
DYNAMIC_IMPORT_FUNCTIONS = frozenset(
    {
        "__import__",
        "builtins.__import__",
        "importlib.import_module",
    }
)


def main() -> None:
    _assert_source_import_detector()
    failures = []
    for path in _source_python_files():
        relative_path = path.relative_to(PACKAGE_ROOT)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        failures.extend(
            f"{relative_path.as_posix()}:{line_number} {label}"
            for line_number, label in _source_import_violations(tree, relative_path)
        )
    if failures:
        details = "\n".join(failures)
        raise AssertionError(f"source layer boundary failed:\n{details}")
    print("OK | source layer boundary smoke passed")


def _source_python_files() -> tuple[Path, ...]:
    return tuple(
        path for path in sorted(SOURCES_ROOT.rglob("*.py")) if not _is_smoke_module(path.relative_to(PACKAGE_ROOT))
    )


def _is_smoke_module(path: Path) -> bool:
    return any(part == "smoke" for part in path.parts) or path.name.endswith("_smoke.py")


def _source_import_violations(tree: ast.Module, relative_path: Path) -> tuple[tuple[int, str], ...]:
    aliases = _import_aliases(tree)
    violations = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _is_forbidden_source_import(alias.name):
                    violations.append((node.lineno, f"source imports application layer {alias.name}"))
        elif isinstance(node, ast.ImportFrom):
            for module_name in _import_from_module_names(node, relative_path):
                if _is_forbidden_source_import(module_name):
                    violations.append((node.lineno, f"source imports application layer {module_name}"))
        elif isinstance(node, ast.Call):
            module_name = _dynamic_import_module_name(node, aliases)
            if module_name and _is_forbidden_source_import(module_name):
                violations.append((node.lineno, f"source dynamically imports application layer {module_name}"))
    return tuple(violations)


def _import_from_module_names(node: ast.ImportFrom, relative_path: Path) -> tuple[str, ...]:
    module = _import_from_module(node, relative_path)
    if not module:
        return ()
    names = []
    for alias in node.names:
        if alias.name == "*":
            names.append(f"{module}.*")
        else:
            candidate = f"{module}.{alias.name}"
            names.append(candidate if _is_forbidden_source_import(candidate) else module)
    return tuple(names)


def _import_from_module(node: ast.ImportFrom, relative_path: Path) -> str:
    if node.level == 0:
        return node.module or ""

    package = _source_package_parts(relative_path)
    parent_count = node.level - 1
    if parent_count >= len(package):
        return ""
    base = package[: len(package) - parent_count]
    module_parts = tuple((node.module or "").split(".")) if node.module else ()
    return ".".join((*base, *module_parts))


def _source_package_parts(relative_path: Path) -> tuple[str, ...]:
    module_parts = ("route74", *relative_path.with_suffix("").parts)
    return module_parts[:-1]


def _is_forbidden_source_import(module_name: str) -> bool:
    return any(
        module_name == prefix or module_name.startswith(f"{prefix}.") for prefix in FORBIDDEN_SOURCE_IMPORT_PREFIXES
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


def _assert_source_import_detector() -> None:
    tree = ast.parse(
        "\n".join(
            [
                "import importlib",
                "import importlib as imports",
                "import builtins",
                "import builtins as py_builtins",
                "from importlib import import_module as load_module",
                "from builtins import __import__ as import_builtin",
                "from route74.domain.commute import CommuteProfile",
                "from route74.models import NOVOSIBIRSK_TZ",
                "from route74.sources.yandex import models",
                "from route74.storage import connect_readonly",
                "from route74.services import commute",
                "from route74.presenters.commute import render_commute",
                "from route74 import dashboard",
                "import route74.web.app as web_app",
                "from .models import YandexLiveForecast",
                "from ...cli import yandex_collect",
                "importlib.import_module('route74.domain.eta')",
                "load_module(module_name)",
                "importlib.import_module('route74.services.commute')",
                "imports.import_module('route74.presenters.stats')",
                "__import__('route74.web.app')",
                "builtins.__import__('route74.dashboard.runtime')",
                "py_builtins.__import__('route74')",
                "import_builtin('route74.cli.yandex_collect')",
            ]
        )
    )
    _assert_equal(
        _labels(tree, "sources/yandex/transport.py"),
        (
            "source imports application layer route74.services.commute",
            "source imports application layer route74.presenters.commute.render_commute",
            "source imports application layer route74.dashboard",
            "source imports application layer route74.web.app",
            "source imports application layer route74.cli.yandex_collect",
            "source dynamically imports application layer route74.services.commute",
            "source dynamically imports application layer route74.presenters.stats",
            "source dynamically imports application layer route74.web.app",
            "source dynamically imports application layer route74.dashboard.runtime",
            "source dynamically imports application layer route74.cli.yandex_collect",
        ),
    )


def _labels(tree: ast.Module, path: str) -> tuple[str, ...]:
    return tuple(label for _, label in _source_import_violations(tree, Path(path)))


def _assert_equal(actual: object, expected: object) -> None:
    if actual != expected:
        raise AssertionError(f"expected {expected!r}, got {actual!r}")


if __name__ == "__main__":
    main()

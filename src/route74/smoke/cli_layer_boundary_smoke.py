from __future__ import annotations

import ast
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
CLI_ROOT = PACKAGE_ROOT / "cli"
FORBIDDEN_CLI_IMPORT_PREFIXES = (
    "route74.web",
    "route74.dashboard",
)
DYNAMIC_IMPORT_FUNCTIONS = frozenset(
    {
        "__import__",
        "builtins.__import__",
        "importlib.import_module",
    }
)


def main() -> None:
    _assert_cli_import_detector()
    failures = []
    for path in _cli_python_files():
        relative_path = path.relative_to(PACKAGE_ROOT)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        package_name = _package_name(path)
        failures.extend(
            f"{relative_path.as_posix()}:{line_number} {label}"
            for line_number, label in _cli_import_violations(tree, package_name=package_name)
        )
    if failures:
        details = "\n".join(failures)
        raise AssertionError(f"CLI layer boundary failed:\n{details}")
    print("OK | CLI layer boundary smoke passed")


def _cli_python_files() -> tuple[Path, ...]:
    return tuple(path for path in sorted(CLI_ROOT.rglob("*.py")) if not _is_smoke_module(path))


def _is_smoke_module(path: Path) -> bool:
    relative = path.relative_to(PACKAGE_ROOT)
    return any(part == "smoke" for part in relative.parts) or relative.name.endswith("_smoke.py")


def _cli_import_violations(tree: ast.Module, *, package_name: str) -> tuple[tuple[int, str], ...]:
    aliases = _import_aliases(tree)
    violations = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _is_forbidden_cli_import(alias.name):
                    violations.append((node.lineno, f"CLI imports runtime transport layer {alias.name}"))
        elif isinstance(node, ast.ImportFrom):
            for module_name in _import_from_module_names(node, package_name=package_name):
                if _is_forbidden_cli_import(module_name):
                    violations.append((node.lineno, f"CLI imports runtime transport layer {module_name}"))
        elif isinstance(node, ast.Call):
            module_name = _dynamic_import_module_name(node, aliases)
            if module_name and _is_forbidden_cli_import(module_name):
                violations.append((node.lineno, f"CLI dynamically imports runtime transport layer {module_name}"))
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
            names.append(candidate if _is_forbidden_cli_import(candidate) else base)
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


def _is_forbidden_cli_import(module_name: str) -> bool:
    return any(
        module_name == prefix or module_name.startswith(f"{prefix}.") for prefix in FORBIDDEN_CLI_IMPORT_PREFIXES
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


def _assert_cli_import_detector() -> None:
    tree = ast.parse(
        "\n".join(
            [
                "import importlib",
                "import importlib as imports",
                "import builtins",
                "import builtins as py_builtins",
                "from importlib import import_module as load_module",
                "from builtins import __import__ as import_builtin",
                "from route74.services import commute",
                "from route74.storage import connect",
                "from route74 import web",
                "from route74.web import app",
                "import route74.web.runtime as web_runtime",
                "from route74.dashboard.runtime import DashboardRuntime",
                "from .common import positive_int",
                "from ..web import runtime",
                "importlib.import_module('route74.services.commute')",
                "load_module('route74.storage')",
                "load_module(module_name)",
                "importlib.import_module('route74.web.runtime')",
                "imports.import_module('route74.dashboard.runtime')",
                "__import__('route74.web.app')",
                "builtins.__import__('route74.dashboard')",
                "py_builtins.__import__('route74.web')",
                "import_builtin('route74.dashboard.app')",
            ]
        )
    )
    _assert_equal(
        _labels(tree, "route74.cli"),
        (
            "CLI imports runtime transport layer route74.web",
            "CLI imports runtime transport layer route74.web.app",
            "CLI imports runtime transport layer route74.web.runtime",
            "CLI imports runtime transport layer route74.dashboard.runtime.DashboardRuntime",
            "CLI imports runtime transport layer route74.web.runtime",
            "CLI dynamically imports runtime transport layer route74.web.runtime",
            "CLI dynamically imports runtime transport layer route74.dashboard.runtime",
            "CLI dynamically imports runtime transport layer route74.web.app",
            "CLI dynamically imports runtime transport layer route74.dashboard",
            "CLI dynamically imports runtime transport layer route74.web",
            "CLI dynamically imports runtime transport layer route74.dashboard.app",
        ),
    )


def _labels(tree: ast.Module, package_name: str) -> tuple[str, ...]:
    return tuple(label for _, label in _cli_import_violations(tree, package_name=package_name))


def _assert_equal(actual: object, expected: object) -> None:
    if actual != expected:
        raise AssertionError(f"expected {expected!r}, got {actual!r}")


if __name__ == "__main__":
    main()

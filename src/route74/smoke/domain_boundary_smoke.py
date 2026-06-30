from __future__ import annotations

import ast
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
DOMAIN_ROOT = PACKAGE_ROOT / "domain"
ALLOWED_DOMAIN_IMPORT_PREFIXES = (
    "route74.domain",
    "route74.models",
)
ALLOWED_DOMAIN_IMPORTS = frozenset({"route74.sources.yandex.models"})
DYNAMIC_IMPORT_FUNCTIONS = frozenset(
    {
        "__import__",
        "builtins.__import__",
        "importlib.import_module",
    }
)


def main() -> None:
    _assert_domain_import_detector()
    failures = []
    for path in _domain_python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        relative_path = path.relative_to(PACKAGE_ROOT)
        failures.extend(
            f"{relative_path.as_posix()}:{line_number} {label}"
            for line_number, label in _domain_import_violations(tree)
        )
    if failures:
        details = "\n".join(failures)
        raise AssertionError(f"domain import boundary failed:\n{details}")
    print("OK | domain boundary smoke passed")


def _domain_python_files() -> tuple[Path, ...]:
    return tuple(path for path in sorted(DOMAIN_ROOT.glob("*.py")) if path.name != "__init__.py")


def _domain_import_violations(tree: ast.Module) -> tuple[tuple[int, str], ...]:
    aliases = _import_aliases(tree)
    violations = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _is_route74_module(alias.name) and not _is_allowed_domain_import(alias.name):
                    violations.append((node.lineno, f"domain imports outer layer {alias.name}"))
        elif isinstance(node, ast.ImportFrom):
            if node.level > 1:
                violations.append((node.lineno, "domain relative import escapes domain package"))
                continue
            for module_name in _import_from_module_names(node):
                if _is_route74_module(module_name) and not _is_allowed_domain_import(module_name):
                    violations.append((node.lineno, f"domain imports outer layer {module_name}"))
        elif isinstance(node, ast.Call):
            module_name = _dynamic_import_module_name(node, aliases)
            if module_name and _is_route74_module(module_name) and not _is_allowed_domain_import(module_name):
                violations.append((node.lineno, f"domain dynamically imports outer layer {module_name}"))
    return tuple(violations)


def _import_from_module_names(node: ast.ImportFrom) -> tuple[str, ...]:
    if node.level:
        return ()
    module = node.module or ""
    names = []
    for alias in node.names:
        if alias.name == "*":
            names.append(f"{module}.*")
        elif _is_allowed_domain_import(module):
            names.append(module)
        else:
            names.append(f"{module}.{alias.name}" if module else alias.name)
    return tuple(names)


def _is_allowed_domain_import(module_name: str) -> bool:
    if module_name in ALLOWED_DOMAIN_IMPORTS:
        return True
    return any(
        module_name == prefix or module_name.startswith(f"{prefix}.") for prefix in ALLOWED_DOMAIN_IMPORT_PREFIXES
    )


def _is_route74_module(module_name: str) -> bool:
    return module_name == "route74" or module_name.startswith("route74.")


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


def _assert_domain_import_detector() -> None:
    tree = ast.parse(
        "\n".join(
            [
                "import importlib",
                "import importlib as imports",
                "import builtins",
                "import builtins as py_builtins",
                "from importlib import import_module as load_module",
                "from builtins import __import__ as import_builtin",
                "from route74.domain.eta import EtaConsensus",
                "from route74.models import NOVOSIBIRSK_TZ",
                "from route74.sources.yandex.models import YandexLiveForecast",
                "from route74.sources.yandex import models",
                "from route74.services import commute",
                "from route74.storage.connection import connect",
                "from route74.web.app import create_app",
                "from route74.sources.yandex import transport",
                "from .eta import EtaConsensus as LocalEtaConsensus",
                "from ..services import commute as relative_commute",
                "importlib.import_module('route74.domain.eta')",
                "load_module('route74.models')",
                "importlib.import_module('route74.sources.yandex.models')",
                "load_module(module_name)",
                "importlib.import_module('route74.services.commute')",
                "imports.import_module('route74.storage.connection')",
                "__import__('route74.web.app')",
                "builtins.__import__('route74.sources.yandex.transport')",
                "py_builtins.__import__('route74')",
                "import_builtin('route74.cli.main')",
            ]
        )
    )
    _assert_equal(
        _labels(tree),
        (
            "domain imports outer layer route74.services.commute",
            "domain imports outer layer route74.storage.connection.connect",
            "domain imports outer layer route74.web.app.create_app",
            "domain imports outer layer route74.sources.yandex.transport",
            "domain relative import escapes domain package",
            "domain dynamically imports outer layer route74.services.commute",
            "domain dynamically imports outer layer route74.storage.connection",
            "domain dynamically imports outer layer route74.web.app",
            "domain dynamically imports outer layer route74.sources.yandex.transport",
            "domain dynamically imports outer layer route74",
            "domain dynamically imports outer layer route74.cli.main",
        ),
    )


def _labels(tree: ast.Module) -> tuple[str, ...]:
    return tuple(label for _, label in _domain_import_violations(tree))


def _assert_equal(actual: object, expected: object) -> None:
    if actual != expected:
        raise AssertionError(f"expected {expected!r}, got {actual!r}")


if __name__ == "__main__":
    main()

from __future__ import annotations

import ast
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
SQLITE_IMPORT_ALLOWLIST = frozenset(
    {
        "dashboard/data.py",
        "services/prediction_engine.py",
        "services/stats.py",
        "services/yandex_telemetry.py",
        "sources/yandex/cache.py",
    }
)
SQLITE_CONNECT_ALLOWLIST = frozenset(
    {
        "storage/connection.py",
        "storage/db_admin.py",
    }
)


def main() -> None:
    _assert_sqlite_boundary_detector()
    failures = []
    for path in _production_python_files():
        relative_path = path.relative_to(PACKAGE_ROOT)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        failures.extend(
            f"{relative_path.as_posix()}:{line_number} {label}"
            for line_number, label in _sqlite_boundary_violations(tree, relative_path)
        )
    if failures:
        details = "\n".join(failures)
        raise AssertionError(f"sqlite boundary contract failed:\n{details}")
    print("OK | sqlite boundary smoke passed")


def _production_python_files() -> tuple[Path, ...]:
    paths = []
    for path in sorted(PACKAGE_ROOT.rglob("*.py")):
        relative = path.relative_to(PACKAGE_ROOT)
        if _is_smoke_module(relative):
            continue
        paths.append(path)
    return tuple(paths)


def _is_smoke_module(path: Path) -> bool:
    return any(part == "smoke" for part in path.parts) or path.name.endswith("_smoke.py")


def _sqlite_boundary_violations(tree: ast.Module, relative_path: Path) -> tuple[tuple[int, str], ...]:
    aliases = _sqlite_aliases(tree)
    violations = []
    for node in ast.walk(tree):
        if _is_sqlite_import(node) and not _sqlite_import_allowed(relative_path):
            violations.append((node.lineno, "sqlite3 imports must stay in storage or explicit readers"))
        if (
            isinstance(node, ast.Call)
            and _is_sqlite_connect_call(node, aliases)
            and relative_path.as_posix() not in SQLITE_CONNECT_ALLOWLIST
        ):
            violations.append((node.lineno, "sqlite3.connect must stay behind storage connection helpers"))
    return tuple(violations)


def _sqlite_import_allowed(path: Path) -> bool:
    return _is_storage_module(path) or path.as_posix() in SQLITE_IMPORT_ALLOWLIST


def _is_storage_module(path: Path) -> bool:
    return bool(path.parts) and path.parts[0] == "storage"


def _is_sqlite_import(node: ast.AST) -> bool:
    if isinstance(node, ast.Import):
        return any(alias.name == "sqlite3" for alias in node.names)
    return isinstance(node, ast.ImportFrom) and node.module == "sqlite3"


def _is_sqlite_connect_call(node: ast.Call, aliases: dict[str, str]) -> bool:
    return _call_name(node.func, aliases) == "sqlite3.connect"


def _sqlite_aliases(tree: ast.Module) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            aliases.update(_sqlite_module_aliases(node))
        elif isinstance(node, ast.ImportFrom):
            aliases.update(_sqlite_from_aliases(node))
    return aliases


def _sqlite_module_aliases(node: ast.Import) -> dict[str, str]:
    aliases = {}
    for alias in node.names:
        if alias.name == "sqlite3":
            aliases[alias.asname or alias.name] = "sqlite3"
    return aliases


def _sqlite_from_aliases(node: ast.ImportFrom) -> dict[str, str]:
    if node.module != "sqlite3":
        return {}
    aliases = {}
    for alias in node.names:
        if alias.name in {"*", "connect"}:
            aliases[alias.asname or "connect"] = "sqlite3.connect"
    return aliases


def _call_name(node: ast.expr, aliases: dict[str, str]) -> str:
    if isinstance(node, ast.Name):
        return aliases.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        parent = _call_name(node.value, aliases)
        return f"{parent}.{node.attr}" if parent else node.attr
    return ""


def _assert_sqlite_boundary_detector() -> None:
    tree = ast.parse(
        "\n".join(
            [
                "import sqlite3",
                "import sqlite3 as sql_db",
                "from sqlite3 import Row",
                "from sqlite3 import connect as raw_connect",
                "from sqlite3 import *",
                "sqlite3.connect(path)",
                "sql_db.connect(path)",
                "raw_connect(path)",
                "connect(path)",
            ]
        )
    )
    _assert_equal(_labels(tree, "storage/connection.py"), ())
    _assert_equal(
        _labels(tree, "services/stats.py"),
        (
            "sqlite3.connect must stay behind storage connection helpers",
            "sqlite3.connect must stay behind storage connection helpers",
            "sqlite3.connect must stay behind storage connection helpers",
            "sqlite3.connect must stay behind storage connection helpers",
        ),
    )
    _assert_equal(
        _labels(tree, "domain/commute.py"),
        (
            "sqlite3 imports must stay in storage or explicit readers",
            "sqlite3 imports must stay in storage or explicit readers",
            "sqlite3 imports must stay in storage or explicit readers",
            "sqlite3 imports must stay in storage or explicit readers",
            "sqlite3 imports must stay in storage or explicit readers",
            "sqlite3.connect must stay behind storage connection helpers",
            "sqlite3.connect must stay behind storage connection helpers",
            "sqlite3.connect must stay behind storage connection helpers",
            "sqlite3.connect must stay behind storage connection helpers",
        ),
    )
    _assert_equal(
        _labels(tree, "sources/yandex/cache.py"),
        (
            "sqlite3.connect must stay behind storage connection helpers",
            "sqlite3.connect must stay behind storage connection helpers",
            "sqlite3.connect must stay behind storage connection helpers",
            "sqlite3.connect must stay behind storage connection helpers",
        ),
    )


def _labels(tree: ast.Module, path: str) -> tuple[str, ...]:
    return tuple(label for _, label in _sqlite_boundary_violations(tree, Path(path)))


def _assert_equal(actual: object, expected: object) -> None:
    if actual != expected:
        raise AssertionError(f"expected {expected!r}, got {actual!r}")


if __name__ == "__main__":
    main()

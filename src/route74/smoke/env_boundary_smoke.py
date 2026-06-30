from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]

_ALLOWED_ENV_ACCESS_MODULES = frozenset(
    {
        "env.py",
        "sources/yandex/browser_client.py",
        "sources/yandex/browser_rate_limit.py",
    }
)


@dataclass(frozen=True)
class EnvAccess:
    line_number: int
    label: str


def main() -> None:
    _assert_env_access_detector()
    failures = []
    for path in _production_python_files():
        relative_path = path.relative_to(PACKAGE_ROOT).as_posix()
        if relative_path in _ALLOWED_ENV_ACCESS_MODULES:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for access in _env_accesses(tree):
            failures.append(f"{relative_path}:{access.line_number} uses {access.label}")
    if failures:
        details = "\n".join(failures)
        raise AssertionError(f"environment access boundary failed:\n{details}")
    print("OK | environment boundary smoke passed")


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


def _env_accesses(tree: ast.Module) -> tuple[EnvAccess, ...]:
    os_aliases = _imported_os_aliases(tree)
    getenv_aliases = _imported_from_os_names(tree, "getenv")
    environ_aliases = _imported_from_os_names(tree, "environ")
    accesses = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            if node.value.id in os_aliases and node.attr in {"getenv", "environ"}:
                accesses.append(EnvAccess(node.lineno, f"{node.value.id}.{node.attr}"))
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            if node.id in getenv_aliases:
                accesses.append(EnvAccess(node.lineno, node.id))
            elif node.id in environ_aliases:
                accesses.append(EnvAccess(node.lineno, node.id))
    return tuple(accesses)


def _imported_os_aliases(tree: ast.Module) -> frozenset[str]:
    aliases = {"os"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            aliases.update(alias.asname or alias.name for alias in node.names if alias.name == "os")
    return frozenset(aliases)


def _imported_from_os_names(tree: ast.Module, name: str) -> frozenset[str]:
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "os":
            names.update(alias.asname or alias.name for alias in node.names if alias.name == name)
            if any(alias.name == "*" for alias in node.names):
                names.add(name)
    return frozenset(names)


def _assert_env_access_detector() -> None:
    tree = ast.parse(
        "\n".join(
            [
                "import os",
                "import os as operating_system",
                "from os import getenv as env_get, environ",
                "from os import *",
                "token = os.getenv('TOKEN')",
                "value = os.environ['TOKEN']",
                "other = operating_system.getenv('TOKEN')",
                "fallback = env_get('TOKEN')",
                "present = 'TOKEN' in environ",
                "star_fallback = getenv('TOKEN')",
                "star_present = 'TOKEN' in environ",
                "def local_access():",
                "    import os as local_os",
                "    from os import getenv as local_getenv, environ as local_environ",
                "    nested = local_os.getenv('TOKEN')",
                "    local_fallback = local_getenv('TOKEN')",
                "    local_present = 'TOKEN' in local_environ",
            ]
        )
    )
    _assert_equal(
        _env_accesses(tree),
        (
            EnvAccess(5, "os.getenv"),
            EnvAccess(6, "os.environ"),
            EnvAccess(7, "operating_system.getenv"),
            EnvAccess(8, "env_get"),
            EnvAccess(9, "environ"),
            EnvAccess(10, "getenv"),
            EnvAccess(11, "environ"),
            EnvAccess(15, "local_os.getenv"),
            EnvAccess(16, "local_getenv"),
            EnvAccess(17, "local_environ"),
        ),
    )


def _assert_equal(actual: object, expected: object) -> None:
    if actual != expected:
        raise AssertionError(f"expected {expected!r}, got {actual!r}")


if __name__ == "__main__":
    main()

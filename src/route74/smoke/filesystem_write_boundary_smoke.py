from __future__ import annotations

import ast
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
FILESYSTEM_WRITE_ALLOWLIST = frozenset(
    {
        "bot/instance.py",
        "bot/atomic_file.py",
        "bot/settings.py",
        "bot/watch_store.py",
        "cli/yandex.py",
        "cli/yandex_collect.py",
        "dashboard/preview.py",
        "sources/yandex/browser_rate_limit.py",
        "storage/connection.py",
        "storage/db_admin.py",
        "web/watch_runtime.py",
    }
)
WRITE_METHODS = frozenset({"mkdir", "rmdir", "unlink", "write_bytes", "write_text"})
DESTRUCTIVE_CALL_LABELS = {
    "os.remove": "os.remove() must stay in explicit filesystem owners",
    "os.replace": "os.replace() must stay in explicit filesystem owners",
    "os.rmdir": "os.rmdir() must stay in explicit filesystem owners",
    "os.unlink": "os.unlink() must stay in explicit filesystem owners",
    "shutil.rmtree": "shutil.rmtree() must stay in explicit filesystem owners",
}
WRITE_MODE_CHARS = frozenset({"w", "a", "x", "+"})


def main() -> None:
    _assert_filesystem_write_detector()
    failures = []
    for path in _production_python_files():
        relative_path = path.relative_to(PACKAGE_ROOT)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        failures.extend(
            f"{relative_path.as_posix()}:{line_number} {label}"
            for line_number, label in _filesystem_write_violations(tree, relative_path)
        )
    if failures:
        details = "\n".join(failures)
        raise AssertionError(f"filesystem write boundary contract failed:\n{details}")
    print("OK | filesystem write boundary smoke passed")


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


def _filesystem_write_violations(
    tree: ast.Module,
    relative_path: Path,
) -> tuple[tuple[int, str], ...]:
    if relative_path.as_posix() in FILESYSTEM_WRITE_ALLOWLIST:
        return ()

    aliases = _import_aliases(tree)
    violations = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        label = _filesystem_write_label(node, aliases)
        if label:
            violations.append((node.lineno, label))
    return tuple(violations)


def _filesystem_write_label(node: ast.Call, aliases: dict[str, str]) -> str | None:
    call_name = _call_name(node.func, aliases)
    if call_name in DESTRUCTIVE_CALL_LABELS:
        return DESTRUCTIVE_CALL_LABELS[call_name]

    if _is_builtin_open_call(node.func, aliases):
        if _open_mode_writes(node, positional_mode_index=1):
            return "open() with write mode must stay in explicit filesystem owners"
        return None

    if isinstance(node.func, ast.Attribute):
        if node.func.attr in WRITE_METHODS:
            return f"{node.func.attr}() must stay in explicit filesystem owners"
        if node.func.attr == "open" and _open_mode_writes(node, positional_mode_index=0):
            return "open() with write mode must stay in explicit filesystem owners"

    if call_name.endswith(".NamedTemporaryFile"):
        return "NamedTemporaryFile() must stay in explicit filesystem owners"
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
        if alias.name in {"builtins", "os", "shutil", "tempfile"}:
            aliases[alias.asname or alias.name] = alias.name
    return aliases


def _from_import_aliases(node: ast.ImportFrom) -> dict[str, str]:
    if node.module not in {"builtins", "os", "shutil", "tempfile"}:
        return {}
    aliases = {}
    for alias in node.names:
        if alias.name == "*":
            continue
        target = _imported_call_name(node.module, alias.name)
        if target:
            aliases[alias.asname or alias.name] = target
    return aliases


def _imported_call_name(module: str, name: str) -> str:
    if module == "builtins" and name == "open":
        return "open"
    if module == "os" and f"os.{name}" in DESTRUCTIVE_CALL_LABELS:
        return f"os.{name}"
    if module == "shutil" and f"shutil.{name}" in DESTRUCTIVE_CALL_LABELS:
        return f"shutil.{name}"
    if module == "tempfile" and name == "NamedTemporaryFile":
        return "tempfile.NamedTemporaryFile"
    return ""


def _is_builtin_open_call(node: ast.expr, aliases: dict[str, str]) -> bool:
    if isinstance(node, ast.Name):
        return _call_name(node, aliases) == "open"
    if isinstance(node, ast.Attribute):
        return node.attr == "open" and _call_name(node.value, aliases) == "builtins"
    return False


def _open_mode_writes(node: ast.Call, *, positional_mode_index: int) -> bool:
    mode = _open_mode(node, positional_mode_index=positional_mode_index)
    if mode is None:
        return False
    if isinstance(mode, str):
        return any(char in mode for char in WRITE_MODE_CHARS)
    return True


def _open_mode(node: ast.Call, *, positional_mode_index: int) -> str | object | None:
    if len(node.args) > positional_mode_index:
        return _string_literal(node.args[positional_mode_index])
    for keyword in node.keywords:
        if keyword.arg == "mode":
            return _string_literal(keyword.value)
    return None


def _string_literal(node: ast.AST) -> str | object:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return object()


def _call_name(node: ast.expr, aliases: dict[str, str]) -> str:
    if isinstance(node, ast.Name):
        return aliases.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        parent = _call_name(node.value, aliases)
        return f"{parent}.{node.attr}" if parent else node.attr
    return ""


def _assert_filesystem_write_detector() -> None:
    tree = ast.parse(
        "\n".join(
            [
                "from pathlib import Path",
                "import builtins",
                "import os",
                "import os as sys_os",
                "import shutil",
                "import shutil as shell_utils",
                "import tempfile as temp_files",
                "from builtins import open as file_open",
                "from os import remove as remove_file, unlink as unlink_file",
                "from shutil import rmtree as remove_tree",
                "from tempfile import NamedTemporaryFile, NamedTemporaryFile as atomic_file",
                "Path('x').read_text(encoding='utf-8')",
                "Path('x').open('r', encoding='utf-8')",
                "builtins.open('x', 'r', encoding='utf-8')",
                "Path('x').write_text('x', encoding='utf-8')",
                "Path('x').open('a+', encoding='utf-8')",
                "open('x', mode='w', encoding='utf-8')",
                "builtins.open('x', 'w', encoding='utf-8')",
                "file_open('x', 'w', encoding='utf-8')",
                "NamedTemporaryFile(delete=False)",
                "atomic_file(delete=False)",
                "temp_files.NamedTemporaryFile(delete=False)",
                "Path('x').mkdir(parents=True)",
                "Path('x').unlink()",
                "Path('x').rmdir()",
                "os.remove('x')",
                "sys_os.remove('x')",
                "remove_file('x')",
                "os.unlink('x')",
                "unlink_file('x')",
                "os.rmdir('x')",
                "os.replace('x', 'y')",
                "shutil.rmtree('x')",
                "shell_utils.rmtree('x')",
                "remove_tree('x')",
            ]
        )
    )
    _assert_equal(_labels(tree, "bot/settings.py"), ())
    _assert_equal(
        _labels(tree, "services/commute.py"),
        (
            "write_text() must stay in explicit filesystem owners",
            "open() with write mode must stay in explicit filesystem owners",
            "open() with write mode must stay in explicit filesystem owners",
            "open() with write mode must stay in explicit filesystem owners",
            "open() with write mode must stay in explicit filesystem owners",
            "NamedTemporaryFile() must stay in explicit filesystem owners",
            "NamedTemporaryFile() must stay in explicit filesystem owners",
            "NamedTemporaryFile() must stay in explicit filesystem owners",
            "mkdir() must stay in explicit filesystem owners",
            "unlink() must stay in explicit filesystem owners",
            "rmdir() must stay in explicit filesystem owners",
            "os.remove() must stay in explicit filesystem owners",
            "os.remove() must stay in explicit filesystem owners",
            "os.remove() must stay in explicit filesystem owners",
            "os.unlink() must stay in explicit filesystem owners",
            "os.unlink() must stay in explicit filesystem owners",
            "os.rmdir() must stay in explicit filesystem owners",
            "os.replace() must stay in explicit filesystem owners",
            "shutil.rmtree() must stay in explicit filesystem owners",
            "shutil.rmtree() must stay in explicit filesystem owners",
            "shutil.rmtree() must stay in explicit filesystem owners",
        ),
    )


def _labels(tree: ast.Module, path: str) -> tuple[str, ...]:
    return tuple(label for _, label in _filesystem_write_violations(tree, Path(path)))


def _assert_equal(actual: object, expected: object) -> None:
    if actual != expected:
        raise AssertionError(f"expected {expected!r}, got {actual!r}")


if __name__ == "__main__":
    main()

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
SLEEP_FUNCTIONS = frozenset({"asyncio.sleep", "time.sleep"})


@dataclass(frozen=True)
class SleepCallKey:
    path: str
    context: str
    call: str


_ALLOWED_BLOCKING_SLEEP_CALLS = frozenset(
    {
        SleepCallKey(
            "cli/yandex_collect.py",
            "_collect_until_done",
            "time.sleep(max(0.0, sleep_for))",
        ),
        SleepCallKey(
            "sources/yandex/browser_rate_limit.py",
            "_run_with_process_lock",
            "time.sleep(wait_seconds)",
        ),
        SleepCallKey(
            "web/watch_runtime.py",
            "WatchLoop._run",
            "asyncio.sleep(POLL_INTERVAL.total_seconds())",
        ),
    }
)


def main() -> None:
    _assert_blocking_sleep_detector()
    failures = []
    for path in _production_python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        aliases = _import_aliases(tree)
        contexts = _parent_contexts(tree)
        relative_path = path.relative_to(PACKAGE_ROOT).as_posix()
        for call in _blocking_sleep_calls(tree, aliases):
            key = SleepCallKey(
                relative_path,
                _node_context(call, contexts),
                ast.unparse(call).strip(),
            )
            if key not in _ALLOWED_BLOCKING_SLEEP_CALLS:
                failures.append(f"{relative_path}:{call.lineno} {key.context} uses blocking sleep: {key.call}")
    if failures:
        details = "\n".join(failures)
        raise AssertionError(f"blocking sleep boundary failed:\n{details}")
    print("OK | blocking sleep boundary smoke passed")


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


def _blocking_sleep_calls(tree: ast.Module, aliases: dict[str, str]) -> tuple[ast.Call, ...]:
    calls = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if _is_sleep_call(node.func, aliases):
            calls.append(node)
    return tuple(sorted(calls, key=lambda call: call.lineno))


def _is_sleep_call(node: ast.expr, aliases: dict[str, str]) -> bool:
    call_name = _call_name(node, aliases)
    if call_name in SLEEP_FUNCTIONS:
        return True
    dynamic_method = _dynamic_getattr(node, aliases)
    if dynamic_method is None:
        return False
    owner, method = dynamic_method
    return f"{_call_name(owner, aliases)}.{method}" in SLEEP_FUNCTIONS


def _dynamic_getattr(node: ast.expr, aliases: dict[str, str]) -> tuple[ast.expr, str] | None:
    if not isinstance(node, ast.Call):
        return None
    if _call_name(node.func, aliases) not in {"builtins.getattr", "getattr"}:
        return None
    if len(node.args) < 2:
        return None
    method = _string_constant(node.args[1])
    if method is None:
        return None
    return node.args[0], method


def _string_constant(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
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
        if alias.name in {"asyncio", "builtins", "time"}:
            aliases[alias.asname or alias.name] = alias.name
    return aliases


def _from_import_aliases(node: ast.ImportFrom) -> dict[str, str]:
    if node.module not in {"asyncio", "builtins", "time"}:
        return {}
    aliases = {}
    for alias in node.names:
        if node.module in {"asyncio", "time"} and alias.name == "sleep":
            aliases[alias.asname or alias.name] = f"{node.module}.sleep"
        elif node.module == "builtins" and alias.name == "getattr":
            aliases[alias.asname or alias.name] = "builtins.getattr"
    return aliases


def _call_name(node: ast.expr, aliases: dict[str, str]) -> str:
    if isinstance(node, ast.Name):
        return aliases.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        parent = _call_name(node.value, aliases)
        return f"{parent}.{node.attr}" if parent else node.attr
    return ""


def _parent_contexts(tree: ast.Module) -> dict[ast.AST, ast.AST]:
    parents = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    return parents


def _node_context(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> str:
    names = []
    while node in parents:
        node = parents[node]
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            names.append(node.name)
        elif isinstance(node, ast.ClassDef):
            names.append(node.name)
    if not names:
        return "<module>"
    return ".".join(reversed(names))


def _assert_blocking_sleep_detector() -> None:
    tree = ast.parse(
        "\n".join(
            [
                "import asyncio",
                "import asyncio as aio",
                "import builtins",
                "import builtins as py_builtins",
                "import time",
                "import time as clock",
                "from asyncio import sleep as async_pause",
                "from builtins import getattr as dynamic_getattr",
                "from time import sleep as pause",
                "time.sleep(1)",
                "clock.sleep(2)",
                "pause(3)",
                "asyncio.sleep(4)",
                "aio.sleep(5)",
                "async_pause(6)",
                "getattr(time, 'sleep')(7)",
                "builtins.getattr(clock, 'sleep')(8)",
                "py_builtins.getattr(asyncio, 'sleep')(9)",
                "dynamic_getattr(aio, 'sleep')(10)",
                "getattr(object(), 'sleep')(11)",
                "def wait():",
                "    time.sleep(12)",
            ]
        )
    )
    aliases = _import_aliases(tree)
    contexts = _parent_contexts(tree)
    _assert_equal(
        tuple(
            (_node_context(call, contexts), ast.unparse(call).strip()) for call in _blocking_sleep_calls(tree, aliases)
        ),
        (
            ("<module>", "time.sleep(1)"),
            ("<module>", "clock.sleep(2)"),
            ("<module>", "pause(3)"),
            ("<module>", "asyncio.sleep(4)"),
            ("<module>", "aio.sleep(5)"),
            ("<module>", "async_pause(6)"),
            ("<module>", "getattr(time, 'sleep')(7)"),
            ("<module>", "builtins.getattr(clock, 'sleep')(8)"),
            ("<module>", "py_builtins.getattr(asyncio, 'sleep')(9)"),
            ("<module>", "dynamic_getattr(aio, 'sleep')(10)"),
            ("wait", "time.sleep(12)"),
        ),
    )


def _assert_equal(actual: object, expected: object) -> None:
    if actual != expected:
        raise AssertionError(f"expected {expected!r}, got {actual!r}")


if __name__ == "__main__":
    main()

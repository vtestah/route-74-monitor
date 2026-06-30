from __future__ import annotations

import ast
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]

ALLOWED_SUBPROCESS_RUN_MODULES = frozenset({"build_info.py"})
DISALLOWED_CALLS = frozenset(
    {
        "eval",
        "exec",
        "os.popen",
        "os.system",
        "subprocess.Popen",
        "subprocess.call",
        "subprocess.check_call",
        "subprocess.check_output",
    }
)


def main() -> None:
    _assert_call_detector()
    _assert_subprocess_run_kwargs_detector()
    failures = []
    for path in _production_module_paths():
        violations = _process_execution_violations(path)
        if violations:
            relative = path.relative_to(PACKAGE_ROOT)
            failures.append(f"{relative}: {', '.join(violations)}")
    if failures:
        details = "\n".join(failures)
        raise AssertionError(f"unsafe process execution contract failed:\n{details}")
    print("OK | process execution smoke passed")


def _production_module_paths() -> tuple[Path, ...]:
    paths = []
    for path in sorted(PACKAGE_ROOT.rglob("*.py")):
        parts = path.relative_to(PACKAGE_ROOT).with_suffix("").parts
        if any(part == "smoke" for part in parts) or parts[-1].endswith("_smoke"):
            continue
        paths.append(path)
    return tuple(paths)


def _process_execution_violations(path: Path) -> tuple[str, ...]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    aliases = _import_aliases(tree)
    labels = set()
    labels.update(
        _process_execution_labels(
            _calls_outside_functions(tree),
            aliases=aliases,
            dict_kwargs=_dict_literal_kwargs_outside_functions(tree),
            path=path,
        )
    )
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            labels.update(
                _process_execution_labels(
                    _calls_outside_functions(node),
                    aliases=aliases,
                    dict_kwargs=_dict_literal_kwargs_outside_functions(node),
                    path=path,
                )
            )
    return tuple(sorted(labels))


def _process_execution_labels(
    calls: tuple[ast.Call, ...],
    *,
    aliases: dict[str, str],
    dict_kwargs: dict[str, dict[str, ast.expr]],
    path: Path,
) -> set[str]:
    labels = set()
    for node in calls:
        if not isinstance(node, ast.Call):
            continue
        call_name = _call_name(node.func, aliases)
        if call_name in DISALLOWED_CALLS:
            labels.add(call_name)
        if call_name == "subprocess.run":
            labels.update(_subprocess_run_violations(node, path, dict_kwargs))
    return labels


def _subprocess_run_violations(
    node: ast.Call,
    path: Path,
    dict_kwargs: dict[str, dict[str, ast.expr]],
) -> tuple[str, ...]:
    violations = []
    if path.name not in ALLOWED_SUBPROCESS_RUN_MODULES:
        violations.append("subprocess.run outside allowlist")
    if _keyword_is_true(node, "shell", dict_kwargs):
        violations.append("subprocess.run shell=True")
    if not _has_non_none_keyword(node, "timeout", dict_kwargs):
        violations.append("subprocess.run missing timeout")
    if not _keyword_is_true(node, "check", dict_kwargs):
        violations.append("subprocess.run missing check=True")
    return tuple(violations)


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
        if alias.name in {"builtins", "os", "subprocess"}:
            label = alias.asname or alias.name
            aliases[label] = alias.name
    return aliases


def _from_import_aliases(node: ast.ImportFrom) -> dict[str, str]:
    if node.module not in {"builtins", "os", "subprocess"}:
        return {}
    aliases = {}
    for alias in node.names:
        if alias.name == "*":
            continue
        label = alias.asname or alias.name
        aliases[label] = _normalize_builtin_call(f"{node.module}.{alias.name}")
    return aliases


def _call_name(node: ast.expr, aliases: dict[str, str]) -> str:
    if isinstance(node, ast.Call):
        return _dynamic_getattr_call_name(node, aliases)
    if isinstance(node, ast.Name):
        return aliases.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        parent = _call_name(node.value, aliases)
        return _normalize_builtin_call(f"{parent}.{node.attr}" if parent else node.attr)
    return ""


def _dynamic_getattr_call_name(node: ast.Call, aliases: dict[str, str]) -> str:
    if _call_name(node.func, aliases) != "getattr":
        return ""
    if len(node.args) < 2:
        return ""
    attribute_name = _string_constant(node.args[1])
    if not attribute_name:
        return ""
    parent = _call_name(node.args[0], aliases)
    if not parent:
        return ""
    return _normalize_builtin_call(f"{parent}.{attribute_name}")


def _string_constant(node: ast.expr) -> str:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return ""


def _normalize_builtin_call(name: str) -> str:
    if name in {"builtins.eval", "builtins.exec", "builtins.getattr"}:
        return name.removeprefix("builtins.")
    return name


def _calls_outside_functions(node: ast.AST) -> tuple[ast.Call, ...]:
    return tuple(child for child in _nodes_outside_functions(node) if isinstance(child, ast.Call))


def _nodes_outside_functions(node: ast.AST) -> tuple[ast.AST, ...]:
    nodes = []
    pending: list[ast.AST] = [node]
    while pending:
        current = pending.pop()
        nodes.append(current)
        children = tuple(ast.iter_child_nodes(current))
        for child in reversed(children):
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
                continue
            pending.append(child)
    return tuple(nodes)


def _dict_literal_kwargs(node: ast.AST) -> dict[str, dict[str, ast.expr]]:
    kwargs: dict[str, dict[str, ast.expr]] = {}
    for child in ast.walk(node):
        target, value = _dict_assignment(child)
        if target is None or value is None:
            continue
        kwargs[target.id] = _dict_keyword_values(value)
    return kwargs


def _dict_literal_kwargs_outside_functions(node: ast.AST) -> dict[str, dict[str, ast.expr]]:
    kwargs: dict[str, dict[str, ast.expr]] = {}
    for child in _nodes_outside_functions(node):
        target, value = _dict_assignment(child)
        if target is None or value is None:
            continue
        kwargs[target.id] = _dict_keyword_values(value)
    return kwargs


def _dict_assignment(node: ast.AST) -> tuple[ast.Name | None, ast.Dict | None]:
    target: ast.expr | None = None
    value: ast.expr | None = None
    if isinstance(node, ast.Assign) and len(node.targets) == 1:
        target = node.targets[0]
        value = node.value
    elif isinstance(node, ast.AnnAssign):
        target = node.target
        value = node.value
    if not isinstance(target, ast.Name) or not isinstance(value, ast.Dict):
        return None, None
    return target, value


def _dict_keyword_values(node: ast.Dict) -> dict[str, ast.expr]:
    values: dict[str, ast.expr] = {}
    for key, value in zip(node.keys, node.values):
        if isinstance(key, ast.Constant) and isinstance(key.value, str):
            values[key.value] = value
    return values


def _keyword_is_true(
    node: ast.Call,
    name: str,
    dict_kwargs: dict[str, dict[str, ast.expr]],
) -> bool:
    for keyword in node.keywords:
        if keyword.arg == name and _is_true_constant(keyword.value):
            return True
        if keyword.arg is None and any(
            _is_true_constant(value) for value in _unpacked_keyword_values(keyword.value, name, dict_kwargs)
        ):
            return True
    return False


def _has_non_none_keyword(
    node: ast.Call,
    name: str,
    dict_kwargs: dict[str, dict[str, ast.expr]],
) -> bool:
    for keyword in node.keywords:
        if keyword.arg == name:
            return not _is_none_constant(keyword.value)
        if keyword.arg is None and any(
            not _is_none_constant(value) for value in _unpacked_keyword_values(keyword.value, name, dict_kwargs)
        ):
            return True
    return False


def _unpacked_keyword_values(
    node: ast.expr,
    name: str,
    dict_kwargs: dict[str, dict[str, ast.expr]],
) -> tuple[ast.expr, ...]:
    values = _dict_keyword_values(node) if isinstance(node, ast.Dict) else None
    if values is None and isinstance(node, ast.Name):
        values = dict_kwargs.get(node.id)
    if not values or name not in values:
        return ()
    return (values[name],)


def _is_true_constant(node: ast.expr) -> bool:
    return isinstance(node, ast.Constant) and node.value is True


def _is_none_constant(node: ast.expr) -> bool:
    return isinstance(node, ast.Constant) and node.value is None


def _assert_call_detector() -> None:
    tree = ast.parse(
        "\n".join(
            [
                "import os",
                "import builtins",
                "import subprocess",
                "import subprocess as sp",
                "import os as operating_system",
                "import builtins as py_builtins",
                "from builtins import getattr as dynamic_getattr",
                "from subprocess import Popen as Spawn, run as run_process",
                "from os import system as shell",
                "from builtins import exec as do_exec",
                "subprocess.run(('git', 'status'))",
                "subprocess.run(('git', 'status'), timeout=5, check=True)",
                "subprocess.run(('git', 'status'), timeout=None)",
                "subprocess.run(('git', 'status'), timeout=5, check=False)",
                "subprocess.run('git status', shell=True)",
                "subprocess.Popen(('git', 'status'))",
                "os.system('git status')",
                "eval('1 + 1')",
                "sp.run(('git', 'status'))",
                "run_process(('git', 'status'))",
                "Spawn(('git', 'status'))",
                "operating_system.popen('git status')",
                "shell('git status')",
                "py_builtins.eval('1 + 1')",
                "do_exec('value = 1')",
                "getattr(subprocess, 'Popen')(('git', 'status'))",
                "getattr(sp, 'run')(('git', 'status'))",
                "getattr(operating_system, 'system')('git status')",
                "getattr(py_builtins, 'eval')('1 + 1')",
                "dynamic_getattr(py_builtins, 'exec')('value = 1')",
                "builtins.getattr(subprocess, 'check_output')(('git', 'status'))",
                "getattr(object(), 'system')('git status')",
            ]
        )
    )
    aliases = _import_aliases(tree)
    calls = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            calls.append(_call_name(node.func, aliases))
    _assert_equal(
        tuple(call for call in calls if call in DISALLOWED_CALLS or call == "subprocess.run"),
        (
            "subprocess.run",
            "subprocess.run",
            "subprocess.run",
            "subprocess.run",
            "subprocess.run",
            "subprocess.Popen",
            "os.system",
            "eval",
            "subprocess.run",
            "subprocess.run",
            "subprocess.Popen",
            "os.popen",
            "os.system",
            "eval",
            "exec",
            "subprocess.Popen",
            "subprocess.run",
            "os.system",
            "eval",
            "exec",
            "subprocess.check_output",
        ),
    )
    run_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and _call_name(node.func, aliases) == "subprocess.run"
    ]
    no_kwargs: dict[str, dict[str, ast.expr]] = {}
    _assert_equal(
        _subprocess_run_violations(run_calls[0], Path("build_info.py"), no_kwargs),
        ("subprocess.run missing timeout", "subprocess.run missing check=True"),
    )
    _assert_equal(_subprocess_run_violations(run_calls[1], Path("build_info.py"), no_kwargs), ())
    _assert_equal(
        _subprocess_run_violations(run_calls[2], Path("build_info.py"), no_kwargs),
        ("subprocess.run missing timeout", "subprocess.run missing check=True"),
    )
    _assert_equal(
        _subprocess_run_violations(run_calls[3], Path("build_info.py"), no_kwargs),
        ("subprocess.run missing check=True",),
    )
    _assert_equal(
        _subprocess_run_violations(run_calls[0], Path("runtime.py"), no_kwargs),
        (
            "subprocess.run outside allowlist",
            "subprocess.run missing timeout",
            "subprocess.run missing check=True",
        ),
    )
    _assert_equal(
        _subprocess_run_violations(run_calls[4], Path("build_info.py"), no_kwargs),
        (
            "subprocess.run shell=True",
            "subprocess.run missing timeout",
            "subprocess.run missing check=True",
        ),
    )
    _assert_equal(
        _subprocess_run_violations(run_calls[2], Path("runtime.py"), no_kwargs),
        (
            "subprocess.run outside allowlist",
            "subprocess.run missing timeout",
            "subprocess.run missing check=True",
        ),
    )


def _assert_subprocess_run_kwargs_detector() -> None:
    tree = ast.parse(
        "\n".join(
            [
                "import subprocess",
                "module_kwargs = {'timeout': 5, 'shell': True, 'check': True}",
                "subprocess.run('git status', **module_kwargs)",
                "def literal_kwargs():",
                "    subprocess.run('git status', **{'timeout': 5, 'shell': True, 'check': True})",
                "def none_timeout_kwargs():",
                "    kwargs = {'timeout': None}",
                "    subprocess.run(('git', 'status'), **kwargs)",
                "def ok_kwargs():",
                "    kwargs = {'timeout': 5, 'check': True}",
                "    subprocess.run(('git', 'status'), **kwargs)",
                "def unchecked_kwargs():",
                "    kwargs = {'timeout': 5, 'check': False}",
                "    subprocess.run(('git', 'status'), **kwargs)",
                "def dynamic_kwargs():",
                "    getattr(subprocess, 'run')('git status', **{'timeout': None, 'shell': True})",
            ]
        )
    )
    aliases = _import_aliases(tree)
    _assert_equal(
        _process_execution_labels(
            _calls_outside_functions(tree),
            aliases=aliases,
            dict_kwargs=_dict_literal_kwargs_outside_functions(tree),
            path=Path("build_info.py"),
        ),
        {"subprocess.run shell=True"},
    )
    functions = tuple(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef))
    function_labels = tuple(
        sorted(
            _process_execution_labels(
                _calls_outside_functions(function),
                aliases=aliases,
                dict_kwargs=_dict_literal_kwargs_outside_functions(function),
                path=Path("build_info.py"),
            )
        )
        for function in functions
    )
    _assert_equal(
        function_labels,
        (
            ["subprocess.run shell=True"],
            ["subprocess.run missing check=True", "subprocess.run missing timeout"],
            [],
            ["subprocess.run missing check=True"],
            [
                "subprocess.run missing check=True",
                "subprocess.run missing timeout",
                "subprocess.run shell=True",
            ],
        ),
    )


def _assert_equal(actual: object, expected: object) -> None:
    if actual != expected:
        raise AssertionError(f"expected {expected!r}, got {actual!r}")


if __name__ == "__main__":
    main()

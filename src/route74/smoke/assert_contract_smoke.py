from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class AssertionAliases:
    assertion_errors: frozenset[str]
    builtins_modules: frozenset[str]
    getattr_functions: frozenset[str]


def main() -> None:
    _assert_runtime_assert_detector()
    failures = []
    for path in _production_python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        contexts = _parent_contexts(tree)
        relative_path = path.relative_to(PACKAGE_ROOT).as_posix()
        for node in _runtime_asserts(tree):
            failures.append(f"{relative_path}:{node.lineno} {_node_context(node, contexts)}")
        for node in _runtime_assertion_raises(tree):
            failures.append(f"{relative_path}:{node.lineno} {_node_context(node, contexts)} raises AssertionError")
        for node in _debug_runtime_flags(tree):
            failures.append(f"{relative_path}:{node.lineno} {_node_context(node, contexts)} uses __debug__")
    if failures:
        details = "\n".join(failures)
        raise AssertionError(f"runtime assert contract failed:\n{details}")
    print("OK | runtime assert contract smoke passed")


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


def _runtime_asserts(tree: ast.Module) -> tuple[ast.Assert, ...]:
    return tuple(node for node in ast.walk(tree) if isinstance(node, ast.Assert))


def _runtime_assertion_raises(tree: ast.Module) -> tuple[ast.Raise, ...]:
    aliases = _assertion_aliases(tree)
    return tuple(
        sorted(
            (node for node in ast.walk(tree) if isinstance(node, ast.Raise) and _raises_assertion_error(node, aliases)),
            key=lambda node: node.lineno,
        )
    )


def _assertion_aliases(tree: ast.Module) -> AssertionAliases:
    assertion_errors = {"AssertionError"}
    builtins_modules = {"builtins"}
    getattr_functions = {"getattr"}

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "builtins":
                    builtins_modules.add(alias.asname or alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module == "builtins" and node.level == 0:
            for alias in node.names:
                if alias.name == "AssertionError":
                    assertion_errors.add(alias.asname or alias.name)
                elif alias.name == "getattr":
                    getattr_functions.add(alias.asname or alias.name)

    return AssertionAliases(
        assertion_errors=frozenset(assertion_errors),
        builtins_modules=frozenset(builtins_modules),
        getattr_functions=frozenset(getattr_functions),
    )


def _raises_assertion_error(node: ast.Raise, aliases: AssertionAliases) -> bool:
    if node.exc is None:
        return False
    target = node.exc.func if isinstance(node.exc, ast.Call) else node.exc
    return _is_assertion_error_expr(target, aliases)


def _is_assertion_error_expr(node: ast.AST, aliases: AssertionAliases) -> bool:
    if isinstance(node, ast.Name):
        return node.id in aliases.assertion_errors
    dynamic_attribute = _dynamic_getattr(node, aliases)
    if dynamic_attribute is not None:
        owner, attribute_name = dynamic_attribute
        return attribute_name == "AssertionError" and _is_builtins_module(owner, aliases)
    return (
        isinstance(node, ast.Attribute) and node.attr == "AssertionError" and _is_builtins_module(node.value, aliases)
    )


def _is_builtins_module(node: ast.AST, aliases: AssertionAliases) -> bool:
    return isinstance(node, ast.Name) and node.id in aliases.builtins_modules


def _dynamic_getattr(
    node: ast.AST,
    aliases: AssertionAliases,
) -> tuple[ast.AST, str] | None:
    if not isinstance(node, ast.Call):
        return None
    if not _is_getattr_function(node.func, aliases):
        return None
    if len(node.args) < 2:
        return None
    attribute_name = _string_constant(node.args[1])
    if not attribute_name:
        return None
    return node.args[0], attribute_name


def _is_getattr_function(node: ast.AST, aliases: AssertionAliases) -> bool:
    if isinstance(node, ast.Name):
        return node.id in aliases.getattr_functions
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "getattr"
        and isinstance(node.value, ast.Name)
        and node.value.id in aliases.builtins_modules
    )


def _string_constant(node: ast.AST) -> str:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return ""


def _debug_runtime_flags(tree: ast.Module) -> tuple[ast.Name, ...]:
    return tuple(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id == "__debug__"
    )


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


def _assert_runtime_assert_detector() -> None:
    tree = ast.parse(
        "\n".join(
            [
                "import builtins",
                "import builtins as py_builtins",
                "from builtins import AssertionError as ContractFailure",
                "from builtins import getattr as dynamic_getattr",
                "assert READY",
                "raise AssertionError('bad state')",
                "raise builtins.AssertionError('bad state')",
                "raise py_builtins.AssertionError('bad state')",
                "raise ContractFailure('bad state')",
                "raise getattr(builtins, 'AssertionError')('bad state')",
                "raise dynamic_getattr(py_builtins, 'AssertionError')('bad state')",
                "raise RuntimeError('ok')",
                "if __debug__:",
                "    validate_debug_mode()",
                "def validate(value):",
                "    if value is None:",
                "        raise ValueError('missing value')",
                "    assert value > 0",
                "    raise AssertionError('invalid value')",
                "    if not __debug__:",
                "        raise RuntimeError('optimized mode')",
                "class Worker:",
                "    async def run(self):",
                "        assert self.ready",
                "        raise AssertionError('not ready')",
                "        mode = 'debug' if __debug__ else 'optimized'",
            ]
        )
    )
    contexts = _parent_contexts(tree)
    _assert_equal(
        tuple(_node_context(node, contexts) for node in _runtime_asserts(tree)),
        ("<module>", "validate", "Worker.run"),
    )
    _assert_equal(
        tuple(_node_context(node, contexts) for node in _runtime_assertion_raises(tree)),
        (
            "<module>",
            "<module>",
            "<module>",
            "<module>",
            "<module>",
            "<module>",
            "validate",
            "Worker.run",
        ),
    )
    _assert_equal(
        tuple(_node_context(node, contexts) for node in _debug_runtime_flags(tree)),
        ("<module>", "validate", "Worker.run"),
    )


def _assert_equal(actual: object, expected: object) -> None:
    if actual != expected:
        raise AssertionError(f"expected {expected!r}, got {actual!r}")


if __name__ == "__main__":
    main()

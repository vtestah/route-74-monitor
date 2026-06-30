from __future__ import annotations

import ast
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    _assert_hash_detector()
    failures = []
    for path in _production_python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        aliases = _import_aliases(tree)
        contexts = _parent_contexts(tree)
        relative_path = path.relative_to(PACKAGE_ROOT).as_posix()
        for node in _hash_calls(tree, aliases):
            context = _node_context(node, contexts)
            failures.append(f"{relative_path}:{node.lineno} {context} uses unstable hash()")
    if failures:
        details = "\n".join(failures)
        raise AssertionError(f"stable hash contract failed:\n{details}")
    print("OK | stable hash contract smoke passed")


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


def _hash_calls(tree: ast.Module, aliases: dict[str, str]) -> tuple[ast.Call, ...]:
    return tuple(node for node in ast.walk(tree) if isinstance(node, ast.Call) and _is_hash_call(node.func, aliases))


def _is_hash_call(node: ast.expr, aliases: dict[str, str]) -> bool:
    return _call_name(node, aliases) == "hash" or _is_getattr_hash_call(node, aliases)


def _is_getattr_hash_call(node: ast.expr, aliases: dict[str, str]) -> bool:
    if not isinstance(node, ast.Call):
        return False
    if _call_name(node.func, aliases) != "getattr":
        return False
    if len(node.args) < 2:
        return False
    if _call_name(node.args[0], aliases) != "builtins":
        return False
    return isinstance(node.args[1], ast.Constant) and node.args[1].value == "hash"


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
        if alias.name == "builtins":
            aliases[alias.asname or alias.name] = alias.name
    return aliases


def _from_import_aliases(node: ast.ImportFrom) -> dict[str, str]:
    if node.module != "builtins":
        return {}
    aliases = {}
    for alias in node.names:
        if alias.name in {"getattr", "hash"}:
            aliases[alias.asname or alias.name] = alias.name
    return aliases


def _call_name(node: ast.expr, aliases: dict[str, str]) -> str:
    if isinstance(node, ast.Name):
        return aliases.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        parent = _call_name(node.value, aliases)
        name = f"{parent}.{node.attr}" if parent else node.attr
        if name in {"builtins.getattr", "builtins.hash"}:
            return node.attr
        return name
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


def _assert_hash_detector() -> None:
    tree = ast.parse(
        "\n".join(
            [
                "import builtins",
                "import builtins as py_builtins",
                "from builtins import getattr as builtins_getattr",
                "from builtins import hash as identity",
                "hash(value)",
                "builtins.hash(value)",
                "py_builtins.hash(value)",
                "identity(value)",
                "getattr(builtins, 'hash')(value)",
                "getattr(py_builtins, 'hash')(value)",
                "builtins.getattr(builtins, 'hash')(value)",
                "builtins_getattr(builtins, 'hash')(value)",
                "getattr(object(), 'hash')(value)",
                "getattr(builtins, 'repr')(value)",
                "class CacheKey:",
                "    def digest(self):",
                "        return hash(self.value)",
            ]
        )
    )
    aliases = _import_aliases(tree)
    contexts = _parent_contexts(tree)
    _assert_equal(
        tuple(_node_context(node, contexts) for node in _hash_calls(tree, aliases)),
        (
            "<module>",
            "<module>",
            "<module>",
            "<module>",
            "<module>",
            "<module>",
            "<module>",
            "<module>",
            "CacheKey.digest",
        ),
    )


def _assert_equal(actual: object, expected: object) -> None:
    if actual != expected:
        raise AssertionError(f"expected {expected!r}, got {actual!r}")


if __name__ == "__main__":
    main()

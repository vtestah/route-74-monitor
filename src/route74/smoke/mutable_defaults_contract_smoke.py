from __future__ import annotations

import ast
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
MUTABLE_FACTORY_NAMES = frozenset(
    {
        "bytearray",
        "Counter",
        "defaultdict",
        "deque",
        "dict",
        "list",
        "OrderedDict",
        "set",
    }
)
MUTABLE_FACTORY_MODULES = frozenset({"builtins", "collections"})
DATACLASS_FIELD_MODULES = frozenset({"dataclasses"})
CALL_QUALIFIER_MODULES = MUTABLE_FACTORY_MODULES | DATACLASS_FIELD_MODULES


def main() -> None:
    _assert_mutable_default_detector()
    failures = []
    for path in _production_python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        contexts = _parent_contexts(tree)
        aliases = _import_aliases(tree)
        relative_path = path.relative_to(PACKAGE_ROOT).as_posix()
        for function, argument in _mutable_default_arguments(tree, aliases):
            context = _function_context(function, contexts)
            failures.append(f"{relative_path}:{function.lineno} {context} has mutable default for {argument}")
        for call, field_name in _mutable_dataclass_field_defaults(tree, aliases, contexts):
            context = _field_context(call, field_name, contexts)
            failures.append(
                f"{relative_path}:{call.lineno} {context} uses dataclass field(default=...) "
                "with mutable default; use default_factory"
            )
    if failures:
        details = "\n".join(failures)
        raise AssertionError(f"mutable default contract failed:\n{details}")
    print("OK | mutable default contract smoke passed")


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


def _mutable_default_arguments(
    tree: ast.Module,
    aliases: dict[str, str],
) -> tuple[tuple[ast.FunctionDef | ast.AsyncFunctionDef, str], ...]:
    failures = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            for argument, default in _positional_defaults(node.args):
                if _is_mutable_default(default, aliases):
                    failures.append((node, argument.arg))
            for argument, default in zip(node.args.kwonlyargs, node.args.kw_defaults, strict=True):
                if default is not None and _is_mutable_default(default, aliases):
                    failures.append((node, argument.arg))
    return tuple(failures)


def _mutable_dataclass_field_defaults(
    tree: ast.Module,
    aliases: dict[str, str],
    parents: dict[ast.AST, ast.AST],
) -> tuple[tuple[ast.Call, str], ...]:
    failures = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not _is_dataclass_field_call(node, aliases):
            continue
        default = _keyword_value(node, "default")
        if default is not None and _is_mutable_default(default, aliases):
            failures.append((node, _field_target_name(node, parents)))
    return tuple(failures)


def _is_dataclass_field_call(node: ast.Call, aliases: dict[str, str]) -> bool:
    return _call_name(node.func, aliases) == "dataclasses.field"


def _keyword_value(node: ast.Call, keyword_name: str) -> ast.expr | None:
    for keyword in node.keywords:
        if keyword.arg == keyword_name:
            return keyword.value
    return None


def _positional_defaults(args: ast.arguments) -> tuple[tuple[ast.arg, ast.expr], ...]:
    defaults = args.defaults
    if not defaults:
        return ()
    arguments = (*args.posonlyargs, *args.args)
    return tuple(zip(arguments[-len(defaults) :], defaults, strict=True))


def _is_mutable_default(node: ast.expr, aliases: dict[str, str]) -> bool:
    if isinstance(node, ast.List | ast.Dict | ast.Set):
        return True
    if isinstance(node, ast.Call):
        return _is_mutable_factory_call(_call_name(node.func, aliases))
    return False


def _is_mutable_factory_call(call_name: str | None) -> bool:
    if call_name is None:
        return False
    return call_name in MUTABLE_FACTORY_NAMES or _short_call_name(call_name) in MUTABLE_FACTORY_NAMES


def _short_call_name(call_name: str) -> str:
    return call_name.rsplit(".", 1)[-1]


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
        if alias.name in CALL_QUALIFIER_MODULES:
            aliases[alias.asname or alias.name] = alias.name
    return aliases


def _from_import_aliases(node: ast.ImportFrom) -> dict[str, str]:
    if node.module not in CALL_QUALIFIER_MODULES:
        return {}
    aliases = {}
    for alias in node.names:
        if alias.name == "*":
            continue
        if node.module == "dataclasses" and alias.name != "field":
            continue
        label = alias.asname or alias.name
        aliases[label] = _normalize_call_name(f"{node.module}.{alias.name}")
    return aliases


def _call_name(node: ast.expr, aliases: dict[str, str]) -> str | None:
    if isinstance(node, ast.Call):
        return _dynamic_getattr_call_name(node, aliases)
    if isinstance(node, ast.Name):
        return aliases.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        parent = _call_name(node.value, aliases)
        if parent in CALL_QUALIFIER_MODULES:
            return _normalize_call_name(f"{parent}.{node.attr}")
        return node.attr
    return None


def _dynamic_getattr_call_name(node: ast.Call, aliases: dict[str, str]) -> str | None:
    if _call_name(node.func, aliases) != "getattr":
        return None
    if len(node.args) < 2:
        return None
    parent = _call_name(node.args[0], aliases)
    if parent not in CALL_QUALIFIER_MODULES:
        return None
    attribute_name = _string_constant(node.args[1])
    if not attribute_name:
        return None
    return _normalize_call_name(f"{parent}.{attribute_name}")


def _string_constant(node: ast.expr) -> str:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return ""


def _normalize_call_name(call_name: str) -> str:
    if call_name.startswith("builtins."):
        return call_name.removeprefix("builtins.")
    return call_name


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


def _function_context(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
    parents: dict[ast.AST, ast.AST],
) -> str:
    parent_context = _node_context(function, parents)
    if parent_context == "<module>":
        return function.name
    return f"{parent_context}.{function.name}"


def _field_target_name(call: ast.Call, parents: dict[ast.AST, ast.AST]) -> str:
    parent = parents.get(call)
    if isinstance(parent, ast.AnnAssign):
        return _target_name(parent.target)
    if isinstance(parent, ast.Assign):
        names = tuple(_target_name(target) for target in parent.targets)
        return ", ".join(name for name in names if name != "<unknown>") or "<unknown>"
    return "<unknown>"


def _target_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _target_name(node.value)
        if parent == "<unknown>":
            return node.attr
        return f"{parent}.{node.attr}"
    return "<unknown>"


def _field_context(call: ast.Call, field_name: str, parents: dict[ast.AST, ast.AST]) -> str:
    parent_context = _node_context(call, parents)
    if field_name == "<unknown>":
        return parent_context
    if parent_context == "<module>":
        return field_name
    return f"{parent_context}.{field_name}"


def _assert_mutable_default_detector() -> None:
    tree = ast.parse(
        "\n".join(
            [
                "import dataclasses",
                "import dataclasses as dc",
                "import builtins as py_builtins",
                "import collections",
                "import collections as collection_types",
                "from builtins import list as make_list",
                "from builtins import getattr as builtins_getattr",
                "from collections import Counter as Count, defaultdict, deque as queue_type",
                "from dataclasses import field, field as data_field",
                "def ok(value=None, *, items=None):",
                "    pass",
                "def immutable_default(items=tuple()):",
                "    pass",
                "def list_default(items=[]):",
                "    pass",
                "def dict_factory(mapping=dict()):",
                "    pass",
                "async def set_default(*, seen=set()):",
                "    pass",
                "def builtin_alias(items=make_list()):",
                "    pass",
                "def builtin_module_alias(mapping=py_builtins.dict()):",
                "    pass",
                "def collection_module_default(cache=collections.defaultdict(list)):",
                "    pass",
                "def collection_module_alias(queue=collection_types.deque()):",
                "    pass",
                "def collection_import_alias(counts=Count()):",
                "    pass",
                "def dynamic_builtin(items=getattr(py_builtins, 'list')()):",
                "    pass",
                "def dynamic_builtin_alias(mapping=builtins_getattr(py_builtins, 'dict')()):",
                "    pass",
                "def dynamic_collection(queue=getattr(collections, 'deque')()):",
                "    pass",
                "class Worker:",
                "    def method(self, cache=defaultdict(list), queue=queue_type()):",
                "        pass",
                "class Payload:",
                "    ok: list[str] = field(default_factory=list)",
                "    literal_items: list[str] = field(default=[])",
                "    factory_mapping: dict[str, str] = dataclasses.field(default=dict())",
                "    alias_queue: object = dc.field(default=queue_type())",
                "    imported_items: list[str] = data_field(default=make_list())",
                "    dynamic_items: list[str] = getattr(dataclasses, 'field')(",
                "        default=getattr(py_builtins, 'list')()",
                "    )",
                "    dynamic_alias: list[str] = builtins_getattr(dc, 'field')(default=[])",
            ]
        )
    )
    _assert_equal(
        tuple((context, argument) for context, argument in _detected_contexts(tree)),
        (
            ("list_default", "items"),
            ("dict_factory", "mapping"),
            ("set_default", "seen"),
            ("builtin_alias", "items"),
            ("builtin_module_alias", "mapping"),
            ("collection_module_default", "cache"),
            ("collection_module_alias", "queue"),
            ("collection_import_alias", "counts"),
            ("dynamic_builtin", "items"),
            ("dynamic_builtin_alias", "mapping"),
            ("dynamic_collection", "queue"),
            ("Worker.method", "cache"),
            ("Worker.method", "queue"),
        ),
    )
    _assert_equal(
        _detected_dataclass_field_contexts(tree),
        (
            "Payload.literal_items",
            "Payload.factory_mapping",
            "Payload.alias_queue",
            "Payload.imported_items",
            "Payload.dynamic_items",
            "Payload.dynamic_alias",
        ),
    )


def _detected_contexts(tree: ast.Module) -> tuple[tuple[str, str], ...]:
    parents = _parent_contexts(tree)
    return tuple(
        (_function_context(function, parents), argument)
        for function, argument in _mutable_default_arguments(tree, _import_aliases(tree))
    )


def _detected_dataclass_field_contexts(tree: ast.Module) -> tuple[str, ...]:
    parents = _parent_contexts(tree)
    aliases = _import_aliases(tree)
    return tuple(
        _field_context(call, field_name, parents)
        for call, field_name in _mutable_dataclass_field_defaults(tree, aliases, parents)
    )


def _assert_equal(actual: object, expected: object) -> None:
    if actual != expected:
        raise AssertionError(f"expected {expected!r}, got {actual!r}")


if __name__ == "__main__":
    main()

from __future__ import annotations

import ast
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
IMMUTABLE_PACKAGE_ROOTS = (
    "domain",
    "services",
    "sources/yandex",
    "storage",
)
DataclassAliases = tuple[frozenset[str], frozenset[str], frozenset[str], frozenset[str]]


def main() -> None:
    _assert_dataclass_detector()
    failures = []
    for path in _value_object_python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        relative_path = path.relative_to(PACKAGE_ROOT).as_posix()
        for class_name, line_number in _mutable_dataclasses(tree):
            failures.append(f"{relative_path}:{line_number} {class_name}")
    if failures:
        details = "\n".join(failures)
        raise AssertionError(f"value object dataclasses must be frozen:\n{details}")
    print("OK | value object immutability smoke passed")


def _value_object_python_files() -> tuple[Path, ...]:
    paths = []
    for package_root in IMMUTABLE_PACKAGE_ROOTS:
        for path in sorted((PACKAGE_ROOT / package_root).rglob("*.py")):
            relative = path.relative_to(PACKAGE_ROOT)
            if _is_smoke_module(relative):
                continue
            paths.append(path)
    return tuple(paths)


def _is_smoke_module(path: Path) -> bool:
    return any(part == "smoke" for part in path.parts) or path.name.endswith("_smoke.py")


def _mutable_dataclasses(tree: ast.Module) -> tuple[tuple[str, int], ...]:
    aliases = _dataclass_aliases(tree)
    failures = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and _has_dataclass_decorator(node, aliases):
            if not _dataclass_decorator_is_frozen(node, aliases):
                failures.append((node.name, node.lineno))
    return tuple(failures)


def _dataclass_aliases(tree: ast.Module) -> DataclassAliases:
    decorator_names = {"dataclass"}
    module_names = {"dataclasses"}
    getattr_names = {"getattr"}
    builtins_module_names = {"builtins"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "dataclasses":
                    module_names.add(alias.asname or alias.name)
                elif alias.name == "builtins":
                    builtins_module_names.add(alias.asname or alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module == "dataclasses":
                decorator_names.update(alias.asname or alias.name for alias in node.names if alias.name == "dataclass")
            elif node.module == "builtins":
                getattr_names.update(alias.asname or alias.name for alias in node.names if alias.name == "getattr")
    return (
        frozenset(decorator_names),
        frozenset(module_names),
        frozenset(getattr_names),
        frozenset(builtins_module_names),
    )


def _has_dataclass_decorator(
    node: ast.ClassDef,
    aliases: DataclassAliases,
) -> bool:
    return any(_is_dataclass_decorator(decorator, aliases) for decorator in node.decorator_list)


def _dataclass_decorator_is_frozen(
    node: ast.ClassDef,
    aliases: DataclassAliases,
) -> bool:
    for decorator in node.decorator_list:
        if isinstance(decorator, ast.Call) and _is_dataclass_decorator(
            decorator.func,
            aliases,
        ):
            return _keyword_is_true(decorator, "frozen")
        if _is_dataclass_decorator(decorator, aliases):
            return False
    return False


def _is_dataclass_decorator(
    node: ast.expr,
    aliases: DataclassAliases,
) -> bool:
    decorator_names, module_names, _, _ = aliases
    if isinstance(node, ast.Call):
        if _is_dataclass_getattr_call(node, aliases):
            return True
        return _is_dataclass_decorator(node.func, aliases)
    if isinstance(node, ast.Name):
        return node.id in decorator_names
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "dataclass"
        and isinstance(node.value, ast.Name)
        and node.value.id in module_names
    )


def _is_dataclass_getattr_call(
    node: ast.Call,
    aliases: DataclassAliases,
) -> bool:
    if not _is_getattr_function(node.func, aliases):
        return False
    if len(node.args) < 2:
        return False
    if not _is_dataclasses_module(node.args[0], aliases):
        return False
    return isinstance(node.args[1], ast.Constant) and node.args[1].value == "dataclass"


def _is_getattr_function(
    node: ast.expr,
    aliases: DataclassAliases,
) -> bool:
    _, _, getattr_names, builtins_module_names = aliases
    if isinstance(node, ast.Name):
        return node.id in getattr_names
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "getattr"
        and isinstance(node.value, ast.Name)
        and node.value.id in builtins_module_names
    )


def _is_dataclasses_module(
    node: ast.expr,
    aliases: DataclassAliases,
) -> bool:
    _, module_names, _, _ = aliases
    return isinstance(node, ast.Name) and node.id in module_names


def _keyword_is_true(node: ast.Call, keyword_name: str) -> bool:
    for keyword in node.keywords:
        if keyword.arg == keyword_name and isinstance(keyword.value, ast.Constant):
            return keyword.value.value is True
    return False


def _assert_dataclass_detector() -> None:
    tree = ast.parse(
        "\n".join(
            [
                "from dataclasses import dataclass",
                "import dataclasses",
                "import dataclasses as dc",
                "from dataclasses import dataclass as value_object",
                "@dataclass(frozen=True)",
                "class Frozen:",
                "    pass",
                "@value_object(frozen=True)",
                "class FrozenAlias:",
                "    pass",
                "@dc.dataclass(frozen=True)",
                "class FrozenModuleAlias:",
                "    pass",
                "@dataclass(slots=True, frozen=True)",
                "class FrozenWithSlots:",
                "    pass",
                "@dataclass",
                "class MutableBare:",
                "    pass",
                "@value_object",
                "class MutableBareAlias:",
                "    pass",
                "@dataclass()",
                "class MutableCall:",
                "    pass",
                "@dataclasses.dataclass(frozen=False)",
                "class MutableAttribute:",
                "    pass",
                "@dc.dataclass(frozen=False)",
                "class MutableModuleAlias:",
                "    pass",
                "import builtins",
                "from builtins import getattr as builtins_getattr",
                "@getattr(dataclasses, 'dataclass')(frozen=True)",
                "class FrozenDynamic:",
                "    pass",
                "@builtins_getattr(dc, 'dataclass')(frozen=True)",
                "class FrozenDynamicAlias:",
                "    pass",
                "@getattr(dataclasses, 'dataclass')",
                "class MutableDynamicBare:",
                "    pass",
                "@builtins.getattr(dc, 'dataclass')(frozen=False)",
                "class MutableDynamicAttribute:",
                "    pass",
                "@builtins_getattr(dc, 'dataclass')()",
                "class MutableDynamicCall:",
                "    pass",
            ]
        )
    )
    _assert_equal(
        _mutable_dataclasses(tree),
        (
            ("MutableBare", 18),
            ("MutableBareAlias", 21),
            ("MutableCall", 24),
            ("MutableAttribute", 27),
            ("MutableModuleAlias", 30),
            ("MutableDynamicBare", 41),
            ("MutableDynamicAttribute", 44),
            ("MutableDynamicCall", 47),
        ),
    )


def _assert_equal(actual: object, expected: object) -> None:
    if actual != expected:
        raise AssertionError(f"expected {expected!r}, got {actual!r}")


if __name__ == "__main__":
    main()

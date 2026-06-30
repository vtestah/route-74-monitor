from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class TimeSourceKey:
    path: str
    context: str
    call: str


@dataclass(frozen=True)
class DatetimeAliases:
    modules: frozenset[str]
    datetime_classes: frozenset[str]
    date_classes: frozenset[str]
    builtins_modules: frozenset[str]
    getattr_functions: frozenset[str]


_ALLOWED_NAIVE_TIME_SOURCES = {
    TimeSourceKey("storage/connection.py", "_apply_schema_migrations", "datetime.now()"),
    TimeSourceKey("storage/db_admin.py", "backup_database", "datetime.now()"),
}


def main() -> None:
    _assert_time_source_detector()
    failures = []

    for path in _production_python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        contexts = _parent_contexts(tree)
        relative_path = path.relative_to(PACKAGE_ROOT).as_posix()
        for call in _risky_time_source_calls(tree):
            key = TimeSourceKey(relative_path, _node_context(call, contexts), ast.unparse(call).strip())
            if key in _ALLOWED_NAIVE_TIME_SOURCES:
                continue
            failures.append(f"{relative_path}:{call.lineno} {key.context} uses {key.call} without timezone")

    if failures:
        details = "\n".join(failures)
        raise AssertionError(f"time source contract failed:\n{details}")
    print("OK | time source contract smoke passed")


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


def _risky_time_source_calls(tree: ast.Module) -> tuple[ast.Call, ...]:
    aliases = _datetime_aliases(tree)
    calls = []
    calls.extend(
        _risky_time_source_calls_in_scope(
            tree,
            aliases,
            _dict_literal_kwargs_outside_functions(tree),
        )
    )
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            calls.extend(
                _risky_time_source_calls_in_scope(
                    node,
                    aliases,
                    _dict_literal_kwargs_outside_functions(node),
                )
            )
    return tuple(calls)


def _risky_time_source_calls_in_scope(
    node: ast.AST,
    aliases: DatetimeAliases,
    dict_kwargs: dict[str, dict[str, ast.expr]],
) -> tuple[ast.Call, ...]:
    return tuple(
        child for child in _calls_outside_functions(node) if _is_risky_time_source_call(child, aliases, dict_kwargs)
    )


def _datetime_aliases(tree: ast.Module) -> DatetimeAliases:
    modules = {"datetime"}
    datetime_classes = {"datetime"}
    date_classes = {"date"}
    builtins_modules = {"builtins"}
    getattr_functions = {"getattr"}

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "datetime":
                    modules.add(alias.asname or alias.name)
                elif alias.name == "builtins":
                    builtins_modules.add(alias.asname or alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module == "datetime" and node.level == 0:
            for alias in node.names:
                name = alias.asname or alias.name
                if alias.name == "datetime":
                    datetime_classes.add(name)
                elif alias.name == "date":
                    date_classes.add(name)
        elif isinstance(node, ast.ImportFrom) and node.module == "builtins" and node.level == 0:
            for alias in node.names:
                if alias.name == "getattr":
                    getattr_functions.add(alias.asname or alias.name)

    return DatetimeAliases(
        modules=frozenset(modules),
        datetime_classes=frozenset(datetime_classes),
        date_classes=frozenset(date_classes),
        builtins_modules=frozenset(builtins_modules),
        getattr_functions=frozenset(getattr_functions),
    )


def _is_risky_time_source_call(
    call: ast.Call,
    aliases: DatetimeAliases,
    dict_kwargs: dict[str, dict[str, ast.expr]],
) -> bool:
    dynamic_method = _dynamic_getattr(call.func, aliases)
    if dynamic_method is not None:
        owner, method = dynamic_method
    elif isinstance(call.func, ast.Attribute):
        owner = call.func.value
        method = call.func.attr
    else:
        return False

    if _is_datetime_class(owner, aliases):
        if method == "now":
            return not _has_non_none_timezone_argument(call, arg_index=0, dict_kwargs=dict_kwargs)
        if method == "today":
            return True
        if method == "utcnow":
            return True
        if method == "fromtimestamp":
            return not _has_non_none_timezone_argument(call, arg_index=1, dict_kwargs=dict_kwargs)
    return _is_date_class(owner, aliases) and method == "today"


def _is_datetime_class(node: ast.AST, aliases: DatetimeAliases) -> bool:
    if isinstance(node, ast.Name):
        return node.id in aliases.datetime_classes
    dynamic_attribute = _dynamic_getattr(node, aliases)
    if dynamic_attribute is not None:
        owner, attribute_name = dynamic_attribute
        return attribute_name == "datetime" and _is_datetime_module(owner, aliases)
    return isinstance(node, ast.Attribute) and node.attr == "datetime" and _is_datetime_module(node.value, aliases)


def _is_date_class(node: ast.AST, aliases: DatetimeAliases) -> bool:
    if isinstance(node, ast.Name):
        return node.id in aliases.date_classes
    dynamic_attribute = _dynamic_getattr(node, aliases)
    if dynamic_attribute is not None:
        owner, attribute_name = dynamic_attribute
        return attribute_name == "date" and _is_datetime_module(owner, aliases)
    return isinstance(node, ast.Attribute) and node.attr == "date" and _is_datetime_module(node.value, aliases)


def _is_datetime_module(node: ast.AST, aliases: DatetimeAliases) -> bool:
    return isinstance(node, ast.Name) and node.id in aliases.modules


def _dynamic_getattr(
    node: ast.AST,
    aliases: DatetimeAliases,
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


def _is_getattr_function(node: ast.AST, aliases: DatetimeAliases) -> bool:
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


def _has_non_none_timezone_argument(
    call: ast.Call,
    *,
    arg_index: int,
    dict_kwargs: dict[str, dict[str, ast.expr]],
) -> bool:
    if len(call.args) > arg_index and not _is_none_literal(call.args[arg_index]):
        return True
    for keyword in call.keywords:
        if keyword.arg == "tz" and not _is_none_literal(keyword.value):
            return True
        if keyword.arg is None and any(
            not _is_none_literal(value) for value in _unpacked_keyword_values(keyword.value, "tz", dict_kwargs)
        ):
            return True
    return False


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


def _is_none_literal(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and node.value is None


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


def _assert_time_source_detector() -> None:
    tree = ast.parse(
        "\n".join(
            [
                "from datetime import date, datetime, timezone",
                "import datetime as dt",
                "import builtins",
                "import builtins as py_builtins",
                "from datetime import date as local_date, datetime as clock",
                "from builtins import getattr as dynamic_getattr",
                "aware_kwargs = {'tz': timezone.utc}",
                "none_kwargs = {'tz': None}",
                "datetime.now()",
                "datetime.now(tz=timezone.utc)",
                "datetime.now(timezone.utc)",
                "datetime.now(tz=None)",
                "datetime.now(**aware_kwargs)",
                "datetime.now(**{'tz': timezone.utc})",
                "datetime.now(**none_kwargs)",
                "datetime.now(**{'tz': None})",
                "datetime.today()",
                "datetime.utcnow()",
                "date.today()",
                "datetime.fromtimestamp(timestamp)",
                "datetime.fromtimestamp(timestamp, tz=timezone.utc)",
                "datetime.fromtimestamp(timestamp, timezone.utc)",
                "datetime.fromtimestamp(timestamp, **aware_kwargs)",
                "datetime.fromtimestamp(timestamp, **none_kwargs)",
                "dt.datetime.now()",
                "dt.datetime.now(tz=timezone.utc)",
                "dt.datetime.today()",
                "dt.datetime.utcnow()",
                "dt.datetime.fromtimestamp(timestamp)",
                "dt.datetime.fromtimestamp(timestamp, tz=timezone.utc)",
                "dt.date.today()",
                "def function_scope():",
                "    function_aware_kwargs = {'tz': timezone.utc}",
                "    function_none_kwargs = {'tz': None}",
                "    datetime.now(**function_aware_kwargs)",
                "    datetime.now(**function_none_kwargs)",
                "clock.now()",
                "clock.now(tz=timezone.utc)",
                "clock.today()",
                "clock.fromtimestamp(timestamp)",
                "clock.fromtimestamp(timestamp, tz=timezone.utc)",
                "local_date.today()",
                "getattr(datetime, 'now')()",
                "getattr(datetime, 'now')(timezone.utc)",
                "getattr(datetime, 'now')(tz=None)",
                "getattr(datetime, 'today')()",
                "getattr(datetime, 'utcnow')()",
                "getattr(datetime, 'fromtimestamp')(timestamp)",
                "getattr(datetime, 'fromtimestamp')(timestamp, timezone.utc)",
                "getattr(date, 'today')()",
                "builtins.getattr(dt.datetime, 'now')()",
                "builtins.getattr(dt.datetime, 'today')()",
                "py_builtins.getattr(clock, 'fromtimestamp')(timestamp)",
                "dynamic_getattr(local_date, 'today')()",
                "getattr(dt, 'datetime').utcnow()",
                "getattr(dt, 'datetime').today()",
                "getattr(getattr(dt, 'datetime'), 'now')()",
                "getattr(getattr(dt, 'datetime'), 'today')()",
                "getattr(getattr(dt, 'date'), 'today')()",
                "getattr(object(), 'now')()",
                "getattr(datetime, method_name)()",
            ]
        )
    )
    _assert_equal(
        tuple(ast.unparse(call).strip() for call in _risky_time_source_calls(tree)),
        (
            "datetime.now()",
            "datetime.now(tz=None)",
            "datetime.now(**none_kwargs)",
            "datetime.now(**{'tz': None})",
            "datetime.today()",
            "datetime.utcnow()",
            "date.today()",
            "datetime.fromtimestamp(timestamp)",
            "datetime.fromtimestamp(timestamp, **none_kwargs)",
            "dt.datetime.now()",
            "dt.datetime.today()",
            "dt.datetime.utcnow()",
            "dt.datetime.fromtimestamp(timestamp)",
            "dt.date.today()",
            "clock.now()",
            "clock.today()",
            "clock.fromtimestamp(timestamp)",
            "local_date.today()",
            "getattr(datetime, 'now')()",
            "getattr(datetime, 'now')(tz=None)",
            "getattr(datetime, 'today')()",
            "getattr(datetime, 'utcnow')()",
            "getattr(datetime, 'fromtimestamp')(timestamp)",
            "getattr(date, 'today')()",
            "builtins.getattr(dt.datetime, 'now')()",
            "builtins.getattr(dt.datetime, 'today')()",
            "py_builtins.getattr(clock, 'fromtimestamp')(timestamp)",
            "dynamic_getattr(local_date, 'today')()",
            "getattr(dt, 'datetime').utcnow()",
            "getattr(dt, 'datetime').today()",
            "getattr(getattr(dt, 'datetime'), 'now')()",
            "getattr(getattr(dt, 'datetime'), 'today')()",
            "getattr(getattr(dt, 'date'), 'today')()",
            "datetime.now(**function_none_kwargs)",
        ),
    )


def _assert_equal(actual: object, expected: object) -> None:
    if actual != expected:
        raise AssertionError(f"expected {expected!r}, got {actual!r}")


if __name__ == "__main__":
    main()

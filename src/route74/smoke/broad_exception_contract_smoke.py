from __future__ import annotations

import ast
from collections import Counter
from dataclasses import dataclass
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
BROAD_EXCEPTION_NAMES = frozenset({"BaseException", "Exception"})
BROAD_EXCEPTION_QUALIFIED_NAMES = frozenset(
    {
        "builtins.BaseException",
        "builtins.Exception",
        *BROAD_EXCEPTION_NAMES,
    }
)
SUPPRESS_FUNCTION_NAMES = frozenset({"contextlib.suppress", "suppress"})


@dataclass(frozen=True)
class HandlerKey:
    path: str
    context: str
    exception_type: str
    body: str


_ALLOWED_UNBOUND_BROAD_HANDLERS = {
    HandlerKey(
        "models.py",
        "_timezone_label",
        "Exception",
        "tz_name = None",
    ): 1,
    HandlerKey(
        "sources/yandex/browser_client.py",
        "ReusableChromium.close",
        "Exception",
        "pass",
    ): 2,
    HandlerKey(
        "sources/yandex/browser_client.py",
        "_browser_connected",
        "Exception",
        "return False",
    ): 1,
    HandlerKey(
        "sources/yandex/browser_client.py",
        "_close_page",
        "Exception",
        "pass",
    ): 1,
    HandlerKey(
        "sources/yandex/browser_client.py",
        "_capture_response",
        "Exception",
        "parse_errors.append(invalid_json_reason); return",
    ): 1,
    HandlerKey(
        "sources/yandex/browser_client.py",
        "_capture_line_response",
        "Exception",
        "return",
    ): 1,
    HandlerKey(
        "sources/yandex/browser_client.py",
        "capture_prediction_response",
        "Exception",
        "_append_parse_error(parse_errors, 'vehicle_prediction_json_invalid'); return",
    ): 1,
    HandlerKey(
        "sources/yandex/browser_client.py",
        "_capture_route_vehicles",
        "Exception",
        "parse_errors.append('browser_route_vehicles_json_invalid'); return",
    ): 1,
    HandlerKey(
        "sources/yandex/dump.py",
        "_capture_dump_response",
        "Exception",
        "raw_payload = None",
    ): 1,
    HandlerKey(
        "sources/yandex/route_traffic.py",
        "_close_page",
        "Exception",
        "pass",
    ): 1,
}


def main() -> None:
    _assert_broad_exception_detector()
    _assert_broad_suppress_detector()
    failures = []
    observed_allowed: Counter[HandlerKey] = Counter()

    for path in _production_python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        contexts = _parent_contexts(tree)
        relative_path = path.relative_to(PACKAGE_ROOT).as_posix()
        for handler in _broad_exception_handlers(tree):
            if _handler_reraises(handler):
                continue
            context = _handler_context(handler, contexts)
            if handler.name is not None:
                if not _handler_uses_exception_name(handler):
                    failures.append(
                        f"{relative_path}:{handler.lineno} {context} catches "
                        f"{_exception_type(handler)} as {handler.name} without diagnostic use"
                    )
                continue

            key = HandlerKey(
                relative_path,
                context,
                _exception_type(handler),
                _handler_body(handler),
            )
            if key in _ALLOWED_UNBOUND_BROAD_HANDLERS:
                observed_allowed[key] += 1
            else:
                failures.append(
                    f"{relative_path}:{handler.lineno} {context} catches "
                    f"{key.exception_type} without binding, diagnostics, or re-raise: {key.body}"
                )
        for call in _broad_suppress_calls(tree):
            context = _node_context(call, contexts)
            failures.append(
                f"{relative_path}:{call.lineno} {context} suppresses "
                f"{_suppress_exception_types(call)} without diagnostics"
            )

    for key, expected_count in _ALLOWED_UNBOUND_BROAD_HANDLERS.items():
        actual_count = observed_allowed[key]
        if actual_count != expected_count:
            failures.append(
                "broad exception allowlist is stale for "
                f"{key.path} {key.context}: expected {expected_count}, saw {actual_count}"
            )

    if failures:
        details = "\n".join(failures)
        raise AssertionError(f"broad exception contract failed:\n{details}")
    print("OK | broad exception contract smoke passed")


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


def _broad_exception_handlers(tree: ast.Module) -> tuple[ast.ExceptHandler, ...]:
    aliases = _import_aliases(tree)
    handlers = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler) and _is_broad_exception_type(node.type, aliases):
            handlers.append(node)
    return tuple(handlers)


def _broad_suppress_calls(tree: ast.Module) -> tuple[ast.Call, ...]:
    aliases = _import_aliases(tree)
    calls = []
    for node in ast.walk(tree):
        if isinstance(node, ast.With | ast.AsyncWith):
            for item in node.items:
                context_expr = item.context_expr
                if isinstance(context_expr, ast.Call) and _is_broad_suppress_call(context_expr, aliases):
                    calls.append(context_expr)
    return tuple(calls)


def _is_broad_suppress_call(call: ast.Call, aliases: dict[str, str]) -> bool:
    if not _is_suppress_function(call.func, aliases):
        return False
    return any(_is_broad_exception_type(argument, aliases) for argument in call.args)


def _is_suppress_function(node: ast.expr, aliases: dict[str, str]) -> bool:
    return _qualified_name(node, aliases) in SUPPRESS_FUNCTION_NAMES


def _suppress_exception_types(call: ast.Call) -> str:
    return ", ".join(ast.unparse(argument).strip() for argument in call.args)


def _is_broad_exception_type(node: ast.expr | None, aliases: dict[str, str]) -> bool:
    if node is None:
        return True
    if _is_broad_exception_name(node, aliases):
        return True
    if _is_broad_exception_getattr(node, aliases):
        return True
    if isinstance(node, ast.Tuple):
        return any(_is_broad_exception_type(element, aliases) for element in node.elts)
    return False


def _is_broad_exception_name(node: ast.expr, aliases: dict[str, str]) -> bool:
    return _qualified_name(node, aliases) in BROAD_EXCEPTION_QUALIFIED_NAMES


def _is_broad_exception_getattr(node: ast.expr, aliases: dict[str, str]) -> bool:
    if not isinstance(node, ast.Call):
        return False
    if _qualified_name(node.func, aliases) not in {"builtins.getattr", "getattr"}:
        return False
    if len(node.args) < 2:
        return False
    if _qualified_name(node.args[0], aliases) != "builtins":
        return False
    if not isinstance(node.args[1], ast.Constant) or not isinstance(node.args[1].value, str):
        return False
    return node.args[1].value in BROAD_EXCEPTION_NAMES


def _import_aliases(tree: ast.Module) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in {"builtins", "contextlib"}:
                    aliases[alias.asname or alias.name] = alias.name
        elif isinstance(node, ast.ImportFrom) and node.module in {"builtins", "contextlib"}:
            for alias in node.names:
                if alias.name == "*":
                    continue
                aliases[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    return aliases


def _qualified_name(node: ast.expr, aliases: dict[str, str]) -> str:
    if isinstance(node, ast.Name):
        return aliases.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        parent = _qualified_name(node.value, aliases)
        return f"{parent}.{node.attr}" if parent else node.attr
    return ""


def _handler_reraises(handler: ast.ExceptHandler) -> bool:
    return any(isinstance(statement, ast.Raise) for statement in handler.body)


def _handler_uses_exception_name(handler: ast.ExceptHandler) -> bool:
    if handler.name is None:
        return False
    body_module = ast.Module(body=handler.body, type_ignores=[])
    parents = _parent_contexts(body_module)
    return any(
        _is_exception_name_load(node, handler.name) and _has_diagnostic_context(node, parents)
        for node in _walk_handler_body(handler.body)
    )


def _walk_handler_body(body: list[ast.stmt]) -> tuple[ast.AST, ...]:
    nodes = []
    pending: list[ast.AST] = list(reversed(body))
    while pending:
        node = pending.pop()
        nodes.append(node)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef | ast.Lambda):
            continue
        pending.extend(reversed(tuple(ast.iter_child_nodes(node))))
    return tuple(nodes)


def _is_exception_name_load(node: ast.AST, name: str) -> bool:
    return isinstance(node, ast.Name) and node.id == name and isinstance(node.ctx, ast.Load)


def _has_diagnostic_context(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> bool:
    while node in parents:
        node = parents[node]
        if isinstance(node, ast.Call | ast.FormattedValue):
            return True
        if isinstance(node, ast.Assign | ast.AnnAssign | ast.AugAssign | ast.Delete):
            return False
    return False


def _exception_type(handler: ast.ExceptHandler) -> str:
    if handler.type is None:
        return "bare except"
    return ast.unparse(handler.type)


def _handler_body(handler: ast.ExceptHandler) -> str:
    return "; ".join(ast.unparse(statement).strip() for statement in handler.body)


def _parent_contexts(tree: ast.Module) -> dict[ast.AST, ast.AST]:
    parents = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    return parents


def _handler_context(handler: ast.ExceptHandler, parents: dict[ast.AST, ast.AST]) -> str:
    return _node_context(handler, parents)


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


def _assert_broad_exception_detector() -> None:
    tree = ast.parse(
        "\n".join(
            [
                "import builtins",
                "import builtins as py_builtins",
                "from builtins import BaseException as FatalError, Exception as BroadError",
                "from builtins import getattr as builtins_getattr",
                "try:",
                "    pass",
                "except (OSError, Exception):",
                "    pass",
                "try:",
                "    pass",
                "except (ValueError, RuntimeError):",
                "    pass",
                "try:",
                "    pass",
                "except BaseException:",
                "    raise",
                "try:",
                "    pass",
                "except:",
                "    pass",
                "try:",
                "    pass",
                "except builtins.Exception:",
                "    pass",
                "try:",
                "    pass",
                "except py_builtins.BaseException:",
                "    pass",
                "try:",
                "    pass",
                "except BroadError:",
                "    pass",
                "try:",
                "    pass",
                "except getattr(builtins, 'Exception'):",
                "    pass",
                "try:",
                "    pass",
                "except builtins_getattr(py_builtins, 'BaseException'):",
                "    pass",
                "try:",
                "    pass",
                "except (ValueError, FatalError):",
                "    pass",
                "try:",
                "    pass",
                "except (ValueError, getattr(builtins, 'BaseException')):",
                "    pass",
            ]
        )
    )
    _assert_equal(
        tuple(_exception_type(handler) for handler in _broad_exception_handlers(tree)),
        (
            "(OSError, Exception)",
            "BaseException",
            "bare except",
            "builtins.Exception",
            "py_builtins.BaseException",
            "BroadError",
            "getattr(builtins, 'Exception')",
            "builtins_getattr(py_builtins, 'BaseException')",
            "(ValueError, FatalError)",
            "(ValueError, getattr(builtins, 'BaseException'))",
        ),
    )
    _assert_equal(
        tuple(_handler_uses_exception_name(handler) for handler in _bound_handlers("error = None")),
        (False,),
    )
    _assert_equal(
        tuple(_handler_uses_exception_name(handler) for handler in _bound_handlers("_ = error")),
        (False,),
    )
    _assert_equal(
        tuple(_handler_uses_exception_name(handler) for handler in _bound_handlers("print(f'boom: {error}')")),
        (True,),
    )
    _assert_equal(
        tuple(_handler_reraises(handler) for handler in _handlers_with_body("def later():", "    raise")),
        (False,),
    )
    _assert_equal(
        tuple(_handler_reraises(handler) for handler in _handlers_with_body("if debug:", "    raise")),
        (False,),
    )
    _assert_equal(
        tuple(_handler_reraises(handler) for handler in _handlers_with_body("print('diagnostic')", "raise")),
        (True,),
    )
    _assert_equal(
        tuple(
            _handler_uses_exception_name(handler) for handler in _handlers_with_body("def later():", "    print(error)")
        ),
        (False,),
    )
    _assert_equal(
        tuple(
            _handler_uses_exception_name(handler)
            for handler in _bound_handlers("reason = _error_reason('source_error', error)")
        ),
        (True,),
    )


def _assert_broad_suppress_detector() -> None:
    tree = ast.parse(
        "\n".join(
            [
                "from contextlib import suppress",
                "from contextlib import suppress as quiet",
                "from builtins import Exception as BroadError",
                "from builtins import getattr as builtins_getattr",
                "import builtins",
                "import contextlib",
                "import contextlib as context_tools",
                "with suppress(FileNotFoundError):",
                "    pass",
                "with suppress(Exception):",
                "    pass",
                "with contextlib.suppress(OSError, BaseException):",
                "    pass",
                "with quiet(BroadError):",
                "    pass",
                "with context_tools.suppress(OSError, builtins.BaseException):",
                "    pass",
                "with suppress(getattr(builtins, 'Exception')):",
                "    pass",
                "with quiet(builtins_getattr(builtins, 'BaseException')):",
                "    pass",
            ]
        )
    )
    _assert_equal(
        tuple(ast.unparse(call).strip() for call in _broad_suppress_calls(tree)),
        (
            "suppress(Exception)",
            "contextlib.suppress(OSError, BaseException)",
            "quiet(BroadError)",
            "context_tools.suppress(OSError, builtins.BaseException)",
            "suppress(getattr(builtins, 'Exception'))",
            "quiet(builtins_getattr(builtins, 'BaseException'))",
        ),
    )
    _assert_equal(
        tuple(_suppress_exception_types(call) for call in _broad_suppress_calls(tree)),
        (
            "Exception",
            "OSError, BaseException",
            "BroadError",
            "OSError, builtins.BaseException",
            "getattr(builtins, 'Exception')",
            "builtins_getattr(builtins, 'BaseException')",
        ),
    )


def _bound_handlers(statement: str) -> tuple[ast.ExceptHandler, ...]:
    return _handlers_with_body(statement, bind_name=True)


def _handlers_with_body(*body_lines: str, bind_name: bool = False) -> tuple[ast.ExceptHandler, ...]:
    except_line = "except Exception as error:" if bind_name else "except Exception:"
    tree = ast.parse(
        "\n".join(
            [
                "try:",
                "    pass",
                except_line,
                *(f"    {line}" for line in body_lines),
            ]
        )
    )
    return tuple(node for node in ast.walk(tree) if isinstance(node, ast.ExceptHandler))


def _assert_equal(actual: object, expected: object) -> None:
    if actual != expected:
        raise AssertionError(f"expected {expected!r}, got {actual!r}")


if __name__ == "__main__":
    main()

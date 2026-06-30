from __future__ import annotations

import ast
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]

HTTPX_CLIENT_CALLS = frozenset({"httpx.Client", "httpx.AsyncClient"})
TIMEOUT_MISSING = "missing"
TIMEOUT_NON_POSITIVE = "non_positive"
TIMEOUT_VALID = "valid"
DIRECT_HTTPX_CALLS = frozenset(
    {
        "httpx.delete",
        "httpx.get",
        "httpx.head",
        "httpx.options",
        "httpx.patch",
        "httpx.post",
        "httpx.put",
        "httpx.request",
        "httpx.stream",
    }
)


def main() -> None:
    _assert_timeout_detector()
    failures = []
    for path in _production_module_paths():
        violations = _network_timeout_violations(path)
        if violations:
            relative = path.relative_to(PACKAGE_ROOT)
            failures.append(f"{relative}: {', '.join(violations)}")
    if failures:
        details = "\n".join(failures)
        raise AssertionError(f"network calls must have explicit timeouts:\n{details}")
    print("OK | network timeout smoke passed")


def _production_module_paths() -> tuple[Path, ...]:
    paths = []
    for path in sorted(PACKAGE_ROOT.rglob("*.py")):
        parts = path.relative_to(PACKAGE_ROOT).with_suffix("").parts
        if any(part == "smoke" for part in parts) or parts[-1].endswith("_smoke"):
            continue
        paths.append(path)
    return tuple(paths)


def _network_timeout_violations(path: Path) -> tuple[str, ...]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return tuple(sorted(_module_timeout_violations(tree)))


def _module_timeout_violations(tree: ast.Module) -> set[str]:
    violations: set[str] = set()
    aliases = _import_aliases(tree)
    module_timeouts = _dict_literal_timeouts_outside_functions(tree)
    for child in _calls_outside_functions(tree):
        violations.update(_call_timeout_violations(child, module_timeouts, aliases))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            violations.update(_function_timeout_violations(node, aliases))
    return violations


def _function_timeout_violations(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    aliases: dict[str, str],
) -> set[str]:
    dict_timeouts = _dict_literal_timeouts(node)
    violations: set[str] = set()
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        violations.update(_call_timeout_violations(child, dict_timeouts, aliases))
    return violations


def _call_timeout_violations(
    node: ast.Call,
    dict_timeouts: dict[str, str],
    aliases: dict[str, str],
) -> set[str]:
    violations: set[str] = set()
    call_name = _call_name(node.func, aliases)
    if call_name in DIRECT_HTTPX_CALLS:
        violations.add(_label(node, f"{call_name} bypasses configured client timeout"))
    if call_name in HTTPX_CLIENT_CALLS:
        status = _call_timeout_status(node, dict_timeouts)
        if status == TIMEOUT_MISSING:
            violations.add(_label(node, f"{call_name} missing timeout"))
        elif status == TIMEOUT_NON_POSITIVE:
            violations.add(_label(node, f"{call_name} non-positive timeout"))
    if call_name.endswith(".goto"):
        status = _call_timeout_status(node, dict_timeouts)
        if status == TIMEOUT_MISSING:
            violations.add(_label(node, f"{call_name} missing timeout"))
        elif status == TIMEOUT_NON_POSITIVE:
            violations.add(_label(node, f"{call_name} non-positive timeout"))
    return violations


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
        if alias.name == "httpx":
            label = alias.asname or alias.name
            aliases[label] = alias.name
    return aliases


def _from_import_aliases(node: ast.ImportFrom) -> dict[str, str]:
    if node.module != "httpx":
        return {}
    aliases = {}
    for alias in node.names:
        if alias.name == "*":
            continue
        label = alias.asname or alias.name
        aliases[label] = f"httpx.{alias.name}"
    return aliases


def _dict_literal_timeouts(node: ast.AST) -> dict[str, str]:
    timeouts: dict[str, str] = {}
    for child in ast.walk(node):
        target: ast.expr | None = None
        value: ast.expr | None = None
        if isinstance(child, ast.Assign) and len(child.targets) == 1:
            target = child.targets[0]
            value = child.value
        elif isinstance(child, ast.AnnAssign):
            target = child.target
            value = child.value
        if not isinstance(target, ast.Name) or not isinstance(value, ast.Dict):
            continue
        timeouts[target.id] = _dict_timeout_status(value)
    return timeouts


def _dict_literal_timeouts_outside_functions(node: ast.AST) -> dict[str, str]:
    timeouts: dict[str, str] = {}
    for child in _nodes_outside_functions(node):
        target: ast.expr | None = None
        value: ast.expr | None = None
        if isinstance(child, ast.Assign) and len(child.targets) == 1:
            target = child.targets[0]
            value = child.value
        elif isinstance(child, ast.AnnAssign):
            target = child.target
            value = child.value
        if not isinstance(target, ast.Name) or not isinstance(value, ast.Dict):
            continue
        timeouts[target.id] = _dict_timeout_status(value)
    return timeouts


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


def _call_timeout_status(node: ast.Call, dict_timeouts: dict[str, str]) -> str:
    for keyword in node.keywords:
        if keyword.arg == "timeout":
            return _timeout_value_status(keyword.value)
        if keyword.arg is None:
            status = _unpacked_mapping_timeout_status(keyword.value, dict_timeouts)
            if status != TIMEOUT_MISSING:
                return status
    return TIMEOUT_MISSING


def _unpacked_mapping_timeout_status(node: ast.expr, dict_timeouts: dict[str, str]) -> str:
    if isinstance(node, ast.Name):
        return dict_timeouts.get(node.id, TIMEOUT_MISSING)
    if isinstance(node, ast.Dict):
        return _dict_timeout_status(node)
    return TIMEOUT_MISSING


def _dict_timeout_status(node: ast.Dict) -> str:
    status = TIMEOUT_MISSING
    for key, value in zip(node.keys, node.values):
        if isinstance(key, ast.Constant) and key.value == "timeout":
            status = _timeout_value_status(value)
    return status


def _timeout_value_status(node: ast.expr) -> str:
    if _is_none_constant(node):
        return TIMEOUT_MISSING
    numeric_value = _numeric_literal(node)
    if numeric_value is not None and numeric_value <= 0:
        return TIMEOUT_NON_POSITIVE
    return TIMEOUT_VALID


def _numeric_literal(node: ast.expr) -> float | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, int | float):
        return None if isinstance(node.value, bool) else float(node.value)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub | ast.UAdd):
        value = _numeric_literal(node.operand)
        if value is None:
            return None
        return -value if isinstance(node.op, ast.USub) else value
    return None


def _is_none_constant(node: ast.expr) -> bool:
    return isinstance(node, ast.Constant) and node.value is None


def _call_name(node: ast.expr, aliases: dict[str, str]) -> str:
    if isinstance(node, ast.Name):
        return aliases.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        parent = _call_name(node.value, aliases)
        return f"{parent}.{node.attr}" if parent else node.attr
    return ""


def _label(node: ast.AST, message: str) -> str:
    return f"line {getattr(node, 'lineno', '?')}: {message}"


def _assert_timeout_detector() -> None:
    tree = ast.parse(
        "\n".join(
            [
                "import httpx",
                "httpx.Client()",
                "module_kwargs = {'timeout': 1.0}",
                "httpx.AsyncClient(**module_kwargs)",
                "module_none_kwargs = {'timeout': None}",
                "httpx.AsyncClient(**module_none_kwargs)",
                "module_zero_kwargs = {'timeout': 0}",
                "httpx.AsyncClient(**module_zero_kwargs)",
                "httpx.Client(**{'timeout': None})",
                "httpx.Client(**{'timeout': 0})",
                "class Browser:",
                "    page.goto('/import-side-effect')",
                "def direct():",
                "    httpx.get('https://example.test')",
                "def explicit_client():",
                "    httpx.Client(timeout=8.0)",
                "def zero_client():",
                "    httpx.Client(timeout=0)",
                "def negative_client():",
                "    httpx.Client(timeout=-1)",
                "def kwargs_client(timeout):",
                "    kwargs = {'timeout': timeout}",
                "    httpx.Client(**kwargs)",
                "def none_kwargs_client():",
                "    kwargs = {'timeout': None}",
                "    httpx.Client(**kwargs)",
                "def zero_kwargs_client():",
                "    kwargs = {'timeout': 0}",
                "    httpx.Client(**kwargs)",
                "def missing_client():",
                "    httpx.Client(base_url='https://example.test')",
                "def missing_nav(page, url):",
                "    page.goto(url)",
                "def ok_nav(page, url):",
                "    page.goto(url, timeout=8000)",
                "def zero_nav(page, url):",
                "    page.goto(url, timeout=0)",
                "def negative_nav(page, url):",
                "    page.goto(url, timeout=-100)",
                "def none_timeout(page, url):",
                "    page.goto(url, timeout=None)",
                "def nav_kwargs(page, url):",
                "    kwargs = {'timeout': 4000}",
                "    page.goto(url, **kwargs)",
                "def zero_nav_kwargs(page, url):",
                "    kwargs = {'timeout': 0}",
                "    page.goto(url, **kwargs)",
                "def aliased_client():",
                "    import httpx as hx",
                "    hx.Client()",
                "def imported_client():",
                "    from httpx import AsyncClient as ImportedAsyncClient",
                "    ImportedAsyncClient()",
                "def imported_direct():",
                "    from httpx import get as imported_get",
                "    imported_get('https://example.test')",
            ]
        )
    )
    violations = _module_timeout_violations(tree)
    _assert_equal(
        violations,
        {
            "line 2: httpx.Client missing timeout",
            "line 6: httpx.AsyncClient missing timeout",
            "line 8: httpx.AsyncClient non-positive timeout",
            "line 9: httpx.Client missing timeout",
            "line 10: httpx.Client non-positive timeout",
            "line 12: page.goto missing timeout",
            "line 14: httpx.get bypasses configured client timeout",
            "line 18: httpx.Client non-positive timeout",
            "line 20: httpx.Client non-positive timeout",
            "line 26: httpx.Client missing timeout",
            "line 29: httpx.Client non-positive timeout",
            "line 31: httpx.Client missing timeout",
            "line 33: page.goto missing timeout",
            "line 37: page.goto non-positive timeout",
            "line 39: page.goto non-positive timeout",
            "line 41: page.goto missing timeout",
            "line 47: page.goto non-positive timeout",
            "line 50: httpx.Client missing timeout",
            "line 53: httpx.AsyncClient missing timeout",
            "line 56: httpx.get bypasses configured client timeout",
        },
    )


def _assert_equal(actual: object, expected: object) -> None:
    if actual != expected:
        raise AssertionError(f"expected {expected!r}, got {actual!r}")


if __name__ == "__main__":
    main()

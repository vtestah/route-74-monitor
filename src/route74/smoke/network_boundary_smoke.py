from __future__ import annotations

import ast
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
NETWORK_IMPORT_PREFIXES = (
    "aiohttp",
    "http.client",
    "httpx",
    "playwright.sync_api",
    "requests",
    "socket",
    "urllib.request",
)
NETWORK_CALL_NAMES = frozenset(
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
        "urllib.request.urlopen",
        "urllib.request.urlretrieve",
    }
)
NETWORK_MODULE_ALLOWLIST = frozenset(
    {
        "dashboard/preview.py",
        "notifications/pushover.py",
        "sources/yandex/browser_client.py",
        "sources/yandex/dump.py",
        "sources/yandex/http_client.py",
        "sources/yandex/route_traffic.py",
    }
)


def main() -> None:
    _assert_network_boundary_detector()
    failures = []
    for path in _production_python_files():
        relative_path = path.relative_to(PACKAGE_ROOT)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        failures.extend(
            f"{relative_path.as_posix()}:{line_number} {label}"
            for line_number, label in _network_boundary_violations(tree, relative_path)
        )
    if failures:
        details = "\n".join(failures)
        raise AssertionError(f"network boundary contract failed:\n{details}")
    print("OK | network boundary smoke passed")


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


def _network_boundary_violations(
    tree: ast.Module,
    relative_path: Path,
) -> tuple[tuple[int, str], ...]:
    if relative_path.as_posix() in NETWORK_MODULE_ALLOWLIST:
        return ()

    violations = []
    for node in ast.walk(tree):
        imported_modules = _network_imports(node)
        violations.extend(
            (node.lineno, f"network import must stay in transport modules: {module_name}")
            for module_name in imported_modules
        )
        if isinstance(node, ast.Call) and _call_name(node.func) in NETWORK_CALL_NAMES:
            violations.append((node.lineno, "direct network calls must stay in transport modules"))
    return tuple(violations)


def _network_imports(node: ast.AST) -> tuple[str, ...]:
    if isinstance(node, ast.Import):
        return tuple(alias.name for alias in node.names if _is_network_module(alias.name))
    if isinstance(node, ast.ImportFrom):
        module_name = node.module or ""
        if _is_network_module(module_name):
            return (module_name,)
        return tuple(
            f"{module_name}.{alias.name}"
            for alias in node.names
            if module_name and _is_network_module(f"{module_name}.{alias.name}")
        )
    return ()


def _is_network_module(module_name: str) -> bool:
    return any(module_name == prefix or module_name.startswith(f"{prefix}.") for prefix in NETWORK_IMPORT_PREFIXES)


def _call_name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _call_name(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    return ""


def _assert_network_boundary_detector() -> None:
    tree = ast.parse(
        "\n".join(
            [
                "import httpx",
                "from urllib.parse import urlencode",
                "from urllib.request import urlopen",
                "from playwright.sync_api import sync_playwright",
                "httpx.get('https://example.test')",
                "urllib.request.urlopen('https://example.test')",
            ]
        )
    )
    _assert_equal(_labels(tree, "notifications/pushover.py"), ())
    _assert_equal(_labels(tree, "sources/yandex/http_client.py"), ())
    _assert_equal(
        _labels(tree, "services/commute.py"),
        (
            "network import must stay in transport modules: httpx",
            "network import must stay in transport modules: urllib.request",
            "network import must stay in transport modules: playwright.sync_api",
            "direct network calls must stay in transport modules",
            "direct network calls must stay in transport modules",
        ),
    )


def _labels(tree: ast.Module, path: str) -> tuple[str, ...]:
    return tuple(label for _, label in _network_boundary_violations(tree, Path(path)))


def _assert_equal(actual: object, expected: object) -> None:
    if actual != expected:
        raise AssertionError(f"expected {expected!r}, got {actual!r}")


if __name__ == "__main__":
    main()

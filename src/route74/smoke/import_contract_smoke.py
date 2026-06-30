from __future__ import annotations

import ast
import importlib
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    _assert_module_name_contract()
    _assert_smoke_import_detector()
    _assert_no_smoke_runtime_imports()
    failures = []
    imported_count = 0
    for module_name in _production_module_names():
        try:
            importlib.import_module(module_name)
        except Exception as exc:
            failures.append(f"{module_name}: {type(exc).__name__}: {exc}")
        else:
            imported_count += 1
    if failures:
        details = "\n".join(failures)
        raise AssertionError(f"production modules must import cleanly:\n{details}")
    print(f"OK | import contract smoke passed modules={imported_count}")


def _production_module_names() -> tuple[str, ...]:
    return tuple(
        _module_name(path.relative_to(PACKAGE_ROOT).with_suffix("").parts) for path in _production_module_paths()
    )


def _production_module_paths() -> tuple[Path, ...]:
    names = []
    for path in sorted(PACKAGE_ROOT.rglob("*.py")):
        relative = path.relative_to(PACKAGE_ROOT)
        parts = relative.with_suffix("").parts
        if _is_smoke_module(parts):
            continue
        names.append(path)
    return tuple(names)


def _is_smoke_module(parts: tuple[str, ...]) -> bool:
    return any(part == "smoke" for part in parts) or parts[-1].endswith("_smoke")


def _module_name(parts: tuple[str, ...]) -> str:
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(("route74", *parts))


def _assert_module_name_contract() -> None:
    names = _production_module_names()
    if len(names) != len(set(names)):
        raise AssertionError("production module contract must not contain duplicate module names")
    if any(name.endswith(".__init__") for name in names):
        raise AssertionError("production module contract must import packages by package name")
    for package_name in ("route74", "route74.cli", "route74.sources.yandex"):
        if package_name not in names:
            raise AssertionError(f"production module contract must include package: {package_name}")


def _assert_no_smoke_runtime_imports() -> None:
    failures = []
    for path in _production_module_paths():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imports = _smoke_runtime_imports(tree)
        if imports:
            relative = path.relative_to(PACKAGE_ROOT)
            failures.append(f"{relative}: {', '.join(imports)}")
    if failures:
        details = "\n".join(failures)
        raise AssertionError(f"production modules must not import smoke modules:\n{details}")


def _smoke_runtime_imports(tree: ast.Module) -> tuple[str, ...]:
    imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names if _is_route74_smoke_module(alias.name))
        elif isinstance(node, ast.ImportFrom):
            imports.extend(_smoke_import_from_labels(node))
    return tuple(imports)


def _smoke_import_from_labels(node: ast.ImportFrom) -> tuple[str, ...]:
    module = node.module or ""
    labels = []
    if node.level > 0:
        prefix = "." * node.level
        if _contains_smoke_part(module):
            labels.append(f"{prefix}{module}")
        labels.extend(
            f"{prefix}{module + '.' if module else ''}{alias.name}"
            for alias in node.names
            if _contains_smoke_part(alias.name)
        )
        return tuple(labels)
    if not _is_route74_module(module):
        return ()
    if _contains_smoke_part(module):
        labels.append(module)
    labels.extend(
        f"{module}.{alias.name}" for alias in node.names if alias.name != "*" and _contains_smoke_part(alias.name)
    )
    return tuple(labels)


def _is_route74_smoke_module(name: str) -> bool:
    return _is_route74_module(name) and _contains_smoke_part(name)


def _is_route74_module(name: str) -> bool:
    return name == "route74" or name.startswith("route74.")


def _contains_smoke_part(name: str) -> bool:
    return any(part == "smoke" or part.endswith("_smoke") for part in name.split("."))


def _assert_smoke_import_detector() -> None:
    tree = ast.parse(
        "\n".join(
            [
                "import route74.smoke.fake_smoke",
                "from route74.web import app",
                "from route74.web import runtime",
                "from . import local_smoke",
                "from .smoke import runner",
                "from external import smoke",
            ]
        )
    )
    _assert_equal(
        _smoke_runtime_imports(tree),
        (
            "route74.smoke.fake_smoke",
            ".local_smoke",
            ".smoke",
        ),
    )


def _assert_equal(actual: object, expected: object) -> None:
    if actual != expected:
        raise AssertionError(f"expected {expected!r}, got {actual!r}")


if __name__ == "__main__":
    main()

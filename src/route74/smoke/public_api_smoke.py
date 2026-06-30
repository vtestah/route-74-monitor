from __future__ import annotations

import ast
import importlib
from collections import Counter
from collections.abc import Sequence
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    _assert_declares_all_detector()
    _assert_module_name_detector()
    failures = []
    checked_modules = 0
    for path in _public_api_paths():
        module_name = _module_name(path)
        checked_modules += 1
        try:
            module = importlib.import_module(module_name)
        except Exception as exc:
            failures.append(f"{module_name}: import failed: {type(exc).__name__}: {exc}")
            continue
        failures.extend(_all_contract_failures(module_name, getattr(module, "__all__", None), module))
    if failures:
        details = "\n".join(failures)
        raise AssertionError(f"public API exports are invalid:\n{details}")
    print(f"OK | public API smoke passed modules={checked_modules}")


def _public_api_paths() -> tuple[Path, ...]:
    return tuple(path for path in sorted(PACKAGE_ROOT.rglob("*.py")) if _declares_all(path))


def _declares_all(path: Path) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return _module_declares_all(tree)


def _module_declares_all(tree: ast.Module) -> bool:
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(_is_all_target(target) for target in node.targets):
            return True
        if isinstance(node, ast.AnnAssign) and _is_all_target(node.target):
            return True
    return False


def _is_all_target(node: ast.expr) -> bool:
    return isinstance(node, ast.Name) and node.id == "__all__"


def _module_name(path: Path) -> str:
    relative = path.relative_to(PACKAGE_ROOT.parent).with_suffix("")
    parts = relative.parts
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _all_contract_failures(module_name: str, names: object, module: object) -> list[str]:
    if isinstance(names, str) or not isinstance(names, Sequence):
        return [f"{module_name}: __all__ must be a sequence of strings"]
    failures = []
    values = list(names)
    non_strings = [repr(name) for name in values if not isinstance(name, str)]
    if non_strings:
        failures.append(f"{module_name}: __all__ contains non-string names: {', '.join(non_strings)}")
    string_names = [name for name in values if isinstance(name, str)]
    duplicates = sorted(name for name, count in Counter(string_names).items() if count > 1)
    if duplicates:
        failures.append(f"{module_name}: __all__ has duplicate names: {', '.join(duplicates)}")
    private_names = sorted(name for name in string_names if name.startswith("_"))
    if private_names:
        failures.append(f"{module_name}: __all__ exposes private names: {', '.join(private_names)}")
    missing = sorted(name for name in string_names if not hasattr(module, name))
    if missing:
        failures.append(f"{module_name}: __all__ exports missing attributes: {', '.join(missing)}")
    return failures


def _assert_declares_all_detector() -> None:
    annotated = ast.parse("__all__: tuple[str, ...] = ('Name',)")
    annotation_only = ast.parse("__all__: tuple[str, ...]")
    nested = ast.parse("if True:\n    __all__ = ('Name',)")
    _assert_equal(_module_declares_all(annotated), True)
    _assert_equal(_module_declares_all(annotation_only), True)
    _assert_equal(_module_declares_all(nested), False)


def _assert_module_name_detector() -> None:
    _assert_equal(_module_name(PACKAGE_ROOT / "storage" / "__init__.py"), "route74.storage")
    _assert_equal(_module_name(PACKAGE_ROOT / "storage" / "yandex.py"), "route74.storage.yandex")


def _assert_equal(actual: object, expected: object) -> None:
    if actual != expected:
        raise AssertionError(f"expected {expected!r}, got {actual!r}")


if __name__ == "__main__":
    main()

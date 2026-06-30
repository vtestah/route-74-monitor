from __future__ import annotations

import ast
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    _assert_package_init_detector()
    failures = []
    for path in _package_init_paths():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        relative_path = path.relative_to(PACKAGE_ROOT)
        failures.extend(
            f"{relative_path.as_posix()}:{line_number} {label}" for line_number, label in _package_init_violations(tree)
        )
    if failures:
        details = "\n".join(failures)
        raise AssertionError(f"package __init__ contract failed:\n{details}")
    print("OK | package init smoke passed")


def _package_init_paths() -> tuple[Path, ...]:
    return tuple(sorted(PACKAGE_ROOT.rglob("__init__.py")))


def _package_init_violations(tree: ast.Module) -> tuple[tuple[int, str], ...]:
    violations = []
    for index, statement in enumerate(tree.body):
        if _is_wildcard_import(statement):
            violations.append((statement.lineno, "package __init__ must not use wildcard imports"))
            continue
        if _is_allowed_init_statement(statement, is_first_statement=index == 0):
            continue
        violations.append(
            (
                getattr(statement, "lineno", 1),
                f"package __init__ must stay a thin export layer: {type(statement).__name__}",
            )
        )
    return tuple(violations)


def _is_allowed_init_statement(statement: ast.stmt, *, is_first_statement: bool) -> bool:
    if is_first_statement and _is_module_docstring(statement):
        return True
    if _is_future_annotations_import(statement):
        return True
    if isinstance(statement, ast.Import | ast.ImportFrom):
        return True
    return _is_all_assignment(statement)


def _is_wildcard_import(statement: ast.stmt) -> bool:
    return isinstance(statement, ast.ImportFrom) and any(alias.name == "*" for alias in statement.names)


def _is_module_docstring(statement: ast.stmt) -> bool:
    return (
        isinstance(statement, ast.Expr)
        and isinstance(statement.value, ast.Constant)
        and isinstance(statement.value.value, str)
    )


def _is_future_annotations_import(statement: ast.stmt) -> bool:
    return (
        isinstance(statement, ast.ImportFrom)
        and statement.module == "__future__"
        and any(alias.name == "annotations" for alias in statement.names)
    )


def _is_all_assignment(statement: ast.stmt) -> bool:
    if isinstance(statement, ast.Assign):
        return any(_is_all_target(target) for target in statement.targets) and _is_static_all_value(statement.value)
    if isinstance(statement, ast.AnnAssign):
        return _is_all_target(statement.target) and (statement.value is None or _is_static_all_value(statement.value))
    return False


def _is_all_target(node: ast.expr) -> bool:
    return isinstance(node, ast.Name) and node.id == "__all__"


def _is_static_all_value(node: ast.expr) -> bool:
    return isinstance(node, ast.List | ast.Tuple) and all(
        isinstance(item, ast.Constant) and isinstance(item.value, str) for item in node.elts
    )


def _assert_package_init_detector() -> None:
    good_tree = ast.parse(
        "\n".join(
            [
                '"""Package docs."""',
                "from __future__ import annotations",
                "from route74.storage.connection import connect",
                "__all__: tuple[str, ...] = ('connect',)",
            ]
        )
    )
    bad_tree = ast.parse(
        "\n".join(
            [
                '"""Package docs."""',
                "__all__ = build_exports()",
                "__all__ = [name for name in names]",
                "from route74.storage import *",
                "STATE = []",
                "def configure():",
                "    return None",
                "if True:",
                "    configure()",
                "configure()",
            ]
        )
    )
    _assert_equal(_package_init_violations(good_tree), ())
    _assert_equal(
        _labels(bad_tree),
        (
            "package __init__ must stay a thin export layer: Assign",
            "package __init__ must stay a thin export layer: Assign",
            "package __init__ must not use wildcard imports",
            "package __init__ must stay a thin export layer: Assign",
            "package __init__ must stay a thin export layer: FunctionDef",
            "package __init__ must stay a thin export layer: If",
            "package __init__ must stay a thin export layer: Expr",
        ),
    )


def _labels(tree: ast.Module) -> tuple[str, ...]:
    return tuple(label for _, label in _package_init_violations(tree))


def _assert_equal(actual: object, expected: object) -> None:
    if actual != expected:
        raise AssertionError(f"expected {expected!r}, got {actual!r}")


if __name__ == "__main__":
    main()

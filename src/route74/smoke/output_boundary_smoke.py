from __future__ import annotations

import ast
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
STD_STREAM_NAMES = frozenset({"stderr", "stdout", "__stderr__", "__stdout__"})


def main() -> None:
    _assert_output_boundary_detector()
    failures = []
    for path in _production_python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        relative_path = path.relative_to(PACKAGE_ROOT)
        failures.extend(
            f"{relative_path.as_posix()}:{line_number} {label}"
            for line_number, label in _output_boundary_violations(tree, relative_path)
        )
    if failures:
        details = "\n".join(failures)
        raise AssertionError(f"output boundary contract failed:\n{details}")
    print("OK | output boundary smoke passed")


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


def _output_boundary_violations(tree: ast.Module, relative_path: Path) -> tuple[tuple[int, str], ...]:
    violations = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if _is_print_call(node):
            violations.extend(_print_violations(node, relative_path))
        elif _is_std_stream_write_call(node):
            violations.extend(_std_stream_violations(node, relative_path))
    return tuple(violations)


def _print_violations(node: ast.Call, relative_path: Path) -> tuple[tuple[int, str], ...]:
    if _is_cli_module(relative_path):
        return ()
    if _is_bot_module(relative_path):
        if _keyword_is_true(node, "flush"):
            return ()
        return ((node.lineno, "bot diagnostics must call print(..., flush=True)"),)
    return ((node.lineno, "print() is only allowed in cli/ or flushed bot diagnostics"),)


def _std_stream_violations(node: ast.Call, relative_path: Path) -> tuple[tuple[int, str], ...]:
    if _is_cli_module(relative_path):
        return ()
    return ((node.lineno, "sys stdout/stderr writes are only allowed in cli/"),)


def _is_cli_module(path: Path) -> bool:
    return bool(path.parts) and path.parts[0] == "cli"


def _is_bot_module(path: Path) -> bool:
    return bool(path.parts) and path.parts[0] == "bot"


def _is_print_call(node: ast.Call) -> bool:
    return isinstance(node.func, ast.Name) and node.func.id == "print"


def _is_std_stream_write_call(node: ast.Call) -> bool:
    if not isinstance(node.func, ast.Attribute) or node.func.attr != "write":
        return False
    stream = node.func.value
    return (
        isinstance(stream, ast.Attribute)
        and stream.attr in STD_STREAM_NAMES
        and isinstance(stream.value, ast.Name)
        and stream.value.id == "sys"
    )


def _keyword_is_true(node: ast.Call, name: str) -> bool:
    for keyword in node.keywords:
        if keyword.arg == name:
            return isinstance(keyword.value, ast.Constant) and keyword.value.value is True
    return False


def _assert_output_boundary_detector() -> None:
    tree = ast.parse(
        "\n".join(
            [
                "import sys",
                "print('hello')",
                "print('hello', flush=True)",
                "sys.stderr.write('boom')",
            ]
        )
    )
    _assert_equal(_labels(tree, "cli/main.py"), ())
    _assert_equal(
        _labels(tree, "bot/app.py"),
        (
            "bot diagnostics must call print(..., flush=True)",
            "sys stdout/stderr writes are only allowed in cli/",
        ),
    )
    _assert_equal(
        _labels(tree, "services/commute.py"),
        (
            "print() is only allowed in cli/ or flushed bot diagnostics",
            "print() is only allowed in cli/ or flushed bot diagnostics",
            "sys stdout/stderr writes are only allowed in cli/",
        ),
    )


def _labels(tree: ast.Module, path: str) -> tuple[str, ...]:
    return tuple(label for _, label in _output_boundary_violations(tree, Path(path)))


def _assert_equal(actual: object, expected: object) -> None:
    if actual != expected:
        raise AssertionError(f"expected {expected!r}, got {actual!r}")


if __name__ == "__main__":
    main()

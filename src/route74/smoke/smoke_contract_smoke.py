from __future__ import annotations

import ast
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parent


def main() -> None:
    _assert_contract_examples()
    _assert_package_runner_examples()
    failures = []
    for path, expected_imported_main_module in _smoke_entrypoints():
        if not path.exists():
            failures.append(f"{_display_path(path)}: missing smoke package __main__.py")
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        problems = _smoke_contract_problems(
            tree,
            expected_imported_main_module=expected_imported_main_module,
        )
        if problems:
            failures.append(f"{_display_path(path)}: {', '.join(problems)}")
    for package_init in sorted(PACKAGE_ROOT.rglob("smoke/__init__.py")):
        package_dir = package_init.parent
        problems = _smoke_package_runner_problems(package_dir)
        if problems:
            failures.append(f"{_display_path(package_dir)}: {', '.join(problems)}")
    if failures:
        details = "\n".join(failures)
        raise AssertionError(f"smoke entrypoints must be executable:\n{details}")
    print("OK | smoke contract smoke passed")


def _smoke_entrypoints() -> tuple[tuple[Path, str | None], ...]:
    entrypoints: list[tuple[Path, str | None]] = [(path, None) for path in sorted(PACKAGE_ROOT.glob("*_smoke.py"))]
    for package_init in sorted(PACKAGE_ROOT.rglob("smoke/__init__.py")):
        package_dir = package_init.parent
        entrypoints.append((package_dir / "__main__.py", _module_name(package_dir / "runner.py")))
    return tuple(entrypoints)


def _smoke_contract_problems(
    tree: ast.Module,
    *,
    expected_imported_main_module: str | None,
) -> tuple[str, ...]:
    problems = []
    main_function = _main_function(tree)
    if main_function is None:
        import_problem = _imported_main_problem(tree, expected_imported_main_module)
        if import_problem is not None:
            problems.append(import_problem)
    if main_function is not None:
        signature_problem = _main_signature_problem(main_function)
        if signature_problem is not None:
            problems.append(signature_problem)
    if not _has_main_guard_call(tree):
        problems.append('missing if __name__ == "__main__": main()')
    return tuple(problems)


def _smoke_package_runner_problems(package_dir: Path) -> tuple[str, ...]:
    runner_path = package_dir / "runner.py"
    if not runner_path.exists():
        return ("missing runner.py",)

    runner_tree = ast.parse(runner_path.read_text(encoding="utf-8"), filename=str(runner_path))
    problems = list(_runner_main_problems(runner_tree))
    called_names = _main_called_names(runner_tree)
    for module_path, run_functions in _smoke_package_run_functions(package_dir).items():
        module_name = _module_name(module_path)
        imported_names = _imported_names_from_module(runner_tree, module_name)
        for function_name in run_functions:
            if function_name not in imported_names:
                problems.append(f"runner.py does not import {module_name}.{function_name}")
            if function_name not in called_names:
                problems.append(f"runner.py does not call {function_name}()")
    return tuple(problems)


def _runner_main_problems(runner_tree: ast.Module) -> tuple[str, ...]:
    main_function = _main_function(runner_tree)
    if main_function is None:
        return ("runner.py missing main()",)
    signature_problem = _main_signature_problem(main_function)
    if signature_problem is None:
        return ()
    return (f"runner.py {signature_problem}",)


def _smoke_package_run_functions(package_dir: Path) -> dict[Path, tuple[str, ...]]:
    run_functions = {}
    skipped = {"__init__.py", "__main__.py", "runner.py"}
    for module_path in sorted(package_dir.glob("*.py")):
        if module_path.name in skipped:
            continue
        tree = ast.parse(module_path.read_text(encoding="utf-8"), filename=str(module_path))
        functions = _run_function_names(tree)
        if functions:
            run_functions[module_path] = functions
    return run_functions


def _run_function_names(tree: ast.Module) -> tuple[str, ...]:
    return tuple(
        node.name
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name.startswith("run_") and node.name.endswith("_smoke")
    )


def _imported_names_from_module(tree: ast.Module, module_name: str) -> frozenset[str]:
    names = set()
    for node in tree.body:
        if not isinstance(node, ast.ImportFrom) or node.module != module_name:
            continue
        for alias in node.names:
            names.add(alias.asname or alias.name)
    return frozenset(names)


def _main_called_names(tree: ast.Module) -> frozenset[str]:
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "main":
            return frozenset(
                child.func.id
                for child in ast.walk(node)
                if isinstance(child, ast.Call) and isinstance(child.func, ast.Name)
            )
    return frozenset()


def _main_function(tree: ast.Module) -> ast.FunctionDef | None:
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "main":
            return node
    return None


def _main_signature_problem(function: ast.FunctionDef) -> str | None:
    required_parameters = _required_parameter_names(function.args)
    if not required_parameters:
        return None
    parameters = ", ".join(required_parameters)
    return f"main() must be callable without arguments; required parameters: {parameters}"


def _required_parameter_names(args: ast.arguments) -> tuple[str, ...]:
    positional_args = [*args.posonlyargs, *args.args]
    required_positional_count = len(positional_args) - len(args.defaults)
    required_names = [arg.arg for arg in positional_args[:required_positional_count]]
    required_names.extend(arg.arg for arg, default in zip(args.kwonlyargs, args.kw_defaults) if default is None)
    return tuple(required_names)


def _imported_main_problem(tree: ast.Module, expected_module: str | None) -> str | None:
    if expected_module is None:
        return "missing main()"
    imported_modules = _imported_main_modules(tree)
    if not imported_modules:
        return "missing main()"
    if expected_module in imported_modules:
        return None
    modules = ", ".join(sorted(imported_modules))
    return f"imported main() must come from {expected_module}, got {modules}"


def _imported_main_modules(tree: ast.Module) -> frozenset[str]:
    modules = set()
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == "main" and (alias.asname or alias.name) == "main":
                    modules.add(node.module or "")
    return frozenset(modules)


def _has_main_guard_call(tree: ast.Module) -> bool:
    for node in tree.body:
        if isinstance(node, ast.If) and _is_main_guard(node.test) and _body_calls_main(node.body):
            return True
    return False


def _is_main_guard(test: ast.expr) -> bool:
    return (
        isinstance(test, ast.Compare)
        and isinstance(test.left, ast.Name)
        and test.left.id == "__name__"
        and len(test.ops) == 1
        and isinstance(test.ops[0], ast.Eq)
        and len(test.comparators) == 1
        and isinstance(test.comparators[0], ast.Constant)
        and test.comparators[0].value == "__main__"
    )


def _body_calls_main(nodes: list[ast.stmt]) -> bool:
    return any(_statement_calls_main(node) for node in nodes)


def _statement_calls_main(node: ast.stmt) -> bool:
    if isinstance(node, ast.Expr):
        return _expression_calls_main(node.value)
    if isinstance(node, ast.Raise) and node.exc is not None:
        return _contains_main_call(node.exc)
    return False


def _expression_calls_main(node: ast.AST) -> bool:
    if _is_main_call(node):
        return True
    if not isinstance(node, ast.Call):
        return False
    return any(_contains_evaluated_main_call(arg) for arg in node.args) or any(
        _contains_evaluated_main_call(keyword.value) for keyword in node.keywords
    )


def _contains_evaluated_main_call(node: ast.AST) -> bool:
    if isinstance(node, ast.Lambda):
        return False
    if _is_main_call(node):
        return True
    if isinstance(node, ast.Call):
        return _expression_calls_main(node)
    return any(_contains_evaluated_main_call(child) for child in ast.iter_child_nodes(node))


def _contains_main_call(node: ast.AST) -> bool:
    return _is_main_call(node) or any(_contains_main_call(child) for child in ast.iter_child_nodes(node))


def _is_main_call(node: ast.AST) -> bool:
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "main"


def _display_path(path: Path) -> str:
    return path.relative_to(PACKAGE_ROOT).as_posix()


def _module_name(path: Path) -> str:
    return ".".join(path.relative_to(PACKAGE_ROOT.parent).with_suffix("").parts)


def _assert_contract_examples() -> None:
    _assert_problems(
        'def main() -> None:\n    pass\n\nif __name__ == "__main__":\n    main()\n',
        expected_imported_main_module=None,
        expected=(),
    )
    _assert_problems(
        'from route74.example.smoke.runner import main\n\nif __name__ == "__main__":\n    main()\n',
        expected_imported_main_module="route74.example.smoke.runner",
        expected=(),
    )
    _assert_problems(
        'from route74.other.smoke.runner import main\n\nif __name__ == "__main__":\n    main()\n',
        expected_imported_main_module="route74.example.smoke.runner",
        expected=("imported main() must come from route74.example.smoke.runner, got route74.other.smoke.runner",),
    )
    _assert_problems(
        'import sys\n\ndef main() -> None:\n    pass\n\nif __name__ == "__main__":\n    sys.exit(main())\n',
        expected_imported_main_module=None,
        expected=(),
    )
    _assert_problems(
        ('def main(argv: list[str] | None = None) -> None:\n    pass\n\nif __name__ == "__main__":\n    main()\n'),
        expected_imported_main_module=None,
        expected=(),
    )
    _assert_problems(
        'from route74.example.runner import main\n\nif __name__ == "__main__":\n    main()\n',
        expected_imported_main_module=None,
        expected=("missing main()",),
    )
    _assert_problems(
        'from route74.example.runner import main as run\n\nif __name__ == "__main__":\n    run()\n',
        expected_imported_main_module="route74.example.smoke.runner",
        expected=("missing main()", 'missing if __name__ == "__main__": main()'),
    )
    _assert_problems(
        'from route74.example.smoke.runner import helper as main\n\nif __name__ == "__main__":\n    main()\n',
        expected_imported_main_module="route74.example.smoke.runner",
        expected=("missing main()",),
    )
    _assert_problems(
        'def main(argv: list[str]) -> None:\n    pass\n\nif __name__ == "__main__":\n    main()\n',
        expected_imported_main_module=None,
        expected=("main() must be callable without arguments; required parameters: argv",),
    )
    _assert_problems(
        'def main(*, profile: str) -> None:\n    pass\n\nif __name__ == "__main__":\n    main()\n',
        expected_imported_main_module=None,
        expected=("main() must be callable without arguments; required parameters: profile",),
    )
    _assert_problems(
        'def main() -> None:\n    pass\n\nif __name__ == "__main__":\n    lambda: main()\n',
        expected_imported_main_module=None,
        expected=('missing if __name__ == "__main__": main()',),
    )
    _assert_problems(
        'import sys\n\ndef main() -> None:\n    pass\n\nif __name__ == "__main__":\n    sys.exit(lambda: main())\n',
        expected_imported_main_module=None,
        expected=('missing if __name__ == "__main__": main()',),
    )
    _assert_problems(
        "def main() -> None:\n    pass\n",
        expected_imported_main_module=None,
        expected=('missing if __name__ == "__main__": main()',),
    )


def _assert_package_runner_examples() -> None:
    case_tree = ast.parse(
        "\n".join(
            [
                "def run_case_smoke() -> None:",
                "    pass",
                "def run_helper() -> None:",
                "    pass",
                "def helper() -> None:",
                "    pass",
            ]
        )
    )
    runner_tree = ast.parse(
        "\n".join(
            [
                "from route74.example.smoke.case import run_case_smoke",
                "",
                "def main() -> None:",
                "    run_case_smoke()",
            ]
        )
    )
    missing_call_tree = ast.parse(
        "\n".join(
            [
                "from route74.example.smoke.case import run_case_smoke",
                "",
                "def main() -> None:",
                "    pass",
            ]
        )
    )
    _assert_equal(_run_function_names(case_tree), ("run_case_smoke",))
    _assert_equal(
        _imported_names_from_module(runner_tree, "route74.example.smoke.case"),
        frozenset({"run_case_smoke"}),
    )
    _assert_equal(_main_called_names(runner_tree), frozenset({"run_case_smoke"}))
    _assert_equal(_main_called_names(missing_call_tree), frozenset())
    _assert_equal(_runner_main_problems(runner_tree), ())
    _assert_equal(
        _runner_main_problems(ast.parse("def main(case: str) -> None:\n    pass\n")),
        ("runner.py main() must be callable without arguments; required parameters: case",),
    )
    _assert_equal(_runner_main_problems(ast.parse("pass\n")), ("runner.py missing main()",))


def _assert_problems(
    source: str,
    *,
    expected_imported_main_module: str | None,
    expected: tuple[str, ...],
) -> None:
    problems = _smoke_contract_problems(
        ast.parse(source),
        expected_imported_main_module=expected_imported_main_module,
    )
    if problems != expected:
        raise AssertionError(f"expected {expected!r}, got {problems!r}")


def _assert_equal(actual: object, expected: object) -> None:
    if actual != expected:
        raise AssertionError(f"expected {expected!r}, got {actual!r}")


if __name__ == "__main__":
    main()

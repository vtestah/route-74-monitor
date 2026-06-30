"""Pytest bridge over the existing smoke harness.

Rather than rewriting the ~90 ``route74.smoke.*_smoke`` modules, this adapter
discovers them and runs each module's ``main()`` as an individual pytest case,
giving pytest-level reporting while the smoke modules remain the source of
truth (and keep running unchanged under ``./bin/check`` and on the server).
"""

from __future__ import annotations

import importlib
import io
import pkgutil
from collections.abc import Iterator
from contextlib import redirect_stdout

import pytest

import route74.smoke as smoke_package

# Heavy or environment-dependent smokes that are validated by the dedicated
# bin/ scripts (web/yandex) rather than the in-process pytest layer.
_EXCLUDED_SMOKE_MODULES = frozenset(
    {
        "route74.smoke.web_runtime_smoke",
        "route74.smoke.web_config_smoke",
    }
)


def _discover_smoke_modules() -> tuple[str, ...]:
    modules: list[str] = []
    for module_info in pkgutil.iter_modules(smoke_package.__path__):
        if not module_info.name.endswith("_smoke"):
            continue
        qualified = f"{smoke_package.__name__}.{module_info.name}"
        if qualified in _EXCLUDED_SMOKE_MODULES:
            continue
        modules.append(qualified)
    return tuple(sorted(modules))


def _smoke_module_ids() -> Iterator[str]:
    # Shorten ids to the bare module name for readable pytest output.
    for module_name in _discover_smoke_modules():
        yield module_name.rsplit(".", 1)[-1]


@pytest.mark.parametrize("module_name", _discover_smoke_modules(), ids=list(_smoke_module_ids()))
def test_smoke_module_passes(module_name: str) -> None:
    module = importlib.import_module(module_name)
    main = getattr(module, "main", None)
    assert callable(main), f"{module_name} must expose a callable main()"
    # Smoke modules signal success by returning without raising; swallow their
    # stdout so pytest output stays focused on failures.
    with redirect_stdout(io.StringIO()):
        main()


def test_smoke_discovery_is_non_empty() -> None:
    assert _discover_smoke_modules(), "expected to discover route74 smoke modules"

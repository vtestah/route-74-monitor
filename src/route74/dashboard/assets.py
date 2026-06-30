"""Dashboard UI HTML and favicon assets, loaded from static/ files."""

from __future__ import annotations

from pathlib import Path

_STATIC_DIR = Path(__file__).resolve().parent / "static"

FAVICON_SVG: str = (_STATIC_DIR / "favicon.svg").read_text(encoding="utf-8")
DASHBOARD_HTML: str = (_STATIC_DIR / "index.html").read_text(encoding="utf-8")

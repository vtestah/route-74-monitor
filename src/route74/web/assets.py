"""Main web UI HTML asset, loaded from static/index.html."""

from __future__ import annotations

from pathlib import Path

_STATIC_DIR = Path(__file__).resolve().parent / "static"

WEB_HTML: str = (_STATIC_DIR / "index.html").read_text(encoding="utf-8")

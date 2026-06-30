from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from datetime import datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any


PACKAGE_NAME = "route74-monitor"
BUILD_INFO_FILENAME = ".route74-build.json"
MIN_COMMIT_LABEL_LENGTH = 7
MAX_COMMIT_LENGTH = 64
HEX_DIGITS = frozenset("0123456789abcdefABCDEF")
MAX_BUILD_TEXT_LENGTH = 120
GIT_COMMAND_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True)
class BuildInfo:
    package_version: str
    commit: str | None = None
    short_commit: str | None = None
    branch: str | None = None
    dirty: bool | None = None
    built_at: str | None = None
    deployed_at: str | None = None
    source: str = "runtime"

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "package_version",
            _optional_text(self.package_version) or _package_version(),
        )
        object.__setattr__(self, "commit", _optional_commit(self.commit))
        object.__setattr__(self, "short_commit", _short_commit(self.short_commit))
        object.__setattr__(self, "branch", _optional_text(self.branch))
        object.__setattr__(self, "dirty", _optional_bool(self.dirty))
        object.__setattr__(self, "built_at", _optional_timestamp(self.built_at))
        object.__setattr__(self, "deployed_at", _optional_timestamp(self.deployed_at))
        object.__setattr__(self, "source", _optional_text(self.source) or "runtime")

    @property
    def display_commit(self) -> str | None:
        return _short_commit(self.short_commit) or _short_commit(self.commit)

    @property
    def label(self) -> str:
        return self.display_commit or _optional_text(self.package_version) or "unknown"

    def to_jsonable(self) -> dict[str, object]:
        return {
            "package_version": self.package_version,
            "commit": self.commit,
            "short_commit": self.short_commit,
            "branch": self.branch,
            "dirty": self.dirty,
            "built_at": self.built_at,
            "deployed_at": self.deployed_at,
            "source": self.source,
        }


def load_build_info(path: Path | None = None) -> BuildInfo:
    for candidate in _candidate_paths(path):
        info = _load_build_file(candidate)
        if info is not None:
            return info
    return _runtime_build_info()


def format_build_status(info: BuildInfo) -> str:
    if info.dirty is True:
        return "dirty"
    if info.dirty is False:
        return "clean"
    return "unknown"


def _candidate_paths(path: Path | None) -> tuple[Path, ...]:
    if path is not None:
        return (Path(path),)
    repo_root = Path(__file__).resolve().parents[2]
    return _unique_paths((repo_root / BUILD_INFO_FILENAME, Path.cwd() / BUILD_INFO_FILENAME))


def _unique_paths(paths: tuple[Path, ...]) -> tuple[Path, ...]:
    result: list[Path] = []
    seen: set[Path] = set()
    for candidate in paths:
        marker = candidate.resolve()
        if marker in seen:
            continue
        seen.add(marker)
        result.append(candidate)
    return tuple(result)


def _load_build_file(path: Path) -> BuildInfo | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    commit = _optional_commit(payload.get("commit"))
    return BuildInfo(
        package_version=_optional_text(payload.get("package_version")) or _package_version(),
        commit=commit,
        short_commit=_short_commit(payload.get("short_commit")) or _short_commit(commit),
        branch=_optional_text(payload.get("branch")),
        dirty=_optional_bool(payload.get("dirty")),
        built_at=_optional_timestamp(payload.get("built_at")),
        deployed_at=_optional_timestamp(payload.get("deployed_at")),
        source=_optional_text(payload.get("source")) or "build-file",
    )


def _runtime_build_info() -> BuildInfo:
    commit = _git_value("rev-parse", "HEAD")
    return BuildInfo(
        package_version=_package_version(),
        commit=commit,
        short_commit=_git_value("rev-parse", "--short", "HEAD"),
        branch=_git_value("rev-parse", "--abbrev-ref", "HEAD"),
        dirty=_git_dirty() if commit else None,
        source="git" if commit else "package",
    )


def _package_version() -> str:
    try:
        return version(PACKAGE_NAME)
    except PackageNotFoundError:
        return "0.1.0"


def _git_value(*args: str) -> str | None:
    try:
        result = subprocess.run(
            ("git", *args),
            cwd=Path(__file__).resolve().parents[2],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=GIT_COMMAND_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    value = result.stdout.strip()
    return value or None


def _git_dirty() -> bool:
    status = _git_value("status", "--porcelain")
    return bool(status)


def _optional_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())
    if len(text) > MAX_BUILD_TEXT_LENGTH:
        return None
    return text or None


def _optional_commit(value: Any) -> str | None:
    text = _optional_text(value)
    if text is None:
        return None
    if not MIN_COMMIT_LABEL_LENGTH <= len(text) <= MAX_COMMIT_LENGTH:
        return None
    if not all(character in HEX_DIGITS for character in text):
        return None
    return text.lower()


def _optional_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        if value.lower() in {"1", "true", "yes", "dirty"}:
            return True
        if value.lower() in {"0", "false", "no", "clean"}:
            return False
    return None


def _optional_timestamp(value: Any) -> str | None:
    text = _optional_text(value)
    if text is None:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.isoformat()


def _short_commit(commit: Any) -> str | None:
    text = _optional_commit(commit)
    if text is None:
        return None
    return text[:MIN_COMMIT_LABEL_LENGTH]

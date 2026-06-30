from __future__ import annotations

import re


ANSI_ESCAPE_PATTERN = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
CONTROL_CHARACTER_PATTERN = re.compile(r"[\x00-\x1f\x7f]")
ABSOLUTE_PATH_PATTERN = re.compile(r"(?<![\w.-])/[^\s'\"<>]+")
SECRET_TOKEN_PATTERN = re.compile(r"\b\d{5,}:[A-Za-z0-9_-]{20,}\b")
SENSITIVE_KEY = (
    r"(?:(?:[a-z0-9]+[_-])*)"
    r"(?:access[_-]?token|refresh[_-]?token|id[_-]?token|csrf[_-]?token|"
    r"session[_-]?id|api[_-]?key|apikey|token|secret|password|passwd)"
)
SENSITIVE_DIAGNOSTIC_PATTERNS = (
    re.compile(rf"(?i)\b({SENSITIVE_KEY}\s*[:=]\s*)[^&\s\"'<>]+"),
    re.compile(rf"(?i)([\"']{SENSITIVE_KEY}[\"']\s*:\s*[\"'])[^\"']+([\"'])"),
    re.compile(r"(?i)\b(authorization\s*[:=]\s*)(?:bearer\s+)?[A-Za-z0-9._~+/=-]+"),
    re.compile(r"(?i)\b(cookie\s*[:=]\s*)[^\s\"'<>]+"),
    re.compile(r"(?i)(https?://)[^/\s:@]+:[^/\s@]+(@)"),
)


def sanitize_diagnostic_text(value: object, *, fallback: str = "-", limit: int = 120) -> str:
    return _sanitize_text(value, fallback=fallback, limit=limit, redact_paths=True)


def sanitize_command_text(value: object, *, fallback: str = "-", limit: int = 160) -> str:
    return _sanitize_text(value, fallback=fallback, limit=limit, redact_paths=False)


def _sanitize_text(
    value: object,
    *,
    fallback: str,
    limit: int,
    redact_paths: bool,
) -> str:
    if value is None:
        normalized = ""
    else:
        sanitized = ANSI_ESCAPE_PATTERN.sub("", str(value))
        sanitized = CONTROL_CHARACTER_PATTERN.sub(" ", sanitized)
        sanitized = SECRET_TOKEN_PATTERN.sub("<redacted>", sanitized)
        for pattern in SENSITIVE_DIAGNOSTIC_PATTERNS:
            sanitized = pattern.sub(_redacted_diagnostic_match, sanitized)
        if redact_paths:
            sanitized = ABSOLUTE_PATH_PATTERN.sub("<path>", sanitized)
        normalized = " ".join(sanitized.split())
    return normalized[:limit] if normalized else fallback


def _redacted_diagnostic_match(match: re.Match[str]) -> str:
    if len(match.groups()) == 2:
        return f"{match.group(1)}<redacted>{match.group(2)}"
    return f"{match.group(1)}<redacted>"

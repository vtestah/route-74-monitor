from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from route74.env import DEFAULT_ENV_PATH, ENV_FILE, env_value, load_env_file

ENV_PUSHOVER_APP_TOKEN = "PUSHOVER_APP_TOKEN"
ENV_PUSHOVER_USER_KEY = "PUSHOVER_USER_KEY"


@dataclass(frozen=True)
class PushoverConfig:
    app_token: str
    user_key: str

    def __post_init__(self) -> None:
        for field_name in ("app_token", "user_key"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"pushover {field_name} must be non-empty text")


def load_pushover_config(env_file: Path | None = None) -> PushoverConfig | None:
    target_env_file = env_file or Path(env_value(ENV_FILE, {}, str(DEFAULT_ENV_PATH)) or str(DEFAULT_ENV_PATH))
    file_env = load_env_file(target_env_file)
    app_token = _optional_env_value(ENV_PUSHOVER_APP_TOKEN, file_env)
    user_key = _optional_env_value(ENV_PUSHOVER_USER_KEY, file_env)
    if not app_token or not user_key:
        return None
    return PushoverConfig(app_token=app_token, user_key=user_key)


def _optional_env_value(name: str, file_env: dict[str, str]) -> str | None:
    value = env_value(name, file_env, None)
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None

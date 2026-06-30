from __future__ import annotations

import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from route74.env import ENV_FILE
from route74.notifications import ENV_PUSHOVER_APP_TOKEN, ENV_PUSHOVER_USER_KEY, load_pushover_config
from route74.web.config import ENV_WEB_ALLOW_PUBLIC, ENV_WEB_HOST, ENV_WEB_PORT, parse_web_config


README_ENV_BLOCK_MARKER = "Минимальный `.env` для web-приложения:"


def main() -> None:
    _assert_web_config_defaults()
    _assert_web_config_reads_env_file()
    _assert_public_host_requires_flag()
    _assert_pushover_config_is_optional()
    print("OK | web config smoke passed")


def _assert_web_config_defaults() -> None:
    with patch.dict(os.environ, {ENV_FILE: "/dev/null"}, clear=True):
        config = parse_web_config([])
    _assert_equal(config.host, "127.0.0.1")
    _assert_equal(config.port, 8074)
    _assert_equal(config.watch_state_path, Path("data/web_watches.json"))


def _assert_web_config_reads_env_file() -> None:
    with TemporaryDirectory() as temp_dir:
        env_path = Path(temp_dir) / ".env"
        env_path.write_text(
            "\n".join(
                [
                    "ROUTE74_WEB_HOST=127.0.0.1",
                    "ROUTE74_WEB_PORT=9090",
                    "ROUTE74_WEB_WATCH_STATE_PATH=data/custom-web-watches.json",
                    f"{ENV_PUSHOVER_APP_TOKEN}=token-123",
                    f"{ENV_PUSHOVER_USER_KEY}=user-456",
                ]
            ),
            encoding="utf-8",
        )
        with patch.dict(os.environ, {ENV_FILE: str(env_path)}, clear=True):
            config = parse_web_config([])
            push = load_pushover_config(env_path)
    _assert_equal(config.port, 9090)
    _assert_equal(config.watch_state_path, Path("data/custom-web-watches.json"))
    if push is None:
        raise AssertionError("expected pushover config")
    _assert_equal(push.app_token, "token-123")
    _assert_equal(push.user_key, "user-456")


def _assert_public_host_requires_flag() -> None:
    with patch.dict(os.environ, {ENV_FILE: "/dev/null", ENV_WEB_HOST: "0.0.0.0"}, clear=True):
        try:
            parse_web_config([])
        except SystemExit as exc:
            _assert_equal(
                str(exc),
                f"{ENV_WEB_ALLOW_PUBLIC}=1 is required for non-loopback host '0.0.0.0'.",
            )
        else:
            raise AssertionError("expected non-loopback host to require allow flag")

    with patch.dict(
        os.environ,
        {
            ENV_FILE: "/dev/null",
            ENV_WEB_HOST: "0.0.0.0",
            ENV_WEB_ALLOW_PUBLIC: "1",
            ENV_WEB_PORT: "9000",
        },
        clear=True,
    ):
        config = parse_web_config([])
    _assert_equal(config.host, "0.0.0.0")
    _assert_equal(config.port, 9000)


def _assert_pushover_config_is_optional() -> None:
    with TemporaryDirectory() as temp_dir:
        env_path = Path(temp_dir) / ".env"
        env_path.write_text(f"{ENV_PUSHOVER_APP_TOKEN}=token-only\n", encoding="utf-8")
        with patch.dict(os.environ, {ENV_FILE: str(env_path)}, clear=True):
            _assert_equal(load_pushover_config(env_path), None)


def _assert_equal(actual: object, expected: object) -> None:
    if actual != expected:
        raise AssertionError(f"expected {expected!r}, got {actual!r}")


if __name__ == "__main__":
    main()

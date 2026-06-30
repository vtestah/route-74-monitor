from __future__ import annotations

from route74.web.config import parse_web_config


def main() -> None:
    config = parse_web_config()
    import uvicorn

    from route74.web.app import create_app

    uvicorn.run(
        create_app(
            config.db_path,
            watch_state_path=config.watch_state_path,
            env_file=config.env_file,
        ),
        host=config.host,
        port=config.port,
        log_level="info",
    )


if __name__ == "__main__":
    main()

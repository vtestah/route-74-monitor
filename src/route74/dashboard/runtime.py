from __future__ import annotations

from route74.dashboard.config import parse_dashboard_config


def main() -> None:
    config = parse_dashboard_config()
    from route74.dashboard.app import create_app
    import uvicorn

    uvicorn.run(create_app(config.db_path), host=config.host, port=config.port, log_level="info")


if __name__ == "__main__":
    main()

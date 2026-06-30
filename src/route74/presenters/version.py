from __future__ import annotations

from route74.build_info import BuildInfo, format_build_status


def format_version_message(info: BuildInfo) -> str:
    lines = [
        "🧩 Версия 74",
        f"Пакет: {info.package_version}",
        f"Коммит: {info.display_commit or '-'}",
        f"Ветка: {info.branch or '-'}",
        f"Состояние: {format_build_status(info)}",
        f"Источник: {info.source}",
    ]
    if info.deployed_at:
        lines.append(f"Деплой: {info.deployed_at}")
    return "\n".join(lines)

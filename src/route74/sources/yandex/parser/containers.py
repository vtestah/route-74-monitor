from __future__ import annotations

from typing import Any


def find_vehicles(payload: dict[str, Any]) -> list[Any] | None:
    data = payload.get("data")
    if isinstance(data, dict) and isinstance(data.get("vehicles"), list):
        return data["vehicles"]
    if isinstance(payload.get("vehicles"), list):
        return payload["vehicles"]
    return _find_first_key(payload, "vehicles")


def _find_first_key(value: Any, key: str) -> list[Any] | None:
    if isinstance(value, dict):
        for item_key, item_value in value.items():
            if item_key == key and isinstance(item_value, list):
                return item_value
        for item_value in value.values():
            found = _find_first_key(item_value, key)
            if found is not None:
                return found
    if isinstance(value, list):
        for item in value:
            found = _find_first_key(item, key)
            if found is not None:
                return found
    return None

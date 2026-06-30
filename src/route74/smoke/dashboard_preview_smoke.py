from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import warnings

warnings.filterwarnings("ignore", message="Using `httpx` with `starlette.testclient` is deprecated.*")

from fastapi.testclient import TestClient

from route74.dashboard import create_app
from route74.dashboard.preview import DashboardPreviewRecord
from route74.smoke.dashboard_smoke import _assert_contains, _assert_equal, _seed
from route74.domain.profiles import MORNING
from route74.models import now_local


PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR"
    b"\x00\x00\x00\x01"
    b"\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00"
    b"\x1f\x15\xc4\x89"
    b"\x00\x00\x00\x0cIDATx\x9cc``\x00\x00\x00\x02\x00\x01"
    b"\xe2!\xbc3"
    b"\x00\x00\x00\x00IEND\xaeB`\x82"
)


def main() -> None:
    with TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "route74.sqlite"
        watch_state_path = Path(temp_dir) / "watch-state.json"
        preview_cache_path = Path(temp_dir) / "preview-cache"
        _seed(db_path)

        client = TestClient(
            create_app(
                db_path,
                watch_state_path=watch_state_path,
                preview_cache_path=preview_cache_path,
                preview_capture_fn=_fake_capture,
            )
        )

        page = client.get("/")
        _assert_equal(page.status_code, 200)
        _assert_contains(page.text, "Обновить preview")
        _assert_contains(page.text, "Preview маршрута")
        _assert_contains(page.text, "data-profile-preview")

        summary_before = client.get("/api/summary")
        _assert_equal(summary_before.status_code, 200)
        morning_before = _profile_by_key(summary_before.json()["operator_profiles"], MORNING.key)
        _assert_equal(morning_before["preview"]["status"], "no_confirmation")
        _assert_equal(morning_before["preview"]["fallback_text"], "нет подтверждения")

        image_before = client.get("/api/preview/morning/image")
        _assert_equal(image_before.status_code, 404)
        preview_before = client.get("/api/preview/morning")
        _assert_equal(preview_before.status_code, 200)
        _assert_equal(preview_before.json()["preview"]["status"], "no_confirmation")

        refresh = client.post("/api/preview/morning/refresh")
        _assert_equal(refresh.status_code, 200)
        preview = refresh.json()["preview"]
        _assert_equal(preview["status"], "ready")
        _assert_contains(preview["captured_at_label"], ":")
        _assert_contains(preview["freshness_label"], "свеж")

        image_after = client.get("/api/preview/morning/image")
        _assert_equal(image_after.status_code, 200)
        _assert_contains(image_after.headers["content-type"], "image/png")
        preview_after = client.get("/api/preview/morning")
        _assert_equal(preview_after.status_code, 200)
        _assert_equal(preview_after.json()["preview"]["status"], "ready")

        summary_after = client.get("/api/summary")
        _assert_equal(summary_after.status_code, 200)
        morning_after = _profile_by_key(summary_after.json()["operator_profiles"], MORNING.key)
        _assert_equal(morning_after["preview"]["status"], "ready")
        _assert_contains(morning_after["preview"]["freshness_label"], "свеж")

    with TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "route74.sqlite"
        preview_cache_path = Path(temp_dir) / "preview-cache"
        _seed(db_path)
        client = TestClient(
            create_app(
                db_path,
                preview_cache_path=preview_cache_path,
                preview_capture_fn=_failing_capture,
            )
        )
        refresh = client.post("/api/preview/morning/refresh")
        _assert_equal(refresh.status_code, 200)
        payload = refresh.json()["preview"]
        _assert_equal(payload["status"], "no_confirmation")
        _assert_equal(payload["fallback_text"], "нет подтверждения")
        _assert_contains(payload["reason"], "preview capture failed")

    print("OK | dashboard preview smoke passed")


def _fake_capture(profile: object, image_path: Path) -> DashboardPreviewRecord:
    image_path.parent.mkdir(parents=True, exist_ok=True)
    image_path.write_bytes(PNG_BYTES)
    profile_key = getattr(profile, "key")
    return DashboardPreviewRecord(
        profile_key=profile_key,
        captured_at=now_local(),
        image_path=image_path,
        source_url=f"https://example.invalid/{profile_key}",
    )


def _failing_capture(_profile: object, _image_path: Path) -> DashboardPreviewRecord:
    raise RuntimeError("preview capture failed")


def _profile_by_key(profiles: list[dict[str, object]], profile_key: str) -> dict[str, object]:
    for profile in profiles:
        if profile.get("profile_key") == profile_key:
            return profile
    raise AssertionError(f"missing profile: {profile_key}")


if __name__ == "__main__":
    main()

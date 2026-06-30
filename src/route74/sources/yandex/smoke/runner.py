from __future__ import annotations

from datetime import datetime

from route74.models import NOVOSIBIRSK_TZ
from route74.sources.yandex.smoke.browser_rate_limit_cases import (
    run_browser_rate_limit_smoke,
)
from route74.sources.yandex.smoke.capture_cases import run_browser_capture_smoke
from route74.sources.yandex.smoke.coordinate_fallback_cases import (
    run_live_eta_evidence_guard_smoke,
    run_raw_vehicle_invalid_coordinate_smoke,
    run_vehicle_prediction_coordinate_fallback_smoke,
    run_vehicle_prediction_invalid_coordinate_smoke,
    run_vehicle_prediction_source_coordinate_fallback_smoke,
)
from route74.sources.yandex.smoke.diagnostics import run_dump_smoke, run_live_probe
from route74.sources.yandex.smoke.direction_cases import run_direction_smoke
from route74.sources.yandex.smoke.line_cases import run_line_smoke
from route74.sources.yandex.smoke.parser_cases import (
    run_vehicle_parser_smoke,
    run_vehicle_prediction_smoke,
)
from route74.sources.yandex.smoke.route_traffic_cases import run_route_traffic_smoke
from route74.sources.yandex.smoke.source_cases import (
    run_browser_cooldown_smoke,
    run_stop_info_fallback_smoke,
    run_vehicle_prediction_source_smoke,
)
from route74.sources.yandex.smoke.source_fallback_cases import (
    run_auto_http_coordinates_continue_to_vehicle_prediction_smoke,
    run_stop_info_fallback_wins_http_schedule_smoke,
    run_vehicle_prediction_no_target_fallback_smoke,
)
from route74.sources.yandex.smoke.stop_info_cases import (
    run_stop_info_midnight_text_smoke,
    run_stop_info_smoke,
)
from route74.sources.yandex.smoke.time_guard_cases import run_yandex_time_guard_smoke


def main() -> None:
    current_time = datetime(2026, 6, 4, 7, 0, tzinfo=NOVOSIBIRSK_TZ)
    run_vehicle_parser_smoke(current_time)
    run_yandex_time_guard_smoke()
    run_vehicle_prediction_smoke()
    run_direction_smoke(current_time)
    run_stop_info_smoke()
    run_stop_info_midnight_text_smoke()
    run_raw_vehicle_invalid_coordinate_smoke(current_time)
    run_line_smoke()
    run_dump_smoke()
    run_browser_rate_limit_smoke()
    run_browser_capture_smoke()
    run_vehicle_prediction_coordinate_fallback_smoke(current_time)
    run_vehicle_prediction_invalid_coordinate_smoke(current_time)
    run_live_eta_evidence_guard_smoke()
    run_browser_cooldown_smoke(current_time)
    run_route_traffic_smoke(current_time)
    run_vehicle_prediction_source_smoke(current_time)
    run_auto_http_coordinates_continue_to_vehicle_prediction_smoke(current_time)
    run_stop_info_fallback_wins_http_schedule_smoke(current_time)
    run_vehicle_prediction_no_target_fallback_smoke(current_time)
    run_vehicle_prediction_source_coordinate_fallback_smoke(current_time)
    run_stop_info_fallback_smoke(current_time)
    print("OK | yandex parser smoke passed")
    run_live_probe()

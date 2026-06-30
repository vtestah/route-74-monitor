from __future__ import annotations

import os
import sqlite3
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from route74.domain.commute import CommuteProfile
from route74.domain.reporting import report_profiles_for_time
from route74.models import now_local
from route74.sources.yandex.constants import (
    expected_thread_ids,
    prediction_stop_ids,
    stop_id,
)
from route74.sources.yandex.line import YandexLineTopology
from route74.sources.yandex.models import YandexLiveForecast, YandexSourceStatus
from route74.sources.yandex.transport import YandexTransportSource
from route74.storage import (
    DEFAULT_DB,
    RouteTrafficSnapshot,
    connect,
    count_arrival_events,
    count_prediction_evaluations,
    count_prediction_events,
    count_yandex_observations,
    count_yandex_snapshots,
    init_db,
    insert_collector_run,
    insert_yandex_snapshot,
    prune_collector_runs,
    prune_yandex_telemetry,
    route_geometry_cache_status,
    touch_route_geometry,
    update_collector_heartbeat,
    upsert_route_geometry,
)


@dataclass(frozen=True)
class YandexTelemetryResult:
    profile_key: str
    source_method: str
    source_status: str
    available: bool
    vehicle_count: int
    arrival_minutes: tuple[int, ...]
    traffic_provider: str
    traffic_status: str
    traffic_reason: str
    route_geometry_status: str
    fallback_reason: str
    total_snapshots: int
    total_observations: int
    route_geometry_reason: str = ""
    prediction_events_created: int = 0
    arrival_events_created: int = 0
    evaluations_created: int = 0


@dataclass(frozen=True)
class RouteGeometryRefresh:
    status: str
    reason: str = ""


@dataclass(frozen=True)
class _PredictionLabCounts:
    prediction_events: int
    arrival_events: int
    evaluations: int


@dataclass(frozen=True)
class _PredictionLabDelta:
    prediction_events_created: int
    arrival_events_created: int
    evaluations_created: int


class YandexTelemetryCollector:
    def __init__(
        self,
        *,
        db_path: Path = DEFAULT_DB,
        source: YandexTransportSource | None = None,
        profiles: Iterable[CommuteProfile],
        heartbeat_name: str = "yandex-collect",
        profile_filter: str = "all",
        retention_days: int = 30,
        report_windows_only: bool = False,
        traffic_source: Callable[[CommuteProfile, datetime], RouteTrafficSnapshot | None] | None = None,
        clock: Callable[[], datetime] = now_local,
    ) -> None:
        self._db_path = db_path
        self._source = source or YandexTransportSource()
        self._profiles = tuple(profiles)
        self._heartbeat_name = heartbeat_name
        self._profile_filter = profile_filter
        self._retention_days = retention_days
        self._report_windows_only = report_windows_only
        self._traffic_source = traffic_source
        self._clock = clock

    def collect_once(self) -> tuple[YandexTelemetryResult, ...]:
        sampled_at = self._clock()
        results: list[YandexTelemetryResult] = []
        profiles = self._report_profiles(sampled_at)
        with connect(self._db_path) as connection:
            init_db(connection)
            if not profiles:
                skip_message = self._skip_message(sampled_at)
                if self._retention_days > 0:
                    prune_yandex_telemetry(
                        connection,
                        older_than=sampled_at - timedelta(days=self._retention_days),
                    )
                    prune_collector_runs(
                        connection,
                        older_than=sampled_at - timedelta(days=self._retention_days),
                        name=self._heartbeat_name,
                    )
                self._save_run(
                    connection,
                    started_at=sampled_at,
                    completed_at=self._clock(),
                    active_profiles=(),
                    status="skipped",
                    message=skip_message,
                    results=(),
                )
                update_collector_heartbeat(
                    connection,
                    name=self._heartbeat_name,
                    pid=os.getpid(),
                    profile_filter=self._profile_filter,
                    last_status="skipped",
                    last_message=skip_message,
                    updated_at=sampled_at,
                )
                return ()
            for profile in profiles:
                forecast = self._forecast(profile, sampled_at)
                route_geometry = self._refresh_route_geometry(connection, profile, forecast, sampled_at)
                traffic = self._traffic(profile, sampled_at)
                prediction_lab_before = _prediction_lab_counts(connection)
                insert_yandex_snapshot(
                    connection,
                    profile.key,
                    forecast,
                    sampled_at,
                    traffic=traffic,
                    route_geometry_status=route_geometry.status,
                    route_geometry_reason=route_geometry.reason,
                )
                prediction_lab = _prediction_lab_delta(
                    prediction_lab_before,
                    _prediction_lab_counts(connection),
                )
                results.append(
                    _result(
                        profile,
                        forecast,
                        traffic,
                        route_geometry,
                        connection,
                        prediction_lab,
                    )
                )
            if self._retention_days > 0:
                prune_yandex_telemetry(
                    connection,
                    older_than=sampled_at - timedelta(days=self._retention_days),
                )
                prune_collector_runs(
                    connection,
                    older_than=sampled_at - timedelta(days=self._retention_days),
                    name=self._heartbeat_name,
                )
            status = _status(results)
            message = _message(results)
            self._save_run(
                connection,
                started_at=sampled_at,
                completed_at=self._clock(),
                active_profiles=tuple(profile.key for profile in profiles),
                status=status,
                message=message,
                results=tuple(results),
            )
            update_collector_heartbeat(
                connection,
                name=self._heartbeat_name,
                pid=os.getpid(),
                profile_filter=self._profile_filter,
                last_status=status,
                last_message=message,
                updated_at=sampled_at,
            )
        return tuple(results)

    def _report_profiles(self, sampled_at: datetime) -> tuple[CommuteProfile, ...]:
        if not self._report_windows_only:
            return self._profiles
        active_profile_keys = set(report_profiles_for_time(sampled_at))
        return tuple(profile for profile in self._profiles if profile.key in active_profile_keys)

    def _skip_message(self, sampled_at: datetime) -> str:
        if self._report_windows_only and report_profiles_for_time(sampled_at):
            return "profile_filter_inactive"
        return "outside_report_window"

    def _forecast(self, profile: CommuteProfile, sampled_at: datetime) -> YandexLiveForecast:
        try:
            return self._source.get_forecast(profile, sampled_at)
        except Exception as error:
            reason = _error_reason("collector_error", error)
            return YandexLiveForecast.unavailable(
                status=YandexSourceStatus.UNAVAILABLE,
                reason=reason,
                diagnostics=(reason,),
            )

    def _traffic(self, profile: CommuteProfile, sampled_at: datetime) -> RouteTrafficSnapshot | None:
        if self._traffic_source is None:
            return None
        try:
            return self._traffic_source(profile, sampled_at)
        except Exception as error:
            return RouteTrafficSnapshot(
                provider="collector",
                status="error",
                raw={"error": _error_reason("traffic_error", error)},
            )

    def _refresh_route_geometry(
        self,
        connection: sqlite3.Connection,
        profile: CommuteProfile,
        forecast: YandexLiveForecast,
        sampled_at: datetime,
    ) -> RouteGeometryRefresh:
        consume_topologies = getattr(self._source, "consume_line_topologies", None)
        if not callable(consume_topologies):
            return RouteGeometryRefresh("not_supported")
        try:
            topologies = tuple(consume_topologies())
        except Exception as error:
            return RouteGeometryRefresh(_error_reason("route_geometry_error", error))
        if not topologies:
            return self._touch_route_geometry(connection, profile, forecast, sampled_at)
        rejected: list[RouteGeometryRefresh] = []
        for topology in topologies:
            inspection = _inspect_route_geometry_topology(profile, topology)
            if inspection.status != "ok":
                rejected.append(inspection)
                continue
            try:
                upsert_route_geometry(
                    connection,
                    profile_key=profile.key,
                    target_stop_id=stop_id(profile),
                    topology=topology,
                    updated_at=sampled_at,
                    preferred_thread_ids=expected_thread_ids(profile),
                    candidate_stop_ids=prediction_stop_ids(profile),
                )
                return RouteGeometryRefresh("saved", inspection.reason)
            except ValueError as error:
                rejected.append(
                    RouteGeometryRefresh(
                        "no_target_stop",
                        _error_reason("route_geometry_rejected", error),
                    )
                )
        return _best_route_geometry_rejection(tuple(rejected))

    def _touch_route_geometry(
        self,
        connection: sqlite3.Connection,
        profile: CommuteProfile,
        forecast: YandexLiveForecast,
        sampled_at: datetime,
    ) -> RouteGeometryRefresh:
        if forecast.vehicle_count > 0:
            try:
                if touch_route_geometry(connection, profile_key=profile.key, updated_at=sampled_at):
                    return RouteGeometryRefresh("touched")
            except Exception as error:
                return RouteGeometryRefresh(_error_reason("route_geometry_touch_error", error))
        return RouteGeometryRefresh(
            route_geometry_cache_status(connection, profile_key=profile.key, sampled_at=sampled_at) or "not_found"
        )

    def _save_run(
        self,
        connection: sqlite3.Connection,
        *,
        started_at: datetime,
        completed_at: datetime,
        active_profiles: tuple[str, ...],
        status: str,
        message: str,
        results: tuple[YandexTelemetryResult, ...],
    ) -> None:
        insert_collector_run(
            connection,
            name=self._heartbeat_name,
            started_at=started_at,
            completed_at=completed_at,
            profile_filter=self._profile_filter,
            report_windows_only=self._report_windows_only,
            active_profiles=active_profiles,
            status=status,
            message=message,
            result_count=len(results),
            eta_result_count=sum(1 for result in results if result.arrival_minutes),
            traffic_ok_count=sum(1 for result in results if result.traffic_status == "ok"),
            raw={"results": [_run_result(result) for result in results]},
        )


def _status(results: list[YandexTelemetryResult]) -> str:
    if not results:
        return "empty"
    if any(result.traffic_status == "error" for result in results):
        return "partial"
    if all(result.available for result in results):
        return "ok"
    if any(result.available for result in results):
        return "partial"
    return results[-1].source_status


def _message(results: list[YandexTelemetryResult]) -> str:
    if not results:
        return "no profiles"
    return "; ".join(
        (
            f"{result.profile_key}:{result.source_method}/{result.source_status}"
            f"/eta={len(result.arrival_minutes)}"
            f"/traffic={result.traffic_provider}/{result.traffic_status}"
            f"/geometry={_geometry_message(result)}"
            f"/prediction_lab={_prediction_lab_message(result)}"
        )
        for result in results
    )


def _error_reason(prefix: str, error: Exception) -> str:
    detail = str(error).strip() or type(error).__name__
    detail = detail[:160]
    return f"{prefix}:{type(error).__name__}:{detail}"


def _result(
    profile: CommuteProfile,
    forecast: YandexLiveForecast,
    traffic: RouteTrafficSnapshot | None,
    route_geometry: RouteGeometryRefresh,
    connection: sqlite3.Connection,
    prediction_lab: _PredictionLabDelta,
) -> YandexTelemetryResult:
    return YandexTelemetryResult(
        profile_key=profile.key,
        source_method=forecast.source_method.value,
        source_status=forecast.status.value,
        available=forecast.available,
        vehicle_count=forecast.vehicle_count,
        arrival_minutes=forecast.arrival_minutes,
        traffic_provider=traffic.provider if traffic is not None else "none",
        traffic_status=traffic.status if traffic is not None else "not_collected",
        traffic_reason=_traffic_reason(traffic),
        route_geometry_status=route_geometry.status,
        fallback_reason=forecast.fallback_reason,
        total_snapshots=count_yandex_snapshots(connection),
        total_observations=count_yandex_observations(connection),
        route_geometry_reason=route_geometry.reason,
        prediction_events_created=prediction_lab.prediction_events_created,
        arrival_events_created=prediction_lab.arrival_events_created,
        evaluations_created=prediction_lab.evaluations_created,
    )


def _run_result(result: YandexTelemetryResult) -> dict[str, object]:
    return {
        "profile_key": result.profile_key,
        "source_method": result.source_method,
        "source_status": result.source_status,
        "available": result.available,
        "vehicle_count": result.vehicle_count,
        "arrival_minutes": list(result.arrival_minutes),
        "traffic_provider": result.traffic_provider,
        "traffic_status": result.traffic_status,
        "traffic_reason": result.traffic_reason,
        "route_geometry_status": result.route_geometry_status,
        "route_geometry_reason": result.route_geometry_reason,
        "fallback_reason": result.fallback_reason,
        "prediction_events_created": result.prediction_events_created,
        "arrival_events_created": result.arrival_events_created,
        "evaluations_created": result.evaluations_created,
    }


def _traffic_reason(traffic: RouteTrafficSnapshot | None) -> str:
    if traffic is None or not isinstance(traffic.raw, dict):
        return ""
    for key in ("reason", "error"):
        value = traffic.raw.get(key)
        if isinstance(value, str) and value:
            return value[:240]
    return ""


def _inspect_route_geometry_topology(
    profile: CommuteProfile,
    topology: YandexLineTopology,
) -> RouteGeometryRefresh:
    expected = expected_thread_ids(profile)
    candidate_stop_ids = _route_geometry_candidate_stop_ids(profile)
    selected = topology.thread_for_stops(candidate_stop_ids, preferred_thread_ids=expected)
    if selected is None:
        return RouteGeometryRefresh(
            "no_target_stop",
            _topology_reason(
                topology,
                expected_thread_ids=expected,
                candidate_stop_ids=candidate_stop_ids,
            ),
        )
    thread, selected_stop_id = selected
    reason = _topology_reason(
        topology,
        expected_thread_ids=expected,
        candidate_stop_ids=candidate_stop_ids,
        selected_thread_id=thread.thread_id,
        selected_stop_id=selected_stop_id,
    )
    if expected and thread.thread_id not in expected:
        return RouteGeometryRefresh("thread_drift", reason)
    return RouteGeometryRefresh("ok", reason)


def _best_route_geometry_rejection(
    rejections: tuple[RouteGeometryRefresh, ...],
) -> RouteGeometryRefresh:
    if not rejections:
        return RouteGeometryRefresh("not_found")
    priority = {"thread_drift": 0, "no_target_stop": 1}
    return sorted(rejections, key=lambda item: priority.get(item.status, 9))[0]


def _route_geometry_candidate_stop_ids(profile: CommuteProfile) -> tuple[str, ...]:
    return _unique_strings((*prediction_stop_ids(profile), stop_id(profile)))


def _topology_reason(
    topology: YandexLineTopology,
    *,
    expected_thread_ids: tuple[str, ...],
    candidate_stop_ids: tuple[str, ...],
    selected_thread_id: str = "",
    selected_stop_id: str = "",
) -> str:
    topology_threads = _unique_strings(tuple(thread.thread_id for thread in topology.threads))
    parts = (
        ("expected", "|".join(expected_thread_ids) or "-"),
        ("selected", selected_thread_id or "-"),
        ("stop", selected_stop_id or "-"),
        ("active", topology.active_thread_id or "-"),
        ("candidates", "|".join(candidate_stop_ids) or "-"),
        ("threads", "|".join(topology_threads[:4]) or "-"),
    )
    return _compact_reason(",".join(f"{key}={value}" for key, value in parts))


def _geometry_message(result: YandexTelemetryResult) -> str:
    if not result.route_geometry_reason:
        return result.route_geometry_status
    return f"{result.route_geometry_status}({result.route_geometry_reason})"


def _prediction_lab_counts(connection: sqlite3.Connection) -> _PredictionLabCounts:
    return _PredictionLabCounts(
        prediction_events=count_prediction_events(connection),
        arrival_events=count_arrival_events(connection),
        evaluations=count_prediction_evaluations(connection),
    )


def _prediction_lab_delta(
    before: _PredictionLabCounts,
    after: _PredictionLabCounts,
) -> _PredictionLabDelta:
    return _PredictionLabDelta(
        prediction_events_created=after.prediction_events - before.prediction_events,
        arrival_events_created=after.arrival_events - before.arrival_events,
        evaluations_created=after.evaluations - before.evaluations,
    )


def _prediction_lab_message(result: YandexTelemetryResult) -> str:
    return f"p{result.prediction_events_created}/a{result.arrival_events_created}/e{result.evaluations_created}"


def _compact_reason(value: str, *, limit: int = 200) -> str:
    printable = "".join(character if character.isprintable() else " " for character in value)
    return " ".join(printable.split())[:limit]


def _unique_strings(values: tuple[str, ...]) -> tuple[str, ...]:
    result: list[str] = []
    for value in values:
        if value and value not in result:
            result.append(value)
    return tuple(result)

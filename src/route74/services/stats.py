from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from route74.build_info import BuildInfo, load_build_info
from route74.diagnostics import sanitize_diagnostic_text
from route74.domain.commute import CommuteProfile, DepartureDecision
from route74.domain.reporting import report_window_for_profile
from route74.domain.runtime_sources import BOT_EVENT_USER_REPLY
from route74.services.commute import CommuteService
from route74.services.yandex_history import (
    DEFAULT_FALLBACK_BUCKET_MINUTES,
    DEFAULT_HISTORY_DAYS,
    DEFAULT_HISTORY_MAX_AGE_SECONDS,
    DEFAULT_MIN_HISTORY_DAYS,
    DEFAULT_MIN_OBSERVATIONS,
    DEFAULT_PRIMARY_BUCKET_MINUTES,
)
from route74.storage import (
    DEFAULT_DB,
    ForecastReadinessSummary,
    YandexTelemetrySummary,
    connect,
    init_db,
    summarize_yandex_forecast_readiness,
    summarize_yandex_telemetry,
)
from route74.storage import (
    BotLatencySummary,
    BotRuntimeCalibration,
    BotRuntimePredictionQuality,
    summarize_bot_latency,
    summarize_bot_runtime_calibration,
    summarize_bot_runtime_predictions,
)
from route74.storage.forecast_backtest import (
    DEFAULT_FORECAST_BACKTEST_PERCENTILES,
    ForecastBacktestSummary,
    summarize_yandex_forecast_backtest,
)
from route74.storage.forecast_health import ForecastHealthSummary, summarize_forecast_health
from route74.storage.helpers import WEEKDAYS
from route74.storage.monitoring import summarize_monitor
from route74.support_actions import (
    bot_latency_command,
    forecast_backtest_command_for_profile,
    forecast_coverage_command_for_profile,
    forecast_readiness_command_for_profile,
    prediction_calibration_command_for_profile,
    prediction_evaluate_command_for_profile,
    support_report_command_for_profile,
    watch_state_command_for_path,
)
from route74.support_triage import (
    build_support_triage,
    operator_primary_triage_item,
)
from route74.watch_state import DEFAULT_WATCH_STATE_PATH, WatchStateSummary, summarize_watch_state


SUMMARY_ERRORS = (OSError, sqlite3.Error, ValueError)


@dataclass(frozen=True)
class StatsSnapshot:
    decision: DepartureDecision
    telemetry: YandexTelemetrySummary | None = None
    forecast_health: ForecastHealthSummary | None = None
    forecast_readiness: ForecastReadinessSummary | None = None
    forecast_backtest: ForecastBacktestSummary | None = None
    bot_latency: BotLatencySummary | None = None
    runtime_quality: BotRuntimePredictionQuality | None = None
    runtime_calibration: BotRuntimeCalibration | None = None
    watch_state: WatchStateSummary | None = None
    build_info: BuildInfo | None = None
    telemetry_error: str = ""
    forecast_health_error: str = ""
    forecast_readiness_error: str = ""
    forecast_backtest_error: str = ""
    bot_latency_error: str = ""
    runtime_error: str = ""
    watch_state_error: str = ""
    bot_latency_command: str = ""
    forecast_readiness_command: str = ""
    forecast_coverage_command: str = ""
    forecast_backtest_command: str = ""
    support_report_command: str = ""
    prediction_calibration_command: str = ""
    prediction_evaluate_command: str = ""
    watch_state_command: str = ""
    triage_action_command: str = ""
    triage_action_key: str = ""
    triage_action_message: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.decision, DepartureDecision):
            raise ValueError("stats snapshot decision needs DepartureDecision")
        if self.telemetry is not None and not isinstance(self.telemetry, YandexTelemetrySummary):
            raise ValueError("stats snapshot telemetry needs YandexTelemetrySummary or None")
        if self.forecast_health is not None and not isinstance(self.forecast_health, ForecastHealthSummary):
            raise ValueError("stats snapshot forecast_health needs ForecastHealthSummary or None")
        if self.forecast_readiness is not None and not isinstance(self.forecast_readiness, ForecastReadinessSummary):
            raise ValueError("stats snapshot forecast_readiness needs ForecastReadinessSummary or None")
        if self.forecast_backtest is not None and not isinstance(self.forecast_backtest, ForecastBacktestSummary):
            raise ValueError("stats snapshot forecast_backtest needs ForecastBacktestSummary or None")
        if self.bot_latency is not None and not isinstance(self.bot_latency, BotLatencySummary):
            raise ValueError("stats snapshot bot_latency needs BotLatencySummary or None")
        if self.runtime_quality is not None and not isinstance(self.runtime_quality, BotRuntimePredictionQuality):
            raise ValueError("stats snapshot runtime_quality needs BotRuntimePredictionQuality or None")
        if self.runtime_calibration is not None and not isinstance(self.runtime_calibration, BotRuntimeCalibration):
            raise ValueError("stats snapshot runtime_calibration needs BotRuntimeCalibration or None")
        if self.watch_state is not None and not isinstance(self.watch_state, WatchStateSummary):
            raise ValueError("stats snapshot watch_state needs WatchStateSummary or None")
        if self.build_info is not None and not isinstance(self.build_info, BuildInfo):
            raise ValueError("stats snapshot build_info needs BuildInfo or None")
        if not isinstance(self.telemetry_error, str):
            raise ValueError("stats snapshot telemetry_error needs text")
        if not isinstance(self.forecast_health_error, str):
            raise ValueError("stats snapshot forecast_health_error needs text")
        if not isinstance(self.forecast_readiness_error, str):
            raise ValueError("stats snapshot forecast_readiness_error needs text")
        if not isinstance(self.forecast_backtest_error, str):
            raise ValueError("stats snapshot forecast_backtest_error needs text")
        if not isinstance(self.bot_latency_error, str):
            raise ValueError("stats snapshot bot_latency_error needs text")
        if not isinstance(self.runtime_error, str):
            raise ValueError("stats snapshot runtime_error needs text")
        if not isinstance(self.watch_state_error, str):
            raise ValueError("stats snapshot watch_state_error needs text")
        if not isinstance(self.bot_latency_command, str):
            raise ValueError("stats snapshot bot_latency_command needs text")
        if not isinstance(self.forecast_readiness_command, str):
            raise ValueError("stats snapshot forecast_readiness_command needs text")
        if not isinstance(self.forecast_coverage_command, str):
            raise ValueError("stats snapshot forecast_coverage_command needs text")
        if not isinstance(self.forecast_backtest_command, str):
            raise ValueError("stats snapshot forecast_backtest_command needs text")
        if not isinstance(self.support_report_command, str):
            raise ValueError("stats snapshot support_report_command needs text")
        if not isinstance(self.prediction_calibration_command, str):
            raise ValueError("stats snapshot prediction_calibration_command needs text")
        if not isinstance(self.prediction_evaluate_command, str):
            raise ValueError("stats snapshot prediction_evaluate_command needs text")
        if not isinstance(self.watch_state_command, str):
            raise ValueError("stats snapshot watch_state_command needs text")
        if not isinstance(self.triage_action_command, str):
            raise ValueError("stats snapshot triage_action_command needs text")
        if not isinstance(self.triage_action_key, str):
            raise ValueError("stats snapshot triage_action_key needs text")
        if not isinstance(self.triage_action_message, str):
            raise ValueError("stats snapshot triage_action_message needs text")
        telemetry_error = _clean_error_text(self.telemetry_error)
        forecast_health_error = _clean_error_text(self.forecast_health_error)
        forecast_readiness_error = _clean_error_text(self.forecast_readiness_error)
        forecast_backtest_error = _clean_error_text(self.forecast_backtest_error)
        bot_latency_error = _clean_error_text(self.bot_latency_error)
        runtime_error = _clean_error_text(self.runtime_error)
        watch_state_error = _clean_error_text(self.watch_state_error)
        bot_latency_command = _clean_error_text(self.bot_latency_command)
        forecast_readiness_command = _clean_error_text(self.forecast_readiness_command)
        forecast_coverage_command = _clean_error_text(self.forecast_coverage_command)
        forecast_backtest_command = _clean_error_text(self.forecast_backtest_command)
        support_report_command = _clean_error_text(self.support_report_command)
        prediction_calibration_command = _clean_error_text(self.prediction_calibration_command)
        prediction_evaluate_command = _clean_error_text(self.prediction_evaluate_command)
        watch_state_command = _clean_error_text(self.watch_state_command)
        triage_action_command = _clean_error_text(self.triage_action_command)
        triage_action_key = _clean_error_text(self.triage_action_key)
        triage_action_message = _clean_error_text(self.triage_action_message)
        if self.telemetry is not None and telemetry_error:
            raise ValueError("stats snapshot telemetry_error must be empty when telemetry is available")
        if self.forecast_health is not None and forecast_health_error:
            raise ValueError("stats snapshot forecast_health_error must be empty when forecast health is available")
        if self.forecast_readiness is not None and forecast_readiness_error:
            raise ValueError("stats snapshot forecast_readiness_error must be empty when forecast readiness is available")
        if self.forecast_backtest is not None and forecast_backtest_error:
            raise ValueError("stats snapshot forecast_backtest_error must be empty when forecast backtest is available")
        if self.bot_latency is not None and bot_latency_error:
            raise ValueError("stats snapshot bot_latency_error must be empty when bot latency is available")
        if (self.runtime_quality is None) != (self.runtime_calibration is None):
            raise ValueError("stats snapshot runtime diagnostics need quality and calibration together")
        if self.runtime_quality is not None and runtime_error:
            raise ValueError("stats snapshot runtime_error must be empty when runtime diagnostics are available")
        if self.watch_state is not None and watch_state_error:
            raise ValueError("stats snapshot watch_state_error must be empty when watch_state is available")
        object.__setattr__(self, "telemetry_error", telemetry_error)
        object.__setattr__(self, "forecast_health_error", forecast_health_error)
        object.__setattr__(self, "forecast_readiness_error", forecast_readiness_error)
        object.__setattr__(self, "forecast_backtest_error", forecast_backtest_error)
        object.__setattr__(self, "bot_latency_error", bot_latency_error)
        object.__setattr__(self, "runtime_error", runtime_error)
        object.__setattr__(self, "watch_state_error", watch_state_error)
        object.__setattr__(self, "bot_latency_command", bot_latency_command)
        object.__setattr__(self, "forecast_readiness_command", forecast_readiness_command)
        object.__setattr__(self, "forecast_coverage_command", forecast_coverage_command)
        object.__setattr__(self, "forecast_backtest_command", forecast_backtest_command)
        object.__setattr__(self, "support_report_command", support_report_command)
        object.__setattr__(self, "prediction_calibration_command", prediction_calibration_command)
        object.__setattr__(self, "prediction_evaluate_command", prediction_evaluate_command)
        object.__setattr__(self, "watch_state_command", watch_state_command)
        object.__setattr__(self, "triage_action_command", triage_action_command)
        object.__setattr__(self, "triage_action_key", triage_action_key)
        object.__setattr__(self, "triage_action_message", triage_action_message)


class StatsService:
    def __init__(
        self,
        commute_service: CommuteService,
        *,
        db_path: Path = DEFAULT_DB,
        watch_state_path: Path = DEFAULT_WATCH_STATE_PATH,
        summary_hours: int = 24,
    ) -> None:
        self._commute_service = commute_service
        self._db_path = db_path
        self._watch_state_path = watch_state_path
        self._summary_hours = _positive_summary_hours(summary_hours)

    def build(self, profile: CommuteProfile, walk_minutes: int) -> StatsSnapshot:
        decision = self._commute_service.build_decision(profile, walk_minutes)
        forecast_coverage_command = forecast_coverage_command_for_profile(profile.key)
        telemetry = None
        forecast_health = None
        forecast_readiness = None
        forecast_backtest = None
        bot_latency = None
        runtime_quality = None
        runtime_calibration = None
        watch_state = None
        telemetry_error = ""
        forecast_health_error = ""
        forecast_readiness_error = ""
        forecast_backtest_error = ""
        bot_latency_error = ""
        runtime_error = ""
        watch_state_error = ""
        triage_action_command = ""
        triage_action_key = ""
        triage_action_message = ""
        watch_state, watch_state_error = _build_watch_state_summary(
            self._watch_state_path,
            current_time=decision.current_time,
        )
        try:
            with connect(self._db_path) as connection:
                init_db(connection)
                forecast_health, forecast_health_error = _build_forecast_health_summary(
                    connection,
                    current_time=decision.current_time,
                )
                forecast_readiness, forecast_readiness_error = _build_forecast_readiness_summary(
                    connection,
                    profile_key=profile.key,
                    current_time=decision.current_time,
                )
                forecast_backtest, forecast_backtest_error = _build_forecast_backtest_summary(
                    connection,
                    profile_key=profile.key,
                )
                telemetry, telemetry_error = _build_telemetry_summary(
                    connection,
                    hours=self._summary_hours,
                    profile_key=profile.key,
                    current_time=decision.current_time,
                )
                bot_latency, bot_latency_error = _build_bot_latency_summary(
                    connection,
                    hours=self._summary_hours,
                    profile_key=profile.key,
                    current_time=decision.current_time,
                )
                runtime_quality, runtime_calibration, runtime_error = _build_runtime_summary(
                    connection,
                    hours=self._summary_hours,
                    profile_key=profile.key,
                    current_time=decision.current_time,
                )
                triage_action_command, triage_action_key, triage_action_message = _build_triage_action(
                    connection,
                    db_path=self._db_path,
                    hours=self._summary_hours,
                    profile_key=profile.key,
                    current_time=decision.current_time,
                    watch_state=watch_state,
                )
        except SUMMARY_ERRORS as error:
            telemetry_error = bot_latency_error = runtime_error = _summary_error(error)
            forecast_health_error = _summary_error(error)
            forecast_readiness_error = _summary_error(error)
            forecast_backtest_error = _summary_error(error)
        return StatsSnapshot(
            decision=decision,
            telemetry=telemetry,
            forecast_health=forecast_health,
            forecast_readiness=forecast_readiness,
            forecast_backtest=forecast_backtest,
            bot_latency=bot_latency,
            runtime_quality=runtime_quality,
            runtime_calibration=runtime_calibration,
            watch_state=watch_state,
            build_info=load_build_info(),
            telemetry_error=telemetry_error,
            forecast_health_error=forecast_health_error,
            forecast_readiness_error=forecast_readiness_error,
            forecast_backtest_error=forecast_backtest_error,
            bot_latency_error=bot_latency_error,
            runtime_error=runtime_error,
            watch_state_error=watch_state_error,
            bot_latency_command=bot_latency_command(
                hours=self._summary_hours,
                profile_key=profile.key,
                event_kind=BOT_EVENT_USER_REPLY,
            ),
            forecast_readiness_command=forecast_readiness_command_for_profile(profile.key),
            forecast_coverage_command=forecast_coverage_command,
            forecast_backtest_command=forecast_backtest_command_for_profile(profile.key),
            support_report_command=support_report_command_for_profile(profile.key),
            prediction_calibration_command=prediction_calibration_command_for_profile(profile.key),
            prediction_evaluate_command=prediction_evaluate_command_for_profile(profile.key),
            watch_state_command=watch_state_command_for_path(self._watch_state_path),
            triage_action_command=triage_action_command,
            triage_action_key=triage_action_key,
            triage_action_message=triage_action_message,
        )


def _build_telemetry_summary(
    connection: sqlite3.Connection,
    *,
    hours: int,
    profile_key: str,
    current_time: datetime,
) -> tuple[YandexTelemetrySummary | None, str]:
    try:
        return (
            summarize_yandex_telemetry(
                connection,
                hours=hours,
                profile_key=profile_key,
                current_time=current_time,
            ),
            "",
        )
    except SUMMARY_ERRORS as error:
        return None, _summary_error(error)


def _build_forecast_health_summary(
    connection: sqlite3.Connection,
    *,
    current_time: datetime,
) -> tuple[ForecastHealthSummary | None, str]:
    try:
        return (
            summarize_forecast_health(
                connection,
                current_date=current_time,
                days=DEFAULT_HISTORY_DAYS,
                min_samples=DEFAULT_MIN_OBSERVATIONS,
                min_distinct_days=DEFAULT_MIN_HISTORY_DAYS,
                primary_bucket_minutes=DEFAULT_PRIMARY_BUCKET_MINUTES,
                fallback_bucket_minutes=DEFAULT_FALLBACK_BUCKET_MINUTES,
                max_age_seconds=DEFAULT_HISTORY_MAX_AGE_SECONDS,
                step_minutes=DEFAULT_PRIMARY_BUCKET_MINUTES,
            ),
            "",
        )
    except SUMMARY_ERRORS as error:
        return None, _summary_error(error)


def _build_forecast_readiness_summary(
    connection: sqlite3.Connection,
    *,
    profile_key: str,
    current_time: datetime,
) -> tuple[ForecastReadinessSummary | None, str]:
    try:
        window = report_window_for_profile(profile_key)
        readiness_time = current_time.replace(
            hour=window.start.hour,
            minute=window.start.minute,
            second=0,
            microsecond=0,
        ) + timedelta(minutes=DEFAULT_PRIMARY_BUCKET_MINUTES)
        return (
            summarize_yandex_forecast_readiness(
                connection,
                profile_key=profile_key,
                current_time=readiness_time,
                days=DEFAULT_HISTORY_DAYS,
                min_samples=DEFAULT_MIN_OBSERVATIONS,
                min_distinct_days=DEFAULT_MIN_HISTORY_DAYS,
                primary_bucket_minutes=DEFAULT_PRIMARY_BUCKET_MINUTES,
                fallback_bucket_minutes=DEFAULT_FALLBACK_BUCKET_MINUTES,
                max_age_seconds=DEFAULT_HISTORY_MAX_AGE_SECONDS,
                weekdays=WEEKDAYS,
                report_window_key=window.key,
            ),
            "",
        )
    except SUMMARY_ERRORS as error:
        return None, _summary_error(error)


def _build_forecast_backtest_summary(
    connection: sqlite3.Connection,
    *,
    profile_key: str,
) -> tuple[ForecastBacktestSummary | None, str]:
    try:
        window = report_window_for_profile(profile_key)
        return (
            summarize_yandex_forecast_backtest(
                connection,
                profile_key=profile_key,
                report_window_key=window.key,
                history_days=DEFAULT_HISTORY_DAYS,
                bucket_minutes=DEFAULT_PRIMARY_BUCKET_MINUTES,
                min_samples=DEFAULT_MIN_OBSERVATIONS,
                min_distinct_days=DEFAULT_MIN_HISTORY_DAYS,
                percentiles=DEFAULT_FORECAST_BACKTEST_PERCENTILES,
                max_age_seconds=DEFAULT_HISTORY_MAX_AGE_SECONDS,
            ),
            "",
        )
    except SUMMARY_ERRORS as error:
        return None, _summary_error(error)


def _build_bot_latency_summary(
    connection: sqlite3.Connection,
    *,
    hours: int,
    profile_key: str,
    current_time: datetime,
) -> tuple[BotLatencySummary | None, str]:
    try:
        return (
            summarize_bot_latency(
                connection,
                hours=hours,
                profile_key=profile_key,
                current_time=current_time,
                event_kind=BOT_EVENT_USER_REPLY,
            ),
            "",
        )
    except SUMMARY_ERRORS as error:
        return None, _summary_error(error)


def _build_runtime_summary(
    connection: sqlite3.Connection,
    *,
    hours: int,
    profile_key: str,
    current_time: datetime,
) -> tuple[BotRuntimePredictionQuality | None, BotRuntimeCalibration | None, str]:
    try:
        quality = summarize_bot_runtime_predictions(
            connection,
            hours=hours,
            profile_key=profile_key,
            current_time=current_time,
            event_kind=BOT_EVENT_USER_REPLY,
        )
        calibration = summarize_bot_runtime_calibration(
            connection,
            hours=hours,
            profile_key=profile_key,
            current_time=current_time,
            event_kind=BOT_EVENT_USER_REPLY,
        )
    except SUMMARY_ERRORS as error:
        return None, None, _summary_error(error)
    return quality, calibration, ""


def _build_watch_state_summary(
    path: Path,
    *,
    current_time: datetime,
) -> tuple[WatchStateSummary | None, str]:
    try:
        return summarize_watch_state(path, current_time), ""
    except SUMMARY_ERRORS as error:
        return None, _summary_error(error)


def _build_triage_action(
    connection: sqlite3.Connection,
    *,
    db_path: Path,
    hours: int,
    profile_key: str,
    current_time: datetime,
    watch_state: WatchStateSummary | None,
) -> tuple[str, str, str]:
    try:
        monitor = summarize_monitor(
            connection,
            db_path=db_path,
            latency_hours=hours,
            runtime_hours=hours,
            profile_key=profile_key,
            current_time=current_time,
        )
        if monitor.runtime is None or monitor.calibration is None:
            return "", "", ""
        window = report_window_for_profile(profile_key)
        triage = build_support_triage(
            window_key=window.key,
            profile_key=profile_key,
            hours=hours,
            monitor=monitor,
            forecast=monitor.forecast,
            runtime_quality=monitor.runtime,
            runtime_calibration=monitor.calibration,
            runtime_event_kind=BOT_EVENT_USER_REPLY,
            watch_state=watch_state,
        )
        item = operator_primary_triage_item(triage)
        if item is None:
            return "", "", ""
        return item.action, item.key, item.message
    except SUMMARY_ERRORS:
        return "", "", ""


def _positive_summary_hours(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("stats summary_hours must be a positive integer")
    return value


def _summary_error(error: Exception) -> str:
    detail = sanitize_diagnostic_text(str(error), fallback="", limit=120)
    reason = f"{type(error).__name__}: {detail}" if detail else type(error).__name__
    return sanitize_diagnostic_text(reason, fallback=type(error).__name__, limit=120)


def _telemetry_error(error: Exception) -> str:
    return _summary_error(error)


def _clean_error_text(value: str) -> str:
    return sanitize_diagnostic_text(value, fallback="", limit=1000)

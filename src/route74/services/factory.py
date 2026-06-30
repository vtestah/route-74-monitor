"""Factory for CommuteService — shared between web and CLI."""

from __future__ import annotations

from pathlib import Path

from route74.services.commute import CommuteService
from route74.services.prediction_engine import PredictionEngine
from route74.services.yandex_history import YandexHistoryPredictor
from route74.sources.yandex.config import YandexSourceConfig
from route74.sources.yandex.transport import YandexTransportSource
from route74.storage.forecast_backtest import DEFAULT_FORECAST_BACKTEST_PERCENTILES


def commute_service(db_path: Path) -> CommuteService:
    return CommuteService(
        yandex_source=YandexTransportSource(YandexSourceConfig()),
        history_predictor=YandexHistoryPredictor(
            db_path=db_path,
            backtest_percentiles=DEFAULT_FORECAST_BACKTEST_PERCENTILES,
        ),
        prediction_engine=PredictionEngine(db_path=db_path),
    )

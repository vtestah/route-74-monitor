from __future__ import annotations

from collections.abc import Callable

from route74.domain.prediction_buckets import (
    PREDICTION_ETA_BUCKETS,
    PredictionEtaBucket,
    prediction_bucket_label,
)


def main() -> None:
    _assert_equal(
        tuple(bucket.label for bucket in PREDICTION_ETA_BUCKETS),
        ("0-3", "3-6", "6-10", "10-15", "15+"),
    )
    _assert_equal(prediction_bucket_label(0), "0-3")
    _assert_equal(prediction_bucket_label(15), "10-15")
    _assert_equal(prediction_bucket_label(16), "15+")
    _assert_value_error(
        lambda: PredictionEtaBucket("", max_minutes=3, accuracy_tolerance_minutes=1),
        "label is required",
    )
    _assert_value_error(
        lambda: PredictionEtaBucket("0-3 ", max_minutes=3, accuracy_tolerance_minutes=1),
        "label must be compact",
    )
    _assert_value_error(
        lambda: PredictionEtaBucket("0 3", max_minutes=3, accuracy_tolerance_minutes=1),
        "label must be compact",
    )
    _assert_value_error(
        lambda: PredictionEtaBucket("\u0434\u043e-3", max_minutes=3, accuracy_tolerance_minutes=1),
        "ASCII key",
    )
    _assert_value_error(
        lambda: PredictionEtaBucket("0/3", max_minutes=3, accuracy_tolerance_minutes=1),
        "ASCII key",
    )
    _assert_value_error(
        lambda: PredictionEtaBucket("0-3", max_minutes=3, accuracy_tolerance_minutes=4),
        "finite bucket max",
    )
    print("OK | prediction buckets smoke passed")


def _assert_equal(actual: object, expected: object) -> None:
    if actual != expected:
        raise AssertionError(f"expected {expected!r}, got {actual!r}")


def _assert_value_error(action: Callable[[], object], expected: str) -> None:
    try:
        action()
    except ValueError as error:
        if expected not in str(error):
            raise AssertionError(f"expected {expected!r} in {error!s}") from error
    else:
        raise AssertionError(f"expected ValueError containing {expected!r}")


if __name__ == "__main__":
    main()

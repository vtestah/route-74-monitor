from __future__ import annotations

from route74.diagnostics import sanitize_diagnostic_text
from route74.domain.eta import EtaFactor, EtaFactorKind, eta_scope_text


MAX_VISIBLE_ETA_FACTORS = 4


def eta_factor_texts(factors: tuple[EtaFactor, ...], *, limit: int = MAX_VISIBLE_ETA_FACTORS) -> tuple[str, ...]:
    if not isinstance(factors, tuple):
        raise ValueError("ETA factors need tuple")
    if limit <= 0:
        return ()
    texts = tuple(text for factor in factors if (text := _eta_factor_text(factor)))
    if len(texts) <= limit:
        return texts
    return (*texts[:limit], f"+{len(texts) - limit} ещё")


def format_eta_factor_texts(factors: tuple[EtaFactor, ...], *, limit: int = MAX_VISIBLE_ETA_FACTORS) -> tuple[str, ...]:
    return eta_factor_texts(factors, limit=limit)


def format_eta_factor_payload_texts(
    payloads: tuple[dict[str, object], ...],
    *,
    limit: int = MAX_VISIBLE_ETA_FACTORS,
) -> tuple[str, ...]:
    if not isinstance(payloads, tuple):
        raise ValueError("ETA factor payloads need tuple")
    if limit <= 0:
        return ()
    texts = tuple(text for payload in payloads if (text := _eta_factor_payload_text(payload)))
    if len(texts) <= limit:
        return texts
    return (*texts[:limit], f"+{len(texts) - limit} ещё")


def eta_factors_line(factors: tuple[EtaFactor, ...]) -> str:
    texts = eta_factor_texts(factors)
    if not texts:
        return ""
    return f"🧮 Почему: {'; '.join(texts)}"


def _eta_factor_text(factor: EtaFactor) -> str:
    if not isinstance(factor, EtaFactor):
        raise ValueError("ETA factors need EtaFactor")
    if factor.kind == EtaFactorKind.RESIDUAL_CORRECTION:
        return (
            f"поправка: на {format_duration_minutes(factor.minutes)} раньше"
            f"{_sample_suffix(factor)}{_scope_suffix(factor)}"
        )
    if factor.kind == EtaFactorKind.SAFETY_BUFFER:
        return (
            f"запас +{format_duration_minutes(factor.minutes)}{_risk_suffix(factor)}"
            f"{_sample_suffix(factor)}{_scope_suffix(factor)}"
        )
    if factor.kind == EtaFactorKind.SOURCE_RISK:
        return f"риск промаха {_percent(factor)}{_sample_suffix(factor)}{_scope_suffix(factor)}"
    if factor.kind == EtaFactorKind.GUARDRAIL_UNAVAILABLE:
        return "прошлые поправки недоступны"
    if factor.kind == EtaFactorKind.SPREAD:
        return f"разброс {format_duration_minutes(factor.minutes)} между сигналами"
    if factor.kind == EtaFactorKind.HISTORY_SAMPLE:
        percentile = f" p{factor.percent}" if factor.percent else ""
        return f"история{percentile}: {_sample_count_text(factor.sample_count)}"
    if factor.kind == EtaFactorKind.HISTORY_DISAGREEMENT:
        return (
            f"история на {format_duration_minutes(factor.minutes)} {_history_disagreement_text(factor.scope)} "
            f"не выбрана{_sample_suffix(factor)}"
        )
    if factor.kind == EtaFactorKind.VEHICLE_PROGRESS_BUFFER:
        return f"координата: запас +{format_duration_minutes(factor.minutes)}{_sample_suffix(factor)}"
    if factor.kind == EtaFactorKind.IGNORED_WEAK_PROGRESS:
        return (
            f"слабая координата на {format_duration_minutes(factor.minutes)} раньше не выбрана{_sample_suffix(factor)}"
        )
    if factor.kind == EtaFactorKind.IGNORED_LIVE_ETA:
        eta = f" {format_duration_minutes(factor.minutes)}" if factor.minutes else ""
        return f"live ETA{eta} не выбрал: {_ignored_live_eta_text(factor.scope)}"
    return ""


def _eta_factor_payload_text(payload: dict[str, object]) -> str:
    if not isinstance(payload, dict):
        return ""
    try:
        factor = EtaFactor(
            kind=EtaFactorKind(str(payload.get("kind", ""))),
            minutes=_payload_int(payload, "minutes"),
            sample_count=_payload_int(payload, "sample_count"),
            percent=_payload_int(payload, "percent"),
            scope=str(payload.get("scope") or ""),
        )
    except (TypeError, ValueError):
        return ""
    return _eta_factor_text(factor)


def _payload_int(payload: dict[str, object], key: str) -> int:
    value = payload.get(key, 0)
    if isinstance(value, bool):
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _risk_suffix(factor: EtaFactor) -> str:
    if factor.percent <= 0:
        return ""
    return f", промахи {_percent(factor)}"


def _sample_suffix(factor: EtaFactor) -> str:
    if factor.sample_count <= 0:
        return ""
    return f", {_sample_count_text(factor.sample_count)}"


def _sample_count_text(count: int) -> str:
    if count % 10 == 1 and count % 100 != 11:
        noun = "замер"
    elif count % 10 in {2, 3, 4} and count % 100 not in {12, 13, 14}:
        noun = "замера"
    else:
        noun = "замеров"
    return f"{count} {noun}"


def _scope_suffix(factor: EtaFactor) -> str:
    if not factor.scope:
        return ""
    return f", {_scope_text(factor.scope)}"


def _scope_text(scope: str) -> str:
    return eta_scope_text(scope)


def _history_disagreement_text(scope: str) -> str:
    if scope == "history_earlier":
        return "раньше"
    if scope == "history_later":
        return "позже"
    return sanitize_diagnostic_text(scope, fallback="отличается", limit=48)


def _ignored_live_eta_text(scope: str) -> str:
    if scope == "stale":
        return "данные устарели"
    if scope == "unknown_confidence":
        return "доверие неизвестно"
    if scope == "untrusted_direction":
        return "нужное направление не подтверждено"
    if scope == "untrusted_live":
        return "сигнал не прошёл проверку"
    return sanitize_diagnostic_text(scope, fallback="сигнал не прошёл проверку", limit=48)


def _percent(factor: EtaFactor) -> str:
    return f"{factor.percent}%"


def format_duration_minutes(minutes: int) -> str:
    if isinstance(minutes, bool) or not isinstance(minutes, int) or minutes < 0:
        raise ValueError("duration minutes must be a non-negative integer")
    if minutes < 60:
        return f"{minutes} мин"
    hours, rest = divmod(minutes, 60)
    if rest == 0:
        return f"{hours}ч"
    return f"{hours}ч {rest} мин"

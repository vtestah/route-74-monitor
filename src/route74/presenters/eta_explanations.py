from __future__ import annotations

from route74.diagnostics import sanitize_diagnostic_text
from route74.domain.eta import (
    EtaExplanation,
    EtaExplanationAction,
    EtaExplanationCode,
    eta_scope_text,
)


MAX_VISIBLE_ETA_EXPLANATIONS = 3


def eta_explanation_label(explanation: EtaExplanation) -> str:
    if not isinstance(explanation, EtaExplanation):
        raise ValueError("ETA explanation needs EtaExplanation")
    if explanation.code == EtaExplanationCode.LIVE_ETA:
        if explanation.detail == "next_catchable_live":
            return "выбран следующий live ETA, первый уже впритык"
        return "прямой live ETA прошёл проверку"
    if explanation.code == EtaExplanationCode.CORRECTED_LIVE:
        return _with_detail("live ETA с поправкой", explanation.detail)
    if explanation.code == EtaExplanationCode.VEHICLE_PROGRESS:
        return "ETA по координате машины, держу запас"
    if explanation.code == EtaExplanationCode.HISTORY_FALLBACK:
        return "live ETA нет, беру историю Яндекса"
    if explanation.code == EtaExplanationCode.RISK_BUFFER:
        return _with_detail("добавлен запас по риску", explanation.detail)
    if explanation.code == EtaExplanationCode.WEAK_LIVE_IGNORED:
        return _weak_live_label(explanation.detail)
    if explanation.code == EtaExplanationCode.STORAGE_GUARDRAIL:
        return "прошлые поправки недоступны, решение без них"
    if explanation.code == EtaExplanationCode.NO_ETA:
        return "точного ETA нет"
    return ""


def eta_explanation_action_label(action: EtaExplanationAction) -> str:
    if not isinstance(action, EtaExplanationAction):
        raise ValueError("ETA explanation action needs EtaExplanationAction")
    if action == EtaExplanationAction.TRUST_ETA:
        return "можно опираться на ETA"
    if action == EtaExplanationAction.KEEP_BUFFER:
        return "держу дополнительный запас"
    if action == EtaExplanationAction.CHECK_MAP:
        return "лучше сверить карту"
    if action == EtaExplanationAction.WATCH_FOR_LIVE:
        return "жду свежий live сигнал"
    if action == EtaExplanationAction.WAIT_FOR_DATA:
        return "обнови прогноз через минуту"
    return ""


def eta_explanation_payloads(
    explanations: tuple[EtaExplanation, ...],
    *,
    limit: int = MAX_VISIBLE_ETA_EXPLANATIONS,
) -> tuple[dict[str, str], ...]:
    if not isinstance(explanations, tuple):
        raise ValueError("ETA explanations need tuple")
    if limit <= 0:
        return ()
    payloads = tuple(_eta_explanation_payload(explanation) for explanation in explanations[:limit])
    if len(explanations) <= limit:
        return payloads
    return (
        *payloads,
        {
            "code": "more",
            "action": "more",
            "label": f"+{len(explanations) - limit} ещё",
            "action_label": "",
        },
    )


def eta_explanation_line(explanations: tuple[EtaExplanation, ...]) -> str:
    payloads = eta_explanation_payloads(explanations)
    labels = tuple(payload["label"] for payload in payloads if payload["label"])
    if not labels:
        return ""
    return f"🧭 Решение ETA: {'; '.join(labels)}"


def primary_eta_explanation_payload(explanations: tuple[EtaExplanation, ...]) -> dict[str, str]:
    payloads = eta_explanation_payloads(explanations, limit=1)
    if not payloads:
        return {"code": "", "action": "", "label": "", "action_label": ""}
    return payloads[0]


def _eta_explanation_payload(explanation: EtaExplanation) -> dict[str, str]:
    return {
        "code": explanation.code.value,
        "action": explanation.action.value,
        "label": eta_explanation_label(explanation),
        "action_label": eta_explanation_action_label(explanation.action),
    }


def _with_detail(prefix: str, detail: str) -> str:
    detail_text = eta_scope_text(detail)
    if not detail_text:
        return prefix
    return f"{prefix}: {detail_text}"


def _weak_live_label(detail: str) -> str:
    if detail == "vehicle_progress":
        return "слабая координата была раньше, но не выбрана"
    if detail == "stale":
        return "live ETA устарел и не выбран"
    if detail == "unknown_confidence":
        return "live ETA без доверия не выбран"
    if detail == "untrusted_direction":
        return "live ETA не подтвердил нужное направление"
    if detail == "untrusted_live":
        return "live ETA не прошёл проверку"
    fallback = sanitize_diagnostic_text(detail, fallback="live ETA не прошёл проверку", limit=48)
    return fallback

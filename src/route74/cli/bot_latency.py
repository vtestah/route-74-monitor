from __future__ import annotations

import argparse

from route74.cli.common import positive_int
from route74.cli.formatting import counts_text
from route74.domain.profiles import PROFILE_KEYS
from route74.domain.runtime_sources import BOT_EVENT_KINDS
from route74.storage import connect, init_db, summarize_bot_latency
from route74.storage.bot_latency import BotLatencySummary


def register_bot_latency_command(subparsers: argparse._SubParsersAction) -> None:
    latency = subparsers.add_parser("runtime-latency", help="Summarize web runtime response latency.")
    latency.add_argument("--hours", type=positive_int, default=24)
    latency.add_argument("--profile", choices=PROFILE_KEYS, default=None)
    latency.add_argument(
        "--event-kind",
        choices=sorted(BOT_EVENT_KINDS),
        default=None,
        help="Focus latency diagnostics on one web runtime event kind.",
    )
    latency.set_defaults(func=cmd_bot_latency)


def cmd_bot_latency(args: argparse.Namespace) -> None:
    with connect(args.db) as connection:
        init_db(connection)
        summary = summarize_bot_latency(
            connection,
            hours=args.hours,
            profile_key=args.profile,
            event_kind=args.event_kind,
        )
    print(format_bot_latency_summary(summary, args.db, event_kind=args.event_kind))


def format_bot_latency_summary(summary: BotLatencySummary, db_path: object, *, event_kind: str | None = None) -> str:
    profile_text = f" profile={summary.profile_key}" if summary.profile_key else ""
    shown_event_kind = event_kind if event_kind is not None else summary.event_kind
    event_kind_text = f" event_kind={shown_event_kind}" if shown_event_kind else ""
    return (
        f"runtime latency{profile_text}{event_kind_text} hours={summary.hours} events={summary.total_events} "
        f"invalid_durations={summary.invalid_duration_events} "
        f"latest={_datetime(summary.latest_received_at)} "
        f"errors={summary.error_events}({summary.error_rate_percent}%) "
        f"no_eta={summary.no_eta_events}({summary.no_eta_rate_percent}%) "
        f"no_eta_reasons={counts_text(summary.no_eta_reasons)} "
        f"p50_total={_ms(summary.p50_total_ms)} p95_total={_ms(summary.p95_total_ms)} "
        f"p95_forecast={_ms(summary.p95_forecast_ms)} p95_send={_ms(summary.p95_send_ms)} "
        f"p95_followup={_ms(summary.p95_render_ms)} "
        f"statuses={counts_text(summary.statuses)} updates={counts_text(summary.update_types)} "
        f"event_kinds={counts_text(summary.event_kinds)} "
        f"reply_sources={counts_text(summary.reply_sources)} methods={counts_text(summary.source_methods)} "
        f"error_categories={counts_text(summary.error_categories)} "
        f"error_reasons={counts_text(summary.error_reasons)} db={db_path}"
    )


def _ms(value: int | None) -> str:
    return "-" if value is None else f"{value}ms"


def _datetime(value: object) -> str:
    return "-" if value is None else str(value)

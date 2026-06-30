from __future__ import annotations

RUNTIME_SOURCE_NONE = ""
RUNTIME_SOURCE_WEB_APP = "web_app"

BOT_EVENT_USER_REPLY = "user_reply"
BOT_EVENT_WATCH_EARLY = "watch_early"
BOT_EVENT_WATCH_FINAL = "watch_final"
BOT_EVENT_KINDS = frozenset(
    {
        BOT_EVENT_USER_REPLY,
        BOT_EVENT_WATCH_EARLY,
        BOT_EVENT_WATCH_FINAL,
    }
)

#!/usr/bin/env python3
"""Tool definitions and executors for the voice agent.

Tools read from local JSON cache at ~/.local/share/jetson-ai/cache/.
Run data-sync.py to populate the cache.

Available tools:
  get_current_datetime — current date/time (no cache needed)
  get_calendar_events  — upcoming calendar events
  get_emails           — recent/unread inbox messages
"""

import json
import logging
from datetime import datetime, date
from pathlib import Path

log = logging.getLogger(__name__)

CACHE_DIR = Path.home() / ".local/share/jetson-ai/cache"

# ── Tool definitions (OpenAI / Ollama format) ─────────────────────────────────

TOOL_DEFS = [
    {
        "type": "function",
        "function": {
            "name": "get_current_datetime",
            "description": (
                "Get the current date and time. Use this when the user asks about "
                "today's date, the current time, or what day of the week it is."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_calendar_events",
            "description": (
                "Get upcoming calendar events. Use this when the user asks about "
                "their schedule, appointments, or what's on their calendar."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "days": {
                        "type": "integer",
                        "description": "How many days ahead to look. 1 = today only, 7 = this week.",
                        "default": 1,
                    }
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_emails",
            "description": (
                "Get recent inbox emails with importance classification. "
                "Use this when the user asks about new mail, unread messages, "
                "emails that need a reply, or important messages."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "filter": {
                        "type": "string",
                        "description": (
                            "Which emails to show: "
                            "'all' (default), 'unread', 'needs_reply', 'high_importance'"
                        ),
                        "default": "all",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max number of emails to return. Default 5.",
                        "default": 5,
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_email_stats",
            "description": (
                "Get email inbox statistics: total count, unread count, "
                "how many need a reply, high importance count, and top senders. "
                "Use this for summary questions like 'how many emails do I have?' "
                "or 'any important messages?'"
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
]

# Anthropic uses input_schema instead of parameters
TOOL_DEFS_ANTHROPIC = [
    {
        "name":         td["function"]["name"],
        "description":  td["function"]["description"],
        "input_schema": td["function"]["parameters"],
    }
    for td in TOOL_DEFS
]


# ── Executors ─────────────────────────────────────────────────────────────────

def execute_tool(name: str, arguments: dict) -> str:
    """Execute a named tool and return a plain-text result for the LLM."""
    if name == "get_current_datetime":
        return _tool_datetime()
    if name == "get_calendar_events":
        return _tool_calendar(int(arguments.get("days", 1)))
    if name == "get_emails":
        return _tool_emails(
            arguments.get("filter", "all"),
            int(arguments.get("limit", 5)),
        )
    if name == "get_email_stats":
        return _tool_email_stats()
    return f"Unknown tool: {name}"


def _load_cache(name: str) -> dict | None:
    path = CACHE_DIR / f"{name}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except Exception as e:
        log.warning("Cache read failed (%s): %s", name, e)
        return None


def _tool_datetime() -> str:
    return datetime.now().strftime("Current date and time: %A, %B %d, %Y at %I:%M %p")


def _tool_calendar(days: int = 1) -> str:
    cache = _load_cache("calendar")
    if not cache:
        return (
            "Calendar cache is empty. "
            "Ask the user to run: python3 voice/data-sync.py"
        )

    synced = cache.get("synced_at", "unknown")
    events = cache.get("events", [])
    now = datetime.now()
    upcoming = []

    for ev in events:
        try:
            start = datetime.fromisoformat(ev["start"])
            delta = (start - now).total_seconds()
            if -3600 <= delta <= days * 86400:
                upcoming.append(ev)
        except Exception:
            continue

    if not upcoming:
        label = "today" if days == 1 else f"the next {days} days"
        return f"No calendar events found for {label}. (Synced: {synced})"

    lines = [f"Upcoming calendar events (synced: {synced}):"]
    for ev in upcoming:
        try:
            s = datetime.fromisoformat(ev["start"]).strftime("%I:%M %p")
            e = datetime.fromisoformat(ev["end"]).strftime("%I:%M %p")
            time_str = f"{s} – {e}"
        except Exception:
            time_str = ev.get("start", "")
        line = f"  • {ev.get('title', 'Untitled')}: {time_str}"
        if ev.get("location"):
            line += f" at {ev['location']}"
        lines.append(line)

    return "\n".join(lines)


def _tool_emails(filter_by: str = "all", limit: int = 5) -> str:
    cache = _load_cache("email")
    if not cache:
        return "Email cache is empty. Run: python3 voice/data-sync.py"

    synced   = cache.get("synced_at", "unknown")
    messages = cache.get("messages", [])

    filter_labels = {
        "unread":          "unread emails",
        "needs_reply":     "emails that need a reply",
        "high_importance": "high-importance emails",
        "all":             "recent emails",
    }

    if filter_by == "unread":
        messages = [m for m in messages if m.get("is_unread")]
    elif filter_by == "needs_reply":
        messages = [m for m in messages if m.get("needs_reply")]
    elif filter_by == "high_importance":
        messages = [m for m in messages if m.get("importance") == "high"]

    messages = messages[:limit]
    label    = filter_labels.get(filter_by, "emails")

    if not messages:
        return f"No {label} found. (Synced: {synced})"

    lines = [f"{label.capitalize()} (synced: {synced}):"]
    for msg in messages:
        flags = []
        if msg.get("is_unread"):      flags.append("UNREAD")
        if msg.get("needs_reply"):    flags.append("REPLY NEEDED")
        if msg.get("importance") == "high": flags.append("HIGH IMPORTANCE")
        flag_str = f" [{', '.join(flags)}]" if flags else ""

        lines.append(f"  • From: {msg.get('from', 'Unknown')}{flag_str}")
        lines.append(f"    Subject: {msg.get('subject', '(no subject)')}")
        if msg.get("date"):
            lines.append(f"    Date: {msg['date']}")
        if msg.get("snippet"):
            lines.append(f"    Preview: {msg['snippet'][:200]}")

    return "\n".join(lines)


def _tool_email_stats() -> str:
    cache = _load_cache("email")
    if not cache:
        return "Email cache is empty. Run: python3 voice/data-sync.py"

    synced = cache.get("synced_at", "unknown")
    stats  = cache.get("stats", {})

    if not stats:
        # Recompute from messages if stats block missing (older cache)
        messages = cache.get("messages", [])
        stats = {
            "total":           len(messages),
            "unread":          sum(1 for m in messages if m.get("is_unread")),
            "needs_reply":     sum(1 for m in messages if m.get("needs_reply")),
            "high_importance": sum(1 for m in messages if m.get("importance") == "high"),
            "top_senders":     [],
        }

    lines = [f"Email inbox summary (synced: {synced}):"]
    lines.append(f"  • Total messages in cache: {stats.get('total', 0)}")
    lines.append(f"  • Unread: {stats.get('unread', 0)}")
    lines.append(f"  • Need a reply: {stats.get('needs_reply', 0)}")
    lines.append(f"  • High importance: {stats.get('high_importance', 0)}")

    top = stats.get("top_senders", [])
    if top:
        lines.append("  • Top senders:")
        for entry in top[:3]:
            lines.append(f"      {entry['sender']} ({entry['count']} messages)")

    return "\n".join(lines)

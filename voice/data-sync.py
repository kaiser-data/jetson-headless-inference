#!/usr/bin/env python3
"""Sync Google Calendar and IMAP email to local JSON cache for the voice agent.

Calendar backend: Google Calendar API (OAuth2 token from setup-google-auth.py).
  Falls back to CalDAV if Google token is not present and CALDAV_URL is configured.

Email backend: IMAP with heuristic importance classifier.
  Adds needs_reply / importance / stats to each sync.

Config (any of these — later overrides earlier):
  ~/.local/share/jetson-ai/sync.json  (JSON, keys in snake_case)
  <repo>/.voice_env                   (KEY=VALUE, loaded by systemd)
  Environment variables               (UPPER_CASE)

Keys:
  Google Calendar (preferred):
    GOOGLE_TOKEN_FILE   path to token JSON (default ~/.local/share/jetson-ai/google_token.json)
    GOOGLE_CALENDAR_ID  calendar ID to sync (default "primary")
    CALDAV_DAYS         days ahead to cache (default 30)

  CalDAV fallback:
    CALDAV_URL  CALDAV_USER  CALDAV_PASS  CALDAV_DAYS

  IMAP email:
    IMAP_HOST  IMAP_USER  IMAP_PASS  IMAP_PORT (default 993)
    EMAIL_LIMIT  max messages to cache (default 30)
    KNOWN_SENDERS  comma-separated important sender addresses or domains

Output:
  ~/.local/share/jetson-ai/cache/calendar.json
  ~/.local/share/jetson-ai/cache/email.json

Usage:
  python3 voice/data-sync.py           # sync both
  python3 voice/data-sync.py calendar  # calendar only
  python3 voice/data-sync.py email     # email only
"""

import email as email_lib
import imaplib
import json
import logging
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from email.header import decode_header
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger(__name__)

CACHE_DIR         = Path.home() / ".local/share/jetson-ai/cache"
CONFIG_FILE       = Path.home() / ".local/share/jetson-ai/sync.json"
VOICE_ENV         = Path(__file__).resolve().parent.parent / ".voice_env"
DEFAULT_TOKEN     = Path.home() / ".local/share/jetson-ai" / "google_token.json"
GOOGLE_SCOPES     = ["https://www.googleapis.com/auth/calendar.readonly"]

# ── Heuristics ─────────────────────────────────────────────────────────────────

# Senders that indicate bulk / automated mail → low importance
_BULK_PATTERNS = re.compile(
    r"no.?reply|noreply|do.not.reply|mailer.daemon|bounce|newsletter|"
    r"notification|automated|alert|digest|unsubscribe|mailchimp|sendgrid|"
    r"marketing|promo|info@|support@|hello@|team@|contact@",
    re.IGNORECASE,
)

# Subject tokens that raise importance
_URGENT_SUBJECT = re.compile(
    r"\b(urgent|asap|action required|action needed|deadline|follow.?up|"
    r"reminder|important|please respond|response needed|fyi:)\b",
    re.IGNORECASE,
)

# Body/subject tokens that suggest a reply is wanted
_REPLY_TOKENS = re.compile(
    r"\b(can you|could you|would you|please|let me know|what do you think|"
    r"thoughts\?|your input|get back to me|reply|respond|confirm|"
    r"do you have|are you available|when can|can we)\b",
    re.IGNORECASE,
)

# Common greeting patterns ("Hi Martin", "Dear Mr Kaiser")
_GREETING = re.compile(
    r"^(hi|hello|hey|dear|good morning|good afternoon)\b",
    re.IGNORECASE | re.MULTILINE,
)


def _classify_email(sender: str, subject: str, snippet: str, is_unread: bool,
                    known_senders: set[str]) -> dict:
    """Return {importance: high|normal|low, needs_reply: bool, reason: str}."""
    sender_l  = sender.lower()
    subject_l = subject.lower()
    combined  = f"{subject} {snippet}"

    # Low: bulk/automated sender
    if _BULK_PATTERNS.search(sender_l):
        return {"importance": "low", "needs_reply": False, "reason": "bulk_sender"}

    # High: known important sender
    for ks in known_senders:
        if ks.lower() in sender_l:
            urgency = bool(_URGENT_SUBJECT.search(subject_l))
            return {
                "importance": "high",
                "needs_reply": True,
                "reason": f"known_sender{'_urgent' if urgency else ''}",
            }

    # High: urgent subject keywords
    if _URGENT_SUBJECT.search(subject_l):
        return {"importance": "high", "needs_reply": True, "reason": "urgent_subject"}

    # Needs reply: direct questions / action requests in subject or snippet
    has_question    = "?" in combined
    has_reply_token = bool(_REPLY_TOKENS.search(combined))
    has_greeting    = bool(_GREETING.search(snippet))

    if has_question and (has_reply_token or has_greeting):
        return {"importance": "normal", "needs_reply": True, "reason": "question_with_action"}
    if has_reply_token and has_greeting:
        return {"importance": "normal", "needs_reply": True, "reason": "direct_request"}
    if has_question and is_unread:
        return {"importance": "normal", "needs_reply": True, "reason": "question_unread"}

    return {"importance": "normal", "needs_reply": False, "reason": "standard"}


def _email_stats(messages: list) -> dict:
    unread        = [m for m in messages if m.get("is_unread")]
    needs_reply   = [m for m in messages if m.get("needs_reply")]
    high_imp      = [m for m in messages if m.get("importance") == "high"]

    # Top 5 senders by message count
    from collections import Counter
    sender_counts = Counter(
        re.sub(r".*<(.+)>.*", r"\1", m.get("from", "unknown")).strip().lower()
        for m in messages
    )
    top_senders = [
        {"sender": s, "count": c}
        for s, c in sender_counts.most_common(5)
    ]

    return {
        "total":              len(messages),
        "unread":             len(unread),
        "needs_reply":        len(needs_reply),
        "high_importance":    len(high_imp),
        "top_senders":        top_senders,
    }


# ── Config ─────────────────────────────────────────────────────────────────────

def _load_config() -> dict:
    cfg: dict = {}
    if CONFIG_FILE.exists():
        try:
            cfg.update(json.loads(CONFIG_FILE.read_text()))
        except Exception as e:
            log.warning("Could not parse %s: %s", CONFIG_FILE, e)
    if VOICE_ENV.exists():
        for line in VOICE_ENV.read_text().splitlines():
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, _, v = line.partition("=")
                cfg[k.strip().lower()] = v.strip().strip('"').strip("'")
    for key in ("GOOGLE_TOKEN_FILE", "GOOGLE_CALENDAR_ID", "CALDAV_DAYS",
                "CALDAV_URL", "CALDAV_USER", "CALDAV_PASS",
                "IMAP_HOST", "IMAP_USER", "IMAP_PASS", "IMAP_PORT",
                "EMAIL_LIMIT", "KNOWN_SENDERS"):
        val = os.getenv(key)
        if val:
            cfg[key.lower()] = val
    return cfg


# ── Calendar: Google Calendar API ─────────────────────────────────────────────

def _load_google_creds(token_path: Path):
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request

    if not token_path.exists():
        return None
    creds = Credentials.from_authorized_user_file(str(token_path), GOOGLE_SCOPES)
    if creds.expired and creds.refresh_token:
        log.info("Refreshing Google token…")
        creds.refresh(Request())
        token_path.write_text(creds.to_json())
        token_path.chmod(0o600)
    return creds if creds.valid else None


def sync_calendar_google(cfg: dict) -> dict:
    token_path  = Path(cfg.get("google_token_file", str(DEFAULT_TOKEN)))
    calendar_id = cfg.get("google_calendar_id", "primary")
    days        = int(cfg.get("caldav_days", 30))

    try:
        from googleapiclient.discovery import build
        from googleapiclient.errors import HttpError
    except ImportError:
        log.error("google-api-python-client not installed: pip install google-api-python-client")
        return {"synced_at": datetime.now().isoformat(), "events": [],
                "error": "google_api_not_installed"}

    creds = _load_google_creds(token_path)
    if creds is None:
        log.warning("Google token missing or invalid — run setup-google-auth.py")
        return {"synced_at": datetime.now().isoformat(), "events": [],
                "error": "no_google_token"}

    try:
        service   = build("calendar", "v3", credentials=creds, cache_discovery=False)
        now_utc   = datetime.now(timezone.utc)
        time_min  = now_utc.isoformat()
        time_max  = (now_utc + timedelta(days=days)).isoformat()

        result = service.events().list(
            calendarId=calendar_id,
            timeMin=time_min,
            timeMax=time_max,
            singleEvents=True,
            orderBy="startTime",
            maxResults=250,
        ).execute()

        events = []
        for item in result.get("items", []):
            start_raw = item.get("start", {})
            end_raw   = item.get("end", {})

            # All-day events use "date"; timed events use "dateTime"
            start_str = start_raw.get("dateTime") or f"{start_raw.get('date')}T00:00:00"
            end_str   = end_raw.get("dateTime")   or f"{end_raw.get('date')}T00:00:00"

            # Normalise to naive local ISO
            def _norm(s: str) -> str:
                try:
                    dt = datetime.fromisoformat(s)
                    if dt.tzinfo:
                        dt = dt.astimezone().replace(tzinfo=None)
                    return dt.isoformat()
                except Exception:
                    return s

            events.append({
                "title":       item.get("summary", "Untitled"),
                "start":       _norm(start_str),
                "end":         _norm(end_str),
                "location":    item.get("location", ""),
                "description": (item.get("description") or "")[:300],
                "url":         item.get("htmlLink", ""),
                "all_day":     "date" in start_raw,
            })

        log.info("Google Calendar: synced %d events (%d days ahead)", len(events), days)
        return {
            "synced_at":   datetime.now().isoformat(),
            "events":      events,
            "calendar_id": calendar_id,
        }

    except Exception as e:
        log.error("Google Calendar sync failed: %s", e)
        return {"synced_at": datetime.now().isoformat(), "events": [], "error": str(e)}


# ── Calendar: CalDAV fallback ──────────────────────────────────────────────────

def sync_calendar_caldav(cfg: dict) -> dict:
    url  = cfg.get("caldav_url")
    user = cfg.get("caldav_user")
    pwd  = cfg.get("caldav_pass")
    days = int(cfg.get("caldav_days", 30))

    if not (url and user and pwd):
        return {"synced_at": datetime.now().isoformat(), "events": [], "error": "no_config"}

    try:
        import caldav
    except ImportError:
        return {"synced_at": datetime.now().isoformat(), "events": [],
                "error": "caldav_not_installed"}

    try:
        from datetime import date
        client    = caldav.DAVClient(url=url, username=user, password=pwd)
        principal = client.principal()
        now       = datetime.now(timezone.utc)
        end       = now + timedelta(days=days)
        events    = []

        for cal in principal.calendars():
            for ev in cal.date_search(start=now, end=end, expand=True):
                try:
                    ve      = ev.vobject_instance.vevent
                    dtstart = ve.dtstart.value
                    dtend   = getattr(ve, "dtend", ve.dtstart).value

                    def _norm(dt):
                        if isinstance(dt, date) and not isinstance(dt, datetime):
                            dt = datetime.combine(dt, datetime.min.time())
                        if getattr(dt, "tzinfo", None):
                            dt = dt.astimezone().replace(tzinfo=None)
                        return dt.isoformat()

                    events.append({
                        "title":       str(ve.summary.value) if hasattr(ve, "summary") else "Untitled",
                        "start":       _norm(dtstart),
                        "end":         _norm(dtend),
                        "location":    str(ve.location.value) if hasattr(ve, "location") else "",
                        "description": (str(ve.description.value) if hasattr(ve, "description") else "")[:300],
                    })
                except Exception as e:
                    log.debug("Skip event: %s", e)

        events.sort(key=lambda e: e["start"])
        log.info("CalDAV: synced %d events", len(events))
        return {"synced_at": datetime.now().isoformat(), "events": events}
    except Exception as e:
        log.error("CalDAV sync failed: %s", e)
        return {"synced_at": datetime.now().isoformat(), "events": [], "error": str(e)}


def sync_calendar(cfg: dict) -> dict:
    """Try Google Calendar API first, fall back to CalDAV."""
    token_path = Path(cfg.get("google_token_file", str(DEFAULT_TOKEN)))
    if token_path.exists():
        log.info("Using Google Calendar API")
        result = sync_calendar_google(cfg)
        if not result.get("error"):
            return result
        log.warning("Google Calendar failed (%s), trying CalDAV…", result["error"])
    return sync_calendar_caldav(cfg)


# ── Email ──────────────────────────────────────────────────────────────────────

def _decode_hdr(raw) -> str:
    if raw is None:
        return ""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    parts = decode_header(raw)
    out = []
    for chunk, enc in parts:
        if isinstance(chunk, bytes):
            out.append(chunk.decode(enc or "utf-8", errors="replace"))
        else:
            out.append(str(chunk))
    return " ".join(out).strip()


_HTML_TAG    = re.compile(r"<[^>]+>")
_HTML_ENTITY = re.compile(r"&(?:#\d+|#x[0-9a-fA-F]+|[a-zA-Z]+);")
_WHITESPACE  = re.compile(r"\s+")


def _extract_body(msg: email_lib.message.Message, max_chars: int = 1500) -> str:
    """Walk a parsed email message and return the best readable plaintext.

    Priority: text/plain → text/html (stripped) → empty string.
    Handles multipart, base64, and quoted-printable encoding transparently
    because get_payload(decode=True) decodes for us.
    """
    plain_parts: list[str] = []
    html_parts:  list[str] = []

    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            cd = part.get("Content-Disposition", "")
            if "attachment" in cd:
                continue
            payload = part.get_payload(decode=True)
            if not payload:
                continue
            charset = part.get_content_charset() or "utf-8"
            text    = payload.decode(charset, errors="replace")
            if ct == "text/plain":
                plain_parts.append(text)
            elif ct == "text/html":
                html_parts.append(text)
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            text    = payload.decode(charset, errors="replace")
            if msg.get_content_type() == "text/html":
                html_parts.append(text)
            else:
                plain_parts.append(text)

    if plain_parts:
        raw = " ".join(plain_parts)
    elif html_parts:
        raw = " ".join(html_parts)
        raw = _HTML_TAG.sub(" ", raw)
        raw = _HTML_ENTITY.sub(" ", raw)
    else:
        return ""

    return _WHITESPACE.sub(" ", raw).strip()[:max_chars]


def sync_email(cfg: dict) -> dict:
    host   = cfg.get("imap_host")
    user   = cfg.get("imap_user")
    pwd    = cfg.get("imap_pass")
    port   = int(cfg.get("imap_port", 993))
    limit  = int(cfg.get("email_limit", 30))
    known  = {s.strip() for s in cfg.get("known_senders", "").split(",") if s.strip()}

    if not (host and user and pwd):
        log.warning("IMAP credentials not configured — skipping email sync")
        return {"synced_at": datetime.now().isoformat(), "messages": [],
                "stats": {}, "error": "no_config"}

    try:
        conn = imaplib.IMAP4_SSL(host, port)
        conn.login(user, pwd)
        conn.select("INBOX")

        _, all_data    = conn.search(None, "ALL")
        _, unseen_data = conn.search(None, "UNSEEN")
        all_ids    = all_data[0].split()
        unread_set = set(unseen_data[0].split())
        recent_ids = all_ids[-limit:]

        messages = []
        for mid in reversed(recent_ids):
            try:
                # Fetch full message in one round trip — PEEK avoids marking as read
                _, raw = conn.fetch(mid, "(BODY.PEEK[])")
                if not raw or not raw[0]:
                    continue
                msg_bytes = raw[0][1] if isinstance(raw[0], tuple) else raw[0]
                msg       = email_lib.message_from_bytes(msg_bytes)

                sender  = _decode_hdr(msg.get("From", ""))
                subject = _decode_hdr(msg.get("Subject", ""))
                date    = msg.get("Date", "")
                body    = _extract_body(msg, max_chars=1500)

                # Snippet shown to the voice agent (shorter, conversational)
                snippet = body[:400]

                is_unread = mid in unread_set
                clf       = _classify_email(sender, subject, body, is_unread, known)

                messages.append({
                    "from":        sender[:120],
                    "subject":     subject[:200] or "(no subject)",
                    "date":        date,
                    "snippet":     snippet,
                    "is_unread":   is_unread,
                    "importance":  clf["importance"],
                    "needs_reply": clf["needs_reply"],
                    "reason":      clf["reason"],
                })
            except Exception as e:
                log.debug("Skip message %s: %s", mid, e)

        conn.logout()
        stats = _email_stats(messages)
        log.info(
            "Email: %d messages — %d unread, %d need reply, %d high importance",
            stats["total"], stats["unread"], stats["needs_reply"], stats["high_importance"],
        )
        return {
            "synced_at": datetime.now().isoformat(),
            "messages":  messages,
            "stats":     stats,
        }

    except Exception as e:
        log.error("IMAP sync failed: %s", e)
        return {"synced_at": datetime.now().isoformat(), "messages": [],
                "stats": {}, "error": str(e)}


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cfg  = _load_config()
    mode = sys.argv[1] if len(sys.argv) > 1 else "both"

    if mode in ("both", "calendar"):
        data = sync_calendar(cfg)
        (CACHE_DIR / "calendar.json").write_text(json.dumps(data, indent=2))
        if data.get("error"):
            log.warning("Calendar cache written with error: %s", data["error"])

    if mode in ("both", "email"):
        data = sync_email(cfg)
        (CACHE_DIR / "email.json").write_text(json.dumps(data, indent=2))
        if data.get("error"):
            log.warning("Email cache written with error: %s", data["error"])

    log.info("Sync complete → %s", CACHE_DIR)


if __name__ == "__main__":
    main()

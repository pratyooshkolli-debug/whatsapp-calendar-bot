"""
Invite -> calendar bot. Claude reads the invite (text, photo, PDF...) and your iPhone adds the event.

Send or forward an invite (text, photo, PDF, Word, .ics) to the bot's WhatsApp
number. Claude extracts the event details, the event is added to Google
Calendar, and the bot replies with a confirmation. Google Calendar syncs to
Apple Calendar on your iPhone.

Three ways to get invites in (use any or all; each turns on when its settings exist):
  * Meta WhatsApp Cloud API  -> WHATSAPP_* and META_APP_SECRET   (webhook: /webhook)
  * Twilio WhatsApp          -> TWILIO_* and WEBHOOK_URL         (webhook: /webhook)
  * iPhone Shortcut          -> INGEST_TOKEN                     (POST to /ingest)
"""
import base64
import hashlib
import hmac
import io
import json
import logging
import os
import html
import re
import secrets
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable
from zoneinfo import ZoneInfo

import requests
from anthropic import Anthropic
from dotenv import load_dotenv
from flask import Flask, Response, abort, has_request_context, jsonify, request
from PIL import Image
from werkzeug.middleware.proxy_fix import ProxyFix

try:  # lets Pillow open iPhone HEIC photos
    import pillow_heif

    pillow_heif.register_heif_opener()
except Exception:  # optional dependency
    pass

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("calbot")

# ---------- Config ----------
# Meta WhatsApp Cloud API
VERIFY_TOKEN = os.environ.get("WHATSAPP_VERIFY_TOKEN", "")
META_ACCESS_TOKEN = os.environ.get("WHATSAPP_ACCESS_TOKEN", "")
PHONE_NUMBER_ID = os.environ.get("WHATSAPP_PHONE_NUMBER_ID", "")
META_APP_SECRET = os.environ.get("META_APP_SECRET", "")
GRAPH_VERSION = os.environ.get("GRAPH_API_VERSION", "v25.0")
META_ENABLED = all([VERIFY_TOKEN, META_ACCESS_TOKEN, PHONE_NUMBER_ID, META_APP_SECRET])

# Twilio WhatsApp
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "")
TWILIO_WHATSAPP_FROM = os.environ.get("TWILIO_WHATSAPP_FROM", "whatsapp:+14155238886")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "")  # public URL exactly as set in Twilio
TWILIO_ENABLED = all([TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, WEBHOOK_URL])

# Calendar: iCloud (shows up on your iPhone) or Google. iCloud is used when its settings exist.
APPLE_ID = os.environ.get("APPLE_ID", "")  # your Apple ID email
ICLOUD_APP_PASSWORD = os.environ.get("ICLOUD_APP_PASSWORD", "")  # app-specific password
ICLOUD_CALENDAR = os.environ.get("ICLOUD_CALENDAR", "")  # optional: calendar name, e.g. "Home"
CALDAV_URL = os.environ.get("CALDAV_URL", "https://caldav.icloud.com/")

GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
GOOGLE_REFRESH_TOKEN = os.environ.get("GOOGLE_REFRESH_TOKEN", "")
CALENDAR_ID = os.environ.get("GOOGLE_CALENDAR_ID", "primary")

# "ics" (default): the bot hands your phone a calendar file/link and your iPhone adds it itself.
# No calendar passwords or tokens are needed. "icloud"/"google" write to the calendar server-side.
PUBLIC_URL = os.environ.get("PUBLIC_URL", "")  # e.g. https://calbot.onrender.com (auto-detected for the Shortcut)
CALENDAR_BACKEND = os.environ.get("CALENDAR_BACKEND") or (
    "icloud" if (APPLE_ID and ICLOUD_APP_PASSWORD)
    else "google" if (GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET and GOOGLE_REFRESH_TOKEN)
    else "ics"
)

TIMEZONE = os.environ.get("TIMEZONE", "Asia/Kolkata")
TZ = ZoneInfo(TIMEZONE)
DEFAULT_DURATION_MIN = int(os.environ.get("DEFAULT_DURATION_MIN", "60"))
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5-5")

# Shared secret for the iPhone Shortcut endpoint (/ingest). Leave empty to disable it.
INGEST_TOKEN = os.environ.get("INGEST_TOKEN", "")

# Only these numbers (country code, digits only, e.g. 917799944315) may use the bot.
ALLOWED_NUMBERS = {
    n.strip() for n in os.environ.get("ALLOWED_NUMBERS", "").split(",") if n.strip()
}

claude = Anthropic()  # reads ANTHROPIC_API_KEY
app = Flask(__name__)
app.json.ensure_ascii = False
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)  # correct https:// links behind Render
_seen_ids: set[str] = set()  # dedupe: providers sometimes retry webhooks

if CALENDAR_BACKEND == "icloud" and not (APPLE_ID and ICLOUD_APP_PASSWORD):
    log.warning("CALENDAR_BACKEND=icloud but APPLE_ID / ICLOUD_APP_PASSWORD are missing.")
if CALENDAR_BACKEND == "google" and not (GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET and GOOGLE_REFRESH_TOKEN):
    log.warning("CALENDAR_BACKEND=google but the GOOGLE_* settings are missing.")
if not (META_ENABLED or TWILIO_ENABLED or INGEST_TOKEN):
    log.warning("No input method is configured (Meta, Twilio or INGEST_TOKEN) - the bot can't receive anything yet.")
log.info("Enabled: meta=%s twilio=%s shortcut=%s calendar=%s",
         META_ENABLED, TWILIO_ENABLED, bool(INGEST_TOKEN), CALENDAR_BACKEND)

_twilio_client = None


def twilio_client():
    global _twilio_client
    if _twilio_client is None:
        from twilio.rest import Client

        _twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    return _twilio_client


# ---------- A provider-neutral incoming message ----------
@dataclass
class Media:
    mime: str
    filename: str
    fetch: Callable[[], bytes]


@dataclass
class Incoming:
    provider: str  # "meta" | "twilio" | "ingest"
    msg_id: str
    reply_to: str  # address to reply to, in the provider's format
    sender: str  # digits only, for the allow-list
    text: str = ""
    media: list = field(default_factory=list)
    unsupported: bool = False  # e.g. a voice note or video


# ---------- Claude: parse the invite ----------
SCHEMA_PROMPT = """Return ONLY a JSON object, no markdown, no commentary:
{
  "events": [
    {
      "title": "short event title",
      "start": "YYYY-MM-DDTHH:MM",   // local time in the timezone above
      "end": "YYYY-MM-DDTHH:MM" or null,
      "all_day": true/false,         // true if no time is given
      "location": "string or null",
      "description": "other useful details (host, dress code, RSVP, links, notes) or empty string"
    }
  ]
}

Rules:
- The input may be text, a photo/screenshot of an invite, a poster, a PDF, a ticket,
  or the contents of a calendar (.ics) or Word file. Read all visible text carefully.
- If there are several events (e.g. a multi-day programme or wedding functions), list each one.
- If there is no event at all, return {"events": []}.
- Resolve relative dates ("this Saturday", "tomorrow") from the current date.
- If no year is given, use the next upcoming occurrence of that date.
- Convert times to 24h in the JSON. If only a start time is given, set "end" to null.
- Keep addresses and meeting links in location/description.
"""


def parse_events(blocks: list) -> list[dict]:
    now = datetime.now(TZ)
    system = (
        "You extract calendar events from invites.\n"
        f"Current date/time: {now.strftime('%A, %d %B %Y, %H:%M')} in timezone {TIMEZONE}.\n\n"
        + SCHEMA_PROMPT
    )
    resp = claude.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=2000,
        system=system,
        messages=[{"role": "user", "content": blocks}],
    )
    raw = "".join(b.text for b in resp.content if b.type == "text").strip()
    raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    events = json.loads(raw).get("events", [])
    return [e for e in events if e.get("title") and e.get("start")]


# ---------- Media -> Claude content blocks ----------
HEIC_TYPES = {"image/heic", "image/heif"}
IMAGE_TYPES = {"image/jpeg", "image/jpg", "image/png", "image/webp", "image/gif"} | HEIC_TYPES
TEXT_TYPES = {"text/calendar", "text/x-vcalendar", "text/plain", "text/csv"}
MAX_IMAGE_BYTES = 3_500_000
MAX_PDF_BYTES = 30_000_000


def sniff_mime(mime: str, filename: str, data: bytes) -> str:
    """Files uploaded from the iPhone Shortcut often arrive as application/octet-stream."""
    if mime and mime != "application/octet-stream":
        return mime
    if data.startswith(b"%PDF"):
        return "application/pdf"
    if data.startswith(b"\xff\xd8"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG"):
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[4:8] == b"ftyp" and data[8:12] in (b"heic", b"heix", b"mif1", b"heif", b"hevc"):
        return "image/heic"
    if filename.endswith(".docx"):
        return "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    if filename.endswith(".ics"):
        return "text/calendar"
    return mime


def image_block(data: bytes, mime: str) -> dict:
    mime = "image/jpeg" if mime == "image/jpg" else mime
    if mime in HEIC_TYPES or len(data) > MAX_IMAGE_BYTES:  # convert HEIC / shrink big photos
        img = Image.open(io.BytesIO(data)).convert("RGB")
        img.thumbnail((2200, 2200))
        out = io.BytesIO()
        img.save(out, "JPEG", quality=85)
        data, mime = out.getvalue(), "image/jpeg"
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": mime, "data": base64.b64encode(data).decode()},
    }


def docx_text(data: bytes) -> str:
    from docx import Document

    doc = Document(io.BytesIO(data))
    parts = [p.text for p in doc.paragraphs if p.text.strip()]
    for table in doc.tables:
        for row in table.rows:
            parts.append(" | ".join(c.text.strip() for c in row.cells))
    return "\n".join(parts)


def build_blocks(inc: Incoming) -> list | str:
    """Turn an incoming message into Claude content blocks.
    Returns a list of blocks, or a string with a user-facing error message."""
    blocks: list = []
    skipped: list[str] = []

    for m in inc.media:
        mime, name = m.mime, m.filename
        data = m.fetch()
        if mime in IMAGE_TYPES:
            blocks.append(image_block(data, mime))
        elif mime == "application/pdf" or name.endswith(".pdf"):
            if len(data) > MAX_PDF_BYTES:
                return "That PDF is too large (max ~30 MB)."
            blocks.append({
                "type": "document",
                "source": {"type": "base64", "media_type": "application/pdf",
                           "data": base64.b64encode(data).decode()},
            })
        elif "wordprocessingml" in mime or name.endswith(".docx"):
            blocks.append({"type": "text", "text": "Contents of attached Word file:\n" + docx_text(data)})
        elif mime in TEXT_TYPES or name.endswith((".ics", ".txt")):
            blocks.append({"type": "text", "text": "Contents of attached file:\n" + data.decode("utf-8", "ignore")})
        else:
            skipped.append(mime or name or "unknown file type")

    if not blocks and not inc.text:
        if skipped:
            return (f"I can't read that file type ({', '.join(skipped)}). "
                    "Try a photo, PDF, Word (.docx) or calendar (.ics) file.")
        if inc.unsupported:
            return "I can't read voice notes or videos yet. Please send text, a photo, a PDF or a Word file."
        return "Please send the invite as text, a photo/screenshot, a PDF or a Word file."

    if inc.text:
        prefix = "Message that came with the file: " if blocks else ""
        blocks.append({"type": "text", "text": prefix + inc.text})

    blocks.append({"type": "text", "text": "Extract the calendar event(s) from the above."})
    return blocks


# ---------- Calendars ----------
def _times(ev: dict):
    """Return (all_day, start, end). All-day: start/end are dates (end exclusive)."""
    start = datetime.fromisoformat(ev["start"])
    if ev.get("all_day"):
        d = start.date()
        return True, d, d + timedelta(days=1)
    start = start.replace(tzinfo=TZ)
    end = datetime.fromisoformat(ev["end"]).replace(tzinfo=TZ) if ev.get("end") else None
    if end is None or end <= start:
        end = start + timedelta(minutes=DEFAULT_DURATION_MIN)
    return False, start, end


def _note(ev: dict) -> str:
    return ((ev.get("description") or "").strip() + "\n\n(Added by calendar bot)").strip()


# --- iCloud (CalDAV): events appear on your iPhone ---
_icloud_calendar = None


def _cal_name(c) -> str:
    try:
        return c.get_display_name() or ""
    except Exception:
        return getattr(c, "name", "") or ""


def icloud_calendar():
    """Connect once and pick the calendar (ICLOUD_CALENDAR by name, else the first event calendar)."""
    global _icloud_calendar
    if _icloud_calendar is None:
        import caldav

        client = caldav.DAVClient(url=CALDAV_URL, username=APPLE_ID, password=ICLOUD_APP_PASSWORD)
        calendars = client.principal().calendars()
        log.info("iCloud calendars found: %s", [_cal_name(c) for c in calendars])
        chosen = None
        if ICLOUD_CALENDAR:
            chosen = next((c for c in calendars if _cal_name(c).lower() == ICLOUD_CALENDAR.lower()), None)
            if chosen is None:
                raise RuntimeError(f"No iCloud calendar named {ICLOUD_CALENDAR!r}; found "
                                   f"{[_cal_name(c) for c in calendars]}")
        else:
            for c in calendars:
                try:
                    kinds = c.get_supported_components()
                except Exception:
                    kinds = ["VEVENT"]
                if "VEVENT" in kinds:
                    chosen = c
                    break
        if chosen is None:
            raise RuntimeError("No event calendar found in iCloud")
        log.info("Using iCloud calendar: %s", _cal_name(chosen))
        _icloud_calendar = chosen
    return _icloud_calendar


def build_calendar(events: list[dict]) -> str:
    """One .ics file containing every event (all-day and timed)."""
    from icalendar import Calendar, Event

    cal = Calendar()
    cal.add("prodid", "-//calbot//EN")
    cal.add("version", "2.0")
    for ev in events:
        all_day, start, end = _times(ev)
        e = Event()
        e.add("uid", f"{uuid.uuid4()}@calbot")
        e.add("dtstamp", datetime.now(timezone.utc))
        e.add("summary", ev["title"])
        if all_day:
            e.add("dtstart", start)
            e.add("dtend", end)
        else:
            e.add("dtstart", start.astimezone(timezone.utc))
            e.add("dtend", end.astimezone(timezone.utc))
        if ev.get("location"):
            e.add("location", ev["location"])
        e.add("description", _note(ev))
        cal.add_component(e)
    return cal.to_ical().decode()


def build_ics(ev: dict) -> str:
    return build_calendar([ev])


# Short-lived, unguessable links so the phone can open the calendar file (kept in memory only).
_store: dict[str, tuple[float, str, str]] = {}
_store_lock = threading.Lock()
STORE_TTL = 3600


def _put(content: str, mimetype: str) -> str:
    now = time.time()
    token = secrets.token_urlsafe(16)
    with _store_lock:
        for k in [k for k, (t, _, _) in _store.items() if now - t > STORE_TTL]:
            del _store[k]
        while len(_store) >= 200:
            del _store[min(_store, key=lambda k: _store[k][0])]
        _store[token] = (now, content, mimetype)
    return token


def base_url() -> str:
    if has_request_context():
        return request.url_root.rstrip("/")
    if PUBLIC_URL:
        return PUBLIC_URL.rstrip("/")
    if WEBHOOK_URL:
        return WEBHOOK_URL.rsplit("/webhook", 1)[0]
    raise RuntimeError("Set PUBLIC_URL so the bot can build calendar links")


def ics_url(events: list[dict]) -> str:
    return f"{base_url()}/ics/{_put(build_calendar(events), 'text/calendar')}.ics"


def note_url(message: str) -> str:
    page = ("<!doctype html><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<body style='font:20px -apple-system,system-ui,sans-serif;padding:2rem;white-space:pre-wrap'>"
            + html.escape(message))
    return f"{base_url()}/n/{_put(page, 'text/html')}"


def create_icloud_event(ev: dict) -> dict:
    global _icloud_calendar
    ics = build_ics(ev)
    for attempt in (1, 2):
        try:
            cal = icloud_calendar()
            saved = cal.save_event(ics)
            return {"htmlLink": str(getattr(saved, "url", "")), "calendar": _cal_name(cal)}
        except Exception:
            if attempt == 2:
                raise
            log.warning("iCloud save failed; reconnecting and retrying", exc_info=True)
            _icloud_calendar = None


# --- Google Calendar ---
def calendar_service():
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    creds = Credentials(
        token=None,
        refresh_token=GOOGLE_REFRESH_TOKEN,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        scopes=["https://www.googleapis.com/auth/calendar.events"],
    )
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def create_google_event(ev: dict) -> dict:
    all_day, start, end = _times(ev)
    body = {"summary": ev["title"], "location": ev.get("location") or None, "description": _note(ev)}
    if all_day:
        body["start"] = {"date": start.isoformat()}
        body["end"] = {"date": end.isoformat()}
    else:
        body["start"] = {"dateTime": start.isoformat(), "timeZone": TIMEZONE}
        body["end"] = {"dateTime": end.isoformat(), "timeZone": TIMEZONE}
    created = calendar_service().events().insert(calendarId=CALENDAR_ID, body=body).execute()
    return {"htmlLink": created.get("htmlLink"), "calendar": None}


def create_event(ev: dict) -> dict:
    return create_icloud_event(ev) if CALENDAR_BACKEND == "icloud" else create_google_event(ev)


# ---------- Meta WhatsApp Cloud API ----------
def _meta_headers() -> dict:
    return {"Authorization": f"Bearer {META_ACCESS_TOKEN}"}


def _meta_fetch(media_id: str) -> Callable[[], bytes]:
    def fetch() -> bytes:
        r = requests.get(f"https://graph.facebook.com/{GRAPH_VERSION}/{media_id}",
                         headers=_meta_headers(), timeout=15)
        r.raise_for_status()
        blob = requests.get(r.json()["url"], headers=_meta_headers(), timeout=60)
        blob.raise_for_status()
        return blob.content
    return fetch


def parse_meta_message(msg: dict) -> Incoming:
    inc = Incoming("meta", msg["id"], msg["from"], msg["from"])
    mtype = msg.get("type")
    if mtype == "text":
        inc.text = msg["text"]["body"]
    elif mtype in ("image", "document", "sticker"):
        p = msg[mtype]
        inc.text = p.get("caption", "") or ""
        inc.media.append(Media((p.get("mime_type") or "").split(";")[0].lower(),
                               (p.get("filename") or "").lower(), _meta_fetch(p["id"])))
    elif mtype in ("audio", "video"):
        inc.unsupported = True
    return inc


def handle_meta_payload(data: dict) -> None:
    for entry in data.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            for st in value.get("statuses", []):  # delivery reports; surface failures in the logs
                if st.get("status") == "failed":
                    log.error("WhatsApp delivery failed: %s", st.get("errors"))
            for msg in value.get("messages", []):
                handle_message(parse_meta_message(msg))


# ---------- Twilio WhatsApp ----------
def _twilio_fetch(url: str) -> Callable[[], bytes]:
    def fetch() -> bytes:
        r = requests.get(url, auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN), timeout=60)
        r.raise_for_status()
        return r.content
    return fetch


def parse_twilio_form(form: dict) -> Incoming:
    sender = form.get("From", "")  # e.g. whatsapp:+917799944315
    inc = Incoming("twilio", form.get("MessageSid", ""), sender, re.sub(r"\D", "", sender),
                   (form.get("Body") or "").strip())
    for i in range(int(form.get("NumMedia", "0") or 0)):
        mime = (form.get(f"MediaContentType{i}") or "").split(";")[0].strip().lower()
        if mime.startswith(("audio/", "video/")):
            inc.unsupported = True
            continue
        inc.media.append(Media(mime, "", _twilio_fetch(form.get(f"MediaUrl{i}"))))
    return inc


# ---------- Replying ----------
def send_reply(inc: Incoming, text: str) -> None:
    try:
        if inc.provider == "meta":
            r = requests.post(
                f"https://graph.facebook.com/{GRAPH_VERSION}/{PHONE_NUMBER_ID}/messages",
                headers=_meta_headers(),
                json={"messaging_product": "whatsapp", "to": inc.reply_to, "type": "text",
                      "text": {"body": text}},
                timeout=15,
            )
            if not r.ok:
                log.error("WhatsApp send failed: %s %s", r.status_code, r.text)
        else:
            twilio_client().messages.create(from_=TWILIO_WHATSAPP_FROM, to=inc.reply_to, body=text[:1500])
    except Exception:
        log.exception("Failed to send reply")


def event_json(ev: dict) -> dict:
    """One event as plain text fields for the iPhone Shortcut's 'Add New Event' action.
    All-day events become 12:00 AM - 11:59 PM so they work with the same simple Shortcut."""
    all_day, start, end = _times(ev)
    if all_day:
        start = datetime(start.year, start.month, start.day, tzinfo=TZ)
        end = start.replace(hour=23, minute=59)
    return {
        "title": ev["title"],
        "start": start.isoformat(timespec="seconds"),  # e.g. 2026-11-08T20:00:00+05:30
        "end": end.isoformat(timespec="seconds"),
        "start_text": start.strftime("%d %b %Y %I:%M %p"),  # fallback if the ISO form isn't understood
        "end_text": end.strftime("%d %b %Y %I:%M %p"),
        "location": ev.get("location") or "",
        "notes": (ev.get("description") or "").strip(),
        "all_day": "yes" if all_day else "no",
    }


def format_one(ev: dict) -> str:
    start = datetime.fromisoformat(ev["start"])
    if ev.get("all_day"):
        when = start.strftime("%a %d %b %Y (all day)")
    else:
        when = start.strftime("%a %d %b %Y, %I:%M %p").replace(" 0", " ")
    lines = [f"✅ {ev['title']}", f"🗓 {when}"]
    if ev.get("location"):
        lines.append(f"📍 {ev['location']}")
    return "\n".join(lines)


def process(inc: Incoming, link: bool = True) -> dict:
    """Read the invite and either add the event(s) to a calendar (icloud/google) or, in the default
    'ics' mode, give the phone what it needs to add them (a calendar link, or the raw events).
    Returns {"message": text, "url": link or None, "events": [...]}.
    Inputs we can't use produce a friendly message instead of an exception."""
    blocks = build_blocks(inc)
    if isinstance(blocks, str):
        return {"message": blocks, "url": None, "events": []}

    events = parse_events(blocks)
    if not events:
        return {"message": "I couldn't find an event in that message.", "url": None, "events": []}

    summaries = "\n\n".join(format_one(ev) for ev in events)
    if CALENDAR_BACKEND == "ics":
        if link:
            return {"message": "Ready to add to your calendar:\n\n" + summaries,
                    "url": ics_url(events), "events": events}
        return {"message": "Added to your calendar:\n\n" + summaries, "url": None, "events": events}

    where = None
    for ev in events:
        created = create_event(ev)
        where = created.get("calendar") or where
        log.info("Created event %s", created.get("htmlLink"))
    header = f"Added to your {where} calendar:" if where else "Added to your calendar:"
    return {"message": header + "\n\n" + summaries, "url": None, "events": events}


def reply_text(res: dict) -> str:
    return res["message"] + (f"\n\nTap to add to your calendar:\n{res['url']}" if res["url"] else "")


def handle_message(inc: Incoming) -> None:
    if inc.msg_id in _seen_ids:
        return
    _seen_ids.add(inc.msg_id)
    if len(_seen_ids) > 5000:
        _seen_ids.clear()

    if ALLOWED_NUMBERS and inc.sender not in ALLOWED_NUMBERS:
        log.warning("Ignoring message from unauthorised number %s", inc.sender)
        return

    try:
        send_reply(inc, reply_text(process(inc)))
    except Exception:
        log.exception("Failed to process message")
        send_reply(inc, "⚠️ Sorry, something went wrong adding that event.")


# ---------- Webhook endpoints ----------
@app.get("/webhook")
def verify():
    """Meta's one-time webhook verification handshake."""
    if (
        META_ENABLED
        and request.args.get("hub.mode") == "subscribe"
        and hmac.compare_digest(request.args.get("hub.verify_token", ""), VERIFY_TOKEN)
    ):
        return request.args.get("hub.challenge", ""), 200
    abort(403)


@app.post("/webhook")
def receive():
    # --- Meta: JSON body signed with the app secret ---
    if request.headers.get("X-Hub-Signature-256") is not None:
        if not META_ENABLED:
            abort(403)
        expected = "sha256=" + hmac.new(META_APP_SECRET.encode(), request.get_data(),
                                        hashlib.sha256).hexdigest()
        if not hmac.compare_digest(request.headers["X-Hub-Signature-256"], expected):
            abort(403)
        # Respond fast; process in the background so Meta doesn't retry
        threading.Thread(target=handle_meta_payload, args=(request.get_json(silent=True) or {},),
                         daemon=True).start()
        return "ok", 200

    # --- Twilio: form body signed with the auth token ---
    if request.headers.get("X-Twilio-Signature") is not None:
        if not TWILIO_ENABLED:
            abort(403)
        from twilio.request_validator import RequestValidator

        form = request.form.to_dict()
        if not RequestValidator(TWILIO_AUTH_TOKEN).validate(
                WEBHOOK_URL, form, request.headers["X-Twilio-Signature"]):
            abort(403)
        threading.Thread(target=lambda: handle_message(parse_twilio_form(form)), daemon=True).start()
        return Response("<Response></Response>", mimetype="text/xml")

    abort(403)


@app.post("/ingest")
def ingest():
    """Endpoint for the iPhone Shortcut: POST a `text` field and/or file uploads (photo, PDF, ...).
    With ?format=events the reply is JSON ({"message", "events": [...]}) for the zero-tap Shortcut.
    In the default 'ics' mode otherwise the reply is a single URL for the phone to open: a calendar file
    (iOS offers to add it) or, if something went wrong, a page explaining why."""
    if not INGEST_TOKEN:
        abort(404)
    if not hmac.compare_digest(request.headers.get("Authorization", ""), f"Bearer {INGEST_TOKEN}"):
        abort(403)

    inc = Incoming("ingest", "", "", "ingest")
    if request.is_json:
        inc.text = str((request.get_json(silent=True) or {}).get("text", "")).strip()
    else:
        inc.text = (request.form.get("text") or "").strip()
    for _, f in request.files.items(multi=True):
        data = f.read()
        name = (f.filename or "").lower()
        inc.media.append(Media(sniff_mime((f.mimetype or "").lower(), name, data), name, lambda d=data: d))
    if not inc.text and not inc.media and not request.is_json:
        inc.text = request.get_data(as_text=True).strip()  # raw text body

    as_events = request.args.get("format") == "events"  # zero-tap Shortcut: JSON in, 'Add New Event' out
    try:
        res = process(inc, link=not as_events)
    except Exception:
        log.exception("Ingest failed")
        res = {"message": "⚠️ Sorry, something went wrong reading that invite. Please try again.",
               "url": None, "events": []}
        if CALENDAR_BACKEND != "ics" and not as_events:
            return Response(res["message"], status=500, mimetype="text/plain")

    if as_events:
        return jsonify({"message": res["message"], "events": [event_json(e) for e in res["events"]]})

    if CALENDAR_BACKEND == "ics":
        return Response(res["url"] or note_url(res["message"]), mimetype="text/plain")
    return Response(res["message"], mimetype="text/plain")


@app.get("/ics/<token>.ics")
def serve_ics(token: str):
    return _serve(token, "text/calendar", 'inline; filename="invite.ics"')


@app.get("/n/<token>")
def serve_note(token: str):
    return _serve(token, "text/html", "inline")


def _serve(token: str, mimetype: str, disposition: str):
    item = _store.get(token)
    if not item or item[2] != mimetype or time.time() - item[0] > STORE_TTL:
        abort(404)
    return Response(item[1], mimetype=mimetype,
                    headers={"Content-Disposition": disposition, "Cache-Control": "no-store"})


@app.get("/")
def health():
    return (f"Calendar bot is running (meta={META_ENABLED}, twilio={TWILIO_ENABLED}, "
            f"shortcut={bool(INGEST_TOKEN)}, calendar={CALENDAR_BACKEND})"), 200


if __name__ == "__main__":
    app.run(port=int(os.environ.get("PORT", 5000)))

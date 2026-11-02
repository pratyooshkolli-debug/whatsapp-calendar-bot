"""
WhatsApp -> Google Calendar bot (Meta Cloud API + Claude for parsing).

Send or forward an invite to the bot's WhatsApp number. It extracts the event
details with Claude, adds the event to Google Calendar, and replies with a
confirmation. Google Calendar syncs to Apple Calendar on your iPhone.
"""
import base64
import hashlib
import hmac
import io
import json
import logging
import os
import threading
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
from anthropic import Anthropic
from dotenv import load_dotenv
from flask import Flask, abort, request
from PIL import Image
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("calbot")

# ---------- Config ----------
VERIFY_TOKEN = os.environ["WHATSAPP_VERIFY_TOKEN"]  # any string you invent
WHATSAPP_TOKEN = os.environ["WHATSAPP_ACCESS_TOKEN"]
PHONE_NUMBER_ID = os.environ["WHATSAPP_PHONE_NUMBER_ID"]
APP_SECRET = os.environ["META_APP_SECRET"]

GOOGLE_CLIENT_ID = os.environ["GOOGLE_CLIENT_ID"]
GOOGLE_CLIENT_SECRET = os.environ["GOOGLE_CLIENT_SECRET"]
GOOGLE_REFRESH_TOKEN = os.environ["GOOGLE_REFRESH_TOKEN"]
CALENDAR_ID = os.environ.get("GOOGLE_CALENDAR_ID", "primary")

TIMEZONE = os.environ.get("TIMEZONE", "Asia/Kolkata")
TZ = ZoneInfo(TIMEZONE)
DEFAULT_DURATION_MIN = int(os.environ.get("DEFAULT_DURATION_MIN", "60"))
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5-5")

# Only these numbers (country code, digits only, e.g. 919876543210) may use the bot.
ALLOWED_NUMBERS = {
    n.strip() for n in os.environ.get("ALLOWED_NUMBERS", "").split(",") if n.strip()
}

claude = Anthropic()  # reads ANTHROPIC_API_KEY
app = Flask(__name__)
_seen_ids: set[str] = set()  # dedupe: Meta sometimes retries webhooks


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


# ---------- Media handling ----------
IMAGE_TYPES = {"image/jpeg", "image/jpg", "image/png", "image/webp", "image/gif"}
MAX_IMAGE_BYTES = 3_500_000
MAX_PDF_BYTES = 30_000_000


def download_media(media_id: str) -> tuple[bytes, str]:
    """Download a file that was sent to the bot. Returns (bytes, mime_type)."""
    auth = {"Authorization": f"Bearer {WHATSAPP_TOKEN}"}
    meta = requests.get(f"https://graph.facebook.com/v21.0/{media_id}", headers=auth, timeout=15)
    meta.raise_for_status()
    info = meta.json()
    blob = requests.get(info["url"], headers=auth, timeout=60)
    blob.raise_for_status()
    return blob.content, (info.get("mime_type") or "").split(";")[0].strip().lower()


def image_block(data: bytes, mime: str) -> dict:
    mime = "image/jpeg" if mime == "image/jpg" else mime
    if len(data) > MAX_IMAGE_BYTES:  # shrink big photos so the API accepts them
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


def build_blocks(msg: dict) -> list | str:
    """Turn a WhatsApp message into Claude content blocks.
    Returns a list of blocks, or a string with a user-facing error message."""
    mtype = msg.get("type")

    if mtype == "text":
        return [{"type": "text", "text": msg["text"]["body"]}]

    if mtype in ("image", "document"):
        payload = msg[mtype]
        caption = payload.get("caption", "")
        filename = (payload.get("filename") or "").lower()
        data, mime = download_media(payload["id"])
        blocks: list = []

        if mime in IMAGE_TYPES:
            blocks.append(image_block(data, mime))
        elif mime == "application/pdf" or filename.endswith(".pdf"):
            if len(data) > MAX_PDF_BYTES:
                return "That PDF is too large (max ~30 MB)."
            blocks.append({
                "type": "document",
                "source": {"type": "base64", "media_type": "application/pdf",
                           "data": base64.b64encode(data).decode()},
            })
        elif filename.endswith(".docx") or "wordprocessingml" in mime:
            blocks.append({"type": "text", "text": "Contents of attached Word file:\n" + docx_text(data)})
        elif filename.endswith(".ics") or mime in ("text/calendar", "text/plain", "text/csv"):
            blocks.append({"type": "text", "text": "Contents of attached file:\n" + data.decode("utf-8", "ignore")})
        else:
            return (f"I can't read that file type ({mime or filename or 'unknown'}). "
                    "Try a photo, PDF, Word (.docx) or calendar (.ics) file.")

        if caption:
            blocks.append({"type": "text", "text": f"Message that came with the file: {caption}"})
        blocks.append({"type": "text", "text": "Extract the calendar event(s) from the above."})
        return blocks

    if mtype in ("audio", "video"):
        return "I can't read voice notes or videos yet. Please send text, a photo, a PDF or a Word file."

    return "Please send the invite as text, a photo/screenshot, a PDF or a Word file."


# ---------- Google Calendar ----------
def calendar_service():
    creds = Credentials(
        token=None,
        refresh_token=GOOGLE_REFRESH_TOKEN,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        scopes=["https://www.googleapis.com/auth/calendar.events"],
    )
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def create_event(ev: dict) -> dict:
    body = {
        "summary": ev["title"],
        "location": ev.get("location") or None,
        "description": (ev.get("description") or "") + "\n\n(Added via WhatsApp bot)",
    }
    start = datetime.fromisoformat(ev["start"])

    if ev.get("all_day"):
        body["start"] = {"date": start.date().isoformat()}
        body["end"] = {"date": (start.date() + timedelta(days=1)).isoformat()}
    else:
        start = start.replace(tzinfo=TZ)
        end = (
            datetime.fromisoformat(ev["end"]).replace(tzinfo=TZ)
            if ev.get("end")
            else start + timedelta(minutes=DEFAULT_DURATION_MIN)
        )
        body["start"] = {"dateTime": start.isoformat(), "timeZone": TIMEZONE}
        body["end"] = {"dateTime": end.isoformat(), "timeZone": TIMEZONE}

    return calendar_service().events().insert(calendarId=CALENDAR_ID, body=body).execute()


# ---------- WhatsApp ----------
def send_whatsapp(to: str, text: str) -> None:
    r = requests.post(
        f"https://graph.facebook.com/v21.0/{PHONE_NUMBER_ID}/messages",
        headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}"},
        json={"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": text}},
        timeout=15,
    )
    if not r.ok:
        log.error("WhatsApp send failed: %s %s", r.status_code, r.text)


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


def handle_message(msg: dict) -> None:
    sender = msg["from"]
    msg_id = msg["id"]
    if msg_id in _seen_ids:
        return
    _seen_ids.add(msg_id)
    if len(_seen_ids) > 5000:
        _seen_ids.clear()

    if ALLOWED_NUMBERS and sender not in ALLOWED_NUMBERS:
        log.warning("Ignoring message from unauthorised number %s", sender)
        return

    try:
        blocks = build_blocks(msg)
        if isinstance(blocks, str):  # unsupported input -> friendly message
            send_whatsapp(sender, blocks)
            return

        events = parse_events(blocks)
        if not events:
            send_whatsapp(sender, "I couldn't find an event in that message.")
            return

        confirmations = []
        for ev in events:
            created = create_event(ev)
            log.info("Created event %s", created.get("htmlLink"))
            confirmations.append(format_one(ev))

        header = "Added to your calendar:\n\n" if len(events) > 1 else "Added to your calendar:\n"
        send_whatsapp(sender, header + "\n\n".join(confirmations))
    except Exception:
        log.exception("Failed to process message")
        send_whatsapp(sender, "⚠️ Sorry, something went wrong adding that event.")


def handle_payload(data: dict) -> None:
    for entry in data.get("entry", []):
        for change in entry.get("changes", []):
            for msg in change.get("value", {}).get("messages", []):
                handle_message(msg)


# ---------- Webhook endpoints ----------
@app.get("/webhook")
def verify():
    if (
        request.args.get("hub.mode") == "subscribe"
        and request.args.get("hub.verify_token") == VERIFY_TOKEN
    ):
        return request.args.get("hub.challenge", ""), 200
    abort(403)


@app.post("/webhook")
def receive():
    # Verify the request really came from Meta
    sig = request.headers.get("X-Hub-Signature-256", "")
    expected = "sha256=" + hmac.new(APP_SECRET.encode(), request.get_data(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        abort(403)

    # Respond fast; process in the background so Meta doesn't retry
    threading.Thread(target=handle_payload, args=(request.get_json(),), daemon=True).start()
    return "ok", 200


@app.get("/")
def health():
    return "WhatsApp calendar bot is running", 200


if __name__ == "__main__":
    app.run(port=int(os.environ.get("PORT", 5000)))

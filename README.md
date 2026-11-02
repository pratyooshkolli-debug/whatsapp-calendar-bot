# Invite → iPhone Calendar

Send or forward an invite to the bot's WhatsApp number → Claude extracts the
details → your iPhone adds the event to Calendar. **No calendar passwords or tokens are stored anywhere.**

Inputs: text, photos/screenshots, PDFs (including scanned), Word `.docx`, `.ics`
and `.txt` files. An invite with several events creates several entries.

Three ways to get invites in; use any or all (each turns on when its settings are filled in):
- **iPhone Shortcut** (no WhatsApp provider, no business verification, no expiry): see the last section.
- **Meta WhatsApp Cloud API** (needs a verified business portfolio).
- **Twilio WhatsApp** (sandbox for testing; 30-day trial).

## 1. How the calendar part works (default: no credentials)
By default the server only *reads* the invite. It answers with a short-lived link to a calendar file (`.ics`)
and your iPhone's own Calendar adds the event after one tap on **Add All**. Nothing about your calendar is stored on the server.
Links expire after an hour and are kept in memory only.

Optional, if you ever want events written server-side with no tap (this needs credentials on the server):
- **iCloud:** set `APPLE_ID` and `ICLOUD_APP_PASSWORD` (an app-specific password from appleid.apple.com; it can reach
  iCloud mail, contacts and calendars), optionally `ICLOUD_CALENDAR`.
- **Google:** set `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_REFRESH_TOKEN` (from `python3 get_google_token.py`).
Force one with `CALENDAR_BACKEND=ics|icloud|google`.

## 2. Meta WhatsApp Cloud API (permanent route)
1. developers.facebook.com → create an app with the **Connect with customers through WhatsApp** use case
   (the app name can't contain "WhatsApp").
2. Use case → Customize → **Step 1. Try it out**: copy the **Phone number ID**, add your own number as a recipient.
3. **App settings → Basic**: copy the **App secret** → `META_APP_SECRET`.
4. **Permanent token**: business.facebook.com → Business settings → Users → **System users** → add one (Admin)
   → Add assets → your app (Full control) → Generate token, expiry **Never**, permissions
   `whatsapp_business_messaging` + `whatsapp_business_management` → `WHATSAPP_ACCESS_TOKEN`.
5. If messages fail with error **131031 (Business Account locked)**, Meta has frozen the account:
   check Business Support Home for the reason, request a review, or open a Meta Direct Support ticket with
   your WABA ID and the error code. The bot logs failed deliveries so you can see the error.

## 3. Deploy (Render)
1. Push this folder to GitHub (`.env` and `client_secret.json` are git-ignored).
2. Render → New → Web Service → pick the repo.
   - Build: `pip install -r requirements.txt`
   - Start: `gunicorn app:app --workers 1 --threads 4`
3. Add the environment variables from `.env.example` (only the provider you use, plus Google and `ANTHROPIC_API_KEY`).
   Set `ALLOWED_NUMBERS` to your number, digits only (e.g. `917799944315`).
4. Deploy and wait for **Live**. Open the Render URL; it should say "bot is running (meta=True…)".

## 4. Connect the webhook
**Meta:** App → WhatsApp → Configuration → Webhook
- Callback URL: `https://YOUR-SERVICE.onrender.com/webhook`
- Verify token: same as `WHATSAPP_VERIFY_TOKEN`
- Verify and save, then subscribe to **messages**.

**Twilio (backup):** Messaging → Settings → WhatsApp sandbox settings → "When a message comes in" =
`https://YOUR-SERVICE.onrender.com/webhook` (POST), and set `WEBHOOK_URL` to the same value.
Join the sandbox from your phone with `join your-code`; membership lapses after ~3 days.
Twilio trial accounts last 30 days; after that, upgrade (about $0.005 per message in or out).

## 5. Use it
Message the bot's number (save it as "Calendar Bot") with text, a photo or a PDF of an invite.
Reply example: `Added to your calendar: ✅ Dinner 🗓 Sat 8 Nov 2026, 8:00 PM 📍 Taj Falaknuma`


## Notes
- Only numbers in `ALLOWED_NUMBERS` can use the bot. Requests are verified with Meta's/Twilio's signature.
- Keep `--workers 1` so the duplicate-message check works.
- Free Render instances sleep when idle; the first message after a quiet spell can be slow.
- The Meta test number can message up to 5 recipients, which is plenty for personal use.

## iPhone Shortcut (no Meta, Twilio or calendar login needed)
You stay in WhatsApp but *share* the invite instead of sending it to a bot.

Server (Render): set only `ANTHROPIC_API_KEY`, `TIMEZONE=Asia/Kolkata` and `INGEST_TOKEN` (a long random string:
`python3 -c "import secrets; print(secrets.token_urlsafe(32))"`). Leave all calendar settings empty.

### A. Zero-tap Shortcut (the iPhone's own "Add New Event" action creates the event)
The server answers `/ingest?format=events` with JSON: `{"message": "...", "events": [{"title", "start", "end", "location", "notes", ...}]}`.
Times are ISO 8601 with offset (e.g. `2026-11-08T20:00:00+05:30`); `start_text`/`end_text` are plain-English fallbacks.
All-day invites come through as 12:00 AM - 11:59 PM.

Build "Add to Calendar" in the Shortcuts app:
1. Name it; (i) → **Show in Share Sheet**; Share Sheet Types: **Images**, **PDFs**.
2. **If** `Shortcut Input` `has any value` → **Get Contents of URL** (`https://YOUR-SERVICE.onrender.com/ingest?format=events`,
   POST, header `Authorization: Bearer YOUR_TOKEN`, Request Body **Form**, field type **File**, key `file`, value `Shortcut Input`).
3. **Otherwise** → **Get Clipboard** → **Get Contents of URL** (same URL and header, Form, field type **Text**, key `text`, value `Clipboard`). **End If**.
4. **Set Variable** `Response`.
5. **Get Dictionary Value** (key `events`, from `Response`) → **Repeat with Each**.
6. Inside the repeat, four pairs of **Get Dictionary Value** (from `Repeat Item`) + **Set Variable**:
   `title` → `Title`, `start` → `Start`, `end` → `End`, `location` → `Place`.
7. **Add New Event**: Title = `Title`, Start Date = `Start`, End Date = `End`, Location = `Place` (Show More to pick the calendar). **End Repeat**.
8. **Get Dictionary Value** (key `message`, from `Response`) → **Show Notification**.
Optional: add `notes` → `Notes` and put it in Add New Event's Notes field.
If a time comes out wrong, use `start_text` / `end_text` instead of `start` / `end`.
The first run asks to allow calendar access and sending data to your server; choose Allow.

### B. One-tap Shortcut (simpler; handles many events at once)
Same as A through step 3, but call `/ingest` (no `?format=events`) and finish with **Open URLs**. Safari then shows
the calendar file and you tap **Add All**.

Use it: open a photo or PDF invite in WhatsApp → Share → **Add to Calendar**.
For a text invite: long-press the message → **Copy** → run the shortcut (e.g. Back Tap double-tap).

# WhatsApp → Google Calendar bot

Send or forward an invite to the bot's WhatsApp number → Claude extracts the
details → event is added to Google Calendar → synced to Apple Calendar.

## 1. Google Calendar (about 10 min)
1. https://console.cloud.google.com → create a project → **APIs & Services → Library** → enable **Google Calendar API**.
2. **OAuth consent screen**: External, add yourself as a test user.
   **Then click "Publish app" (In production).** If you leave it in "Testing", the refresh token expires after 7 days. An unverified personal app is fine.
3. **Credentials → Create credentials → OAuth client ID → Desktop app**. Download the JSON, save it here as `client_secret.json`.
4. Run:
   ```
   pip install -r requirements.txt
   python get_google_token.py
   ```
   Copy the three printed values into your `.env`.

## 2. Meta WhatsApp Cloud API
1. https://developers.facebook.com → **Create App** → type **Business** → add the **WhatsApp** product.
2. In **WhatsApp → API Setup** you get a free **test number** and a **Phone number ID**. Add your own number as a recipient and verify it with the code.
3. **App settings → Basic** → copy the **App Secret** → `META_APP_SECRET`.
4. **Permanent token** (the one on the API Setup page expires in 24h):
   Business Settings → Users → **System Users** → add one (Admin) → assign your app → **Generate token** with `whatsapp_business_messaging` and `whatsapp_business_management`, no expiry → `WHATSAPP_ACCESS_TOKEN`.

## 3. Deploy (Render example)
1. Push this folder to a GitHub repo (`.env` is git-ignored).
2. Render → **New Web Service** → pick the repo.
   - Build: `pip install -r requirements.txt`
   - Start: `gunicorn app:app --workers 1 --threads 4`
3. Add every variable from `.env.example` under **Environment**, plus `ANTHROPIC_API_KEY`.
4. Deploy and note the URL, e.g. `https://calbot.onrender.com`.

Free Render instances sleep when idle, so the first message can be slow and Meta may retry. Railway/Fly.io, or Render's paid tier, avoid this.

## 4. Connect the webhook
Meta app → **WhatsApp → Configuration → Webhook**:
- Callback URL: `https://YOUR-URL/webhook`
- Verify token: the same value as `WHATSAPP_VERIFY_TOKEN`
- Click **Verify and save**, then **subscribe to the `messages` field**.

## 5. Use it
Message the test number from your phone. Paste text, or send a photo, screenshot or PDF of the invite:
> "Dinner at Taj Falaknuma this Saturday 8pm, bring Priya!"

Reply: `✅ Added: Dinner…  🗓 Sat 8 Nov 2026, 8:00 PM  📍 Taj Falaknuma`

## 6. Apple Calendar
iPhone → Settings → Calendar → Accounts → Add Account → Google → sign in → turn on **Calendars**.

## Local testing
```
cp .env.example .env   # fill in
python app.py
ngrok http 5000        # use the ngrok URL as the webhook callback
```

## Notes
- Only numbers in `ALLOWED_NUMBERS` can use the bot. Don't leave it empty on a public URL.
- Requests are verified with Meta's signature, so random POSTs are rejected.
- Keep `--workers 1` so the duplicate-message check works.
- Supported inputs: text, photos/screenshots (JPG, PNG, WebP), PDFs (including scanned ones), Word `.docx`, calendar `.ics` and `.txt` files. A caption sent with a file is used as extra context.
- One invite with several events (e.g. a wedding programme) creates one calendar entry per event.
- Not supported yet: voice notes, videos, old `.doc`, Excel and PowerPoint files. The bot tells you if you send one.
- Send files as normal attachments. WhatsApp's "Document" attach option works for PDFs and for full-quality images.

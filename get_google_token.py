"""
Run this ONCE on your laptop to get a Google refresh token.

1. Put your OAuth "Desktop app" client file next to this script as client_secret.json
2. python get_google_token.py
3. Sign in in the browser, approve access
4. Copy the printed values into your .env / hosting environment variables
"""
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/calendar.events"]

flow = InstalledAppFlow.from_client_secrets_file("client_secret.json", SCOPES)
creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")

print("\n--- Add these to your environment ---")
print(f"GOOGLE_CLIENT_ID={creds.client_id}")
print(f"GOOGLE_CLIENT_SECRET={creds.client_secret}")
print(f"GOOGLE_REFRESH_TOKEN={creds.refresh_token}")

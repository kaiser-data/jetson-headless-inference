#!/usr/bin/env python3
"""One-time Google OAuth device-flow authentication.

Run this once on any machine (Jetson headless is fine — it prints a URL
you open on your phone or laptop).  The resulting token is saved to:
  ~/.local/share/jetson-ai/google_token.json

After that, data-sync.py reads and auto-refreshes the token silently.

Requirements:
  pip install google-auth google-auth-oauthlib google-api-python-client

You also need a Google Cloud project with the Calendar API enabled:
  1. Go to https://console.cloud.google.com/
  2. Create a project (or reuse one)
  3. Enable "Google Calendar API"
  4. Go to APIs & Services → Credentials → Create → OAuth client ID
     Type: Desktop app (or "TV and Limited Input" for pure device flow)
  5. Download the JSON → save as ~/gamma4_models/.google_credentials.json

Then run:
  python3 voice/setup-google-auth.py
"""

import json
import sys
from pathlib import Path

CREDS_FILE = Path.home() / "gamma4_models" / ".google_credentials.json"
TOKEN_FILE = Path.home() / ".local/share/jetson-ai" / "google_token.json"
SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]


def main():
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
        from google.oauth2.credentials import Credentials
    except ImportError:
        print("Missing packages. Run:")
        print("  pip install google-auth google-auth-oauthlib google-api-python-client")
        sys.exit(1)

    if not CREDS_FILE.exists():
        print(f"Credentials file not found: {CREDS_FILE}")
        print()
        print("Steps to create one:")
        print("  1. https://console.cloud.google.com/ → select your project")
        print("  2. APIs & Services → Credentials → + Create Credentials → OAuth client ID")
        print("  3. Application type: Desktop app")
        print("  4. Download JSON → save as:", CREDS_FILE)
        sys.exit(1)

    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)

    # InstalledAppFlow with out_of_band (OOB) for headless / device flow
    flow = InstalledAppFlow.from_client_secrets_file(str(CREDS_FILE), SCOPES)

    print("Opening OAuth flow...")
    print("Log in with the Google account that owns the calendar you want to sync.")
    print()

    try:
        # Try local server first (works if you have SSH port-forwarding or are local)
        creds = flow.run_local_server(port=0, open_browser=False)
        print("Auth via local callback succeeded.")
    except Exception:
        # Fallback: manual copy-paste (true headless)
        auth_url, _ = flow.authorization_url(access_type="offline", prompt="consent")
        print("Open this URL in any browser (phone, laptop — doesn't need to be this machine):")
        print()
        print(f"  {auth_url}")
        print()
        code = input("Paste the authorisation code here: ").strip()
        flow.fetch_token(code=code)
        creds = flow.credentials

    TOKEN_FILE.write_text(creds.to_json())
    print()
    print(f"Token saved to: {TOKEN_FILE}")
    print("Run 'python3 voice/data-sync.py calendar' to test.")


if __name__ == "__main__":
    main()

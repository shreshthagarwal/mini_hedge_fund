"""
upstox_auth.py — get Upstox access token via OAuth2

Run once each morning before fetching data or trading:
    python scripts/upstox_auth.py

Flow:
  1. Opens browser to Upstox login
  2. Local server catches the redirect with auth code
  3. Exchanges code for access token
  4. Saves token to .upstox_token

Token is valid for 1 day. Re-run each morning.

Set credentials first:
    $env:UPSTOX_API_KEY    = "your_api_key"
    $env:UPSTOX_API_SECRET = "your_api_secret"
"""

import os
import sys
import json
import webbrowser
import requests
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

API_KEY    = os.getenv("UPSTOX_API_KEY",    "").strip()
API_SECRET = os.getenv("UPSTOX_API_SECRET", "").strip()
REDIRECT   = "http://127.0.0.1:5000/callback"
TOKEN_FILE = ".upstox_token"
BASE_URL   = "https://api.upstox.com"

if not API_KEY or not API_SECRET:
    print(
        "\nERROR: Credentials not set. Run in PowerShell:\n"
        "  $env:UPSTOX_API_KEY    = 'your_api_key'\n"
        "  $env:UPSTOX_API_SECRET = 'your_api_secret'\n"
        "Then re-run this script."
    )
    sys.exit(1)

# ── Callback server ───────────────────────────────────────────────────────────

_code = {"value": None}

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        params = parse_qs(urlparse(self.path).query)
        if "code" in params:
            _code["value"] = params["code"][0]
            self.send_response(200)
            self.send_header("Content-type", "text/html")
            self.end_headers()
            self.wfile.write(b"""
                <html><body style="font-family:sans-serif;padding:40px;background:#0f1117;color:#e0e0e0">
                <h2 style="color:#4ade80">Login successful</h2>
                <p>Access token saved. You can close this tab.</p>
                <script>setTimeout(()=>window.close(),2000)</script>
                </body></html>
            """)
        else:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"Missing code parameter")

    def log_message(self, *args):
        pass   # suppress request logs


def exchange_code(code: str) -> str:
    resp = requests.post(
        f"{BASE_URL}/v2/login/authorization/token",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={
            "code":          code,
            "client_id":     API_KEY,
            "client_secret": API_SECRET,
            "redirect_uri":  REDIRECT,
            "grant_type":    "authorization_code",
        },
        timeout=15,
    )
    if not resp.ok:
        print(f"\nUpstox error {resp.status_code}:")
        print(resp.text)
        sys.exit(1)
    return resp.json()["access_token"]


if __name__ == "__main__":
    login_url = (
        f"{BASE_URL}/v2/login/authorization/dialog"
        f"?client_id={API_KEY}"
        f"&redirect_uri={REDIRECT}"
        f"&response_type=code"
    )
    print(f"Opening browser for Upstox login...")
    print(f"If browser doesn't open, visit:\n{login_url}\n")
    webbrowser.open(login_url)

    print("Waiting for login callback on http://127.0.0.1:5000/callback ...")
    HTTPServer(("127.0.0.1", 5000), Handler).handle_request()

    code = _code["value"]
    if not code:
        print("ERROR: No auth code received.")
        sys.exit(1)

    print("Exchanging auth code for access token...")
    token = exchange_code(code)

    with open(TOKEN_FILE, "w") as f:
        f.write(token)

    # Also print it so user can set env var if needed
    print(f"\nAccess token saved to {TOKEN_FILE}")
    print(f"\nOptionally set as env var:")
    print(f"  $env:UPSTOX_ACCESS_TOKEN = \"{token[:20]}...\"")
    print(f"\nNow run: python scripts/build_history_upstox.py")
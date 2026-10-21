"""
upstox_token.py — daily Upstox access token via the Access Token Request API.

No phone/OTP/PIN typing:
  1. request_token() asks Upstox to send the account holder an approval
     prompt (Upstox app + WhatsApp).
  2. On approval Upstox POSTs the token to the app's Notifier Webhook URL,
     which scripts/token_service.py receives and passes to save_token().

Upstox tokens expire at 03:30 IST the day after issue, whenever they were
issued — so a token only counts as fresh if it was saved after the most
recent 03:30.
"""

import os
import requests
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

IST        = ZoneInfo("Asia/Kolkata")
TOKEN_FILE = ".upstox_token"
API_V2     = "https://api.upstox.com/v2"
API_V3     = "https://api.upstox.com/v3"


def last_expiry(now: datetime = None) -> datetime:
    """Most recent 03:30 IST — every token issued before it is dead."""
    now = now or datetime.now(IST)
    cut = now.replace(hour=3, minute=30, second=0, microsecond=0)
    return cut if now >= cut else cut - timedelta(days=1)


def token_saved_at(path: str = TOKEN_FILE):
    if not os.path.exists(path):
        return None
    return datetime.fromtimestamp(os.path.getmtime(path), IST)


def token_is_fresh(path: str = TOKEN_FILE) -> bool:
    saved = token_saved_at(path)
    return saved is not None and saved >= last_expiry()


def request_token() -> dict:
    """Trigger the approval prompt. Returns Upstox's JSON response."""
    key    = os.getenv("UPSTOX_API_KEY", "").strip()
    secret = os.getenv("UPSTOX_API_SECRET", "").strip()
    if not key or not secret:
        return {"status": "error", "errors": [{"message": "UPSTOX_API_KEY/SECRET not set"}]}
    resp = requests.post(
        f"{API_V3}/login/auth/token/request/{key}",
        json    = {"client_secret": secret},
        headers = {"Accept": "application/json", "Content-Type": "application/json"},
        timeout = 15,
    )
    try:
        return resp.json()
    except ValueError:
        return {"status": "error", "errors": [{"message": f"HTTP {resp.status_code}"}]}


def verify_token(token: str) -> bool:
    """True if Upstox accepts the token (profile call succeeds)."""
    try:
        resp = requests.get(
            f"{API_V2}/user/profile",
            headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"},
            timeout = 10,
        )
        return resp.ok
    except requests.RequestException:
        return False


def save_token(token: str, path: str = TOKEN_FILE):
    """Atomic write, owner-only permissions."""
    tmp = f"{path}.tmp"
    fd  = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(token.strip())
    os.replace(tmp, path)

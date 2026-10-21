"""
upstox_token.py — daily Upstox access token (OAuth code flow) helpers,
used by scripts/token_service.py.

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


def login_url(redirect_uri: str, state: str) -> str:
    """Upstox's standard login page (phone → OTP → PIN) for the OAuth code flow."""
    from urllib.parse import urlencode
    q = urlencode({
        "response_type": "code",
        "client_id":     os.getenv("UPSTOX_API_KEY", "").strip(),
        "redirect_uri":  redirect_uri,
        "state":         state,
    })
    return f"{API_V2}/login/authorization/dialog?{q}"


def exchange_code(code: str, redirect_uri: str) -> str:
    """Swap the OAuth code for an access token. Raises on failure."""
    resp = requests.post(
        f"{API_V2}/login/authorization/token",
        headers = {"Content-Type": "application/x-www-form-urlencoded",
                   "Accept": "application/json"},
        data    = {
            "code":          code,
            "client_id":     os.getenv("UPSTOX_API_KEY", "").strip(),
            "client_secret": os.getenv("UPSTOX_API_SECRET", "").strip(),
            "redirect_uri":  redirect_uri,
            "grant_type":    "authorization_code",
        },
        timeout = 15,
    )
    if not resp.ok:
        raise RuntimeError(f"Upstox token exchange HTTP {resp.status_code}: {resp.text[:200]}")
    return resp.json()["access_token"]


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

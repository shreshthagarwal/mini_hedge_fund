"""
token_service.py — always-on helper for the daily Upstox token.

Runs separately from the bot (the bot is stopped overnight), on
127.0.0.1:5174 behind Caddy:

  POST /upstox/webhook/<secret>   Upstox Notifier Webhook (public, secret path)
  GET  /auth                      status page: token + bot state (password-protected)
  POST /auth/request              send a new approval prompt
  POST /auth/start | /auth/stop   start/stop the bot

CLI (used by cron):
  python scripts/token_service.py serve
  python scripts/token_service.py request          # 03:35 — send approval prompt
  python scripts/token_service.py start-if-fresh   # 09:00 — start bot if token is valid

Daily flow: 03:35 prompt → you tap Approve on WhatsApp/Upstox → token
lands here → bot starts at 09:00 (or immediately if approved during market).
"""

import os
import sys
import hmac
import secrets
import time as _time
import logging
import subprocess
from datetime import datetime, time
from html import escape
from urllib.parse import quote

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, os.path.join(ROOT, "src"))

from env import load_env
load_env()

from upstox_token import (
    IST, request_token, verify_token, save_token,
    token_is_fresh, token_saved_at, login_url, exchange_code,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  token  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("token")

SERVICE      = "hedgebot"
MARKET_START = time(9, 0)
MARKET_LAST  = time(15, 20)   # no point starting after square-off


# ── Bot control ───────────────────────────────────────────────────────────────

def bot_active() -> bool:
    r = subprocess.run(["systemctl", "is-active", "--quiet", SERVICE])
    return r.returncode == 0


def _systemctl(action: str) -> bool:
    r = subprocess.run(["sudo", "-n", "systemctl", action, SERVICE],
                       capture_output=True, text=True)
    if r.returncode != 0:
        logger.error(f"systemctl {action} failed: {r.stderr.strip()}")
    return r.returncode == 0


def in_market_window(now: datetime = None) -> bool:
    now = now or datetime.now(IST)
    return now.weekday() < 5 and MARKET_START <= now.time() < MARKET_LAST


def start_if_fresh() -> str:
    if not token_is_fresh():
        logger.warning("Not starting bot: no token issued since 03:30 today")
        return "no fresh token — approve the login request first"
    if bot_active():
        return "bot already running"
    ok = _systemctl("start")
    logger.info(f"Bot start {'ok' if ok else 'FAILED'}")
    return "bot started" if ok else "bot start failed (see logs)"


def send_request() -> str:
    res = request_token()
    if res.get("status") == "success":
        data = res.get("data", {})
        logger.info(f"Approval prompt sent; expires {data.get('authorization_expiry')}  "
                    f"notifier={data.get('notifier_url')}")
        return "approval prompt sent — check WhatsApp / Upstox app"
    errs = "; ".join(e.get("message", "") for e in res.get("errors", []))
    logger.error(f"Token request failed: {errs or res}")
    return f"request failed: {errs or res}"


# ── Web app ───────────────────────────────────────────────────────────────────

def create_app():
    from flask import Flask, request, jsonify, redirect

    app    = Flask(__name__)
    secret = os.getenv("UPSTOX_WEBHOOK_SECRET", "")
    logging.getLogger("werkzeug").setLevel(logging.WARNING)

    def same_origin() -> bool:
        origin = request.headers.get("Origin")
        return not origin or origin.split("://", 1)[-1] == request.host

    @app.post("/upstox/webhook/<path_secret>")
    def webhook(path_secret):
        if not secret or not hmac.compare_digest(path_secret, secret):
            return "", 404
        body = request.get_json(silent=True) or {}
        if body.get("message_type") != "access_token":
            logger.info(f"Ignoring webhook message_type={body.get('message_type')}")
            return jsonify(ok=True)
        if body.get("client_id") != os.getenv("UPSTOX_API_KEY", "").strip():
            logger.warning("Webhook client_id mismatch — ignored")
            return "", 403
        token = body.get("access_token", "")
        if not token or not verify_token(token):
            logger.error("Webhook token rejected by Upstox profile check")
            return "", 400

        save_token(token)
        logger.info(f"Token saved (expires {body.get('expires_at')})")
        if in_market_window():
            start_if_fresh()
        return jsonify(ok=True)

    # ── Browser login (OAuth code flow) ───────────────────────────────────────
    # Works regardless of Upstox's webhook firewall: the code comes back via
    # your own browser. UPSTOX_WEB_REDIRECT_URI must equal the Redirect URL
    # registered in the Upstox app, e.g. https://<site>/auth/callback.
    web_redirect = os.getenv("UPSTOX_WEB_REDIRECT_URI", "").strip()
    pending_states: dict = {}   # state → issued-at; single-process service

    @app.get("/auth/login")
    def auth_login():
        if not web_redirect:
            return redirect(f"/auth?msg={quote('UPSTOX_WEB_REDIRECT_URI not set')}", code=303)
        state = secrets.token_urlsafe(24)
        now   = _time.time()
        for s, t in list(pending_states.items()):
            if now - t > 600:
                del pending_states[s]
        pending_states[state] = now
        return redirect(login_url(web_redirect, state), code=302)

    @app.get("/auth/callback")
    def auth_callback():
        state = request.args.get("state", "")
        code  = request.args.get("code", "")
        if not state or pending_states.pop(state, None) is None:
            return redirect(f"/auth?msg={quote('login expired or invalid — try again')}", code=303)
        if not code:
            return redirect(f"/auth?msg={quote('Upstox returned no code')}", code=303)
        try:
            token = exchange_code(code, web_redirect)
        except Exception as e:
            logger.error(f"Code exchange failed: {e}")
            return redirect(f"/auth?msg={quote('token exchange failed (see logs)')}", code=303)
        if not verify_token(token):
            return redirect(f"/auth?msg={quote('Upstox rejected the token')}", code=303)

        save_token(token)
        logger.info("Token saved via browser login")
        msg = "logged in — token saved"
        if in_market_window():
            msg += "; " + start_if_fresh()
        return redirect(f"/auth?msg={quote(msg)}", code=303)

    @app.get("/auth")
    def auth_page():
        saved  = token_saved_at()
        fresh  = token_is_fresh()
        active = bot_active()
        msg    = request.args.get("msg", "")
        return PAGE.format(
            token_state = "valid" if fresh else "expired / missing",
            token_cls   = "ok" if fresh else "bad",
            saved       = saved.strftime("%d %b %H:%M IST") if saved else "never",
            bot_state   = "running" if active else "stopped",
            bot_cls     = "ok" if active else "bad",
            msg         = f'<p class="msg">{escape(msg)}</p>' if msg else "",
        )

    @app.post("/auth/<action>")
    def auth_action(action):
        if not same_origin():
            return "Cross-site request refused", 403
        if action == "request":
            msg = send_request()
        elif action == "start":
            msg = start_if_fresh()
        elif action == "stop":
            msg = "bot stopped" if _systemctl("stop") else "stop failed"
        else:
            return "", 404
        return redirect(f"/auth?msg={quote(msg)}", code=303)

    return app


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Bot Login</title>
<style>
  :root {{ --bg:#f6f7f9; --card:#fff; --fg:#1b1f24; --muted:#5b6470;
          --ok:#1a7f4b; --bad:#b42318; --btn:#1b1f24; --btn-fg:#fff; --line:#e3e6ea; }}
  @media (prefers-color-scheme: dark) {{
    :root {{ --bg:#0f1216; --card:#171b21; --fg:#e8ebef; --muted:#9aa4b1;
            --ok:#4cc38a; --bad:#ff7a6b; --btn:#e8ebef; --btn-fg:#0f1216; --line:#262c34; }}
  }}
  body {{ margin:0; background:var(--bg); color:var(--fg);
         font:16px/1.5 system-ui, -apple-system, Segoe UI, sans-serif; }}
  main {{ max-width:440px; margin:40px auto; padding:0 16px; }}
  .card {{ background:var(--card); border:1px solid var(--line); border-radius:12px; padding:20px; }}
  h1 {{ font-size:20px; margin:0 0 16px; }}
  dl {{ display:grid; grid-template-columns:auto 1fr; gap:8px 16px; margin:0 0 20px; }}
  dt {{ color:var(--muted); }} dd {{ margin:0; font-weight:600; }}
  .ok {{ color:var(--ok); }} .bad {{ color:var(--bad); }}
  form {{ display:inline; }}
  button {{ background:var(--btn); color:var(--btn-fg); border:0; border-radius:8px;
           padding:10px 14px; font-size:15px; margin:0 8px 8px 0; cursor:pointer; }}
  .msg {{ background:var(--bg); border-radius:8px; padding:10px 12px; margin:0 0 16px; }}
  a {{ color:inherit; }}
</style></head>
<body><main><div class="card">
  <h1>Trading bot</h1>
  {msg}
  <dl>
    <dt>Upstox token</dt><dd class="{token_cls}">{token_state}</dd>
    <dt>Token saved</dt><dd>{saved}</dd>
    <dt>Bot</dt><dd class="{bot_cls}">{bot_state}</dd>
  </dl>
  <form method="get" action="/auth/login"><button>Log in with Upstox</button></form>
  <form method="post" action="/auth/request"><button>Send WhatsApp approval</button></form>
  <form method="post" action="/auth/start"><button>Start bot</button></form>
  <form method="post" action="/auth/stop"><button>Stop bot</button></form>
  <p><a href="/">Open dashboard →</a></p>
</div></main></body></html>"""


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "serve"
    if cmd == "serve":
        create_app().run(host="127.0.0.1", port=int(os.getenv("TOKEN_SERVICE_PORT", "5174")))
    elif cmd == "request":
        print(send_request())
    elif cmd == "start-if-fresh":
        print(start_if_fresh())
    else:
        sys.exit(f"Unknown command {cmd}: use serve | request | start-if-fresh")

"""The team's bot ON / OFF button (client decision 2026-10-21). One page, works on a phone.

    GET  /team            login form, or the button page with the bot's state
    POST /team/login      password=...      -> a cookie for 30 days
    POST /team/bot        state=on | off    -> switch the bot for ALL chats
    POST /team/logout

Rules:

* The router is included in main.py only when TEAM_PASSWORD is set (8+ characters). Without it
  the paths do not exist: 404. One password for the whole team.
* The cookie holds an HMAC of the password, not the password: changing TEAM_PASSWORD logs
  everyone out. HttpOnly + SameSite=Strict (another site cannot press the button for you);
  Secure outside APP_ENV=local. Use it over HTTPS only (the tunnel or the load balancer).
* A wrong password is slow (1 s) and after MAX_FAILURES wrong tries in FAILURE_WINDOW from one
  address the login is refused for a while. Every switch leaves a `team_switch` security event.
* Form bodies are parsed by hand (urllib): no python-multipart dependency for two fields.
"""

import asyncio
import hashlib
import hmac
import html
import logging
import time
from collections import defaultdict, deque
from typing import Annotated
from urllib.parse import parse_qs

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.settings import get_settings
from app.db import repositories as repo
from app.db.session import get_session
from app.owner.presence import BOT_AUTO_ON_FLAG, BOT_OFF_FLAG, local_time, set_bot

log = logging.getLogger(__name__)
security_log = logging.getLogger("security")

router = APIRouter(prefix="/team", tags=["team"])
SessionDep = Annotated[AsyncSession, Depends(get_session)]

COOKIE = "team_session"
COOKIE_DAYS = 30
MAX_FAILURES = 5
FAILURE_WINDOW = 15 * 60  # seconds
WRONG_PASSWORD_DELAY = 1.0  # seconds
_failures: dict[str, deque[float]] = defaultdict(deque)


def _token() -> str:
    password = get_settings().team_password.get_secret_value()
    return hmac.new(password.encode(), b"team-page-v1", hashlib.sha256).hexdigest()


def _logged_in(request: Request) -> bool:
    cookie = request.cookies.get(COOKIE, "")
    return bool(cookie) and hmac.compare_digest(cookie.encode(), _token().encode())


def _client(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _locked(client: str) -> bool:
    tries = _failures[client]
    while tries and tries[0] < time.monotonic() - FAILURE_WINDOW:
        tries.popleft()
    return len(tries) >= MAX_FAILURES


async def _form(request: Request) -> dict[str, str]:
    raw = (await request.body())[:2000].decode("utf-8", errors="replace")
    return {key: values[0] for key, values in parse_qs(raw).items()}


def _see_page() -> RedirectResponse:
    return RedirectResponse("/team", status_code=303)


# ---- pages

_STYLE = """
body{margin:0;font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:#f4f6f8;
color:#1d2733}
main{max-width:420px;margin:0 auto;padding:28px 16px}
h1{font-size:20px;margin:0 0 18px}
.card{background:#fff;border:1px solid #d5dbe1;border-radius:14px;padding:20px}
.state{font-size:30px;font-weight:700;margin:4px 0 8px}
.on{color:#0f7b6c}.off{color:#b45309}
p{line-height:1.5;margin:8px 0}
.small{font-size:13px;color:#6b7685}
.warn{background:#fff7ed;border:1px solid #b45309;border-radius:10px;padding:10px;margin:12px 0}
button{width:100%;border:0;border-radius:12px;padding:18px;font-size:19px;font-weight:700;
color:#fff;cursor:pointer;margin-top:14px}
.go-on{background:#0f7b6c}.go-off{background:#b45309}
.link{background:none;color:#6b7685;font-size:14px;font-weight:400;padding:8px;margin-top:18px}
input{width:100%;box-sizing:border-box;padding:14px;font-size:17px;border:1px solid #d5dbe1;
border-radius:10px;margin-top:8px}
"""


def _page(body: str, *, refresh: bool = False) -> HTMLResponse:
    meta = '<meta http-equiv="refresh" content="30">' if refresh else ""
    text = (
        '<!doctype html><html lang="ur"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"{meta}<title>Bot ON / OFF</title><style>{_STYLE}</style></head>"
        f"<body><main><h1>Tussles Store: WhatsApp bot</h1>{body}</main></body></html>"
    )
    response = HTMLResponse(text)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Frame-Options"] = "DENY"
    return response


def _login_page(message: str = "") -> HTMLResponse:
    note = f'<p class="warn">{html.escape(message)}</p>' if message else ""
    return _page(
        f'<div class="card">{note}<form method="post" action="/team/login">'
        "<p>Team ka password likhein:</p>"
        '<input type="password" name="password" autocomplete="current-password" required>'
        '<button class="go-on" type="submit">Login</button></form></div>'
    )


@router.get("", response_class=HTMLResponse)
async def team_page(request: Request, session: SessionDep) -> HTMLResponse:
    if not _logged_in(request):
        return _login_page()
    settings = get_settings()
    off = await repo.get_flag(session, BOT_OFF_FLAG)
    automatic = await repo.get_flag(session, BOT_AUTO_ON_FLAG)
    changed = await repo.flag_changed_at(session, BOT_OFF_FLAG)
    since = f"{local_time(changed, settings.business_utc_offset)} se" if changed else ""
    killed = settings.kill_switch or await repo.get_flag(session, repo.KILL_SWITCH_FLAG)

    if off:
        state = '<div class="state off">Bot OFF</div>'
        about = (
            "<p>Team customers ko WhatsApp Business app se reply kar rahi hai. Bot khud se "
            "reply nahi karega.</p>"
            f"<p>Agar kisi customer ko <b>{settings.team_wait_minutes} minute</b> mein team ka "
            "reply nahi mila, to bot usay jawab de dega aur khud ON ho jayega.</p>"
        )
        button = '<button class="go-on" name="state" value="on">Bot ON karein</button>'
    else:
        state = '<div class="state on">Bot ON</div>'
        about = "<p>Bot har customer ko foran reply kar raha hai.</p>"
        if automatic:
            about += (
                '<p class="warn">Bot <b>khud ON</b> hua tha: ek customer ko waqt pe team ka '
                "reply nahi mila. Team mojood hai to bot OFF kar dein.</p>"
            )
        button = '<button class="go-off" name="state" value="off">Bot OFF karein</button>'
    kill = (
        '<p class="warn">Kill switch ON hai: bot kisi ko reply nahi kar raha, chahe yahan '
        "ON ho. Developer se baat karein.</p>"
        if killed
        else ""
    )
    return _page(
        f'<div class="card">{state}<p class="small">{html.escape(since)}</p>{kill}{about}'
        f'<form method="post" action="/team/bot">{button}</form></div>'
        '<form method="post" action="/team/logout">'
        '<button class="link" type="submit">Logout</button></form>'
        '<p class="small">Yeh page har 30 second mein khud refresh hota hai.</p>',
        refresh=True,
    )


@router.post("/login", response_model=None)
async def login(request: Request) -> Response:
    client = _client(request)
    if _locked(client):
        security_log.warning("team_login_locked")
        return _login_page("Bohat dafa ghalat password. 15 minute baad dobara koshish karein.")
    password = (await _form(request)).get("password", "")
    expected = get_settings().team_password.get_secret_value()
    if not hmac.compare_digest(password.encode(), expected.encode()):
        _failures[client].append(time.monotonic())
        security_log.warning("team_login_failed")
        await asyncio.sleep(WRONG_PASSWORD_DELAY)
        return _login_page("Password ghalat hai.")
    _failures.pop(client, None)
    response = _see_page()
    response.set_cookie(
        COOKIE,
        _token(),
        max_age=COOKIE_DAYS * 24 * 3600,
        httponly=True,
        samesite="strict",
        secure=get_settings().app_env != "local",
        path="/team",
    )
    return response


@router.post("/bot", response_model=None)
async def switch(request: Request, session: SessionDep) -> Response:
    if not _logged_in(request):
        return _see_page()
    state = (await _form(request)).get("state")
    if state in ("on", "off"):
        on = state == "on"
        await set_bot(session, on)
        await repo.add_security_event(session, "team_switch", detail={"bot_on": on})
        await session.commit()
    return _see_page()


@router.post("/logout", response_model=None)
async def logout() -> Response:
    response = _see_page()
    response.delete_cookie(COOKIE, path="/team")
    return response

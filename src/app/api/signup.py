"""Embedded Signup page: connect the store's WhatsApp Business app number to the bot (P5).

    GET  /signup?key=...      the page with the 'Connect WhatsApp' button (Meta's JS SDK)
    POST /signup/complete     JSON {key, code, event, waba_id, phone_number_id} from that page

This is WhatsApp "Coexistence": the number stays in the WhatsApp Business app on the phone AND
the bot uses it through Cloud API. Meta allows it only through Embedded Signup run by a Tech
Provider app (ws-bot), see docs: embedded-signup/onboarding-business-app-users. Embedded Signup
v2/v3 stopped on 2026-10-15: the v4 flow is chosen by the Facebook Login for Business
configuration (SIGNUP_CONFIG_ID), the code below is the same for v4.

What /signup/complete does, server to server, right after the flow (the code lives 30 s):

1. code -> business token (GET /oauth/access_token, app id + app secret). The token is used for
   the next calls only and is NEVER stored, logged or sent back to the page. The bot keeps using
   WHATSAPP_ACCESS_TOKEN (the Wsbot system user): give that user the new WABA as an asset.
2. subscribe ws-bot to the WABA's webhooks (POST /{waba_id}/subscribed_apps).
3. find the phone number id if the page did not get one (GET /{waba_id}/phone_numbers).
4. Coexistence only (event FINISH_WHATSAPP_BUSINESS_APP_ONBOARDING): start the contacts sync and
   the history sync (POST /{phone_number_id}/smb_app_data). Meta requires both within 24 hours
   of onboarding, or the number must be offboarded and connected again. Each can run only once.
   The number is NOT registered (/register): a Business app number is already registered.
5. read is_on_biz_app + platform_type back so the page can show whether it worked.

Rules: the router exists only when SIGNUP_KEY is set (main.py); every call needs the key
(compared with hmac.compare_digest); ids must be digits; a failed step is reported, never hidden.
"""

import hmac
import html
import json
import logging
import re
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from app.core.settings import get_settings

log = logging.getLogger(__name__)
security_log = logging.getLogger("security")

router = APIRouter(prefix="/signup", tags=["signup"])

COEXISTENCE_EVENT = "FINISH_WHATSAPP_BUSINESS_APP_ONBOARDING"
FINISH_EVENTS = {COEXISTENCE_EVENT, "FINISH", "FINISH_ONLY_WABA"}
GRAPH = "https://graph.facebook.com"
HTTP_TIMEOUT = 20
_DIGITS = re.compile(r"\d{5,25}")


class CompleteRequest(BaseModel):
    key: str = ""
    code: str = ""
    event: str = ""
    waba_id: str = ""
    phone_number_id: str = ""


def _key_ok(key: str) -> bool:
    expected = get_settings().signup_key.get_secret_value()
    return bool(expected) and hmac.compare_digest(key.encode(), expected.encode())


def _not_found() -> HTTPException:
    # a wrong key looks exactly like a page that does not exist
    return HTTPException(status_code=404, detail="Not Found")


def _meta_error(response: httpx.Response) -> str:
    """Meta's own message and code, never the token."""
    try:
        error = response.json()["error"]
        return f"HTTP {response.status_code}, code {error.get('code')}: {error.get('message', '')}"
    except (ValueError, KeyError, TypeError):
        return f"HTTP {response.status_code}"


async def _exchange_code(http: httpx.AsyncClient, code: str) -> str:
    s = get_settings()
    response = await http.get(
        f"{GRAPH}/{s.meta_graph_version}/oauth/access_token",
        params={
            "client_id": s.meta_app_id,
            "client_secret": s.whatsapp_app_secret.get_secret_value(),
            "code": code,
        },
    )
    if response.status_code != 200:
        raise RuntimeError(f"token exchange failed ({_meta_error(response)})")
    try:
        token = str(response.json()["access_token"])
    except (ValueError, KeyError, TypeError):
        token = response.text.strip()  # the docs show the bare token as the answer
    if not token:
        raise RuntimeError("token exchange gave no token")
    return token


async def onboard(*, code: str, event: str, waba_id: str, phone_number_id: str) -> dict[str, Any]:
    """Run the onboarding calls. Returns a report for the page; never contains the token."""
    s = get_settings()
    version = s.meta_graph_version
    steps: list[dict[str, Any]] = []
    report: dict[str, Any] = {
        "ok": False,
        "event": event,
        "waba_id": waba_id,
        "phone_number_id": phone_number_id,
        "steps": steps,
    }

    def step(name: str, ok: bool, note: str = "") -> None:
        steps.append({"step": name, "ok": ok, "note": note})

    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as http:
        try:
            token = await _exchange_code(http, code)
        except RuntimeError as exc:
            step("token", False, str(exc))
            return report
        except httpx.HTTPError:
            step("token", False, "Meta not reachable")
            return report
        step("token", True)
        auth = {"Authorization": f"Bearer {token}"}

        response = await http.post(f"{GRAPH}/{version}/{waba_id}/subscribed_apps", headers=auth)
        step(
            "subscribe_webhooks",
            response.status_code == 200,
            "" if response.status_code == 200 else _meta_error(response),
        )

        if not phone_number_id:
            response = await http.get(
                f"{GRAPH}/{version}/{waba_id}/phone_numbers",
                params={"fields": "id,display_phone_number"},
                headers=auth,
            )
            numbers = response.json().get("data", []) if response.status_code == 200 else []
            if len(numbers) == 1:
                phone_number_id = str(numbers[0]["id"])
                report["phone_number_id"] = phone_number_id
                step("find_phone_number", True)
            else:
                note = (
                    _meta_error(response)
                    if response.status_code != 200
                    else (f"{len(numbers)} numbers on this WABA, expected 1")
                )
                step("find_phone_number", False, note)
                return report

        if event == COEXISTENCE_EVENT:
            for sync_type in ("smb_app_state_sync", "history"):
                response = await http.post(
                    f"{GRAPH}/{version}/{phone_number_id}/smb_app_data",
                    headers=auth,
                    json={"messaging_product": "whatsapp", "sync_type": sync_type},
                )
                ok = response.status_code == 200
                request_id = response.json().get("request_id", "") if ok else ""
                step(
                    f"sync_{sync_type}",
                    ok,
                    f"request_id {request_id}" if ok else _meta_error(response),
                )
        else:
            step(
                "coexistence",
                False,
                "This number was NOT connected from the WhatsApp Business app (event "
                f"{event}). For Coexistence choose 'Connect your existing WhatsApp Business app'.",
            )

        response = await http.get(
            f"{GRAPH}/{version}/{phone_number_id}",
            params={"fields": "is_on_biz_app,platform_type,display_phone_number"},
            headers=auth,
        )
        if response.status_code == 200:
            data = response.json()
            report["display_phone_number"] = data.get("display_phone_number", "")
            report["is_on_biz_app"] = data.get("is_on_biz_app")
            report["platform_type"] = data.get("platform_type")
            step("check_status", True)
        else:
            step("check_status", False, _meta_error(response))

    report["ok"] = all(item["ok"] for item in steps) and report.get("is_on_biz_app") is True
    return report


@router.post("/complete")
async def complete(request: Request) -> JSONResponse:
    try:
        body = CompleteRequest.model_validate(json.loads((await request.body())[:5000]))
    except ValueError as exc:
        raise _not_found() from exc
    if not _key_ok(body.key):
        security_log.warning("signup_wrong_key")
        raise _not_found()
    s = get_settings()
    if not (s.meta_app_id and s.meta_graph_version and s.whatsapp_app_secret.get_secret_value()):
        return JSONResponse(
            {"ok": False, "error": "Set META_APP_ID, META_GRAPH_VERSION, WHATSAPP_APP_SECRET"},
            status_code=500,
        )
    if body.event not in FINISH_EVENTS or not body.code:
        return JSONResponse({"ok": False, "error": "flow not finished"}, status_code=400)
    if not _DIGITS.fullmatch(body.waba_id) or (
        body.phone_number_id and not _DIGITS.fullmatch(body.phone_number_id)
    ):
        return JSONResponse({"ok": False, "error": "bad ids"}, status_code=400)

    try:
        report = await onboard(
            code=body.code,
            event=body.event,
            waba_id=body.waba_id,
            phone_number_id=body.phone_number_id,
        )
    except httpx.HTTPError:
        # a later call could not reach Meta: say so, the steps before it may have run
        report = {
            "ok": False,
            "event": body.event,
            "waba_id": body.waba_id,
            "phone_number_id": body.phone_number_id,
            "steps": [{"step": "network", "ok": False, "note": "Meta not reachable"}],
        }
    # ids only: no token, no phone number in the log
    security_log.info(
        "signup_completed",
        extra={
            "ok": report["ok"],
            "event": body.event,
            "waba_id": body.waba_id,
            "phone_number_id": report.get("phone_number_id", ""),
            "failed_steps": [i["step"] for i in report["steps"] if not i["ok"]],
        },
    )
    return JSONResponse(report)


# ---- the page

_STYLE = """
body{margin:0;font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:#f4f6f8;
color:#1d2733}
main{max-width:560px;margin:0 auto;padding:28px 16px}
h1{font-size:20px;margin:0 0 18px}
.card{background:#fff;border:1px solid #d5dbe1;border-radius:14px;padding:20px;margin-bottom:16px}
p,li{line-height:1.55}
ol{padding-left:20px}
.warn{background:#fff7ed;border:1px solid #b45309;border-radius:10px;padding:10px}
button{width:100%;border:0;border-radius:12px;padding:16px;font-size:18px;font-weight:700;
color:#fff;background:#1877f2;cursor:pointer}
button:disabled{opacity:.5;cursor:wait}
#result{white-space:pre-wrap;font-family:ui-monospace,Consolas,monospace;font-size:13px}
.ok{color:#0f7b6c;font-weight:700}.bad{color:#b42318;font-weight:700}
"""

_SCRIPT = """
const KEY = __KEY__;
let session = null;   // from Meta's message event (asset ids)
let sent = false;
const out = document.getElementById('result');
const btn = document.getElementById('connect');

window.fbAsyncInit = function () {
  FB.init({appId: __APP_ID__, autoLogAppEvents: true, xfbml: true, version: __VERSION__});
  btn.disabled = false;
};

window.addEventListener('message', (event) => {
  if (!event.origin.endsWith('facebook.com')) return;
  try {
    const data = JSON.parse(event.data);
    if (data.type !== 'WA_EMBEDDED_SIGNUP') return;
    if (data.event === 'CANCEL') {
      out.innerHTML = '<span class="bad">Flow beech mein band hua.</span>\\n' +
        JSON.stringify(data.data || {}, null, 2);
      btn.disabled = false;
      return;
    }
    session = {event: data.event, waba_id: (data.data || {}).waba_id || '',
               phone_number_id: (data.data || {}).phone_number_id || ''};
  } catch (e) { /* not a JSON message from the flow */ }
});

async function send(code) {
  if (sent) return;
  sent = true;
  out.textContent = 'Connect ho raha hai, sync shuru ho raha hai... page band na karein.';
  const body = Object.assign({key: KEY, code: code}, session || {});
  try {
    const r = await fetch('/signup/complete', {method: 'POST',
      headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
    const report = await r.json();
    const head = report.ok
      ? '<span class="ok">Ho gaya: number bot se jud gaya (Coexistence).</span>'
      : '<span class="bad">Kuch steps nakam hue. Yeh result developer ko bhejein.</span>';
    out.innerHTML = head + '\\n\\n' + JSON.stringify(report, null, 2)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;');
  } catch (e) {
    out.innerHTML = '<span class="bad">Server tak nahi pohncha.</span>';
  }
}

function waitForSession(code, triesLeft) {
  // Meta usually sends the asset ids before the code; wait a little if not (code lives 30 s)
  if (session || triesLeft <= 0) { send(code); return; }
  setTimeout(() => waitForSession(code, triesLeft - 1), 250);
}

function launch() {
  btn.disabled = true;
  out.textContent = 'Meta ki window khul rahi hai...';
  FB.login((response) => {
    if (response.authResponse && response.authResponse.code) {
      waitForSession(response.authResponse.code, 16);
    } else {
      out.innerHTML = '<span class="bad">Login poora nahi hua.</span>';
      btn.disabled = false;
    }
  }, {
    config_id: __CONFIG_ID__,
    response_type: 'code',
    override_default_response_type: true,
    extras: {setup: {}, featureType: 'whatsapp_business_app_onboarding', sessionInfoVersion: '3'}
  });
}
"""


@router.get("", response_class=HTMLResponse)
async def signup_page(key: str = "") -> HTMLResponse:
    if not _key_ok(key):
        security_log.warning("signup_wrong_key")
        raise _not_found()
    s = get_settings()
    missing = [
        name
        for name, value in (
            ("META_APP_ID", s.meta_app_id),
            ("SIGNUP_CONFIG_ID", s.signup_config_id),
            ("META_GRAPH_VERSION", s.meta_graph_version),
        )
        if not value
    ]
    script = (
        _SCRIPT.replace("__KEY__", json.dumps(key))
        .replace("__APP_ID__", json.dumps(s.meta_app_id))
        .replace("__VERSION__", json.dumps(s.meta_graph_version))
        .replace("__CONFIG_ID__", json.dumps(s.signup_config_id))
    )
    note = (
        f'<p class="warn">.env mein yeh set karein: {html.escape(", ".join(missing))}</p>'
        if missing
        else ""
    )
    body = f"""
<div class="card">{note}
<p>Is se store ka <b>WhatsApp Business app wala number</b> bot se jud jayega. Number phone ki
app mein bhi chalta rahega.</p>
<ol>
<li>Phone par WhatsApp Business app <b>latest version</b> ho aur khuli rahe.</li>
<li>Neeche button dabayein, Facebook se login karein (Tussles Store ka admin).</li>
<li>Business portfolio: <b>Tussles Store</b> chunein (baad mein badla nahi ja sakta).</li>
<li><b>Connect your existing WhatsApp Business app</b> chunein aur store ka number likhein.</li>
<li>Phone par Facebook Business ka message aayega: <b>Connect</b> dabayein aur code daalein.</li>
<li>Chat history share karne ka sawal aaye to <b>share na karna</b> behtar hai: bot ko purani
chats nahi chahiye.</li>
<li>Aakhir tak flow poora karein. Result neeche aayega.</li>
</ol>
<button id="connect" onclick="launch()" disabled>Connect WhatsApp</button>
</div>
<div class="card"><div id="result">Abhi tak kuch nahi.</div></div>
"""
    text = (
        '<!doctype html><html lang="ur"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<meta name="robots" content="noindex"><title>Connect WhatsApp</title>'
        f"<style>{_STYLE}</style></head><body><main><h1>Tussles Store: WhatsApp connect</h1>"
        f"{body}</main><script>{script}</script>"
        '<script async defer crossorigin="anonymous" '
        'src="https://connect.facebook.net/en_US/sdk.js"></script></body></html>'
    )
    response = HTMLResponse(text)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"  # the key is in the URL
    return response

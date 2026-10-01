#!/usr/bin/env python3
"""Drive the full OIDC interactive login for devbox_bot.desktop via CDP.

Requires: headless Chrome on :<cdp-port> (shared profile with a DevBox
web session, or we sign in first), desktop.py running WITHOUT
DEVBOX_API_KEY on <bot-port> with --auth-origin pointing at the OIDC
issuer. Records only non-sensitive token metadata (iss/aud/alg/exp).
"""
import base64
import hashlib
import json
import os
import secrets
import sys
import time
import urllib.request

import websocket

CDP = sys.argv[1] if len(sys.argv) > 1 else "29229"
BOT = sys.argv[2] if len(sys.argv) > 2 else "7820"
PASSWORD = os.environ["DEVBOX_LOCAL_PASSWORD"]
OUT = sys.argv[3] if len(sys.argv) > 3 else "oidc-e2e.json"

def b64url(b):
    return base64.urlsafe_b64encode(b).decode().rstrip("=")

verifier = b64url(secrets.token_bytes(32))
challenge = b64url(hashlib.sha256(verifier.encode()).digest())
uuid = "oidc-e2e-" + secrets.token_hex(4)

tabs = json.load(urllib.request.urlopen(
    f"http://127.0.0.1:{CDP}/json/list"))
page = next(t for t in tabs if t["type"] == "page")
ws = websocket.create_connection(page["webSocketDebuggerUrl"], timeout=30, suppress_origin=True)
_id = 0

def call(method, params=None):
    global _id
    _id += 1
    ws.send(json.dumps({"id": _id, "method": method, "params": params or {}}))
    while True:
        r = json.loads(ws.recv())
        if r.get("id") == _id:
            return r.get("result", {})

def ev(expr):
    return call("Runtime.evaluate", {
        "expression": expr, "returnByValue": True,
        "awaitPromise": True}).get("result", {}).get("value")

result = {"uuid": uuid, "steps": []}
step = result["steps"].append

def login_at(origin):
    """Fill the DevBox email+password login on <origin>."""
    call("Page.navigate", {"url":
        f"{origin}/auth/login?redirect=%2Fsettings%2Fpreferences"})
    time.sleep(3)
    for _ in range(3):
        has_pw = ev("!!document.querySelector('input[type=password]')")
        if has_pw:
            ev('document.querySelector("input[type=password]").focus()')
            call("Input.insertText", {"text": PASSWORD})
            ev('[...document.querySelectorAll("button")].find('
               'x=>/sign in|log in|continue/i.test(x.innerText))?.click()')
            time.sleep(5)
            continue
        if ev("location.pathname.includes('/auth/login')") and \
                ev("!!document.querySelector('input')"):
            ev('document.querySelector("input").focus()')
            call("Input.insertText", {"text": "user@devbox.local"})
            ev('[...document.querySelectorAll("button")].find('
               'x=>/log in|continue/i.test(x.innerText))?.click()')
            time.sleep(4)
            continue
        break
    return ev("location.href")


# 1. DevBox web login on BOTH the CP origin and the OIDC origin
#    (the session cookie is host-scoped, not port-scoped, but each
#    service gates its own /auth/login -> authorize bounce).
for origin in ("http://127.0.0.1:8000", "http://127.0.0.1:8001"):
    url1 = login_at(origin)
    step({"step": f"web login {origin}", "landed": url1})

# 2. loginDeepControl -> authorize -> callback
call("Page.navigate", {"url":
    f"http://127.0.0.1:{BOT}/loginDeepControl?challenge={challenge}"
    f"&uuid={uuid}&mode=login&redirectTarget=devtools://app"})
time.sleep(6)
url2 = ev("location.href")
body_txt = ev("document.body ? document.body.innerText.slice(0,300) : ''")
step({"step": "authorize->callback", "landed": url2,
      "body": body_txt})

# The OIDC service may bounce to its own /auth/login when the web
# session cookie doesn't carry — fill that form too, then let the
# authorize redirect chain continue.
for _ in range(4):
    on_login = ev(
        "location.pathname.includes('/auth/login')")
    if not on_login:
        break
    has_pw = ev("!!document.querySelector('input[type=password]')")
    if not has_pw:
        ev('document.querySelector("input") && '
           'document.querySelector("input").focus()')
        call("Input.insertText", {"text": "user@devbox.local"})
        ev('[...document.querySelectorAll("button")].find('
           'x=>/log in|continue/i.test(x.innerText))?.click()')
        time.sleep(3)
        continue
    ev('document.querySelector("input[type=password]").focus()')
    call("Input.insertText", {"text": PASSWORD})
    ev('[...document.querySelectorAll("button")].find('
       'x=>/sign in|log in|continue/i.test(x.innerText))?.click()')
    time.sleep(5)

# If a confirm/approve button exists, click it
ev('[...document.querySelectorAll("button,[role=button],input[type=submit]")]'
   '.find(x=>/approve|authorize|continue|allow|sign in/i'
   '.test(x.innerText||x.value||""))?.click()')
time.sleep(5)
url3 = ev("location.href")
step({"step": "post-approve", "landed": url3})

# 3. poll with the client verifier
req = urllib.request.Request(
    f"http://127.0.0.1:{BOT}/auth/poll?uuid={uuid}&verifier={verifier}")
try:
    with urllib.request.urlopen(req, timeout=10) as r:
        poll_status, poll_body = r.status, json.loads(r.read())
except urllib.error.URLError as e:
    poll_status, poll_body = getattr(e, "code", 0), {"error": str(e)}
step({"step": "auth/poll", "status": poll_status,
      "keys": sorted(poll_body) if isinstance(poll_body, dict) else "?"})

tok = poll_body.get("accessToken", "")
if tok.count(".") == 2:
    claims = json.loads(base64.urlsafe_b64decode(
        tok.split(".")[1] + "=="))
    hdr = json.loads(base64.urlsafe_b64decode(
        tok.split(".")[0] + "=="))
    step({"step": "token metadata", "alg": hdr.get("alg"),
          "iss": claims.get("iss"), "aud": claims.get("aud"),
          "exp": claims.get("exp"), "sub": claims.get("sub")})
    # 4. refresh passthrough
    rt = poll_body.get("refreshToken", "")
    if rt:
        req = urllib.request.Request(
            f"http://127.0.0.1:{BOT}/oauth/token",
            data=json.dumps({
                "grant_type": "refresh_token",
                "client_id": "grok-bot-desktop",
                "refresh_token": rt}).encode(),
            headers={"content-type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                rb = json.loads(r.read())
                step({"step": "oauth/token refresh", "status": r.status,
                      "keys": sorted(rb)})
        except urllib.error.URLError as e:
            step({"step": "oauth/token refresh",
                  "status": getattr(e, "code", 0), "error": str(e)})
    result["ok"] = True

ws.close()
with open(OUT, "w") as fh:
    json.dump(result, fh, indent=2)
print(json.dumps(result, indent=2)[:3000])
sys.exit(0 if result.get("ok") else 1)

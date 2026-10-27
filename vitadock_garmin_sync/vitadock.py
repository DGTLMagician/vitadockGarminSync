"""Client for the VitaDock Online cloud API used by the VitaDock+ app.

Reverse engineered from VitaDock+ 2.9.42 (Unity/IL2CPP, classes ``API``,
``DataConnection`` and ``Trinity.Helper.AuthorizationBuilder``):

* every request is signed with OAuth 1.0a, HMAC-SHA256;
* the consumer key/secret are baked into the app (read from the environment here);
* login is a three-legged OAuth flow whose browser step is a plain Spring
  Security form at ``/portalv2/signin``, so it can be driven headlessly;
* the resulting access token does not expire unless revoked
  (``/auth/accesses/logout``), so it is stored and reused.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import logging
import os
import re
import secrets
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NamedTuple

import requests

log = logging.getLogger(__name__)


SERVERS = {
    "eu": "https://cloud.vitadock.com",
    "asia": "https://asia.vitadock.online",
}

# Module names as used in /data/<module> URLs.
MODULE_TARGETSCALE = "targetscales"
MODULE_CARDIODOCK = "cardiodocks"
MODULE_THERMODOCK = "thermodocks"
MODULE_GLUCODOCK = "glucodockglucoses"

CALLBACK_SCHEME = "vitadock2://"
CALLBACK_URL = "vitadock2://callback"
PAGE_SIZE = 100


class VitaDockError(Exception):
    pass


class AuthError(VitaDockError):
    pass


@dataclass
class AccessToken:
    token: str
    secret: str


def consumer() -> tuple[str, str]:
    """The app's OAuth consumer key/secret.

    They are shipped in the VitaDock+ app (production ``Config.ServerConfiguration``)
    but deliberately not in this repository; see the README for how to extract them.
    """
    key = os.environ.get("VITADOCK_CONSUMER_KEY", "")
    secret = os.environ.get("VITADOCK_CONSUMER_SECRET", "")
    if not key or not secret:
        raise AuthError("VITADOCK_CONSUMER_KEY and VITADOCK_CONSUMER_SECRET must be set")
    return key, secret


def _q(value: str) -> str:
    return urllib.parse.quote(value, safe="~-._")


def oauth_header(
    method: str,
    url: str,
    *,
    token: str | None = None,
    token_secret: str = "",
    verifier: str | None = None,
    callback: str | None = None,
    query: dict[str, str] | None = None,
    nonce: str | None = None,
    timestamp: str | None = None,
) -> str:
    """Build an ``Authorization: OAuth ...`` header (RFC 5849, HMAC-SHA256)."""
    consumer_key, consumer_secret = consumer()
    params = {
        "oauth_consumer_key": consumer_key,
        "oauth_nonce": nonce or secrets.token_urlsafe(24),
        "oauth_signature_method": "HMAC-SHA256",
        "oauth_timestamp": timestamp or str(int(time.time())),
        "oauth_version": "1.0",
    }
    if token:
        params["oauth_token"] = token
    if verifier:
        params["oauth_verifier"] = verifier
    if callback:
        params["oauth_callback"] = callback

    signed = dict(params)
    signed.update(query or {})
    normalized = "&".join(f"{_q(k)}={_q(v)}" for k, v in sorted(signed.items()))
    base = "&".join((method.upper(), _q(url), _q(normalized)))
    key = f"{_q(consumer_secret)}&{_q(token_secret)}"
    digest = hmac.new(key.encode(), base.encode(), hashlib.sha256).digest()
    params["oauth_signature"] = base64.b64encode(digest).decode()
    return "OAuth " + ",".join(f'{k}="{_q(v)}"' for k, v in params.items())


class VitaDockClient:
    def __init__(self, server: str = "eu", access: AccessToken | None = None):
        self.base = SERVERS.get(server, server).rstrip("/")
        self.access = access
        self.http = requests.Session()
        self.http.headers["User-Agent"] = "vitadock-garmin-sync"

    # -- authentication -------------------------------------------------

    def login(self, email: str, password: str) -> AccessToken:
        """Run the full three-legged OAuth flow with e-mail and password."""
        request_token, request_secret = self._request_token()
        verifier = self._portal_login(request_token, email, password)
        self.access = self._access_token(request_token, request_secret, verifier)
        return self.access

    def _request_token(self) -> tuple[str, str]:
        url = f"{self.base}/auth/unauthorizedaccesses"
        auth = oauth_header("POST", url, callback=CALLBACK_URL)
        r = self.http.post(url, headers={"Authorization": auth}, timeout=30)
        if r.status_code != 200:
            raise AuthError(f"request token failed: HTTP {r.status_code} {r.text[:200]}"
                            "(check VITADOCK_CONSUMER_KEY/VITADOCK_CONSUMER_SECRET)")
        data = urllib.parse.parse_qs(r.text)
        return data["oauth_token"][0], data["oauth_token_secret"][0]

    def _portal_login(self, request_token: str, email: str, password: str) -> str:
        """Log in on the web portal and return the OAuth verifier.

        The portal ends by redirecting to ``vitadock2://callback?...oauth_verifier=``,
        which is where the app's web view picks it up; here the redirect chain
        is followed by hand so that the custom-scheme Location can be read.
        """
        self._hops: list[requests.Response] = []
        url = f"{self.base}/portalv2/desiredaccessrights/request?oauth_token={_q(request_token)}"
        r = self._follow(self.http.get(url, allow_redirects=False, timeout=30))
        if (v := self._verifier(r)) is not None:
            return v

        form = _parse_form(r.text, form_id="User")
        if form is None:
            raise AuthError("login form not found on VitaDock portal")
        form.fields.update({"j_username": email, "j_password": password})
        r = self._submit(r, form)

        # Then the consent page ("desired access rights"); approve it. It is a
        # GET form to /portalv2/permission/thanks; posting it gives HTTP 403.
        for _ in range(3):
            if (v := self._verifier(r)) is not None:
                return v
            if "j_password" in r.text or "error" in r.url:
                raise AuthError("VitaDock login rejected (wrong e-mail or password?)")
            form = _parse_form(r.text)
            if form is None:
                break
            r = self._submit(r, form)
        if (v := self._verifier(r)) is not None:
            return v
        hint = f"; portal pages saved in {self._dump()}" if os.environ.get("VDGS_DEBUG_DIR") else \
            "; rerun with VDGS_DEBUG_DIR=<dir> to save the portal pages"
        raise AuthError(f"no OAuth verifier received from portal (ended at {r.url}){hint}")

    def _submit(self, page: requests.Response, form: Form) -> requests.Response:
        url = urllib.parse.urljoin(page.url, form.action)
        if form.method == "get":
            r = self.http.get(url, params=form.fields, allow_redirects=False, timeout=30)
        else:
            r = self.http.post(url, data=form.fields, allow_redirects=False, timeout=30)
        return self._follow(r)

    def _follow(self, r: requests.Response) -> requests.Response:
        for _ in range(10):
            self._hops.append(r)
            loc = r.headers.get("Location")
            if not r.is_redirect or not loc or loc.startswith(CALLBACK_SCHEME):
                return r
            r = self.http.get(urllib.parse.urljoin(r.url, loc), allow_redirects=False, timeout=30)
        raise AuthError("too many redirects on VitaDock portal")

    def _verifier(self, r: requests.Response) -> str | None:
        """Find ``oauth_verifier`` in any redirect, URL, cookie or page seen so far."""
        for hop in [*getattr(self, "_hops", []), r]:
            texts = [hop.headers.get("Location", ""), hop.url or "", hop.text]
            texts += [c.value or "" for c in hop.cookies]
            for text in texts:
                m = re.search(r"oauth_verifier(?:=|&#61;|\"\s*:\s*\"|\"?\s+value=\")([A-Za-z0-9_\-.%]+)",
                              html.unescape(text))
                if m:
                    return urllib.parse.unquote(m.group(1))
        return None

    def _dump(self) -> str:
        """Save the portal responses for debugging (no password: that is only sent, never echoed)."""
        out = Path(os.environ["VDGS_DEBUG_DIR"]).expanduser()
        out.mkdir(parents=True, exist_ok=True, mode=0o700)
        for i, hop in enumerate(self._hops):
            meta = {"method": hop.request.method, "url": hop.url, "status": hop.status_code,
                    "location": hop.headers.get("Location"),
                    "cookies": sorted(c.name for c in hop.cookies),
                    "request_fields": sorted(urllib.parse.parse_qs(hop.request.body or "").keys())
                    if isinstance(hop.request.body, str) else None}
            (out / f"{i:02d}.json").write_text(json.dumps(meta, indent=1))
            (out / f"{i:02d}.html").write_text(hop.text)
        return str(out)

    def _access_token(self, token: str, secret: str, verifier: str) -> AccessToken:
        url = f"{self.base}/auth/accesses/verify"
        auth = oauth_header("POST", url, token=token, token_secret=secret, verifier=verifier)
        r = self.http.post(url, headers={"Authorization": auth}, timeout=30)
        if r.status_code != 200:
            raise AuthError(f"access token failed: HTTP {r.status_code} {r.text[:200]}")
        data = urllib.parse.parse_qs(r.text)
        return AccessToken(data["oauth_token"][0], data["oauth_token_secret"][0])

    # -- data -------------------------------------------------------------

    def _get(self, path: str, query: dict[str, str] | None = None) -> Any:
        if self.access is None:
            raise AuthError("not logged in")
        url = f"{self.base}/data/{path}"
        auth = oauth_header("GET", url, token=self.access.token,
                            token_secret=self.access.secret, query=query)
        r = self.http.get(url, params=query, headers={"Authorization": auth}, timeout=60)
        if r.status_code in (401, 403):
            raise AuthError(f"VitaDock rejected access token: HTTP {r.status_code}")
        if r.status_code != 200:
            raise VitaDockError(f"GET {path}: HTTP {r.status_code} {r.text[:200]}")
        return r.json() if r.text.strip() else []

    def measurements(self, module: str, since_ms: int = 0) -> list[dict[str, Any]]:
        """All records of a module changed since ``since_ms`` (epoch millis)."""
        out: list[dict[str, Any]] = []
        start = 0
        while True:
            page = self._get(module, {"start": str(start), "max": str(PAGE_SIZE),
                                      "date_since": str(since_ms)})
            if not isinstance(page, list):
                raise VitaDockError(f"unexpected response for {module}: {str(page)[:200]}")
            out.extend(page)
            if len(page) < PAGE_SIZE:
                return out
            start += PAGE_SIZE


class Form(NamedTuple):
    action: str
    fields: dict[str, str]
    method: str  # "get" or "post"


def _parse_form(page: str, form_id: str | None = None) -> Form | None:
    """Return action, fields and method of a form, including the submit button a user would click.

    Consent forms often only record approval via the clicked button's name/value,
    so pick the approving button when there are several.
    """
    for m in re.finditer(r"<form\b([^>]*)>(.*?)</form>", page, re.S | re.I):
        attrs, body = m.group(1), m.group(2)
        if form_id and not re.search(rf'\bid="{form_id}"', attrs):
            continue
        action = re.search(r'\baction="([^"]*)"', attrs)
        fields: dict[str, str] = {}
        submits: list[tuple[str, str, str]] = []
        for tag in re.finditer(r"<input\b[^>]*>|<button\b[^>]*>.*?</button>", body, re.I | re.S):
            t = tag.group(0)
            name = re.search(r'\bname="([^"]*)"', t)
            value = re.search(r'\bvalue="([^"]*)"', t)
            val = html.unescape(value.group(1)) if value else ""
            is_submit = t.lower().startswith("<button") and not re.search(r'type="(button|reset)"', t, re.I) \
                or re.search(r'type="(submit|image)"', t, re.I)
            if is_submit:
                if name:
                    label = re.sub(r"<[^>]+>", " ", t) + " " + val
                    submits.append((name.group(1), val, label.lower()))
                continue
            if not name or re.search(r'type="(checkbox|radio)"', t, re.I) and "checked" not in t.lower():
                continue
            fields[name.group(1)] = val
        if submits:
            approve = re.compile(r"accept|allow|approv|authori|grant|confirm|agree|zustimm|erlaub|akzept|"
                                 r"bestätig|toestaan|akkoord|\bok\b|\byes\b|\btrue\b|\bja\b")
            deny = re.compile(r"deny|reject|cancel|declin|ablehn|abbrech|weiger|annuleer|\bno\b|\bfalse\b")
            best = next((s for s in submits if approve.search(s[2]) and not deny.search(s[2])),
                        next((s for s in submits if not deny.search(s[2])), submits[0]))
            fields[best[0]] = best[1]
        method = re.search(r'\bmethod="([^"]*)"', attrs)
        return Form(html.unescape(action.group(1)) if action else "", fields,
                    method.group(1).lower() if method else "get")
    return None

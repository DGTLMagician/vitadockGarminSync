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
import logging
import os
import re
import secrets
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any

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
        r = self.http.post(url, headers={"Authorization": oauth_header("POST", url)}, timeout=30)
        if r.status_code != 200:
            raise AuthError(f"request token failed: HTTP {r.status_code} {r.text[:200]}")
        data = urllib.parse.parse_qs(r.text)
        return data["oauth_token"][0], data["oauth_token_secret"][0]

    def _portal_login(self, request_token: str, email: str, password: str) -> str:
        """Log in on the web portal and return the OAuth verifier.

        The portal ends by redirecting to ``vitadock2://callback?...oauth_verifier=``,
        which is where the app's web view picks it up; here the redirect chain
        is followed by hand so that the custom-scheme Location can be read.
        """
        url = f"{self.base}/portalv2/desiredaccessrights/request?oauth_token={_q(request_token)}"
        r = self._follow(self.http.get(url, allow_redirects=False, timeout=30))
        if (v := self._verifier(r)) is not None:
            return v

        form = _parse_form(r.text, form_id="User")
        if form is None:
            raise AuthError("login form not found on VitaDock portal")
        action, fields = form
        fields.update({"j_username": email, "j_password": password})
        r = self.http.post(urllib.parse.urljoin(r.url, action), data=fields,
                           allow_redirects=False, timeout=30)
        r = self._follow(r)

        # Some accounts get a consent page ("desired access rights") first.
        for _ in range(3):
            if (v := self._verifier(r)) is not None:
                return v
            if "j_password" in r.text or "error" in r.url:
                raise AuthError("VitaDock login rejected (wrong e-mail or password?)")
            form = _parse_form(r.text)
            if form is None:
                break
            action, fields = form
            r = self._follow(self.http.post(urllib.parse.urljoin(r.url, action), data=fields,
                                            allow_redirects=False, timeout=30))
        raise AuthError(f"no OAuth verifier received from portal (ended at {r.url})")

    def _follow(self, r: requests.Response) -> requests.Response:
        for _ in range(10):
            loc = r.headers.get("Location")
            if not r.is_redirect or not loc or loc.startswith(CALLBACK_SCHEME):
                return r
            r = self.http.get(urllib.parse.urljoin(r.url, loc), allow_redirects=False, timeout=30)
        raise AuthError("too many redirects on VitaDock portal")

    @staticmethod
    def _verifier(r: requests.Response) -> str | None:
        candidates = [r.headers.get("Location", "")]
        candidates += re.findall(r"vitadock2://[^\"'\s<>]+", r.text)
        for loc in candidates:
            if loc.startswith(CALLBACK_SCHEME):
                query = urllib.parse.urlsplit(html.unescape(loc)).query
                v = urllib.parse.parse_qs(query).get("oauth_verifier")
                if v:
                    return v[0]
        return None

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


def _parse_form(page: str, form_id: str | None = None) -> tuple[str, dict[str, str]] | None:
    for m in re.finditer(r"<form\b([^>]*)>(.*?)</form>", page, re.S | re.I):
        attrs, body = m.group(1), m.group(2)
        if form_id and not re.search(rf'\bid="{form_id}"', attrs):
            continue
        action = re.search(r'\baction="([^"]*)"', attrs)
        fields = {}
        for inp in re.finditer(r"<input\b[^>]*>", body, re.I):
            tag = inp.group(0)
            name = re.search(r'\bname="([^"]*)"', tag)
            if not name or re.search(r'type="(submit|button)"', tag):
                continue
            value = re.search(r'\bvalue="([^"]*)"', tag)
            fields[name.group(1)] = html.unescape(value.group(1)) if value else ""
        return html.unescape(action.group(1)) if action else "", fields
    return None

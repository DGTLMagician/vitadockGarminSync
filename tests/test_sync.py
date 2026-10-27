import base64
import hashlib
import hmac
import urllib.parse
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest
import requests

from vitadock_garmin_sync.sync import State, Syncer, measured_at
from vitadock_garmin_sync.vitadock import VitaDockClient, _parse_form, oauth_header


@pytest.fixture(autouse=True)
def consumer_env(monkeypatch):
    monkeypatch.setenv("VITADOCK_CONSUMER_KEY", "test-key")
    monkeypatch.setenv("VITADOCK_CONSUMER_SECRET", "test-secret")


def parse_header(h):
    assert h.startswith("OAuth ")
    return {k: urllib.parse.unquote(v.strip('"'))
            for k, v in (p.split("=", 1) for p in h[6:].split(","))}


def test_signature_includes_query_and_token_secret():
    url = "https://cloud.vitadock.com/data/targetscales"
    q = {"start": "0", "max": "100", "date_since": "0"}
    p = parse_header(oauth_header("GET", url, token="tok", token_secret="sec", query=q,
                                  nonce="n", timestamp="1"))
    assert p["oauth_token"] == "tok" and p["oauth_signature_method"] == "HMAC-SHA256"
    assert "start" not in p  # query params are signed, not sent in the header
    params = {k: v for k, v in p.items() if k != "oauth_signature"} | q
    norm = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    base = "GET&" + urllib.parse.quote(url, safe="") + "&" + urllib.parse.quote(norm, safe="")
    assert p["oauth_consumer_key"] == "test-key"
    key = b"test-secret&sec"
    expected = base64.b64encode(hmac.new(key, base.encode(), hashlib.sha256).digest()).decode()
    assert p["oauth_signature"] == expected


def test_parse_login_form():
    page = '''<form id="Other" action="/x"></form>
    <form id="User" name="User" action="/portalv2/j_spring_security_check" method="POST">
    <input name="j_username" type="email"/><input name="j_password" type="password"/>
    <input id="proceed" value="Log in" type="submit"/>
    <input value="TOKEN" name="oauth_token" type="hidden"/>
    <input type="hidden" name="_csrf" value="abc" /></form>'''
    action, fields = _parse_form(page, form_id="User")
    assert action == "/portalv2/j_spring_security_check"
    assert fields == {"j_username": "", "j_password": "", "oauth_token": "TOKEN", "_csrf": "abc"}


def response(location=None, body=""):
    r = requests.Response()
    r.status_code = 302 if location else 200
    if location:
        r.headers["Location"] = location
    r._content = body.encode()
    r.url = "https://cloud.vitadock.com/portalv2/permission/thanks"
    return r


def test_verifier_from_callback_redirect():
    r = response("vitadock2://callback?oauth_token=T&oauth_verifier=V123")
    assert VitaDockClient()._verifier(r) == "V123"


def test_verifier_from_page_or_earlier_hop():
    c = VitaDockClient()
    assert c._verifier(response(body='<input type="hidden" name="oauth_verifier" value="abc-9"/>')) == "abc-9"
    page = '<p>Code: <input name="x" value="1"></p><a href="vitadock2://callback/?oauth_token=T&amp;oauth_verifier=Q7">'
    assert c._verifier(response(body=page)) == "Q7"
    c._hops = [response("/portalv2/permission/thanks?oauth_verifier=HOP1")]
    assert c._verifier(response(body="<h1>Thanks</h1>")) == "HOP1"


def test_consent_form_picks_approve_button():
    page = '''<form action="/portalv2/desiredaccessrights/grant" method="POST">
    <input type="hidden" name="oauth_token" value="T"/>
    <input type="checkbox" name="share" value="on" checked/>
    <input type="checkbox" name="newsletter" value="on"/>
    <button type="submit" name="decision" value="deny">Deny</button>
    <button type="submit" name="decision" value="allow">Allow access</button></form>'''
    action, fields = _parse_form(page)
    assert action == "/portalv2/desiredaccessrights/grant"
    assert fields == {"oauth_token": "T", "share": "on", "decision": "allow"}


def test_consent_form_with_submit_inputs():
    page = '''<form action="/grant"><input type="submit" name="cancel" value="Abbrechen"/>
    <input type="submit" name="accept" value="Zustimmen"/></form>'''
    assert _parse_form(page)[1] == {"accept": "Zustimmen"}


def test_measured_at_utc_vs_wallclock():
    rec = {"measurementDate": 1_700_000_000_000}  # 2023-11-14T22:13:20Z
    tz = ZoneInfo("Europe/Amsterdam")
    assert measured_at(rec, tz, wallclock=False).isoformat() == "2023-11-14T23:13:20+01:00"
    assert measured_at(rec, tz, wallclock=True).isoformat() == "2023-11-14T22:13:20+01:00"


class FakeVitaDock:
    def __init__(self, data):
        self.data = data
        self.calls = []

    def measurements(self, module, since_ms=0):
        self.calls.append((module, since_ms))
        return self.data.get(module, [])


class FakeGarmin:
    def __init__(self):
        self.weights, self.bp = [], []

    def add_body_composition(self, ts, weight, **kw):
        self.weights.append((ts, weight, kw))

    def set_blood_pressure(self, s, d, p=None, timestamp="", notes=""):
        self.bp.append((s, d, p, timestamp))


def test_sync_uploads_once_and_skips_inactive(tmp_path):
    data = {
        "targetscales": [
            {"id": "a", "measurementDate": 1_700_000_000_000, "bodyWeight": 80.0,
             "bodyFat": 20.0, "bodyWater": 55.0, "muscleMass": 40.0, "boneMass": 3.2,
             "kcal": 1800, "active": True},
            {"id": "b", "measurementDate": 1_700_000_100_000, "bodyWeight": 81.0, "active": False},
        ],
        "cardiodocks": [{"id": "c", "measurementDate": 1_700_000_000_000,
                         "systole": 120, "diastole": 80, "pulse": 60}],
    }
    garmin = FakeGarmin()
    state = State(tmp_path / "state.json")
    s = Syncer(FakeVitaDock(data), garmin, state)
    assert s.run() == 2
    ts, weight, kw = garmin.weights[0]
    assert weight == 80.0 and kw["muscle_mass"] == 32.0 and kw["percent_fat"] == 20.0
    assert garmin.bp == [(120, 80, 60, "2023-11-14T23:13:20+01:00")]

    # Second run with a reloaded state uploads nothing new.
    s2 = Syncer(FakeVitaDock(data), garmin, State.load(tmp_path / "state.json"))
    assert s2.run() == 0
    assert len(garmin.weights) == 1


def test_initial_since_filters_old_records(tmp_path):
    data = {"targetscales": [{"id": "a", "measurementDate": 1_700_000_000_000, "bodyWeight": 80}]}
    garmin = FakeGarmin()
    s = Syncer(FakeVitaDock(data), garmin, State(tmp_path / "s.json"),
               initial_since=datetime(2024, 1, 1, tzinfo=timezone.utc), modules=("targetscales",))
    assert s.run() == 0

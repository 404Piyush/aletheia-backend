"""Security regression tests (audit Phase 3 fixes)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi.testclient import TestClient

os.environ["DB_PATH"] = "/tmp/test_trust.db"
os.environ["ANCHOR_STORE"] = "/tmp/test_anchors.json"
for f in ("/tmp/test_trust.db", "/tmp/test_anchors.json"):
    try:
        os.remove(f)
    except FileNotFoundError:
        pass
os.environ["REDTEAM_MODE"] = "1"

from backend.main import app, require_local
from backend import chain as ch

import pytest
from fastapi import HTTPException

client = TestClient(app)


@pytest.fixture(autouse=True)
def _isolated_db(monkeypatch):
    monkeypatch.setenv("DB_PATH", "/tmp/test_trust.db")
    monkeypatch.setenv("ANCHOR_STORE", "/tmp/test_anchors.json")
    for f in ("/tmp/test_trust.db", "/tmp/test_anchors.json"):
        try:
            os.remove(f)
        except FileNotFoundError:
            pass
    yield


class _Req:
    def __init__(self, host):
        self.client = type("C", (), {"host": host})()


def test_require_local_gate():
    for h in ("127.0.0.1", "::1", "localhost", "testclient"):
        require_local(_Req(h))  # no raise
    for h in ("10.0.0.5", "203.0.113.9", "", "tunnel.cloudflared"):
        with pytest.raises(HTTPException) as ex:
            require_local(_Req(h))
        assert ex.value.status_code == 403


def _seed():
    r = client.post("/seed/demo")
    assert r.status_code == 200, r.text
    r = client.post("/lots", json={"lot_id": "LOT-SEC", "size": 2, "manufacturer": "did:maker1"})
    assert r.status_code == 200, r.text
    return r.json()


def test_event_input_validation():
    _seed()
    s = "LOT-SEC-000001"
    import time
    now = int(time.time())
    bad = [
        {"serial": s, "type": "SCAN", "actor": "did:transporter1", "ts": now + 3600},
        {"serial": s, "type": "SCAN", "actor": "did:transporter1", "ts": -5},
        {"serial": s, "type": "SCAN", "actor": "did:transporter1", "lat": 9999.0, "lon": 0.0},
        {"serial": s, "type": "SCAN", "actor": "did:transporter1", "lat": 0.0, "lon": 200.0},
        {"serial": s, "type": "PWNED", "actor": "did:transporter1"},
        {"serial": "X" * 200, "type": "SCAN", "actor": "did:transporter1"},
    ]
    for ev in bad:
        r = client.post("/events/sign", json=ev)
        assert r.status_code == 422, (ev, r.status_code, r.text)
    r = client.post("/events/sign", json={"serial": s, "type": "SCAN",
                                          "actor": "did:transporter1",
                                          "lat": 19.07, "lon": 72.87})
    assert r.status_code == 200, r.text


def test_claim_empty_session_rejected():
    lot = _seed()
    sec = lot["sample_secrets"]["LOT-SEC-000001"]
    s = "LOT-SEC-000001"
    client.post("/events/sign", json={"serial": s, "type": "SOLD", "actor": "did:retailer1"})
    r = client.post(f"/products/{s}/claim", json={"secret": sec, "session_id": ""})
    assert r.status_code == 422, r.text
    r = client.post(f"/products/{s}/claim", json={"secret": sec, "session_id": "   "})
    assert r.status_code == 422, r.text
    assert client.post(f"/products/{s}/claim",
                       json={"secret": sec, "session_id": "a"}).json()["status"] == "CLAIMED"
    r = client.post(f"/products/{s}/claim", json={"secret": sec, "session_id": "b"})
    assert r.json()["rule"] == "R12", r.text


def test_freeze_fail_closed(monkeypatch):
    _seed()
    s = "LOT-SEC-000001"

    def _boom(_lot):
        raise RuntimeError("rpc down")
    monkeypatch.setattr(ch, "is_frozen", _boom)
    r = client.post("/events/sign", json={"serial": s, "type": "SCAN",
                                          "actor": "did:transporter1"})
    assert r.status_code == 503, (r.status_code, r.text)

    monkeypatch.setattr(ch, "freeze_lot", _boom)
    r = client.post("/lots/LOT-SEC/recall")
    assert r.status_code == 502, (r.status_code, r.text)
    # DB must NOT be marked frozen when the chain freeze failed
    import sqlite3
    con = sqlite3.connect("/tmp/test_trust.db")
    assert con.execute("SELECT frozen FROM lots WHERE lot_id='LOT-SEC'").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) n FROM products WHERE lot_id='LOT-SEC' AND status='FROZEN'").fetchone()[0] == 0


def test_lot_reregister_root_mismatch():
    _seed()
    r = client.post("/lots", json={"lot_id": "LOT-SEC", "size": 7, "manufacturer": "did:maker1"})
    assert r.status_code == 409, (r.status_code, r.text)


def test_unknown_serial_flood_throttled():
    _seed()
    last = None
    for i in range(55):
        last = client.get(f"/products/LOT-SEC-FAKE{i:04d}/verify")
    assert last.status_code == 429, (last.status_code, last.text)


def test_cors_restricted_by_default():
    r = client.get("/health", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in {k.lower() for k in r.headers}, dict(r.headers)
    r = client.get("/health", headers={"Origin": "http://localhost:3001"})
    assert r.headers.get("access-control-allow-origin") == "http://localhost:3001"

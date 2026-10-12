"""Red-team console tests (USP 7). In-process TestClient passes the localhost
gate; REDTEAM_MODE is enabled per-test. One test per attack button."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi.testclient import TestClient

from backend.main import app
from backend.main import con as app_con

client = TestClient(app, raise_server_exceptions=False)


def _enable_redteam(monkeypatch):
    monkeypatch.setenv("REDTEAM_MODE", "1")


def _attacked_serial():
    # /redteam/* acts on the first product in the active DB; discover it the
    # same way instead of assuming test order or DB file.
    c = app_con()
    try:
        row = c.execute("SELECT serial FROM products LIMIT 1").fetchone()
    finally:
        c.close()
    assert row is not None, "need a seeded lot: POST /lots first"
    return row["serial"]


def _wipe_and_seed():
    # The suite shares one scratch DB file (last-imported module's DB_PATH
    # wins at request time) and /redteam/* acts on LIMIT 1. Wipe the
    # product/event tables so the attacked serial is deterministically ours.
    # Later test modules create their own lots; nothing depends on these rows.
    c = app_con()
    try:
        for t in ("products", "events", "claims", "unknown_attempts"):
            c.execute(f"DELETE FROM {t}")
        c.commit()
    finally:
        c.close()
    r = client.post("/lots", json={"lot_id": "REDTEAM-T", "size": 2,
                                   "manufacturer": "did:maker1"})
    assert r.status_code == 200, r.text


def test_clone_button_produces_two_signal_clone(monkeypatch):
    _enable_redteam(monkeypatch)
    _wipe_and_seed()
    r = client.post("/redteam/clone")
    assert r.status_code == 200, r.text
    serial = _attacked_serial()
    assert serial.startswith("REDTEAM-T-"), serial
    v = client.get(f"/products/{serial}/verify").json()
    assert v["verdict"] == "CLONE_DETECTED", v
    failed = {c["id"] for c in v["checks"] if c["status"] == "fail"}
    assert {"R04", "R07"} <= failed, failed
    assert v["risk_score"] == 65, v  # critical 40 + high 25, nothing else


def test_unknown_attack_is_rejected(monkeypatch):
    _enable_redteam(monkeypatch)
    _wipe_and_seed()
    r = client.post("/redteam/rm -rf")
    assert r.status_code in (400, 404, 422), r.status_code


def test_forge_button_rejects_wrong_key(monkeypatch):
    _enable_redteam(monkeypatch)
    _wipe_and_seed()
    r = client.post("/redteam/forge")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["rule"] == "R08/R09", body
    assert body["code"] in (400, 401, 422), body
    assert body["rejected"], body


def test_forge_leaves_no_trace(monkeypatch):
    _enable_redteam(monkeypatch)
    _wipe_and_seed()
    client.post("/redteam/forge")
    serial = _attacked_serial()
    assert serial.startswith("REDTEAM-T-"), serial
    v = client.get(f"/products/{serial}/verify").json()
    # Rejected forgery never entered history: unit stays clean.
    assert v["verdict"] == "VERIFIED", v
    bundle = client.get(f"/products/{serial}/bundle").json()
    actors = {e.get("actor") for e in bundle.get("events", [])}
    assert "did:transporter1" not in actors, actors


def test_redteam_gate_needs_redteam_mode(monkeypatch):
    _enable_redteam(monkeypatch)
    _wipe_and_seed()
    monkeypatch.delenv("REDTEAM_MODE", raising=False)
    r = client.post("/redteam/clone")
    assert r.status_code == 403, r.status_code

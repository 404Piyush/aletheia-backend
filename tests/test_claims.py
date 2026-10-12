"""Claim lifecycle tests (USP 6). SOLD -> CLAIMED -> CONTESTED, wrong-secret
handling, R02 burst throttle."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ["DB_PATH"] = "/tmp/test_claims.db"
os.environ["ANCHOR_STORE"] = "/tmp/test_claims_anchors.json"
for _f in ("/tmp/test_claims.db", "/tmp/test_claims_anchors.json"):
    try:
        os.remove(_f)
    except FileNotFoundError:
        pass

from fastapi.testclient import TestClient

from backend.main import app
from backend.main import con as app_con

client = TestClient(app, raise_server_exceptions=False)


def _wipe_and_seed():
    c = app_con()
    try:
        for t in ("products", "events", "claims", "unknown_attempts"):
            c.execute(f"DELETE FROM {t}")
        c.commit()
    finally:
        c.close()
    r = client.post("/lots", json={"lot_id": "CLAIM-T", "size": 2,
                                   "manufacturer": "did:maker1"})
    assert r.status_code == 200, r.text
    secrets = r.json()["sample_secrets"]
    assert "CLAIM-T-000001" in secrets and "CLAIM-T-000002" in secrets
    return secrets


def _sold(serial):
    r = client.post("/events/sign", json={"serial": serial, "type": "SOLD",
                                           "actor": "did:retailer1"})
    assert r.status_code == 200, r.text


def _status(serial):
    d = client.get("/lots/CLAIM-T").json()
    return next(u["status"] for u in d["units"] if u["serial"] == serial)


def test_claim_lifecycle_to_contested():
    secrets = _wipe_and_seed()
    s = "CLAIM-T-000001"
    _sold(s)
    assert _status(s) == "SOLD"
    r = client.post(f"/products/{s}/claim",
                    json={"secret": secrets[s], "session_id": "buyer-1"})
    assert r.json() == {"ok": True, "status": "CLAIMED"}, r.text
    assert _status(s) == "CLAIMED"
    r = client.post(f"/products/{s}/claim",
                    json={"secret": secrets[s], "session_id": "buyer-1"})
    assert r.json().get("idempotent") is True, r.text
    r = client.post(f"/products/{s}/claim",
                    json={"secret": secrets[s], "session_id": "buyer-2"})
    assert r.json()["rule"] == "R12", r.text
    assert _status(s) == "CONTESTED"


def test_wrong_secret_rejected_and_burst_throttled():
    secrets = _wipe_and_seed()
    s = "CLAIM-T-000002"
    _sold(s)
    r = client.post(f"/products/{s}/claim",
                    json={"secret": "nope", "session_id": "buyer-1"})
    assert r.json() == {"ok": False, "reason": "invalid"}, r.text
    assert _status(s) == "SOLD"
    for i in range(4):
        client.post(f"/products/{s}/claim",
                    json={"secret": "nope", "session_id": f"b-{i}"})
    r = client.post(f"/products/{s}/claim",
                    json={"secret": "nope", "session_id": "buyer-9"})
    assert r.status_code == 429, (r.status_code, r.text)

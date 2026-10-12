"""C2: caseId + checks[] in incident payload; verdict gating on /converge/case."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi.testclient import TestClient

os.environ["DB_PATH"] = "/tmp/test_c2.db"
os.environ["ANCHOR_STORE"] = "/tmp/test_c2_anchors.json"
for f in ("/tmp/test_c2.db", "/tmp/test_c2_anchors.json"):
    try:
        os.remove(f)
    except FileNotFoundError:
        pass

import pytest  # noqa: E402

from backend.main import app  # noqa: E402
import backend.convergence as cv  # noqa: E402

client = TestClient(app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def _live_partner_down(monkeypatch):
    class Boom:
        def __init__(self, *a, **k):
            raise ConnectionError("partner down in test")

    monkeypatch.setattr(cv.httpx, "post", Boom)
    monkeypatch.setattr(cv.httpx, "get", Boom)
    monkeypatch.setattr(cv, "OUTBOX", "/tmp/test_c2_outbox.jsonl")
    try:
        os.remove("/tmp/test_c2_outbox.jsonl")
    except FileNotFoundError:
        pass
    # Reset the global dispatch throttle: it lives in whichever DB is active
    # (same one the TestClient requests hit), so clear it via the app conn.
    from backend.main import con as app_con
    c = app_con()
    try:
        c.execute("DELETE FROM meta WHERE key='last_dispatch'")
        c.commit()
    except Exception:
        pass
    finally:
        c.close()
    yield


def test_case_id_and_checks_in_queued_payload():
    r = client.post("/lots", json={"lot_id": "C2T", "size": 1, "manufacturer": "did:maker1"})
    assert r.status_code == 200, r.text
    r = client.post("/converge/case", json={"serial": "C2T-000001", "force": True})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["caseId"].startswith("TF-C2T-000001-"), body
    import json as _json
    with open("/tmp/test_c2_outbox.jsonl") as f:
        lines = f.readlines()
    assert lines, "dispatch should have queued while partner is down"
    payload = _json.loads(lines[-1])["payload"]
    assert payload["caseId"] == body["caseId"]
    assert isinstance(payload["checks"], list) and payload["checks"], payload


def test_verified_refused_without_force(monkeypatch):
    import backend.main as _main
    v = {"verdict": "VERIFIED", "risk_score": 0, "confidence": "high",
         "product": {"lot_id": "C2V"}, "checks": [], "next_steps": []}
    monkeypatch.setattr(_main, "build_verdict", lambda serial: v)
    r = client.post("/converge/case", json={"serial": "C2V-000001"})
    assert r.status_code == 422, r.text
    assert "VERIFIED" in r.json()["detail"]
    r = client.post("/converge/case", json={"serial": "C2V-000001", "force": True})
    assert r.status_code == 200, r.text


def test_unknown_serial_refused_even_with_force():
    r = client.post("/converge/case", json={"serial": "NOPE-NOT-REAL", "force": True})
    assert r.status_code == 422, r.text


def test_handoff_ids_stored_and_returned(monkeypatch):
    import backend.main as _main
    monkeypatch.setattr(
        cv, "dispatch_case",
        lambda s, v, u, case_id=None: {"ok": True, "via": "incidents", "caseId": case_id,
                                       "partner": {"caseId": case_id, "handoffId": "HO-1",
                                                   "incidentId": "INC-1"}})
    v = {"verdict": "TAMPERED", "risk_score": 40, "confidence": "high",
         "product": {"lot_id": "C2H"}, "checks": [], "next_steps": []}
    monkeypatch.setattr(_main, "build_verdict", lambda serial: v)
    r = client.post("/lots", json={"lot_id": "C2H", "size": 1, "manufacturer": "did:maker1"})
    assert r.status_code == 200, r.text
    r = client.post("/converge/case", json={"serial": "C2H-000001"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["handoffId"] == "HO-1" and body["incidentId"] == "INC-1"
    from backend.main import con as app_con
    c = app_con()
    try:
        row = c.execute("SELECT case_id,handoff_id,incident_id FROM converge_cases "
                        "WHERE serial='C2H-000001'").fetchone()
    finally:
        c.close()
    assert row is not None and tuple(row) == (body["caseId"], "HO-1", "INC-1"), row

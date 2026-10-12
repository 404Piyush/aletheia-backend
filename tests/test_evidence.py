"""Evidence completeness (USP 2). Every failed check in a verdict must carry
a human-readable reason, evidence refs, and a next step — the live drill
once showed blank LOT_COMMITMENT reasons in the partner briefing."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ["DB_PATH"] = "/tmp/test_evid.db"
os.environ["ANCHOR_STORE"] = "/tmp/test_evid_anchors.json"
for _f in ("/tmp/test_evid.db", "/tmp/test_evid_anchors.json"):
    try:
        os.remove(_f)
    except FileNotFoundError:
        pass

from fastapi.testclient import TestClient

from backend.main import app
from backend.main import con as app_con

client = TestClient(app, raise_server_exceptions=False)


def _fresh_attack(monkeypatch, attack):
    from backend.main import con as app_con
    monkeypatch.setenv("REDTEAM_MODE", "1")
    c = app_con()
    try:
        for t in ("products", "events", "claims", "unknown_attempts"):
            c.execute(f"DELETE FROM {t}")
        c.commit()
        c.execute("SELECT 1")
    finally:
        c.close()
    r = client.post("/lots", json={"lot_id": "EVID-T", "size": 1,
                                   "manufacturer": "did:maker1"})
    assert r.status_code == 200, r.text
    for i, did in enumerate(("did:maker1", "did:attacker", "did:attacker2")):
        r = client.post("/actors", json={"did": did, "pubkey": "0x" + f"{i + 1:02x}" * 32,
                                         "role": "demo"})
        assert r.status_code == 200, r.text
    r = client.post(f"/redteam/{attack}")
    assert r.status_code == 200, r.text
    c = app_con()
    try:
        serial = c.execute("SELECT serial FROM products LIMIT 1").fetchone()["serial"]
    finally:
        c.close()
    assert serial.startswith("EVID-T-"), serial
    return client.get(f"/products/{serial}/verify").json()


def _assert_evidence_complete(v):
    failed = [c for c in v["checks"] if c["status"] == "fail"]
    assert failed, f"expected failing checks in {v['verdict']}"
    for c in failed:
        assert (c.get("reason") or "").strip(), f"hollow reason in {c['id']}"
        assert c.get("evidence_refs"), f"no evidence refs in {c['id']}"
        assert (c.get("next_step") or "").strip(), f"no next step in {c['id']}"


def test_clone_evidence_complete(monkeypatch):
    v = _fresh_attack(monkeypatch, "clone")
    assert v["verdict"] == "CLONE_DETECTED"
    _assert_evidence_complete(v)


def test_handover_evidence_complete(monkeypatch):
    v = _fresh_attack(monkeypatch, "handover")
    assert v["verdict"] == "SUSPICIOUS"
    _assert_evidence_complete(v)


def test_tamper_evidence_complete(monkeypatch):
    v = _fresh_attack(monkeypatch, "tamper")
    assert v["verdict"] == "TAMPERED"
    _assert_evidence_complete(v)

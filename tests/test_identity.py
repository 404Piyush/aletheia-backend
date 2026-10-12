"""Crypto-identity rules (USP 1/3/4): R08 unregistered actor, R03 lot
inclusion. The actor registry file is patched to a fresh temp file so
results don't depend on stale /tmp state from earlier runs."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ["DB_PATH"] = "/tmp/test_ident.db"
os.environ["ANCHOR_STORE"] = "/tmp/test_ident_anchors.json"
for _f in ("/tmp/test_ident.db", "/tmp/test_ident_anchors.json"):
    try:
        os.remove(_f)
    except FileNotFoundError:
        pass

from fastapi.testclient import TestClient

from backend.main import app
from backend.main import con as app_con
from backend import chain as ch

client = TestClient(app, raise_server_exceptions=False)
STORE = "/tmp/test_ident_anchors.json"


def _fresh_env(monkeypatch):
    try:
        os.remove(STORE)
    except FileNotFoundError:
        pass
    monkeypatch.setattr(ch, "STORE", STORE)
    c = app_con()
    try:
        for t in ("products", "events", "claims", "unknown_attempts", "actors"):
            c.execute(f"DELETE FROM {t}")
        c.commit()
    finally:
        c.close()
    r = client.post("/lots", json={"lot_id": "IDENT-T", "size": 1,
                                   "manufacturer": "did:maker1"})
    assert r.status_code == 200, r.text


def _offer(actor):
    return client.post("/events/sign",
                       json={"serial": "IDENT-T-000001", "type": "HANDOVER_OFFER",
                             "actor": actor, "to_actor": "did:retailer1"})


def test_r08_unregistered_actor_is_tampered(monkeypatch):
    _fresh_env(monkeypatch)
    r = _offer("did:stranger99")
    assert r.status_code == 200, r.text  # stored unsigned, judged at verify
    v = client.get("/products/IDENT-T-000001/verify").json()
    failed = {c["id"] for c in v["checks"] if c["status"] == "fail"}
    assert "R08" in failed, failed
    assert v["verdict"] == "TAMPERED", v


def test_r08_registered_actor_passes(monkeypatch):
    _fresh_env(monkeypatch)
    r = client.post("/actors", json={"did": "did:maker1", "pubkey": "0x" + "aa" * 32,
                                     "role": "manufacturer"})
    assert r.status_code == 200, r.text
    r = client.post("/events/sign",
                    json={"serial": "IDENT-T-000001", "type": "HANDOVER_OFFER",
                          "actor": "did:maker1", "to_actor": "did:transporter1"})
    assert r.status_code == 200, r.text
    v = client.get("/products/IDENT-T-000001/verify").json()
    failed = {c["id"] for c in v["checks"] if c["status"] == "fail"}
    assert "R08" not in failed, failed
    assert "R07" not in failed, failed  # maker1 is the custodian: clean offer
    assert v["verdict"] == "VERIFIED", v


def test_r03_root_mismatch_is_clone(monkeypatch):
    _fresh_env(monkeypatch)
    c = app_con()
    try:
        c.execute("UPDATE lots SET root=? WHERE lot_id='IDENT-T'",
                  ("0x" + "ff" * 32,))
        c.commit()
    finally:
        c.close()
    v = client.get("/products/IDENT-T-000001/verify").json()
    by_id = {c["id"]: c for c in v["checks"]}
    assert by_id["LOT_COMMITMENT"]["status"] == "fail", by_id["LOT_COMMITMENT"]
    assert by_id["LOT_COMMITMENT"]["reason"].strip(), "summary check must carry evidence"
    assert by_id["LOT_COMMITMENT"]["evidence_refs"], "summary check must cite refs"
    assert "R03" in {c["id"] for c in v["checks"] if c["status"] == "fail"}
    assert v["verdict"] == "CLONE_DETECTED", v

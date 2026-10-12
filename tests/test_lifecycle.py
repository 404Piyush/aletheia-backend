"""Lifecycle rules (USP 1/6): R11 post-sale scan, R02 pre-sale claim,
R13 tap-counter regression."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ["DB_PATH"] = "/tmp/test_life.db"
os.environ["ANCHOR_STORE"] = "/tmp/test_life_anchors.json"
for _f in ("/tmp/test_life.db", "/tmp/test_life_anchors.json"):
    try:
        os.remove(_f)
    except FileNotFoundError:
        pass

from fastapi.testclient import TestClient

from backend.main import app
from backend.main import con as app_con

client = TestClient(app, raise_server_exceptions=False)


def _fresh():
    c = app_con()
    try:
        for t in ("products", "events", "claims", "unknown_attempts"):
            c.execute(f"DELETE FROM {t}")
        c.commit()
    finally:
        c.close()
    r = client.post("/actors", json={"did": "did:maker1", "pubkey": "0x" + "aa" * 32,
                                     "role": "demo"})
    assert r.status_code == 200, r.text
    for i, did in enumerate(("did:transporter1", "did:retailer1")):
        r = client.post("/actors", json={"did": did, "pubkey": "0x" + f"{i + 2:02x}" * 32,
                                         "role": "demo"})
        assert r.status_code == 200, r.text
    r = client.post("/lots", json={"lot_id": "LIFE-T", "size": 1,
                                   "manufacturer": "did:maker1"})
    assert r.status_code == 200, r.text
    return r.json()["sample_secrets"]["LIFE-T-000001"]


def _verify():
    return client.get("/products/LIFE-T-000001/verify").json()


def _failed(v):
    return {c["id"] for c in v["checks"] if c["status"] == "fail"}


def test_r11_scan_after_sold():
    import time as _time
    _fresh()
    base = int(_time.time()) - 120
    client.post("/events/sign", json={"serial": "LIFE-T-000001", "type": "SOLD",
                                      "actor": "did:maker1", "ts": base})
    client.post("/events/sign", json={"serial": "LIFE-T-000001", "type": "SCAN",
                                      "actor": "did:maker1", "ts": base + 60})
    v = _verify()
    assert "R11" in _failed(v), _failed(v)
    assert v["verdict"] == "SUSPICIOUS", v


def test_r02_claim_before_sale():
    secret = _fresh()
    r = client.post("/products/LIFE-T-000001/claim",
                    json={"secret": secret, "session_id": "early-buyer"})
    assert r.json()["rule"] == "R02", r.text
    v = _verify()
    assert "R02" in _failed(v), _failed(v)


def test_r13_counter_regression():
    _fresh()
    for n in (5, 3):
        r = client.post("/events/sign", json={"serial": "LIFE-T-000001", "type": "SCAN",
                                              "actor": "did:maker1", "counter": n})
        assert r.status_code == 200, r.text
    v = _verify()
    assert "R13" in _failed(v), _failed(v)
    assert v["verdict"] == "SUSPICIOUS", v


def test_r13_monotonic_counters_clean():
    _fresh()
    for n in (3, 5):
        r = client.post("/events/sign", json={"serial": "LIFE-T-000001", "type": "SCAN",
                                              "actor": "did:maker1", "counter": n})
        assert r.status_code == 200, r.text
    v = _verify()
    assert "R13" not in _failed(v), _failed(v)


def test_counter_bounds_rejected():
    _fresh()
    r = client.post("/events/sign", json={"serial": "LIFE-T-000001", "type": "SCAN",
                                          "actor": "did:maker1", "counter": -1})
    assert r.status_code == 422, r.status_code


def test_r05_overproduction_is_clone():
    _fresh()
    c = app_con()
    try:
        c.execute("INSERT INTO products(serial,lot_id,status,current_custodian) VALUES(?,?,?,?)",
                  ("LIFE-T-GHOST", "LIFE-T", "ACTIVE", "did:maker1"))
        c.commit()
    finally:
        c.close()
    v = _verify()
    assert "R05" in _failed(v), _failed(v)
    assert v["verdict"] == "CLONE_DETECTED", v


def test_r05_exact_size_is_silent():
    _fresh()
    v = _verify()
    assert "R05" not in _failed(v), _failed(v)


def test_r06_backdated_custody_step():
    import time as _time
    _fresh()
    base = int(_time.time()) - 300
    client.post("/events/sign", json={"serial": "LIFE-T-000001", "type": "HANDOVER_OFFER",
                                      "actor": "did:maker1", "to_actor": "did:transporter1",
                                      "ts": base})
    client.post("/events/sign", json={"serial": "LIFE-T-000001", "type": "HANDOVER_ACCEPT",
                                      "actor": "did:transporter1", "ts": base - 60})
    v = _verify()
    assert "R06" in _failed(v), _failed(v)
    assert v["verdict"] == "SUSPICIOUS", v

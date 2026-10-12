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

from backend.main import app

import pytest

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


def test_full_flow():
    r = client.post("/seed/demo")
    assert r.status_code == 200, r.text
    r = client.post("/lots", json={"lot_id": "LOT-T1", "size": 5, "manufacturer": "did:maker1"})
    assert r.status_code == 200, r.text
    serial = "LOT-T1-000001"
    # valid scan
    r = client.post("/events/sign", json={"serial": serial, "type": "SCAN",
                                          "actor": "did:transporter1", "lat": 19.07, "lon": 72.87})
    assert r.status_code == 200, r.text
    # clone: far city 20 min later -> R04
    import time
    r = client.post("/events/sign", json={"serial": serial, "type": "SCAN",
                                          "actor": "did:attacker2", "ts": int(time.time()) + 1200,
                                          "lat": 28.61, "lon": 77.20})
    assert r.status_code == 200, r.text
    r = client.get(f"/products/{serial}/verify")
    assert r.status_code == 200, r.text
    v = r.json()
    assert v["verdict"] in ("CLONE_DETECTED", "SUSPICIOUS", "TAMPERED"), v
    assert any(c["id"] == "R04" for c in v["checks"] if c.get("status") == "fail"), v
    # invented serial
    r = client.get("/products/LOT-T1-FAKE9999/verify")
    assert r.json()["verdict"] == "UNKNOWN"
    # anchor + audit
    r = client.post("/anchor/run")
    assert r.status_code == 200
    r = client.get("/audit/integrity")
    assert r.json()["ok"], r.json()
    # tamper then audit fails
    import sqlite3
    import json as _j
    con = sqlite3.connect("/tmp/test_trust.db")
    con.row_factory = sqlite3.Row
    row = con.execute("SELECT rowid AS rid, raw FROM events WHERE serial=? ORDER BY rid LIMIT 1", (serial,)).fetchone()
    raw = _j.loads(row["raw"])
    raw["biz_step"] = "tampered"
    con.execute("UPDATE events SET raw=?, biz_step='tampered' WHERE rowid=?", (_j.dumps(raw), row["rid"]))
    con.commit()
    r = client.get("/audit/integrity")
    assert not r.json()["ok"]

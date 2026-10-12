import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi.testclient import TestClient

os.environ["DB_PATH"] = "/tmp/test_inc.db"
os.environ["ANCHOR_STORE"] = "/tmp/test_inc_anchors.json"
for f in ("/tmp/test_inc.db", "/tmp/test_inc_anchors.json"):
    try:
        os.remove(f)
    except FileNotFoundError:
        pass

from backend.main import app

import pytest

client = TestClient(app)


@pytest.fixture(autouse=True)
def _isolated_db(monkeypatch):
    monkeypatch.setenv("DB_PATH", "/tmp/test_inc.db")
    monkeypatch.setenv("ANCHOR_STORE", "/tmp/test_inc_anchors.json")
    for f in ("/tmp/test_inc.db", "/tmp/test_inc_anchors.json"):
        try:
            os.remove(f)
        except FileNotFoundError:
            pass
    yield


def test_incident_upsert_and_feed():
    r = client.post("/seed/demo")
    assert r.status_code == 200, r.text
    r = client.post("/lots", json={"lot_id": "LOT-I", "size": 2, "manufacturer": "did:maker1"})
    assert r.status_code == 200, r.text
    s = "LOT-I-000001"
    # clean serial -> incident row created, status clear
    r = client.get(f"/products/{s}/verify")
    assert r.status_code == 200
    r = client.get("/incidents")
    inc = {i["serial"]: i for i in r.json()["incidents"]}
    assert inc[s]["status"] == "clear"
    # clone -> same row flips to open, findings present
    import time
    client.post("/events/sign", json={"serial": s, "type": "SCAN", "actor": "did:attacker",
                                      "ts": int(time.time()), "lat": 19.07, "lon": 72.87})
    client.post("/events/sign", json={"serial": s, "type": "SCAN", "actor": "did:attacker2",
                                      "ts": int(time.time()) + 120, "lat": 28.61, "lon": 77.20})
    r = client.get(f"/products/{s}/verify")
    assert r.json()["verdict"] in ("CLONE_DETECTED", "SUSPICIOUS")
    r = client.get("/incidents")
    inc = {i["serial"]: i for i in r.json()["incidents"]}
    assert inc[s]["status"] == "open"
    assert any(f["rule_id"] == "R04" for f in inc[s]["findings"])
    n = len(inc)
    # re-verify must not duplicate rows
    client.get(f"/products/{s}/verify")
    r = client.get("/incidents")
    assert len(r.json()["incidents"]) == n
    # detail interleaves timeline
    r = client.get(f"/incidents/{s}")
    assert r.status_code == 200
    assert len(r.json()["timeline"]) >= 2
    assert r.json()["findings"]
    # unknown serial -> 404, no row
    r = client.get("/incidents/NOPE-1")
    assert r.status_code == 404

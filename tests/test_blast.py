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


def test_tree_seed_and_blast():
    r = client.post("/seed/demo")
    assert r.status_code == 200, r.text
    r = client.post("/seed/tree")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["claimed"]["result"]["status"] == "CLAIMED", body

    r = client.get("/lots/LOT-TREE/blast-radius")
    assert r.status_code == 200, r.text
    b = r.json()
    # flat keys unchanged
    assert b["total_units"] == 8
    assert set(b) >= {"lot", "total_units", "by_status", "by_custodian",
                      "invented_attempts", "produced", "overflow"}
    # nested tree: 2 pallets / 3 cases / 8 units
    assert len(b["tree"]) == 2, b["tree"]
    cases = [cs for p in b["tree"] for cs in p["cases"]]
    assert len(cases) == 3, cases
    units = [u for cs in cases for u in cs["units"]]
    assert len(units) == 8, units
    assert b["claimed_units"] == 1
    assert b["by_status"].get("SOLD", 0) == 1  # 000004 sold, unclaimed
    # claimed unit sits inside the tree with CLAIMED status
    assert any(u["status"] == "CLAIMED" for u in units), units

    # recall freezes 7, CLAIMED survives
    r = client.post("/lots/LOT-TREE/recall")
    assert r.status_code == 200, r.text
    b2 = r.json()
    assert b2["by_status"].get("FROZEN", 0) == 7, b2["by_status"]
    assert b2["by_status"].get("CLAIMED", 0) == 1, b2["by_status"]
    assert b2["claimed_units"] == 1
    units2 = [u for p in b2["tree"] for cs in p["cases"] for u in cs["units"]]
    assert len(units2) == 8
    # frozen lot rejects new custody events
    r = client.post("/events/sign", json={"serial": "LOT-TREE-000005",
                                          "type": "SCAN", "actor": "did:transporter1"})
    assert r.status_code == 423, r.text
    # suite shares the live chain: leave LOT-TREE unfrozen for the demo
    from backend import chain as ch
    ch.freeze_lot("LOT-TREE", False)
    assert not ch.is_frozen("LOT-TREE")

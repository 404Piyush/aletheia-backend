import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi.testclient import TestClient

os.environ["DB_PATH"] = "/tmp/test_sig.db"
os.environ["ANCHOR_STORE"] = "/tmp/test_sig_anchors.json"

from backend.main import app  # noqa: E402

import pytest  # noqa: E402

client = TestClient(app)


@pytest.fixture(autouse=True)
def _isolated_db(monkeypatch):
    monkeypatch.setenv("DB_PATH", "/tmp/test_sig.db")
    monkeypatch.setenv("ANCHOR_STORE", "/tmp/test_sig_anchors.json")
    for f in ("/tmp/test_sig.db", "/tmp/test_sig_anchors.json"):
        try:
            os.remove(f)
        except FileNotFoundError:
            pass
    yield


def test_auto_sign_verify_reject():
    assert client.post("/seed/demo").status_code == 200
    assert client.post("/lots", json={"lot_id": "LOT-S", "size": 1,
                                      "manufacturer": "did:maker1"}).status_code == 200
    s = "LOT-S-000001"
    # no signature -> server auto-signs via device simulator
    r = client.post("/events/sign", json={"serial": s, "type": "SCAN",
                                          "actor": "did:transporter1"})
    assert r.status_code == 200, r.text
    # stored signature verifies
    import sqlite3
    con = sqlite3.connect("/tmp/test_sig.db")
    row = con.execute("SELECT signature, signer, raw FROM events WHERE serial=? AND seq IS NULL",
                      (s,)).fetchone()
    assert row[0] and not row[0].startswith(("demo:", "unsigned:"))
    from backend.crypto import verify_event
    import json
    ev = json.loads(row[2])
    ev.update({"signature": row[0], "signer": row[1]})
    assert verify_event(ev)
    # forged signature rejected
    r = client.post("/events/sign", json={"serial": s, "type": "SCAN",
                                          "actor": "did:transporter1",
                                          "signature": "0x" + "ab" * 64,
                                          "signer": "0x" + "cd" * 32})
    assert r.status_code == 401, r.text
    # signer service roundtrip
    r = client.get("/signer/pubkey/did:maker1")
    assert r.json()["pubkey"].startswith("0x")

"""Read endpoints for the UI tower: GET /lots + GET /lots/{id}."""

import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi.testclient import TestClient

os.environ["DB_PATH"] = "/tmp/test_lots.db"
os.environ["ANCHOR_STORE"] = "/tmp/test_lots_anchors.json"
for f in ("/tmp/test_lots.db", "/tmp/test_lots_anchors.json"):
    try:
        os.remove(f)
    except FileNotFoundError:
        pass

import pytest

from backend.main import app  # noqa: E402
from backend.db import connect as db_connect  # noqa: E402

client = TestClient(app, raise_server_exceptions=False)


def _seed() -> None:
    c = db_connect()
    c.execute(
        "INSERT OR REPLACE INTO lots(lot_id,gtin,size,root,tx,frozen) VALUES(?,?,?,?,?,?)",
        ("LOT-UI", "GTIN-1", 2, "0xabc", "0xtx1", 0))
    c.execute(
        "INSERT OR REPLACE INTO products(serial,lot_id,status,current_custodian) VALUES(?,?,?,?)",
        ("LOT-UI-000001", "LOT-UI", "ACTIVE", "did:maker1"))
    c.execute(
        "INSERT OR REPLACE INTO products(serial,lot_id,status,current_custodian) VALUES(?,?,?,?)",
        ("LOT-UI-000002", "LOT-UI", "SOLD", "did:retailer1"))
    c.commit()
    c.close()


@pytest.fixture(scope="module", autouse=True)
def _seed_db() -> None:
    # Seed at test time: collection imports every module first, so the DB
    # env active during a request is the last-imported module's. Seeding
    # here guarantees rows land in the DB this module's tests actually hit.
    _seed()
    yield


def test_list_lots() -> None:
    r = client.get("/lots")
    assert r.status_code == 200, r.status_code
    lots = {l["lot_id"]: l for l in r.json()["lots"]}
    assert lots["LOT-UI"]["units"] == 2
    assert lots["LOT-UI"]["by_status"] == {"ACTIVE": 1, "SOLD": 1}
    assert lots["LOT-UI"]["frozen"] is False


def test_lot_detail() -> None:
    r = client.get("/lots/LOT-UI")
    assert r.status_code == 200, r.status_code
    d = r.json()
    assert d["lot_id"] == "LOT-UI" and len(d["units"]) == 2
    assert d["custodians"] == ["did:maker1", "did:retailer1"]
    assert d["root"] == "0xabc"


def test_lot_detail_unknown_and_oversize() -> None:
    assert client.get("/lots/NOPE-NOPE").status_code == 404
    assert client.get("/lots/" + "L" * 200).status_code == 422

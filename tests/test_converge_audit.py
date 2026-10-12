"""Convergence-audit regression tests (isolated DB /tmp/test_cvaudit.db, live chain)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi.testclient import TestClient

os.environ["DB_PATH"] = "/tmp/test_cvaudit.db"
os.environ["REDTEAM_MODE"] = "1"

from backend.main import app  # noqa: E402

import pytest  # noqa: E402

client = TestClient(app)


@pytest.fixture(autouse=True)
def _isolated_db(monkeypatch):
    monkeypatch.setenv("DB_PATH", "/tmp/test_cvaudit.db")
    try:
        os.remove("/tmp/test_cvaudit.db")
    except FileNotFoundError:
        pass
    yield


LONG = "S" * 200


def test_serial_caps():
    assert client.get(f"/products/{LONG}/verify").status_code == 422
    assert client.get(f"/products/{LONG}/bundle").status_code == 422
    assert client.get(f"/products/{LONG}/anchor").status_code == 422
    r = client.post("/converge/case", json={"serial": LONG})
    assert r.status_code == 422, r.text


def test_anchor_id_matches_chain():
    r = client.post("/lots", json={"lot_id": "AUDIT-A", "size": 2, "manufacturer": "did:maker1"})
    assert r.status_code == 200, r.text
    r = client.post("/anchor/run")
    assert r.status_code == 200, r.text
    aid = r.json()["anchor_id"]
    r = client.get("/products/AUDIT-A-000001/anchor")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["anchored"] is True
    assert d["anchor_id"] == aid, d
    import backend.chain as ch
    assert ch.get_anchor(aid)["root"] == d["root"]


def test_dispatch_4xx_does_not_fall_back(monkeypatch, tmp_path):
    import backend.convergence as cv
    calls = []

    class R:
        status_code = 400
        text = "bad schema: missing lotId"

    def fake_post(url, json=None, headers=None, timeout=8.0):
        calls.append(url)
        return R()

    monkeypatch.setattr(cv.httpx, "post", fake_post)
    monkeypatch.setattr(cv, "OUTBOX", str(tmp_path / "outbox.jsonl"))
    out = cv.dispatch_case("S-1", {"verdict": "SUSPICIOUS", "risk_score": 10,
                                   "confidence": "low", "checks": [], "next_steps": []}, "http://x")
    assert out["queued"] is True and out.get("rejected") is True
    assert len(calls) == 1 and "incidents" in calls[0], calls


def test_dispatch_200_non_json_no_double_delivery(monkeypatch, tmp_path):
    import backend.convergence as cv
    calls = []

    class R:
        status_code = 200
        text = "<html>ok</html>"

        def json(self):
            raise ValueError("no json")

    def fake_post(url, json=None, headers=None, timeout=8.0):
        calls.append(url)
        return R()

    monkeypatch.setattr(cv.httpx, "post", fake_post)
    monkeypatch.setattr(cv, "OUTBOX", str(tmp_path / "outbox.jsonl"))
    out = cv.dispatch_case("S-1", {"verdict": "SUSPICIOUS", "risk_score": 10,
                                   "confidence": "low", "checks": [], "next_steps": []}, "http://x")
    assert out["ok"] is True and out["via"] == "incidents"
    assert len(calls) == 1, calls


def test_dispatch_5xx_still_falls_back(monkeypatch, tmp_path):
    import backend.convergence as cv
    calls = []

    class R500:
        status_code = 500
        text = "boom"

    class R201:
        status_code = 201
        text = ""

        def json(self):
            return {"id": "CASE-1"}

    def fake_post(url, json=None, headers=None, timeout=8.0):
        calls.append(url)
        return R201() if "handoffs" in url else R500()

    monkeypatch.setattr(cv.httpx, "post", fake_post)
    monkeypatch.setattr(cv, "OUTBOX", str(tmp_path / "outbox.jsonl"))
    out = cv.dispatch_case("S-1", {"verdict": "SUSPICIOUS", "risk_score": 10,
                                   "confidence": "low", "checks": [], "next_steps": []}, "http://x")
    assert out["ok"] is True and out["via"] == "handoffs"
    assert len(calls) == 2, calls


def test_case_id_validation():
    import backend.convergence as cv
    assert cv.valid_case_id("CASE-123_x.y") == "CASE-123_x.y"
    assert cv.valid_case_id("../../etc/passwd") == ""
    assert cv.valid_case_id("a/b") == ""
    assert cv.valid_case_id(None) == ""
    assert cv.valid_case_id("x" * 200) == ""


def test_converge_case_stores_validated_id(monkeypatch):
    import backend.convergence as cv
    monkeypatch.setattr(cv, "dispatch_case",
                        lambda s, v, u: {"ok": True, "via": "incidents",
                                         "partner": {"id": "../../evil"}})
    r = client.post("/lots", json={"lot_id": "AUDIT-C", "size": 1, "manufacturer": "did:maker1"})
    assert r.status_code == 200, r.text
    r = client.post("/converge/case", json={"serial": "AUDIT-C-000001"})
    assert r.status_code == 200, r.text
    import sqlite3
    con = sqlite3.connect("/tmp/test_cvaudit.db")
    row = con.execute("SELECT case_id FROM converge_cases WHERE serial='AUDIT-C-000001'").fetchone()
    con.close()
    assert row is not None and row[0] == "", row


def test_dispatch_throttle():
    r = client.post("/lots", json={"lot_id": "AUDIT-T", "size": 1, "manufacturer": "did:maker1"})
    assert r.status_code == 200, r.text
    r1 = client.post("/converge/case", json={"serial": "AUDIT-T-000001"})
    assert r1.status_code == 200, r1.text
    r2 = client.post("/converge/case", json={"serial": "AUDIT-T-000001"})
    assert r2.status_code == 429, r2.text

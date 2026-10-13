"""Actor logins (3 users) + session-backed RBAC. Consumers stay anonymous."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ["DB_PATH"] = "/tmp/test_auth.db"
os.environ["ANCHOR_STORE"] = "/tmp/test_auth_anchors.json"
for f in ("/tmp/test_auth.db", "/tmp/test_auth_anchors.json"):
    try:
        os.remove(f)
    except FileNotFoundError:
        pass

from fastapi.testclient import TestClient  # noqa: E402

import pytest  # noqa: E402

from backend.main import app  # noqa: E402
from backend.main import con as app_con  # noqa: E402
from backend import auth as _auth  # noqa: E402

client = TestClient(app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def _rbac_closed(monkeypatch):
    import backend.main as _main
    monkeypatch.setattr(_main, "RBAC_OPEN", False)
    yield


def _seed_users():
    c = app_con()
    try:
        _auth.seed_users(c, {})
    finally:
        c.close()


def test_login_me_logout():
    _seed_users()
    r = client.post("/auth/login", json={"username": "maker", "password": "maker123"})
    assert r.status_code == 200, r.text
    assert r.json()["did"] == "did:maker1"
    assert "tf_session=" in r.headers["set-cookie"]
    r = client.get("/auth/me")
    assert r.json()["did"] == "did:maker1", r.text  # cookie jar persists
    r = client.post("/auth/logout")
    assert r.status_code == 200
    assert client.get("/auth/me").json()["anonymous"] is True


def test_bad_credentials_rejected():
    _seed_users()
    assert client.post("/auth/login",
                       json={"username": "maker", "password": "nope"}).status_code == 401
    assert client.post("/auth/login",
                       json={"username": "ghost", "password": "x"}).status_code == 401
    assert client.get("/auth/me").json()["anonymous"] is True


def test_session_grants_operator():
    _seed_users()
    client.post("/auth/login", json={"username": "retailer", "password": "retail123"})
    r = client.post("/lots", json={"lot_id": "AUTH-T", "size": 1,
                                   "manufacturer": "did:maker1"})
    assert r.status_code == 200, r.text
    # recall via session cookie alone (no X-Role header) — then logout revokes
    r = client.post("/lots/AUTH-T/recall")
    assert r.status_code == 200, r.text
    client.post("/auth/logout")
    r = client.post("/lots/AUTH-T/recall")
    assert r.status_code == 403, r.status_code


def test_act_page_served():
    r = client.get("/act")
    assert r.status_code == 200, r.status_code
    assert "Trust Fabric · Act" in r.text
    assert "/auth/login" in r.text and "HANDOVER_OFFER" in r.text


def test_anonymous_scan_defaults_consumer(monkeypatch):
    import backend.main as _main
    monkeypatch.setattr(_main, "RBAC_OPEN", True)
    r = client.post("/actors", json={"did": "did:consumer1", "pubkey": "0x" + "cc" * 32,
                                     "role": "consumer"})
    assert r.status_code == 200, r.text
    r = client.post("/actors", json={"did": "did:maker1", "pubkey": "0x" + "aa" * 32,
                                     "role": "demo"})
    assert r.status_code == 200, r.text
    r = client.post("/lots", json={"lot_id": "ACT-T", "size": 1,
                                   "manufacturer": "did:maker1"})
    assert r.status_code == 200, r.text
    # no actor submitted, no session: recorded as shared consumer identity
    r = client.post("/events/sign", json={"serial": "ACT-T-000001", "type": "SCAN"})
    assert r.status_code == 200, r.text
    v = client.get("/products/ACT-T-000001/verify").json()
    assert "R08" not in {c["id"] for c in v["checks"] if c["status"] == "fail"}


def test_session_binds_actor_no_spoof(monkeypatch):
    import backend.main as _main
    monkeypatch.setattr(_main, "RBAC_OPEN", True)
    c = app_con()
    try:
        for t in ("products", "events", "claims", "unknown_attempts"):
            c.execute(f"DELETE FROM {t}")
        c.commit()
    finally:
        c.close()
    client.post("/auth/login", json={"username": "maker", "password": "maker123"})
    r = client.post("/lots", json={"lot_id": "BIND-T", "size": 1,
                                   "manufacturer": "did:maker1"})
    assert r.status_code == 200, r.text
    # logged in as maker but submitting as attacker: stored as maker
    r = client.post("/events/sign", json={"serial": "BIND-T-000001", "type": "SCAN",
                                          "actor": "did:attacker"})
    assert r.status_code == 200, r.text
    c = app_con()
    try:
        row = c.execute("SELECT actor FROM events WHERE serial='BIND-T-000001' AND type='SCAN'").fetchone()
    finally:
        c.close()
    assert row["actor"] == "did:maker1", dict(row)


def test_hub_page_served():
    r = client.get("/hub")
    assert r.status_code == 200, r.status_code
    assert "Convergence Hub" in r.text
    assert "/converge/health" in r.text and "/converge/case" in r.text

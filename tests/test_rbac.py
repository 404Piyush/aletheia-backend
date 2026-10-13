"""RBAC gate (demo authz): writes need X-Role operator; reads stay open."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi.testclient import TestClient

os.environ["DB_PATH"] = "/tmp/test_rbac.db"
os.environ["ANCHOR_STORE"] = "/tmp/test_rbac_anchors.json"
for f in ("/tmp/test_rbac.db", "/tmp/test_rbac_anchors.json"):
    try:
        os.remove(f)
    except FileNotFoundError:
        pass

import pytest  # noqa: E402

from backend.main import app  # noqa: E402
import backend.main as _main  # noqa: E402

client = TestClient(app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def _rbac_closed(monkeypatch):
    monkeypatch.setattr(_main, "RBAC_OPEN", False)
    yield


def _seed():
    # Once per module: re-POSTing a lot regenerates identical MANUFACTURED
    # hashes (same-second ts) and hits the event UNIQUE constraint.
    r = client.post("/lots", json={"lot_id": "RBAC-T", "size": 1,
                                   "manufacturer": "did:maker1"},
                    headers={"X-Role": "operator"})
    assert r.status_code == 200, r.text


@pytest.fixture(scope="module", autouse=True)
def _seed_once():
    _seed()
    yield


def test_reads_open_without_role():
    assert client.get("/products/RBAC-T-000001/verify").status_code == 200
    assert client.get("/lots/RBAC-T").status_code == 200


def test_writes_need_operator():
    for method, url, body in [
        ("POST", "/lots/RBAC-T/recall", None),
        ("POST", "/converge/case", {"serial": "RBAC-T-000001", "force": True}),
        ("POST", "/products/RBAC-T-000001/claim",
         {"secret": "x", "session_id": "s"}),
    ]:
        r = client.request(method, url, json=body)
        assert r.status_code == 403, (url, r.status_code)
        r = client.request(method, url, json=body, headers={"X-Role": "verifier"})
        assert r.status_code == 403, (url, r.status_code)


def test_operator_allowed_and_logged(monkeypatch, capsys):
    r = client.post("/products/RBAC-T-000001/claim",
                    json={"secret": "x", "session_id": "s"},
                    headers={"X-Role": "operator"})
    assert r.status_code == 200, r.text  # wrong secret -> ok:false, not 403
    out = capsys.readouterr().out
    assert "RBAC" in out and "operator" in out

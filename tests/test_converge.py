import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from backend.convergence import build_case_payload, build_incident_payload


def test_case_payload_shape():
    v = {"verdict": "CLONE_DETECTED", "risk_score": 80, "confidence": "high",
         "product": {"lot_id": "LOT-A"},
         "checks": [{"id": "R04", "status": "fail", "reason": "far travel"}],
         "next_steps": ["Inspect package"]}
    p = build_case_payload("LOT-A-1", v, "http://x/bundle")
    assert p["projectId"]
    assert "CLONE_DETECTED" in p["note"] or "CLONE_DETECTED" in p["executiveSummary"]
    assert isinstance(p["nextSteps"], list)


def test_incident_payload_matches_partner_schema():
    v = {"verdict": "CLONE_DETECTED", "risk_score": 80, "confidence": "high",
         "product": {"lot_id": "LOT-A"},
         "checks": [{"id": "R04", "status": "fail", "reason": "far travel"}],
         "next_steps": []}
    p = build_incident_payload("LOT-A-000123", v, "https://continuity.404piyush.me/x")
    assert p == {"serial": "LOT-A-000123", "lotId": "LOT-A", "verdict": "CLONE_DETECTED",
                 "riskScore": 80, "confidence": "high",
                 "failedChecks": ["Check R04: far travel"],
                 "evidenceBundleUrl": "https://continuity.404piyush.me/x"}


def test_dispatch_queues_when_partner_down(monkeypatch):
    monkeypatch.setenv("PARTNER_BASE", "http://127.0.0.1:9")
    monkeypatch.setenv("CONVERGE_OUTBOX", "/tmp/test_outbox.jsonl")
    try:
        os.remove("/tmp/test_outbox.jsonl")
    except FileNotFoundError:
        pass
    import importlib
    import backend.convergence as cv
    importlib.reload(cv)
    out = cv.dispatch_case("S-1", {"verdict": "SUSPICIOUS", "risk_score": 10,
                                  "confidence": "low", "checks": [], "next_steps": []}, "http://x")
    assert out["queued"] is True

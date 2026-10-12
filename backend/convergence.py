"""Convergence client → CONTINUITY handoff platform (:8080).
Graceful: partner down → payload queued to converge_outbox.jsonl (demo via transcript).
No shared DB, API keys only."""
import json
import os
import time

PARTNER_BASE = os.environ.get("PARTNER_BASE", "http://localhost:8080")
PARTNER_USER = os.environ.get("PARTNER_USER", "user-yash")
PARTNER_PROJECT = os.environ.get("PARTNER_PROJECT", "proj-apollo")
OUTBOX = os.environ.get("CONVERGE_OUTBOX", "converge_outbox.jsonl")

try:
    with open(os.path.join(os.path.dirname(__file__), "converge_map.json")) as f:
        ACTOR_MAP = json.load(f)
except FileNotFoundError:
    ACTOR_MAP = {}


def partner_up(timeout: float = 3.0) -> bool:
    try:
        import httpx
        r = httpx.get(f"{PARTNER_BASE}/api/health", timeout=timeout)
        return r.status_code < 500
    except Exception:
        return False


def _queue(payload: dict, reason: str) -> dict:
    entry = {"ts": int(time.time()), "status": "queued", "reason": reason, "payload": payload}
    with open(OUTBOX, "a") as f:
        f.write(json.dumps(entry) + "\n")
    return {"ok": False, "queued": True, "reason": reason}


def build_case_payload(serial: str, verdict: dict, bundle_url: str) -> dict:
    custodian = verdict.get("checks", [{}])[0] if verdict.get("checks") else {}
    recipient = ACTOR_MAP.get("did:retailer1", "user-jessica")
    reasons = "; ".join(c.get("reason", c.get("id", "")) for c in verdict.get("checks", [])
                        if c.get("status") == "fail")[:500]
    return {
        "projectId": PARTNER_PROJECT,
        "recipientId": recipient,
        "note": f"TrustFabric alert {serial}: {verdict.get('verdict')} risk {verdict.get('risk_score')}",
        "executiveSummary": (
            f"Serial {serial} flagged {verdict.get('verdict')} "
            f"(risk {verdict.get('risk_score')}/100, confidence {verdict.get('confidence')}). "
            f"Evidence: {reasons}. Full proof: {bundle_url}"
        )[:2000],
        "completedItems": [],
        "nextSteps": verdict.get("next_steps", [])[:5],
        "criticalBlockers": [f"Hold lot pending review (risk {verdict.get('risk_score')})"],
        "runbookCommands": [f"curl {bundle_url}"],
    }


def dispatch_case(serial: str, verdict: dict, bundle_url: str) -> dict:
    payload = build_case_payload(serial, verdict, bundle_url)
    try:
        import httpx
        r = httpx.post(f"{PARTNER_BASE}/api/handoffs", json=payload,
                       headers={"Content-Type": "application/json", "X-User-Id": PARTNER_USER},
                       timeout=8.0)
        if r.status_code in (200, 201):
            return {"ok": True, "partner": r.json()}
        return _queue(payload, f"partner HTTP {r.status_code}: {r.text[:200]}")
    except Exception as e:
        return _queue(payload, f"partner unreachable: {type(e).__name__}")


def case_status(serial: str) -> dict:
    try:
        import httpx
        r = httpx.get(f"{PARTNER_BASE}/api/handoffs", timeout=8.0)
        if r.status_code != 200:
            return {"ok": False, "reason": f"partner HTTP {r.status_code}"}
        items = r.json() if isinstance(r.json(), list) else r.json().get("handoffs", [])
        hits = [h for h in items if serial in json.dumps(h)]
        return {"ok": True, "matches": hits}
    except Exception as e:
        return {"ok": False, "reason": f"partner unreachable: {type(e).__name__}"}

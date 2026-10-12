"""Convergence client → CONTINUITY handoff platform (:8080).
Graceful: partner down → payload queued to converge_outbox.jsonl (demo via transcript).
No shared DB, API keys only."""
import json
import os
import time

import httpx

PARTNER_BASE = os.environ.get("PARTNER_BASE", "http://localhost:8080")
PARTNER_USER = os.environ.get("PARTNER_USER", "user-yash")
PARTNER_PROJECT = os.environ.get("PARTNER_PROJECT", "apollo-core")
PUBLIC_BASE = os.environ.get("PUBLIC_BASE", "https://continuity.404piyush.me")
OUTBOX = os.environ.get("CONVERGE_OUTBOX", "converge_outbox.jsonl")

try:
    with open(os.path.join(os.path.dirname(__file__), "converge_map.json")) as f:
        ACTOR_MAP = json.load(f)
except FileNotFoundError:
    ACTOR_MAP = {}


def partner_up(timeout: float = 3.0) -> bool:
    try:
        r = httpx.get(f"{PARTNER_BASE}/api/health", timeout=timeout)
        return r.status_code < 500
    except Exception:
        return False


def bridge_status(timeout: float = 5.0) -> dict:
    """His bridge health: GET /api/convergence/status."""
    try:
        r = httpx.get(f"{PARTNER_BASE}/api/convergence/status", timeout=timeout)
        return {"reachable": True, "status": r.status_code, "body": r.json()
                if "json" in r.headers.get("content-type", "") else r.text[:300]}
    except Exception as e:
        return {"reachable": False, "reason": f"{type(e).__name__}"}


def fetch_actor_map(timeout: float = 5.0) -> dict:
    """Live roster: GET /api/convergence/actor-map -> {did: userId}. Falls back to file."""
    try:
        r = httpx.get(f"{PARTNER_BASE}/api/convergence/actor-map", timeout=timeout)
        if r.status_code == 200:
            actors = r.json().get("actors", r.json() if isinstance(r.json(), list) else [])
            live = {a["did"]: a.get("userId", a.get("user_id", "")) for a in actors if "did" in a}
            if live:
                return {"source": "live", "map": live}
    except Exception:
        pass
    return {"source": "static", "map": dict(ACTOR_MAP)}


def live_map() -> dict:
    return fetch_actor_map().get("map", dict(ACTOR_MAP))


def _queue(payload: dict, reason: str) -> dict:
    entry = {"ts": int(time.time()), "status": "queued", "reason": reason, "payload": payload}
    with open(OUTBOX, "a") as f:
        f.write(json.dumps(entry) + "\n")
    # cap: keep outbox a readable transcript, not unbounded growth
    try:
        with open(OUTBOX) as f:
            lines = f.readlines()
        if len(lines) > 1000:
            with open(OUTBOX, "w") as f:
                f.writelines(lines[-1000:])
    except OSError:
        pass
    return {"ok": False, "queued": True, "reason": reason}


def build_incident_payload(serial: str, verdict: dict, bundle_url: str) -> dict:
    """His canonical schema: POST /api/convergence/incidents (CONVERGENCE.md 3.2)."""
    lot = (verdict.get("product") or {}).get("lot_id", serial.rsplit("-", 1)[0])
    failed = [f"Check {c.get('id')}: {c.get('reason', '')}"[:200]
              for c in verdict.get("checks", []) if c.get("status") == "fail"]
    return {
        "serial": serial,
        "lotId": lot,
        "verdict": verdict.get("verdict"),
        "riskScore": verdict.get("risk_score"),
        "confidence": verdict.get("confidence"),
        "failedChecks": failed,
        "evidenceBundleUrl": bundle_url,
    }


def build_case_payload(serial: str, verdict: dict, bundle_url: str) -> dict:
    """Legacy fallback: POST /api/handoffs."""
    recipient = live_map().get("did:retailer1", "user-jessica")
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
    incident = build_incident_payload(serial, verdict, bundle_url)
    try:
        r = httpx.post(f"{PARTNER_BASE}/api/convergence/incidents", json=incident,
                       headers={"Content-Type": "application/json", "X-User-Id": PARTNER_USER},
                       timeout=8.0)
        if r.status_code in (200, 201):
            return {"ok": True, "via": "incidents", "partner": r.json()}
    except Exception:
        pass
    payload = build_case_payload(serial, verdict, bundle_url)
    try:
        r = httpx.post(f"{PARTNER_BASE}/api/handoffs", json=payload,
                       headers={"Content-Type": "application/json", "X-User-Id": PARTNER_USER},
                       timeout=8.0)
        if r.status_code in (200, 201):
            return {"ok": True, "via": "handoffs", "partner": r.json()}
        return _queue(incident, f"partner HTTP {r.status_code}: {r.text[:200]}")
    except Exception as e:
        return _queue(incident, f"partner unreachable: {type(e).__name__}")


def case_detail(case_id: str, timeout: float = 5.0) -> dict | None:
    """His case state: GET /api/convergence/cases/{id}."""
    try:
        r = httpx.get(f"{PARTNER_BASE}/api/convergence/cases/{case_id}", timeout=timeout)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None


def case_status(serial: str) -> dict:
    try:
        r = httpx.get(f"{PARTNER_BASE}/api/handoffs", timeout=8.0)
        if r.status_code != 200:
            return {"ok": False, "reason": f"partner HTTP {r.status_code}"}
        items = r.json() if isinstance(r.json(), list) else r.json().get("handoffs", [])
        hits = [h for h in items if isinstance(h, dict) and h.get("serial") == serial]
        if not hits:
            # fallback: substring over the payload (partner schema may vary)
            hits = [h for h in items if serial in json.dumps(h)]
        return {"ok": True, "matches": hits}
    except Exception as e:
        return {"ok": False, "reason": f"partner unreachable: {type(e).__name__}"}

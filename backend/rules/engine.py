"""Rules R01-R12. Each returns Finding or None. Sorted by ts, ints only."""
import hashlib
import math
import time
from dataclasses import dataclass

R = 6371.0

SEV_WEIGHT = {"critical": 40, "high": 25, "medium": 10, "low": 3}
CATEGORY = {"R01": "crypto", "R03": "crypto", "R05": "crypto", "R09": "crypto",
            "R04": "spatiotemporal", "R07": "custody", "R06": "custody",
            "R08": "identity", "R02": "secret", "R13": "secret",
            "R11": "lifecycle", "R12": "lifecycle", "R14": "device"}


@dataclass
class Finding:
    rule_id: str
    severity: str
    reason: str
    evidence_refs: list
    next_step: str


def haversine_km(lat1_mdeg, lon1_mdeg, lat2_mdeg, lon2_mdeg) -> float:
    p1, p2 = math.radians(lat1_mdeg / 1000), math.radians(lat2_mdeg / 1000)
    dphi = math.radians((lat2_mdeg - lat1_mdeg) / 1000)
    dl = math.radians((lon2_mdeg - lon1_mdeg) / 1000)
    a = math.sin(dphi / 2) ** 2 + math.cos(math.radians(lat1_mdeg / 1000)) * math.cos(math.radians(lat2_mdeg / 1000)) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def r01_fork(custody_events) -> Finding | None:
    seen = {}
    for e in custody_events:
        key = (e["seq"], e["prev_hash"])
        if e["seq"] in seen and seen[e["seq"]] != e["event_hash"]:
            return Finding("R01", "critical",
                           f"Chain fork at seq {e['seq']}: two histories claim same step",
                           [seen[e["seq"]], e["event_hash"]], "Hold stock; inspect physical package")
        seen[e["seq"]] = e["event_hash"]
    man = [e for e in custody_events if e["type"] == "MANUFACTURED"]
    if len(man) > 1:
        return Finding("R01", "critical", "Second MANUFACTURED for same serial",
                       [e["event_hash"] for e in man], "Hold stock; check enrolment")
    return None


def r04_travel(located) -> Finding | None:
    located = sorted([e for e in located if e["lat_mdeg"] is not None], key=lambda x: x["ts"])
    for a, b in zip(located, located[1:]):
        dt_h = (b["ts"] - a["ts"]) / 3600
        if dt_h <= 0:
            continue
        d = haversine_km(a["lat_mdeg"], a["lon_mdeg"], b["lat_mdeg"], b["lon_mdeg"])
        v = d / dt_h
        if v > 900:
            return Finding("R04", "critical",
                           f"Seen {d:.0f}km apart in {dt_h:.2f}h (≈{v:.0f} km/h)",
                           [a["event_hash"], b["event_hash"]],
                           "Inspect physical package; contact distributor")
    return None


def r07_custody(custody_events, all_events) -> Finding | None:
    custodian = None
    offered_to = None
    for e in sorted(custody_events, key=lambda x: x["seq"] or 0):
        if e["type"] == "MANUFACTURED":
            custodian = e["actor"]
        elif e["type"] == "HANDOVER_OFFER":
            if e["actor"] != custodian:
                return Finding("R07", "high", f"Offer by non-custodian {e['actor'][:12]}",
                               [e["event_hash"]], "Verify custody paperwork")
            offered_to = e.get("to_actor")
        elif e["type"] == "HANDOVER_ACCEPT":
            if offered_to and e["actor"] != offered_to:
                return Finding("R07", "high", "Accept without matching offer",
                               [e["event_hash"]], "Verify handover")
            custodian = e["actor"]
            offered_to = None
    return None


def score_and_confidence(findings: list[Finding], history_len: int, anchored: bool):
    by_rule = {f.rule_id: f for f in findings}
    score = min(100, sum(SEV_WEIGHT[f.severity] for f in by_rule.values()))
    cats = {CATEGORY.get(r, "other") for r in by_rule}
    if "R03" in by_rule or "R09" in by_rule or len(cats) >= 2:
        conf = "high"
    elif len(cats) == 1:
        conf = "medium" if history_len >= 3 else "low"
    else:
        conf = "high" if history_len >= 3 and anchored else "medium"
    return score, conf


def verdict_for(findings: list[Finding]) -> tuple[str, str]:
    ids = {f.rule_id for f in findings}
    if not findings:
        return "VERIFIED", "History consistent"
    if ids & {"R01", "R02", "R03", "R05", "R12"}:
        return "CLONE_DETECTED", "Contested: conflicting histories"
    if ids & {"R08", "R09"}:
        return "TAMPERED", "Integrity failure"
    if "R04" in ids and "R07" in ids:
        # far travel + custody break together = cloned identity in wrong hands
        return "CLONE_DETECTED", "Contested: conflicting histories"
    return "SUSPICIOUS", "Suspicious movement/custody"

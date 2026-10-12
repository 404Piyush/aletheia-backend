"""Per-rule engine tests (USP 1, 10). Pure unit tests: no DB, chain, or server."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from backend.rules.engine import Finding, r01_fork, r04_travel, r07_custody, score_and_confidence, verdict_for

# Oslo store POS -> Gdansk kiosk, mirrors the live LOT-DEMO-A clone pair.
OSLO = {"ts": 1_000_000, "lat_mdeg": 59914, "lon_mdeg": 10752, "event_hash": "evt-oslo"}
GDANSK = {"ts": 1_000_000 + 1200, "lat_mdeg": 54352, "lon_mdeg": 21042, "event_hash": "evt-gdansk"}


def test_r04_impossible_travel_fires():
    f = r04_travel([OSLO, GDANSK])
    assert f is not None
    assert f.rule_id == "R04"
    assert f.severity == "critical"
    assert "km/h" in f.reason
    assert f.evidence_refs == ["evt-oslo", "evt-gdansk"]
    assert f.next_step, "every failed check must tell the operator what to do"


def test_r04_plausible_travel_is_silent():
    same_pair_slow = [OSLO, {**GDANSK, "ts": OSLO["ts"] + 5 * 3600}]
    assert r04_travel(same_pair_slow) is None
    same_place = [OSLO, {**OSLO, "ts": OSLO["ts"] + 600, "event_hash": "evt-oslo-2"}]
    assert r04_travel(same_place) is None


def test_r04_alone_is_suspicious_not_clone():
    # Two-signal doctrine: far travel alone -> SUSPICIOUS; travel + custody
    # break (R07) -> CLONE_DETECTED. A single anomaly must never cry clone.
    f = r04_travel([OSLO, GDANSK])
    assert f is not None
    verdict, _ = verdict_for([f])
    assert verdict == "SUSPICIOUS"


def _ev(seq, h, prev="p0", typ="HANDOVER_ACCEPT", actor="did:maker1"):
    return {"seq": seq, "event_hash": h, "prev_hash": prev, "type": typ,
            "actor": actor, "ts": 1_000_000 + seq * 60}


def test_r01_fork_fires():
    a = [_ev(0, "h-man", "GENESIS", "MANUFACTURED"), _ev(1, "h-a", "h-man")]
    b = [_ev(0, "h-man", "GENESIS", "MANUFACTURED"), _ev(1, "h-b", "h-man")]
    f = r01_fork(a + b[1:])
    assert f is not None
    assert f.rule_id == "R01"
    assert f.severity == "critical"
    assert set(f.evidence_refs) == {"h-a", "h-b"}
    assert f.next_step


def test_r01_double_birth_fires():
    evs = [_ev(0, "h-man-1", "GENESIS", "MANUFACTURED"),
           _ev(5, "h-man-2", "h-man-1", "MANUFACTURED")]
    f = r01_fork(evs)
    assert f is not None and f.rule_id == "R01"
    assert "MANUFACTURED" in f.reason


def test_r01_linear_chain_is_silent():
    evs = [_ev(0, "h-man", "GENESIS", "MANUFACTURED"),
           _ev(1, "h-a", "h-man"), _ev(2, "h-c", "h-a")]
    assert r01_fork(evs) is None


def test_r01_alone_is_clone():
    # Crypto-identity breaks escalate immediately: unlike travel-alone
    # (SUSPICIOUS), a forked history is CLONE_DETECTED on its own.
    f = r01_fork([_ev(0, "h-man", "GENESIS", "MANUFACTURED"),
                  _ev(1, "h-a", "h-man"), _ev(1, "h-b", "h-man")])
    assert f is not None
    verdict, _ = verdict_for([f])
    assert verdict == "CLONE_DETECTED"


def _chain(*evs):
    return list(evs)


def test_r07_offer_by_non_custodian_fires():
    evs = _chain(
        _ev(0, "h-man", "GENESIS", "MANUFACTURED", "did:maker1"),
        {"seq": 1, "event_hash": "h-evil", "prev_hash": "h-man",
         "type": "HANDOVER_OFFER", "actor": "did:attacker", "to_actor": "did:retailer1",
         "ts": 1_000_060},
    )
    f = r07_custody(evs, evs)
    assert f is not None
    assert f.rule_id == "R07"
    assert f.severity == "high"
    assert "did:attacker" in f.reason
    assert f.evidence_refs == ["h-evil"]
    assert f.next_step


def test_r07_accept_without_offer_fires():
    evs = _chain(
        _ev(0, "h-man", "GENESIS", "MANUFACTURED", "did:maker1"),
        {"seq": 1, "event_hash": "h-offer", "prev_hash": "h-man",
         "type": "HANDOVER_OFFER", "actor": "did:maker1", "to_actor": "did:transporter1",
         "ts": 1_000_060},
        {"seq": 2, "event_hash": "h-sneak", "prev_hash": "h-offer",
         "type": "HANDOVER_ACCEPT", "actor": "did:attacker", "ts": 1_000_120},
    )
    f = r07_custody(evs, evs)
    assert f is not None and f.rule_id == "R07"
    assert f.evidence_refs == ["h-sneak"]


def test_r07_clean_chain_is_silent():
    evs = _chain(
        _ev(0, "h-man", "GENESIS", "MANUFACTURED", "did:maker1"),
        {"seq": 1, "event_hash": "h-offer", "prev_hash": "h-man",
         "type": "HANDOVER_OFFER", "actor": "did:maker1", "to_actor": "did:transporter1",
         "ts": 1_000_060},
        {"seq": 2, "event_hash": "h-acc", "prev_hash": "h-offer",
         "type": "HANDOVER_ACCEPT", "actor": "did:transporter1", "ts": 1_000_120},
    )
    assert r07_custody(evs, evs) is None


def test_r04_plus_r07_is_clone():
    # The live clone's exact signature: impossible travel AND custody break.
    travel = r04_travel([OSLO, GDANSK])
    custody = r07_custody(
        [_ev(0, "h-man", "GENESIS", "MANUFACTURED", "did:maker1"),
         {"seq": 1, "event_hash": "h-evil", "prev_hash": "h-man",
          "type": "HANDOVER_OFFER", "actor": "did:attacker",
          "to_actor": "did:retailer1", "ts": 1_000_060}],
        [])
    assert travel is not None and custody is not None
    verdict, _ = verdict_for([travel, custody])
    assert verdict == "CLONE_DETECTED"


def _f(rule, sev="critical"):
    return Finding(rule, sev, f"{rule} fired", ["h1"], "Hold stock")


def test_scoring_weights_pinned():
    assert score_and_confidence([_f("R04")], 6, True)[0] == 40
    assert score_and_confidence([_f("R04"), _f("R07", "high")], 6, True)[0] == 65
    assert score_and_confidence([_f("R04"), _f("R07", "high"), _f("R08")], 6, True)[0] == 100
    assert score_and_confidence([_f("R01"), _f("R04"), _f("R09")], 6, True)[0] == 100
    assert score_and_confidence([_f("R06", "medium")], 6, True)[0] == 10


def test_scoring_dedups_same_rule():
    one = score_and_confidence([_f("R04")], 6, True)[0]
    two = score_and_confidence([_f("R04"), _f("R04")], 6, True)[0]
    assert one == two == 40


def test_verdict_routing_table():
    for rule in ("R01", "R02", "R03", "R05", "R12"):
        assert verdict_for([_f(rule)])[0] == "CLONE_DETECTED", rule
    for rule in ("R08", "R09"):
        assert verdict_for([_f(rule)])[0] == "TAMPERED", rule
    for rule in ("R06", "R07", "R11", "R13", "R14"):
        assert verdict_for([_f(rule)])[0] == "SUSPICIOUS", rule
    assert verdict_for([])[0] == "VERIFIED"


def test_confidence_rules():
    assert score_and_confidence([_f("R03")], 1, False)[1] == "high"
    assert score_and_confidence([_f("R09")], 1, False)[1] == "high"
    assert score_and_confidence([_f("R06", "medium")], 3, False)[1] == "medium"
    assert score_and_confidence([_f("R06", "medium")], 1, False)[1] == "low"
    assert score_and_confidence([], 5, True)[1] == "high"
    assert score_and_confidence([], 1, False)[1] == "medium"

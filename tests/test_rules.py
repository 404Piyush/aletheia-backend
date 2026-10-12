"""Per-rule engine tests (USP 1, 10). Pure unit tests: no DB, chain, or server."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from backend.rules.engine import r04_travel, verdict_for

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

"""FastAPI backend — blockchain side, no UI. Run: uvicorn backend.main:app --port 8000"""
import hashlib
import json
import os
import secrets
import sqlite3
import time

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

from .crypto import event_hash, sign_event, verify_event, GENESIS_PREV, to_hex
from .db import connect
from .merkle import root_of, get_proof, verify_proof
from . import chain as ch
from .rules.engine import (r01_fork, r04_travel, r07_custody, score_and_confidence,
                           verdict_for, Finding)

app = FastAPI(title="Trust Fabric Chain API")
CUSTODY = ("MANUFACTURED", "HANDOVER_OFFER", "HANDOVER_ACCEPT", "SOLD")


def con() -> sqlite3.Connection:
    return connect()


def require_redteam():
    if os.environ.get("REDTEAM_MODE") != "1":
        raise HTTPException(403, "redteam disabled")


# ---------- models ----------
class ActorIn(BaseModel):
    did: str
    pubkey: str
    role: str


class LotIn(BaseModel):
    lot_id: str
    gtin: str = "09506000.134352"
    size: int = 20
    manufacturer: str


class EventIn(BaseModel):
    serial: str
    type: str
    actor: str
    ts: int | None = None
    lat: float | None = None
    lon: float | None = None
    biz_step: str = "shipping"
    to_actor: str | None = None
    signature: str | None = None
    signer: str | None = None


class ClaimIn(BaseModel):
    secret: str
    session_id: str


# ---------- actors ----------
@app.post("/actors")
def register_actor(a: ActorIn):
    c = con()
    c.execute("INSERT OR REPLACE INTO actors(did,pubkey,role,active) VALUES(?,?,?,1)",
              (a.did, a.pubkey, a.role))
    c.commit()
    ch.set_actor(a.pubkey, True)
    return {"ok": True}


# ---------- lots ----------
@app.post("/lots")
def create_lot(lot: LotIn):
    c = con()
    serials = [f"{lot.lot_id}-{i:06d}" for i in range(1, lot.size + 1)]
    secrets = {}
    for s in serials:
        sec = __import__("secrets").token_hex(8)
        secrets[s] = sec
        c.execute(
            "INSERT OR REPLACE INTO products(serial,lot_id,secret_hash,status,current_custodian) VALUES(?,?,?,?,?)",
            (s, lot.lot_id, hashlib.sha256(sec.encode()).hexdigest(), "CREATED", lot.manufacturer))
    leaves = [s.encode() for s in serials]
    root = "0x" + root_of(leaves).hex()
    tx = ch.register_lot(lot.lot_id, lot.manufacturer, root, lot.size)
    c.execute("INSERT OR REPLACE INTO lots(lot_id,gtin,size,root,tx) VALUES(?,?,?,?,?)",
              (lot.lot_id, lot.gtin, lot.size, root, tx))
    # MANUFACTURED per unit
    for s in serials:
        ev = {"serial": s, "type": "MANUFACTURED", "actor": lot.manufacturer,
              "ts": int(time.time()), "lat_mdeg": None, "lon_mdeg": None,
              "biz_step": "commissioning", "to_actor": None,
              "prev_hash": GENESIS_PREV, "ref_hash": None, "seq": 0}
        ev["event_hash"] = event_hash(ev)
        ev.update({"signature": "demo:manufacturer", "signer": lot.manufacturer})
        c.execute(
        "INSERT INTO events(serial,type,actor,ts,lat_mdeg,lon_mdeg,biz_step,to_actor,prev_hash,ref_hash,seq,event_hash,signature,signer,raw) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (s, "MANUFACTURED", lot.manufacturer, ev["ts"], None, None, "commissioning", None,
         GENESIS_PREV, None, 0, ev["event_hash"], "demo:manufacturer", lot.manufacturer, json.dumps(ev)))
        c.execute("UPDATE products SET status='ACTIVE', current_custodian=? WHERE serial=?",
                  (lot.manufacturer, s))
    c.commit()
    return {"lot_id": lot.lot_id, "root": root, "tx": tx, "serials": serials[:5],
            "total": len(serials), "note": "full secrets only in demo seed response",
            "sample_secrets": {s: secrets[s] for s in serials[:3]}}


def _head(c, serial):
    row = c.execute(
        "SELECT event_hash, seq FROM events WHERE serial=? AND seq IS NOT NULL ORDER BY seq DESC LIMIT 1",
        (serial,)).fetchone()
    if row:
        return row["event_hash"], row["seq"]
    return GENESIS_PREV, -1


@app.post("/events/sign")
def sign_event_api(e: EventIn):
    c = con()
    prod = c.execute("SELECT * FROM products WHERE serial=?", (e.serial,)).fetchone()
    if not prod:
        # unknown serial with known lot prefix -> R03 bookkeeping
        lot_guess = "-".join(e.serial.split("-")[:-1]) if "-" in e.serial else ""
        if lot_guess:
            c.execute("INSERT INTO unknown_attempts(serial,lot_id,ts,source) VALUES(?,?,?,?)",
                      (e.serial, lot_guess, int(time.time()), e.actor))
            c.commit()
        raise HTTPException(404, "unknown serial")
    if ch.is_frozen(prod["lot_id"]):
        raise HTTPException(423, "lot frozen/recalled")
    ts = e.ts or int(time.time())
    lat_mdeg = int(e.lat * 1000) if e.lat is not None else None
    lon_mdeg = int(e.lon * 1000) if e.lon is not None else None
    is_custody = e.type in CUSTODY
    if is_custody:
        prev, seq = _head(c, e.serial)
        body = {"serial": e.serial, "type": e.type, "actor": e.actor, "ts": ts,
                "lat_mdeg": lat_mdeg, "lon_mdeg": lon_mdeg, "biz_step": e.biz_step,
                "to_actor": e.to_actor, "prev_hash": prev, "ref_hash": None, "seq": seq + 1}
    else:
        head_h, _ = _head(c, e.serial)
        body = {"serial": e.serial, "type": e.type, "actor": e.actor, "ts": ts,
                "lat_mdeg": lat_mdeg, "lon_mdeg": lon_mdeg, "biz_step": e.biz_step,
                "to_actor": e.to_actor, "prev_hash": None, "ref_hash": head_h, "seq": None}
    body["event_hash"] = event_hash(body)
    # prototype: accept demo signatures; real verify if signer key known
    sig = e.signature or f"demo:{e.actor}"
    signer = e.signer or e.actor
    c.execute(
        "INSERT INTO events(serial,type,actor,ts,lat_mdeg,lon_mdeg,biz_step,to_actor,prev_hash,ref_hash,seq,event_hash,signature,signer,raw) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (e.serial, e.type, e.actor, ts, lat_mdeg, lon_mdeg, e.biz_step, e.to_actor,
         body["prev_hash"], body["ref_hash"], body["seq"], body["event_hash"], sig, signer, json.dumps(body)))
    if e.type == "HANDOVER_ACCEPT":
        c.execute("UPDATE products SET current_custodian=? WHERE serial=?", (e.actor, e.serial))
    if e.type == "SOLD":
        c.execute("UPDATE products SET status='SOLD' WHERE serial=?", (e.serial,))
    c.commit()
    # custody-path + frozen checks surface at verify time; return head
    return {"event_hash": body["event_hash"], "seq": body["seq"]}


def _events(c, serial):
    rows = c.execute("SELECT * FROM events WHERE serial=? ORDER BY ts, id", (serial,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d.update(json.loads(r["raw"] or "{}"))
        except Exception:
            pass
        out.append(d)
    return out


def _lot_proof(c, serial):
    prod = c.execute("SELECT * FROM products WHERE serial=?", (serial,)).fetchone()
    if not prod:
        return {"status": "unknown_serial"}
    lot = c.execute("SELECT * FROM lots WHERE lot_id=?", (prod["lot_id"],)).fetchone()
    if not lot:
        return {"status": "unknown_lot"}
    sisters = [r["serial"] for r in c.execute(
        "SELECT serial FROM products WHERE lot_id=? ORDER BY rowid", (prod["lot_id"],)).fetchall()]
    try:
        idx = sisters.index(serial)
    except ValueError:
        return {"status": "not_in_lot", "root": lot["root"]}
    leaves = [s.encode() for s in sisters]
    proof = get_proof(leaves, idx)
    ok = verify_proof(serial.encode(),
                      [(p, left) for p, left in proof], bytes.fromhex(lot["root"][2:]))
    return {"status": "pass" if ok else "fail", "root": lot["root"],
            "proof": [( "0x"+p.hex(), left) for p, left in proof]}


def build_verdict(serial: str):
    c = con()
    evs = _events(c, serial)
    prod = c.execute("SELECT * FROM products WHERE serial=?", (serial,)).fetchone()
    findings: list[Finding] = []
    custody = [e for e in evs if e["type"] in CUSTODY]
    located = [e for e in evs if e.get("lat_mdeg") is not None]
    if custody:
        f = r01_fork(custody)
        if f:
            findings.append(f)
        f = r07_custody(custody, evs)
        if f:
            findings.append(f)
    f = r04_travel(located)
    if f:
        findings.append(f)
    # R03 lot inclusion
    lp = _lot_proof(c, serial) if prod else {"status": "unknown_serial"}
    if lp.get("status") in ("fail", "not_in_lot"):
        findings.append(Finding("R03", "critical", "Serial fails Merkle inclusion vs on-chain lot root",
                                [serial], "Quarantine unit; check enrolment"))
    if not prod:
        c.execute("INSERT INTO unknown_attempts(serial,lot_id,ts,source) VALUES(?,?,?,?)",
                  (serial, "", int(time.time()), "verify"))
        c.commit()
    # R11 post-SOLD scan
    sold_ts = next((e["ts"] for e in custody if e["type"] == "SOLD"), None)
    if sold_ts and any(e["ts"] > sold_ts and e["type"] in ("SCAN", "HANDOVER_OFFER") for e in evs):
        findings.append(Finding("R11", "medium", "Supply-chain activity after SOLD",
                                [serial], "Check return flow; possible diversion"))
    # R12 double-claim
    if prod and prod["status"] == "CONTESTED":
        findings.append(Finding("R12", "high", "Second claimant after CLAIMED — contested",
                                [serial], "Inspect seal; do not pick winner from data"))
    # R08 revoked/unregistered actor (consult actors table; unknown key = unregistered)
    known = {r["pubkey"] for r in c.execute("SELECT pubkey FROM actors WHERE active=1").fetchall()}
    known_dids = {r["did"] for r in c.execute("SELECT did FROM actors WHERE active=1").fetchall()}
    for e in evs:
        sig = e.get("signer", e.get("actor", ""))
        if sig.startswith("demo:"):
            sig = sig[5:]
        if sig not in known and e.get("actor") not in known_dids:
            if not ch.is_active(sig, default=False):
                findings.append(Finding("R08", "critical",
                                        f"Event signed by revoked/unregistered actor {e.get('actor','')[:24]}",
                                        [e.get("event_hash", "")], "Reject new events; rotate keys"))
                break
    # R09 integrity spot-check
    for e in evs:
        if e.get("event_hash") != event_hash({k: e[k] for k in
                ("serial","type","actor","ts","lat_mdeg","lon_mdeg","biz_step","to_actor","prev_hash","ref_hash","seq") if k in e}):
            findings.append(Finding("R09", "critical", "Broken hash/signature chain",
                                    [e.get("event_hash", "")], "Run /audit/integrity"))
            break
    anchored = bool(c.execute("SELECT id FROM batches LIMIT 1").fetchone())
    score, conf = score_and_confidence(findings, len(evs), anchored)
    verdict, display = verdict_for(findings)
    if not prod:
        verdict, display = "UNKNOWN", "Unknown serial"
    checks = [{"id": "LOT_COMMITMENT",
               "status": "pass" if lp.get("status") == "pass" else ("na" if not prod else "fail"),
               "evidence": {"root": lp.get("root"), "proof_len": len(lp.get("proof", []))}},
              {"id": "CHAIN_INTEGRITY", "status": "fail" if any(f.rule_id == "R09" for f in findings) else "pass"}]
    for f in findings:
        checks.append({"id": f.rule_id, "status": "fail", "severity": f.severity,
                       "reason": f.reason, "evidence_refs": f.evidence_refs, "next_step": f.next_step})
    return {"serial": serial, "verdict": verdict, "display": display, "risk_score": score,
            "confidence": conf,
            "checks": checks,
            "uncertainty": ["Only %d observations" % len(evs)] if len(evs) < 3 else [],
            "next_steps": [f.next_step for f in findings][:3] or ["No action"],
            "proofs": {"lot": lp}}


@app.get("/products/{serial}/verify")
@app.get("/anomaly/{serial}")
@app.get("/events/verify")
def verify(serial: str | None = None):
    if not serial:
        raise HTTPException(422, "serial required")
    return build_verdict(serial)


@app.get("/products/{serial}/bundle")
def bundle(serial: str):
    c = con()
    evs = _events(c, serial)
    lp = _lot_proof(c, serial)
    return {"serial": serial, "events": evs, "lot_proof": lp,
            "chain": {"rpc": os.environ.get("CHAIN_RPC", "local"),
                      "contract": os.environ.get("CONTRACT", "local-mimic"),
                      "anchors": _load_anchors()},
            "verdict": build_verdict(serial)}


def _load_anchors():
    try:
        import json as j
        with open(ch.STORE) as f:
            return j.load(f).get("anchors", [])
    except FileNotFoundError:
        return []


@app.post("/anchor/run")
def anchor_run():
    from .merkle import root_of as _r
    c = con()
    rows = c.execute("SELECT id, event_hash FROM events WHERE batch_id IS NULL ORDER BY id LIMIT 10").fetchall()
    if not rows:
        return {"anchored": 0}
    leaves = [bytes.fromhex(r["event_hash"][2:]) for r in rows]
    # leaves are already hashes; merkle re-hashes — for audit consistency hash the hex bytes
    root = "0x" + _r([bytes.fromhex(r["event_hash"][2:]) for r in rows]).hex()
    aid, tx = ch.anchor_batch(root, len(rows))
    c.execute("INSERT INTO batches(root,count,tx,ts) VALUES(?,?,?,?)",
              (root, len(rows), tx, int(time.time())))
    bid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
    for r in rows:
        c.execute("UPDATE events SET batch_id=? WHERE id=?", (bid, r["id"]))
    c.commit()
    return {"anchored": len(rows), "anchor_id": aid, "root": root, "tx": tx}


@app.get("/audit/integrity")
def audit():
    c = con()
    bad = []
    keys = ("serial", "type", "actor", "ts", "lat_mdeg", "lon_mdeg",
            "biz_step", "to_actor", "prev_hash", "ref_hash", "seq")
    for r in c.execute("SELECT event_hash, raw FROM events").fetchall():
        try:
            raw = json.loads(r["raw"] or "{}")
        except Exception:
            bad.append(r["event_hash"])
            continue
        body = {k: raw[k] for k in keys if k in raw}
        if event_hash(body) != r["event_hash"]:
            bad.append(r["event_hash"])
    return {"checked": c.execute("SELECT COUNT(*) c FROM events").fetchone()["c"],
            "mismatches": bad, "ok": not bad}


@app.post("/products/{serial}/claim")
def claim(serial: str, body: ClaimIn):
    c = con()
    p = c.execute("SELECT * FROM products WHERE serial=?", (serial,)).fetchone()
    if not p:
        raise HTTPException(404, "unknown")
    if hashlib.sha256(body.secret.encode()).hexdigest() != p["secret_hash"]:
        c.execute("INSERT INTO claims(serial,session_hash,ts,outcome) VALUES(?,?,?,?)",
                  (serial, hashlib.sha256(body.session_id.encode()).hexdigest(), int(time.time()), "wrong_secret"))
        c.commit()
        return {"ok": False, "reason": "invalid"}
    sh = hashlib.sha256(body.session_id.encode()).hexdigest()
    if p["status"] == "CLAIMED":
        if p["claimed_session_hash"] == sh:
            return {"ok": True, "status": "CLAIMED", "idempotent": True}
        c.execute("UPDATE products SET status='CONTESTED' WHERE serial=?", (serial,))
        c.execute("INSERT INTO claims(serial,session_hash,ts,outcome) VALUES(?,?,?,?)",
                  (serial, sh, int(time.time()), "second_claim"))
        c.commit()
        return {"ok": False, "reason": "R12 second first-buyer — contested", "rule": "R12"}
    if p["status"] == "CONTESTED":
        c.execute("INSERT INTO claims(serial,session_hash,ts,outcome) VALUES(?,?,?,?)",
                  (serial, sh, int(time.time()), "contested_repeat"))
        c.commit()
        return {"ok": False, "reason": "R12 already contested", "rule": "R12"}
    if p["status"] != "SOLD":
        c.execute("INSERT INTO claims(serial,session_hash,ts,outcome) VALUES(?,?,?,?)",
                  (serial, sh, int(time.time()), "pre_sale"))
        c.commit()
        return {"ok": False, "reason": "R02 secret before sale", "rule": "R02"}
    c.execute("UPDATE products SET status='CLAIMED', claimed_session_hash=?, claimed_ts=? WHERE serial=?",
              (sh, int(time.time()), serial))
    c.execute("INSERT INTO claims(serial,session_hash,ts,outcome) VALUES(?,?,?,?)",
              (serial, sh, int(time.time()), "claimed"))
    c.commit()
    return {"ok": True, "status": "CLAIMED"}


@app.post("/lots/{lot_id}/recall")
def recall(lot_id: str):
    c = con()
    import hashlib as hl
    ch.freeze_lot(hl.sha256(lot_id.encode()).hexdigest() if False else lot_id, True)
    # store by raw lot_id (chain mimic uses raw key)
    c.execute("UPDATE products SET status='FROZEN' WHERE lot_id=? AND status!='CLAIMED'", (lot_id,))
    c.execute("UPDATE lots SET frozen=1 WHERE lot_id=?", (lot_id,))
    c.commit()
    return {"lot": lot_id, "frozen": True, **blast_radius(lot_id)}


@app.get("/lots/{lot_id}/blast-radius")
def blast_radius_api(lot_id: str):
    return blast_radius(lot_id)


def blast_radius(lot_id: str):
    c = con()
    units = c.execute("SELECT serial,status,current_custodian,container_id FROM products WHERE lot_id=?",
                      (lot_id,)).fetchall()
    by_status: dict = {}
    by_cust: dict = {}
    for u in units:
        by_status[u["status"]] = by_status.get(u["status"], 0) + 1
        by_cust[(u["current_custodian"], u["status"])] = by_cust.get((u["current_custodian"], u["status"]), 0) + 1
    inv = c.execute("SELECT COUNT(*) n FROM unknown_attempts WHERE lot_id=?", (lot_id,)).fetchone()["n"]
    lot = c.execute("SELECT size FROM lots WHERE lot_id=?", (lot_id,)).fetchone()
    return {"lot": lot_id, "total_units": len(units), "by_status": by_status,
            "invented_attempts": inv, "produced": lot["size"] if lot else None,
            "overflow": bool(lot and inv > 0),
            "by_custodian": [{"actor": a, "status": s, "units": n} for (a, s), n in by_cust.items()]}


@app.post("/anchor/document")
def anchor_doc(body: dict, x_api_key: str | None = Header(default=None)):
    h = body.get("doc_hash", "")
    aid, tx = ch.anchor_batch(h if h.startswith("0x") else "0x" + hashlib.sha256(h.encode()).hexdigest(), 1)
    return {"anchor_id": aid, "tx": tx}


# ---------- redteam (localhost only) ----------
@app.post("/redteam/{attack}")
def redteam(attack: str):
    require_redteam()
    c = con()
    row = c.execute("SELECT serial,lot_id FROM products LIMIT 1").fetchone()
    if not row:
        raise HTTPException(400, "seed first")
    s = row["serial"]
    base = int(time.time())
    if attack == "clone":
        sign_event_api(EventIn(serial=s, type="SCAN", actor="did:attacker", ts=base, lat=19.07, lon=72.87))
        sign_event_api(EventIn(serial=s, type="HANDOVER_OFFER", actor="did:attacker", ts=base + 600, to_actor="did:attacker2"))
        return sign_event_api(EventIn(serial=s, type="SCAN", actor="did:attacker2", ts=base + 1200, lat=28.61, lon=77.20))
    if attack == "invented":
        out = []
        for i in range(5):
            fake = f"{row['lot_id']}-FAKE{i:04d}"
            try:
                sign_event_api(EventIn(serial=fake, type="SCAN", actor="did:attacker", ts=base))
            except HTTPException as ex:
                out.append({"serial": fake, "rejected": ex.detail})
        return {"invented": out}
    if attack == "forge":
        try:
            return sign_event_api(EventIn(serial="NOPE-000001", type="SCAN", actor="did:unknown", ts=base))
        except HTTPException as ex:
            return {"rejected": ex.detail, "rule": "R08"}
    if attack == "handover":
        return sign_event_api(EventIn(serial=s, type="HANDOVER_OFFER", actor="did:attacker", ts=base, to_actor="did:attacker2"))
    if attack == "tamper":
        import json as _j
        row = c.execute("SELECT rowid AS rid, raw FROM events WHERE serial=? ORDER BY rid LIMIT 1", (s,)).fetchone()
        raw = _j.loads(row["raw"])
        raw["biz_step"] = "tampered"
        c.execute("UPDATE events SET raw=?, biz_step='tampered' WHERE rowid=?", (_j.dumps(raw), row["rid"]))
        c.commit()
        return {"tampered": s, "run": "/audit/integrity"}
    if attack in ("double_claim", "postsale"):
        return {"hint": "use /products/{serial}/claim and SCAN after SOLD"}
    raise HTTPException(400, "unknown attack")


@app.post("/seed/demo")
def seed_demo():
    create_lot(LotIn(lot_id="LOT-DEMO-A", size=10, manufacturer="did:maker1"))
    c = con()
    for did, role in (("did:maker1", "manufacturer"), ("did:transporter1", "transporter"),
                      ("did:retailer1", "retailer"), ("did:attacker", "transporter"),
                      ("did:attacker2", "retailer")):
        c.execute("INSERT OR IGNORE INTO actors(did,pubkey,role) VALUES(?,?,?)", (did, did, role))
    c.commit()
    return {"seeded": True}

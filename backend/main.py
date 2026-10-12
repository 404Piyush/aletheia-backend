"""FastAPI backend — blockchain side, no UI. Run: uvicorn backend.main:app --port 8000"""
import hashlib
import json
import os
import secrets
import sqlite3
import threading
import time
from contextvars import ContextVar

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from .crypto import event_hash, sign_event, verify_event, GENESIS_PREV, to_hex
from .db import connect
from .merkle import root_of, get_proof, verify_proof
from . import chain as ch
from .rules.engine import (r01_fork, r04_travel, r07_custody, score_and_confidence,
                           verdict_for, Finding)

app = FastAPI(title="Trust Fabric Chain API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.environ.get("CORS_ORIGINS", "*").split(","),
    allow_methods=["*"],
    allow_headers=["*"],
)
CUSTODY = ("MANUFACTURED", "HANDOVER_OFFER", "HANDOVER_ACCEPT", "SOLD")


@app.get("/")
def root():
    return {"service": "trust-fabric", "docs": "/docs", "health": "/converge/health",
            "chain": ch.chain_info()}


@app.get("/health")
def health():
    c = con()
    evts = c.execute("SELECT COUNT(*) n FROM events").fetchone()["n"]
    inc = c.execute("SELECT COUNT(*) n FROM incidents WHERE status='open'").fetchone()["n"]
    return {"status": "ok", "service": "trust-fabric", "events": evts,
            "open_incidents": inc, "chain": ch.chain_info()}


_db_lock = threading.Lock()
_open_conns: ContextVar[list] = ContextVar("conns", default=[])


def con() -> sqlite3.Connection:
    c = connect()
    _open_conns.get().append(c)
    return c


@app.middleware("http")
async def _serialize_db(request: Request, call_next):
    # SQLite demo store: one request at a time, always release connections.
    with _db_lock:
        token = _open_conns.set([])
        try:
            return await call_next(request)
        finally:
            for c in _open_conns.get():
                try:
                    c.close()
                except Exception:
                    pass
            _open_conns.reset(token)


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
        from . import signer as _signer
        try:
            _s = _signer.sign_for(lot.manufacturer, ev)
            ev.update({"signature": _s["signature"], "signer": _s["signer"]})
        except Exception:
            ev.update({"signature": "demo:manufacturer", "signer": lot.manufacturer})
        c.execute(
        "INSERT INTO events(serial,type,actor,ts,lat_mdeg,lon_mdeg,biz_step,to_actor,prev_hash,ref_hash,seq,event_hash,signature,signer,raw) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (s, "MANUFACTURED", lot.manufacturer, ev["ts"], None, None, "commissioning", None,
         GENESIS_PREV, None, 0, ev["event_hash"], ev["signature"], ev["signer"], json.dumps(ev)))
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
    from . import signer as _signer
    if e.signature:
        probe = dict(body, signature=e.signature, signer=e.signer or e.actor)
        if not verify_event(probe):
            raise HTTPException(401, "bad signature for signer")
        sig, signer = e.signature, e.signer or e.actor
    else:
        row = c.execute("SELECT pubkey FROM actors WHERE did=? AND active=1",
                        (e.actor,)).fetchone()
        if row and row["pubkey"] == _signer.pubkey_hex(e.actor):
            signed = _signer.sign_for(e.actor, body)
            sig, signer = signed["signature"], signed["signer"]
        else:
            sig, signer = f"unsigned:{e.actor}", e.signer or e.actor
    try:
        c.execute(
            "INSERT INTO events(serial,type,actor,ts,lat_mdeg,lon_mdeg,biz_step,to_actor,prev_hash,ref_hash,seq,event_hash,signature,signer,raw) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (e.serial, e.type, e.actor, ts, lat_mdeg, lon_mdeg, e.biz_step, e.to_actor,
             body["prev_hash"], body["ref_hash"], body["seq"], body["event_hash"], sig, signer, json.dumps(body)))
    except sqlite3.IntegrityError:
        raise HTTPException(409, "duplicate event (replay rejected)")
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
    product = None
    if prod:
        product = {"lot_id": prod["lot_id"], "status": prod["status"],
                   "custodian": prod["current_custodian"]}
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
    out = {"serial": serial, "verdict": verdict, "display": display, "risk_score": score,
           "confidence": conf, "product": product,
           "checks": checks,
           "uncertainty": ["Only %d observations" % len(evs)] if len(evs) < 3 else [],
           "next_steps": [f.next_step for f in findings][:3] or ["No action"],
           "proofs": {"lot": lp}}
    if prod:
        _upsert_incident(c, serial, verdict, findings)
    return out


_SEV_RANK = {"low": 0, "medium": 1, "high": 2, "critical": 3}


def _upsert_incident(c, serial: str, verdict: str, findings: list) -> None:
    """One incident per serial; findings deduped by (rule, evidence)."""
    seen = {}
    for f in findings:
        seen[(f.rule_id, tuple(f.evidence_refs))] = {
            "rule_id": f.rule_id, "severity": f.severity, "reason": f.reason,
            "evidence_refs": list(f.evidence_refs), "next_step": f.next_step}
    sev = max([f["severity"] for f in seen.values()],
              key=lambda s: _SEV_RANK.get(s, 0), default="low")
    c.execute(
        "INSERT INTO incidents(serial,severity,status,findings_json,updated_ts) VALUES(?,?,?,?,?) "
        "ON CONFLICT(serial) DO UPDATE SET severity=excluded.severity, status=excluded.status, "
        "findings_json=excluded.findings_json, updated_ts=excluded.updated_ts",
        (serial, sev, "open" if seen else "clear",
         json.dumps(sorted(seen.values(), key=lambda f: f["rule_id"])), int(time.time())))
    c.commit()


@app.get("/incidents")
def list_incidents(status: str = "all", severity: str = "all"):
    c = con()
    q = "SELECT serial,severity,status,findings_json,updated_ts FROM incidents ORDER BY updated_ts DESC"
    rows = [dict(r) for r in c.execute(q).fetchall()]
    for r in rows:
        r["findings"] = json.loads(r.pop("findings_json") or "[]")
        r["count"] = len(r["findings"])
    if status != "all":
        rows = [r for r in rows if r["status"] == status]
    if severity != "all":
        rows = [r for r in rows if r["severity"] == severity]
    return {"incidents": rows, "total": len(rows)}


@app.get("/incidents/{serial}")
def get_incident(serial: str):
    c = con()
    evs = _events(c, serial)
    timeline = [{"event_hash": e.get("event_hash"), "type": e.get("type"),
                 "actor": e.get("actor"), "ts": e.get("ts"),
                 "biz_step": e.get("biz_step")} for e in evs]
    row = c.execute("SELECT severity,status,findings_json,updated_ts FROM incidents WHERE serial=?",
                    (serial,)).fetchone()
    if not row:
        v = build_verdict(serial)
        if v["verdict"] == "UNKNOWN":
            raise HTTPException(404, "unknown serial")
        row = c.execute("SELECT severity,status,findings_json,updated_ts FROM incidents WHERE serial=?",
                        (serial,)).fetchone()
    return {"serial": serial, "severity": row["severity"], "status": row["status"],
            "findings": json.loads(row["findings_json"] or "[]"),
            "updated_ts": row["updated_ts"], "timeline": timeline}


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
            "chain": {**ch.chain_info(),
                      "rpc": os.environ.get("CHAIN_RPC", "http://localhost:9545")},
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
    # R02 burst: >=5 wrong secrets in 10 min -> throttle
    burst = c.execute(
        "SELECT COUNT(*) n FROM claims WHERE serial=? AND outcome='wrong_secret' AND ts>?",
        (serial, int(time.time()) - 600)).fetchone()["n"]
    if burst >= 5:
        raise HTTPException(429, "too many attempts (R02 burst); try later")
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


@app.get("/products/{serial}/anchor")
def serial_anchor(serial: str):
    """Latest batch anchor covering this serial + lot root + chain head (for UI proof panel)."""
    c = con()
    row = c.execute(
        "SELECT batch_id FROM events WHERE serial=? AND batch_id IS NOT NULL ORDER BY batch_id DESC LIMIT 1",
        (serial,)).fetchone()
    if not row:
        return {"serial": serial, "anchored": False, "lot": _lot_proof(c, serial),
                "chain": ch.chain_info()}
    b = c.execute("SELECT * FROM batches WHERE id=?", (row["batch_id"],)).fetchone()
    info = ch.chain_info()
    head = None
    if ch.USE_CHAIN:
        try:
            head = ch._w3.eth.block_number
        except Exception:
            pass
    return {"serial": serial, "anchored": True, "anchor_id": row["batch_id"] - 1,
            "root": b["root"], "count": b["count"], "tx": b["tx"],
            "block": head, "lot": _lot_proof(c, serial), "chain": info}


@app.post("/anchor/document")
def anchor_doc(body: dict, x_api_key: str | None = Header(default=None)):
    h = body.get("doc_hash", "")
    aid, tx = ch.anchor_batch(h if h.startswith("0x") else "0x" + hashlib.sha256(h.encode()).hexdigest(), 1)
    return {"anchor_id": aid, "tx": tx}


class ConvergeIn(BaseModel):
    serial: str


@app.post("/converge/case")
def converge_case(body: ConvergeIn, request: Request):
    """Push current verdict+bundle to partner handoff platform (queues if down)."""
    from . import convergence as cv
    v = build_verdict(body.serial)
    public = os.environ.get("PUBLIC_BASE", "https://continuity.404piyush.me").rstrip("/")
    bundle_url = f"{public}/products/{body.serial}/bundle"
    out = cv.dispatch_case(body.serial, v, bundle_url)
    if out.get("ok"):
        try:
            p = out.get("partner", {})
            cid = (p.get("id") or p.get("caseId") or p.get("handoffId")
                   if isinstance(p, dict) else None)
            c = con()
            c.execute("INSERT OR REPLACE INTO converge_cases(serial,case_id,ts,last_status) VALUES(?,?,?,?)",
                      (body.serial, str(cid or ""), int(time.time()), "open"))
            c.commit()
        except Exception:
            pass
    return {"serial": body.serial, "verdict": v["verdict"],
            "risk_score": v["risk_score"], **out}


@app.get("/converge/status")
def converge_status(serial: str):
    from . import convergence as cv
    try:
        c = con()
        row = c.execute("SELECT case_id, last_status FROM converge_cases WHERE serial=?",
                        (serial,)).fetchone()
    except Exception:
        row = None
    live = None
    if row and row["case_id"]:
        live = cv.case_detail(row["case_id"])
    return {"serial": serial, "tracked": dict(row) if row else None,
            "live": live, **cv.case_status(serial)}


@app.get("/converge/health")
def converge_health():
    from . import convergence as cv
    m = cv.fetch_actor_map()
    return {"partner_up": cv.partner_up(), "bridge": cv.bridge_status(),
            "partner_base": cv.PARTNER_BASE, "project": cv.PARTNER_PROJECT,
            "actor_map_source": m["source"], "actor_map": m["map"]}


@app.get("/converge/actor-map")
def converge_actor_map():
    from . import convergence as cv
    return cv.fetch_actor_map()


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
            return sign_event_api(EventIn(serial=s, type="SCAN", actor="did:transporter1",
                                          ts=base, signature="0x" + "ff" * 64,
                                          signer="0x" + "ee" * 32))
        except HTTPException as ex:
            return {"rejected": ex.detail, "rule": "R08/R09", "code": ex.status_code}
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
    from . import signer as _signer
    c = con()
    for did, role in (("did:maker1", "manufacturer"), ("did:transporter1", "transporter"),
                      ("did:retailer1", "retailer"), ("did:attacker", "transporter"),
                      ("did:attacker2", "retailer")):
        c.execute("INSERT OR REPLACE INTO actors(did,pubkey,role,active) VALUES(?,?,?,1)",
                  (did, _signer.pubkey_hex(did), role))
    c.commit()
    create_lot(LotIn(lot_id="LOT-DEMO-A", size=10, manufacturer="did:maker1"))
    return {"seeded": True, "signer": "ed25519 device-simulator holds demo keys"}


class SignIn(BaseModel):
    did: str
    payload: dict


@app.get("/signer/pubkey/{did}")
def signer_pubkey(did: str):
    from . import signer as _signer
    return {"did": did, "pubkey": _signer.pubkey_hex(did)}


@app.post("/signer/sign")
def signer_sign(body: SignIn):
    from . import signer as _signer
    return _signer.sign_for(body.did, body.payload)

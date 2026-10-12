"""FastAPI backend — blockchain side, no UI. Run: uvicorn backend.main:app --port 8000"""
import hashlib
import json
import os
import secrets
import sqlite3
import sys
import threading
import time
import traceback
from contextvars import ContextVar

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from .crypto import event_hash, sign_event, verify_event, GENESIS_PREV, to_hex
from .db import connect
from .merkle import root_of, get_proof, verify_proof
from . import chain as ch
from .rules.engine import (r01_fork, r04_travel, r07_custody, score_and_confidence,
                           verdict_for, Finding)

app = FastAPI(title="Trust Fabric Chain API")
_DEFAULT_ORIGINS = ["http://localhost:3000", "http://localhost:3001",
                    "http://localhost:8000", "https://continuity.404piyush.me"]
app.add_middleware(
    CORSMiddleware,
    # Restricted by default: an open `*` lets any website POST events from a
    # visitor's browser. Override with CORS_ORIGINS env (comma-separated).
    allow_origins=os.environ.get("CORS_ORIGINS", ",".join(_DEFAULT_ORIGINS)).split(","),
    allow_methods=["*"],
    allow_headers=["*"],
)
CUSTODY = ("MANUFACTURED", "HANDOVER_OFFER", "HANDOVER_ACCEPT", "SOLD")
KNOWN_EVENT_TYPES = set(CUSTODY) | {"SCAN"}
MAX_TS_SKEW_FUTURE = 300  # reject timestamps >5 min ahead (timeline poisoning)


@app.get("/")
def root():
    return {"service": "trust-fabric", "docs": "/docs", "health": "/converge/health",
            "chain": safe_chain_info()}


@app.get("/health")
def health():
    c = con()
    evts = c.execute("SELECT COUNT(*) n FROM events").fetchone()["n"]
    inc = c.execute("SELECT COUNT(*) n FROM incidents WHERE status='open'").fetchone()["n"]
    return {"status": "ok", "service": "trust-fabric", "events": evts,
            "open_incidents": inc, "chain": safe_chain_info()}


_db_lock = threading.Lock()
# A worker stuck in an unbounded blocking call (e.g. DNS inside an httpx
# request) holds _db_lock for the whole request; the async middleware would
# then block the event loop forever and wedge the server (seen 3x). Bound
# the wait: shed load with 503 instead of hanging. Normal acquisition is
# instant; hitting the timeout means a worker is genuinely stuck.
_LOCK_TIMEOUT_S = float(os.environ.get("DB_LOCK_TIMEOUT_S", "30"))
_open_conns: ContextVar[list] = ContextVar("conns", default=[])


def con() -> sqlite3.Connection:
    c = connect()
    _open_conns.get().append(c)
    return c


@app.middleware("http")
async def _serialize_db(request: Request, call_next):
    # SQLite demo store: one request at a time, always release connections.
    # /debug/threads is the wedge-breaker glass: it must answer even when a
    # worker is stuck holding the lock, so it bypasses the lock entirely
    # (it touches no DB state).
    if request.url.path == "/debug/threads":
        return await call_next(request)
    t0 = time.time()
    acquired = _db_lock.acquire(timeout=_LOCK_TIMEOUT_S)
    if not acquired:
        return JSONResponse(status_code=503,
                            content={"error": "server busy (db lock); retry shortly"})
    token = _open_conns.set([])
    try:
        resp = await call_next(request)
    finally:
        for c in _open_conns.get():
            try:
                c.close()
            except Exception:
                pass
        _open_conns.reset(token)
        _db_lock.release()
    dt = time.time() - t0
    if dt > 5:
        print(f"SLOW {dt:.1f}s {request.method} {request.url.path}", flush=True)
    return resp


@app.get("/debug/threads")
def debug_threads(request: Request):
    """Localhost-only stack dump of every thread. If the server ever wedges
    again, this shows exactly which worker is stuck and in what call."""
    require_local(request)
    frames = sys._current_frames()
    out = {}
    for tid, frame in frames.items():
        out[str(tid)] = "".join(traceback.format_stack(frame))[-2000:]
    return {"threads": len(out), "stacks": out}


def require_redteam():
    if os.environ.get("REDTEAM_MODE") != "1":
        raise HTTPException(403, "redteam disabled")


_LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost", "testclient"}


def require_local(request: Request):
    """Demo-destructive endpoints (seed/reset, attack console, signing oracle)
    must not be reachable through the public tunnel."""
    host = request.client.host if request.client else ""
    if host not in _LOCAL_HOSTS:
        raise HTTPException(403, "localhost only")


def _cap_serial(serial: str) -> str:
    # Partner- and public-facing GETs must not bank unbounded strings into
    # unknown_attempts rows (storage farming) or incident payloads.
    if len(serial) > 128:
        raise HTTPException(422, "serial too long (<=128 chars)")
    return serial


def safe_chain_info() -> dict:
    """chain_info() raises when Besu hiccups; read paths (incl. partner
    verify/bundle) must degrade with a flag, not 500."""
    try:
        return ch.chain_info()
    except Exception:
        return {"mode": "besu-qbft" if ch.USE_CHAIN else "local-mimic",
                "reachable": False}


_DISPATCH_MIN_GAP = 3  # seconds between /converge/case dispatches


def _dispatch_allowed(c) -> bool:
    """Global dispatch throttle: each dispatch holds the DB lock through up
    to ~25s+ of partner timeouts, so unauthenticated spam could wedge the API.
    DB-backed so it holds across workers."""
    now = int(time.time())
    row = c.execute("SELECT val FROM meta WHERE key='last_dispatch'").fetchone()
    if row and now - row["val"] < _DISPATCH_MIN_GAP:
        return False
    c.execute("INSERT INTO meta(key,val) VALUES('last_dispatch',?) "
              "ON CONFLICT(key) DO UPDATE SET val=excluded.val", (now,))
    c.commit()
    return True


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
@app.get("/lots")
def list_lots():
    """Inventory for the UI tower: every lot with live unit counts + freeze flag."""
    c = con()
    out = []
    for lot in c.execute("SELECT lot_id,gtin,size,root,tx,frozen FROM lots ORDER BY lot_id").fetchall():
        by_status: dict = {}
        n = 0
        for u in c.execute("SELECT status FROM products WHERE lot_id=?", (lot["lot_id"],)).fetchall():
            by_status[u["status"]] = by_status.get(u["status"], 0) + 1
            n += 1
        out.append({"lot_id": lot["lot_id"], "gtin": lot["gtin"], "size": lot["size"],
                    "root": lot["root"], "tx": lot["tx"], "frozen": bool(lot["frozen"]),
                    "units": n, "by_status": by_status})
    return {"lots": out}


@app.get("/lots/{lot_id}")
def lot_detail_api(lot_id: str):
    """Single-lot detail for the UI tower (units, custodians, anchor, freeze flag)."""
    if len(lot_id) > 128:
        raise HTTPException(422, "lot id too long (<=128 chars)")
    c = con()
    lot = c.execute("SELECT lot_id,gtin,size,root,tx,frozen FROM lots WHERE lot_id=?",
                    (lot_id,)).fetchone()
    if not lot:
        raise HTTPException(404, "unknown lot")
    units = [{"serial": u["serial"], "status": u["status"],
              "custodian": u["current_custodian"]}
             for u in c.execute("SELECT serial,status,current_custodian FROM products WHERE lot_id=? ORDER BY serial",
                                (lot_id,)).fetchall()]
    custodians = sorted({u["custodian"] for u in units if u["custodian"]})
    return {"lot_id": lot["lot_id"], "gtin": lot["gtin"], "size": lot["size"],
            "root": lot["root"], "tx": lot["tx"], "frozen": bool(lot["frozen"]),
            "units": units, "custodians": custodians}


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
    if isinstance(tx, str) and tx.startswith("exists:"):
        # Same lot id, different content -> refuse: silently keeping the old
        # on-chain root while storing a new DB root breaks R03 for every unit.
        try:
            onchain = ch.lot_root(lot.lot_id)
        except Exception:
            onchain = None
        if onchain and onchain.lower() != root.lower():
            raise HTTPException(409, "lot id already registered on-chain with a different root")
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


def _unknown_flood(c, lot_id: str) -> bool:
    """Throttle invented-serial spam: >50 unknown attempts for one lot in
    10 min means someone is farming unknown_attempts rows (storage DoS)."""
    n = c.execute("SELECT COUNT(*) n FROM unknown_attempts WHERE lot_id=? AND ts>?",
                  (lot_id, int(time.time()) - 600)).fetchone()["n"]
    return n >= 50


@app.post("/events/sign")
def sign_event_api(e: EventIn, skip_ts_bounds: bool = False):
    c = con()
    # --- input validation (reject absurd/forged fields before touching state)
    now = int(time.time())
    if len(e.serial) > 128 or len(e.actor) > 128 or (e.to_actor and len(e.to_actor) > 128):
        raise HTTPException(422, "serial/actor too long")
    if e.type not in KNOWN_EVENT_TYPES:
        raise HTTPException(422, f"unknown event type {e.type[:32]}")
    if len(e.biz_step) > 64:
        raise HTTPException(422, "biz_step too long")
    ts = e.ts or now
    # skip_ts_bounds is Python-internal only (never a request field): the
    # localhost-gated redteam console must be able to stage attacks with
    # future-dated scans (e.g. the clone button's travel pair). Public
    # ingest always enforces the skew bound.
    if not (0 < ts <= now + MAX_TS_SKEW_FUTURE) and not skip_ts_bounds:
        raise HTTPException(422, "ts out of range (must be <= now+5min)")
    if e.lat is not None and not (-90 <= e.lat <= 90):
        raise HTTPException(422, "lat out of range")
    if e.lon is not None and not (-180 <= e.lon <= 180):
        raise HTTPException(422, "lon out of range")
    prod = c.execute("SELECT * FROM products WHERE serial=?", (e.serial,)).fetchone()
    if not prod:
        # unknown serial with known lot prefix -> R03 bookkeeping
        lot_guess = "-".join(e.serial.split("-")[:-1]) if "-" in e.serial else ""
        if lot_guess:
            if _unknown_flood(c, lot_guess):
                raise HTTPException(429, "too many unknown-serial attempts for this lot")
            c.execute("INSERT INTO unknown_attempts(serial,lot_id,ts,source) VALUES(?,?,?,?)",
                      (e.serial, lot_guess, now, e.actor))
            c.commit()
        raise HTTPException(404, "unknown serial")
    try:
        frozen = ch.is_frozen(prod["lot_id"])
    except Exception:
        # Fail CLOSED: if the chain is unreachable we cannot know whether the
        # lot is recalled, so pause writes instead of allowing them.
        raise HTTPException(503, "chain state unknown; writes paused")
    if frozen:
        raise HTTPException(423, "lot frozen/recalled")
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
        if _unknown_flood(c, ""):
            raise HTTPException(429, "too many unknown-serial attempts")
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
    return build_verdict(_cap_serial(serial))


@app.get("/products/{serial}/bundle")
def bundle(serial: str):
    serial = _cap_serial(serial)
    c = con()
    evs = _events(c, serial)
    lp = _lot_proof(c, serial)
    return {"serial": serial, "events": evs, "lot_proof": lp,
            "chain": safe_chain_info(),
            "verdict": build_verdict(serial)}


def _load_anchors():
    try:
        import json as j
        with open(ch.STORE) as f:
            return j.load(f).get("anchors", [])
    except FileNotFoundError:
        return []


@app.post("/anchor/run")
def anchor_run(request: Request):
    # Operator tooling (chain writes + batch churn): not partner-facing,
    # localhost-only like the other state-changing debug endpoints.
    require_local(request)
    from .merkle import root_of as _r
    c = con()
    rows = c.execute("SELECT id, event_hash FROM events WHERE batch_id IS NULL ORDER BY id LIMIT 10").fetchall()
    if not rows:
        return {"anchored": 0}
    leaves = [bytes.fromhex(r["event_hash"][2:]) for r in rows]
    # leaves are already hashes; merkle re-hashes — for audit consistency hash the hex bytes
    root = "0x" + _r([bytes.fromhex(r["event_hash"][2:]) for r in rows]).hex()
    aid, tx = ch.anchor_batch(root, len(rows))
    c.execute("INSERT INTO batches(root,count,tx,ts,aid) VALUES(?,?,?,?,?)",
              (root, len(rows), tx, int(time.time()), aid))
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
    session = (body.session_id or "").strip()
    if not session or len(session) > 128 or len(body.secret) > 256:
        # Empty session is an R12 bypass: two buyers with "" hash identically
        # and the second gets idempotent CLAIMED instead of contested.
        raise HTTPException(422, "session_id required (non-empty, <=128 chars)")
    body = ClaimIn(secret=body.secret, session_id=session)
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
    try:
        ch.freeze_lot(lot_id, True)
    except Exception as ex:
        # Chain first, DB second: never mark frozen locally when the on-chain
        # freeze failed, or writes stay blocked here while the chain is open.
        raise HTTPException(502, f"on-chain freeze failed ({type(ex).__name__}); lot NOT frozen")
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
    # nested tree: pallets -> cases -> units (flat keys above unchanged)
    conts = {r["id"]: dict(r) for r in
             c.execute("SELECT id,type,parent_id FROM containers WHERE lot_id=?", (lot_id,)).fetchall()}
    by_container: dict = {}
    unassigned = []
    for u in units:
        node = {"serial": u["serial"], "status": u["status"],
                "custodian": u["current_custodian"]}
        if u["container_id"]:
            by_container.setdefault(u["container_id"], []).append(node)
        else:
            unassigned.append(node)
    tree = []
    for cid, cm in conts.items():
        if cm["type"] == "PALLET" or not cm["parent_id"]:
            cases = [{"case_id": kid,
                      "units": sorted(by_container.get(kid, []), key=lambda n: n["serial"])}
                     for kid, kcm in conts.items() if kcm["parent_id"] == cid]
            # pallet-level units (packed directly, no case)
            direct = [n for n in by_container.get(cid, [])]
            if direct:
                cases.append({"case_id": None, "units": sorted(direct, key=lambda n: n["serial"])})
            tree.append({"pallet_id": cid, "cases": cases})
    # orphan cases (parent pallet missing) + unpacked units
    orphans = [{"case_id": kid, "units": sorted(by_container.get(kid, []), key=lambda n: n["serial"])}
               for kid, kcm in conts.items()
               if kcm["type"] == "CASE" and kcm["parent_id"] not in conts]
    if orphans or unassigned:
        tree.append({"pallet_id": None,
                     "cases": orphans + ([{"case_id": None,
                                           "units": sorted(unassigned, key=lambda n: n["serial"])}]
                                         if unassigned else [])})
    return {"lot": lot_id, "total_units": len(units), "by_status": by_status,
            "invented_attempts": inv, "produced": lot["size"] if lot else None,
            "overflow": bool(lot and inv > 0),
            "by_custodian": [{"actor": a, "status": s, "units": n} for (a, s), n in by_cust.items()],
            "claimed_units": by_status.get("CLAIMED", 0),
            "tree": tree}


@app.get("/products/{serial}/anchor")
def serial_anchor(serial: str):
    """Latest batch anchor covering this serial + lot root + chain head (for UI proof panel)."""
    serial = _cap_serial(serial)
    c = con()
    row = c.execute(
        "SELECT batch_id FROM events WHERE serial=? AND batch_id IS NOT NULL ORDER BY batch_id DESC LIMIT 1",
        (serial,)).fetchone()
    if not row:
        return {"serial": serial, "anchored": False, "lot": _lot_proof(c, serial),
                "chain": safe_chain_info()}
    b = c.execute("SELECT * FROM batches WHERE id=?", (row["batch_id"],)).fetchone()
    if not b:
        return {"serial": serial, "anchored": False, "note": "batch row missing",
                "lot": _lot_proof(c, serial), "chain": safe_chain_info()}
    info = safe_chain_info()
    head = None
    if ch.USE_CHAIN and info.get("reachable", True):
        try:
            head = ch._w3.eth.block_number
        except Exception:
            pass
    # aid is the chain-side anchor id stored at anchor time. Pre-audit rows
    # have aid NULL (the old batch_id-1 arithmetic breaks after any DB reset,
    # because the chain keeps counting) — the per-row tx hash stays the truth.
    return {"serial": serial, "anchored": True, "anchor_id": b["aid"],
            "root": b["root"], "count": b["count"], "tx": b["tx"],
            "block": head, "lot": _lot_proof(c, serial), "chain": info}


@app.post("/anchor/document")
def anchor_doc(body: dict, request: Request, x_api_key: str | None = Header(default=None)):
    key = os.environ.get("ANCHOR_API_KEY", "")
    if key:
        if x_api_key != key:
            raise HTTPException(403, "bad api key")
    else:
        require_local(request)
    h = body.get("doc_hash", "")
    if not isinstance(h, str) or len(h) > 256:
        raise HTTPException(422, "doc_hash must be a short hex string (<=256 chars)")
    aid, tx = ch.anchor_batch(h if h.startswith("0x") else "0x" + hashlib.sha256(h.encode()).hexdigest(), 1)
    return {"anchor_id": aid, "tx": tx}


class ConvergeIn(BaseModel):
    serial: str = Field(max_length=128)
    force: bool = False


# Verdicts worth paging the partner's custodians about (their incident enum,
# PDF 2.1). Pushing a VERIFIED unit would open a junk handoff + Besu anchor
# + 3 recall tasks on his box; require explicit force for that.
_DISPATCHABLE = {"CLONE_DETECTED", "TAMPERED", "SUSPICIOUS"}


@app.post("/converge/case")
def converge_case(body: ConvergeIn, request: Request):
    """Push current verdict+bundle to partner handoff platform (queues if down)."""
    from . import convergence as cv
    v = build_verdict(body.serial)
    if v["verdict"] == "UNKNOWN":
        raise HTTPException(422, "unknown serial: no verdict to dispatch")
    if v["verdict"] not in _DISPATCHABLE and not body.force:
        raise HTTPException(422, f"verdict is {v['verdict']}: nothing to escalate "
                                 "(re-run with force=true to dispatch anyway)")
    c0 = con()
    if not _dispatch_allowed(c0):
        raise HTTPException(429, "dispatch throttled (one case per few seconds); retry shortly")
    public = os.environ.get("PUBLIC_BASE", "https://continuity.404piyush.me").rstrip("/")
    bundle_url = f"{public}/products/{body.serial}/bundle"
    case_id = cv._new_case_id(body.serial)
    out = cv.dispatch_case(body.serial, v, bundle_url, case_id)
    if out.get("ok"):
        try:
            p = out.get("partner", {})
            cid = (p.get("id") or p.get("caseId") or p.get("handoffId")
                   if isinstance(p, dict) else None)
            cid = cv.valid_case_id(cid) or case_id
            hid = cv.valid_case_id(p.get("handoffId")) if isinstance(p, dict) else ""
            iid = cv.valid_case_id(p.get("incidentId")) if isinstance(p, dict) else ""
            c = con()
            c.execute("INSERT OR REPLACE INTO converge_cases(serial,case_id,ts,last_status,handoff_id,incident_id) VALUES(?,?,?,?,?,?)",
                      (body.serial, cid, int(time.time()), "open", hid, iid))
            c.commit()
        except Exception:
            pass
    return {"serial": body.serial, "verdict": v["verdict"],
            "risk_score": v["risk_score"], "caseId": out.get("caseId", case_id),
            "handoffId": out.get("partner", {}).get("handoffId") if isinstance(out.get("partner"), dict) else None,
            "incidentId": out.get("partner", {}).get("incidentId") if isinstance(out.get("partner"), dict) else None,
            **out}


@app.get("/converge/status")
def converge_status(serial: str):
    from concurrent.futures import ThreadPoolExecutor
    from . import convergence as cv
    try:
        c = con()
        row = c.execute("SELECT case_id, handoff_id, incident_id, last_status FROM converge_cases WHERE serial=?",
                        (serial,)).fetchone()
    except Exception:
        row = None
    # case_detail + case_status are independent partner calls (~17s each on
    # his box); run concurrently so one status check holds the lock ~25s,
    # not ~50s sequential.
    live = None
    with ThreadPoolExecutor(max_workers=2) as pool:
        f_live = pool.submit(cv.case_detail, row["case_id"]) if row and row["case_id"] else None
        f_matches = pool.submit(cv.case_status, serial)
        if f_live is not None:
            live = f_live.result()
        matches = f_matches.result()
    return {"serial": serial, "tracked": dict(row) if row else None,
            "live": live, **matches}


@app.get("/converge/health")
def converge_health():
    from concurrent.futures import ThreadPoolExecutor
    from . import convergence as cv
    # The partner box answers in ~17s; run the three probes concurrently so
    # health costs one latency, not three. Each probe has its own timeout.
    with ThreadPoolExecutor(max_workers=3) as pool:
        f_map = pool.submit(cv.fetch_actor_map)
        f_up = pool.submit(cv.partner_up)
        f_bridge = pool.submit(cv.bridge_status)
        m, up, bridge = f_map.result(), f_up.result(), f_bridge.result()
    return {"partner_up": up, "bridge": bridge,
            "partner_base": cv.PARTNER_BASE, "project": cv.PARTNER_PROJECT,
            "actor_map_source": m["source"], "actor_map": m["map"]}


@app.get("/converge/actor-map")
def converge_actor_map():
    from . import convergence as cv
    return cv.fetch_actor_map()


# ---------- redteam (localhost only) ----------
@app.post("/redteam/{attack}")
def redteam(attack: str, request: Request):
    require_local(request)
    require_redteam()
    c = con()
    row = c.execute("SELECT serial,lot_id FROM products LIMIT 1").fetchone()
    if not row:
        raise HTTPException(400, "seed first")
    s = row["serial"]
    base = int(time.time())
    if attack == "clone":
        sign_event_api(EventIn(serial=s, type="SCAN", actor="did:attacker", ts=base, lat=19.07, lon=72.87))
        sign_event_api(EventIn(serial=s, type="HANDOVER_OFFER", actor="did:attacker", ts=base + 600, to_actor="did:attacker2"), skip_ts_bounds=True)
        return sign_event_api(EventIn(serial=s, type="SCAN", actor="did:attacker2", ts=base + 1200, lat=28.61, lon=77.20), skip_ts_bounds=True)
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
def seed_demo(request: Request):
    require_local(request)
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


@app.post("/seed/tree")
def seed_tree(request: Request):
    """Phase 3: LOT-TREE (8 units) packed as pallet -> cases -> units with mixed
    custodians. One unit SOLD+CLAIMED (recall must skip it), one SOLD, rest ACTIVE
    across transporter/retailer. Re-runnable: resets the tree."""
    require_local(request)
    c = con()
    lot_id = "LOT-TREE"
    mk, tp, rt = "did:maker1", "did:transporter1", "did:retailer1"
    lot = create_lot(LotIn(lot_id=lot_id, size=8, manufacturer=mk))
    serials = [f"{lot_id}-{i:06d}" for i in range(1, 9)]
    secrets = lot.get("sample_secrets", {})

    def _ev(serial, type, actor, to=None, ts=None):
        return sign_event_api(EventIn(serial=serial, type=type, actor=actor,
                                      to_actor=to, ts=ts))

    base = int(time.time())
    tick = [base]

    def _t():
        tick[0] += 1
        return tick[0]

    # C-T1 (in P-T1): 3 units to transporter; 000003 goes on to retailer->SOLD->CLAIMED
    for s in serials[:3]:
        _ev(s, "HANDOVER_OFFER", mk, to=tp, ts=_t())
        _ev(s, "HANDOVER_ACCEPT", tp, ts=_t())
    s3 = serials[2]
    _ev(s3, "HANDOVER_OFFER", tp, to=rt, ts=_t())
    _ev(s3, "HANDOVER_ACCEPT", rt, ts=_t())
    _ev(s3, "SOLD", rt, ts=_t())
    sec3 = secrets.get(s3)
    claim_out = None
    if sec3:
        claim_out = claim(s3, ClaimIn(secret=sec3, session_id="buyer1-session"))
    # C-T2 (in P-T1): 000004 retailer->SOLD, 000005 stays ACTIVE at maker
    s4, s5 = serials[3], serials[4]
    _ev(s4, "HANDOVER_OFFER", mk, to=rt, ts=_t())
    _ev(s4, "HANDOVER_ACCEPT", rt, ts=_t())
    _ev(s4, "SOLD", rt, ts=_t())
    # C-T3 (in P-T2): 3 units to retailer
    for s in serials[5:]:
        _ev(s, "HANDOVER_OFFER", mk, to=rt, ts=_t())
        _ev(s, "HANDOVER_ACCEPT", rt, ts=_t())

    c.execute("INSERT OR REPLACE INTO containers(id,type,parent_id,lot_id) VALUES(?,?,?,?)",
              ("P-T1", "PALLET", None, lot_id))
    c.execute("INSERT OR REPLACE INTO containers(id,type,parent_id,lot_id) VALUES(?,?,?,?)",
              ("P-T2", "PALLET", None, lot_id))
    packing = {"C-T1": ("P-T1", serials[:3]), "C-T2": ("P-T1", serials[3:5]),
               "C-T3": ("P-T2", serials[5:])}
    for case, (pallet, units) in packing.items():
        c.execute("INSERT OR REPLACE INTO containers(id,type,parent_id,lot_id) VALUES(?,?,?,?)",
                  (case, "CASE", pallet, lot_id))
        for s in units:
            c.execute("UPDATE products SET container_id=? WHERE serial=?", (case, s))
    c.commit()
    return {"seeded": True, "lot_id": lot_id, "root": lot["root"], "tx": lot["tx"],
            "packing": {k: v[1] for k, v in packing.items()},
            "claimed": {"serial": s3, "result": claim_out},
            "sold_unclaimed": s4}


class SignIn(BaseModel):
    did: str
    payload: dict


@app.get("/signer/pubkey/{did}")
def signer_pubkey(did: str):
    from . import signer as _signer
    return {"did": did, "pubkey": _signer.pubkey_hex(did)}


@app.post("/signer/sign")
def signer_sign(body: SignIn, request: Request):
    require_local(request)
    from . import signer as _signer
    return _signer.sign_for(body.did, body.payload)

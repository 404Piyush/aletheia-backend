# Trust Fabric — Blockchain Side (no UI)

Permissioned-write / public-read provenance. QR assumed clonable; fakes caught by impossible history.

## Quick start
```bash
pip install -r requirements.txt
export REDTEAM_MODE=1 DB_PATH=trust.db ANCHOR_STORE=anchors.json
uvicorn backend.main:app --port 8000
pytest tests/ -x -q
```

## Flow
1. `POST /seed/demo` → demo actors
2. `POST /lots {"lot_id":"LOT-A","size":20,"manufacturer":"did:maker1"}` → root+tx, sample secrets in response
3. `POST /events/sign {"serial":"LOT-A-000001","type":"SCAN","actor":"did:transporter1","lat":19.07,"lon":72.87}`
4. `GET /products/LOT-A-000001/verify` → `{verdict,risk_score,confidence,checks,proofs}`
5. `POST /anchor/run` every ~10 events → on-chain root
6. `GET /audit/integrity` → tamper check
7. `GET /products/:s/bundle` → download + `python tools/verify_bundle.py bundle.json`
8. `POST /products/:s/claim {"secret":"...","session_id":"..."}`
9. `POST /lots/:id/recall` → freeze + blast-radius
10. `POST /redteam/clone|invented|forge|handover|tamper` (REDTEAM_MODE=1, localhost only)

## APIs for UI person
- `GET /products/{serial}/verify` and `GET /anomaly/{serial}`: verdict object
- `GET /products/{serial}/bundle`: full evidence
- `GET /lots/{id}/blast-radius`: recall tree counts
- `POST /anchor/document {"doc_hash"}` with `X-API-KEY`: generic anchor for partner

## Convergence (CONTINUITY handoff platform, :8080)
- `GET /converge/health`: partner reachability + actor map
- `POST /converge/case {"serial"}`: push verdict+bundle as handoff dossier (queues to `converge_outbox.jsonl` if partner down, delivers when up)
- `GET /converge/status?serial=`: poll partner handoffs for this serial
- Actor map: `backend/converge_map.json` (`did:*` → `user-*`). Env: `PARTNER_BASE`, `PARTNER_USER`, `PARTNER_PROJECT`

## On-chain vs off-chain
ON: lot roots+qty, batch roots, freeze, revocation. OFF: events, sensors, secrets, claims, docs.
`contracts/ProvenanceRegistry.sol` is the same contract for Besu local and Amoy/Base Sepolia.

## Trust
Enrolment honest, >2/3 validators honest, clocks synced. VERIFIED = history consistent, not genuine. Contested ≠ fake. Consumers operator-attested, coarse location only.

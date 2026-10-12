# Convergence Audit — our exposed surface + bridge (2026-10-12)

Scope: the five endpoints we gave CONTINUITY (`verify`, `bundle`, `anchor`,
`blast-radius`, `anchor/document`) + our bridge (`/converge/*`, `convergence.py`)
+ doc-vs-reality. Their `:8080` was down throughout, so their side is queued
for a second half when they expose it (contract + black-box probing plan is in
chat history).

Method: static re-read of all six handlers + `convergence.py` + `chain.py`
calls they depend on, then live-fire against `:8000` and the public tunnel.

## Findings (all fixed unless noted)

| # | Sev | Finding | Evidence | Fix |
|---|---|---|---|---|
| C1 | **High (correctness)** | `/products/{serial}/anchor` returned the **wrong on-chain anchor id**. `anchor_id = batch_id - 1` assumed DB/chain lockstep; after any DB reset the chain keeps counting. Live: returned `1`, chain truth `19` (proof panel pointed at someone else's anchor root). | `GET /anchor` vs `MAX(batches.id)=2` vs chain `anchorCount=20` | `batches.aid` column stores the chain id at anchor time; endpoint returns it (`null` for pre-fix rows — honest, tx hash stays the truth). Backfilled row 2→19 (mapping verified by construction); row 1 stays NULL. |
| C2 | Med | Dispatch fallthrough: partner 4xx on `/incidents` → silently retried a **different** schema at `/handoffs` and could report `ok`; 200-with-non-JSON body → **double delivery** (exception at `r.json()` fell into the handoffs POST). | static (`dispatch_case`) | Fall back only on network error/5xx/429; 4xx → queue + `rejected:true` with partner reason; defensive `r.json()` parse. |
| C3 | Med | Unbounded `serial` on public GETs: 500-char serial → 200 + 500-char row banked in `unknown_attempts` (storage farming within throttle). `ConvergeIn.serial` unbounded → giant incident payloads to partner. | live: 500-char verify → `UNKNOWN`, row `length 500` | 128-char cap on verify/bundle/anchor (`422`) + `ConvergeIn` (`Field(max_length=128)`), matching `EventIn`. |
| C4 | Med | Unauthenticated `POST /anchor/run` (chain writes + batch churn) open on the public URL. Not in the partner's endpoint list — operator-only. | live: `{"anchored":10,"anchor_id":19}` unauthenticated | `require_local` (testclient + localhost + reset script unaffected). |
| C5 | Med | `POST /converge/case` unauthenticated + each dispatch holds the **global DB lock** through up to 16s of partner timeouts. Spam (or a hanging partner) wedges the whole API — matches the historical wedge symptom. | static (middleware + 8s×2 timeouts); connect-refused is fast today, hang is the trigger | DB-backed global throttle: 1 dispatch / 3s (`429`); `meta` table so it holds across workers. Residual: a hanging partner still stalls one lock-holder ~16s — accepted (partner is a known peer; timeouts stay 8s for tunnel latency). |
| C6 | Low | Partner-controlled `case_id` stored raw, interpolated into outbound URL path, re-served via API. | static (`converge_case`, `case_detail`) | `valid_case_id` allowlist `[A-Za-z0-9._-]{1,128}`, else `""`; `case_detail` refuses invalid ids. |
| C7 | Low | `bundle` leaked internal `chain.rpc` URL; `chain_info()` **raised** (→500) on Besu hiccups, failing partner reads. | live: `rpc: http://localhost:9545` in bundle | Dropped `rpc` (UI never consumed it); `safe_chain_info()` degrades with `reachable:false` on `/`, `/health`, `/bundle`, `/anchor`. |
| C8 | Low | `anchor/document` accepted unbounded `doc_hash` (large-body hashing); `case_status` substring-scanned the **entire** partner handoff list per request. | static | `doc_hash` ≤256 chars (`422`); substring fallback capped at first 300 items. |

## Verified secure (no change)

- `require_local` **holds through the public tunnel**: valid-shape `/signer/sign` and `/redteam/*` via `https://continuity.404piyush.me` → `403` (tunnel ingress does not present as 127.0.0.1). Spot-check, not proof for all time — re-verify after any tunnel/host reconfiguration.
- Verdict/bundle leak no secrets: `product{lot_id,status,custodian}` only; `secret_hash`/`claimed_session_hash` never leave the DB; event rows carry signatures (proofs, intended).
- Unknown-serial writes (verify/bundle GETs bank `unknown_attempts`) stay, throttled at 50/10min — GET-with-side-effect noted, accepted for R03 bookkeeping.
- Outbox capped at 1000 lines; `OUTBOX` path env-controlled (ours).
- CORS restricted (no `*`); `X-User-Id` static header over loopback/LAN only.
- Actor-map ingestion is exception-safe (garbage → static fallback); hostile entries are re-served verbatim via `/converge/health` — partner is semi-trusted, accepted.
- `anchor_id` off-by-one originally **correct by construction** (DB ids from 1, chain aids from 0) — the bug was staleness after reset, not arithmetic.

## Accepted risks / partner notes

- Unknown-serial throttle is a **shared global budget** (50/10min, `lot_id=""` bucket): their sim bulk-testing invented serials will see `429`s — by design, back off.
- `/converge/case` throttle: one dispatch per ~3s; double-clicks get `429`.
- Pre-fix batch rows report `anchor_id: null` — use `tx`; re-anchor for fresh ids.
- `total_units: 0` + nulls on unknown lots (`blast-radius`) — no `404`; their fan-out must handle it.
- Outbound `X-User-Id: user-yash` is a demo identifier, not a credential.

## Tests

`tests/test_converge_audit.py` (8): serial caps (4 endpoints), anchor aid
round-trip incl. on-chain root match, 4xx-no-fallback, non-JSON-no-double-delivery,
5xx-still-falls-back, case-id validation (+ endpoint stores `""`), dispatch
throttle. Suite: **26 green**.

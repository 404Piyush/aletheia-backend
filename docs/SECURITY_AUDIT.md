# Security audit — Trust Fabric (public-surface scope)

Date: 2026-10-12. Scope: all request paths reachable via the public tunnel
(`https://continuity.404piyush.me` → `:8000`), chain adapter fail-open paths,
repo secrets, UI reflection points. Method: static pass of every handler +
live-fire exploits (all confirmed before fixing). Primitives (Ed25519, SHA256,
QBFT) and Besu/Cloudflare internals out of scope.

## Findings

`Exploit` = confirmed working from an unauthenticated remote call pre-fix.
`Fix` = commit in this repo. `Test` = regression test.

### Critical

| ID | Finding | Exploit | Fix | Test |
|---|---|---|---|---|
| C1 | `/redteam/*` gated by env var only; `REDTEAM_MODE=1` in the live run **and** docker-compose → tamper/clone/forge reachable through the public tunnel | yes (localhost; same app serves tunnel) | `require_local` localhost gate **in addition** to env flag | `test_require_local_gate` |
| C2 | `/seed/demo`, `/seed/tree` reset all demo state, no auth | yes | same localhost gate | same |
| C3 | `besu-qbft/validator.key` committed to git (single-validator chain key) | repo read = chain control | `git rm --cached` + `.gitignore`; key stays on disk for the running node | — (history still contains it; dev-only key, rotation = new genesis, see accepted risks) |

### High

| ID | Finding | Exploit | Fix | Test |
|---|---|---|---|---|
| H1 | `POST /signer/sign` signed arbitrary payloads for any DID — a public signature oracle | yes, standalone signature verified with `verify_event` | localhost-only | gate unit test |
| H2 | `is_frozen` returned `False` on any RPC error → recalled lots become writable when Besu flakes (fail-open) | code path confirmed | `ChainUnavailable` raised; ingest answers `503`, writes pause | `test_freeze_fail_closed` |
| H3 | `freeze_lot` swallowed chain errors; recall still marked the DB frozen → chain open / DB blocked divergence, reported as success | code path confirmed | errors propagate; recall answers `502` with DB untouched | same |
| H4 | DID string = identity: auto-sign gives a valid device signature to anyone who names a registered DID | by design (prototype) | **accepted risk** (see below); R08 still flags truly-unsigned history at verify time | — |
| H5 | Sync chain waits (`wait_for_transaction_receipt` 60s) inside single-worker handlers while holding the global DB lock — root cause of the observed full-hang wedge | observed twice live | receipt timeout 60→20s; full async refactor deferred, not silently dropped | — |

### Medium

| ID | Finding | Exploit | Fix | Test |
|---|---|---|---|---|
| M1 | Empty `session_id` claims hash identically → second "buyer" gets idempotent `CLAIMED`, bypassing R12 contest | yes, live-fired | `session_id` required non-empty (≤128), secret ≤256 → `422` | `test_claim_empty_session_rejected` |
| M2 | No event validation: `ts` year 2286, `lat` 9999, unknown types all accepted (timeline/coordinate poisoning) | yes, `200` on absurd input | type whitelist (`MANUFACTURED/HANDOVER_*/SOLD/SCAN`), `ts ≤ now+5min`, lat/lon ranges, length caps → `422` | `test_event_input_validation` |
| M3 | Re-`POST /lots` with same id + different size silently kept the old on-chain root while storing a new DB root (R03 breakage) | code path confirmed | on-chain root comparison → `409` on mismatch | `test_lot_reregister_root_mismatch` |
| M4 | `unknown_attempts` row per invented serial, unbounded (storage-DoS via serial spam) | rows accumulate, confirmed | `429` past 50 attempts/lot/10min on sign + verify paths | `test_unknown_serial_flood_throttled` |
| M5 | `POST /anchor/document` wrote to chain with no auth and held the global lock during the receipt wait | code path confirmed | localhost-only, or `X-API-KEY` when `ANCHOR_API_KEY` is set | gate unit test |
| M6 | CORS `*` → any website could POST events from a visitor's browser | header confirmed (`*`) | restricted default origins (3000/3001/8000 + public URL), env override | `test_cors_restricted_by_default` |

### Low

| ID | Finding | Fix |
|---|---|---|
| L1 | Unpinned `requirements.txt` (supply chain drift) | versions pinned from the running env |
| L2 | Partner case match by substring (`SHOW-000001` matches `SHOW-0000010`) | exact `serial` match first, substring fallback |
| L3 | `converge_outbox.jsonl` append-only, unbounded | capped at last 1000 lines |
| L4 | UI `hash.html` interpolates values into an Alpine `x-text` attribute | no change: Jinja autoescape neutralizes it, values are hex; noted fragile pattern |
| L5 | Dead code (`sha256(...) if False` in recall), confusing mimic `is_active` path | recall cleaned; mimic path documented as inert under `USE_CHAIN` |

## Accepted risks (judge-visible, not holes)

- Single-validator QBFT: one key controls the demo chain. Fine for a prototype, but the stated trust assumption (">2/3 validators honest") does not hold with one validator — a multi-validator set is later work.
- Device-simulator keys (`SIGNER_MASTER` default): anyone with the master derives all actor keys. Documented in `signer.py`; production = secure-element wallets.
- DID-string identity (H4): venue-visible DIDs are impersonable. Mitigation path recorded: require client-supplied signatures for custody events, server auto-sign becomes fallback.
- `validator.key` remains in git history (C3): dev-only key; rotation requires a new genesis + redeploy (documented, not done — would invalidate the running chain mid-event).
- Consumer scans are operator-attested; locations coarse. Per the honest trust model in README.

## Retest

- Full suite: **18 passed** (`tests/test_security.py`: 7 new).
- Live-fire exploits re-run post-fix: oracle gated, absurd input → 422, empty session → 422, flood → 429, evil-origin CORS header absent.
- Live server restarted on the fixed code; demo state verified unchanged (SHOW/LOT-DEMO-A/LOT-TREE intact, none frozen).

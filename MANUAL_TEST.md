# Manual Test Guide — Trust Fabric backend

Base: `http://localhost:8000` or `https://continuity.404piyush.me`.
UI mirror: `http://localhost:3001/verify/<serial>`. All commands assume the demo
seed is loaded (`POST /seed/demo` + `SHOW` lot — see `scripts/reset_demo.sh`
for a full reset).

## 1. Health + chain

```bash
curl localhost:8000/health
# -> {"status":"ok","events":N,"open_incidents":M,"chain":{"mode":"besu-qbft",
#     "chain_id":1338,"block":<rising>,"contract":"0xEf89…"}}
```

Pass = `status ok`, block height rises between calls.

## 2. Legit journey → VERIFIED

```bash
curl -X POST localhost:8000/events/sign -H 'Content-Type: application/json' -d '{
  "serial":"SHOW-000001","type":"HANDOVER_OFFER","actor":"did:maker1",
  "to_actor":"did:transporter1","lat":19.07,"lon":72.87}'
curl localhost:8000/products/SHOW-000001/verify
# -> VERIFIED, risk 0. Server auto-signed via device simulator (no signature sent).
```

## 3. Forged signature rejected

```bash
curl -X POST localhost:8000/events/sign -H 'Content-Type: application/json' -d '{
  "serial":"SHOW-000001","type":"SCAN","actor":"did:transporter1",
  "signature":"0xffffff","signer":"0xeeee"}'
# -> 401 "bad signature for signer"
# One-click: curl -X POST localhost:8000/redteam/forge
```

## 4. Signer service

```bash
curl localhost:8000/signer/pubkey/did:maker1
# -> {"did":"did:maker1","pubkey":"0x…"} (stable across restarts)
```

## 5. Clone attack → CLONE_DETECTED

```bash
curl -X POST localhost:8000/redteam/clone
curl localhost:8000/products/LOT-DEMO-A-000001/verify
# -> CLONE_DETECTED 65 high, failed checks R04 (impossible travel) + R07 (custody)
```

## 6. Incidents feed

```bash
curl localhost:8000/incidents
curl localhost:8000/incidents/LOT-DEMO-A-000001
# -> rows with severity/status/findings; detail interleaves event timeline.
# Re-verify does not duplicate rows. Unknown serial -> 404.
```

## 7. Replay rejected

Send the identical SCAN twice (same `ts`):
```bash
# first -> 200, second -> 409 "duplicate event (replay rejected)"
```

## 8. Consumer claim lifecycle

Lot `CLAIM` (secret from lot-creation response, `sample_secrets`):

```bash
curl -X POST localhost:8000/events/sign -H 'Content-Type: application/json' -d '{
  "serial":"CLAIM-000001","type":"SOLD","actor":"did:retailer1"}'
curl -X POST localhost:8000/products/CLAIM-000001/claim \
  -H 'Content-Type: application/json' -d '{"secret":"<hex>","session_id":"buyer-1"}'
# -> CLAIMED. Same session again -> idempotent CLAIMED.
# Different session -> R12 contested. 6th wrong secret in 10 min -> 429.
```

## 9. Tamper + recall

```bash
curl -X POST localhost:8000/redteam/tamper
curl localhost:8000/audit/integrity
# -> ok:false with the tampered event hash listed
curl -X POST localhost:8000/lots/SHOW/recall
# -> on-chain freeze; further writes to SHOW lots -> 423
curl localhost:8000/lots/SHOW/blast-radius
# -> counts by status/custodian + invented_attempts/overflow flags
```

## 10. Proof without trusting the backend

```bash
curl localhost:8000/products/SHOW-000001/bundle -o bundle.json
python tools/verify_bundle.py bundle.json
# -> ALL PASS (checks hashes, chain links, Merkle proof vs live Besu RPC)
```

## 11. Convergence (partner)

```bash
curl localhost:8000/converge/health
# -> partner_up, bridge status, actor_map_source (static until :8080 answers)
curl -X POST localhost:8000/converge/case \
  -H 'Content-Type: application/json' -d '{"serial":"LOT-DEMO-A-000001"}'
# -> CLONE_DETECTED payload delivered, or queued:true with reason
```

## 12. Full suite

```bash
CHAIN_RPC=http://localhost:9545 python3 -m pytest tests/ -q
# expect: 10 passed
```

## Failure dictionary

| Symptom | Meaning | Fix |
|---|---|---|
| `404 unknown serial` | serial not registered | check lot exists / spelling |
| `423 lot frozen` | lot recalled | expected after recall demo |
| `409 duplicate` | exact replay | expected (replay protection) |
| `401 bad signature` | forged signer | expected (forgery rejected) |
| `429` on claim | brute-force throttle | wait out the 10-min window |
| `500 database is locked` | burst overload (patched: WAL + serialized access) | report with timestamp from server log |
| requests hang >30s | event-loop wedge (under watch) | restart backend, report immediately |

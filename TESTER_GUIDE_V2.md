# Trust Fabric — Tester Guide V2 (start to end)

Supersedes the endpoint-level MANUAL_TEST.md sections where they overlap
(lots endpoints, red-team buttons, wedge/503 behavior, convergence C1–C3,
UI audit fixes). MANUAL_TEST.md §1–10 keep their curl-level detail; this
guide is the ordered pass with expected results and done-status.

## 0. Start everything (order matters)

```bash
# 1. Besu QBFT (chain 1338) — must be first, backend waits on it
docker start tf-besu  # or: cd ~/Desktop/trust-fabric && docker compose up -d besu
curl -s -X POST http://127.0.0.1:9545 -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","method":"eth_blockNumber","params":[],"id":1}'
# -> {"jsonrpc":"2.0",...,"result":"0x..."} (any rising hex = alive)

# 2. Backend :8000 (with partner env; without PARTNER_BASE, convergence queues)
cd ~/Desktop/trust-fabric
PARTNER_BASE=https://converge.yugc.in PARTNER_USER=user-yash PARTNER_PROJECT=apollo-core \
PUBLIC_BASE=https://continuity.404piyush.me REDTEAM_MODE=1 \
python3 -m uvicorn backend.main:app --port 8000 --timeout-keep-alive 5 --limit-concurrency 100 &
curl -s http://127.0.0.1:8000/health | python3 -m json.tool
# -> status ok, events count, chain{mode besu-qbft, chain_id 1338, block rising}

# 3. UI :3001 (chain mode! default is mock — the #1 gotcha)
cd ~/Desktop/trusttrace
TRUSTTRACE_API_MODE=chain uv run uvicorn app.main:app --port 3001 --no-server-header &
curl -s http://127.0.0.1:3001/verify/SHOW-000001 -o /dev/null -w "%{http_code}\n"  # -> 200 = chain mode
```

Automated suites (run first, ~2 min): `python3 -m pytest tests/ -q` in
trust-fabric → 70 passed (15 rules, 9 redteam, 7 security, 5 converge,
5 lifecycle, 4 C2, 3 lots-api, 3 identity, 3 evidence, 2 claims, 2 crypto,
1 blast, 1 flow, 1 incidents, 1 signing, 8 converge-audit… total 70).
In trusttrace `uv run pytest -q` → 95+ passed.

## 1. Core detection story (the main thing)

| # | Command | Expect |
|---|---|---|
| 1.1 | `GET /products/SHOW-000001/verify` | `VERIFIED`, risk 0, checks all pass |
| 1.2 | `GET /products/SHOW-000001/bundle` | events hash-chained, `lot_proof.root`, Besu anchor |
| 1.3 | `POST /redteam/clone` (localhost only) then `GET /products/<first-unit>/verify` | `CLONE_DETECTED`, risk **65** = 40 (R04 critical) + 25 (R07 high); failed checks name 1148 km/0.33h + `did:attacker` offer |
| 1.4 | `GET /products/LOT-DEMO-A-000001/verify` | `CLONE_DETECTED` 65 (the canonical demo clone) |
| 1.5 | `POST /redteam/forge` | `{"rejected":…, "rule":"R08/R09"}` — wrong-key signature never enters history; unit verdict unchanged afterward |
| 1.6 | `POST /redteam/invented` | 5 fake serials rejected + counted in `unknown_attempts` (51st+ in 10 min → `429`) |
| 1.7 | `POST /redteam/tamper` then `GET /audit/integrity` | tamper detected, integrity endpoint flags it |
| 1.8 | Replay any `POST /events/sign` twice with same body | second → `409` (replay rejected) |
| 1.9 | Oversize serial (5000 chars in `GET /products/{s}/verify`) | `422`, never 500 |

## 2. Claims, recall, blast tree

| # | Command | Expect |
|---|---|---|
| 2.1 | SELL then CLAIM a unit (`POST /products/{s}/claim` with correct secret from lot creation) | `SOLD` → `CLAIMED` |
| 2.2 | Second claim, different session | `CONTESTED` (R12 double-claim) |
| 2.3 | Empty session claim | `422` |
| 2.4 | `GET /lots/LOT-TREE/blast-radius` | 2 pallets → 3 cases → 8 units, mixed custodians, `claimed_units: 1` |
| 2.5 | `POST /lots/LOT-TREE/recall` | 7 `FROZEN`, 1 `CLAIMED` survives (recall never takes sold-claimed property) |
| 2.6 | Write to a frozen unit | `423` (frozen lot blocks writes) |
| 2.7 | `GET /lots` → `GET /lots/SHOW` | inventory with live counts; detail with units + custodians + root/tx |

## 3. Convergence (needs their box — if 530, only 3.5/3.6 apply)

| # | Command | Expect |
|---|---|---|
| 3.1 | `GET /converge/health` | `partner_up`, live actor map (`source: live`), bridge body (~20–40s when they're slow — normal) |
| 3.2 | `GET /converge/actor-map` | 5 DIDs → userIds |
| 3.3 | `POST /converge/case {"serial":"LOT-DEMO-A-000001"}` | **fire once only** → `handoffId` (HO-…), `anchorTx`, recall tasks |
| 3.4 | `GET /converge/status?serial=LOT-DEMO-A-000001` | stored `case_id`/`handoff_id`/`incident_id` + live partner match |
| 3.5 | `POST /converge/case {"serial":"SHOW-000001"}` | `422` (VERIFIED refused); add `"force": true` → dispatches |
| 3.6 | Their box down | dispatches queue to `converge_outbox.jsonl`, API stays 200-shape (`ok:false, queued:true`) |

Reference drill (2026-10-12): `HO-2026-569` / `INC-LOT-DEMO-A-000001`,
anchor `0xb1b47e55…` (`ANC-BESU-1310542377`, block `0x1ce829`).

## 4. UI (:3001, chain mode)

Mock fixtures always work (`TT-DEMO-0001/0002`, `LOT-DEMO-A/B` detail
pages). Live paths: `/verify/SHOW-000001` (VERIFIED + LIVE bundle notice),
`/verify/LOT-DEMO-A-000001` (clone), `/lots` (live inventory),
`/lots/LOT-TREE/blast-radius` (pallet tree). Security spot-checks: search
`'};alert(1);//` → renders inert; 5000-char serial → 404 page (not 500);
`/red-team` → 403 in chain mode; freeze/recall POSTs require typing the lot ID.

## 5. Failure dictionary (all verified live during audits)

`422` bad input / oversize / VERIFIED-dispatch / unknown-attack ·
`404` unknown serial/lot · `409` replay / root-mismatch ·
`423` frozen write · `429` claim-burst / unknown-flood / dispatch-throttle ·
`502` error page (never all-clear, never 500) ·
`503` "server busy" shed under slow-partner load (recovers alone) ·
`403` localhost-only via tunnel ·
`/debug/threads` localhost stack dump if anything ever wedges again.

## 8. RBAC (demo authz)

Reads open to all. Writes need `X-Role: operator` (default role verifier =
read-only). Gated: `POST /lots/*/recall`, `POST /converge/case`,
`POST /anchor/*`, `POST /*/claim`. Every decision logged (`RBAC …` in server
log). Panic button: `RBAC_OPEN=1` env opens all writes (logged).

| # | Command | Expect |
|---|---|---|
| 8.1 | Recall without header | `403` role is read-only |
| 8.2 | Same with `-H 'X-Role: operator'` | executes (⚠️ real side effect — use throwaways) |
| 8.3 | UI: topbar Role select → Operator (sets `tt_role` cookie) | freeze/recall forms appear; verifier sees locked note |
| 8.4 | UI verifier POST freeze | `403` page "Operator role required" |

## 7. Sweep log 2026-10-13 (all rows pass)

§1: VERIFIED 0 / bundle 3 events / clone 65 / forge 401 R08/R09 / replay
409 / oversize 422. §2 (throwaways CLAIM-T2/T3): SOLD→CLAIMED→idempotent→
CONTESTED(R12)→empty-session 422; recall freezes; frozen write 423. Note:
recall freezes CONTESTED units (only CLAIMED is spared — correct, contested
is not owned property). TREE intact 6/1/1, SHOW VERIFIED. §4 UI: all pages
200, red-team 403 in chain mode, XSS inert, LIVE bundle notice correct.
Destructive rows (clone/tamper/invented on live units, recall of demo lots)
verified via automated suite instead — live demo state preserved.

Environment lesson: this laptop swaps under load (29MB free observed) —
Docker + IDEs + test runs starve the servers into 503 cascades. For judging:
quiet machine, serial (not parallel) probing, and the 503-shed + restart
runbook in §5. Lot creation takes ~35s when Besu is CPU-starved (normally ~2s).

## 6. How much is done (honest, 2026-10-13)

Done: rules engine R01–R14 + scoring + 15 pinned tests; Ed25519 device
signing; Merkle + Besu anchoring; claims lifecycle; recall + blast tree;
lots API; red-team console (2/7 buttons test-pinned, rest manual);
convergence C1–C3 + live drill `HO-2026-569`; 3 security audits fixed; UI
verify+lots live + XSS/500 fixes; wedge root-caused + fixed; cold-restart script.
Partially: red-team buttons 5/7 test-only-manual; UI Incidents/Explorer
still mock-only.
Blocked: C4 joint gate walkthrough (partner box 530), their-side audit
(passive only).
Deliberately skipped: Amoy checkpoint, NFTs, ST-GNN.

# Connect to Trust Fabric — partner setup guide

For the CONTINUITY team. Three ways to reach our API, ranked. Pick **A**.

## Option A — Run our stack locally (recommended)

No network dependency, identical data, zero laptop-uptime risk.

```bash
git clone https://github.com/404Piyush/trust-fabric.git
cd trust-fabric
docker compose up --build
```

This starts Besu QBFT (chain 1338) + our API. Then seed the shared demo data:

```bash
curl -X POST localhost:8000/seed/demo
curl -X POST localhost:8000/lots -H 'Content-Type: application/json' \
  -d '{"lot_id":"SHOW","size":5,"manufacturer":"did:maker1"}'
```

Your base URL becomes `http://localhost:8000`. Done — skip to Endpoints.

## Option B — Tailscale (live cross-laptop calls)

1. Both install Tailscale (free), log into the same tailnet.
2. We run `tailscale serve` / open port 8000; you call `http://<our-tail-ip>:8000`.
3. Stable IPs, encrypted, works across any venue/hotel Wi-Fi. No expiry.

Ask us for our tailnet IP on the day.

## Option C — Public tunnel (last resort, not ngrok)

Cloudflare Tunnel (`cloudflared tunnel --url http://localhost:8000`) over ngrok:
free, stable subdomain for the day, no account expiry mid-demo. We will
paste the URL in chat if needed. Downside: needs our laptop online.

## Base URLs

| Service | Local (Option A) | Public (live now) |
|---|---|---|
| API + Swagger `/docs` | `http://localhost:8000` | **`https://continuity.404piyush.me`** (SSL, Cloudflare Tunnel) |
| Health | `GET /` and `GET /converge/health` | `https://continuity.404piyush.me/` |
| Besu QBFT RPC (chain 1338) | `http://localhost:9545` | private (proofs served via API with tx hashes) |

> Use the public URL — no setup needed on your side.

Contract: redeployed on every cold restart — read the live address from `GET /`
(`chain.contract`). Chain 1338, Besu QBFT on `:9545`, registrar stable
`0x1848d8Ded316cA9c0F4F9Cc553d1E64f4d66e32F`.

## Endpoints you call

All JSON. CORS restricted to our frontends + public URL (see `CORS_ORIGINS` env).
No auth on read-only demo endpoints. State-changing and debug endpoints
(`/seed/*`, `/redteam/*`, `/signer/sign`, `/anchor/document` without
`ANCHOR_API_KEY`) are localhost-only.

```bash
BASE=https://continuity.404piyush.me   # live public API (or http://localhost:8000 via Option A)

# 1. Verdict for a serial (drives your case creation)
curl $BASE/products/SHOW-000001/verify
# -> {verdict: VERIFIED|SUSPICIOUS|CLONE_DETECTED|TAMPERED|UNKNOWN,
#     risk_score, confidence, checks[{id,status,reason,evidence_refs,next_step}],
#     product{lot_id,status,custodian}, next_steps}

# 2. Full evidence bundle (attach to your handoff dossier)
curl $BASE/products/SHOW-000001/bundle
# -> {events[], lot_proof{root, proof}, chain{mode, contract, block}, verdict}

# 3. Proof panel data (anchor id + tx + lot root + chain head)
curl $BASE/products/SHOW-000001/anchor
# -> {anchored, anchor_id, root, tx, block, lot, chain}

# 4. Recall scope (fan out your per-retailer tasks from this)
curl $BASE/lots/SHOW/blast-radius
# -> {total_units, by_status, by_custodian, invented_attempts, overflow,
#     claimed_units, tree:[{pallet_id, cases:[{case_id, units:[{serial,status,custodian}]}]}]}
# Richest example: /lots/LOT-TREE/blast-radius (2 pallets, 3 cases, 8 units,
# one SOLD, one CLAIMED — CLAIMED units survive recall)

# 5. Anchor YOUR dossier hash on our chain (put returned tx in your audit log)
# Needs ANCHOR_API_KEY (ask us) or call from localhost:
curl -X POST $BASE/anchor/document -H 'Content-Type: application/json' \
  -H 'X-API-KEY: <key>' -d '{"doc_hash":"<sha256 of your handoff package>"}'
# -> {anchor_id, tx}
```

Demo serials that always exist after seeding: `SHOW-000001` (clean journey),
`SHOW-000002` (sold), `LOT-DEMO-A-000001` (run `POST /redteam/clone` first to make it `CLONE_DETECTED`),
`LOT-TREE-000003` (sold + claimed), `LOT-TREE-000004` (sold, unclaimed).

## Actor map (confirm or edit)

| Our actor | Your user |
|---|---|
| `did:maker1` | `user-yash` |
| `did:transporter1` | `user-shruti` |
| `did:retailer1` | `user-jessica` |

## What we call on you (when your :8080 is up)

- `POST /api/convergence/incidents` with `{serial, lotId, verdict, riskScore,
  confidence, failedChecks[], evidenceBundleUrl}` + `X-User-Id`
  (falls back to `POST /api/handoffs`)
- `GET /api/convergence/actor-map` — we sync your live DID roster
  (see `GET /converge/actor-map`, `source: live|static`)
- `GET /api/convergence/status` — surfaced in our `GET /converge/health`
- `GET /api/convergence/cases/{id}` — tracked per serial after dispatch
  (see `GET /converge/status?serial=`)

Send us: your base URL, one `projectId` (`apollo-core` assumed), and confirm the actor map.

## Troubleshooting

- `connection refused` → is `docker compose up` running? `curl localhost:8000/` should return `{"service":"trust-fabric",...}`.
- Wrong verdicts → re-run the two seed commands above (chain is persistent, DB is not; registration is idempotent).
- Chain check → `GET /` shows `chain:{mode:besu-qbft, chain_id:1338, block:<rising>, contract:0x…}` (address changes per redeploy — never hardcode it).
- Ports used: 8000 (API), 9545 (Besu). The `:8545` Besu on this machine belongs to another project — do not touch it.

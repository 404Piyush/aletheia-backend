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

| Service | Local (A) | Live (B/C) |
|---|---|---|
| API + Swagger `/docs` | `http://localhost:8000` | `<shared-base-url>` |
| Health | `GET /` and `GET /converge/health` | same |
| Besu QBFT RPC (chain 1338) | `http://localhost:9545` | same host, port 9545 |

Contract: `0xEf89440626EC6FCA59dfb164f42D027BfeC7c881` (lot roots, batch anchors, freeze flags).

## Endpoints you call

All JSON. CORS open. No auth on demo endpoints (`X-API-KEY` accepted, not enforced).

```bash
BASE=http://localhost:8000   # or the shared base URL

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
# -> {total_units, by_status, by_custodian, invented_attempts, overflow}

# 5. Anchor YOUR dossier hash on our chain (put returned tx in your audit log)
curl -X POST $BASE/anchor/document -H 'Content-Type: application/json' \
  -d '{"doc_hash":"<sha256 of your handoff package>"}'
# -> {anchor_id, tx}
```

Demo serials that always exist after seeding: `SHOW-000001` (clean journey),
`SHOW-000002` (sold), `LOT-DEMO-A-000001` (run `POST /redteam/clone` first to make it `CLONE_DETECTED`).

## Actor map (confirm or edit)

| Our actor | Your user |
|---|---|
| `did:maker1` | `user-yash` |
| `did:transporter1` | `user-shruti` |
| `did:retailer1` | `user-jessica` |

## What we call on you (when your :8080 is up)

- `POST /api/handoffs` with `{projectId, recipientId, note, executiveSummary, nextSteps, criticalBlockers}` + `X-User-Id`
- `GET /api/handoffs` to poll owner/ack state

Send us: your base URL, one `projectId`, and confirm the actor map.

## Troubleshooting

- `connection refused` → is `docker compose up` running? `curl localhost:8000/` should return `{"service":"trust-fabric",...}`.
- Wrong verdicts → re-run the two seed commands above (chain is persistent, DB is not; registration is idempotent).
- Chain check → `GET /` shows `chain:{mode:besu-qbft, chain_id:1338, block:<rising>, contract:0xEf89…}`.
- Ports used: 8000 (API), 9545 (Besu). The `:8545` Besu on this machine belongs to another project — do not touch it.

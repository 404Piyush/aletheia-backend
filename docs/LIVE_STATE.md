# Live demo state

Snapshot of what's loaded right now (regenerate with `scripts/reset_demo.sh`).
Nothing here is secret — all data synthetic. Secrets below are demo-only.

## Lots (none frozen)

| Lot | Units | Notable serials |
|---|---|---|
| `SHOW` (5) | 4 ACTIVE, 1 SOLD | `SHOW-000001` VERIFIED journey (maker→transporter) · `SHOW-000002` SOLD |
| `LOT-DEMO-A` (10) | 10 ACTIVE | `LOT-DEMO-A-000001` → `CLONE_DETECTED` 65 after `POST /redteam/clone` (R04+R07) |
| `LOT-TREE` (8) | 6 ACTIVE, 1 SOLD, 1 CLAIMED | P-T1 → C-T1 (000001/2 transporter, 000003 CLAIMED) + C-T2 (000004 SOLD, 000005 maker) · P-T2 → C-T3 (000006–8 retailer) |

## Claim demo

`LOT-TREE-000003` is already CLAIMED (session `buyer1-session`); re-claim with
that session → idempotent `CLAIMED`, any other session → R12 `contested`.
For a from-scratch claim run, create a `CLAIM` lot per `MANUAL_TEST.md` §8
(secrets are one-way hashed — always capture `sample_secrets` at creation).

## Chain

Besu QBFT, chain 1338, RPC `:9545`. Contract address changes on redeploy —
read live from `GET /`. Registrar key stable.

## Don't

- Recall `SHOW` mid-demo unless the freeze is the point (writes → 423 after).
- Re-`POST /lots` an existing lot id — it resets that lot's units + secrets.
- Point anything at `:8545` — wrong chain (another project).

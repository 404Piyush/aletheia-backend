# 3-Laptop Node Setup (hotspot LAN)

One hotspot (Android preferred — iPhones may isolate clients). Test first:
from laptop 2, `ping <laptop1-ip>` and `curl <laptop1-ip>:8000/health`.
All ports below are fixed; only IPs change (`hostname -I` on the day).

## Roles

| Laptop | Besu | Backend DB | Serves |
|---|---|---|---|
| 1 (manufacturer) | validator 1 (existing) | own DB | API :8000, tunnel (public URL lives here — keep awake) |
| 2 (distributor) | validator 2 (new key) | own DB | API :8000 (LAN) |
| 3 (public) | validator 3 (new key) | own DB | API :8000 + UI :3001 (LAN) |

## 0. Prep (done on laptop 1)

```bash
python3 tools/mk_validators.py   # prints validator addresses + enode URLs
# copy to laptops 2/3 (USB/stick, never chat):
#   besu-qbft/genesis.json, besu-qbft/validator{N}.key, this repo
```

## 1. Besu on laptops 2/3 (same chain, no redeploy)

```bash
docker run -d --name tf-besu -p 9545:8545 \
  -v $PWD/besu-qbft/genesis.json:/config/genesis.json:ro \
  -v $PWD/besu-qbft/validatorN.key:/config/validator.key:ro \
  -v tf-besu-data:/var/lib/besu hyperledger/besu:24.12.0 \
  --genesis-file=/config/genesis.json \
  --node-private-key-file=/config/validator.key \
  --data-path=/var/lib/besu \
  --bootnodes=enode://<LAPTOP1-PUBKEY>@<LAPTOP1-IP>:30303 \
  --rpc-http-enabled --rpc-http-host=0.0.0.0 --rpc-http-port=8545 \
  --rpc-http-api=ETH,NET,WEB3,QBFT --host-allowlist='*'
# open UDP+TCP 30303 between laptops if peer count stays 0
```

Laptop 1's enode pubkey: from `besu-qbft/validator.key` via
`tools/mk_validators.py` output (run it with the existing key).

## 2. Vote the validators in (keeps chain + contract — no redeploy)

On laptop 1 (existing sole validator, its own vote is majority of 1):

```bash
curl -X POST http://127.0.0.1:9545 -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","method":"qbft_proposeValidatorVote","params":["0x<VALIDATOR2>",true],"id":1}'
```

Then laptop 2 is a validator. For laptop 3, **both** 1 and 2 must vote yes
(majority of 2). Verify: `qbft_getValidatorsByBlockNumber` `["latest"]`
should list all three.

## 3. Backends (each laptop, own DB)

```bash
PARTNER_BASE=https://converge.yugc.in PARTNER_USER=user-yash \
PARTNER_PROJECT=apollo-core PUBLIC_BASE=https://continuity.404piyush.me \
REDTEAM_MODE=1 ACTOR_PASSWORDS="maker:..,distributor:..,retailer:.." \
python3 -m uvicorn backend.main:app --port 8000 \
  --timeout-keep-alive 5 --limit-concurrency 100 &
```

Each backend seeds its own DB (`POST /seed/demo` on localhost per laptop —
registers actors + demo lot on the SHARED chain, idempotent). Contract
address is chain-global: copy `contracts/deployed.json` from laptop 1
(or read it from `GET /` on laptop 1).

## 4. Act page nodes + tunnel

- Tunnel stays on laptop 1: all phones (actors + consumers) use
  `https://continuity.404piyush.me/act` (https = camera works; LAN http
  would block getUserMedia on phones).
- In `/act` → nodes field, enter all three LAN backends:
  `http://<IP1>:8000, http://<IP2>:8000, http://<IP3>:8000`
  Every action posts to all three; each node verifies independently.
- Logins per phone: maker / distributor / retailer (+ passwords from step 3).
  Consumers: no login.

## 5. Kill test (the 10-second wow)

`docker stop tf-besu` on laptop 2 mid-demo → chain keeps producing (QBFT
tolerates 1/3 down) → act page still green on all... except node 2's posts
fail openly per-node (red ✗ on node 2, green ✓ on 1+3). Restart to rejoin.

## If the hotspot isolates clients

Fall back: laptop 1 serves everything (tunnel URL for all phones), laptops
2–3 run Besu-only as validators (still 3 living chain nodes, still the
kill test). The act page posts to one backend.

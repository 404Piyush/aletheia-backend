#!/bin/bash
# Cold restart: Besu (persistent volume) + redeploy + fresh DB + demo seed.
# Safe to re-run. Run from trust-fabric/.
set -e
cd "$(dirname "$0")/.."

echo "== Besu (persistent volume) =="
docker rm -f tf-besu >/dev/null 2>&1 || true
docker volume create tf-besu-data >/dev/null 2>&1 || true
docker run -d --name tf-besu -p 9545:8545 \
  -v "$PWD/besu-qbft/genesis.json:/config/genesis.json:ro" \
  -v "$PWD/besu-qbft/validator.key:/config/validator.key:ro" \
  -v tf-besu-data:/var/lib/besu \
  hyperledger/besu:24.12.0 \
  --genesis-file=/config/genesis.json \
  --node-private-key-file=/config/validator.key \
  --data-path=/var/lib/besu \
  --rpc-http-enabled --rpc-http-host=0.0.0.0 --rpc-http-port=8545 \
  --rpc-http-api=ETH,NET,WEB3 --host-allowlist='*' >/dev/null
for i in $(seq 1 30); do
  BLOCK=$(curl -s -m 3 -X POST localhost:9545 -H 'Content-Type: application/json' \
    -d '{"jsonrpc":"2.0","method":"eth_blockNumber","params":[],"id":1}' | grep -o '"result":"0x[0-9a-f]*"' || true)
  [ -n "$BLOCK" ] && break
  sleep 2
done
echo "besu: $BLOCK"

echo "== redeploy contract =="
python3 - <<'EOF'
import json
from web3 import Web3
w3 = Web3(Web3.HTTPProvider("http://localhost:9545"))
key = open("besu-qbft/validator.key").read().strip()
acct = w3.eth.account.from_key(key)
abi = json.load(open("contracts/registry_abi.json"))
bytecode = open("contracts/registry.bin").read().strip()
C = w3.eth.contract(abi=abi, bytecode=bytecode)
tx = C.constructor().build_transaction({"from": acct.address, "nonce": w3.eth.get_transaction_count(acct.address), "gas": 4000000, "gasPrice": 0})
h = w3.eth.send_raw_transaction(acct.sign_transaction(tx).raw_transaction)
rc = w3.eth.wait_for_transaction_receipt(h, timeout=60)
assert rc.status == 1, "deploy reverted"
json.dump({"address": rc.contractAddress, "deploy_tx": h.hex(), "rpc": "http://localhost:9545", "chain_id": 1338}, open("contracts/deployed.json", "w"))
print("contract:", rc.contractAddress)
EOF

echo "== backend =="
lsof -ti:8000 | xargs kill -9 2>/dev/null || true
sleep 1
rm -f trust.db-shm trust.db-wal trust.db anchors.json converge_outbox.jsonl
CHAIN_RPC=http://localhost:9545 REDTEAM_MODE=1 DB_PATH=trust.db ANCHOR_STORE=anchors.json \
  nohup python3 -m uvicorn backend.main:app --port 8000 >/tmp/tf.log 2>&1 &
sleep 5

echo "== seed + demo lots =="
curl -s -m 20 -X POST localhost:8000/seed/demo; echo
curl -s -m 30 -X POST localhost:8000/lots -H 'Content-Type: application/json' \
  -d '{"lot_id":"SHOW","size":5,"manufacturer":"did:maker1"}' | python3 -c "import json,sys; d=json.load(sys.stdin); print('SHOW tx:', d['tx'])"
curl -s -m 20 -X POST localhost:8000/events/sign -H 'Content-Type: application/json' \
  -d '{"serial":"SHOW-000001","type":"HANDOVER_OFFER","actor":"did:maker1","to_actor":"did:transporter1","lat":19.07,"lon":72.87}' >/dev/null
curl -s -m 20 -X POST localhost:8000/events/sign -H 'Content-Type: application/json' \
  -d '{"serial":"SHOW-000001","type":"HANDOVER_ACCEPT","actor":"did:transporter1","lat":19.10,"lon":72.90}' >/dev/null
curl -s -m 20 -X POST localhost:8000/events/sign -H 'Content-Type: application/json' \
  -d '{"serial":"SHOW-000002","type":"SOLD","actor":"did:retailer1"}' >/dev/null
curl -s -m 60 -X POST localhost:8000/seed/tree | python3 -c "import json,sys; d=json.load(sys.stdin); print('TREE:', d['lot_id'], d['packing'], d['claimed']['result'])"
curl -s -m 20 -X POST localhost:8000/redteam/clone >/dev/null
curl -s -m 30 -X POST localhost:8000/anchor/run | python3 -c "import json,sys; d=json.load(sys.stdin); print('anchor:', d.get('tx'), 'id', d.get('anchor_id'))"

echo "== verify =="
curl -s -m 15 localhost:8000/products/SHOW-000001/verify | python3 -c "import json,sys; v=json.load(sys.stdin); print('SHOW-000001:', v['verdict'], v['risk_score'])"
curl -s -m 15 localhost:8000/products/LOT-DEMO-A-000001/verify | python3 -c "import json,sys; v=json.load(sys.stdin); print('DEMO clone:', v['verdict'], v['risk_score'])"
curl -s -m 15 localhost:8000/lots/LOT-TREE/blast-radius | python3 -c "import json,sys; b=json.load(sys.stdin); print('TREE:', b['by_status'], len(b['tree']), 'pallets')"
curl -s -m 15 localhost:8000/converge/health | python3 -c "import json,sys; h=json.load(sys.stdin); print('health ok, map:', h['actor_map_source'])"
echo DONE

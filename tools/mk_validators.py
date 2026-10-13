"""Mint validator keys for laptops 2 and 3 (run once, on any machine).

Outputs besu-qbft/validator2.key + validator3.key (gitignored: never commit
private keys) and prints the QBFT validator addresses + enode URLs to fill
into docs/NODES_3LAPTOP.md. QBFT validators are the node addresses
(keccak(uncompressed_pubkey)[-20:]), NOT contract addresses.
"""

import secrets

from eth_keys import datatypes, keys

for n in (2, 3):
    priv = keys.PrivateKey(secrets.token_bytes(32))
    path = f"besu-qbft/validator{n}.key"
    with open(path, "w") as f:
        f.write(priv.to_hex()[2:] if hasattr(priv, "to_hex") else priv.to_bytes().hex())
    addr = priv.public_key.to_address()
    enode_pub = priv.public_key.to_bytes().hex()
    print(f"--- laptop {n} ---")
    print(f"key file:  {path}  (DO NOT COMMIT)")
    print(f"validator: {addr}")
    print(f"enode key: {enode_pub}")
    print(f'enode url:  enode://{enode_pub}@<LAPTOP{n}-LAN-IP>:30303')

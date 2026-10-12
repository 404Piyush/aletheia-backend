import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from backend.crypto import gen_keypair, sign_event, verify_event, event_hash
from backend.merkle import root_of, get_proof, verify_proof


def test_crypto_roundtrip():
    sk, vk = gen_keypair()
    body = {"serial": "X-1", "type": "SCAN", "actor": "did:a", "ts": 1,
            "lat_mdeg": None, "lon_mdeg": None, "biz_step": "s",
            "to_actor": None, "prev_hash": None, "ref_hash": "0x" + "00" * 32, "seq": None}
    ev = sign_event(sk, body)
    assert verify_event(ev)
    ev["biz_step"] = "tampered"
    assert not verify_event(ev)


def test_merkle_proof():
    leaves = [f"S-{i}".encode() for i in range(8)]
    root = root_of(leaves)
    proof = get_proof(leaves, 3)
    assert verify_proof(leaves[3], proof, root)
    assert not verify_proof(b"forged", proof, root)

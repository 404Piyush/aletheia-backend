"""Canonical JSON + hashing + Ed25519. No floats for coords/ts on the wire."""
import hashlib
import json
from typing import Any, Dict

from nacl.signing import SigningKey, VerifyKey
from nacl.exceptions import BadSignatureError

GENESIS_PREV = "0x" + "00" * 32


def canonical(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def sha256_hex(data: bytes) -> str:
    return "0x" + hashlib.sha256(data).hexdigest()


def event_hash(payload: Dict) -> str:
    """Hash of event body excluding event_hash/signature."""
    body = {k: v for k, v in payload.items() if k not in ("event_hash", "signature", "signer")}
    return sha256_hex(canonical(body))


def to_hex(b: bytes) -> str:
    return "0x" + b.hex()


def gen_keypair(seed_hex: str | None = None):
    if seed_hex:
        seed = bytes.fromhex(seed_hex.replace("0x", ""))[:32]
    else:
        import os
        seed = os.urandom(32)
    sk = SigningKey(seed)
    return sk, sk.verify_key


def sign_event(sk: SigningKey, payload: Dict) -> Dict:
    h = event_hash(payload)
    sig = sk.sign(bytes.fromhex(h[2:])).signature
    out = dict(payload)
    out["event_hash"] = h
    out["signature"] = to_hex(sig)
    out["signer"] = to_hex(bytes(sk.verify_key))
    return out


def verify_event(ev: Dict) -> bool:
    try:
        h = event_hash(ev)
        if ev.get("event_hash") != h:
            return False
        vk = VerifyKey(bytes.fromhex(ev["signer"][2:]))
        vk.verify(bytes.fromhex(ev["signature"][2:]) + bytes.fromhex(h[2:]))
        return True
    except (BadSignatureError, KeyError, ValueError):
        return False

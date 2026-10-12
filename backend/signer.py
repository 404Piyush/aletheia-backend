"""Device-simulator keystore. Holds demo actor keys, signs on request.
Prototype stand-in for secure-element wallets. Deterministic from master secret
so restarts keep the same keys. NEVER use in production."""
import hashlib
import hmac
import os

from nacl.signing import SigningKey

from .crypto import sign_event, to_hex

MASTER = os.environ.get("SIGNER_MASTER", "trustfabric-dev-master").encode()
_dids = ["did:maker1", "did:transporter1", "did:retailer1",
         "did:attacker", "did:attacker2", "operator"]


def _key_for(did: str) -> SigningKey:
    return SigningKey(hmac.new(MASTER, did.encode(), hashlib.sha256).digest())


def pubkey_hex(did: str) -> str:
    return to_hex(bytes(_key_for(did).verify_key))


def sign_for(did: str, payload: dict) -> dict:
    return sign_event(_key_for(did), payload)


def known_dids() -> list[str]:
    return list(_dids)

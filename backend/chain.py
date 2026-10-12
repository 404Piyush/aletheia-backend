"""Chain adapter: real Besu QBFT when CHAIN_RPC + contract available, else local mimic.
Contract: contracts/ProvenanceRegistry.flat.sol -> contracts/deployed.json
Env: CHAIN_RPC (default http://localhost:9545), CONTRACT (overrides deployed.json),
     REGISTRAR_KEY (overrides besu-qbft/validator.key dev key)."""
import hashlib
import json
import os
import threading
import time

STORE = os.environ.get("ANCHOR_STORE", "anchors.json")
_file_lock = threading.Lock()
_tx_lock = threading.Lock()

_w3 = None
_contract = None
_registrar = None
USE_CHAIN = False


def _init():
    global _w3, _contract, _registrar, USE_CHAIN
    rpc = os.environ.get("CHAIN_RPC", "http://localhost:9545")
    addr = os.environ.get("CONTRACT")
    if not addr:
        try:
            with open(os.path.join(os.path.dirname(__file__), "..", "contracts", "deployed.json")) as f:
                addr = json.load(f)["address"]
        except (FileNotFoundError, KeyError):
            return
    try:
        from web3 import Web3
        w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 10}))
        if not w3.is_connected():
            return
        with open(os.path.join(os.path.dirname(__file__), "..", "contracts", "registry_abi.json")) as f:
            abi = json.load(f)
        key = os.environ.get("REGISTRAR_KEY")
        if not key:
            try:
                with open(os.path.join(os.path.dirname(__file__), "..", "besu-qbft", "validator.key")) as f:
                    key = f.read().strip()
            except FileNotFoundError:
                return
        _w3, _contract = w3, w3.eth.contract(address=addr, abi=abi)
        _registrar = w3.eth.account.from_key(key)
        USE_CHAIN = True
    except Exception:
        USE_CHAIN = False


_init()


def lot_key(lot_id: str) -> bytes:
    if _w3:
        return _w3.keccak(text=lot_id)
    return hashlib.sha256(lot_id.encode()).digest()


def _send(fn):
    with _tx_lock:
        tx = fn.build_transaction({
            "from": _registrar.address,
            "nonce": _w3.eth.get_transaction_count(_registrar.address),
            "gas": 500000,
            "gasPrice": 0,
        })
        signed = _registrar.sign_transaction(tx)
        h = _w3.eth.send_raw_transaction(signed.raw_transaction)
        # Short receipt wait: QBFT dev chain seals instantly. A long wait here
        # blocks the single-worker API for every request (past outage cause).
        rc = _w3.eth.wait_for_transaction_receipt(h, timeout=20)
        assert rc.status == 1, f"tx reverted: {h.hex()}"
        return h.hex()


# ---------- mimic fallback ----------
def _load():
    try:
        with open(STORE) as f:
            return json.load(f)
    except FileNotFoundError:
        return {"lots": {}, "anchors": [], "frozen": {}, "actors": {}}


def _save(s):
    with _file_lock:
        with open(STORE, "w") as f:
            json.dump(s, f)


# ---------- public API ----------
class ChainUnavailable(Exception):
    """Raised when the chain backend cannot be reached. Callers must fail
    closed (deny writes) rather than assume a safe state."""
def register_lot(lot_id: str, manufacturer: str, root: str, qty: int) -> str:
    if USE_CHAIN:
        try:
            existing = _contract.functions.lots(lot_key(lot_id)).call()
            if existing[0] != "0x0000000000000000000000000000000000000000":
                return f"exists:{lot_id}"
        except Exception:
            pass
        h = _send(_contract.functions.registerLot(
            lot_key(lot_id), _registrar.address,
            bytes.fromhex(root[2:]), qty))
        return h
    s = _load()
    s["lots"][lot_id] = {"manufacturer": manufacturer, "root": root, "qty": qty, "ts": int(time.time())}
    _save(s)
    return f"local:register:{lot_id}"


def lot_root(lot_id: str):
    if USE_CHAIN:
        try:
            lot = _contract.functions.lots(lot_key(lot_id)).call()
            return "0x" + lot[1].hex() if lot[1] != b"\x00" * 32 else None
        except Exception:
            return None
    s = _load()
    lot = s["lots"].get(lot_id)
    return lot["root"] if lot else None


def lot_qty(lot_id: str):
    if USE_CHAIN:
        try:
            return _contract.functions.lots(lot_key(lot_id)).call()[2]
        except Exception:
            return None
    s = _load()
    lot = s["lots"].get(lot_id)
    return lot["qty"] if lot else None


def anchor_batch(root: str, count: int) -> tuple[int, str]:
    if USE_CHAIN:
        h = _send(_contract.functions.anchorBatch(bytes.fromhex(root[2:]), count))
        aid = _contract.functions.anchorCount().call() - 1
        return aid, h
    s = _load()
    s["anchors"].append({"root": root, "count": count, "ts": int(time.time())})
    _save(s)
    aid = len(s["anchors"]) - 1
    return aid, f"local:anchor:{aid}"


def get_anchor(aid: int):
    if USE_CHAIN:
        try:
            r, c, t = _contract.functions.getAnchor(aid).call()
            return {"root": "0x" + r.hex(), "count": c, "ts": t}
        except Exception:
            return None
    s = _load()
    if 0 <= aid < len(s["anchors"]):
        return s["anchors"][aid]
    return None


def freeze_lot(lot_id: str, value: bool = True):
    if USE_CHAIN:
        # Propagate: callers (recall) must NOT mark the DB frozen when the
        # on-chain freeze failed, or chain and DB diverge silently.
        _send(_contract.functions.freezeLot(lot_key(lot_id), value))
        return
    s = _load()
    s["frozen"][lot_id] = value
    _save(s)


def is_frozen(lot_id: str) -> bool:
    if USE_CHAIN:
        try:
            return bool(_contract.functions.lotFrozen(lot_key(lot_id)).call())
        except Exception as e:
            # Fail CLOSED: unknown chain state must pause writes, never allow them.
            raise ChainUnavailable(f"lotFrozen call failed: {type(e).__name__}")
    return bool(_load()["frozen"].get(lot_id, False))


def set_actor(addr: str, active: bool):
    if USE_CHAIN:
        return  # actor registry lives in backend actors table for prototype
    s = _load()
    s["actors"][addr] = active
    _save(s)


def is_active(addr: str, default: bool = True) -> bool:
    return bool(_load()["actors"].get(addr, default))


def chain_info() -> dict:
    if USE_CHAIN:
        return {"mode": "besu-qbft", "chain_id": _w3.eth.chain_id,
                "block": _w3.eth.block_number, "contract": _contract.address,
                "registrar": _registrar.address}
    return {"mode": "local-mimic"}

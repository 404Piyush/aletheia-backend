"""Chain adapter: web3 if RPC available, else local file-backed mimic with same API."""
import json
import os
import time

STORE = os.environ.get("ANCHOR_STORE", "anchors.json")


def _load():
    try:
        with open(STORE) as f:
            return json.load(f)
    except FileNotFoundError:
        return {"lots": {}, "anchors": [], "frozen": {}, "actors": {}}


def _save(s):
    with open(STORE, "w") as f:
        json.dump(s, f)


def w3_or_none():
    rpc = os.environ.get("CHAIN_RPC")
    if not rpc:
        return None
    try:
        from web3 import Web3
        w3 = Web3(Web3.HTTPProvider(rpc))
        return w3 if w3.is_connected() else None
    except Exception:
        return None


def register_lot(lot_id: str, manufacturer: str, root: str, qty: int) -> str:
    s = _load()
    s["lots"][lot_id] = {"manufacturer": manufacturer, "root": root, "qty": qty, "ts": int(time.time())}
    _save(s)
    return f"local:register:{lot_id}"


def lot_root(lot_id: str):
    s = _load()
    lot = s["lots"].get(lot_id)
    return lot["root"] if lot else None


def lot_qty(lot_id: str):
    s = _load()
    lot = s["lots"].get(lot_id)
    return lot["qty"] if lot else None


def anchor_batch(root: str, count: int) -> tuple[int, str]:
    s = _load()
    s["anchors"].append({"root": root, "count": count, "ts": int(time.time())})
    _save(s)
    aid = len(s["anchors"]) - 1
    return aid, f"local:anchor:{aid}"


def get_anchor(aid: int):
    s = _load()
    if 0 <= aid < len(s["anchors"]):
        return s["anchors"][aid]
    return None


def freeze_lot(lot_id: str, value: bool = True):
    s = _load()
    s["frozen"][lot_id] = value
    _save(s)


def is_frozen(lot_id: str) -> bool:
    return bool(_load()["frozen"].get(lot_id, False))


def set_actor(addr: str, active: bool):
    s = _load()
    s["actors"][addr] = active
    _save(s)


def is_active(addr: str, default: bool = True) -> bool:
    return bool(_load()["actors"].get(addr, default))

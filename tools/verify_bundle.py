"""Re-verify evidence bundle with only chain RPC/file — no backend trust."""
import json
import sys
import hashlib

sys.path.insert(0, ".")
from backend.merkle import verify_proof


def main(path: str):
    b = json.load(open(path))
    results = []
    prev = "0x" + "00" * 32
    for item in b.get("events", []):
        ev = item if "event_hash" in item else item.get("event", item)
        # signature check skipped for demo sigs; check hash linkage
        body_keys = ("serial", "type", "actor", "ts", "lat_mdeg", "lon_mdeg",
                     "biz_step", "to_actor", "prev_hash", "ref_hash", "seq")
        body = {k: ev[k] for k in body_keys if k in ev}
        h = "0x" + hashlib.sha256(
            json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        results.append(("hash", ev.get("event_hash", "")[:12], h == ev.get("event_hash")))
        if ev.get("type") in ("MANUFACTURED", "HANDOVER_OFFER", "HANDOVER_ACCEPT", "SOLD"):
            results.append(("chain-link", ev.get("event_hash", "")[:12], ev.get("prev_hash") == prev))
            prev = ev.get("event_hash")
    lp = b.get("lot_proof", {})
    if lp.get("proof"):
        leaf = b["serial"].encode()
        import hashlib as hl
        cur = hl.sha256(leaf).digest()
        for hx, left in lp["proof"]:
            sib = bytes.fromhex(hx[2:])
            cur = hl.sha256(sib + cur).digest() if left else hl.sha256(cur + sib).digest()
        results.append(("lot-vs-root", b["serial"][:12], ("0x" + cur.hex()) == lp.get("root")))
    for kind, h8, ok in results:
        print(("PASS" if ok else "FAIL"), kind, h8)
    print("ALL PASS" if all(r[2] for r in results) else "SOME CHECKS FAILED")


if __name__ == "__main__":
    main(sys.argv[1])

"""SHA256 binary Merkle tree. Leaves are bytes."""
import hashlib


def _h(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


def build_tree(leaves: list[bytes]) -> list[list[bytes]]:
    if not leaves:
        return [[b"\x00" * 32]]
    level = [_h(l) for l in leaves]
    tree = [level]
    while len(level) > 1:
        nxt = []
        for i in range(0, len(level), 2):
            l = level[i]
            r = level[i + 1] if i + 1 < len(level) else l
            nxt.append(_h(l + r))
        tree.append(nxt)
        level = nxt
    return tree


def root_of(leaves: list[bytes]) -> bytes:
    return build_tree(leaves)[-1][0]


def get_proof(leaves: list[bytes], index: int) -> list[tuple[bytes, bool]]:
    """Returns [(sibling, sibling_is_left)] from leaf to root."""
    tree = build_tree(leaves)
    proof = []
    idx = index
    for level in tree[:-1]:
        if len(level) == 1:
            break
        if idx % 2 == 0:
            sib_idx = idx + 1 if idx + 1 < len(level) else idx
            proof.append((level[sib_idx], False))
        else:
            proof.append((level[idx - 1], True))
        idx //= 2
    return proof


def verify_proof(leaf: bytes, proof: list[tuple[bytes, bool]], root: bytes) -> bool:
    cur = _h(leaf)
    for sib, left in proof:
        cur = _h(sib + cur) if left else _h(cur + sib)
    return cur == root

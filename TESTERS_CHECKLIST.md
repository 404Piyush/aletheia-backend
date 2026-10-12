# Tester Checklist — 10-minute pass/fail

For non-backend testers. Base URL: `http://localhost:8000`
(or `https://continuity.404piyush.me`). Full commands in `MANUAL_TEST.md`
(section numbers referenced as §N). Tick each row; anything not matching the
**Expect** column is a bug — report the exact response + time.

**Rules:** run top to bottom. Stop before §E if anything fails (later rows
depend on earlier state). §E freezes a live lot — run it **last**.

| # | Check (§) | Expect |
|---|---|---|
| A | `GET /health` (§1) | `status: ok`, `block` rises on repeat calls |
| B | Verify `SHOW-000001` (§2) | `VERIFIED`, risk `0` |
| C | Forge a signature (§3) | `401` rejected |
| D | Clone attack then verify `LOT-DEMO-A-000001` (§5) | `CLONE_DETECTED`, risk `65`, failed checks include `R04` |
| E | `GET /incidents/LOT-DEMO-A-000001` (§6) | max severity `critical`, ≥2 findings, timeline present |
| F | Claim lifecycle on a fresh `CLAIM` lot (§8) | `SOLD` → `CLAIMED`; same session repeats `CLAIMED`; new session → `contested` |
| G | Tree blast-radius (§12) | 6 ACTIVE / 1 SOLD / 1 CLAIMED, 2 pallets, 3 cases, 8 units |
| H | Recall `LOT-TREE` (§12) | 7 `FROZEN` + 1 `CLAIMED`; new writes to the lot → `423` |
| I | Full suite (§13) | `11 passed` |

After H: unfreeze if the demo needs `LOT-TREE` writable again
(`MANUAL_TEST.md` header has the one-liner). Unknown responses → check the
Failure dictionary at the bottom of `MANUAL_TEST.md` first.

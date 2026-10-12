# Changes needed on CONTINUITY side — convergence alignment

Hi — reviewed your `Continuity-convergence` package. Your hub + spec are solid.
Three things on your side need to change for the joint demo to work. Everything
else (actor map, incident schema, gate flow) already matches.

## 1. CRITICAL — Wrong Besu network in your UI/config

Your `convergence/page.tsx` (Bridge Status card + curl example) points at:

- `http://localhost:8545`, Chain ID **1337**

That is **not our chain** — something else on this machine owns `:8545`.
Our chain:

- RPC `http://localhost:9545`, Chain ID **1338**
- Contract: redeployed per cold restart — read live from our `GET /`
  (`chain.contract`); registrar stable `0x1848d8Ded316cA9c0F4F9Cc553d1E64f4d66e32F`

Any `simulate-clone` / anchor call you send to `:8545` will fail or land on the
wrong ledger. Two options, pick one:

- **(a) Recommended:** stop calling Besu directly. Use our endpoints —
  `POST /anchor/document` for dossier anchoring, `GET /bundle` + `/anchor`
  for proofs. They carry real tx hashes from our chain. Our public base
  `https://continuity.404piyush.me` works from anywhere, no RPC needed.
- **(b)** Repoint your Besu calls to `http://localhost:9545`, chain 1338,
  contract above. Read-only calls need no key.

Also update the hardcoded `http://localhost:8545` + `Chain Id: 1337` labels in
`app/(dashboard)/convergence/page.tsx` (~lines 239–266) and the curl example
(~line 628) to the values above, or point them at the public URL.

## 2. Our API base URL — use the public one for cross-machine links

Your page hardcodes `http://localhost:8000` for our API. Fine when both run on
one laptop, breaks otherwise. Our live public endpoint (SSL, no setup):

- **`https://continuity.404piyush.me`**

Suggested: `NEXT_PUBLIC_TRUSTFABRIC_API=https://continuity.404piyush.me`
with `http://localhost:8000` fallback. In particular the `evidenceBundleUrl`
you render should be the public URL so it opens from any machine.

## 3. Your Spring properties must point at us (not defaults)

`TrustFabricConvergenceService.java` reads our endpoints from properties —
with defaults that only work single-machine / wrong chain:

```properties
trustfabric.base-url=https://continuity.404piyush.me
# trustfabric.besu-url: do NOT use the :8545 default (wrong network).
# Either http://localhost:9545 (chain 1338, same machine) or remove direct
# Besu calls and use our POST /anchor/document + GET /bundle instead.
```

Same for the hardcoded `http://localhost:8000/products/LOT-A-000123/bundle`
in your simulate flow (~lines 305, 327) — build it from `trustfabric.base-url`
so links work from any machine.

## 4. Single-laptop ports (don't move these, nothing clashes)

| Port | Owner |
|---|---|
| 3000 | your Next.js |
| 3001 | our TrustTrace UI |
| 8000 | our FastAPI |
| 8080 | your Spring Boot |
| 8545 | other project — hands off |
| 9545 | our Besu QBFT |

Note: our `/redteam/*` attack endpoints are localhost-only by design, so your
`simulate-clone` can't trigger them remotely — drive your own sim, then read
our public `/verify` + `/bundle` for the evidence (that's the supported path).

## 5. Confirm these (one-line replies)

1. Project id: we send `apollo-core` (per your CONVERGENCE.md). Confirm.
2. `POST /api/convergence/incidents` — is it live on your `:8080`? Our
   dispatcher tries it first with exactly your 3.2 schema
   (`serial, lotId, verdict, riskScore, confidence, failedChecks[], evidenceBundleUrl`),
   then falls back to `/api/handoffs`. Tell us when `:8080` is up and we'll
   fire one real incident end-to-end.
3. Actor map confirmed as-is (`did:maker1→user-yash`,
   `did:transporter1→user-shruti`, `did:retailer1→user-jessica`)?

## Nothing to change (already aligned)

- Incident schema 3.2, handoff creation 3.3, completeness gate 400/force-accept,
  blast-radius shape, joint 5-min demo order.
- Our `POST /converge/case` already emits your exact incident payload with the
  public bundle URL. Verified against your spec (test-pinned).

Once 1–5 are done, joint flow is: our clone → your dossier + 3 recall tasks →
gate blocks sign-off → force-accept passes → both audit trails match.

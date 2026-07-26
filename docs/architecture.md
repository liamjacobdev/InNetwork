# InNetwork — architecture

InNetwork answers one question — *"which licensed providers near me take my insurance,
and can I act on it now?"* — from **free, public data only**, and never claims more than
a real source supports.

## Components

```
                    ┌─────────────────────────────────────────────┐
   Browser          │  innetwork.html  (CSP, no inline script)      │
                    │  ├─ innetwork.config.js   (injected config)   │
                    │  ├─ innetwork.bundle.js   (esbuild ← src/)    │
                    │  └─ innetwork.logic.js    (pure, unit-tested) │
                    └───────────────┬─────────────────────────────┘
                                    │ same-origin /api/* (CORS-locked, rate-limited)
                    ┌───────────────▼─────────────────────────────┐
                    │  FastAPI backend (app/)                      │
                    │  main.py  — routes, middleware, /healthz,    │
                    │             /readyz, /metrics, /coverage     │
                    │  insurance.py — two-tier confidence model    │
                    │  nppes.py · geocode.py — upstream proxies    │
                    │  planet_registry.py · verify_payers.py       │
                    │  ── seams (interfaces.py) ──                 │
                    │  Datastore · CacheBackend · RateLimiter ·    │
                    │  GeocoderBackend                             │
                    └───┬───────────┬───────────┬─────────────┬────┘
                        │           │           │             │
                  ┌─────▼────┐ ┌────▼─────┐ ┌───▼──────┐ ┌────▼─────────┐
                  │ SQLite   │ │  NPPES   │ │ Census / │ │ FHIR Plan-Net│
                  │ (db.py)  │ │ registry │ │Nominatim │ │  + TiC files │
                  └──────────┘ └──────────┘ └──────────┘ └──────────────┘
                   datastore     providers    geocoding    verified insurance
```

## Data flow (a search)
1. The page calls `GET /api/providers/search` (same origin).
2. `nppes.search` queries the NPPES registry (cached, retried, timeout-bounded) → providers.
3. `insurance.Registry.annotate` tags each provider per plan with `{value, confidence,
   level, source, source_url?, fetched_at?}` — **verified** (a real source for that NPI)
   or **estimated** (a clearly-labeled catalog guess). Verified always wins; "unknown"
   is never turned into a yes.
4. For a radius search the backend geocodes the candidate pool, keeps only those within
   `radius` miles of the ZIP centroid, sorts by distance, then truncates — the backend is
   authoritative for the boundary.
5. The page renders cards + a map; verified hits show provenance ("Verify · checked <date>").

## The two-tier confidence model (the heart)
- **verified** — Medicare enrollment file (national), an ingested Transparency-in-Coverage
  in-network file (by NPI), a validated public FHIR Plan-Net directory (per-NPI, network
  linked), or a Marketplace issuer's machine-readable `providers.json` (per-NPI **per
  plan**). Carries `{source, source_url, fetched_at}`. A green badge is always traceable.
- **estimated** — a curated major payer that operates in the provider's state. Hidden by
  default; shown only via "Include estimated" and labeled "likely — confirm". National
  estimates that match everyone in-state are honestly framed as *area context*, not a match.

Trust invariants are executable: `tests/test_trust_rules.py` asserts no path turns
unknown→yes, estimates never render Confirmed, and verified results always carry provenance.

`level` separates the two strengths of a verified answer: **plan** (Medicare, Marketplace —
the claim is about a specific plan) vs **payer** (TiC, Plan-Net — the provider is listed in
the payer's network, so the UI still says *confirm your specific plan*).

## Membership at plan-level scale
Rail 4 turns the catalog from tens of payers into thousands of plan-level entries, which
changes two things in `app/membership.py`:

- **Bitmaps decode on demand.** `MembershipStore.load()` reads the manifest and stats each
  blob; it decodes nothing. A blob is mmap'd, sha256-verified, and deserialized the first
  time it is actually asked a membership question, and least-recently-used decodings are
  evicted past `INNETWORK_MEMBERSHIP_MAX_RESIDENT`. A search in Texas never touches an
  Alaska plan's bytes. The integrity guarantee is unchanged, only re-timed: a missing or
  wrong-sized blob is caught by the `stat` at load, and the full hash is still verified
  before any blob answers.
- **Blobs are shared.** An issuer sells many plans over one network, so each plan is its own
  manifest entry pointing at one content-addressed `payers/mrf/<sig>.roaring`.

Serving follows: `/api/insurance/plans?state=XX` returns national plans plus that state's,
so a client never receives thousands of irrelevant plans, and `/healthz` reports plan
*counts* rather than the full list. Its dead-man's-switch trips on a stalled pipeline (a
critical source, or a material share of sources stale) — not on one flaky issuer.

## Scale-readiness seams (interfaces.py)
Every external dependency sits behind a Protocol so scaling is a **config swap**, not a
rewrite: `Datastore` (SQLite→Postgres), `CacheBackend` (in-proc→Redis), `RateLimiter`
(per-worker→shared), `GeocoderBackend`. Upstreams are timeout-bounded + retried, and
degrade to "unknown" (never a fabricated answer) on failure.

## Deploy
One HTML file + the built bundle + the FastAPI backend, behind Caddy (TLS + the
authoritative security headers). Ingestion is a free GitHub Actions cron hitting a
token-secured endpoint; `/healthz` enforces data-age SLOs. See [docs/runbook.md](runbook.md).

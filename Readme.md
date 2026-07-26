# InNetwork API

The real backend behind InNetwork. It does four jobs:

1. **Proxies the official CMS NPPES registry** (`/api/npi`, `/api/providers/search`) — server-side, so the browser never depends on flaky public CORS proxies.
2. **Batches geocoding server-side** (`/api/geocode/batch`) with a persistent SQLite cache. Works **out of the box** via the free, keyless [US Census Geocoder](https://geocoding.geo.census.gov) (US-only — exactly this app's scope, with no rate limit); OpenStreetMap Nominatim is an optional fallback (set `INNETWORK_UA` to a real contact email to enable it — Nominatim rejects placeholder agents). One request from the browser geocodes a whole page of results.
3. **Resolves real insurance acceptance** (`/api/insurance/...`) — Medicare out of the box, commercial payer networks as you configure them. No fabricated data, ever.
4. **Orchestrates all of the above** in one call (`/api/providers/search`) so the frontend gets providers, insurance flags, and coordinates in a single round trip.

---

## The insurance data — what's real, and how

InNetwork covers **many plans, grouped by category** (Medicare, Medicare Advantage, Medicaid, Commercial/Employer, ACA Marketplace, TRICARE, VA) **and** by named payer (UnitedHealthcare, Aetna, Cigna, Blue Cross Blue Shield, Humana, Kaiser, …). It never fabricates — instead every answer carries a **confidence tier**:

- **Verified** (green *Confirmed* badge): confirmed for *this* provider from a real source — the Medicare enrollment file, a payer's FHIR Plan-Net directory, or an ingested Transparency-in-Coverage file.
- **Estimated** (amber *Likely* badge): a major payer that operates in the provider's state, from the curated catalog in `app/catalog.py`. Shown as "likely — confirm with the provider," **never** as confirmed. A verified source always supersedes an estimate.

**Verified by default.** The filter defaults to **Verified only**: estimated payers are hidden from the filter, estimated badges never render, and search requires confirmed acceptance (`accepts_mode=verified`). The **Include estimated** toggle (`accepts_mode=any`) is the *only* way estimates surface, and even then they read "likely — confirm," never *Confirmed*. As a payer gets backed by a verified source (FHIR Plan-Net or a TiC ingest) it graduates to a green *Confirmed* filter automatically — no UI change, because the catalog `id` is the stable join key.

### Source 1 — Medicare (works once you ingest one file)
The CMS **Medicare Fee-For-Service Public Provider Enrollment** dataset lists every NPI approved to bill Medicare. It's free, national, and updated quarterly.

- Dataset: https://data.cms.gov/provider-characteristics/medicare-provider-supplier-enrollment/medicare-fee-for-service-public-provider-enrollment
- Download the CSV, then ingest:

```bash
python -m app.ingest_medicare /path/to/enrollment.csv
# or stream from a URL:
python -m app.ingest_medicare "https://data.cms.gov/.../enrollment.csv"
```

Re-run quarterly to refresh. After ingest, "Medicare" appears as a verified filter and matching providers show a **Confirmed** badge.

### Source 2 — Commercial networks via FHIR Plan-Net (real, validated, auto-wired)
Under the CMS Interoperability rule (CMS-9115-F), Medicare Advantage, Medicaid, and CHIP payers must publish a **public, unauthenticated Provider Directory API** in FHIR R4 (Da Vinci PDEX Plan-Net). InNetwork queries it by NPI to confirm network participation.

The **validated public endpoints** in `app/planet_registry.py` are wired as *Confirmed* filters **out of the box** — no config. `python -m app.verify_payers` live-checks each one and regenerates the provenance ledger ([docs/provenance.md](docs/provenance.md)). To add a payer that isn't in the registry (e.g. one needing a free API key), copy `payers.example.json` to `payers.json`; a payer returns **in-network / not-found / unknown** and InNetwork never turns "unknown" into a yes.

**What "validated" requires (the trust gate).** It is *not* enough that `/PractitionerRole` returns a Bundle. The validator runs the exact per-NPI lookup the app performs and only wires an endpoint that answers it truthfully **both** ways:
- a **bogus** NPI must *not* resolve in-network — otherwise the directory ignores the NPI filter and would mark everyone in-network (a fabricated *yes*; e.g. Connecticut's Medicaid directory does this);
- a **real, listed** NPI must resolve in-network — otherwise per-NPI search returns nothing for everyone (a fabricated *no*; e.g. Premera and the reachable state-Medicaid directories do this).

**Validated public endpoints** (live-checked through 2026-07-01; see [docs/provenance.md](docs/provenance.md) for the full, auto-generated ledger including the tracked-but-not-wired ones):

| Payer (scope) | catalog id | Base URL | Round-trip |
|---|---|---|---|
| **UnitedHealthcare (national, commercial)** | `unitedhealthcare` | `https://flex.optum.com/fhirpublic/R4` | ✓ bogus→none, listed→active + network-linked (two-step) |
| **Cigna (national, commercial)** | `cigna` | `https://p-hi2.digitaledge.cigna.com/ProviderDirectory/v1` | ✓ bogus→none, listed→active + network-linked |
| **Humana (national, commercial)** | `humana` | `https://fhir.humana.com/api` | ✓ bogus→none, listed→active + network-linked (two-step) |
| Excellus BlueCross BlueShield (NY, commercial) | `excellus` | `https://fhir.excellusbcbs.com/fhir/api` | ✓ bogus→none, listed→active + network-linked (two-step) |
| Priority Partners — Johns Hopkins (MD Medicaid) | `priority_partners` | `https://api.jhhpfhir.com/r4/public-pp` | ✓ bogus→none, listed→in-network (Bundle 83,024) |

> **Honest finding (the public set is small, but it now includes three of the top-five insurers).**
> **UnitedHealthcare**, **Cigna**, and **Humana** all publish fully public, unauthenticated
> Da Vinci PDEX Plan-Net directories whose PractitionerRoles carry real network links — all
> three pass the round-trip and are wired as verified *national* commercial filters. (UHC and
> Humana don't support the chained `practitioner.identifier` search, so they use a **two-step**
> lookup — resolve the Practitioner by NPI, then its roles — configured per endpoint.) Other
> carriers like Aetna and Anthem/Elevance gate their Plan-Net behind developer registration
> (reachable, but `PractitionerRole` returns 401/403 without a key); and the 37 State Medicaid
> directories in the
> [CMS SMA-Endpoint-Directory](https://github.com/CMSgov/SMA-Endpoint-Directory) screened
> here return a Bundle but **fail the per-NPI round-trip** (no network links, or empty
> results for listed NPIs), so wiring them would fabricate answers. They stay **estimated**,
> never verified. Beyond the nationals, regional Blues graduate the same way as they pass:
> **Excellus BlueCross BlueShield** (NY) validates via the two-step path — its directory
> requires a search parameter (rejecting an unfiltered browse), which the validator now
> discovers around — and is wired **state-scoped to NY**, so it is never queried for, nor
> fabricates an answer about, an out-of-state provider.
> Because a live directory call is per-NPI, a verified payer is queried only
> when you actually filter by it, never on an unfiltered search. The registry +
> `verify_payers` make growing the set turnkey: an endpoint graduates to *Confirmed*
> automatically the moment it passes — never by assertion.

### Source 3 — Transparency-in-Coverage (verified commercial, by ingest)
Every commercial plan must publish machine-readable in-network files. Ingest a payer's in-network NPIs and that payer becomes a **verified** filter — a *Confirmed* badge that supersedes its estimated catalog entry. The payer id must match a catalog entry (`app/catalog.py`), e.g. `aetna`, `cigna`, `unitedhealthcare`:

```bash
python -m app.ingest_tic aetna /path/to/aetna_npis.csv
# accepts a CSV/list of NPIs, a TiC in-network .json/.json.gz, OR a TiC
# table-of-contents index — the index is auto-discovered and InNetwork fans out
# across every in-network file it lists, deduping NPIs across them (C2).
python -m app.ingest_tic cigna "https://payer.example/toc/index.json.gz"
```

Pointing the ingest at a payer's **published TiC root** is the easy path: most payers
publish a table-of-contents (`reporting_structure` → `in_network_files[].location`), and
InNetwork discovers and ingests each file automatically — no need to hand-list every
in-network URL.

**Scheduled refresh (monthly).** For ongoing operation, list each payer's published
in-network URL once in `tic_sources.json` (copy `tic_sources.example.json`) and run
the job — it ingests every configured payer and reports which flipped to *verified*.
Re-running is **idempotent**, so it's safe on a monthly cron:

```bash
cp tic_sources.example.json tic_sources.json   # then fill in real per-payer URLs
python -m app.ingest_tic_job          # refresh all configured payers
python -m app.ingest_tic_job aetna    # refresh just one
```

Document each payer's source URL and the date you retrieved it here as you wire them:

| Payer | TiC in-network source URL | Retrieved |
|-------|---------------------------|-----------|
| _(add each payer's published machine-readable index URL as you configure it)_ | | |

> Honest scope note: there is **no single free API** for all commercial insurers. The **estimated** tier gives you broad, recognizable named-payer filters on day one (clearly labeled, never presented as confirmed); the **verified** tier grows as you wire FHIR Plan-Net endpoints and ingest Transparency-in-Coverage files. Medicare is verified and national out of the box.

### Source 4 — Marketplace machine-readable files (verified, **plan-level**, national in one pass)

The broadest source, and the only one that arrives as a *registry of payers* rather than one
payer. Under 45 CFR 156.221(i) every Qualified Health Plan issuer on a Federally-Facilitated
Exchange must publish a public `index.json` → `providers.json` stating, per provider **per
plan**, that the provider is in that plan's network. CMS publishes the master list of those
URLs as the **Machine-Readable URL PUF**.

That makes it the strongest badge in the app. Transparency-in-Coverage is payer-level ("listed
somewhere in Aetna's file", so the UI must still say *confirm your specific plan*); this is
plan-level — the claim is about HIOS plan `73836AK0930001` specifically, so entries are written
with `level="plan"`, the same tier as the Medicare enrollment file.

```bash
python -m app.marketplace_registry --refresh   # parse the CMS PUF -> marketplace_sources.json
python -m app.marketplace_registry --probe     # which issuer indexes are reachable today
python -m app.harvest_marketplace              # harvest every issuer into plan bitmaps
python -m app.harvest_marketplace --index-url https://issuer.example/cms-data-index.json
python -m app.harvest_marketplace --state TX --max-files 2   # probe: writes nothing
```

Measured against the live PY2026 corpus (2026-07-24): **346 issuer rows → 108 distinct index
URLs across 30 states · 4,294 provider files · ~59 GB**, parsing at ~38 MB/s. It fits one free
GitHub Actions job — unlike TiC, these files are megabytes, not gigabytes. The
[monthly workflow](.github/workflows/harvest-marketplace.yml) refreshes the registry and
re-harvests; a new plan year is picked up automatically because the PUF URL resolver probes
newer years first.

**The trust gate, translated to a file rail.** There is no live endpoint to round-trip, so the
two-way check becomes four machine-checked conditions, all enforced before anything is written:

| Condition | Catches |
|---|---|
| **Luhn** (`membership.encode`) | a TIN or truncated id in the NPI slot fabricating a "yes" |
| **Completeness** | a `provider_urls` entry that failed to read — a hole would read as a fabricated "no", so the issuer keeps its last-good bitmaps |
| **Discrimination** | a "network" containing an implausible share of the national provider set — a directory dump that would mark everyone in-network |
| **Positive control** | sampled members that don't exist in NPPES — ghost identifiers are not a network |

Three shapes in the real data drive the parser, each verified against live files rather than
assumed:

- **`network_tier` is free text, and one value means the opposite of in-network.** Sampling 18
  issuers found 25+ distinct tiers (`PREFERRED`, `PREFERREDTIER`, `STANDARDTIER`, `IN-NETWORK`,
  named third-party networks like `CX--CONNECTION-DENTAL--PPO-USA`) — and `OUT-OF-NETWORK`. So
  the rule is a **denylist**: being listed under a plan is the issuer's assertion of
  participation, and only an explicit out-of-network tier is excluded. (`NON-PREFERRED` is a
  rate tier *inside* the network and is kept.)
- **Plan rows carry `years`.** Rows for a prior plan year are dropped, or last season's network
  ships as current.
- **A `providers.json` is not one issuer** — one issuer's file routinely carries another's plan
  ids. Issuer and state come from the HIOS plan id itself, never from the URL we arrived by.

Issuers sell many plans over one network (measured 4.3x–10x), so plans are separate catalog
entries but share one content-addressed blob under `payers/mrf/<sig>.roaring`.

### Automated refresh + freshness SLOs (zero manual data steps)
The [scheduled-ingest workflow](.github/workflows/ingest.yml) (free GitHub Actions cron) refreshes the deployed instance — TiC monthly, Medicare quarterly — by POSTing the **token-secured** `POST /admin/ingest?source=tic|medicare|all` endpoint (set repo secrets `INNETWORK_URL` + `INNETWORK_ADMIN_TOKEN`; the endpoint is disabled until `INNETWORK_ADMIN_TOKEN` is set, and runs the ingest in the background). `GET /healthz` reports per-source **data ages vs SLOs** and **flips to 503** when a source goes stale (Medicare > `INNETWORK_MEDICARE_MAX_AGE_DAYS`, default 100; payers > `INNETWORK_PAYER_MAX_AGE_DAYS`, default 35) — a dead-man's-switch an uptime monitor can watch, so a stalled ingest is surfaced rather than silently serving old data.

---

## Run it locally

```bash
pip install -r requirements.txt
export INNETWORK_DB=./innetwork.db
python -m app.ingest_medicare sample_medicare.csv   # tiny demo file included (optional)
uvicorn app.main:app --reload --port 8000
# Open http://localhost:8000  — the backend serves the web page AND proxies the
# registry server-side, so provider search works with no browser CORS limits.
# (http://localhost:8000/healthz for a status check.)
```

**Windows, one click:** double-click `start-innetwork.bat`. It uses the bundled
`.venv`, starts the backend, and opens `http://localhost:8000` for you.

> Note: the provider search queries the **live** CMS NPPES registry through the
> backend — there's no multi-GB database to download. Opening `innetwork.html`
> as a plain file (or via a static server like `python -m http.server`) can't
> reach the registry from the browser; run the backend instead.

## Deploy to a domain you own

Docker Compose with Caddy handles TLS automatically.

1. **Point DNS** at your server: an `A` record for `api.yourdomain.com` → your server's IP.
2. **Edit `Caddyfile`** — replace `api.yourdomain.com` with that subdomain.
3. **Create `.env`** from `.env.example`; set `ALLOWED_ORIGINS` to the domain your frontend is served from (e.g. `https://innetwork.yourdomain.com`).
4. **Bring it up:**

```bash
cp payers.example.json payers.json   # optional: add commercial payers
docker compose up -d --build
# Caddy fetches a Let's Encrypt cert for your domain automatically.
```

5. **Ingest Medicare into the running container:**

```bash
docker compose exec api python -m app.ingest_medicare "https://data.cms.gov/.../enrollment.csv"
```

6. **Connect the frontend** in one step (sets both `API_BASE` and the CSP `connect-src`, which must agree):

```bash
python configure_frontend.py https://api.yourdomain.com
# or write a separate file: --out innetwork.prod.html
```

   Host the result on `https://innetwork.yourdomain.com` (any static host). The insurance filter appears automatically once the API reports available plans.

Any container host works (Fly.io, Railway, Render, a VPS). Keep the SQLite volume persistent so the geocode cache and Medicare index survive restarts.

> **Reverse-proxy trust (rate limiting).** Behind Caddy, the app would otherwise see every request as coming from Caddy's container IP, collapsing the per-client rate limiter into a single global bucket that can lock out all users. The provided setup fixes this: uvicorn runs with `--proxy-headers` and `docker-compose.yml` sets `INNETWORK_TRUST_PROXY=true`, so the limiter buckets on the real client via `X-Forwarded-For` (Caddy's `reverse_proxy` sets it by default). **Only enable `INNETWORK_TRUST_PROXY` when the API is reachable solely through a proxy you control** — the `api` service uses `expose` (not `ports`), so it is not directly reachable. If you front it with something other than Caddy, ensure that proxy sets/overwrites `X-Forwarded-For`; otherwise leave the flag off so a client can't spoof the header to dodge the limit.

---

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/healthz` | Status, Medicare index size, available plans |
| GET | `/api/insurance/plans` | Filterable plans — flat list + grouped `categories`, each with a `confidence` |
| GET | `/api/insurance/{npi}?state=` | Coverage for one NPI (`state` resolves estimated-tier plans) |
| GET | `/api/npi` | Proxied NPPES search (raw results) |
| GET | `/api/providers/search` | NPPES + insurance flags + plan filter + radius + batch geocode |
| GET | `/api/geocode?q=` | One geocode (cached) |
| POST | `/api/geocode/batch` | Batch geocode `{items:[{key,q}]}` |
| GET | `/api/reverse?lat=&lon=` | Reverse geocode → postcode |

`/api/providers/search` params: `zip, city, state, npi, name, taxonomy, type, limit`, `radius` (miles; widens beyond the exact ZIP and distance-filters), `accepts` (comma-sep plan ids), `accepts_mode` (`verified` | `any`), `geocode` (bool). Each provider's `insurance` is `{plan_id: {value, confidence, source}}`.

All `/api/*` routes are **rate-limited per client** (`RATE_LIMIT_MAX`/`RATE_LIMIT_WINDOW`; behind a proxy set `INNETWORK_TRUST_PROXY=true` so the bucket is the real client IP — see the deploy note above) and **CORS** is locked to `ALLOWED_ORIGINS` (localhost-only if unset — never a blanket `*`).

### Resilience
Every upstream call (NPPES, the geocoders, each FHIR Plan-Net directory) is **bounded** (timeouts) and **retried/cached**, and degrades to a safe answer on failure (search → controlled 502; insurance → "unknown", never a fabricated yes; map → no coords) — a down payer never affects another. `GET /readyz` is a load-balancer readiness probe (datastore reachable + registry built). The shared-state seams (rate limiter, cache, datastore — [app/interfaces.py](app/interfaces.py)) make a global limit/cache/store across workers a config swap to Redis/Postgres.

### Security & privacy
- **CSP with no `'unsafe-inline'` for scripts** — all JS is external (same-origin config + bundle) or the pinned Leaflet CDNs, so an injected inline `<script>` won't run. Sent as a real header at the edge (Caddyfile) and as the page's meta CSP.
- **Full security headers** on every response: HSTS, `X-Content-Type-Options`, `X-Frame-Options: DENY`, `Referrer-Policy`, `Permissions-Policy` (geolocation only for "near me"), `Cross-Origin-Opener-Policy`.
- **No PII in logs** — search terms, the upstream URL, and client IPs are never persisted (failure logs record only which fields were present + the error type; httpx request logging is pinned to WARNING). Enforced by a test.
- **Input-length caps** on every query field; **`/admin/ingest` and `/metrics`** are guarded by `INNETWORK_ADMIN_TOKEN`.

## Tests
```bash
pip install -r requirements-dev.txt
pytest          # NPPES params, DB/indexes, insurance confidence model, geocoder chain, API end-to-end

npm install
npm run build   # esbuild: bundle src/ -> innetwork.bundle.js (the page loads this)
npm test        # Vitest unit tests for innetwork.logic.js (enforces coverage threshold)
npm run test:e2e   # Playwright smoke (run `npx playwright install chromium` once first)
```
The frontend is authored as ES modules under `src/` (`config.js` reads the injected
`window.INNETWORK_CONFIG`; `main.js` is the app) plus the pure, unit-tested
`innetwork.logic.js`. `npm run build` bundles them into a single same-origin
`innetwork.bundle.js` — so the deploy story stays "one HTML file + the bundle + a
backend", the page carries no inline business logic, and the build is reproducible
(CI rebuilds and fails on any diff). Edit `src/` and rebuild; never hand-edit the
bundle. `tests/fixtures/normalize_golden.json` is asserted by both Python and JS so
the `normalize()` ↔ `buildProviders()` contract can't drift. CI runs all suites on
every push.

## What was tested vs. what needs your network
Verified here with an automated suite: app boots, DB/ingest (Medicare + TiC), the insurance confidence model (verified vs estimated, regional gating, verified-supersedes-estimate, post-startup TiC ingest with no restart), FHIR `check_many` mapping (mocked), the geocoder source chain (Census primary, Nominatim fallback, SQLite cache — mocked), NPPES param building incl. radius widening, per-client rate limiting behind a proxy, the batch-geocode cap, the `normalize()` golden shape, and server-authoritative radius search (out-of-radius dropped, distance-sorted, closest survive truncation) end-to-end against a mocked registry. **Not** reachable from the build sandbox, so verify in your environment: live NPPES results, live geocoding, and each payer's FHIR endpoint. The code paths and error handling for those are in place.
## Documentation
- [docs/architecture.md](docs/architecture.md) — components, data flow, the two-tier model, the seams.
- [docs/runbook.md](docs/runbook.md) — zero→running (<15 min), ingest, backup/restore, incident response.
- [docs/provenance.md](docs/provenance.md) — auto-generated ledger of validated public endpoints.
- API: live Swagger at `/docs`, schema at `/openapi.json`; the committed [docs/openapi.json](docs/openapi.json) is kept current by a test.

## Contributing & License
Contributions welcome — see [CONTRIBUTING.md](CONTRIBUTING.md) for the dev setup, the
test layout, and the trust rules (verified vs. estimated is sacred; never ship what you
can't verify). InNetwork is released under the [MIT License](LICENSE).

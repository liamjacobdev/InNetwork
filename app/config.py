"""Runtime configuration, sourced from environment variables with safe defaults.

Nothing here is secret-by-default; the only values you must set for production are
ALLOWED_ORIGINS (lock CORS to your frontend) and, if you use commercial payers,
the entries in payers.json.
"""
import json
import os
from pathlib import Path
from typing import Any

# The shipped default User-Agent. Nominatim's free usage policy rejects placeholder/
# templated agents (HTTP 403), so this only matters for the optional Nominatim
# fallback — geocoding works out of the box via the Census source regardless.
_DEFAULT_UA = "InNetwork/3.1 self-hosted (single-user; set INNETWORK_UA with your email)"


class Settings:
    def __init__(self) -> None:
        # SQLite file holding the Medicare index + geocode cache. Keep it on a
        # persistent volume in production so both survive restarts.
        self.db_path = os.environ.get("INNETWORK_DB", "./innetwork.db")

        # Comma-separated list of origins allowed to call the API. Empty -> "*"
        # (fine for local dev; set this to your real frontend origin in prod).
        origins = os.environ.get("ALLOWED_ORIGINS", "").strip()
        self.allowed_origins = [o.strip() for o in origins.split(",") if o.strip()]

        # Nominatim and NPPES both ask callers to identify themselves. Put a real
        # contact in INNETWORK_UA before pointing this at the live services.
        # Nominatim/NPPES ask callers to identify themselves with a real contact.
        # Set INNETWORK_UA to your own email before any heavy use — the public
        # OpenStreetMap geocoder blocks placeholder/templated user-agents (HTTP 403).
        self.contact_ua = os.environ.get("INNETWORK_UA", _DEFAULT_UA)
        # True when INNETWORK_UA is still the shipped placeholder — i.e. the Nominatim
        # fallback won't work (403). Geocoding still works via Census; used only to
        # decide whether to warn at startup.
        self.ua_is_placeholder = self.contact_ua == _DEFAULT_UA

        # Primary geocoder: the free, keyless, US-only Census Geocoder. Set
        # GEOCODE_USE_CENSUS=false to force the Nominatim-only path (needs INNETWORK_UA).
        self.geocode_use_census = os.environ.get(
            "GEOCODE_USE_CENSUS", "true"
        ).strip().lower() in ("1", "true", "yes", "on")
        self.census_base = os.environ.get(
            "CENSUS_GEOCODER_BASE", "https://geocoding.geo.census.gov"
        ).rstrip("/")

        self.nominatim_base = os.environ.get(
            "NOMINATIM_BASE", "https://nominatim.openstreetmap.org"
        ).rstrip("/")
        self.nppes_base = os.environ.get(
            "NPPES_BASE", "https://npiregistry.cms.hhs.gov/api/"
        )

        # Where to find configured commercial payers (see payers.example.json).
        self.payers_file = os.environ.get("INNETWORK_PAYERS", "payers.json")

        # Per-payer Transparency-in-Coverage source URLs for the scheduled ingest job
        # (see tic_sources.example.json). Maps a catalog payer id -> its published
        # in-network file URL, so a monthly cron can refresh every payer in one run.
        self.tic_sources_file = os.environ.get("INNETWORK_TIC_SOURCES", "tic_sources.json")

        # FHIR Plan-Net result cache TTLs (seconds). A definite answer (in-network /
        # not-found) is stable, so it's cached for a day; an "unknown" (the payer's
        # endpoint errored/timed out) is cached only briefly so a recovered endpoint
        # is retried soon rather than pinned as unknown. Never is "unknown" read as a
        # "no" — see app/insurance.py.
        self.fhir_cache_ttl = int(os.environ.get("INNETWORK_FHIR_CACHE_TTL", str(24 * 3600)))
        self.fhir_cache_unknown_ttl = int(os.environ.get("INNETWORK_FHIR_CACHE_UNKNOWN_TTL", "600"))

        # Short TTL for the NPPES search-result cache (C4). Kept brief because the public
        # registry changes and a search is interactive; long enough to absorb pagination
        # and repeat queries within a session.
        self.nppes_cache_ttl = int(os.environ.get("INNETWORK_NPPES_CACHE_TTL", "120"))

        # How strict the FHIR Plan-Net "in-network" determination is.
        #   "network"  (default) — a Confirmed answer requires an *active*
        #       PractitionerRole that links to a network. A role that is listed but
        #       carries no resolvable network reference is "unknown" (None), never a
        #       fabricated yes. This is the trust-preserving default.
        #   "directory" — looser: an active PractitionerRole counts as in-network even
        #       without a network link (directory presence). Opt-in only, for payers
        #       whose published directory is known not to populate network references.
        self.fhir_strictness = os.environ.get(
            "INNETWORK_FHIR_STRICTNESS", "network"
        ).strip().lower()
        if self.fhir_strictness not in ("network", "directory"):
            self.fhir_strictness = "network"

        # Hard ceiling on a single ingest download (TiC / Medicare). Remote files
        # are streamed and aborted the moment they exceed this, so a hostile or
        # mistyped URL can't OOM the box by being read fully into memory. Default
        # 2 GiB — well above any real single-payer file, far below "exhaust RAM".
        self.ingest_max_bytes = int(os.environ.get("INNETWORK_INGEST_MAX_BYTES", str(2 * 1024**3)))

        # Polite minimum seconds between live Nominatim requests (their usage
        # policy is max 1 req/sec). The cache means we rarely hit this.
        self.geocode_min_interval = float(os.environ.get("GEOCODE_MIN_INTERVAL", "1.0"))

        # Per-client rate limit on /api/* (requests per window, window seconds).
        # Protects the upstream registries from abuse via this open proxy. 0 = off.
        self.rate_limit_max = int(os.environ.get("RATE_LIMIT_MAX", "60"))
        self.rate_limit_window = int(os.environ.get("RATE_LIMIT_WINDOW", "60"))

        # When deployed behind a trusted reverse proxy (the documented Caddy setup),
        # the real client IP arrives in X-Forwarded-For; without this the limiter sees
        # only the proxy's IP and collapses into one global bucket. Enable ONLY behind
        # a proxy you control — otherwise a client could spoof the header to dodge the
        # limit. The provided docker-compose sets this for the Caddy front-end.
        self.trust_proxy = os.environ.get("INNETWORK_TRUST_PROXY", "false").strip().lower() in (
            "1", "true", "yes", "on",
        )

        # Scale-readiness seams (see app/interfaces.py). Each external dependency sits
        # behind a Protocol so scaling is a config swap, not a rewrite. Defaults are the
        # $0 self-hosted implementations; alternates (e.g. Redis, Postgres) are wired in
        # D4 and selected here without touching call sites.
        self.datastore = os.environ.get("INNETWORK_DATASTORE", "sqlite").strip().lower()
        self.rate_limiter = os.environ.get("INNETWORK_RATE_LIMITER", "memory").strip().lower()
        self.cache_backend = os.environ.get("INNETWORK_CACHE", "memory").strip().lower()

        # Wire the validated public FHIR Plan-Net endpoints (app/planet_registry.py) as
        # verified filters out of the box. On by default so a fresh clone gets verified
        # coverage with zero config; tests turn it off for hermeticity and opt in.
        self.use_planet_registry = os.environ.get(
            "INNETWORK_USE_PLANET_REGISTRY", "true"
        ).strip().lower() in ("1", "true", "yes", "on")

        # Harvested membership bitmaps (app/membership.py) — the rebuilt verified tier:
        # each payer's in-network NPI set as a local Roaring bitmap, mmap'd read-only and
        # answered instantly (no live per-NPI calls). `membership_dir` holds manifest.json
        # + the per-payer .roaring blobs. On by default so a fresh clone gets instant,
        # always-on verified coverage; tests point it at an empty dir for hermeticity.
        self.use_membership = os.environ.get(
            "INNETWORK_USE_MEMBERSHIP", "true"
        ).strip().lower() in ("1", "true", "yes", "on")
        self.membership_dir = os.environ.get("INNETWORK_MEMBERSHIP_DIR", "payers")
        # How many decoded payer bitmaps stay resident. Bitmaps are decoded lazily, on
        # first membership test, and the least-recently-used are evicted past this cap —
        # which is what lets the store hold THOUSANDS of plan-level payers (Rail 4) on a
        # serverless function: a single search touches only the handful of payers that
        # serve the searched state, so the rest are never decoded at all. Raise it to
        # trade memory for fewer re-decodes on a long-lived server.
        self.membership_max_resident = int(
            os.environ.get("INNETWORK_MEMBERSHIP_MAX_RESIDENT", "64"))

        # When the same process serves BOTH the page and the API (e.g. the Vercel
        # serverless deploy), serve innetwork.config.js with apiBase rewritten to the
        # request's own origin — so a fresh deploy works on first load with no
        # configure_frontend step. Off by default (a separately-hosted frontend keeps the
        # apiBase baked by configure_frontend.py).
        self.same_origin_frontend = os.environ.get(
            "INNETWORK_SAME_ORIGIN", "false"
        ).strip().lower() in ("1", "true", "yes", "on")

        # Data-age SLOs (C3). A source whose last ingest is older than its budget is
        # "stale" and flips /healthz to 503 — a dead-man's-switch for a stalled ingest.
        # Medicare refreshes quarterly (~92d; allow slack); TiC payers monthly.
        self.medicare_max_age_days = int(os.environ.get("INNETWORK_MEDICARE_MAX_AGE_DAYS", "100"))
        self.payer_max_age_days = int(os.environ.get("INNETWORK_PAYER_MAX_AGE_DAYS", "35"))

        # Token guarding POST /admin/ingest, which the scheduled ingest cron calls to
        # refresh the deployed instance. Unset -> the admin endpoint is disabled (404).
        self.admin_token = os.environ.get("INNETWORK_ADMIN_TOKEN", "").strip()
        # Direct URL to the CMS Medicare enrollment CSV, used by the admin "medicare"
        # ingest trigger. Unset -> that trigger reports it's unconfigured (no guessing).
        self.medicare_ingest_url = os.environ.get("INNETWORK_MEDICARE_INGEST_URL", "").strip()

    def load_payers(self) -> list[dict[str, Any]]:
        """Read payers.json -> list of payer config dicts. Missing file is fine."""
        path = Path(self.payers_file)
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return []
        if isinstance(data, dict):
            return data.get("payers", []) or []
        return data if isinstance(data, list) else []


settings = Settings()

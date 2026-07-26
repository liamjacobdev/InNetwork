"""Rail 4 — Marketplace (QHP) machine-readable provider-directory harvester.

Every Federally-Facilitated-Exchange issuer publishes, under 45 CFR 156.221(i), a public
`providers.json` that states — per provider, **per plan** — that the provider is in that
plan's network. `app/marketplace_registry.py` supplies the issuer index URLs; this module
turns them into membership bitmaps.

Why this rail earns the strongest badge in the app: TiC (Rail 2) is payer-level — a hit
means "listed somewhere in Aetna's network file", so the UI must still say *confirm your
specific plan*. This is **plan-level**: the assertion is about HIOS plan `73836AK0930001`,
not about an issuer in general. That is the same tier as the Medicare enrollment file, so
entries are written with `level="plan"`.

Three things measured on the live PY2026 corpus (2026-07-24) that shaped this code — each
one would have been a bug if assumed instead:

  1. **`network_tier` is free text, and one value means the opposite of in-network.**
     Sampling 18 issuers turned up 25+ distinct tiers: `PREFERRED`, `PREFERREDTIER`,
     `STANDARDTIER`, `IN-NETWORK`, named third-party networks (`CX--CONNECTION-DENTAL--PPO-USA`,
     `CS--AETNA`), and — critically — `OUT-OF-NETWORK`. So the rule here is a **denylist,
     not a whitelist**: being listed under a plan IS the issuer's assertion of network
     participation, and only an explicitly out-of-network tier is excluded. A whitelist of
     "PREFERRED" would have silently discarded most legitimate networks; admitting
     everything would have imported providers the issuer explicitly flagged as
     out-of-network — a fabricated "yes", the one thing this codebase never does.

  2. **A `providers.json` is not one issuer.** Moda's Alaska file carries Delta Dental's
     plan ids alongside its own. So plans are grouped by `plan_id`, and the issuer + state
     are derived from the HIOS plan id itself (`73836` + `AK`), never from the PUF row that
     led us to the file. A plan id that isn't HIOS-shaped can't be attributed or
     state-scoped, so it is skipped rather than guessed at.

  3. **Plans share networks — 13 plans collapsed to 3 distinct NPI sets (4.3x).** Each plan
     stays its own catalog entry (it is a real, separately-purchasable plan), but the blob
     is content-addressed and shared via `membership.write_payer(file_name=...)`.

The trust gate, translated to a file rail. There is no live endpoint here to round-trip,
so the two-way check of `app/verify_payers.py` becomes four machine-checked conditions,
ALL enforced before a single byte is written (`gate_network`):

  • **Luhn** — every NPI passes `membership.encode` (the check digit), so a TIN or a
    truncated id in the NPI slot can't fabricate a "yes".
  • **Completeness** — if any advertised `provider_urls` entry fails to read, the issuer
    writes NOTHING and keeps its last-good bitmaps. A missing file would silently turn its
    providers into a fabricated "no".
  • **Discrimination** (the bogus-NPI analog) — a plan network that contains an
    implausible share of the entire national provider universe isn't discriminating; it is
    a directory dump that would mark everyone in-network. Rejected.
  • **Positive control** (the real-NPI analog) — sampled NPIs from the harvested set must
    resolve to active providers in the CMS NPPES registry. A set of ghost identifiers is
    not a network.

    python -m app.marketplace_registry --refresh
    python -m app.harvest_marketplace --max-issuers 3
    python -m app.harvest_marketplace --index-url https://www.modahealth.com/cms-data-index.json
"""
from __future__ import annotations

import argparse
import codecs
import hashlib
import io
import json
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any, cast

import httpx
import ijson
from pyroaring import BitMap

from . import marketplace_registry, membership
from .config import settings
from .harvest_tic import open_json_stream
from .marketplace_registry import IssuerSource

# A HIOS plan id is <5-digit issuer><2-letter state><7-digit product/component>. It is the
# only self-describing thing in the file: it tells us who the issuer is and which state's
# exchange the plan is sold on, without trusting the path we arrived by.
HIOS_RE = re.compile(r"^(\d{5})([A-Z]{2})(\d{7})$")

# Tiers that explicitly denote NOT in-network. Compared after normalization (uppercased,
# non-alphanumerics stripped), so "Out-of-Network", "OUT_OF_NETWORK" and "out of network"
# all collapse to the same token. Deliberately small: anything not provably out-of-network
# is left in, because the issuer chose to list that provider under that plan.
_TIER_DENY = frozenset({
    "OUTOFNETWORK", "OON", "NONPARTICIPATING", "NONPAR", "NOTCONTRACTED",
    "OUTOFPLAN", "NONNETWORK", "NONCONTRACTED",
})

# Discrimination ceiling. A single QHP network cannot plausibly contain most of the
# country's providers; a file that claims so is a dump, not a network. Expressed as a
# fraction of the national Medicare-enrolled set (2.56M NPIs) because that is a real,
# already-loaded national denominator. Week 4's local NPPES index replaces this with the
# sharper per-state test the plan calls for.
DISCRIMINATION_MAX_FRACTION = 0.60
_NATIONAL_NPI_FALLBACK = 2_556_661

# Positive control: how many NPIs to round-trip against NPPES per distinct network, and how
# many must resolve. Kept small because it runs once per NETWORK (not per plan) — a handful
# of requests per issuer, not thousands.
POSITIVE_CONTROL_SAMPLE = 5
POSITIVE_CONTROL_MIN_HITS = 3


@dataclass
class MarketplaceStats:
    """One issuer's harvest outcome. `complete` is the write gate — it mirrors
    `TicStats.complete`: any hole means a partial view that must never be served as if it
    were the whole network."""
    index_url: str = ""
    provider_files: int = 0            # provider_urls successfully streamed
    records: int = 0                   # provider records seen
    plan_rows: int = 0                 # (provider, plan) pairs seen
    admitted: int = 0                  # (provider, plan) pairs admitted
    rejected_luhn: int = 0             # NPI failed the check digit
    rejected_tier: int = 0             # explicitly out-of-network tier
    rejected_year: int = 0             # plan row for a different plan year
    skipped_non_hios: int = 0          # plan id we cannot attribute or state-scope
    plans: int = 0                     # distinct plan ids collected
    networks: int = 0                  # distinct NPI sets after dedupe
    gate_rejected: list[str] = field(default_factory=list)   # "<network>: <why>" — TRUST
    label_warnings: list[str] = field(default_factory=list)  # cosmetic: names unavailable
    failures: list[str] = field(default_factory=list)        # "<src>: <why>" per hole
    error: str | None = None

    @property
    def complete(self) -> bool:
        return self.error is None and not self.failures


def norm_tier(value: Any) -> str:
    """Normalize a `network_tier` for comparison: uppercase, alphanumerics only."""
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


def tier_is_out_of_network(value: Any) -> bool:
    """True iff the tier explicitly says this listing is NOT in-network.

    Note the asymmetry, and that it is deliberate: an unrecognized tier is treated as
    in-network because the provider's presence under the plan is itself the issuer's
    assertion. Only an explicit denial overrides that.
    """
    return norm_tier(value) in _TIER_DENY


def split_hios(plan_id: str) -> tuple[str, str] | None:
    """(issuer_id, state) from a HIOS plan id, or None if it isn't HIOS-shaped."""
    m = HIOS_RE.match((plan_id or "").strip().upper())
    return (m.group(1), m.group(2)) if m else None


def _plan_applies(plan: dict[str, Any], plan_year: int) -> bool:
    """Whether a plan row covers `plan_year`. Rows carry a `years` array; a row without one
    is assumed current (the field is optional in the schema), a row with one must include
    the target year — otherwise last year's network leaks in as if it were live."""
    years = plan.get("years")
    if not isinstance(years, list) or not years:
        return True
    return any(str(y).strip() == str(plan_year) for y in years)


def stream_plan_npis(stream: IO[bytes], plan_year: int, stats: MarketplaceStats,
                     into: dict[str, BitMap] | None = None) -> dict[str, BitMap]:
    """Event-parse one `providers.json` into {plan_id: BitMap of in-network NPIs}.

    The file is a top-level JSON array (verified across 18 issuers), so `ijson.items(...,
    "item")` yields one provider record at a time and memory stays flat — the largest file
    measured is 345 MB and the corpus is ~59 GB. NPIs are accumulated straight into Roaring
    bitmaps rather than Python sets: a 100k-NPI network costs ~200 KB instead of ~7 MB.
    """
    out = into if into is not None else {}
    for rec in ijson.items(stream, "item"):
        if not isinstance(rec, dict):
            continue
        stats.records += 1
        key = membership.encode(str(rec.get("npi") or "").strip())
        plans = rec.get("plans") or []
        if not isinstance(plans, list):
            continue
        for plan in plans:
            if not isinstance(plan, dict):
                continue
            stats.plan_rows += 1
            if not _plan_applies(plan, plan_year):
                stats.rejected_year += 1
                continue
            if tier_is_out_of_network(plan.get("network_tier")):
                stats.rejected_tier += 1
                continue
            plan_id = str(plan.get("plan_id") or "").strip().upper()
            if split_hios(plan_id) is None:
                stats.skipped_non_hios += 1
                continue
            # Luhn is checked after the cheap filters so the reject count reflects rows we
            # actually wanted to admit.
            if key is None:
                stats.rejected_luhn += 1
                continue
            out.setdefault(plan_id, BitMap()).add(key)
            stats.admitted += 1
    return out


def _is_encoding_error(exc: BaseException) -> bool:
    """Whether a parse failure looks like invalid UTF-8 rather than malformed JSON.

    ijson's C backend reports this as a lexical error naming the bad bytes, so it is
    matched on the message; a genuine structural error must NOT trigger the re-read,
    because re-parsing broken JSON leniently would be a way to admit garbage.
    """
    if isinstance(exc, UnicodeDecodeError):
        return True
    text = str(exc).lower()
    return "invalid bytes in utf8" in text or "utf8 string" in text


class Utf8Sanitizer(io.RawIOBase):
    """A read-only byte stream that repairs invalid UTF-8 on the fly.

    Real case, hit on a live issuer: a provider's name carries the cp1252 byte 0x82
    ("Blas\\x82") inside a file served as JSON. `ijson` is strict, so that single byte
    aborts the parse — and the completeness guard then correctly refuses to write, costing
    the ENTIRE issuer's coverage over one accented letter.

    So invalid sequences are replaced (U+FFFD) rather than fatal. The trade is deliberate
    and safe: the damage lands only in free-text fields we do not store, while the NPIs,
    plan ids, and tiers this rail actually harvests are ASCII and pass through untouched.
    Losing a whole issuer to fix a character we discard would be the worse bargain.

    An incremental decoder is used so a multi-byte character split across two reads is
    still decoded correctly instead of being mangled at the seam.
    """

    def __init__(self, raw: IO[bytes], chunk: int = 1 << 20) -> None:
        self._raw = raw
        self._chunk = chunk
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._buf = b""
        self._eof = False

    def readable(self) -> bool:
        return True

    def readinto(self, b: Any) -> int:  # noqa: ANN401 - buffer protocol
        want = len(b)
        while len(self._buf) < want and not self._eof:
            data = self._raw.read(self._chunk)
            if not data:
                self._eof = True
                # final=True flushes any dangling partial sequence as U+FFFD.
                self._buf += self._decoder.decode(b"", True).encode("utf-8")
                break
            self._buf += self._decoder.decode(data).encode("utf-8")
        take = self._buf[:want]
        self._buf = self._buf[len(take):]
        b[:len(take)] = take
        return len(take)


def _decode_json_bytes(raw: bytes) -> str:
    """Decode an issuer JSON body that isn't the UTF-8 it claims to be.

    Real case: BCBS Michigan serves `plans.json` as `application/json` containing the
    cp1252 byte 0xAE — the ® in "Blue Cross® Premier PPO". Decoding with
    `errors="replace"` would "work" while putting U+FFFD into a plan name a user reads, so
    the ladder tries the encodings that actually round-trip first and only degrades if
    every one of them fails.
    """
    for enc in ("utf-8", "utf-8-sig", "cp1252", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def fetch_plan_names(index: dict[str, Any], client: httpx.Client,
                     stats: MarketplaceStats) -> dict[str, str]:
    """{plan_id: marketing name} from the issuer's `plans.json` files.

    Worth the extra fetch: without it the catalog would list 14-digit HIOS ids, which is
    unusable in a picker. These files are small. A plans.json that fails to load is NOT a
    completeness failure — a missing label degrades to the plan id, it never affects who is
    in the network.
    """
    names: dict[str, str] = {}
    for url in (index.get("plan_urls") or []):
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            continue
        try:
            r = client.get(url, timeout=60)
            r.raise_for_status()
            try:
                data = r.json()
            except UnicodeDecodeError:
                data = json.loads(_decode_json_bytes(r.content))
        except Exception as e:  # noqa: BLE001 - labels are cosmetic; never fail the harvest
            stats.label_warnings.append(f"{url}: plan names unavailable ({type(e).__name__})")
            continue
        for p in (data if isinstance(data, list) else []):
            if not isinstance(p, dict):
                continue
            pid = str(p.get("plan_id") or "").strip().upper()
            label = str(p.get("marketing_name") or "").strip()
            if pid and label:
                names[pid] = label
    return names


def harvest_index(index_url: str, client: httpx.Client, *, plan_year: int,
                  max_files: int | None = None) -> tuple[dict[str, BitMap], dict[str, str],
                                                         MarketplaceStats]:
    """Harvest every `providers.json` under one issuer index into per-plan bitmaps.

    A `max_files` cap makes this a probe: it records a failure so `complete` is False and
    the caller writes nothing — the same convention Rail 2 uses, so a recon run can never
    be mistaken for a real harvest.
    """
    stats = MarketplaceStats(index_url=index_url)
    plans: dict[str, BitMap] = {}
    names: dict[str, str] = {}
    try:
        index = marketplace_registry.fetch_index(index_url, client)
    except Exception as e:  # noqa: BLE001
        stats.error = f"index unreadable: {type(e).__name__}: {e}"
        return plans, names, stats

    urls = marketplace_registry.provider_urls(index)
    if not urls:
        stats.error = "index.json advertises no provider_urls"
        return plans, names, stats

    names = fetch_plan_names(index, client, stats)

    for i, url in enumerate(urls):
        if max_files is not None and i >= max_files:
            stats.failures.append(f"{index_url}: probe cap of {max_files} file(s) hit "
                                  f"({len(urls)} advertised) — partial by construction")
            break
        # Strict first: the great majority of issuer files are valid UTF-8 and pay nothing
        # for the sanitizer. Only a file that actually trips the decoder is re-read through
        # it, so one bad byte costs a second fetch instead of the whole issuer.
        for sanitize in (False, True):
            closers: list[Any] = []
            try:
                stream, closers = open_json_stream(url)
                src: IO[bytes] = cast("IO[bytes]", Utf8Sanitizer(stream)) if sanitize else stream
                before = dict(plans)
                stream_plan_npis(src, plan_year, stats, into=plans)
                stats.provider_files += 1
                break
            except Exception as e:  # noqa: BLE001 - one hole is enough to block the write
                if not sanitize and _is_encoding_error(e):
                    # Roll back the partial read so the retry can't double-count, then
                    # re-read the file through the sanitizer.
                    plans.clear()
                    plans.update(before)
                    continue
                stats.failures.append(f"{url}: {type(e).__name__}: {e}")
                break
            finally:
                for c in closers:
                    try:
                        c.close()
                    except Exception:  # noqa: BLE001
                        pass

    stats.plans = len(plans)
    return plans, names, stats


# ── The trust gate ────────────────────────────────────────────────────────────
def _national_npi_denominator(root: Path) -> int:
    """A national provider count to measure a network against — the Medicare bitmap's
    cardinality when present (a real, verified national set), else its known size."""
    try:
        data = json.loads((Path(root) / membership.MANIFEST_NAME).read_text(encoding="utf-8"))
        n = int(((data.get("payers") or {}).get("medicare") or {}).get("count") or 0)
        if n > 0:
            return n
    except Exception:  # noqa: BLE001
        pass
    return _NATIONAL_NPI_FALLBACK


def check_discrimination(bitmap: BitMap, denominator: int) -> str | None:
    """The bogus-NPI analog: reject a "network" that is really a directory dump.

    Returns a rejection reason, or None if the set discriminates. A QHP network sold in a
    handful of states cannot contain most of the nation's providers; a file that says so
    would mark effectively everyone in-network — the exact fabricated "yes" the live rail's
    bogus-NPI probe exists to catch.
    """
    if denominator <= 0:
        return None
    share = len(bitmap) / denominator
    if share > DISCRIMINATION_MAX_FRACTION:
        return (f"covers {share:.0%} of the national provider set "
                f"({len(bitmap):,}/{denominator:,}) — does not discriminate")
    return None


def check_positive_control(bitmap: BitMap, client: httpx.Client, *,
                           sample: int = POSITIVE_CONTROL_SAMPLE,
                           min_hits: int = POSITIVE_CONTROL_MIN_HITS) -> str | None:
    """The real-NPI analog: sampled members must exist in the CMS NPPES registry.

    A network of identifiers that no registry recognizes is not a network — it is
    corrupt or synthetic data, and admitting it would fabricate "yes" for NPIs that don't
    describe a real provider. Samples are spread across the bitmap rather than taken from
    the front, so a file whose first records are fine but whose tail is garbage still
    fails. A sample smaller than `min_hits` (a genuinely tiny network) is not gated.
    """
    n = len(bitmap)
    if n == 0:
        return "empty network"
    if n < min_hits:
        return None
    step = max(1, n // sample)
    picks: list[str] = []
    for i, key in enumerate(bitmap):
        if i % step == 0:
            picks.append(str(key + membership.OFFSET))
        if len(picks) >= sample:
            break

    hits = 0
    for npi in picks:
        try:
            r = client.get(settings.nppes_base,
                           params={"version": "2.1", "number": npi, "limit": 1},
                           timeout=25)
            r.raise_for_status()
            data = r.json()
            if isinstance(data, dict) and (data.get("results") or []):
                hits += 1
        except Exception:  # noqa: BLE001 - an unreachable registry is not a data verdict
            return None
    if hits < min_hits:
        return f"positive control failed — only {hits}/{len(picks)} sampled NPIs exist in NPPES"
    return None


def gate_network(bitmap: BitMap, *, denominator: int,
                 client: httpx.Client | None) -> str | None:
    """Run every per-network gate condition. Returns the first rejection reason, or None.

    (The Luhn condition is enforced upstream in `stream_plan_npis` via `membership.encode`,
    and completeness is enforced per-issuer by `MarketplaceStats.complete` — those two
    cannot be expressed as a function of a finished bitmap.)
    """
    reason = check_discrimination(bitmap, denominator)
    if reason:
        return reason
    if client is not None:
        return check_positive_control(bitmap, client)
    return None


# ── Write side ────────────────────────────────────────────────────────────────
def _signature(bitmap: BitMap) -> str:
    """A stable content hash of the NPI set, for deduping plans onto one shared blob."""
    return hashlib.sha256(bitmap.serialize()).hexdigest()[:16]


def write_plans(root: Path, plans: dict[str, BitMap], names: dict[str, str], *,
                index_url: str, plan_year: int, stats: MarketplaceStats,
                client: httpx.Client | None) -> list[membership.ManifestEntry]:
    """Gate, dedupe, and write one issuer's plans.

    Each distinct NPI set is gated ONCE and written ONCE (content-addressed under
    `mrf/<sig>.roaring`); every plan sharing that set gets its own manifest entry pointing
    at the shared blob. A network that fails the gate takes all of its plans with it and is
    recorded in `stats.gate_rejected` — nothing partial, nothing silent.
    """
    denominator = _national_npi_denominator(root)
    by_sig: dict[str, list[str]] = {}
    sig_map: dict[str, BitMap] = {}
    for plan_id, bm in plans.items():
        sig = _signature(bm)
        by_sig.setdefault(sig, []).append(plan_id)
        sig_map[sig] = bm
    stats.networks = len(by_sig)

    written: list[membership.ManifestEntry] = []
    now = time.time()
    for sig, plan_ids in sorted(by_sig.items()):
        bm = sig_map[sig]
        reason = gate_network(bm, denominator=denominator, client=client)
        if reason:
            stats.gate_rejected.append(f"network {sig} ({len(plan_ids)} plan(s)): {reason}")
            continue
        fname = f"mrf/{sig}.roaring"
        for plan_id in sorted(plan_ids):
            hios = split_hios(plan_id)
            if hios is None:            # unreachable: filtered upstream, kept defensive
                continue
            issuer_id, state = hios
            label = names.get(plan_id) or f"Marketplace plan {plan_id}"
            entry = membership.write_payer(
                root,
                id=plan_id.lower(),
                label=f"{label} ({state})",
                category="marketplace",
                level="plan",           # a per-plan assertion — the strongest tier
                method="marketplace-mrf",
                source_url=index_url,
                states=[state],
                bitmap=bm,
                fetched_at=now,
                max_age_days=settings.payer_max_age_days,
                file_name=fname,
            )
            written.append(entry)
    return written


def harvest_issuer(index_url: str, root: Path, client: httpx.Client, *, plan_year: int,
                   max_files: int | None = None, dry_run: bool = False,
                   gate_client: httpx.Client | None = None,
                   ) -> tuple[list[membership.ManifestEntry], MarketplaceStats]:
    """Harvest one issuer index end to end, writing only a COMPLETE, gated result."""
    plans, names, stats = harvest_index(index_url, client, plan_year=plan_year,
                                        max_files=max_files)
    if not plans:
        if stats.error is None:
            stats.error = "no in-network plan rows found"
        return [], stats
    if not stats.complete:
        return [], stats            # completeness guard: partial view is never written
    if dry_run:
        stats.networks = len({_signature(b) for b in plans.values()})
        return [], stats
    written = write_plans(root, plans, names, index_url=index_url, plan_year=plan_year,
                          stats=stats, client=gate_client)
    return written, stats


def _report(index_url: str, entries: list[membership.ManifestEntry],
            stats: MarketplaceStats) -> None:
    print(f"[{index_url}]", flush=True)
    print(f"    files={stats.provider_files} records={stats.records:,} "
          f"plan_rows={stats.plan_rows:,} admitted={stats.admitted:,}", flush=True)
    dedupe = f" (dedupe {stats.plans / stats.networks:.1f}x)" if stats.networks else ""
    print(f"    plans={stats.plans} networks={stats.networks}{dedupe}", flush=True)
    print(f"    rejected: luhn={stats.rejected_luhn:,} out-of-network-tier="
          f"{stats.rejected_tier:,} wrong-year={stats.rejected_year:,} "
          f"non-hios={stats.skipped_non_hios:,}", flush=True)
    for r in stats.gate_rejected:
        print(f"    GATE REJECTED {r}", flush=True)
    for w in stats.label_warnings:
        print(f"    label warning (cosmetic) {w}", flush=True)
    for f in stats.failures:
        print(f"    HOLE {f}", flush=True)
    if stats.error:
        print(f"    ERROR {stats.error}", flush=True)
    if entries:
        print(f"    wrote {len(entries)} plan entries over "
              f"{len({e.file for e in entries})} blob(s).", flush=True)
    elif not stats.complete:
        print("    INCOMPLETE — nothing written (last-good kept).", flush=True)


def _plan_year_from_sources(root: Path) -> int:
    """The plan year the committed registry was generated for, so the harvest and the
    source list can never drift apart silently."""
    path = Path(root) / marketplace_registry.SOURCES_FILE
    try:
        return int(json.loads(path.read_text(encoding="utf-8"))["plan_year"])
    except Exception:  # noqa: BLE001
        return marketplace_registry.DEFAULT_PLAN_YEAR


def main(argv: list[str]) -> None:
    ap = argparse.ArgumentParser(
        description="Harvest Marketplace (QHP) machine-readable provider directories into "
                    "plan-level membership bitmaps.")
    ap.add_argument("--index-url", default=None,
                    help="harvest a single issuer index.json (skips the registry)")
    ap.add_argument("--max-issuers", type=int, default=None,
                    help="harvest at most N issuer indexes (recon)")
    ap.add_argument("--max-files", type=int, default=None,
                    help="cap provider files per issuer — a PROBE; writes nothing")
    ap.add_argument("--plan-year", type=int, default=None,
                    help="plan year to keep (default: from marketplace_sources.json)")
    ap.add_argument("--state", default=None, help="only issuers filed under this state")
    ap.add_argument("--dry-run", action="store_true", help="harvest + report, write nothing")
    ap.add_argument("--root", default=None, help="membership dir (default: settings)")
    args = ap.parse_args(argv[1:])

    root = Path(args.root or settings.membership_dir)
    repo = Path(".")
    plan_year = args.plan_year or _plan_year_from_sources(repo)

    if args.index_url:
        targets = [args.index_url]
    else:
        sources: list[IssuerSource] = marketplace_registry.load_sources(repo)
        if not sources:
            raise SystemExit("No marketplace_sources.json — run "
                             "`python -m app.marketplace_registry --refresh` first.")
        if args.state:
            sources = [s for s in sources if s.state.upper() == args.state.upper()]
        # Issuers routinely share one index URL across states (108 URLs for 346 rows), so
        # fetch each URL once.
        targets = sorted({s.index_url for s in sources})
        if args.max_issuers is not None:
            targets = targets[:args.max_issuers]

    headers = {"User-Agent": settings.contact_ua, "Accept": "application/json"}
    n_issuers = n_written = n_incomplete = n_gated = 0
    blobs: set[str] = set()
    with httpx.Client(follow_redirects=True, headers=headers) as client, \
            httpx.Client(follow_redirects=True, headers=headers) as gate_client:
        for url in targets:
            entries, stats = harvest_issuer(
                url, root, client, plan_year=plan_year, max_files=args.max_files,
                dry_run=args.dry_run, gate_client=None if args.dry_run else gate_client)
            _report(url, entries, stats)
            n_issuers += 1
            n_written += len(entries)
            blobs |= {e.file for e in entries}
            n_incomplete += 0 if stats.complete else 1
            n_gated += len(stats.gate_rejected)

    print(f"\nRail 4 summary: {n_issuers} issuer index(es), "
          f"{n_written} plan entries over {len(blobs)} blob(s); "
          f"{n_incomplete} incomplete (nothing written), "
          f"{n_gated} gate rejection(s).", flush=True)


if __name__ == "__main__":
    main(sys.argv)

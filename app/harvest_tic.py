"""Rail 2 — streaming Transparency-in-Coverage (TiC) harvester.

Under the federal TiC rule (CMS-9915-F) every commercial plan publishes machine-readable
in-network files, public and login-free. This harvests the set of in-network NPIs for a
payer into a membership bitmap, so the payer becomes an instant local verified filter.

Two hard corrections over the old `app/ingest_tic.py` (which would silently harvest ~0
NPIs from most modern payers):

  1. STREAMING. TiC in-network files run to hundreds of GB. We never land the whole file:
     ijson event-parses the byte stream and plucks only `npi` arrays (and external
     `provider_references` locations), so memory stays flat regardless of file size.

  2. DEREFERENCE `provider_references`. Modern TiC files don't inline `provider_groups`
     under each rate — they factor provider groups into a top-level `provider_references`
     list, and many of those are EXTERNAL files referenced by `location`. The old parser
     only read inlined `provider_groups[].npi[]`, so it saw almost nothing. We collect NPIs
     from inlined groups AND follow every external `provider_references[].location`.

Trust: TiC membership is PAYER-network-level (`level="payer"`) — a hit means "listed in
the payer's in-network file", not "accepts your specific plan". Every NPI passes the Luhn
gate (app/npi.py), so a garbage identifier (a TIN in the NPI slot — the exact TiC failure
mode) can't fabricate a "yes". A table-of-contents root is auto-discovered and fanned out.

    python -m app.harvest_tic aetna https://.../aetna-index.json
    python -m app.harvest_tic cigna /path/to/in-network.json.gz
"""
from __future__ import annotations

import argparse
import gzip
import os
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any, cast

import httpx
import ijson
from pyroaring import BitMap

from . import membership, tic_index
from .catalog import PAYER_CATALOG
from .config import settings

_CATALOG_IDS = {e["id"] for e in PAYER_CATALOG}
_CATALOG = {e["id"]: e for e in PAYER_CATALOG}


@dataclass
class TicStats:
    files: int = 0                 # in-network + external ref files streamed
    external_refs: int = 0         # provider_references locations followed
    npi_values_seen: int = 0       # every npi token encountered (incl. dupes)
    rejected: int = 0              # failed the Luhn gate
    unique_npis: int = 0           # final bitmap cardinality
    failed_files: int = 0          # ToC children / external refs that couldn't be read
    failures: list[str] = field(default_factory=list)  # "<src>: <why>" per hole (+ probe cap)
    error: str | None = None       # a fatal error that aborted the whole harvest

    @property
    def complete(self) -> bool:
        """A harvest is complete only if nothing errored, no file failed, and no probe cap
        was hit — the write gate mirrors Rail 1: any hole means a partial fan-out that must
        NOT be served as complete (a missing in-network file would read as a fabricated no)."""
        return self.error is None and not self.failures


def _open_binary(src: str) -> tuple[IO[bytes], list[Any]]:
    """Open `src` (URL or path) as a seekable binary stream, transparently gunzipping.
    Returns (stream, closers). A URL streams to a disk-backed spool (bounded by
    settings.ingest_max_bytes) so even a huge file never lands in RAM."""
    closers: list[Any] = []
    if src.startswith(("http://", "https://")):
        from .download import stream_to_spool
        raw: IO[bytes] = stream_to_spool(src)  # rewound, seekable, disk-backed
    else:
        raw = open(src, "rb")
    closers.append(raw)
    head = raw.read(2)
    raw.seek(0)
    if head == b"\x1f\x8b" or src.endswith(".gz"):
        # mode="rb" is REQUIRED, not cosmetic: with no mode, GzipFile infers it from
        # `fileobj.mode`, and a spooled download (SpooledTemporaryFile, "w+b") makes it open
        # for WRITING -> every gzipped URL died with "read() on write-only GzipFile object".
        gz = gzip.GzipFile(fileobj=raw, mode="rb")
        closers.insert(0, gz)
        # GzipFile is a binary stream but typeshed doesn't type it as IO[bytes]; the cast
        # reflects the real contract (ijson reads it like any binary file object).
        return cast("IO[bytes]", gz), closers
    return raw, closers


# Public alias. Rail 4 (app/harvest_marketplace.py) opens issuer `providers.json` files
# under exactly this contract — remote-or-local, transparently gunzipped, seekable — so it
# shares this one implementation. That matters for the planned no-spool streaming change:
# there is one place to fix, not two.
open_json_stream = _open_binary


def _top_level_keys(stream: IO[bytes], limit: int = 8) -> list[str]:
    """The first few top-level object keys, to classify a file (ToC vs in-network) without
    reading it whole. Rewinds the stream afterward (callers pass a seekable stream)."""
    keys: list[str] = []
    for prefix, event, value in ijson.parse(stream):
        if event == "map_key" and prefix == "":
            keys.append(value)
            if len(keys) >= limit:
                break
    stream.seek(0)
    return keys


def _stream_npis(stream: IO[bytes], external_refs: list[str]) -> Iterator[str]:
    """Single streaming pass: yield every NPI token (from inlined provider_groups anywhere
    in the doc) and record every external `provider_references[].location` for follow-up.

    The `.npi.item` suffix matches an npi under BOTH `in_network[].negotiated_rates[]
    .provider_groups[]` and top-level `provider_references[].provider_groups[]`, and the
    root `provider_groups[]` of an external ref file — one rule covers every shape.
    """
    for prefix, event, value in ijson.parse(stream):
        if prefix.endswith(".npi.item") and event in ("number", "string"):
            # npi is usually an int in TiC; normalize both int and string forms.
            yield str(value) if event == "string" else str(int(value))
        elif event == "string" and prefix.endswith("provider_references.item.location"):
            if value:
                external_refs.append(value)


class TicHarvester:
    """Accumulates in-network NPIs directly into a Roaring bitmap (bounded memory even for
    millions of NPIs), following TiC indexes and external provider references."""

    def __init__(self, max_files: int | None = None) -> None:
        self.bitmap = BitMap()
        self.stats = TicStats()
        self._visited: set[str] = set()
        self.max_files = max_files       # recon cap on in-network files streamed (probe run)
        self._capped = False

    def _admit(self, npi: str) -> None:
        self.stats.npi_values_seen += 1
        v = membership.encode(npi)
        if v is None:
            self.stats.rejected += 1
        else:
            self.bitmap.add(v)   # idempotent — the bitmap is the dedup

    def harvest(self, src: str) -> None:
        """Harvest `src`, which may be a TiC table-of-contents (fanned out), an in-network
        file, or an external provider-reference file. Cycle-guarded via `_visited`.

        Resilient by design: a child/external file that fails to open or stream is recorded
        as a HOLE (`stats.failures`) and does NOT abort its siblings — so one run surfaces
        the whole failure picture, and the completeness guard then refuses to ship the
        partial set (a missing in-network file would make its providers read as "no")."""
        if src in self._visited or self._capped:
            return
        self._visited.add(src)
        child_srcs: list[str] = []
        external_after: list[str] = []
        try:
            stream, closers = _open_binary(src)
            try:
                keys = _top_level_keys(stream)
                if "reporting_structure" in keys:
                    # A table-of-contents. It's the small index file (not an in-network file),
                    # so reading it whole to discover the in-network URLs is fine; fan out.
                    refs = tic_index.parse_index(stream.read())
                    child_srcs = [r.location for r in refs]
                else:
                    # An in-network file (or external ref file). Honor the recon cap BEFORE
                    # streaming another file so a `--max-files` probe stays cheap; hitting it
                    # marks the run incomplete (a capped run must never write as complete).
                    if self.max_files is not None and self.stats.files >= self.max_files:
                        self._capped = True
                        self.stats.failures.append(
                            f"hit --max-files cap ({self.max_files}) — probe run, not complete")
                        return
                    self.stats.files += 1
                    for npi in _stream_npis(stream, external_after):
                        self._admit(npi)
            finally:
                for c in closers:
                    try:
                        c.close()
                    except Exception:
                        pass
        except Exception as e:  # noqa: BLE001 - one bad file is a hole, not a crash
            self.stats.failed_files += 1
            self.stats.failures.append(f"{src}: {type(e).__name__}: {e}")
            return
        # Fan out to ToC children, then to external provider-reference files.
        for child in child_srcs:
            self.harvest(child)
        for ext in external_after:
            self.stats.external_refs += 1
            self.harvest(_resolve_ref(src, ext))

    def finalize(self) -> TicStats:
        self.stats.unique_npis = len(self.bitmap)
        return self.stats


def _resolve_ref(parent: str, ref: str) -> str:
    """Resolve an external provider-reference location. Absolute URLs/paths pass through;
    a relative URL is resolved against its parent in-network file's URL."""
    if ref.startswith(("http://", "https://")) or Path(ref).is_absolute():
        return ref
    if parent.startswith(("http://", "https://")):
        from urllib.parse import urljoin
        return urljoin(parent, ref)
    return str(Path(parent).parent / ref)


def harvest_to_bitmap(payer_id: str, src: str, root: Path, *, complete_only: bool = True,
                      max_files: int | None = None) -> tuple[membership.ManifestEntry | None, TicStats]:
    """Harvest `payer_id` from TiC source `src` and write its membership bitmap (method
    "tic", level "payer"). Returns (entry, stats); entry is None (nothing written) when:

      • the harvest collected nothing (never overwrite a good bitmap with an empty one); or
      • `complete_only` and the fan-out was PARTIAL — a ToC child or external provider-ref
        file failed to read, a fatal error aborted the walk, or a `--max-files` probe cap was
        hit. This mirrors Rail 1's completeness guard: a partial in-network set served as
        complete would make providers in the missing files read as a fabricated "no", so a
        partial run keeps the last-good bitmap and lets `/healthz` staleness surface instead.
    """
    if payer_id not in _CATALOG_IDS:
        print(f"Warning: '{payer_id}' is not in app/catalog.py — it won't surface as a "
              f"named filter until added there.", flush=True)
    h = TicHarvester(max_files=max_files)
    try:
        h.harvest(src)
    except Exception as e:  # noqa: BLE001
        h.stats.error = f"{type(e).__name__}: {e}"
    stats = h.finalize()
    if stats.unique_npis == 0:
        return None, stats
    if complete_only and not stats.complete:
        return None, stats
    cat = _CATALOG.get(payer_id, {})
    entry = membership.write_payer(
        root,
        id=payer_id, label=cat.get("label", payer_id),
        category=cat.get("category", "commercial"),
        level="payer",                 # TiC = listed in the payer's in-network file
        method="tic", source_url=src,
        states=cat.get("states"), bitmap=h.bitmap,
        max_age_days=settings.payer_max_age_days,
    )
    return entry, stats


def _child_size(loc: str, client: httpx.Client) -> int:
    """Best-effort byte size of a ToC child, to RANK children largest-first. A HEAD's
    Content-Length for a URL, or the file size for a local path; 0 on failure (sorts last)."""
    if loc.startswith(("http://", "https://")):
        try:
            r = client.head(loc, timeout=30, follow_redirects=True)
            return int(r.headers.get("content-length") or 0)
        except Exception:
            return 0
    try:
        return os.path.getsize(loc)
    except OSError:
        return 0


@dataclass
class Convergence:
    """One file's contribution to the running union, so a human can SEE the payer's network
    saturate across plan files (the empirical completeness signal)."""
    file: str
    added: int
    total: int
    growth: float


def _stratified_pick(sized: list[tuple[int, str]], n: int) -> list[tuple[int, str]]:
    """Pick `n` files SPANNING the size distribution: sort by size descending, split into `n`
    equal-count bands, and take the largest file of each band.

    Why not simply the n largest: the biggest files are a payer's broadest networks, so a
    top-N sample can only ever show that the giants agree with each other. A provider unique
    to a NARROW plan (a small file) would never be sampled, would be absent from the union,
    and would then be served as a confident — and wrong — "not in network". Stratified bands
    keep the overall largest file (maximum coverage) while forcing small/narrow plans into
    the sample, which is precisely what makes the saturation verdict meaningful.
    """
    desc = sorted(sized, reverse=True)
    if n <= 0:
        return []
    if n >= len(desc):
        return desc
    return [desc[(i * len(desc)) // n] for i in range(n)]


def harvest_toc_sampled(
    payer_id: str, toc_src: str, root: Path, *, sample_n: int, plateau: float = 0.005,
    complete_only: bool = True,
) -> tuple[membership.ManifestEntry | None, TicStats, list[Convergence]]:
    """Harvest the UNION of `sample_n` in-network files sampled ACROSS SIZE BANDS from a TiC
    table-of-contents, and report whether the union saturated.

    Why this exists: a payer's full ToC can fan out to hundreds of multi-GB files (Aetna's
    national ToC ≈ 283 files ≈ 890 GB) — far too big for one job, AND a single plan file served
    as the payer's WHOLE network fabricates "no" for providers in its other plans. Payers
    largely repeat one provider network across plans, so a union of sampled plans converges on
    the complete national set.

    The sample is STRATIFIED, not the n largest (see `_stratified_pick`): the largest files are
    the broadest networks, so a top-N sample can only show that the giants agree with each
    other, leaving providers unique to a NARROW plan absent from the union and served as a
    confident, wrong "not in network". Bands force small plans into the sample, so the
    saturation verdict actually means something. There is deliberately NO early plateau-stop —
    quitting on the first dip would skip the very bands the claim rests on.

    Files stream sequentially so peak disk stays ~one file (runner-feasible), not the sum.
    Children above the download cap are skipped (they cannot be spooled). Writes only if EVERY
    file it pulled completed (completeness guard) — a hole keeps last-good.
    """
    if payer_id not in _CATALOG_IDS:
        print(f"Warning: '{payer_id}' is not in app/catalog.py — it won't surface as a "
              f"named filter until added there.", flush=True)
    h = TicHarvester()
    convergence: list[Convergence] = []
    with httpx.Client() as client:
        try:
            stream, closers = _open_binary(toc_src)
            try:
                refs = tic_index.parse_index(stream.read())
            finally:
                for c in closers:
                    try:
                        c.close()
                    except Exception:
                        pass
        except Exception as e:  # noqa: BLE001 - an unreadable ToC is a clean failure, not a crash
            h.stats.error = f"table-of-contents unreadable: {type(e).__name__}: {e}"
            return None, h.finalize(), convergence
        children = sorted({r.location for r in refs})
        if not children:
            h.stats.error = "table-of-contents listed no in-network files"
            return None, h.finalize(), convergence
        # Size every child once, then drop the ones that can't physically be downloaded:
        # the spool lands the whole gzip on disk, so a file above the byte cap would abort
        # with DownloadTooLarge and (being a "hole") block the write entirely. Aetna's
        # largest national file is ~8.5 GB vs a runner's ~14 GB disk, so the biggest files
        # are exactly the ones we must skip. We take the largest files that DO fit — the
        # union still converges, and the skip is logged loudly rather than hidden.
        sized = [(_child_size(u, client), u) for u in children]
        cap = settings.ingest_max_bytes
        too_big = [(s, u) for s, u in sized if s > cap]
        fits = sorted(((s, u) for s, u in sized if s <= cap), reverse=True)
        if too_big:
            print(f"[{payer_id}] skipping {len(too_big)} file(s) over the "
                  f"{cap / 1e9:.1f} GB download cap (largest {max(s for s, _ in too_big) / 1e9:.1f} GB) "
                  f"— they cannot be spooled on this runner; using the largest that fit.",
                  flush=True)
        if not fits:
            h.stats.error = (f"every in-network file exceeds the {cap / 1e9:.1f} GB cap "
                             f"(raise INNETWORK_INGEST_MAX_BYTES or stream without spooling)")
            return None, h.finalize(), convergence
        picks = _stratified_pick(fits, sample_n)
        print(f"[{payer_id}] sampling {len(picks)} of {len(fits)} eligible files across size "
              f"bands ({picks[0][0] / 1e9:.2f} GB … {picks[-1][0] / 1e9:.2f} GB) — spanning the "
              f"distribution so NARROW-network plans are tested, not just the biggest.",
              flush=True)
        for i, (sz, loc) in enumerate(picks, 1):
            before = len(h.bitmap)
            h.harvest(loc)                        # unions into h.bitmap; sequential -> low disk
            total = len(h.bitmap)
            added = total - before
            growth = (added / total) if total else 0.0
            convergence.append(Convergence(loc, added, total, growth))
            print(f"[{payer_id}] band {i}/{len(picks)} ({sz / 1e9:.2f} GB) +{added:,} -> "
                  f"{total:,} unique (+{growth * 100:.2f}%) "
                  f"failed_files={h.stats.failed_files}", flush=True)
            if h.stats.failed_files:
                break                             # a hole -> the guard will refuse the write
            # NOTE: deliberately no early plateau-stop here. Stopping as soon as growth dips
            # would quit before the small/narrow bands are sampled — exactly the files the
            # saturation claim depends on.
    stats = h.finalize()

    # Saturation verdict. Because the bands span the size distribution, "no band after the
    # first added materially" is real evidence the payer repeats ONE network across its plans
    # and the union is effectively its whole network — which is what makes a served, verified
    # "false" trustworthy rather than a fabricated "no". Reported either way, never hidden.
    later = [c.growth for c in convergence[1:]]
    if later:
        worst = max(later)
        if worst < plateau:
            print(f"[{payer_id}] SATURATED: no sampled band added more than {worst * 100:.2f}% "
                  f"(< {plateau * 100:.2f}%) across {len(convergence)} bands — the union is "
                  f"effectively the payer's whole network; verified 'not in network' answers "
                  f"are well-supported.", flush=True)
        else:
            print(f"[{payer_id}] NOT SATURATED: a sampled band added {worst * 100:.2f}% new "
                  f"NPIs (>= {plateau * 100:.2f}%). The network is NOT uniform across plans, so "
                  f"providers in unsampled plans may be missing and would read as a false "
                  f"'no' — raise the sample count before trusting negatives.", flush=True)

    if stats.unique_npis == 0:
        return None, stats, convergence
    if complete_only and not stats.complete:
        return None, stats, convergence
    cat = _CATALOG.get(payer_id, {})
    entry = membership.write_payer(
        root, id=payer_id, label=cat.get("label", payer_id),
        category=cat.get("category", "commercial"), level="payer",
        method="tic", source_url=toc_src, states=cat.get("states"),
        bitmap=h.bitmap, max_age_days=settings.payer_max_age_days,
    )
    return entry, stats, convergence


def main(argv: list[str]) -> None:
    ap = argparse.ArgumentParser(
        description="Streaming Transparency-in-Coverage harvest -> membership bitmap.")
    ap.add_argument("payer", help="catalog id (e.g. aetna, anthem) so the bitmap supersedes the estimate")
    ap.add_argument("src", help="TiC table-of-contents index URL/path, or a single in-network file")
    ap.add_argument("--max-files", type=int, default=None,
                    help="recon cap on in-network files streamed; hitting it marks the run "
                         "incomplete (a probe never writes a partial set as complete)")
    ap.add_argument("--toc-sample-files", "--toc-top-files", type=int, default=None,
                    metavar="N", dest="toc_sample_files",
                    help="treat src as a table-of-contents and union N in-network files sampled "
                         "ACROSS SIZE BANDS (largest + mid + narrow plans) — for giant ToCs that "
                         "don't fit a job (e.g. Aetna national). --toc-top-files is a legacy alias")
    ap.add_argument("--plateau", type=float, default=0.005,
                    help="saturation threshold: if no sampled band after the first adds this "
                         "fraction of new NPIs, the union is reported SATURATED "
                         "(default 0.005 = 0.5%%)")
    ap.add_argument("--allow-partial", action="store_true",
                    help="opt out of the completeness guard and write whatever was collected "
                         "(unsafe — a hole reads as a fabricated 'no'; for debugging only)")
    args = ap.parse_args(argv[1:])

    root = Path(settings.membership_dir)
    payer = args.payer
    if args.toc_sample_files is not None:
        entry, stats, convergence = harvest_toc_sampled(
            args.payer, args.src, root, sample_n=args.toc_sample_files, plateau=args.plateau,
            complete_only=not args.allow_partial)
    else:
        entry, stats = harvest_to_bitmap(args.payer, args.src, root,
                                         complete_only=not args.allow_partial, max_files=args.max_files)
    print(f"[{payer}] files={stats.files} external_refs={stats.external_refs} "
          f"npi_tokens={stats.npi_values_seen:,} rejected={stats.rejected:,} "
          f"unique={stats.unique_npis:,} failed_files={stats.failed_files}", flush=True)
    if stats.error:
        print(f"[{payer}] stopped early: {stats.error}", flush=True)
    for f in stats.failures[:10]:
        print(f"[{payer}]   hole: {f}", flush=True)
    if entry is not None:
        print(f"[{payer}] wrote {root / entry.file} ({entry.count:,} NPIs, "
              f"{entry.sha256[:12]}…).", flush=True)
    elif stats.unique_npis and not stats.complete:
        print(f"[{payer}] partial fan-out ({stats.unique_npis:,} NPIs but "
              f"{len(stats.failures)} hole(s)) — bitmap NOT written (kept last good; "
              f"staleness surfaces).", flush=True)
    else:
        print(f"[{payer}] harvest collected 0 NPIs — bitmap NOT written (kept last good).",
              flush=True)


if __name__ == "__main__":
    main(sys.argv)

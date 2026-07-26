"""Rail 4 source list — every Marketplace issuer's machine-readable index URL.

Under 45 CFR 156.221(i) every Qualified Health Plan issuer on a Federally-Facilitated
Exchange must publish a public, login-free `index.json` pointing at `providers.json`,
`plans.json`, and `drugs.json`. CMS publishes the master list of those index URLs as the
**Machine-Readable URL PUF** — one row per issuer, refreshed each plan year. That file is
the thing this module reads.

Why this rail matters more than the others: it is the only source that hands us a
*registry of payers* instead of one payer. Rails 1-3 grow by a human researching one
payer at a time; this grows by reading a government spreadsheet. Measured against the
live PY2026 file (2026-07-24): **346 issuer rows · 108 distinct index URLs · 30 states**.

Two deliberate choices:

  • **No new dependency.** The PUF is a 21 KB .xlsx — a zip of XML — so it is read with
    `zipfile` + `ElementTree` instead of pulling openpyxl into the Vercel function bundle,
    where every megabyte competes with payer bitmaps. The reader below handles exactly the
    cell shapes this file uses (shared strings, inline strings, raw numbers) and indexes
    cells by their column letter, so a sparse row can never shift a column.

  • **The Tech POC Email column is dropped, never persisted.** It is a real person's work
    address; we have no use for it and no business republishing it in this repo.

Columns are looked up **by header name**, not position, so CMS reordering the sheet is a
no-op rather than a silent data corruption.

    python -m app.marketplace_registry --refresh     # rewrite marketplace_sources.json
    python -m app.marketplace_registry --probe       # fetch each index.json, report health
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import asdict, dataclass
from io import BytesIO
from pathlib import Path
from typing import Any

import httpx

from .config import settings
from .download import stream_to_bytes

# The PUF lives at a stable, plan-year-templated path. A new plan year's file appears
# mid-year, so `resolve_latest_url` probes newer years first and falls back — one less
# thing to remember to bump by hand.
MRPUF_URL_TEMPLATE = "https://data.healthcare.gov/datafile/py{year}/machine_readable_PUF.xlsx"
DEFAULT_PLAN_YEAR = 2026

SOURCES_FILE = "marketplace_sources.json"

_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_COL_RE = re.compile(r"^([A-Z]+)")

# Header labels as CMS writes them, normalized (lowercased, non-alphanumerics stripped).
_H_STATE = "state"
_H_ISSUER = "issuerid"
_H_URL = "urlsubmitted"


@dataclass(frozen=True)
class IssuerSource:
    """One issuer's Marketplace machine-readable index. `state` is the exchange state the
    row was filed under — an issuer selling in five states appears as five rows sharing
    one `index_url`, which is why callers dedupe on the URL before fetching."""
    issuer_id: str
    state: str
    index_url: str


def _norm_header(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _col_letter(ref: str) -> str:
    """'BC12' -> 'BC'. The cell's column letter is the only reliable position key: a row
    that omits an empty cell would otherwise shift every value after it into the wrong
    column (and silently mis-file issuer ids as URLs)."""
    m = _COL_RE.match(ref or "")
    return m.group(1) if m else ""


def _shared_strings(z: zipfile.ZipFile) -> list[str]:
    try:
        raw = z.read("xl/sharedStrings.xml")
    except KeyError:
        return []
    root = ET.fromstring(raw)
    # A shared string may be split across several <t> runs (rich text); join them.
    return ["".join(t.text or "" for t in si.iter(_NS + "t")) for si in root.findall(_NS + "si")]


def _first_sheet_name(z: zipfile.ZipFile) -> str:
    """The first worksheet part. The PUF has exactly one sheet; resolving it from the
    archive rather than hardcoding 'sheet1.xml' keeps a re-exported file readable."""
    names = [n for n in z.namelist() if n.startswith("xl/worksheets/") and n.endswith(".xml")]
    if not names:
        raise ValueError("workbook contains no worksheet parts")
    return sorted(names)[0]


def read_xlsx_rows(data: bytes) -> list[dict[str, str]]:
    """Parse an .xlsx into a list of {column_letter: text} rows, in sheet order.

    Only the cell kinds this PUF actually uses are decoded: shared strings (`t="s"`),
    inline strings (`t="inlineStr"`), formula strings (`t="str"`), and bare numbers. An
    unrecognized cell yields "" rather than raising — a stray styled cell must not take
    down the whole harvest.
    """
    with zipfile.ZipFile(BytesIO(data)) as z:
        shared = _shared_strings(z)
        sheet = ET.fromstring(z.read(_first_sheet_name(z)))
    rows: list[dict[str, str]] = []
    for row in sheet.iter(_NS + "row"):
        cells: dict[str, str] = {}
        for c in row.findall(_NS + "c"):
            col = _col_letter(c.get("r", ""))
            if not col:
                continue
            kind = c.get("t")
            if kind == "inlineStr":
                is_el = c.find(_NS + "is")
                val = "".join(t.text or "" for t in is_el.iter(_NS + "t")) if is_el is not None else ""
            else:
                v = c.find(_NS + "v")
                text = (v.text or "") if v is not None else ""
                if kind == "s":
                    try:
                        val = shared[int(text)]
                    except (ValueError, IndexError):
                        val = ""
                else:
                    val = text
            cells[col] = val.strip()
        if cells:
            rows.append(cells)
    return rows


def parse_mrpuf(data: bytes) -> list[IssuerSource]:
    """Decode the MR-PUF workbook into issuer sources, keyed off the header row.

    Rows missing a state, issuer id, or an http(s) URL are skipped — the PUF is
    issuer-submitted and does carry the occasional blank or malformed row, and a source
    we can't address is not a source.
    """
    rows = read_xlsx_rows(data)
    if not rows:
        raise ValueError("MR-PUF contained no rows")
    header, body = rows[0], rows[1:]
    cols = {_norm_header(v): k for k, v in header.items()}
    missing = [h for h in (_H_STATE, _H_ISSUER, _H_URL) if h not in cols]
    if missing:
        raise ValueError(f"MR-PUF header missing expected column(s) {missing}; saw {list(header.values())}")

    out: list[IssuerSource] = []
    for r in body:
        state = r.get(cols[_H_STATE], "").upper()
        issuer = r.get(cols[_H_ISSUER], "")
        url = r.get(cols[_H_URL], "")
        if not (state and issuer and url.startswith(("http://", "https://"))):
            continue
        out.append(IssuerSource(issuer_id=issuer, state=state, index_url=url))
    return out


def resolve_latest_url(client: httpx.Client | None = None, *,
                       newest_year: int | None = None) -> tuple[str, int]:
    """The newest published MR-PUF URL and its plan year.

    Probes plan years newest-first so next year's file is picked up the day CMS posts it
    — the alternative is a hardcoded URL that silently serves stale coverage until someone
    notices. Falls back to the pinned default if nothing responds.
    """
    owns = client is None
    client = client or httpx.Client(follow_redirects=True,
                                    headers={"User-Agent": settings.contact_ua})
    # Plan-year files publish ahead of the calendar year, so start one year out.
    start = newest_year or (time.gmtime().tm_year + 1)
    try:
        for year in range(start, DEFAULT_PLAN_YEAR - 1, -1):
            url = MRPUF_URL_TEMPLATE.format(year=year)
            try:
                r = client.head(url, timeout=20)
                if r.status_code < 400:
                    return url, year
            except Exception:  # noqa: BLE001 - an unreachable year is just "not published"
                continue
    finally:
        if owns:
            client.close()
    return MRPUF_URL_TEMPLATE.format(year=DEFAULT_PLAN_YEAR), DEFAULT_PLAN_YEAR


def fetch_sources(url: str | None = None) -> tuple[list[IssuerSource], str, int]:
    """Download + parse the MR-PUF. Returns (sources, url, plan_year).

    The download goes through `download.stream_to_bytes`, so the same byte ceiling that
    guards every other ingest path guards this one — a mistyped or hostile URL can't be
    read into memory unbounded.
    """
    if url:
        year = _year_from_url(url) or DEFAULT_PLAN_YEAR
    else:
        url, year = resolve_latest_url()
    data = stream_to_bytes(url, timeout=120)
    return parse_mrpuf(data), url, year


def _year_from_url(url: str) -> int | None:
    m = re.search(r"/py(\d{4})/", url)
    return int(m.group(1)) if m else None


def write_sources(root: Path, sources: list[IssuerSource], *, url: str, plan_year: int) -> Path:
    """Persist the parsed registry to marketplace_sources.json.

    Mirrors the `tic_sources.json` convention: a committed, human-readable record of what
    the harvest will read, so a coverage change is reviewable in a diff instead of being
    invisible inside a CI job.
    """
    path = Path(root) / SOURCES_FILE
    payload = {
        "_comment": (
            "Marketplace (QHP) machine-readable index URLs, parsed from the CMS "
            "Machine-Readable URL PUF by app/marketplace_registry.py. Regenerate with "
            "`python -m app.marketplace_registry --refresh`. Issuers sharing an index_url "
            "are fetched once. The PUF's Tech POC Email column is intentionally dropped."
        ),
        "source_url": url,
        "plan_year": plan_year,
        "retrieved": time.strftime("%Y-%m-%d", time.gmtime()),
        "issuer_count": len(sources),
        "index_url_count": len({s.index_url for s in sources}),
        "sources": [asdict(s) for s in sorted(sources, key=lambda s: (s.state, s.issuer_id))],
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def load_sources(root: Path | str = ".") -> list[IssuerSource]:
    """Read the committed registry. Empty when it hasn't been generated yet."""
    path = Path(root) / SOURCES_FILE
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - a corrupt registry means "no sources", not a crash
        return []
    out: list[IssuerSource] = []
    for r in data.get("sources") or []:
        if isinstance(r, dict) and r.get("index_url"):
            out.append(IssuerSource(issuer_id=str(r.get("issuer_id", "")),
                                    state=str(r.get("state", "")),
                                    index_url=str(r["index_url"])))
    return out


def fetch_index(url: str, client: httpx.Client) -> dict[str, Any]:
    """Fetch one issuer's index.json. Raises on any failure — the caller decides whether a
    dead issuer is tolerable (it is: 10 of 108 were unreachable when measured) or fatal."""
    r = client.get(url, timeout=30)
    r.raise_for_status()
    data = r.json()
    if not isinstance(data, dict):
        raise ValueError(f"{url}: index.json is not a JSON object")
    return data


def provider_urls(index: dict[str, Any]) -> list[str]:
    """The `provider_urls` entries of an index.json, http(s) only."""
    return [u for u in (index.get("provider_urls") or [])
            if isinstance(u, str) and u.startswith(("http://", "https://"))]


def main(argv: list[str]) -> None:
    ap = argparse.ArgumentParser(description="Parse the CMS Machine-Readable URL PUF into "
                                             "marketplace_sources.json.")
    ap.add_argument("--refresh", action="store_true", help="download the PUF and rewrite the registry")
    ap.add_argument("--probe", action="store_true",
                    help="fetch every index.json and report which issuers are reachable")
    ap.add_argument("--url", default=None, help="override the PUF URL (default: newest published)")
    ap.add_argument("--root", default=".", help="where marketplace_sources.json lives")
    args = ap.parse_args(argv[1:])

    root = Path(args.root)
    if args.refresh:
        sources, url, year = fetch_sources(args.url)
        path = write_sources(root, sources, url=url, plan_year=year)
        print(f"PY{year}: {len(sources)} issuer rows, "
              f"{len({s.index_url for s in sources})} distinct index URLs, "
              f"{len({s.state for s in sources})} states -> {path}", flush=True)
    if args.probe:
        sources = load_sources(root)
        if not sources:
            raise SystemExit("No marketplace_sources.json — run --refresh first.")
        urls = sorted({s.index_url for s in sources})
        ok = bad = files = 0
        with httpx.Client(follow_redirects=True,
                          headers={"User-Agent": settings.contact_ua}) as client:
            for u in urls:
                try:
                    idx = fetch_index(u, client)
                except Exception as e:  # noqa: BLE001 - reporting reachability is the point
                    bad += 1
                    print(f"  UNREACHABLE {u} -> {type(e).__name__}", flush=True)
                    continue
                ok += 1
                files += len(provider_urls(idx))
        print(f"index.json reachable {ok}/{len(urls)} (unreachable {bad}); "
              f"{files} provider files advertised.", flush=True)
    if not (args.refresh or args.probe):
        ap.print_help()


if __name__ == "__main__":
    main(sys.argv)

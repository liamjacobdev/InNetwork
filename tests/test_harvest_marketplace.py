"""Rail 4 (Marketplace machine-readable files) — parser, dedupe, and trust gate.

The fixtures below mirror the real PY2026 corpus measured on 2026-07-24, including the
two shapes that would silently corrupt a naive parser: a free-text `network_tier`
vocabulary that contains an explicit `OUT-OF-NETWORK` value, and a `providers.json` that
carries a second issuer's plan ids.
"""
from __future__ import annotations

import io
import json
import zipfile

import httpx
import pytest
import respx
from pyroaring import BitMap

from app import marketplace_registry, membership
from app.harvest_marketplace import (
    DISCRIMINATION_MAX_FRACTION,
    MarketplaceStats,
    check_discrimination,
    check_positive_control,
    harvest_index,
    harvest_issuer,
    split_hios,
    stream_plan_npis,
    tier_is_out_of_network,
    write_plans,
)

# Luhn-valid NPIs (the gate rejects anything else).
NPI_A = "1003007915"
NPI_B = "1194728105"
NPI_C = "1245319599"
NPI_BAD = "1234567890"     # 10 digits, invalid check digit


def _rec(npi: str, plans: list[dict]) -> dict:
    return {"npi": npi, "type": "INDIVIDUAL", "name": {"first": "A", "last": "B"},
            "addresses": [{"address": "1 Main St", "city": "Juneau", "state": "AK",
                           "zip": "99801"}],
            "specialty": ["Family"], "plans": plans}


def _plan(plan_id: str, tier: str = "PREFERRED", years: list[int] | None = None) -> dict:
    p = {"plan_id_type": "HIOS-PLAN-ID", "plan_id": plan_id, "network_tier": tier}
    if years is not None:
        p["years"] = years
    return p


def _stream(records: list[dict]) -> io.BytesIO:
    return io.BytesIO(json.dumps(records).encode())


# ── The tier rule: a denylist, because the vocabulary is free text ────────────
@pytest.mark.parametrize("tier", [
    "OUT-OF-NETWORK", "out of network", "Out_Of_Network", "OON",
    "NON-PARTICIPATING", "NOT-CONTRACTED", "NON-NETWORK",
])
def test_explicit_out_of_network_tiers_are_excluded(tier):
    assert tier_is_out_of_network(tier) is True


@pytest.mark.parametrize("tier", [
    "PREFERRED", "PREFERREDTIER", "STANDARDTIER", "IN-NETWORK", "NON-PREFERRED",
    "CX--CONNECTION-DENTAL--PPO-USA", "CS--AETNA", "COPAYMENT-NETWORK", "", None,
])
def test_real_world_tier_vocabulary_stays_in_network(tier):
    """Measured across 18 live issuers: 25+ distinct tier strings, most of them issuer
    invented. A whitelist would discard real networks, so anything not explicitly
    out-of-network counts as the issuer listing that provider under that plan.

    NON-PREFERRED is deliberately included: it is a rate tier *inside* the network, not
    an out-of-network marker.
    """
    assert tier_is_out_of_network(tier) is False


def test_out_of_network_rows_never_enter_the_bitmap():
    stats = MarketplaceStats()
    plans = stream_plan_npis(_stream([
        _rec(NPI_A, [_plan("73836AK0930001", "PREFERRED")]),
        _rec(NPI_B, [_plan("73836AK0930001", "OUT-OF-NETWORK")]),
    ]), 2026, stats)
    assert membership.encode(NPI_B) not in plans["73836AK0930001"]
    assert stats.rejected_tier == 1
    assert len(plans["73836AK0930001"]) == 1


# ── Luhn, plan year, and issuer attribution ──────────────────────────────────
def test_luhn_invalid_npi_cannot_fabricate_membership():
    stats = MarketplaceStats()
    plans = stream_plan_npis(_stream([
        _rec(NPI_BAD, [_plan("73836AK0930001")]),
        _rec(NPI_A, [_plan("73836AK0930001")]),
    ]), 2026, stats)
    assert len(plans["73836AK0930001"]) == 1
    assert stats.rejected_luhn == 1


def test_a_prior_year_plan_row_is_not_served_as_current():
    stats = MarketplaceStats()
    plans = stream_plan_npis(_stream([
        _rec(NPI_A, [_plan("73836AK0930001", years=[2025])]),
        _rec(NPI_B, [_plan("73836AK0930001", years=[2026])]),
    ]), 2026, stats)
    assert len(plans["73836AK0930001"]) == 1
    assert stats.rejected_year == 1


def test_a_plan_row_without_years_is_treated_as_current():
    stats = MarketplaceStats()
    plans = stream_plan_npis(_stream([_rec(NPI_A, [_plan("73836AK0930001")])]), 2026, stats)
    assert len(plans["73836AK0930001"]) == 1


def test_non_hios_plan_ids_are_skipped_not_guessed():
    """A plan id we can't attribute to an issuer + state can't be state-scoped, and this
    codebase never maps an unattributable network onto a payer id."""
    stats = MarketplaceStats()
    plans = stream_plan_npis(_stream([
        _rec(NPI_A, [_plan("AD005003"), _plan("73836AK0930001")]),
    ]), 2026, stats)
    assert list(plans) == ["73836AK0930001"]
    assert stats.skipped_non_hios == 1


def test_issuer_and_state_come_from_the_plan_id_not_the_file():
    """Moda's Alaska file carries Delta Dental's plan ids — so attribution must come from
    the HIOS id itself, never from the index URL that led us to the file."""
    assert split_hios("73836AK0930001") == ("73836", "AK")
    assert split_hios("21989AK0030001") == ("21989", "AK")
    assert split_hios("AD005003") is None


# ── The discrimination gate (the bogus-NPI analog) ───────────────────────────
def test_a_directory_dump_is_rejected_as_non_discriminating():
    national = 1000
    dump = BitMap(range(int(national * DISCRIMINATION_MAX_FRACTION) + 50))
    reason = check_discrimination(dump, national)
    assert reason is not None and "does not discriminate" in reason


def test_a_plausible_network_passes_discrimination():
    assert check_discrimination(BitMap(range(100)), 1000) is None


# ── The positive control (the real-NPI analog) ───────────────────────────────
@respx.mock
def test_ghost_npis_fail_the_positive_control():
    respx.get(url__startswith="https://npiregistry.cms.hhs.gov").mock(
        return_value=httpx.Response(200, json={"results": []}))
    bm = BitMap([membership.encode(n) for n in (NPI_A, NPI_B, NPI_C)])
    with httpx.Client() as c:
        reason = check_positive_control(bm, c, sample=3, min_hits=2)
    assert reason is not None and "positive control failed" in reason


@respx.mock
def test_real_npis_pass_the_positive_control():
    respx.get(url__startswith="https://npiregistry.cms.hhs.gov").mock(
        return_value=httpx.Response(200, json={"results": [{"number": NPI_A}]}))
    bm = BitMap([membership.encode(n) for n in (NPI_A, NPI_B, NPI_C)])
    with httpx.Client() as c:
        assert check_positive_control(bm, c, sample=3, min_hits=2) is None


@respx.mock
def test_an_unreachable_registry_is_not_treated_as_a_verdict():
    """A down NPPES must not condemn a good network (nor bless a bad one) — it is simply
    not evidence, so the control abstains."""
    respx.get(url__startswith="https://npiregistry.cms.hhs.gov").mock(
        side_effect=httpx.ConnectError("boom"))
    bm = BitMap([membership.encode(n) for n in (NPI_A, NPI_B, NPI_C)])
    with httpx.Client() as c:
        assert check_positive_control(bm, c, sample=3, min_hits=2) is None


# ── Completeness guard + dedupe, end to end ──────────────────────────────────
_INDEX = {"provider_urls": ["https://issuer.test/providers.json"],
          "plan_urls": ["https://issuer.test/plans.json"], "formulary_urls": []}

_RECORDS = [
    _rec(NPI_A, [_plan("73836AK0930001"), _plan("73836AK0930002")]),
    _rec(NPI_B, [_plan("73836AK0930001"), _plan("73836AK0930002")]),
    _rec(NPI_C, [_plan("73836AK0940001")]),
]


@respx.mock
def test_a_missing_provider_file_writes_nothing(tmp_path):
    """The completeness guard: a hole would turn that file's providers into a fabricated
    'no', so a partial fan-out is never written."""
    respx.get("https://issuer.test/index.json").mock(
        return_value=httpx.Response(200, json={
            "provider_urls": ["https://issuer.test/a.json", "https://issuer.test/b.json"],
            "plan_urls": []}))
    respx.get("https://issuer.test/a.json").mock(
        return_value=httpx.Response(200, json=_RECORDS))
    respx.get("https://issuer.test/b.json").mock(return_value=httpx.Response(500))

    with httpx.Client() as c:
        plans, _names, stats = harvest_index("https://issuer.test/index.json", c,
                                             plan_year=2026)
    assert plans                      # it did read real data from the first file
    assert stats.complete is False    # ...and still refuses to be written
    assert stats.failures


@respx.mock
def test_a_probe_cap_is_never_mistaken_for_a_complete_harvest(tmp_path):
    respx.get("https://issuer.test/index.json").mock(
        return_value=httpx.Response(200, json={
            "provider_urls": ["https://issuer.test/a.json", "https://issuer.test/b.json"],
            "plan_urls": []}))
    respx.get("https://issuer.test/a.json").mock(
        return_value=httpx.Response(200, json=_RECORDS))

    with httpx.Client() as c:
        _plans, _names, stats = harvest_index("https://issuer.test/index.json", c,
                                              plan_year=2026, max_files=1)
    assert stats.complete is False


@respx.mock
def test_plans_sharing_a_network_share_one_blob(tmp_path):
    """Measured 4.3x on the first real issuer: plans are separate catalog entries, but
    identical NPI sets must not be written once per plan."""
    respx.get("https://issuer.test/index.json").mock(
        return_value=httpx.Response(200, json=_INDEX))
    respx.get("https://issuer.test/providers.json").mock(
        return_value=httpx.Response(200, json=_RECORDS))
    respx.get("https://issuer.test/plans.json").mock(
        return_value=httpx.Response(200, json=[
            {"plan_id": "73836AK0930001", "marketing_name": "Beacon Gold"},
            {"plan_id": "73836AK0930002", "marketing_name": "Beacon Silver"},
            {"plan_id": "73836AK0940001", "marketing_name": "Summit Bronze"},
        ]))

    with httpx.Client() as c:
        plans, names, stats = harvest_index("https://issuer.test/index.json", c,
                                            plan_year=2026)
        assert stats.complete
        entries = write_plans(tmp_path, plans, names,
                              index_url="https://issuer.test/index.json",
                              plan_year=2026, stats=stats, client=None)

    assert len(entries) == 3                       # three plans...
    assert len({e.file for e in entries}) == 2     # ...over two distinct networks
    assert stats.networks == 2

    by_id = {e.id: e for e in entries}
    assert by_id["73836ak0930001"].label == "Beacon Gold (AK)"
    assert by_id["73836ak0930001"].level == "plan"
    assert by_id["73836ak0930001"].method == "marketplace-mrf"
    assert by_id["73836ak0930001"].states == ["AK"]
    assert by_id["73836ak0930001"].count == 2
    assert by_id["73836ak0940001"].count == 1


@respx.mock
def test_shared_blobs_load_once_and_answer_for_every_plan(tmp_path):
    """The serve-side counterpart: entries sharing a blob must each answer membership,
    while the bytes are decoded a single time."""
    respx.get("https://issuer.test/index.json").mock(
        return_value=httpx.Response(200, json=_INDEX))
    respx.get("https://issuer.test/providers.json").mock(
        return_value=httpx.Response(200, json=_RECORDS))
    respx.get("https://issuer.test/plans.json").mock(
        return_value=httpx.Response(200, json=[]))

    with httpx.Client() as c:
        plans, names, stats = harvest_index("https://issuer.test/index.json", c,
                                            plan_year=2026)
        write_plans(tmp_path, plans, names, index_url="https://issuer.test/index.json",
                    plan_year=2026, stats=stats, client=None)

    store = membership.MembershipStore(tmp_path)
    assert store.load() == 3
    try:
        # Indexing decodes nothing — the whole point of plan-level scale.
        assert store.resident() == 0
        assert store.has("73836ak0930001", NPI_A) is True
        assert store.resident() == 1
        # Its sibling plan rides the SAME blob, so it answers without a second decode.
        assert store.has("73836ak0930002", NPI_A) is True
        assert store.resident() == 1
        assert store.has("73836ak0940001", NPI_A) is False     # different network
        assert store.has("73836ak0940001", NPI_C) is True
        assert store.resident() == 2
    finally:
        store.close()


@respx.mock
def test_a_non_discriminating_network_takes_all_its_plans_with_it(tmp_path):
    """The gate is per-NETWORK, and a failure is total for that network: both plans sold
    over it disappear, it is reported rather than silently dropped, and the issuer's other
    (plausible) network is unaffected."""
    respx.get("https://issuer.test/index.json").mock(
        return_value=httpx.Response(200, json=_INDEX))
    respx.get("https://issuer.test/providers.json").mock(
        return_value=httpx.Response(200, json=_RECORDS))
    respx.get("https://issuer.test/plans.json").mock(
        return_value=httpx.Response(200, json=[]))

    # Denominator 2: the 2-NPI network reads as 100% of the country (rejected), while the
    # 1-NPI network reads as 50% (plausible, kept).
    (tmp_path / "manifest.json").write_text(json.dumps(
        {"version": 1, "offset": membership.OFFSET,
         "payers": {"medicare": {"id": "medicare", "label": "M", "category": "medicare",
                                 "level": "plan", "method": "cms-enrollment",
                                 "source_url": "x", "fetched_at": 0, "count": 2,
                                 "file": "medicare.roaring", "sha256": ""}}}), encoding="utf-8")

    with httpx.Client() as c:
        plans, names, stats = harvest_index("https://issuer.test/index.json", c,
                                            plan_year=2026)
        entries = write_plans(tmp_path, plans, names,
                              index_url="https://issuer.test/index.json",
                              plan_year=2026, stats=stats, client=None)

    written = {e.id for e in entries}
    # Both plans sold over the rejected network are gone — not one of them, both.
    assert "73836ak0930001" not in written
    assert "73836ak0930002" not in written
    # ...and the issuer's other, plausible network still ships.
    assert written == {"73836ak0940001"}
    assert any("does not discriminate" in r for r in stats.gate_rejected)


@respx.mock
def test_a_mis_encoded_plans_file_still_yields_readable_names(tmp_path):
    """Real case: BCBS Michigan serves plans.json as application/json containing the
    cp1252 byte 0xAE (the ® in 'Blue Cross® Premier PPO'). Labels are what a user reads,
    so they must come out intact rather than peppered with U+FFFD."""
    body = json.dumps([{"plan_id": "73836AK0930001",
                        "marketing_name": "Blue Cross® Premier PPO"}]).encode("cp1252")
    respx.get("https://issuer.test/index.json").mock(
        return_value=httpx.Response(200, json=_INDEX))
    respx.get("https://issuer.test/providers.json").mock(
        return_value=httpx.Response(200, json=_RECORDS))
    respx.get("https://issuer.test/plans.json").mock(
        return_value=httpx.Response(200, content=body,
                                    headers={"Content-Type": "application/json"}))

    with httpx.Client() as c:
        plans, names, stats = harvest_index("https://issuer.test/index.json", c,
                                            plan_year=2026)
    assert names["73836AK0930001"] == "Blue Cross® Premier PPO"
    assert "�" not in names["73836AK0930001"]
    assert stats.label_warnings == []


@respx.mock
def test_unavailable_plan_names_are_cosmetic_not_a_trust_rejection(tmp_path):
    """A missing label must never be reported as a gate rejection — that would imply a
    trust check refused a network, and it makes a clean run look dirty."""
    respx.get("https://issuer.test/index.json").mock(
        return_value=httpx.Response(200, json=_INDEX))
    respx.get("https://issuer.test/providers.json").mock(
        return_value=httpx.Response(200, json=_RECORDS))
    respx.get("https://issuer.test/plans.json").mock(return_value=httpx.Response(500))

    with httpx.Client() as c:
        plans, names, stats = harvest_index("https://issuer.test/index.json", c,
                                            plan_year=2026)
        entries = write_plans(tmp_path, plans, names,
                              index_url="https://issuer.test/index.json",
                              plan_year=2026, stats=stats, client=None)
    assert stats.gate_rejected == []          # not a trust event
    assert stats.label_warnings                # ...but not silent either
    assert stats.complete                      # and the harvest still ships
    assert {e.id for e in entries} == {"73836ak0930001", "73836ak0930002", "73836ak0940001"}
    assert all(e.label.startswith("Marketplace plan ") for e in entries)


# ── The MR-PUF reader ────────────────────────────────────────────────────────
def _xlsx(rows: list[list[str]]) -> bytes:
    """A minimal real .xlsx (shared strings + one sheet), so the stdlib reader is tested
    against the actual container format rather than a mock."""
    shared: list[str] = []
    idx: dict[str, int] = {}
    body = []
    for r, row in enumerate(rows, start=1):
        cells = []
        for c, val in enumerate(row):
            if val not in idx:
                idx[val] = len(shared)
                shared.append(val)
            ref = f"{chr(ord('A') + c)}{r}"
            cells.append(f'<c r="{ref}" t="s"><v>{idx[val]}</v></c>')
        body.append(f'<row r="{r}">{"".join(cells)}</row>')
    sst = "".join(f"<si><t>{s}</s_t></si>".replace("</s_t>", "</t>") for s in shared)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("xl/sharedStrings.xml",
                   f'<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">{sst}</sst>')
        z.writestr("xl/worksheets/sheet1.xml",
                   '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                   f'<sheetData>{"".join(body)}</sheetData></worksheet>')
    return buf.getvalue()


def test_mrpuf_is_parsed_by_header_name_not_position():
    """CMS reordering the sheet must be a no-op, not a silent swap of issuer id and URL."""
    data = _xlsx([
        ["Tech POC Email", "URL Submitted", "Issuer ID", "State"],
        ["a@b.com", "https://issuer.test/index.json", "12345", "ak"],
    ])
    sources = marketplace_registry.parse_mrpuf(data)
    assert len(sources) == 1
    assert sources[0].issuer_id == "12345"
    assert sources[0].state == "AK"
    assert sources[0].index_url == "https://issuer.test/index.json"


def test_mrpuf_rows_without_a_usable_url_are_dropped():
    data = _xlsx([
        ["State", "Issuer ID", "URL Submitted"],
        ["AK", "12345", "not-a-url"],
        ["AK", "", "https://issuer.test/index.json"],
        ["TX", "99999", "https://ok.test/index.json"],
    ])
    sources = marketplace_registry.parse_mrpuf(data)
    assert [s.issuer_id for s in sources] == ["99999"]


def test_mrpuf_with_an_unexpected_header_fails_loudly():
    data = _xlsx([["Something", "Else"], ["a", "b"]])
    with pytest.raises(ValueError, match="missing expected column"):
        marketplace_registry.parse_mrpuf(data)


def test_the_tech_poc_email_is_never_persisted():
    """It is a real person's work address and we have no use for it."""
    data = _xlsx([
        ["State", "Issuer ID", "URL Submitted", "Tech POC Email"],
        ["AK", "12345", "https://issuer.test/index.json", "someone@insurer.example"],
    ])
    sources = marketplace_registry.parse_mrpuf(data)
    assert "someone@insurer.example" not in json.dumps(
        [s.__dict__ for s in sources])


# ── Serving: state scoping is what makes thousands of plans usable ────────────
@respx.mock
def test_plan_list_is_scoped_to_the_searched_state(tmp_path, monkeypatch):
    """Rail 4 coverage is mostly REGIONAL. Offering an Alaska-only Marketplace plan to a
    Texan is noise, and selecting it could only ever return nothing — so /api/insurance/
    plans?state= returns national plans plus that state's, and nothing else."""
    from app.config import settings as app_settings
    from app.insurance import Registry

    respx.get("https://issuer.test/index.json").mock(
        return_value=httpx.Response(200, json=_INDEX))
    respx.get("https://issuer.test/providers.json").mock(
        return_value=httpx.Response(200, json=[
            _rec(NPI_A, [_plan("73836AK0930001")]),      # Alaska plan
            _rec(NPI_B, [_plan("26049MI0010001")]),      # Michigan plan
        ]))
    respx.get("https://issuer.test/plans.json").mock(return_value=httpx.Response(200, json=[]))

    with httpx.Client() as c:
        plans, names, stats = harvest_index("https://issuer.test/index.json", c,
                                            plan_year=2026)
        write_plans(tmp_path, plans, names, index_url="https://issuer.test/index.json",
                    plan_year=2026, stats=stats, client=None)

    old_dir, old_use = app_settings.membership_dir, app_settings.use_membership
    app_settings.membership_dir, app_settings.use_membership = str(tmp_path), True
    try:
        r = Registry()
        r.build()
        def ids(st):
            return {p["id"] for p in r.plans(st) if p["confidence"] == "verified"}

        def mrf(st):
            return {i for i in ids(st) if i[:5].isdigit()}

        assert mrf("") == {"73836ak0930001", "26049mi0010001"}   # unscoped: everything
        assert mrf("MI") == {"26049mi0010001"}                   # a Texan never sees Alaska
        assert mrf("AK") == {"73836ak0930001"}
        # National plans survive every scope — scoping narrows the regional noise, it must
        # never hide Medicare from someone searching a particular state.
        assert "medicare" in ids("MI") and "medicare" in ids("AK")
        # The scoped payload carries the scope so a UI can label it honestly.
        mi = [p for p in r.plans("MI") if p["id"] == "26049mi0010001"][0]
        assert mi["states"] == ["MI"]
        if r.membership_store:
            r.membership_store.close()
    finally:
        app_settings.membership_dir, app_settings.use_membership = old_dir, old_use


# ── Registry plumbing: resolve, persist, reload ──────────────────────────────
@respx.mock
def test_the_newest_published_plan_year_wins():
    """The PUF is republished each plan year. Probing newer years first means a new year is
    picked up the day CMS posts it, instead of silently serving last year's issuer list."""
    respx.head(marketplace_registry.MRPUF_URL_TEMPLATE.format(year=2028)).mock(
        return_value=httpx.Response(404))
    respx.head(marketplace_registry.MRPUF_URL_TEMPLATE.format(year=2027)).mock(
        return_value=httpx.Response(200))
    with httpx.Client() as c:
        url, year = marketplace_registry.resolve_latest_url(c, newest_year=2028)
    assert year == 2027 and "py2027" in url


@respx.mock
def test_resolution_falls_back_to_the_pinned_year_when_nothing_answers():
    respx.head(url__startswith="https://data.healthcare.gov").mock(
        side_effect=httpx.ConnectError("down"))
    with httpx.Client() as c:
        url, year = marketplace_registry.resolve_latest_url(c, newest_year=2027)
    assert year == marketplace_registry.DEFAULT_PLAN_YEAR
    assert str(marketplace_registry.DEFAULT_PLAN_YEAR) in url


@respx.mock
def test_fetch_parses_the_puf_from_a_pinned_url(tmp_path):
    data = _xlsx([
        ["State", "Issuer ID", "URL Submitted"],
        ["TX", "12345", "https://issuer.test/index.json"],
    ])
    respx.get("https://data.healthcare.gov/datafile/py2026/machine_readable_PUF.xlsx").mock(
        return_value=httpx.Response(200, content=data))
    sources, url, year = marketplace_registry.fetch_sources(
        "https://data.healthcare.gov/datafile/py2026/machine_readable_PUF.xlsx")
    assert year == 2026 and len(sources) == 1


def test_the_registry_round_trips_through_disk(tmp_path):
    sources = [
        marketplace_registry.IssuerSource("12345", "TX", "https://a.test/index.json"),
        marketplace_registry.IssuerSource("67890", "AK", "https://a.test/index.json"),
    ]
    path = marketplace_registry.write_sources(tmp_path, sources,
                                              url="https://puf.test/f.xlsx", plan_year=2026)
    payload = json.loads(path.read_text(encoding="utf-8"))
    # Issuers sharing an index are counted once for fetching, but both rows are kept.
    assert payload["issuer_count"] == 2 and payload["index_url_count"] == 1
    back = marketplace_registry.load_sources(tmp_path)
    assert {s.issuer_id for s in back} == {"12345", "67890"}


def test_a_missing_or_corrupt_registry_means_no_sources_not_a_crash(tmp_path):
    assert marketplace_registry.load_sources(tmp_path) == []
    (tmp_path / marketplace_registry.SOURCES_FILE).write_text("{ nope", encoding="utf-8")
    assert marketplace_registry.load_sources(tmp_path) == []


@respx.mock
def test_provider_urls_ignores_entries_that_are_not_fetchable():
    respx.get("https://issuer.test/index.json").mock(return_value=httpx.Response(200, json={
        "provider_urls": ["https://ok.test/p.json", "ftp://nope.test/p.json", "", 7],
    }))
    with httpx.Client() as c:
        idx = marketplace_registry.fetch_index("https://issuer.test/index.json", c)
    assert marketplace_registry.provider_urls(idx) == ["https://ok.test/p.json"]


@respx.mock
def test_an_index_that_is_not_an_object_is_rejected():
    respx.get("https://issuer.test/index.json").mock(
        return_value=httpx.Response(200, json=["not", "an", "object"]))
    with httpx.Client() as c, pytest.raises(ValueError, match="not a JSON object"):
        marketplace_registry.fetch_index("https://issuer.test/index.json", c)


# ── Harvest orchestration ────────────────────────────────────────────────────
@respx.mock
def test_an_unreachable_issuer_index_is_recorded_not_raised(tmp_path):
    """~10 of 108 live indexes 403/404 on a given day. One dead issuer must not abort the
    ~100 that harvest cleanly."""
    respx.get("https://issuer.test/index.json").mock(return_value=httpx.Response(403))
    with httpx.Client() as c:
        entries, stats = harvest_issuer("https://issuer.test/index.json", tmp_path, c,
                                        plan_year=2026)
    assert entries == []
    assert stats.error and "index unreadable" in stats.error


@respx.mock
def test_an_index_advertising_no_provider_files_is_an_error(tmp_path):
    respx.get("https://issuer.test/index.json").mock(
        return_value=httpx.Response(200, json={"provider_urls": [], "plan_urls": []}))
    with httpx.Client() as c:
        entries, stats = harvest_issuer("https://issuer.test/index.json", tmp_path, c,
                                        plan_year=2026)
    assert entries == [] and "no provider_urls" in (stats.error or "")


@respx.mock
def test_a_dry_run_reports_without_writing(tmp_path):
    respx.get("https://issuer.test/index.json").mock(
        return_value=httpx.Response(200, json=_INDEX))
    respx.get("https://issuer.test/providers.json").mock(
        return_value=httpx.Response(200, json=_RECORDS))
    respx.get("https://issuer.test/plans.json").mock(return_value=httpx.Response(200, json=[]))
    with httpx.Client() as c:
        entries, stats = harvest_issuer("https://issuer.test/index.json", tmp_path, c,
                                        plan_year=2026, dry_run=True)
    assert entries == []
    assert stats.networks == 2 and stats.complete
    assert not (tmp_path / "manifest.json").exists()


def test_the_national_denominator_prefers_the_real_medicare_count(tmp_path):
    from app.harvest_marketplace import _NATIONAL_NPI_FALLBACK, _national_npi_denominator
    assert _national_npi_denominator(tmp_path) == _NATIONAL_NPI_FALLBACK   # no manifest
    (tmp_path / "manifest.json").write_text(json.dumps(
        {"payers": {"medicare": {"count": 4242}}}), encoding="utf-8")
    assert _national_npi_denominator(tmp_path) == 4242


def test_json_bytes_decode_ladder_prefers_a_lossless_encoding():
    from app.harvest_marketplace import _decode_json_bytes
    assert _decode_json_bytes("Blue Cross® PPO".encode()) == "Blue Cross® PPO"
    assert _decode_json_bytes("Blue Cross® PPO".encode("cp1252")) == "Blue Cross® PPO"


# ── The CLIs the monthly workflow actually invokes ───────────────────────────
_PUF_URL = "https://data.healthcare.gov/datafile/py2026/machine_readable_PUF.xlsx"


@respx.mock
def test_registry_cli_refresh_writes_the_sources_file(tmp_path, monkeypatch, capsys):
    respx.head(url__startswith="https://data.healthcare.gov").mock(
        return_value=httpx.Response(404))
    respx.get(_PUF_URL).mock(return_value=httpx.Response(200, content=_xlsx([
        ["State", "Issuer ID", "URL Submitted"],
        ["TX", "12345", "https://issuer.test/index.json"],
        ["AK", "67890", "https://issuer.test/index.json"],
    ])))
    monkeypatch.chdir(tmp_path)
    marketplace_registry.main(["prog", "--refresh"])
    out = capsys.readouterr().out
    assert "2 issuer rows" in out and "1 distinct index URLs" in out
    assert (tmp_path / marketplace_registry.SOURCES_FILE).exists()


@respx.mock
def test_registry_cli_probe_reports_reachability(tmp_path, monkeypatch, capsys):
    marketplace_registry.write_sources(
        tmp_path,
        [marketplace_registry.IssuerSource("1", "TX", "https://up.test/index.json"),
         marketplace_registry.IssuerSource("2", "AK", "https://down.test/index.json")],
        url=_PUF_URL, plan_year=2026)
    respx.get("https://up.test/index.json").mock(return_value=httpx.Response(
        200, json={"provider_urls": ["https://up.test/p.json"]}))
    respx.get("https://down.test/index.json").mock(return_value=httpx.Response(403))
    monkeypatch.chdir(tmp_path)
    marketplace_registry.main(["prog", "--probe"])
    out = capsys.readouterr().out
    assert "UNREACHABLE https://down.test/index.json" in out
    assert "reachable 1/2" in out and "1 provider files advertised" in out


def test_registry_cli_probe_without_a_registry_fails_loudly(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit, match="run --refresh first"):
        marketplace_registry.main(["prog", "--probe"])


@respx.mock
def test_harvest_cli_single_index(tmp_path, monkeypatch, capsys):
    from app import harvest_marketplace as hm

    respx.get("https://issuer.test/index.json").mock(
        return_value=httpx.Response(200, json=_INDEX))
    respx.get("https://issuer.test/providers.json").mock(
        return_value=httpx.Response(200, json=_RECORDS))
    respx.get("https://issuer.test/plans.json").mock(return_value=httpx.Response(200, json=[]))
    respx.get(url__startswith="https://npiregistry.cms.hhs.gov").mock(
        return_value=httpx.Response(200, json={"results": [{"number": NPI_A}]}))
    monkeypatch.chdir(tmp_path)
    hm.main(["prog", "--index-url", "https://issuer.test/index.json",
             "--root", str(tmp_path / "payers")])
    out = capsys.readouterr().out
    assert "plans=3 networks=2" in out
    assert "wrote 3 plan entries over 2 blob(s)" in out
    assert "Rail 4 summary: 1 issuer index(es), 3 plan entries" in out


def test_harvest_cli_without_a_registry_fails_loudly(tmp_path, monkeypatch):
    from app import harvest_marketplace as hm

    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit, match="marketplace_registry --refresh"):
        hm.main(["prog", "--root", str(tmp_path / "payers")])


@respx.mock
def test_harvest_cli_reads_the_registry_and_honours_state_and_caps(tmp_path, monkeypatch, capsys):
    """The workflow's --state / --max-issuers path: sources come from the committed
    registry, and issuers sharing an index URL are fetched once."""
    from app import harvest_marketplace as hm

    marketplace_registry.write_sources(
        tmp_path,
        [marketplace_registry.IssuerSource("73836", "AK", "https://issuer.test/index.json"),
         marketplace_registry.IssuerSource("99999", "TX", "https://other.test/index.json")],
        url=_PUF_URL, plan_year=2026)
    respx.get("https://issuer.test/index.json").mock(
        return_value=httpx.Response(200, json=_INDEX))
    respx.get("https://issuer.test/providers.json").mock(
        return_value=httpx.Response(200, json=_RECORDS))
    respx.get("https://issuer.test/plans.json").mock(return_value=httpx.Response(200, json=[]))
    respx.get(url__startswith="https://npiregistry.cms.hhs.gov").mock(
        return_value=httpx.Response(200, json={"results": [{"number": NPI_A}]}))
    monkeypatch.chdir(tmp_path)
    hm.main(["prog", "--state", "AK", "--root", str(tmp_path / "payers")])
    out = capsys.readouterr().out
    assert "other.test" not in out                    # the TX issuer was filtered out
    assert "Rail 4 summary: 1 issuer index(es)" in out


def test_the_plan_year_comes_from_the_committed_registry(tmp_path):
    from app.harvest_marketplace import _plan_year_from_sources

    assert _plan_year_from_sources(tmp_path) == marketplace_registry.DEFAULT_PLAN_YEAR
    marketplace_registry.write_sources(tmp_path, [], url=_PUF_URL, plan_year=2027)
    assert _plan_year_from_sources(tmp_path) == 2027


# ── Invalid UTF-8 in a provider file must not cost the whole issuer ──────────
def _cp1252_records() -> bytes:
    """A providers.json carrying the exact failure seen live: a provider name with the
    cp1252 byte 0x82 inside an otherwise-JSON file."""
    good = json.dumps(_RECORDS).encode()
    return good.replace(b'"first": "A"', b'"first": "Blas\x82"', 1)


def test_the_sanitizer_repairs_invalid_utf8_without_touching_the_data():
    from app.harvest_marketplace import Utf8Sanitizer

    stats = MarketplaceStats()
    plans = stream_plan_npis(Utf8Sanitizer(io.BytesIO(_cp1252_records())), 2026, stats)
    # Every NPI and plan id still lands — the damage is confined to the free-text name.
    assert set(plans) == {"73836AK0930001", "73836AK0930002", "73836AK0940001"}
    assert len(plans["73836AK0930001"]) == 2
    assert stats.records == 3


def test_the_sanitizer_handles_a_character_split_across_reads():
    """A multi-byte character straddling a chunk boundary must decode, not be mangled at
    the seam — which is why an incremental decoder is used."""
    from app.harvest_marketplace import Utf8Sanitizer

    payload = ('{"n": "' + "é" * 400 + '"}').encode()
    out = Utf8Sanitizer(io.BytesIO(payload), chunk=7).read()
    assert out == payload


@respx.mock
def test_one_bad_byte_does_not_cost_the_entire_issuer(tmp_path):
    """Observed on a live issuer: a single cp1252 byte in a provider's name aborted the
    strict parse, and the completeness guard then correctly discarded the WHOLE issuer.
    The retry-through-the-sanitizer is what turns that into a harvested issuer."""
    respx.get("https://issuer.test/index.json").mock(
        return_value=httpx.Response(200, json=_INDEX))
    respx.get("https://issuer.test/providers.json").mock(
        return_value=httpx.Response(200, content=_cp1252_records(),
                                    headers={"Content-Type": "application/json"}))
    respx.get("https://issuer.test/plans.json").mock(return_value=httpx.Response(200, json=[]))

    with httpx.Client() as c:
        plans, _names, stats = harvest_index("https://issuer.test/index.json", c,
                                             plan_year=2026)
    assert stats.complete, f"issuer was dropped: {stats.failures}"
    assert stats.provider_files == 1
    assert set(plans) == {"73836AK0930001", "73836AK0930002", "73836AK0940001"}
    # The retry must not double-count the rows it read before the error.
    assert len(plans["73836AK0930001"]) == 2


@respx.mock
def test_structurally_broken_json_is_still_a_hole_not_a_lenient_parse(tmp_path):
    """The lenient re-read is for ENCODING only. Malformed JSON must remain a failure —
    re-parsing it leniently would be a way to admit garbage into a verified set."""
    respx.get("https://issuer.test/index.json").mock(
        return_value=httpx.Response(200, json=_INDEX))
    respx.get("https://issuer.test/providers.json").mock(
        return_value=httpx.Response(200, content=b'[{"npi": "1003007915", "plans": [',
                                    headers={"Content-Type": "application/json"}))
    respx.get("https://issuer.test/plans.json").mock(return_value=httpx.Response(200, json=[]))

    with httpx.Client() as c:
        _plans, _names, stats = harvest_index("https://issuer.test/index.json", c,
                                              plan_year=2026)
    assert stats.complete is False and stats.failures


def _available_ijson_backends():
    """Every ijson backend importable here. Which one is active depends on whether the
    compiled extension built on this machine, so the encoding-error detection has to hold
    for all of them — not just the one that happens to be installed on a laptop."""
    import importlib

    out = []
    for name in ("yajl2_c", "yajl2_cffi", "yajl2", "yajl", "python"):
        try:
            out.append((name, importlib.import_module(f"ijson.backends.{name}")))
        except Exception:  # noqa: BLE001 - a backend that isn't built simply isn't tested
            continue
    return out


def test_every_ijson_backend_reports_bad_utf8_in_a_way_we_recognize():
    """The backends word this completely differently — yajl says "invalid bytes in UTF8
    string", the pure-Python one raises a wrapped UnicodeDecodeError. Matching only one
    would silently disable the sanitizer wherever the other is active, dropping whole
    issuers for a single accented letter."""
    from app.harvest_marketplace import _is_encoding_error

    backends = _available_ijson_backends()
    assert backends, "no ijson backend importable"
    bad = _cp1252_records()
    for name, mod in backends:
        with pytest.raises(Exception) as ei:  # noqa: PT011 - backend-specific error types
            list(mod.items(io.BytesIO(bad), "item"))
        assert _is_encoding_error(ei.value), (
            f"{name} backend's encoding error not recognized: {ei.value!r}")


def test_structural_errors_are_not_mistaken_for_encoding_errors_on_any_backend():
    """The other half of the boundary: broken JSON must never be re-read leniently."""
    from app.harvest_marketplace import _is_encoding_error

    truncated = b'[{"npi": "1003007915", "plans": ['
    for name, mod in _available_ijson_backends():
        with pytest.raises(Exception) as ei:  # noqa: PT011 - backend-specific error types
            list(mod.items(io.BytesIO(truncated), "item"))
        assert not _is_encoding_error(ei.value), (
            f"{name} backend's structural error wrongly treated as encoding: {ei.value!r}")

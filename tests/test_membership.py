"""The membership store — the rebuilt verified tier. These tests hold it to the same bar
as tests/test_trust_rules.py: a hit is a real, provenanced, verified in-network answer;
absence is a genuine "no" (the bitmap is the complete harvested set); a not-loaded payer
is "unknown", never a fabricated "no"; a garbage NPI can't fabricate a "yes"; and a stale
payer is flagged, never silently served as a fresh green."""
import time

import pytest

from app import membership
from app.config import settings
from app.insurance import MembershipSource, Registry

# Real, valid NPIs (pass the Luhn gate). "PRESENT" get added to the test bitmap; "ABSENT"
# is valid but deliberately not added, so it exercises the genuine-False path.
PRESENT = ["1003000126", "1003000134", "1003000142"]
ABSENT = "1992999874"
BOGUS = "0000000000"  # fails Luhn — must never match


def _write(root, npis, **kw):
    """Build a bitmap from `npis` and write it as a payer, returning the ManifestEntry."""
    bm, _admitted, _rejected = membership.build_bitmap(npis)
    kw.setdefault("id", "cigna")
    kw.setdefault("label", "Cigna")
    kw.setdefault("category", "commercial")
    kw.setdefault("level", "payer")
    kw.setdefault("method", "fhir-plannet")
    kw.setdefault("source_url", "https://cigna.example/directory")
    kw.setdefault("states", None)
    return membership.write_payer(root, bitmap=bm, **kw)


def _store(root):
    s = membership.MembershipStore(root)
    s.load()
    return s


# ── encode / build: the Luhn admission gate ───────────────────────────────────
def test_encode_offsets_valid_and_rejects_garbage():
    v = membership.encode("1003000126")
    assert v == 1003000126 - membership.OFFSET
    assert 0 <= v <= 0xFFFFFFFF
    for bad in [BOGUS, "1234567890", "123", "", "abcdefghij"]:
        assert membership.encode(bad) is None


def test_build_bitmap_counts_rejections():
    bm, admitted, rejected = membership.build_bitmap(PRESENT + [BOGUS, "1234567890", "x"])
    assert admitted == len(PRESENT)
    assert rejected == 3
    assert len(bm) == len(PRESENT)


# ── store roundtrip + membership ──────────────────────────────────────────────
def test_roundtrip_membership(tmp_path):
    entry = _write(tmp_path, PRESENT)
    assert entry.count == len(PRESENT)
    assert entry.sha256 and (tmp_path / entry.file).exists()

    s = _store(tmp_path)
    assert s.loaded("cigna") and s.count("cigna") == len(PRESENT)
    for n in PRESENT:
        assert s.has("cigna", n) is True
    assert s.has("cigna", ABSENT) is False   # complete set -> absence is a genuine no
    assert s.has("cigna", BOGUS) is False     # a non-NPI query can't match
    assert s.has("unknown_payer", PRESENT[0]) is False
    got = s.has_many("cigna", PRESENT + [ABSENT, BOGUS])
    assert got == set(PRESENT)


def test_manifest_entry_is_forward_compatible():
    raw = {"id": "x", "label": "X", "category": "commercial", "level": "payer",
           "method": "tic", "source_url": "u", "fetched_at": 1.0, "count": 0,
           "file": "x.roaring", "sha256": "", "states": None,
           "future_field_we_dont_know": 42}  # unknown keys must be ignored, not crash
    e = membership._entry_from_dict(raw)
    assert e.id == "x" and e.method == "tic"


# ── failure modes never fabricate a "no" ──────────────────────────────────────
def test_missing_blob_is_skipped_not_fatal(tmp_path):
    _write(tmp_path, PRESENT)
    (tmp_path / "cigna.roaring").unlink()   # blob gone, manifest still references it
    s = _store(tmp_path)
    assert not s.loaded("cigna")             # unknown, not "everyone out of network"
    assert s.has("cigna", PRESENT[0]) is False
    assert s.has_many("cigna", PRESENT) == set()


def test_corrupt_blob_is_refused(tmp_path):
    _write(tmp_path, PRESENT)
    (tmp_path / "cigna.roaring").write_bytes(b"not a roaring bitmap")  # sha256 mismatch
    s = _store(tmp_path)
    assert not s.loaded("cigna")


# ── staleness: flagged, never a silent stale green ────────────────────────────
def test_staleness_flag(tmp_path):
    fresh = _write(tmp_path, PRESENT, id="fresh", fetched_at=time.time(), max_age_days=45)
    stale = _write(tmp_path, PRESENT, id="stale",
                   fetched_at=time.time() - 100 * 86400, max_age_days=45)
    assert fresh.is_stale() is False
    assert stale.is_stale() is True
    assert stale.age_days() > 45


# ── MembershipSource: verified, instant, provenanced, on by default ───────────
@pytest.mark.asyncio
async def test_source_trust_semantics(tmp_path):
    _write(tmp_path, PRESENT, id="cigna")
    s = _store(tmp_path)
    src = MembershipSource(s.entry("cigna"), s)
    assert src.confidence == "verified"
    assert src.requires_network is False          # the whole point: no live call
    assert src.available()

    out = await src.check_many(PRESENT + [ABSENT])
    assert all(out[n] is True for n in PRESENT)
    assert out[ABSENT] is False

    prov = src.provenance_many(PRESENT)
    for n in PRESENT:
        assert prov[n]["source_url"] == "https://cigna.example/directory"
        assert prov[n]["fetched_at"] and prov[n]["stale"] is False


@pytest.mark.asyncio
async def test_source_regional_scoping_yields_unknown_out_of_state(tmp_path):
    _write(tmp_path, PRESENT, id="excellus", states=["NY"])
    s = _store(tmp_path)
    src = MembershipSource(s.entry("excellus"), s)
    out = await src.check_many_ctx({PRESENT[0]: {"state": "TX"}})
    assert out[PRESENT[0]] is None                 # out of scope -> unknown, never fabricated
    out = await src.check_many_ctx({PRESENT[0]: {"state": "NY"}})
    assert out[PRESENT[0]] is True


@pytest.mark.asyncio
async def test_not_loaded_source_answers_unknown(tmp_path):
    _write(tmp_path, PRESENT, id="cigna")
    s = _store(tmp_path)
    s.close()                                       # release the mmap so the blob unlinks
    (tmp_path / "cigna.roaring").unlink()
    s.load()                                        # reload: blob gone
    # A source pointed at a now-unloaded payer must answer None, never False.
    entry = membership.ManifestEntry(
        id="cigna", label="Cigna", category="commercial", level="payer", method="fhir-plannet",
        source_url="u", fetched_at=time.time(), count=0, file="cigna.roaring", sha256="")
    src = MembershipSource(entry, s)
    assert not src.available()
    out = await src.check_many(PRESENT)
    assert all(out[n] is None for n in PRESENT)


# ── Registry integration: harvested payer is verified-by-default, supersedes legacy ──
def test_healthz_freshness_and_coverage_read_the_manifest(tmp_path, temp_db):
    """A harvested payer's freshness comes from its manifest entry, so a stale BITMAP trips
    the /healthz dead-man's-switch (not just a stale sqlite ingest), and /coverage counts
    its NPIs from the store."""
    from app import coverage as cov
    from app import routes_ops
    from app.insurance import registry as global_registry

    _write(tmp_path, PRESENT, id="cigna", method="fhir-plannet",
           fetched_at=time.time() - 100 * 86400, max_age_days=45)  # stale
    old_dir, old_use = settings.membership_dir, settings.use_membership
    settings.membership_dir, settings.use_membership = str(tmp_path), True
    try:
        global_registry.build()
        fresh = routes_ops._data_freshness()
        cigna = next(s for s in fresh["sources"] if s["source"] == "cigna")
        assert cigna["stale"] is True and cigna["method"] == "fhir-plannet"
        assert cigna["count"] == len(PRESENT)
        assert "cigna" in fresh["stale"] and fresh["slos_met"] is False
        rep = cov.coverage_report(global_registry)
        assert rep["verified_counts"]["cigna"] == len(PRESENT)
    finally:
        if global_registry.membership_store:
            global_registry.membership_store.close()
        settings.membership_dir, settings.use_membership = old_dir, old_use
        global_registry.build()  # restore the hermetic (empty) registry


@pytest.mark.asyncio
async def test_membership_medicare_supersedes_legacy_and_is_on_by_default(tmp_path, temp_db):
    _write(tmp_path, PRESENT, id="medicare", label="Medicare (Original)",
           category="medicare", level="plan", method="cms-enrollment",
           source_url="https://data.cms.gov/enrollment")
    old_dir, old_use = settings.membership_dir, settings.use_membership
    settings.membership_dir, settings.use_membership = str(tmp_path), True
    try:
        reg = Registry()
        reg.build()
        medicare_sources = [s for s in reg.sources if s.id == "medicare"]
        # Exactly one medicare source, and it is the membership one (legacy superseded).
        assert len(medicare_sources) == 1
        assert isinstance(medicare_sources[0], MembershipSource)
        assert medicare_sources[0].requires_network is False

        # Verified-by-default: annotate WITHOUT `only` (an unfiltered search) still returns
        # the verified medicare answer, with provenance — no live call, nationwide.
        ann = await reg.annotate([{"npi": PRESENT[0], "stateAb": "CA"}])
        info = ann[PRESENT[0]]["medicare"]
        assert info["value"] is True and info["confidence"] == "verified"
        assert info["source_url"] and info["fetched_at"]
    finally:
        settings.membership_dir, settings.use_membership = old_dir, old_use
        if reg.membership_store:
            reg.membership_store.close()


def test_unreadable_manifest_starts_fresh(tmp_path):
    (tmp_path / "manifest.json").write_text("{ not valid json", encoding="utf-8")
    s = membership.MembershipStore(tmp_path)
    assert s.load() == 0          # no payers, no crash


def test_malformed_manifest_entry_is_skipped(tmp_path):
    import json
    _write(tmp_path, PRESENT, id="good")
    p = tmp_path / "manifest.json"
    data = json.loads(p.read_text(encoding="utf-8"))
    data["payers"]["bad"] = {"id": "bad"}   # missing required fields -> skipped, not fatal
    p.write_text(json.dumps(data), encoding="utf-8")
    s = membership.MembershipStore(tmp_path)
    s.load()
    assert s.loaded("good") and not s.loaded("bad")
    s.close()


# ── lazy decoding: what makes plan-level scale affordable ─────────────────────
def test_load_indexes_without_decoding_any_bitmap(tmp_path):
    """Indexing must be O(manifest), not O(bytes on disk). Eagerly decoding was fine at 3
    payers; at thousands of Marketplace plans it would hash and copy hundreds of MB on
    every serverless cold start to answer a search touching a handful of them."""
    for i in range(5):
        _write(tmp_path, PRESENT, id=f"payer{i}", label=f"Payer {i}")
    s = membership.MembershipStore(tmp_path)
    assert s.load() == 5
    assert s.resident() == 0
    # Metadata is served straight from the manifest — still no decode.
    assert s.count("payer0") == len(PRESENT)
    assert s.loaded("payer0") is True
    assert len(s.payers()) == 5
    assert s.resident() == 0
    s.close()


def test_a_bitmap_is_decoded_only_when_actually_asked(tmp_path):
    for i in range(3):
        _write(tmp_path, PRESENT, id=f"payer{i}", label=f"Payer {i}")
    s = membership.MembershipStore(tmp_path)
    s.load()
    assert s.has("payer1", PRESENT[0]) is True
    assert s.resident() == 1              # only the payer we asked about
    s.close()


def test_a_malformed_npi_is_answered_without_decoding(tmp_path):
    """A junk query must not be able to force disk work — otherwise a hostile caller could
    make every request decode every payer."""
    _write(tmp_path, PRESENT)
    s = membership.MembershipStore(tmp_path)
    s.load()
    assert s.has("cigna", BOGUS) is False
    assert s.has_many("cigna", [BOGUS, "nope"]) == set()
    assert s.resident() == 0
    s.close()


def test_least_recently_used_bitmaps_are_evicted(tmp_path):
    for i in range(4):
        _write(tmp_path, PRESENT, id=f"payer{i}", label=f"Payer {i}")
    s = membership.MembershipStore(tmp_path, max_resident=2)
    s.load()
    s.has("payer0", PRESENT[0])
    s.has("payer1", PRESENT[0])
    assert s.resident() == 2
    s.has("payer2", PRESENT[0])
    assert s.resident() == 2               # capped, not growing
    # Evicted payers still answer correctly — eviction is a cache concern, never a
    # correctness one.
    assert s.has("payer0", PRESENT[0]) is True
    assert s.has("payer0", ABSENT) is False
    s.close()


def test_a_corrupt_blob_is_still_refused_when_it_is_finally_decoded(tmp_path):
    """Verification moved from startup to first use — it did not weaken. A blob whose
    bytes don't match the manifest hash must never answer, and must be refused once rather
    than re-read on every request."""
    entry = _write(tmp_path, PRESENT)
    # Same length, different bytes: passes the cheap size check at load, fails sha256.
    (tmp_path / entry.file).write_bytes(b"\x00" * entry.size)
    s = membership.MembershipStore(tmp_path)
    assert s.load() == 1                   # indexed: size still matches
    assert s.has("cigna", PRESENT[0]) is False
    assert s.loaded("cigna") is False      # dropped after the hash mismatch
    assert s.resident() == 0
    s.close()


def test_a_wrong_sized_blob_is_caught_at_index_time(tmp_path):
    entry = _write(tmp_path, PRESENT)
    (tmp_path / entry.file).write_bytes(b"truncated")
    s = membership.MembershipStore(tmp_path)
    assert s.load() == 0
    assert s.has("cigna", PRESENT[0]) is False
    s.close()


def test_healthz_stays_small_when_a_whole_rail_goes_stale(tmp_path, temp_db):
    """The failure this endpoint must survive is a RAIL stalling, not one payer. With
    thousands of plan-level entries the payload must stay bounded, and the dead-man's
    switch must still trip on a stalled pipeline."""
    from app import routes_ops
    from app.insurance import registry as global_registry

    stale_at = time.time() - 400 * 86400
    for i in range(120):
        _write(tmp_path, PRESENT, id=f"plan{i:03d}", label=f"Plan {i}", method="marketplace-mrf",
               fetched_at=stale_at, max_age_days=35)
    old_dir, old_use = settings.membership_dir, settings.use_membership
    settings.membership_dir, settings.use_membership = str(tmp_path), True
    try:
        global_registry.build()
        fresh = routes_ops._data_freshness()
        assert fresh["tracked"] == 120
        assert fresh["stale_count"] == 120
        assert len(fresh["sources"]) <= routes_ops._MAX_SOURCE_ROWS
        assert len(fresh["stale"]) <= routes_ops._MAX_SOURCE_ROWS
        assert fresh["sources_truncated"] is True
        assert fresh["slos_met"] is False        # a whole rail stale IS a stalled pipeline
    finally:
        if global_registry.membership_store:
            global_registry.membership_store.close()
        settings.membership_dir, settings.use_membership = old_dir, old_use
        global_registry.build()


def test_one_flaky_payer_does_not_take_the_service_down(tmp_path, temp_db):
    """The counterpart: with 40 healthy plans, a single stale one is reported but must not
    503 the whole service — that would make coverage growth a reliability liability."""
    from app import routes_ops
    from app.insurance import registry as global_registry

    for i in range(40):
        _write(tmp_path, PRESENT, id=f"plan{i:03d}", label=f"Plan {i}", method="marketplace-mrf",
               max_age_days=35)
    _write(tmp_path, PRESENT, id="flaky", label="Flaky", method="marketplace-mrf",
           fetched_at=time.time() - 400 * 86400, max_age_days=35)
    old_dir, old_use = settings.membership_dir, settings.use_membership
    settings.membership_dir, settings.use_membership = str(tmp_path), True
    try:
        global_registry.build()
        fresh = routes_ops._data_freshness()
        assert "flaky" in fresh["stale"]         # reported...
        assert fresh["slos_met"] is True         # ...but not a service-down event
    finally:
        if global_registry.membership_store:
            global_registry.membership_store.close()
        settings.membership_dir, settings.use_membership = old_dir, old_use
        global_registry.build()


def test_a_manifest_written_before_the_size_field_still_loads(tmp_path):
    """Every entry in the currently-deployed manifest predates `size`, so a missing field
    must mean "skip the cheap check", never "refuse the payer" — otherwise shipping this
    change would blank out live coverage until the next harvest rewrote the manifest."""
    import json

    entry = _write(tmp_path, PRESENT)
    p = tmp_path / "manifest.json"
    data = json.loads(p.read_text(encoding="utf-8"))
    del data["payers"][entry.id]["size"]           # exactly what production looks like today
    p.write_text(json.dumps(data), encoding="utf-8")

    s = membership.MembershipStore(tmp_path)
    assert s.load() == 1
    assert s.entry("cigna").size == 0
    assert s.has("cigna", PRESENT[0]) is True      # and the sha256 check still runs
    s.close()

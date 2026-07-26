"""Insurance acceptance — the Registry that merges sources into the two-tier confidence
model. Source classes live in sources.py + fhir_source.py and are re-exported here so the
public import path (`from app.insurance import FhirPlanNetSource, Registry, ...`) is stable.

A plan id may have several sources (e.g. a verified TiC ingest + an estimated catalog
entry for "aetna"); the Registry merges them and a verified answer always wins. A source
may answer True / False / None; None ("unknown") is never turned into a yes.
"""
import logging
from typing import Any

from . import planet_registry
from .catalog import CATEGORY_ORDER, PAYER_CATALOG, category_label
from .config import settings
from .fhir_source import FhirPlanNetSource
from .membership import MembershipStore
from .sources import (
    _CONFIDENCE_RANK,
    Answer,
    Ctx,
    EstimatedPayerSource,
    InsuranceSource,
    MedicareSource,
    MembershipSource,
    TicSource,
)

log = logging.getLogger("innetwork.insurance")

# Re-exported for a stable import path (tests, app.verify_payers, app.main).
__all__ = ["Answer", "Ctx", "InsuranceSource", "MedicareSource", "TicSource",
           "FhirPlanNetSource", "MembershipSource", "EstimatedPayerSource",
           "Registry", "registry"]

class Registry:
    def __init__(self) -> None:
        self.sources: list[InsuranceSource] = []
        # The mmap'd membership store, kept alive for the process lifetime so its bitmaps
        # stay mapped. None until build() runs (or when membership is disabled).
        self.membership_store: MembershipStore | None = None

    def build(self) -> None:
        sources: list[InsuranceSource] = []

        # 1) Verified tier — harvested membership bitmaps (the rebuilt core: an instant,
        # local, always-on set-membership test). A harvested payer SUPERSEDES its legacy
        # live-FHIR / sqlite source for the same id, so it costs zero per-NPI live calls.
        membership_ids: set[str] = set()
        self.membership_store = None
        if settings.use_membership:
            store = MembershipStore(settings.membership_dir)
            try:
                store.load()
            except Exception as e:
                log.warning("Membership store failed to load from %s: %s: %s",
                            settings.membership_dir, type(e).__name__, e)
            else:
                for m_entry in store.payers():
                    sources.append(MembershipSource(m_entry, store))
                    membership_ids.add(m_entry.id)
                self.membership_store = store

        # 2) Medicare — legacy sqlite index, only if not already served by a bitmap.
        if "medicare" not in membership_ids:
            sources.append(MedicareSource())

        # 3) Verified commercial payers still checked live via FHIR Plan-Net. Operator
        # payers.json takes precedence; the validated public registry (C1) fills in the
        # rest. Any id already served by a local bitmap is skipped — no live calls for it.
        fhir_cfgs = list(settings.load_payers())
        configured_ids = {(c or {}).get("id") for c in fhir_cfgs}
        for cfg in planet_registry.validated_payer_configs():
            if cfg["id"] not in configured_ids:
                fhir_cfgs.append(cfg)
        for cfg in fhir_cfgs:
            if (cfg or {}).get("id") in membership_ids:
                continue
            try:
                sources.append(FhirPlanNetSource(cfg))
            except Exception as e:
                # A malformed payers.json entry shouldn't kill startup, but it must
                # not vanish silently either — name the offending entry.
                log.warning("Skipping FHIR payer entry %r: %s: %s",
                            (cfg or {}).get("id", cfg), type(e).__name__, e)
        # 4) Verified commercial payers from Transparency-in-Coverage sqlite ingests.
        # Availability is checked live (short TTL) like Medicare, so a payer ingested
        # AFTER startup surfaces within seconds. Skipped when a bitmap already serves it.
        for entry in PAYER_CATALOG:
            if entry["id"] in membership_ids:
                continue
            sources.append(
                TicSource(entry["id"], entry["label"], entry.get("category", "commercial"))
            )
        # 5) Estimated tier: every catalog payer is also offered as a labeled estimate.
        for entry in PAYER_CATALOG:
            sources.append(EstimatedPayerSource(entry))
        self.sources = sources

    def available(self) -> list[InsuranceSource]:
        return [s for s in self.sources if s.available()]

    def _sources_by_plan(self) -> dict[str, list[InsuranceSource]]:
        by_plan: dict[str, list[InsuranceSource]] = {}
        for s in self.available():
            by_plan.setdefault(s.id, []).append(s)
        return by_plan

    def plans(self, state: str = "") -> list[dict[str, Any]]:
        """Flat list of filterable plans, best confidence per plan id.

        `state` (a 2-letter code) narrows the list to plans that actually operate there:
        national plans plus the regional ones scoped to that state. This is not cosmetic.
        Rail 4 turns the catalog into thousands of plan-level entries, and a Texan has no
        use for an Alaska-only Marketplace plan — offering it is noise at best, and at
        worst invites a filter that can only ever return nothing.
        """
        want = (state or "").strip().upper()
        out: list[dict[str, Any]] = []
        for plan_id, srcs in self._sources_by_plan().items():
            best = max(srcs, key=lambda s: _CONFIDENCE_RANK.get(s.confidence, 0))
            states = best.states
            if want and states is not None and want not in states:
                continue
            out.append({
                "id": plan_id, "label": best.label, "category": best.category,
                "payer": best.payer, "confidence": best.confidence, "kind": best.kind,
                "level": best.level, "filterable": best.discriminates(),
                "states": sorted(states) if states else None,
            })
        out.sort(key=lambda p: (CATEGORY_ORDER.get(p["category"], 99), p["label"].lower()))
        return out

    def categories(self, state: str = "") -> list[dict[str, Any]]:
        """Plans grouped by coverage category, in canonical display order."""
        groups: dict[str, list[dict[str, Any]]] = {}
        for p in self.plans(state):
            groups.setdefault(p["category"], []).append(p)
        ordered = sorted(groups.items(), key=lambda kv: CATEGORY_ORDER.get(kv[0], 99))
        return [{"id": cid, "label": category_label(cid), "plans": plans} for cid, plans in ordered]

    async def check_all(self, npi: str, state: str = "") -> dict[str, Any]:
        """Single-NPI lookup. Without provider context, estimates stay unknown."""
        ann = await self.annotate([{"npi": npi, "state": state}])
        return ann.get(npi, {})

    async def annotate(
        self, providers: list[dict[str, Any]], only: list[str] | None = None
    ) -> dict[str, dict[str, Any]]:
        """providers: [{"npi":..., "state":...}] -> {npi: {plan_id: {value,confidence,source}}}.

        For each plan id, the best available answer across its sources wins:
        a verified True/False beats an estimate; an estimated True beats nothing.
        """
        contexts: Ctx = {}
        for p in providers:
            npi = p.get("npi")
            if npi:
                contexts[npi] = {"state": p.get("state") or p.get("stateAb") or ""}
        npis = list(contexts.keys())
        result: dict[str, dict[str, Any]] = {npi: {} for npi in npis}
        # Verified winners grouped by their source, so provenance is fetched once per
        # source rather than per result.
        verified_by_source: dict[InsuranceSource, list[tuple[str, str]]] = {}

        for plan_id, srcs in self._sources_by_plan().items():
            if only and plan_id not in only:
                continue
            # Query each source for this plan; merge by confidence then definiteness.
            per_source: list[tuple[InsuranceSource, dict[str, Answer]]] = []
            for s in srcs:
                # A live per-NPI directory call only fires when the plan is explicitly
                # requested (`only`). On an unfiltered search we never hit a payer's FHIR
                # directory for every provider in the pool — it would add a round-trip per
                # NPI to a search the user didn't even scope to that payer.
                if only is None and s.requires_network:
                    continue
                try:
                    per_source.append((s, await s.check_many_ctx(contexts)))
                except Exception as e:
                    # One source failing degrades its plan to "unknown" for this
                    # batch; the rest still answer. Record which source broke.
                    log.warning("Insurance source %r failed for %d NPIs: %s: %s",
                                s.id, len(npis), type(e).__name__, e)
                    per_source.append((s, {n: None for n in npis}))
            for npi in npis:
                best: tuple[int, int, Answer, InsuranceSource] | None = None
                for s, m in per_source:
                    v = m.get(npi)
                    if v is None:
                        continue
                    rank = _CONFIDENCE_RANK.get(s.confidence, 0)
                    cand = (rank, 1 if v is True else 0, v, s)
                    if best is None or cand[:2] > best[:2]:
                        best = cand
                if best is not None:
                    _, _, value, s = best
                    result[npi][plan_id] = {
                        "value": value, "confidence": s.confidence, "source": s.id,
                        "level": s.level,
                    }
                    if s.confidence == "verified":
                        verified_by_source.setdefault(s, []).append((npi, plan_id))

        # Stamp provenance (source URL + fetch date) onto every verified result, so a
        # green badge is always traceable to a real source — the A3 trust invariant.
        for s, winners in verified_by_source.items():
            prov = s.provenance_many([npi for npi, _ in winners])
            for npi, plan_id in winners:
                pv = prov.get(str(npi))
                if pv:
                    result[npi][plan_id]["source_url"] = pv.get("source_url", "")
                    result[npi][plan_id]["fetched_at"] = pv.get("fetched_at")
                    # A harvested payer past its freshness SLO stays a real verified True
                    # (the data is there), but carries `stale` so the serve layer demotes
                    # its badge to "confirmed <date>" instead of a fresh green — never a
                    # silent stale green, and never flipped to a "no".
                    if pv.get("stale"):
                        result[npi][plan_id]["stale"] = True
        return result


registry = Registry()

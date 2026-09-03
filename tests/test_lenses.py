"""Six readers over one body of evidence.

The tests that matter are the ones about what a lens is *not* allowed to do.
Selecting by domain is exactly how a coverage gap disappears — filter a threat
brief to what concerns a crew and "AD 2.10 was never read" concerns nobody, so
it vanishes, leaving a clean page about an aerodrome whose obstacle environment
is unknown. Every lens therefore states the sections its reader depends on, and
those gaps survive every filter.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from aeropub.aip import (
    AipCoverage,
    HoldingState,
    SectionHolding,
    aerodrome_sections,
    section,
)
from aeropub.airac import AiracCycle
from aeropub.bulletin import between_cycles
from aeropub.dossier import build
from aeropub.facts import Fact, FactStore, Precedence
from aeropub.horizon import horizon
from aeropub.lenses import LENSES, Audience, Lens, LensView, lens_for, view
from aeropub.provenance import SourceRef

N1 = AiracCycle.from_identifier("2609")
N2 = AiracCycle.from_identifier("2610")
DAY_BEFORE = N2.effective_date - timedelta(days=1)
NOW = datetime(2026, 10, 5, 6, 0, tzinfo=timezone.utc)


def ref(document: str = "AIP AMDT 09/26", locator: str = "AD 2.13") -> SourceRef:
    return SourceRef(
        source_id="QA-CAA", document=document, locator=locator,
        retrieved_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
        content_hash="b" * 64, parser_id="eaip-eurocontrol", parser_version="0.1.0",
    )


def fact(entity, attribute, value, valid_from, valid_to=None,
         precedence=Precedence.AIP, document="AIP AMDT 09/26", locator="AD 2.13"):
    return Fact(entity=entity, attribute=attribute, value=value,
                valid_from=valid_from, valid_to=valid_to,
                source=ref(document, locator), precedence=precedence)


@pytest.fixture
def store():
    facts = FactStore()
    facts.add(fact("OTHH", "rffs_category", 10, date(2026, 1, 1), DAY_BEFORE,
                   locator="AD 2.6"))
    facts.add(fact("OTHH", "rffs_category", 9, N2.effective_date,
                   document="AIP AMDT 10/26", locator="AD 2.6"))
    facts.add(fact("OTHH/RWY34L", "lda_m", 3900, date(2026, 1, 1)))
    facts.add(fact("OTHH/RWY34L", "lda_m", 3500, date(2026, 9, 10), date(2026, 10, 20),
                   precedence=Precedence.SUP, document="AIP SUP 07/26"))
    facts.add(fact("OTHH/RWY34L", "papi_angle", 3.2, N2.effective_date,
                   document="AIP AMDT 10/26", locator="AD 2.14"))
    return facts


def _coverage(omit: tuple[str, ...] = ()) -> AipCoverage:
    return AipCoverage([
        SectionHolding(section=s, entity="OTHH", state=HoldingState.HELD,
                       source=ref("AIP", s.code))
        for s in aerodrome_sections() if s.code not in omit
    ])


@pytest.fixture
def evidence(store):
    coverage = _coverage(omit=("AD 2.10",))
    return {
        "dossier": build("OTHH", facts=store, coverage=coverage, as_at=NOW, cycle=N2),
        "bulletin": between_cycles(store, "OTHH", N1, N2,
                                   coverage_before=coverage, coverage_after=coverage),
        "ahead": horizon(store, "OTHH", from_date=NOW.date(), days=60),
    }


class TestGapsSurviveEveryFilter:
    """The invariant that makes a filtered document safe to read."""

    def test_a_missing_section_a_reader_depends_on_makes_the_view_unsound(self, evidence):
        brief = view(Audience.FLIGHT_CREW, "OTHH", as_at=NOW, **evidence)
        assert not brief.is_sound
        assert [e.section.code for e in brief.blocking_gaps] == ["AD 2.10"]

    def test_the_warning_comes_before_the_content(self, evidence):
        rendered = view(Audience.FLIGHT_CREW, "OTHH", as_at=NOW, **evidence).render()
        assert "NOT SOUND" in rendered
        assert rendered.index("NOT SOUND") < rendered.index("WHAT CHANGED")
        assert "Do not take an absence below as an all-clear" in rendered

    def test_a_gap_outside_a_readers_dependencies_does_not_block_them(self, evidence):
        # Obstacles are not what decides dispatchability, so the same missing
        # section leaves the digest sound. Two readers, one body of evidence,
        # two correct and different answers.
        digest = view(Audience.DISPATCH, "OTHH", as_at=NOW, **evidence)
        assert digest.is_sound
        assert digest.blocking_gaps == ()

    def test_a_complete_reading_is_stated_as_such(self, store):
        full = _coverage()
        brief = view(
            Audience.FLIGHT_CREW, "OTHH", as_at=NOW,
            dossier=build("OTHH", facts=store, coverage=full, as_at=NOW),
        )
        assert brief.is_sound
        assert "Sound: all" in brief.render()

    def test_an_incomplete_change_list_is_carried_into_the_view(self, evidence):
        brief = view(Audience.FLIGHT_CREW, "OTHH", as_at=NOW, **evidence)
        assert "not complete" in brief.coverage_note
        assert "The change list is not complete" in brief.render()


class TestSelection:
    def test_each_reader_sees_what_concerns_them(self, evidence):
        brief = view(Audience.FLIGHT_CREW, "OTHH", as_at=NOW, **evidence)
        digest = view(Audience.DISPATCH, "OTHH", as_at=NOW, **evidence)
        ats = view(Audience.ATS, "OTHH", as_at=NOW, **evidence)

        assert "papi_angle" in {c.attribute for c in brief.changes}
        assert "papi_angle" not in {c.attribute for c in digest.changes}
        assert ats.changes == ()

    def test_fire_category_reaches_the_crew_as_well_as_dispatch(self, evidence):
        # Plan section 21 names RFFS in the threat brief: a crew choosing a
        # diversion needs it, not only the dispatcher releasing the flight.
        brief = view(Audience.FLIGHT_CREW, "OTHH", as_at=NOW, **evidence)
        assert "rffs_category" in {c.attribute for c in brief.changes}
        assert "AD 2.6" in lens_for(Audience.FLIGHT_CREW).depends_on

    def test_the_engineering_study_sees_everything(self, evidence):
        study = view(Audience.AERODROME_STUDY, "OTHH", as_at=NOW, **evidence)
        assert len(study.changes) == len(evidence["bulletin"].changes)
        assert len(study.lens.depends_on) == 25

    def test_the_forward_view_is_filtered_the_same_way(self, evidence):
        digest = view(Audience.DISPATCH, "OTHH", as_at=NOW, **evidence)
        assert {t.attribute for t in digest.ahead} == {"lda_m"}
        assert digest.unannounced == digest.ahead

    def test_selection_is_by_section_because_domains_are_too_coarse(self, evidence):
        # dispatch covers both aerodrome dispatchability and flight planning;
        # procedures covers both ATS procedure and runway lighting. Selecting
        # on domain alone put every fire category change into an ATS document.
        ats = lens_for(Audience.ATS)
        assert ats.admits(["dispatch"]) is False
        assert ats.admits_section("AD 2.17")
        assert not ats.admits_section("AD 2.6")
        # AD 2.6 carries the dispatch domain, and is still correctly excluded.
        assert not ats.admits_content("AD 2.6", ["dispatch", "suitability"])

    def test_content_with_no_aip_section_falls_back_to_domains(self, evidence):
        # Losing it entirely would be worse than routing it broadly.
        digest = lens_for(Audience.DISPATCH)
        assert digest.admits_content(None, ["dispatch"])
        assert not digest.admits_content(None, ["charts"])

    def test_an_unplaced_change_reaches_the_readers_it_concerns(self, store):
        store.add(fact("OTHH", "runway_condition_scheme", "GRF",
                       date(2026, 1, 1), DAY_BEFORE, locator="AD 2.7"))
        bulletin = between_cycles(store, "OTHH", N1, N2)
        unplaced = [c for c in bulletin.changes if c.section is None]
        assert [c.attribute for c in unplaced] == ["runway_condition_scheme"]

        # It has no section and no rule, so it has no domains — every filter
        # rejects it. It still has to land somewhere, and it lands with the two
        # readers whose remit includes what nobody has modelled.
        study = view(Audience.AERODROME_STUDY, "OTHH", as_at=NOW, bulletin=bulletin)
        worklist = view(Audience.AIS, "OTHH", as_at=NOW, bulletin=bulletin)
        assert "runway_condition_scheme" in {c.attribute for c in study.changes}
        assert "runway_condition_scheme" in {c.attribute for c in worklist.changes}

    def test_unclassifiable_content_reaches_someone_from_every_lens_set(self, store):
        # The invariant: no combination of lenses may drop it entirely.
        store.add(fact("OTHH", "runway_condition_scheme", "GRF",
                       date(2026, 1, 1), DAY_BEFORE, locator="AD 2.7"))
        bulletin = between_cycles(store, "OTHH", N1, N2)
        reached = {
            attribute
            for audience in Audience
            for attribute in {
                c.attribute
                for c in view(audience, "OTHH", as_at=NOW, bulletin=bulletin).changes
            }
        }
        assert {c.attribute for c in bulletin.changes} <= reached

    def test_an_unannounced_change_is_marked_in_the_output(self, evidence):
        rendered = view(Audience.DISPATCH, "OTHH", as_at=NOW, **evidence).render()
        assert "nothing will be published" in rendered


class TestLensDefinitions:
    def test_all_six_audiences_are_defined(self):
        assert set(LENSES) == set(Audience)

    def test_every_lens_names_a_reader_and_a_decision(self):
        for lens in LENSES.values():
            assert lens.reader.strip()
            assert lens.purpose.strip().endswith(".")
            assert lens.domains

    def test_every_dependency_is_a_real_aip_section(self):
        for lens in LENSES.values():
            for code in lens.depends_on:
                assert section(code).code == code

    def test_a_lens_with_an_unknown_domain_is_refused(self):
        with pytest.raises(ValueError, match="unknown domains"):
            Lens(audience=Audience.ATS, title="x", reader="y", purpose="z.",
                 domains=frozenset({"perfromance"}), depends_on=())

    def test_a_lens_with_an_invented_section_is_refused(self):
        with pytest.raises(KeyError):
            Lens(audience=Audience.ATS, title="x", reader="y", purpose="z.",
                 domains=frozenset({"airspace"}), depends_on=("AD 2.99",))

    def test_the_two_partial_lenses_declare_what_they_lack(self):
        # Route and airspace work needs ENR facts nobody has connected yet.
        # Declared, so the document says it rather than quietly omitting it.
        assert lens_for(Audience.ROUTE_STUDY).needs_unbuilt
        assert lens_for(Audience.ATS).needs_unbuilt
        assert not lens_for(Audience.DISPATCH).needs_unbuilt

    def test_a_partial_lens_says_so_in_its_own_output(self, evidence):
        rendered = view(Audience.ROUTE_STUDY, "OTHH", as_at=NOW, **evidence).render()
        assert "partial by construction" in rendered
        assert "ENR" in rendered


class TestAssembly:
    def test_a_view_built_with_nothing_still_names_its_reader_and_purpose(self):
        empty = view(Audience.AIS, "OTHH", as_at=NOW)
        rendered = empty.render()
        assert "AIS and AIM team" in rendered
        assert "Nothing in this reader's domains" in rendered
        assert empty.summary()["changes"] == 0

    def test_conduct_findings_reach_the_document(self, evidence):
        from aeropub.notam_register import (
            NotamRegister, RegisteredNotam, Subject, SubjectKind,
        )
        from aeropub.quality import assess_quality

        register = NotamRegister([
            RegisteredNotam(
                identifier="A1201/26",
                subjects=(Subject(entity="OTHH/RWY34L", kind=SubjectKind.RUNWAY),),
                source=ref("NOTAM A1201/26", "NOTAM"),
                text="RWY 34L PAPI U/S",
                effective_start=datetime(2026, 1, 5, tzinfo=timezone.utc),
                effective_end=datetime(2026, 12, 31, tzinfo=timezone.utc),
            )
        ])
        report = assess_quality(register=register, as_at=NOW)
        worklist = view(Audience.AIS, "OTHH", as_at=NOW, conduct=report, **evidence)
        assert worklist.conduct
        assert "HOW THIS IS BEING PUBLISHED" in worklist.render()

    def test_the_entity_is_normalised(self, evidence):
        assert view(Audience.DISPATCH, " othh ", as_at=NOW, **evidence).entity == "OTHH"

    def test_a_naive_moment_is_refused(self, evidence):
        with pytest.raises(ValueError, match="timezone-aware"):
            view(Audience.DISPATCH, "OTHH", as_at=datetime(2026, 10, 5), **evidence)

    def test_an_audience_can_be_named_by_string(self, evidence):
        assert view("dispatch", "OTHH", as_at=NOW, **evidence).lens.audience is (
            Audience.DISPATCH
        )

    def test_an_unknown_audience_is_refused(self):
        with pytest.raises(ValueError):
            lens_for("marketing")

    def test_the_summary_is_serialisable_for_a_status_board(self, evidence):
        import json

        summary = view(Audience.FLIGHT_CREW, "OTHH", as_at=NOW, **evidence).summary()
        assert json.loads(json.dumps(summary))["sound"] is False


class TestNoLensComputesAnything:
    def test_a_lens_reorders_and_selects_but_never_derives(self, evidence):
        # Six implementations of one calculation would eventually disagree, and
        # the one that disagreed would be the one somebody flew on.
        brief = view(Audience.FLIGHT_CREW, "OTHH", as_at=NOW, **evidence)
        from_bulletin = {id(c) for c in evidence["bulletin"].changes}
        assert all(id(c) in from_bulletin for c in brief.changes)

        from_horizon = {id(t) for t in evidence["ahead"].transitions}
        assert all(id(t) in from_horizon for t in brief.ahead)

"""The complete aerodrome dossier.

Assembled from the four components already built, and asserted against real
data: the NOTAM and the aerodrome values come from the AIXM the FAA issued,
and every citation resolves back to those archived bytes.

The values are recorded at NOTAM precedence because that is what they are —
the FAA's statement of the object as it stood when the event was issued, not
a publication of the aerodrome's AIP baseline. Filing them as AIP would be the
quiet over-claim this project exists to avoid.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from aeropub.aip import (
    AipCoverage,
    HoldingState,
    SectionHolding,
    aerodrome_sections,
    section,
)
from aeropub.airac import AiracCycle
from aeropub.archive import Archive
from aeropub.dossier import build
from aeropub.faa.aixm import read_notams
from aeropub.faa.register import register_feed
from aeropub.facts import Fact, FactStore, Precedence
from aeropub.notam_register import ForceState

FIXTURE = Path(__file__).parent / "fixtures" / "faa" / "nms-initial-load-sample.raw"
AS_AT = datetime(2025, 9, 1, 6, 0, tzinfo=timezone.utc)
CYCLE = AiracCycle.from_identifier("2509")


@pytest.fixture(scope="module")
def notams():
    return read_notams(str(FIXTURE))


@pytest.fixture
def entry(tmp_path):
    archive = Archive(tmp_path / "raw")
    return archive.put(
        FIXTURE.read_bytes(),
        source_id="FAA-NMS-PROD",
        url="https://api-nms.aim.faa.gov/nmsapi/v1/notams/il",
        retrieved_at=datetime(2025, 9, 12, 17, 25, tzinfo=timezone.utc),
    )


@pytest.fixture
def ref(notams, entry):
    return notams[0].source_ref(entry)


@pytest.fixture
def register(notams, entry):
    return register_feed(notams, entry)


@pytest.fixture
def store(notams, ref):
    aerodrome = notams[0].aerodromes()[0]
    facts = FactStore()
    for entity, attribute, value in (
        ("8WC", "aerodrome_name", aerodrome.name),
        ("8WC", "latitude", aerodrome.latitude),
        ("8WC", "longitude", aerodrome.longitude),
        ("8WC/RWY20", "runway_designator", "20"),
    ):
        facts.add(
            Fact(entity=entity, attribute=attribute, value=value,
                 valid_from=date(2025, 8, 21), source=ref, precedence=Precedence.NOTAM)
        )
    return facts


@pytest.fixture
def coverage(ref):
    return AipCoverage([
        SectionHolding(section=section("AD 2.1"), entity="8WC",
                       state=HoldingState.HELD, source=ref, cycle=CYCLE),
        SectionHolding(section=section("AD 2.2"), entity="8WC",
                       state=HoldingState.HELD, source=ref, cycle=CYCLE),
        SectionHolding(section=section("AD 2.16"), entity="8WC",
                       state=HoldingState.ABSENT,
                       detail="no helicopter landing area listed in AD 0.4"),
        SectionHolding(section=section("AD 2.10"), entity="8WC",
                       state=HoldingState.FAILED,
                       detail="obstacle table would not parse"),
    ])


@pytest.fixture
def dossier(store, coverage, register):
    return build("8WC", facts=store, coverage=coverage, register=register,
                 as_at=AS_AT, cycle=CYCLE)


class TestCompleteness:
    def test_every_ad_2_section_appears_whether_held_or_not(self, dossier):
        # The design position of the whole module. A tidy report listing only
        # what we happen to hold is indistinguishable from a complete one, and
        # a crew reading it has no way to know AD 2.10 was never checked.
        assert len(dossier.sections) == 25
        assert [e.section.code for e in dossier.sections] == [
            s.code for s in aerodrome_sections()
        ]

    def test_the_four_states_partition_the_sections(self, dossier):
        counts = dossier.summary()
        assert counts["held"] + counts["absent"] + counts["gaps"] == 25
        assert (counts["held"], counts["absent"], counts["gaps"]) == (2, 1, 22)

    def test_a_dossier_with_gaps_is_not_complete(self, dossier):
        assert not dossier.is_complete
        assert "AD 2.13" in [e.section.code for e in dossier.gaps]

    def test_what_the_state_does_not_publish_is_not_our_gap(self, dossier):
        assert [e.section.code for e in dossier.absent] == ["AD 2.16"]
        assert "AD 2.16" not in [e.section.code for e in dossier.gaps]

    def test_a_dossier_built_with_nothing_still_lists_every_section(self):
        # An omission has to be visible. Building with no store, no coverage
        # and no register must not produce a short report.
        empty = build("OTHH", as_at=AS_AT)
        assert len(empty.sections) == 25
        assert len(empty.gaps) == 25
        assert empty.values() == ()


class TestValuePlacement:
    def test_values_file_under_the_section_icao_publishes_them_in(self, dossier):
        assert [v.attribute for v in dossier.section("AD 2.1").values] == [
            "aerodrome_name"
        ]
        assert sorted(v.attribute for v in dossier.section("AD 2.2").values) == [
            "latitude", "longitude",
        ]

    def test_an_unmapped_attribute_is_shown_not_guessed_into_a_section(self, dossier):
        # A value filed under a plausible section reads as though that section
        # said it, which is worse than an admitted loose end.
        assert [v.attribute for v in dossier.unplaced] == ["runway_designator"]
        assert all(
            "runway_designator" not in [v.attribute for v in e.values]
            for e in dossier.sections
        )

    def test_a_runway_scoped_value_says_which_runway(self, dossier):
        assert dossier.unplaced[0].scope == "RWY20"
        assert dossier.section("AD 2.1").values[0].scope == "aerodrome"

    def test_every_value_carries_a_citation_that_resolves(self, dossier, entry, tmp_path):
        archive = Archive(tmp_path / "raw")
        for value in dossier.values():
            assert archive.get(value.fact.source.content_hash) == FIXTURE.read_bytes()

    def test_values_come_from_the_real_document(self, dossier):
        assert dossier.section("AD 2.1").values[0].value == "WASHINGTON COUNTY"
        latitudes = [v.value for v in dossier.section("AD 2.2").values
                     if v.attribute == "latitude"]
        assert latitudes == [pytest.approx(37.92919525)]


class TestEffectiveState:
    def test_the_dossier_shows_what_is_in_force_not_what_the_aip_said(self, ref):
        # A supplement or NOTAM covering a value is the point of the CES. A
        # dossier that printed the base AIP figure would be confidently wrong.
        facts = FactStore()
        facts.add(Fact(entity="OTHH/RWY34L", attribute="lda_m", value=3900,
                       valid_from=date(2025, 1, 1), source=ref, precedence=Precedence.AIP))
        facts.add(Fact(entity="OTHH/RWY34L", attribute="lda_m", value=3100,
                       valid_from=date(2025, 8, 1), valid_to=date(2025, 12, 1),
                       source=ref, precedence=Precedence.NOTAM))

        dossier = build("OTHH", facts=facts, as_at=AS_AT)
        values = dossier.section("AD 2.13").values
        assert [v.value for v in values] == [3100]
        assert values[0].fact.precedence is Precedence.NOTAM

    def test_the_layer_beneath_resurfaces_when_the_overlay_expires(self, ref):
        facts = FactStore()
        facts.add(Fact(entity="OTHH/RWY34L", attribute="lda_m", value=3900,
                       valid_from=date(2025, 1, 1), source=ref, precedence=Precedence.AIP))
        facts.add(Fact(entity="OTHH/RWY34L", attribute="lda_m", value=3100,
                       valid_from=date(2025, 8, 1), valid_to=date(2025, 8, 31),
                       source=ref, precedence=Precedence.NOTAM))

        after = build("OTHH", facts=facts, as_at=AS_AT, on=date(2025, 9, 1))
        assert [v.value for v in after.section("AD 2.13").values] == [3900]

    def test_an_attribute_with_nothing_in_force_is_absent_not_null(self, ref):
        facts = FactStore()
        facts.add(Fact(entity="OTHH/RWY34L", attribute="lda_m", value=3100,
                       valid_from=date(2024, 1, 1), valid_to=date(2024, 12, 31),
                       source=ref, precedence=Precedence.AIP))
        dossier = build("OTHH", facts=facts, as_at=AS_AT, on=date(2025, 9, 1))
        assert dossier.section("AD 2.13").values == ()

    def test_facts_for_another_aerodrome_are_not_gathered(self, ref):
        facts = FactStore()
        facts.add(Fact(entity="OTBD/RWY15", attribute="lda_m", value=4570,
                       valid_from=date(2025, 1, 1), source=ref, precedence=Precedence.AIP))
        assert build("OTHH", facts=facts, as_at=AS_AT).values() == ()

    def test_a_prefix_match_is_not_a_path_match(self, ref):
        facts = FactStore()
        facts.add(Fact(entity="OTHHX", attribute="lda_m", value=1,
                       valid_from=date(2025, 1, 1), source=ref, precedence=Precedence.AIP))
        assert build("OTHH", facts=facts, as_at=AS_AT).values() == ()


class TestNotamOverlay:
    def test_notam_are_resolved_to_the_minute_of_the_dossier(self, dossier):
        assert [n.identifier for n, _ in dossier.notams] == ["STL 08/430"]
        assert dossier.notams[0][1] is ForceState.IN_FORCE

    def test_an_unread_schedule_reaches_the_dossier_as_unresolved(self, register, entry):
        # The Boston notice is dormant at 06:00 by its schedule, but the
        # schedule has not been parsed. It appears, marked, rather than being
        # claimed in force or dropped.
        dossier = build("KZBW", register=register, as_at=AS_AT)
        assert [state for _, state in dossier.notams] == [ForceState.SCHEDULE_UNKNOWN]
        assert dossier.summary()["notams_unresolved"] == 1

    def test_a_runway_notam_surfaces_on_the_aerodrome(self, dossier):
        # Roll-up: the NOTAM names 8WC/RWY20, and an aerodrome dossier must
        # show it.
        subjects = {s.entity for n, _ in dossier.notams for s in n.subjects}
        assert "8WC/RWY20" in subjects

    def test_no_register_says_coverage_gap_rather_than_nothing_to_report(self):
        rendered = build("8WC", as_at=AS_AT).render()
        assert "none indexed for this aerodrome" in rendered
        assert "not a quiet aerodrome" in rendered


class TestReport:
    def test_the_report_names_the_moment_and_the_cycle(self, dossier):
        rendered = dossier.render()
        assert "8WC" in rendered
        assert "2025-09-01 06:00Z" in rendered
        assert "AIRAC 2509" in rendered

    def test_gaps_are_restated_at_the_end_with_a_warning(self, dossier):
        rendered = dossier.render()
        assert "COVERAGE GAPS" in rendered
        assert "nothing below them should be assumed" in rendered
        assert "AD 2.13   Declared distances" in rendered

    def test_a_failed_section_reads_differently_from_an_absent_one(self, dossier):
        rendered = dossier.render()
        lines = rendered.splitlines()
        assert any("!!" in ln and "AD 2.10" in ln for ln in lines)
        assert any("--" in ln and "AD 2.16" in ln for ln in lines)
        assert "obstacle table would not parse" in rendered
        assert "no helicopter landing area listed in AD 0.4" in rendered

    def test_every_printed_value_is_printed_with_its_source(self, dossier):
        rendered = dossier.render()
        assert "WASHINGTON COUNTY" in rendered
        assert rendered.count("faa-nms-aixm") >= len(dossier.values())

    def test_an_unplaced_value_is_labelled_as_such(self, dossier):
        assert "Held, but not attributed to a section" in dossier.render()


class TestArguments:
    def test_a_naive_moment_is_refused(self):
        with pytest.raises(ValueError, match="timezone-aware"):
            build("8WC", as_at=datetime(2025, 9, 1, 6, 0))

    def test_an_empty_aerodrome_is_refused(self):
        with pytest.raises(ValueError, match="non-empty"):
            build("   ", as_at=AS_AT)

    def test_the_aerodrome_key_is_normalised(self, store, coverage, register):
        dossier = build(" 8wc ", facts=store, coverage=coverage, register=register,
                        as_at=AS_AT)
        assert dossier.aerodrome == "8WC"
        assert len(dossier.held) == 2

    def test_an_unknown_section_lookup_says_so(self, dossier):
        with pytest.raises(KeyError, match="AD 2.99"):
            dossier.section("AD 2.99")

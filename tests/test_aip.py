"""The AIP's own structure, and what we hold of it.

Like the NOTAM format, the AIP's index comes from the specification — ICAO
Annex 15 and PANS-AIM — rather than from a captured document, so these tests
assert against the standard's numbering and titles. Where a section is *not* in
Annex 15, that is asserted too: claiming ICAO mandates something it does not
would make a State's omission look like a deficiency.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from aeropub.aip import (
    DOMAINS,
    SECTIONS,
    AipCoverage,
    HoldingState,
    Part,
    Repeat,
    Section,
    SectionHolding,
    aerodrome_sections,
    currency_sections,
    heliport_sections,
    section,
    sections_for,
)
from aeropub.airac import AiracCycle
from aeropub.archive import Archive

FIXTURE = Path(__file__).parent / "fixtures" / "faa" / "nms-initial-load-sample.raw"


@pytest.fixture
def ref(tmp_path):
    """A real citation over real archived bytes."""
    archive = Archive(tmp_path / "raw")
    entry = archive.put(
        FIXTURE.read_bytes(),
        source_id="OT-EAIP",
        url="https://www.aim.gov.qa/eaip/2026-01-08-AIRAC/html/eAIP/GEN-0.1-en-GB.html",
        retrieved_at=datetime(2026, 1, 8, tzinfo=timezone.utc),
    )
    return entry.to_source_ref(
        document="eAIP section", locator="OTHH",
        parser_id="eaip-eurocontrol", parser_version="0.1.0",
    )


def _codes(sections) -> list[str]:
    return [s.code for s in sections]


class TestIndexIntegrity:
    def test_every_code_is_unique(self):
        assert len({s.code for s in SECTIONS}) == len(SECTIONS)

    def test_every_section_declares_only_known_domains(self):
        for candidate in SECTIONS:
            assert set(candidate.domains) <= DOMAINS, candidate.code

    def test_a_typo_in_a_domain_is_refused_at_construction(self):
        # A section routed to a domain nobody subscribes to is a change that
        # silently reaches no one.
        with pytest.raises(ValueError, match="unknown domains"):
            Section(code="XX 1.1", part=Part.GEN, chapter=1, ordinal=1,
                    title="Test", domains=("performence",))

    def test_the_domain_vocabulary_agrees_with_the_impact_layer(self):
        # Two modules, one vocabulary. If impact.py routes a runway change to
        # a domain the AIP index has never heard of, a consumer subscribed by
        # section would miss it.
        from aeropub.impact import RULES

        for rule in RULES.values():
            assert set(rule.domains) <= DOMAINS, rule.attribute


class TestIcaoStructure:
    @pytest.mark.parametrize(
        "part,chapter,count",
        [
            (Part.GEN, 0, 6), (Part.GEN, 1, 7), (Part.GEN, 2, 7),
            (Part.GEN, 3, 6), (Part.GEN, 4, 2),
            (Part.ENR, 0, 6), (Part.ENR, 1, 14), (Part.ENR, 2, 2),
            (Part.ENR, 3, 6), (Part.ENR, 4, 5), (Part.ENR, 5, 6), (Part.ENR, 6, 1),
            (Part.AD, 0, 6), (Part.AD, 1, 5), (Part.AD, 2, 25), (Part.AD, 3, 23),
        ],
    )
    def test_chapter_lengths_match_annex_15(self, part, chapter, count):
        assert len(sections_for(part, chapter=chapter)) == count

    def test_ad_2_is_contiguous_from_1_to_25(self):
        # The plan settled complete AD 2.1–2.25 coverage. A hole in the index
        # is a section no parser would ever be asked for.
        assert [s.ordinal for s in aerodrome_sections()] == list(range(1, 26))

    def test_ad_3_is_contiguous_from_1_to_23(self):
        assert [s.ordinal for s in heliport_sections()] == list(range(1, 24))

    @pytest.mark.parametrize(
        "code,title",
        [
            ("GEN 1.7", "Differences from ICAO Standards, Recommended Practices and Procedures"),
            ("ENR 1.7", "Altimeter setting procedures"),
            ("ENR 5.4", "Air navigation obstacles — en-route"),
            ("AD 1.2", "Rescue and fire fighting services and snow plan"),
            ("AD 2.13", "Declared distances"),
            ("AD 2.12", "Runway physical characteristics"),
            ("AD 2.24", "Charts related to an aerodrome"),
        ],
    )
    def test_titles_are_icaos_own_not_a_paraphrase(self, code, title):
        assert section(code).title == title

    def test_ad_2_25_is_marked_as_not_annex_15(self):
        # The eAIP specification and individual States use it; Annex 15's own
        # list stops at 2.24. Recorded, so a State that omits 2.25 is not
        # reported as incomplete.
        assert section("AD 2.25").icao_defined is False
        assert "not defined by Annex 15" in section("AD 2.25").describe()
        assert all(s.icao_defined for s in aerodrome_sections() if s.ordinal < 25)


class TestCurrencySpine:
    def test_chapter_zero_of_every_part_is_the_currency_spine(self):
        assert {s.part for s in currency_sections()} == set(Part)
        assert all(s.chapter == 0 for s in currency_sections())

    def test_the_page_checklists_are_the_reconciliation_source(self):
        # Not boilerplate: these say what the State believes it published, and
        # therefore what we can be missing.
        assert _codes(s for s in SECTIONS if s.is_checklist) == [
            "GEN 0.4", "ENR 0.4", "AD 0.4",
        ]

    def test_every_currency_section_routes_to_the_currency_domain(self):
        assert all(s.applies_to("currency") for s in currency_sections())


class TestRepetition:
    def test_ad_2_repeats_per_aerodrome_and_ad_3_per_heliport(self):
        assert all(s.repeats is Repeat.PER_AERODROME for s in aerodrome_sections())
        assert all(s.repeats is Repeat.PER_HELIPORT for s in heliport_sections())

    def test_everything_else_appears_once(self):
        once = sections_for(repeats=Repeat.ONCE)
        assert not any(s.chapter in (2, 3) and s.part is Part.AD for s in once)
        assert len(once) == len(SECTIONS) - 25 - 23


class TestLookup:
    def test_a_code_is_normalised_before_lookup(self):
        assert section("ad 2.13") is section("AD 2.13")
        assert section("  AD   2.13 ") is section("AD 2.13")

    def test_an_unknown_code_says_what_to_do_instead(self):
        with pytest.raises(KeyError, match="State extension"):
            section("AD 2.99")

    def test_filtering_by_domain(self):
        assert "AD 2.13" in _codes(sections_for(domain="performance"))
        assert "AD 2.7" in _codes(sections_for(domain="winter"))
        assert "AD 1.2" in _codes(sections_for(domain="winter"))

    def test_an_unknown_domain_is_refused_rather_than_returning_nothing(self):
        # Silently returning no sections would read as "nothing is affected".
        with pytest.raises(ValueError, match="unknown domain"):
            sections_for(domain="perfromance")

    def test_filters_combine(self):
        assert _codes(sections_for(Part.AD, chapter=2, domain="obstacles")) == ["AD 2.10"]


class TestHolding:
    def test_held_requires_a_citation(self, ref):
        # A section we cannot cite is not one we hold. Same invariant as Fact.
        with pytest.raises(ValueError, match="cannot cite"):
            SectionHolding(section=section("AD 2.13"), entity="OTHH",
                           state=HoldingState.HELD)

    def test_absent_requires_its_basis(self):
        # Absence is a claim about the State, not about our search. It needs
        # the checklist or contents page that supports it.
        with pytest.raises(ValueError, match="needs its basis"):
            SectionHolding(section=section("AD 2.16"), entity="OTHH",
                           state=HoldingState.ABSENT)

    def test_absent_with_a_basis_is_accepted(self):
        holding = SectionHolding(
            section=section("AD 2.16"), entity="OTHH", state=HoldingState.ABSENT,
            detail="AD 0.4 checklist lists no such page",
        )
        assert not holding.state.is_gap

    def test_failed_and_unchecked_are_our_gaps_not_the_states(self):
        assert HoldingState.FAILED.is_gap
        assert HoldingState.NOT_CHECKED.is_gap
        assert not HoldingState.HELD.is_gap
        assert not HoldingState.ABSENT.is_gap

    def test_an_entity_is_required(self, ref):
        with pytest.raises(ValueError, match="entity"):
            SectionHolding(section=section("AD 2.13"), entity=" ",
                           state=HoldingState.HELD, source=ref)


class TestCoverage:
    def test_nothing_recorded_reads_as_never_checked_not_as_missing(self):
        # The distinction the whole module exists for. An aerodrome we never
        # looked at must not report the same as one with nothing to report.
        coverage = AipCoverage()
        holding = coverage.holding("OTHH", "AD 2.13")
        assert holding.state is HoldingState.NOT_CHECKED
        assert holding.section.code == "AD 2.13"

    def test_the_expected_set_for_an_aerodrome_is_ad_2(self):
        coverage = AipCoverage()
        assert _codes(coverage.expected("OTHH", per_aerodrome=True)) == _codes(
            aerodrome_sections()
        )

    def test_a_later_record_replaces_an_earlier_one(self, ref):
        coverage = AipCoverage([
            SectionHolding(section=section("AD 2.13"), entity="OTHH",
                           state=HoldingState.FAILED, detail="timed out"),
        ])
        coverage.record(SectionHolding(section=section("AD 2.13"), entity="OTHH",
                                       state=HoldingState.HELD, source=ref))
        assert coverage.holding("OTHH", "AD 2.13").state is HoldingState.HELD
        assert len(coverage) == 1

    def test_gaps_exclude_what_the_state_genuinely_does_not_publish(self, ref):
        coverage = AipCoverage([
            SectionHolding(section=section("AD 2.13"), entity="OTHH",
                           state=HoldingState.HELD, source=ref),
            SectionHolding(section=section("AD 2.16"), entity="OTHH",
                           state=HoldingState.ABSENT, detail="no helicopter area"),
        ])
        gaps = [h.section.code for h in coverage.gaps("OTHH", per_aerodrome=True)]
        assert "AD 2.13" not in gaps
        assert "AD 2.16" not in gaps
        assert "AD 2.12" in gaps
        assert len(gaps) == 23

    def test_the_summary_accounts_for_every_expected_section(self, ref):
        coverage = AipCoverage([
            SectionHolding(section=section("AD 2.13"), entity="OTHH",
                           state=HoldingState.HELD, source=ref,
                           cycle=AiracCycle.from_identifier("2601")),
            SectionHolding(section=section("AD 2.10"), entity="OTHH",
                           state=HoldingState.FAILED, detail="table would not parse"),
            SectionHolding(section=section("AD 2.16"), entity="OTHH",
                           state=HoldingState.ABSENT, detail="no helicopter area"),
        ])
        counts = coverage.summary("OTHH", per_aerodrome=True)
        assert counts["expected"] == 25
        assert counts["held"] + counts["absent"] + counts["failed"] + counts["not_checked"] == 25
        assert (counts["held"], counts["absent"], counts["failed"]) == (1, 1, 1)

    def test_coverage_is_kept_per_entity(self, ref):
        coverage = AipCoverage([
            SectionHolding(section=section("AD 2.13"), entity="OTHH",
                           state=HoldingState.HELD, source=ref),
        ])
        assert coverage.holding("OTBD", "AD 2.13").state is HoldingState.NOT_CHECKED


class TestReport:
    def test_every_expected_section_appears_whether_held_or_not(self, ref):
        coverage = AipCoverage([
            SectionHolding(section=section("AD 2.13"), entity="OTHH",
                           state=HoldingState.HELD, source=ref),
        ])
        rendered = coverage.render("OTHH", per_aerodrome=True)
        for candidate in aerodrome_sections():
            assert candidate.code in rendered

    def test_the_three_states_are_visually_distinct(self, ref):
        coverage = AipCoverage([
            SectionHolding(section=section("AD 2.13"), entity="OTHH",
                           state=HoldingState.HELD, source=ref),
            SectionHolding(section=section("AD 2.10"), entity="OTHH",
                           state=HoldingState.FAILED, detail="would not parse"),
            SectionHolding(section=section("AD 2.16"), entity="OTHH",
                           state=HoldingState.ABSENT, detail="no helicopter area"),
        ])
        lines = {
            line.split()[-3] if False else line
            for line in coverage.render("OTHH", per_aerodrome=True).splitlines()
        }
        assert any("!!" in line and "AD 2.10" in line for line in lines)
        assert any("--" in line and "AD 2.16" in line for line in lines)
        assert any(line.strip().startswith("AD 2.13") for line in lines)

    def test_an_untouched_aerodrome_reports_everything_unaccounted_for(self):
        rendered = AipCoverage().render("OTBD", per_aerodrome=True)
        assert "0 of 25 sections held" in rendered
        assert "25 unaccounted for" in rendered
        assert rendered.count("??") == 25

    def test_the_state_level_report_covers_gen_enr_and_ad(self):
        rendered = AipCoverage().render("OT", per_aerodrome=False)
        for code in ("GEN 1.7", "ENR 1.7", "AD 1.2"):
            assert code in rendered
        assert "AD 2.13" not in rendered

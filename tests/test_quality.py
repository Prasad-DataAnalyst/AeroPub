"""Findings about how a State publishes.

The threshold is ICAO's: PANS-AIM puts a three-month limit on information
carried by NOTAM, and anything expected to persist beyond that belongs in a
Supplement or an Amendment. These tests assert measurement against that
standard, never a compliance verdict — a quality harness that cries wolf gets
switched off, and the real findings go with it.

Objects are constructed to test the detection logic. The no-mock-data rule
governs source data entering the product.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from aeropub.airac import AiracCycle
from aeropub.notam_register import NotamRegister, RegisteredNotam, Subject, SubjectKind
from aeropub.provenance import SourceRef
from aeropub.quality import (
    MAX_NOTAM_DAYS,
    VOLATILITY_THRESHOLD,
    FindingKind,
    assess_quality,
    lapsed_estimates,
    permanent_by_notam,
    serial_reissues,
    volatility,
)

NOW = datetime(2026, 9, 3, tzinfo=timezone.utc)


def utc(year, month, day) -> datetime:
    return datetime(year, month, day, tzinfo=timezone.utc)


def ref(document: str) -> SourceRef:
    return SourceRef(
        source_id="QA-CAA", document=document, locator="NOTAM",
        retrieved_at=NOW, content_hash="b" * 64,
        parser_id="notam", parser_version="0.1.0",
    )


def notam(identifier, text, start, end, *, estimated=False,
          entity="OTHH/RWY34L", kind=SubjectKind.RUNWAY) -> RegisteredNotam:
    return RegisteredNotam(
        identifier=identifier,
        subjects=(Subject(entity=entity, kind=kind),),
        source=ref(f"NOTAM {identifier}"),
        text=text,
        effective_start=start,
        effective_end=end,
        estimated=estimated,
    )


class TestPermanentByNotam:
    def test_a_message_past_the_limit_is_reported_with_its_duration(self):
        register = NotamRegister([
            notam("A1201/26", "RWY 34L PAPI U/S", utc(2026, 1, 5), utc(2026, 12, 31)),
        ])
        findings = permanent_by_notam(register, NOW)
        assert [f.kind for f in findings] == [FindingKind.PERMANENT_BY_NOTAM]
        assert findings[0].days == (NOW - utc(2026, 1, 5)).days == 241
        assert findings[0].messages == ("A1201/26",)

    def test_a_message_inside_the_limit_is_not_a_finding(self):
        register = NotamRegister([
            notam("A4001/26", "RWY 16R WIP", NOW - timedelta(days=30), NOW + timedelta(days=5)),
        ])
        assert permanent_by_notam(register, NOW) == ()

    def test_the_boundary_is_the_standards_and_is_exclusive(self):
        exactly = notam("A1/26", "X", NOW - timedelta(days=MAX_NOTAM_DAYS),
                        NOW + timedelta(days=1))
        over = notam("A2/26", "Y", NOW - timedelta(days=MAX_NOTAM_DAYS + 1),
                     NOW + timedelta(days=1))
        assert permanent_by_notam(NotamRegister([exactly]), NOW) == ()
        assert len(permanent_by_notam(NotamRegister([over]), NOW)) == 1

    def test_a_message_that_has_already_ended_is_not_still_carrying_anything(self):
        register = NotamRegister([
            notam("A9/26", "OLD", utc(2025, 1, 1), utc(2025, 12, 1)),
        ])
        assert permanent_by_notam(register, NOW) == ()

    def test_a_message_not_yet_in_force_is_not_counted(self):
        register = NotamRegister([
            notam("A8/26", "FUTURE", NOW + timedelta(days=10), NOW + timedelta(days=400)),
        ])
        assert permanent_by_notam(register, NOW) == ()

    def test_the_consequence_names_where_it_should_have_been_published(self):
        register = NotamRegister([
            notam("A1201/26", "RWY 34L PAPI U/S", utc(2026, 1, 5), utc(2026, 12, 31)),
        ])
        consequence = permanent_by_notam(register, NOW)[0].consequence
        assert "Supplement or Amendment" in consequence
        assert "aerodrome study" in consequence


class TestSerialReissue:
    """The one that reading messages as they arrive cannot find."""

    @pytest.fixture
    def carried(self):
        text = "TWY B CLSD BTN TWY A AND TWY C"
        return NotamRegister([
            notam("A2001/26", text, utc(2025, 11, 1), utc(2026, 1, 20),
                  entity="OTHH/TWYB", kind=SubjectKind.TAXIWAY),
            notam("A2044/26", text, utc(2026, 1, 21), utc(2026, 4, 10),
                  entity="OTHH/TWYB", kind=SubjectKind.TAXIWAY),
            notam("A2090/26", text, utc(2026, 4, 11), utc(2026, 6, 30),
                  entity="OTHH/TWYB", kind=SubjectKind.TAXIWAY),
            notam("A2140/26", text, utc(2026, 7, 1), utc(2026, 9, 20),
                  entity="OTHH/TWYB", kind=SubjectKind.TAXIWAY),
        ])

    def test_each_message_is_within_the_limit_and_the_condition_is_not(self, carried):
        # Every individual message would pass permanent_by_notam.
        assert permanent_by_notam(carried, NOW) == ()

        findings = serial_reissues(carried, NOW)
        assert [f.kind for f in findings] == [FindingKind.SERIAL_REISSUE]
        assert findings[0].days == (NOW - utc(2025, 11, 1)).days == 306

    def test_every_message_in_the_chain_is_cited(self, carried):
        finding = serial_reissues(carried, NOW)[0]
        assert finding.messages == ("A2001/26", "A2044/26", "A2090/26", "A2140/26")
        assert len(finding.sources) == 4

    def test_the_chain_spans_messages_that_have_already_expired(self, carried):
        # The opposite of permanent_by_notam on purpose: here the condition is
        # what is measured, and the earlier messages are the evidence.
        assert serial_reissues(carried, NOW)[0].days > MAX_NOTAM_DAYS

    def test_different_conditions_at_one_aerodrome_are_not_grouped(self):
        register = NotamRegister([
            notam("A1/26", "TWY B CLSD", utc(2025, 11, 1), utc(2026, 3, 1),
                  entity="OTHH/TWYB"),
            notam("A2/26", "TWY C CLSD", utc(2026, 3, 2), utc(2026, 9, 1),
                  entity="OTHH/TWYB"),
        ])
        assert serial_reissues(register, NOW) == ()

    def test_the_same_words_at_different_objects_are_not_grouped(self):
        register = NotamRegister([
            notam("A1/26", "WIP", utc(2025, 11, 1), utc(2026, 3, 1), entity="OTHH/TWYB"),
            notam("A2/26", "WIP", utc(2026, 3, 2), utc(2026, 9, 1), entity="OTHH/TWYC"),
        ])
        assert serial_reissues(register, NOW) == ()

    def test_whitespace_and_case_do_not_break_the_chain(self):
        register = NotamRegister([
            notam("A1/26", "twy b  clsd", utc(2025, 11, 1), utc(2026, 3, 1),
                  entity="OTHH/TWYB"),
            notam("A2/26", "TWY B CLSD", utc(2026, 3, 2), utc(2026, 9, 1),
                  entity="OTHH/TWYB"),
        ])
        assert len(serial_reissues(register, NOW)) == 1

    def test_one_message_is_never_a_chain(self):
        register = NotamRegister([
            notam("A1/26", "TWY B CLSD", utc(2025, 11, 1), utc(2026, 12, 1),
                  entity="OTHH/TWYB"),
        ])
        assert serial_reissues(register, NOW) == ()

    def test_a_short_chain_inside_the_limit_is_not_a_finding(self):
        register = NotamRegister([
            notam("A1/26", "WIP", NOW - timedelta(days=40), NOW - timedelta(days=20)),
            notam("A2/26", "WIP", NOW - timedelta(days=19), NOW + timedelta(days=5)),
        ])
        assert serial_reissues(register, NOW) == ()


class TestLapsedEstimate:
    def test_an_estimate_in_the_past_is_reported(self):
        register = NotamRegister([
            notam("A3010/26", "CRANE ERECTED 410FT AMSL", utc(2026, 3, 1),
                  utc(2026, 6, 30), estimated=True, entity="OTHH",
                  kind=SubjectKind.AERODROME),
        ])
        findings = lapsed_estimates(register, NOW)
        assert [f.kind for f in findings] == [FindingKind.LAPSED_ESTIMATE]
        assert findings[0].days == (NOW - utc(2026, 6, 30)).days

    def test_a_firm_end_date_in_the_past_is_not_a_lapsed_estimate(self):
        # It ended. That is a message doing exactly what it said.
        register = NotamRegister([
            notam("A1/26", "WIP", utc(2026, 3, 1), utc(2026, 6, 30), estimated=False),
        ])
        assert lapsed_estimates(register, NOW) == ()

    def test_an_estimate_still_ahead_is_not_a_finding(self):
        register = NotamRegister([
            notam("A1/26", "WIP", utc(2026, 3, 1), NOW + timedelta(days=5),
                  estimated=True),
        ])
        assert lapsed_estimates(register, NOW) == ()

    def test_the_consequence_warns_against_planning_a_return(self):
        register = NotamRegister([
            notam("A1/26", "WIP", utc(2026, 3, 1), utc(2026, 6, 30), estimated=True),
        ])
        assert "unknown duration" in lapsed_estimates(register, NOW)[0].consequence


class TestVolatility:
    def _store_with(self, changes_per_cycle: int, through: AiracCycle):
        from aeropub.facts import Fact, FactStore, Precedence

        facts = FactStore()
        window = [through.shifted_by(-n) for n in range(6, -1, -1)]
        for index, cycle in enumerate(window):
            for slot in range(changes_per_cycle):
                facts.add(Fact(
                    entity=f"OTHH/RWY34L", attribute=f"lda_m_{slot}",
                    value=3900 + index, valid_from=cycle.effective_date,
                    valid_to=cycle.next.effective_date - timedelta(days=1),
                    source=ref("AIP"), precedence=Precedence.AIP,
                ))
        return facts

    def test_it_counts_changes_cycle_over_cycle(self):
        through = AiracCycle.from_identifier("2610")
        store = self._store_with(2, through)
        reading = volatility(store, "OTHH", through=through, cycles=6)
        assert len(reading.per_cycle) == 6
        assert reading.total > 0
        assert reading.cycles[-1] == through

    def test_a_steady_aerodrome_is_not_flagged(self):
        from aeropub.facts import Fact, FactStore, Precedence

        through = AiracCycle.from_identifier("2610")
        facts = FactStore()
        facts.add(Fact(entity="OTHH/RWY34L", attribute="lda_m", value=3900,
                       valid_from=utc(2020, 1, 1).date(), source=ref("AIP"),
                       precedence=Precedence.AIP))
        reading = volatility(facts, "OTHH", through=through)
        assert reading.total == 0
        assert not reading.is_unstable

    def test_the_threshold_is_a_reading_aid_not_a_standard(self):
        # Named and documented as such, so nothing downstream treats it as one.
        assert VOLATILITY_THRESHOLD == 3.0

    def test_another_aerodrome_is_not_counted(self):
        from aeropub.facts import Fact, FactStore, Precedence

        through = AiracCycle.from_identifier("2610")
        facts = FactStore()
        facts.add(Fact(entity="OTBD/RWY15", attribute="lda_m", value=4570,
                       valid_from=through.effective_date, source=ref("AIP"),
                       precedence=Precedence.AIP))
        assert volatility(facts, "OTHH", through=through).total == 0


class TestReport:
    @pytest.fixture
    def register(self):
        text = "TWY B CLSD BTN TWY A AND TWY C"
        return NotamRegister([
            notam("A1201/26", "RWY 34L PAPI U/S", utc(2026, 1, 5), utc(2026, 12, 31)),
            notam("A2001/26", text, utc(2025, 11, 1), utc(2026, 1, 20),
                  entity="OTHH/TWYB"),
            notam("A2044/26", text, utc(2026, 1, 21), utc(2026, 9, 20),
                  entity="OTHH/TWYB"),
            notam("A3010/26", "CRANE 410FT AMSL", utc(2026, 3, 1), utc(2026, 6, 30),
                  estimated=True, entity="OTHH", kind=SubjectKind.AERODROME),
            notam("A4001/26", "RWY 16R WIP", NOW - timedelta(days=5),
                  NOW + timedelta(days=5)),
        ])

    def test_all_three_notam_findings_are_composed(self, register):
        report = assess_quality(register=register, as_at=NOW)
        assert {f.kind for f in report.findings} == {
            FindingKind.PERMANENT_BY_NOTAM,
            FindingKind.SERIAL_REISSUE,
            FindingKind.LAPSED_ESTIMATE,
        }
        assert report.notams_examined == 5

    def test_the_report_says_what_it_examined(self, register):
        rendered = assess_quality(register=register, as_at=NOW).render()
        assert "5 NOTAM examined" in rendered
        assert "PANS-AIM" in rendered

    def test_it_measures_rather_than_judging_compliance(self, register):
        rendered = assess_quality(register=register, as_at=NOW).render()
        assert "not a compliance judgement" in rendered
        for verdict in ("non-compliant", "violation", "breach", "illegal"):
            assert verdict not in rendered.lower()

    def test_an_empty_report_does_not_claim_a_clean_bill_of_health(self):
        rendered = assess_quality(register=NotamRegister(), as_at=NOW).render()
        assert "not a clean bill of health" in rendered

    def test_findings_can_be_narrowed_to_one_entity(self, register):
        report = assess_quality(register=register, entity="OTHH/TWYB", as_at=NOW)
        assert {f.entity for f in report.findings} == {"OTHH/TWYB"}
        assert report.scope == "OTHH/TWYB"

    def test_a_naive_moment_is_refused(self, register):
        with pytest.raises(ValueError, match="timezone-aware"):
            assess_quality(register=register, as_at=datetime(2026, 9, 3))

    def test_volatility_joins_the_report_when_a_store_is_supplied(self):
        from aeropub.facts import Fact, FactStore, Precedence

        through = AiracCycle.from_identifier("2610")
        facts = FactStore()
        for index in range(20):
            cycle = through.shifted_by(-(index % 6))
            facts.add(Fact(
                entity="OTHH/RWY34L", attribute=f"a{index}", value=index,
                valid_from=cycle.effective_date,
                valid_to=cycle.next.effective_date - timedelta(days=1),
                source=ref("AIP"), precedence=Precedence.AIP,
            ))
        report = assess_quality(store=facts, entity="OTHH", through=through, as_at=NOW)
        assert report.cycles_examined == 6

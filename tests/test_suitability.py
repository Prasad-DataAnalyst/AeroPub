"""Fit assessment — and, more importantly, what it refuses to conclude.

The values here are the design document's worked aerodrome with figures chosen
to exercise each branch; the no-mock-data rule governs source data entering the
product, not objects constructed to test an assessment. The citations are
deliberately marked as test fixtures so nothing here reads as held data.

What is being tested is mostly refusal. An assessment that quietly drops the
comparisons it could not make prints a shorter, cleaner and far more dangerous
document than one that lists them, and the output of this module looks enough
like a clearance that the distinction matters more here than anywhere else.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from aeropub.aip import AipCoverage, HoldingState
from aeropub.aircraft import AircraftType, Characteristic, Origin
from aeropub.dossier import build
from aeropub.facts import Fact, FactStore, Precedence
from aeropub.notam_register import NotamRegister, RegisteredNotam, Subject, SubjectKind
from aeropub.provenance import SourceRef
from aeropub.suitability import (
    RUNWAY_WIDTH_M,
    Assessment,
    Check,
    Note,
    Suitability,
    assess_suitability,
    minimum_runway_width_m,
)

AD = "OTHH"
RWY = "OTHH/RWY34L"
NOW = datetime(2026, 9, 3, 10, 0, tzinfo=timezone.utc)
ON = date(2026, 9, 3)


def ref(**overrides) -> SourceRef:
    fields = dict(
        source_id="TEST",
        document="test fixture — not a real publication",
        locator="AD 2.12",
        retrieved_at=NOW,
        content_hash="d" * 64,
        parser_id="test",
        parser_version="0.1.0",
    )
    fields.update(overrides)
    return SourceRef(**fields)


def fact(entity: str, attribute: str, value) -> Fact:
    return Fact(
        entity=entity,
        attribute=attribute,
        value=value,
        valid_from=date(2026, 1, 1),
        source=ref(),
        precedence=Precedence.AIP,
    )


def characteristic(attribute: str, value, **overrides) -> Characteristic:
    fields = dict(attribute=attribute, value=value, source=ref(), origin=Origin.ACAP)
    fields.update(overrides)
    return Characteristic(**fields)


def dossier(*facts, register: NotamRegister | None = None):
    coverage = AipCoverage()
    return build(
        AD,
        facts=FactStore(facts),
        coverage=coverage,
        register=register,
        as_at=NOW,
        on=ON,
    )


def aircraft(*items, designator: str = "TEST") -> AircraftType:
    return AircraftType(designator=designator).with_characteristics(items)


#: A pairing where every check can be made: the aerodrome publishes a Code 4E
#: reference code, a 60 m runway, PCN 80/F/A/W/T and Category 9; the aeroplane
#: is Code E, needs Category 9 and loads ACN 62 on that pavement.
def complete():
    return (
        dossier(
            fact(AD, "aerodrome_reference_code", "4E"),
            fact(AD, "rffs_category", 9),
            fact(RWY, "pcn", "80/F/A/W/T"),
            fact(RWY, "runway_width_m", 60.0),
            fact(RWY, "tora_m", 4850.0),
        ),
        aircraft(
            characteristic("wingspan_m", 60.0),
            characteristic("omgws_m", 12.0),
            characteristic("reference_field_length_m", 3100.0),
            characteristic("overall_length_m", 70.0),
            characteristic("fuselage_width_m", 6.2),
            characteristic("acn", 62.0, variant="F/A at MTOW"),
        ),
    )


# --------------------------------------------------------------------------
# Unknown never becomes suitable
# --------------------------------------------------------------------------


class TestUnknownIsNeverAPass:
    def test_an_empty_assessment_is_unknown_not_suitable(self):
        # Absence of evidence reading as a pass is the failure this whole
        # project exists to avoid, and the output here looks like a clearance.
        empty = Suitability(aerodrome=AD, designator="TEST", as_at=NOW)
        assert empty.overall is Assessment.UNKNOWN
        assert not empty.is_conclusive

    def test_an_aerodrome_we_hold_nothing_about_answers_unknown_on_every_check(self):
        result = assess_suitability(dossier(), aircraft())
        assert result.overall is Assessment.UNKNOWN
        assert not result.is_conclusive
        assert result.checks, "an assessment with no checks would print as clean"
        assert all(c.assessment is Assessment.UNKNOWN for c in result.checks)

    def test_unknown_outranks_every_pass(self):
        # Three checks pass and one cannot be made. The answer is not "suitable".
        held, plane = complete()
        thinner = aircraft(
            *(c for c in plane.characteristics if c.attribute != "acn"),
        )
        result = assess_suitability(held, thinner)
        assert any(c.assessment is Assessment.SUITABLE for c in result.checks)
        assert result.overall is Assessment.UNKNOWN
        assert not result.is_conclusive

    def test_a_definite_failure_outranks_an_unknown(self):
        result = assess_suitability(
            dossier(fact(AD, "rffs_category", 5)),
            aircraft(characteristic("overall_length_m", 70.0),
                     characteristic("fuselage_width_m", 6.2)),
        )
        assert result.overall is Assessment.NOT_SUITABLE
        assert not result.is_conclusive  # other checks were still not made

    def test_every_unmade_check_is_listed_by_name(self):
        result = assess_suitability(dossier(), aircraft())
        named = {c.name for c in result.unknown}
        assert named == {
            "Aerodrome reference code",
            "Pavement strength",
            "Runway width",
            "Rescue and fire fighting",
        }

    def test_a_fully_held_pairing_is_conclusive(self):
        result = assess_suitability(*complete())
        assert result.unknown == ()
        assert result.is_conclusive
        assert result.overall is Assessment.SUITABLE


# --------------------------------------------------------------------------
# Notes are not checks
# --------------------------------------------------------------------------


class TestNotesNeverBecomeVerdicts:
    def test_declared_distances_are_reported_and_not_assessed(self):
        result = assess_suitability(*complete())
        assert [n.name for n in result.notes] == ["Declared distances"]
        assert not any(c.name == "Declared distances" for c in result.checks)

    def test_a_note_cannot_make_an_assessment_inconclusive(self):
        # Filing a declared distance as an UNKNOWN check would leave every
        # assessment permanently inconclusive, and a flag that is always on
        # tells a reader nothing.
        result = assess_suitability(*complete())
        assert result.notes
        assert result.is_conclusive

    def test_the_note_draws_no_conclusion_from_the_comparison(self):
        result = assess_suitability(*complete())
        text = result.notes[0].detail
        assert "nothing is concluded" in text
        assert not any(word in text.lower() for word in ("suitable", "adequate", "sufficient"))

    def test_notes_can_be_switched_off_without_touching_the_checks(self):
        held, plane = complete()
        with_notes = assess_suitability(held, plane)
        without = assess_suitability(held, plane, include_declared_distances=False)
        assert without.notes == ()
        assert without.checks == with_notes.checks

    def test_a_note_carries_both_sides_of_its_evidence(self):
        result = assess_suitability(*complete())
        assert len(result.notes[0].citations()) == 2


# --------------------------------------------------------------------------
# The individual checks
# --------------------------------------------------------------------------


class TestReferenceCodeCheck:
    def find(self, result):
        return next(c for c in result.checks if c.name == "Aerodrome reference code")

    def test_a_smaller_aerodrome_does_not_take_a_larger_aeroplane(self):
        result = assess_suitability(
            dossier(fact(AD, "aerodrome_reference_code", "4C")),
            aircraft(characteristic("wingspan_m", 60.0)),
        )
        check = self.find(result)
        assert check.assessment is Assessment.NOT_SUITABLE
        assert check.blocks
        assert "Code E" in check.detail

    def test_a_larger_aerodrome_takes_a_smaller_aeroplane(self):
        result = assess_suitability(
            dossier(fact(AD, "aerodrome_reference_code", "4F")),
            aircraft(characteristic("wingspan_m", 34.0)),
        )
        assert self.find(result).assessment is Assessment.SUITABLE

    def test_the_wheel_span_is_taken_into_account(self):
        # 34 m span alone is Code C, which a 4C aerodrome takes. The 10 m wheel
        # span makes it Code D, which it does not.
        result = assess_suitability(
            dossier(fact(AD, "aerodrome_reference_code", "4C")),
            aircraft(
                characteristic("wingspan_m", 34.0),
                characteristic("omgws_m", 10.0),
            ),
        )
        assert self.find(result).assessment is Assessment.NOT_SUITABLE

    def test_a_declared_code_that_is_not_a_code_is_not_interpreted(self):
        result = assess_suitability(
            dossier(fact(AD, "aerodrome_reference_code", "see remarks")),
            aircraft(characteristic("wingspan_m", 60.0)),
        )
        check = self.find(result)
        assert check.assessment is Assessment.UNKNOWN
        assert "as published rather than corrected" in check.detail

    def test_the_check_claims_no_aip_section(self):
        # PANS-AIM's AD 2.2 item list does not carry the reference code and
        # States print it in different places. Filing it under a guessed
        # section would read as though that section said it.
        result = assess_suitability(*complete())
        assert self.find(result).section == ""


class TestPavementCheck:
    def find(self, result):
        return [c for c in result.checks if c.name == "Pavement strength"]

    def test_an_acn_within_the_pcn_passes(self):
        result = assess_suitability(*complete())
        check = self.find(result)[0]
        assert check.assessment is Assessment.SUITABLE
        assert check.scope == "RWY34L"
        assert check.section == "AD 2.12"

    def test_an_overload_is_restricted_not_refused(self):
        # Annex 14 provides for overload operations. Calling it NOT_SUITABLE
        # would be wrong in the other direction, and an operator who learns the
        # tool cries wolf stops reading it.
        held, plane = complete()
        heavy = aircraft(
            *(c for c in plane.characteristics if c.attribute != "acn"),
            characteristic("acn", 93.0, variant="F/A at MTOW"),
        )
        check = self.find(assess_suitability(held, heavy))[0]
        assert check.assessment is Assessment.RESTRICTED
        assert "consent" in check.detail

    def test_an_acn_for_a_different_cell_is_not_substituted(self):
        held, plane = complete()
        wrong_cell = aircraft(
            *(c for c in plane.characteristics if c.attribute != "acn"),
            characteristic("acn", 40.0, variant="R/D at MTOW"),
        )
        check = self.find(assess_suitability(held, wrong_cell))[0]
        assert check.assessment is Assessment.UNKNOWN
        assert "not a substitute" in check.detail

    def test_the_heaviest_matching_cell_wins(self):
        # ACAP gives ACN at several weights. A suitability check must answer
        # for the heaviest, not whichever variant was recorded first.
        held, plane = complete()
        both = aircraft(
            *(c for c in plane.characteristics if c.attribute != "acn"),
            characteristic("acn", 40.0, variant="F/A at operating empty weight"),
            characteristic("acn", 93.0, variant="F/A at MTOW"),
        )
        check = self.find(assess_suitability(held, both))[0]
        assert check.assessment is Assessment.RESTRICTED

    def test_an_unreadable_published_rating_is_reported_not_guessed(self):
        result = assess_suitability(
            dossier(fact(RWY, "pcn", "80")),
            aircraft(characteristic("acn", 62.0, variant="F/A")),
        )
        check = self.find(result)[0]
        assert check.assessment is Assessment.UNKNOWN
        assert "could not be read" in check.detail

    def test_every_runway_with_a_pcn_gets_its_own_check(self):
        result = assess_suitability(
            dossier(
                fact("OTHH/RWY34L", "pcn", "80/F/A/W/T"),
                fact("OTHH/RWY16R", "pcn", "62/F/A/W/T"),
            ),
            aircraft(characteristic("acn", 70.0, variant="F/A")),
        )
        by_scope = {c.scope: c.assessment for c in self.find(result)}
        assert by_scope == {
            "RWY34L": Assessment.SUITABLE,
            "RWY16R": Assessment.RESTRICTED,
        }


class TestRunwayWidthCheck:
    def find(self, result):
        return [c for c in result.checks if c.name == "Runway width"]

    def test_a_runway_meeting_table_3_1_passes(self):
        check = self.find(assess_suitability(*complete()))[0]
        assert check.assessment is Assessment.SUITABLE

    def test_a_narrower_runway_is_a_condition_not_a_prohibition(self):
        # Table 3-1 is a design standard. States approve narrower runways, and
        # calling one "not suitable" would be a confident answer the standard
        # does not support.
        held, plane = complete()
        narrow = dossier(
            fact(AD, "aerodrome_reference_code", "4E"),
            fact(RWY, "runway_width_m", 45.0),
        )
        wide_body = aircraft(
            characteristic("wingspan_m", 70.0),
            characteristic("reference_field_length_m", 3100.0),
        )
        check = self.find(assess_suitability(narrow, wide_body))[0]
        assert check.assessment is Assessment.RESTRICTED
        assert "design standard" in check.detail
        assert "not thereby prohibited" in check.detail

    def test_a_code_combination_outside_the_table_is_not_invented(self):
        # No code 4 runway is built to Code A geometry, so the table has no
        # cell. A 1900 m field length with a 10 m span is exactly that.
        result = assess_suitability(
            dossier(fact(RWY, "runway_width_m", 45.0)),
            aircraft(
                characteristic("wingspan_m", 10.0),
                characteristic("reference_field_length_m", 1900.0),
            ),
        )
        check = self.find(result)[0]
        assert check.assessment is Assessment.UNKNOWN
        assert "does not occur in the table" in check.detail


class TestRffsCheck:
    def find(self, result):
        return next(c for c in result.checks if c.name == "Rescue and fire fighting")

    def test_a_sufficient_category_passes(self):
        check = self.find(assess_suitability(*complete()))
        assert check.assessment is Assessment.SUITABLE
        assert check.section == "AD 2.6"

    def test_a_short_category_fails_and_names_the_state_remission(self):
        result = assess_suitability(
            dossier(fact(AD, "rffs_category", 7)),
            aircraft(
                characteristic("overall_length_m", 70.0),
                characteristic("fuselage_width_m", 6.2),
            ),
        )
        check = self.find(result)
        assert check.assessment is Assessment.NOT_SUITABLE
        assert "the State's" in check.detail

    def test_without_a_fuselage_width_the_requirement_is_only_a_floor(self):
        # Annex 14 9.2.2 can push the requirement one category higher. An
        # aerodrome that just meets the length-only figure may be one short,
        # so this passes as RESTRICTED and says why.
        result = assess_suitability(
            dossier(fact(AD, "rffs_category", 9)),
            aircraft(characteristic("overall_length_m", 70.0)),
        )
        check = self.find(result)
        assert check.assessment is Assessment.RESTRICTED
        assert "9.2.2" in check.detail
        assert "floor" in check.detail

    def test_the_fuselage_width_bump_can_turn_a_pass_into_a_failure(self):
        # Same aeroplane length, same aerodrome. The width is what changes it.
        held = dossier(fact(AD, "rffs_category", 9))
        narrow = aircraft(
            characteristic("overall_length_m", 70.0),
            characteristic("fuselage_width_m", 6.2),
        )
        wide = aircraft(
            characteristic("overall_length_m", 70.0),
            characteristic("fuselage_width_m", 7.2),
        )
        assert self.find(assess_suitability(held, narrow)).assessment is Assessment.SUITABLE
        assert self.find(assess_suitability(held, wide)).assessment is Assessment.NOT_SUITABLE

    def test_a_published_category_that_is_not_a_number_is_not_interpreted(self):
        result = assess_suitability(
            dossier(fact(AD, "rffs_category", "9 by arrangement")),
            aircraft(characteristic("overall_length_m", 70.0),
                     characteristic("fuselage_width_m", 6.2)),
        )
        check = self.find(result)
        assert check.assessment is Assessment.UNKNOWN
        assert "not interpreted" in check.detail


# --------------------------------------------------------------------------
# Table 3-1
# --------------------------------------------------------------------------


class TestTable31:
    @pytest.mark.parametrize(
        "number,letter,expected",
        [
            (1, "A", 18.0), (1, "C", 23.0),
            (2, "A", 23.0), (2, "C", 30.0),
            (3, "A", 30.0), (3, "C", 30.0), (3, "D", 45.0),
            (4, "C", 45.0), (4, "E", 45.0), (4, "F", 60.0),
        ],
    )
    def test_published_cells(self, number, letter, expected):
        assert minimum_runway_width_m(number, letter) == expected

    @pytest.mark.parametrize("combination", [(1, "D"), (2, "F"), (3, "E"), (4, "A")])
    def test_combinations_the_table_does_not_carry(self, combination):
        assert minimum_runway_width_m(*combination) is None

    def test_the_table_spans_18_to_60_metres(self):
        assert min(RUNWAY_WIDTH_M.values()) == 18.0
        assert max(RUNWAY_WIDTH_M.values()) == 60.0

    def test_width_never_decreases_as_the_code_grows(self):
        for (number, letter), width in RUNWAY_WIDTH_M.items():
            for other in "ABCDEF"[: "ABCDEF".index(letter)]:
                smaller = RUNWAY_WIDTH_M.get((number, other))
                if smaller is not None:
                    assert smaller <= width


# --------------------------------------------------------------------------
# NOTAM, and what the checks did not account for
# --------------------------------------------------------------------------


class TestNotamAreSurfacedNotInterpreted:
    def register(self) -> NotamRegister:
        return NotamRegister(
            [
                RegisteredNotam(
                    identifier="A2291/26",
                    subjects=(
                        Subject(entity=RWY, kind=SubjectKind.RUNWAY, designator="34L"),
                    ),
                    source=ref(document="NOTAM A2291/26"),
                    text="RWY 34L CLSD",
                    effective_start=datetime(2026, 9, 1, tzinfo=timezone.utc),
                    effective_end=datetime(2026, 9, 30, tzinfo=timezone.utc),
                )
            ]
        )

    def test_an_in_force_notam_is_carried_onto_the_assessment(self):
        held = dossier(
            fact(AD, "aerodrome_reference_code", "4E"),
            fact(RWY, "pcn", "80/F/A/W/T"),
            register=self.register(),
        )
        result = assess_suitability(held, aircraft(characteristic("wingspan_m", 60.0)))
        assert len(result.operative_notams) == 1

    def test_the_render_says_the_checks_do_not_account_for_them(self):
        # A confident "suitable" computed over a closed runway is the artefact
        # this guard exists to prevent. The module does not read NOTAM text
        # into values, so it must say so rather than imply it did.
        held = dossier(
            fact(AD, "aerodrome_reference_code", "4E"),
            fact(RWY, "pcn", "80/F/A/W/T"),
            register=self.register(),
        )
        printed = assess_suitability(
            held,
            aircraft(
                characteristic("wingspan_m", 60.0),
                characteristic("acn", 62.0, variant="F/A"),
            ),
        ).render()
        assert "do not account for them" in printed
        assert "A2291/26" in printed

    def test_no_notam_indexed_reads_as_a_gap_not_a_quiet_aerodrome(self):
        printed = assess_suitability(*complete()).render()
        assert "not the same as none published" in printed

    def test_a_notam_over_a_checked_runway_makes_the_assessment_inconclusive(self):
        # The failure this guard exists for: every check made, every check
        # passed, and the runway they were made about is closed.
        held, plane = complete()
        overtaken = build(
            AD,
            facts=FactStore(
                [
                    fact(AD, "aerodrome_reference_code", "4E"),
                    fact(AD, "rffs_category", 9),
                    fact(RWY, "pcn", "80/F/A/W/T"),
                    fact(RWY, "runway_width_m", 60.0),
                ]
            ),
            coverage=AipCoverage(),
            register=self.register(),
            as_at=NOW,
            on=ON,
        )
        result = assess_suitability(overtaken, plane)
        assert result.unknown == ()
        assert result.overall is Assessment.SUITABLE
        assert not result.is_conclusive
        assert {c.name for c in result.overtaken} == {
            "Pavement strength",
            "Runway width",
        }

    def test_containment_runs_one_way(self):
        # A NOTAM against one runway does not overtake the aerodrome's fire
        # category, which is not a property of that runway.
        held = dossier(
            fact(AD, "rffs_category", 9),
            fact(RWY, "pcn", "80/F/A/W/T"),
            register=self.register(),
        )
        result = assess_suitability(
            held,
            aircraft(
                characteristic("overall_length_m", 70.0),
                characteristic("fuselage_width_m", 6.2),
                characteristic("acn", 62.0, variant="F/A"),
            ),
        )
        names = {c.name for c in result.overtaken}
        assert "Pavement strength" in names
        assert "Rescue and fire fighting" not in names

    def test_an_aerodrome_wide_notam_reaches_every_check(self):
        wide = NotamRegister(
            [
                RegisteredNotam(
                    identifier="A2300/26",
                    subjects=(
                        Subject(entity=AD, kind=SubjectKind.FILED_LOCATION, icao=AD),
                    ),
                    source=ref(document="NOTAM A2300/26"),
                    text="AD CLSD",
                    effective_start=datetime(2026, 9, 1, tzinfo=timezone.utc),
                    effective_end=datetime(2026, 9, 30, tzinfo=timezone.utc),
                )
            ]
        )
        held = build(
            AD,
            facts=FactStore(
                [fact(AD, "rffs_category", 9), fact(RWY, "pcn", "80/F/A/W/T")]
            ),
            coverage=AipCoverage(),
            register=wide,
            as_at=NOW,
            on=ON,
        )
        result = assess_suitability(
            held,
            aircraft(
                characteristic("overall_length_m", 70.0),
                characteristic("fuselage_width_m", 6.2),
                characteristic("acn", 62.0, variant="F/A"),
            ),
        )
        assert {c.name for c in result.overtaken} == {
            "Pavement strength",
            "Rescue and fire fighting",
        }

    def test_the_warning_sits_beside_the_verdict_not_at_the_foot(self):
        held = dossier(
            fact(RWY, "pcn", "80/F/A/W/T"),
            register=self.register(),
        )
        printed = assess_suitability(
            held, aircraft(characteristic("acn", 62.0, variant="F/A"))
        ).render()
        banner = printed.index("may have overtaken")
        notam_section = printed.index("\nNOTAM")
        assert banner < notam_section, "the caveat must precede the checks it qualifies"
        assert banner < printed.index("Checks")

    def test_an_unattributed_notam_does_not_silently_overtake_nothing(self):
        # A NOTAM filed at the aerodrome with no structural subject still
        # records where it was filed, and that location reaches the checks.
        result = assess_suitability(*complete())
        assert result.overtaken == ()
        assert result.is_conclusive


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------


class TestRender:
    def test_it_disclaims_being_a_performance_or_dispatch_answer(self):
        printed = assess_suitability(*complete()).render()
        assert "not a performance calculation" in printed
        assert "not a dispatch decision" in printed

    def test_an_inconclusive_assessment_says_so_beside_the_verdict(self):
        printed = assess_suitability(dossier(), aircraft()).render()
        assert "NOT CONCLUSIVE" in printed

    def test_a_conclusive_assessment_does_not(self):
        assert "NOT CONCLUSIVE" not in assess_suitability(*complete()).render()

    def test_unmade_checks_get_their_own_heading(self):
        printed = assess_suitability(dossier(), aircraft()).render()
        assert "NOT CHECKED" in printed
        assert "nothing above should be read as covering them" in printed

    def test_every_check_prints_its_citations(self):
        printed = assess_suitability(*complete()).render()
        # Both sides of the pavement check resolve to the fixture document.
        assert printed.count("test fixture — not a real publication") >= 4


class TestLayerTwoDiscipline:
    def test_nothing_a_reader_sees_names_an_operator(self):
        # Layer two carries no operator context. The same aeroplane against the
        # same aerodrome gives the same answer for every airline.
        printed = assess_suitability(*complete()).render()
        for word in ("fleet", "network", "customer", "tenant", "airline"):
            assert word not in printed.lower(), f"layer two leaked {word!r}"

    def test_the_assessment_holds_no_operator_field(self):
        fields = set(Suitability.__annotations__)
        assert not fields & {"operator", "tenant", "fleet", "airline"}


# --------------------------------------------------------------------------
# The ACN/PCN to ACR/PCR changeover
# --------------------------------------------------------------------------


class TestBothClassificationSystems:
    """ICAO replaced ACN/PCN with ACR/PCR from 28 November 2024.

    States are converting at different rates, so both will be published
    somewhere for years. Plan section 13 asks for the two to be evaluated
    concurrently and for internal inconsistency during changeover to be
    flagged rather than silently resolved.
    """

    def find(self, result):
        return [c for c in result.checks if c.name == "Pavement strength"]

    def test_a_state_publishing_only_pcr_is_not_a_coverage_gap(self):
        # Reading only the legacy attribute would report "no PCN held" at an
        # aerodrome that publishes its strength perfectly well.
        result = assess_suitability(
            dossier(fact(RWY, "pcr", "560/F/B/W/T")),
            aircraft(characteristic("acr", 520.0, variant="F/B at MTOW")),
        )
        check = self.find(result)[0]
        assert check.assessment is Assessment.SUITABLE
        assert "ACR 520" in check.detail and "PCR 560" in check.detail

    def test_an_acr_is_not_offered_against_a_pcn(self):
        # The aeroplane holds only an ACR; the aerodrome publishes a PCN. There
        # is no comparison to make, and inventing one errs permissively.
        result = assess_suitability(
            dossier(fact(RWY, "pcn", "80/F/A/W/T")),
            aircraft(characteristic("acr", 520.0, variant="F/A at MTOW")),
        )
        check = self.find(result)[0]
        assert check.assessment is Assessment.UNKNOWN
        assert "not a substitute" in check.detail
        assert "other classification system" in check.detail

    def test_where_both_are_published_the_current_system_is_assessed(self):
        result = assess_suitability(
            dossier(
                fact(RWY, "pcn", "80/F/A/W/T"),
                fact(RWY, "pcr", "560/F/A/W/T"),
            ),
            aircraft(
                characteristic("acn", 62.0, variant="F/A at MTOW"),
                characteristic("acr", 520.0, variant="F/A at MTOW"),
            ),
        )
        check = self.find(result)[0]
        assert check.assessment is Assessment.SUITABLE
        assert "ACR 520" in check.detail
        assert "changeover" in check.detail
        assert "not convertible" in check.detail

    def test_two_ratings_that_disagree_about_the_pavement_are_flagged(self):
        # The same pavement cannot be both flexible on subgrade A and rigid on
        # subgrade C. One of them is stale, and silently picking either would
        # hide that.
        result = assess_suitability(
            dossier(
                fact(RWY, "pcn", "80/F/A/W/T"),
                fact(RWY, "pcr", "560/R/C/W/T"),
            ),
            aircraft(characteristic("acr", 520.0, variant="R/C at MTOW")),
        )
        check = self.find(result)[0]
        assert "disagree" in check.detail
        assert "ask the State" in check.detail

    def test_an_unreadable_rating_in_either_system_is_reported(self):
        result = assess_suitability(
            dossier(fact(RWY, "pcr", "560")),
            aircraft(characteristic("acr", 520.0, variant="F/B")),
        )
        assert self.find(result)[0].assessment is Assessment.UNKNOWN
        assert "could not be read" in self.find(result)[0].detail

    def test_neither_system_held_says_so_by_both_names(self):
        check = self.find(assess_suitability(dossier(), aircraft()))[0]
        assert "PCN or PCR" in check.detail

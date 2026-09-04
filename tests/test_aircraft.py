"""Aircraft characteristics, the reference code table, and the pavement check.

Two things are being tested here, and they are different in kind.

The reference code table and the ACN/PCN rules are a **published standard read
into code** — ICAO Annex 14 Volume I, Table 1-1 and 1.6.3. The tests below walk
every band boundary, because a standard encoded approximately is worse than one
not encoded at all: it answers confidently and wrongly. One metre of wingspan
error moves an aeroplane across a code letter boundary and changes which
taxiways it may use.

The aircraft library is the opposite. It is tested for what it **refuses**: a
figure with no citation, a figure with no value, and operator data leaving the
tenant it belongs to. The no-mock-data rule governs source data entering the
product; the objects constructed here are test scaffolding, and the citations
on them are deliberately fictional so no reader mistakes them for held data.
"""

from __future__ import annotations

import ast
from datetime import datetime, timezone
from pathlib import Path

import pytest

from aeropub.aircraft import (
    CODE_LETTERS,
    CODE_NUMBERS,
    RFFS_CATEGORIES,
    TYRE_PRESSURE_MPA,
    AircraftType,
    Characteristic,
    Origin,
    PavementRating,
    PavementVerdict,
    RatingSystem,
    accommodates,
    code_letter,
    code_number,
    compare_pavement,
    reference_code,
    rffs_category,
)
from aeropub.provenance import SourceRef


def ref(**overrides) -> SourceRef:
    fields = dict(
        source_id="TEST",
        document="test fixture — not a real publication",
        locator="table 1",
        retrieved_at=datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc),
        content_hash="c" * 64,
        parser_id="test",
        parser_version="0.1.0",
    )
    fields.update(overrides)
    return SourceRef(**fields)


def characteristic(attribute: str, value, **overrides) -> Characteristic:
    fields = dict(attribute=attribute, value=value, source=ref(), origin=Origin.ACAP)
    fields.update(overrides)
    return Characteristic(**fields)


# --------------------------------------------------------------------------
# A figure cannot exist without a citation
# --------------------------------------------------------------------------


class TestCharacteristicRefuses:
    def test_a_figure_without_a_source_cannot_be_constructed(self):
        # The whole project is built against the recalled figure. This is the
        # gate: there is no way to put a wingspan into the model without saying
        # which document it was read from.
        with pytest.raises(TypeError) as caught:
            Characteristic(
                attribute="wingspan_m", value=64.8, source=None, origin=Origin.ACAP
            )
        assert "provenance" in str(caught.value)

    def test_a_string_is_not_a_citation(self):
        with pytest.raises(TypeError):
            Characteristic(
                attribute="wingspan_m",
                value=64.8,
                source="Boeing 777 ACAP",
                origin=Origin.ACAP,
            )

    def test_a_characteristic_with_no_value_is_a_gap_not_a_characteristic(self):
        # A None wingspan reads as "we hold nothing" everywhere else in the
        # platform. Storing it as a held characteristic would make a gap look
        # like an answer.
        with pytest.raises(ValueError) as caught:
            Characteristic(
                attribute="wingspan_m", value=None, source=ref(), origin=Origin.ACAP
            )
        assert "gap" in str(caught.value)

    def test_an_unnamed_attribute_is_refused(self):
        with pytest.raises(ValueError):
            characteristic("   ", 64.8)

    def test_a_figure_with_a_citation_is_accepted_and_describes_itself(self):
        item = characteristic("wingspan_m", 64.8, unit="m", variant="with winglets")
        assert item.value == 64.8
        assert "64.8 m" in item.describe()
        assert "with winglets" in item.describe()


# --------------------------------------------------------------------------
# ICAO Annex 14 Volume I, Table 1-1 — code element 2
# --------------------------------------------------------------------------


class TestCodeLetterBoundaries:
    @pytest.mark.parametrize(
        "wingspan_m,expected",
        [
            (0.0, "A"),
            (14.9, "A"),
            (15.0, "B"),  # bands are closed below, open above
            (23.9, "B"),
            (24.0, "C"),
            (35.9, "C"),
            (36.0, "D"),
            (51.9, "D"),
            (52.0, "E"),
            (64.9, "E"),
            (65.0, "F"),
            (79.9, "F"),
        ],
    )
    def test_every_wingspan_boundary(self, wingspan_m, expected):
        assert code_letter(wingspan_m=wingspan_m) == expected

    @pytest.mark.parametrize(
        "omgws_m,expected",
        [
            (0.0, "A"),
            (4.4, "A"),
            (4.5, "B"),
            (5.9, "B"),
            (6.0, "C"),
            (8.9, "C"),
            (9.0, "D"),  # D and E share this band; wheel span cannot separate them
            (13.9, "D"),
            (14.0, "F"),
            (15.9, "F"),
        ],
    )
    def test_every_wheel_span_boundary(self, omgws_m, expected):
        assert code_letter(omgws_m=omgws_m) == expected

    def test_a_wingspan_above_the_table_is_outside_the_code(self):
        # Annex 14 stops at 80 m. An aeroplane wider than Code F has no letter,
        # and inventing one would be worse than saying so.
        assert code_letter(wingspan_m=80.0) is None
        assert code_letter(omgws_m=16.0) is None

    def test_a_figure_outside_the_table_is_not_rescued_by_the_other_column(self):
        # A 90 m span with a 15 m wheel span is not Code F. Reporting F would
        # describe an aeroplane that does not exist, on the strength of the one
        # column that happened to be in range.
        assert code_letter(wingspan_m=90.0, omgws_m=15.0) is None
        assert code_letter(wingspan_m=64.8, omgws_m=20.0) is None

    def test_nothing_known_gives_no_letter(self):
        assert code_letter() is None
        assert code_letter(wingspan_m=None, omgws_m=None) is None

    def test_the_wingspan_bands_are_contiguous(self):
        # If a future edit leaves a hole between bands, some real wingspan
        # silently classifies as None. Prove there is no hole.
        for lower, upper in zip(CODE_LETTERS, CODE_LETTERS[1:]):
            assert lower.wingspan_to_m == upper.wingspan_from_m

    def test_the_wheel_span_column_is_contiguous_once_d_and_e_are_read_as_one(self):
        # The wheel span column is not strictly increasing, because D and E
        # share a band. Collapse the duplicate and it is contiguous; if a future
        # edit breaks that, some real wheel span classifies as None.
        column = []
        for band in CODE_LETTERS:
            if column and column[-1] == (band.omgws_from_m, band.omgws_to_m):
                continue
            column.append((band.omgws_from_m, band.omgws_to_m))
        for (_, upper), (lower, _) in zip(column, column[1:]):
            assert upper == lower

    def test_d_and_e_are_the_only_letters_sharing_a_wheel_span_band(self):
        shared = [
            (a.letter, b.letter)
            for a, b in zip(CODE_LETTERS, CODE_LETTERS[1:])
            if (a.omgws_from_m, a.omgws_to_m) == (b.omgws_from_m, b.omgws_to_m)
        ]
        assert shared == [("D", "E")]


class TestTheMoreDemandingCriterionWins:
    """Annex 14 1.6.3 — where the two criteria disagree, the higher applies."""

    def test_a_code_c_wingspan_with_a_code_d_wheel_span_is_code_d(self):
        # The trap, in both directions. Reading the wingspan column alone
        # under-reports and sends an aeroplane down a taxiway built for a
        # narrower gear. Letting the shared 9-14 m band vote for E over-reports
        # and awards a Code E letter to a 34 m span aeroplane.
        assert code_letter(wingspan_m=34.0) == "C"
        assert code_letter(omgws_m=10.0) == "D"
        assert code_letter(wingspan_m=34.0, omgws_m=10.0) == "D"

    def test_a_code_e_wingspan_with_a_code_c_wheel_span_is_code_e(self):
        assert code_letter(wingspan_m=60.0, omgws_m=7.0) == "E"

    def test_agreement_gives_that_letter(self):
        assert code_letter(wingspan_m=34.0, omgws_m=7.0) == "C"

    def test_wingspan_alone_separates_d_from_e(self):
        # Both letters share the 9-14 m wheel span band, so a wheel span of
        # 12.9 m can never on its own produce E.
        assert code_letter(omgws_m=12.9) == "D"
        assert code_letter(wingspan_m=64.8, omgws_m=12.9) == "E"

    def test_one_known_figure_still_classifies(self):
        assert code_letter(wingspan_m=64.8) == "E"
        assert code_letter(omgws_m=12.9) == "D"


# --------------------------------------------------------------------------
# Code element 1
# --------------------------------------------------------------------------


class TestCodeNumber:
    @pytest.mark.parametrize(
        "metres,expected",
        [
            (0.0, 1),
            (799.0, 1),
            (800.0, 2),
            (1199.0, 2),
            (1200.0, 3),
            (1799.0, 3),
            (1800.0, 4),
            (4500.0, 4),
        ],
    )
    def test_every_field_length_boundary(self, metres, expected):
        assert code_number(metres) == expected

    def test_a_negative_field_length_is_refused(self):
        with pytest.raises(ValueError):
            code_number(-1.0)

    def test_the_bands_are_contiguous(self):
        for (_, _, upper), (_, lower, _) in zip(CODE_NUMBERS, CODE_NUMBERS[1:]):
            assert upper == lower


class TestReferenceCode:
    def test_both_halves_known_gives_the_full_code(self):
        assert (
            reference_code(wingspan_m=64.8, omgws_m=12.9, reference_field_length_m=3100)
            == "4E"
        )

    def test_a_missing_field_length_gives_no_code(self):
        # Half a code is not a code. AD 2.2 wants "4E", not "E".
        assert reference_code(wingspan_m=64.8, omgws_m=12.9) is None

    def test_a_missing_span_gives_no_code(self):
        assert reference_code(reference_field_length_m=3100) is None


class TestAccommodates:
    def test_an_aerodrome_takes_its_own_letter_and_below(self):
        assert accommodates("E", "E")
        assert accommodates("E", "C")

    def test_an_aerodrome_does_not_take_a_larger_letter(self):
        assert not accommodates("C", "E")

    def test_letters_are_read_forgivingly_but_validated(self):
        assert accommodates(" e ", "c")
        with pytest.raises(ValueError):
            accommodates("G", "C")


# --------------------------------------------------------------------------
# ICAO Annex 14 Volume I, Table 9-1 and 9.2.2
# --------------------------------------------------------------------------


class TestRffsCategory:
    @pytest.mark.parametrize(
        "length_m,expected",
        [
            (0.0, 1), (8.9, 1),
            (9.0, 2), (11.9, 2),
            (12.0, 3), (17.9, 3),
            (18.0, 4), (23.9, 4),
            (24.0, 5), (27.9, 5),
            (28.0, 6), (38.9, 6),
            (39.0, 7), (48.9, 7),
            (49.0, 8), (60.9, 8),
            (61.0, 9), (75.9, 9),
            (76.0, 10), (89.9, 10),
        ],
    )
    def test_every_length_boundary(self, length_m, expected):
        assert rffs_category(overall_length_m=length_m) == expected

    def test_the_length_bands_are_contiguous(self):
        for lower, upper in zip(RFFS_CATEGORIES, RFFS_CATEGORIES[1:]):
            assert lower.length_to_m == upper.length_from_m
            assert upper.category == lower.category + 1

    def test_an_aeroplane_beyond_the_table_gets_no_category(self):
        # Annex 14 stops at 90 m. Beyond it the State determines the category,
        # and inventing a row would be a confident answer about an aeroplane
        # the standard does not cover.
        assert rffs_category(overall_length_m=90.0) is None
        assert rffs_category(overall_length_m=None) is None


class TestTheFuselageWidthBump:
    """Annex 14 9.2.2 — length selects, then width can promote."""

    def test_a_width_within_the_band_leaves_the_category_alone(self):
        assert rffs_category(overall_length_m=70.0, fuselage_width_m=6.2) == 9

    def test_a_width_above_the_band_promotes_one_category(self):
        # The half people forget. Same length, one category apart.
        assert rffs_category(overall_length_m=70.0, fuselage_width_m=7.2) == 10

    def test_a_width_exactly_at_the_maximum_does_not_promote(self):
        # The provision reads "greater than", not "at least".
        assert rffs_category(overall_length_m=70.0, fuselage_width_m=7.0) == 9

    def test_the_promotion_is_one_step_not_a_reclassification(self):
        # A short, very wide aeroplane goes up exactly one category — it is not
        # reclassified by width as though width were the primary criterion.
        assert rffs_category(overall_length_m=30.0, fuselage_width_m=9.0) == 7

    def test_the_table_stops_at_ten(self):
        # There is no category 11. An aeroplane already in the last row has
        # nowhere to be promoted to, and 10 is the most the table can say.
        assert rffs_category(overall_length_m=80.0, fuselage_width_m=9.0) == 10

    def test_without_a_width_the_answer_is_a_floor(self):
        # Documented as a floor, not an answer: 9.2.2 may still apply.
        assert rffs_category(overall_length_m=70.0) == 9
        assert rffs_category(overall_length_m=70.0, fuselage_width_m=7.2) == 10


# --------------------------------------------------------------------------
# Pavement
# --------------------------------------------------------------------------


class TestRatingParsing:
    def test_the_reported_five_part_form(self):
        rating = PavementRating.pcn("80/F/A/W/T")
        assert rating.number == 80.0
        assert rating.pavement == "F"
        assert rating.subgrade == "A"
        assert rating.tyre_pressure == "W"
        assert rating.method == "T"
        assert rating.is_technical
        assert rating.system is RatingSystem.ACN_PCN

    def test_it_round_trips(self):
        for text in ("80/F/A/W/T", "62/R/B/X/U", "105/F/C/Y/T"):
            assert str(PavementRating.pcn(text)) == text

    def test_whitespace_lower_case_and_a_numeric_tyre_limit_are_accepted(self):
        rating = PavementRating.pcn(" 55 / r / c / 1.25 / u ")
        assert (rating.number, rating.pavement, rating.subgrade) == (55.0, "R", "C")
        assert rating.tyre_pressure == "1.25"
        assert not rating.is_technical

    def test_a_leading_pcn_or_pcr_label_is_accepted(self):
        # States print the label in AD 2.12. It carries no information the
        # system argument does not already have, so it is tolerated, not read.
        assert PavementRating.pcn("PCN 80/F/A/W/T").number == 80.0
        assert PavementRating.pcr("PCR 560/F/B/W/T").number == 560.0

    @pytest.mark.parametrize(
        "text",
        [
            "80",  # the number alone, which is how the mistake starts
            "80/F/A/W",
            "80/F/E/W/T",  # there is no subgrade E
            "80/X/A/W/T",  # pavement is R or F
            "80/F/A/W/Q",  # method is T or U
            "",
        ],
    )
    def test_malformed_ratings_are_refused_rather_than_guessed(self, text):
        with pytest.raises(ValueError):
            PavementRating.pcn(text)


class TestTheTwoClassificationSystems:
    """ACN/PCN and ACR/PCR share a format and share nothing else."""

    def test_the_system_is_required_and_never_inferred(self):
        # A real PCR runs in the hundreds where the PCN for the same pavement
        # runs in the tens, so guessing from the number is wrong in the
        # permissive direction.
        with pytest.raises(TypeError) as caught:
            PavementRating.parse("560/F/B/W/T", system=None)
        assert "never inferred" in str(caught.value)

    def test_the_same_string_parses_in_either_system(self):
        legacy = PavementRating.pcn("80/F/A/W/T")
        current = PavementRating.pcr("80/F/A/W/T")
        assert legacy.number == current.number
        assert legacy != current
        assert legacy.label == "PCN"
        assert current.label == "PCR"
        assert legacy.system.aircraft_label == "ACN"
        assert current.system.aircraft_label == "ACR"

    def test_the_tyre_pressure_categories_kept_their_letters_and_moved_their_limits(self):
        # Exactly the kind of silent difference that makes reading a rating
        # without knowing its system dangerous.
        assert PavementRating.pcn("80/F/A/X/T").tyre_pressure_limit_mpa == 1.50
        assert PavementRating.pcr("560/F/A/X/T").tyre_pressure_limit_mpa == 1.75
        assert PavementRating.pcn("80/F/A/Y/T").tyre_pressure_limit_mpa == 1.00
        assert PavementRating.pcr("560/F/A/Y/T").tyre_pressure_limit_mpa == 1.25

    def test_z_and_w_did_not_move(self):
        for build in (PavementRating.pcn, PavementRating.pcr):
            assert build("80/F/A/Z/T").tyre_pressure_limit_mpa == 0.50
            assert build("80/F/A/W/T").tyre_pressure_limit_mpa is None

    def test_a_reported_figure_is_its_own_limit(self):
        assert PavementRating.pcn("80/F/A/1.4/T").tyre_pressure_limit_mpa == 1.4

    def test_describing_a_rating_names_its_system(self):
        # Anywhere a reader sees the number, they see which system it is in.
        assert PavementRating.pcr("560/F/B/W/T").describe() == "PCR 560/F/B/W/T"
        assert PavementRating.pcn("80/F/A/W/T").describe() == "PCN 80/F/A/W/T"


class TestPavementComparison:
    def test_an_acn_within_the_pcn_permits_unrestricted_operation(self):
        check = compare_pavement(
            acn=54, acn_pavement="F", acn_subgrade="A", pcn=PavementRating.pcn("80/F/A/W/T")
        )
        assert check.verdict is PavementVerdict.WITHIN
        assert check.verdict.permits_operation

    def test_an_equal_acn_is_within(self):
        check = compare_pavement(acn=80, pcn=PavementRating.pcn("80/F/A/W/T"))
        assert check.verdict is PavementVerdict.WITHIN

    def test_an_acn_above_the_pcn_is_an_overload_not_a_prohibition(self):
        check = compare_pavement(
            acn=93, acn_pavement="F", acn_subgrade="A", pcn=PavementRating.pcn("80/F/A/W/T")
        )
        assert check.verdict is PavementVerdict.OVERLOAD
        assert not check.verdict.permits_operation
        assert "consent" in check.detail

    def test_an_overload_against_an_experience_rating_says_so(self):
        check = compare_pavement(acn=93, pcn=PavementRating.pcn("80/F/A/W/U"))
        assert check.verdict is PavementVerdict.OVERLOAD
        assert "experience" in check.detail

    def test_a_different_pavement_type_is_not_compared(self):
        # 54 is well under 80, and the answer is still not "within".
        check = compare_pavement(
            acn=54, acn_pavement="R", acn_subgrade="A", pcn=PavementRating.pcn("80/F/A/W/T")
        )
        assert check.verdict is PavementVerdict.NOT_COMPARABLE
        assert "pavement" in check.detail

    def test_a_different_subgrade_is_not_compared(self):
        check = compare_pavement(
            acn=54, acn_pavement="F", acn_subgrade="C", pcn=PavementRating.pcn("80/F/A/W/T")
        )
        assert check.verdict is PavementVerdict.NOT_COMPARABLE
        assert "subgrade" in check.detail

    def test_a_missing_side_is_unknown_not_assumed(self):
        assert (
            compare_pavement(acn=None, pcn=PavementRating.pcn("80/F/A/W/T")).verdict
            is PavementVerdict.UNKNOWN
        )
        assert compare_pavement(acn=54, pcn=None).verdict is PavementVerdict.UNKNOWN

    def test_a_parsed_rating_object_is_accepted_directly(self):
        check = compare_pavement(acn=54, pcn=PavementRating.pcn("80/F/A/W/T"))
        assert check.verdict is PavementVerdict.WITHIN

    def test_an_acr_against_a_pcn_is_not_compared(self):
        # The failure this check exists for. ACR 700 against PCN 80 is not an
        # overload by 620; it is two numbers that mean nothing to each other,
        # and the naive comparison errs in the permissive direction whenever
        # the aircraft figure is the smaller one.
        check = compare_pavement(
            acn=700, acn_system=RatingSystem.ACR_PCR,
            pcn=PavementRating.pcn("80/F/A/W/T"),
        )
        assert check.verdict is PavementVerdict.NOT_COMPARABLE
        assert "28 November 2024" in check.detail
        assert "not convertible" in check.detail

    def test_an_acn_against_a_pcr_is_not_compared(self):
        # And the permissive direction: ACN 62 against PCR 560 would read as a
        # vast margin.
        check = compare_pavement(
            acn=62, acn_system=RatingSystem.ACN_PCN,
            pcn=PavementRating.pcr("560/F/B/W/T"),
        )
        assert check.verdict is PavementVerdict.NOT_COMPARABLE

    def test_matching_systems_compare_normally(self):
        within = compare_pavement(
            acn=520, acn_system=RatingSystem.ACR_PCR,
            acn_pavement="F", acn_subgrade="B",
            pcn=PavementRating.pcr("560/F/B/W/T"),
        )
        assert within.verdict is PavementVerdict.WITHIN
        assert "ACR 520" in within.detail and "PCR 560" in within.detail

    def test_the_system_check_runs_before_the_pavement_and_subgrade_checks(self):
        # Reporting a subgrade mismatch for a cross-system comparison would
        # send a reader to the wrong ACAP table.
        check = compare_pavement(
            acn=700, acn_system=RatingSystem.ACR_PCR,
            acn_pavement="R", acn_subgrade="D",
            pcn=PavementRating.pcn("80/F/A/W/T"),
        )
        assert check.verdict is PavementVerdict.NOT_COMPARABLE
        assert "classification systems" in check.detail


# --------------------------------------------------------------------------
# The library, and what stays inside the tenant
# --------------------------------------------------------------------------


class TestAircraftType:
    def test_a_type_begins_empty(self):
        aircraft = AircraftType(designator="B77W")
        assert aircraft.characteristics == ()
        assert aircraft.code_letter() is None
        assert aircraft.reference_code() is None
        assert "code letter unknown" in aircraft.describe()

    def test_an_unnamed_designator_is_refused(self):
        with pytest.raises(ValueError):
            AircraftType(designator="  ")

    def test_adding_characteristics_does_not_mutate_the_original(self):
        empty = AircraftType(designator="B77W")
        filled = empty.with_characteristics([characteristic("wingspan_m", 64.8)])
        assert empty.characteristics == ()
        assert filled.value("wingspan_m") == 64.8

    def test_a_missing_attribute_reads_as_a_gap(self):
        # There is no default wingspan. None here means "we hold nothing",
        # which is what the dossier renders.
        aircraft = AircraftType(designator="B77W")
        assert aircraft.get("wingspan_m") is None
        assert aircraft.value("wingspan_m") is None

    def test_the_code_letter_comes_from_held_figures_only(self):
        aircraft = AircraftType(designator="B77W").with_characteristics(
            [
                characteristic("wingspan_m", 64.8, unit="m"),
                characteristic("omgws_m", 12.9, unit="m"),
                characteristic("reference_field_length_m", 3100.0, unit="m"),
            ]
        )
        assert aircraft.code_letter() == "E"
        assert aircraft.reference_code() == "4E"
        assert "Code E" in aircraft.describe()

    def test_variants_are_kept_apart(self):
        # A figure without its variant is right for some aeroplanes carrying
        # this designator and wrong for others.
        aircraft = AircraftType(designator="B738").with_characteristics(
            [
                characteristic("wingspan_m", 34.3, variant="without winglets"),
                characteristic("wingspan_m", 35.8, variant="with winglets"),
            ]
        )
        assert aircraft.value("wingspan_m", variant="with winglets") == 35.8
        assert aircraft.value("wingspan_m", variant="without winglets") == 34.3
        assert aircraft.value("wingspan_m", variant="blended scimitar") is None


class TestOperatorDataStaysWithTheOperator:
    def test_operator_supplied_figures_are_not_redistributable(self):
        assert not Origin.OPERATOR.is_redistributable
        assert Origin.ACAP.is_redistributable
        assert Origin.STATE.is_redistributable

    def test_the_redistributable_view_excludes_operator_data(self):
        # FCOM-derived figures are licensed to the operator that bought the
        # aeroplane. They may drive that tenant's answers; they may not leave.
        aircraft = AircraftType(designator="B77W").with_characteristics(
            [
                characteristic("wingspan_m", 64.8, origin=Origin.ACAP),
                characteristic("mtow_kg", 351533.0, origin=Origin.OPERATOR),
                characteristic("apron_restriction", "stand 12 only", origin=Origin.STATE),
            ]
        )
        attributes = {c.attribute for c in aircraft.redistributable}
        assert attributes == {"wingspan_m", "apron_restriction"}

    def test_operator_data_still_answers_inside_the_tenant(self):
        aircraft = AircraftType(designator="B77W").with_characteristics(
            [characteristic("mtow_kg", 351533.0, origin=Origin.OPERATOR)]
        )
        assert aircraft.value("mtow_kg") == 351533.0


class TestNoFiguresAreShipped:
    """The module encodes the standard. It does not carry an aircraft library.

    A wingspan baked into source is a figure with no citation, which is exactly
    what :class:`Characteristic` refuses at runtime. This asserts the same
    discipline at the module level, so a future edit cannot slip a "handy
    default" table past the constructor by writing it as a literal.
    """

    def test_the_module_holds_no_type_designators(self):
        source = Path("src/aeropub/aircraft.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        literals = {
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        }
        # ICAO type designators are four characters, letter-then-alphanumeric.
        # Docstrings and messages are exempt only because they are longer than
        # four characters; a bare "B77W" constant would be caught.
        designators = {
            text
            for text in literals
            if len(text) == 4 and text[0].isalpha() and text.isalnum() and text.isupper()
        }
        assert designators == set(), f"aircraft figures do not belong in source: {designators}"

    def test_the_only_numbers_in_the_module_are_the_annex_14_table(self):
        source = Path("src/aeropub/aircraft.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        numbers = {
            float(node.value)
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, (int, float))
            and not isinstance(node.value, bool)
        }
        # Every number in the module must come from one of the Annex 14 tables
        # it encodes. Those tables are the published standard; anything else is
        # an aircraft figure, and aircraft figures belong in cited
        # Characteristics rather than in source.
        permitted = {
            band
            for row in CODE_LETTERS
            for band in (
                row.wingspan_from_m, row.wingspan_to_m, row.omgws_from_m, row.omgws_to_m
            )
            if band is not None
        }
        permitted |= {float(lower) for _, lower, _ in CODE_NUMBERS}
        permitted |= {float(number) for number, _, _ in CODE_NUMBERS}
        permitted |= {
            figure
            for row in RFFS_CATEGORIES
            for figure in (
                float(row.category), row.length_from_m, row.length_to_m,
                row.max_fuselage_width_m,
            )
        }
        permitted |= {
            limit
            for table in TYRE_PRESSURE_MPA.values()
            for limit in table.values()
            if limit is not None
        }
        permitted |= {0.0, 1.0}  # indices, and the negative-length guard
        assert numbers <= permitted, (
            "a numeric literal outside the Annex 14 tables appeared in "
            f"aircraft.py: {sorted(numbers - permitted)}. Aircraft figures belong "
            "in cited Characteristics, not in source."
        )

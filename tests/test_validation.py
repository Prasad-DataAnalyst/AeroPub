"""Tests for the validation harness.

The values here are chosen to be plainly right or plainly wrong against real
aeronautical bounds. Nothing stands in for a publication; these exercise the
checks themselves.
"""

from datetime import date, datetime, timezone

import pytest

from aeropub.facts import Fact, Precedence
from aeropub.provenance import SourceRef
from aeropub.validation import (
    CONTINUITY_THRESHOLD,
    RANGES,
    Severity,
    check_agreement,
    check_continuity,
    check_declared_distances,
    check_value,
    validate,
)

RWY = "OTHH/RWY34L"


def ref(source_id="QA-CAA", document="AIP AD 2.13"):
    return SourceRef(
        source_id=source_id, document=document, locator="AD 2.13",
        retrieved_at=datetime(2026, 8, 20, 9, 0, tzinfo=timezone.utc),
        content_hash="f" * 64, parser_id="ad2-parser", parser_version="1.0",
    )


def fact(attribute, value, *, entity=RWY, precedence=Precedence.AIP, source_id="QA-CAA"):
    return Fact(
        entity=entity, attribute=attribute, value=value, valid_from=date(2026, 1, 1),
        source=ref(source_id), precedence=precedence,
    )


def severities(findings):
    return [f.severity for f in findings]


class TestRangeChecks:
    def test_a_plausible_value_passes(self):
        assert check_value(fact("lda_m", 3900)) == []

    def test_a_negative_runway_is_impossible(self):
        findings = check_value(fact("lda_m", -100))
        assert severities(findings) == [Severity.INVALID]

    def test_a_fire_category_above_ten_is_impossible(self):
        # ICAO defines 1 to 10. Nothing else exists.
        assert severities(check_value(fact("rffs_category", 14))) == [Severity.INVALID]

    def test_a_fire_category_of_zero_is_impossible(self):
        assert severities(check_value(fact("rffs_category", 0))) == [Severity.INVALID]

    def test_feet_stored_as_metres_is_caught(self):
        # 13 000 ft is a real runway; 13 000 m is not, and this is the single
        # most likely extraction error.
        findings = check_value(fact("lda_m", 13000))
        assert severities(findings) == [Severity.INVALID]

    def test_an_unusual_but_possible_length_is_only_suspect(self):
        # 5 000 m exceeds the typical range but real runways reach 5 500 m, so
        # it is held for confirmation rather than discarded.
        findings = check_value(fact("lda_m", 5000))
        assert severities(findings) == [Severity.SUSPECT]
        assert "check the unit" in findings[0].message

    def test_inches_of_mercury_mislabelled_as_hectopascals_is_caught(self):
        assert severities(check_value(fact("qnh_hpa", 29.92))) == [Severity.INVALID]

    def test_a_non_numeric_value_where_a_number_belongs_is_invalid(self):
        assert severities(check_value(fact("lda_m", "3900 m"))) == [Severity.INVALID]

    def test_booleans_are_not_accepted_as_numbers(self):
        assert severities(check_value(fact("rffs_category", True))) == [Severity.INVALID]

    def test_an_unmodelled_attribute_is_left_alone(self):
        # Silence, not a guess. Inventing bounds for an attribute nobody has
        # modelled would quarantine correct data.
        assert check_value(fact("arresting_system", "EMAS")) == []

    @pytest.mark.parametrize("value", [-91, 91])
    def test_impossible_latitudes_are_caught(self, value):
        assert severities(check_value(fact("latitude", value))) == [Severity.INVALID]

    def test_a_below_sea_level_aerodrome_is_accepted(self):
        # The Dead Sea aerodromes sit near -1 270 ft. A "typical" band tight
        # enough to be useful here would flag them on every cycle.
        assert check_value(fact("elevation_ft", -1266)) == []
        assert check_value(fact("elevation_ft", 14472)) == []

    def test_an_impossible_elevation_is_still_caught(self):
        assert severities(check_value(fact("elevation_ft", 40000))) == [Severity.INVALID]

    def test_a_steep_approach_angle_is_accepted_as_unusual(self):
        assert severities(check_value(fact("papi_angle", 5.5))) == []
        assert severities(check_value(fact("papi_angle", 6.5))) == [Severity.SUSPECT]


class TestDeclaredDistances:
    def test_a_consistent_set_passes(self):
        facts = {
            "tora_m": fact("tora_m", 4000),
            "toda_m": fact("toda_m", 4200),
            "asda_m": fact("asda_m", 4100),
            "lda_m": fact("lda_m", 3900),
        }
        assert check_declared_distances(facts) == []

    def test_toda_shorter_than_tora_is_impossible(self):
        # A clearway can only add distance.
        findings = check_declared_distances({
            "tora_m": fact("tora_m", 4000), "toda_m": fact("toda_m", 3800),
        })
        assert severities(findings) == [Severity.INVALID]
        assert "clearway cannot subtract" in findings[0].message

    def test_asda_shorter_than_tora_is_impossible(self):
        findings = check_declared_distances({
            "tora_m": fact("tora_m", 4000), "asda_m": fact("asda_m", 3800),
        })
        assert severities(findings) == [Severity.INVALID]
        assert "stopway cannot subtract" in findings[0].message

    def test_lda_longer_than_tora_is_only_an_advisory(self):
        # Unusual, but legitimate where take-off run is shortened for obstacle
        # reasons while landing distance stays full length. Asserting this as an
        # invariant would quarantine correctly published data.
        findings = check_declared_distances({
            "tora_m": fact("tora_m", 3000), "lda_m": fact("lda_m", 3500),
        })
        assert severities(findings) == [Severity.ADVISORY]
        assert not findings[0].blocks_publication

    def test_a_displacement_consuming_the_whole_runway_is_impossible(self):
        findings = check_declared_distances({
            "tora_m": fact("tora_m", 3000),
            "displaced_threshold_m": fact("displaced_threshold_m", 3000),
        })
        assert severities(findings) == [Severity.INVALID]

    def test_zero_displacement_is_normal(self):
        findings = check_declared_distances({
            "tora_m": fact("tora_m", 3000),
            "displaced_threshold_m": fact("displaced_threshold_m", 0),
        })
        assert findings == []

    def test_a_partial_set_checks_only_what_is_present(self):
        assert check_declared_distances({"tora_m": fact("tora_m", 4000)}) == []


class TestContinuity:
    def test_no_history_means_nothing_to_compare(self):
        assert check_continuity(fact("lda_m", 3900), []) == []

    def test_a_small_movement_passes(self):
        # Works change a runway by tens of metres all the time.
        assert check_continuity(fact("lda_m", 3800), [fact("lda_m", 3900)]) == []

    def test_a_halving_is_held_for_confirmation(self):
        findings = check_continuity(fact("lda_m", 1900), [fact("lda_m", 3900)])
        assert severities(findings) == [Severity.SUSPECT]
        assert "against its own history" in findings[0].message

    def test_the_threshold_is_the_boundary(self):
        previous = 1000
        just_within = previous * (1 + CONTINUITY_THRESHOLD)
        beyond = just_within + 1
        assert check_continuity(fact("lda_m", just_within), [fact("lda_m", previous)]) == []
        assert check_continuity(fact("lda_m", beyond), [fact("lda_m", previous)])

    def test_the_most_recent_comparable_value_is_used(self):
        history = [fact("lda_m", 3900), fact("lda_m", "unparsed"), fact("lda_m", 3850)]
        assert check_continuity(fact("lda_m", 3800), history) == []

    def test_non_numeric_values_are_skipped(self):
        assert check_continuity(fact("surface", "asphalt"), [fact("surface", "concrete")]) == []


class TestCrossSourceAgreement:
    def test_one_source_cannot_disagree_with_itself(self):
        assert check_agreement([fact("lda_m", 3900)]) == []

    def test_agreeing_sources_produce_nothing(self):
        assert check_agreement([
            fact("lda_m", 3900, source_id="QA-CAA"),
            fact("lda_m", 3900, source_id="EAD"),
        ]) == []

    def test_independent_sources_disagreeing_is_a_finding(self):
        findings = check_agreement([
            fact("lda_m", 3900, source_id="QA-CAA"),
            fact("lda_m", 3500, source_id="EAD"),
        ])
        assert severities(findings) == [Severity.SUSPECT]
        assert "disagree" in findings[0].message

    def test_different_layers_disagreeing_is_the_system_working(self):
        # An AIP saying 3900 and a NOTAM saying 3100 is the CES layering doing
        # its job. Flagging it would light up every temporary restriction.
        assert check_agreement([
            fact("lda_m", 3900, precedence=Precedence.AIP, source_id="QA-CAA"),
            fact("lda_m", 3100, precedence=Precedence.NOTAM, source_id="QA-NOTAM"),
        ]) == []

    def test_different_entities_are_not_compared(self):
        assert check_agreement([
            fact("lda_m", 3900, entity="OTHH/RWY34L", source_id="A"),
            fact("lda_m", 3500, entity="OTHH/RWY16R", source_id="B"),
        ]) == []


class TestSeverityHandling:
    def test_invalid_and_suspect_both_block_publication(self):
        assert Severity.INVALID.blocks_publication
        assert Severity.SUSPECT.blocks_publication

    def test_an_advisory_does_not(self):
        # A harness that cries wolf gets switched off.
        assert not Severity.ADVISORY.blocks_publication


class TestValidateBatch:
    def test_a_clean_batch_produces_nothing(self):
        assert validate([
            fact("tora_m", 4000), fact("toda_m", 4200),
            fact("asda_m", 4100), fact("lda_m", 3900),
        ]) == []

    def test_range_and_relationship_failures_are_both_reported(self):
        findings = validate([
            fact("tora_m", 4000),
            fact("toda_m", 3800),          # impossible against TORA
            fact("rffs_category", 14),     # impossible on its own
        ])
        assert len(findings) == 2
        assert all(f.severity is Severity.INVALID for f in findings)

    def test_continuity_is_checked_when_history_is_supplied(self):
        findings = validate(
            [fact("lda_m", 1000)],
            history={(RWY, "lda_m"): [fact("lda_m", 3900)]},
        )
        assert any(f.rule == "continuity" for f in findings)

    def test_entities_are_not_mixed_when_checking_relationships(self):
        # One runway's TORA must never be compared with another's TODA.
        assert validate([
            fact("tora_m", 4000, entity="OTHH/RWY34L"),
            fact("toda_m", 3000, entity="OTHH/RWY16R"),
        ]) == []

    def test_a_value_that_failed_on_its_own_is_not_also_reported_in_relationships(self):
        # One extraction error should produce one finding. Reporting that an
        # impossible landing distance also exceeds the take-off run adds noise
        # to a queue a human has to work through.
        findings = validate([fact("tora_m", 4000), fact("lda_m", 13000)])
        assert len(findings) == 1
        assert findings[0].rule == "range"

    def test_relationships_are_still_checked_between_valid_values(self):
        findings = validate([fact("tora_m", 3000), fact("lda_m", 3500)])
        assert [f.rule for f in findings] == ["declared-distances"]

    def test_findings_describe_themselves_readably(self):
        finding = validate([fact("rffs_category", 14)])[0]
        assert finding.describe().startswith("[invalid] range:")


class TestRangeTable:
    @pytest.mark.parametrize("attribute", sorted(RANGES))
    def test_hard_bounds_enclose_typical_bounds(self, attribute):
        r = RANGES[attribute]
        assert r.hard_min < r.hard_max
        if r.typical_min is not None:
            assert r.hard_min <= r.typical_min
        if r.typical_max is not None:
            assert r.typical_max <= r.hard_max

    @pytest.mark.parametrize("attribute", sorted(RANGES))
    def test_every_range_reads_as_a_name_not_a_field(self, attribute):
        # "landing distance available", not "lda_m". Where the field name is
        # already the human name — latitude — repeating it is correct.
        label = RANGES[attribute].label
        assert label and "_" not in label

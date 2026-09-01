"""Tests for the generic operational impact layer.

The property under test throughout is that these statements hold for *any*
operator. A consequence that names a fleet, a network or a customer has leaked
tenant reasoning into the universal layer, and there is a test for that.
"""

from datetime import date, datetime, timezone

import pytest

from aeropub.changes import Change, ChangeKind
from aeropub.facts import Fact, Precedence
from aeropub.impact import RULES, Direction, assess
from aeropub.provenance import SourceRef

BEFORE = date(2026, 9, 1)
AFTER = date(2026, 10, 1)


def ref(document="AIP AD 2.13"):
    return SourceRef(
        source_id="QA-CAA", document=document, locator="AD 2.13",
        retrieved_at=datetime(2026, 8, 20, 9, 0, tzinfo=timezone.utc),
        content_hash="d" * 64, parser_id="ad2-parser", parser_version="1.0",
    )


def fact(value, attribute, entity="OTHH/RWY34L"):
    return Fact(entity=entity, attribute=attribute, value=value, valid_from=BEFORE,
                source=ref(), precedence=Precedence.AIP)


def change(attribute, before_value, after_value, entity="OTHH/RWY34L"):
    return Change(
        entity=entity, attribute=attribute, kind=ChangeKind.MODIFIED,
        before=fact(before_value, attribute, entity),
        after=fact(after_value, attribute, entity),
        observed_from=BEFORE, observed_to=AFTER,
    )


class TestDirection:
    def test_a_shorter_runway_is_worse(self):
        assert assess(change("lda_m", 3900, 3500)).direction is Direction.WORSE

    def test_a_longer_runway_is_better(self):
        assert assess(change("lda_m", 3500, 3900)).direction is Direction.BETTER

    def test_a_lower_rffs_category_is_worse(self):
        assert assess(change("rffs_category", 9, 7)).direction is Direction.WORSE

    def test_a_higher_rffs_category_is_better(self):
        assert assess(change("rffs_category", 7, 9)).direction is Direction.BETTER

    def test_a_larger_threshold_displacement_is_worse(self):
        # The one attribute where the number going up is the bad direction.
        assert assess(change("displaced_threshold_m", 0, 300)).direction is Direction.WORSE
        assert assess(change("displaced_threshold_m", 300, 0)).direction is Direction.BETTER

    def test_a_glide_path_change_has_no_better_or_worse(self):
        assert assess(change("papi_angle", 3.0, 3.2)).direction is Direction.NEUTRAL

    def test_a_non_numeric_change_is_neutral(self):
        assert assess(change("lda_m", "unknown", "3500")).direction is Direction.NEUTRAL


class TestOpportunities:
    def test_an_improvement_is_flagged_as_an_opportunity(self):
        # Nothing else in this domain tells an operator when a constraint lifted.
        assert assess(change("lda_m", 3500, 3900)).is_opportunity

    def test_a_worsening_is_not(self):
        assert not assess(change("lda_m", 3900, 3500)).is_opportunity

    def test_the_consequence_differs_by_direction(self):
        worse = assess(change("lda_m", 3900, 3500)).consequence
        better = assess(change("lda_m", 3500, 3900)).consequence
        assert worse != better
        assert "reduced" in worse
        assert "restored" in better


class TestSummary:
    def test_states_the_movement_and_its_size(self):
        summary = assess(change("lda_m", 3900, 3500)).summary
        assert "landing distance available" in summary
        assert "reduced by 400 m" in summary
        assert "3900 → 3500" in summary

    def test_names_the_entity(self):
        assert "OTHH/RWY34L" in assess(change("lda_m", 3900, 3500)).summary

    def test_uses_the_human_label_not_the_field_name(self):
        assert "lda_m" not in assess(change("lda_m", 3900, 3500)).summary

    def test_renders_fractional_values_without_noise(self):
        assert "0.2" in assess(change("papi_angle", 3.0, 3.2)).summary


class TestAddedAndRemoved:
    def test_an_addition_reports_the_new_value(self):
        c = Change("OTHH/RWY34L", "lda_m", ChangeKind.ADDED, None,
                   fact(3900, "lda_m"), BEFORE, AFTER)
        assert "published as 3900" in assess(c).summary

    def test_a_removal_warns_against_carrying_the_old_value_forward(self):
        c = Change("OTHH/RWY34L", "papi_angle", ChangeKind.REMOVED,
                   fact(3.0, "papi_angle"), None, BEFORE, AFTER)
        impact = assess(c)
        assert "withdrawn" in impact.summary
        assert "unknown" in impact.consequence

    def test_a_removed_rffs_category_says_suitability_cannot_be_assumed(self):
        c = Change("OTHH", "rffs_category", ChangeKind.REMOVED,
                   fact(9, "rffs_category", "OTHH"), None, BEFORE, AFTER)
        assert "cannot be assumed" in assess(c).consequence


class TestUnmodelledAttributes:
    def test_an_unknown_attribute_admits_it_rather_than_inventing(self):
        # A plausible sentence about an attribute nobody modelled is worse than
        # an admitted gap, because it reads exactly like one that was.
        impact = assess(change("some_unmodelled_field", 1, 2))
        assert not impact.assessed
        assert impact.direction is Direction.UNKNOWN
        assert "needs a human" in impact.consequence

    def test_it_still_states_the_change_factually(self):
        impact = assess(change("some_unmodelled_field", 1, 2))
        assert "1 → 2" in impact.summary

    def test_modelled_attributes_are_marked_assessed(self):
        assert assess(change("lda_m", 3900, 3500)).assessed


class TestUniversality:
    """Nothing here may assume who is reading it."""

    TENANT_WORDS = ("your", "our fleet", "you operate", "customer", "tenant")

    @pytest.mark.parametrize("attribute", sorted(RULES))
    def test_no_rule_addresses_a_specific_operator(self, attribute):
        rule = RULES[attribute]
        text = " ".join([rule.worse, rule.better, rule.changed, rule.added, rule.removed]).lower()
        for word in self.TENANT_WORDS:
            assert word not in text, f"{attribute} leaks tenant reasoning: {word!r}"

    @pytest.mark.parametrize("attribute", sorted(RULES))
    def test_every_rule_says_something_for_the_directions_it_claims(self, attribute):
        rule = RULES[attribute]
        if rule.higher_is_better is None:
            assert rule.changed, f"{attribute} has no direction but no changed text"
        else:
            assert rule.worse and rule.better, f"{attribute} claims a direction without both texts"

    @pytest.mark.parametrize("attribute", sorted(RULES))
    def test_every_rule_names_the_domains_it_touches(self, attribute):
        assert RULES[attribute].domains, f"{attribute} names no operational domain"

    @pytest.mark.parametrize("attribute", sorted(RULES))
    def test_every_rule_has_a_human_label(self, attribute):
        rule = RULES[attribute]
        assert rule.label and rule.label != attribute

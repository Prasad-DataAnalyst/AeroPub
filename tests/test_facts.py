"""Tests for the bitemporal fact model and CES resolution.

The worked example throughout is the one from the design document: RWY 34L
declared landing distance, published as 3900 m in the AIP, temporarily reduced
to 3500 m by a supplement for works, then to 3100 m by a NOTAM extending them.
It is used because it exercises every layer of the stack at once.

On the no-mock-data rule: it governs source data entering the product, not the
construction of objects to test resolution logic. Nothing here stands in for a
real publication.
"""

from datetime import date, datetime, timedelta, timezone

import pytest

from aeropub.facts import Fact, FactStore, Precedence
from aeropub.provenance import SourceRef

RWY = "OTHH/RWY34L"
LDA = "lda_m"


def ref(document: str) -> SourceRef:
    return SourceRef(
        source_id="QA-CAA",
        document=document,
        locator="AD 2.13",
        retrieved_at=datetime(2026, 10, 11, 14, 23, tzinfo=timezone.utc),
        content_hash="b" * 64,
        parser_id="ad2-parser",
        parser_version="1.0",
    )


def fact(value, precedence, valid_from, valid_to=None, document="AIP AD 2.13", **kw):
    return Fact(
        entity=RWY,
        attribute=LDA,
        value=value,
        valid_from=valid_from,
        valid_to=valid_to,
        source=ref(document),
        precedence=precedence,
        **kw,
    )


BASE = fact(3900, Precedence.AIP, date(2020, 1, 1))
SUP = fact(3500, Precedence.SUP, date(2026, 9, 1), date(2026, 11, 30), "AIP SUP 14/26")
NOTAM = fact(3100, Precedence.NOTAM, date(2026, 10, 12), date(2026, 10, 20), "NOTAM A2291/26")


class TestConstruction:
    def test_a_fact_cannot_exist_without_provenance(self):
        # The central invariant: no code path produces an unattributed value.
        with pytest.raises(TypeError, match="without provenance"):
            Fact(
                entity=RWY,
                attribute=LDA,
                value=3900,
                valid_from=date(2026, 1, 1),
                source=None,  # type: ignore[arg-type]
                precedence=Precedence.AIP,
            )

    def test_source_must_actually_be_a_source_ref(self):
        with pytest.raises(TypeError, match="SourceRef"):
            Fact(
                entity=RWY,
                attribute=LDA,
                value=3900,
                valid_from=date(2026, 1, 1),
                source="AIP AD 2.13",  # type: ignore[arg-type]
                precedence=Precedence.AIP,
            )

    def test_a_missing_value_is_a_coverage_gap_not_a_fact(self):
        with pytest.raises(ValueError, match="coverage gap"):
            fact(None, Precedence.AIP, date(2026, 1, 1))

    def test_validity_window_cannot_invert(self):
        with pytest.raises(ValueError, match="precedes"):
            fact(3900, Precedence.AIP, date(2026, 11, 30), date(2026, 9, 1))

    def test_naive_recorded_at_is_rejected(self):
        with pytest.raises(ValueError, match="timezone-aware"):
            fact(3900, Precedence.AIP, date(2026, 1, 1), recorded_at=datetime(2026, 1, 1))

    def test_superseded_cannot_precede_recorded(self):
        now = datetime(2026, 6, 1, tzinfo=timezone.utc)
        with pytest.raises(ValueError, match="precedes recorded_at"):
            fact(
                3900,
                Precedence.AIP,
                date(2026, 1, 1),
                recorded_at=now,
                superseded_at=now - timedelta(days=1),
            )

    @pytest.mark.parametrize("blank", ["", "  "])
    def test_entity_and_attribute_cannot_be_blank(self, blank):
        with pytest.raises(ValueError):
            Fact(
                entity=blank,
                attribute=LDA,
                value=1,
                valid_from=date(2026, 1, 1),
                source=ref("AIP"),
                precedence=Precedence.AIP,
            )


class TestValidTime:
    def test_open_ended_facts_apply_indefinitely(self):
        assert BASE.applies_on(date(2030, 1, 1))

    def test_window_is_inclusive_at_both_ends(self):
        assert SUP.applies_on(date(2026, 9, 1))
        assert SUP.applies_on(date(2026, 11, 30))

    def test_outside_the_window_it_does_not_apply(self):
        assert not SUP.applies_on(date(2026, 8, 31))
        assert not SUP.applies_on(date(2026, 12, 1))


class TestPrecedence:
    def test_layers_are_ordered_by_immediacy(self):
        assert Precedence.AIP < Precedence.AMDT < Precedence.SUP < Precedence.NOTAM

    def test_notam_wins_while_in_force(self):
        store = FactStore([BASE, SUP, NOTAM])
        winner = store.effective(RWY, LDA, date(2026, 10, 15))
        assert winner is not None
        assert winner.value == 3100
        assert winner.source.document == "NOTAM A2291/26"

    def test_supplement_resurfaces_when_the_notam_expires(self):
        # The layer beneath is not deleted; it is covered. This is the whole
        # reason resolution is a stack rather than an overwrite.
        store = FactStore([BASE, SUP, NOTAM])
        assert store.effective(RWY, LDA, date(2026, 10, 21)).value == 3500

    def test_base_resurfaces_when_the_supplement_expires(self):
        store = FactStore([BASE, SUP, NOTAM])
        assert store.effective(RWY, LDA, date(2026, 12, 1)).value == 3900

    def test_base_applies_before_any_override(self):
        store = FactStore([BASE, SUP, NOTAM])
        assert store.effective(RWY, LDA, date(2026, 8, 1)).value == 3900

    def test_equal_precedence_resolves_to_the_later_record(self):
        early = fact(3900, Precedence.AMDT, date(2026, 1, 1),
                     recorded_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
        late = fact(3800, Precedence.AMDT, date(2026, 1, 1),
                    recorded_at=datetime(2026, 2, 1, tzinfo=timezone.utc))
        store = FactStore([early, late])
        assert store.effective(RWY, LDA, date(2026, 6, 1)).value == 3800


class TestStack:
    def test_the_receipt_shows_every_layer_not_just_the_winner(self):
        store = FactStore([BASE, SUP, NOTAM])
        layers = store.stack(RWY, LDA, date(2026, 10, 15))
        assert [f.value for f in layers] == [3100, 3500, 3900]
        assert [f.precedence for f in layers] == [
            Precedence.NOTAM,
            Precedence.SUP,
            Precedence.AIP,
        ]

    def test_stack_excludes_layers_not_in_force(self):
        store = FactStore([BASE, SUP, NOTAM])
        assert [f.value for f in store.stack(RWY, LDA, date(2026, 10, 21))] == [3500, 3900]


class TestCoverageGap:
    def test_nothing_known_returns_none_rather_than_a_default(self):
        # A caller must render this as a gap. Returning a plausible default here
        # is exactly the silent failure the design forbids.
        store = FactStore([SUP])
        assert store.effective(RWY, LDA, date(2020, 1, 1)) is None

    def test_unknown_attribute_returns_none(self):
        store = FactStore([BASE])
        assert store.effective(RWY, "rffs_category", date(2026, 10, 15)) is None


class TestTransactionTime:
    """The time machine — what we believed, as distinct from what was true."""

    def test_a_fact_is_not_known_before_it_was_recorded(self):
        recorded = datetime(2026, 10, 11, tzinfo=timezone.utc)
        f = fact(3100, Precedence.NOTAM, date(2026, 10, 12), recorded_at=recorded)
        assert not f.was_known_at(recorded - timedelta(days=1))
        assert f.was_known_at(recorded)

    def test_a_superseded_belief_is_not_known_afterwards(self):
        recorded = datetime(2026, 10, 11, tzinfo=timezone.utc)
        dropped = datetime(2026, 10, 14, tzinfo=timezone.utc)
        f = fact(3100, Precedence.NOTAM, date(2026, 10, 12),
                 recorded_at=recorded, superseded_at=dropped)
        assert f.was_known_at(dropped - timedelta(seconds=1))
        assert not f.was_known_at(dropped)

    def test_resolution_can_be_asked_as_of_a_past_moment(self):
        # Before the NOTAM was issued we would have answered 3500, and the store
        # must still be able to reproduce that answer afterwards.
        notam_recorded = datetime(2026, 10, 11, 14, 23, tzinfo=timezone.utc)
        notam = fact(3100, Precedence.NOTAM, date(2026, 10, 12), date(2026, 10, 20),
                     "NOTAM A2291/26", recorded_at=notam_recorded)
        store = FactStore([BASE, SUP, notam])

        before = notam_recorded - timedelta(hours=1)
        assert store.effective(RWY, LDA, date(2026, 10, 15), as_known_at=before).value == 3500
        assert store.effective(RWY, LDA, date(2026, 10, 15)).value == 3100

    def test_superseded_returns_a_copy_leaving_the_original_intact(self):
        dropped = datetime(2026, 10, 14, tzinfo=timezone.utc)
        replacement = NOTAM.superseded(dropped)
        assert replacement.superseded_at == dropped
        assert NOTAM.superseded_at is None
        assert replacement.value == NOTAM.value


class TestStore:
    def test_holds_only_facts(self):
        with pytest.raises(TypeError, match="Fact"):
            FactStore().add("AIP says 3900")  # type: ignore[arg-type]

    def test_length_and_iteration(self):
        store = FactStore([BASE, SUP])
        assert len(store) == 2
        assert list(store) == [BASE, SUP]

    def test_history_is_ordered_by_when_we_learned_it(self):
        first = fact(3900, Precedence.AIP, date(2020, 1, 1),
                     recorded_at=datetime(2020, 1, 1, tzinfo=timezone.utc))
        second = fact(3500, Precedence.SUP, date(2026, 9, 1),
                      recorded_at=datetime(2026, 8, 20, tzinfo=timezone.utc))
        store = FactStore([second, first])
        assert store.history(RWY, LDA) == [first, second]

    def test_entities_and_attributes_are_discoverable(self):
        other = Fact(
            entity="OTHH",
            attribute="rffs_category",
            value=9,
            valid_from=date(2026, 1, 1),
            source=ref("AIP AD 2.6"),
            precedence=Precedence.AIP,
        )
        store = FactStore([BASE, other])
        assert store.entities() == {RWY, "OTHH"}
        assert store.attributes(RWY) == {LDA}
        assert store.attributes("OTHH") == {"rffs_category"}

"""The forward view.

The worked example is the design document's, as in ``test_facts.py``: a
supplement shortening RWY 34L over a base AIP figure. The no-mock-data rule
governs source data entering the product, not the construction of objects to
test resolution logic.

The assertion this module lives or dies by is the off-by-one. A window with
``valid_to`` of the 20th applies *on* the 20th, so the state changes on the
21st. Getting that wrong by a day is the difference between telling a
dispatcher the restriction lifts before their flight and after it.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from aeropub.facts import Fact, FactStore, Precedence
from aeropub.horizon import DEFAULT_DAYS, Horizon, Trigger, horizon
from aeropub.provenance import SourceRef

TODAY = date(2026, 9, 3)
RWY = "OTHH/RWY34L"


def ref(document: str, locator: str = "AD 2.13") -> SourceRef:
    return SourceRef(
        source_id="QA-CAA", document=document, locator=locator,
        retrieved_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
        content_hash="b" * 64, parser_id="eaip-eurocontrol", parser_version="0.1.0",
    )


def fact(entity, attribute, value, valid_from, valid_to=None,
         precedence=Precedence.AIP, document="AIP AD 2.13", locator="AD 2.13",
         recorded_at=None):
    kwargs = {}
    if recorded_at is not None:
        kwargs["recorded_at"] = recorded_at
    return Fact(entity=entity, attribute=attribute, value=value,
                valid_from=valid_from, valid_to=valid_to,
                source=ref(document, locator), precedence=precedence, **kwargs)


@pytest.fixture
def store():
    facts = FactStore()
    facts.add(fact(RWY, "lda_m", 3900, date(2026, 1, 1)))
    # A supplement shortening it, ending on the 20th.
    facts.add(fact(RWY, "lda_m", 3100, date(2026, 6, 1), date(2026, 9, 20),
                   precedence=Precedence.SUP, document="AIP SUP 04/26"))
    # A NOTAM downgrading RFFS, ending on the 12th.
    facts.add(fact("OTHH", "rffs_category", 9, date(2026, 1, 1), locator="AD 2.6",
                   document="AIP AD 2.6"))
    facts.add(fact("OTHH", "rffs_category", 7, date(2026, 9, 5), date(2026, 9, 12),
                   precedence=Precedence.NOTAM, document="NOTAM A1234/26",
                   locator="AD 2.6"))
    # An amendment already published for next cycle.
    facts.add(fact(RWY, "papi_angle", 3.2, date(2026, 10, 1),
                   precedence=Precedence.AMDT, document="AIP AMDT 10/26",
                   locator="AD 2.14"))
    return facts


@pytest.fixture
def ahead(store):
    return horizon(store, "OTHH", from_date=TODAY, days=60)


class TestTheOffByOne:
    def test_a_window_applies_on_its_closing_date(self, ahead):
        # valid_to of the 20th means the supplement is still in force on the
        # 20th. The reversion is on the 21st. A day either way is the
        # difference between a restriction lifting before a flight or after it.
        lda = [t for t in ahead.transitions if t.attribute == "lda_m"]
        assert [t.on for t in lda] == [date(2026, 9, 21)]

    def test_a_window_opens_on_its_first_date(self, ahead):
        rffs = [t for t in ahead.transitions if t.attribute == "rffs_category"]
        assert rffs[0].on == date(2026, 9, 5)

    def test_days_away_is_counted_from_the_start(self, ahead):
        lda = [t for t in ahead.transitions if t.attribute == "lda_m"][0]
        assert lda.days_away == (date(2026, 9, 21) - TODAY).days == 18


class TestTriggers:
    def test_a_layer_expiring_is_a_reversion_nobody_publishes(self, ahead):
        lda = [t for t in ahead.transitions if t.attribute == "lda_m"][0]
        assert lda.trigger is Trigger.REVERSION
        assert not lda.is_announced
        assert "Nothing will be published" in lda.why()
        assert lda.before.precedence is Precedence.SUP
        assert lda.after.precedence is Precedence.AIP

    def test_a_layer_beginning_is_published(self, ahead):
        rffs = [t for t in ahead.transitions if t.attribute == "rffs_category"]
        assert rffs[0].trigger is Trigger.PUBLISHED
        assert rffs[0].is_announced
        assert "NOTAM A1234/26" in rffs[0].why()

    def test_an_expiry_with_nothing_beneath_is_a_withdrawal(self):
        facts = FactStore()
        facts.add(fact("OTHH", "papi_angle", 3.0, date(2026, 8, 1), date(2026, 9, 30),
                       precedence=Precedence.SUP, document="AIP SUP 06/26",
                       locator="AD 2.14"))
        ahead = horizon(facts, "OTHH", from_date=TODAY, days=60)
        assert [t.trigger for t in ahead.transitions] == [Trigger.WITHDRAWAL]
        assert "do not carry the previous figure forward" in ahead.transitions[0].why()

    def test_a_new_layer_at_the_same_precedence_is_published(self):
        facts = FactStore()
        facts.add(fact(RWY, "lda_m", 3900, date(2026, 1, 1), date(2026, 9, 30)))
        facts.add(fact(RWY, "lda_m", 3800, date(2026, 10, 1)))
        ahead = horizon(facts, "OTHH", from_date=TODAY, days=60)
        assert [t.trigger for t in ahead.transitions] == [Trigger.PUBLISHED]

    def test_only_published_counts_as_announced(self):
        assert Trigger.PUBLISHED.is_announced
        assert not Trigger.REVERSION.is_announced
        assert not Trigger.WITHDRAWAL.is_announced


class TestWhatItSurfaces:
    def test_the_unannounced_set_is_what_nobody_will_tell_you(self, ahead):
        assert {t.attribute for t in ahead.unannounced} == {"lda_m", "rffs_category"}
        assert {t.attribute for t in ahead.announced} == {"rffs_category", "papi_angle"}
        assert len(ahead.unannounced) + len(ahead.announced) == len(ahead.transitions)

    def test_a_restored_value_reads_as_an_opportunity_not_a_warning(self, ahead):
        # 800 m of landing distance coming back is money, and nothing else in
        # this domain tells an operator when a constraint lifts.
        lda = [t for t in ahead.transitions if t.attribute == "lda_m"][0]
        assert lda.impact.is_opportunity
        assert "restored" in lda.impact.consequence

    def test_each_transition_cites_the_layer_that_is_expiring(self, ahead):
        lda = [t for t in ahead.transitions if t.attribute == "lda_m"][0]
        assert lda.before.source.document == "AIP SUP 04/26"
        assert "AIP SUP 04/26" in ahead.render()

    def test_transitions_carry_their_aip_section(self, ahead):
        placed = {t.attribute: t.section.code for t in ahead.transitions if t.section}
        assert placed["lda_m"] == "AD 2.13"
        assert placed["rffs_category"] == "AD 2.6"
        assert placed["papi_angle"] == "AD 2.14"

    def test_a_domain_lens_works_on_the_forward_view_too(self, ahead):
        assert {t.attribute for t in ahead.for_domain("dispatch")} == {
            "lda_m", "rffs_category"
        }


class TestWindow:
    def test_nothing_before_the_start_is_reported(self, store):
        ahead = horizon(store, "OTHH", from_date=date(2026, 9, 13), days=60)
        assert all(t.on > date(2026, 9, 13) for t in ahead.transitions)

    def test_the_last_day_of_the_window_is_included(self, store):
        # days=10 from the 3rd ends on the 13th, and the RFFS reversion falls
        # on the 13th. A window that excluded its own last day would drop a
        # change on the very date a planner asked about.
        ahead = horizon(store, "OTHH", from_date=TODAY, days=10)
        assert ahead.through == date(2026, 9, 13)
        assert [t.on for t in ahead.transitions] == [date(2026, 9, 5), date(2026, 9, 13)]

    def test_nothing_past_the_end_is_reported(self, store):
        ahead = horizon(store, "OTHH", from_date=TODAY, days=9)
        assert ahead.through == date(2026, 9, 12)
        assert [t.on for t in ahead.transitions] == [date(2026, 9, 5)]

    def test_the_first_day_of_the_window_is_excluded(self, store):
        # A change on the start date is the present, not the horizon — the
        # dossier is what states it.
        ahead = horizon(store, "OTHH", from_date=date(2026, 9, 5), days=60)
        assert all(t.on > date(2026, 9, 5) for t in ahead.transitions)

    def test_within_narrows_without_recomputing(self, ahead):
        assert {t.attribute for t in ahead.within(7)} == {"rffs_category"}
        assert len(ahead.within(60)) == len(ahead.transitions)

    def test_an_explicit_end_date_overrides_days(self, store):
        ahead = horizon(store, "OTHH", from_date=TODAY, through=date(2026, 9, 30))
        assert ahead.through == date(2026, 9, 30)
        assert all(t.on <= date(2026, 9, 30) for t in ahead.transitions)

    def test_the_default_window_is_three_airac_cycles(self):
        assert DEFAULT_DAYS == 84

    def test_a_reversed_window_is_refused(self, store):
        with pytest.raises(ValueError, match="precedes"):
            horizon(store, "OTHH", from_date=TODAY, through=TODAY - timedelta(days=1))

    def test_an_empty_entity_is_refused(self, store):
        with pytest.raises(ValueError, match="non-empty"):
            horizon(store, "  ")


class TestScope:
    def test_facts_on_a_runway_belong_to_the_aerodromes_horizon(self, ahead):
        assert any(t.entity == RWY for t in ahead.transitions)
        assert any(t.entity == "OTHH" for t in ahead.transitions)

    def test_another_aerodrome_is_not_included(self, store):
        store.add(fact("OTBD/RWY15", "lda_m", 4570, date(2026, 6, 1), date(2026, 9, 20),
                       precedence=Precedence.SUP))
        ahead = horizon(store, "OTHH", from_date=TODAY, days=60)
        assert all(not t.entity.startswith("OTBD") for t in ahead.transitions)

    def test_the_entity_is_normalised(self, store):
        assert horizon(store, " othh ", from_date=TODAY, days=60).entity == "OTHH"


class TestHonesty:
    def test_an_empty_horizon_says_it_is_not_a_forecast(self):
        rendered = horizon(FactStore(), "OTHH", from_date=TODAY).render()
        assert "No dated change ahead in what is held" in rendered
        assert "not a forecast" in rendered

    def test_the_report_states_what_it_is_silent_about(self, ahead):
        rendered = ahead.render()
        assert "silent about any not yet issued" in rendered

    def test_the_unannounced_are_printed_first_and_labelled(self, ahead):
        rendered = ahead.render()
        assert "NOTHING WILL BE PUBLISHED TO TELL YOU ABOUT THESE" in rendered
        assert rendered.index("NOTHING WILL BE PUBLISHED") < rendered.index("PUBLISHED —")

    def test_the_belief_it_was_computed_from_is_recorded(self, store):
        # A horizon is only as complete as what was held when it was taken.
        # Recording that is what makes it reproducible rather than arguable.
        known_at = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
        ahead = horizon(store, "OTHH", from_date=TODAY, days=60, as_known_at=known_at)
        assert ahead.as_known_at == known_at

    def test_a_horizon_can_be_recomputed_as_it_stood_before_a_notam_arrived(self):
        # Transaction time, not valid time: what would we have said on Monday?
        early = datetime(2026, 9, 1, tzinfo=timezone.utc)
        late = datetime(2026, 9, 2, tzinfo=timezone.utc)
        facts = FactStore()
        facts.add(fact(RWY, "lda_m", 3900, date(2026, 1, 1), recorded_at=early))
        facts.add(fact(RWY, "lda_m", 3100, date(2026, 9, 10), date(2026, 9, 20),
                       precedence=Precedence.NOTAM, document="NOTAM A9999/26",
                       recorded_at=late))

        before_it_arrived = horizon(facts, "OTHH", from_date=TODAY, days=60,
                                    as_known_at=early)
        after_it_arrived = horizon(facts, "OTHH", from_date=TODAY, days=60,
                                   as_known_at=late)
        assert before_it_arrived.transitions == ()
        assert len(after_it_arrived.transitions) == 2

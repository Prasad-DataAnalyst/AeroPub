"""What was knowable and when — as distinct from what turned out to be true.

The distinction this module exists to protect: "what was in force on 15 October"
and "what anybody could have known on 15 October" are different documents. Every
system with a date picker returns the first and calls it history. For a safety
investigation the second is the only honest answer, because reporting the
corrected record as contemporaneous quietly blames a crew for not knowing
something nobody had sent them yet.

So the tests below are mostly about the gap between the two, and about the one
measurement that falls out of it: how long a change was in force before the
platform held it.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from aeropub.aip import AipCoverage
from aeropub.dossier import build
from aeropub.facts import Fact, FactStore, Precedence
from aeropub.notam_register import NotamRegister
from aeropub.provenance import SourceRef
from aeropub.retrospect import (
    Blindness,
    LateArrival,
    Retrospect,
    blind_spots,
    retrospect,
)

AD = "OTHH"
RWY = "OTHH/RWY34L"

#: We began watching this aerodrome on 1 September.
WATCHED_FROM = datetime(2026, 9, 1, tzinfo=timezone.utc)
#: A NOTAM effective from the 11th that only reached us on the 14th.
ARRIVED_LATE = datetime(2026, 10, 14, 9, 0, tzinfo=timezone.utc)
#: The morning in question.
THE_MORNING = datetime(2026, 10, 12, 6, 0, tzinfo=timezone.utc)
THE_DAY = date(2026, 10, 12)
#: Every query needs an explicit horizon: the fixtures are dated later than the
#: session clock, and a default of "now" would silently drop them.
AFTERWARDS = datetime(2026, 11, 1, tzinfo=timezone.utc)


def ref(document: str, read_at: datetime) -> SourceRef:
    return SourceRef(
        source_id="QA-CAA", document=document, locator="AD 2.6",
        retrieved_at=read_at, content_hash="a" * 64,
        parser_id="test", parser_version="1",
    )


def fact(entity, attribute, value, *, valid_from, recorded_at,
         precedence=Precedence.AIP, valid_to=None, document="AIP") -> Fact:
    return Fact(
        entity=entity, attribute=attribute, value=value,
        valid_from=valid_from, valid_to=valid_to,
        source=ref(document, recorded_at), precedence=precedence,
        recorded_at=recorded_at,
    )


def store_with_late_notam() -> FactStore:
    """The standing AIP, plus a NOTAM that reached us three days late."""
    return FactStore([
        fact(AD, "rffs_category", 9, valid_from=date(2026, 1, 1),
             recorded_at=WATCHED_FROM),
        fact(RWY, "lda_m", 3900.0, valid_from=date(2026, 1, 1),
             recorded_at=WATCHED_FROM),
        fact(AD, "rffs_category", 7, valid_from=date(2026, 10, 11),
             valid_to=date(2026, 10, 31), recorded_at=ARRIVED_LATE,
             precedence=Precedence.NOTAM, document="NOTAM A2291/26"),
    ])


# --------------------------------------------------------------------------
# The two questions, kept apart
# --------------------------------------------------------------------------


class TestValidTimeIsNotTransactionTime:
    def test_the_corrected_record_shows_the_notam(self):
        # "What was in force on the 12th" — today's holdings, filtered to that
        # day. The NOTAM was effective from the 11th, so it wins.
        held = store_with_late_notam()
        assert held.effective(AD, "rffs_category", THE_DAY).value == 7

    def test_what_was_knowable_that_morning_does_not(self):
        # The same day, asked as "what could anybody have printed that
        # morning". The NOTAM had not reached us, so Category 9 is the honest
        # answer — and it is the one a crew acted on.
        held = store_with_late_notam()
        assert held.effective(
            AD, "rffs_category", THE_DAY, as_known_at=THE_MORNING
        ).value == 9

    def test_the_dossier_threads_the_distinction_through(self):
        held = store_with_late_notam()
        corrected = build(AD, facts=held, coverage=AipCoverage(),
                          register=NotamRegister(), as_at=THE_MORNING, on=THE_DAY)
        contemporaneous = build(AD, facts=held, coverage=AipCoverage(),
                                register=NotamRegister(), as_at=THE_MORNING,
                                on=THE_DAY, as_known_at=THE_MORNING)
        values = lambda d: {v.attribute: v.value for v in d.values()}
        assert values(corrected)["rffs_category"] == 7
        assert values(contemporaneous)["rffs_category"] == 9

    def test_a_retrospective_dossier_says_it_is_one(self):
        # A printed retrospective view that does not announce itself is
        # indistinguishable from a current one.
        printed = build(AD, facts=store_with_late_notam(), coverage=AipCoverage(),
                        register=NotamRegister(), as_at=THE_MORNING,
                        on=THE_DAY, as_known_at=THE_MORNING).render()
        assert "RETROSPECTIVE" in printed
        assert "not from what is known now" in printed

    def test_a_current_dossier_carries_no_such_claim(self):
        printed = build(AD, facts=store_with_late_notam(), coverage=AipCoverage(),
                        register=NotamRegister(), as_at=THE_MORNING,
                        on=THE_DAY).render()
        assert "RETROSPECTIVE" not in printed


# --------------------------------------------------------------------------
# The comparison
# --------------------------------------------------------------------------


class TestRetrospect:
    def looking_back(self) -> Retrospect:
        return retrospect(store_with_late_notam(), AD,
                          on=THE_DAY, as_known_at=THE_MORNING)

    def test_it_names_what_moved(self):
        moved = {r.attribute: r for r in self.looking_back().changed}
        assert set(moved) == {"rffs_category"}
        assert moved["rffs_category"].restated
        assert "9 then; 7 now" in moved["rffs_category"].describe()

    def test_unchanged_attributes_are_kept_rather_than_dropped(self):
        # An audit wants to see what was examined, not only what differed.
        looked = self.looking_back()
        assert len(looked.revisions) == 2
        assert len(looked.changed) == 1

    def test_a_record_that_did_not_move_says_so_explicitly(self):
        # True is a real and useful answer, and is stated rather than left to
        # be inferred from an empty list.
        steady = FactStore([
            fact(AD, "rffs_category", 9, valid_from=date(2026, 1, 1),
                 recorded_at=WATCHED_FROM),
        ])
        looked = retrospect(steady, AD, on=THE_DAY, as_known_at=THE_MORNING)
        assert looked.is_faithful
        assert "FAITHFUL" in looked.render()
        assert "is what it says today" in looked.render()

    def test_a_value_we_only_learned_later_reads_as_appeared(self):
        later = FactStore([
            fact(AD, "rffs_category", 9, valid_from=date(2026, 1, 1),
                 recorded_at=ARRIVED_LATE),
        ])
        revision = retrospect(later, AD, on=THE_DAY,
                              as_known_at=THE_MORNING).revisions[0]
        assert revision.appeared
        assert "nothing held then" in revision.describe()

    def test_it_reaches_everything_beneath_the_aerodrome(self):
        assert {r.entity for r in self.looking_back().revisions} == {AD, RWY}

    def test_a_naive_moment_is_refused(self):
        # Getting this wrong by a timezone is how a retrospective answer
        # becomes fiction.
        with pytest.raises(ValueError) as caught:
            retrospect(store_with_late_notam(), AD, on=THE_DAY,
                       as_known_at=datetime(2026, 10, 12, 6, 0))
        assert "becomes fiction" in str(caught.value)

    def test_both_dates_are_required(self):
        # Defaulting either would let a caller ask the ambiguous question this
        # module exists to separate.
        with pytest.raises(TypeError):
            retrospect(store_with_late_notam(), AD, on=THE_DAY)


class TestTheNotamLimitationIsStated:
    def test_a_retrospect_declares_notam_are_not_retrospective(self):
        # The register records when a NOTAM is effective, not when we learned
        # of it. Presenting a mixed document as a clean one would be worse than
        # not offering the view at all.
        looked = retrospect(store_with_late_notam(), AD,
                            on=THE_DAY, as_known_at=THE_MORNING)
        assert not looked.notam_is_retrospective
        assert "NOTAM are NOT included" in looked.render()

    def test_the_limitation_travels_on_the_document(self):
        # Carried as a field, so it reaches a JSON consumer that never reads a
        # docstring.
        assert "notam_is_retrospective" in Retrospect.__annotations__


# --------------------------------------------------------------------------
# Blindness — a measure of us, not of the State
# --------------------------------------------------------------------------


class TestBlindness:
    def measured(self) -> Blindness:
        return blind_spots(store_with_late_notam(), AD, through=AFTERWARDS)

    def test_it_measures_the_window_a_change_was_in_force_before_we_held_it(self):
        # Effective 11 October, held 14 October at 0900Z. Measured from the
        # start of the effective day, which is the conservative direction for a
        # measure of our own lateness.
        late = self.measured().late
        assert len(late) == 1
        assert late[0].blind_hours == 81.0
        assert "81h blind" in late[0].describe()

    def test_a_standing_value_from_before_we_watched_is_not_blindness(self):
        # The false positive this guards. An AIP value effective since January,
        # first read when the aerodrome was onboarded in September, is not eight
        # months of blindness — we were not watching. Counting it would make
        # every new source look catastrophic and bury the case that matters.
        arrivals = {a.fact.attribute: a for a in self.measured().arrivals}
        standing = arrivals["lda_m"]
        assert standing.predates_watching
        assert standing.blind == timedelta(0)
        assert not standing.was_blind

    def test_a_change_held_before_it_takes_effect_is_not_blindness_either(self):
        # The healthy case: an AIRAC amendment held 42 days ahead.
        ahead = FactStore([
            fact(AD, "rffs_category", 9, valid_from=date(2026, 1, 1),
                 recorded_at=WATCHED_FROM),
            fact(AD, "rffs_category", 8, valid_from=date(2026, 12, 1),
                 recorded_at=datetime(2026, 10, 20, tzinfo=timezone.utc)),
        ])
        assert blind_spots(ahead, AD, through=AFTERWARDS).late == ()

    def test_the_worst_window_is_reported_not_only_the_mean(self):
        # A collection usually prompt and occasionally three days late is not
        # the same as one uniformly ninety minutes late, and averaging hides
        # exactly the case an investigation is looking for.
        measured = self.measured()
        assert measured.worst is not None
        assert measured.worst.blind_hours == 81.0
        assert measured.summary()["worst_hours"] == 81.0

    def test_nothing_late_reports_a_clean_measure(self):
        clean = FactStore([
            fact(AD, "rffs_category", 9, valid_from=date(2026, 1, 1),
                 recorded_at=WATCHED_FROM),
        ])
        measured = blind_spots(clean, AD, through=AFTERWARDS)
        assert measured.late == ()
        assert measured.worst is None
        assert measured.mean_blind_hours == 0.0

    def test_an_entity_we_hold_nothing_for_measures_nothing(self):
        assert blind_spots(FactStore(), AD).arrivals == ()

    def test_the_horizon_excludes_what_we_had_not_yet_learned(self):
        # Asked as of a moment before the NOTAM reached us, it is not yet a
        # late arrival — it is not an arrival at all.
        before = blind_spots(store_with_late_notam(), AD, through=THE_MORNING)
        assert before.late == ()

    def test_a_retrospect_does_not_truncate_its_blindness_at_the_moment_asked(self):
        # The late arrival is precisely the value that reached us AFTER the
        # moment in question. Filtering to that moment would hide the only
        # thing worth seeing.
        looked = retrospect(store_with_late_notam(), AD,
                            on=THE_DAY, as_known_at=THE_MORNING)
        assert isinstance(looked.blindness, Blindness)


class TestLateArrivalArithmetic:
    def build(self, *, valid_from, recorded_at, watching_since) -> LateArrival:
        return LateArrival(
            fact=fact(AD, "x", 1, valid_from=valid_from, recorded_at=recorded_at),
            watching_since=watching_since,
        )

    def test_measured_from_the_start_of_the_effective_day(self):
        # A valid_from is a date and a recorded_at is an instant; one
        # convention is needed and the conservative one makes the window as
        # long as it could have been.
        arrival = self.build(
            valid_from=date(2026, 10, 11),
            recorded_at=datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc),
            watching_since=WATCHED_FROM,
        )
        assert arrival.blind_hours == 12.0

    def test_same_day_before_midnight_is_impossible_so_never_negative(self):
        arrival = self.build(
            valid_from=date(2026, 10, 11),
            recorded_at=datetime(2026, 10, 10, tzinfo=timezone.utc),
            watching_since=WATCHED_FROM,
        )
        assert arrival.blind == timedelta(0)
        assert not arrival.was_blind


class TestTheDenominatorIsHonest:
    """An attribute with nothing on either side was not compared.

    Counting it as agreement inflates the denominator: "4 of 5 read the same"
    implies four were examined and matched, when four had nothing to compare
    because their validity had not begun. In an audit document that difference
    matters.
    """

    def not_yet_in_force(self) -> FactStore:
        return FactStore([
            # In force from November — nothing on either side on the 12th of
            # October.
            fact(AD, "rffs_category", 9, valid_from=date(2026, 11, 1),
                 recorded_at=WATCHED_FROM),
            fact(RWY, "lda_m", 3900.0, valid_from=date(2026, 1, 1),
                 recorded_at=WATCHED_FROM),
        ])

    def test_an_attribute_in_force_on_neither_side_is_not_compared(self):
        looked = retrospect(self.not_yet_in_force(), AD,
                            on=THE_DAY, as_known_at=THE_MORNING)
        assert looked.summary()["attributes"] == 2
        assert looked.summary()["compared"] == 1
        assert looked.summary()["not_in_force"] == 1

    def test_it_is_not_counted_as_agreement(self):
        looked = retrospect(self.not_yet_in_force(), AD,
                            on=THE_DAY, as_known_at=THE_MORNING)
        uncompared = [r for r in looked.revisions if not r.is_held]
        assert [r.attribute for r in uncompared] == ["rffs_category"]
        assert uncompared[0] not in looked.compared

    def test_the_render_reports_it_separately(self):
        printed = retrospect(self.not_yet_in_force(), AD,
                             on=THE_DAY, as_known_at=THE_MORNING).render()
        assert "in force that day" in printed or "in force" in printed
        assert "were not compared" in printed

    def test_a_faithful_record_counts_only_what_was_compared(self):
        printed = retrospect(self.not_yet_in_force(), AD,
                             on=THE_DAY, as_known_at=THE_MORNING).render()
        assert "all 1 attributes in force that day" in printed

"""One flight, one aeroplane, one date — the business aviation entry point.

A flight department has no network. The question is not "what changed this
cycle" but "can I take this aeroplane into that airport on Thursday, and what
will bite me?" — and the plan calls it the faster path to a first paying
customer.

Two things separate a trip from a network sweep, and both are tested hardest
here: it is assessed for the **day of the flight** rather than today, and it
reports **what changes between now and then**. A supplement that lapses before
departure has already lapsed by the time the aeroplane arrives, and assessing
today would clear an aerodrome that will not be clear when it matters.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from aeropub.aip import AipCoverage, HoldingState, SectionHolding
from aeropub.aip import section as aip_section
from aeropub.aircraft import AircraftType, Characteristic, Origin
from aeropub.facts import Fact, FactStore, Precedence
from aeropub.operator import Exposure, OperatorProfile, Role
from aeropub.provenance import SourceRef
from aeropub.trip import BIZAV_SECTIONS, Trip, assess_trip

NOW = datetime(2026, 9, 4, 8, 0, tzinfo=timezone.utc)
TODAY = NOW.date()
DEPARTS = date(2026, 9, 25)


def ref(document: str = "AIP AD 2") -> SourceRef:
    return SourceRef(
        source_id="TEST", document=document, locator="AD 2",
        retrieved_at=NOW, content_hash="a" * 64,
        parser_id="test", parser_version="1",
    )


def fact(entity, attribute, value, *, layer=Precedence.AIP,
         valid_from=date(2026, 1, 1), valid_to=None, document="AIP AD 2") -> Fact:
    return Fact(entity=entity, attribute=attribute, value=value,
                valid_from=valid_from, valid_to=valid_to,
                source=ref(document), precedence=layer)


def characteristic(attribute, value, **overrides) -> Characteristic:
    fields = dict(attribute=attribute, value=value, source=ref("ACAP"),
                  origin=Origin.ACAP)
    fields.update(overrides)
    return Characteristic(**fields)


#: A Global-class aeroplane: Code C, needs fire Category 6.
JET = AircraftType(designator="GL7T").with_characteristics([
    characteristic("wingspan_m", 31.7),
    characteristic("omgws_m", 6.4),
    characteristic("reference_field_length_m", 1920.0),
    characteristic("overall_length_m", 33.9),
    characteristic("fuselage_width_m", 2.7),
    characteristic("acn", 38.0, variant="F/B at MTOW"),
])


def aerodrome(name: str, *, rffs: int = 7, code: str = "4C") -> list[Fact]:
    return [
        fact(name, "aerodrome_reference_code", code),
        fact(name, "rffs_category", rffs),
        fact(f"{name}/RWY01", "runway_width_m", 45.0),
        fact(f"{name}/RWY01", "pcn", "60/F/B/X/T"),
    ]


def lapsing(name: str) -> list[Fact]:
    """Category 5 in the AIP, held at 7 by a supplement that lapses on the 20th."""
    return [
        fact(name, "aerodrome_reference_code", "4C"),
        fact(name, "rffs_category", 5),
        fact(name, "rffs_category", 7, layer=Precedence.SUP,
             valid_from=date(2026, 8, 1), valid_to=date(2026, 9, 20),
             document="AIP SUP 11/26"),
        fact(f"{name}/RWY01", "runway_width_m", 45.0),
        fact(f"{name}/RWY01", "pcn", "60/F/B/X/T"),
    ]


def coverage_for(*aerodromes: str, sections=tuple(BIZAV_SECTIONS)) -> AipCoverage:
    return AipCoverage([
        SectionHolding(section=aip_section(code), entity=name,
                       state=HoldingState.HELD, source=ref())
        for name in aerodromes
        for code in sections
    ])


def trip(**overrides) -> Trip:
    fields = dict(
        reference="N901GX/25SEP", aircraft=JET, on=DEPARTS,
        departure="KTEB", destination="KASE", alternates=("KGJT",),
        operator="Example Aviation",
    )
    fields.update(overrides)
    return Trip(**fields)


# --------------------------------------------------------------------------
# The day of the flight, not today
# --------------------------------------------------------------------------


class TestAssessedForTheFlightDate:
    def store(self) -> FactStore:
        return FactStore(
            aerodrome("KTEB") + lapsing("KASE") + aerodrome("KGJT")
        )

    def test_a_supplement_that_lapses_before_departure_has_lapsed(self):
        # The whole point. Today KASE holds Category 7 and the aeroplane needs
        # 6. On the 25th the supplement has gone and the AIP's 5 is back.
        assessed = assess_trip(
            self.store(), trip(), as_at=NOW,
            coverage=coverage_for("KTEB", "KASE", "KGJT"),
        )
        assert assessed.leg("KASE").exposure is Exposure.CRITICAL
        assert assessed.overall is Exposure.CRITICAL

    def test_the_same_trip_flown_today_would_read_clear(self):
        # Proving the date is doing the work, not the fixture.
        today = assess_trip(
            self.store(), trip(on=TODAY), as_at=NOW,
            coverage=coverage_for("KTEB", "KASE", "KGJT"),
        )
        assert today.leg("KASE").exposure is Exposure.NONE

    def test_the_change_before_departure_is_named_with_its_date(self):
        assessed = assess_trip(
            self.store(), trip(), as_at=NOW,
            coverage=coverage_for("KTEB", "KASE", "KGJT"),
        )
        leg = assessed.leg("KASE")
        assert leg.changes_before
        assert leg.changes_before[0].on == date(2026, 9, 21)

    def test_and_flagged_as_one_nobody_will_publish(self):
        # A supplement lapsing is a real change to what is in force, and from
        # the State's side nothing has happened.
        assessed = assess_trip(
            self.store(), trip(), as_at=NOW,
            coverage=coverage_for("KTEB", "KASE", "KGJT"),
        )
        assert assessed.leg("KASE").unannounced_before
        assert "nothing will be published" in assessed.render()

    def test_the_forward_window_stops_at_the_flight(self):
        # What changes after the aeroplane has left is somebody else's trip.
        near = assess_trip(
            self.store(), trip(on=date(2026, 9, 10)), as_at=NOW,
            coverage=coverage_for("KTEB", "KASE", "KGJT"),
        )
        assert near.leg("KASE").changes_before == ()

    def test_the_render_states_both_dates(self):
        printed = assess_trip(
            self.store(), trip(), as_at=NOW,
            coverage=coverage_for("KTEB", "KASE", "KGJT"),
        ).render()
        assert "T+21" in printed
        assert "for the state in force on 2026-09-25" in printed


# --------------------------------------------------------------------------
# Same engine, different entry point
# --------------------------------------------------------------------------


class TestItIsTheSameEngine:
    def test_a_trip_produces_an_operator_profile(self):
        profile = trip().as_profile()
        assert isinstance(profile, OperatorProfile)
        assert [t.designator for t in profile.fleet] == ["GL7T"]

    def test_roles_come_out_as_the_flight_uses_them(self):
        assessed = trip(takeoff_alternate="KEWR",
                        enroute_alternates=("KDEN",))
        assert assessed.role_of("KASE") is Role.DESTINATION
        assert assessed.role_of("KGJT") is Role.ALTERNATE
        assert assessed.role_of("KEWR") is Role.TAKEOFF_ALTERNATE
        assert assessed.role_of("KDEN") is Role.EDTO_ALTERNATE

    def test_the_departure_aerodrome_is_checked_for_fit(self):
        # The aeroplane must physically fit it to leave, so the fit checks
        # apply exactly as at a destination.
        assert trip().role_of("KTEB") is Role.DESTINATION

    def test_an_aerodrome_not_on_the_trip_is_outside_it(self):
        assert trip().role_of("KJFK") is Role.NOT_IN_NETWORK

    def test_a_flight_department_gets_the_same_answer_as_an_airline(self):
        # The aerodrome does not know who is asking, so the same aeroplane at
        # the same aerodrome on the same date must read the same either way.
        from aeropub.dossier import build
        from aeropub.notam_register import NotamRegister
        from aeropub.operator import (
            Fleet, Network, NetworkEntry, assess_operator,
        )

        store = FactStore(lapsing("KASE"))
        held = coverage_for("KASE")
        as_airline = assess_operator(
            build("KASE", facts=store, coverage=held, register=NotamRegister(),
                  as_at=NOW, on=DEPARTS),
            OperatorProfile(name="An Airline", fleet=Fleet((JET,)),
                            network=Network((NetworkEntry("KASE", Role.DESTINATION),))),
        )
        as_trip = assess_trip(
            store, trip(departure="KASE", destination="KASE", alternates=()),
            as_at=NOW, coverage=held,
        ).leg("KASE")
        assert as_trip.exposure is as_airline.overall


class TestSoleAlternateIsDerived:
    def test_one_nominated_alternate_has_nothing_to_swap_to(self):
        # A flight department nominating a single alternate is usually not
        # thinking of it as sole-suitable. It is.
        assert trip(alternates=("KGJT",)).sole_alternate
        entry = next(
            e for e in trip(alternates=("KGJT",)).as_profile().network
            if e.aerodrome == "KGJT"
        )
        assert entry.sole_suitable

    def test_two_alternates_are_not_sole(self):
        assert not trip(alternates=("KGJT", "KDEN")).sole_alternate

    def test_a_failing_sole_alternate_is_critical_not_high(self):
        store = FactStore(
            aerodrome("KTEB") + aerodrome("KASE") + aerodrome("KGJT", rffs=3)
        )
        assessed = assess_trip(
            store, trip(), as_at=NOW,
            coverage=coverage_for("KTEB", "KASE", "KGJT"),
        )
        assert assessed.leg("KGJT").exposure is Exposure.CRITICAL


# --------------------------------------------------------------------------
# What binds a business aviation trip
# --------------------------------------------------------------------------


class TestTheSectionsThatBite:
    def test_a_missing_section_is_named_with_what_it_means(self):
        # "AD 2.3 not held" and "we do not know whether it is open when you
        # arrive" are different sentences, and only one of them stops a crew.
        assessed = assess_trip(
            FactStore(aerodrome("KTEB") + aerodrome("KASE") + aerodrome("KGJT")),
            trip(), as_at=NOW,
            coverage=coverage_for("KTEB", "KASE", "KGJT",
                                  sections=("AD 2.6", "AD 2.12", "AD 2.13")),
        )
        missing = dict(assessed.leg("KASE").missing_sections)
        assert "AD 2.3" in missing
        assert "open when you arrive" in missing["AD 2.3"]

    def test_a_missing_section_makes_the_trip_inconclusive(self):
        # The aeroplane fitting an aerodrome says nothing about whether it is
        # open when you arrive.
        assessed = assess_trip(
            FactStore(aerodrome("KTEB") + aerodrome("KASE") + aerodrome("KGJT")),
            trip(), as_at=NOW,
            coverage=coverage_for("KTEB", "KASE", "KGJT",
                                  sections=("AD 2.6", "AD 2.12", "AD 2.13")),
        )
        assert assessed.overall is Exposure.NONE
        assert not assessed.is_conclusive
        assert "NOT CONCLUSIVE" in assessed.render()

    def test_a_fully_held_trip_is_conclusive(self):
        assessed = assess_trip(
            FactStore(aerodrome("KTEB") + aerodrome("KASE") + aerodrome("KGJT")),
            trip(), as_at=NOW,
            coverage=coverage_for("KTEB", "KASE", "KGJT"),
        )
        assert assessed.is_conclusive
        assert "NOT CONCLUSIVE" not in assessed.render()

    def test_the_bound_sections_are_the_ones_the_plan_names(self):
        # Runway length, RFFS, PPR lead time, customs hours, noise curfews.
        assert set(BIZAV_SECTIONS) >= {"AD 2.3", "AD 2.6", "AD 2.12", "AD 2.20"}


# --------------------------------------------------------------------------
# A trip is a question about a date
# --------------------------------------------------------------------------


class TestExpiry:
    def test_a_past_trip_is_expired(self):
        assert trip(on=date(2026, 8, 1)).is_expired(TODAY)

    def test_today_is_not_expired(self):
        assert not trip(on=TODAY).is_expired(TODAY)

    def test_an_expired_assessment_says_it_is_history(self):
        # A stale assessment sitting in a list looking current is the failure
        # this exists to prevent.
        assessed = assess_trip(
            FactStore(aerodrome("KASE")),
            trip(on=date(2026, 8, 1), departure="KASE", destination="KASE",
                 alternates=()),
            as_at=NOW, coverage=coverage_for("KASE"),
        )
        assert assessed.expired
        assert "history, not a live answer" in assessed.render()


class TestInputs:
    def test_a_trip_needs_a_reference(self):
        with pytest.raises(ValueError) as caught:
            trip(reference="  ")
        assert "the operator's own" in str(caught.value)

    def test_a_trip_needs_a_departure_and_a_destination(self):
        with pytest.raises(ValueError):
            trip(departure="")
        with pytest.raises(ValueError):
            trip(destination="  ")

    def test_aerodrome_keys_are_normalised(self):
        assert trip(departure=" kteb ").departure == "KTEB"
        assert trip(alternates=(" kgjt ",)).alternates == ("KGJT",)

    def test_the_aerodrome_list_is_ordered_and_deduplicated(self):
        assert trip(alternates=("KGJT", "KTEB")).aerodromes == (
            "KTEB", "KASE", "KGJT",
        )

    def test_a_naive_moment_is_refused(self):
        with pytest.raises(ValueError):
            assess_trip(FactStore(), trip(), as_at=datetime(2026, 9, 4, 8, 0))

    def test_an_empty_trip_is_not_a_clear_one(self):
        from aeropub.trip import TripAssessment

        empty = TripAssessment(trip=trip(), as_at=NOW)
        assert empty.overall is Exposure.UNKNOWN
        assert not empty.is_conclusive

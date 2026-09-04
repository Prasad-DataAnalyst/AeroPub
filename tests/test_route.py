"""Route dossiers — the headline, and what it refuses to leave out.

A route dossier is an assembly, so most of it is tested where the parts are
tested. What is tested here is the thing assembly gets wrong: a document made
of six sections prints beautifully when five of them are empty, and reads
exactly like a route with nothing wrong.

So the assertions concentrate on the headline — how much of this route the
platform can speak for — and on the three ways that number can be quietly
inflated:

- a jurisdiction nobody has read producing no findings, and no findings
  reading as fine;
- an altimetry boundary with one side missing reading as a boundary where
  nothing changes;
- an element the platform cannot address at all simply not appearing.

The aerodromes and regions below are fixtures. Nothing here is a claim about a
real airspace.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from aeropub.aip import AipCoverage
from aeropub.aircraft import AircraftType, Characteristic, Origin
from aeropub.entities import named
from aeropub.facts import Fact, FactStore, Precedence
from aeropub.notam_register import NotamRegister
from aeropub.operator import Exposure, Fleet, Role
from aeropub.provenance import SourceRef
from aeropub.route import (
    FIR,
    NOT_YET_ADDRESSED,
    Altimetry,
    AltimetryChange,
    Jurisdiction,
    JurisdictionCover,
    OpenItem,
    Route,
    build_route_dossier,
)

NOW = datetime(2026, 10, 5, 6, 0, tzinfo=timezone.utc)
ON = date(2026, 10, 5)


def ref(**overrides) -> SourceRef:
    fields = dict(
        source_id="TEST",
        document="test fixture — not a real publication",
        locator="ENR 1.7",
        retrieved_at=NOW,
        content_hash="e" * 64,
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


def characteristic(attribute: str, value) -> Characteristic:
    return Characteristic(
        attribute=attribute, value=value, source=ref(), origin=Origin.ACAP
    )


NARR = Fleet(
    (
        AircraftType(designator="NARR").with_characteristics(
            (
                characteristic("wingspan_m", 34.0),
                characteristic("omgws_m", 8.0),
                characteristic("overall_length_m", 38.0),
                characteristic("fuselage_width_m", 4.0),
            )
        ),
    )
)

AAAA = Jurisdiction(designator="AAAA", name="Alpha FIR")
BBBB = Jurisdiction(designator="BBBB", name="Bravo FIR", state="BX")
CCCC = Jurisdiction(designator="CCCC", name="Charlie FIR")


def route(**overrides) -> Route:
    fields = dict(
        departure="XXXX",
        destination="YYYY",
        alternates=("ZZZZ",),
        crosses=(AAAA, BBBB, CCCC),
        designator="NARR",
    )
    fields.update(overrides)
    return Route(**fields)


def store(*facts) -> FactStore:
    return FactStore(facts)


def dossier(sector: Route, *facts, **overrides):
    fields = dict(
        fleet=NARR,
        as_at=NOW,
        on=ON,
        register=NotamRegister(),
        coverage=AipCoverage(),
    )
    fields.update(overrides)
    return build_route_dossier(store(*facts), sector, **fields)


#: A minimal aerodrome: enough held that it is read, not enough to be clear.
def aerodrome(where: str, *, code: str = "4C", rffs: int = 7):
    return (
        fact(where, "aerodrome_reference_code", code),
        fact(where, "rffs_category", rffs),
    )


# --------------------------------------------------------------------------
# The route itself
# --------------------------------------------------------------------------


class TestRoute:
    def test_every_aerodrome_appears_once_in_role_order(self):
        sector = Route(
            departure="XXXX",
            destination="YYYY",
            alternates=("ZZZZ", "YYYY"),
            takeoff_alternate="WWWW",
            enroute_alternates=("VVVV",),
        )
        assert sector.aerodromes == ("XXXX", "YYYY", "ZZZZ", "WWWW", "VVVV")

    def test_the_departure_takes_the_destination_role_not_the_enroute_one(self):
        """Pavement and fire category matter at the field you are sitting on."""
        profile = route().as_profile(NARR)
        assert profile.network.role_of("XXXX") is Role.DESTINATION

    def test_a_single_alternate_is_sole_suitable(self):
        profile = route(alternates=("ZZZZ",)).as_profile(NARR)
        assert profile.network.is_sole_suitable("ZZZZ")

    def test_several_alternates_leave_the_judgement_with_the_operator(self):
        profile = route(alternates=("ZZZZ", "WWWW")).as_profile(NARR)
        assert not profile.network.is_sole_suitable("ZZZZ")

    def test_the_document_names_each_end_the_way_a_reader_would(self):
        """The role and the position are different things, and both belong.

        The departure is assessed at the destination role because that role
        carries the checks that matter at the field the aeroplane is sitting
        on. Printing it as "destination" leaves a reader working out which end
        is which.
        """
        sector = route()
        assert sector.position_of("XXXX") == "departure"
        assert sector.position_of("YYYY") == "destination"
        assert sector.position_of("ZZZZ") == "alternate"
        text = dossier(sector).render()
        assert "XXXX   departure" in text

    def test_the_two_ends_are_not_a_redundancy_group(self):
        """A group of one reads to the sweep as a region down to its last option.

        Naming a group for the departure produced a CRITICAL "0 of 1
        dependable" finding about the aerodrome the flight starts from, which
        is not a redundancy problem at all.
        """
        profile = route().as_profile(NARR)
        groups = {e.group for e in profile.network if e.group}
        assert "departure" not in groups
        assert "destination" not in groups

    def test_no_redundancy_finding_is_raised_about_the_departure(self):
        built = dossier(route())
        assert not [
            item
            for item in built.open_items
            if item.what == "redundancy" and item.where in ("departure", "destination")
        ]

    def test_the_same_region_twice_in_a_row_is_refused(self):
        """It would produce a boundary between a region and itself."""
        with pytest.raises(ValueError, match="twice in a row"):
            route(crosses=(AAAA, AAAA, BBBB))

    def test_a_region_re_entered_later_is_allowed(self):
        """Routes do leave and re-enter a region, and that is two boundaries."""
        sector = route(crosses=(AAAA, BBBB, AAAA))
        assert len(sector.crosses) == 3

    def test_a_jurisdiction_is_keyed_free_standing(self):
        """An FIR belongs to no aerodrome, and must never roll up under one."""
        assert AAAA.key == named(FIR, "AAAA")
        assert AAAA.key.startswith(FIR)

    def test_the_publisher_is_the_state_where_one_is_named(self):
        """An FIR and the State that publishes for it are not always one to one."""
        assert BBBB.publisher == "BX"
        assert AAAA.publisher == "AAAA"


# --------------------------------------------------------------------------
# The headline
# --------------------------------------------------------------------------


class TestCoverage:
    def test_an_unread_route_speaks_for_nothing(self):
        built = dossier(route())
        assert built.coverage == (0, 6)
        assert not built.is_conclusive

    def test_reading_the_aerodromes_alone_does_not_make_it_conclusive(self):
        """The failure this module exists to prevent.

        Both ends read, the whole middle unknown, and a document that would
        otherwise print its conclusions with full confidence.
        """
        built = dossier(
            route(),
            *aerodrome("XXXX"),
            *aerodrome("YYYY"),
            *aerodrome("ZZZZ"),
        )
        assert built.coverage == (3, 6)
        assert not built.is_conclusive
        assert "have never been read" in built.render()

    def test_a_fully_read_route_is_conclusive(self):
        built = dossier(
            route(crosses=(AAAA, BBBB)),
            *aerodrome("XXXX"),
            *aerodrome("YYYY"),
            *aerodrome("ZZZZ"),
            fact(AAAA.key, "transition_altitude_ft", 4000),
            fact(BBBB.key, "transition_altitude_ft", 5000),
        )
        assert built.coverage == (5, 5)
        assert built.is_conclusive

    def test_a_region_with_no_facts_is_a_row_not_a_missing_row(self):
        built = dossier(route())
        assert [c.jurisdiction.designator for c in built.jurisdictions] == [
            "AAAA", "BBBB", "CCCC"
        ]
        assert all(not c.is_covered for c in built.jurisdictions)
        assert "never read" in built.render()

    def test_a_route_crossing_nothing_says_the_middle_was_not_checked(self):
        """No regions listed is a gap in the route, not a quiet route."""
        built = dossier(route(crosses=()))
        assert "gap in the route" in built.render()

    def test_a_value_not_in_force_on_the_day_is_not_counted_as_read(self):
        """Held is not the same as effective.

        A transition altitude whose validity ended last cycle is not a value
        we may quote, and counting it would inflate the headline with a number
        nobody may use.
        """
        expired = Fact(
            entity=AAAA.key,
            attribute="transition_altitude_ft",
            value=4000,
            valid_from=date(2026, 1, 1),
            valid_to=date(2026, 6, 1),
            source=ref(),
            precedence=Precedence.AIP,
        )
        built = dossier(route(crosses=(AAAA,)), expired)
        assert not built.jurisdictions[0].is_covered


# --------------------------------------------------------------------------
# Altimetry
# --------------------------------------------------------------------------


class TestAltimetry:
    def test_the_boundary_is_the_finding_not_the_table(self):
        built = dossier(
            route(crosses=(AAAA, BBBB, CCCC)),
            fact(AAAA.key, "transition_altitude_ft", 4000),
            fact(BBBB.key, "transition_altitude_ft", 5000),
            fact(CCCC.key, "transition_altitude_ft", 5000),
        )
        moved = built.altimetry.changes
        assert len(moved) == 1
        assert moved[0].leaving.designator == "AAAA"
        assert moved[0].entering.designator == "BBBB"
        assert moved[0].delta_ft == 1000.0

    def test_a_boundary_with_one_side_missing_is_not_a_boundary_with_no_change(self):
        built = dossier(
            route(crosses=(AAAA, BBBB)),
            fact(AAAA.key, "transition_altitude_ft", 4000),
        )
        assert built.altimetry.changes == ()
        assert len(built.altimetry.unknown) == 1
        assert not built.altimetry.is_complete
        assert "not held for BBBB" in built.altimetry.unknown[0].describe()

    def test_an_unparseable_transition_altitude_is_left_unread(self):
        """Rounding one into place is how a crew gets a number nobody published."""
        built = dossier(
            route(crosses=(AAAA,)),
            fact(AAAA.key, "transition_altitude_ft", "see AIP"),
        )
        assert built.jurisdictions[0].transition_altitude_ft is None
        assert built.jurisdictions[0].is_covered

    def test_a_missing_transition_altitude_is_an_open_item(self):
        built = dossier(
            route(crosses=(AAAA,)),
            fact(AAAA.key, "transition_level", "FL070"),
        )
        assert any(
            item.what == "transition altitude not held" for item in built.open_items
        )

    def test_one_region_produces_no_boundaries(self):
        assert Altimetry(
            covers=(JurisdictionCover(jurisdiction=AAAA, facts_held=1),)
        ).boundaries == ()

    def test_an_unknown_boundary_reports_which_side_is_missing(self):
        boundary = AltimetryChange(
            leaving=AAAA, entering=BBBB, from_ft=None, to_ft=5000.0
        )
        assert not boundary.is_known
        assert boundary.delta_ft is None
        assert "AAAA" in boundary.describe()


# --------------------------------------------------------------------------
# Open items
# --------------------------------------------------------------------------


class TestOpenItems:
    def test_an_unread_aerodrome_is_an_open_item(self):
        built = dossier(route())
        unread = [i for i in built.open_items if i.what == "never read"]
        assert {i.where for i in unread} >= {"XXXX", "YYYY", "ZZZZ"}

    def test_an_unread_region_is_an_open_item_naming_its_publisher(self):
        built = dossier(route(crosses=(BBBB,)))
        item = next(i for i in built.open_items if i.where == "BBBB")
        assert "BX" in item.why

    def test_items_are_ordered_worst_first(self):
        built = dossier(
            route(),
            *aerodrome("XXXX"),
            *aerodrome("YYYY", rffs=1),
            *aerodrome("ZZZZ"),
        )
        rank = {
            Exposure.CRITICAL: 0, Exposure.HIGH: 1, Exposure.MEDIUM: 2,
            Exposure.UNKNOWN: 3, Exposure.LOW: 4, Exposure.NONE: 5,
        }
        seen = [rank[i.severity] for i in built.open_items]
        assert seen == sorted(seen)

    def test_a_jurisdiction_finding_is_not_outranked_into_invisibility(self):
        """The overall must see the regions, not only the aerodromes.

        Built directly so the finding lives only in the open items. Taking the
        overall from the sweep alone would report a route at UNKNOWN while a
        region it crosses was closed.
        """
        from aeropub.route import Altimetry, RouteDossier
        from aeropub.sweep import NetworkSweep

        quiet = NetworkSweep(operator="test", as_at=NOW, on=ON)
        built = RouteDossier(
            route=route(),
            as_at=NOW,
            on=ON,
            sweep=quiet,
            jurisdictions=(JurisdictionCover(jurisdiction=AAAA),),
            altimetry=Altimetry(),
            open_items=(
                OpenItem(
                    where="AAAA",
                    what="airspace closed",
                    severity=Exposure.CRITICAL,
                    why="the sector crosses it",
                ),
            ),
        )
        assert quiet.overall is not Exposure.CRITICAL
        assert built.overall is Exposure.CRITICAL

    def test_an_open_item_describes_itself_with_its_severity(self):
        item = OpenItem(
            where="XXXX", what="never read", severity=Exposure.UNKNOWN, why="no facts"
        )
        assert "UNKNOWN" in item.describe()
        assert "XXXX" in item.describe()


# --------------------------------------------------------------------------
# What it did not look at
# --------------------------------------------------------------------------


class TestNotAddressed:
    def test_the_dossier_names_what_it_could_not_address(self):
        """Absent rather than approximated, and said out loud."""
        text = dossier(route()).render()
        assert "NOT ADDRESSED" in text
        assert "driftdown" in text

    def test_every_unaddressed_element_appears_in_the_document(self):
        text = dossier(route()).render()
        for element in NOT_YET_ADDRESSED:
            assert element.split(" —")[0] in text

    def test_the_list_can_shrink_as_the_platform_grows(self):
        built = dossier(route(), not_addressed=())
        assert "NOT ADDRESSED" not in built.render()


# --------------------------------------------------------------------------
# Agreement with the rest of the platform
# --------------------------------------------------------------------------


class TestAgreement:
    def test_the_aerodrome_verdicts_come_from_the_sweep_unchanged(self):
        """A number here that disagreed with the sweep would be a defect."""
        facts = (*aerodrome("XXXX"), *aerodrome("YYYY"), *aerodrome("ZZZZ"))
        built = dossier(route(), *facts)
        from aeropub.sweep import sweep as run_sweep

        direct = run_sweep(
            store(*facts),
            route().as_profile(NARR),
            as_at=NOW,
            on=ON,
            register=NotamRegister(),
            coverage=AipCoverage(),
        )
        assert [e.aerodrome for e in built.sweep.ranked] == [
            e.aerodrome for e in direct.ranked
        ]
        assert built.sweep.overall is direct.overall

    def test_as_at_must_be_timezone_aware(self):
        with pytest.raises(ValueError, match="timezone-aware"):
            dossier(route(), as_at=datetime(2026, 10, 5, 6, 0))

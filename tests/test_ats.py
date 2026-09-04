"""ATS routes — the grammar, the structure, and the honesty of the join.

Three things are tested hard here.

**The grammar**, because it is the one interface every operator already has.
A planner pastes the route they are about to file; if the parser silently drops
an element it does not recognise, the screen below runs on a shorter route than
the one being flown and reports fewer findings than exist. So an unreadable
element stays visible and makes the whole route unparsed.

**The walk**, because a leg filed as two points may cross a dozen published
segments and the *highest* minimum en-route altitude among them is the one that
binds. Taking the first, or averaging, clears a level that is legal on most of
the leg and not on all of it.

**The coverage count**, because a route string parses perfectly whether or not
a single fact is held about the airspace it crosses. The distinction that
matters is between a leg we could not resolve — a gap — and a leg flown direct,
which has nothing to resolve and is a decision the operator made.

Every designator below is a fixture. None of it is a claim about real airspace.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from aeropub.ats import (
    ATS_ROUTE,
    DIRECT,
    FIX,
    NAVAID,
    AtsStructure,
    CruisingLevels,
    ElementKind,
    PointKind,
    Resolution,
    RouteSegment,
    SignificantPoint,
    expand,
    load_ats_structure,
    notams_on_route,
    parse_route_string,
    route_entities,
    screen_levels,
    structure_template,
)
from aeropub.entities import named
from aeropub.manifest import ManifestError
from aeropub.notam_register import (
    ForceState,
    NotamRegister,
    RegisteredNotam,
    Subject,
    SubjectKind,
)
from aeropub.provenance import SourceRef

NOW = datetime(2026, 10, 5, 6, 0, tzinfo=timezone.utc)
READ_AT = "2026-09-01T12:00:00Z"


def ref(**overrides) -> SourceRef:
    fields = dict(
        source_id="TEST",
        document="test fixture — not a real publication",
        locator="ENR 3.1",
        retrieved_at=NOW,
        content_hash="e" * 64,
        parser_id="test",
        parser_version="0.1.0",
    )
    fields.update(overrides)
    return SourceRef(**fields)


def segment(route: str, start: str, end: str, **overrides) -> RouteSegment:
    fields = dict(route=route, start=start, end=end, source=ref())
    fields.update(overrides)
    return RouteSegment(**fields)


#: One airway, three published segments, climbing minimum en-route altitude.
UM688 = (
    segment("UM688", "ALSEM", "MIDLE", mea_ft=15000, distance_nm=120,
            direction=CruisingLevels.ODD, navigation_spec="RNAV 5"),
    segment("UM688", "MIDLE", "KUKLA", mea_ft=24000, distance_nm=80,
            direction=CruisingLevels.ODD, navigation_spec="RNAV 5"),
    segment("UM688", "KUKLA", "BAYAN", mea_ft=18000, distance_nm=60,
            direction=CruisingLevels.ODD, navigation_spec="RNAV 5"),
)


def structure(**overrides) -> AtsStructure:
    fields = dict(segments=UM688)
    fields.update(overrides)
    return AtsStructure(**fields)


# --------------------------------------------------------------------------
# The grammar
# --------------------------------------------------------------------------


class TestGrammar:
    def test_the_four_forms_of_significant_point(self):
        for text, kind in (
            ("ALSEM", PointKind.NAME_CODE),
            ("DOH", PointKind.NAVAID),
            ("DOH180040", PointKind.BEARING_DISTANCE),
            ("4620N07805W", PointKind.LATLON),
            ("46N078W", PointKind.LATLON),
        ):
            element = parse_route_string(text).elements[0]
            assert element.kind is ElementKind.POINT, text
            assert element.point_kind is kind, text

    def test_a_bearing_and_distance_is_not_a_nine_letter_word(self):
        """DOH180040 reads as one token and is a navaid, a radial and a range."""
        element = parse_route_string("DOH180040").elements[0]
        assert element.point_kind is PointKind.BEARING_DISTANCE

    def test_speed_and_level_groups_in_both_conventions(self):
        for text in ("N0450F350", "M082F390", "K0850S1130", "N0300A045"):
            assert parse_route_string(text).elements[0].kind is ElementKind.SPEED_LEVEL

    def test_a_level_change_attaches_to_the_point_it_happens_at(self):
        """It is a property of arriving there, not a separate element."""
        element = parse_route_string("BAYAN/N0460F370").elements[0]
        assert element.kind is ElementKind.POINT
        assert element.speed_level == "N0460F370"
        assert element.describe() == "BAYAN/N0460F370"

    def test_route_designators_and_procedure_designators_both_read(self):
        for text in ("UM688", "L604", "A1", "W12", "BAYA1A"):
            assert parse_route_string(text).elements[0].kind is ElementKind.DESIGNATOR

    def test_direct_stay_rules_and_truncation(self):
        assert parse_route_string("DCT").elements[0].kind is ElementKind.DIRECT
        assert parse_route_string("STAY1/0030").elements[0].kind is ElementKind.STAY
        assert parse_route_string("VFR").elements[0].kind is ElementKind.RULES
        assert parse_route_string("T").elements[0].kind is ElementKind.TRUNCATED

    def test_an_unreadable_element_stays_visible(self):
        """Dropping it silently would screen a shorter route than the one flown."""
        route = parse_route_string("ALSEM ?!?! BAYAN")
        assert route.unparsed == ("?!?!",)
        assert not route.is_parsed

    def test_a_readable_route_says_so(self):
        assert parse_route_string("ALSEM UM688 BAYAN").is_parsed

    def test_aerodromes_are_given_not_taken_from_the_string(self):
        """Item 15 does not carry them; Items 13 and 16 do.

        A string that happens to start with four letters may be naming a
        point, and reading it as the departure would silently move the route.
        """
        route = parse_route_string("ALSEM UM688 BAYAN", departure="OTHH", destination="EGLL")
        assert route.departure == "OTHH"
        assert route.points[0] == "ALSEM"


class TestLegs:
    def test_a_route_becomes_a_walk_from_point_to_point(self):
        route = parse_route_string(
            "ALSEM UM688 BAYAN DCT KIA", departure="OTHH", destination="EGLL"
        )
        assert [leg.describe() for leg in route.legs] == [
            "OTHH DCT ALSEM",
            "ALSEM UM688 BAYAN",
            "BAYAN DCT KIA",
            "KIA DCT EGLL",
        ]

    def test_two_points_with_nothing_between_them_are_direct(self):
        """Whether or not the string bothered to say DCT."""
        route = parse_route_string("ALSEM BAYAN")
        assert route.legs[0].via == DIRECT
        assert route.legs[0].is_direct

    def test_a_speed_level_group_does_not_break_the_walk(self):
        route = parse_route_string("N0450F350 ALSEM UM688 BAYAN")
        assert [leg.describe() for leg in route.legs] == ["ALSEM UM688 BAYAN"]

    def test_the_ends_are_included_where_the_aerodromes_are_known(self):
        """The first and last legs are where a procedure attaches."""
        route = parse_route_string("ALSEM", departure="OTHH", destination="EGLL")
        assert len(route.legs) == 2


# --------------------------------------------------------------------------
# The published structure
# --------------------------------------------------------------------------


class TestStructure:
    def test_a_leg_walks_every_published_segment_between_its_ends(self):
        """A leg filed as two points may cross a dozen segments."""
        walked = structure().between("UM688", "ALSEM", "BAYAN")
        assert [s.end for s in walked] == ["MIDLE", "KUKLA", "BAYAN"]

    def test_an_airway_is_walked_in_both_directions(self):
        """Airways are published one way and flown both."""
        walked = structure().between("UM688", "BAYAN", "ALSEM")
        assert len(walked) == 3

    def test_a_pair_the_structure_does_not_join_returns_nothing(self):
        assert structure().between("UM688", "ALSEM", "NOWAY") == ()

    def test_an_unheld_airway_returns_nothing(self):
        assert structure().between("ZZ99", "ALSEM", "BAYAN") == ()

    def test_a_loop_in_the_published_data_is_refused_not_walked_forever(self):
        looped = AtsStructure(
            segments=(
                segment("Q1", "AAAAA", "BBBBB"),
                segment("Q1", "BBBBB", "AAAAA"),
            )
        )
        assert looped.between("Q1", "AAAAA", "CCCCC") == ()

    def test_a_segment_needs_both_ends(self):
        with pytest.raises(ValueError, match="both be named"):
            segment("UM688", "ALSEM", "")

    def test_a_segment_to_itself_is_refused(self):
        with pytest.raises(ValueError, match="not a segment"):
            segment("UM688", "ALSEM", "ALSEM")

    def test_a_segment_cannot_be_built_without_a_citation(self):
        with pytest.raises(TypeError):
            RouteSegment(route="UM688", start="A", end="B", source=None)

    def test_the_floor_prefers_the_mea_over_the_lower_limit(self):
        held = segment("UM688", "A", "B", mea_ft=15000, lower_limit_ft=10000)
        assert held.floor_ft == 15000

    def test_the_moca_is_never_used_as_the_floor(self):
        """It guarantees obstacle clearance and not navigation signal.

        A flight at the MOCA is legal only in circumstances this platform
        cannot know about, so quoting it as the minimum would clear a level
        nobody may plan.
        """
        held = segment("UM688", "A", "B", moca_ft=9000)
        assert held.floor_ft is None


class TestCruisingLevels:
    def test_odd_and_even_read_flight_levels_the_way_they_are_flown(self):
        assert CruisingLevels.ODD.permits(35000)
        assert not CruisingLevels.ODD.permits(36000)
        assert CruisingLevels.EVEN.permits(36000)

    def test_both_permits_anything_and_none_permits_nothing(self):
        assert CruisingLevels.BOTH.permits(35000)
        assert not CruisingLevels.NONE.permits(35000)

    def test_a_level_that_is_not_a_whole_hundred_is_refused_not_rounded(self):
        """Rounding here would clear a level nobody may fly."""
        assert not CruisingLevels.ODD.permits(35050)


# --------------------------------------------------------------------------
# The expansion
# --------------------------------------------------------------------------


class TestExpansion:
    def test_a_resolved_leg_carries_every_segment_it_crosses(self):
        found = expand(parse_route_string("ALSEM UM688 BAYAN"), structure())
        assert found.legs[0].resolution is Resolution.RESOLVED
        assert len(found.legs[0].segments) == 3

    def test_the_binding_minimum_is_the_highest_not_the_first(self):
        """One segment at FL240 makes the whole leg FL240.

        Taking the first, or averaging, clears a level that is legal on most
        of the leg and not on all of it.
        """
        found = expand(parse_route_string("ALSEM UM688 BAYAN"), structure())
        assert found.legs[0].highest_mea_ft == 24000

    def test_distances_add_across_the_segments_of_a_leg(self):
        found = expand(parse_route_string("ALSEM UM688 BAYAN"), structure())
        assert found.legs[0].distance_nm == 260

    def test_a_direct_leg_is_not_a_gap(self):
        """It has nothing to resolve. Counting it against coverage would
        report a route filed entirely DCT as nought per cent covered."""
        found = expand(
            parse_route_string("ALSEM DCT BAYAN"), structure()
        )
        assert found.legs[0].resolution is Resolution.DIRECT
        assert found.coverage == (0, 0)

    def test_an_unheld_airway_is_a_gap_and_says_which_kind(self):
        found = expand(parse_route_string("ALSEM ZZ99 BAYAN"), structure())
        assert found.legs[0].resolution is Resolution.UNRESOLVED
        assert "not in the held structure" in found.legs[0].reason

    def test_a_held_airway_that_does_not_join_the_points_says_that_instead(self):
        found = expand(parse_route_string("ALSEM UM688 NOWAY"), structure())
        assert "no published path joins" in found.legs[0].reason

    def test_a_terminal_procedure_is_not_screened_as_an_airway(self):
        """The one genuine ambiguity in Item 15, resolved against evidence."""
        found = expand(
            parse_route_string("ALSEM BAYA1A BAYAN"),
            structure(procedures=("BAYA1A",)),
        )
        assert found.legs[0].resolution is Resolution.PROCEDURE
        assert found.coverage == (0, 0)

    def test_coverage_counts_only_what_could_have_been_screened(self):
        found = expand(
            parse_route_string("ALSEM UM688 BAYAN DCT KIA", departure="OTHH"),
            structure(),
        )
        assert found.coverage == (1, 1)
        assert found.is_complete
        assert len(found.direct) == 2

    def test_an_unparsed_route_is_never_complete(self):
        """An element nobody could read may have been an airway.

        A route missing a leg it never knew about screens clean, and that is
        the most confident possible way of saying nothing.
        """
        found = expand(parse_route_string("ALSEM ?!?! BAYAN"), structure())
        assert not found.is_complete

    def test_a_partial_distance_is_reported_as_no_distance(self):
        """A smaller number than the route is worse than none.

        A planner reading it as the route length plans fuel against a
        distance nobody flew.
        """
        partial = AtsStructure(
            segments=(
                segment("Q1", "AAAAA", "BBBBB", distance_nm=100),
                segment("Q2", "BBBBB", "CCCCC"),
            )
        )
        found = expand(parse_route_string("AAAAA Q1 BBBBB Q2 CCCCC"), partial)
        assert found.legs[0].distance_nm == 100
        assert found.distance_nm is None


# --------------------------------------------------------------------------
# Screening
# --------------------------------------------------------------------------


class TestScreening:
    def screened(self, level, holds=("RNAV 5",)):
        found = expand(parse_route_string("ALSEM UM688 BAYAN"), structure())
        return screen_levels(found, planned_ft=level, holds=holds)

    def test_a_level_below_the_minimum_is_blocking(self):
        findings = [f for f in self.screened(21000) if "below the minimum" in f.reason]
        assert len(findings) == 1
        assert findings[0].blocking
        assert findings[0].segment.end == "KUKLA"

    def test_a_level_above_the_maximum_is_blocking(self):
        capped = AtsStructure(
            segments=(segment("Q1", "AAAAA", "BBBBB", maa_ft=24500),)
        )
        found = expand(parse_route_string("AAAAA Q1 BBBBB"), capped)
        assert any(
            "above the maximum" in f.reason
            for f in screen_levels(found, planned_ft=35000)
        )

    def test_the_wrong_parity_for_the_direction_is_blocking(self):
        findings = [f for f in self.screened(36000) if "cruising levels" in f.reason]
        assert findings and all(f.blocking for f in findings)

    def test_the_right_parity_above_every_minimum_finds_nothing(self):
        assert self.screened(35000) == ()

    def test_a_navigation_specification_not_held_is_raised_but_not_blocking(self):
        """The operator may well hold it. We simply do not know."""
        findings = [f for f in self.screened(35000, holds=()) if "requires" in f.reason]
        assert findings
        assert not any(f.blocking for f in findings)

    def test_unresolved_legs_produce_no_findings_by_construction(self):
        """Which is exactly why coverage travels with the findings."""
        found = expand(parse_route_string("ALSEM ZZ99 BAYAN"), structure())
        assert screen_levels(found, planned_ft=100) == ()
        assert found.coverage == (0, 1)


# --------------------------------------------------------------------------
# NOTAM along the route
# --------------------------------------------------------------------------


def notam(identifier: str, entity: str, kind: SubjectKind) -> RegisteredNotam:
    return RegisteredNotam(
        identifier=identifier,
        subjects=(Subject(kind=kind, entity=entity),),
        effective_start=NOW - timedelta(days=1),
        effective_end=NOW + timedelta(days=1),
        source=ref(locator=identifier),
        text="test fixture",
    )


class TestNotamOnRoute:
    def test_every_object_on_the_route_becomes_a_key(self):
        found = expand(
            parse_route_string("ALSEM UM688 BAYAN", departure="OTHH", destination="EGLL"),
            structure(),
        )
        keys = route_entities(found, structure())
        assert "OTHH" in keys and "EGLL" in keys
        assert named(FIX, "ALSEM") in keys
        assert named(ATS_ROUTE, "UM688") in keys

    def test_a_navaid_point_is_keyed_as_a_navaid(self):
        """States file against a navaid and a name-code differently."""
        held = structure(
            points=(
                SignificantPoint(
                    designator="DOH", source=ref(), kind=PointKind.NAVAID
                ),
            )
        )
        found = expand(parse_route_string("DOH UM688 BAYAN"), held)
        assert named(NAVAID, "DOH") in route_entities(found, held)

    def test_a_notam_on_an_airway_is_found_with_the_entity_it_bites_on(self):
        register = NotamRegister(
            [notam("A0001/26", named(ATS_ROUTE, "UM688"), SubjectKind.ROUTE)]
        )
        found = expand(parse_route_string("ALSEM UM688 BAYAN"), structure())
        hits = notams_on_route(register, found, NOW, structure=structure())
        assert [(e, n.identifier) for e, n, _ in hits] == [
            (named(ATS_ROUTE, "UM688"), "A0001/26")
        ]

    def test_a_notam_reachable_from_two_keys_is_reported_once_per_key_at_most(self):
        register = NotamRegister(
            [notam("A0002/26", named(FIX, "ALSEM"), SubjectKind.AIRSPACE)]
        )
        found = expand(parse_route_string("ALSEM UM688 ALSEM"), structure())
        hits = notams_on_route(register, found, NOW, structure=structure())
        assert len({n.identifier for _, n, _ in hits}) == 1

    def test_an_unresolved_schedule_still_reaches_the_planner(self):
        register = NotamRegister(
            [notam("A0003/26", named(FIX, "BAYAN"), SubjectKind.AIRSPACE)]
        )
        found = expand(parse_route_string("ALSEM UM688 BAYAN"), structure())
        hits = notams_on_route(register, found, NOW, structure=structure())
        assert all(isinstance(state, ForceState) for _, _, state in hits)


# --------------------------------------------------------------------------
# Reading an ENR 3 manifest
# --------------------------------------------------------------------------


@pytest.fixture
def document(tmp_path: Path) -> Path:
    path = tmp_path / "enr3.txt"
    path.write_text("an ENR 3 table, standing in for one somebody read\n",
                    encoding="utf-8")
    return path


def write(tmp_path: Path, name: str, payload: dict) -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def manifest(**overrides) -> dict:
    payload = {
        "source": {
            "source_id": "EXAMPLE",
            "document": "AIP ENR 3.1 — lower ATS routes",
            "document_path": "enr3.txt",
            "retrieved_at": READ_AT,
        },
        "segments": [
            {
                "route": "UM688", "start": "ALSEM", "end": "MIDLE",
                "mea_ft": 15000, "direction": "odd", "navigation_spec": "RNAV 5",
                "distance_nm": 120, "locator": "ENR 3.1 row 4",
            }
        ],
        "points": [
            {"designator": "ALSEM", "kind": "name_code", "locator": "ENR 4.4"}
        ],
        "procedures": ["BAYA1A"],
    }
    payload.update(overrides)
    return payload


class TestLoading:
    def test_a_structure_loads_with_every_segment_cited(self, tmp_path, document):
        held = load_ats_structure(write(tmp_path, "enr3.json", manifest()))
        assert held.routes == ("UM688",)
        found = held.on("UM688")[0]
        assert found.source.locator == "ENR 3.1 row 4"
        assert len(found.source.content_hash) == 64

    def test_the_direction_of_cruising_levels_is_read_as_published(self, tmp_path, document):
        held = load_ats_structure(write(tmp_path, "enr3.json", manifest()))
        assert held.on("UM688")[0].direction is CruisingLevels.ODD

    def test_a_segment_needs_a_locator(self, tmp_path, document):
        payload = manifest()
        del payload["segments"][0]["locator"]
        with pytest.raises(ManifestError, match="locator"):
            load_ats_structure(write(tmp_path, "enr3.json", payload))

    def test_an_unreadable_minimum_altitude_is_refused_not_rounded(self, tmp_path, document):
        payload = manifest()
        payload["segments"][0]["mea_ft"] = "see remarks"
        with pytest.raises(ManifestError, match="not a number"):
            load_ats_structure(write(tmp_path, "enr3.json", payload))

    def test_an_unknown_direction_is_refused(self, tmp_path, document):
        payload = manifest()
        payload["segments"][0]["direction"] = "westbound"
        with pytest.raises(ManifestError, match="direction must be"):
            load_ats_structure(write(tmp_path, "enr3.json", payload))

    def test_procedures_separate_a_departure_from_an_airway(self, tmp_path, document):
        held = load_ats_structure(write(tmp_path, "enr3.json", manifest()))
        assert held.is_procedure("BAYA1A")
        assert not held.is_procedure("UM688")

    def test_the_template_round_trips_as_json(self):
        blank = json.loads(structure_template())
        assert blank["segments"][0]["direction"] == "both"
        assert "procedures" in blank

"""ENR 1.12 — the section that is skipped because nothing ever comes of it.

Interception is rare and its consequences are not recoverable. The failure
this module exists to prevent is not a wrong value; it is a silence read as
conformance. A crew flying the Annex 2 signals into a State that publishes its
own is doing the wrong thing confidently, and nothing en route says so.

So most of what is asserted here is that an unread section never reads as a
pass, that a blank listening-watch column never reads as "not required", and
that a State publishing use of force cannot be folded into a general
"departs" and read past.

Nothing here is a claim about a real State's procedures.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from aeropub.interception import (
    EMERGENCY_FREQUENCY_MHZ,
    Conformance,
    Departure,
    Interception,
    InterceptionRegister,
    interception_template,
    load_interception,
    view_interception,
)
from aeropub.manifest import ManifestError
from aeropub.provenance import SourceRef

NOW = datetime(2026, 10, 5, 6, 0, tzinfo=timezone.utc)


def ref(**overrides) -> SourceRef:
    fields = dict(
        source_id="TEST",
        document="test fixture — not a real publication",
        locator="ENR 1.12 para 1",
        retrieved_at=NOW,
        content_hash="b" * 64,
        parser_id="test",
        parser_version="0.1.0",
    )
    fields.update(overrides)
    return SourceRef(**fields)


def procedure(region: str = "AAAA", **overrides) -> Interception:
    fields = dict(
        region=region,
        source=ref(),
        conformance=Conformance.ANNEX_2,
        frequencies=("121.500",),
    )
    fields.update(overrides)
    return Interception(**fields)


def register(*procedures, covers=()) -> InterceptionRegister:
    held = procedures or (procedure(),)
    return InterceptionRegister(
        procedures=held, covers=frozenset(covers) | {p.region for p in held}
    )


# --------------------------------------------------------------------------
# The silence
# --------------------------------------------------------------------------


class TestNotReadIsNotConforming:
    def test_an_unread_section_does_not_say_annex_2_applies(self):
        assert Conformance.UNREAD.is_annex_2 is None

    def test_a_section_read_and_silent_also_does_not(self):
        """Read and publishing nothing is a different absence from unread,
        and neither of them is a yes."""
        assert Conformance.NOT_PUBLISHED.is_annex_2 is None

    def test_only_a_published_statement_is_conformance(self):
        assert Conformance.ANNEX_2.is_annex_2 is True
        assert Conformance.DEPARTS.is_annex_2 is False

    def test_the_two_absences_are_told_apart(self):
        assert Conformance.NOT_PUBLISHED.was_read
        assert not Conformance.UNREAD.was_read

    def test_the_view_says_so_in_the_render(self):
        view = view_interception(InterceptionRegister(), regions=["AAAA"])
        assert "NOT READ IS NOT CONFORMING" in view.render()
        assert "has not published that it follows Annex 2" in view.render()

    def test_an_unread_region_is_not_conclusive(self):
        view = view_interception(InterceptionRegister(), regions=["AAAA"])
        assert view.unread_regions == ("AAAA",)
        assert not view.is_conclusive


# --------------------------------------------------------------------------
# Force
# --------------------------------------------------------------------------


class TestForce:
    def armed(self):
        return procedure(
            conformance=Conformance.DEPARTS,
            departures=(Departure.FORCE,),
            published_text="weapons may be employed against a non-complying aircraft",
        )

    def test_it_is_its_own_category(self):
        """Folded into a general "departs" it would be read past."""
        assert Departure.FORCE.is_grave
        assert not Departure.SIGNALS.is_grave

    def test_a_region_publishing_it_is_reported_separately(self):
        view = view_interception(register(self.armed()), regions=["AAAA"])
        assert [p.region for p in view.armed] == ["AAAA"]

    def test_the_render_says_it_in_words_not_a_code(self):
        assert "FIRED ON" in self.armed().describe()

    def test_it_gets_its_own_section_on_the_page(self):
        view = view_interception(register(self.armed()), regions=["AAAA"])
        assert "PUBLISHED USE OF FORCE" in view.render()

    def test_a_region_not_publishing_it_is_not_listed(self):
        assert view_interception(register(), regions=["AAAA"]).armed == ()


# --------------------------------------------------------------------------
# The boundary
# --------------------------------------------------------------------------


class TestBoundaries:
    def crossing(self, before, after):
        return view_interception(
            register(
                procedure("AAAA", **before),
                procedure("BBBB", **after),
            ),
            regions=["AAAA", "BBBB"],
        )

    def test_conforming_into_departing_is_the_finding(self):
        view = self.crossing(
            {},
            {"conformance": Conformance.DEPARTS, "departures": (Departure.SIGNALS,)},
        )
        assert len(view.crossings_into_a_departure) == 1
        assert "departs from Annex 2 (signals)" in view.render()

    def test_the_direction_matters(self):
        """Departing into conforming is a crew becoming more conservative
        than it needs to be, which is not a briefing item."""
        view = self.crossing(
            {"conformance": Conformance.DEPARTS, "departures": (Departure.SIGNALS,)},
            {},
        )
        assert view.crossings_into_a_departure == ()

    def test_two_conforming_regions_produce_no_finding(self):
        assert self.crossing({}, {}).crossings_into_a_departure == ()

    def test_crossing_into_published_force_is_named(self):
        view = self.crossing(
            {},
            {
                "conformance": Conformance.DEPARTS,
                "departures": (Departure.FORCE,),
            },
        )
        assert "may be fired on" in view.boundaries[0].describe()

    def test_a_boundary_with_one_side_unread_is_kept_apart(self):
        """Not a boundary where nothing changes — one nobody can speak for."""
        view = view_interception(register(procedure("AAAA")), regions=["AAAA", "BBBB"])
        assert len(view.unspeakable_boundaries) == 1
        assert view.crossings_into_a_departure == ()
        assert "nobody can speak for this boundary" in view.render()

    def test_an_unspeakable_boundary_names_which_side(self):
        view = view_interception(register(procedure("AAAA")), regions=["AAAA", "BBBB"])
        assert "BBBB unread" in view.unspeakable_boundaries[0].describe()

    def test_one_region_produces_no_boundary(self):
        assert view_interception(register(), regions=["AAAA"]).boundaries == ()


# --------------------------------------------------------------------------
# Frequencies and the listening watch
# --------------------------------------------------------------------------


class TestFrequencies:
    def test_the_emergency_frequency_present_raises_nothing(self):
        assert not procedure().omits_emergency_frequency

    def test_a_published_list_without_it_is_noticed(self):
        """A crew will call on 121.5 whatever the page says."""
        found = procedure(frequencies=("243.000",))
        assert found.omits_emergency_frequency

    def test_trailing_zeros_do_not_make_it_a_different_frequency(self):
        assert not procedure(frequencies=("121.5",)).omits_emergency_frequency

    def test_publishing_no_frequency_is_not_an_omission(self):
        """Nothing published is a gap in the page, not a statement about
        what the State monitors."""
        assert not procedure(frequencies=()).omits_emergency_frequency

    def test_the_constant_is_the_annex_10_frequency(self):
        assert EMERGENCY_FREQUENCY_MHZ == "121.500"


class TestListeningWatch:
    def test_not_stated_is_none_not_false(self):
        """A State silent about a listening watch has not excused you from
        one."""
        assert procedure().listening_watch is None

    def test_a_required_watch_is_reported(self):
        view = view_interception(
            register(procedure(listening_watch=True, listening_watch_airspace="AAAA UIR")),
            regions=["AAAA"],
        )
        assert [p.region for p in view.listening_watch_required] == ["AAAA"]

    def test_the_requirement_can_be_stated_on_its_own(self):
        """An open item whose reason repeats every other finding about the
        region buries the one thing it is asking for."""
        found = procedure(listening_watch=True, listening_watch_airspace="AAAA UIR")
        alone = found.describe_watch()
        assert "continuous watch on 121.500 in AAAA UIR" in alone
        assert "Annex 2" not in alone

    def test_a_region_not_requiring_one_has_nothing_to_say(self):
        assert procedure().describe_watch() == ""

    def test_the_airspace_travels_with_it(self):
        found = procedure(listening_watch=True, listening_watch_airspace="AAAA UIR")
        assert "in AAAA UIR" in found.describe()

    def test_a_region_that_is_silent_is_not_listed_as_requiring_one(self):
        assert view_interception(register(), regions=["AAAA"]).listening_watch_required == ()


# --------------------------------------------------------------------------
# What it refuses to record
# --------------------------------------------------------------------------


class TestConsistency:
    def test_a_departure_recorded_against_annex_2_conformance_is_refused(self):
        """Recording both would let the departure be read past."""
        with pytest.raises(ValueError, match="read past"):
            procedure(
                conformance=Conformance.ANNEX_2, departures=(Departure.SIGNALS,)
            )

    def test_a_region_must_be_named(self):
        with pytest.raises(ValueError, match="region must be named"):
            procedure(region="")


# --------------------------------------------------------------------------
# Reading a manifest
# --------------------------------------------------------------------------


def manifest(**overrides) -> dict:
    payload = {
        "source": {
            "source_id": "TEST",
            "document": "AIP AA ENR 1.12",
            "retrieved_at": "2026-09-01T00:00:00Z",
            "content_hash": "a" * 64,
        },
        "region": "AAAA",
        "procedures": [
            {
                "conformance": "annex_2",
                "frequencies": ["121.500"],
                "locator": "ENR 1.12 para 1",
            }
        ],
    }
    payload.update(overrides)
    return payload


def write(tmp_path, name, payload):
    path = tmp_path / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


class TestLoading:
    def test_a_procedure_is_read_with_its_citation(self, tmp_path):
        held = load_interception(write(tmp_path, "enr112.json", manifest()))
        found = held.for_region("AAAA")
        assert found.source.locator == "ENR 1.12 para 1"
        assert found.conformance is Conformance.ANNEX_2

    def test_a_blank_listening_watch_is_read_as_not_stated(self, tmp_path):
        payload = manifest()
        payload["procedures"][0]["listening_watch"] = ""
        held = load_interception(write(tmp_path, "enr112.json", payload))
        assert held.for_region("AAAA").listening_watch is None

    def test_a_nonsense_listening_watch_says_a_blank_is_not_a_no(self, tmp_path):
        payload = manifest()
        payload["procedures"][0]["listening_watch"] = "maybe"
        with pytest.raises(ManifestError, match="not excused you from one"):
            load_interception(write(tmp_path, "enr112.json", payload))

    def test_a_row_with_no_locator_is_refused(self, tmp_path):
        payload = manifest()
        del payload["procedures"][0]["locator"]
        with pytest.raises(ManifestError, match="locator is required"):
            load_interception(write(tmp_path, "enr112.json", payload))

    def test_an_unknown_departure_is_refused(self, tmp_path):
        payload = manifest()
        payload["procedures"][0]["conformance"] = "departs"
        payload["procedures"][0]["departures"] = ["shouting"]
        with pytest.raises(ManifestError, match="departures must be one of"):
            load_interception(write(tmp_path, "enr112.json", payload))

    def test_a_region_read_with_no_rows_is_covered_not_absent(self, tmp_path):
        held = load_interception(write(tmp_path, "enr112.json", manifest(procedures=[])))
        assert held.is_read("AAAA")
        assert held.for_region("AAAA") is None

    def test_a_covered_but_empty_region_reads_as_publishing_nothing(self, tmp_path):
        held = load_interception(write(tmp_path, "enr112.json", manifest(procedures=[])))
        view = view_interception(held, regions=["AAAA"])
        assert view.not_publishing == ("AAAA",)
        assert view.unread_regions == ()

    def test_the_template_round_trips_as_json(self):
        blank = json.loads(interception_template())
        assert blank["procedures"][0]["frequencies"] == ["121.500"]
        assert blank["procedures"][0]["listening_watch"] == ""

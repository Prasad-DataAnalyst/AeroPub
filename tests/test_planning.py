"""ENR 1.10 — the deadline that has already passed.

The paperwork is what stops a non-scheduled flight, and the assertions here
are about the three ways this module could get that wrong.

**A window has two ends.** Late and early are different problems with
different remedies, and both mean the plan cannot be filed right now.

**An indicator must never be invented.** The Item 18 parser splits on a closed
list, because a token found inside a free-text remark would make a required
indicator read as present — a false pass, which is the wrong direction to be
wrong in.

**Read-and-silent is not unread.** A State whose ENR 1.10 publishes no
deadline and a State nobody has read look identical in a table and are
opposite answers to "can we still file".

Every region, address and indicator below is a fixture.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from aeropub.manifest import ManifestError
from aeropub.planning import (
    ITEM18_INDICATORS,
    Acceptance,
    FilingChannel,
    FilingRule,
    PlanKind,
    PlanningRegister,
    Timeliness,
    load_planning,
    parse_item18,
    planning_template,
    screen_delay,
    screen_filing,
    screen_item18,
    view_planning,
)
from aeropub.provenance import SourceRef

NOW = datetime(2026, 10, 5, 6, 0, tzinfo=timezone.utc)
READ_AT = "2026-09-01T12:00:00Z"


def ref(**overrides) -> SourceRef:
    fields = dict(
        source_id="TEST",
        document="test fixture — not a real publication",
        locator="ENR 1.10",
        retrieved_at=NOW,
        content_hash="a" * 64,
        parser_id="test",
        parser_version="0.1.0",
    )
    fields.update(overrides)
    return SourceRef(**fields)


def rule(region: str, **overrides) -> FilingRule:
    fields = dict(region=region, source=ref())
    fields.update(overrides)
    return FilingRule(**fields)


SLOW = rule(
    "AAAA",
    minimum_notice_hours=24.0,
    channel=FilingChannel.ARO,
    address="the AAAA ARO",
    office_hours="0600-1400 daily",
    required_item18=("STS", "RMK"),
    delay_tolerance_minutes=30.0,
    applies_to="non-scheduled",
)
QUICK = rule("BBBB", minimum_notice_hours=1.0, maximum_notice_hours=120.0)
SILENT = rule("CCCC")
NO_RPL = rule(
    "AAAA", plan_kind=PlanKind.REPETITIVE, acceptance=Acceptance.NOT_ACCEPTED
)


def register(*rules: FilingRule, covers: tuple[str, ...] = ()) -> PlanningRegister:
    held = rules or (SLOW, QUICK, SILENT, NO_RPL)
    return PlanningRegister(rules=held, covers=frozenset(covers))


# --------------------------------------------------------------------------
# The window
# --------------------------------------------------------------------------


class TestWindow:
    def test_too_little_notice_is_late(self):
        assert SLOW.timeliness(4.0) is Timeliness.LATE

    def test_enough_notice_is_in_the_window(self):
        assert SLOW.timeliness(30.0) is Timeliness.IN_WINDOW

    def test_exactly_the_minimum_is_in_the_window(self):
        """The published figure is the deadline, not the first minute past
        it."""
        assert SLOW.timeliness(24.0) is Timeliness.IN_WINDOW

    def test_too_much_notice_is_too_early(self):
        """The end of the window nobody remembers."""
        assert QUICK.timeliness(200.0) is Timeliness.TOO_EARLY

    def test_a_state_that_publishes_nothing_answers_nothing(self):
        assert SILENT.timeliness(4.0) is Timeliness.NO_WINDOW_PUBLISHED

    def test_neither_absence_reads_as_filed(self):
        assert Timeliness.NO_WINDOW_PUBLISHED.can_file_now is None
        assert Timeliness.UNREAD.can_file_now is None
        assert Timeliness.IN_WINDOW.can_file_now is True

    def test_too_early_is_not_a_softer_no(self):
        """A plan the State will not accept yet is a plan that does not
        exist, however early it was sent."""
        assert Timeliness.TOO_EARLY.can_file_now is False

    def test_only_late_cannot_be_fixed_by_waiting(self):
        assert Timeliness.LATE.blocks_departure
        assert not Timeliness.TOO_EARLY.blocks_departure

    def test_a_window_that_never_opens_is_refused_at_construction(self):
        """Both ends read from the wrong row, and the arithmetic downstream
        would be silently impossible."""
        with pytest.raises(ValueError, match="never opens"):
            rule("DDDD", minimum_notice_hours=48.0, maximum_notice_hours=2.0)

    def test_a_rule_with_no_region_is_refused(self):
        with pytest.raises(ValueError, match="region"):
            FilingRule(region="", source=ref())


class TestFilingScreen:
    def test_every_rule_produces_a_finding_including_the_met_ones(self):
        """A screen that returned only failures would get shorter as coverage
        got worse."""
        found = screen_filing(register().rules, notice_hours=4.0)
        assert len(found) == 3  # the three individual rules, not the RPL

    def test_the_shortfall_is_named(self):
        found = screen_filing((SLOW,), notice_hours=4.0)[0]
        assert found.short_by_hours == pytest.approx(20.0)
        assert "short by 20" in found.describe()

    def test_a_met_window_has_no_shortfall(self):
        assert screen_filing((SLOW,), notice_hours=30.0)[0].short_by_hours is None

    def test_working_days_are_said_out_loud(self):
        held = rule("EEEE", minimum_notice_hours=48.0, working_days=True)
        text = screen_filing((held,), notice_hours=4.0)[0].describe()
        assert "working days" in text

    def test_the_screen_is_about_one_kind_of_message(self):
        found = screen_filing(
            register().rules, notice_hours=4.0, plan_kind=PlanKind.REPETITIVE
        )
        assert [f.region for f in found] == ["AAAA"]


# --------------------------------------------------------------------------
# Item 18
# --------------------------------------------------------------------------


class TestItem18:
    def test_the_indicators_are_split_out(self):
        found, _ = parse_item18("PBN/A1B1 DOF/260905 REG/A7ABC")
        assert found["PBN"] == "A1B1"
        assert found["DOF"] == "260905"
        assert found["REG"] == "A7ABC"

    def test_a_value_runs_to_the_next_indicator(self):
        found, _ = parse_item18("EET/EGTT0026 EHAA0039 RMK/TCAS EQUIPPED")
        assert found["EET"] == "EGTT0026 EHAA0039"
        assert found["RMK"] == "TCAS EQUIPPED"

    def test_a_token_inside_a_remark_is_not_taken_for_an_indicator(self):
        """The failure this closed list exists to stop: splitting at ON/ would
        make everything after it look like a separate field."""
        found, suspect = parse_item18("RMK/CONTACT OPS ON/OFF FREQ")
        assert found["RMK"] == "CONTACT OPS ON/OFF FREQ"
        assert "ON" in suspect

    def test_something_shaped_like_an_indicator_is_reported_not_swallowed(self):
        """Regional indicators exist and this list does not claim to be every
        one of them."""
        _found, suspect = parse_item18("PBN/A1B1 AWR/R1")
        assert suspect == ("AWR",)

    def test_a_repeated_indicator_keeps_the_first(self):
        """The unit that has to act on the plan reads it left to right."""
        found, _ = parse_item18("DOF/260905 DOF/260906")
        assert found["DOF"] == "260905"

    def test_an_empty_item_18_parses_to_nothing(self):
        assert parse_item18("") == ({}, ())
        assert parse_item18("   ") == ({}, ())

    def test_lower_case_is_still_read(self):
        found, _ = parse_item18("dof/260905")
        assert "DOF" in found

    def test_a_required_indicator_that_is_missing_is_a_finding(self):
        found = screen_item18((SLOW,), item18="PBN/A1B1 RMK/NIL")
        assert [f.indicator for f in found] == ["STS"]

    def test_nothing_is_found_when_everything_required_is_filed(self):
        assert screen_item18((SLOW,), item18="STS/HOSP RMK/NIL") == ()

    def test_a_rule_requiring_nothing_finds_nothing(self):
        assert screen_item18((QUICK,), item18="") == ()

    def test_the_recognised_set_is_the_pans_atm_one(self):
        for indicator in ("STS", "PBN", "DOF", "RALT", "RMK", "EET"):
            assert indicator in ITEM18_INDICATORS
        assert "ON" not in ITEM18_INDICATORS


# --------------------------------------------------------------------------
# EOBT slip
# --------------------------------------------------------------------------


class TestDelay:
    def test_a_slip_past_the_tolerance_needs_a_new_plan(self):
        found = screen_delay((SLOW,), slip_minutes=45.0)
        assert len(found) == 1
        assert "replaced, not delayed" in found[0].describe()

    def test_a_slip_within_it_is_not_a_finding(self):
        assert screen_delay((SLOW,), slip_minutes=15.0) == ()

    def test_exactly_the_tolerance_is_within_it(self):
        assert screen_delay((SLOW,), slip_minutes=30.0) == ()

    def test_a_rule_publishing_no_tolerance_produces_nothing_here(self):
        """Not knowing the tolerance and being past it are different problems,
        and only the second is arithmetic."""
        assert screen_delay((SILENT,), slip_minutes=600.0) == ()


# --------------------------------------------------------------------------
# The view
# --------------------------------------------------------------------------


class TestView:
    def test_an_unread_region_is_named_and_not_screened(self):
        found = view_planning(register(), regions=["ZZZZ"], notice_hours=1.0)
        assert found.unread_regions == ("ZZZZ",)
        assert found.filing[0].timeliness is Timeliness.UNREAD
        assert not found.is_conclusive

    def test_a_read_region_that_publishes_nothing_is_not_unread(self):
        found = view_planning(register(), regions=["CCCC"], notice_hours=1.0)
        assert found.unread_regions == ()
        assert found.filing[0].timeliness is Timeliness.NO_WINDOW_PUBLISHED
        assert "Ask the State" in found.filing[0].describe()

    def test_a_region_declared_read_with_no_rules_counts_as_read(self):
        held = register(QUICK, covers=("FFFF",))
        assert held.is_read("FFFF")
        assert view_planning(held, regions=["FFFF"]).unread_regions == ()

    def test_a_read_region_with_no_rules_still_appears_in_the_screen(self):
        """It would otherwise vanish, and a region absent from the screen
        reads as a region with nothing to worry about."""
        held = register(QUICK, covers=("FFFF",))
        found = view_planning(held, regions=["FFFF"], notice_hours=4.0)
        assert [f.region for f in found.filing] == ["FFFF"]
        assert found.filing[0].timeliness is Timeliness.NO_WINDOW_PUBLISHED

    def test_a_region_with_no_rule_of_this_kind_does_not_vanish_either(self):
        """AAAA publishes an individual rule and nothing about changes. Asked
        about changes, it is still a region somebody read."""
        found = view_planning(
            register(),
            regions=["AAAA"],
            notice_hours=4.0,
            plan_kind=PlanKind.CHANGE,
        )
        assert found.filing[0].timeliness is Timeliness.NO_WINDOW_PUBLISHED

    def test_one_hour_is_not_one_hours(self):
        assert "1 hour before EOBT" in QUICK.describe()

    def test_the_late_and_the_early_are_separated(self):
        """One flight can be late for one State and early for its neighbour,
        and the remedies are opposite."""
        held = register(rule("HHHH", minimum_notice_hours=360.0), QUICK)
        found = view_planning(
            held, regions=["HHHH", "BBBB"], notice_hours=200.0
        )
        assert [f.region for f in found.late] == ["HHHH"]
        assert [f.region for f in found.early] == ["BBBB"]

    def test_no_notice_figure_screens_no_window(self):
        """Reporting every window as met because nobody said how much notice
        there was would be the worst possible default."""
        found = view_planning(register(), regions=["AAAA"])
        assert found.filing == ()
        assert "no notice figure supplied" in found.render()

    def test_a_state_refusing_repetitive_plans_is_surfaced(self):
        found = view_planning(register(), regions=["AAAA"], notice_hours=48.0)
        assert found.repetitive_refused == (NO_RPL,)
        assert "one plan per flight" in found.render()

    def test_the_render_names_what_is_missing_from_item_18(self):
        page = view_planning(
            register(), regions=["AAAA"], notice_hours=48.0, item18="RMK/NIL"
        ).render()
        assert "STS/" in page

    def test_the_render_names_the_tokens_it_did_not_recognise(self):
        page = view_planning(
            register(), regions=["AAAA"], notice_hours=48.0,
            item18="STS/HOSP RMK/NIL AWR/R1",
        ).render()
        assert "AWR" in page
        assert "Reported rather than acted on" in page

    def test_the_unanswered_are_not_counted_as_answers(self):
        found = view_planning(
            register(), regions=["CCCC", "ZZZZ"], notice_hours=4.0
        )
        assert len(found.unanswered) == 2
        assert found.late == ()

    def test_a_slip_reaches_the_view(self):
        found = view_planning(
            register(), regions=["AAAA"], notice_hours=48.0, slip_minutes=45.0
        )
        assert len(found.delays) == 1

    def test_an_empty_region_matches_nothing(self):
        assert register().in_region("") == ()


# --------------------------------------------------------------------------
# Reading a manifest
# --------------------------------------------------------------------------


@pytest.fixture
def document(tmp_path: Path) -> Path:
    path = tmp_path / "enr110.txt"
    path.write_text(
        "an ENR 1.10 paragraph, standing in for one somebody read\n",
        encoding="utf-8",
    )
    return path


def write(tmp_path: Path, payload: dict) -> Path:
    path = tmp_path / "enr110.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def manifest(**overrides) -> dict:
    payload = {
        "source": {
            "source_id": "EXAMPLE",
            "document": "AIP AAAA ENR 1.10",
            "document_path": "enr110.txt",
            "retrieved_at": READ_AT,
        },
        "region": "AAAA",
        "covers": ["BBBB"],
        "rules": [
            {
                "plan_kind": "individual",
                "minimum_notice_hours": 24,
                "channel": "aro",
                "address": "the AAAA ARO",
                "office_hours": "0600-1400 daily",
                "required_item18": ["sts", "rmk/"],
                "delay_tolerance_minutes": 30,
                "applies_to": "non-scheduled",
                "locator": "ENR 1.10 para 1.2",
            }
        ],
    }
    payload.update(overrides)
    return payload


class TestLoading:
    def test_a_register_loads_with_every_rule_cited(self, tmp_path, document):
        held = load_planning(write(tmp_path, manifest()))
        found = held.in_region("AAAA")[0]
        assert found.source.locator == "ENR 1.10 para 1.2"
        assert found.minimum_notice_hours == 24.0

    def test_the_indicators_are_normalised_as_they_are_read(self, tmp_path, document):
        held = load_planning(write(tmp_path, manifest()))
        assert held.in_region("AAAA")[0].required_item18 == ("STS", "RMK")

    def test_the_declared_regions_are_read_even_with_no_rules(self, tmp_path, document):
        held = load_planning(write(tmp_path, manifest()))
        assert held.is_read("BBBB")
        assert held.in_region("BBBB") == ()

    def test_a_row_without_a_locator_is_refused(self, tmp_path, document):
        payload = manifest()
        del payload["rules"][0]["locator"]
        with pytest.raises(ManifestError, match="locator"):
            load_planning(write(tmp_path, payload))

    def test_a_deadline_that_is_not_a_number_is_refused(self, tmp_path, document):
        payload = manifest()
        payload["rules"][0]["minimum_notice_hours"] = "one day"
        with pytest.raises(ManifestError, match="minimum_notice_hours"):
            load_planning(write(tmp_path, payload))

    def test_a_negative_deadline_is_refused(self, tmp_path, document):
        payload = manifest()
        payload["rules"][0]["minimum_notice_hours"] = -4
        with pytest.raises(ManifestError, match="negative"):
            load_planning(write(tmp_path, payload))

    def test_an_impossible_window_is_refused_by_the_loader(self, tmp_path, document):
        payload = manifest()
        payload["rules"][0]["maximum_notice_hours"] = 2
        with pytest.raises(ManifestError, match="never opens"):
            load_planning(write(tmp_path, payload))

    def test_an_unknown_channel_is_refused(self, tmp_path, document):
        payload = manifest()
        payload["rules"][0]["channel"] = "carrier pigeon"
        with pytest.raises(ManifestError, match="channel"):
            load_planning(write(tmp_path, payload))

    def test_a_missing_acceptance_is_not_stated(self, tmp_path, document):
        held = load_planning(write(tmp_path, manifest()))
        assert held.in_region("AAAA")[0].acceptance is Acceptance.NOT_STATED

    def test_covers_must_be_a_list(self, tmp_path, document):
        with pytest.raises(ManifestError, match="covers"):
            load_planning(write(tmp_path, manifest(covers="BBBB")))

    def test_the_template_round_trips_as_json(self):
        blank = json.loads(planning_template())
        assert blank["covers"] == []
        assert blank["rules"][0]["minimum_notice_hours"] is None

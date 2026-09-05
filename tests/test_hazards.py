"""ENR 5 — three verbs, six subsections, and the pointer to NOTAM.

What is tested hardest here is the refusal to flatten. Prohibited, restricted
and danger are not degrees of one thing: one forbids, one conditions, one only
warns. A screen that grouped them by severity would lose the difference between
a legal boundary and a risk assessment, and it is the reader who would pay.

The second is the pointer. An area published as active *by NOTAM* is the AIP
saying the AIP is not enough. A planner who reads only ENR 5 has read half the
answer, so that half has to be visible as a list rather than as an activation
value nobody looked at.

The third is the season. A migration corridor is a finding in April and not in
January, and a seasonal entry read as continuous buries the two months that
matter under ten that do not.

Every designator below is a fixture. None of it is a claim about real airspace.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from aeropub.hazards import (
    HAZARD,
    Activation,
    Clearance,
    ClearanceKind,
    Hazard,
    HazardKind,
    HazardRegister,
    hazard_template,
    load_hazards,
    notams_on_hazards,
    screen_clearances,
    screen_hazards,
)
from aeropub.entities import named
from aeropub.manifest import ManifestError
from aeropub.notam_register import (
    NotamRegister,
    RegisteredNotam,
    Subject,
    SubjectKind,
)
from aeropub.provenance import SourceRef

APRIL = datetime(2026, 4, 12, 12, 0, tzinfo=timezone.utc)
JANUARY = datetime(2026, 1, 12, 12, 0, tzinfo=timezone.utc)
READ_AT = "2026-09-01T12:00:00Z"


def ref(**overrides) -> SourceRef:
    fields = dict(
        source_id="TEST",
        document="test fixture — not a real publication",
        locator="ENR 5.1",
        retrieved_at=APRIL,
        content_hash="e" * 64,
        parser_id="test",
        parser_version="0.1.0",
    )
    fields.update(overrides)
    return SourceRef(**fields)


def hazard(designator: str, kind: HazardKind, **overrides) -> Hazard:
    fields = dict(designator=designator, kind=kind, source=ref(), region="AAAA")
    fields.update(overrides)
    return Hazard(**fields)


PROHIBITED = hazard(
    "AP-1", HazardKind.PROHIBITED, name="Palace", lower_ft=0,
    upper_ft=float("inf"), activation=Activation.CONTINUOUS,
)
RESTRICTED = hazard(
    "AR-7", HazardKind.RESTRICTED, lower_ft=20000, upper_ft=45000,
    activation=Activation.SCHEDULED, hours="0600-1800", authority="Alpha CAA",
)
DANGER = hazard(
    "AD-3", HazardKind.DANGER, lower_ft=0, upper_ft=15000,
    activation=Activation.BY_NOTAM, activity="gunnery",
)
BIRDS = hazard(
    "BIRD-N", HazardKind.BIRD_MIGRATION, lower_ft=0, upper_ft=8000,
    activation=Activation.SEASONAL, months=(4, 5, 9, 10),
    activity="raptor migration",
)
OBSTACLE = hazard(
    "OBST-9", HazardKind.OBSTACLE, elevation_ft=4200, activity="mast",
)


def register(*hazards: Hazard, clearances=()) -> HazardRegister:
    return HazardRegister(
        hazards=hazards or (PROHIBITED, RESTRICTED, DANGER, BIRDS),
        clearances=clearances,
    )


# --------------------------------------------------------------------------
# Three verbs, not a severity scale
# --------------------------------------------------------------------------


class TestKinds:
    def test_the_three_verbs_are_kept_apart(self):
        assert HazardKind.PROHIBITED.forbids_entry
        assert not HazardKind.RESTRICTED.forbids_entry
        assert not HazardKind.DANGER.forbids_entry

    def test_a_danger_area_forbids_nothing(self):
        """It warns. The decision belongs to the commander."""
        assert HazardKind.DANGER.is_advisory
        assert not HazardKind.DANGER.is_conditional

    def test_a_restricted_area_is_opened_by_somebody(self):
        assert HazardKind.RESTRICTED.is_conditional
        assert not HazardKind.RESTRICTED.is_advisory

    def test_an_adiz_is_an_identification_requirement(self):
        """A flight that identifies itself may enter, unlike everything else."""
        assert HazardKind.ADIZ.is_conditional
        assert not HazardKind.ADIZ.forbids_entry

    def test_an_obstacle_bears_on_the_level_not_the_route(self):
        assert HazardKind.OBSTACLE.is_vertical

    def test_every_kind_names_the_subsection_that_publishes_it(self):
        assert HazardKind.PROHIBITED.section == "ENR 5.1"
        assert HazardKind.MILITARY.section == "ENR 5.2"
        assert HazardKind.DANGEROUS_ACTIVITY.section == "ENR 5.3"
        assert HazardKind.OBSTACLE.section == "ENR 5.4"
        assert HazardKind.SPORTING.section == "ENR 5.5"
        assert HazardKind.BIRD_MIGRATION.section == "ENR 5.6"

    def test_all_six_subsections_are_covered(self):
        assert {k.section for k in HazardKind} == {
            "ENR 5.1", "ENR 5.2", "ENR 5.3", "ENR 5.4", "ENR 5.5", "ENR 5.6"
        }


class TestActivation:
    def test_continuous_is_answered_by_the_aip_alone(self):
        assert PROHIBITED.active_at(APRIL) is True

    def test_scheduled_is_answered_once_the_time_is_known(self):
        assert RESTRICTED.active_at(APRIL) is True
        assert RESTRICTED.active_at(APRIL.replace(hour=3)) is False

    def test_a_window_crossing_midnight_is_two_intervals(self):
        overnight = hazard(
            "AN-1", HazardKind.DANGER, activation=Activation.SCHEDULED,
            hours="2200-0400",
        )
        assert overnight.active_at(APRIL.replace(hour=23)) is True
        assert overnight.active_at(APRIL.replace(hour=2)) is True
        assert overnight.active_at(APRIL.replace(hour=12)) is False

    def test_hours_in_prose_fall_through_to_unknown(self):
        """A parser that guessed at SR-SS would answer what nobody published."""
        prose = hazard(
            "AP-9", HazardKind.DANGER, activation=Activation.SCHEDULED,
            hours="SR-SS",
        )
        assert prose.active_at(APRIL) is None

    def test_by_notam_cannot_be_answered_from_the_aip(self):
        """False here would be a clear verdict from an absence of evidence."""
        assert DANGER.active_at(APRIL) is None
        assert DANGER.activation.needs_notam

    def test_a_season_is_answered_by_the_month(self):
        assert BIRDS.active_at(APRIL) is True
        assert BIRDS.active_at(JANUARY) is False

    def test_a_seasonal_entry_without_months_is_refused(self):
        """Without them it is active always or never, and neither was published."""
        with pytest.raises(ValueError, match="seasonal entry needs the months"):
            hazard("BIRD-X", HazardKind.BIRD_MIGRATION, activation=Activation.SEASONAL)

    def test_a_month_outside_the_year_is_refused(self):
        with pytest.raises(ValueError, match="not 1 to 12"):
            hazard(
                "BIRD-X", HazardKind.BIRD_MIGRATION,
                activation=Activation.SEASONAL, months=(4, 13),
            )


# --------------------------------------------------------------------------
# The screen
# --------------------------------------------------------------------------


class TestScreen:
    def test_altitude_rules_out_what_it_can(self):
        screen = screen_hazards(register(), regions=["AAAA"], planned_ft=35000)
        assert {h.designator for h in screen.candidates} == {"AP-1", "AR-7"}
        assert {h.designator for h in screen.eliminated} == {"AD-3", "BIRD-N"}

    def test_the_verbs_group_the_output_rather_than_a_severity(self):
        screen = screen_hazards(register(), regions=["AAAA"], planned_ft=35000)
        assert [h.designator for h in screen.prohibited] == ["AP-1"]
        assert [h.designator for h in screen.conditional] == ["AR-7"]
        assert screen.advisory == ()

    def test_an_entry_with_no_limits_is_never_eliminated(self):
        """Telling somebody an area is out of the way when nobody read how
        high it goes is the one false negative that matters."""
        unbounded = hazard("AU-1", HazardKind.RESTRICTED)
        screen = screen_hazards(
            register(unbounded), regions=["AAAA"], planned_ft=35000
        )
        assert screen.unbounded == (unbounded,)
        assert not screen.is_conclusive

    def test_an_en_route_obstacle_survives_the_altitude_filter(self):
        """It is the reason the minimum level is what it is.

        Dropping it at cruise removes the evidence behind the number the
        level screen is checking against.
        """
        screen = screen_hazards(
            register(OBSTACLE), regions=["AAAA"], planned_ft=35000
        )
        assert screen.obstacles == (OBSTACLE,)

    def test_the_by_notam_list_is_the_pointer_to_the_other_half(self):
        screen = screen_hazards(register(), regions=["AAAA"])
        assert [h.designator for h in screen.needs_notam] == ["AD-3"]
        assert "the AIP is not enough" in screen.render()

    def test_an_unread_region_is_named_not_omitted(self):
        screen = screen_hazards(register(), regions=["ZZZZ"], planned_ft=35000)
        assert screen.unread_regions == ("ZZZZ",)
        assert "nothing screened is the same shape as nothing found" in screen.render()

    def test_the_document_refuses_the_containment_claim(self):
        text = screen_hazards(register(), regions=["AAAA"], planned_ft=35000).render()
        assert "holds no geometry" in text

    def test_a_clear_screen_says_it_eliminated_by_altitude_only(self):
        screen = screen_hazards(register(DANGER), regions=["AAAA"], planned_ft=35000)
        assert screen.candidates == ()
        assert "elimination" in screen.render()
        assert "lateral position was never tested" in screen.render()

    def test_active_now_is_a_separate_question_from_not_ruled_out(self):
        screen = screen_hazards(register(), regions=["AAAA"], planned_ft=35000)
        assert {h.designator for h in screen.active_at(APRIL)} == {"AP-1", "AR-7"}
        assert {h.designator for h in screen.active_at(APRIL.replace(hour=3))} == {
            "AP-1"
        }


# --------------------------------------------------------------------------
# NOTAM on what could not be ruled out
# --------------------------------------------------------------------------


def notam(identifier: str, entity: str) -> RegisteredNotam:
    return RegisteredNotam(
        identifier=identifier,
        subjects=(Subject(kind=SubjectKind.AIRSPACE, entity=entity),),
        effective_start=APRIL - timedelta(days=1),
        effective_end=APRIL + timedelta(days=1),
        source=ref(locator=identifier),
        text="test fixture",
    )


class TestNotamJoin:
    def test_a_notam_on_a_candidate_area_is_found(self):
        notams = NotamRegister([notam("A0001/26", named(HAZARD, "AR-7"))])
        screen = screen_hazards(register(), regions=["AAAA"], planned_ft=35000)
        found = notams_on_hazards(notams, screen, APRIL)
        assert [n.identifier for _, n, _ in found] == ["A0001/26"]

    def test_an_area_that_altitude_ruled_out_brings_no_notam(self):
        notams = NotamRegister([notam("A0002/26", named(HAZARD, "AD-3"))])
        screen = screen_hazards(register(), regions=["AAAA"], planned_ft=35000)
        assert notams_on_hazards(notams, screen, APRIL) == ()

    def test_an_area_with_unread_limits_still_brings_its_notam(self):
        """An area we could not eliminate is one whose NOTAM still matters."""
        unbounded = hazard("AU-1", HazardKind.RESTRICTED)
        notams = NotamRegister([notam("A0003/26", named(HAZARD, "AU-1"))])
        screen = screen_hazards(
            register(unbounded), regions=["AAAA"], planned_ft=35000
        )
        assert len(notams_on_hazards(notams, screen, APRIL)) == 1

    def test_the_key_space_is_shared_with_enr_2(self):
        """A State files against R-123 without caring which section published it."""
        from aeropub.airspace import AIRSPACE

        assert HAZARD == AIRSPACE


# --------------------------------------------------------------------------
# Clearance
# --------------------------------------------------------------------------


def clearance(state: str, **overrides) -> Clearance:
    fields = dict(state=state, kind=ClearanceKind.OVERFLIGHT, source=ref())
    fields.update(overrides)
    return Clearance(**fields)


class TestClearance:
    def test_a_lead_time_longer_than_the_notice_is_a_finding(self):
        found = screen_clearances(
            [clearance("AAAA", lead_time_hours=72)], notice_hours=24
        )
        assert len(found) == 1
        assert found[0].short_by_hours == 48

    def test_working_days_are_called_out_because_a_weekend_makes_it_worse(self):
        found = screen_clearances(
            [clearance("AAAA", lead_time_hours=48, working_days=True)],
            notice_hours=24,
        )
        assert "working days" in found[0].describe()

    def test_enough_notice_is_not_a_finding(self):
        assert screen_clearances(
            [clearance("AAAA", lead_time_hours=12)], notice_hours=24
        ) == ()

    def test_a_requirement_that_does_not_apply_is_not_a_finding(self):
        assert screen_clearances(
            [clearance("AAAA", required=False, lead_time_hours=72)], notice_hours=24
        ) == ()

    def test_a_missing_lead_time_is_a_different_problem_from_being_late(self):
        """Only the second is arithmetic."""
        screen = screen_hazards(
            register(clearances=(clearance("AAAA"),)),
            regions=["AAAA"], notice_hours=24,
        )
        assert screen.clearance_findings == ()
        assert [c.state for c in screen.clearances_without_lead_time] == ["AAAA"]
        assert "lead time is not held" in screen.render()

    def test_clearances_are_listed_and_not_screened_without_a_notice(self):
        """How much notice a flight has is not something the AIP knows."""
        screen = screen_hazards(
            register(clearances=(clearance("AAAA", lead_time_hours=72),)),
            regions=["AAAA"],
        )
        assert screen.clearance_findings == ()
        assert len(screen.clearances) == 1

    def test_a_clearance_cannot_be_built_without_a_citation(self):
        with pytest.raises(TypeError):
            Clearance(state="AAAA", kind=ClearanceKind.OVERFLIGHT, source=None)


# --------------------------------------------------------------------------
# Reading an ENR 5 manifest
# --------------------------------------------------------------------------


@pytest.fixture
def document(tmp_path: Path) -> Path:
    path = tmp_path / "enr5.txt"
    path.write_text("an ENR 5 table, standing in for one somebody read\n",
                    encoding="utf-8")
    return path


def write(tmp_path: Path, payload: dict) -> Path:
    path = tmp_path / "enr5.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def manifest(**overrides) -> dict:
    payload = {
        "source": {
            "source_id": "EXAMPLE",
            "document": "AIP ENR 5.1",
            "document_path": "enr5.txt",
            "retrieved_at": READ_AT,
        },
        "region": "AAAA",
        "hazards": [
            {
                "designator": "AR-7", "kind": "restricted",
                "lower": "FL200", "upper": "FL450",
                "activation": "scheduled", "hours": "0600-1800",
                "authority": "Alpha CAA", "locator": "ENR 5.1 row 3",
            },
            {
                "designator": "BIRD-N", "kind": "bird_migration",
                "lower": "SFC", "upper": "8000",
                "activation": "seasonal", "months": [4, 5, 9, 10],
                "activity": "raptor migration", "locator": "ENR 5.6 row 1",
            },
        ],
        "clearances": [
            {
                "state": "AAAA", "kind": "overflight", "lead_time_hours": 72,
                "working_days": True, "authority": "Alpha MFA",
                "locator": "GEN 1.2",
            }
        ],
    }
    payload.update(overrides)
    return payload


class TestLoading:
    def test_a_register_loads_with_every_entry_cited(self, tmp_path, document):
        held = load_hazards(write(tmp_path, manifest()))
        found = held.hazard("AR-7")
        assert found.source.locator == "ENR 5.1 row 3"
        assert found.lower_ft == 20000.0
        assert len(found.source.content_hash) == 64

    def test_seasonal_months_load(self, tmp_path, document):
        held = load_hazards(write(tmp_path, manifest()))
        assert held.hazard("BIRD-N").months == (4, 5, 9, 10)

    def test_clearances_load_beside_the_hazards(self, tmp_path, document):
        held = load_hazards(write(tmp_path, manifest()))
        assert held.for_state("AAAA")[0].lead_time_hours == 72

    def test_an_unknown_kind_is_refused(self, tmp_path, document):
        payload = manifest()
        payload["hazards"][0]["kind"] = "warning"
        with pytest.raises(ManifestError, match="kind must be"):
            load_hazards(write(tmp_path, payload))

    def test_an_unknown_activation_is_refused(self, tmp_path, document):
        payload = manifest()
        payload["hazards"][0]["activation"] = "sometimes"
        with pytest.raises(ManifestError, match="activation must be"):
            load_hazards(write(tmp_path, payload))

    def test_an_entry_needs_a_region(self, tmp_path, document):
        """Without it no route can surface it."""
        payload = manifest()
        del payload["region"]
        with pytest.raises(ManifestError, match="region is required"):
            load_hazards(write(tmp_path, payload))

    def test_an_entry_needs_a_locator(self, tmp_path, document):
        payload = manifest()
        del payload["hazards"][0]["locator"]
        with pytest.raises(ManifestError, match="locator"):
            load_hazards(write(tmp_path, payload))

    def test_an_unreadable_limit_is_refused(self, tmp_path, document):
        payload = manifest()
        payload["hazards"][0]["upper"] = "as notified"
        with pytest.raises(ManifestError, match="could not be read"):
            load_hazards(write(tmp_path, payload))

    def test_a_bad_month_is_refused(self, tmp_path, document):
        payload = manifest()
        payload["hazards"][1]["months"] = [4, "spring"]
        with pytest.raises(ManifestError, match="not a number 1 to 12"):
            load_hazards(write(tmp_path, payload))

    def test_the_template_round_trips_as_json(self):
        blank = json.loads(hazard_template())
        assert blank["hazards"][0]["activation"] == "by_notam"
        assert blank["hazards"][0]["months"] == []

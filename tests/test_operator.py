"""Layer three — the same change, two operators.

The plan states this as the product's reason to exist: an RFFS downgrade from
Category 9 to 7 is critical at a sole-suitable EDTO alternate for a wide-body
and irrelevant to a narrow-body operator that needs Category 6. Same
publication, same change record, same generic impact, two different answers.
The test that matters most here is that both answers fall out of held data
rather than a table of assertions — change the fleet, the role, or what the
aerodrome publishes, and the answer moves.

The rest is refusal, as everywhere else. "No exposure" is a real answer and
must never be produced by not having checked.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from aeropub.aip import AipCoverage
from aeropub.aircraft import AircraftType, Characteristic, Origin
from aeropub.dossier import build
from aeropub.facts import Fact, FactStore, Precedence
from aeropub.notam_register import NotamRegister, RegisteredNotam, Subject, SubjectKind
from aeropub.operator import (
    Exposure,
    Fleet,
    Network,
    NetworkEntry,
    OperatorAssessment,
    OperatorProfile,
    Role,
    assess_operator,
)
from aeropub.provenance import SourceRef
from aeropub.suitability import Assessment

AD = "XXXX"
RWY = "XXXX/RWY34L"
NOW = datetime(2026, 10, 5, 6, 0, tzinfo=timezone.utc)
ON = date(2026, 10, 5)


def ref(**overrides) -> SourceRef:
    fields = dict(
        source_id="TEST", document="test fixture — not a real publication",
        locator="AD 2.6", retrieved_at=NOW, content_hash="e" * 64,
        parser_id="test", parser_version="0.1.0",
    )
    fields.update(overrides)
    return SourceRef(**fields)


def fact(entity: str, attribute: str, value) -> Fact:
    return Fact(entity=entity, attribute=attribute, value=value,
                valid_from=date(2026, 1, 1), source=ref(), precedence=Precedence.AIP)


def characteristic(attribute: str, value, **overrides) -> Characteristic:
    fields = dict(attribute=attribute, value=value, source=ref(), origin=Origin.ACAP)
    fields.update(overrides)
    return Characteristic(**fields)


def dossier(*facts, register: NotamRegister | None = None):
    return build(AD, facts=FactStore(facts), coverage=AipCoverage(),
                 register=register or NotamRegister(), as_at=NOW, on=ON)


def aircraft(designator: str, *items) -> AircraftType:
    return AircraftType(designator=designator).with_characteristics(items)


#: Two aeroplanes differing only in dimensions, both fully cited. WIDE needs
#: fire Category 9; NARR needs Category 6.
WIDE = aircraft(
    "WIDE",
    characteristic("wingspan_m", 60.0),
    characteristic("omgws_m", 12.0),
    characteristic("reference_field_length_m", 3100.0),
    characteristic("overall_length_m", 70.0),
    characteristic("fuselage_width_m", 6.2),
    characteristic("acn", 62.0, variant="F/A at MTOW"),
)
NARR = aircraft(
    "NARR",
    characteristic("wingspan_m", 34.0),
    characteristic("omgws_m", 7.0),
    characteristic("reference_field_length_m", 2100.0),
    characteristic("overall_length_m", 37.6),
    characteristic("fuselage_width_m", 4.0),
    characteristic("acn", 45.0, variant="F/A at MTOW"),
)


def profile(*types, role=Role.DESTINATION, sole=False, name="Test Operator"):
    return OperatorProfile(
        name=name,
        fleet=Fleet(types),
        network=Network((NetworkEntry(AD, role, sole_suitable=sole),)),
    )


def complete(rffs: int = 9):
    """An aerodrome where every check can be made."""
    return dossier(
        fact(AD, "aerodrome_reference_code", "4E"),
        fact(AD, "rffs_category", rffs),
        fact(RWY, "pcn", "80/F/A/W/T"),
        fact(RWY, "runway_width_m", 60.0),
    )


# --------------------------------------------------------------------------
# The headline
# --------------------------------------------------------------------------


class TestTheSameChangeTwoOperators:
    def test_a_fire_category_downgrade_splits_the_two_fleets(self):
        # The plan's section 13 row, computed rather than asserted. Nothing in
        # the module says "RFFS downgrade is critical" — it falls out of what
        # each aeroplane requires under Annex 14 Table 9-1.
        after = complete(rffs=7)

        exposed = assess_operator(after, profile(WIDE, role=Role.EDTO_ALTERNATE, sole=True))
        unaffected = assess_operator(after, profile(NARR, role=Role.DESTINATION))

        assert exposed.overall is Exposure.CRITICAL
        assert unaffected.worst_by_type()["NARR"] is Exposure.NONE

    def test_before_the_downgrade_neither_is_exposed(self):
        # Same fleets, same roles. Only the published category changed.
        before = complete(rffs=9)
        for who in (
            profile(WIDE, role=Role.EDTO_ALTERNATE, sole=True),
            profile(NARR, role=Role.DESTINATION),
        ):
            assert assess_operator(before, who).overall is Exposure.NONE

    def test_one_fleet_two_types_reports_both(self):
        # "Your fleet is exposed" is not actionable. "The WIDE is, the NARR is
        # not" is.
        result = assess_operator(
            complete(rffs=7), profile(WIDE, NARR, role=Role.DESTINATION)
        )
        assert result.worst_by_type() == {
            "WIDE": Exposure.CRITICAL,
            "NARR": Exposure.NONE,
        }

    def test_the_roll_up_is_the_worst_case_never_an_average(self):
        # An operator whose wide-body cannot use an aerodrome is not "medium"
        # because their narrow-body can.
        result = assess_operator(
            complete(rffs=7), profile(WIDE, NARR, role=Role.DESTINATION)
        )
        assert result.overall is Exposure.CRITICAL


# --------------------------------------------------------------------------
# Role is the multiplier
# --------------------------------------------------------------------------


class TestRoleChangesTheAnswer:
    def failing(self, role, sole=False):
        return assess_operator(
            complete(rffs=7), profile(WIDE, role=role, sole=sole)
        )

    def test_an_edto_alternate_is_critical(self):
        result = self.failing(Role.EDTO_ALTERNATE)
        assert result.overall is Exposure.CRITICAL
        assert "already airborne" in result.actionable[0].reason

    def test_a_destination_is_critical(self):
        assert self.failing(Role.DESTINATION).overall is Exposure.CRITICAL

    def test_an_ordinary_alternate_is_high_because_another_can_be_nominated(self):
        result = self.failing(Role.ALTERNATE)
        rffs = next(
            f for f in result.findings if f.check.name == "Rescue and fire fighting"
        )
        assert rffs.exposure is Exposure.HIGH
        assert "Another must be nominated" in rffs.reason

    def test_a_sole_suitable_alternate_has_nothing_to_swap_to(self):
        result = self.failing(Role.ALTERNATE, sole=True)
        rffs = next(
            f for f in result.findings if f.check.name == "Rescue and fire fighting"
        )
        assert rffs.exposure is Exposure.CRITICAL
        assert "nothing to swap to" in rffs.reason

    def test_an_overflown_aerodrome_is_not_exposed_to_a_landing_check(self):
        result = self.failing(Role.ENROUTE)
        assert result.overall is Exposure.NONE
        assert all("bears on landing" in f.reason for f in result.findings)

    def test_the_most_demanding_role_governs_where_several_apply(self):
        # An aerodrome does not stop being somebody's EDTO alternate because it
        # is also a destination.
        both = OperatorProfile(
            name="Test Operator",
            fleet=Fleet((WIDE,)),
            network=Network((
                NetworkEntry(AD, Role.DESTINATION),
                NetworkEntry(AD, Role.EDTO_ALTERNATE),
            )),
        )
        assert assess_operator(complete(rffs=7), both).role is Role.EDTO_ALTERNATE

    def test_sole_suitable_is_recorded_not_inferred(self):
        # Only the operator knows what else is within reach.
        assert not Network((NetworkEntry(AD, Role.ALTERNATE),)).is_sole_suitable(AD)
        assert Network(
            (NetworkEntry(AD, Role.ALTERNATE, sole_suitable=True),)
        ).is_sole_suitable(AD)


# --------------------------------------------------------------------------
# No exposure, and what it must never mean
# --------------------------------------------------------------------------


class TestNoExposureIsARealAnswer:
    def test_an_aerodrome_outside_the_network_resolves_to_none(self):
        elsewhere = OperatorProfile(
            name="Test Operator", fleet=Fleet((WIDE,)),
            network=Network((NetworkEntry("YYYY", Role.DESTINATION),)),
        )
        result = assess_operator(complete(rffs=7), elsewhere)
        assert result.role is Role.NOT_IN_NETWORK
        assert result.overall is Exposure.NONE
        assert "not in the network" in result.findings[0].reason

    def test_the_record_beneath_survives_in_full(self):
        # Nothing is skipped to save work: when the operator adds this
        # destination, the assessment is already computed.
        elsewhere = OperatorProfile(
            name="Test Operator", fleet=Fleet((WIDE,)),
            network=Network((NetworkEntry("YYYY", Role.DESTINATION),)),
        )
        result = assess_operator(complete(rffs=7), elsewhere)
        assert len(result.suitability) == 1
        assert result.suitability[0].overall is Assessment.NOT_SUITABLE

    def test_an_empty_network_still_computes_the_assessment(self):
        result = assess_operator(complete(), profile(WIDE, role=Role.NOT_IN_NETWORK))
        assert result.suitability


class TestUnknownNeverBecomesNoExposure:
    def test_an_unmade_check_is_unknown_not_none(self):
        # Not being able to check and having no exposure are opposite
        # conclusions that would otherwise print the same comforting word.
        bare = dossier(fact(AD, "rffs_category", 9))
        result = assess_operator(bare, profile(WIDE, role=Role.DESTINATION))
        pavement = next(
            f for f in result.findings if f.check.name == "Pavement strength"
        )
        assert pavement.exposure is Exposure.UNKNOWN
        assert "not an absence of exposure" in pavement.reason

    def test_an_unmade_check_where_dispatch_relies_on_it_is_worse_than_unknown(self):
        # A flight cannot be planned against "no exposure" when the truth is
        # "nobody checked".
        bare = dossier(fact(AD, "rffs_category", 9))
        result = assess_operator(bare, profile(WIDE, role=Role.EDTO_ALTERNATE))
        pavement = next(
            f for f in result.findings if f.check.name == "Pavement strength"
        )
        assert pavement.exposure is Exposure.HIGH
        assert "opposite conclusions" in pavement.reason

    def test_a_fleet_type_with_no_figures_cannot_be_cleared(self):
        # An aeroplane we hold nothing about is not an aeroplane with no
        # exposure.
        blank = AircraftType(designator="BLNK")
        result = assess_operator(complete(), profile(blank, role=Role.DESTINATION))
        assert result.overall is Exposure.UNKNOWN
        assert not result.is_conclusive

    def test_unknown_outranks_every_pass_in_the_roll_up(self):
        partial = dossier(
            fact(AD, "aerodrome_reference_code", "4E"),
            fact(AD, "rffs_category", 9),
        )
        result = assess_operator(partial, profile(WIDE, role=Role.DESTINATION))
        assert any(f.exposure is Exposure.NONE for f in result.findings)
        assert result.overall is Exposure.UNKNOWN

    def test_an_empty_assessment_is_unknown_not_none(self):
        empty = OperatorAssessment(
            operator="Test Operator", aerodrome=AD, as_at=NOW, role=Role.DESTINATION
        )
        assert empty.overall is Exposure.UNKNOWN
        assert not empty.is_conclusive

    def test_a_notam_over_a_checked_value_keeps_it_inconclusive(self):
        # The suitability layer's own guard carries through: a check computed
        # from AIP values that a live NOTAM overlays is not something to
        # dispatch against.
        register = NotamRegister([
            RegisteredNotam(
                identifier="A2291/26",
                subjects=(Subject(entity=RWY, kind=SubjectKind.RUNWAY, designator="34L"),),
                source=ref(document="NOTAM A2291/26"),
                text="RWY 34L CLSD",
                effective_start=datetime(2026, 10, 1, tzinfo=timezone.utc),
                effective_end=datetime(2026, 10, 31, tzinfo=timezone.utc),
            )
        ])
        overlaid = build(
            AD,
            facts=FactStore([
                fact(AD, "aerodrome_reference_code", "4E"),
                fact(AD, "rffs_category", 9),
                fact(RWY, "pcn", "80/F/A/W/T"),
                fact(RWY, "runway_width_m", 60.0),
            ]),
            coverage=AipCoverage(), register=register, as_at=NOW, on=ON,
        )
        result = assess_operator(overlaid, profile(WIDE, role=Role.EDTO_ALTERNATE))
        assert not result.is_conclusive


class TestRestrictedConditions:
    def test_a_condition_at_an_ordinary_destination_is_medium(self):
        overloaded = dossier(
            fact(AD, "aerodrome_reference_code", "4E"),
            fact(AD, "rffs_category", 9),
            fact(RWY, "pcn", "50/F/A/W/T"),
            fact(RWY, "runway_width_m", 60.0),
        )
        result = assess_operator(overloaded, profile(WIDE, role=Role.DESTINATION))
        pavement = next(
            f for f in result.findings if f.check.name == "Pavement strength"
        )
        assert pavement.exposure is Exposure.MEDIUM

    def test_the_same_condition_where_dispatch_relies_on_it_is_high(self):
        overloaded = dossier(
            fact(AD, "aerodrome_reference_code", "4E"),
            fact(AD, "rffs_category", 9),
            fact(RWY, "pcn", "50/F/A/W/T"),
            fact(RWY, "runway_width_m", 60.0),
        )
        result = assess_operator(overloaded, profile(WIDE, role=Role.EDTO_ALTERNATE))
        pavement = next(
            f for f in result.findings if f.check.name == "Pavement strength"
        )
        assert pavement.exposure is Exposure.HIGH
        assert "every time, not on the day" in pavement.reason


# --------------------------------------------------------------------------
# The profile
# --------------------------------------------------------------------------


class TestProfile:
    def test_a_type_listed_twice_is_refused(self):
        # Two manifests for one type are merged, not listed twice — otherwise
        # one silently answers and the other does not.
        with pytest.raises(ValueError) as caught:
            Fleet((WIDE, WIDE))
        assert "more than once" in str(caught.value)

    def test_an_unnamed_operator_is_refused(self):
        with pytest.raises(ValueError):
            OperatorProfile(name="  ")

    def test_network_keys_are_normalised(self):
        network = Network((NetworkEntry("  xxxx  ", Role.DESTINATION),))
        assert network.role_of("XXXX") is Role.DESTINATION
        assert network.role_of("xxxx") is Role.DESTINATION

    def test_a_role_must_be_a_role(self):
        with pytest.raises(TypeError):
            NetworkEntry(AD, "destination")  # type: ignore[arg-type]

    def test_a_fleet_finds_its_types_by_designator(self):
        fleet = Fleet((WIDE, NARR))
        assert fleet.type("narr") is NARR
        assert fleet.type("A320") is None
        assert len(fleet) == 2


class TestOutput:
    def test_the_worst_finding_is_read_first(self):
        # A list that buries the critical finding under three high ones fails
        # at the job it exists for.
        result = assess_operator(
            dossier(fact(AD, "rffs_category", 7)),
            profile(WIDE, role=Role.EDTO_ALTERNATE, sole=True),
        )
        assert result.actionable[0].exposure is Exposure.CRITICAL

    def test_the_render_leads_with_the_per_type_split(self):
        printed = assess_operator(
            complete(rffs=7), profile(WIDE, NARR, role=Role.DESTINATION)
        ).render()
        assert "By type" in printed
        assert printed.index("By type") < printed.index("NEEDS ACTION")
        assert "WIDE" in printed and "NARR" in printed

    def test_the_render_says_the_record_beneath_is_unchanged(self):
        printed = assess_operator(complete(), profile(WIDE)).render()
        assert "same for everyone" in printed

    def test_an_empty_fleet_is_not_a_clean_answer(self):
        printed = assess_operator(
            complete(), OperatorProfile(name="Test Operator")
        ).render()
        assert "not a clean answer" in printed

    def test_unmade_checks_get_their_own_heading(self):
        printed = assess_operator(
            dossier(fact(AD, "rffs_category", 9)),
            profile(WIDE, role=Role.DESTINATION),
        ).render()
        assert "not an absence of exposure" in printed

    def test_findings_name_the_type_they_are_about(self):
        result = assess_operator(complete(rffs=7), profile(WIDE, NARR))
        assert all(f.designator in ("WIDE", "NARR") for f in result.findings)
        assert {f.designator for f in result.for_type("wide")} == {"WIDE"}


class TestLayerTwoIsUnchangedBeneath:
    def test_the_suitability_record_names_no_operator(self):
        # Layer three may name a fleet. What it must not do is push that back
        # down into the layer everyone shares.
        result = assess_operator(
            complete(rffs=7), profile(WIDE, role=Role.EDTO_ALTERNATE, sole=True)
        )
        printed = "\n".join(s.render() for s in result.suitability)
        for word in ("operator", "fleet", "network", "edto", "sole"):
            assert word not in printed.lower(), f"layer two leaked {word!r}"

    def test_the_same_dossier_gives_every_operator_the_same_suitability(self):
        held = complete(rffs=7)
        one = assess_operator(held, profile(WIDE, role=Role.EDTO_ALTERNATE, sole=True))
        two = assess_operator(held, profile(WIDE, role=Role.ENROUTE))
        assert one.suitability[0].checks == two.suitability[0].checks
        assert one.overall is not two.overall

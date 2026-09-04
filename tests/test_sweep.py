"""The whole network at once, and the aerodromes nobody has read.

Two things are being tested. The first is arithmetic honesty: an aerodrome with
nothing held must never be counted among the clear ones, because a dashboard
showing 197 green where 150 of the greens were never read is the single worst
artefact this system could produce — worse than no dashboard, because it puts a
number on absence and somebody will quote it.

The second is the forward half, which is the reason the module exists. Knowing
a supplement expires on a date is interesting. Knowing that when it does, a
sole-suitable EDTO alternate goes invalid, and that no State will publish a
word about it, is the answer.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from aeropub.aircraft import AircraftType, Characteristic, Origin
from aeropub.facts import Fact, FactStore, Precedence
from aeropub.operator import (
    Exposure,
    Fleet,
    Network,
    NetworkEntry,
    OperatorProfile,
    Role,
    assess_operator,
)
from aeropub.provenance import SourceRef
from aeropub.sweep import NetworkSweep, sweep

NOW = datetime(2026, 10, 5, 6, 0, tzinfo=timezone.utc)
ON = date(2026, 10, 5)


def ref() -> SourceRef:
    return SourceRef(
        source_id="TEST", document="test fixture — not a real publication",
        locator="AD 2", retrieved_at=NOW, content_hash="e" * 64,
        parser_id="test", parser_version="0.1.0",
    )


def fact(entity, attribute, value, *, layer=Precedence.AIP,
         valid_from=date(2026, 1, 1), valid_to=None) -> Fact:
    return Fact(entity=entity, attribute=attribute, value=value,
                valid_from=valid_from, valid_to=valid_to,
                source=ref(), precedence=layer)


def characteristic(attribute, value, *, variant=None) -> Characteristic:
    return Characteristic(attribute=attribute, value=value, source=ref(),
                          origin=Origin.ACAP, variant=variant)


#: Needs fire Category 9.
WIDE = AircraftType(designator="WIDE").with_characteristics([
    characteristic("wingspan_m", 60.0),
    characteristic("omgws_m", 12.0),
    characteristic("reference_field_length_m", 3100.0),
    characteristic("overall_length_m", 70.0),
    characteristic("fuselage_width_m", 6.2),
    # The variant is the ACAP table cell. Without it the pavement check
    # correctly refuses to match the figure to a reported rating.
    characteristic("acn", 62.0, variant="F/A at MTOW"),
])


def complete(aerodrome: str, rffs: int = 9) -> list[Fact]:
    """Every value a check needs, for one aerodrome."""
    return [
        fact(aerodrome, "aerodrome_reference_code", "4E"),
        fact(aerodrome, "rffs_category", rffs),
        fact(f"{aerodrome}/RWY34L", "pcn", "80/F/A/W/T"),
        fact(f"{aerodrome}/RWY34L", "runway_width_m", 60.0),
    ]


def profile(*entries: NetworkEntry, name="Example Airways") -> OperatorProfile:
    return OperatorProfile(name=name, fleet=Fleet((WIDE,)),
                           network=Network(entries))


def run(facts, *entries, **kwargs) -> NetworkSweep:
    return sweep(FactStore(facts), profile(*entries), as_at=NOW, on=ON, **kwargs)


# --------------------------------------------------------------------------
# Coverage is never rolled into "clear"
# --------------------------------------------------------------------------


class TestUnreadAerodromesAreNeverClear:
    def test_an_aerodrome_with_nothing_held_is_reported_as_unread(self):
        result = run([], NetworkEntry("DDDD", Role.DESTINATION))
        assert result.summary()["uncovered"] == 1
        assert result.summary()["clear"] == 0
        assert [e.aerodrome for e in result.uncovered] == ["DDDD"]

    def test_the_clear_count_excludes_everything_unread(self):
        # The arithmetic that matters. Two aerodromes, one read and clear, one
        # never read. "1 clear of 2" is the honest answer; "2 clear" is not.
        result = run(
            complete("BBBB"),
            NetworkEntry("BBBB", Role.DESTINATION),
            NetworkEntry("DDDD", Role.DESTINATION),
        )
        counts = result.summary()
        assert counts == {**counts, "aerodromes": 2, "covered": 1,
                          "uncovered": 1, "clear": 1}

    def test_no_percentage_can_be_quoted_without_the_coverage_number(self):
        # covered + uncovered accounts for every aerodrome, and the severity
        # counts only ever describe the covered ones.
        result = run(
            complete("AAAA", rffs=7) + complete("BBBB"),
            NetworkEntry("AAAA", Role.DESTINATION),
            NetworkEntry("BBBB", Role.DESTINATION),
            NetworkEntry("DDDD", Role.DESTINATION),
        )
        counts = result.summary()
        assert counts["covered"] + counts["uncovered"] == counts["aerodromes"]
        graded = sum(
            counts[k] for k in ("critical", "high", "medium", "unknown", "clear")
        )
        assert graded == counts["covered"]

    def test_an_unread_aerodrome_makes_the_sweep_inconclusive(self):
        result = run(
            complete("BBBB"),
            NetworkEntry("BBBB", Role.DESTINATION),
            NetworkEntry("DDDD", Role.DESTINATION),
        )
        assert not result.is_conclusive

    def test_a_fully_read_network_is_conclusive(self):
        result = run(complete("BBBB"), NetworkEntry("BBBB", Role.DESTINATION))
        assert result.is_conclusive
        assert result.overall is Exposure.NONE

    def test_unread_sorts_above_read_at_the_same_exposure(self):
        # An unknown where nobody looked is a different problem from an unknown
        # where somebody looked and came up short.
        result = run(
            [fact("BBBB", "rffs_category", 9)],
            NetworkEntry("BBBB", Role.DESTINATION),
            NetworkEntry("DDDD", Role.DESTINATION),
        )
        assert [e.aerodrome for e in result.ranked] == ["DDDD", "BBBB"]

    def test_an_empty_network_is_not_a_clean_network(self):
        result = run([])
        assert not result.is_conclusive
        assert result.overall is Exposure.UNKNOWN
        assert "not a network with nothing wrong" in result.render()


# --------------------------------------------------------------------------
# Ranking and agreement with the single-aerodrome report
# --------------------------------------------------------------------------


class TestRanking:
    def test_the_worst_aerodrome_is_read_first(self):
        result = run(
            complete("AAAA", rffs=7) + complete("BBBB"),
            NetworkEntry("AAAA", Role.DESTINATION),
            NetworkEntry("BBBB", Role.DESTINATION),
        )
        assert result.ranked[0].aerodrome == "AAAA"
        assert result.overall is Exposure.CRITICAL

    def test_a_sole_suitable_aerodrome_sorts_above_an_equal_one(self):
        result = run(
            complete("AAAA", rffs=7) + complete("CCCC", rffs=7),
            NetworkEntry("AAAA", Role.DESTINATION),
            NetworkEntry("CCCC", Role.DESTINATION, sole_suitable=True),
        )
        assert result.ranked[0].aerodrome == "CCCC"

    def test_an_aerodrome_serving_two_roles_is_swept_once(self):
        # Under its most demanding role, the same rule the profile uses —
        # otherwise it appears twice with two different answers.
        result = run(
            complete("AAAA", rffs=7),
            NetworkEntry("AAAA", Role.DESTINATION),
            NetworkEntry("AAAA", Role.EDTO_ALTERNATE),
        )
        assert len(result.entries) == 1
        assert result.entries[0].role is Role.EDTO_ALTERNATE

    def test_the_sweep_agrees_with_the_single_aerodrome_report(self):
        # A number here that disagrees with the report for the same aerodrome
        # is a defect in this module.
        facts = complete("AAAA", rffs=7)
        who = profile(NetworkEntry("AAAA", Role.EDTO_ALTERNATE, sole_suitable=True))
        swept = sweep(FactStore(facts), who, as_at=NOW, on=ON)

        from aeropub.aip import AipCoverage
        from aeropub.dossier import build
        from aeropub.notam_register import NotamRegister

        alone = assess_operator(
            build("AAAA", facts=FactStore(facts), coverage=AipCoverage(),
                  register=NotamRegister(), as_at=NOW, on=ON),
            who,
        )
        assert swept.entries[0].assessment.overall is alone.overall
        assert swept.entries[0].assessment.findings == alone.findings


# --------------------------------------------------------------------------
# The forward half
# --------------------------------------------------------------------------


def lapsing_supplement(aerodrome: str) -> list[Fact]:
    """The AIP says Category 7. A supplement holds it at 9 until 20 November.

    When the supplement lapses the 7 beneath resurfaces. From the State's side
    nothing has happened, so nothing is published.
    """
    return [
        fact(aerodrome, "aerodrome_reference_code", "4E"),
        fact(aerodrome, "rffs_category", 7),
        fact(aerodrome, "rffs_category", 9, layer=Precedence.SUP,
             valid_from=date(2026, 9, 1), valid_to=date(2026, 11, 20)),
        fact(f"{aerodrome}/RWY34L", "pcn", "80/F/A/W/T"),
        fact(f"{aerodrome}/RWY34L", "runway_width_m", 60.0),
    ]


class TestExposureAhead:
    def sweep_it(self, role=Role.EDTO_ALTERNATE, sole=True):
        return run(
            lapsing_supplement("CCCC"),
            NetworkEntry("CCCC", role, sole_suitable=sole),
        )

    def test_exposure_today_is_clear(self):
        entry = self.sweep_it().entries[0]
        assert entry.exposure is Exposure.NONE

    def test_and_it_is_not_clear_on_the_day_the_supplement_lapses(self):
        entry = self.sweep_it().entries[0]
        assert entry.worsens_on == date(2026, 11, 21)
        assert entry.worst_ahead is Exposure.CRITICAL

    def test_the_worsening_is_flagged_as_unannounced(self):
        # The two halves have to be read together: a worsening on an AIRAC date
        # arrives with a publication somebody reads. This one arrives with
        # nothing at all.
        assert self.sweep_it().entries[0].deteriorates_unannounced

    def test_the_deteriorating_list_is_soonest_first(self):
        result = run(
            lapsing_supplement("CCCC") + [
                fact("EEEE", "aerodrome_reference_code", "4E"),
                fact("EEEE", "rffs_category", 7),
                fact("EEEE", "rffs_category", 9, layer=Precedence.SUP,
                     valid_from=date(2026, 9, 1), valid_to=date(2026, 10, 20)),
            ],
            NetworkEntry("CCCC", Role.EDTO_ALTERNATE, sole_suitable=True),
            NetworkEntry("EEEE", Role.EDTO_ALTERNATE, sole_suitable=True),
        )
        assert [e.aerodrome for e in result.deteriorating] == ["EEEE", "CCCC"]

    def test_an_aerodrome_that_does_not_worsen_is_not_listed(self):
        result = run(complete("BBBB"), NetworkEntry("BBBB", Role.DESTINATION))
        assert result.deteriorating == ()
        assert result.entries[0].worsens_on is None
        assert result.entries[0].worst_ahead is Exposure.NONE

    def test_a_change_that_does_not_touch_this_fleet_does_not_deteriorate(self):
        # The supplement lapses and the category falls from 10 to 9. WIDE needs
        # 9, so nothing about this operator changes.
        result = run(
            [
                fact("CCCC", "aerodrome_reference_code", "4E"),
                fact("CCCC", "rffs_category", 9),
                fact("CCCC", "rffs_category", 10, layer=Precedence.SUP,
                     valid_from=date(2026, 9, 1), valid_to=date(2026, 11, 20)),
                fact("CCCC/RWY34L", "pcn", "80/F/A/W/T"),
                fact("CCCC/RWY34L", "runway_width_m", 60.0),
            ],
            NetworkEntry("CCCC", Role.EDTO_ALTERNATE, sole_suitable=True),
        )
        assert result.entries[0].changes_ahead  # the change is still reported
        assert result.entries[0].worsens_on is None  # it just does not bite

    def test_the_summary_counts_the_unannounced_deteriorations_separately(self):
        counts = self.sweep_it().summary()
        assert counts["deteriorating"] == 1
        assert counts["deteriorating_unannounced"] == 1

    def test_the_window_bounds_the_forward_view(self):
        near = run(
            lapsing_supplement("CCCC"),
            NetworkEntry("CCCC", Role.EDTO_ALTERNATE),
            days=7,
        )
        assert near.entries[0].worsens_on is None
        assert near.deteriorating == ()


class TestOutput:
    def test_the_unread_warning_precedes_the_findings(self):
        printed = run(
            complete("AAAA", rffs=7),
            NetworkEntry("AAAA", Role.DESTINATION),
            NetworkEntry("DDDD", Role.DESTINATION),
        ).render()
        assert "never been read" in printed
        assert printed.index("never been read") < printed.index("NEEDS ACTION")
        assert "They are not clear" in printed

    def test_a_worsening_names_its_date_and_says_nothing_is_published(self):
        printed = run(
            lapsing_supplement("CCCC"),
            NetworkEntry("CCCC", Role.EDTO_ALTERNATE, sole_suitable=True),
        ).render()
        assert "EXPOSURE WORSENS AHEAD" in printed
        assert "2026-11-21" in printed
        assert "nothing will be published" in printed

    def test_unread_aerodromes_get_their_own_section(self):
        printed = run([], NetworkEntry("DDDD", Role.DESTINATION)).render()
        assert "NOTHING HELD" in printed
        assert "before relying on anything above" in printed

    def test_the_counts_line_shows_read_and_never_read(self):
        printed = run(
            complete("BBBB"),
            NetworkEntry("BBBB", Role.DESTINATION),
            NetworkEntry("DDDD", Role.DESTINATION),
        ).render()
        assert "1 read" in printed
        assert "1 never read" in printed


class TestInputs:
    def test_a_naive_timestamp_is_refused(self):
        with pytest.raises(ValueError):
            sweep(FactStore(), profile(), as_at=datetime(2026, 10, 5))


class TestNoActionMeansNoAction:
    def test_an_aerodrome_that_deteriorates_is_not_listed_as_no_action(self):
        # It is clear today and critical in 47 days. The action is to plan for
        # it now, and a reader who scans headings must not stop at the wrong
        # one.
        printed = run(
            lapsing_supplement("CCCC"),
            NetworkEntry("CCCC", Role.EDTO_ALTERNATE, sole_suitable=True),
        ).render()
        assert "EXPOSURE WORSENS AHEAD" in printed
        if "No action" in printed:
            assert "CCCC" not in printed[printed.index("No action"):]

    def test_an_aerodrome_that_is_clear_and_stays_clear_is_listed(self):
        printed = run(
            complete("BBBB"), NetworkEntry("BBBB", Role.DESTINATION)
        ).render()
        assert "No action" in printed
        assert "BBBB" in printed[printed.index("No action"):]


# --------------------------------------------------------------------------
# Redundancy — the finding no single aerodrome carries
# --------------------------------------------------------------------------

REGION = "North Atlantic alternates"
STALE_READ = datetime(2026, 5, 2, tzinfo=timezone.utc)


def fact_read_at(entity, attribute, value, when) -> Fact:
    return Fact(
        entity=entity, attribute=attribute, value=value,
        valid_from=date(2026, 1, 1), precedence=Precedence.AIP,
        source=SourceRef(
            source_id="TEST", document="AIP AD 2", locator=attribute,
            retrieved_at=when, content_hash="e" * 64,
            parser_id="aip-manifest", parser_version="1",
        ),
    )


def held(aerodrome: str, rffs: int = 9, *, when=NOW) -> list[Fact]:
    return [
        fact_read_at(aerodrome, "aerodrome_reference_code", "4E", when),
        fact_read_at(aerodrome, "rffs_category", rffs, when),
        fact_read_at(f"{aerodrome}/RWY34L", "pcn", "80/F/A/W/T", when),
        fact_read_at(f"{aerodrome}/RWY34L", "runway_width_m", 60.0, when),
    ]


def region(*aerodromes: str) -> tuple[NetworkEntry, ...]:
    return tuple(
        NetworkEntry(a, Role.EDTO_ALTERNATE, group=REGION) for a in aerodromes
    )


class TestRedundancyIsNotTheWorstMember:
    def test_three_healthy_alternates_carry_no_group_finding(self):
        result = run(
            held("ALFA") + held("BRVO") + held("CHLI"), *region("ALFA", "BRVO", "CHLI")
        )
        group = result.groups[0]
        assert group.remaining == 3
        assert group.exposure is Exposure.NONE
        assert result.at_risk_groups == ()

    def test_one_critical_member_among_three_is_not_a_group_finding(self):
        # The critical one is a finding about that aerodrome, not the region.
        result = run(
            held("ALFA") + held("BRVO", rffs=7) + held("CHLI"),
            *region("ALFA", "BRVO", "CHLI"),
        )
        assert result.groups[0].remaining == 2
        assert result.groups[0].exposure is Exposure.NONE

    def test_two_degrading_in_one_cycle_leaves_the_region_single_threaded(self):
        # The failure this exists for. Two unrelated medium findings is what
        # the per-aerodrome view produces; a region down to one is what it is.
        result = run(
            held("ALFA") + held("BRVO", rffs=7) + held("CHLI", rffs=7),
            *region("ALFA", "BRVO", "CHLI"),
        )
        group = result.groups[0]
        assert group.is_single_threaded
        assert group.exposure is Exposure.HIGH
        assert "one left of 3" in group.describe()

    def test_the_operator_declared_none_of_them_sole_suitable(self):
        # And that is the point: this is derived, and catches the case they
        # have not noticed.
        result = run(
            held("ALFA") + held("BRVO", rffs=7) + held("CHLI", rffs=7),
            *region("ALFA", "BRVO", "CHLI"),
        )
        assert not any(e.sole_suitable for e in result.entries)
        assert result.groups[0].is_single_threaded

    def test_a_region_with_nothing_left_is_critical(self):
        result = run(
            held("ALFA", rffs=7) + held("BRVO", rffs=7), *region("ALFA", "BRVO")
        )
        group = result.groups[0]
        assert group.is_exhausted
        assert group.exposure is Exposure.CRITICAL
        assert "exhausted" in group.describe()

    def test_a_group_finding_reaches_the_sweep_overall(self):
        # No aerodrome in this region carries the group's exposure, so an
        # overall taken from members alone would miss it entirely.
        result = run(
            held("ALFA") + held("BRVO", when=STALE_READ) + held("CHLI", when=STALE_READ),
            *region("ALFA", "BRVO", "CHLI"),
        )
        assert all(e.exposure is Exposure.NONE for e in result.entries)
        assert result.groups[0].exposure is Exposure.HIGH
        assert result.overall is Exposure.HIGH


class TestStaleDataCannotPropUpARegion:
    def test_a_stale_clear_verdict_does_not_count_toward_redundancy(self):
        # Counting it would make the group look healthier the longer nobody
        # looked at it, which is precisely backwards.
        result = run(
            held("ALFA") + held("BRVO", when=STALE_READ), *region("ALFA", "BRVO")
        )
        group = result.groups[0]
        assert group.remaining == 1
        assert [e.aerodrome for e in group.unreliable] == ["BRVO"]

    def test_unreliable_is_not_the_same_as_degraded(self):
        # A degraded aerodrome is a known problem; an unreliable one is an
        # unknown, and the fix is different — go and read it.
        result = run(
            held("ALFA") + held("BRVO", rffs=7) + held("CHLI", when=STALE_READ),
            *region("ALFA", "BRVO", "CHLI"),
        )
        group = result.groups[0]
        assert [e.aerodrome for e in group.degraded] == ["BRVO"]
        assert [e.aerodrome for e in group.unreliable] == ["CHLI"]

    def test_an_unread_member_counts_as_unreliable_too(self):
        result = run(held("ALFA") + held("BRVO"), *region("ALFA", "BRVO", "DDDD"))
        group = result.groups[0]
        assert [e.aerodrome for e in group.unreliable] == ["DDDD"]
        assert group.exposure is Exposure.MEDIUM

    def test_stale_data_makes_the_sweep_inconclusive(self):
        result = run(held("ALFA", when=STALE_READ), NetworkEntry("ALFA", Role.DESTINATION))
        assert result.entries[0].is_covered
        assert not result.entries[0].is_current
        assert not result.entries[0].is_dependable
        assert not result.is_conclusive

    def test_the_summary_separates_clear_from_dependable(self):
        # Two clear, one of them stale. "2 clear" is true and "2 dependable"
        # would not be.
        result = run(
            held("ALFA") + held("BRVO", when=STALE_READ),
            NetworkEntry("ALFA", Role.DESTINATION),
            NetworkEntry("BRVO", Role.DESTINATION),
        )
        counts = result.summary()
        assert counts["clear"] == 2
        assert counts["dependable"] == 1
        assert counts["stale"] == 1

    def test_stale_aerodromes_get_their_own_section(self):
        printed = run(
            held("ALFA", when=STALE_READ), NetworkEntry("ALFA", Role.DESTINATION)
        ).render()
        assert "STALE" in printed
        assert "a claim about the past" in printed
        assert "cycles behind" in printed


class TestRedundancyAhead:
    def test_a_region_that_thins_on_a_known_date_says_when(self):
        result = run(
            held("ALFA") + lapsing_supplement("CCCC"),
            NetworkEntry("ALFA", Role.EDTO_ALTERNATE, group=REGION),
            NetworkEntry("CCCC", Role.EDTO_ALTERNATE, group=REGION),
        )
        group = result.groups[0]
        assert group.remaining == 2
        assert group.thins_on == date(2026, 11, 21)
        assert group.remaining_on(date(2026, 11, 21)) == 1
        assert "falls to 1 on 2026-11-21" in group.describe()

    def test_a_region_that_stays_whole_reports_no_thinning(self):
        result = run(held("ALFA") + held("BRVO"), *region("ALFA", "BRVO"))
        assert result.groups[0].thins_on is None


class TestGroupsAreOptional:
    def test_a_network_with_no_groups_has_none(self):
        result = run(held("ALFA"), NetworkEntry("ALFA", Role.DESTINATION))
        assert result.groups == ()
        assert result.at_risk_groups == ()
        assert result.summary()["groups"] == 0

    def test_an_aerodrome_records_the_groups_it_belongs_to(self):
        result = run(held("ALFA"), NetworkEntry("ALFA", Role.EDTO_ALTERNATE, group=REGION))
        assert result.entries[0].groups == (REGION,)

    def test_the_render_names_each_member_and_its_state(self):
        printed = run(
            held("ALFA") + held("BRVO", rffs=7) + held("CHLI", when=STALE_READ),
            *region("ALFA", "BRVO", "CHLI"),
        ).render()
        assert "REDUNDANCY" in printed
        assert "which no single aerodrome carries" in printed
        block = printed[printed.index("REDUNDANCY"):]
        assert "ALFA     dependable" in block
        assert "CHLI     stale" in block

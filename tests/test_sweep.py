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

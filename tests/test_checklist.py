"""GEN 0.4 — the State's own list, against what we hold.

Every other module answers what the AIP says. This one answers whether what we
hold *is* the AIP, and the assertions are about the four ways that goes wrong.

**Stale is the finding that matters.** A section nobody fetched is visible
everywhere downstream. A section held at last cycle renders, cites and answers,
and every answer is one cycle out of date, with nothing on its face to say so.

**Contradicted points at us.** An ABSENT holding is a claim about the State,
and the State's own checklist is what proves it wrong.

**Ahead points at the checklist.** Holding a newer cycle than the checklist
names is not a coverage gap: the checklist is the stale document.

**Unplaced is refused, not guessed.** A page counted against the wrong section
reconciles something nobody published.

Every page identifier and cycle below is a fixture.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from aeropub.aip import AipCoverage, HoldingState, SectionHolding, section
from aeropub.airac import AiracCycle
from aeropub.checklist import (
    AmendmentRecord,
    Checklist,
    ChecklistEntry,
    PageStatus,
    SupplementRecord,
    SupplementStatus,
    checklist_template,
    load_checklist,
    reconcile,
    sequence_gaps,
)
from aeropub.manifest import ManifestError
from aeropub.provenance import SourceRef

NOW = datetime(2026, 10, 5, 6, 0, tzinfo=timezone.utc)
READ_AT = "2026-09-01T12:00:00Z"
CYCLE = AiracCycle.from_identifier("2610")
EARLIER = CYCLE.previous
LATER = CYCLE.next


def ref(**overrides) -> SourceRef:
    fields = dict(
        source_id="TEST",
        document="test fixture — not a real publication",
        locator="GEN 0.4",
        retrieved_at=NOW,
        content_hash="b" * 64,
        parser_id="test",
        parser_version="0.1.0",
    )
    fields.update(overrides)
    return SourceRef(**fields)


def entry(page: str, **overrides) -> ChecklistEntry:
    fields = dict(page=page, source=ref(), cycle=CYCLE)
    fields.update(overrides)
    return ChecklistEntry(**fields)


def checklist(*entries: ChecklistEntry, **overrides) -> Checklist:
    fields = dict(
        entity="AA",
        source=ref(),
        published_for=CYCLE,
        entries=entries or (entry("ENR 3.1-5"), entry("GEN 1.7-2")),
    )
    fields.update(overrides)
    return Checklist(**fields)


def holding(code: str, state: HoldingState, **overrides) -> SectionHolding:
    fields = dict(
        section=section(code),
        entity="AA",
        state=state,
        cycle=CYCLE,
    )
    if state is HoldingState.HELD:
        fields["source"] = ref(locator=code)
    fields.update(overrides)
    return SectionHolding(**fields)


def coverage(*holdings: SectionHolding) -> AipCoverage:
    return AipCoverage(holdings)


# --------------------------------------------------------------------------
# Placing a page
# --------------------------------------------------------------------------


class TestPlacing:
    def test_a_page_identifier_places_itself_where_it_can(self):
        assert entry("ENR 3.1-5").section is not None
        assert entry("ENR 3.1-5").section.code == "ENR 3.1"

    def test_an_aerodrome_page_does_not_place_itself(self):
        """AD 2 pages number sequentially. The identifier does not say which
        subsection the page belongs to, and guessing reconciles something
        nobody published."""
        assert entry("AD 2 OTHH-13").section is None

    def test_an_explicit_section_is_used_when_the_identifier_cannot_place(self):
        assert entry("AD 2 OTHH-13", section_code="AD 2.12").section.code == "AD 2.12"

    def test_an_unplaced_page_is_reported_not_dropped(self):
        found = reconcile(checklist(entry("AD 2 OTHH-13")), coverage())
        assert found.findings[0].status is PageStatus.UNPLACED
        assert "could not be placed" in found.findings[0].describe()

    def test_an_unplaced_page_stops_the_reconciliation_being_complete(self):
        """However many pages matched."""
        found = reconcile(
            checklist(entry("ENR 3.1-5"), entry("AD 2 OTHH-13")),
            coverage(holding("ENR 3.1", HoldingState.HELD)),
        )
        assert not found.is_reconciled

    def test_a_page_with_no_identifier_is_refused(self):
        with pytest.raises(ValueError, match="page"):
            ChecklistEntry(page="", source=ref())


# --------------------------------------------------------------------------
# What we hold against what is listed
# --------------------------------------------------------------------------


class TestReconciliation:
    def test_a_page_held_at_the_listed_cycle_is_current(self):
        found = reconcile(
            checklist(entry("ENR 3.1-5")),
            coverage(holding("ENR 3.1", HoldingState.HELD)),
        )
        assert found.findings[0].status is PageStatus.CURRENT

    def test_a_page_held_at_an_older_cycle_is_stale(self):
        """The one that renders and answers and is wrong."""
        found = reconcile(
            checklist(entry("ENR 3.1-5")),
            coverage(holding("ENR 3.1", HoldingState.HELD, cycle=EARLIER)),
        )
        finding = found.findings[0]
        assert finding.status is PageStatus.STALE
        assert "every answer is out of date" in finding.describe()

    def test_a_page_held_at_a_newer_cycle_blames_the_checklist(self):
        found = reconcile(
            checklist(entry("ENR 3.1-5")),
            coverage(holding("ENR 3.1", HoldingState.HELD, cycle=LATER)),
        )
        finding = found.findings[0]
        assert finding.status is PageStatus.AHEAD
        assert "checklist is the stale document" in finding.describe()

    def test_a_page_held_with_no_cycle_cannot_be_shown_current(self):
        found = reconcile(
            checklist(entry("ENR 3.1-5")),
            coverage(holding("ENR 3.1", HoldingState.HELD, cycle=None)),
        )
        assert found.findings[0].status is PageStatus.STALE
        assert "no cycle recorded" in found.findings[0].detail

    def test_a_page_nobody_fetched_is_missing(self):
        found = reconcile(checklist(entry("ENR 3.1-5")), coverage())
        assert found.findings[0].status is PageStatus.MISSING

    def test_a_page_we_failed_on_is_kept_apart_from_one_we_never_tried(self):
        """The remedy differs."""
        found = reconcile(
            checklist(entry("ENR 3.1-5")),
            coverage(holding("ENR 3.1", HoldingState.FAILED)),
        )
        assert found.findings[0].status is PageStatus.UNREADABLE

    def test_our_absence_claim_is_contradicted_by_the_states_own_list(self):
        """An absence closes the question, so a wrong one is worse than a
        gap."""
        found = reconcile(
            checklist(entry("ENR 3.1-5")),
            coverage(
                holding(
                    "ENR 3.1",
                    HoldingState.ABSENT,
                    detail="the contents page does not list it",
                )
            ),
        )
        finding = found.findings[0]
        assert finding.status is PageStatus.CONTRADICTED
        assert "the State's own checklist lists it" in finding.describe()
        assert "contents page" in finding.describe()

    def test_every_listed_page_produces_a_finding_including_the_matches(self):
        """A report of discrepancies alone would get shorter as coverage got
        worse, and the count of pages checked is the point."""
        found = reconcile(
            checklist(entry("ENR 3.1-5"), entry("GEN 1.7-2")),
            coverage(holding("ENR 3.1", HoldingState.HELD)),
        )
        assert len(found.findings) == 2
        assert found.counts()["listed"] == 2

    def test_a_checklist_with_no_cycle_on_a_page_does_not_call_it_stale(self):
        """Nothing to compare against is not a comparison that failed."""
        found = reconcile(
            checklist(entry("ENR 3.1-5", cycle=None)),
            coverage(holding("ENR 3.1", HoldingState.HELD, cycle=EARLIER)),
        )
        assert found.findings[0].status is PageStatus.CURRENT


class TestUnlisted:
    def test_a_section_we_hold_and_the_state_does_not_list_is_surfaced(self):
        """Withdrawn, mis-keyed, or the checklist is incomplete — and until
        somebody says which, we are briefing from it."""
        found = reconcile(
            checklist(entry("ENR 3.1-5")),
            coverage(
                holding("ENR 3.1", HoldingState.HELD),
                holding("ENR 3.2", HoldingState.HELD),
            ),
        )
        assert found.unlisted == ("ENR 3.2",)
        assert not found.is_reconciled

    def test_another_entitys_holdings_are_not_counted_against_this_checklist(self):
        found = reconcile(
            checklist(entry("ENR 3.1-5")),
            coverage(
                holding("ENR 3.1", HoldingState.HELD),
                holding("ENR 3.2", HoldingState.HELD, entity="BB"),
            ),
        )
        assert found.unlisted == ()

    def test_a_section_we_only_failed_on_is_not_called_unlisted(self):
        """We are not briefing from something we could not read."""
        found = reconcile(
            checklist(entry("ENR 3.1-5")),
            coverage(
                holding("ENR 3.1", HoldingState.HELD),
                holding("ENR 3.2", HoldingState.FAILED),
            ),
        )
        assert found.unlisted == ()

    def test_everything_matching_reconciles(self):
        found = reconcile(
            checklist(entry("ENR 3.1-5")),
            coverage(holding("ENR 3.1", HoldingState.HELD)),
        )
        assert found.is_reconciled


# --------------------------------------------------------------------------
# The amendment sequence
# --------------------------------------------------------------------------


def amendment(identifier: str, **overrides) -> AmendmentRecord:
    fields = dict(identifier=identifier, source=ref())
    fields.update(overrides)
    return AmendmentRecord(**fields)


class TestAmendments:
    def test_a_skipped_number_is_a_gap(self):
        gaps = sequence_gaps(
            [amendment("01/26"), amendment("02/26"), amendment("04/26")]
        )
        assert [g.identifier for g in gaps] == ["03/26"]

    def test_a_contiguous_record_has_no_gaps(self):
        assert sequence_gaps([amendment("01/26"), amendment("02/26")]) == ()

    def test_the_end_of_the_record_is_not_a_gap(self):
        """A record stopping at 04/26 in June says nothing about 05/26 not yet
        issued, and reporting it would produce a finding every cycle that
        means nothing."""
        assert sequence_gaps([amendment("04/26")]) == ()

    def test_years_are_counted_separately(self):
        gaps = sequence_gaps(
            [amendment("12/25"), amendment("01/26"), amendment("03/26")]
        )
        assert [g.identifier for g in gaps] == ["02/26"]

    def test_a_four_digit_year_reads_the_same(self):
        assert amendment("3/2026").parsed == (2026, 3)

    def test_a_reference_in_an_unexpected_shape_is_skipped_not_guessed(self):
        assert amendment("SPECIAL EDITION").parsed is None
        assert sequence_gaps([amendment("SPECIAL EDITION")]) == ()

    def test_the_gaps_reach_the_reconciliation(self):
        found = reconcile(
            checklist(
                entry("ENR 3.1-5"),
                amendments=(amendment("01/26"), amendment("03/26")),
            ),
            coverage(holding("ENR 3.1", HoldingState.HELD)),
        )
        assert len(found.amendment_gaps) == 1
        assert "02/26" in found.render()


# --------------------------------------------------------------------------
# Supplements
# --------------------------------------------------------------------------


def supplement(identifier: str) -> SupplementRecord:
    return SupplementRecord(identifier=identifier, source=ref())


class TestSupplements:
    def test_one_in_force_and_never_received_is_a_finding(self):
        found = reconcile(
            checklist(entry("ENR 3.1-5"), supplements=(supplement("A05/26"),)),
            coverage(holding("ENR 3.1", HoldingState.HELD)),
        )
        assert found.supplements[0].status is SupplementStatus.NOT_HELD
        assert "looks exactly like one never issued" in found.supplements[0].describe()

    def test_one_we_hold_and_in_force_needs_nothing(self):
        found = reconcile(
            checklist(entry("ENR 3.1-5"), supplements=(supplement("A05/26"),)),
            coverage(holding("ENR 3.1", HoldingState.HELD)),
            held_supplements=["A05/26"],
        )
        assert found.supplements[0].status is SupplementStatus.HELD
        assert not found.supplements[0].status.needs_action

    def test_one_we_hold_that_has_dropped_off_the_record_is_withdrawn(self):
        """Still citable and no longer in force, which is the dangerous
        direction."""
        found = reconcile(
            checklist(entry("ENR 3.1-5")),
            coverage(holding("ENR 3.1", HoldingState.HELD)),
            held_supplements=["A01/26"],
        )
        assert found.supplements[0].status is SupplementStatus.WITHDRAWN
        assert "no longer in force" in found.supplements[0].describe()


# --------------------------------------------------------------------------
# The report
# --------------------------------------------------------------------------


class TestRender:
    def test_the_page_count_and_the_gap_count_lead(self):
        page = reconcile(
            checklist(entry("ENR 3.1-5"), entry("GEN 1.7-2")),
            coverage(holding("ENR 3.1", HoldingState.HELD)),
        ).render()
        assert "2 pages listed" in page
        assert "1 current" in page

    def test_a_contradiction_is_stated_as_ours(self):
        page = reconcile(
            checklist(entry("ENR 3.1-5")),
            coverage(
                holding("ENR 3.1", HoldingState.ABSENT, detail="not on the contents page")
            ),
        ).render()
        assert "WE SAID THE STATE DOES NOT PUBLISH THESE" in page

    def test_the_checklist_cycle_is_named(self):
        assert f"AIRAC {CYCLE.identifier}" in reconcile(
            checklist(), coverage()
        ).render()

    def test_a_checklist_with_no_entity_is_refused(self):
        with pytest.raises(ValueError, match="entity"):
            Checklist(entity="", source=ref())


# --------------------------------------------------------------------------
# Reading a manifest
# --------------------------------------------------------------------------


@pytest.fixture
def document(tmp_path: Path) -> Path:
    path = tmp_path / "gen04.txt"
    path.write_text(
        "a checklist of AIP pages, standing in for one somebody read\n",
        encoding="utf-8",
    )
    return path


def write(tmp_path: Path, payload: dict) -> Path:
    path = tmp_path / "gen04.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def manifest(**overrides) -> dict:
    payload = {
        "source": {
            "source_id": "EXAMPLE",
            "document": "AIP AA GEN 0.4",
            "document_path": "gen04.txt",
            "retrieved_at": READ_AT,
        },
        "entity": "AA",
        "published_for": "2610",
        "pages": [
            {"page": "ENR 3.1-5", "cycle": "2610", "locator": "line 41"},
            {"page": "GEN 1.7-2", "cycle": "2609", "locator": "line 12"},
        ],
        "amendments": [
            {"identifier": "01/26", "held": True, "locator": "GEN 0.2 row 1"},
            {"identifier": "03/26", "locator": "GEN 0.2 row 3"},
        ],
        "supplements": [
            {"identifier": "A05/26", "effective": "2026-09-10", "locator": "GEN 0.3"}
        ],
    }
    payload.update(overrides)
    return payload


class TestLoading:
    def test_a_checklist_loads_with_every_line_cited(self, tmp_path, document):
        held = load_checklist(write(tmp_path, manifest()))
        assert held.entity == "AA"
        assert held.entries[0].source.locator == "line 41"
        assert held.entries[0].cycle == AiracCycle.from_identifier("2610")

    def test_the_amendments_and_supplements_come_with_it(self, tmp_path, document):
        held = load_checklist(write(tmp_path, manifest()))
        assert len(held.amendments) == 2
        assert held.supplements[0].identifier == "A05/26"

    def test_a_page_without_a_locator_is_refused(self, tmp_path, document):
        payload = manifest()
        del payload["pages"][0]["locator"]
        with pytest.raises(ManifestError, match="locator"):
            load_checklist(write(tmp_path, payload))

    def test_a_cycle_that_is_not_a_cycle_is_refused(self, tmp_path, document):
        payload = manifest()
        payload["pages"][0]["cycle"] = "next one"
        with pytest.raises(ManifestError, match="cycle"):
            load_checklist(write(tmp_path, payload))

    def test_a_checklist_without_an_entity_is_refused(self, tmp_path, document):
        payload = manifest()
        del payload["entity"]
        with pytest.raises(ManifestError, match="entity"):
            load_checklist(write(tmp_path, payload))

    def test_the_sections_it_places_are_listed(self, tmp_path, document):
        held = load_checklist(write(tmp_path, manifest()))
        assert held.sections == ("ENR 3.1", "GEN 1.7")

    def test_a_loaded_checklist_reconciles(self, tmp_path, document):
        held = load_checklist(write(tmp_path, manifest()))
        found = reconcile(
            held,
            coverage(
                holding("ENR 3.1", HoldingState.HELD),
                holding("GEN 1.7", HoldingState.HELD, cycle=EARLIER),
            ),
        )
        statuses = {f.section_code: f.status for f in found.findings}
        assert statuses["ENR 3.1"] is PageStatus.CURRENT
        assert statuses["GEN 1.7"] is PageStatus.CURRENT

    def test_the_template_round_trips_as_json(self):
        blank = json.loads(checklist_template())
        assert blank["pages"][0]["cycle"] == ""
        assert blank["supplements"][0]["identifier"] == ""

"""Facts read this cycle, into the store that keeps them.

Most of what matters the store already enforces. What this has to get right is
what happens to last cycle's reading of the same document — and in particular
what must *not* happen to a NOTAM sitting above it.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from aeropub.facts import Fact, Precedence
from aeropub.live import DocumentLink, Retention
from aeropub.provenance import Confidence, SourceRef
from aeropub.publication import Edition, EditionStatus, Kind, Publication
from aeropub.reader import ReadResult
from aeropub.record import Recorded, record_cycle, record_result
from aeropub.store import SqliteFactStore

NOW = datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc)
LATER = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)
EDITION = Edition(index_url="https://aim.gov.qa/i.html", status=EditionStatus.CURRENT)

SECTION = Publication(
    url="https://aim.gov.qa/eAIP/QA-AD-2-OTHH-en-GB.html",
    kind=Kind.AIP_SECTION, edition=EDITION, code="AD 2 OTHH",
)


def a_source(document: str, precedence_note: str = "") -> SourceRef:
    return SourceRef(
        source_id="QA-CAA", document=document, locator="table 1",
        retrieved_at=NOW, content_hash="a" * 64,
        parser_id="test", parser_version="1",
    )


def a_fact(value, *, document: str, precedence=Precedence.AIP) -> Fact:
    return Fact(
        entity="OTHH", attribute="runway.16L.length_m", value=value,
        valid_from=date(2026, 8, 6), precedence=precedence,
        source=a_source(document),
    )


def a_result(
    facts=(), *, parsed=True, document="AIP Qatar AD 2 OTHH",
    confidence=Confidence.HIGH, retention=Retention.ARCHIVED,
) -> ReadResult:
    return ReadResult(
        publication=SECTION,
        link=DocumentLink(
            url=SECTION.url, document=document, media_type="text/html",
            retrieved_at=NOW, retention=retention,
            archive_key="sha256:abc" if retention is not Retention.LINKED else None,
        ),
        facts=tuple(facts), parsed=parsed, confidence=confidence,
    )


@pytest.fixture()
def store(tmp_path):
    made = SqliteFactStore(tmp_path / "facts.db")
    yield made
    made.close()


class TestWritingWhatWasRead:

    def test_facts_land_in_the_store(self, store):
        record_result(store, a_result([a_fact(4850.0, document="AIP Qatar AD 2 OTHH")]), at=NOW)
        assert len(store) == 1

    def test_the_count_is_reported(self, store):
        written = record_result(
            store,
            a_result([a_fact(4850.0, document="AIP Qatar AD 2 OTHH"),
                      a_fact(4851.0, document="AIP Qatar AD 2 OTHH")]),
            at=NOW,
        )
        assert (written.documents, written.facts) == (1, 2)

    def test_a_re_read_closes_the_previous_reading(self, store):
        record_result(store, a_result([a_fact(4850.0, document="AIP Qatar AD 2 OTHH")]), at=NOW)
        written = record_result(
            store, a_result([a_fact(4900.0, document="AIP Qatar AD 2 OTHH")]), at=LATER
        )
        assert written.superseded == 1
        assert store.effective("OTHH", "runway.16L.length_m", date(2026, 9, 10)).value == 4900.0

    def test_the_old_row_is_kept_not_deleted(self, store):
        """What we believed, and when we stopped, is the audit answer."""
        record_result(store, a_result([a_fact(4850.0, document="AIP Qatar AD 2 OTHH")]), at=NOW)
        record_result(store, a_result([a_fact(4900.0, document="AIP Qatar AD 2 OTHH")]), at=LATER)
        assert len(store) == 2


class TestANotamIsNotClosedByAReRead:
    """The reason this module exists rather than a two-line call.

    A NOTAM sits above the AIP in Precedence precisely so it overrides it.
    Closing it because the layer beneath was refetched takes a restriction
    that is still in force off an operator's screen.
    """

    def _with_both(self, store):
        store.add(a_fact(4850.0, document="AIP Qatar AD 2 OTHH"))
        store.add(
            a_fact(2500.0, document="NOTAM A1234/26", precedence=Precedence.NOTAM)
        )

    def test_the_notam_survives(self, store):
        self._with_both(store)
        record_result(
            store, a_result([a_fact(4900.0, document="AIP Qatar AD 2 OTHH")]), at=LATER
        )
        live = [f for f in store if f.source.document == "NOTAM A1234/26"]
        assert live and live[0].superseded_at is None

    def test_the_notam_still_wins(self, store):
        """The restriction is what an operator must see."""
        self._with_both(store)
        record_result(
            store, a_result([a_fact(4900.0, document="AIP Qatar AD 2 OTHH")]), at=LATER
        )
        current = store.effective("OTHH", "runway.16L.length_m", date(2026, 9, 10))
        assert current.value == 2500.0
        assert current.precedence is Precedence.NOTAM

    def test_only_the_re_read_document_is_closed(self, store):
        self._with_both(store)
        written = record_result(
            store, a_result([a_fact(4900.0, document="AIP Qatar AD 2 OTHH")]), at=LATER
        )
        assert written.superseded == 1

    def test_a_supersede_with_no_document_is_refused(self, store):
        """An empty document would match every row that never recorded one."""
        with pytest.raises(ValueError, match="needs a document"):
            store.supersede_document("  ", LATER)


class TestAbsenceOfAParserIsNotEvidence:

    def test_an_unparsed_document_writes_nothing(self, store):
        record_result(store, a_result(parsed=False), at=NOW)
        assert len(store) == 0

    def test_it_supersedes_nothing_either(self, store):
        """Not having read a page is not evidence the old values are wrong."""
        store.add(a_fact(4850.0, document="AIP Qatar AD 2 OTHH"))
        written = record_result(store, a_result(parsed=False), at=LATER)
        assert written.superseded == 0
        assert store.effective("OTHH", "runway.16L.length_m", date(2026, 9, 10)).value == 4850.0

    def test_it_is_counted_separately_from_finding_nothing(self, store):
        """One is a parser we have not written; the other is an empty page."""
        no_parser = record_result(store, a_result(parsed=False), at=NOW)
        found_nothing = record_result(store, a_result([], parsed=True), at=NOW)
        assert no_parser.skipped_unparsed == 1
        assert found_nothing.skipped_unparsed == 0
        assert found_nothing.facts == 0

    def test_parsing_and_finding_nothing_still_closes_the_old_reading(self, store):
        """The page was read. What it used to say is no longer what it says."""
        record_result(store, a_result([a_fact(4850.0, document="AIP Qatar AD 2 OTHH")]), at=NOW)
        written = record_result(store, a_result([], parsed=True), at=LATER)
        assert written.superseded == 1


class TestAnUnresolvableCitationIsVisible:

    def test_low_confidence_facts_are_still_written(self, store):
        """Refusing them would deny a State's data over a fault of ours."""
        written = record_result(
            store,
            a_result([a_fact(4850.0, document="AIP Qatar AD 2 OTHH")],
                     confidence=Confidence.LOW, retention=Retention.LINKED),
            at=NOW,
        )
        assert written.facts == 1
        assert len(store) == 1

    def test_but_they_are_counted(self, store):
        written = record_result(
            store,
            a_result([a_fact(4850.0, document="AIP Qatar AD 2 OTHH")],
                     confidence=Confidence.LOW, retention=Retention.LINKED),
            at=NOW,
        )
        assert written.unresolvable == 1

    def test_an_archived_reading_is_not_counted(self, store):
        written = record_result(
            store, a_result([a_fact(4850.0, document="AIP Qatar AD 2 OTHH")]), at=NOW
        )
        assert written.unresolvable == 0

    def test_the_report_says_so(self):
        text = Recorded(documents=1, facts=3, unresolvable=3).describe()
        assert "will not resolve" in text


class TestRecordingAWholeCycle:

    def test_only_documents_that_were_read(self, store):
        """An unchanged document's facts are already in the store. Rewriting
        them replaces a row recorded when the value arrived with one recorded
        today, losing the date an investigation asks for."""
        from aeropub.cycle import CycleReport, DocumentOutcome, Outcome, StateOutcome

        read = DocumentOutcome(
            url=SECTION.url, outcome=Outcome.READ, code="AD 2 OTHH",
            result=a_result([a_fact(4850.0, document="AIP Qatar AD 2 OTHH")]),
        )
        unchanged = DocumentOutcome(
            url="https://aim.gov.qa/eAIP/QA-ENR-3.2-en-GB.html",
            outcome=Outcome.UNCHANGED, code="ENR 3.2",
            result=a_result([a_fact(1.0, document="AIP Qatar ENR 3.2")]),
        )
        report = CycleReport(
            at=NOW,
            states=(StateOutcome(state="OT", name="Qatar", edition=EDITION,
                                 documents=(read, unchanged)),),
        )
        written = record_cycle(store, report, at=NOW)
        assert written.documents == 1
        assert len(store) == 1

    def test_an_empty_cycle_writes_nothing(self, store):
        from aeropub.cycle import CycleReport

        assert record_cycle(store, CycleReport(at=NOW), at=NOW) == Recorded()

    def test_a_naive_timestamp_is_refused(self, store):
        with pytest.raises(ValueError, match="timezone-aware"):
            record_result(store, a_result([]), at=datetime(2026, 9, 9, 12, 0))

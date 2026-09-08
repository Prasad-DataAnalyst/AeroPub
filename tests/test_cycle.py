"""The cycle that does not stop.

Reading a State once is a demonstration. What is asserted here is the operation:
that a State going down, a page 500ing, a resolver raising, or a State moving
its whole layout produces a recorded outcome and the cycle carries on — and
that none of those is ever reported as a State that is up to date.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from aeropub.cycle import (
    Cycle,
    CycleReport,
    InMemoryLedger,
    Outcome,
    StateOutcome,
)
from aeropub.publication import Edition, EditionStatus, Kind, Publication
from aeropub.reader import Retrieved

NOW = datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc)

CURRENT = Edition(
    index_url="https://a.test/current/index.html",
    status=EditionStatus.CURRENT,
    label="AIP 2nd Edition",
)
NEXT = Edition(
    index_url="https://a.test/next/index.html",
    status=EditionStatus.NEXT,
    label="AMDT 01/2026",
)


def section(host: str, code: str) -> Publication:
    return Publication(
        url=f"https://{host}/eAIP/{code.replace(' ', '-')}.html",
        kind=Kind.AIP_SECTION,
        edition=CURRENT,
        code=code,
    )


class FakeState:
    """A State that behaves however the test needs it to."""

    def __init__(self, state, name, *, codes=("ENR 3.2", "GEN 0.4"),
                 editions=(CURRENT, NEXT), raises=None, host=None):
        self.state, self.name = state, name
        self.entry_point = f"https://{host or state.lower()}.test/history.html"
        self._codes = codes
        self._editions = editions
        self._raises = raises
        self._host = host or f"{state.lower()}.test"

    def editions(self, read):
        if self._raises:
            raise self._raises
        return self._editions

    def publications(self, edition, read):
        return tuple(section(self._host, c) for c in self._codes)


def body_for(url: str) -> bytes:
    return f"<html><body>{url}</body></html>".encode()


def retriever(*, fails: set[str] = frozenset(), bodies=None):
    def _retrieve(url: str) -> Retrieved:
        if url in fails:
            raise ConnectionError(f"{url} refused the connection")
        body = (bodies or {}).get(url, body_for(url))
        return Retrieved(
            url=url, body=body, media_type="text/html",
            type_was_declared=True, retrieved_at=NOW,
        )
    return _retrieve


def archiver():
    import hashlib
    def _keep(body: bytes, media_type: str) -> str:
        return "sha256:" + hashlib.sha256(body).hexdigest()[:16]
    return _keep


class TestNothingStopsIt:
    """The guarantee. Every one of these would end a naive loop."""

    def test_a_state_that_raises_does_not_stop_the_others(self):
        cycle = Cycle(
            resolvers=(
                FakeState("OT", "Qatar"),
                FakeState("OE", "Saudi Arabia", raises=ConnectionError("host down")),
                FakeState("EG", "United Kingdom"),
            ),
            retrieve=retriever(), keep=archiver(),
        )
        report = cycle.run(now=NOW)
        assert len(report.states) == 3
        assert len(report.complete) == 2
        assert len(report.unreached) == 1

    def test_the_failure_is_recorded_not_swallowed(self):
        cycle = Cycle(
            resolvers=(FakeState("OE", "Saudi", raises=ValueError("layout moved")),),
            retrieve=retriever(), keep=archiver(),
        )
        [outcome] = cycle.run(now=NOW).states
        assert not outcome.reached
        assert "layout moved" in outcome.failed_because

    def test_one_page_failing_does_not_stop_its_state(self):
        bad = "https://ot.test/eAIP/ENR-3.2.html"
        cycle = Cycle(
            resolvers=(FakeState("OT", "Qatar", host="ot.test"),),
            retrieve=retriever(fails={bad}), keep=archiver(),
        )
        [outcome] = cycle.run(now=NOW).states
        assert outcome.reached
        assert len(outcome.failed) == 1
        assert len(outcome.read) == 1

    def test_run_never_raises_whatever_happens(self):
        """Anything escaping as an exception is a defect here, not a caller's
        condition to handle."""
        class Hostile:
            state = "XX"
            name = "Hostile"
            entry_point = "https://x.test/"
            def editions(self, read):
                raise RuntimeError("boom")
            def publications(self, edition, read):
                raise RuntimeError("boom")

        report = Cycle(
            resolvers=(Hostile(),), retrieve=retriever(), keep=archiver()
        ).run(now=NOW)
        assert isinstance(report, CycleReport)

    def test_a_resolver_missing_its_attributes_is_survived(self):
        class Broken:
            def editions(self, read):
                raise AttributeError("no")
            def publications(self, edition, read):
                return ()

        report = Cycle(
            resolvers=(Broken(),), retrieve=retriever(), keep=archiver()
        ).run(now=NOW)
        assert report.states[0].state == "??"


class TestSilenceIsNeverSuccess:

    def test_a_state_with_a_failed_page_is_not_complete(self):
        """79 of 80 read is not a State that is up to date."""
        cycle = Cycle(
            resolvers=(FakeState("OT", "Qatar", host="ot.test"),),
            retrieve=retriever(fails={"https://ot.test/eAIP/GEN-0.4.html"}),
            keep=archiver(),
        )
        [outcome] = cycle.run(now=NOW).states
        assert not outcome.is_complete

    def test_failure_leaves_data_stale_not_current(self):
        cycle = Cycle(
            resolvers=(FakeState("OT", "Qatar", host="ot.test"),),
            retrieve=retriever(fails={"https://ot.test/eAIP/GEN-0.4.html"}),
            keep=archiver(),
        )
        [outcome] = cycle.run(now=NOW).states
        assert outcome.failed[0].leaves_data_stale

    def test_the_report_separates_the_four_counts(self):
        cycle = Cycle(
            resolvers=(
                FakeState("OT", "Qatar", host="ot.test"),
                FakeState("OE", "Saudi", raises=ConnectionError("down")),
            ),
            retrieve=retriever(fails={"https://ot.test/eAIP/GEN-0.4.html"}),
            keep=archiver(),
        )
        text = cycle.run(now=NOW).describe()
        assert "NOT REACHED" in text
        assert "stale, not current" in text

    def test_an_unreached_state_has_unknown_documents_not_none(self):
        """An empty documents tuple on an unreached State means 'we do not
        know', and is_complete must not read it as 'nothing to do'."""
        cycle = Cycle(
            resolvers=(FakeState("OE", "Saudi", raises=ConnectionError("down")),),
            retrieve=retriever(), keep=archiver(),
        )
        [outcome] = cycle.run(now=NOW).states
        assert outcome.documents == ()
        assert not outcome.is_complete


class TestItResumesRatherThanRestarts:

    def test_a_second_cycle_finds_everything_unchanged(self):
        ledger = InMemoryLedger()
        cycle = Cycle(
            resolvers=(FakeState("OT", "Qatar", host="ot.test"),),
            retrieve=retriever(), ledger=ledger, keep=archiver(),
        )
        first = cycle.run(now=NOW)
        second = cycle.run(now=NOW)
        assert len(first.states[0].read) == 2
        assert len(second.states[0].unchanged) == 2
        assert second.states[0].is_complete

    def test_a_changed_document_is_read_again(self):
        ledger = InMemoryLedger()
        url = "https://ot.test/eAIP/ENR-3.2.html"
        cycle = Cycle(
            resolvers=(FakeState("OT", "Qatar", host="ot.test"),),
            retrieve=retriever(), ledger=ledger, keep=archiver(),
        )
        cycle.run(now=NOW)
        cycle.retrieve = retriever(bodies={url: b"<html>Qatar changed ENR 3.2</html>"})
        second = cycle.run(now=NOW)
        assert [d.code for d in second.states[0].read] == ["ENR 3.2"]

    def test_an_unchanged_cycle_is_quiet(self):
        ledger = InMemoryLedger()
        cycle = Cycle(
            resolvers=(FakeState("OT", "Qatar", host="ot.test"),),
            retrieve=retriever(), ledger=ledger, keep=archiver(),
        )
        cycle.run(now=NOW)
        assert cycle.run(now=NOW).quiet

    def test_a_failed_document_is_not_recorded_as_seen(self):
        """Recording it would make the next cycle skip a page it never read."""
        ledger = InMemoryLedger()
        bad = "https://ot.test/eAIP/ENR-3.2.html"
        Cycle(
            resolvers=(FakeState("OT", "Qatar", host="ot.test"),),
            retrieve=retriever(fails={bad}), ledger=ledger, keep=archiver(),
        ).run(now=NOW)
        assert ledger.hash_for(bad) is None


class TestRepeatedFailureIsVisible:
    """A State failing once is weather. Failing every cycle for a fortnight is
    a layout change nobody noticed, and both look the same from inside one
    cycle."""

    def test_failures_accumulate(self):
        ledger = InMemoryLedger()
        cycle = Cycle(
            resolvers=(FakeState("OE", "Saudi", raises=ConnectionError("down")),),
            retrieve=retriever(), ledger=ledger, keep=archiver(),
        )
        for _ in range(3):
            report = cycle.run(now=NOW)
        assert report.states[0].consecutive_failures == 3

    def test_a_fortnight_is_persistent_not_weather(self):
        ledger = InMemoryLedger()
        cycle = Cycle(
            resolvers=(FakeState("OE", "Saudi", raises=ConnectionError("down")),),
            retrieve=retriever(), ledger=ledger, keep=archiver(),
        )
        for _ in range(14):
            report = cycle.run(now=NOW)
        assert report.states[0].is_persistently_failing
        assert "PERSISTENTLY FAILING" in report.describe()

    def test_a_recovery_clears_the_count(self):
        ledger = InMemoryLedger()
        failing = Cycle(
            resolvers=(FakeState("OE", "Saudi", raises=ConnectionError("down")),),
            retrieve=retriever(), ledger=ledger, keep=archiver(),
        )
        failing.run(now=NOW)
        failing.run(now=NOW)
        recovered = Cycle(
            resolvers=(FakeState("OE", "Saudi"),),
            retrieve=retriever(), ledger=ledger, keep=archiver(),
        )
        assert recovered.run(now=NOW).states[0].consecutive_failures == 0


class TestWhichEditionIsRead:

    def test_the_declared_current_one(self):
        cycle = Cycle(
            resolvers=(FakeState("OT", "Qatar"),), retrieve=retriever(), keep=archiver()
        )
        assert cycle.run(now=NOW).states[0].edition.status is EditionStatus.CURRENT

    def test_an_undeclared_edition_is_not_promoted(self):
        """Reading next cycle's AIP as though it were in force is the failure
        this whole model exists to avoid, so no edition is better than a
        guessed one."""
        undeclared = Edition(index_url="https://a.test/i.html")
        cycle = Cycle(
            resolvers=(FakeState("OT", "Qatar", editions=(undeclared,)),),
            retrieve=retriever(), keep=archiver(),
        )
        [outcome] = cycle.run(now=NOW).states
        assert not outcome.reached
        assert "unknown status" in outcome.failed_because

    def test_a_caller_planning_ahead_can_take_next(self):
        cycle = Cycle(
            resolvers=(FakeState("OT", "Qatar"),),
            retrieve=retriever(), keep=archiver(),
            choose_edition=lambda eds: next(
                (e for e in eds if e.status is EditionStatus.NEXT), None
            ),
        )
        assert cycle.run(now=NOW).states[0].edition.status is EditionStatus.NEXT


class TestOneStateCannotConsumeTheCycle:

    def test_the_budget_bounds_a_state(self):
        cycle = Cycle(
            resolvers=(FakeState("OT", "Qatar", codes=("ENR 3.2", "GEN 0.4", "AD 1.1")),),
            retrieve=retriever(), keep=archiver(), budget=2,
        )
        [outcome] = cycle.run(now=NOW).states
        assert len(outcome.read) == 2
        assert len(outcome.not_attempted) == 1

    def test_what_it_displaced_is_not_reported_as_unchanged(self):
        """The quietest possible lie: a page nobody looked at, counted among
        the ones confirmed identical."""
        cycle = Cycle(
            resolvers=(FakeState("OT", "Qatar", codes=("ENR 3.2", "GEN 0.4", "AD 1.1")),),
            retrieve=retriever(), keep=archiver(), budget=2,
        )
        [outcome] = cycle.run(now=NOW).states
        assert outcome.not_attempted[0].outcome is Outcome.NOT_ATTEMPTED
        assert outcome.not_attempted[0].leaves_data_stale
        assert not outcome.is_complete

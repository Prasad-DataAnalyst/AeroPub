"""The loop that keeps running, and how often it asks.

Both wrong answers are expensive. Check too rarely and an amendment sits
unread for days after it published. Check too often and a State's AIM server
sees enough traffic from one address to block it, which turns into a silent
coverage gap — the worst failure this system has.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from aeropub.airac import AiracCycle
from aeropub.cycle import Cycle, InMemoryLedger, Outcome
from aeropub.publication import Edition, EditionStatus, Kind, Publication
from aeropub.reader import Retrieved
from aeropub.serve import QUIET_INTERVAL, Loop, interval_at
from aeropub.watcher import WINDOW_INTERVAL

NOW = datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc)
CURRENT = Edition(
    index_url="https://a.test/i.html", status=EditionStatus.CURRENT, label="AIP 29"
)


class FakeState:
    state, name, entry_point = "OT", "Qatar", "https://a.test/h.html"

    def __init__(self, codes=("ENR 3.2", "GEN 0.4")):
        self._codes = codes

    def editions(self, read):
        return (CURRENT,)

    def publications(self, edition, read):
        return tuple(
            Publication(
                url=f"https://a.test/eAIP/{c.replace(' ', '-')}.html",
                kind=Kind.AIP_SECTION, edition=edition, code=c,
            )
            for c in self._codes
        )


def retriever(bodies=None):
    def _retrieve(url: str) -> Retrieved:
        body = (bodies or {}).get(url, f"<html>{url}</html>".encode())
        return Retrieved(url=url, body=body, media_type="text/html",
                         type_was_declared=True, retrieved_at=NOW)
    return _retrieve


def a_cycle(state=None, ledger=None, retrieve=None):
    return Cycle(
        resolvers=(state or FakeState(),),
        retrieve=retrieve or retriever(),
        ledger=ledger or InMemoryLedger(),
        keep=lambda body, media_type: f"sha256:{len(body):08x}",
    )


class TestWhenToAsk:
    """Cadence follows the AIRAC calendar because publication does."""

    def test_a_publication_window_is_checked_often(self):
        cycle = AiracCycle.containing(date(2026, 9, 9)).next
        assert interval_at(cycle.distribution_deadline) == WINDOW_INTERVAL

    def test_the_quiet_stretch_is_not(self):
        cycle = AiracCycle.containing(date(2026, 9, 9)).next
        quiet_day = cycle.distribution_deadline - timedelta(days=20)
        assert interval_at(quiet_day) == QUIET_INTERVAL

    def test_the_window_opens_before_the_deadline(self):
        """A State that publishes early must not be missed."""
        cycle = AiracCycle.containing(date(2026, 9, 9)).next
        assert interval_at(cycle.distribution_deadline - timedelta(days=4)) == (
            WINDOW_INTERVAL
        )

    def test_it_stays_open_after(self):
        """A deadline is a 'no later than'; publication clusters just after."""
        cycle = AiracCycle.containing(date(2026, 9, 9)).next
        assert interval_at(cycle.distribution_deadline + timedelta(days=5)) == (
            WINDOW_INTERVAL
        )

    def test_the_quiet_interval_is_not_a_day(self):
        """An AIP changing outside its window has changed unexpectedly, and a
        quarter of a day is soon enough to notice something nobody expected."""
        assert QUIET_INTERVAL <= timedelta(hours=8)


class TestMostPassesAreShallow:
    """A five-minute cadence re-checking eighty sections is three million
    requests a year per State. Across a hundred and eighty, seventeen a
    second, forever. No AIM office should absorb that."""

    def test_a_shallow_pass_presumes_what_the_ledger_holds(self):
        ledger = InMemoryLedger()
        cycle = a_cycle(ledger=ledger)
        cycle.run(now=NOW)
        report = cycle.run(now=NOW, deep=False)
        assert len(report.states[0].presumed) == 2
        assert not report.states[0].unchanged

    def test_a_shallow_pass_still_reads_what_it_has_never_seen(self):
        """A section appearing mid-edition is exactly what must not be missed."""
        ledger = InMemoryLedger()
        cycle = a_cycle(ledger=ledger)
        cycle.run(now=NOW)
        cycle.resolvers = (FakeState(codes=("ENR 3.2", "GEN 0.4", "ENR 5.1")),)
        report = cycle.run(now=NOW, deep=False)
        assert [d.code for d in report.states[0].read] == ["ENR 5.1"]

    def test_presumed_is_not_spelled_the_same_as_confirmed(self):
        ledger = InMemoryLedger()
        cycle = a_cycle(ledger=ledger)
        cycle.run(now=NOW)
        presumed = cycle.run(now=NOW, deep=False).states[0].documents[0]
        confirmed = cycle.run(now=NOW, deep=True).states[0].documents[0]
        assert presumed.outcome is Outcome.PRESUMED_UNCHANGED
        assert confirmed.outcome is Outcome.UNCHANGED
        assert not presumed.was_confirmed
        assert confirmed.was_confirmed

    def test_a_presumption_does_not_make_data_stale(self):
        """We hold it. We simply did not ask again this minute."""
        ledger = InMemoryLedger()
        cycle = a_cycle(ledger=ledger)
        cycle.run(now=NOW)
        [outcome] = cycle.run(now=NOW, deep=False).states
        assert not outcome.documents[0].leaves_data_stale
        assert outcome.is_complete

    def test_the_report_says_which_kind_of_pass_it_was(self):
        cycle = a_cycle()
        assert "shallow" in cycle.run(now=NOW, deep=False).describe()
        assert "shallow" not in cycle.run(now=NOW, deep=True).describe()


class TestTheLoop:

    def test_it_runs_until_bounded(self):
        waited: list[float] = []
        loop = Loop(cycle=a_cycle(), sleep=waited.append, now=lambda: NOW,
                    max_passes=3)
        report = loop.run_forever()
        assert report.passes == 3

    def test_the_first_pass_is_deep(self):
        """Nothing is known yet; presuming would presume about nothing."""
        seen: list = []
        loop = Loop(cycle=a_cycle(), sleep=lambda s: None, now=lambda: NOW,
                    on_report=seen.append, max_passes=1)
        loop.run_forever()
        assert seen[0].deep

    def test_later_passes_are_shallow_until_deep_every(self):
        seen: list = []
        loop = Loop(cycle=a_cycle(), sleep=lambda s: None, now=lambda: NOW,
                    on_report=seen.append, max_passes=4,
                    deep_every=timedelta(hours=24))
        loop.run_forever()
        assert [r.deep for r in seen] == [True, False, False, False]

    def test_a_deep_pass_comes_round_again(self):
        clock = [NOW]
        seen: list = []

        def advance(seconds):
            clock[0] = clock[0] + timedelta(seconds=seconds)

        loop = Loop(cycle=a_cycle(), sleep=advance, now=lambda: clock[0],
                    on_report=seen.append, max_passes=6,
                    deep_every=timedelta(hours=6))
        loop.run_forever()
        assert sum(1 for r in seen if r.deep) >= 2

    def test_it_waits_between_passes(self):
        waited: list[float] = []
        Loop(cycle=a_cycle(), sleep=waited.append, now=lambda: NOW,
             max_passes=2).run_forever()
        assert waited == [QUIET_INTERVAL.total_seconds()]

    def test_it_does_not_wait_after_the_last_pass(self):
        waited: list[float] = []
        Loop(cycle=a_cycle(), sleep=waited.append, now=lambda: NOW,
             max_passes=1).run_forever()
        assert waited == []

    def test_every_pass_is_reported_including_quiet_ones(self):
        """What is worth printing is the caller's decision, not this one's."""
        seen: list = []
        Loop(cycle=a_cycle(), sleep=lambda s: None, now=lambda: NOW,
             on_report=seen.append, max_passes=3).run_forever()
        assert len(seen) == 3


class TestStoppingCleanly:

    def test_stop_ends_the_loop(self):
        loop = Loop(cycle=a_cycle(), sleep=lambda s: None, now=lambda: NOW)

        def stop_after_one(report):
            loop.stop("test asked")

        loop.on_report = stop_after_one
        report = loop.run_forever()
        assert report.passes == 1
        assert report.stopped_because == "test asked"

    def test_a_pass_in_flight_is_never_interrupted(self):
        """A ledger half-written by a kill claims things it should not — the
        failure reconciliation exists to catch, and better not to cause."""
        ledger = InMemoryLedger()
        loop = Loop(cycle=a_cycle(ledger=ledger), sleep=lambda s: None,
                    now=lambda: NOW)
        loop.stop("before it began")
        loop.on_report = lambda r: None
        report = loop.run_forever()
        assert report.passes == 0
        assert ledger.hashes == {}

    def test_stopping_mid_run_finishes_the_pass_first(self):
        ledger = InMemoryLedger()
        loop = Loop(cycle=a_cycle(ledger=ledger), sleep=lambda s: None,
                    now=lambda: NOW)
        loop.on_report = lambda r: loop.stop("mid-run")
        loop.run_forever()
        assert len(ledger.hashes) == 2

    def test_it_does_not_sleep_after_being_asked_to_stop(self):
        waited: list[float] = []
        loop = Loop(cycle=a_cycle(), sleep=waited.append, now=lambda: NOW)
        loop.on_report = lambda r: loop.stop("now")
        loop.run_forever()
        assert waited == []


class TestItKeepsGoingThroughTrouble:

    def test_a_failing_state_does_not_end_the_loop(self):
        class Broken:
            state, name, entry_point = "OE", "Saudi", "https://b.test/"
            def editions(self, read):
                raise ConnectionError("host down")
            def publications(self, edition, read):
                return ()

        cycle = Cycle(resolvers=(Broken(),), retrieve=retriever(),
                      ledger=InMemoryLedger(), keep=lambda b, m: "sha256:x")
        report = Loop(cycle=cycle, sleep=lambda s: None, now=lambda: NOW,
                      max_passes=3).run_forever()
        assert report.passes == 3

    def test_the_counts_survive_a_failing_state(self):
        class Broken:
            state, name, entry_point = "OE", "Saudi", "https://b.test/"
            def editions(self, read):
                raise ConnectionError("down")
            def publications(self, edition, read):
                return ()

        cycle = Cycle(resolvers=(Broken(),), retrieve=retriever(),
                      ledger=InMemoryLedger(), keep=lambda b, m: "sha256:x")
        report = Loop(cycle=cycle, sleep=lambda s: None, now=lambda: NOW,
                      max_passes=2).run_forever()
        assert report.documents_read == 0
        assert report.quiet_passes == 0  # an unreached State is not quiet

"""Running 24/7 — the supervision, and the two ways it must never lie.

The tick is easy to get right on a good day and easy to get quietly wrong on a
bad one. Two failures are tested here harder than anything else, because both
produce a report that looks *better* as the system gets worse:

**Held back read as healthy.** A source we chose not to ask is not a source
that is fine. If restraint were expressed as absence from the report, a tick
that asked forty of a hundred sources would render like a quiet tick, and the
board would be green while sixty States went unwatched.

**Silence read as nothing happening.** A watcher that stops watching shows
yesterday's green indefinitely. So a gap between ticks is a stated fact with a
duration, not a missing row — and a source checked either side of it is not
covered in between.

Everything here runs on an injected clock. Nothing sleeps, nothing polls, and
the fetchers below answer without a network — the transport is a Protocol for
exactly this reason.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from aeropub.registry import (
    CheckOutcome,
    DetectionTier,
    Source,
    SourceFormat,
    SourceKind,
    SourceRegistry,
    SourceState,
)
from aeropub.runtime import (
    BLOCKED_COOLING,
    DEAD_MANS_TOLERANCE,
    MAX_COOLING,
    OPEN_AFTER_FAILURES,
    Breaker,
    BreakerState,
    Heartbeat,
    HostBudget,
    Outage,
    Restraint,
    Supervisor,
    host_of,
)
from aeropub.watcher import FetchResult, Watcher

NOW = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)
HASH = "a" * 64


def source(source_id: str, *, host: str = "ais.example.test", **overrides) -> Source:
    fields = dict(
        source_id=source_id,
        authority="XX",
        name=f"{source_id} index",
        kind=SourceKind.AIP,
        url=f"https://{host}/aip/{source_id}",
        fmt=SourceFormat.EAIP_HTML,
        tier=DetectionTier.ADAPTIVE_POLL,
    )
    fields.update(overrides)
    return Source(**fields)


def registry(*sources: Source) -> SourceRegistry:
    found = SourceRegistry()
    for one in sources:
        found.add(one)
    return found


class Unchanging:
    """A transport that always answers, always with the same content."""

    def __init__(self, content_hash: str = HASH) -> None:
        self.content_hash = content_hash
        self.calls: list[str] = []

    def fetch(self, source: Source, *, credential: str | None) -> FetchResult:
        self.calls.append(source.source_id)
        return FetchResult(ok=True, http_status=200, content_hash=self.content_hash)


class Refuses:
    """A transport that is being rate limited. The failure that must be loud."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def fetch(self, source: Source, *, credential: str | None) -> FetchResult:
        self.calls.append(source.source_id)
        return FetchResult(
            ok=False, http_status=429, blocked=True, error="rate limited"
        )


class Fails:
    """A transport that errors without being refused. A fault, not a ban."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def fetch(self, source: Source, *, credential: str | None) -> FetchResult:
        self.calls.append(source.source_id)
        return FetchResult(ok=False, error="connection reset")


def outcome(source_id: str, state: SourceState, *, at: datetime = NOW, **overrides):
    fields = dict(source_id=source_id, checked_at=at, state=state)
    fields.update(overrides)
    return CheckOutcome(**fields)


# --------------------------------------------------------------------------
# Politeness — spent per host, not per source
# --------------------------------------------------------------------------


class TestHostBudget:
    def test_the_budget_is_shared_across_sources_on_one_host(self):
        """Twelve rows in our registry are one machine at the other end."""
        budget = HostBudget(rate_per_minute=10, burst=3)
        assert [budget.spend("ais.example.test", NOW) for _ in range(4)] == [
            True, True, True, False
        ]

    def test_separate_hosts_have_separate_budgets(self):
        budget = HostBudget(rate_per_minute=10, burst=1)
        assert budget.spend("one.example.test", NOW)
        assert budget.spend("two.example.test", NOW)

    def test_tokens_refill_over_time(self):
        budget = HostBudget(rate_per_minute=10, burst=2)
        budget.spend("h", NOW)
        budget.spend("h", NOW)
        assert not budget.allows("h", NOW)
        assert budget.allows("h", NOW + timedelta(seconds=6))

    def test_refill_is_capped_at_the_burst(self):
        """A quiet week does not earn a right to flood on Monday."""
        budget = HostBudget(rate_per_minute=10, burst=2)
        budget.spend("h", NOW)
        assert budget.available("h", NOW + timedelta(days=7)) == 2.0

    def test_a_host_may_be_given_the_rate_it_agreed_to(self):
        """An agreed rate is evidence; a guessed one is not."""
        budget = HostBudget(rate_per_minute=1, overrides={"fast.example.test": 600})
        assert budget.rate_for("fast.example.test") == 600
        assert budget.rate_for("other.example.test") == 1

    def test_the_wait_is_stated_not_guessed(self):
        budget = HostBudget(rate_per_minute=60, burst=1)
        budget.spend("h", NOW)
        assert budget.wait_for("h", NOW) == timedelta(seconds=1)
        assert budget.wait_for("h", NOW + timedelta(seconds=1)) == timedelta(0)

    def test_the_host_is_the_netloc_not_the_scheme(self):
        assert host_of("https://AIS.Example.test/aip") == "ais.example.test"
        assert host_of("http://ais.example.test:8080/aip") == "ais.example.test:8080"


# --------------------------------------------------------------------------
# Health — per source
# --------------------------------------------------------------------------


class TestBreaker:
    def test_a_healthy_source_is_always_asked(self):
        breaker = Breaker(source_id="S")
        assert breaker.allows(NOW)
        assert breaker.state is BreakerState.CLOSED

    def test_it_takes_repeated_faults_to_open(self):
        breaker = Breaker(source_id="S")
        for _ in range(OPEN_AFTER_FAILURES - 1):
            breaker.record(outcome("S", SourceState.FETCH_FAILED))
        assert breaker.allows(NOW)
        breaker.record(outcome("S", SourceState.FETCH_FAILED))
        assert not breaker.allows(NOW)

    def test_a_refusal_opens_it_immediately(self):
        """Waiting for three refusals is three chances to earn a ban.

        A source that answered "no" does not need convincing that it meant it.
        """
        breaker = Breaker(source_id="S")
        breaker.record(outcome("S", SourceState.BLOCKED, error="rate limited"))
        assert not breaker.allows(NOW)
        assert breaker.cooling() >= BLOCKED_COOLING

    def test_success_closes_it_and_forgets_the_count(self):
        breaker = Breaker(source_id="S")
        breaker.record(outcome("S", SourceState.BLOCKED))
        breaker.record(outcome("S", SourceState.WATCHING))
        assert breaker.state is BreakerState.CLOSED
        assert breaker.failures == 0
        assert breaker.allows(NOW)

    def test_cooling_grows_with_each_consecutive_failure(self):
        breaker = Breaker(source_id="S")
        seen = []
        for _ in range(4):
            breaker.record(outcome("S", SourceState.FETCH_FAILED))
            seen.append(breaker.cooling())
        assert seen == sorted(seen)
        assert seen[0] < seen[-1]

    def test_cooling_stops_growing_rather_than_abandoning_the_source(self):
        """A source retried never again is the silent gap wearing a hat.

        The interval grows and then stops, so a State that returns after a
        fortnight of outage is noticed within hours rather than never.
        """
        breaker = Breaker(source_id="S")
        for _ in range(40):
            breaker.record(outcome("S", SourceState.BLOCKED))
        assert breaker.cooling() == MAX_COOLING

    def test_it_half_opens_once_cooled_and_one_attempt_decides(self):
        breaker = Breaker(source_id="S")
        breaker.record(outcome("S", SourceState.BLOCKED, at=NOW))
        assert not breaker.allows(NOW)
        later = NOW + breaker.cooling()
        assert breaker.allows(later)
        assert breaker.state is BreakerState.HALF_OPEN
        breaker.record(outcome("S", SourceState.WATCHING, at=later))
        assert breaker.state is BreakerState.CLOSED

    def test_a_credential_failure_counts_as_a_failure(self):
        """Asking again with the same rejected key is not a strategy."""
        breaker = Breaker(source_id="S")
        breaker.record(outcome("S", SourceState.CREDENTIAL_MISSING))
        assert breaker.failures == 1


# --------------------------------------------------------------------------
# The dead man's switch
# --------------------------------------------------------------------------


class TestHeartbeat:
    def test_a_watcher_that_has_never_ticked_is_not_alive(self):
        """An unstarted scheduler and a dead one are the same amount of watching."""
        assert not Heartbeat().is_alive(NOW)

    def test_a_recent_tick_is_alive(self):
        beat = Heartbeat()
        beat.beat(NOW)
        assert beat.is_alive(NOW + timedelta(minutes=1))

    def test_silence_past_the_tolerance_fires_the_switch(self):
        beat = Heartbeat()
        beat.beat(NOW)
        assert not beat.is_alive(NOW + DEAD_MANS_TOLERANCE + timedelta(seconds=1))

    def test_a_late_tick_is_not_an_outage(self):
        """One missed beat is a busy machine, not a gap in coverage."""
        beat = Heartbeat()
        beat.beat(NOW)
        assert beat.beat(NOW + timedelta(minutes=2)) is None
        assert beat.outages == []

    def test_a_real_gap_is_recorded_with_its_duration(self):
        beat = Heartbeat()
        beat.beat(NOW)
        gap = beat.beat(NOW + timedelta(hours=2))
        assert gap is not None
        assert gap.duration == timedelta(hours=2)
        assert beat.unwatched() == timedelta(hours=2)

    def test_an_outage_cannot_end_before_it_began(self):
        with pytest.raises(ValueError):
            Outage(began=NOW, ended=NOW - timedelta(minutes=1))

    def test_the_gap_describes_itself_as_unwatched_time(self):
        gap = Outage(began=NOW, ended=NOW + timedelta(hours=2))
        assert "unwatched" in gap.describe()
        assert gap.is_significant()


# --------------------------------------------------------------------------
# One supervised tick
# --------------------------------------------------------------------------


def supervisor(fetcher, *sources: Source, budget: HostBudget | None = None):
    return Supervisor(Watcher(registry(*sources), fetcher), budget=budget)


class TestSupervisedTick:
    def test_a_healthy_tick_checks_everything_due(self):
        transport = Unchanging()
        sup = supervisor(transport, source("A"), source("B"))
        report = sup.tick(NOW)
        assert report.asked == 2
        assert report.restrained == ()

    def test_the_host_budget_holds_back_the_excess(self):
        sup = supervisor(
            Unchanging(),
            *[source(f"S{n}") for n in range(6)],
            budget=HostBudget(rate_per_minute=10, burst=4),
        )
        report = sup.tick(NOW)
        assert report.asked == 4
        assert len(report.restrained_for("host budget")) == 2

    def test_held_back_is_never_expressed_as_absence(self):
        """The rule the whole module exists to keep.

        Every source that was due appears somewhere: asked, skipped, or named
        with the reason it was not asked. A report that dropped the difference
        would get shorter as the system got sicker.
        """
        sup = supervisor(
            Unchanging(),
            *[source(f"S{n}") for n in range(6)],
            budget=HostBudget(rate_per_minute=10, burst=4),
        )
        report = sup.tick(NOW)
        assert report.due == 6
        accounted = (
            set(report.tick.checked)
            | set(report.tick.skipped)
            | {r.source_id for r in report.restrained}
        )
        assert accounted == {f"S{n}" for n in range(6)}

    def test_a_tick_that_held_anything_back_is_not_quiet(self):
        sup = supervisor(
            Unchanging(),
            *[source(f"S{n}") for n in range(6)],
            budget=HostBudget(rate_per_minute=10, burst=4),
        )
        assert not sup.tick(NOW).quiet

    def test_a_refused_source_stops_being_asked(self):
        transport = Refuses()
        sup = supervisor(transport, source("A"))
        sup.tick(NOW)
        assert transport.calls == ["A"]
        # Long past this source's 15-minute check interval, and still not
        # asked: the circuit is open for an hour after a refusal.
        sup.tick(NOW + timedelta(minutes=45))
        assert transport.calls == ["A"]
        # And it does come back. A source retried never again is the silent
        # gap this module exists to prevent.
        sup.tick(NOW + BLOCKED_COOLING)
        assert transport.calls == ["A", "A"]

    def test_the_refusal_is_named_rather_than_silently_skipped(self):
        sup = supervisor(Refuses(), source("A"))
        sup.tick(NOW)
        report = sup.tick(NOW + timedelta(minutes=45))
        held = report.restrained_for("circuit open")
        assert [r.source_id for r in held] == ["A"]
        assert "refused" in held[0].detail

    def test_a_tick_whose_every_request_was_refused_says_so(self):
        """Without this the loudest possible failure renders as a quiet tick."""
        sup = supervisor(Refuses(), source("A"), source("B"))
        text = sup.tick(NOW).render()
        assert "FAILED" in text
        assert "rate limited" in text

    def test_health_is_decided_before_politeness_is_spent(self):
        """An open circuit must not consume budget a healthy source could use.

        Both sources share a host with one token. If the failing one were
        admitted first it would spend the token and the healthy one would be
        deferred for a request that was never going to be made.
        """
        transport = Refuses()
        sick = source("SICK")
        sup = Supervisor(
            Watcher(registry(sick, source("WELL")), transport),
            budget=HostBudget(rate_per_minute=10, burst=1),
        )
        sup.breaker("SICK").record(
            outcome("SICK", SourceState.BLOCKED, at=NOW - timedelta(minutes=1))
        )
        report = sup.tick(NOW)
        assert report.tick.checked == ("WELL",)
        assert [r.reason for r in report.restrained] == ["circuit open"]

    def test_a_source_never_asked_stays_due(self):
        """Deferred is not done. It comes back on the next tick."""
        sup = supervisor(
            Unchanging(),
            source("A"),
            source("B"),
            budget=HostBudget(rate_per_minute=60, burst=1),
        )
        first = sup.tick(NOW)
        assert first.asked == 1
        second = sup.tick(NOW + timedelta(seconds=60))
        assert second.asked == 1
        assert set(first.tick.checked) | set(second.tick.checked) == {"A", "B"}

    def test_an_outage_is_stated_on_the_tick_that_ends_it(self):
        sup = supervisor(Unchanging(), source("A"))
        sup.tick(NOW)
        report = sup.tick(NOW + timedelta(hours=2))
        assert report.gap is not None
        assert report.gap.duration == timedelta(hours=2)
        assert "UNWATCHED" in report.render()
        assert not report.quiet

    def test_the_outage_says_what_cannot_be_recovered(self):
        """A change that appeared and went inside the gap left no trace."""
        sup = supervisor(Unchanging(), source("A"))
        sup.tick(NOW)
        text = sup.tick(NOW + timedelta(hours=2)).render()
        assert "no later check" in text

    def test_a_credential_gap_is_reported_separately_from_a_failure(self):
        transport = Fails()
        sup = supervisor(transport, source("A"))
        report = sup.tick(NOW)
        assert report.tick.failed == ("A",)
        assert report.tick.skipped == ()

    def test_running_several_ticks_advances_the_clock(self):
        transport = Unchanging()
        sup = supervisor(transport, source("A"))
        reports = sup.run(3, start=NOW, every=timedelta(hours=1))
        assert [r.at for r in reports] == [
            NOW, NOW + timedelta(hours=1), NOW + timedelta(hours=2)
        ]
        assert transport.calls == ["A", "A", "A"]

    def test_a_restraint_describes_itself_for_a_person(self):
        held = Restraint(
            source_id="A", reason="host budget", until=NOW, detail="x at 10/min"
        )
        assert "A" in held.describe()
        assert "host budget" in held.describe()


# --------------------------------------------------------------------------
# Catch-up on restart, not resume
# --------------------------------------------------------------------------


class TestRestart:
    def test_unchanged_content_does_not_report_a_change_after_a_restart(self):
        """The defect this fixed had a cost measured in a thousand sources.

        The last hash lived only in memory, so a fresh process compared the
        current content against nothing and called every source changed at
        once — a deploy firing the whole heavy pipeline and writing a change
        record for content nobody touched.
        """
        held = registry(source("A"))
        transport = Unchanging()
        Watcher(held, transport).tick(NOW)
        # A new process. Same durable registry, no in-memory state.
        restarted = Watcher(held, Unchanging()).tick(NOW + timedelta(hours=12))
        assert restarted.changed == ()

    def test_a_genuine_change_across_a_restart_is_still_detected(self):
        held = registry(source("A"))
        Watcher(held, Unchanging(HASH)).tick(NOW)
        restarted = Watcher(held, Unchanging("b" * 64)).tick(NOW + timedelta(hours=12))
        assert restarted.changed == ("A",)

    def test_the_first_check_of_a_new_source_is_a_change(self):
        """New content is new, and that is not the same defect."""
        assert Watcher(registry(source("A")), Unchanging()).tick(NOW).changed == ("A",)

    def test_the_comparison_reads_the_last_recorded_hash_not_the_last_check(self):
        """A failed check records no hash, and must not erase the known one."""
        held = registry(source("A"))
        Watcher(held, Unchanging()).tick(NOW)
        Watcher(held, Fails()).tick(NOW + timedelta(hours=1))
        after = Watcher(held, Unchanging()).tick(NOW + timedelta(hours=2))
        assert after.changed == ()

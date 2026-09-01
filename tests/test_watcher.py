"""Tests for the publication watcher.

The fetcher here is a scripted transport, not fabricated aeronautical data. It
returns HTTP-level facts — 200, 304, 429, a content hash — which is what a
transport reports. The no-mock rule governs the *content* of publications, and
no publication content appears in this file.
"""

from datetime import date, datetime, timedelta, timezone

import pytest

from aeropub.airac import AiracCycle, cycle_for
from aeropub.registry import (
    CredentialRef,
    DetectionTier,
    Source,
    SourceFormat,
    SourceKind,
    SourceRegistry,
    SourceState,
)
from aeropub.watcher import (
    WINDOW_CLOSES_AFTER_DEADLINE,
    WINDOW_INTERVAL,
    WINDOW_OPENS_BEFORE_DEADLINE,
    FetchResult,
    TickReport,
    Watcher,
    publication_window_cycle,
)

HASH_A = "a" * 64
HASH_B = "b" * 64


class ScriptedFetcher:
    """Returns a prepared sequence of transport results, per source."""

    def __init__(self, script=None, default=None):
        self.script = {k: list(v) for k, v in (script or {}).items()}
        self.default = default or FetchResult(ok=True, http_status=304, not_modified=True)
        self.calls = []

    def fetch(self, source, *, credential):
        self.calls.append((source.source_id, credential))
        queue = self.script.get(source.source_id)
        if queue:
            return queue.pop(0)
        return self.default


def a_source(source_id="aip", kind=SourceKind.AIP, tier=DetectionTier.ADAPTIVE_POLL, **kw):
    return Source(
        source_id=source_id,
        authority="XX",
        name=f"Source {source_id}",
        kind=kind,
        url="https://example.invalid/aip",
        fmt=SourceFormat.EAIP_HTML,
        tier=tier,
        **kw,
    )


def at(day: date, hour=12, minute=0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=timezone.utc)


def quiet_moment() -> datetime:
    """A moment outside every publication window, verified rather than assumed."""
    day = AiracCycle.from_identifier("2606").effective_date
    for _ in range(28):
        if publication_window_cycle(day) is None:
            return at(day)
        day += timedelta(days=1)
    raise AssertionError("no quiet day in a full cycle — the window is too wide")


def window_moment(identifier: str = "2610") -> datetime:
    """A moment inside a publication window."""
    return at(AiracCycle.from_identifier(identifier).distribution_deadline)


class TestPublicationWindow:
    def test_brackets_the_distribution_deadline(self):
        cycle = AiracCycle.from_identifier("2610")
        assert publication_window_cycle(cycle.distribution_deadline) == cycle

    def test_opens_before_and_closes_after_the_deadline(self):
        cycle = AiracCycle.from_identifier("2610")
        opens = cycle.distribution_deadline - timedelta(days=WINDOW_OPENS_BEFORE_DEADLINE)
        closes = cycle.distribution_deadline + timedelta(days=WINDOW_CLOSES_AFTER_DEADLINE)
        assert publication_window_cycle(opens) == cycle
        assert publication_window_cycle(closes) == cycle
        assert publication_window_cycle(opens - timedelta(days=1)) is None
        assert publication_window_cycle(closes + timedelta(days=1)) is None

    def test_closes_later_than_it_opens_because_deadlines_are_no_later_than(self):
        assert WINDOW_CLOSES_AFTER_DEADLINE > WINDOW_OPENS_BEFORE_DEADLINE

    def test_most_of_a_cycle_stays_relaxed(self):
        # A window covering nearly the whole cycle would mean tightening always,
        # which is the failure this test exists to catch.
        day = AiracCycle.from_identifier("2610").effective_date - timedelta(days=70)
        tightened = sum(
            1 for i in range(28) if publication_window_cycle(day + timedelta(days=i))
        )
        assert 0 < tightened <= 12

    def test_windows_never_overlap(self):
        width = WINDOW_OPENS_BEFORE_DEADLINE + WINDOW_CLOSES_AFTER_DEADLINE + 1
        assert width < 28


class TestCadence:
    def test_tier_default_applies_outside_the_window(self):
        registry = SourceRegistry([a_source(tier=DetectionTier.ADAPTIVE_POLL)])
        watcher = Watcher(registry, ScriptedFetcher())
        assert watcher.interval_for(registry.get("aip"), quiet_moment()) == timedelta(minutes=15)

    def test_publication_indexes_tighten_inside_the_window(self):
        inside = window_moment()
        registry = SourceRegistry([a_source(tier=DetectionTier.ADAPTIVE_POLL)])
        watcher = Watcher(registry, ScriptedFetcher())
        assert watcher.interval_for(registry.get("aip"), inside) == WINDOW_INTERVAL

    def test_notam_feeds_are_exempt_from_tightening(self):
        # They have no cycle to anticipate and are already at their fastest.
        inside = window_moment()
        registry = SourceRegistry([a_source("notam", SourceKind.NOTAM, DetectionTier.FAST_POLL)])
        watcher = Watcher(registry, ScriptedFetcher())
        assert watcher.interval_for(registry.get("notam"), inside) == timedelta(minutes=5)

    def test_tightening_never_slows_a_faster_source(self):
        inside = window_moment()
        fast = a_source(tier=DetectionTier.PUSH, interval=timedelta(seconds=30))
        watcher = Watcher(SourceRegistry([fast]), ScriptedFetcher())
        assert watcher.interval_for(fast, inside) == timedelta(seconds=30)


class TestDueSelection:
    def test_a_never_checked_source_is_due_immediately(self):
        registry = SourceRegistry([a_source()])
        watcher = Watcher(registry, ScriptedFetcher())
        assert [s.source_id for s in watcher.due(quiet_moment())] == ["aip"]

    def test_not_due_again_until_the_interval_elapses(self):
        now = quiet_moment()
        registry = SourceRegistry([a_source()])
        watcher = Watcher(registry, ScriptedFetcher())
        watcher.check(registry.get("aip"), now)

        assert watcher.due(now + timedelta(minutes=14)) == []
        assert [s.source_id for s in watcher.due(now + timedelta(minutes=15))] == ["aip"]

    def test_disabled_sources_are_never_due(self):
        registry = SourceRegistry([a_source(enabled=False)])
        watcher = Watcher(registry, ScriptedFetcher())
        assert watcher.due(quiet_moment()) == []


class TestChangeDetection:
    def test_first_successful_fetch_counts_as_a_change(self):
        now = quiet_moment()
        registry = SourceRegistry([a_source()])
        fetcher = ScriptedFetcher({"aip": [FetchResult(ok=True, http_status=200, content_hash=HASH_A)]})
        outcome = Watcher(registry, fetcher).check(registry.get("aip"), now)
        assert outcome.changed
        assert outcome.state is SourceState.CHANGE_DETECTED

    def test_the_same_hash_twice_is_not_a_change(self):
        now = quiet_moment()
        registry = SourceRegistry([a_source()])
        fetcher = ScriptedFetcher({"aip": [
            FetchResult(ok=True, http_status=200, content_hash=HASH_A),
            FetchResult(ok=True, http_status=200, content_hash=HASH_A),
        ]})
        watcher = Watcher(registry, fetcher)
        watcher.check(registry.get("aip"), now)
        second = watcher.check(registry.get("aip"), now + timedelta(minutes=15))
        assert not second.changed
        assert second.state is SourceState.WATCHING

    def test_a_new_hash_is_a_change(self):
        now = quiet_moment()
        registry = SourceRegistry([a_source()])
        fetcher = ScriptedFetcher({"aip": [
            FetchResult(ok=True, http_status=200, content_hash=HASH_A),
            FetchResult(ok=True, http_status=200, content_hash=HASH_B),
        ]})
        watcher = Watcher(registry, fetcher)
        watcher.check(registry.get("aip"), now)
        assert watcher.check(registry.get("aip"), now + timedelta(minutes=15)).changed

    def test_not_modified_is_never_a_change(self):
        now = quiet_moment()
        registry = SourceRegistry([a_source()])
        fetcher = ScriptedFetcher({"aip": [FetchResult(ok=True, http_status=304, not_modified=True)]})
        outcome = Watcher(registry, fetcher).check(registry.get("aip"), now)
        assert not outcome.changed


class TestFailureHandling:
    def test_a_refusal_is_blocked_not_merely_failed(self):
        # Blocked means back off. Treating it as an ordinary failure and
        # retrying is how an address earns a ban.
        registry = SourceRegistry([a_source()])
        fetcher = ScriptedFetcher({"aip": [FetchResult(ok=False, http_status=429, blocked=True)]})
        outcome = Watcher(registry, fetcher).check(registry.get("aip"), quiet_moment())
        assert outcome.state is SourceState.BLOCKED

    def test_a_transport_error_is_a_fetch_failure(self):
        registry = SourceRegistry([a_source()])
        fetcher = ScriptedFetcher({"aip": [FetchResult(ok=False, error="connection reset")]})
        outcome = Watcher(registry, fetcher).check(registry.get("aip"), quiet_moment())
        assert outcome.state is SourceState.FETCH_FAILED
        assert outcome.error == "connection reset"


class TestCredentials:
    def test_a_source_without_its_key_is_not_fetched_at_all(self):
        cred = CredentialRef(env_var="KEY", label="Key")
        registry = SourceRegistry([a_source(credential=cred)])
        fetcher = ScriptedFetcher()
        outcome = Watcher(registry, fetcher, environ={}).check(registry.get("aip"), quiet_moment())
        assert outcome.state is SourceState.CREDENTIAL_MISSING
        assert fetcher.calls == []  # never reached the network

    def test_the_error_names_the_variable_to_set(self):
        cred = CredentialRef(env_var="FAA_NOTAM_API_KEY", label="FAA")
        registry = SourceRegistry([a_source(credential=cred)])
        outcome = Watcher(registry, ScriptedFetcher(), environ={}).check(
            registry.get("aip"), quiet_moment()
        )
        assert "FAA_NOTAM_API_KEY" in outcome.error

    def test_the_key_is_passed_to_the_fetcher_when_present(self):
        cred = CredentialRef(env_var="KEY", label="Key")
        registry = SourceRegistry([a_source(credential=cred)])
        fetcher = ScriptedFetcher()
        Watcher(registry, fetcher, environ={"KEY": "k"}).check(registry.get("aip"), quiet_moment())
        assert fetcher.calls == [("aip", "k")]

    def test_rejection_is_recorded_against_the_credential(self):
        cred = CredentialRef(env_var="KEY", label="Key")
        registry = SourceRegistry([a_source(credential=cred)])
        fetcher = ScriptedFetcher({"aip": [FetchResult(ok=False, http_status=401, unauthorised=True)]})
        watcher = Watcher(registry, fetcher, environ={"KEY": "wrong"})
        watcher.check(registry.get("aip"), quiet_moment())
        row = registry.status("aip", now=quiet_moment(), environ={"KEY": "wrong"})
        assert row.credential_status.value == "invalid"

    def test_a_working_key_clears_a_previous_rejection(self):
        cred = CredentialRef(env_var="KEY", label="Key").verified(quiet_moment())
        registry = SourceRegistry([a_source(credential=cred)])
        fetcher = ScriptedFetcher({"aip": [
            FetchResult(ok=False, http_status=401, unauthorised=True),
            FetchResult(ok=True, http_status=200, content_hash=HASH_A),
        ]})
        watcher = Watcher(registry, fetcher, environ={"KEY": "k"})
        now = quiet_moment()
        watcher.check(registry.get("aip"), now)
        watcher.check(registry.get("aip"), now + timedelta(minutes=15))
        row = registry.status("aip", now=now, environ={"KEY": "k"})
        assert row.credential_status.value == "configured"


class TestOverdue:
    """The publication that never arrived."""

    CYCLE = AiracCycle.from_identifier("2610")

    def moments(self):
        """Dates around one cycle's deadline, all consistent with each other."""
        c = self.CYCLE
        window_open = c.distribution_deadline - timedelta(days=WINDOW_OPENS_BEFORE_DEADLINE)
        return {
            "cycle": c,
            "window_open": window_open,
            "long_ago": at(window_open - timedelta(days=20)),
            "watching": at(c.distribution_deadline - timedelta(days=1)),
            "too_late": at(c.distribution_deadline + timedelta(days=3)),
            # Anywhere inside the preceding cycle, so `cycle` is the next one.
            "now": at(c.previous.effective_date + timedelta(days=5)),
        }

    def test_the_deadline_is_always_past_by_the_time_a_cycle_is_next(self):
        # Deadlines sit 42 days out, cycles 28 days apart, so the "next" cycle's
        # deadline is 14 to 42 days behind. There is no before-the-deadline case
        # to guard, which is why the implementation does not pretend there is.
        m = self.moments()
        assert m["cycle"].distribution_deadline < m["now"].date()
        assert m["cycle"].effective_date > m["now"].date()

    def test_overdue_when_we_watched_and_nothing_was_published(self):
        m = self.moments()
        registry = SourceRegistry([a_source()])
        watcher = Watcher(registry, ScriptedFetcher())
        watcher.check(registry.get("aip"), m["watching"])
        assert watcher.is_overdue(registry.get("aip"), m["now"])

    def test_not_declared_late_when_we_were_not_watching(self):
        # Absence of evidence is not evidence of absence.
        m = self.moments()
        registry = SourceRegistry([a_source()])
        assert not Watcher(registry, ScriptedFetcher()).is_overdue(registry.get("aip"), m["now"])

    def test_not_declared_late_when_we_only_started_watching_afterwards(self):
        # We would have missed the publication either way, so we cannot tell.
        m = self.moments()
        registry = SourceRegistry([a_source()])
        watcher = Watcher(registry, ScriptedFetcher())
        watcher.check(registry.get("aip"), m["too_late"])
        assert not watcher.is_overdue(registry.get("aip"), m["now"])

    def test_failed_checks_do_not_count_as_watching(self):
        # A window full of connection errors says nothing about the State.
        m = self.moments()
        registry = SourceRegistry([a_source()])
        fetcher = ScriptedFetcher({"aip": [FetchResult(ok=False, error="connection reset")]})
        watcher = Watcher(registry, fetcher)
        watcher.check(registry.get("aip"), m["watching"])
        assert not watcher.is_overdue(registry.get("aip"), m["now"])

    def test_a_change_inside_the_window_clears_it(self):
        m = self.moments()
        registry = SourceRegistry([a_source()])
        fetcher = ScriptedFetcher({"aip": [FetchResult(ok=True, http_status=200, content_hash=HASH_A)]})
        watcher = Watcher(registry, fetcher)
        watcher.check(registry.get("aip"), m["watching"])
        assert not watcher.is_overdue(registry.get("aip"), m["now"])

    def test_a_change_before_the_window_does_not_clear_it(self):
        # Last cycle's amendment says nothing about this cycle's.
        m = self.moments()
        registry = SourceRegistry([a_source()])
        fetcher = ScriptedFetcher({"aip": [
            FetchResult(ok=True, http_status=200, content_hash=HASH_A),  # long ago
            FetchResult(ok=True, http_status=304, not_modified=True),     # in window
        ]})
        watcher = Watcher(registry, fetcher)
        watcher.check(registry.get("aip"), m["long_ago"])
        watcher.check(registry.get("aip"), m["watching"])
        assert watcher.is_overdue(registry.get("aip"), m["now"])

    def test_notam_feeds_are_never_overdue(self):
        # They have no publication deadline to miss.
        m = self.moments()
        registry = SourceRegistry([a_source("notam", SourceKind.NOTAM)])
        assert not Watcher(registry, ScriptedFetcher()).is_overdue(registry.get("notam"), m["now"])


class TestTick:
    def test_reports_what_it_did(self):
        now = quiet_moment()
        registry = SourceRegistry([a_source("a"), a_source("b")])
        fetcher = ScriptedFetcher({
            "a": [FetchResult(ok=True, http_status=200, content_hash=HASH_A)],
            "b": [FetchResult(ok=False, error="timed out")],
        })
        report = Watcher(registry, fetcher).tick(now)
        assert set(report.checked) == {"a", "b"}
        assert report.changed == ("a",)
        assert report.failed == ("b",)
        assert not report.quiet

    def test_a_quiet_tick_is_quiet(self):
        registry = SourceRegistry([a_source()])
        report = Watcher(registry, ScriptedFetcher()).tick(quiet_moment())
        assert report.quiet
        assert report.changed == ()

    def test_sources_waiting_on_a_key_are_skipped_not_checked(self):
        cred = CredentialRef(env_var="KEY", label="Key")
        registry = SourceRegistry([a_source(credential=cred)])
        report = Watcher(registry, ScriptedFetcher(), environ={}).tick(quiet_moment())
        assert report.skipped == ("aip",)
        assert report.checked == ()

    def test_summary_reads_as_a_log_line(self):
        now = quiet_moment()
        registry = SourceRegistry([a_source("a"), a_source("b")])
        fetcher = ScriptedFetcher({"a": [FetchResult(ok=True, http_status=200, content_hash=HASH_A)]})
        assert "changed" in Watcher(registry, fetcher).tick(now).summary()

    def test_repeated_ticks_respect_the_interval(self):
        registry = SourceRegistry([a_source()])
        watcher = Watcher(registry, ScriptedFetcher())
        reports = watcher.run(5, start=quiet_moment(), every=timedelta(minutes=1))
        # 15-minute cadence, so only the first tick of five does any work.
        assert [len(r.checked) for r in reports] == [1, 0, 0, 0, 0]

    def test_tightened_cadence_checks_more_often_inside_the_window(self):
        registry = SourceRegistry([a_source()])
        watcher = Watcher(registry, ScriptedFetcher())
        # Twelve one-minute ticks: relaxed cadence checks once, tightened twice.
        inside = watcher.run(12, start=window_moment(), every=timedelta(minutes=1))
        assert sum(len(r.checked) for r in inside) == 3

        fresh = Watcher(SourceRegistry([a_source()]), ScriptedFetcher())
        outside = fresh.run(12, start=quiet_moment(), every=timedelta(minutes=1))
        assert sum(len(r.checked) for r in outside) == 1

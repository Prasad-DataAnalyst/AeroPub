"""The publication watcher — the tick that makes the platform live.

Every minute the watcher wakes, works out which sources are due, checks them,
and records what happened. Detection is deliberately cheap: a conditional
request that usually returns "not modified" costs almost nothing, so checking
the world every minute is affordable. Parsing is expensive and only runs when
something actually changed.

Two things here are not obvious from the outside.

**Cadence follows the AIRAC calendar.** A State publishes its AIRAC material by
a distribution deadline 42 days before the effective date, and in practice it
lands in the days around that deadline. Publication indexes tighten across that
window and relax through the rest of the cycle. NOTAM feeds are exempt — they
have no cycle to anticipate and are already at their fastest.

The tightening is modest on purpose. At 42 days of lead time, detecting a
publication in five minutes rather than fifteen changes nothing operationally;
the bulletin goes out weeks later either way. What it buys is a smaller chance
of missing a short-lived intermediate state — an index that is republished
twice in an afternoon — and that is worth five minutes, not one. Polling a
State's index every sixty seconds for a fortnight is how an address gets
blocked, and a blocked source is a silent coverage gap, which is the worst
failure this system has.

The window covers roughly a third of each cycle. It deliberately does not
bracket the 56-day major-change deadline as well: doing so would leave almost
no quiet period, and at 56 days of lead time the ordinary cadence is entirely
adequate.

**The watcher looks for what did not arrive.** Once a State's distribution
deadline passes with nothing published, that is a finding. Either the State is
late or our watching is broken, and both need a human. Nothing else in this
domain detects the publication that never came.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Protocol

from aeropub.airac import AiracCycle, cycle_for
from aeropub.registry import (
    CheckOutcome,
    Source,
    SourceKind,
    SourceRegistry,
    SourceState,
)

__all__ = [
    "FetchResult",
    "Fetcher",
    "TickReport",
    "Watcher",
    "WINDOW_OPENS_BEFORE_DEADLINE",
    "WINDOW_CLOSES_AFTER_DEADLINE",
    "WINDOW_INTERVAL",
    "publication_window_cycle",
]

#: Days before the distribution deadline that the window opens — enough margin
#: for a State that publishes early.
WINDOW_OPENS_BEFORE_DEADLINE = 4

#: Days after it that the window closes. Wider than the lead-in, because a
#: deadline is a "no later than" and publication clusters on or just after it.
WINDOW_CLOSES_AFTER_DEADLINE = 5

#: Cadence for publication indexes inside the window. Five minutes, not one —
#: see the module docstring for why the difference does not buy anything at
#: this lead time and costs a real risk of being blocked.
WINDOW_INTERVAL = timedelta(minutes=5)

#: Kinds whose cadence follows the AIRAC calendar. NOTAM is absent on purpose.
_CYCLE_DRIVEN = frozenset(
    {
        SourceKind.AIP,
        SourceKind.AMDT_INDEX,
        SourceKind.SUP_INDEX,
        SourceKind.AIC_INDEX,
        SourceKind.CHARTS,
    }
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def publication_window_cycle(day: date) -> AiracCycle | None:
    """The cycle whose publication window ``day`` falls in, if any.

    The window is ten days wide against a 28-day spacing, so at most one cycle
    can match and there is no overlap to resolve.
    """
    cycle = cycle_for(day)
    # The window sits about six weeks ahead, so look forward a few cycles.
    for offset in range(0, 4):
        candidate = cycle.shifted_by(offset)
        opens = candidate.distribution_deadline - timedelta(
            days=WINDOW_OPENS_BEFORE_DEADLINE
        )
        closes = candidate.distribution_deadline + timedelta(
            days=WINDOW_CLOSES_AFTER_DEADLINE
        )
        if opens <= day <= closes:
            return candidate
    return None


@dataclass(frozen=True, slots=True)
class FetchResult:
    """What a transport reports back. Not aeronautical data — a transport fact."""

    ok: bool
    http_status: int | None = None
    content_hash: str | None = None
    not_modified: bool = False
    blocked: bool = False
    """Rate limited or refused — back off rather than retry into a ban."""

    unauthorised: bool = False
    """Credential rejected. Distinct from blocked, and needs a different fix."""

    error: str | None = None
    duration_ms: int | None = None


class Fetcher(Protocol):
    """How the watcher reaches a source.

    Kept abstract so the HTTP implementation, which needs the network, is
    separable from the scheduling logic, which does not.
    """

    def fetch(self, source: Source, *, credential: str | None) -> FetchResult:
        ...


@dataclass(frozen=True, slots=True)
class TickReport:
    """What one tick did. The unit the status board and the logs are built from."""

    at: datetime
    checked: tuple[str, ...] = ()
    changed: tuple[str, ...] = ()
    failed: tuple[str, ...] = ()
    skipped: tuple[str, ...] = ()
    """Due, but not checked — disabled, or waiting on a credential."""

    overdue: tuple[str, ...] = ()
    """Sources whose State should have published by now and has not."""

    @property
    def quiet(self) -> bool:
        return not (self.changed or self.failed or self.overdue)

    def summary(self) -> str:
        parts = [f"{len(self.checked)} checked"]
        if self.changed:
            parts.append(f"{len(self.changed)} changed")
        if self.failed:
            parts.append(f"{len(self.failed)} failed")
        if self.skipped:
            parts.append(f"{len(self.skipped)} skipped")
        if self.overdue:
            parts.append(f"{len(self.overdue)} OVERDUE")
        return ", ".join(parts)


class Watcher:
    """Drives the registry: decides what is due, checks it, records the outcome."""

    def __init__(
        self,
        registry: SourceRegistry,
        fetcher: Fetcher,
        *,
        environ: dict[str, str] | None = None,
    ) -> None:
        self.registry = registry
        self.fetcher = fetcher
        self.environ = environ
        self._last_hash: dict[str, str] = {}

    # -- cadence ---------------------------------------------------------

    def interval_for(self, source: Source, now: datetime) -> timedelta:
        """How often this source should be checked, right now.

        Tightens inside the AIRAC publication window for cycle-driven kinds, but
        never loosens a source that is already faster than the window cadence.
        """
        base = source.check_interval
        if source.kind not in _CYCLE_DRIVEN:
            return base
        if publication_window_cycle(now.date()) is None:
            return base
        return min(base, WINDOW_INTERVAL)

    def next_due_at(self, source: Source, now: datetime) -> datetime | None:
        history = self.registry.checks(source.source_id)
        if not history:
            return None  # never checked — due immediately
        return history[-1].checked_at + self.interval_for(source, now)

    def due(self, now: datetime | None = None) -> list[Source]:
        """Sources whose next check has come around."""
        moment = now or _utcnow()
        ready = []
        for source in self.registry:
            if not source.enabled:
                continue
            due_at = self.next_due_at(source, moment)
            if due_at is None or due_at <= moment:
                ready.append(source)
        return ready

    # -- overdue ---------------------------------------------------------

    def is_overdue(self, source: Source, now: datetime) -> bool:
        """Whether a State should have published for the coming cycle and has not.

        Only meaningful for cycle-driven sources: a NOTAM feed has no deadline to
        miss. Answered by asking whether anything changed since the window for
        the next effective cycle opened.
        """
        if source.kind not in _CYCLE_DRIVEN:
            return False

        today = now.date()
        cycle = cycle_for(today).next

        # No deadline guard here, and deliberately so: the distribution deadline
        # sits 42 days before the effective date while cycles are 28 days apart,
        # so by the time a cycle is the *next* one its deadline is already 14 to
        # 42 days past. A "has the deadline passed" check would always be true.

        window_opened = cycle.distribution_deadline - timedelta(
            days=WINDOW_OPENS_BEFORE_DEADLINE
        )
        in_window = [
            o
            for o in self.registry.checks(source.source_id)
            if o.checked_at.date() >= window_opened
        ]

        # A State can only be called late if we were watching while it should
        # have published — which means a successful check at or before the
        # deadline, not merely one at some point in the window. Starting to
        # watch afterwards tells us nothing: we would have missed the
        # publication either way, and reporting that as OVERDUE blames the State
        # for a gap of our own. Not-watched surfaces as a coverage gap instead.
        watched_in_time = any(
            not o.state.is_exception
            and o.checked_at.date() <= cycle.distribution_deadline
            for o in in_window
        )
        if not watched_in_time:
            return False

        return not any(o.changed for o in in_window)

    # -- checking --------------------------------------------------------

    def check(self, source: Source, now: datetime | None = None) -> CheckOutcome:
        """Check one source and record the outcome."""
        moment = now or _utcnow()

        credential = None
        if source.credential is not None:
            credential = source.credential.resolve(self.environ)
            if credential is None:
                outcome = CheckOutcome(
                    source_id=source.source_id,
                    checked_at=moment,
                    state=SourceState.CREDENTIAL_MISSING,
                    error=f"{source.credential.env_var} is not set",
                )
                self.registry.record_check(outcome)
                return outcome

        result = self.fetcher.fetch(source, credential=credential)
        outcome = self._interpret(source, result, moment)
        self.registry.record_check(outcome)

        if result.unauthorised:
            self.registry.mark_credential_rejected(source.source_id)
        elif result.ok:
            self.registry.mark_credential_rejected(source.source_id, False)

        if result.content_hash:
            self._last_hash[source.source_id] = result.content_hash
        return outcome

    def _known_hash(self, source_id: str) -> str | None:
        """The last content hash held for this source, across restarts.

        In-memory only, this was a defect with a cost: a fresh process has an
        empty map, so the first check after any restart compared the current
        content against nothing and reported a change for every source at
        once. On the order of a thousand sources, that is a deploy firing the
        whole heavy pipeline and writing a change record for content nobody
        changed.

        The registry has already recorded the hash, so the fallback reads it
        back. That is what the plan means by catching up on restart rather than
        resuming: the comparison is against the last known state of the source,
        not against where the queue happened to stop.
        """
        remembered = self._last_hash.get(source_id)
        if remembered is not None:
            return remembered
        for outcome in reversed(self.registry.checks(source_id)):
            if outcome.content_hash:
                return outcome.content_hash
        return None

    def _interpret(
        self, source: Source, result: FetchResult, moment: datetime
    ) -> CheckOutcome:
        common = dict(
            source_id=source.source_id,
            checked_at=moment,
            http_status=result.http_status,
            duration_ms=result.duration_ms,
        )

        if result.unauthorised:
            return CheckOutcome(
                **common,
                state=SourceState.CREDENTIAL_MISSING,
                error=result.error or "credential rejected by the authority",
            )
        if result.blocked:
            return CheckOutcome(
                **common,
                state=SourceState.BLOCKED,
                error=result.error or "refused or rate limited",
            )
        if not result.ok:
            return CheckOutcome(
                **common,
                state=SourceState.FETCH_FAILED,
                error=result.error or "fetch failed",
            )

        previous = self._known_hash(source.source_id)
        changed = (
            not result.not_modified
            and result.content_hash is not None
            and result.content_hash != previous
        )
        return CheckOutcome(
            **common,
            state=SourceState.CHANGE_DETECTED if changed else SourceState.WATCHING,
            changed=changed,
            content_hash=result.content_hash,
        )

    # -- the tick --------------------------------------------------------

    def tick(self, now: datetime | None = None) -> TickReport:
        """One pass: check what is due, and look for what never arrived."""
        moment = now or _utcnow()
        checked: list[str] = []
        changed: list[str] = []
        failed: list[str] = []
        skipped: list[str] = []

        for source in self.due(moment):
            outcome = self.check(source, moment)
            if outcome.state is SourceState.CREDENTIAL_MISSING:
                skipped.append(source.source_id)
                continue
            checked.append(source.source_id)
            if outcome.changed:
                changed.append(source.source_id)
            if outcome.state.is_exception:
                failed.append(source.source_id)

        overdue = [
            s.source_id
            for s in self.registry
            if s.enabled and self.is_overdue(s, moment)
        ]

        return TickReport(
            at=moment,
            checked=tuple(checked),
            changed=tuple(changed),
            failed=tuple(failed),
            skipped=tuple(skipped),
            overdue=tuple(overdue),
        )

    def run(self, ticks: int, *, start: datetime, every: timedelta) -> list[TickReport]:
        """Run a fixed number of ticks. Useful for replay and for tests."""
        reports = []
        moment = start
        for _ in range(ticks):
            reports.append(self.tick(moment))
            moment += every
        return reports

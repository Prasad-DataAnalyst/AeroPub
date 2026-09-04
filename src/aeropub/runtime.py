"""Running 24/7 — the supervision a tick needs to survive 180 States.

:mod:`aeropub.watcher` decides what is due and checks it. That is enough to run
against a handful of sources on a good day. It is not enough to run
continuously against a thousand endpoints belonging to national authorities who
did not agree to be polled, on hardware that gets restarted, behind networks
that fail. This module is the layer that makes the tick survivable.

The failure it exists to prevent
--------------------------------
A blocked source is a **silent** coverage gap, and that is the worst failure
this platform has. Everything else announces itself: a parse error is loud, a
missing value renders as a gap, an overdue publication is a finding. A source
that quietly stopped answering because our own address was banned looks exactly
like a State that has published nothing — and the platform would go on
reporting "nothing changed" indefinitely, with complete confidence and no
evidence at all.

So politeness is not a courtesy here. It is a correctness property, and it is
enforced in code rather than in a runbook.

Two different reasons not to ask
--------------------------------
:class:`HostBudget` is about the *host*: several sources can share one national
AIS server, and the server does not care that they are separate rows in our
registry. The budget is spent per host, so a State with twelve watched
endpoints is not asked twelve times a minute.

:class:`Breaker` is about the *source*: something that has failed three times
running is not going to succeed on the fourth attempt thirty seconds later, and
continuing to ask is how a temporary fault becomes a permanent ban. The
cooling period grows with each consecutive failure, and a source that was
refused outright starts near the top of that range rather than working its way
up to it.

Held back is not the same as healthy
------------------------------------
The rule this module keeps, and the reason :class:`RuntimeReport` is shaped the
way it is: **a source we chose not to ask is never reported as a source that is
fine.** A tick that checked forty sources and held back sixty must not read
like a quiet tick. Every source held back is named, with the reason, in the
same document — because the alternative is a report that gets shorter as the
system gets sicker.

The dead man's switch
---------------------
A watcher that stops watching, silently, is worse than no watcher: the status
board still shows yesterday's green. :class:`Heartbeat` records when the tick
actually fired, so silence is detectable from outside, and
:class:`Outage` turns a gap between ticks into something the system states
rather than skips. A source checked either side of a two-hour gap is *not*
covered for those two hours: a NOTAM that appeared and was cancelled inside the
gap is gone, and no later check can recover it. That is blindness in the sense
:mod:`aeropub.retrospect` already uses, and it belongs in the record.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Iterable, Mapping
from urllib.parse import urlsplit

from aeropub.registry import CheckOutcome, Source, SourceState
from aeropub.watcher import TickReport, Watcher

__all__ = [
    "BLOCKED_COOLING",
    "Breaker",
    "BreakerState",
    "DEAD_MANS_TOLERANCE",
    "DEFAULT_BURST",
    "DEFAULT_RATE_PER_MINUTE",
    "DEFAULT_TICK",
    "HostBudget",
    "Heartbeat",
    "MAX_COOLING",
    "OPEN_AFTER_FAILURES",
    "Outage",
    "Restraint",
    "RuntimeReport",
    "Supervisor",
    "host_of",
]

#: The plan's heartbeat. What varies per source is the cadence, not whether the
#: system is watching.
DEFAULT_TICK = timedelta(seconds=60)

#: How long the tick may be silent before the dead man's switch fires. Five
#: missed ticks, not one: a single late tick is a busy machine, and an alert
#: that cries at every one of those is an alert nobody reads.
DEAD_MANS_TOLERANCE = timedelta(minutes=5)

#: Requests one host will be asked to serve per minute, across every source
#: that shares it. Deliberately modest — this is a national authority's web
#: server, not a CDN, and the cost of being wrong is a silent ban.
DEFAULT_RATE_PER_MINUTE = 10.0

#: How many requests may be spent at once before the rate applies. A small
#: burst lets a State's dozen endpoints be checked together on the tick they
#: come due, rather than being smeared across twelve minutes.
DEFAULT_BURST = 4

#: Consecutive failures before a source stops being asked.
OPEN_AFTER_FAILURES = 3

#: The longest a failing source waits between attempts. It never stops being
#: retried: a source abandoned entirely is exactly the silent gap this module
#: exists to prevent, so the interval grows and then stops growing.
MAX_COOLING = timedelta(hours=6)

#: Where a refusal starts. A source that answered "no" does not need three
#: more attempts to establish that it meant it, and each one raises the chance
#: of a ban that outlives the reason for it.
BLOCKED_COOLING = timedelta(hours=1)


#: Enough doublings to reach :data:`MAX_COOLING` from the shortest base,
#: and no more. Kept as a bound on the exponent so the arithmetic cannot
#: overflow before the cap is applied.
_MAX_DOUBLINGS = 16


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def host_of(url: str) -> str:
    """The host a URL will actually be asked, lower-cased.

    Port included, because a different port is a different listener; the
    scheme is not, because http and https to one name are one server as far as
    its operator's patience is concerned.
    """
    return (urlsplit(url).netloc or "").lower()


# --------------------------------------------------------------------------
# Politeness — spent per host, not per source
# --------------------------------------------------------------------------


@dataclass
class HostBudget:
    """A token bucket per host, so shared servers are asked once, not twelve times.

    The registry's unit is the source; the server's unit is the host. Twelve
    endpoints on one national AIS site are twelve rows here and one machine
    there, and only this class knows the difference.

    Not thread-safe, and deliberately not: the tick is single-threaded by
    design — one scheduler, one due set, one decision about who gets asked —
    and a lock here would imply otherwise.
    """

    rate_per_minute: float = DEFAULT_RATE_PER_MINUTE
    burst: int = DEFAULT_BURST
    overrides: Mapping[str, float] = field(default_factory=dict)
    """Per-host rates, for the sources whose operators have told us what they
    will tolerate. An agreed rate is evidence; a guessed one is not, so
    anything not named here gets the cautious default."""

    _tokens: dict[str, float] = field(default_factory=dict, repr=False)
    _last: dict[str, datetime] = field(default_factory=dict, repr=False)

    def rate_for(self, host: str) -> float:
        return float(self.overrides.get(host, self.rate_per_minute))

    def available(self, host: str, now: datetime) -> float:
        """Tokens this host has accrued, refilled to now and capped at the burst."""
        last = self._last.get(host)
        tokens = self._tokens.get(host, float(self.burst))
        if last is not None:
            elapsed = (now - last).total_seconds() / 60.0
            tokens = min(float(self.burst), tokens + elapsed * self.rate_for(host))
        return tokens

    def allows(self, host: str, now: datetime) -> bool:
        return self.available(host, now) >= 1.0

    def spend(self, host: str, now: datetime) -> bool:
        """Take one request's worth of budget. False if there is none."""
        tokens = self.available(host, now)
        self._last[host] = now
        if tokens < 1.0:
            self._tokens[host] = tokens
            return False
        self._tokens[host] = tokens - 1.0
        return True

    def wait_for(self, host: str, now: datetime) -> timedelta:
        """How long until this host can be asked again."""
        tokens = self.available(host, now)
        if tokens >= 1.0:
            return timedelta(0)
        rate = self.rate_for(host)
        if rate <= 0:
            return MAX_COOLING
        return timedelta(minutes=(1.0 - tokens) / rate)


# --------------------------------------------------------------------------
# Health — per source
# --------------------------------------------------------------------------


class BreakerState(str, Enum):
    """Whether a source is being asked, and why not."""

    CLOSED = "closed"
    """Healthy. Asked whenever the watcher says it is due."""

    OPEN = "open"
    """Failing. Not asked until the cooling period passes."""

    HALF_OPEN = "half_open"
    """Cooled off. One attempt is allowed; it decides which way this goes."""


@dataclass
class Breaker:
    """One source's failure history, and what it earns.

    Deliberately never latches permanently. An abandoned source is a silent
    coverage gap wearing a different hat — the interval grows to
    :data:`MAX_COOLING` and stops there, so a State that comes back after a
    fortnight of outage is noticed within six hours rather than never.
    """

    source_id: str
    failures: int = 0
    state: BreakerState = BreakerState.CLOSED
    opened_at: datetime | None = None
    last_error: str = ""
    was_blocked: bool = False
    """Whether the last failure was a refusal rather than a fault. A refusal
    is the one this module is most afraid of, and it cools longest."""

    def cooling(self) -> timedelta:
        """How long this source waits before the next attempt."""
        if self.failures <= 0:
            return timedelta(0)
        base = BLOCKED_COOLING if self.was_blocked else timedelta(minutes=1)
        # Bounded before the shift, not after. A source that has failed forty
        # times running is a real case — a State offline for a fortnight — and
        # 2**39 hours overflows timedelta rather than capping.
        doublings = min(self.failures - 1, _MAX_DOUBLINGS)
        return min(base * (2 ** doublings), MAX_COOLING)

    def ready_at(self) -> datetime | None:
        if self.opened_at is None:
            return None
        return self.opened_at + self.cooling()

    def poll(self, now: datetime) -> BreakerState:
        """The state as of ``now``, cooling an open breaker to half-open."""
        if self.state is BreakerState.OPEN:
            ready = self.ready_at()
            if ready is not None and now >= ready:
                self.state = BreakerState.HALF_OPEN
        return self.state

    def allows(self, now: datetime) -> bool:
        return self.poll(now) is not BreakerState.OPEN

    def record(self, outcome: CheckOutcome) -> None:
        """Update from one check. Success closes; failure counts."""
        if not outcome.state.is_exception:
            self.failures = 0
            self.state = BreakerState.CLOSED
            self.opened_at = None
            self.last_error = ""
            self.was_blocked = False
            return

        self.failures += 1
        self.last_error = outcome.error or outcome.state.value
        self.was_blocked = outcome.state is SourceState.BLOCKED
        # A refusal opens the circuit at once. Waiting for three of them is
        # three chances to turn a rate limit into a ban.
        if self.was_blocked or self.failures >= OPEN_AFTER_FAILURES:
            self.state = BreakerState.OPEN
            self.opened_at = outcome.checked_at


# --------------------------------------------------------------------------
# What a tick did not do
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Restraint:
    """One source that was due and was not asked.

    Exists so that "held back" can never be expressed as absence. A source
    missing from a report is indistinguishable from a source that was fine,
    and those are the two answers this platform must never confuse.
    """

    source_id: str
    reason: str
    until: datetime | None = None
    detail: str = ""

    def describe(self) -> str:
        when = f", retry after {self.until:%H:%M:%SZ}" if self.until else ""
        detail = f" — {self.detail}" if self.detail else ""
        return f"{self.source_id}: {self.reason}{when}{detail}"


@dataclass(frozen=True, slots=True)
class Outage:
    """A period in which the tick did not fire.

    Not merely a monitoring event. Nothing was watched across it, so a change
    that appeared and was withdrawn inside the gap left no trace anywhere and
    no later check can recover it. A source checked either side of a two-hour
    silence is covered up to the start and from the end, and not in between.
    """

    began: datetime
    ended: datetime

    def __post_init__(self) -> None:
        if self.ended < self.began:
            raise ValueError("Outage.ended precedes Outage.began")

    @property
    def duration(self) -> timedelta:
        return self.ended - self.began

    def is_significant(self, tick: timedelta = DEFAULT_TICK) -> bool:
        """Whether this is a gap rather than a late tick.

        One missed beat is scheduling noise. The threshold is the dead man's
        tolerance, so what the switch calls silence and what the record calls
        an outage are the same thing.
        """
        return self.duration > max(DEAD_MANS_TOLERANCE, tick)

    def describe(self) -> str:
        minutes = self.duration.total_seconds() / 60
        return (
            f"{self.began:%Y-%m-%d %H:%MZ} to {self.ended:%H:%MZ} "
            f"({minutes:.0f} min unwatched)"
        )


@dataclass
class Heartbeat:
    """When the tick actually fired. The dead man's switch reads this.

    Kept separate from the tick itself on purpose: something that monitors the
    monitor cannot be a property of the monitor. This records; deciding that
    the silence has gone on too long is :meth:`is_alive`, and it can be asked
    by anything, including a process that is not the scheduler.
    """

    tick: timedelta = DEFAULT_TICK
    tolerance: timedelta = DEAD_MANS_TOLERANCE
    beats: list[datetime] = field(default_factory=list)
    outages: list[Outage] = field(default_factory=list)

    @property
    def last(self) -> datetime | None:
        return self.beats[-1] if self.beats else None

    def beat(self, at: datetime) -> Outage | None:
        """Record a tick, returning the gap it just closed, if any."""
        previous = self.last
        self.beats.append(at)
        if previous is None:
            return None
        gap = Outage(began=previous, ended=at)
        if not gap.is_significant(self.tick):
            return None
        self.outages.append(gap)
        return gap

    def silence(self, now: datetime) -> timedelta:
        """How long since the last tick. The whole of time if there never was one."""
        if self.last is None:
            return timedelta.max
        return now - self.last

    def is_alive(self, now: datetime) -> bool:
        """Whether the tick is still firing.

        A watcher that has never ticked is not alive. That reads harshly for a
        process still starting up, and it is the right way round: an
        unstarted scheduler and a dead one are the same amount of watching.
        """
        if self.last is None:
            return False
        return self.silence(now) <= self.tolerance

    def unwatched(self) -> timedelta:
        """Total time the platform was not watching. The blindness figure."""
        total = timedelta(0)
        for gap in self.outages:
            total += gap.duration
        return total


# --------------------------------------------------------------------------
# One supervised tick
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RuntimeReport:
    """What one supervised tick did, and everything it decided not to do."""

    at: datetime
    tick: TickReport
    restrained: tuple[Restraint, ...] = ()
    gap: Outage | None = None
    alive: bool = True
    breakers: Mapping[str, Breaker] = field(default_factory=dict)
    """The health of every source that has failed at least once, as of this
    tick. Carried so a failure can say what it was without a second lookup."""

    @property
    def due(self) -> int:
        """How many sources were due, asked or not."""
        return len(self.tick.checked) + len(self.tick.skipped) + len(self.restrained)

    @property
    def asked(self) -> int:
        return len(self.tick.checked)

    @property
    def quiet(self) -> bool:
        """Whether this tick found nothing that needs a person.

        A tick that held sources back is never quiet, whatever it found in the
        ones it did ask. That is the point: the report must get louder as the
        system gets sicker, not shorter.
        """
        return self.tick.quiet and not self.restrained and self.gap is None

    def restrained_for(self, reason: str) -> tuple[Restraint, ...]:
        return tuple(r for r in self.restrained if r.reason == reason)

    def summary(self) -> str:
        parts = [self.tick.summary()]
        if self.restrained:
            parts.append(f"{len(self.restrained)} held back")
        if self.gap is not None:
            parts.append(f"gap {self.gap.describe()}")
        return ", ".join(parts)

    def render(self) -> str:
        lines = [
            f"TICK {self.at:%Y-%m-%d %H:%M:%SZ}",
            f"{self.due} due  ·  {self.asked} asked  ·  "
            f"{len(self.tick.changed)} changed  ·  "
            f"{len(self.restrained)} held back",
        ]
        if not self.alive:
            lines += [
                "",
                "!! THE TICK HAS STOPPED. The status board below is history, "
                "not the world.",
            ]
        if self.gap is not None:
            lines += [
                "",
                f"UNWATCHED — {self.gap.describe()}",
                "   Nothing was checked across that period. A change that "
                "appeared and was",
                "   withdrawn inside it left no trace, and no later check "
                "recovers it.",
            ]
        if self.tick.failed:
            # A tick whose every request was refused renders, without this,
            # as a tick on which nothing happened.
            lines += [
                "",
                f"FAILED — {len(self.tick.failed)} of {self.asked} asked did "
                "not answer",
            ]
            for source_id in self.tick.failed:
                breaker = self.breakers.get(source_id)
                detail = f" — {breaker.last_error}" if breaker and breaker.last_error else ""
                lines.append(f"  {source_id}{detail}")
        if self.tick.skipped:
            lines += [
                "",
                f"NOT ASKED — {len(self.tick.skipped)} waiting on a credential",
                "  " + ", ".join(self.tick.skipped),
            ]
        if self.tick.overdue:
            lines += [
                "",
                f"OVERDUE — {len(self.tick.overdue)} States should have "
                "published and have not",
                "  " + ", ".join(self.tick.overdue),
            ]
        if self.restrained:
            lines += [
                "",
                "HELD BACK — due, and deliberately not asked. Not checked is "
                "not clear.",
            ]
            for held in self.restrained:
                lines.append(f"  {held.describe()}")
        return "\n".join(lines)


class Supervisor:
    """The tick, with politeness, health and a heartbeat around it.

    Wraps a :class:`~aeropub.watcher.Watcher` rather than replacing it: the
    watcher answers what is due and what changed, and nothing here second-
    guesses either. What this adds is the decision not to ask, made explicit,
    and the record of the times nobody asked anything at all.
    """

    def __init__(
        self,
        watcher: Watcher,
        *,
        budget: HostBudget | None = None,
        heartbeat: Heartbeat | None = None,
        tick: timedelta = DEFAULT_TICK,
    ) -> None:
        self.watcher = watcher
        self.budget = budget or HostBudget()
        self.heartbeat = heartbeat or Heartbeat(tick=tick)
        self.tick_interval = tick
        self.breakers: dict[str, Breaker] = {}

    def breaker(self, source_id: str) -> Breaker:
        return self.breakers.setdefault(source_id, Breaker(source_id=source_id))

    def open_breakers(self, now: datetime) -> tuple[Breaker, ...]:
        """Every source currently not being asked because it keeps failing."""
        return tuple(
            b for b in self.breakers.values() if b.poll(now) is BreakerState.OPEN
        )

    # -- deciding who gets asked -----------------------------------------

    def admit(
        self, due: Iterable[Source], now: datetime
    ) -> tuple[list[Source], list[Restraint]]:
        """Split the due set into what will be asked and what will not.

        Health first, then politeness. The order matters: a source whose
        breaker is open should not spend host budget that a healthy source
        sharing that host could have used.
        """
        admitted: list[Source] = []
        held: list[Restraint] = []
        for source in due:
            breaker = self.breaker(source.source_id)
            if not breaker.allows(now):
                held.append(
                    Restraint(
                        source_id=source.source_id,
                        reason="circuit open",
                        until=breaker.ready_at(),
                        detail=(
                            f"{breaker.failures} consecutive failures"
                            + (" — refused" if breaker.was_blocked else "")
                            + (f": {breaker.last_error}" if breaker.last_error else "")
                        ),
                    )
                )
                continue
            host = host_of(source.url)
            if not self.budget.spend(host, now):
                held.append(
                    Restraint(
                        source_id=source.source_id,
                        reason="host budget",
                        until=now + self.budget.wait_for(host, now),
                        detail=f"{host} at {self.budget.rate_for(host):g}/min",
                    )
                )
                continue
            admitted.append(source)
        return admitted, held

    # -- the tick --------------------------------------------------------

    def tick(self, now: datetime | None = None) -> RuntimeReport:
        """One supervised pass."""
        moment = now or _utcnow()
        gap = self.heartbeat.beat(moment)

        due = self.watcher.due(moment)
        admitted, held = self.admit(due, moment)

        checked: list[str] = []
        changed: list[str] = []
        failed: list[str] = []
        skipped: list[str] = []
        for source in admitted:
            outcome = self.watcher.check(source, moment)
            self.breaker(source.source_id).record(outcome)
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
            for s in self.watcher.registry
            if s.enabled and self.watcher.is_overdue(s, moment)
        ]

        return RuntimeReport(
            at=moment,
            tick=TickReport(
                at=moment,
                checked=tuple(checked),
                changed=tuple(changed),
                failed=tuple(failed),
                skipped=tuple(skipped),
                overdue=tuple(overdue),
            ),
            restrained=tuple(held),
            gap=gap,
            breakers={
                source_id: breaker
                for source_id, breaker in self.breakers.items()
                if breaker.failures
            },
            # Asked as of this tick, which has just fired — so this is only
            # ever False when the tick itself has never run.
            alive=self.heartbeat.is_alive(moment),
        )

    def run(
        self, ticks: int, *, start: datetime, every: timedelta | None = None
    ) -> list[RuntimeReport]:
        """Run a fixed number of ticks. The replay path, and the test path."""
        interval = every or self.tick_interval
        reports = []
        moment = start
        for _ in range(ticks):
            reports.append(self.tick(moment))
            moment += interval
        return reports

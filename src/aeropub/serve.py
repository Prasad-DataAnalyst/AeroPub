"""The loop that keeps running — one pass, wait, repeat, for years.

:mod:`aeropub.watch` runs one pass. An operation runs passes forever, and the
only interesting question is *how often*. Both wrong answers are expensive:
check too rarely and an amendment sits unread for days after it published;
check too often and a State's AIM server sees a hundred and eighty thousand
requests a day from one address and blocks it — which turns into a silent
coverage gap, the worst failure this system has.

Cadence follows the AIRAC calendar, because publication does
------------------------------------------------------------
An AIP does not change at random. It changes on a 28-day cycle, and a State
publishes its material by a distribution deadline 42 days before the effective
date. In practice it lands in the days around that deadline. So there is a
window of roughly nine days per cycle when something is likely to appear, and
nineteen when almost nothing will.

Checking every five minutes for nine days and every six hours for the rest
finds an amendment within minutes of publication while costing a fraction of a
flat cadence. :func:`interval_at` is that decision and nothing else, so it can
be read and argued with on its own.

Most passes are shallow, and that is the larger saving
-------------------------------------------------------
A five-minute cadence that re-checked eighty sections would be three million
requests a year *per State* — across a hundred and eighty of them, seventeen a
second, sustained, forever. No AIM office should have to absorb that and
several would rightly block us for it.

They do not need to. A eAIP edition is largely immutable once published: a
change normally means a *new* edition, which the history page announces in one
request. So a frequent pass is shallow — resolve, read anything the ledger has
never seen, presume the rest — which is three requests rather than eighty-three.
A deep pass, which confirms every document, runs on :attr:`Loop.deep_every`,
because a State that republishes a section in place without touching its index
would look identical to a shallow pass and is not impossible.

Presumed is not confirmed, and :class:`~aeropub.cycle.Outcome` spells them
differently for that reason.

NOTAM is not on this clock. It is issued continuously and has its own path.

What "does not stop" means here
-------------------------------
:meth:`Loop.run_forever` returns only when asked to stop. A pass that fails
does not end it — :meth:`aeropub.cycle.Cycle.run` already guarantees it does
not raise, and the storage around it is guarded here — and neither does a
signal arriving mid-pass: the pass finishes, the ledger is closed cleanly, and
the loop returns. A ledger left half-written by a kill is a ledger that claims
things it should not, which is the failure the reconciliation exists to catch
and better not to cause.
"""

from __future__ import annotations

import signal
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Callable

from .cycle import Cycle, CycleReport
from .watcher import (
    WINDOW_INTERVAL,
    publication_window_cycle,
)

__all__ = ["Loop", "LoopReport", "interval_at", "QUIET_INTERVAL"]

#: Cadence outside a publication window. Six hours: an AIP that changes then
#: has changed unexpectedly, and finding out within a quarter of a day is
#: soon enough for something nobody expected to move.
QUIET_INTERVAL = timedelta(hours=6)


def interval_at(day: date) -> timedelta:
    """How long to wait before the next pass, given where the calendar is.

    Inside a publication window, the window cadence — publication clusters
    around the distribution deadline and being minutes behind it is the point.
    Outside one, :data:`QUIET_INTERVAL`.

    The window's own definition lives in :mod:`aeropub.watcher` so there is one
    answer to "is a State likely to be publishing today", not two that can
    drift apart.
    """
    return WINDOW_INTERVAL if publication_window_cycle(day) else QUIET_INTERVAL


@dataclass(frozen=True, slots=True)
class LoopReport:
    """What the loop did over its lifetime."""

    passes: int = 0
    deep_passes: int = 0
    documents_read: int = 0
    quiet_passes: int = 0
    stopped_because: str = ""

    def describe(self) -> str:
        return (
            f"{self.passes} passes ({self.deep_passes} deep)  ·  "
            f"{self.documents_read} documents read  ·  {self.quiet_passes} quiet\n"
            f"stopped: {self.stopped_because}"
        )


@dataclass
class Loop:
    """Runs a cycle on the AIRAC cadence until asked to stop."""

    cycle: Cycle
    sleep: Callable[[float], None]
    """Injected so a test can run a year of passes in a millisecond, and so a
    caller can substitute an interruptible wait."""

    on_report: Callable[[CycleReport], None] | None = None
    """Called after every pass. Quiet passes are still reported — a caller
    that wants to print only the interesting ones checks
    :attr:`~aeropub.cycle.CycleReport.quiet` itself, rather than this deciding
    what is worth knowing."""

    now: Callable[[], datetime] = field(
        default_factory=lambda: lambda: datetime.now(timezone.utc)
    )
    deep_every: timedelta = timedelta(hours=24)
    """How often to confirm every document rather than presume it.

    A day. Long enough that the saving is nearly all of it, short enough that
    a State republishing a section in place is caught within one operational
    period rather than one AIRAC cycle.
    """

    max_passes: int | None = None
    """A bound for a test or a one-shot run. ``None`` means forever."""

    _stopping: bool = field(default=False, init=False)
    _reason: str = field(default="", init=False)
    _last_deep: datetime | None = field(default=None, init=False)

    def stop(self, reason: str = "asked to stop") -> None:
        """Ask the loop to finish the current pass and return.

        Never interrupts a pass in flight. A ledger half-written by a kill
        claims things it should not, which is exactly what reconciliation
        exists to catch and better not to cause.
        """
        self._stopping = True
        self._reason = reason

    def install_signal_handlers(self) -> None:
        """Make SIGINT and SIGTERM ask rather than kill."""
        for received in (signal.SIGINT, signal.SIGTERM):
            signal.signal(
                received,
                lambda number, frame: self.stop(f"signal {signal.Signals(number).name}"),
            )

    def run_forever(self) -> LoopReport:
        passes = read = quiet = deep_passes = 0
        while not self._stopping:
            moment = self.now()
            deep = self._last_deep is None or (
                moment - self._last_deep >= self.deep_every
            )
            report = self.cycle.run(now=moment, deep=deep)
            if deep:
                self._last_deep = moment
                deep_passes += 1
            passes += 1
            read += report.documents_read
            if report.quiet:
                quiet += 1
            if self.on_report is not None:
                self.on_report(report)

            if self._stopping:
                break
            # The bound is checked here rather than at the top of the loop, so
            # a bounded run ends when its work does. Checked at the top it
            # sleeps a full interval first and then discovers it was finished
            # — invisible in an unbounded operation, and six hours of a
            # --passes 1 run doing nothing.
            if self.max_passes is not None and passes >= self.max_passes:
                self._reason = f"reached {self.max_passes} passes"
                break
            wait = interval_at(self.now().date())
            self.sleep(wait.total_seconds())

        return LoopReport(
            passes=passes,
            deep_passes=deep_passes,
            documents_read=read,
            quiet_passes=quiet,
            stopped_because=self._reason or "asked to stop",
        )

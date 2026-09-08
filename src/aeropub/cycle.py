"""The cycle that does not stop.

Reading a State once is a demonstration. Running an operation means reading
every State on its own cadence, for years, while States move their pages,
change their layouts, go down for maintenance and occasionally serve nonsense —
and never having that interrupt the platform.

So the guarantee this module makes is narrow and absolute: :meth:`Cycle.run`
does not raise. Every failure becomes a recorded outcome and the cycle carries
on. A State whose entry point is unreachable does not stop the other 179. A
section that 500s does not stop the other 79 in its own AIP. Anything that
escapes as an exception is a defect in this module, not a condition a caller
should handle.

Three properties make that useful rather than merely quiet
----------------------------------------------------------
**It resumes, it does not restart.** A :class:`Ledger` remembers the hash of
every document last read. On the next cycle an unchanged document costs a
conditional request and nothing else, so checking the world stays affordable
and an interrupted run picks up where it stopped rather than re-reading an AIP.

**Silence is never success.** A document that failed keeps whatever was last
read of it, and that data is now *stale by a known amount* rather than current.
:attr:`StateOutcome.is_complete` is false while anything failed, and
:meth:`CycleReport.describe` reports read, unchanged, failed and never-attempted
as four separate counts. Three of those are fine and one is not, and a single
"180 states checked" would hide which.

**Repeated failure is visible.** A State failing once is weather. A State
failing every cycle for a fortnight is a layout change nobody noticed, and it
looks identical from inside a single cycle. :attr:`StateOutcome.consecutive_failures`
carries forward so the difference is on the report rather than in a log nobody
reads.
"""

from __future__ import annotations

import traceback
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from typing import Callable, Iterable, Protocol

from .publication import Edition, EditionStatus, Publication
from .reader import Keep, ReadResult, Retrieve, read_publication
from .resolve import Resolver

__all__ = [
    "Outcome",
    "Ledger",
    "InMemoryLedger",
    "DocumentOutcome",
    "StateOutcome",
    "CycleReport",
    "Cycle",
]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Outcome(str, Enum):
    """What became of one document this cycle."""

    READ = "READ"
    """Fetched, and its content differs from what we last held."""

    UNCHANGED = "UNCHANGED"
    """Confirmed identical to what we hold. The cheap and usual case."""

    FAILED = "FAILED"
    """Reached for and not obtained. What we hold is now stale, not current."""

    NOT_ATTEMPTED = "NOT_ATTEMPTED"
    """Never reached for — the cycle stopped short of it.

    Distinct from FAILED. A document nobody tried is not a document that
    refused, and treating them alike turns an abandoned run into a report of
    healthy failures.
    """


class Ledger(Protocol):
    """What has already been read, so a cycle resumes rather than restarts."""

    def hash_for(self, url: str) -> str | None:
        """The content hash last recorded for this URL, or ``None``."""
        ...

    def record(
        self,
        url: str,
        content_hash: str,
        at: datetime,
        *,
        archive_key: str | None = None,
    ) -> None:
        """Note what this URL holds, and what we kept of it.

        The archive key is what lets a ledger be checked against the archive.
        Without it a ledger that outlives its archive answers UNCHANGED
        forever while holding nothing — total data loss reported as health.
        """
        ...

    def failures_for(self, state: str) -> int:
        """Consecutive failed cycles for a State."""
        ...

    def note_state(self, state: str, *, failed: bool) -> None:
        ...


@dataclass
class InMemoryLedger:
    """A ledger that forgets when the process does.

    Correct for a test and for a one-off run, and wrong for an operation: with
    it every restart re-reads every AIP, which is both slow and rude to the
    States. The persistent implementation belongs with the fact store.
    """

    hashes: dict[str, str] = field(default_factory=dict)
    seen_at: dict[str, datetime] = field(default_factory=dict)
    archive_keys: dict[str, str | None] = field(default_factory=dict)
    state_failures: dict[str, int] = field(default_factory=dict)

    def hash_for(self, url: str) -> str | None:
        return self.hashes.get(url)

    def record(
        self,
        url: str,
        content_hash: str,
        at: datetime,
        *,
        archive_key: str | None = None,
    ) -> None:
        self.hashes[url] = content_hash
        self.seen_at[url] = at
        self.archive_keys[url] = archive_key

    def failures_for(self, state: str) -> int:
        return self.state_failures.get(state, 0)

    def note_state(self, state: str, *, failed: bool) -> None:
        if failed:
            self.state_failures[state] = self.state_failures.get(state, 0) + 1
        else:
            self.state_failures.pop(state, None)


@dataclass(frozen=True, slots=True)
class DocumentOutcome:
    """One document, this cycle."""

    url: str
    outcome: Outcome
    code: str = ""
    detail: str = ""
    result: ReadResult | None = None

    @property
    def leaves_data_stale(self) -> bool:
        """Whether what we hold for this is older than we intended it to be."""
        return self.outcome in (Outcome.FAILED, Outcome.NOT_ATTEMPTED)


@dataclass(frozen=True, slots=True)
class StateOutcome:
    """One State, this cycle."""

    state: str
    name: str
    edition: Edition | None = None
    documents: tuple[DocumentOutcome, ...] = ()
    failed_because: str = ""
    """Set where the State itself could not be reached or resolved.

    Distinct from a State whose documents individually failed: this one never
    got as far as having documents, so an empty ``documents`` here means
    "unknown", not "none".
    """

    consecutive_failures: int = 0

    @property
    def reached(self) -> bool:
        return not self.failed_because

    @property
    def read(self) -> tuple[DocumentOutcome, ...]:
        return tuple(d for d in self.documents if d.outcome is Outcome.READ)

    @property
    def unchanged(self) -> tuple[DocumentOutcome, ...]:
        return tuple(d for d in self.documents if d.outcome is Outcome.UNCHANGED)

    @property
    def failed(self) -> tuple[DocumentOutcome, ...]:
        return tuple(d for d in self.documents if d.outcome is Outcome.FAILED)

    @property
    def not_attempted(self) -> tuple[DocumentOutcome, ...]:
        return tuple(d for d in self.documents if d.outcome is Outcome.NOT_ATTEMPTED)

    @property
    def is_complete(self) -> bool:
        """Whether everything this State publishes was accounted for.

        False while anything failed or went unattempted. A State that is 79 of
        80 read is not a State that is up to date.
        """
        return self.reached and not (self.failed or self.not_attempted)

    @property
    def is_persistently_failing(self) -> bool:
        """Failing long enough that it is a change, not weather.

        A fortnight of daily cycles is half an AIRAC period — long enough that
        a State's next amendment would land unread.
        """
        return self.consecutive_failures >= _PERSISTENT_AFTER

    def describe(self) -> str:
        if not self.reached:
            mark = " (persistent)" if self.is_persistently_failing else ""
            return (
                f"{self.state} {self.name}: NOT REACHED{mark} — "
                f"{self.failed_because}"
            )
        parts = [
            f"{len(self.read)} read",
            f"{len(self.unchanged)} unchanged",
        ]
        if self.failed:
            parts.append(f"{len(self.failed)} FAILED")
        if self.not_attempted:
            parts.append(f"{len(self.not_attempted)} not attempted")
        edition = ""
        if self.edition is not None:
            when = self.edition.effective_on
            edition = f"  [{self.edition.status.value}{f' {when}' if when else ''}]"
        return f"{self.state} {self.name}:{edition}  " + "  ·  ".join(parts)


#: Consecutive failing cycles after which a State is called persistently
#: failing rather than momentarily unavailable.
_PERSISTENT_AFTER = 14


@dataclass(frozen=True, slots=True)
class CycleReport:
    """What one pass over every State did."""

    at: datetime
    states: tuple[StateOutcome, ...] = ()

    @property
    def complete(self) -> tuple[StateOutcome, ...]:
        return tuple(s for s in self.states if s.is_complete)

    @property
    def incomplete(self) -> tuple[StateOutcome, ...]:
        return tuple(s for s in self.states if s.reached and not s.is_complete)

    @property
    def unreached(self) -> tuple[StateOutcome, ...]:
        return tuple(s for s in self.states if not s.reached)

    @property
    def persistently_failing(self) -> tuple[StateOutcome, ...]:
        return tuple(s for s in self.states if s.is_persistently_failing)

    @property
    def documents_read(self) -> int:
        return sum(len(s.read) for s in self.states)

    @property
    def quiet(self) -> bool:
        """Nothing changed and nothing broke — the shape of most cycles."""
        return not self.documents_read and not self.incomplete and not self.unreached

    def describe(self) -> str:
        lines = [
            f"CYCLE {self.at.isoformat(timespec='seconds')}",
            "",
            f"{len(self.states)} States  ·  {len(self.complete)} complete  ·  "
            f"{len(self.incomplete)} incomplete  ·  {len(self.unreached)} not reached",
            f"{self.documents_read} documents read",
        ]
        if self.persistently_failing:
            lines += [
                "",
                "PERSISTENTLY FAILING — a layout change, not weather",
            ]
            lines += [f"  {s.describe()}" for s in self.persistently_failing]
        if self.unreached:
            lines += ["", "NOT REACHED"]
            lines += [
                f"  {s.describe()}"
                for s in self.unreached
                if not s.is_persistently_failing
            ]
        if self.incomplete:
            lines += [
                "",
                "INCOMPLETE — what is held for these is stale, not current",
            ]
            lines += [f"  {s.describe()}" for s in self.incomplete]
        if not (self.unreached or self.incomplete):
            lines += ["", "Every State accounted for."]
        return "\n".join(lines)


@dataclass
class Cycle:
    """One pass over every State, isolated so nothing can stop it.

    ``choose_edition`` decides which of a State's editions to read. The default
    takes the one the State declares current, because that is what is in force;
    a caller planning ahead passes one that takes ``NEXT`` instead.
    """

    resolvers: tuple[Resolver, ...]
    retrieve: Retrieve
    ledger: Ledger = field(default_factory=InMemoryLedger)
    keep: Keep | None = None
    parse_for: Callable[[Publication], Callable | None] | None = None
    choose_edition: Callable[[tuple[Edition, ...]], Edition | None] | None = None
    budget: int | None = None
    """Documents to read per State per cycle. ``None`` for no limit.

    A bound exists so one State republishing its whole AIP cannot consume a
    cycle that 179 others are waiting in. What it displaces is reported as
    NOT_ATTEMPTED, never as unchanged.
    """

    def run(self, *, now: datetime | None = None) -> CycleReport:
        """Every State, once. Does not raise."""
        moment = now or _utcnow()
        return CycleReport(
            at=moment,
            states=tuple(self._state_safely(r, moment) for r in self.resolvers),
        )

    # -- one State ---------------------------------------------------------

    def _state_safely(self, resolver: Resolver, moment: datetime) -> StateOutcome:
        state = getattr(resolver, "state", "??")
        name = getattr(resolver, "name", "")
        try:
            outcome = self._run_state(resolver, moment)
        except Exception as error:  # noqa: BLE001 — the guarantee of this module
            outcome = StateOutcome(
                state=state,
                name=name,
                failed_because=f"{type(error).__name__}: {error}".strip()
                or type(error).__name__,
            )
        # The count is recorded first and read back second. Reading it first
        # reports a State that has just recovered as still failing, which is
        # the one direction of this error nobody would chase down.
        self.ledger.note_state(state, failed=not outcome.reached)
        return replace(
            outcome, consecutive_failures=self.ledger.failures_for(state)
        )

    def _run_state(self, resolver: Resolver, moment: datetime) -> StateOutcome:
        state, name = resolver.state, resolver.name

        editions = resolver.editions(self._read_bytes)
        edition = self._pick(editions)
        if edition is None:
            return StateOutcome(
                state=state,
                name=name,
                failed_because=(
                    "no edition could be chosen from "
                    f"{len(editions)} listed. An edition of unknown status is "
                    "not read on the assumption that it is current."
                ),
            )

        publications = resolver.publications(edition, self._read_bytes)
        documents: list[DocumentOutcome] = []
        budget = self.budget
        for publication in publications:
            if budget is not None and budget <= 0:
                documents.append(
                    DocumentOutcome(
                        url=publication.url,
                        code=publication.code,
                        outcome=Outcome.NOT_ATTEMPTED,
                        detail="the cycle's per-State budget was spent",
                    )
                )
                continue
            found = self._document_safely(publication, name, moment)
            documents.append(found)
            if budget is not None and found.outcome is Outcome.READ:
                budget -= 1

        return StateOutcome(
            state=state,
            name=name,
            edition=edition,
            documents=tuple(documents),
        )

    # -- one document ------------------------------------------------------

    def _document_safely(
        self, publication: Publication, state_name: str, moment: datetime
    ) -> DocumentOutcome:
        try:
            return self._run_document(publication, state_name, moment)
        except Exception as error:  # noqa: BLE001 — one page never stops a State
            return DocumentOutcome(
                url=publication.url,
                code=publication.code,
                outcome=Outcome.FAILED,
                detail=f"{type(error).__name__}: {error}".strip()
                or traceback.format_exc(limit=1),
            )

    def _run_document(
        self, publication: Publication, state_name: str, moment: datetime
    ) -> DocumentOutcome:
        parse = self.parse_for(publication) if self.parse_for else None
        result = read_publication(
            publication,
            self.retrieve,
            state_name=state_name,
            parse=parse,
            keep=self.keep,
        )

        if result.unchanged:
            # The server settled it without sending a body. Asking the ledger
            # would be asking a question already answered, and hashing what
            # did not arrive is how a 304 becomes a reported failure.
            return DocumentOutcome(
                url=publication.url,
                code=publication.code,
                outcome=Outcome.UNCHANGED,
                result=result,
            )

        held = self.ledger.hash_for(publication.url)
        current = result.link.content_hash
        if held is not None and current == held:
            return DocumentOutcome(
                url=publication.url,
                code=publication.code,
                outcome=Outcome.UNCHANGED,
                result=result,
            )

        if current:
            self.ledger.record(
                publication.url,
                current,
                moment,
                archive_key=result.link.archive_key,
            )
        return DocumentOutcome(
            url=publication.url,
            code=publication.code,
            outcome=Outcome.READ,
            result=result,
        )

    # -- helpers -----------------------------------------------------------

    def _read_bytes(self, url: str) -> bytes:
        """Bytes alone, for traversal. Errors propagate to the State guard."""
        return self.retrieve(url).body

    def _pick(self, editions: Iterable[Edition]) -> Edition | None:
        listed = tuple(editions)
        if self.choose_edition is not None:
            return self.choose_edition(listed)
        for edition in listed:
            if edition.status is EditionStatus.CURRENT:
                return edition
        # No State declared one current. An edition of undeclared status is not
        # promoted here: reading next cycle's AIP as though it were in force is
        # the failure this whole model exists to avoid.
        return None

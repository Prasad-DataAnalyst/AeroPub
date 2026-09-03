"""What is in force, on what, right now.

The fact store answers *"what is the LDA on RWY 34L on 15 October"* — a
question about a value, resolved per day. NOTAM do not fit that shape, and
forcing them into it loses the two things that matter most about them.

**Precision.** A NOTAM is valid from a minute, not from a date. A runway closed
from 02:34 was open at 02:00, and a fact whose validity is a ``date`` cannot
say so. Flattening the window would silently over-claim for part of a day.

**Schedules.** ``Daily:1100-0001`` means the NOTAM is dormant for eleven hours
inside its own validity window. A register that reports it in force at 06:00
because the window covers that date is not approximately right, it is wrong in
the direction that gets someone airborne on a false assumption. So a NOTAM with
a schedule this module has not interpreted reports
:attr:`ForceState.SCHEDULE_UNKNOWN`, never ``IN_FORCE``.

What the register does claim is deterministic: which aeronautical objects a
NOTAM is attached to, over what window, with what text, from where. Structured
sources such as the FAA's AIXM link those objects explicitly, and where a
source links nothing the subject falls back to the location the NOTAM was filed
against, marked as such — knowing a message concerns ZBW is not the same as
knowing which runway it closes, and the two must not read alike.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Iterable, Iterator

from aeropub.entities import aerodrome_of, covers, normalise
from aeropub.notam import NotamKind
from aeropub.provenance import SourceRef

__all__ = [
    "ForceState",
    "NotamRegister",
    "RegisteredNotam",
    "Subject",
    "SubjectKind",
]


class SubjectKind(str, Enum):
    """What sort of thing a NOTAM is attached to."""

    AERODROME = "aerodrome"
    RUNWAY = "runway"
    RUNWAY_DIRECTION = "runway_direction"
    TAXIWAY = "taxiway"
    APRON = "apron"
    NAVAID = "navaid"
    AIRSPACE = "airspace"
    ROUTE = "route"
    OBSTACLE = "obstacle"
    PROCEDURE = "procedure"

    FILED_LOCATION = "filed_location"
    """No structural link — all we know is where the message was filed.

    Kept as its own kind rather than guessed into one of the others. A NOTAM
    filed against an ARTCC affects *something* inside it, and treating that as
    "affects the aerodrome" would attach an airspace restriction to a runway.
    """

    @property
    def is_structural(self) -> bool:
        return self is not SubjectKind.FILED_LOCATION


class ForceState(str, Enum):
    """Whether a NOTAM applies at a given moment."""

    IN_FORCE = "in_force"
    NOT_YET = "not_yet"
    EXPIRED = "expired"

    SCHEDULE_UNKNOWN = "schedule_unknown"
    """Inside the validity window, but a schedule narrows it and we have not
    read the schedule. The honest answer, and never collapsed into
    ``IN_FORCE``: a NOTAM active 1100-0001 daily is dormant for eleven hours a
    day, and reporting it in force throughout is wrong in the dangerous
    direction."""

    UNKNOWN = "unknown"
    """No usable window. Reported rather than assumed either way."""

    @property
    def is_operative(self) -> bool:
        """Whether a planner must take this NOTAM into account.

        ``SCHEDULE_UNKNOWN`` counts. An unresolved schedule is a reason to read
        the NOTAM, not a reason to ignore it.
        """
        return self in (ForceState.IN_FORCE, ForceState.SCHEDULE_UNKNOWN, ForceState.UNKNOWN)


@dataclass(frozen=True, slots=True)
class Subject:
    """One aeronautical object a NOTAM is attached to."""

    entity: str
    """Canonical key, matching the fact store's convention: ``"8WC"`` for an
    aerodrome, ``"8WC/RWY20"`` for a runway direction."""

    kind: SubjectKind
    designator: str | None = None
    name: str | None = None

    icao: str | None = None
    """The ICAO location indicator, only where the source supplied one.

    Many FAA identifiers have no ICAO equivalent — ``8WC`` is three characters
    and prefixing ``K`` would invent an aerodrome — so this stays ``None``
    rather than being derived."""

    uuid: str | None = None
    """The source's stable identifier for the object, where it has one. What
    lets the same runway be recognised after it is renumbered."""

    def __post_init__(self) -> None:
        if not self.entity.strip():
            raise ValueError("Subject.entity must be a non-empty string")
        # Normalised at construction, so a key stored lower case can never
        # become an entity that lookups silently miss.
        object.__setattr__(self, "entity", normalise(self.entity))

    @property
    def aerodrome(self) -> str | None:
        """The aerodrome this subject hangs from, or ``None`` if it hangs from
        none — airspace and routes belong to no aerodrome."""
        return aerodrome_of(self.entity)

    @property
    def is_structural(self) -> bool:
        return self.kind.is_structural

    def describe(self) -> str:
        label = self.designator or self.name or self.entity
        if self.kind is SubjectKind.FILED_LOCATION:
            return f"filed against {label} (affected object not stated)"
        return f"{self.kind.value.replace('_', ' ')} {label}"


@dataclass(frozen=True, slots=True)
class RegisteredNotam:
    """One NOTAM, with what it affects and where it came from."""

    identifier: str
    subjects: tuple[Subject, ...]
    source: SourceRef
    text: str = ""

    effective_start: datetime | None = None
    effective_end: datetime | None = None
    permanent: bool = False
    estimated: bool = False
    """The end is a projection. Worth keeping: an estimated end is the NOTAM
    most likely to be extended."""

    schedule: str | None = None
    printed: str | None = None
    """The message as the authority prints it, where that differs from the text."""

    classification: str | None = None
    kind: NotamKind | None = None

    def __post_init__(self) -> None:
        if not self.identifier.strip():
            raise ValueError("RegisteredNotam.identifier must be a non-empty string")
        if not isinstance(self.source, SourceRef):
            raise TypeError(
                "RegisteredNotam.source must be a SourceRef; a NOTAM without "
                "provenance cannot be cited, and an uncitable NOTAM is not usable"
            )
        for name in ("effective_start", "effective_end"):
            moment = getattr(self, name)
            if moment is not None and moment.tzinfo is None:
                raise ValueError(f"RegisteredNotam.{name} must be timezone-aware (UTC)")

    @property
    def has_schedule(self) -> bool:
        return bool(self.schedule and self.schedule.strip())

    def state_at(self, moment: datetime) -> ForceState:
        """Whether this NOTAM applies at ``moment``, to the minute."""
        if moment.tzinfo is None:
            raise ValueError("moment must be timezone-aware (UTC)")
        if self.effective_start is None:
            return ForceState.UNKNOWN
        if moment < self.effective_start:
            return ForceState.NOT_YET
        if not self.permanent and self.effective_end is not None:
            if moment > self.effective_end:
                return ForceState.EXPIRED
        # Inside the window. A schedule we have not read narrows it further.
        return ForceState.SCHEDULE_UNKNOWN if self.has_schedule else ForceState.IN_FORCE

    def affects(self, entity: str) -> bool:
        """Whether any subject is this entity, or sits beneath it.

        ``"8WC"`` matches the aerodrome and every runway on it; ``"8WC/RWY20"``
        matches only that direction. Roll-up in one direction only — asking
        about a runway must not return a NOTAM about the whole aerodrome, which
        would attribute an apron closure to a runway.
        """
        return any(covers(entity, s.entity) for s in self.subjects)

    @property
    def entities(self) -> tuple[str, ...]:
        return tuple(s.entity for s in self.subjects)

    @property
    def is_structurally_attributed(self) -> bool:
        """Whether the source told us what the NOTAM affects, rather than only
        where it was filed."""
        return any(s.is_structural for s in self.subjects)

    def describe(self) -> str:
        subjects = "; ".join(s.describe() for s in self.subjects) or "no subject"
        return f"{self.identifier} — {subjects}"


class NotamRegister:
    """NOTAM indexed by what they affect."""

    def __init__(self, notams: Iterable[RegisteredNotam] | None = None) -> None:
        self._notams: list[RegisteredNotam] = []
        self._by_entity: dict[str, list[RegisteredNotam]] = defaultdict(list)
        self._seen: set[str] = set()
        for notam in notams or ():
            self.add(notam)

    def add(self, notam: RegisteredNotam) -> None:
        """Index a NOTAM. Re-adding the same identifier replaces nothing.

        Superseding is the archive's and the fact store's business, not an
        index's: a register that quietly dropped an earlier message would make
        the replacement look like the only one ever issued.
        """
        self._notams.append(notam)
        self._seen.add(notam.identifier)
        for subject in notam.subjects:
            self._by_entity[subject.entity].append(notam)

    def extend(self, notams: Iterable[RegisteredNotam]) -> None:
        for notam in notams:
            self.add(notam)

    def __len__(self) -> int:
        return len(self._notams)

    def __iter__(self) -> Iterator[RegisteredNotam]:
        return iter(self._notams)

    def entities(self) -> set[str]:
        return set(self._by_entity)

    def aerodromes(self) -> set[str]:
        """Every aerodrome with at least one structurally attributed NOTAM."""
        return {
            s.aerodrome
            for n in self._notams
            for s in n.subjects
            if s.kind in (SubjectKind.AERODROME, SubjectKind.RUNWAY, SubjectKind.RUNWAY_DIRECTION)
        }

    def for_entity(self, entity: str) -> tuple[RegisteredNotam, ...]:
        """Every NOTAM on this entity or beneath it, regardless of window."""
        return tuple(n for n in self._notams if n.affects(entity))

    def at(
        self, entity: str, moment: datetime, *, include_unresolved: bool = True
    ) -> tuple[tuple[RegisteredNotam, ForceState], ...]:
        """NOTAM a planner must consider for this entity at this moment.

        Each is paired with its state, so ``SCHEDULE_UNKNOWN`` reaches the
        caller instead of being flattened into a yes or a no. Set
        ``include_unresolved`` false to get only those certainly in force —
        useful for a count, never for a briefing.
        """
        out = []
        for notam in self.for_entity(entity):
            state = notam.state_at(moment)
            if state is ForceState.IN_FORCE or (include_unresolved and state.is_operative):
                out.append((notam, state))
        return tuple(out)

    def unattributed(self) -> tuple[RegisteredNotam, ...]:
        """NOTAM the source did not link to any object.

        A visible category, not a discard pile. These are real messages whose
        affected object we do not know, and an aerodrome dossier that omitted
        them silently would read as complete.
        """
        return tuple(n for n in self._notams if not n.is_structurally_attributed)

    def coverage(self) -> dict[str, int]:
        """Counts for the status board: what we indexed and what we could not."""
        structural = sum(1 for n in self._notams if n.is_structurally_attributed)
        return {
            "notams": len(self._notams),
            "entities": len(self._by_entity),
            "structurally_attributed": structural,
            "filed_location_only": len(self._notams) - structural,
            "with_schedule": sum(1 for n in self._notams if n.has_schedule),
            "estimated_end": sum(1 for n in self._notams if n.estimated),
        }

    def render(self, entity: str, moment: datetime) -> str:
        """A plain-text briefing for one entity. Absence renders as absence."""
        rows = self.at(entity, moment)
        header = f"{normalise(entity)} — {moment:%Y-%m-%d %H:%MZ}"
        if not rows:
            covered = any(covers(entity, e) for e in self._by_entity)
            note = (
                "no NOTAM in force"
                if covered
                else "no NOTAM indexed for this entity — this is a coverage gap, "
                "not a quiet aerodrome"
            )
            return f"{header}\n  {note}"

        lines = [header]
        for notam, state in rows:
            mark = "" if state is ForceState.IN_FORCE else f"  [{state.value}]"
            lines.append(f"  {notam.identifier}{mark}")
            for subject in notam.subjects:
                lines.append(f"      {subject.describe()}")
            if notam.text:
                lines.append(f"      {notam.text}")
            if notam.has_schedule:
                lines.append(f"      schedule: {notam.schedule}")
            lines.append(f"      {notam.source.describe()}")
        return "\n".join(lines)

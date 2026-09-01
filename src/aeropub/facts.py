"""Bitemporal facts and Consolidated Effective State resolution.

An aeronautical fact is rarely published once. The base **AIP** states a value,
an **AMDT** permanently changes it, a **SUP** temporarily overrides it, and a
**NOTAM** overrides everything for a few days. All four can be simultaneously
in force, each with its own validity window, and the operationally true value
is whichever sits on top.

Asking *"what is the LDA on RWY 34L at 1400Z on 15 October?"* therefore means
resolving a stack, not reading a field. That resolution is what
:class:`FactStore` does, and everything else in the platform is a view over it.

Two independent time axes
-------------------------
**Valid time** (``valid_from`` / ``valid_to``) is when the fact applies in the
world — the operational question.

**Transaction time** (``recorded_at`` / ``superseded_at``) is when *we* knew it
— the audit question. It is what makes it possible to answer "what did we
believe on the day of the event", which is a different question from "what was
true", and the one an investigation actually asks.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, datetime, timezone
from enum import IntEnum
from typing import Any, Iterable, Iterator

from aeropub.provenance import SourceRef

__all__ = ["Precedence", "Fact", "FactStore"]


class Precedence(IntEnum):
    """Which publication wins when several cover the same fact.

    Ordered by immediacy, not importance: a NOTAM is the most recent word on a
    subject, so it sits on top until it expires and the layer beneath resurfaces.
    """

    AIP = 10
    """The standing publication — the baseline everything else modifies."""

    AMDT = 20
    """Permanent change, AIRAC or otherwise."""

    SUP = 30
    """Supplement — a temporary change with an explicit validity window."""

    NOTAM = 40
    """Immediate temporary change. Overrides all of the above while in force."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class Fact:
    """One attributed value, valid over a window, from one publication layer.

    A ``Fact`` cannot be constructed without a :class:`SourceRef`. That is the
    whole point: there is no code path that produces an unattributed value, so
    a placeholder cannot reach a user by being forgotten.
    """

    entity: str
    """What it is about. e.g. ``"OTHH/RWY34L"``, ``"OTHH"``."""

    attribute: str
    """Which property. e.g. ``"lda_m"``, ``"rffs_category"``."""

    value: Any
    """The value itself. Never ``None`` — absence is a coverage gap, not a fact."""

    valid_from: date
    """First date the value applies operationally."""

    source: SourceRef
    """Where it came from. Required, always."""

    precedence: Precedence
    """Which publication layer this came from."""

    valid_to: date | None = None
    """Last date it applies, inclusive. ``None`` means open-ended."""

    recorded_at: datetime = field(default_factory=_utcnow)
    """When we learned it."""

    superseded_at: datetime | None = None
    """When we stopped believing it. ``None`` means still current."""

    def __post_init__(self) -> None:
        for name in ("entity", "attribute"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"Fact.{name} must be a non-empty string")

        if self.value is None:
            raise ValueError(
                "Fact.value must not be None — an unknown value is a coverage gap, "
                "not a fact with no value"
            )

        if not isinstance(self.source, SourceRef):
            raise TypeError(
                "Fact.source must be a SourceRef; a fact without provenance "
                "cannot exist"
            )

        if not isinstance(self.precedence, Precedence):
            raise TypeError("Fact.precedence must be a Precedence")

        if not isinstance(self.valid_from, date) or isinstance(self.valid_from, datetime):
            raise TypeError("Fact.valid_from must be a date")

        if self.valid_to is not None:
            if not isinstance(self.valid_to, date) or isinstance(self.valid_to, datetime):
                raise TypeError("Fact.valid_to must be a date or None")
            if self.valid_to < self.valid_from:
                raise ValueError(
                    f"Fact.valid_to ({self.valid_to}) precedes "
                    f"valid_from ({self.valid_from})"
                )

        if self.recorded_at.tzinfo is None:
            raise ValueError("Fact.recorded_at must be timezone-aware (UTC)")
        if self.superseded_at is not None:
            if self.superseded_at.tzinfo is None:
                raise ValueError("Fact.superseded_at must be timezone-aware (UTC)")
            if self.superseded_at < self.recorded_at:
                raise ValueError("Fact.superseded_at precedes recorded_at")

    # -- valid time ------------------------------------------------------

    @property
    def key(self) -> tuple[str, str]:
        """What this fact is about, for grouping."""
        return (self.entity, self.attribute)

    def applies_on(self, day: date) -> bool:
        """Whether the value is operationally in force on ``day``."""
        if day < self.valid_from:
            return False
        return self.valid_to is None or day <= self.valid_to

    # -- transaction time ------------------------------------------------

    def was_known_at(self, moment: datetime) -> bool:
        """Whether we held this belief at ``moment``.

        The time machine reads this: it is how the platform distinguishes what
        was true from what we knew, which an investigation needs kept apart.
        """
        if moment < self.recorded_at:
            return False
        return self.superseded_at is None or moment < self.superseded_at

    def superseded(self, at: datetime | None = None) -> "Fact":
        """A copy marked as no longer believed from ``at``."""
        return replace(self, superseded_at=at or _utcnow())


class FactStore:
    """In-memory bitemporal store with Consolidated Effective State resolution.

    Deliberately simple. The resolution semantics are the part worth getting
    right; swapping the storage for Postgres later changes none of them.
    """

    def __init__(self, facts: Iterable[Fact] | None = None) -> None:
        self._facts: list[Fact] = []
        for fact in facts or ():
            self.add(fact)

    def __len__(self) -> int:
        return len(self._facts)

    def __iter__(self) -> Iterator[Fact]:
        return iter(self._facts)

    def add(self, fact: Fact) -> None:
        if not isinstance(fact, Fact):
            raise TypeError("FactStore holds Fact objects")
        self._facts.append(fact)

    def extend(self, facts: Iterable[Fact]) -> None:
        for fact in facts:
            self.add(fact)

    # -- queries ---------------------------------------------------------

    def stack(
        self,
        entity: str,
        attribute: str,
        on: date,
        *,
        as_known_at: datetime | None = None,
    ) -> list[Fact]:
        """Every fact in force on ``on``, highest precedence first.

        This is the receipt a reviewer opens: not just the winning value but the
        whole layering beneath it, so the AIP value, the SUP that changed it and
        the NOTAM that overrode both are all visible at once.
        """
        candidates = [
            f
            for f in self._facts
            if f.key == (entity, attribute)
            and f.applies_on(on)
            and (as_known_at is None or f.was_known_at(as_known_at))
        ]
        # Highest precedence wins; among equals, the most recently recorded.
        candidates.sort(key=lambda f: (f.precedence, f.recorded_at), reverse=True)
        return candidates

    def effective(
        self,
        entity: str,
        attribute: str,
        on: date,
        *,
        as_known_at: datetime | None = None,
    ) -> Fact | None:
        """The operationally true fact on ``on``, or ``None`` if nothing covers it.

        ``None`` means the platform holds no value for that date — which the
        caller must render as a coverage gap, never as a blank or a default.
        """
        resolved = self.stack(entity, attribute, on, as_known_at=as_known_at)
        return resolved[0] if resolved else None

    def history(self, entity: str, attribute: str) -> list[Fact]:
        """Every fact ever held for this attribute, oldest belief first."""
        return sorted(
            (f for f in self._facts if f.key == (entity, attribute)),
            key=lambda f: (f.recorded_at, f.precedence),
        )

    def entities(self) -> set[str]:
        return {f.entity for f in self._facts}

    def attributes(self, entity: str) -> set[str]:
        return {f.attribute for f in self._facts if f.entity == entity}

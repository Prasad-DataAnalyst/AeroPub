"""The universal change record — what changed, from what to what.

Layer one of the three in the design: produced for every publication, in every
State, with no operator profile anywhere near it. A change record is a
statement about the world, not about anybody's fleet, so it is computed once
and read by everyone.

The comparison is between two *effective states*, not between two documents.
Asking "what changed between AIRAC 2609 and 2610" means resolving the full
AIP/AMDT/SUP/NOTAM stack on a date in each cycle and diffing the answers. That
is the only comparison that tells the truth: a supplement expiring changes the
operational value with no new publication at all, and a document diff would
miss it entirely.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import Enum
from typing import Iterable

from aeropub.airac import AiracCycle
from aeropub.facts import Fact, FactStore

__all__ = ["Change", "ChangeKind", "diff_effective", "diff_cycles"]


class ChangeKind(str, Enum):
    """What kind of difference this is."""

    ADDED = "added"
    """Covered after, uncovered before — newly published, or a supplement starting."""

    REMOVED = "removed"
    """Covered before, uncovered after. Not the same as a value going to zero."""

    MODIFIED = "modified"
    """Covered in both, with a different value."""


@dataclass(frozen=True, slots=True)
class Change:
    """One attribute's difference between two moments."""

    entity: str
    attribute: str
    kind: ChangeKind
    before: Fact | None
    after: Fact | None
    observed_from: date
    observed_to: date

    def __post_init__(self) -> None:
        if self.before is None and self.after is None:
            raise ValueError("a change needs a before or an after")
        if self.kind is ChangeKind.MODIFIED and (self.before is None or self.after is None):
            raise ValueError("a modification needs both sides")
        if self.kind is ChangeKind.ADDED and self.before is not None:
            raise ValueError("an addition cannot have a before")
        if self.kind is ChangeKind.REMOVED and self.after is not None:
            raise ValueError("a removal cannot have an after")

    @property
    def from_value(self):
        return self.before.value if self.before else None

    @property
    def to_value(self):
        return self.after.value if self.after else None

    @property
    def key(self) -> tuple[str, str]:
        return (self.entity, self.attribute)

    @property
    def source_before(self) -> str | None:
        return self.before.source.document if self.before else None

    @property
    def source_after(self) -> str | None:
        return self.after.source.document if self.after else None

    def describe(self) -> str:
        """One line, factual, with no interpretation."""
        if self.kind is ChangeKind.ADDED:
            return f"{self.attribute} published as {self.to_value}"
        if self.kind is ChangeKind.REMOVED:
            return f"{self.attribute} no longer published (was {self.from_value})"
        return f"{self.attribute} {self.from_value} → {self.to_value}"

    def numeric_delta(self) -> float | None:
        """How far a numeric value moved, or ``None`` if it is not numeric."""
        if self.kind is not ChangeKind.MODIFIED:
            return None
        if isinstance(self.from_value, bool) or isinstance(self.to_value, bool):
            return None
        if isinstance(self.from_value, (int, float)) and isinstance(
            self.to_value, (int, float)
        ):
            return float(self.to_value) - float(self.from_value)
        return None


def _attributes(store: FactStore, entity: str | None) -> set[tuple[str, str]]:
    keys = {f.key for f in store}
    if entity is not None:
        keys = {k for k in keys if k[0] == entity}
    return keys


def diff_effective(
    store: FactStore,
    before: date,
    after: date,
    *,
    entity: str | None = None,
    attributes: Iterable[str] | None = None,
) -> list[Change]:
    """Every difference in effective state between two dates.

    Both sides are resolved through the CES stack, so a supplement expiring
    registers as a change even though nothing was published to cause it.
    """
    wanted = set(attributes) if attributes is not None else None
    changes: list[Change] = []

    for entity_id, attribute in sorted(_attributes(store, entity)):
        if wanted is not None and attribute not in wanted:
            continue

        was = store.effective(entity_id, attribute, before)
        now = store.effective(entity_id, attribute, after)

        if was is None and now is None:
            continue
        if was is None:
            kind = ChangeKind.ADDED
        elif now is None:
            kind = ChangeKind.REMOVED
        elif was.value == now.value:
            continue
        else:
            kind = ChangeKind.MODIFIED

        changes.append(
            Change(
                entity=entity_id,
                attribute=attribute,
                kind=kind,
                before=was,
                after=now,
                observed_from=before,
                observed_to=after,
            )
        )

    return changes


def diff_cycles(
    store: FactStore,
    before: AiracCycle,
    after: AiracCycle,
    *,
    entity: str | None = None,
    attributes: Iterable[str] | None = None,
) -> list[Change]:
    """Every difference between two AIRAC cycles.

    Each cycle is sampled on its own effective date, which is the date its
    information is in force from.
    """
    return diff_effective(
        store,
        before.effective_date,
        after.effective_date,
        entity=entity,
        attributes=attributes,
    )

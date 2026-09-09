"""AIP Supplements — the layer between the amendment and the NOTAM.

`facts.Precedence` has said since the beginning that a supplement outranks the
AIP and is outranked by a NOTAM. Every ENR module built since then reads the
AIP and gets overridden by NOTAM, and none of them has ever heard of a
supplement. So a State that raises a danger area to FL500 for the summer, or
withdraws an airway for a construction season, publishes that in a SUP — and
the atlas keeps drawing the base AIP with nothing to say it is no longer what
governs.

A supplement is a pointer, not a patch
---------------------------------------
This module does **not** apply a supplement's content. It records what a
supplement bears on and when, and that is deliberate: reading "AR-7 upper limit
becomes FL500" out of a paragraph of published prose and writing 50000 into a
register is exactly the kind of derived value the rest of this platform
refuses. Somebody transcribes a supplement into a manifest or nobody does.

What it does instead is the same thing a NOTAM does to a navaid's published
status: it **reopens the question**. A value from a section a supplement
modifies comes back as superseded, naming the supplement, and the reader goes
and reads it. That is a smaller claim than a patched value and a true one.

Why the validity window is dates and not moments
-------------------------------------------------
A NOTAM has a start and end to the minute and often a schedule inside that. A
supplement is published against AIRAC dates and runs for weeks or a season. So
the window here is dates, the state at a given day is a clean three-way answer,
and nothing pretends to an hour it was not given.

The relationship to GEN 0.3
----------------------------
:class:`aeropub.checklist.SupplementRecord` is a line of the *State's list* of
supplements in force — evidence about what exists. A :class:`Supplement` here
is one we hold and have read far enough to know which section and which objects
it touches. The checklist reconciliation compares the two, and a supplement on
the State's list that never became one of these is exactly the gap it reports.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from enum import Enum
from pathlib import Path
from typing import Iterable, Mapping

from aeropub.entities import normalise
from aeropub.facts import Precedence, SourceRef
from aeropub.manifest import (
    ManifestError,
    document_source,
    read_manifest,
    sub_source,
    to_date,
)

__all__ = [
    "ForcePeriod",
    "Supersession",
    "Supplement",
    "SupplementRegister",
    "load_supplements",
    "supplement_template",
]

#: The parser identity written into citations read from a supplement manifest.
SUPPLEMENT_PARSER_ID = "aeropub.supplement"

#: Where a supplement sits against everything else. Not defined here — it has
#: always been in :mod:`aeropub.facts`, and this module is what finally uses it.
PRECEDENCE = Precedence.SUP


class ForcePeriod(str, Enum):
    """Whether a supplement applies on a given day."""

    IN_FORCE = "in_force"
    NOT_YET = "not_yet"
    EXPIRED = "expired"
    OPEN_ENDED = "open_ended"
    """In force, with no published end. Common — a supplement runs "until
    further notice" — and kept apart from a dated one because a reader
    planning six months out needs to know nothing says when it stops."""

    UNDATED = "undated"
    """Held with no window at all. Never read as expired: a supplement whose
    dates nobody transcribed is one nobody can say has ended."""

    @property
    def applies(self) -> bool | None:
        """Whether it bears on the day asked about.

        ``None`` for :attr:`UNDATED`. Not a soft no — an unread window is not
        an ended one, and treating it as one would quietly retire a supplement
        that is still in force.
        """
        if self is ForcePeriod.UNDATED:
            return None
        return self in (ForcePeriod.IN_FORCE, ForcePeriod.OPEN_ENDED)


class Supersession(str, Enum):
    """What a supplement does to the section it names."""

    REPLACES = "replaces"
    """The published text does not apply for the period. The strongest form,
    and the one that makes a held value wrong rather than incomplete."""

    AMENDS = "amends"
    """Part of the section changes. The rest still stands, and which part is in
    the supplement's own text."""

    ADDS = "adds"
    """Something new for the period — a temporary restricted area, a temporary
    route. Nothing held is wrong; something held is missing."""

    NOT_STATED = "not_stated"

    @property
    def invalidates_held_values(self) -> bool | None:
        """Whether a value read from that section may still be relied on.

        ``None`` where the extract did not say, because a supplement that might
        replace a section is not one that certainly does not.
        """
        if self is Supersession.NOT_STATED:
            return None
        return self in (Supersession.REPLACES, Supersession.AMENDS)


@dataclass(frozen=True, slots=True)
class Supplement:
    """One AIP Supplement, as far as it has been read."""

    identifier: str
    source: SourceRef
    section: str = ""
    """The AIP section it modifies — ``ENR 5.1``, ``AD 2.12``. Held as
    published; nothing derives it from the subjects."""

    subjects: tuple[str, ...] = ()
    """Entity keys it bears on, in the same key space everything else uses —
    ``AIRSPACE:AR-7``, ``ATS:UM688``, ``NAVAID:ALP``. What lets a supplement
    reach the thing it is about instead of sitting in a list of documents."""

    effective_from: date | None = None
    effective_to: date | None = None
    supersession: Supersession = Supersession.NOT_STATED
    summary: str = ""
    """What it says, in the State's own words. This module reads no further
    into it than that: a value transcribed out of prose here would be a
    derived value with a citation, which is the most convincing kind of
    wrong."""

    replaces: str = ""
    """A supplement this one supersedes, where it says so."""

    region: str = ""
    remarks: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "identifier", str(self.identifier).strip().upper())
        object.__setattr__(self, "section", str(self.section).strip())
        object.__setattr__(self, "region", normalise(self.region))
        object.__setattr__(
            self, "subjects", tuple(normalise(s) for s in self.subjects if str(s).strip())
        )
        if not self.identifier:
            raise ValueError(
                "Supplement.identifier must be a non-empty string — a "
                "supplement nobody can name is one nobody can supersede."
            )
        if not isinstance(self.supersession, Supersession):
            raise TypeError("Supplement.supersession must be a Supersession")
        if not isinstance(self.source, SourceRef):
            raise TypeError("Supplement.source must be a SourceRef")
        if (
            self.effective_from is not None
            and self.effective_to is not None
            and self.effective_to < self.effective_from
        ):
            raise ValueError(
                f"{self.identifier}: ends {self.effective_to} before it starts "
                f"{self.effective_from}. One of the two was read from the "
                "wrong column."
            )

    def state_on(self, day: date) -> ForcePeriod:
        """Whether it applies on that day."""
        if self.effective_from is None and self.effective_to is None:
            return ForcePeriod.UNDATED
        if self.effective_from is not None and day < self.effective_from:
            return ForcePeriod.NOT_YET
        if self.effective_to is None:
            return ForcePeriod.OPEN_ENDED
        if day > self.effective_to:
            return ForcePeriod.EXPIRED
        return ForcePeriod.IN_FORCE

    def bears_on(self, key: str) -> bool:
        """Whether it names this object.

        Exact against the entity key. No prefix matching: a supplement about
        ``AIRSPACE:AR-7`` is not one about ``AIRSPACE:AR-70``, and a lookup
        that thought otherwise would attach a restriction to the wrong area.
        """
        return normalise(key) in self.subjects

    def describe(self) -> str:
        parts = [f"SUP {self.identifier}"]
        if self.section:
            parts.append(self.section)
        if self.supersession is not Supersession.NOT_STATED:
            parts.append(self.supersession.value)
        window = self.window()
        if window:
            parts.append(window)
        if self.summary:
            parts.append(self.summary)
        return "  ·  ".join(parts)

    def window(self) -> str:
        if self.effective_from is None and self.effective_to is None:
            return "no validity window read"
        if self.effective_to is None:
            return f"from {self.effective_from} until further notice"
        if self.effective_from is None:
            return f"until {self.effective_to}"
        return f"{self.effective_from} to {self.effective_to}"


@dataclass(frozen=True, slots=True)
class SupplementRegister:
    """Every supplement read so far.

    The lookup mirrors :class:`aeropub.notam_register.NotamRegister` on
    purpose: everywhere a NOTAM already reaches an object, a supplement can
    reach it the same way and with the same shape of answer.
    """

    supplements: tuple[Supplement, ...] = ()

    def __len__(self) -> int:
        return len(self.supplements)

    def __iter__(self):
        return iter(self.supplements)

    @property
    def identifiers(self) -> tuple[str, ...]:
        return tuple(sorted({s.identifier for s in self.supplements}))

    @property
    def superseded(self) -> frozenset[str]:
        """Supplements another one says it replaces."""
        return frozenset(s.replaces.strip().upper() for s in self.supplements if s.replaces)

    def at(
        self, key: str, on: date, *, include_undated: bool = True
    ) -> tuple[tuple[Supplement, ForcePeriod], ...]:
        """Supplements a planner must consider for this object on this day.

        Each is paired with its period, so ``UNDATED`` reaches the caller
        rather than being flattened into a yes or a no. A supplement another
        one replaces is dropped: the State said which is current, and showing
        both would be showing a document that has been withdrawn.
        """
        wanted = normalise(key)
        if not wanted:
            return ()
        replaced = self.superseded
        out: list[tuple[Supplement, ForcePeriod]] = []
        for supplement in self.supplements:
            if supplement.identifier in replaced:
                continue
            if not supplement.bears_on(wanted):
                continue
            period = supplement.state_on(on)
            if period.applies or (include_undated and period is ForcePeriod.UNDATED):
                out.append((supplement, period))
        return tuple(out)

    def for_section(
        self, code: str, on: date
    ) -> tuple[tuple[Supplement, ForcePeriod], ...]:
        """Supplements modifying a whole section on this day.

        The other half of the question: a supplement may name no object at all
        and still replace ENR 5.1 for a month, and a lookup by object would
        never find it.
        """
        wanted = str(code).strip().casefold()
        if not wanted:
            return ()
        replaced = self.superseded
        out: list[tuple[Supplement, ForcePeriod]] = []
        for supplement in self.supplements:
            if supplement.identifier in replaced:
                continue
            if supplement.section.strip().casefold() != wanted:
                continue
            period = supplement.state_on(on)
            if period.applies is not False:
                out.append((supplement, period))
        return tuple(out)

    def in_force(self, on: date) -> tuple[Supplement, ...]:
        """Supplements *known* to apply on this day.

        Excludes those whose window nobody has read, because saying a
        supplement is in force is a claim and an unread window does not
        support one. That exclusion is the reason
        :meth:`not_known_to_have_ended` exists, and why a screening path must
        use that instead: ``applies`` is three-valued, ``None`` is falsy, and
        a filter written on truthiness drops exactly the supplements nobody
        can vouch for either way.
        """
        replaced = self.superseded
        return tuple(
            s
            for s in self.supplements
            if s.identifier not in replaced and s.state_on(on).applies is True
        )

    def of_unread_window(self) -> tuple[Supplement, ...]:
        """Supplements held with no dates read from them.

        A visible category, like :meth:`unattached`, and not a discard pile. A
        supplement discovered in a State's list and never opened is a real
        document that exists; what is unknown is when it starts and stops.
        """
        replaced = self.superseded
        return tuple(
            s
            for s in self.supplements
            if s.identifier not in replaced
            and s.state_on(date.min) is ForcePeriod.UNDATED
        )

    def not_known_to_have_ended(self, on: date) -> tuple[Supplement, ...]:
        """Everything a screening path must consider: in force, or unreadable.

        The union of :meth:`in_force` and :meth:`of_unread_window`. A
        supplement whose dates nobody transcribed is one nobody can say has
        ended, so retiring it from a screen would take a restriction that may
        still be in force off an operator's display on a day nothing happened.
        Erring the other way shows a document that may have lapsed, which a
        reader can check.
        """
        replaced = self.superseded
        return tuple(
            s
            for s in self.supplements
            if s.identifier not in replaced
            and s.state_on(on).applies is not False
        )

    def section_wide(
        self, on: date
    ) -> tuple[tuple[Supplement, ForcePeriod], ...]:
        """Supplements naming a section and no object, in force on this day.

        The half a lookup by object cannot reach. A State that supersedes the
        whole of ENR 5.1 for a month names no danger area in the heading, so
        every ``at()`` call returns nothing and the document disappears — from
        a dossier that is meanwhile drawing every area the section published.
        Reaching them takes a query that starts from the absence of a subject.
        """
        replaced = self.superseded
        out: list[tuple[Supplement, ForcePeriod]] = []
        for supplement in self.supplements:
            if supplement.identifier in replaced:
                continue
            if supplement.subjects or not supplement.section:
                continue
            period = supplement.state_on(on)
            if period.applies is not False:
                out.append((supplement, period))
        return tuple(out)

    def unattached(self) -> tuple[Supplement, ...]:
        """Supplements naming neither a section nor an object.

        A visible category, not a discard pile. These are real documents in
        force whose reach nobody has established, and a dossier that omitted
        them silently would read as complete.
        """
        return tuple(
            s for s in self.supplements if not s.subjects and not s.section
        )


# --------------------------------------------------------------------------
# Reading a supplement manifest
# --------------------------------------------------------------------------


def load_supplements(path: Path | str) -> SupplementRegister:
    """Read one supplement extract, with every entry cited to it."""
    path = Path(path)
    manifest = read_manifest(path)
    document = document_source(
        manifest.get("source"),
        base=path.parent,
        where=f"{path}: source",
        parser_id=SUPPLEMENT_PARSER_ID,
    )
    default_region = str(manifest.get("region", "")).strip()

    rows = manifest.get("supplements", [])
    if not isinstance(rows, list):
        raise ManifestError(f"{path}: supplements must be a list")

    held: list[Supplement] = []
    for index, row in enumerate(rows):
        where = f"{path}: supplements[{index}]"
        if not isinstance(row, Mapping):
            raise ManifestError(f"{where}: must be an object")
        locator = str(row.get("locator", "")).strip()
        if not locator:
            raise ManifestError(
                f"{where}: locator is required — where in the supplement this "
                "was read from."
            )
        try:
            supersession = Supersession(
                str(row.get("supersession", Supersession.NOT_STATED.value))
                .strip()
                .lower()
            )
        except ValueError:
            raise ManifestError(
                f"{where}: supersession must be one of "
                f"{', '.join(s.value for s in Supersession)}"
            ) from None
        try:
            held.append(
                Supplement(
                    identifier=str(row.get("identifier", "")),
                    source=sub_source(document, locator),
                    section=str(row.get("section", "")),
                    subjects=tuple(str(s) for s in row.get("subjects", [])),
                    effective_from=to_date(
                        row.get("effective_from"), where=where, field="effective_from"
                    ),
                    effective_to=to_date(
                        row.get("effective_to"), where=where, field="effective_to"
                    ),
                    supersession=supersession,
                    summary=str(row.get("summary", "")).strip(),
                    replaces=str(row.get("replaces", "")).strip(),
                    region=str(row.get("region", default_region)),
                    remarks=str(row.get("remarks", "")).strip(),
                )
            )
        except (ValueError, TypeError) as error:
            raise ManifestError(f"{where}: {error}") from None

    return SupplementRegister(supplements=tuple(held))


_SUPPLEMENT_TEMPLATE = {
    "source": {
        "source_id": "",
        "document": "",
        "document_path": "",
        "retrieved_at": "",
        "published_at": "",
        "original_url": "",
    },
    "region": "",
    "supplements": [
        {
            "identifier": "",
            "section": "",
            "subjects": [],
            "effective_from": "",
            "effective_to": "",
            "supersession": "not_stated",
            "summary": "",
            "replaces": "",
            "remarks": "",
            "locator": "",
        }
    ],
}


def supplement_template() -> str:
    """A blank supplement extract.

    ``subjects`` are entity keys in the same space everything else uses —
    ``AIRSPACE:AR-7``, ``ATS:UM688``, ``NAVAID:ALP``, ``FIX:KUKLA`` — and they
    are what lets a supplement reach the thing it is about rather than sitting
    in a list of documents. ``section`` catches the other half: a supplement
    may name no object and still replace ENR 5.1 for a month.

    ``summary`` is the State's own words and nothing reads further into it.
    Transcribing "upper limit becomes FL500" into a number here would be a
    derived value carrying a citation, which is the most convincing kind of
    wrong. What a supplement does downstream is reopen the question, not
    answer it.

    ``effective_to`` is left empty for one running until further notice — that
    is reported as open-ended, which is different from a dated window and
    different again from one nobody transcribed.
    """
    return json.dumps(_SUPPLEMENT_TEMPLATE, indent=2)

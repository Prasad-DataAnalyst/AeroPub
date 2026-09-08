"""What a State publishes, discovered by following its own links.

A State's AIP is a graph, not a path. The tempting model is arithmetic — take
the AIRAC cycle, build the URL, fetch it — and it is wrong in a way that fails
silently. Qatar's current edition sits under::

    /AIP/03-SEP-2026/AIP-30/2026-10-01-000000/html/

Three of those four fields fall out of the calendar. ``AIP-30`` does not: it is
a running amendment count that only Qatar knows. A constructed URL is therefore
a guess, and the failure mode is not a 404 you would notice — it is fetching
last cycle's edition and reporting success.

So exactly one address is treated as known: the State's entry point. Everything
else is reached by following a link on a page the State served. That is the
part least likely to change, and the part that is checkable when it does.

Three states, everywhere
------------------------
Discovery is full of things that may be true, false, or simply not said, and
this module never collapses the third into either of the others:

* :attr:`EditionStatus.UNDECLARED` — the State did not label this edition. It
  is not ``CURRENT`` by default, because an edition wrongly believed current is
  how an operator flies against withdrawn data.
* :attr:`Kind.UNKNOWN` — a document was found and its kind could not be
  determined. It is not an AIP section by default, because
  :class:`~aeropub.facts.Precedence` is read from the kind, and defaulting to
  ``AIP`` is defaulting to *the layer everything else overrides*.
* :meth:`Edition.in_force_on` returns ``None`` where neither a declaration nor
  a date settles it, rather than ``False``.

Why the kind is load-bearing
----------------------------
Precedence runs AIP < AMDT < SUP < NOTAM. A supplement in force changes what an
AIP section means. Discovering a supplement and filing it as an AIP section
does not merely mislabel it — it puts it *underneath* the thing it is supposed
to override, and the section it supersedes goes on reading as though nothing
had happened.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from enum import Enum

from .facts import Precedence

__all__ = [
    "Kind",
    "EditionStatus",
    "Edition",
    "Publication",
    "kind_of",
]


class Kind(str, Enum):
    """What a discovered document is.

    Determines precedence, so an unrecognised document is ``UNKNOWN`` and
    never quietly an AIP section.
    """

    AIP_SECTION = "AIP_SECTION"
    AMENDMENT = "AMENDMENT"
    SUPPLEMENT = "SUPPLEMENT"
    CIRCULAR = "CIRCULAR"
    CHART = "CHART"
    """A graphic. Read by people, not parsed for values, and never archived."""

    NAVIGATION = "NAVIGATION"
    """An index, menu or frameset — structure, not content."""

    UNKNOWN = "UNKNOWN"
    """Found, kind undetermined. Reported; never given a precedence."""

    @property
    def precedence(self) -> Precedence | None:
        """Which layer this document's values belong to.

        ``None`` where the document carries no values, or where its kind was
        not determined. A caller that needs a precedence must handle ``None``
        rather than receive a default that would file the document under the
        layer everything else overrides.
        """
        return {
            Kind.AIP_SECTION: Precedence.AIP,
            Kind.AMENDMENT: Precedence.AMDT,
            Kind.SUPPLEMENT: Precedence.SUP,
        }.get(self)

    @property
    def carries_values(self) -> bool:
        """Whether facts are drawn from this, and so whether it must be kept."""
        return self.precedence is not None


class EditionStatus(str, Enum):
    """What the State says an edition is.

    Read from the State's own markup where it declares one. Qatar files each
    edition under ``current-issues-table``, ``next-issues-table`` or
    ``archived-issues-table``, which is better evidence than any date
    arithmetic — and stays right when a State republishes out of order.
    """

    CURRENT = "CURRENT"
    NEXT = "NEXT"
    EXPIRED = "EXPIRED"
    UNDECLARED = "UNDECLARED"
    """The State did not say. Never treated as CURRENT."""


#: What a document's URL says it is. Ordered: the first match wins, and the
#: supplement and circular directories are tested before the section shape,
#: because ``eSUP/QA-SUP-16-2026-en-GB.html`` contains neither GEN, ENR nor AD
#: but a State that named one ``ENR-SUP`` would otherwise land as a section.
_KIND_BY_PATH: tuple[tuple[Kind, re.Pattern[str]], ...] = (
    (Kind.CHART, re.compile(r"\.(?:pdf|png|jpe?g|gif|svg|tiff?)$", re.I)),
    (Kind.SUPPLEMENT, re.compile(r"(?:^|/)e?SUPs?/|(?:^|[-_/])SUP[-_ ]?\d", re.I)),
    (Kind.CIRCULAR, re.compile(r"(?:^|/)e?AICs?/|(?:^|[-_/])AIC[-_ ]?\d", re.I)),
    (Kind.AMENDMENT, re.compile(r"(?:^|[-_/])AMDT(?:[-_ ]|\.html?$)", re.I)),
    (
        Kind.NAVIGATION,
        re.compile(r"(?:index|menu|frameset|toc|contents|history|banner|search|cover)", re.I),
    ),
    (Kind.AIP_SECTION, re.compile(r"(?:^|[-_/])(?:GEN|ENR|AD)[-_ ]?\d", re.I)),
)


def kind_of(url: str) -> Kind:
    """What a document at this URL is, from its path.

    A chart is decided by extension before anything else: ``eSUP/chart.pdf`` is
    a graphic that happens to live beside supplements, and reading it as a
    supplement would file an image under a precedence layer.
    """
    path = url.split("?", 1)[0].split("#", 1)[0]
    for kind, pattern in _KIND_BY_PATH:
        if pattern.search(path):
            return kind
    return Kind.UNKNOWN


@dataclass(frozen=True, slots=True)
class Edition:
    """One issue of a State's AIP, as the State presents it."""

    index_url: str
    status: EditionStatus = EditionStatus.UNDECLARED
    label: str = ""
    """The State's own words, e.g. ``"AIRAC AIP AMDT 01/2026"``."""

    effective_on: date | None = None
    published_on: date | None = None

    def __post_init__(self) -> None:
        if not self.index_url.strip():
            raise ValueError("Edition.index_url must be a non-empty string")

    def in_force_on(self, day: date) -> bool | None:
        """Whether this edition is in force on ``day``.

        ``None`` where nothing settles it — no declaration and no effective
        date. The caller must not read that as "no": an edition of unknown
        status is a reason to stop and look, not a reason to skip.
        """
        if self.status is EditionStatus.CURRENT:
            return True
        if self.status is EditionStatus.EXPIRED:
            return False
        if self.status is EditionStatus.NEXT and self.effective_on is not None:
            return day >= self.effective_on
        if self.effective_on is None:
            return None
        return day >= self.effective_on

    def describe(self) -> str:
        when = self.effective_on.isoformat() if self.effective_on else "effective ?"
        return f"[{self.status.value}] {when}  {self.label or self.index_url}"


@dataclass(frozen=True, slots=True)
class Publication:
    """One document a State publishes, found by following a link to it."""

    url: str
    kind: Kind
    edition: Edition
    code: str = ""
    """The section or document reference, e.g. ``"ENR 3.2"``, ``"SUP 16/2026"``."""

    title: str = ""

    def __post_init__(self) -> None:
        if not self.url.strip():
            raise ValueError("Publication.url must be a non-empty string")

    @property
    def precedence(self) -> Precedence | None:
        return self.kind.precedence

    @property
    def must_be_kept(self) -> bool:
        """Whether losing this would leave a value uncitable.

        True exactly when values are drawn from it. A chart is not kept; the
        section a runway length was read from is.
        """
        return self.kind.carries_values

    def cite_as(self, state_name: str) -> str:
        """How a value from this document names its source.

        The document, not the file. A citation naming a URL tells a reader
        where we got it; a citation naming "AIP Qatar ENR 3.2" tells them what
        it is, which is what an auditor asks for.
        """
        parts = [f"AIP {state_name}" if self.kind is Kind.AIP_SECTION else state_name]
        # A citation naming only the State identifies nothing — every value we
        # ever read from Qatar would carry the same one. Where neither a code
        # nor a title was determined, the filename is a poor name but a real
        # one, and a reader can resolve it.
        parts.append(
            self.code
            or self.title
            or self.url.rsplit("/", 1)[-1].split("?")[0].split("#")[0]
        )
        return " ".join(p for p in parts if p)

    @property
    def is_identified(self) -> bool:
        """Whether this document is named by something better than its filename.

        False is not an error — a State may publish a document whose reference
        is only inside it. It is a signal that the citation will read as a
        filename until the document is opened.
        """
        return bool(self.code or self.title)

    def describe(self) -> str:
        layer = self.precedence.name if self.precedence else "no precedence"
        return f"{self.kind.value:12} {layer:14} {self.code or self.title or self.url}"

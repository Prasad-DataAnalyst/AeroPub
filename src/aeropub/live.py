"""Reading a State's publication where it lives, and deciding what to keep.

The platform reads a State's website rather than a downloaded copy, and points
the user at the State's own document rather than serving a duplicate of it. A
chart belongs to the authority that drew it, and an operator opening it should
be looking at the authority's copy, not ours.

That settles where a document is *read* and where a user is *sent*. It does not
settle what we keep, and those are different questions with different answers.

What keeping is for
-------------------
Not convenience. Three things depend on holding the bytes we parsed:

* **Change detection.** :mod:`aeropub.changes` says what moved between one
  AIRAC cycle and the next by comparing them. With nothing held there is no
  previous cycle to compare against, and the question cannot be asked at all.
* **Citation.** A value's :class:`~aeropub.provenance.SourceRef` names a
  content hash. Resolving that hash back to what was actually parsed is what
  makes the value defensible years later.
* **Answering afterwards.** "What did the AIP say on the day of the event" is
  the question an investigation asks, and it is asked long after the State has
  moved on.

Why a link is not a substitute
------------------------------
Qatar's eAIP history page lists its expired issues as ``NIL``. It serves the
current edition and the next one; everything before that is gone. That is not
unusual and it is not a criticism — a State publishes what is in force, and
Annex 15 asks it to withdraw what is not. But it means a citation that resolves
only by fetching the State's URL resolves for one AIRAC cycle and then stops.
Every value cited against AIP-30 becomes unverifiable the day AIP-31 publishes.

What it costs
-------------
Measured on Qatar's own pages: eAIP markup gzips to about 14% of its size. At
100 KB a section, 80 sections a State, 180 States and 13 cycles a year, keeping
every cycle in full is roughly 3.4 GB a year, and a cycle changes well under a
tenth of its sections, so with content-addressed deduplication the new bytes
run nearer 0.35 GB a year. The audit trail is not what makes a corpus large.

Charts and imagery are. They are already-compressed formats — the images in
this project's own fixtures gzip to between 88% and 100% of their size — they
do not deduplicate between cycles, and nobody wants our copy of a chart in
preference to the State's. Those are linked, never held.

Between the two sits a document we parsed but should not keep whole: a
supplement published as PDF. Keeping the megabytes is waste; keeping nothing
makes the facts we drew from it unverifiable. So the text we extracted is kept
and the original is linked, and :class:`Retention` says which of the three
happened rather than leaving a caller to assume.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

__all__ = [
    "Retention",
    "DocumentLink",
    "retention_for",
    "KEEP_WHOLE",
    "KEEP_EXTRACT",
]


class Retention(str, Enum):
    """What was kept of a document, and therefore what it can still answer.

    Three states rather than kept/not-kept. The middle one is the common case
    for a PDF supplement and it behaves like neither of the others: the facts
    stay verifiable, the original does not.
    """

    ARCHIVED = "ARCHIVED"
    """The bytes we parsed are held. Every question above can be answered."""

    EXTRACT_ONLY = "EXTRACT_ONLY"
    """The text we extracted is held; the original is a link.

    A citation resolves to what the parser read. The document as the State laid
    it out — pagination, figures, signatures — is not recoverable once the
    State withdraws it.
    """

    LINKED = "LINKED"
    """Nothing is held. The citation resolves only while the State serves it.

    Correct for a chart, and never correct for something a value was drawn
    from. :attr:`DocumentLink.citation_expires_with_the_state` is true here,
    and it is the caller's job to not draw facts from such a document.
    """


#: Media types whose bytes are kept whole: the markup and data formats a
#: parser reads. Small, highly compressible, and deduplicating between cycles.
KEEP_WHOLE: frozenset[str] = frozenset(
    {
        "text/html",
        "application/xhtml+xml",
        "text/xml",
        "application/xml",
        "application/json",
        "application/gml+xml",
        "text/plain",
        "text/csv",
    }
)

#: Media types we read for facts but do not keep whole. The extracted text is
#: archived in place of the original, which stays a link.
KEEP_EXTRACT: frozenset[str] = frozenset(
    {
        "application/pdf",
        "application/msword",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    }
)


def retention_for(media_type: str, *, carries_values: bool) -> Retention:
    """What to keep of a document of this type.

    ``carries_values`` is a property of the *document*, not of whether a parser
    happened to run. That distinction cost a design pass to find: keying this
    on "was it parsed" meant a State onboarded before its profile was written
    archived nothing at all, and when the profile finally arrived there was no
    history to read it against. A section is kept because it is a section.

    Two things need the bytes and only one of them is parsing. The other is
    change detection: a hash says *that* a section changed, and only the
    previous bytes say *what* changed — which is the question an AIRAC diff
    exists to answer.

    A document that carries no values — a chart, a navigation page — is linked
    whatever its type. There is no citation to protect and no diff anyone will
    ask for, so holding it would be storage spent on nothing.

    An unrecognised media type that was parsed is archived whole rather than
    linked. Guessing wrong towards keeping costs bytes; guessing wrong towards
    discarding costs the citation, and only one of those is recoverable.
    """
    if not carries_values:
        return Retention.LINKED
    normalised = media_type.split(";")[0].strip().lower()
    if normalised in KEEP_EXTRACT:
        return Retention.EXTRACT_ONLY
    return Retention.ARCHIVED


@dataclass(frozen=True, slots=True)
class DocumentLink:
    """A document the application points at, and what it kept of it.

    This is what the user is offered when they ask to see the source: the
    State's own URL. It carries the retention alongside, because "open the
    official document" and "this is the copy we parsed" are different promises
    and a screen that blurs them misleads.
    """

    url: str
    document: str
    """What to cite it as, e.g. ``"AIP Qatar SUP 16/2026"``."""

    media_type: str
    retrieved_at: datetime
    retention: Retention
    content_hash: str | None = None
    """SHA-256 of what we read, whether or not we kept it.

    Recorded even for a linked document, because it is what detects a State
    republishing under the same URL. Absent only where the document was never
    read — a chart named in a page and never opened.
    """

    archive_key: str | None = None
    """Where the kept bytes are. ``None`` for :attr:`Retention.LINKED`."""

    size: int | None = None

    def __post_init__(self) -> None:
        if not self.url.strip():
            raise ValueError("DocumentLink.url must be a non-empty string")
        if not self.document.strip():
            raise ValueError("DocumentLink.document must be a non-empty string")
        if self.retention is Retention.LINKED and self.archive_key:
            raise ValueError(
                f"{self.document}: retention is LINKED but an archive key is "
                "set. One of the two is wrong, and a citation that claims a "
                "copy it does not have is the more dangerous way round."
            )
        if self.retention is not Retention.LINKED and not self.archive_key:
            raise ValueError(
                f"{self.document}: retention is {self.retention.value} but no "
                "archive key is set. Nothing was kept, so the retention is "
                "overstating what this citation can resolve."
            )

    @property
    def citation_expires_with_the_state(self) -> bool:
        """Whether this stops resolving once the State withdraws the document.

        True for a linked document, and the reason a value must never be drawn
        from one. Qatar's expired issues list is ``NIL``: withdrawal is a
        single AIRAC cycle away, not a distant hypothetical.
        """
        return self.retention is Retention.LINKED

    @property
    def holds_the_original_layout(self) -> bool:
        """Whether the document can be reproduced as the State laid it out."""
        return self.retention is Retention.ARCHIVED

    def describe(self) -> str:
        held = {
            Retention.ARCHIVED: "archived whole",
            Retention.EXTRACT_ONLY: "extracted text kept, original linked",
            Retention.LINKED: "linked only, nothing kept",
        }[self.retention]
        return f"{self.document} — {held}\n  {self.url}"

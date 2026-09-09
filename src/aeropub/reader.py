"""Reading a discovered publication: bytes in, cited facts out.

:mod:`aeropub.resolve` finds what a State publishes. This reads one of those
documents and produces facts that carry a resolvable citation. It is the join,
and it is where three things that are easy to get quietly wrong are decided.

**The media type is determined, not assumed.** Retention depends on it — a PDF
keeps its extracted text, markup keeps its bytes, a chart keeps nothing — and a
type read off a URL extension is a guess. The server's own ``Content-Type`` is
used where there is one, the bytes are sniffed where there is not, and
:attr:`Retrieved.type_was_declared` says which happened rather than leaving a
caller to assume the first.

**A citation may not claim a copy that does not exist.** If a document's kind
says values are drawn from it, and nothing was archived, the facts still get
produced — refusing them would deny a State's data over a fault of ours — but
they are marked :attr:`~aeropub.provenance.Confidence.LOW` and the reason is
recorded. Qatar withdraws editions with no archive, so an unarchived citation
stops resolving within one AIRAC cycle, and a value that reads as HIGH while
resting on a link that will 404 is worse than no value.

**Nothing is parsed without a profile.** A document read with no profile yields
no facts and says so. It does not yield zero facts and report success, which is
the same output an empty page produces and the reason a coverage gap can hide
for a whole cycle.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

from .live import DocumentLink, Retention, retention_for
from .provenance import Confidence
from .publication import Kind, Publication

__all__ = [
    "Retrieved",
    "Retrieve",
    "Keep",
    "ReadResult",
    "media_type_of",
    "read_publication",
]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


#: Extensions whose type is unambiguous enough to stand in for a declaration.
_BY_EXTENSION: tuple[tuple[str, str], ...] = (
    (r"\.x?html?$", "text/html"),
    (r"\.xml$", "application/xml"),
    (r"\.json$", "application/json"),
    (r"\.pdf$", "application/pdf"),
    (r"\.png$", "image/png"),
    (r"\.jpe?g$", "image/jpeg"),
    (r"\.gif$", "image/gif"),
    (r"\.svg$", "image/svg+xml"),
    (r"\.te?xt$", "text/plain"),
    (r"\.csv$", "text/csv"),
)

#: Leading bytes that identify a format regardless of what anything claims.
_BY_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"%PDF-", "application/pdf"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF8", "image/gif"),
)


def media_type_of(url: str, body: bytes, declared: str = "") -> tuple[str, bool]:
    """``(media type, whether the server declared it)``.

    The bytes are consulted before the URL, and beat a declaration that
    contradicts them: a State serving a PDF as ``text/html`` is common enough,
    and archiving a PDF's bytes as though they were markup wastes the storage
    the retention policy exists to save.
    """
    for magic, media_type in _BY_MAGIC:
        if body.startswith(magic):
            return (media_type, bool(declared))

    if declared.strip():
        return (declared.split(";")[0].strip().lower(), True)

    path = url.split("?", 1)[0].split("#", 1)[0].lower()
    for pattern, media_type in _BY_EXTENSION:
        if re.search(pattern, path):
            return (media_type, False)

    head = body[:512].lstrip().lower()
    if head.startswith(b"<!doctype html") or head.startswith(b"<html"):
        return ("text/html", False)
    if head.startswith(b"<?xml"):
        return ("application/xml", False)
    return ("application/octet-stream", False)


@dataclass(frozen=True, slots=True)
class Retrieved:
    """One document as it came back."""

    url: str
    body: bytes
    media_type: str
    type_was_declared: bool
    retrieved_at: datetime = field(default_factory=_utcnow)
    unchanged: bool = False
    """The server answered 304. ``body`` is empty and carries no meaning.

    A conditional request is how checking the world stays affordable — most
    checks find nothing new — but the empty body it returns is a trap. Hashing
    it would record every unchanged document as having changed to nothing.
    """

    @property
    def content_hash(self) -> str:
        """SHA-256 of exactly what we read, lowercase hex."""
        if self.unchanged:
            raise ValueError(
                f"{self.url} answered 'not modified', so there is no body to "
                "hash. The hash that still stands is the one recorded when it "
                "was last read — take that from the ledger rather than hashing "
                "an empty body."
            )
        return hashlib.sha256(self.body).hexdigest()

    @property
    def size(self) -> int:
        return len(self.body)


#: Fetching one document. Distinct from :data:`aeropub.resolve.Read`, which
#: returns bytes alone: traversal needs only the markup, whereas reading needs
#: the media type to decide retention.
Retrieve = Callable[[str], Retrieved]

#: Archiving bytes and returning the key that resolves them again. Injected so
#: reading is testable without a filesystem, and so a caller may decline to
#: archive — which is recorded rather than hidden.
Keep = Callable[[bytes, str], str]


@dataclass(frozen=True, slots=True)
class ReadResult:
    """What reading one publication produced, and what it did not."""

    publication: Publication
    link: DocumentLink
    facts: tuple = ()
    parsed: bool = False
    not_parsed_because: str = ""
    unchanged: bool = False
    """The server confirmed what we hold is current. Nothing was re-read.

    Distinct from every other not-parsed reason: those mean we do not have
    the values, this means we already do.
    """
    confidence: Confidence = Confidence.HIGH
    degraded_because: str = ""

    @property
    def citation_will_expire(self) -> bool:
        """Whether these facts stop being verifiable when the State moves on."""
        return self.link.citation_expires_with_the_state

    @property
    def is_usable_operationally(self) -> bool:
        """Whether a value from this should drive anything without review.

        False where the citation cannot be resolved later. That is not a
        judgement about the State's data — it is a judgement about ours.

        An unchanged document is usable: what we hold was confirmed current,
        and it was archived when it was read.
        """
        if self.unchanged:
            return True
        return (
            self.parsed
            and self.confidence is not Confidence.LOW
            and not self.citation_will_expire
        )

    def describe(self) -> str:
        lines = [self.link.describe()]
        if self.unchanged:
            lines.append("  unchanged — what we hold was confirmed current")
        elif self.parsed:
            lines.append(f"  {len(self.facts)} facts, confidence {self.confidence.value}")
        else:
            lines.append(f"  not parsed — {self.not_parsed_because}")
        if self.degraded_because:
            lines.append(f"  ! {self.degraded_because}")
        return "\n".join(lines)


def read_publication(
    publication: Publication,
    retrieve: Retrieve,
    *,
    state_name: str = "",
    parse: Callable[[Retrieved, Publication], tuple] | None = None,
    keep: Keep | None = None,
) -> ReadResult:
    """Read one document, keep what should be kept, and parse it if we can.

    ``parse`` is injected rather than chosen here: which parser applies is a
    property of the State's profile, and this module has no business deciding
    it. Passing ``None`` reads and archives without extracting, which is what
    a chart or an unprofiled section wants.

    ``state_name`` names the authority in the citation. Left empty the
    citation still identifies the document, but a reader of it has to know
    already which State it came from — so a caller with the name should pass
    it.

    ``keep`` returns an archive key. Passing ``None`` archives nothing — legal,
    and reported: a document whose kind carries values then produces facts at
    ``LOW`` confidence with the reason recorded, because its citation will stop
    resolving as soon as the State withdraws the edition.
    """
    got = retrieve(publication.url)

    if got.unchanged:
        # The healthiest answer a server gives, and the commonest. There is
        # no body to hash, nothing to archive and nothing to parse — the copy
        # we already hold is confirmed current, which is the whole point of
        # asking conditionally. Treating this as a failure, which hashing an
        # absent body would, turns a working feed into a permanently
        # incomplete one.
        return ReadResult(
            publication=publication,
            link=DocumentLink(
                url=got.url,
                document=publication.cite_as(state_name),
                media_type=got.media_type,
                retrieved_at=got.retrieved_at,
                retention=Retention.LINKED,
                content_hash=None,
            ),
            parsed=False,
            not_parsed_because="not modified since it was last read",
            unchanged=True,
        )

    will_parse = parse is not None and publication.kind.carries_values
    # Retention follows the document, not the parser. A section with no
    # profile yet is still the baseline the next cycle's diff is read against,
    # so it is kept whether or not anything read values out of it today.
    retention = retention_for(
        got.media_type, must_be_kept=publication.kind.must_be_kept
    )

    archive_key: str | None = None
    degraded = ""
    if retention is not Retention.LINKED:
        if keep is None:
            # Nothing to archive into. The document is still read and still
            # parsed — a storage fault of ours must not deny a State's data —
            # but the record says the copy does not exist.
            retention = Retention.LINKED
            degraded = (
                "nothing was archived, so this citation resolves only while "
                "the State serves the URL"
            )
        else:
            archive_key = keep(got.body, got.media_type)
            if not archive_key:
                retention = Retention.LINKED
                degraded = "the archive returned no key; the copy was not kept"

    link = DocumentLink(
        url=got.url,
        document=publication.cite_as(state_name),
        media_type=got.media_type,
        retrieved_at=got.retrieved_at,
        retention=retention,
        content_hash=got.content_hash,
        archive_key=archive_key,
        size=got.size,
    )

    if not will_parse:
        return ReadResult(
            publication=publication,
            link=link,
            parsed=False,
            not_parsed_because=(
                "no parser was given"
                if parse is None
                else f"a {publication.kind.value} carries no values"
            ),
            degraded_because=degraded,
        )

    facts = tuple(parse(got, publication))
    return ReadResult(
        publication=publication,
        link=link,
        facts=facts,
        parsed=True,
        confidence=Confidence.LOW if degraded else Confidence.HIGH,
        degraded_because=degraded,
    )

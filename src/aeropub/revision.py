"""What changed inside a document, when no parser has read it yet.

:mod:`aeropub.changes` compares *effective states* — the AIP/AMDT/SUP/NOTAM
stack resolved on a date in each cycle — and that is the comparison that tells
the operational truth. It has one blind spot, and it is not small: a document
nothing parses produces no facts, so a State can amend ENR 3.2, the cycle can
archive both versions, and the change board can say nothing changed.

Most of an AIP is in that state for most States, and will be for years. So this
is the other comparison: two archived versions of one document, read as text.
It says less than a fact diff and it says it about everything.

Byte-changed is not text-changed
--------------------------------
A State regenerating its eAIP rewrites timestamps, element ids and whitespace
across every page. Every hash moves. A change detector built on hashes alone
then reports eighty sections amended on a day nothing was amended, and an
operator who sees that twice stops reading the board — which costs more than
the feature was worth.

So :class:`DocumentRevision` separates them. Text identical and bytes different is
``REGENERATED`` and says so plainly. It is the commonest kind of change in an
eAIP and the one that must never be dressed up as an amendment.

What it does not claim
----------------------
That a text change is operationally significant, or that an unchanged text
means an unchanged meaning. A table whose cells are reordered reads as changed;
a figure replaced with a different figure of the same caption reads as
unchanged. This is evidence for a person, and where a parser exists its facts
are the better answer — :attr:`DocumentRevision.facts_were_read` says which case a
reader is in rather than leaving them to assume.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from enum import Enum

__all__ = ["RevisionKind", "DocumentRevision", "extract_text", "compare"]

#: Markup that carries no reading text. Removed before comparison, or a page
#: whose stylesheet changed would read as an amended page.
_INVISIBLE = re.compile(
    r"<(script|style|head)\b[^>]*>.*?</\1>", re.I | re.S
)
_TAG = re.compile(r"<[^>]+>")
_ENTITY = {
    "&nbsp;": " ", "&amp;": "&", "&lt;": "<", "&gt;": ">",
    "&quot;": '"', "&#39;": "'", "&apos;": "'",
}


class RevisionKind(str, Enum):
    """What kind of change one document underwent."""

    AMENDED = "AMENDED"
    """The reading text differs. Something was published."""

    REGENERATED = "REGENERATED"
    """The bytes differ and the text does not.

    An eAIP rebuilt with new element ids and timestamps. The commonest kind of
    byte change in a State that regenerates its whole AIP each cycle, and never
    to be reported as an amendment.
    """

    UNCHANGED = "UNCHANGED"
    """Byte-identical."""

    FIRST_SEEN = "FIRST_SEEN"
    """No previous version held. Not a change — there is nothing to compare.

    Distinct from UNCHANGED, because "we have never seen this before" and "this
    is the same as last time" mean opposite things about coverage.
    """


def extract_text(html: bytes | str) -> tuple[str, ...]:
    """The reading text of a page, as lines, with markup and scripts removed.

    Whitespace is collapsed and blank lines dropped, because an eAIP's
    indentation changes whenever its generator does and none of it is
    aeronautical information.
    """
    if isinstance(html, bytes):
        html = html.decode("utf-8", "replace")
    html = _INVISIBLE.sub(" ", html)
    html = re.sub(r"<br\s*/?>|</(p|div|tr|li|h[1-6])>", "\n", html, flags=re.I)
    html = _TAG.sub(" ", html)
    for entity, char in _ENTITY.items():
        html = html.replace(entity, char)
    html = re.sub(r"&#(\d+);", lambda m: chr(int(m.group(1))), html)
    lines = (" ".join(line.split()) for line in html.split("\n"))
    return tuple(line for line in lines if line)


@dataclass(frozen=True, slots=True)
class DocumentRevision:
    """How one document differs from the version we held before it."""

    document: str
    kind: RevisionKind
    added: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    lines_before: int = 0
    lines_after: int = 0
    facts_were_read: bool = False
    """Whether a parser read this document.

    False means this text comparison is all the evidence there is. A reader
    must be able to tell that from a document whose facts were extracted and
    whose values can be diffed properly.
    """

    @property
    def is_substantive(self) -> bool:
        """Whether the reading text moved at all."""
        return self.kind is RevisionKind.AMENDED

    @property
    def net_lines(self) -> int:
        return self.lines_after - self.lines_before

    @property
    def needs_a_person(self) -> bool:
        """Whether a human must look before this is understood.

        True for every amendment nothing parsed. The text says *that* something
        changed and roughly where; only a reader or a parser says what it means
        operationally.
        """
        return self.is_substantive and not self.facts_were_read

    def describe(self, limit: int = 8) -> str:
        if self.kind is RevisionKind.FIRST_SEEN:
            return f"{self.document}: first seen, nothing to compare against"
        if self.kind is RevisionKind.UNCHANGED:
            return f"{self.document}: unchanged"
        if self.kind is RevisionKind.REGENERATED:
            return (
                f"{self.document}: regenerated — every byte moved, not one "
                "word did"
            )

        lines = [
            f"{self.document}: AMENDED  "
            f"(+{len(self.added)} −{len(self.removed)} lines, "
            f"net {self.net_lines:+d})"
        ]
        for line in self.removed[:limit]:
            lines.append(f"  − {line[:100]}")
        if len(self.removed) > limit:
            lines.append(f"  − and {len(self.removed) - limit} more")
        for line in self.added[:limit]:
            lines.append(f"  + {line[:100]}")
        if len(self.added) > limit:
            lines.append(f"  + and {len(self.added) - limit} more")
        if self.needs_a_person:
            lines.append(
                "  No parser read this, so no values moved. The change is "
                "real and unread."
            )
        return "\n".join(lines)


def compare(
    document: str,
    before: bytes | None,
    after: bytes,
    *,
    facts_were_read: bool = False,
) -> DocumentRevision:
    """How ``after`` differs from ``before``.

    ``before`` is ``None`` where no earlier version is held, which is
    :attr:`RevisionKind.FIRST_SEEN` and not a change.
    """
    if before is None:
        return DocumentRevision(
            document=document,
            kind=RevisionKind.FIRST_SEEN,
            lines_after=len(extract_text(after)),
            facts_were_read=facts_were_read,
        )

    if before == after:
        text = extract_text(after)
        return DocumentRevision(
            document=document, kind=RevisionKind.UNCHANGED,
            lines_before=len(text), lines_after=len(text),
            facts_were_read=facts_were_read,
        )

    old, new = extract_text(before), extract_text(after)
    if old == new:
        return DocumentRevision(
            document=document, kind=RevisionKind.REGENERATED,
            lines_before=len(old), lines_after=len(new),
            facts_were_read=facts_were_read,
        )

    added: list[str] = []
    removed: list[str] = []
    for line in difflib.unified_diff(old, new, n=0, lineterm=""):
        if line.startswith("+++") or line.startswith("---") or line.startswith("@@"):
            continue
        if line.startswith("+"):
            added.append(line[1:])
        elif line.startswith("-"):
            removed.append(line[1:])

    return DocumentRevision(
        document=document,
        kind=RevisionKind.AMENDED,
        added=tuple(added),
        removed=tuple(removed),
        lines_before=len(old),
        lines_after=len(new),
        facts_were_read=facts_were_read,
    )

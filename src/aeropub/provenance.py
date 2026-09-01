"""Provenance — where a value came from.

Every value the platform holds carries a :class:`SourceRef`. This is not
metadata bolted on afterwards; it is part of the value's identity, and the
model is built so that a value cannot exist without one. A reviewer must be
able to ask any number on any screen where it came from and get a complete
answer, and an auditor must be able to resolve that answer years later.

The fields below are the minimum that makes a value defensible: which
authority published it, which document and where inside it, when we read it,
and a hash proving what we read has not changed underneath us since.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum

__all__ = ["Confidence", "SourceRef"]

_SHA256 = re.compile(r"\A[0-9a-f]{64}\Z")


class Confidence(str, Enum):
    """How much to trust an extraction.

    ``LOW`` does not mean the value is wrong — it means a human should look
    before it drives anything operational. The review gate reads this.
    """

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


@dataclass(frozen=True, slots=True)
class SourceRef:
    """A complete, resolvable citation for one extracted value."""

    source_id: str
    """Which State, authority or provider. e.g. ``"QA-CAA"``, ``"FAA"``."""

    document: str
    """The publication. e.g. ``"AIP AD 2.13"``, ``"NOTAM A2291/26"``."""

    locator: str
    """Where inside it — section, page, table cell, XPath. Not the whole file."""

    retrieved_at: datetime
    """When we read it. Makes staleness visible rather than implicit."""

    content_hash: str
    """SHA-256 of the raw artefact, lowercase hex.

    Proves what we parsed is what the archive holds. If a State silently
    republishes under the same URL, this is what catches it.
    """

    parser_id: str
    """Which extractor produced the value."""

    parser_version: str
    """Its version, so a parser defect traces to every value it touched."""

    confidence: Confidence = Confidence.HIGH

    published_at: date | None = None
    """When the State issued it. Unknown for some sources, hence optional."""

    original_url: str | None = None
    """Where it came from, if it had a URL."""

    archive_key: str | None = None
    """Key of the immutable archived copy, so the citation resolves later."""

    def __post_init__(self) -> None:
        for name in ("source_id", "document", "locator", "parser_id", "parser_version"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"SourceRef.{name} must be a non-empty string")

        if not isinstance(self.retrieved_at, datetime):
            raise TypeError("SourceRef.retrieved_at must be a datetime")
        if self.retrieved_at.tzinfo is None:
            # Aeronautical time is UTC. A naive timestamp is ambiguous, and
            # ambiguity in a citation defeats the point of having one.
            raise ValueError("SourceRef.retrieved_at must be timezone-aware (UTC)")

        if not isinstance(self.content_hash, str) or not _SHA256.match(self.content_hash):
            raise ValueError(
                "SourceRef.content_hash must be a lowercase hex SHA-256 digest"
            )

        if not isinstance(self.confidence, Confidence):
            raise TypeError("SourceRef.confidence must be a Confidence")

    def describe(self) -> str:
        """One line a reviewer can read without decoding field names."""
        when = self.retrieved_at.strftime("%d %b %Y %H%MZ")
        return (
            f"{self.document}, {self.locator} "
            f"(read {when}, {self.parser_id} {self.parser_version}, "
            f"confidence {self.confidence.value})"
        )

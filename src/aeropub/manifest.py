"""Citation manifests — how a document somebody read becomes cited data.

Most of the world's 180 States do not publish a machine-readable eAIP, and
several of those that do publish one no parser has been written against yet. A
platform that can only ingest what a parser understands is a platform that
holds nothing for most of the world, and reports it as a coverage gap forever.

A manifest is the other way in: a person reads the document, records the
figures, and the file they write is checked hard enough that the result is as
citable as a parsed one. It is not a shortcut around provenance — it is
provenance done by hand, and the format exists to make doing it wrong fail
rather than pass quietly.

What every manifest shares
--------------------------
One file describes **one document**. The document is named once at the top and
hashed as the file loads, so the citation cannot be written unless the document
is on disk to be hashed. Every value inside points at where in that document it
was found. Both halves matter: the hash proves *which* document, the locator
proves *where*, and a citation missing either is one a reviewer cannot resolve.

There is no partial success anywhere in this module. A manifest with one
uncitable value raises rather than loading the rest, because a store that is
nearly all cited is one people stop checking.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from pathlib import Path
from typing import Any, Mapping

from aeropub.provenance import Confidence, SourceRef

__all__ = [
    "ManifestError",
    "document_source",
    "read_manifest",
    "sha256_of",
    "sub_source",
    "to_date",
    "to_moment",
]


class ManifestError(ValueError):
    """The manifest cannot be read into cited values.

    Always names the file and the field. These are hand-written, and the
    person fixing one is usually not the person who wrote it.
    """


def sha256_of(path: Path | str) -> str:
    """Lowercase hex SHA-256 of a file, read in chunks."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def read_manifest(path: Path | str) -> dict[str, Any]:
    """Parse the manifest JSON, with the filename on any error."""
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise ManifestError(f"{path}: cannot be read — {error}") from None
    try:
        loaded = json.loads(text)
    except json.JSONDecodeError as error:
        raise ManifestError(f"{path}: not valid JSON — {error}") from None
    if not isinstance(loaded, dict):
        raise ManifestError(f"{path}: the manifest must be a JSON object")
    return loaded


def to_moment(text: Any, *, where: str) -> datetime:
    if not isinstance(text, str):
        raise ManifestError(f"{where}: retrieved_at must be an ISO-8601 string")
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise ManifestError(f"{where}: retrieved_at is not ISO-8601 — {error}") from None
    if moment.tzinfo is None:
        raise ManifestError(
            f"{where}: retrieved_at must carry a timezone. Aeronautical time is "
            "UTC, and an ambiguous timestamp defeats the point of a citation."
        )
    return moment


def to_date(text: Any, *, where: str, field: str) -> date | None:
    if text is None:
        return None
    if not isinstance(text, str):
        raise ManifestError(f"{where}: {field} must be an ISO-8601 date (YYYY-MM-DD)")
    try:
        return date.fromisoformat(text)
    except ValueError as error:
        raise ManifestError(f"{where}: {field} is not a date — {error}") from None


def document_source(
    block: Mapping[str, Any],
    *,
    base: Path,
    where: str,
    parser_id: str,
) -> SourceRef:
    """The citation for the document a manifest describes.

    ``document_path`` is hashed as it is read, which is the point of preferring
    it: the citation cannot be written unless the document is there to hash. A
    bare ``content_hash`` is accepted for a document held elsewhere, and then
    nothing verifies it — so where both are given and they disagree, that is an
    error rather than a preference, because the document on disk is not the one
    the manifest was written against.
    """
    if not isinstance(block, Mapping):
        raise ManifestError(f"{where}: source must be an object naming the document")
    for field in ("source_id", "document"):
        if not str(block.get(field, "")).strip():
            raise ManifestError(f"{where}: source.{field} is required")

    path = block.get("document_path")
    given = block.get("content_hash")
    if path:
        resolved = Path(path)
        if not resolved.is_absolute():
            resolved = base / resolved
        if not resolved.is_file():
            raise ManifestError(
                f"{where}: source.document_path {str(resolved)!r} is not a file. "
                "The hash is taken from the document itself, so it has to be "
                "where the manifest says it is."
            )
        content_hash = sha256_of(resolved)
        if given and str(given).lower() != content_hash:
            raise ManifestError(
                f"{where}: source.content_hash does not match {str(resolved)!r}. "
                "The document on disk is not the one this manifest was written "
                "against — re-read it before trusting the values."
            )
    elif given:
        content_hash = str(given).lower()
    else:
        raise ManifestError(
            f"{where}: give source.document_path so the document can be hashed as "
            "it is read, or source.content_hash where it lives elsewhere. A value "
            "whose document cannot be identified is not citable."
        )

    try:
        graded = Confidence(block.get("confidence", Confidence.HIGH.value))
    except ValueError:
        raise ManifestError(
            f"{where}: confidence must be one of "
            f"{', '.join(c.value for c in Confidence)}"
        ) from None

    try:
        return SourceRef(
            source_id=str(block["source_id"]).strip(),
            document=str(block["document"]).strip(),
            locator=str(block.get("locator", "")).strip() or "(whole document)",
            retrieved_at=to_moment(block.get("retrieved_at"), where=where),
            content_hash=content_hash,
            parser_id=str(block.get("parser_id") or parser_id),
            parser_version=str(block.get("parser_version") or "1"),
            confidence=graded,
            published_at=to_date(
                block.get("published_at"), where=where, field="source.published_at"
            ),
            original_url=block.get("original_url") or None,
        )
    except (ValueError, TypeError) as error:
        raise ManifestError(f"{where}: {error}") from None


def sub_source(document: SourceRef, locator: str) -> SourceRef:
    """The document's citation, pointed at one place inside it.

    Everything but the locator is carried through unchanged: the same document,
    the same hash, the same reading. Only *where* differs, which is the only
    thing that should.
    """
    return SourceRef(
        source_id=document.source_id,
        document=document.document,
        locator=locator,
        retrieved_at=document.retrieved_at,
        content_hash=document.content_hash,
        parser_id=document.parser_id,
        parser_version=document.parser_version,
        confidence=document.confidence,
        published_at=document.published_at,
        original_url=document.original_url,
    )

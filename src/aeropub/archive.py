"""The raw store — every artefact ever fetched, kept forever.

Content-addressed and append-only. A document is stored under the SHA-256 of
its bytes, so the same content fetched twice occupies one copy, and a citation
that names a hash can always be resolved back to exactly what was parsed.

**Nothing is ever deleted.** That is not a default to be revisited when storage
gets expensive; it is the capability. A State replaces its eAIP each cycle and
drops superseded NOTAM entirely, so the only copy of what was published on a
given day is the one we kept. An archive not kept cannot be recovered, which
makes pruning a one-way loss of the ability to answer "what did this say on the
day of the event" — the question an investigation actually asks.

Deduplication makes that affordable. Most checks find a document unchanged, and
an unchanged document costs nothing beyond the metadata recording that we saw
it again.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from aeropub.provenance import Confidence, SourceRef

__all__ = ["Archive", "ArchiveEntry", "digest_of"]


def digest_of(body: bytes) -> str:
    """The SHA-256 of ``body``, lowercase hex — an artefact's identity."""
    return hashlib.sha256(body).hexdigest()


@dataclass(frozen=True, slots=True)
class ArchiveEntry:
    """One artefact in the store, and where it came from."""

    digest: str
    source_id: str
    url: str
    retrieved_at: datetime
    size: int
    http_status: int | None = None
    content_type: str | None = None
    first_seen_at: datetime | None = None
    """When this exact content was first archived, if earlier than this fetch."""

    def to_source_ref(
        self,
        *,
        document: str,
        locator: str,
        parser_id: str,
        parser_version: str,
        confidence: Confidence = Confidence.HIGH,
    ) -> SourceRef:
        """The citation for a value extracted from this artefact."""
        return SourceRef(
            source_id=self.source_id,
            document=document,
            locator=locator,
            retrieved_at=self.retrieved_at,
            content_hash=self.digest,
            parser_id=parser_id,
            parser_version=parser_version,
            confidence=confidence,
            original_url=self.url,
            archive_key=self.digest,
        )


class Archive:
    """An append-only, content-addressed store on the filesystem."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)

    # -- layout ----------------------------------------------------------

    def _blob_path(self, digest: str) -> Path:
        # Two levels of fan-out: a flat directory of millions of files is
        # miserable on most filesystems and unusable to inspect by hand.
        return self.root / "blobs" / digest[:2] / digest[2:4] / digest

    def _meta_path(self, digest: str) -> Path:
        return self._blob_path(digest).with_suffix(".json")

    # -- writing ---------------------------------------------------------

    def put(
        self,
        body: bytes,
        *,
        source_id: str,
        url: str,
        retrieved_at: datetime,
        http_status: int | None = None,
        content_type: str | None = None,
    ) -> ArchiveEntry:
        """Store ``body`` and return its entry. Storing the same bytes is a no-op.

        The returned entry carries ``first_seen_at`` when this content was
        already held, which is how a caller distinguishes "unchanged" from
        "new" without re-reading the blob.
        """
        if retrieved_at.tzinfo is None:
            raise ValueError("retrieved_at must be timezone-aware (UTC)")

        digest = digest_of(body)
        blob = self._blob_path(digest)
        meta = self._meta_path(digest)

        if blob.exists():
            stored = json.loads(meta.read_text()) if meta.exists() else {}
            first_seen = stored.get("first_seen_at")
            return ArchiveEntry(
                digest=digest,
                source_id=source_id,
                url=url,
                retrieved_at=retrieved_at,
                size=len(body),
                http_status=http_status,
                content_type=content_type,
                first_seen_at=(
                    datetime.fromisoformat(first_seen) if first_seen else None
                ),
            )

        blob.parent.mkdir(parents=True, exist_ok=True)
        # Write to a temporary name and move into place, so a crash mid-write
        # cannot leave a truncated blob under a hash that claims to be complete.
        staging = blob.with_name(blob.name + ".partial")
        staging.write_bytes(body)
        staging.replace(blob)

        meta.write_text(
            json.dumps(
                {
                    "digest": digest,
                    "source_id": source_id,
                    "url": url,
                    "first_seen_at": retrieved_at.isoformat(),
                    "size": len(body),
                    "http_status": http_status,
                    "content_type": content_type,
                },
                indent=2,
            )
            + "\n"
        )

        return ArchiveEntry(
            digest=digest,
            source_id=source_id,
            url=url,
            retrieved_at=retrieved_at,
            size=len(body),
            http_status=http_status,
            content_type=content_type,
        )

    # -- reading ---------------------------------------------------------

    def has(self, digest: str) -> bool:
        return self._blob_path(digest).exists()

    def get(self, digest: str) -> bytes:
        """The exact bytes stored under ``digest``.

        Verifies the content still hashes to its own name. A mismatch means the
        store has been corrupted or tampered with, and a citation resolving to
        the wrong bytes is worse than one that fails.
        """
        path = self._blob_path(digest)
        if not path.exists():
            raise KeyError(f"nothing archived under {digest}")
        body = path.read_bytes()
        actual = digest_of(body)
        if actual != digest:
            raise ValueError(
                f"archive corruption: {digest} holds content hashing to {actual}"
            )
        return body

    def metadata(self, digest: str) -> dict:
        meta = self._meta_path(digest)
        if not meta.exists():
            raise KeyError(f"no metadata archived under {digest}")
        return json.loads(meta.read_text())

    def digests(self) -> Iterator[str]:
        """Every digest held, in no particular order."""
        blobs = self.root / "blobs"
        if not blobs.exists():
            return
        for path in blobs.rglob("*"):
            if path.is_file() and path.suffix != ".json" and not path.name.endswith(".partial"):
                yield path.name

    def __len__(self) -> int:
        return sum(1 for _ in self.digests())

    def total_bytes(self) -> int:
        blobs = self.root / "blobs"
        if not blobs.exists():
            return 0
        return sum(
            p.stat().st_size
            for p in blobs.rglob("*")
            if p.is_file() and p.suffix != ".json"
        )

    def verify(self) -> list[str]:
        """Digests whose stored bytes no longer hash to their name."""
        broken = []
        for digest in self.digests():
            try:
                self.get(digest)
            except ValueError:
                broken.append(digest)
        return broken

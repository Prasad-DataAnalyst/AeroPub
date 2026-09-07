"""Ingesting an initial load that somebody else fetched.

The connector's happy path is this platform calling the FAA. That path needs
outbound HTTPS to CGI Federal's hosts, and in a good many deployments — an
airline's segmented network, a build agent, a restricted analysis
environment — it does not exist and will not be granted quickly. The data is
not unavailable in those places; it is one machine away. An operator with a
normal connection can fetch the bundle in one command and hand the file over.

What that must not do is quietly become a second, unlabelled source of truth.
A bundle we fetched is citable to the FAA gateway at a moment we witnessed. A
bundle handed to us is citable to whoever handed it over, at a moment we did
not see, and the difference is the whole basis of a citation. So a relayed
bundle is archived and read exactly like a fetched one, and cited differently.

Two things stop it being trust
-------------------------------
The FAA's own wrapper carries both facts needed to check a file nobody here
watched arrive:

``numberReturned``
    The count the FAA states. Compared against what actually parses, so a
    truncated file — the commonest failure in a hand-carried transfer, and one
    that looks exactly like a quiet day — is refused rather than loaded.

``timeStamp``
    When the FAA generated the bundle. This is the citation's ``retrieved_at``,
    not the moment the file was read here: a bundle carried across a network
    boundary can be hours or days old, and dating it to its arrival would make
    stale NOTAM look fresh. Read from the file rather than typed in, so it
    cannot be fudged by the person doing the relaying.

A bundle whose wrapper states neither is not rejected — some are legitimately
partial — but it is reported as unverifiable, and it never reads as verified.
"""

from __future__ import annotations

import gzip
import hashlib
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aeropub.archive import Archive
from aeropub.faa.aixm import NotamFeed
from aeropub.faa.client import GZIP_MAGIC, InitialLoad

__all__ = [
    "RelayError",
    "RelayedLoad",
    "relay_initial_load",
    "STALE_AFTER",
]

RELAY_PARSER_ID = "aeropub.faa.relay"

#: How old a bundle may be before it is called out. NOTAM change continuously
#: and the FAA's own production cadence is a pull every three minutes, so a
#: day-old baseline is a different document from a current one.
STALE_AFTER = timedelta(hours=24)


class RelayError(ValueError):
    """The file cannot be accepted as an initial load."""


@dataclass(frozen=True, slots=True)
class RelayedLoad:
    """A bundle somebody else fetched, archived and checked."""

    load: InitialLoad
    """The bundle itself, in the same shape a fetched one takes, so every
    reader downstream is unchanged."""

    path: Path
    obtained_from: str = ""
    """Who relayed it. Provenance a person needs and no parser can supply."""

    read_at: datetime | None = None
    """When *we* read the file — distinct from when the FAA generated it."""

    generated_at: datetime | None = None
    """The FAA's own ``timeStamp``. The citation's retrieval moment."""

    claimed: int | None = None
    """``numberReturned``, as the FAA stated it."""

    parsed: int = 0

    @property
    def is_complete(self) -> bool | None:
        """Whether everything the FAA claimed actually parsed.

        ``None`` where the wrapper stated no count — unverifiable, which is
        not the same as verified.
        """
        if self.claimed is None:
            return None
        return self.parsed >= self.claimed

    @property
    def age(self) -> timedelta | None:
        if self.generated_at is None or self.read_at is None:
            return None
        return self.read_at - self.generated_at

    @property
    def is_stale(self) -> bool | None:
        """Whether the bundle is old enough to be a different document.

        ``None`` where the wrapper carries no timestamp: a bundle whose age
        cannot be established is not thereby fresh.
        """
        age = self.age
        if age is None:
            return None
        return age > STALE_AFTER

    def describe(self) -> str:
        parts = [f"relayed initial load from {self.path.name}"]
        if self.obtained_from:
            parts.append(f"via {self.obtained_from}")
        if self.claimed is None:
            parts.append(
                f"{self.parsed} NOTAM parsed; the wrapper states no count, so "
                "completeness is unverifiable"
            )
        elif self.is_complete:
            parts.append(f"{self.parsed} of {self.claimed} NOTAM")
        else:
            parts.append(
                f"SHORT READ — {self.parsed} parsed of {self.claimed} claimed"
            )
        if self.generated_at is not None:
            parts.append(f"generated {self.generated_at:%Y-%m-%d %H:%MZ}")
            age = self.age
            if age is not None:
                hours = age.total_seconds() / 3600.0
                parts.append(
                    f"{hours:.1f} h old" + ("  ·  STALE" if self.is_stale else "")
                )
        else:
            parts.append("no timestamp in the wrapper, so its age is unknown")
        return "  ·  ".join(parts)


def relay_initial_load(
    path: Path | str,
    *,
    archive: Archive,
    classification: str | None = None,
    obtained_from: str = "",
    read_at: datetime | None = None,
) -> RelayedLoad:
    """Read, archive and check a bundle fetched elsewhere.

    The bytes are archived exactly as they arrived, before anything is parsed,
    so what was read is what can be produced later. A short read raises rather
    than returning: presenting two NOTAM as a successful load of a country is
    the failure that looks exactly like a quiet day.
    """
    path = Path(path)
    try:
        payload = path.read_bytes()
    except OSError as error:
        raise RelayError(f"{path}: cannot be read — {error}") from None
    if not payload:
        raise RelayError(f"{path}: is empty")

    # Accept either shape the FAA serves: the gzip it signs, or the plain XML
    # a transfer may already have decompressed.
    if payload[:2] != GZIP_MAGIC:
        head = payload.lstrip()[:200].lower()
        if not (head.startswith(b"<?xml") or head.startswith(b"<soap") or b"featurecollection" in head):
            raise RelayError(
                f"{path}: is neither gzip nor AIXM. An initial load is a "
                "gzipped SOAP envelope carrying an AIXM FeatureCollection; a "
                "JSON handover response is the pointer to it, not the bundle."
            )

    moment = read_at or datetime.now(timezone.utc)
    digest = hashlib.sha256(payload).hexdigest()

    # Read before archiving, because the header decides how the bytes are
    # cited: an ArchiveEntry's retrieved_at becomes the citation's, and for a
    # relayed bundle that must be when the FAA generated it, not when this
    # machine happened to open the file. Dating a day-old baseline to its
    # arrival would make stale NOTAM look fresh. The payload is held in memory
    # throughout and archived unconditionally below, including when the read
    # turns out short — a bad copy still has to be citable.
    staged = InitialLoad(entry=None, payload=payload, classification=classification)
    try:
        with staged.open() as stream:
            feed = NotamFeed(stream)
            parsed = sum(1 for _ in feed)
    except (OSError, EOFError, gzip.BadGzipFile, ET.ParseError) as error:
        raise RelayError(
            f"{path}: could not be read — {type(error).__name__}: {error}. A "
            "file truncated in transfer fails exactly here."
        ) from None

    header = feed.header
    generated_at = header.timestamp if header is not None else None

    entry = archive.put(
        payload,
        source_id="FAA-RELAY",
        # Marked so a citation cannot be mistaken for one we fetched: the
        # difference between a moment we witnessed and one we were told about
        # is the whole basis of a citation.
        url=f"file:{path.name}"
        + (f" (relayed by {obtained_from.strip()})" if obtained_from.strip() else " (relayed)"),
        retrieved_at=generated_at or moment,
        content_type=(
            "application/gzip" if payload[:2] == GZIP_MAGIC else "application/xml"
        ),
    )

    relayed = RelayedLoad(
        load=InitialLoad(entry=entry, payload=payload, classification=classification),
        path=path,
        obtained_from=obtained_from.strip(),
        read_at=moment,
        generated_at=generated_at,
        claimed=header.number_returned if header is not None else None,
        parsed=parsed,
    )

    if feed.is_complete is False:
        raise RelayError(
            f"{path}: short read — {parsed} NOTAM parsed of "
            f"{relayed.claimed} the FAA states. A file truncated in transfer "
            "looks exactly like a quiet day, so it is refused rather than "
            "loaded. Re-fetch and re-transfer.\n"
            f"  Archived as {digest[:12]} either way, so the bad copy is "
            "still citable."
        )
    return relayed

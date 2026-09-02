"""Reading FAA NOTAM out of AIXM 5.1.

The NMS-API serves NOTAM as AIXM ``AIXMBasicMessage`` documents rather than as
text, and that is the reason to prefer it. A text NOTAM says *"RWY 20 RWY END
ID LGT U/S"* and leaves a reader to work out which aerodrome, which runway and
which physical end. The AIXM message carries the ``AirportHeliport``, the
``Runway`` and the ``RunwayDirection`` as linked features alongside the text —
so the affected object is known structurally rather than inferred from prose.
That is what makes a NOTAM joinable to an aerodrome dossier.

Two design decisions worth stating.

**Elements are matched on local name.** Namespace prefixes in the FAA's feed
are inconsistent with the AIXM schema in places — ``member`` is bound to the
AIXM namespace where the message schema defines it — and a reader keyed on
exact qualified names breaks the first time either is corrected. Local names in
this vocabulary are distinctive enough to match on safely.

**Parsing is streamed.** A domestic initial load is over twenty thousand
messages. Each is read, emitted and released, so the reader's memory does not
grow with the size of the feed.

Nothing here interprets a NOTAM. It reports what the message contains, with
unparsed fields left as ``None`` rather than filled with a plausible reading.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import IO, Any, Iterator
from xml.etree import ElementTree as ET

from aeropub.archive import ArchiveEntry
from aeropub.notam import Notam, NotamKind
from aeropub.notam import parse as parse_icao_notam
from aeropub.provenance import Confidence, SourceRef

__all__ = [
    "PARSER_ID",
    "PARSER_VERSION",
    "AffectedFeature",
    "FeedHeader",
    "NmsNotam",
    "NotamFeed",
    "iter_notams",
    "read_notams",
]

PARSER_ID = "faa-nms-aixm"
PARSER_VERSION = "0.1.0"

#: Local names of features the FAA links to an event. Anything else still
#: becomes an :class:`AffectedFeature`, tagged with whatever the element is
#: called — an unrecognised feature type is information, not an error.
_KNOWN_FEATURES = frozenset(
    {
        "AirportHeliport",
        "Runway",
        "RunwayDirection",
        "RunwayElement",
        "Navaid",
        "Airspace",
        "DesignatedPoint",
        "Route",
        "RouteSegment",
        "TaxiwayElement",
        "Taxiway",
        "ApronElement",
        "Apron",
        "VerticalStructure",
        "Procedure",
    }
)

#: FAA domestic header, e.g. ``!STL 08/430 8WC RWY 20 ...``. The accountability,
#: the number as the FAA prints it, and the affected location, in that order.
_DOMESTIC_HEADER = re.compile(
    r"^!(?P<accountability>[A-Z0-9]{3,4})\s+"
    r"(?P<number>\d{1,2}/\d{1,4})\s+"
    r"(?P<location>[A-Z0-9]{3,4})\b"
)

#: An ICAO-format NOTAM opens with its own identifier. Used only to decide
#: whether handing the text to the ICAO parser is meaningful.
_ICAO_HEADER = re.compile(r"\b[A-Z]\d{4}/\d{2}\s+NOTAM[NRC]\b")


def _local(tag: Any) -> str:
    """The element's local name, namespace discarded."""
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1]


def _parse_iso(value: str | None) -> datetime | None:
    """An ISO 8601 instant as the feed writes it, or ``None``.

    Tolerant of the two things that vary: a trailing ``Z`` where Python before
    3.11 wants an offset, and fractional seconds of any length.
    """
    if not value:
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    if "." in text:
        head, _, tail = text.partition(".")
        digits = ""
        rest = ""
        for index, char in enumerate(tail):
            if char.isdigit():
                digits += char
            else:
                rest = tail[index:]
                break
        if digits:
            text = f"{head}.{digits[:6].ljust(6, '0')}{rest}"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _parse_effective(value: str | None) -> tuple[datetime | None, bool, bool]:
    """An ``effectiveStart``/``effectiveEnd`` value.

    Returns the moment, whether it is permanent, and whether it is estimated.
    The FAA writes these as ``YYYYMMDDHHMM``, with ``PERM`` for an indefinite
    end and an ``EST`` suffix where the end is a projection. An estimated end
    matters operationally: it is the NOTAM most likely to be extended.
    """
    if not value:
        return None, False, False
    text = value.strip().upper()
    if text.startswith("PERM"):
        return None, True, False
    estimated = text.endswith("EST")
    if estimated:
        text = text[:-3].strip()
    # Two widths are both real and both appear in FAA data. The AIXM element
    # carries a four-digit year (202508210234); the printed NOTAM text beside
    # it carries the ICAO two-digit form (2508210234). Reading a 12-digit value
    # with the 10-digit rule yields month 25 and a silent None, so the width
    # decides the century rather than a guess.
    if re.fullmatch(r"\d{12}", text):
        year, rest = int(text[0:4]), text[4:]
    elif re.fullmatch(r"\d{10}", text):
        year, rest = 2000 + int(text[0:2]), text[2:]
    else:
        # Some entries carry a full ISO instant instead. Try that before
        # giving up, but never invent a reading from a shape we do not know.
        return _parse_iso(value), False, estimated
    try:
        return (
            datetime(
                year,
                int(rest[0:2]),
                int(rest[2:4]),
                int(rest[4:6]),
                int(rest[6:8]),
                tzinfo=timezone.utc,
            ),
            False,
            estimated,
        )
    except ValueError:
        return None, False, estimated


def _text(element: ET.Element | None) -> str | None:
    if element is None or element.text is None:
        return None
    value = element.text.strip()
    return value or None


def _find(parent: ET.Element, *names: str) -> ET.Element | None:
    """Descend by local name, one level per name."""
    current: ET.Element | None = parent
    for name in names:
        if current is None:
            return None
        current = next((c for c in current.iter() if _local(c.tag) == name), None)
    return current


def _child_text(parent: ET.Element, name: str) -> str | None:
    """Text of the named field: a direct child for preference, else a descendant.

    Direct children first because local names repeat at different depths and
    document order is not a contract. ``event:NOTAM`` has a ``type`` of ``N``;
    its nested ``NOTAMTranslation`` has a ``type`` of ``LOCAL_FORMAT``. A plain
    depth-first search returns the right one today only because the FAA happens
    to emit them in that order.
    """
    direct = next((c for c in parent if _local(c.tag) == name), None)
    if direct is not None:
        return _text(direct)
    return _text(next((c for c in parent.iter() if _local(c.tag) == name), None))


def _as_int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value.strip())
    except ValueError:
        return None


@dataclass(frozen=True, slots=True)
class AffectedFeature:
    """One aeronautical object the event is attached to."""

    kind: str
    """AIXM feature type, e.g. ``"Runway"``, ``"AirportHeliport"``."""

    designator: str | None = None
    name: str | None = None
    uuid: str | None = None
    """The feature's ``gml:identifier`` — stable across cycles, which is what
    makes it worth keeping. It is how the same runway is recognised in a later
    message without matching on a designator that may have been renumbered."""

    latitude: float | None = None
    longitude: float | None = None

    def describe(self) -> str:
        label = self.designator or self.name or self.uuid or "unidentified"
        return f"{self.kind} {label}"


@dataclass(frozen=True, slots=True)
class FeedHeader:
    """What the WFS wrapper says about the response as a whole."""

    number_returned: int | None = None
    """The FAA's own count. Compared against what we actually read, so a
    truncated download is caught rather than quietly under-reporting."""

    timestamp: datetime | None = None


@dataclass(frozen=True, slots=True)
class NmsNotam:
    """One NOTAM as the NMS-API delivers it."""

    nms_id: str | None
    """The NMS message id, from ``gml:id`` on the enclosing message."""

    uuid: str | None
    number: int | None
    year: int | None
    kind: NotamKind | None
    """``N``, ``R`` or ``C``. ``None`` where the feed gave something else."""

    issued: datetime | None = None
    location: str | None = None
    """The FAA location the NOTAM is filed against — often a three-character
    domestic identifier, not an ICAO indicator. See :attr:`icao_location`."""

    effective_start: datetime | None = None
    effective_end: datetime | None = None
    permanent: bool = False
    estimated: bool = False
    schedule: str | None = None
    text: str = ""
    simple_text: str | None = None
    """The NOTAM as the FAA prints it, from ``NOTAMTranslation``."""

    translation_type: str | None = None
    classification: str | None = None
    """``DOM``, ``INTL``, ``MIL``, ``LMIL``, ``FDC`` — the short form used in
    the payload, not the long form used in request paths."""

    account_id: str | None = None
    airport_name: str | None = None
    icao_location: str | None = None
    """Present only where the FAA supplies one; many domestic NOTAM have no
    ICAO indicator at all, and inventing one from the location would be wrong
    for every three-character identifier."""

    last_updated: datetime | None = None
    sequence_number: int | None = None
    correction_number: int | None = None
    scenario: str | None = None
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    """The event's own validity, from ``gml:TimePeriod``. Usually agrees with
    the effective window; kept separately because where it does not, the
    disagreement is the finding."""

    interpretation: str | None = None
    features: tuple[AffectedFeature, ...] = ()

    # -- identity --------------------------------------------------------

    @property
    def domestic_number(self) -> str | None:
        """The number as the FAA prints it, e.g. ``08/430``.

        Read from the printed text, never reconstructed. The leading pair is
        the month of issue, so deriving it from ``issued`` would be right most
        of the time and wrong across a month boundary — which is precisely when
        someone is searching for the NOTAM by number.
        """
        if not self.simple_text:
            return None
        match = _DOMESTIC_HEADER.match(self.simple_text.strip())
        return match.group("number") if match else None

    @property
    def accountability(self) -> str | None:
        """The issuing facility, e.g. ``STL``. From the printed text or fnse."""
        if self.simple_text:
            match = _DOMESTIC_HEADER.match(self.simple_text.strip())
            if match:
                return match.group("accountability")
        return self.account_id

    @property
    def identifier(self) -> str:
        """A stable label for logs and citations."""
        printed = self.domestic_number
        if printed:
            return f"{self.accountability or '?'} {printed}"
        if self.number is not None and self.year is not None:
            return f"{self.number}/{self.year}"
        return self.nms_id or self.uuid or "unidentified NOTAM"

    @property
    def is_international(self) -> bool:
        return (self.classification or "").upper().startswith("INT")

    # -- interpretation --------------------------------------------------

    def is_in_force(self, moment: datetime) -> bool:
        if self.effective_start is None or moment < self.effective_start:
            return False
        if self.permanent or self.effective_end is None:
            return True
        return moment <= self.effective_end

    def aerodromes(self) -> tuple[AffectedFeature, ...]:
        return tuple(f for f in self.features if f.kind == "AirportHeliport")

    def runways(self) -> tuple[AffectedFeature, ...]:
        return tuple(f for f in self.features if f.kind in ("Runway", "RunwayDirection"))

    def to_icao_notam(self) -> Notam | None:
        """The ICAO-format reading, where the text is in ICAO format.

        Most FAA domestic NOTAM are not: they use the FAA's own format, which
        has no Q-line and no lettered items, and the ICAO parser would rightly
        refuse it. International-series messages generally are. Returns
        ``None`` rather than a partial parse, so a caller can tell the
        difference between "not applicable" and "failed".
        """
        for candidate in (self.simple_text, self.text):
            if candidate and _ICAO_HEADER.search(candidate):
                try:
                    return parse_icao_notam(candidate)
                except ValueError:
                    return None
        return None

    # -- provenance ------------------------------------------------------

    def source_ref(
        self,
        entry: ArchiveEntry,
        *,
        confidence: Confidence = Confidence.HIGH,
    ) -> SourceRef:
        """The citation for anything read out of this message."""
        locator = self.nms_id or self.uuid or self.identifier
        return entry.to_source_ref(
            document=f"FAA NMS NOTAM {self.identifier}",
            locator=f"AIXMBasicMessage {locator}",
            parser_id=PARSER_ID,
            parser_version=PARSER_VERSION,
            confidence=confidence,
        )


# --------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------


class NotamFeed:
    """Streams NOTAM out of an AIXM document.

    Iterate it. :attr:`header` is populated as soon as the wrapper is seen, and
    the counters are meaningful once iteration finishes — they are how a short
    read is noticed, which otherwise looks exactly like a quiet day.
    """

    def __init__(self, source: IO[bytes] | str) -> None:
        self._source = source
        self.header: FeedHeader | None = None
        self.messages_seen = 0
        self.notams_read = 0
        self.messages_without_notam = 0

    def __iter__(self) -> Iterator[NmsNotam]:
        container: ET.Element | None = None
        for event, element in ET.iterparse(self._source, events=("start", "end")):
            name = _local(element.tag)

            if event == "start":
                if name == "FeatureCollection" and self.header is None:
                    self.header = FeedHeader(
                        number_returned=_as_int(element.get("numberReturned")),
                        timestamp=_parse_iso(element.get("timeStamp")),
                    )
                    container = element
                continue

            if name != "AIXMBasicMessage":
                continue

            self.messages_seen += 1
            notam = _read_message(element)
            if notam is None:
                # A message carrying only feature updates and no event. Counted
                # rather than discarded silently: if these ever outnumber the
                # NOTAM, something about the feed has changed.
                self.messages_without_notam += 1
            else:
                self.notams_read += 1
                yield notam

            element.clear()
            if container is not None:
                # Release completed members. The parser keeps its own stack, so
                # emptying the container mid-document is safe and is what keeps
                # memory flat across a twenty-thousand-message load.
                container.clear()

    @property
    def is_complete(self) -> bool | None:
        """Whether we read as many messages as the feed claimed to send.

        ``None`` when the feed did not say. A ``False`` here means the download
        was truncated, and everything derived from it under-reports.
        """
        if self.header is None or self.header.number_returned is None:
            return None
        return self.messages_seen >= self.header.number_returned


def _read_message(message: ET.Element) -> NmsNotam | None:
    """One ``AIXMBasicMessage``, or ``None`` if it carries no NOTAM."""
    nms_id = next(
        (v for k, v in message.attrib.items() if _local(k) == "id"), None
    )

    event = next((c for c in message.iter() if _local(c.tag) == "Event"), None)
    if event is None:
        return None
    notam_el = next((c for c in event.iter() if _local(c.tag) == "NOTAM"), None)
    if notam_el is None:
        return None

    slice_el = next(
        (c for c in event.iter() if _local(c.tag) == "EventTimeSlice"), event
    )

    valid_from = _parse_iso(_child_text(slice_el, "beginPosition"))
    valid_to = _parse_iso(_child_text(slice_el, "endPosition"))

    start, start_perm, start_est = _parse_effective(_child_text(notam_el, "effectiveStart"))
    end, end_perm, end_est = _parse_effective(_child_text(notam_el, "effectiveEnd"))

    kind = None
    raw_type = (_child_text(notam_el, "type") or "").strip().upper()
    if raw_type in ("N", "R", "C"):
        kind = {"N": NotamKind.NEW, "R": NotamKind.REPLACE, "C": NotamKind.CANCEL}[raw_type]

    translation = next(
        (c for c in notam_el.iter() if _local(c.tag) == "NOTAMTranslation"), None
    )

    extension = next(
        (c for c in event.iter() if _local(c.tag) == "EventExtension"), None
    )

    return NmsNotam(
        nms_id=nms_id,
        uuid=_child_text(event, "identifier"),
        number=_as_int(_child_text(notam_el, "number")),
        year=_as_int(_child_text(notam_el, "year")),
        kind=kind,
        issued=_parse_iso(_child_text(notam_el, "issued")),
        location=_child_text(notam_el, "location"),
        effective_start=start,
        effective_end=end,
        permanent=start_perm or end_perm,
        estimated=start_est or end_est,
        schedule=_child_text(notam_el, "schedule"),
        text=_child_text(notam_el, "text") or "",
        simple_text=_child_text(translation, "simpleText") if translation is not None else None,
        translation_type=_child_text(translation, "type") if translation is not None else None,
        classification=_child_text(extension, "classification") if extension is not None else None,
        account_id=_child_text(extension, "accountId") if extension is not None else None,
        airport_name=_child_text(extension, "airportname") if extension is not None else None,
        icao_location=_child_text(extension, "icaoLocation") if extension is not None else None,
        last_updated=(
            _parse_iso(_child_text(extension, "lastUpdated"))
            if extension is not None
            else None
        ),
        sequence_number=_as_int(_child_text(slice_el, "sequenceNumber")),
        correction_number=_as_int(_child_text(slice_el, "correctionNumber")),
        scenario=_child_text(slice_el, "scenario"),
        valid_from=valid_from,
        valid_to=valid_to,
        interpretation=_child_text(slice_el, "interpretation"),
        features=_read_features(message),
    )


def _read_features(message: ET.Element) -> tuple[AffectedFeature, ...]:
    """Every aeronautical object linked to the event, in document order."""
    found: list[AffectedFeature] = []
    for child in message.iter():
        kind = _local(child.tag)
        if kind == "Event" or not kind:
            continue
        if kind not in _KNOWN_FEATURES and not kind.endswith("TimeSlice"):
            continue
        if kind.endswith("TimeSlice"):
            continue
        latitude = longitude = None
        position = _find(child, "pos")
        if position is not None and position.text:
            parts = position.text.split()
            if len(parts) >= 2:
                try:
                    latitude, longitude = float(parts[0]), float(parts[1])
                except ValueError:
                    latitude = longitude = None
        found.append(
            AffectedFeature(
                kind=kind,
                designator=_child_text(child, "designator"),
                name=_child_text(child, "name"),
                uuid=_child_text(child, "identifier"),
                latitude=latitude,
                longitude=longitude,
            )
        )
    return tuple(found)


def iter_notams(source: IO[bytes] | str) -> Iterator[NmsNotam]:
    """Stream NOTAM from an AIXM document, file object or path."""
    return iter(NotamFeed(source))


def read_notams(source: IO[bytes] | str) -> tuple[NmsNotam, ...]:
    """Read every NOTAM at once. Use :class:`NotamFeed` for a large load."""
    return tuple(NotamFeed(source))

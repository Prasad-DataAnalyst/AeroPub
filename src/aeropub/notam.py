"""NOTAM parsing.

Unlike an eAIP, whose layout every State invents for itself, a NOTAM has a
format defined by ICAO Annex 15 and PANS-AIM. That makes it the one source
which can be parsed from the specification rather than from a captured sample —
the grammar is the same whether it comes from Doha, Riyadh or Anchorage.

Parsing runs in two stages, and the split matters:

**Structure is certain.** The Q-line grammar and Items A through G are fixed. A
NOTAM either matches them or it is malformed, and either answer is reliable.

**Meaning is partial, and says so.** The Q-code's subject and condition letters
map to a large table in ICAO Doc 8126. The table below holds only entries
carrying no doubt; everything else parses structurally with its decoded meaning
left as ``None``. A wrong plain-language reading of a runway-closure code is far
more dangerous than an admitted gap, so the gap is admitted.

.. warning::
   Built against the published format, not against captured traffic. It must be
   re-validated on real NOTAM before anything operational depends on it — the
   specification says what a NOTAM should look like, and States are inventive.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum

__all__ = [
    "Notam",
    "NotamKind",
    "QLine",
    "SUBJECTS",
    "CONDITIONS",
    "decode_qcode",
    "parse",
    "parse_validity",
]


class NotamKind(str, Enum):
    """What the message does to the series."""

    NEW = "new"
    REPLACE = "replace"
    CANCEL = "cancel"


#: Q-code subjects, second and third characters. Deliberately partial: only
#: entries carrying no doubt. Complete from ICAO Doc 8126 before relying on it.
SUBJECTS: dict[str, str] = {
    "FA": "aerodrome",
    "FF": "fire fighting and rescue",
    "FU": "fuel availability",
    "IC": "instrument landing system",
    "ID": "DME associated with ILS",
    "IG": "glide path",
    "IL": "localizer",
    "LA": "approach lighting system",
    "LC": "runway centre line lights",
    "LE": "runway edge lights",
    "LP": "precision approach path indicator",
    "LT": "threshold lights",
    "MA": "movement area",
    "MR": "runway",
    "MS": "stopway",
    "MT": "threshold",
    "MX": "taxiway",
    "NB": "non-directional radio beacon",
    "NV": "VOR",
    "PA": "standard instrument arrival",
    "PD": "standard instrument departure",
    "PI": "instrument approach procedure",
    "RD": "danger area",
    "RP": "prohibited area",
    "RR": "restricted area",
    "RT": "temporary restricted area",
}

#: Q-code conditions, fourth and fifth characters. Same discipline.
CONDITIONS: dict[str, str] = {
    "AS": "unserviceable",
    "AU": "not available",
    "AW": "completely withdrawn",
    "CC": "completed",
    "CH": "changed",
    "CN": "cancelled",
    "CS": "installed",
    "HW": "work in progress",
    "HV": "work completed",
    "HX": "concentration of birds",
    "LC": "closed",
    "LT": "limited to",
    "LV": "closed to VFR operations",
    "LI": "closed to IFR operations",
    "XX": "plain language",
}

_HEADER = re.compile(
    r"^\s*(?P<series>[A-Z])(?P<number>\d{4})/(?P<year>\d{2})\s+"
    r"NOTAM(?P<kind>[NRC])"
    r"(?:\s+(?P<ref>[A-Z]\d{4}/\d{2}))?",
    re.MULTILINE,
)

_QLINE = re.compile(
    r"\bQ\)\s*"
    r"(?P<fir>[A-Z]{4})/"
    r"(?P<code>Q[A-Z]{4})/"
    r"(?P<traffic>[IVK]{1,2})/"
    r"(?P<purpose>[NBOM]{1,3})/"
    r"(?P<scope>[AEWK]{1,2})/"
    r"(?P<lower>\d{3})/"
    r"(?P<upper>\d{3})/"
    r"(?P<lat>\d{4}[NS])(?P<lon>\d{5}[EW])(?P<radius>\d{3})"
)

#: Items are delimited by the next item marker, so each runs to the following
#: letter-paren or the end of the message.
_ITEM = r"\b{}\)\s*(?P<value>.*?)(?=\s*\b[A-G]\)|\Z)"


@dataclass(frozen=True, slots=True)
class QLine:
    """The machine-readable summary line."""

    fir: str
    code: str
    traffic: str
    purpose: str
    scope: str
    lower_fl: int
    upper_fl: int
    latitude: str
    longitude: str
    radius_nm: int

    @property
    def subject_code(self) -> str:
        """Characters two and three — what the NOTAM is about."""
        return self.code[1:3]

    @property
    def condition_code(self) -> str:
        """Characters four and five — what has happened to it."""
        return self.code[3:5]

    @property
    def subject(self) -> str | None:
        return SUBJECTS.get(self.subject_code)

    @property
    def condition(self) -> str | None:
        return CONDITIONS.get(self.condition_code)

    @property
    def decoded(self) -> str | None:
        """Plain language, or ``None`` where either half is unknown.

        Never a partial guess: "runway <unknown>" reads as though the condition
        were understood, which is exactly the misreading to avoid.
        """
        if self.subject is None or self.condition is None:
            return None
        return f"{self.subject} {self.condition}"

    @property
    def is_aerodrome_scope(self) -> bool:
        return "A" in self.scope

    @property
    def is_enroute_scope(self) -> bool:
        return "E" in self.scope


@dataclass(frozen=True, slots=True)
class Notam:
    """One parsed NOTAM."""

    series: str
    number: int
    year: int
    kind: NotamKind
    raw: str
    references: str | None = None
    q: QLine | None = None
    locations: tuple[str, ...] = ()
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    permanent: bool = False
    estimated: bool = False
    schedule: str | None = None
    text: str = ""
    lower_limit: str | None = None
    upper_limit: str | None = None

    @property
    def identifier(self) -> str:
        return f"{self.series}{self.number:04d}/{self.year:02d}"

    @property
    def supersedes(self) -> str | None:
        """What this message replaces or cancels, if anything."""
        return self.references if self.kind is not NotamKind.NEW else None

    def is_in_force(self, moment: datetime) -> bool:
        if self.valid_from is None or moment < self.valid_from:
            return False
        if self.permanent or self.valid_to is None:
            return True
        return moment <= self.valid_to


def decode_qcode(code: str) -> tuple[str | None, str | None]:
    """Subject and condition in plain language, either possibly ``None``."""
    if len(code) != 5 or not code.startswith("Q"):
        raise ValueError(f"a Q-code is Q plus four letters, got {code!r}")
    return SUBJECTS.get(code[1:3]), CONDITIONS.get(code[3:5])


def parse_validity(value: str) -> tuple[datetime | None, bool, bool]:
    """Parse an Item B or C value.

    Returns the moment, whether it is permanent, and whether it is estimated.
    Item C carries ``PERM`` for permanent and an ``EST`` suffix where the end is
    an estimate — a distinction worth keeping, since an estimated end date is a
    NOTAM likely to be extended.
    """
    text = value.strip().upper()
    if text.startswith("PERM"):
        return None, True, False

    estimated = text.endswith("EST")
    if estimated:
        text = text[:-3].strip()

    if not re.fullmatch(r"\d{10}", text):
        return None, False, estimated

    year = 2000 + int(text[0:2])
    moment = datetime(
        year, int(text[2:4]), int(text[4:6]), int(text[6:8]), int(text[8:10]),
        tzinfo=timezone.utc,
    )
    return moment, False, estimated


def _item(body: str, letter: str) -> str | None:
    match = re.search(_ITEM.format(letter), body, re.DOTALL)
    if match is None:
        return None
    value = match.group("value").strip()
    return value or None


def parse(message: str) -> Notam:
    """Parse a NOTAM message.

    Raises :class:`ValueError` when the header is missing or malformed. A
    message that cannot be identified is not partially accepted — an
    unidentifiable NOTAM has nothing to attach a finding to.
    """
    header = _HEADER.search(message)
    if header is None:
        raise ValueError("no NOTAM header found (expected e.g. 'A1234/26 NOTAMN')")

    kind = {
        "N": NotamKind.NEW,
        "R": NotamKind.REPLACE,
        "C": NotamKind.CANCEL,
    }[header.group("kind")]

    body = message[header.end():]

    q = None
    qmatch = _QLINE.search(body)
    if qmatch is not None:
        q = QLine(
            fir=qmatch.group("fir"),
            code=qmatch.group("code"),
            traffic=qmatch.group("traffic"),
            purpose=qmatch.group("purpose"),
            scope=qmatch.group("scope"),
            lower_fl=int(qmatch.group("lower")),
            upper_fl=int(qmatch.group("upper")),
            latitude=qmatch.group("lat"),
            longitude=qmatch.group("lon"),
            radius_nm=int(qmatch.group("radius")),
        )

    locations: tuple[str, ...] = ()
    raw_a = _item(body, "A")
    if raw_a:
        locations = tuple(re.findall(r"\b[A-Z]{4}\b", raw_a))

    valid_from = valid_to = None
    permanent = estimated = False
    raw_b = _item(body, "B")
    if raw_b:
        valid_from, _, _ = parse_validity(raw_b)
    raw_c = _item(body, "C")
    if raw_c:
        valid_to, permanent, estimated = parse_validity(raw_c)

    return Notam(
        series=header.group("series"),
        number=int(header.group("number")),
        year=int(header.group("year")),
        kind=kind,
        references=header.group("ref"),
        raw=message,
        q=q,
        locations=locations,
        valid_from=valid_from,
        valid_to=valid_to,
        permanent=permanent,
        estimated=estimated,
        schedule=_item(body, "D"),
        text=_item(body, "E") or "",
        lower_limit=_item(body, "F"),
        upper_limit=_item(body, "G"),
    )

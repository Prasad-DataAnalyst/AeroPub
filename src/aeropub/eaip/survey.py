"""Survey a directory of saved eAIP pages, rather than one page at a time.

A State's AIP is a hundred and more pages. Probing them one at a time answers
"what is in this page" a hundred times over and never answers the question
that comes first: *is what arrived the AIP the State publishes*.

So this reads the whole directory at once and sets the result against the
section list PANS-AIM Doc 10066 Appendix 2 defines, which :mod:`aeropub.aip`
already holds. Three absences are reported and none of them is allowed to
look like a pass:

* a section PANS-AIM names that did not arrive at all;
* a page that arrived but carries nothing the probe recognises — a frameset,
  a PDF wrapper, a navigation stub;
* a page whose *filename* claims a section its *content* does not carry.

The third is the quiet one. A file called ``ENR-3.2-en-GB.html`` that holds no
ENR 3.2 identifier still lands on disk, still has a plausible size, and still
counts towards "97 pages fetched". Only opening it says otherwise.

This is the structural half of the reconciliation. The sharper half needs the
State's own GEN 0.4 checklist — what Qatar says it publishes, rather than what
ICAO says an AIP contains — and lives in :mod:`aeropub.checklist`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from ..aip import Part, Repeat, Section, SECTIONS
from .probe import StructureReport, probe

#: How a saved eAIP file names its section. ``QA-ENR-3.2-en-GB.html`` and
#: ``ENR-3.2.html`` both read as ENR 3.2; ``QA-AD-2-OTHH-en-GB.html`` reads as
#: AD 2 for aerodrome OTHH, because a State publishes one page per aerodrome
#: covering every AD 2 subsection rather than a page per subsection.
_FILE_CODE = re.compile(
    r"(?:^|[-_])(?P<part>GEN|ENR|AD)[-_ ]?(?P<chapter>\d+)"
    r"(?:[.\-_](?P<ordinal>\d+))?"
    r"(?:[-_](?P<aerodrome>[A-Z]{4}))?",
    re.I,
)

#: How many codes to name before summarising. A fetch that went wrong lists
#: most of the AIP as absent, and a wall of 121 codes buries the shape of the
#: failure rather than showing it.
_LIST_LIMIT = 12

#: A section code as it appears in an element identifier, which is where the
#: probe finds them. Same shape the draft profile looks for, deliberately —
#: the survey must agree with what a profile would go on to read.
_ID_CODE = re.compile(r"(?i).*?(AD|ENR|GEN)[\W_]?(\d+)[\W_](\d+)")


class Legibility(str, Enum):
    """What became of one page when the probe opened it.

    Three states, not two. "Not recognised" and "not read" are different
    failures with different fixes — a frameset needs a different fetch, a
    decode error needs a different encoding — and collapsing them into one
    "bad" bucket loses the distinction that tells you which.
    """

    RECOGNISED = "RECOGNISED"
    """Carries element identifiers that name AIP sections."""

    NO_STRUCTURE = "NO_STRUCTURE"
    """Read cleanly, but nothing in it resembles an AIP section."""

    UNREAD = "UNREAD"
    """Could not be read at all. Never counted as either of the above."""


def read_code(filename: str) -> tuple[str, str]:
    """``("ENR 3.2", "")`` or ``("AD 2", "OTHH")`` from a saved page's name.

    Returns ``("", "")`` when the name says nothing about a section, which is
    the honest answer for ``index-en-GB.html``. The caller decides what to do
    about it; this does not guess.
    """
    stem = re.sub(r"\.html?$", "", filename, flags=re.I)
    stem = re.sub(r"-[a-z]{2}-[A-Z]{2}$", "", stem)
    match = _FILE_CODE.search(stem)
    if match is None:
        return ("", "")
    part = match.group("part").upper()
    chapter = int(match.group("chapter"))
    ordinal = match.group("ordinal")
    aerodrome = (match.group("aerodrome") or "").upper()
    code = f"{part} {chapter}" if ordinal is None else f"{part} {chapter}.{int(ordinal)}"
    return (code, aerodrome)


def codes_within(report: StructureReport) -> frozenset[str]:
    """The section codes a page's own element identifiers name."""
    found: set[str] = set()
    for identifier in report.identifiers:
        match = _ID_CODE.fullmatch(identifier)
        if match is None:
            continue
        found.add(f"{match.group(1).upper()} {int(match.group(2))}.{int(match.group(3))}")
    return frozenset(found)


@dataclass(frozen=True, slots=True)
class PageSurvey:
    """One saved page, as the probe found it."""

    path: Path
    named_code: str
    """What the filename claims the page is. May be empty."""

    aerodrome: str
    """Set where the filename names one, e.g. ``OTHH`` for an AD 2 page."""

    size: int
    legibility: Legibility
    report: StructureReport | None = None
    unread_because: str = ""
    codes_inside: frozenset[str] = frozenset()

    @property
    def contradicts_its_name(self) -> bool:
        """The filename claims a section the content does not carry.

        Only asserted where there is something to contradict: a page the probe
        could not read is ``UNREAD``, not a contradiction, and an aerodrome
        page is named for the chapter rather than any one subsection.
        """
        if self.legibility is Legibility.UNREAD or not self.named_code:
            return False
        if self.aerodrome:
            return not any(
                code.startswith(f"{self.named_code} ") or code.startswith(self.named_code)
                for code in self.codes_inside
            )
        if not self.codes_inside:
            return True
        return self.named_code not in self.codes_inside

    def describe(self) -> str:
        where = self.path.name
        if self.legibility is Legibility.UNREAD:
            return f"{where}: UNREAD — {self.unread_because}"
        held = ", ".join(sorted(self.codes_inside)[:6]) or "no section identifiers"
        mark = "  ← name and content disagree" if self.contradicts_its_name else ""
        return f"{where}: {held}{mark}"


@dataclass(frozen=True, slots=True)
class DirectorySurvey:
    """What a directory of saved eAIP pages holds, against what it should."""

    directory: Path
    pages: tuple[PageSurvey, ...] = ()

    @property
    def recognised(self) -> tuple[PageSurvey, ...]:
        return tuple(p for p in self.pages if p.legibility is Legibility.RECOGNISED)

    @property
    def without_structure(self) -> tuple[PageSurvey, ...]:
        return tuple(p for p in self.pages if p.legibility is Legibility.NO_STRUCTURE)

    @property
    def unread(self) -> tuple[PageSurvey, ...]:
        return tuple(p for p in self.pages if p.legibility is Legibility.UNREAD)

    @property
    def contradictions(self) -> tuple[PageSurvey, ...]:
        return tuple(p for p in self.pages if p.contradicts_its_name)

    @property
    def aerodromes(self) -> tuple[str, ...]:
        return tuple(sorted({p.aerodrome for p in self.pages if p.aerodrome}))

    @property
    def codes_read(self) -> frozenset[str]:
        """Sections a page actually carries, by its own element identifiers.

        These are the ones a profile can go on to read. Nothing here rests on
        a filename.
        """
        found: set[str] = set()
        for page in self.pages:
            found |= page.codes_inside
        return frozenset(found)

    @property
    def codes_named_only(self) -> frozenset[str]:
        """Sections a file claims but whose content carries no such thing.

        Neither held nor missing, and it must not be filed as either. A State
        that publishes ENR 5.1 as a PDF stub, or as a page with no element
        identifiers, has published it — so calling it missing is wrong. We
        cannot read it — so calling it held is worse, because everything
        downstream would then treat an empty page as covered.
        """
        named: set[str] = set()
        for page in self.pages:
            if (
                page.named_code
                and not page.codes_inside
                and page.legibility is not Legibility.UNREAD
                and not page.aerodrome
            ):
                named.add(page.named_code)
        return frozenset(named) - self.codes_read

    @property
    def codes_held(self) -> frozenset[str]:
        """Everything that arrived in any form, readable or not."""
        return self.codes_read | self.codes_named_only

    @property
    def missing(self) -> tuple[Section, ...]:
        """Sections PANS-AIM names that did not arrive at all.

        Per-aerodrome sections count as arrived when any aerodrome page
        carries them; whether *every* aerodrome has every subsection is a
        separate question this does not pretend to answer.
        """
        held = self.codes_held
        return tuple(s for s in SECTIONS if s.code not in held)

    @property
    def arrived_unreadable(self) -> tuple[Section, ...]:
        """Sections that arrived but that nothing downstream can read yet."""
        named = self.codes_named_only
        return tuple(s for s in SECTIONS if s.code in named)

    @property
    def unexpected(self) -> tuple[str, ...]:
        """Codes present here that PANS-AIM does not name.

        Not a fault. States add sections, and Doc 10066 permits it. Listed so
        a State's own additions are visible rather than silently dropped.
        """
        canonical = {s.code for s in SECTIONS}
        return tuple(sorted(c for c in self.codes_held if c not in canonical))

    @property
    def currency_spine(self) -> tuple[Section, ...]:
        """The chapter 0 sections that arrived *and* can be read.

        GEN 0.2, 0.3 and 0.4 are what the State says it has published. Without
        them there is nothing to reconcile against and coverage is a guess. A
        GEN 0.4 that arrived as an unreadable stub does not count here — it
        would be the one place where crediting an unreadable page turns the
        whole coverage claim into a fiction.
        """
        readable = self.codes_read
        return tuple(s for s in SECTIONS if s.is_currency and s.code in readable)

    def describe(self) -> str:
        lines = [
            f"EAIP SURVEY — {self.directory}",
            "",
            f"{len(self.pages)} pages  ·  {len(self.recognised)} with recognisable "
            f"structure  ·  {len(self.without_structure)} without  ·  "
            f"{len(self.unread)} unread",
            f"{len(self.codes_read)} sections readable  ·  "
            f"{len(self.codes_named_only)} arrived but unreadable  ·  "
            f"{len(self.missing)} of {len(SECTIONS)} absent",
        ]
        if self.aerodromes:
            lines.append(
                f"{len(self.aerodromes)} aerodromes: {', '.join(self.aerodromes)}"
            )

        lines += ["", "AGAINST PANS-AIM DOC 10066 APPENDIX 2"]
        missing = self.missing
        if not missing:
            lines.append("  Every section Doc 10066 names is present.")
        else:
            lines.append(
                f"  {len(missing)} of {len(SECTIONS)} sections did not arrive:"
            )
            for part in (Part.GEN, Part.ENR, Part.AD):
                absent = [s.code for s in missing if s.part is part]
                if not absent:
                    continue
                shown = ", ".join(absent[:_LIST_LIMIT])
                if len(absent) > _LIST_LIMIT:
                    shown += f", and {len(absent) - _LIST_LIMIT} more"
                lines.append(f"    {part.value} ({len(absent)}): {shown}")
            lines.append(
                "  A section may be absent because the State does not publish "
                "it, or because\n  the fetch missed it. GEN 0.4 tells them "
                "apart; this cannot."
            )

        unreadable = self.arrived_unreadable
        if unreadable:
            lines += ["", "ARRIVED BUT UNREADABLE"]
            lines.append(
                "  " + ", ".join(s.code for s in unreadable)
            )
            lines.append(
                "  A file arrived for each of these carrying nothing a profile "
                "can read — a\n  PDF stub, or a page with no element "
                "identifiers. Neither held nor missing:\n  the State published "
                "it and we cannot yet use it."
            )

        spine = self.currency_spine
        lines += ["", "CURRENCY SPINE"]
        if spine:
            lines.append("  held: " + ", ".join(s.code for s in spine))
        wanted = [s.code for s in SECTIONS if s.is_currency]
        absent_spine = [c for c in wanted if c not in self.codes_held]
        if absent_spine:
            lines.append(
                "  absent: " + ", ".join(absent_spine)
                + "\n  Without these there is nothing to reconcile coverage "
                "against."
            )

        if self.unexpected:
            lines += ["", "NOT NAMED BY DOC 10066 (a State may add sections)"]
            lines.append("  " + ", ".join(self.unexpected))

        if self.contradictions:
            lines += ["", "NAME AND CONTENT DISAGREE"]
            for page in self.contradictions:
                lines.append(f"  {page.describe()}")
            lines.append(
                "  Each of these landed on disk at a plausible size and counts "
                "towards the\n  fetch total. Only opening them says otherwise."
            )

        if self.unread:
            lines += ["", "UNREAD"]
            lines += [f"  {p.describe()}" for p in self.unread]

        return "\n".join(lines)


def survey_directory(directory: Path | str, *, pattern: str = "*.htm*") -> DirectorySurvey:
    """Probe every saved page in a directory."""
    root = Path(directory)
    if not root.is_dir():
        raise ValueError(f"{root} is not a directory")

    pages: list[PageSurvey] = []
    for path in sorted(root.glob(pattern)):
        if not path.is_file():
            continue
        code, aerodrome = read_code(path.name)
        size = path.stat().st_size
        try:
            html = path.read_text(encoding="utf-8", errors="replace")
        except OSError as error:
            pages.append(
                PageSurvey(
                    path=path, named_code=code, aerodrome=aerodrome, size=size,
                    legibility=Legibility.UNREAD, unread_because=str(error),
                )
            )
            continue

        report = probe(html, source=path.name)
        inside = codes_within(report)
        pages.append(
            PageSurvey(
                path=path,
                named_code=code,
                aerodrome=aerodrome,
                size=size,
                legibility=(
                    Legibility.RECOGNISED if inside else Legibility.NO_STRUCTURE
                ),
                report=report,
                codes_inside=inside,
            )
        )
    return DirectorySurvey(directory=root, pages=tuple(pages))

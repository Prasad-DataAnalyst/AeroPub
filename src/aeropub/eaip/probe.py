"""Read a real eAIP page and report what is actually in it.

This exists so that onboarding a State does not require sending anyone a copy
of the page. An AIS officer saves the AD 2 page from their browser, runs::

    python -m aeropub.eaip.probe OTHH-AD-2.html

and gets back a description of the document's structure plus a draft profile.
They check the draft against the page in front of them — which they can do and
a stranger cannot — mark it verified, and the parser reads that State from then
on.

It reports; it does not conclude
--------------------------------
The probe says *"25 elements carry an id matching AD-2.<number>"*. It does not
say those are the AD 2 sections. The distinction is the whole design: a tool
that decided would be wrong about some State, confidently, and nobody would
find out until a value came through misfiled. So the output is observations and
a draft, and a person confirms it.

Nothing here is aeronautical
----------------------------
No knowledge of what an AIP contains lives in this module — that is
:mod:`aeropub.aip`, which holds the 127 sections ICAO defines. This is a
document-structure reader that happens to be pointed at eAIPs, and keeping it
ignorant is what stops it "helpfully" recognising a section that is not there.

Standard library only, so it runs on a laptop with no virtualenv — the same
constraint as :mod:`aeropub.capture`, and for the same reason: the machine that
can reach the source is usually not the machine with the project installed.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Iterable

from aeropub.eaip.profile import EaipProfile, Locator, SectionRule

__all__ = [
    "Element",
    "Observation",
    "StructureReport",
    "draft_profile",
    "probe",
    "read_document",
]

_HEADINGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
_SKIP_TEXT = {"script", "style"}

#: Identifier patterns worth reporting, most specific first. These are
#: descriptions of what AIP identifiers tend to look like, not a claim that a
#: document containing them is an AIP — the probe reports a match, and a person
#: decides what it means.
_ID_SHAPES: tuple[tuple[str, str], ...] = (
    ("AD 2 section", r"(?i).*AD[\W_]?2[\W_]\d+.*"),
    ("AD 3 section", r"(?i).*AD[\W_]?3[\W_]\d+.*"),
    ("ENR section", r"(?i).*ENR[\W_]?\d+[\W_]\d+.*"),
    ("GEN section", r"(?i).*GEN[\W_]?\d+[\W_]\d+.*"),
    ("aerodrome designator", r"(?i).*\b[A-Z]{4}\b.*"),
)


@dataclass(frozen=True, slots=True)
class Element:
    """One element the document actually contains."""

    tag: str
    identifier: str = ""
    classes: tuple[str, ...] = ()
    text: str = ""
    rows: tuple[tuple[str, ...], ...] = ()
    """Table rows inside this element, each as its cells.

    Kept as cells rather than flattened into text, because a label and its
    value are in *adjacent cells* and flattening loses the boundary between
    one row and the next. A reader that scans forward from a label through
    flattened text will happily take the number out of the following row —
    which produced a runway width of 80 m read from the PCN."""

    depth: int = 0

    def describe(self) -> str:
        marks = []
        if self.identifier:
            marks.append(f"id={self.identifier}")
        if self.classes:
            marks.append(f"class={' '.join(self.classes)}")
        excerpt = " ".join(self.text.split())[:70]
        return f"<{self.tag}> {' '.join(marks)}{('  ' + excerpt) if excerpt else ''}"


@dataclass(frozen=True, slots=True)
class Observation:
    """One thing found, how often, and an example.

    ``confidence`` is deliberately absent. The probe has no basis for one, and
    a number here would be read as an assessment rather than a count.
    """

    what: str
    count: int
    examples: tuple[str, ...] = ()

    def describe(self) -> str:
        shown = ", ".join(self.examples[:4])
        more = f", ... ({self.count} in all)" if self.count > len(self.examples[:4]) else ""
        return f"{self.count:5}  {self.what}: {shown}{more}"


@dataclass
class _Open:
    """One element still being read."""

    tag: str
    identifier: str
    classes: tuple[str, ...]
    chunks: list[str]
    rows: list[tuple[str, ...]]


class _Reader(HTMLParser):
    """Collect every element with an identifier, class, or heading text."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.elements: list[Element] = []
        self.tag_counts: Counter[str] = Counter()
        self._stack: list[_Open] = []
        self._suppress = 0
        self._cell: list[str] | None = None
        self._row: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        self.tag_counts[tag] += 1
        if tag in _SKIP_TEXT:
            self._suppress += 1
        values = dict(attrs)
        identifier = (values.get("id") or "").strip()
        classes = tuple((values.get("class") or "").split())
        if tag == "tr":
            self._row = []
        elif tag in ("td", "th"):
            self._cell = []
        self._stack.append(_Open(tag, identifier, classes, [], []))

    def handle_endtag(self, tag):
        if tag in _SKIP_TEXT and self._suppress:
            self._suppress -= 1
        if tag in ("td", "th") and self._cell is not None:
            cell = " ".join("".join(self._cell).split())
            if self._row is not None:
                self._row.append(cell)
            self._cell = None
        elif tag == "tr" and self._row is not None:
            row = tuple(self._row)
            self._row = None
            if row:
                # A row belongs to every element it sits inside, so a section
                # div can be asked for its rows without knowing the table
                # nesting between them.
                for entry in self._stack:
                    entry.rows.append(row)

        # Unbalanced markup is normal in published pages. Unwind to the
        # matching tag rather than assuming the document is well formed.
        for index in range(len(self._stack) - 1, -1, -1):
            if self._stack[index].tag != tag:
                continue
            for opened in self._stack[index:]:
                text = "".join(opened.chunks).strip()
                if (
                    opened.identifier
                    or opened.classes
                    or opened.tag in _HEADINGS
                    or opened.tag == "table"
                ):
                    self.elements.append(
                        Element(
                            tag=opened.tag,
                            identifier=opened.identifier,
                            classes=opened.classes,
                            text=text,
                            rows=tuple(opened.rows),
                            depth=index,
                        )
                    )
            del self._stack[index:]
            return

    def handle_data(self, data):
        if self._suppress or not self._stack:
            return
        if self._cell is not None:
            self._cell.append(data)
        # Text belongs to every open element, so a heading nested in a div is
        # still findable by the div.
        for entry in self._stack:
            entry.chunks.append(data)


def read_document(html: str) -> list[Element]:
    """Every identified, classed, heading or table element, in document order."""
    reader = _Reader()
    reader.feed(html)
    reader.close()
    # Anything still open at the end of a malformed document is still evidence.
    for opened in reader._stack:
        if (
            opened.identifier
            or opened.classes
            or opened.tag in _HEADINGS
            or opened.tag == "table"
        ):
            reader.elements.append(
                Element(
                    tag=opened.tag, identifier=opened.identifier,
                    classes=opened.classes, text="".join(opened.chunks).strip(),
                    rows=tuple(opened.rows),
                )
            )
    return reader.elements


@dataclass(frozen=True, slots=True)
class StructureReport:
    """What one document contains, as counts and examples."""

    source: str
    elements: int
    tables: int
    identifiers: tuple[str, ...] = ()
    observations: tuple[Observation, ...] = ()
    headings: tuple[str, ...] = ()
    likely_toolchain: str = ""
    """A guess, named as one. ``eurocontrol`` where the document carries the
    identifier shapes that toolchain emits — but a State may have customised
    it, so nothing branches on this and the draft profile does not depend on
    it."""

    def describe(self) -> str:
        lines = [
            f"EAIP STRUCTURE — {self.source}",
            "",
            f"{self.elements} identified elements  ·  {self.tables} tables  ·  "
            f"{len(self.identifiers)} distinct ids",
        ]
        if self.likely_toolchain:
            lines.append(
                f"Identifier shapes resemble the {self.likely_toolchain} "
                "toolchain. That is a resemblance, not a determination."
            )
        lines += ["", "WHAT IS IN THE DOCUMENT"]
        if not self.observations:
            lines.append(
                "  Nothing recognisable. No element carries an id or class "
                "resembling an AIP\n  section reference. This document may be a "
                "PDF wrapper, a frameset, or a\n  navigation page rather than "
                "the content itself."
            )
        lines += [f"  {o.describe()}" for o in self.observations]

        if self.headings:
            lines += ["", "HEADINGS (first 20)"]
            lines += [f"  {h}" for h in self.headings[:20]]

        lines += [
            "",
            "WHAT TO DO WITH THIS",
            "  The draft profile below is a hypothesis written from the shapes "
            "above.",
            "  Check each section rule against the page you are looking at, "
            "correct it,",
            "  then set verified_at and verified_by. Until you do, every value "
            "read with",
            "  it is marked unverified and the coverage board says so.",
        ]
        return "\n".join(lines)


def _identifier_observations(elements: Iterable[Element]) -> list[Observation]:
    found: list[Observation] = []
    identifiers = [e.identifier for e in elements if e.identifier]
    for label, pattern in _ID_SHAPES:
        matching = [i for i in identifiers if re.fullmatch(pattern, i)]
        if matching:
            found.append(
                Observation(what=label, count=len(matching),
                            examples=tuple(sorted(set(matching))[:4]))
            )
    classes = Counter(c for e in elements for c in e.classes)
    if classes:
        found.append(
            Observation(
                what="most common classes",
                count=sum(classes.values()),
                examples=tuple(name for name, _ in classes.most_common(4)),
            )
        )
    return found


def probe(html: str, *, source: str = "document") -> StructureReport:
    """Describe what a document contains, without deciding what it means."""
    elements = read_document(html)
    identifiers = tuple(sorted({e.identifier for e in elements if e.identifier}))
    observations = _identifier_observations(elements)
    headings = tuple(
        " ".join(e.text.split())[:80]
        for e in elements
        if e.tag in _HEADINGS and e.text.strip()
    )
    toolchain = (
        "EUROCONTROL eAIP"
        if any(o.what.endswith("section") and o.count > 3 for o in observations)
        else ""
    )
    return StructureReport(
        source=source,
        elements=len(elements),
        tables=sum(1 for e in elements if e.tag == "table"),
        identifiers=identifiers,
        observations=tuple(observations),
        headings=headings,
        likely_toolchain=toolchain,
    )


def draft_profile(
    html: str, *, state: str, source_url: str = "", name: str = ""
) -> EaipProfile:
    """Propose a profile from what a document actually contains.

    Every section rule is written from an identifier that is really in the
    document, so a draft never references something that is not there. It is
    still a draft: the probe knows the element exists, not that it holds what
    its identifier suggests. ``verified_at`` is left unset, deliberately, and
    nothing sets it but a person.
    """
    elements = read_document(html)
    section_pattern = re.compile(r"(?i).*?(AD|ENR|GEN)[\W_]?(\d+)[\W_](\d+)")

    rules: list[SectionRule] = []
    seen: set[str] = set()
    for element in elements:
        if not element.identifier:
            continue
        match = section_pattern.fullmatch(element.identifier)
        if match is None:
            continue
        part, chapter, number = match.group(1).upper(), match.group(2), match.group(3)
        code = f"{part} {chapter}.{number}"
        if code in seen:
            continue
        seen.add(code)
        rules.append(
            SectionRule(
                code=code,
                locate=Locator(attribute="id", pattern=re.escape(element.identifier)),
                fields=(),
            )
        )

    report = probe(html)
    return EaipProfile(
        state=state,
        name=name,
        toolchain=report.likely_toolchain,
        sections=tuple(sorted(rules, key=lambda r: r.code)),
        source_url=source_url,
        note=(
            "Drafted by aeropub.eaip.probe from an observed document. Section "
            "rules point at identifiers that are really present; the fields "
            "under each are for a person to add from the page. Not verified."
        ),
    )

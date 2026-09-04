"""Read an eAIP page into cited facts, using a profile, or refuse.

There is one rule and everything else follows from it: **a value this module
emits was found where the profile said it would be, or it was not emitted.**
No fallback selector, no fuzzy match, no "best effort" pass that scrapes
something plausible. A section the profile could not locate is reported as a
miss, and a miss becomes a coverage gap — which is a true statement about what
we hold, and safe. A guessed value is not.

The failure report is the product
---------------------------------
A parse that finds nothing is not an error to be swallowed; it is the most
useful thing this module produces on the day a State re-lays-out their eAIP.
:class:`ParseResult` carries what was looked for, what was found, and what was
not, so the answer to "why is AD 2.12 empty this cycle" is in the output rather
than in somebody's debugger.

Provenance
----------
Every fact carries a ``SourceRef`` naming the document, the section it was read
from, and the SHA-256 of the page as parsed. ``parser_id`` records the profile
so a bad rule traces to every value it produced — the same discipline as a code
parser, applied to a rule that happens to live in JSON.

An unverified profile is not refused, because refusing would make verification
impossible: somebody has to parse with the draft to check it. Instead every
value it produces is marked ``Confidence.LOW``, which the review gate already
reads.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any

from aeropub.eaip.probe import Element, read_document
from aeropub.eaip.profile import EaipProfile, FieldRule, SectionRule
from aeropub.entities import normalise, under
from aeropub.facts import Fact, Precedence
from aeropub.provenance import Confidence, SourceRef

__all__ = ["ParseResult", "SectionOutcome", "parse_page"]

PARSER_VERSION = "0.1.0"

_NUMBER = re.compile(r"-?\d+(?:[.,]\d+)?")


@dataclass(frozen=True, slots=True)
class SectionOutcome:
    """What happened for one section rule. Recorded whether or not it worked."""

    code: str
    located: bool
    fields_expected: int = 0
    fields_found: int = 0
    detail: str = ""

    @property
    def is_complete(self) -> bool:
        return self.located and self.fields_found == self.fields_expected

    def describe(self) -> str:
        if not self.located:
            return f"  MISS  {self.code:9} {self.detail}"
        if self.fields_expected == 0:
            return f"  ----  {self.code:9} located, no fields defined in the profile"
        mark = "  ok  " if self.is_complete else " PART "
        return (
            f"{mark}{self.code:9} {self.fields_found} of {self.fields_expected} "
            f"fields{('  ' + self.detail) if self.detail else ''}"
        )


@dataclass(frozen=True, slots=True)
class ParseResult:
    """Everything read, and everything that was looked for and not found."""

    aerodrome: str
    profile_state: str
    verified_profile: bool
    facts: tuple[Fact, ...] = ()
    outcomes: tuple[SectionOutcome, ...] = ()
    content_hash: str = ""

    @property
    def missed(self) -> tuple[SectionOutcome, ...]:
        """Sections the profile could not locate. Coverage gaps, not errors."""
        return tuple(o for o in self.outcomes if not o.located)

    @property
    def partial(self) -> tuple[SectionOutcome, ...]:
        return tuple(o for o in self.outcomes if o.located and not o.is_complete)

    @property
    def is_complete(self) -> bool:
        return bool(self.outcomes) and all(o.is_complete for o in self.outcomes)

    def render(self) -> str:
        lines = [
            f"EAIP PARSE — {self.aerodrome}  (profile {self.profile_state})",
            "",
            f"{len(self.facts)} values from {len(self.outcomes)} section rules"
            + ("" if self.is_complete else "  ·  INCOMPLETE"),
        ]
        if not self.verified_profile:
            lines += [
                "",
                "!! This profile is not verified. Every value below is recorded "
                "at LOW confidence",
                "   and must be checked against the published page before "
                "anything depends on it.",
            ]
        lines += ["", "SECTIONS"]
        lines += [o.describe() for o in self.outcomes]
        if self.missed:
            lines += [
                "",
                "NOT FOUND — the profile looked for these and the document did "
                "not have them.",
                "They are coverage gaps, not values. Either the profile is "
                "wrong or the State",
                "has changed their layout; re-run the probe against the current "
                "page to see which.",
            ]
        if self.facts:
            lines += ["", "VALUES"]
            for held in self.facts:
                lines.append(f"  {held.entity:20} {held.attribute:26} {held.value}")
        return "\n".join(lines)


def _text_of(element: Element) -> str:
    return " ".join(element.text.split())


def _locate(rule: SectionRule, elements: list[Element]) -> Element | None:
    for element in elements:
        if rule.locate.pattern:
            if element.identifier and rule.locate.matches_attribute(
                element.identifier if rule.locate.attribute == "id"
                else " ".join(element.classes)
            ):
                return element
        elif rule.locate.matches_heading(_text_of(element)):
            return element
    return None


def _convert(text: str, field: FieldRule) -> Any | None:
    """Turn matched text into the declared kind, or return ``None``.

    ``None`` means the text was there and could not be read as what the profile
    declared it to be. That is a miss, not a value — storing the raw string
    would put "45 m" where a number belongs and nothing downstream would
    compare against it.
    """
    if field.kind in ("number", "integer"):
        found = _NUMBER.search(text)
        if found is None:
            return None
        try:
            number = float(found.group().replace(",", "."))
        except ValueError:
            return None
        if field.kind == "number":
            return number
        # A count that is not whole is not a count. Better to report the field
        # as unread than to silently truncate a fire category of 9.5 to 9.
        return int(number) if number.is_integer() else None
    cleaned = text.strip()
    if field.kind == "code":
        # A code read out of prose carries the sentence's punctuation with it,
        # and "80/F/A/W/T." is not a PCN — PavementRating.parse refuses it, in
        # a module three steps away from the one that introduced the full stop.
        cleaned = cleaned.strip(" .,;:")
    return cleaned or None


def _read_field(
    element: Element, field: FieldRule, others: tuple[FieldRule, ...]
) -> Any | None:
    """Find one labelled value in a section.

    **Rows first, and rows are the whole point.** An eAIP lays values out as
    label and value in adjacent cells, so a row whose label matches yields that
    row's own remaining cells and nothing beyond them. An earlier version
    matched the label against the section's flattened text and read the next
    120 characters, which walked straight into the following row: with the
    width cell reading "see remarks", it returned 80 from the PCN below it. A
    runway width of 80 m, confidently, from the pavement rating.

    Prose is the fallback, for sections a State writes as text rather than a
    table, and it is bounded — the window stops at the next label this profile
    knows about, so a reader can never cross into another field's territory
    even where there are no cells to stop it.
    """
    for row in element.rows:
        for index, cell in enumerate(row):
            if re.search(field.label, cell, re.I) is None:
                continue
            # The value is what follows the label in this row. Where the label
            # cell also carries it ("Width of RWY 60 m"), take the remainder of
            # that cell rather than the next one.
            after = cell[re.search(field.label, cell, re.I).end():]
            for candidate in [after, *row[index + 1 :]]:
                value = _convert(candidate, field)
                if value is not None:
                    return value
            return None

    text = _text_of(element)
    match = re.search(field.label, text, re.I)
    if match is None:
        return None
    window = text[match.end() :]
    # Never read past another field's label. Without this the fallback has the
    # same defect the row rule was written to remove.
    boundaries = [
        found.start()
        for other in others
        if other is not field
        for found in [re.search(other.label, window, re.I)]
        if found is not None
    ]
    if boundaries:
        window = window[: min(boundaries)]
    return _convert(window[:120], field)


def parse_page(
    html: str,
    profile: EaipProfile,
    *,
    aerodrome: str,
    document: str,
    valid_from: date,
    source_id: str = "",
    original_url: str = "",
    retrieved_at: datetime | None = None,
    precedence: Precedence = Precedence.AIP,
) -> ParseResult:
    """Read one page with one profile. Emits only what the profile located."""
    key = normalise(aerodrome)
    if not key:
        raise ValueError("aerodrome must be a non-empty location indicator")
    if not profile.sections:
        raise ValueError(
            f"the {profile.state} profile defines no sections, so this would "
            "parse nothing and report success. Run the probe against the page "
            "and add section rules first."
        )

    read_at = retrieved_at or datetime.now(timezone.utc)
    content_hash = hashlib.sha256(html.encode("utf-8", "replace")).hexdigest()
    elements = read_document(html)

    facts: list[Fact] = []
    outcomes: list[SectionOutcome] = []
    for rule in profile.sections:
        element = _locate(rule, elements)
        if element is None:
            outcomes.append(
                SectionOutcome(
                    code=rule.code,
                    located=False,
                    fields_expected=len(rule.fields),
                    detail=f"nothing matched {rule.locate.describe()}",
                )
            )
            continue

        found = 0
        for field in rule.fields:
            value = _read_field(element, field, rule.fields)
            if value is None:
                continue
            found += 1
            entity = under(key, field.scope) if field.scope else key
            facts.append(
                Fact(
                    entity=entity,
                    attribute=field.attribute,
                    value=value,
                    valid_from=valid_from,
                    precedence=precedence,
                    source=SourceRef(
                        source_id=source_id or profile.state,
                        document=document,
                        locator=f"{rule.code} ({rule.locate.describe()})",
                        retrieved_at=read_at,
                        content_hash=content_hash,
                        parser_id=f"eaip-profile:{profile.state}",
                        parser_version=PARSER_VERSION,
                        # An unverified profile is a hypothesis, and every
                        # value it produces inherits that.
                        confidence=(
                            Confidence.HIGH if profile.is_verified else Confidence.LOW
                        ),
                        original_url=original_url or None,
                    ),
                )
            )
        outcomes.append(
            SectionOutcome(
                code=rule.code, located=True,
                fields_expected=len(rule.fields), fields_found=found,
            )
        )

    return ParseResult(
        aerodrome=key,
        profile_state=profile.state,
        verified_profile=profile.is_verified,
        facts=tuple(facts),
        outcomes=tuple(outcomes),
        content_hash=content_hash,
    )

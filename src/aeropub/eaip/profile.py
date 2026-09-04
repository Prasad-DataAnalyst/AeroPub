"""What one State's eAIP looks like, as data rather than code.

A profile says three things: how to find a section in the document, how to find
a value inside that section, and what the value means. Nothing else. It cannot
express "and if that fails, try this instead", because a fallback is how a
parser starts producing values it did not really find.

The shape
---------
::

    {
      "state": "OT",
      "name": "Qatar",
      "toolchain": "eurocontrol",
      "sections": [
        {
          "code": "AD 2.12",
          "locate": {"attribute": "id", "pattern": "AD-2\\\\.12"},
          "fields": [
            {"attribute": "runway_width_m", "label": "Width",
             "unit": "m", "kind": "number"}
          ]
        }
      ]
    }

``locate`` finds the section element: an ``id``, a ``class``, or a heading whose
text matches. ``fields`` name what to pull out of it and what each is called in
the fact store — so the same value from two States lands under the same
attribute, which is the whole point of having a fact store rather than a pile
of documents.

Verification, and why it is a field
-----------------------------------
:attr:`EaipProfile.verified_at` records that a person looked at a draft profile
beside the real page and confirmed it. A profile the probe wrote and nobody
checked is a hypothesis. The registry already keeps registered and verified
apart for URLs; this keeps them apart for layouts, and for the same reason —
an unverified guess that looks like a confirmed fact is the failure mode this
whole project is built against.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

__all__ = [
    "EaipProfile",
    "FieldRule",
    "Locator",
    "ProfileError",
    "SectionRule",
    "load_layout",
]


class ProfileError(ValueError):
    """A profile that cannot be read, or does not describe anything findable."""


#: What a field's text is turned into. Anything not listed is refused rather
#: than passed through as a string, because a runway width stored as the text
#: "45 m" is not a number and will not compare against anything.
#:
#: ``integer`` is separate from ``number`` on purpose, and it is not fussiness.
#: A fire category read as ``number`` becomes ``9.0``, and the RFFS check reads
#: the published category with ``int()`` — which raises on ``"9.0"`` and reports
#: the aerodrome as publishing something it could not interpret. A count is not
#: a measurement, and storing one as the other breaks quietly, downstream, in a
#: module nobody was editing at the time.
KINDS = ("number", "integer", "text", "code")


@dataclass(frozen=True, slots=True)
class Locator:
    """How to find an element: by attribute value, or by heading text."""

    attribute: str = ""
    """``id``, ``class``, or any attribute whose value identifies the element."""

    pattern: str = ""
    """Regular expression the attribute value must match, anchored at both ends
    so a pattern for ``AD-2.1`` cannot also match ``AD-2.12``."""

    heading: str = ""
    """Alternative: a regular expression matched against heading text, for
    documents that carry no usable identifiers. Weaker, and the probe says so."""

    def __post_init__(self) -> None:
        if not (self.pattern or self.heading):
            raise ProfileError(
                "a locator needs either an attribute pattern or a heading "
                "pattern. One that matches everything finds nothing useful."
            )
        if self.pattern and not self.attribute:
            raise ProfileError("a locator with a pattern must name the attribute")
        for expression in (self.pattern, self.heading):
            if expression:
                try:
                    re.compile(expression)
                except re.error as error:
                    raise ProfileError(f"{expression!r} is not a regex: {error}") from None

    @property
    def by_heading(self) -> bool:
        return not self.pattern

    def matches_attribute(self, value: str) -> bool:
        return bool(self.pattern) and re.fullmatch(self.pattern, value or "") is not None

    def matches_heading(self, text: str) -> bool:
        return bool(self.heading) and re.search(self.heading, text or "", re.I) is not None

    def describe(self) -> str:
        if self.pattern:
            return f"{self.attribute}=~/{self.pattern}/"
        return f"heading=~/{self.heading}/"


@dataclass(frozen=True, slots=True)
class FieldRule:
    """One value to pull out of a section, and what it is called."""

    attribute: str
    """The fact-store attribute this becomes — ``runway_width_m``, ``pcn``.

    Deliberately the platform's own vocabulary, not the State's label. Two
    States calling the same thing "Width" and "Largeur" must land under one
    attribute or nothing downstream can compare them."""

    label: str
    """Regular expression matched against the row label or cell text in the
    published document. This is the State's wording."""

    kind: str = "text"
    unit: str = ""
    scope: str = ""
    """``runway`` where the value belongs to a runway rather than the aerodrome.

    Without it a per-runway PCN would be filed against the aerodrome, and the
    entity key grammar would then answer the wrong question."""

    def __post_init__(self) -> None:
        if not self.attribute.strip():
            raise ProfileError("a field rule needs an attribute name")
        if not self.label.strip():
            raise ProfileError(f"{self.attribute}: a field rule needs a label pattern")
        if self.kind not in KINDS:
            raise ProfileError(
                f"{self.attribute}: kind must be one of {', '.join(KINDS)}; "
                f"got {self.kind!r}. A value with no declared kind would be "
                "stored as whatever text happened to be there."
            )
        try:
            re.compile(self.label)
        except re.error as error:
            raise ProfileError(f"{self.attribute}: {self.label!r} is not a regex: {error}") from None


@dataclass(frozen=True, slots=True)
class SectionRule:
    """One AIP section, where to find it, and what to read from it."""

    code: str
    locate: Locator
    fields: tuple[FieldRule, ...] = ()

    def __post_init__(self) -> None:
        if not self.code.strip():
            raise ProfileError("a section rule needs the AIP section code")


@dataclass(frozen=True, slots=True)
class EaipProfile:
    """How to read one State's eAIP. Data, not code."""

    state: str
    name: str = ""
    toolchain: str = ""
    """``eurocontrol`` where the State uses that toolchain, else the State's own
    name for it, or empty. Recorded because it predicts what else will work,
    not because anything branches on it."""

    sections: tuple[SectionRule, ...] = ()
    verified_at: datetime | None = None
    verified_by: str = ""
    source_url: str = ""
    note: str = ""

    def __post_init__(self) -> None:
        if not self.state.strip():
            raise ProfileError("a profile needs the State's location-indicator prefix")
        codes = [s.code for s in self.sections]
        repeated = {c for c in codes if codes.count(c) > 1}
        if repeated:
            raise ProfileError(
                f"{', '.join(sorted(repeated))} appear more than once. Two rules "
                "for one section means whichever runs last wins, silently."
            )

    @property
    def is_verified(self) -> bool:
        """Whether a person confirmed this against the real page.

        A profile the probe wrote and nobody checked is a hypothesis. Parsing
        with one is allowed — it has to be, or nobody could ever verify it —
        but every value it produces is marked, and the coverage board says
        unverified.
        """
        return self.verified_at is not None

    def section(self, code: str) -> SectionRule | None:
        wanted = " ".join(code.strip().upper().split())
        return next((s for s in self.sections if s.code.upper() == wanted), None)

    def describe(self) -> str:
        state = f"{self.state}{' — ' + self.name if self.name else ''}"
        mark = (
            f"verified {self.verified_at:%Y-%m-%d}"
            + (f" by {self.verified_by}" if self.verified_by else "")
            if self.is_verified
            else "UNVERIFIED — a draft nobody has checked against the page"
        )
        return f"{state}  ·  {len(self.sections)} sections  ·  {mark}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "name": self.name,
            "toolchain": self.toolchain,
            "source_url": self.source_url,
            "verified_at": self.verified_at.isoformat() if self.verified_at else None,
            "verified_by": self.verified_by,
            "note": self.note,
            "sections": [
                {
                    "code": rule.code,
                    "locate": (
                        {"attribute": rule.locate.attribute, "pattern": rule.locate.pattern}
                        if rule.locate.pattern
                        else {"heading": rule.locate.heading}
                    ),
                    "fields": [
                        {
                            "attribute": f.attribute,
                            "label": f.label,
                            "kind": f.kind,
                            **({"unit": f.unit} if f.unit else {}),
                            **({"scope": f.scope} if f.scope else {}),
                        }
                        for f in rule.fields
                    ],
                }
                for rule in self.sections
            ],
        }

    def dumps(self, *, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)


def _locator(block: Any, *, where: str) -> Locator:
    if not isinstance(block, Mapping):
        raise ProfileError(f"{where}: locate must be an object")
    return Locator(
        attribute=str(block.get("attribute", "")).strip(),
        pattern=str(block.get("pattern", "")).strip(),
        heading=str(block.get("heading", "")).strip(),
    )


def load_layout(path: Path | str) -> EaipProfile:
    """Read a layout profile from JSON, with the filename on any error.

    Named ``load_layout`` rather than ``load_profile`` because
    :func:`aeropub.operator.load_profile` already loads an *operator* profile —
    a fleet and a network. Two things called a profile is tolerable inside
    their own modules; one name meaning both at package level is how somebody
    imports the wrong one.
    """
    path = Path(path)
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise ProfileError(f"{path}: cannot be read — {error}") from None
    except json.JSONDecodeError as error:
        raise ProfileError(f"{path}: not valid JSON — {error}") from None
    if not isinstance(loaded, Mapping):
        raise ProfileError(f"{path}: the profile must be a JSON object")

    rules: list[SectionRule] = []
    for index, entry in enumerate(loaded.get("sections", [])):
        where = f"{path}: sections[{index}]"
        if not isinstance(entry, Mapping):
            raise ProfileError(f"{where}: must be an object")
        fields: list[FieldRule] = []
        for position, item in enumerate(entry.get("fields", [])):
            if not isinstance(item, Mapping):
                raise ProfileError(f"{where}: fields[{position}] must be an object")
            fields.append(
                FieldRule(
                    attribute=str(item.get("attribute", "")).strip(),
                    label=str(item.get("label", "")).strip(),
                    kind=str(item.get("kind", "text")).strip(),
                    unit=str(item.get("unit", "")).strip(),
                    scope=str(item.get("scope", "")).strip(),
                )
            )
        rules.append(
            SectionRule(
                code=str(entry.get("code", "")).strip(),
                locate=_locator(entry.get("locate"), where=where),
                fields=tuple(fields),
            )
        )

    verified = loaded.get("verified_at")
    return EaipProfile(
        state=str(loaded.get("state", "")).strip(),
        name=str(loaded.get("name", "")).strip(),
        toolchain=str(loaded.get("toolchain", "")).strip(),
        sections=tuple(rules),
        verified_at=(
            datetime.fromisoformat(str(verified).replace("Z", "+00:00"))
            if verified
            else None
        ),
        verified_by=str(loaded.get("verified_by", "")).strip(),
        source_url=str(loaded.get("source_url", "")).strip(),
        note=str(loaded.get("note", "")).strip(),
    )

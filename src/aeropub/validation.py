"""The validation harness — what stops a parser writing nonsense into the store.

A self-building parser without this is a self-corrupting one. Every extracted
value passes these checks before it can become a fact, and a failure quarantines
it rather than publishing it quietly.

Findings are graded, because the three cases need different handling and
collapsing them causes real harm in both directions:

``INVALID``
    Cannot be true. A negative runway, a fire category of 14. Quarantine.

``SUSPECT``
    Outside the range real aerodromes occupy. Usually a unit error — a
    declared distance in feet stored as metres — so hold it for confirmation
    rather than discarding it.

``ADVISORY``
    Unusual but legitimate. Publish, with a note. Treating these as failures
    trains people to ignore the harness, which is worse than not having one.

A note on what is *not* asserted here. It is tempting to require LDA ≤ TORA,
since a displaced threshold normally makes landing distance the shorter figure.
But a State may shorten TORA for obstacle or intersection reasons while landing
distance stays full length, so the relationship is usual rather than
guaranteed. It is an advisory. Asserting it as an invariant would quarantine
correctly published data, and a harness that cries wolf gets switched off.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterable, Mapping, Sequence

from aeropub.facts import Fact

__all__ = [
    "Finding",
    "Severity",
    "Range",
    "RANGES",
    "check_value",
    "check_declared_distances",
    "check_continuity",
    "check_agreement",
    "validate",
]

#: A numeric change larger than this against a value's own history is held for
#: confirmation. Real declared distances move by tens of metres, not by half.
CONTINUITY_THRESHOLD = 0.5


class Severity(str, Enum):
    INVALID = "invalid"
    SUSPECT = "suspect"
    ADVISORY = "advisory"

    @property
    def blocks_publication(self) -> bool:
        return self in (Severity.INVALID, Severity.SUSPECT)


@dataclass(frozen=True, slots=True)
class Finding:
    """One thing wrong, or worth a second look."""

    severity: Severity
    rule: str
    message: str
    facts: tuple[Fact, ...] = ()

    @property
    def blocks_publication(self) -> bool:
        return self.severity.blocks_publication

    def describe(self) -> str:
        return f"[{self.severity.value}] {self.rule}: {self.message}"


@dataclass(frozen=True, slots=True)
class Range:
    """Physical and typical bounds for one numeric attribute."""

    attribute: str
    label: str
    hard_min: float
    hard_max: float
    unit: str | None = None
    typical_min: float | None = None
    typical_max: float | None = None
    note: str = ""


RANGES: dict[str, Range] = {
    r.attribute: r
    for r in [
        # Declared distances. The longest runways in the world are around
        # 5 500 m; below about 100 m nothing is a runway. A value in the tens of
        # thousands is almost always feet stored as metres.
        Range("tora_m", "take-off run available", 100, 6000, "m", 500, 4500),
        Range("toda_m", "take-off distance available", 100, 6500, "m", 500, 5000),
        Range("asda_m", "accelerate-stop distance available", 100, 6500, "m", 500, 5000),
        Range("lda_m", "landing distance available", 100, 6000, "m", 500, 4500),
        Range(
            "displaced_threshold_m", "displaced threshold", 0, 3000, "m", 0, 1000,
            note="Zero is normal and means no displacement.",
        ),
        # ICAO defines categories 1 to 10. Nothing else exists.
        Range("rffs_category", "rescue and fire fighting category", 1, 10),
        # Code A runways start at 18 m wide; 80 m is beyond any code F runway.
        Range("runway_width_m", "runway width", 7, 100, "m", 18, 60),
        # Steep approaches reach 5.5°; below 2° is not a glide path.
        Range("papi_angle", "PAPI glide path angle", 2.0, 7.0, "°", 2.5, 5.5),
        # Hard bounds only, deliberately. The Dead Sea aerodromes sit near
        # -1 270 ft and Daocheng Yading near 14 500 ft, so the plausible range
        # is very nearly the physical one and any narrower band would flag real
        # aerodromes. A typical band would not help here anyway: elevation in
        # metres stored as feet gives a smaller number that stays in range and
        # is undetectable by magnitude alone.
        Range("elevation_ft", "aerodrome elevation", -1500, 15000, "ft"),
        Range("latitude", "latitude", -90, 90, "°"),
        Range("longitude", "longitude", -180, 180, "°"),
        # QNH in hectopascals. A value near 30 is inches of mercury mislabelled.
        Range("qnh_hpa", "QNH", 850, 1100, "hPa", 950, 1050),
        Range("magnetic_variation", "magnetic variation", -180, 180, "°", -40, 40),
    ]
}


def _numeric(value) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def check_value(fact: Fact) -> list[Finding]:
    """Range and plausibility checks on a single value."""
    bounds = RANGES.get(fact.attribute)
    if bounds is None:
        return []

    number = _numeric(fact.value)
    if number is None:
        return [
            Finding(
                Severity.INVALID,
                "type",
                f"{bounds.label} should be numeric, got {fact.value!r}",
                (fact,),
            )
        ]

    unit = f" {bounds.unit}" if bounds.unit else ""

    if number < bounds.hard_min or number > bounds.hard_max:
        return [
            Finding(
                Severity.INVALID,
                "range",
                f"{bounds.label} of {number:g}{unit} is outside the physical range "
                f"{bounds.hard_min:g} to {bounds.hard_max:g}{unit}",
                (fact,),
            )
        ]

    if bounds.typical_min is not None and number < bounds.typical_min:
        return [
            Finding(
                Severity.SUSPECT,
                "unlikely",
                f"{bounds.label} of {number:g}{unit} is below the range real "
                f"aerodromes occupy; check the unit and the extraction",
                (fact,),
            )
        ]
    if bounds.typical_max is not None and number > bounds.typical_max:
        return [
            Finding(
                Severity.SUSPECT,
                "unlikely",
                f"{bounds.label} of {number:g}{unit} is above the range real "
                f"aerodromes occupy; check the unit and the extraction",
                (fact,),
            )
        ]
    return []


def check_declared_distances(facts: Mapping[str, Fact]) -> list[Finding]:
    """Relationships between the four declared distances for one runway.

    TODA and ASDA are TORA plus a clearway or stopway, so neither can be
    shorter than TORA. That is arithmetic, not convention, and a violation
    means the extraction is wrong.
    """
    findings: list[Finding] = []

    def value(name: str) -> float | None:
        fact = facts.get(name)
        return _numeric(fact.value) if fact is not None else None

    tora, toda, asda, lda = (value(n) for n in ("tora_m", "toda_m", "asda_m", "lda_m"))

    if tora is not None and toda is not None and toda < tora:
        findings.append(
            Finding(
                Severity.INVALID,
                "declared-distances",
                f"TODA ({toda:g} m) is shorter than TORA ({tora:g} m); a clearway "
                "cannot subtract from the take-off run",
                tuple(f for f in (facts.get("tora_m"), facts.get("toda_m")) if f),
            )
        )

    if tora is not None and asda is not None and asda < tora:
        findings.append(
            Finding(
                Severity.INVALID,
                "declared-distances",
                f"ASDA ({asda:g} m) is shorter than TORA ({tora:g} m); a stopway "
                "cannot subtract from the take-off run",
                tuple(f for f in (facts.get("tora_m"), facts.get("asda_m")) if f),
            )
        )

    if tora is not None and lda is not None and lda > tora:
        findings.append(
            Finding(
                Severity.ADVISORY,
                "declared-distances",
                f"LDA ({lda:g} m) exceeds TORA ({tora:g} m). Legitimate where "
                "take-off run is shortened for obstacle or intersection reasons "
                "while landing distance stays full length, but worth confirming",
                tuple(f for f in (facts.get("tora_m"), facts.get("lda_m")) if f),
            )
        )

    displaced = value("displaced_threshold_m")
    if displaced is not None and tora is not None and displaced >= tora:
        findings.append(
            Finding(
                Severity.INVALID,
                "declared-distances",
                f"threshold displacement ({displaced:g} m) is not shorter than the "
                f"take-off run ({tora:g} m), which would leave no runway",
                tuple(
                    f
                    for f in (facts.get("displaced_threshold_m"), facts.get("tora_m"))
                    if f
                ),
            )
        )

    return findings


def check_continuity(new: Fact, history: Sequence[Fact]) -> list[Finding]:
    """Hold a value that jumps sharply against its own past.

    Real declared distances move by tens of metres when works change a runway.
    A figure that halves is far more likely to be a parse error than a State
    rebuilding an aerodrome.
    """
    if not history:
        return []

    number = _numeric(new.value)
    previous = next(
        (_numeric(f.value) for f in reversed(history) if _numeric(f.value) is not None),
        None,
    )
    if number is None or previous is None or previous == 0:
        return []

    relative = abs(number - previous) / abs(previous)
    if relative <= CONTINUITY_THRESHOLD:
        return []

    label = RANGES[new.attribute].label if new.attribute in RANGES else new.attribute
    return [
        Finding(
            Severity.SUSPECT,
            "continuity",
            f"{label} moved from {previous:g} to {number:g}, a change of "
            f"{relative:.0%} against its own history; confirm before publishing",
            (new,),
        )
    ]


def check_agreement(facts: Sequence[Fact]) -> list[Finding]:
    """Independent sources disagreeing about the same thing.

    Only compares facts at the same precedence. An AIP saying 3 900 and a NOTAM
    saying 3 100 is the layering working exactly as intended, not a conflict —
    treating that as disagreement would flag every temporary restriction in the
    world.
    """
    findings: list[Finding] = []
    by_layer: dict[tuple[str, str, int], list[Fact]] = {}

    for fact in facts:
        by_layer.setdefault(
            (fact.entity, fact.attribute, int(fact.precedence)), []
        ).append(fact)

    for (entity, attribute, _), group in sorted(by_layer.items()):
        sources = {f.source.source_id for f in group}
        values = {f.value for f in group}
        if len(sources) > 1 and len(values) > 1:
            rendered = ", ".join(
                f"{f.source.source_id} says {f.value!r}" for f in group
            )
            findings.append(
                Finding(
                    Severity.SUSPECT,
                    "cross-source",
                    f"{entity} {attribute}: independent sources disagree — {rendered}",
                    tuple(group),
                )
            )
    return findings


def validate(
    facts: Iterable[Fact],
    *,
    history: Mapping[tuple[str, str], Sequence[Fact]] | None = None,
) -> list[Finding]:
    """Run every check over a batch of extracted facts.

    ``history`` maps ``(entity, attribute)`` to what was previously held, so
    continuity can be checked. Omit it when there is nothing to compare against.
    """
    batch = list(facts)
    findings: list[Finding] = []
    rejected: set[int] = set()

    for fact in batch:
        found = check_value(fact)
        findings.extend(found)
        if any(f.severity is Severity.INVALID for f in found):
            rejected.add(id(fact))

    # Relationship checks skip values that already failed on their own. A
    # landing distance of 13 000 m is one extraction error, and reporting that
    # it also exceeds the take-off run adds nothing but noise to a queue a human
    # has to work through.
    by_entity: dict[str, dict[str, Fact]] = {}
    for fact in batch:
        if id(fact) in rejected:
            continue
        by_entity.setdefault(fact.entity, {})[fact.attribute] = fact
    for attributes in by_entity.values():
        findings.extend(check_declared_distances(attributes))

    if history:
        for fact in batch:
            findings.extend(check_continuity(fact, history.get(fact.key, ())))

    findings.extend(check_agreement(batch))
    return findings

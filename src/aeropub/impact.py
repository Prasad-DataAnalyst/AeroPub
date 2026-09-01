"""Generic operational impact — why a change matters, to anybody.

Layer two of three. Still no fleet, no network, no customer: this says what a
change means in general aviation terms, which is the same for every operator
reading it. Only layer three — severity for a particular fleet at a particular
aerodrome in a particular role — needs to know who is asking.

Every statement here is written to hold for any operator. "Recompute required
landing distance for any type previously LDA-limited here" is a generic impact.
"Your 777s can no longer dispatch" is not; that belongs to the tenant layer.

Where no rule covers an attribute, the assessment says so rather than inventing
a consequence. A plausible-sounding sentence about an attribute nobody has
modelled is worse than an admitted gap, because it reads exactly like one that
was.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from aeropub.changes import Change, ChangeKind

__all__ = ["Direction", "AttributeRule", "Impact", "assess", "RULES"]


class Direction(str, Enum):
    """Whether a change tightens or relaxes a constraint."""

    WORSE = "worse"
    BETTER = "better"
    """A constraint lifted. Worth surfacing on its own — nothing else in this
    domain tells an operator when something became possible again."""

    NEUTRAL = "neutral"
    """Changed, but not on an axis where better and worse mean anything."""

    UNKNOWN = "unknown"
    """No rule covers this attribute."""


@dataclass(frozen=True, slots=True)
class AttributeRule:
    """What one attribute means, and what moving it implies."""

    attribute: str
    label: str
    unit: str | None = None
    higher_is_better: bool | None = None
    """``None`` where the axis has no better or worse — a glide path angle,
    a frequency, an identifier."""

    worse: str = ""
    better: str = ""
    changed: str = ""
    """Used when direction is not meaningful."""

    added: str = ""
    removed: str = ""
    domains: tuple[str, ...] = ()


RULES: dict[str, AttributeRule] = {
    r.attribute: r
    for r in [
        AttributeRule(
            attribute="lda_m",
            label="landing distance available",
            unit="m",
            higher_is_better=True,
            worse=(
                "Landing distance available is reduced. Recompute required landing "
                "distance for any type previously LDA-limited here, and re-check the "
                "wet and contaminated cases, which lose margin fastest."
            ),
            better=(
                "Landing distance available is restored. Any landing-weight "
                "restriction derived from the previous figure can be reassessed."
            ),
            removed="Landing distance is no longer published; the runway may be closed to landing.",
            domains=("performance", "dispatch", "charts"),
        ),
        AttributeRule(
            attribute="tora_m",
            label="take-off run available",
            unit="m",
            higher_is_better=True,
            worse=(
                "Take-off run available is reduced. Recompute maximum take-off "
                "weight; field length becomes limiting sooner, and more so at high "
                "temperature or elevation."
            ),
            better="Take-off run available is restored; previous field-length limits can be reassessed.",
            domains=("performance", "dispatch"),
        ),
        AttributeRule(
            attribute="toda_m",
            label="take-off distance available",
            unit="m",
            higher_is_better=True,
            worse="Take-off distance available is reduced. Recompute maximum take-off weight.",
            better="Take-off distance available is restored.",
            domains=("performance", "dispatch"),
        ),
        AttributeRule(
            attribute="asda_m",
            label="accelerate-stop distance available",
            unit="m",
            higher_is_better=True,
            worse=(
                "Accelerate-stop distance available is reduced. V1 and the rejected "
                "take-off case need recomputing; this often binds before take-off run."
            ),
            better="Accelerate-stop distance available is restored.",
            domains=("performance", "dispatch"),
        ),
        AttributeRule(
            attribute="rffs_category",
            label="rescue and fire fighting category",
            higher_is_better=True,
            worse=(
                "The aerodrome now provides a lower rescue and fire fighting "
                "category. Any aircraft requiring more than the new category is "
                "affected, and suitability must be reconsidered for every role the "
                "aerodrome is held in — destination, alternate and diversion alike."
            ),
            better=(
                "A higher rescue and fire fighting category is now provided. Aircraft "
                "previously excluded on this ground may become admissible."
            ),
            removed="No rescue and fire fighting category is published; suitability cannot be assumed.",
            domains=("suitability", "dispatch", "alternates"),
        ),
        AttributeRule(
            attribute="pcn",
            label="pavement classification number",
            higher_is_better=True,
            worse=(
                "Published pavement strength is reduced. Any aircraft whose "
                "classification number exceeds the new figure needs a compatibility "
                "check, and operations at maximum weight may no longer be supportable."
            ),
            better="Published pavement strength is increased; previous weight restrictions can be reassessed.",
            domains=("suitability", "performance"),
        ),
        AttributeRule(
            attribute="pcr",
            label="pavement classification rating",
            higher_is_better=True,
            worse=(
                "Published pavement rating is reduced. Aircraft classification "
                "ratings must be re-checked against the new figure."
            ),
            better="Published pavement rating is increased.",
            domains=("suitability", "performance"),
        ),
        AttributeRule(
            attribute="papi_angle",
            label="PAPI glide path angle",
            unit="°",
            higher_is_better=None,
            changed=(
                "The visual glide path angle has changed. Threshold crossing height "
                "and wheel clearance change with it, which matters most for aircraft "
                "with a large eye-to-wheel height."
            ),
            domains=("procedures", "charts", "crew"),
        ),
        AttributeRule(
            attribute="displaced_threshold_m",
            label="displaced threshold",
            unit="m",
            higher_is_better=False,
            worse=(
                "The threshold is displaced further. Landing distance available and "
                "the approach profile both change; the published declared distances "
                "should be checked for consistency with this figure."
            ),
            better="The threshold displacement is reduced or removed.",
            domains=("performance", "procedures", "charts"),
        ),
        AttributeRule(
            attribute="runway_width_m",
            label="runway width",
            unit="m",
            higher_is_better=True,
            worse=(
                "The runway is narrower than published previously. Aerodrome "
                "reference code compatibility should be re-checked for wider-bodied "
                "types."
            ),
            better="The runway is wider than published previously.",
            domains=("suitability",),
        ),
    ]
}


@dataclass(frozen=True, slots=True)
class Impact:
    """A generic, operator-agnostic reading of one change."""

    change: Change
    direction: Direction
    summary: str
    consequence: str
    domains: tuple[str, ...] = ()
    assessed: bool = True
    """``False`` when no rule covers the attribute and nothing was inferred."""

    @property
    def is_opportunity(self) -> bool:
        return self.direction is Direction.BETTER

    def describe(self) -> str:
        return f"{self.summary} — {self.consequence}"


def _direction(change: Change, rule: AttributeRule | None) -> Direction:
    if rule is None:
        return Direction.UNKNOWN
    if change.kind is not ChangeKind.MODIFIED or rule.higher_is_better is None:
        return Direction.NEUTRAL
    delta = change.numeric_delta()
    if delta is None or delta == 0:
        return Direction.NEUTRAL
    improved = delta > 0 if rule.higher_is_better else delta < 0
    return Direction.BETTER if improved else Direction.WORSE


def _summary(change: Change, rule: AttributeRule | None) -> str:
    label = rule.label if rule else change.attribute
    unit = f" {rule.unit}" if rule and rule.unit else ""

    if change.kind is ChangeKind.ADDED:
        return f"{change.entity}: {label} published as {change.to_value}{unit}"
    if change.kind is ChangeKind.REMOVED:
        return f"{change.entity}: {label} withdrawn (was {change.from_value}{unit})"

    delta = change.numeric_delta()
    if delta is not None:
        movement = "increased" if delta > 0 else "reduced"
        size = abs(delta)
        rendered = f"{size:g}"
        return (
            f"{change.entity}: {label} {movement} by {rendered}{unit} "
            f"({change.from_value} → {change.to_value})"
        )
    return f"{change.entity}: {label} {change.from_value} → {change.to_value}"


def assess(change: Change) -> Impact:
    """Read one change in general operational terms, with no operator context."""
    rule = RULES.get(change.attribute)
    direction = _direction(change, rule)
    summary = _summary(change, rule)

    if rule is None:
        return Impact(
            change=change,
            direction=Direction.UNKNOWN,
            summary=summary,
            consequence=(
                "No generic assessment is available for this attribute. The change "
                "is recorded and cited; its operational reading needs a human."
            ),
            assessed=False,
        )

    if change.kind is ChangeKind.ADDED:
        consequence = rule.added or rule.changed or (
            "Newly published. Review against current assumptions for this aerodrome."
        )
    elif change.kind is ChangeKind.REMOVED:
        consequence = rule.removed or (
            "No longer published. Do not carry the previous value forward; treat "
            "the attribute as unknown until republished."
        )
    elif direction is Direction.WORSE:
        consequence = rule.worse
    elif direction is Direction.BETTER:
        consequence = rule.better
    else:
        consequence = rule.changed or rule.worse

    return Impact(
        change=change,
        direction=direction,
        summary=summary,
        consequence=consequence,
        domains=rule.domains,
    )

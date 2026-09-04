"""The review gate — who is accountable for a verdict before it reaches a crew.

The plan is unusually direct about why this exists, and it is worth repeating
because it changes what the module must do. A system with no attestation is
entirely buildable. It is **not sellable to an airline**, because an operator's
regulator will ask who is accountable for the data feeding an operational
decision, and "no one, the system decided" ends the conversation. The liability
runs the same way: one missed fire-category downgrade at a sole-suitable
alternate, published unreviewed, is an existential event for a young company.

So the gate is not a technical limit and it is not on the data plane. Finding,
fetching, parsing, validating, resolving, diffing, assessing and drafting are
**fully autonomous** — no human anywhere in that path. The gate sits on the one
step where a verdict reaches an operational consumer, and only there.

The three decisions this module makes carefully
-----------------------------------------------
**An attestation binds to what was attested.** A reviewer confirms a specific
finding, and if that finding changes the attestation does not carry over.
:func:`fingerprint` hashes the content a reviewer actually read, and a stale
attestation is refused rather than silently re-used. Without this, "you attested
to this" is not provable, and the whole point of the gate was provability.

**Audit sampling is deterministic, not random.** The obvious implementation
draws a random sample, and it is wrong here: an auditor asking "why was this one
sampled and that one not" needs an answer, and "chance" is not one. The sample
is drawn from the fingerprint, so it is reproducible years later from the
finding alone, and the same finding is always either in or out.

**Unknown never auto-publishes, at any threshold.** An unmade check is not a
low-severity finding — it is the absence of a finding, and releasing it
unattended would publish "we did not look" as though it were "nothing to
report". A tenant can widen the gate as far as CRITICAL and this still holds.

Designed to move
----------------
The plan wants the auto-publish threshold to rise as accuracy is demonstrated,
with the auto-published share tracked as a product metric. :class:`GateLog`
reports it. What the log also records is the **default** the tenant departed
from, so a regulator reading it can see that a wider gate was a choice somebody
made rather than how the product arrived.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Iterable

from aeropub.operator import Exposure, ExposureFinding

__all__ = [
    "DEFAULT_AUTO_PUBLISH",
    "DEFAULT_SAMPLE_RATE",
    "Attestation",
    "Disposition",
    "GateLog",
    "Release",
    "ReviewGate",
    "StaleAttestation",
    "decide",
    "fingerprint",
    "review",
]

#: The plan's default: info and low auto-publish, medium auto-publishes with
#: audit sampling, high and critical need attestation. Expressed as the most
#: severe level that releases without a human.
DEFAULT_AUTO_PUBLISH = Exposure.MEDIUM

#: What share of auto-published findings are drawn for audit. Applied to the
#: medium band, which is where the plan puts sampling.
DEFAULT_SAMPLE_RATE = 0.1


class StaleAttestation(ValueError):
    """An attestation that does not match the finding it is offered for.

    Raised rather than ignored. Silently declining to apply a stale attestation
    would look identical to never having one, and the reviewer would not learn
    that the thing they signed for has moved underneath them.
    """


class Disposition(str, Enum):
    """What happened to one finding at the gate."""

    PUBLISHED = "published"
    """Released to the operational consumer with no human in the path."""

    SAMPLED = "sampled"
    """Released, and drawn for audit. Still published — sampling is a check on
    the system, not a hold on the finding."""

    HELD = "held"
    """Awaiting attestation. **Not** invisible: a held finding is in front of
    the reviewer, it is simply not yet in front of a crew."""

    ATTESTED = "attested"
    """A person confirmed it, and it released."""

    WITHHELD = "withheld"
    """A reviewer looked and declined to release it. A real outcome, and
    recorded as distinct from never having been reviewed."""

    @property
    def is_released(self) -> bool:
        return self in (
            Disposition.PUBLISHED,
            Disposition.SAMPLED,
            Disposition.ATTESTED,
        )

    @property
    def needs_a_person(self) -> bool:
        return self is Disposition.HELD


def fingerprint(finding: ExposureFinding) -> str:
    """A stable hash of what a reviewer would actually read.

    Covers the aeroplane, the check, the verdict, the reason and the role — the
    content of the judgement. It deliberately does **not** cover the timestamp
    of the run, so re-assessing an unchanged aerodrome does not invalidate an
    attestation; and it deliberately **does** cover the detail text, so a
    changed reason does.
    """
    material = " ".join(
        [
            finding.designator,
            finding.check.name,
            finding.check.scope,
            finding.check.assessment.value,
            finding.check.detail,
            finding.exposure.value,
            finding.reason,
            finding.role.value,
            "sole" if finding.sole_suitable else "",
        ]
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class Attestation:
    """A person taking responsibility for one specific finding."""

    by: str
    at: datetime
    finding: str
    """The fingerprint of what was attested. Binds the signature to the
    content, so "you attested to this" is provable rather than asserted."""

    released: bool = True
    """``False`` where the reviewer looked and declined. Declining is a
    decision and is recorded as one."""

    note: str = ""

    def __post_init__(self) -> None:
        if not self.by.strip():
            raise ValueError(
                "an attestation needs the person taking responsibility. That "
                "is the entire point of it."
            )
        if self.at.tzinfo is None:
            raise ValueError("an attestation must be timestamped in UTC")
        if len(self.finding) != 64:
            raise ValueError(
                "an attestation must carry the fingerprint of what was "
                "attested; without it the signature covers nothing"
            )

    def covers(self, finding: ExposureFinding) -> bool:
        return self.finding == fingerprint(finding)


@dataclass(frozen=True, slots=True)
class ReviewGate:
    """One tenant's policy for what reaches an operational consumer unattended."""

    tenant: str
    auto_publish_at_or_below: Exposure = DEFAULT_AUTO_PUBLISH
    sample_rate: float = DEFAULT_SAMPLE_RATE

    def __post_init__(self) -> None:
        if not self.tenant.strip():
            raise ValueError("a gate belongs to a tenant")
        if not 0.0 <= self.sample_rate <= 1.0:
            raise ValueError("sample_rate is a share between 0 and 1")

    @property
    def is_default(self) -> bool:
        return self.auto_publish_at_or_below is DEFAULT_AUTO_PUBLISH

    @property
    def is_widened(self) -> bool:
        """Whether this tenant releases more without a person than the default.

        Recorded so a regulator reading the log can see that a wider gate was a
        choice somebody made, rather than how the product arrived.
        """
        return self.auto_publish_at_or_below.rank < DEFAULT_AUTO_PUBLISH.rank

    def auto_publishes(self, exposure: Exposure) -> bool:
        """Whether this severity releases without a person.

        ``UNKNOWN`` never does, at any threshold. An unmade check is not a
        low-severity finding; it is the absence of one, and releasing it
        unattended publishes "we did not look" as though it were "nothing to
        report".
        """
        if exposure is Exposure.UNKNOWN:
            return False
        return exposure.rank >= self.auto_publish_at_or_below.rank

    def samples(self, mark: str) -> bool:
        """Whether this fingerprint is drawn for audit.

        Deterministic. An auditor asking why this one and not that one gets an
        answer that reproduces from the finding alone, years later — which
        "chance" does not.
        """
        if self.sample_rate <= 0.0:
            return False
        if self.sample_rate >= 1.0:
            return True
        return (int(mark[:8], 16) % 10000) < round(self.sample_rate * 10000)

    def describe(self) -> str:
        widened = "  (WIDENED from the default)" if self.is_widened else ""
        return (
            f"{self.tenant}: auto-publish at or below "
            f"{self.auto_publish_at_or_below.value}, "
            f"{self.sample_rate:.0%} audit sampling{widened}"
        )


@dataclass(frozen=True, slots=True)
class Release:
    """One finding's passage through the gate, and why it went that way."""

    finding: ExposureFinding
    mark: str
    disposition: Disposition
    reason: str
    gate: ReviewGate
    attestation: Attestation | None = None
    at: datetime | None = None

    @property
    def is_released(self) -> bool:
        return self.disposition.is_released

    @property
    def needs_a_person(self) -> bool:
        return self.disposition.needs_a_person

    def describe(self) -> str:
        who = f"  by {self.attestation.by}" if self.attestation else ""
        return (
            f"[{self.disposition.value}] {self.finding.designator} - "
            f"{self.finding.check.name} ({self.finding.exposure.value}){who} "
            f"- {self.reason}"
        )


def decide(
    gate: ReviewGate,
    finding: ExposureFinding,
    *,
    attestation: Attestation | None = None,
    at: datetime | None = None,
) -> Release:
    """Put one finding through the gate.

    An attestation offered for a different finding raises
    :class:`StaleAttestation` rather than being quietly ignored — a reviewer
    whose signature no longer applies needs to be told, not to have the finding
    silently fall back to held.
    """
    moment = at or datetime.now(timezone.utc)
    mark = fingerprint(finding)

    if attestation is not None:
        if not attestation.covers(finding):
            raise StaleAttestation(
                f"the attestation by {attestation.by} was for a different "
                "finding. What was reviewed has changed since it was signed, "
                "so the signature does not carry over — put the new wording in "
                "front of them again."
            )
        return Release(
            finding=finding,
            mark=mark,
            disposition=(
                Disposition.ATTESTED if attestation.released else Disposition.WITHHELD
            ),
            reason=(
                f"attested by {attestation.by}"
                if attestation.released
                else f"reviewed by {attestation.by} and not released"
            ),
            gate=gate,
            attestation=attestation,
            at=moment,
        )

    if not gate.auto_publishes(finding.exposure):
        why = (
            "an unmade check never releases unattended, at any threshold - "
            "publishing it would say 'we did not look' as though it were "
            "'nothing to report'"
            if finding.exposure is Exposure.UNKNOWN
            else f"{finding.exposure.value} is above this tenant's "
            f"{gate.auto_publish_at_or_below.value} threshold"
        )
        return Release(
            finding=finding, mark=mark, disposition=Disposition.HELD,
            reason=why, gate=gate, at=moment,
        )

    if gate.samples(mark):
        return Release(
            finding=finding, mark=mark, disposition=Disposition.SAMPLED,
            reason="released, and drawn for audit",
            gate=gate, at=moment,
        )
    return Release(
        finding=finding, mark=mark, disposition=Disposition.PUBLISHED,
        reason=f"{finding.exposure.value} releases without a person under this gate",
        gate=gate, at=moment,
    )


@dataclass(frozen=True, slots=True)
class GateLog:
    """Every decision the gate made, and the metric the plan wants tracked."""

    gate: ReviewGate
    releases: tuple[Release, ...] = ()

    @property
    def held(self) -> tuple[Release, ...]:
        return tuple(r for r in self.releases if r.needs_a_person)

    @property
    def released(self) -> tuple[Release, ...]:
        return tuple(r for r in self.releases if r.is_released)

    @property
    def withheld(self) -> tuple[Release, ...]:
        return tuple(
            r for r in self.releases if r.disposition is Disposition.WITHHELD
        )

    @property
    def sampled(self) -> tuple[Release, ...]:
        return tuple(
            r for r in self.releases if r.disposition is Disposition.SAMPLED
        )

    @property
    def auto_published_share(self) -> float:
        """The metric the plan wants climbing every cycle.

        Counts what released with no person in the path, over everything the
        gate saw. Attested releases are excluded from the numerator: a person
        was in that path, which is the whole distinction being measured.
        """
        if not self.releases:
            return 0.0
        automatic = sum(
            1
            for r in self.releases
            if r.disposition in (Disposition.PUBLISHED, Disposition.SAMPLED)
        )
        return round(automatic / len(self.releases), 4)

    def summary(self) -> dict[str, float | int]:
        return {
            "findings": len(self.releases),
            "published": sum(
                1 for r in self.releases if r.disposition is Disposition.PUBLISHED
            ),
            "sampled": len(self.sampled),
            "held": len(self.held),
            "attested": sum(
                1 for r in self.releases if r.disposition is Disposition.ATTESTED
            ),
            "withheld": len(self.withheld),
            "auto_published_share": self.auto_published_share,
        }

    def render(self) -> str:
        counts = self.summary()
        lines = [
            f"REVIEW GATE - {self.gate.tenant}",
            f"  {self.gate.describe()}",
            "",
            f"{counts['findings']} findings  ·  {counts['published']} published  "
            f"·  {counts['sampled']} sampled  ·  {counts['held']} held  ·  "
            f"{counts['attested']} attested  ·  {counts['withheld']} withheld",
            f"auto-published without a person: {self.auto_published_share:.0%}",
        ]
        if self.gate.is_widened:
            lines += [
                "",
                "!! This gate releases more without a person than the product "
                "default. That was a",
                "   choice this tenant made, and it is recorded here so it is "
                "visible in an audit.",
            ]
        if self.held:
            lines += ["", "AWAITING ATTESTATION - in front of a reviewer, not a crew"]
            lines += [f"  {r.describe()}" for r in self.held]
        if self.withheld:
            lines += ["", "REVIEWED AND NOT RELEASED"]
            lines += [f"  {r.describe()}" for r in self.withheld]
        rest = [
            r
            for r in self.releases
            if not r.needs_a_person and r.disposition is not Disposition.WITHHELD
        ]
        if rest:
            lines += ["", "RELEASED"]
            lines += [f"  {r.describe()}" for r in rest]
        return "\n".join(lines)


def review(
    gate: ReviewGate,
    findings: Iterable[ExposureFinding],
    *,
    attestations: Iterable[Attestation] = (),
    at: datetime | None = None,
) -> GateLog:
    """Put a set of findings through the gate, applying any attestations held.

    An attestation whose fingerprint matches nothing in this set is not an
    error — it is a signature for a finding that has since changed or gone
    away, and the finding it was for is simply not here any more.
    """
    by_mark = {a.finding: a for a in attestations}
    return GateLog(
        gate=gate,
        releases=tuple(
            decide(
                gate,
                finding,
                attestation=by_mark.get(fingerprint(finding)),
                at=at,
            )
            for finding in findings
        ),
    )

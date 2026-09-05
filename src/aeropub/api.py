"""The JSON the API returns — and two rules it cannot be talked out of.

Plan section 25 settled the design rules. Two of them are structural rather
than editorial, so they are enforced here in code instead of being left to
whoever writes the next endpoint.

**Provenance is never omitted.** Every value in every response travels with the
``source_ref`` block that produced it. An integrator who strips it downstream
has made a choice; the API does not make it for them. A serialised value with
no citation would be indistinguishable from one somebody typed, which is the
whole failure this system exists to prevent — and it is worse over an API than
on a screen, because nothing downstream can tell.

**Redistribution governs verbatim text, and unknown withholds.** The boundary
is the plan's own: *chart analysis is ours; chart images are theirs.* An
extracted figure — a declared distance, a fire category — is the analysis, and
it travels. A reproduced passage of a State's prose or a NOTAM's text is their
content, and it travels only where the licence is known to allow it. The
default for an unrecorded source is to withhold, because assuming permission is
the expensive mistake and a payload that quietly republished a State's text
under ``UNKNOWN`` is a licensing problem found by a lawyer rather than a test.

Getting that line wrong in the other direction is not harmless either. An
earlier draft withheld the *value* ``3900`` while the assessment beside it read
"reduced by 400 m (3900 → 3500)" — protection that leaks through the next field
is worse than none, because it reads as though something were being protected.

Withholding is never silent. A redacted field is replaced by an object saying
what was withheld and why, so an integrator sees a licence decision rather than
an empty string that looks like missing data.

What this module is not
-----------------------
Not an HTTP server. It builds the documents; routing, auth and transport belong
to whatever hosts it. Keeping them apart means the same payloads serve the API,
the offline package, the email report and the printed document without three of
them drifting.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from enum import Enum
from typing import Any, Callable, Iterable, Iterator, Mapping

from aeropub.aircraft import Characteristic
from aeropub.bulletin import ChangeBulletin, ReportedChange
from aeropub.charts import Chart, ChartReview, Minimum
from aeropub.dossier import AerodromeDossier, SectionEntry, ValueLine
from aeropub.facts import Fact
from aeropub.fleet import FleetScreen, OperatorFleet
from aeropub.gate import GateLog, Release
from aeropub.runtime import RuntimeReport
from aeropub.horizon import Horizon, Transition
from aeropub.lenses import LensView
from aeropub.obstacles import Obstacle, ObstacleChange, ObstacleReview
from aeropub.provenance import SourceRef
from aeropub.quality import QualityFinding, QualityReport
from aeropub.operator import ExposureFinding, OperatorAssessment
from aeropub.registry import Redistribution
from aeropub.retrospect import Blindness, LateArrival, Retrospect, Revision
from aeropub.route import RouteDossier
from aeropub.suitability import Check, Note, Suitability
from aeropub.currency import DataCurrency
from aeropub.sweep import AerodromeExposure, GroupRedundancy, NetworkSweep
from aeropub.trip import LegAssessment, TripAssessment

__all__ = [
    "API_VERSION",
    "Licensing",
    "VERBATIM_THRESHOLD",
    "document",
    "dumps",
    "operator_assessment",
    "suitability",
    "network_sweep",
    "obstacle_review",
    "gate_log",
    "trip_assessment",
    "retrospect_document",
    "ndjson",
    "to_json",
]

#: Path segment and payload version. Additive changes only within it; a
#: removal or a rename waits for v2 and two AIRAC cycles of notice, so an
#: integrator plans against the same calendar the data uses.
API_VERSION = "v1"

#: Redistribution states under which verbatim source text may travel.
#: CONDITIONAL is included because its conditions — attribution, currency
#: warnings — are satisfied by the citation the payload already carries.
_MAY_REPUBLISH = frozenset({Redistribution.PERMITTED, Redistribution.CONDITIONAL})


@dataclass(frozen=True, slots=True)
class Licensing:
    """What each source permits, and what to assume when nothing is recorded."""

    by_source: Mapping[str, Redistribution] = None  # type: ignore[assignment]
    default: Redistribution = Redistribution.UNKNOWN
    """Deliberately the most restrictive. A source nobody has recorded a
    licence for is not a source we may republish."""

    def for_source(self, source_id: str) -> Redistribution:
        if not self.by_source:
            return self.default
        return self.by_source.get(source_id, self.default)

    def may_republish(self, source_id: str) -> bool:
        return self.for_source(source_id) in _MAY_REPUBLISH


_PERMISSIVE = Licensing(by_source={}, default=Redistribution.PERMITTED)

#: Above this length a string value is treated as reproduced prose rather than
#: an extracted data point, and the licence gate applies to it. The number is a
#: judgement, and stated as one: a runway designator, a surface type or a fire
#: category is plainly a fact, and three paragraphs of local regulations is
#: plainly the State's text. Anything near the boundary should carry its own
#: verbatim marker rather than lean on a length.
VERBATIM_THRESHOLD = 200


def _is_verbatim(value: Any) -> bool:
    """Whether a value is reproduced text rather than an extracted fact."""
    return isinstance(value, str) and len(value) > VERBATIM_THRESHOLD


def _withheld(source_id: str, licensing: Licensing) -> dict[str, Any]:
    """What stands in place of text we may not republish.

    An object rather than an empty string, so an integrator sees a licence
    decision instead of what looks like missing data.
    """
    return {
        "withheld": True,
        "reason": "redistribution not permitted for this source",
        "redistribution": licensing.for_source(source_id).value,
        "source_id": source_id,
    }


def _moment(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        raise ValueError(
            "every timestamp in an API payload must be timezone-aware; a naive "
            "one is unreadable by an integrator in another timezone"
        )
    return value.astimezone(timezone.utc).isoformat()


def _day(value: date | None) -> str | None:
    return value.isoformat() if value is not None else None


def _enum(value: Any) -> Any:
    return value.value if isinstance(value, Enum) else value


# --------------------------------------------------------------------------
# The block that travels with every value
# --------------------------------------------------------------------------


def source_ref(ref: SourceRef) -> dict[str, Any]:
    """A complete, resolvable citation. Never abbreviated, never omitted."""
    return {
        "source_id": ref.source_id,
        "document": ref.document,
        "locator": ref.locator,
        "retrieved_at": _moment(ref.retrieved_at),
        "content_hash": ref.content_hash,
        "parser_id": ref.parser_id,
        "parser_version": ref.parser_version,
        "confidence": ref.confidence.value,
        "published_at": _day(ref.published_at),
        "original_url": ref.original_url,
        "archive_key": ref.archive_key,
    }


def fact(item: Fact, *, licensing: Licensing = _PERMISSIVE) -> dict[str, Any]:
    """One attributed value, with its window, its layer and its citation.

    The value travels unless it is reproduced prose. A declared distance is
    the analysis and is ours to publish; a passage of a State's text is not.
    """
    permitted = (
        not _is_verbatim(item.value)
        or licensing.may_republish(item.source.source_id)
    )
    return {
        "entity": item.entity,
        "attribute": item.attribute,
        "value": item.value if permitted else _withheld(item.source.source_id, licensing),
        "precedence": item.precedence.name.lower(),
        "valid_from": _day(item.valid_from),
        "valid_to": _day(item.valid_to),
        "recorded_at": _moment(item.recorded_at),
        "superseded_at": _moment(item.superseded_at),
        "source_ref": source_ref(item.source),
    }


# --------------------------------------------------------------------------
# Documents
# --------------------------------------------------------------------------


def _value_line(line: ValueLine, licensing: Licensing) -> dict[str, Any]:
    payload = fact(line.fact, licensing=licensing)
    payload["scope"] = line.scope
    return payload


def _section_entry(entry: SectionEntry, licensing: Licensing) -> dict[str, Any]:
    return {
        "code": entry.section.code,
        "title": entry.section.title,
        "domains": list(entry.section.domains),
        "icao_defined": entry.section.icao_defined,
        "holding": entry.state.value,
        "is_gap": entry.is_gap,
        "detail": entry.detail,
        "values": [_value_line(v, licensing) for v in entry.values],
    }


def dossier(item: AerodromeDossier, *, licensing: Licensing = _PERMISSIVE) -> dict[str, Any]:
    """Consolidated effective state — every AD 2 section, held or not."""
    return {
        "aerodrome": item.aerodrome,
        "as_at": _moment(item.as_at),
        "on": _day(item.on),
        "cycle": item.cycle.identifier if item.cycle else None,
        "complete": item.is_complete,
        "summary": item.summary(),
        "sections": [_section_entry(e, licensing) for e in item.sections],
        "unplaced": [_value_line(v, licensing) for v in item.unplaced],
        "notams": [
            {
                "identifier": n.identifier,
                "state": s.value,
                "entities": list(n.entities),
                "text": (
                    n.text
                    if licensing.may_republish(n.source.source_id)
                    else _withheld(n.source.source_id, licensing)
                ),
                "schedule": n.schedule,
                "effective_start": _moment(n.effective_start),
                "effective_end": _moment(n.effective_end),
                "source_ref": source_ref(n.source),
            }
            for n, s in item.notams
        ],
        "coverage_gaps": [e.section.code for e in item.gaps],
    }


def _reported_change(item: ReportedChange, licensing: Licensing) -> dict[str, Any]:
    change = item.change
    return {
        "entity": item.entity,
        "attribute": item.attribute,
        "kind": change.kind.value,
        "attention": item.attention.label,
        "direction": item.impact.direction.value,
        "assessed": item.impact.assessed,
        "section": item.section.code if item.section else None,
        "domains": list(item.domains),
        "summary": item.impact.summary,
        "consequence": item.impact.consequence,
        "before": fact(change.before, licensing=licensing) if change.before else None,
        "after": fact(change.after, licensing=licensing) if change.after else None,
    }


def bulletin(item: ChangeBulletin, *, licensing: Licensing = _PERMISSIVE) -> dict[str, Any]:
    """The change record between two moments, with its own completeness stated."""
    return {
        "entity": item.entity,
        "before": _day(item.before),
        "after": _day(item.after),
        "before_cycle": item.before_cycle.identifier if item.before_cycle else None,
        "after_cycle": item.after_cycle.identifier if item.after_cycle else None,
        "conclusive": item.is_conclusive,
        "coverage_known": item.coverage_known,
        "coverage_statement": item.coverage_statement(),
        "sections_compared": [s.code for s in item.covered],
        "sections_not_compared": [s.code for s in item.blind],
        "summary": item.summary(),
        "changes": [_reported_change(c, licensing) for c in item.changes],
    }


def _transition(item: Transition, licensing: Licensing) -> dict[str, Any]:
    return {
        "on": _day(item.on),
        "days_away": item.days_away,
        "entity": item.entity,
        "attribute": item.attribute,
        "trigger": item.trigger.value,
        "announced": item.is_announced,
        "section": item.section.code if item.section else None,
        "summary": item.impact.summary,
        "consequence": item.impact.consequence,
        "why": item.why(),
        "before": fact(item.before, licensing=licensing) if item.before else None,
        "after": fact(item.after, licensing=licensing) if item.after else None,
    }


def horizon(item: Horizon, *, licensing: Licensing = _PERMISSIVE) -> dict[str, Any]:
    """What changes next, with the unannounced set called out."""
    return {
        "entity": item.entity,
        "from_date": _day(item.from_date),
        "through": _day(item.through),
        "as_known_at": _moment(item.as_known_at),
        "summary": item.summary(),
        "transitions": [_transition(t, licensing) for t in item.transitions],
        "unannounced": [_transition(t, licensing) for t in item.unannounced],
        "note": (
            "Exact for the publications held when this was taken, and silent "
            "about any issued since."
        ),
    }


def _finding(item: QualityFinding) -> dict[str, Any]:
    return {
        "kind": item.kind.value,
        "entity": item.entity,
        "summary": item.summary,
        "consequence": item.consequence,
        "days": item.days,
        "messages": list(item.messages),
        "source_refs": [source_ref(s) for s in item.sources],
    }


def quality(item: QualityReport) -> dict[str, Any]:
    """Publication conduct — measurements against PANS-AIM, not verdicts."""
    return {
        "scope": item.scope,
        "as_at": _moment(item.as_at),
        "summary": item.summary(),
        "findings": [_finding(f) for f in item.findings],
        "standard": (
            "ICAO PANS-AIM: information expected to persist beyond three months "
            "belongs in an AIP Supplement or Amendment, not a NOTAM."
        ),
    }


def lens_view(item: LensView, *, licensing: Licensing = _PERMISSIVE) -> dict[str, Any]:
    """One audience's document, including whether it is sound for them."""
    return {
        "audience": item.lens.audience.value,
        "title": item.lens.title,
        "reader": item.lens.reader,
        "purpose": item.lens.purpose,
        "entity": item.entity,
        "as_at": _moment(item.as_at),
        "sound": item.is_sound,
        "depends_on": list(item.lens.depends_on),
        "blocking_gaps": [
            {"code": e.section.code, "title": e.section.title, "state": e.state.value}
            for e in item.blocking_gaps
        ],
        "not_yet_connected": list(item.lens.needs_unbuilt),
        "coverage_note": item.coverage_note,
        "summary": item.summary(),
        "changes": [_reported_change(c, licensing) for c in item.changes],
        "ahead": [_transition(t, licensing) for t in item.ahead],
        "conduct": [_finding(f) for f in item.conduct],
    }


def _check(item: Check, licensing: Licensing) -> dict[str, Any]:
    return {
        "name": item.name,
        "assessment": item.assessment.value,
        "conclusive": item.is_known,
        "blocks": item.blocks,
        "scope": item.scope,
        "section": item.section or None,
        "detail": item.detail,
        "aerodrome_basis": [_value_line(v, licensing) for v in item.aerodrome_basis],
        "aircraft_basis": [_characteristic(c) for c in item.aircraft_basis],
    }


def _note(item: Note, licensing: Licensing) -> dict[str, Any]:
    """A note is emitted without an ``assessment`` key, deliberately.

    An integrator reading these into a table must not be able to treat one as
    a verdict by reading a field that happens to be there. The absence of the
    key is the guarantee.
    """
    return {
        "name": item.name,
        "scope": item.scope,
        "section": item.section or None,
        "detail": item.detail,
        "aerodrome_basis": [_value_line(v, licensing) for v in item.aerodrome_basis],
        "aircraft_basis": [_characteristic(c) for c in item.aircraft_basis],
    }


def _characteristic(item: Characteristic) -> dict[str, Any]:
    """One aircraft figure with its citation.

    Operator-supplied figures are not filtered out here — a payload built for
    the tenant that supplied them may carry them. What must not happen is the
    origin being lost, so a caller redistributing a payload can tell which
    values are theirs to pass on. ``redistributable`` is that flag, stated
    rather than left to be inferred from ``origin``.
    """
    return {
        "attribute": item.attribute,
        "value": item.value,
        "unit": item.unit,
        "variant": item.variant,
        "origin": item.origin.value,
        "redistributable": item.origin.is_redistributable,
        "source_ref": source_ref(item.source),
    }


def suitability(
    item: Suitability, *, licensing: Licensing = _PERMISSIVE
) -> dict[str, Any]:
    """One aeroplane against one aerodrome, with everything it could not check.

    ``conclusive`` and ``overtaken`` are first-class fields rather than
    something an integrator has to derive. A consumer that reads only
    ``overall`` would get "suitable" for an assessment computed over a runway
    a NOTAM has closed, and the payload must make that impossible to miss.
    """
    return {
        "aerodrome": item.aerodrome,
        "designator": item.designator,
        "as_at": _moment(item.as_at),
        "overall": item.overall.value,
        "conclusive": item.is_conclusive,
        "checks": [_check(c, licensing) for c in item.checks],
        "notes": [_note(n, licensing) for n in item.notes],
        "unknown": [c.name for c in item.unknown],
        "blocking": [c.name for c in item.blocking],
        "overtaken": [
            {"name": c.name, "scope": c.scope} for c in item.overtaken
        ],
        "notams": [
            {
                "identifier": n.identifier,
                "state": s.value,
                "entities": list(n.entities),
                "source_ref": source_ref(n.source),
            }
            for n, s in item.notams
        ],
        "disclaimer": (
            "A fit assessment against published aerodrome data. Not a "
            "performance calculation and not a dispatch decision."
        ),
    }


def _exposure_finding(item: ExposureFinding, licensing: Licensing) -> dict[str, Any]:
    return {
        "designator": item.designator,
        "exposure": item.exposure.value,
        "needs_action": item.needs_action,
        "reason": item.reason,
        "role": item.role.value,
        "sole_suitable": item.sole_suitable,
        "check": _check(item.check, licensing),
    }


def operator_assessment(
    item: OperatorAssessment, *, licensing: Licensing = _PERMISSIVE
) -> dict[str, Any]:
    """Layer three — exposure for one operator, with layer two attached.

    ``suitability`` carries the operator-agnostic assessment this was derived
    from, unchanged, so an integrator can show both: what is true for everyone
    and what follows for this tenant. Emitting only the tenant view would make
    the shared record unverifiable from the payload that depends on it.

    ``worst_by_type`` is the headline and is emitted rather than left to be
    derived, because deriving it wrongly — by averaging — is the mistake the
    whole layer exists to prevent.
    """
    return {
        "operator": item.operator,
        "aerodrome": item.aerodrome,
        "as_at": _moment(item.as_at),
        "role": item.role.value,
        "sole_suitable": item.sole_suitable,
        "overall": item.overall.value,
        "conclusive": item.is_conclusive,
        "worst_by_type": {k: v.value for k, v in item.worst_by_type().items()},
        "findings": [_exposure_finding(f, licensing) for f in item.findings],
        "actionable": [
            {"designator": f.designator, "check": f.check.name,
             "scope": f.check.scope, "exposure": f.exposure.value}
            for f in item.actionable
        ],
        "unknown": [
            {"designator": f.designator, "check": f.check.name,
             "scope": f.check.scope}
            for f in item.unknown
        ],
        "suitability": [suitability(s, licensing=licensing) for s in item.suitability],
        "note": (
            "Exposure for one operator. The publication record and the generic "
            "assessment beneath it are the same for everyone and are unchanged "
            "by it."
        ),
    }


def _currency(item: DataCurrency | None) -> dict[str, Any] | None:
    if item is None:
        return None
    return {
        "state": item.state.value,
        "usable": item.is_usable,
        "cycles_behind": item.cycles_behind,
        "spread_cycles": item.spread_cycles,
        "newest": _moment(item.newest),
        "oldest": _moment(item.oldest),
        "facts": item.facts,
    }


def _group_redundancy(item: GroupRedundancy) -> dict[str, Any]:
    """A region's own health, which no member aerodrome carries.

    ``remaining`` counts only members that are read, current and clear. A stale
    clear verdict is excluded deliberately: counting it would make a region look
    healthier the longer nobody looked at it.
    """
    thinning = item.thins_on
    return {
        "name": item.name,
        "exposure": item.exposure.value,
        "members": [e.aerodrome for e in item.members],
        "remaining": item.remaining,
        "dependable": [e.aerodrome for e in item.dependable],
        "degraded": [e.aerodrome for e in item.degraded],
        "unreliable": [e.aerodrome for e in item.unreliable],
        "single_threaded": item.is_single_threaded,
        "exhausted": item.is_exhausted,
        "thins_on": _day(thinning),
        "remaining_after": item.remaining_on(thinning) if thinning else None,
    }


def _aerodrome_exposure(
    item: AerodromeExposure, licensing: Licensing
) -> dict[str, Any]:
    return {
        "aerodrome": item.aerodrome,
        "role": item.role.value,
        "sole_suitable": item.sole_suitable,
        "exposure": item.exposure.value,
        "worst_ahead": item.worst_ahead.value,
        "worsens_on": _day(item.worsens_on),
        "deteriorates_unannounced": item.deteriorates_unannounced,
        # Emitted rather than derivable from the exposure alone. A consumer
        # reading only `exposure` would paint an aerodrome nobody has read the
        # same colour as one that was read and came back clear.
        "covered": item.is_covered,
        "current": item.is_current,
        "dependable": item.is_dependable,
        "facts_held": item.facts_held,
        "currency": _currency(item.currency),
        "groups": list(item.groups),
        "changes_ahead": [_transition(t, licensing) for t in item.changes_ahead],
        "unannounced_ahead": [
            _transition(t, licensing) for t in item.unannounced_ahead
        ],
        "assessment": operator_assessment(item.assessment, licensing=licensing),
    }


def network_sweep(
    item: NetworkSweep, *, licensing: Licensing = _PERMISSIVE
) -> dict[str, Any]:
    """Every aerodrome an operator uses, ranked, with coverage stated.

    ``summary`` carries ``covered`` and ``uncovered`` beside every severity
    count, deliberately, so no integrator can render a single percentage
    without the coverage figure being right there. An aerodrome nobody has read
    appears in ``uncovered`` and in no severity bucket at all.
    """
    return {
        "operator": item.operator,
        "as_at": _moment(item.as_at),
        "on": _day(item.on),
        "days_ahead": item.days_ahead,
        "overall": item.overall.value,
        "conclusive": item.is_conclusive,
        "summary": item.summary(),
        "aerodromes": [_aerodrome_exposure(e, licensing) for e in item.ranked],
        "uncovered": [e.aerodrome for e in item.uncovered],
        "stale": [e.aerodrome for e in item.stale],
        "groups": [_group_redundancy(g) for g in item.groups],
        "groups_at_risk": [_group_redundancy(g) for g in item.at_risk_groups],
        "deteriorating": [
            {
                "aerodrome": e.aerodrome,
                "on": _day(e.worsens_on),
                "from": e.exposure.value,
                "to": e.worst_ahead.value,
                "unannounced": e.deteriorates_unannounced,
            }
            for e in item.deteriorating
        ],
        "note": (
            "Aerodromes with nothing held are counted in `uncovered` and in no "
            "severity bucket. They are not clear; nothing has been checked at "
            "them. `clear` counts a verdict of no exposure however old the "
            "reading behind it is; `dependable` counts only those read recently "
            "enough to stand on. A group's exposure is not the worst of its "
            "members and is not covered by them."
        ),
    }


def _late_arrival(item: LateArrival) -> dict[str, Any]:
    return {
        "entity": item.fact.entity,
        "attribute": item.fact.attribute,
        "value": item.fact.value,
        "effective_from": _day(item.effective_from),
        "known_from": _moment(item.known_from),
        "blind_hours": item.blind_hours,
        "predates_watching": item.predates_watching,
        "source_ref": source_ref(item.fact.source),
    }


def _revision(item: Revision, licensing: Licensing) -> dict[str, Any]:
    return {
        "entity": item.entity,
        "attribute": item.attribute,
        "changed": item.changed,
        "appeared": item.appeared,
        "withdrawn": item.withdrawn,
        "restated": item.restated,
        "then": fact(item.then, licensing=licensing) if item.then else None,
        "now": fact(item.now, licensing=licensing) if item.now else None,
    }


def _blindness(item: Blindness) -> dict[str, Any]:
    return {
        "summary": item.summary(),
        "late": [_late_arrival(a) for a in item.late],
    }


def retrospect_document(
    item: Retrospect, *, licensing: Licensing = _PERMISSIVE
) -> dict[str, Any]:
    """What was knowable at a moment, beside what is held now.

    Both readings travel, never one. A payload carrying only ``now`` would be
    the corrected record wearing a past date, which is the confusion this whole
    document exists to prevent — so every revision emits ``then`` and ``now``
    side by side, each with its own citation.

    ``notam_is_retrospective`` is emitted even though it is always false today.
    A consumer must be able to see that the NOTAM picture is current rather
    than contemporaneous, and a field that is absent teaches nobody anything.
    """
    return {
        "entity": item.entity,
        "on": _day(item.on),
        "as_known_at": _moment(item.as_known_at),
        "faithful": item.is_faithful,
        "summary": item.summary(),
        "revisions": [_revision(r, licensing) for r in item.revisions],
        "changed": [
            {"entity": r.entity, "attribute": r.attribute}
            for r in item.changed
        ],
        "blindness": _blindness(item.blindness),
        "notam_is_retrospective": item.notam_is_retrospective,
        "note": (
            "`then` is what could have been printed at as_known_at; `now` is "
            "today's corrected record for the same day. They are different "
            "answers and both are given. NOTAM are not filtered to past "
            "knowledge - the register records effectivity, not when we learned."
        ),
    }


def _obstacle(
    item: Obstacle, *, runway_bearing_deg: float | None = None
) -> dict[str, Any]:
    from aeropub.obstacles import penetrates_ois, required_gradient

    resolved = item.position(runway_bearing_deg)
    return {
        "identifier": item.identifier,
        "kind": item.kind or None,
        "height_above_der_m": item.height_above_der_m,
        "height_ft": item.height_ft,
        "distance_from_der_m": item.distance_from_der_m,
        "distance_nm": item.distance_nm,
        "bearing_from_der_deg": item.bearing_from_der_deg,
        "lighted": item.lighted,
        "marked": item.marked,
        "valid_from": _day(item.valid_from),
        "valid_to": _day(item.valid_to),
        "temporary": item.is_temporary,
        "measurable": item.is_measurable,
        "along_track_m": resolved.along_track_m if resolved else None,
        "lateral_offset_m": resolved.offset_m if resolved else None,
        "required_gradient_percent": required_gradient(
            item, runway_bearing_deg=runway_bearing_deg
        ),
        "penetration": penetrates_ois(
            item, runway_bearing_deg=runway_bearing_deg
        ).value,
        "source_ref": source_ref(item.source),
    }


def _obstacle_change(
    item: ObstacleChange, *, runway_bearing_deg: float | None = None
) -> dict[str, Any]:
    return {
        "identifier": item.identifier,
        "changed": item.changed,
        "appeared": item.appeared,
        "removed": item.removed,
        "raised": item.raised,
        "extended": item.extended,
        "before": _obstacle(item.before, runway_bearing_deg=runway_bearing_deg)
        if item.before
        else None,
        "after": _obstacle(item.after, runway_bearing_deg=runway_bearing_deg)
        if item.after
        else None,
    }


def obstacle_review(
    item: ObstacleReview, *, licensing: Licensing = _PERMISSIVE
) -> dict[str, Any]:
    """Obstacles for one runway end, and the gradient they require.

    ``sector_membership`` is emitted as an explicit refusal rather than left
    out. A consumer must be able to see that whether an obstacle lies inside
    the protected departure area has not been decided — an absent field reads
    as an answer of "no".
    """
    from aeropub.obstacles import OIS_PERCENT, STANDARD_PDG_PERCENT

    exposed = item.exposure()
    bearing = item.runway_bearing_deg
    return {
        "runway": item.runway,
        "runway_bearing_deg": bearing,
        "required_gradient_percent": item.required_percent,
        "standard_gradient_percent": STANDARD_PDG_PERCENT,
        "exceeds_standard": item.exceeds_standard,
        "ois_percent": OIS_PERCENT,
        "governing": _obstacle(item.governing, runway_bearing_deg=bearing)
        if item.governing
        else None,
        "obstacles": [
            {
                **_obstacle(o, runway_bearing_deg=bearing),
                "in_departure_area": item.contains(o),
            }
            for o in item.obstacles
        ],
        "penetrating": [o.identifier for o in item.penetrating],
        "unmeasured": [o.identifier for o in item.unmeasured],
        "inside_area": [o.identifier for o in item.inside_area],
        "outside_area": [o.identifier for o in item.outside_area],
        "changes": [
            _obstacle_change(c, runway_bearing_deg=bearing)
            for c in item.changes
            if c.changed
        ],
        "fleet_exposure": (
            {
                "required_percent": exposed.required_percent,
                "capable": list(exposed.capable),
                "incapable": list(exposed.incapable),
                "unassessed": list(exposed.unassessed),
                "conclusive": exposed.is_conclusive,
            }
            if exposed is not None
            else None
        ),
        "departure_area": (
            {
                "name": item.area.name,
                "half_width_at_der_m": item.area.half_width_at_der_m,
                "splay_percent": item.area.splay_percent,
                "splay_degrees": item.area.splay_degrees,
                "max_half_width_m": item.area.max_half_width_m,
                "note": item.area.note,
            }
            if item.area is not None
            else None
        ),
        "area_note": (
            "Membership is computed against the named area above. Two "
            "published conventions share the number 15 and mean different "
            "things - PANS-OPS splays 15 per cent each side, the Annex 14 "
            "instrument departure surface 15 degrees - so which was used is "
            "always stated. A State may publish a non-standard area for a "
            "specific procedure, and where it has, that one governs."
            if item.area is not None
            else "No protected area was given, so every measurable obstacle is "
            "counted. That is the conservative reading, not a statement that "
            "all of them lie in the departure area."
        ),
        "eosid": {
            "computed": False,
            "reason": (
                "The engine-out net flight path depends on the aeroplane's net "
                "performance and the operator's approved data, and designing an "
                "escape path is certified engineering. The numbers that review "
                "needs are in this document."
            ),
        },
    }


def _leg(item: LegAssessment, licensing: Licensing) -> dict[str, Any]:
    return {
        "aerodrome": item.aerodrome,
        "role": item.role.value,
        "exposure": item.exposure.value,
        "needs_action": item.needs_action,
        "changes_before_departure": [
            _transition(t, licensing) for t in item.changes_before
        ],
        "unannounced_before_departure": [
            _transition(t, licensing) for t in item.unannounced_before
        ],
        "missing_sections": [
            {"section": code, "consequence": meaning}
            for code, meaning in item.missing_sections
        ],
        "assessment": operator_assessment(item.assessment, licensing=licensing),
    }


def trip_assessment(
    item: TripAssessment, *, licensing: Licensing = _PERMISSIVE
) -> dict[str, Any]:
    """One flight, assessed for its own date.

    ``on`` and ``as_at`` are both emitted and they are different questions: the
    first is the day whose effective state was resolved, the second is when the
    assessment was taken. A consumer showing only the second would present a
    three-week-old answer about a future date as current.

    ``missing_sections`` carries the consequence beside the code, because
    "AD 2.3" tells an integrator nothing and "whether it is open when you
    arrive" tells them what to put on the screen.
    """
    return {
        "reference": item.trip.reference,
        "operator": item.trip.operator or None,
        "designator": item.trip.aircraft.designator,
        "on": _day(item.trip.on),
        "as_at": _moment(item.as_at),
        "days_away": item.trip.days_away(item.as_at.date()),
        "expired": item.expired,
        "departure": item.trip.departure,
        "destination": item.trip.destination,
        "alternates": list(item.trip.alternates),
        "sole_alternate": item.trip.sole_alternate,
        "overall": item.overall.value,
        "conclusive": item.is_conclusive,
        "legs": [_leg(leg, licensing) for leg in item.legs],
        "blocking": [leg.aerodrome for leg in item.blocking],
        "changing_before_departure": [
            leg.aerodrome for leg in item.changing_before_departure
        ],
        "note": (
            "A fit and exposure assessment for the day of the flight. Not a "
            "performance calculation, not a dispatch release, and not a "
            "substitute for the published AIP."
        ),
    }


def _release(item: Release, licensing: Licensing) -> dict[str, Any]:
    return {
        "fingerprint": item.mark,
        "disposition": item.disposition.value,
        "released": item.is_released,
        "needs_a_person": item.needs_a_person,
        "reason": item.reason,
        "at": _moment(item.at),
        "attestation": (
            {
                "by": item.attestation.by,
                "at": _moment(item.attestation.at),
                "finding": item.attestation.finding,
                "released": item.attestation.released,
                "note": item.attestation.note or None,
            }
            if item.attestation is not None
            else None
        ),
        "finding": _exposure_finding(item.finding, licensing),
    }


def gate_log(item: GateLog, *, licensing: Licensing = _PERMISSIVE) -> dict[str, Any]:
    """Every decision the gate made, and who is accountable for each.

    ``fingerprint`` travels on every release, because that is what an
    attestation binds to. Without it a consumer cannot tell whether a signature
    still covers the wording in front of them, which is the one thing the gate
    exists to make provable.
    """
    return {
        "tenant": item.gate.tenant,
        "gate": {
            "auto_publish_at_or_below": item.gate.auto_publish_at_or_below.value,
            "sample_rate": item.gate.sample_rate,
            "is_default": item.gate.is_default,
            "is_widened": item.gate.is_widened,
        },
        "summary": item.summary(),
        "auto_published_share": item.auto_published_share,
        "releases": [_release(r, licensing) for r in item.releases],
        "held": [r.mark for r in item.held],
        "withheld": [r.mark for r in item.withheld],
        "note": (
            "The data plane is autonomous; this gate sits only where a verdict "
            "reaches an operational consumer. An unmade check never releases "
            "unattended at any threshold. Audit sampling is drawn from the "
            "fingerprint, so it reproduces from the finding alone."
        ),
    }


def operator_fleet(
    item: OperatorFleet, *, licensing: Licensing = _PERMISSIVE
) -> dict[str, Any]:
    """What the library holds for one operator, gaps included.

    ``complete`` is a first-class field because the omission it guards against
    is invisible: a fleet payload listing three types reads identically whether
    the operator flies three or whether two more had no figures behind them.
    """
    record = item.operator
    return {
        "operator": {
            "icao": record.icao,
            "iata": record.iata,
            "name": record.name,
            "segment": record.segment.value,
            "bases": list(record.bases),
            "fleet_size": record.fleet_size,
        },
        "complete": item.is_complete,
        "types": [
            {
                "designator": aircraft.designator,
                "manufacturer": aircraft.manufacturer,
                "model": aircraft.model,
                "code_letter": aircraft.code_letter(),
                "characteristics": [
                    _characteristic(c) for c in aircraft.characteristics
                ],
            }
            for aircraft in item.fleet
        ],
        "gaps": [
            {
                "designator": gap.designator,
                "coverage": gap.coverage.value,
                "actionable": gap.is_actionable,
                "marks": list(gap.marks),
                "references": [
                    {
                        "publisher": reference.publisher,
                        "document": reference.document,
                        "revision": reference.revision,
                        "locator": reference.locator,
                        "url": reference.url,
                    }
                    for reference in gap.references
                ],
            }
            for gap in item.gaps
        ],
        "unidentified": list(item.unidentified),
        "note": (
            "A bibliography entry is not provenance. A type listed under gaps "
            "with references names the document that holds its figures; nobody "
            "has read it, and no value here rests on it."
        ),
    }


def fleet_screen(
    item: FleetScreen, *, licensing: Licensing = _PERMISSIVE
) -> dict[str, Any]:
    """Which of an operator's types can use one aerodrome.

    ``unchecked`` deliberately merges types that reached no conclusion with
    types the library could not describe. To a planner the two are one answer
    — not yet — and splitting them in the payload invites a consumer to count
    only the first.
    """
    return {
        "aerodrome": item.aerodrome,
        "operator": {
            "icao": item.operator.icao,
            "name": item.operator.name,
            "segment": item.operator.segment.value,
        },
        "complete": item.is_complete,
        "suitable": list(item.suitable),
        "restricted": list(item.restricted),
        "not_suitable": list(item.not_suitable),
        "unchecked": list(item.unchecked),
        "screened": [
            {
                "designator": entry.designator,
                "marks": list(entry.marks),
                "assessment": entry.assessment.value,
                "conclusive": entry.is_conclusive,
                "suitability": suitability(entry.suitability, licensing=licensing),
            }
            for entry in item.screened
        ],
        "not_screened": [
            {
                "designator": gap.designator,
                "coverage": gap.coverage.value,
                "marks": list(gap.marks),
            }
            for gap in item.gaps
        ],
        "unidentified": list(item.unidentified),
        "disclaimer": (
            "A fit assessment against published aerodrome data, run across a "
            "fleet. Not a performance calculation and not a dispatch decision."
        ),
    }


def runtime_report(
    item: RuntimeReport, *, licensing: Licensing = _PERMISSIVE
) -> dict[str, Any]:
    """One supervised tick — including everything it decided not to do.

    ``restrained`` is not an operational detail that belongs in a log. A
    consumer reading ``checked`` and ``changed`` alone would see a healthy tick
    on a platform that was asking forty sources out of a hundred, and the
    payload has to make that impossible. Same for ``gap``: a period nobody
    watched is a fact about coverage, not about uptime.
    """
    return {
        "at": _moment(item.at),
        "alive": item.alive,
        "quiet": item.quiet,
        "due": item.due,
        "asked": item.asked,
        "checked": list(item.tick.checked),
        "changed": list(item.tick.changed),
        "failed": list(item.tick.failed),
        "skipped": list(item.tick.skipped),
        "overdue": list(item.tick.overdue),
        "restrained": [
            {
                "source_id": held.source_id,
                "reason": held.reason,
                "until": _moment(held.until) if held.until else None,
                "detail": held.detail,
            }
            for held in item.restrained
        ],
        "gap": (
            {
                "began": _moment(item.gap.began),
                "ended": _moment(item.gap.ended),
                "seconds": int(item.gap.duration.total_seconds()),
            }
            if item.gap is not None
            else None
        ),
        "breakers": {
            source_id: {
                "state": breaker.state.value,
                "failures": breaker.failures,
                "blocked": breaker.was_blocked,
                "error": breaker.last_error,
                "ready_at": (
                    _moment(breaker.ready_at()) if breaker.ready_at() else None
                ),
            }
            for source_id, breaker in item.breakers.items()
        },
        "note": (
            "A source that was held back is not a source that is fine. Read "
            "restrained and gap before concluding anything from checked."
        ),
    }


def _minimum(item: Minimum) -> dict[str, Any]:
    return {
        "category": item.category,
        "line": item.line,
        "da_ft": item.da_ft,
        "mda_ft": item.mda_ft,
        "rvr_m": item.rvr_m,
        "vis_m": item.vis_m,
        "source_ref": source_ref(item.source),
    }


def chart(item: Chart, *, licensing: Licensing = _PERMISSIVE) -> dict[str, Any]:
    """One published chart as the State's index describes it.

    ``transcribed`` is a first-class field. Without it an empty ``minima``
    array is ambiguous between a chart nobody has read and a chart with no
    minima, and those are opposite answers.
    """
    return {
        "aerodrome": item.aerodrome,
        "kind": item.kind.value,
        "identifier": item.identifier,
        "label": item.label,
        "revision": item.revision,
        "cycle": item.cycle,
        "runways": list(item.runways),
        "amended": item.amended,
        "transcribed": item.is_transcribed,
        "minima": [_minimum(m) for m in item.minima],
        "requirements": [
            {
                "code": r.code,
                "detail": r.detail,
                "source_ref": source_ref(r.source),
            }
            for r in item.requirements
        ],
        "note": item.note,
        "source_ref": source_ref(item.source),
    }


def chart_review(
    item: ChartReview, *, licensing: Licensing = _PERMISSIVE
) -> dict[str, Any]:
    """A chart set reconciled against the AIP changes that should drive it.

    Both directions are carried, and ``unmapped`` alongside them. A consumer
    reading ``discrepancies`` alone would see a clean reconciliation on a
    review that never decided what half the changes implied — which is why
    ``conclusive`` sits at the top rather than being derivable.
    """
    transcribed, registered = item.register.coverage()
    return {
        "aerodrome": item.aerodrome,
        "on": _day(item.on),
        "from_cycle": item.from_cycle,
        "to_cycle": item.to_cycle,
        "conclusive": item.is_conclusive,
        "registered": registered,
        "transcribed": transcribed,
        "amended": [c.identifier for c in item.register.amended],
        "expected": [
            {
                "chart": e.chart.identifier,
                "kind": e.chart.kind.value,
                "entity": e.entity,
                "attribute": e.attribute,
                "change": e.change.describe(),
            }
            for e in item.expected
        ],
        "discrepancies": [
            {
                "chart": d.chart.identifier,
                "kind": d.chart.kind.value,
                "revision": d.chart.revision,
                "entity": d.change.entity,
                "attribute": d.change.attribute,
                "detail": d.describe(),
            }
            for d in item.discrepancies
        ],
        "unexplained": [
            {
                "chart": u.chart.identifier,
                "kind": u.chart.kind.value,
                "revision": u.chart.revision,
                "detail": u.describe(),
            }
            for u in item.unexplained
        ],
        "unmapped": [
            {"entity": c.entity, "attribute": c.attribute, "change": c.describe()}
            for c in item.unmapped
        ],
        "disclaimer": (
            "A reconciliation between two published things, not a reading of "
            "the chart image. A discrepancy says the AIP and the chart index "
            "disagree; it does not say which of them is wrong."
        ),
    }


def route_dossier(
    item: RouteDossier, *, licensing: Licensing = _PERMISSIVE
) -> dict[str, Any]:
    """One sector, assembled from everything held about it.

    ``spoken_for`` and ``places`` are the headline and they sit at the top for
    the reason the document puts them there: a payload whose sections are
    simply absent where nothing is held reads identically to one for a route
    with nothing wrong. ``not_addressed`` is carried for the same reason —
    what the platform did not look at is part of the answer.
    """
    read, total = item.coverage
    return {
        "route": {
            "reference": item.route.reference,
            "label": item.route.label,
            "departure": item.route.departure,
            "destination": item.route.destination,
            "alternates": list(item.route.alternates),
            "takeoff_alternate": item.route.takeoff_alternate,
            "enroute_alternates": list(item.route.enroute_alternates),
            "designator": item.route.designator,
            "crosses": [
                {
                    "designator": j.designator,
                    "name": j.name,
                    "publisher": j.publisher,
                    "entity": j.key,
                }
                for j in item.route.crosses
            ],
        },
        "as_at": _moment(item.as_at),
        "on": _day(item.on),
        "spoken_for": read,
        "places": total,
        "conclusive": item.is_conclusive,
        "overall": item.overall.value,
        "sweep": network_sweep(item.sweep, licensing=licensing),
        "jurisdictions": [
            {
                "designator": j.jurisdiction.designator,
                "publisher": j.jurisdiction.publisher,
                "covered": j.is_covered,
                "current": j.is_current,
                "facts_held": j.facts_held,
                "transition_altitude_ft": j.transition_altitude_ft,
                "transition_level": j.transition_level,
            }
            for j in item.jurisdictions
        ],
        "altimetry": {
            "complete": item.altimetry.is_complete,
            "changes": [
                {
                    "leaving": b.leaving.designator,
                    "entering": b.entering.designator,
                    "from_ft": b.from_ft,
                    "to_ft": b.to_ft,
                    "delta_ft": b.delta_ft,
                }
                for b in item.altimetry.changes
            ],
            "unknown": [
                {"leaving": b.leaving.designator, "entering": b.entering.designator}
                for b in item.altimetry.unknown
            ],
        },
        "open_items": [
            {
                "where": i.where,
                "what": i.what,
                "severity": i.severity.value,
                "why": i.why,
            }
            for i in item.open_items
        ],
        "filed_route": (
            {
                "text": item.expansion.route.text,
                "parsed": item.expansion.route.is_parsed,
                "unparsed": list(item.expansion.route.unparsed),
                "resolved": item.expansion.resolved,
                "checkable": item.expansion.checkable,
                "direct_legs": len(item.expansion.direct),
                "highest_mea_ft": item.expansion.highest_mea_ft,
                "distance_nm": item.expansion.distance_nm,
                "airway_distance_nm": item.expansion.airway_distance_nm,
                "navigation_specs": list(item.expansion.navigation_specs),
                "legs": [
                    {
                        "start": leg.leg.start,
                        "via": leg.leg.via,
                        "end": leg.leg.end,
                        "resolution": leg.resolution.value,
                        "reason": leg.reason,
                        "segments": [
                            {
                                "route": s.route,
                                "start": s.start,
                                "end": s.end,
                                "mea_ft": s.mea_ft,
                                "moca_ft": s.moca_ft,
                                "maa_ft": s.maa_ft,
                                "direction": s.direction.value,
                                "navigation_spec": s.navigation_spec,
                                "distance_nm": s.distance_nm,
                                "source_ref": source_ref(s.source),
                            }
                            for s in leg.segments
                        ],
                    }
                    for leg in item.expansion.legs
                ],
            }
            if item.expansion is not None
            else None
        ),
        "levels": [
            {
                "route": f.segment.route,
                "start": f.segment.start,
                "end": f.segment.end,
                "planned_ft": f.planned_ft,
                "reason": f.reason,
                "blocking": f.blocking,
            }
            for f in item.levels
        ],
        "enroute_notams": [
            {
                "entity": entity,
                "identifier": notam.identifier,
                "state": state.value,
                "source_ref": source_ref(notam.source),
            }
            for entity, notam, state in item.enroute_notams
        ],
        "profile": {
            "departures": [
                {
                    "designator": link.procedure.designator,
                    "kind": link.procedure.kind.value,
                    "point": link.point,
                    "runways": list(link.procedure.runways),
                    "source_ref": source_ref(link.procedure.source),
                }
                for link in item.departures
            ],
            "arrivals": [
                {
                    "designator": link.procedure.designator,
                    "kind": link.procedure.kind.value,
                    "point": link.point,
                    "runways": list(link.procedure.runways),
                    "source_ref": source_ref(link.procedure.source),
                }
                for link in item.arrivals
            ],
        },
        "energy": [
            {
                "procedure": trap.procedure,
                "start": trap.start,
                "end": trap.end,
                "from_ft": trap.from_ft,
                "to_ft": trap.to_ft,
                "distance_nm": trap.distance_nm,
                "required_ft_per_nm": trap.required_ft_per_nm,
                "required_percent": trap.required_percent,
                "capability_ft_per_nm": trap.capability_ft_per_nm,
                "descending": trap.descending,
                "short_by_ft_per_nm": trap.exceeds_by,
            }
            for trap in item.traps
        ],
        "not_addressed": list(item.not_addressed),
        "disclaimer": (
            "An assembly of what is held about one sector. Not a flight plan, "
            "not a performance calculation, and silent on everything listed "
            "under not_addressed."
        ),
    }


_SERIALISERS: tuple[tuple[type, str, Any], ...] = (
    (AerodromeDossier, "aerodrome_dossier", dossier),
    (ChangeBulletin, "change_bulletin", bulletin),
    (Horizon, "forward_view", horizon),
    (QualityReport, "publication_conduct", quality),
    (LensView, "lens_view", lens_view),
    (Suitability, "aerodrome_suitability", suitability),
    (OperatorAssessment, "operator_exposure", operator_assessment),
    (NetworkSweep, "network_sweep", network_sweep),
    (Retrospect, "retrospect", retrospect_document),
    (ObstacleReview, "obstacle_review", obstacle_review),
    (TripAssessment, "trip_assessment", trip_assessment),
    (GateLog, "gate_log", gate_log),
    (OperatorFleet, "operator_fleet", operator_fleet),
    (FleetScreen, "fleet_screen", fleet_screen),
    (RuntimeReport, "runtime_tick", runtime_report),
    (ChartReview, "chart_review", chart_review),
    (Chart, "chart", chart),
    (RouteDossier, "route_dossier", route_dossier),
    (Fact, "fact", fact),
)


def to_json(obj: Any, *, licensing: Licensing = _PERMISSIVE) -> dict[str, Any]:
    """Serialise any AeroPub document. Raises on anything unrecognised.

    Refusing is deliberate. Falling back to ``dataclasses.asdict`` would emit
    something shaped like an API response with none of the guarantees this
    module exists to keep — provenance in every value, licence honoured, times
    unambiguous.
    """
    for kind, _, serialiser in _SERIALISERS:
        if isinstance(obj, kind):
            try:
                return serialiser(obj, licensing=licensing)
            except TypeError:
                return serialiser(obj)
    raise _unrepresentable(obj)


def _unrepresentable(obj: Any) -> TypeError:
    """One refusal message, wherever the fault is met.

    Two spellings of it meant a caller who went through ``document`` got the
    terse one and a caller who went through ``to_json`` got the useful one,
    for the same mistake.
    """
    return TypeError(
        f"{type(obj).__name__} has no API representation. Add one here rather "
        "than serialising it generically: a payload without provenance in every "
        "value is indistinguishable from one somebody typed."
    )


def _kind_of(obj: Any) -> str:
    for kind, name, _ in _SERIALISERS:
        if isinstance(obj, kind):
            return name
    raise _unrepresentable(obj)


def document(
    obj: Any,
    *,
    licensing: Licensing = _PERMISSIVE,
    generated_at: datetime | None = None,
    request: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Wrap a payload in the versioned envelope every response carries."""
    moment = generated_at or datetime.now(timezone.utc)
    return {
        "aeropub": {
            "version": API_VERSION,
            "kind": _kind_of(obj),
            "generated_at": _moment(moment),
            "request": dict(request) if request else {},
        },
        "data": to_json(obj, licensing=licensing),
    }


def dumps(obj: Any, *, indent: int | None = None, **kwargs: Any) -> str:
    """Serialise to a JSON string.

    Keys are sorted, so two identical documents produce identical bytes and a
    diff between cycles shows what changed rather than how a dict was ordered.
    """
    return json.dumps(document(obj, **kwargs), indent=indent, sort_keys=True)


def ndjson(items: Iterable[Any], **kwargs: Any) -> Iterator[str]:
    """One document per line, for a whole-cycle pull without pagination."""
    for item in items:
        yield json.dumps(document(item, **kwargs), sort_keys=True)

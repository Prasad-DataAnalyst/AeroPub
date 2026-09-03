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

from aeropub.bulletin import ChangeBulletin, ReportedChange
from aeropub.dossier import AerodromeDossier, SectionEntry, ValueLine
from aeropub.facts import Fact
from aeropub.horizon import Horizon, Transition
from aeropub.lenses import LensView
from aeropub.provenance import SourceRef
from aeropub.quality import QualityFinding, QualityReport
from aeropub.registry import Redistribution

__all__ = [
    "API_VERSION",
    "Licensing",
    "VERBATIM_THRESHOLD",
    "document",
    "dumps",
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


_SERIALISERS: tuple[tuple[type, str, Any], ...] = (
    (AerodromeDossier, "aerodrome_dossier", dossier),
    (ChangeBulletin, "change_bulletin", bulletin),
    (Horizon, "forward_view", horizon),
    (QualityReport, "publication_conduct", quality),
    (LensView, "lens_view", lens_view),
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

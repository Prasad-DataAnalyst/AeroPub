"""Facts read this cycle, into the store that keeps them.

:mod:`aeropub.cycle` moves documents. This is what turns a document that was
read into values the application holds, and it is short because the store
already enforces most of what matters — append-only, bitemporal, a citation on
every row.

What it has to get right is what happens to *last* cycle's reading of the same
document. Appending alone leaves two live values for one key with no way to
tell which is current. Superseding too widely is worse, and is the reason this
module exists rather than a two-line call at the end of the cycle.

The narrow supersede
--------------------
An AIP section is re-read and yields a new value for OTHH's runway length. The
previous value from *that section* is no longer current and must be closed. A
NOTAM covering the same attribute is a different matter entirely: it sits above
the AIP in :class:`~aeropub.facts.Precedence` precisely so it overrides it, and
closing it because the layer beneath was refetched would take a restriction
that is still in force off an operator's screen. So the supersede is scoped to
the document, never to the key.

Nothing is recorded from a citation that will not resolve
---------------------------------------------------------
A document read but not archived produces facts at ``LOW`` confidence, because
its citation stops resolving as soon as the State withdraws the edition. Those
facts are still recorded — refusing them would deny a State's data over a fault
of ours — but :attr:`Recorded.unresolvable` counts them, so a store that has
quietly filled with values nobody can verify is visible rather than inferred.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

from .facts import Fact
from .provenance import Confidence, SourceRef
from .reader import ReadResult

__all__ = [
    "Recorded",
    "FactSink",
    "record_result",
    "record_cycle",
    "supplements_from",
    "write_supplement_manifest",
]


class FactSink(Protocol):
    """The part of a fact store this needs. Injected, so a test needs no disk."""

    def extend(self, facts) -> None:
        ...

    def supersede_document(self, document: str, at: datetime) -> int:
        ...


@dataclass(frozen=True, slots=True)
class Recorded:
    """What one recording pass wrote."""

    documents: int = 0
    facts: int = 0
    superseded: int = 0
    unresolvable: int = 0
    """Facts whose citation stops resolving when the State moves on.

    Written, and counted. A store filling with values nobody can verify is a
    condition to see, not one to infer from a confidence column nobody reads.
    """

    skipped_unparsed: int = 0
    """Documents that produced no facts because nothing parsed them.

    Never confused with a document that parsed and found nothing: one is a
    parser we have not written, the other is a page with nothing in it.
    """

    def describe(self) -> str:
        lines = [
            f"{self.facts} facts from {self.documents} documents"
            f"  ·  {self.superseded} superseded"
        ]
        if self.unresolvable:
            lines.append(
                f"  {self.unresolvable} rest on a citation that will not "
                "resolve once the State withdraws the edition"
            )
        if self.skipped_unparsed:
            lines.append(
                f"  {self.skipped_unparsed} documents had no parser, so nothing "
                "was read from them"
            )
        return "\n".join(lines)


def record_result(
    sink: FactSink, result: ReadResult, *, at: datetime | None = None
) -> Recorded:
    """Write one document's facts, closing that document's previous reading.

    A document that was not parsed writes nothing and supersedes nothing —
    an absent parser is not evidence that the previous values are wrong.
    """
    moment = at or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        raise ValueError("record_result(at=) must be timezone-aware (UTC)")

    if not result.parsed:
        return Recorded(documents=1, skipped_unparsed=1)

    document = result.link.document
    # Closed before the new rows go in, so no window exists where both the
    # old and the new reading are current.
    closed = sink.supersede_document(document, moment)

    facts = tuple(result.facts)
    if facts:
        sink.extend(facts)

    return Recorded(
        documents=1,
        facts=len(facts),
        superseded=closed,
        unresolvable=(
            len(facts)
            if result.confidence is Confidence.LOW or result.citation_will_expire
            else 0
        ),
    )


def record_cycle(sink: FactSink, report, *, at: datetime | None = None) -> Recorded:
    """Write every document read this cycle.

    Only documents that were *read*. An unchanged document's facts are already
    in the store and rewriting them would replace a row recorded when the
    value actually arrived with one recorded today — losing exactly the date
    an investigation asks for.
    """
    moment = at or datetime.now(timezone.utc)
    total = Recorded()
    for state in report.states:
        for outcome in state.read:
            if outcome.result is None:
                continue
            one = record_result(sink, outcome.result, at=moment)
            total = Recorded(
                documents=total.documents + one.documents,
                facts=total.facts + one.facts,
                superseded=total.superseded + one.superseded,
                unresolvable=total.unresolvable + one.unresolvable,
                skipped_unparsed=total.skipped_unparsed + one.skipped_unparsed,
            )
    return total


def supplements_from(
    report, *, state: str, parser_version: str = "discovery-1"
) -> tuple:
    """Every supplement this cycle knows about, as register records.

    The cycle discovers a supplement as a document: it has an identifier, a
    URL and a hash, and nothing has read a word of it. That is a real thing to
    know and the register is where it belongs — a supplement nobody has read
    is still a supplement that exists, and an operator asking "what
    supplements are in force at OTHH" should be told about it rather than have
    it silently omitted until a parser is written.

    What it must not do is claim a window. :attr:`ForcePeriod.UNDATED` exists
    for precisely this, and :attr:`ForcePeriod.applies` returns ``None`` for
    it — not a soft no. A supplement whose dates nobody has transcribed is one
    nobody can say has ended, and quietly retiring it would take a restriction
    that is still in force off an operator's screen on a day nothing happened.
    """
    from .publication import Kind
    from .supplement import Supplement

    found: list[Supplement] = []
    seen: set[str] = set()
    for state_outcome in report.states:
        for document in state_outcome.documents:
            result = document.result
            if result is None:
                continue
            if result.publication.kind is not Kind.SUPPLEMENT:
                continue
            identifier = result.publication.code or result.link.document
            if not identifier.strip() or identifier in seen:
                continue
            seen.add(identifier)
            found.append(
                Supplement(
                    identifier=identifier,
                    source=SourceRef(
                        source_id=state,
                        document=result.link.document,
                        locator="the supplement list",
                        retrieved_at=result.link.retrieved_at,
                        content_hash=result.link.content_hash or "0" * 64,
                        parser_id="cycle.discovery",
                        parser_version=parser_version,
                        confidence=Confidence.LOW,
                        original_url=result.link.url,
                        archive_key=result.link.archive_key,
                    ),
                    summary=(
                        "Discovered in the State's supplement list. Nothing "
                        "has read its content, so neither its validity window "
                        "nor what it bears on is known."
                    ),
                )
            )
    return tuple(found)


def write_supplement_manifest(
    path,
    supplements,
    *,
    state: str,
    list_url: str = "",
    list_hash: str = "",
) -> int:
    """Write discovered supplements where :func:`load_supplements` can read them.

    The manifest format already exists for supplements a person transcribed,
    and a discovered one is the same shape with fewer fields filled. Reusing it
    means nothing downstream needs to know which way a supplement arrived — and
    a person who later reads the windows edits this file rather than replacing
    it.

    Every entry is written with empty dates, deliberately. A file that carried
    invented windows would be indistinguishable from a transcribed one, and the
    whole point of :attr:`~aeropub.supplement.ForcePeriod.UNDATED` is that an
    unread window stays visibly unread.

    ``list_hash`` is the hash of the page the supplements were listed on, and
    it is required: :func:`~aeropub.supplement.load_supplements` refuses a
    manifest whose document cannot be identified, so writing one without it
    produces a file that can never be read back. Failing here, with the reason,
    beats leaving that to be discovered later.
    """
    import json
    from pathlib import Path

    target = Path(path)
    ordered = sorted(supplements, key=lambda s: s.identifier)
    if ordered and not list_hash.strip():
        raise ValueError(
            "write_supplement_manifest needs list_hash — the hash of the page "
            "these were listed on. load_supplements refuses a manifest whose "
            "document cannot be identified, so one written without it can "
            "never be read back."
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    first = ordered[0].source if ordered else None

    manifest = {
        "source": {
            "source_id": state,
            "document": f"{state} supplement list",
            "document_path": "",
            "retrieved_at": (
                first.retrieved_at.isoformat()
                if first
                else datetime.now(timezone.utc).isoformat()
            ),
            "published_at": "",
            "original_url": list_url or (first.original_url if first else ""),
            "content_hash": list_hash,
        },
        "region": "",
        "supplements": [
            {
                "identifier": s.identifier,
                "section": s.section,
                "subjects": list(s.subjects),
                "effective_from": "",
                "effective_to": "",
                "supersession": s.supersession.value,
                "summary": s.summary,
                "replaces": s.replaces,
                "remarks": s.remarks,
                "locator": s.source.original_url or s.source.locator,
            }
            for s in ordered
        ],
    }
    target.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return len(ordered)

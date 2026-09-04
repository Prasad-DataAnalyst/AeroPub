"""Loading facts from an AIP page somebody read.

The eAIP parser is the milestone this project is built toward, and it will
serve the States that publish a machine-readable eAIP in a layout a parser has
been written against. That is not most of the 180. A platform that can only
hold what a parser understands reports the rest as a coverage gap forever, and
"we have no parser for Chad" is a fact about us, not about Chad.

This is the other way in. An AIS officer reads AD 2.12 in the published AIP,
writes down the runway figures with the table row each came from, and the
manifest they write loads into the same fact store, with the same bitemporal
model and the same citation discipline as a parsed source. Every downstream
component — the dossier, the bulletin, the forward view, the suitability
assessment — cannot tell the difference and should not: what matters is that
the value resolves to a document and a place inside it, not which mechanism
put it there.

What it costs, said plainly
---------------------------
A hand-written manifest is one person's reading. ``parser_id`` records that
(``aip-manifest``, not an extractor's name) so a systematic transcription
error traces the same way a parser defect does, and ``confidence`` is there to
be lowered when the source was hard to read. It does not remove the need for
the parser; it removes the need to wait for one.

The shape
---------
One file describes one document — one AIP section, one supplement, one
amendment::

    {
      "source": {
        "source_id": "QA-CAA",
        "document": "AIP Qatar AD 2 OTHH",
        "document_path": "archive/othh-ad2.pdf",
        "retrieved_at": "2026-09-03T09:00:00Z",
        "published_at": "2026-09-03",
        "original_url": "https://www.aim.gov.qa/..."
      },
      "precedence": "aip",
      "valid_from": "2026-09-03",
      "facts": [
        {"entity": "OTHH/RWY34L", "attribute": "pcn",
         "value": "80/F/A/W/T", "locator": "AD 2.12, RWY 34L, strength"}
      ]
    }

``precedence`` is which publication layer this document *is* — AIP, AMDT, SUP
or NOTAM — and it decides what overrides what in the effective state. It is
required and never defaulted: a supplement loaded as an AIP would sit beneath
the base it is supposed to override, and the resulting effective state would
be wrong in a way nothing downstream could detect.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any, Iterable, Mapping

from aeropub.entities import normalise
from aeropub.facts import Fact, Precedence
from aeropub.manifest import (
    ManifestError,
    document_source,
    read_manifest,
    sub_source,
    to_date,
)

__all__ = [
    "DEFAULT_PARSER_ID",
    "ManifestError",
    "load_facts",
    "template",
]

#: The parser recorded on every fact loaded this way. Deliberately not an
#: extractor's name: a value transcribed by a person and a value pulled out by
#: code have different failure modes, and a defect in one must not be
#: attributed to the other.
DEFAULT_PARSER_ID = "aip-manifest"


def _precedence(value: Any, *, where: str) -> Precedence:
    if value is None:
        raise ManifestError(
            f"{where}: precedence is required — which publication layer this "
            f"document is, one of {', '.join(p.name.lower() for p in Precedence)}. "
            "A supplement loaded as an AIP sits beneath the base it is meant to "
            "override, and the effective state comes out wrong with nothing "
            "downstream able to tell."
        )
    try:
        return Precedence[str(value).strip().upper()]
    except KeyError:
        raise ManifestError(
            f"{where}: precedence must be one of "
            f"{', '.join(p.name.lower() for p in Precedence)}; got {value!r}"
        ) from None


def load_facts(path: Path | str) -> tuple[Fact, ...]:
    """Read one manifest into cited, dated facts.

    Nothing is stored here — the caller decides which store these go into, and
    a manifest that raises has put nothing anywhere. There is no partial
    success: one uncitable value fails the file.
    """
    path = Path(path)
    manifest = read_manifest(path)
    document = document_source(
        manifest.get("source"),
        base=path.parent,
        where=f"{path}: source",
        parser_id=DEFAULT_PARSER_ID,
    )
    layer = _precedence(manifest.get("precedence"), where=str(path))

    default_from = to_date(
        manifest.get("valid_from"), where=str(path), field="valid_from"
    )
    default_to = to_date(manifest.get("valid_to"), where=str(path), field="valid_to")
    if default_from is None and any(
        "valid_from" not in entry
        for entry in manifest.get("facts", [])
        if isinstance(entry, Mapping)
    ):
        raise ManifestError(
            f"{path}: valid_from is required, at the top or on every fact. A "
            "value with no date is one nothing can resolve an effective state "
            "from — the whole model is 'what was in force on this day'."
        )

    entries = manifest.get("facts")
    if not isinstance(entries, list) or not entries:
        raise ManifestError(
            f"{path}: facts must be a non-empty list. A document with nothing "
            "read out of it is a coverage gap and belongs in the manifest as "
            "nothing at all."
        )

    loaded: list[Fact] = []
    for index, entry in enumerate(entries):
        where = f"{path}: facts[{index}]"
        if not isinstance(entry, Mapping):
            raise ManifestError(f"{where}: must be an object")

        entity = normalise(str(entry.get("entity", "")))
        if not entity:
            raise ManifestError(
                f"{where}: entity is required — what the value is about, e.g. "
                "OTHH or OTHH/RWY34L"
            )
        attribute = str(entry.get("attribute", "")).strip()
        if not attribute:
            raise ManifestError(f"{where}: attribute is required")
        if "value" not in entry or entry["value"] is None:
            raise ManifestError(
                f"{where}: {attribute} has no value. An unknown value is a "
                "coverage gap, not a fact with no value — leave it out."
            )
        locator = str(entry.get("locator", "")).strip()
        if not locator:
            raise ManifestError(
                f"{where}: {attribute} needs a locator — the section, table or "
                "row it was read from. Naming the document alone is not a "
                "citation a reviewer can resolve."
            )
        if "precedence" in entry:
            raise ManifestError(
                f"{where}: precedence belongs to the document, not to one value "
                "inside it. A manifest describes one publication; put facts from "
                "a different layer in their own manifest."
            )

        valid_from = (
            to_date(entry["valid_from"], where=where, field="valid_from")
            if "valid_from" in entry
            else default_from
        )
        valid_to = (
            to_date(entry["valid_to"], where=where, field="valid_to")
            if "valid_to" in entry
            else default_to
        )
        if valid_from is None:
            raise ManifestError(f"{where}: {attribute} has no valid_from")
        if valid_to is not None and valid_to < valid_from:
            raise ManifestError(
                f"{where}: {attribute} expires ({valid_to}) before it takes "
                f"effect ({valid_from})"
            )

        try:
            loaded.append(
                Fact(
                    entity=entity,
                    attribute=attribute,
                    value=entry["value"],
                    valid_from=valid_from,
                    valid_to=valid_to,
                    source=sub_source(document, locator),
                    precedence=layer,
                )
            )
        except (ValueError, TypeError) as error:
            raise ManifestError(f"{where}: {error}") from None

    return tuple(loaded)


#: A blank manifest. Every value empty, so there is nothing here for somebody
#: to keep who did not open the document.
_TEMPLATE = {
    "source": {
        "source_id": "",
        "document": "",
        "document_path": "",
        "retrieved_at": "",
        "published_at": "",
        "original_url": "",
    },
    "precedence": "aip",
    "valid_from": "",
    "facts": [
        {"entity": "", "attribute": "", "value": None, "locator": ""},
    ],
}


def template() -> str:
    """A blank fact manifest, for an AIS officer to fill in from a page they read."""
    return json.dumps(_TEMPLATE, indent=2)

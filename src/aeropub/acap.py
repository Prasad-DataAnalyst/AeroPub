"""Loading aircraft characteristics from a document somebody actually read.

This is the only way figures enter :mod:`aeropub.aircraft`, and it exists
because there is no other honest one. No wingspan ships in the source, so an
operator brings their own — and the format is built so that bringing them
without a citation is not possible rather than merely discouraged.

The file is a manifest, not a database
--------------------------------------
One file describes one aircraft type as read from one document. The document
is named once at the top, every characteristic points at where inside it the
figure was found, and the whole file resolves to a SHA-256 of the document
itself::

    {
      "designator": "B77W",
      "manufacturer": "Boeing",
      "model": "777-300ER",
      "source": {
        "source_id": "BOEING",
        "document": "777 Airplane Characteristics for Airport Planning",
        "document_path": "docs/acap/boeing-777-acap.pdf",
        "retrieved_at": "2026-09-01T12:00:00Z"
      },
      "origin": "acap",
      "characteristics": [
        {"attribute": "wingspan_m", "value": 64.8, "unit": "m",
         "locator": "Table 2.1.1"}
      ]
    }

``document_path`` is hashed as the file is loaded, which is the point: the
citation cannot be written unless the document is on disk to be hashed. A
``content_hash`` may be given instead where the file lives elsewhere, but then
nothing checks it. Every characteristic needs its own ``locator`` — "the ACAP"
is not a citation, "Table 2.1.1" is, and a reviewer three years from now has to
be able to open the page.

**One manifest, one document, one origin.** A characteristic may not declare an
origin of its own, because the citation it would carry is the manifest's — the
document named at the top. An operator figure sitting in an ACAP manifest comes
out cited to the ACAP, which is a false citation and a worse failure than an
uncited figure: it resolves, to the wrong page. Write a second manifest for the
second document and :func:`merge` them.

Why the operator brings the file
--------------------------------
ACAP is public and shippable; a manufacturer's FCOM or FPPM is not, and an
operator's own performance data is theirs. ``origin`` records which, and
``operator`` marks a figure that must not leave the tenant that supplied it —
see :class:`~aeropub.aircraft.Origin`. Nothing here decides that for the
operator; it records what they declare.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from aeropub.aircraft import AircraftType, Characteristic, Origin
from aeropub.manifest import (
    ManifestError,
    document_source,
    read_manifest,
    sha256_of,
    sub_source,
)

__all__ = [
    "DEFAULT_PARSER_ID",
    "ManifestError",
    "load_aircraft",
    "merge",
    "sha256_of",
    "template",
]

#: Recorded as the parser on every characteristic loaded this way. A figure
#: typed from a document by a person is a different provenance from one an
#: extractor pulled out, and a defect in one should not be attributed to the
#: other.
DEFAULT_PARSER_ID = "acap-manifest"


def _origin(value: Any, *, where: str) -> Origin:
    try:
        return Origin(str(value).strip().lower())
    except ValueError:
        raise ManifestError(
            f"{where}: origin must be one of {', '.join(o.value for o in Origin)}. "
            "It decides whether the figure may leave this tenant, so it is not "
            "defaulted."
        ) from None


def load_aircraft(path: Path | str) -> AircraftType:
    """Read one aircraft manifest into a cited :class:`AircraftType`.

    Every characteristic comes back carrying a ``SourceRef`` that resolves to
    the document the manifest names. There is no partial success: a manifest
    with one uncitable figure raises rather than loading the rest, because a
    library that is nearly all cited is one somebody will stop checking.
    """
    path = Path(path)
    manifest = read_manifest(path)
    base = path.parent

    designator = str(manifest.get("designator", "")).strip()
    if not designator:
        raise ManifestError(
            f"{path}: designator is required — the ICAO type designator, not a "
            "marketing name. Two marketing names can share a designator and one "
            "marketing name can span several."
        )

    document = document_source(
        manifest.get("source"),
        base=base,
        where=f"{path}: source",
        parser_id=DEFAULT_PARSER_ID,
    )
    default_origin = _origin(manifest.get("origin", "acap"), where=f"{path}: origin")

    entries = manifest.get("characteristics")
    if not isinstance(entries, list) or not entries:
        raise ManifestError(
            f"{path}: characteristics must be a non-empty list. An aircraft with "
            "no figures is not a smaller answer than one with figures; it is a "
            "coverage gap, and it belongs in the manifest as nothing at all."
        )

    loaded: list[Characteristic] = []
    for index, entry in enumerate(entries):
        where = f"{path}: characteristics[{index}]"
        if not isinstance(entry, Mapping):
            raise ManifestError(f"{where}: must be an object")
        attribute = str(entry.get("attribute", "")).strip()
        if not attribute:
            raise ManifestError(f"{where}: attribute is required")
        if "value" not in entry or entry["value"] is None:
            raise ManifestError(
                f"{where}: {attribute} has no value. A characteristic with no "
                "value is a gap in what is held, not a characteristic — leave "
                "it out of the manifest."
            )
        if "origin" in entry:
            raise ManifestError(
                f"{where}: {attribute} declares its own origin. One manifest "
                "describes one document, and every figure in it is cited to "
                "that document — so a figure from a different source would come "
                "out cited to this one, which resolves to the wrong page. Put "
                "it in its own manifest and load both."
            )
        locator = str(entry.get("locator", "")).strip()
        if not locator:
            raise ManifestError(
                f"{where}: {attribute} needs a locator — the table, figure or "
                "page the figure was read from. Naming the document alone is "
                "not a citation a reviewer can resolve."
            )
        loaded.append(
            Characteristic(
                attribute=attribute,
                value=entry["value"],
                source=sub_source(document, locator),
                origin=default_origin,
                unit=str(entry["unit"]) if entry.get("unit") else None,
                variant=str(entry["variant"]) if entry.get("variant") else None,
            )
        )

    return AircraftType(
        designator=designator,
        manufacturer=str(manifest.get("manufacturer", "")).strip(),
        model=str(manifest.get("model", "")).strip(),
        characteristics=tuple(loaded),
    )


def merge(*aircraft: AircraftType) -> AircraftType:
    """Combine manifests for one type, each keeping its own citations.

    The usual case is an ACAP manifest and an operator manifest for the same
    aeroplane: public dimensions from one document, licensed figures from
    another, and each characteristic still resolving to the page it was read
    from. Identity comes from the first — a merge across designators is a
    mistake, not a fleet.
    """
    if not aircraft:
        raise ValueError("merge needs at least one aircraft")
    first = aircraft[0]
    mismatched = {a.designator for a in aircraft if a.designator != first.designator}
    if mismatched:
        raise ValueError(
            f"cannot merge {first.designator} with "
            f"{', '.join(sorted(mismatched))}: these are different types. Merging "
            "them would produce one aeroplane with another's wingspan."
        )
    return first.with_characteristics(
        item for other in aircraft[1:] for item in other.characteristics
    )


#: A manifest skeleton, printed rather than shipped filled in. Every value is
#: empty: there is no figure here to be copied by somebody who did not open the
#: document, which is the failure the whole citation discipline exists against.
_TEMPLATE = {
    "designator": "",
    "manufacturer": "",
    "model": "",
    "source": {
        "source_id": "",
        "document": "",
        "document_path": "",
        "retrieved_at": "",
        "original_url": "",
    },
    "origin": "acap",
    "characteristics": [
        {"attribute": name, "value": None, "unit": unit, "locator": ""}
        for name, unit in (
            ("wingspan_m", "m"),
            ("omgws_m", "m"),
            ("overall_length_m", "m"),
            ("fuselage_width_m", "m"),
            ("reference_field_length_m", "m"),
        )
    ]
    + [{"attribute": "acn", "value": None, "variant": "F/A at MTOW", "locator": ""}],
}


def template() -> str:
    """A blank manifest, for an operator to fill in from a document they hold.

    The attributes listed are the ones the suitability assessment reads. Delete
    any the document does not give rather than guessing: an absent figure is a
    check reported as not made, which is a true statement, and a guessed one is
    a check reported as passed, which may not be.
    """
    return json.dumps(_TEMPLATE, indent=2)

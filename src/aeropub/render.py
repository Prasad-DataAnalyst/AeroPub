"""Printable output — the same payload, laid out for a person.

Plan section 24 makes print a first-class channel rather than a browser's
best guess at one, and section 23 sets what the page has to do: delta first,
a consequence on every line, provenance on every value, and coverage gaps
shown rather than omitted.

The rule that keeps it honest is that this module **renders the API payload and
nothing else**. It receives what :mod:`aeropub.api` produced and fills a
template with it; it does not reach back into a fact store, recompute a value,
or re-word an assessment. A printed document that disagreed with the JSON for
the same aerodrome would be the worst artefact this system could produce,
because the two would be compared exactly when something had gone wrong.

That is also why the page carries its own JSON. An engineer who wants to check
that the printed figure is the delivered figure can open the payload on the
same page rather than take it on trust.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from aeropub.api import document
from aeropub.bulletin import ChangeBulletin
from aeropub.dossier import AerodromeDossier
from aeropub.horizon import Horizon
from aeropub.lenses import Audience, LensView
from aeropub.quality import QualityReport

__all__ = ["PLACEHOLDER", "render_dossier", "template"]

#: Where the template expects its data. One substitution, so the template
#: stays a file a designer can open rather than a string built by code.
PLACEHOLDER = "__DATA__"

_TEMPLATE = Path(__file__).parent / "templates" / "dossier.html"


def template() -> str:
    """The page template, as shipped."""
    return _TEMPLATE.read_text(encoding="utf-8")


def _embed(payload: Mapping[str, Any]) -> str:
    """Serialise data for a ``<script type="application/json">`` block.

    ``</`` is escaped because an unescaped one inside a script element ends it,
    and a NOTAM or an AIP extract containing ``</`` would otherwise break the
    page — silently, and only for the aerodromes whose text happens to contain
    it, which is the worst kind of bug to find in production.
    """
    return json.dumps(payload, sort_keys=True).replace("</", "<\\/")


def render_dossier(
    dossier: AerodromeDossier,
    *,
    bulletin: ChangeBulletin | None = None,
    horizon: Horizon | None = None,
    conduct: QualityReport | None = None,
    lenses: Mapping[Audience | str, LensView] | None = None,
    generated_at: datetime | None = None,
    **api_options: Any,
) -> str:
    """One aerodrome study as a printable page.

    Every argument beyond the dossier is optional, and every omission is
    visible: the page renders the sections it was given and says plainly where
    something is absent, rather than dropping a heading and reading complete.
    """
    moment = generated_at or datetime.now(timezone.utc)
    empty_bulletin = bulletin is None
    payload: dict[str, Any] = {
        "dossier": document(dossier, generated_at=moment, **api_options),
        "lenses": {
            (key.value if isinstance(key, Audience) else str(key)): document(
                value, generated_at=moment, **api_options
            )
            for key, value in (lenses or {}).items()
        },
    }
    for name, item in (
        ("bulletin", bulletin), ("horizon", horizon), ("conduct", conduct)
    ):
        payload[name] = (
            document(item, generated_at=moment, **api_options)
            if item is not None
            else {"aeropub": {"version": "v1", "kind": name, "generated_at": None},
                  "data": None}
        )
    if empty_bulletin:
        # Said in the payload rather than left for the reader to infer from a
        # missing heading.
        payload["bulletin"]["data"] = {
            "summary": {"changes": 0, "action": 0},
            "coverage_statement": (
                "No change record was supplied for this document, so it states "
                "what is true now and not what moved to get here."
            ),
            "changes": [],
            "conclusive": False,
        }
    return template().replace(PLACEHOLDER, _embed(payload))

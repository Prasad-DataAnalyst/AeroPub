"""One page for one sector: the map, the open items, and what was not read.

Every module here produces a view and every view renders as text. A dispatcher
does not read twelve texts. This puts them in one document with the drawing at
the top and the open items under it, ranked, and it is the form the work has
been heading towards since the route dossier got its first section.

The headline is coverage, not comfort
--------------------------------------
The first thing on the page is how much of the sector the platform can speak
for — ``spoken for 9 of 14`` — and the verdict is qualified by it. A dossier
that read both ends of a route and none of the middle would otherwise print
"no critical findings" in the same typeface as one that read everything, and a
reader would take the two for the same statement. So a page that is not
conclusive says so before it says anything else, and it never shows a green
verdict.

Nothing is computed here
------------------------
This module lays out what the dossier already decided. It does not rank, screen
or conclude — every severity on the page came from the section that raised it,
and every sentence under a finding is that section's own words. A layout that
started making judgements would be a second opinion nobody could trace.

What a reader can always find
------------------------------
The open items are the operational summary and they come first, because that is
the list somebody works from. Under them, each section is available in full,
collapsed — the reasoning is there for the reader who wants it and out of the
way of the one who does not. And "not addressed" is a section of its own rather
than a footnote: what the platform did not look at is not a smaller fact than
what it found.
"""

from __future__ import annotations

import html
from typing import Iterable

from aeropub.airac import AiracCycle
from aeropub.atlas import ATLAS_CSS, ATLAS_JS, Atlas, atlas_svg
from aeropub.operator import Exposure
from aeropub.route import OpenItem, RouteDossier

__all__ = ["BRIEFING_CSS", "briefing_html", "sections_of"]

#: Severity order, worst first. The page is a list somebody works down.
_ORDER = (
    Exposure.CRITICAL,
    Exposure.HIGH,
    Exposure.MEDIUM,
    Exposure.UNKNOWN,
    Exposure.LOW,
)

#: What each severity means on the page, in the words `operator.py` defines it
#: with. Repeated here because a reader meeting "UNKNOWN" for the first time
#: needs to know it is not a mild version of LOW.
_MEANING = {
    Exposure.CRITICAL: "the operation as planned is not available",
    Exposure.HIGH: "something must be recomputed or replanned before operating",
    Exposure.MEDIUM: "a condition to observe. The operation stands",
    Exposure.UNKNOWN: (
        "nobody has read what would answer this. Not a mild finding — an "
        "absent one"
    ),
    Exposure.LOW: "worth knowing. Nothing follows from it on its own",
}


def _e(text: object) -> str:
    return html.escape(str(text), quote=True)


def sections_of(dossier: RouteDossier) -> tuple[tuple[str, str], ...]:
    """Each section of the dossier that was assembled, with its own render.

    A section not supplied contributes nothing rather than an empty heading:
    "ENR 5 turned up no hazards" and "no ENR 5 was given" are different
    statements and the second has no findings to show.
    """
    found: list[tuple[str, str]] = []
    for label, view in (
        ("Airspace — ENR 2", dossier.airspace),
        ("Navigation warnings — ENR 5", dossier.hazards),
        ("Surveillance — ENR 1.6", dossier.surveillance),
        ("Supplementary procedures — ENR 1.8", dossier.supps),
        ("GNSS — ENR 4.3", dossier.gnss),
        ("Flight planning — ENR 1.10", dossier.planning),
    ):
        if view is not None:
            found.append((label, view.render()))
    return tuple(found)


BRIEFING_CSS = """
:root { --bf-ground: #eef1f3; --bf-panel: #fff; --bf-ink: #16202b;
  --bf-muted: #64757f; --bf-rule: #c6d2d8; --bf-soft: #e3e9ec;
  --bf-critical: #a02f22; --bf-high: #b8541c; --bf-medium: #9a7b16;
  --bf-unknown: #5f6f7a; --bf-low: #4a7a63; --bf-ok: #0d6e63; }
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) { --bf-ground: #0d141a; --bf-panel: #18222b;
    --bf-ink: #e6edf3; --bf-muted: #93a4b3; --bf-rule: #27343d;
    --bf-soft: #1d2831; --bf-critical: #e5705f; --bf-high: #f0913f;
    --bf-medium: #d9b64a; --bf-unknown: #93a4b3; --bf-low: #63c69b;
    --bf-ok: #4fbfae; } }
:root[data-theme="dark"] { --bf-ground: #0d141a; --bf-panel: #18222b;
  --bf-ink: #e6edf3; --bf-muted: #93a4b3; --bf-rule: #27343d;
  --bf-soft: #1d2831; --bf-critical: #e5705f; --bf-high: #f0913f;
  --bf-medium: #d9b64a; --bf-unknown: #93a4b3; --bf-low: #63c69b;
  --bf-ok: #4fbfae; }
* { box-sizing: border-box; }
body { margin: 0; background: var(--bf-ground); color: var(--bf-ink);
  font: 400 15px/1.55 ui-sans-serif, system-ui, sans-serif; }
.bf { max-width: 1180px; margin: 0 auto; padding: 32px 22px 64px; }
.bf-head { border-bottom: 2px solid var(--bf-ink); padding-bottom: 16px; }
.bf-head h1 { margin: 0 0 4px; font-size: clamp(24px, 3.6vw, 34px);
  letter-spacing: -0.02em; text-wrap: balance; }
.bf-sub { margin: 0; color: var(--bf-muted);
  font: 400 13px/1.5 ui-monospace, SFMono-Regular, Menlo, monospace; }
.bf-verdict { display: flex; flex-wrap: wrap; gap: 12px; align-items: stretch;
  margin: 20px 0 8px; }
.bf-card { background: var(--bf-panel); border: 1px solid var(--bf-rule);
  border-radius: 3px; padding: 12px 16px; flex: 1 1 190px; }
.bf-card dt { font: 600 10px/1.2 ui-monospace, monospace; letter-spacing: .08em;
  text-transform: uppercase; color: var(--bf-muted); margin: 0 0 6px; }
.bf-card dd { margin: 0; font-size: 20px; font-variant-numeric: tabular-nums;
  font-weight: 600; }
.bf-card .bf-note { display: block; font-size: 12px; font-weight: 400;
  color: var(--bf-muted); margin-top: 4px; }
.bf-warn { border-left: 3px solid var(--bf-high); }
.bf-banner { background: var(--bf-panel); border: 1px solid var(--bf-rule);
  border-left: 3px solid var(--bf-high); border-radius: 3px;
  padding: 12px 16px; margin: 16px 0; font-size: 14px; }
.bf-banner strong { color: var(--bf-high); }
h2 { font: 600 12px/1.2 ui-monospace, monospace; text-transform: uppercase;
  letter-spacing: .09em; color: var(--bf-muted); margin: 34px 0 12px;
  border-top: 1px solid var(--bf-rule); padding-top: 14px; }
.bf-band { margin: 0 0 18px; }
.bf-band h3 { display: flex; align-items: baseline; gap: 10px; margin: 0 0 8px;
  font: 600 13px/1.3 ui-sans-serif, system-ui, sans-serif; }
.bf-tag { font: 600 10px/1 ui-monospace, monospace; letter-spacing: .07em;
  text-transform: uppercase; border: 1px solid currentColor; border-radius: 2px;
  padding: 3px 6px; }
.bf-meaning { font-weight: 400; color: var(--bf-muted); font-size: 12.5px; }
.bf-critical { color: var(--bf-critical); }
.bf-high { color: var(--bf-high); }
.bf-medium { color: var(--bf-medium); }
.bf-unknown { color: var(--bf-unknown); }
.bf-low { color: var(--bf-low); }
.bf-items { list-style: none; margin: 0; padding: 0;
  border: 1px solid var(--bf-rule); border-radius: 3px;
  background: var(--bf-panel); }
.bf-items li { padding: 10px 14px; border-top: 1px solid var(--bf-rule);
  display: grid; grid-template-columns: minmax(90px, 150px) 1fr; gap: 4px 16px; }
.bf-items li:first-child { border-top: 0; }
.bf-where { font: 600 12px/1.5 ui-monospace, monospace; color: var(--bf-muted);
  word-break: break-word; }
.bf-what { font-weight: 500; }
.bf-why { grid-column: 2; color: var(--bf-muted); font-size: 13px; }
.bf-none { color: var(--bf-muted); font-size: 14px; margin: 0 0 18px; }
details { background: var(--bf-panel); border: 1px solid var(--bf-rule);
  border-radius: 3px; margin-bottom: 8px; }
summary { cursor: pointer; padding: 10px 14px; font-weight: 500;
  font-size: 14px; }
summary::marker { color: var(--bf-muted); }
details pre { margin: 0; padding: 0 14px 14px; overflow-x: auto;
  font: 12px/1.5 ui-monospace, SFMono-Regular, Menlo, monospace;
  color: var(--bf-ink); white-space: pre-wrap; word-break: break-word; }
.bf-unread { list-style: none; margin: 0; padding: 0; }
.bf-unread li { padding: 8px 0; border-top: 1px solid var(--bf-rule);
  font-size: 14px; color: var(--bf-muted); }
.bf-unread li:first-child { border-top: 0; }
footer { margin-top: 44px; border-top: 1px solid var(--bf-rule);
  padding-top: 16px; color: var(--bf-muted); font-size: 12.5px; }
"""


def _verdict(dossier: RouteDossier) -> tuple[str, str]:
    """The headline, and the class it is drawn in.

    A dossier that is not conclusive never shows a settled verdict, however
    little it happened to find: the two look identical on a page and are not
    the same statement.
    """
    worst = dossier.overall
    if not dossier.is_conclusive:
        return ("not established", "bf-unknown")
    if worst is Exposure.NONE:
        return ("nothing found", "bf-ok")
    return (worst.value, f"bf-{worst.value}")


def _items_html(items: Iterable[OpenItem]) -> str:
    rows = []
    for item in items:
        why = f'<span class="bf-why">{_e(item.why)}</span>' if item.why else ""
        rows.append(
            f'<li><span class="bf-where">{_e(item.where)}</span>'
            f'<span class="bf-what">{_e(item.what)}</span>{why}</li>'
        )
    return f'<ul class="bf-items">{"".join(rows)}</ul>'


def briefing_html(
    dossier: RouteDossier, *, atlas: Atlas | None = None, title: str = ""
) -> str:
    """One page for one sector: the drawing, the open items, the gaps.

    ``atlas`` is optional and drawn where it is given. A briefing without one
    is still a briefing — the findings are the product and the map is how a
    reader places them.
    """
    route = dossier.route
    name = title or (
        route.designator or f"{route.departure} – {route.destination}"
    )
    verdict, verdict_class = _verdict(dossier)
    spoken, places = dossier.coverage
    cycle = AiracCycle.containing(dossier.on)

    parts: list[str] = [
        f"<title>{_e(name)} briefing</title>",
        "<style>" + BRIEFING_CSS + (ATLAS_CSS if atlas is not None else "") + "</style>",
        '<div class="bf">',
        '<header class="bf-head">',
        f"<h1>{_e(name)}</h1>",
        '<p class="bf-sub">'
        + _e(
            f"{route.departure} → {route.destination}"
            + (
                f" · via {', '.join(j.designator for j in route.crosses)}"
                if route.crosses
                else ""
            )
            + f" · for {dossier.on.isoformat()} · AIRAC {cycle.identifier}"
            + f" · assembled {dossier.as_at.isoformat(timespec='minutes')}"
        )
        + "</p>",
        "</header>",
        '<div class="bf-verdict">',
        f'<dl class="bf-card"><dt>Verdict</dt><dd class="{verdict_class}">'
        f"{_e(verdict.upper())}</dd></dl>",
        '<dl class="bf-card"><dt>Spoken for</dt>'
        f"<dd>{spoken} of {places}"
        '<span class="bf-note">aerodromes, regions and route legs this '
        "dossier could read</span></dd></dl>",
        '<dl class="bf-card"><dt>Open items</dt>'
        f"<dd>{len(dossier.open_items)}</dd></dl>",
    ]
    if route.planned_level_ft is not None:
        parts.append(
            '<dl class="bf-card"><dt>Planned level</dt>'
            f"<dd>{route.planned_level_ft:.0f} ft</dd></dl>"
        )
    parts.append("</div>")

    if not dossier.is_conclusive:
        parts.append(
            '<p class="bf-banner"><strong>This does not speak for the whole '
            "sector.</strong> "
            f"{places - spoken} of {places} places on it are unread, so the "
            "absence of a finding below is not the same as the absence of a "
            "problem. Every unread place is an open item of its own.</p>"
        )

    if atlas is not None:
        parts += [
            "<h2>The sector</h2>",
            '<div class="at-wrap">',
            atlas_svg(atlas, width=1120, height=560),
            '<div class="at-controls">',
        ]
        for key, label in (
            ("graticule", "Grid"),
            ("basemap", "Coast"),
            ("fir", "FIR/UIR"),
            ("terminal", "TMA/CTR"),
            ("routes", "ATS routes"),
            ("track", "Filed route"),
            ("points", "Points"),
            ("hazard", "P/R/D"),
        ):
            parts.append(
                f'<button type="button" data-layer="{key}" '
                f'aria-pressed="true">{label}</button>'
            )
        parts += [
            '<button type="button" data-reset>Reset</button></div>',
            '<aside class="at-panel" aria-live="polite"><h3>Click anything</h3>'
            "<p>Every feature says what the AIP published about it and which "
            "document that was.</p></aside>",
            "</div>",
        ]
        if atlas.unplaced:
            parts.append(
                '<p class="bf-banner">Named by the AIP and not drawn: '
                + _e(", ".join(atlas.unplaced))
                + ". A point in the wrong place is a map; a point missing is "
                "a gap.</p>"
            )

    parts.append("<h2>Open items</h2>")
    if not dossier.open_items:
        parts.append(
            '<p class="bf-none">Nothing was raised by any section that ran. '
            "Read that against the coverage above rather than on its own.</p>"
        )
    for severity in _ORDER:
        found = dossier.items_at(severity)
        if not found:
            continue
        parts.append(
            f'<section class="bf-band"><h3>'
            # Uppercased in the markup, not only by text-transform: a page
            # read as text, or with styles stripped, still has to say HIGH.
            f'<span class="bf-tag bf-{severity.value}">'
            f'{severity.value.upper()}</span>'
            f'<span class="bf-meaning">{_e(_MEANING[severity])}</span></h3>'
            + _items_html(found)
            + "</section>"
        )

    if dossier.not_addressed:
        parts.append("<h2>Not addressed</h2>")
        parts.append(
            '<p class="bf-none">What this platform did not look at. Listed '
            "because a reader cannot tell the difference between a check that "
            "passed and one nobody ran.</p>"
        )
        parts.append(
            '<ul class="bf-unread">'
            + "".join(f"<li>{_e(item)}</li>" for item in dossier.not_addressed)
            + "</ul>"
        )

    sections = sections_of(dossier)
    if sections:
        parts.append("<h2>Sections in full</h2>")
        for label, body in sections:
            parts.append(
                f"<details><summary>{_e(label)}</summary>"
                f"<pre>{_e(body)}</pre></details>"
            )

    parts += [
        "<footer>Assembled by AeroPub from the publications named in each "
        "section. Every finding above is the reasoning of the section that "
        "raised it; this page ranks and lays out, and decides nothing. "
        "Nothing here answers whether a point is inside an area.</footer>",
        "</div>",
    ]
    if atlas is not None:
        parts.append("<script>" + ATLAS_JS + "</script>")
    return "\n".join(parts)

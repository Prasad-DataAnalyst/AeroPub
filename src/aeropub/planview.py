"""The plan view — published positions, the track that actually joins them.

This is the picture the schematic could not be. :mod:`aeropub.diagram` draws
connectivity because connectivity is all the platform held; once ENR 4 has been
read, positions are held too, and a plan view stops being an invention and
becomes a rendering of a published table.

What separates it from a chart
-------------------------------
It is not one, and the page says so. A chart carries terrain, airspace
boundaries, obstacles, magnetic variation and a hundred things drawn from
survey. This carries exactly what has been read: the positions of points and
aids, the airways between them, and the track between the points a route names.
Everything absent from it is absent because nobody read it, not because it is
not there — and the page states that rather than letting an empty area read as
empty airspace.

Two rules do all the work
--------------------------
**A point without a held position is never placed.** It is listed, by name,
under the drawing. A waypoint at a guessed position is the one output that
would be worse than no drawing at all: a gap announces itself and a wrong
position does not.

**A route leg is drawn as a great circle.** The straight line between two
points on a Mercator sheet is a different route — between distant points it
sits hundreds of miles from the track flown — so every leg is drawn through
intermediate positions computed on the sphere.

Interaction, and why it is this much
-------------------------------------
Pan, zoom, layer toggles and click-for-detail, in about eighty lines of inline
script and no library. That is the amount of interaction the data justifies: a
route crossing six FIRs does not fit legibly at one scale, and a reader needs
to be able to ask what a dot is. Anything more — measuring tools, editing,
route building — would be a second product built on figures this platform reads
rather than owns.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Iterable, Mapping, Sequence

from aeropub.entities import normalise
from aeropub.geo import (
    Bounds,
    Position,
    bounds_of,
    great_circle_nm,
    great_circle_path,
    initial_bearing,
    mercator,
)

__all__ = [
    "Airway",
    "PlanView",
    "PlottedPoint",
    "RouteLeg",
    "plan_html",
    "plan_svg",
    "plan_view",
]

_MARGIN = 0.06


@dataclass(frozen=True, slots=True)
class PlottedPoint:
    """One point that has a held position and can therefore be drawn."""

    designator: str
    position: Position
    kind: str = "fix"
    """``fix``, ``navaid`` or ``aerodrome`` — what symbol to draw, and what the
    detail panel says it is."""

    on_route: bool = False
    notams: int = 0
    detail: str = ""

    @property
    def key(self) -> str:
        return self.designator


@dataclass(frozen=True, slots=True)
class Airway:
    """One published airway, as the positions of its points allow it drawn.

    ``gaps`` counts the points on the airway that could not be plotted. An
    airway drawn through five of its seven points is a different shape from
    the published one, and the count is what stops that being invisible.
    """

    route: str
    positions: tuple[Position, ...] = ()
    gaps: int = 0
    closed: bool = False
    notams: int = 0

    @property
    def is_drawable(self) -> bool:
        return len(self.positions) >= 2


@dataclass(frozen=True, slots=True)
class RouteLeg:
    """One leg of the filed route, drawn as the track actually flown."""

    start: str
    end: str
    via: str
    path: tuple[Position, ...] = ()
    distance_nm: float | None = None
    bearing_deg: float | None = None

    @property
    def is_drawable(self) -> bool:
        return len(self.path) >= 2


@dataclass(frozen=True, slots=True)
class PlanView:
    """Everything the drawing needs, and everything it could not draw."""

    points: tuple[PlottedPoint, ...] = ()
    airways: tuple[Airway, ...] = ()
    legs: tuple[RouteLeg, ...] = ()
    unplottable: tuple[str, ...] = ()
    """Named in the route or the structure, with no held position. Listed
    under the drawing rather than placed anywhere."""

    title: str = ""
    bounds: Bounds | None = None

    @property
    def route_distance_nm(self) -> float | None:
        """Total great-circle distance across the drawn legs.

        ``None`` if any leg could not be drawn, because a partial total is a
        smaller number than the route and a reader would take it for the
        route length. Computed from published coordinates, and labelled as
        computed wherever it is shown.
        """
        if not self.legs or any(not leg.is_drawable for leg in self.legs):
            return None
        found = [leg.distance_nm for leg in self.legs]
        if any(d is None for d in found):
            return None
        return sum(found)

    @property
    def is_complete(self) -> bool:
        """Whether everything named could be drawn.

        Never true merely because the drawing looks full: an airway missing
        two of its points draws a plausible line through the rest.
        """
        return (
            bool(self.points)
            and not self.unplottable
            and not any(a.gaps for a in self.airways)
        )


def _detail(point: PlottedPoint) -> str:
    return point.detail or point.position.describe()


def plan_view(
    *,
    positions: Mapping[str, Position],
    route_points: Sequence[str] = (),
    airways: Mapping[str, Sequence[str]] | None = None,
    closed_routes: Iterable[str] = (),
    notams: Iterable[tuple] = (),
    navaids: Iterable[str] = (),
    aerodromes: Iterable[str] = (),
    details: Mapping[str, str] | None = None,
    title: str = "",
    steps: int = 24,
) -> PlanView:
    """Assemble a plan view from held positions and what names them.

    ``positions`` is the only source of geometry, and anything not in it is
    unplottable rather than placed. That is the whole discipline of this
    module: a name with no position is a name, and it goes in a list.
    """
    held = {normalise(k): v for k, v in positions.items()}
    navaid_names = {normalise(n) for n in navaids}
    aerodrome_names = {normalise(a) for a in aerodromes}
    on_route = [normalise(p) for p in route_points if str(p).strip()]
    shut = {normalise(r) for r in closed_routes if str(r).strip()}
    described = {normalise(k): v for k, v in (details or {}).items()}

    counted: dict[str, int] = {}
    for entry in notams:
        key = normalise(entry[0]) if entry else ""
        if key:
            counted[key] = counted.get(key, 0) + 1

    def notam_count(designator: str) -> int:
        return sum(
            counted.get(key, 0)
            for key in (
                designator,
                f"FIX:{designator}",
                f"NAVAID:{designator}",
            )
        )

    missing: list[str] = []
    named: list[str] = list(on_route)
    for route, points in (airways or {}).items():
        named.extend(normalise(p) for p in points)
    named.extend(sorted(navaid_names | aerodrome_names))

    plotted: list[PlottedPoint] = []
    seen: set[str] = set()
    for designator in named:
        if designator in seen:
            continue
        seen.add(designator)
        position = held.get(designator)
        if position is None:
            missing.append(designator)
            continue
        kind = (
            "aerodrome"
            if designator in aerodrome_names
            else ("navaid" if designator in navaid_names else "fix")
        )
        plotted.append(
            PlottedPoint(
                designator=designator,
                position=position,
                kind=kind,
                on_route=designator in on_route,
                notams=notam_count(designator),
                detail=described.get(designator, ""),
            )
        )

    drawn_airways: list[Airway] = []
    for route, points in (airways or {}).items():
        canonical = normalise(route)
        here = [normalise(p) for p in points]
        found = [held[p] for p in here if p in held]
        drawn_airways.append(
            Airway(
                route=canonical,
                positions=tuple(found),
                gaps=len(here) - len(found),
                closed=canonical in shut,
                notams=counted.get(f"ATS:{canonical}", 0),
            )
        )

    legs: list[RouteLeg] = []
    for start, end in zip(on_route, on_route[1:]):
        a, b = held.get(start), held.get(end)
        if a is None or b is None:
            legs.append(RouteLeg(start=start, end=end, via=""))
            continue
        legs.append(
            RouteLeg(
                start=start,
                end=end,
                via="",
                path=great_circle_path(a, b, steps=steps),
                distance_nm=great_circle_nm(a, b),
                bearing_deg=initial_bearing(a, b),
            )
        )

    everything = [p.position for p in plotted]
    for leg in legs:
        everything.extend(leg.path)
    window = bounds_of(everything)

    return PlanView(
        points=tuple(plotted),
        airways=tuple(drawn_airways),
        legs=tuple(legs),
        unplottable=tuple(sorted(set(missing))),
        title=title,
        bounds=window.padded(_MARGIN) if window else None,
    )


def _escape(text: str) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def plan_svg(view: PlanView, *, width: float = 960.0, height: float = 620.0) -> str:
    """Draw the plan view as one ``<svg>`` element over the projected window."""
    if view.bounds is None:
        return (
            '<svg class="pv" viewBox="0 0 960 200" width="100%" role="img" '
            'aria-label="No positions held" '
            'xmlns="http://www.w3.org/2000/svg">'
            '<text x="24" y="100" class="pv-note">No position has been read for '
            "anything on this route. Nothing is drawn, which is a coverage gap "
            "and not an empty sky.</text></svg>"
        )

    box = view.bounds
    # The window is fitted to the drawing rather than the drawing to the
    # window, and the aspect is preserved: stretching a projection to fill a
    # box changes every angle on it, which is the one property Mercator is
    # chosen for.
    span = max(box.width, box.height * (width / height))
    span_y = span * (height / width)
    centre_x = (box.min_x + box.max_x) / 2.0
    centre_y = (box.min_y + box.max_y) / 2.0

    def to_xy(position: Position) -> tuple[float, float]:
        x, y = mercator(position)
        return (
            width * (0.5 + (x - centre_x) / span),
            height * (0.5 - (y - centre_y) / span_y),
        )

    out: list[str] = [
        f'<svg class="pv" id="pv" viewBox="0 0 {width:.0f} {height:.0f}" '
        f'width="100%" role="img" '
        f'aria-label="Plan view{": " + _escape(view.title) if view.title else ""}" '
        'xmlns="http://www.w3.org/2000/svg">',
        '<g id="pv-pan">',
        '<g class="pv-layer" data-layer="airways">',
    ]

    for airway in view.airways:
        if not airway.is_drawable:
            continue
        points = " ".join(f"{x:.1f},{y:.1f}" for x, y in map(to_xy, airway.positions))
        classes = "pv-airway" + (" pv-shut" if airway.closed else "")
        out.append(f'<polyline points="{points}" class="{classes}"/>')
        head_x, head_y = to_xy(airway.positions[0])
        out.append(
            f'<text x="{head_x + 8:.1f}" y="{head_y - 8:.1f}" class="pv-airway-label">'
            f"{_escape(airway.route)}{' CLOSED' if airway.closed else ''}</text>"
        )
    out.append("</g>")

    out.append('<g class="pv-layer" data-layer="route">')
    for leg in view.legs:
        if not leg.is_drawable:
            continue
        points = " ".join(f"{x:.1f},{y:.1f}" for x, y in map(to_xy, leg.path))
        out.append(f'<polyline points="{points}" class="pv-track"/>')
    out.append("</g>")

    out.append('<g class="pv-layer" data-layer="points">')
    for point in view.points:
        x, y = to_xy(point.position)
        classes = f"pv-point pv-{point.kind}"
        if point.on_route:
            classes += " pv-on-route"
        if point.notams:
            classes += " pv-notam"
        payload = _escape(
            json.dumps(
                {
                    "name": point.designator,
                    "kind": point.kind,
                    "position": point.position.describe(),
                    "notams": point.notams,
                    "detail": _detail(point),
                }
            )
        )
        out.append(
            f'<g class="{classes}" tabindex="0" role="button" '
            f'data-info="{payload}" '
            f'aria-label="{_escape(point.designator)}">'
        )
        if point.kind == "aerodrome":
            out.append(
                f'<rect x="{x - 5:.1f}" y="{y - 5:.1f}" width="10" height="10" '
                'class="pv-mark"/>'
            )
        elif point.kind == "navaid":
            out.append(
                f'<polygon points="{x:.1f},{y - 6:.1f} {x + 6:.1f},{y:.1f} '
                f'{x:.1f},{y + 6:.1f} {x - 6:.1f},{y:.1f}" class="pv-mark"/>'
            )
        else:
            out.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4" class="pv-mark"/>')
        if point.notams:
            out.append(
                f'<circle cx="{x:.1f}" cy="{y:.1f}" r="10" class="pv-ring"/>'
            )
        out.append(
            f'<text x="{x:.1f}" y="{y - 12:.1f}" class="pv-label" '
            f'text-anchor="middle">{_escape(point.designator)}</text>'
        )
        out.append("</g>")
    out.append("</g>")

    out.append("</g>")
    out.append("</svg>")
    return "\n".join(out)


PLAN_VIEW_CSS = """
.pv-wrap { --pv-ink: #16202b; --pv-muted: #5b6b7a; --pv-ground: #eef2f5;
  --pv-line: #7d97a8; --pv-track: #1b6ca8; --pv-shut: #c0392b;
  --pv-mark: #16202b; --pv-card: #ffffff; }
@media (prefers-color-scheme: dark) {
  .pv-wrap:not([data-theme="light"]) { --pv-ink: #e6edf3; --pv-muted: #93a4b3;
    --pv-ground: #131b22; --pv-line: #57707f; --pv-track: #63b3ed;
    --pv-shut: #e5705f; --pv-mark: #cfd9e2; --pv-card: #1b242c; } }
.pv-wrap { background: var(--pv-ground); border: 1px solid var(--pv-line);
  border-radius: 3px; position: relative; overflow: hidden; }
.pv { display: block; touch-action: none; cursor: grab; }
.pv:active { cursor: grabbing; }
.pv-airway { fill: none; stroke: var(--pv-line); stroke-width: 1.5;
  stroke-linejoin: round; }
.pv-shut { stroke: var(--pv-shut); stroke-dasharray: 8 5; stroke-width: 2; }
.pv-track { fill: none; stroke: var(--pv-track); stroke-width: 2.5;
  stroke-linejoin: round; stroke-linecap: round; }
.pv-mark { fill: var(--pv-card); stroke: var(--pv-mark); stroke-width: 1.6; }
.pv-on-route .pv-mark { fill: var(--pv-track); stroke: var(--pv-track); }
.pv-ring { fill: none; stroke: var(--pv-shut); stroke-width: 1.5; }
.pv-label, .pv-airway-label, .pv-note { font: 11px/1.3 system-ui, sans-serif;
  fill: var(--pv-ink); }
.pv-airway-label { fill: var(--pv-muted); font-weight: 600; }
.pv-point { cursor: pointer; }
.pv-point:focus { outline: none; }
.pv-point:focus .pv-mark, .pv-point:hover .pv-mark { stroke-width: 3; }
.pv-layer[hidden] { display: none; }
.pv-panel { position: absolute; right: 10px; top: 10px; max-width: 260px;
  background: var(--pv-card); border: 1px solid var(--pv-line);
  border-radius: 3px; padding: 12px 14px; font: 12.5px/1.5 system-ui, sans-serif;
  color: var(--pv-ink); }
.pv-panel h3 { margin: 0 0 6px; font-size: 13px; }
.pv-panel dl { margin: 0; display: grid; grid-template-columns: auto 1fr;
  gap: 2px 10px; }
.pv-panel dt { color: var(--pv-muted); }
.pv-panel dd { margin: 0; }
.pv-controls { position: absolute; left: 10px; top: 10px; display: flex;
  gap: 6px; flex-wrap: wrap; }
.pv-controls button { font: 600 11px/1 system-ui, sans-serif; padding: 7px 10px;
  border: 1px solid var(--pv-line); border-radius: 2px; cursor: pointer;
  background: var(--pv-card); color: var(--pv-ink); }
.pv-controls button[aria-pressed="false"] { opacity: .45; }
"""

_PLAN_VIEW_JS = """
(function () {
  var svg = document.getElementById('pv');
  if (!svg) return;
  var pan = document.getElementById('pv-pan');
  var view = { x: 0, y: 0, k: 1 };
  var dragging = null;

  function apply() {
    pan.setAttribute('transform',
      'translate(' + view.x + ',' + view.y + ') scale(' + view.k + ')');
  }
  svg.addEventListener('pointerdown', function (e) {
    dragging = { x: e.clientX - view.x, y: e.clientY - view.y };
    svg.setPointerCapture(e.pointerId);
  });
  svg.addEventListener('pointermove', function (e) {
    if (!dragging) return;
    view.x = e.clientX - dragging.x;
    view.y = e.clientY - dragging.y;
    apply();
  });
  svg.addEventListener('pointerup', function () { dragging = null; });
  svg.addEventListener('wheel', function (e) {
    e.preventDefault();
    var box = svg.getBoundingClientRect();
    var px = e.clientX - box.left, py = e.clientY - box.top;
    var next = Math.min(24, Math.max(0.5, view.k * (e.deltaY < 0 ? 1.15 : 1 / 1.15)));
    view.x = px - (px - view.x) * (next / view.k);
    view.y = py - (py - view.y) * (next / view.k);
    view.k = next;
    apply();
  }, { passive: false });

  document.querySelectorAll('.pv-controls button[data-layer]').forEach(
    function (button) {
      button.addEventListener('click', function () {
        var on = button.getAttribute('aria-pressed') !== 'true';
        button.setAttribute('aria-pressed', on ? 'true' : 'false');
        document
          .querySelectorAll('[data-layer="' + button.dataset.layer + '"]')
          .forEach(function (layer) { layer.hidden = !on; });
      });
    });

  var reset = document.querySelector('.pv-controls button[data-reset]');
  if (reset) reset.addEventListener('click', function () {
    view = { x: 0, y: 0, k: 1 }; apply();
  });

  var panel = document.querySelector('.pv-panel');
  function show(info) {
    if (!panel) return;
    panel.innerHTML =
      '<h3>' + info.name + '</h3><dl>' +
      '<dt>type</dt><dd>' + info.kind + '</dd>' +
      '<dt>position</dt><dd>' + info.position + '</dd>' +
      (info.notams ? '<dt>NOTAM</dt><dd>' + info.notams + ' in force</dd>' : '') +
      '</dl>';
  }
  document.querySelectorAll('.pv-point').forEach(function (node) {
    function open() { show(JSON.parse(node.dataset.info)); }
    node.addEventListener('click', open);
    node.addEventListener('focus', open);
  });
})();
"""


def plan_html(view: PlanView) -> str:
    """The plan view as a whole page: pan, zoom, layers, click for detail."""
    notes: list[str] = []
    if view.unplottable:
        notes.append(
            "<p><strong>"
            + _escape(f"{len(view.unplottable)} named and not drawn.</strong>")
            + " "
            + _escape(
                ", ".join(view.unplottable)
                + " have no held position. They are listed rather than placed: "
                "a waypoint at a guessed position is the one output worse than "
                "no drawing at all."
            )
            + "</p>"
        )
    partial = [a for a in view.airways if a.gaps]
    if partial:
        notes.append(
            "<p>"
            + _escape(
                ", ".join(f"{a.route} ({a.gaps})" for a in partial)
                + " are drawn through fewer points than they publish. An airway "
                "missing points is a different shape from the published one."
            )
            + "</p>"
        )
    total = view.route_distance_nm
    if total is not None:
        notes.append(
            "<p>"
            + _escape(
                f"Route length {total:.0f} NM, computed from the published "
                "coordinates. Not a published distance: a State's segment "
                "figure is what a route is planned and charged on."
            )
            + "</p>"
        )
    notes.append(
        "<p>"
        + _escape(
            "This is a plan view of published positions, not a chart. It "
            "carries no terrain, no airspace boundaries and no obstacles — "
            "everything absent is absent because nobody read it, not because "
            "it is not there."
        )
        + "</p>"
    )

    return (
        "<style>"
        + PLAN_VIEW_CSS
        + "\nbody{margin:0;padding:24px;font:14px/1.6 system-ui,sans-serif}"
        ".pv-notes{max-width:72ch;font-size:13.5px}</style>\n"
        '<div class="pv-wrap">\n'
        '<div class="pv-controls">'
        '<button type="button" data-layer="route" aria-pressed="true">Route</button>'
        '<button type="button" data-layer="airways" aria-pressed="true">Airways</button>'
        '<button type="button" data-layer="points" aria-pressed="true">Points</button>'
        '<button type="button" data-reset="1">Reset</button>'
        "</div>\n"
        '<div class="pv-panel"><h3>Click a point</h3>'
        "<dl><dt>&nbsp;</dt><dd>its published position appears here</dd></dl></div>\n"
        + plan_svg(view)
        + "\n</div>\n"
        + '<div class="pv-notes">'
        + "\n".join(notes)
        + "</div>\n<script>"
        + _PLAN_VIEW_JS
        + "</script>"
    )

"""Chart studies — and the plate that should have moved and did not.

Most of what a chart holds is a picture, and reading pictures is not what this
platform is for. What it *is* for is the relationship between two published
things: the AIP said the threshold moved, and the approach plate serving that
threshold carries last cycle's revision date. One of those two is wrong, and
nobody is looking.

That asymmetry is the whole module. A chart set is not analysed here — it is
*reconciled* against the AIP changes that ought to have driven it, in both
directions:

**Expected and not amended.** The AIP moved a runway's declared distances and
the aerodrome diagram did not follow. Either the chart is stale, or the AIP
change was not real. Either way somebody flies on one of them.

**Amended and not expected.** A plate changed and nothing we hold explains it.
That is not a clean bill of health — it is the far more likely reading that we
missed the AIP change behind it, and the chart is the only evidence we have
that it happened.

The second is the more valuable of the two, and it is the one a system built
around "watch the AIP" would never produce. A chart index is a second, partly
independent witness to what a State published, and disagreement between two
witnesses is information.

Why this is not computer vision
-------------------------------
The FAA publishes the complete US terminal procedure set every 28 days with an
explicit list of which charts changed. Where a State does that, chart change
detection is reading a file. Where it does not, a chart is *registered* with
its revision and nothing is claimed about its content — and that is a coverage
state, not a defect. Nothing here infers a minimum, a gradient or a hot spot
from an image; every figure arrives cited, through a manifest, exactly as
:mod:`aeropub.acap` requires for aircraft.

What the rules encode, and what they do not
-------------------------------------------
:data:`IMPLICATIONS` maps an AIP attribute to the chart kinds it drives. That
is reasoning about how aeronautical publication fits together — the same kind
of thing as the Annex 14 tables in :mod:`aeropub.aircraft` — and it belongs in
source. What never belongs in source is a figure: no minimum, no gradient, no
revision date, no chart identifier is written here.

The rules are deliberately generous. A missed implication is a discrepancy
nobody reports; a surplus one is a question somebody answers in a minute. When
the two failures cost that differently, the tuning goes one way.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from enum import Enum
from pathlib import Path
from typing import Iterable, Mapping

from aeropub.changes import Change, ChangeKind
from aeropub.entities import aerodrome_of, normalise, scope_of
from aeropub.facts import SourceRef
from aeropub.manifest import (
    ManifestError,
    document_source,
    read_manifest,
    sub_source,
)
from aeropub.suitability import Assessment

__all__ = [
    "Chart",
    "chart_kinds_for",
    "ChartKind",
    "Constraint",
    "ConstraintKind",
    "ChartRegister",
    "ChartReview",
    "compare_minima",
    "connecting_procedures",
    "Discrepancy",
    "Expectation",
    "FEET_PER_NM",
    "GradientFinding",
    "expectations",
    "IMPLICATIONS",
    "load_procedures",
    "load_register",
    "MinimaChange",
    "Minimum",
    "Procedure",
    "ProcedureKind",
    "ProcedureLeg",
    "ProcedureLink",
    "procedure_template",
    "register_template",
    "Requirement",
    "STANDARD_CLIMB_GRADIENT",
    "STANDARD_DESCENT_GRADIENT",
    "review_charts",
    "screen_climb",
    "screen_descent",
    "serves",
    "Unexplained",
    "Usability",
    "usable",
]

#: The parser identity written into citations read from a chart index.
CHART_PARSER_ID = "aeropub.charts"


class ChartKind(str, Enum):
    """The chart types the plan tracks, by what each one governs."""

    IAP = "iap"
    """Instrument approach — ILS, RNP, RNAV, VOR, NDB, visual, circling. The
    one that carries minima."""

    SID = "sid"
    """Standard instrument departure. Climb gradients and obstacle notes."""

    STAR = "star"
    """Standard arrival. Speed and level windows, and the energy traps in
    them."""

    ODP = "odp"
    """Obstacle departure procedure or textual departure — the alternative
    departure requirement a SID does not cover."""

    AERODROME_DIAGRAM = "aerodrome_diagram"
    """Layout, hot spots, stand numbering, restricted taxiways."""

    TERMINAL_AREA = "terminal_area"
    """Terminal airspace structure, entry and exit points."""

    MVA = "mva"
    """Radar minimum vectoring or radar vectoring altitudes — the safety net
    behind every vector, and the chart crews least often hold."""

    OBSTACLE_TYPE_A = "obstacle_type_a"
    """Take-off obstacle data. Feeds the obstacle studies."""

    ENROUTE = "enroute"
    """Route structure, MEA and MORA bands, reporting points."""

    NOISE = "noise"
    """Noise abatement procedure charts."""

    @property
    def carries_minima(self) -> bool:
        return self is ChartKind.IAP

    @property
    def is_runway_specific(self) -> bool:
        """Whether one of these serves a particular runway.

        An aerodrome diagram covers every runway at once, so a threshold move
        implicates the single diagram; an ILS plate serves one, so the same
        move implicates only the plates for that runway. Getting this wrong in
        the permissive direction produces noise, and in the strict direction
        produces a missed stale plate.
        """
        return self in (ChartKind.IAP, ChartKind.SID, ChartKind.STAR, ChartKind.ODP)


#: Which chart kinds an AIP attribute drives.
#:
#: Read as: if this attribute changed, a chart of these kinds serving the same
#: place should carry a new revision. Keyed on fragments of the attribute
#: names the fact store already uses, and matched by containment rather than by
#: a strict prefix: ``threshold`` has to reach ``displaced_threshold_m`` as well
#: as ``threshold_elevation_ft``, and a prefix rule would miss the first. One
#: entry therefore covers a family of related attributes instead of a list that
#: goes stale the first time a parser learns a new field name.
IMPLICATIONS: Mapping[str, tuple[ChartKind, ...]] = {
    # Runway geometry. Moves the picture and every procedure built on it.
    "threshold": (
        ChartKind.AERODROME_DIAGRAM,
        ChartKind.IAP,
        ChartKind.SID,
        ChartKind.ODP,
    ),
    "declared_": (ChartKind.AERODROME_DIAGRAM, ChartKind.SID, ChartKind.ODP),
    "runway_length": (ChartKind.AERODROME_DIAGRAM, ChartKind.SID, ChartKind.ODP),
    "runway_width": (ChartKind.AERODROME_DIAGRAM,),
    "runway_designator": (
        ChartKind.AERODROME_DIAGRAM,
        ChartKind.IAP,
        ChartKind.SID,
        ChartKind.STAR,
        ChartKind.ODP,
    ),
    "displaced_threshold": (ChartKind.AERODROME_DIAGRAM, ChartKind.IAP),
    "runway_slope": (ChartKind.AERODROME_DIAGRAM,),
    # Lighting and marking. Approach lighting drives minima directly.
    "approach_lighting": (ChartKind.IAP,),
    "runway_lighting": (ChartKind.IAP, ChartKind.AERODROME_DIAGRAM),
    "papi": (ChartKind.IAP,),
    "vasis": (ChartKind.IAP,),
    # Navigation aids. An identifier or frequency change invalidates the plate
    # that tunes it, and nothing about the plate says so.
    "navaid": (ChartKind.IAP, ChartKind.SID, ChartKind.STAR, ChartKind.ENROUTE),
    "ils": (ChartKind.IAP,),
    "glidepath": (ChartKind.IAP,),
    "frequency": (ChartKind.IAP, ChartKind.SID, ChartKind.STAR),
    # Obstacles. The reason a gradient is what it is.
    "obstacle": (ChartKind.OBSTACLE_TYPE_A, ChartKind.SID, ChartKind.ODP),
    "climb_gradient": (ChartKind.SID, ChartKind.ODP),
    # Airspace and altimetry. Every plate carries the transition.
    "transition_altitude": (
        ChartKind.IAP,
        ChartKind.SID,
        ChartKind.STAR,
        ChartKind.TERMINAL_AREA,
    ),
    "transition_level": (ChartKind.IAP, ChartKind.SID, ChartKind.STAR),
    "airspace": (ChartKind.TERMINAL_AREA, ChartKind.MVA, ChartKind.ENROUTE),
    "minimum_vectoring": (ChartKind.MVA,),
    # Movement area. The diagram is the only place these live.
    "taxiway": (ChartKind.AERODROME_DIAGRAM,),
    "hot_spot": (ChartKind.AERODROME_DIAGRAM,),
    "stand": (ChartKind.AERODROME_DIAGRAM,),
    "apron": (ChartKind.AERODROME_DIAGRAM,),
    # Noise.
    "noise": (ChartKind.NOISE, ChartKind.SID),
}


def chart_kinds_for(attribute: str) -> tuple[ChartKind, ...]:
    """Which chart kinds this attribute drives, by longest matching fragment.

    A rule matches where its key appears anywhere in the attribute name, so
    ``threshold`` reaches ``displaced_threshold_m`` as well as
    ``threshold_elevation_ft``. Longest match wins, so a specific rule beats a
    general one: ``runway_designator`` is answered by its own entry and not by
    ``runway_width``.

    An attribute matching nothing returns empty, which is honest — it means we
    have not decided what that attribute implies, not that it implies nothing.
    A caller reporting "no charts affected" from an empty result would be
    stating a conclusion the rules never reached, which is why
    :class:`ChartReview` carries those changes as ``unmapped`` instead.
    """
    key = str(attribute).strip().lower()
    matched: tuple[ChartKind, ...] = ()
    best = -1
    for prefix, kinds in IMPLICATIONS.items():
        if prefix in key and len(prefix) > best:
            matched, best = kinds, len(prefix)
    return matched


@dataclass(frozen=True, slots=True)
class Minimum:
    """One approach minimum, for one aircraft category.

    Both altitude fields are optional and both may be absent: a plate can
    publish a DA for a precision approach and an MDA for the circling line on
    the same chart, and neither is derivable from the other.
    """

    category: str
    """Aircraft approach category — A to E. Not the operator's choice: it
    follows from the aeroplane's threshold speed."""

    source: SourceRef
    da_ft: float | None = None
    mda_ft: float | None = None
    rvr_m: float | None = None
    vis_m: float | None = None
    line: str = ""
    """Which minima line this is — "S-ILS 34L", "CIRCLING", "LNAV/VNAV". One
    chart carries several, and comparing across them is meaningless."""

    def __post_init__(self) -> None:
        category = str(self.category).strip().upper()
        object.__setattr__(self, "category", category)
        if category not in {"A", "B", "C", "D", "E"}:
            raise ValueError(
                f"approach category {self.category!r} is not one of A-E. The "
                "category follows from threshold speed and is not free text."
            )
        if not isinstance(self.source, SourceRef):
            raise TypeError("Minimum.source must be a SourceRef")

    @property
    def altitude_ft(self) -> float | None:
        """The decision or minimum descent altitude, whichever this line has."""
        return self.da_ft if self.da_ft is not None else self.mda_ft

    def describe(self) -> str:
        parts = [f"CAT {self.category}"]
        if self.line:
            parts.append(self.line)
        if self.da_ft is not None:
            parts.append(f"DA {self.da_ft:.0f} ft")
        elif self.mda_ft is not None:
            parts.append(f"MDA {self.mda_ft:.0f} ft")
        if self.rvr_m is not None:
            parts.append(f"RVR {self.rvr_m:.0f} m")
        elif self.vis_m is not None:
            parts.append(f"VIS {self.vis_m:.0f} m")
        return "  ".join(parts)


@dataclass(frozen=True, slots=True)
class Requirement:
    """Equipment or an approval a chart demands before it may be flown.

    Held as the code the chart prints, not as an interpretation of it. "RNP AR
    APCH" is what the plate says; whether a given operator holds that approval
    is a fact about the operator, and the two are never merged.
    """

    code: str
    source: SourceRef
    detail: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "code", normalise(self.code))
        if not self.code:
            raise ValueError("Requirement.code must be a non-empty string")
        if not isinstance(self.source, SourceRef):
            raise TypeError("Requirement.source must be a SourceRef")


@dataclass(frozen=True, slots=True)
class Chart:
    """One published chart, as the State's own index describes it.

    Nothing here is read from the image. ``revision`` and ``amended`` come from
    the State's chart index or change list; ``minima`` and ``requirements``
    come from a manifest somebody filled in from the plate. A chart with an
    empty ``minima`` is a chart nobody has transcribed, which is a coverage
    state and not a chart without minima.
    """

    aerodrome: str
    kind: ChartKind
    identifier: str
    source: SourceRef
    revision: str = ""
    cycle: str = ""
    """The AIRAC cycle this revision is effective for, as the State states it."""

    runways: tuple[str, ...] = ()
    """Which runways this chart serves. Empty on an aerodrome-wide chart, and
    that is the same as "all" for implication purposes — an aerodrome diagram
    with no runway list still follows a threshold move."""

    amended: bool = False
    """Whether the State's own change list says this revision differs from the
    last. Not inferred from the revision string: States format those however
    they like, and a string comparison across two formats produces a change
    every cycle."""

    minima: tuple[Minimum, ...] = ()
    requirements: tuple[Requirement, ...] = ()
    note: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "aerodrome", normalise(self.aerodrome))
        object.__setattr__(self, "identifier", " ".join(str(self.identifier).split()))
        object.__setattr__(
            self, "runways", tuple(normalise(r) for r in self.runways)
        )
        if not self.aerodrome:
            raise ValueError("Chart.aerodrome must be a non-empty string")
        if not self.identifier:
            raise ValueError(
                "Chart.identifier must be a non-empty string — the name the "
                "State prints on the plate, so a finding can say which one."
            )
        if not isinstance(self.kind, ChartKind):
            raise TypeError("Chart.kind must be a ChartKind")
        if not isinstance(self.source, SourceRef):
            raise TypeError("Chart.source must be a SourceRef")

    @property
    def key(self) -> str:
        return f"{self.aerodrome}/{self.kind.value}/{self.identifier}"

    @property
    def label(self) -> str:
        """How a finding names this chart.

        The kind is prefixed only where the State's own name does not already
        carry it, so an aerodrome diagram does not print as "AERODROME_DIAGRAM
        AERODROME DIAGRAM".
        """
        words = set(self.kind.value.upper().split("_"))
        if words <= set(self.identifier.upper().replace("/", " ").split()):
            return self.identifier
        return f"{self.kind.value.upper()} {self.identifier}"

    @property
    def is_transcribed(self) -> bool:
        """Whether anybody has read the plate itself.

        A chart that is registered and not transcribed is watched for revision
        and silent on content. Reporting it as having no minima would be a
        statement nobody made.
        """
        return bool(self.minima or self.requirements)

    def minima_for(self, category: str) -> tuple[Minimum, ...]:
        wanted = str(category).strip().upper()
        return tuple(m for m in self.minima if m.category == wanted)

    def lowest(self, category: str, *, line: str = "") -> Minimum | None:
        """The lowest published minimum for this category, on one line.

        Restricted to a single minima line by default because comparing a
        precision DA against a circling MDA answers a question nobody asked.
        Passing no line compares within whatever lines are held, which is only
        meaningful on a chart carrying one.
        """
        candidates = [
            m
            for m in self.minima_for(category)
            if (not line or m.line == line) and m.altitude_ft is not None
        ]
        if not candidates:
            return None
        return min(candidates, key=lambda m: m.altitude_ft)


def serves(chart: Chart, entity: str) -> bool:
    """Whether this chart governs the place an entity names.

    An aerodrome-level change reaches every chart at that aerodrome. A
    runway-level change reaches the runway-specific charts serving it, plus
    every aerodrome-wide chart — a threshold move belongs on the diagram
    whether or not the diagram lists runways.
    """
    where = aerodrome_of(entity)
    if where is None or where != chart.aerodrome:
        return False
    scope = scope_of(entity)
    if scope is None:
        return True
    if not chart.kind.is_runway_specific:
        return True
    if not chart.runways:
        # A runway-specific chart that does not say which runway it serves is
        # included rather than excluded. The cost of asking is a question; the
        # cost of skipping is a stale plate nobody looked at.
        return True
    return any(scope == runway or runway in scope for runway in chart.runways)


@dataclass(frozen=True, slots=True)
class Expectation:
    """A chart that should carry a new revision, and why.

    Holds the change rather than a summary of it, so the finding can show the
    published values that drove it instead of asserting that something moved.
    """

    chart: Chart
    change: Change

    @property
    def entity(self) -> str:
        return self.change.entity

    @property
    def attribute(self) -> str:
        return self.change.attribute

    def describe(self) -> str:
        return (
            f"{self.chart.identifier} — {self.entity} {self.change.describe()}"
        )


@dataclass(frozen=True, slots=True)
class Discrepancy:
    """An expected amendment that did not arrive.

    One of two things is true and the finding does not pretend to know which:
    the chart is stale, or the AIP change was not what it appeared to be. Both
    are worth somebody's attention, and only one of them is the State's fault.
    """

    expectation: Expectation

    @property
    def chart(self) -> Chart:
        return self.expectation.chart

    @property
    def change(self) -> Change:
        return self.expectation.change

    def describe(self) -> str:
        chart = self.chart
        revision = f" (revision {chart.revision})" if chart.revision else ""
        return (
            f"{chart.label}{revision} was not amended, and "
            f"{self.change.entity} {self.change.describe()}"
        )


@dataclass(frozen=True, slots=True)
class Unexplained:
    """A chart that was amended with nothing held to account for it.

    The more likely reading is not that the State amended a plate for no
    reason. It is that an AIP change happened and we do not hold it — which
    makes the chart index a second witness catching a gap in the first.
    """

    chart: Chart

    def describe(self) -> str:
        revision = f" revision {self.chart.revision}" if self.chart.revision else ""
        return (
            f"{self.chart.label}{revision} was amended, and nothing held "
            "explains it"
        )


@dataclass(frozen=True, slots=True)
class ChartRegister:
    """Every chart held for an aerodrome, at one cycle."""

    aerodrome: str
    cycle: str = ""
    charts: tuple[Chart, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "aerodrome", normalise(self.aerodrome))
        foreign = sorted(
            {c.aerodrome for c in self.charts if c.aerodrome != self.aerodrome}
        )
        if foreign:
            raise ValueError(
                f"a register for {self.aerodrome} holds charts for "
                f"{', '.join(foreign)}. One register, one aerodrome — "
                "otherwise a reconciliation silently spans two."
            )
        keys = [c.key for c in self.charts]
        twice = sorted({k for k in keys if keys.count(k) > 1})
        if twice:
            raise ValueError(
                f"{self.aerodrome} lists {', '.join(twice)} more than once. "
                "Two revisions of one chart answer differently depending on "
                "which is read first."
            )

    def __len__(self) -> int:
        return len(self.charts)

    def __iter__(self):
        return iter(self.charts)

    def of_kind(self, kind: ChartKind) -> tuple[Chart, ...]:
        return tuple(c for c in self.charts if c.kind is kind)

    @property
    def amended(self) -> tuple[Chart, ...]:
        return tuple(c for c in self.charts if c.amended)

    @property
    def transcribed(self) -> tuple[Chart, ...]:
        return tuple(c for c in self.charts if c.is_transcribed)

    def coverage(self) -> tuple[int, int]:
        """How many charts are transcribed, out of how many are registered."""
        return (len(self.transcribed), len(self.charts))


def expectations(
    changes: Iterable[Change], register: ChartRegister
) -> tuple[Expectation, ...]:
    """Which charts each AIP change implies should have been amended.

    A change with no mapped implication produces nothing, and the review
    reports how many of those there were rather than letting them vanish: an
    attribute whose chart consequences nobody has decided is a gap in the
    rules, not evidence that the charts are fine.
    """
    found: list[Expectation] = []
    seen: set[tuple[str, str, str]] = set()
    for change in changes:
        kinds = chart_kinds_for(change.attribute)
        if not kinds:
            continue
        for chart in register:
            if chart.kind not in kinds:
                continue
            if not serves(chart, change.entity):
                continue
            mark = (chart.key, change.entity, change.attribute)
            if mark in seen:
                continue
            seen.add(mark)
            found.append(Expectation(chart=chart, change=change))
    return tuple(found)


@dataclass(frozen=True, slots=True)
class ChartReview:
    """The chart set reconciled against the AIP changes that should drive it."""

    aerodrome: str
    on: date
    register: ChartRegister
    expected: tuple[Expectation, ...] = ()
    discrepancies: tuple[Discrepancy, ...] = ()
    unexplained: tuple[Unexplained, ...] = ()
    unmapped: tuple[Change, ...] = ()
    """Changes whose chart consequences the rules have not decided. Reported so
    a quiet review can be told apart from an incomplete one."""

    from_cycle: str = ""
    to_cycle: str = ""

    @property
    def is_conclusive(self) -> bool:
        """Whether this review covers every change it was given.

        False while any change is unmapped or any chart is untranscribed: the
        reconciliation is only as complete as the rules and the transcription
        behind it, and "no discrepancies" from a partial review reads exactly
        like "no discrepancies" from a complete one.
        """
        transcribed, registered = self.register.coverage()
        return not self.unmapped and transcribed == registered

    @property
    def has_findings(self) -> bool:
        return bool(self.discrepancies or self.unexplained)

    def render(self) -> str:
        transcribed, registered = self.register.coverage()
        lines = [
            f"CHART REVIEW — {self.aerodrome}",
            f"effective {self.on:%Y-%m-%d}"
            + (
                f"  ·  {self.from_cycle} → {self.to_cycle}"
                if self.from_cycle and self.to_cycle
                else ""
            ),
            "",
            f"{registered} charts registered  ·  {transcribed} transcribed  ·  "
            f"{len(self.register.amended)} amended this cycle",
            f"{len(self.expected)} amendments expected  ·  "
            f"{len(self.discrepancies)} not made  ·  "
            f"{len(self.unexplained)} unexplained"
            + ("" if self.is_conclusive else "  ·  NOT CONCLUSIVE"),
        ]
        if not registered:
            lines += [
                "",
                "No charts registered for this aerodrome. Nothing was "
                "reconciled — that is a coverage gap, not a clean chart set.",
            ]
            return "\n".join(lines)

        if self.discrepancies:
            lines += [
                "",
                "EXPECTED AND NOT AMENDED — the AIP moved and the plate did not",
            ]
            for finding in self.discrepancies:
                lines.append(f"  {finding.describe()}")
            lines += [
                "",
                "  Either the chart is stale or the AIP change was not what it "
                "appeared to be.",
                "  Both are worth an hour; only one of them is the State's.",
            ]
        if self.unexplained:
            lines += [
                "",
                "AMENDED AND NOT EXPECTED — a plate moved with nothing behind it",
            ]
            for finding in self.unexplained:
                lines.append(f"  {finding.describe()}")
            lines += [
                "",
                "  The likelier reading is not that the State amended these for "
                "no reason. It is",
                "  that an AIP change happened and we do not hold it.",
            ]
        if self.unmapped:
            lines += [
                "",
                f"NOT RECONCILED — {len(self.unmapped)} changes whose chart "
                "consequences are undecided",
            ]
            for change in self.unmapped:
                lines.append(f"  {change.entity}: {change.describe()}")
        if transcribed < registered:
            lines += [
                "",
                f"{registered - transcribed} of {registered} charts are watched "
                "for revision and not read.",
                "Their minima, gradients and notes are unknown here, and unknown "
                "is not unchanged.",
            ]
        return "\n".join(lines)


def review_charts(
    register: ChartRegister,
    changes: Iterable[Change],
    *,
    on: date,
    from_cycle: str = "",
    to_cycle: str = "",
) -> ChartReview:
    """Reconcile a chart set against the AIP changes of the same period.

    Both directions, because they catch different failures. An expected
    amendment that did not arrive says the chart set is behind the AIP; an
    amendment nothing explains says our AIP holdings are behind the State.
    """
    listed = list(changes)
    expected = expectations(listed, register)

    discrepancies = tuple(
        Discrepancy(expectation=e) for e in expected if not e.chart.amended
    )
    explained = {e.chart.key for e in expected}
    unexplained = tuple(
        Unexplained(chart=c) for c in register.amended if c.key not in explained
    )
    unmapped = tuple(c for c in listed if not chart_kinds_for(c.attribute))

    return ChartReview(
        aerodrome=register.aerodrome,
        on=on,
        register=register,
        expected=expected,
        discrepancies=discrepancies,
        unexplained=unexplained,
        unmapped=unmapped,
        from_cycle=from_cycle,
        to_cycle=to_cycle,
    )


# --------------------------------------------------------------------------
# Minima, cycle over cycle
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MinimaChange:
    """One minima line moving between two revisions of one chart.

    Compared within a line and a category, never across them. A precision DA
    and a circling MDA on the same plate are different numbers about different
    procedures, and subtracting one from the other produces a figure that looks
    like a finding and means nothing.
    """

    identifier: str
    category: str
    line: str
    before: Minimum | None = None
    after: Minimum | None = None

    def __post_init__(self) -> None:
        if self.before is None and self.after is None:
            raise ValueError("a minima change needs a before or an after")

    @property
    def kind(self) -> ChangeKind:
        if self.before is None:
            return ChangeKind.ADDED
        if self.after is None:
            return ChangeKind.REMOVED
        return ChangeKind.MODIFIED

    @property
    def delta_ft(self) -> float | None:
        """How far the minimum moved. ``None`` where either side is absent.

        A line that was withdrawn has no delta, and reporting one as though the
        minimum went to zero would be the most dangerous arithmetic in this
        module.
        """
        if self.before is None or self.after is None:
            return None
        if self.before.altitude_ft is None or self.after.altitude_ft is None:
            return None
        return self.after.altitude_ft - self.before.altitude_ft

    @property
    def is_adverse(self) -> bool:
        """Whether this moved against the operator.

        A raised minimum, a raised RVR, or a line withdrawn entirely. A
        withdrawal counts: losing the LPV line costs the same dispatch the
        raised DA would have, and it is easier to miss.
        """
        if self.kind is ChangeKind.REMOVED:
            return True
        delta = self.delta_ft
        if delta is not None and delta > 0:
            return True
        if self.before is not None and self.after is not None:
            before_vis = self.before.rvr_m if self.before.rvr_m is not None else self.before.vis_m
            after_vis = self.after.rvr_m if self.after.rvr_m is not None else self.after.vis_m
            if before_vis is not None and after_vis is not None:
                return after_vis > before_vis
        return False

    def describe(self) -> str:
        where = f"{self.identifier} CAT {self.category}"
        if self.line:
            where += f" {self.line}"
        if self.kind is ChangeKind.ADDED:
            return f"{where}: published as {self.after.describe()}"
        if self.kind is ChangeKind.REMOVED:
            return f"{where}: withdrawn (was {self.before.describe()})"
        delta = self.delta_ft
        moved = f" ({delta:+.0f} ft)" if delta is not None else ""
        return f"{where}: {self.before.describe()} → {self.after.describe()}{moved}"


def compare_minima(before: Chart, after: Chart) -> tuple[MinimaChange, ...]:
    """Every minima line that moved between two revisions of one chart.

    Refuses to compare two different charts. Two plates for different runways
    have unrelated minima, and a diff between them would report every line as
    changed while telling nobody anything.
    """
    if before.key != after.key:
        raise ValueError(
            f"{before.key} and {after.key} are different charts. Minima are "
            "compared between revisions of one plate, never across two."
        )

    def index(chart: Chart) -> dict[tuple[str, str], Minimum]:
        return {(m.category, m.line): m for m in chart.minima}

    old, new = index(before), index(after)
    changes: list[MinimaChange] = []
    for key in sorted(set(old) | set(new)):
        category, line = key
        was, now = old.get(key), new.get(key)
        if was is not None and now is not None:
            same = (
                was.da_ft == now.da_ft
                and was.mda_ft == now.mda_ft
                and was.rvr_m == now.rvr_m
                and was.vis_m == now.vis_m
            )
            if same:
                continue
        changes.append(
            MinimaChange(
                identifier=after.identifier,
                category=category,
                line=line,
                before=was,
                after=now,
            )
        )
    return tuple(changes)


# --------------------------------------------------------------------------
# Capability matching
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Usability:
    """Whether an operator may fly this chart on what they hold.

    Three answers, not two. An approval they do not hold is *not suitable*; a
    chart nobody has transcribed is *unknown*, because its requirements have
    never been read and an unread plate demanding RNP AR looks exactly like one
    demanding nothing.
    """

    chart: Chart
    holds: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()

    @property
    def assessment(self) -> Assessment:
        if not self.chart.is_transcribed:
            return Assessment.UNKNOWN
        if self.missing:
            return Assessment.NOT_SUITABLE
        return Assessment.SUITABLE

    @property
    def is_usable(self) -> bool:
        return self.assessment is Assessment.SUITABLE

    def describe(self) -> str:
        if self.assessment is Assessment.UNKNOWN:
            return (
                f"{self.chart.identifier}: requirements not read — "
                "unknown, not unrestricted"
            )
        if self.missing:
            return (
                f"{self.chart.identifier}: needs "
                + ", ".join(self.missing)
                + " — not held"
            )
        return f"{self.chart.identifier}: every stated requirement held"


def usable(chart: Chart, approvals: Iterable[str]) -> Usability:
    """Match one chart's stated requirements against what an operator holds.

    ``approvals`` is the operator's own list, in the codes the charts print.
    Matching is exact on the normalised code: an approval that nearly matches
    is not an approval, and a fuzzy match here would clear an RNP AR approach
    for an operator holding plain RNP APCH.
    """
    held = {normalise(a) for a in approvals if str(a).strip()}
    needed = [r.code for r in chart.requirements]
    return Usability(
        chart=chart,
        holds=tuple(sorted(c for c in needed if c in held)),
        missing=tuple(sorted(c for c in needed if c not in held)),
    )


# --------------------------------------------------------------------------
# Reading a chart register
# --------------------------------------------------------------------------


def _kind(value: object, *, where: str) -> ChartKind:
    try:
        return ChartKind(str(value).strip().lower())
    except ValueError:
        raise ManifestError(
            f"{where}: kind must be one of "
            f"{', '.join(k.value for k in ChartKind)}. What a chart governs "
            "decides which AIP changes should have amended it, so there is no "
            "safe default."
        ) from None


def _number(value: object, *, where: str, field: str) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ManifestError(
            f"{where}: {field} {value!r} is not a number. A minimum that "
            "cannot be read is left unread, never rounded into place."
        ) from None


def load_register(path: Path | str) -> ChartRegister:
    """Read one chart index, with every chart cited to it.

    One document, one aerodrome — the same rule the aircraft manifests keep.
    A State's chart index for one aerodrome is one publication, and a file
    spanning two would emit both cited to whichever the header named.

    ``amended`` comes from the State's own change list and is never inferred
    from the revision string: States format those however they like, and a
    string comparison across two formats reports a change every cycle.
    """
    path = Path(path)
    manifest = read_manifest(path)

    document = document_source(
        manifest.get("source"),
        base=path.parent,
        where=f"{path}: source",
        parser_id=CHART_PARSER_ID,
    )

    aerodrome = str(manifest.get("aerodrome", "")).strip()
    if not aerodrome:
        raise ManifestError(
            f"{path}: aerodrome is required — a chart index with no aerodrome "
            "names plates at every aerodrome that has them."
        )

    rows = manifest.get("charts", [])
    if not isinstance(rows, list):
        raise ManifestError(f"{path}: charts must be a list")

    charts: list[Chart] = []
    for index, row in enumerate(rows):
        where = f"{path}: charts[{index}]"
        if not isinstance(row, Mapping):
            raise ManifestError(f"{where}: must be an object")
        identifier = str(row.get("identifier", "")).strip()
        if not identifier:
            raise ManifestError(
                f"{where}: identifier is required — the name the State prints "
                "on the plate, so a finding can say which one."
            )
        locator = str(row.get("locator", "")).strip()
        if not locator:
            raise ManifestError(
                f"{where}: {identifier} needs a locator — where in the index "
                "this entry was read."
            )
        chart_source = sub_source(document, locator)

        minima: list[Minimum] = []
        listed = row.get("minima", [])
        if not isinstance(listed, list):
            raise ManifestError(f"{where}: minima must be a list")
        for position, entry in enumerate(listed):
            place = f"{where}: minima[{position}]"
            if not isinstance(entry, Mapping):
                raise ManifestError(f"{place}: must be an object")
            try:
                minima.append(
                    Minimum(
                        category=str(entry.get("category", "")),
                        source=sub_source(
                            document,
                            str(entry.get("locator", "")).strip() or locator,
                        ),
                        da_ft=_number(entry.get("da_ft"), where=place, field="da_ft"),
                        mda_ft=_number(
                            entry.get("mda_ft"), where=place, field="mda_ft"
                        ),
                        rvr_m=_number(entry.get("rvr_m"), where=place, field="rvr_m"),
                        vis_m=_number(entry.get("vis_m"), where=place, field="vis_m"),
                        line=str(entry.get("line", "")).strip(),
                    )
                )
            except ValueError as error:
                raise ManifestError(f"{place}: {error}") from None

        requirements: list[Requirement] = []
        listed = row.get("requirements", [])
        if not isinstance(listed, list):
            raise ManifestError(f"{where}: requirements must be a list")
        for position, entry in enumerate(listed):
            place = f"{where}: requirements[{position}]"
            if isinstance(entry, str):
                entry = {"code": entry}
            if not isinstance(entry, Mapping):
                raise ManifestError(f"{place}: must be a code or an object")
            try:
                requirements.append(
                    Requirement(
                        code=str(entry.get("code", "")),
                        source=sub_source(
                            document,
                            str(entry.get("locator", "")).strip() or locator,
                        ),
                        detail=str(entry.get("detail", "")),
                    )
                )
            except ValueError as error:
                raise ManifestError(f"{place}: {error}") from None

        try:
            charts.append(
                Chart(
                    aerodrome=aerodrome,
                    kind=_kind(row.get("kind"), where=where),
                    identifier=identifier,
                    source=chart_source,
                    revision=str(row.get("revision", "")).strip(),
                    cycle=str(row.get("cycle", manifest.get("cycle", ""))).strip(),
                    runways=tuple(str(r) for r in row.get("runways", [])),
                    amended=bool(row.get("amended", False)),
                    minima=tuple(minima),
                    requirements=tuple(requirements),
                    note=str(row.get("note", "")),
                )
            )
        except ValueError as error:
            raise ManifestError(f"{where}: {error}") from None

    try:
        return ChartRegister(
            aerodrome=aerodrome,
            cycle=str(manifest.get("cycle", "")).strip(),
            charts=tuple(charts),
        )
    except ValueError as error:
        raise ManifestError(f"{path}: {error}") from None


_REGISTER_TEMPLATE = {
    "source": {
        "source_id": "",
        "document": "",
        "document_path": "",
        "retrieved_at": "",
        "published_at": "",
        "original_url": "",
    },
    "aerodrome": "",
    "cycle": "",
    "charts": [
        {
            "kind": "iap",
            "identifier": "",
            "revision": "",
            "runways": [],
            "amended": False,
            "locator": "",
            "minima": [
                {
                    "category": "C",
                    "line": "",
                    "da_ft": None,
                    "mda_ft": None,
                    "rvr_m": None,
                    "locator": "",
                }
            ],
            "requirements": [],
        }
    ],
}


def register_template() -> str:
    """A blank chart index.

    ``amended`` is the State's own word, from its change list — not a guess
    from the revision string. ``minima`` and ``requirements`` may be left out
    entirely: a chart registered and not transcribed is watched for revision
    and silent on content, which is a coverage state the review reports rather
    than a chart with no minima.
    """
    return json.dumps(_REGISTER_TEMPLATE, indent=2)


# --------------------------------------------------------------------------
# Procedures — the structure behind the plate
# --------------------------------------------------------------------------
#
# A SID or STAR is not only a picture. It is a sequence of fixes with level
# and speed constraints between them, and those constraints are arithmetic:
# how much height must be lost between two fixes, over how far, is a division
# anybody can do and almost nobody does before the aeroplane is already high.
#
# That is what makes this worth building. An energy trap is not a subtle
# judgement — it is a published descent requirement steeper than an aeroplane
# can achieve at idle, sitting in plain sight on a chart that thousands of
# crews fly. The screen below finds them by doing the division.


#: Feet lost per nautical mile at a nominal three-degree idle descent. Three
#: degrees is the design gradient of nearly every published descent path and
#: works out at almost exactly 318 ft/NM; 300 is the round figure planners use
#: and sits on the conservative side of it, so a trap reported here is a trap
#: on a more generous assumption too.
STANDARD_DESCENT_GRADIENT = 300.0

#: Feet gained per nautical mile at the standard 3.3% procedure design
#: gradient. The same figure :mod:`aeropub.obstacles` works in, expressed the
#: way a chart prints a climb requirement.
STANDARD_CLIMB_GRADIENT = 200.0

#: One nautical mile in feet, for turning a ft/NM gradient into the percentage
#: a departure chart prints alongside it.
FEET_PER_NM = 6076.115


class ConstraintKind(str, Enum):
    """How a published level constraint binds.

    Four kinds, because they bind in different directions and confusing them
    is how a screen produces a finding that is exactly backwards. An
    at-or-above constraint costs a descending aeroplane nothing and everything
    to a climbing one.
    """

    AT = "at"
    AT_OR_ABOVE = "at_or_above"
    AT_OR_BELOW = "at_or_below"
    WINDOW = "window"
    """Both, at once — "between 8000 and 10000". The one that traps, because
    it removes the crew's discretion in both directions."""


@dataclass(frozen=True, slots=True)
class Constraint:
    """One published level constraint at one fix."""

    kind: ConstraintKind
    source: SourceRef
    lower_ft: float | None = None
    upper_ft: float | None = None
    speed_kt: float | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, ConstraintKind):
            raise TypeError("Constraint.kind must be a ConstraintKind")
        if not isinstance(self.source, SourceRef):
            raise TypeError("Constraint.source must be a SourceRef")
        if self.kind is ConstraintKind.AT and self.lower_ft is None:
            raise ValueError(
                "an AT constraint needs the altitude it is at. A constraint "
                "with no altitude constrains nothing and would silently drop "
                "out of every screen."
            )
        if self.kind is ConstraintKind.WINDOW and (
            self.lower_ft is None or self.upper_ft is None
        ):
            raise ValueError(
                "a WINDOW constraint needs both bounds — half a window is one "
                "of the other three kinds, and reading it as a window would "
                "invent a limit the State did not publish"
            )
        if (
            self.lower_ft is not None
            and self.upper_ft is not None
            and self.lower_ft > self.upper_ft
        ):
            raise ValueError(
                f"lower {self.lower_ft} is above upper {self.upper_ft}"
            )

    @property
    def ceiling_ft(self) -> float | None:
        """The highest a compliant aeroplane may be here.

        ``None`` where nothing caps it — an at-or-above constraint places no
        ceiling, and treating its altitude as one would manufacture a descent
        requirement that does not exist.
        """
        if self.kind is ConstraintKind.AT:
            return self.lower_ft
        if self.kind in (ConstraintKind.AT_OR_BELOW, ConstraintKind.WINDOW):
            return self.upper_ft
        return None

    @property
    def floor_ft(self) -> float | None:
        """The lowest a compliant aeroplane may be here."""
        if self.kind is ConstraintKind.AT:
            return self.lower_ft
        if self.kind in (ConstraintKind.AT_OR_ABOVE, ConstraintKind.WINDOW):
            return self.lower_ft
        return None

    def describe(self) -> str:
        if self.kind is ConstraintKind.AT:
            text = f"at {self.lower_ft:.0f}"
        elif self.kind is ConstraintKind.AT_OR_ABOVE:
            text = f"at or above {self.lower_ft:.0f}"
        elif self.kind is ConstraintKind.AT_OR_BELOW:
            text = f"at or below {self.upper_ft:.0f}"
        else:
            text = f"between {self.lower_ft:.0f} and {self.upper_ft:.0f}"
        if self.speed_kt is not None:
            text += f", {self.speed_kt:.0f} kt"
        return text


@dataclass(frozen=True, slots=True)
class ProcedureLeg:
    """One fix on a procedure, and how far it is from the one before."""

    fix: str
    distance_nm: float | None = None
    """Track distance from the previous fix. ``None`` on the first leg, and on
    any leg the chart does not print — an unmeasured leg cannot be screened,
    and guessing a distance would produce a gradient nobody published."""

    constraint: Constraint | None = None
    track_deg: float | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "fix", normalise(self.fix))
        if not self.fix:
            raise ValueError("ProcedureLeg.fix must be a non-empty string")


class ProcedureKind(str, Enum):
    SID = "sid"
    STAR = "star"
    APPROACH = "approach"
    ODP = "odp"

    @property
    def is_departure(self) -> bool:
        return self in (ProcedureKind.SID, ProcedureKind.ODP)


@dataclass(frozen=True, slots=True)
class Procedure:
    """A departure or arrival procedure as a sequence of constrained fixes.

    Held beside the :class:`Chart` rather than inside it, because a chart is
    what the State publishes and this is what somebody transcribed from it.
    A procedure with no legs is one nobody has read, and it screens to nothing
    rather than to nothing wrong.
    """

    aerodrome: str
    kind: ProcedureKind
    designator: str
    source: SourceRef
    runways: tuple[str, ...] = ()
    legs: tuple[ProcedureLeg, ...] = ()
    climb_gradient_ft_per_nm: float | None = None
    """A gradient the chart prints as a requirement, above the standard. Held
    as published; the screen below also derives one from the constraints,
    and the two are reported separately because they are different claims."""

    transition: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "aerodrome", normalise(self.aerodrome))
        object.__setattr__(self, "designator", normalise(self.designator))
        object.__setattr__(self, "runways", tuple(normalise(r) for r in self.runways))
        if not self.aerodrome:
            raise ValueError("Procedure.aerodrome must be a non-empty string")
        if not self.designator:
            raise ValueError("Procedure.designator must be a non-empty string")
        if not isinstance(self.kind, ProcedureKind):
            raise TypeError("Procedure.kind must be a ProcedureKind")
        if not isinstance(self.source, SourceRef):
            raise TypeError("Procedure.source must be a SourceRef")

    @property
    def fixes(self) -> tuple[str, ...]:
        return tuple(leg.fix for leg in self.legs)

    @property
    def is_transcribed(self) -> bool:
        return bool(self.legs)

    @property
    def terminates_at(self) -> str:
        """The last fix — where a SID hands over to the en-route structure."""
        return self.legs[-1].fix if self.legs else ""

    @property
    def begins_at(self) -> str:
        """The first fix — where a STAR takes over from the en-route structure."""
        return self.legs[0].fix if self.legs else ""

    def joins(self, point: str) -> bool:
        """Whether this procedure connects the aerodrome to that point.

        A SID joins at its terminal fix and a STAR at its first: that is the
        direction each is flown, and matching either end against either would
        connect an arrival to a departure point.
        """
        wanted = normalise(point)
        if not wanted:
            return False
        if self.kind.is_departure:
            return self.terminates_at == wanted
        return self.begins_at == wanted


@dataclass(frozen=True, slots=True)
class GradientFinding:
    """A published pair of constraints demanding a gradient.

    One finding type for both directions, because the arithmetic is the same
    and only the sign differs: a descent requirement is height to lose over
    distance, a climb requirement is height to gain over distance, and both
    are compared against what an aeroplane can actually do.
    """

    procedure: str
    start: str
    end: str
    from_ft: float
    to_ft: float
    distance_nm: float
    capability_ft_per_nm: float
    descending: bool

    @property
    def required_ft_per_nm(self) -> float:
        return abs(self.to_ft - self.from_ft) / self.distance_nm

    @property
    def exceeds_by(self) -> float:
        return self.required_ft_per_nm - self.capability_ft_per_nm

    @property
    def is_trap(self) -> bool:
        return self.required_ft_per_nm > self.capability_ft_per_nm

    @property
    def required_percent(self) -> float:
        """The same gradient the way a departure chart prints it."""
        return self.required_ft_per_nm / FEET_PER_NM * 100.0

    def describe(self) -> str:
        verb = "lose" if self.descending else "gain"
        return (
            f"{self.procedure} {self.start} to {self.end}: "
            f"{verb} {abs(self.to_ft - self.from_ft):.0f} ft in "
            f"{self.distance_nm:.1f} NM — {self.required_ft_per_nm:.0f} ft/NM "
            f"against {self.capability_ft_per_nm:.0f} available"
            + (f", short by {self.exceeds_by:.0f}" if self.is_trap else "")
        )


def screen_descent(
    procedure: Procedure,
    *,
    capability_ft_per_nm: float = STANDARD_DESCENT_GRADIENT,
) -> tuple[GradientFinding, ...]:
    """Find published constraint pairs an aeroplane cannot descend between.

    The energy trap the plan names, done by arithmetic rather than by feel.
    Between two fixes, the binding pair is the *lowest* the aeroplane may be at
    the earlier one and the *highest* it may be at the later: a floor followed
    by a ceiling. Anything else leaves the crew room, and only this pair takes
    it away.

    Unmeasured legs produce nothing and cannot: a gradient over an unknown
    distance is not a smaller finding, it is arithmetic with a hole in it.
    """
    findings: list[GradientFinding] = []
    held: list[tuple[str, Constraint, float]] = []
    running = 0.0
    for index, leg in enumerate(procedure.legs):
        if index and leg.distance_nm is None:
            # The chain of distances is broken here; nothing after this fix
            # can be measured back to anything before it.
            held = []
            running = 0.0
            if leg.constraint is not None:
                held.append((leg.fix, leg.constraint, running))
            continue
        running += leg.distance_nm or 0.0
        if leg.constraint is None:
            continue
        ceiling = leg.constraint.ceiling_ft
        if ceiling is not None:
            for fix, earlier, at in held:
                floor = earlier.floor_ft
                distance = running - at
                if floor is None or distance <= 0 or floor <= ceiling:
                    continue
                findings.append(
                    GradientFinding(
                        procedure=procedure.designator,
                        start=fix,
                        end=leg.fix,
                        from_ft=floor,
                        to_ft=ceiling,
                        distance_nm=distance,
                        capability_ft_per_nm=capability_ft_per_nm,
                        descending=True,
                    )
                )
        held.append((leg.fix, leg.constraint, running))
    return tuple(f for f in findings if f.is_trap)


def screen_climb(
    procedure: Procedure,
    *,
    capability_ft_per_nm: float = STANDARD_CLIMB_GRADIENT,
) -> tuple[GradientFinding, ...]:
    """Find published constraint pairs demanding more climb than is standard.

    The mirror of the descent screen, and the reason a departure needs one: a
    crossing restriction that quietly requires more than the standard 3.3% is
    a performance limitation nobody filed a gradient note about, and it binds
    exactly when an engine has failed and the margin was already spent.
    """
    findings: list[GradientFinding] = []
    held: list[tuple[str, Constraint, float]] = []
    running = 0.0
    for index, leg in enumerate(procedure.legs):
        if index and leg.distance_nm is None:
            held = []
            running = 0.0
            if leg.constraint is not None:
                held.append((leg.fix, leg.constraint, running))
            continue
        running += leg.distance_nm or 0.0
        if leg.constraint is None:
            continue
        floor = leg.constraint.floor_ft
        if floor is not None:
            for fix, earlier, at in held:
                ceiling = earlier.ceiling_ft
                distance = running - at
                if ceiling is None or distance <= 0 or ceiling >= floor:
                    continue
                findings.append(
                    GradientFinding(
                        procedure=procedure.designator,
                        start=fix,
                        end=leg.fix,
                        from_ft=ceiling,
                        to_ft=floor,
                        distance_nm=distance,
                        capability_ft_per_nm=capability_ft_per_nm,
                        descending=False,
                    )
                )
        held.append((leg.fix, leg.constraint, running))
    return tuple(f for f in findings if f.is_trap)


@dataclass(frozen=True, slots=True)
class ProcedureLink:
    """A procedure that joins this aerodrome to the filed route.

    The departure and arrival half of a route profile. Which SID gets an
    aeroplane from the runway to the first point of the filed route is a
    question with a published answer, and answering it is what turns a route
    string into a flyable profile.
    """

    procedure: Procedure
    point: str

    @property
    def is_departure(self) -> bool:
        return self.procedure.kind.is_departure

    def describe(self) -> str:
        end = "to" if self.is_departure else "from"
        return (
            f"{self.procedure.designator} ({self.procedure.kind.value.upper()}) "
            f"{end} {self.point}"
        )


def connecting_procedures(
    procedures: Iterable[Procedure],
    *,
    aerodrome: str,
    point: str,
    departure: bool,
) -> tuple[ProcedureLink, ...]:
    """Which held procedures join this aerodrome to this point.

    Empty is a coverage answer and not a statement that none exists. Almost
    every aerodrome has a published way onto the airway structure; an empty
    result here means we have not read the procedures, and the caller must
    render it that way.
    """
    where = normalise(aerodrome)
    wanted = normalise(point)
    return tuple(
        ProcedureLink(procedure=p, point=wanted)
        for p in procedures
        if p.aerodrome == where
        and p.kind.is_departure is departure
        and p.joins(wanted)
    )


def _constraint(
    block: object, *, document: SourceRef, where: str, locator: str
) -> Constraint | None:
    """Read one level constraint, or ``None`` where the fix carries none.

    A fix with no constraint is not a fix constrained to nothing: it is a fix
    the crew may cross at any level the procedure otherwise allows, and it
    correctly drops out of both gradient screens.
    """
    if block is None:
        return None
    if not isinstance(block, Mapping):
        raise ManifestError(f"{where}: constraint must be an object")
    try:
        kind = ConstraintKind(str(block.get("kind", "")).strip().lower())
    except ValueError:
        raise ManifestError(
            f"{where}: constraint.kind must be one of "
            f"{', '.join(k.value for k in ConstraintKind)}. Which direction a "
            "constraint binds in decides whether it traps a descent or a "
            "climb, so there is no safe default."
        ) from None
    try:
        return Constraint(
            kind=kind,
            source=sub_source(
                document, str(block.get("locator", "")).strip() or locator
            ),
            lower_ft=_number(block.get("lower_ft"), where=where, field="lower_ft"),
            upper_ft=_number(block.get("upper_ft"), where=where, field="upper_ft"),
            speed_kt=_number(block.get("speed_kt"), where=where, field="speed_kt"),
        )
    except ValueError as error:
        raise ManifestError(f"{where}: {error}") from None


def load_procedures(path: Path | str) -> tuple[Procedure, ...]:
    """Read procedures transcribed from one State's plates.

    One document, one citation, as everywhere else. The legs are the point: a
    procedure with no legs is one nobody has read, and it screens to nothing
    rather than to nothing wrong.
    """
    path = Path(path)
    manifest = read_manifest(path)
    document = document_source(
        manifest.get("source"),
        base=path.parent,
        where=f"{path}: source",
        parser_id=CHART_PARSER_ID,
    )
    aerodrome = str(manifest.get("aerodrome", "")).strip()
    if not aerodrome:
        raise ManifestError(
            f"{path}: aerodrome is required — a procedure with no aerodrome "
            "names a departure from everywhere."
        )

    rows = manifest.get("procedures", [])
    if not isinstance(rows, list):
        raise ManifestError(f"{path}: procedures must be a list")

    found: list[Procedure] = []
    for index, row in enumerate(rows):
        where = f"{path}: procedures[{index}]"
        if not isinstance(row, Mapping):
            raise ManifestError(f"{where}: must be an object")
        designator = str(row.get("designator", "")).strip()
        if not designator:
            raise ManifestError(f"{where}: designator is required")
        locator = str(row.get("locator", "")).strip()
        if not locator:
            raise ManifestError(
                f"{where}: {designator} needs a locator — which plate, and "
                "where on it, this was transcribed from."
            )
        try:
            kind = ProcedureKind(str(row.get("kind", "")).strip().lower())
        except ValueError:
            raise ManifestError(
                f"{where}: kind must be one of "
                f"{', '.join(k.value for k in ProcedureKind)}"
            ) from None

        listed = row.get("legs", [])
        if not isinstance(listed, list):
            raise ManifestError(f"{where}: legs must be a list of fixes")
        legs: list[ProcedureLeg] = []
        for position, entry in enumerate(listed):
            place = f"{where}: legs[{position}]"
            if isinstance(entry, str):
                entry = {"fix": entry}
            if not isinstance(entry, Mapping):
                raise ManifestError(f"{place}: must be a fix or an object")
            try:
                legs.append(
                    ProcedureLeg(
                        fix=str(entry.get("fix", "")),
                        distance_nm=_number(
                            entry.get("distance_nm"), where=place, field="distance_nm"
                        ),
                        constraint=_constraint(
                            entry.get("constraint"),
                            document=document,
                            where=place,
                            locator=locator,
                        ),
                        track_deg=_number(
                            entry.get("track_deg"), where=place, field="track_deg"
                        ),
                    )
                )
            except ValueError as error:
                raise ManifestError(f"{place}: {error}") from None

        try:
            found.append(
                Procedure(
                    aerodrome=aerodrome,
                    kind=kind,
                    designator=designator,
                    source=sub_source(document, locator),
                    runways=tuple(str(r) for r in row.get("runways", [])),
                    legs=tuple(legs),
                    climb_gradient_ft_per_nm=_number(
                        row.get("climb_gradient_ft_per_nm"),
                        where=where,
                        field="climb_gradient_ft_per_nm",
                    ),
                    transition=str(row.get("transition", "")).strip(),
                )
            )
        except (ValueError, TypeError) as error:
            raise ManifestError(f"{where}: {error}") from None
    return tuple(found)


_PROCEDURE_TEMPLATE = {
    "source": {
        "source_id": "",
        "document": "",
        "document_path": "",
        "retrieved_at": "",
        "published_at": "",
        "original_url": "",
    },
    "aerodrome": "",
    "procedures": [
        {
            "kind": "sid",
            "designator": "",
            "runways": [],
            "transition": "",
            "climb_gradient_ft_per_nm": None,
            "locator": "",
            "legs": [
                {
                    "fix": "",
                    "distance_nm": None,
                    "track_deg": None,
                    "constraint": {
                        "kind": "at_or_above",
                        "lower_ft": None,
                        "upper_ft": None,
                        "speed_kt": None,
                    },
                }
            ],
        }
    ],
}


def procedure_template() -> str:
    """A blank procedure transcription.

    ``distance_nm`` is the track distance from the *previous* fix, and leaving
    it out is not a smaller entry: an unmeasured leg cannot be screened, and a
    guessed distance produces a gradient nobody published. ``constraint`` may
    be omitted entirely where a fix carries none — which is different from a
    fix constrained to nothing, and drops out of the screens correctly.
    """
    return json.dumps(_PROCEDURE_TEMPLATE, indent=2)

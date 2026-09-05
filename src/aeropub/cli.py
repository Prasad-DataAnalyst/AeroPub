"""The command line — one way in to everything the platform assembles.

Twenty modules and no way to run them was the gap this closes. Every command
below is a thin wrapper: it opens the store, calls the same function the API
calls, and prints. Nothing is computed here, so there is no path by which the
terminal and the JSON can disagree about the same aerodrome — which is exactly
when the two get compared.

The store is the source
-----------------------
Every command reads the SQLite fact store, defaulting to ``aeropub.db`` and
overridable with ``--store`` or ``AEROPUB_STORE``. A store that does not exist
is created empty, and an empty store is reported as an empty store: every AD 2
section printed as a gap, and a line saying nothing has been loaded. It is
never reported as an aerodrome with nothing to say.

Exit codes
----------
``0`` the command produced its document. ``1`` the command ran and the answer
is adverse — a suitability check that failed, a validation finding that cannot
be true. ``2`` the command could not run: a bad argument, an unreadable
manifest. A caller scripting against this can tell "it said no" from "it could
not tell", which are different answers and must not share a code.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path

from aeropub.acap import ManifestError, load_aircraft, merge, template
from aeropub.aip import AipCoverage
from aeropub.airspace import AirspaceStructure, airspace_template, load_airspace
from aeropub.hazards import HazardRegister, hazard_template, load_hazards
from aeropub.diagram import (
    diagram_for,
    network_for,
    network_html,
    route_html,
)
from aeropub.planview import plan_html, plan_view
from aeropub.gnss import (
    ApproachCapability,
    GnssRegister,
    gnss_template,
    load_gnss,
)
from aeropub.navaids import NavaidRegister, load_navaids, navaid_template
from aeropub.planning import (
    PlanningRegister,
    load_planning,
    planning_template,
)
from aeropub.airac import AiracCycle, current_cycle, cycle_for, cycles_in_year
from aeropub.entities import aerodrome_of
from aeropub.api import dumps
from aeropub.bulletin import between_cycles
from aeropub.changes import diff_cycles
from aeropub.charts import (
    load_procedures,
    load_register,
    procedure_template,
    register_template,
    review_charts,
)
from aeropub.credentials import CredentialStore, describe as describe_secret
from aeropub.currency import Currency, assess_currency
from aeropub.dossier import build
from aeropub.fleet import (
    fleet_of,
    library_template,
    load_library,
    merge_libraries,
    screen as screen_fleet,
)
from aeropub.horizon import DEFAULT_DAYS, horizon
from aeropub.ingest import load_facts
from aeropub.ingest import template as fact_template
from aeropub.lenses import LENSES, Audience, view
from aeropub.notam_register import NotamRegister
from aeropub.operator import Exposure, assess_operator, load_profile
from aeropub.operator import profile_template
from aeropub.quality import assess_quality
from aeropub.render import render_dossier
from aeropub.retrospect import blind_spots, retrospect
from aeropub.ats import (
    AtsStructure,
    load_ats_structure,
    parse_route_string,
    structure_template,
)
from aeropub.route import Jurisdiction, Route, build_route_dossier
from aeropub.store import open_store
from aeropub.sweep import sweep as sweep_network
from aeropub.suitability import Assessment, assess_suitability
from aeropub.enroute import chart_for, chart_html
from aeropub.checklist import (
    checklist_template,
    holdings_template,
    load_checklist,
    load_holdings,
    reconcile,
)
from aeropub.trip import Trip, assess_trip

__all__ = ["DEFAULT_STORE", "main"]

DEFAULT_STORE = "aeropub.db"

OK = 0
ADVERSE = 1
CANNOT_RUN = 2


def current_cycle_for(day: date) -> str:
    return AiracCycle.containing(day).identifier


def _store_path(args: argparse.Namespace) -> Path:
    return Path(args.store or os.environ.get("AEROPUB_STORE") or DEFAULT_STORE)


def _moment(args: argparse.Namespace) -> datetime:
    if getattr(args, "as_at", None):
        parsed = datetime.fromisoformat(args.as_at.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    return datetime.now(timezone.utc)


def _day(text: str | None, fallback: date) -> date:
    return date.fromisoformat(text) if text else fallback


def _plural(count: int, singular: str, plural: str | None = None) -> str:
    """A count and its noun, agreeing. Output nobody has to forgive."""
    return f"{count} {singular if count == 1 else (plural or singular + 's')}"


def _emit(document, args: argparse.Namespace, rendered: str) -> None:
    """Print the JSON payload or the printable form, never a third thing."""
    if getattr(args, "json", False):
        print(dumps(document, indent=2))
    else:
        print(rendered)


def _emptiness_note(store, path: Path) -> str:
    """What an empty store says for itself.

    An aerodrome with no facts and an aerodrome nobody has loaded print the
    same document, and only this line separates them. It is not decoration.
    """
    if len(store):
        return ""
    return (
        f"\n!! {path} holds no facts. Everything above is a coverage gap, not a "
        "quiet aerodrome.\n"
        "   Load a source before reading anything into this: nothing has been "
        "read for any aerodrome."
    )


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


def _cmd_airac(args: argparse.Namespace) -> int:
    if args.year:
        for cycle in cycles_in_year(args.year):
            print(f"{cycle.identifier}   {cycle.effective_date}")
        return OK
    on = _day(args.on, date.today())
    cycle = cycle_for(on) if args.on else current_cycle()
    print(f"AIRAC {cycle.identifier}")
    print(f"  effective              {cycle.effective_date}")
    print(f"  expires                {cycle.expiry_date}")
    print(f"  next cycle             {cycle.next.effective_date}  ({cycle.next.identifier})")
    print()
    # The deadlines an AIS office actually works to. Annex 15 and PANS-AIM put
    # the obligation on the publishing State, and every one of them has already
    # passed by the time a cycle is effective — which is the point of printing
    # them: a source that arrives after these dates arrived late, and lateness
    # is a measurable property of a State, not an impression.
    print("  ICAO distribution deadlines for this cycle")
    print(f"    distribution         {cycle.distribution_deadline}")
    print(f"    major change         {cycle.major_change_deadline}")
    print(f"    in recipients' hands {cycle.recipient_deadline}")
    return OK


def _cmd_dossier(args: argparse.Namespace) -> int:
    path = _store_path(args)
    store = open_store(path)
    try:
        moment = _moment(args)
        document = build(
            args.aerodrome,
            facts=store,
            coverage=AipCoverage(),
            register=NotamRegister(),
            as_at=moment,
            on=_day(args.on, moment.date()),
        )
        if args.html:
            Path(args.html).write_text(render_dossier(document), encoding="utf-8")
            print(f"wrote {args.html}")
            return OK
        _emit(document, args, document.render() + _emptiness_note(store, path))
        return OK
    finally:
        store.close()


def _cmd_bulletin(args: argparse.Namespace) -> int:
    path = _store_path(args)
    store = open_store(path)
    try:
        document = between_cycles(
            store,
            args.aerodrome,
            AiracCycle.from_identifier(args.from_cycle),
            AiracCycle.from_identifier(args.to_cycle),
        )
        _emit(document, args, document.render() + _emptiness_note(store, path))
        return OK
    finally:
        store.close()


def _cmd_charts(args: argparse.Namespace) -> int:
    """Reconcile a chart index against the AIP changes that should drive it.

    Both directions. An expected amendment that did not arrive says the chart
    set is behind the AIP; an amendment nothing explains says our AIP holdings
    are behind the State, and that one is the more likely of the two.
    """
    if args.template:
        print(register_template())
        return OK
    if not args.register:
        print(
            "give --register, or --template for a blank chart index",
            file=sys.stderr,
        )
        return CANNOT_RUN

    register = load_register(args.register)
    if args.aerodrome and aerodrome_of(args.aerodrome) != register.aerodrome:
        print(
            f"{args.register} is the chart index for {register.aerodrome}, "
            f"not {args.aerodrome}. Reconciling one aerodrome's plates against "
            "another's changes would find nothing and say so confidently.",
            file=sys.stderr,
        )
        return CANNOT_RUN

    path = _store_path(args)
    store = open_store(path)
    try:
        # Defaulting to the cycle now in force and the one before it. A
        # planner asking whether the plates followed this cycle's changes
        # should not have to name two cycle identifiers to be asked the
        # question they already had in mind.
        to_cycle = (
            AiracCycle.from_identifier(args.to_cycle)
            if args.to_cycle
            else current_cycle()
        )
        from_cycle = (
            AiracCycle.from_identifier(args.from_cycle)
            if args.from_cycle
            else to_cycle.previous
        )
        document = review_charts(
            register,
            diff_cycles(store, from_cycle, to_cycle, entity=register.aerodrome),
            on=to_cycle.effective_date,
            from_cycle=from_cycle.identifier,
            to_cycle=to_cycle.identifier,
        )
        _emit(document, args, document.render() + _emptiness_note(store, path))
        return ADVERSE if document.has_findings else OK
    finally:
        store.close()


def _cmd_horizon(args: argparse.Namespace) -> int:
    path = _store_path(args)
    store = open_store(path)
    try:
        moment = _moment(args)
        document = horizon(
            store, args.aerodrome, from_date=moment.date(), days=args.days
        )
        _emit(document, args, document.render() + _emptiness_note(store, path))
        return OK
    finally:
        store.close()


def _cmd_quality(args: argparse.Namespace) -> int:
    path = _store_path(args)
    store = open_store(path)
    try:
        document = assess_quality(
            store=store, entity=args.entity, as_at=_moment(args)
        )
        _emit(document, args, document.render() + _emptiness_note(store, path))
        return OK
    finally:
        store.close()


def _cmd_lens(args: argparse.Namespace) -> int:
    path = _store_path(args)
    store = open_store(path)
    try:
        moment = _moment(args)
        dossier = build(
            args.aerodrome,
            facts=store,
            coverage=AipCoverage(),
            register=NotamRegister(),
            as_at=moment,
        )
        document = view(
            args.audience,
            args.aerodrome,
            as_at=moment,
            dossier=dossier,
            ahead=horizon(store, args.aerodrome, from_date=moment.date()),
        )
        _emit(document, args, document.render() + _emptiness_note(store, path))
        return OK
    finally:
        store.close()


def _cmd_fit(args: argparse.Namespace) -> int:
    path = _store_path(args)
    store = open_store(path)
    try:
        aircraft = merge(*(load_aircraft(m) for m in args.aircraft))
        moment = _moment(args)
        dossier = build(
            args.aerodrome,
            facts=store,
            coverage=AipCoverage(),
            register=NotamRegister(),
            as_at=moment,
            on=_day(args.on, moment.date()),
        )
        document = assess_suitability(dossier, aircraft)
        _emit(document, args, document.render() + _emptiness_note(store, path))
        # An adverse answer exits non-zero so a script can act on it. An
        # inconclusive one does not: "I could not tell" is not "no", and a
        # caller that treats them alike will act on the wrong one.
        return ADVERSE if document.overall is Assessment.NOT_SUITABLE else OK
    finally:
        store.close()


def _cmd_load(args: argparse.Namespace) -> int:
    """Read manifests into the store, or none of them.

    Every manifest is parsed before anything is written. A run that fails
    halfway would leave the store holding part of a document, and a partial AIP
    section is worse than none: the sections that did load look complete.
    """
    if args.template:
        print(fact_template())
        return OK
    if not args.manifest:
        print(
            "give one or more manifests, or --template for a blank one",
            file=sys.stderr,
        )
        return CANNOT_RUN

    batches = [(m, load_facts(m)) for m in args.manifest]
    path = _store_path(args)
    store = open_store(path)
    try:
        for name, facts in batches:
            for item in facts:
                store.add(item)
            print(f"  {len(facts):4} facts from {name}")
        print(f"{path} now holds {len(store)} facts")
        return OK
    finally:
        store.close()


def _cmd_exposure(args: argparse.Namespace) -> int:
    if args.template:
        print(profile_template())
        return OK
    if not args.profile or not args.aerodrome:
        print(
            "give an aerodrome and --profile, or --template for a blank profile",
            file=sys.stderr,
        )
        return CANNOT_RUN

    path = _store_path(args)
    store = open_store(path)
    try:
        profile = load_profile(args.profile)
        moment = _moment(args)
        dossier = build(
            args.aerodrome, facts=store, coverage=AipCoverage(),
            register=NotamRegister(), as_at=moment,
            on=_day(args.on, moment.date()),
        )
        document = assess_operator(dossier, profile)
        _emit(document, args, document.render() + _emptiness_note(store, path))
        # Only a definite adverse finding exits non-zero. UNKNOWN does not:
        # "nobody checked" and "no" are different answers.
        return ADVERSE if document.overall in (
            Exposure.CRITICAL, Exposure.HIGH
        ) else OK
    finally:
        store.close()


def _cmd_sweep(args: argparse.Namespace) -> int:
    path = _store_path(args)
    store = open_store(path)
    try:
        profile = load_profile(args.profile)
        moment = _moment(args)
        document = sweep_network(
            store, profile, as_at=moment, on=_day(args.on, moment.date()),
            days=args.days,
        )
        _emit(document, args, document.render() + _emptiness_note(store, path))
        return ADVERSE if document.overall in (
            Exposure.CRITICAL, Exposure.HIGH
        ) else OK
    finally:
        store.close()


def _cmd_currency(args: argparse.Namespace) -> int:
    """How old everything in the store is, worst first.

    Reads the aerodromes actually held rather than a watchlist, because the
    question this answers is about what we have, not what we meant to have.
    Coverage against an intended list is the registry's job.
    """
    path = _store_path(args)
    store = open_store(path)
    try:
        moment = _moment(args)
        day = _day(args.on, moment.date())
        aerodromes = sorted({aerodrome_of(e) or e for e in store.entities()})
        if not aerodromes:
            print(f"STORE — {path}")
            print(
                "\n  Empty. Nothing has been read for any aerodrome, so there "
                "is nothing whose age\n  could be reported."
            )
            return OK

        held = [assess_currency(store, a, as_of=day) for a in aerodromes]
        order = {
            Currency.NEVER_READ: 0, Currency.STALE: 1,
            Currency.AGEING: 2, Currency.CURRENT: 3,
        }
        held.sort(key=lambda c: (order[c.state], -c.cycles_behind, c.entity))
        counts = {state: sum(1 for c in held if c.state is state) for state in Currency}

        print(f"DATA CURRENCY — {path}")
        print(f"as at {day}  ·  AIRAC {current_cycle_for(day)}")
        print()
        print(
            f"{counts[Currency.CURRENT]} current  ·  "
            f"{counts[Currency.AGEING]} ageing  ·  "
            f"{counts[Currency.STALE]} stale"
        )
        print()
        for entry in held:
            if args.stale_only and entry.is_usable:
                continue
            print(f"  {entry.describe()}")
        if counts[Currency.STALE]:
            print()
            print(
                "Staleness is counted in AIRAC cycles, not days: an amendment "
                "could have landed\nin each one and nobody went back for it. A "
                "clear verdict on stale data is a claim\nabout the past."
            )
        return ADVERSE if counts[Currency.STALE] else OK
    finally:
        store.close()


#: Every secret the platform can use, what it is for, and who issues it.
#: Recorded here so an operator can see the whole list without reading source.
KNOWN_CREDENTIALS: tuple[tuple[str, str], ...] = (
    ("AEROPUB_FAA_CLIENT_ID",
     "FAA NMS-API OAuth2 client id — issued by the FAA / CGI with registration"),
    ("AEROPUB_FAA_CLIENT_SECRET",
     "FAA NMS-API OAuth2 client secret — issued with the client id"),
)


def _cmd_credentials(args: argparse.Namespace) -> int:
    """Show, set or remove a stored secret. Never prints a value.

    Values are read from a prompt rather than an argument, deliberately: a
    secret passed on the command line lands in the shell history and in the
    process list, where anyone on the machine can read it.
    """
    store = CredentialStore()

    if args.set:
        import getpass

        try:
            value = getpass.getpass(f"{args.set}: ")
        except (EOFError, KeyboardInterrupt):
            print("\nnothing stored", file=sys.stderr)
            return CANNOT_RUN
        try:
            written = store.set_secret(args.set, value)
        except ValueError as error:
            print(str(error), file=sys.stderr)
            return CANNOT_RUN
        print(f"stored {args.set} in {written}")
        print("  The file is owner-readable only and sits outside any "
              "repository, so it cannot be committed.")
        return OK

    if args.forget:
        if store.forget(args.forget):
            print(f"removed {args.forget} from {store.file}")
            return OK
        print(f"{args.forget} was not in {store.file}", file=sys.stderr)
        return CANNOT_RUN

    print("CREDENTIALS")
    print(f"  file: {store.file}")
    print()
    missing = 0
    for name, purpose in KNOWN_CREDENTIALS:
        held = store.get(name)
        if not held:
            missing += 1
        print(f"  {name}")
        print(f"      {describe_secret(held)}  ·  {store.where(name)}")
        print(f"      {purpose}")
    extra = [n for n in store.names() if n not in dict(KNOWN_CREDENTIALS)]
    if extra:
        print()
        print("  Also stored (not used by any connector this build knows):")
        for name in extra:
            print(f"      {name}")
    if missing:
        print()
        print(f"{missing} not set. Set one with: aeropub credentials --set NAME")
        print("In a hosted environment prefer environment variables — they "
              "survive restarts\nand never touch a disk this project can read.")
    return ADVERSE if missing else OK


def _cmd_retrospect(args: argparse.Namespace) -> int:
    """What was knowable at a past moment, beside what is held now."""
    path = _store_path(args)
    store = open_store(path)
    try:
        known = datetime.fromisoformat(args.known.replace("Z", "+00:00"))
        if known.tzinfo is None:
            known = known.replace(tzinfo=timezone.utc)
        document = retrospect(
            store, args.aerodrome,
            on=_day(args.on, known.date()),
            as_known_at=known,
        )
        _emit(document, args, document.render() + _emptiness_note(store, path))
        # A record that moved is the finding, not a failure. Exit adverse so a
        # scripted audit can act on it.
        return OK if document.is_faithful else ADVERSE
    finally:
        store.close()


def _cmd_blindspots(args: argparse.Namespace) -> int:
    """How late our own collection was. A measure of us, not of the State."""
    path = _store_path(args)
    store = open_store(path)
    try:
        entities = sorted({aerodrome_of(e) or e for e in store.entities()})
        if args.aerodrome:
            entities = [args.aerodrome.strip().upper()]
        print(f"COLLECTION BLINDNESS - {path}")
        print(
            "How long each change was operationally in force before we held it."
        )
        print(
            "Values predating our watching an entity are excluded: that is "
            "onboarding, not lateness."
        )
        print()
        worst_overall = 0.0
        for entity in entities:
            measured = blind_spots(store, entity, through=_moment(args))
            counts = measured.summary()
            if not counts["facts"]:
                continue
            mark = "" if not counts["late"] else (
                f"  {counts['late']} late, worst {counts['worst_hours']:g}h"
            )
            print(f"  {entity:12} {counts['facts']} values{mark}")
            for arrival in measured.late:
                print(f"      {arrival.describe()}")
            worst_overall = max(worst_overall, float(counts["worst_hours"]))
        if worst_overall:
            print()
            print(
                f"Worst blind window across the store: {worst_overall:g} hours. "
                "Every report for that\nentity in that window was confidently "
                "wrong and said nothing to suggest it."
            )
            return ADVERSE
        print("  Nothing arrived late in what is held.")
        return OK
    finally:
        store.close()


def _cmd_trip(args: argparse.Namespace) -> int:
    """Assess one flight, for its own date.

    Everything is given on the command line rather than in a profile file. A
    flight department asking about Thursday should not have to write a network
    definition first; that is the whole point of a trip being the lighter
    entity.
    """
    path = _store_path(args)
    store = open_store(path)
    try:
        aircraft = merge(*(load_aircraft(m) for m in args.aircraft))
        document = assess_trip(
            store,
            Trip(
                reference=args.reference,
                aircraft=aircraft,
                on=date.fromisoformat(args.on),
                departure=args.departure,
                destination=args.destination,
                alternates=tuple(args.alternate or ()),
                takeoff_alternate=args.takeoff_alternate,
                enroute_alternates=tuple(args.enroute or ()),
                operator=args.operator or "",
            ),
            as_at=_moment(args),
        )
        _emit(document, args, document.render() + _emptiness_note(store, path))
        return ADVERSE if document.overall in (
            Exposure.CRITICAL, Exposure.HIGH
        ) else OK
    finally:
        store.close()


def _cmd_fleet(args: argparse.Namespace) -> int:
    """The fleet library: who operates what, and whether we can check it.

    Three questions, one command, because they are the same lookup at
    different depths — what the library covers, what one operator flies, and
    which of their types can use an aerodrome.
    """
    if args.template:
        print(library_template())
        return OK
    if not args.library:
        print(
            "give --library (repeatable), or --template for a blank library",
            file=sys.stderr,
        )
        return CANNOT_RUN

    library = merge_libraries(*(load_library(path) for path in args.library))

    if not args.operator:
        # No operator named: report the library's own coverage. Worst first,
        # because the research is the point and the wins are not.
        rows = library.coverage_report()
        print(
            f"FLEET LIBRARY — {_plural(len(library), 'operator')}  ·  "
            f"{_plural(len(rows), 'type')}"
        )
        print(
            f"{_plural(len(library.registrations), 'registration')}  ·  "
            f"{_plural(len(library.references), 'bibliography entry', 'bibliography entries')}"
        )
        print()
        for designator, coverage in rows:
            print(f"  {designator:<6} {coverage.value.upper()}")
        ranked = library.ranked_by_fleet_size(args.top)
        if ranked:
            print()
            print("BY TAILS HELD — as the library holds them, not as they fly")
            for record in ranked:
                print(
                    f"  {record.icao:<5} {record.fleet_size:>4} tails  "
                    f"{record.segment.value:<11} {record.name}"
                )
        return OK

    resolved = fleet_of(library, args.operator)
    if not args.aerodrome:
        _emit(resolved, args, resolved.render())
        return OK

    path = _store_path(args)
    store = open_store(path)
    try:
        moment = _moment(args)
        dossier = build(
            args.aerodrome, facts=store, coverage=AipCoverage(),
            register=NotamRegister(), as_at=moment,
            on=_day(args.on, moment.date()),
        )
        document = screen_fleet(
            library, args.operator, dossier, designators=args.type or None
        )
        _emit(document, args, document.render() + _emptiness_note(store, path))
        # A definite failure for any type is adverse. Unchecked is not: the
        # command could not answer for those, and it did not answer no.
        return ADVERSE if document.not_suitable else OK
    finally:
        store.close()


def _write_profile(document, destination: str) -> None:
    """Draw the vertical profile beside the printed dossier.

    Only where a route was filed: without one there is no sequence of legs to
    stand boxes on, and an empty drawing would look like a route with nothing
    in it rather than a question nobody asked.
    """
    if document.expansion is None:
        print(
            "no --route was given, so there is no profile to draw",
            file=sys.stderr,
        )
        return
    drawing = diagram_for(
        document.expansion,
        planned_ft=document.route.planned_level_ft,
        regions=[j.designator for j in document.route.crosses],
        unread_regions=(
            document.airspace.unread_regions if document.airspace else ()
        ),
        title=document.route.label,
        notams=document.enroute_notams,
    )
    Path(destination).write_text(route_html(drawing), encoding="utf-8")
    print(f"profile written to {destination}", file=sys.stderr)


def _write_plan(document, structure, navaids, destination: str) -> None:
    """Draw the plan view from every position that has been read.

    Positions come from ENR 4 — the significant points in the route structure
    and the aids in the navaid register. A name with no held position is not
    placed anywhere; it is listed under the drawing, which is the whole
    discipline of the view.
    """
    positions: dict[str, object] = {}
    if structure is not None:
        for point in structure.points:
            if point.position is not None:
                positions[point.designator] = point.position
    if navaids is not None:
        for aid in navaids:
            if aid.position is not None:
                positions.setdefault(aid.ident, aid.position)

    drawing = plan_view(
        positions=positions,
        route_points=(
            document.expansion.route.points
            if document.expansion is not None
            else ()
        ),
        airways={
            route: structure.points_on(route)
            for route in (structure.routes if structure is not None else ())
        },
        navaids=[aid.ident for aid in (navaids or ())],
        aerodromes=[document.route.departure, document.route.destination],
        notams=document.enroute_notams,
        title=document.route.label,
    )
    Path(destination).write_text(plan_html(drawing), encoding="utf-8")
    print(f"plan view written to {destination}", file=sys.stderr)


def _cmd_route(args: argparse.Namespace) -> int:
    """Assemble everything held about one sector, and say what is missing.

    The aerodromes go through the same sweep the network report uses, so a
    verdict here is the verdict there. What this adds is the regions between
    them — and the headline is how much of the route we can speak for, not a
    risk score.
    """
    if args.structure_template:
        print(structure_template())
        return OK
    if args.procedure_template:
        print(procedure_template())
        return OK
    if args.airspace_template:
        print(airspace_template())
        return OK
    if args.hazard_template:
        print(hazard_template())
        return OK
    if args.navaid_template:
        print(navaid_template())
        return OK
    if args.gnss_template:
        print(gnss_template())
        return OK
    if args.planning_template:
        print(planning_template())
        return OK

    aircraft = merge(*(load_aircraft(path) for path in args.aircraft))
    crosses = tuple(
        Jurisdiction(designator=name) for name in (args.crosses or ())
    )
    structure = None
    if args.structure:
        loaded = [load_ats_structure(path) for path in args.structure]
        structure = AtsStructure(
            segments=tuple(s for held in loaded for s in held.segments),
            points=tuple(p for held in loaded for p in held.points),
            procedures=tuple(p for held in loaded for p in held.procedures),
        )
    procedures = tuple(
        p for path in (args.procedures or []) for p in load_procedures(path)
    )
    airspace = None
    if args.airspace:
        loaded = [load_airspace(path) for path in args.airspace]
        airspace = AirspaceStructure(
            volumes=tuple(v for held in loaded for v in held.volumes)
        )
    navaids = None
    if args.navaids:
        loaded = [load_navaids(path) for path in args.navaids]
        navaids = NavaidRegister(
            navaids=tuple(n for held in loaded for n in held.navaids)
        )
    capabilities = tuple(
        ApproachCapability(value) for value in (args.capability or ())
    )
    gnss = None
    if args.gnss:
        loaded = [load_gnss(path) for path in args.gnss]
        gnss = GnssRegister(
            services=tuple(s for held in loaded for s in held.services),
            covers=frozenset().union(*(held.covers for held in loaded)),
        )

    planning = None
    if args.planning:
        loaded = [load_planning(path) for path in args.planning]
        planning = PlanningRegister(
            rules=tuple(r for held in loaded for r in held.rules),
            covers=frozenset().union(*(held.covers for held in loaded)),
        )

    hazards = None
    if args.hazards:
        loaded = [load_hazards(path) for path in args.hazards]
        hazards = HazardRegister(
            hazards=tuple(h for held in loaded for h in held.hazards),
            clearances=tuple(c for held in loaded for c in held.clearances),
        )
    filed = (
        parse_route_string(
            args.route, departure=args.departure, destination=args.destination
        )
        if args.route
        else None
    )
    sector = Route(
        departure=args.departure,
        destination=args.destination,
        alternates=tuple(args.alternate or ()),
        takeoff_alternate=args.takeoff_alternate or "",
        enroute_alternates=tuple(args.enroute_alternate or ()),
        crosses=crosses,
        designator=aircraft.designator,
        reference=args.reference or "",
        filed=filed,
        planned_level_ft=args.level,
        holds=tuple(args.holds or ()),
    )

    path = _store_path(args)
    store = open_store(path)
    try:
        moment = _moment(args)
        document = build_route_dossier(
            store,
            sector,
            fleet=[aircraft],
            as_at=moment,
            on=_day(args.on, moment.date()),
            register=NotamRegister(),
            coverage=AipCoverage(),
            structure=structure,
            procedures=procedures,
            airspace=airspace,
            hazards=hazards,
            navaids=navaids,
            gnss=gnss,
            capabilities=capabilities,
            planning=planning,
            item18=args.item18 or "",
            slip_minutes=args.slip_minutes,
            notice_hours=args.notice_hours,
        )
        if args.profile:
            _write_profile(document, args.profile)
        if args.plan:
            _write_plan(document, structure, navaids, args.plan)
        if args.network:
            if structure is None:
                print(
                    "no --structure was given, so there is no route structure "
                    "to draw",
                    file=sys.stderr,
                )
            else:
                drawing = network_for(
                    structure,
                    closed_routes=args.closed or (),
                    notams=document.enroute_notams,
                    highlight=(
                        document.expansion.route.points
                        if document.expansion is not None
                        else ()
                    ),
                    title=f"ATS route structure — {sector.label}",
                )
                Path(args.network).write_text(
                    network_html(drawing), encoding="utf-8"
                )
                print(f"network written to {args.network}", file=sys.stderr)
        _emit(document, args, document.render() + _emptiness_note(store, path))
        # Adverse on anything above medium, or on a route we cannot speak for.
        # An inconclusive route dossier is not a pass: most of what it did not
        # cover, it did not cover because nobody has read it.
        return (
            ADVERSE
            if document.overall.rank <= Exposure.MEDIUM.rank
            or not document.is_conclusive
            else OK
        )
    finally:
        store.close()


def _cmd_store(args: argparse.Namespace) -> int:
    path = _store_path(args)
    store = open_store(path)
    try:
        entities = sorted(store.entities())
        print(f"STORE — {path}")
        print(f"  {len(store)} facts across {len(entities)} entities")
        if not entities:
            print(
                "\n  Empty. Nothing has been read for any aerodrome, and every "
                "report from this store\n  will be a coverage gap from end to end."
            )
            return OK
        for entity in entities:
            attributes = sorted(store.attributes(entity))
            print(f"    {entity:24} {len(attributes)} attributes")
            if args.verbose:
                for attribute in attributes:
                    print(f"        {attribute}")
        return OK
    finally:
        store.close()


def _cmd_enroute(args: argparse.Namespace) -> int:
    """Draw the published ATS route structure."""
    if args.template:
        print(structure_template())
        return OK
    if not args.structure:
        print(
            "give one or more ENR 3 extracts, or --template for a blank one",
            file=sys.stderr,
        )
        return CANNOT_RUN

    loaded = [load_ats_structure(path) for path in args.structure]
    structure = AtsStructure(
        segments=tuple(s for held in loaded for s in held.segments),
        points=tuple(p for held in loaded for p in held.points),
        procedures=tuple(p for held in loaded for p in held.procedures),
    )
    aids = None
    if args.navaids:
        read = [load_navaids(path) for path in args.navaids]
        aids = NavaidRegister(
            navaids=tuple(n for held in read for n in held.navaids)
        )

    chart = chart_for(
        structure,
        regions=args.region or (),
        routes=args.route or (),
        navaids=aids,
        level_ft=args.level,
        closed_routes=args.closed or (),
    )
    print(chart.render())
    if args.page:
        Path(args.page).write_text(chart_html(chart), encoding="utf-8")
        print(f"\n  written to {args.page}")
    return OK if chart.is_conclusive else ADVERSE


def _cmd_checklist(args: argparse.Namespace) -> int:
    """Reconcile a State's own checklist against what we hold."""
    if args.template:
        print(checklist_template())
        return OK
    if args.holdings_template:
        print(holdings_template())
        return OK
    if not args.checklist:
        print(
            "give a checklist extract, or --template for a blank one",
            file=sys.stderr,
        )
        return CANNOT_RUN

    held = load_checklist(args.checklist)
    if not args.holdings:
        # Reconciling against nothing would report every page the State
        # publishes as missing. A report where everything is a finding is a
        # report nobody reads, and it would be a finding about us rather than
        # about the AIP.
        print(f"{held.entity} — checklist for "
              + (f"AIRAC {held.published_for.identifier}"
                 if held.published_for else "no cycle stated"))
        print(f"  {len(held)} pages listed  ·  "
              f"{len(held.sections)} sections placed  ·  "
              f"{len(held.unplaced)} not placeable")
        print(f"  {len(held.amendments)} amendments  ·  "
              f"{len(held.supplements)} supplements in force")
        print()
        print("  Nothing was supplied to reconcile against. Give --holdings to")
        print("  compare this against what we hold; without it this is the")
        print("  State's list and nothing more.")
        for entry in held.entries:
            print(f"    {entry.describe()}")
        return OK

    coverage = load_holdings(args.holdings)
    found = reconcile(
        held, coverage, held_supplements=args.held_supplement or ()
    )
    print(found.render())
    return OK if found.is_reconciled else ADVERSE


def _cmd_aircraft(args: argparse.Namespace) -> int:
    if args.template:
        print(template())
        return OK
    if not args.manifest:
        print(
            "give one or more manifests, or --template for a blank one",
            file=sys.stderr,
        )
        return CANNOT_RUN
    aircraft = merge(*(load_aircraft(m) for m in args.manifest))
    print(aircraft.describe())
    letter = aircraft.code_letter()
    code = aircraft.reference_code()
    print(f"  reference code   {code or 'unknown — needs span and field length'}")
    print(f"  code letter      {letter or 'unknown'}")
    withheld = len(aircraft.characteristics) - len(aircraft.redistributable)
    print(f"  characteristics  {len(aircraft.characteristics)} held", end="")
    print(f", {withheld} not redistributable" if withheld else "")
    print()
    for item in aircraft.characteristics:
        print(f"  {item.describe()}")
        print(f"      {item.source.describe()}")
    return OK


# --------------------------------------------------------------------------
# Wiring
# --------------------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m aeropub",
        description=(
            "Fleet-aware analysis of aeronautical publications. Every command "
            "reads the fact store and prints what is held — and what is not."
        ),
    )
    parser.add_argument(
        "--store",
        default=None,
        help=f"path to the fact store (default {DEFAULT_STORE}, or AEROPUB_STORE)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add(name: str, help_text: str, *, aerodrome: bool = True, as_at: bool = True):
        command = sub.add_parser(name, help=help_text, description=help_text)
        if aerodrome:
            command.add_argument("aerodrome", help="ICAO location indicator, e.g. OTHH")
        if as_at:
            command.add_argument(
                "--as-at", dest="as_at", default=None,
                help="the moment to speak for, ISO-8601 UTC (default: now)",
            )
        command.add_argument(
            "--json", action="store_true", help="emit the API payload instead"
        )
        return command

    airac = sub.add_parser("airac", help="the AIRAC cycle calendar")
    airac.add_argument("--on", default=None, help="the cycle in force on this date")
    airac.add_argument("--year", type=int, default=None, help="every cycle in a year")
    airac.set_defaults(handler=_cmd_airac)

    dossier = add("dossier", "everything held about one aerodrome, and every gap")
    dossier.add_argument("--on", default=None, help="resolve the effective state for this date")
    dossier.add_argument("--html", default=None, metavar="FILE", help="write a printable page")
    dossier.set_defaults(handler=_cmd_dossier)

    bulletin = add("bulletin", "what changed between two AIRAC cycles")
    bulletin.add_argument("--from", dest="from_cycle", required=True, metavar="CYCLE")
    bulletin.add_argument("--to", dest="to_cycle", required=True, metavar="CYCLE")
    bulletin.set_defaults(handler=_cmd_bulletin)

    plates = add(
        "charts",
        "reconcile a chart index against the AIP changes that should have "
        "amended it",
        aerodrome=False,
    )
    plates.add_argument(
        "aerodrome", nargs="?", default=None,
        help="ICAO location indicator, checked against the index (e.g. OTHH)",
    )
    plates.add_argument(
        "--register", default=None, metavar="FILE",
        help="path to a chart index manifest for one aerodrome",
    )
    plates.add_argument(
        "--from", dest="from_cycle", default=None, metavar="CYCLE",
        help="the cycle to compare from (default: the one before --to)",
    )
    plates.add_argument(
        "--to", dest="to_cycle", default=None, metavar="CYCLE",
        help="the cycle to compare to (default: the one now in force)",
    )
    plates.add_argument(
        "--template", action="store_true",
        help="print a blank chart index to fill in from a State's own index",
    )
    plates.set_defaults(handler=_cmd_charts)

    ahead = add("horizon", "what changes next, including what nobody will announce")
    ahead.add_argument("--days", type=int, default=DEFAULT_DAYS)
    ahead.set_defaults(handler=_cmd_horizon)

    conduct = add("quality", "how this State publishes, measured against PANS-AIM",
                  aerodrome=False)
    conduct.add_argument("--entity", default=None, help="narrow to one aerodrome")
    conduct.set_defaults(handler=_cmd_quality)

    lens = add("lens", "one audience's view of the evidence")
    lens.add_argument(
        "--audience", required=True, choices=sorted(a.value for a in Audience),
        help="; ".join(f"{a.value}: {LENSES[a].purpose}" for a in Audience)
        if all(hasattr(LENSES[a], "purpose") for a in Audience) else None,
    )
    lens.set_defaults(handler=_cmd_lens)

    fit = add("fit", "whether an aeroplane fits an aerodrome, and what was not checked")
    fit.add_argument(
        "--aircraft", required=True, action="append", metavar="MANIFEST",
        help=(
            "path to an aircraft manifest, repeatable. One manifest describes "
            "one document; give several for one aeroplane and each figure keeps "
            "the citation it was read with."
        ),
    )
    fit.add_argument("--on", default=None)
    fit.set_defaults(handler=_cmd_fit)

    ingest = sub.add_parser(
        "load", help="read cited facts from manifests into the store"
    )
    ingest.add_argument("manifest", nargs="*")
    ingest.add_argument(
        "--template", action="store_true",
        help="print a blank manifest to fill in from a page you read",
    )
    ingest.set_defaults(handler=_cmd_load)

    exposure = add(
        "exposure", "what an aerodrome means for one operator's fleet and network",
        aerodrome=False,
    )
    # Optional so --template works with nothing else supplied.
    exposure.add_argument("aerodrome", nargs="?", help="ICAO location indicator")
    exposure.add_argument("--profile", default=None, metavar="PROFILE",
                          help="path to an operator profile")
    exposure.add_argument("--template", action="store_true",
                          help="print a blank operator profile")
    exposure.add_argument("--on", default=None)
    exposure.set_defaults(handler=_cmd_exposure)

    network = add(
        "sweep",
        "every aerodrome in the network, ranked, with coverage stated",
        aerodrome=False,
    )
    network.add_argument("--profile", required=True, metavar="PROFILE")
    network.add_argument("--days", type=int, default=DEFAULT_DAYS,
                         help="how far ahead to look for exposure that worsens")
    network.add_argument("--on", default=None)
    network.set_defaults(handler=_cmd_sweep)

    age = add("currency", "how old the held data is, against the AIRAC calendar",
              aerodrome=False)
    age.add_argument("--on", default=None)
    age.add_argument("--stale-only", action="store_true",
                     help="list only what is too old to stand on")
    age.set_defaults(handler=_cmd_currency)

    secrets = sub.add_parser(
        "credentials", help="show, set or remove a stored secret (never prints one)"
    )
    secrets.add_argument("--set", metavar="NAME",
                         help="store a secret, read from a prompt")
    secrets.add_argument("--forget", metavar="NAME", help="remove a stored secret")
    secrets.set_defaults(handler=_cmd_credentials)

    back = add(
        "retrospect",
        "what was knowable at a past moment, beside what is held now",
    )
    back.add_argument("--known", required=True, metavar="MOMENT",
                      help="the moment whose knowledge to use, ISO-8601 UTC")
    back.add_argument("--on", default=None,
                      help="the day to resolve (default: the day of --known)")
    back.set_defaults(handler=_cmd_retrospect)

    blind = add("blindspots", "how late our own collection was",
                aerodrome=False)
    blind.add_argument("aerodrome", nargs="?", help="narrow to one aerodrome")
    blind.set_defaults(handler=_cmd_blindspots)

    flight = add(
        "trip", "one flight, one aeroplane, one date", aerodrome=False
    )
    flight.add_argument("--reference", required=True,
                        help="your own trip number")
    flight.add_argument("--aircraft", required=True, action="append",
                        metavar="MANIFEST")
    flight.add_argument("--on", required=True, metavar="DATE",
                        help="the day of the flight (YYYY-MM-DD)")
    flight.add_argument("--from", dest="departure", required=True, metavar="ICAO")
    flight.add_argument("--to", dest="destination", required=True, metavar="ICAO")
    flight.add_argument("--alternate", action="append", metavar="ICAO",
                        help="destination alternate, repeatable")
    flight.add_argument("--takeoff-alternate", dest="takeoff_alternate",
                        default=None, metavar="ICAO")
    flight.add_argument("--enroute", action="append", metavar="ICAO")
    flight.add_argument("--operator", default=None)
    flight.set_defaults(handler=_cmd_trip)

    library = add(
        "fleet",
        "the fleet library: coverage, one operator's fleet, or a fleet "
        "screened against an aerodrome",
        aerodrome=False,
    )
    library.add_argument(
        "aerodrome", nargs="?", default=None,
        help="ICAO location indicator to screen the fleet against, e.g. OTHH",
    )
    library.add_argument(
        "--library", action="append", metavar="FILE",
        help=(
            "path to a library document, repeatable. One document holds one "
            "kind of claim — a register, an operator's own fleet list, or an "
            "observation set — and several are merged with each statement "
            "keeping its own citation."
        ),
    )
    library.add_argument(
        "--operator", default=None, metavar="CODE",
        help="ICAO operator designator, IATA code or name",
    )
    library.add_argument(
        "--type", action="append", metavar="DESIGNATOR",
        help="narrow the screen to these type designators, repeatable",
    )
    library.add_argument(
        "--top", type=int, default=None, metavar="N",
        help="show only the N operators holding the most tails",
    )
    library.add_argument("--on", default=None)
    library.add_argument(
        "--template", action="store_true",
        help="print a blank library document to fill in from a source you hold",
    )
    library.set_defaults(handler=_cmd_fleet)

    sector = add(
        "route",
        "one sector end to end — both ends, the alternates, and the regions "
        "between them",
        aerodrome=False,
    )
    sector.add_argument("--from", dest="departure", required=True, metavar="ICAO")
    sector.add_argument("--to", dest="destination", required=True, metavar="ICAO")
    sector.add_argument(
        "--aircraft", required=True, action="append", metavar="MANIFEST",
        help="path to an aircraft manifest, repeatable",
    )
    sector.add_argument(
        "--alternate", action="append", metavar="ICAO",
        help="destination alternate, repeatable. One is treated as sole suitable",
    )
    sector.add_argument(
        "--takeoff-alternate", dest="takeoff_alternate", default=None, metavar="ICAO"
    )
    sector.add_argument(
        "--enroute-alternate", dest="enroute_alternate", action="append",
        metavar="ICAO", help="en-route or EDTO alternate, repeatable",
    )
    sector.add_argument(
        "--crosses", action="append", metavar="FIR",
        help=(
            "a flight information region this sector crosses, in order of "
            "overflight, repeatable. Regions not named are not checked, and "
            "the dossier says so."
        ),
    )
    sector.add_argument(
        "--route", default=None, metavar="ITEM15",
        help=(
            "the route as Item 15 of the flight plan states it, e.g. "
            "\"N0450F350 ALSEM UM688 BAYAN DCT KIA\". Without one the dossier "
            "speaks about both ends and the regions named, and nothing between"
        ),
    )
    sector.add_argument(
        "--structure", action="append", metavar="FILE",
        help="path to an ENR 3 extract, repeatable — one per State on the route",
    )
    sector.add_argument(
        "--level", type=float, default=None, metavar="FEET",
        help="planned cruising level in feet, screened against every segment",
    )
    sector.add_argument(
        "--holds", action="append", metavar="SPEC",
        help=(
            "a navigation specification the operator holds, in the codes the "
            "AIP prints, repeatable. Omitted means we do not know what they "
            "hold, which is reported as not knowing"
        ),
    )
    sector.add_argument(
        "--procedures", action="append", metavar="FILE",
        help=(
            "path to a procedure transcription, repeatable. Gives the "
            "departure and arrival profile — which SID reaches the first point "
            "of the route, which STAR leaves the last — and screens both for "
            "constraints an aeroplane cannot make"
        ),
    )
    sector.add_argument(
        "--airspace", action="append", metavar="FILE",
        help=(
            "path to an ENR 2 extract, repeatable — the airspace you are "
            "inside, its class, unit and carriage requirements"
        ),
    )
    sector.add_argument(
        "--hazards", action="append", metavar="FILE",
        help=(
            "path to an ENR 5 extract, repeatable — prohibited, restricted "
            "and danger areas, military and dangerous activity, sporting, "
            "bird migration, and overflight clearance lead times"
        ),
    )
    sector.add_argument(
        "--navaids", action="append", metavar="FILE",
        help=(
            "path to an ENR 4 extract, repeatable — the aids the route names, "
            "their frequency, coverage, hours and status"
        ),
    )
    sector.add_argument(
        "--navaid-template", dest="navaid_template", action="store_true",
        help="print a blank ENR 4 extract",
    )
    sector.add_argument(
        "--gnss", action="append", metavar="FILE",
        help=(
            "path to an ENR 4.3 extract, repeatable — which GNSS elements the "
            "State approves, for what, and what it requires before departure"
        ),
    )
    sector.add_argument(
        "--capability", action="append", metavar="LINE",
        choices=[c.value for c in ApproachCapability],
        help=(
            "an approach line this operation intends to use (lnav, "
            "lnav_vnav, lp, lpv, gls), repeatable — checked against what "
            "each State's ENR 4.3 actually authorises"
        ),
    )
    sector.add_argument(
        "--gnss-template", dest="gnss_template", action="store_true",
        help="print a blank ENR 4.3 extract",
    )
    sector.add_argument(
        "--planning", action="append", metavar="FILE",
        help=(
            "path to an ENR 1.10 extract, repeatable — filing deadlines, "
            "repetitive-plan acceptance, required Item 18 indicators and the "
            "EOBT slip a delay message covers"
        ),
    )
    sector.add_argument(
        "--item18", metavar="TEXT",
        help=(
            "Item 18 as filed, checked against the indicators each State "
            "crossed requires"
        ),
    )
    sector.add_argument(
        "--slip-minutes", dest="slip_minutes", type=float, default=None,
        metavar="MINUTES",
        help=(
            "how far EOBT has slipped, for screening against the delay "
            "tolerance each State publishes"
        ),
    )
    sector.add_argument(
        "--planning-template", dest="planning_template", action="store_true",
        help="print a blank ENR 1.10 extract",
    )
    sector.add_argument(
        "--notice-hours", dest="notice_hours", type=float, default=None,
        metavar="HOURS",
        help=(
            "how much notice this flight has, for screening clearance lead "
            "times. Omitted, the clearances are listed and not screened"
        ),
    )
    sector.add_argument(
        "--airspace-template", dest="airspace_template", action="store_true",
        help="print a blank ENR 2 extract",
    )
    sector.add_argument(
        "--hazard-template", dest="hazard_template", action="store_true",
        help="print a blank ENR 5 extract",
    )
    sector.add_argument(
        "--structure-template", dest="structure_template", action="store_true",
        help="print a blank ENR 3 extract to fill in from a State's route table",
    )
    sector.add_argument(
        "--procedure-template", dest="procedure_template", action="store_true",
        help="print a blank procedure transcription to fill in from a plate",
    )
    sector.add_argument(
        "--profile", default=None, metavar="FILE",
        help=(
            "write the vertical profile as a self-contained HTML page — the "
            "planned level as a line across the page, each leg standing on its "
            "binding minimum, and every gap drawn as a gap"
        ),
    )
    sector.add_argument(
        "--network", default=None, metavar="FILE",
        help=(
            "write the ATS route structure as a schematic — one lane per "
            "airway, its points in published order, and a connector wherever "
            "an airway meets another. Connectivity only: no coordinates are "
            "held, so it is not a map"
        ),
    )
    sector.add_argument(
        "--plan", default=None, metavar="FILE",
        help=(
            "write the plan view as a self-contained HTML page — published "
            "positions, the great-circle track between them, pan, zoom and "
            "click for detail. A point with no held position is listed rather "
            "than placed"
        ),
    )
    sector.add_argument(
        "--closed", action="append", metavar="ROUTE",
        help=(
            "an airway established as closed, repeatable. Passed in rather "
            "than inferred: a NOTAM against an airway may close it, restrict a "
            "level band on it, or say something else"
        ),
    )
    sector.add_argument("--reference", default=None, help="your own name for this sector")
    sector.add_argument("--on", default=None)
    sector.set_defaults(handler=_cmd_route)

    inventory = sub.add_parser("store", help="what the fact store holds")
    inventory.add_argument("-v", "--verbose", action="store_true")
    inventory.set_defaults(handler=_cmd_store)

    chart = sub.add_parser(
        "enroute",
        help="draw the published ATS route structure",
        description=(
            "ENR 3 is what a State publishes its route network as, and this "
            "draws it: every airway, its level band, its direction and its "
            "navigation specification, plotted on the coordinates ENR 4 "
            "publishes. A point with no published position is listed, never "
            "placed."
        ),
    )
    chart.add_argument(
        "structure", nargs="*", help="paths to ENR 3 extracts"
    )
    chart.add_argument(
        "--region", action="append", metavar="FIR",
        help="scope the chart to one region, repeatable",
    )
    chart.add_argument(
        "--route", action="append", metavar="DESIGNATOR",
        help="scope the chart to named airways, repeatable",
    )
    chart.add_argument(
        "--navaids", action="append", metavar="FILE",
        help="path to an ENR 4 extract, repeatable — positions and detail",
    )
    chart.add_argument(
        "--level", type=float, default=None, metavar="FEET",
        help=(
            "draw the airways whose published band contains this level; the "
            "rest are set aside with a reason, and any airway publishing no "
            "band is drawn anyway"
        ),
    )
    chart.add_argument(
        "--closed", action="append", metavar="ROUTE",
        help="an airway known closed, repeatable",
    )
    chart.add_argument(
        "--page", metavar="FILE", help="write the chart as a standalone page"
    )
    chart.add_argument(
        "--template", action="store_true", help="print a blank ENR 3 extract"
    )
    chart.set_defaults(handler=_cmd_enroute)

    audit = sub.add_parser(
        "checklist",
        help="reconcile a State's own checklist of pages against what we hold",
        description=(
            "GEN 0.4 is the State's list of every page in its AIP and the "
            "cycle each is current to. Reconciling against it is the "
            "difference between holding everything we fetched and holding "
            "everything the State says exists."
        ),
    )
    audit.add_argument("checklist", nargs="?", help="path to a checklist extract")
    audit.add_argument(
        "--holdings", metavar="FILE",
        help="path to a record of what we hold, to reconcile against",
    )
    audit.add_argument(
        "--held-supplement", action="append", metavar="ID",
        help="a supplement we hold, repeatable — checked against GEN 0.3",
    )
    audit.add_argument(
        "--template", action="store_true",
        help="print a blank checklist extract",
    )
    audit.add_argument(
        "--holdings-template", dest="holdings_template", action="store_true",
        help="print a blank record of holdings",
    )
    audit.set_defaults(handler=_cmd_checklist)

    aircraft = sub.add_parser(
        "aircraft", help="read aircraft manifests and show what they hold"
    )
    aircraft.add_argument("manifest", nargs="*")
    aircraft.add_argument(
        "--template", action="store_true",
        help="print a blank manifest to fill in from a document you hold",
    )
    aircraft.set_defaults(handler=_cmd_aircraft)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        return args.handler(args)
    except ManifestError as error:
        print(str(error), file=sys.stderr)
        return CANNOT_RUN
    except (KeyError, ValueError) as error:
        # A bad cycle identifier, an unknown audience, an unparseable date.
        # The command could not run; it did not run and answer no.
        print(f"{args.command}: {error}", file=sys.stderr)
        return CANNOT_RUN
    except BrokenPipeError:  # pragma: no cover - `| head` closing the pipe
        return OK

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
from aeropub.airac import AiracCycle, current_cycle, cycle_for, cycles_in_year
from aeropub.entities import aerodrome_of
from aeropub.api import dumps
from aeropub.bulletin import between_cycles
from aeropub.credentials import CredentialStore, describe as describe_secret
from aeropub.currency import Currency, assess_currency
from aeropub.dossier import build
from aeropub.horizon import DEFAULT_DAYS, horizon
from aeropub.ingest import load_facts
from aeropub.ingest import template as fact_template
from aeropub.lenses import LENSES, Audience, view
from aeropub.notam_register import NotamRegister
from aeropub.operator import Exposure, assess_operator, load_profile
from aeropub.operator import profile_template
from aeropub.quality import assess_quality
from aeropub.render import render_dossier
from aeropub.store import open_store
from aeropub.sweep import sweep as sweep_network
from aeropub.suitability import Assessment, assess_suitability

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

    inventory = sub.add_parser("store", help="what the fact store holds")
    inventory.add_argument("-v", "--verbose", action="store_true")
    inventory.set_defaults(handler=_cmd_store)

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

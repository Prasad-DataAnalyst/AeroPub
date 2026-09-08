"""``python -m aeropub.watch`` — the cycle, wired to real storage and a network.

Everything the live path needs has existed as separate pieces: a resolver per
State, a conditional transport, a reader that cites what it reads, a retention
policy, a ledger that survives a restart, and a cycle that does not stop. This
is the assembly, and it is deliberately thin — it decides nothing about
aeronautical data, only which pieces are handed to which.

    python -m aeropub.watch run                 every registered State, once
    python -m aeropub.watch run --state OT      one State
    python -m aeropub.watch run --plan          what it would fetch, fetching nothing
    python -m aeropub.watch reconcile           check the ledger against the archive
    python -m aeropub.watch status              what is held, and what is failing

Reconciliation runs first, always
---------------------------------
Every ``run`` reconciles before it fetches. A ledger that outlived its archive
answers UNCHANGED for every document forever, reports every State complete, and
says nothing — so the check that catches it belongs before the work, not behind
a flag somebody remembers to set.

It is cheap: one archive lookup per known URL, no network. ``--no-reconcile``
exists for a run that must not touch the ledger, and is the wrong default.

On being one process
--------------------
Within a single run the transport's validators start empty, so forgetting a URL
in the ledger and forgetting it in the transport amount to the same thing. They
diverge in a long-running process, where the transport has been accumulating
ETags for hours — and that is exactly the case where a lost archive would
otherwise go unnoticed longest. The wiring is the same either way, so it is
done properly here rather than left as a difference between how this runs and
how a daemon would.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

from .archive import Archive
from .cycle import Cycle, CycleReport
from .ledger import SqliteLedger
from .publication import Edition, EditionStatus
from .reader import Retrieved
from .resolve import Resolver
from .states import qatar
from .transport import LiveTransport, TransportError

__all__ = ["main", "resolvers", "build_cycle"]

OK = 0
INCOMPLETE = 1
CANNOT_RUN = 2

#: Where a run keeps its ledger and archive, unless told otherwise.
DEFAULT_HOME = Path.home() / ".aeropub"


def resolvers() -> dict[str, Resolver]:
    """Every State with a written route, by location-indicator prefix.

    A State appears here when someone has written and verified its route, not
    when it has been thought about. The registry being short is a statement
    about coverage, and a truthful one.
    """
    return {qatar.RESOLVER.state: qatar.RESOLVER}


def _keeper(archive: Archive, state: str):
    """Adapts :meth:`Archive.put` to the cycle's ``Keep``.

    The archive is content-addressed, so storing the same bytes twice is a
    no-op and the digest it returns is the key a citation resolves through.
    """

    def keep(body: bytes, media_type: str) -> str:
        entry = archive.put(
            body,
            source_id=state,
            url="",
            retrieved_at=datetime.now(timezone.utc),
            content_type=media_type or None,
        )
        return entry.digest

    return keep


def build_cycle(
    home: Path,
    *,
    only: str = "",
    edition: str = "current",
    transport: LiveTransport | None = None,
) -> tuple[Cycle, SqliteLedger, Archive]:
    """Assemble a cycle over real storage."""
    chosen = resolvers()
    if only:
        wanted = only.upper()
        chosen = {k: v for k, v in chosen.items() if k.upper() == wanted}
        if not chosen:
            raise ValueError(
                f"no route is written for {only!r}. Known: "
                f"{', '.join(sorted(resolvers())) or 'none'}. "
                "A State absent here has not been onboarded, which is not the "
                "same as a State that publishes nothing."
            )

    archive = Archive(home / "archive")
    ledger = SqliteLedger(home / "ledger.db")
    fetch = transport or LiveTransport()

    # One State's archive keys are not another's, so the keeper is per-State.
    # With one resolver this is the same object; with a hundred it is not, and
    # writing it the general way now avoids a rewrite that would be invisible
    # until a second State was added.
    def keep(body: bytes, media_type: str) -> str:
        return _keeper(archive, next(iter(chosen), "??"))(body, media_type)

    cycle = Cycle(
        resolvers=tuple(chosen.values()),
        retrieve=fetch,
        ledger=ledger,
        keep=keep,
        choose_edition=_edition_chooser(edition),
    )
    return cycle, ledger, archive


def _edition_chooser(which: str):
    wanted = {
        "current": EditionStatus.CURRENT,
        "next": EditionStatus.NEXT,
    }.get(which.lower())
    if wanted is None:
        raise ValueError(f"--edition takes 'current' or 'next', not {which!r}")

    def choose(editions: tuple[Edition, ...]) -> Edition | None:
        # No fallback to an undeclared edition. Reading next cycle's AIP as
        # though it were in force is the failure the whole model avoids.
        return next((e for e in editions if e.status is wanted), None)

    return choose


# -- commands --------------------------------------------------------------


def _cmd_run(args: argparse.Namespace) -> int:
    home = Path(args.home)
    transport = LiveTransport()
    try:
        cycle, ledger, archive = build_cycle(
            home, only=args.state, edition=args.edition, transport=transport
        )
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return CANNOT_RUN

    try:
        if not args.no_reconcile:
            result = ledger.reconcile(
                holds=archive.has, on_forget=transport.forget
            )
            if not result.is_sound:
                print(result.describe())
                print()

        if args.plan:
            return _plan(cycle, transport)

        report = cycle.run()
        print(report.describe())
        _print_changes(report)
        return OK if not (report.incomplete or report.unreached) else INCOMPLETE
    finally:
        ledger.close()


def _plan(cycle: Cycle, transport: LiveTransport) -> int:
    """What a run would fetch, fetching nothing.

    Resolution still needs the network — a State's documents are discovered by
    following its links, so they cannot be listed without asking. What this
    skips is reading the documents themselves, which is the bulk.
    """
    print("PLAN — resolving only, no documents read\n")
    for resolver in cycle.resolvers:
        try:
            editions = resolver.editions(cycle._read_bytes)
            chosen = cycle._pick(editions)
            if chosen is None:
                print(f"  {resolver.state} {resolver.name}: no edition chosen")
                continue
            publications = resolver.publications(chosen, cycle._read_bytes)
            print(f"  {resolver.state} {resolver.name}: {chosen.describe()}")
            print(f"    {len(publications)} documents would be read")
        except (TransportError, Exception) as error:  # noqa: BLE001
            print(f"  {resolver.state}: {type(error).__name__}: {error}")
    return OK


def _print_changes(report: CycleReport) -> None:
    changed = [
        (state.state, document.code or document.url)
        for state in report.states
        for document in state.read
    ]
    if not changed:
        return
    print("\nREAD THIS CYCLE")
    for state, what in changed[:40]:
        print(f"  {state}  {what}")
    if len(changed) > 40:
        print(f"  and {len(changed) - 40} more")


def _cmd_reconcile(args: argparse.Namespace) -> int:
    home = Path(args.home)
    archive = Archive(home / "archive")
    ledger = SqliteLedger(home / "ledger.db")
    try:
        result = ledger.reconcile(holds=archive.has, forget_missing=not args.check)
        print(result.describe())
        return OK if result.is_sound else INCOMPLETE
    finally:
        ledger.close()


def _cmd_status(args: argparse.Namespace) -> int:
    home = Path(args.home)
    if not home.exists():
        print(f"nothing held: {home} does not exist", file=sys.stderr)
        return CANNOT_RUN

    archive = Archive(home / "archive")
    ledger = SqliteLedger(home / "ledger.db")
    try:
        entries = list(ledger.entries())
        print(f"AEROPUB — {home}")
        print()
        print(f"  {len(entries)} documents known  ·  {len(archive)} archived  ·  "
              f"{archive.total_bytes():,} bytes")
        unarchived = [e for e in entries if not e.claims_a_copy]
        if unarchived:
            print(f"  {len(unarchived)} known without an archived copy")

        known = resolvers()
        print(f"\n  {len(known)} States with a written route")
        unbacked_anywhere = False
        for code, resolver in sorted(known.items()):
            host = resolver.entry_point.split("/")[2]
            mine = [e for e in entries if e.url.split("/")[2:3] == [host]]
            backed = sum(1 for e in mine if e.claims_a_copy and archive.has(e.archive_key))
            failures = ledger.failures_for(code)

            # "80 documents" beside an empty archive is the exact shape of
            # absence rendering as a pass. A count of documents known is not a
            # count of documents held, and the line says both or neither.
            line = f"    {code} {resolver.name}: {len(mine)} known"
            if backed != len(mine):
                unbacked_anywhere = True
                line += f", only {backed} backed by a copy"
            else:
                line += f", {backed} backed"
            if failures:
                line += f"  ·  failing {failures} cycles"
            print(line)

        if unbacked_anywhere:
            print(
                "\n  Documents known but not backed answer UNCHANGED while "
                "holding nothing.\n  Run: python -m aeropub.watch reconcile"
            )
            return INCOMPLETE
        return OK
    finally:
        ledger.close()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m aeropub.watch",
        description="Read every State's publications, on a cadence, without stopping.",
    )
    parser.add_argument(
        "--home", default=str(DEFAULT_HOME),
        help=f"where the ledger and archive live (default {DEFAULT_HOME})",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="one pass over every State")
    run.add_argument("--state", default="", metavar="OT", help="just this one")
    run.add_argument(
        "--edition", default="current", choices=("current", "next"),
        help="which edition to read (default: the one in force)",
    )
    run.add_argument(
        "--plan", action="store_true",
        help="resolve and report, without reading documents",
    )
    run.add_argument(
        "--no-reconcile", action="store_true",
        help="skip the ledger check. The wrong default, and rarely right",
    )
    run.set_defaults(handler=_cmd_run)

    check = sub.add_parser(
        "reconcile", help="check every ledger claim against the archive"
    )
    check.add_argument(
        "--check", action="store_true",
        help="report only; do not forget what is missing",
    )
    check.set_defaults(handler=_cmd_reconcile)

    where = sub.add_parser("status", help="what is held, and what is failing")
    where.set_defaults(handler=_cmd_status)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return args.handler(args)
    except KeyboardInterrupt:
        print("\ninterrupted; the ledger holds what was read", file=sys.stderr)
        return CANNOT_RUN


if __name__ == "__main__":
    raise SystemExit(main())

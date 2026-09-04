"""``python -m aeropub.eaip`` — the two commands that onboard a State.

    probe <page.html> --state OT --draft OT.json
    parse <page.html> --profile OT.json --aerodrome OTHH --document "AIP AD 2"

Deliberately its own entry point rather than a subcommand of ``aeropub``. The
person who runs ``probe`` is at a desk with the published page open and does
not necessarily have a fact store, a fleet or a network — and requiring any of
those to look at a document would be the wrong shape.

Standard library only, so it runs from a checkout on a machine that can reach
the State's website.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timezone
from pathlib import Path

from aeropub.eaip.parse import parse_page
from aeropub.eaip.probe import draft_profile, probe
from aeropub.eaip.profile import ProfileError, load_layout

__all__ = ["main"]

OK = 0
INCOMPLETE = 1
CANNOT_RUN = 2


def _read(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError as error:
        raise ProfileError(f"{path}: cannot be read — {error}") from None


def _cmd_probe(args: argparse.Namespace) -> int:
    html = _read(args.page)
    report = probe(html, source=args.page)
    print(report.describe())

    if args.draft:
        draft = draft_profile(
            html, state=args.state or "??", name=args.name or "",
            source_url=args.url or "",
        )
        Path(args.draft).write_text(draft.dumps() + "\n", encoding="utf-8")
        print()
        print(f"Draft profile written to {args.draft}")
        print(f"  {draft.describe()}")
        if not draft.sections:
            print(
                "\n  It has no section rules, because no element in this "
                "document carried an\n  identifier shaped like an AIP section "
                "reference. That is worth knowing:\n  this page may be a "
                "frameset, a navigation index, or a PDF wrapper rather\n  than "
                "the content. Check what you saved."
            )
            return INCOMPLETE
        print(
            "\n  Add the fields you want under each section, check every rule "
            "against the\n  page, then set verified_at and verified_by. Until "
            "then every value read\n  with it is recorded at LOW confidence."
        )
    return OK


def _cmd_parse(args: argparse.Namespace) -> int:
    profile = load_layout(args.profile)
    result = parse_page(
        _read(args.page),
        profile,
        aerodrome=args.aerodrome,
        document=args.document,
        valid_from=date.fromisoformat(args.valid_from),
        source_id=args.source_id or "",
        original_url=args.url or "",
        retrieved_at=(
            datetime.fromisoformat(args.retrieved_at.replace("Z", "+00:00"))
            if args.retrieved_at
            else datetime.now(timezone.utc)
        ),
    )
    print(result.render())
    # Incomplete is not failure. It is the answer, and a caller scripting this
    # wants to know the difference between "read everything" and "read some".
    return OK if result.is_complete else INCOMPLETE


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m aeropub.eaip",
        description=(
            "Read an eAIP page. `probe` describes a document and drafts a "
            "profile; `parse` reads one with a profile, and emits only what "
            "the profile located."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    look = sub.add_parser("probe", help="describe a page and draft a profile")
    look.add_argument("page", help="a saved eAIP page (HTML)")
    look.add_argument("--state", default="", help="location-indicator prefix, e.g. OT")
    look.add_argument("--name", default="", help="the State's name")
    look.add_argument("--url", default="", help="where the page came from")
    look.add_argument("--draft", default=None, metavar="FILE",
                      help="write a draft profile here")
    look.set_defaults(handler=_cmd_probe)

    read = sub.add_parser("parse", help="read a page with a profile")
    read.add_argument("page")
    read.add_argument("--profile", required=True)
    read.add_argument("--aerodrome", required=True, help="e.g. OTHH")
    read.add_argument("--document", required=True,
                      help='what to cite, e.g. "AIP Qatar AD 2 OTHH"')
    read.add_argument("--valid-from", dest="valid_from", required=True,
                      help="the date these values take effect (YYYY-MM-DD)")
    read.add_argument("--source-id", dest="source_id", default="")
    read.add_argument("--url", default="")
    read.add_argument("--retrieved-at", dest="retrieved_at", default=None)
    read.set_defaults(handler=_cmd_parse)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return args.handler(args)
    except (ProfileError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return CANNOT_RUN

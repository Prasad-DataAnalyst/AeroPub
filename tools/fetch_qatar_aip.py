#!/usr/bin/env python3
"""Fetch Qatar's eAIP, on a machine that can reach aim.gov.qa.

Standard library only — Python 3.8+, nothing to install. Qatar's eAIP is
public, so no credential is involved.

    python3 fetch_qatar_aip.py --out qatar-aip/
    python3 fetch_qatar_aip.py --out qatar-aip/ --section ENR-4.4 --section ENR-3.2

Then hand the directory to AeroPub:

    python -m aeropub.eaip probe qatar-aip/ENR-4.4-en-GB.html --state OT

Why it follows links instead of building URLs
----------------------------------------------
Qatar changed its eAIP layout between 2025 and 2026. The current edition path
carries a running amendment number that **cannot be derived from the AIRAC
cycle** — three of its four fields fall out of the calendar and the amendment
number does not. So a constructed URL is a guess that goes stale silently.

This starts at the published edition history, follows the newest edition's
link to its index, and follows the index to the sections. Nothing is assumed
about the path shape beyond it being a link on a page, which is the part least
likely to change and the part that is checkable when it does.
"""

from __future__ import annotations

import argparse
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

AIM = "https://aim.gov.qa"
HISTORY = f"{AIM}/AIP/QA-history-en-GB.html"
AGENT = "AeroPub/0.1 (aeronautical information analysis)"

#: A starter set for --quick. Without coordinates the route structure draws as
#: a list of names, so ENR 4.4 leads.
QUICK_SECTIONS = ("ENR-4.4", "ENR-3.2", "ENR-3.1", "ENR-2.1", "ENR-5.1")

#: What an AIP page looks like, by filename. GEN, ENR and AD parts, plus the
#: per-aerodrome AD 2 pages. Deliberately broad: the whole AIP is the point,
#: and a page this does not recognise is reported rather than skipped
#: silently.
_SECTION_FILE = re.compile(
    r"(?:^|[-_])(GEN|ENR|AD)[-_ ]?\d", re.I
)

#: Pages that are navigation rather than content. Fetched anyway where they
#: are small, but not counted as sections.
_NOT_A_SECTION = re.compile(
    r"(?:index|menu|frame|toc|contents|history|banner|search)", re.I
)

#: Seconds between requests. A full AIP is a hundred and more pages, and a
#: State's AIM server is not a CDN — this is a courtesy, not a rate limit
#: anybody imposed.
POLITE_DELAY = 0.5


def die(message: str) -> "NoReturn":  # noqa: F821
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(1)


def fetch(url: str, timeout: int, fatal: bool = True) -> "bytes | None":
    """The page, or None when ``fatal`` is off and it could not be had.

    Discovery walks pages it has only guessed at, and one dead frame there
    should not end a run that is otherwise going fine. Fetching a section
    the index actually named is a different matter, and stays fatal.
    """
    request = urllib.request.Request(url, headers={"User-Agent": AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()
    except urllib.error.HTTPError as error:
        if not fatal:
            print(f"    skipped {url} (HTTP {error.code})")
            return None
        die(f"{url} answered HTTP {error.code}")
    except urllib.error.URLError as error:
        if not fatal:
            print(f"    skipped {url} ({error.reason})")
            return None
        die(
            f"could not reach {url}: {error.reason}\n"
            "       If this is a corporate network, try mobile data — some "
            "block State AIM hosts."
        )


def links(html: str, base: str) -> list[tuple[str, str]]:
    """(absolute url, link text) for every anchor on the page."""
    found = []
    for match in re.finditer(
        r"<a\b[^>]*href\s*=\s*[\"']([^\"']+)[\"'][^>]*>(.*?)</a>",
        html,
        re.I | re.S,
    ):
        href, text = match.group(1), re.sub(r"<[^>]+>", " ", match.group(2))
        found.append((urllib.parse.urljoin(base, href), " ".join(text.split())))
    return found


def editions(html: str, base: str) -> list[tuple[str, str]]:
    """Every eAIP edition the history page links to, newest guess first.

    Recognised by the shape EUROCONTROL eAIP uses — an index page under a
    dated directory — rather than by position on the page, which is the part
    that moves when a layout changes.
    """
    found = [
        (url, text)
        for url, text in links(html, base)
        if re.search(r"/(?:AIP|eAIP)/", url, re.I)
        and re.search(r"index[^/]*\.html?$", url, re.I)
    ]
    # Dates in the path sort correctly as text when they are ISO; where they
    # are not, the page's own order is the better guide, so this is a stable
    # sort over the page order rather than a re-ordering.
    def dated(item):
        stamp = re.search(r"(\d{4})-(\d{2})-(\d{2})", item[0])
        return stamp.group(0) if stamp else ""
    return sorted(found, key=dated, reverse=True)


def sections_on(html: str, base: str, wanted: tuple[str, ...]) -> dict[str, str]:
    """Section code -> url, for the sections asked for."""
    found: dict[str, str] = {}
    for url, _ in links(html, base):
        name = url.rsplit("/", 1)[-1]
        for code in wanted:
            # ENR-4.4 matches ENR-4.4-en-GB.html and QA-ENR-4.4-en-GB.html.
            if re.search(rf"(?:^|[^0-9A-Za-z]){re.escape(code)}(?:[^0-9]|$)", name, re.I):
                found.setdefault(code, url)
    return found


def all_sections_on(html: str, base: str) -> dict[str, str]:
    """Every AIP section the page links to: code -> url.

    The whole AIP rather than a chosen few. The code is taken from the
    filename with the State prefix and language suffix removed, so
    ``QA-ENR-4.4-en-GB.html`` files under ``ENR-4.4`` and
    ``QA-AD-2-OTHH-en-GB.html`` under ``AD-2-OTHH``.
    """
    found: dict[str, str] = {}
    for url in referenced_pages(html, base):
        name = url.rsplit("/", 1)[-1].split("?")[0]
        if not name.lower().endswith((".html", ".htm")):
            continue
        if _NOT_A_SECTION.search(name):
            continue
        if not _SECTION_FILE.search(name):
            continue
        code = re.sub(r"\.html?$", "", name, flags=re.I)
        code = re.sub(r"^[A-Z]{2}-", "", code)            # QA- prefix
        code = re.sub(r"-[a-z]{2}-[A-Z]{2}$", "", code)   # -en-GB suffix
        found.setdefault(code, url)
    return found


#: Any quoted path ending .html — an anchor's href, a frame's src, or a
#: string inside a menu's JavaScript. They are indistinguishable here and
#: that is the point: reading only <a href> misses a frameset entirely.
#: An anchor in a real eAIP menu carries a fragment — ``QA-GEN-0.1-en-GB.html#i197343``
#: names the page *and* the element within it. Requiring the quote straight after
#: ``.html`` made every one of Qatar's hundred-odd section links invisible, so the
#: fragment and any query are matched and discarded rather than assumed absent.
_HREFISH = re.compile(
    r"""["']([^"'<>\s#?]+?\.html?)(?:[#?][^"'<>\s]*)?["']""", re.I
)


def referenced_pages(text: str, base: str) -> list[str]:
    """Every .html path the text quotes anywhere, made absolute.

    Deliberately not an HTML parse. Qatar's index is a frameset whose menu
    is assembled by ``menu.js``, so the contents tree is in a script rather
    than in markup, and an anchor-only reader comes back empty.
    """
    seen: dict[str, None] = {}
    for match in _HREFISH.finditer(text):
        seen.setdefault(urllib.parse.urljoin(base, match.group(1)), None)
    return list(seen)


def _follow_from(text: str, base: str) -> list[str]:
    """Pages worth following when this one carries no sections itself.

    Frames first, then scripts, then the anchors that name a menu or
    contents page. Order matters only in that it puts the likely answer
    first; everything found is tried.
    """
    out: dict[str, None] = {}
    for pattern in (
        r"<i?frame\b[^>]*\bsrc\s*=\s*[\"']([^\"']+)[\"']",
        r"<script\b[^>]*\bsrc\s*=\s*[\"']([^\"']+\.js)[\"']",
    ):
        for match in re.finditer(pattern, text, re.I):
            out.setdefault(urllib.parse.urljoin(base, match.group(1)), None)
    for url in referenced_pages(text, base):
        if _NOT_A_SECTION.search(url.rsplit("/", 1)[-1]):
            out.setdefault(url, None)
    return list(out)


def discover(
    text: str, url: str, timeout: int, delay: float, depth: int = 3, budget: int = 40
) -> dict[str, str]:
    """Walk from a page to the AIP sections, through frames and scripts.

    An eAIP index is normally a frameset, so the sections are two or three
    pages in: index -> menu frame -> the menu's own script. This follows
    that chain rather than assuming its shape, and stops at the first level
    that yields sections so a working AIP costs one extra request, not a
    crawl.
    """
    found = all_sections_on(text, url)
    if found:
        return found

    seen = {url}
    frontier = _follow_from(text, url)
    for level in range(1, depth + 1):
        frontier = [u for u in frontier if u not in seen]
        if not frontier:
            break
        print(f"  no sections yet — following {len(frontier)} link(s), depth {level}")
        next_frontier: list[str] = []
        for target in frontier:
            if len(seen) >= budget:
                print(f"    stopping at {budget} pages looked at")
                break
            seen.add(target)
            time.sleep(delay)
            body = fetch(target, timeout, fatal=False)
            if body is None:
                continue
            inner = body.decode("utf-8", "replace")
            found.update(all_sections_on(inner, target))
            next_frontier.extend(_follow_from(inner, target))
        if found:
            print(f"  {len(found)} sections found at depth {level}")
            return found
        frontier = next_frontier
    return found


def effective_date(url: str) -> "date | None":
    """The date an edition takes effect, read from its path.

    A EUROCONTROL eAIP path carries two dates that mean different things:
    ``/AIP/03-SEP-2026/AIP-30/2026-10-01-000000/html/`` was *published* on
    3 September and takes *effect* on 1 October. So the newest published
    edition is routinely one that is not in force yet — right for seeing
    what is about to change, wrong for saying what is in force today.
    Both are worth having and the difference must not be silent.
    """
    stamp = re.search(r"/(\d{4})-(\d{2})-(\d{2})-\d{6}/", url)
    if stamp is None:
        stamp = re.search(r"(\d{4})-(\d{2})-(\d{2})", url)
    if stamp is None:
        return None
    try:
        return date(int(stamp.group(1)), int(stamp.group(2)), int(stamp.group(3)))
    except ValueError:
        return None


def in_force_on(
    found: list[tuple[str, str]], day: date
) -> "tuple[str, str] | None":
    """The newest edition already effective on this day, if it can be told."""
    dated = [
        (effective_date(url), url, text)
        for url, text in found
        if effective_date(url) is not None
    ]
    current = [d for d in dated if d[0] <= day]
    if not current:
        return None
    best = max(current, key=lambda d: d[0])
    return (best[1], best[2])


#: Qatar's history page files each edition under a table that says what it
#: is: ``current-issues-table``, ``next-issues-table``,
#: ``archived-issues-table``. That is the State's own declaration, and it
#: beats inferring status from a date in a path — the inference happens to
#: agree here, but only the declaration stays right when a State republishes
#: out of order or carries two effective editions at once.
_STATUS_TABLE = re.compile(
    r"<table[^>]*\bclass\s*=\s*[\"']([^\"']*)[\"'][^>]*>(.*?)</table>",
    re.I | re.S,
)

_STATUS_NAMES = (
    ("current", "current-issues"),
    ("next", "next-issues"),
    ("archived", "archived-issues"),
)


def editions_by_status(html: str, base: str) -> dict[str, list[tuple[str, str]]]:
    """Editions grouped as the State itself groups them.

    Returns ``{}`` when the page carries no such tables, which is the honest
    answer for a layout that does not declare status — the caller then falls
    back to dates rather than this inventing a status.
    """
    grouped: dict[str, list[tuple[str, str]]] = {}
    for match in _STATUS_TABLE.finditer(html):
        classes, body = match.group(1).lower(), match.group(2)
        status = next(
            (name for name, marker in _STATUS_NAMES if marker in classes), None
        )
        if status is None:
            continue
        rows = [
            (url, text)
            for url, text in links(body, base)
            if re.search(r"index[^/]*\.html?$", url, re.I)
        ]
        if rows:
            grouped.setdefault(status, []).extend(rows)
    return grouped


#: The menu's tabs name three more bodies of publication that are not AIP
#: sections and are not optional. AeroPub's precedence is
#: AIP < AMDT < SUP < NOTAM, so fetching the AIP part alone takes the
#: *lowest* layer and silently misses everything that supersedes it. A
#: supplement in force changes what an AIP section means; not having it is
#: not the same as it not existing.
_COMPANION_TAB = re.compile(r"(?:AMDT|eSUPs?|eAICs?)[-_]?", re.I)


def companion_indexes(html: str, base: str) -> dict[str, str]:
    """The AMDT, SUP and AIC list pages the menu links to: label -> url."""
    found: dict[str, str] = {}
    for url, text in links(html, base):
        name = url.rsplit("/", 1)[-1].split("#")[0]
        if not name.lower().endswith((".html", ".htm")):
            continue
        match = _COMPANION_TAB.search(name)
        if match is None:
            continue
        label = re.sub(r"^QA-|-[a-z]{2}-[A-Z]{2}\.html?$", "", name, flags=re.I)
        found.setdefault(label or (text or name), url)
    return found


def documents_beside(html: str, base: str) -> dict[str, str]:
    """Every document a companion index links to, in its own directory.

    Scoped to the index's directory deliberately: a SUP list links to its
    supplements and also back to the AIP menu, and following the latter would
    walk the whole AIP a second time.
    """
    home = base.rsplit("/", 1)[0] + "/"
    found: dict[str, str] = {}
    for url in referenced_pages(html, base):
        if not url.startswith(home):
            continue
        name = url[len(home):]
        if "/" in name or _NOT_A_SECTION.search(name):
            continue
        found.setdefault(re.sub(r"\.html?$", "", name, flags=re.I), url)
    return found


def save(out: Path, name: str, body: bytes) -> Path:
    out.mkdir(parents=True, exist_ok=True)
    path = out / name
    path.write_bytes(body)
    print(f"  wrote {path}  ({len(body):,} bytes)")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fetch Qatar's published eAIP. The whole AIP by default."
    )
    parser.add_argument("--out", default="qatar-aip", help="directory to write into")
    parser.add_argument(
        "--section", action="append", metavar="CODE",
        help="fetch only these, repeatable. Default is every section on the index",
    )
    parser.add_argument(
        "--quick", action="store_true",
        help=f"only the starter set: {', '.join(QUICK_SECTIONS)}",
    )
    parser.add_argument(
        "--edition", metavar="URL",
        help="a specific edition index URL, skipping the history lookup",
    )
    parser.add_argument(
        "--in-force", action="store_true",
        help="take the edition in force today, not the newest published",
    )
    parser.add_argument(
        "--delay", type=float, default=POLITE_DELAY,
        help=f"seconds between requests (default {POLITE_DELAY})",
    )
    parser.add_argument(
        "--refetch", action="store_true",
        help="fetch pages already saved. Without it the run resumes",
    )
    parser.add_argument(
        "--no-companions", action="store_true",
        help="AIP sections only — skip the AMDT, SUP and AIC lists",
    )
    parser.add_argument("--timeout", type=int, default=60)
    args = parser.parse_args()

    out = Path(args.out)

    if args.edition:
        index_url = args.edition
        print(f"edition given: {index_url}")
    else:
        print(f"history: {HISTORY}")
        body = fetch(HISTORY, args.timeout)
        save(out, "QA-history-en-GB.html", body)
        found = editions(body.decode("utf-8", "replace"), HISTORY)
        if not found:
            die(
                "no edition links found on the history page. It is saved — "
                "hand it over rather than letting this guess a URL."
            )
        today = date.today()
        declared = editions_by_status(body.decode("utf-8", "replace"), HISTORY)
        if declared:
            print("  the State labels its editions:")
            for status in ("current", "next", "archived"):
                for url, text in declared.get(status, []):
                    print(f"    {status:9} {effective_date(url)}  {text[:52]}")
        print(f"  {len(found)} editions listed:")
        for url, text in found:
            effective = effective_date(url)
            if effective is None:
                when = "effective ?         "
            elif effective <= today:
                when = f"effective {effective}  in force ({(today - effective).days}d)"
            else:
                when = f"effective {effective}  in {(effective - today).days} days"
            print(f"    {when}  {text or '(no text)'}")

        # The State's own label wins over a date read out of a path. They
        # agree here; only the label stays right when a State republishes out
        # of order or carries two effective editions at once.
        current = None
        if declared.get("current"):
            current = declared["current"][0]
        else:
            current = in_force_on(found, today)
        if declared.get("next"):
            found = declared["next"] + [e for e in found if e not in declared["next"]]

        if args.in_force:
            if current is None:
                die(
                    "no listed edition is in force today. The history page is "
                    "saved — hand it over."
                )
            index_url = current[0]
            print(f"\n  taking the edition in force today: {current[1]}")
        else:
            index_url = found[0][0]
            chosen = effective_date(index_url)
            print(f"\n  taking the newest published: {found[0][1]}")
            if chosen is not None and chosen > today:
                print(
                    f"    note: it does not take effect until {chosen}. "
                    "What is in force today is"
                )
                print(
                    f"    {current[1] if current else 'not listed here'}"
                    " — --in-force takes that instead."
                )

    print(f"\nindex: {index_url}")
    body = fetch(index_url, args.timeout)
    save(out, "index-en-GB.html", body)
    index_html = body.decode("utf-8", "replace")

    # An eAIP index is a frameset whose menu is often built in JavaScript, so
    # the sections are two or three pages in. Walk it rather than assume it.
    located = discover(index_html, index_url, args.timeout, args.delay)

    if not located:
        die(
            "no AIP sections found. The index is saved — hand it over and the "
            "reader will describe what is actually there."
        )

    if args.section:
        wanted = {c.upper() for c in args.section}
        located = {
            k: v for k, v in located.items()
            if k.upper() in wanted or any(w in k.upper() for w in wanted)
        }
    elif args.quick:
        located = {
            k: v for k, v in located.items()
            if any(q.upper() in k.upper() for q in QUICK_SECTIONS)
        }

    order = sorted(located, key=_sort_key)
    print(f"\n{len(order)} sections to fetch")
    if args.delay:
        print(f"  {args.delay}s between requests — a State's AIM server is not a CDN")

    got = skipped = 0
    for index, code in enumerate(order, start=1):
        target = out / f"{code}-en-GB.html"
        if target.exists() and not args.refetch:
            skipped += 1
            continue
        if got:
            time.sleep(args.delay)
        print(f"  [{index}/{len(order)}] {code}")
        save(out, target.name, fetch(located[code], args.timeout))
        got += 1

    # The AIP part is the lowest layer of the precedence stack. A supplement
    # in force changes what a section means, and an AIP fetched without its
    # supplements reads as though nothing supersedes it — which is not a gap
    # you would notice by looking at the pages that did arrive.
    companions = companion_indexes(index_html, index_url)
    if args.no_companions:
        print("\nskipping AMDT, SUP and AIC lists (--no-companions)")
    elif not companions:
        print(
            "\nno AMDT, SUP or AIC list found on the index. That may be right "
            "for this\n  State, or the menu may name them elsewhere — it is "
            "reported, not assumed."
        )
    else:
        print(f"\n{len(companions)} companion lists: {', '.join(companions)}")
        for label, url in companions.items():
            time.sleep(args.delay)
            page = fetch(url, args.timeout, fatal=False)
            if page is None:
                continue
            folder = url.rsplit("/", 2)[-2]
            here = out / folder
            save(here, url.rsplit("/", 1)[-1], page)
            documents = documents_beside(page.decode("utf-8", "replace"), url)
            documents.pop(re.sub(r"\.html?$", "", url.rsplit("/", 1)[-1], flags=re.I), None)
            print(f"  {label}: {len(documents)} documents")
            for name in sorted(documents):
                target = here / f"{name}.html"
                if target.exists() and not args.refetch:
                    skipped += 1
                    continue
                time.sleep(args.delay)
                content = fetch(documents[name], args.timeout, fatal=False)
                if content is None:
                    continue
                save(here, target.name, content)
                got += 1

    print(f"\n{got} fetched, {skipped} already held")
    if skipped and not args.refetch:
        print("  --refetch to fetch them again")

    parts: dict[str, int] = {}
    for code in order:
        parts[code.split("-")[0].upper()] = parts.get(code.split("-")[0].upper(), 0) + 1
    print("  " + "  ".join(f"{k} {v}" for k, v in sorted(parts.items())))

    print(f"\nHand {out}/ to AeroPub:")
    print(f"  python -m aeropub.eaip probe {out}/ENR-4.4-en-GB.html --state OT \\")
    print("      --name Qatar --draft profiles/ot.json")
    return 0


def _sort_key(code: str):
    """GEN before ENR before AD, then by number rather than as text.

    So ENR-3.2 sorts before ENR-10 rather than after it, and a reader looking
    for a section finds it where an AIP would put it.
    """
    part = code.split("-")[0].upper()
    rank = {"GEN": 0, "ENR": 1, "AD": 2}.get(part, 3)
    numbers = [int(n) for n in re.findall(r"\d+", code)]
    return (rank, numbers, code)


if __name__ == "__main__":
    raise SystemExit(main())

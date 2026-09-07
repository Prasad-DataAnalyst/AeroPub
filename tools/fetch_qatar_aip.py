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


def fetch(url: str, timeout: int) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()
    except urllib.error.HTTPError as error:
        die(f"{url} answered HTTP {error.code}")
    except urllib.error.URLError as error:
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
    for url, _ in links(html, base):
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
        "--delay", type=float, default=POLITE_DELAY,
        help=f"seconds between requests (default {POLITE_DELAY})",
    )
    parser.add_argument(
        "--refetch", action="store_true",
        help="fetch pages already saved. Without it the run resumes",
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
        print(f"  {len(found)} editions listed; taking the newest:")
        for url, text in found[:3]:
            print(f"    {text or '(no text)'}  {url}")
        index_url = found[0][0]

    print(f"\nindex: {index_url}")
    body = fetch(index_url, args.timeout)
    save(out, "index-en-GB.html", body)
    index_html = body.decode("utf-8", "replace")

    # An eAIP index is often a frameset. Gather sections from it and from one
    # level of whatever it points at, which is where the menu usually lives.
    located = all_sections_on(index_html, index_url)
    if not located:
        print("  no sections on the index itself; following its frames")
        for url, _ in links(index_html, index_url)[:30]:
            if not re.search(r"\.html?$", url, re.I):
                continue
            time.sleep(args.delay)
            inner = fetch(url, args.timeout).decode("utf-8", "replace")
            located.update(all_sections_on(inner, url))
        if located:
            print(f"  found {len(located)} through the frames")

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

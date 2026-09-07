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
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

AIM = "https://aim.gov.qa"
HISTORY = f"{AIM}/AIP/QA-history-en-GB.html"
AGENT = "AeroPub/0.1 (aeronautical information analysis)"

#: Most useful first. Without coordinates the route structure draws as a list
#: of names, so ENR 4.4 leads.
DEFAULT_SECTIONS = ("ENR-4.4", "ENR-3.2", "ENR-3.1", "ENR-2.1", "ENR-5.1")


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


def save(out: Path, name: str, body: bytes) -> Path:
    out.mkdir(parents=True, exist_ok=True)
    path = out / name
    path.write_bytes(body)
    print(f"  wrote {path}  ({len(body):,} bytes)")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch Qatar's published eAIP.")
    parser.add_argument("--out", default="qatar-aip", help="directory to write into")
    parser.add_argument(
        "--section", action="append", metavar="CODE",
        help=f"section to fetch, repeatable. Default: {', '.join(DEFAULT_SECTIONS)}",
    )
    parser.add_argument(
        "--edition", metavar="URL",
        help="a specific edition index URL, skipping the history lookup",
    )
    parser.add_argument("--timeout", type=int, default=60)
    args = parser.parse_args()

    out = Path(args.out)
    wanted = tuple(args.section) if args.section else DEFAULT_SECTIONS

    if args.edition:
        index_url = args.edition
        print(f"edition given: {index_url}")
    else:
        print(f"history: {HISTORY}")
        body = fetch(HISTORY, args.timeout)
        save(out, "QA-history-en-GB.html", body)
        html = body.decode("utf-8", "replace")
        found = editions(html, HISTORY)
        if not found:
            die(
                "no edition links found on the history page. Save it and hand "
                "it over — the layout has changed and the reader needs to see "
                "the real thing rather than guess."
            )
        print(f"  {len(found)} editions listed; newest looks like:")
        for url, text in found[:3]:
            print(f"    {text or '(no text)'}  {url}")
        index_url = found[0][0]

    print(f"\nindex: {index_url}")
    body = fetch(index_url, args.timeout)
    save(out, "index-en-GB.html", body)
    index_html = body.decode("utf-8", "replace")

    located = sections_on(index_html, index_url, wanted)
    missing = [c for c in wanted if c not in located]

    if not located:
        # The index may be a frameset pointing at a menu. Follow one level.
        print("  no sections on the index; following its frames")
        for url, _ in links(index_html, index_url)[:20]:
            if not re.search(r"\.html?$", url, re.I):
                continue
            inner = fetch(url, args.timeout).decode("utf-8", "replace")
            located.update(sections_on(inner, url, wanted))
            if located:
                break
        missing = [c for c in wanted if c not in located]

    print()
    for code, url in sorted(located.items()):
        save(out, f"{code}-en-GB.html", fetch(url, args.timeout))

    if missing:
        print(f"\nnot found on this edition: {', '.join(missing)}")
        print("  Not necessarily absent — the index may name them differently.")
        print(f"  {out}/index-en-GB.html is saved; hand it over and the reader")
        print("  will describe what is actually there.")

    print(f"\nHand {out}/ to AeroPub:")
    print(f"  python -m aeropub.eaip probe {out}/ENR-4.4-en-GB.html --state OT \\")
    print("      --name Qatar --draft profiles/ot.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

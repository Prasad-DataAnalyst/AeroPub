"""How a State's publications are reached — one route per State.

There is no universal way in. A State's route is a property of that State's
website, and the differences are not cosmetic:

* **Qatar** takes five hops. A history page lists editions; an edition points
  at a frameset; the frameset names a navigation frame; the frame holds the
  contents; the contents link the sections, and separately the supplement and
  circular lists in their own directories.
* **A State on a plain index** takes one. The index links the sections.
* **The FAA** takes none of these — its publications come through an
  authenticated API returning AIXM, and no page is traversed at all.

So the traversal is per-State and the *model* is shared. :class:`Resolver` is
the contract every State meets; :class:`EaipTraversal` is the machinery States
on the EUROCONTROL toolchain reuse, which is most of the ones that publish an
eAIP at all. A State whose route is stranger implements the contract directly
and shares nothing but :mod:`aeropub.publication`.

Reading is injected
-------------------
A resolver never opens a socket. It is handed a :class:`Read` — one function
from URL to bytes — so the same resolver runs against the live transport, a
directory of saved pages, or a test fixture, and none of them is a special
case. It also means a resolver can be written and verified against pages
captured from a State before the network to that State exists.
"""

from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass, field
from datetime import date
from typing import Callable, Protocol, runtime_checkable

from .publication import Edition, EditionStatus, Kind, Publication, kind_of

__all__ = [
    "Read",
    "Resolver",
    "EaipTraversal",
    "links",
    "referenced_pages",
]

#: One function from URL to bytes. Everything a resolver needs from a network.
Read = Callable[[str], bytes]


@runtime_checkable
class Resolver(Protocol):
    """One State's route to its publications."""

    state: str
    """Location-indicator prefix, e.g. ``"OT"``."""

    name: str
    entry_point: str
    """The single address treated as known. Everything else is followed."""

    def editions(self, read: Read) -> tuple[Edition, ...]:
        """Every edition the State currently offers, as it presents them."""
        ...

    def publications(self, edition: Edition, read: Read) -> tuple[Publication, ...]:
        """Every document in one edition."""
        ...


_ANCHOR = re.compile(
    r"<a\b[^>]*href\s*=\s*[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", re.I | re.S
)

#: Any quoted path to a page, wherever it appears — an anchor's href, a frame's
#: src, or a string inside a menu's JavaScript. A real eAIP menu writes
#: ``QA-GEN-0.1-en-GB.html#i197343``: it names the page *and* the element
#: within it, so the fragment is matched and discarded rather than assumed
#: absent. Requiring the quote straight after ``.html`` makes a menu naming an
#: entire AIP read as empty.
_HREFISH = re.compile(
    r"""["']([^"'<>\s#?]+?\.(?:html?|xml|pdf))(?:[#?][^"'<>\s]*)?["']""", re.I
)

_FRAME = re.compile(r"<i?frame\b[^>]*\bsrc\s*=\s*[\"']([^\"']+)[\"']", re.I)
_SCRIPT = re.compile(r"<script\b[^>]*\bsrc\s*=\s*[\"']([^\"']+\.js)[\"']", re.I)

_STATUS_TABLE = re.compile(
    r"<table[^>]*\bclass\s*=\s*[\"']([^\"']*)[\"'][^>]*>(.*?)</table>", re.I | re.S
)

_DECLARED_STATUS: tuple[tuple[str, EditionStatus], ...] = (
    ("current-issues", EditionStatus.CURRENT),
    ("next-issues", EditionStatus.NEXT),
    ("archived-issues", EditionStatus.EXPIRED),
)


def links(html: str, base: str) -> list[tuple[str, str]]:
    """``(absolute url, link text)`` for every anchor."""
    out = []
    for match in _ANCHOR.finditer(html):
        text = re.sub(r"<[^>]+>", " ", match.group(2))
        out.append(
            (urllib.parse.urljoin(base, match.group(1)), " ".join(text.split()))
        )
    return out


def referenced_pages(text: str, base: str) -> list[str]:
    """Every document path the text quotes anywhere, made absolute.

    Deliberately not an HTML parse. An eAIP index is a frameset whose menu is
    assembled by ``menu.js``, so the contents tree lives in a script rather
    than in markup and an anchor-only reader comes back with nothing.
    """
    seen: dict[str, None] = {}
    for match in _HREFISH.finditer(text):
        seen.setdefault(urllib.parse.urljoin(base, match.group(1)), None)
    return list(seen)


def _effective_from_path(url: str) -> date | None:
    """The effective date an eAIP path carries, if it carries one.

    A EUROCONTROL path holds two dates meaning different things:
    ``/AIP/03-SEP-2026/AIP-30/2026-10-01-000000/html/`` was *published* on 3
    September and takes *effect* on 1 October. The ISO-with-time field is
    effect; the other is publication.
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


def _section_code(url: str) -> str:
    """``"ENR 3.2"`` from ``QA-ENR-3.2-en-GB.html``, or ``""``."""
    name = url.rsplit("/", 1)[-1].split("?")[0].split("#")[0]
    name = re.sub(r"\.html?$", "", name, flags=re.I)
    name = re.sub(r"-[a-z]{2}-[A-Z]{2}$", "", name)
    match = re.search(
        r"(?:^|[-_])(GEN|ENR|AD)[-_ ]?(\d+)(?:[.\-_](\d+))?(?:[-_]([A-Z]{4}))?",
        name,
        re.I,
    )
    if match is None:
        return ""
    part, chapter, ordinal, aerodrome = match.groups()
    code = f"{part.upper()} {int(chapter)}"
    if ordinal is not None:
        code += f".{int(ordinal)}"
    if aerodrome:
        code += f" {aerodrome.upper()}"
    return code


@dataclass
class EaipTraversal:
    """Walking a EUROCONTROL eAIP: frameset, menu, then the documents.

    Shared because the toolchain is shared — a State publishing an eAIP at all
    most likely publishes this one. A State that has customised it past
    recognition subclasses or replaces this rather than bending it.

    The walk stops at the first level that yields documents, so a working AIP
    costs one extra request rather than a crawl of the site.
    """

    depth: int = 3
    budget: int = 40
    """Pages opened while looking. A bound, not a target."""

    def editions_on(self, html: str, base: str) -> tuple[Edition, ...]:
        """Editions a history page lists, with the State's own labels.

        Where the page files editions under tables that say what they are, the
        declaration is used. Where it does not, status is ``UNDECLARED`` and
        the effective date from the path is all there is — which is honest,
        and leaves :meth:`Edition.in_force_on` free to answer ``None``.
        """
        declared: list[Edition] = []
        seen: set[str] = set()
        for match in _STATUS_TABLE.finditer(html):
            classes, body = match.group(1).lower(), match.group(2)
            status = next(
                (s for marker, s in _DECLARED_STATUS if marker in classes), None
            )
            if status is None:
                continue
            for url, text in links(body, base):
                if not re.search(r"index[^/]*\.html?$", url, re.I) or url in seen:
                    continue
                seen.add(url)
                declared.append(
                    Edition(
                        index_url=url,
                        status=status,
                        label=text,
                        effective_on=_effective_from_path(url),
                    )
                )
        if declared:
            return tuple(declared)

        return tuple(
            Edition(
                index_url=url,
                status=EditionStatus.UNDECLARED,
                label=text,
                effective_on=_effective_from_path(url),
            )
            for url, text in links(html, base)
            if re.search(r"index[^/]*\.html?$", url, re.I)
        )

    def contents_of(self, edition: Edition, read: Read) -> tuple[str, str]:
        """``(html, base url)`` of the page that actually lists the documents.

        For a frameset that is two or three hops in. Returns the index itself
        where the index already lists them, so a State without frames costs
        nothing extra.
        """
        base = edition.index_url
        html = read(base).decode("utf-8", "replace")
        if self._documents_on(html, base):
            return (html, base)

        seen = {base}
        frontier = self._navigation_from(html, base)
        for _ in range(self.depth):
            frontier = [u for u in frontier if u not in seen]
            if not frontier:
                break
            following: list[str] = []
            for target in frontier:
                if len(seen) >= self.budget:
                    break
                seen.add(target)
                try:
                    inner = read(target).decode("utf-8", "replace")
                except Exception:  # noqa: BLE001 — a dead frame is not fatal
                    continue
                if self._documents_on(inner, target):
                    return (inner, target)
                following.extend(self._navigation_from(inner, target))
            frontier = following
        return (html, base)

    def publications_on(
        self, html: str, base: str, edition: Edition
    ) -> tuple[Publication, ...]:
        """Every document a contents page names, typed by kind."""
        found: dict[str, Publication] = {}
        for url in referenced_pages(html, base):
            kind = kind_of(url)
            if kind is Kind.NAVIGATION:
                continue
            code = _section_code(url) if kind is Kind.AIP_SECTION else ""
            found.setdefault(
                url,
                Publication(url=url, kind=kind, edition=edition, code=code),
            )
        return tuple(found.values())

    def companion_indexes(self, html: str, base: str) -> dict[str, str]:
        """The supplement, circular and amendment lists a contents page names.

        Separate from :meth:`publications_on` because these are *lists* of
        documents, not documents. Missing them costs the supplement layer
        entirely, and precedence runs AIP < AMDT < SUP < NOTAM — so an AIP
        gathered without them reads as though nothing supersedes it.
        """
        found: dict[str, str] = {}
        for url, text in links(html, base):
            name = url.rsplit("/", 1)[-1].split("#")[0]
            if not name.lower().endswith((".html", ".htm")):
                continue
            if not re.search(r"(?:AMDT|eSUPs?|eAICs?)", name, re.I):
                continue
            label = re.sub(
                r"^[A-Z]{2}-|-[a-z]{2}-[A-Z]{2}\.html?$", "", name, flags=re.I
            )
            found.setdefault(label or text or name, url)
        return found

    def documents_beside(self, html: str, base: str) -> tuple[str, ...]:
        """Documents a list page links to *in its own directory*.

        Scoped deliberately: a supplement list links to its supplements and
        also back to the AIP menu, and following the latter walks the whole
        AIP a second time.
        """
        home = base.rsplit("/", 1)[0] + "/"
        out: dict[str, None] = {}
        for url in referenced_pages(html, base):
            if not url.startswith(home):
                continue
            tail = url[len(home):]
            if "/" in tail or url == base:
                continue
            if kind_of(url) is Kind.NAVIGATION:
                continue
            out.setdefault(url, None)
        return tuple(out)

    # -- internals ---------------------------------------------------------

    def _documents_on(self, html: str, base: str) -> bool:
        return any(
            kind_of(u) is Kind.AIP_SECTION for u in referenced_pages(html, base)
        )

    def _navigation_from(self, html: str, base: str) -> list[str]:
        """Where to look next when a page names no documents.

        Frames first — an eAIP index is a frameset and carries no anchors at
        all — then scripts, because a menu built in JavaScript holds the
        contents tree in a ``.js`` file rather than in markup.
        """
        out: dict[str, None] = {}
        for pattern in (_FRAME, _SCRIPT):
            for match in pattern.finditer(html):
                out.setdefault(urllib.parse.urljoin(base, match.group(1)), None)
        for url in referenced_pages(html, base):
            if kind_of(url) is Kind.NAVIGATION:
                out.setdefault(url, None)
        return list(out)

"""Qatar's route to its publications, against the pages Qatar actually served.

Every fixture here is unmodified markup retrieved from aim.gov.qa on
07 SEP 2026. That matters more than usual: the inferred description of Qatar's
layout that this repository carried before those pages arrived was wrong in
three separate ways, and no amount of reading the eAIP specification would have
found any of them.
"""

from __future__ import annotations

from collections import Counter
from datetime import date
from pathlib import Path

import pytest

from aeropub.publication import Edition, EditionStatus, Kind, Publication, kind_of
from aeropub.resolve import EaipTraversal, Resolver
from aeropub.states.qatar import RESOLVER, QatarResolver

FIXTURES = Path(__file__).parent / "fixtures" / "qatar"
HOST = "https://aim.gov.qa"
EDITION = f"{HOST}/AIP/03-SEP-2026/AIP-30/2026-10-01-000000/html"

SERVED = {
    f"{HOST}/AIP/QA-history-en-GB.html": "history-en-GB.html",
    f"{EDITION}/index-en-GB.html": "index-en-GB.html",
    f"{EDITION}/eAIP/QA-menu-en-GB.html": "QA-menu-en-GB.html",
}


@pytest.fixture()
def read():
    """Qatar's own pages, and a refusal for anything we did not capture.

    The refusal is deliberate. A resolver that only works when every page it
    reaches for is present is a resolver that has never met a real site.
    """
    def _read(url: str) -> bytes:
        name = SERVED.get(url)
        if name is None:
            raise FileNotFoundError(url)
        return (FIXTURES / name).read_bytes()

    return _read


@pytest.fixture()
def next_edition(read):
    return next(
        e for e in RESOLVER.editions(read) if e.status is EditionStatus.NEXT
    )


class TestTheContract:

    def test_qatar_meets_it(self):
        assert isinstance(RESOLVER, Resolver)

    def test_only_one_address_is_known(self):
        """The amendment number AIP-30 cannot be derived from the AIRAC cycle,
        so an edition URL is followed, never built."""
        assert RESOLVER.entry_point == f"{HOST}/AIP/QA-history-en-GB.html"


class TestTheEditionsQatarDeclares:

    def test_both_are_found(self, read):
        assert len(RESOLVER.editions(read)) == 2

    def test_qatars_own_labels_are_used(self, read):
        statuses = {e.status for e in RESOLVER.editions(read)}
        assert statuses == {EditionStatus.CURRENT, EditionStatus.NEXT}

    def test_the_newest_published_is_not_the_one_in_force(self, read):
        """Qatar published AIP-30 on 03 SEP to take effect 01 OCT. For the
        whole of September the newest edition is not the current one."""
        editions = {e.status: e for e in RESOLVER.editions(read)}
        assert editions[EditionStatus.CURRENT].effective_on == date(2026, 8, 6)
        assert editions[EditionStatus.NEXT].effective_on == date(2026, 10, 1)

    def test_in_force_answers_for_the_day_asked(self, read):
        editions = {e.status: e for e in RESOLVER.editions(read)}
        on_7_sep = date(2026, 9, 7)
        assert editions[EditionStatus.CURRENT].in_force_on(on_7_sep) is True
        assert editions[EditionStatus.NEXT].in_force_on(on_7_sep) is False
        assert editions[EditionStatus.NEXT].in_force_on(date(2026, 10, 1)) is True

    def test_the_nil_archive_row_is_not_an_edition(self, read):
        """Qatar's expired-issues table holds a NIL row, not a link. It must
        not become an edition with no URL."""
        assert all(e.index_url for e in RESOLVER.editions(read))
        assert not [
            e for e in RESOLVER.editions(read) if e.status is EditionStatus.EXPIRED
        ]


class TestReachingTheContents:
    """The index is a frameset. This is the hop that did not exist before."""

    def test_the_index_names_no_sections(self, read):
        html = (FIXTURES / "index-en-GB.html").read_text()
        walk = EaipTraversal()
        assert not walk._documents_on(html, f"{EDITION}/index-en-GB.html")

    def test_the_walk_reaches_the_menu(self, read, next_edition):
        _, base = EaipTraversal().contents_of(next_edition, read)
        assert base == f"{EDITION}/eAIP/QA-menu-en-GB.html"

    def test_a_missing_frame_does_not_end_the_walk(self, read, next_edition):
        """commands-en-GB.html is reached for and not held. The walk continues."""
        contents, _ = EaipTraversal().contents_of(next_edition, read)
        assert "QA-GEN-0.1-en-GB.html" in contents


class TestWhatWasFound:

    def test_eighty_sections_and_three_lists(self, read, next_edition):
        pubs = RESOLVER.publications(next_edition, read)
        assert len(pubs) == 83
        assert sum(1 for p in pubs if p.kind is Kind.AIP_SECTION) == 80

    def test_the_sections_and_the_three_lists(self, read, next_edition):
        kinds = Counter(p.kind for p in RESOLVER.publications(next_edition, read))
        assert kinds == {Kind.AIP_SECTION: 80, Kind.INDEX: 3}

    def test_all_three_parts_are_present(self, read, next_edition):
        codes = {p.code for p in RESOLVER.publications(next_edition, read)}
        assert "GEN 0.4" in codes
        assert "ENR 3.2" in codes
        assert "AD 2 OTHH" in codes

    def test_both_aerodromes(self, read, next_edition):
        codes = {p.code for p in RESOLVER.publications(next_edition, read)}
        assert {"AD 2 OTBD", "AD 2 OTHH"} <= codes

    def test_every_publication_is_identified(self, read, next_edition):
        """A citation reading as a bare filename means we never worked out
        what the document is."""
        assert all(p.is_identified for p in RESOLVER.publications(next_edition, read))

    def test_citations_name_the_document(self, read, next_edition):
        pubs = {p.code: p for p in RESOLVER.publications(next_edition, read)}
        assert pubs["ENR 3.2"].cite_as("Qatar") == "AIP Qatar ENR 3.2"


class TestAListIsNotASupplement:
    """Typing the menu's tabs by their directory would file three index pages
    as an AMDT, a SUP and an AIC — documents a parser would then read values
    out of. They are their own kind: no precedence, nothing parses them, and
    they are monitored, because a State whose SUP list is unreachable is a
    State whose supplements we cannot know about."""

    def test_the_companion_indexes_are_found(self, read, next_edition):
        contents, base = EaipTraversal().contents_of(next_edition, read)
        found = EaipTraversal().companion_indexes(contents, base)
        assert set(found) == {"AMDT", "eSUPs", "eAICs"}

    def test_they_are_published_as_indexes(self, read, next_edition):
        kinds = {
            p.code: p.kind
            for p in RESOLVER.publications(next_edition, read)
            if "list" in p.code
        }
        assert kinds == {
            "AMDT list": Kind.INDEX,
            "eSUPs list": Kind.INDEX,
            "eAICs list": Kind.INDEX,
        }

    def test_an_index_is_never_filed_under_a_precedence_layer(self, read, next_edition):
        """Filing a table of contents as a supplement puts it above the AIP."""
        for publication in RESOLVER.publications(next_edition, read):
            if publication.kind is Kind.INDEX:
                assert publication.precedence is None

    def test_every_section_still_has_one(self, read, next_edition):
        sections = [
            p for p in RESOLVER.publications(next_edition, read)
            if p.kind is Kind.AIP_SECTION
        ]
        assert len(sections) == 80
        assert all(p.precedence is not None for p in sections)

    def test_a_labelled_index_beats_an_unlabelled_one(self, read, next_edition):
        """publications_on emits the same URLs typed from their paths and with
        no code. De-duplication keeps whichever arrived first, so a failing SUP
        list must report as 'eSUPs list' rather than as a blank."""
        pubs = {p.url: p for p in RESOLVER.publications(next_edition, read)}
        sup_list = next(p for p in pubs.values() if "eSUPs" in p.url)
        assert sup_list.code == "eSUPs list"


class TestKindDecidesPrecedence:
    """Precedence runs AIP < AMDT < SUP < NOTAM. Filing a supplement as an AIP
    section puts it underneath the thing it is meant to override."""

    @pytest.mark.parametrize(
        "url,kind",
        [
            (f"{EDITION}/eAIP/QA-ENR-3.2-en-GB.html", Kind.AIP_SECTION),
            (f"{EDITION}/eSUP/QA-SUP-16-2026-en-GB.html", Kind.SUPPLEMENT),
            (f"{EDITION}/eAIC/QA-AIC-07-2026-A-en-GB.html", Kind.CIRCULAR),
            (f"{EDITION}/eAIP/QA-AMDT-en-GB.html", Kind.INDEX),
            (f"{EDITION}/eSUP/QA-eSUPs-en-GB.html", Kind.INDEX),
            (f"{EDITION}/eSUP/QA-SUP-16-2026-en-GB.html", Kind.SUPPLEMENT),
            (f"{EDITION}/graphics/OTHH-ADC.pdf", Kind.CHART),
            (f"{EDITION}/index-en-GB.html", Kind.NAVIGATION),
        ],
    )
    def test_kinds_are_read_from_the_path(self, url, kind):
        assert kind_of(url) is kind

    def test_a_chart_beside_supplements_is_still_a_chart(self):
        """eSUP/chart.pdf is a graphic that happens to live beside supplements.
        Reading it as a supplement files an image under a precedence layer."""
        assert kind_of(f"{EDITION}/eSUP/OTHH-diagram.pdf") is Kind.CHART

    def test_an_unrecognised_document_is_not_an_aip_section(self):
        """Defaulting to AIP_SECTION defaults to the layer everything else
        overrides — the quietest possible way to be wrong."""
        assert kind_of(f"{EDITION}/something-else.html") is Kind.UNKNOWN

    def test_an_unknown_kind_has_no_precedence(self):
        assert Kind.UNKNOWN.precedence is None
        assert not Kind.UNKNOWN.carries_values

    def test_a_chart_carries_no_values_so_is_never_kept(self):
        assert not Kind.CHART.carries_values


class TestAnUndeclaredEditionIsNotCurrent:

    def test_status_is_undeclared_not_assumed(self):
        edition = Edition(index_url="https://x.test/index.html")
        assert edition.status is EditionStatus.UNDECLARED

    def test_in_force_is_unknown_not_false(self):
        """An edition of unknown status is a reason to stop and look, not a
        reason to skip. False would be silently skipping it."""
        edition = Edition(index_url="https://x.test/index.html")
        assert edition.in_force_on(date(2026, 9, 7)) is None

    def test_a_date_alone_still_answers(self):
        edition = Edition(
            index_url="https://x.test/index.html", effective_on=date(2026, 8, 6)
        )
        assert edition.in_force_on(date(2026, 9, 7)) is True


class TestTheStateDeclaresItsOwnAbsences:
    """A eAIP menu writes [NIL] beside a section the State has nothing to
    publish in. That is the strongest evidence a coverage board can have and
    the opposite of a gap: the State was asked and answered.
    """

    def test_qatar_declares_eighteen(self, read, next_edition):
        pubs = RESOLVER.publications(next_edition, read)
        assert sum(1 for p in pubs if p.declared_empty) == 18

    def test_the_page_checklist_is_one_of_them(self, read, next_edition):
        """GEN 0.4 is the reconciliation source checklist.py was built for.
        Qatar publishes it as NIL, so that reconciliation is not available
        for this State — which is a fact about Qatar, not a gap in us."""
        pubs = {p.code: p for p in RESOLVER.publications(next_edition, read)}
        assert pubs["GEN 0.4"].declared_empty

    def test_conventional_routes_are_declared_empty(self, read, next_edition):
        """Qatar publishes RNAV routes only. A parser finding nothing in
        ENR 3.1 has succeeded."""
        pubs = {p.code: p for p in RESOLVER.publications(next_edition, read)}
        assert pubs["ENR 3.1"].declared_empty
        assert not pubs["ENR 3.2"].declared_empty

    def test_a_section_with_content_is_not_marked(self, read, next_edition):
        pubs = {p.code: p for p in RESOLVER.publications(next_edition, read)}
        assert not pubs["ENR 4.4"].declared_empty
        assert not pubs["AD 2 OTHH"].declared_empty

    def test_the_marker_is_read_despite_the_fragment(self, read, next_edition):
        """A menu anchor names an element within a page. Keying the NIL set on
        the raw href builds two sets that never intersect, and every section
        reads as ordinary — which is how this was wrong the first time."""
        pubs = {p.code: p for p in RESOLVER.publications(next_edition, read)}
        assert pubs["ENR 5.6"].declared_empty

    def test_a_declared_absence_shows_in_the_description(self, read, next_edition):
        pubs = {p.code: p for p in RESOLVER.publications(next_edition, read)}
        assert "declared NIL" in pubs["ENR 3.1"].describe()


class TestSupplementsAndCircularsAreMonitored:
    """Precedence runs AIP < AMDT < SUP < NOTAM. A supplement in force changes
    what a section means, so an AIP gathered without its supplements reads as
    though nothing supersedes it — and Qatar's own edition title names nine.
    """

    SUPS = (
        "https://aim.gov.qa/AIP/03-SEP-2026/AIP-30/2026-10-01-000000/html/"
        "eSUP/QA-eSUPs-en-GB.html"
    )
    LISTING = (
        b'<html><a href="QA-SUP-16-2026-en-GB.html">SUP 16/2026</a>'
        b'<a href="QA-SUP-15-2026-en-GB.html">SUP 15/2026</a></html>'
    )

    @pytest.fixture()
    def read_with_sups(self):
        def _read(url: str) -> bytes:
            name = SERVED.get(url)
            if name is not None:
                return (FIXTURES / name).read_bytes()
            if url == self.SUPS:
                return self.LISTING
            raise FileNotFoundError(url)
        return _read

    def test_the_lists_are_published_not_only_followed(self, read, next_edition):
        """A State whose SUP list is unreachable is a State whose supplements
        we cannot know about, and that must be a document that failed."""
        codes = {p.code for p in RESOLVER.publications(next_edition, read)}
        assert {"eSUPs list", "eAICs list", "AMDT list"} <= codes

    def test_a_list_is_typed_as_an_index(self, read, next_edition):
        pubs = {p.code: p for p in RESOLVER.publications(next_edition, read)}
        assert pubs["eSUPs list"].kind is Kind.INDEX

    def test_an_index_carries_no_values(self):
        """Nothing may parse a table of contents for aeronautical data."""
        assert not Kind.INDEX.carries_values
        assert Kind.INDEX.precedence is None

    def test_but_it_is_kept(self):
        """It is the evidence of which supplements existed at a date."""
        assert Kind.INDEX.must_be_kept

    def test_supplements_behind_the_list_are_found(self, read_with_sups, next_edition):
        codes = {p.code for p in RESOLVER.publications(next_edition, read_with_sups)}
        assert {"SUP 16/2026", "SUP 15/2026"} <= codes

    def test_a_supplement_outranks_the_aip(self, read_with_sups, next_edition):
        from aeropub.facts import Precedence

        pubs = {p.code: p for p in RESOLVER.publications(next_edition, read_with_sups)}
        assert pubs["SUP 16/2026"].precedence is Precedence.SUP
        assert pubs["SUP 16/2026"].precedence > pubs["ENR 3.2"].precedence


class TestACircularIsKeptEvenThoughItOverridesNothing:
    """An AIC carries no values and is still a document the State issued —
    small, textual, and something an operator may need to read a year after
    the State withdrew it. Treating it like a chart left it unarchived."""

    def test_it_has_no_precedence(self):
        assert Kind.CIRCULAR.precedence is None

    def test_it_is_still_kept(self):
        assert Kind.CIRCULAR.must_be_kept

    def test_a_chart_is_not(self):
        """What a State serves better than we can: large, no deduplication,
        and wanted from the authority that drew it."""
        assert not Kind.CHART.must_be_kept

    def test_retention_follows_being_kept_not_carrying_values(self):
        from aeropub.live import Retention, retention_for

        assert retention_for("text/html", must_be_kept=True) is Retention.ARCHIVED
        assert retention_for("text/html", must_be_kept=False) is Retention.LINKED

"""What changed inside a document, when no parser has read it yet.

aeropub.changes compares effective states, which is the comparison that tells
the operational truth and has one blind spot: a document nothing parses
produces no facts, so a State can amend a section, the cycle can archive both
versions, and the change board can say nothing changed. Most of an AIP is in
that state for most States and will be for years.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from aeropub.revision import (
    DocumentRevision,
    RevisionKind,
    compare,
    extract_text,
)

FIXTURE = Path(__file__).parent / "fixtures" / "qatar" / "QA-menu-en-GB.html"


@pytest.fixture(scope="module")
def menu() -> bytes:
    return FIXTURE.read_bytes()


@pytest.fixture(scope="module")
def regenerated(menu: bytes) -> bytes:
    """The same AIP rebuilt: every element id moves, not one word does."""
    out = re.sub(
        rb'id="i(\d+)"',
        lambda m: b'id="i' + str(int(m.group(1)) + 900000).encode() + b'"',
        menu,
    )
    return re.sub(
        rb'href="#i(\d+)"',
        lambda m: b'href="#i' + str(int(m.group(1)) + 900000).encode() + b'"',
        out,
    )


class TestReadingTheText:

    def test_markup_is_removed(self):
        assert extract_text(b"<p>ENR 3.2 <b>Area</b> navigation</p>") == (
            "ENR 3.2 Area navigation",
        )

    def test_a_stylesheet_change_is_not_a_text_change(self):
        """Otherwise a State restyling its eAIP amends every page at once."""
        before = b"<style>.a{color:red}</style><p>ENR 3.2</p>"
        after = b"<style>.a{color:blue}</style><p>ENR 3.2</p>"
        assert extract_text(before) == extract_text(after)

    def test_scripts_are_not_read(self):
        assert extract_text(b"<script>var t=1</script><p>ENR 3.2</p>") == ("ENR 3.2",)

    def test_entities_are_resolved(self):
        assert extract_text(b"<p>4850&nbsp;m &amp; rising</p>") == ("4850 m & rising",)

    def test_blank_lines_are_dropped(self):
        assert extract_text(b"<p>a</p><p>  </p><p>b</p>") == ("a", "b")

    def test_qatars_menu_reads_as_text(self, menu):
        lines = extract_text(menu)
        assert any("ENR 3.2" in line for line in lines)
        assert len(lines) > 100


class TestRegeneratedIsNotAmended:
    """A change detector built on hashes alone reports eighty sections amended
    on a day nothing was amended. An operator who sees that twice stops
    reading the board, which costs more than the feature was worth."""

    def test_every_byte_moved(self, menu, regenerated):
        assert menu != regenerated

    def test_and_it_is_not_an_amendment(self, menu, regenerated):
        assert compare("AIP Qatar menu", menu, regenerated).kind is RevisionKind.REGENERATED

    def test_it_is_not_substantive(self, menu, regenerated):
        assert not compare("AIP Qatar menu", menu, regenerated).is_substantive

    def test_the_wording_says_so_plainly(self, menu, regenerated):
        text = compare("AIP Qatar menu", menu, regenerated).describe()
        assert "not one word did" in text

    def test_the_line_count_is_unchanged(self, menu, regenerated):
        revision = compare("AIP Qatar menu", menu, regenerated)
        assert revision.lines_before == revision.lines_after


class TestARealAmendment:

    def _amended(self, menu: bytes) -> bytes:
        return menu.replace(
            b'<a href="QA-ENR-5.6-en-GB.html',
            b'<a href="QA-ENR-5.7-en-GB.html#i9001" id="i9001">'
            b'ENR 5.7 Bird migration</a><a href="QA-ENR-5.6-en-GB.html',
            1,
        )

    def test_it_is_amended(self, menu):
        assert compare("m", menu, self._amended(menu)).kind is RevisionKind.AMENDED

    def test_the_added_text_is_named(self, menu):
        revision = compare("m", menu, self._amended(menu))
        assert any("ENR 5.7" in line for line in revision.added)

    def test_it_is_substantive(self, menu):
        assert compare("m", menu, self._amended(menu)).is_substantive


class TestWhoHasToLook:

    def test_an_unparsed_amendment_needs_a_person(self, menu):
        """The text says that something changed and roughly where. Only a
        reader or a parser says what it means operationally."""
        amended = menu.replace(b"ENR 3.2", b"ENR 3.2 REVISED", 1)
        assert compare("m", menu, amended).needs_a_person

    def test_a_parsed_one_does_not(self, menu):
        amended = menu.replace(b"ENR 3.2", b"ENR 3.2 REVISED", 1)
        revision = compare("m", menu, amended, facts_were_read=True)
        assert not revision.needs_a_person

    def test_a_regeneration_never_needs_a_person(self, menu, regenerated):
        assert not compare("m", menu, regenerated).needs_a_person

    def test_the_report_says_nothing_read_it(self, menu):
        amended = menu.replace(b"ENR 3.2", b"ENR 3.2 REVISED", 1)
        assert "real and unread" in compare("m", menu, amended).describe()


class TestFirstSeenIsNotUnchanged:
    """'We have never seen this' and 'this is the same as last time' mean
    opposite things about coverage."""

    def test_no_previous_version(self, menu):
        assert compare("m", None, menu).kind is RevisionKind.FIRST_SEEN

    def test_it_is_not_reported_as_a_change(self, menu):
        assert not compare("m", None, menu).is_substantive

    def test_identical_bytes_are_unchanged(self, menu):
        assert compare("m", menu, menu).kind is RevisionKind.UNCHANGED

    def test_the_two_read_differently(self, menu):
        assert compare("m", None, menu).describe() != compare("m", menu, menu).describe()

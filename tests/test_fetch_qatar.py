"""Finding Qatar's current eAIP edition by following links, not building URLs.

Qatar changed its eAIP layout between 2025 and 2026. The current path carries
a running amendment number that cannot be derived from the AIRAC cycle — three
of its four fields fall out of the calendar and the amendment number does not.
A constructed URL is therefore a guess that goes stale silently, which is the
worst way for a fetch to fail.

So the fetcher starts at the published edition history and follows links. What
is asserted here is that it survives both layouts Qatar has used, ignores the
links that are not editions, and says a section is missing rather than
inventing a path to it.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

TOOL = Path(__file__).parent.parent / "tools" / "fetch_qatar_aip.py"


@pytest.fixture(scope="module")
def tool():
    spec = importlib.util.spec_from_file_location("fetch_qatar_aip", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


HISTORY = """<html><body><table>
<tr><td>AIP AMDT 30</td><td><a
 href="/AIP/03-SEP-2026/AIP-30/2026-10-01-000000/html/index-en-GB.html">01 OCT 2026</a></td></tr>
<tr><td>AIP AMDT 29</td><td><a
 href="/AIP/09-JUL-2026/AIP-29/2026-08-06-000000/html/index-en-GB.html">06 AUG 2026</a></td></tr>
<tr><td>AIP AMDT 28</td><td><a
 href="/eaip/2026-06-11-AIRAC/html/index-en-GB.html">11 JUN 2026</a></td></tr>
<tr><td>Contact</td><td><a href="/contact.html">here</a></td></tr>
</table></body></html>"""

INDEX = """<html><body>
<a href="QA-ENR-4.4-en-GB.html">ENR 4.4</a>
<a href="QA-ENR-3.2-en-GB.html">ENR 3.2</a>
<a href="QA-AD-2-OTHH-en-GB.html">OTHH</a>
</body></html>"""

BASE = "https://aim.gov.qa/AIP/03-SEP-2026/AIP-30/2026-10-01-000000/html/index-en-GB.html"


class TestFindingEditions:
    def test_every_edition_is_found(self, tool):
        assert len(tool.editions(HISTORY, tool.HISTORY)) == 3

    def test_both_layouts_qatar_has_used_are_recognised(self, tool):
        """The 2026 /AIP/<published>/AIP-<n>/<effective>/ shape and the older
        /eaip/<effective>-AIRAC/ one."""
        urls = [u for u, _ in tool.editions(HISTORY, tool.HISTORY)]
        assert any("/AIP-30/" in u for u in urls)
        assert any("-AIRAC/" in u for u in urls)

    def test_a_link_that_is_not_an_edition_is_ignored(self, tool):
        urls = [u for u, _ in tool.editions(HISTORY, tool.HISTORY)]
        assert not any("contact" in u for u in urls)

    def test_the_newest_comes_first(self, tool):
        """By the effective date in the path, not by page order — a history
        page may list oldest first."""
        first = tool.editions(HISTORY, tool.HISTORY)[0][0]
        assert "2026-10-01" in first

    def test_relative_links_become_absolute(self, tool):
        for url, _ in tool.editions(HISTORY, tool.HISTORY):
            assert url.startswith("https://aim.gov.qa/")

    def test_a_page_with_no_editions_yields_none(self, tool):
        """Rather than a guess. The caller then asks for the page itself."""
        assert tool.editions("<html><body>nothing here</body></html>", tool.HISTORY) == []


class TestFindingSections:
    def test_a_section_is_located_through_the_state_prefix(self, tool):
        """Qatar names them QA-ENR-4.4-en-GB.html, not ENR-4.4.html."""
        found = tool.sections_on(INDEX, BASE, ("ENR-4.4",))
        assert found["ENR-4.4"].endswith("QA-ENR-4.4-en-GB.html")

    def test_a_section_not_on_the_index_is_absent_not_invented(self, tool):
        found = tool.sections_on(INDEX, BASE, ("ENR-4.4", "ENR-5.1"))
        assert "ENR-4.4" in found
        assert "ENR-5.1" not in found

    def test_a_near_miss_does_not_match(self, tool):
        """ENR-4.4 must not match ENR-4.40, the way a prefix would."""
        page = '<a href="QA-ENR-4.40-en-GB.html">x</a>'
        assert tool.sections_on(page, BASE, ("ENR-4.4",)) == {}

    def test_the_quick_set_puts_coordinates_first(self, tool):
        """Without ENR 4.4 the route structure draws as a list of names."""
        assert tool.QUICK_SECTIONS[0] == "ENR-4.4"


FULL_INDEX = """<html><body>
<a href="QA-GEN-0.4-en-GB.html">GEN 0.4</a>
<a href="QA-GEN-2.1-en-GB.html">GEN 2.1</a>
<a href="QA-ENR-4.4-en-GB.html">ENR 4.4</a>
<a href="QA-ENR-10.1-en-GB.html">ENR 10.1</a>
<a href="QA-ENR-3.2-en-GB.html">ENR 3.2</a>
<a href="QA-AD-2-OTHH-en-GB.html">OTHH</a>
<a href="QA-AD-2-OTBD-en-GB.html">OTBD</a>
<a href="index-en-GB.html">index</a>
<a href="QA-menu-en-GB.html">menu</a>
<a href="/contact.html">contact us</a>
<a href="QA-history-en-GB.html">history</a>
</body></html>"""


class TestTheWholeAip:
    """The default is every section, not a chosen few. An AIP platform that
    holds five sections of an AIP holds five sections of an AIP."""

    def test_every_section_is_discovered(self, tool):
        found = tool.all_sections_on(FULL_INDEX, BASE)
        assert set(found) == {
            "GEN-0.4", "GEN-2.1", "ENR-4.4", "ENR-10.1", "ENR-3.2",
            "AD-2-OTHH", "AD-2-OTBD",
        }

    def test_navigation_pages_are_not_sections(self, tool):
        """The index, the menu, the history and a contact page are not AIP
        content and would each parse as an empty section."""
        found = tool.all_sections_on(FULL_INDEX, BASE)
        assert not any(
            k.lower().startswith(("index", "menu", "history", "contact"))
            for k in found
        )

    def test_per_aerodrome_pages_are_kept_apart(self, tool):
        """AD 2 is published once per aerodrome, and they are different
        documents."""
        found = tool.all_sections_on(FULL_INDEX, BASE)
        assert found["AD-2-OTHH"] != found["AD-2-OTBD"]

    def test_the_state_prefix_and_language_suffix_are_stripped(self, tool):
        """QA-ENR-4.4-en-GB.html files under ENR-4.4, so a code means the same
        thing across States."""
        assert "ENR-4.4" in tool.all_sections_on(FULL_INDEX, BASE)

    def test_a_page_that_is_not_an_aip_section_is_left_out(self, tool):
        page = '<a href="QA-styles-en-GB.html">styles</a>'
        assert tool.all_sections_on(page, BASE) == {}


class TestOrdering:
    def test_sections_come_out_in_aip_order(self, tool):
        """GEN, then ENR, then AD — where a reader expects them."""
        codes = ["AD-1.1", "ENR-3.2", "GEN-0.4"]
        assert sorted(codes, key=tool._sort_key) == ["GEN-0.4", "ENR-3.2", "AD-1.1"]

    def test_numbers_sort_as_numbers(self, tool):
        """ENR 3.2 before ENR 10.1, which sorting as text gets backwards."""
        codes = ["ENR-10.1", "ENR-3.2"]
        assert sorted(codes, key=tool._sort_key) == ["ENR-3.2", "ENR-10.1"]


class TestCourtesy:
    def test_there_is_a_delay_between_requests(self, tool):
        """A full AIP is a hundred and more pages and a State's AIM server is
        not a CDN."""
        assert tool.POLITE_DELAY > 0


class TestLinks:
    def test_link_text_travels_with_the_url(self, tool):
        found = dict(
            (text, url) for url, text in tool.links(HISTORY, tool.HISTORY)
        )
        assert "01 OCT 2026" in found

    def test_markup_inside_a_link_is_stripped(self, tool):
        page = '<a href="/x.html">ENR <b>4.4</b> points</a>'
        assert tool.links(page, BASE)[0][1] == "ENR 4.4 points"

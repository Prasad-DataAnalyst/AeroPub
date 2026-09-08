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


#: What Qatar's index actually served on 07 SEP 2026, unmodified. It is a
#: frameset: no anchors at all, and the menu is assembled by menu.js. An
#: anchor-only reader comes back empty here, which is how the first run
#: failed — with the whole AIP behind a page it could not see into.
REAL_FRAMESET = """<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.0 Frameset//EN"
 "http://www.w3.org/TR/xhtml1/DTD/xhtml1-frameset.dtd">
<html xmlns="http://www.w3.org/1999/xhtml">
	<head>
		<TITLE>eAIP - English version</TITLE>
		<script type="text/javascript">
            if (screen.width <= 720) {
                window.location = "index-mobile-en-GB.html";
            }
            </script>
		<script src="menu.js" type="text/javascript"></script>
		<script src="amendments.js" type="text/javascript"></script>
		<script src="commands.js" type="text/javascript"></script>
	</head>
	<frameset cols="320,*" onload="openTarget()">
		<frameset rows="100, *" border="0" frameSpacing="0" frameBorder="0">
			<frame name="eAISCommands" src="commands-en-GB.html" scrolling="no">
			<frame name="eAISNavigation" src="eAIP/QA-menu-en-GB.html" scrolling="yes">
		</frameset>
		<frame name="eAISContent" src="QA-cover-en-GB.html">
	</frameset>
</html>"""

#: A menu built in JavaScript rather than markup, which is what menu.js is
#: for. There is not an anchor in it.
MENU_JS = """
var tree = new Array();
tree[0] = new Node("GEN 0.4", "eAIP/QA-GEN-0.4-en-GB.html");
tree[1] = new Node("ENR 3.2", 'eAIP/QA-ENR-3.2-en-GB.html');
tree[2] = new Node("AD 2 OTHH", "eAIP/QA-AD-2-OTHH-en-GB.html");
"""


class TestAFrameset:
    """The index is a frameset. Reading only anchors finds nothing at all."""

    def test_a_frameset_has_no_sections_of_its_own(self, tool):
        assert tool.all_sections_on(REAL_FRAMESET, BASE) == {}

    def test_the_navigation_frame_is_followed(self, tool):
        following = tool._follow_from(REAL_FRAMESET, BASE)
        assert any(url.endswith("eAIP/QA-menu-en-GB.html") for url in following)

    def test_the_menu_script_is_followed_too(self, tool):
        """The tree may be in the script, not the page it names."""
        following = tool._follow_from(REAL_FRAMESET, BASE)
        assert any(url.endswith("menu.js") for url in following)

    def test_sections_are_read_out_of_javascript(self, tool):
        found = tool.all_sections_on(MENU_JS, BASE)
        assert found["GEN-0.4"].endswith("eAIP/QA-GEN-0.4-en-GB.html")
        assert found["ENR-3.2"].endswith("eAIP/QA-ENR-3.2-en-GB.html")
        assert found["AD-2-OTHH"].endswith("eAIP/QA-AD-2-OTHH-en-GB.html")

    def test_single_quotes_are_read(self, tool):
        """menu.js mixes quote styles; a reader that takes only one misses rows."""
        assert "ENR-3.2" in tool.all_sections_on(MENU_JS, BASE)


class TestWhichEditionIsInForce:
    """Newest published and in force today are different editions.

    Qatar published AIP-30 on 03 SEP 2026 to take effect 01 OCT 2026. For the
    whole of September the newest edition is one that is not in force. Both
    are worth having; the fetcher must not blur them.
    """

    NEWEST = "https://aim.gov.qa/AIP/03-SEP-2026/AIP-30/2026-10-01-000000/html/index-en-GB.html"
    CURRENT = "https://aim.gov.qa/AIP/11-JUN-2026/AIP-29/2026-08-06-000000/html/index-en-GB.html"

    def test_effective_date_is_the_iso_field_not_the_publication_date(self, tool):
        from datetime import date

        assert tool.effective_date(self.NEWEST) == date(2026, 10, 1)

    def test_the_newest_is_not_yet_in_force(self, tool):
        from datetime import date

        listed = [(self.NEWEST, "AMDT 01/2026"), (self.CURRENT, "2nd Edition")]
        chosen = tool.in_force_on(listed, date(2026, 9, 7))
        assert chosen is not None
        assert chosen[0] == self.CURRENT

    def test_after_the_effective_date_the_newest_takes_over(self, tool):
        from datetime import date

        listed = [(self.NEWEST, "AMDT 01/2026"), (self.CURRENT, "2nd Edition")]
        assert tool.in_force_on(listed, date(2026, 10, 1))[0] == self.NEWEST

    def test_nothing_in_force_is_reported_not_guessed(self, tool):
        from datetime import date

        listed = [(self.NEWEST, "AMDT 01/2026")]
        assert tool.in_force_on(listed, date(2026, 1, 1)) is None

    def test_an_undateable_edition_does_not_become_the_answer(self, tool):
        from datetime import date

        listed = [("https://aim.gov.qa/AIP/whenever/html/index-en-GB.html", "?")]
        assert tool.in_force_on(listed, date(2026, 9, 7)) is None


#: An anchor as Qatar's menu really writes it. Every one of the hundred-odd
#: section links carries a fragment, because the menu points at an element
#: within a page rather than at the page. A reader that requires the closing
#: quote straight after ``.html`` finds none of them — and reports "no AIP
#: sections found" for an index that names the entire AIP.
REAL_MENU = """<html><body>
<div class="tab">
<a target="_self" href="../eAIP/QA-menu-en-GB.html" title="eAIP ToC: " id="current">AIP</a>
<a target="_self" href="../eAIP/QA-AMDT-en-GB.html" title="List of Changes">AMDT</a>
<a target="_self" href="../eSUP/QA-eSUPs-en-GB.html" title="List of AIP Supplements">SUPs</a>
<a target="_self" href="../eAIC/QA-eAICs-en-GB.html" title="List of Circulars">AICs</a>
<a target="_self" href="../search/QA-search-en-GB.html" title="">Search</a>
</div>
<div class="H1"><a href="QA-GEN-0.1-en-GB.html#i197343" id="i197343">GEN 0.1</a></div>
<div class="H2"><a href="QA-ENR-1.3-en-GB.html#i198012" id="i198012">ENR 1.3</a></div>
<div class="H2"><a href="QA-AD-2-OTHH-en-GB.html#i201456" id="i201456">OTHH</a></div>
</body></html>"""

#: Qatar's history page, as served. It files each edition under a table whose
#: class says what the edition *is* — the State's own declaration, which is
#: better evidence than a date read out of a URL.
REAL_HISTORY = """<html><body>
<div class="section"><h2>Currently effective Issues</h2>
<table class="history-table current-issues-table"><tbody><tr>
<td class="date">11 JUN 2026</td><td class="date">06 AUG 2026</td>
<td class="description-top"><a
 href="11-JUN-2026/AIP-29/2026-08-06-000000/html/index-en-GB.html">AIP 2nd Edition</a></td>
</tr></tbody></table></div>
<div class="section"><h2>Next Issues</h2>
<table class="history-table next-issues-table"><tbody><tr>
<td class="date">03 SEP 2026</td><td class="date">01 OCT 2026</td>
<td class="description-top"><a
 href="03-SEP-2026/AIP-30/2026-10-01-000000/html/index-en-GB.html">AIRAC AIP AMDT 01/2026</a></td>
</tr></tbody></table></div>
<div class="section"><h2>Expired Issues (Archives)</h2>
<table class="history-table archived-issues-table"><tbody><tr>
<td class="date">NIL</td><td class="date">NIL</td><td>NIL</td>
</tr></tbody></table></div>
</body></html>"""

MENU_BASE = (
    "https://aim.gov.qa/AIP/03-SEP-2026/AIP-30/2026-10-01-000000/html/eAIP/"
    "QA-menu-en-GB.html"
)


class TestAnchorsCarryFragments:
    """The defect that made a fully-populated menu read as empty."""

    def test_a_fragment_does_not_hide_the_page(self, tool):
        found = tool.all_sections_on(REAL_MENU, MENU_BASE)
        assert "GEN-0.1" in found
        assert found["GEN-0.1"].endswith("QA-GEN-0.1-en-GB.html")

    def test_the_fragment_is_dropped_not_kept(self, tool):
        """Kept, it would be saved as a separate file per anchor."""
        assert "#" not in tool.all_sections_on(REAL_MENU, MENU_BASE)["ENR-1.3"]

    def test_every_part_is_reached(self, tool):
        found = tool.all_sections_on(REAL_MENU, MENU_BASE)
        assert {"GEN-0.1", "ENR-1.3", "AD-2-OTHH"} <= set(found)

    def test_a_query_string_is_dropped_too(self, tool):
        html = '<a href="QA-ENR-4.4-en-GB.html?lang=en">x</a>'
        assert tool.all_sections_on(html, MENU_BASE)["ENR-4.4"].endswith(
            "QA-ENR-4.4-en-GB.html"
        )


class TestTheCompanionPublications:
    """AIP < AMDT < SUP < NOTAM. Fetching the AIP alone takes the bottom.

    A supplement in force changes what an AIP section means. An AIP fetched
    without its supplements reads as though nothing supersedes it, and no
    amount of looking at the pages that did arrive would show the gap.
    """

    def test_the_three_lists_are_found(self, tool):
        found = tool.companion_indexes(REAL_MENU, MENU_BASE)
        assert set(found) == {"AMDT", "eSUPs", "eAICs"}

    def test_they_are_absolute_urls(self, tool):
        found = tool.companion_indexes(REAL_MENU, MENU_BASE)
        assert found["eSUPs"].endswith("/html/eSUP/QA-eSUPs-en-GB.html")

    def test_search_is_not_one_of_them(self, tool):
        assert "search" not in " ".join(tool.companion_indexes(REAL_MENU, MENU_BASE))

    def test_documents_beside_an_index_are_found(self, tool):
        index = (
            '<a href="QA-SUP-16-2026-en-GB.html">SUP 16/2026</a>'
            '<a href="QA-SUP-15-2026-en-GB.html">SUP 15/2026</a>'
        )
        base = "https://aim.gov.qa/AIP/x/html/eSUP/QA-eSUPs-en-GB.html"
        assert set(tool.documents_beside(index, base)) == {
            "QA-SUP-16-2026-en-GB", "QA-SUP-15-2026-en-GB"
        }

    def test_a_link_back_to_the_aip_is_not_followed(self, tool):
        """A SUP list links back to the menu. Following it re-walks the AIP."""
        index = '<a href="../eAIP/QA-menu-en-GB.html">back</a>'
        base = "https://aim.gov.qa/AIP/x/html/eSUP/QA-eSUPs-en-GB.html"
        assert tool.documents_beside(index, base) == {}


class TestTheStateLabelsItsOwnEditions:
    """Qatar says which edition is current. That beats inferring from a date."""

    HISTORY_URL = "https://aim.gov.qa/AIP/QA-history-en-GB.html"

    def test_the_declaration_is_read(self, tool):
        grouped = tool.editions_by_status(REAL_HISTORY, self.HISTORY_URL)
        assert set(grouped) >= {"current", "next"}

    def test_current_is_the_older_edition(self, tool):
        """The newest published is the one that is not yet in force."""
        grouped = tool.editions_by_status(REAL_HISTORY, self.HISTORY_URL)
        assert "AIP-29" in grouped["current"][0][0]
        assert "AIP-30" in grouped["next"][0][0]

    def test_the_declaration_agrees_with_the_dates_here(self, tool):
        """Belt and braces: two independent readings, same answer."""
        from datetime import date

        grouped = tool.editions_by_status(REAL_HISTORY, self.HISTORY_URL)
        by_date = tool.in_force_on(
            tool.editions(REAL_HISTORY, self.HISTORY_URL), date(2026, 9, 7)
        )
        assert by_date[0] == grouped["current"][0][0]

    def test_an_empty_archive_row_is_not_an_edition(self, tool):
        """The archive table holds a NIL row, not a link."""
        grouped = tool.editions_by_status(REAL_HISTORY, self.HISTORY_URL)
        assert "archived" not in grouped

    def test_a_layout_without_labels_returns_nothing(self, tool):
        """Not a guess. The caller falls back to dates rather than this
        inventing a status the page never declared."""
        assert tool.editions_by_status(HISTORY, self.HISTORY_URL) == {}

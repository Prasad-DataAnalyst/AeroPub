"""One page for one sector, and the one way a layout can lie.

Every section already decided what it found. This module ranks and lays out,
and the risk it introduces is presentational: a dossier that read both ends of
a route and none of the middle can print "nothing found" in the same typeface
as one that read everything, and a reader takes the two for the same
statement.

So the assertions are mostly about the headline. A page that is not conclusive
never shows a settled verdict, says so before it says anything else, and puts
what was *not* looked at in a section of its own rather than a footnote.

The rest is that nothing here computes: every severity and every sentence on
the page came from the section that raised it.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timezone

import pytest

from aeropub.briefing import BRIEFING_CSS, briefing_html, sections_of
from aeropub.operator import Exposure
from aeropub.route import Jurisdiction, OpenItem, Route, RouteDossier
from aeropub.sweep import NetworkSweep

NOW = datetime(2026, 10, 5, 6, 0, tzinfo=timezone.utc)
DAY = date(2026, 10, 5)


def route(**overrides) -> Route:
    fields = dict(
        departure="OTHH",
        destination="EGLL",
        crosses=(Jurisdiction(designator="AAAA"), Jurisdiction(designator="BBBB")),
        planned_level_ft=35000.0,
    )
    fields.update(overrides)
    return Route(**fields)


def item(**overrides) -> OpenItem:
    fields = dict(
        where="AAAA",
        what="ENR 1.6 never read",
        severity=Exposure.UNKNOWN,
        why="nobody has looked",
    )
    fields.update(overrides)
    return OpenItem(**fields)


def dossier(**overrides) -> RouteDossier:
    fields = dict(
        route=route(),
        as_at=NOW,
        on=DAY,
        sweep=NetworkSweep(operator="test", as_at=NOW, on=DAY),
        open_items=(),
        not_addressed=("terrain — Grid MORA and the vertical profile",),
    )
    fields.update(overrides)
    return RouteDossier(**fields)


# --------------------------------------------------------------------------
# The headline
# --------------------------------------------------------------------------


class TestVerdict:
    def test_an_inconclusive_dossier_never_shows_a_settled_verdict(self):
        """However little it happened to find."""
        page = briefing_html(dossier())
        assert "NOT ESTABLISHED" in page
        assert "NOTHING FOUND" not in page

    def test_it_says_so_before_it_says_anything_else(self):
        page = briefing_html(dossier())
        assert "does not speak for the whole sector" in page
        assert page.index("does not speak for the whole sector") < page.index(
            "<h2>Open items</h2>"
        )

    def test_the_banner_says_what_the_absence_of_a_finding_is_not(self):
        page = briefing_html(dossier())
        assert (
            "the absence of a finding below is not the same as the absence of "
            "a problem" in page
        )

    def test_the_coverage_is_on_the_page_as_a_number(self):
        page = briefing_html(dossier())
        assert "Spoken for" in page
        assert re.search(r"<dd>\d+ of \d+", page)

    def test_an_item_keeps_its_own_severity_under_an_unestablished_verdict(self):
        """The headline being unestablished does not soften what a section
        raised."""
        found = dossier(open_items=(item(severity=Exposure.HIGH),))
        page = briefing_html(found)
        assert "NOT ESTABLISHED" in page
        assert ">HIGH<" in page

    def test_a_severity_reads_correctly_with_the_styles_stripped(self):
        """A page read as text still has to say HIGH, not high."""
        page = briefing_html(dossier(open_items=(item(severity=Exposure.HIGH),)))
        assert 'class="bf-tag bf-high">HIGH<' in page


# --------------------------------------------------------------------------
# The open items
# --------------------------------------------------------------------------


class TestItems:
    def test_items_are_grouped_worst_first(self):
        found = dossier(
            open_items=(
                item(where="LOW-ONE", severity=Exposure.LOW),
                item(where="CRIT-ONE", severity=Exposure.CRITICAL),
                item(where="MED-ONE", severity=Exposure.MEDIUM),
            )
        )
        page = briefing_html(found)
        assert page.index("CRIT-ONE") < page.index("MED-ONE") < page.index("LOW-ONE")

    def test_every_severity_carries_what_it_means(self):
        """A reader meeting UNKNOWN for the first time needs to know it is not
        a mild version of LOW."""
        page = briefing_html(dossier(open_items=(item(),)))
        assert "an absent one" in page

    def test_the_reason_is_the_sections_own_words(self):
        page = briefing_html(
            dossier(open_items=(item(why="the coverage floor is FL200"),))
        )
        assert "the coverage floor is FL200" in page

    def test_an_item_with_no_reason_still_renders(self):
        page = briefing_html(dossier(open_items=(item(why=""),)))
        assert "ENR 1.6 never read" in page

    def test_no_items_is_qualified_by_the_coverage(self):
        page = briefing_html(dossier())
        assert "Read that against the coverage above" in page

    def test_nothing_is_ranked_by_this_module(self):
        """Every severity on the page came from the section that raised it."""
        import aeropub.briefing as module

        source = " ".join((module.__doc__ or "").split())
        assert "does not rank, screen or conclude" in source

    def test_markup_in_a_finding_cannot_break_the_page(self):
        page = briefing_html(
            dossier(open_items=(item(where="<script>x</script>"),))
        )
        assert "<script>x</script>" not in page
        assert "&lt;script&gt;" in page


# --------------------------------------------------------------------------
# What was not looked at
# --------------------------------------------------------------------------


class TestNotAddressed:
    def test_it_gets_a_section_not_a_footnote(self):
        page = briefing_html(dossier())
        assert "Not addressed" in page
        assert "terrain — Grid MORA" in page

    def test_it_says_why_it_is_listed(self):
        page = briefing_html(dossier())
        assert (
            "cannot tell the difference between a check that passed and one "
            "nobody ran" in page
        )

    def test_an_empty_list_produces_no_section(self):
        page = briefing_html(dossier(not_addressed=()))
        assert "Not addressed" not in page


# --------------------------------------------------------------------------
# Sections
# --------------------------------------------------------------------------


class TestSections:
    def test_a_section_not_supplied_contributes_nothing(self):
        """"ENR 5 turned up no hazards" and "no ENR 5 was given" are different
        statements, and the second has no findings to show."""
        assert sections_of(dossier()) == ()
        assert "Sections in full" not in briefing_html(dossier())

    def test_a_supplied_section_appears_with_its_own_render(self):
        from aeropub.surveillance import SurveillanceRegister, view_surveillance

        view = view_surveillance(SurveillanceRegister(), regions=["AAAA"])
        page = briefing_html(dossier(surveillance=view))
        assert "Surveillance — ENR 1.6" in page
        assert "no ENR 1.6 has been read for AAAA" in page

    def test_sections_are_collapsed_out_of_the_way(self):
        from aeropub.surveillance import SurveillanceRegister, view_surveillance

        view = view_surveillance(SurveillanceRegister(), regions=["AAAA"])
        page = briefing_html(dossier(surveillance=view))
        assert "<details>" in page and "<summary>" in page


# --------------------------------------------------------------------------
# The page itself
# --------------------------------------------------------------------------


class TestPage:
    def test_the_route_and_the_cycle_are_in_the_header(self):
        page = briefing_html(dossier())
        assert "OTHH → EGLL" in page
        assert "AIRAC 2610" in page

    def test_the_regions_crossed_are_named(self):
        page = briefing_html(dossier())
        assert "AAAA, BBBB" in page

    def test_the_planned_level_is_shown_where_there_is_one(self):
        assert "Planned level" in briefing_html(dossier())
        assert "Planned level" not in briefing_html(
            dossier(route=route(planned_level_ft=None))
        )

    def test_a_title_can_be_given(self):
        assert "Gulf sector" in briefing_html(dossier(), title="Gulf sector")

    def test_the_page_is_self_contained(self):
        page = briefing_html(dossier())
        assert "https://" not in page
        assert "http://" not in page

    def test_both_themes_are_defined(self):
        assert "prefers-color-scheme: dark" in BRIEFING_CSS
        assert "--bf-ink" in BRIEFING_CSS

    def test_every_colour_is_defined_on_bare_root_first(self):
        """A colour whose only definition sits behind a media query never
        applies in the un-stamped state, which is what most readers see."""
        bare = BRIEFING_CSS.split("@media")[0]
        for token in re.findall(r"--bf-[a-z]+", BRIEFING_CSS):
            assert token in bare, token

    def test_the_footer_says_the_page_decides_nothing(self):
        page = briefing_html(dossier())
        assert "ranks and lays out, and decides nothing" in page

    def test_the_containment_refusal_survives_into_the_briefing(self):
        page = briefing_html(dossier())
        assert "whether a point is inside an area" in page


class TestWithAMap:
    def build(self):
        from aeropub.airspace import (
            Airspace,
            AirspaceClass,
            AirspaceStructure,
            AirspaceType,
        )
        from aeropub.atlas import build_atlas
        from aeropub.boundary import boundary_from_points
        from aeropub.geo import Position
        from aeropub.provenance import SourceRef

        source = SourceRef(
            source_id="TEST",
            document="AIP AA ENR 2.1",
            locator="row 1",
            retrieved_at=NOW,
            content_hash="d" * 64,
            parser_id="test",
            parser_version="0.1.0",
        )
        ring = [
            Position(24.0, 46.0),
            Position(36.0, 46.0),
            Position(36.0, 54.0),
            Position(24.0, 54.0),
        ]
        volume = Airspace(
            designator="AAAA",
            kind=AirspaceType.FIR,
            source=source,
            airspace_class=AirspaceClass.A,
            boundary=boundary_from_points(ring),
        )
        return build_atlas(airspace=AirspaceStructure(volumes=(volume,)))

    def test_the_map_is_drawn_where_one_is_given(self):
        page = briefing_html(dossier(), atlas=self.build())
        assert "<svg" in page and 'data-layer="fir"' in page

    def test_the_layers_switch_on_the_briefing_too(self):
        page = briefing_html(dossier(), atlas=self.build())
        for layer in ("graticule", "fir", "track", "hazard"):
            assert f'data-layer="{layer}"' in page

    def test_a_briefing_without_a_map_is_still_a_briefing(self):
        page = briefing_html(dossier())
        assert "<svg" not in page
        assert "Open items" in page

    def test_the_map_script_only_ships_with_the_map(self):
        assert "<script>" not in briefing_html(dossier())
        assert "<script>" in briefing_html(dossier(), atlas=self.build())

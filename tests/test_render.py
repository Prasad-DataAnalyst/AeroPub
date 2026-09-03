"""Printable output.

The rule these tests hold is that the page renders the API payload and nothing
else. A printed document that disagreed with the JSON for the same aerodrome
would be the worst artefact this system could produce, because the two get
compared exactly when something has already gone wrong.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta, timezone

import pytest

from aeropub.aip import AipCoverage, HoldingState, SectionHolding, aerodrome_sections
from aeropub.airac import AiracCycle
from aeropub.bulletin import between_cycles
from aeropub.dossier import build
from aeropub.facts import Fact, FactStore, Precedence
from aeropub.horizon import horizon
from aeropub.lenses import Audience, view
from aeropub.provenance import SourceRef
from aeropub.quality import assess_quality
from aeropub.render import PLACEHOLDER, render_dossier, template

N1 = AiracCycle.from_identifier("2609")
N2 = AiracCycle.from_identifier("2610")
NOW = datetime(2026, 10, 5, 6, 0, tzinfo=timezone.utc)


def ref(document_name="AIP AMDT 09/26", locator="AD 2.13"):
    return SourceRef(
        source_id="QA-CAA", document=document_name, locator=locator,
        retrieved_at=datetime(2026, 10, 1, 9, 14, tzinfo=timezone.utc),
        content_hash="7f3c" + "a" * 60, parser_id="eaip-eurocontrol",
        parser_version="0.1.0",
    )


@pytest.fixture
def store():
    facts = FactStore()
    facts.add(Fact(entity="OTHH/RWY34L", attribute="lda_m", value=3900,
                   valid_from=date(2026, 1, 1),
                   valid_to=N2.effective_date - timedelta(days=1),
                   source=ref(), precedence=Precedence.AIP))
    facts.add(Fact(entity="OTHH/RWY34L", attribute="lda_m", value=3500,
                   valid_from=N2.effective_date,
                   source=ref("AIP AMDT 10/26"), precedence=Precedence.AIP))
    return facts


@pytest.fixture
def coverage():
    return AipCoverage([
        SectionHolding(section=s, entity="OTHH", state=HoldingState.HELD,
                       source=ref("AIP AMDT 10/26", s.code), cycle=N2)
        for s in aerodrome_sections() if s.code != "AD 2.10"
    ])


@pytest.fixture
def page(store, coverage):
    dossier = build("OTHH", facts=store, coverage=coverage, as_at=NOW, cycle=N2)
    bulletin = between_cycles(store, "OTHH", N1, N2,
                              coverage_before=coverage, coverage_after=coverage)
    ahead = horizon(store, "OTHH", from_date=NOW.date(), days=84)
    return render_dossier(
        dossier,
        bulletin=bulletin,
        horizon=ahead,
        conduct=assess_quality(as_at=NOW),
        lenses={a: view(a, "OTHH", as_at=NOW, dossier=dossier, bulletin=bulletin,
                        ahead=ahead) for a in Audience},
        generated_at=NOW,
    )


def _embedded(html: str) -> dict:
    match = re.search(
        r'<script id="aeropub-data" type="application/json">(.*?)</script>',
        html, re.S,
    )
    assert match, "the page carries no data block"
    return json.loads(match.group(1).replace("<\\/", "</"))


class TestItRendersThePayload:
    def test_the_page_carries_the_api_payload_verbatim(self, page, store, coverage):
        from aeropub.api import document

        data = _embedded(page)
        expected = document(
            build("OTHH", facts=store, coverage=coverage, as_at=NOW, cycle=N2),
            generated_at=NOW,
        )
        assert data["dossier"] == expected

    def test_an_engineer_can_check_the_printed_figure_against_the_payload(self, page):
        data = _embedded(page)
        values = [
            v["value"]
            for s in data["dossier"]["data"]["sections"]
            for v in s["values"]
        ]
        assert 3500 in values

    def test_every_document_type_reaches_the_page(self, page):
        data = _embedded(page)
        assert set(data) == {"dossier", "bulletin", "horizon", "conduct", "lenses"}
        assert set(data["lenses"]) == {a.value for a in Audience}

    def test_nothing_is_recomputed_for_display(self, page):
        # The template holds no arithmetic and no assessment text; if a figure
        # is on the page it came from the payload.
        markup = template()
        for reworded in ("reduced by", "increased by", "restored", "U/S"):
            assert reworded not in markup


class TestScriptSafety:
    def test_a_closing_tag_inside_the_data_cannot_end_the_script(self, store, coverage):
        # A NOTAM or AIP extract containing "</" would otherwise break the page
        # silently, and only for the aerodromes whose text happens to have one.
        store.add(Fact(entity="OTHH", attribute="local_regulations",
                       value="See </script> and the note below",
                       valid_from=date(2026, 1, 1), source=ref(locator="AD 2.20"),
                       precedence=Precedence.AIP))
        html = render_dossier(
            build("OTHH", facts=store, coverage=coverage, as_at=NOW), generated_at=NOW
        )
        assert "</script>" not in html.split('id="aeropub-data"')[1].split("</script>")[0]
        assert _embedded(html)["dossier"]["data"]["aerodrome"] == "OTHH"


class TestOmissionsAreVisible:
    def test_a_page_without_a_change_record_says_so(self, store, coverage):
        html = render_dossier(
            build("OTHH", facts=store, coverage=coverage, as_at=NOW), generated_at=NOW
        )
        statement = _embedded(html)["bulletin"]["data"]["coverage_statement"]
        assert "No change record was supplied" in statement
        assert "not what moved to get here" in statement

    def test_absent_sections_are_null_rather_than_missing_keys(self, store, coverage):
        html = render_dossier(
            build("OTHH", facts=store, coverage=coverage, as_at=NOW), generated_at=NOW
        )
        data = _embedded(html)
        assert data["horizon"]["data"] is None
        assert data["conduct"]["data"] is None

    def test_coverage_gaps_are_in_the_payload_the_page_renders(self, page):
        assert _embedded(page)["dossier"]["data"]["coverage_gaps"] == ["AD 2.10"]

    def test_lens_soundness_differs_by_reader(self, page):
        lenses = _embedded(page)["lenses"]
        assert lenses["flight_crew"]["data"]["sound"] is False
        assert lenses["dispatch"]["data"]["sound"] is True


class TestTemplate:
    def test_the_template_ships_with_the_package(self):
        assert PLACEHOLDER in template()
        assert len(template()) > 5000

    def test_the_page_is_print_ready(self):
        markup = template()
        assert "@media print" in markup
        assert "break-inside" in markup

    def test_both_themes_are_defined(self):
        markup = template()
        assert "prefers-color-scheme: dark" in markup
        assert '[data-theme="dark"]' in markup

    def test_every_colour_token_is_defined_on_bare_root(self):
        markup = template()
        root = markup[markup.index(":root{"):markup.index("@media (prefers-color-scheme")]
        defined = set(re.findall(r"(--[a-z0-9-]+):", root))
        used = set(re.findall(r"var\((--[a-z0-9-]+)\)", markup))
        assert used - defined == set()

    def test_the_placeholder_is_gone_once_rendered(self, page):
        assert PLACEHOLDER not in page

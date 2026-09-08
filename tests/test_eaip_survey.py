"""Surveying a whole fetched AIP, rather than one page at a time.

The question a fetch raises first is not "what is in this page" but "is what
arrived the AIP the State publishes". A hundred separate probes never answer
it, and the ways a fetch goes quietly wrong are all absences: a section that
never came, a page that came empty, a filename that claims what its content
does not carry. What is asserted here is that none of the three can be
mistaken for a page that arrived and can be read.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aeropub.aip import SECTIONS
from aeropub.eaip.survey import (
    DirectorySurvey,
    Legibility,
    codes_within,
    read_code,
    survey_directory,
)
from aeropub.eaip.probe import probe

ENR_32 = """<html><body>
<div id="ENR-3.2"><h1>ENR 3.2 Area Navigation Routes</h1>
<table id="ENR-3.2-table"><tr><th>Route</th></tr><tr><td>M318</td></tr></table>
</div></body></html>"""

GEN_04 = """<html><body><div id="GEN-0.4"><h1>Checklist of AIP pages</h1>
<table><tr><td>ENR 3.2</td><td>01 OCT 2026</td></tr></table></div></body></html>"""

AERODROME = """<html><body>
<div id="AD-2.1"><h2>OTHH AD 2.1 Aerodrome indicator</h2></div>
<div id="AD-2.12"><h2>OTHH AD 2.12 Runway physical characteristics</h2></div>
<div id="AD-2.13"><h2>OTHH AD 2.13 Declared distances</h2></div>
</body></html>"""

#: The quiet one: it has a section's filename, a plausible size, and no
#: section in it. This is what a State publishing a part of its AIP as PDF
#: leaves behind, and it counts towards "97 pages fetched" like any other.
PDF_STUB = """<html><body><p>Published as PDF.
<a href="ENR-5.1.pdf">Download</a></p></body></html>"""

FRAMESET = """<html><frameset><frame src="eAIP/QA-menu-en-GB.html"></frameset></html>"""


@pytest.fixture()
def fetched(tmp_path: Path) -> Path:
    (tmp_path / "QA-ENR-3.2-en-GB.html").write_text(ENR_32)
    (tmp_path / "QA-GEN-0.4-en-GB.html").write_text(GEN_04)
    (tmp_path / "QA-AD-2-OTHH-en-GB.html").write_text(AERODROME)
    (tmp_path / "QA-ENR-5.1-en-GB.html").write_text(PDF_STUB)
    (tmp_path / "index-en-GB.html").write_text(FRAMESET)
    return tmp_path


class TestReadingASectionFromAFilename:

    @pytest.mark.parametrize(
        "filename,code,aerodrome",
        [
            ("QA-ENR-3.2-en-GB.html", "ENR 3.2", ""),
            ("ENR-3.2.html", "ENR 3.2", ""),
            ("QA-GEN-0.4-en-GB.html", "GEN 0.4", ""),
            ("QA-AD-2-OTHH-en-GB.html", "AD 2", "OTHH"),
            ("QA-AD-1.1-en-GB.html", "AD 1.1", ""),
            ("QA-ENR-1-en-GB.html", "ENR 1", ""),
        ],
    )
    def test_codes_are_read_from_the_name(self, filename, code, aerodrome):
        assert read_code(filename) == (code, aerodrome)

    def test_a_name_that_says_nothing_returns_nothing(self):
        """Not a guess and not an error. index-en-GB.html is not a section."""
        assert read_code("index-en-GB.html") == ("", "")

    def test_leading_zeros_do_not_make_a_different_section(self):
        assert read_code("QA-ENR-03.02-en-GB.html")[0] == "ENR 3.2"


class TestReadingSectionsFromContent:

    def test_identifiers_name_the_sections(self):
        assert codes_within(probe(ENR_32)) == {"ENR 3.2"}

    def test_an_aerodrome_page_carries_many_sections(self):
        found = codes_within(probe(AERODROME))
        assert found == {"AD 2.1", "AD 2.12", "AD 2.13"}

    def test_a_page_with_no_identifiers_carries_nothing(self):
        assert codes_within(probe(PDF_STUB)) == frozenset()


class TestWhatArrived:

    def test_every_page_is_surveyed(self, fetched):
        assert len(survey_directory(fetched).pages) == 5

    def test_readable_sections_come_from_content(self, fetched):
        read = survey_directory(fetched).codes_read
        assert {"ENR 3.2", "GEN 0.4", "AD 2.1", "AD 2.12", "AD 2.13"} <= read

    def test_an_aerodrome_is_named(self, fetched):
        assert survey_directory(fetched).aerodromes == ("OTHH",)

    def test_a_frameset_has_no_structure_but_is_not_unread(self, fetched):
        """Different failures, different fixes. Neither is 'bad'."""
        survey = survey_directory(fetched)
        frameset = next(p for p in survey.pages if p.path.name.startswith("index"))
        assert frameset.legibility is Legibility.NO_STRUCTURE


class TestAnEmptyPageIsNeverAPass:
    """The discipline the whole survey exists for.

    A file named for a section, carrying none of it, must not be counted as
    holding that section. If it were, ``missing`` would under-report and every
    consumer downstream would treat an empty page as covered.
    """

    def test_it_is_not_counted_as_readable(self, fetched):
        assert "ENR 5.1" not in survey_directory(fetched).codes_read

    def test_it_is_reported_as_arrived_but_unreadable(self, fetched):
        survey = survey_directory(fetched)
        assert "ENR 5.1" in survey.codes_named_only
        assert "ENR 5.1" in {s.code for s in survey.arrived_unreadable}

    def test_it_is_not_reported_as_missing_either(self, fetched):
        """The State did publish it. Calling it missing is the other error."""
        assert "ENR 5.1" not in {s.code for s in survey_directory(fetched).missing}

    def test_the_disagreement_is_named(self, fetched):
        survey = survey_directory(fetched)
        assert [p.path.name for p in survey.contradictions] == [
            "QA-ENR-5.1-en-GB.html"
        ]

    def test_a_readable_page_is_not_a_contradiction(self, fetched):
        survey = survey_directory(fetched)
        page = next(p for p in survey.pages if p.named_code == "ENR 3.2")
        assert not page.contradicts_its_name

    def test_an_aerodrome_page_is_not_a_contradiction(self, fetched):
        """Named AD 2, carries AD 2.1 and others. That is agreement."""
        survey = survey_directory(fetched)
        page = next(p for p in survey.pages if p.aerodrome == "OTHH")
        assert not page.contradicts_its_name


class TestAgainstDoc10066:

    def test_what_did_not_arrive_is_reported(self, fetched):
        missing = {s.code for s in survey_directory(fetched).missing}
        assert "ENR 1.3" in missing
        assert "GEN 0.2" in missing

    def test_what_arrived_is_not_reported_missing(self, fetched):
        missing = {s.code for s in survey_directory(fetched).missing}
        assert "ENR 3.2" not in missing
        assert "AD 2.13" not in missing

    def test_the_currency_spine_requires_a_readable_page(self, tmp_path):
        """A GEN 0.4 that arrived as a stub cannot be reconciled against.

        This is the one place where crediting an unreadable page would turn
        the coverage claim into a fiction, so it is asserted separately.
        """
        (tmp_path / "QA-GEN-0.4-en-GB.html").write_text(PDF_STUB)
        assert survey_directory(tmp_path).currency_spine == ()

    def test_a_readable_checklist_counts(self, fetched):
        spine = {s.code for s in survey_directory(fetched).currency_spine}
        assert "GEN 0.4" in spine

    def test_a_state_addition_is_listed_not_dropped(self, tmp_path):
        """Doc 10066 permits a State to add sections. Silence would hide them."""
        (tmp_path / "QA-ENR-9.9-en-GB.html").write_text(
            '<html><body><div id="ENR-9.9"><h1>National</h1></div></body></html>'
        )
        assert survey_directory(tmp_path).unexpected == ("ENR 9.9",)


class TestTheReport:

    def test_it_names_the_absences(self, fetched):
        text = survey_directory(fetched).describe()
        assert "ARRIVED BUT UNREADABLE" in text
        assert "NAME AND CONTENT DISAGREE" in text

    def test_a_long_absence_list_is_summarised_not_dumped(self, fetched):
        """121 codes in a row buries the shape of a failed fetch."""
        text = survey_directory(fetched).describe()
        assert "and 26 more" in text

    def test_an_empty_directory_is_not_a_clean_bill(self, tmp_path):
        survey = survey_directory(tmp_path)
        assert survey.pages == ()
        assert len(survey.missing) == len(SECTIONS)

    def test_a_missing_directory_is_an_error_not_an_empty_result(self, tmp_path):
        with pytest.raises(ValueError):
            survey_directory(tmp_path / "never-fetched")

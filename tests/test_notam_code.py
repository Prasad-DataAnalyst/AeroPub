"""The ICAO Doc 8400 NOTAM Code, vendored rather than consulted.

A Q-code is a fixed table. What was here before was a hand-picked 26 subjects
and 15 conditions — enough for the cases somebody had happened to meet, which
is a different thing from the code. The first 33 real NOTAM this platform read
used six subjects it did not contain, including GNSS area-wide operations and
aircraft stands, and each of those rendered as an unnamed five-letter string.

So the assertions here are about coverage, about provenance, and about the one
property that must survive a bigger table: a code the table does not have is
still reported as absent, never guessed at.
"""

from __future__ import annotations

import pytest

from aeropub import notam_code
from aeropub.notam import CONDITIONS, SUBJECTS, QLine


class TestProvenance:
    def test_the_table_says_where_it_came_from(self):
        """A value nobody can trace out of the code is one nobody can audit."""
        source = notam_code.NOTAM_CODE_SOURCE
        assert "8400" in source["document"]
        assert source["transcribed_from"].startswith("https://")
        assert source["transcription_licence"] == "MIT"
        assert source["retrieved_on"]

    def test_it_names_the_generator_rather_than_inviting_hand_edits(self):
        assert notam_code.NOTAM_CODE_SOURCE["generator"].endswith(".py")
        assert "Do not edit by hand" in notam_code.__doc__

    def test_it_says_it_is_a_transcription(self):
        """Not the primary document, and the reader is told so."""
        assert "transcription, not the primary document" in notam_code.__doc__


class TestCoverage:
    def test_the_subject_table_is_the_whole_code(self):
        assert len(SUBJECTS) > 150

    def test_the_condition_table_is_the_whole_code(self):
        assert len(CONDITIONS) > 70

    @pytest.mark.parametrize(
        "code,expected",
        [
            ("PO", "obstacle clearance altitude and height"),
            ("IU", "ILS Category III"),
            ("OB", "obstacle"),
            ("MP", "aircraft stands"),
            ("GW", "GNSS area-wide operations"),
            ("NA", "all radio navigation facilities"),
        ],
    )
    def test_the_six_subjects_real_doha_traffic_used(self, code, expected):
        """Every one of these appeared in the first live pull and had no name.
        Each is corroborated by the NOTAM's own text — MP against "ACFT STAND
        605 NOT AVBL", GW against "GPS SIGNAL INTERFERENCE"."""
        assert SUBJECTS[code] == expected

    def test_the_q_line_letter_fields_are_tables_too(self):
        assert notam_code.TRAFFIC["IV"] == "IFR and VFR"
        assert notam_code.SCOPES["A"] == "aerodrome"
        assert notam_code.SCOPES["W"] == "navigation warning"
        assert "N" in notam_code.PURPOSES

    def test_the_checklist_code_is_present_in_every_field(self):
        """A checklist NOTAM is K throughout, and a reader meeting one has to
        recognise it rather than report three unknown fields."""
        assert notam_code.TRAFFIC["K"]
        assert notam_code.PURPOSES["K"]
        assert notam_code.SCOPES["K"]

    def test_the_contractions_are_carried(self):
        """Doc 8400 Part 1 — what the words in item E mean."""
        assert len(notam_code.CONTRACTIONS) > 350
        assert notam_code.CONTRACTIONS["CLSD"] == "closed"
        assert notam_code.CONTRACTIONS["WIP"] == "work in progress"

    def test_multi_word_contractions_survive_extraction(self):
        """Eight of them carry a space — 'BA POOR', 'TDZ LGT' — and a key
        pattern that allowed only letters dropped them silently."""
        assert notam_code.CONTRACTIONS["BA POOR"]
        assert notam_code.CONTRACTIONS["TDZ LGT"]


class TestNothingIsGuessed:
    def line(self, code: str) -> QLine:
        return QLine(
            fir="OTDF", code=code, traffic="IV", purpose="NBO", scope="A",
            lower_fl=0, upper_fl=999, latitude="2516N", longitude="05137E",
            radius_nm=5,
        )

    def test_a_code_outside_the_table_has_no_subject(self):
        """A bigger table is still a finite one, and Doc 8400 is amended."""
        assert self.line("QZZAS").subject is None

    def test_a_code_outside_the_table_has_no_condition(self):
        assert self.line("QMXZZ").condition is None

    def test_a_half_known_code_decodes_to_nothing(self):
        """"taxiway <unknown>" reads as though the condition were understood,
        which is exactly the misreading to avoid."""
        assert self.line("QMXZZ").decoded is None
        assert self.line("QZZLC").decoded is None

    def test_a_fully_known_code_decodes(self):
        assert self.line("QMXLC").decoded == "taxiway(s) closed"

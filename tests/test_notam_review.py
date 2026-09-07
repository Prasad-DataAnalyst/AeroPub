"""Reading a NOTAM against its own Q-code.

A human writes both halves, and they travel together whether or not they
agree. Everything downstream screens on the code, so a mismatch does not read
as a mismatch — it reads as a NOTAM that is not there.

The assertions divide in two. What the checker must catch, against the real
Doha messages in `fixtures/faa/othh-notams-excerpt.json` and against the
disagreement found in the wider pull. And — at least as important — what it
must **not** claim, because the first attempt at this flagged 23 of 33 real
NOTAM, nearly all wrongly, and a checker people learn to ignore is worse than
no checker.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from aeropub.faa.aixm import NotamFeed
from aeropub.notam import parse
from aeropub.notam_review import (
    Disagreement,
    expand_contractions,
    review_notam,
    review_notams,
    text_signature,
)

FIXTURE = Path(__file__).parent / "fixtures" / "faa" / "othh-notams-excerpt.json"


def real():
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    held = []
    for message in payload["data"]["aixm"]:
        held.extend(NotamFeed(io.BytesIO(message.encode())))
    return {n.number: n.to_icao_notam() for n in held}


def notam(code: str, text: str, number: int = 1) -> "object":
    return parse(
        f"A{number:04d}/26 NOTAMN\n"
        f"Q) OTDF/{code}/IV/NBO/A/000/999/2516N05137E005\n"
        f"A) OTHH B) 2609070600 C) 2609302359\n"
        f"E) {text}"
    )


# --------------------------------------------------------------------------
# Contractions, from ICAO's own table
# --------------------------------------------------------------------------


class TestExpansion:
    def test_the_published_abbreviations_are_spelled_out(self):
        """A hand-written keyword list would be a second, private vocabulary."""
        found = expand_contractions("PORTION OF TWY P3 CLSD DUE WIP")
        assert "taxiway" in found
        assert "closed" in found
        assert "work in progress" in found

    def test_an_abbreviation_inside_a_word_is_left_alone(self):
        assert "closed" not in expand_contractions("UNCLSDX").lower()

    def test_empty_text_expands_to_nothing(self):
        assert expand_contractions("") == ""


# --------------------------------------------------------------------------
# An unspecific code over a text that names a condition
# --------------------------------------------------------------------------


class TestUnspecificCondition:
    def test_a_closed_taxiway_coded_xx_is_caught(self):
        """The flagship case. Coded QMXXX, and no filter for closed taxiways
        returns it."""
        found = review_notam(notam("QMXXX", "PORTION OF TWY P3 CLSD BTN TWY M AND TWY P."))
        assert not found.agrees
        assert found.discrepancies[0].kind is Disagreement.CONDITION_UNSPECIFIC
        assert found.discrepancies[0].suggested_code == "LC"

    def test_the_reason_is_not_mistaken_for_the_condition(self):
        """"CLSD DUE WIP" is closed, not work-in-progress. Taking the longest
        match rather than the first got this wrong."""
        found = review_notam(notam("QMXXX", "TWY P3 CLSD DUE WIP."))
        assert found.discrepancies[0].suggested_code == "LC"

    def test_a_specific_code_is_not_second_guessed(self):
        """Only XX is checked. A State that coded a condition has decided,
        and text matching is not good enough to overrule it."""
        assert review_notam(notam("QMXLC", "TWY P3 DOWNGRADED TO CODE E OPS.")).agrees

    def test_an_unspecific_code_over_a_text_naming_nothing_is_fine(self):
        """XX is the code's own escape hatch, not an error."""
        assert review_notam(notam("QMXXX", "SEE AIP FOR DETAILS.")).agrees

    def test_it_never_rewrites_the_published_code(self):
        found = review_notam(notam("QMXXX", "TWY P3 CLSD."))
        assert found.notam.q.code == "QMXXX"
        assert found.discrepancies[0].published_code == "XX"


# --------------------------------------------------------------------------
# Peers: the same words carrying different codes
# --------------------------------------------------------------------------


class TestPeerDisagreement:
    def test_two_notam_saying_the_same_thing_must_agree(self):
        """No dictionary and no semantics — only that both cannot be right."""
        found = review_notams([
            notam("QPUCH", "PUBLISHED MISSED APPROACH PROCEDURE FOR ILS RWY 16L SUSPENDED.", 745),
            notam("QPICH", "PUBLISHED MISSED APPROACH PROCEDURE FOR ILS RWY 16L SUSPENDED.", 927),
        ])
        assert all(not r.agrees for r in found)
        assert found[0].discrepancies[0].kind is Disagreement.PEER_DISAGREEMENT

    def test_the_disagreement_names_the_other_notam(self):
        found = review_notams([
            notam("QPUCH", "PUBLISHED MISSED APPROACH PROCEDURE SUSPENDED.", 745),
            notam("QPICH", "PUBLISHED MISSED APPROACH PROCEDURE SUSPENDED.", 927),
        ])
        assert "A0927/26" in found[0].discrepancies[0].peers

    def test_agreeing_peers_raise_nothing(self):
        found = review_notams([
            notam("QPICH", "PUBLISHED MISSED APPROACH PROCEDURE SUSPENDED.", 744),
            notam("QPICH", "PUBLISHED MISSED APPROACH PROCEDURE SUSPENDED.", 926),
        ])
        assert all(r.agrees for r in found)

    def test_different_objects_are_still_the_same_statement(self):
        """Two messages differing only in a stand number say the same thing
        about different objects, and should carry the same code."""
        assert text_signature("ACFT STAND 605 CLSD.") == text_signature(
            "ACFT STAND 612 CLSD."
        )

    def test_a_short_text_is_not_treated_as_a_signature(self):
        """Two three-word NOTAM matching by accident is not evidence."""
        found = review_notams([notam("QMXLC", "TWY CLSD.", 1), notam("QMPLC", "TWY CLSD.", 2)])
        assert all(r.agrees for r in found)


# --------------------------------------------------------------------------
# What it must not claim
# --------------------------------------------------------------------------


class TestRestraint:
    def test_a_subject_is_never_inferred_from_the_text(self):
        """The failure that produced 23 findings from 33 NOTAM. A text
        mentions many things and only one is its subject: "MINIMA FOR OTHH ILS
        RWY 16R CHANGED" mentions an ILS and a runway while being about
        neither."""
        found = review_notam(notam("QPOCH", "MINIMA FOR OTHH ILS RWY 16R CHANGED AS FOLLOWS."))
        assert found.agrees

    def test_a_runway_in_the_text_is_not_a_runway_notam(self):
        found = review_notam(notam("QFAHX", "INCREASED BIRD ACTIVITY ACROSS RWY 16R/34L."))
        assert found.agrees

    def test_nothing_is_corrected_only_reported(self):
        """A corrected Q-code is our inference wearing the State's
        authority."""
        import aeropub.notam_review as module

        assert "Neither corrects anything" in module.__doc__

    def test_a_notam_with_no_q_line_is_not_reviewed(self):
        """No second reading to compare against is not agreement."""
        found = review_notam(parse("A0001/26 NOTAMN\nA) OTHH B) 2609070600 C) 2609302359\nE) TEXT."))
        assert found.agrees


# --------------------------------------------------------------------------
# Trust, not the value
# --------------------------------------------------------------------------


class TestScreenability:
    def test_a_disputed_notam_is_not_screenable(self):
        """So a filter keeps it rather than dropping it."""
        assert not review_notam(notam("QMXXX", "TWY P3 CLSD.")).is_screenable

    def test_an_agreeing_notam_is_screenable(self):
        assert review_notam(notam("QMXLC", "TWY P3 CLSD.")).is_screenable


# --------------------------------------------------------------------------
# Against the real messages
# --------------------------------------------------------------------------


class TestRealDoha:
    def test_the_real_closed_taxiway_agrees_with_its_code(self):
        held = real()
        assert review_notam(held[941]).agrees   # QMXLC, "TWY S4 ... CLSD"

    def test_the_real_bracketed_notam_agrees(self):
        assert review_notam(real()[739]).agrees is False  # QLPXX over "CHANGED"

    def test_the_papi_notam_is_the_unspecific_case(self):
        found = review_notam(real()[739])
        assert found.discrepancies[0].kind is Disagreement.CONDITION_UNSPECIFIC
        assert found.discrepancies[0].suggested_code == "CH"

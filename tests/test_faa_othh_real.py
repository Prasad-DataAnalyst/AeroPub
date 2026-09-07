"""The first real international NOTAM this platform ever read.

Every assertion here runs against `fixtures/faa/othh-notams-excerpt.json` —
three unmodified messages the FAA served for OTHH, kept because each one pins
a way the real format differs from the documentation, and every one of those
differences was silently losing data.

None of these were found by reasoning about the specification. They were found
by asking the FAA for Doha and reading what came back.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from aeropub.faa.aixm import NotamFeed

FIXTURE = Path(__file__).parent / "fixtures" / "faa" / "othh-notams-excerpt.json"


def notams():
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    held = []
    for message in payload["data"]["aixm"]:
        held.extend(NotamFeed(io.BytesIO(message.encode())))
    return {n.number: n for n in held}


@pytest.fixture(scope="module")
def held():
    return notams()


class TestTheResponseShape:
    def test_the_query_endpoint_returns_standalone_messages(self):
        """Not one FeatureCollection with members, as an initial load is —
        a JSON envelope with each AIXMBasicMessage as its own document."""
        payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
        assert payload["status"] == "Success"
        assert all(
            m.lstrip().startswith("<?xml") for m in payload["data"]["aixm"]
        )

    def test_every_message_is_othh_international(self, held):
        assert len(held) == 3
        for notam in held.values():
            assert notam.location == "OTHH"
            assert notam.classification == "INTL"
            assert notam.is_international is True


class TestTheICAOReading:
    def test_the_translation_type_is_prefixed(self, held):
        """Real messages label it OTHER:ICAO, not ICAO. An equality check
        never fired, so every international NOTAM came back with no ICAO
        reading — which is the whole reason those messages are worth having.
        """
        assert all(n.icao_text for n in held.values())

    def test_the_formatted_text_is_xhtml_not_text(self, held):
        """It arrives wrapped in an XHTML div around an escaped pre block, so
        reading the element's own text returns whitespace."""
        assert "<html:div" not in held[941].icao_text
        assert "<pre>" not in held[941].icao_text
        assert held[941].icao_text.startswith("A0941/26 NOTAMR")

    def test_there_is_no_local_reading_at_all(self, held):
        """Every one of these carries a single OTHER:ICAO translation and no
        LOCAL_FORMAT, so a reader that only knew simpleText found nothing.

        And simple_text stays None rather than being filled with the ICAO
        text: a caller asking whether a plain-language rendering exists has
        to get a truthful answer.
        """
        assert held[941].simple_text is None
        assert held[941].icao_text is not None

    def test_every_message_yields_a_q_line(self, held):
        for number, notam in held.items():
            icao = notam.to_icao_notam()
            assert icao is not None, number
            assert icao.q is not None, number


class TestTheQuirksEachMessagePins:
    def test_941_decodes_a_closed_taxiway(self, held):
        q = held[941].to_icao_notam().q
        assert q.fir == "OTDF"
        assert q.code == "QMXLC"
        assert (q.subject, q.condition) == ("taxiway(s)", "closed")
        assert (q.lower_fl, q.upper_fl) == (0, 999)
        assert q.radius_nm == 5

    def test_941_records_what_it_replaces(self, held):
        """A NOTAMR supersedes an earlier one, and losing the reference loses
        the chain."""
        assert "A0908/26" in held[941].to_icao_notam().references

    def test_739_is_enclosed_in_icao_brackets(self, held):
        """Doc 8126 encloses a NOTAM in brackets and real traffic carries
        them. The header pattern is anchored, so a leading '(' failed the
        whole parse."""
        assert held[739].icao_text.strip().startswith("(")
        assert held[739].to_icao_notam() is not None

    def test_744_has_space_padded_q_line_fields(self, held):
        """Some originators pad to a fixed width: 'I ' for traffic, 'A ' for
        scope. A pattern demanding the letter immediately before the slash
        failed the Q-line silently, and the NOTAM still rendered as text."""
        assert "/I /" in held[744].icao_text
        q = held[744].to_icao_notam().q
        assert q.fir == "OTDF"
        assert q.traffic == "I"
        assert q.scope == "A"
        assert q.subject.startswith("instrument approach procedure")

    def test_744_carries_an_estimated_end(self, held):
        """C) 2608122359EST — estimated, which is not the same as a firm end
        and is what a planner has to know."""
        icao = held[744].to_icao_notam()
        assert icao.estimated

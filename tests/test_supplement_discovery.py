"""Supplements the cycle discovered, and the ones nobody can vouch for.

Precedence runs AIP < AMDT < SUP < NOTAM, so a supplement in force changes
what an AIP section means. The cycle finds supplements as documents — an
identifier, a URL, a hash, and not one word read. That is a real thing to
know, and the register is where it belongs.

What it must never do is claim a window it has not read, or drop a supplement
because it could not read one.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from aeropub.cycle import CycleReport, DocumentOutcome, Outcome, StateOutcome
from aeropub.live import DocumentLink, Retention
from aeropub.provenance import Confidence, SourceRef
from aeropub.publication import Edition, EditionStatus, Kind, Publication
from aeropub.reader import ReadResult
from aeropub.record import supplements_from
from aeropub.supplement import ForcePeriod, Supplement, SupplementRegister

NOW = datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc)
TODAY = date(2026, 9, 9)
EDITION = Edition(index_url="https://aim.gov.qa/i.html", status=EditionStatus.CURRENT)


def a_source() -> SourceRef:
    return SourceRef(
        source_id="QA-CAA", document="d", locator="l", retrieved_at=NOW,
        content_hash="a" * 64, parser_id="p", parser_version="1",
    )


def a_report(*codes: str, kind: Kind = Kind.SUPPLEMENT) -> CycleReport:
    documents = []
    for code in codes:
        publication = Publication(
            url=f"https://aim.gov.qa/eSUP/{code.replace(' ', '-').replace('/', '-')}.html",
            kind=kind, edition=EDITION, code=code,
        )
        documents.append(
            DocumentOutcome(
                url=publication.url, outcome=Outcome.READ, code=code,
                result=ReadResult(
                    publication=publication,
                    link=DocumentLink(
                        url=publication.url, document=f"Qatar {code}",
                        media_type="text/html", retrieved_at=NOW,
                        retention=Retention.ARCHIVED, archive_key="sha256:abc",
                        content_hash="b" * 64,
                    ),
                ),
            )
        )
    return CycleReport(
        at=NOW,
        states=(StateOutcome(state="OT", name="Qatar", edition=EDITION,
                             documents=tuple(documents)),),
    )


class TestWhatTheCycleFound:

    def test_supplements_become_register_records(self):
        found = supplements_from(a_report("SUP 15/2026", "SUP 16/2026"), state="QA-CAA")
        assert {s.identifier for s in found} == {"SUP 15/2026", "SUP 16/2026"}

    def test_sections_are_not_supplements(self):
        assert supplements_from(
            a_report("ENR 3.2", kind=Kind.AIP_SECTION), state="QA-CAA"
        ) == ()

    def test_an_index_is_not_a_supplement(self):
        """A list of supplements is not one, and a register full of tables of
        contents would be worse than an empty one."""
        assert supplements_from(
            a_report("eSUPs list", kind=Kind.INDEX), state="QA-CAA"
        ) == ()

    def test_each_carries_a_resolvable_citation(self):
        [found] = supplements_from(a_report("SUP 15/2026"), state="QA-CAA")
        assert found.source.original_url.endswith("SUP-15-2026.html")
        assert found.source.archive_key == "sha256:abc"

    def test_the_citation_says_nothing_read_it(self):
        [found] = supplements_from(a_report("SUP 15/2026"), state="QA-CAA")
        assert found.source.confidence is Confidence.LOW
        assert "Nothing has read its content" in found.summary

    def test_no_window_is_claimed(self):
        [found] = supplements_from(a_report("SUP 15/2026"), state="QA-CAA")
        assert found.effective_from is None and found.effective_to is None
        assert found.state_on(TODAY) is ForcePeriod.UNDATED

    def test_nothing_it_bears_on_is_invented(self):
        """Reading what a supplement modifies out of its prose is exactly the
        derived value this platform refuses."""
        [found] = supplements_from(a_report("SUP 15/2026"), state="QA-CAA")
        assert found.subjects == () and found.section == ""

    def test_duplicates_are_collapsed(self):
        report = a_report("SUP 15/2026", "SUP 15/2026")
        assert len(supplements_from(report, state="QA-CAA")) == 1

    def test_an_empty_cycle_finds_none(self):
        assert supplements_from(CycleReport(at=NOW), state="QA-CAA") == ()


class TestAnUnreadWindowIsNotAnEndedOne:
    """``applies`` is three-valued and ``None`` is falsy. A filter written on
    truthiness drops exactly the supplements nobody can vouch for either way —
    taking a restriction that may still be in force off an operator's screen
    on a day nothing happened."""

    @pytest.fixture()
    def register(self) -> SupplementRegister:
        return SupplementRegister((
            Supplement(identifier="SUP 07/2026", source=a_source(),
                       effective_from=date(2026, 6, 1),
                       effective_to=date(2026, 12, 31)),
            Supplement(identifier="SUP 08/2026", source=a_source(),
                       effective_from=date(2026, 1, 1),
                       effective_to=date(2026, 3, 1)),
            Supplement(identifier="SUP 15/2026", source=a_source()),
        ))

    def test_in_force_is_a_claim_and_excludes_the_unread(self, register):
        assert [s.identifier for s in register.in_force(TODAY)] == ["SUP 07/2026"]

    def test_the_unread_are_their_own_visible_category(self, register):
        assert [s.identifier for s in register.of_unread_window()] == ["SUP 15/2026"]

    def test_screening_keeps_both(self, register):
        assert [s.identifier for s in register.not_known_to_have_ended(TODAY)] == [
            "SUP 07/2026", "SUP 15/2026",
        ]

    def test_an_expired_one_is_in_none_of_them(self, register):
        for found in (register.in_force(TODAY), register.of_unread_window(),
                      register.not_known_to_have_ended(TODAY)):
            assert "SUP 08/2026" not in {s.identifier for s in found}

    def test_a_supplement_not_yet_started_is_not_screened_in(self, register):
        """Not yet in force is a definite answer, unlike an unread window."""
        early = SupplementRegister((
            Supplement(identifier="SUP 20/2026", source=a_source(),
                       effective_from=date(2026, 12, 1)),
        ))
        assert early.not_known_to_have_ended(TODAY) == ()

    def test_the_three_agree_with_state_on(self, register):
        for supplement in register.supplements:
            period = supplement.state_on(TODAY)
            screened = supplement in register.not_known_to_have_ended(TODAY)
            assert screened == (period.applies is not False)

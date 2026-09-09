"""What entered and left a State's supplement list.

A supplement leaving the list is the State withdrawing it. Nothing is
published to say so — no NOTAM, no amendment, no AIRAC date — and until
somebody reads the validity windows it is the only end date obtainable. So it
is worth catching, and it is worth being careful about: a SUP index that 404s
makes every supplement look withdrawn at once.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from aeropub.cycle import Cycle
from aeropub.ledger import SqliteLedger
from aeropub.reader import Retrieved, media_type_of
from aeropub.record import ListingChange, Withdrawal, listing_change
from aeropub.states.qatar import RESOLVER

FIXTURES = Path(__file__).parent / "fixtures" / "qatar"
HOST = "https://aim.gov.qa"
ED = f"{HOST}/AIP/11-JUN-2026/AIP-29/2026-08-06-000000/html"
SERVED = {
    f"{HOST}/AIP/QA-history-en-GB.html": "history-en-GB.html",
    f"{ED}/index-en-GB.html": "index-en-GB.html",
    f"{ED}/eAIP/QA-menu-en-GB.html": "QA-menu-en-GB.html",
}
SUPS = f"{ED}/eSUP/QA-eSUPs-en-GB.html"
DAY_ONE = datetime(2026, 9, 9, tzinfo=timezone.utc)


class Site:
    """A State whose supplement list can be changed and taken down."""

    def __init__(self, numbers=range(7, 16)):
        self.numbers = list(numbers)
        self.up = True

    def listing(self) -> bytes:
        return b"".join(
            b'<a href="QA-SUP-%02d-2026-en-GB.html">SUP %02d/2026</a>' % (n, n)
            for n in self.numbers
        )

    def __call__(self, url: str) -> Retrieved:
        if url == SUPS and not self.up:
            raise ConnectionError("SUP index 503")
        name = SERVED.get(url)
        if name is not None:
            body = (FIXTURES / name).read_bytes()
        elif url == SUPS:
            body = self.listing()
        else:
            body = f"<html><p>{url}</p></html>".encode()
        media_type, declared = media_type_of(url, body, "text/html")
        return Retrieved(
            url=url, body=body, media_type=media_type,
            type_was_declared=declared, retrieved_at=datetime.now(timezone.utc),
        )


@pytest.fixture()
def running(tmp_path):
    site = Site()
    ledger = SqliteLedger(tmp_path / "ledger.db")
    cycle = Cycle(
        resolvers=(RESOLVER,), retrieve=site, ledger=ledger,
        keep=lambda body, media_type: hashlib.sha256(body).hexdigest(),
    )

    def run(day: int = 0):
        report = cycle.run(now=DAY_ONE + timedelta(days=day))
        return listing_change(report, ledger, state="OT")

    yield site, run
    ledger.close()


class TestWhatEnteredAndLeft:

    def test_a_first_sight_is_all_additions(self, running):
        _, run = running
        change = run()
        assert len(change.added) == 9
        assert change.withdrawn == ()

    def test_added_is_not_empty_merely_because_the_ledger_saw_them(self, running):
        """The ledger is written during the cycle, so by comparison time
        everything discovered is already known to it. What separates new from
        old is when it was first seen, not whether it is present."""
        _, run = running
        assert run().added

    def test_a_withdrawal_is_caught(self, running):
        site, run = running
        run()
        site.numbers = list(range(9, 16))
        change = run(day=1)
        assert {w.identifier for w in change.withdrawn} == {
            "SUP 7/2026", "SUP 8/2026",
        }

    def test_it_records_when_the_supplement_was_last_listed(self, running):
        site, run = running
        run()
        site.numbers = list(range(9, 16))
        [first, _] = sorted(run(day=1).withdrawn, key=lambda w: w.identifier)
        assert first.last_seen_at.date() == DAY_ONE.date()

    def test_an_addition_and_a_withdrawal_together(self, running):
        site, run = running
        run()
        site.numbers = list(range(9, 17))
        change = run(day=1)
        assert change.added == ("SUP 16/2026",)
        assert len(change.withdrawn) == 2

    def test_an_unchanged_list_says_so(self, running):
        _, run = running
        run()
        change = run(day=1)
        assert change.is_conclusive
        assert change.describe() == "SUPPLEMENT LISTING — unchanged"

    def test_the_list_page_is_never_reported_withdrawn(self, running):
        """It lives in the same directory as the supplements it lists, so a
        path test calls it one — and then reports the index as withdrawn every
        cycle, because it is an INDEX and never appears among them."""
        _, run = running
        run()
        change = run(day=1)
        assert not any("eSUP" in w.identifier for w in change.withdrawn)


class TestAbsenceOfEvidenceIsNotEvidenceOfAbsence:
    """The whole reason this is a class rather than a set difference."""

    def test_an_unreadable_list_draws_no_conclusion(self, running):
        site, run = running
        run()
        site.up = False
        change = run(day=1)
        assert not change.is_conclusive
        assert change.withdrawn == ()

    def test_it_says_why(self, running):
        site, run = running
        run()
        site.up = False
        assert "not read this cycle" in run(day=1).unverifiable_because

    def test_nine_supplements_are_not_retired_by_one_bad_page(self, running):
        """Acting on this would take a State's entire live supplement layer
        off an operator's screen because one page was briefly down."""
        site, run = running
        run()
        site.up = False
        assert run(day=1).withdrawn == ()

    def test_recovery_restores_the_comparison(self, running):
        site, run = running
        run()
        site.up = False
        run(day=1)
        site.up = True
        site.numbers = list(range(9, 16))
        change = run(day=2)
        assert change.is_conclusive
        assert len(change.withdrawn) == 2

    def test_a_shallow_pass_draws_no_conclusion_either(self, tmp_path):
        """It resolved the list without confirming it, so what it holds is a
        presumption and not evidence of what the State lists today."""
        site = Site()
        ledger = SqliteLedger(tmp_path / "l.db")
        cycle = Cycle(
            resolvers=(RESOLVER,), retrieve=site, ledger=ledger,
            keep=lambda body, media_type: hashlib.sha256(body).hexdigest(),
        )
        cycle.run(now=DAY_ONE)
        report = cycle.run(now=DAY_ONE + timedelta(days=1), deep=False)
        change = listing_change(report, ledger, state="OT")
        assert not change.is_conclusive
        assert "shallow pass" in change.unverifiable_because
        ledger.close()

    def test_a_state_absent_from_the_cycle_is_not_a_withdrawal(self, running):
        _, run = running
        report_change = run()
        assert report_change.is_conclusive
        from aeropub.cycle import CycleReport

        elsewhere = listing_change(CycleReport(at=DAY_ONE), None, state="OE")
        assert not elsewhere.is_conclusive


class TestHowItReads:

    def test_a_withdrawal_names_what_now_governs(self, running):
        site, run = running
        run()
        site.numbers = list(range(9, 16))
        text = run(day=1).describe()
        assert "withdrawn" in text
        assert "the layer beneath it now governs" in text

    def test_an_unreadable_list_says_nothing_is_withdrawn(self, running):
        site, run = running
        run()
        site.up = False
        assert "Nothing is treated as withdrawn" in run(day=1).describe()

    def test_an_empty_change_is_conclusive(self):
        assert ListingChange().is_conclusive

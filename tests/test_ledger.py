"""The ledger that survives a restart, and the claim it must not make falsely.

A ledger is a claim about what we hold, and a claim can outlive the thing it
describes. Lose the archive while the ledger survives and every document
answers UNCHANGED forever: every State reports complete, every citation
resolves to a key that is not there, and nothing says so. Total data loss,
reported as health. Most of what is asserted here is about preventing that.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from aeropub.ledger import LedgerEntry, Reconciliation, SqliteLedger

NOW = datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc)
LATER = NOW + timedelta(days=1)
URL = "https://aim.gov.qa/AIP/x/html/eAIP/QA-ENR-3.2-en-GB.html"


@pytest.fixture()
def ledger():
    made = SqliteLedger()
    yield made
    made.close()


class TestRememberingWhatWasRead:

    def test_an_unread_url_is_unknown(self, ledger):
        assert ledger.hash_for(URL) is None

    def test_a_read_is_remembered(self, ledger):
        ledger.record(URL, "a" * 64, NOW, archive_key="sha256:abc")
        assert ledger.hash_for(URL) == "a" * 64

    def test_a_changed_document_supersedes(self, ledger):
        ledger.record(URL, "a" * 64, NOW, archive_key="sha256:abc")
        ledger.record(URL, "b" * 64, LATER, archive_key="sha256:def")
        assert ledger.hash_for(URL) == "b" * 64

    def test_reads_counts_versions_not_checks(self, ledger):
        """A section confirmed unchanged on four hundred cycles still reads 1:
        that is how many versions of it we hold."""
        ledger.record(URL, "a" * 64, NOW, archive_key="sha256:abc")
        ledger.record(URL, "a" * 64, LATER, archive_key="sha256:abc")
        assert ledger.entry_for(URL).reads == 1

    def test_a_real_change_increments_it(self, ledger):
        ledger.record(URL, "a" * 64, NOW, archive_key="sha256:abc")
        ledger.record(URL, "b" * 64, LATER, archive_key="sha256:def")
        assert ledger.entry_for(URL).reads == 2

    def test_first_seen_survives_a_change(self, ledger):
        ledger.record(URL, "a" * 64, NOW, archive_key="sha256:abc")
        ledger.record(URL, "b" * 64, LATER, archive_key="sha256:def")
        assert ledger.entry_for(URL).first_seen_at == NOW

    def test_forgetting_makes_it_unknown_again(self, ledger):
        ledger.record(URL, "a" * 64, NOW, archive_key="sha256:abc")
        ledger.forget(URL)
        assert ledger.hash_for(URL) is None


class TestSurvivingARestart:
    """The whole reason this exists. In memory, every restart re-reads every
    AIP: slow, and rude to a hundred and eighty States."""

    def test_what_was_read_is_still_known(self, tmp_path):
        path = tmp_path / "ledger.db"
        first = SqliteLedger(path)
        first.record(URL, "a" * 64, NOW, archive_key="sha256:abc")
        first.close()

        second = SqliteLedger(path)
        assert second.hash_for(URL) == "a" * 64
        second.close()

    def test_state_health_survives_too(self, tmp_path):
        """Otherwise a State failing for a fortnight looks new every restart."""
        path = tmp_path / "ledger.db"
        first = SqliteLedger(path)
        for _ in range(3):
            first.note_state("OE", failed=True)
        first.close()

        second = SqliteLedger(path)
        assert second.failures_for("OE") == 3
        second.close()

    def test_a_missing_directory_is_created(self, tmp_path):
        made = SqliteLedger(tmp_path / "nested" / "deeper" / "ledger.db")
        made.record(URL, "a" * 64, NOW)
        assert made.hash_for(URL) == "a" * 64
        made.close()


class TestStateHealth:

    def test_an_unknown_state_has_not_failed(self, ledger):
        assert ledger.failures_for("OT") == 0

    def test_failures_accumulate(self, ledger):
        for _ in range(5):
            ledger.note_state("OE", failed=True)
        assert ledger.failures_for("OE") == 5

    def test_a_success_clears_the_count(self, ledger):
        ledger.note_state("OE", failed=True)
        ledger.note_state("OE", failed=True)
        ledger.note_state("OE", failed=False)
        assert ledger.failures_for("OE") == 0

    def test_states_are_counted_separately(self, ledger):
        ledger.note_state("OE", failed=True)
        ledger.note_state("OT", failed=False)
        assert (ledger.failures_for("OE"), ledger.failures_for("OT")) == (1, 0)


class TestTheClaimMustBeBacked:
    """A ledger that outlives its archive is the dangerous failure: it reports
    health while holding nothing."""

    def test_a_sound_ledger_reconciles_clean(self, ledger):
        ledger.record(URL, "a" * 64, NOW, archive_key="sha256:abc")
        result = ledger.reconcile(holds=lambda key: True)
        assert result.is_sound
        assert (result.checked, result.present) == (1, 1)

    def test_a_missing_copy_is_found(self, ledger):
        ledger.record(URL, "a" * 64, NOW, archive_key="sha256:gone")
        result = ledger.reconcile(holds=lambda key: False)
        assert not result.is_sound
        assert [e.url for e in result.missing] == [URL]

    def test_a_missing_copy_is_forgotten_so_it_is_read_again(self, ledger):
        """No entry costs one re-read. A false claim costs every future cycle."""
        ledger.record(URL, "a" * 64, NOW, archive_key="sha256:gone")
        ledger.reconcile(holds=lambda key: False)
        assert ledger.hash_for(URL) is None

    def test_forgetting_can_be_declined_for_a_dry_run(self, ledger):
        ledger.record(URL, "a" * 64, NOW, archive_key="sha256:gone")
        result = ledger.reconcile(holds=lambda key: False, forget_missing=False)
        assert result.missing and result.forgotten == 0
        assert ledger.hash_for(URL) == "a" * 64

    def test_an_entry_that_never_claimed_a_copy_is_named_not_counted_clean(self, ledger):
        """Read before an archive was configured. Not a fault in itself, and
        not to be filed among the healthy either."""
        ledger.record(URL, "a" * 64, NOW)
        result = ledger.reconcile(holds=lambda key: True)
        assert not result.is_sound
        assert [e.url for e in result.unarchived] == [URL]
        assert result.present == 0

    def test_only_the_missing_are_forgotten(self, ledger):
        good = "https://aim.gov.qa/a.html"
        bad = "https://aim.gov.qa/b.html"
        ledger.record(good, "a" * 64, NOW, archive_key="sha256:here")
        ledger.record(bad, "b" * 64, NOW, archive_key="sha256:gone")
        ledger.reconcile(holds=lambda key: key == "sha256:here")
        assert ledger.hash_for(good) == "a" * 64
        assert ledger.hash_for(bad) is None

    def test_the_report_says_what_a_lost_archive_looks_like(self, ledger):
        ledger.record(URL, "a" * 64, NOW, archive_key="sha256:gone")
        text = ledger.reconcile(holds=lambda key: False).describe()
        assert "CLAIMED A COPY THE ARCHIVE DOES NOT HOLD" in text
        assert "UNCHANGED forever" in text

    def test_a_long_list_is_summarised(self, ledger):
        for n in range(15):
            ledger.record(f"https://aim.gov.qa/{n}.html", "a" * 64, NOW,
                          archive_key="sha256:gone")
        text = ledger.reconcile(holds=lambda key: False).describe()
        assert "and 5 more" in text

    def test_an_empty_ledger_is_sound(self, ledger):
        assert ledger.reconcile(holds=lambda key: False).is_sound


class TestItSatisfiesTheCycle:

    def test_a_cycle_runs_against_it(self, tmp_path):
        """The point of the protocol: the cycle does not know which it has."""
        from datetime import timezone as tz

        from aeropub.cycle import Cycle
        from aeropub.publication import Edition, EditionStatus, Kind, Publication
        from aeropub.reader import Retrieved

        edition = Edition(index_url="https://a.test/i.html",
                          status=EditionStatus.CURRENT)

        class State:
            state, name, entry_point = "OT", "Qatar", "https://a.test/h.html"
            def editions(self, read):
                return (edition,)
            def publications(self, edition, read):
                return (Publication(url=URL, kind=Kind.AIP_SECTION,
                                    edition=edition, code="ENR 3.2"),)

        def retrieve(url):
            return Retrieved(url=url, body=b"<html>x</html>", media_type="text/html",
                             type_was_declared=True, retrieved_at=NOW)

        ledger = SqliteLedger(tmp_path / "l.db")
        cycle = Cycle(resolvers=(State(),), retrieve=retrieve, ledger=ledger,
                      keep=lambda body, media_type: "sha256:kept")
        first = cycle.run(now=NOW)
        second = cycle.run(now=LATER)

        assert len(first.states[0].read) == 1
        assert len(second.states[0].unchanged) == 1
        assert ledger.entry_for(URL).archive_key == "sha256:kept"
        ledger.close()


class TestThereAreTwoCaches:
    """Forgetting in the ledger makes the *cycle* want a document again. It
    does nothing to the *transport*, which still holds an ETag for that URL
    and will send it — so the server answers 304, the reader reports
    unchanged, nothing is archived, and the re-read that reconciliation
    exists to force never happens. Clearing one cache is worse than clearing
    neither, because it looks fixed.
    """

    def test_forgetting_notifies_the_transport(self, ledger):
        dropped: list[str] = []
        ledger.record(URL, "a" * 64, NOW, archive_key="sha256:gone")
        ledger.reconcile(holds=lambda key: False, on_forget=dropped.append)
        assert dropped == [URL]

    def test_a_backed_entry_does_not_notify(self, ledger):
        dropped: list[str] = []
        ledger.record(URL, "a" * 64, NOW, archive_key="sha256:here")
        ledger.reconcile(holds=lambda key: True, on_forget=dropped.append)
        assert dropped == []

    def test_a_dry_run_notifies_nothing(self, ledger):
        dropped: list[str] = []
        ledger.record(URL, "a" * 64, NOW, archive_key="sha256:gone")
        ledger.reconcile(
            holds=lambda key: False, forget_missing=False, on_forget=dropped.append
        )
        assert dropped == []

    def test_the_report_names_the_second_cache(self, ledger):
        ledger.record(URL, "a" * 64, NOW, archive_key="sha256:gone")
        text = ledger.reconcile(holds=lambda key: False).describe()
        assert "transport's validators must be dropped" in text

    def test_the_transports_forget_satisfies_the_callback(self):
        """The callback exists to be handed LiveTransport.forget."""
        from datetime import timedelta

        from aeropub.http import HostThrottle
        from aeropub.transport import LiveTransport

        transport = LiveTransport(throttle=HostThrottle(timedelta(0)))
        transport.conditional_for(URL).etag = '"v1"'
        made = SqliteLedger()
        made.record(URL, "a" * 64, NOW, archive_key="sha256:gone")
        made.reconcile(holds=lambda key: False, on_forget=transport.forget)
        assert transport.conditional_for(URL).etag is None
        made.close()

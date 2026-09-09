"""``python -m aeropub.watch`` — the assembly, over real storage.

Thin by design: it decides nothing about aeronautical data, only which piece is
handed to which. What is asserted is that the wiring is right, that the checks
which catch a lost archive run before the work rather than behind a flag, and
that nothing it prints lets a count of documents *known* read as a count of
documents *held*.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from aeropub.reader import Retrieved, media_type_of
from aeropub.watch import (
    CANNOT_RUN,
    INCOMPLETE,
    OK,
    build_cycle,
    main,
    resolvers,
)

FIXTURES = Path(__file__).parent / "fixtures" / "qatar"
HOST = "https://aim.gov.qa"
ED = f"{HOST}/AIP/11-JUN-2026/AIP-29/2026-08-06-000000/html"
SERVED = {
    f"{HOST}/AIP/QA-history-en-GB.html": "history-en-GB.html",
    f"{ED}/index-en-GB.html": "index-en-GB.html",
    f"{ED}/eAIP/QA-menu-en-GB.html": "QA-menu-en-GB.html",
}


class Offline:
    """Stands in for LiveTransport. Records what it was asked to forget."""

    def __init__(self):
        self.forgotten: list[str] = []

    def forget(self, url: str) -> None:
        self.forgotten.append(url)

    def __call__(self, url: str) -> Retrieved:
        name = SERVED.get(url)
        body = (
            (FIXTURES / name).read_bytes()
            if name
            else f"<html><div id='s'>{url}</div></html>".encode()
        )
        media_type, declared = media_type_of(url, body, "text/html")
        return Retrieved(
            url=url, body=body, media_type=media_type,
            type_was_declared=declared, retrieved_at=datetime.now(timezone.utc),
        )


@pytest.fixture()
def home(tmp_path: Path) -> Path:
    return tmp_path / "aeropub"


def run_once(home: Path, transport=None):
    cycle, ledger, archive = build_cycle(
        home, only="OT", transport=transport or Offline()
    )
    try:
        return cycle.run(), archive
    finally:
        ledger.close()


class TestTheAssembly:

    def test_a_state_with_a_route_is_registered(self):
        assert "OT" in resolvers()

    def test_a_state_without_one_is_refused_by_name(self, home):
        """Absent here means not onboarded, which is not the same as a State
        that publishes nothing — so the message says so."""
        with pytest.raises(ValueError, match="not been onboarded"):
            build_cycle(home, only="ZZ")

    def test_a_run_reads_the_whole_aip(self, home):
        """Eighty sections plus the three lists — AMDT, SUP and AIC. This
        fixture's SUP list is empty, so no supplements sit behind it; the
        supplement path has its own test below."""
        report, _ = run_once(home)
        assert len(report.states[0].read) == 83

    def test_it_archives_to_disk(self, home):
        _, archive = run_once(home)
        assert len(archive) == 83
        assert archive.total_bytes() > 0

    def test_a_second_run_is_quiet(self, home):
        run_once(home)
        report, _ = run_once(home)
        assert report.quiet
        assert len(report.states[0].unchanged) == 83

    def test_the_ledger_survives_between_runs(self, home):
        """The point of the durable one: a restart re-reads nothing."""
        run_once(home)
        report, _ = run_once(home)
        assert not report.states[0].read

    def test_an_undeclared_edition_is_not_read(self, home):
        """No fallback. Reading next cycle's AIP as though in force is the
        failure the whole model avoids."""
        cycle, ledger, _ = build_cycle(home, only="OT", transport=Offline())
        try:
            assert cycle._pick(()) is None
        finally:
            ledger.close()

    def test_next_can_be_chosen_deliberately(self, home):
        cycle, ledger, _ = build_cycle(
            home, only="OT", edition="next", transport=Offline()
        )
        try:
            report = cycle.run()
            assert report.states[0].edition.label.startswith("AIRAC AIP AMDT")
        finally:
            ledger.close()

    def test_a_bad_edition_name_is_refused(self, home):
        with pytest.raises(ValueError, match="current"):
            build_cycle(home, only="OT", edition="whenever")


class TestALostArchiveIsCaught:
    """The check that catches it belongs before the work, not behind a flag
    somebody remembers to set."""

    def test_reconcile_reports_a_sound_store(self, home, capsys):
        run_once(home)
        assert main(["--home", str(home), "reconcile"]) == OK
        assert "Sound" in capsys.readouterr().out

    def test_reconcile_finds_a_missing_archive(self, home, capsys):
        run_once(home)
        import shutil

        shutil.rmtree(home / "archive")
        assert main(["--home", str(home), "reconcile", "--check"]) == INCOMPLETE
        assert "CLAIMED A COPY THE ARCHIVE DOES NOT HOLD" in capsys.readouterr().out

    def test_check_does_not_forget(self, home):
        from aeropub.ledger import SqliteLedger
        import shutil

        run_once(home)
        shutil.rmtree(home / "archive")
        main(["--home", str(home), "reconcile", "--check"])
        ledger = SqliteLedger(home / "ledger.db")
        assert len(list(ledger.entries())) == 83
        ledger.close()

    def test_reconcile_forgets_so_the_next_run_re_reads(self, home):
        import shutil

        run_once(home)
        shutil.rmtree(home / "archive")
        main(["--home", str(home), "reconcile"])
        report, archive = run_once(home)
        assert len(report.states[0].read) == 83
        assert len(archive) == 83

    def test_a_run_reconciles_before_it_fetches(self, home, capsys):
        """Not behind a flag: a ledger outliving its archive answers UNCHANGED
        forever and says nothing."""
        import shutil

        run_once(home)
        shutil.rmtree(home / "archive")
        transport = Offline()
        cycle, ledger, archive = build_cycle(home, only="OT", transport=transport)
        ledger.close()
        main(["--home", str(home), "run", "--state", "OT", "--plan"])
        assert "CLAIMED A COPY" in capsys.readouterr().out

    def test_both_caches_are_cleared(self, home):
        """Forgetting only in the ledger looks fixed and is not: the transport
        still holds an ETag and the server answers 304."""
        import shutil

        from aeropub.ledger import SqliteLedger
        from aeropub.archive import Archive

        run_once(home)
        shutil.rmtree(home / "archive")
        transport = Offline()
        ledger = SqliteLedger(home / "ledger.db")
        ledger.reconcile(holds=Archive(home / "archive").has,
                         on_forget=transport.forget)
        ledger.close()
        assert len(transport.forgotten) == 83


class TestNothingReadsAsHealthWhenItIsNot:

    def test_status_on_a_sound_store_exits_ok(self, home, capsys):
        run_once(home)
        assert main(["--home", str(home), "status"]) == OK
        assert "83 known, 83 backed" in capsys.readouterr().out

    def test_status_does_not_report_known_as_held(self, home, capsys):
        """'80 documents' beside an empty archive is absence rendering as a
        pass — in our own status output."""
        import shutil

        run_once(home)
        shutil.rmtree(home / "archive")
        assert main(["--home", str(home), "status"]) == INCOMPLETE
        out = capsys.readouterr().out
        assert "only 0 backed by a copy" in out
        assert "answer UNCHANGED while holding nothing" in out

    def test_status_on_nothing_is_an_error_not_an_empty_report(self, tmp_path):
        assert main(["--home", str(tmp_path / "never"), "status"]) == CANNOT_RUN

    def test_a_complete_run_exits_ok(self, home, capsys):
        cycle, ledger, _ = build_cycle(home, only="OT", transport=Offline())
        report = cycle.run()
        ledger.close()
        assert report.states[0].is_complete


class TestSupplementsAreRecordedByARun:
    """A run that found supplements must leave them where a person can fill in
    the validity windows, and must say why when it cannot."""

    SUPS = (
        "https://aim.gov.qa/AIP/11-JUN-2026/AIP-29/2026-08-06-000000/html/"
        "eSUP/QA-eSUPs-en-GB.html"
    )
    LISTING = b"".join(
        b'<a href="QA-SUP-%02d-2026-en-GB.html">SUP %02d/2026</a>' % (n, n)
        for n in range(7, 16)
    )

    class WithSupplements(Offline):
        def __call__(self, url: str) -> Retrieved:
            if url == TestSupplementsAreRecordedByARun.SUPS:
                body = TestSupplementsAreRecordedByARun.LISTING
                media_type, declared = media_type_of(url, body, "text/html")
                return Retrieved(
                    url=url, body=body, media_type=media_type,
                    type_was_declared=declared,
                    retrieved_at=datetime.now(timezone.utc),
                )
            return super().__call__(url)

    def test_the_manifest_is_written(self, home, capsys):
        from aeropub.watch import _write_supplements

        report, _ = run_once(home, self.WithSupplements())
        _write_supplements(home, report)
        assert (home / "supplements-OT.json").exists()
        assert "9 supplements recorded" in capsys.readouterr().out

    def test_it_loads_back_with_windows_unread(self, home):
        from aeropub.supplement import load_supplements
        from aeropub.watch import _write_supplements

        report, _ = run_once(home, self.WithSupplements())
        _write_supplements(home, report)
        register = load_supplements(home / "supplements-OT.json")
        assert len(register) == 9
        assert len(register.of_unread_window()) == 9

    def test_status_reports_them_as_unread(self, home, capsys):
        from aeropub.watch import _write_supplements

        report, _ = run_once(home, self.WithSupplements())
        _write_supplements(home, report)
        capsys.readouterr()
        main(["--home", str(home), "status"])
        assert "9 with no window read" in capsys.readouterr().out

    def test_nothing_is_written_when_the_list_was_not_read(self, home, capsys):
        """The list is what the manifest cites. Without it the file cannot be
        read back, so none is written and the reason is said."""
        from aeropub.cycle import Outcome
        from aeropub.watch import _write_supplements
        import dataclasses

        report, _ = run_once(home, self.WithSupplements())
        state = report.states[0]
        stripped = tuple(
            dataclasses.replace(d, result=None, outcome=Outcome.FAILED)
            if d.result is not None
            and d.result.publication.code.startswith("eSUP")
            else d
            for d in state.documents
        )
        report = dataclasses.replace(
            report, states=(dataclasses.replace(state, documents=stripped),)
        )
        _write_supplements(home, report)
        assert not (home / "supplements-OT.json").exists()
        assert "nothing could cite them" in capsys.readouterr().out

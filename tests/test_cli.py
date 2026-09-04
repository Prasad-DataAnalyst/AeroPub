"""The command line.

Two things are worth testing here and they are not "does argparse work".

**The exit codes carry meaning.** ``0`` produced a document, ``1`` the answer
is adverse, ``2`` the command could not run. A script that treats "I could not
tell" as "no" acts on the wrong one, so the distinction is tested rather than
documented.

**An empty store never reads as a quiet aerodrome.** Every command over a store
with nothing in it must say so in its own output. That line is the only thing
separating "we read the AIP and there is nothing to report" from "nobody has
ever loaded anything", and those are opposite answers that otherwise print the
same document.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aeropub.cli import ADVERSE, CANNOT_RUN, OK, main

READ_AT = "2026-09-01T12:00:00Z"


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    (tmp_path / "ad2.txt").write_text("a page somebody read\n", encoding="utf-8")
    (tmp_path / "acap.txt").write_text("a characteristics document\n", encoding="utf-8")
    return tmp_path


def write(path: Path, payload: dict) -> str:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


def aerodrome_manifest(workspace: Path, **facts) -> str:
    return write(workspace / "ad2.json", {
        "source": {
            "source_id": "EXAMPLE-CAA",
            "document": "AIP Example AD 2 XXXX",
            "document_path": "ad2.txt",
            "retrieved_at": READ_AT,
        },
        "precedence": "aip",
        "valid_from": "2020-01-01",
        "facts": [
            {"entity": entity, "attribute": attribute, "value": value,
             "locator": f"AD 2, {attribute}"}
            for entity, attribute, value in facts["rows"]
        ],
    })


def aircraft_manifest(workspace: Path, *characteristics) -> str:
    return write(workspace / "plane.json", {
        "designator": "TEST",
        "source": {
            "source_id": "EXAMPLE",
            "document": "Airplane Characteristics for Airport Planning",
            "document_path": "acap.txt",
            "retrieved_at": READ_AT,
        },
        "origin": "acap",
        "characteristics": [
            {"attribute": a, "value": v, "locator": "Table 2.1.1", **extra}
            for a, v, extra in characteristics
        ],
    })


def run(capsys, *argv) -> tuple[int, str, str]:
    code = main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


class TestExitCodes:
    def test_a_produced_document_exits_zero(self, capsys, workspace):
        code, _, _ = run(capsys, "--store", str(workspace / "s.db"), "dossier", "XXXX")
        assert code == OK

    def test_an_adverse_answer_exits_one(self, capsys, workspace):
        rows = [("XXXX", "rffs_category", 5)]
        aerodrome = aerodrome_manifest(workspace, rows=rows)
        plane = aircraft_manifest(
            workspace,
            ("overall_length_m", 70.0, {}),
            ("fuselage_width_m", 6.2, {}),
        )
        store = str(workspace / "s.db")
        run(capsys, "--store", store, "load", aerodrome)
        code, out, _ = run(
            capsys, "--store", store, "fit", "XXXX", "--aircraft", plane
        )
        assert code == ADVERSE
        assert "not_suitable" in out

    def test_an_inconclusive_answer_does_not_exit_adverse(self, capsys, workspace):
        # "I could not tell" is not "no". A caller that conflates them acts on
        # the wrong one.
        plane = aircraft_manifest(workspace, ("wingspan_m", 60.0, {}))
        code, out, _ = run(
            capsys, "--store", str(workspace / "s.db"),
            "fit", "XXXX", "--aircraft", plane,
        )
        assert code == OK
        assert "NOT CONCLUSIVE" in out

    def test_a_manifest_that_cannot_be_read_exits_two(self, capsys, workspace):
        code, _, err = run(
            capsys, "--store", str(workspace / "s.db"),
            "fit", "XXXX", "--aircraft", str(workspace / "absent.json"),
        )
        assert code == CANNOT_RUN
        assert "absent.json" in err

    def test_a_bad_cycle_identifier_exits_two(self, capsys, workspace):
        code, _, err = run(
            capsys, "--store", str(workspace / "s.db"),
            "bulletin", "XXXX", "--from", "nonsense", "--to", "2610",
        )
        assert code == CANNOT_RUN
        assert err

    def test_an_unknown_audience_is_rejected_by_the_parser(self, capsys, workspace):
        with pytest.raises(SystemExit) as caught:
            main(["lens", "XXXX", "--audience", "everyone"])
        assert caught.value.code == 2


class TestAnEmptyStoreSaysSo:
    @pytest.mark.parametrize(
        "argv",
        [
            ("dossier", "XXXX"),
            ("horizon", "XXXX"),
            ("quality",),
            ("lens", "XXXX", "--audience", "flight_crew"),
        ],
    )
    def test_every_report_over_an_empty_store_says_it_is_empty(
        self, capsys, workspace, argv
    ):
        code, out, _ = run(capsys, "--store", str(workspace / "s.db"), *argv)
        assert code == OK
        assert "holds no facts" in out or "NOT SOUND" in out

    def test_the_store_command_says_so_loudest(self, capsys, workspace):
        code, out, _ = run(capsys, "--store", str(workspace / "s.db"), "store")
        assert code == OK
        assert "Empty" in out
        assert "coverage gap from end to end" in out

    def test_a_loaded_store_stops_saying_it(self, capsys, workspace):
        store = str(workspace / "s.db")
        aerodrome = aerodrome_manifest(workspace, rows=[("XXXX", "rffs_category", 9)])
        run(capsys, "--store", store, "load", aerodrome)
        _, out, _ = run(capsys, "--store", store, "dossier", "XXXX")
        assert "holds no facts" not in out


class TestLoad:
    def test_facts_reach_the_store(self, capsys, workspace):
        store = str(workspace / "s.db")
        aerodrome = aerodrome_manifest(workspace, rows=[
            ("XXXX", "rffs_category", 9),
            ("XXXX/RWY34L", "pcn", "80/F/A/W/T"),
        ])
        code, out, _ = run(capsys, "--store", store, "load", aerodrome)
        assert code == OK
        assert "2 facts" in out
        _, listing, _ = run(capsys, "--store", store, "store", "-v")
        assert "XXXX/RWY34L" in listing
        assert "pcn" in listing

    def test_a_bad_manifest_writes_nothing(self, capsys, workspace):
        # Every manifest is parsed before anything is written: a partial AIP
        # section is worse than none, because the sections that did load look
        # complete.
        store = str(workspace / "s.db")
        good = aerodrome_manifest(workspace, rows=[("XXXX", "rffs_category", 9)])
        bad = write(workspace / "bad.json", {
            "source": {"source_id": "X", "document": "D",
                       "document_path": "ad2.txt", "retrieved_at": READ_AT},
            "precedence": "aip", "valid_from": "2020-01-01",
            "facts": [{"entity": "XXXX", "attribute": "elevation_ft", "value": 35}],
        })
        code, _, err = run(capsys, "--store", store, "load", good, bad)
        assert code == CANNOT_RUN
        assert "locator" in err
        _, listing, _ = run(capsys, "--store", store, "store")
        assert "Empty" in listing

    def test_a_template_is_offered_when_no_manifest_is_given(self, capsys, workspace):
        code, out, _ = run(capsys, "--store", str(workspace / "s.db"), "load")
        assert code == CANNOT_RUN
        code, out, _ = run(capsys, "--store", str(workspace / "s.db"),
                           "load", "--template")
        assert code == OK
        assert json.loads(out)["precedence"] == "aip"


class TestOutput:
    def test_json_and_text_come_from_the_same_document(self, capsys, workspace):
        store = str(workspace / "s.db")
        aerodrome = aerodrome_manifest(workspace, rows=[("XXXX", "rffs_category", 9)])
        run(capsys, "--store", store, "load", aerodrome)
        _, printed, _ = run(capsys, "--store", store, "dossier", "XXXX")
        _, emitted, _ = run(capsys, "--store", store, "dossier", "XXXX", "--json")
        payload = json.loads(emitted)
        assert payload["aeropub"]["kind"] == "aerodrome_dossier"
        # The same value appears in both renderings of the same document.
        assert "rffs_category" in printed
        assert any(
            "rffs_category" in json.dumps(section)
            for section in payload["data"]["sections"]
        )

    def test_the_html_page_is_written_where_asked(self, capsys, workspace):
        store = str(workspace / "s.db")
        target = workspace / "page.html"
        code, out, _ = run(
            capsys, "--store", store, "dossier", "XXXX", "--html", str(target)
        )
        assert code == OK
        assert target.is_file()
        assert "<" in target.read_text(encoding="utf-8")
        assert str(target) in out

    def test_airac_prints_the_distribution_deadlines(self, capsys):
        # The dates an AIS office works to. All three precede the effective
        # date, which is what makes lateness measurable.
        code, out, _ = run(capsys, "airac")
        assert code == OK
        for line in ("distribution", "major change", "in recipients' hands"):
            assert line in out

    def test_aircraft_shows_what_may_not_be_redistributed(self, capsys, workspace):
        plane = aircraft_manifest(
            workspace, ("wingspan_m", 60.0, {"unit": "m"})
        )
        code, out, _ = run(capsys, "aircraft", plane)
        assert code == OK
        assert "Code E" in out
        # Every figure prints its citation beneath it.
        assert "Table 2.1.1" in out


def operator_profile(workspace: Path, name: str, role: str, *, sole: bool = False) -> str:
    plane = aircraft_manifest(
        workspace,
        ("overall_length_m", 70.0, {}),
        ("fuselage_width_m", 6.2, {}),
    )
    return write(workspace / f"{name.lower().replace(' ', '-')}.json", {
        "name": name,
        "fleet": [plane],
        "network": [
            {"aerodrome": "XXXX", "role": role, "sole_suitable": sole},
        ],
    })


class TestExposure:
    def test_the_same_aerodrome_splits_two_operators(self, capsys, workspace):
        # The plan's headline, from a terminal: one loaded document, two
        # profiles, two different answers and two different exit codes.
        store = str(workspace / "s.db")
        run(capsys, "--store", store, "load",
            aerodrome_manifest(workspace, rows=[("XXXX", "rffs_category", 7)]))

        exposed = operator_profile(workspace, "Operator A", "edto_alternate", sole=True)
        code, out, _ = run(
            capsys, "--store", store, "exposure", "XXXX", "--profile", exposed
        )
        assert code == ADVERSE
        assert "CRITICAL" in out

        run(capsys, "--store", store, "load",
            aerodrome_manifest(workspace, rows=[("XXXX", "rffs_category", 9)]))
        # A profile whose aeroplane the published category now satisfies.
        fine = operator_profile(workspace, "Operator B", "destination")
        code, out, _ = run(
            capsys, "--store", store, "exposure", "XXXX", "--profile", fine
        )
        assert code == OK

    def test_an_unknown_exposure_does_not_exit_adverse(self, capsys, workspace):
        # "Nobody checked" and "no" are different answers.
        store = str(workspace / "s.db")
        fine = operator_profile(workspace, "Operator C", "destination")
        code, out, _ = run(
            capsys, "--store", store, "exposure", "XXXX", "--profile", fine
        )
        assert code == OK
        assert "unknown" in out.lower()

    def test_a_template_is_offered(self, capsys, workspace):
        code, out, _ = run(capsys, "exposure", "--template")
        assert code == OK
        assert json.loads(out)["network"][0]["role"] == "destination"

    def test_an_aerodrome_without_a_profile_cannot_run(self, capsys, workspace):
        code, _, err = run(
            capsys, "--store", str(workspace / "s.db"), "exposure", "XXXX"
        )
        assert code == CANNOT_RUN
        assert "--profile" in err

    def test_an_unknown_role_names_the_valid_ones(self, capsys, workspace):
        bad = write(workspace / "bad-profile.json", {
            "name": "Operator D", "fleet": [],
            "network": [{"aerodrome": "XXXX", "role": "sometimes"}],
        })
        code, _, err = run(
            capsys, "--store", str(workspace / "s.db"),
            "exposure", "XXXX", "--profile", bad,
        )
        assert code == CANNOT_RUN
        assert "edto_alternate" in err
        assert "never defaulted" in err

    def test_the_payload_carries_the_shared_record(self, capsys, workspace):
        store = str(workspace / "s.db")
        run(capsys, "--store", store, "load",
            aerodrome_manifest(workspace, rows=[("XXXX", "rffs_category", 7)]))
        profile = operator_profile(workspace, "Operator E", "destination")
        _, out, _ = run(
            capsys, "--store", store, "exposure", "XXXX",
            "--profile", profile, "--json",
        )
        payload = json.loads(out)
        assert payload["aeropub"]["kind"] == "operator_exposure"
        assert payload["data"]["suitability"]


class TestSweep:
    def network_profile(self, workspace: Path) -> str:
        plane = aircraft_manifest(
            workspace,
            ("overall_length_m", 70.0, {}),
            ("fuselage_width_m", 6.2, {}),
        )
        return write(workspace / "network.json", {
            "name": "Example Airways",
            "fleet": [plane],
            "network": [
                {"aerodrome": "XXXX", "role": "edto_alternate", "sole_suitable": True},
                {"aerodrome": "ZZZZ", "role": "destination"},
            ],
        })

    def test_an_unread_aerodrome_is_counted_apart_from_the_clear_ones(self, capsys, workspace):
        store = str(workspace / "s.db")
        run(capsys, "--store", store, "load",
            aerodrome_manifest(workspace, rows=[("XXXX", "rffs_category", 9)]))
        code, out, _ = run(
            capsys, "--store", store, "sweep",
            "--profile", self.network_profile(workspace),
        )
        # Adverse, and correctly so: the pavement and width checks could not be
        # made at a sole-suitable EDTO alternate, which grades HIGH rather than
        # UNKNOWN. The coverage arithmetic is what this test is about.
        assert code == ADVERSE
        assert "1 never read" in out
        assert "NOTHING HELD" in out
        assert "ZZZZ" in out

    def test_an_adverse_network_exits_one(self, capsys, workspace):
        store = str(workspace / "s.db")
        run(capsys, "--store", store, "load",
            aerodrome_manifest(workspace, rows=[("XXXX", "rffs_category", 7)]))
        code, out, _ = run(
            capsys, "--store", store, "sweep",
            "--profile", self.network_profile(workspace),
        )
        assert code == ADVERSE
        assert "CRITICAL" in out

    def test_the_payload_keeps_coverage_beside_every_severity_count(self, capsys, workspace):
        store = str(workspace / "s.db")
        run(capsys, "--store", store, "load",
            aerodrome_manifest(workspace, rows=[("XXXX", "rffs_category", 9)]))
        _, out, _ = run(
            capsys, "--store", store, "sweep",
            "--profile", self.network_profile(workspace), "--json",
        )
        payload = json.loads(out)
        assert payload["aeropub"]["kind"] == "network_sweep"
        summary = payload["data"]["summary"]
        assert summary["covered"] + summary["uncovered"] == summary["aerodromes"]
        assert payload["data"]["uncovered"] == ["ZZZZ"]


class TestCurrency:
    def test_an_empty_store_has_nothing_whose_age_could_be_reported(self, capsys, workspace):
        code, out, _ = run(capsys, "--store", str(workspace / "s.db"), "currency")
        assert code == OK
        assert "nothing whose age" in out

    def test_data_read_inside_the_current_cycle_is_current(self, capsys, workspace):
        # The manifest fixture records its reading at 2026-09-01, which falls
        # in AIRAC 2608 (effective 2026-08-06, running to 2026-09-02).
        store = str(workspace / "s.db")
        run(capsys, "--store", store, "load",
            aerodrome_manifest(workspace, rows=[("XXXX", "rffs_category", 9)]))
        code, out, _ = run(capsys, "--store", store, "currency", "--on", "2026-09-02")
        assert code == OK
        assert "1 current" in out

    def test_one_cycle_later_the_same_data_is_ageing(self, capsys, workspace):
        # 2026-09-03 begins AIRAC 2609. Nothing about the data changed; the
        # calendar moved, and an amendment could have landed in the gap.
        store = str(workspace / "s.db")
        run(capsys, "--store", store, "load",
            aerodrome_manifest(workspace, rows=[("XXXX", "rffs_category", 9)]))
        code, out, _ = run(capsys, "--store", store, "currency", "--on", "2026-09-10")
        assert code == OK
        assert "1 ageing" in out

    def test_stale_data_exits_adverse_and_says_why(self, capsys, workspace):
        store = str(workspace / "s.db")
        run(capsys, "--store", store, "load",
            aerodrome_manifest(workspace, rows=[("XXXX", "rffs_category", 9)]))
        code, out, _ = run(capsys, "--store", store, "currency", "--on", "2027-02-01")
        assert code == ADVERSE
        assert "stale" in out
        assert "counted in AIRAC cycles, not days" in out

    def test_stale_only_narrows_the_listing(self, capsys, workspace):
        store = str(workspace / "s.db")
        run(capsys, "--store", store, "load",
            aerodrome_manifest(workspace, rows=[("XXXX", "rffs_category", 9)]))
        _, out, _ = run(capsys, "--store", store, "currency",
                        "--on", "2026-09-02", "--stale-only")
        assert "XXXX:" not in out

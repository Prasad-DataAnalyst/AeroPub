"""Registering the FAA on the status board, and verifying the connection.

Covers the two things an operator interacts with directly: the rows the FAA
puts on the board, and ``python -m aeropub.faa.check``, which answers whether
anything is actually reaching the FAA.
"""

from __future__ import annotations

import io
import json
from datetime import datetime, timedelta, timezone
from email.message import Message

import pytest

from aeropub.faa.auth import AccessToken, TokenClient
from aeropub.faa.check import (
    EXIT_CREDENTIALS,
    EXIT_OK,
    EXIT_UNAVAILABLE,
    ConnectionReport,
    main,
    verify,
)
from aeropub.faa.client import NmsClient
from aeropub.faa.config import ENVIRONMENTS, ClientCredentials
from aeropub.faa.errors import NmsTransportError, NmsUnavailableError
from aeropub.faa.sources import credential_rows, nms_sources
from aeropub.http import HostThrottle
from aeropub.registry import (
    CredentialStatus,
    DetectionTier,
    Redistribution,
    SourceKind,
    SourceRegistry,
    render_board,
)
from aeropub.states import get_profile
from aeropub.states.united_states import profile as us_profile

BOTH = {"FAA_NMS_CLIENT_ID": "id-value-xyz", "FAA_NMS_CLIENT_SECRET": "secret-value-xyz"}
NOW = datetime(2025, 9, 12, 17, 25, tzinfo=timezone.utc)


class TestSources:
    def test_registers_the_endpoints_worth_watching(self):
        ids = {s.source_id for s in nms_sources(ENVIRONMENTS["prod"], environ={})}
        assert ids == {
            "FAA-NMS-PROD-NOTAM",
            "FAA-NMS-PROD-CHECKLIST",
            "FAA-NMS-PROD-INITIAL-LOAD",
            "FAA-NMS-PROD-LOCATIONS",
        }

    def test_source_ids_name_the_environment(self):
        # Staging and production must never collide in the registry: a NOTAM
        # read from staging is not evidence about the real world.
        staging = {s.source_id for s in nms_sources(ENVIRONMENTS["staging"], environ={})}
        prod = {s.source_id for s in nms_sources(ENVIRONMENTS["prod"], environ={})}
        assert staging.isdisjoint(prod)

    def test_notam_is_watched_at_push_cadence(self):
        sources = {s.source_id: s for s in nms_sources(ENVIRONMENTS["prod"], environ={})}
        assert sources["FAA-NMS-PROD-NOTAM"].tier is DetectionTier.PUSH
        assert sources["FAA-NMS-PROD-NOTAM"].check_interval == timedelta(minutes=1)

    def test_the_initial_load_is_a_daily_baseline_not_the_feed(self):
        sources = {s.source_id: s for s in nms_sources(ENVIRONMENTS["prod"], environ={})}
        assert sources["FAA-NMS-PROD-INITIAL-LOAD"].check_interval == timedelta(hours=24)

    def test_redistribution_is_conditional_not_assumed_permitted(self):
        # US Government work, but the NMS-API terms attach conditions. The
        # render layer gates on this, and guessing would publish somebody
        # else's data on our assumption rather than their terms.
        for source in nms_sources(ENVIRONMENTS["prod"], environ={}):
            assert source.redistribution is Redistribution.CONDITIONAL

    def test_nothing_is_verified_until_a_call_succeeds(self):
        # A URL from the FAA's own onboarding pack is a well-sourced claim, and
        # a claim is not evidence.
        assert all(not s.is_verified for s in nms_sources(ENVIRONMENTS["prod"], environ={}))

    def test_the_secret_is_the_credential_the_board_watches(self):
        for source in nms_sources(ENVIRONMENTS["prod"], environ={}):
            assert source.credential is not None
            assert source.credential.env_var == "FAA_NMS_CLIENT_SECRET"

    def test_the_sources_go_on_the_same_board_as_everything_else(self):
        registry = SourceRegistry(nms_sources(ENVIRONMENTS["prod"], environ={}))
        rows = registry.board(environ={})
        assert len(rows) == 4
        assert all(r.needs_attention for r in rows)  # no key installed yet
        board = render_board(rows)
        assert "FAA" in board and "credential_missing" in board
        assert "secret" not in board.lower().replace("faa_nms_client_secret", "")


class TestCredentialRows:
    def test_reports_each_half_separately(self):
        rows = credential_rows(environ={"FAA_NMS_CLIENT_ID": "key"})
        by_var = {r.env_var: r for r in rows}
        assert by_var["FAA_NMS_CLIENT_ID"].status is CredentialStatus.UNVERIFIED
        assert by_var["FAA_NMS_CLIENT_SECRET"].status is CredentialStatus.MISSING

    def test_a_rejected_key_reads_invalid_not_missing(self):
        rows = credential_rows(environ=BOTH, rejected=True)
        assert all(r.status is CredentialStatus.INVALID for r in rows)

    def test_a_row_carries_no_value(self):
        # The env var name and the label are meant to be visible; the value
        # behind them is not, and a row is what the status screen renders.
        rows = credential_rows(environ=BOTH)
        rendered = repr(rows)
        assert "id-value-xyz" not in rendered
        assert "secret-value-xyz" not in rendered
        assert "FAA_NMS_CLIENT_SECRET" in rendered
        assert all(r.hint is None for r in rows)


class TestUnitedStatesProfile:
    def test_is_registered_under_the_k_prefix(self):
        assert get_profile("K").name == "United States"

    def test_reflects_the_environment_in_use(self):
        # A board showing production URLs for a connection pointed at staging
        # would be wrong in the quiet way this project exists to avoid.
        staging = us_profile(ENVIRONMENTS["staging"], environ={})
        assert all("api-staging" in s.url for s in staging.sources)

    def test_claims_nothing_it_has_not_connected(self):
        us = us_profile(ENVIRONMENTS["prod"], environ={})
        assert SourceKind.AIP in us.unknown_kinds()
        assert SourceKind.CHARTS in us.unknown_kinds()
        assert us.absent == frozenset()

    def test_records_the_other_prefixes_the_faa_covers(self):
        # So a lookup on PANC or TJSJ is a known gap, not a silent miss.
        assert "TJ" in us_profile(ENVIRONMENTS["prod"], environ={}).notes


class _Response(io.BytesIO):
    def __init__(self, body, status=200):
        super().__init__(body)
        self.status = status
        self.headers = Message()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def _client(*, ping=None, error=None):
    tokens = TokenClient(
        ENVIRONMENTS["staging"], environ=BOTH, clock=lambda: NOW,
        opener=lambda request, timeout=None: _Response(
            json.dumps({"access_token": "BEARER TOKEN HERE", "expires_in": "1799",
                        "organization_name": "faa-XXXX", "status": "approved",
                        "api_product_list_json": ["FAA Staging Preprod APIs"]}).encode()
        ),
    )

    def opener(request, timeout=None):
        if error is not None:
            raise error
        return _Response(ping or b"pong")

    return NmsClient(
        ENVIRONMENTS["staging"],
        tokens=tokens,
        throttle=HostThrottle(gap=timedelta(0)),
        opener=opener,
        clock=lambda: NOW,
    )


class TestVerify:
    def test_stops_at_the_missing_credential_rather_than_calling(self):
        # Asking for NOTAM when the key is absent produces a second, less
        # informative error about the same fault.
        report = verify(ENVIRONMENTS["staging"], environ={})
        assert report.exit_code == EXIT_CREDENTIALS
        assert [s.name for s in report.stages] == ["configuration", "credentials"]
        assert "FAA_NMS_CLIENT_ID" in report.stages[-1].detail
        assert "spreadsheet" in report.stages[-1].detail

    def test_a_working_connection_reaches_ping(self):
        report = verify(ENVIRONMENTS["staging"], environ=BOTH, client=_client())
        assert report.ok and report.exit_code == EXIT_OK
        assert [s.name for s in report.stages] == [
            "configuration",
            "credentials",
            "token",
            "ping",
        ]

    def test_an_unavailable_gateway_is_told_apart_from_a_bad_key(self):
        report = verify(
            ENVIRONMENTS["staging"],
            environ=BOTH,
            client=_client(error=NmsUnavailableError("HTTP 503", status=503)),
        )
        assert report.exit_code == EXIT_UNAVAILABLE

    def test_the_report_names_the_environment_and_marks_production(self):
        report = verify(ENVIRONMENTS["prod"], environ={})
        assert report.environment == "prod" and report.is_production
        assert "PRODUCTION" in report.render()

    def test_the_report_carries_no_token_value(self):
        report = verify(ENVIRONMENTS["staging"], environ=BOTH, client=_client())
        rendered = report.render() + json.dumps(report.to_dict())
        assert "BEARER TOKEN HERE" not in rendered
        assert report.token["masked"] == "****"
        assert report.token["organization"] == "faa-XXXX"

    def test_the_report_serialises_for_the_status_api(self):
        report = verify(ENVIRONMENTS["staging"], environ=BOTH, client=_client())
        document = json.loads(json.dumps(report.to_dict()))
        assert document["ok"] is True
        assert document["environment"] == "staging"
        assert [s["name"] for s in document["stages"]][-1] == "ping"
        assert {c["env_var"] for c in document["credentials"]} == set(BOTH)

    def test_the_check_builds_a_client_that_waits_for_the_throttle(self, monkeypatch):
        # Regression. The stages run back to back against one host, so with the
        # default two-second gap the data stage failed with "the FAA is
        # unavailable" when nothing was wrong with the FAA at all.
        import aeropub.faa.check as check_module

        captured = {}

        class Recording(NmsClient):
            def __init__(self, *args, **kwargs):
                captured.update(kwargs)
                raise NmsTransportError("stop here — construction is what is under test")

        monkeypatch.setattr(check_module, "NmsClient", Recording)
        with pytest.raises(NmsTransportError):
            verify(ENVIRONMENTS["staging"], environ=BOTH)
        assert captured["wait_for_throttle"] is True

    def test_an_unreachable_gateway_reports_the_stage_that_broke(self):
        report = verify(
            ENVIRONMENTS["staging"],
            environ=BOTH,
            client=_client(error=NmsTransportError("could not reach host")),
        )
        assert report.exit_code == EXIT_UNAVAILABLE
        failed = [s for s in report.stages if not s.ok]
        assert [s.name for s in failed] == ["ping"]


class TestCli:
    def test_exits_non_zero_when_the_key_is_absent(self, capsys, monkeypatch):
        monkeypatch.delenv("FAA_NMS_CLIENT_ID", raising=False)
        monkeypatch.delenv("FAA_NMS_CLIENT_SECRET", raising=False)
        assert main(["--environment", "staging"]) == EXIT_CREDENTIALS
        assert "FAA_NMS_CLIENT_ID" in capsys.readouterr().out

    def test_json_output_is_a_document(self, capsys, monkeypatch):
        monkeypatch.delenv("FAA_NMS_CLIENT_ID", raising=False)
        main(["--environment", "staging", "--json"])
        document = json.loads(capsys.readouterr().out)
        assert document["environment"] == "staging"
        assert document["ok"] is False

    def test_an_unknown_environment_is_reported_not_guessed(self, capsys):
        assert main(["--environment", "mars"]) != EXIT_OK
        assert "mars" in capsys.readouterr().err

    def test_pulling_data_without_an_archive_is_refused(self, capsys):
        assert main(["--environment", "staging", "--data"]) != EXIT_OK
        assert "cannot be cited" in capsys.readouterr().err

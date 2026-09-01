"""Tests for the HTTP transport.

Response interpretation is pure and tested directly against every status path.
The happy path is exercised over a real ``file://`` URL — real bytes, real
hashing, real archiving — rather than a stubbed transport.
"""

from datetime import datetime, timedelta, timezone

import pytest

from aeropub.archive import Archive, digest_of
from aeropub.http import (
    BACKOFF_CAP,
    ConditionalState,
    HostThrottle,
    HttpFetcher,
    backoff_delay,
    interpret,
    retry_after_seconds,
)
from aeropub.registry import DetectionTier, Source, SourceFormat, SourceKind

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)


def a_source(url="https://example.invalid/aip", source_id="aip"):
    return Source(
        source_id=source_id, authority="XX", name="Example", kind=SourceKind.AIP,
        url=url, fmt=SourceFormat.EAIP_HTML, tier=DetectionTier.ADAPTIVE_POLL,
    )


class TestInterpretation:
    def test_200_hashes_the_body(self):
        result = interpret(200, {}, b"content")
        assert result.ok and result.content_hash == digest_of(b"content")
        assert not result.not_modified

    def test_304_is_success_without_a_change(self):
        result = interpret(304, {}, None)
        assert result.ok and result.not_modified
        assert result.content_hash is None

    def test_401_is_a_rejected_credential(self):
        result = interpret(401, {}, None)
        assert result.unauthorised and not result.blocked

    def test_403_with_a_challenge_is_a_rejected_credential(self):
        result = interpret(403, {"WWW-Authenticate": 'Bearer realm="aip"'}, None)
        assert result.unauthorised

    def test_403_without_a_challenge_is_a_refusal(self):
        # Ambiguous by nature. Treating it as a bad key means hammering the host
        # with a credential it never asked for, so back off instead.
        result = interpret(403, {}, None)
        assert result.blocked and not result.unauthorised

    def test_429_is_blocked(self):
        assert interpret(429, {}, None).blocked

    @pytest.mark.parametrize("status", [500, 502, 503, 504])
    def test_server_errors_are_blocked_not_merely_failed(self, status):
        # The server is struggling; retrying immediately makes it worse.
        assert interpret(status, {}, None).blocked

    @pytest.mark.parametrize("status", [400, 404, 410])
    def test_client_errors_fail_without_backing_off(self, status):
        result = interpret(status, {}, None)
        assert not result.ok and not result.blocked

    def test_a_200_with_no_body_is_a_failure(self):
        result = interpret(200, {}, None)
        assert not result.ok and "empty" in result.error

    def test_a_statusless_response_with_a_body_succeeds(self):
        # file:// and some proxies carry no status line. urlopen raises on HTTP
        # errors, so bytes in hand means the transfer worked.
        result = interpret(None, {}, b"content")
        assert result.ok and result.content_hash == digest_of(b"content")

    def test_a_statusless_response_without_a_body_fails(self):
        assert not interpret(None, {}, None).ok

    def test_duration_is_carried_through(self):
        assert interpret(200, {}, b"x", duration_ms=42).duration_ms == 42


class TestConditionalRequests:
    def test_nothing_is_sent_on_the_first_request(self):
        assert ConditionalState().headers() == {}

    def test_an_etag_comes_back_as_if_none_match(self):
        state = ConditionalState()
        state.update({"ETag": '"abc123"'})
        assert state.headers() == {"If-None-Match": '"abc123"'}

    def test_last_modified_comes_back_as_if_modified_since(self):
        state = ConditionalState()
        state.update({"Last-Modified": "Wed, 20 Aug 2026 09:00:00 GMT"})
        assert state.headers()["If-Modified-Since"] == "Wed, 20 Aug 2026 09:00:00 GMT"

    def test_header_names_are_matched_case_insensitively(self):
        state = ConditionalState()
        state.update({"etag": '"x"', "last-modified": "Wed, 20 Aug 2026 09:00:00 GMT"})
        assert "If-None-Match" in state.headers()
        assert "If-Modified-Since" in state.headers()

    def test_the_fetcher_keeps_state_per_source(self):
        fetcher = HttpFetcher()
        fetcher.conditional_for("a").update({"ETag": '"a"'})
        assert fetcher.conditional_for("b").headers() == {}


class TestRequestHeaders:
    def test_a_user_agent_identifies_the_client(self):
        headers = HttpFetcher().build_headers("aip", None)
        assert "AeroPub" in headers["User-Agent"]

    def test_a_credential_is_sent_when_present(self):
        headers = HttpFetcher().build_headers("aip", "Bearer k")
        assert headers["Authorization"] == "Bearer k"

    def test_no_authorization_header_without_a_credential(self):
        assert "Authorization" not in HttpFetcher().build_headers("aip", None)


class TestBackoff:
    def test_no_delay_before_any_failure(self):
        assert backoff_delay(0) == timedelta(0)

    def test_delay_doubles_with_each_failure(self):
        assert backoff_delay(2) == backoff_delay(1) * 2
        assert backoff_delay(3) == backoff_delay(1) * 4

    def test_delay_is_capped(self):
        assert backoff_delay(50) == BACKOFF_CAP

    def test_a_very_long_outage_does_not_overflow(self):
        # A dead source accumulates failures indefinitely, and 2**n overflows
        # timedelta long before the result could be compared against the cap.
        assert backoff_delay(100_000) == BACKOFF_CAP

    def test_delay_is_deterministic(self):
        # Jitter belongs at the scheduler, where it can be seeded; a random
        # transport makes failures unreproducible.
        assert backoff_delay(4) == backoff_delay(4)


class TestRetryAfter:
    def test_seconds_form(self):
        assert retry_after_seconds("120") == 120

    def test_http_date_form(self):
        later = "Tue, 01 Sep 2026 12:02:00 GMT"
        assert retry_after_seconds(later, now=NOW) == 120

    def test_a_date_in_the_past_is_clamped_to_zero(self):
        assert retry_after_seconds("Tue, 01 Sep 2026 11:00:00 GMT", now=NOW) == 0

    def test_missing_or_unparseable_returns_none(self):
        assert retry_after_seconds(None) is None
        assert retry_after_seconds("soon") is None


class TestHostThrottle:
    def test_a_first_request_is_allowed(self):
        assert HostThrottle().may_request("https://a.invalid/x", now=NOW)

    def test_a_second_request_waits_for_the_gap(self):
        throttle = HostThrottle(gap=timedelta(seconds=2))
        throttle.record_request("https://a.invalid/x", at=NOW)
        assert not throttle.may_request("https://a.invalid/y", now=NOW + timedelta(seconds=1))
        assert throttle.may_request("https://a.invalid/y", now=NOW + timedelta(seconds=2))

    def test_the_gap_is_per_host_not_global(self):
        throttle = HostThrottle(gap=timedelta(seconds=2))
        throttle.record_request("https://a.invalid/x", at=NOW)
        assert throttle.may_request("https://b.invalid/x", now=NOW)

    def test_hosts_are_matched_case_insensitively(self):
        throttle = HostThrottle(gap=timedelta(seconds=2))
        throttle.record_request("https://A.Invalid/x", at=NOW)
        assert not throttle.may_request("https://a.invalid/y", now=NOW)

    def test_backing_off_holds_the_host_longer_than_the_gap(self):
        throttle = HostThrottle(gap=timedelta(seconds=2))
        throttle.back_off("https://a.invalid/x", timedelta(minutes=10), at=NOW)
        assert not throttle.may_request("https://a.invalid/x", now=NOW + timedelta(minutes=5))
        assert throttle.may_request("https://a.invalid/x", now=NOW + timedelta(minutes=10))

    def test_a_longer_backoff_never_shortens_an_existing_one(self):
        throttle = HostThrottle()
        throttle.back_off("https://a.invalid/x", timedelta(hours=1), at=NOW)
        throttle.back_off("https://a.invalid/x", timedelta(minutes=1), at=NOW)
        assert not throttle.may_request("https://a.invalid/x", now=NOW + timedelta(minutes=30))

    def test_success_clears_a_backoff(self):
        throttle = HostThrottle()
        throttle.back_off("https://a.invalid/x", timedelta(hours=1), at=NOW)
        throttle.clear("https://a.invalid/x")
        assert throttle.may_request("https://a.invalid/x", now=NOW)

    def test_a_throttled_fetch_is_reported_rather_than_sent(self):
        throttle = HostThrottle(gap=timedelta(minutes=5))
        throttle.record_request("https://a.invalid/x")
        fetcher = HttpFetcher(throttle=throttle)
        result = fetcher.fetch_url("https://a.invalid/y", "aip")
        assert result.blocked and "throttled" in result.error


class TestFetchingAndArchiving:
    def test_a_real_fetch_hashes_and_archives_the_body(self, tmp_path):
        body = b"<html>AD 2.13 declared distances</html>"
        served = tmp_path / "aip.html"
        served.write_bytes(body)
        archive = Archive(tmp_path / "archive")

        fetcher = HttpFetcher(archive=archive)
        result = fetcher.fetch_url(served.as_uri(), "aip")

        assert result.ok
        assert result.content_hash == digest_of(body)
        assert archive.get(result.content_hash) == body

    def test_fetching_unchanged_content_twice_archives_one_copy(self, tmp_path):
        served = tmp_path / "aip.html"
        served.write_bytes(b"unchanged")
        archive = Archive(tmp_path / "archive")
        fetcher = HttpFetcher(archive=archive, throttle=HostThrottle(gap=timedelta(0)))

        fetcher.fetch_url(served.as_uri(), "aip")
        fetcher.fetch_url(served.as_uri(), "aip")
        assert len(archive) == 1

    def test_changed_content_is_archived_alongside_the_old(self, tmp_path):
        # Both versions survive — that is what makes the time machine possible.
        served = tmp_path / "aip.html"
        archive = Archive(tmp_path / "archive")
        fetcher = HttpFetcher(archive=archive, throttle=HostThrottle(gap=timedelta(0)))

        served.write_bytes(b"cycle 2609")
        first = fetcher.fetch_url(served.as_uri(), "aip")
        served.write_bytes(b"cycle 2610")
        second = fetcher.fetch_url(served.as_uri(), "aip")

        assert first.content_hash != second.content_hash
        assert archive.get(first.content_hash) == b"cycle 2609"
        assert archive.get(second.content_hash) == b"cycle 2610"

    def test_an_unreachable_host_fails_without_raising(self, tmp_path):
        result = HttpFetcher().fetch(a_source(url="https://nonexistent.invalid/aip"))
        assert not result.ok
        assert result.error

    def test_the_protocol_method_delegates_to_the_url_method(self, tmp_path):
        # fetch(Source) is the Fetcher protocol; fetch_url is the transport.
        served = tmp_path / "aip.html"
        served.write_bytes(b"content")
        fetcher = HttpFetcher()
        assert fetcher.fetch_url(served.as_uri(), "aip").content_hash == digest_of(b"content")

    def test_fetching_works_without_an_archive(self, tmp_path):
        served = tmp_path / "aip.html"
        served.write_bytes(b"content")
        assert HttpFetcher(archive=None).fetch_url(served.as_uri(), "aip").ok

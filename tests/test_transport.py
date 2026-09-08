"""The live transport: conditional, polite, and honest about failure.

An AIP changes on a 28-day cycle and is checked far more often, so almost every
check should find nothing new. Downloading eighty sections from every State on
every pass to discover that would work, and would also mean pulling tens of
gigabytes a day from States' AIM servers to learn nothing happened. A State
that blocks our address becomes a silent coverage gap, so politeness here is
correctness, not courtesy.
"""

from __future__ import annotations

import urllib.error
from datetime import timedelta
from email.message import Message
from io import BytesIO

import pytest

from aeropub.http import HostThrottle
from aeropub.transport import DEFAULT_GAP, LiveTransport, TransportError


def headers_of(**pairs) -> Message:
    message = Message()
    for key, value in pairs.items():
        message[key.replace("_", "-")] = value
    return message


class FakeResponse:
    def __init__(self, body: bytes, status: int = 200, **headers):
        self._body, self.status = body, status
        self.headers = headers_of(**headers)

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture()
def opened(monkeypatch):
    """Records every request and replies however the test queued."""
    seen: list = []
    queue: list = []

    def _open(request, timeout=None):
        seen.append(request)
        reply = queue.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr("urllib.request.urlopen", _open)
    return seen, queue


def unthrottled(**kwargs) -> LiveTransport:
    """A transport with no host gap.

    For the tests about conditional behaviour, which make several requests to
    one host in one moment and are not about politeness. The throttle has its
    own tests, and it refusing here is it working.
    """
    return LiveTransport(throttle=HostThrottle(timedelta(0)), **kwargs)


def not_modified(**headers) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "https://aim.gov.qa/x", 304, "Not Modified", headers_of(**headers), BytesIO(b"")
    )


class TestAskingConditionally:

    def test_the_first_request_carries_no_validator(self, opened):
        seen, queue = opened
        queue.append(FakeResponse(b"<html>a</html>", Content_Type="text/html"))
        LiveTransport()("https://aim.gov.qa/a.html")
        assert "If-None-Match" not in seen[0].headers
        assert "If-Modified-Since" not in seen[0].headers

    def test_the_next_one_does(self, opened):
        seen, queue = opened
        queue.append(FakeResponse(b"<html>a</html>", ETag='"v1"', Content_Type="text/html"))
        queue.append(not_modified())
        transport = unthrottled()
        transport("https://aim.gov.qa/a.html")
        transport("https://aim.gov.qa/a.html")
        assert seen[1].headers["If-none-match"] == '"v1"'

    def test_last_modified_is_used_too(self, opened):
        seen, queue = opened
        when = "Thu, 03 Sep 2026 00:00:00 GMT"
        queue.append(FakeResponse(b"x", Last_Modified=when, Content_Type="text/html"))
        queue.append(not_modified())
        transport = unthrottled()
        transport("https://aim.gov.qa/a.html")
        transport("https://aim.gov.qa/a.html")
        assert seen[1].headers["If-modified-since"] == when

    def test_validators_are_kept_per_url(self, opened):
        """ENR 3.2 being amended says nothing about whether GEN 0.4 moved. A
        shared validator would re-read all eighty when one changed."""
        seen, queue = opened
        queue.append(FakeResponse(b"a", ETag='"enr"', Content_Type="text/html"))
        queue.append(FakeResponse(b"b", ETag='"gen"', Content_Type="text/html"))
        queue.append(not_modified())
        transport = unthrottled()
        transport("https://aim.gov.qa/ENR-3.2.html")
        transport("https://aim.gov.qa/GEN-0.4.html")
        transport("https://aim.gov.qa/ENR-3.2.html")
        assert seen[2].headers["If-none-match"] == '"enr"'

    def test_forgetting_makes_the_next_request_unconditional(self, opened):
        """For a document we hold no copy of: asking conditionally invites a
        304 confirming a copy that does not exist."""
        seen, queue = opened
        queue.append(FakeResponse(b"a", ETag='"v1"', Content_Type="text/html"))
        queue.append(FakeResponse(b"a", Content_Type="text/html"))
        transport = unthrottled()
        transport("https://aim.gov.qa/a.html")
        transport.forget("https://aim.gov.qa/a.html")
        transport("https://aim.gov.qa/a.html")
        assert "If-none-match" not in seen[1].headers


class TestA304IsTheGoodOutcome:

    def test_it_is_reported_as_unchanged(self, opened):
        _, queue = opened
        queue.append(not_modified())
        assert LiveTransport()("https://aim.gov.qa/a.html").unchanged

    def test_it_carries_no_body(self, opened):
        _, queue = opened
        queue.append(not_modified())
        assert LiveTransport()("https://aim.gov.qa/a.html").body == b""

    def test_hashing_it_is_refused(self, opened):
        """Hashing an absent body records every unchanged document as having
        changed to nothing."""
        _, queue = opened
        queue.append(not_modified())
        got = LiveTransport()("https://aim.gov.qa/a.html")
        with pytest.raises(ValueError, match="not modified"):
            got.content_hash

    def test_a_refreshed_validator_is_recorded(self, opened):
        seen, queue = opened
        queue.append(FakeResponse(b"a", ETag='"v1"', Content_Type="text/html"))
        queue.append(not_modified(ETag='"v2"'))
        queue.append(not_modified())
        transport = unthrottled()
        transport("https://aim.gov.qa/a.html")
        transport("https://aim.gov.qa/a.html")
        transport("https://aim.gov.qa/a.html")
        assert seen[2].headers["If-none-match"] == '"v2"'


class TestPolitenessIsCorrectness:

    def test_a_second_request_to_one_host_is_throttled(self, opened):
        _, queue = opened
        queue.append(FakeResponse(b"a", Content_Type="text/html"))
        transport = LiveTransport()
        transport("https://aim.gov.qa/a.html")
        with pytest.raises(TransportError, match="throttled"):
            transport("https://aim.gov.qa/b.html")

    def test_a_caller_may_wait_instead_of_failing(self, opened):
        _, queue = opened
        queue.append(FakeResponse(b"a", Content_Type="text/html"))
        queue.append(FakeResponse(b"b", Content_Type="text/html"))
        waited: list = []
        transport = LiveTransport(sleep=waited.append)
        transport("https://aim.gov.qa/a.html")
        transport("https://aim.gov.qa/b.html")
        assert waited and waited[0] > 0

    def test_a_429_backs_the_host_off(self, opened):
        """Retrying into a ban turns one busy moment into a coverage gap."""
        _, queue = opened
        queue.append(
            urllib.error.HTTPError(
                "https://aim.gov.qa/a", 429, "Too Many", headers_of(Retry_After="60"),
                BytesIO(b""),
            )
        )
        transport = LiveTransport()
        with pytest.raises(TransportError, match="backing off"):
            transport("https://aim.gov.qa/a.html")
        assert not transport.throttle.may_request("https://aim.gov.qa/b.html")

    def test_retry_after_is_honoured_not_overridden(self, opened):
        from aeropub.transport import _retry_after

        assert _retry_after(headers_of(Retry_After="90")) == timedelta(seconds=90)

    def test_a_missing_retry_after_gets_a_conservative_default(self):
        from aeropub.transport import _retry_after

        assert _retry_after(headers_of()) >= timedelta(minutes=1)

    def test_a_nonsense_retry_after_does_not_crash(self):
        from aeropub.transport import _retry_after

        assert _retry_after(headers_of(Retry_After="soon")) >= timedelta(minutes=1)


class TestFailureIsNeverAnEmptyDocument:

    def test_a_404_raises_rather_than_returning_nothing(self, opened):
        """An unreachable page must not be indistinguishable from an empty one."""
        _, queue = opened
        queue.append(
            urllib.error.HTTPError(
                "https://aim.gov.qa/a", 404, "Not Found", headers_of(), BytesIO(b"")
            )
        )
        with pytest.raises(TransportError, match="404"):
            LiveTransport()("https://aim.gov.qa/a.html")

    def test_a_network_failure_raises(self, opened):
        _, queue = opened
        queue.append(urllib.error.URLError("connection refused"))
        with pytest.raises(TransportError, match="refused"):
            LiveTransport()("https://aim.gov.qa/a.html")

    def test_an_unexpected_fault_does_not_leak_out_raw(self, opened):
        """The cycle guards on TransportError. A raw fault reaching it as some
        other type still gets recorded, but loses the URL that names it."""
        _, queue = opened
        queue.append(TimeoutError("timed out"))
        with pytest.raises(TransportError, match="aim.gov.qa"):
            LiveTransport()("https://aim.gov.qa/a.html")


class TestWhatCameBack:

    def test_the_servers_content_type_is_used(self, opened):
        _, queue = opened
        queue.append(FakeResponse(b"<html>x</html>", Content_Type="text/html; charset=UTF-8"))
        got = LiveTransport()("https://aim.gov.qa/a.html")
        assert got.media_type == "text/html"
        assert got.type_was_declared

    def test_bytes_beat_a_wrong_content_type(self, opened):
        """A State serving a PDF as text/html is common enough."""
        _, queue = opened
        queue.append(FakeResponse(b"%PDF-1.7 x", Content_Type="text/html"))
        assert LiveTransport()("https://aim.gov.qa/sup").media_type == "application/pdf"

    def test_the_body_is_hashed(self, opened):
        import hashlib

        _, queue = opened
        body = b"<html>ENR 3.2</html>"
        queue.append(FakeResponse(body, Content_Type="text/html"))
        got = LiveTransport()("https://aim.gov.qa/a.html")
        assert got.content_hash == hashlib.sha256(body).hexdigest()

    def test_we_identify_ourselves(self, opened):
        """An AIM administrator who wants to ask why we are fetching should
        be able to work out who we are."""
        seen, queue = opened
        queue.append(FakeResponse(b"a", Content_Type="text/html"))
        LiveTransport()("https://aim.gov.qa/a.html")
        assert "AeroPub" in seen[0].headers["User-agent"]

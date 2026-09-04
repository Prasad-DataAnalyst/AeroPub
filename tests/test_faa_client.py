"""The FAA NMS-API client.

Transport is replayed rather than live — the FAA's own documented request and
response shapes, and for the initial load, the FAA's own handover example with
its signature removed (see ``tests/fixtures/faa/nms-initial-load-handover.json``).
The AIXM payload replayed through it is the real sample the FAA issued.

Most of these tests are about the three things the curl examples do not tell
you: that the redirect must not be followed automatically, that two responses
are credentials and must stay out of the archive, and that the bundle may
arrive already decompressed.
"""

from __future__ import annotations

import gzip
import io
import json
import urllib.error
from datetime import datetime, timedelta, timezone
from email.message import Message
from pathlib import Path

import pytest

from aeropub.archive import Archive
from aeropub.faa.auth import AccessToken, TokenClient
from aeropub.faa.client import (
    MAX_THROTTLE_WAIT,
    NmsClient,
    _NoRedirect,
    parse_signed_url,
)
from aeropub.faa.config import ENVIRONMENTS
from aeropub.faa.errors import (
    NmsAuthError,
    NmsConfigurationError,
    NmsError,
    NmsProtocolError,
    NmsTransportError,
    NmsUnavailableError,
)
from aeropub.http import HostThrottle

FIXTURES = Path(__file__).parent / "fixtures" / "faa"
AIXM = (FIXTURES / "nms-initial-load-sample.raw").read_bytes()
HANDOVER = json.loads((FIXTURES / "nms-initial-load-handover.raw").read_text())
SIGNED_URL = HANDOVER["data"]["url"]

NOW = datetime(2025, 9, 12, 17, 25, 30, tzinfo=timezone.utc)


class _Response(io.BytesIO):
    def __init__(self, body, status, headers=None):
        super().__init__(body)
        self.status = status
        self.headers = Message()
        for key, value in (headers or {}).items():
            self.headers[key] = value

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


class Router:
    """Replays a response per URL fragment and records every request sent."""

    def __init__(self, routes):
        self.routes = routes
        self.requests = []

    def __call__(self, request, timeout=None):
        self.requests.append(request)
        for fragment, outcome in self.routes.items():
            if fragment in request.full_url:
                if isinstance(outcome, Exception):
                    raise outcome
                body, status, headers = outcome
                return _Response(body, status, headers)
        raise AssertionError(f"no route for {request.full_url}")

    def sent_to(self, fragment):
        return [r for r in self.requests if fragment in r.full_url]


def _http_error(code, url="https://api-nms.aim.faa.gov/x", body=b"", headers=None):
    message = Message()
    for key, value in (headers or {}).items():
        message[key] = value
    return urllib.error.HTTPError(url, code, "err", message, io.BytesIO(body))


#: A minimal token payload in the gateway's documented shape. The value is the
#: FAA's own placeholder, so nothing token-shaped enters this repository.
TOKEN_PAYLOAD = {"access_token": "BEARER TOKEN HERE", "expires_in": "1799", "status": "approved"}


def _tokens(clock=None):
    """A token client that can genuinely re-authorise, as production can."""

    def token_endpoint(request, timeout=None):
        return _Response(json.dumps(TOKEN_PAYLOAD).encode(), 200)

    tokens = TokenClient(
        ENVIRONMENTS["prod"],
        environ={"FAA_NMS_CLIENT_ID": "key", "FAA_NMS_CLIENT_SECRET": "secret"},
        opener=token_endpoint,
        clock=clock or (lambda: NOW),
    )
    tokens._token = AccessToken(
        _value="test-bearer", expires_at=NOW + timedelta(minutes=25), obtained_at=NOW
    )
    return tokens


def _client(router, *, archive=None, clock=None):
    tokens = _tokens(clock)
    return NmsClient(
        ENVIRONMENTS["prod"],
        tokens=tokens,
        archive=archive,
        throttle=HostThrottle(gap=timedelta(0)),
        opener=router,
        clock=clock or (lambda: NOW),
    )


class TestRequestConstruction:
    def test_carries_the_bearer_token(self):
        router = Router({"/v1/ping": (b"pong", 200, {})})
        _client(router).ping()
        assert router.requests[0].get_header("Authorization") == "Bearer test-bearer"

    def test_carries_the_header_the_notam_endpoint_requires(self):
        router = Router({"/v1/notams": (AIXM, 200, {})})
        _client(router).notams(location="KDFW")
        # get_header title-cases the name it was given.
        assert router.requests[0].get_header("Nmsresponseformat") == "AIXM"

    def test_builds_the_documented_query(self):
        router = Router({"/v1/notams": (AIXM, 200, {})})
        _client(router).notams(location="kdfw", notam_number="10/108")
        url = router.requests[0].full_url
        assert "location=KDFW" in url
        assert "notamNumber=10%2F108" in url

    def test_formats_timestamps_the_way_the_api_expects(self):
        router = Router({"/v1/locationseries": (b"[]", 200, {})})
        _client(router).location_series(
            last_updated=datetime(2025, 7, 1, 10, 0, tzinfo=timezone.utc)
        )
        assert "lastUpdatedDate=2025-07-01T10%3A00%3A00Z" in router.requests[0].full_url

    def test_a_naive_timestamp_is_refused(self):
        router = Router({"/v1/locationseries": (b"[]", 200, {})})
        with pytest.raises(ValueError, match="timezone-aware"):
            _client(router).location_series(last_updated=datetime(2025, 7, 1, 10, 0))

    def test_unset_filters_are_omitted_entirely(self):
        router = Router({"/v1/notams/checklist": (b"[]", 200, {})})
        _client(router).notam_checklist(location="KDFW")
        url = router.requests[0].full_url
        assert "location=KDFW" in url
        assert "accountability" not in url and "classification" not in url


class TestFilterValidation:
    def test_mixed_filter_families_are_refused_before_the_call(self):
        # The server's own error does not say which filter it disliked.
        router = Router({})
        with pytest.raises(NmsConfigurationError, match="mutually exclusive"):
            _client(router).notams(nms_id="1234567812345678", location="KDFW")
        assert router.requests == []

    def test_an_unfiltered_query_points_at_the_initial_load(self):
        with pytest.raises(NmsConfigurationError, match="fetch_initial_load"):
            _client(Router({})).notams()

    def test_a_notam_number_without_a_location_is_refused(self):
        with pytest.raises(NmsConfigurationError, match="location as well"):
            _client(Router({})).notams(notam_number="10/108")

    def test_a_partial_circle_is_refused(self):
        with pytest.raises(NmsConfigurationError, match="not defined by two"):
            _client(Router({})).notams(latitude=32.897, longitude=-97.037)

    def test_a_complete_circle_is_accepted(self):
        router = Router({"/v1/notams": (AIXM, 200, {})})
        _client(router).notams(latitude=32.897, longitude=-97.037, radius=50)
        url = router.requests[0].full_url
        assert "latitude=32.897" in url and "longitude=-97.037" in url and "radius=50" in url

    def test_last_updated_narrows_a_primary_filter(self):
        # "What changed at this aerodrome since I last asked" is the query
        # incremental collection is built on; refusing it would be our
        # restriction, not the API's.
        router = Router({"/v1/notams": (AIXM, 200, {})})
        _client(router).notams(
            location="KDFW", last_updated=datetime(2025, 7, 1, tzinfo=timezone.utc)
        )
        url = router.requests[0].full_url
        assert "location=KDFW" in url and "lastUpdatedDate=" in url


class TestArchiving:
    def test_notam_responses_are_archived_before_they_are_parsed(self, tmp_path):
        archive = Archive(tmp_path / "raw")
        router = Router({"/v1/notams": (AIXM, 200, {"Content-Type": "application/xml"})})
        response = _client(router, archive=archive).notams(location="K8WC")

        assert response.archived is not None
        assert archive.get(response.archived.digest) == AIXM

    def test_a_liveness_check_is_not_archived(self, tmp_path):
        # An archive of pings is noise in a store meant to answer evidential
        # questions, and it is never cited.
        archive = Archive(tmp_path / "raw")
        _client(Router({"/v1/ping": (b"pong", 200, {})}), archive=archive).ping()
        assert len(archive) == 0

    def test_the_signed_url_handover_is_never_archived(self, tmp_path):
        # The archive has no delete. A signed URL written into it could not be
        # withdrawn, so this one response is excluded by construction.
        archive = Archive(tmp_path / "raw")
        router = Router(
            {
                "/v1/notams/il": (json.dumps(HANDOVER).encode(), 200, {}),
                "storage.googleapis.com": (gzip.compress(AIXM), 200, {}),
            }
        )
        _client(router, archive=archive).fetch_initial_load("DOMESTIC")

        assert len(archive) == 1
        stored = next(iter(archive.digests()))
        recorded = archive.metadata(stored)["url"]
        # The parameter name survives; the signature does not. The archive has
        # no delete, so this is the only chance to get it right.
        assert "X-Goog-Signature=%5Bredacted%5D" in recorded
        assert "X-Goog-Signature=REDACTED" not in recorded

    def test_the_bundle_is_archived_as_served(self, tmp_path):
        # Compressed, byte for byte: a citation should resolve to what the FAA
        # sent, not to our decompression of it.
        archive = Archive(tmp_path / "raw")
        compressed = gzip.compress(AIXM)
        router = Router(
            {
                "/v1/notams/il": (json.dumps(HANDOVER).encode(), 200, {}),
                "storage.googleapis.com": (compressed, 200, {}),
            }
        )
        load = _client(router, archive=archive).fetch_initial_load()
        assert archive.get(load.entry.digest) == compressed

    def test_the_source_id_names_the_environment(self, tmp_path):
        # A NOTAM read from staging is not evidence about the real world.
        archive = Archive(tmp_path / "raw")
        client = _client(Router({"/v1/notams": (AIXM, 200, {})}), archive=archive)
        assert client.source_id == "FAA-NMS-PROD"
        response = client.notams(location="K8WC")
        assert response.archived.source_id == "FAA-NMS-PROD"


class TestInitialLoadHandover:
    def test_a_redirect_is_not_followed_automatically(self):
        # urllib's redirect handler carries Authorization across hosts on the
        # versions this project supports, and GCS refuses a request presenting
        # both its own signature and an Authorization header.
        assert _NoRedirect().redirect_request(None, None, 302, "", {}, SIGNED_URL) is None

    def test_the_redirect_form_of_the_handover_is_read(self):
        router = Router(
            {"/v1/notams/il": _http_error(302, headers={"Location": SIGNED_URL})}
        )
        signed = _client(router).initial_load_handover()
        assert signed.url == SIGNED_URL

    def test_the_json_form_of_the_handover_is_read(self):
        # The FAA documents the redirect and ships the JSON example. Handling
        # only the one documented last would break on a non-breaking change.
        router = Router({"/v1/notams/il": (json.dumps(HANDOVER).encode(), 200, {})})
        assert _client(router).initial_load_handover().url == SIGNED_URL

    def test_a_redirect_without_a_location_says_so(self):
        router = Router({"/v1/notams/il": _http_error(302)})
        with pytest.raises(NmsProtocolError, match="without a Location"):
            _client(router).initial_load_handover()

    def test_a_failed_handover_never_echoes_the_payload(self):
        # If a URL is in there in a shape we did not expect, printing the
        # response discloses the signature we are refusing to log.
        body = json.dumps({"status": "Success", "result": {"link": SIGNED_URL}}).encode()
        router = Router({"/v1/notams/il": (body, 200, {})})
        with pytest.raises(NmsProtocolError) as caught:
            _client(router).initial_load_handover()
        assert "X-Goog-Credential" not in str(caught.value)
        assert "shape has changed" in str(caught.value)

    def test_a_non_success_status_is_refused(self):
        body = json.dumps({"status": "Failed", "data": {}}).encode()
        router = Router({"/v1/notams/il": (body, 200, {})})
        with pytest.raises(NmsProtocolError, match="rather than Success"):
            _client(router).initial_load_handover()

    def test_the_classification_reaches_the_path(self):
        router = Router({"/v1/notams/il": (json.dumps(HANDOVER).encode(), 200, {})})
        _client(router).initial_load_handover("domestic")
        assert router.requests[0].full_url.endswith("/v1/notams/il/DOMESTIC")


class TestSignedUrl:
    def test_reads_the_validity_window_from_the_query_string(self):
        signed = parse_signed_url(SIGNED_URL)
        assert signed.issued_at == datetime(2025, 9, 12, 17, 25, 4, tzinfo=timezone.utc)
        assert signed.expires_at == signed.issued_at + timedelta(seconds=300)

    def test_knows_when_it_has_expired(self):
        signed = parse_signed_url(SIGNED_URL)
        assert not signed.is_expired(datetime(2025, 9, 12, 17, 27, tzinfo=timezone.utc))
        assert signed.is_expired(datetime(2025, 9, 12, 17, 35, tzinfo=timezone.utc))

    def test_an_unparseable_window_is_unknown_not_expired(self):
        signed = parse_signed_url("https://storage.googleapis.com/bundle.gz")
        assert signed.expires_at is None
        assert signed.seconds_remaining() is None
        assert not signed.is_expired()

    def test_the_masked_form_drops_the_signature_and_keeps_the_rest(self):
        masked = parse_signed_url(SIGNED_URL).masked
        assert "X-Goog-Signature=%5Bredacted%5D" in masked
        assert "REDACTED" not in masked.replace("%5Bredacted%5D", "")
        assert "INITIAL_LOAD/DOM/initial_load_aixm_20250912T172401Z.gz" in masked

    def test_an_expired_url_fails_before_the_request_with_a_useful_message(self):
        # GCS answers an expired signature with a 403 and an XML body that says
        # nothing about time. The bug report reads "the FAA is refusing us".
        router = Router({"storage.googleapis.com": (b"", 200, {})})
        client = _client(router, clock=lambda: datetime(2025, 9, 12, 18, 0, tzinfo=timezone.utc))
        with pytest.raises(NmsError, match="expired before it could be used"):
            client.download_signed(parse_signed_url(SIGNED_URL))
        assert router.requests == []


class TestSignedDownload:
    def test_the_download_is_unauthenticated(self, tmp_path):
        # A GCS V4 signed URL signs the host header and nothing else. Adding
        # Authorization makes GCS reject the request for presenting two
        # credentials at once.
        router = Router(
            {
                "/v1/notams/il": (json.dumps(HANDOVER).encode(), 200, {}),
                "storage.googleapis.com": (gzip.compress(AIXM), 200, {}),
            }
        )
        _client(router, archive=Archive(tmp_path / "raw")).fetch_initial_load()

        download = router.sent_to("storage.googleapis.com")[0]
        assert download.get_header("Authorization") is None
        assert download.get_header("User-agent")

    def test_a_storage_refusal_reports_the_masked_url(self, tmp_path):
        router = Router(
            {
                "/v1/notams/il": (json.dumps(HANDOVER).encode(), 200, {}),
                "storage.googleapis.com": _http_error(403, url=SIGNED_URL),
            }
        )
        with pytest.raises(NmsError) as caught:
            _client(router, archive=Archive(tmp_path / "raw")).fetch_initial_load()
        assert "X-Goog-Signature=REDACTED" not in str(caught.value)
        assert "initial-load download was refused" in str(caught.value)


class TestDecompression:
    def test_a_gzipped_bundle_is_decompressed(self, tmp_path):
        router = Router(
            {
                "/v1/notams/il": (json.dumps(HANDOVER).encode(), 200, {}),
                "storage.googleapis.com": (gzip.compress(AIXM), 200, {}),
            }
        )
        load = _client(router, archive=Archive(tmp_path / "raw")).fetch_initial_load()
        assert load.compressed
        with load.open() as stream:
            assert stream.read() == AIXM

    def test_a_bundle_gcs_already_decompressed_is_read_as_is(self, tmp_path):
        # Decompressive transcoding is a GCS feature, not a hypothetical: an
        # object stored with Content-Encoding gzip arrives decompressed. A
        # client that assumes gzip fails with a BadGzipFile on the whole feed.
        router = Router(
            {
                "/v1/notams/il": (json.dumps(HANDOVER).encode(), 200, {}),
                "storage.googleapis.com": (AIXM, 200, {"Content-Encoding": "gzip"}),
            }
        )
        load = _client(router, archive=Archive(tmp_path / "raw")).fetch_initial_load()
        assert not load.compressed
        with load.open() as stream:
            assert stream.read() == AIXM

    def test_the_bundle_parses_end_to_end(self, tmp_path):
        from aeropub.faa.aixm import NotamFeed

        router = Router(
            {
                "/v1/notams/il": (json.dumps(HANDOVER).encode(), 200, {}),
                "storage.googleapis.com": (gzip.compress(AIXM), 200, {}),
            }
        )
        load = _client(router, archive=Archive(tmp_path / "raw")).fetch_initial_load("DOMESTIC")
        with load.open() as stream:
            notams = list(NotamFeed(stream))
        assert [n.identifier for n in notams] == ["STL 08/430", "BDR 04/221"]

        # And the citation resolves back to the archived bytes.
        ref = notams[0].source_ref(load.entry)
        assert ref.content_hash == load.entry.digest

    def test_an_empty_bundle_is_refused(self, tmp_path):
        router = Router(
            {
                "/v1/notams/il": (json.dumps(HANDOVER).encode(), 200, {}),
                "storage.googleapis.com": (b"", 200, {}),
            }
        )
        with pytest.raises(NmsProtocolError, match="empty"):
            _client(router, archive=Archive(tmp_path / "raw")).fetch_initial_load()

    def test_the_bundle_needs_an_archive_and_says_so_before_downloading(self):
        # Discovering there is nowhere to put the bundle after pulling tens of
        # megabytes through a five-minute signed URL wastes both.
        router = Router({"/v1/notams/il": (json.dumps(HANDOVER).encode(), 200, {})})
        with pytest.raises(NmsConfigurationError, match="cannot be cited"):
            _client(router).fetch_initial_load()
        assert router.requests == []


class TestFailureModes:
    def test_a_401_is_retried_once_with_a_fresh_token(self):
        # A token can be revoked between our expiry check and the gateway
        # reading it. One retry; not a loop.
        calls = {"n": 0}

        def opener(request, timeout=None):
            calls["n"] += 1
            if calls["n"] == 1:
                raise _http_error(401)
            return _Response(b"pong", 200)

        tokens = TokenClient(ENVIRONMENTS["prod"], environ={})
        tokens._token = AccessToken(
            _value="stale", expires_at=NOW + timedelta(minutes=25), obtained_at=NOW
        )
        replacements = {"n": 0}

        def reissue(force=False):
            replacements["n"] += 1
            return AccessToken(
                _value=f"fresh-{replacements['n']}",
                expires_at=NOW + timedelta(minutes=25),
                obtained_at=NOW,
            )

        tokens.token = reissue
        client = NmsClient(
            ENVIRONMENTS["prod"],
            tokens=tokens,
            throttle=HostThrottle(gap=timedelta(0)),
            opener=opener,
            clock=lambda: NOW,
        )
        assert client.ping().status == 200
        assert calls["n"] == 2

    def test_a_persistent_401_raises_and_marks_the_credentials_rejected(self):
        router = Router({"/v1/ping": _http_error(401)})
        client = _client(router)
        with pytest.raises(NmsAuthError):
            client.ping()
        assert client.tokens.rejected

    def test_a_404_points_at_the_overlay_rather_than_the_code(self):
        router = Router({"/v1/ping": _http_error(404)})
        with pytest.raises(NmsConfigurationError, match="AEROPUB_FAA_NMS_CONFIG"):
            _client(router).ping()

    @pytest.mark.parametrize("status", [429, 500, 502, 503])
    def test_server_trouble_is_retryable_and_carries_retry_after(self, status):
        router = Router({"/v1/ping": _http_error(status, headers={"Retry-After": "45"})})
        with pytest.raises(NmsUnavailableError) as caught:
            _client(router).ping()
        assert caught.value.is_retryable
        assert caught.value.retry_after == 45

    def test_an_unreachable_host_is_a_transport_error(self):
        router = Router({"/v1/ping": urllib.error.URLError("no route to host")})
        with pytest.raises(NmsTransportError, match="could not reach"):
            _client(router).ping()

    def test_a_waiting_caller_holds_instead_of_reporting_the_faa_unavailable(self):
        # A one-shot diagnostic makes four sequential calls to one host. If it
        # raised on its own politeness gap it would report the FAA unavailable
        # for a delay entirely of our own making — a false alarm about somebody
        # else's service, which is worse than the delay it avoids.
        router = Router({"/v1/ping": (b"pong", 200, {})})
        moment = [NOW]
        slept = []

        client = NmsClient(
            ENVIRONMENTS["prod"],
            tokens=_tokens(lambda: moment[0]),
            throttle=HostThrottle(gap=timedelta(seconds=2)),
            opener=router,
            clock=lambda: moment[0],
            wait_for_throttle=True,
            sleep=lambda s: (slept.append(s), moment.__setitem__(0, moment[0] + timedelta(seconds=s))),
        )
        client.ping()
        client.ping()

        assert slept == [2.0]
        assert len(router.requests) == 2

    def test_a_waiting_caller_still_refuses_an_unreasonable_hold(self):
        # Past the ceiling it raises. A check that pauses for six hours is not
        # waiting, it is hanging, and the operator cannot tell the difference.
        router = Router({"/v1/ping": (b"pong", 200, {})})
        throttle = HostThrottle(gap=timedelta(0))
        throttle.back_off(
            ENVIRONMENTS["prod"].url("ping"), MAX_THROTTLE_WAIT + timedelta(seconds=1), at=NOW
        )
        client = NmsClient(
            ENVIRONMENTS["prod"],
            tokens=_tokens(),
            throttle=throttle,
            opener=router,
            clock=lambda: NOW,
            wait_for_throttle=True,
            sleep=lambda s: pytest.fail("should not have slept"),
        )
        with pytest.raises(NmsUnavailableError, match="holding off"):
            client.ping()

    def test_the_throttle_holds_a_host_off_rather_than_hammering_it(self):
        # A State or an authority that blocks our address becomes a silent
        # coverage gap, which is the worst failure this system has.
        router = Router({"/v1/ping": (b"pong", 200, {})})
        client = NmsClient(
            ENVIRONMENTS["prod"],
            tokens=_client(router).tokens,
            throttle=HostThrottle(gap=timedelta(seconds=2)),
            opener=router,
            clock=lambda: NOW,
        )
        client.ping()
        with pytest.raises(NmsUnavailableError, match="holding off"):
            client.ping()
        assert len(router.requests) == 1


class TestTheHandoverDecidesWhereTheBearerTravels:
    """The FAA changed this, and the two answers are opposite.

    It used to hand back a Google Cloud Storage V4 signed URL, where the
    correct behaviour is to send no Authorization header at all. It now hands
    back /nmsapi/v1/content/{token} on its own host, which requires the same
    bearer as every other call. Both shapes remain possible, so the decision is
    read off the URL rather than set in configuration — a setting is one more
    thing to get wrong on the day they change it back.
    """

    HOST = "https://api-staging.cgifederal-aim.com"

    def needs(self, url: str) -> bool:
        from aeropub.faa.client import handover_needs_bearer

        return handover_needs_bearer(url, host=self.HOST)

    def test_the_relative_content_endpoint_needs_it(self):
        assert self.needs("/nmsapi/v1/content/eyJhbGciOi")

    def test_the_absolute_content_endpoint_on_our_host_needs_it(self):
        assert self.needs(f"{self.HOST}/nmsapi/v1/content/eyJhbGciOi")

    def test_a_google_signed_url_must_not_receive_it(self):
        # GCS signs the host header and nothing else; a bearer alongside the
        # signature is two credentials at once and is rejected.
        assert not self.needs(
            "https://storage.googleapis.com/bucket/f.gz?X-Goog-Signature=abc"
        )

    def test_a_signature_outranks_the_hostname(self):
        # Signed storage can sit behind a custom domain — including, as the
        # conformance harness does, the same host as the API. The signature is
        # the stronger signal, and this ordering was found by that harness.
        assert not self.needs(f"{self.HOST}/storage/f.gz?X-Goog-Signature=abc")

    def test_the_token_never_travels_off_host(self):
        # An unrecognised off-host handover is far more likely to be a redirect
        # we should not have followed than a place to present credentials.
        assert not self.needs("https://elsewhere.example/file.gz")

    def test_an_empty_handover_needs_nothing(self):
        assert not self.needs("   ")

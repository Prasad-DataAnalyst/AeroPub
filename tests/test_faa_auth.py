"""OAuth2 against the FAA gateway.

The token payload replayed here is the FAA's own published example from the
onboarding pack, values and all — including the placeholders the FAA itself
wrote in place of real secrets (``"BEARER TOKEN HERE"``, ``"KEY provided unique
to entity"``). That is deliberate on both counts: the *shape* is the
authority's, so the parser is tested against what the gateway actually sends,
and the *values* are the authority's own redactions, so nothing credential-like
enters this repository.
"""

from __future__ import annotations

import io
import json
import urllib.error
from datetime import datetime, timedelta, timezone
from email.message import Message

import pytest

from aeropub.faa.auth import CONSERVATIVE_TTL, REFRESH_MARGIN, AccessToken, TokenClient
from aeropub.faa.config import ENVIRONMENTS, ClientCredentials
from aeropub.faa.errors import (
    NmsAuthError,
    NmsConfigurationError,
    NmsProtocolError,
    NmsTransportError,
    NmsUnavailableError,
    redact,
)

#: Verbatim from the FAA onboarding pack, "Step 2 — Bearer token is returned".
FAA_TOKEN_RESPONSE = {
    "refresh_token_expires_in": "0",
    "api_product_list": "[FAA Staging Preprod APIs]",
    "api_product_list_json": ["FAA Staging Preprod APIs"],
    "organization_name": "faa-XXXX",
    "developer.email": "whoever@email.com",
    "token_type": "BearerToken values",
    "issued_at": "1752851243984",
    "client_id": "KEY provided unique to entity",
    "access_token": "BEARER TOKEN HERE",
    "application_name": "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
    "scope": "",
    "expires_in": "1799",
    "refresh_count": "0",
    "status": "approved",
}

CREDENTIALS = {"FAA_NMS_CLIENT_ID": "key-value", "FAA_NMS_CLIENT_SECRET": "secret-value"}


class Replay:
    """Replays a canned response and records what was sent."""

    def __init__(self, body, status=200, error=None):
        self.body = json.dumps(body).encode() if isinstance(body, dict) else body
        self.status = status
        self.error = error
        self.requests = []

    def __call__(self, request, timeout=None):
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return _Response(self.body, self.status)


class _Response(io.BytesIO):
    def __init__(self, body, status):
        super().__init__(body)
        self.status = status
        self.headers = Message()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def _http_error(code, body=b"", headers=None):
    message = Message()
    for key, value in (headers or {}).items():
        message[key] = value
    return urllib.error.HTTPError(
        "https://api-nms.aim.faa.gov/v1/auth/token", code, "err", message, io.BytesIO(body)
    )


def _client(replay, environ=None, clock=None):
    return TokenClient(
        ENVIRONMENTS["staging"],
        ClientCredentials.default(),
        environ=environ if environ is not None else CREDENTIALS,
        opener=replay,
        clock=clock or (lambda: datetime(2025, 9, 12, 12, 0, tzinfo=timezone.utc)),
    )


class TestTheRequest:
    def test_posts_client_credentials_with_http_basic(self):
        # Exactly the FAA's curl: -X POST -d grant_type=client_credentials -u KEY:SECRET
        replay = Replay(FAA_TOKEN_RESPONSE)
        _client(replay).token()

        request = replay.requests[0]
        assert request.get_method() == "POST"
        assert request.full_url == "https://api-staging.cgifederal-aim.com/v1/auth/token"
        assert request.data == b"grant_type=client_credentials"

        import base64

        header = request.get_header("Authorization")
        assert header.startswith("Basic ")
        decoded = base64.b64decode(header.split(" ", 1)[1]).decode()
        assert decoded == "key-value:secret-value"

    def test_missing_credentials_say_which_and_where_they_come_from(self):
        with pytest.raises(NmsConfigurationError) as caught:
            _client(Replay(FAA_TOKEN_RESPONSE), environ={}).token()
        message = str(caught.value)
        assert "FAA_NMS_CLIENT_ID" in message and "FAA_NMS_CLIENT_SECRET" in message
        assert "spreadsheet" in message


class TestTheResponse:
    def test_reads_the_faa_payload(self):
        token = _client(Replay(FAA_TOKEN_RESPONSE)).token()
        assert token.response.organization == "faa-XXXX"
        assert token.response.api_products == ("FAA Staging Preprod APIs",)
        assert token.response.status == "approved"
        assert token.response.expires_in == 1799

    def test_numbers_arrive_as_strings_and_are_coerced(self):
        # The gateway sends "expires_in": "1799", not 1799.
        token = _client(Replay(FAA_TOKEN_RESPONSE)).token()
        assert isinstance(token.response.expires_in, int)
        assert token.response.issued_at == datetime.fromtimestamp(
            1752851243984 / 1000, tz=timezone.utc
        )

    def test_numbers_that_arrive_as_numbers_are_accepted_too(self):
        payload = dict(FAA_TOKEN_RESPONSE, expires_in=1799, issued_at=1752851243984)
        assert _client(Replay(payload)).token().response.expires_in == 1799

    def test_the_product_list_falls_back_to_the_bracketed_string(self):
        payload = {k: v for k, v in FAA_TOKEN_RESPONSE.items() if k != "api_product_list_json"}
        token = _client(Replay(payload)).token()
        assert token.response.api_products == ("FAA Staging Preprod APIs",)

    def test_an_empty_token_is_refused(self):
        with pytest.raises(NmsProtocolError):
            _client(Replay(dict(FAA_TOKEN_RESPONSE, access_token=""))).token()

    def test_a_missing_token_does_not_echo_token_bearing_keys(self):
        payload = {k: v for k, v in FAA_TOKEN_RESPONSE.items() if k != "access_token"}
        with pytest.raises(NmsProtocolError) as caught:
            _client(Replay(payload)).token()
        assert "refresh_token_expires_in" not in str(caught.value)

    def test_a_non_json_response_says_so(self):
        with pytest.raises(NmsProtocolError, match="not JSON"):
            _client(Replay(b"<html>gateway error</html>")).token()

    def test_an_unapproved_application_fails_here_not_later(self):
        # Some gateways issue a token to an unapproved application and then
        # refuse every call with it, which is a much harder fault to read.
        with pytest.raises(NmsAuthError, match="not approved"):
            _client(Replay(dict(FAA_TOKEN_RESPONSE, status="pending"))).token()


class TestExpiry:
    def test_expiry_is_measured_from_our_clock_not_the_gateways(self):
        # issued_at in the FAA's example is July 2025; our clock says September.
        # Trusting theirs would hold a token we believe live for two months.
        now = datetime(2025, 9, 12, 12, 0, tzinfo=timezone.utc)
        token = _client(Replay(FAA_TOKEN_RESPONSE), clock=lambda: now).token()
        assert token.expires_at == now + timedelta(seconds=1799)
        assert token.obtained_at == now

    def test_a_missing_lifetime_is_short_not_assumed(self):
        payload = {k: v for k, v in FAA_TOKEN_RESPONSE.items() if k != "expires_in"}
        now = datetime(2025, 9, 12, 12, 0, tzinfo=timezone.utc)
        token = _client(Replay(payload), clock=lambda: now).token()
        assert token.expires_at == now + CONSERVATIVE_TTL

    def test_a_token_is_replaced_before_it_expires_not_after(self):
        now = datetime(2025, 9, 12, 12, 0, tzinfo=timezone.utc)
        token = AccessToken(
            _value="x", expires_at=now + REFRESH_MARGIN - timedelta(seconds=1), obtained_at=now
        )
        assert not token.is_usable(now)
        assert AccessToken(
            _value="x", expires_at=now + REFRESH_MARGIN + timedelta(seconds=1), obtained_at=now
        ).is_usable(now)


class TestCaching:
    def test_a_live_token_is_reused(self):
        replay = Replay(FAA_TOKEN_RESPONSE)
        client = _client(replay)
        first, second = client.token(), client.token()
        assert first is second
        assert len(replay.requests) == 1

    def test_an_expired_token_is_replaced(self):
        replay = Replay(FAA_TOKEN_RESPONSE)
        moment = [datetime(2025, 9, 12, 12, 0, tzinfo=timezone.utc)]
        client = _client(replay, clock=lambda: moment[0])
        client.token()
        moment[0] += timedelta(seconds=1799)
        client.token()
        assert len(replay.requests) == 2

    def test_forget_forces_reauthorisation(self):
        replay = Replay(FAA_TOKEN_RESPONSE)
        client = _client(replay)
        client.token()
        client.forget()
        assert client.current() is None
        client.token()
        assert len(replay.requests) == 2


class TestFailureModes:
    @pytest.mark.parametrize("status", [400, 401, 403])
    def test_a_rejected_credential_is_not_retryable(self, status):
        # 400 belongs here: an OAuth2 gateway answers a bad grant with
        # invalid_client and a 400, not a 401.
        replay = Replay(b"", error=_http_error(status, b'{"error":"invalid_client"}'))
        with pytest.raises(NmsAuthError) as caught:
            _client(replay).token()
        assert not caught.value.is_retryable
        assert "environment" in str(caught.value)

    @pytest.mark.parametrize("status", [429, 500, 503])
    def test_an_unavailable_gateway_is_retryable(self, status):
        replay = Replay(b"", error=_http_error(status, b"", {"Retry-After": "30"}))
        with pytest.raises(NmsUnavailableError) as caught:
            _client(replay).token()
        assert caught.value.is_retryable
        assert caught.value.retry_after == 30

    def test_an_unreachable_host_is_a_transport_error(self):
        replay = Replay(b"", error=urllib.error.URLError("Name or service not known"))
        with pytest.raises(NmsTransportError, match="could not reach"):
            _client(replay).token()

    def test_a_timeout_says_how_long_it_waited(self):
        replay = Replay(b"", error=TimeoutError())
        with pytest.raises(NmsTransportError, match="timed out after 30s"):
            _client(replay).token()


class TestDisclosure:
    def test_the_token_is_not_in_its_own_repr(self):
        token = _client(Replay(FAA_TOKEN_RESPONSE)).token()
        for rendering in (repr(token), str(token), f"{token}"):
            assert "BEARER TOKEN HERE" not in rendering
            assert "****" in rendering

    def test_the_token_is_not_in_the_response_record(self):
        # TokenResponse is what the status board and the JSON API render.
        token = _client(Replay(FAA_TOKEN_RESPONSE)).token()
        assert "BEARER TOKEN HERE" not in repr(token.response)
        assert not hasattr(token.response, "access_token")

    def test_the_only_way_to_the_value_is_the_authorization_header(self):
        token = _client(Replay(FAA_TOKEN_RESPONSE)).token()
        assert token.header() == {"Authorization": "Bearer BEARER TOKEN HERE"}

    def test_the_client_copies_no_secret_into_state_of_its_own(self):
        # The client holds a reference to the environment it was handed — the
        # same relationship os.environ has to the secret, and the mechanism by
        # which the value is read at the moment of authentication. What it must
        # never do is copy the secret out into an attribute, where it would
        # outlive the call and reach a repr, a log or a pickle.
        client = _client(Replay(FAA_TOKEN_RESPONSE))
        client.token()

        own = {k: v for k, v in vars(client).items() if k != "_environ"}
        assert "secret-value" not in repr(own)
        assert "key-value" not in repr(own)
        assert client._environ is CREDENTIALS

    def test_the_default_client_holds_no_environment_at_all(self):
        # The production path passes nothing, so os.environ is read directly
        # and there is no mapping on the instance to leak.
        client = TokenClient(ENVIRONMENTS["staging"], opener=Replay(FAA_TOKEN_RESPONSE))
        assert client._environ is None

    def test_the_default_repr_does_not_dump_state(self):
        client = _client(Replay(FAA_TOKEN_RESPONSE))
        client.token()
        assert repr(client).startswith("<aeropub.faa.auth.TokenClient object")

    def test_an_error_body_echoing_a_credential_is_redacted(self):
        # Defence in depth: a gateway that reflects the Authorization header
        # into its error body would otherwise put it in a log kept forever.
        leaky = b'{"error":"bad","sent":"Basic a2V5LXZhbHVlOnNlY3JldA==","access_token":"abc123xyz"}'
        replay = Replay(b"", error=_http_error(401, leaky))
        with pytest.raises(NmsAuthError) as caught:
            _client(replay).token()
        message = str(caught.value)
        assert "a2V5LXZhbHVlOnNlY3JldA==" not in message
        assert "abc123xyz" not in message
        assert "[redacted]" in message


class TestRedact:
    @pytest.mark.parametrize(
        "text,gone",
        [
            ("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9", "eyJhbGciOiJIUzI1NiJ9"),
            ("Authorization: Basic dXNlcjpwYXNzd29yZA==", "dXNlcjpwYXNzd29yZA=="),
            ('{"client_secret": "s3cr3t-value-here"}', "s3cr3t-value-here"),
            ("https://x/y?X-Goog-Signature=deadbeefcafe&z=1", "deadbeefcafe"),
        ],
    )
    def test_credential_shapes_are_stripped(self, text, gone):
        assert gone not in redact(text)

    def test_ordinary_text_survives(self):
        assert redact("HTTP 503 from the FAA gateway") == "HTTP 503 from the FAA gateway"
        assert redact("") == ""

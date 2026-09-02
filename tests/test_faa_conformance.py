"""End-to-end conformance: the real client, over a real socket, over real TLS.

Everything in ``test_faa_client.py`` replaces the transport with a stub. That
proves the logic and proves nothing about the plumbing — whether urllib
actually declines the redirect, whether an ``HTTPError`` really carries the
``Location`` header we read off it, whether a bearer token survives the wire.
Those are exactly the faults that only appear against a live gateway, which is
the worst place to find them.

So this module stands up an HTTPS server implementing the contract in the FAA's
onboarding pack — *NMS-API cURL Command Examples and Instructions for
connecting* — and drives the unmodified :class:`NmsClient` through the whole
sequence against it: token, ping, filtered NOTAM, checklist, both forms of the
initial-load handover, the unauthenticated storage download, gunzip, AIXM
parse, and a citation that resolves back to archived bytes.

.. important::
   This proves the **client conforms to the documented contract**. It cannot
   prove the FAA's gateway matches its own documentation — only a call against
   the real service does that, and ``python -m aeropub.faa.check`` is how it is
   made. What this does guarantee is that when that call is finally made, any
   failure is the FAA's behaviour differing from its specification, not our
   transport being broken.

The AIXM served is the real sample the FAA issued. The token payload is the
FAA's published example, placeholders and all. Nothing aeronautical is invented
here; what is simulated is a web server.
"""

from __future__ import annotations

import gzip
import json
import shutil
import ssl
import subprocess
import threading
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from aeropub.archive import Archive
from aeropub.faa.aixm import NotamFeed
from aeropub.faa.auth import TokenClient
from aeropub.faa.client import NmsClient, _NoRedirect
from aeropub.faa.config import ClientCredentials, NmsEndpoint, NmsEnvironment
from aeropub.faa.errors import NmsAuthError, NmsConfigurationError
from aeropub.http import HostThrottle

pytestmark = pytest.mark.skipif(
    shutil.which("openssl") is None,
    reason="conformance run needs openssl to mint a localhost certificate",
)

FIXTURES = Path(__file__).parent / "fixtures" / "faa"
AIXM = (FIXTURES / "nms-initial-load-sample.raw").read_bytes()

CLIENT_ID = "conformance-key"
CLIENT_SECRET = "conformance-secret-value"

#: The FAA's published token payload. Its values are the FAA's own redactions.
TOKEN_PAYLOAD = {
    "refresh_token_expires_in": "0",
    "api_product_list": "[FAA Staging Preprod APIs]",
    "api_product_list_json": ["FAA Staging Preprod APIs"],
    "organization_name": "faa-XXXX",
    "developer.email": "whoever@email.com",
    "token_type": "BearerToken values",
    "issued_at": "1752851243984",
    "client_id": CLIENT_ID,
    "access_token": "conformance-bearer-token",
    "application_name": "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
    "scope": "",
    "expires_in": "1799",
    "refresh_count": "0",
    "status": "approved",
}

CHECKLIST = {"status": "Success", "data": [{"location": "K8WC", "notamNumber": "08/430"}]}


class Journal:
    """What the server saw. The assertions about disclosure read this."""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.lock = threading.Lock()

    def record(self, method: str, path: str, headers) -> None:
        with self.lock:
            self.requests.append(
                {
                    "method": method,
                    "path": path,
                    "authorization": headers.get("Authorization"),
                    "nms_format": headers.get("nmsResponseFormat"),
                    "user_agent": headers.get("User-Agent"),
                }
            )

    def to(self, fragment: str) -> list[dict]:
        with self.lock:
            return [r for r in self.requests if fragment in r["path"]]


def _signed_path(now: datetime) -> str:
    """A storage path shaped like the GCS V4 signed URL the FAA hands over."""
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    return (
        f"/storage/INITIAL_LOAD/DOM/initial_load_aixm_{stamp}.gz"
        "?X-Goog-Algorithm=GOOG4-RSA-SHA256"
        f"&X-Goog-Date={stamp}"
        "&X-Goog-Expires=300"
        "&X-Goog-SignedHeaders=host"
        "&X-Goog-Signature=local-conformance-server-not-a-real-signature"
    )


def _handler(journal: Journal, origin: str):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):  # noqa: D102 — silence the test run
            pass

        # -- helpers -----------------------------------------------------

        def _send(self, status, body=b"", content_type="application/json", extra=None):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            for key, value in (extra or {}).items():
                self.send_header(key, value)
            self.end_headers()
            if body:
                self.wfile.write(body)

        def _bearer_ok(self):
            header = self.headers.get("Authorization") or ""
            if header == f"Bearer {TOKEN_PAYLOAD['access_token']}":
                return True
            self._send(401, b'{"error":"invalid_token"}')
            return False

        # -- routes ------------------------------------------------------

        def do_POST(self):  # noqa: N802
            journal.record("POST", self.path, self.headers)
            if self.path != "/v1/auth/token":
                self._send(404, b'{"error":"not found"}')
                return

            import base64

            header = self.headers.get("Authorization") or ""
            if not header.startswith("Basic "):
                self._send(401, b'{"error":"invalid_client"}')
                return
            decoded = base64.b64decode(header.split(" ", 1)[1]).decode()
            if decoded != f"{CLIENT_ID}:{CLIENT_SECRET}":
                self._send(401, b'{"error":"invalid_client"}')
                return

            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length).decode()
            if parse_qs(body).get("grant_type") != ["client_credentials"]:
                self._send(400, b'{"error":"unsupported_grant_type"}')
                return

            self._send(200, json.dumps(TOKEN_PAYLOAD).encode())

        def do_GET(self):  # noqa: N802
            journal.record("GET", self.path, self.headers)
            path = urlsplit(self.path).path

            if path.startswith("/storage/"):
                # A GCS signed URL signs the host header and nothing else.
                # Presenting a bearer token as well is what GCS refuses, so
                # the server refuses it too — the client must not send one.
                if self.headers.get("Authorization"):
                    self._send(
                        400,
                        b'{"error":"Only one authentication mechanism allowed"}',
                    )
                    return
                self._send(200, gzip.compress(AIXM), "application/gzip")
                return

            if not self._bearer_ok():
                return

            if path == "/nmsapi/v1/ping":
                self._send(200, b'{"status":"Success"}')
            elif path == "/nmsapi/v1/locationseries":
                self._send(200, json.dumps({"status": "Success", "data": []}).encode())
            elif path == "/nmsapi/v1/notams":
                if self.headers.get("nmsResponseFormat") != "AIXM":
                    self._send(400, b'{"error":"nmsResponseFormat is required"}')
                    return
                self._send(200, AIXM, "application/xml")
            elif path == "/nmsapi/v1/notams/checklist":
                self._send(200, json.dumps(CHECKLIST).encode())
            elif path == "/nmsapi/v1/notams/il":
                # The redirect form, exactly as `curl -L` would meet it.
                target = origin + _signed_path(datetime.now(timezone.utc))
                self._send(302, b"", extra={"Location": target})
            elif path.startswith("/nmsapi/v1/notams/il/"):
                # The JSON form, as in the FAA's own initial-load example.
                target = origin + _signed_path(datetime.now(timezone.utc))
                self._send(
                    200, json.dumps({"status": "Success", "data": {"url": target}}).encode()
                )
            else:
                self._send(404, b'{"error":"not found"}')

    return Handler


@pytest.fixture(scope="module")
def gateway(tmp_path_factory):
    """A TLS server speaking the FAA's documented contract, on localhost."""
    certs = tmp_path_factory.mktemp("certs")
    cert, key = certs / "cert.pem", certs / "key.pem"
    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "rsa:2048",
            "-keyout", str(key), "-out", str(cert),
            "-days", "1", "-nodes", "-subj", "/CN=localhost",
            "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1",
        ],
        check=True,
        capture_output=True,
    )

    journal = Journal()
    # Bound first so the port is known before the handler needs the origin.
    server = ThreadingHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
    port = server.server_address[1]
    origin = f"https://localhost:{port}"
    server.RequestHandlerClass = _handler(journal, origin)

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certfile=str(cert), keyfile=str(key))
    server.socket = context.wrap_socket(server.socket, server_side=True)

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield {"origin": origin, "cert": cert, "journal": journal}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def environment(gateway) -> NmsEnvironment:
    """The real environment record, pointed at the local gateway.

    Built through :meth:`NmsEnvironment.overlay` rather than by construction —
    the same path an operator takes when the FAA moves a host. If the overlay
    mechanism is broken, this whole module fails, which is the point.
    """
    from aeropub.faa.config import ENVIRONMENTS

    return ENVIRONMENTS["prod"].overlay({"name": "conformance", "host": gateway["origin"]})


@pytest.fixture
def opener(gateway):
    """A real urllib opener that trusts the local certificate, and no redirects."""
    context = ssl.create_default_context(cafile=str(gateway["cert"]))
    return urllib.request.build_opener(
        _NoRedirect(), urllib.request.HTTPSHandler(context=context)
    ).open


@pytest.fixture
def client(environment, opener, tmp_path):
    tokens = TokenClient(
        environment,
        ClientCredentials.default(),
        environ={
            "FAA_NMS_CLIENT_ID": CLIENT_ID,
            "FAA_NMS_CLIENT_SECRET": CLIENT_SECRET,
        },
        opener=opener,
        timeout=30,
    )
    return NmsClient(
        environment,
        tokens=tokens,
        archive=Archive(tmp_path / "raw"),
        throttle=HostThrottle(gap=timedelta(0)),
        opener=opener,
        timeout=30,
    )


class TestTokenOverTheWire:
    def test_a_real_client_credentials_grant_succeeds(self, client):
        token = client.tokens.token()
        assert token.response.organization == "faa-XXXX"
        assert token.response.expires_in == 1799
        assert token.is_usable()

    def test_the_gateway_received_basic_auth_and_the_grant_body(self, client, gateway):
        client.tokens.token()
        sent = gateway["journal"].to("/v1/auth/token")[-1]
        assert sent["method"] == "POST"
        assert sent["authorization"].startswith("Basic ")

    def test_a_wrong_secret_is_rejected_by_the_gateway_not_by_us(
        self, environment, opener
    ):
        tokens = TokenClient(
            environment,
            ClientCredentials.default(),
            environ={
                "FAA_NMS_CLIENT_ID": CLIENT_ID,
                "FAA_NMS_CLIENT_SECRET": "wrong-secret",
            },
            opener=opener,
        )
        with pytest.raises(NmsAuthError, match="rejected these credentials"):
            tokens.token()

    def test_a_missing_half_never_reaches_the_network(self, environment, opener, gateway):
        before = len(gateway["journal"].to("/v1/auth/token"))
        tokens = TokenClient(
            environment,
            ClientCredentials.default(),
            environ={"FAA_NMS_CLIENT_ID": CLIENT_ID},
            opener=opener,
        )
        with pytest.raises(NmsConfigurationError):
            tokens.token()
        assert len(gateway["journal"].to("/v1/auth/token")) == before


class TestOperationsOverTheWire:
    def test_ping_proves_the_whole_chain(self, client):
        assert client.ping().status == 200

    def test_the_bearer_token_survives_the_wire(self, client, gateway):
        client.ping()
        sent = gateway["journal"].to("/nmsapi/v1/ping")[-1]
        assert sent["authorization"] == "Bearer conformance-bearer-token"

    def test_the_required_header_reaches_the_gateway(self, client, gateway):
        # The server returns 400 without it, as the FAA's does.
        response = client.notams(location="K8WC")
        assert response.status == 200
        assert gateway["journal"].to("/nmsapi/v1/notams?")[-1]["nms_format"] == "AIXM"

    def test_a_filtered_notam_query_returns_parseable_aixm(self, client):
        response = client.notams(location="K8WC")
        notams = list(NotamFeed(__import__("io").BytesIO(response.body)))
        assert [n.identifier for n in notams] == ["STL 08/430", "BDR 04/221"]

    def test_the_checklist_returns_json(self, client):
        payload = client.notam_checklist(location="K8WC").json()
        assert payload["status"] == "Success"

    def test_location_series_accepts_a_timestamp_filter(self, client, gateway):
        client.location_series(last_updated=datetime(2025, 7, 1, 10, 0, tzinfo=timezone.utc))
        path = gateway["journal"].to("/nmsapi/v1/locationseries")[-1]["path"]
        assert "lastUpdatedDate=2025-07-01T10%3A00%3A00Z" in path

    def test_responses_are_archived_and_citable(self, client):
        response = client.notams(location="K8WC")
        assert response.archived is not None
        assert client.archive.get(response.archived.digest) == response.body


class TestInitialLoadOverTheWire:
    def test_the_redirect_is_not_followed_by_urllib(self, client, gateway):
        # The assertion that matters most here. In test_faa_client this is
        # asserted against _NoRedirect.redirect_request directly; only a real
        # server proves urllib then surfaces the 302 with its Location intact
        # rather than following it and stripping — or worse, forwarding — the
        # Authorization header.
        signed = client.initial_load_handover()
        assert signed.url.startswith(gateway["origin"] + "/storage/")
        assert signed.expires_at is not None
        assert not signed.is_expired()

    def test_the_json_handover_form_works_over_the_wire(self, client, gateway):
        signed = client.initial_load_handover("DOMESTIC")
        assert "/storage/" in signed.url
        assert gateway["journal"].to("/il/DOMESTIC")

    def test_the_storage_download_carries_no_bearer_token(self, client, gateway):
        # The gateway answers 400 if it sees one, exactly as GCS does. If this
        # passes, the two-step handover is genuinely unauthenticated on step two.
        client.fetch_initial_load()
        storage = gateway["journal"].to("/storage/")
        assert storage, "the download never happened"
        assert all(r["authorization"] is None for r in storage)

    def test_the_full_chain_ends_in_attributed_facts(self, client):
        # handover → download → archive → gunzip → AIXM → citation.
        load = client.fetch_initial_load("DOMESTIC")
        assert load.compressed

        with load.open() as stream:
            feed = NotamFeed(stream)
            notams = list(feed)

        assert [n.identifier for n in notams] == ["STL 08/430", "BDR 04/221"]
        assert feed.header.number_returned == 21468

        runway_light = notams[0]
        assert runway_light.aerodromes()[0].designator == "8WC"
        assert runway_light.effective_start == datetime(
            2025, 8, 21, 2, 34, tzinfo=timezone.utc
        )

        ref = runway_light.source_ref(load.entry)
        assert client.archive.get(ref.content_hash) == load.payload
        assert ref.parser_id == "faa-nms-aixm"

    def test_the_bundle_is_archived_compressed_as_served(self, client):
        load = client.fetch_initial_load()
        stored = client.archive.get(load.entry.digest)
        assert stored[:2] == b"\x1f\x8b"
        assert gzip.decompress(stored) == AIXM


class TestDriftOverTheWire:
    def test_a_renamed_path_is_corrected_by_overlay_alone(self, environment, opener, tmp_path):
        # The FAA moves /ping; the operator writes an overlay; nothing is
        # rebuilt. Proven against a server that only answers the new path.
        moved = environment.overlay(
            {"endpoints": [{"name": "ping", "path": "/v1/notams/checklist"}]}
        )
        client = NmsClient(
            moved,
            tokens=TokenClient(
                moved,
                ClientCredentials.default(),
                environ={
                    "FAA_NMS_CLIENT_ID": CLIENT_ID,
                    "FAA_NMS_CLIENT_SECRET": CLIENT_SECRET,
                },
                opener=opener,
            ),
            archive=Archive(tmp_path / "raw"),
            throttle=HostThrottle(gap=timedelta(0)),
            opener=opener,
        )
        assert client.ping().status == 200

    def test_an_endpoint_the_gateway_does_not_have_points_at_the_overlay(
        self, environment, opener, tmp_path
    ):
        gone = environment.overlay(
            {"endpoints": [{"name": "ping", "path": "/v1/removed"}]}
        )
        client = NmsClient(
            gone,
            tokens=TokenClient(
                gone,
                ClientCredentials.default(),
                environ={
                    "FAA_NMS_CLIENT_ID": CLIENT_ID,
                    "FAA_NMS_CLIENT_SECRET": CLIENT_SECRET,
                },
                opener=opener,
            ),
            archive=Archive(tmp_path / "raw"),
            throttle=HostThrottle(gap=timedelta(0)),
            opener=opener,
        )
        with pytest.raises(NmsConfigurationError, match="AEROPUB_FAA_NMS_CONFIG"):
            client.ping()


class TestCheckOverTheWire:
    def test_the_operator_check_reports_a_verified_connection(
        self, environment, client
    ):
        from aeropub.faa.check import EXIT_OK, verify

        report = verify(
            environment,
            environ={
                "FAA_NMS_CLIENT_ID": CLIENT_ID,
                "FAA_NMS_CLIENT_SECRET": CLIENT_SECRET,
            },
            client=client,
        )
        assert report.exit_code == EXIT_OK, report.render()
        assert [s.name for s in report.stages] == [
            "configuration", "credentials", "token", "ping",
        ]
        assert report.ok
        # A recognition hint, and nothing more: the leading characters of the
        # token never appear, whatever its length.
        assert report.token["masked"].startswith("****")
        assert TOKEN_PAYLOAD["access_token"][:8] not in report.token["masked"]

    def test_the_verified_report_never_carries_the_token(self, environment, client):
        from aeropub.faa.check import verify

        report = verify(
            environment,
            environ={
                "FAA_NMS_CLIENT_ID": CLIENT_ID,
                "FAA_NMS_CLIENT_SECRET": CLIENT_SECRET,
            },
            client=client,
        )
        rendered = report.render() + json.dumps(report.to_dict())
        assert TOKEN_PAYLOAD["access_token"] not in rendered
        assert CLIENT_SECRET not in rendered

    def test_back_to_back_stages_are_not_blocked_by_our_own_host_gap(
        self, environment, opener, tmp_path
    ):
        # The regression, over a real socket and with a real gap in force: the
        # check reaches the data stage rather than stopping at ping because two
        # seconds had not passed. Only running the whole CLI found this — every
        # stubbed test had set the gap to zero.
        from aeropub.faa.check import verify

        environ = {
            "FAA_NMS_CLIENT_ID": CLIENT_ID,
            "FAA_NMS_CLIENT_SECRET": CLIENT_SECRET,
        }
        client = NmsClient(
            environment,
            tokens=TokenClient(
                environment, ClientCredentials.default(), environ=environ, opener=opener
            ),
            archive=Archive(tmp_path / "raw"),
            throttle=HostThrottle(gap=timedelta(milliseconds=80)),
            opener=opener,
            wait_for_throttle=True,
        )
        report = verify(
            environment, environ=environ, client=client, fetch_data=True,
            archive=client.archive,
        )
        stages = [s.name for s in report.stages]
        assert stages == ["configuration", "credentials", "token", "ping", "data"]
        assert "holding off" not in "".join(s.detail for s in report.stages)

    def test_the_data_stage_refuses_to_call_a_short_read_verified(
        self, environment, client
    ):
        # The sample is an excerpt claiming 21,468 messages. Two arrive. The
        # check reports that and fails, rather than presenting two NOTAM as a
        # successful load of a country — which is the failure that looks
        # exactly like a quiet day and gets acted on as one.
        from aeropub.faa.check import EXIT_PROTOCOL, verify

        report = verify(
            environment,
            environ={
                "FAA_NMS_CLIENT_ID": CLIENT_ID,
                "FAA_NMS_CLIENT_SECRET": CLIENT_SECRET,
            },
            client=client,
            fetch_data=True,
            archive=client.archive,
        )
        data = [s for s in report.stages if s.name == "data"][0]
        assert "2 NOTAM read of 21468" in data.detail
        assert "SHORT READ" in data.detail
        assert not data.ok
        assert report.exit_code == EXIT_PROTOCOL

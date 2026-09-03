"""Naming the layer that broke.

Most of this runs against real sockets: a real TLS server with a certificate
nothing trusts, and a real closed port. Both bind to 127.0.0.1, which sits in
every sane ``no_proxy``, so the results are the same whether or not the machine
running the tests is behind an egress proxy.

The proxy-denial and DNS classifications are asserted against the exception
objects urllib actually raises, rather than by arranging the failure — a test
that needs a hostile proxy to run is a test that does not run.
"""

from __future__ import annotations

import os
import io
import socket
import ssl
import urllib.error
import shutil
import subprocess
import threading
import urllib.error
from email.message import Message
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from aeropub.netcheck import (
    CA_BUNDLE_VARS,
    Layer,
    Probe,
    _classify,
    ca_bundle,
    opener_for,
    probe,
    proxy_for,
)

pytestmark = pytest.mark.skipif(
    shutil.which("openssl") is None, reason="needs openssl to mint a certificate"
)


@pytest.fixture
def clean_proxy_env(monkeypatch):
    """A process environment with no proxy variables of any spelling.

    getproxies_environment() lowercases every name ending in ``_proxy`` and
    merges them into one mapping, so HTTPS_PROXY, https_proxy and a platform's
    own NPM_CONFIG_HTTPS_PROXY all compete for the same key. Setting one
    without clearing the rest tests the machine, not the code.
    """
    for name in list(os.environ):
        if name.lower().endswith("_proxy"):
            monkeypatch.delenv(name, raising=False)
    return monkeypatch


@pytest.fixture(scope="module")
def guarded(tmp_path_factory):
    """A real HTTPS server, with a certificate nothing trusts, answering 401."""
    certs = tmp_path_factory.mktemp("netcheck-certs")
    cert, key = certs / "cert.pem", certs / "key.pem"
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-keyout", str(key),
         "-out", str(cert), "-days", "1", "-nodes", "-subj", "/CN=localhost",
         "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1"],
        check=True, capture_output=True,
    )

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def do_GET(self):
            body = b'{"error":"invalid_token"}'
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certfile=str(cert), keyfile=str(key))
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield {"url": f"https://localhost:{server.server_address[1]}/ping", "cert": cert}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


class TestReachability:
    def test_a_401_is_reachable_not_a_failure(self, guarded):
        # The whole point of a credential-free probe. An authority answering
        # 401 has proved DNS, the proxy, TLS and its own front door all work.
        # Reporting that as unreachable is what sends someone to rotate a
        # perfectly good key.
        result = probe(
            guarded["url"],
            opener=opener_for(ca_bundle_path=str(guarded["cert"])),
            environ={},
        )
        assert result.reachable
        assert result.layer is Layer.OK
        assert result.http_status == 401
        assert "as expected" in result.detail
        assert result.remedy() == ""

    def test_an_untrusted_certificate_is_ours_to_fix(self, guarded):
        # Same server, same port, no CA. An intercepting proxy looks exactly
        # like this, and it is configuration rather than an attack — but it is
        # not distinguishable from one, so it is never waved through.
        result = probe(guarded["url"], environ={})
        assert result.layer is Layer.TLS_UNTRUSTED
        assert result.layer.is_ours
        assert not result.layer.is_network_policy
        assert "CA bundle" in result.remedy()
        assert all(v in result.remedy() for v in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE"))

    def test_a_closed_port_is_refused(self):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()

        result = probe(f"https://127.0.0.1:{port}/", timeout=5, environ={})
        assert result.layer in (Layer.REFUSED, Layer.TIMEOUT)
        assert not result.reachable


class TestProxyBlockPages:
    """A 403 that never left the building.

    "Any HTTP status means reachable" holds only when the answer came from the
    origin. This session's proxy answers plain HTTP for a host outside its
    allowlist with a 403 and a block-page body, and reading that naively
    reported an unreachable manufacturer's site as reachable — which would send
    an operator hunting for a fault at the far end of a connection that was
    never made.
    """

    def _headers(self, **fields):
        message = Message()
        for name, value in fields.items():
            message[name.replace("_", "-")] = value
        return message

    def test_a_structured_denial_header_is_believed_over_the_status(self, guarded):
        opener = opener_for(ca_bundle_path=str(guarded["cert"]))

        def blocked(request, timeout=None):
            raise urllib.error.HTTPError(
                request.full_url, 403, "Forbidden",
                self._headers(x_deny_reason="host_not_allowed"),
                io.BytesIO(b"Host not in allowlist"),
            )

        result = probe(guarded["url"], opener=blocked, environ={})
        assert result.layer is Layer.PROXY_DENIED
        assert result.layer.is_network_policy
        assert "host_not_allowed" in result.detail
        assert not result.reachable

    def test_a_block_page_body_is_caught_where_no_header_says_so(self, guarded):
        def blocked(request, timeout=None):
            raise urllib.error.HTTPError(
                request.full_url, 403, "Forbidden", Message(),
                io.BytesIO(
                    b"Host not in allowlist: www.example.gov. Add this host to "
                    b"your network egress settings to allow access."
                ),
            )

        result = probe(guarded["url"], opener=blocked, environ={})
        assert result.layer is Layer.PROXY_DENIED
        assert "allowlist" in result.detail

    def test_a_denial_delivered_as_a_success_is_still_a_denial(self, guarded):
        # Some proxies serve their block page with 200.
        class _Response(io.BytesIO):
            def __init__(self, headers):
                super().__init__(b"blocked")
                self.status = 200
                self.headers = headers

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                self.close()
                return False

        headers = self._headers(x_deny_reason="host_not_allowed")
        result = probe(guarded["url"],
                       opener=lambda r, timeout=None: _Response(headers), environ={})
        assert result.layer is Layer.PROXY_DENIED

    def test_the_origins_own_403_is_not_turned_into_a_network_finding(self, guarded):
        # Matching loosely would send an operator to the wrong team for an
        # authorisation problem at the far end.
        def refused(request, timeout=None):
            raise urllib.error.HTTPError(
                request.full_url, 403, "Forbidden", Message(), io.BytesIO(b"Forbidden")
            )

        result = probe(guarded["url"], opener=refused, environ={})
        assert result.layer is Layer.OK
        assert result.reachable
        assert result.http_status == 403

    def test_a_401_from_the_origin_still_reads_as_reachable(self, guarded):
        result = probe(
            guarded["url"],
            opener=opener_for(ca_bundle_path=str(guarded["cert"])),
            environ={},
        )
        assert result.reachable and result.http_status == 401


class TestClassification:
    def test_a_refused_tunnel_is_the_proxys_doing_not_the_authoritys(self):
        # The single most misread failure in this stack. The proxy answers
        # CONNECT before any TLS happens, and urllib surfaces it as a bare
        # string that reads exactly like the authority refusing us.
        reason = "Tunnel connection failed: 403 Forbidden"
        layer, detail = _classify(OSError(reason), reason)
        assert layer is Layer.PROXY_DENIED
        assert layer.is_network_policy
        assert "403" in detail

    def test_a_407_is_also_the_proxy(self):
        reason = "Tunnel connection failed: 407 Proxy Authentication Required"
        assert _classify(OSError(reason), reason)[0] is Layer.PROXY_DENIED

    def test_an_unresolvable_name_is_dns(self):
        exc = socket.gaierror(-2, "Name or service not known")
        assert _classify(exc, str(exc))[0] is Layer.DNS

    def test_a_verification_failure_is_told_apart_from_other_tls_faults(self):
        verify = ssl.SSLCertVerificationError("certificate verify failed: unable to get issuer")
        assert _classify(verify, str(verify))[0] is Layer.TLS_UNTRUSTED
        other = ssl.SSLError("record layer failure")
        assert _classify(other, str(other))[0] is Layer.TLS

    def test_an_unrecognised_fault_is_unknown_not_guessed(self):
        assert _classify(OSError("something new"), "something new")[0] is Layer.UNKNOWN


class TestRemedies:
    def test_a_policy_denial_names_the_host_and_port_to_allow(self):
        # So the message can be forwarded to a network team unedited.
        result = Probe(
            url="https://api-nms.aim.faa.gov/nmsapi/v1/ping",
            host="api-nms.aim.faa.gov",
            layer=Layer.PROXY_DENIED,
            detail="egress proxy answered 403 to CONNECT",
        )
        assert "api-nms.aim.faa.gov:443" in result.remedy()
        assert "Do not route around it" in result.remedy()

    def test_the_owner_of_each_failure_is_recorded(self):
        assert Layer.PROXY_DENIED.is_network_policy
        assert Layer.PROXY_UNREACHABLE.is_network_policy
        assert Layer.TLS_UNTRUSTED.is_ours
        assert Layer.DNS.is_ours
        assert not Layer.REFUSED.is_ours and not Layer.REFUSED.is_network_policy


class TestConfiguration:
    def test_the_ca_bundle_comes_from_the_first_variable_that_exists(self, tmp_path):
        bundle = tmp_path / "ca.pem"
        bundle.write_text("")
        assert ca_bundle({"SSL_CERT_FILE": str(bundle)}) == str(bundle)
        # A variable naming a file that is not there is not a bundle.
        assert ca_bundle({"SSL_CERT_FILE": str(tmp_path / "absent.pem")}) is None
        assert ca_bundle({}) is None

    def test_aeropub_ca_bundle_wins_over_the_platform_variables(self, tmp_path):
        ours, theirs = tmp_path / "ours.pem", tmp_path / "theirs.pem"
        ours.write_text("")
        theirs.write_text("")
        assert CA_BUNDLE_VARS[0] == "AEROPUB_CA_BUNDLE"
        assert ca_bundle(
            {"AEROPUB_CA_BUNDLE": str(ours), "SSL_CERT_FILE": str(theirs)}
        ) == str(ours)

    def test_no_proxy_is_honoured_so_the_report_is_not_misleading(self):
        env = {"HTTPS_PROXY": "http://proxy:8080", "NO_PROXY": "localhost,.faa.gov"}
        assert proxy_for("https://api-nms.aim.faa.gov/x", env) is None
        assert proxy_for("https://localhost:9/x", env) is None
        assert proxy_for("https://example.gov/x", env) == "http://proxy:8080"

    def test_verification_cannot_be_switched_off(self):
        # Not a preference. A connector that can be talked into trusting
        # anything is one whose citations mean nothing.
        import inspect

        import aeropub.netcheck as module

        source = inspect.getsource(module)
        assert "CERT_NONE" not in source
        assert "check_hostname = False" not in source
        assert "_create_unverified" not in source

        opener = opener_for(environ={})
        handler = [h for h in opener.__self__.handlers
                   if type(h).__name__ == "HTTPSHandler"][0]
        context = handler._context
        assert context.check_hostname is True
        assert context.verify_mode is ssl.CERT_REQUIRED

    def test_the_opener_uses_the_proxy_the_machine_requires(self, clean_proxy_env):
        # Losing proxy handling would mean every request in a proxied
        # environment silently timing out. Every *_proxy variable is cleared
        # first: getproxies_environment lowercases and merges them all, so a
        # machine that sets https_proxy alongside HTTPS_PROXY — this one does,
        # and so do most managed environments — decides the outcome instead of
        # the test.
        clean_proxy_env.setenv("HTTPS_PROXY", "http://proxy.internal:8080")
        opener = opener_for(environ={})
        handlers = {type(h).__name__: h for h in opener.__self__.handlers}
        assert "ProxyHandler" in handlers
        assert handlers["ProxyHandler"].proxies.get("https") == "http://proxy.internal:8080"

    def test_proxy_settings_come_from_the_process_not_the_argument(self, clean_proxy_env):
        # environ selects the CA bundle. Reading proxies from a caller-supplied
        # mapping instead would let a caller quietly bypass the proxy the
        # machine actually requires.
        clean_proxy_env.setenv("HTTPS_PROXY", "http://proxy.internal:8080")
        opener = opener_for(environ={"HTTPS_PROXY": "http://ignored:9999"})
        handlers = {type(h).__name__: h for h in opener.__self__.handlers}
        assert handlers["ProxyHandler"].proxies.get("https") == "http://proxy.internal:8080"

    def test_no_proxy_configured_is_not_proxying_disabled(self, clean_proxy_env):
        # build_opener constructs a ProxyHandler, finds nothing for it to
        # handle, and drops it. Absence means "no proxy here", not "proxying
        # switched off" — worth pinning, because the opposite reading turns a
        # working opener into a bug report.
        opener = opener_for(environ={})
        names = [type(h).__name__ for h in opener.__self__.handlers]
        assert "ProxyHandler" not in names
        assert "HTTPSHandler" in names

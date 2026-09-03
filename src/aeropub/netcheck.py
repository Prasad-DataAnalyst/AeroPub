"""Which layer is broken: the network, the proxy, TLS, or the authority.

A connector that reports "could not reach the FAA" when the truth is "your own
egress proxy refused that host" sends an operator to the wrong place, and in an
airline that means a ticket to the wrong team. The four failures look identical
from inside a client and have entirely different owners:

===================  ====================================================
``dns``              the name does not resolve — our resolver, our problem
``proxy_denied``     an egress proxy refused the tunnel — the network team
``tls_untrusted``    an intercepting proxy's CA is not trusted — our config
``refused``          the host answered and closed — the authority
===================  ====================================================

The probe is credential-free by design. An authority answering ``401`` has
proved every one of those layers works, so reachability is established without
a key — which is exactly what an operator needs to know *before* concluding
their key is bad.

Nothing here disables certificate verification, and nothing here should ever
grow the option. A connector that can be talked into trusting anything is one
whose citations mean nothing.
"""

from __future__ import annotations

import os
import socket
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit

__all__ = ["Layer", "Probe", "opener_for", "probe"]

#: Environment variables that already carry a CA bundle, in the order a
#: sensible tool consults them. Set by most managed environments and by
#: corporate proxy tooling, so the common case needs no configuration at all.
CA_BUNDLE_VARS = ("AEROPUB_CA_BUNDLE", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE")


class Layer(str, Enum):
    """Where a connection attempt stopped."""

    OK = "ok"
    """The host answered. Any status counts — ``401`` proves reachability."""

    DNS = "dns"
    RESOLVED_NO_ROUTE = "no_route"
    PROXY_DENIED = "proxy_denied"
    """An egress proxy refused to open the tunnel. Almost always an allowlist."""

    PROXY_UNREACHABLE = "proxy_unreachable"
    TLS_UNTRUSTED = "tls_untrusted"
    """Certificate verification failed — usually an intercepting proxy whose CA
    we do not trust, which is configuration rather than an attack, but is not
    distinguishable from one and so is never waved through."""

    TLS = "tls"
    REFUSED = "refused"
    TIMEOUT = "timeout"
    UNKNOWN = "unknown"

    @property
    def is_ours(self) -> bool:
        """Whether we can fix this without asking anybody."""
        return self in (Layer.TLS_UNTRUSTED, Layer.DNS)

    @property
    def is_network_policy(self) -> bool:
        """Whether this needs a network administrator rather than a code change."""
        return self in (Layer.PROXY_DENIED, Layer.PROXY_UNREACHABLE)


@dataclass(frozen=True, slots=True)
class Probe:
    """One credential-free reachability attempt."""

    url: str
    host: str
    layer: Layer
    detail: str = ""
    http_status: int | None = None
    proxy: str | None = None
    ca_bundle: str | None = None
    duration_ms: int | None = None

    @property
    def reachable(self) -> bool:
        return self.layer is Layer.OK

    def remedy(self) -> str:
        """What to do about it, addressed to whoever can act."""
        if self.layer is Layer.OK:
            return ""
        if self.layer is Layer.PROXY_DENIED:
            return (
                f"An egress proxy refused a tunnel to {self.host}. This is a "
                "policy decision, not a fault: ask whoever owns the egress "
                f"allowlist to permit {self.host}:443. Do not route around it."
            )
        if self.layer is Layer.PROXY_UNREACHABLE:
            return (
                f"The proxy at {self.proxy} could not be contacted. Check "
                "HTTPS_PROXY is right and the proxy is running."
            )
        if self.layer is Layer.TLS_UNTRUSTED:
            return (
                "Certificate verification failed, which usually means an "
                "intercepting proxy is presenting its own certificate. Point "
                f"one of {', '.join(CA_BUNDLE_VARS)} at that proxy's CA bundle. "
                "Never disable verification."
            )
        if self.layer is Layer.DNS:
            return f"{self.host} does not resolve. Check the name and the resolver."
        if self.layer is Layer.TIMEOUT:
            return f"No answer from {self.host} within the timeout."
        if self.layer is Layer.REFUSED:
            return f"{self.host} refused the connection."
        return f"Could not reach {self.host}: {self.detail}"

    def describe(self) -> str:
        if self.layer is Layer.OK:
            via = f" via {self.proxy}" if self.proxy else " directly"
            return f"reached {self.host}{via} (HTTP {self.http_status})"
        return f"{self.layer.value}: {self.detail}"


def ca_bundle(environ: Mapping[str, str] | None = None) -> str | None:
    """The CA bundle in force, if one is configured."""
    env = os.environ if environ is None else environ
    for name in CA_BUNDLE_VARS:
        value = env.get(name)
        if value and os.path.exists(value):
            return value
    return None


def proxy_for(url: str, environ: Mapping[str, str] | None = None) -> str | None:
    """The HTTPS proxy that would carry this URL, honouring no_proxy."""
    env = os.environ if environ is None else environ
    host = urlsplit(url).hostname or ""
    skip = env.get("NO_PROXY") or env.get("no_proxy") or ""
    for entry in (e.strip().lower() for e in skip.split(",")):
        if entry and (host.lower() == entry or host.lower().endswith("." + entry.lstrip("."))):
            return None
    return env.get("HTTPS_PROXY") or env.get("https_proxy") or None


def opener_for(
    *,
    ca_bundle_path: str | None = None,
    extra_handlers: tuple[Any, ...] = (),
    environ: Mapping[str, str] | None = None,
) -> Callable[..., Any]:
    """An opener that trusts the configured CA and honours the configured proxy.

    Both are usually already right: ``ProxyHandler`` reads the environment on
    its own, and Python's default TLS context honours ``SSL_CERT_FILE``. This
    exists for the environment where they are not — a corporate proxy whose CA
    is on disk but not in any variable the platform pre-sets.

    ``environ`` selects the CA bundle only. Proxy settings always come from the
    process environment, because that is where a real connection has to read
    them: an opener built from a caller-supplied mapping would quietly bypass
    the proxy the machine actually requires.

    Note that ``build_opener`` only *lists* a ``ProxyHandler`` when a proxy is
    configured — with none set it constructs one, finds nothing to handle, and
    drops it. Absence from ``opener.__self__.handlers`` therefore means "no
    proxy in this environment", not "proxying disabled".
    """
    path = ca_bundle_path or ca_bundle(environ)
    context = ssl.create_default_context(cafile=path) if path else ssl.create_default_context()
    # Stated rather than assumed: the two settings that make a certificate mean
    # something. Nothing in this module may turn them off.
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    return urllib.request.build_opener(
        *extra_handlers, urllib.request.HTTPSHandler(context=context)
    ).open


def _classify(reason: Any, text: str) -> tuple[Layer, str]:
    """Turn a URLError reason into the layer that owns it."""
    lowered = text.lower()

    # The proxy answers CONNECT with a status before any TLS happens, and
    # urllib surfaces it as a bare string. It is the single most misread
    # failure in this whole stack: it looks like the authority refusing us.
    if "tunnel connection failed" in lowered:
        status = "".join(c for c in text.split("Tunnel connection failed:")[-1] if c.isdigit())[:3]
        if status in ("403", "407"):
            return Layer.PROXY_DENIED, f"egress proxy answered {status} to CONNECT"
        return Layer.PROXY_DENIED, text.strip()

    if isinstance(reason, ssl.SSLCertVerificationError) or "certificate verify failed" in lowered:
        return Layer.TLS_UNTRUSTED, text.strip()
    if isinstance(reason, ssl.SSLError) or "ssl" in lowered:
        return Layer.TLS, text.strip()
    if isinstance(reason, socket.gaierror) or "name or service not known" in lowered \
            or "nodename nor servname" in lowered or "getaddrinfo" in lowered:
        return Layer.DNS, text.strip()
    if isinstance(reason, ConnectionRefusedError) or "connection refused" in lowered:
        return Layer.REFUSED, text.strip()
    if isinstance(reason, (TimeoutError, socket.timeout)) or "timed out" in lowered:
        return Layer.TIMEOUT, text.strip()
    if "no route to host" in lowered or "network is unreachable" in lowered:
        return Layer.RESOLVED_NO_ROUTE, text.strip()
    return Layer.UNKNOWN, text.strip()


def probe(
    url: str,
    *,
    timeout: int = 20,
    environ: Mapping[str, str] | None = None,
    opener: Callable[..., Any] | None = None,
    user_agent: str = "AeroPub/0.1 reachability probe",
) -> Probe:
    """Can we reach this address at all, without a credential?

    A ``401`` is a success: it means DNS, the proxy, TLS and the authority's
    front door all work, and only the key is missing. Reporting that as a
    failure is what sends someone to rotate a perfectly good credential.
    """
    host = urlsplit(url).hostname or url
    via = proxy_for(url, environ)
    bundle = ca_bundle(environ)
    call = opener or urllib.request.urlopen
    request = urllib.request.Request(url, headers={"User-Agent": user_agent})

    started = time.monotonic()
    try:
        with call(request, timeout=timeout) as response:
            return Probe(
                url=url, host=host, layer=Layer.OK,
                http_status=getattr(response, "status", None),
                detail="reachable", proxy=via, ca_bundle=bundle,
                duration_ms=int((time.monotonic() - started) * 1000),
            )
    except urllib.error.HTTPError as exc:
        # Any HTTP status means the whole path worked. 401 is the expected
        # answer to an unauthenticated probe and is the strongest signal here.
        return Probe(
            url=url, host=host, layer=Layer.OK, http_status=exc.code,
            detail=f"reachable (HTTP {exc.code} without a credential, as expected)",
            proxy=via, ca_bundle=bundle,
            duration_ms=int((time.monotonic() - started) * 1000),
        )
    except urllib.error.URLError as exc:
        layer, detail = _classify(exc.reason, str(exc.reason))
        return Probe(
            url=url, host=host, layer=layer, detail=detail, proxy=via,
            ca_bundle=bundle, duration_ms=int((time.monotonic() - started) * 1000),
        )
    except (TimeoutError, socket.timeout):
        return Probe(
            url=url, host=host, layer=Layer.TIMEOUT,
            detail=f"no answer within {timeout}s", proxy=via, ca_bundle=bundle,
            duration_ms=int((time.monotonic() - started) * 1000),
        )
    except OSError as exc:
        layer, detail = _classify(exc, str(exc))
        return Probe(
            url=url, host=host, layer=layer, detail=detail, proxy=via,
            ca_bundle=bundle, duration_ms=int((time.monotonic() - started) * 1000),
        )

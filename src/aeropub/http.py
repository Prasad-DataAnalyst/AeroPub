"""HTTP transport — conditional, polite, and archived.

Implements the :class:`~aeropub.watcher.Fetcher` protocol against real HTTP.
Three concerns, kept separate so each is testable on its own:

**Conditional requests.** A source is checked far more often than it changes, so
almost every check should cost a ``304 Not Modified`` and a few hundred bytes.
``ETag`` and ``Last-Modified`` are remembered per source and sent back as
``If-None-Match`` and ``If-Modified-Since``.

**Politeness.** A minimum gap between requests to the same host, exponential
backoff after failures, and ``Retry-After`` honoured when a server sends one.
This is not courtesy for its own sake: a State that blocks our address turns
into a silent coverage gap, which is the worst failure this system has.

**Archiving.** Every body that comes back is written to the raw store before it
is parsed, so a citation always resolves to the exact bytes that produced it.
"""

from __future__ import annotations

import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Mapping
from urllib.parse import urlsplit

from aeropub.archive import Archive, digest_of
from aeropub.registry import Source
from aeropub.watcher import FetchResult

__all__ = [
    "BACKOFF_BASE",
    "BACKOFF_CAP",
    "DEFAULT_HOST_GAP",
    "ConditionalState",
    "HostThrottle",
    "HttpFetcher",
    "backoff_delay",
    "interpret",
    "retry_after_seconds",
]

USER_AGENT = (
    "AeroPub/0.1 (+https://github.com/Prasad-DataAnalyst/AeroPub) "
    "aeronautical publication monitoring"
)

#: Minimum gap between requests to one host. National AIS sites are small
#: estates, often a single server, and are not built for aggressive polling.
DEFAULT_HOST_GAP = timedelta(seconds=2)

BACKOFF_BASE = timedelta(minutes=1)
BACKOFF_CAP = timedelta(hours=6)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def backoff_delay(consecutive_failures: int) -> timedelta:
    """How long to wait after ``n`` failures in a row.

    Doubles each time to a hard cap. Deterministic on purpose: jitter belongs at
    the scheduler, where it can be seeded, not buried in a transport where it
    would make failures unreproducible.
    """
    if consecutive_failures <= 0:
        return timedelta(0)
    # Cap the exponent, not just the result: a long-dead source accumulates
    # failures indefinitely, and 2**n overflows timedelta long before the
    # comparison against the cap ever runs.
    exponent = min(consecutive_failures - 1, 20)
    return min(BACKOFF_BASE * (2 ** exponent), BACKOFF_CAP)


def retry_after_seconds(value: str | None, *, now: datetime | None = None) -> int | None:
    """Parse a ``Retry-After`` header, which may be seconds or an HTTP date."""
    if not value:
        return None
    text = value.strip()
    if text.isdigit():
        return int(text)
    try:
        when = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    seconds = int((when - (now or _utcnow())).total_seconds())
    return max(seconds, 0)


@dataclass
class ConditionalState:
    """What the last response told us, so the next request can be conditional."""

    etag: str | None = None
    last_modified: str | None = None

    def headers(self) -> dict[str, str]:
        out = {}
        if self.etag:
            out["If-None-Match"] = self.etag
        if self.last_modified:
            out["If-Modified-Since"] = self.last_modified
        return out

    def update(self, response_headers: Mapping[str, str]) -> None:
        lower = {k.lower(): v for k, v in response_headers.items()}
        if "etag" in lower:
            self.etag = lower["etag"]
        if "last-modified" in lower:
            self.last_modified = lower["last-modified"]


class HostThrottle:
    """Enforces a minimum gap between requests to the same host."""

    def __init__(self, gap: timedelta = DEFAULT_HOST_GAP) -> None:
        self.gap = gap
        self._last: dict[str, datetime] = {}
        self._blocked_until: dict[str, datetime] = {}

    @staticmethod
    def host_of(url: str) -> str:
        return urlsplit(url).netloc.lower()

    def ready_at(self, url: str, *, now: datetime | None = None) -> datetime:
        """The earliest moment this host may be contacted again."""
        moment = now or _utcnow()
        host = self.host_of(url)
        earliest = moment
        last = self._last.get(host)
        if last is not None:
            earliest = max(earliest, last + self.gap)
        blocked = self._blocked_until.get(host)
        if blocked is not None:
            earliest = max(earliest, blocked)
        return earliest

    def may_request(self, url: str, *, now: datetime | None = None) -> bool:
        moment = now or _utcnow()
        return self.ready_at(url, now=moment) <= moment

    def record_request(self, url: str, *, at: datetime | None = None) -> None:
        self._last[self.host_of(url)] = at or _utcnow()

    def back_off(self, url: str, delay: timedelta, *, at: datetime | None = None) -> None:
        """Hold off this host — after a refusal, or on a server's instruction."""
        moment = at or _utcnow()
        host = self.host_of(url)
        until = moment + delay
        self._blocked_until[host] = max(self._blocked_until.get(host, moment), until)

    def clear(self, url: str) -> None:
        self._blocked_until.pop(self.host_of(url), None)


def interpret(
    status: int | None,
    headers: Mapping[str, str],
    body: bytes | None,
    *,
    duration_ms: int | None = None,
    now: datetime | None = None,
) -> FetchResult:
    """Turn an HTTP response into a transport verdict.

    Pure, so every status path is testable without a server.
    """
    lower = {k.lower(): v for k, v in headers.items()}

    if status is None:
        # Not every scheme carries a status line — file:// does not, and some
        # proxies drop it. urlopen raises on HTTP errors, so reaching here with
        # a body means the transfer succeeded; without one it did not.
        if body is None:
            return FetchResult(
                ok=False, error="no status and no body", duration_ms=duration_ms
            )
        return FetchResult(
            ok=True, content_hash=digest_of(body), duration_ms=duration_ms
        )

    if status == 304:
        return FetchResult(
            ok=True, http_status=304, not_modified=True, duration_ms=duration_ms
        )

    if status in (401, 403):
        # 403 is ambiguous: it can mean "your key is wrong" or "you are being
        # refused". A WWW-Authenticate header settles it; without one, treat it
        # as a refusal and back off rather than hammering with a bad credential.
        if status == 401 or "www-authenticate" in lower:
            return FetchResult(
                ok=False,
                http_status=status,
                unauthorised=True,
                error="credential rejected by the authority",
                duration_ms=duration_ms,
            )
        return FetchResult(
            ok=False,
            http_status=status,
            blocked=True,
            error="refused",
            duration_ms=duration_ms,
        )

    if status == 429 or 500 <= status < 600:
        return FetchResult(
            ok=False,
            http_status=status,
            blocked=True,
            error=f"HTTP {status}",
            duration_ms=duration_ms,
        )

    if 400 <= status < 500:
        return FetchResult(
            ok=False, http_status=status, error=f"HTTP {status}", duration_ms=duration_ms
        )

    if 200 <= status < 300:
        if body is None:
            return FetchResult(
                ok=False,
                http_status=status,
                error="empty response body",
                duration_ms=duration_ms,
            )
        return FetchResult(
            ok=True,
            http_status=status,
            content_hash=digest_of(body),
            duration_ms=duration_ms,
        )

    return FetchResult(
        ok=False, http_status=status, error=f"unexpected HTTP {status}",
        duration_ms=duration_ms,
    )


@dataclass
class HttpFetcher:
    """Fetches sources over HTTP, conditionally, politely, and into the archive."""

    archive: Archive | None = None
    throttle: HostThrottle = field(default_factory=HostThrottle)
    timeout: int = 60
    user_agent: str = USER_AGENT
    _conditional: dict[str, ConditionalState] = field(default_factory=dict, init=False)

    def conditional_for(self, source_id: str) -> ConditionalState:
        return self._conditional.setdefault(source_id, ConditionalState())

    def build_headers(self, source_id: str, credential: str | None) -> dict[str, str]:
        headers = {"User-Agent": self.user_agent}
        headers.update(self.conditional_for(source_id).headers())
        if credential:
            # Sent, never stored, never logged, never archived.
            headers["Authorization"] = credential
        return headers

    def fetch(self, source: Source, *, credential: str | None = None) -> FetchResult:
        """Fetch a registered source. Satisfies the Fetcher protocol."""
        return self.fetch_url(source.url, source.source_id, credential=credential)

    def fetch_url(
        self, url: str, source_id: str, *, credential: str | None = None
    ) -> FetchResult:
        """Fetch one address.

        Separate from :meth:`fetch` because the transport has no business
        knowing what a registry Source is — it needs an address and something to
        key conditional state by, and nothing more.
        """
        now = _utcnow()
        if not self.throttle.may_request(url, now=now):
            wait = self.throttle.ready_at(url, now=now) - now
            return FetchResult(
                ok=False,
                blocked=True,
                error=f"host throttled, ready in {int(wait.total_seconds())}s",
            )

        request = urllib.request.Request(
            url, headers=self.build_headers(source_id, credential)
        )
        started = time.monotonic()
        self.throttle.record_request(url, at=now)

        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read()
                status = response.status
                headers = dict(response.headers.items())
        except urllib.error.HTTPError as exc:
            elapsed = int((time.monotonic() - started) * 1000)
            headers = dict(exc.headers.items()) if exc.headers else {}
            result = interpret(exc.code, headers, None, duration_ms=elapsed, now=now)
            if result.blocked:
                seconds = retry_after_seconds(headers.get("Retry-After"), now=now)
                delay = timedelta(seconds=seconds) if seconds else backoff_delay(1)
                self.throttle.back_off(url, delay, at=now)
            return result
        except urllib.error.URLError as exc:
            elapsed = int((time.monotonic() - started) * 1000)
            return FetchResult(
                ok=False, error=f"could not reach host: {exc.reason}", duration_ms=elapsed
            )
        except TimeoutError:
            elapsed = int((time.monotonic() - started) * 1000)
            return FetchResult(ok=False, error="timed out", duration_ms=elapsed)

        elapsed = int((time.monotonic() - started) * 1000)
        result = interpret(status, headers, body, duration_ms=elapsed, now=now)

        if result.ok:
            self.throttle.clear(url)
            self.conditional_for(source_id).update(headers)

        # Archive before parsing, so a citation always resolves to real bytes.
        if result.ok and not result.not_modified and self.archive is not None:
            self.archive.put(
                body,
                source_id=source_id,
                url=url,
                retrieved_at=now,
                http_status=status,
                content_type=headers.get("Content-Type"),
            )
        return result

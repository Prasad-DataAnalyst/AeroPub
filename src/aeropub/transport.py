"""The live transport: conditional, polite, and archived.

:mod:`aeropub.cycle` needs one function from URL to :class:`~aeropub.reader.Retrieved`.
This is that function over real HTTP, and it exists so the cycle can ask the
world what changed without downloading the world to find out.

Why conditional requests are not an optimisation
------------------------------------------------
An AIP changes on a 28-day cycle and is checked far more often than that, so
almost every check should find nothing new. Downloading 80 sections from every
State on every pass to compare hashes would work, and would also mean pulling
tens of gigabytes a day from States' AIM servers to learn that nothing had
happened. A State that blocks our address turns into a silent coverage gap,
which is the worst failure this system has — so politeness here is a
correctness property, not a courtesy.

With ``ETag`` and ``Last-Modified`` remembered per URL, the same check costs a
``304`` and a few hundred bytes. The cycle already knows what to do with one:
:attr:`~aeropub.reader.Retrieved.unchanged` says the copy we hold was confirmed
current, and nothing is re-read, re-hashed or re-archived.

What this deliberately does not do
----------------------------------
It does not decide what to keep, what to parse, or what a document is. Those
belong to :mod:`aeropub.live`, the profile, and :mod:`aeropub.publication`
respectively. A transport that knew about AIP sections would be a transport
that had to change every time a State did.
"""

from __future__ import annotations

import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .http import ConditionalState, HostThrottle
from .reader import Retrieved, media_type_of

__all__ = ["LiveTransport", "TransportError"]

#: Identifies us to a State's server. A real contact address matters: an AIM
#: administrator who wants to ask why we are fetching should be able to.
USER_AGENT = "AeroPub/0.1 (aeronautical information analysis)"

#: Minimum gap between requests to one host. An AIP is a hundred pages and a
#: State's AIM server is not a CDN.
DEFAULT_GAP = timedelta(milliseconds=500)


class TransportError(RuntimeError):
    """A document could not be retrieved.

    Raised rather than returned so the cycle's own guard records it as a failed
    document. A transport that returned an empty :class:`Retrieved` on failure
    would let an unreachable page look like an empty one.
    """


@dataclass
class LiveTransport:
    """Fetches one document, conditionally and politely.

    Conditional state is keyed by URL rather than by source, because a State's
    sections change independently: ENR 3.2 being amended says nothing about
    whether GEN 0.4 has moved, and a shared validator would re-read all eighty
    whenever one of them changed.
    """

    throttle: HostThrottle = field(default_factory=lambda: HostThrottle(DEFAULT_GAP))
    timeout: int = 60
    user_agent: str = USER_AGENT
    _conditional: dict[str, ConditionalState] = field(default_factory=dict, init=False)

    #: Set by the caller to wait out a throttle rather than fail. Left alone,
    #: a throttled request raises and the cycle records it — which is right for
    #: a batch that should not block, and wrong for a single deliberate fetch.
    sleep: object | None = None

    def conditional_for(self, url: str) -> ConditionalState:
        return self._conditional.setdefault(url, ConditionalState())

    def forget(self, url: str) -> None:
        """Drop what we remember, so the next request is unconditional.

        For a document we hold no archived copy of: asking conditionally would
        invite a 304 confirming a copy that does not exist.
        """
        self._conditional.pop(url, None)

    def __call__(self, url: str) -> Retrieved:
        """Satisfies :data:`aeropub.reader.Retrieve`."""
        now = datetime.now(timezone.utc)
        if not self.throttle.may_request(url, now=now):
            wait = self.throttle.ready_at(url, now=now) - now
            if self.sleep is None:
                raise TransportError(
                    f"{url}: host throttled, ready in "
                    f"{max(0, int(wait.total_seconds()))}s"
                )
            self.sleep(max(0.0, wait.total_seconds()))
            now = datetime.now(timezone.utc)

        headers = {"User-Agent": self.user_agent}
        headers.update(self.conditional_for(url).headers())
        request = urllib.request.Request(url, headers=headers)
        self.throttle.record_request(url, at=now)

        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read()
                received = dict(response.headers.items())
                status = response.status
        except urllib.error.HTTPError as error:
            if error.code == 304:
                # Confirmed current. The headers may carry a refreshed
                # validator, so they are still recorded.
                self.conditional_for(url).update(dict(error.headers.items()))
                return Retrieved(
                    url=url,
                    body=b"",
                    media_type=error.headers.get("Content-Type", "") or "",
                    type_was_declared=bool(error.headers.get("Content-Type")),
                    retrieved_at=datetime.now(timezone.utc),
                    unchanged=True,
                )
            if error.code in (429, 503):
                # Back off this host rather than retry into a ban.
                self.throttle.back_off(url, _retry_after(error.headers), at=now)
                raise TransportError(
                    f"{url}: HTTP {error.code}, backing off this host"
                ) from None
            raise TransportError(f"{url}: HTTP {error.code}") from None
        except urllib.error.URLError as error:
            raise TransportError(f"{url}: {error.reason}") from None
        except Exception as error:  # noqa: BLE001 — never leak a raw transport fault
            raise TransportError(f"{url}: {type(error).__name__}: {error}") from None

        if status == 304:  # some servers answer 304 without raising
            return Retrieved(
                url=url, body=b"", media_type="", type_was_declared=False,
                retrieved_at=datetime.now(timezone.utc), unchanged=True,
            )

        self.conditional_for(url).update(received)
        media_type, declared = media_type_of(
            url, body, received.get("Content-Type", "")
        )
        return Retrieved(
            url=url,
            body=body,
            media_type=media_type,
            type_was_declared=declared,
            retrieved_at=datetime.now(timezone.utc),
        )


def _retry_after(headers) -> timedelta:
    """How long a server asked us to wait, or a sane default.

    Honoured rather than overridden: a server that says 60 seconds and is
    ignored is a server that stops answering.
    """
    raw = headers.get("Retry-After")
    if raw:
        try:
            return timedelta(seconds=max(1, int(str(raw).strip())))
        except ValueError:
            pass
    return timedelta(minutes=5)

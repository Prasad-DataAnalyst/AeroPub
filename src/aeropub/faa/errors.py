"""Failure modes of the FAA connection, kept distinct because the fixes differ.

A connector that raises one exception type forces the operator to read the
message to know whether to rotate a key, wait, or call the FAA. These types
carry that verdict in the type itself, so the status board and the retry logic
can both act on it without parsing prose.
"""

from __future__ import annotations

import re

__all__ = [
    "NmsAuthError",
    "NmsConfigurationError",
    "NmsError",
    "NmsProtocolError",
    "NmsTransportError",
    "NmsUnavailableError",
    "redact",
]

#: Patterns for anything credential-shaped that might appear in a response
#: body, a header echo or a traceback. Applied to every message before it can
#: reach a log, a status board or an exception a caller might print.
_SECRETS = (
    re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._\-]{8,}"),
    re.compile(r"(?i)\b(basic)\s+[A-Za-z0-9+/=]{8,}"),
    re.compile(r'(?i)("?(?:access_token|client_secret|refresh_token|secret|password)"?\s*[:=]\s*"?)[^",\s&}]+'),
    re.compile(r"(?i)([?&](?:X-Goog-Signature|Signature|key|api_key)=)[^&\s]+"),
)


def redact(text: str) -> str:
    """Strip credential-shaped substrings from a message.

    Defence in depth, not the primary control. Secrets are not supposed to be
    in these strings at all; this exists because a gateway that echoes the
    Authorization header in an error body would otherwise put a live bearer
    token into a log file we keep forever.
    """
    if not text:
        return text
    out = text
    for pattern in _SECRETS:
        out = pattern.sub(lambda m: f"{m.group(1)}[redacted]", out)
    return out


class NmsError(Exception):
    """Base class for every failure of the FAA NMS connection."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(redact(message))
        self.status = status

    @property
    def is_retryable(self) -> bool:
        """Whether trying again unchanged could plausibly succeed."""
        return False


class NmsConfigurationError(NmsError):
    """We are misconfigured: a missing key, an unknown endpoint, a bad host.

    Never retryable. Retrying a configuration error is how a client ends up
    rate-limited for a fault entirely its own.
    """


class NmsAuthError(NmsError):
    """The FAA rejected the credentials, or the token they issued.

    Distinct from :class:`NmsUnavailableError` because the remedy is a rotated
    key, not patience.
    """


class NmsTransportError(NmsError):
    """The request did not complete — DNS, TLS, connection, timeout."""

    @property
    def is_retryable(self) -> bool:
        return True


class NmsUnavailableError(NmsError):
    """The FAA is refusing or failing — 429 or 5xx."""

    def __init__(
        self, message: str, *, status: int | None = None, retry_after: int | None = None
    ) -> None:
        super().__init__(message, status=status)
        self.retry_after = retry_after
        """Seconds the server asked us to wait, when it said."""

    @property
    def is_retryable(self) -> bool:
        return True


class NmsProtocolError(NmsError):
    """The response arrived but was not what the contract describes.

    Raised rather than guessed around. A payload we do not recognise is a
    coverage gap that must be visible, not one to paper over with a default.
    """

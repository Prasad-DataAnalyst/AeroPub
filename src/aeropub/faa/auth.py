"""OAuth2 client-credentials against the FAA NMS-API.

The FAA gateway issues a bearer token that lives about thirty minutes. Every
call needs one, so the whole connector's reliability rests on this module doing
three unglamorous things correctly:

**Refreshing early.** A token is replaced before it expires, not after a call
fails with it. Expiry is measured against *our* clock from the moment the
request was sent, never against the gateway's ``issued_at``: the two clocks
differ, and trusting theirs means holding a token we believe is live for as
long as the skew.

**Refreshing once.** The watcher runs many sources concurrently. Without a lock
they would each notice the same expiry and each ask for a token, which is both
rude and a good way to trip a rate limit at exactly the wrong moment.

**Never disclosing the token.** :class:`AccessToken` does not render its own
value — not in ``repr``, not in ``str``, not in a traceback, not in a log line.
The token response body is also never archived, unlike every other response
this system fetches: the archive is append-only and never pruned, so a
credential written into it could not be withdrawn.
"""

from __future__ import annotations

import base64
import json
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping

from aeropub.faa.config import ClientCredentials, NmsEnvironment, load_environment
from aeropub.netcheck import opener_for
from aeropub.faa.errors import (
    NmsAuthError,
    NmsConfigurationError,
    NmsProtocolError,
    NmsTransportError,
    NmsUnavailableError,
    redact,
)

__all__ = [
    "CONSERVATIVE_TTL",
    "REFRESH_MARGIN",
    "AccessToken",
    "TokenClient",
    "TokenResponse",
]

#: Replace a token this long before it expires. Comfortably longer than any
#: single call, so a token cannot die between the check and the request.
REFRESH_MARGIN = timedelta(seconds=120)

#: Used when the gateway omits ``expires_in``. Short on purpose: re-authorising
#: more often than necessary costs one cheap call, while over-estimating a
#: lifetime costs a failed fetch during a publication window.
CONSERVATIVE_TTL = timedelta(seconds=300)

_USER_AGENT = (
    "AeroPub/0.1 (+https://github.com/Prasad-DataAnalyst/AeroPub) "
    "aeronautical publication monitoring"
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class TokenResponse:
    """The gateway's token payload, minus the token.

    Everything here is safe to log and useful when something is wrong: which
    key was used, which organisation it belongs to, which products it grants.
    The token itself lives in :class:`AccessToken` and is never copied here.
    """

    client_id: str | None = None
    organization: str | None = None
    developer_email: str | None = None
    application_name: str | None = None
    api_products: tuple[str, ...] = ()
    scope: str = ""
    status: str | None = None
    token_type: str | None = None
    issued_at: datetime | None = None
    """The gateway's own view of when it issued this. Diagnostic only —
    :attr:`AccessToken.expires_at` is not derived from it."""

    expires_in: int | None = None

    def describe(self) -> str:
        """One line for an operator checking a new key works."""
        who = self.organization or self.client_id or "unknown client"
        products = ", ".join(self.api_products) or "no products listed"
        ttl = f"{self.expires_in}s" if self.expires_in else "unstated lifetime"
        return f"{who} — {products} — status {self.status or 'unstated'}, {ttl}"


@dataclass(frozen=True, slots=True)
class AccessToken:
    """A bearer token and when to stop trusting it.

    The value is deliberately awkward to get at: :meth:`header` produces the
    one thing callers legitimately need, and every other accessor returns a
    masked form. This is not decoration — an ``f"{token}"`` in a debug line is
    exactly how bearer tokens end up in logs.
    """

    _value: str
    expires_at: datetime
    obtained_at: datetime
    response: TokenResponse = field(default_factory=TokenResponse)

    def __post_init__(self) -> None:
        if not self._value.strip():
            raise NmsProtocolError("the gateway returned an empty access token")
        for name in ("expires_at", "obtained_at"):
            moment = getattr(self, name)
            if moment.tzinfo is None:
                raise ValueError(f"AccessToken.{name} must be timezone-aware (UTC)")

    def header(self) -> dict[str, str]:
        """The Authorization header. The only sanctioned use of the value."""
        return {"Authorization": f"Bearer {self._value}"}

    @property
    def masked(self) -> str:
        """A recognition hint. Never enough to use."""
        tail = self._value[-4:] if len(self._value) >= 24 else ""
        return f"****{tail}" if tail else "****"

    def expires_in(self, now: datetime | None = None) -> timedelta:
        return self.expires_at - (now or _utcnow())

    def is_usable(self, now: datetime | None = None, *, margin: timedelta = REFRESH_MARGIN) -> bool:
        """Whether this token can safely carry a call starting now."""
        return self.expires_in(now) > margin

    def __repr__(self) -> str:
        return (
            f"AccessToken(masked={self.masked!r}, "
            f"expires_at={self.expires_at.isoformat()})"
        )

    __str__ = __repr__


class TokenClient:
    """Obtains and holds one bearer token, refreshing it before it dies."""

    def __init__(
        self,
        environment: NmsEnvironment | None = None,
        credentials: ClientCredentials | None = None,
        *,
        environ: Mapping[str, str] | None = None,
        timeout: int = 30,
        opener: Callable[..., Any] | None = None,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self.environment = environment or load_environment(environ=environ)
        self.credentials = credentials or ClientCredentials.default()
        self.timeout = timeout
        # The caller's environment, or None for os.environ. Held as a reference
        # so the secret can be read at the moment of authentication, which is
        # the same relationship os.environ has to it — the mapping belongs to
        # the caller. What must never happen, and does not, is this class
        # copying a secret out of it into state of its own.
        self._environ = environ
        self._opener = opener or opener_for(environ=environ)
        self._clock = clock
        self._token: AccessToken | None = None
        self._lock = threading.Lock()
        self._rejected = False

    # -- state -----------------------------------------------------------

    @property
    def rejected(self) -> bool:
        """Whether the FAA last told us these credentials are not valid."""
        return self._rejected

    def current(self) -> AccessToken | None:
        """The held token, if any. Does not fetch."""
        return self._token

    def forget(self) -> None:
        """Drop the held token, so the next call re-authorises.

        Called when a request comes back 401 despite a token we believed live —
        which happens when the FAA revokes early, or restarts the gateway.
        """
        with self._lock:
            self._token = None

    # -- acquisition -----------------------------------------------------

    def token(self, *, force: bool = False) -> AccessToken:
        """A usable token, refreshing if the held one is near expiry."""
        with self._lock:
            held = self._token
            if not force and held is not None and held.is_usable(self._clock()):
                return held
            fresh = self._request_token()
            self._token = fresh
            return fresh

    def header(self) -> dict[str, str]:
        """The Authorization header for an API call, refreshing as needed."""
        return self.token().header()

    def _request_token(self) -> AccessToken:
        pair = self.credentials.resolve(self._environ)
        if pair is None:
            missing = ", ".join(self.credentials.missing(self._environ))
            raise NmsConfigurationError(
                f"FAA NMS credentials are not installed: set {missing}. "
                "The FAA issues these on the onboarding spreadsheet — the KEY "
                "column is the client id, the SECRET column the client secret."
            )
        client_id, client_secret = pair

        # HTTP Basic, per the FAA's own curl example (-u KEY:SECRET). Encoded
        # here and discarded with the local; never held on the instance.
        basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode("ascii")
        body = urllib.parse.urlencode({"grant_type": "client_credentials"}).encode()
        request = urllib.request.Request(
            self.environment.token_url,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Basic {basic}",
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
                "User-Agent": _USER_AGENT,
            },
        )

        # Taken before the request, so the lifetime we compute is the one the
        # gateway granted minus the time the round trip took, never more.
        sent_at = self._clock()

        try:
            with self._opener(request, timeout=self.timeout) as response:
                payload = response.read()
                status = getattr(response, "status", 200)
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = redact(exc.read().decode("utf-8", "replace")[:400])
            except Exception:  # pragma: no cover - the body is a nicety
                detail = ""
            self._raise_for_status(exc.code, detail, exc.headers)
            raise  # pragma: no cover - _raise_for_status always raises
        except urllib.error.URLError as exc:
            raise NmsTransportError(
                f"could not reach the FAA token endpoint at "
                f"{self.environment.token_url}: {exc.reason}"
            ) from None
        except TimeoutError:
            raise NmsTransportError(
                f"timed out after {self.timeout}s asking the FAA for a token"
            ) from None

        if status is not None and not 200 <= status < 300:
            self._raise_for_status(status, "", {})

        return self._build_token(payload, sent_at)

    @staticmethod
    def _raise_for_status(status: int, detail: str, headers: Any) -> None:
        suffix = f" — {detail}" if detail else ""
        if status in (400, 401, 403):
            # 400 belongs here: an OAuth2 gateway answers a bad
            # client_credentials grant with invalid_client and a 400, not a 401.
            raise NmsAuthError(
                f"the FAA rejected these credentials (HTTP {status}){suffix}. "
                "Check the key is for this environment — a staging key does "
                "not work against production.",
                status=status,
            )
        if status == 429 or 500 <= status < 600:
            retry_after = None
            try:
                raw = headers.get("Retry-After") if headers else None
                retry_after = int(raw) if raw and str(raw).strip().isdigit() else None
            except Exception:  # pragma: no cover
                retry_after = None
            raise NmsUnavailableError(
                f"the FAA token endpoint is unavailable (HTTP {status}){suffix}",
                status=status,
                retry_after=retry_after,
            )
        raise NmsProtocolError(
            f"unexpected HTTP {status} from the FAA token endpoint{suffix}",
            status=status,
        )

    def _build_token(self, payload: bytes, sent_at: datetime) -> AccessToken:
        try:
            data = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise NmsProtocolError(
                f"the FAA token endpoint returned something that is not JSON: {exc}"
            ) from None
        if not isinstance(data, dict):
            raise NmsProtocolError(
                "the FAA token endpoint returned JSON that is not an object"
            )

        value = str(data.get("access_token") or "").strip()
        if not value:
            raise NmsProtocolError(
                "the FAA token response carried no access_token "
                f"(keys: {sorted(k for k in data if 'token' not in k.lower())})"
            )

        status = _as_str(data.get("status"))
        if status is not None and status.lower() not in ("approved", "active", ""):
            # An unapproved application still receives a token from some
            # gateways, and then every API call fails obscurely. Fail here,
            # where the message can say why.
            raise NmsAuthError(
                f"the FAA reports this application's status as {status!r}, "
                "not approved"
            )

        seconds = _as_int(data.get("expires_in"))
        ttl = timedelta(seconds=seconds) if seconds and seconds > 0 else CONSERVATIVE_TTL

        response = TokenResponse(
            client_id=_as_str(data.get("client_id")),
            organization=_as_str(data.get("organization_name")),
            developer_email=_as_str(data.get("developer.email")),
            application_name=_as_str(data.get("application_name")),
            api_products=_as_products(data),
            scope=_as_str(data.get("scope")) or "",
            status=status,
            token_type=_as_str(data.get("token_type")),
            issued_at=_as_epoch_millis(data.get("issued_at")),
            expires_in=seconds,
        )

        self._rejected = False
        return AccessToken(
            _value=value,
            expires_at=sent_at + ttl,
            obtained_at=sent_at,
            response=response,
        )

    def mark_rejected(self, rejected: bool = True) -> None:
        """Record that an API call refused the token these credentials produce."""
        self._rejected = rejected
        if rejected:
            self.forget()


# --------------------------------------------------------------------------
# Coercion
#
# The gateway returns numbers as JSON strings ("expires_in": "1799"). That is
# its documented behaviour today and not something to rely on staying that way,
# so every scalar is read through a coercion that accepts either form.
# --------------------------------------------------------------------------


def _as_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _as_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _as_epoch_millis(value: Any) -> datetime | None:
    """``issued_at`` is milliseconds since the epoch, as a string."""
    millis = _as_int(value)
    if millis is None or millis <= 0:
        return None
    try:
        return datetime.fromtimestamp(millis / 1000, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _as_products(data: Mapping[str, Any]) -> tuple[str, ...]:
    """Products come as both a JSON array and a bracketed string. Prefer the array."""
    listed = data.get("api_product_list_json")
    if isinstance(listed, list):
        return tuple(str(item).strip() for item in listed if str(item).strip())
    raw = _as_str(data.get("api_product_list"))
    if not raw:
        return ()
    return tuple(part.strip() for part in raw.strip("[]").split(",") if part.strip())

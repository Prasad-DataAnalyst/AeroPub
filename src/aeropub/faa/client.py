"""The FAA NMS-API client.

Sits on :mod:`aeropub.faa.auth` for bearer tokens and
:mod:`aeropub.faa.config` for where everything lives, and adds the things a
production client needs and a curl example does not: archiving, throttling,
one retry when a token dies early, and correct handling of the initial-load
handover to Google Cloud Storage.

Three behaviours here are not obvious from the FAA's documentation and are the
difference between a client that works and one that works on Tuesdays.

**Redirects are not followed automatically.** ``/notams/il`` hands off to a
signed GCS URL on another host. Python's redirect handler carries the original
``Authorization`` header across that hop on the versions this project supports,
and GCS refuses a request bearing both its own signature and an Authorization
header. The handover is therefore done in two explicit steps, and the second
one is unauthenticated.

**The handover response is never archived.** Everything else fetched is written
to the append-only archive before it is parsed. That response is the exception,
because the URL it carries *is* a credential — a signed, time-limited grant —
and the archive has no delete.

**The bundle may or may not still be compressed.** GCS can transcode a gzipped
object on the way out. The payload is sniffed rather than assumed.
"""

from __future__ import annotations

import gzip
import io
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping

from aeropub.archive import Archive, ArchiveEntry
from aeropub.faa.auth import TokenClient
from aeropub.faa.config import NmsEnvironment, load_environment
from aeropub.faa.errors import (
    NmsAuthError,
    NmsConfigurationError,
    NmsError,
    NmsProtocolError,
    NmsTransportError,
    NmsUnavailableError,
    redact,
)
from aeropub.http import USER_AGENT, HostThrottle
from aeropub.netcheck import opener_for

__all__ = [
    "GZIP_MAGIC",
    "MAX_THROTTLE_WAIT",
    "InitialLoad",
    "NmsClient",
    "NmsResponse",
    "SignedUrl",
    "handover_needs_bearer",
    "parse_signed_url",
]

#: First two bytes of any gzip member (RFC 1952).
GZIP_MAGIC = b"\x1f\x8b"

#: The longest a waiting caller will hold for the host throttle. Past this it
#: raises instead, because a diagnostic that pauses for six hours is not
#: waiting, it is hanging, and the operator cannot tell the difference.
MAX_THROTTLE_WAIT = timedelta(seconds=30)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(moment: datetime, field_name: str) -> str:
    """Format a moment the way the API's date filters expect it."""
    if moment.tzinfo is None:
        raise ValueError(f"{field_name} must be timezone-aware (UTC)")
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True, slots=True)
class NmsResponse:
    """One completed API call."""

    url: str
    status: int
    headers: Mapping[str, str]
    body: bytes
    archived: ArchiveEntry | None = None
    duration_ms: int | None = None

    @property
    def content_type(self) -> str:
        for key, value in self.headers.items():
            if key.lower() == "content-type":
                return value
        return ""

    def text(self, encoding: str = "utf-8") -> str:
        return self.body.decode(encoding, "replace")

    def json(self) -> Any:
        try:
            return json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise NmsProtocolError(
                f"expected JSON from {self.url} but could not decode it: {exc}"
            ) from None


@dataclass(frozen=True, slots=True)
class SignedUrl:
    """A time-limited GCS grant. Held in memory only, never archived or logged."""

    url: str
    issued_at: datetime | None = None
    expires_at: datetime | None = None

    @property
    def masked(self) -> str:
        """The URL with its signature removed — safe to print or store."""
        parts = urllib.parse.urlsplit(self.url)
        query = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
        cleaned = [
            (k, "[redacted]" if k.lower().endswith("signature") else v) for k, v in query
        ]
        return urllib.parse.urlunsplit(
            (parts.scheme, parts.netloc, parts.path, urllib.parse.urlencode(cleaned), "")
        )

    def seconds_remaining(self, now: datetime | None = None) -> int | None:
        if self.expires_at is None:
            return None
        return int((self.expires_at - (now or _utcnow())).total_seconds())

    def is_expired(self, now: datetime | None = None) -> bool:
        remaining = self.seconds_remaining(now)
        return remaining is not None and remaining <= 0


def handover_needs_bearer(url: str, *, host: str) -> bool:
    """Whether the initial-load handover must carry the bearer token.

    The FAA changed this and the two answers are opposite, so it is decided
    from the URL itself rather than from configuration — a setting would be one
    more thing to get wrong on the day they change it back.

    Two rules, in this order, and the order matters:

    **A signed URL never gets the bearer, wherever it lives.** A Google V4
    signature signs the ``host`` header and nothing else, so a bearer alongside
    it is two credentials at once and is rejected. The signature is the
    stronger signal than the hostname, because signed storage can sit behind a
    custom domain — including, as the conformance harness demonstrates, the
    same host as the API.

    **Otherwise, the bearer travels only to the FAA's own host.** A relative
    ``/nmsapi/v1/content/{token}``, or an absolute URL on the configured host,
    is the content endpoint and requires it. Anything else is a third party,
    and our token has no business going there — an off-host handover we do not
    recognise is far more likely to be a redirect we should not have followed
    than a place to present credentials.
    """
    if not url.strip():
        return False
    parts = urllib.parse.urlsplit(url)
    if "x-goog-signature" in parts.query.lower():
        return False
    if not parts.netloc:
        return True  # relative — necessarily on the API host
    return parts.netloc.lower() == urllib.parse.urlsplit(host).netloc.lower()


def parse_signed_url(url: str) -> SignedUrl:
    """Read the validity window out of a GCS V4 signed URL.

    ``X-Goog-Date`` and ``X-Goog-Expires`` are part of the signed query string,
    so the deadline can be known before the request is sent. Worth doing: an
    expired signed URL comes back as a 403 with an XML body that says nothing
    about time, and the resulting bug report is "the FAA is refusing us".
    """
    query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
    issued = None
    raw_date = query.get("X-Goog-Date")
    if raw_date:
        try:
            issued = datetime.strptime(raw_date, "%Y%m%dT%H%M%SZ").replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            issued = None
    expires = None
    raw_expires = query.get("X-Goog-Expires")
    if issued is not None and raw_expires and raw_expires.isdigit():
        expires = issued + timedelta(seconds=int(raw_expires))
    return SignedUrl(url=url, issued_at=issued, expires_at=expires)


@dataclass
class InitialLoad:
    """A downloaded initial-load bundle, archived and ready to read."""

    entry: ArchiveEntry
    """The archived artefact — the exact bytes the FAA served."""

    payload: bytes = field(repr=False)
    """Those bytes, as received. Still gzipped unless GCS transcoded them."""

    signed_url: SignedUrl | None = None
    classification: str | None = None

    @property
    def compressed(self) -> bool:
        return self.payload[:2] == GZIP_MAGIC

    def open(self) -> io.BufferedIOBase:
        """A binary stream of the AIXM, decompressing if needed.

        Streamed rather than returned whole: a full domestic load is tens of
        thousands of messages, and the reader in :mod:`aeropub.faa.aixm` pulls
        from this incrementally so the file never has to fit in memory twice.
        """
        raw = io.BytesIO(self.payload)
        if self.compressed:
            return gzip.GzipFile(fileobj=raw, mode="rb")
        return raw

    def read_text(self, encoding: str = "utf-8") -> str:
        with self.open() as stream:
            return stream.read().decode(encoding, "replace")


class NmsClient:
    """Calls the FAA NMS-API. One instance per environment."""

    #: Version of this connector, recorded in every citation it produces.
    PARSER_ID = "faa-nms"
    PARSER_VERSION = "0.1.0"

    def __init__(
        self,
        environment: NmsEnvironment | None = None,
        *,
        tokens: TokenClient | None = None,
        archive: Archive | None = None,
        throttle: HostThrottle | None = None,
        environ: Mapping[str, str] | None = None,
        timeout: int = 120,
        opener: Callable[..., Any] | None = None,
        clock: Callable[[], datetime] = _utcnow,
        wait_for_throttle: bool = False,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.environment = environment or load_environment(environ=environ)
        self.tokens = tokens or TokenClient(self.environment, environ=environ)
        self.archive = archive
        self.throttle = throttle or HostThrottle()
        self.timeout = timeout
        self._clock = clock
        # The host gap protects a State's estate from a scheduler running many
        # sources; refusing and rescheduling is right there, because sleeping
        # would stall the whole tick. A one-shot diagnostic making four
        # sequential calls to one host is not abusive, and reporting "the FAA
        # is unavailable" because our own politeness gap has not elapsed is a
        # false alarm about somebody else's service. So it waits instead.
        self.wait_for_throttle = wait_for_throttle
        self._sleep = sleep
        # No redirect handler: the initial-load handover crosses hosts carrying
        # an Authorization header, and that hop must be made deliberately.
        # opener_for keeps CA handling identical between the token client and
        # this one, and honours an explicitly configured bundle for the
        # corporate proxy whose CA is on disk but in no variable the platform
        # pre-sets. ProxyHandler still comes from build_opener's defaults, so
        # HTTPS_PROXY is honoured without being restated here.
        self._opener = opener or opener_for(extra_handlers=(_NoRedirect(),), environ=environ)

    # -- source identity -------------------------------------------------

    @property
    def source_id(self) -> str:
        """Stable id for archiving and citation, naming the environment.

        The environment is part of the identity on purpose: a NOTAM read from
        staging is not evidence about the real world, and a citation that
        cannot tell you which one it came from is not a citation.
        """
        return f"FAA-NMS-{self.environment.name.upper()}"

    # -- transport -------------------------------------------------------

    def request(
        self,
        endpoint: str,
        *,
        params: Mapping[str, Any] | None = None,
        path_params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        archive: bool = True,
        _retried: bool = False,
    ) -> NmsResponse:
        """Call one configured endpoint and return the response.

        Retries exactly once, and only on a 401, because a token can be revoked
        between our expiry check and the gateway reading it. Everything else is
        raised: waiting is the scheduler's job, and a client that sleeps inside
        a request hides a failing source behind a slow one.
        """
        spec = self.environment.endpoint(endpoint)
        url = f"{self.environment.base}{spec.format(**(path_params or {}))}"
        query = _clean_params(params)
        if query:
            url = f"{url}?{urllib.parse.urlencode(query)}"

        request_headers: dict[str, str] = {
            "User-Agent": USER_AGENT,
            "Accept": "*/*",
        }
        request_headers.update(spec.headers)
        if headers:
            request_headers.update(headers)
        request_headers.update(self.tokens.header())

        now = self._clock()
        if not self.throttle.may_request(url, now=now):
            remaining = self.throttle.ready_at(url, now=now) - now
            if self.wait_for_throttle and remaining <= MAX_THROTTLE_WAIT:
                self._sleep(max(remaining.total_seconds(), 0))
                now = self._clock()
            else:
                wait = int(remaining.total_seconds())
                raise NmsUnavailableError(
                    f"holding off {self.environment.host} for another {wait}s",
                    retry_after=wait,
                )
        self.throttle.record_request(url, at=now)

        started = time.monotonic()
        try:
            with self._opener(
                urllib.request.Request(url, headers=request_headers), timeout=self.timeout
            ) as response:
                body = response.read()
                status = getattr(response, "status", 200) or 200
                response_headers = dict(response.headers.items())
        except urllib.error.HTTPError as exc:
            response_headers = dict(exc.headers.items()) if exc.headers else {}
            if exc.code in (301, 302, 303, 307, 308):
                # Not an error — our opener refuses to follow redirects, so a
                # handover arrives here. Hand it back for the caller to act on.
                return NmsResponse(
                    url=url,
                    status=exc.code,
                    headers=response_headers,
                    body=b"",
                    duration_ms=int((time.monotonic() - started) * 1000),
                )
            detail = _safe_detail(exc)
            if exc.code == 401 and not _retried:
                self.tokens.forget()
                return self.request(
                    endpoint,
                    params=params,
                    path_params=path_params,
                    headers=headers,
                    archive=archive,
                    _retried=True,
                )
            self._raise_for_status(exc.code, url, detail, response_headers)
            raise  # pragma: no cover - _raise_for_status always raises
        except urllib.error.URLError as exc:
            raise NmsTransportError(f"could not reach {url}: {exc.reason}") from None
        except TimeoutError:
            raise NmsTransportError(
                f"timed out after {self.timeout}s calling {url}"
            ) from None

        elapsed = int((time.monotonic() - started) * 1000)
        if not 200 <= status < 300:
            self._raise_for_status(status, url, "", response_headers)

        self.throttle.clear(url)

        entry = None
        if archive and self.archive is not None and body:
            entry = self.archive.put(
                body,
                source_id=self.source_id,
                url=url,
                retrieved_at=now,
                http_status=status,
                content_type=response_headers.get("Content-Type"),
            )

        return NmsResponse(
            url=url,
            status=status,
            headers=response_headers,
            body=body,
            archived=entry,
            duration_ms=elapsed,
        )

    def _raise_for_status(
        self, status: int, url: str, detail: str, headers: Mapping[str, str]
    ) -> None:
        suffix = f" — {detail}" if detail else ""
        if status in (401, 403):
            self.tokens.mark_rejected()
            raise NmsAuthError(
                f"the FAA refused {url} (HTTP {status}){suffix}. The token was "
                "accepted or refused by the gateway, not by us — check the key "
                "is authorised for this environment and product.",
                status=status,
            )
        if status == 404:
            raise NmsConfigurationError(
                f"{url} does not exist (HTTP 404){suffix}. If the FAA has moved "
                "this operation, correct its path in the overlay file named by "
                "AEROPUB_FAA_NMS_CONFIG rather than editing code.",
                status=status,
            )
        if status == 429 or 500 <= status < 600:
            raw = headers.get("Retry-After") or ""
            retry_after = int(raw) if str(raw).strip().isdigit() else None
            raise NmsUnavailableError(
                f"the FAA is not serving {url} (HTTP {status}){suffix}",
                status=status,
                retry_after=retry_after,
            )
        raise NmsProtocolError(f"unexpected HTTP {status} from {url}{suffix}", status=status)

    # -- operations ------------------------------------------------------

    def ping(self) -> NmsResponse:
        """Prove the whole chain: credentials, token, gateway, product access.

        Not archived — it carries no aeronautical content, and an archive of
        liveness checks is noise in a store meant to answer evidential
        questions.
        """
        return self.request("ping", archive=False)

    def location_series(self, *, last_updated: datetime | None = None) -> NmsResponse:
        """The location series list, whole or changed since ``last_updated``."""
        params = {}
        if last_updated is not None:
            params["lastUpdatedDate"] = _timestamp(last_updated, "last_updated")
        return self.request("location_series", params=params)

    def notams(
        self,
        *,
        nms_id: str | None = None,
        location: str | None = None,
        notam_number: str | None = None,
        latitude: float | None = None,
        longitude: float | None = None,
        radius: float | None = None,
        last_updated: datetime | None = None,
        response_format: str | None = None,
    ) -> NmsResponse:
        """Query NOTAM by exactly one primary filter.

        The API defines four ways to select: by NMS id, by location (optionally
        narrowed to one NOTAM number), by a circle, or by change time. Mixing
        them produces a server-side error that does not say which filter it
        disliked, so the combination is checked here where the message can.

        ``last_updated`` is allowed alongside a primary filter, because "what
        changed at this aerodrome since I last asked" is the query incremental
        collection is actually built on.
        """
        families = {
            "nms_id": nms_id is not None,
            "location": location is not None,
            "geospatial": any(v is not None for v in (latitude, longitude, radius)),
        }
        chosen = [name for name, present in families.items() if present]

        if len(chosen) > 1:
            raise NmsConfigurationError(
                "NOTAM filters are mutually exclusive; got "
                f"{', '.join(sorted(chosen))}. Pick one of nms_id, location, or "
                "latitude/longitude/radius."
            )
        # Checked before the "no filter at all" case, so someone who supplied
        # a NOTAM number is told what is missing rather than that they supplied
        # nothing.
        if notam_number is not None and location is None:
            raise NmsConfigurationError(
                "notam_number identifies a NOTAM within a location; supply "
                "location as well."
            )
        if not chosen and last_updated is None:
            raise NmsConfigurationError(
                "a NOTAM query needs a filter: nms_id, location, "
                "latitude/longitude/radius, or last_updated. An unfiltered "
                "query is the initial load — use fetch_initial_load()."
            )
        if families["geospatial"] and any(
            v is None for v in (latitude, longitude, radius)
        ):
            raise NmsConfigurationError(
                "a geospatial NOTAM query needs all of latitude, longitude and "
                "radius — a circle is not defined by two of the three."
            )

        params: dict[str, Any] = {}
        if nms_id is not None:
            params["nmsId"] = nms_id
        if location is not None:
            params["location"] = location.strip().upper()
        if notam_number is not None:
            params["notamNumber"] = notam_number
        if latitude is not None:
            params["latitude"] = latitude
            params["longitude"] = longitude
            params["radius"] = radius
        if last_updated is not None:
            params["lastUpdatedDate"] = _timestamp(last_updated, "last_updated")

        headers = {"nmsResponseFormat": response_format} if response_format else None
        return self.request("notams", params=params, headers=headers)

    def notam_checklist(
        self,
        *,
        accountability: str | None = None,
        classification: str | None = None,
        location: str | None = None,
    ) -> NmsResponse:
        """The checklist of current NOTAM numbers.

        This is the reconciliation source: it answers "which messages does the
        FAA consider live", which is how a gap in what we received becomes
        visible instead of looking like quiet airspace.
        """
        params = {
            "accountability": accountability,
            "classification": classification,
            "location": location.strip().upper() if location else None,
        }
        return self.request("notam_checklist", params=params)

    # -- initial load ----------------------------------------------------

    def initial_load_handover(self, classification: str | None = None) -> SignedUrl:
        """Ask for the bundle and return the signed URL, without archiving it.

        Two response shapes are accepted, because the FAA's own documentation
        shows both: a redirect whose ``Location`` is the signed URL, and a 200
        carrying ``{"status": "Success", "data": {"url": ...}}``. Handling only
        the one that happened to be documented last would make this connector
        fail on a change that is not even a breaking one.
        """
        if classification is None:
            endpoint, path_params = "initial_load", {}
        else:
            endpoint = "initial_load_by_classification"
            path_params = {"classification": classification.strip().upper()}

        # archive=False: the payload of this call is a credential.
        response = self.request(endpoint, path_params=path_params, archive=False)

        if response.status in (301, 302, 303, 307, 308):
            location = next(
                (v for k, v in response.headers.items() if k.lower() == "location"), ""
            )
            if not location:
                raise NmsProtocolError(
                    f"the FAA redirected the initial load from {response.url} "
                    "without a Location header"
                )
            return parse_signed_url(location)

        payload = response.json()
        if not isinstance(payload, dict):
            raise NmsProtocolError(
                f"the initial-load handover at {response.url} returned JSON that "
                "is not an object"
            )
        status = str(payload.get("status", "")).strip()
        if status and status.lower() not in ("success", "ok"):
            raise NmsProtocolError(
                f"the FAA reported the initial load as {status!r} rather than Success"
            )
        url = ""
        data = payload.get("data")
        if isinstance(data, dict):
            url = str(data.get("url") or "")
        if not url:
            # Never echo the payload: if a URL is in there in a shape we did not
            # expect, printing it discloses the signature we are refusing to log.
            raise NmsProtocolError(
                f"the initial-load handover at {response.url} carried no "
                "data.url; the response shape has changed"
            )
        return parse_signed_url(url)

    def download_signed(self, signed: SignedUrl) -> bytes:
        """Fetch the initial-load bundle from wherever the handover pointed.

        Whether this carries the bearer token depends on where it points, and
        the two answers are opposite:

        The FAA used to hand back a Google Cloud Storage V4 signed URL, and the
        correct behaviour was to send **no** ``Authorization`` header — GCS
        signs the ``host`` header and nothing else, so a bearer alongside the
        signature is two credentials at once and is rejected. It would also
        disclose our token to a third party.

        They now hand back ``/nmsapi/v1/content/{token}`` on their own host,
        which proxies the data and **requires** the same bearer as every other
        call. Both shapes remain possible, so
        :func:`handover_needs_bearer` decides from the URL rather than from a
        setting somebody would have to remember to change.
        """
        if signed.is_expired(self._clock()):
            raise NmsError(
                "the FAA's signed URL expired before it could be used "
                f"(valid until {signed.expires_at:%H:%M:%SZ}). These last about "
                "five minutes; request the handover immediately before the "
                "download rather than caching it."
            )

        target = signed.url
        headers = {"User-Agent": USER_AGENT, "Accept": "*/*"}
        if handover_needs_bearer(target, host=self.environment.host):
            if not urllib.parse.urlsplit(target).netloc:
                target = f"{self.environment.host.rstrip('/')}{target}"
            headers.update(self.tokens.header())

        request = urllib.request.Request(target, headers=headers)
        try:
            with self._opener(request, timeout=self.timeout) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            remaining = signed.seconds_remaining(self._clock())
            hint = (
                " The signed URL had already expired."
                if remaining is not None and remaining <= 0
                else ""
            )
            if exc.code == 401:
                hint += (
                    " A 401 here means the content endpoint did not accept the "
                    "bearer token. The FAA's content endpoint requires it; a "
                    "Google signed URL refuses it. Check which shape the "
                    "handover returned."
                )
            raise NmsError(
                f"the initial-load download was refused (HTTP {exc.code}).{hint} "
                f"URL: {signed.masked}",
                status=exc.code,
            ) from None
        except urllib.error.URLError as exc:
            raise NmsTransportError(
                f"could not reach the initial-load storage service: {exc.reason}"
            ) from None
        except TimeoutError:
            raise NmsTransportError(
                f"timed out after {self.timeout}s downloading the initial load"
            ) from None

    def fetch_initial_load(self, classification: str | None = None) -> InitialLoad:
        """Handover, download and archive one initial-load bundle.

        The download is archived as served — compressed, byte for byte — so a
        citation resolves to what the FAA actually sent rather than to our
        decompression of it.
        """
        # Checked before anything is requested. Discovering there is nowhere to
        # put the bundle after pulling tens of megabytes through a five-minute
        # signed URL wastes the download and the window with it.
        if self.archive is None:
            raise NmsConfigurationError(
                "fetch_initial_load needs an archive: the bundle is evidence, "
                "and evidence that is not stored cannot be cited later."
            )

        signed = self.initial_load_handover(classification)
        payload = self.download_signed(signed)
        if not payload:
            raise NmsProtocolError("the initial-load download was empty")

        now = self._clock()
        entry = self.archive.put(
            payload,
            source_id=self.source_id,
            url=signed.masked,
            retrieved_at=now,
            http_status=200,
            content_type="application/gzip" if payload[:2] == GZIP_MAGIC else None,
        )
        return InitialLoad(
            entry=entry,
            payload=payload,
            signed_url=signed,
            classification=classification,
        )


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuses to follow redirects, so cross-host handovers stay deliberate."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


def _clean_params(params: Mapping[str, Any] | None) -> dict[str, str]:
    """Drop unset filters and render the rest as strings."""
    if not params:
        return {}
    return {k: str(v) for k, v in params.items() if v is not None and str(v) != ""}


def _safe_detail(exc: urllib.error.HTTPError) -> str:
    try:
        return redact(exc.read().decode("utf-8", "replace")[:400]).strip()
    except Exception:  # pragma: no cover - a detail is a nicety, not a contract
        return ""

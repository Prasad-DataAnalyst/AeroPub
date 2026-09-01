"""Source registry and the live status board.

Every source the platform watches — a State's eAIP index, an amendment listing,
a NOTAM API — is registered here with what is needed to reach it and what has
happened when we tried. The registry answers the operator's question:

    *Is the system actually checking, and is anything failing quietly?*

Two things this module deliberately does **not** do:

**It never holds a secret.** A source that needs an API key carries a
:class:`CredentialRef` — the *name* of an environment variable, a label, and
enough of a hint to recognise which key it is. The key itself lives in the
environment or a secret store and is read on use. Nothing here can be logged,
committed, or rendered into a status board and leak a credential.

**It never guesses a URL.** Sources are registered explicitly. When a State
moves its eAIP the old address is kept in :attr:`Source.url_history`, because
"where did this used to live" is a real question during an investigation.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Iterable, Iterator

__all__ = [
    "CheckOutcome",
    "CredentialRef",
    "CredentialStatus",
    "DetectionTier",
    "Freshness",
    "Redistribution",
    "Source",
    "SourceFormat",
    "SourceKind",
    "SourceRegistry",
    "SourceState",
    "StatusRow",
    "UrlChange",
    "mask_secret",
    "render_board",
]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------
# Credentials
# --------------------------------------------------------------------------


class CredentialStatus(str, Enum):
    """What we know about a key, from the last time we tried to use it."""

    CONFIGURED = "configured"
    """Present and last used successfully."""

    UNVERIFIED = "unverified"
    """Present but never yet used successfully. A new key starts here."""

    MISSING = "missing"
    """The registry expects this key and the environment does not have it."""

    INVALID = "invalid"
    """Present, but the authority rejected it — 401 or 403."""

    EXPIRED = "expired"
    """Past its known expiry date."""


def mask_secret(secret: str) -> str:
    """A recognition hint, never enough to reconstruct the key.

    Short values are masked entirely rather than partially revealed, since four
    characters of an eight-character secret is a meaningful disclosure.
    """
    if len(secret) < 12:
        return "****"
    return f"****{secret[-4:]}"


@dataclass(frozen=True, slots=True)
class CredentialRef:
    """A reference to a secret. Never the secret itself."""

    env_var: str
    """Environment variable holding the key, e.g. ``"FAA_NOTAM_API_KEY"``."""

    label: str
    """What a human calls it, e.g. ``"FAA NOTAM API"``."""

    added_at: datetime = field(default_factory=_utcnow)
    last_verified_at: datetime | None = None
    expires_at: datetime | None = None
    hint: str | None = None
    """Masked tail of the key, for recognising which one is installed."""

    def __post_init__(self) -> None:
        if not self.env_var.strip():
            raise ValueError("CredentialRef.env_var must be a non-empty string")
        if not self.label.strip():
            raise ValueError("CredentialRef.label must be a non-empty string")

    def is_present(self, environ: dict[str, str] | None = None) -> bool:
        env = os.environ if environ is None else environ
        return bool(env.get(self.env_var, "").strip())

    def resolve(self, environ: dict[str, str] | None = None) -> str | None:
        """The secret, read at point of use. Never stored, never cached."""
        env = os.environ if environ is None else environ
        value = env.get(self.env_var, "").strip()
        return value or None

    def status(
        self,
        environ: dict[str, str] | None = None,
        *,
        now: datetime | None = None,
        rejected: bool = False,
    ) -> CredentialStatus:
        if not self.is_present(environ):
            return CredentialStatus.MISSING
        if rejected:
            return CredentialStatus.INVALID
        moment = now or _utcnow()
        if self.expires_at is not None and moment >= self.expires_at:
            return CredentialStatus.EXPIRED
        if self.last_verified_at is None:
            return CredentialStatus.UNVERIFIED
        return CredentialStatus.CONFIGURED

    def with_hint_from(self, secret: str) -> "CredentialRef":
        """A copy carrying a masked hint derived from ``secret``."""
        return replace(self, hint=mask_secret(secret))

    def verified(self, at: datetime | None = None) -> "CredentialRef":
        return replace(self, last_verified_at=at or _utcnow())


# --------------------------------------------------------------------------
# Source description
# --------------------------------------------------------------------------


class SourceKind(str, Enum):
    AIP = "aip"
    AMDT_INDEX = "amdt_index"
    SUP_INDEX = "sup_index"
    AIC_INDEX = "aic_index"
    NOTAM = "notam"
    CHARTS = "charts"
    OBSTACLES = "obstacles"
    REGISTRY = "registry"


class SourceFormat(str, Enum):
    EAIP_XML = "eaip_xml"
    EAIP_HTML = "eaip_html"
    PDF = "pdf"
    SCANNED_PDF = "scanned_pdf"
    REST_API = "rest_api"
    FEED = "feed"
    STREAM = "stream"


class DetectionTier(int, Enum):
    """How a source is watched, and therefore how often. See plan section 6."""

    PUSH = 1
    FAST_POLL = 2
    ADAPTIVE_POLL = 3
    SCHEDULED = 4

    @property
    def default_interval(self) -> timedelta:
        return {
            DetectionTier.PUSH: timedelta(minutes=1),
            DetectionTier.FAST_POLL: timedelta(minutes=5),
            DetectionTier.ADAPTIVE_POLL: timedelta(minutes=15),
            DetectionTier.SCHEDULED: timedelta(hours=6),
        }[self]


class Redistribution(str, Enum):
    """Whether source content may be republished. Enforced at render time."""

    PERMITTED = "permitted"
    PROHIBITED = "prohibited"
    CONDITIONAL = "conditional"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class UrlChange:
    """A State moved its publication. Kept, because it gets asked about later."""

    changed_at: datetime
    old_url: str
    new_url: str
    note: str = ""


@dataclass(frozen=True, slots=True)
class Source:
    """One watched endpoint, and everything needed to reach it."""

    source_id: str
    authority: str
    """ICAO State code or authority, e.g. ``"US"``, ``"QA"``, ``"FAA"``."""

    name: str
    kind: SourceKind
    url: str
    fmt: SourceFormat
    tier: DetectionTier
    credential: CredentialRef | None = None
    redistribution: Redistribution = Redistribution.UNKNOWN
    enabled: bool = True
    interval: timedelta | None = None
    """Overrides the tier default when a source needs its own cadence."""

    url_history: tuple[UrlChange, ...] = ()
    verified_at: datetime | None = None
    """When a human last confirmed this URL serves what we think it does.

    A registered URL is a claim until someone checks it. An unverified source
    is not an error, but it is not evidence either, and the board says so.
    """

    note: str = ""

    def __post_init__(self) -> None:
        for name in ("source_id", "authority", "name", "url"):
            if not getattr(self, name).strip():
                raise ValueError(f"Source.{name} must be a non-empty string")
        if not self.url.startswith(("http://", "https://")):
            raise ValueError(f"Source.url must be an http(s) URL, got {self.url!r}")
        if self.interval is not None and self.interval <= timedelta(0):
            raise ValueError("Source.interval must be positive")

    @property
    def is_verified(self) -> bool:
        return self.verified_at is not None

    def verified(self, at: datetime | None = None) -> "Source":
        """A copy marked as confirmed by a human at ``at``."""
        return replace(self, verified_at=at or _utcnow())

    @property
    def check_interval(self) -> timedelta:
        return self.interval or self.tier.default_interval

    def moved_to(self, new_url: str, *, note: str = "", at: datetime | None = None) -> "Source":
        """A copy pointing at ``new_url``, with the old address recorded."""
        if new_url == self.url:
            return self
        change = UrlChange(
            changed_at=at or _utcnow(), old_url=self.url, new_url=new_url, note=note
        )
        return replace(self, url=new_url, url_history=self.url_history + (change,))


# --------------------------------------------------------------------------
# Observed state
# --------------------------------------------------------------------------


class SourceState(str, Enum):
    """Where a source sits in the pipeline. Plan section 6."""

    WATCHING = "watching"
    CHANGE_DETECTED = "change_detected"
    FETCHED = "fetched"
    PARSED = "parsed"
    DIFFED = "diffed"
    ASSESSED = "assessed"
    IN_REVIEW = "in_review"
    PUBLISHED = "published"
    # Exceptions — these matter as much as the happy path.
    FETCH_FAILED = "fetch_failed"
    PARSE_FAILED = "parse_failed"
    BLOCKED = "blocked"
    STALE = "stale"
    OVERDUE = "overdue"
    DISABLED = "disabled"
    CREDENTIAL_MISSING = "credential_missing"

    @property
    def is_exception(self) -> bool:
        return self in _EXCEPTION_STATES


_EXCEPTION_STATES = frozenset(
    {
        SourceState.FETCH_FAILED,
        SourceState.PARSE_FAILED,
        SourceState.BLOCKED,
        SourceState.STALE,
        SourceState.OVERDUE,
        SourceState.CREDENTIAL_MISSING,
    }
)


class Freshness(str, Enum):
    """Whether checking is actually happening on schedule."""

    ON_TIME = "on_time"
    LATE = "late"
    STALE = "stale"
    NEVER_CHECKED = "never_checked"


@dataclass(frozen=True, slots=True)
class CheckOutcome:
    """The result of one attempt to check a source."""

    source_id: str
    checked_at: datetime
    state: SourceState
    changed: bool = False
    http_status: int | None = None
    content_hash: str | None = None
    duration_ms: int | None = None
    error: str | None = None

    def __post_init__(self) -> None:
        if self.checked_at.tzinfo is None:
            raise ValueError("CheckOutcome.checked_at must be timezone-aware (UTC)")


@dataclass(frozen=True, slots=True)
class StatusRow:
    """One line of the status board — what the operator actually looks at."""

    source: Source
    state: SourceState
    freshness: Freshness
    last_checked_at: datetime | None
    next_due_at: datetime | None
    last_change_at: datetime | None
    consecutive_failures: int
    credential_status: CredentialStatus | None
    last_error: str | None

    @property
    def is_unverified(self) -> bool:
        """Registered but never confirmed against the authority's own site."""
        return not self.source.is_verified

    @property
    def needs_attention(self) -> bool:
        return (
            self.state.is_exception
            or self.freshness is Freshness.STALE
            or self.consecutive_failures >= 3
            or self.credential_status
            in (
                CredentialStatus.MISSING,
                CredentialStatus.INVALID,
                CredentialStatus.EXPIRED,
            )
        )


# --------------------------------------------------------------------------
# Registry
# --------------------------------------------------------------------------

#: Overdue by more than this multiple of its interval and a source is stale.
STALE_MULTIPLE = 3


class SourceRegistry:
    """Sources, their check history, and the live status board over both."""

    def __init__(self, sources: Iterable[Source] | None = None) -> None:
        self._sources: dict[str, Source] = {}
        self._checks: dict[str, list[CheckOutcome]] = {}
        self._rejected: set[str] = set()
        for source in sources or ():
            self.add(source)

    def __len__(self) -> int:
        return len(self._sources)

    def __iter__(self) -> Iterator[Source]:
        return iter(self._sources.values())

    def __contains__(self, source_id: object) -> bool:
        return source_id in self._sources

    # -- configuration ---------------------------------------------------

    def add(self, source: Source) -> None:
        if source.source_id in self._sources:
            raise ValueError(f"source {source.source_id!r} is already registered")
        self._sources[source.source_id] = source
        self._checks.setdefault(source.source_id, [])

    def get(self, source_id: str) -> Source:
        try:
            return self._sources[source_id]
        except KeyError:
            raise KeyError(f"no source registered as {source_id!r}") from None

    def update(self, source: Source) -> None:
        """Replace a registered source, keeping its check history."""
        if source.source_id not in self._sources:
            raise KeyError(f"no source registered as {source.source_id!r}")
        self._sources[source.source_id] = source

    def move(self, source_id: str, new_url: str, *, note: str = "") -> Source:
        """Point a source at a new address, recording where it used to be."""
        moved = self.get(source_id).moved_to(new_url, note=note)
        self._sources[source_id] = moved
        return moved

    def set_credential(self, source_id: str, credential: CredentialRef | None) -> Source:
        """Attach or replace the credential reference for a source."""
        updated = replace(self.get(source_id), credential=credential)
        self._sources[source_id] = updated
        return updated

    def set_enabled(self, source_id: str, enabled: bool) -> Source:
        updated = replace(self.get(source_id), enabled=enabled)
        self._sources[source_id] = updated
        return updated

    # -- observation -----------------------------------------------------

    def record_check(self, outcome: CheckOutcome) -> None:
        if outcome.source_id not in self._sources:
            raise KeyError(f"no source registered as {outcome.source_id!r}")
        self._checks[outcome.source_id].append(outcome)

    def checks(self, source_id: str) -> list[CheckOutcome]:
        self.get(source_id)
        return list(self._checks[source_id])

    def mark_credential_rejected(self, source_id: str, rejected: bool = True) -> None:
        """Record that the authority rejected this source's key."""
        self.get(source_id)
        self._rejected.discard(source_id)
        if rejected:
            self._rejected.add(source_id)

    # -- the board -------------------------------------------------------

    def status(
        self,
        source_id: str,
        *,
        now: datetime | None = None,
        environ: dict[str, str] | None = None,
    ) -> StatusRow:
        source = self.get(source_id)
        moment = now or _utcnow()
        history = self._checks[source_id]
        last = history[-1] if history else None

        changes = [c for c in history if c.changed]
        last_change_at = changes[-1].checked_at if changes else None

        failures = 0
        for outcome in reversed(history):
            if outcome.state.is_exception:
                failures += 1
            else:
                break

        credential_status = (
            source.credential.status(
                environ, now=moment, rejected=source_id in self._rejected
            )
            if source.credential
            else None
        )

        if last is None:
            freshness = Freshness.NEVER_CHECKED
            next_due = None
        else:
            interval = source.check_interval
            next_due = last.checked_at + interval
            overdue_by = moment - next_due
            if overdue_by <= timedelta(0):
                freshness = Freshness.ON_TIME
            elif overdue_by <= interval * (STALE_MULTIPLE - 1):
                freshness = Freshness.LATE
            else:
                freshness = Freshness.STALE

        state = self._derive_state(source, last, freshness, credential_status)

        return StatusRow(
            source=source,
            state=state,
            freshness=freshness,
            last_checked_at=last.checked_at if last else None,
            next_due_at=next_due,
            last_change_at=last_change_at,
            consecutive_failures=failures,
            credential_status=credential_status,
            last_error=last.error if last else None,
        )

    @staticmethod
    def _derive_state(
        source: Source,
        last: CheckOutcome | None,
        freshness: Freshness,
        credential_status: CredentialStatus | None,
    ) -> SourceState:
        # Ordered by what an operator must act on first. A missing credential
        # outranks staleness because it explains it.
        if not source.enabled:
            return SourceState.DISABLED
        if credential_status in (
            CredentialStatus.MISSING,
            CredentialStatus.INVALID,
            CredentialStatus.EXPIRED,
        ):
            return SourceState.CREDENTIAL_MISSING
        if last is None:
            return SourceState.WATCHING
        if last.state.is_exception:
            return last.state
        if freshness is Freshness.STALE:
            return SourceState.STALE
        return last.state

    def board(
        self,
        *,
        now: datetime | None = None,
        environ: dict[str, str] | None = None,
    ) -> list[StatusRow]:
        """Every source, problems first, then by authority and name."""
        rows = [
            self.status(sid, now=now, environ=environ) for sid in self._sources
        ]
        rows.sort(
            key=lambda r: (
                not r.needs_attention,
                r.source.authority,
                r.source.name,
            )
        )
        return rows


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


def _ago(moment: datetime | None, now: datetime) -> str:
    if moment is None:
        return "never"
    seconds = int((now - moment).total_seconds())
    if seconds < 0:
        return "in the future"
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


def render_board(rows: list[StatusRow], *, now: datetime | None = None) -> str:
    """The status board as text — the operator's answer to 'is it checking?'."""
    moment = now or _utcnow()
    header = (
        f"{'':1} {'AUTHORITY':9} {'SOURCE':26} {'STATE':18} "
        f"{'LAST CHECK':12} {'FRESHNESS':13} {'KEY':12} {'URL':10}"
    )
    lines = [header, "-" * len(header)]

    for row in rows:
        marker = "!" if row.needs_attention else " "
        key = row.credential_status.value if row.credential_status else "-"
        url_state = "unverified" if row.is_unverified else "verified"
        lines.append(
            f"{marker} {row.source.authority:9} {row.source.name[:26]:26} "
            f"{row.state.value:18} {_ago(row.last_checked_at, moment):12} "
            f"{row.freshness.value:13} {key:12} {url_state:10}"
        )
        if row.last_error:
            lines.append(f"{'':12}  error: {row.last_error}")

    attention = sum(1 for r in rows if r.needs_attention)
    lines.append("-" * len(header))
    lines.append(
        f"{len(rows)} sources, {attention} needing attention "
        f"— board generated {moment:%d %b %Y %H:%M:%SZ}"
    )
    return "\n".join(lines)

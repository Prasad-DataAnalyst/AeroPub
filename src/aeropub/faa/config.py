"""FAA NMS-API — connection configuration, held as data rather than code.

The FAA's NOTAM Management System API is the first live, credentialed source
AeroPub connects to, and the one most likely to move. Its hosts are operated by
a contractor, its paths are versioned, and the onboarding pack that describes
them is a text file emailed to each registrant — all three change without
notice and without a deprecation window.

So nothing about the connection is written into the calling code. An
:class:`NmsEnvironment` is a plain record of hosts, paths and required headers;
:data:`ENVIRONMENTS` holds the three the FAA publishes today; and
:meth:`NmsEnvironment.overlay` and :func:`load_environment` let an operator
correct any part of it from a JSON file or an environment variable, with the
service running and without a release.

The one thing configuration deliberately cannot do is supply a credential. Keys
are named here — never carried.

Source: *NMS-API cURL Command Examples and Instructions for connecting*, issued
by the FAA with API registration.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

from aeropub.registry import CredentialRef, CredentialStatus

__all__ = [
    "CONFIG_PATH_VAR",
    "DEFAULT_ENVIRONMENT",
    "ENVIRONMENTS",
    "ENVIRONMENT_VAR",
    "KNOWN_CLASSIFICATIONS",
    "ClientCredentials",
    "NmsEndpoint",
    "NmsEnvironment",
    "load_environment",
]

#: Selects which built-in environment to use, e.g. ``"prod"``.
ENVIRONMENT_VAR = "FAA_NMS_ENVIRONMENT"

#: Path to a JSON file overlaying the built-in configuration. This is the
#: escape hatch: when the FAA moves a host or renames a path, the operator
#: writes the correction here and restarts, rather than waiting for a release.
CONFIG_PATH_VAR = "AEROPUB_FAA_NMS_CONFIG"

DEFAULT_ENVIRONMENT = "prod"

#: NOTAM classifications the FAA used at the time of writing. Not a closed set,
#: and not enforced anywhere — see :meth:`NmsEnvironment.initial_load_url`. An
#: unrecognised classification is passed through to the API, because the API is
#: the authority on what it accepts and a client that rejects a newly-issued
#: classification manufactures a coverage gap out of its own staleness.
#:
#: Note the asymmetry, which is real and catches people: the request path uses
#: the long form (``DOMESTIC``) while the AIXM payload reports the short form
#: (``DOM``) in ``fnse:classification``.
KNOWN_CLASSIFICATIONS: tuple[str, ...] = ("DOMESTIC", "INTERNATIONAL", "MILITARY", "LMIL", "FDC")


@dataclass(frozen=True, slots=True)
class NmsEndpoint:
    """One callable operation: where it lives and what it insists on.

    ``path`` may carry ``{placeholders}``; :meth:`format` fills them. Required
    headers travel with the endpoint rather than being remembered at each call
    site, because forgetting ``nmsResponseFormat`` on ``/notams`` produces an
    error whose message does not mention the header.
    """

    name: str
    path: str
    headers: Mapping[str, str] = field(default_factory=dict)
    note: str = ""

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("NmsEndpoint.name must be a non-empty string")
        if not self.path.startswith("/"):
            raise ValueError(f"NmsEndpoint.path must start with '/', got {self.path!r}")

    def format(self, **params: str) -> str:
        """The path with placeholders substituted."""
        try:
            return self.path.format(**params)
        except KeyError as exc:
            raise ValueError(
                f"endpoint {self.name!r} needs parameter {exc.args[0]!r} "
                f"for path {self.path!r}"
            ) from None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "path": self.path,
            "headers": dict(self.headers),
            "note": self.note,
        }


#: The operations described in the FAA onboarding pack. Paths are relative to
#: ``api_base``; the token endpoint is not, and sits on the bare host.
_DEFAULT_ENDPOINTS: tuple[NmsEndpoint, ...] = (
    NmsEndpoint(
        name="ping",
        path="/v1/ping",
        note="Liveness and credential check. The cheapest call that proves the "
        "whole chain works.",
    ),
    NmsEndpoint(
        name="location_series",
        path="/v1/locationseries",
        note="Accepts lastUpdatedDate for incremental collection.",
    ),
    NmsEndpoint(
        name="notams",
        path="/v1/notams",
        headers={"nmsResponseFormat": "AIXM"},
        note="Filtered NOTAM query. nmsResponseFormat is required, not optional.",
    ),
    NmsEndpoint(
        name="notam_checklist",
        path="/v1/notams/checklist",
        note="The series checklist — which NOTAM numbers the FAA holds as "
        "current. Used to detect messages we never received.",
    ),
    NmsEndpoint(
        name="initial_load",
        path="/v1/notams/il",
        note="Redirects to a short-lived signed URL for a gzipped AIXM bundle "
        "of every active NOTAM.",
    ),
    NmsEndpoint(
        name="initial_load_by_classification",
        path="/v1/notams/il/{classification}",
        note="As initial_load, narrowed to one classification.",
    ),
)


@dataclass(frozen=True, slots=True)
class NmsEnvironment:
    """One deployment of the NMS-API — where it is and how it is spoken to."""

    name: str
    host: str
    """Scheme and authority only, no trailing slash. e.g. ``https://api-nms.aim.faa.gov``."""

    auth_path: str = "/v1/auth/token"
    """Token endpoint, on the bare host — *not* under ``api_base``. This trips
    people up: the OAuth2 endpoint and the API live at different prefixes."""

    api_base: str = "/nmsapi"
    """Prefix every operation hangs from, once the bearer token is held."""

    endpoints: tuple[NmsEndpoint, ...] = _DEFAULT_ENDPOINTS
    description: str = ""
    is_production: bool = False

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("NmsEnvironment.name must be a non-empty string")
        parts = urlsplit(self.host)
        if parts.scheme != "https":
            # A bearer token on a plaintext connection is a disclosed bearer
            # token. There is no test environment worth relaxing this for.
            raise ValueError(
                f"NmsEnvironment.host must be an https URL, got {self.host!r}"
            )
        if not parts.netloc:
            raise ValueError(f"NmsEnvironment.host has no host part: {self.host!r}")
        if parts.path.rstrip("/"):
            raise ValueError(
                f"NmsEnvironment.host must be scheme and host only, got {self.host!r}"
            )
        for attr in ("auth_path", "api_base"):
            value = getattr(self, attr)
            if not value.startswith("/"):
                raise ValueError(f"NmsEnvironment.{attr} must start with '/'")
        names = [e.name for e in self.endpoints]
        if len(names) != len(set(names)):
            raise ValueError(f"duplicate endpoint names in environment {self.name!r}")

    # -- addressing ------------------------------------------------------

    @property
    def base(self) -> str:
        """Where API operations hang from, e.g. ``https://host/nmsapi``."""
        return f"{self.host.rstrip('/')}{self.api_base.rstrip('/')}"

    @property
    def token_url(self) -> str:
        return f"{self.host.rstrip('/')}{self.auth_path}"

    def endpoint(self, name: str) -> NmsEndpoint:
        for candidate in self.endpoints:
            if candidate.name == name:
                return candidate
        raise KeyError(
            f"environment {self.name!r} has no endpoint {name!r}; "
            f"configured: {sorted(e.name for e in self.endpoints)}"
        )

    def url(self, name: str, **params: str) -> str:
        """The full URL for an operation, placeholders filled."""
        return f"{self.base}{self.endpoint(name).format(**params)}"

    def initial_load_url(self, classification: str | None = None) -> str:
        """Initial-load URL, whole feed or one classification.

        No membership check against :data:`KNOWN_CLASSIFICATIONS`: if the FAA
        adds one, refusing to ask for it would look exactly like the FAA not
        publishing it.
        """
        if classification is None:
            return self.url("initial_load")
        return self.url(
            "initial_load_by_classification", classification=classification.strip().upper()
        )

    # -- adaptation ------------------------------------------------------

    def overlay(self, changes: Mapping[str, Any]) -> "NmsEnvironment":
        """A copy with ``changes`` applied — the mechanism for FAA drift.

        Endpoints merge by name: an overlay naming one endpoint corrects that
        endpoint and leaves the rest alone. Replacing the whole list would mean
        an operator fixing one path silently dropped the other five.
        """
        fields: dict[str, Any] = {}
        for key in ("name", "host", "auth_path", "api_base", "description"):
            if key in changes:
                fields[key] = str(changes[key])
        if "is_production" in changes:
            fields["is_production"] = bool(changes["is_production"])

        if "endpoints" in changes:
            merged = {e.name: e for e in self.endpoints}
            for raw in changes["endpoints"]:
                name = str(raw["name"])
                existing = merged.get(name)
                merged[name] = NmsEndpoint(
                    name=name,
                    path=str(raw.get("path", existing.path if existing else "")),
                    headers=dict(
                        raw.get("headers", dict(existing.headers) if existing else {})
                    ),
                    note=str(raw.get("note", existing.note if existing else "")),
                )
            fields["endpoints"] = tuple(merged.values())

        return replace(self, **fields)

    def to_dict(self) -> dict[str, Any]:
        """Serialisable form — what an operator edits to produce an overlay."""
        return {
            "name": self.name,
            "host": self.host,
            "auth_path": self.auth_path,
            "api_base": self.api_base,
            "description": self.description,
            "is_production": self.is_production,
            "endpoints": [e.to_dict() for e in self.endpoints],
        }


#: The three environments the FAA documents. Registration is per-environment:
#: a key issued for staging does not work against production.
ENVIRONMENTS: dict[str, NmsEnvironment] = {
    "fit": NmsEnvironment(
        name="fit",
        host="https://api-fit.cgifederal-aim.com",
        description="FIT — Facility Integration Test. Where the FAA expects "
        "first connection attempts.",
    ),
    "staging": NmsEnvironment(
        name="staging",
        host="https://api-staging.cgifederal-aim.com",
        description="Staging (Pre-Prod).",
    ),
    "prod": NmsEnvironment(
        name="prod",
        host="https://api-nms.aim.faa.gov",
        description="Production. The only environment whose NOTAM are "
        "operationally valid.",
        is_production=True,
    ),
}


def load_environment(
    name: str | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    config_path: Path | str | None = None,
) -> NmsEnvironment:
    """Resolve the environment to use, applying any operator overlay.

    Order of precedence, loosest last so the operator always wins:

    1. the built-in environment named by ``name``, ``FAA_NMS_ENVIRONMENT``,
       or :data:`DEFAULT_ENVIRONMENT`;
    2. an overlay from the JSON file at ``config_path`` or
       ``AEROPUB_FAA_NMS_CONFIG``.

    The overlay file may name a different base environment via ``"base"``, so a
    single file can say "staging, but the host moved".
    """
    env = os.environ if environ is None else environ

    overlay: dict[str, Any] = {}
    path = config_path if config_path is not None else env.get(CONFIG_PATH_VAR, "")
    if path:
        text = Path(path).read_text(encoding="utf-8")
        loaded = json.loads(text)
        if not isinstance(loaded, dict):
            raise ValueError(f"{path}: NMS configuration must be a JSON object")
        overlay = loaded

    selected = name or overlay.get("base") or env.get(ENVIRONMENT_VAR) or DEFAULT_ENVIRONMENT
    key = str(selected).strip().lower()
    try:
        base = ENVIRONMENTS[key]
    except KeyError:
        raise KeyError(
            f"unknown FAA NMS environment {selected!r}; "
            f"known: {sorted(ENVIRONMENTS)}. Set {ENVIRONMENT_VAR}, or describe "
            f"a new one in the file named by {CONFIG_PATH_VAR}."
        ) from None

    return base.overlay(overlay) if overlay else base


# --------------------------------------------------------------------------
# Credentials
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ClientCredentials:
    """The OAuth2 client-credentials pair, by name only.

    The FAA issues these on a spreadsheet during onboarding, where the column
    headed KEY is the client id and the one headed SECRET is the client secret.
    Both are held here as :class:`~aeropub.registry.CredentialRef` — an
    environment variable name and a masked hint, never a value.

    They are separate refs because they fail separately and are fixed
    separately: half a pair installed is the most common onboarding mistake,
    and it produces a 401 that says nothing about which half is missing.
    """

    client_id: CredentialRef
    client_secret: CredentialRef

    @classmethod
    def default(cls, *, label: str = "FAA NMS-API") -> "ClientCredentials":
        return cls(
            client_id=CredentialRef(
                env_var="FAA_NMS_CLIENT_ID", label=f"{label} client id (spreadsheet KEY)"
            ),
            client_secret=CredentialRef(
                env_var="FAA_NMS_CLIENT_SECRET",
                label=f"{label} client secret (spreadsheet SECRET)",
            ),
        )

    def refs(self) -> tuple[CredentialRef, CredentialRef]:
        return (self.client_id, self.client_secret)

    def resolve(
        self, environ: Mapping[str, str] | None = None
    ) -> tuple[str, str] | None:
        """Both halves, read at point of use, or ``None`` if either is absent."""
        env = dict(os.environ if environ is None else environ)
        key = self.client_id.resolve(env)
        secret = self.client_secret.resolve(env)
        if not key or not secret:
            return None
        return key, secret

    def missing(self, environ: Mapping[str, str] | None = None) -> tuple[str, ...]:
        """Names of the variables that are not set. What the console shows."""
        env = dict(os.environ if environ is None else environ)
        return tuple(ref.env_var for ref in self.refs() if not ref.is_present(env))

    def status(
        self,
        environ: Mapping[str, str] | None = None,
        *,
        rejected: bool = False,
    ) -> CredentialStatus:
        """The worse of the two halves — a pair is only as good as both."""
        env = dict(os.environ if environ is None else environ)
        order = [
            CredentialStatus.MISSING,
            CredentialStatus.INVALID,
            CredentialStatus.EXPIRED,
            CredentialStatus.UNVERIFIED,
            CredentialStatus.CONFIGURED,
        ]
        statuses = [ref.status(env, rejected=rejected) for ref in self.refs()]
        return min(statuses, key=order.index)

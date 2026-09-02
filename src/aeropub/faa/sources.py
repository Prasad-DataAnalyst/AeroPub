"""Registering the FAA NMS-API with the watcher and the status board.

Turns an :class:`~aeropub.faa.config.NmsEnvironment` into the
:class:`~aeropub.registry.Source` records the rest of the platform already
understands, so the FAA appears on the same board as every eAIP, with the same
freshness rules and the same visible failure modes.

One asymmetry has to be handled here. A ``Source`` carries a single
:class:`~aeropub.registry.CredentialRef`, and OAuth2 needs two — a client id and
a client secret, which go missing independently and are the commonest
onboarding mistake. The Source therefore carries the secret, which is the half
that must never be disclosed, and :func:`credential_rows` reports both so the
operator's screen can say *which* half is absent rather than only that
something is.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Mapping

from aeropub.faa.config import ClientCredentials, NmsEnvironment, load_environment
from aeropub.registry import (
    CredentialStatus,
    DetectionTier,
    Redistribution,
    Source,
    SourceFormat,
    SourceKind,
)

__all__ = [
    "CredentialRow",
    "credential_rows",
    "nms_sources",
]

#: The initial load is a complete restatement of every active NOTAM. Pulled
#: daily as a reconciliation baseline, not as the primary feed — incremental
#: collection by lastUpdatedDate is what runs at NOTAM speed.
INITIAL_LOAD_INTERVAL = timedelta(hours=24)

#: FAA data is US Government work, but the NMS-API's terms attach conditions —
#: attribution, currency warnings, and no implication of endorsement. Recording
#: that as CONDITIONAL rather than PERMITTED is deliberate: the render layer
#: gates on this, and guessing "permitted" would publish somebody else's data
#: on our assumption rather than on their terms.
FAA_REDISTRIBUTION = Redistribution.CONDITIONAL


def nms_sources(
    environment: NmsEnvironment | None = None,
    credentials: ClientCredentials | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> tuple[Source, ...]:
    """The watchable NMS endpoints as registry sources.

    Not verified: a URL from the FAA's own onboarding pack is a well-sourced
    claim, and it stays a claim until a call against it succeeds. The board
    says ``unverified`` until :func:`aeropub.faa.check.verify` marks it.
    """
    env = environment or load_environment(environ=environ)
    creds = credentials or ClientCredentials.default()
    secret = creds.client_secret
    prefix = f"FAA-NMS-{env.name.upper()}"

    return (
        Source(
            source_id=f"{prefix}-NOTAM",
            authority="FAA",
            name=f"NMS NOTAM ({env.name})",
            kind=SourceKind.NOTAM,
            url=env.url("notams"),
            fmt=SourceFormat.REST_API,
            tier=DetectionTier.PUSH,
            credential=secret,
            redistribution=FAA_REDISTRIBUTION,
            note="Incremental collection by lastUpdatedDate. AIXM 5.1 with FAA "
            "fnse extensions; nmsResponseFormat: AIXM is required.",
        ),
        Source(
            source_id=f"{prefix}-CHECKLIST",
            authority="FAA",
            name=f"NMS NOTAM checklist ({env.name})",
            kind=SourceKind.NOTAM,
            url=env.url("notam_checklist"),
            fmt=SourceFormat.REST_API,
            tier=DetectionTier.ADAPTIVE_POLL,
            credential=secret,
            redistribution=FAA_REDISTRIBUTION,
            note="Reconciliation. A number on the FAA's checklist that we never "
            "received is a coverage gap, and this is the only thing that "
            "makes it visible.",
        ),
        Source(
            source_id=f"{prefix}-INITIAL-LOAD",
            authority="FAA",
            name=f"NMS initial load ({env.name})",
            kind=SourceKind.NOTAM,
            url=env.initial_load_url(),
            fmt=SourceFormat.REST_API,
            tier=DetectionTier.SCHEDULED,
            interval=INITIAL_LOAD_INTERVAL,
            credential=secret,
            redistribution=FAA_REDISTRIBUTION,
            note="Full active-NOTAM baseline, gzipped AIXM behind a five-minute "
            "signed URL.",
        ),
        Source(
            source_id=f"{prefix}-LOCATIONS",
            authority="FAA",
            name=f"NMS location series ({env.name})",
            kind=SourceKind.REGISTRY,
            url=env.url("location_series"),
            fmt=SourceFormat.REST_API,
            tier=DetectionTier.SCHEDULED,
            credential=secret,
            redistribution=FAA_REDISTRIBUTION,
            note="Which locations the FAA files NOTAM against, and how they map "
            "to ICAO indicators.",
        ),
    )


@dataclass(frozen=True, slots=True)
class CredentialRow:
    """One key on the operator's screen. Carries a name and a status, never a value."""

    env_var: str
    label: str
    status: CredentialStatus
    present: bool
    hint: str | None = None

    @property
    def needs_attention(self) -> bool:
        return self.status is not CredentialStatus.CONFIGURED

    def describe(self) -> str:
        return f"{self.env_var}: {self.status.value}" + (
            f" ({self.hint})" if self.hint else ""
        )


def credential_rows(
    credentials: ClientCredentials | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    rejected: bool = False,
) -> tuple[CredentialRow, ...]:
    """Both halves of the pair, for the key-status screen.

    ``rejected`` is what the last live call learned, not what we can see from
    here: a key that is present and well-formed is indistinguishable from a
    valid one until the FAA has been asked.
    """
    creds = credentials or ClientCredentials.default()
    env = dict(environ) if environ is not None else None
    rows = []
    for ref in creds.refs():
        rows.append(
            CredentialRow(
                env_var=ref.env_var,
                label=ref.label,
                status=ref.status(env, rejected=rejected),
                present=ref.is_present(env),
                hint=ref.hint,
            )
        )
    return tuple(rows)

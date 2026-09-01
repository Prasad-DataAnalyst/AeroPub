"""Qatar (OT) — Civil Aviation Authority AIM.

Entry point: https://aim.gov.qa
Dataset catalogue: https://aim.gov.qa/datasets.html

Both addresses come from the operator who works this State daily, not from
discovery, because this environment's egress policy blocks the host and the
site could not be reached to confirm anything.

That is why every source below is **unverified**, and why the shape of what
Qatar publishes — whether the eAIP is HTML or PDF, whether amendments and
supplements have their own indexes, what the dataset catalogue actually
contains — is left as ``unknown`` rather than guessed. Filling those in from
memory would put invented structure into the registry, which is precisely the
failure the no-mock rule exists to prevent.

To complete this profile, run ``python -m aeropub.capture`` against these URLs
from a machine with normal internet access, commit the fixtures, and the
structure can then be read from what Qatar actually serves.
"""

from __future__ import annotations

from aeropub.registry import (
    DetectionTier,
    Redistribution,
    Source,
    SourceFormat,
    SourceKind,
)
from aeropub.states import StateProfile

AIM_URL = "https://aim.gov.qa"
DATASETS_URL = "https://aim.gov.qa/datasets.html"

PROFILE = StateProfile(
    code="OT",
    name="Qatar",
    authority="Qatar Civil Aviation Authority — Aeronautical Information Management",
    aim_url=AIM_URL,
    sources=(
        Source(
            source_id="ot-aim-index",
            authority="OT",
            name="Qatar AIM entry point",
            kind=SourceKind.AIP,
            url=AIM_URL,
            # Format unconfirmed — the site was unreachable, so this records
            # the most likely shape for a national AIM portal and must be
            # corrected against a captured response before any parser is built.
            fmt=SourceFormat.EAIP_HTML,
            tier=DetectionTier.ADAPTIVE_POLL,
            redistribution=Redistribution.UNKNOWN,
            note="Address supplied by the operator; not yet reached from this environment.",
        ),
        Source(
            source_id="ot-datasets",
            authority="OT",
            name="Qatar AIM dataset catalogue",
            kind=SourceKind.REGISTRY,
            url=DATASETS_URL,
            fmt=SourceFormat.EAIP_HTML,
            tier=DetectionTier.ADAPTIVE_POLL,
            redistribution=Redistribution.UNKNOWN,
            note=(
                "Catalogue page. Contents unknown — likely the route into "
                "structured data (eTOD, obstacles, AIXM) if Qatar publishes any."
            ),
        ),
    ),
    # Nothing is declared absent. Qatar may well publish amendment and
    # supplement indexes, NOTAM and charts; we simply have not been able to
    # look. Declaring absence without checking would be a false statement about
    # the State, which is worse than an admitted gap.
    absent=frozenset(),
    notes=(
        "Blocked by egress policy from the build environment on 01 SEP 2026. "
        "Capture from a networked machine to confirm structure."
    ),
)

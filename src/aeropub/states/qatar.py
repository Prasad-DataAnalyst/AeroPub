"""Qatar (OT) — Civil Aviation Authority AIM.

Portal
    https://www.caa.gov.qa/en/aeronautical-information-management
eAIP host
    https://www.aim.gov.qa
Dataset catalogue
    https://aim.gov.qa/datasets.html

Provenance of this profile
--------------------------
The build environment's egress policy blocks ``aim.gov.qa``, so **no page here
has been fetched**. The URL structure below was inferred from live URLs
appearing in a public search index — real addresses on the authority's own
domain, but observed second-hand rather than retrieved.

That makes every source ``unverified``, and the inference explicit rather than
silent. What was observed:

    /eaip/2018-01-04-AIRAC/pdf/GEN-0.1.pdf
    /eaip/2022-01-27-AIRAC/html/eAIP/GEN-0.1-en-GB.html
    /eaip/2022-10-06-AIRAC/html/eAIP/GEN-3.1-en-GB.html
    /eaip/2025-05-15-AIRAC/html/eAIC/eAIC-2025-03-A-en-GB.html
    /eaip/2025-10-02-AIRAC/html/eAIC/eAIC-2025-07-A-en-GB.html
    /eaip/Initial/html/eAIP/GEN-0.1-en-GB.html

All five dated paths are exact AIRAC effective dates, which the AIRAC calendar
confirms independently. The layout is the EUROCONTROL eAIP specification —
good news for coverage, since a generic eAIP reader should handle it.

Confirm by capture before building a parser::

    python -m aeropub.capture https://aim.gov.qa/datasets.html --as ot-datasets
"""

from __future__ import annotations

from aeropub.airac import AiracCycle
from aeropub.registry import (
    DetectionTier,
    Redistribution,
    Source,
    SourceFormat,
    SourceKind,
)
from aeropub.states import StateProfile

__all__ = [
    "AIM_URL",
    "DATASETS_URL",
    "DATASET_MARKER",
    "EAIP_HOST",
    "PORTAL_URL",
    "PROFILE",
    "eaic_url",
    "eaip_base",
    "eaip_pdf_url",
    "eaip_section_url",
    "sources_for",
]

#: The authority's public portal. The eAIP itself is served from the AIM host.
PORTAL_URL = "https://www.caa.gov.qa/en/aeronautical-information-management"

#: Host serving the eAIP. Note the ``www.`` — the observed URLs carry it, while
#: the dataset catalogue was given to us without. Capture will settle whether
#: both forms resolve.
EAIP_HOST = "https://www.aim.gov.qa"

AIM_URL = "https://aim.gov.qa"
DATASETS_URL = "https://aim.gov.qa/datasets.html"

#: Marker Qatar uses in the eAIP where a section has a machine-readable dataset.
DATASET_MARKER = "[AIP-DS]"


def eaip_base(cycle: AiracCycle) -> str:
    """Root of one AIRAC edition, e.g. ``.../eaip/2026-10-01-AIRAC``.

    The path carries the cycle's *effective date*, not its identifier, so the
    AIRAC calendar is what addresses the publication.
    """
    return f"{EAIP_HOST}/eaip/{cycle.effective_date:%Y-%m-%d}-AIRAC"


def eaip_section_url(cycle: AiracCycle, section: str, *, lang: str = "en-GB") -> str:
    """HTML of one eAIP section, e.g. ``GEN-0.1``, ``ENR-3.1``, ``AD-2.OTHH``."""
    return f"{eaip_base(cycle)}/html/eAIP/{section}-{lang}.html"


def eaip_pdf_url(cycle: AiracCycle, section: str) -> str:
    """PDF of one eAIP section, where the edition publishes one."""
    return f"{eaip_base(cycle)}/pdf/{section}.pdf"


def eaic_url(
    cycle: AiracCycle,
    year: int,
    number: int,
    *,
    series: str = "A",
    lang: str = "en-GB",
) -> str:
    """One Aeronautical Information Circular within an edition."""
    return f"{eaip_base(cycle)}/html/eAIC/eAIC-{year}-{number:02d}-{series}-{lang}.html"


def sources_for(cycle: AiracCycle) -> tuple[Source, ...]:
    """Registry entries addressing one AIRAC edition.

    Every entry is unverified. None has been fetched.
    """
    return (
        Source(
            source_id=f"ot-eaip-{cycle.identifier}",
            authority="OT",
            name=f"Qatar eAIP {cycle.identifier}",
            kind=SourceKind.AIP,
            url=f"{eaip_base(cycle)}/html/index-en-GB.html",
            fmt=SourceFormat.EAIP_HTML,
            tier=DetectionTier.ADAPTIVE_POLL,
            redistribution=Redistribution.UNKNOWN,
            note=(
                "Edition root. Index filename inferred from the EUROCONTROL "
                "eAIP layout and not observed — confirm by capture."
            ),
        ),
    )


PROFILE = StateProfile(
    code="OT",
    name="Qatar",
    authority="Qatar Civil Aviation Authority — Aeronautical Information Management",
    aim_url=AIM_URL,
    sources=(
        Source(
            source_id="ot-portal",
            authority="OT",
            name="QCAA AIM portal",
            kind=SourceKind.AIP,
            url=PORTAL_URL,
            fmt=SourceFormat.EAIP_HTML,
            tier=DetectionTier.ADAPTIVE_POLL,
            redistribution=Redistribution.UNKNOWN,
            note="Authority's published entry point for the eAIP.",
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
                "Qatar publishes AIP data sets alongside the eAIP, marking the "
                "corresponding section with [AIP-DS]. Contents unconfirmed; this "
                "is the likely route to structured data."
            ),
        ),
    ),
    # Still nothing declared absent. Qatar publishes AIRAC AMDTs, SUPs and AICs
    # — the AICs are directly observable in the URLs above — but their index
    # pages have not been located, and "we have not found it" is not "it does
    # not exist".
    absent=frozenset(),
    notes=(
        "eAIP follows the EUROCONTROL layout, addressed by AIRAC effective date. "
        "AIRAC AMDT/SUP cut-off is 28 days, 56 for major changes, per QCAR. "
        "Structure inferred from a public search index on 01 SEP 2026; host "
        "unreachable from the build environment, so nothing is verified."
    ),
)

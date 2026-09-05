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
    /AIP/QA-history-en-GB.html

All five dated paths are exact AIRAC effective dates, which the AIRAC calendar
confirms independently. The layout is the EUROCONTROL eAIP specification —
good news for coverage, since a generic eAIP reader should handle it.

The last path is the **edition history**, and it arrived differently from the
others: an operator who uses the site gave it to us directly. It is still
unfetched — the egress policy refuses the host either way — but it is
first-hand rather than search-index, and it is the page that lists every
edition, which makes it the right entry point rather than guessing an edition
root. ``QA-history-en-GB.html`` is the standard EUROCONTROL history filename
with the State prefix, which is one more piece of evidence for the layout.

Qatar has changed its layout
----------------------------
The same operator supplied a current edition root::

    /AIP/03-SEP-2026/AIP-30/2026-10-01-000000/html/index-en-GB.html

That is **not** the ``/eaip/{effective}-AIRAC/`` form every 2018–2025
observation uses. The current path carries four things:

=====================  =========================================================
``03-SEP-2026``        the publication date. For AIRAC 2610 this is exactly
                       T-28 — the ICAO recipient deadline, which this
                       repository's own calendar computes independently
``AIP-30``             a running amendment number, **not derivable from the
                       cycle**
``2026-10-01-000000``  the AIRAC effective date and time. 2026-10-01 is
                       AIRAC 2610's effective date, again confirmed against
                       the calendar
``html/index-en-GB``   the EUROCONTROL layout, unchanged
=====================  =========================================================

Three of the four fall out of the AIRAC calendar. The amendment number does
not, and that is the finding that matters for automation: **a current Qatar
edition cannot be addressed from the cycle alone.** The history page is not a
convenience, it is the only published way to learn which amendment number an
edition carries. Anything that tries to construct a 2026 URL from a cycle will
be guessing at one field in four.

Both layouts are kept below. The legacy builders still reproduce every
2018–2025 observation exactly, and a State that changed its scheme once may
serve old editions at old addresses.

Confirm by capture before building a parser::

    python -m aeropub.capture https://aim.gov.qa/datasets.html --as ot-datasets
"""

from __future__ import annotations

from datetime import date

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
    "HISTORY_URL",
    "PORTAL_URL",
    "PROFILE",
    "eaic_url",
    "eaip_base",
    "edition_base",
    "edition_index_url",
    "edition_section_url",
    "publication_date",
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

#: The edition history: every eAIP edition Qatar has published, with its dates.
#: The right entry point for a first fetch, because it addresses the editions
#: rather than requiring one to be guessed from the AIRAC calendar.
HISTORY_URL = f"{AIM_URL}/AIP/QA-history-en-GB.html"


def eaip_base(cycle: AiracCycle) -> str:
    """Root of one edition in the **legacy** layout.

    ``.../eaip/2022-01-27-AIRAC``. The path carries the cycle's effective date,
    not its identifier, so the AIRAC calendar addresses the publication on its
    own. Every 2018–2025 observation is this form.

    Superseded for current editions — see :func:`edition_base`, which needs an
    amendment number the calendar cannot supply.
    """
    return f"{EAIP_HOST}/eaip/{cycle.effective_date:%Y-%m-%d}-AIRAC"


def publication_date(cycle: AiracCycle) -> date:
    """When Qatar publishes an edition: T-28 before it takes effect.

    Observed once — AIP-30 was published 03 SEP 2026 for an edition effective
    01 OCT 2026 — and that is exactly the ICAO recipient deadline this
    repository computes for AIRAC 2610 from the calendar alone. One
    observation and one independent derivation agreeing is worth more than
    either, but it is still one observation: a State that publishes early for
    one cycle would break it.
    """
    return cycle.recipient_deadline


def edition_base(
    cycle: AiracCycle, amendment: int, *, published: date | None = None
) -> str:
    """Root of one edition in the **current** layout.

    ``.../AIP/03-SEP-2026/AIP-30/2026-10-01-000000``.

    ``amendment`` is Qatar's running AIP amendment number and has no relation
    to the cycle — it is why this function takes an argument the AIRAC calendar
    cannot supply, and why :data:`HISTORY_URL` is the entry point rather than a
    convenience. ``published`` defaults to :func:`publication_date`.
    """
    if amendment <= 0:
        raise ValueError(
            "amendment must be a positive running number — it is Qatar's own "
            "count of AIP amendments, not anything derived from the cycle"
        )
    when = published or publication_date(cycle)
    # The month is upper case in the path — 03-SEP-2026, not 03-Sep-2026 — and
    # a server that cares about case would 404 on the difference.
    stamp = f"{when:%d-%b-%Y}".upper()
    return (
        f"{AIM_URL}/AIP/{stamp}/AIP-{amendment}"
        f"/{cycle.effective_date:%Y-%m-%d}-000000"
    )


def edition_index_url(
    cycle: AiracCycle,
    amendment: int,
    *,
    published: date | None = None,
    lang: str = "en-GB",
) -> str:
    """The index of one edition in the current layout."""
    return (
        f"{edition_base(cycle, amendment, published=published)}"
        f"/html/index-{lang}.html"
    )


def edition_section_url(
    cycle: AiracCycle,
    amendment: int,
    section: str,
    *,
    published: date | None = None,
    lang: str = "en-GB",
) -> str:
    """One section of one edition in the current layout.

    ``section`` is the eAIP name — ``ENR-3.1``, ``ENR-2.1``, ``AD-2.OTHH``.
    The section filenames are unobserved in this layout; only the index is.
    They follow the EUROCONTROL convention in every other eAIP and almost
    certainly do here, but that is inference and a capture should settle it.
    """
    return (
        f"{edition_base(cycle, amendment, published=published)}"
        f"/html/eAIP/{section}-{lang}.html"
    )


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
            source_id="ot-history",
            authority="OT",
            name="Qatar eAIP edition history",
            kind=SourceKind.REGISTRY,
            url=HISTORY_URL,
            fmt=SourceFormat.EAIP_HTML,
            tier=DetectionTier.ADAPTIVE_POLL,
            redistribution=Redistribution.UNKNOWN,
            note=(
                "Lists every published edition. Given first-hand by an operator "
                "rather than read from a search index, and still unfetched: the "
                "egress policy refuses the host. The right first request once it "
                "does not."
            ),
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

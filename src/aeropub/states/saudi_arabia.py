"""Saudi Arabia (OE) — Saudi Air Navigation Services, AIM.

Portal
    https://www.sans.com.sa/services/services-aim
eAIP host
    https://aimss.sans.com.sa
Published eAIPs index
    https://aimss.sans.com.sa/assets/FileManagerFiles/History-en-SA.html

Provenance of this profile
--------------------------
The host is blocked by this build environment's egress policy, so **no page
here has been fetched**. The structure below was decoded from live URLs
appearing in a public search index — real addresses on the authority's own
domain, observed second-hand. Every source is therefore ``unverified``.

Observed paths::

    /assets/FileManagerFiles/History-en-SA.html
    /assets/FileManagerFiles/default.html
    /assets/FileManagerFiles/AIC/OE-eAIC-2025-05-en-SA.pdf
    /assets/FileManagerFiles/AIC/OE-eAIC-2024-08-en-SA.pdf
    /assets/FileManagerFiles/AIRAC AIP AMDT 05_24_2024_05_16/eAIC/...
    /assets/FileManagerFiles/AIRAC AIP AMDT 11_25_2025_10_30/eAIC/...
    /assets/FileManagerFiles/AIRAC AIP AMDT 01_26_2026_01_22/eAIC/...

An amendment directory is named ``AIRAC AIP AMDT {NN}_{YY}_{YYYY_MM_DD}``,
and all three parts are derivable from the AIRAC cycle: the amendment number
is the cycle's ordinal within its year, and the date is its effective date.
Verified against the calendar for 2405, 2511 and 2601.

Circular dates *inside* an edition are not so regular. Two of the three
observed are AIRAC effective dates and one is not, so a circular carries a
plain date rather than a cycle.

Note the contrast with Qatar, which addresses editions by effective date alone.
Same region, same ICAO framework, different URL grammar — which is why each
State gets its own module rather than a shared guess.
"""

from __future__ import annotations

from datetime import date
from urllib.parse import quote

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
    "DEFAULT_INDEX_URL",
    "EAIP_HOST",
    "FILES_BASE",
    "HISTORY_URL",
    "MAJOR_SUBMISSION_CUTOFF_DAYS",
    "PORTAL_URL",
    "PROFILE",
    "SUBMISSION_CUTOFF_DAYS",
    "amendment_base",
    "amendment_name",
    "circular_url",
    "standalone_circular_url",
]

PORTAL_URL = "https://www.sans.com.sa/services/services-aim"
EAIP_HOST = "https://aimss.sans.com.sa"
FILES_BASE = f"{EAIP_HOST}/assets/FileManagerFiles"

#: "Published eAIPs — Kingdom of Saudi Arabia": the edition index.
HISTORY_URL = f"{FILES_BASE}/History-en-SA.html"

#: A second index page seen alongside it; relationship between the two unknown.
DEFAULT_INDEX_URL = f"{FILES_BASE}/default.html"

#: Days before the effective date that originators must submit material to AIM.
#: This is a *submission* cut-off inside the State, not the ICAO distribution
#: deadline of 42/56 days, which governs AIS to recipients. Different stages of
#: the same pipeline; conflating them would misread both.
SUBMISSION_CUTOFF_DAYS = 70
MAJOR_SUBMISSION_CUTOFF_DAYS = 84


def amendment_name(cycle: AiracCycle) -> str:
    """The amendment directory name for a cycle, e.g. ``AIRAC AIP AMDT 01_26_2026_01_22``.

    The amendment number is the cycle's ordinal within its year — confirmed
    against three observed editions spanning 2024 to 2026.
    """
    return (
        f"AIRAC AIP AMDT {cycle.ordinal:02d}_{cycle.year % 100:02d}_"
        f"{cycle.effective_date:%Y_%m_%d}"
    )


def amendment_base(cycle: AiracCycle) -> str:
    """Root URL of one amendment edition, with the directory name encoded."""
    return f"{FILES_BASE}/{quote(amendment_name(cycle))}"


def circular_url(
    edition: AiracCycle,
    number: int,
    year: int,
    issued: date,
    *,
    lang: str = "en-GB",
) -> str:
    """A circular carried inside an amendment edition.

    ``issued`` is the circular's own date and takes a plain ``date``, not a
    cycle, deliberately. Two of the three observed circulars are dated on AIRAC
    effective dates and the third — 29 October 2024, inside edition 2411 — is
    not; cycle 2411 became effective on the 31st. Typing this as a cycle would
    have produced confidently wrong URLs that 404 silently, which is the failure
    an unverified structure is most likely to cause.
    """
    return (
        f"{amendment_base(edition)}/eAIC/{number:02d}-{year}_"
        f"{issued:%Y_%m_%d}/OE-AIC-{lang}.html"
    )


def standalone_circular_url(year: int, number: int, *, lang: str = "en-SA") -> str:
    """A circular published as a PDF outside any amendment edition."""
    return f"{FILES_BASE}/AIC/OE-eAIC-{year}-{number:02d}-{lang}.pdf"


PROFILE = StateProfile(
    code="OE",
    name="Saudi Arabia",
    authority="Saudi Air Navigation Services — Aeronautical Information Management",
    aim_url=EAIP_HOST,
    sources=(
        Source(
            source_id="oe-eaip-history",
            authority="OE",
            name="Published eAIPs index",
            kind=SourceKind.AIP,
            url=HISTORY_URL,
            fmt=SourceFormat.EAIP_HTML,
            tier=DetectionTier.ADAPTIVE_POLL,
            redistribution=Redistribution.UNKNOWN,
            note="Edition index. The natural watch point for new amendments.",
        ),
        Source(
            source_id="oe-aic",
            authority="OE",
            name="Aeronautical Information Circulars",
            kind=SourceKind.AIC_INDEX,
            url=f"{FILES_BASE}/AIC/",
            fmt=SourceFormat.PDF,
            tier=DetectionTier.ADAPTIVE_POLL,
            redistribution=Redistribution.UNKNOWN,
            note=(
                "Circulars are published as PDFs here and also republished inside "
                "amendment editions. Whether this directory is browsable is unknown."
            ),
        ),
    ),
    # Saudi demonstrably publishes AIRAC amendments and supplements — the
    # amendment directories are directly observable — so nothing is absent.
    # The supplement index has simply not been located.
    absent=frozenset(),
    notes=(
        "Amendment editions are addressed by cycle ordinal, year and effective "
        "date. Originator submission cut-off is 70 days, 84 for major changes — "
        "an internal deadline, distinct from the ICAO 42/56-day distribution "
        "requirement. Structure decoded from a public search index on 01 SEP 2026; "
        "host unreachable from the build environment, so nothing is verified."
    ),
)

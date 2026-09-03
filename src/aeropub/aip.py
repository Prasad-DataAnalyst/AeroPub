"""The AIP's own structure — the index every parser fills and every dossier renders.

ICAO Annex 15 and PANS-AIM (Doc 10066) define what an AIP contains and how it
is numbered. That makes this, like the NOTAM format, buildable from the
specification rather than from a captured document: GEN 1.7 means the same
thing in Doha as in Denver, even though the two States lay the page out
differently.

What it is for
--------------
**Coverage that can be proved.** Holding "the AIP" for a State is not a fact
about anything. Holding AD 2.12 and AD 2.13 for OTHH, effective cycle 2610, and
*not* holding AD 2.10, is. :class:`AipCoverage` records that section by section,
and the three states it keeps apart are the same three the source registry
keeps apart — held, absent by the State's own account, and never checked.

**Routing.** Each section declares the operational domains it feeds, in the
same vocabulary :mod:`aeropub.impact` uses, so a change in a section reaches
the people it concerns without a lookup table maintained somewhere else.

**The audit mechanism.** GEN 0.4, ENR 0.4 and AD 0.4 are checklists of the
pages the State considers current. They are not boilerplate: reconciling them
against what we hold is what turns "we scraped their website" into "we can
prove we have the complete, current publication".

.. warning::
   This is the *reference* structure. States deviate — omitting sections they
   have nothing to put in, adding their own, occasionally renumbering. A
   section missing from a State's AIP is a fact about that State, recorded as
   :attr:`HoldingState.ABSENT`, and must never be recorded merely because we
   did not look.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Iterable, Iterator

from aeropub.airac import AiracCycle
from aeropub.provenance import SourceRef

__all__ = [
    "DOMAINS",
    "ATTRIBUTE_SECTIONS",
    "SECTIONS",
    "AipCoverage",
    "HoldingState",
    "Part",
    "Repeat",
    "Section",
    "SectionHolding",
    "aerodrome_sections",
    "currency_sections",
    "heliport_sections",
    "section",
    "section_for_attribute",
    "sections_for",
]


class Part(str, Enum):
    """The AIP's three parts."""

    GEN = "GEN"
    ENR = "ENR"
    AD = "AD"


class Repeat(str, Enum):
    """Whether a section appears once, or once per facility."""

    ONCE = "once"
    PER_AERODROME = "per_aerodrome"
    PER_HELIPORT = "per_heliport"


#: The operational domains a section can feed. Extends the vocabulary
#: :mod:`aeropub.impact` already uses for its attribute rules — the two must
#: agree, or a change would be routed to a domain no consumer subscribes to.
#: A test asserts every domain named in ``impact.RULES`` appears here.
DOMAINS: frozenset[str] = frozenset(
    {
        # shared with aeropub.impact
        "performance",
        "dispatch",
        "charts",
        "suitability",
        "alternates",
        "procedures",
        "crew",
        # the AIP reaches further than runway attributes do
        "currency",
        "airspace",
        "obstacles",
        "met",
        "comms",
        "navaids",
        "winter",
        "noise",
        "security",
        "permits",
        "cost",
        "ground",
        "regulatory",
    }
)


@dataclass(frozen=True, slots=True)
class Section:
    """One numbered section of an AIP."""

    code: str
    """As the AIP prints it, e.g. ``"AD 2.13"``."""

    part: Part
    chapter: int
    ordinal: int | None
    """Position within the chapter. ``None`` for a chapter with no subdivision."""

    title: str
    """ICAO's own title for the section, not a paraphrase."""

    domains: tuple[str, ...] = ()
    """Which operational domains a change here reaches."""

    repeats: Repeat = Repeat.ONCE
    note: str = ""
    """Why it matters, where that is not obvious from the title."""

    icao_defined: bool = True
    """Whether Annex 15 itself defines this section.

    ``False`` marks a section the eAIP specification or individual States add.
    Recorded rather than smoothed over: claiming ICAO mandates a section it
    does not would make a State's omission look like a deficiency."""

    def __post_init__(self) -> None:
        if not self.code.strip() or not self.title.strip():
            raise ValueError("Section.code and Section.title must be non-empty")
        unknown = set(self.domains) - DOMAINS
        if unknown:
            raise ValueError(
                f"section {self.code} names unknown domains {sorted(unknown)}; "
                f"add them to aip.DOMAINS or fix the typo"
            )

    @property
    def is_currency(self) -> bool:
        """Whether this section is part of the currency spine.

        Chapter 0 of each part carries the amendment record, the supplement
        record and the page checklist — the sections that say what the State
        believes it has published, and therefore what we can be missing.
        """
        return self.chapter == 0

    @property
    def is_checklist(self) -> bool:
        """The page checklist specifically — the reconciliation source."""
        return self.chapter == 0 and self.ordinal == 4

    def applies_to(self, domain: str) -> bool:
        return domain in self.domains

    def describe(self) -> str:
        extra = "" if self.icao_defined else " (not defined by Annex 15)"
        return f"{self.code} {self.title}{extra}"


def _s(code, part, chapter, ordinal, title, domains=(), repeats=Repeat.ONCE,
       note="", icao_defined=True) -> Section:
    return Section(
        code=code, part=part, chapter=chapter, ordinal=ordinal, title=title,
        domains=tuple(domains), repeats=repeats, note=note, icao_defined=icao_defined,
    )


_G, _E, _A = Part.GEN, Part.ENR, Part.AD

_GEN: tuple[Section, ...] = (
    _s("GEN 0.1", _G, 0, 1, "Preface", ("currency",)),
    _s("GEN 0.2", _G, 0, 2, "Record of AIP amendments", ("currency",),
       note="What the State says it has issued. Reconciled against what we hold."),
    _s("GEN 0.3", _G, 0, 3, "Record of AIP supplements", ("currency",),
       note="A supplement we never received looks exactly like one never issued."),
    _s("GEN 0.4", _G, 0, 4, "Checklist of AIP pages", ("currency",),
       note="The audit mechanism. Every discrepancy against it is a coverage gap."),
    _s("GEN 0.5", _G, 0, 5, "List of hand amendments to the AIP", ("currency",)),
    _s("GEN 0.6", _G, 0, 6, "Table of contents to Part 1", ("currency",)),

    _s("GEN 1.1", _G, 1, 1, "Designated authorities", ("regulatory", "permits")),
    _s("GEN 1.2", _G, 1, 2, "Entry, transit and departure of aircraft",
       ("permits", "dispatch"), note="Overflight and landing permit lead times."),
    _s("GEN 1.3", _G, 1, 3, "Entry, transit and departure of passengers and crew",
       ("permits", "crew")),
    _s("GEN 1.4", _G, 1, 4, "Entry, transit and departure of cargo", ("permits",)),
    _s("GEN 1.5", _G, 1, 5, "Aircraft instruments, equipment and flight documents",
       ("regulatory", "dispatch")),
    _s("GEN 1.6", _G, 1, 6,
       "Summary of national regulations and international agreements", ("regulatory",)),
    _s("GEN 1.7", _G, 1, 7,
       "Differences from ICAO Standards, Recommended Practices and Procedures",
       ("regulatory", "procedures", "crew"),
       note="The most under-read section in the AIP. Filed differences are "
            "precisely the assumptions that catch crews out."),

    _s("GEN 2.1", _G, 2, 1, "Measuring system, aircraft markings, holidays",
       ("procedures", "crew"),
       note="Units are an altimetry trap — hPa against inHg, feet against metre levels."),
    _s("GEN 2.2", _G, 2, 2, "Abbreviations used in AIS publications", ("procedures",)),
    _s("GEN 2.3", _G, 2, 3, "Chart symbols", ("charts",)),
    _s("GEN 2.4", _G, 2, 4, "Location indicators", ("dispatch",)),
    _s("GEN 2.5", _G, 2, 5, "List of radio navigation aids", ("navaids",)),
    _s("GEN 2.6", _G, 2, 6, "Conversion tables", ("procedures",)),
    _s("GEN 2.7", _G, 2, 7, "Sunrise/sunset tables", ("dispatch", "crew"),
       note="Drives night-operation requirements and curfew arithmetic."),

    _s("GEN 3.1", _G, 3, 1, "Aeronautical information services", ("currency",)),
    _s("GEN 3.2", _G, 3, 2, "Aeronautical charts", ("charts",)),
    _s("GEN 3.3", _G, 3, 3, "Air traffic services", ("airspace", "procedures")),
    _s("GEN 3.4", _G, 3, 4, "Communication services", ("comms",)),
    _s("GEN 3.5", _G, 3, 5, "Meteorological services", ("met", "alternates"),
       note="MET provision drives alternate minima policy."),
    _s("GEN 3.6", _G, 3, 6, "Search and rescue", ("dispatch",),
       note="SAR coverage matters on oceanic and remote routing."),

    _s("GEN 4.1", _G, 4, 1, "Aerodrome/heliport charges", ("cost",)),
    _s("GEN 4.2", _G, 4, 2, "Air navigation services charges", ("cost",)),
)

_ENR: tuple[Section, ...] = (
    _s("ENR 0.1", _E, 0, 1, "Preface", ("currency",)),
    _s("ENR 0.2", _E, 0, 2, "Record of AIP amendments", ("currency",)),
    _s("ENR 0.3", _E, 0, 3, "Record of AIP supplements", ("currency",)),
    _s("ENR 0.4", _E, 0, 4, "Checklist of AIP pages", ("currency",)),
    _s("ENR 0.5", _E, 0, 5, "List of hand amendments to the AIP", ("currency",)),
    _s("ENR 0.6", _E, 0, 6, "Table of contents to Part 2", ("currency",)),

    _s("ENR 1.1", _E, 1, 1, "General rules", ("procedures",)),
    _s("ENR 1.2", _E, 1, 2, "Visual flight rules", ("procedures",)),
    _s("ENR 1.3", _E, 1, 3, "Instrument flight rules", ("procedures",)),
    _s("ENR 1.4", _E, 1, 4, "ATS airspace classification and description", ("airspace",)),
    _s("ENR 1.5", _E, 1, 5, "Holding, approach and departure procedures",
       ("procedures", "charts")),
    _s("ENR 1.6", _E, 1, 6, "ATS surveillance services and procedures",
       ("airspace", "procedures")),
    _s("ENR 1.7", _E, 1, 7, "Altimeter setting procedures", ("procedures", "crew"),
       note="Transition altitude and level vary by State and by aerodrome; a "
            "mismatch is a direct altimetry hazard."),
    _s("ENR 1.8", _E, 1, 8, "Regional supplementary procedures", ("procedures",)),
    _s("ENR 1.9", _E, 1, 9, "Air traffic flow management and airspace management",
       ("dispatch",), note="Drives ATFM exposure and slot risk."),
    _s("ENR 1.10", _E, 1, 10, "Flight planning", ("dispatch",),
       note="Flight-plan validity rules; a change invalidates filed plans."),
    _s("ENR 1.11", _E, 1, 11, "Addressing of flight plan messages", ("dispatch",)),
    _s("ENR 1.12", _E, 1, 12, "Interception of civil aircraft", ("security", "crew")),
    _s("ENR 1.13", _E, 1, 13, "Unlawful interference", ("security", "crew")),
    _s("ENR 1.14", _E, 1, 14, "Air traffic incidents", ("procedures",)),

    _s("ENR 2.1", _E, 2, 1, "FIR, UIR, TMA and CTA", ("airspace",),
       note="Boundary crossings and handover points, where procedures change."),
    _s("ENR 2.2", _E, 2, 2, "Other regulated airspace", ("airspace",)),

    _s("ENR 3.1", _E, 3, 1, "Lower ATS routes", ("airspace", "dispatch"),
       note="Withdrawal invalidates filed plans."),
    _s("ENR 3.2", _E, 3, 2, "Upper ATS routes", ("airspace", "dispatch")),
    _s("ENR 3.3", _E, 3, 3, "Area navigation routes", ("airspace", "dispatch")),
    _s("ENR 3.4", _E, 3, 4, "Helicopter routes", ("airspace",)),
    _s("ENR 3.5", _E, 3, 5, "Other routes", ("airspace",)),
    _s("ENR 3.6", _E, 3, 6, "En-route holding", ("procedures",)),

    _s("ENR 4.1", _E, 4, 1, "Radio navigation aids — en-route", ("navaids",)),
    _s("ENR 4.2", _E, 4, 2, "Special navigation systems", ("navaids",)),
    _s("ENR 4.3", _E, 4, 3, "Global navigation satellite system (GNSS)",
       ("navaids", "dispatch"), note="Feeds RAIM prediction and PBN substitution."),
    _s("ENR 4.4", _E, 4, 4, "Name-code designators for significant points",
       ("dispatch", "charts")),
    _s("ENR 4.5", _E, 4, 5, "Aeronautical ground lights — en-route", ("navaids",)),

    _s("ENR 5.1", _E, 5, 1, "Prohibited, restricted and danger areas",
       ("airspace", "security")),
    _s("ENR 5.2", _E, 5, 2,
       "Military exercise and training areas and air defence identification zone",
       ("airspace", "security")),
    _s("ENR 5.3", _E, 5, 3,
       "Other activities of a dangerous nature and other potential hazards",
       ("airspace", "security")),
    _s("ENR 5.4", _E, 5, 4, "Air navigation obstacles — en-route", ("obstacles",)),
    _s("ENR 5.5", _E, 5, 5, "Aerial sporting and recreational activities", ("airspace",)),
    _s("ENR 5.6", _E, 5, 6, "Bird migration and areas with sensitive fauna",
       ("airspace", "crew"), note="Migration corridors feed seasonal hazard profiling."),

    _s("ENR 6.1", _E, 6, 1, "En-route charts", ("charts",),
       note="Chart inventory and revision tracking."),
)

#: AD 2, once per aerodrome. The core dossier.
_AD2: tuple[Section, ...] = (
    _s("AD 2.1", _A, 2, 1, "Aerodrome location indicator and name",
       ("suitability",), Repeat.PER_AERODROME),
    _s("AD 2.2", _A, 2, 2, "Aerodrome geographical and administrative data",
       ("suitability", "procedures"), Repeat.PER_AERODROME,
       note="Reference point, elevation and magnetic variation currency."),
    _s("AD 2.3", _A, 2, 3, "Operational hours", ("dispatch", "suitability"),
       Repeat.PER_AERODROME, note="Curfew, PPR lead time and slot exposure."),
    _s("AD 2.4", _A, 2, 4, "Handling services and facilities",
       ("ground", "dispatch", "winter"), Repeat.PER_AERODROME,
       note="Fuel type and uplift; de-icing capacity against fleet and season."),
    _s("AD 2.5", _A, 2, 5, "Passenger facilities", ("ground",), Repeat.PER_AERODROME,
       note="Diversion support capability."),
    _s("AD 2.6", _A, 2, 6, "Rescue and fire fighting services",
       ("suitability", "dispatch", "alternates"), Repeat.PER_AERODROME,
       note="Category held against category required per type, including "
            "on-request availability and agent depletion limits."),
    _s("AD 2.7", _A, 2, 7, "Seasonal availability — clearing",
       ("winter", "performance", "dispatch"), Repeat.PER_AERODROME,
       note="Clearance priority, GRF readiness and braking action measurement."),
    _s("AD 2.8", _A, 2, 8, "Aprons, taxiways and check locations/positions data",
       ("ground", "suitability"), Repeat.PER_AERODROME,
       note="Stand and taxi-route compatibility by wingspan and OMGWS; Code E/F routing."),
    _s("AD 2.9", _A, 2, 9,
       "Surface movement guidance and control system and markings",
       ("ground", "procedures"), Repeat.PER_AERODROME,
       note="Low-visibility taxi capability; stop bars as an incursion defence."),
    _s("AD 2.10", _A, 2, 10, "Aerodrome obstacles", ("obstacles", "performance"),
       Repeat.PER_AERODROME, note="Areas 2 and 3. Feeds the obstacle study."),
    _s("AD 2.11", _A, 2, 11, "Meteorological information provided",
       ("met", "alternates"), Repeat.PER_AERODROME,
       note="Whether a TAF exists at all is an alternate-eligibility gate."),
    _s("AD 2.12", _A, 2, 12, "Runway physical characteristics",
       ("performance", "suitability", "charts"), Repeat.PER_AERODROME,
       note="Dimensions, slope, composition, PCN/PCR, thresholds, arrestor beds."),
    _s("AD 2.13", _A, 2, 13, "Declared distances", ("performance", "dispatch"),
       Repeat.PER_AERODROME,
       note="The payload driver. Deltas against the previous cycle matter most."),
    _s("AD 2.14", _A, 2, 14, "Approach and runway lighting",
       ("procedures", "performance", "charts"), Repeat.PER_AERODROME,
       note="Minima dependencies; PAPI angle against wheel-to-threshold clearance."),
    _s("AD 2.15", _A, 2, 15, "Other lighting, secondary power supply",
       ("procedures",), Repeat.PER_AERODROME,
       note="Switch-over time against low-visibility requirements."),
    _s("AD 2.16", _A, 2, 16, "Helicopter landing area", ("suitability",),
       Repeat.PER_AERODROME),
    _s("AD 2.17", _A, 2, 17, "ATS airspace", ("airspace",), Repeat.PER_AERODROME,
       note="Class and entry requirements against equipage."),
    _s("AD 2.18", _A, 2, 18, "ATS communication facilities", ("comms",),
       Repeat.PER_AERODROME,
       note="Frequency and hours coverage; CPDLC logon changes affect "
            "datalink-mandated airspace."),
    _s("AD 2.19", _A, 2, 19, "Radio navigation and landing aids",
       ("navaids", "procedures"), Repeat.PER_AERODROME,
       note="Navaid availability against the approaches actually flown."),
    _s("AD 2.20", _A, 2, 20, "Local aerodrome regulations",
       ("ground", "procedures", "crew"), Repeat.PER_AERODROME,
       note="Hot spots, mandatory tug or pushback, training restrictions."),
    _s("AD 2.21", _A, 2, 21, "Noise abatement procedures",
       ("noise", "performance", "procedures"), Repeat.PER_AERODROME,
       note="NADP 1 against NADP 2 changes thrust-reduction and flap-retraction "
            "heights, and with them engine thermal exposure."),
    _s("AD 2.22", _A, 2, 22, "Flight procedures", ("procedures", "charts"),
       Repeat.PER_AERODROME, note="SIDs, STARs, missed approach; EOSID requirement."),
    _s("AD 2.23", _A, 2, 23, "Additional information", ("crew", "security"),
       Repeat.PER_AERODROME,
       note="Bird hazard patterns, laser warnings, seasonal anomalies."),
    _s("AD 2.24", _A, 2, 24, "Charts related to an aerodrome", ("charts",),
       Repeat.PER_AERODROME, note="Feeds the chart study."),
    _s("AD 2.25", _A, 2, 25, "Additional/State-specific information",
       ("regulatory",), Repeat.PER_AERODROME,
       note="Content varies by State — geodetic rules, security measures, local "
            "environmental protocols. A prime candidate for the free-text path.",
       icao_defined=False),
)

#: AD 3, once per heliport. Same treatment where relevant.
_AD3_TITLES: tuple[tuple[int, str, tuple[str, ...]], ...] = (
    (1, "Heliport location indicator and name", ("suitability",)),
    (2, "Heliport geographical and administrative data", ("suitability",)),
    (3, "Operational hours", ("dispatch",)),
    (4, "Handling services and facilities", ("ground",)),
    (5, "Passenger facilities", ("ground",)),
    (6, "Rescue and fire fighting services", ("suitability", "dispatch")),
    (7, "Seasonal availability — clearing", ("winter",)),
    (8, "Aprons, taxiways and check locations/positions data", ("ground",)),
    (9, "Surface movement guidance and control system and markings", ("ground",)),
    (10, "Heliport obstacles", ("obstacles", "performance")),
    (11, "Meteorological information provided", ("met",)),
    (12, "Heliport data", ("performance", "suitability")),
    (13, "Declared distances", ("performance",)),
    (14, "Approach and FATO lighting", ("procedures",)),
    (15, "Other lighting, secondary power supply", ("procedures",)),
    (16, "ATS airspace", ("airspace",)),
    (17, "ATS communication facilities", ("comms",)),
    (18, "Radio navigation and landing aids", ("navaids",)),
    (19, "Local heliport regulations", ("ground", "procedures")),
    (20, "Noise abatement procedures", ("noise",)),
    (21, "Flight procedures", ("procedures",)),
    (22, "Additional information", ("crew",)),
    (23, "Charts related to a heliport", ("charts",)),
)

_AD: tuple[Section, ...] = (
    _s("AD 0.1", _A, 0, 1, "Preface", ("currency",)),
    _s("AD 0.2", _A, 0, 2, "Record of AIP amendments", ("currency",)),
    _s("AD 0.3", _A, 0, 3, "Record of AIP supplements", ("currency",)),
    _s("AD 0.4", _A, 0, 4, "Checklist of AIP pages", ("currency",)),
    _s("AD 0.5", _A, 0, 5, "List of hand amendments to the AIP", ("currency",)),
    _s("AD 0.6", _A, 0, 6, "Table of contents to Part 3", ("currency",)),

    _s("AD 1.1", _A, 1, 1, "Aerodrome/heliport availability and conditions of use",
       ("suitability", "dispatch")),
    _s("AD 1.2", _A, 1, 2, "Rescue and fire fighting services and snow plan",
       ("suitability", "winter"),
       note="The State-level RFFS framework and snow plan, complementing AD 2.6 "
            "and AD 2.7."),
    _s("AD 1.3", _A, 1, 3, "Index to aerodromes and heliports", ("dispatch",)),
    _s("AD 1.4", _A, 1, 4, "Grouping of aerodromes/heliports", ("dispatch",)),
    _s("AD 1.5", _A, 1, 5, "Certification status of aerodromes", ("suitability",),
       note="A suitability gate in its own right."),
    *_AD2,
    *tuple(
        _s(f"AD 3.{n}", _A, 3, n, title, domains, Repeat.PER_HELIPORT)
        for n, title, domains in _AD3_TITLES
    ),
)

#: The complete index, in publication order.
SECTIONS: tuple[Section, ...] = _GEN + _ENR + _AD

_BY_CODE: dict[str, Section] = {s.code: s for s in SECTIONS}
if len(_BY_CODE) != len(SECTIONS):  # pragma: no cover - a construction error
    raise RuntimeError("duplicate section codes in the AIP index")


#: Where each modelled attribute is published. ICAO decides this, not us:
#: declared distances are AD 2.13 wherever you are, so a fact and the section
#: it came from can be shown together without a per-State lookup.
#:
#: A test asserts every attribute :mod:`aeropub.impact` models appears here. An
#: attribute with no AIP home would be assessed for impact and then never
#: appear in a dossier, which is a worse failure than not modelling it at all.
ATTRIBUTE_SECTIONS: dict[str, str] = {
    # AD 2.2 — geographical and administrative data
    "elevation_ft": "AD 2.2",
    "magnetic_variation": "AD 2.2",
    "latitude": "AD 2.2",
    "longitude": "AD 2.2",
    "aerodrome_name": "AD 2.1",
    # AD 2.6 — rescue and fire fighting
    "rffs_category": "AD 2.6",
    # AD 2.12 — runway physical characteristics
    "runway_width_m": "AD 2.12",
    "runway_length_m": "AD 2.12",
    "pcn": "AD 2.12",
    "pcr": "AD 2.12",
    "surface": "AD 2.12",
    "displaced_threshold_m": "AD 2.12",
    # AD 2.13 — declared distances
    "tora_m": "AD 2.13",
    "toda_m": "AD 2.13",
    "asda_m": "AD 2.13",
    "lda_m": "AD 2.13",
    # AD 2.14 — approach and runway lighting
    "papi_angle": "AD 2.14",
    "approach_lighting": "AD 2.14",
}


def section_for_attribute(attribute: str) -> Section | None:
    """Which AIP section publishes this attribute, or ``None``.

    ``None`` is a real answer and must be rendered as one. An attribute we
    have not placed is shown under "not attributed to a section" rather than
    filed under a plausible guess, because a value in the wrong section reads
    as though the section said it.
    """
    code = ATTRIBUTE_SECTIONS.get(attribute)
    return section(code) if code else None


def section(code: str) -> Section:
    """One section by its printed code, e.g. ``"AD 2.13"``."""
    key = " ".join(code.strip().upper().split())
    try:
        return _BY_CODE[key]
    except KeyError:
        raise KeyError(
            f"no AIP section {code!r}. States do add their own; record it as a "
            "State extension rather than looking it up here."
        ) from None


def sections_for(
    part: Part | None = None,
    *,
    chapter: int | None = None,
    domain: str | None = None,
    repeats: Repeat | None = None,
) -> tuple[Section, ...]:
    """Sections matching every filter given."""
    if domain is not None and domain not in DOMAINS:
        raise ValueError(f"unknown domain {domain!r}; known: {sorted(DOMAINS)}")
    return tuple(
        s
        for s in SECTIONS
        if (part is None or s.part is part)
        and (chapter is None or s.chapter == chapter)
        and (domain is None or s.applies_to(domain))
        and (repeats is None or s.repeats is repeats)
    )


def aerodrome_sections() -> tuple[Section, ...]:
    """AD 2.1 to AD 2.25 — the complete per-aerodrome dossier."""
    return sections_for(Part.AD, chapter=2)


def heliport_sections() -> tuple[Section, ...]:
    """AD 3.1 to AD 3.23."""
    return sections_for(Part.AD, chapter=3)


def currency_sections() -> tuple[Section, ...]:
    """Chapter 0 of every part — the amendment, supplement and page records."""
    return tuple(s for s in SECTIONS if s.is_currency)


# --------------------------------------------------------------------------
# Coverage
# --------------------------------------------------------------------------


class HoldingState(str, Enum):
    """What we know about one section of one publication.

    The same three-way distinction the source registry insists on, at section
    granularity: conflating them is how a monitoring system comes to lie.
    """

    HELD = "held"
    """Fetched, parsed, and attributable."""

    ABSENT = "absent"
    """The State does not publish it — a recorded property of the State,
    established from its own checklist or contents page. Never inferred from
    our failure to find it."""

    FAILED = "failed"
    """We tried and could not read it. A visible coverage gap, not an absence."""

    NOT_CHECKED = "not_checked"
    """We have not looked. The default, and the honest one."""

    @property
    def is_gap(self) -> bool:
        """Whether this is a hole in our coverage rather than in the State's AIP."""
        return self in (HoldingState.FAILED, HoldingState.NOT_CHECKED)


@dataclass(frozen=True, slots=True)
class SectionHolding:
    """What we hold for one section, of one entity, for one cycle."""

    section: Section
    entity: str
    """Whose section it is: a State prefix like ``"OT"``, or an aerodrome for
    the per-aerodrome sections, e.g. ``"OTHH"``."""

    state: HoldingState
    cycle: AiracCycle | None = None
    source: SourceRef | None = None
    recorded_at: datetime | None = None
    detail: str = ""

    def __post_init__(self) -> None:
        if not self.entity.strip():
            raise ValueError("SectionHolding.entity must be a non-empty string")
        if self.state is HoldingState.HELD and self.source is None:
            raise ValueError(
                f"{self.section.code} for {self.entity} is recorded as held with no "
                "SourceRef; a section we cannot cite is not one we hold"
            )
        if self.state is HoldingState.ABSENT and not self.detail.strip():
            raise ValueError(
                f"{self.section.code} for {self.entity} is recorded as absent with no "
                "reason. Absence is a claim about the State and needs its basis — "
                "the checklist or contents page that says so."
            )

    @property
    def key(self) -> tuple[str, str]:
        return (self.entity, self.section.code)

    def describe(self) -> str:
        line = f"{self.section.code:9} {self.entity:6} {self.state.value}"
        if self.cycle is not None:
            line += f"  cycle {self.cycle.identifier}"
        return line + (f"  — {self.detail}" if self.detail else "")


class AipCoverage:
    """What we hold of an AIP, section by section, and what we do not."""

    def __init__(self, holdings: Iterable[SectionHolding] | None = None) -> None:
        self._holdings: dict[tuple[str, str], SectionHolding] = {}
        for holding in holdings or ():
            self.record(holding)

    def record(self, holding: SectionHolding) -> None:
        """Record what we hold. A later record for the same key replaces it."""
        self._holdings[holding.key] = holding

    def __len__(self) -> int:
        return len(self._holdings)

    def __iter__(self) -> Iterator[SectionHolding]:
        return iter(self._holdings.values())

    def holding(self, entity: str, code: str) -> SectionHolding:
        """What we hold for one section.

        Returns a ``NOT_CHECKED`` holding where nothing has been recorded,
        rather than raising or returning ``None``. Never having looked is a
        real state with a real answer, and the caller should render it.
        """
        wanted = section(code)
        existing = self._holdings.get((entity, wanted.code))
        if existing is not None:
            return existing
        return SectionHolding(section=wanted, entity=entity, state=HoldingState.NOT_CHECKED)

    def expected(self, entity: str, *, per_aerodrome: bool) -> tuple[Section, ...]:
        """Which sections should exist for this entity.

        An aerodrome is expected to have AD 2.1 to AD 2.25 and nothing else; a
        State is expected to have everything that appears once.
        """
        if per_aerodrome:
            return aerodrome_sections()
        return sections_for(repeats=Repeat.ONCE)

    def gaps(self, entity: str, *, per_aerodrome: bool) -> tuple[SectionHolding, ...]:
        """Sections we cannot account for — ours, not the State's."""
        return tuple(
            h
            for h in (self.holding(entity, s.code) for s in self.expected(entity, per_aerodrome=per_aerodrome))
            if h.state.is_gap
        )

    def summary(self, entity: str, *, per_aerodrome: bool) -> dict[str, int]:
        expected = self.expected(entity, per_aerodrome=per_aerodrome)
        counts = {state.value: 0 for state in HoldingState}
        for candidate in expected:
            counts[self.holding(entity, candidate.code).state.value] += 1
        counts["expected"] = len(expected)
        return counts

    def render(self, entity: str, *, per_aerodrome: bool) -> str:
        """A coverage report. Every expected section appears, held or not."""
        counts = self.summary(entity, per_aerodrome=per_aerodrome)
        scope = "AD 2" if per_aerodrome else "GEN, ENR and AD"
        lines = [
            f"{entity} — {scope}",
            f"  {counts['held']} of {counts['expected']} sections held"
            f"  ·  {counts['absent']} not published by the State"
            f"  ·  {counts['failed'] + counts['not_checked']} unaccounted for",
            "",
        ]
        for candidate in self.expected(entity, per_aerodrome=per_aerodrome):
            holding = self.holding(entity, candidate.code)
            mark = {
                HoldingState.HELD: "  ",
                HoldingState.ABSENT: "--",
                HoldingState.FAILED: "!!",
                HoldingState.NOT_CHECKED: "??",
            }[holding.state]
            lines.append(f"  {mark}  {candidate.code:9} {candidate.title}")
        return "\n".join(lines)

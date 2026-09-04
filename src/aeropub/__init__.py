"""AeroPub — fleet-aware analysis of aeronautical publications.

The two questions the platform answers, and where each is assembled:

- *"Tell me everything about this."* — :func:`aeropub.dossier.build`
- *"Something was published; what does it mean?"* — :func:`aeropub.bulletin.between_cycles`
- *"What changes next, including what nobody will announce?"* — :func:`aeropub.horizon.horizon`
- *"How does this State actually publish?"* — :func:`aeropub.quality.assess_quality`

Each of those is one body of evidence. :func:`aeropub.lenses.view` arranges it
for one of six readers without ever filtering away a coverage gap, and
:func:`aeropub.api.document` emits any of them as JSON with the citation
still attached to every value.

Everything below those is the machinery they stand on: the AIRAC calendar as
the time spine, :class:`Fact` and :class:`SourceRef` as the attributed core,
:class:`FactStore` resolving the Consolidated Effective State, the AIP index
saying what a publication should contain, and the source registry saying what
we are actually watching.

The connectors live in subpackages — :mod:`aeropub.faa` today — and are not
imported here, so ``import aeropub`` costs nothing a caller has not asked for.
"""

from aeropub.acap import load_aircraft, merge
from aeropub.aip import (
    AipCoverage,
    HoldingState,
    Section,
    SectionHolding,
    aerodrome_sections,
    section,
)
from aeropub.aircraft import (
    AircraftType,
    Pcn,
    PavementCheck,
    Characteristic,
    Origin,
    PavementVerdict,
    accommodates,
    code_letter,
    code_number,
    compare_pavement,
    reference_code,
    rffs_category,
)
from aeropub.airac import AiracCycle, current_cycle, cycle_for, cycles_in_year
from aeropub.changes import Change, ChangeKind, diff_cycles, diff_effective
from aeropub.facts import Fact, FactStore, Precedence
from aeropub.impact import Direction, Impact, assess
from aeropub.ingest import load_facts
from aeropub.manifest import ManifestError, sha256_of
from aeropub.provenance import Confidence, SourceRef
from aeropub.quality import FindingKind, QualityFinding, QualityReport, assess_quality
from aeropub.store import SqliteFactStore, open_store
from aeropub.suitability import (
    Assessment,
    Check,
    Note,
    Suitability,
    assess_suitability,
    minimum_runway_width_m,
)
from aeropub.api import Licensing, document, dumps, ndjson, to_json
from aeropub.bulletin import Attention, ChangeBulletin, between_cycles, compile_bulletin
from aeropub.dossier import AerodromeDossier, SectionEntry, build_dossier
from aeropub.entities import aerodrome_of, covers, scope_of
from aeropub.horizon import Horizon, Transition, Trigger, horizon
from aeropub.lenses import LENSES, Audience, Lens, LensView, lens_for, view
from aeropub.notam import Notam, NotamKind, QLine
from aeropub.notam_register import (
    ForceState,
    NotamRegister,
    RegisteredNotam,
    Subject,
    SubjectKind,
)
from aeropub.validation import Finding, Severity, validate
from aeropub.render import render_dossier
from aeropub.registry import (
    CheckOutcome,
    CredentialRef,
    CredentialStatus,
    DetectionTier,
    Freshness,
    Redistribution,
    Source,
    SourceFormat,
    SourceKind,
    SourceRegistry,
    SourceState,
    StatusRow,
    render_board,
)

__all__ = [
    "accommodates",
    "aerodrome_of",
    "aerodrome_sections",
    "AerodromeDossier",
    "AipCoverage",
    "AiracCycle",
    "AircraftType",
    "assess",
    "assess_quality",
    "assess_suitability",
    "Assessment",
    "Attention",
    "Audience",
    "between_cycles",
    "build_dossier",
    "Change",
    "ChangeBulletin",
    "ChangeKind",
    "Characteristic",
    "Check",
    "CheckOutcome",
    "code_letter",
    "code_number",
    "compare_pavement",
    "compile_bulletin",
    "Confidence",
    "covers",
    "CredentialRef",
    "CredentialStatus",
    "current_cycle",
    "cycle_for",
    "cycles_in_year",
    "DetectionTier",
    "diff_cycles",
    "diff_effective",
    "Direction",
    "document",
    "dumps",
    "Fact",
    "FactStore",
    "Finding",
    "FindingKind",
    "ForceState",
    "Freshness",
    "HoldingState",
    "horizon",
    "Horizon",
    "Impact",
    "Lens",
    "lens_for",
    "LENSES",
    "LensView",
    "Licensing",
    "load_aircraft",
    "load_facts",
    "ManifestError",
    "merge",
    "minimum_runway_width_m",
    "ndjson",
    "Notam",
    "NotamKind",
    "NotamRegister",
    "Note",
    "open_store",
    "Origin",
    "PavementCheck",
    "PavementVerdict",
    "Pcn",
    "Precedence",
    "QLine",
    "QualityFinding",
    "QualityReport",
    "Redistribution",
    "reference_code",
    "RegisteredNotam",
    "render_board",
    "render_dossier",
    "rffs_category",
    "scope_of",
    "section",
    "Section",
    "SectionEntry",
    "SectionHolding",
    "Severity",
    "sha256_of",
    "Source",
    "SourceFormat",
    "SourceKind",
    "SourceRef",
    "SourceRegistry",
    "SourceState",
    "SqliteFactStore",
    "StatusRow",
    "Subject",
    "SubjectKind",
    "Suitability",
    "to_json",
    "Transition",
    "Trigger",
    "validate",
    "view",
]

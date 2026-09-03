"""AeroPub — fleet-aware analysis of aeronautical publications.

The two questions the platform answers, and where each is assembled:

- *"Tell me everything about this."* — :func:`aeropub.dossier.build`
- *"Something was published; what does it mean?"* — :func:`aeropub.bulletin.between_cycles`
- *"What changes next, including what nobody will announce?"* — :func:`aeropub.horizon.horizon`
- *"How does this State actually publish?"* — :func:`aeropub.quality.assess_quality`

Each of those is one body of evidence. :func:`aeropub.lenses.view` arranges it
for one of six readers without ever filtering away a coverage gap.

Everything below those is the machinery they stand on: the AIRAC calendar as
the time spine, :class:`Fact` and :class:`SourceRef` as the attributed core,
:class:`FactStore` resolving the Consolidated Effective State, the AIP index
saying what a publication should contain, and the source registry saying what
we are actually watching.

The connectors live in subpackages — :mod:`aeropub.faa` today — and are not
imported here, so ``import aeropub`` costs nothing a caller has not asked for.
"""

from aeropub.aip import (
    AipCoverage,
    HoldingState,
    Section,
    SectionHolding,
    aerodrome_sections,
    section,
)
from aeropub.airac import AiracCycle, current_cycle, cycle_for, cycles_in_year
from aeropub.changes import Change, ChangeKind, diff_cycles, diff_effective
from aeropub.facts import Fact, FactStore, Precedence
from aeropub.impact import Direction, Impact, assess
from aeropub.provenance import Confidence, SourceRef
from aeropub.quality import FindingKind, QualityFinding, QualityReport, assess_quality
from aeropub.store import SqliteFactStore, open_store
from aeropub.bulletin import Attention, ChangeBulletin, between_cycles, compile_bulletin
from aeropub.dossier import AerodromeDossier, SectionEntry, build_dossier
from aeropub.entities import aerodrome_of, covers, scope_of
from aeropub.horizon import Horizon, Transition, Trigger, horizon
from aeropub.lenses import LENSES, Audience, Lens, LensView, lens_for, view
from aeropub.store import SqliteFactStore, open_store
from aeropub.notam import Notam, NotamKind, QLine
from aeropub.notam_register import (
    ForceState,
    NotamRegister,
    RegisteredNotam,
    Subject,
    SubjectKind,
)
from aeropub.validation import Finding, Severity, validate
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
    "view",
    "lens_for",
    "LensView",
    "Lens",
    "Audience",
    "LENSES",
    "open_store",
    "assess_quality",
    "SqliteFactStore",
    "QualityReport",
    "QualityFinding",
    "FindingKind",
    "open_store",
    "SqliteFactStore",
    "horizon",
    "Trigger",
    "Transition",
    "Horizon",
    "section",
    "scope_of",
    "covers",
    "compile_bulletin",
    "build_dossier",
    "between_cycles",
    "aerodrome_sections",
    "aerodrome_of",
    "SubjectKind",
    "Subject",
    "SectionHolding",
    "SectionEntry",
    "Section",
    "RegisteredNotam",
    "NotamRegister",
    "HoldingState",
    "ForceState",
    "ChangeBulletin",
    "Attention",
    "AipCoverage",
    "AerodromeDossier",
    "AiracCycle",
    "Change",
    "ChangeKind",
    "CheckOutcome",
    "Confidence",
    "Direction",
    "CredentialRef",
    "CredentialStatus",
    "DetectionTier",
    "Fact",
    "FactStore",
    "Finding",
    "Impact",
    "Notam",
    "NotamKind",
    "Freshness",
    "Precedence",
    "QLine",
    "Redistribution",
    "Source",
    "SourceFormat",
    "SourceKind",
    "SourceRef",
    "SourceRegistry",
    "SourceState",
    "StatusRow",
    "Severity",
    "assess",
    "current_cycle",
    "cycle_for",
    "diff_cycles",
    "diff_effective",
    "validate",
    "cycles_in_year",
    "render_board",
]

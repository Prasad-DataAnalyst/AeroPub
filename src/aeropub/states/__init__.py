"""Per-State knowledge.

States do not publish alike. One offers a structured eAIP with an amendment
index and a machine-readable dataset catalogue; the next offers a single PDF
behind a portal, in a language that is not English, with no supplement listing
at all. There is no universal reader, and pretending otherwise is how coverage
projects fail.

So each State gets a module here, added deliberately, holding what that State
actually publishes and where. Breadth comes from accumulating these, not from
one clever crawler.

Two distinctions this package insists on:

**Absent is not the same as unchecked.** If a State publishes no separate AIC
index, that is a recorded property of the State, declared in
:attr:`StateProfile.absent`. It is not a hole in our coverage, and the two must
never look alike on a status board.

**Registered is not the same as verified.** A URL in a profile is a claim until
a human confirms it serves what we think. :attr:`Source.verified_at` records
that confirmation, and until it exists the board says ``unverified``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from aeropub.registry import Source, SourceKind

__all__ = ["StateProfile", "profiles", "get_profile"]


@dataclass(frozen=True, slots=True)
class StateProfile:
    """What one State publishes, and where."""

    code: str
    """ICAO location indicator prefix, e.g. ``"OT"`` for Qatar, ``"K"`` for the
    contiguous United States. Not the ISO country code — aeronautical work uses
    location indicators, and mixing the two causes subtle lookup bugs."""

    name: str
    authority: str
    """The AIS/AIM provider, as the State itself names it."""

    aim_url: str
    """The authority's own entry point, from which everything else hangs."""

    sources: tuple[Source, ...] = ()

    absent: frozenset[SourceKind] = field(default_factory=frozenset)
    """Kinds this State genuinely does not publish separately.

    Declaring absence is a positive statement, distinct from not having looked.
    """

    verified_at: datetime | None = None
    """When a human last confirmed this profile against the authority's site."""

    notes: str = ""

    def __post_init__(self) -> None:
        if not self.code.strip():
            raise ValueError("StateProfile.code must be a non-empty string")
        if not self.name.strip():
            raise ValueError("StateProfile.name must be a non-empty string")
        ids = [s.source_id for s in self.sources]
        if len(ids) != len(set(ids)):
            raise ValueError(f"duplicate source ids in profile {self.code!r}")
        overlap = {s.kind for s in self.sources} & self.absent
        if overlap:
            raise ValueError(
                f"profile {self.code!r} declares {sorted(k.value for k in overlap)} "
                "both present and absent"
            )

    def source_kinds(self) -> frozenset[SourceKind]:
        return frozenset(s.kind for s in self.sources)

    def unknown_kinds(self) -> frozenset[SourceKind]:
        """Kinds neither registered nor declared absent — the real gaps."""
        return frozenset(SourceKind) - self.source_kinds() - self.absent

    def unverified_sources(self) -> tuple[Source, ...]:
        return tuple(s for s in self.sources if not s.is_verified)


def profiles() -> dict[str, StateProfile]:
    """Every State profile currently implemented, by location indicator prefix.

    The United States profile is built rather than imported, so its addresses
    reflect whichever NMS environment is configured now. A board showing
    production URLs for a connection pointed at staging would be wrong in the
    quiet way that matters.
    """
    from aeropub.states import qatar, saudi_arabia, united_states

    return {
        p.code: p
        for p in (qatar.PROFILE, saudi_arabia.PROFILE, united_states.profile())
    }


def get_profile(code: str) -> StateProfile:
    try:
        return profiles()[code.upper()]
    except KeyError:
        raise KeyError(
            f"no State profile for {code!r}; implemented: "
            f"{sorted(profiles())}"
        ) from None

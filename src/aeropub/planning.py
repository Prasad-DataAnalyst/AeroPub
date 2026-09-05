"""ENR 1.10 — flight planning, and the deadline that has already passed.

Everything else this platform screens is about the air. This section is about
the paperwork, and for a non-scheduled operation the paperwork is what stops
the flight. A trip planned at four hours' notice through a State that requires
the plan twenty-four hours ahead is not a trip with a filing problem — it is
not a trip.

Three things ENR 1.10 publishes are arithmetic, and this module does the
arithmetic:

**Minimum notice before EOBT.** One hour is the common case and it is not the
only case. The screen takes the same ``notice_hours`` the overflight-clearance
screen in :mod:`aeropub.hazards` takes, so one number answers both questions —
whether the diplomatic clearance can still be obtained, and whether the plan
can still be filed.

**A window has two ends.** Several States refuse a plan submitted more than
some hours ahead. Being early is a smaller problem than being late — it defers
rather than blocks — but a dispatcher who files at T-6 days into a State with
a 120-hour limit has filed nothing, and nothing is what the ATS unit will have.

**Item 18 indicators the State requires.** A plan missing one is a plan that
gets rejected or, worse, accepted and wrong. Given the Item 18 as filed, this
says which required indicators are absent, per State.

What it does not do
-------------------
It does not file anything, address anything, or validate a whole flight plan.
Item 18 is parsed only far enough to see which indicators are present.

And it does not guess at the indicator set. Splitting on anything that looks
like ``XXX/`` would find a key inside a free-text remark — ``RMK/CONTACT OPS
ON/OFF FREQ`` would split at ``ON/`` — and an invented key is worse than a
missed one here, because a required indicator would then read as present. The
parser recognises a closed list of indicators and reports anything else that
looks like one rather than acting on it.

Four states again
-----------------
A filing window resolves to ``IN_WINDOW``, ``LATE``, ``TOO_EARLY``,
``NO_WINDOW_PUBLISHED`` (read, and publishing no minimum) or ``UNREAD``. The
last two are not answers, and they are kept apart from each other because they
need different things done: one needs somebody to read an AIP, the other needs
somebody to ask the State.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Iterable, Mapping

from aeropub.entities import normalise
from aeropub.facts import SourceRef
from aeropub.manifest import (
    ManifestError,
    document_source,
    read_manifest,
    sub_source,
)

__all__ = [
    "Acceptance",
    "DelayFinding",
    "FilingChannel",
    "FilingFinding",
    "FilingRule",
    "ITEM18_INDICATORS",
    "Item18Finding",
    "PlanKind",
    "PlanningRegister",
    "PlanningView",
    "Timeliness",
    "load_planning",
    "parse_item18",
    "planning_template",
    "screen_delay",
    "screen_filing",
    "screen_item18",
    "view_planning",
]

#: The parser identity written into citations read from an ENR 1.10 manifest.
PLANNING_PARSER_ID = "aeropub.planning"

#: The Item 18 indicators this parser recognises, from PANS-ATM Appendix 2.
#:
#: A closed list on purpose. Splitting on anything shaped like ``XXX/`` finds
#: keys inside free text — ``RMK/CONTACT OPS ON/OFF FREQ`` would split at
#: ``ON/`` — and an invented indicator makes a required one read as present,
#: which is the wrong direction to be wrong in. Anything else that looks like
#: an indicator is reported rather than acted on, because regional indicators
#: exist and this list does not claim to be every one of them.
ITEM18_INDICATORS: frozenset[str] = frozenset(
    {
        "STS", "PBN", "NAV", "COM", "DAT", "SUR", "DEP", "DEST", "DOF",
        "REG", "EET", "SEL", "TYP", "CODE", "DLE", "OPR", "ORGN", "PER",
        "ALTN", "RALT", "TALT", "RIF", "RMK",
    }
)

_LOOKS_LIKE_INDICATOR = re.compile(r"\b([A-Z][A-Z0-9]{1,4})/")


def _span(hours: float, *, working_days: bool = False) -> str:
    """A published interval, written the way a person would say it."""
    unit = "working hour" if working_days else "hour"
    return f"{hours:g} {unit}" + ("" if hours == 1 else "s")


class PlanKind(str, Enum):
    """Which message the rule is about."""

    INDIVIDUAL = "individual"
    REPETITIVE = "repetitive"
    """An RPL. Filed weeks ahead against a season, and a State that does not
    accept them turns one submission into one per flight."""

    CHANGE = "change"
    DELAY = "delay"
    CANCELLATION = "cancellation"


class FilingChannel(str, Enum):
    """Where the State says to submit it."""

    ARO = "aro"
    """An office with opening hours. The one that can be shut when you need
    it."""

    AFTN = "aftn"
    IFPS = "ifps"
    PORTAL = "portal"
    EMAIL = "email"
    FAX = "fax"
    OTHER = "other"

    @property
    def has_opening_hours(self) -> bool:
        """Whether it is a place that can be closed rather than a service."""
        return self is FilingChannel.ARO


class Acceptance(str, Enum):
    """Whether the State takes this kind of plan at all."""

    ACCEPTED = "accepted"
    CONDITIONAL = "conditional"
    NOT_ACCEPTED = "not_accepted"
    NOT_STATED = "not_stated"

    @property
    def is_accepted(self) -> bool | None:
        """``None`` where the extract did not say. Never assumed either way."""
        if self is Acceptance.NOT_STATED:
            return None
        return self is not Acceptance.NOT_ACCEPTED


class Timeliness(str, Enum):
    """Where the notice available falls against the published window."""

    IN_WINDOW = "in_window"
    LATE = "late"
    TOO_EARLY = "too_early"
    NO_WINDOW_PUBLISHED = "no_window_published"
    UNREAD = "unread"

    @property
    def can_file_now(self) -> bool | None:
        """``None`` for the two that are not answers.

        ``TOO_EARLY`` is ``False`` and not a softer thing: a plan the State
        will not accept yet is a plan that does not exist, however early it was
        sent. It differs from ``LATE`` in the remedy, not in the answer.
        """
        if self in (Timeliness.NO_WINDOW_PUBLISHED, Timeliness.UNREAD):
            return None
        return self is Timeliness.IN_WINDOW

    @property
    def blocks_departure(self) -> bool:
        """Whether no amount of waiting fixes it."""
        return self is Timeliness.LATE


@dataclass(frozen=True, slots=True)
class FilingRule:
    """One State's flight-planning requirement, as ENR 1.10 publishes it."""

    region: str
    source: SourceRef
    plan_kind: PlanKind = PlanKind.INDIVIDUAL
    acceptance: Acceptance = Acceptance.NOT_STATED
    minimum_notice_hours: float | None = None
    """How far before EOBT the plan must be in. ``None`` where the extract
    published none, which is reported as not knowing and never as no
    deadline."""

    maximum_notice_hours: float | None = None
    """How far ahead the State will accept one. The other end of the window,
    and the one nobody remembers."""

    working_days: bool = False
    """Whether the notice is counted in working days. An RPL deadline of ten
    days across two weekends is fourteen, which is the arithmetic a seasonal
    submission gets wrong."""

    channel: FilingChannel = FilingChannel.OTHER
    address: str = ""
    """The AFTN address, portal or office, as published."""

    office_hours: str = ""
    """When it is open, where the channel is an office. A deadline met at an
    office that is shut is a deadline missed."""

    required_item18: tuple[str, ...] = ()
    """Indicators the State requires in Item 18."""

    delay_tolerance_minutes: float | None = None
    """How far EOBT may slip before the plan has to be replaced rather than
    delayed. Thirty minutes is the common figure and not the only one."""

    applies_to: str = ""
    """Which operations it binds — "non-scheduled", "international", "IFR". A
    requirement that does not apply is not a finding."""

    conditions: str = ""
    remarks: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "region", normalise(self.region))
        object.__setattr__(
            self,
            "required_item18",
            tuple(
                str(k).strip().upper().rstrip("/")
                for k in self.required_item18
                if str(k).strip()
            ),
        )
        if not self.region:
            raise ValueError(
                "FilingRule.region must be a non-empty string. A filing "
                "deadline with no State attached would be read as global."
            )
        for field in ("plan_kind", "acceptance", "channel"):
            value = getattr(self, field)
            expected = {
                "plan_kind": PlanKind,
                "acceptance": Acceptance,
                "channel": FilingChannel,
            }[field]
            if not isinstance(value, expected):
                raise TypeError(f"FilingRule.{field} must be a {expected.__name__}")
        if not isinstance(self.source, SourceRef):
            raise TypeError("FilingRule.source must be a SourceRef")
        low, high = self.minimum_notice_hours, self.maximum_notice_hours
        if low is not None and high is not None and low > high:
            raise ValueError(
                f"{self.region}: a window opening {low:g} hours before EOBT and "
                f"closing {high:g} hours before it never opens. One of the two "
                "was read from the wrong row."
            )

    @property
    def window_known(self) -> bool:
        return (
            self.minimum_notice_hours is not None
            or self.maximum_notice_hours is not None
        )

    def timeliness(self, notice_hours: float) -> Timeliness:
        """Where this much notice falls against the published window."""
        if not self.window_known:
            return Timeliness.NO_WINDOW_PUBLISHED
        if (
            self.minimum_notice_hours is not None
            and notice_hours < self.minimum_notice_hours
        ):
            return Timeliness.LATE
        if (
            self.maximum_notice_hours is not None
            and notice_hours > self.maximum_notice_hours
        ):
            return Timeliness.TOO_EARLY
        return Timeliness.IN_WINDOW

    def describe(self) -> str:
        parts = [f"{self.region} {self.plan_kind.value}"]
        if self.acceptance is not Acceptance.NOT_STATED:
            parts.append(self.acceptance.value.replace("_", " "))
        if self.minimum_notice_hours is not None:
            parts.append(
                "not later than "
                f"{_span(self.minimum_notice_hours, working_days=self.working_days)}"
                " before EOBT"
            )
        if self.maximum_notice_hours is not None:
            parts.append(
                "not earlier than "
                f"{_span(self.maximum_notice_hours, working_days=self.working_days)}"
                " before EOBT"
            )
        if self.channel is not FilingChannel.OTHER:
            where = f"via {self.channel.value.upper()}"
            if self.address:
                where += f" ({self.address})"
            parts.append(where)
        if self.office_hours:
            parts.append(self.office_hours)
        if self.required_item18:
            parts.append("Item 18: " + ", ".join(self.required_item18))
        if self.applies_to:
            parts.append(f"applies to {self.applies_to}")
        return "  ·  ".join(parts)


@dataclass(frozen=True, slots=True)
class FilingFinding:
    """One State's window, and where this flight's notice falls in it."""

    region: str
    timeliness: Timeliness
    rule: FilingRule | None = None
    notice_hours: float | None = None

    @property
    def can_file_now(self) -> bool | None:
        return self.timeliness.can_file_now

    @property
    def short_by_hours(self) -> float | None:
        """How much notice is missing. ``None`` unless the answer is late."""
        if self.timeliness is not Timeliness.LATE or self.rule is None:
            return None
        return (self.rule.minimum_notice_hours or 0.0) - (self.notice_hours or 0.0)

    def describe(self) -> str:
        if self.timeliness is Timeliness.UNREAD:
            return f"{self.region}: no ENR 1.10 read for this region"
        if self.timeliness is Timeliness.NO_WINDOW_PUBLISHED:
            return (
                f"{self.region}: read, and publishes no filing deadline. Ask "
                "the State rather than assuming there is none"
            )
        if self.rule is None:
            return f"{self.region}: {self.timeliness.value.replace('_', ' ')}"
        working = self.rule.working_days
        if self.timeliness is Timeliness.LATE:
            short = self.short_by_hours or 0.0
            text = (
                f"{self.region}: needs "
                f"{_span(self.rule.minimum_notice_hours or 0.0, working_days=working)}"
                f" and there are {self.notice_hours:g} — short by {short:g}"
            )
            if working:
                text += " (counted in working days, so a weekend makes it worse)"
            return text
        if self.timeliness is Timeliness.TOO_EARLY:
            return (
                f"{self.region}: will not accept a plan more than "
                f"{_span(self.rule.maximum_notice_hours or 0.0, working_days=working)}"
                f" ahead, and there are {self.notice_hours:g} — file later"
            )
        return f"{self.region}: within the published window"


@dataclass(frozen=True, slots=True)
class Item18Finding:
    """An indicator one State requires and the filed Item 18 does not carry."""

    region: str
    indicator: str
    rule: FilingRule

    def describe(self) -> str:
        return (
            f"{self.region}: Item 18 requires {self.indicator}/ and the filed "
            "Item 18 does not carry it"
        )


@dataclass(frozen=True, slots=True)
class DelayFinding:
    """A slip in EOBT past what the State lets a delay message cover."""

    region: str
    rule: FilingRule
    slip_minutes: float

    def describe(self) -> str:
        return (
            f"{self.region}: EOBT has slipped {self.slip_minutes:g} minutes and "
            f"a delay message covers {self.rule.delay_tolerance_minutes:g} — "
            "the plan must be replaced, not delayed"
        )


def screen_filing(
    rules: Iterable[FilingRule],
    *,
    notice_hours: float,
    plan_kind: PlanKind = PlanKind.INDIVIDUAL,
) -> tuple[FilingFinding, ...]:
    """Where this much notice falls against each published window.

    Every rule of the requested kind produces a finding, including the ones
    that are met. A screen that returned only the failures would be shorter
    when coverage was worse, which is the reading this platform exists to stop.
    """
    return tuple(
        FilingFinding(
            region=rule.region,
            timeliness=rule.timeliness(notice_hours),
            rule=rule,
            notice_hours=notice_hours,
        )
        for rule in rules
        if rule.plan_kind is plan_kind
    )


def parse_item18(text: str) -> tuple[dict[str, str], tuple[str, ...]]:
    """Split a filed Item 18 into indicators, and flag what looks like one.

    Returns the indicators found and the tokens that are shaped like an
    indicator but are not in :data:`ITEM18_INDICATORS`. The second half is the
    honest part: regional indicators exist, and a token this parser does not
    know is reported rather than either acted on or silently swallowed.
    """
    filed = str(text or "").strip()
    if not filed:
        return {}, ()

    hits = [
        (match.start(), match.group(1))
        for match in _LOOKS_LIKE_INDICATOR.finditer(filed.upper())
    ]
    known = [(start, key) for start, key in hits if key in ITEM18_INDICATORS]
    suspect = tuple(
        dict.fromkeys(key for _start, key in hits if key not in ITEM18_INDICATORS)
    )

    found: dict[str, str] = {}
    for index, (start, key) in enumerate(known):
        end = known[index + 1][0] if index + 1 < len(known) else len(filed)
        value = filed[start + len(key) + 1 : end].strip()
        # A repeated indicator keeps the first: a State reading the plan reads
        # it left to right, and picking the last would answer differently from
        # the unit that has to act on it.
        found.setdefault(key, value)
    return found, suspect


def screen_item18(
    rules: Iterable[FilingRule], *, item18: str
) -> tuple[Item18Finding, ...]:
    """Which required indicators the filed Item 18 does not carry."""
    filed, _suspect = parse_item18(item18)
    findings: list[Item18Finding] = []
    for rule in rules:
        for indicator in rule.required_item18:
            if indicator not in filed:
                findings.append(
                    Item18Finding(
                        region=rule.region, indicator=indicator, rule=rule
                    )
                )
    return tuple(findings)


def screen_delay(
    rules: Iterable[FilingRule], *, slip_minutes: float
) -> tuple[DelayFinding, ...]:
    """Where the slip in EOBT is past what a delay message covers.

    A rule publishing no tolerance produces nothing here. "No tolerance
    published" and "you are past the tolerance" are different problems and
    only the second is arithmetic.
    """
    return tuple(
        DelayFinding(region=rule.region, rule=rule, slip_minutes=slip_minutes)
        for rule in rules
        if rule.delay_tolerance_minutes is not None
        and slip_minutes > rule.delay_tolerance_minutes
    )


@dataclass(frozen=True, slots=True)
class PlanningRegister:
    """Every ENR 1.10 rule read so far, and where it was read.

    ``covers`` carries the same weight it does in :mod:`aeropub.gnss`: a region
    in it has been read, and one not in it has not. Without that a State whose
    ENR 1.10 publishes no deadline is indistinguishable from a State nobody
    looked at.
    """

    rules: tuple[FilingRule, ...] = ()
    covers: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "covers",
            frozenset(normalise(r) for r in self.covers if normalise(r))
            | {r.region for r in self.rules},
        )

    def __len__(self) -> int:
        return len(self.rules)

    def __iter__(self):
        return iter(self.rules)

    @property
    def regions(self) -> tuple[str, ...]:
        return tuple(sorted(self.covers))

    def is_read(self, region: str) -> bool:
        return normalise(region) in self.covers

    def in_region(self, region: str) -> tuple[FilingRule, ...]:
        wanted = normalise(region)
        if not wanted:
            return ()
        return tuple(r for r in self.rules if r.region == wanted)

    def of_kind(
        self, kind: PlanKind, *, region: str = ""
    ) -> tuple[FilingRule, ...]:
        held = self.in_region(region) if region else self.rules
        return tuple(r for r in held if r.plan_kind is kind)


@dataclass(frozen=True, slots=True)
class PlanningView:
    """What the crossed States require before this plan is a plan."""

    regions: tuple[str, ...] = ()
    rules: tuple[FilingRule, ...] = ()
    unread_regions: tuple[str, ...] = ()
    filing: tuple[FilingFinding, ...] = ()
    item18: tuple[Item18Finding, ...] = ()
    delays: tuple[DelayFinding, ...] = ()
    unrecognised_item18: tuple[str, ...] = ()
    repetitive_refused: tuple[FilingRule, ...] = ()
    notice_hours: float | None = None

    @property
    def is_conclusive(self) -> bool:
        return not self.unread_regions

    @property
    def late(self) -> tuple[FilingFinding, ...]:
        return tuple(f for f in self.filing if f.timeliness is Timeliness.LATE)

    @property
    def early(self) -> tuple[FilingFinding, ...]:
        return tuple(
            f for f in self.filing if f.timeliness is Timeliness.TOO_EARLY
        )

    @property
    def unanswered(self) -> tuple[FilingFinding, ...]:
        """Windows nobody can speak for — unread, or read and unpublished."""
        return tuple(f for f in self.filing if f.can_file_now is None)

    def render(self) -> str:
        lines = [
            "FLIGHT PLANNING — whether this plan can still be filed",
            f"{len(self.regions)} regions  ·  {len(self.rules)} published rules"
            + (
                f"  ·  {self.notice_hours:g} hours of notice"
                if self.notice_hours is not None
                else "  ·  no notice figure supplied, so no window was screened"
            ),
        ]
        if self.unread_regions:
            lines += [
                "",
                f"!! no ENR 1.10 has been read for "
                f"{', '.join(self.unread_regions)}. There may be a deadline",
                "   there and there may not, and neither reading is available "
                "from what is held.",
            ]
        if self.late:
            lines += ["", "TOO LATE TO FILE"]
            for finding in self.late:
                lines.append(f"  {finding.describe()}")
        if self.early:
            lines += ["", "TOO EARLY TO FILE — file later, not never"]
            for finding in self.early:
                lines.append(f"  {finding.describe()}")
        unanswered = [
            f for f in self.unanswered if f.timeliness is Timeliness.NO_WINDOW_PUBLISHED
        ]
        if unanswered:
            lines += ["", "NO DEADLINE PUBLISHED"]
            for finding in unanswered:
                lines.append(f"  {finding.describe()}")
        if self.repetitive_refused:
            lines += ["", "REPETITIVE PLANS NOT ACCEPTED — one plan per flight"]
            for rule in self.repetitive_refused:
                lines.append(f"  {rule.describe()}")
        if self.item18:
            lines += ["", "ITEM 18 — required and not filed"]
            for finding in self.item18:
                lines.append(f"  {finding.describe()}")
        if self.unrecognised_item18:
            lines += [
                "",
                "ITEM 18 — tokens shaped like an indicator this parser does "
                "not know:",
                f"  {', '.join(self.unrecognised_item18)}",
                "  Reported rather than acted on. Regional indicators exist "
                "and this list does not claim to be every one.",
            ]
        if self.delays:
            lines += ["", "EOBT SLIP"]
            for finding in self.delays:
                lines.append(f"  {finding.describe()}")
        if self.rules:
            lines += ["", "PUBLISHED"]
            for rule in self.rules:
                lines.append(f"  {rule.describe()}")
        return "\n".join(lines)


def view_planning(
    register: PlanningRegister,
    *,
    regions: Iterable[str],
    notice_hours: float | None = None,
    item18: str = "",
    slip_minutes: float | None = None,
    plan_kind: PlanKind = PlanKind.INDIVIDUAL,
) -> PlanningView:
    """What ENR 1.10 requires of this flight in these regions.

    ``notice_hours`` is the same figure the overflight-clearance screen takes,
    so one number answers both questions. Without it no window is screened and
    the view says so rather than reporting every window as met.
    """
    wanted = tuple(dict.fromkeys(normalise(r) for r in regions if normalise(r)))

    rules: list[FilingRule] = []
    unread: list[str] = []
    for region in wanted:
        if not register.is_read(region):
            unread.append(region)
            continue
        rules.extend(register.in_region(region))

    filing: list[FilingFinding] = []
    for region in unread:
        filing.append(
            FilingFinding(region=region, timeliness=Timeliness.UNREAD)
        )
    if notice_hours is not None:
        filing.extend(
            screen_filing(rules, notice_hours=notice_hours, plan_kind=plan_kind)
        )
        # A region declared read and carrying no rule of this kind would
        # otherwise disappear from the screen entirely, which reads as nothing
        # to worry about. It is the same answer a rule with no window gives:
        # somebody looked, and there is nothing here to look at.
        screened = {f.region for f in filing}
        for region in wanted:
            if region in screened or region in unread:
                continue
            filing.append(
                FilingFinding(
                    region=region,
                    timeliness=Timeliness.NO_WINDOW_PUBLISHED,
                    notice_hours=notice_hours,
                )
            )

    _filed, suspect = parse_item18(item18)
    findings = screen_item18(rules, item18=item18) if item18 else ()
    delays = (
        screen_delay(rules, slip_minutes=slip_minutes)
        if slip_minutes is not None
        else ()
    )
    refused = tuple(
        r
        for r in rules
        if r.plan_kind is PlanKind.REPETITIVE
        and r.acceptance is Acceptance.NOT_ACCEPTED
    )

    return PlanningView(
        regions=wanted,
        rules=tuple(rules),
        unread_regions=tuple(unread),
        filing=tuple(filing),
        item18=findings,
        delays=delays,
        unrecognised_item18=suspect,
        repetitive_refused=refused,
        notice_hours=notice_hours,
    )


# --------------------------------------------------------------------------
# Reading an ENR 1.10 manifest
# --------------------------------------------------------------------------


def _duration(value: object, *, where: str, field: str) -> float | None:
    """A published interval, in whatever unit the field is named for."""
    if value is None or value == "":
        return None
    try:
        interval = float(value)
    except (TypeError, ValueError):
        raise ManifestError(
            f"{where}: {field} {value!r} is not a number. A deadline that "
            "cannot be read is left unread — a rounded one is a deadline "
            "nobody published."
        ) from None
    if interval < 0:
        raise ManifestError(f"{where}: {field} cannot be negative")
    return interval


def _enum(enum_type, value: object, *, where: str, field: str):
    try:
        return enum_type(str(value).strip().lower())
    except ValueError:
        allowed = ", ".join(member.value for member in enum_type)
        raise ManifestError(f"{where}: {field} must be one of {allowed}") from None


def load_planning(path: Path | str) -> PlanningRegister:
    """Read one ENR 1.10 extract, with every rule cited to it."""
    path = Path(path)
    manifest = read_manifest(path)
    document = document_source(
        manifest.get("source"),
        base=path.parent,
        where=f"{path}: source",
        parser_id=PLANNING_PARSER_ID,
    )
    default_region = str(manifest.get("region", "")).strip()

    covers = manifest.get("covers", [])
    if not isinstance(covers, list):
        raise ManifestError(
            f"{path}: covers must be a list of the regions this extract was "
            "read for"
        )
    if default_region:
        covers = list(covers) + [default_region]

    rows = manifest.get("rules", [])
    if not isinstance(rows, list):
        raise ManifestError(f"{path}: rules must be a list")

    rules: list[FilingRule] = []
    for index, row in enumerate(rows):
        where = f"{path}: rules[{index}]"
        if not isinstance(row, Mapping):
            raise ManifestError(f"{where}: must be an object")
        locator = str(row.get("locator", "")).strip()
        if not locator:
            raise ManifestError(
                f"{where}: locator is required — which paragraph of ENR 1.10 "
                "this came from."
            )
        try:
            rules.append(
                FilingRule(
                    region=str(row.get("region", default_region)),
                    source=sub_source(document, locator),
                    plan_kind=_enum(
                        PlanKind,
                        row.get("plan_kind", PlanKind.INDIVIDUAL.value),
                        where=where,
                        field="plan_kind",
                    ),
                    acceptance=_enum(
                        Acceptance,
                        row.get("acceptance", Acceptance.NOT_STATED.value),
                        where=where,
                        field="acceptance",
                    ),
                    minimum_notice_hours=_duration(
                        row.get("minimum_notice_hours"),
                        where=where,
                        field="minimum_notice_hours",
                    ),
                    maximum_notice_hours=_duration(
                        row.get("maximum_notice_hours"),
                        where=where,
                        field="maximum_notice_hours",
                    ),
                    working_days=bool(row.get("working_days", False)),
                    channel=_enum(
                        FilingChannel,
                        row.get("channel", FilingChannel.OTHER.value),
                        where=where,
                        field="channel",
                    ),
                    address=str(row.get("address", "")).strip(),
                    office_hours=str(row.get("office_hours", "")).strip(),
                    required_item18=tuple(
                        str(k) for k in row.get("required_item18", [])
                    ),
                    delay_tolerance_minutes=_duration(
                        row.get("delay_tolerance_minutes"),
                        where=where,
                        field="delay_tolerance_minutes",
                    ),
                    applies_to=str(row.get("applies_to", "")).strip(),
                    conditions=str(row.get("conditions", "")).strip(),
                    remarks=str(row.get("remarks", "")).strip(),
                )
            )
        except (ValueError, TypeError) as error:
            raise ManifestError(f"{where}: {error}") from None

    return PlanningRegister(rules=tuple(rules), covers=frozenset(covers))


_PLANNING_TEMPLATE = {
    "source": {
        "source_id": "",
        "document": "",
        "document_path": "",
        "retrieved_at": "",
        "published_at": "",
        "original_url": "",
    },
    "region": "",
    "covers": [],
    "rules": [
        {
            "region": "",
            "plan_kind": "individual",
            "acceptance": "not_stated",
            "minimum_notice_hours": None,
            "maximum_notice_hours": None,
            "working_days": False,
            "channel": "aro",
            "address": "",
            "office_hours": "",
            "required_item18": [],
            "delay_tolerance_minutes": None,
            "applies_to": "",
            "conditions": "",
            "remarks": "",
            "locator": "",
        }
    ],
}


def planning_template() -> str:
    """A blank ENR 1.10 extract.

    ``covers`` lists every region this extract was read for, including any
    that publish no deadline at all. Without it, a State that publishes none
    is indistinguishable from a State nobody looked at, and those are opposite
    answers to "can we still file".

    ``minimum_notice_hours`` is how far before EOBT the plan must be in;
    ``maximum_notice_hours`` is how far ahead the State will accept one, which
    is the end of the window nobody remembers. ``working_days`` matters for
    the seasonal RPL deadlines: ten working days across two weekends is
    fourteen.
    """
    return json.dumps(_PLANNING_TEMPLATE, indent=2)

# ENR coverage — what is built, what is not, and why

The en-route part of an AIP is six parts and about thirty subsections. This is
the audit: every one of them, what the platform does with it today, and where
the remaining work is. It is kept in the repository rather than in a plan
document because it goes stale the moment a module lands, and a stale coverage
claim is worse than none.

Read `is_conclusive` on any en-route document as the runtime form of this
table: it is false while anything below is unread for the regions in question.

## ENR 0 — Preface, amendments, supplements, checklists

| Subsection | Content | Status |
|---|---|---|
| ENR 0.1–0.5 | Preface, record of amendments and supplements, checklist of pages | **Partial** — the checklist drives `OVERDUE` in `watcher.py` and coverage in `aip.py`. The page-level checklist reconciliation of §6 is not built |
| ENR 0.6 | Table of contents | Not held. Low value on its own |

**The gap that matters.** A State's own checklist is the audit trail against
what we hold. Reconciling it page by page at cycle close is the difference
between "we hold everything we fetched" and "we hold everything the State
says exists", and only the second is a coverage claim.

## ENR 1 — General rules and procedures

| Subsection | Content | Status |
|---|---|---|
| 1.1 | General rules | Not held |
| 1.2 | Visual flight rules | Not held. Out of scope for the operations this serves |
| 1.3 | Instrument flight rules | Not held |
| 1.4 | ATS airspace classification | **Built** — `airspace.py`, `AirspaceClass`. What each class answers about clearance, separation and VFR |
| 1.5 | Holding, approach and departure procedures | **Built** — `holding.py`. Level band, speed against the published limit or the PANS-OPS table, outbound timing, and the entry sector for an arrival heading including the 5° flexibility zone |
| 1.6 | ATS surveillance services | Not held |
| 1.7 | **Altimeter setting procedures** | **Built** — transition altitude and level per region, reported as boundaries in `route.py` |
| 1.8 | Regional supplementary procedures | Not held. Where a region's SUPPS differ from ICAO, a crew planning from Annex alone is wrong |
| 1.9 | ATFM and airspace management | Not held. Slots and CTOT are operational rather than published-state, and belong with a live feed |
| 1.10 | Flight planning | **Not built.** Filing requirements, repetitive plans, minimum notice — the charter variant needs it |
| 1.11 | Addressing of flight plan messages | Not held |
| 1.12 | Interception of civil aircraft | Not held. Procedural, and worth carrying for the conflict-zone case |
| 1.13 | Unlawful interference | Not held |
| 1.14 | Air traffic incidents | Not held |

## ENR 2 — ATS airspace

| Subsection | Content | Status |
|---|---|---|
| 2.1 | FIR, UIR, TMA, CTA | **Built** — `airspace.py`. Class, vertical limits, unit, frequency, carriage requirements |
| 2.2 | Other regulated airspace | **Built** — `AirspaceType.OTHER`, CTR, ATZ, FIZ, OCA |

**What it does.** Reports the *boundary* rather than a table of classes, names
the transition where IFR separation stops being provided, and takes the class
from the volume that reaches the planned level rather than the one that shares
the region's name.

**What it does not.** No geometry, so never a containment verdict.

## ENR 3 — ATS routes

| Subsection | Content | Status |
|---|---|---|
| 3.1 | Lower ATS routes | **Built** — `ats.py` |
| 3.2 | Upper ATS routes | **Built** — same model; upper and lower differ by the limits on the segment |
| 3.3 | Area navigation routes | **Built** — `navigation_spec` on the segment |
| 3.4 | Helicopter routes | **Built** — same model |
| 3.5 | Other routes | **Built** |
| 3.6 | En-route holding | **Built** — same model as ENR 1.5; a fix can carry an en-route hold and a missed-approach hold at once and both are held |

**What it does.** Parses Item 15, resolves each leg against the published
segments, screens the planned level for minimum, maximum, direction of
cruising levels and navigation specification, and lands NOTAM on every point
and airway.

## ENR 4 — Radio navigation aids and significant points

| Subsection | Content | Status |
|---|---|---|
| 4.1 | Radio navigation aids — en-route | **Built** — `navaids.py`. Ident, kind, frequency or channel, published coverage, status and hours, plus what each aid is published as serving. A NOTAM in force overrides the published status |
| 4.2 | Special navigation systems | Not held |
| 4.3 | GNSS | **Built** — `gnss.py`. Which GNSS elements the State approves, for which operations, on what conditions; which approach lines that authorises; the published RAIM prediction requirement and the provider named for it; and what GNSS is published as usable in lieu of |
| 4.4 | Name-code designators for significant points | **Built** — `ats.SignificantPoint` |
| 4.5 | Aeronautical ground lights — en-route | Not held. Low value for the operations this serves |

**What it does.** Answers one question the approach plate cannot: whether the
State authorises the minimum line printed on it. An LPV line is drawn from
SBAS, and a State that approves no SBAS service has not authorised it — nothing
on the plate says so. Answers per region, so a sector crossing an approving
State and a non-approving one gets two findings rather than an average.

**What it does not.** It computes no RAIM prediction and never will. A
prediction needs the current almanac, satellite health, the receiver model and
the exact time and place; none of those are held here. What is reported is the
published *requirement* and the provider the State names. It also does not rule
on substitution — `substitutions_for` lists what is published, and the choice
stays the operator's.

**The four states matter.** A capability comes back `PUBLISHED`, `WITHDRAWN`
(somebody decided against it), `NOT_PUBLISHED` (read here, and no such service
is approved) or `UNREAD`. The register tracks which regions an extract *covers*
as well as which have rows, because without that a single loaded row would make
every unmentioned element read as refused — a coverage gap silently promoted to
a finding.

## ENR 5 — Navigation warnings

| Subsection | Content | Status |
|---|---|---|
| 5.1 | Prohibited, restricted, danger areas | **Built** — `hazards.py`, three verbs kept apart |
| 5.2 | Military exercise and training areas, ADIZ | **Built** |
| 5.3 | Other activities of a dangerous nature | **Built** |
| 5.4 | Air navigation obstacles — en-route | **Built** as a register entry. The gradient analysis of `obstacles.py` is aerodrome-scoped and is not applied en route |
| 5.5 | Aerial sporting and recreational | **Built** |
| 5.6 | Bird migration and sensitive fauna | **Built**, with seasons as published months |

**What it does.** Screens by altitude only, keeps the three verbs apart, and
surfaces the by-NOTAM list as the pointer to the other half of the answer.
Overflight clearance lead times are screened against the notice available.

## ENR 6 — En-route charts

| Subsection | Content | Status |
|---|---|---|
| ENR 6 | En-route charts | **Partial** — `ChartKind.ENROUTE` exists in the chart register and reconciles like any other chart. Nothing extracts route structure from the chart itself, and nothing should: ENR 3 is the authority for that and the chart is the picture of it |

## Build order from here

1. **ENR 1.10 flight planning** — filing requirements and minimum notice, which
   the charter variant needs beside the clearance lead times already built.
2. **ENR 0 checklist reconciliation** — page-level, at cycle close.
3. **ENR 1.8 regional supplementary procedures** — where a region's SUPPS
   differ from the Annex, a crew planning from the Annex alone is wrong.

Everything above is buildable offline from published text.

**On geometry.** ENR 4.1 and 4.4 publish coordinates, so reading them is
reading the AIP, and `geo.py` and `planview.py` do exactly that: a point is
drawn where the State published it or it is not drawn at all. What the platform
still refuses is *derived* geometry — a boundary polygon assembled from a
prose description, a coverage footprint computed from anything but the
published figure, a containment verdict. The moment it answers "you are inside
this area" from geometry nobody published, it is producing the most dangerous
output it could produce. Reading a published position is not that; inventing
one is.

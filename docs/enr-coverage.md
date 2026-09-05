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
| 4.3 | GNSS | **Not built.** Feeds RAIM prediction and PBN substitution — both named in the plan and both unaddressed |
| 4.4 | Name-code designators for significant points | **Built** — `ats.SignificantPoint` |
| 4.5 | Aeronautical ground lights — en-route | Not held. Low value for the operations this serves |

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

1. **ENR 4.3 GNSS** — outages and RAIM, which the PBN work already asks for.
2. **ENR 1.10 flight planning** — filing requirements and minimum notice, which
   the charter variant needs beside the clearance lead times already built.
3. **ENR 0 checklist reconciliation** — page-level, at cycle close.

Everything above is buildable offline from published text. None of it needs
geometry, and none of it should acquire any: the moment this platform holds a
polygon it starts making containment claims, and a containment claim from
incomplete geometry is the most dangerous output it could produce.

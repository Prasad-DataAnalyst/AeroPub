# AeroPub

Fleet-aware analysis of aeronautical publications — AIP, AIRAC amendments, supplements, AICs,
charts and NOTAM — running on live verified sources, with every value traceable to where it came
from.

## The problem

An airline flying to 180 States has to know what changed in every one of their aeronautical
publications, every 28-day AIRAC cycle, and what each change means for the aircraft they actually
fly. Today that is done by hand. A large carrier has a team for it; a charter operator has one
person doing it between other duties.

Most of the work is not judgement. It is *finding* the publication, *reading* it, and *spotting*
what moved — and all three are automatable.

## What it does

Two questions, one model underneath:

- **"Tell me everything about this."** Pick an aerodrome, city pair or route and get a complete
  dossier — every AIP section, current supplements, live NOTAM, applicable AICs, obstacles, charts,
  snow plan, plus derived suitability and risk for the operator's aircraft.
- **"Something was just published — what does it mean?"** A watcher detects the publication within
  minutes, diffs it, and produces a ranked operational impact bulletin.

Severity is not a property of the change. An RFFS downgrade from Category 9 to 7 is *critical* at a
sole-suitable diversion airfield for a 777, and *irrelevant* to an A320 operator that needs
Category 6. The analysis is told in terms of the fleet.

## Design positions

These are settled and deliberate. [`docs/plan.md`](docs/plan.md) has the reasoning.

| | |
|---|---|
| **Live verified sources only** | No synthetic data anywhere, including tests and demos. Tests replay recorded real responses |
| **Nothing unattributed** | A `Fact` cannot be constructed without a `SourceRef`. Enforced by the type system, not convention |
| **Universal first, operator second** | Every publication gets a change record and a generic impact assessment with no fleet configured. Fleet filtering is a layer on top, never a precondition |
| **Fail loud** | A source that cannot be read produces a visible coverage gap, never a blank. Absence that looks like "nothing changed" is the dangerous failure here |
| **The archive is never pruned** | Superseded NOTAM are retained after States drop them. This cannot be applied retroactively |
| **Decision support, not source of truth** | The official AIP and NOTAM as published by the State remain authoritative |

## Status

Early. Following the build order in [`docs/plan.md`](docs/plan.md) section 30.

- [x] AIRAC cycle calendar — the time spine every other component consumes
- [x] `Fact` and `SourceRef` model — the bitemporal core, with CES resolution
- [x] Source registry and live status board — API keys, State URLs, freshness
- [ ] One live source end to end, with a working provenance receipt
- [ ] Publication watcher
- [ ] First change record

## Development

```bash
python -m pip install -e ".[dev]"
python -m pytest
```

Requires Python 3.10 or later. No runtime dependencies yet.

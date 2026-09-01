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
- [x] Per-State profiles and the fixture capture tool
- [x] Publication watcher — adaptive cadence, change detection, overdue
- [x] Immutable content-addressed archive, and the HTTP transport
- [x] Universal change record and generic operational impact
- [ ] First real fixture captured from a State
- [ ] eAIP parser feeding facts into the change record

## Development

```bash
python -m pip install -e ".[dev]"
python -m pytest
```

Requires Python 3.10 or later. No runtime dependencies yet.

### Capturing a fixture

Parsers are built against real captured responses, never against hand-written
expectations. Run this from a machine that can reach the source:

```bash
python -m aeropub.capture https://aim.gov.qa/datasets.html --as ot-datasets
```

It writes `tests/fixtures/<name>.raw` and `<name>.json` — the body byte for byte,
plus the url, fetch time, HTTP status, response headers and SHA-256 needed to
cite it. Commit both.

For a source behind a login, pass a header from a browser session you are
already signed into:

```bash
python -m aeropub.capture https://aim.gov.qa/datasets.html \
    --as ot-datasets --header "Cookie: $QATAR_AIM_COOKIE"
```

The secret stays on your machine. Request headers are dropped from what gets
saved — the fixture records only *that* a capture was authenticated, never how —
so it is safe to commit to a public repository.

**Never put a credential in a source file, a commit, or a message.** Secrets
belong in your environment; the registry stores only the name of the variable
holding one, plus a masked hint.

### Adding a State

States do not publish alike, so each gets its own module under
`src/aeropub/states/`. See `qatar.py` and `saudi_arabia.py` — two Gulf States under the same
ICAO framework that address their editions with entirely different URL grammar, which is why
there is no shared guess. A profile records what the State
publishes and where, and keeps three things apart: **registered** (we have a
URL), **verified** (a human confirmed it serves what we think), and **absent**
(the State genuinely does not publish this) — with everything else reported as
unknown rather than assumed.

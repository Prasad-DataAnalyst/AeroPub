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
- [x] Validation harness — physical, relational and continuity invariants
- [x] NOTAM parser — Q-line and Items A–G, built from the ICAO format
- [x] FAA NMS-API connector — OAuth2, AIXM 5.1, and the first live State source
- [x] NOTAM register — indexed by the aerodrome, runway or airspace each one affects
- [ ] First captured fixture from a State that publishes an eAIP
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

### Connecting the FAA

The first live credentialed source. The FAA issues an OAuth2 pair on a
spreadsheet during onboarding — the **KEY** column is the client id, **SECRET**
is the client secret:

```bash
export FAA_NMS_CLIENT_ID=...
export FAA_NMS_CLIENT_SECRET=...
export FAA_NMS_ENVIRONMENT=fit      # fit, staging or prod

python -m aeropub.faa.check         # add --json for the machine-readable report
```

The check runs configuration → credentials → network → token → ping → data and
stops at the first failure, so the output names the stage that broke. Nothing
it prints can contain a secret.

Egress needs four hosts, not three — `api-nms.aim.faa.gov` (or the staging/FIT
host in use) **and `storage.googleapis.com`**, where the initial-load bundle
actually lives. Miss the fourth and everything works except the daily full
load. The network stage names the blocked host and who can unblock it.

When the FAA moves a host or renames a path, correct it in a JSON overlay named
by `AEROPUB_FAA_NMS_CONFIG` — no code change, no release.
[`docs/faa-nms.md`](docs/faa-nms.md) has the connector's reasoning, including
the three things the FAA's own curl examples do not tell you.

### Adding a State

States do not publish alike, so each gets its own module under
`src/aeropub/states/`. See `qatar.py` and `saudi_arabia.py` — two Gulf States under the same
ICAO framework that address their editions with entirely different URL grammar, which is why
there is no shared guess. A profile records what the State
publishes and where, and keeps three things apart: **registered** (we have a
URL), **verified** (a human confirmed it serves what we think), and **absent**
(the State genuinely does not publish this) — with everything else reported as
unknown rather than assumed.

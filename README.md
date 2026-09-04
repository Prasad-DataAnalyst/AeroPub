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
| **Fit is ours; performance is the operator's** | Aerodrome compatibility is computed from public ACAP data and Annex 14. FCOM and FPPM are the manufacturer's, licensed to the operator — certified performance stays in the operator's own tool, and anything they supply from it is marked, kept to their tenant and never redistributed |
| **Decision support, not source of truth** | The official AIP and NOTAM as published by the State remain authoritative |

## Status

Early. Following the build order in [`docs/plan.md`](docs/plan.md) section 32, "Where to start".

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
- [x] AIP index — GEN 0–4, ENR 0–6, AD 0–3 incl. AD 2.1–2.25, with per-section coverage
- [x] Aerodrome dossier — every AD 2 section, effective values, live NOTAM, gaps stated
- [x] Change bulletin — cycle to cycle, ranked, cited, and honest about what it did not compare
- [x] Forward view — what changes next, and which of it nobody will publish a word about
- [x] Durable store — SQLite, append-only enforced by the schema itself
- [x] Publication conduct — conditions carried on NOTAM past the three-month limit
- [x] Six output lenses — crew, aerodrome, route, ATS, dispatch and AIS, each stating its own gaps
- [x] JSON/API payloads — versioned, deterministic, provenance on every value, licence honoured
- [x] Printable dossier — a controlled document that renders the payload verbatim
- [x] Aircraft reference code and pavement — Annex 14 Table 1-1, ACN against PCN, no figures shipped
- [x] Aerodrome suitability — code, pavement, width and fire category, with every unmade check named
- [x] Citation manifests — a cited way in for the States no parser reaches
- [x] Command line — `aeropub dossier`, `bulletin`, `horizon`, `quality`, `lens`, `fit`, `load`
- [x] Both pavement systems — ACN/PCN and the ACR/PCR that replaced it, never compared across
- [x] Operator exposure — severity for this fleet, at this aerodrome, in this role
- [x] Network sweep — every aerodrome ranked, unread ones never counted as clear
- [x] Data currency — staleness in AIRAC cycles, so a clear verdict states its own age
- [x] Redundancy analysis — a region down to one alternate, which no aerodrome reports
- [x] eAIP reader — layout as configuration, with a prober that drafts it from a real page
- [x] Time machine — what was knowable at a past moment, beside what is held now
- [x] Collection blindness — how long a change was in force before we held it
- [x] Obstacles — required climb gradient, OIS penetration, cycle delta, crane tracking
- [x] Trips — one flight, one aeroplane, one date, assessed for the day of the flight
- [x] Review gate — severity-configurable, attestation bound to what was attested
- [x] Fleet library — operators, tails and types, with a bibliography for what nobody has read
- [ ] A verified layout profile for a first State (needs one page, from a networked machine)

## Using it

```
pip install -e .

aeropub load --template > othh-ad2.json     # fill it in from the AIP page you are reading
aeropub load othh-ad2.json                  # the document is hashed as it loads
aeropub store -v                            # what is now held

aeropub dossier OTHH                        # every AD 2 section, held or not
aeropub horizon OTHH                        # what changes next, announced or not
aeropub bulletin OTHH --from 2609 --to 2610 # what changed, and what was not compared
aeropub lens OTHH --audience flight_crew    # one reader's view, gaps never filtered out
aeropub quality                             # how this State publishes, against PANS-AIM

aeropub aircraft --template > b77w.json     # fill it in from an ACAP document you hold
aeropub fit OTHH --aircraft b77w.json       # code, pavement, width, fire category

aeropub exposure --template > fleet.json    # your fleet, your network, your roles
aeropub exposure OTHH --profile fleet.json  # what it means for you, per type
aeropub sweep --profile fleet.json          # your whole network, ranked
aeropub currency --stale-only               # what has gone stale, in AIRAC cycles

aeropub retrospect OTHH --known 2026-10-12T06:00Z   # what could we have said that morning
aeropub blindspots                                  # how late our own collection was

aeropub trip --reference N901GX/25SEP --aircraft gl7t.json \
    --on 2026-09-25 --from KTEB --to KASE --alternate KGJT

aeropub fleet --template > register.json      # fill it in from a register or a fleet list
aeropub fleet --library register.json         # coverage per type, operators by tails held
aeropub fleet --library register.json --operator QTR      # what they fly, and what we cannot check
aeropub fleet OTHH --library register.json --operator QTR # which of their types can use it
```

The library is the base that makes the first session a lookup rather than a form. One document
holds one kind of claim — a national register, an operator's own fleet list, an observation set —
and several are merged with each statement keeping the citation it arrived with. A type whose
figures nobody has read yet is listed as *registered* with the document to go and read, never
as a silent absence.

A trip needs no profile file — a flight department asking about Thursday should not have to
write a network definition first. It is assessed for the day of the flight, and reports what
changes between now and then.

### Onboarding a State

Save the AD 2 page from your browser, then:

```
python -m aeropub.eaip probe OTHH-AD-2.html --state OT --draft ot.json
# add the fields you want under each section, check every rule against the page,
# then set verified_at and verified_by
python -m aeropub.eaip parse OTHH-AD-2.html --profile ot.json \
    --aerodrome OTHH --document "AIP Qatar AD 2 OTHH" --valid-from 2026-09-03
```

No code is written to add a State, and nothing leaves your machine. Until a profile is marked
verified every value it reads is recorded at LOW confidence.

Add `--json` to any report for the API payload, or `--html FILE` to a dossier for a printable page.
The store defaults to `aeropub.db`; `--store` or `AEROPUB_STORE` moves it.

Exit codes: `0` produced a document, `1` the answer is adverse, `2` the command could not run. An
inconclusive assessment exits `0` — "I could not tell" is not "no".

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

### Credentials

```
aeropub credentials --set AEROPUB_FAA_CLIENT_SECRET   # prompts, never echoes
aeropub credentials                                    # what is set, never a value
```

Stored in `~/.aeropub/credentials.json`, owner-readable only, outside any repository so it
cannot be committed. In a hosted environment prefer real environment variables — the store
checks those first. A test scans every tracked file for credential-shaped content on each run.

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

# Getting a real AIP in

Everything in `atlas.py`, `enroute.py`, `airspace.py`, `ats.py`, `navaids.py`
and `hazards.py` is built and tested. None of it is holding a real State's
data, and this document is about closing that gap.

Read it as the honest status: **no Qatar or Saudi AIP content is held in this
repository.** The state profiles in `src/aeropub/states/` are URL structures,
not publications. `qatar.PROFILE.unverified_sources()` returns every source it
has, and that is asserted by a test.

## Why nothing has been fetched

The build environment's egress policy refuses the AIM hosts. Every attempt is
a 403 at the gateway, before the request reaches the authority:

```
aim.gov.qa:443           gateway answered 403 to CONNECT
aimss.sans.com.sa:443    gateway answered 403 to CONNECT
www.caa.gov.qa:443       gateway answered 403 to CONNECT
```

That is a network policy on our side, not anything the authority did. The URLs
themselves are good — the current Qatar edition root supplied by an operator
decodes exactly against this repository's own AIRAC calendar, publication date
and all.

## Route one: allow the hosts

Add to the environment's network allowlist, then start a new session:

```
aim.gov.qa
aimss.sans.com.sa
```

Egress policy is set on the environment, not in the repository, so this cannot
be changed from here. See the Claude Code on the web documentation for
environment network configuration.

With those two hosts reachable, the first request is the edition history rather
than a constructed URL:

```
https://aim.gov.qa/AIP/QA-history-en-GB.html
```

**Why the history page and not a cycle.** Qatar changed its eAIP layout. Every
2018–2025 address is `/eaip/{effective}-AIRAC/...`; the current one is:

```
/AIP/03-SEP-2026/AIP-30/2026-10-01-000000/html/index-en-GB.html
     └ published    └ AMDT  └ effective + time
```

Three of those four fields fall out of the AIRAC calendar — `2026-10-01` is
AIRAC 2610's effective date and `03-SEP-2026` is exactly T-28, the ICAO
recipient deadline, both confirmed here independently. The amendment number
does not. So a current edition **cannot be addressed from the cycle alone**,
and the history page is the only published way to learn which amendment number
an edition carries. `qatar.edition_index_url(cycle, amendment)` builds the rest.

## Route two: one saved page

Anyone with normal access to the eAIP can save a page from a browser. That is
enough to start, and it is the faster route.

Most useful first, in this order:

| Page | Gives | Fills |
|---|---|---|
| `ENR-4.4` significant points | designator, coordinates | every dot on the map |
| `ENR-3.1` / `ENR-3.2` ATS routes | route, segment ends, MEA, MAA, direction, RNAV spec | every line |
| `ENR-2.1` FIR/TMA/CTA | designator, class, limits, unit, frequency, lateral limits | every region |
| `ENR-5.1` P/R/D areas | designator, limits, activation, lateral limits | every restricted area |

ENR 4.4 first, because without coordinates the route structure draws as a list
of names and nothing else. ENR 3 without ENR 4.4 produces a chart that says so
and draws no geometry — which is correct behaviour and not a useful map.

Save as HTML (not a screenshot, not a print-to-PDF of the rendered page — the
table markup is what gets read), put the file in the repository or hand it over,
and:

```
python -m aeropub.eaip probe ENR-4.4-en-GB.html --state OT --name Qatar \
    --draft profiles/ot.json
```

`probe` describes the page's actual structure and drafts a reading profile
against it. It does not guess: a section a profile cannot locate is reported as
a miss, and a miss is a coverage gap rather than a wrong value.

For the tables themselves — which is most of an ENR page — `aeropub tables`
reads them directly. List what is on the page:

```
python -m aeropub tables ENR-3.2-en-GB.html
```

It prints every table with its size and its column headers, including the
two-row headers an eAIP uses (`Vertical limits Upper`, `Track (°M) Fwd`). Then
name the columns and emit a manifest:

```
python -m aeropub tables ENR-3.2-en-GB.html --table 0 --kind segments --pair \
  --map "route=Route designator,point=Significant points,\
distance_nm=Distance (NM),upper_limit_ft=Vertical limits Upper,\
mea_ft=Vertical limits Lower,airspace_class=Airspace class" \
  --attributes-from second --locator "ENR 3.2" --region OTDF \
  --out enr3.json
```

Two flags there carry the whole risk of reading an ENR 3 table.

**`--pair`** — an ENR 3 table lists one significant point per row, and a
segment is the gap between two consecutive rows of the same route. ALSEM,
MIDLE, KUKLA is three rows and two segments.

**`--attributes-from`** — whether a row's track, distance and limits describe
the leg *arriving* at that point or the leg *leaving* it. Both conventions are
published, nothing can tell which a State used, and guessing puts every
distance on the neighbouring segment: an error of one leg, invisible in the
output, wrong the whole length of the route. Check the page. The answer is
recorded on every row it produces.

Columns are matched exactly or by index. There is no fuzzy match, because
`MEA` and `MAA` differ by one letter and twenty thousand feet. A header the
mapping names and the table does not have is an error you fix in ten seconds;
a scored near-miss is a manifest that is correct in every respect except the
numbers.

Cells spanning rows are expanded before any of this. An ENR 3 table writes the
route designator once over its segments, and a reader that walks the markup in
order shifts every later row one column left — the next segment's route becomes
a waypoint and its minimum altitude becomes a designator. That output parses,
loads and draws.

## What the manifests look like

The loaders read JSON, and every row carries a `locator` naming the row of the
AIP it came from — that is what makes a value citable rather than merely
present. Blank ones to fill:

```
python -m aeropub route --from OTHH --to EGLL --aircraft a.json --structure-template   # ENR 3
python -m aeropub route --from OTHH --to EGLL --aircraft a.json --airspace-template    # ENR 2
python -m aeropub route --from OTHH --to EGLL --aircraft a.json --navaid-template      # ENR 4.1
python -m aeropub route --from OTHH --to EGLL --aircraft a.json --hazard-template      # ENR 5
python -m aeropub route --from OTHH --to EGLL --aircraft a.json --gnss-template        # ENR 4.3
python -m aeropub route --from OTHH --to EGLL --aircraft a.json --planning-template    # ENR 1.10
python -m aeropub checklist --template                                                 # GEN 0.4
```

Boundaries take either form an AIP publishes. A coordinate list:

```json
"boundary": {
  "described_as": "251500N 0510000E - 254500N 0522000E - ...",
  "points": [
    {"latitude": "251500N", "longitude": "0510000E"},
    {"latitude": "254500N", "longitude": "0522000E"}
  ],
  "edges": [
    {"to_point": 2, "kind": "arc", "centre_latitude": "252000N",
     "centre_longitude": "0515000E", "radius_nm": 30, "clockwise": true}
  ]
}
```

or a circle, which is how most danger areas and control zones are published:

```json
"boundary": {"circle": {"latitude": "251500N", "longitude": "0510000E",
                        "radius_nm": 5}}
```

or nothing but the printed words, which are read as far as they can be read:

```json
"boundary": {"described_as": "251500N 0510000E - thence along the State
                              boundary to 250000N 0505000E"}
```

That last case draws as the published piece and stops. The clause the State
described in words becomes a visible gap, and the area is never filled — a
filled shape reads as a definite extent and the extent is exactly what the
prose withheld.

## Then

```
python -m aeropub atlas --airspace enr2.json --structure enr3.json \
    --navaids enr4.json --hazards enr5.json --region OTDF \
    --page qatar.html --title "Doha FIR"
```

One sheet: FIR and UIR, TMA and CTR, the ATS routes through them, the points
they are made of, the prohibited, restricted and danger areas, over a
public-domain coastline. Six independent layers. Click anything and it says
what the AIP published about it and which document that was.

Re-run it each cycle. Nothing is cached between runs, so the map is whatever
the manifests currently say — and `aeropub checklist` reconciles those against
the State's own GEN 0.4 to catch the page that is a cycle out of date while
still rendering perfectly.

## What will still not be answered

Nothing tells you whether a point is inside an area. Not a route against an
FIR, not a waypoint against a danger area. A drawing puts two things on the
same sheet; it does not make one contain the other, and a containment answer
computed from a boundary that is partly prose, stepped through its arcs and
rounded to the published second would be confidently wrong in exactly the
cases where being wrong matters. Airspace is entered on a clearance and a
chart.

The coastline never answers which country anything is in either. The AIP does:
the State that published the section is the State whose airspace it is. An FIR
is not a country — they run over the high seas and are delegated between
States — and the two lines disagree in a great many places.

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

**One cell, two values.** ENR 5.1 writes the upper limit above the lower in a
single cell, and puts the identification, the name and the lateral limits in
another. A `<br/>` is structure, so lines stay addressable — `Limits:0` is the
upper, `Limits:1` the lower, and `0:1` is the second line of column zero:

```
python -m aeropub tables ENR-5.1-en-GB.html --table 0 --kind hazards \
  --map "designator=0:0,boundary=0:1,upper=1:0,lower=1:1,remarks=2" \
  --locator "ENR 5.1" --region OTDF --out enr5.json
```

A newline in the *source* is not a line break — an AIP wraps a boundary
description across source lines for readability, and treating that as
structure would cut it in half at whatever column the author's editor used.
Only what the markup asks for counts.

A header containing a comma — and AIP headers are full of them, like
*Identification, name and lateral limits* — is addressed by index instead,
since the mapping itself is comma-separated.

Whatever lands in `boundary` is emitted as the description, and
`boundary.parse_boundary` reads the coordinates, arcs and circles out of it.
What it cannot read stays a narrative edge, so *thence along the coastline*
becomes a visible gap and the area is drawn open.

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
python -m aeropub route --from OTHH --to EGLL --aircraft a.json --supplement-template  # AIP SUP
python -m aeropub route --from OTHH --to EGLL --aircraft a.json --flight-rules-template # ENR 1.3
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

## The rule the blank column defers to

An ENR 3 table prints a direction only where the segment departs from the
State's general rule, and most segments do not. That blank means ENR 1.3
governs, not that any level is available — so a route screened without ENR 1.3
is unscreened for direction on most of its length:

```
python -m aeropub route --from OTHH --to EGLL --aircraft b77w.json \
  --crosses OTDF --level 35000 --route "ALSEM UM688 KUKLA" \
  --structure enr3.json --flight-rules enr13.json
```

Fill `sector_from`, `sector_to` and `sector_parity` from what the State prints,
not from the Annex. The Annex default is 000°–180° taking odd levels, and a
State that prints something else has printed it *because* it differs. Set
`basis` to `magnetic` or `true` only where the section says which; leaving it
`not_stated` is reported on the route rather than resolved, because a track
near a sector boundary changes sector once the local variation is applied.

`parity_ceiling` is where the State's table stops being a parity — FL410 under
Annex 2 Appendix 3, above which the levels step by 4000 ft and FL450 sits with
FL350. A level above it is reported as unscreened, never cleared.

A State prescribing a regional table or publishing in metres gets
`scheme: regional_table` or `scheme: metric`, and its segments come back
unscreened with the reason. The table is the answer and nothing here reads it
out of a paragraph.

## The supplements in force

An AIP page is only current until a supplement says otherwise, and a State
publishes those on a list of their own — GEN 0.3 or an AIP SUP index. A
supplement outranks every value on the page it names, so a dossier drawn from
the base AIP alone is a complete drawing of a superseded thing.

```
python -m aeropub route --from OTHH --to EGLL --aircraft b77w.json \
  --crosses OTDF --airspace enr2.json --hazards enr5.json \
  --supplement sup.json
```

Fill `subjects` with the entity key the section itself uses — `AIRSPACE:OTDF`
for a region as ENR 2.1 publishes it, `AIRSPACE:AD-31` for a danger area,
`ATS:UM688` for a route, `FIX:ALSEM` for a point. A supplement heading a whole
section names no object at all, and that is the common case: leave `subjects`
empty and put `ENR 5.1` in `section`, and it reaches every area the section
published. One naming neither is carried too, as *not placed* — a real
document in force whose reach nobody has established, which is a different
statement from an irrelevant one.

Nothing reads a value out of a supplement. What lands on the route and the map
is that the object has been superseded, by which document, over what window,
and whether the State said it *replaces*, *amends* or merely *adds* — an
addition does not make what is held wrong, and the severity says so. The new
number is in the SUP, and a person reads it.

## Checking it against the State's own list

Once the manifests exist, reconcile them against GEN 0.4 — the State's list of
every page in its AIP and the cycle each is current to:

```
python -m aeropub checklist gen04.json \
  --held "ENR 2.1=enr2.json" --held "ENR 3.2=enr3.json" \
  --held "ENR 4.1=enr4.json" --held "ENR 5.1=enr5.json"
```

Holdings are derived from the manifests themselves, so this is a check on what
ingestion actually produced. The finding that matters is not the missing page —
that is visible in every dossier downstream — it is the **stale** one: a
section held at last cycle renders, cites and answers, and every answer is a
cycle out of date with nothing on its face to say so.

`--absent "ENR 4.5=the contents page does not list it"` records a section the
State does not publish, with its basis. If the State's checklist then lists it,
the reconciliation says so: a wrong absence closes a question a gap would have
kept open.

## The whole sector as one page

`--briefing` puts the map and the findings in one document — the drawing at
the top, the open items ranked under it, and what was not looked at in a
section of its own:

```
python -m aeropub route --from OTHH --to EGLL --aircraft b77w.json \
  --crosses OTDF --crosses OIIX --level 35000 \
  --route "ALSEM UM688 KUKLA L604 RASKI" \
  --structure enr3.json --airspace enr2.json --navaids enr4.json \
  --hazards enr5.json --surveillance enr16.json --supps enr18.json \
  --briefing sector.html
```

The headline on that page is coverage, not comfort. A dossier that read both
ends of a route and none of the middle would otherwise print "nothing found"
in the same typeface as one that read everything, so a page that cannot speak
for the whole sector says so before it says anything else and never shows a
settled verdict.

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

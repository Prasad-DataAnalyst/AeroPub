# AeroPub — working notes

Fleet-aware analysis of aeronautical publications. `docs/plan.md` is the design document and is
authoritative; follow its settled decisions rather than re-deriving them.

## Non-negotiable invariants

These are not style preferences. Breaking any of them breaks the product's core claim.

1. **No mock data.** Not in the product, not in tests, not in demos, not in screenshots. Tests
   replay real responses captured from live sources and stored as fixtures. If a value cannot be
   obtained from a real source, it is not shown.
2. **Nothing unattributed.** A `Fact` cannot be constructed without a `SourceRef`. There must be no
   code path that produces a value without provenance. Enforce this in the type system so it cannot
   be forgotten.
3. **Absence renders as absence.** A source that fails to fetch or parse produces a visible coverage
   gap. Never a blank, never a plausible default, never a silent omission.
4. **The archive is never pruned.** Superseded NOTAM are retained after the State drops them. This
   cannot be applied retroactively — see plan section 27.
5. **Deterministic before probabilistic.** Rules first, LLM assistance second, and never to suppress
   a rule hit. An LLM may write a parser; it does not read each document at runtime.

## Secrets

Never accept, store, log or commit a credential. The registry holds a `CredentialRef` — the *name*
of an environment variable plus a masked hint — and reads the secret at point of use without caching
it. Capture strips request headers from fixture metadata so a captured file can be committed
publicly. If a credential ever appears in conversation or a file, say so plainly and advise rotating
it rather than quietly using it.

`horizon.py` looks the other way. A supplement expiring publishes nothing; a NOTAM lapsing publishes
nothing; the layer beneath resurfaces and the operationally true value changes with no message
issued. The CES already knows this, so walking the layer boundaries forward turns it into a list.
Three triggers, and the distinction is the whole point: PUBLISHED will reach an operator through
normal channels, REVERSION and WITHDRAWAL will not. Never collapse them — "5 changes ahead" without
"3 of them unannounced" throws away the only part nobody else can tell you.

It is exact, not predictive: it states what the publications in hand imply, and `as_known_at` records
the belief it was computed from so a horizon is reproducible rather than arguable. The off-by-one is
the thing to protect: a window with `valid_to` of the 20th applies *on* the 20th, so the change is on
the 21st, and a day either way is the difference between a restriction lifting before a flight or
after it.

`store.py` is the durable side: SQLite for facts and provenance, the content-addressed archive for
the documents. Append-only is enforced by the schema, not by convention — DELETE always aborts and
UPDATE aborts unless it is setting `superseded_at` on a row that has none, changing nothing else.
Held in memory that rule is a habit; written to disk it has to survive every future maintainer, and
a promise kept only by people remembering it is not a promise. Tests exercise those triggers through
raw SQL, because going through the store's own API would only prove the API is polite.

Resolution is delegated to `FactStore`, never reimplemented in SQL. Two implementations of the CES
layering that could disagree would be the worst divergence in this system. The store loads rows and
hands them to the tested resolver; `for_entity()` is the pushdown that keeps a country's NOTAM out of
memory when the question is about one runway.

Postgres is the later answer, when several airlines share an instance. Nothing above `store.py`
changes then, because the analysis layers take a fact source rather than a database.

`quality.py` reads publications for their conduct rather than their content. PANS-AIM puts a
three-month limit on what a NOTAM may carry; past that a condition belongs in a Supplement or an
Amendment, where the aerodrome study and the payload table will actually pick it up. The finding
that earns the module is the **serial re-issue**: each message sits comfortably inside the limit
while the condition has run for eleven months across nine of them, which is invisible to anyone
reading messages as they arrive — which is everyone.

Two deliberate asymmetries. `permanent_by_notam` reports only messages still in force, because one
that ended is no longer carrying anything; `serial_reissues` spans expired messages, because there
the *condition* is measured and the earlier messages are the evidence. And condition matching is
strict — same objects, same words — because a looser match groups unrelated work and produces a
finding nobody can check, and a quality harness that cries wolf gets switched off along with its
real findings.

Nothing here calls a State non-compliant. It reports the duration, cites every message, names the
standard, and stops. A test asserts the rendered output contains no verdict vocabulary.

`lenses.py` arranges one body of evidence for six readers. A lens selects and orders; it never
computes, because six implementations of one calculation would eventually disagree and the one that
disagreed would be the one somebody flew on.

**A lens filters findings; it never filters gaps.** Filtering a threat brief down to what concerns a
crew makes "AD 2.10 was never read" concern nobody, so it vanishes — leaving a clean-looking page
about an aerodrome whose obstacle environment is unknown. Every lens names the sections its reader
depends on, those gaps are shown first whatever the filter says, and `is_sound` is false while any
remain.

Selection is **by section, not by domain**. The domain vocabulary is shared with the impact layer
and is deliberately coarse: `dispatch` spans aerodrome dispatchability and flight planning, and
`procedures` spans ATS procedure and runway lighting, so an ATS document filtered on domains
collected every fire category change in the network. Domains remain the fallback for content with no
section mapped — and content with neither a section nor a domain lands with the two lenses that
declare `catches_unclassified`, because a change nobody has classified reaching nobody is the same
failure the bulletin layer already refuses.

`api.py` builds the payloads, not the server — routing and auth belong to whatever hosts it, and
keeping them apart is what lets one set of documents serve the API, the offline package, the email
report and the print output without four of them drifting.

Two of plan section 25's rules are enforced structurally rather than left to whoever writes the next
endpoint. **Provenance is never omitted**: a test walks every document and fails on any object
carrying a `value` without a `source_ref`, because a serialised value with no citation is
indistinguishable from one somebody typed, and over an API nothing downstream can tell. **An
unrecognised object is refused** rather than falling back to `asdict`, which would emit something
API-shaped with none of the guarantees.

The licence boundary is the plan's own — *chart analysis is ours; chart images are theirs*. An
extracted figure is the analysis and travels; reproduced prose is the State's and travels only where
the licence allows, with `UNKNOWN` withholding because assuming permission is the expensive mistake.
Withholding is an object saying what and why, never an empty string that reads as missing data. An
earlier draft withheld the value `3900` while the assessment beside it said "3900 → 3500" —
protection that leaks through the next field is worse than none.

`render.py` prints. Its one rule is that it renders the API payload and nothing else — it does not
reach back into a fact store, recompute a value, or re-word an assessment. A printed document that
disagreed with the JSON for the same aerodrome would be the worst artefact this system could
produce, because the two get compared exactly when something has already gone wrong. A test asserts
the template contains no assessment vocabulary of its own, and the page carries its own payload so a
reader can check the printed figure against the delivered one rather than trust it.

The template lives in `templates/` and ships as package data: a renderer that cannot find its
template is a deployment that looks fine until somebody prints. Data is embedded with `</` escaped,
because an AIP extract containing one would otherwise end the script element — silently, and only
for the aerodromes whose text happens to have one.

## Aircraft, and the licence line through the middle of them

`aircraft.py` answers *does this aeroplane fit this aerodrome* and refuses to answer *what can it
lift off this runway today*. The split is plan decision D and it is a licensing fact before it is a
design one.

**FCOM and FPPM do not ship with this product and never will.** They are the manufacturer's
proprietary documentation, licensed to the operator who bought the aeroplane. An operator has every
right to their own copy and to compute against it; a platform has no right to redistribute it.
Certified performance computation stays with the operator's own tool. What crosses into AeroPub is
data the operator supplies under their own licence, marked `Origin.OPERATOR`, kept to their tenant,
and excluded from `AircraftType.redistributable`.

**ACAP is the public equivalent and it covers most of what aerodrome work needs.** Boeing, Airbus,
Embraer and COMAC publish *Airplane Characteristics for Airport Planning* freely, to the NAS 3601
specification: dimensions, wheel spans, turning radii, ground service arrangements, and the ACN
pavement tables. That answers fit. It does not answer what an aeroplane lifts off a wet runway at
42 °C, and the module does not pretend otherwise.

**No aircraft figures are in the source.** `Characteristic` cannot be constructed without a
`SourceRef`, exactly as `Fact` cannot, and two tests parse the module's own AST to prove no type
designator and no numeric literal outside Annex 14 Table 1-1 has been baked in. The library begins
empty and fills from documents that were actually read. A wingspan recalled from memory is the
failure this whole project is built against, and it is worse here: one metre moves an aeroplane
across a code letter boundary and changes which taxiways it may use.

What *is* encoded is the standard — Annex 14 Volume I Table 1-1, the way `airac.py` encodes the
28-day cycle. The table is read **one column at a time**, first matching row per criterion, and then
1.6.3 applies: the more demanding of the two letters wins. The order of those two steps is the whole
subtlety. Code D and Code E share the 9–14 m wheel span band, so taking the most demanding letter
across all admitting rows at once calls a 34 m span aeroplane Code E — a letter its wingspan has
already ruled out. Each criterion gets one vote, and the shared band votes D. A figure outside the
table yields no letter at all rather than borrowing the other column's.

The pavement comparison has the same shape of trap. A PCN is not a number; it is a number *and* a
pavement type *and* a subgrade *and* a tyre pressure limit *and* how it was determined. An ACN
quoted against a rigid pavement on subgrade B says nothing about a flexible pavement on subgrade C.
`compare_pavement` returns `NOT_COMPARABLE` across either mismatch rather than comparing the
numbers anyway, because the alternative is a confident answer about the wrong pavement. `OVERLOAD`
is not a prohibition — Annex 14 provides for overload operations — but it needs the aerodrome's
procedures and its consent, never a dispatch decision alone, and a `U` rating says what the pavement
has been seen to carry rather than what it was calculated to carry.

`suitability.py` is where the aircraft module earns its place: one aeroplane against one aerodrome
dossier, every check either made on cited data or listed as unmade. It reads the reference code
(Table 1-1), the pavement (ACN/PCN), the runway width (Table 3-1) and the fire category (Table 9-1),
and it does not compute performance — declared distances appear as a `Note`, never a `Check`, so no
verdict can be drawn from a reference field length that is a sea-level ISA classification figure.

Three rules hold it up. **Unknown never becomes suitable** — an unmade check ranks above every pass
in `overall`, and an empty assessment is `UNKNOWN`, not `SUITABLE`. **Every verdict carries both
sides of its evidence**, the aerodrome value and the aircraft characteristic, each with its own
`SourceRef`. And **NOTAM are surfaced, not interpreted**: `overtaken` names the checks resting on
values a NOTAM in force may have overtaken, using the same one-directional containment as
everywhere else, and it makes `is_conclusive` false. That last one came out of reading the worked
example: every check passed, the verdict read RESTRICTED, and the runway those checks were about
was closed by NOTAM. The caveat now sits beside the verdict rather than at the foot of the page.

`Note` deliberately has no `assessment` field and the JSON emits no `assessment` key for one. An
integrator reading these into a table must not be able to treat a note as a verdict by reading a
field that happens to be there; the absence of the key is the guarantee. Filing declared distances
as `UNKNOWN` checks — the first draft — would have made every assessment permanently inconclusive,
and a flag that is always on tells a reader nothing.

The Annex 14 tables are read into code, not fetched and archived. Table 1-1 and Table 9-1 were
confirmed against published sources; Table 3-1 was confirmed only in part. Confirm all three against
the current edition before anything operational depends on them, and treat a Table 3-1 shortfall as
`RESTRICTED` rather than a prohibition — it is a design standard, and States approve narrower
runways.

## Getting data in, and getting it out

`cli.py` is the one way to run any of this: `python -m aeropub <command>`, or `aeropub` once
installed. Every command opens the store, calls the same function `api.py` calls, and prints. Nothing
is computed there, so there is no path by which the terminal and the JSON can disagree about the
same aerodrome — which is exactly when the two get compared.

Exit codes carry meaning and a script depends on it. `0` produced a document, `1` the answer is
adverse, `2` the command could not run. **An inconclusive assessment exits `0`, not `1`**: "I could
not tell" is not "no", and a caller that conflates them acts on the wrong one.

Every command over an empty store says the store is empty, in its own output. That line separates
"we read the AIP and there is nothing to report" from "nobody has ever loaded anything", and those
are opposite answers that otherwise print an identical document.

`manifest.py` holds the citation-manifest machinery both loaders share: one file describes one
document, the document is hashed **as the file loads**, and every value inside points at where in it
the value was found. The hash proves which document, the locator proves where, and a citation
missing either is one a reviewer cannot resolve. Prefer `document_path` over `content_hash` — the
citation then cannot be written unless the document is on disk to be hashed. There is no partial
success anywhere: one uncitable value fails the file, because a store that is nearly all cited is
one people stop checking.

`ingest.py` is the answer to a real problem, not a shortcut around the eAIP parser. Most of the 180
States do not publish a machine-readable eAIP, and "we have no parser for Chad" is a fact about us,
not about Chad. An AIS officer reads AD 2.12 and writes a manifest; it loads into the same store
with the same bitemporal model, and every downstream component cannot tell the difference and should
not. `parser_id` records `aip-manifest` rather than an extractor's name, because a transcription
error and a parser defect have different failure modes and must trace separately. `precedence` is
required and never defaulted: a supplement loaded as an AIP sits *beneath* the base it is meant to
override, and the effective state comes out wrong with nothing downstream able to detect it.

`acap.py` is the same discipline for aircraft. **One manifest, one document, one origin** — a
characteristic may not declare an origin of its own, because the citation it would carry is the
manifest's, so an operator figure inside an ACAP manifest comes out cited to the ACAP. That is worse
than an uncited figure: it resolves, to the wrong page. Write a second manifest and `merge()`; each
figure then keeps the citation it was read with, and the operator half still stays out of
`redistributable`.

Both loaders offer `--template`, and both templates carry `null` for every value. There is nothing
in them for somebody to keep who did not open the document, and neither loads as it stands.

## Layer three, at last

`operator.py` is the layer the product is named for, and the only one that may know who is asking.
The plan's headline case: an RFFS downgrade from Category 9 to 7 is **critical** at a sole-suitable
EDTO alternate for a wide-body and **irrelevant** to a narrow-body operator that needs Category 6.
Same publication, same change record, same generic impact, two different answers — and neither
answer is a property of the change.

**Severity is derived, never asserted.** There is no table saying "RFFS downgrade = critical".
Exposure falls out of three cited things: what the fleet's aeroplanes require under Annex 14, what
the aerodrome publishes, and what role the operator has given it. Change any one and the answer
moves, which is what makes it an assessment rather than an opinion. There is a test for both
directions of that.

Role is the multiplier, and `demands_certainty` is the distinction that matters: an unmade check at a
destination can be made before the next flight is planned; an unmade check at an EDTO or take-off
alternate is a gap in something a dispatched flight is already relying on. `sole_suitable` is
recorded, never inferred — only the operator knows what else is within reach, what their approvals
cover and what their handling arrangements allow. It is the difference between "choose another
alternate" and "there is no other alternate".

Three rules. **"No exposure" is a real answer and the record beneath survives in full** — an
aerodrome outside the network resolves to `NONE` with the complete suitability assessment attached,
so adding a destination later needs no catch-up run. **Unknown never becomes "no exposure"**: those
are opposite conclusions that would print the same comforting word, and at a role that demands
certainty an unmade check is graded `HIGH`, not `UNKNOWN`. **Every finding names its type** — "your
fleet is exposed" is not actionable, "the wide-body is, the narrow-body is not" is; the roll-up is
the worst case across types and never an average.

`actionable` sorts worst-first. That came from reading the output: the critical finding sat below
three high ones, and a list that buries the thing it exists to surface has failed.

Layer two stays clean beneath. A test renders every `Suitability` inside an `OperatorAssessment` and
fails on the words operator, fleet, network, EDTO or sole — and another asserts two operators with
different roles get byte-identical suitability checks off the same dossier.

An operator profile is **not** a citation manifest and carries no document hash. The other manifests
describe something somebody read, and the hash is what makes the reading resolvable; a profile
describes the operator's own operation, where there is no external document to be right or wrong
about. Demanding a hash for one would be provenance theatre. The aircraft manifests it references
carry their citations as usual.

## The two pavement systems

ICAO replaced ACN/PCN with ACR/PCR in Annex 14 Volume I, mandatory from **28 November 2024**, and
States are converting at different rates — so both are published somewhere for years. The two share
an identical five-part format and share nothing else: a real PCR runs in the hundreds where the PCN
for the same pavement runs in the tens, and the tyre pressure categories kept their letters while
moving their MPa limits.

So `PavementRating.parse` **requires** the system and never infers it from the number. Guessing is
wrong in the permissive direction: an ACN of 62 against a PCR of 560 reads as a vast margin.
`compare_pavement` checks the system before the pavement type and the subgrade, because reporting a
subgrade mismatch for a cross-system comparison sends a reader to the wrong ACAP table.

`suitability.py` reads both the `pcn` and `pcr` attributes — reading one would report a coverage gap
at an aerodrome that publishes the other. Where a runway carries both it assesses the ACR/PCR one
and says so; the other is not a cross-check, because the scales are unrelated. Where the two
disagree about pavement type or subgrade, that is flagged as changeover inconsistency rather than
silently resolved: the same pavement cannot be both, one of them is stale, and only the State can
say which.

## The whole network, and the aerodromes nobody has read

`sweep.py` answers the question an airline with two hundred destinations actually opens each
morning: across everything I fly to, what needs attention today, and what will need it before I next
look.

**The artefact this module exists to avoid** is a network dashboard showing 197 green and 3 red where
150 of the greens are aerodromes nobody has ever read. That is worse than no dashboard, because it
converts absence of data into a green tile and puts a number on it somebody will quote in a safety
meeting. So coverage is a first-class column, not a footnote: an unread aerodrome is never counted
among the clear ones, it appears in its own section, it makes the sweep inconclusive, and `summary()`
reports `covered` and `uncovered` beside every severity count so no single percentage can be quoted
without both. A test asserts `covered + uncovered == aerodromes` and that the severity buckets sum to
`covered` alone.

**The forward half is the differentiator.** Running the horizon over the whole network is
interesting; re-running layer three on each date a change takes effect is the answer. That is what
`future` does — the same assessment on each transition date — and it yields lines like *"on 21
November your sole-suitable EDTO alternate goes invalid, and nothing will be published about it"*.
Forty-seven days of notice on something no State will announce. `deteriorates_unannounced` is the
flag that matters: a worsening on an AIRAC date arrives with a publication somebody reads; a
worsening when a supplement quietly lapses arrives with nothing at all.

Two things came from reading the output rather than the code. An aerodrome that deteriorates ahead is
excluded from the "No action" list — it already has its own section, and listing it twice lets a
reader who scans headings stop at the wrong one. And unread aerodromes sort *above* read ones at the
same exposure: an unknown where nobody looked is a different problem from an unknown where somebody
looked and came up short.

Nothing here is computed. Exposure comes from `operator.py`, the forward view from `horizon.py`, and
this module ranks and counts. A test builds the single-aerodrome report independently and asserts the
sweep's findings are identical — a number here that disagrees with the report for the same aerodrome
is a defect in this module, not a different opinion.

## The entity key grammar

`entities.py` owns how everything is named — `OTHH`, `OTHH/RWY34L`, `AIRSPACE:EGTT` — and it is the
only place the rule may be written. It was previously spelled out at four call sites and they had
drifted: two normalised case and two did not, so `register.at("8wc")` returned nothing for an
aerodrome with a live runway NOTAM and `render("8wc")` reported it as a coverage gap. A confident
"nothing here" about somewhere that has something is the exact failure this project exists to
avoid. Use `covers`, `aerodrome_of`, `scope_of`, `compose` and `normalise`; never write the
`startswith` again.

Containment is one-directional: an aerodrome query reaches its runways, a runway query does not
reach the aerodrome. Only the first separator divides a key, because a runway pair designator
legitimately contains one (`8WC/RWY02/20`).

## Conventions

- Python 3.10+, standard library only unless a dependency genuinely earns its place.
- Type hints throughout. `from __future__ import annotations` at the top of modules.
- Tests assert published ground truth where it exists, and the standard's own invariants elsewhere.
- Comments explain *why*, not *what*. Aeronautical domain reasoning is worth writing down;
  restating the code is not.
- Docstrings on public API carry the domain context a reader will not have.

## Build order

Plan section 30. Done: AIRAC calendar, the bitemporal fact model, the source registry and status
board, per-State profiles, the fixture capture tool, the publication watcher, the archive and the
HTTP transport, the universal change record, the generic impact layer, the validation harness, the
NOTAM parser, the FAA NMS-API connector, the NOTAM register, the AIP index, the aerodrome dossier,
the change bulletin, the change horizon, SQLite persistence, AIS quality intelligence, the six
output lenses, the JSON surface, the printable dossier, the aircraft reference code and pavement
checks, the aerodrome suitability assessment, the citation manifests, the command line,
layer three and the network sweep. Next: a captured
fixture from a State that publishes an eAIP, then the eAIP parser built against it — plan section 31
step 5, the milestone that proves the system.

`aip.py` is the AIP's own structure, built from Annex 15 and PANS-AIM the way the NOTAM parser was
built from the NOTAM format: 127 sections across GEN 0–4, ENR 0–6 and AD 0–3, including AD 2.1–2.25
per aerodrome and AD 3.1–3.23 per heliport. It is the reference structure, not a claim about any
State. AD 2.25 is marked `icao_defined=False` because Annex 15's own list stops at 2.24 — claiming
ICAO mandates a section it does not would make a State's omission look like a deficiency.

Coverage is recorded per section and keeps four states apart: `HELD` (and citable — recording it
without a `SourceRef` is refused), `ABSENT` (which needs its basis, normally the State's own
checklist, because absence is a claim about the State and not about our search), `FAILED`, and
`NOT_CHECKED`. The last two are our gaps; the first two are not. A coverage report prints every
expected section whether held or not, so an aerodrome nobody looked at cannot read like one with
nothing to report.

`dossier.py` composes those four — index, coverage, CES, NOTAM register — into one attributed
document. It assembles; it does not decide. Values are filed under the section ICAO publishes them
in (`aip.ATTRIBUTE_SECTIONS`), and an attribute with no mapping is printed under "held, but not
attributed to a section" rather than guessed into a plausible one, because a value under the wrong
heading reads as though that section said it. NOTAM are shown against the objects they name, never
routed into an AD 2 section by reading their text. A dossier built with nothing still prints all 25
sections: an omission that is invisible is the failure this project exists to avoid.

`bulletin.py` is plan section 31's milestone — what changed between two cycles, why it matters, and
where every value came from. It has one claim to earn: "everything that changed" is false for any
section not read on **both** dates, so coverage is first-class. `blind` names those sections,
`is_conclusive` is false whenever any exist, and an empty bulletin with blind sections says "no
change detected in what was compared", never "nothing changed". Without coverage it says
completeness cannot be stated rather than assuming it. A bulletin that quietly omitted unread
sections would read as a clean bill of health, which is the most dangerous artefact this system
could produce.

`Attention` is a reading order, not a severity: action, then anything no rule covers (it may be
either), then opportunities, then the rest. Severity is a property of a change *and* an operator and
belongs to layer three. A test asserts nothing a reader sees names a fleet, a network or a customer,
and that the band names avoid severity vocabulary.

`src/aeropub/faa/` is the reference shape for every connector that follows: where the service lives
is data an operator can correct from a JSON overlay while the service runs (`AEROPUB_FAA_NMS_CONFIG`),
credentials are named and never held, and the two responses that *are* credentials — the token and
the signed-URL handover — are the only things excluded from the archive. `docs/faa-nms.md` has the
reasoning. Do not add a connector that hardcodes a host.

Every connector gets a conformance run like `tests/test_faa_conformance.py`: a real HTTPS server
speaking the authority's documented contract, driving the unmodified client over a real socket.
Mocked transport tests prove logic and prove nothing about plumbing, and the faults they cannot
reach — a redirect followed when it should not be, a header lost on the wire, our own throttle
reported as the authority's outage — only appear against a live service, which is the worst place
to find them. Note what it does *not* prove: that the authority behaves as documented. Only
`python -m aeropub.faa.check` against the real endpoint shows that.

NOTAM are indexed, not turned into facts. `Fact` validity is a `date`; a NOTAM is valid from a
minute and may carry a schedule that leaves it dormant for hours inside its own window. Flattening
either would over-claim, so `notam_register.py` keeps the source's precision and reports
`SCHEDULE_UNKNOWN` rather than `IN_FORCE` for a window we cover but a schedule we have not read.
Never collapse that state into a yes: a NOTAM active 1100-0001 daily reported as in force at 0600
is wrong in the direction that gets someone airborne on a false assumption.

Subject roll-up runs one way. Asking about an aerodrome returns its runways; asking about a runway
must never return a NOTAM filed against the whole aerodrome, which would attribute an apron closure
to a runway. Where a source links no feature, the subject records only where the message was filed,
kind `FILED_LOCATION` — knowing a NOTAM concerns ZBW is real information and is not the same
information as knowing which runway it closes.

NOTAM is the one source parseable from its specification rather than a captured sample, because
ICAO defines the format. An eAIP is not — every State invents its own layout, so those parsers wait
for fixtures. The NOTAM Q-code tables are a deliberate subset: decode only codes carrying no doubt
and leave the rest as None. A half-decoded reading looks like a complete one.

Validation findings are graded, and the grades mean different things. INVALID cannot be true and is
quarantined; SUSPECT is probably a unit error and is held for confirmation; ADVISORY is unusual but
legitimate and publishes with a note. Never promote an advisory to a failure to be safe — a harness
that cries wolf gets switched off, which costs more than the false negative it was avoiding.

Layers one and two carry no operator context. A generic impact statement that names a fleet, a
network or a customer has leaked tenant reasoning upward; there is a test asserting none of them do.
Where no rule covers an attribute, say so — a plausible sentence about something nobody modelled
reads exactly like one that was.

The archive has no delete, prune or purge, and must not grow one. Content-addressed storage
deduplicates unchanged fetches, which is what makes keeping everything affordable.

**This build environment has no outbound web access.** Egress policy blocks every external host
except package registries, so no source can be reached from here. Capture fixtures from a networked
machine with `python -m aeropub.capture` and commit them; parsers are then built against those.
Never fill the gap by writing what a source is assumed to return.

Keep three coverage states apart and never let them look alike: **registered** (a URL exists),
**verified** (a human confirmed it), **absent** (the State genuinely does not publish it). Anything
else is unknown, and unknown is reported, not assumed.

Secrets never enter the registry, the database, a log or a status board. A source that needs a key
holds a `CredentialRef` — the name of an environment variable plus a masked hint. Read the secret at
point of use and do not cache it.

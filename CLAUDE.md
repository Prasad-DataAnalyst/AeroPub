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
HTTP transport. Next: a captured fixture from a real State, then the eAIP parser built against it.

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

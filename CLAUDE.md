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

## Conventions

- Python 3.10+, standard library only unless a dependency genuinely earns its place.
- Type hints throughout. `from __future__ import annotations` at the top of modules.
- Tests assert published ground truth where it exists, and the standard's own invariants elsewhere.
- Comments explain *why*, not *what*. Aeronautical domain reasoning is worth writing down;
  restating the code is not.
- Docstrings on public API carry the domain context a reader will not have.

## Build order

Plan section 30. Currently: AIRAC calendar and the bitemporal fact model done; next is one
live source end to end.

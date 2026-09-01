# Fixtures

Real responses, captured from real sources, replayed by tests.

Nothing in this directory is written by hand. Every fixture is a pair produced
by `python -m aeropub.capture`:

- `<name>.raw` — the response body, byte for byte
- `<name>.json` — url, fetch time, HTTP status, headers, SHA-256 of the body

The metadata becomes a `SourceRef` when the fixture is replayed, so a test
asserts against data carrying the same provenance chain as production.

## Capturing

Run from a machine that can reach the source:

```bash
python -m aeropub.capture https://aim.gov.qa/datasets.html --as ot-datasets
```

Then commit both files. Keep them small — capture the index or a single
document, not an entire publication.

## Why not just write the expected data by hand?

Because a parser tested against what we *imagine* a State publishes passes
right up to the moment it meets what the State *actually* publishes. The
fixtures are the only thing standing between the test suite and that failure.

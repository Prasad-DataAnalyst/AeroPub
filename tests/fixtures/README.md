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

## Fixtures that did not arrive over HTTP

`faa/` holds sample AIXM and a handover response the FAA issued with API
registration, by email rather than from an endpoint. They follow the same
`.raw` / `.json` pair, and the sidecar says plainly how they arrived and who
issued them — `capture.py` did not produce them and does not claim to.

One of them is redacted, and the sidecar names the redaction: the handover
response's `X-Goog-Signature` is a Google Cloud Storage bearer capability. That
one expired minutes after it was issued in September 2025 and grants nothing,
but a credential-shaped string is not committed to a public repository whatever
its state. Everything the parser reads — bucket path, parameter set,
`X-Goog-Date`, `X-Goog-Expires` — is intact.

That is the only sanctioned edit to a fixture, and only for a credential. Never
adjust aeronautical content to make a test pass.

## Why not just write the expected data by hand?

Because a parser tested against what we *imagine* a State publishes passes
right up to the moment it meets what the State *actually* publishes. The
fixtures are the only thing standing between the test suite and that failure.

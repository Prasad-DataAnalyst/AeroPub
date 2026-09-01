"""Capture a real response as a test fixture.

The no-mock rule says tests replay recorded reality rather than invented data.
This is the tool that does the recording. Run it from a machine that can reach
the source, commit what it writes, and parsers are then built against exactly
what the authority served — not against anyone's idea of what it serves.

    python -m aeropub.capture https://aim.gov.qa/datasets.html --as ot-datasets

Each capture writes two files into ``tests/fixtures/``:

``<name>.raw``
    The response body, byte for byte, unmodified.

``<name>.json``
    What is needed to cite it: the URL, when it was fetched, the HTTP status,
    the response headers, and the SHA-256 of the body. This becomes the
    ``SourceRef`` when the fixture is replayed, so a test asserts against data
    with the same provenance chain as production.

Sources behind a login are captured by passing a header from a browser session
you are already signed into::

    python -m aeropub.capture https://aim.gov.qa/datasets.html \
        --as ot-datasets --header "Cookie: $QATAR_AIM_COOKIE"

The secret stays on your machine. It is never written into the fixture, never
recorded in the metadata, and never needs to be shared with anyone — request
headers are dropped from what gets saved, precisely so a captured fixture can
be committed to a public repository without leaking the session that fetched it.

Deliberately uses only the standard library, so it runs anywhere without a
virtualenv — including on a laptop that merely has network access.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

__all__ = ["capture", "fixture_dir"]

USER_AGENT = (
    "AeroPub/0.1 (+https://github.com/Prasad-DataAnalyst/AeroPub) "
    "aeronautical publication monitoring"
)

DEFAULT_TIMEOUT = 60

#: Request headers never reach the fixture metadata. A captured file is meant to
#: be committed, and a Cookie or Authorization header in it would be a leak.
_NEVER_RECORDED = frozenset({"cookie", "authorization", "proxy-authorization", "x-api-key"})


def fixture_dir() -> Path:
    """Where fixtures live, relative to the repository root."""
    return Path(__file__).resolve().parents[2] / "tests" / "fixtures"


def _parse_header(raw: str) -> tuple[str, str]:
    name, sep, value = raw.partition(":")
    if not sep or not name.strip():
        raise ValueError(f"header must be 'Name: value', got {raw!r}")
    return name.strip(), value.strip()


def capture(
    url: str,
    name: str,
    *,
    into: Path | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    headers: dict[str, str] | None = None,
) -> dict:
    """Fetch ``url`` and write it as a fixture pair. Returns the metadata.

    ``headers`` may carry authentication for a source behind a login. Whatever
    is passed is used for the request and then discarded — it does not appear in
    the returned metadata or the written files.
    """
    target = into or fixture_dir()
    target.mkdir(parents=True, exist_ok=True)

    sent = {"User-Agent": USER_AGENT}
    sent.update(headers or {})
    request = urllib.request.Request(url, headers=sent)
    fetched_at = datetime.now(timezone.utc)

    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read()
        status = response.status
        headers = dict(response.headers.items())
        final_url = response.geturl()

    digest = hashlib.sha256(body).hexdigest()

    meta = {
        "name": name,
        "url": url,
        "final_url": final_url,
        "fetched_at": fetched_at.isoformat(),
        "http_status": status,
        "content_hash": digest,
        "content_length": len(body),
        "response_headers": headers,
        # Which request headers were sent, by name only. Enough to know a
        # capture was authenticated; not enough to repeat it.
        "authenticated": sorted(
            n for n in (sent.keys() - {"User-Agent"}) if n.lower() in _NEVER_RECORDED
        ),
    }

    (target / f"{name}.raw").write_bytes(body)
    (target / f"{name}.json").write_text(json.dumps(meta, indent=2) + "\n")
    return meta


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m aeropub.capture",
        description="Record a real response as a test fixture.",
    )
    parser.add_argument("url")
    parser.add_argument(
        "--as",
        dest="name",
        required=True,
        help="fixture name, conventionally the source id (e.g. ot-datasets)",
    )
    parser.add_argument("--into", type=Path, default=None)
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    parser.add_argument(
        "--header",
        action="append",
        default=[],
        metavar="'Name: value'",
        help=(
            "request header, repeatable. Use for sources behind a login, e.g. "
            "--header \"Cookie: $SESSION\". Never written to the fixture."
        ),
    )
    args = parser.parse_args(argv)

    try:
        extra = dict(_parse_header(h) for h in args.header)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    try:
        meta = capture(
            args.url, args.name, into=args.into, timeout=args.timeout, headers=extra
        )
    except urllib.error.HTTPError as exc:
        print(f"HTTP {exc.code} from {args.url}", file=sys.stderr)
        return 1
    except urllib.error.URLError as exc:
        print(f"could not reach {args.url}: {exc.reason}", file=sys.stderr)
        return 1

    where = args.into or fixture_dir()
    print(f"captured {meta['content_length']} bytes from {meta['final_url']}")
    print(f"  http {meta['http_status']}   sha256 {meta['content_hash'][:16]}...")
    print(f"  wrote {where / (args.name + '.raw')}")
    print(f"  wrote {where / (args.name + '.json')}")
    if meta["authenticated"]:
        print(f"  authenticated via {', '.join(meta['authenticated'])} "
              "(header not recorded in the fixture)")
    if meta["final_url"] != meta["url"]:
        print("  note: redirected — update the registry URL to the final address")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

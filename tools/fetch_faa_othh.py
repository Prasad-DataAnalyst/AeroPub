#!/usr/bin/env python3
"""Fetch OTHH international NOTAM from the FAA, on a machine that can reach it.

Standard library only — Python 3.8+, nothing to install. Run it anywhere with
a normal internet connection, then hand the output files to AeroPub:

    python3 fetch_faa_othh.py --xlsx AeroPub.xlsx
    python3 fetch_faa_othh.py --key KEY --secret SECRET

It writes, beside itself:

    othh-notams.xml       the OTHH international NOTAM, AIXM
    initial-load.gz       the whole INTERNATIONAL baseline (with --initial-load)

Then, in AeroPub:

    python -m aeropub.faa.check --relay initial-load.gz --archive raw/

Nothing here is printed that should not be. The secret is never echoed, never
put on a command line by this script, and never written to either output file.
"""

import argparse
import base64
import getpass
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HOSTS = {
    "staging": "https://api-staging.cgifederal-aim.com",
    "fit": "https://api-fit.cgifederal-aim.com",
    "prod": "https://api-nms.aim.faa.gov",
}


def die(message: str) -> "NoReturn":  # noqa: F821
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(1)


def credentials(args) -> "tuple[str, str]":
    """The key and secret, from the spreadsheet, the flags, or a prompt.

    Reading the workbook is preferred: transcribing a 64-character opaque
    string is how one character goes wrong, and the 401 that follows is
    indistinguishable from a revoked key.
    """
    if args.xlsx:
        try:
            import io

            import msoffcrypto  # type: ignore
            import openpyxl  # type: ignore
        except ImportError:
            die(
                "reading the spreadsheet needs: "
                "pip install openpyxl msoffcrypto-tool\n"
                "       or pass --key and --secret instead."
            )
        password = args.xlsx_password or getpass.getpass(
            f"password for {Path(args.xlsx).name}: "
        )
        buffer = io.BytesIO()
        try:
            with open(args.xlsx, "rb") as handle:
                office = msoffcrypto.OfficeFile(handle)
                office.load_key(password=password)
                office.decrypt(buffer)
        except Exception as error:
            die(f"could not decrypt {args.xlsx}: {type(error).__name__}")
        buffer.seek(0)
        book = openpyxl.load_workbook(buffer, data_only=True)
        found = {}
        for sheet in book.worksheets:
            for row in sheet.iter_rows():
                cells = [c.value for c in row if c.value is not None]
                if len(cells) >= 2 and str(cells[0]).strip().lower() in ("key", "secret"):
                    found[str(cells[0]).strip().lower()] = str(cells[1]).strip()
        if "key" not in found or "secret" not in found:
            die(f"{args.xlsx} has no Key and Secret rows")
        return found["key"], found["secret"]

    key = args.key or input("client id (Key): ").strip()
    secret = args.secret or getpass.getpass("client secret (Secret): ")
    if not key or not secret:
        die("both a key and a secret are needed")
    return key, secret


def token(host: str, key: str, secret: str, timeout: int) -> str:
    """OAuth2 client_credentials, HTTP Basic, per the FAA's own example.

    Note the token endpoint is NOT under /nmsapi — the FAA names this as the
    commonest failure. And no JSON Content-Type: tools that default to one get
    a failure that looks like bad credentials.
    """
    url = f"{host}/v1/auth/token"
    body = urllib.parse.urlencode({"grant_type": "client_credentials"}).encode()
    basic = base64.b64encode(f"{key}:{secret}".encode()).decode("ascii")
    request = urllib.request.Request(
        url,
        data=body,
        headers={
            "Authorization": f"Basic {basic}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace")[:300]
        die(f"token request refused (HTTP {error.code}): {detail}")
    except urllib.error.URLError as error:
        die(f"could not reach {url}: {error.reason}")
    access = payload.get("access_token")
    if not access:
        die(f"no access_token in the response: {sorted(payload)}")
    print(
        f"  token obtained  ·  expires in {payload.get('expires_in')} s"
        f"  ·  {payload.get('organization_name', '')}"
    )
    return access


def fetch(url: str, bearer: str, timeout: int, *, aixm: bool = False) -> bytes:
    headers = {"Authorization": f"Bearer {bearer}"}
    if aixm:
        # Required on /v1/notams. Omitting it is an error, not a default.
        headers["nmsResponseFormat"] = "AIXM"
    try:
        with urllib.request.urlopen(
            urllib.request.Request(url, headers=headers), timeout=timeout
        ) as response:
            return response.read()
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace")[:300]
        die(f"{url} refused (HTTP {error.code}): {detail}")
    except urllib.error.URLError as error:
        die(f"could not reach {url}: {error.reason}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fetch OTHH international NOTAM from the FAA NMS-API."
    )
    parser.add_argument("--xlsx", help="the FAA onboarding spreadsheet")
    parser.add_argument("--xlsx-password", help="its password (prompted if omitted)")
    parser.add_argument("--key", help="client id, if not using --xlsx")
    parser.add_argument("--secret", help="client secret, if not using --xlsx")
    parser.add_argument(
        "--environment", default="staging", choices=sorted(HOSTS),
        help="default: staging, which is what onboarding issues credentials for",
    )
    parser.add_argument("--location", default="OTHH", help="default: OTHH")
    parser.add_argument(
        "--initial-load", action="store_true",
        help="also pull the whole INTERNATIONAL baseline (one per 24 h)",
    )
    parser.add_argument("--out", default=".", help="directory to write into")
    parser.add_argument("--timeout", type=int, default=60)
    args = parser.parse_args()

    host = HOSTS[args.environment]
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    print(f"FAA NMS-API — {args.environment}  ·  {host}")
    key, secret = credentials(args)
    print(f"  credentials: {len(key)} / {len(secret)} characters")
    bearer = token(host, key, secret, args.timeout)

    query = urllib.parse.urlencode(
        {"location": args.location, "classification": "INTERNATIONAL"}
    )
    body = fetch(f"{host}/nmsapi/v1/notams?{query}", bearer, args.timeout, aixm=True)
    target = out / f"{args.location.lower()}-notams.xml"
    target.write_bytes(body)
    print(f"  wrote {target}  ({len(body):,} bytes)")
    if len(body) < 400:
        print(
            "  That is very small. It may be an empty result — which means "
            "either no active\n  NOTAM, or this location not being in the "
            "FAA's holdings. Those are different."
        )

    if args.initial_load:
        # allowRedirect=false returns the handover as JSON rather than a 307,
        # so the URL can be seen before it is followed.
        handover = fetch(
            f"{host}/nmsapi/v1/notams/il/INTERNATIONAL?allowRedirect=false",
            bearer, args.timeout,
        )
        try:
            pointer = json.loads(handover.decode("utf-8"))["data"]["url"]
        except Exception:
            die(f"could not read the handover: {handover[:200]!r}")
        # A Google-signed URL takes no bearer — the signature covers the host
        # header and a bearer alongside it is two credentials at once. A
        # relative path on the FAA's own host needs the bearer.
        if pointer.startswith("http"):
            signed, needs_bearer = pointer, "X-Goog-Signature" not in pointer
        else:
            signed, needs_bearer = f"{host}/nmsapi{pointer}", True
        bundle = fetch(signed, bearer if needs_bearer else "", args.timeout)
        target = out / "initial-load.gz"
        target.write_bytes(bundle)
        print(f"  wrote {target}  ({len(bundle):,} bytes)")

    print("\nHand these to AeroPub:")
    print(f"  python -m aeropub.faa.check --relay {out}/initial-load.gz --archive raw/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

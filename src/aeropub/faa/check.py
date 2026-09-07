"""``python -m aeropub.faa.check`` — does the FAA connection actually work?

The operator question this answers is narrow and important: *the key is
installed, but is anything reaching the FAA?* A registered source with a
present credential looks identical to a working one until something asks.

Run it after installing a key, after the FAA changes anything, and from the
status screen. It reports in stages, so a failure names the stage that broke:

    configuration → credentials → network → token → ping → data

``--json`` emits the same report as a document, which is what the status API
serves and what the console screen renders. No stage of it can print a secret:
the token is masked at the type level and the report is built from
:class:`~aeropub.faa.auth.TokenResponse`, which never holds the token at all.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping

from aeropub.archive import Archive
from aeropub.faa.auth import TokenClient
from aeropub.faa.client import NmsClient
from aeropub.faa.config import (
    CONFIG_PATH_VAR,
    ENVIRONMENT_VAR,
    ClientCredentials,
    NmsEnvironment,
    load_environment,
)
from aeropub.faa.errors import (
    NmsAuthError,
    NmsError,
    NmsTransportError,
    NmsUnavailableError,
)
from aeropub.faa.sources import credential_rows
from aeropub.netcheck import Probe, probe

__all__ = ["ConnectionReport", "StageResult", "main", "verify"]


#: Exit codes, so a scheduled check can be acted on without parsing output.
EXIT_OK = 0
EXIT_CREDENTIALS = 1
EXIT_UNAVAILABLE = 2
EXIT_PROTOCOL = 3
EXIT_NETWORK = 4
"""Something between us and the FAA refused the connection. Distinct from
UNAVAILABLE because the remedy is a network administrator rather than
patience, and distinct from CREDENTIALS because the key is very likely fine."""


@dataclass
class StageResult:
    """One stage of the check."""

    name: str
    ok: bool
    detail: str = ""
    duration_ms: int | None = None

    def line(self) -> str:
        mark = "ok  " if self.ok else "FAIL"
        timing = f" [{self.duration_ms}ms]" if self.duration_ms is not None else ""
        return f"  {mark}  {self.name}{timing}" + (f" — {self.detail}" if self.detail else "")


@dataclass
class ConnectionReport:
    """Everything the status screen needs to say about the FAA connection."""

    environment: str
    host: str
    token_url: str
    api_base: str
    checked_at: datetime
    overlay_file: str | None = None
    is_production: bool = False
    stages: list[StageResult] = field(default_factory=list)
    credentials: list[dict[str, Any]] = field(default_factory=list)
    token: dict[str, Any] | None = None
    network: dict[str, Any] | None = None
    exit_code: int = EXIT_OK

    @property
    def ok(self) -> bool:
        return all(stage.ok for stage in self.stages)

    def to_dict(self) -> dict[str, Any]:
        return {
            "environment": self.environment,
            "host": self.host,
            "token_url": self.token_url,
            "api_base": self.api_base,
            "overlay_file": self.overlay_file,
            "is_production": self.is_production,
            "checked_at": self.checked_at.isoformat(),
            "ok": self.ok,
            "exit_code": self.exit_code,
            "credentials": self.credentials,
            "network": self.network,
            "token": self.token,
            "stages": [asdict(stage) for stage in self.stages],
        }

    def render(self) -> str:
        lines = [
            f"FAA NMS-API — {self.environment}"
            + ("  (PRODUCTION)" if self.is_production else ""),
            f"  host      {self.host}",
            f"  token     {self.token_url}",
            f"  api base  {self.api_base}",
        ]
        if self.overlay_file:
            lines.append(f"  overlay   {self.overlay_file}")
        lines.append("")
        lines.append("Credentials")
        width = max(
            (len(row["env_var"]) for row in self.credentials), default=0
        )
        for row in self.credentials:
            mark = "ok  " if row["status"] == "configured" else "----"
            note = (
                f"  — set as {row['found_as']}, which is the earlier name"
                if row.get("found_as") and row["found_as"] != row["env_var"]
                else ""
            )
            lines.append(
                f"  {mark}  {row['env_var']:<{width}}  {row['status']}{note}"
            )
        lines.append("")
        lines.append("Connection")
        lines.extend(stage.line() for stage in self.stages)
        if self.network:
            lines.append("")
            lines.append("Network")
            lines.append(f"  host             {self.network['host']}")
            lines.append(f"  proxy            {self.network['proxy'] or 'none (direct)'}")
            lines.append(f"  ca bundle        {self.network['ca_bundle'] or 'system default'}")
            if not self.network["reachable"]:
                lines.append(f"  blocked at       {self.network['layer']}")
                lines.append(f"  remedy           {self.network['remedy']}")
        if self.token:
            lines.append("")
            lines.append("Token")
            for key in ("organization", "client_id", "api_products", "expires_in", "masked"):
                value = self.token.get(key)
                if value:
                    shown = ", ".join(value) if isinstance(value, list) else value
                    lines.append(f"  {key:16} {shown}")
        lines.append("")
        lines.append(
            "Connection verified." if self.ok else "Connection NOT verified — see above."
        )
        return "\n".join(lines)


def _configuration(
    report: ConnectionReport,
    environment: NmsEnvironment | None,
    client: NmsClient | None,
    env_map: Mapping[str, str] | None,
) -> NmsEnvironment | None:
    """Resolve where the FAA is. Returns ``None`` having recorded the failure."""
    try:
        env = environment or (
            client.environment if client else load_environment(environ=env_map)
        )
    except (KeyError, OSError, ValueError) as exc:
        report.stages.append(StageResult("configuration", False, str(exc)))
        report.exit_code = EXIT_PROTOCOL
        return None

    report.environment = env.name
    report.host = env.host
    report.token_url = env.token_url
    report.api_base = env.base
    report.is_production = env.is_production
    report.stages.append(
        StageResult(
            "configuration", True,
            env.description or f"{len(env.endpoints)} endpoints",
        )
    )
    return env


def _credentials(
    report: ConnectionReport,
    creds: ClientCredentials,
    env_map: Mapping[str, str] | None,
) -> bool:
    """Are both halves installed? Local and instant, so it runs before the wire."""
    report.credentials = [
        {
            "env_var": row.env_var,
            "label": row.label,
            "status": row.status.value,
            "present": row.present,
            "hint": row.hint,
            "found_as": row.found_as,
        }
        for row in credential_rows(creds, environ=env_map)
    ]
    missing = creds.missing(env_map)
    if missing:
        report.stages.append(
            StageResult(
                "credentials", False,
                f"not set: {', '.join(missing)}. The FAA onboarding spreadsheet's "
                "KEY column is the client id and SECRET is the client secret.",
            )
        )
        report.exit_code = EXIT_CREDENTIALS
        return False
    stale = creds.deprecated_names(env_map)
    if stale:
        report.stages.append(
            StageResult(
                "credentials", True,
                "both halves present, but "
                + "; ".join(
                    f"{found} is the earlier name for {current}" for found, current in stale
                )
                + ". Two live names for one secret is how a rotated credential "
                "loses to a stale one — move it and unset the old name.",
            )
        )
        return True
    report.stages.append(StageResult("credentials", True, "both halves present"))
    return True


def _network(
    report: ConnectionReport, env: NmsEnvironment, supplied: Probe | None
) -> bool:
    """Can anything reach the FAA at all?

    Credential-free, and deliberately before the token request. An egress proxy
    refusing the host and the FAA rejecting a key look identical from here, and
    telling someone to rotate a working credential because their own network
    blocked the call is the most expensive wrong answer this tool can give.
    """
    reach = supplied if supplied is not None else probe(env.url("ping"))
    report.network = {
        "host": reach.host,
        "layer": reach.layer.value,
        "reachable": reach.reachable,
        "http_status": reach.http_status,
        "proxy": reach.proxy,
        "ca_bundle": reach.ca_bundle,
        "detail": reach.detail,
        "remedy": reach.remedy(),
    }
    if reach.reachable:
        report.stages.append(
            StageResult("network", True, reach.describe(), duration_ms=reach.duration_ms)
        )
        return True

    report.stages.append(
        StageResult(
            "network", False, f"{reach.describe()} — {reach.remedy()}",
            duration_ms=reach.duration_ms,
        )
    )
    report.exit_code = (
        EXIT_NETWORK
        if reach.layer.is_network_policy or reach.layer.is_ours
        else EXIT_UNAVAILABLE
    )
    return False


def _run(report: ConnectionReport, name: str, call, *, on_success) -> bool:
    """Run one live stage, mapping each failure class to its own exit code.

    The mapping is the point: a rejected key, an unreachable gateway and a
    response that does not match the contract need three different people to
    do three different things.
    """
    try:
        outcome = call()
    except NmsAuthError as exc:
        report.stages.append(StageResult(name, False, str(exc)))
        report.exit_code = EXIT_CREDENTIALS
        return False
    except (NmsTransportError, NmsUnavailableError) as exc:
        report.stages.append(StageResult(name, False, str(exc)))
        report.exit_code = EXIT_UNAVAILABLE
        return False
    except NmsError as exc:
        report.stages.append(StageResult(name, False, str(exc)))
        report.exit_code = EXIT_UNAVAILABLE if exc.is_retryable else EXIT_PROTOCOL
        return False
    return on_success(outcome)


def _token(report: ConnectionReport, client: NmsClient) -> bool:
    def succeeded(token) -> bool:
        report.token = {
            "masked": token.masked,
            "expires_at": token.expires_at.isoformat(),
            "expires_in": token.response.expires_in,
            "organization": token.response.organization,
            "client_id": token.response.client_id,
            "api_products": list(token.response.api_products),
            "status": token.response.status,
            "scope": token.response.scope,
        }
        report.stages.append(StageResult("token", True, token.response.describe()))
        return True

    return _run(report, "token", lambda: client.tokens.token(force=True),
                on_success=succeeded)


def _ping(report: ConnectionReport, client: NmsClient) -> bool:
    def succeeded(response) -> bool:
        report.stages.append(
            StageResult("ping", True, f"HTTP {response.status}",
                        duration_ms=response.duration_ms)
        )
        return True

    return _run(report, "ping", client.ping, on_success=succeeded)


def _data(report: ConnectionReport, client: NmsClient) -> bool:
    """Pull a real load and count what parsed against what the FAA claimed.

    A short read is reported as a failure. Presenting two NOTAM as a successful
    load of a country is the failure that looks exactly like a quiet day.
    """
    from aeropub.faa.aixm import NotamFeed  # local: only needed on this path

    def succeeded(load) -> bool:
        with load.open() as stream:
            feed = NotamFeed(stream)
            count = sum(1 for _ in feed)
        claimed = feed.header.number_returned if feed.header else None
        detail = f"{count} NOTAM read"
        if claimed is not None:
            detail += f" of {claimed} the FAA reported"
            if feed.is_complete is False:
                detail += " — SHORT READ"
        detail += f"; archived as {load.entry.digest[:12]}"
        ok = feed.is_complete is not False
        report.stages.append(StageResult("data", ok, detail))
        if not ok:
            report.exit_code = EXIT_PROTOCOL
        return ok

    # INTERNATIONAL, not DOMESTIC: this platform reads international NOTAM in
    # ICAO format, and a domestic load proves the transport while exercising
    # none of the parsing the application actually depends on.
    return _run(report, "data", lambda: client.fetch_initial_load("INTERNATIONAL"),
                on_success=succeeded)


def verify(
    environment: NmsEnvironment | None = None,
    *,
    credentials: ClientCredentials | None = None,
    environ: Mapping[str, str] | None = None,
    client: NmsClient | None = None,
    fetch_data: bool = False,
    archive: Archive | None = None,
    now: datetime | None = None,
    network_probe: Probe | None = None,
) -> ConnectionReport:
    """Run the staged check and return the report.

    Stops at the first failure. Asking for NOTAM when the token was refused
    produces a second, less informative error about the same fault — and sends
    the reader to the wrong stage.
    """
    moment = now or datetime.now(timezone.utc)
    creds = credentials or ClientCredentials.default()
    env_map = dict(environ) if environ is not None else None

    report = ConnectionReport(
        environment="unknown",
        host="",
        token_url="",
        api_base="",
        checked_at=moment,
        overlay_file=(env_map or {}).get(CONFIG_PATH_VAR) if env_map is not None else None,
    )

    env = _configuration(report, environment, client, env_map)
    if env is None:
        return report
    if not _credentials(report, creds, env_map):
        return report
    if not _network(report, env, network_probe):
        return report

    active = client or NmsClient(
        env,
        tokens=TokenClient(env, creds, environ=env_map),
        archive=archive,
        environ=env_map,
        # Stages run back to back against one host. Without this the check
        # reports the FAA unavailable when what actually happened is that our
        # own two-second host gap had not elapsed since the previous stage.
        wait_for_throttle=True,
    )

    if not _token(report, active):
        return report
    if not _ping(report, active):
        return report
    if fetch_data:
        _data(report, active)
    return report


def _fetch_notams(
    env, environ, archive, *, location: str, classification: str | None, out: str | None
) -> int:
    """Fetch one location's active NOTAM and say what came back.

    Separate from the staged check because it is not a diagnostic: it is the
    ordinary operation, and its failure modes are the ordinary ones.
    """
    import json as _json

    from aeropub.faa.aixm import NotamFeed
    from aeropub.faa.client import NmsClient
    from aeropub.faa.errors import NmsError

    client = NmsClient(env, archive=archive, environ=environ)
    try:
        response = client.notams(location=location, classification=classification)
    except NmsError as error:
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return EXIT_NETWORK if getattr(error, "is_retryable", False) else EXIT_PROTOCOL

    import io

    held = list(NotamFeed(io.BytesIO(response.body)))

    where = f"{location}" + (f" / {classification}" if classification else "")
    print(f"{where}: {len(held)} active NOTAM")
    if response.archived is not None:
        print(f"  archived as {response.archived.digest[:12]}")
    if not held:
        # An aerodrome with no NOTAM and an aerodrome the FAA does not hold
        # look identical in an empty response, and they are not the same fact.
        print(
            "  Nothing came back. That is either no active NOTAM, or this "
            "location not being in the FAA's holdings — the response cannot "
            "tell them apart, and the State's own AIS can."
        )
    for notam in held[:10]:
        print(f"  {notam.describe() if hasattr(notam, 'describe') else notam}")
    if len(held) > 10:
        print(f"  ... and {len(held) - 10} more")

    if out:
        Path(out).write_text(
            _json.dumps(
                {
                    "location": location,
                    "classification": classification,
                    "archived_as": (
                        response.archived.digest if response.archived else None
                    ),
                    "retrieved_at": (
                        response.archived.retrieved_at.isoformat()
                        if response.archived
                        else None
                    ),
                    "count": len(held),
                    "notams": [
                        {
                            "number": n.number,
                            "year": n.year,
                            "location": n.location,
                            "effective_start": (
                                n.effective_start.isoformat() if n.effective_start else None
                            ),
                            "effective_end": (
                                n.effective_end.isoformat() if n.effective_end else None
                            ),
                            "text": n.text,
                        }
                        for n in held
                    ],
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"  wrote {out}")
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m aeropub.faa.check",
        description="Verify the FAA NMS-API connection, stage by stage.",
    )
    parser.add_argument(
        "--environment",
        "-e",
        help=f"fit, staging or prod. Defaults to ${ENVIRONMENT_VAR}, then prod.",
    )
    parser.add_argument(
        "--config",
        help=f"JSON overlay describing a changed host or path. Defaults to ${CONFIG_PATH_VAR}.",
    )
    parser.add_argument(
        "--data",
        action="store_true",
        help="also pull and parse the domestic initial load. Needs --archive.",
    )
    parser.add_argument("--archive", help="directory for the raw store.")
    parser.add_argument(
        "--notams", metavar="LOCATION",
        help=(
            "fetch the active NOTAM for one location and write them out, e.g. "
            "OTHH. Needs --archive so the response stays citable."
        ),
    )
    parser.add_argument(
        "--classification", metavar="KIND", default="INTERNATIONAL",
        help=(
            "which classification to read. Defaults to INTERNATIONAL, which "
            "is what this platform is built on: ICAO-format messages with a "
            "Q-line. DOMESTIC, FDC, MILITARY and LOCAL_MILITARY are also "
            "accepted, and 'ALL' disables the filter."
        ),
    )
    parser.add_argument(
        "--out", metavar="FILE",
        help="write the NOTAM read by --notams to this file, as JSON.",
    )
    parser.add_argument(
        "--relay", metavar="FILE",
        help=(
            "ingest an initial load fetched elsewhere, instead of calling the "
            "FAA. For a network with no route to CGI Federal: fetch on a "
            "machine that has one and hand the file over. Needs --archive."
        ),
    )
    parser.add_argument(
        "--relayed-by", dest="relayed_by", default="", metavar="WHO",
        help="who fetched the --relay file. Recorded on the citation.",
    )
    parser.add_argument("--json", action="store_true", help="emit the report as JSON.")
    args = parser.parse_args(argv)

    import os

    environ = dict(os.environ)
    if args.config:
        environ[CONFIG_PATH_VAR] = args.config

    try:
        env = load_environment(args.environment, environ=environ)
    except (KeyError, OSError, ValueError) as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return EXIT_PROTOCOL

    archive = Archive(args.archive) if args.archive else None

    if args.notams:
        if archive is None:
            print(
                "--notams needs --archive: a NOTAM answered from a response "
                "nobody kept is not citable.",
                file=sys.stderr,
            )
            return EXIT_PROTOCOL
        wanted = args.classification
        return _fetch_notams(
            env, environ, archive,
            location=args.notams,
            classification=None if str(wanted).upper() == "ALL" else wanted,
            out=args.out,
        )

    if args.relay:
        if archive is None:
            print(
                "--relay needs --archive: a bundle nobody watched arrive is "
                "evidence, and evidence that is not stored cannot be cited "
                "later.",
                file=sys.stderr,
            )
            return EXIT_PROTOCOL
        from aeropub.faa.relay import RelayError, relay_initial_load

        try:
            relayed = relay_initial_load(
                args.relay, archive=archive, obtained_from=args.relayed_by
            )
        except RelayError as error:
            print(str(error), file=sys.stderr)
            return EXIT_PROTOCOL
        print(relayed.describe())
        print(f"  archived as {relayed.load.entry.digest[:12]}")
        if relayed.is_stale:
            print(
                "  This baseline is older than the FAA's own pull cadence. "
                "Re-fetch before operating on it."
            )
        if relayed.is_complete is None:
            print(
                "  The wrapper states no count, so nothing here can say the "
                "file is whole."
            )
        return EXIT_OK

    if args.data and archive is None:
        print(
            "--data needs --archive: the bundle is evidence, and evidence that "
            "is not stored cannot be cited later.",
            file=sys.stderr,
        )
        return EXIT_PROTOCOL

    report = verify(env, environ=environ, fetch_data=args.data, archive=archive)
    print(json.dumps(report.to_dict(), indent=2) if args.json else report.render())
    return report.exit_code


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

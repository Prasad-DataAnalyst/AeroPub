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
        for row in self.credentials:
            mark = "ok  " if row["status"] == "configured" else "----"
            lines.append(f"  {mark}  {row['env_var']:24} {row['status']}")
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
    produces a second, less informative error about the same fault.
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

    # -- stage 1: configuration -----------------------------------------
    try:
        env = environment or (client.environment if client else load_environment(environ=env_map))
    except (KeyError, OSError, ValueError) as exc:
        report.stages.append(StageResult("configuration", False, str(exc)))
        report.exit_code = EXIT_PROTOCOL
        return report

    report.environment = env.name
    report.host = env.host
    report.token_url = env.token_url
    report.api_base = env.base
    report.is_production = env.is_production
    report.stages.append(
        StageResult("configuration", True, env.description or f"{len(env.endpoints)} endpoints")
    )

    # -- stage 2: credentials -------------------------------------------
    rows = credential_rows(creds, environ=env_map)
    report.credentials = [
        {
            "env_var": row.env_var,
            "label": row.label,
            "status": row.status.value,
            "present": row.present,
            "hint": row.hint,
        }
        for row in rows
    ]
    missing = creds.missing(env_map)
    if missing:
        report.stages.append(
            StageResult(
                "credentials",
                False,
                f"not set: {', '.join(missing)}. The FAA onboarding spreadsheet's "
                "KEY column is the client id and SECRET is the client secret.",
            )
        )
        report.exit_code = EXIT_CREDENTIALS
        return report
    report.stages.append(StageResult("credentials", True, "both halves present"))

    # -- stage 3: network -------------------------------------------------
    # Credential-free, and deliberately before the token request. An egress
    # proxy refusing the host and the FAA rejecting a key look identical from
    # here, and telling someone to rotate a working credential because their
    # own network blocked the call is the most expensive wrong answer this
    # tool can give.
    reach = network_probe if network_probe is not None else probe(env.url("ping"))
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
    if not reach.reachable:
        report.stages.append(
            StageResult("network", False, f"{reach.describe()} — {reach.remedy()}",
                        duration_ms=reach.duration_ms)
        )
        report.exit_code = (
            EXIT_NETWORK if reach.layer.is_network_policy or reach.layer.is_ours
            else EXIT_UNAVAILABLE
        )
        return report
    report.stages.append(
        StageResult("network", True, reach.describe(), duration_ms=reach.duration_ms)
    )

    # -- stage 4: token --------------------------------------------------
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
    try:
        token = active.tokens.token(force=True)
    except NmsAuthError as exc:
        report.stages.append(StageResult("token", False, str(exc)))
        report.exit_code = EXIT_CREDENTIALS
        return report
    except (NmsTransportError, NmsUnavailableError) as exc:
        report.stages.append(StageResult("token", False, str(exc)))
        report.exit_code = EXIT_UNAVAILABLE
        return report
    except NmsError as exc:
        report.stages.append(StageResult("token", False, str(exc)))
        report.exit_code = EXIT_PROTOCOL
        return report

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

    # -- stage 5: ping ---------------------------------------------------
    try:
        response = active.ping()
    except NmsAuthError as exc:
        report.stages.append(StageResult("ping", False, str(exc)))
        report.exit_code = EXIT_CREDENTIALS
        return report
    except (NmsTransportError, NmsUnavailableError) as exc:
        report.stages.append(StageResult("ping", False, str(exc)))
        report.exit_code = EXIT_UNAVAILABLE
        return report
    except NmsError as exc:
        report.stages.append(StageResult("ping", False, str(exc)))
        report.exit_code = EXIT_PROTOCOL
        return report

    report.stages.append(
        StageResult(
            "ping", True, f"HTTP {response.status}", duration_ms=response.duration_ms
        )
    )

    # -- stage 6: data ---------------------------------------------------
    if fetch_data:
        try:
            load = active.fetch_initial_load("DOMESTIC")
        except NmsError as exc:
            report.stages.append(StageResult("data", False, str(exc)))
            report.exit_code = (
                EXIT_UNAVAILABLE if exc.is_retryable else EXIT_PROTOCOL
            )
            return report

        from aeropub.faa.aixm import NotamFeed  # local: only needed on this path

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
        report.stages.append(
            StageResult("data", feed.is_complete is not False, detail)
        )
        if feed.is_complete is False:
            report.exit_code = EXIT_PROTOCOL

    return report


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

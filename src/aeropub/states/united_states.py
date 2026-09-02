"""United States — FAA.

The first State profile built on a live credentialed API rather than on a
published eAIP, and the shape is different because of it. Where Qatar and Saudi
Arabia are documents at addresses, the FAA is an authenticated service: the
sources here are endpoints, the credential is an OAuth2 pair, and freshness is
measured in minutes rather than AIRAC cycles.

What this profile deliberately does **not** claim:

The FAA publishes far more than NOTAM — an eAIP, the digital chart supplement,
terminal procedures, obstacle data. None of it is registered here, because no
URL for it has been verified, and a registered-but-wrong URL is worse than an
acknowledged gap: it makes the board look covered. Those kinds come back from
:meth:`StateProfile.unknown_kinds` as unknown, which is the truth.

Nothing is declared absent either. The FAA certainly publishes an AIP; we have
simply not connected it yet, and "we have not looked" must never be recorded as
"the State does not publish it".
"""

from __future__ import annotations

from typing import Mapping

from aeropub.faa.config import ENVIRONMENTS, ClientCredentials, NmsEnvironment
from aeropub.faa.sources import nms_sources
from aeropub.states import StateProfile

__all__ = ["AIM_URL", "NOTAM_SEARCH_URL", "PROFILE", "profile"]

#: The FAA's Aeronautical Information Services entry point.
AIM_URL = "https://www.faa.gov/air_traffic/flight_info/aeronav"

#: The public NOTAM search — the human-facing view of the same holdings the
#: NMS-API serves. Useful for confirming by eye what the API returned.
NOTAM_SEARCH_URL = "https://notams.aim.faa.gov/notamSearch/"

#: Other ICAO prefixes the FAA is the AIS authority for. The profile is keyed
#: on K because that is where the traffic is; the others are recorded so a
#: lookup on PANC or TJSJ is a known gap rather than a silent miss.
OTHER_PREFIXES: tuple[str, ...] = ("PA", "PH", "PG", "TJ", "TI", "NS")


def profile(
    environment: NmsEnvironment | None = None,
    credentials: ClientCredentials | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> StateProfile:
    """The FAA profile for one NMS environment.

    A function rather than a constant because which environment is in use is a
    runtime decision. A board built while pointed at FIT must show FIT URLs —
    showing production addresses for a connection that is not talking to
    production is the kind of quiet inaccuracy this project exists to avoid.
    """
    env = environment or ENVIRONMENTS["prod"]
    return StateProfile(
        code="K",
        name="United States",
        authority="Federal Aviation Administration",
        aim_url=AIM_URL,
        sources=nms_sources(env, credentials, environ=environ),
        notes=(
            "NOTAM via the NMS-API (AIXM 5.1 with FAA fnse extensions), "
            f"{env.name} environment. Requires an OAuth2 client-credentials pair "
            "issued by the FAA per environment. The FAA is also the AIS "
            f"authority for {', '.join(OTHER_PREFIXES)}; none of those, and no "
            "FAA AIP, chart or obstacle source, is connected yet."
        ),
    )


#: The production profile, for callers that want one without choosing.
PROFILE = profile()

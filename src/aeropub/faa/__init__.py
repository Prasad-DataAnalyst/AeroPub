"""The FAA NMS-API connector.

The FAA's NOTAM Management System is AeroPub's first live, credentialed source,
and the reference implementation for every connector that follows it. The
shape it establishes:

- **Where the service lives is data, not code** (:mod:`aeropub.faa.config`).
  Hosts, paths and required headers are a record an operator can correct from a
  JSON file while the service runs, because an authority that moves an endpoint
  does not wait for our release train.
- **Credentials are named, never held** (:class:`ClientCredentials`). Two
  environment variables; the connector reads them at the moment it
  authenticates and keeps neither.
- **The bearer token is refreshed early, once, and never printed**
  (:mod:`aeropub.faa.auth`).
- **Everything fetched is archived before it is parsed, except the two things
  that are credentials** — the token response and the signed-URL handover
  (:mod:`aeropub.faa.client`).
- **Structure is read, meaning is not invented** (:mod:`aeropub.faa.aixm`).
- **NOTAM are indexed by what they affect, at the precision the source has**
  (:mod:`aeropub.faa.register` onto :mod:`aeropub.notam_register`).

Getting started::

    aeropub credentials --set AEROPUB_FAA_CLIENT_ID      # prompts, never echoes
    aeropub credentials --set AEROPUB_FAA_CLIENT_SECRET
    export FAA_NMS_ENVIRONMENT=fit                        # fit, staging or prod
    aeropub netcheck                                      # reachability, no key used
    python -m aeropub.faa.check                           # the whole chain

Anything hosted should set ``AEROPUB_FAA_CLIENT_ID`` and
``AEROPUB_FAA_CLIENT_SECRET`` as real environment variables instead: they
survive a restart and touch no disk. Either way the connector reads the
environment first and the credential file second. ``FAA_NMS_CLIENT_ID`` and
``FAA_NMS_CLIENT_SECRET`` are the earlier names and are still read, and the
check command says so when it finds one.
"""

from __future__ import annotations

from aeropub.faa.aixm import AffectedFeature, FeedHeader, NmsNotam, NotamFeed, iter_notams
from aeropub.faa.auth import AccessToken, TokenClient, TokenResponse
from aeropub.faa.client import InitialLoad, NmsClient, NmsResponse, SignedUrl
from aeropub.faa.config import (
    ENVIRONMENTS,
    ClientCredentials,
    NmsEndpoint,
    NmsEnvironment,
    load_environment,
)
from aeropub.faa.errors import (
    NmsAuthError,
    NmsConfigurationError,
    NmsError,
    NmsProtocolError,
    NmsTransportError,
    NmsUnavailableError,
)
from aeropub.faa.register import register_feed, registered, subjects_of
from aeropub.faa.sources import CredentialRow, credential_rows, nms_sources

__all__ = [
    "ENVIRONMENTS",
    "AccessToken",
    "AffectedFeature",
    "ClientCredentials",
    "CredentialRow",
    "FeedHeader",
    "InitialLoad",
    "NmsAuthError",
    "NmsClient",
    "NmsConfigurationError",
    "NmsEndpoint",
    "NmsEnvironment",
    "NmsError",
    "NmsNotam",
    "NmsProtocolError",
    "NmsResponse",
    "NmsTransportError",
    "NmsUnavailableError",
    "NotamFeed",
    "SignedUrl",
    "TokenClient",
    "TokenResponse",
    "credential_rows",
    "iter_notams",
    "load_environment",
    "nms_sources",
    "register_feed",
    "registered",
    "subjects_of",
]

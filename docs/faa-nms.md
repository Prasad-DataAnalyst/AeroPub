# FAA NMS-API connector

The FAA's NOTAM Management System is AeroPub's first live, credentialed source.
It is also the reference implementation: every connector after it follows this
shape, so the decisions here are worth reading before writing the next one.

## Getting connected

The FAA issues an OAuth2 client-credentials pair on a spreadsheet during
onboarding. The column headed **KEY** is the client id; the column headed
**SECRET** is the client secret.

```bash
export FAA_NMS_CLIENT_ID=...        # spreadsheet KEY
export FAA_NMS_CLIENT_SECRET=...    # spreadsheet SECRET
export FAA_NMS_ENVIRONMENT=fit      # fit, staging or prod

python -m aeropub.faa.check
```

`check` runs five stages and stops at the first failure, so the output names
what broke rather than what broke *next*:

```
configuration → credentials → token → ping → data
```

`--json` emits the same report as a document. That is what the status API
serves and what the console screen renders — there is one code path, not a
display version and a real version.

Exit codes: `0` verified, `1` credentials, `2` unavailable, `3` protocol.

## What "working" has been proven to mean

Two different claims, and they are not interchangeable.

**The client conforms to the documented contract.** `tests/test_faa_conformance.py`
stands up an HTTPS server implementing the FAA's onboarding pack and drives the
unmodified client through the whole sequence over a real socket, real TLS and
real urllib: token, ping, filtered NOTAM, checklist, both handover forms, the
unauthenticated storage download, gunzip, AIXM parse, and a citation resolving
back to archived bytes. Nothing is stubbed. That is what catches faults the
mocked tests structurally cannot — whether urllib really declines the redirect,
whether the `HTTPError` really carries `Location`, whether the bearer survives
the wire.

**The FAA's gateway matches its own documentation.** Only a call against the
real service shows that, and `python -m aeropub.faa.check` is how it is made.
Until it has been run with a real key, that claim is untested. If it then
fails, the conformance suite is what tells you the fault is the FAA differing
from its specification rather than our transport being broken.

## Environments

| Name | Host | Notes |
|---|---|---|
| `fit` | `api-fit.cgifederal-aim.com` | Facility Integration Test. Start here |
| `staging` | `api-staging.cgifederal-aim.com` | Pre-production |
| `prod` | `api-nms.aim.faa.gov` | The only environment whose NOTAM are operationally valid |

Registration is per-environment: a staging key does not work against
production, and the resulting 401 says nothing about why.

Two addressing traps, both from the FAA's own examples:

- The token endpoint is `/v1/auth/token` on the **bare host**. Every operation
  is under **`/nmsapi`**. Building the token URL from the API base gives a 404
  that reads like a bad credential.
- `/notams` requires the header `nmsResponseFormat: AIXM`. Without it the call
  fails with an error that does not mention the header. The header is attached
  to the endpoint, so no call site can forget it.

## When the FAA changes something

Nothing about the connection is written into calling code. Hosts, paths and
required headers are data. To correct any of it while the service is running,
write a JSON overlay and point `AEROPUB_FAA_NMS_CONFIG` at it:

```json
{
  "base": "prod",
  "host": "https://nms2.aim.faa.gov",
  "endpoints": [
    { "name": "notams", "path": "/v2/notams",
      "headers": { "nmsResponseFormat": "AIXM", "X-FAA-Version": "2" } }
  ]
}
```

Endpoints merge by name, so correcting one leaves the other five alone. A
wholly new endpoint can be added the same way. A 404 from the connector says
this explicitly rather than pointing at the source tree.

## What the connector does that a curl example does not

**It refuses to follow redirects.** `/notams/il` hands off to a signed Google
Cloud Storage URL on another host. Python's redirect handler carries the
`Authorization` header across that hop on the versions this project supports,
and GCS rejects a request presenting both its own signature and an
Authorization header. The handover is two explicit steps and the second is
unauthenticated.

**It keeps two responses out of the archive.** Everything fetched is archived
before it is parsed — except the token response and the signed-URL handover,
because both *are* credentials and the archive has no delete. The bundle they
lead to is archived, as served, compressed, byte for byte.

**It knows when the signed URL dies.** `X-Goog-Date` and `X-Goog-Expires` are
in the signed query string, so the ~5-minute deadline is known before the
request goes out. An expired signed URL otherwise comes back as a 403 with an
XML body that says nothing about time, and the bug report reads "the FAA is
refusing us".

**It sniffs the bundle.** GCS can transcode a gzipped object on the way out, so
the payload may arrive already decompressed. A client that assumes gzip fails
on the whole feed.

**It waits for its own throttle rather than blaming the FAA.** The host gap
protects a small AIS estate from a scheduler running many sources, and there
refusing and rescheduling is right — sleeping would stall the whole tick. But a
diagnostic making four sequential calls to one host is not abusive, and
reporting "the FAA is unavailable" because our own two-second gap had not
elapsed is a false alarm about somebody else's service. `check` opts into
waiting, up to `MAX_THROTTLE_WAIT`; past that it raises, because a check that
pauses for six hours is not waiting, it is hanging.

**It measures token expiry against our clock.** Never against the gateway's
`issued_at`: the two clocks differ, and trusting theirs means holding a token
we believe is live for as long as the skew. Refresh happens 120 seconds early,
under a lock, so twenty concurrent sources do not each ask for a token.

## Reading the AIXM

This is the reason to prefer NMS over text NOTAM. A text NOTAM says

> RWY 20 RWY END ID LGT U/S

and leaves a reader to work out which aerodrome and which physical runway end.
The AIXM message carries the `AirportHeliport`, the `Runway` and the
`RunwayDirection` as linked features beside the text, each with a UUID that
survives renumbering. That is what makes a NOTAM joinable to an aerodrome
dossier rather than a string to be searched.

```python
from aeropub.faa import NmsClient, NotamFeed

client = NmsClient(archive=archive)
load = client.fetch_initial_load("DOMESTIC")

with load.open() as stream:
    feed = NotamFeed(stream)
    for notam in feed:
        for runway in notam.runways():
            ...
    assert feed.is_complete is not False   # a short read under-reports a country
```

Things it deliberately does not do:

- **It does not invent an ICAO indicator.** `8WC` is a three-character FAA
  identifier with no ICAO equivalent; prefixing `K` would invent an aerodrome.
  `icao_location` is populated only where the FAA supplies one.
- **It does not force FAA domestic text through the ICAO parser.**
  `to_icao_notam()` returns `None` unless the text actually carries an ICAO
  header, so "not applicable" is distinguishable from "failed to parse".
- **It does not reconstruct the printed NOTAM number.** `08/430` leads with the
  month of issue; deriving it from `issued` is right until a NOTAM issued on
  the 1st carries the previous month's number — exactly when someone is
  searching for it by number.
- **It counts what it could not use.** `messages_seen`, `notams_read` and
  `messages_without_notam` are how a truncated download becomes visible instead
  of looking like a quiet day.

Two date widths are both real. The AIXM elements carry `YYYYMMDDHHMM`; the
printed text beside them carries the ICAO `YYMMDDHHMM`. Reading the first with
the second's rule yields month 25 and a silent `None`, which is how a feed
loses every validity window it has.

## Coverage this connector does not provide

NOTAM only. The FAA also publishes an eAIP, the digital chart supplement,
terminal procedures and obstacle data; none of it is connected, and none of it
is registered with a guessed URL. Those kinds come back from
`StateProfile.unknown_kinds()` as unknown, which is the truth. Nothing is
declared absent either — "we have not looked" must never be recorded as "the
State does not publish it".

The FAA is also the AIS authority for `PA`, `PH`, `PG`, `TJ`, `TI` and `NS`.
The profile is keyed on `K`; the others are recorded in its notes so a lookup
on PANC or TJSJ is a known gap rather than a silent miss.

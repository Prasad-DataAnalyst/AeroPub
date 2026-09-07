# FAA NMS-API — everything they told us

Written down here so the next person to touch this connector does not have to
find an email thread. Sources are the *Welcome to NMS* onboarding email, the
*NMS-API Frequently Asked Questions* (CGI Inc.), the `NMS-API Pre-Prod` SoapUI
project, and the sample initial-load AIXM and checklist JSON — all supplied
with API registration.

**No credential appears in this file or anywhere else in this repository.** See
[Credentials](#credentials).

**To bring the connection up, follow [docs/faa-connect-runbook.md](faa-connect-runbook.md)** — this file is the reference behind it.

Support: `7-AWA-NAIMES@faa.gov`, or 866-466-1336. Report test-environment
problems there; ask the same address for production onboarding once testing is
validated.

## Hosts

| Environment | Host | Source |
|---|---|---|
| FIT | `https://api-fit.cgifederal-aim.com` | OpenAPI spec + cURL examples |
| Staging / Pre-Prod | `https://api-staging.cgifederal-aim.com` | onboarding email, OpenAPI spec |
| Production | `https://api-nms.aim.faa.gov` | OpenAPI spec + cURL examples |

All three are now confirmed against the NMS-API OpenAPI specification
(v1.0.18, revised 2026-02-12) and the FAA's own cURL examples file.

**`api-fit`, not `api-sit`.** Earlier work here guessed "SIT" from an
initialism and built a hostname to match. No FAA document names `api-sit`;
both authoritative files say **FIT** — Field Integration Test. Anything set to
`sit` was pointed at a host that does not exist. The key is kept as an alias
resolving to the correct host, so an existing setting recovers rather than
failing in a new way.

**Production being named is not production being open.** The host is
confirmed; access is granted separately. Validate in staging, then request
onboarding at `7-AWA-NAIMES@faa.gov` or 866-466-1336.

Note the hosts are CGI Federal's, not `faa.gov`. Anyone allowlisting egress for
this connector needs `*.cgifederal-aim.com`, which is not an obvious guess.

## The four things that trip people up

**1. The token endpoint is not under `/nmsapi`.** The FAQ names this as the most
common failure. Data calls go to `https://<host>/nmsapi/v1/...`; the token call
goes to `https://<host>/v1/auth/token`. Same host, different root.

**2. The initial-load handover changed shape.** It used to return a Google Cloud
Storage V4 signed URL, where the correct behaviour is to send **no**
`Authorization` header — GCS signs the `host` header and nothing else, so a
bearer alongside the signature is two credentials at once and is rejected. It
now returns a relative `/nmsapi/v1/content/{token}` on the FAA's own host, and
that endpoint **requires the same bearer as every other call**. The `{token}` is
a Base64 representation of a signed URL which the NMS-API decrypts and proxies.

Both shapes remain possible, so `handover_needs_bearer()` decides from the URL:
a Google signature means no bearer *wherever the URL lives*; otherwise the
bearer travels only to the FAA's own host, and never off it.

**3. `nmsResponseFormat` is a required header on `/v1/notams`,** not an option.
Values are `AIXM` or `GEOJSON`. Omitting it is an error, not a default.

**4. The token call must not carry a JSON `Content-Type`.** Remove the header, or
set `application/x-www-form-urlencoded`. Tools that default to JSON get a
failure that looks like bad credentials.

## Authentication

OAuth2 `client_credentials`, HTTP Basic with the client id and secret:

```
curl -X POST --location "https://<host>/v1/auth/token" \
  -d grant_type=client_credentials \
  -u <CLIENT_ID>:<CLIENT_SECRET>
```

The response carries `access_token`, `token_type: BearerToken`, and:

- `expires_in` as the **string** `"1799"` — about 30 minutes, not a number
- `issued_at` in **milliseconds**
- `status: approved`, `api_product_list`, `organization_name`

**Keys do not expire; tokens do.** A 401 reading *"Access Token is Invalid or
Expired"* means the bearer lapsed, not that the credentials did. An invalid key
produces a different error.

Subsequent calls carry `Authorization: Bearer <token>`.

## Endpoints

All under `https://<host>/nmsapi`:

| Path | What it returns |
|---|---|
| `GET /v1/ping` | Liveness and credential check. The cheapest call that proves the whole chain. |
| `GET /v1/notams` | Filtered NOTAM. **Requires** the `nmsResponseFormat` header. |
| `GET /v1/notams/checklist` | Which NOTAM numbers the FAA holds as current. |
| `GET /v1/notams/il` | Handover to a bulk load of every active NOTAM. |
| `GET /v1/notams/il/{classification}` | As above, one classification. |
| `GET /v1/content/{token}` | Where a handover now points. Needs the bearer. |
| `GET /v1/locationseries` | Location-series mappings. |

**Every response carries `x-request-id`.** It is the correlation handle the
FAA asks for when reporting a problem, so capture it before raising a ticket.

### Query parameter rules

Parameters combine with logical AND, and several are only valid together:

- `notamNumber` requires `location`
- `latitude`, `longitude` and `radius` are used together; radius 0–100 NM
- `effectiveStartDate` and `effectiveEndDate` are used together
- `notamNumber` requires **`location` or `accountability`** — either will do
- `lastUpdatedDate` on `/v1/notams` is limited to a **24-hour** window and
  returns both active and inactive NOTAM. *(The FAQ and the SoapUI project both
  say 72 hours; the OpenAPI spec v1.0.18 says 24. The spec is newer and is what
  the gateway enforces — a 48-hour lookback that used to work now fails.)*
- `lastUpdatedDate` on `/v1/locationseries` must not exceed **5 days** in the
  past. Omitting it returns **a full initial load of all active
  Location-Series**, not a 1-hour window as the SoapUI project's description
  claims — so an unfiltered poll is far heavier than it looks
- Location-Series rows carry a status of New (`N`), Updated (`U`) or Deleted
  (`D`). A delta returns all three; an initial load excludes `D`
- `classification` **as the sole query parameter** on `/v1/notams` returns a
  relative content path rather than inline data, and that path expires in
  5 minutes. With any other parameter it returns data in the body
- A request taking longer than 30 s returns **408**, so a client timeout below
  that turns a slow answer into a different error
- `allowRedirect=false` on the initial-load endpoints returns the handover as
  JSON instead of a 307
- **A request with no query parameters at all is an error**, not "give me
  everything"

## Rate limits — strict, and enforced

| | Limit |
|---|---|
| Pre-production | **1 request per second**; content calls about 2 per second |
| Production, data | **1 pull every 3 minutes**, returning the previous 3 minutes |
| Initial load | **1 bulk pull per 24 hours**, whether by `/il` or full-classification |

More frequent use requires FAA approval **and** produces rate-limit errors.
These are encoded on `NmsEnvironment` (`min_request_interval`,
`min_data_pull_interval`, `min_initial_load_interval`) so the client paces
itself rather than relying on a caller to remember.

## Data formats

Responses are JSON. NOTAM content inside them is AIXM 5.1 or GeoJSON per the
`nmsResponseFormat` header. Initial-load bundles are gzipped SOAP envelopes
carrying an AIXM `FeatureCollection` with FAA `fnse` extensions.

The checklist is plain JSON:

```json
{"status": "Success",
 "data": {"checklist": [{"id": "…", "classification": "DOMESTIC",
                         "accountId": "ATL", "number": "09/186",
                         "location": "ATL", "icaoLocation": "KATL",
                         "lastUpdated": "2025-09-12T10:21:00Z"}]}}
```

Note `effectiveStart` and `effectiveEnd` inside AIXM come as **`YYYYMMDDHHMM`**
— twelve digits, not the ten-digit ICAO NOTAM form. Reading one with the other's
rule yields month 25 and a silent `None`; `aeropub.faa.aixm` branches on width
for exactly this reason.

## Credentials

Two are needed:

| Name | What |
|---|---|
| `AEROPUB_FAA_CLIENT_ID` | OAuth2 client id, issued with registration |
| `AEROPUB_FAA_CLIENT_SECRET` | OAuth2 client secret, issued with it |

### Installing them from the file, without ever seeing them

The FAA issues the credential twice, in two shapes, neither of which anything
can consume directly:

- an **encrypted spreadsheet** with a `Key` row and a `Secret` row, whose
  password arrives in a separate email;
- the **SoapUI project**, with the OAuth2 profile filled in — client id, client
  secret, and whatever bearer the person who exported it was holding, in plain
  text.

Import either directly (`pip install "aeropub[onboarding]"` first for the
spreadsheet):

```
aeropub credentials --import-pack AeroPub.xlsx --dry-run
aeropub credentials --import-pack AeroPub.xlsx      # prompts for the password
```

The value goes from the file the FAA sent to a mode-600 file outside any
repository, and is never rendered on the way. That is the whole point, and it
is not only about leakage. Transcribing this project's own secret from a
photograph of the spreadsheet got **one character wrong out of sixty-four** — a
lowercase `l` read as a capital `I`, the same picture in most screen fonts.
Both strings are 64 characters, both look right, and the gateway answers with a
401 indistinguishable from a revoked key, so the investigation goes to the
credential rather than the typo.

The leakage argument is the other half: an operator opening the file, finding a
64-character opaque string and moving it by hand into a terminal — where it
lands in shell history and the process list — or into a chat window to ask
which field is which, or into a screenshot for a ticket. Every one is a copy of
a live credential somewhere nobody is tracking, and nobody spots 64 characters
of opaque text in a scrollback.

**Beware the sample.** The SoapUI project the FAA ships is named `..._sample`
and carries the FAA's own demo key and secret, not yours. It is the right file
for reading endpoints and request shapes and the wrong one to authenticate
with. The spreadsheet is the credential.

The **access token is deliberately not imported**. A bearer in an exported
project is minutes old at best and is a third credential to leak; the client
mints its own from the id and secret on demand. Its presence is reported, so
the operator knows the pack is hazardous, and dropped.

The importer also reads which environment the pack is for off its own
`accessTokenURI` and says so. A pack's keys against another environment give a
401 that says nothing about why.

### Or by hand

```
aeropub credentials --set AEROPUB_FAA_CLIENT_ID       # prompts, never echoes
aeropub credentials --set AEROPUB_FAA_CLIENT_SECRET
aeropub credentials                                    # shows what is set, never a value
```

They are stored in `~/.aeropub/credentials.json`, owner-readable only, **outside
any repository** so they cannot be committed by accident. In a hosted
environment prefer real environment variables — they survive restarts and touch
no disk this project can read; the store checks the environment first.

**Earlier names.** `FAA_NMS_CLIENT_ID` and `FAA_NMS_CLIENT_SECRET` are still
read, so an installation predating the rename keeps working. `aeropub.faa.check`
says which name carried the value whenever it is not the current one — two live
names for one secret is how a rotated credential loses to a stale one still
sitting in an environment nobody remembers setting.

There was a period in which none of this worked: the connector read only the
environment, so a credential installed with `aeropub credentials --set` went
into a file the connector never opened and the check reported it missing. It
now resolves through the same store the command writes to, environment first
and file second.

`tests/test_credentials.py` scans every tracked file on each test run for
credential-shaped content, and fails the build if it finds any.

### If a credential leaks

The onboarding pack itself is a hazard: the SoapUI project ships with the client
id, the client secret and a live bearer token in **plain text**, and the FAA's
own FAQ says not to send those in the clear. If that file has been emailed or
shared, ask `7-AWA-NAIMES@faa.gov` to rotate the credentials, then
`aeropub credentials --set` the new ones. Nothing else in the platform changes,
because nothing else ever held them.

## Network egress

This connector needs outbound HTTPS to the CGI Federal hosts. In a restricted
environment the allowlist entry is `api-staging.cgifederal-aim.com` (and
`api-sit.cgifederal-aim.com` if testing there) — **not** any `faa.gov` host.

Run this first when anything fails:

```
aeropub netcheck --all          # or: python -m aeropub.netcheck --all
```

It probes every configured host, uses no credential and sends none, and names
who can fix what it finds. The exit code is the verdict, so a health check does
not have to read prose:

| Code | Means |
|---|---|
| `0` | every host answered — the network is not the problem |
| `1` | a host did not answer, and it is the authority's end |
| `2` | an egress proxy refused — a network administrator, not a code change |
| `3` | our own configuration, usually an untrusted intercepting CA |

An authority answering `401` counts as reachable: that proves DNS, the proxy,
TLS and its front door all work, and only the key is missing. The distinction
matters because the commonest wrong move after a blocked host is to rotate a
perfectly good credential — so a failed probe says in as many words that no key
was tested.

**As of this writing, this environment's egress refuses all three hosts** with a
`403` at the gateway before the request leaves the building. That is a policy on
our side, not anything the FAA or CGI did.

### When the allowlist is not coming

`--relay` ingests a bundle fetched on a machine that has a route, checked
against the FAA's own `numberReturned` and dated by its own `timeStamp`, and
cited as relayed rather than as something this platform fetched. See
[the runbook, step 3a](faa-connect-runbook.md).

### The allowlist request

Everything except the wire is verified: configuration resolves, both credential
halves are installed and readable, and the client is proven against a local
server implementing the FAA's documented contract over real TLS. What remains is
one allowlist entry. The request, in the form whoever owns egress needs it:

| | |
|---|---|
| **Hosts** | `api-staging.cgifederal-aim.com`, `api-sit.cgifederal-aim.com` |
| **Port** | 443, TLS |
| **Direction** | outbound only |
| **Owner** | CGI Federal, operating the FAA's NMS-API |
| **Why not `faa.gov`** | the API is not hosted on an FAA domain; allowlisting `faa.gov` does nothing |
| **Production** | `api-nms.aim.faa.gov` — **unconfirmed**, see [Hosts](#hosts). Do not allowlist it on our guess; ask the FAA for the real one when requesting production onboarding |

Confirm it took effect with `aeropub netcheck --all`, which needs no credential.
Exit `0` means every host answered.

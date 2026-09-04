# FAA NMS-API — everything they told us

Written down here so the next person to touch this connector does not have to
find an email thread. Sources are the *Welcome to NMS* onboarding email, the
*NMS-API Frequently Asked Questions* (CGI Inc.), the `NMS-API Pre-Prod` SoapUI
project, and the sample initial-load AIXM and checklist JSON — all supplied
with API registration.

**No credential appears in this file or anywhere else in this repository.** See
[Credentials](#credentials).

Support: `7-AWA-NAIMES@faa.gov`, or 866-466-1336. Report test-environment
problems there; ask the same address for production onboarding once testing is
validated.

## Hosts

| Environment | Host | Confirmed |
|---|---|---|
| SIT | `https://api-sit.cgifederal-aim.com` | yes — the FAQ's own token example |
| Staging / Pre-Prod | `https://api-staging.cgifederal-aim.com` | yes — the onboarding email |
| Production | `https://api-nms.aim.faa.gov` | **no** — assumed, see below |

The production host is **not named in any document supplied with
registration**. It is carried in `ENVIRONMENTS` with `confirmed=False` so the
check command says so rather than letting a guess look like a fact. The FAA
issues production details separately when onboarding is requested; correct it
with `AEROPUB_FAA_NMS_CONFIG` rather than editing code.

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

### Query parameter rules

Parameters combine with logical AND, and several are only valid together:

- `notamNumber` requires `location`
- `latitude`, `longitude` and `radius` are used together; radius 0–100 NM
- `effectiveStartDate` and `effectiveEndDate` are used together
- `lastUpdatedDate` on `/v1/notams` is limited to a **72-hour** window and
  returns both active and inactive NOTAM
- `lastUpdatedDate` on `/v1/locationseries` defaults to a **1-hour** window and
  must not exceed **5 days** in the past
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

Set them once:

```
aeropub credentials --set AEROPUB_FAA_CLIENT_SECRET   # prompts, never echoes
aeropub credentials                                    # shows what is set, never a value
```

They are stored in `~/.aeropub/credentials.json`, owner-readable only, **outside
any repository** so they cannot be committed by accident. In a hosted
environment prefer real environment variables — they survive restarts and touch
no disk this project can read; the store checks the environment first.

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
`python -m aeropub.netcheck` reports whether egress reaches them, without
needing a credential.

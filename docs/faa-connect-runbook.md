# Bringing the FAA NMS-API connection up

Written to be executed top to bottom, by a person or by Claude. Every step
says what "worked" looks like, so a failure is caught where it happens rather
than three steps later as a 401 that explains nothing.

Reference for *why* any of this is shaped the way it is: `docs/faa-nms.md`.

---

## 0. What you need before starting

| | |
|---|---|
| **Key** and **Secret** | the two columns of the FAA onboarding spreadsheet |
| **Egress** | outbound 443 to `*.cgifederal-aim.com` |
| **Environment** | `staging` — the onboarding email issues Pre-Prod credentials |

**The SoapUI project is not your credential.** The file the FAA ships is named
`..._sample.xml` and carries the FAA's own demo key and secret. It is useful
for reading endpoints and request shapes; it is the wrong pair to authenticate
with. Your pair is on the spreadsheet. This has already caught us once — the
sample pack's id and secret differ from the spreadsheet's, and installing the
pack's pair produces a 401 that looks exactly like a bad credential.

---

## 1. Install the credential

```
aeropub credentials --set AEROPUB_FAA_CLIENT_ID       # prompts; the spreadsheet KEY column
aeropub credentials --set AEROPUB_FAA_CLIENT_SECRET   # the SECRET column
```

Or, in anything hosted, set them as real environment variables instead — they
survive a restart and touch no disk. Both are read, environment first.

**Verify:**
```
aeropub credentials
```
Expect `set (48 characters, mixed)` for the id and `set (64 characters, mixed)`
for the secret. Those two lengths are the FAA's format; anything else means a
truncated paste, and a truncated secret is indistinguishable from a wrong one
once the gateway sees it.

> Typing a 64-character opaque string by hand is where these leak and where
> they get corrupted. If the FAA sent a SoapUI project whose profile has *your*
> keys in it, `aeropub credentials --import-pack <file>` moves them across
> without rendering them. Check the fingerprints against the spreadsheet first.

---

## 2. Point at the right environment

```
export FAA_NMS_ENVIRONMENT=staging
```

The default is `prod`. Pre-Prod credentials against production give a 401 that
says nothing about why, so this is not optional.

| Name | Host |
|---|---|
| `fit` | `https://api-fit.cgifederal-aim.com` |
| `staging` | `https://api-staging.cgifederal-aim.com` |
| `prod` | `https://api-nms.aim.faa.gov` |

---

## 3. Prove the network before blaming the key

```
aeropub netcheck --all
```

No credential is used or sent. An authority answering `401` counts as
**reachable** — that proves DNS, the proxy, TLS and its front door all work.

| Exit | Means | Who fixes it |
|---|---|---|
| `0` | every host answered | nobody — go to step 4 |
| `1` | a host did not answer | the authority |
| `2` | an egress proxy refused | your network administrator |
| `3` | our own config, usually an untrusted intercepting CA | you |

**On `2`** — the allowlist request, in the form a network team needs it:

> Permit outbound TLS on port 443 to `api-staging.cgifederal-aim.com` and
> `api-fit.cgifederal-aim.com`. These are CGI Federal hosts operating the FAA's
> NOTAM Management Service API. Allowlisting any `faa.gov` domain does **not**
> cover them.

Do not route around a `2`. It is a policy decision, and the connector is built
to report it rather than work around it.

---

## 4. Run the whole chain

```
python -m aeropub.faa.check
```

Four stages, each of which must pass before the next is attempted:

```
ok    configuration — Staging / Pre-Production
ok    credentials   — both halves present
ok    network       — reached api-staging.cgifederal-aim.com (HTTP 401 ...)
ok    token         — client_credentials grant accepted
ok    ping          — /v1/ping answered
```

Exit `0` is a verified connection. Anything else stops at the stage that
failed and names the remedy.

| Exit | Stage | First thing to check |
|---|---|---|
| `1` | credentials | both halves installed, right lengths |
| `2` | service unavailable | the FAA's end; retry, then report |
| `3` | protocol | a host or path moved — overlay it, see step 6 |
| `4` | network | step 3 |

---

## 5. Pull real data

```
python -m aeropub.faa.check --data --archive raw/
```

Fetches and parses the DOMESTIC initial load. `--archive` is required and not a
formality: the bundle is the evidence a NOTAM was cited from, and evidence that
is not stored cannot be cited later.

Expect roughly 20 000 NOTAM in the staging DOMESTIC load. A short read is
reported as a short read, never as a successful empty pull.

Then, in ordinary use:

```python
from aeropub.faa import NmsClient
client.notams(location="KDFW")                       # one aerodrome
client.notams(notam_number="10/108", location="KDFW")
client.notams(notam_number="10/108", accountability="ZFW")   # either works
client.notams(latitude=32.897, longitude=-97.037, radius=50)
client.checklist(location="KATL", classification="DOMESTIC")
```

### The limits that bite

- **`lastUpdatedDate` on `/v1/notams`: 24 hours.** The FAQ and the SoapUI
  project say 72. The OpenAPI spec v1.0.18 says 24, is newer, and is what the
  gateway enforces. A 48-hour lookback that used to work now fails.
- **Location-Series with no `lastUpdatedDate` returns everything**, not the
  1-hour window the SoapUI description claims. An unfiltered poll is far
  heavier than it looks.
- **Initial load: one bulk pull per 24 hours.** Pre-prod is 1 request/second.
  Production data is one pull per 3 minutes, returning the previous 3 minutes.
  More than that needs FAA approval and produces rate-limit errors meanwhile.
- **30 seconds is a 408.** Keep the client timeout above it.
- **`nmsResponseFormat` is required on `/v1/notams`** — `AIXM` or `GEOJSON`.
  Omitting it is an error, not a default.
- **The token endpoint is not under `/nmsapi`.** Data goes to
  `https://<host>/nmsapi/v1/...`; the token goes to `https://<host>/v1/auth/token`.
  The FAA names this as the most common failure.
- **The token call must not carry a JSON `Content-Type`.** Tools that default
  to JSON get a failure that looks like bad credentials.

---

## 6. When the FAA moves something

Correct it with an overlay, not a code change:

```
export AEROPUB_FAA_NMS_CONFIG=faa-hosts.json
```
```json
{"staging": {"host": "https://new-host.example"}}
```

The conformance suite proves the overlay path works, so a moved host is a
config edit rather than a release.

---

## 7. Going to production

Only after staging is validated. Email `7-AWA-NAIMES@faa.gov` or call
866-466-1336 to request production onboarding; they issue production access
separately. Then `FAA_NMS_ENVIRONMENT=prod`, re-run step 3 and step 4, and
allowlist `api-nms.aim.faa.gov`.

---

## Rotation

The FAA ships credentials in plain text — in a spreadsheet, and in the SoapUI
project — over email, and its own FAQ says not to send credentials in the
clear. Anything that has travelled that way should be reissued at
`7-AWA-NAIMES@faa.gov`, re-installed with step 1, and the original file
deleted. Keys do not expire; tokens do. A 401 reading *"Access Token is
Invalid or Expired"* means the bearer lapsed and the client will mint another —
it does not mean the key is bad.

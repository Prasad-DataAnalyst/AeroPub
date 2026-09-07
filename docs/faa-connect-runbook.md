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

**Read it from the file. Never transcribe it.**

```
pip install "aeropub[onboarding]"     # once — openpyxl and msoffcrypto-tool
aeropub credentials --import-pack AeroPub.xlsx --dry-run
aeropub credentials --import-pack AeroPub.xlsx
```

The workbook is encrypted and its password arrives in a separate email; you
are prompted for it, so it stays out of shell history. The value goes from the
FAA's own file to a mode-600 file outside any repository, and is never
rendered on the way. The same command reads a SoapUI project — pass whichever
file you were sent.

> **Why this matters, from this project's own history.** Transcribing this
> secret from a photograph of the spreadsheet got one character wrong out of
> sixty-four: a lowercase `l` read as a capital `I`, which in most screen fonts
> is the same picture. Both are 64 characters, both look right, and the gateway
> answers with a 401 that is indistinguishable from a revoked key — so the
> hours go into chasing the credential instead of the typo. Reading the file is
> the only version of this that cannot be wrong.

If you must do it by hand:

```
aeropub credentials --set AEROPUB_FAA_CLIENT_ID       # the spreadsheet Key row
aeropub credentials --set AEROPUB_FAA_CLIENT_SECRET   # the Secret row
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

**On `2`** — you have two options. The allowlist below, or **step 3a**, which
does not need one.

The allowlist request, in the form a network team needs it:

> Permit outbound TLS on port 443 to `api-staging.cgifederal-aim.com` and
> `api-fit.cgifederal-aim.com`. These are CGI Federal hosts operating the FAA's
> NOTAM Management Service API. Allowlisting any `faa.gov` domain does **not**
> cover them.

Do not route around a `2`. It is a policy decision, and the connector is built
to report it rather than work around it.

---

## 3a. When the allowlist is not coming

The data is not unavailable — it is one machine away. Anyone with a normal
connection fetches the bundle and hands the file over.

**On a machine that can reach CGI Federal**, two commands:

```
TOKEN=$(curl -s -X POST --location "https://api-staging.cgifederal-aim.com/v1/auth/token" \
  -d grant_type=client_credentials -u "$KEY:$SECRET" | python3 -c 'import json,sys;print(json.load(sys.stdin)["access_token"])')

curl -sL -o initial-load.gz "https://api-staging.cgifederal-aim.com/nmsapi/v1/notams/il/DOMESTIC" \
  --header "Authorization: Bearer $TOKEN"
```

**Then, here:**

```
python -m aeropub.faa.check --relay initial-load.gz --archive raw/ \
  --relayed-by "ops workstation"
```

Two things make this an ingest rather than an act of trust, and both come from
the FAA's own wrapper rather than from whoever carried the file:

- **`numberReturned`** — the count the FAA states, checked against what
  actually parses. A file truncated in transfer looks exactly like a quiet
  day, so a short read is refused rather than loaded, exit `3`. This is not
  theoretical: the sample bundle the FAA ships declares 21 468 NOTAM and
  contains two, and a naive loader reports it as a successful load.
- **`timeStamp`** — when the FAA generated the bundle. This dates the
  citation, *not* the moment the file was opened here. A baseline carried
  across a network boundary can be hours old, and dating it to its arrival
  would make stale NOTAM look fresh. Read from the file, so the person
  relaying it cannot fudge it. Anything older than 24 hours is called stale.

A relayed bundle is cited as **relayed** — `FAA-RELAY`, naming who handed it
over — never as something this platform fetched. The difference between a
moment we witnessed and one we were told about is the whole basis of a
citation.

The bundle comes back in the same shape a fetched one takes, so everything
downstream is unchanged.

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

### One location's active NOTAM

```
python -m aeropub.faa.check --notams OTHH --classification INTERNATIONAL \
  --archive raw/ --out othh-notams.json
```

`INTERNATIONAL` is the default and can be omitted — it is what this platform
is built on. US NOTAM are `DOMESTIC`, and `--classification ALL` disables the
filter. `--archive` is required: a NOTAM answered from a response nobody kept
is not citable.

> **The FAA is not the source of record for a foreign aerodrome.** It
> redistributes international NOTAM; the State's own AIS issues them. For OTHH
> that is Qatar CAA. The FAA copy can lag, can hold a subset, and is a
> different citation — useful, and not the same document. Where the two
> disagree, the State's own publication governs.
>
> An empty response cannot tell you which of two things happened: no active
> NOTAM, or this location not being in the FAA's holdings at all. The command
> says so rather than reporting a quiet aerodrome.

Then, in ordinary use:

```python
from aeropub.faa import NmsClient
client.notams(location="KDFW")                       # one aerodrome
client.notams(notam_number="10/108", location="KDFW")
client.notams(notam_number="10/108", accountability="ZFW")   # either works
client.notams(latitude=25.273, longitude=51.608, radius=50)      # around OTHH
client.notams(location="OTHH", classification="INTERNATIONAL")
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

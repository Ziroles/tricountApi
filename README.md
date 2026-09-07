# SplitTicket API

The service behind [SplitTicket](https://github.com/MathieuMarthy/triCountHelper): reading till
receipts with a vision AI, groups backed by Tricount, shared storage of receipts, and pushing
the expense.

> [!WARNING]
> **Legal and technical warning**
> Tricount publishes no programmable interface. This service uses
> [`tricount-api`](https://github.com/elrandar/tricount-api), an unofficial Python client
> reverse-engineered from the Android app.
> - This project is neither affiliated with nor endorsed by Tricount or bunq.
> - This use falls outside the service's terms of use.
> - The endpoints may change or disappear without notice.

---

## What the service does

- **Groups.** A group *is* a tricount, identified by its invitation code. Joining it brings back
  the list of its members: nobody types the participants in by hand any more.
- **Receipt scanning.** The call to Gemini happens here, with the user's key or the instance's.
  The result is **written onto the receipt before answering**: a connection that drops during
  the scan no longer wastes the call.
- **Shared receipts.** A receipt belongs to the group, not to the device. Optimistic lock by
  version: two simultaneous edits do not overwrite each other silently.
- **Identity by device.** A device enrols itself on first launch. An account is *optional*, and
  only serves to find your groups again elsewhere.
- **Pushing the expense.** Shares carry member uuids: no more matching by name.

Built on FastAPI and SQLite. The photos live on a volume, so does the database.

---

## Requirements

- **Recommended**: [Docker](https://docs.docker.com/get-docker/) and Docker Compose
- **Otherwise**: Python 3.12+ and `pip`

---

## Installation

### Docker Compose

1. **Clone**:
   ```bash
   git clone https://github.com/Ziroles/tricountApi.git
   cd tricountApi
   cp .env.example .env
   ```

2. **Generate the encryption key** — it protects users' Gemini keys at rest. Without it, the
   service refuses to store one rather than write it in the clear:
   ```bash
   python3 -c "import secrets; print(secrets.token_urlsafe(32))"
   ```
   Copy it into `.env` under `SPLITTICKET_SECRET_KEY`.

3. **Start**:
   ```bash
   docker compose up -d
   docker compose logs -f
   ```
   The service listens on `http://localhost:8787`. `GET /health` says what it can do, and
   `/docs` exposes the generated OpenAPI documentation.

### Without Docker

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export SPLITTICKET_SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
export SPLITTICKET_DATA_DIR=./data
uvicorn app.main:app --host 127.0.0.1 --port 8787
```

---

## Environment variables

| Variable | Role | Default |
|---|---|---|
| `SPLITTICKET_SECRET_KEY` | **Required** to store users' Gemini keys (encryption at rest). | *none* |
| `SPLITTICKET_SIGNUP_KEY` | Optional. Required to enrol a **new** device (`X-Signup-Key`): closes off a public instance without managing accounts. | empty (open) |
| `GEMINI_API_KEY` | Optional. Fallback key for when a user has none of their own. Empty = everyone brings their own, and the app says so. | empty |
| `GEMINI_MODEL` | Default model. | `gemini-2.5-flash` |
| `SPLITTICKET_DATA_DIR` | Database, photos, Tricount credentials. | `/data` |
| `SPLITTICKET_ALLOWED_ORIGINS` | Allowed origins, comma-separated. | `*` |
| `SPLITTICKET_IMAGE_RETENTION_DAYS` | Purge photos after N days. `0` = never. | `90` |
| `SPLITTICKET_HOST` / `SPLITTICKET_PORT` | Listen address. | `127.0.0.1` / `8787` |
| `TRICOUNT_CREDENTIALS_PATH` | Tricount device credentials. | `$DATA_DIR/.tricount-credentials.json` |

`*` as an allowed origin is fine for a personal deployment: authentication is a **bearer
token**, not a cookie, so there is no CSRF to worry about. Restricting it is still preferable in
public.

`SPLITTICKET_RELAY_KEY` no longer exists; `TRICOUNT_RELAY_KEY` is still read as a fallback for
`SPLITTICKET_SIGNUP_KEY`, so as not to break an existing deployment.

---

## Authentication

The user is never asked to sign up. On first launch, the app calls:

```http
POST /v1/devices
X-Signup-Key: <if the instance requires one>

→ 201 {"deviceId": "...", "token": "..."}
```

The token is returned **once only** and presented on every request afterwards:

```http
Authorization: Bearer <token>
```

The database only keeps its fingerprint: a copy of the file is not enough to impersonate a
device.

An **account** (`POST /v1/accounts`, `POST /v1/sessions`) is optional. It only serves to attach
several devices to the same groups. Attaching a device transfers its access to the account, so
that nothing disappears along the way.

---

## Routes

| | |
|---|---|
| `POST /v1/devices` | Enrol a device |
| `POST /v1/accounts` · `POST /v1/sessions` | Optional account |
| `GET /v1/me` · `PUT /v1/me/settings` | Gemini key, model |
| `GET /v1/models` | Models readable with the effective key |
| `GET · POST /v1/groups` | List, join by share link |
| `GET · DELETE /v1/groups/{id}` | View, leave (for yourself only) |
| `POST /v1/groups/{id}/members/refresh` | Resynchronise the members |
| `GET · POST /v1/groups/{id}/receipts` | Receipts of the group |
| `GET · PUT · DELETE /v1/receipts/{id}` | One receipt; `PUT` carries its `version` |
| `POST · GET /v1/receipts/{id}/image` | Photo |
| `POST /v1/receipts/{id}/scan` | OCR scan, written onto the receipt |
| `POST /v1/receipts/{id}/push` | Expense in the tricount |

Errors carry an actionable code: `{"detail": {"code": "no_gemini_key", "reason": "…"}}`.
`reason` is written to be displayed to the user as-is, in English.

### Concurrency

`PUT /v1/receipts/{id}` carries the `version` the client believes it is editing. If it is stale,
the server answers `409` **with the current receipt attached**: the client can explain what
happened and pick up again, instead of receiving a bare refusal.

### Gemini key

The order is: the user's key first, the instance's key second. Someone who went to the trouble
of saving their own wants their scans billed to them. If none is available, the response is
`400 {"code": "no_gemini_key"}`, which the app turns into an invitation to set one.

Keys are encrypted at rest (Fernet) and **never** come back out in full: the API only exposes a
hint, `AIza…7fQ`, enough for its owner to recognise which one is in place.

---

## Housekeeping

A background task runs at startup and then once a day: it deletes photos older than
`SPLITTICKET_IMAGE_RETENTION_DAYS`, along with files that no row references (leftovers from an
interrupted upload).

**Only the photo disappears.** The receipt lines were checked by a human on the verification
screen; the photo is only supporting evidence, useful for a few weeks. The receipt stays, and
its `imageId` goes back to `null` so the app knows there is nothing left to display rather than
asking for a missing file.

Setting `0` disables the purge — that is a choice, but the volume then grows without limit.

---

## Version compatibility

`GET /health` announces `contractVersion`. The app compares it to its own at startup and warns
the user on a mismatch, rather than failing later on a route that changed shape. Bump it on
every breaking change to the `/v1` surface.

---

## Scope and privacy

The service holds a **single** Tricount device identity for all of its users. Three accepted
consequences:

1. `list_tricounts()` would return the tricounts joined by *all* the users of the instance.
   **It is never called**: the list of groups comes from the `group_access` table, and from
   nowhere else. A device asking for a group it has no access to gets `404`, not `403` — having
   no right to it and it not existing must be indistinguishable.
2. Reading the members goes through `get_tricount`, not `join_tricount`: reading must not sign
   our bot up to someone else's tricount.
3. The quota is shared. If bunq cuts this bot off, the whole instance goes down.

So host it for yourself and the people close to you. An instance open to the public should at a
minimum set `SPLITTICKET_SIGNUP_KEY`, and do without `GEMINI_API_KEY` so that everyone pays for
their own scans.

---

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

Tricount and Gemini are faked: the suite costs nothing and creates no real expense.

`tests/test_normalize.py` and `tests/test_money.py` are the **case-for-case port** of the
TypeScript tests from the previous version. They are what guarantees that moving the OCR to the
server did not move a cent: as long as they pass, normalisation returns exactly the same amounts
the client used to return.

---

## Structure

```
app/
  main.py            Application, CORS, startup
  config.py          Environment variables
  db.py              SQLite, schema, migrations by user_version
  auth.py            Devices, accounts, owner
  crypto.py          Encryption of Gemini keys at rest
  models.py          HTTP contract (Pydantic)
  tricount_client.py Access to Tricount
  extraction/        money · normalize · prompt · gemini
  routes/            identity · groups · receipts
tests/
```

---

## Licence

Free software. Read Tricount/bunq's terms of use before any deployment.

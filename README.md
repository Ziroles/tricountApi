# Tricount API Relay

A lightweight, standalone HTTP relay to create expenses with custom splits on **Tricount** (bunq), designed primarily as a backend bridge for client applications such as [SplitTicket](https://github.com/MathieuMarthy/triCountHelper).

> [!WARNING]
> **Legal & Technical Disclaimer**:
> Tricount does not provide any official public API. This relay uses [`tricount-api`](https://github.com/elrandar/tricount-api), an unofficial Python client reverse-engineered from the Tricount Android application.
> - This project is neither affiliated with nor endorsed by Tricount or bunq.
> - Using this service falls outside Tricount's official Terms of Service.
> - Upstream endpoints may change or break at any time without notice.

---

## Features

- **No heavy frameworks**: Built entirely on Python standard library (`http.server`) and a minimal client library.
- **Full CORS support**: Handles browser preflight (`OPTIONS`) requests and CORS headers out of the box.
- **Token authentication**: Protected by a 32-character static secret key (`TRICOUNT_RELAY_KEY`) verified via constant-time comparison (`hmac.compare_digest`).
- **Device credential persistence**: Automatically generates and stores an Android device keypair (`.tricount-credentials.json` or Docker persistent volume) to avoid registering a new device on every restart.
- **Strict validation**: Guarantees transaction integrity (the sum of individual shares must match the total down to the exact cent) before dispatching to Tricount.

---

## Prerequisites

- **Recommended**: [Docker](https://docs.docker.com/get-docker/) & [Docker Compose](https://docs.docker.com/compose/)
- **Manual option**: Python 3.12+ and `pip`

---

## Installation & Getting Started

### Option 1: Docker Compose (Recommended)

1. **Clone the repository**:
   ```bash
   git clone https://github.com/Ziroles/tricountApi.git
   cd tricountApi
   ```

2. **Create your environment configuration**:
   ```bash
   cp .env.example .env
   ```

3. **Generate a 32-character secret key**:
   ```bash
   python3 -c "import secrets; print(secrets.token_urlsafe(24)[:32])"
   ```
   Edit `.env` and set `TRICOUNT_RELAY_KEY`:
   ```dotenv
   TRICOUNT_RELAY_KEY=paste_your_32_char_key_here
   ```

4. **Start the container**:
   ```bash
   docker compose up -d
   ```
   The relay will start listening on `http://localhost:8787`.

5. **View logs**:
   ```bash
   docker compose logs -f
   ```

6. **Stop the container**:
   ```bash
   docker compose down
   ```

---

### Option 2: Local Installation (without Docker)

1. **Clone the repository**:
   ```bash
   git clone https://github.com/Ziroles/tricountApi.git
   cd tricountApi
   ```

2. **Create a virtual environment and install dependencies**:
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

3. **Generate and set the required environment variable**:
   ```bash
   export TRICOUNT_RELAY_KEY=$(python3 -c "import secrets; print(secrets.token_urlsafe(24)[:32])")
   echo "Your relay key: $TRICOUNT_RELAY_KEY"
   ```

4. **Start the relay**:
   ```bash
   python relay.py
   ```
   The server will start on `http://localhost:8787` (or whatever `TRICOUNT_RELAY_HOST` / `TRICOUNT_RELAY_PORT` you configure).

---

## Environment Variables

| Variable | Description | Default |
|---|---|---|
| `TRICOUNT_RELAY_KEY` | **Required**. Static access key (must be **exactly 32 characters**). | *None (required)* |
| `TRICOUNT_RELAY_PORT` | HTTP server listening port. | `8787` |
| `TRICOUNT_RELAY_HOST` | Host address to bind to. | `localhost` (local) / `0.0.0.0` (Docker) |
| `TRICOUNT_CREDENTIALS_PATH` | Path to the credentials JSON file storing device tokens. | `../.tricount-credentials.json` or `/data/.tricount-credentials.json` |

---

## API Reference

### Authentication

Every request must include the 32-character key configured in `TRICOUNT_RELAY_KEY`, using one of the following headers:
- `Authorization: Bearer <YOUR_32_CHAR_KEY>`
- or `X-API-Key: <YOUR_32_CHAR_KEY>`

### Create Expense (`POST /`)

- **URL**: `http://localhost:8787/`
- **Method**: `POST`
- **Headers**:
  ```http
  Content-Type: application/json
  Authorization: Bearer <YOUR_32_CHAR_KEY>
  ```
- **Request Body (JSON)**:
  ```json
  {
    "action": "expense",
    "code": "tABCDE123456",
    "description": "Grocery shopping",
    "totalCents": 4250,
    "payerName": "Mathieu",
    "shares": [
      { "name": "Mathieu", "amountCents": 2125 },
      { "name": "Alice", "amountCents": 2125 }
    ],
    "date": "2026-09-02"
  }
  ```

#### Fields:
- `action` *(string)*: Must be `"expense"`.
- `code` *(string)*: Tricount share code (the ID at the end of `https://tricount.com/tABCDE123456`).
- `description` *(string)*: Title / description of the expense on Tricount.
- `totalCents` *(integer)*: Total transaction amount in cents (e.g. `4250` for $42.50).
- `payerName` *(string)*: Name of the person who paid. Must match an existing Tricount member name (case-insensitive).
- `shares` *(array)*: List of participants with their share (`amountCents`). Names must match existing Tricount members. The sum of all `amountCents` must equal `totalCents`.
- `date` *(string, optional)*: Purchase date formatted as `YYYY-MM-DD` or `YYYY-MM-DDTHH:MM:SS`. Defaults to current time if omitted.

#### Responses:
- **Success (`200 OK`)**:
  ```json
  {
    "ok": true,
    "value": {
      "id": "12345678"
    }
  }
  ```
- **Error (`400 Bad Request` / `401 Unauthorized` / `502 Bad Gateway`)**:
  ```json
  {
    "ok": false,
    "reason": "No member named \"Alice\" found in this tricount."
  }
  ```

---

## Integration with SplitTicket

1. Start `tricountApi` (e.g. on port 8787).
2. In SplitTicket ([`triCountHelper`](https://github.com/MathieuMarthy/triCountHelper)):
   - Enable the Tricount integration (`VITE_TRICOUNT_ENABLED=true` in `.env.local`).
   - Open **Settings** (gear icon).
   - Configure **Relay URL**: `http://localhost:8787` (or your reverse-proxy URL).
   - Enter **Relay Token**: your 32-character key.
3. Upon completing receipt splitting, use the "Send to Tricount" action to dispatch the expense automatically.

---

## License

Open source project. Please review third-party terms of service for Tricount/bunq prior to deployment.

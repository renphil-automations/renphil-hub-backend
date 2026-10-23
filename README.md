# RenPhil Hub — Backend API

Production-grade FastAPI backend for the Renaissance Philanthropy Hub.

## Features

| Feature | Endpoint prefix | Description |
|---------|----------------|-------------|
| **Google OAuth** | `/auth` | Sign-in restricted to `@renphil.org` emails |
| **Google Drive** | `/drive` | List / retrieve files from a shared Drive folder |
| **Dify.ai Chat** | `/dify` | Proxy chat queries to Dify.ai and return responses |
| **Embedded forms** | `/forms` | Proxy the form-builder service (list forms / embed info), save `form` component settings, and resolve a form component's embed URL (per-user token when required) |
| **Health** | `/health` | Simple liveness check |

## Project structure

```
Backend/
├── app/
│   ├── main.py              # FastAPI app factory + lifespan
│   ├── config.py            # Pydantic settings (env-driven)
│   ├── dependencies.py      # DI: services, current-user auth
│   ├── models/              # Pydantic request/response schemas
│   │   ├── auth.py
│   │   ├── dify.py
│   │   └── drive.py
│   ├── routers/             # Thin HTTP layer
│   │   ├── auth.py
│   │   ├── dify.py
│   │   └── drive.py
│   ├── services/            # Business logic
│   │   ├── auth_service.py
│   │   ├── dify_service.py
│   │   └── drive_service.py
│   └── helpers/             # Utilities & clients
│       ├── exceptions.py
│       ├── google_client.py
│       └── http_client.py
├── .env.example
├── requirements.txt
├── Dockerfile
└── README.md
```

## Quick start

```bash
# 1. Clone & enter the directory
cd Backend

# 2. Create a virtual environment
python -m venv .venv && source .venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Copy and fill in environment variables
cp .env.example .env
# → edit .env with real credentials

# 5. Run the development server
uvicorn app.main:app --reload

# 6. Open docs
# http://localhost:8000/docs
```

## Authentication flow

1. Frontend redirects the user to `GET /auth/login`.
2. User signs in via Google and is redirected to `/auth/callback`.
3. Backend verifies the email domain is `renphil.org`, then returns a JWT.
4. Frontend stores the JWT and sends it as `Authorization: Bearer <token>` on
   all subsequent requests.
5. Protected endpoints use the `get_current_user` dependency to validate the token.

## MCP OAuth (standalone RenPhil MCP)

Hub is the OAuth issuer for the standalone RenPhil MCP. Only four callers are
ever admitted: the Hub Agent (`MCP_OAUTH_AGENT_CLIENT_ID`, delegated tokens via
the service-authenticated `/mcp/oauth/agent-token`) and exactly one approved
client each for Claude, ChatGPT and Gemini (`MCP_OAUTH_CLIENTS_JSON` +
`MCP_OAUTH_CLIENT_PROVIDERS_JSON`). External clients must present a DPoP proof
at `/mcp/oauth/token`; dynamic client registration is disabled. Bot Management
(All Scopes admins) controls the master external toggle, each provider toggle
and its verification state, and the Hub Agent tool mode; admission is
re-checked on every MCP capability call. See `app/services/mcp_access_policy.py`.
`PUT /v2/bot-management/settings` requires `expected_version_id` (the active
version the form was loaded from). Saves are serialized with a row lock, and a
save based on an older version returns `409`, so a stale page can never
silently re-enable access another administrator switched off.

Schema: run the V2 Alembic chain before enabling MCP OAuth:

```bash
alembic upgrade head   # single root revision 0001_mcp_oauth
```

### Abuse protection for the public MCP OAuth endpoints

`/mcp/oauth/authorize`, `/mcp/oauth/token`, the discovery documents,
`/mcp/oauth/jwks.json` and the disabled registration routes
(`/mcp/oauth/register`, `/register`) are rate limited by
`app/services/mcp_oauth_rate_limit.py` through a route class used only by the
MCP OAuth routers. Hub Google login (`/auth/*`), session creation, access
checks, `/mcp/oauth/agent-token` (server-to-server, sync-token authenticated)
and every other Hub route are not limited.

- Shared, atomic fixed-window counters in the existing Upstash Redis (one
  `EVAL` per check; buckets are evaluated in order and a throttled caller
  does not consume the shared per-client/global buckets).
- Limits per 60 s unless noted: authorize 60/network, 600/approved client
  (60 shared for all unknown/malformed client IDs), 3000 global; token
  30/network, 300/approved client (30 unknown), 1500 global, plus at most 20
  failed exchanges per network per 10 minutes; registration 5/network, 60
  global; discovery and JWKS 120/network, 6000 global each.
- Over the limit: `429` with `Retry-After`, `Cache-Control: no-store` and
  `{"error": "temporarily_unavailable", ...}`. Store unavailable (or not
  configured) outside local development: `503` with `Retry-After: 5`. No
  in-memory fallback except for a loopback issuer without Upstash.
- Networks (IPv4 address or IPv6 /64, from the right-most
  `X-Forwarded-For` entry added by the hosting proxy) are stored only as an
  HMAC keyed from `JWT_SECRET_KEY`; client buckets are the approved provider
  name or `unknown`. No IP, email, code, token, PKCE verifier, redirect URI,
  client ID or body is written to Redis, logs or responses.
- No new environment variables.

Tests for this feature (`tests/test_mcp_oauth_*.py`,
`tests/test_mcp_access_policy.py`, `tests/test_bot_management_mcp.py`) are
tracked; run them with `python -m pytest tests/ -q`.

## Environment variables

See [`.env.example`](.env.example) for the full list with descriptions.

## Docker

```bash
docker build -t renphil-hub-api .
docker run -p 8000:8000 --env-file .env renphil-hub-api
```

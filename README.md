# RenPhil Hub — Backend API

Production-grade FastAPI backend for the Renaissance Philanthropy Hub.

## Features

| Feature | Endpoint prefix | Description |
|---------|----------------|-------------|
| **Google OAuth** | `/api/v1/auth` | Sign-in restricted to `@renphil.org` emails |
| **Google Drive** | `/api/v1/drive` | List / retrieve files from a shared Drive folder |
| **Dify.ai Chat** | `/api/v1/dify` | Proxy chat queries to Dify.ai and return responses |
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

1. Frontend redirects the user to `GET /api/v1/auth/login`.
2. User signs in via Google and is redirected to `/api/v1/auth/callback`.
3. Backend verifies the email domain is `renphil.org`, then returns a JWT.
4. Frontend stores the JWT and sends it as `Authorization: Bearer <token>` on
   all subsequent requests.
5. Protected endpoints use the `get_current_user` dependency to validate the token.

## Environment variables

See [`.env.example`](.env.example) for the full list with descriptions.

## Docker

```bash
docker build -t renphil-hub-api .
docker run -p 8000:8000 --env-file .env renphil-hub-api
```

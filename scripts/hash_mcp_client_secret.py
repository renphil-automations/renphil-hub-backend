"""Print a PBKDF2 hash of an MCP OAuth client secret for MCP_OAUTH_CLIENTS_JSON.

The secret is read from standard input and is never echoed or printed:

    python scripts/hash_mcp_client_secret.py            (prompts, no echo)
    <secret-manager-cli> | python scripts/hash_mcp_client_secret.py

Only the encoded hash is printed. Put it in the client's registry entry:

    {"<client id>": {"redirect_uris": ["https://..."],
                     "client_secret_hash": "pbkdf2_sha256$..."}}

The script needs no database, network or application settings.
"""

from __future__ import annotations

import getpass
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.services.mcp_oauth_client_secret import (  # noqa: E402
    MIN_NEW_SECRET_CHARS,
    ClientSecretHashError,
    hash_client_secret,
)


def read_secret(stream=None) -> str:
    stream = sys.stdin if stream is None else stream
    if stream.isatty():
        return getpass.getpass("Client secret (not echoed): ")
    # Exactly one line; only the line terminator is removed so a secret with
    # meaningful surrounding characters is preserved.
    return stream.readline().rstrip("\r\n")


def main(argv: list[str] | None = None) -> int:
    del argv
    try:
        secret = read_secret()
        if len(secret) < MIN_NEW_SECRET_CHARS:
            # The fast hash assumes a high-entropy, provider-generated secret.
            raise ClientSecretHashError(f"Client secret must be at least {MIN_NEW_SECRET_CHARS} characters.")
        print(hash_client_secret(secret))
    except ClientSecretHashError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

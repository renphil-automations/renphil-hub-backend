"""
Shared async HTTP client (httpx) managed via the FastAPI lifespan.
Avoids creating a new client per request and reuses connections.
"""

import httpx

_client: httpx.AsyncClient | None = None


async def init_http_client() -> None:
    """Create the global async HTTP client."""
    global _client
    _client = httpx.AsyncClient(timeout=30.0)


async def close_http_client() -> None:
    """Gracefully close the global async HTTP client."""
    global _client
    if _client:
        await _client.aclose()
        _client = None


def get_http_client() -> httpx.AsyncClient:
    """Return the shared async HTTP client instance."""
    if _client is None:
        raise RuntimeError("HTTP client not initialised. Call init_http_client() first.")
    return _client

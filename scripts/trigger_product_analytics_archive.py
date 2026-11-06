"""Call the protected analytics archive endpoint once from a scheduler."""

import os
import sys
from urllib.parse import urlsplit

import httpx

PATH = "/internal/product-analytics/archive/run"


def run(*, env=None, client=None, out=print):
    env = os.environ if env is None else env
    token = env.get("PRODUCT_ANALYTICS_ARCHIVE_TRIGGER_TOKEN", "")
    url = env.get("PRODUCT_ANALYTICS_ARCHIVE_TRIGGER_URL", "").strip()
    if len(token) < 32:
        out("Archive trigger token is missing or too short.")
        return 2
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
        allowed = parsed.scheme == "https" or (parsed.scheme == "http" and host in {"localhost", "127.0.0.1", "::1"})
        if not allowed or not host or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path != PATH:
            raise ValueError
        timeout = float(env.get("PRODUCT_ANALYTICS_ARCHIVE_TRIGGER_TIMEOUT_SECONDS", "1800"))
        if not 1 <= timeout <= 3600:
            raise ValueError
        max_requests = int(env.get("PRODUCT_ANALYTICS_ARCHIVE_TRIGGER_MAX_REQUESTS", "20"))
        if not 1 <= max_requests <= 100:
            raise ValueError
    except (TypeError, ValueError):
        out("Archive trigger URL or timeout is invalid.")
        return 2
    owned = client is None
    if owned:
        client = httpx.Client(follow_redirects=False, trust_env=False, timeout=timeout)
    try:
        for _ in range(max_requests):
            response = client.post(url, headers={"X-Analytics-Archive-Token": token}, timeout=timeout, follow_redirects=False)
            if response.status_code != 200:
                out(f"Archive endpoint returned HTTP {response.status_code}; check backend logs.")
                return 1
            try:
                body = response.json()
                status = body.get("status")
            except (ValueError, AttributeError):
                status = None
            if status == "disabled":
                out("Archive endpoint disabled.")
                return 0
            if status != "completed" or not isinstance(body.get("has_more"), bool):
                out("Archive endpoint returned an invalid success response.")
                return 1
            if not body["has_more"]:
                out("Archive endpoint completed; no further full batch was detected.")
                return 0
        out("Archive endpoint reached the request limit; more work may remain.")
        return 1
    except httpx.HTTPError as exc:
        out(f"Archive endpoint request failed: {type(exc).__name__}; check backend logs before retrying.")
        return 1
    finally:
        if owned:
            client.close()


if __name__ == "__main__":
    sys.exit(run())

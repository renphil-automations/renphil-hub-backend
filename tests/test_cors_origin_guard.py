"""CORS runs with ``allow_credentials=True``, so the origin list is guarded
at start-up: never a wildcard or ``null``, and outside local/debug only
HTTPS (loopback excepted)."""

from __future__ import annotations

from unittest.mock import patch

import pytest

import app.main as main_module
from app.config import Settings


def _settings(origins, *, debug=False, environment="production"):
    return Settings(ALLOWED_ORIGINS=origins, DEBUG=debug, HUB_ENVIRONMENT=environment)


@pytest.mark.parametrize("bad", ["*", " * ", "null", "NULL"])
@pytest.mark.parametrize("debug,environment", [(False, "production"), (True, "local"), (False, "local")])
def test_wildcard_and_null_are_refused_everywhere(bad, debug, environment):
    with pytest.raises(RuntimeError, match="ALLOWED_ORIGINS"):
        main_module.check_cors_origins(_settings(["https://hub.example", bad], debug=debug, environment=environment))


@pytest.mark.parametrize("bad", ["http://hub.example", "hub.example", "ftp://hub.example", "http://10.0.0.5:5000"])
@pytest.mark.parametrize("environment", ["production", "sandbox", ""])
def test_non_https_is_refused_outside_local_and_debug(bad, environment):
    with pytest.raises(RuntimeError, match="ALLOWED_ORIGINS"):
        main_module.check_cors_origins(_settings(["https://hub.example", bad], environment=environment))


@pytest.mark.parametrize(
    "origin",
    [
        "https://hub.example",
        "http://localhost:5173",
        "http://127.0.0.1:5000",
        "http://[::1]:5000",
        "http://app.localhost:5000",
    ],
)
def test_https_and_loopback_are_accepted_outside_local(origin):
    main_module.check_cors_origins(_settings([origin], environment="production"))


@pytest.mark.parametrize("debug,environment", [(True, "production"), (False, "local")])
def test_plain_http_is_allowed_locally_or_in_debug(debug, environment):
    main_module.check_cors_origins(_settings(["http://hub.test:5000"], debug=debug, environment=environment))


def test_create_app_refuses_to_start_with_a_wildcard_origin():
    with patch.object(main_module, "get_settings", return_value=_settings(["*"], debug=True, environment="local")):
        with pytest.raises(RuntimeError, match="ALLOWED_ORIGINS"):
            main_module.create_app()


def test_create_app_starts_with_a_valid_origin_list():
    with patch.object(main_module, "get_settings", return_value=_settings(["https://hub.example"])):
        assert main_module.create_app() is not None

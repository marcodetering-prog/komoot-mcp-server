"""Tests for AuthManager."""

import os

import pytest

from komoot_mcp.auth import AuthError, AuthManager


class TestAuthManager:
    def test_init_reads_env_vars(self):
        os.environ["KOMOOT_EMAIL"] = "test@example.com"
        os.environ["KOMOOT_PASSWORD"] = "secret123"
        am = AuthManager()
        assert am.email == "test@example.com"
        assert am.password == "secret123"
        assert am.user_id is None
        assert am.token is None

    def test_not_authenticated_initially(self):
        os.environ["KOMOOT_EMAIL"] = "test@example.com"
        os.environ["KOMOOT_PASSWORD"] = "secret123"
        am = AuthManager()
        assert not am.is_authenticated()

    def test_login_missing_credentials(self):
        os.environ.pop("KOMOOT_EMAIL", None)
        os.environ.pop("KOMOOT_PASSWORD", None)
        am = AuthManager()
        with pytest.raises(AuthError, match="environment variables"):
            am.login()

    def test_get_auth_headers_requires_auth(self):
        os.environ["KOMOOT_EMAIL"] = "test@example.com"
        os.environ["KOMOOT_PASSWORD"] = "secret123"
        am = AuthManager()
        with pytest.raises(AuthError, match="Not authenticated"):
            am.get_auth_headers()

    def test_get_user_id_requires_auth(self):
        os.environ["KOMOOT_EMAIL"] = "test@example.com"
        os.environ["KOMOOT_PASSWORD"] = "secret123"
        am = AuthManager()
        with pytest.raises(AuthError, match="Not authenticated"):
            am.get_user_id()

    def test_auth_headers_format(self):
        os.environ["KOMOOT_EMAIL"] = "test@example.com"
        os.environ["KOMOOT_PASSWORD"] = "secret123"
        am = AuthManager()
        am.user_id = "12345"
        am.token = "tok_abc"
        headers = am.get_auth_headers()
        assert "Authorization" in headers
        assert headers["Authorization"].startswith("Basic ")

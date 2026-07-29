"""AuthManager — per-instance Komoot credential holder.

Multi-tenant safe: each request gets its own AuthManager instance via
the ContextVar machinery in ``komoot_mcp.context``. Falls back to env
vars only when constructed with no explicit credentials (local stdio /
dev mode).
"""
import base64
import os

import requests


class AuthError(Exception):
    pass


class AuthManager:
    def __init__(self, email: str | None = None, password: str | None = None):
        # Explicit credentials win; otherwise fall back to env for stdio/dev.
        self.email = email if email is not None else os.environ.get("KOMOOT_EMAIL")
        self.password = password if password is not None else os.environ.get("KOMOOT_PASSWORD")
        self.user_id = None
        self.token = None

    def login(self):
        if not self.email or not self.password:
            raise AuthError("KOMOOT_EMAIL and KOMOOT_PASSWORD environment variables must be set")

        url = f"https://api.komoot.de/v006/account/email/{self.email}/"
        auth_str = base64.b64encode(f"{self.email}:{self.password}".encode()).decode()
        headers = {"Authorization": f"Basic {auth_str}"}

        try:
            resp = requests.get(url, headers=headers, timeout=30)
            if resp.status_code == 401 or resp.status_code == 403:
                raise AuthError("Invalid credentials. Check KOMOOT_EMAIL and KOMOOT_PASSWORD.")
            resp.raise_for_status()
            data = resp.json()
            self.user_id = data.get("username")
            self.token = data.get("password")
            if not self.user_id or not self.token:
                raise AuthError("Unexpected login response: missing user_id or token")
        except requests.exceptions.RequestException as e:
            raise AuthError(f"Login failed: {e}") from e

    def get_auth_headers(self):
        if not self.is_authenticated():
            raise AuthError("Not authenticated. Call login() first.")
        auth_str = base64.b64encode(f"{self.user_id}:{self.token}".encode()).decode()
        return {"Authorization": f"Basic {auth_str}"}

    def is_authenticated(self):
        return self.user_id is not None and self.token is not None

    def get_user_id(self):
        if not self.is_authenticated():
            raise AuthError("Not authenticated.")
        return self.user_id

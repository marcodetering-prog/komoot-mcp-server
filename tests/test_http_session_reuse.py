"""HTTP connection reuse tests for the direct-REST helpers.

Covers both halves of the change: the Session is reused across calls, and it
stays scoped to the per-request client rather than becoming a module global
carrying a tenant's credentials.

Patched at ``requests.Session.request`` (which ``get``/``post`` funnel
through) so no socket opens. ``autospec=True`` records the bound ``self``,
which is what lets us assert *which* Session served each call.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import requests

from komoot_mcp.auth import AuthManager
from komoot_mcp.client import KomootClient


class _NoLimit:
    async def acquire(self):
        return None


def _resp(status=200, json_body=None, text=""):
    """Build a fake ``requests.Response``-shaped object."""
    r = SimpleNamespace()
    r.status_code = status
    r.ok = 200 <= status < 300
    r.text = text

    def _json():
        if json_body is None:
            raise ValueError("no json")
        return json_body

    r.json = _json
    return r


def _make_client():
    am = AuthManager(email="t@x.com", password="pw")
    c = KomootClient(am, _NoLimit())
    # Pre-seed ``_api`` so ``_basic_auth`` never constructs a real kompy
    # connector (which would attempt a login round-trip).
    api = MagicMock()
    auth = MagicMock()
    auth.get_username.return_value = "12345"
    auth.get_password.return_value = "long-lived-token"
    api.authentication = auth
    c._api = api
    return c


@pytest.fixture
def client():
    return _make_client()


class TestSessionScoping:
    def test_client_owns_a_requests_session(self, client):
        assert isinstance(client._session, requests.Session)

    def test_two_clients_get_distinct_sessions(self):
        a, b = _make_client(), _make_client()
        assert a._session is not b._session

    def test_no_shared_session_at_module_or_class_scope(self):
        """A process-wide Session would pool one tenant's connections for all.

        Checks the class body too: ``KomootClient._session = Session()`` there
        would be shared by every tenant, which is the regression that actually
        matters, and it lives in ``vars(KomootClient)``, not the module.
        """
        from komoot_mcp import client as client_mod

        shared = [
            f"{scope.__name__}.{name}"
            for scope in (client_mod, KomootClient)
            for name, value in vars(scope).items()
            if isinstance(value, requests.Session)
        ]
        assert shared == []

    def test_session_auth_is_never_set(self, client):
        """session.auth would outlive the call and defeat no_auth=True."""
        assert client._session.auth is None


class TestSessionReuse:
    @pytest.mark.asyncio
    async def test_repeated_gets_reuse_one_session(self, client):
        with patch.object(
            requests.Session, "request", autospec=True,
        ) as mock_req:
            mock_req.return_value = _resp(200, {"id": 1})
            for _ in range(3):
                await client._http_get_json("https://api.komoot.de/v007/x")

        assert mock_req.call_count == 3
        # args[0] is the bound self, thanks to autospec.
        sessions = {id(call.args[0]) for call in mock_req.call_args_list}
        assert sessions == {id(client._session)}

    @pytest.mark.asyncio
    async def test_get_post_and_request_all_share_one_session(self, client):
        with patch.object(
            requests.Session, "request", autospec=True,
        ) as mock_req:
            mock_req.return_value = _resp(200, {"id": 7})
            await client._http_get_json("https://api.komoot.de/v007/tours/1")
            await client._http_request(
                "PATCH", "https://api.komoot.de/v007/tours/1",
                json_body={"name": "x"},
            )
            await client.save_planned_tour({"distance": 1}, name="x")

        assert mock_req.call_count == 3
        sessions = {id(call.args[0]) for call in mock_req.call_args_list}
        assert sessions == {id(client._session)}

    @pytest.mark.asyncio
    async def test_two_clients_do_not_share_a_session(self):
        a, b = _make_client(), _make_client()
        with patch.object(
            requests.Session, "request", autospec=True,
        ) as mock_req:
            mock_req.return_value = _resp(200, {"id": 1})
            await a._http_get_json("https://api.komoot.de/v007/x")
            await b._http_get_json("https://api.komoot.de/v007/x")

        used = [id(call.args[0]) for call in mock_req.call_args_list]
        assert used == [id(a._session), id(b._session)]
        assert used[0] != used[1]


class TestPlannerSharesTheClientSession:
    """plan_and_upload does planner POST + save POST, both to www.komoot.com."""

    @pytest.mark.asyncio
    async def test_planner_and_save_share_one_session(self, client):
        from komoot_mcp.routing import KomootNativePlanner

        planner = KomootNativePlanner(
            auth_pair=client._basic_auth(), session=client._session,
        )
        assert planner._session is client._session

        with patch.object(
            requests.Session, "request", autospec=True,
        ) as mock_req:
            mock_req.return_value = _resp(200, {"distance": 1000})
            planner.plan(
                waypoints=[(47.9, 7.8), (48.0, 7.9)], sport_komoot="hike",
            )
            await client.save_planned_tour({"distance": 1000}, name="x")

        assert mock_req.call_count == 2
        sessions = {id(call.args[0]) for call in mock_req.call_args_list}
        assert sessions == {id(client._session)}

    def test_planner_falls_back_to_a_private_session(self):
        from komoot_mcp.routing import KomootNativePlanner

        a = KomootNativePlanner(auth_pair=("u", "t"))
        b = KomootNativePlanner(auth_pair=("u", "t"))
        assert isinstance(a._session, requests.Session)
        assert a._session is not b._session


class TestRequestKwargsUnchanged:
    """The Session swap must not alter a single per-call argument."""

    @pytest.mark.asyncio
    async def test_http_get_json_kwargs(self, client):
        with patch.object(
            requests.Session, "request", autospec=True,
        ) as mock_req:
            mock_req.return_value = _resp(200, {"ok": True})
            out = await client._http_get_json(
                "https://api.komoot.de/v007/tours/42", params={"page": 0},
            )

        assert out == {"ok": True}
        call = mock_req.call_args
        # Session.get forwards as request("GET", url, **kwargs).
        assert call.args[1] == "GET"
        assert call.args[2] == "https://api.komoot.de/v007/tours/42"
        assert call.kwargs["auth"] == ("12345", "long-lived-token")
        assert call.kwargs["params"] == {"page": 0}
        assert call.kwargs["timeout"] == 30
        assert (
            call.kwargs["headers"]["Accept"]
            == "application/hal+json, application/json"
        )
        assert call.kwargs["headers"]["User-Agent"] == "komoot-mcp-server"

    @pytest.mark.asyncio
    async def test_http_request_kwargs(self, client):
        with patch.object(
            requests.Session, "request", autospec=True,
        ) as mock_req:
            mock_req.return_value = _resp(200, {"id": 42}, text="{}")
            await client._http_request(
                "PATCH", "https://api.komoot.de/v007/tours/42",
                params={"hl": "en"}, json_body={"name": "new"},
            )

        call = mock_req.call_args
        assert call.kwargs["method"] == "PATCH"
        assert call.kwargs["url"] == "https://api.komoot.de/v007/tours/42"
        assert call.kwargs["params"] == {"hl": "en"}
        assert call.kwargs["json"] == {"name": "new"}
        assert call.kwargs["auth"] == ("12345", "long-lived-token")
        assert call.kwargs["timeout"] == 30

    @pytest.mark.asyncio
    async def test_no_auth_branch_sends_no_credentials(self, client):
        """Share-token URLs authenticate via query param, not Basic auth."""
        with patch.object(
            requests.Session, "request", autospec=True,
        ) as mock_req:
            mock_req.return_value = _resp(200, {"id": 1}, text="{}")
            await client._http_request(
                "GET", "https://www.komoot.com/api/v007/tours/1",
                params={"share_token": "abc"}, no_auth=True,
            )

        assert mock_req.call_args.kwargs["auth"] is None
        assert client._session.auth is None

    @pytest.mark.asyncio
    async def test_save_planned_tour_kwargs(self, client):
        with patch.object(
            requests.Session, "request", autospec=True,
        ) as mock_req:
            mock_req.return_value = _resp(201, {"id": 99})
            out = await client.save_planned_tour(
                {"distance": 1000}, name="Feldberg", status="private",
            )

        assert out == {"id": 99, "status": "saved"}
        call = mock_req.call_args
        assert call.args[1] == "POST"
        assert call.kwargs["auth"] == ("12345", "long-lived-token")
        assert call.kwargs["timeout"] == 60
        assert call.kwargs["json"]["type"] == "tour_planned"
        assert call.kwargs["json"]["name"] == "Feldberg"

    @pytest.mark.asyncio
    async def test_upload_gpx_goes_through_the_session(self, client):
        gpx = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<gpx version="1.1" creator="test" '
            'xmlns="http://www.topografix.com/GPX/1/1">'
            "<trk><name>t</name><trkseg>"
            '<trkpt lat="49.0" lon="8.4"><ele>120</ele></trkpt>'
            '<trkpt lat="49.01" lon="8.41"><ele>125</ele></trkpt>'
            "</trkseg></trk></gpx>"
        )
        client._api.authentication.get_email_address.return_value = "t@x.com"

        with patch.object(
            requests.Session, "request", autospec=True,
        ) as mock_req:
            mock_req.return_value = _resp(201, {"id": 555})
            out = await client.upload_gpx_capture_id(
                gpx_content=gpx, sport="hike", tour_name="t",
            )

        assert out == {"id": 555, "status": "uploaded"}
        call = mock_req.call_args
        assert call.args[0] is client._session
        assert call.args[1] == "POST"
        # Upload still uses the email/token pair and a raw body, not JSON.
        assert call.kwargs["auth"] == ("t@x.com", "long-lived-token")
        assert call.kwargs["params"]["data_type"] == "gpx"
        assert b"<trkpt" in call.kwargs["data"]


class TestSessionLifecycle:
    def test_close_closes_the_session(self, client):
        closed = []
        client._session.close = lambda: closed.append(True)
        client.close()
        assert closed == [True]

    def test_close_is_idempotent_and_never_raises(self, client):
        client.close()
        client.close()

    def test_clear_request_state_closes_a_client_it_can_see(self):
        """Only holds same-context. See the cross-task test below for prod."""
        from komoot_mcp import context as ctx

        ctx.clear_request_state()
        token = ctx.set_auth_manager(
            AuthManager(email="t@x.com", password="pw")
        )
        try:
            c = ctx.get_client()
            closed = []
            c._session.close = lambda: closed.append(True)
            ctx.clear_request_state()
            assert closed == [True]
            fresh = ctx.get_client()
            assert fresh is not c
            assert fresh._session is not c._session
        finally:
            try:
                ctx.reset_auth_manager(token)
            except (ValueError, LookupError):
                pass
            ctx.clear_request_state()

    @pytest.mark.asyncio
    async def test_client_built_in_a_child_task_is_invisible_to_teardown(self):
        """Pins the deployed topology: teardown cannot see the client.

        Tools call ``get_client()`` inside the MCP handler's task, and a
        ``ContextVar.set`` there does not propagate back to the middleware
        that later runs ``clear_request_state()``. So the Session close is a
        no-op in production and GC reclaims the sockets. Documented here so
        the limitation is visible rather than implied by a passing test.
        """
        import asyncio

        from komoot_mcp import context as ctx

        ctx.clear_request_state()
        token = ctx.set_auth_manager(
            AuthManager(email="t@x.com", password="pw")
        )
        try:
            closed = []

            async def handler():
                c = ctx.get_client()
                c._session.close = lambda: closed.append(True)

            await asyncio.create_task(handler())

            assert ctx._client_var.get() is None
            ctx.clear_request_state()
            assert closed == []
        finally:
            try:
                ctx.reset_auth_manager(token)
            except (ValueError, LookupError):
                pass
            ctx.clear_request_state()

    def test_clear_request_state_without_a_client_is_a_noop(self):
        from komoot_mcp import context as ctx

        ctx.clear_request_state()
        ctx.clear_request_state()

"""Issue #17 — ``komoot_upload_tour`` must not render a failure as
"Tour uploaded successfully: False".

kompy's ``upload_tour`` returns a bool (True on 201/202, False on
anything else). The old tool wrapper rendered the bool with f-string
interpolation, so a failure surfaced as the literal string
``Tour uploaded successfully: False`` — which reads like success.

The fix is two-fold:

* The client now treats a False return as a failure and raises
  ``KomootAPIError`` with a real diagnostic message.
* The tool wrapper renders dict results with the new tour ID + URL
  when available, and a plain success line otherwise — never the raw
  bool.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from komoot_mcp.auth import AuthManager
from komoot_mcp.client import KomootAPIError, KomootClient
from komoot_mcp.context import clear_request_state

GPX_SAMPLE = """<?xml version="1.0" encoding="UTF-8"?>
<gpx version="1.1" creator="test" xmlns="http://www.topografix.com/GPX/1/1">
  <trk><name>inline-track</name>
    <trkseg>
      <trkpt lat="49.0" lon="8.4"><ele>120</ele></trkpt>
      <trkpt lat="49.01" lon="8.41"><ele>125</ele></trkpt>
    </trkseg>
  </trk>
</gpx>
"""


class _NoLimit:
    async def acquire(self):
        return None


@pytest.fixture(autouse=True)
def _reset():
    clear_request_state()
    yield
    clear_request_state()


@pytest.fixture
def client():
    am = AuthManager(email="t@x.com", password="pw")
    return KomootClient(am, _NoLimit())


def _install_failing_upload(client):
    """Mock kompy.upload_tour to return False (Komoot rejected)."""
    api = MagicMock()
    api.upload_tour = MagicMock(return_value=False)
    client._api = api


def _install_succeeding_upload_bool(client):
    """Mock kompy.upload_tour to return True (older kompy behaviour)."""
    api = MagicMock()
    api.upload_tour = MagicMock(return_value=True)
    client._api = api


class TestClientRaisesOnFalseReturn:
    """At the client layer, a False return must become an exception."""

    @pytest.mark.asyncio
    async def test_false_kompy_return_becomes_komoot_api_error(self, client):
        _install_failing_upload(client)
        with pytest.raises(KomootAPIError) as ei:
            await client.upload_tour(
                gpx_content=GPX_SAMPLE, sport="hike",
            )
        msg = str(ei.value).lower()
        # Must NOT silently coerce False into "successfully" language.
        assert "successfully" not in msg
        assert "rejected" in msg or "status" in msg

    @pytest.mark.asyncio
    async def test_none_return_also_raises(self, client):
        api = MagicMock()
        api.upload_tour = MagicMock(return_value=None)
        client._api = api
        with pytest.raises(KomootAPIError):
            await client.upload_tour(
                gpx_content=GPX_SAMPLE, sport="hike",
            )

    @pytest.mark.asyncio
    async def test_true_kompy_return_becomes_normalized_dict(self, client):
        _install_succeeding_upload_bool(client)
        out = await client.upload_tour(
            gpx_content=GPX_SAMPLE, sport="hike",
        )
        assert out == {"id": None, "status": "uploaded"}


class TestUploadToolSurfacesFailureCleanly:
    """At the MCP tool layer, the output string must not mislead."""

    @pytest.mark.asyncio
    async def test_failure_string_does_not_say_successfully(self, monkeypatch):
        from komoot_mcp.tools import write_tools

        registered = {}

        class _Mcp:
            def tool(self):
                def deco(fn):
                    registered[fn.__name__] = fn
                    return fn
                return deco

        write_tools.register(_Mcp())

        class _FakeClient:
            async def upload_tour(self, **kwargs):
                raise KomootAPIError(
                    "Komoot rejected the upload (HTTP 400) — GPX is "
                    "probably in route format."
                )

        monkeypatch.setattr(write_tools, "get_client", lambda: _FakeClient())

        out = await registered["komoot_upload_tour"](
            gpx_content=GPX_SAMPLE,
        )
        # The literal bug shape from issue #17 must not appear.
        assert "successfully: False" not in out
        assert "Tour uploaded successfully: False" not in out
        # Output must read as an error.
        assert out.startswith("Error uploading tour")
        assert "400" in out

    @pytest.mark.asyncio
    async def test_success_with_id_renders_url(self, monkeypatch):
        from komoot_mcp.tools import write_tools

        registered = {}

        class _Mcp:
            def tool(self):
                def deco(fn):
                    registered[fn.__name__] = fn
                    return fn
                return deco

        write_tools.register(_Mcp())

        class _FakeClient:
            async def upload_tour(self, **kwargs):
                return {"id": 123456, "status": "uploaded"}

        monkeypatch.setattr(write_tools, "get_client", lambda: _FakeClient())

        out = await registered["komoot_upload_tour"](
            gpx_content=GPX_SAMPLE,
        )
        assert "Tour uploaded successfully" in out
        assert "123456" in out
        assert "https://www.komoot.com/tour/123456" in out

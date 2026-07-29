"""Regression tests for issue #14 — ``komoot_upload_tour`` must accept
inline GPX content under the multi-tenant gateway.

The old shape took only ``filepath`` and called ``open(filepath, 'rb')``,
which is useless under the gateway because the MCP server can't reach
the caller's filesystem. Same architectural class as #9 / #11, already
fixed for downloads in PR #12.

The fix adds ``gpx_content`` (preferred), keeps ``filepath`` for
stdio / local-dev backward compat, and improves the error message when
``filepath`` is missing — hinting at ``gpx_content``.
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


@pytest.fixture
def client():
    am = AuthManager(email="t@x.com", password="pw")
    return KomootClient(am, _NoLimit())


@pytest.fixture(autouse=True)
def _reset_request_state():
    clear_request_state()
    yield
    clear_request_state()


def _install_mock_api(client):
    """Install a kompy mock that captures upload_tour kwargs and returns
    a stable success payload.
    """
    captured = {}

    def fake_upload(**kwargs):
        captured.update(kwargs)
        return {"id": 999, "name": kwargs.get("tour_name")}

    api = MagicMock()
    api.upload_tour = MagicMock(side_effect=fake_upload)
    client._api = api
    return captured


class TestUploadFromGpxContent:
    """gpx_content is the gateway-mode path — no disk access."""

    @pytest.mark.asyncio
    async def test_uploads_via_inline_gpx_content(self, client):
        captured = _install_mock_api(client)
        result = await client.upload_tour(
            gpx_content=GPX_SAMPLE, sport="hike",
        )
        # The mock returns what we gave it, so we know the call landed.
        assert result["id"] == 999
        # kompy.upload_tour got a parsed gpxpy GPX object (not the raw
        # string, not bytes) — proving we parsed in memory rather than
        # writing the content to a temp file.
        tour_obj = captured["tour_object"]
        assert hasattr(tour_obj, "tracks")
        assert captured["activity_type"] == "hike"
        # The tour name falls back to the GPX track name when not
        # explicitly provided.
        assert captured["tour_name"] == "inline-track"

    @pytest.mark.asyncio
    async def test_does_not_open_any_file_when_gpx_content_used(
        self, client, monkeypatch,
    ):
        """Sentinel: prove no disk reads happen on the gpx_content path."""
        _install_mock_api(client)

        def guarded_open(path, mode="r", *args, **kwargs):
            raise AssertionError(
                f"gpx_content path must not touch disk; got "
                f"open({path!r}, {mode!r})"
            )

        monkeypatch.setattr("builtins.open", guarded_open)
        # Should succeed without ever opening anything.
        await client.upload_tour(gpx_content=GPX_SAMPLE)

    @pytest.mark.asyncio
    async def test_explicit_tour_name_wins(self, client):
        captured = _install_mock_api(client)
        await client.upload_tour(
            gpx_content=GPX_SAMPLE, tour_name="my-custom-name",
        )
        assert captured["tour_name"] == "my-custom-name"

    @pytest.mark.asyncio
    async def test_gpx_content_takes_precedence_over_filepath(
        self, client, tmp_path,
    ):
        """When both are provided, gpx_content wins. The filepath must
        not be touched — even if it doesn't exist."""
        captured = _install_mock_api(client)
        # Point filepath at a nonexistent location — if the code falls
        # through to the filepath branch it'll raise. It must not.
        await client.upload_tour(
            gpx_content=GPX_SAMPLE,
            filepath=str(tmp_path / "does-not-exist.gpx"),
            sport="hike",
        )
        # Used the inline content's track name, not the bogus filepath.
        assert captured["tour_name"] == "inline-track"

    @pytest.mark.asyncio
    async def test_rejects_inline_content_for_non_gpx_data_type(
        self, client,
    ):
        """FIT/TCX are binary — gpx_content (a string) can't carry them.
        The error message should make that clear."""
        _install_mock_api(client)
        with pytest.raises(KomootAPIError) as ei:
            await client.upload_tour(
                gpx_content="not really fit bytes", data_type="fit",
            )
        msg = str(ei.value)
        assert "gpx_content" in msg
        assert "FIT" in msg or "fit" in msg


class TestUploadFromFilepath:
    """Legacy stdio / local-dev path — must keep working."""

    @pytest.mark.asyncio
    async def test_uploads_via_filepath(self, client, tmp_path):
        captured = _install_mock_api(client)
        gpx_file = tmp_path / "ride.gpx"
        gpx_file.write_text(GPX_SAMPLE)

        result = await client.upload_tour(
            filepath=str(gpx_file), sport="touringbicycle",
        )
        assert result["id"] == 999
        # Name derived from the filename (no track name on the parsed
        # object would be picked up here because we go through the
        # filepath branch, which uses the basename).
        assert captured["tour_name"] == "ride"
        assert captured["activity_type"] == "touringbicycle"


class TestErrorMessages:
    """The error surface is the contract under the gateway. If a user
    blindly passes a local path, the message must point them at
    ``gpx_content``.
    """

    @pytest.mark.asyncio
    async def test_neither_filepath_nor_content_raises_clear_error(
        self, client,
    ):
        _install_mock_api(client)
        with pytest.raises(KomootAPIError) as ei:
            await client.upload_tour()
        msg = str(ei.value)
        assert "gpx_content" in msg
        assert "filepath" in msg

    @pytest.mark.asyncio
    async def test_missing_filepath_hints_at_gpx_content(self, client):
        _install_mock_api(client)
        bogus = "/definitely/not/a/real/path/ride.gpx"
        with pytest.raises(KomootAPIError) as ei:
            await client.upload_tour(filepath=bogus)
        msg = str(ei.value)
        # The improved error must call out the gateway and gpx_content.
        assert "gpx_content" in msg
        assert "gateway" in msg.lower() or "filesystem" in msg.lower()
        assert bogus in msg


class TestToolWrapper:
    """The MCP tool wrapper must accept the new kwarg without changing
    behavior for legacy callers.
    """

    @pytest.mark.asyncio
    async def test_tool_accepts_gpx_content(self, monkeypatch):
        from komoot_mcp import context as ctx_mod
        from komoot_mcp.tools import write_tools

        registered = {}

        class _Mcp:
            def tool(self):
                def deco(fn):
                    registered[fn.__name__] = fn
                    return fn
                return deco

        write_tools.register(_Mcp())

        captured = {}

        class _FakeClient:
            async def upload_tour(self, **kwargs):
                captured.update(kwargs)
                return {"id": 42}

        monkeypatch.setattr(write_tools, "get_client", lambda: _FakeClient())
        monkeypatch.setattr(ctx_mod, "get_client", lambda: _FakeClient())

        out = await registered["komoot_upload_tour"](
            gpx_content=GPX_SAMPLE, sport="hike", tour_name="x",
        )
        assert "Tour uploaded successfully" in out
        assert captured["gpx_content"] == GPX_SAMPLE
        assert captured["sport"] == "hike"
        assert captured["tour_name"] == "x"
        # filepath stays None on the gateway path.
        assert captured["filepath"] is None

    @pytest.mark.asyncio
    async def test_tool_surfaces_clear_error_when_nothing_provided(
        self, monkeypatch,
    ):
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
                    "Either gpx_content (GPX XML as a string) or "
                    "filepath (path readable by the MCP server) must "
                    "be provided."
                )

        monkeypatch.setattr(write_tools, "get_client", lambda: _FakeClient())

        out = await registered["komoot_upload_tour"]()
        assert "Error uploading tour" in out
        assert "gpx_content" in out

"""Tour data tools for Komoot MCP server."""

from komoot_mcp.context import get_client


def _format_gpx_response(label: str, gpx: str) -> str:
    """Wrap GPX XML in a fenced code block with a byte-count header.

    Returns a tool-result string of the shape::

        GPX for <label> (<N> bytes):
        ```xml
        <gpx>...</gpx>
        ```

    The full GPX body is always returned verbatim — callers need the
    complete content to upload back to Komoot or save locally. Real-
    world planned routes routinely exceed 300–500 KB; MCP / JSON-RPC
    handles payloads of that size fine, so no size cap is applied.
    Designed for issue #9: callers behind the multi-tenant gateway
    have no access to the server's filesystem.
    """
    size = len(gpx)
    return f"GPX for {label} ({size} bytes):\n```xml\n{gpx}\n```"


def register(mcp):
    @mcp.tool()
    async def komoot_get_tour_coordinates(tour_id: int) -> str:
        """Get the coordinate array (lat, lng, altitude) for a tour.

        Args:
            tour_id: The numeric tour ID
        """
        try:
            coords = await get_client().get_tour_coordinates(tour_id)
            if not coords:
                return "No coordinates found."
            lines = [f"Tour {tour_id}: {len(coords)} coordinate points"]
            for i, c in enumerate(coords[:5]):
                if isinstance(c, dict):
                    lines.append(f"  [{i}] lat={c.get('lat')}, lng={c.get('lng')}, alt={c.get('alt', '?')}")
                elif isinstance(c, (list, tuple)) and len(c) >= 2:
                    alt = c[2] if len(c) >= 3 else "?"
                    lines.append(f"  [{i}] lat={c[0]}, lng={c[1]}, alt={alt}")
            if len(coords) > 5:
                lines.append(f"  ... and {len(coords) - 5} more points")
            return "\n".join(lines)
        except Exception as e:
            return f"Error getting coordinates: {e}"

    @mcp.tool()
    async def komoot_get_tour_gpx(tour_id: int) -> str:
        """Return a tour's GPX content inline in the response.

        The GPX XML is embedded directly in the tool result (fenced code
        block) so the caller can read, save, or forward it without
        needing access to the server's filesystem. The full body is
        always returned; the byte-size is reported in the header line.

        Args:
            tour_id: The numeric tour ID
        """
        try:
            gpx = await get_client().get_tour_gpx(tour_id)
        except Exception as e:
            return f"Error downloading GPX: {e}"
        return _format_gpx_response(f"tour {tour_id}", gpx)

    @mcp.tool()
    async def komoot_get_tour_directions(tour_id: int) -> str:
        """Get turn-by-turn directions for a tour."""
        try:
            directions = await get_client().get_tour_directions(tour_id)
            if not directions:
                return "No directions found."
            lines = [f"Tour {tour_id} directions:"]
            for d in directions[:20]:
                if isinstance(d, dict):
                    lines.append(f"  {d.get('text', str(d))}")
                else:
                    lines.append(f"  {d}")
            if len(directions) > 20:
                lines.append(f"  ... and {len(directions) - 20} more steps")
            return "\n".join(lines)
        except Exception as e:
            return f"Error getting directions: {e}"

    @mcp.tool()
    async def komoot_get_tour_way_types(tour_id: int) -> str:
        """Get the way type breakdown for a tour (road, trail, path percentages)."""
        try:
            way_types = await get_client().get_tour_way_types(tour_id)
            if not way_types:
                return "No way type data found."
            if isinstance(way_types, list):
                lines = [f"Way types for tour {tour_id}:"]
                for w in way_types:
                    if isinstance(w, dict):
                        name = w.get("way_type", "?")
                        frac = w.get("fraction")
                        if isinstance(frac, (int, float)):
                            lines.append(f"  {name}: {frac * 100:.1f}%")
                        else:
                            lines.append(f"  {name}: {frac}")
                    else:
                        lines.append(f"  {w}")
                return "\n".join(lines)
            return f"Way types for tour {tour_id}: {way_types}"
        except Exception as e:
            return f"Error getting way types: {e}"

    @mcp.tool()
    async def komoot_get_tour_surfaces(tour_id: int) -> str:
        """Get the surface breakdown for a tour (paved, gravel, trail percentages)."""
        try:
            surfaces = await get_client().get_tour_surfaces(tour_id)
            if not surfaces:
                return "No surface data found."
            if isinstance(surfaces, list):
                return f"Surfaces for tour {tour_id}:\n" + "\n".join(f"  {s}" for s in surfaces)
            return f"Surfaces for tour {tour_id}: {surfaces}"
        except Exception as e:
            return f"Error getting surfaces: {e}"

    @mcp.tool()
    async def komoot_get_tour_timeline(tour_id: int) -> str:
        """Get the event timeline for a tour."""
        try:
            timeline = await get_client().get_tour_timeline(tour_id)
            if not timeline:
                return "No timeline events found."
            lines = [f"Tour {tour_id} timeline:"]
            for event in timeline[:20]:
                if isinstance(event, dict):
                    lines.append(f"  {event.get('type', 'event')}: {event.get('description', str(event))}")
                else:
                    lines.append(f"  {event}")
            if len(timeline) > 20:
                lines.append(f"  ... and {len(timeline) - 20} more events")
            return "\n".join(lines)
        except Exception as e:
            return f"Error getting timeline: {e}"

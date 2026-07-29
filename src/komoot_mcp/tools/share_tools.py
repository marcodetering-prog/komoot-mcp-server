"""Share-link tools for Komoot MCP server.

Komoot's tour share tokens let a tour owner expose a "secret link" to a
non-authenticated viewer. These tools create / revoke share tokens on a
tour you own, and resolve a share URL back to the underlying tour
metadata.
"""

from komoot_mcp.context import get_client


def register(mcp):
    @mcp.tool()
    async def komoot_create_share_link(tour_id: int) -> str:
        """Create a public share link for a tour.

        Returns the share token + a directly-clickable URL of the shape
        ``https://www.komoot.com/tour/{tour_id}?share_token={token}``.

        Example: ``komoot_create_share_link(tour_id=2614957086)``.

        Args:
            tour_id: The numeric tour ID (must be a tour you own)
        """
        try:
            data = await get_client().create_tour_share_link(tour_id)
        except Exception as e:
            return f"Error creating share link: {e}"

        if not isinstance(data, dict):
            return f"Created share link for tour {tour_id}: {data}"
        token = data.get("token") or data.get("share_token") or data.get("value")
        if not token:
            return f"Share link created for tour {tour_id} but no token field recognised. Raw keys: {list(data.keys())}"
        url = f"https://www.komoot.com/tour/{tour_id}?share_token={token}"
        return f"Share link created for tour {tour_id}:\n  Token: {token}\n  URL: {url}"

    @mcp.tool()
    async def komoot_revoke_share_link(tour_id: int) -> str:
        """Revoke the share token for a tour.

        After this, any previously-issued share URL stops working.

        Example: ``komoot_revoke_share_link(tour_id=2614957086)``.

        Args:
            tour_id: The numeric tour ID
        """
        try:
            await get_client().revoke_tour_share_link(tour_id)
            return f"Share link for tour {tour_id} revoked."
        except Exception as e:
            return f"Error revoking share link: {e}"

    @mcp.tool()
    async def komoot_resolve_share_url(share_url: str) -> str:
        """Resolve a Komoot share URL to tour metadata.

        Accepts URLs of the shape
        ``https://www.komoot.com/tour/{tour_id}?share_token={t}``.
        When ``share_token`` is present in the URL, the share token is
        used as the auth cap — no login needed.

        Example: ``komoot_resolve_share_url(share_url="https://www.komoot.com/tour/2614957086?share_token=a1b2c3")``.

        Args:
            share_url: The full share URL
        """
        try:
            data = await get_client().resolve_share_url(share_url)
        except Exception as e:
            return f"Error resolving share URL: {e}"

        tid = data.get("tour_id")
        share_token = data.get("share_token")
        tour = data.get("tour") or {}
        if not isinstance(tour, dict):
            return f"Resolved share URL but tour payload unexpected: {tour}"

        name = tour.get("name", "?")
        sport = tour.get("sport") or tour.get("sports") or "?"
        distance = tour.get("distance")
        elev_up = tour.get("elevation_up")
        status = tour.get("status", "?")
        date = tour.get("date", "?")

        lines = [
            f"Resolved share URL → tour {tid}:",
            f"  Name: {name}",
            f"  Sport: {sport} | Status: {status} | Date: {date}",
        ]
        if isinstance(distance, (int, float)):
            lines.append(f"  Distance: {distance} m")
        if isinstance(elev_up, (int, float)):
            lines.append(f"  Elevation up: {elev_up} m")
        if share_token:
            lines.append(f"  Share token: {share_token}")
        return "\n".join(lines)

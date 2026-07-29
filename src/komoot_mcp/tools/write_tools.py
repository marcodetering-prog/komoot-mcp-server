"""Write operation tools for Komoot MCP server."""

from komoot_mcp.context import get_client


def register(mcp):
    @mcp.tool()
    async def komoot_upload_tour(
        gpx_content: str = None,
        filepath: str = None,
        data_type: str = None,
        sport: str = "touringbicycle",
        tour_name: str = None,
    ) -> str:
        """Upload a tour to Komoot from GPX content or a file path.

        Prefer ``gpx_content`` — that's the only mode that works under
        the multi-tenant gateway, where the MCP server cannot read the
        caller's local filesystem. ``filepath`` is still supported for
        stdio / local-dev use, and is the only way to upload binary
        FIT/TCX files. Mirrors the inline-content shape introduced for
        ``komoot_get_tour_gpx`` and ``komoot_plan_route`` in PR #12
        (issue #9).

        If both are provided, ``gpx_content`` wins. If neither is
        provided you get a clear error.

        Args:
            gpx_content: GPX XML as a string. Preferred under the
                gateway — pass the body of a .gpx file directly.
            filepath: Path to a GPX/FIT/TCX file on the MCP server's
                filesystem. Only useful in stdio / local-dev mode.
            data_type: File format ('gpx', 'fit', 'tcx'). Auto-detected
                from extension when ``filepath`` is used; defaults to
                'gpx' when ``gpx_content`` is used.
            sport: Komoot activity type (e.g. 'touringbicycle', 'hike',
                'mountainbike', 'racebike', 'jogging'). Defaults to
                'touringbicycle'.
            tour_name: Optional display name for the uploaded tour.
                If omitted, the GPX track name (or the filename, for
                the filepath path) is used.
        """
        try:
            result = await get_client().upload_tour(
                filepath=filepath,
                data_type=data_type,
                sport=sport,
                gpx_content=gpx_content,
                tour_name=tour_name,
            )
        except Exception as e:
            return f"Error uploading tour: {e}"

        # Issue #17: don't render the raw kompy bool. The client now
        # raises on False, so any value reaching here is a success.
        # Render a tour ID and URL when the client returned them
        # (currently only via ``upload_gpx_capture_id`` /
        # ``komoot_plan_and_upload``); fall back to a plain success line
        # because kompy's bool-only return drops the new tour ID.
        if isinstance(result, dict) and result.get("id"):
            tid = result["id"]
            return f"Tour uploaded successfully.\n  Tour ID: {tid}\n  URL: https://www.komoot.com/tour/{tid}"
        return "Tour uploaded successfully."

    @mcp.tool()
    async def komoot_modify_tour(
        tour_id: int,
        name: str = None,
        sport: str = None,
        status: str = None,
    ) -> str:
        """Modify a Komoot tour's metadata.

        Args:
            tour_id: The numeric tour ID to modify
            name: New name for the tour
            sport: New sport type (e.g. 'hike', 'touringbicycle', 'mountainbike')
            status: New visibility ('public', 'private', 'friends')
        """
        try:
            result = await get_client().modify_tour(tour_id, name=name, sport=sport, status=status)
            return f"Tour {tour_id} updated: {result}"
        except Exception as e:
            return f"Error modifying tour: {e}"

    @mcp.tool()
    async def komoot_delete_tour(tour_id: int) -> str:
        """Delete a Komoot tour permanently.

        Args:
            tour_id: The numeric tour ID to delete
        """
        try:
            await get_client().delete_tour(tour_id)
            return f"Tour {tour_id} deleted."
        except Exception as e:
            return f"Error deleting tour: {e}"

    @mcp.tool()
    async def komoot_modify_tour_extended(
        tour_id: int,
        description: str = None,
        gear: str = None,
        date: str = None,
        status: str = None,
        name: str = None,
        sport: str = None,
    ) -> str:
        """Modify extended tour metadata (description, gear, date, etc.).

        Wider field coverage than ``komoot_modify_tour`` — uses Komoot's
        REST PATCH endpoint directly so we can update fields kompy
        doesn't expose. Only non-None fields are sent in the PATCH body
        so callers can update arbitrary subsets without clobbering
        existing values.

        Example: ``komoot_modify_tour_extended(tour_id=2614957086,
        description="Sunny Schwarzwald loop", gear="MTB",
        date="2026-05-18T08:00:00Z")``.

        Args:
            tour_id: The numeric tour ID
            description: Long-form description text
            gear: Gear / equipment used (string)
            date: ISO-8601 date string (e.g. "2026-05-18T08:00:00Z")
            status: Visibility ('public', 'private', 'friends')
            name: New tour name
            sport: Sport type (e.g. 'hike', 'touringbicycle')
        """
        try:
            await get_client().modify_tour_extended(
                tour_id,
                description=description,
                gear=gear,
                date=date,
                status=status,
                name=name,
                sport=sport,
            )
        except Exception as e:
            return f"Error modifying tour: {e}"
        updated = [
            label
            for label, val in [
                ("description", description),
                ("gear", gear),
                ("date", date),
                ("status", status),
                ("name", name),
                ("sport", sport),
            ]
            if val is not None
        ]
        return f"Tour {tour_id} updated. Fields set: {', '.join(updated) if updated else 'none'}"

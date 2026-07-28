"""Routing tools for Komoot MCP server."""

from komoot_mcp.context import get_client, get_geocoder, get_routing_manager
from komoot_mcp.routing import (
    NATIVE_SPORT_PROFILES,
    KomootNativePlanner,
    RoutingError,
)
from komoot_mcp.tools.data_tools import _format_gpx_response


# Map our public sport identifiers (used by komoot_plan_route) to the
# Komoot activity strings (used by komoot_upload_tour). The two
# vocabularies diverged historically — ``mountain_bike`` on the routing
# side, ``mtb`` on the Komoot side, etc. Keeping the map narrow on
# purpose: when a sport isn't mapped explicitly we pass through
# ``touringbicycle`` so the upload still succeeds with a sensible default.
_SPORT_TO_KOMOOT_ACTIVITY = {
    "hike": "hike",
    "trail_run": "jogging",
    "mountain_bike": "mtb",
    "road_cycle": "racebike",
    "gravel_ride": "mtb_easy",
}


def _komoot_activity_for(sport: str) -> str:
    return _SPORT_TO_KOMOOT_ACTIVITY.get(sport, "touringbicycle")


def _komoot_native_sport_for(sport: str) -> str:
    """Map public sport name to Komoot's native-planner sport name.

    Falls back to ``touringbicycle`` (the most permissive cycling
    profile) for unknown sports — the native planner rejects unknown
    sport tokens with HTTP 400.
    """
    return NATIVE_SPORT_PROFILES.get(sport, "touringbicycle")


def register(mcp):
    @mcp.tool()
    async def komoot_geocode(query: str, limit: int = 5) -> str:
        """Geocode a place name to coordinates using Komoot's Photon API (free, no key needed).

        Args:
            query: Place name, address, or coordinates (e.g. 'Berlin', 'Marienplatz Munich', '52.52,13.40')
            limit: Max number of results (default 5)
        """
        try:
            geocoder = get_geocoder()
            parts = query.split(",")
            if len(parts) == 2:
                try:
                    lat = float(parts[0].strip())
                    lon = float(parts[1].strip())
                    result = geocoder.reverse(lat, lon)
                    return (
                        f"Location: {result.get('display_name', 'unknown')}\n"
                        f"  City: {result.get('city', '?')}\n"
                        f"  Country: {result.get('country', '?')}\n"
                        f"  Coordinates: {result['lat']}, {result['lon']}\n"
                        f"  Type: {result.get('type', '?')}"
                    )
                except ValueError:
                    pass

            results = geocoder.forward(query, limit)
            if not results:
                return f"No locations found for '{query}'"
            lines = [f"Geocoding results for '{query}':"]
            for i, r in enumerate(results):
                lines.append(
                    f"  [{i}] {r['display_name']} ({r['city']}, {r['country']}) | "
                    f"{r['lat']}, {r['lon']} | type: {r['type']}"
                )
            return "\n".join(lines)
        except Exception as e:
            return f"Geocoding error: {e}"

    @mcp.tool()
    async def komoot_plan_route(
        start: str,
        end: str = None,
        roundtrip: bool = False,
        target_distance_km: float = None,
        sport: str = "hike",
        prefer_trails: bool = False,
        avoid_roads: bool = False,
        waypoints: str = None,
    ) -> str:
        """Plan a route using OpenRouteService with sport-specific profiles.

        Use this to create routes with preferences like "maximize trails, minimize roads".
        The planned route can then be uploaded to Komoot with komoot_upload_tour.

        Args:
            start: Starting point — place name or 'lat,lng'
            end: End point — place name or 'lat,lng' (omit for roundtrip)
            roundtrip: If True, creates a loop starting and ending at start
            target_distance_km: Target distance in kilometers (required for roundtrip)
            sport: One of 'hike', 'trail_run', 'mountain_bike', 'road_cycle', 'gravel_ride'
            prefer_trails: If True, maximize trails and paths, avoid highways
            avoid_roads: If True, minimize paved roads
            waypoints: Comma-separated coordinates e.g. "52.5,13.4|52.6,13.5"
        """
        routing = get_routing_manager()
        if routing is None:
            return (
                "Error: ORS API key not configured for this org. "
                "Add it in the dashboard under Komoot credentials "
                "(free signup at https://openrouteservice.org/dev/#/signup)."
            )

        try:
            geocoder = get_geocoder()
            start_coords = _parse_location(start, geocoder)
            if not start_coords:
                return f"Could not geocode start location: {start}"

            end_coords = None
            if end:
                end_coords = _parse_location(end, geocoder)
                if not end_coords:
                    return f"Could not geocode end location: {end}"

            waypoint_coords = None
            if waypoints:
                waypoint_coords = []
                for wp_str in waypoints.split("|"):
                    wp = _parse_coords(wp_str.strip())
                    if wp:
                        waypoint_coords.append(wp)

            result = routing.plan_route(
                start=start_coords,
                end=end_coords,
                roundtrip=roundtrip,
                target_distance_km=target_distance_km,
                sport=sport,
                prefer_trails=prefer_trails,
                avoid_roads=avoid_roads,
                waypoints=waypoint_coords,
            )

            # Issue #9: GPX content is returned inline so the caller can
            # use it directly. Server-side filesystem paths are useless
            # under the multi-tenant gateway.
            summary = (
                f"Route planned successfully!\n"
                f"  Distance: {result['distance_km']} km\n"
                f"  Elevation gain: {result['elevation_gain_m']} m\n"
                f"  Estimated duration: {result['duration_minutes']} min\n"
                f"  Sport profile: {sport}\n"
                f"  Waypoints: {len(result['waypoints'])} points\n\n"
            )
            gpx_block = _format_gpx_response(
                f"planned {sport} route", result["gpx"],
            )
            return summary + gpx_block
        except Exception as e:
            return f"Route planning failed: {e}"

    @mcp.tool()
    async def komoot_plan_and_upload(
        start: str,
        end: str = None,
        roundtrip: bool = False,
        target_distance_km: float = None,
        sport: str = "hike",
        prefer_trails: bool = False,
        avoid_roads: bool = False,
        waypoints: str = None,
        tour_name: str = None,
        tour_status: str = "private",
    ) -> str:
        """Plan a route with Komoot's own planner and save it as a
        Planned Tour in one server-side operation. Returns the Komoot
        tour ID and URL.

        This tool calls Komoot's native planner endpoint directly, so
        the saved tour gets ``type=tour_planned`` and shows up under
        Planned Routes in Komoot — *not* in the activity feed. (The
        previous GPX-upload path always created a ``tour_recorded``
        activity, even when we asked for ``tour_planned``; the native
        planner is the only way to get the record type right.)

        For raw ORS output without saving to Komoot, use the separate
        ``komoot_plan_route`` tool.

        Args:
            start: Starting point — place name or 'lat,lng'
            end: End point — place name or 'lat,lng' (omit for roundtrip)
            roundtrip: If True, returns to ``start``. Komoot's native
                planner has no built-in roundtrip generator, so the
                "loop" here is just an out-and-back over the supplied
                waypoints. For an arbitrary-shape loop with a target
                distance, use ``komoot_plan_route`` (ORS) — but the
                result lands on disk, not in your Komoot account.
            target_distance_km: Ignored. The native planner doesn't
                accept a target distance; the route length is whatever
                the waypoints produce. Kept for signature compatibility
                with ``komoot_plan_route``.
            sport: One of 'hike', 'trail_run', 'mountain_bike',
                'road_cycle', 'gravel_ride'. Maps to Komoot's native
                planner profile. Note: Komoot has no separate gravel
                profile, so ``gravel_ride`` becomes ``touringbicycle``
                (the all-rounder bike profile).
            prefer_trails: Ignored when using the native planner.
                Komoot's per-sport profile already biases toward trails
                for ``mountain_bike``/``hike`` and toward roads for
                ``road_cycle``. Kept for signature compatibility.
            avoid_roads: Ignored — see ``prefer_trails``.
            waypoints: Comma-separated coordinates e.g.
                "52.5,13.4|52.6,13.5" to be visited in order between
                ``start`` and ``end``.
            tour_name: Display name for the saved tour. Defaults to
                "Planned <sport> route".
            tour_status: Privacy: "private" (default), "public", or
                "friends".
        """
        # Geocode the inputs first — the native planner doesn't itself
        # geocode, it wants lat/lng directly.
        try:
            geocoder = get_geocoder()
            start_coords = _parse_location(start, geocoder)
            if not start_coords:
                return f"Could not geocode start location: {start}"

            end_coords = None
            if end:
                end_coords = _parse_location(end, geocoder)
                if not end_coords:
                    return f"Could not geocode end location: {end}"
            elif roundtrip:
                # Out-and-back over the supplied waypoints. The user
                # asked for a roundtrip — the planner needs at least 2
                # waypoints so we send start at both ends. Intermediate
                # vias are inserted between them.
                end_coords = start_coords
            else:
                return (
                    "komoot_plan_and_upload needs either an 'end' "
                    "location or roundtrip=true with at least one "
                    "waypoint to round-trip through."
                )

            via_coords = []
            if waypoints:
                for wp_str in waypoints.split("|"):
                    wp = _parse_coords(wp_str.strip())
                    if wp:
                        via_coords.append(wp)

            # Assemble waypoints in order: start → vias → end.
            all_waypoints = [start_coords, *via_coords, end_coords]
            if roundtrip and not via_coords and end_coords == start_coords:
                # A start→start roundtrip with no via points is a
                # degenerate plan (zero distance) — surface a clear
                # error rather than letting Komoot return a confusing
                # one.
                return (
                    "Roundtrip with no waypoints would be a zero-distance "
                    "route. Supply one or more waypoints to round-trip "
                    "through, e.g. waypoints='47.97,7.85'."
                )
        except Exception as e:
            return f"Geocoding failed: {e}"

        # --- Step 1: plan via Komoot's native planner ---
        client = get_client()
        try:
            auth_pair = client._basic_auth()
        except Exception as e:
            return f"Komoot authentication failed: {e}"

        sport_komoot = _komoot_native_sport_for(sport)
        planner = KomootNativePlanner(auth_pair=auth_pair, session=client._session)
        try:
            route = planner.plan(
                waypoints=all_waypoints, sport_komoot=sport_komoot,
            )
        except RoutingError as e:
            return f"Route planning failed: {e}"
        except Exception as e:
            return f"Route planning failed: {e}"

        # --- Step 2: save as tour_planned ---
        name = tour_name or f"Planned {sport} route"
        try:
            saved = await client.save_planned_tour(
                route_response=route, name=name, status=tour_status,
            )
        except Exception as e:
            distance_km = round((route.get("distance") or 0) / 1000, 2)
            elev_m = round(route.get("elevation_up") or 0, 1)
            return (
                f"Route planned ({distance_km} km, {elev_m} m climb) "
                f"but save to Komoot failed: {e}"
            )

        # --- Format response ---
        distance_km = round((route.get("distance") or 0) / 1000, 2)
        elev_m = round(route.get("elevation_up") or 0, 1)
        duration_min = round((route.get("duration") or 0) / 60, 1)
        tid = saved.get("id")
        status = saved.get("status", "saved")
        lines = [
            "Route planned and saved to Komoot as a Planned Tour.",
            f"  Distance: {distance_km} km",
            f"  Elevation gain: {elev_m} m",
            f"  Estimated duration: {duration_min} min",
            f"  Sport: {sport} (Komoot profile: {sport_komoot})",
            f"  Status: {status} ({tour_status})",
        ]
        if tid:
            lines.append(f"  Tour ID: {tid}")
            lines.append(f"  URL: https://www.komoot.com/tour/{tid}")
        else:
            lines.append(
                "  Tour ID: <not returned by Komoot> — check your tours list."
            )
        return "\n".join(lines)


def _parse_location(s: str, geocoder):
    coords = _parse_coords(s)
    if coords:
        return coords
    results = geocoder.forward(s, limit=1)
    if results:
        r = results[0]
        return (r["lat"], r["lon"])
    return None


def _parse_coords(s: str):
    parts = s.split(",")
    if len(parts) == 2:
        try:
            lat = float(parts[0].strip())
            lon = float(parts[1].strip())
            return (lat, lon)
        except ValueError:
            pass
    return None

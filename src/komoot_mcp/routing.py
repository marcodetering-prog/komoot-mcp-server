import json
import os

import gpxpy
import gpxpy.gpx
import openrouteservice
import requests


class RoutingError(Exception):
    pass


def _ors_rte_to_trk_gpx(ors_xml: str, name: str = "Planned route") -> str:
    """Convert an ORS GPX (which encodes routes as ``<rte>/<rtept>``) into
    a Komoot-compatible GPX (which expects ``<trk>/<trkseg>/<trkpt>``).

    Issue #18: Komoot's upload endpoint rejects ORS-formatted GPX with
    HTTP 400. ORS returns route points (``rtept``); Komoot wants track
    points (``trkpt``). Without this conversion every route planned via
    ``komoot_plan_route`` fails to upload.

    Behaviour:

    * Every ``<rte>`` in the ORS GPX becomes one ``<trk>`` with a single
      ``<trkseg>`` carrying the same points (lat, lon, ele preserved).
    * Existing ``<trk>`` elements in the input are preserved verbatim.
    * Top-level metadata (``name``, ``creator``) is set to indicate the
      conversion source so downstream debugging is unambiguous.
    """
    src = gpxpy.parse(ors_xml)
    out = gpxpy.gpx.GPX()
    out.creator = "komoot-mcp-server (via openrouteservice)"
    out.name = name

    # Preserve any pre-existing tracks (ORS rarely emits them but a future
    # change to ORS or a different upstream could).
    for trk in src.tracks:
        out.tracks.append(trk)

    # Convert each route to a track.
    for rte in src.routes:
        track = gpxpy.gpx.GPXTrack(name=rte.name or name)
        out.tracks.append(track)
        seg = gpxpy.gpx.GPXTrackSegment()
        track.segments.append(seg)
        for pt in rte.points:
            seg.points.append(gpxpy.gpx.GPXTrackPoint(
                latitude=pt.latitude,
                longitude=pt.longitude,
                elevation=pt.elevation,
            ))

    return out.to_xml()

SPORT_PROFILES = {
    "hike": "foot-hiking",
    "trail_run": "foot-walking",
    "mountain_bike": "cycling-mountain",
    "road_cycle": "cycling-road",
    "gravel_ride": "cycling-regular",
}


# Public sport identifiers → Komoot's native-planner sport vocabulary.
# The names diverge from both ORS profiles and from kompy's upload
# activity strings. Komoot's own planner accepts these on the
# ``/api/routing/tour`` endpoint:
#
#   mtb, mountainbike, touringbicycle, racebike, e_racebike, e_mtb,
#   jogging, hike, nordicwalking, e_touringbicycle
#
# Komoot does not have a separate "gravel" profile — its tour planner
# folds gravel routing into ``touringbicycle`` (the all-rounder bike
# profile). We document that in the tool docstring.
NATIVE_SPORT_PROFILES = {
    "hike": "hike",
    "trail_run": "jogging",
    "mountain_bike": "mtb",
    "road_cycle": "racebike",
    "gravel_ride": "touringbicycle",
}


class KomootNativePlanner:
    """Calls Komoot's own planner endpoint instead of OpenRouteService.

    Background: ``komoot_plan_and_upload`` historically went via ORS to
    build a GPX, then uploaded the GPX as a "planned tour" (issue #21).
    Komoot's GPX-upload endpoint always created a ``tour_recorded``
    record though — passing ``?type=tour_planned`` did not flip the
    record type. The right path is to use Komoot's native planner
    (``POST /api/routing/tour``) and then save the returned route blob
    to ``POST /api/v007/tours/?hl=en`` with ``type: tour_planned``
    inside the JSON body, which IS honored.

    Auth: Basic ``(user_id, long_lived_token)`` — the same Basic pair
    that already authenticates ``upload_gpx_capture_id`` and all the
    other v007 calls. Confirmed working with a live probe (status 200
    on the planning call, 201 on the save call).
    """

    BASE = "https://www.komoot.com/api/routing/tour"
    # Embed the full route detail: coordinates (for surface/way overlays),
    # way_types + surfaces (for the per-segment composition), directions
    # (for turn-by-turn). These are what Komoot's own web frontend
    # requests, so the saved tour ends up indistinguishable from one
    # planned in the browser.
    _EMBED = "coordinates,way_types,surfaces,directions"

    def __init__(self, auth_pair, session=None):
        """``auth_pair``: ``(user_id, token)`` tuple for HTTP Basic auth.

        The token is the long-lived password-equivalent returned by
        Komoot's v006 login (see :class:`AuthManager.login`), NOT the
        user's actual password. ``KomootClient._basic_auth`` builds
        this tuple from the kompy connector.

        ``session``: pass the calling ``KomootClient``'s Session so the plan
        POST and the follow-up save POST reuse one connection to
        www.komoot.com. Falls back to a private Session, never a shared one.
        """
        self._auth = auth_pair
        self._session = session if session is not None else requests.Session()

    def plan(self, waypoints, sport_komoot, constitution=3):
        """Plan a route through the supplied waypoints.

        Args:
            waypoints: List of ``(lat, lon)`` tuples — at least two
                (start + end). Intermediate points become routing
                via-points. Roundtrips: pass ``[start, start]`` plus
                whatever extra via-points you want.
            sport_komoot: One of the Komoot-vocabulary sport names
                (e.g. ``"mtb"``, ``"touringbicycle"``, ``"hike"``).
                Use :data:`NATIVE_SPORT_PROFILES` to map from our
                public vocab.
            constitution: Fitness self-rating 1-5; affects duration
                estimates but not the geometry. Default 3 (average).

        Returns the parsed route response dict — the same object you
        pass to :meth:`KomootClient.save_planned_tour` to commit it.
        Includes ``distance`` (m), ``duration`` (s), ``path``,
        ``segments``, ``elevation_up`` / ``elevation_down``, ``query``
        (the planner's own token), and an ``_embedded`` block with
        coordinates, way_types, surfaces, and directions.
        """
        if not waypoints or len(waypoints) < 2:
            raise RoutingError(
                "Komoot's native planner needs at least 2 waypoints "
                "(start + end). For a roundtrip pass the same point "
                "twice."
            )
        path = [
            {"location": {"lat": float(lat), "lng": float(lon)}}
            for lat, lon in waypoints
        ]
        body = {
            "constitution": int(constitution),
            "sport": sport_komoot,
            "path": path,
            # Komoot requires len(segments) == len(path) - 1 — one
            # "Routed" segment per leg between waypoints. We're not
            # importing manually-drawn geometry, so each segment's
            # ``geometry`` stays empty and Komoot fills it in via its
            # own routing engine.
            "segments": [
                {"geometry": [], "type": "Routed"} for _ in range(len(path) - 1)
            ],
        }
        params = {"sport": sport_komoot, "_embedded": self._EMBED}
        try:
            resp = self._session.post(
                self.BASE,
                params=params,
                auth=self._auth,
                json=body,
                headers={
                    "User-Agent": "komoot-mcp-server",
                    "Accept": "application/json",
                },
                timeout=60,
            )
        except requests.exceptions.Timeout as e:
            raise RoutingError(f"Komoot planner timed out: {e}")
        except requests.exceptions.RequestException as e:
            raise RoutingError(f"Komoot planner transport error: {e}")

        if resp.status_code != 200:
            snippet = (resp.text or "")[:400]
            raise RoutingError(
                f"Komoot planner request failed "
                f"(HTTP {resp.status_code}): {snippet}"
            )
        try:
            return resp.json()
        except ValueError as e:
            raise RoutingError(f"Komoot planner returned non-JSON: {e}")

# ORS only allows specific avoid_features values per profile family. Sending
# the wrong one (e.g. "highways" on cycling-mountain) causes a request-wide
# 400 with error 2003. Source: ORS docs — routing-options.md.
# https://github.com/giscience/openrouteservice/blob/main/docs/api-reference/endpoints/directions/routing-options.md
_AVOID_FEATURES_BY_FAMILY = {
    "driving": {"highways", "tollways", "ferries"},
    "cycling": {"steps", "ferries", "fords"},
    "foot": {"ferries", "fords", "steps"},
}

# Snap-radius default in ORS is 350m, which is too tight for geocoder hits
# that land off-network (e.g. atop a station's building footprint). Extend
# to 1km per waypoint to cover the common cases the user hit.
_DEFAULT_SNAP_RADIUS_M = 1000


def _profile_family(profile: str) -> str:
    """Map an ORS profile name to its avoid-features family."""
    if profile.startswith("driving"):
        return "driving"
    if profile.startswith("cycling"):
        return "cycling"
    if profile.startswith("foot"):
        return "foot"
    # Unknown family — default to the most restrictive (foot).
    return "foot"


def _filter_avoid_features(profile: str, requested: list[str]) -> list[str]:
    """Drop avoid-features values the given profile doesn't accept."""
    allowed = _AVOID_FEATURES_BY_FAMILY.get(_profile_family(profile), set())
    return [f for f in requested if f in allowed]


def _to_lon_lat(coord):
    """Convert an internal ``(lat, lon)`` coord to the ``[lon, lat]`` ORS
    expects. Accepts tuple/list of length 2+ (alt is ignored). The whole
    server stores geocoded points as ``(lat, lon)`` — historically the
    routing layer forwarded those unchanged to ORS, which produced
    400/error-2010 "Could not find routable point" because ORS searched
    for the network in the wrong hemisphere/region.
    """
    if coord is None:
        return None
    lat, lon = coord[0], coord[1]
    return [float(lon), float(lat)]


class RoutingManager:
    def __init__(self, api_key: str | None = None):
        # Per-request key wins; fall back to env for stdio/local-dev so
        # single-user setups keep working without the platform gateway.
        key = api_key or os.environ.get("ORS_API_KEY")
        if not key:
            raise RoutingError(
                "ORS API key not configured for this org. Add it in the "
                "dashboard under Komoot credentials (free signup at "
                "https://openrouteservice.org/dev/#/signup), or set "
                "ORS_API_KEY when running in stdio mode."
            )
        self._key = key
        self.client = openrouteservice.Client(key=key)

    def _fetch_gpx(self, *, profile, coordinates, options, radiuses):
        """POST to ORS ``directions/{profile}/gpx`` and return XML text.

        Issue #11: the ``openrouteservice`` Python client routes every
        response through ``_get_body``, which calls ``response.json()``.
        For ``format="gpx"`` the body is XML, so ``json.JSONDecodeError``
        fires and the client raises ``HTTPError(response.status_code)``
        — i.e. ``HTTP Error: 200`` on a perfectly successful request.
        Our previous ``except Exception`` block surfaced that as
        "Route planning failed: HTTP Error: 200" to the user.

        We sidestep the client and POST ourselves for the GPX format
        only; the GeoJSON call (which IS valid JSON) still goes through
        ``client.directions``.
        """
        body: dict = {"coordinates": coordinates}
        if options:
            body["options"] = options
        if radiuses:
            body["radiuses"] = radiuses
        body["elevation"] = True

        url = (
            f"{self.client._base_url}/v2/directions/{profile}/gpx"
        )
        headers = {
            "Authorization": self._key,
            "Content-Type": "application/json",
            "Accept": "application/gpx+xml, application/xml",
        }
        try:
            resp = requests.post(
                url,
                data=json.dumps(body),
                headers=headers,
                timeout=self.client._timeout,
            )
        except requests.exceptions.Timeout as e:
            raise RoutingError(f"OpenRouteService timed out fetching GPX: {e}")
        except requests.exceptions.RequestException as e:
            raise RoutingError(f"OpenRouteService transport error: {e}")

        if resp.status_code != 200:
            # Try to surface a useful body (ORS returns JSON for errors
            # even when GPX was requested).
            try:
                err = resp.json()
            except ValueError:
                err = resp.text[:500]
            raise RoutingError(
                f"OpenRouteService GPX request failed "
                f"(status {resp.status_code}): {err}"
            )
        return resp.text

    def _build_options(self, profile, prefer_trails, avoid_roads):
        # Both prefer_trails and avoid_roads previously appended
        # "highways", which ORS rejects for any cycling-* or foot-*
        # profile. Build a profile-aware avoid_features list instead.
        # For cycling we add "steps" (closest analog to "no rough
        # stairs") for prefer_trails; "fords" for avoid_roads can be
        # debated — leaving avoid_roads as a no-op on cycling/foot
        # rather than silently sending an unsupported feature.
        requested: list[str] = []
        family = _profile_family(profile)
        if prefer_trails:
            if family == "driving":
                # Drivers wanting "trails" really mean "avoid highways".
                requested.append("highways")
            elif family == "cycling":
                # Bikers wanting trails want to avoid steps & fords.
                requested.append("steps")
            # foot-* has no good "prefer trails" toggle in avoid_features.
        if avoid_roads:
            if family == "driving":
                requested.append("highways")
            # On cycling/foot, "avoid_roads" doesn't map to any ORS
            # avoid_features value — leave the request open rather than
            # ship an invalid one. Prefer_trails covers the steps case.
        avoid_features = _filter_avoid_features(profile, list(set(requested)))
        options = {}
        if avoid_features:
            options["avoid_features"] = avoid_features
        return options if options else None

    def plan_route(self, start, end=None, roundtrip=False, target_distance_km=None,
                   sport="hike", prefer_trails=False, avoid_roads=False, waypoints=None,
                   _raw_ors_gpx=False):
        profile = SPORT_PROFILES.get(sport)
        if not profile:
            raise RoutingError(f"Unknown sport: {sport}. Valid: {list(SPORT_PROFILES.keys())}")

        if roundtrip:
            if not target_distance_km:
                raise RoutingError("target_distance_km is required for roundtrip routing")
            coords = [_to_lon_lat(start)]
            options = self._build_options(profile, prefer_trails, avoid_roads) or {}
            options["round_trip"] = {
                "length": int(target_distance_km * 1000),
                "points": 3,
                "seed": 42
            }
        else:
            if not end:
                raise RoutingError("end point is required for point-to-point routing")
            coords = [_to_lon_lat(start)]
            if waypoints:
                coords.extend(_to_lon_lat(wp) for wp in waypoints)
            coords.append(_to_lon_lat(end))
            options = self._build_options(profile, prefer_trails, avoid_roads)

        # Extend ORS's snap radius per waypoint. Default 350m is too tight
        # for geocoder hits that land on building footprints; 1km covers
        # the named-place + lat/lng failures the user reported (error 2010
        # "Could not find routable point within a radius of 350.0 meters").
        radiuses = [_DEFAULT_SNAP_RADIUS_M] * len(coords)

        try:
            # GeoJSON for summary + parsed coords; JSON shape is what the
            # vendored ORS client expects, so this path is unchanged.
            result = self.client.directions(
                coordinates=coords,
                profile=profile,
                format="geojson",
                options=options,
                instructions=True,
                elevation=True,
                extra_info=["surface", "waytype"],
                radiuses=radiuses,
            )
        except openrouteservice.exceptions.ApiError as e:
            raise RoutingError(f"OpenRouteService error: {e}")
        except openrouteservice.exceptions.HTTPError as e:
            raise RoutingError(f"OpenRouteService transport error: {e}")
        except RoutingError:
            raise
        except Exception as e:
            raise RoutingError(f"Route planning failed: {e}")

        # GPX format is XML — the ORS client unconditionally calls
        # ``response.json()`` on it and raises ``HTTPError(200)``. Fetch
        # it ourselves over plain HTTP. See ``_fetch_gpx`` for the long
        # version of why.
        ors_gpx = self._fetch_gpx(
            profile=profile,
            coordinates=coords,
            options=options,
            radiuses=radiuses,
        )

        # Issue #18: ORS GPX uses <rte>/<rtept> which Komoot rejects on
        # upload with HTTP 400. Convert to <trk>/<trkseg>/<trkpt> by
        # default so the output can flow straight into komoot_upload_tour
        # or komoot_plan_and_upload. The ``_raw_ors_gpx`` escape hatch is
        # internal-only (no MCP tool exposes it) — useful for debugging
        # ORS responses or feeding tools that prefer route format.
        if _raw_ors_gpx:
            gpx_result = ors_gpx
        else:
            try:
                gpx_result = _ors_rte_to_trk_gpx(
                    ors_gpx, name=f"Planned {sport} route",
                )
            except Exception as e:
                # Don't fail the whole plan if conversion blows up —
                # surface the raw ORS body so the caller has *something*
                # to work with, plus a clear hint in the error.
                raise RoutingError(
                    f"ORS route planned but GPX format conversion failed: {e}"
                )

        feature = result["features"][0]
        props = feature["properties"]
        summary = props.get("summary", {})

        route_coords = feature["geometry"]["coordinates"]

        return {
            "gpx": gpx_result,
            "distance_km": round(summary.get("distance", 0) / 1000, 2),
            "elevation_gain_m": round(summary.get("ascent", 0), 1),
            "duration_minutes": round(summary.get("duration", 0) / 60, 1),
            "waypoints": [(c[1], c[0]) for c in route_coords],  # lon,lat → lat,lon
        }

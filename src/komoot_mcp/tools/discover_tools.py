"""Discovery tools for Komoot MCP server.

Phase 2 added ``komoot_recommend_tours_near`` (wraps the
``/v007/discover/{lat,lng}/elements/`` umbrella endpoint). Phase 3
extends with Smart Tour suggestions and attribute-filtered discovery.

All Phase 3 URLs in this module were live-probed 2026-05-18 against
``www.komoot.com/api/v007/`` (the previously-guessed
``smarttour-api.main.komoot.net`` and ``search-api.main.komoot.net``
hosts return 404 anonymously). The Komoot Smart Tour service is
exposed via the same ``discover_tours/from_location/`` endpoint with
slightly different param combinations:

* No filter → all smart tours near the point.
* ``highlight_id`` → tours that pass through that POI.
* Route attributes are listed by ``route_attribute_options`` (which
  itself needs the location + sport context, not a static catalog).

A standalone ``komoot_search`` tool and Trailview photo lookup were
also probed and dropped — Komoot exposes neither publicly.
"""

from komoot_mcp.context import get_client


def _items(data):
    """Pull a list of items out of a HAL response (best effort)."""
    if not isinstance(data, dict):
        return []
    emb = data.get("_embedded")
    if isinstance(emb, dict):
        items = emb.get("items")
        if isinstance(items, list):
            return items
        for v in emb.values():
            if isinstance(v, list):
                return v
    for key in ("items", "results", "content"):
        v = data.get(key)
        if isinstance(v, list):
            return v
    return []


def _render_tour_item(item):
    """Render one discover/smart-tour item as a single line."""
    if not isinstance(item, dict):
        return None
    name = item.get("name") or item.get("title") or "unnamed"
    tid = item.get("id") or item.get("tour_id") or "?"
    sport = item.get("sport") or item.get("sports") or "?"
    distance = item.get("distance")
    elev = item.get("elevation_up")
    bits = [f"[{tid}] {name} | sport={sport}"]
    if isinstance(distance, (int, float)):
        bits.append(f"{distance / 1000:.1f} km")
    if isinstance(elev, (int, float)):
        bits.append(f"+{int(elev)}m")
    return "  " + " | ".join(bits)


def register(mcp):
    @mcp.tool()
    async def komoot_recommend_tours_near(
        lat: float,
        lng: float,
        sport: str = None,
        radius_km: float = 20,
        limit: int = 10,
    ) -> str:
        """Recommend tours, smart tours, and collections near a point.

        Wraps Komoot's discovery surface
        (``/v007/discover/{lat,lng}/elements/``). Returns up to ``limit``
        items, each with name, type, sport, distance, and a share URL
        when one is exposed.

        Args:
            lat: Latitude of the center point
            lng: Longitude of the center point
            sport: Optional sport filter (e.g. 'hike', 'touringbicycle',
                'mountainbike', 'racebike'). When omitted, all sports
                are returned.
            radius_km: Search radius (forwarded as a hint; Komoot may
                clamp or ignore). Default 20 km.
            limit: Max items to return (default 10).
        """
        try:
            data = await get_client().discover_near(
                lat=lat,
                lng=lng,
                sport=sport,
                limit=limit,
            )
        except Exception as e:
            return f"Error discovering tours: {e}"

        items = []
        if isinstance(data, dict):
            emb = data.get("_embedded") or {}
            if isinstance(emb, dict):
                items = emb.get("items") or []
            if not items:
                items = data.get("items") or []

        if not items:
            return f"No discoverable tours near ({lat}, {lng}). Try a different point or remove the sport filter."

        header = f"Discovered {len(items)} items near ({lat}, {lng})"
        if sport:
            header += f" filtered by sport={sport}"
        lines = [header + ":"]

        for i, item in enumerate(items[:limit]):
            if not isinstance(item, dict):
                continue
            kind = item.get("type") or item.get("item_type") or "?"
            name = item.get("name") or item.get("title")
            sport_str = item.get("sport") or item.get("sports") or "?"
            distance = item.get("distance")

            sub_emb = item.get("_embedded") or {}
            if isinstance(sub_emb, dict):
                main = sub_emb.get("main_tour")
                if isinstance(main, dict):
                    name = name or main.get("name")
                    sport_str = sport_str if sport_str != "?" else main.get("sport") or "?"
                    distance = distance if distance is not None else main.get("distance")

            url = None
            links = item.get("_links") or {}
            if isinstance(links, dict):
                self_link = links.get("self")
                if isinstance(self_link, dict):
                    href = self_link.get("href")
                    if isinstance(href, str) and href.startswith("/"):
                        url = "https://www.komoot.com" + href
                    elif isinstance(href, str):
                        url = href
            if not url:
                tid = item.get("id")
                if isinstance(sub_emb, dict):
                    main = sub_emb.get("main_tour")
                    if isinstance(main, dict) and main.get("id"):
                        tid = main["id"]
                if tid:
                    url = f"https://www.komoot.com/tour/{tid}"

            name = name or "unnamed"
            bits = [f"[{i}] {kind} | {name} | sport={sport_str}"]
            if isinstance(distance, (int, float)):
                bits.append(f"{distance / 1000:.1f} km")
            line = "  " + " | ".join(bits)
            if url:
                line += f"\n      {url}"
            lines.append(line)

        return "\n".join(lines)

    @mcp.tool()
    async def komoot_smart_tours_near(
        lat: float,
        lng: float,
        sport: str,
        max_distance: int = 20000,
    ) -> str:
        """Recommend Smart Tours near a point.

        Hits ``www.komoot.com/api/v007/discover_tours/from_location/``
        (live-probed 2026-05-18). For a broader mix of items
        (collections + tours + smart tours) use
        ``komoot_recommend_tours_near`` instead — it wraps the umbrella
        ``/v007/discover/{lat,lng}/elements/`` endpoint. This tool
        returns Smart Tours only.

        Example: ``komoot_smart_tours_near(lat=47.9959, lng=7.8522,
        sport='mountainbike', max_distance=20000)`` for Freiburg.

        Args:
            lat: Latitude
            lng: Longitude
            sport: Sport profile (e.g. 'hike', 'touringbicycle',
                'mountainbike', 'racebike')
            max_distance: Search radius in metres (default 20000)
        """
        try:
            data = await get_client().smart_tours_near(
                lat,
                lng,
                sport,
                max_distance=max_distance,
            )
        except Exception as e:
            return f"Error getting smart tours: {e}"

        items = _items(data)
        if not items:
            return f"No smart tours near ({lat}, {lng}) for sport={sport}. Try a different point or wider max_distance."
        lines = [
            (f"Smart Tours near ({lat}, {lng}) for sport={sport} (max_distance {max_distance}m, {len(items)} found):")
        ]
        for it in items[:20]:
            line = _render_tour_item(it)
            if line:
                lines.append(line)
        return "\n".join(lines)

    @mcp.tool()
    async def komoot_smart_tour_for_highlight(
        highlight_id: int,
        lat: float,
        lng: float,
        sport: str = None,
    ) -> str:
        """Suggested smart tours that pass through a highlight (POI).

        Komoot's API exposes this via the same ``from_location``
        endpoint as ``komoot_smart_tours_near``, with a ``highlight_id``
        query param (live-probed 2026-05-18 — there is no dedicated
        ``for_highlight`` path).

        Example: ``komoot_smart_tour_for_highlight(highlight_id=15829,
        lat=47.9959, lng=7.8522, sport='hike')``.

        Args:
            highlight_id: The numeric highlight ID
            lat: Latitude near the highlight (required by the endpoint)
            lng: Longitude near the highlight (required by the endpoint)
            sport: Optional sport filter
        """
        try:
            data = await get_client().smart_tour_for_highlight(
                highlight_id,
                lat,
                lng,
                sport=sport,
            )
        except Exception as e:
            return f"Error getting smart tour for highlight: {e}"

        items = _items(data)
        if not items:
            return f"No suggested tours for highlight {highlight_id}."
        lines = [(f"Suggested tours for highlight {highlight_id} ({len(items)} found):")]
        for it in items[:20]:
            line = _render_tour_item(it)
            if line:
                lines.append(line)
        return "\n".join(lines)

    @mcp.tool()
    async def komoot_discover_with_attributes(
        lat: float,
        lng: float,
        sport: str = None,
        attributes: str = None,
    ) -> str:
        """Discover tours near a point, filtered by route attributes.

        Route attributes are tags like ``waterfalls``, ``lakes_rivers``,
        ``mountain_summits``. Use ``komoot_route_attribute_options`` to
        enumerate the legal values for a given location and sport.

        Example: ``komoot_discover_with_attributes(lat=47.9959,
        lng=7.8522, sport='mountainbike', attributes='waterfalls')``.

        Args:
            lat: Latitude
            lng: Longitude
            sport: Optional sport filter
            attributes: Comma-separated attribute names (e.g.
                ``waterfalls,lakes_rivers``)
        """
        try:
            data = await get_client().discover_with_attributes(
                lat,
                lng,
                sport=sport,
                attributes=attributes,
            )
        except Exception as e:
            return f"Error discovering tours by attributes: {e}"

        items = _items(data)
        if not items:
            return f"No tours match attributes={attributes!r} near ({lat}, {lng})."
        header = f"Discovered {len(items)} tours near ({lat}, {lng})"
        if attributes:
            header += f" with attributes={attributes}"
        if sport:
            header += f" sport={sport}"
        lines = [header + ":"]
        for it in items[:20]:
            line = _render_tour_item(it)
            if line:
                lines.append(line)
        return "\n".join(lines)

    @mcp.tool()
    async def komoot_route_attribute_options(
        lat: float,
        lng: float,
        sport: str,
        max_distance: int = 20000,
    ) -> str:
        """List the legal route-attribute names accepted by discovery.

        Komoot's API requires ALL four parameters (lat, lng, sport,
        max_distance) — anything missing returns HTTP 400 (live-probed
        2026-05-18). Pass these through to
        ``komoot_discover_with_attributes`` once you've picked an
        attribute.

        Example: ``komoot_route_attribute_options(lat=47.9959,
        lng=7.8522, sport='mountainbike', max_distance=20000)``.

        Args:
            lat: Latitude near the area you're searching
            lng: Longitude near the area you're searching
            sport: Sport profile (e.g. 'mountainbike', 'hike')
            max_distance: Search radius in metres (default 20000)
        """
        try:
            data = await get_client().route_attribute_options(
                lat,
                lng,
                sport,
                max_distance=max_distance,
            )
        except Exception as e:
            return f"Error getting route attributes: {e}"

        attrs = None
        if isinstance(data, dict):
            v = data.get("route_attributes")
            if isinstance(v, list):
                attrs = v
        if attrs is None:
            attrs = _items(data)
            if not attrs and isinstance(data, dict):
                attrs = list(data.keys())

        if not attrs:
            return f"No route attributes returned. Raw shape: {type(data).__name__}"
        lines = [f"Route attributes ({len(attrs)} options):"]
        for it in attrs:
            if isinstance(it, dict):
                name = it.get("name") or it.get("key") or it.get("id") or "?"
                label = it.get("label") or ""
                lines.append(f"  - {name}: {label}".rstrip(": "))
            else:
                lines.append(f"  - {it}")
        return "\n".join(lines)

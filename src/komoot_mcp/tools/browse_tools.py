"""Browse and search tools for Komoot MCP server."""

from komoot_mcp.context import get_client


def register(mcp):
    @mcp.tool()
    async def komoot_list_tours(
        page: int = 0,
        limit: int = 50,
        sport_type: str = None,
        status: str = None,
        name: str = None,
        sort_field: str = "date",
        sort_direction: str = "desc",
    ) -> str:
        """List your Komoot tours with filters.

        Args:
            page: Page number (0-indexed)
            limit: Results per page (max 50)
            sport_type: Filter by sport (e.g. 'hike', 'touringbicycle', 'mountainbike', 'racebike', 'run')
            status: Filter by visibility ('public', 'private', 'friends')
            name: Search by tour name (case-insensitive substring)
            sort_field: Sort by ('date', 'name', 'elevation', 'duration')
            sort_direction: Sort order ('asc' or 'desc')
        """
        try:
            result = await get_client().list_tours(
                page=page,
                limit=limit,
                sport_type=sport_type,
                status=status,
                name=name,
                sort_field=sort_field,
                sort_direction=sort_direction,
            )
            tours = result.get("tours", [])
            if not tours:
                return "No tours found."
            lines = [f"Tours (page {page}, {len(tours)} results):"]
            for t in tours:
                dist = t.get("distance", "?")
                elev = t.get("elevation_up", "?")
                sport = t.get("sport", "?")
                status_str = t.get("status", "?")
                lines.append(f"  [{t['id']}] {t.get('name', 'unnamed')} | {sport} | {status_str} | {dist}m | +{elev}m")
            return "\n".join(lines)
        except Exception as e:
            return f"Error listing tours: {e}"

    @mcp.tool()
    async def komoot_get_tour(tour_id: int) -> str:
        """Get full details of a specific Komoot tour by ID.

        Args:
            tour_id: The numeric tour ID
        """
        try:
            tour = await get_client().get_tour(tour_id)
            lines = [f"Tour: {tour.get('name', 'unnamed')}"]
            for key in [
                "id",
                "sport",
                "status",
                "distance",
                "elevation_up",
                "elevation_down",
                "duration",
                "date",
                "difficulty_grade",
                "difficulty_fitness",
                "difficulty_technical",
            ]:
                val = tour.get(key)
                if val is not None:
                    lines.append(f"  {key}: {val}")
            return "\n".join(lines)
        except Exception as e:
            return f"Error getting tour: {e}"

    @mcp.tool()
    async def komoot_get_tour_full(tour_id: int) -> str:
        """Hydrate a tour in ONE call (replaces 5+ chained calls).

        Returns a concise summary covering: name, sport, distance,
        duration, elevation, start point, difficulty, surface and
        way-type breakdowns, direction-step count, and any highlights
        on the timeline (id + name). Verbose payloads (full coordinate
        arrays, every direction step) are NOT inlined — the response
        notes their availability and points at the dedicated tools
        (``komoot_get_tour_coordinates``, ``komoot_get_tour_directions``)
        if the full detail is needed.

        Args:
            tour_id: The numeric tour ID
        """
        try:
            data = await get_client().get_tour_full(tour_id)
        except Exception as e:
            return f"Error getting tour: {e}"

        embedded = data.get("_embedded") or {}
        # ---- top-level scalars ----
        name = data.get("name", "unnamed")
        sport = data.get("sport", "?")
        distance = data.get("distance", "?")
        duration = data.get("duration", "?")
        elev_up = data.get("elevation_up", "?")
        elev_down = data.get("elevation_down", "?")
        status = data.get("status", "?")
        date = data.get("date", "?")
        diff = data.get("difficulty") or {}
        diff_grade = diff.get("grade", "?")

        start_point = data.get("start_point") or {}
        sp_lat = start_point.get("lat")
        sp_lng = start_point.get("lng")
        sp_alt = start_point.get("alt")

        lines = [
            f"Tour: {name} (id={data.get('id', tour_id)})",
            f"  Sport: {sport} | Status: {status} | Date: {date}",
            f"  Distance: {distance} m | Duration: {duration} s",
            f"  Elevation: +{elev_up} m / -{elev_down} m",
            f"  Difficulty grade: {diff_grade}",
        ]
        if sp_lat is not None and sp_lng is not None:
            lines.append(f"  Start: lat={sp_lat}, lng={sp_lng}, alt={sp_alt}")

        # ---- embedded coordinates ----
        coords = embedded.get("coordinates") or {}
        coord_items = coords.get("items") if isinstance(coords, dict) else None
        if isinstance(coord_items, list):
            lines.append(
                f"  Coordinates: {len(coord_items)} points available (use komoot_get_tour_coordinates for full array)"
            )

        # ---- embedded way_types ----
        way_types = embedded.get("way_types") or {}
        way_items = way_types.get("items") if isinstance(way_types, dict) else None
        if isinstance(way_items, list) and way_items:
            top = sorted(
                way_items,
                key=lambda w: w.get("amount", 0) if isinstance(w, dict) else 0,
                reverse=True,
            )[:5]
            parts = []
            for w in top:
                if not isinstance(w, dict):
                    continue
                wt = w.get("type") or w.get("way_type") or "?"
                amt = w.get("amount")
                if isinstance(amt, (int, float)):
                    parts.append(f"{wt} {amt * 100:.0f}%")
                else:
                    parts.append(f"{wt}")
            if parts:
                lines.append(f"  Way types: {', '.join(parts)}")

        # ---- embedded surfaces ----
        surfaces = embedded.get("surfaces") or {}
        surf_items = surfaces.get("items") if isinstance(surfaces, dict) else None
        if isinstance(surf_items, list) and surf_items:
            top = sorted(
                surf_items,
                key=lambda s: s.get("amount", 0) if isinstance(s, dict) else 0,
                reverse=True,
            )[:5]
            parts = []
            for s in top:
                if not isinstance(s, dict):
                    continue
                st = s.get("type") or s.get("surface_type") or "?"
                amt = s.get("amount")
                if isinstance(amt, (int, float)):
                    parts.append(f"{st} {amt * 100:.0f}%")
                else:
                    parts.append(f"{st}")
            if parts:
                lines.append(f"  Surfaces: {', '.join(parts)}")

        # ---- embedded directions ----
        directions = embedded.get("directions") or {}
        dir_items = directions.get("items") if isinstance(directions, dict) else None
        if isinstance(dir_items, list):
            lines.append(f"  Directions: {len(dir_items)} steps (use komoot_get_tour_directions for full list)")

        # ---- embedded timeline (highlights) ----
        timeline = embedded.get("timeline") or {}
        tl_items = timeline.get("items") if isinstance(timeline, dict) else None
        if isinstance(tl_items, list) and tl_items:
            lines.append(f"  Timeline: {len(tl_items)} entries")
            # Show up to 5 highlights with their IDs so the caller can
            # drill into them via ``komoot_get_highlight``.
            shown = 0
            for entry in tl_items:
                if not isinstance(entry, dict) or shown >= 5:
                    continue
                # Highlights are nested at ``entry._embedded.reference``
                # in Komoot's HAL shape; fall back to scanning common keys.
                hl = None
                ent_emb = entry.get("_embedded") or {}
                ref = ent_emb.get("reference") if isinstance(ent_emb, dict) else None
                if isinstance(ref, dict):
                    hl = ref
                elif isinstance(entry.get("reference"), dict):
                    hl = entry["reference"]
                if hl is None:
                    continue
                hid = hl.get("id")
                hname = hl.get("name", "?")
                if hid is not None:
                    lines.append(f"    highlight {hid}: {hname}")
                    shown += 1
            if shown < len(tl_items):
                lines.append("    (use komoot_get_highlight on any id for details)")

        # ---- embedded cover images ----
        cover = embedded.get("cover_images") or {}
        cover_items = cover.get("items") if isinstance(cover, dict) else None
        if isinstance(cover_items, list) and cover_items:
            lines.append(f"  Cover images: {len(cover_items)}")

        return "\n".join(lines)

    @mcp.tool()
    async def komoot_get_highlight(
        highlight_id: int,
        include_tips: bool = False,
        include_recommenders: bool = False,
    ) -> str:
        """Resolve a Komoot highlight (POI) by ID.

        Tour timelines reference highlight IDs. Without this resolver
        those IDs are dead-ends. Returns metadata (name, category,
        sport, score, location); optionally also community tips and a
        recommender count.

        Args:
            highlight_id: The numeric highlight ID
            include_tips: Fetch the community tips list (extra HTTP call)
            include_recommenders: Fetch the recommenders list (extra HTTP call)
        """
        try:
            data = await get_client().get_highlight(
                highlight_id,
                with_tips=include_tips,
                with_recommenders=include_recommenders,
            )
        except Exception as e:
            return f"Error getting highlight: {e}"

        meta = data.get("metadata") or {}
        if not isinstance(meta, dict):
            return f"Highlight {highlight_id}: unexpected response shape"

        hid = meta.get("id", highlight_id)
        name = meta.get("name", "?")
        sport = meta.get("sports") or meta.get("sport") or "?"
        category = meta.get("category") or meta.get("type") or "?"
        score = meta.get("score") or meta.get("rating")
        loc = meta.get("location") or meta.get("mid_point") or {}
        lat = loc.get("lat") if isinstance(loc, dict) else None
        lng = loc.get("lng") if isinstance(loc, dict) else None

        lines = [
            f"Highlight {hid}: {name}",
            f"  Category: {category}",
            f"  Sport: {sport}",
        ]
        if score is not None:
            lines.append(f"  Score: {score}")
        if lat is not None and lng is not None:
            lines.append(f"  Location: lat={lat}, lng={lng}")

        if include_tips:
            tips_block = data.get("tips") or {}
            tips_err = data.get("tips_error")
            if tips_err:
                lines.append(f"  Tips: error — {tips_err}")
            else:
                tip_items = []
                if isinstance(tips_block, dict):
                    emb = tips_block.get("_embedded") or {}
                    if isinstance(emb, dict):
                        tip_items = emb.get("items") or []
                    if not tip_items:
                        tip_items = tips_block.get("items") or []
                lines.append(f"  Tips: {len(tip_items)} community tips")
                for t in tip_items[:3]:
                    if isinstance(t, dict):
                        text = (t.get("text") or "").strip().replace("\n", " ")
                        if len(text) > 160:
                            text = text[:157] + "..."
                        if text:
                            lines.append(f"    - {text}")

        if include_recommenders:
            rec_block = data.get("recommenders") or {}
            rec_err = data.get("recommenders_error")
            if rec_err:
                lines.append(f"  Recommenders: error — {rec_err}")
            else:
                rec_items = []
                if isinstance(rec_block, dict):
                    emb = rec_block.get("_embedded") or {}
                    if isinstance(emb, dict):
                        rec_items = emb.get("items") or []
                    if not rec_items:
                        rec_items = rec_block.get("items") or []
                lines.append(f"  Recommenders: {len(rec_items)} users")

        return "\n".join(lines)

    @mcp.tool()
    async def komoot_tour_weather(tour_id: int) -> str:
        """Weather forecast along a planned tour (EXPERIMENTAL).

        Hits Komoot's dedicated weather-along-tour service at
        ``weather-along-tour-api.komoot.de/v1/weather?tour_id={id}``.

        The exact request signature wasn't live-probed for this
        multi-tenant deployment, so this tool ships as best-effort: if
        Komoot rejects the request shape, the error is surfaced
        verbatim so the caller can iterate. The most likely fix is
        passing the tour's start coordinates inline instead of an ID —
        a future revision may switch to that signature.

        Args:
            tour_id: The numeric tour ID (must be a tour you can read)
        """
        try:
            data = await get_client().get_tour_weather(tour_id)
        except Exception as e:
            return (
                f"Error getting weather for tour {tour_id}: {e}\n"
                "(This endpoint's exact request shape is best-guess — "
                "may need runtime verification.)"
            )

        if not isinstance(data, dict):
            return f"Weather for tour {tour_id}: unexpected response shape"

        # Komoot's weather service has not been formally documented for
        # us — render whatever forecast-like collection we find. Common
        # shapes seen in the wild: top-level ``forecast``, ``items``,
        # or a HAL ``_embedded.items``.
        forecast = data.get("forecast") or data.get("items") or (data.get("_embedded") or {}).get("items")
        lines = [f"Weather forecast for tour {tour_id}:"]
        if isinstance(forecast, list) and forecast:
            for entry in forecast[:8]:
                if not isinstance(entry, dict):
                    continue
                t = entry.get("time") or entry.get("timestamp") or "?"
                temp = entry.get("temperature") or entry.get("temp_c")
                cond = entry.get("condition") or entry.get("summary") or ""
                rain = entry.get("precipitation_mm") or entry.get("rain_mm")
                bits = [str(t)]
                if temp is not None:
                    bits.append(f"{temp}°C")
                if rain is not None:
                    bits.append(f"rain {rain}mm")
                if cond:
                    bits.append(str(cond))
                lines.append("  " + " | ".join(bits))
            if len(forecast) > 8:
                lines.append(f"  ... and {len(forecast) - 8} more entries")
        else:
            # No recognisable forecast collection — surface the raw keys
            # so the caller (or a future maintainer) can adjust the
            # parser to whatever shape Komoot actually returns.
            keys = list(data.keys()) if isinstance(data, dict) else []
            lines.append(f"  (raw response keys: {keys})")
        return "\n".join(lines)

    @mcp.tool()
    async def komoot_get_user_profile() -> str:
        """Get your Komoot user profile information."""
        try:
            profile = await get_client().get_user_profile()
            if isinstance(profile, dict):
                # ``display_name`` already falls back from username -> email
                # -> user_id in the client, so this never renders the
                # literal "unknown" placeholder kompy sometimes hands back.
                display = profile.get("display_name") or profile.get("email") or "?"
                email = profile.get("email") or "?"
                user_id = profile.get("user_id") or profile.get("username") or "?"
                return f"Profile: {display} ({email}) | User ID: {user_id}"
            return str(profile)
        except Exception as e:
            return f"Error getting profile: {e}"

    @mcp.tool()
    async def komoot_get_tour_photos(
        tour_id: int,
        page: int = 0,
        limit: int = 5,
    ) -> str:
        """Get the cover/photo images attached to a tour.

        Returns a bullet list of image URLs (resolved to width=800 from
        Komoot's templated URL placeholders).

        Example: ``komoot_get_tour_photos(tour_id=2614957086, page=0,
        limit=5)``.

        Args:
            tour_id: The numeric tour ID
            page: Page number (0-indexed)
            limit: Results per page
        """
        try:
            data = await get_client().get_tour_photos(
                tour_id,
                page=page,
                limit=limit,
            )
        except Exception as e:
            return f"Error getting tour photos: {e}"

        items = _hal_items(data)
        if not items:
            return f"No photos found for tour {tour_id}."

        lines = [f"Tour {tour_id} photos ({len(items)} on page {page}):"]
        for it in items:
            if not isinstance(it, dict):
                continue
            img_id = it.get("id", "?")
            src = it.get("src") or ""
            # Komoot returns templated URLs like
            # ``https://...?width={width}&height={height}&crop={crop}``.
            # Render at 800 wide so callers get a usable preview URL.
            resolved = src.replace("{width}", "800").replace("{height}", "600").replace("{crop}", "true")
            rating = it.get("rating")
            line = f"  [{img_id}] {resolved}"
            if rating is not None:
                line += f" (rating={rating})"
            lines.append(line)
        return "\n".join(lines)

    @mcp.tool()
    async def komoot_get_tour_line(tour_id: int) -> str:
        """Get a tour's simplified line geometry (lightweight alt to coordinates).

        Returns the coordinate count and a 5-point sample of waypoints.
        Use ``komoot_get_tour_coordinates`` for the full coordinate
        array.

        Example: ``komoot_get_tour_line(tour_id=2614957086)``.

        Args:
            tour_id: The numeric tour ID
        """
        try:
            data = await get_client().get_tour_line(tour_id)
        except Exception as e:
            return f"Error getting tour line: {e}"

        coords = []
        if isinstance(data, dict):
            # Live response uses ``geometry``; older shape used
            # ``coordinates``/``items``/``points`` — accept all.
            for key in ("geometry", "coordinates", "items", "points"):
                v = data.get(key)
                if isinstance(v, list):
                    coords = v
                    break

        if not coords:
            return (
                f"Tour {tour_id} line: no coordinate-shaped payload found "
                f"(raw response keys: {list(data.keys()) if isinstance(data, dict) else type(data).__name__})"
            )

        lines = [f"Tour {tour_id} line: {len(coords)} points"]
        for i, c in enumerate(coords[:5]):
            if isinstance(c, dict):
                lines.append(f"  [{i}] lat={c.get('lat')}, lng={c.get('lng')}, alt={c.get('alt', '?')}")
            elif isinstance(c, (list, tuple)) and len(c) >= 2:
                alt = c[2] if len(c) >= 3 else "?"
                lines.append(f"  [{i}] lat={c[0]}, lng={c[1]}, alt={alt}")
        if len(coords) > 5:
            lines.append(f"  ... and {len(coords) - 5} more points")
        return "\n".join(lines)

    @mcp.tool()
    async def komoot_list_user_highlights(
        user_id: str,
        page: int = 0,
        limit: int = 20,
    ) -> str:
        """List a user's saved highlights (POIs).

        Example: ``komoot_list_user_highlights(user_id="2069076024",
        page=0, limit=20)``.

        Args:
            user_id: The Komoot user_id (numeric, as string)
            page: Page number (0-indexed)
            limit: Max items per page
        """
        try:
            data = await get_client().list_user_highlights(
                user_id,
                page=page,
                limit=limit,
            )
        except Exception as e:
            return f"Error listing user highlights: {e}"

        items = _hal_items(data)
        if not items:
            return f"No highlights found for user {user_id}."
        lines = [f"Highlights for user {user_id} (page {page}, {len(items)} items):"]
        for h in items:
            if not isinstance(h, dict):
                continue
            hid = h.get("id", "?")
            name = h.get("name") or "?"
            sport = h.get("sports") or h.get("sport") or "?"
            cat = h.get("category") or h.get("type") or "?"
            lines.append(f"  [{hid}] {name} | sport={sport} | {cat}")
        return "\n".join(lines)


def _hal_items(data):
    """Extract a HAL-style items list from a Komoot response.

    Komoot uses HAL+JSON for paginated collections — items live under
    ``_embedded.items``. Fall back to a top-level ``items`` or
    ``content`` for non-HAL responses.
    """
    if not isinstance(data, dict):
        return []
    emb = data.get("_embedded")
    if isinstance(emb, dict):
        items = emb.get("items")
        if isinstance(items, list):
            return items
        # Sometimes the embed key matches the resource name (e.g.
        # ``cover_images`` on tours, ``highlights`` on users).
        for v in emb.values():
            if isinstance(v, list):
                return v
    for key in ("items", "content"):
        v = data.get(key)
        if isinstance(v, list):
            return v
    return []

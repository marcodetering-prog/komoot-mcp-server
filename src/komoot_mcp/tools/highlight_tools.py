"""Highlight (POI) tools for Komoot MCP server.

Phase 3: lightweight standalone tools for highlight sub-resources.
Phase 2's ``komoot_get_highlight`` (in ``browse_tools``) hits the
umbrella endpoint and optionally embeds tips/recommenders. The tools
below are intentionally narrower — useful when the caller already has
a highlight id and wants just the images, or just the tips, without
the metadata round-trip.
"""

from komoot_mcp.context import get_client


def _items(data):
    """Pull a list of items out of a HAL response (best effort)."""
    if not isinstance(data, dict):
        return []
    emb = data.get("_embedded")
    if isinstance(emb, dict):
        for v in emb.values():
            if isinstance(v, list):
                return v
    for key in ("items", "content"):
        v = data.get(key)
        if isinstance(v, list):
            return v
    return []


def register(mcp):
    @mcp.tool()
    async def komoot_get_highlight_images(
        highlight_id: int,
        page: int = 0,
    ) -> str:
        """Get photos attached to a Komoot highlight (POI).

        Example: ``komoot_get_highlight_images(highlight_id=15829,
        page=0)``.

        Args:
            highlight_id: The numeric highlight ID
            page: Page number (0-indexed)
        """
        try:
            data = await get_client().get_highlight_images(
                highlight_id,
                page=page,
            )
        except Exception as e:
            return f"Error getting highlight images: {e}"

        items = _items(data)
        if not items:
            return f"No images found for highlight {highlight_id}."

        lines = [f"Highlight {highlight_id} images (page {page}, {len(items)}):"]
        for it in items:
            if not isinstance(it, dict):
                continue
            img_id = it.get("id", "?")
            src = it.get("src") or ""
            resolved = src.replace("{width}", "800").replace("{height}", "600").replace("{crop}", "true")
            attr = it.get("attribution") or it.get("creator") or ""
            line = f"  [{img_id}] {resolved}"
            if attr:
                line += f" (by {attr})"
            lines.append(line)
        return "\n".join(lines)

    @mcp.tool()
    async def komoot_get_highlight_tips(
        highlight_id: int,
        page: int = 0,
    ) -> str:
        """Get community tips for a Komoot highlight.

        Lighter alternative to ``komoot_get_highlight(include_tips=True)``
        — only the tips, no metadata round-trip.

        Example: ``komoot_get_highlight_tips(highlight_id=15829,
        page=0)``.

        Args:
            highlight_id: The numeric highlight ID
            page: Page number (0-indexed)
        """
        try:
            data = await get_client().get_highlight_tips(
                highlight_id,
                page=page,
            )
        except Exception as e:
            return f"Error getting highlight tips: {e}"

        items = _items(data)
        if not items:
            return f"No tips found for highlight {highlight_id}."

        lines = [f"Highlight {highlight_id} tips (page {page}, {len(items)}):"]
        for t in items[:20]:
            if not isinstance(t, dict):
                continue
            tid = t.get("id", "?")
            text = (t.get("text") or "").strip().replace("\n", " ")
            if len(text) > 200:
                text = text[:197] + "..."
            creator = t.get("creator") or {}
            author = (
                (creator.get("display_name") or creator.get("username") or "?") if isinstance(creator, dict) else "?"
            )
            lines.append(f"  [{tid}] {author}: {text}")
        if len(items) > 20:
            lines.append(f"  ... and {len(items) - 20} more tips")
        return "\n".join(lines)

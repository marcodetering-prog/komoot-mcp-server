"""Collection tools for Komoot MCP server.

Komoot collections are curated bundles of tours (think "Best gravel
rides in Bavaria"). These tools expose the collection metadata, list a
user's saved collections, and enumerate the tours inside a collection.
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
    for key in ("items", "content"):
        v = data.get(key)
        if isinstance(v, list):
            return v
    return []


def register(mcp):
    @mcp.tool()
    async def komoot_get_collection(collection_id: int) -> str:
        """Get a Komoot collection's metadata.

        Example: ``komoot_get_collection(collection_id=947498)``.

        Args:
            collection_id: The numeric collection ID
        """
        try:
            data = await get_client().get_collection(collection_id)
        except Exception as e:
            return f"Error getting collection: {e}"

        if not isinstance(data, dict):
            return f"Collection {collection_id}: unexpected response shape"
        cid = data.get("id", collection_id)
        name = data.get("name") or data.get("title") or "?"
        sport = data.get("sport") or data.get("sports") or "?"
        n_tours = data.get("number_of_tours") or data.get("tour_count")
        creator = data.get("creator") or {}
        creator_name = (
            (creator.get("display_name") or creator.get("username") or "?") if isinstance(creator, dict) else "?"
        )
        desc = data.get("description") or data.get("intro") or ""
        if len(desc) > 300:
            desc = desc[:297] + "..."
        lines = [
            f"Collection {cid}: {name}",
            f"  Sport: {sport}",
            f"  Creator: {creator_name}",
        ]
        if n_tours is not None:
            lines.append(f"  Tours: {n_tours}")
        if desc:
            lines.append(f"  Description: {desc}")
        return "\n".join(lines)

    @mcp.tool()
    async def komoot_get_collection_tours(
        collection_id: int,
        page: int = 0,
    ) -> str:
        """List the tours inside a collection (compilation).

        Example: ``komoot_get_collection_tours(collection_id=947498,
        page=0)``.

        Args:
            collection_id: The numeric collection ID
            page: Page number (0-indexed)
        """
        try:
            data = await get_client().get_collection_tours(
                collection_id,
                page=page,
            )
        except Exception as e:
            return f"Error getting collection tours: {e}"

        items = _items(data)
        if not items:
            return f"No tours found in collection {collection_id}."
        lines = [f"Collection {collection_id} tours (page {page}, {len(items)}):"]
        for t in items:
            if not isinstance(t, dict):
                continue
            sub_emb = t.get("_embedded") or {}
            tour = None
            if isinstance(sub_emb, dict):
                tour = sub_emb.get("tour") or sub_emb.get("main_tour") or sub_emb.get("reference")
            if not isinstance(tour, dict):
                tour = t
            tid = tour.get("id", "?")
            name = tour.get("name") or "?"
            sport = tour.get("sport") or tour.get("sports") or "?"
            distance = tour.get("distance")
            line = f"  [{tid}] {name} | sport={sport}"
            if isinstance(distance, (int, float)):
                line += f" | {distance / 1000:.1f} km"
            lines.append(line)
        return "\n".join(lines)

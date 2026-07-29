import json
import time
import urllib.parse
import urllib.request


class GeocoderError(Exception):
    pass

class Geocoder:
    def __init__(self):
        self._last_call = 0.0
        self._min_interval = 0.5

    def _wait(self):
        elapsed = time.monotonic() - self._last_call
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)
        self._last_call = time.monotonic()

    def forward(self, query: str, limit: int = 5) -> list[dict]:
        self._wait()
        params = urllib.parse.urlencode({"q": query, "limit": limit})
        url = f"https://photon.komoot.io/api/?{params}"

        try:
            with urllib.request.urlopen(url, timeout=15) as resp:
                data = json.loads(resp.read().decode())
        except urllib.error.URLError as e:
            raise GeocoderError(f"Geocoding failed: {e}") from e

        results = []
        for feat in data.get("features", []):
            props = feat.get("properties", {})
            coords = feat.get("geometry", {}).get("coordinates", [])
            results.append({
                "display_name": props.get("name", ""),
                "lat": coords[1] if len(coords) >= 2 else None,
                "lon": coords[0] if len(coords) >= 1 else None,
                "type": props.get("type", ""),
                "city": props.get("city", props.get("name", "")),
                "country": props.get("country", ""),
                "osm_id": props.get("osm_id", ""),
            })
        return results

    def reverse(self, lat: float, lon: float) -> dict:
        self._wait()
        params = urllib.parse.urlencode({"lat": lat, "lon": lon})
        url = f"https://photon.komoot.io/reverse/?{params}"

        try:
            with urllib.request.urlopen(url, timeout=15) as resp:
                data = json.loads(resp.read().decode())
        except urllib.error.URLError as e:
            raise GeocoderError(f"Reverse geocoding failed: {e}") from e

        features = data.get("features", [])
        if not features:
            return {"display_name": "Unknown location", "lat": lat, "lon": lon, "type": "", "city": "", "country": ""}

        feat = features[0]
        props = feat.get("properties", {})
        coords = feat.get("geometry", {}).get("coordinates", [])
        return {
            "display_name": props.get("name", ""),
            "lat": coords[1] if len(coords) >= 2 else lat,
            "lon": coords[0] if len(coords) >= 1 else lon,
            "type": props.get("type", ""),
            "city": props.get("city", props.get("name", "")),
            "country": props.get("country", ""),
            "osm_id": props.get("osm_id", ""),
        }

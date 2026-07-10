"""
url_discovery.py — Per-item URL discovery via Brave Search API.

AI NEVER generates URLs. This module discovers URLs for every named
attraction, restaurant, scenic drive, and en-route stop after AI
content generation is complete.

Two-pass restaurant strategy:
  Pass 1: Google Maps domain filter (top-rated, accurate hours)
  Pass 2: TripAdvisor domain filter (local favorites, cuisine diversity)

Search API history:
  v1.0: Bing Search API v7 (retired August 11, 2025)
  v1.1: Google Custom Search (deprecated full-web search, unusable)
  v1.2: Brave Search API (current) — api.search.brave.com
        Free tier: 2,000 queries/month, no credit card required
"""
from __future__ import annotations
import logging, os, time
from typing import Any
import requests

logger = logging.getLogger(__name__)
BRAVE_SEARCH_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"
MAX_FALLBACK_ATTEMPTS = 4
REQUEST_DELAY = 0.25


def _build_query_variants(name: str, destination: str, category: str) -> list[str]:
    """Return 4 progressively broader query strings for a named item."""
    return [
        f'"{name}" {destination} {category} official site',
        f'"{name}" {destination} {category}',
        f'{name} {destination} {category}',
        f'{name} {destination}',
    ]


class URLDiscoverer:
    def __init__(self, config_path: str | Any = "config.yaml") -> None:
        self._api_key = os.environ["BRAVE_SEARCH_API_KEY"]
        self._headers = {
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
            "X-Subscription-Token": self._api_key,
        }
        self._session = requests.Session()
        self._session.headers.update(self._headers)

    # ── Public entry point ───────────────────────────────────────────────────

    def discover_all(self, trip: dict[str, Any]) -> None:
        for dest in trip.get("destinations", []):
            name = dest["name"]
            ai = dest.get("ai_content", {})
            nps_code = dest.get("nps_park_code")
            logger.info("URL discovery for '%s'…", name)
            self._discover_attractions(ai, dest_name=name, nps_code=nps_code)
            self._discover_restaurants(ai, dest_name=name)
            self._discover_en_route_stops(ai, dest_name=name)
            self._discover_scenic_drives(dest, dest_name=name)

    # ── Attractions ──────────────────────────────────────────────────────────

    def _discover_attractions(
        self, ai: dict[str, Any], dest_name: str, nps_code: str | None
    ) -> None:
        for attr in ai.get("top_attractions", []):
            attr_name = attr.get("name", "")
            # For NPS parks, prefer nps.gov results
            site_hint = f"site:nps.gov/{nps_code}" if nps_code else None
            url = self._search_first(
                _build_query_variants(attr_name, dest_name, "trail hike attraction"),
                site_filter="nps.gov" if nps_code else None,
                site_hint=site_hint,
            )
            # Fallback: AllTrails
            if not url:
                url = self._search_first(
                    _build_query_variants(attr_name, dest_name, "trail hiking"),
                    site_filter="alltrails.com",
                )
            attr["url"] = url or ""

    # ── Restaurants — two-pass ───────────────────────────────────────────────

    def _discover_restaurants(self, ai: dict[str, Any], dest_name: str) -> None:
        for rest in ai.get("dinner_recommendations", []):
            rest_name = rest.get("name", "")
            # Pass 1: Google Maps
            url = self._search_first(
                _build_query_variants(rest_name, dest_name, "restaurant"),
                site_filter="google.com/maps",
            )
            # Pass 2: TripAdvisor
            if not url:
                url = self._search_first(
                    _build_query_variants(rest_name, dest_name, "restaurant"),
                    site_filter="tripadvisor.com",
                )
            rest["url"] = url or ""

    # ── En-Route Stops ───────────────────────────────────────────────────────

    def _discover_en_route_stops(self, ai: dict[str, Any], dest_name: str) -> None:
        for stop in ai.get("getting_here", {}).get("en_route_stops", []):
            stop_name = stop.get("name", "")
            url = self._search_first(
                _build_query_variants(stop_name, dest_name, "attraction stop")
            )
            stop["url"] = url or ""

    # ── Scenic Drives ────────────────────────────────────────────────────────

    def _discover_scenic_drives(self, dest: dict[str, Any], dest_name: str) -> None:
        for drive in dest.get("scenic_drives", []):
            drive_name = drive.get("title", "")
            url = self._search_first(
                _build_query_variants(drive_name, dest_name, "scenic drive viewpoint")
            )
            drive["url"] = url or ""

    # ── Brave Search helpers ─────────────────────────────────────────────────

    def _search_first(
        self,
        query_variants: list[str],
        site_filter: str | None = None,
        site_hint: str | None = None,
    ) -> str | None:
        from generator.url_validator import URLValidator
        uv = URLValidator()

        for query in query_variants[:MAX_FALLBACK_ATTEMPTS]:
            # Prepend site: operator if filtering to a specific domain
            if site_hint:
                full_query = f"{site_hint} {query}"
            elif site_filter:
                full_query = f"site:{site_filter} {query}"
            else:
                full_query = query

            try:
                resp = self._session.get(
                    BRAVE_SEARCH_ENDPOINT,
                    params={"q": full_query, "count": 5, "search_lang": "en"},
                    timeout=10,
                )
                resp.raise_for_status()
                results = resp.json().get("web", {}).get("results", [])
                for item in results:
                    url = item.get("url", "")
                    if not url:
                        continue
                    # If domain filtering, check that result is on right domain
                    if site_filter and site_filter not in url:
                        continue
                    ok, _ = uv.verify_url(url)
                    if ok:
                        logger.debug("  URL: %s → %s", full_query[:60], url[:80])
                        return url
                time.sleep(REQUEST_DELAY)
            except requests.RequestException as exc:
                logger.warning("Brave Search error for '%s': %s", query[:60], exc)

        return None

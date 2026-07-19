"""
url_discovery.py — Per-item URL discovery via Bing Web Search (Azure AI Services).

AI NEVER generates URLs. This module discovers URLs for every named
attraction, restaurant, scenic drive, and en-route stop after AI
content generation is complete.

Two-pass restaurant strategy:
  Pass 1: Google Maps domain filter (top-rated, accurate hours)
  Pass 2: TripAdvisor domain filter (local favorites, cuisine diversity)

Search API history:
  v1.0: Bing Search API v7 (retired August 11, 2025)
  v1.1: Google Custom Search (deprecated full-web search, unusable)
  v1.2: Brave Search API (retired in favour of Azure AI Services)
  v1.3: Bing Web Search API — Azure AI Services (current)
        api.bing.microsoft.com/v7.0/search
"""
from __future__ import annotations
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any
from generator.bing_search import BingWebSearch

logger = logging.getLogger(__name__)
MAX_FALLBACK_ATTEMPTS = 4


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
        self._search = BingWebSearch()

    # ── Public entry point ───────────────────────────────────────────────────

    def discover_all(self, trip: dict[str, Any]) -> None:
        destinations = trip.get("destinations", [])

        def _discover_one(dest: dict) -> None:
            name = dest["name"]
            ai = dest.get("ai_content", {})
            nps_code = dest.get("nps_park_code")
            logger.info("URL discovery for '%s'…", name)
            # Parallelise the four independent URL categories within each destination
            with ThreadPoolExecutor(max_workers=4) as inner:
                futs = [
                    inner.submit(self._discover_attractions, ai, name, nps_code),
                    inner.submit(self._discover_restaurants, ai, name),
                    inner.submit(self._discover_en_route_stops, ai, name),
                    inner.submit(self._discover_scenic_drives, dest, name),
                ]
                for f in as_completed(futs):
                    f.result()

        with ThreadPoolExecutor(max_workers=min(len(destinations), 3)) as pool:
            futures = [pool.submit(_discover_one, d) for d in destinations]
            for f in as_completed(futures):
                f.result()

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

    # ── Bing Search helpers ──────────────────────────────────────────────────

    def _search_first(
        self,
        query_variants: list[str],
        site_filter: str | None = None,
        site_hint: str | None = None,
    ) -> str | None:
        return self._search.search_first_url(
            query_variants,
            site_filter=site_filter,
            site_hint=site_hint,
            max_attempts=MAX_FALLBACK_ATTEMPTS,
        )

"""
url_discovery.py — Per-item URL discovery via Grok semantic search.

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
  v1.3: Bing Web Search API (deprecated, limited availability)
  v1.4: Google Programmable Search Engine (rate-limited, prohibitive costs)
  v1.5: xAI Grok semantic search (current)
        api.x.ai/v1/chat/completions
"""
from __future__ import annotations
import logging
import re
from urllib.parse import quote
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any
from generator.grok_search import GrokSearch
from generator.llm_client import MultiLLMClient
from generator.url_validator import URLValidator

logger = logging.getLogger(__name__)
MAX_FALLBACK_ATTEMPTS = 4

# ── URL Search Cache ────────────────────────────────────────────────────
_url_cache: dict[tuple[str, str, str], str | None] = {}


def _build_query_variants(name: str, destination: str, category: str) -> list[str]:
    """Return progressively broader query strings for a named item."""
    return [
        f'"{name}" {destination} {category} official site',
        f'"{name}" {destination} {category}',
        f'{name} {destination} {category}',
        f'{name} {destination}',
    ]


class URLDiscoverer:
    def __init__(self, config_path: str | Any = "config.yaml", llm_client: MultiLLMClient | None = None) -> None:
        self._llm = llm_client or MultiLLMClient(config_path)
        self._search = GrokSearch(
            usage_tracker=self._llm.usage_tracker,
            usage_operation_prefix="url_discovery",
        )
        self._url_validator = URLValidator()

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
            attr_type = str(attr.get("type", "attraction") or "attraction").lower()

            # Hiking items must resolve to AllTrails only.
            if attr_type == "hike":
                url = self._search_first(
                    _build_query_variants(attr_name, dest_name, "trail hiking"),
                    site_filter="alltrails.com",
                    item_name=attr_name,
                    dest_name=dest_name,
                )
                attr["url"] = url or f"https://www.google.com/maps/search/?api=1&query={quote(f'{attr_name} {dest_name}') }"
                logger.info("  hike link (alltrails): %s -> %s", attr_name, (url or "(none)"))
                continue

            # For NPS parks, prefer nps.gov results
            site_hint = f"site:nps.gov/{nps_code}" if nps_code else None
            url = self._search_first(
                _build_query_variants(attr_name, dest_name, "trail hike attraction"),
                site_filter="nps.gov" if nps_code else None,
                site_hint=site_hint,
                item_name=attr_name,
                dest_name=dest_name,
            )
            # Fallback: AllTrails
            if not url:
                url = self._search_first(
                    _build_query_variants(attr_name, dest_name, "trail hiking"),
                    site_filter="alltrails.com",
                    item_name=attr_name,
                    dest_name=dest_name,
                )
            attr["url"] = url or f"https://www.google.com/maps/search/?api=1&query={quote(f'{attr_name} {dest_name}') }"
            logger.info("  attraction link: %s -> %s", attr_name, (url or "(none)"))

    # ── Restaurants — two-pass ───────────────────────────────────────────────

    def _discover_restaurants(self, ai: dict[str, Any], dest_name: str) -> None:
        for rest in ai.get("dinner_recommendations", []):
            rest_name = rest.get("name", "")
            # Pass 1: Google Maps
            url = self._search_first(
                _build_query_variants(rest_name, dest_name, "restaurant"),
                site_filter="google.com/maps",
                item_name=rest_name,
                dest_name=dest_name,
            )
            # Pass 2: TripAdvisor
            if not url:
                url = self._search_first(
                    _build_query_variants(rest_name, dest_name, "restaurant"),
                    site_filter="tripadvisor.com",
                    item_name=rest_name,
                    dest_name=dest_name,
                )
            rest["url"] = url or ""
            rest["maps_url"] = f"https://www.google.com/maps/search/?api=1&query={rest_name.replace(' ', '+')}+{dest_name.replace(' ', '+')}"
            logger.info("  restaurant link: %s -> %s", rest_name, (url or "(none)"))

    # ── En-Route Stops ───────────────────────────────────────────────────────

    def _discover_en_route_stops(self, ai: dict[str, Any], dest_name: str) -> None:
        for stop in ai.get("getting_here", {}).get("en_route_stops", []):
            stop_name = stop.get("name", "")
            url = self._search_first(
                _build_query_variants(stop_name, dest_name, "attraction stop"),
                item_name=stop_name,
                dest_name=dest_name,
            )
            stop["url"] = url or f"https://www.google.com/maps/search/?api=1&query={quote(f'{stop_name} {dest_name}') }"
            logger.info("  en-route link: %s -> %s", stop_name, (url or "(none)"))

    # ── Scenic Drives ────────────────────────────────────────────────────────

    def _discover_scenic_drives(self, dest: dict[str, Any], dest_name: str) -> None:
        for drive in dest.get("scenic_drives", []):
            drive_name = drive.get("title", "")
            url = self._search_first(
                _build_query_variants(drive_name, dest_name, "scenic drive viewpoint"),
                item_name=drive_name,
                dest_name=dest_name,
            )
            drive["url"] = url or f"https://www.google.com/search?q={quote(f'{drive_name} {dest_name} scenic drive') }"
            logger.info("  scenic drive link: %s -> %s", drive_name, (url or "(none)"))

    # ── Bing Search helpers ──────────────────────────────────────────────────

    def _search_first(
        self,
        query_variants: list[str],
        site_filter: str | None = None,
        site_hint: str | None = None,
        item_name: str = "",
        dest_name: str = "",
    ) -> str | None:
        # Check cache first
        cache_key = (item_name, dest_name, site_filter or "")
        if cache_key in _url_cache:
            logger.info("  cache hit: %s (%s) -> %s", item_name, site_filter or "any", _url_cache[cache_key] or "(none)")
            return _url_cache[cache_key]
        
        # Search and cache result
        result = self._search_first_strict(
            query_variants=query_variants,
            site_filter=site_filter,
            site_hint=site_hint,
            item_name=item_name,
            dest_name=dest_name,
        )
        _url_cache[cache_key] = result
        logger.info("  resolved: %s (%s) -> %s", item_name, site_filter or "any", result or "(none)")
        return result

    def _search_first_strict(
        self,
        *,
        query_variants: list[str],
        site_filter: str | None,
        site_hint: str | None,
        item_name: str,
        dest_name: str,
    ) -> str | None:
        for query in query_variants[:MAX_FALLBACK_ATTEMPTS]:
            full_query = f"{site_hint} {query}" if site_hint else (f"site:{site_filter} {query}" if site_filter else query)
            candidates = self._search.search(full_query, count=10)

            # Pass 1: specific pages only
            for item in candidates:
                url = item.get("url", "")
                if not url:
                    continue
                if site_filter and site_filter not in url:
                    continue
                if not self._is_specific_result_url(url, item_name, dest_name):
                    continue
                if self._is_alltrails_trail_url(url):
                    if self._is_relevant_result(url, item_name, dest_name):
                        logger.debug("  URL strict (alltrails): %s -> %s", full_query[:70], url[:120])
                        return url
                    continue
                ok, _ = self._url_validator.verify_url(url)
                if ok and self._is_relevant_result(url, item_name, dest_name):
                    logger.debug("  URL strict: %s -> %s", full_query[:70], url[:120])
                    return url

            # Pass 2: any live URL for this variant as fallback
            for item in candidates:
                url = item.get("url", "")
                if not url:
                    continue
                if site_filter and site_filter not in url:
                    continue
                if self._is_alltrails_trail_url(url):
                    logger.debug("  URL fallback (alltrails): %s -> %s", full_query[:70], url[:120])
                    return url
                ok, _ = self._url_validator.verify_url(url)
                if ok:
                    logger.debug("  URL fallback: %s -> %s", full_query[:70], url[:120])
                    return url

        return None

    def _is_specific_result_url(self, url: str, item_name: str, dest_name: str) -> bool:
        lower = url.lower()
        if "google.com/search" in lower or "/search?" in lower:
            return False
        if "nps.gov" in lower and "/search" in lower:
            return False

        # Reject attribution/media pages that are not "more info" landing pages.
        if "commons.wikimedia.org" in lower or "wikipedia.org/wiki/file:" in lower:
            return False

        # Reject obvious generic landing pages.
        generic_patterns = [
            "/plan-your-visit",
            "/visit",
            "/things-to-do",
            "/explore",
            "/about",
            "/home",
            "/index.htm",
            "/index.html",
        ]
        if any(pattern in lower for pattern in generic_patterns):
            return False

        item_tokens = self._significant_tokens(item_name)
        if item_tokens and not any(token in lower for token in item_tokens):
            return False

        # If destination tokens exist in URL it's usually a much better match.
        dest_tokens = self._significant_tokens(dest_name)
        if dest_tokens and any(token in lower for token in dest_tokens):
            return True

        return True

    def _is_relevant_result(self, url: str, item_name: str, dest_name: str) -> bool:
        """Lightweight relevance gate: avoid live but useless links."""
        if self._is_alltrails_trail_url(url):
            lower_url = url.lower()
            item_tokens = self._significant_tokens(item_name)
            if item_tokens and not any(t in lower_url for t in item_tokens[:3]):
                return False
            return True
        try:
            resp = self._url_validator.session.get(url, timeout=8)
            if resp.status_code >= 400:
                return False
            text = (resp.text or "").lower()
            item_tokens = self._significant_tokens(item_name)
            dest_tokens = self._significant_tokens(dest_name)
            if item_tokens and not any(t in text for t in item_tokens[:3]):
                return False
            if dest_tokens and not any(t in text for t in dest_tokens[:2]):
                return False
            return True
        except Exception:
            return False

    @staticmethod
    def _is_alltrails_trail_url(url: str) -> bool:
        lower = (url or "").lower()
        return "alltrails.com" in lower and "/trail/" in lower

    @staticmethod
    def _significant_tokens(text: str) -> list[str]:
        tokens = re.findall(r"[a-z0-9]+", (text or "").lower())
        stop = {
            "the", "and", "for", "with", "from", "near", "park", "national", "state", "trail",
            "road", "drive", "point", "restaurant", "cafe", "grill", "utah", "colorado", "new", "mexico",
        }
        return [t for t in tokens if len(t) >= 4 and t not in stop]

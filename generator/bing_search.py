"""
bing_search.py — Bing Web Search API client (Azure AI Services).

Replaces: Brave Search API (v1.2, retired in favour of Azure AI Services)
Docs:     https://learn.microsoft.com/azure/cognitive-services/bing-web-search/
Endpoint: https://api.bing.microsoft.com/v7.0/search
Env var:  BING_SEARCH_API_KEY

Search API history:
  v1.0: Bing Search API v7 (retired August 11, 2025)
  v1.1: Google Custom Search (deprecated full-web search, unusable)
  v1.2: Brave Search API (retired in favour of Azure AI Services)
  v1.3: Bing Web Search API — Azure AI Services (current)
        api.bing.microsoft.com/v7.0/search
        Key managed via Azure portal; pay-per-query
"""
from __future__ import annotations
import logging, os, threading, time
from typing import Any
import requests

logger = logging.getLogger(__name__)
BING_ENDPOINT = "https://api.bing.microsoft.com/v7.0/search"
_DEFAULT_DELAY = 0.1


class BingWebSearch:
    """
    Thread-safe Bing Web Search v7 client.

    Each calling thread gets its own ``requests.Session`` via
    ``threading.local()``, so the instance is safe to share across a
    ``ThreadPoolExecutor``.
    """

    def __init__(
        self,
        api_key: str | None = None,
        results_per_query: int = 8,
        timeout_seconds: int = 10,
        request_delay_seconds: float = _DEFAULT_DELAY,
    ) -> None:
        self._api_key = api_key or os.environ["BING_SEARCH_API_KEY"]
        self._results_per_query = results_per_query
        self._timeout = timeout_seconds
        self._delay = request_delay_seconds
        self._session_local = threading.local()

    # ── Thread-local session ─────────────────────────────────────────────────

    def _get_session(self) -> requests.Session:
        if not hasattr(self._session_local, "session"):
            s = requests.Session()
            s.headers.update({
                "Ocp-Apim-Subscription-Key": self._api_key,
                "Accept": "application/json",
            })
            self._session_local.session = s
        return self._session_local.session

    # ── Core search ──────────────────────────────────────────────────────────

    def search(self, query: str, count: int | None = None) -> list[dict[str, Any]]:
        """
        Execute a single Bing web search.

        Returns a list of ``{name, snippet, url}`` dicts (up to ``count``
        results).  Returns ``[]`` on any HTTP or parse error so callers
        never need to guard against exceptions.
        """
        n = count or self._results_per_query
        try:
            resp = self._get_session().get(
                BING_ENDPOINT,
                params={
                    "q": query,
                    "count": n,
                    "mkt": "en-US",
                    "responseFilter": "Webpages",
                    "textDecorations": False,
                },
                timeout=self._timeout,
            )
            resp.raise_for_status()
            time.sleep(self._delay)
            return [
                {
                    "name": item.get("name", ""),
                    "snippet": item.get("snippet", ""),
                    "url": item.get("url", ""),
                }
                for item in resp.json().get("webPages", {}).get("value", [])
            ]
        except requests.RequestException as exc:
            logger.warning("Bing Search error for %r: %s", query[:60], exc)
            return []

    # ── URL-resolution helper ────────────────────────────────────────────────

    def search_first_url(
        self,
        query_variants: list[str],
        site_filter: str | None = None,
        site_hint: str | None = None,
        max_attempts: int = 4,
    ) -> str | None:
        """
        Try each query variant in order and return the first live URL found.

        *site_filter*  — only accept URLs whose string contains this value
                         (e.g. ``"alltrails.com"``).
        *site_hint*    — prepend a ``site:`` operator string verbatim
                         (e.g. ``"site:nps.gov/zion"``).
        *max_attempts* — cap on how many variants are tried.

        URL liveness is verified via :class:`~generator.url_validator.URLValidator`.
        Returns ``None`` when no valid URL is found across all variants.
        """
        from generator.url_validator import URLValidator
        uv = URLValidator()

        for query in query_variants[:max_attempts]:
            if site_hint:
                full_query = f"{site_hint} {query}"
            elif site_filter:
                full_query = f"site:{site_filter} {query}"
            else:
                full_query = query

            for item in self.search(full_query, count=5):
                url = item.get("url", "")
                if not url:
                    continue
                if site_filter and site_filter not in url:
                    continue
                ok, _ = uv.verify_url(url)
                if ok:
                    logger.debug("  URL: %s → %s", full_query[:60], url[:80])
                    return url

        return None

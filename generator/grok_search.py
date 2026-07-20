"""
grok_search.py — xAI Grok-based web search provider.

Uses Grok's chat completions API to perform semantic web search and extract
structured results. Grok is instructed to return valid JSON only.

Replaces: Google Programmable Search Engine (v1.4)
Docs:     https://docs.x.ai/
Endpoint: https://api.x.ai/v1/chat/completions
Env var:  XAI_API_KEY

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
import json, logging, os, threading
from typing import Any
import requests

logger = logging.getLogger(__name__)
GROK_ENDPOINT = "https://api.x.ai/v1/chat/completions"
_DEFAULT_DELAY = 0.05


class GrokSearch:
    """
    Thread-safe xAI Grok-based search provider.

    Uses Grok's chat completions API to perform semantic web search.
    Grok is instructed to return structured JSON results only.

    Each calling thread gets its own ``requests.Session`` via
    ``threading.local()``, so the instance is safe to share across a
    ``ThreadPoolExecutor``.
    """

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        timeout_seconds: int = 15,
        request_delay_seconds: float = _DEFAULT_DELAY,
    ) -> None:
        self._api_key = api_key or os.environ["XAI_API_KEY"]
        self._model = model or os.environ.get("XAI_MODEL", "grok-2-latest")
        self._timeout = timeout_seconds
        self._delay = request_delay_seconds
        self._session_local = threading.local()

    # ── Thread-local session ─────────────────────────────────────────────────

    def _get_session(self) -> requests.Session:
        if not hasattr(self._session_local, "session"):
            s = requests.Session()
            s.headers.update({
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            })
            self._session_local.session = s
        return self._session_local.session

    # ── Core search ──────────────────────────────────────────────────────────

    def search(self, query: str, count: int | None = None) -> list[dict[str, Any]]:
        """
        Execute a semantic web search via Grok.

        Returns a list of ``{name, snippet, url}`` dicts (up to ``count``
        results).  Returns ``[]`` on any HTTP, parse, or API error so callers
        never need to guard against exceptions.
        """
        try:
            results = self._grok_search(query, attempt=1)
            return results[:count] if count else results
        except Exception as exc:
            logger.warning("Grok search error for %r: %s", query[:60], exc)
            return []

    def _grok_search(self, query: str, attempt: int = 1) -> list[dict[str, Any]]:
        """Execute Grok search with optional retry on malformed JSON."""
        system_prompt = (
            "You are a web search engine. Perform a web search for the user query and return results "
            "strictly in this JSON format:\n"
            '{"results": [{"title": "...", "url": "...", "snippet": "..."}]}\n'
            "Return only valid JSON. No commentary, no prose, no markdown. JSON only."
            if attempt == 1
            else
            "Return valid JSON only. No commentary. Format: "
            '{"results": [{"title": "...", "url": "...", "snippet": "..."}]}'
        )

        try:
            payload = {
                "model": self._model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": query},
                ],
                "temperature": 0.7,
            }
            logger.debug(f"[Grok-Attempt{attempt}] Posting to {GROK_ENDPOINT} with model={self._model}")
            logger.debug(f"[Grok-Attempt{attempt}] API Key prefix: {self._api_key[:20]}...")
            logger.debug(f"[Grok-Attempt{attempt}] Query: {query[:100]}")
            
            resp = self._get_session().post(
                GROK_ENDPOINT,
                json=payload,
                timeout=self._timeout,
            )
            logger.debug(f"[Grok-Attempt{attempt}] Response Status: {resp.status_code}")
            
            resp.raise_for_status()
            response_json = resp.json()
            content = response_json.get("choices", [{}])[0].get("message", {}).get("content", "")

            # Parse JSON from Grok response
            parsed = json.loads(content)
            results = parsed.get("results", [])

            # Normalize to {name, snippet, url} format
            normalized = []
            for item in results:
                normalized.append({
                    "name": item.get("title", ""),
                    "snippet": item.get("snippet", ""),
                    "url": item.get("url", ""),
                })
            return normalized

        except json.JSONDecodeError as exc:
            if attempt < 2:
                logger.warning("Grok returned malformed JSON on attempt 1, retrying with stricter prompt")
                return self._grok_search(query, attempt=2)
            logger.warning("Grok returned malformed JSON after retry for %r: %s", query[:60], exc)
            return []
        except requests.RequestException as exc:
            # Log full response body for 4xx/5xx errors
            if hasattr(exc, 'response') and exc.response is not None:
                try:
                    resp_body = exc.response.text[:500]
                    logger.warning(
                        "Grok API error for %r (status=%s): %s | Response: %s",
                        query[:60], 
                        exc.response.status_code,
                        exc,
                        resp_body
                    )
                except Exception:
                    logger.warning("Grok API error for %r: %s", query[:60], exc)
            else:
                logger.warning("Grok API error for %r: %s", query[:60], exc)
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

            for item in self.search(full_query, count=10):
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

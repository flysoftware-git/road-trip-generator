"""
cultural_events.py — Auto-discover cultural events via Brave Search API + AI synthesis.

NEVER invents events. Uses has_events decision tree:
  Format A: real events discovered, with venue, dates, admission
  Format B: honest fallback — no invented events

Search API history:
  v1.0: Bing Search API v7 (retired August 11, 2025)
  v1.1: Google Custom Search (deprecated full-web search, unusable)
  v1.2: Brave Search API (current) — api.search.brave.com
        Free tier: 2,000 queries/month, no credit card required
        Single API key, no engine ID needed
"""
from __future__ import annotations
import json, logging, os, time
from pathlib import Path
from typing import Any
import requests
from tenacity import retry, stop_after_attempt, wait_exponential
from generator.llm_client import MultiLLMClient

logger = logging.getLogger(__name__)
PROMPTS_DIR = Path(__file__).parent.parent / "prompts"
BRAVE_SEARCH_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"


class CulturalEventsDiscoverer:
    def __init__(
        self,
        config_path: Path | str = "config.yaml",
        llm_client: MultiLLMClient | None = None,
    ) -> None:
        import yaml
        with Path(config_path).open() as f:
            self._config = yaml.safe_load(f)
        self._api_key = os.environ["BRAVE_SEARCH_API_KEY"]
        self._llm = llm_client or MultiLLMClient(config_path)
        self._template = (PROMPTS_DIR / "cultural_events.txt").read_text(encoding="utf-8")
        self._system_prompt = (PROMPTS_DIR / "system_prompt.txt").read_text(encoding="utf-8")

    def discover(self, trip: dict[str, Any]) -> None:
        for dest in trip.get("destinations", []):
            logger.info("Discovering cultural events for '%s'…", dest["name"])
            dest["cultural_events"] = self._discover_for_dest(dest)

    def _discover_for_dest(self, dest: dict[str, Any]) -> dict[str, Any]:
        raw_results = self._brave_search(dest["name"], dest["dates"])
        dest_type = self._classify_destination(dest["name"])
        result = self._synthesize(dest["name"], dest["dates"], dest_type, raw_results)
        # Verify any event URLs that came back
        if result.get("has_events") and result.get("events"):
            from generator.url_validator import URLValidator
            uv = URLValidator()
            for event in result["events"]:
                if event.get("url"):
                    ok, _ = uv.verify_url(event["url"])
                    if not ok:
                        event.pop("url", None)
        return result

    def _brave_search(self, destination: str, dates: str) -> list[dict[str, Any]]:
        month = dates.split()[0] if dates else "October"
        queries = [
            f"{destination} festivals events {month} 2026",
            f"{destination} cultural events concerts {month} 2026",
            f"events near {destination} fall 2026",
        ]
        all_results: list[dict[str, Any]] = []
        headers = {
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
            "X-Subscription-Token": self._api_key,
        }
        for query in queries:
            try:
                resp = requests.get(
                    BRAVE_SEARCH_ENDPOINT,
                    headers=headers,
                    params={"q": query, "count": 8, "search_lang": "en"},
                    timeout=10,
                )
                resp.raise_for_status()
                data = resp.json()
                for item in data.get("web", {}).get("results", []):
                    all_results.append({
                        "name": item.get("title", ""),
                        "snippet": item.get("description", ""),
                        "url": item.get("url", ""),
                    })
                time.sleep(0.25)
            except requests.RequestException as exc:
                logger.warning("Brave Search error for '%s': %s", destination, exc)
        return all_results[:20]

    def _classify_destination(self, name: str) -> str:
        lower = name.lower()
        if any(k in lower for k in ("national park", "national monument", "canyon", "reef")):
            return "national_park"
        if any(k in lower for k in ("telluride", "aspen", "vail", "park city")):
            return "resort_town"
        if any(k in lower for k in ("santa fe", "albuquerque", "denver", "phoenix")):
            return "city"
        return "small_town"

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=2, min=2, max=30))
    def _synthesize(
        self, name: str, dates: str, dest_type: str, search_results: list[dict[str, Any]]
    ) -> dict[str, Any]:
        search_context = json.dumps(search_results[:15], indent=2)
        prompt = self._template.format(
            destination_name=name,
            dates=dates,
            destination_type=dest_type,
            bing_results=search_context,   # field name kept for prompt compatibility
        )
        return self._llm.generate_json(
            system_prompt=self._system_prompt,
            user_prompt=prompt,
            operation=f"cultural_events:{name}",
            temperature=0.3,
            max_tokens=1500,
        )

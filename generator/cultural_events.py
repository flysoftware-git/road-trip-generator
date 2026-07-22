"""
cultural_events.py — Auto-discover cultural events via Grok semantic search + AI synthesis.

NEVER invents events. Uses has_events decision tree:
  Format A: real events discovered, with venue, dates, admission
  Format B: honest fallback — no invented events

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
import json, logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from tenacity import retry, stop_after_attempt, wait_exponential
from generator.grok_search import GrokSearch
from generator.llm_client import MultiLLMClient

logger = logging.getLogger(__name__)
PROMPTS_DIR = Path(__file__).parent.parent / "prompts"


class CulturalEventsDiscoverer:
    def __init__(
        self,
        config_path: Path | str = "config.yaml",
        llm_client: MultiLLMClient | None = None,
    ) -> None:
        import yaml
        with Path(config_path).open() as f:
            self._config = yaml.safe_load(f)
        self._llm = llm_client or MultiLLMClient(config_path)
        self._search = GrokSearch(
            usage_tracker=self._llm.usage_tracker,
            usage_operation_prefix="cultural_events",
        )
        self._template = (PROMPTS_DIR / "cultural_events.txt").read_text(encoding="utf-8")
        self._system_prompt = (PROMPTS_DIR / "system_prompt.txt").read_text(encoding="utf-8")

    def discover(self, trip: dict[str, Any]) -> None:
        destinations = trip.get("destinations", [])

        def _one(dest: dict) -> None:
            try:
                logger.info("Discovering cultural events for '%s'…", dest["name"])
                result = self._discover_for_dest(dest)
                dest["cultural_events"] = result
                logger.info("  → Cultural events for '%s': has_events=%s, count=%d", 
                           dest["name"], result.get("has_events", False), len(result.get("events", [])))
            except Exception as e:
                logger.error("Failed to discover cultural events for '%s': %s", dest["name"], e, exc_info=True)
                # Return honest fallback on any error
                dest["cultural_events"] = {
                    "has_events": False,
                    "honest_assessment": f"Unable to discover events at this time."
                }

        with ThreadPoolExecutor(max_workers=min(len(destinations), 4)) as pool:
            futures = [pool.submit(_one, d) for d in destinations]
            for f in as_completed(futures):
                f.result()

    def _discover_for_dest(self, dest: dict[str, Any]) -> dict[str, Any]:
        try:
            raw_results = self._grok_search(dest["name"], dest["dates"])
            dest_type = self._classify_destination(dest["name"])
            result = self._synthesize(dest["name"], dest["dates"], dest_type, raw_results)
            
            # Ensure result is a dict with expected structure
            if not isinstance(result, dict):
                logger.warning("_synthesize returned non-dict for '%s': %s", dest["name"], type(result))
                return {"has_events": False, "honest_assessment": "Unable to parse events."}
            
            # Verify any event URLs that came back
            if result.get("has_events") and result.get("events"):
                from generator.url_validator import URLValidator
                uv = URLValidator()
                for event in result["events"]:
                    if event.get("url"):
                        ok, _ = uv.verify_url(event["url"])
                        if not ok:
                            event.pop("url", None)

            result = self._sanitize_local_tip_by_itinerary_days(result, dest.get("dates", ""))
            return result
        except Exception as e:
            logger.error("Exception in _discover_for_dest for '%s': %s", dest["name"], e, exc_info=True)
            return {"has_events": False, "honest_assessment": "Event discovery encountered an error."}

    def _grok_search(self, destination: str, dates: str) -> list[dict[str, Any]]:
        month = dates.split()[0] if dates else "October"
        queries = [
            f"{destination} festivals events {month} 2026",
            f"{destination} cultural events concerts {month} 2026",
            f"events near {destination} fall 2026",
        ]
        all_results: list[dict[str, Any]] = []
        for query in queries:
            all_results.extend(self._search.search(query, count=8))
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

    def _sanitize_local_tip_by_itinerary_days(self, result: dict[str, Any], dates: str) -> dict[str, Any]:
        if not isinstance(result, dict) or result.get("has_events"):
            return result

        tip = str(result.get("local_tip", "") or "").strip()
        if not tip:
            return result

        mentioned_days = self._extract_mentioned_weekdays(tip)
        if not mentioned_days:
            return result

        trip_days = self._extract_trip_weekdays(dates)
        # If we cannot determine trip weekdays with confidence, omit weekday-specific tips.
        if not trip_days:
            result.pop("local_tip", None)
            return result

        # Omit tips that reference any weekday not present in the itinerary window.
        if not mentioned_days.issubset(trip_days):
            result.pop("local_tip", None)
        return result

    def _extract_mentioned_weekdays(self, text: str) -> set[str]:
        day_tokens = {
            "monday": "monday",
            "mon": "monday",
            "tuesday": "tuesday",
            "tue": "tuesday",
            "tues": "tuesday",
            "wednesday": "wednesday",
            "wed": "wednesday",
            "thursday": "thursday",
            "thu": "thursday",
            "thurs": "thursday",
            "friday": "friday",
            "fri": "friday",
            "saturday": "saturday",
            "sat": "saturday",
            "sunday": "sunday",
            "sun": "sunday",
        }
        words = re.findall(r"[A-Za-z]+", text.lower())
        result = {day_tokens[w] for w in words if w in day_tokens}

        if "weekend" in words or "weekends" in words:
            result.update({"saturday", "sunday"})
        return result

    def _extract_trip_weekdays(self, dates: str) -> set[str]:
        parsed = self._parse_date_range(dates)
        if not parsed:
            return set()
        start_date, end_date = parsed
        names = [
            "monday",
            "tuesday",
            "wednesday",
            "thursday",
            "friday",
            "saturday",
            "sunday",
        ]
        out: set[str] = set()
        cursor = start_date
        while cursor <= end_date:
            out.add(names[cursor.weekday()])
            cursor += timedelta(days=1)
        return out

    def _parse_date_range(self, dates: str) -> tuple[datetime, datetime] | None:
        if not dates:
            return None

        normalized = dates.replace("–", "-")
        m = re.search(
            r"([A-Za-z]+)\s+(\d{1,2})(?:\s*-\s*(\d{1,2}))?,\s*(\d{4})",
            normalized,
        )
        if m:
            month_name = m.group(1)
            day_start = int(m.group(2))
            day_end = int(m.group(3) or m.group(2))
            year = int(m.group(4))
            try:
                start = datetime.strptime(f"{month_name} {day_start} {year}", "%B %d %Y")
                end = datetime.strptime(f"{month_name} {day_end} {year}", "%B %d %Y")
            except ValueError:
                return None
            if end < start:
                return None
            return start, end

        # Support explicit start/end dates like "2026-10-07 to 2026-10-09".
        iso = re.findall(r"(\d{4}-\d{2}-\d{2})", normalized)
        if len(iso) >= 2:
            try:
                start = datetime.strptime(iso[0], "%Y-%m-%d")
                end = datetime.strptime(iso[1], "%Y-%m-%d")
            except ValueError:
                return None
            if end < start:
                return None
            return start, end

        return None

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

"""
ai_content.py — Multi-provider LLM content generation.

CRITICAL: AI must NEVER generate URLs. This module produces names,
descriptions, schedules, and structured content only. All URLs are
discovered separately by url_discovery.py after this stage completes.
"""
from __future__ import annotations
from datetime import datetime
from difflib import SequenceMatcher
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any
import requests
from tenacity import retry, stop_after_attempt, wait_exponential
from generator.llm_client import MultiLLMClient

logger = logging.getLogger(__name__)
PROMPTS_DIR = Path(__file__).parent.parent / "prompts"


class AIContentGenerator:
    def __init__(
        self,
        config_path: Path | str = "config.yaml",
        llm_client: MultiLLMClient | None = None,
    ) -> None:
        import yaml
        with Path(config_path).open() as f:
            self._config = yaml.safe_load(f)
        self._llm = llm_client or MultiLLMClient(config_path)
        self._system_prompt = (PROMPTS_DIR / "system_prompt.txt").read_text(encoding="utf-8")
        self._dest_template = (PROMPTS_DIR / "destination_content.txt").read_text(encoding="utf-8")
        self._drives_template = (PROMPTS_DIR / "scenic_drives.txt").read_text(encoding="utf-8")
        self._weather_cache: dict[tuple[float, float, int], tuple[int, int] | None] = {}

    def generate_destination_content(self, trip: dict[str, Any]) -> None:
        """Generate AI content for every destination. Attaches 'ai_content' in-place."""
        destinations = trip.get("destinations", [])
        prev_names = ["none"] + [d["name"] for d in destinations[:-1]]
        next_names = [d["name"] for d in destinations[1:]] + [""]

        def _one(args: tuple[int, dict]) -> None:
            i, dest = args
            logger.info("Generating AI content for '%s'…", dest["name"])
            dest["ai_content"] = self._generate_for_destination(dest, trip["trip"], prev_names[i], next_names[i])

        with ThreadPoolExecutor(max_workers=min(len(destinations), 4)) as pool:
            futures = [pool.submit(_one, (i, d)) for i, d in enumerate(destinations)]
            for f in as_completed(futures):
                f.result()

    def generate_scenic_drive_descriptions(self, trip: dict[str, Any]) -> None:
        """Generate scenic drive popup descriptions. Attaches 'scenic_drives' in-place."""
        destinations = trip.get("destinations", [])

        def _one(dest: dict) -> None:
            logger.info("Generating scenic drives for '%s'…", dest["name"])
            result = self._generate_drives(dest)
            dest["scenic_drives"] = result
            logger.debug(f"  Set scenic_drives for {dest['name']}: {len(result)} drives")

        with ThreadPoolExecutor(max_workers=min(len(destinations), 4)) as pool:
            futures = [pool.submit(_one, d) for d in destinations]
            for f in as_completed(futures):
                f.result()
        
        # Verify all destinations have scenic_drives
        for dest in destinations:
            count = len(dest.get("scenic_drives", []))
            logger.info(f"✓ {dest['name']}: {count} scenic_drives")

    def generate_all(self, trip: dict[str, Any]) -> None:
        self.generate_destination_content(trip)
        self.generate_scenic_drive_descriptions(trip)

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=2, min=2, max=30))
    def _generate_for_destination(
        self, dest: dict[str, Any], trip_meta: dict[str, Any], prev: str, next_dest: str
    ) -> dict[str, Any]:
        seeds = dest.get("seeds", [])
        prompt = self._dest_template.format(
            destination_name=dest["name"],
            dates=dest["dates"],
            trip_title=trip_meta["title"],
            previous_destination=prev,
            next_destination=next_dest or "none",
            budget_guidance=self._build_budget_guidance(trip_meta),
            seeds="\n  ".join(f"- {s}" for s in seeds) if seeds else "  (none — generate full recommendations)",
        )
        result = self._llm.generate_json(
            system_prompt=self._system_prompt,
            user_prompt=prompt,
            operation=f"destination_content:{dest['id']}",
            temperature=self._config.get("ai", {}).get("temperature", self._config.get("azure_openai", {}).get("temperature", 0.7)),
            max_tokens=self._config.get("ai", {}).get("max_tokens", self._config.get("azure_openai", {}).get("max_tokens", 4096)),
        )
        return self._normalize_destination_content(
            result,
            dest.get("dates", ""),
            dest,
            trip_meta,
            prev,
            next_dest,
        )

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=2, min=2, max=30))
    def _generate_drives(self, dest: dict[str, Any]) -> list[dict[str, Any]]:
        # Derive region from destination name
        region_map = {"utah": "Utah", "colorado": "Colorado", "new mexico": "New Mexico",
                      "arizona": "Arizona", "nevada": "Nevada", "california": "California"}
        name_lower = dest["name"].lower()
        region = next((v for k, v in region_map.items() if k in name_lower), "Western United States")

        prompt = self._drives_template.format(
            destination_name=dest["name"],
            dates=dest["dates"],
            region=region,
        )
        data = self._llm.generate_json(
            system_prompt=self._system_prompt,
            user_prompt=prompt,
            operation=f"scenic_drives:{dest['id']}",
            temperature=self._config.get("ai", {}).get("temperature", self._config.get("azure_openai", {}).get("temperature", 0.7)),
            max_tokens=2048,
        )
        return data.get("scenic_drives", [])

    def _build_budget_guidance(self, trip_meta: dict[str, Any]) -> str:
        budget = trip_meta.get("budget")
        if budget in (None, "", {}):
            return "No explicit trip budget provided."

        if isinstance(budget, str):
            return f"Budget preference: {budget}"

        if isinstance(budget, (int, float)):
            return f"Budget cap noted: {budget}"

        if isinstance(budget, dict):
            parts: list[str] = []
            for key, value in budget.items():
                label = str(key).replace("_", " ")
                parts.append(f"{label}={value}")
            if parts:
                return "Budget guidance: " + "; ".join(parts)

        return f"Budget guidance: {budget}"

    def _normalize_destination_content(
        self,
        payload: dict[str, Any],
        dates: str,
        dest: dict[str, Any],
        trip_meta: dict[str, Any],
        previous_destination: str,
        next_destination: str,
    ) -> dict[str, Any]:
        payload["expected_environment"] = self._normalize_environment(
            payload.get("expected_environment", {}),
            dates,
            dest,
        )
        payload["getting_here"] = self._normalize_getting_here(
            payload.get("getting_here", {}),
            dest.get("name", ""),
        )
        payload["top_attractions"] = self._remove_enroute_stops_from_attractions(
            self._normalize_attractions(payload.get("top_attractions", [])),
            payload.get("getting_here", {}),
        )
        payload["possible_daily_schedule"] = self._normalize_schedule(
            payload.get("possible_daily_schedule", {}),
            payload.get("dinner_recommendations", []),
            dates,
            payload.get("getting_here", {}),
            previous_destination,
            next_destination,
        )
        payload["dinner_recommendations"] = self._normalize_restaurants(
            payload.get("dinner_recommendations", []),
            trip_meta.get("budget"),
        )
        return payload

    def _remove_enroute_stops_from_attractions(
        self,
        attractions: list[dict[str, Any]],
        getting_here: dict[str, Any],
    ) -> list[dict[str, Any]]:
        stops = getting_here.get("en_route_stops", []) if isinstance(getting_here, dict) else []
        stop_names = [str(s.get("name", "") or "").strip() for s in stops if isinstance(s, dict)]
        stop_names = [s for s in stop_names if s]
        if not attractions or not stop_names:
            return attractions

        def norm(text: str) -> str:
            n = text.lower().strip()
            n = re.sub(r"[^a-z0-9\s]", " ", n)
            n = re.sub(r"\b(trail|road|highway|route|state\s+park|national\s+park|park|overlook|viewpoint)\b", " ", n)
            n = re.sub(r"\s+", " ", n).strip()
            return n

        stop_norm = [norm(name) for name in stop_names]
        stop_norm = [s for s in stop_norm if s]
        if not stop_norm:
            return attractions

        filtered: list[dict[str, Any]] = []
        for attraction in attractions:
            attr_name = str(attraction.get("name", "") or "").strip()
            attr_norm = norm(attr_name)
            if not attr_norm:
                filtered.append(attraction)
                continue

            is_enroute_match = False
            for stop_name in stop_norm:
                if attr_norm == stop_name:
                    is_enroute_match = True
                    break
                if attr_norm in stop_name or stop_name in attr_norm:
                    is_enroute_match = True
                    break
                if SequenceMatcher(None, attr_norm, stop_name).ratio() >= 0.9:
                    is_enroute_match = True
                    break

            if not is_enroute_match:
                filtered.append(attraction)

        return filtered

    def _normalize_getting_here(self, getting_here: Any, dest_name: str) -> dict[str, Any]:
        if not isinstance(getting_here, dict):
            return {}
        out = dict(getting_here)
        normalized_stops: list[dict[str, Any]] = []
        for stop in out.get("en_route_stops", []) or []:
            if not isinstance(stop, dict):
                continue
            item = dict(stop)
            if item.get("detour_distance_miles") in (None, ""):
                item["detour_distance_miles"] = 0
            if item.get("detour_time_minutes") in (None, ""):
                item["detour_time_minutes"] = 0
            normalized_stops.append(item)
        out["en_route_stops"] = normalized_stops
        if not out.get("route_summary") and out.get("drive_time"):
            out["route_summary"] = f"Arrival leg into {dest_name} typically takes about {out.get('drive_time')}."
        return out

    def _normalize_environment(self, environment: Any, dates: str, dest: dict[str, Any]) -> Any:
        if not isinstance(environment, dict):
            return environment

        month = self._extract_month_index(dates)
        if month is None:
            return environment

        normals = self._get_monthly_temperature_normals(dest.get("lat"), dest.get("lng"), month)
        if not normals:
            return environment

        high_f, low_f = normals
        environment["temperature_high_f"] = high_f
        environment["temperature_low_f"] = low_f

        month_name = datetime(2000, month, 1).strftime("%B")
        grounded_sentence = f"Typical {month_name} temperatures are around {high_f}°F daytime and {low_f}°F overnight."
        summary = str(environment.get("summary", "") or "").strip()

        if summary:
            summary_without_temp = self._remove_temperature_claims(summary)
            environment["summary"] = (
                grounded_sentence if not summary_without_temp
                else f"{grounded_sentence} {summary_without_temp}"
            )
        else:
            environment["summary"] = grounded_sentence

        return environment

    def _get_monthly_temperature_normals(
        self,
        lat: Any,
        lng: Any,
        month: int,
    ) -> tuple[int, int] | None:
        try:
            lat_f = float(lat)
            lng_f = float(lng)
        except (TypeError, ValueError):
            return None

        key = (round(lat_f, 2), round(lng_f, 2), month)
        if key in self._weather_cache:
            return self._weather_cache[key]

        try:
            resp = requests.get(
                "https://archive-api.open-meteo.com/v1/archive",
                params={
                    "latitude": lat_f,
                    "longitude": lng_f,
                    "start_date": "2014-01-01",
                    "end_date": "2023-12-31",
                    "daily": "temperature_2m_max,temperature_2m_min",
                    "temperature_unit": "fahrenheit",
                    "timezone": "UTC",
                },
                timeout=20,
            )
            resp.raise_for_status()
            data = resp.json().get("daily", {})
            times = data.get("time", [])
            max_vals = data.get("temperature_2m_max", [])
            min_vals = data.get("temperature_2m_min", [])

            month_max: list[float] = []
            month_min: list[float] = []
            for day, max_v, min_v in zip(times, max_vals, min_vals):
                if not day or max_v is None or min_v is None:
                    continue
                if int(day[5:7]) != month:
                    continue
                month_max.append(float(max_v))
                month_min.append(float(min_v))

            if not month_max or not month_min:
                self._weather_cache[key] = None
                return None

            result = (round(sum(month_max) / len(month_max)), round(sum(month_min) / len(month_min)))
            self._weather_cache[key] = result
            return result
        except Exception as exc:
            logger.warning("Weather normals lookup failed for %.3f, %.3f month=%s: %s", lat_f, lng_f, month, exc)
            self._weather_cache[key] = None
            return None

    def _extract_month_index(self, dates: str) -> int | None:
        month_lookup = {
            "january": 1,
            "february": 2,
            "march": 3,
            "april": 4,
            "may": 5,
            "june": 6,
            "july": 7,
            "august": 8,
            "september": 9,
            "october": 10,
            "november": 11,
            "december": 12,
        }
        for token in re.findall(r"[A-Za-z]+", dates or ""):
            idx = month_lookup.get(token.lower())
            if idx:
                return idx
        return None

    def _remove_temperature_claims(self, text: str) -> str:
        sentences = re.split(r"(?<=[.!?])\s+", text)
        temp_pattern = re.compile(
            r"(°\s*[FC]|fahrenheit|celsius|temperature|temperatures|\bhighs?\b|\blows?\b|\b\d+\s*[-–]\s*\d+\s*°?F\b)",
            flags=re.IGNORECASE,
        )
        kept = [s.strip() for s in sentences if s.strip() and not temp_pattern.search(s)]
        return " ".join(kept).strip()

    def _normalize_attractions(self, attractions: list[dict[str, Any]]) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        must_see_budget = 2
        difficulty_rank = {"strenuous": 0, "moderate": 1, "easy": 2, "n/a": 3, "": 4}
        for attraction in attractions:
            item = dict(attraction)
            item_type = str(item.get("type", "attraction") or "attraction").lower()
            item["type"] = item_type
            if item.get("must_see") and must_see_budget > 0:
                must_see_budget -= 1
                item["must_see"] = True
            else:
                item["must_see"] = False
            normalized.append(item)

        normalized = self._dedupe_attractions(normalized)

        # Keep genuinely highlighted items first, then order by challenge level.
        normalized.sort(
            key=lambda x: (
                0 if x.get("must_see") else 1,
                difficulty_rank.get(str(x.get("difficulty", "")).lower(), 4),
                str(x.get("name", "")).lower(),
            )
        )
        return normalized

    def _dedupe_attractions(self, attractions: list[dict[str, Any]]) -> list[dict[str, Any]]:
        def canonical(name: str) -> str:
            n = (name or "").lower()
            n = n.replace("kolb", "kolob")
            n = re.sub(r"\b(road|rd|trail|hike|loop|route|area|point)\b", " ", n)
            n = re.sub(r"\s+", " ", n).strip()
            return n

        deduped: list[dict[str, Any]] = []
        for item in attractions:
            name = str(item.get("name", "") or "")
            key = canonical(name)
            merged = False
            for existing in deduped:
                existing_key = canonical(str(existing.get("name", "") or ""))
                if not key or not existing_key:
                    continue
                sim = SequenceMatcher(None, key, existing_key).ratio()
                if key == existing_key or sim >= 0.92:
                    if len(str(item.get("description", ""))) > len(str(existing.get("description", ""))):
                        existing["description"] = item.get("description", "")
                    if item.get("must_see"):
                        existing["must_see"] = True
                    if not existing.get("duration") and item.get("duration"):
                        existing["duration"] = item.get("duration")
                    if not existing.get("practical_note") and item.get("practical_note"):
                        existing["practical_note"] = item.get("practical_note")
                    merged = True
                    break
            if not merged:
                deduped.append(item)
        return deduped

    def _normalize_schedule(
        self,
        schedule: Any,
        restaurants: list[dict[str, Any]],
        dates: str,
        getting_here: dict[str, Any],
        previous_destination: str,
        next_destination: str,
    ) -> list[dict[str, Any]]:
        restaurant_names = [r.get("name", "") for r in restaurants if r.get("name")]

        def clean_text(text: str) -> str:
            cleaned = str(text)
            cleaned = re.sub(r"^\s*[🌅☀️🌙🗺️]\s*", "", cleaned)
            cleaned = re.sub(r"^\s*(morning|afternoon|evening|plan)\s*:\s*", "", cleaned, flags=re.IGNORECASE)
            cleaned = re.sub(r"^\s*\d{1,2}:\d{2}\s*(?:am|pm)?\s*[—-]\s*", "", cleaned, flags=re.IGNORECASE)
            cleaned = re.sub(r"\s+", " ", cleaned).strip()
            if not cleaned:
                return ""
            if "dinner" in cleaned.lower() and restaurant_names:
                mentions_restaurant = any(name.lower() in cleaned.lower() for name in restaurant_names)
                if not mentions_restaurant:
                    cleaned = re.sub(
                        r"dinner[^.]*",
                        f"dinner at {restaurant_names[0]}",
                        cleaned,
                        count=1,
                        flags=re.IGNORECASE,
                    )
            return cleaned

        if isinstance(schedule, list):
            normalized_days: list[dict[str, Any]] = []
            for index, day in enumerate(schedule, start=1):
                if isinstance(day, dict) and day.get("periods"):
                    periods = []
                    for period in day.get("periods", []):
                        label = str(period.get("period", "")).title()
                        summary = clean_text(period.get("summary", ""))
                        if label and summary:
                            periods.append({"period": label, "summary": summary})
                    if periods:
                        normalized_days.append({
                            "day_label": day.get("day_label") or f"Day {index}",
                            "periods": periods,
                        })
                elif isinstance(day, str):
                    normalized_days.append({
                        "day_label": f"Day {index}",
                        "periods": [{"period": "Plan", "summary": clean_text(day)}],
                    })
            day_count = self._infer_day_count(dates)
            normalized_days = self._expand_days(normalized_days, day_count)
            return self._inject_travel_realism(normalized_days, getting_here, previous_destination, next_destination)

        if isinstance(schedule, dict):
            periods = []
            for key in ["morning", "afternoon", "evening"]:
                value = clean_text(schedule.get(key, ""))
                if value:
                    periods.append({"period": key.title(), "summary": value})
            if periods:
                expanded = self._expand_days([{"day_label": "Day 1", "periods": periods}], self._infer_day_count(dates))
                return self._inject_travel_realism(expanded, getting_here, previous_destination, next_destination)

        return []

    def _inject_travel_realism(
        self,
        days: list[dict[str, Any]],
        getting_here: dict[str, Any],
        previous_destination: str,
        next_destination: str,
    ) -> list[dict[str, Any]]:
        if not days:
            return days

        # For single-day stops, avoid duplicating route language already shown in Getting Here.
        if len(days) <= 1:
            return days

        drive_time = str(getting_here.get("drive_time", "") or "").strip()
        first = days[0]
        first_periods = first.get("periods", [])
        if first_periods and (drive_time or previous_destination.lower() != "none"):
            existing = str(first_periods[0].get("summary", "") or "")
            if re.search(r"\b(arrive|arrival|drive|driving|route|i-\d+|us-\d+)\b", existing, re.IGNORECASE):
                arrival_note = ""
            else:
                arrival_note = "Arrival day"
            if arrival_note and drive_time:
                prefix = f"{arrival_note}: allow about {drive_time} of inbound driving"
            elif arrival_note:
                prefix = arrival_note
            elif drive_time:
                prefix = f"Allow about {drive_time} of inbound driving"
            else:
                prefix = ""
            if prefix:
                first_periods[0]["summary"] = f"{prefix}. {existing}".strip()

        if len(days) > 1 and next_destination:
            last = days[-1]
            last_periods = last.get("periods", [])
            if last_periods:
                last_periods[-1]["summary"] = (
                    f"Wrap key stops early and prepare for onward drive to {next_destination}. "
                    f"{last_periods[-1].get('summary', '')}"
                ).strip()
        return days

    def _infer_day_count(self, dates: str) -> int:
        text = (dates or "").replace("–", "-")
        # "October 17-21, 2026" or "October 17, 2026"
        m = re.search(r"[A-Za-z]+\s+(\d{1,2})(?:\s*-\s*(\d{1,2}))?(?:,\s*\d{4})?", text)
        if m:
            start = int(m.group(1))
            end = int(m.group(2) or m.group(1))
            if end >= start:
                return max(1, min(5, end - start + 1))
            return 1
        # ISO range fallback: 2026-10-17 to 2026-10-21
        iso = re.findall(r"(\d{4}-\d{2}-\d{2})", text)
        if len(iso) >= 2:
            try:
                from datetime import datetime as _dt
                d0 = _dt.strptime(iso[0], "%Y-%m-%d")
                d1 = _dt.strptime(iso[1], "%Y-%m-%d")
                if d1 >= d0:
                    return max(1, min(5, (d1 - d0).days + 1))
            except ValueError:
                return 1
        return 1

    def _expand_days(self, days: list[dict[str, Any]], day_count: int) -> list[dict[str, Any]]:
        if not days:
            return days
        if day_count <= 1:
            return days[:1]
        if len(days) > day_count:
            return days[:day_count]
        if day_count <= 1:
            return days
        if len(days) >= day_count:
            return days

        base_periods = days[0].get("periods", [])
        if not base_periods:
            return days

        expanded = []
        for idx in range(day_count):
            if idx < len(days):
                expanded.append(days[idx])
                continue
            # Spread existing period ideas across additional days with lightweight variation.
            period_template = base_periods[idx % len(base_periods)]
            expanded.append({
                "day_label": f"Day {idx + 1}",
                "periods": [{
                    "period": period_template.get("period", "Plan"),
                    "summary": period_template.get("summary", "Continue with priority attractions and logistics.")
                }],
            })
        return expanded

    def _normalize_restaurants(self, restaurants: list[dict[str, Any]], budget: Any = None) -> list[dict[str, Any]]:
        normalized = []
        price_rank = {"$": 0, "$$": 1, "$$$": 2, "$$$$": 3}
        budget_text = str(budget or "").lower()
        low_budget = any(k in budget_text for k in ["budget", "cheap", "economy", "value", "frugal"])
        high_budget = any(k in budget_text for k in ["luxury", "premium", "high", "splurge", "upscale"])

        for restaurant in restaurants:
            item = dict(restaurant)
            item["price_range"] = item.get("price_range") or item.get("price") or ""
            tier = str(item.get("price_range", "")).strip()

            if low_budget and tier in {"$$$", "$$$$"}:
                # Keep at most one splurge option for low-budget trips.
                if any(str(r.get("price_range", "")).strip() in {"$$$", "$$$$"} for r in normalized):
                    continue

            if high_budget and tier in {"$", "$$"}:
                # Keep at most one casual option for high-budget trips.
                if any(str(r.get("price_range", "")).strip() in {"$", "$$"} for r in normalized):
                    continue

            normalized.append(item)

        # Sort from inexpensive to expensive and keep cuisine variety visible.
        normalized.sort(
            key=lambda r: (
                price_rank.get(str(r.get("price_range", "")).strip(), 99),
                str(r.get("cuisine", "")).lower(),
                str(r.get("name", "")).lower(),
            )
        )
        return normalized

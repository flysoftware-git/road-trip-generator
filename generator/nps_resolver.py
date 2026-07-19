"""
nps_resolver.py — Detect NPS parks and resolve park codes via the NPS API.
"""
from __future__ import annotations
import logging, os, re
from typing import Any
import requests

logger = logging.getLogger(__name__)
NPS_API_BASE = "https://developer.nps.gov/api/v1"
NPS_KEYWORDS = [
    "national park", "national monument", "national recreation area",
    "national seashore", "national lakeshore", "national preserve",
    "national memorial", "national historic", "national battlefield",
]


class NPSResolver:
    
    def __init__(self) -> None:
        self.api_key = os.environ.get("NPS_API_KEY", "DEMO_KEY")
        self.session = requests.Session()
        self.session.headers.update({"X-Api-Key": self.api_key})

    def enrich(self, trip: dict[str, Any]) -> None:
        for dest in trip.get("destinations", []):
            if self._looks_like_nps(dest["name"]):
                code = self._resolve_park_code(dest["name"])
                if code:
                    dest["nps_park_code"] = code
                    logger.info("NPS park '%s' → code '%s'", dest["name"], code)
                else:
                    logger.warning("Could not resolve NPS park code for '%s'", dest["name"])

    def _looks_like_nps(self, name: str) -> bool:
        lower = name.lower()
        return any(kw in lower for kw in NPS_KEYWORDS)

    def _resolve_park_code(self, name: str) -> str | None:
        query = re.sub(
            "|".join(NPS_KEYWORDS), "", name, flags=re.IGNORECASE
        ).strip()[:40]
        try:
            resp = self.session.get(
                f"{NPS_API_BASE}/parks",
                params={"q": query, "limit": 5},
                timeout=10,
            )
            resp.raise_for_status()
            parks = resp.json().get("data", [])
            if parks:
                return parks[0].get("parkCode")
        except requests.RequestException as exc:
            logger.warning("NPS API error for '%s': %s", name, exc)
        return None

    def resolve(self, name: str) -> str | None:
        """Public wrapper for park code resolution."""
        if self._looks_like_nps(name):
            return self._resolve_park_code(name)
        return None

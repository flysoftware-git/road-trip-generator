"""
geocoder.py — Geocode destination names to lat/lng using Nominatim.
"""
from __future__ import annotations
import logging
import time
from typing import Any
from geopy.geocoders import Nominatim
from geopy.exc import GeocoderServiceError, GeocoderTimedOut

logger = logging.getLogger(__name__)


class Geocoder:
    def __init__(self, user_agent: str = "RoadTripItineraryGenerator/1.0", timeout: int = 5) -> None:
        self.geolocator = Nominatim(user_agent=user_agent, timeout=timeout)

    def enrich(self, trip: dict[str, Any]) -> None:
        """Geocode all destinations and attach lat/lng in-place."""
        for dest in trip.get("destinations", []):
            lat, lng = self._geocode(dest["name"])
            dest["lat"] = lat
            dest["lng"] = lng
            logger.info("Geocoded '%s' → %.4f, %.4f", dest["name"], lat, lng)
            time.sleep(1.1)  # Nominatim rate limit: 1 req/sec

    def _geocode(self, name: str, retries: int = 2) -> tuple[float, float]:
        for attempt in range(retries + 1):
            try:
                location = self.geolocator.geocode(name)
                if location:
                    return location.latitude, location.longitude
                raise ValueError(f"Nominatim returned no results for: '{name}'")
            except (GeocoderTimedOut, GeocoderServiceError) as exc:
                if attempt == retries:
                    raise
                logger.warning("Geocoder retry %d for '%s': %s", attempt + 1, name, exc)
                time.sleep(2)
        raise ValueError(f"Geocoding failed for: '{name}'")

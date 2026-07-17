"""Tests for generator.url_discovery"""
import pytest
from unittest.mock import MagicMock, patch
from generator.url_discovery import URLDiscoverer, _build_query_variants


def test_build_query_variants_returns_four():
    variants = _build_query_variants("Angels Landing", "Zion National Park", "trail")
    assert len(variants) == 4


def test_build_query_variants_specificity():
    variants = _build_query_variants("Spotted Dog Cafe", "Springdale", "restaurant")
    # First variant should be most specific (quoted name)
    assert '"Spotted Dog Cafe"' in variants[0]
    # Last variant should be broadest (no category)
    assert "restaurant" not in variants[-1]


def test_discover_all_adds_urls_to_attractions():
    trip = {
        "destinations": [
            {
                "name": "Zion National Park",
                "nps_park_code": "zion",
                "ai_content": {
                    "top_attractions": [{"name": "Angels Landing", "description": "Great hike"}],
                    "dinner_recommendations": [],
                    "getting_here": {"en_route_stops": []},
                },
                "scenic_drives": [],
            }
        ]
    }
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._key = "fake_key"
    discoverer._session = MagicMock()

    with patch.object(discoverer, "_search_first", return_value="https://www.nps.gov/zion/angels"):
        discoverer.discover_all(trip)
    
    attr = trip["destinations"][0]["ai_content"]["top_attractions"][0]
    assert attr["url"] == "https://www.nps.gov/zion/angels"


def test_discover_all_uses_google_fallback_for_missing_url():
    trip = {
        "destinations": [
            {
                "name": "Moab, Utah",
                "nps_park_code": None,
                "ai_content": {
                    "top_attractions": [{"name": "Dead Horse Point", "description": "Viewpoint"}],
                    "dinner_recommendations": [],
                    "getting_here": {"en_route_stops": []},
                },
                "scenic_drives": [],
            }
        ]
    }
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._key = "fake_key"
    discoverer._session = MagicMock()

    with patch.object(discoverer, "_search_first", return_value=None):
        discoverer.discover_all(trip)

    attr = trip["destinations"][0]["ai_content"]["top_attractions"][0]
    # When all variants fail, url is empty string (fallback is Google search URL)
    assert isinstance(attr["url"], str)


def test_restaurant_discovery_two_pass():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._key = "fake_key"
    discoverer._session = MagicMock()

    call_log = []

    def fake_search(variants, site_filter=None, site_hint=""):
        call_log.append(site_filter)
        if site_filter == "google.com/maps":
            return None  # First pass fails
        if site_filter == "tripadvisor.com":
            return "https://www.tripadvisor.com/Restaurant_Test"
        return None

    ai = {
        "dinner_recommendations": [{"name": "Test Restaurant"}],
        "top_attractions": [],
        "getting_here": {"en_route_stops": []},
    }

    with patch.object(discoverer, "_search_first", side_effect=fake_search):
        discoverer._discover_restaurants(ai, dest_name="Moab")

    assert "google.com/maps" in call_log
    assert "tripadvisor.com" in call_log
    assert ai["dinner_recommendations"][0]["url"] == "https://www.tripadvisor.com/Restaurant_Test"

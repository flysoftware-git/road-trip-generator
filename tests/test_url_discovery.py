"""Tests for generator.url_discovery"""
import json
import re
import threading
import time
import pytest
from unittest.mock import MagicMock, patch
from threading import Lock
from generator.url_discovery import (
    DEFAULT_EN_ROUTE_DETOUR_MAX_MILES,
    DEFAULT_EN_ROUTE_DETOUR_MAX_MINUTES,
    URLDiscoverer,
    _build_query_variants,
)


def test_build_query_variants_returns_four():
    variants = _build_query_variants("Angels Landing", "Zion National Park", "trail")
    assert len(variants) == 4


def test_build_query_variants_specificity():
    variants = _build_query_variants("Spotted Dog Cafe", "Springdale", "restaurant")
    # First variant should be most specific (quoted name)
    assert '"Spotted Dog Cafe"' in variants[0]
    # Last variant should be broadest (no category)
    assert "restaurant" not in variants[-1]


def test_build_query_variants_compacts_overly_long_categories():
    variants = _build_query_variants(
        "Piedra Falls",
        "Pagosa Springs",
        "trail hike attraction official site",
    )
    assert len(variants) == 4
    # Category is intentionally compacted to avoid over-constraining search.
    assert "official" not in variants[0].lower()
    assert "site" not in variants[0].lower()
    assert "trail" in variants[0].lower()
    assert "hike" in variants[0].lower()


def test_is_obviously_generic_url_rejects_nps_news_article():
    """Real dipstick67 regression: the general-search recovery path
    (authoritative_no_match_recovered_via_general_search) accepted a live,
    on-domain nps.gov page for 'Chimney Rock' that was actually a news post
    about a missing-hiker search-and-rescue effort -- a real page, on the
    right domain, that happens to mention Capitol Reef, but never a stable
    entity-specific reference page. Page TYPE (a dated news/incident
    article) must be rejected regardless of token overlap."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    real_url = (
        "https://www.nps.gov/care/learn/news/"
        "capitol-reef-national-park-seeks-public-assistance-in-locating-missing-hiker.htm"
    )
    assert discoverer._is_obviously_generic_url(real_url.lower()) is True


def test_is_obviously_generic_url_still_allows_real_planyourvisit_page():
    """Guard against over-rejection: a real nps.gov attraction detail page
    under /planyourvisit/<topic> must remain allowed (existing behavior)."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    real_url = "https://www.nps.gov/care/planyourvisit/chimneyrock.htm"
    assert discoverer._is_obviously_generic_url(real_url.lower()) is False


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


def _empty_dest(dest_id: str, name: str, lat: float, lng: float, group_with: str | None = None) -> dict:
    dest: dict = {
        "id": dest_id,
        "name": name,
        "lat": lat,
        "lng": lng,
        "nps_park_code": None,
        "ai_content": {
            "top_attractions": [],
            "dinner_recommendations": [],
            "getting_here": {"en_route_stops": []},
        },
        "scenic_drives": [],
    }
    if group_with:
        dest["group_with"] = group_with
    return dest


def test_discover_all_group_origin_resolution_uses_shared_base_not_previous_sibling():
    """GH #68 multi-site grouping §4: a grouped entry's origin must be its
    group base (a day-trip/detour), and the next ungrouped destination
    after a group must also measure from that shared base -- never from
    whichever grouped sibling happened to render last."""
    trip = {
        "destinations": [
            _empty_dest("moab", "Moab", 38.5733, -109.5498),
            _empty_dest("arches", "Arches National Park", 38.7331, -109.5925, group_with="moab"),
            _empty_dest("canyonlands", "Canyonlands National Park", 38.2, -109.93, group_with="moab"),
            _empty_dest("springdale", "Springdale", 37.19, -112.98),
        ]
    }
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._key = "fake_key"
    discoverer._session = MagicMock()
    discoverer._route_distance_live_fetch_enabled = False

    captured_origins: dict[str, str] = {}
    real_en_route = URLDiscoverer._discover_en_route_stops

    def spying_en_route_stops(self, ai, dest_name, dest_dates=None, origin_name="", origin_lat=None,
                               origin_lng=None, dest_lat=None, dest_lng=None, dest=None):
        captured_origins[dest_name] = origin_name
        return real_en_route(
            self, ai, dest_name, dest_dates, origin_name, origin_lat, origin_lng, dest_lat, dest_lng, dest,
        )

    with patch.object(discoverer, "_search_first", return_value=None):
        with patch.object(URLDiscoverer, "_discover_en_route_stops", spying_en_route_stops):
            discoverer.discover_all(trip)

    assert captured_origins["Moab"] == ""  # trip's first destination -- unchanged legacy behavior
    assert captured_origins["Arches National Park"] == "Moab"
    assert captured_origins["Canyonlands National Park"] == "Moab"
    # Springdale is the next *ungrouped* destination after the group -- its
    # leg must be measured from Moab (the shared base), not Canyonlands
    # (whichever grouped sibling happens to be last in list order).
    assert captured_origins["Springdale"] == "Moab"


def test_discover_all_group_origin_resolves_by_id_regardless_of_list_order():
    """A grouped entry can legally appear before its base in the manifest;
    origin resolution must not depend on iteration order."""
    trip = {
        "destinations": [
            _empty_dest("arches", "Arches National Park", 38.7331, -109.5925, group_with="moab"),
            _empty_dest("moab", "Moab", 38.5733, -109.5498),
        ]
    }
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._key = "fake_key"
    discoverer._session = MagicMock()
    discoverer._route_distance_live_fetch_enabled = False

    captured_origins: dict[str, str] = {}

    def spying_en_route_stops(self, ai, dest_name, dest_dates=None, origin_name="", *args, **kwargs):
        captured_origins[dest_name] = origin_name

    with patch.object(discoverer, "_search_first", return_value=None):
        with patch.object(URLDiscoverer, "_discover_en_route_stops", spying_en_route_stops):
            discoverer.discover_all(trip)

    assert captured_origins["Arches National Park"] == "Moab"


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
    discoverer._restaurant_source = "search"
    discoverer._key = "fake_key"
    discoverer._session = MagicMock()

    call_log = []

    def fake_search(variants, site_filter=None, site_hint="", **_kwargs):
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


# ── GH #68 multi-site grouping: base_owned_categories discovery gate ────────


def test_discover_restaurants_skips_entirely_when_category_deferred_to_base():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._restaurant_source = "search"
    discoverer._multi_site_base_owned_categories = frozenset({"restaurant"})

    ai = {"dinner_recommendations": [{"name": "Arches Cafe"}]}
    grouped_dest = {"id": "arches", "name": "Arches National Park", "group_with": "moab"}

    with patch.object(discoverer, "_search_first") as fake_search:
        discoverer._discover_restaurants(ai, dest_name="Arches National Park", dest=grouped_dest)

    fake_search.assert_not_called()
    # Cleared (not left as dead-link placeholders) so html_assembler.py can
    # render a clean "see base" pointer instead of urlless restaurant rows.
    assert ai["dinner_recommendations"] == []


def test_discover_restaurants_runs_normally_for_ungrouped_entry_even_with_config_default():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._restaurant_source = "search"
    discoverer._multi_site_base_owned_categories = frozenset({"restaurant"})

    ai = {"dinner_recommendations": [{"name": "Moab Diner"}]}
    base_dest = {"id": "moab"}  # no group_with -- this IS the base

    with patch.object(discoverer, "_search_first", return_value="https://example.com/moab-diner"):
        discoverer._discover_restaurants(ai, dest_name="Moab", dest=base_dest)

    assert ai["dinner_recommendations"][0]["url"] == "https://example.com/moab-diner"


def test_discover_restaurants_runs_normally_when_entry_opts_out_via_empty_override():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._restaurant_source = "search"
    discoverer._multi_site_base_owned_categories = frozenset({"restaurant"})

    ai = {"dinner_recommendations": [{"name": "Canyonlands Grill"}]}
    grouped_dest_opted_out = {"id": "canyonlands", "group_with": "moab", "base_owned_categories": []}

    with patch.object(discoverer, "_search_first", return_value="https://example.com/canyonlands-grill"):
        discoverer._discover_restaurants(ai, dest_name="Canyonlands National Park", dest=grouped_dest_opted_out)

    assert ai["dinner_recommendations"][0]["url"] == "https://example.com/canyonlands-grill"


def test_discover_restaurants_honors_per_entry_override_beyond_config_default():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._restaurant_source = "search"
    discoverer._multi_site_base_owned_categories = frozenset()  # config default: nothing deferred

    ai = {"dinner_recommendations": [{"name": "Arches Cafe"}]}
    grouped_dest = {"id": "arches", "group_with": "moab", "base_owned_categories": ["restaurant"]}

    with patch.object(discoverer, "_search_first") as fake_search:
        discoverer._discover_restaurants(ai, dest_name="Arches National Park", dest=grouped_dest)

    fake_search.assert_not_called()


def test_discover_en_route_stops_skips_stops_but_still_updates_route_distance_when_deferred():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._en_route_source = "search"
    discoverer._multi_site_base_owned_categories = frozenset({"en_route_stop"})
    discoverer._route_distance_live_fetch_enabled = False

    ai = {"getting_here": {"en_route_stops": [{"name": "Wilson Arch"}]}}
    grouped_dest = {"id": "arches", "name": "Arches National Park", "group_with": "moab"}

    with patch.object(discoverer, "_search_first") as fake_search:
        discoverer._discover_en_route_stops(
            ai, "Arches National Park", origin_name="Moab",
            origin_lat=38.5733, origin_lng=-109.5498,
            dest_lat=38.7331, dest_lng=-109.5925,
            dest=grouped_dest,
        )

    fake_search.assert_not_called()
    assert ai["getting_here"]["en_route_stops"] == []
    # Distance/time still gets computed from the real lat/lng pair -- this
    # category gate must never silently disable route-distance math.
    assert ai["getting_here"].get("distance_miles")


def test_ensure_en_route_seed_candidates_adds_missing_seed():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    dest = {"id": "pagosa_springs", "en_route_seeds": ["Enchanted Circle Scenic Drive"]}

    result = discoverer._ensure_en_route_seed_candidates([], dest, "Pagosa Springs")

    assert len(result) == 1
    assert result[0]["name"] == "Enchanted Circle Scenic Drive"
    assert result[0]["is_seed"] is True


def test_ensure_en_route_seed_candidates_dedupes_against_existing_stop():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    dest = {"id": "pagosa_springs", "en_route_seeds": ["Wolf Creek Pass Overlook"]}
    existing = [{"name": "Wolf Creek Pass Overlook", "description": "Already proposed by the AI."}]

    result = discoverer._ensure_en_route_seed_candidates(existing, dest, "Pagosa Springs")

    assert len(result) == 1
    assert result[0]["description"] == "Already proposed by the AI."


def test_ensure_en_route_seed_candidates_no_seeds_returns_same_list():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    stops = [{"name": "Some AI Stop"}]

    result = discoverer._ensure_en_route_seed_candidates(stops, {"id": "x"}, "X")

    assert result is stops


def test_discover_en_route_stops_includes_manifest_seed_as_search_candidate():
    """A traveler-supplied `en_route_seeds` name hint (manifest_parser.py) must
    surface as an en-route-stop candidate for the leg arriving at this
    destination, and go through the normal search/link-resolution path just
    like any AI-proposed stop -- not an unconditional include."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._en_route_source = "search"
    discoverer._route_distance_live_fetch_enabled = False

    ai = {"getting_here": {"en_route_stops": []}}
    dest = {"id": "pagosa_springs", "en_route_seeds": ["Enchanted Circle Scenic Drive"]}

    with patch.object(
        discoverer,
        "_search_first",
        return_value="https://www.example.com/enchanted-circle-scenic-drive",
    ) as fake_search:
        discoverer._discover_en_route_stops(
            ai, "Pagosa Springs", origin_name="Taos", dest=dest,
        )

    stops = ai["getting_here"]["en_route_stops"]
    names = [s.get("name") for s in stops]
    assert "Enchanted Circle Scenic Drive" in names
    seeded = next(s for s in stops if s.get("name") == "Enchanted Circle Scenic Drive")
    assert seeded["url"] == "https://www.example.com/enchanted-circle-scenic-drive"
    fake_search.assert_called()


def test_discover_en_route_stops_seed_survives_missing_detour_metadata_threshold():
    """Regression for dipstick60: Pagosa Springs' en_route_seeds
    ["Enchanted Circle Scenic Byway"] correctly logged
    'en_route_seed_injected', but in the direct_link_batch source mode the
    seed was then immediately dropped by the pre-existing
    en_route_threshold_filtered (missing_detour_metadata) filter before it
    ever reached real geocoding/route-proximity verification --
    "Enchanted Circle Scenic Byway" appeared zero times in the final HTML.
    A manifest-seeded en-route candidate is the traveler's own explicit
    pick and must not need pre-existing detour distance/time metadata to
    survive this filter, mirroring the existing seed_threshold_override
    precedent already used for the max_trail_miles threshold."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._en_route_source = "direct_link_batch"
    discoverer._en_route_require_detour_metadata = True

    ai = {"getting_here": {"en_route_stops": []}}
    dest = {"id": "pagosa_springs", "en_route_seeds": ["Enchanted Circle Scenic Byway"]}

    with patch.object(discoverer, "_get_en_route_direct_batch_rows_for_destination", return_value=[]):
        with patch.object(discoverer, "_search_en_route_stop_from_direct_batch", return_value=None):
            with patch.object(
                discoverer,
                "_search_first",
                return_value="https://www.example.com/enchanted-circle-scenic-byway",
            ):
                with patch.object(discoverer, "_geocode_en_route_stop_for_route", return_value=None):
                    discoverer._discover_en_route_stops(
                        ai, "Pagosa Springs", origin_name="Taos", dest=dest,
                    )

    stops = ai["getting_here"]["en_route_stops"]
    names = [str(s.get("name", "") or "") for s in stops]
    assert "Enchanted Circle Scenic Byway" in names


def test_discover_en_route_stops_forwards_en_route_seeds_to_direct_batch_prioritize():
    """In direct_link_batch mode, _discover_en_route_stops must forward this
    destination's manifest en_route_seeds into
    _prioritize_direct_batch_en_route_stops (which in turn threads them into
    the harvest prompt via _get_en_route_direct_batch_rows_for_destination) --
    the harvest-prompt-recall fix's en-route-stop counterpart to the seeds ->
    attraction/trail harvest wiring."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._en_route_source = "direct_link_batch"

    ai = {"getting_here": {"en_route_stops": []}}
    dest = {"id": "zion", "en_route_seeds": ["Kolob Canyons Viewpoint"]}

    with patch.object(
        discoverer, "_prioritize_direct_batch_en_route_stops", return_value=[]
    ) as mock_prioritize:
        discoverer._discover_en_route_stops(
            ai, "Zion National Park", origin_name="St. George", dest=dest,
        )

    mock_prioritize.assert_called_once_with(
        [], "Zion National Park", None, "St. George", seed_names=["Kolob Canyons Viewpoint"]
    )


def test_en_route_stop_within_threshold_seed_override_still_enforces_hard_caps():
    """The seed override in _en_route_stop_within_threshold relaxes only the
    missing-metadata requirement -- a seed with real detour metadata that
    exceeds a configured hard cap (max minutes/miles) must still be
    filtered, exactly like a non-seed candidate."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._en_route_require_detour_metadata = True
    discoverer._en_route_detour_max_minutes = 20
    discoverer._en_route_detour_max_miles = 0.0

    seed_missing_metadata = {"name": "Enchanted Circle Scenic Byway", "is_seed": True}
    keep, reason = discoverer._en_route_stop_within_threshold(seed_missing_metadata)
    assert keep is True
    assert reason == "seed_threshold_override"

    seed_over_cap = {"name": "Enchanted Circle Scenic Byway", "is_seed": True, "detour_time_minutes": 45}
    keep, reason = discoverer._en_route_stop_within_threshold(seed_over_cap)
    assert keep is False
    assert reason == "detour_minutes_exceeded"

    non_seed_missing_metadata = {"name": "Some AI Stop"}
    keep, reason = discoverer._en_route_stop_within_threshold(non_seed_missing_metadata)
    assert keep is False
    assert reason == "missing_detour_metadata"


def test_en_route_stop_within_threshold_uses_real_nonzero_mile_default():
    """Regression for the real published Telluride -> Pagosa leg, where
    DEFAULT_EN_ROUTE_DETOUR_MAX_MILES being 0.0 meant the miles cap could
    never reject anything: 'Dolores River Overlook' (49.4 mi), 'Durango &
    Silverton Narrow Gauge Railroad Depot' (87.3 mi), 'Animas River Trail'
    (93.2 mi), and 'Mancos State Park' (112.9 mi) were all published as
    "can't-miss" en-route stops despite having only a mined
    detour_distance_miles (no detour_time_minutes text), so the inert
    miles-only cap was their sole distance gate. This exercises the class
    default directly (no per-instance override), matching how a real
    URLDiscoverer is constructed."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._en_route_require_detour_metadata = True
    discoverer._en_route_detour_max_minutes = DEFAULT_EN_ROUTE_DETOUR_MAX_MINUTES
    discoverer._en_route_detour_max_miles = DEFAULT_EN_ROUTE_DETOUR_MAX_MILES

    assert DEFAULT_EN_ROUTE_DETOUR_MAX_MILES > 0

    for name, miles in (
        ("Dolores River Overlook", 49.4),
        ("Durango & Silverton Narrow Gauge Railroad Depot", 87.3),
        ("Animas River Trail", 93.2),
        ("Mancos State Park", 112.9),
    ):
        keep, reason = discoverer._en_route_stop_within_threshold(
            {"name": name, "detour_distance_miles": miles}
        )
        assert keep is False, f"{name} ({miles} mi) should have been rejected"
        assert reason == "detour_miles_exceeded"

    # A genuinely quick, worth-it detour must still survive the new default.
    keep, reason = discoverer._en_route_stop_within_threshold(
        {"name": "Roadside Overlook", "detour_distance_miles": 3.5}
    )
    assert keep is True
    assert reason == "ok"


def test_discover_en_route_stops_does_not_add_seed_from_a_different_destination():
    """en_route_seeds is scoped per destination to the leg arriving at that
    destination -- a seed on one destination's manifest entry must not leak
    into a sibling destination's en-route-stop candidates."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._en_route_source = "search"
    discoverer._route_distance_live_fetch_enabled = False

    ai = {"getting_here": {"en_route_stops": []}}
    # This destination ("pagosa_springs") intentionally has no en_route_seeds
    # of its own -- "Wilson Arch" belongs to a sibling manifest entry
    # (e.g. "moab") and must never leak in here.
    dest_without_seed = {"id": "pagosa_springs"}

    with patch.object(discoverer, "_search_first", return_value=""):
        discoverer._discover_en_route_stops(
            ai, "Pagosa Springs", origin_name="Taos", dest=dest_without_seed,
        )

    assert ai["getting_here"]["en_route_stops"] == []


def test_discover_scenic_drives_skips_and_clears_content_when_deferred():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._multi_site_base_owned_categories = frozenset({"scenic_drive"})

    grouped_dest = {
        "id": "arches",
        "name": "Arches National Park",
        "group_with": "moab",
        "scenic_drives": [{"title": "Arches Scenic Drive", "description": "AI-generated blurb."}],
    }

    with patch.object(discoverer, "_search_first") as fake_search:
        discoverer._discover_scenic_drives(grouped_dest, "Arches National Park", "arch")

    fake_search.assert_not_called()
    assert grouped_dest["scenic_drives"] == []


def test_discover_attractions_defers_trail_category_when_configured():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._multi_site_base_owned_categories = frozenset()

    ai = {
        "top_attractions": [
            {"name": "Delicate Arch Trail", "type": "hike", "description": "Iconic hike."},
        ]
    }
    grouped_dest = {"id": "arches", "group_with": "moab", "base_owned_categories": ["trail"]}

    with patch.object(discoverer, "_search_first") as fake_search:
        discoverer._discover_attractions(ai, "Arches National Park", "arch", dest=grouped_dest)

    fake_search.assert_not_called()
    assert ai["top_attractions"][0]["url"] == ""


def test_restaurant_discovery_uses_ai_url_candidates_before_search_passes():
    """AI-provided url_candidates resolve without the multi-pass maps/tripadvisor
    _search_first fallback -- but TripAdvisor is still eligible for the
    separate, intentional official-site upgrade attempt (see
    _maybe_upgrade_tripadvisor_restaurant_link), so exactly one _search_first
    call for that purpose is expected, not zero."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._restaurant_source = "search"

    ai = {
        "dinner_recommendations": [
            {
                "name": "The Spotted Dog Cafe",
                "url_candidates": [
                    "https://www.tripadvisor.com/Restaurant_Review-g57119-d123456-Reviews-The_Spotted_Dog_Cafe-Springdale_Utah.html"
                ],
            }
        ],
        "top_attractions": [],
        "getting_here": {"en_route_stops": []},
    }

    with patch.object(discoverer, "_search_first", return_value=None) as mock_search:
        with patch.object(discoverer, "_retain_discovered_url", side_effect=lambda url, *_args, **_kwargs: url):
            discoverer._discover_restaurants(ai, dest_name="Zion National Park")

    assert "tripadvisor.com" in ai["dinner_recommendations"][0]["url"]
    mock_search.assert_called_once()


def test_maybe_upgrade_tripadvisor_restaurant_link_prefers_found_official_site() -> None:
    """Real reported feedback: TripAdvisor links are frequently generic, not
    targeted to the actual restaurant. A found, non-aggregator official site
    must replace the TripAdvisor link."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._restaurant_prefer_official_site_over_tripadvisor = True
    rest = {
        "name": "Benja Thai & Sushi",
        "url": "https://www.tripadvisor.com/Restaurant_Review-g57112-d456789-Benja_Thai_Sushi-St_George_Utah.html",
        "maps_url": "https://www.google.com/maps/search/?api=1&query=Benja+Thai",
    }

    with patch.object(discoverer, "_search_first", return_value="https://www.benjathaistgeorge.com/"):
        with patch.object(discoverer, "_retain_discovered_url", side_effect=lambda url, *_a, **_k: url):
            discoverer._maybe_upgrade_tripadvisor_restaurant_link(rest, "St. George, Utah")

    assert rest["url"] == "https://www.benjathaistgeorge.com/"
    assert "maps_url" not in rest


def test_maybe_upgrade_tripadvisor_restaurant_link_keeps_tripadvisor_when_no_official_site_found() -> None:
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._restaurant_prefer_official_site_over_tripadvisor = True
    rest = {
        "name": "Los Jilbertos Mexican Food",
        "url": "https://www.tripadvisor.com/Restaurant_Review-g57112-d789012-Los_Jilbertos-St_George_Utah.html",
    }

    with patch.object(discoverer, "_search_first", return_value=None):
        discoverer._maybe_upgrade_tripadvisor_restaurant_link(rest, "St. George, Utah")

    assert rest["url"] == "https://www.tripadvisor.com/Restaurant_Review-g57112-d789012-Los_Jilbertos-St_George_Utah.html"


def test_maybe_upgrade_tripadvisor_restaurant_link_rejects_another_aggregator_result() -> None:
    """The upgrade search must not swap TripAdvisor for a different aggregator
    (Yelp, Facebook, ...) -- only a genuine official/source domain counts."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._restaurant_prefer_official_site_over_tripadvisor = True
    rest = {
        "name": "Sakura Japanese Steakhouse",
        "url": "https://www.tripadvisor.com/Restaurant_Review-g57112-d345678-Sakura-Japanese-St_George_Utah.html",
    }

    with patch.object(discoverer, "_search_first", return_value="https://www.yelp.com/biz/sakura-st-george"):
        discoverer._maybe_upgrade_tripadvisor_restaurant_link(rest, "St. George, Utah")

    assert "tripadvisor.com" in rest["url"]


def test_maybe_upgrade_tripadvisor_restaurant_link_is_opt_out() -> None:
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._restaurant_prefer_official_site_over_tripadvisor = False
    rest = {
        "name": "Benja Thai & Sushi",
        "url": "https://www.tripadvisor.com/Restaurant_Review-g57112-d456789-Benja_Thai_Sushi-St_George_Utah.html",
    }

    with patch.object(
        discoverer, "_search_first", side_effect=AssertionError("must not search when opted out")
    ):
        discoverer._maybe_upgrade_tripadvisor_restaurant_link(rest, "St. George, Utah")

    assert "tripadvisor.com" in rest["url"]


def test_discover_restaurants_seeds_candidates_from_direct_batch_when_ai_list_empty():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._restaurant_source = "direct_link_batch"

    ai = {
        "dinner_recommendations": [],
        "top_attractions": [],
        "getting_here": {"en_route_stops": []},
    }

    rows = [
        {"name": "Alley House Grille", "url": "https://www.tripadvisor.com/Restaurant_Review-..."},
        {"name": "Kip's Grill", "url": "https://www.tripadvisor.com/Restaurant_Review-..."},
    ]

    with patch.object(discoverer, "_get_restaurant_direct_batch_rows_for_destination", return_value=rows):
        with patch.object(discoverer, "_search_restaurant_from_direct_batch", return_value=None):
            with patch.object(discoverer, "_search_first", return_value=None):
                discoverer._discover_restaurants(ai, dest_name="Pagosa Springs")

    names = [str(item.get("name", "") or "") for item in ai.get("dinner_recommendations", [])]
    assert "Alley House Grille" in names
    assert "Kip's Grill" in names


def test_discover_restaurants_direct_batch_replaces_ai_list_even_when_nonempty():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._restaurant_source = "direct_link_batch"

    ai = {
        "dinner_recommendations": [
            {"name": "Pizza Factory"},
            {"name": "The Pasta Factory"},
        ],
        "top_attractions": [],
        "getting_here": {"en_route_stops": []},
    }
    rows = [
        {"name": "Painted Pony", "url": "https://www.painted-pony.com/"},
        {"name": "Wood Ash Rye", "url": "https://www.woodashrye.com/"},
    ]

    with patch.object(discoverer, "_get_restaurant_direct_batch_rows_for_destination", return_value=rows):
        with patch.object(discoverer, "_search_restaurant_from_direct_batch", side_effect=[rows[0]["url"], rows[1]["url"]]):
            with patch.object(discoverer, "_search_first") as fallback_search:
                discoverer._discover_restaurants(ai, dest_name="St. George, Utah")

    names = [str(item.get("name", "") or "") for item in ai["dinner_recommendations"]]
    assert names == ["Painted Pony", "Wood Ash Rye"]
    fallback_search.assert_not_called()


def test_discover_restaurants_direct_batch_backfills_metadata_from_similar_existing_name() -> None:
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._restaurant_source = "direct_link_batch"

    ai = {
        "dinner_recommendations": [
            {
                "name": "Morty's Cafe",
                "cuisine": "American",
                "price_range": "$$",
                "reserve_recommended": False,
            },
        ],
        "top_attractions": [],
        "getting_here": {"en_route_stops": []},
    }
    rows = [
        {
            "name": "Mortys Cafe",
            "url": "https://www.google.com/maps/place/Mortys+Cafe/",
            "description": "Local favorite diner.",
        },
    ]

    with patch.object(discoverer, "_get_restaurant_direct_batch_rows_for_destination", return_value=rows):
        with patch.object(discoverer, "_search_restaurant_from_direct_batch", return_value=rows[0]["url"]):
            discoverer._discover_restaurants(ai, dest_name="St. George, Utah")

    out = ai["dinner_recommendations"][0]
    assert out["name"] == "Mortys Cafe"
    assert out.get("cuisine", "") == "American"
    assert out.get("price_range", "") == "$$"


def test_alltrails_trail_url_requires_matching_page_content():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._url_validator = MagicMock()
    discoverer._url_validator.session.get.return_value = MagicMock(
        status_code=200,
        text="Navajo Loop Trail Bryce Canyon hike details and reviews",
    )

    url = "https://www.alltrails.com/trail/us/utah/navajo-loop-trail"
    assert discoverer._is_alltrails_trail_url(url)
    assert discoverer._is_relevant_result(url, "Navajo Loop Trail", "Bryce Canyon National Park")


def test_search_strict_accepts_live_alltrails_trail_with_matching_content():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._search = MagicMock()
    discoverer._url_validator = MagicMock()
    discoverer._url_validator.session.get.return_value = MagicMock(
        status_code=200,
        text="Navajo Loop Trail in Bryce Canyon National Park hiking guide",
    )
    discoverer._search.search.return_value = [
        {"url": "https://www.alltrails.com/trail/us/utah/navajo-loop-trail"}
    ]

    result = discoverer._search_first_strict(
        query_variants=['"Navajo Loop Trail" Bryce Canyon National Park trail hiking'],
        site_filter="alltrails.com",
        site_hint=None,
        item_name="Navajo Loop Trail",
        dest_name="Bryce Canyon National Park",
        allow_alltrails=True,
    )

    assert result == "https://www.alltrails.com/trail/us/utah/navajo-loop-trail"
    discoverer._url_validator.verify_url.assert_not_called()


def test_search_strict_rejects_alltrails_non_trail_paths_under_alltrails_filter():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._search = MagicMock()
    discoverer._url_validator = MagicMock()
    discoverer._search.search.return_value = [
        {"url": "https://www.alltrails.com/blog/angels-landing-zion"},
        {
            "url": "https://www.alltrails.com/trail/us/utah/angels-landing-trail",
            "name": "Angels Landing Trail in Zion National Park | AllTrails",
            "snippet": "2.0 mile out and back trail with reviews, maps, and route details.",
        },
    ]

    with patch.object(
        discoverer,
        "_fetch_page_text",
        return_value=(
            True,
            200,
            "Angels Landing Trail in Zion National Park. 2.0 mile out and back trail with route details and reviews.",
        ),
    ):
        result = discoverer._search_first_strict(
            query_variants=['"Angels Landing" Zion National Park trail hiking'],
            site_filter="alltrails.com",
            site_hint=None,
            item_name="Angel's Landing",
            dest_name="Zion National Park",
            allow_alltrails=True,
        )

    assert result == "https://www.alltrails.com/trail/us/utah/angels-landing-trail"


def test_search_strict_prefers_exact_alltrails_slug_over_via_variant():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._search = MagicMock()
    discoverer._url_validator = MagicMock()
    discoverer._url_validator.session.get.return_value = MagicMock(
        status_code=200,
        text="Angels Landing Trail in Zion with route details and reviews.",
    )
    discoverer._search.search.return_value = [
        {"url": "https://www.alltrails.com/trail/us/utah/angels-landing-via-west-rim-trail"},
        {"url": "https://www.alltrails.com/trail/us/utah/angels-landing-trail"},
    ]

    result = discoverer._search_first_strict(
        query_variants=['"Angels Landing" Zion National Park trail hiking'],
        site_filter="alltrails.com",
        site_hint=None,
        item_name="Angel's Landing",
        dest_name="Zion National Park",
        allow_alltrails=True,
    )

    assert result == "https://www.alltrails.com/trail/us/utah/angels-landing-trail"


def test_search_alltrails_for_trail_upgrades_via_variant_to_verified_canonical_slug():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)

    with patch.object(
        discoverer,
        "_search_first",
        return_value="https://www.alltrails.com/trail/us/utah/angels-landing-via-west-rim-trail",
    ):
        with patch.object(discoverer, "_fetch_page_text") as mock_fetch:
            def fake_fetch(url, timeout=8):
                if url.endswith("/angels-landing-trail"):
                    return True, 200, "Angels Landing Trail route details and reviews"
                return False, "timeout", ""

            mock_fetch.side_effect = fake_fetch
            result = discoverer._search_alltrails_for_trail("Angels Landing", "Zion National Park")

    assert result == "https://www.alltrails.com/trail/us/utah/angels-landing-trail"


def test_search_alltrails_for_trail_upgrades_destination_suffixed_slug_to_canonical():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)

    with patch.object(
        discoverer,
        "_search_first",
        return_value="https://www.alltrails.com/trail/us/utah/canyon-overlook-trail-zion-national-park",
    ):
        with patch.object(discoverer, "_fetch_page_text") as mock_fetch:
            def fake_fetch(url, timeout=8):
                if url.endswith("/canyon-overlook-trail"):
                    return True, 200, "Canyon Overlook Trail route details and reviews"
                return False, "timeout", ""

            mock_fetch.side_effect = fake_fetch
            result = discoverer._search_alltrails_for_trail("Canyon Overlook Trail", "Zion National Park")

    assert result == "https://www.alltrails.com/trail/us/utah/canyon-overlook-trail"


def test_prefer_canonical_alltrails_url_does_not_keep_404_slug_as_fallback():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)

    with patch.object(discoverer, "_fetch_page_text") as mock_fetch:
        def fake_fetch(url, timeout=8):
            if url.endswith("/the-narrows-trail"):
                return False, 404, ""
            # "the-narrows" also not reachable in this test
            return False, "timeout", ""

        mock_fetch.side_effect = fake_fetch
        result = discoverer._prefer_canonical_alltrails_url(
            "https://www.alltrails.com/trail/us/utah/the-narrows-top-down",
            "The Narrows Trail",
        )

    # Must not return the 404 slug; falls back to the original noisy URL instead
    assert result != "https://www.alltrails.com/trail/us/utah/the-narrows-trail"


def test_prefer_canonical_alltrails_url_does_not_promote_unverified_blocked_candidate():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)

    with patch.object(discoverer, "_fetch_page_text") as mock_fetch:
        def fake_fetch(url, timeout=8):
            if url.endswith("/bryce-point-trail"):
                return False, 403, ""
            return False, "timeout", ""

        mock_fetch.side_effect = fake_fetch
        result = discoverer._prefer_canonical_alltrails_url(
            "https://www.alltrails.com/trail/us/utah/bryce-point-via-scenic-loop",
            "Bryce Point Trail",
        )

    # A 403/blocked fetch never confirms the synthesized slug actually exists,
    # so it must not be promoted -- keep the original (search/harvest-sourced) URL.
    assert result == "https://www.alltrails.com/trail/us/utah/bryce-point-via-scenic-loop"


def test_prefer_canonical_alltrails_url_skips_fallback_when_verify_reports_404():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)

    with patch.object(discoverer, "_fetch_page_text", return_value=(False, 403, "")):
        with patch.object(discoverer, "_verify_url_cached", return_value=(False, 404)):
            result = discoverer._prefer_canonical_alltrails_url(
                "https://www.alltrails.com/trail/us/utah/the-narrows-top-down",
                "The Narrows",
            )

    assert result == "https://www.alltrails.com/trail/us/utah/the-narrows-top-down"


def test_search_alltrails_for_trail_upgrades_short_trail_slug_to_canonical_trail_page():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)

    with patch.object(
        discoverer,
        "_search_first",
        return_value="https://www.alltrails.com/trail/us/utah/bryce-point",
    ):
        with patch.object(discoverer, "_fetch_page_text") as mock_fetch:
            def fake_fetch(url, timeout=8):
                if url.endswith("/bryce-point-trail"):
                    return True, 200, "Bryce Point Trail route details and reviews"
                return False, "timeout", ""

            mock_fetch.side_effect = fake_fetch
            result = discoverer._search_alltrails_for_trail("Bryce Point Trail", "Bryce Canyon National Park")

    assert result == "https://www.alltrails.com/trail/us/utah/bryce-point-trail"


def test_search_alltrails_for_trail_strips_tracking_query_from_result_url():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)

    with patch.object(
        discoverer,
        "_search_first",
        return_value="https://www.alltrails.com/trail/us/utah/canyon-overlook-trail?u=i",
    ):
        with patch.object(discoverer, "_fetch_page_text", return_value=(False, "timeout", "")):
            result = discoverer._search_alltrails_for_trail("Canyon Overlook Trail", "Zion National Park")

    assert result == "https://www.alltrails.com/trail/us/utah/canyon-overlook-trail"


def test_search_alltrails_for_trail_direct_batch_source_selects_matching_slug():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._alltrails_source = "direct_link_batch"
    discoverer._disable_trails = False

    with patch.object(
        discoverer,
        "_get_alltrails_direct_batch_rows_for_destination",
        return_value=[
            {
                "url": "https://www.alltrails.com/trail/us/utah/riverside-walk",
                "name": "Riverside Walk Trail",
                "snippet": "Easy river walk in Zion National Park.",
            },
            {
                "url": "https://www.alltrails.com/trail/us/utah/the-narrows-trail",
                "name": "The Narrows Trail",
                "snippet": "Popular 2.0 mile route in Zion National Park.",
            },
        ],
    ):
        with patch.object(discoverer, "_prefer_canonical_alltrails_url", side_effect=lambda url, _item: url):
            result = discoverer._search_alltrails_for_trail("The Narrows", "Zion National Park")

    assert result == "https://www.alltrails.com/trail/us/utah/the-narrows-trail"


def test_passes_alltrails_post_search_filters_rejects_blocked_fetch_when_verify_reports_404() -> None:
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._enable_filtered_alltrails_selection = True

    with patch.object(discoverer, "_fetch_page_text", return_value=(False, 403, "")):
        with patch.object(discoverer, "_verify_url_cached", return_value=(False, 404)):
            ok = discoverer._passes_alltrails_post_search_filters(
                "https://www.alltrails.com/trail/us/utah/the-narrows-trail",
                "The Narrows",
                "Zion National Park",
            )

    assert ok is False


def test_search_alltrails_post_search_filters_reject_hard_trail_after_broad_fallback() -> None:
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._alltrails_source = "search"
    discoverer._disable_trails = False
    discoverer._enable_filtered_alltrails_selection = True
    discoverer._alltrails_filter_allowed_difficulties = ("easy", "moderate", "moderately challenging")
    discoverer._alltrails_filter_max_miles = 4.0
    discoverer._max_trail_miles = 4.0
    discoverer._alltrails_filter_max_gain_feet = 1000
    discoverer._alltrails_filter_min_reviews = 5

    with patch.object(discoverer, "_get_filtered_alltrails_selection", return_value=None):
        with patch.object(
            discoverer,
            "_search_first",
            return_value="https://www.alltrails.com/trail/us/utah/navajo-knobs-trail",
        ):
            with patch.object(
                discoverer,
                "_fetch_page_text",
                return_value=(
                    True,
                    200,
                    "Navajo Knobs Trail. Hard. 9.4 mi. Elevation gain 1,620 ft. 4.8 stars, 785 reviews.",
                ),
            ):
                result = discoverer._search_alltrails_for_trail("Navajo Knobs", "Capitol Reef National Park")

    assert result is None


def test_search_alltrails_post_search_filters_reject_direct_batch_selection_when_constraints_fail() -> None:
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._alltrails_source = "direct_link_batch"
    discoverer._direct_batch_authoritative = True
    discoverer._disable_trails = False
    discoverer._enable_filtered_alltrails_selection = True
    discoverer._alltrails_filter_allowed_difficulties = ("easy", "moderate", "moderately challenging")
    discoverer._alltrails_filter_max_miles = 4.0
    discoverer._max_trail_miles = 4.0
    discoverer._alltrails_filter_max_gain_feet = 1000
    discoverer._alltrails_filter_min_reviews = 5

    with patch.object(
        discoverer,
        "_search_alltrails_for_trail_from_direct_batch",
        return_value="https://www.alltrails.com/trail/us/utah/navajo-knobs-trail",
    ):
        with patch.object(
            discoverer,
            "_fetch_page_text",
            return_value=(
                True,
                200,
                "Navajo Knobs Trail. Strenuous. 9.4 mi. Elevation gain 1,620 ft. 4.8 stars, 785 reviews.",
            ),
        ):
            result = discoverer._search_alltrails_for_trail("Navajo Knobs", "Capitol Reef National Park")

    assert result is None


def test_search_alltrails_post_search_filters_fail_open_when_metadata_unavailable() -> None:
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._alltrails_source = "search"
    discoverer._disable_trails = False
    discoverer._enable_filtered_alltrails_selection = True

    with patch.object(discoverer, "_get_filtered_alltrails_selection", return_value=None):
        with patch.object(
            discoverer,
            "_search_first",
            return_value="https://www.alltrails.com/trail/us/utah/canyon-overlook-trail",
        ):
            with patch.object(discoverer, "_fetch_page_text", return_value=(False, 403, "")):
                result = discoverer._search_alltrails_for_trail("Canyon Overlook Trail", "Zion National Park")

    assert result == "https://www.alltrails.com/trail/us/utah/canyon-overlook-trail"


def test_candidate_mentions_conflicting_destination_detects_other_park() -> None:
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    candidate = {
        "title": "Navajo Loop Trail",
        "snippet": "Popular route in Zion National Park near Springdale.",
    }

    assert discoverer._candidate_mentions_conflicting_destination(candidate, "Bryce Canyon National Park")


def test_discover_restaurants_can_use_direct_batch_source():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._restaurant_source = "direct_link_batch"

    ai = {
        "dinner_recommendations": [{"name": "The Spotted Dog Cafe"}],
        "top_attractions": [],
        "getting_here": {"en_route_stops": []},
    }

    with patch.object(discoverer, "_resolve_ai_candidate_url", return_value=None):
        with patch.object(
            discoverer,
            "_search_restaurant_from_direct_batch",
            return_value="https://www.tripadvisor.com/Restaurant_Review-g57119-d123456-Reviews-The_Spotted_Dog_Cafe-Springdale_Utah.html",
        ):
            discoverer._discover_restaurants(ai, dest_name="Zion National Park")

    entry = ai["dinner_recommendations"][0]
    assert "tripadvisor.com" in entry["url"]
    assert "maps_url" not in entry


def test_discover_restaurants_direct_batch_preserves_existing_url_without_rematch() -> None:
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._restaurant_source = "direct_link_batch"

    ai = {
        "dinner_recommendations": [
            {
                "name": "Painted Pony",
                "url": "https://www.painted-pony.com/",
            }
        ],
        "top_attractions": [],
        "getting_here": {"en_route_stops": []},
    }

    with patch.object(discoverer, "_retain_discovered_url", side_effect=lambda url, *_a, **_k: url):
        with patch.object(discoverer, "_search_restaurant_from_direct_batch") as batch_search:
            with patch.object(discoverer, "_search_first") as fallback_search:
                discoverer._discover_restaurants(ai, dest_name="St. George, Utah")

    assert ai["dinner_recommendations"][0]["url"] == "https://www.painted-pony.com/"
    batch_search.assert_not_called()
    fallback_search.assert_not_called()


def test_discover_restaurants_direct_batch_preserves_existing_maps_url_without_rematch() -> None:
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._restaurant_source = "direct_link_batch"

    ai = {
        "dinner_recommendations": [
            {
                "name": "Painted Pony",
                "url": "https://www.google.com/maps/search/?api=1&query=Painted+Pony+St+George",
            }
        ],
        "top_attractions": [],
        "getting_here": {"en_route_stops": []},
    }

    # See the contract note on
    # test_discover_restaurants_direct_batch_authoritative_does_not_fallback_to_search:
    # the batch still wins, but its silence is no longer treated as an answer.
    with patch.object(discoverer, "_search_restaurant_from_direct_batch", return_value=None) as batch_search:
        with patch.object(discoverer, "_search_first", return_value=None) as fallback_search:
            discoverer._discover_restaurants(ai, dest_name="St. George, Utah")

    assert ai["dinner_recommendations"][0].get("url", "") == ""
    assert "maps_url" not in ai["dinner_recommendations"][0]
    batch_search.assert_called_once()
    assert fallback_search.called


def test_discover_restaurants_preserved_existing_url_still_gets_rating_and_cuisine_metadata() -> None:
    """Regression for dipstick55 Theme D: the 'existing URL already
    attached, just validate it' shortcut for restaurants had the same gap as
    the attraction-side one -- it skipped the row-metadata merge, so a
    restaurant seeded with its url pre-attached lost rating/cuisine/price
    even though the matched batch row had them."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._restaurant_source = "direct_link_batch"

    ai = {
        "dinner_recommendations": [
            {
                "name": "Cliffside Restaurant",
                "url": "https://www.cliffsiderestaurant.com/",
            }
        ],
        "top_attractions": [],
        "getting_here": {"en_route_stops": []},
    }

    rows = [
        {
            "name": "Cliffside Restaurant",
            "title": "Cliffside Restaurant",
            "url": "https://www.cliffsiderestaurant.com/",
            "rating": 4.4,
            "raw_rating": "4.4/5",
            "cuisine": "American",
            "price_range": "$$$",
        }
    ]

    with patch.object(discoverer, "_retain_discovered_url", side_effect=lambda url, *_a, **_k: url):
        with patch.object(discoverer, "_get_restaurant_direct_batch_rows_for_destination", return_value=rows):
            discoverer._discover_restaurants(ai, dest_name="St. George, Utah")

    rest = ai["dinner_recommendations"][0]
    assert rest["url"] == "https://www.cliffsiderestaurant.com/"
    assert rest.get("raw_rating") == "4.4/5"
    assert rest.get("cuisine") == "American"
    assert rest.get("price_range") == "$$$"


def test_enrich_restaurant_metadata_from_url_populates_missing_fields() -> None:
    discoverer = URLDiscoverer.__new__(URLDiscoverer)

    rest = {
        "name": "Painted Pony",
        "url": "https://www.painted-pony.com/",
        "description": "",
        "cuisine": "",
    }

    json_ld = json.dumps({
        "@type": "Restaurant",
        "servesCuisine": ["American", "Contemporary"],
        "priceRange": "$$$",
        "description": "Award-winning fine dining in downtown St. George.",
    })
    html = f'<html><head><script type="application/ld+json">{json_ld}</script></head></html>'

    with patch.object(discoverer, "_fetch_page_text", return_value=(True, 200, html)):
        discoverer._enrich_restaurant_metadata_from_url(rest)

    assert rest["cuisine"] == "American, Contemporary"
    assert rest["price_range"] == "$$$"
    assert "fine dining" in rest["description"]


def test_enrich_restaurant_metadata_from_url_infers_from_title_when_jsonld_missing() -> None:
    discoverer = URLDiscoverer.__new__(URLDiscoverer)

    rest = {
        "name": "Casa Roma",
        "url": "https://www.example.com/casa-roma",
        "description": "",
        "cuisine": "",
        "price_range": "",
    }

    html = "<html><head><title>Casa Roma Italian $$ in Moab</title></head><body>Welcome</body></html>"
    with patch.object(discoverer, "_fetch_page_text", return_value=(True, 200, html)):
        discoverer._enrich_restaurant_metadata_from_url(rest)

    assert rest.get("cuisine") == "Italian"
    assert rest.get("price_range") == "$$"


def test_enrich_restaurant_metadata_skips_when_all_fields_present() -> None:
    discoverer = URLDiscoverer.__new__(URLDiscoverer)

    rest = {
        "name": "Painted Pony",
        "url": "https://www.painted-pony.com/",
        "description": "Great food.",
        "cuisine": "American",
        "price_range": "$$",
    }

    with patch.object(discoverer, "_fetch_page_text") as mock_fetch:
        discoverer._enrich_restaurant_metadata_from_url(rest)

    mock_fetch.assert_not_called()


def test_enrich_restaurant_metadata_skips_maps_fallback_urls() -> None:
    discoverer = URLDiscoverer.__new__(URLDiscoverer)

    rest = {
        "name": "Painted Pony",
        "url": "https://www.google.com/maps/search/?api=1&query=Painted+Pony",
    }

    with patch.object(discoverer, "_fetch_page_text") as mock_fetch:
        discoverer._enrich_restaurant_metadata_from_url(rest)

    mock_fetch.assert_not_called()


def test_backfill_restaurant_metadata_from_available_text_inferrs_cuisine_and_price() -> None:
    rest = {
        "name": "Riggatti's Wood Fired Pizza",
        "description": "Popular local spot $$ with patio seating.",
        "url": "",
        "maps_url": "https://www.google.com/maps/search/?api=1&query=Riggattis+Wood+Fired+Pizza+St+George",
    }

    URLDiscoverer._backfill_restaurant_metadata_from_available_text(rest)

    assert rest.get("cuisine") == "Pizza"
    assert rest.get("price_range") == "$$"


def test_infer_restaurant_metadata_extracts_price_immediately_followed_by_comma() -> None:
    """dipstick61: the common harvest format is "Name - 4.7/5, $$$, Cuisine"
    -- the price run has no trailing space before the comma. The prior
    regex required whitespace-or-end-of-string on both sides, so price_range
    silently never got set despite 15 real restaurants stating a real price
    in their raw harvest text (Wood Ash Rye, Cliffside Restaurant, etc.)."""
    meta = URLDiscoverer._infer_restaurant_metadata_from_text_and_url(
        "Wood Ash Rye - 4.7/5, $$$, New American", ""
    )
    assert meta.get("price_range") == "$$$"

    meta2 = URLDiscoverer._infer_restaurant_metadata_from_text_and_url(
        "Cliffside Restaurant - 4.4/5, $$$, Upscale American", ""
    )
    assert meta2.get("price_range") == "$$$"


def test_backfill_restaurant_metadata_does_not_override_existing_fields() -> None:
    rest = {
        "name": "Painted Pony",
        "description": "Contemporary southwestern fare.",
        "cuisine": "American",
        "price_range": "$$$",
        "maps_url": "https://www.google.com/maps/search/?api=1&query=Painted+Pony+St+George",
    }

    URLDiscoverer._backfill_restaurant_metadata_from_available_text(rest)

    assert rest.get("cuisine") == "American"
    assert rest.get("price_range") == "$$$"


def test_direct_batch_row_matches_item_ignores_shared_destination_name_token() -> None:
    """Full-pipeline regression for a real reported bug: in a 'St. George, Utah'
    destination, almost every harvested row's address text contains 'George'
    (e.g. via the Maps query string), so a bare token-overlap match without
    destination-name awareness falsely matched 18 of 20 unrelated attractions
    against 'St. George Temple'. Passing dest_name must exclude destination-name
    tokens from the match so only genuinely related rows match."""
    row = {
        "name": "Brigham Young Winter Home",
        "title": "Brigham Young Winter Home",
        "snippet": (
            "Brigham Young Winter Home Source Maps Links: "
            "https://www.nps.gov/gosp/learn/historyculture/byhome.htm "
            "https://www.google.com/maps/search/?api=1&query=Brigham+Young+Winter+Home+67+W+200+N+St.+George+UT"
        ),
    }
    assert URLDiscoverer._direct_batch_row_matches_item(row, "St. George Temple", "St. George, Utah") is False
    # Without dest_name (backward-compatible default), the historical loose
    # behavior is preserved -- this documents why dest_name must be threaded
    # through call sites rather than relied on implicitly.
    assert URLDiscoverer._direct_batch_row_matches_item(row, "St. George Temple") is True


def test_direct_batch_url_matches_item_ignores_shared_destination_name_token() -> None:
    url = "https://www.google.com/maps/search/?api=1&query=Brigham+Young+Winter+Home+67+W+200+N+St.+George+UT"
    assert URLDiscoverer._direct_batch_url_matches_item(url, "St. George Temple", "St. George, Utah") is False
    assert URLDiscoverer._direct_batch_url_matches_item(url, "St. George Temple") is True


def test_direct_batch_url_priority_prefers_specific_source_over_maps_search_for_attraction() -> None:
    """Regression for the actual reported bug: a Maps search link must never
    outrank a specific official/source page for attractions -- it did, because
    this scoring block previously had maps_search/maps_place assigned the
    *lowest* (winning) numbers instead of the highest."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    official = discoverer._direct_batch_url_priority(
        "https://www.churchofjesuschrist.org/temples/details/st-george-temple", kind="attraction"
    )
    maps_search = discoverer._direct_batch_url_priority(
        "https://www.google.com/maps/search/?api=1&query=St.+George+Temple", kind="attraction"
    )
    tripadvisor = discoverer._direct_batch_url_priority(
        "https://www.tripadvisor.com/Attraction_Review-g1-d1-St_George_Temple.html", kind="attraction"
    )
    assert official < maps_search
    assert official < tripadvisor
    assert tripadvisor < maps_search


def test_url_recommendation_source_count_tracks_multiple_mechanisms() -> None:
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    url = "https://example.com/angels-landing"
    assert discoverer._url_recommendation_source_count(url) == 0
    discoverer._record_url_recommendation_source(url, "direct_batch")
    assert discoverer._url_recommendation_source_count(url) == 1
    discoverer._record_url_recommendation_source(url, "direct_batch")
    assert discoverer._url_recommendation_source_count(url) == 1
    discoverer._record_url_recommendation_source(url, "ai_candidate")
    assert discoverer._url_recommendation_source_count(url) == 2


def test_search_attraction_matches_ai_named_overlook_to_bare_harvest_row_name() -> None:
    """Full-pipeline regression for a real reported bug: AI-generated attraction
    names often append a generic descriptive suffix ('Overlook') the harvest
    row doesn't use ('Bryce Point' vs 'Bryce Point Overlook'). The destination-
    name-token exclusion (see the dest-token test above) can strip the row's
    only remaining distinctive word when it coincides with the destination's
    own name ('Bryce' in 'Bryce Canyon'), which must not make the item
    unmatchable -- but it also must not let a single shared word alone match
    an unrelated row like 'Bryce Canyon Lodge'."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._direct_batch_authoritative = True
    rows = [
        {
            "name": "Bryce Point",
            "title": "Bryce Point",
            "url": "https://www.nps.gov/brca/planyourvisit/bryce-point.htm",
            "snippet": "Bryce Point Links: https://www.nps.gov/brca/planyourvisit/bryce-point.htm",
        },
        {
            "name": "Bryce Canyon Lodge",
            "title": "Bryce Canyon Lodge",
            "url": "https://www.brycecanyonforever.com/",
            "snippet": "Bryce Canyon Lodge Links: https://www.brycecanyonforever.com/",
        },
    ]

    with patch.object(discoverer, "_get_attraction_direct_batch_rows_for_destination", return_value=rows):
        with patch.object(discoverer, "_retain_discovered_url", side_effect=lambda url, *a, **k: url):
            out = discoverer._search_attraction_from_direct_batch(
                "Bryce Point Overlook", "Bryce Canyon National Park", "October 19-21, 2026"
            )

    assert out == "https://www.nps.gov/brca/planyourvisit/bryce-point.htm"


def test_direct_batch_row_match_strength_rejects_single_shared_word_against_unrelated_row() -> None:
    row = {
        "name": "Bryce Canyon Lodge",
        "title": "Bryce Canyon Lodge",
        "snippet": "Bryce Canyon Lodge Links: https://www.brycecanyonforever.com/",
    }
    strength = URLDiscoverer._direct_batch_row_match_strength(
        row, "Bryce Point Overlook", "Bryce Canyon National Park"
    )
    assert strength == 0


def test_direct_batch_row_match_strength_rejects_canyon_generic_anchor_overlap() -> None:
    """Real dipstick67 bug: the seed attraction "Jenny's Canyon Trail" (St.
    George, Utah) was authoritatively linked to an unrelated direct-batch
    row for "Entrada at Snow Canyon Golf Course" -- a golf course with
    nothing to do with the trail. Root cause: after "trail" is dropped as a
    generic stopword, the item's only remaining significant tokens are
    "jenny" and "canyon"; the golf course row's blob shares only "canyon"
    (via "Snow Canyon"), which was enough to reach the weak single-anchor-
    token match tier (strength 1) purely because "canyon" is >= 5 characters.
    This is the same generic-word-overlap class as the "scenic"/"byway"
    fix, except "canyon" is common enough across real, unrelated named
    places in canyon country (Snow Canyon, Zion Canyon, Bryce Canyon, Red
    Canyon, ...) that it must not single-handedly justify even the weak
    match tier. Real evidence: console log line
    "[attraction-st-george-utah-jenny-s-canyon-trail|reason=
    direct_batch_selected_authoritative] attraction link (direct-link batch
    authoritative): Jenny's Canyon Trail -> https://www.google.com/maps/
    search/?api=1&query=Entrada+at+Snow+Canyon+Golf+Course+St.+George+UT"
    from the real SW2026-dipstick67 run."""
    row = {
        "name": "Entrada at Snow Canyon Golf Course",
        "title": "Entrada at Snow Canyon Golf Course",
        "url": "https://www.google.com/maps/search/?api=1&query=Entrada+at+Snow+Canyon+Golf+Course+St.+George+UT",
        "snippet": (
            "Entrada at Snow Canyon Golf Course - private golf club Links: "
            "https://www.google.com/maps/search/?api=1&query=Entrada+at+Snow+Canyon+Golf+Course+St.+George+UT"
        ),
    }
    strength = URLDiscoverer._direct_batch_row_match_strength(
        row, "Jenny's Canyon Trail", "St. George, Utah"
    )
    assert strength == 0
    assert URLDiscoverer._direct_batch_url_matches_item(
        row["url"], "Jenny's Canyon Trail", "St. George, Utah"
    ) is False


def test_search_attraction_from_direct_batch_rejects_unrelated_canyon_golf_course() -> None:
    """Full-path regression for the dipstick67 Jenny's Canyon Trail mismatch
    (see test_direct_batch_row_match_strength_rejects_canyon_generic_anchor_
    overlap above): resolving the seed attraction's link from the real St.
    George direct-batch harvest must not return the unrelated golf course
    row just because both mention "canyon"."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._direct_batch_authoritative = True

    rows = [
        {
            "name": "Entrada at Snow Canyon Golf Course",
            "title": "Entrada at Snow Canyon Golf Course",
            "url": "https://www.google.com/maps/search/?api=1&query=Entrada+at+Snow+Canyon+Golf+Course+St.+George+UT",
            "snippet": (
                "Entrada at Snow Canyon Golf Course - private golf club Links: "
                "https://www.google.com/maps/search/?api=1&query=Entrada+at+Snow+Canyon+Golf+Course+St.+George+UT"
            ),
        },
    ]

    with patch.object(discoverer, "_get_attraction_direct_batch_rows_for_destination", return_value=rows):
        with patch.object(discoverer, "_retain_discovered_url", side_effect=lambda url, *a, **k: url):
            out = discoverer._search_attraction_from_direct_batch(
                "Jenny's Canyon Trail", "St. George, Utah", "October 17, 2026"
            )

    assert out is None


def test_direct_batch_row_match_strength_rejects_scenic_byway_boilerplate_overlap() -> None:
    """Real dipstick62 bug: the en-route-stop seed "Enchanted Circle Scenic
    Byway" (a real byway near Taos, NM, unrelated to Ouray, CO) rendered
    linked to "https://ourayhotsprings.com/" -- Ouray Hot Springs Pool, a
    completely different attraction. Root cause: "scenic" and "byway" are
    generic route-type descriptors (the same class of word "trail"/"road"/
    "drive"/"point" are already excluded for), not identifying words. The
    real harvested row for Ouray Hot Springs Pool mentions being near a
    "scenic" mountain setting -- sharing just "scenic" with the item name
    was, before this fix, enough (combined with "byway" also counting as
    generic boilerplate) to approach the required token-overlap bar without
    either of the item's real identifying words ("enchanted"/"circle")
    ever matching. This is the exact real row captured in
    dev/dev/url_discovery_direct_batch_html/pagosa-springs.en-route-stop...html
    from that run."""
    row = {
        "name": "Ouray Hot Springs Pool",
        "title": "Ouray Hot Springs Pool",
        "url": "https://ourayhotsprings.com/",
        "description": "natural mineral pools in scenic mountain setting",
        "snippet": (
            "Ouray Hot Springs Pool - natural mineral pools in scenic mountain "
            "setting - detour 12 mi / 17 min Links: https://ourayhotsprings.com/ "
            "https://www.google.com/maps/search/?api=1&query=Ouray+Hot+Springs+Pool+Ouray+CO"
        ),
    }
    strength = URLDiscoverer._direct_batch_row_match_strength(
        row, "Enchanted Circle Scenic Byway", "Pagosa Springs"
    )
    assert strength == 0


def test_search_en_route_stop_from_direct_batch_rejects_unrelated_scenic_byway_seed_match() -> None:
    """Full-path regression for the dipstick62 Ouray Hot Springs Pool
    mismatch (see test_direct_batch_row_match_strength_rejects_scenic_byway_
    boilerplate_overlap above): even when the seed's own live page fetch
    happens to mention "scenic" boilerplate copy, resolving the seed's URL
    from the real Telluride->Pagosa Springs harvest batch must not return
    the unrelated Ouray Hot Springs Pool link."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._direct_batch_authoritative = True

    rows = [
        {
            "name": "Treasure Falls",
            "title": "Treasure Falls",
            "url": "https://www.fs.usda.gov/recarea/sanjuan/recarea/?recid=43046",
            "maps_url": "https://www.google.com/maps/search/?api=1&query=Treasure+Falls+Pagosa+Springs+CO",
            "snippet": "Treasure Falls - scenic roadside waterfall pullout with short trail - detour 0 mi / 5 min",
        },
        {
            "name": "Ouray Hot Springs Pool",
            "title": "Ouray Hot Springs Pool",
            "url": "https://ourayhotsprings.com/",
            "maps_url": "https://www.google.com/maps/search/?api=1&query=Ouray+Hot+Springs+Pool+Ouray+CO",
            "description": "natural mineral pools in scenic mountain setting",
            "snippet": (
                "Ouray Hot Springs Pool - natural mineral pools in scenic mountain "
                "setting - detour 12 mi / 17 min"
            ),
        },
    ]

    with patch.object(discoverer, "_get_en_route_direct_batch_rows_for_destination", return_value=rows):
        with patch.object(
            discoverer,
            "_fetch_page_text",
            return_value=(True, 200, "Visit Ouray Hot Springs Pool along the scenic San Juan Skyway."),
        ):
            url = discoverer._search_en_route_stop_from_direct_batch(
                "Enchanted Circle Scenic Byway", "Pagosa Springs", "October 26-27, 2026", "Telluride"
            )

    assert url != "https://ourayhotsprings.com/"
    assert url is None


def test_persistent_cache_round_trips_en_route_geocode_results(tmp_path) -> None:
    """Full-pipeline regression: the en-route geocode cache was in-memory only,
    so every stop got re-geocoded (and re-throttled against Nominatim) on
    every run -- and twice per run, since URL discovery runs a second time in
    the selective-retry pass. Confirmed coordinates must survive a save/load
    round trip so repeat runs (and the retry pass) can skip the network call."""
    cache_path = tmp_path / "persistent_cache.json"

    writer = URLDiscoverer.__new__(URLDiscoverer)
    writer._persistent_cache_enabled = True
    writer._persistent_cache_path = str(cache_path)
    writer._persistent_cache_dirty = True
    writer._request_cache_lock = Lock()
    writer._en_route_stop_geocode_cache = {"red canyon|zion national park|bryce canyon national park": (37.72, -112.31)}
    writer._save_persistent_caches()

    assert cache_path.exists()

    reader = URLDiscoverer.__new__(URLDiscoverer)
    reader._persistent_cache_enabled = True
    reader._persistent_cache_path = str(cache_path)
    reader._en_route_stop_geocode_cache = {}
    reader._load_persistent_caches()

    assert reader._en_route_stop_geocode_cache["red canyon|zion national park|bryce canyon national park"] == (37.72, -112.31)


def test_persistent_cache_never_saves_failed_en_route_geocode_results(tmp_path) -> None:
    """A 'no result' (None) is often a transient Nominatim rate-limit/timeout
    outcome, not a durable 'this place doesn't exist' answer -- it must not be
    frozen into the persistent cache."""
    cache_path = tmp_path / "persistent_cache.json"

    writer = URLDiscoverer.__new__(URLDiscoverer)
    writer._persistent_cache_enabled = True
    writer._persistent_cache_path = str(cache_path)
    writer._persistent_cache_dirty = True
    writer._request_cache_lock = Lock()
    writer._en_route_stop_geocode_cache = {"nowhere|a|b": None}
    writer._save_persistent_caches()

    payload = json.loads(cache_path.read_text(encoding="utf-8"))
    assert payload["en_route_geocode"] == {}


def test_persistent_cache_round_trips_alltrails_fetch_results(tmp_path) -> None:
    cache_path = tmp_path / "persistent_cache.json"

    writer = URLDiscoverer.__new__(URLDiscoverer)
    writer._persistent_cache_enabled = True
    writer._persistent_cache_path = str(cache_path)
    writer._persistent_cache_dirty = True
    writer._request_cache_lock = Lock()
    writer._alltrails_fetch_cache = {
        "https://www.alltrails.com/trail/us/utah/angels-landing-trail": (True, 200, "<html>trail page</html>")
    }
    writer._save_persistent_caches()

    reader = URLDiscoverer.__new__(URLDiscoverer)
    reader._persistent_cache_enabled = True
    reader._persistent_cache_path = str(cache_path)
    reader._alltrails_fetch_cache = {}
    reader._load_persistent_caches()

    assert reader._alltrails_fetch_cache["https://www.alltrails.com/trail/us/utah/angels-landing-trail"] == (
        True,
        200,
        "<html>trail page</html>",
    )


def test_persistent_cache_never_saves_blocked_alltrails_fetch_results(tmp_path) -> None:
    """Caching a transient DataDome block (401/403) would freeze that block
    state in across runs, making every subsequent run treat a live trail page
    as permanently blocked."""
    cache_path = tmp_path / "persistent_cache.json"

    writer = URLDiscoverer.__new__(URLDiscoverer)
    writer._persistent_cache_enabled = True
    writer._persistent_cache_path = str(cache_path)
    writer._persistent_cache_dirty = True
    writer._request_cache_lock = Lock()
    writer._alltrails_fetch_cache = {
        "https://www.alltrails.com/trail/us/utah/blocked-trail": (False, 403, "")
    }
    writer._save_persistent_caches()

    payload = json.loads(cache_path.read_text(encoding="utf-8"))
    assert payload["alltrails_fetch_results"] == {}


def _page_text_cache_writer(tmp_path, entries):
    cache_path = tmp_path / "persistent_cache.json"
    writer = URLDiscoverer.__new__(URLDiscoverer)
    writer._persistent_cache_enabled = True
    writer._persistent_cache_path = str(cache_path)
    writer._persistent_cache_dirty = True
    writer._request_cache_lock = Lock()
    writer._page_text_cache = dict(entries)
    writer._fetch_final_url_cache = {}
    writer._save_persistent_caches()
    return cache_path


def test_persistent_cache_only_saves_page_text_fetches_that_observed_something(tmp_path) -> None:
    """The general page-text cache feeds the relevance and redirect gates, and
    it was persisting outcomes that are not observations. A 401/403 block page,
    a timeout or an SSL error says nothing about the URL -- it says this run
    could not reach it -- and `classify_link_liveness` already calls exactly
    those outcomes `unchecked`. Written to disk, that placeholder is inherited
    by every later run and read as a measurement.

    The two caches immediately below this one in `_save_persistent_caches`
    refuse the same thing for their own transient failures (the geocoder's
    "no result", AllTrails' DataDome block). A live status and a definitive
    404 are observations and must still round trip.
    """
    live = "https://www.nps.gov/brca/"
    gone = "https://example.org/removed"
    blocked = "https://www.tripadvisor.com/Restaurant_Review-g1-d1.html"
    timed_out = "https://www.opentable.com/r/somewhere"

    cache_path = _page_text_cache_writer(
        tmp_path,
        {
            live: (True, 200, "<html>Bryce Canyon National Park</html>"),
            gone: (False, 404, ""),
            blocked: (False, 403, ""),
            timed_out: (False, "HTTPSConnectionPool: Read timed out.", ""),
        },
    )

    saved = json.loads(cache_path.read_text(encoding="utf-8"))["page_text_results"]
    assert live in saved
    assert gone in saved
    assert blocked not in saved
    assert timed_out not in saved


def test_a_blocked_page_text_entry_already_on_disk_is_not_loaded_as_a_measurement(tmp_path) -> None:
    """Refusing to write new ones does not remove the ones already written --
    they are on disk, restored by every run, and still deciding. The loader
    applies the same predicate, so an inherited placeholder costs one real
    fetch instead of standing in for a verdict."""
    blocked = "https://www.tripadvisor.com/Restaurant_Review-g1-d1.html"
    live = "https://www.nps.gov/brca/"

    cache_path = tmp_path / "persistent_cache.json"
    cache_path.write_text(
        json.dumps(
            {
                "page_text_results": {
                    blocked: {"ts": time.time(), "ok": False, "status": 403, "text": "", "final_url": ""},
                    live: {"ts": time.time(), "ok": True, "status": 200, "text": "<html>ok</html>", "final_url": ""},
                }
            }
        ),
        encoding="utf-8",
    )

    reader = URLDiscoverer.__new__(URLDiscoverer)
    reader._persistent_cache_enabled = True
    reader._persistent_cache_path = str(cache_path)
    reader._page_text_cache = {}
    reader._fetch_final_url_cache = {}
    reader._load_persistent_caches()

    assert blocked not in reader._page_text_cache
    assert live in reader._page_text_cache


def test_the_retention_gate_reaches_the_same_verdict_after_a_run_that_was_blocked(tmp_path) -> None:
    """The whole point of the two guards above, at the gate they protect.

    One URL, one item, one code path, twice -- differing only in what the
    cache held. A run that reached the page sees the redirect to a generic
    hub and rejects. A run that inherited a blocked fetch from an earlier run
    never asks the network at all: the failed fetch fails open on liveness,
    and the redirect record that entry does not carry is read as "this URL did
    not redirect" rather than "nothing ever looked". The gate then accepts, on
    no evidence, the URL the other run refused.

    A gate whose verdict depends on cache warmth is not a gate. Both runs must
    reach the same answer, and the second one has to spend a request to do it.
    """
    url = "https://www.nps.gov/brca/"
    final = "https://www.nps.gov/brca/index.htm"
    item = "Bryce Canyon National Park"
    dest = "Bryce Canyon"
    body = "<html><body><h1>Bryce Canyon National Park</h1></body></html>"
    row = {"name": item, "title": item, "url": url, "snippet": "Official park site."}

    def build(cache_path, status):
        validator = MagicMock()
        validator._last_final_url = ""

        def get_text(_url, timeout=None):
            if status == 200:
                validator._last_final_url = final
                return True, 200, body
            validator._last_final_url = ""
            return False, status, ""

        validator.get_text = get_text
        discoverer = URLDiscoverer.__new__(URLDiscoverer)
        discoverer._url_validator = validator
        discoverer._persistent_cache_enabled = True
        discoverer._persistent_cache_path = str(cache_path)
        discoverer._persistent_cache_dirty = True
        discoverer._request_cache_lock = Lock()
        discoverer._page_text_cache = {}
        discoverer._fetch_final_url_cache = {}
        discoverer._verify_url_cache = {}
        discoverer._domain_blocked_until_ts = {}
        discoverer._domain_block_cooldown_seconds = 0
        discoverer._direct_batch_authoritative = True
        return discoverer

    def verdict(discoverer):
        return discoverer._retain_discovered_url(
            url, item, dest,
            allow_alltrails=False,
            kind="en-route stop",
            candidate=row,
            allow_shallow_relevance=False,
            allow_google_maps_search=True,
        )

    reached = build(tmp_path / "reached.json", 200)
    assert verdict(reached) == ""
    assert reached._last_retention_rejection[0] == 28

    blocked_run = build(tmp_path / "shared.json", 403)
    verdict(blocked_run)
    blocked_run._save_persistent_caches()

    later_run = build(tmp_path / "shared.json", 200)
    later_run._load_persistent_caches()
    assert verdict(later_run) == ""
    assert later_run._last_retention_rejection[0] == 28


def test_persistent_cache_round_trips_direct_batch_harvest_rows(tmp_path) -> None:
    """Regression for issue #66: direct-batch harvest rows (the expensive
    per-destination-per-kind Grok HTML-list calls for attractions/restaurants/
    trails/en-route stops) were in-memory only, so an unchanged manifest run
    again the same day re-harvested every destination from scratch. Rows must
    survive a save/load round trip, routed back into the correct per-kind
    cache dict."""
    cache_path = tmp_path / "persistent_cache.json"

    writer = URLDiscoverer.__new__(URLDiscoverer)
    writer._persistent_cache_enabled = True
    writer._persistent_cache_path = str(cache_path)
    writer._persistent_cache_dirty = True
    writer._request_cache_lock = Lock()
    writer._alltrails_direct_batch_cache = {
        "zion national park||2026-10-12 to 2026-10-15|html|trail": [
            {"name": "Angels Landing", "url": "https://www.alltrails.com/trail/us/utah/angels-landing-trail"},
        ]
    }
    writer._attraction_direct_batch_cache = {
        "zion national park||2026-10-12 to 2026-10-15|html|attraction": [
            {"name": "Zion Human History Museum", "url": "https://www.nps.gov/zion/planyourvisit/history-museum.htm"},
        ]
    }
    writer._restaurant_direct_batch_cache = {}
    writer._en_route_direct_batch_cache = {}
    writer._save_persistent_caches()

    payload = json.loads(cache_path.read_text(encoding="utf-8"))
    assert "direct_batch_harvest_alltrails" in payload
    assert "direct_batch_harvest_attractions" in payload

    reader = URLDiscoverer.__new__(URLDiscoverer)
    reader._persistent_cache_enabled = True
    reader._persistent_cache_path = str(cache_path)
    reader._load_persistent_caches()

    assert reader._alltrails_direct_batch_cache[
        "zion national park||2026-10-12 to 2026-10-15|html|trail"
    ] == [{"name": "Angels Landing", "url": "https://www.alltrails.com/trail/us/utah/angels-landing-trail"}]
    assert reader._attraction_direct_batch_cache[
        "zion national park||2026-10-12 to 2026-10-15|html|attraction"
    ] == [{"name": "Zion Human History Museum", "url": "https://www.nps.gov/zion/planyourvisit/history-museum.htm"}]
    # The other two kinds' caches were empty at save time and must not appear
    # as spurious entries after load.
    assert reader._restaurant_direct_batch_cache == {}
    assert reader._en_route_direct_batch_cache == {}


def test_persistent_cache_never_saves_empty_direct_batch_harvest_results(tmp_path) -> None:
    """An empty harvest batch is often a transient upstream hiccup (e.g. a
    Grok timeout burst), not durable proof the destination has zero
    attractions -- it must not be frozen into the persistent cache."""
    cache_path = tmp_path / "persistent_cache.json"

    writer = URLDiscoverer.__new__(URLDiscoverer)
    writer._persistent_cache_enabled = True
    writer._persistent_cache_path = str(cache_path)
    writer._persistent_cache_dirty = True
    writer._request_cache_lock = Lock()
    writer._attraction_direct_batch_cache = {"nowhere||2026-10-12|html|attraction": []}
    writer._save_persistent_caches()

    payload = json.loads(cache_path.read_text(encoding="utf-8"))
    assert payload["direct_batch_harvest_attractions"] == {}


def test_get_direct_batch_html_rows_marks_persistent_cache_dirty_on_success(tmp_path) -> None:
    """Regression (2026-08-15, dipstick55): the earlier round-trip test
    manually set _persistent_cache_dirty=True before saving, which proved
    the save/load mechanics work but never proved the real write path
    (_get_direct_batch_html_rows_for_destination) actually flips that flag.
    It didn't -- a successful harvest updated the in-memory cache but never
    marked the persistent cache dirty, so nothing was ever actually
    persisted to disk despite the feature being enabled by default and
    fully wired on the load side. Real cost: a same-run retry pass building
    a fresh URLDiscoverer had no persisted cache to load and re-fetched
    every destination from scratch, roughly doubling that run's real spend."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._persistent_cache_dirty = False
    discoverer._persistent_cache_pending_writes = 0
    discoverer._persistent_cache_write_every = 25
    discoverer._persistent_cache_enabled = True
    discoverer._persistent_cache_path = str(tmp_path / "persistent_cache.json")

    with patch.object(discoverer, "_fetch_direct_batch_html_rows", return_value=[{"name": "Angels Landing", "url": "https://www.alltrails.com/x"}]):
        rows = discoverer._get_direct_batch_html_rows_for_destination(
            cache={}, destination="Zion National Park", dates="October 7-9, 2026", kind="trail",
        )

    assert rows
    assert discoverer._persistent_cache_dirty is True


def test_second_url_discoverer_instance_skips_refetch_via_persistent_cache(tmp_path) -> None:
    """Regression (2026-08-15, dipstick56+): the dirty-flag tests above prove
    a write marks the cache dirty and gets saved, but never prove the thing
    that actually matters for cost -- that a SECOND URLDiscoverer instance
    (e.g. a same-run selective-retry pass, which constructs a fresh
    instance with empty in-memory caches) actually loads the persisted
    cache and skips a real refetch. It wasn't tested, and a real live
    validation run (dipstick56+) found url_discovery's batching-efficiency
    ratio (requests avoided vs. a naive one-call-per-item baseline) was
    WORSE than the prior run despite this exact fix landing first --
    meaning this end-to-end path may not be working as intended in
    practice.

    Must go through the real production entry point
    (_get_alltrails_direct_batch_rows_for_destination), not the lower-level
    _get_direct_batch_html_rows_for_destination directly -- that method
    takes `cache` as an injected parameter, and calling it with a bare `{}`
    (as an earlier version of this test, and the existing dirty-flag tests
    above, both do) writes into a throwaway dict that's never attached to
    the instance's real _alltrails_direct_batch_cache attribute, silently
    passing without ever exercising real persistence. Found exactly this
    gap while writing this test: it initially "passed" the wrong way (by
    proving persistence was broken) because of this exact mistake."""
    cache_path = tmp_path / "persistent_cache.json"

    writer = URLDiscoverer.__new__(URLDiscoverer)
    writer._persistent_cache_dirty = False
    writer._persistent_cache_pending_writes = 0
    writer._persistent_cache_write_every = 25
    writer._persistent_cache_enabled = True
    writer._persistent_cache_path = str(cache_path)

    with patch.object(writer, "_fetch_direct_batch_html_rows", return_value=[{"name": "Angels Landing", "url": "https://www.alltrails.com/x"}]) as fetch_mock:
        rows = writer._get_alltrails_direct_batch_rows_for_destination("Zion National Park", "October 7-9, 2026")
    assert rows
    assert fetch_mock.call_count == 1
    # discover_all calls this explicitly at the end of every run (see
    # url_discovery.py) -- without it, nothing in _mark_persistent_cache_dirty's
    # write_every=25 threshold would flush a single write to disk.
    writer._save_persistent_caches()
    assert cache_path.exists()
    assert json.loads(cache_path.read_text(encoding="utf-8"))["direct_batch_harvest_alltrails"], (
        "sanity check: the saved JSON must actually contain the harvested row, "
        "not an empty section"
    )

    reader = URLDiscoverer.__new__(URLDiscoverer)
    reader._persistent_cache_enabled = True
    reader._persistent_cache_path = str(cache_path)
    reader._load_persistent_caches()

    with patch.object(
        reader, "_fetch_direct_batch_html_rows",
        side_effect=AssertionError("must not re-fetch: a second instance should see the persisted cache hit"),
    ):
        reader_rows = reader._get_alltrails_direct_batch_rows_for_destination("Zion National Park", "October 7-9, 2026")

    assert reader_rows == rows


def test_fetch_and_cache_grouped_direct_batch_marks_persistent_cache_dirty() -> None:
    """Same regression as above, for the new grouped-batch write path
    (_fetch_and_cache_grouped_direct_batch) -- this is the path that was
    actually running in the real dipstick55 run."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._restaurant_direct_batch_item_count = 4
    discoverer._restaurant_direct_batch_min_results = 1
    discoverer._request_cache_lock = Lock()
    discoverer._persistent_cache_dirty = False
    discoverer._persistent_cache_pending_writes = 0
    discoverer._persistent_cache_write_every = 25
    discoverer._search = MagicMock()
    discoverer._search.is_circuit_open.return_value = False
    discoverer._search.chat_completion.return_value = (
        "<h2>St. George, Utah</h2><ul>"
        "<li>Cliffside Restaurant <a href='https://www.cliffsiderestaurant.com/'>Source</a> 4.4/5 $$</li>"
        "</ul>"
        "<h2>Springdale, Utah</h2><ul>"
        "<li>Oscar's Cafe <a href='https://oscarscafe.com/'>Source</a> 4.5/5 $$</li>"
        "</ul>"
    )

    group = [
        {"name": "St. George, Utah", "dates": "October 17, 2026"},
        {"name": "Springdale, Utah", "dates": "October 18, 2026"},
    ]
    discoverer._fetch_and_cache_grouped_direct_batch(kind="restaurant", group=group)

    assert discoverer._persistent_cache_dirty is True


def test_load_persistent_caches_respects_harvest_ttl(tmp_path) -> None:
    cache_path = tmp_path / "persistent_cache.json"
    cache_path.write_text(
        json.dumps(
            {
                "version": 1,
                "updated_at": time.time(),
                "direct_batch_harvest_attractions": {
                    "stale-dest||2026-01-01|html|attraction": {
                        "ts": time.time() - (48 * 3600),
                        "rows": [{"name": "Old Attraction"}],
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    reader = URLDiscoverer.__new__(URLDiscoverer)
    reader._persistent_cache_enabled = True
    reader._persistent_cache_path = str(cache_path)
    reader._persistent_harvest_cache_ttl_hours = 24.0
    reader._load_persistent_caches()

    assert "stale-dest||2026-01-01|html|attraction" not in reader._attraction_direct_batch_cache


def test_load_persistent_caches_respects_geocode_ttl(tmp_path) -> None:
    cache_path = tmp_path / "persistent_cache.json"
    cache_path.write_text(
        json.dumps(
            {
                "version": 1,
                "updated_at": time.time(),
                "en_route_geocode": {
                    "stale|a|b": {"ts": time.time() - (800 * 3600), "lat": 37.72, "lng": -112.31},
                },
            }
        ),
        encoding="utf-8",
    )

    reader = URLDiscoverer.__new__(URLDiscoverer)
    reader._persistent_cache_enabled = True
    reader._persistent_cache_path = str(cache_path)
    reader._persistent_geocode_cache_ttl_hours = 720.0
    reader._en_route_stop_geocode_cache = {}
    reader._load_persistent_caches()

    assert "stale|a|b" not in reader._en_route_stop_geocode_cache


def test_save_persistent_caches_does_not_deadlock_when_dirty_mark_holds_request_cache_lock(tmp_path) -> None:
    """Regression for a real deadlock hazard found during the search-cache
    cost audit: _fetch_and_cache_grouped_direct_batch (~line 5928) calls
    _mark_persistent_cache_dirty() WHILE HOLDING _request_cache_lock, and
    _mark_persistent_cache_dirty() can itself trigger _save_persistent_caches()
    once write_every dirty-marks accumulate. If the save path reused
    _request_cache_lock (a plain, non-reentrant Lock) to serialize its file
    write, that exact call site would self-deadlock the first time a
    checkpoint save fires from inside it. _save_persistent_caches() must use
    its own dedicated lock instead.

    Runs the real hazard sequence on a background thread with a timeout so a
    regression hangs the test with a clear failure instead of hanging the
    whole suite forever."""
    cache_path = tmp_path / "persistent_cache.json"

    writer = URLDiscoverer.__new__(URLDiscoverer)
    writer._persistent_cache_enabled = True
    writer._persistent_cache_path = str(cache_path)
    writer._persistent_cache_dirty = False
    writer._persistent_cache_pending_writes = 0
    writer._persistent_cache_write_every = 1  # force the very next dirty-mark to save immediately
    writer._request_cache_lock = Lock()
    writer._search_results_cache = {"some query": [{"url": "https://example.com"}]}

    def _hazard_sequence() -> None:
        # Mirrors the real call site: hold _request_cache_lock, then mark dirty
        # (which, with write_every=1, immediately calls _save_persistent_caches()).
        with writer._request_cache_lock:
            writer._mark_persistent_cache_dirty()

    thread = threading.Thread(target=_hazard_sequence, daemon=True)
    thread.start()
    thread.join(timeout=5.0)

    assert not thread.is_alive(), "deadlocked: _save_persistent_caches reused _request_cache_lock"
    assert cache_path.exists()
    payload = json.loads(cache_path.read_text(encoding="utf-8"))
    assert "some query" in payload["search_results"]


def test_save_persistent_caches_survives_concurrent_saves_from_multiple_threads(tmp_path) -> None:
    """Regression for the real WinError 32/2/Errno 13 failures logged in
    tonight's dipstick69/70/72 run-console.log: _discover_attractions et al.
    run destinations concurrently via a ThreadPoolExecutor, and each worker
    thread's own periodic checkpoint save could previously race another
    thread's save on the same fixed, shared tmp path. Fires several
    concurrent saves at the same URLDiscoverer instance and asserts none of
    them raise and the final on-disk file is valid, complete JSON."""
    cache_path = tmp_path / "persistent_cache.json"

    writer = URLDiscoverer.__new__(URLDiscoverer)
    writer._persistent_cache_enabled = True
    writer._persistent_cache_path = str(cache_path)
    writer._persistent_cache_dirty = True
    writer._request_cache_lock = Lock()
    writer._search_results_cache = {f"query {i}": [{"url": f"https://example.com/{i}"}] for i in range(20)}

    errors: list[BaseException] = []

    def _save() -> None:
        try:
            writer._persistent_cache_dirty = True
            writer._save_persistent_caches()
        except BaseException as exc:  # pragma: no cover - failure path only
            errors.append(exc)

    threads = [threading.Thread(target=_save) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5.0)

    assert not any(t.is_alive() for t in threads)
    assert not errors
    assert cache_path.exists()
    payload = json.loads(cache_path.read_text(encoding="utf-8"))
    assert len(payload["search_results"]) == 20
    # No stray per-attempt tmp files left behind after a successful save.
    assert list(tmp_path.glob("*.tmp")) == []


def test_search_attraction_authoritative_prefers_strong_match_over_weak_anchor_match() -> None:
    """Corroboration/disambiguation regression: when a batch has one row that
    fully matches the item's tokens (strength 2) and a second, different row
    that only weakly matches via a single shared short anchor word (strength 1,
    e.g. two different '* Temple' rows sharing only 'temple' once the
    destination-name token is excluded), the strong match must win outright --
    the weak row's candidates must not even be pooled into the tie-break."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._direct_batch_authoritative = True
    rows = [
        {
            "name": "St. George Temple",
            "title": "St. George Temple",
            "url": "https://www.churchofjesuschrist.org/temples/details/st-george-temple",
            "snippet": "St. George Temple Links: https://www.churchofjesuschrist.org/temples/details/st-george-temple",
        },
        {
            "name": "Red Cliffs Temple",
            "title": "Red Cliffs Temple",
            "url": "https://www.churchofjesuschrist.org/temples/details/red-cliffs-temple",
            "snippet": "Red Cliffs Temple Links: https://www.churchofjesuschrist.org/temples/details/red-cliffs-temple",
        },
    ]

    with patch.object(discoverer, "_get_attraction_direct_batch_rows_for_destination", return_value=rows):
        with patch.object(discoverer, "_retain_discovered_url", side_effect=lambda url, *a, **k: url):
            out = discoverer._search_attraction_from_direct_batch(
                "St. George Temple", "St. George, Utah", "October 17, 2026"
            )

    assert out == "https://www.churchofjesuschrist.org/temples/details/st-george-temple"


def test_search_attraction_authoritative_prefers_exact_name_over_shared_prefix_row() -> None:
    """Regression for dipstick59: real Zion National Park attraction-batch rows
    (parsed from the actual harvested HTML for this run) included both
    "Zion Canyon Scenic Drive" (nps.gov/zion/planyourvisit/scenicdrive.htm,
    the correct answer) and "Zion Canyon Visitor Center" (a different,
    unrelated page). Both share the words "Zion" and "Canyon" with the
    requested item, and the old single "strong" match tier (2) let a
    same-destination row that merely overlaps on those two generic,
    non-distinctive words tie with -- and, via a Google Maps candidate,
    beat -- the row that is an exact, word-for-word title match. The
    rendered trip literally linked "Zion Canyon Scenic Drive" to a Google
    Maps search for "Zion Canyon Visitor Center Springdale UT 84767"
    instead of its own real, harvested, item-specific NPS scenic-drive page.

    A third real row in the same batch ("Kolob Canyons Visitor Center")
    compounded the problem: its snippet embeds
    "Links: https://www.nps.gov/zion/planyourvisit/visitorcenters.htm ...",
    and that embedded URL text alone contains "zion" as a substring (every
    NPS Zion page's URL does), which combined with "canyon" being a
    substring of its own title's "Canyons" was enough to also reach the old
    "strong" match tier despite the row having nothing to do with the
    requested item.

    Fix: an exact/full row-name match (every word of the item's name --
    minus generic suffixes -- present in the row's own declared name) is
    now its own, higher match-strength tier (3) that outranks the generic
    blob/URL-text overlap tier (2), so a same-destination row sharing only
    generic words can no longer tie with -- or beat -- the row that
    actually is the requested item.
    """
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._direct_batch_authoritative = True

    rows = [
        {
            "title": "Zion Canyon Visitor Center",
            "name": "Zion Canyon Visitor Center",
            "url": "https://www.nps.gov/zion/planyourvisit/visitorcenters.htm",
            "maps_url": "https://www.google.com/maps/search/?api=1&query=Zion+Canyon+Visitor+Center+Springdale+UT+84767",
            "snippet": (
                "Zion Canyon Visitor Center Source Maps 4.6/5 Gateway for maps, "
                "shuttles, and park orientation. Links: "
                "https://www.nps.gov/zion/planyourvisit/visitorcenters.htm "
                "https://www.google.com/maps/search/?api=1&query=Zion+Canyon+Visitor+Center+Springdale+UT+84767"
            ),
        },
        {
            "title": "Kolob Canyons Visitor Center",
            "name": "Kolob Canyons Visitor Center",
            "url": "https://www.nps.gov/zion/planyourvisit/visitorcenters.htm",
            "maps_url": "https://www.google.com/maps/search/?api=1&query=Kolob+Canyons+Visitor+Center+UT",
            "snippet": (
                "Kolob Canyons Visitor Center Source Maps 4.5/5 Hub for the "
                "quieter northwest park section. Links: "
                "https://www.nps.gov/zion/planyourvisit/visitorcenters.htm "
                "https://www.google.com/maps/search/?api=1&query=Kolob+Canyons+Visitor+Center+UT"
            ),
        },
        {
            "title": "Zion Canyon Scenic Drive",
            "name": "Zion Canyon Scenic Drive",
            "url": "https://www.nps.gov/zion/planyourvisit/scenicdrive.htm",
            "maps_url": "https://www.google.com/maps/search/?api=1&query=Zion+Canyon+Scenic+Drive+Springdale+UT",
            "snippet": (
                "Zion Canyon Scenic Drive Source Maps 4.8/5 Relaxed drive with "
                "stunning canyon vistas and pullouts. Links: "
                "https://www.nps.gov/zion/planyourvisit/scenicdrive.htm "
                "https://www.google.com/maps/search/?api=1&query=Zion+Canyon+Scenic+Drive+Springdale+UT"
            ),
        },
    ]

    strengths = {
        row["title"]: URLDiscoverer._direct_batch_row_match_strength(
            row, "Zion Canyon Scenic Drive", "Zion National Park"
        )
        for row in rows
    }
    assert strengths["Zion Canyon Scenic Drive"] == 3
    assert strengths["Zion Canyon Visitor Center"] == 2
    assert strengths["Kolob Canyons Visitor Center"] == 2

    with patch.object(discoverer, "_get_attraction_direct_batch_rows_for_destination", return_value=rows):
        with patch.object(discoverer, "_retain_discovered_url", side_effect=lambda url, *a, **k: url):
            out = discoverer._search_attraction_from_direct_batch(
                "Zion Canyon Scenic Drive", "Zion National Park", "October 18, 2026"
            )

    assert out == "https://www.nps.gov/zion/planyourvisit/scenicdrive.htm"


def test_search_attraction_authoritative_does_not_fabricate_match_from_unrelated_row() -> None:
    """When no row in the destination batch actually matches the requested item,
    an unrelated attraction's specific URL must not be selected. Direct-batch
    authority must not leak a different attraction's link (mirrors the analogous
    restaurant-side fix)."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._direct_batch_authoritative = True

    rows = [
        {
            "title": "Kolob Canyons Viewpoint",
            "name": "Kolob Canyons Viewpoint",
            "url": "https://www.nps.gov/zion/planyourvisit/kolob-canyons.htm",
            "snippet": "Kolob Canyons Viewpoint scenic overlook",
        },
    ]

    with patch.object(discoverer, "_get_attraction_direct_batch_rows_for_destination", return_value=rows):
        with patch.object(discoverer, "_retain_discovered_url", side_effect=lambda url, *_args, **_kwargs: url):
            out = discoverer._search_attraction_from_direct_batch("Zion Human History Museum", "Zion National Park", "October 18, 2026")

    assert out != "https://www.nps.gov/zion/planyourvisit/kolob-canyons.htm"


def test_search_attraction_from_direct_batch_prefers_google_maps_place_over_alternate():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._direct_batch_authoritative = False
    discoverer._url_validator = MagicMock()
    discoverer._maps_url_resolution_cache = {}
    discoverer._fetch_final_url_cache = {}

    rows = [
        {
            "url": "https://example.org/official/mesa-overlook",
            "title": "Mesa Overlook",
            "snippet": "Official attraction page",
        },
        {
            "url": "https://www.google.com/maps/place/Mesa+Overlook/@37.2,-112.9,17z/data=!4m6!3m5!1s0x80cac2f9e17f7c3f:0x1234!8m2!3d37.2!4d-112.9",
            "title": "Mesa Overlook",
            "snippet": "Maps place entry",
        },
    ]

    discoverer._url_validator.session.get.side_effect = lambda _url, timeout=8: MagicMock(url=_url)

    with patch.object(discoverer, "_get_attraction_direct_batch_rows_for_destination", return_value=rows):
        with patch.object(discoverer, "_retain_discovered_url", side_effect=lambda url, *_args, **_kwargs: url):
            out = discoverer._search_attraction_from_direct_batch("Mesa Overlook", "Zion National Park", "October 7-9, 2026")

    assert out.startswith("https://www.google.com/maps/place/")


def test_search_restaurant_direct_batch_authoritative_uses_source_when_row_maps_query_is_rejected():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._direct_batch_authoritative = True

    rows = [
        {
            "name": "The Shed",
            "url": "https://www.google.com/maps/search/?api=1&query=The+Shed+Santa+Fe+NM",
            "snippet": "The Shed Links: https://sfshed.com/ https://www.google.com/maps/search/?api=1&query=The+Shed+Santa+Fe+NM",
        }
    ]

    with patch.object(discoverer, "_get_restaurant_direct_batch_rows_for_destination", return_value=rows):
        with patch.object(discoverer, "_retain_discovered_url", side_effect=lambda url, *_args, **_kwargs: ("" if "maps/search" in url else url)):
            out = discoverer._search_restaurant_from_direct_batch("The Shed", "Santa Fe", "October 18-20, 2026")

    assert out == "https://sfshed.com/"


def test_search_restaurant_from_direct_batch_prefers_google_maps_place_over_alternate():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._direct_batch_authoritative = False
    discoverer._url_validator = MagicMock()
    discoverer._maps_url_resolution_cache = {}
    discoverer._fetch_final_url_cache = {}

    rows = [
        {
            "url": "https://www.tripadvisor.com/Restaurant_Review-g57119-d9999-Reviews-Example-St_George_Utah.html",
            "title": "Example Cafe",
            "snippet": "TripAdvisor listing",
        },
        {
            "url": "https://www.google.com/maps/place/Example+Cafe/@37.1,-113.5,17z/data=!4m6!3m5!1s0x80ca1234abcd5678:0xbeef!8m2!3d37.1!4d-113.5",
            "title": "Example Cafe",
            "snippet": "Maps place entry",
        },
    ]

    discoverer._url_validator.session.get.side_effect = lambda _url, timeout=8: MagicMock(url=_url)

    with patch.object(discoverer, "_get_restaurant_direct_batch_rows_for_destination", return_value=rows):
        with patch.object(discoverer, "_retain_discovered_url", side_effect=lambda url, *_args, **_kwargs: url):
            out = discoverer._search_restaurant_from_direct_batch("Example Cafe", "St. George", "October 7-9, 2026")

    assert out.startswith("https://www.google.com/maps/place/")


def test_search_restaurant_from_direct_batch_falls_back_to_source_when_maps_missing():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._direct_batch_authoritative = False

    rows = [
        {
            "name": "Painted Pony",
            "url": "",
            "snippet": "Painted Pony Links: https://www.painted-pony.com/",
        }
    ]

    with patch.object(discoverer, "_get_restaurant_direct_batch_rows_for_destination", return_value=rows):
        with patch.object(discoverer, "_retain_discovered_url", side_effect=lambda url, *_args, **_kwargs: url):
            out = discoverer._search_restaurant_from_direct_batch("Painted Pony", "St. George, Utah", "October 7-9, 2026")

    assert out == "https://www.painted-pony.com/"


def test_search_restaurant_non_authoritative_rejects_tripadvisor_area_listing() -> None:
    """Generic area listing must be dropped even in non-authoritative ranked path."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._direct_batch_authoritative = False

    rows = [
        {
            "name": "Oscar's Cafe",
            "url": "https://www.tripadvisor.com/Restaurants-g60771-Zion_National_Park_Utah.html",
            "snippet": "Oscar's Cafe best restaurant in Springdale",
        }
    ]

    with patch.object(discoverer, "_get_restaurant_direct_batch_rows_for_destination", return_value=rows):
        out = discoverer._search_restaurant_from_direct_batch("Oscar's Cafe", "Zion National Park", "October 18, 2026")

    assert out is None or (out is not None and "Restaurants-g" not in out)


def test_search_restaurant_non_authoritative_prefers_maps_place_over_generic_non_maps() -> None:
    """A specific Maps place URL should win over a non-maps generic area page."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._direct_batch_authoritative = False
    discoverer._maps_url_resolution_cache = {}
    discoverer._fetch_final_url_cache = {}
    discoverer._url_validator = MagicMock()
    discoverer._url_validator.session.get.return_value = MagicMock(
        url="https://www.google.com/maps/place/Oscar's+Cafe/@37.1882,-112.9983,17z/data=!4m6!3m5!1s0x80cac2f8:0x5678!8m2!3d37.1882!4d-112.9983"
    )

    rows = [
        {
            "name": "Oscar's Cafe",
            "url": "https://maps.app.goo.gl/rbaK8ZtvD67ZAjNa7",
            "snippet": "Oscar's Cafe Springdale Utah",
        },
        {
            "name": "Oscar's Cafe",
            "url": "https://www.tripadvisor.com/Restaurants-g60771-Zion_National_Park_Utah.html",
            "snippet": "Best restaurants near Zion",
        },
    ]

    with patch.object(discoverer, "_get_restaurant_direct_batch_rows_for_destination", return_value=rows):
        out = discoverer._search_restaurant_from_direct_batch("Oscar's Cafe", "Zion National Park", "October 18, 2026")

    # If the maps place resolves, it should be preferred over the area listing.
    if out is not None:
        assert "Restaurants-g" not in out


def test_search_restaurant_authoritative_rejects_other_row_urls_when_matching_item_name() -> None:
    """Authoritative direct-batch rows must stay scoped to the requested restaurant item."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._direct_batch_authoritative = True

    rows = [
        {
            "title": "Cafe Soleil",
            "name": "Cafe Soleil",
            "url": "https://cafesoleilzion.com",
            "snippet": "Cafe Soleil 4.7/5 $$",
        },
        {
            "title": "Oscar's Cafe",
            "name": "Oscar's Cafe",
            "url": "https://www.tripadvisor.com/Restaurant_Review-g29122-d456789-Oscar_s_Cafe-Springdale_Utah.html",
            "snippet": "Oscar's Cafe 4.5/5 $$",
        },
    ]

    with patch.object(discoverer, "_get_restaurant_direct_batch_rows_for_destination", return_value=rows):
        with patch.object(discoverer, "_retain_discovered_url", side_effect=lambda url, *_args, **_kwargs: url):
            out = discoverer._search_restaurant_from_direct_batch("Oscar's Cafe", "Zion National Park", "October 18, 2026")

    assert out == "https://www.tripadvisor.com/Restaurant_Review-g29122-d456789-Oscar_s_Cafe-Springdale_Utah.html"


def test_search_restaurant_authoritative_does_not_fabricate_match_from_unrelated_row() -> None:
    """When no row in the batch actually matches the requested item, an unrelated
    row's specific (non-generic-looking) URL must not be silently treated as a
    match. Direct-batch authority must not leak a different restaurant's link."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._direct_batch_authoritative = True

    rows = [
        {
            "title": "Zion Pizza & Noodle Co",
            "name": "Zion Pizza & Noodle Co",
            "url": "https://zionpizzanoodle.com",
            "snippet": "Zion Pizza & Noodle Co 4.4/5 $$",
        },
    ]

    with patch.object(discoverer, "_get_restaurant_direct_batch_rows_for_destination", return_value=rows):
        with patch.object(discoverer, "_retain_discovered_url", side_effect=lambda url, *_args, **_kwargs: url):
            out = discoverer._search_restaurant_from_direct_batch("Bit & Spur", "Zion National Park", "October 18, 2026")

    assert out != "https://zionpizzanoodle.com"


def test_audit_emits_audit_rejection_event_for_restaurant_generic_url() -> None:
    """Audit stripping a restaurant URL should emit an audit_url_rejected broker event."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._decision_threads_by_destination = {}
    discoverer._decision_stats_by_destination = {}
    discoverer._decision_source_stats_by_destination = {}
    discoverer._decision_event_sequence = 0
    discoverer._request_cache_lock = __import__("threading").Lock()
    discoverer._direct_batch_authoritative = False
    discoverer._direct_batch_authoritative_urls = set()

    trip = {
        "destinations": [
            {
                "id": "zion",
                "name": "Zion National Park",
                "ai_content": {
                    "top_attractions": [],
                    "getting_here": {"en_route_stops": []},
                    "dinner_recommendations": [
                        {
                            "name": "Oscar's Cafe",
                            "url": "https://www.tripadvisor.com/Restaurants-g60771-Zion_National_Park_Utah.html",
                        }
                    ],
                },
                "scenic_drives": [],
                "cultural_events": {"events": []},
            }
        ]
    }

    with patch.object(discoverer, "_prewarm_url_validation_cache", return_value=None):
        discoverer.audit_discovered_urls(trip)

    threads = discoverer._decision_threads_by_destination.get("Zion National Park", {})
    all_events = [ev for evs in threads.values() for ev in evs if isinstance(ev, dict)]
    audit_events = [ev for ev in all_events if "audit_url_rejected" in str(ev.get("reason", ""))]
    assert len(audit_events) >= 1, f"Expected an audit_url_rejected event; got: {all_events}"
    assert audit_events[0]["item"] == "Oscar's Cafe"


    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._url_validator = MagicMock()
    discoverer._url_validator.session.get.return_value = MagicMock(
        status_code=200,
        text="Bear Creek Trail Colorado hiking route details and reviews",
    )

    url = "https://www.alltrails.com/trail/us/colorado/bear-creek-trail"
    assert discoverer._is_relevant_result(url, "Bear Creek Trail", "Telluride")


def test_search_first_alltrails_can_use_variant_beyond_default_attempt_limit():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._search = MagicMock()
    discoverer._url_validator = MagicMock()
    discoverer._url_validator.session.get.return_value = MagicMock(
        status_code=200,
        text="Jud Wiebe Trail Colorado hiking details and route",
    )

    call_count = {"n": 0}

    def fake_search(_query, count=10):
        call_count["n"] += 1
        if call_count["n"] == 5:
            return [{"url": "https://www.alltrails.com/trail/us/colorado/jud-wiebe-trail"}]
        return []

    discoverer._search.search.side_effect = fake_search

    variants = [
        '"Jud Wiebe Trail" Telluride trail hiking',
        '"Jud Wiebe Trail" Telluride',
        'Jud Wiebe Trail Telluride trail',
        'Jud Wiebe Trail Telluride',
        '"Jud Wiebe Trail" trail',
        'Jud Wiebe Trail',
    ]

    result = discoverer._search_first(
        variants,
        site_filter="alltrails.com",
        item_name="Jud Wiebe Trail",
        dest_name="Telluride",
        max_attempts=len(variants),
    )

    assert result == "https://www.alltrails.com/trail/us/colorado/jud-wiebe-trail"
    assert call_count["n"] >= 5


def test_search_strict_accepts_alltrails_with_strong_metadata_when_page_fetch_fails():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._search = MagicMock()
    discoverer._url_validator = MagicMock()
    discoverer._url_validator.session.get.side_effect = Exception("timeout")
    discoverer._search.search.return_value = [
        {
            "url": "https://www.alltrails.com/trail/us/colorado/jud-wiebe-memorial-trail",
            "name": "Jud Wiebe Memorial Trail, Colorado - 4,059 Reviews, Map | AllTrails",
            "snippet": "Popular hiking trail near Telluride with route details and reviews.",
        }
    ]

    result = discoverer._search_first_strict(
        query_variants=['"Jud Wiebe Trail" Telluride trail hiking'],
        site_filter="alltrails.com",
        site_hint=None,
        item_name="Jud Wiebe Trail",
        dest_name="Telluride",
        allow_alltrails=True,
    )

    assert result == "https://www.alltrails.com/trail/us/colorado/jud-wiebe-memorial-trail"


def test_search_strict_rejects_alltrails_on_fetch_failure_without_metadata_when_slug_matches():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._search = MagicMock()
    discoverer._url_validator = MagicMock()
    discoverer._url_validator.session.get.side_effect = Exception("timeout")
    discoverer._search.search.return_value = [
        {
            "url": "https://www.alltrails.com/trail/us/utah/angels-landing-trail",
        }
    ]

    result = discoverer._search_first_strict(
        query_variants=['"Angels Landing" Zion National Park trail hiking'],
        site_filter="alltrails.com",
        site_hint=None,
        item_name="Angel's Landing",
        dest_name="Zion National Park",
        allow_alltrails=True,
    )

    assert result is None


def test_search_strict_rejects_alltrails_on_401_without_candidate_metadata():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._search = MagicMock()
    discoverer._url_validator = MagicMock()
    discoverer._search.search.return_value = [
        {
            "url": "https://www.alltrails.com/trail/us/utah/angels-landing-trail",
        }
    ]

    with patch.object(discoverer, "_fetch_page_text", return_value=(False, 401, "")):
        result = discoverer._search_first_strict(
            query_variants=['"Angels Landing" Zion National Park trail hiking'],
            site_filter="alltrails.com",
            site_hint=None,
            item_name="Angel's Landing",
            dest_name="Zion National Park",
            allow_alltrails=True,
        )

    assert result is None


def test_search_strict_rejects_single_token_alltrails_on_403_without_destination_metadata():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._search = MagicMock()
    discoverer._url_validator = MagicMock()
    discoverer._search.search.return_value = [
        {
            "url": "https://www.alltrails.com/trail/us/utah/the-narrows-top-down",
            "name": "The Narrows Top Down - 500 reviews | AllTrails",
            "snippet": "Popular hiking route with permits and river crossing notes.",
        }
    ]

    with patch.object(discoverer, "_fetch_page_text", return_value=(False, 403, "")):
        result = discoverer._search_first_strict(
            query_variants=['"The Narrows" Zion National Park trail hiking'],
            site_filter="alltrails.com",
            site_hint=None,
            item_name="The Narrows",
            dest_name="Zion National Park",
            allow_alltrails=True,
        )

    assert result is None


def test_search_strict_accepts_single_token_alltrails_on_403_with_destination_metadata():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._search = MagicMock()
    discoverer._url_validator = MagicMock()
    discoverer._search.search.return_value = [
        {
            "url": "https://www.alltrails.com/trail/us/utah/the-narrows-top-down",
            "name": "The Narrows Top Down in Zion National Park | AllTrails",
            "snippet": "Classic Zion river hike with permit logistics and shuttle context.",
        }
    ]

    with patch.object(discoverer, "_fetch_page_text", return_value=(False, 403, "")):
        result = discoverer._search_first_strict(
            query_variants=['"The Narrows" Zion National Park trail hiking'],
            site_filter="alltrails.com",
            site_hint=None,
            item_name="The Narrows",
            dest_name="Zion National Park",
            allow_alltrails=True,
        )

    assert result == "https://www.alltrails.com/trail/us/utah/the-narrows-top-down"


def test_search_strict_rejects_blocked_alltrails_when_config_disabled():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._search = MagicMock()
    discoverer._url_validator = MagicMock()
    discoverer._allow_blocked_alltrails = False
    discoverer._search.search.return_value = [
        {
            "url": "https://www.alltrails.com/trail/us/utah/the-narrows-top-down",
            "name": "The Narrows Top Down in Zion National Park | AllTrails",
            "snippet": "Classic Zion river hike with permit logistics and shuttle context.",
        }
    ]

    with patch.object(discoverer, "_fetch_page_text", return_value=(False, 403, "")):
        result = discoverer._search_first_strict(
            query_variants=['"The Narrows" Zion National Park trail hiking'],
            site_filter="alltrails.com",
            site_hint=None,
            item_name="The Narrows",
            dest_name="Zion National Park",
            allow_alltrails=True,
        )

    assert result is None


def test_alltrails_rejects_candidate_when_miles_exceed_configured_max():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._max_trail_miles = 3.0

    candidate = {
        "url": "https://www.alltrails.com/trail/us/utah/fairyland-loop-trail-bryce-canyon-national-park",
        "name": "Fairyland Loop Trail - Bryce Canyon National Park",
        "snippet": "8.0 mile heavily trafficked loop trail in Bryce Canyon National Park",
    }

    with patch.object(discoverer, "_fetch_page_text", return_value=(False, 403, "")):
        ok = discoverer._is_relevant_result(
            candidate["url"],
            "Fairyland Loop Trail",
            "Bryce Canyon National Park",
            candidate=candidate,
        )

    assert ok is False


def test_alltrails_rejects_candidate_when_km_exceed_configured_max():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._max_trail_miles = 3.0

    candidate = {
        "url": "https://www.alltrails.com/trail/us/utah/fairyland-loop-trail",
        "name": "Fairyland Loop Trail",
        "snippet": "12.9 km loop trail near Bryce Canyon National Park",
    }

    with patch.object(discoverer, "_fetch_page_text", return_value=(False, 403, "")):
        ok = discoverer._is_relevant_result(
            candidate["url"],
            "Fairyland Loop Trail",
            "Bryce Canyon National Park",
            candidate=candidate,
        )

    assert ok is False


def test_alltrails_accepts_candidate_when_miles_within_configured_max():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._max_trail_miles = 3.0
    discoverer._allow_blocked_alltrails = True

    candidate = {
        "url": "https://www.alltrails.com/trail/us/utah/canyon-overlook-trail",
        "name": "Canyon Overlook Trail",
        "snippet": "1.0 mile out and back trail in Zion National Park",
    }

    with patch.object(discoverer, "_fetch_page_text", return_value=(False, 403, "")):
        ok = discoverer._is_relevant_result(
            candidate["url"],
            "Canyon Overlook Trail",
            "Zion National Park",
            candidate=candidate,
        )

    assert ok is True


def test_extract_trail_miles_parses_hyphenated_distance_formats():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    assert discoverer._extract_trail_miles("A 5-mile hike with steep switchbacks.") == 5.0
    assert discoverer._extract_trail_miles("Approx. 5 mi round-trip route.") == 5.0
    assert discoverer._extract_trail_miles("A 12.9-km scenic loop.") == pytest.approx(8.0157, rel=1e-4)


def test_alltrails_rejects_when_fetched_page_km_distance_exceeds_threshold():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._max_trail_miles = 3.0

    with patch.object(
        discoverer,
        "_fetch_page_text",
        return_value=(True, 200, "Fairyland Loop Trail is a 12.9 km loop near Bryce Canyon."),
    ):
        ok = discoverer._is_relevant_result(
            "https://www.alltrails.com/trail/us/utah/fairyland-loop-trail",
            "Fairyland Loop Trail",
            "Bryce Canyon National Park",
            candidate={"name": "Fairyland Loop Trail", "snippet": "Bryce Canyon loop"},
        )

    assert ok is False


def test_search_alltrails_direct_batch_authoritative_accepts_first_available_trail_link():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._direct_batch_authoritative = True

    rows = [
        {
            "url": "https://www.alltrails.com/trail/us/utah/cassidy-arch-trail",
            "title": "Cassidy Arch Trail",
            "snippet": "Trail option",
        }
    ]

    with patch.object(discoverer, "_get_alltrails_direct_batch_rows_for_destination", return_value=rows):
        out = discoverer._search_alltrails_for_trail_from_direct_batch(
            "Cassidy Arch",
            "Capitol Reef National Park",
            "October 21, 2026",
        )

    assert out == "https://www.alltrails.com/trail/us/utah/cassidy-arch-trail"


def test_alltrails_direct_batch_html_prompt_includes_length_and_rating_constraints():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._max_trail_miles = 3.0
    discoverer._alltrails_rating_min = 4.5
    discoverer._alltrails_rating_min_votes = 200

    prompt = discoverer._direct_batch_html_prompt(
        kind="trail",
        dest_name="Zion National Park",
        dates="October 7-9, 2026",
    )

    assert prompt is not None
    system_prompt, user_prompt = prompt
    assert "AllTrails" in system_prompt
    assert "3" in system_prompt
    assert "4.5+" in system_prompt
    assert "200 reviews" in system_prompt
    assert "rating" in system_prompt.lower()
    assert user_prompt.startswith("Generate clickable hikes from AllTrails for Zion National Park (October 7-9, 2026).")
    assert "rating" in user_prompt.lower()


def test_get_alltrails_direct_batch_rows_prefers_html_capture_before_search_fallback():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._alltrails_direct_batch_cache = {}

    html_rows = [{"url": "https://www.alltrails.com/trail/us/utah/angels-landing-trail", "name": "Angels Landing"}]

    with patch.object(discoverer, "_get_direct_batch_html_rows_for_destination", return_value=html_rows) as html_mock, patch.object(
        discoverer,
        "_get_direct_batch_rows_for_destination",
        side_effect=AssertionError("search fallback should not run when trail html rows exist"),
    ):
        rows = discoverer._get_alltrails_direct_batch_rows_for_destination("Zion National Park", "October 7-9, 2026")

    assert rows == html_rows
    html_mock.assert_called_once()


def test_direct_batch_html_prompt_for_attractions_uses_precise_maps_guidance():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)

    prompt = discoverer._direct_batch_html_prompt(
        kind="attraction",
        dest_name="Zion National Park",
        dates="October 7-9, 2026",
    )

    assert prompt is not None
    system_prompt, _user_prompt = prompt
    assert "precise google maps place or search link" in system_prompt.lower()
    assert "generic destination listing pages" in system_prompt.lower()


def test_direct_batch_html_prompt_multi_covers_each_destination_with_own_count():
    """The multi-destination prompt builder must list every destination with
    its own day-scaled item count in the user prompt, and instruct the model
    to keep each destination's items in its own <h2> section."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)

    prompt = discoverer._direct_batch_html_prompt_multi(
        kind="restaurant",
        destinations=[
            ("St. George, Utah", "October 17, 2026"),
            ("Springdale, Utah", "October 18, 2026"),
        ],
    )

    assert prompt is not None
    system_prompt, user_prompt = prompt
    assert "<h2>" in system_prompt
    assert "do not mix items between destinations" in system_prompt.lower()
    assert "St. George, Utah" in user_prompt
    assert "Springdale, Utah" in user_prompt


def test_direct_batch_html_prompt_multi_excludes_en_route_stop():
    """en_route_stop depends on per-destination origin/route context that
    doesn't fit the shared multi-destination shape -- must return None so
    callers keep it on the original one-call-per-destination path."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)

    prompt = discoverer._direct_batch_html_prompt_multi(
        kind="en_route_stop",
        destinations=[("Zion National Park", "October 7, 2026"), ("Moab", "October 8, 2026")],
    )

    assert prompt is None


def test_split_multi_destination_html_separates_sections_by_h2():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    html = (
        "<h2>St. George, Utah</h2><ul><li>Cliffside</li><li>Painted Pony</li></ul>"
        "<h2>Springdale, Utah</h2><ul><li>Oscar's Cafe</li></ul>"
    )

    sections = discoverer._split_multi_destination_html(html)

    assert [header for header, _body in sections] == ["St. George, Utah", "Springdale, Utah"]
    assert "Cliffside" in sections[0][1]
    assert "Painted Pony" in sections[0][1]
    assert "Cliffside" not in sections[1][1]
    assert "Oscar's Cafe" in sections[1][1]


def test_split_multi_destination_html_returns_empty_for_no_headers():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)

    assert discoverer._split_multi_destination_html("<ul><li>no header here</li></ul>") == []
    assert discoverer._split_multi_destination_html("") == []


def test_match_destination_section_exact_then_fuzzy():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    sections = [("St. George, Utah", "a"), ("Springdale, Utah", "b")]

    assert discoverer._match_destination_section("St. George, Utah", sections) == 0
    assert discoverer._match_destination_section("Springdale, Utah", sections) == 1
    # Tolerates the model dropping/adding minor formatting around the name.
    assert discoverer._match_destination_section("St. George", sections) == 0
    assert discoverer._match_destination_section("Nonexistent Place", sections) is None


def test_prefetch_grouped_direct_batch_noop_when_group_size_is_one():
    """group_size=1 (the safe-rollback value) must not fire any grouped
    calls at all -- exercised via a client whose chat_completion would raise
    if grouping incorrectly fired."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._direct_batch_group_size = 1
    discoverer._search = MagicMock()
    discoverer._search.chat_completion.side_effect = AssertionError("must not be called when group_size=1")

    discoverer._prefetch_grouped_direct_batch(
        [{"name": "Zion National Park", "dates": "October 7, 2026"}, {"name": "Moab", "dates": "October 8, 2026"}]
    )

    discoverer._search.chat_completion.assert_not_called()


def test_prefetch_grouped_direct_batch_noop_with_fewer_than_two_destinations():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._direct_batch_group_size = 2
    discoverer._search = MagicMock()
    discoverer._search.chat_completion.side_effect = AssertionError("must not be called with <2 destinations")

    discoverer._prefetch_grouped_direct_batch([{"name": "Zion National Park", "dates": "October 7, 2026"}])

    discoverer._search.chat_completion.assert_not_called()


def test_fetch_and_cache_grouped_direct_batch_populates_per_destination_caches():
    """End-to-end (mocked network) check: a single grouped call's combined
    HTML response must be split and land in the SAME per-kind cache dict
    and SAME cache key shape the existing single-destination getters check,
    so real call sites get transparent cache hits."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._restaurant_direct_batch_item_count = 4
    discoverer._restaurant_direct_batch_min_results = 1
    discoverer._request_cache_lock = Lock()

    combined_html = (
        "<h2>St. George, Utah</h2><ul>"
        "<li>Cliffside Restaurant <a href='https://www.cliffsiderestaurant.com/'>Source</a> 4.4/5 $$</li>"
        "</ul>"
        "<h2>Springdale, Utah</h2><ul>"
        "<li>Oscar's Cafe <a href='https://oscarscafe.com/'>Source</a> 4.5/5 $$</li>"
        "</ul>"
    )
    discoverer._search = MagicMock()
    discoverer._search.chat_completion.return_value = combined_html
    discoverer._search.is_circuit_open.return_value = False

    group = [
        {"name": "St. George, Utah", "dates": "October 17, 2026"},
        {"name": "Springdale, Utah", "dates": "October 18, 2026"},
    ]
    discoverer._fetch_and_cache_grouped_direct_batch(kind="restaurant", group=group)

    key1 = discoverer._batch_cache_key("St. George, Utah", "October 17, 2026|html|restaurant")
    key2 = discoverer._batch_cache_key("Springdale, Utah", "October 18, 2026|html|restaurant")
    assert discoverer._restaurant_direct_batch_cache[key1][0]["title"] == "Cliffside Restaurant"
    assert discoverer._restaurant_direct_batch_cache[key2][0]["title"] == "Oscar's Cafe"

    # And the existing single-destination getter must see this as a cache hit.
    with patch.object(
        discoverer, "_get_direct_batch_rows_for_destination", side_effect=AssertionError("must not re-fetch on cache hit")
    ):
        rows = discoverer._get_restaurant_direct_batch_rows_for_destination("St. George, Utah", "October 17, 2026")
    assert rows[0]["title"] == "Cliffside Restaurant"


def test_fetch_and_cache_grouped_direct_batch_forwards_seeds_for_attractions_and_trails():
    """The grouped path is the default in production (DEFAULT_DIRECT_BATCH_
    GROUP_SIZE=2), so it must build a destination-name -> seeds map from
    each group member's manifest `seeds` and pass it into
    _direct_batch_html_prompt_multi for attraction/trail kinds -- otherwise
    the grouped call would silently bypass the seed-hint fix entirely and a
    seed would never reach the harvest prompt whenever grouping is active."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    group = [
        {"name": "St. George, Utah", "dates": "October 17, 2026", "seeds": ["Snow Canyon Overlook"]},
        {"name": "Springdale, Utah", "dates": "October 18, 2026"},
    ]

    with patch.object(discoverer, "_direct_batch_html_prompt_multi", return_value=None) as mock_prompt:
        discoverer._fetch_and_cache_grouped_direct_batch(kind="attraction", group=group)

    mock_prompt.assert_called_once_with(
        kind="attraction",
        destinations=[("St. George, Utah", "October 17, 2026"), ("Springdale, Utah", "October 18, 2026")],
        seed_names_by_destination={"St. George, Utah": ["Snow Canyon Overlook"], "Springdale, Utah": []},
    )


def test_fetch_and_cache_grouped_direct_batch_omits_seed_map_for_restaurants():
    """Restaurants have no seed concept (manifest `seeds` is documented as
    attraction/hike/experience hints only) -- the grouped restaurant call
    must not pass a seed map at all."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    group = [{"name": "St. George, Utah", "dates": "October 17, 2026", "seeds": ["Snow Canyon Overlook"]}]

    with patch.object(discoverer, "_direct_batch_html_prompt_multi", return_value=None) as mock_prompt:
        discoverer._fetch_and_cache_grouped_direct_batch(kind="restaurant", group=group)

    mock_prompt.assert_called_once_with(
        kind="restaurant",
        destinations=[("St. George, Utah", "October 17, 2026")],
        seed_names_by_destination=None,
    )


def test_fetch_and_cache_grouped_direct_batch_leaves_thin_destination_uncached():
    """A destination whose section parses to fewer rows than
    _direct_batch_min_required must NOT be cached -- it should fall through
    to a real single-destination retry rather than locking in a too-thin
    result."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._restaurant_direct_batch_item_count = 4
    discoverer._restaurant_direct_batch_min_results = 2
    discoverer._request_cache_lock = Lock()

    combined_html = (
        "<h2>St. George, Utah</h2><ul>"
        "<li>Cliffside Restaurant <a href='https://www.cliffsiderestaurant.com/'>Source</a> 4.4/5 $$</li>"
        "</ul>"
        "<h2>Springdale, Utah</h2><ul></ul>"
    )
    discoverer._search = MagicMock()
    discoverer._search.chat_completion.return_value = combined_html
    discoverer._search.is_circuit_open.return_value = False

    group = [
        {"name": "St. George, Utah", "dates": "October 17, 2026"},
        {"name": "Springdale, Utah", "dates": "October 18, 2026"},
    ]
    discoverer._fetch_and_cache_grouped_direct_batch(kind="restaurant", group=group)

    key1 = discoverer._batch_cache_key("St. George, Utah", "October 17, 2026|html|restaurant")
    key2 = discoverer._batch_cache_key("Springdale, Utah", "October 18, 2026|html|restaurant")
    # St. George only got 1 row < min_results=2, so it must also be left uncached.
    assert key1 not in discoverer._restaurant_direct_batch_cache
    assert key2 not in discoverer._restaurant_direct_batch_cache


def test_fetch_and_cache_grouped_direct_batch_handles_empty_response_gracefully():
    """A failed/empty grouped call must not raise and must leave the cache
    untouched, so every destination falls through to the normal
    single-destination path."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._restaurant_direct_batch_item_count = 4
    discoverer._restaurant_direct_batch_min_results = 1
    discoverer._request_cache_lock = Lock()
    discoverer._search = MagicMock()
    discoverer._search.chat_completion.return_value = ""
    discoverer._search.is_circuit_open.return_value = False

    group = [
        {"name": "St. George, Utah", "dates": "October 17, 2026"},
        {"name": "Springdale, Utah", "dates": "October 18, 2026"},
    ]
    discoverer._fetch_and_cache_grouped_direct_batch(kind="restaurant", group=group)

    assert not hasattr(discoverer, "_restaurant_direct_batch_cache")


def test_fetch_and_cache_grouped_direct_batch_skips_when_circuit_open():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._restaurant_direct_batch_item_count = 4
    discoverer._restaurant_direct_batch_min_results = 1
    discoverer._request_cache_lock = Lock()
    discoverer._search = MagicMock()
    discoverer._search.is_circuit_open.return_value = True
    discoverer._search.chat_completion.side_effect = AssertionError("must not call out when circuit is open")

    group = [
        {"name": "St. George, Utah", "dates": "October 17, 2026"},
        {"name": "Springdale, Utah", "dates": "October 18, 2026"},
    ]
    discoverer._fetch_and_cache_grouped_direct_batch(kind="restaurant", group=group)

    discoverer._search.chat_completion.assert_not_called()


def test_direct_batch_html_prompt_scales_attraction_count_to_short_stay():
    """Regression: attraction/trail harvest requests used to always ask for
    the flat configured ceiling (default 20) regardless of how short the
    stay is -- a 1-day stay only ever keeps items_per_day=3 attractions, so
    asking for 20 wastes completion tokens and generation time on rows that
    get discarded. Count must scale down for a short stay, never up."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)

    prompt = discoverer._direct_batch_html_prompt(
        kind="attraction",
        dest_name="Telluride",
        dates="October 7, 2026",
    )

    assert prompt is not None
    system_prompt, _user_prompt = prompt
    # 1 day * 3/day * buffer 2 = 6, well under the default ceiling of 20.
    assert "exactly 6 <li>" in system_prompt


def test_direct_batch_html_prompt_caps_attraction_count_at_configured_ceiling():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._direct_link_batch_count = 20

    prompt = discoverer._direct_batch_html_prompt(
        kind="attraction",
        dest_name="Telluride",
        dates="October 1-10, 2026",
    )

    assert prompt is not None
    system_prompt, _user_prompt = prompt
    # 10 days * 3/day * buffer 2 = 60, but must never exceed the ceiling.
    assert "exactly 20 <li>" in system_prompt


def test_direct_batch_html_prompt_scales_trail_count_to_short_stay():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)

    prompt = discoverer._direct_batch_html_prompt(
        kind="trail",
        dest_name="Telluride",
        dates="October 7, 2026",
    )

    assert prompt is not None
    system_prompt, _user_prompt = prompt
    # 1 day * 2/day * buffer 2 = 4, floored to the minimum of 5.
    assert "exactly 5 <li>" in system_prompt


def test_direct_batch_html_prompt_for_restaurants_requires_rating_and_price_indicators():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)

    prompt = discoverer._direct_batch_html_prompt(
        kind="restaurant",
        dest_name="St. George, Utah",
        dates="October 17, 2026",
    )

    assert prompt is not None
    system_prompt, user_prompt = prompt
    assert "rating" in system_prompt.lower()
    assert "price" in system_prompt.lower()
    # A numeric rating floor must be present -- removing it entirely was a first
    # attempt that went too far, and this assertion is what caught it. The VALUE
    # is now budget-aware: 4.3 by default, relaxed to 4.0 on a low-cost brief,
    # because 4.3 skews toward destination restaurants and a friterie locals
    # rate 4.1 is exactly what such a brief wants. See _batch_rating_floor.
    assert re.search(r"rated above \d\.\d", system_prompt), system_prompt
    assert "price indicator" in user_prompt.lower() or "price" in user_prompt.lower()


def test_direct_batch_html_prompt_for_restaurants_requests_a_real_descriptive_note():
    """Root-cause fix for the dipstick59 completeness regression: 38 of 62
    restaurants rendered with no description/teaser at all. Real captured
    harvest HTML (Bryce Canyon direct-batch restaurant rows) showed every
    single row following the exact shape "Name - Rating/5, $Price Cuisine."
    with zero real prose -- unlike the attraction/trail prompts, the
    restaurant prompt never asked the model for a descriptive note at all,
    only for rating and price. It must now ask as explicitly as the
    attraction prompt does (both single- and multi-destination variants)."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)

    prompt = discoverer._direct_batch_html_prompt(
        kind="restaurant",
        dest_name="St. George, Utah",
        dates="October 17, 2026",
    )
    assert prompt is not None
    system_prompt, user_prompt = prompt
    assert "descriptive note" in system_prompt.lower()
    assert "descriptive note" in user_prompt.lower()

    multi_prompt = discoverer._direct_batch_html_prompt_multi(
        kind="restaurant",
        destinations=[("St. George, Utah", "October 17, 2026")],
    )
    assert multi_prompt is not None
    multi_system_prompt, multi_user_prompt = multi_prompt
    assert "descriptive note" in multi_system_prompt.lower()
    assert "descriptive note" in multi_user_prompt.lower()


def test_direct_batch_html_prompt_for_en_route_stops_prefers_specific_stop_pages():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)

    prompt = discoverer._direct_batch_html_prompt(
        kind="en_route_stop",
        dest_name="Zion National Park",
        dates="October 7-9, 2026",
        origin_name="St. George",
    )

    assert prompt is not None
    system_prompt, _user_prompt = prompt
    assert "specific official or authoritative page" in system_prompt.lower()
    assert "generic destination landing page or park home page" in system_prompt.lower()
    assert "prefer specific official or authoritative pages" in _user_prompt.lower()


def test_direct_batch_html_prompt_unchanged_when_no_seeds():
    """Root cause of the recurring "named seed missing a URL" pattern
    (Sunrise Point / Bryce Canyon, Imogene Pass / Telluride, etc.): the
    direct-batch harvest prompt had no mechanism at all to surface manifest
    seeds as candidates, so an obscure-but-real seed had no real chance
    against more famous nearby attractions for the model's limited slot
    budget -- this is upstream of the (unmodified) verification/matching
    trust boundary. Passing seed_names=None (or omitting it) must leave the
    prompt text byte-identical to before this parameter existed, for every
    kind that accepts it."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._max_trail_miles = 3.0
    discoverer._alltrails_rating_min = 4.5
    discoverer._alltrails_rating_min_votes = 200

    for kind, kwargs in (
        ("attraction", {}),
        ("trail", {}),
        ("en_route_stop", {"origin_name": "St. George"}),
    ):
        without_seeds = discoverer._direct_batch_html_prompt(
            kind=kind, dest_name="Bryce Canyon", dates="October 7, 2026", **kwargs
        )
        with_empty_seeds = discoverer._direct_batch_html_prompt(
            kind=kind, dest_name="Bryce Canyon", dates="October 7, 2026", seed_names=[], **kwargs
        )
        assert without_seeds == with_empty_seeds


def test_direct_batch_html_prompt_lists_seed_names_for_attractions_and_trails():
    """When a destination has manifest seeds, both the attraction and trail
    (AllTrails) single-destination harvest prompts must name them explicitly
    and instruct the model to verify and include any that are real -- the
    fix for seeds like "Sunrise Point" never even appearing as harvest
    candidates."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._max_trail_miles = 3.0
    discoverer._alltrails_rating_min = 4.5
    discoverer._alltrails_rating_min_votes = 200

    for kind in ("attraction", "trail"):
        prompt = discoverer._direct_batch_html_prompt(
            kind=kind,
            dest_name="Bryce Canyon",
            dates="October 7, 2026",
            seed_names=["Sunrise Point", "Inspiration Point"],
        )
        assert prompt is not None
        system_prompt, user_prompt = prompt
        assert "Sunrise Point" in system_prompt
        assert "Inspiration Point" in system_prompt
        assert "Sunrise Point" in user_prompt
        assert "Inspiration Point" in user_prompt
        assert "verify" in system_prompt.lower()
        assert "real" in system_prompt.lower()


def test_direct_batch_html_prompt_lists_seed_names_for_en_route_stops():
    """en_route_seeds (the en-route-stop counterpart to `seeds`) must reach
    the en-route-stop harvest prompt the same way."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)

    prompt = discoverer._direct_batch_html_prompt(
        kind="en_route_stop",
        dest_name="Zion National Park",
        dates="October 7-9, 2026",
        origin_name="St. George",
        seed_names=["Kolob Canyons Viewpoint"],
    )

    assert prompt is not None
    system_prompt, user_prompt = prompt
    assert "Kolob Canyons Viewpoint" in system_prompt
    assert "Kolob Canyons Viewpoint" in user_prompt


def test_direct_batch_html_prompt_ignores_seed_names_for_restaurants():
    """`seeds` in the manifest are documented as "Attraction/hike/experience
    name hints only" -- restaurants have no seed concept, so a seed_names
    argument (if ever passed by mistake) must not change the restaurant
    prompt at all."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)

    without_seeds = discoverer._direct_batch_html_prompt(
        kind="restaurant", dest_name="St. George, Utah", dates="October 17, 2026"
    )
    with_seeds = discoverer._direct_batch_html_prompt(
        kind="restaurant",
        dest_name="St. George, Utah",
        dates="October 17, 2026",
        seed_names=["Some Named Attraction"],
    )
    assert without_seeds == with_seeds


def test_direct_batch_html_prompt_multi_unchanged_when_no_seeds():
    """Same byte-identical guarantee as the single-destination prompt, for
    the grouped multi-destination variant (the path actually used by
    default, since DEFAULT_DIRECT_BATCH_GROUP_SIZE > 1)."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._max_trail_miles = 3.0
    discoverer._alltrails_rating_min = 4.5
    discoverer._alltrails_rating_min_votes = 200
    destinations = [("St. George, Utah", "October 17, 2026"), ("Springdale, Utah", "October 18, 2026")]

    for kind in ("attraction", "trail"):
        without_seeds = discoverer._direct_batch_html_prompt_multi(kind=kind, destinations=destinations)
        with_empty_map = discoverer._direct_batch_html_prompt_multi(
            kind=kind, destinations=destinations, seed_names_by_destination={}
        )
        assert without_seeds == with_empty_map


def test_direct_batch_html_prompt_multi_lists_seed_names_per_destination():
    """The grouped multi-destination prompt must attach each destination's
    own seed names to its own dest_line only -- a seed for St. George must
    not leak into Springdale's line -- and add a generic verify-and-include
    instruction when any destination in the group has seeds."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._max_trail_miles = 3.0
    discoverer._alltrails_rating_min = 4.5
    discoverer._alltrails_rating_min_votes = 200
    destinations = [("St. George, Utah", "October 17, 2026"), ("Springdale, Utah", "October 18, 2026")]

    for kind in ("attraction", "trail"):
        prompt = discoverer._direct_batch_html_prompt_multi(
            kind=kind,
            destinations=destinations,
            seed_names_by_destination={"St. George, Utah": ["Snow Canyon Overlook"]},
        )
        assert prompt is not None
        system_prompt, user_prompt = prompt
        assert "Snow Canyon Overlook" in user_prompt
        assert "verify" in system_prompt.lower()
        # The seed name must be attached to its own destination's line, not
        # bled into a different destination's line.
        st_george_line = next(line for line in user_prompt.splitlines() if "St. George" in line)
        springdale_line = next(line for line in user_prompt.splitlines() if "Springdale" in line)
        assert "Snow Canyon Overlook" in st_george_line
        assert "Snow Canyon Overlook" not in springdale_line


def test_prioritize_direct_batch_attractions_forwards_seed_names_to_harvest():
    """The harvest-call trigger (_get_attraction_direct_batch_rows_for_destination)
    must receive the same seed_names _prioritize_direct_batch_attractions
    was already given, so the prompt actually sent to the model includes
    them -- previously seed_names was accepted here but silently dropped."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._attraction_direct_batch_items_per_day = 6

    with patch.object(discoverer, "_get_attraction_direct_batch_rows_for_destination", return_value=[]) as mock_get:
        discoverer._prioritize_direct_batch_attractions(
            [], "Bryce Canyon", "October 7, 2026", seed_names=["Sunrise Point"]
        )

    mock_get.assert_called_once_with("Bryce Canyon", "October 7, 2026", seed_names=["Sunrise Point"])


def test_prioritize_direct_batch_trails_forwards_seed_names_to_harvest():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._trail_direct_batch_items_per_day = 2

    with patch.object(discoverer, "_get_alltrails_direct_batch_rows_for_destination", return_value=[]) as mock_get:
        discoverer._prioritize_direct_batch_trails(
            [], "Telluride", "October 7, 2026", seed_names=["Imogene Pass"]
        )

    mock_get.assert_called_once_with("Telluride", "October 7, 2026", seed_names=["Imogene Pass"])


def test_prioritize_direct_batch_en_route_stops_forwards_seed_names_to_harvest():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)

    with patch.object(discoverer, "_get_en_route_direct_batch_rows_for_destination", return_value=[]) as mock_get:
        discoverer._prioritize_direct_batch_en_route_stops(
            [], "Zion National Park", "October 7, 2026", "St. George", seed_names=["Kolob Canyons Viewpoint"]
        )

    mock_get.assert_called_once_with(
        "Zion National Park", "October 7, 2026", "St. George", seed_names=["Kolob Canyons Viewpoint"]
    )


def test_search_alltrails_direct_batch_authoritative_rejects_mismatched_trail_link():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._direct_batch_authoritative = True

    rows = [
        {
            "url": "https://www.alltrails.com/trail/us/utah/canyon-overlook-trail",
            "title": "Canyon Overlook Trail",
            "snippet": "Popular short Zion trail",
        }
    ]

    with patch.object(discoverer, "_get_alltrails_direct_batch_rows_for_destination", return_value=rows):
        out = discoverer._search_alltrails_for_trail_from_direct_batch(
            "Kolob Canyons",
            "Zion National Park",
            "October 18, 2026",
        )

    assert out is None


def test_search_alltrails_direct_batch_authoritative_does_not_fallback_to_search():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._direct_batch_authoritative = True
    discoverer._alltrails_source = "direct_link_batch"
    discoverer._disable_trails = False

    with patch.object(discoverer, "_search_alltrails_for_trail_from_direct_batch", return_value=None), patch.object(
        discoverer,
        "_search_first",
        side_effect=AssertionError("search fallback should not run in authoritative direct-batch mode"),
    ):
        out = discoverer._search_alltrails_for_trail(
            "Angels Landing",
            "Zion National Park",
            "October 18, 2026",
        )

    assert out is None


def test_search_restaurant_direct_batch_authoritative_prefers_item_matching_url():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._direct_batch_authoritative = True

    rows = [
        {
            "url": "https://www.tripadvisor.com/Restaurants-g60771-Zion_National_Park_Utah.html",
            "title": "Zion National Park Restaurants",
            "snippet": "Top places to eat in Zion area",
        },
        {
            "url": "https://www.google.com/maps/search/?api=1&query=Spotted+Dog+Cafe+Springdale+UT",
            "title": "Spotted Dog Cafe",
            "snippet": "Google Maps listing",
        },
    ]

    with patch.object(discoverer, "_get_restaurant_direct_batch_rows_for_destination", return_value=rows):
        out = discoverer._search_restaurant_from_direct_batch(
            "Spotted Dog Cafe",
            "Zion National Park",
            "October 18, 2026",
        )

    assert out is None


def test_discover_restaurants_direct_batch_authoritative_does_not_fallback_to_search():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._direct_batch_authoritative = True
    discoverer._restaurant_source = "direct_link_batch"

    ai = {
        "dinner_recommendations": [
            {
                "name": "The Spotted Dog Cafe",
            }
        ]
    }

    # CONTRACT CHANGED 2026-08-27. This previously asserted that the search
    # fallback must NEVER run in authoritative mode. That conflated two things:
    # whose answer WINS (the batch, still true) and what happens when the batch
    # has NO answer (previously: the item is dropped).
    #
    # The Brussels run measured the cost. Chicon Farsi, Thaiburi, Yummy Bowl,
    # Pasta Divina and Rotisse were all dropped for "no verified URL", and all
    # five have official sites a single Serper query finds. 77% of that
    # destination's dining was lost to a fallback never attempted.
    #
    # The genuine safety property is unchanged and covered by
    # test_search_restaurant_direct_batch_authoritative_rejects_raw_capture_without_item_match:
    # the batch must not lend an UNMATCHED row to a different item. A fresh
    # per-item search is a different operation and is still URL-validated.
    with patch.object(discoverer, "_resolve_ai_candidate_url", return_value=None), patch.object(
        discoverer,
        "_search_restaurant_from_direct_batch",
        return_value=None,
    ), patch.object(discoverer, "_search_first", return_value=None) as fallback_search:
        discoverer._discover_restaurants(ai, "Zion National Park", "October 18, 2026")

    # Still no URL -- the fallback was tried and also came up empty.
    assert ai["dinner_recommendations"][0].get("url", "") == ""
    # But it WAS tried, which is the change.
    assert fallback_search.called, "an item the batch cannot place must still get one search"


def test_search_restaurant_direct_batch_authoritative_rejects_raw_capture_without_item_match():
    """Fail-closed contract: a destination batch with zero row/URL match for the
    requested item must publish no canonical URL, even if the batch has only one
    (unrelated) row. Borrowing an unmatched raw capture risks publishing a
    different restaurant's link under the requested item's name."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._direct_batch_authoritative = True

    rows = [
        {
            "url": "https://www.officialrestaurant.example/chef-special",
            "title": "A nearby dining option",
            "snippet": "A valid restaurant capture for the destination.",
        }
    ]

    with patch.object(discoverer, "_get_restaurant_direct_batch_rows_for_destination", return_value=rows), patch.object(
        discoverer,
        "_retain_discovered_url",
        side_effect=lambda url, *_args, **_kwargs: url,
    ):
        out = discoverer._search_restaurant_from_direct_batch(
            "The Spotted Dog Cafe",
            "Zion National Park",
            "October 18, 2026",
        )

    assert out is None


def test_search_restaurant_direct_batch_authoritative_rejects_tripadvisor_area_listing():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._direct_batch_authoritative = True

    rows = [
        {
            "url": "https://www.tripadvisor.com/Restaurants-g60771-Zion_National_Park_Utah.html",
            "title": "Oscar's Cafe",
            "snippet": "Local favorite",
        }
    ]

    with patch.object(discoverer, "_get_restaurant_direct_batch_rows_for_destination", return_value=rows):
        out = discoverer._search_restaurant_from_direct_batch(
            "Oscar's Cafe",
            "Zion National Park",
            "October 18, 2026",
        )

    assert out is None


def test_search_restaurant_direct_batch_authoritative_rejects_near_destination_maps_query():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._direct_batch_authoritative = True

    rows = [
        {
            "url": "https://www.google.com/maps/search/restaurants+near+Zion+National+Park/",
            "title": "The Spotted Dog Cafe",
            "snippet": "Google Maps listing",
        }
    ]

    with patch.object(discoverer, "_get_restaurant_direct_batch_rows_for_destination", return_value=rows):
        out = discoverer._search_restaurant_from_direct_batch(
            "The Spotted Dog Cafe",
            "Zion National Park",
            "October 18, 2026",
        )

    assert out is None


def test_search_restaurant_direct_batch_authoritative_rejects_maps_q_for_other_venue():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._direct_batch_authoritative = True

    rows = [
        {
            "url": "https://maps.google.com/?q=Capitol+Reef+Resort+Torrey+UT",
            "title": "The Rim Rock Restaurant",
            "snippet": "Map candidate",
        }
    ]

    with patch.object(discoverer, "_get_restaurant_direct_batch_rows_for_destination", return_value=rows):
        out = discoverer._search_restaurant_from_direct_batch(
            "The Rim Rock Restaurant",
            "Capitol Reef National Park",
            "October 18, 2026",
        )

    assert out is None


def test_search_restaurant_direct_batch_authoritative_prefers_official_over_tripadvisor_when_both_match():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._direct_batch_authoritative = True

    rows = [
        {
            "url": "https://www.tripadvisor.com/Restaurant_Review-g60771-d123456-Spotted_Dog_Cafe-Springdale_Utah.html",
            "title": "Spotted Dog Cafe",
            "snippet": "Tripadvisor listing",
        },
        {
            "url": "https://www.spotteddogcafe.com/",
            "title": "Spotted Dog Cafe",
            "snippet": "Official restaurant site",
        },
    ]

    with patch.object(discoverer, "_get_restaurant_direct_batch_rows_for_destination", return_value=rows):
        with patch.object(discoverer, "_retain_discovered_url", side_effect=lambda u, *_a, **_k: u):
            out = discoverer._search_restaurant_from_direct_batch(
                "Spotted Dog Cafe",
                "Zion National Park",
                "October 18, 2026",
            )

    assert out == "https://www.spotteddogcafe.com/"


def test_search_restaurant_direct_batch_authoritative_prefers_tripadvisor_over_maps_search_query():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._direct_batch_authoritative = True

    rows = [
        {
            "url": "https://www.google.com/maps/search/?api=1&query=Spotted+Dog+Cafe+Springdale+UT",
            "title": "Spotted Dog Cafe",
            "snippet": "Google Maps search result",
        },
        {
            "url": "https://www.tripadvisor.com/Restaurant_Review-g60771-d123456-Spotted_Dog_Cafe-Springdale_Utah.html",
            "title": "Spotted Dog Cafe",
            "snippet": "Tripadvisor listing",
        },
    ]

    with patch.object(discoverer, "_get_restaurant_direct_batch_rows_for_destination", return_value=rows):
        with patch.object(discoverer, "_retain_discovered_url", side_effect=lambda u, *_a, **_k: u):
            out = discoverer._search_restaurant_from_direct_batch(
                "Spotted Dog Cafe",
                "Zion National Park",
                "October 18, 2026",
            )

    assert out == "https://www.tripadvisor.com/Restaurant_Review-g60771-d123456-Spotted_Dog_Cafe-Springdale_Utah.html"


def test_search_attraction_direct_batch_authoritative_prefers_official_over_tripadvisor_when_both_match():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._direct_batch_authoritative = True

    rows = [
        {
            "url": "https://www.tripadvisor.com/Attraction_Review-g60771-d123456-Reviews-Observation_Point-Zion_National_Park_Utah.html",
            "title": "Observation Point",
            "snippet": "TripAdvisor attraction page",
        },
        {
            "url": "https://www.zionadventures.com/observation-point",
            "title": "Observation Point",
            "snippet": "Official local operator page",
        },
    ]

    with patch.object(discoverer, "_get_attraction_direct_batch_rows_for_destination", return_value=rows):
        with patch.object(discoverer, "_retain_discovered_url", side_effect=lambda u, *_a, **_k: u):
            out = discoverer._search_attraction_from_direct_batch(
                "Observation Point",
                "Zion National Park",
                "October 18, 2026",
            )

    assert out == "https://www.zionadventures.com/observation-point"


def test_prefer_canonical_alltrails_url_keeps_noisy_variant_when_fetch_blocked():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    noisy = "https://www.alltrails.com/trail/us/utah/the-narrows-top-down"

    with patch.object(discoverer, "_fetch_page_text", side_effect=[(False, 403, ""), (False, 403, "")]):
        out = discoverer._prefer_canonical_alltrails_url(noisy, "The Narrows")

    # Neither synthesized slug ("-trail" or bare) was ever confirmed live -- both
    # fetches were blocked, not verified. Promoting either would be exactly the
    # kind of fabricated-slug guess (e.g. "the-narrows-trail") the fail-closed
    # named-entity URL policy forbids, so the original noisy-but-real URL is kept.
    assert out == noisy


def test_discover_restaurants_direct_batch_takes_precedence_over_ai_candidate_url():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._restaurant_source = "direct_link_batch"

    ai = {
        "dinner_recommendations": [{"name": "The Spotted Dog Cafe"}],
        "top_attractions": [],
        "getting_here": {"en_route_stops": []},
    }

    with patch.object(
        discoverer,
        "_search_restaurant_from_direct_batch",
        return_value="https://www.tripadvisor.com/Restaurant_Review-g60771-d123456-Reviews-The_Spotted_Dog_Cafe-Springdale_Utah.html",
    ), patch.object(
        discoverer,
        "_resolve_ai_candidate_url",
        return_value="https://example.com/different-restaurant-url",
    ):
        discoverer._discover_restaurants(ai, dest_name="Zion National Park")

    entry = ai["dinner_recommendations"][0]
    assert "tripadvisor.com" in entry["url"]
    assert "example.com" not in entry["url"]


def test_discover_attractions_direct_batch_takes_precedence_over_ai_candidate_url():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._attraction_source = "direct_link_batch"

    ai = {
        "top_attractions": [{"name": "Canyon Overlook Trail"}],
    }

    with patch.object(
        discoverer,
        "_search_alltrails_for_trail",
        return_value="https://www.alltrails.com/trail/us/utah/canyon-overlook-trail",
    ), patch.object(
        discoverer,
        "_resolve_ai_candidate_url",
        return_value="https://example.com/other-trail-url",
    ):
        discoverer._discover_attractions(ai, "Zion National Park", None, "October 18, 2026")

    entry = ai["top_attractions"][0]
    assert entry["url"] == "https://www.alltrails.com/trail/us/utah/canyon-overlook-trail"
    assert "example.com" not in entry["url"]


def test_search_attraction_direct_batch_authoritative_prefers_specific_page_over_maps_search():
    """A specific official/source page is always more useful to a reader than a
    generic Maps search query -- it must win, not lose, when both are candidates.
    (This inverts a prior version of this test that asserted the opposite; that
    was the exact bug reported against a live run: an attraction's official page
    was available but a vague Maps search link was rendered instead.)"""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._direct_batch_authoritative = True

    rows = [
        {
            "url": "https://www.nps.gov/zion/planyourvisit/canyon-overlook-trail.htm",
            "title": "Canyon Overlook Trail",
            "snippet": "NPS page",
        },
        {
            "url": "https://www.google.com/maps/search/?api=1&query=Canyon+Overlook+Trail+Zion+National+Park",
            "title": "Canyon Overlook Trail",
            "snippet": "Google Maps listing",
        },
    ]

    with patch.object(discoverer, "_get_attraction_direct_batch_rows_for_destination", return_value=rows):
        with patch.object(discoverer, "_retain_discovered_url", side_effect=lambda u, *_a, **_k: u):
            out = discoverer._search_attraction_from_direct_batch(
                "Canyon Overlook Trail",
                "Zion National Park",
                "October 18, 2026",
            )

    assert out == "https://www.nps.gov/zion/planyourvisit/canyon-overlook-trail.htm"


def test_search_attraction_direct_batch_authoritative_keeps_live_raw_capture_url_from_st_george_capture():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._direct_batch_authoritative = True

    rows = [
        {
            "title": "Tuacahn Amphitheatre & Center for the Arts",
            "name": "Tuacahn Amphitheatre & Center for the Arts",
            "url": "https://www.tuacahn.org/",
            "maps_url": "https://www.google.com/maps/search/?api=1&query=Tuacahn+Amphitheatre+1100+Tuacahn+Dr+St.+George+UT",
            "snippet": "Tuacahn Amphitheatre & Center for the Arts Source Maps Links: https://www.tuacahn.org/ https://www.google.com/maps/search/?api=1&query=Tuacahn+Amphitheatre+1100+Tuacahn+Dr+St.+George+UT",
            "description": "Tuacahn Amphitheatre & Center for the Arts",
        }
    ]

    with patch.object(discoverer, "_get_attraction_direct_batch_rows_for_destination", return_value=rows):
        with patch.object(discoverer, "_retain_discovered_url", side_effect=lambda u, *_a, **_k: u):
            out = discoverer._search_attraction_from_direct_batch(
                "Tuacahn Amphitheatre & Center for the Arts",
                "St. George, Utah",
                "October 17, 2026",
            )

    assert out == "https://www.tuacahn.org/"


def test_discover_attractions_direct_batch_takes_precedence_over_ai_candidate_for_non_trail():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._attraction_source = "direct_link_batch"

    ai = {
        "top_attractions": [
            {
                "name": "Canyon View Park",
                "type": "attraction",
                "description": "Popular city overlook.",
            }
        ],
        "getting_here": {"en_route_stops": []},
    }

    with patch.object(
        discoverer,
        "_search_attraction_from_direct_batch",
        return_value="https://www.visitstgeorge.com/canyon-view-park",
    ), patch.object(
        discoverer,
        "_get_attraction_direct_batch_rows_for_destination",
        return_value=[{"name": "Canyon View Park", "url": "https://www.visitstgeorge.com/canyon-view-park"}],
    ), patch.object(
        discoverer,
        "_resolve_ai_candidate_url",
        return_value="https://example.com/different-attraction-url",
    ), patch.object(
        discoverer,
        "_is_uninterested_attraction",
        return_value=False,
    ):
        discoverer._discover_attractions(ai, "St. George, Utah", None)

    out = ai["top_attractions"][0].get("url", "")
    assert out == "https://www.visitstgeorge.com/canyon-view-park"
    assert "example.com" not in out


def test_discover_attractions_direct_batch_preserves_existing_url_without_rematch() -> None:
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._attraction_source = "direct_link_batch"

    ai = {
        "top_attractions": [
            {
                "name": "Zion Human History Museum",
                "type": "museum",
                "url": "https://www.nps.gov/zion/planyourvisit/museum.htm",
            }
        ]
    }

    with patch.object(discoverer, "_retain_discovered_url", side_effect=lambda url, *_a, **_k: url):
        with patch.object(discoverer, "_search_attraction_from_direct_batch") as batch_search:
            with patch.object(discoverer, "_search_first") as fallback_search:
                discoverer._discover_attractions(ai, "Zion National Park", "zion")

    assert ai["top_attractions"][0]["url"] == "https://www.nps.gov/zion/planyourvisit/museum.htm"
    batch_search.assert_not_called()
    fallback_search.assert_not_called()


def test_discover_attractions_preserved_existing_url_still_gets_rating_metadata() -> None:
    """Regression for dipstick55 Theme D: 'Red Hills Desert Garden' rendered
    with no rating badge at all even though its harvested direct-batch row
    carried a 4.8 rating. Root cause: the 'existing URL already attached,
    just validate it' shortcut (direct_batch_existing_url_preserved) skipped
    the row-metadata merge that the fresh-lookup branch performs, so an
    attraction seeded with its url pre-attached by
    _prioritize_direct_batch_attractions silently lost its rating/votes."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._attraction_source = "direct_link_batch"

    ai = {
        "top_attractions": [
            {
                "name": "Red Hills Desert Garden",
                "type": "attraction",
                "url": "https://redhillsdesertgarden.com/",
            }
        ]
    }

    rows = [
        {
            "name": "Red Hills Desert Garden",
            "title": "Red Hills Desert Garden",
            "url": "https://redhillsdesertgarden.com/",
            "rating": 4.8,
            "raw_rating": "4.8/5",
        }
    ]

    with patch.object(discoverer, "_retain_discovered_url", side_effect=lambda url, *_a, **_k: url):
        with patch.object(discoverer, "_get_attraction_direct_batch_rows_for_destination", return_value=rows):
            discoverer._discover_attractions(ai, "St. George, Utah", None)

    attr = ai["top_attractions"][0]
    assert attr["url"] == "https://redhillsdesertgarden.com/"
    assert attr.get("raw_rating") == "4.8/5"
    assert attr.get("rating") == 4.8


def test_discover_attractions_removes_closed_nonseed_attraction_page() -> None:
    discoverer = URLDiscoverer.__new__(URLDiscoverer)

    ai = {
        "top_attractions": [
            {
                "name": "Weeping Rock",
                "type": "attraction",
                "description": "Currently closed for safety reasons.",
            }
        ]
    }

    with patch.object(discoverer, "_resolve_ai_candidate_url", return_value=None):
        with patch.object(discoverer, "_search_first", return_value="https://www.nps.gov/zion/planyourvisit/weeping-rock.htm"):
            with patch.object(
                discoverer,
                "_fetch_page_text",
                return_value=(True, 200, "Weeping Rock. Currently closed for safety reasons."),
            ):
                discoverer._discover_attractions(ai=ai, dest={"_registry_decisions": []}, dest_name="Zion National Park", nps_code="zion")

    assert ai["top_attractions"] == []


def test_discover_attractions_keeps_closed_seeded_attraction_page() -> None:
    """A closed seed keeps its card (never dropped -- it's the user's
    explicit pick) with its canonical link unlinked and a "verify status
    before you go" practical note. That note is only actionable if the item
    still has some link to click, so -- since the closure-detection
    no-link-fallback fix -- it now gets the same safe maps-search fallback
    every other "no URL found" attraction gets, instead of literally nothing.
    """
    discoverer = URLDiscoverer.__new__(URLDiscoverer)

    ai = {
        "top_attractions": [
            {
                "name": "Weeping Rock",
                "type": "attraction",
                "description": "Currently closed for safety reasons.",
            }
        ]
    }

    with patch.object(discoverer, "_resolve_ai_candidate_url", return_value=None):
        with patch.object(discoverer, "_search_first", return_value="https://www.nps.gov/zion/planyourvisit/weeping-rock.htm"):
            with patch.object(
                discoverer,
                "_fetch_page_text",
                return_value=(True, 200, "Weeping Rock. Currently closed for safety reasons."),
            ):
                discoverer._discover_attractions(
                    ai=ai,
                    dest={"_registry_decisions": []},
                    dest_name="Zion National Park",
                    nps_code="zion",
                    seed_names=["Weeping Rock"],
                )

    assert ai["top_attractions"]
    assert ai["top_attractions"][0]["name"] == "Weeping Rock"
    assert ai["top_attractions"][0].get("url", "") == (
        "https://www.google.com/maps/search/?api=1&query=Weeping%20Rock%20Zion%20National%20Park"
    )
    assert ai["top_attractions"][0].get("maps_url", "") == ai["top_attractions"][0].get("url", "")
    assert ai["top_attractions"][0].get("url") != "https://www.nps.gov/zion/planyourvisit/weeping-rock.htm"
    assert "currently closed" in str(ai["top_attractions"][0].get("practical_note", "")).lower()


def test_discover_attractions_real_moifa_closure_false_positive_gets_maps_fallback() -> None:
    """Regression using the real affected name from the SW2026-dipstick64 run:
    Santa Fe's "Museum of International Folk Art" was correctly resolved to
    its real site (moifa.org) via the authoritative direct-link batch, but
    the closure-detection second pass fetched that page's live text, matched
    an ATTRACTION_CLOSURE_MARKERS substring, and (before this fix) stripped
    both url and maps_url with the note "Currently closed; verify status
    before you go." -- leaving the seed completely unlinked with no way to
    actually do that verification. (Re-fetching moifa.org afterward showed
    "Open Today from 10-5" with no closure language at all, consistent with
    this having been a transient/false-positive match rather than a real,
    persistent closure.) It must now get the same safe maps-search fallback
    every other "no URL found" attraction gets.
    """
    discoverer = URLDiscoverer.__new__(URLDiscoverer)

    ai = {
        "top_attractions": [
            {
                "name": "Museum of International Folk Art",
                "type": "attraction",
                "description": (
                    "This museum houses a diverse collection of folk art from "
                    "around the world. The architecture itself is notable, "
                    "with the 'bark' building design."
                ),
                "practical_note": "Free admission on Sundays.",
            }
        ]
    }

    with patch.object(discoverer, "_resolve_ai_candidate_url", return_value=None):
        with patch.object(discoverer, "_search_attraction_from_direct_batch", return_value="https://www.moifa.org/"):
            with patch.object(discoverer, "_direct_batch_row_quality_metadata_for_url", return_value={}):
                with patch.object(
                    discoverer,
                    "_fetch_page_text",
                    return_value=(True, 200, "Museum currently closed for a private event today."),
                ):
                    discoverer._discover_attractions(
                        ai=ai,
                        dest={"_registry_decisions": []},
                        dest_name="Santa Fe",
                        nps_code=None,
                        seed_names=["Museum of International Folk Art"],
                    )

    attr = ai["top_attractions"][0]
    assert attr["name"] == "Museum of International Folk Art"
    assert attr.get("url") == (
        "https://www.google.com/maps/search/?api=1&query=Museum%20of%20International%20Folk%20Art%20Santa%20Fe"
    )
    assert attr.get("maps_url") == attr.get("url")
    assert attr.get("url") != "https://www.moifa.org/"
    assert "verify status before you go" in str(attr.get("practical_note", "")).lower()


def test_has_attraction_closure_marker_ignores_html_comment_noise() -> None:
    """Part 2 case (1) regression, grounded in real evidence from
    docs/reports/link_recall_strategy_experiment.md (2026-08-17 live
    re-fetch of internationalfolkart.org): the real "Museum of
    International Folk Art" page contains, inside a raw HTML comment
    (invisible to real visitors), the text "The Girard Wing will be
    temporarily closed now through May 2026 for critical roof repair. The
    Bartlett Gallery and Lloyd's Treasure Chest are also currently closed
    due to regular gallery changeover." Before stripping HTML comments,
    _has_attraction_closure_marker's naive whole-text substring match fired
    on "temporarily closed" / "currently closed" inside that comment even
    though the visible page describes an operating museum.
    """
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    page_text = (
        "<html><head></head><body>"
        "<h1>Museum of International Folk Art</h1>"
        "<p>Open Daily 10:00 AM - 5:00 PM May-October. Free admission on Sundays.</p>"
        "<!-- The Girard Wing will be temporarily closed now through May 2026 for "
        "critical roof repair. The Bartlett Gallery and Lloyd's Treasure Chest are "
        "also currently closed due to regular gallery changeover. -->"
        "<p>Plan your visit to the world's largest collection of folk art.</p>"
        "</body></html>"
    )
    assert discoverer._has_attraction_closure_marker(page_text) is False


def test_has_attraction_closure_marker_ignores_stale_partial_wing_closure() -> None:
    """Part 2 case (2) regression, grounded in the same experiment's evidence
    from the state's independent listing page (newmexicoculture.org), which
    visibly (not in a comment) carries a real but partial, stale notice: one
    specific exhibit in the Girard Wing closed for roof repair through a
    window that has already elapsed, while the rest of the museum operates
    normally. A same-sentence "wing"/"exhibit" qualifier next to the closure
    marker should be read as a partial closure, not a whole-attraction one.
    """
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    page_text = (
        "Museum of International Folk Art, New Mexico Department of Cultural "
        "Affairs. Since its founding in 1953, the museum has been a place to "
        "connect people through creative expression. The exhibit 'Multiple "
        "Visions: A Common Bond' in the Girard Wing is temporarily closed for "
        "roof repair through May 2026. The rest of the museum remains open "
        "daily to visitors."
    )
    assert discoverer._has_attraction_closure_marker(page_text) is False


def test_has_attraction_closure_marker_still_detects_real_full_closure() -> None:
    """Control for the Part 2 fixes: a real, whole-venue closure notice --
    with no HTML-comment noise and no wing/gallery/exhibit qualifier in the
    same sentence -- must still be detected. This guards against the
    comment-stripping and partial-closure heuristics silently widening into
    false negatives."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    assert discoverer._has_attraction_closure_marker(
        "Museum of International Folk Art. This museum is currently closed "
        "for renovations until further notice."
    ) is True
    assert discoverer._has_attraction_closure_marker(
        "Weeping Rock. Currently closed for safety reasons."
    ) is True


def test_is_under_construction_page_detects_real_nps_placeholder_text() -> None:
    """Real bug (Bryce Canyon eval run): "Bryce Canyon Visitor Center" linked
    to https://www.nps.gov/brca/planyourvisit/visitorcenters.htm, whose
    entire visible content (confirmed via live fetch) is a placeholder
    stub, not real visitor-center information. This page is technically
    live (200 status), on the right domain, and even mentions the
    destination -- none of the existing checks reason about whether a page
    actually contains any real content."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    page_text = (
        "National Park Service. Page In-Progress. This page is currently "
        "being worked on. Please check back later. Return to NPS home."
    )
    assert discoverer._is_under_construction_page(page_text) is True


def test_is_under_construction_page_ignores_html_comment_noise() -> None:
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    page_text = (
        "Bryce Canyon Visitor Center. Open daily with exhibits, a bookstore, "
        "and ranger-led programs. "
        "<!-- old dev note: this section is currently being worked on -->"
    )
    assert discoverer._is_under_construction_page(page_text) is False


def test_is_under_construction_page_false_on_substantive_content() -> None:
    """Control: a real, populated page must not be flagged just because it
    happens to be short or mentions an unrelated future item."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    page_text = (
        "Bryce Canyon Visitor Center. While at the Visitor Center you will "
        "want to see our award-winning film, 'A Song of Seasons.' Explore "
        "our museum exhibits and bookstore. A new exhibit is coming soon."
    )
    assert discoverer._is_under_construction_page(page_text) is False


def test_relevant_result_rejects_under_construction_placeholder_page() -> None:
    """End-to-end: _is_relevant_result's general (non-AllTrails) deep-check
    branch must reject a page whose own text says it's a placeholder, even
    though it otherwise clears the destination/item token checks (it's a
    real nps.gov page, on the right park, with the right item name in its
    URL slug region)."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    page_text = (
        "Bryce Canyon National Park. Visitor Center. Page In-Progress. "
        "This page is currently being worked on. Please check back later."
    )

    with patch.object(discoverer, "_fetch_page_text", return_value=(True, 200, page_text)):
        ok = discoverer._is_relevant_result(
            "https://www.nps.gov/brca/planyourvisit/visitorcenters.htm",
            "Bryce Canyon Visitor Center",
            "Bryce Canyon National Park",
        )

    assert ok is False


def test_discover_attractions_real_moifa_html_comment_closure_false_positive_keeps_real_link() -> None:
    """Full-pipeline Part 2 regression using the real evidence from
    link_recall_strategy_experiment.md: the direct-batch harvest found a
    real link for "Museum of International Folk Art"
    (internationalfolkart.org), but a live re-fetch of that page contains
    the real closure-sounding phrases only inside an HTML comment (see the
    unit test above for the exact real text). Before the comment-stripping
    fix, the second-pass closure check would have matched that comment and
    stripped the real, verified link down to a maps-search fallback even
    though the museum is actually open. With the fix, the item keeps its
    real, discovered link.
    """
    discoverer = URLDiscoverer.__new__(URLDiscoverer)

    ai = {
        "top_attractions": [
            {
                "name": "Museum of International Folk Art",
                "type": "attraction",
                "description": "Houses a diverse collection of international folk art.",
                "practical_note": "Free admission on Sundays.",
            }
        ]
    }

    page_text = (
        "<html><body><h1>Museum of International Folk Art</h1>"
        "<p>Open Daily 10:00 AM - 5:00 PM May-October.</p>"
        "<!-- The Girard Wing will be temporarily closed now through May 2026 "
        "for critical roof repair. The Bartlett Gallery and Lloyd's Treasure "
        "Chest are also currently closed due to regular gallery changeover. -->"
        "</body></html>"
    )

    with patch.object(discoverer, "_resolve_ai_candidate_url", return_value=None):
        with patch.object(
            discoverer, "_search_attraction_from_direct_batch", return_value="https://www.internationalfolkart.org/"
        ):
            with patch.object(discoverer, "_direct_batch_row_quality_metadata_for_url", return_value={}):
                with patch.object(discoverer, "_fetch_page_text", return_value=(True, 200, page_text)):
                    discoverer._discover_attractions(
                        ai=ai,
                        dest={"_registry_decisions": []},
                        dest_name="Santa Fe",
                        nps_code=None,
                        seed_names=["Museum of International Folk Art"],
                    )

    attr = ai["top_attractions"][0]
    assert attr.get("url") == "https://www.internationalfolkart.org/"
    assert "closed" not in str(attr.get("practical_note", "") or "").lower()


def test_search_restaurant_direct_batch_authoritative_skips_invalid_maps_and_uses_other():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._direct_batch_authoritative = True

    rows = [
        {
            "url": "https://www.google.com/maps/place/Desert+Bistro/@38.5742,-109.5516,17z",
            "title": "Desert Bistro",
            "snippet": "Map listing",
        },
        {
            "url": "https://www.desertbistro.com/desert-bistro-moab",
            "title": "Desert Bistro",
            "snippet": "Official site",
        },
    ]

    def _retain(url: str, *_args, **_kwargs) -> str:
        if "google.com/maps/place/" in str(url):
            return ""
        return str(url)

    with patch.object(discoverer, "_get_restaurant_direct_batch_rows_for_destination", return_value=rows):
        with patch.object(discoverer, "_retain_discovered_url", side_effect=_retain):
            out = discoverer._search_restaurant_from_direct_batch(
                "Desert Bistro",
                "Moab",
                "October 18, 2026",
            )

    assert out == "https://www.desertbistro.com/desert-bistro-moab"


def test_search_en_route_direct_batch_authoritative_skips_invalid_maps_place_url():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._direct_batch_authoritative = True

    rows = [
        {
            "url": "https://www.google.com/maps/place/Red+Canyon+Visitor+Center,+UT",
            "title": "Red Canyon",
            "snippet": "Maps listing",
        }
    ]

    with patch.object(discoverer, "_get_en_route_direct_batch_rows_for_destination", return_value=rows):
        out = discoverer._search_en_route_stop_from_direct_batch(
            "Red Canyon",
            "Bryce Canyon National Park",
            "October 18, 2026",
        )

    assert out is None


def test_search_en_route_direct_batch_authoritative_rejects_off_region_row_and_keeps_destination_match():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._direct_batch_authoritative = True

    rows = [
        {
            "url": "https://maps.google.com/?q=Mount+Si+Village+Seattle+WA",
            "title": "Mount Si Village",
            "snippet": "Shops near Seattle, Washington.",
        },
        {
            "url": "https://www.google.com/maps/search/?api=1&query=Lizard+Head+Pass+Telluride+CO",
            "title": "Lizard Head Pass",
            "snippet": "Scenic pass on the drive into Telluride.",
        },
    ]

    with patch.object(discoverer, "_get_en_route_direct_batch_rows_for_destination", return_value=rows):
        with patch.object(discoverer, "_retain_discovered_url", side_effect=lambda u, *_a, **_k: u):
            out = discoverer._search_en_route_stop_from_direct_batch(
                "Lizard Head Pass",
                "Telluride",
                "October 18, 2026",
            )

    assert out == "https://www.google.com/maps/search/?api=1&query=Lizard+Head+Pass+Telluride+CO"


def test_search_en_route_direct_batch_authoritative_prefers_source_over_generic_maps_search():
    """A generic google.com/maps/search/ URL is documented as never qualifying
    as canonical entity evidence (docs/design/url-discovery-and-audit.md) and
    _item_has_verified_url refuses to count it as "verified" -- so selecting
    one here over an already-retained real source page (fs.usda.gov, a
    verified-class URL) doesn't just deprioritize the source page, it
    guarantees the item gets discarded outright at audit with nothing to
    show. Regression coverage for dipstick67: "Mancos State Park" had its
    real cpw.state.co.us page found and preserved, then out-selected for a
    maps-search URL here, which then failed audit and the whole stop was
    removed -- a real page thrown away for one that was never going to
    survive. A deterministic Maps *place* URL is a different, verified
    policy class and still wins over a source page (see
    test_search_en_route_direct_batch_authoritative_prefers_maps_place_over_source_url)."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._direct_batch_authoritative = True

    rows = [
        {
            "url": "https://www.fs.usda.gov/recarea/gmug/recreation/recarea/?recid=33482",
            "title": "Lizard Head Pass",
            "snippet": "USFS stop details",
        },
        {
            "url": "https://www.google.com/maps/search/?api=1&query=Lizard+Head+Pass+Telluride+CO",
            "title": "Lizard Head Pass",
            "snippet": "Google Maps listing",
        },
    ]

    with patch.object(discoverer, "_get_en_route_direct_batch_rows_for_destination", return_value=rows):
        with patch.object(discoverer, "_retain_discovered_url", side_effect=lambda u, *_a, **_k: u):
            out = discoverer._search_en_route_stop_from_direct_batch(
                "Lizard Head Pass",
                "Telluride",
                "October 18, 2026",
            )

    assert out == "https://www.fs.usda.gov/recarea/gmug/recreation/recarea/?recid=33482"


def test_search_en_route_direct_batch_authoritative_prefers_maps_place_over_source_url():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._direct_batch_authoritative = True

    rows = [
        {
            "name": "Red Cliffs Desert Reserve",
            "url": "https://www.google.com/maps/place/Red+Cliffs+Desert+Reserve/@37.1467,-113.4249,12z",
            "snippet": (
                "Red Cliffs Desert Reserve Source Maps Links: "
                "https://www.blm.gov/visit/red-cliffs-national-conservation-area "
                "https://www.google.com/maps/place/Red+Cliffs+Desert+Reserve/@37.1467,-113.4249,12z"
            ),
        }
    ]

    with patch.object(discoverer, "_get_en_route_direct_batch_rows_for_destination", return_value=rows):
        with patch.object(discoverer, "_retain_discovered_url", side_effect=lambda u, *_a, **_k: u):
            out = discoverer._search_en_route_stop_from_direct_batch(
                "Red Cliffs Desert Reserve",
                "St. George, Utah",
                "October 17, 2026",
            )

    assert out == "https://www.google.com/maps/place/Red+Cliffs+Desert+Reserve/@37.1467,-113.4249,12z"


def test_search_restaurant_direct_batch_authoritative_uses_shallow_relevance_for_snippet_source_url():
    """Matched restaurant rows skip the expensive deep relevance/text check (that
    is what 'shallow relevance' means here) -- but a cheap liveness ping still
    runs so a matched row pointing at a dead domain isn't published regardless."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._direct_batch_authoritative = True

    rows = [
        {
            "name": "Morty's Cafe",
            "url": "https://www.google.com/maps/search/?api=1&query=Mortys+Cafe+St+George+UT",
            "snippet": (
                "Morty's Cafe Source Maps Links: "
                "https://www.mortyscafe.com/ "
                "https://www.google.com/maps/search/?api=1&query=Mortys+Cafe+St+George+UT"
            ),
        }
    ]

    with patch.object(discoverer, "_get_restaurant_direct_batch_rows_for_destination", return_value=rows):
        with patch.object(discoverer, "_fetch_page_text", return_value=(True, 200, "")):
            out = discoverer._search_restaurant_from_direct_batch(
                "Morty's Cafe",
                "St. George, Utah",
                "October 17, 2026",
            )

    assert out == "https://www.mortyscafe.com/"


def test_search_en_route_stop_from_direct_batch_falls_back_to_source_when_maps_missing():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._direct_batch_authoritative = False

    rows = [
        {
            "name": "Wilson Arch",
            "url": "",
            "snippet": "Wilson Arch Links: https://www.blm.gov/visit/wilson-arch",
        }
    ]

    with patch.object(discoverer, "_get_en_route_direct_batch_rows_for_destination", return_value=rows):
        with patch.object(discoverer, "_retain_discovered_url", side_effect=lambda u, *_a, **_k: u):
            out = discoverer._search_en_route_stop_from_direct_batch(
                "Wilson Arch",
                "Moab",
                "October 7-9, 2026",
            )

    assert out == "https://www.blm.gov/visit/wilson-arch"


def test_classify_url_policy_class_treats_maps_google_q_as_maps_search():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    out = discoverer._classify_url_policy_class("https://maps.google.com/?q=221+South+Oak+Telluride")
    assert out == "google_maps_search"


def test_classify_url_policy_class_handles_maps_url_variants():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    assert discoverer._classify_url_policy_class(
        "https://www.google.com/maps/search/?api=1&query=Benja+Thai+Sushi+St+George+UT"
    ) == "google_maps_search"
    assert discoverer._classify_url_policy_class(
        "https://www.google.com/maps/dir//Bit+%26+Spur+Restaurant+%26+Saloon,+1212+Zion+Park+Blvd,+Springdale,+UT+84767"
    ) == "google_maps_dir"
    assert discoverer._classify_url_policy_class(
        "https://www.google.com/maps/place/Benja+Thai+%26+Sushi/@37.1,-113.5,17z/data=!3m1!4b1"
    ) == "general"


def test_direct_batch_html_uses_real_live_search():
    """Regression for the 2026-08-14 fix: live_search=False used to be
    hardcoded here (this test used to assert exactly that), silently running
    every harvest call on the model's training-data memory since xAI's
    live_search mechanism was itself deprecated. Real search must be
    requested now -- probe evidence showed 21/21 embedded URLs matching the
    model's own search citations with this enabled, vs. no verifiable
    provenance at all without it."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._direct_link_batch_limit = lambda: 3
    discoverer._direct_batch_min_required = lambda kind: 1
    discoverer._persist_direct_batch_html_capture = lambda **kwargs: None
    discoverer._search = MagicMock()
    discoverer._search.chat_completion.return_value = "<h2>St. George</h2><ul><li>St. George Dinosaur Discovery Site <a href=\"https://example.com\">Source</a></li></ul>"
    discoverer._direct_batch_rows_from_html = lambda html: [{"title": "St. George Dinosaur Discovery Site", "url": "https://example.com"}] if "<h2>" in html else []

    rows = discoverer._get_direct_batch_html_rows_for_destination(
        cache={},
        destination="St. George, Utah",
        dates="October 17, 2026",
        kind="attraction",
    )

    assert rows
    assert discoverer._search.chat_completion.call_args.kwargs["live_search"] is True


def test_direct_batch_html_retries_empty_attraction_result():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._request_cache_lock = Lock()
    discoverer._attraction_direct_batch_cache = {}
    discoverer._direct_link_batch_limit = lambda: 3
    discoverer._persist_direct_batch_html_capture = lambda **kwargs: None
    discoverer._search = MagicMock()
    discoverer._search.is_circuit_open.return_value = False
    discoverer._search.chat_completion.side_effect = [
        "",
        "<h2>Moab</h2><ul><li>Arches National Park <a href=\"https://example.com\">Source</a> <a href=\"https://www.google.com/maps/search/?api=1&query=Arches+National+Park+Moab+UT\">Maps</a></li></ul>",
    ]
    discoverer._direct_batch_rows_from_html = lambda html: [
        {"title": "Arches National Park", "url": "https://example.com", "maps_url": "https://www.google.com/maps/search/?api=1&query=Arches+National+Park+Moab+UT"}
    ] if "<h2>" in html else []

    rows = discoverer._get_direct_batch_html_rows_for_destination(
        cache={},
        destination="Moab",
        dates="October 18, 2026",
        kind="attraction",
    )

    assert rows
    assert discoverer._search.chat_completion.call_count == 2


def _batch_html_discoverer_with_fallback(*, primary_html: str = "", fallback_html: str = "") -> URLDiscoverer:
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._request_cache_lock = Lock()
    discoverer._attraction_direct_batch_cache = {}
    discoverer._direct_link_batch_limit = lambda: 3
    discoverer._persist_direct_batch_html_capture = lambda **kwargs: None
    discoverer._search = MagicMock()
    discoverer._search.is_circuit_open.return_value = False
    discoverer._search.chat_completion.return_value = primary_html
    discoverer._search_fallback = MagicMock()
    discoverer._search_fallback.is_circuit_open.return_value = False
    discoverer._search_fallback.chat_completion.return_value = fallback_html
    discoverer._direct_batch_rows_from_html = lambda html: (
        [{"title": "Angels Landing", "url": "https://alltrails.com/x", "maps_url": "https://maps/x"}]
        if "<h2>" in html
        else []
    )
    return discoverer


def test_direct_batch_html_falls_back_to_cross_provider_when_primary_empty():
    """2026-08-15 fix: when the primary's batch harvest (and its own
    retry-prompt) comes back empty, retry the SAME purpose-built batch
    prompt through the fallback client before ever dropping to the
    narrower single-query mode -- a live run found the narrower mode
    structurally couldn't match every specific named item."""
    discoverer = _batch_html_discoverer_with_fallback(
        primary_html="",
        fallback_html="<h2>Zion</h2><ul><li>Angels Landing <a href='https://alltrails.com/x'>Source</a></li></ul>",
    )

    rows = discoverer._get_direct_batch_html_rows_for_destination(
        cache={}, destination="Zion National Park", dates="October 18, 2026", kind="attraction",
    )

    assert rows
    discoverer._search_fallback.chat_completion.assert_called_once()
    fallback_kwargs = discoverer._search_fallback.chat_completion.call_args.kwargs
    assert fallback_kwargs["live_search"] is True
    # Same purpose-built prompt as the primary got, not a different/narrower query.
    primary_kwargs = discoverer._search.chat_completion.call_args_list[0].kwargs
    assert fallback_kwargs["system_prompt"] == primary_kwargs["system_prompt"]
    assert fallback_kwargs["user_prompt"] == primary_kwargs["user_prompt"]


def test_direct_batch_html_skips_cross_provider_fallback_while_its_circuit_open():
    discoverer = _batch_html_discoverer_with_fallback(primary_html="", fallback_html="<h2>x</h2><ul></ul>")
    discoverer._search_fallback.is_circuit_open.return_value = True

    rows = discoverer._get_direct_batch_html_rows_for_destination(
        cache={}, destination="Zion National Park", dates="October 18, 2026", kind="attraction",
    )

    assert rows == []
    discoverer._search_fallback.chat_completion.assert_not_called()


def test_direct_batch_html_does_not_call_fallback_when_primary_already_sufficient():
    discoverer = _batch_html_discoverer_with_fallback(
        primary_html="<h2>Zion</h2><ul><li>Angels Landing <a href='https://alltrails.com/x'>Source</a></li></ul>",
        fallback_html="<h2>x</h2><ul></ul>",
    )

    rows = discoverer._get_direct_batch_html_rows_for_destination(
        cache={}, destination="Zion National Park", dates="October 18, 2026", kind="attraction",
    )

    assert rows
    discoverer._search_fallback.chat_completion.assert_not_called()


def test_direct_batch_html_keeps_primary_result_when_fallback_does_not_improve():
    discoverer = _batch_html_discoverer_with_fallback(primary_html="", fallback_html="")

    rows = discoverer._get_direct_batch_html_rows_for_destination(
        cache={}, destination="Zion National Park", dates="October 18, 2026", kind="attraction",
    )

    assert rows == []
    discoverer._search_fallback.chat_completion.assert_called_once()


def test_direct_batch_html_capture_records_which_provider_supplied_the_result():
    from generator.claude_search import ClaudeSearch
    from generator.grok_search import GrokSearch

    discoverer = _batch_html_discoverer_with_fallback(
        primary_html="",
        fallback_html="<h2>Zion</h2><ul><li>Angels Landing <a href='https://alltrails.com/x'>Source</a></li></ul>",
    )
    discoverer._search.__class__ = GrokSearch
    discoverer._search_fallback.__class__ = ClaudeSearch
    captured: dict = {}
    discoverer._persist_direct_batch_html_capture = lambda **kwargs: captured.update(kwargs)

    discoverer._get_direct_batch_html_rows_for_destination(
        cache={}, destination="Zion National Park", dates="October 18, 2026", kind="attraction",
    )

    assert captured["provider"] == "ClaudeSearch"


def test_url_discoverer_shares_llm_model_with_grok_search_when_provider_is_grok():
    """Regression for issue #65/#64: GrokSearch used to always fall back to
    its own independent XAI_MODEL env var, disconnected from whatever model
    MultiLLMClient actually resolved -- the two could silently diverge."""
    mock_llm = type("MockLLM", (), {"provider": "grok", "model": "grok-4.5", "usage_tracker": None})()
    # ClaudeSearch is also patched: config.yaml's url_discovery.
    # nonbatch_search_provider is claude, so __init__ now also builds a
    # second (fallback) client that would otherwise need a real
    # ANTHROPIC_API_KEY. Only GrokSearch's call_args (the batch client,
    # search_provider=grok) matters for this assertion.
    with patch("generator.search_provider.GrokSearch") as mock_grok_search_cls, patch(
        "generator.search_provider.ClaudeSearch"
    ), patch.object(URLDiscoverer, "_read_search_model_override", staticmethod(lambda _p: "")):
        URLDiscoverer(config_path="config.yaml", llm_client=mock_llm)
    assert mock_grok_search_cls.call_args.kwargs["model"] == "grok-4.5"


def test_url_discoverer_search_model_override_splits_discovery_from_content():
    """Discovery may run on a cheaper model than content generation.

    Measured 2026-08-21: URL discovery is 91% of all tokens and is extraction
    from retrieved pages, while the destination content bundles are 0.4%
    each. Splitting the two moves the dominant cost term without touching
    the quality of what the traveller actually reads.
    """
    mock_llm = type("MockLLM", (), {"provider": "grok", "model": "grok-4.5", "usage_tracker": None})()
    with patch("generator.search_provider.GrokSearch") as mock_grok_search_cls, patch(
        "generator.search_provider.ClaudeSearch"
    ), patch.object(URLDiscoverer, "_read_search_model_override", staticmethod(lambda _p: "grok-4.3")):
        URLDiscoverer(config_path="config.yaml", llm_client=mock_llm)
    # discovery gets the cheap model; the content client is untouched
    assert mock_grok_search_cls.call_args.kwargs["model"] == "grok-4.3"
    assert mock_llm.model == "grok-4.5"


def test_search_model_override_applies_even_when_content_provider_is_not_grok():
    """Regression, 2026-08-22: the override was gated on the CONTENT
    provider being grok. This project generates content with openai and
    searches with grok, so the gate was always false and the override
    silently never applied -- a whole baseline run billed the expensive
    tier while config said otherwise.

    grok_model is consumed only by GrokSearch, so setting it is inert
    unless the search provider is grok. Gating it on the content provider
    was never meaningful.
    """
    mock_llm = type("MockLLM", (), {"provider": "openai", "model": "gpt-4o-mini", "usage_tracker": None})()
    with patch("generator.search_provider.GrokSearch") as mock_grok_search_cls, patch(
        "generator.search_provider.ClaudeSearch"
    ), patch.object(URLDiscoverer, "_read_search_model_override", staticmethod(lambda _p: "grok-4.3")):
        URLDiscoverer(config_path="config.yaml", llm_client=mock_llm)
    assert mock_grok_search_cls.call_args.kwargs["model"] == "grok-4.3"


class TestAllowedDomainsForBatchKind:
    """Constrain server-side search where a category has one authoritative source.

    link_types.hike.discovery_site_filter has declared "alltrails.com" since
    the taxonomy was written and was read by no module -- the filter was
    applied only to results, so we paid to search the whole web and then
    discarded whatever was off-domain. Retrieved content is billed back as
    input tokens, which was 87% of spend on the 2026-08-21 run.
    """

    @staticmethod
    def _discoverer():
        mock_llm = type("MockLLM", (), {"provider": "grok", "model": "grok-4.5", "usage_tracker": None})()
        with patch("generator.search_provider.GrokSearch"), patch("generator.search_provider.ClaudeSearch"):
            return URLDiscoverer(config_path="config.yaml", llm_client=mock_llm)

    def test_trail_batch_is_constrained_to_alltrails(self):
        assert self._discoverer()._allowed_domains_for_batch_kind("trail") == ["alltrails.com"]

    def test_heterogeneous_kinds_are_left_unconstrained(self):
        """Attractions, restaurants and en-route stops legitimately live on
        many domains. Constraining them would trade cost for coverage, and
        link_types.scenic_drive sets discovery_site_filter: null explicitly."""
        d = self._discoverer()
        for kind in ("attraction", "restaurant", "en_route_stop", "scenic_drive"):
            assert d._allowed_domains_for_batch_kind(kind) == [], kind


def test_url_discoverer_leaves_grok_search_model_alone_when_provider_is_not_grok():
    """A non-grok CONTENT model must never leak into GrokSearch.

    The search_model override is neutralised here so this asserts what it
    always meant to: absent an explicit override, GrokSearch gets None
    rather than "gpt-4o-mini". (With the override set -- as config.yaml
    does -- it correctly gets that grok model instead; see
    test_search_model_override_applies_even_when_content_provider_is_not_grok.)
    """
    mock_llm = type("MockLLM", (), {"provider": "openai", "model": "gpt-4o-mini", "usage_tracker": None})()
    with patch("generator.search_provider.GrokSearch") as mock_grok_search_cls, patch(
        "generator.search_provider.ClaudeSearch"
    ), patch.object(URLDiscoverer, "_read_search_model_override", staticmethod(lambda _p: "")):
        URLDiscoverer(config_path="config.yaml", llm_client=mock_llm)
    assert mock_grok_search_cls.call_args.kwargs["model"] is None


def test_url_discoverer_builds_independent_batch_and_fallback_clients_from_real_config():
    """Regression: url_discovery.search_provider (batch/direct-batch-harvest)
    and url_discovery.nonbatch_search_provider (per-item fallback, used by
    _search_cached) are independent knobs, and must produce two distinct
    clients rather than one object reused twice.

    They now resolve to DIFFERENT providers, which is what makes this
    independence load-bearing rather than incidental. As of 2026-08-23 the
    batch stays on grok -- it invents the item list, which a SERP API cannot
    do -- while the fallback is Serper, a pure lookup. That split is the
    whole reason the Serper change could be scoped to half the pipeline
    without threading a single flag (see
    docs/design/cost-accounting-and-reduction.md section 8.7).
    """
    from generator.grok_search import GrokSearch
    from generator.serper_search import SerperSearch

    mock_llm = type("MockLLM", (), {"provider": "grok", "model": "grok-4.5", "usage_tracker": None})()
    with patch.dict("os.environ", {"XAI_API_KEY": "test-key", "ANTHROPIC_API_KEY": "test-key",
                                   "SERPER_API_KEY": "test-key"}):
        discoverer = URLDiscoverer(config_path="config.yaml", llm_client=mock_llm)

    assert isinstance(discoverer._search, GrokSearch)
    assert isinstance(discoverer._search_fallback, SerperSearch)
    assert discoverer._search is not discoverer._search_fallback


def test_url_discoverer_search_provider_override_forces_single_provider_no_fallback():
    """--search-provider (2026-08-15): forces one provider for both the
    batch client and (by not building one at all) the fallback, for a
    clean single-provider comparison run with zero cross-provider
    fallback contamination."""
    from generator.claude_search import ClaudeSearch

    mock_llm = type("MockLLM", (), {"provider": "grok", "model": "grok-4.5", "usage_tracker": None})()
    with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}):
        discoverer = URLDiscoverer(
            config_path="config.yaml", llm_client=mock_llm, search_provider_override="claude"
        )

    assert isinstance(discoverer._search, ClaudeSearch)
    assert discoverer._search_fallback is None


def test_search_cached_prefers_fallback_client_over_batch_client_when_both_set():
    """_search_cached (used by _search_first/_search_first_strict) must route
    through self._search_fallback when it's present, not self._search --
    that's the whole point of the split: the batch harvest client and the
    per-item fallback client can be pinned to different providers."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._request_cache_lock = Lock()
    discoverer._search = MagicMock()
    discoverer._search.search.return_value = [{"name": "wrong client", "url": "https://example.com/batch"}]
    discoverer._search_fallback = MagicMock()
    discoverer._search_fallback.search.return_value = [{"name": "right client", "url": "https://example.com/fallback"}]

    results = discoverer._search_cached("some query")

    assert results == [{"name": "right client", "url": "https://example.com/fallback"}]
    discoverer._search_fallback.search.assert_called_once()
    discoverer._search.search.assert_not_called()


def test_search_cached_falls_back_to_batch_client_when_fallback_client_unset():
    """Partially-constructed instances (e.g. URLDiscoverer.__new__ in other
    tests) that only ever set self._search must keep working unchanged --
    _search_cached degrades to the batch client rather than returning []."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._request_cache_lock = Lock()
    discoverer._search = MagicMock()
    discoverer._search.search.return_value = [{"name": "only client", "url": "https://example.com/only"}]

    results = discoverer._search_cached("some query")

    assert results == [{"name": "only client", "url": "https://example.com/only"}]


def test_direct_batch_html_cache_ignores_empty_cached_results_for_retries():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._request_cache_lock = Lock()
    discoverer._direct_link_batch_limit = lambda: 3
    discoverer._persist_direct_batch_html_capture = lambda **kwargs: None
    discoverer._search = MagicMock()
    discoverer._search.chat_completion.return_value = (
        '<h2>Zion</h2><ul>'
        '<li>Angels Landing <a href="https://example.com/angels">Source</a>'
        ' <a href="https://www.google.com/maps/search/?api=1&query=Angels+Landing+Zion+National+Park">Maps</a></li>'
        '</ul>'
    )
    discoverer._direct_batch_rows_from_html = lambda html: [
        {
            "title": "Angels Landing",
            "url": "https://example.com/angels",
            "maps_url": "https://www.google.com/maps/search/?api=1&query=Angels+Landing+Zion+National+Park",
        }
    ] if '<h2>' in html else []

    cache_key = discoverer._batch_cache_key("Zion National Park", "October 7-9, 2026|html|attraction")
    cache = {cache_key: []}

    rows = discoverer._get_direct_batch_html_rows_for_destination(
        cache=cache,
        destination="Zion National Park",
        dates="October 7-9, 2026",
        kind="attraction",
    )

    assert rows
    assert rows[0]["title"] == "Angels Landing"
    assert discoverer._search.chat_completion.call_count == 1


def test_direct_batch_html_failure_cooldown_short_circuits_repeat_callers():
    """Regression for a real production incident: under a sustained xAI
    outage, every item at a destination that needs the same harvest key
    independently re-triggered a full multi-attempt timeout cycle for a call
    that had just failed seconds earlier, turning one slow endpoint into a
    pile-up. A caller within the cooldown window must get [] immediately
    without touching the network again."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._request_cache_lock = Lock()
    discoverer._direct_batch_html_key_locks = {}
    discoverer._direct_batch_html_failure_ts = {}
    discoverer._direct_batch_html_failure_cooldown_seconds = 180.0
    discoverer._direct_link_batch_limit = lambda: 3
    # Disable the unrelated "insufficient rows" in-call retry-prompt so this
    # test isolates the across-call cooldown behavior being verified here.
    discoverer._direct_batch_min_required = lambda kind: 0
    discoverer._persist_direct_batch_html_capture = lambda **kwargs: None
    discoverer._search = MagicMock()
    discoverer._search.chat_completion.return_value = ""
    discoverer._direct_batch_rows_from_html = lambda html: []

    cache: dict = {}
    first = discoverer._get_direct_batch_html_rows_for_destination(
        cache=cache, destination="Zion National Park", dates="Oct 7-9, 2026", kind="trail"
    )
    second = discoverer._get_direct_batch_html_rows_for_destination(
        cache=cache, destination="Zion National Park", dates="Oct 7-9, 2026", kind="trail"
    )

    assert first == []
    assert second == []
    # Second call short-circuited on the cooldown instead of retrying.
    assert discoverer._search.chat_completion.call_count == 1


def test_direct_batch_html_failure_cooldown_expires_and_allows_retry():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._request_cache_lock = Lock()
    discoverer._direct_batch_html_key_locks = {}
    discoverer._direct_batch_html_failure_ts = {}
    discoverer._direct_batch_html_failure_cooldown_seconds = 0.0
    discoverer._direct_link_batch_limit = lambda: 3
    discoverer._direct_batch_min_required = lambda kind: 0
    discoverer._persist_direct_batch_html_capture = lambda **kwargs: None
    discoverer._search = MagicMock()
    discoverer._search.chat_completion.side_effect = [
        "",
        '<h2>Zion</h2><ul><li>Angels Landing <a href="https://example.com">Source</a></li></ul>',
    ]
    discoverer._direct_batch_rows_from_html = lambda html: (
        [{"title": "Angels Landing", "url": "https://example.com"}] if "<h2>" in html else []
    )

    cache: dict = {}
    first = discoverer._get_direct_batch_html_rows_for_destination(
        cache=cache, destination="Zion National Park", dates="Oct 7-9, 2026", kind="trail"
    )
    second = discoverer._get_direct_batch_html_rows_for_destination(
        cache=cache, destination="Zion National Park", dates="Oct 7-9, 2026", kind="trail"
    )

    assert first == []
    assert second
    assert discoverer._search.chat_completion.call_count == 2


def test_direct_batch_html_coalesces_concurrent_callers_for_same_key():
    """Two threads asking for the same destination/kind/dates at the same
    time must share one network call, not fire two."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._request_cache_lock = Lock()
    discoverer._direct_batch_html_key_locks = {}
    discoverer._direct_batch_html_failure_ts = {}
    discoverer._direct_batch_html_failure_cooldown_seconds = 180.0
    discoverer._direct_link_batch_limit = lambda: 3
    discoverer._direct_batch_min_required = lambda kind: 1
    discoverer._persist_direct_batch_html_capture = lambda **kwargs: None

    call_count = {"n": 0}
    release_event = threading.Event()

    def _slow_chat_completion(**kwargs):
        call_count["n"] += 1
        release_event.wait(timeout=5)
        return '<h2>Zion</h2><ul><li>Angels Landing <a href="https://example.com">Source</a></li></ul>'

    discoverer._search = MagicMock()
    discoverer._search.chat_completion.side_effect = _slow_chat_completion
    discoverer._direct_batch_rows_from_html = lambda html: (
        [{"title": "Angels Landing", "url": "https://example.com"}] if "<h2>" in html else []
    )

    cache: dict = {}
    results: list = []

    def _worker():
        results.append(
            discoverer._get_direct_batch_html_rows_for_destination(
                cache=cache, destination="Zion National Park", dates="Oct 7-9, 2026", kind="trail"
            )
        )

    t1 = threading.Thread(target=_worker)
    t2 = threading.Thread(target=_worker)
    t1.start()
    # Give thread 1 a moment to acquire the per-key lock and enter the fetch
    # before starting thread 2, so it observes an in-flight fetch rather than
    # racing to grab the lock first itself.
    time.sleep(0.1)
    t2.start()
    time.sleep(0.1)
    release_event.set()
    t1.join(timeout=5)
    t2.join(timeout=5)

    assert call_count["n"] == 1
    assert len(results) == 2
    assert all(r for r in results)


def test_zion_attraction_direct_batch_html_integration_round_trip(tmp_path):
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._request_cache_lock = Lock()
    discoverer._attraction_direct_batch_cache = {}
    discoverer._run_output_dir = tmp_path
    discoverer._direct_batch_html_capture_enabled = True
    discoverer._direct_batch_html_capture_subdir = "dev/url_discovery_direct_batch_html"
    discoverer._search = MagicMock()
    discoverer._search.chat_completion.return_value = (
        "<h2>Zion National Park</h2><ul>"
        "<li>Angels Landing <a href=\"https://example.com/angels\">Source</a> "
        "<a href=\"https://www.google.com/maps/search/?api=1&query=Angels+Landing+Zion+National+Park\">Maps</a></li>"
        "<li>The Narrows <a href=\"https://example.com/narrows\">Source</a> "
        "<a href=\"https://www.google.com/maps/search/?api=1&query=The+Narrows+Zion+National+Park\">Maps</a></li>"
        "</ul>"
    )

    rows = discoverer._get_attraction_direct_batch_rows_for_destination("Zion National Park", "October 18, 2026")

    assert len(rows) == 2
    assert {row["title"] for row in rows} == {"Angels Landing", "The Narrows"}

    capture_dir = tmp_path / "dev" / "url_discovery_direct_batch_html"
    html_files = list(capture_dir.glob("*.html"))
    meta_files = list(capture_dir.glob("*.meta.json"))
    assert len(html_files) == 1
    assert len(meta_files) == 1
    assert "Zion National Park" in html_files[0].read_text(encoding="utf-8")

    meta = json.loads(meta_files[0].read_text(encoding="utf-8"))
    assert meta["destination"] == "Zion National Park"
    assert meta["kind"] == "attraction"
    assert meta["row_count"] == 2


def test_zion_all_trail_items_still_capture_attraction_direct_batch_payload(tmp_path):
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._request_cache_lock = Lock()
    discoverer._attraction_direct_batch_cache = {}
    discoverer._run_output_dir = tmp_path
    discoverer._direct_batch_html_capture_enabled = True
    discoverer._direct_batch_html_capture_subdir = "dev/url_discovery_direct_batch_html"
    discoverer._attraction_source = "direct_link_batch"
    discoverer._direct_batch_authoritative = True
    discoverer._disable_trails = True
    discoverer._search = MagicMock()
    discoverer._search.chat_completion.return_value = (
        "<h2>Zion National Park</h2><ul>"
        "<li>Zion Human History Museum <a href=\"https://www.nps.gov/zion/learn/historyculture/zion-human-history-museum.htm\">Source</a></li>"
        "</ul>"
    )
    ai = {
        "top_attractions": [
            {"name": "The Narrows", "type": "hike", "description": "River hike."},
            {"name": "Emerald Pools Trail", "type": "trail", "description": "Pool trail."},
            {"name": "Canyon Overlook Trail", "type": "hike", "description": "Overlook hike."},
        ]
    }

    discoverer._discover_attractions(
        ai,
        "Zion National Park",
        "zion",
        "October 18, 2026",
        seed_names=["The Narrows"],
    )

    capture_dir = tmp_path / "dev" / "url_discovery_direct_batch_html"
    meta_files = list(capture_dir.glob("zion-national-park.attraction.*.meta.json"))
    assert len(meta_files) == 1
    meta = json.loads(meta_files[0].read_text(encoding="utf-8"))
    assert meta["destination"] == "Zion National Park"
    assert meta["kind"] == "attraction"
    assert meta["row_count"] == 1


def test_html_capture_replay_harness_returns_clickable_links(tmp_path):
    capture_dir = tmp_path / "url_discovery_direct_batch_html"
    capture_dir.mkdir(parents=True)
    html_text = (
        '<h2>Capitol Reef</h2><ul>'
        '<li>Capitol Reef Scenic Drive <a href="https://example.com/scenic-drive">Source</a>'
        ' <a href="https://www.google.com/maps/search/?api=1&query=Capitol+Reef+Scenic+Drive">Maps</a>'
        '</li></ul>'
    )
    html_file = capture_dir / "capitol-reef.attraction.2026.html"
    html_file.write_text(html_text, encoding="utf-8")
    meta_file = capture_dir / "capitol-reef.attraction.2026.meta.json"
    meta_file.write_text(
        json.dumps(
            {
                "destination": "Capitol Reef National Park",
                "dates": "October 21, 2026",
                "kind": "attraction",
                "query": "Generate local attractions for Capitol Reef National Park.",
                "html_file": html_file.name,
            }
        ),
        encoding="utf-8",
    )

    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    entries = discoverer.replay_html_capture_directory(capture_dir)

    assert len(entries) == 1
    assert entries[0]["destination"] == "Capitol Reef National Park"
    assert entries[0]["rows"][0]["title"] == "Capitol Reef Scenic Drive"
    assert "https://example.com/scenic-drive" in entries[0]["clickable_links"][0]


def test_html_capture_replay_harness_can_write_report_file(tmp_path):
    capture_dir = tmp_path / "url_discovery_direct_batch_html"
    capture_dir.mkdir(parents=True)
    html_text = (
        '<h2>Capitol Reef</h2><ul>'
        '<li>Capitol Reef Scenic Drive <a href="https://example.com/scenic-drive">Source</a>'
        ' <a href="https://www.google.com/maps/search/?api=1&query=Capitol+Reef+Scenic+Drive">Maps</a>'
        '</li></ul>'
    )
    html_file = capture_dir / "capitol-reef.attraction.2026.html"
    html_file.write_text(html_text, encoding="utf-8")
    meta_file = capture_dir / "capitol-reef.attraction.2026.meta.json"
    meta_file.write_text(
        json.dumps(
            {
                "destination": "Capitol Reef National Park",
                "dates": "October 21, 2026",
                "kind": "attraction",
                "query": "Generate local attractions for Capitol Reef National Park.",
                "html_file": html_file.name,
            }
        ),
        encoding="utf-8",
    )

    report_path = tmp_path / "reports" / "url_discovery_replay_report.html"
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    entries = discoverer.replay_html_capture_directory(capture_dir, output_path=report_path)

    assert len(entries) == 1
    assert report_path.exists()
    report_text = report_path.read_text(encoding="utf-8")
    assert "Capitol Reef National Park" in report_text
    assert "Generate local attractions for Capitol Reef National Park." in report_text
    assert "https://example.com/scenic-drive" in report_text
    assert "Source" in report_text or "official" in report_text.lower()


def test_is_generic_restaurant_landing_url_distinguishes_specific_vs_area_pages():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    assert not discoverer._is_generic_restaurant_landing_url(
        "https://www.bearpawcafe.com",
        "Bear Paw Cafe",
        "St. George, Utah",
    )
    assert discoverer._is_generic_restaurant_landing_url(
        "https://www.tripadvisor.com/Restaurants-g60771-Zion_National_Park_Utah.html",
        "Bear Paw Cafe",
        "St. George, Utah",
    )
    assert not discoverer._is_generic_restaurant_landing_url(
        "https://www.tripadvisor.com/Restaurant_Review-g28964-d1234567-Reviews-Benja_Thai_Sushi-St_George_Utah.html",
        "Benja Thai & Sushi",
        "St. George, Utah",
    )
    # "RestaurantsNear-g..." (no hyphen before "Near") is TripAdvisor's other
    # area-listing URL shape, distinct from "Restaurants-g...-near" -- seen in
    # the wild as a rejected restaurant-name substitute (Dipstick48).
    assert discoverer._is_generic_restaurant_landing_url(
        "https://www.tripadvisor.com/RestaurantsNear-g143057-d143021-Zion_National_Park_Utah.html",
        "Bear Paw Cafe",
        "Zion National Park, Utah",
    )


def test_looks_like_item_specific_homepage_distinguishes_brand_homepage_from_city_page():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._fetch_page_text = lambda *args, **kwargs: (False, 403, "")

    assert discoverer._looks_like_item_specific_homepage(
        "https://anasazisteakhouse.com",
        "Anasazi Steakhouse & Grill",
    )
    assert not discoverer._looks_like_item_specific_homepage(
        "https://www.stgeorgeutah.com",
        "Bear Paw Cafe",
    )


def test_generic_section_landing_page_catches_bare_nps_park_code_homepage():
    """Regression for dipstick58: real Canyonlands seed attraction "Island in
    the Sky" (a specific district within the park) rendered linked to
    https://www.nps.gov/cany/ -- the bare park-code homepage, not a
    district-specific page -- because _is_generic_section_landing_page only
    caught an empty path or a small set of named generic segments
    ("plan-your-visit", "about", etc.), not a bare single-segment NPS unit
    code like "cany". This is the same "area-reference instead of
    subject-specific destination" pattern PR-011 already polices for other
    URL shapes.
    """
    assert URLDiscoverer._is_generic_section_landing_page("https://www.nps.gov/cany/")
    assert URLDiscoverer._is_generic_section_landing_page("https://www.nps.gov/zion")
    # A real district/subject-specific page under the same park must not be
    # caught by this new rule.
    assert not URLDiscoverer._is_generic_section_landing_page(
        "https://www.nps.gov/cany/planyourvisit/islandinthesky.htm"
    )
    # Non-nps.gov hosts with an incidental 4-letter path segment are unaffected.
    assert not URLDiscoverer._is_generic_section_landing_page("https://www.example.com/blog/")


def test_generic_section_landing_page_catches_tripadvisor_things_to_do_listing():
    """Regression: the project owner found a real published link that's a
    generic listing/landing page, not a specific attraction -- "THE 15 BEST
    Things to Do in Pagosa Springs (2026) - Tripadvisor", which resolves to
    https://www.tripadvisor.com/Attractions-g33584-Activities-Pagosa_Springs_Colorado.html
    (confirmed via live search). TripAdvisor's "Attractions-g<id>-Activities-
    <city>.html" URL shape is a category/listing page for the whole
    destination -- distinct from its "Attraction_Review-g<id>-d<id>-Reviews-
    <name>.html" shape for one specific named place, which must not be
    caught by this check.
    """
    assert URLDiscoverer._is_generic_section_landing_page(
        "https://www.tripadvisor.com/Attractions-g33584-Activities-Pagosa_Springs_Colorado.html"
    )
    # A category-filtered variant of the same listing shape (e.g. paginated or
    # scoped to a sub-category) is still a listing page, not a specific place.
    assert URLDiscoverer._is_generic_section_landing_page(
        "https://www.tripadvisor.com/Attractions-g33584-Activities-c47-Pagosa_Springs_Colorado.html"
    )
    # A genuinely specific TripAdvisor attraction review page must not be caught.
    assert not URLDiscoverer._is_generic_section_landing_page(
        "https://www.tripadvisor.com/Attraction_Review-g33584-d123456-Reviews-"
        "Chimney_Rock_National_Monument-Pagosa_Springs_Colorado.html"
    )


def test_retain_discovered_url_rebuilds_en_route_maps_search_query_with_destination():
    """Item 2 fix (project owner: "Red Hollow Canyon...falls back to a Map
    link for the primary, but not one specific to that location", other
    en-route stops affected too): when an en-route stop's Google Maps search
    URL gets rebuilt (allow_google_maps_search=True, the leniency
    `_search_en_route_stop_from_direct_batch` already grants its own
    candidates), the rebuild must always end up with a destination-qualified
    query -- not just whatever the plain `_maps_fallback_query_text` builder
    would produce, which omits the destination whenever the item name alone
    looks "location-qualified" (e.g. contains a comma). That heuristic is
    tuned for attractions/restaurants that live inside the destination; an
    en-route stop lives along the leg between two places and always benefits
    from an explicit destination anchor in its fallback query, which is
    exactly what `_en_route_maps_fallback_query_text` (already used
    everywhere else in this file for en-route fallback queries) guarantees.
    """
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._url_policy_mode = "enforce"

    # "Sunrise Point, Utah" looks location-qualified to the generic builder
    # (comma) -- that builder alone would drop the destination entirely.
    bad_query_url = "https://www.google.com/maps/search/?api=1&query=Sunrise+Point"
    out = discoverer._retain_discovered_url(
        bad_query_url,
        "Sunrise Point, Utah",
        "Bryce Canyon National Park",
        allow_alltrails=False,
        kind="en_route_stop",
        allow_google_maps_search=True,
    )
    assert out != ""
    assert out != bad_query_url
    assert "Sunrise+Point" in out or "Sunrise%20Point" in out
    assert "Bryce" in out, out

    # The hyphenated "en-route stop" spelling must behave identically.
    out2 = discoverer._retain_discovered_url(
        bad_query_url,
        "Sunrise Point, Utah",
        "Bryce Canyon National Park",
        allow_alltrails=False,
        kind="en-route stop",
        allow_google_maps_search=True,
    )
    assert "Bryce" in out2, out2


def test_retain_discovered_url_rejects_tripadvisor_listing_for_en_route_stop_kind():
    """Regression: _retain_discovered_url's generic-section-landing-page gate
    only checked kind in {"generic", "attraction", "en-route stop",
    "getting_there route option"} -- but the en-route-stop "preserve existing
    direct-link-batch URL" call site (_discover_en_route_stops) passes
    kind="en_route_stop" (underscore), not "en-route stop" (hyphen/space).
    That string mismatch let a TripAdvisor "Things to Do" listing page sail
    straight through the gate whenever it showed up as an en-route stop's
    already-attached URL, even though the exact same URL is correctly
    rejected for kind="attraction". Two other kind-gated checks in this same
    function already treat "en-route stop" and "en_route_stop" as synonyms
    (see the google-maps-place-url and alltrails-slug-corroboration checks
    just above/below this one) -- this gate had fallen out of sync with that
    precedent.
    """
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    listing_url = "https://www.tripadvisor.com/Attractions-g33584-Activities-Pagosa_Springs_Colorado.html"

    assert (
        discoverer._retain_discovered_url(
            listing_url,
            "Pagosa Springs",
            "Pagosa Springs",
            allow_alltrails=False,
            kind="en_route_stop",
        )
        == ""
    )
    # Same rejection already worked for the correctly-spelled kind string --
    # confirm both spellings now agree.
    assert (
        discoverer._retain_discovered_url(
            listing_url,
            "Pagosa Springs",
            "Pagosa Springs",
            allow_alltrails=False,
            kind="en-route stop",
        )
        == ""
    )
    # A genuinely specific attraction review page for this kind must still be
    # preserved -- the fix must not over-reject real content.
    specific_url = (
        "https://www.tripadvisor.com/Attraction_Review-g33584-d123456-Reviews-"
        "Chimney_Rock_National_Monument-Pagosa_Springs_Colorado.html"
    )
    assert (
        discoverer._retain_discovered_url(
            specific_url,
            "Chimney Rock National Monument",
            "Pagosa Springs",
            allow_alltrails=False,
            kind="en_route_stop",
        )
        == specific_url
    )


def test_discover_en_route_stops_direct_batch_discards_stale_tripadvisor_listing_url():
    """End-to-end regression for the same bug: an en-route stop that already
    carries a generic TripAdvisor "Things to Do" listing URL (e.g. left over
    from an earlier harvest, or a row whose own URL field was never a
    specific place) must not have that URL preserved verbatim -- it must be
    discarded and re-resolved via the normal direct-batch search, unlike
    test_discover_en_route_stops_direct_batch_preserves_existing_url_without_rematch
    where the existing URL is a real specific page and correctly is kept.
    """
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._en_route_source = "direct_link_batch"

    ai = {
        "getting_here": {
            "en_route_stops": [
                {
                    "name": "Chimney Rock National Monument",
                    "url": "https://www.tripadvisor.com/Attractions-g33584-Activities-Pagosa_Springs_Colorado.html",
                    "detour_time_minutes": 10,
                }
            ]
        }
    }

    with patch.object(
        discoverer,
        "_search_en_route_stop_from_direct_batch",
        return_value="https://www.tripadvisor.com/Attraction_Review-g33584-d123456-Reviews-"
        "Chimney_Rock_National_Monument-Pagosa_Springs_Colorado.html",
    ) as batch_search:
        with patch.object(discoverer, "_search_first") as fallback_search:
            discoverer._discover_en_route_stops(ai, "Pagosa Springs", origin_name="Durango")

    stop = ai["getting_here"]["en_route_stops"][0]
    assert stop["url"] != "https://www.tripadvisor.com/Attractions-g33584-Activities-Pagosa_Springs_Colorado.html"
    assert stop["url"] == (
        "https://www.tripadvisor.com/Attraction_Review-g33584-d123456-Reviews-"
        "Chimney_Rock_National_Monument-Pagosa_Springs_Colorado.html"
    )
    batch_search.assert_called_once()
    fallback_search.assert_not_called()


def test_discover_en_route_stops_rebuilds_bad_query_existing_maps_url_instead_of_discarding():
    """Item 2 fix, companion to the TripAdvisor-listing test above: an
    en-route stop's existing (harvest-provided) `url` can itself be an
    AI-authored Google Maps search link carrying whatever raw query text the
    model wrote -- e.g. just the bare item name, missing destination
    context (project owner's real "Red Hollow Canyon" example). Before this
    fix, `_discover_en_route_stops`'s existing-url preservation call to
    `_retain_discovered_url` did not pass `allow_google_maps_search=True`,
    so a blocked-class Maps-search URL was simply rejected outright in
    enforce mode -- discarding the stop's only link and forcing a fresh
    (possibly failing) re-search -- instead of being repaired the same way
    `_search_en_route_stop_from_direct_batch`'s own per-candidate check
    already repairs one. Now it must be rebuilt with a controlled,
    destination-qualified query and kept, without triggering the two
    fallback searches at all.
    """
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._en_route_source = "direct_link_batch"
    discoverer._url_policy_mode = "enforce"

    ai = {
        "getting_here": {
            "en_route_stops": [
                {
                    "name": "Red Hollow Canyon",
                    "url": "https://www.google.com/maps/search/?api=1&query=Red+Hollow+Canyon",
                    "detour_time_minutes": 15,
                }
            ]
        }
    }

    with patch.object(discoverer, "_search_en_route_stop_from_direct_batch") as batch_search:
        with patch.object(discoverer, "_search_first") as fallback_search:
            discoverer._discover_en_route_stops(ai, "Bryce Canyon National Park", origin_name="Zion National Park")

    stop = ai["getting_here"]["en_route_stops"][0]
    assert stop["url"] != "https://www.google.com/maps/search/?api=1&query=Red+Hollow+Canyon"
    assert "Bryce" in stop["url"], stop["url"]
    batch_search.assert_not_called()
    fallback_search.assert_not_called()


def test_looks_like_item_specific_homepage_allows_bare_nps_code_only_for_that_exact_park():
    """The bare nps.gov/<code>/ homepage newly caught above as generic must
    still pass through for the one item it's genuinely correct for: an
    attraction that names the park itself (e.g. "Canyonlands National Park").
    Any other item within that park -- like the real "Island in the Sky"
    district from dipstick58 -- still needs a more specific page.
    """
    discoverer = URLDiscoverer.__new__(URLDiscoverer)

    assert discoverer._looks_like_item_specific_homepage(
        "https://www.nps.gov/cany/",
        "Canyonlands National Park",
    )
    assert not discoverer._looks_like_item_specific_homepage(
        "https://www.nps.gov/cany/",
        "Island in the Sky",
    )


def test_alltrails_slug_matches_item_requires_non_generic_anchor_token():
    assert not URLDiscoverer._alltrails_slug_matches_item(
        "https://www.alltrails.com/trail/us/colorado/cornet-creek-falls",
        "Bear Creek Falls",
    )


def test_alltrails_slug_matches_item_rejects_off_by_one_trail_swap():
    assert not URLDiscoverer._alltrails_slug_matches_item(
        "https://www.alltrails.com/trail/us/colorado/piedra-falls-trail",
        "San Juan River Walk",
    )


def test_alltrails_slug_matches_item_rejects_combined_trail_slug():
    """Real dipstick69 bug: Bryce Canyon's 'Navajo Loop Trail' (a real ~1.3
    mile loop, per the AI's own description) token-overlap-matched
    'navajo-loop-trail-to-peekaboo-loop' -- a real but different, ~5.3 mile
    COMBINED trail joining Navajo Loop with Peekaboo Loop -- because
    'navajo'/'loop'/'trail' are all subset tokens of the slug. The rendered
    card ended up linking its "1.3 mile loop" description to a page
    describing a 5.3-mile route. AllTrails' own naming convention marks a
    joined/combined route with a '-to-' segment; that must be rejected
    unless the item's own name also legitimately describes a combined route."""
    assert not URLDiscoverer._alltrails_slug_matches_item(
        "https://www.alltrails.com/trail/us/utah/navajo-loop-trail-to-peekaboo-loop",
        "Navajo Loop Trail",
    )


def test_alltrails_slug_matches_item_accepts_standalone_trail_slug():
    """Companion positive control: the same trail's own standalone slug (no
    '-to-' combinator) must still be accepted."""
    assert URLDiscoverer._alltrails_slug_matches_item(
        "https://www.alltrails.com/trail/us/utah/navajo-loop-trail",
        "Navajo Loop Trail",
    )


def test_alltrails_slug_matches_item_allows_combined_slug_when_item_itself_says_to():
    """When the trip owner's own seed name legitimately describes a combined
    route (contains the word 'to'), a matching '-to-' slug must not be
    rejected purely for containing that convention."""
    assert URLDiscoverer._alltrails_slug_matches_item(
        "https://www.alltrails.com/trail/us/utah/navajo-loop-trail-to-peekaboo-loop",
        "Navajo Loop Trail to Peekaboo Loop",
    )



def test_search_attraction_direct_batch_authoritative_prefers_item_specific_url_over_generic_landing_page():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._direct_batch_authoritative = True

    rows = [
        {
            "url": "https://www.visitutah.com/places-to-go/cities-and-towns/st-george",
            "title": "St. George area attractions",
            "snippet": "Snow Canyon State Park details and driving tips for the area.",
        },
        {
            "url": "https://www.nps.gov/statepark/snow-canyon/",
            "title": "Snow Canyon State Park",
            "snippet": "Snow Canyon State Park official park page.",
        },
    ]

    with patch.object(discoverer, "_get_attraction_direct_batch_rows_for_destination", return_value=rows):
        with patch.object(discoverer, "_retain_discovered_url", side_effect=lambda u, *_a, **_k: u):
            out = discoverer._search_attraction_from_direct_batch(
                "Snow Canyon State Park",
                "St. George, Utah",
                "October 18, 2026",
            )

    assert out == "https://www.nps.gov/statepark/snow-canyon/"


def test_search_attraction_direct_batch_authoritative_uses_maps_link_from_snippet_text():
    """SUPERSEDED BY GH #59, 2026-09-08.

    This asserted that a town's own landing page is an acceptable link for a
    specific attraction inside it, on the strength of the batch row's snippet
    naming the item. #59 is the same shape seen from the other side -- "Red
    Canyon" linking to a generic Capitol Reef listing -- and says such a link
    must be refused.

    The snippet is not the distinguishing evidence it looks like: a
    destination's overview page legitimately names the things inside it, so
    almost any of them can produce a row whose text matches. Fetched live, the
    Capitol Reef page contains "hickman bridge", "cathedral valley" and
    "sulphur creek".

    Kept as a test rather than deleted, with the assertion reversed, because
    the case is still the one worth pinning -- only the answer changed.
    """
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._direct_batch_authoritative = True

    rows = [
        {
            "url": "https://www.visitutah.com/places-to-go/cities-and-towns/st-george",
            "title": "St. George area attractions",
            "snippet": "Snow Canyon State Park details. Google Maps: https://maps.google.com/?q=Snow+Canyon+State+Park+Utah",
        }
    ]

    with patch.object(discoverer, "_get_attraction_direct_batch_rows_for_destination", return_value=rows):
        out = discoverer._search_attraction_from_direct_batch(
            "Snow Canyon State Park",
            "St. George, Utah",
            "October 18, 2026",
        )

    assert not out, "the town's own page is not a specific attraction's link (GH #59)"


def test_search_attraction_direct_batch_authoritative_rejects_snippet_maps_link_for_other_item():
    """SUPERSEDED BY GH #59, 2026-09-08.

    This asserted that a town's own landing page is an acceptable link for a
    specific attraction inside it, on the strength of the batch row's snippet
    naming the item. #59 is the same shape seen from the other side -- "Red
    Canyon" linking to a generic Capitol Reef listing -- and says such a link
    must be refused.

    The snippet is not the distinguishing evidence it looks like: a
    destination's overview page legitimately names the things inside it, so
    almost any of them can produce a row whose text matches. Fetched live, the
    Capitol Reef page contains "hickman bridge", "cathedral valley" and
    "sulphur creek".

    Kept as a test rather than deleted, with the assertion reversed, because
    the case is still the one worth pinning -- only the answer changed.
    """
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._direct_batch_authoritative = True

    rows = [
        {
            "url": "https://www.visitutah.com/places-to-go/cities-and-towns/st-george",
            "title": "St. George area attractions",
            "snippet": "Pioneer Park listed here. Google Maps: https://maps.google.com/?q=St+George+Art+Museum+Utah",
        }
    ]

    with patch.object(discoverer, "_get_attraction_direct_batch_rows_for_destination", return_value=rows):
        out = discoverer._search_attraction_from_direct_batch(
            "Pioneer Park",
            "St. George, Utah",
            "October 18, 2026",
        )

    assert not out, "the town's own page is not a specific attraction's link (GH #59)"


def test_search_attraction_direct_batch_authoritative_keeps_item_matching_generic_landing_page():
    """SUPERSEDED BY GH #59, 2026-09-08.

    This asserted that a town's own landing page is an acceptable link for a
    specific attraction inside it, on the strength of the batch row's snippet
    naming the item. #59 is the same shape seen from the other side -- "Red
    Canyon" linking to a generic Capitol Reef listing -- and says such a link
    must be refused.

    The snippet is not the distinguishing evidence it looks like: a
    destination's overview page legitimately names the things inside it, so
    almost any of them can produce a row whose text matches. Fetched live, the
    Capitol Reef page contains "hickman bridge", "cathedral valley" and
    "sulphur creek".

    Kept as a test rather than deleted, with the assertion reversed, because
    the case is still the one worth pinning -- only the answer changed.
    """
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._direct_batch_authoritative = True

    rows = [
        {
            "url": "https://www.visitutah.com/places-to-go/cities-and-towns/st-george",
            "title": "St. George area attractions",
            "snippet": "Snow Canyon State Park details and driving tips for the area.",
        }
    ]

    with patch.object(discoverer, "_get_attraction_direct_batch_rows_for_destination", return_value=rows):
        out = discoverer._search_attraction_from_direct_batch(
            "Snow Canyon State Park",
            "St. George, Utah",
            "October 18, 2026",
        )

    assert not out, "the town's own page is not a specific attraction's link (GH #59)"


def test_search_attraction_direct_batch_authoritative_accepts_valid_feature_name_variant() -> None:
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._direct_batch_authoritative = True

    rows = [
        {
            "title": "Petroglyph Panel Viewpoint",
            "name": "Petroglyph Panel Viewpoint",
            "url": "https://www.nps.gov/care/learn/historyculture/petroglyphs.htm",
            "maps_url": "https://maps.google.com/?q=Petroglyph+Panel+Viewpoint+Torrey+UT",
            "snippet": "Petroglyph Panel Viewpoint official NPS page for local petroglyphs.",
        }
    ]

    with patch.object(discoverer, "_get_attraction_direct_batch_rows_for_destination", return_value=rows):
        out = discoverer._search_attraction_from_direct_batch(
            "Fremont Petroglyphs",
            "Capitol Reef National Park",
            "October 21-22, 2026",
        )

    assert out == "https://www.nps.gov/care/learn/historyculture/petroglyphs.htm"


def test_discover_attractions_direct_batch_authoritative_no_match_assigns_maps_fallback():
    """Regression for the SW2026-dipstick63 "no URL or maps fallback" quality
    gate spike (13-14 unverified attractions in one real run). Previously,
    when authoritative direct-link-batch mode found no matching harvest row
    for a real, non-ambiguous attraction name, the code cleared url/maps_url
    and gave up entirely -- leaving the card with literally no link, not even
    the safe Google-Maps-search-by-name fallback every other "no URL found"
    attraction gets. A maps-search-query link doesn't assert "this is
    definitely the correct source page" the way a direct hyperlink does, so
    it doesn't carry the fabrication risk authoritative mode exists to
    guard against; it should still be assigned here.
    """
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._direct_batch_authoritative = True
    discoverer._attraction_source = "direct_link_batch"

    ai = {
        "top_attractions": [
            {
                "name": "Snow Canyon State Park",
                "type": "attraction",
                "description": "Red rock landscape with overlooks and short walks.",
            }
        ]
    }

    with patch.object(discoverer, "_search_attraction_from_direct_batch", return_value=None):
        with patch.object(discoverer, "_search_attraction_from_item_query_fanout") as fanout_search:
            discoverer._discover_attractions(ai, "St. George, Utah", None, "October 18, 2026")

    out = ai["top_attractions"][0]
    assert out["url"] == (
        "https://www.google.com/maps/search/?api=1&query=Snow%20Canyon%20State%20Park"
    )
    assert out["maps_url"] == out["url"]
    stats = getattr(discoverer, "_decision_stats_by_destination", {}).get("St. George, Utah", {})
    assert stats.get("maps_fallback_assigned", 0) == 1
    assert stats.get("direct_batch_source_locked_no_match", 0) == 0
    source_stats = getattr(discoverer, "_decision_source_stats_by_destination", {}).get("St. George, Utah", {})
    # The final decision recorded is the maps fallback, not the direct-batch
    # miss that preceded it -- _search_attraction_from_direct_batch was still
    # called (see fanout_search.assert_not_called() below confirming no other
    # source was tried), it just didn't produce a loggable candidate.
    assert source_stats.get("maps", 0) == 1
    # Authoritative mode must still not fall back to a live fanout search --
    # only the deterministic, name-based maps-search fallback is safe here.
    fanout_search.assert_not_called()


def test_discover_attractions_trail_like_misclassification_recovers_via_attraction_batch() -> None:
    """Regression for dipstick58: real Bryce Canyon "Bryce Point" (type
    "viewpoint") rendered unverified with no link, even though the attraction
    direct-batch harvest for that exact run had a matching row ("Bryce Point
    Overlook" -> https://www.nps.gov/brca/planyourvisit/brycepoint.htm).

    Root cause: _is_trail_like_attraction's generic keyword catch-all matched
    the word "walk" in the real harvested description ("...via a short drive
    followed by a short walk"), so the item was classified trail_like=True and
    routed into the AllTrails-only direct-batch path. That path predictably
    found no matching trail row (Bryce Point isn't a trail), and in
    authoritative direct-batch mode the trail branch locked to "no match"
    without ever falling through to the general attraction-batch matching
    that sibling items like "Sunrise Point" and "Inspiration Point" (whose
    descriptions had no trail keyword) used successfully in the same real run.

    The fix adds a same-cost fallback: when the trail path's direct-batch
    search finds no trail, try the already-harvested attraction rows before
    giving up.
    """
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._direct_batch_authoritative = True
    discoverer._attraction_source = "direct_link_batch"
    discoverer._alltrails_source = "direct_link_batch"

    ai = {
        "top_attractions": [
            {
                "name": "Bryce Point",
                "type": "viewpoint",
                "description": (
                    "Offering one of the best panoramic views in the park, Bryce "
                    "Point overlooks the main amphitheater. It's accessible via a "
                    "short drive followed by a short walk."
                ),
            }
        ]
    }

    attraction_rows = [
        {
            "title": "Bryce Point Overlook",
            "name": "Bryce Point Overlook",
            "url": "https://www.nps.gov/brca/planyourvisit/brycepoint.htm",
            "maps_url": "https://www.google.com/maps/search/?api=1&query=Bryce+Point+Bryce+Canyon+National+Park+UT",
            "snippet": "Bryce Point Overlook Source Maps 4.7/5 One of the best panoramic views in the park.",
        }
    ]

    with patch.object(discoverer, "_search_alltrails_for_trail_from_direct_batch", return_value=None):
        with patch.object(discoverer, "_search_alltrails_for_trail", return_value=None):
            with patch.object(
                discoverer,
                "_get_attraction_direct_batch_rows_for_destination",
                return_value=attraction_rows,
            ):
                discoverer._discover_attractions(ai, "Bryce Canyon National Park", "brca", "October 19-21, 2026")

    out = ai["top_attractions"][0]
    assert out["url"] == "https://www.nps.gov/brca/planyourvisit/brycepoint.htm"
    stats = getattr(discoverer, "_decision_stats_by_destination", {}).get("Bryce Canyon National Park", {})
    assert stats.get("trail_like_misclassified_attraction_batch_recovered", 0) == 1
    assert stats.get("direct_batch_source_locked_no_match", 0) == 0


def test_inspiration_point_short_walk_description_is_not_trail_like() -> None:
    """Regression for dipstick59: real Bryce Canyon "Inspiration Point" (type
    "viewpoint") rendered unverified with no link ("badge-hike-easy", "⚠
    Unverified") because its real harvested description ("Accessible via a
    short walk from the parking lot, this viewpoint provides an elevated
    look at Bryce Canyon's formations.") tripped the exact same "walk"
    catch-all false positive already patched once for sibling item "Bryce
    Point" (dipstick58, commit for
    test_discover_attractions_trail_like_misclassification_recovers_via_attraction_batch
    above). Unlike Bryce Point, Inspiration Point's attraction-batch row
    wasn't picked up by that earlier fallback fix in this real run (the
    fallback only helps when the item happens to already be present in the
    harvested attraction-batch rows the fallback checks), so this fixes the
    root cause directly in _is_trail_like_attraction: a bare "walk" mention
    only counts as a trail signal when it's in the item's own name (e.g.
    "Riverside Walk") or co-occurs with real trail-length/difficulty signals
    -- not when it's a short/brief/easy walk mentioned alongside a parking
    lot/pullout/overlook/viewpoint cue, which is a non-trail access note.
    """
    discoverer = URLDiscoverer.__new__(URLDiscoverer)

    assert discoverer._is_trail_like_attraction(
        "Inspiration Point",
        "viewpoint",
        (
            "Accessible via a short walk from the parking lot, this "
            "viewpoint provides an elevated look at Bryce Canyon's "
            "formations. It's a great location for photography."
        ),
    ) is False


def test_bryce_point_short_walk_description_without_parking_cue_stays_trail_like() -> None:
    """Companion to the Inspiration Point test above: the root-cause fix must
    not regress the sibling "Bryce Point" case the earlier fallback fix
    (dipstick58) was built around. Bryce Point's real description mentions a
    "short walk" too, but with no parking-lot/pullout/overlook/viewpoint cue
    alongside it, so it's ambiguous rather than a clear non-trail access
    note -- it should stay trail_like=True and keep relying on the existing
    attraction-batch fallback (exercised by
    test_discover_attractions_trail_like_misclassification_recovers_via_attraction_batch
    above) rather than being excluded here.
    """
    discoverer = URLDiscoverer.__new__(URLDiscoverer)

    assert discoverer._is_trail_like_attraction(
        "Bryce Point",
        "viewpoint",
        (
            "Offering one of the best panoramic views in the park, Bryce "
            "Point overlooks the main amphitheater. It's accessible via a "
            "short drive followed by a short walk."
        ),
    ) is True


def test_discover_attractions_inspiration_point_resolves_directly_without_alltrails_detour() -> None:
    """End-to-end companion to test_inspiration_point_short_walk_description_is_not_trail_like:
    with trail_like correctly False, the item should resolve straight through
    the general attraction direct-batch path to its real harvested NPS URL
    (https://www.nps.gov/brca/planyourvisit/inspiration.htm, confirmed present
    in the dipstick59 attraction-batch harvest for Bryce Canyon), without ever
    touching the AllTrails-only trail path -- which would find nothing, since
    Inspiration Point isn't a trail, and previously left the item unverified.
    """
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._direct_batch_authoritative = True
    discoverer._attraction_source = "direct_link_batch"

    ai = {
        "top_attractions": [
            {
                "name": "Inspiration Point",
                "type": "viewpoint",
                "description": (
                    "Accessible via a short walk from the parking lot, this "
                    "viewpoint provides an elevated look at Bryce Canyon's "
                    "formations. It's a great location for photography."
                ),
            }
        ]
    }

    attraction_rows = [
        {
            "title": "Inspiration Point",
            "name": "Inspiration Point",
            "url": "https://www.nps.gov/brca/planyourvisit/inspiration.htm",
            "maps_url": "https://www.google.com/maps/search/?api=1&query=Inspiration+Point+Bryce+Canyon+National+Park+UT",
            "snippet": "Inspiration Point Source Maps 4.6/5",
        }
    ]

    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("trail-only AllTrails path should not run for a non-trail viewpoint")

    with patch.object(discoverer, "_search_alltrails_for_trail_from_direct_batch", side_effect=fail_if_called):
        with patch.object(discoverer, "_search_alltrails_for_trail", side_effect=fail_if_called):
            with patch.object(
                discoverer,
                "_get_attraction_direct_batch_rows_for_destination",
                return_value=attraction_rows,
            ):
                discoverer._discover_attractions(ai, "Bryce Canyon National Park", "brca", "October 19-21, 2026")

    out = ai["top_attractions"][0]
    assert out["url"] == "https://www.nps.gov/brca/planyourvisit/inspiration.htm"
    stats = getattr(discoverer, "_decision_stats_by_destination", {}).get("Bryce Canyon National Park", {})
    assert stats.get("direct_batch_accepted", 0) == 1
    assert stats.get("trail_like_misclassified_attraction_batch_recovered", 0) == 0


def test_paria_view_no_hiking_required_note_is_not_trail_like() -> None:
    """Regression for dipstick59: real Bryce Canyon "Paria View" (type
    "viewpoint") published a real 404 AllTrails URL
    (https://www.alltrails.com/trail/us/utah/paria-view-trail) instead of
    its own correct, harvested NPS page
    (https://www.nps.gov/brca/planyourvisit/paria.htm).

    Root cause: _attraction_trail_context() folds the item's practical_note
    into the text _is_trail_like_attraction scans, and Paria View's real
    practical note is "Accessible with no hiking required; parking is
    limited." -- the bare substring "hiking" tripped the trail-keyword
    catch-all even though the note explicitly says hiking is NOT required.
    That misclassification routed this plain viewpoint down the
    AllTrails-only path, where a same-name-but-different "Paria View
    Trail" AllTrails candidate (present in this run's separate trail-kind
    harvest) got selected in place of the viewpoint's own correct
    attraction-batch row.

    Fixes negated hiking/walking/trail phrasing ("no hiking required",
    "without a hike", "doesn't require any walking") so it's no longer
    counted as a positive trail signal.
    """
    discoverer = URLDiscoverer.__new__(URLDiscoverer)

    assert discoverer._is_trail_like_attraction(
        "Paria View",
        "viewpoint",
        (
            "A less crowded viewpoint offering views of the canyon and "
            "surrounding landscape. It's a perfect spot for a quiet moment "
            "away from the busier areas. Accessible with no hiking "
            "required; parking is limited."
        ),
    ) is False


def test_discover_attractions_paria_view_resolves_to_nps_page_not_alltrails() -> None:
    """End-to-end companion to test_paria_view_no_hiking_required_note_is_not_trail_like:
    with trail_like correctly False, the item should resolve straight
    through the general attraction direct-batch path to its real harvested
    NPS URL, and must never touch the AllTrails-only trail path -- which is
    exactly how the real 404 "paria-view-trail" AllTrails URL got published
    in the dipstick59 run.
    """
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._direct_batch_authoritative = True
    discoverer._attraction_source = "direct_link_batch"

    ai = {
        "top_attractions": [
            {
                "name": "Paria View",
                "type": "viewpoint",
                "description": (
                    "A less crowded viewpoint offering views of the canyon "
                    "and surrounding landscape. It's a perfect spot for a "
                    "quiet moment away from the busier areas."
                ),
                "practical_note": "Accessible with no hiking required; parking is limited.",
            }
        ]
    }

    attraction_rows = [
        {
            "title": "Paria View",
            "name": "Paria View",
            "url": "https://www.nps.gov/brca/planyourvisit/paria.htm",
            "maps_url": "https://www.google.com/maps/search/?api=1&query=Paria+View+Bryce+Canyon+National+Park+UT",
            "snippet": "Paria View Source Maps 4.5/5 Quiet sunset location with fewer visitors.",
        }
    ]

    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("AllTrails-only trail path should not run for a non-trail viewpoint")

    with patch.object(discoverer, "_search_alltrails_for_trail_from_direct_batch", side_effect=fail_if_called):
        with patch.object(discoverer, "_search_alltrails_for_trail", side_effect=fail_if_called):
            with patch.object(
                discoverer,
                "_get_attraction_direct_batch_rows_for_destination",
                return_value=attraction_rows,
            ):
                discoverer._discover_attractions(ai, "Bryce Canyon National Park", "brca", "October 19-21, 2026")

    out = ai["top_attractions"][0]
    assert out["url"] == "https://www.nps.gov/brca/planyourvisit/paria.htm"


def test_discover_attractions_direct_batch_authoritative_uses_item_fanout_when_batch_has_no_match() -> None:
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._direct_batch_authoritative = True
    discoverer._attraction_source = "direct_link_batch"

    ai = {
        "top_attractions": [
            {
                "name": "Snow Canyon State Park",
                "type": "attraction",
                "description": "Red rock landscape with overlooks and short walks.",
            }
        ]
    }

    with patch.object(discoverer, "_search_attraction_from_direct_batch", return_value=None):
        with patch.object(
            discoverer,
            "_search_attraction_from_item_query_fanout",
            return_value=("https://www.nps.gov/snowcanyon", "nps"),
        ) as fanout_search:
            discoverer._discover_attractions(ai, "St. George, Utah", None, "October 18, 2026")

    # Even with a would-be fanout match available, authoritative mode must not
    # call the live fanout search -- it gets the safe maps fallback instead.
    out = ai["top_attractions"][0]
    assert out["url"] == (
        "https://www.google.com/maps/search/?api=1&query=Snow%20Canyon%20State%20Park"
    )
    fanout_search.assert_not_called()


def test_infer_item_nps_code_returns_code_for_known_parks() -> None:
    assert URLDiscoverer._infer_item_nps_code("Arches National Park") == "arch"
    assert URLDiscoverer._infer_item_nps_code("Canyonlands National Park") == "cany"
    assert URLDiscoverer._infer_item_nps_code("Zion National Park") == "zion"
    assert URLDiscoverer._infer_item_nps_code("Capitol Reef National Park") == "care"
    assert URLDiscoverer._infer_item_nps_code("Moab Giants Dinosaur Park") is None
    assert URLDiscoverer._infer_item_nps_code("Desert Bistro") is None


def test_discover_attractions_infers_nps_code_for_park_attraction_at_non_nps_dest() -> None:
    """Arches National Park in non-authoritative mode should use the ordinary broad search path without a forced NPS site override."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._direct_batch_authoritative = False
    discoverer._attraction_source = "search"

    ai = {
        "top_attractions": [
            {
                "name": "Arches National Park",
                "type": "attraction",
                "description": "Iconic redrock arches and fins near Moab.",
            }
        ]
    }

    with patch.object(discoverer, "_search_attraction_from_direct_batch", return_value=None):
        with patch.object(discoverer, "_search_first", return_value="https://www.nps.gov/arch/index.htm") as search_first_mock:
            discoverer._discover_attractions(ai, "Moab", None, "October 13, 2026")

    assert ai["top_attractions"][0]["url"] == "https://www.nps.gov/arch/index.htm"
    first_kwargs = search_first_mock.call_args_list[0].kwargs
    assert first_kwargs["site_filter"] is None
    assert first_kwargs["site_hint"] is None


def test_trail_like_attraction_falls_back_to_maps_not_nps_fanout_when_alltrails_fails() -> None:
    """In authoritative direct-batch mode, trail items with no batch match
    must not fall back to the generic NPS fanout (that path is reserved for
    non-authoritative mode -- see the `not self._direct_batch_is_authoritative()`
    guard a few lines above the fanout call). Since the trail-like-branch
    no-link-fallback fix, they instead get the same safe maps-search
    fallback every other "no URL found" attraction gets, rather than being
    left with no link and no maps fallback at all.
    """
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._direct_batch_authoritative = True
    discoverer._attraction_source = "direct_link_batch"
    discoverer._alltrails_source = "direct_link_batch"

    ai = {
        "top_attractions": [
            {
                "name": "Cassidy Arch",
                "type": "hike",
                "description": "3.4-mile out-and-back to a natural arch. Moderate, 670 ft gain.",
            }
        ]
    }

    with patch.object(discoverer, "_search_alltrails_for_trail", return_value=None):
        with patch.object(discoverer, "_search_alltrails_for_seed_relaxed", return_value=None):
            with patch.object(
                discoverer,
                "_search_attraction_from_item_query_fanout",
                return_value=("https://www.nps.gov/care/planyourvisit/cassidy-arch.htm", "nps"),
            ) as fanout_search:
                with patch.object(discoverer, "_search_attraction_from_direct_batch", return_value=None):
                    discoverer._discover_attractions(
                        ai, "Capitol Reef National Park", "care", "October 11, 2026",
                        seed_names=["Cassidy Arch"],
                    )

    out = ai["top_attractions"][0]
    assert out["url"] == (
        "https://www.google.com/maps/search/?api=1&query=Cassidy%20Arch%20Capitol%20Reef%20National%20Park"
    )
    assert out["maps_url"] == out["url"]
    fanout_search.assert_not_called()


def test_trail_like_attraction_skips_nps_fanout_when_no_nps_code() -> None:
    """Trail items at non-NPS destinations skip the NPS fanout and go straight to maps fallback."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._direct_batch_authoritative = True
    discoverer._attraction_source = "direct_link_batch"
    discoverer._alltrails_source = "direct_link_batch"
    discoverer._url_policy_mode = "enforce"
    discoverer._url_policy_blocked_classes = {"google_maps_search"}
    discoverer._url_policy_allowlisted_urls = set()
    discoverer._url_domain_denylist = frozenset()

    ai = {
        "top_attractions": [
            {
                "name": "Ajax Peak",
                "type": "hike",
                "description": "Strenuous summit scramble above Telluride.",
            }
        ]
    }

    with patch.object(discoverer, "_search_alltrails_for_trail", return_value=None):
        with patch.object(discoverer, "_search_alltrails_for_seed_relaxed", return_value=None):
            with patch.object(discoverer, "_search_attraction_from_item_query_fanout") as fanout_mock:
                discoverer._discover_attractions(ai, "Telluride", None, "October 15, 2026")

    fanout_mock.assert_not_called()


def test_discover_attractions_direct_batch_authoritative_ignores_maps_area_fanout():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._direct_batch_authoritative = True
    discoverer._attraction_source = "direct_link_batch"

    ai = {
        "top_attractions": [
            {
                "name": "Snow Canyon State Park",
                "type": "attraction",
                "description": "Red rock landscape with overlooks and short walks.",
            }
        ]
    }

    with patch.object(discoverer, "_search_attraction_from_direct_batch", return_value=None):
        with patch.object(discoverer, "_search_attraction_from_item_query_fanout") as fanout_search:
            discoverer._discover_attractions(ai, "St. George, Utah", None, "October 18, 2026")

    out = ai["top_attractions"][0]
    assert out["url"] == (
        "https://www.google.com/maps/search/?api=1&query=Snow%20Canyon%20State%20Park"
    )
    stats = getattr(discoverer, "_decision_stats_by_destination", {}).get("St. George, Utah", {})
    assert stats.get("maps_fallback_assigned", 0) == 1
    assert stats.get("direct_batch_source_locked_no_match", 0) == 0
    fanout_search.assert_not_called()


def test_trail_like_direct_batch_authoritative_no_match_assigns_maps_fallback() -> None:
    """Regression for the trail-like-branch counterpart of the attractions
    no-link-fallback fix: real dipstick64 names ("Canyon Overlook Trail",
    "Queens Garden Trail", etc.) rendered with no url and no maps_url because
    authoritative direct-batch mode's trail-like branch cleared both fields
    with no fall-through to the maps-search fallback used a few lines later
    in the same branch (the `trail_maps_fallback_assigned` case) or by the
    equivalent non-trail-branch fallback. It must not call the generic NPS
    fanout (that's a non-authoritative-mode-only recovery path) but it must
    get the safe maps-search fallback instead of being left completely
    unverified.
    """
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._direct_batch_authoritative = True
    discoverer._attraction_source = "direct_link_batch"
    discoverer._alltrails_source = "direct_link_batch"

    ai = {
        "top_attractions": [
            {
                "name": "Cassidy Arch",
                "type": "hike",
                "description": "3.4-mile out-and-back to a natural arch. Moderate, 670 ft gain.",
            }
        ]
    }

    with patch.object(discoverer, "_search_alltrails_for_trail_from_direct_batch", return_value=None):
        with patch.object(discoverer, "_search_alltrails_for_trail", return_value=None):
            with patch.object(discoverer, "_search_attraction_from_item_query_fanout") as fanout_search:
                with patch.object(discoverer, "_search_attraction_from_direct_batch", return_value=None):
                    discoverer._discover_attractions(ai, "Capitol Reef National Park", "care", "October 11, 2026")

    out = ai["top_attractions"][0]
    assert out["url"] == (
        "https://www.google.com/maps/search/?api=1&query=Cassidy%20Arch%20Capitol%20Reef%20National%20Park"
    )
    assert out["maps_url"] == out["url"]
    stats = getattr(discoverer, "_decision_stats_by_destination", {}).get("Capitol Reef National Park", {})
    assert stats.get("maps_fallback_assigned", 0) == 1
    fanout_search.assert_not_called()


def test_trail_like_direct_batch_authoritative_no_match_ambiguous_geography_still_fail_closed() -> None:
    """Uniformity check mirroring the non-trail-branch fail-closed tests:
    an ambiguous, generic geographic trail name must still get no link at
    all (not a maps fallback) even now that the trail-like authoritative
    no-match path falls through to the shared fallback-or-fail-closed logic.
    """
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._direct_batch_authoritative = True
    discoverer._attraction_source = "direct_link_batch"
    discoverer._alltrails_source = "direct_link_batch"

    ai = {
        "top_attractions": [
            {
                "name": "Echo Canyon",
                "type": "hike",
                "description": "A scenic canyon hike near the park entrance.",
            }
        ]
    }

    with patch.object(discoverer, "_search_alltrails_for_trail_from_direct_batch", return_value=None):
        with patch.object(discoverer, "_search_alltrails_for_trail", return_value=None):
            with patch.object(discoverer, "_search_attraction_from_direct_batch", return_value=None):
                discoverer._discover_attractions(ai, "Some National Park", None, "October 18, 2026")

    out = ai["top_attractions"][0]
    assert out["url"] == ""
    assert "maps_url" not in out or not out["maps_url"]
    stats = getattr(discoverer, "_decision_stats_by_destination", {}).get("Some National Park", {})
    assert stats.get("maps_fallback_omitted_ambiguous_geography", 0) == 1
    assert stats.get("maps_fallback_assigned", 0) == 0


def test_discover_attractions_direct_batch_authoritative_no_match_real_bryce_attraction_gets_maps_fallback() -> None:
    """Regression using a real affected name from the SW2026-dipstick63 run:
    Bryce Canyon "Sunrise Point" rendered unverified (no url, no maps_url)
    because authoritative direct-batch mode found no harvest row and, before
    this fix, gave up instead of assigning the same safe maps-search
    fallback every other attraction with no discovered URL gets.
    """
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._direct_batch_authoritative = True
    discoverer._attraction_source = "direct_link_batch"

    ai = {
        "top_attractions": [
            {
                "name": "Sunrise Point",
                "type": "viewpoint",
                "description": "Popular sunrise viewing spot along the amphitheater rim.",
            }
        ]
    }

    with patch.object(discoverer, "_search_attraction_from_direct_batch", return_value=None):
        discoverer._discover_attractions(ai, "Bryce Canyon National Park", "brca", "October 19-21, 2026")

    out = ai["top_attractions"][0]
    assert out["url"] == (
        "https://www.google.com/maps/search/?api=1&query=Sunrise%20Point%20Bryce%20Canyon%20National%20Park"
    )
    assert out["maps_url"] == out["url"]
    stats = getattr(discoverer, "_decision_stats_by_destination", {}).get("Bryce Canyon National Park", {})
    assert stats.get("maps_fallback_assigned", 0) == 1


def test_discover_attractions_direct_batch_authoritative_no_match_real_stgeorge_attraction_gets_maps_fallback() -> None:
    """Second real dipstick63-affected name: St. George Dinosaur Discovery
    Site at Johnson Farm. Confirms the fix isn't specific to one name shape --
    a multi-word, already location-qualified attraction name also recovers a
    usable link instead of being left blank.
    """
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._direct_batch_authoritative = True
    discoverer._attraction_source = "direct_link_batch"

    ai = {
        "top_attractions": [
            {
                "name": "St. George Dinosaur Discovery Site at Johnson Farm",
                "type": "attraction",
                "description": "Museum showcasing preserved dinosaur tracks and fossils.",
            }
        ]
    }

    with patch.object(discoverer, "_search_attraction_from_direct_batch", return_value=None):
        discoverer._discover_attractions(ai, "St. George, Utah", None, "October 18, 2026")

    out = ai["top_attractions"][0]
    assert out["url"] == (
        "https://www.google.com/maps/search/?api=1&query="
        "St.%20George%20Dinosaur%20Discovery%20Site%20at%20Johnson%20Farm"
    )
    assert out["maps_url"] == out["url"]


def test_discover_attractions_direct_batch_authoritative_no_match_ambiguous_geography_still_fail_closed() -> None:
    """The three pre-existing fail-closed exceptions must still apply
    uniformly when they're reached via the authoritative-no-match path, not
    just via the general no-URL path. An ambiguous, generic geographic name
    ("Echo Canyon" -- a name that recurs at many unrelated parks) must still
    get no link at all, not a maps fallback, even now that authoritative mode
    falls through to the shared fallback-or-fail-closed logic.
    """
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._direct_batch_authoritative = True
    discoverer._attraction_source = "direct_link_batch"

    ai = {
        "top_attractions": [
            {
                "name": "Echo Canyon",
                "type": "attraction",
                "description": "A scenic canyon area near the park entrance.",
            }
        ]
    }

    with patch.object(discoverer, "_search_attraction_from_direct_batch", return_value=None):
        discoverer._discover_attractions(ai, "Some National Park", None, "October 18, 2026")

    out = ai["top_attractions"][0]
    assert out["url"] == ""
    assert "maps_url" not in out or not out["maps_url"]
    stats = getattr(discoverer, "_decision_stats_by_destination", {}).get("Some National Park", {})
    assert stats.get("maps_fallback_omitted_ambiguous_geography", 0) == 1
    assert stats.get("maps_fallback_assigned", 0) == 0


def test_discover_attractions_direct_batch_authoritative_no_match_category_activity_still_fail_closed() -> None:
    """Same uniformity check for the category-style-activity fail-closed
    exception (e.g. "stargazing" -- an activity, not a specific named place
    a maps search can meaningfully target).
    """
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._direct_batch_authoritative = True
    discoverer._attraction_source = "direct_link_batch"

    ai = {
        "top_attractions": [
            {
                "name": "Bryce Canyon Stargazing",
                "type": "attraction",
                "description": "Ranger-led evening stargazing program.",
            }
        ]
    }

    with patch.object(discoverer, "_search_attraction_from_direct_batch", return_value=None):
        discoverer._discover_attractions(ai, "Bryce Canyon National Park", "brca", "October 19-21, 2026")

    out = ai["top_attractions"][0]
    assert out["url"] == ""
    stats = getattr(discoverer, "_decision_stats_by_destination", {}).get("Bryce Canyon National Park", {})
    assert stats.get("category_activity_fail_closed", 0) == 1
    assert stats.get("maps_fallback_assigned", 0) == 0


def test_discover_attractions_direct_batch_authoritative_no_match_policy_enforce_still_fail_closed() -> None:
    """Same uniformity check for the enforce-mode URL-policy exception: when
    the google_maps_search URL class is blocked by policy, the authoritative-
    no-match path must respect that too, not just the general no-URL path.
    """
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._direct_batch_authoritative = True
    discoverer._attraction_source = "direct_link_batch"
    discoverer._url_policy_mode = "enforce"
    discoverer._url_policy_blocked_classes = {"google_maps_search"}

    ai = {
        "top_attractions": [
            {
                "name": "Sunrise Point",
                "type": "viewpoint",
                "description": "Popular sunrise viewing spot along the amphitheater rim.",
            }
        ]
    }

    with patch.object(discoverer, "_search_attraction_from_direct_batch", return_value=None):
        discoverer._discover_attractions(ai, "Bryce Canyon National Park", "brca", "October 19-21, 2026")

    out = ai["top_attractions"][0]
    assert out["url"] == ""
    assert "maps_url" not in out or not out["maps_url"]
    stats = getattr(discoverer, "_decision_stats_by_destination", {}).get("Bryce Canyon National Park", {})
    assert stats.get("maps_fallback_omitted_policy_enforce", 0) == 1
    assert stats.get("maps_fallback_assigned", 0) == 0


def test_discover_attractions_direct_batch_authoritative_recovers_seed_from_ai_candidate() -> None:
    """Fabrication-guard tripwire: even for a seed item with an AI-suggested
    url_candidate on hand, authoritative direct-batch mode must NOT resolve
    that candidate when the batch harvest itself found no match -- an
    AI-suggested URL claims to *be* the correct source page for this specific
    item, and that claim is exactly the unverified/potentially-wrong-link risk
    authoritative mode exists to block (see the Tier-1 URL-fabrication-
    prevention work this test was added for).

    This must stay true regardless of whether the item afterward gets a
    maps-search fallback. A maps-search-query URL is a different risk class:
    it's a deterministic "search this name near this destination" link, not
    an assertion that any particular page is the right one, so it doesn't
    carry the same fabrication risk -- which is why, after the
    attractions-no-link-fallback fix, this real non-ambiguous museum name now
    gets that fallback instead of being left with no link at all. The
    load-bearing assertion here is `ai_candidate_mock.assert_not_called()`;
    the exact fallback URL below only confirms the fallback path taken did
    not go through the AI-candidate resolver.
    """
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._direct_batch_authoritative = True
    discoverer._attraction_source = "direct_link_batch"

    ai = {
        "top_attractions": [
            {
                "name": "Georgia O'Keeffe Museum",
                "type": "attraction",
                "description": "Modern and regional art collections.",
                "url_candidates": ["https://en.wikipedia.org/wiki/Georgia_O%27Keeffe_Museum"],
            }
        ]
    }

    with patch.object(discoverer, "_search_attraction_from_direct_batch", return_value=None):
        with patch.object(
            discoverer,
            "_resolve_ai_candidate_url",
            return_value="https://en.wikipedia.org/wiki/Georgia_O%27Keeffe_Museum",
        ) as ai_candidate_mock:
            discoverer._discover_attractions(
                ai,
                "Santa Fe",
                None,
                "October 18, 2026",
                seed_names=["Georgia O'Keeffe Museum"],
            )

    out = ai["top_attractions"][0]
    # Not the AI-suggested Wikipedia URL, and not empty either -- the safe
    # name+destination maps-search fallback.
    assert out["url"] == (
        "https://www.google.com/maps/search/?api=1&query=Georgia%20O%27Keeffe%20Museum%20Santa%20Fe"
    )
    ai_candidate_mock.assert_not_called()


def test_discover_attractions_direct_batch_authoritative_recovers_via_general_search() -> None:
    """Part 1 regression, grounded in real evidence from
    docs/reports/link_recall_strategy_experiment.md /
    link_recall_strategy_experiment_results.json (2026-08-17, run against a
    live SW2026-dipstick65 output): production shipped "Sunrise Point"
    (Bryce Canyon NP) with no link at all, because authoritative direct-
    batch mode went straight from "no harvested row match" to the maps-
    fallback-or-fail-closed helper without ever trying the general web-
    search path this project's own _build_query_variants /_search_first
    already support elsewhere in this same function (the "For NPS parks"
    section). A live re-run of that exact production-style query in the
    experiment found a real, live, policy-compliant page
    (nps.gov/brca/planyourvisit/sunrise.htm) on the first try. This test
    proves the authoritative-no-match branch now tries that same real,
    independently-verified search before giving up, and uses the result
    when found -- without reopening the AI-candidate fabrication risk the
    sibling tripwire test above guards (no url_candidates are supplied or
    consulted here; the recovered URL comes only from the mocked
    _search_first, matching the real _search_first_strict verification
    pipeline's own specificity/relevance/liveness gates).
    """
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._direct_batch_authoritative = True
    discoverer._attraction_source = "direct_link_batch"

    ai = {
        "top_attractions": [
            {
                "name": "Sunrise Point",
                "type": "viewpoint",
                "description": "Northernmost viewpoint over the Bryce Amphitheater.",
            }
        ]
    }

    def fake_search_first(variants, site_filter=None, **kwargs):
        if site_filter == "nps.gov":
            return "https://www.nps.gov/brca/planyourvisit/sunrise.htm"
        return None

    with patch.object(discoverer, "_search_attraction_from_direct_batch", return_value=None):
        with patch.object(discoverer, "_search_first", side_effect=fake_search_first):
            discoverer._discover_attractions(ai, "Bryce Canyon National Park", "brca")

    attr = ai["top_attractions"][0]
    assert attr.get("url") == "https://www.nps.gov/brca/planyourvisit/sunrise.htm"


def test_discover_attractions_direct_batch_authoritative_recovers_trail_via_general_search() -> None:
    """Part 1 regression (trail-like branch), grounded in the same real
    evidence: production's dipstick65 run shipped "Park Avenue Trail"
    (Arches NP) with no link because authoritative mode's trail-like branch
    exhausted only its AllTrails-specific paths -- and, being authoritative,
    never tried the per-item NPS/web fan-out either (that fan-out helper is
    itself hard-gated off in authoritative mode; see
    _search_attraction_from_item_query_fanout's
    authoritative_direct_batch_lockout) -- before falling straight to
    maps-fallback-or-fail-closed. The live experiment found a real nps.gov
    trailhead page for this exact item on a second query variant
    (alt_phrasing_grok). This test proves the trail-like authoritative-no-
    match branch now also tries one more real, verified general-search
    attempt before giving up on a real link.
    """
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._direct_batch_authoritative = True
    discoverer._alltrails_source = "direct_link_batch"
    discoverer._attraction_source = "direct_link_batch"

    ai = {
        "top_attractions": [
            {
                "name": "Park Avenue Trail",
                "type": "trail",
                "description": "Short trail beneath towering sandstone fins.",
            }
        ]
    }

    def fake_search_first(variants, site_filter=None, **kwargs):
        if site_filter == "nps.gov":
            return "https://www.nps.gov/places/park-avenue-viewpoint-and-trailhead.htm"
        return None

    with patch.object(discoverer, "_search_attraction_from_direct_batch", return_value=None):
        with patch.object(discoverer, "_search_first", side_effect=fake_search_first):
            discoverer._discover_attractions(ai, "Arches National Park", "arch")

    attr = ai["top_attractions"][0]
    assert attr.get("url") == "https://www.nps.gov/places/park-avenue-viewpoint-and-trailhead.htm"


def test_search_attraction_from_maps_area_pool_selects_item_specific_maps_candidate():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)

    rows = [
        {
            "url": "https://www.google.com/maps/search/?api=1&query=attractions+near+St+George+Utah",
            "title": "Top attractions in St. George",
            "snippet": "Snow Canyon State Park maps entry: https://www.google.com/maps/search/?api=1&query=Snow+Canyon+State+Park+Utah",
        }
    ]

    with patch.object(discoverer, "_get_attraction_maps_area_rows_for_destination", return_value=rows):
        with patch.object(discoverer, "_retain_discovered_url", side_effect=lambda url, *_args, **_kwargs: url):
            out = discoverer._search_attraction_from_maps_area_pool("Snow Canyon State Park", "St. George, Utah")

    assert out == "https://www.google.com/maps/search/?api=1&query=Snow+Canyon+State+Park+Utah"


def test_search_restaurant_direct_batch_authoritative_uses_maps_link_from_snippet_text():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._direct_batch_authoritative = True

    rows = [
        {
            "url": "https://www.tripadvisor.com/Restaurants-g60771-Zion_National_Park_Utah.html",
            "title": "Top restaurants around Zion",
            "snippet": "Spotted Dog Cafe appears here. Google Maps: https://www.google.com/maps/search/?api=1&query=Spotted+Dog+Cafe+Springdale+UT",
        }
    ]

    with patch.object(discoverer, "_get_restaurant_direct_batch_rows_for_destination", return_value=rows):
        out = discoverer._search_restaurant_from_direct_batch(
            "Spotted Dog Cafe",
            "Zion National Park",
            "October 18, 2026",
        )

    assert out is None


def test_search_restaurant_direct_batch_authoritative_keeps_tripadvisor_match_for_benja_thai():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._direct_batch_authoritative = True

    rows = [
        {
            "name": "Benja Thai & Sushi",
            "url": "https://www.tripadvisor.com/Restaurant_Review-g28964-d1234567-Reviews-Benja_Thai_Sushi-St_George_Utah.html",
            "snippet": "Benja Thai & Sushi Source Maps Links: https://www.tripadvisor.com/Restaurant_Review-g28964-d1234567-Reviews-Benja_Thai_Sushi-St_George_Utah.html https://www.google.com/maps/search/?api=1&query=Benja+Thai+Sushi+St+George+UT",
        }
    ]

    with patch.object(discoverer, "_get_restaurant_direct_batch_rows_for_destination", return_value=rows):
        with patch.object(discoverer, "_fetch_page_text", return_value=(False, 403, "")):
            out = discoverer._search_restaurant_from_direct_batch(
                "Benja Thai & Sushi",
                "St. George, Utah",
                "October 17, 2026",
            )

    assert out == "https://www.tripadvisor.com/Restaurant_Review-g28964-d1234567-Reviews-Benja_Thai_Sushi-St_George_Utah.html"


def test_direct_batch_rows_from_html_prefers_source_over_maps_url():
    html = (
        "<h2>St. George, Utah</h2>"
        "<ul>"
        "<li>Painted Pony <a href=\"https://paintedponyrestaurant.com/\">Source</a> "
        "<a href=\"https://www.google.com/maps/search/?api=1&query=Painted+Pony+St+George\">Maps</a></li>"
        "</ul>"
    )

    rows = URLDiscoverer._direct_batch_rows_from_html(html)
    assert rows
    assert rows[0]["title"] == "Painted Pony"
    # Source link must win over the Maps search URL
    assert rows[0]["url"] == "https://paintedponyrestaurant.com/"
    # Maps link is preserved as fallback metadata
    assert rows[0]["maps_url"].startswith("https://www.google.com/maps/search/")
    assert "paintedponyrestaurant.com" in rows[0]["snippet"]


def test_direct_batch_rows_from_html_infers_restaurant_metadata_from_text() -> None:
    html = (
        "<h2>St. George</h2>"
        "<ul>"
        "<li>Wood Ash Rye - $$ upscale American plates and cocktails "
        "<a href=\"https://www.google.com/maps/search/?api=1&query=Wood+Ash+Rye+St+George\">Maps</a></li>"
        "</ul>"
    )

    rows = URLDiscoverer._direct_batch_rows_from_html(html)
    assert rows
    assert rows[0].get("price_range", "") == "$$"
    assert rows[0].get("cuisine", "") == "American"


def test_direct_batch_rows_from_html_infers_restaurant_cuisine_from_maps_query_last_resort() -> None:
    html = (
        "<h2>St. George</h2>"
        "<ul>"
        "<li>Sakura House "
        "<a href=\"https://www.google.com/maps/search/?api=1&query=Sakura+House+Sushi+St+George\">Maps</a></li>"
        "</ul>"
    )

    rows = URLDiscoverer._direct_batch_rows_from_html(html)
    assert rows
    assert rows[0].get("cuisine", "") == "Japanese"


def test_direct_batch_rows_from_html_extracts_en_route_detour_metadata_and_note():
    html = (
        "<h2>Moab</h2>"
        "<ul>"
        "<li>Wilson Arch - quick roadside arch stop - detour 3 mi / 8 min "
        "<a href=\"https://www.blm.gov/visit/wilson-arch\">Source</a> "
        "<a href=\"https://www.google.com/maps/search/?api=1&query=Wilson+Arch+Moab+UT\">Maps</a></li>"
        "</ul>"
    )

    rows = URLDiscoverer._direct_batch_rows_from_html(html)
    assert rows
    # Source link wins; Maps search link is held as maps_url fallback
    assert rows[0]["url"] == "https://www.blm.gov/visit/wilson-arch"
    assert rows[0]["maps_url"].startswith("https://www.google.com/maps/search/")
    assert rows[0]["detour_distance_miles"] == 3.0
    assert rows[0]["detour_time_minutes"] == 8
    assert rows[0]["practical_note"] == "quick roadside arch stop"


def test_direct_batch_rows_from_html_extracts_attraction_note_without_corrupting_name():
    """Regression for dipstick55 Theme D: 'Sunset Point' and 'Natural Bridge'
    (real Bryce Canyon attraction rows) rendered with no teaser at all,
    because the direct-batch attraction/trail prompts never asked for one
    (unlike the en-route-stop prompt, which does and works correctly). Once
    the attraction/trail prompts are extended to request a short note after
    the rating, the anchor links land *between* the name and the rating
    (matching the real captured HTML: "Name <a>Source</a> <a>Maps</a>
    4.8/5"), unlike en-route stops where links trail at the very end -- a
    naive trailing-only strip of the Source/Maps anchor-label words left
    them glued onto the extracted name once real content (rating, then a
    dash-separated note) followed them. This must extract a clean name and
    the note as a separate field, matching the real reported item names."""
    html = (
        "<h2>Bryce Canyon National Park</h2>"
        "<ul>"
        '<li>Sunset Point <a href="https://www.nps.gov/brca/planyourvisit/sunset.htm">Source</a> '
        '<a href="https://www.google.com/maps/search/?api=1&query=Sunset+Point+Bryce+Canyon+UT">Maps</a> '
        "4.8/5 - Iconic canyon overlook with sweeping hoodoo views at sunset.</li>"
        "</ul>"
    )

    rows = URLDiscoverer._direct_batch_rows_from_html(html)
    assert rows
    row = rows[0]
    assert row["name"] == "Sunset Point"
    assert row["title"] == "Sunset Point"
    assert row["url"] == "https://www.nps.gov/brca/planyourvisit/sunset.htm"
    assert row["raw_rating"] == "4.8/5"
    assert row["rating"] == 4.8
    assert row["practical_note"] == "Iconic canyon overlook with sweeping hoodoo views at sunset."
    assert "Source" not in row["name"]
    assert "Maps" not in row["name"]


def test_direct_batch_rows_from_html_extracts_trail_note_without_corrupting_name():
    """Same regression as the attraction case above, for the trail (AllTrails)
    harvest prompt -- e.g. the real reported 'Chuckwalla Trail' with no
    teaser."""
    html = (
        "<h2>Moab</h2>"
        "<ul>"
        '<li>Chuckwalla Trail <a href="https://www.alltrails.com/trail/us/utah/chuckwalla-trail">AllTrails</a> '
        "4.6/5 1.7 mi - Rolling desert singletrack through cactus and slickrock.</li>"
        "</ul>"
    )

    rows = URLDiscoverer._direct_batch_rows_from_html(html)
    assert rows
    row = rows[0]
    assert row["name"] == "Chuckwalla Trail"
    assert row["url"] == "https://www.alltrails.com/trail/us/utah/chuckwalla-trail"
    assert row["raw_rating"] == "4.6/5"
    assert row["practical_note"] == "Rolling desert singletrack through cactus and slickrock."


def test_direct_batch_rows_from_html_extracts_note_with_no_dash_separator():
    """Confirmed live against the real xAI Grok API (grok-4-fast) after the
    attraction/trail prompts were extended to ask for a note: the model does
    not reliably use a dash before the note (observed real output: 'Bryce
    Canyon Visitor Center <a>Source</a> <a>Maps</a> 4.4/5 Interactive
    exhibits and award-winning film introduce park geology and history.' --
    a space, not a dash, separates the rating from the note). The dash-split
    alone would leave detail_text as the whole name+rating+note blob in this
    shape; the name/rating/distance-boundary cursor must still isolate a
    clean note."""
    html = (
        "<h2>Bryce Canyon National Park</h2>"
        "<ul>"
        '<li>Bryce Canyon Visitor Center <a href="https://www.nps.gov/brca/planyourvisit/tourvisitor.htm">Source</a> '
        '<a href="https://www.google.com/maps/search/?api=1&amp;query=x">Maps</a> 4.4/5 '
        "Interactive exhibits and award-winning film introduce park geology and history.</li>"
        "</ul>"
    )

    rows = URLDiscoverer._direct_batch_rows_from_html(html)
    assert rows
    row = rows[0]
    assert row["name"] == "Bryce Canyon Visitor Center"
    assert row["raw_rating"] == "4.4/5"
    assert row["practical_note"] == "Interactive exhibits and award-winning film introduce park geology and history."


def test_direct_batch_rows_from_html_extracts_trail_note_with_distance_and_no_dash():
    """Same no-dash regression, for a trail row with a distance-in-miles
    token between the rating and the note (confirmed live: 'Corona and
    Bowtie Arch via Corona Arch Trail <a>AllTrails</a> 4.9/5 2.4 mi Iconic
    arches reached via scenic slickrock route.')."""
    html = (
        "<h2>Moab</h2>"
        "<ul>"
        '<li>Corona and Bowtie Arch via Corona Arch Trail '
        '<a href="https://www.alltrails.com/trail/us/utah/corona-and-bowtie-arch-trail">AllTrails</a> '
        "4.9/5 2.4 mi Iconic arches reached via scenic slickrock route.</li>"
        "</ul>"
    )

    rows = URLDiscoverer._direct_batch_rows_from_html(html)
    assert rows
    row = rows[0]
    assert row["name"] == "Corona and Bowtie Arch via Corona Arch Trail"
    assert row["raw_rating"] == "4.9/5"
    assert row["practical_note"] == "Iconic arches reached via scenic slickrock route."


def test_direct_batch_rows_from_html_attraction_without_note_still_parses_cleanly():
    """The un-modified real-world format (no trailing note, as harvested
    before this fix) must keep working exactly as before."""
    html = (
        "<h2>Bryce Canyon National Park</h2>"
        "<ul>"
        '<li>Sunset Point <a href="https://www.nps.gov/brca/planyourvisit/sunset.htm">Source</a> '
        '<a href="https://www.google.com/maps/search/?api=1&query=Sunset+Point+Bryce+Canyon+UT">Maps</a> 4.8/5</li>'
        "</ul>"
    )

    rows = URLDiscoverer._direct_batch_rows_from_html(html)
    assert rows
    assert rows[0]["name"] == "Sunset Point"
    assert rows[0]["raw_rating"] == "4.8/5"


def test_direct_batch_rows_from_html_strips_google_maps_name_prefix():
    html = (
        "<h2>Santa Fe</h2>"
        "<ul>"
        "<li>Google Maps: La Bajada Overlook and Scenic Pullouts "
        "<a href=\"https://maps.google.com/maps?q=La+Bajada+Hill+Overlook+Santa+Fe\">Maps</a></li>"
        "</ul>"
    )

    rows = URLDiscoverer._direct_batch_rows_from_html(html)
    assert rows
    assert rows[0]["title"] == "La Bajada Overlook and Scenic Pullouts"


def test_direct_batch_rows_from_html_strips_maps_name_prefix():
    html = (
        "<h2>Santa Fe</h2>"
        "<ul>"
        "<li>Maps: Turquoise Trail Scenic Byway Route "
        "<a href=\"https://www.turquoisetrail.org/\">Source</a></li>"
        "</ul>"
    )

    rows = URLDiscoverer._direct_batch_rows_from_html(html)
    assert rows
    assert rows[0]["title"] == "Turquoise Trail Scenic Byway Route"


def test_direct_batch_rows_from_html_strips_source_maps_before_rating_price_cuisine_tail() -> None:
    """Real harvested restaurant rows are shaped 'Name - <a>Source</a> <a>Maps</a>
    RATING PRICE CUISINE' (rating/price/cuisine come AFTER the links, not before).
    The old trailing-only Source/Maps strip only handles those words when they're
    the last tokens in the string, so it never fires here and 'Source Maps' leaks
    into the description, which then either renders a garbled teaser or gets
    inconsistently suppressed depending on unrelated AI-description backfill.
    Since there is no real prose in this shape at all (only metadata), the
    description must end up empty and consistent across rows, not junk text."""
    html = (
        "<h2>St. George Restaurants</h2>"
        "<ul>"
        "<li>Painted Pony - "
        "<a href=\"https://www.tripadvisor.com/Restaurant_Review-g28964-d1-Painted_Pony.html\">Source</a> "
        "<a href=\"https://www.google.com/maps/search/?api=1&query=Painted+Pony+St+George+UT\">Maps</a> "
        "4.6/5 $$$ American</li>"
        "<li>Thai Chili - "
        "<a href=\"https://www.tripadvisor.com/Restaurant_Review-g28964-d2-Thai_Chili.html\">Source</a> "
        "<a href=\"https://www.google.com/maps/search/?api=1&query=Thai+Chili+St+George+UT\">Maps</a> "
        "4.5/5 $$ Thai</li>"
        "</ul>"
    )

    rows = URLDiscoverer._direct_batch_rows_from_html(html)
    assert len(rows) == 2
    for row in rows:
        assert "source" not in row["description"].lower()
        assert "maps" not in row["description"].lower()
        assert row["description"] == ""
        assert row["practical_note"] == ""
    assert rows[0]["rating"] == 4.6
    assert rows[0]["price_range"] == "$$$"
    assert rows[0]["cuisine"] == "American"


def test_direct_batch_rows_from_html_recovers_markdown_bullet_list_fallback() -> None:
    """Regression for the Moab -> Canyonlands en-route-stops anomaly: a real
    dipstick63 harvest for that exact leg returned Grok's response as a
    Markdown "- Name - detail <a href=...>Source</a> <a href=...>Maps</a>"
    bullet list instead of the requested <ul><li> markup. The old parser only
    recognized <li> elements, so it silently produced zero rows even though
    the model proposed 8 real, well-formed candidates -- including "Dead
    Horse Point State Park Overlook", the obvious real stop on that route --
    which meant Moab->Canyonlands rendered with zero en-route stops while the
    identical prompt against the identical leg on other runs (returned as
    proper <li> markup) parsed fine. This is the actual harvested text
    (trimmed to 3 of the 8 real items) that must now parse successfully."""
    html = (
        "<div class=\"direct_batch_query\"><strong>Query:</strong> "
        "Generate en-route stopovers for the drive from Moab to Canyonlands "
        "National Park (October 24, 2026)</div>\n\n"
        "**En-Route Stopovers: Moab to Canyonlands National Park (Island in the Sky)**\n\n"
        "- Moab Giants Dinosaur Museum - life-size dinos & tracks along the route - "
        "detour 0 mi / 0 min <a href=\"https://moabgiants.com/\">Source</a> "
        "<a href=\"https://www.google.com/maps/search/?api=1&query=Moab+Giants+Dinosaur+Museum+Moab+UT\">Maps</a>\n"
        "- Gemini Bridges Trailhead View - dramatic twin arches overlook - detour 3 mi / 8 min "
        "<a href=\"https://www.discovermoab.com/places-to-go/scenic-byways/scenic-byway-u-313/\">Source</a> "
        "<a href=\"https://www.google.com/maps/search/?api=1&query=Gemini+Bridges+Trailhead+UT-313+Moab+UT\">Maps</a>\n"
        "- Dead Horse Point State Park Overlook - sweeping canyon panorama - detour 5 mi / 12 min "
        "<a href=\"https://www.discovermoab.com/places-to-go/state-parks/dead-horse-point-state-park/\">Source</a> "
        "<a href=\"https://www.google.com/maps/search/?api=1&query=Dead+Horse+Point+State+Park+Overlook+Moab+UT\">Maps</a>\n"
    )

    rows = URLDiscoverer._direct_batch_rows_from_html(html)
    assert len(rows) == 3
    names = [row["title"] for row in rows]
    assert "Dead Horse Point State Park Overlook" in names
    dead_horse = next(row for row in rows if row["title"] == "Dead Horse Point State Park Overlook")
    assert dead_horse["url"] == "https://www.discovermoab.com/places-to-go/state-parks/dead-horse-point-state-park/"
    assert dead_horse["detour_distance_miles"] == 5.0
    assert dead_horse["detour_time_minutes"] == 12
    assert dead_horse["practical_note"] == "sweeping canyon panorama"


def test_direct_batch_rows_from_html_markdown_bullet_fallback_only_used_when_no_li_found() -> None:
    """The Markdown-bullet fallback must never fire when real <li> markup is
    present -- it's a last-resort recovery for a fully <li>-less reply, not a
    replacement for the primary parser. A stray leading '-' inside a normal
    <li>-based list (e.g. a hyphenated name) must not get double-counted."""
    html = (
        "<ul>"
        "<li>Wilson Arch - quick roadside arch stop "
        "<a href=\"https://www.blm.gov/visit/wilson-arch\">Source</a></li>"
        "</ul>"
        "\n- This line looks like a bullet but must be ignored because real <li> rows were already found."
    )

    rows = URLDiscoverer._direct_batch_rows_from_html(html)
    assert len(rows) == 1
    assert rows[0]["title"] == "Wilson Arch"


def test_direct_batch_rows_from_html_no_separator_name_metadata_yields_empty_description() -> None:
    """Some harvested rows have no ' - ' separator at all between the name and
    its links (e.g. 'Name <a>Source</a> <a>Maps</a> RATING PRICE CUISINE').
    Without a separator, detail_text never gets split from the name, so the
    name's own words previously inflated the 'is there real content' check and
    let 'Name RATING PRICE CUISINE' leak through as a fake teaser. It must
    still collapse to an empty, consistent description like the separator case."""
    html = (
        "<h2>Zion Restaurants</h2>"
        "<ul>"
        "<li>Zion Pizza &amp; Noodle Co. "
        "<a href=\"https://www.tripadvisor.com/Restaurant_Review-g29115-d1-Zion_Pizza.html\">Source</a> "
        "<a href=\"https://www.google.com/maps/search/?api=1&query=Zion+Pizza+Springdale+UT\">Maps</a> "
        "4.4/5 $ Italian</li>"
        "<li>Oscar's Cafe "
        "<a href=\"https://www.tripadvisor.com/Restaurant_Review-g29115-d2-Oscars.html\">Source</a> "
        "<a href=\"https://www.google.com/maps/search/?api=1&query=Oscars+Cafe+Springdale+UT\">Maps</a> "
        "4.5/5 $$ Mexican-American</li>"
        "</ul>"
    )

    rows = URLDiscoverer._direct_batch_rows_from_html(html)
    assert len(rows) == 2
    for row in rows:
        assert row["description"] == ""
        assert row["practical_note"] == ""
    assert rows[0]["name"] == "Zion Pizza & Noodle Co."
    assert rows[0]["rating"] == 4.4
    assert rows[0]["price_range"] == "$"
    assert rows[1]["name"] == "Oscar's Cafe"
    assert rows[1]["rating"] == 4.5


def test_direct_batch_rows_from_html_sanitizes_source_maps_description_noise():
    html = (
        "<h2>Moab</h2>"
        "<ul>"
        "<li>Desert Bistro - cozy patio and local ingredients Source Maps "
        "<a href=\"https://desertbistro.com/\">Source</a> "
        "<a href=\"https://www.google.com/maps/search/?api=1&query=Desert+Bistro+Moab\">Maps</a></li>"
        "</ul>"
    )

    rows = URLDiscoverer._direct_batch_rows_from_html(html)
    assert rows
    assert rows[0]["practical_note"] == "cozy patio and local ingredients"
    assert "source" not in rows[0]["description"].lower()
    assert "maps" not in rows[0]["description"].lower()


def test_build_primary_items_from_direct_batch_carries_restaurant_metadata_fields() -> None:
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    rows = [
        {
            "name": "Painted Pony",
            "url": "https://paintedponyrestaurant.com/",
            "cuisine": "Southwestern",
            "price_range": "$$$",
            "reserve_recommended": True,
            "description": "Chef-driven Southwestern plates.",
        }
    ]

    merged = discoverer._build_primary_items_from_direct_batch(
        rows=rows,
        existing_items=[],
        target_count=1,
        fallback_description="Locally surfaced dinner option.",
    )

    assert merged
    assert merged[0]["cuisine"] == "Southwestern"
    assert merged[0]["price_range"] == "$$$"
    assert merged[0]["reserve_recommended"] is True
    assert merged[0]["description"] == "Chef-driven Southwestern plates."


def test_build_primary_items_from_direct_batch_carries_rating_fields() -> None:
    """Rating info harvested onto a direct-batch row (via
    _infer_direct_batch_quality_metadata) must survive the merge into the final
    restaurant dict, or it never reaches the html_assembler badge and the only
    place a rating can appear is baked into the name text."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    rows = [
        {
            "name": "Cafe Soleil",
            "url": "https://cafesoleilzion.com/",
            "cuisine": "Cafe",
            "price_range": "$$",
            "description": "Fresh market-driven breakfast and brunch.",
            "rating": 4.7,
            "raw_rating": "4.7/5",
            "votes": 230,
        }
    ]

    merged = discoverer._build_primary_items_from_direct_batch(
        rows=rows,
        existing_items=[],
        target_count=1,
        fallback_description="Locally surfaced dinner option.",
    )

    assert merged
    assert merged[0].get("rating") == 4.7


def test_direct_batch_rows_from_html_recognizes_bistro_as_cuisine() -> None:
    """Dipstick58 bug 2: a harvested row like "Book Club Bistro 4.9/5 $$
    Bistro" trails the word "Bistro" as its cuisine token (matching the
    format used for every other restaurant row), but "bistro" was missing
    from `_infer_restaurant_metadata_from_text_and_url`'s cuisine keyword
    map, so the cuisine field stayed empty and the rendered card lost its
    cuisine badge entirely even though the harvest source did supply one."""
    html = (
        "<li>Book Club Bistro 4.9/5 $$ Bistro "
        '<a href="https://www.opentable.com/r/book-club-bistro-saint-george">Source</a> '
        '<a href="https://www.google.com/maps/search/?api=1&query=Book+Club+Bistro+St.+George+UT">Maps</a></li>'
    )
    rows = URLDiscoverer._direct_batch_rows_from_html(html)
    assert rows
    assert rows[0]["name"] == "Book Club Bistro"
    assert rows[0]["cuisine"] == "Bistro"


def test_direct_batch_rows_from_html_recognizes_poke_pies_cafe_as_cuisine() -> None:
    """Dipstick60: real validation run + project owner manual review found
    "Wood Ash Rye", "Hawaiian Poke Bowl", "Croshaw's Gourmet Pies Inc", and
    "MeMe's Cafe" (all St. George/Zion, all NEW discoveries surfaced as a
    side effect of the same-night restaurant-description-quality fix)
    rendering with no cuisine badge. The real captured harvest HTML (St.
    George / Zion direct-batch restaurant captures) showed three of the four
    genuinely had a cuisine signal that
    `_infer_restaurant_metadata_from_text_and_url`'s keyword map just didn't
    recognize yet: "poke bowls" (Hawaiian Poke Bowl), "pies" (Croshaw's
    Gourmet Pies Inc., both in its name and its description "handmade pies
    and baked goods"), and "cafe" (MeMe's Cafe, both in its name and its
    description "Cozy cafe known for..."). The fourth, Wood Ash Rye, is a
    separate harvest-prompt-gap bug -- see
    test_restaurant_direct_batch_prompts_request_cuisine_field below; its
    real captured text has zero cuisine-indicating word anywhere, so no
    keyword-map addition could ever recover it."""
    html = (
        '<li>Hawaiian Poke Bowl <a href="https://www.tripadvisor.com/Restaurant_Review-g57119-d4226440-Reviews-Hawaiian_Poke_Bowl-St_George_Utah.html">Source</a> '
        '<a href="https://www.google.com/maps/search/?api=1&query=Hawaiian+Poke+Bowl+St+George+UT">Maps</a> 4.8/5 $ Fresh, customizable poke bowls in a casual setting.</li>'
        "<li>Croshaw's Gourmet Pies Inc. "
        '<a href="https://www.tripadvisor.com/Restaurant_Review-g57119-d420070-Reviews-Croshaw_s_Gourmet_Pies_Inc-St_George_Utah.html">Source</a> '
        "<a href=\"https://www.google.com/maps/search/?api=1&query=Croshaw's+Gourmet+Pies+Inc+St+George+UT\">Maps</a> 4.6/5 $ Exceptional handmade pies and baked goods with flaky crusts.</li>"
        "<li>MeMe's Cafe "
        '<a href="https://www.tripadvisor.com/Restaurant_Review-g61001-d10086610-Reviews-MeMe_s_Cafe-Springdale_Utah.html">Source</a> '
        "<a href=\"https://www.google.com/maps/search/?api=1&query=MeMe's+Cafe+Springdale+UT\">Maps</a> 4.5/5 $$ Cozy cafe known for inventive breakfast crepes and fresh local salads.</li>"
    )
    rows = URLDiscoverer._direct_batch_rows_from_html(html)
    by_name = {row["name"]: row for row in rows}
    assert by_name["Hawaiian Poke Bowl"]["cuisine"] == "Hawaiian"
    assert by_name["Croshaw's Gourmet Pies Inc."]["cuisine"] == "Bakery"
    assert by_name["MeMe's Cafe"]["cuisine"] == "Cafe"


def test_memes_cafe_direct_batch_html_renders_clean_name_cuisine_badge() -> None:
    """Full-pipeline regression for dipstick60: the real captured Zion
    direct-batch restaurant HTML row for "MeMe's Cafe" must render with its
    full name intact and a populated "Cafe" cuisine badge. This is exactly
    the "Wild Rabbit Cafe" scenario _sanitize_restaurant_display_name's own
    docstring already calls out as needing protection -- a clean harvested
    name whose own last word happens to equal the cuisine badge value must
    not be truncated just because there's no glued-on rating/price
    decoration to signal a truncation boundary."""
    from generator.html_assembler import HTMLAssembler

    html = (
        "<li>MeMe's Cafe "
        '<a href="https://www.tripadvisor.com/Restaurant_Review-g61001-d10086610-Reviews-MeMe_s_Cafe-Springdale_Utah.html">Source</a> '
        "<a href=\"https://www.google.com/maps/search/?api=1&query=MeMe's+Cafe+Springdale+UT\">Maps</a> 4.5/5 $$ Cozy cafe known for inventive breakfast crepes and fresh local salads.</li>"
    )
    rows = URLDiscoverer._direct_batch_rows_from_html(html)
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    merged = discoverer._build_primary_items_from_direct_batch(
        rows=rows,
        existing_items=[],
        target_count=1,
        fallback_description="Locally surfaced dinner option.",
    )

    assembler = HTMLAssembler.__new__(HTMLAssembler)
    out = assembler._build_restaurants({"dinner_recommendations": merged}, "Zion National Park")

    assert "MeMe&#x27;s Cafe" in out
    assert '<span class="badge cuisine-badge">Cafe</span>' in out


def test_restaurant_direct_batch_prompts_request_cuisine_field() -> None:
    """Dipstick60 harvest-prompt-gap root cause for "Wood Ash Rye": the
    dipstick59 fix (commit 0600cd5) added "then a short descriptive note
    ... -- real prose, not just a repeat of the cuisine or price" to the
    restaurant harvest prompt, to stop cuisine words leaking verbatim into
    descriptions. Side effect: it stopped asking the model for a cuisine
    field at all. Real captured harvest HTML proves this directly for the
    same restaurant, before and after: the pre-fix dipstick55 capture reads
    "Wood Ash Rye 4.5/5 $$$ New American ..." (an explicit cuisine field),
    while the post-fix dipstick60 capture for the identical restaurant reads
    "Wood Ash Rye ... 4.7/5 $$$ Refined seasonal dishes in elegant hotel
    setting with craft cocktails." -- zero cuisine-indicating word anywhere.
    No keyword-map addition can recover a cuisine that was never harvested,
    so both the single- and multi-destination restaurant prompts must
    explicitly ask for the cuisine/restaurant type again, alongside (not
    instead of) the descriptive note."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._restaurant_direct_batch_item_count = 6

    single = discoverer._direct_batch_html_prompt(kind="restaurant", dest_name="Moab", dates="October 18, 2026")
    assert single is not None
    single_system, single_user = single
    assert "cuisine or restaurant type" in single_system
    assert "cuisine or restaurant type" in single_user

    multi = discoverer._direct_batch_html_prompt_multi(
        kind="restaurant",
        destinations=[("Moab", "October 18, 2026"), ("Zion National Park", "October 19, 2026")],
    )
    assert multi is not None
    multi_system, multi_user = multi
    assert "cuisine or restaurant type" in multi_system
    assert "cuisine or restaurant type" in multi_user


def test_build_primary_items_from_direct_batch_does_not_synthesize_description_from_name_and_metadata() -> None:
    """Dipstick58 bug 1: when a harvested row has no real description/
    practical_note (just the item's own name plus rating/price/cuisine
    metadata -- e.g. "Book Club Bistro 4.9/5 $$ Bistro"), the row-merge
    logic used to fall back to the row's raw "snippet" field as the
    description. That snippet is just the name plus the same metadata
    already rendered as badges, so once html_assembler's rating/price
    stripping ran on it, "Book Club Bistro 4.9/5 $$ Bistro" collapsed into
    "Book Club Bistro Bistro" -- the cuisine word duplicated onto the end
    of the name. The merge must recognize that snippet as metadata-only and
    fall back to the generic fallback_description instead."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    rows = [
        {
            "name": "Book Club Bistro",
            "title": "Book Club Bistro",
            "url": "https://www.opentable.com/r/book-club-bistro-saint-george",
            "snippet": (
                "Book Club Bistro 4.9/5 $$ Bistro Source Maps "
                "Links: https://www.opentable.com/r/book-club-bistro-saint-george "
                "https://www.google.com/maps/search/?api=1&query=Book+Club+Bistro+St.+George+UT"
            ),
            "description": "",
            "practical_note": "",
            "cuisine": "Bistro",
            "price_range": "$$",
            "rating": 4.9,
            "raw_rating": "4.9/5",
        }
    ]

    merged = discoverer._build_primary_items_from_direct_batch(
        rows=rows,
        existing_items=[],
        target_count=1,
        fallback_description="Locally surfaced dinner option.",
    )

    assert merged
    assert merged[0]["description"] == "Locally surfaced dinner option."
    assert "Bistro" not in merged[0]["description"]


def test_build_primary_items_from_direct_batch_replaces_stale_hallucinated_description_with_harvest_text() -> None:
    """Real dipstick62 bug: two real en-route-stop items rendered with
    descriptions that named the wrong place entirely.
    "Little Wild Horse Canyon Trailhead" (a real slot-canyon trailhead near
    Goblin Valley, UT) rendered with "Visit this park for sweeping views of
    the Colorado River and a dramatic canyon overlook..." -- Little Wild
    Horse Canyon is nowhere near the Colorado River. "Wedge Overlook (San
    Rafael Swell)" rendered describing "Castleton Tower", a real but
    ~100-mile-distant, unrelated Moab-area landmark.

    Root cause, confirmed against the real captured harvest HTML
    (dev/dev/url_discovery_direct_batch_html/moab.en-route-stop...html from
    that run): the actual harvested rows for both items carry short, correct
    descriptions ("slot canyon hiking access", "dramatic canyon rim views").
    The merge already trusted the row unconditionally for every other field
    (rating, votes, practical_note -- populated from this exact same
    underlying text) but kept whatever (unverified, pre-harvest) description
    an existing item already had, so the hallucinated text survived in
    `description` while the correct harvested text landed in
    `practical_note` -- both were rendered side by side. The merge must
    prefer the harvested description here too, exactly like it already does
    for practical_note."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    rows = [
        {
            "name": "Wedge Overlook (San Rafael Swell)",
            "title": "Wedge Overlook (San Rafael Swell)",
            "url": "https://www.blm.gov/visit/wedge-overlook",
            "maps_url": "https://www.google.com/maps/search/?api=1&query=Wedge+Overlook+San+Rafael+Swell+Castle+Dale+UT",
            "description": "dramatic canyon rim views",
            "practical_note": "dramatic canyon rim views",
            "detour_distance_miles": 12.0,
            "detour_time_minutes": 18,
        },
        {
            "name": "Little Wild Horse Canyon Trailhead",
            "title": "Little Wild Horse Canyon Trailhead",
            "url": "https://www.blm.gov/visit/little-wild-horse-canyon",
            "maps_url": "https://www.google.com/maps/search/?api=1&query=Little+Wild+Horse+Canyon+Trailhead+Hanksville+UT",
            "description": "slot canyon hiking access",
            "practical_note": "slot canyon hiking access",
            "detour_distance_miles": 5.0,
            "detour_time_minutes": 10,
        },
    ]
    existing = [
        {
            "name": "Little Wild Horse Canyon Trailhead",
            "description": (
                "Visit this park for sweeping views of the Colorado River and a "
                "dramatic canyon overlook. It's a great location for photography."
            ),
        },
        {
            "name": "Wedge Overlook (San Rafael Swell)",
            "description": "A short pull-off offers views of the Castleton Tower and the surrounding valley.",
        },
    ]

    merged = discoverer._build_primary_items_from_direct_batch(
        rows=rows,
        existing_items=existing,
        target_count=2,
        fallback_description="Optional stop for the inbound transfer leg.",
        dest_name="Moab",
    )

    by_name = {item["name"]: item for item in merged}
    assert by_name["Little Wild Horse Canyon Trailhead"]["description"] == "slot canyon hiking access"
    assert "Colorado River" not in by_name["Little Wild Horse Canyon Trailhead"]["description"]
    assert by_name["Wedge Overlook (San Rafael Swell)"]["description"] == "dramatic canyon rim views"
    assert "Castleton Tower" not in by_name["Wedge Overlook (San Rafael Swell)"]["description"]


def test_book_club_bistro_direct_batch_html_renders_clean_name_cuisine_badge_no_duplicate_description() -> None:
    """Full-pipeline regression for dipstick58: the real captured St. George
    direct-batch restaurant HTML row for "Book Club Bistro" must render with
    its full name intact, a populated cuisine badge, and no duplicated
    "Bistro Bistro"-style description text."""
    from generator.html_assembler import HTMLAssembler

    html = (
        "<li>Book Club Bistro 4.9/5 $$ Bistro "
        '<a href="https://www.opentable.com/r/book-club-bistro-saint-george">Source</a> '
        '<a href="https://www.google.com/maps/search/?api=1&query=Book+Club+Bistro+St.+George+UT">Maps</a></li>'
    )
    rows = URLDiscoverer._direct_batch_rows_from_html(html)
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    merged = discoverer._build_primary_items_from_direct_batch(
        rows=rows,
        existing_items=[],
        target_count=1,
        fallback_description="Locally surfaced dinner option.",
    )

    assembler = HTMLAssembler.__new__(HTMLAssembler)
    out = assembler._build_restaurants({"dinner_recommendations": merged}, "St. George")

    assert ">Book Club Bistro<" in out
    assert '<span class="badge cuisine-badge">Bistro</span>' in out
    assert "Book Club Bistro Bistro" not in out
    assert ">Book Club<" not in out


def test_build_primary_items_from_direct_batch_rejects_generic_listing_row() -> None:
    """Reproduces the Dipstick48 bug: a harvested row whose only "name" is a
    TripAdvisor/Yelp listicle title (and whose url is the listing page itself)
    must never be synthesized into a restaurant/attraction item -- otherwise
    the rendered card's name literally reads "THE 10 BEST Restaurants in
    St. George - Tripadvisor" instead of an actual restaurant name."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    rows = [
        {
            "title": "THE 10 BEST Restaurants in St. George - Tripadvisor",
            "url": "https://www.tripadvisor.com/Restaurants-g60852-St_George_Utah.html",
            "description": "Find the best restaurants in St. George.",
        },
        {
            "name": "THE 15 BEST Things to Do in Moab 2024 (with Photos) - Tripadvisor",
            "url": "https://www.tripadvisor.com/Attractions-g60724-Activities-Moab_Utah.html",
        },
        {
            "name": "Best Restaurants Near Zion National Park - TripAdvisor",
            "url": "https://www.tripadvisor.com/RestaurantsNear-g143057-d143021-Zion_National_Park_Utah.html",
        },
        {
            "name": "Painted Pony",
            "url": "https://paintedponyrestaurant.com/",
            "description": "Chef-driven Southwestern plates.",
        },
    ]

    merged = discoverer._build_primary_items_from_direct_batch(
        rows=rows,
        existing_items=[],
        target_count=4,
        fallback_description="Locally surfaced dinner option.",
    )

    names = [item.get("name", "") for item in merged]
    assert names == ["Painted Pony"]


def test_is_metadata_only_residual_text_catches_review_volume_and_cuisine_junk():
    """Root-cause fix for the dipstick59 restaurant-description completeness
    regression: manual review of a real run (dipstick59) found 38 of 62
    restaurants rendering with literally no description, and a further 8
    rendering with decorative junk text that isn't a real description either
    (e.g. "(high volume), American." or "(Contemporary American)"). Both
    classes trace back to _is_metadata_only_residual_text under-counting
    words like "American", "fine", "dining", or "volume" as substantive
    content, when they're really just a restatement of the rating/price/
    cuisine metadata that already has its own dedicated fields. These exact
    fixtures were traced from the real dipstick59 harvest/render output for
    Bryce Canyon, St. George, and Santa Fe restaurants."""
    cases = [
        ("(high volume), $$ American.", "The Lodge at Bryce Canyon Restaurant"),
        ("American fast food/pizza.", "Canyon Diner"),
        ("(French/Southwestern fine dining)", "Geronimo"),
        ("(Contemporary American)", "The Compound"),
        ("(New Mexican breakfast/lunch)", "Tia Sophia's"),
    ]
    for text, name in cases:
        assert URLDiscoverer._is_metadata_only_residual_text(text, name=name) is True, (
            f"expected {text!r} to be recognized as metadata-only for {name!r}"
        )


def test_is_metadata_only_residual_text_still_allows_real_prose_descriptions():
    """Companion to the filler-word guard above: tightening the metadata-only
    detection must not start rejecting genuine descriptive prose just because
    it happens to contain a cuisine or meal-type word (e.g. "breakfast" or
    "American") as part of a real sentence. These fixtures are real captured
    descriptions from the dipstick59 run that must keep rendering."""
    cases = [
        ("Fresh market-driven breakfast and brunch.", "Cafe Soleil"),
        ("Offers a diverse menu with homemade pies.", "Bryce Canyon Pines Restaurant"),
        ("Local artisan goods and handmade items shop.", "Moab Made"),
    ]
    for text, name in cases:
        assert URLDiscoverer._is_metadata_only_residual_text(text, name=name) is False, (
            f"expected {text!r} to still be treated as real content for {name!r}"
        )


def test_direct_batch_rows_from_html_lodge_restaurant_review_volume_junk_suppressed():
    """Full row-parse regression using the exact raw HTML captured for "The
    Lodge at Bryce Canyon Restaurant" in the dipstick59 run: the harvested
    row's description field is genuinely just "(high volume), $$ American."
    (a review-volume qualifier plus price plus cuisine, with no real prose)
    -- price and cuisine must still land in their own structured fields, but
    the leftover metadata fragment must not leak into description/
    practical_note now that the guard recognizes it as junk."""
    html = (
        "<li>The Lodge at Bryce Canyon Restaurant - 4.0/5 (high volume), $$ American. "
        '<a href="https://www.visitbrycecanyon.com/dining/the-lodge-at-bryce-canyon-restaurant/">Source</a> '
        '<a href="https://www.google.com/maps/search/?api=1&query=The+Lodge+at+Bryce+Canyon+Restaurant+Bryce+Canyon+National+Park+UT">Maps</a></li>'
    )
    rows = URLDiscoverer._direct_batch_rows_from_html(html)
    assert rows
    row = rows[0]
    assert row["name"] == "The Lodge at Bryce Canyon Restaurant"
    assert row["cuisine"] == "American"
    assert row["price_range"] == "$$"
    assert row["description"] == ""
    assert row["practical_note"] == ""


def test_direct_batch_restaurant_rating_reaches_badge_not_title_end_to_end() -> None:
    """Full-pipeline regression: a direct-batch harvested restaurant row's rating
    must end up in the html_assembler rating badge, not stuck in (or duplicated
    into) the rendered title. This chains the url_discovery merge stage into the
    html_assembler render stage — the gap here survived an isolated sanitizer-only
    test because that test hand-supplied `rating`/`raw_rating` directly instead of
    going through _build_primary_items_from_direct_batch."""
    from generator.html_assembler import HTMLAssembler

    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    rows = [
        {
            "name": "Cafe Soleil",
            "url": "https://cafesoleilzion.com/",
            "cuisine": "Cafe",
            "price_range": "$$",
            "description": "Fresh market-driven breakfast and brunch.",
            "rating": 4.7,
            "raw_rating": "4.7/5",
            "votes": 230,
        }
    ]

    merged = discoverer._build_primary_items_from_direct_batch(
        rows=rows,
        existing_items=[],
        target_count=1,
        fallback_description="Locally surfaced dinner option.",
    )

    assembler = HTMLAssembler.__new__(HTMLAssembler)
    html = assembler._build_restaurants({"dinner_recommendations": merged}, "Zion National Park")

    assert "★ 4.7/5" in html
    title_span = re.search(r'<span class="rest-name">.*?</span>', html, flags=re.DOTALL)
    assert title_span is not None
    assert "4.7" not in title_span.group(0)


def test_direct_batch_row_quality_metadata_for_url_matches_by_url():
    """The per-item single-URL resolution paths (attraction, trail-like AllTrails,
    restaurant) previously discarded rating/votes data that was already present
    on the harvested row -- only the batch-shortfall padding path carried it.
    This helper recovers it at zero extra network cost by matching the accepted
    url back to the row it came from."""
    rows = [
        {
            "name": "Angels Landing",
            "url": "https://www.nps.gov/zion/angels-landing.htm",
            "rating": 4.9,
            "raw_rating": "4.9/5",
            "votes": 5000,
        },
        {
            "name": "Emerald Pools",
            "url": "https://www.nps.gov/zion/emerald-pools.htm",
        },
    ]

    meta = URLDiscoverer._direct_batch_row_quality_metadata_for_url(
        rows, "https://www.nps.gov/zion/angels-landing.htm"
    )
    assert meta == {"rating": 4.9, "raw_rating": "4.9/5", "votes": 5000}

    # A row with no rating data at all contributes nothing.
    assert URLDiscoverer._direct_batch_row_quality_metadata_for_url(
        rows, "https://www.nps.gov/zion/emerald-pools.htm"
    ) == {}

    # No matching row for this url.
    assert URLDiscoverer._direct_batch_row_quality_metadata_for_url(
        rows, "https://www.nps.gov/zion/the-narrows.htm"
    ) == {}


def test_get_restaurant_direct_batch_rows_prefers_html_payload_before_search_rows():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._request_cache_lock = Lock()
    discoverer._restaurant_direct_batch_cache = {}
    discoverer._search = MagicMock()
    discoverer._restaurant_direct_batch_item_count = 4
    discoverer._search.chat_completion.return_value = (
        "<h2>Moab</h2><ul>"
        "<li>Desert Bistro <a href=\"https://desertbistro.com/\">Source</a> "
        "<a href=\"https://www.google.com/maps/search/?api=1&query=Desert+Bistro+Moab+UT\">Maps</a></li>"
        "</ul>"
    )

    with patch.object(discoverer, "_get_direct_batch_rows_for_destination", side_effect=AssertionError("search fallback should not run when HTML rows exist")):
        rows = discoverer._get_restaurant_direct_batch_rows_for_destination("Moab", "October 18, 2026")

    assert rows
    assert rows[0]["title"] == "Desert Bistro"
    assert rows[0]["url"] == "https://desertbistro.com/"
    assert rows[0]["maps_url"].startswith("https://www.google.com/maps/search/")


def test_get_restaurant_direct_batch_rows_persists_html_capture_artifacts(tmp_path):
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._request_cache_lock = Lock()
    discoverer._restaurant_direct_batch_cache = {}
    discoverer._search = MagicMock()
    discoverer._restaurant_direct_batch_item_count = 4
    discoverer._run_output_dir = tmp_path
    discoverer._direct_batch_html_capture_enabled = True
    discoverer._direct_batch_html_capture_subdir = "dev/url_discovery_direct_batch_html"
    discoverer._search.chat_completion.return_value = (
        "<h2>Moab</h2><ul>"
        "<li>Desert Bistro <a href=\"https://desertbistro.com/\">Source</a> "
        "<a href=\"https://www.google.com/maps/search/?api=1&query=Desert+Bistro+Moab+UT\">Maps</a></li>"
        "</ul>"
    )

    rows = discoverer._get_restaurant_direct_batch_rows_for_destination("Moab", "October 18, 2026")

    assert rows
    capture_dir = tmp_path / "dev" / "url_discovery_direct_batch_html"
    html_files = list(capture_dir.glob("*.html"))
    meta_files = list(capture_dir.glob("*.meta.json"))
    assert len(html_files) == 1
    assert len(meta_files) == 1

    html_payload = html_files[0].read_text(encoding="utf-8")
    assert "Desert Bistro" in html_payload
    assert "direct_batch_query" in html_payload

    meta = json.loads(meta_files[0].read_text(encoding="utf-8"))
    assert meta["destination"] == "Moab"
    assert meta["kind"] == "restaurant"
    assert meta["row_count"] == 1
    assert meta["html_file"] == html_files[0].name
    # The persisted query is asserted in full because it is the audit record of
    # what was actually asked -- a capture whose query drifts from the prompt is
    # worthless for diagnosing a bad harvest. The rating floor is now written as
    # "rated above N" and N is budget-aware (4.3 default, 4.0 on a low-cost
    # brief), so the phrasing changed with it. No budget is set on this fixture.
    assert meta["query"] == (
        "Generate a list of local restaurants near Moab (October 18, 2026) with clickable links to source material and corresponding Google Maps content. "
        "Include a rating, price indicator, and the cuisine or restaurant type for each item when available, using a clear numeric or price format, and a short descriptive note about the food, atmosphere, or signature dishes for each item when available. "
        "Keep only items rated above 4.3, include cuisine variety, and keep only places likely open on the indicated dates. "
        "Include only suggestions with reliable clickable links."
    )


def test_get_en_route_direct_batch_rows_falls_back_to_search_when_html_empty():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._request_cache_lock = Lock()
    discoverer._en_route_direct_batch_cache = {}
    discoverer._search = MagicMock()
    discoverer._en_route_direct_batch_item_count = 4
    discoverer._search.chat_completion.return_value = ""

    fallback_rows = [{"url": "https://www.blm.gov/visit/wilson-arch", "title": "Wilson Arch", "snippet": "BLM"}]
    with patch.object(discoverer, "_get_direct_batch_rows_for_destination", return_value=fallback_rows) as mock_fallback:
        rows = discoverer._get_en_route_direct_batch_rows_for_destination("Moab", "October 18, 2026")

    assert rows == fallback_rows
    mock_fallback.assert_called_once()


def test_get_en_route_direct_batch_rows_retries_html_prompt_when_rows_below_minimum():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._request_cache_lock = Lock()
    discoverer._en_route_direct_batch_cache = {}
    discoverer._search = MagicMock()
    discoverer._search.is_circuit_open.return_value = False
    discoverer._en_route_direct_batch_item_count = 4
    discoverer._en_route_direct_batch_min_results = 3
    discoverer._search.chat_completion.side_effect = [
        "<h2>Telluride</h2><ul><li>Lizard Head Pass <a href=\"https://www.google.com/maps/search/?api=1&query=Lizard+Head+Pass+Telluride\">Maps</a></li></ul>",
        "<h2>Telluride</h2><ul><li>Lizard Head Pass <a href=\"https://www.google.com/maps/search/?api=1&query=Lizard+Head+Pass+Telluride\">Maps</a></li><li>Rico Historic District <a href=\"https://www.colorado.com/articles/why-rico-colorado-worth-stop\">Source</a></li><li>Dolores River Overlook <a href=\"https://www.google.com/maps/search/?api=1&query=Dolores+River+Overlook\">Maps</a></li></ul>",
    ]

    rows = discoverer._get_en_route_direct_batch_rows_for_destination("Telluride", "October 18, 2026")

    assert len(rows) >= 3
    assert discoverer._search.chat_completion.call_count == 2


def test_get_restaurant_direct_batch_rows_retries_html_prompt_when_rows_below_minimum():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._request_cache_lock = Lock()
    discoverer._restaurant_direct_batch_cache = {}
    discoverer._search = MagicMock()
    discoverer._search.is_circuit_open.return_value = False
    discoverer._restaurant_direct_batch_item_count = 4
    discoverer._restaurant_direct_batch_min_results = 3
    discoverer._search.chat_completion.side_effect = [
        "<h2>Santa Fe</h2><ul><li>Restaurant A <a href=\"https://example.com/a\">Source</a></li></ul>",
        "<h2>Santa Fe</h2><ul><li>Restaurant A <a href=\"https://example.com/a\">Source</a></li><li>Restaurant B <a href=\"https://example.com/b\">Source</a></li><li>Restaurant C <a href=\"https://example.com/c\">Source</a></li></ul>",
    ]

    rows = discoverer._get_restaurant_direct_batch_rows_for_destination("Santa Fe", "October 18, 2026")

    assert len(rows) >= 3
    assert discoverer._search.chat_completion.call_count == 2


def test_direct_batch_html_skips_insufficient_rows_retry_prompt_while_circuit_open():
    """Firing a second expensive harvest call is the worst possible moment to
    do it while the circuit breaker is open -- that state means a recent
    burst of transient errors, and the retry-prompt would just compound it."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._request_cache_lock = Lock()
    discoverer._restaurant_direct_batch_cache = {}
    discoverer._search = MagicMock()
    discoverer._search.is_circuit_open.return_value = True
    discoverer._restaurant_direct_batch_item_count = 4
    discoverer._restaurant_direct_batch_min_results = 3
    discoverer._persist_direct_batch_html_capture = lambda **kwargs: None
    discoverer._search.chat_completion.return_value = (
        "<h2>Santa Fe</h2><ul><li>Restaurant A <a href=\"https://example.com/a\">Source</a></li></ul>"
    )

    rows = discoverer._get_restaurant_direct_batch_rows_for_destination("Santa Fe", "October 18, 2026")

    assert len(rows) == 1
    assert discoverer._search.chat_completion.call_count == 1


def test_is_search_circuit_open_delegates_to_underlying_search() -> None:
    """Public wrapper used by main.py's selective-retry gate (Dipstick48
    follow-up) so callers outside url_discovery.py don't reach into the
    private _search attribute directly."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._search = MagicMock()

    discoverer._search.is_circuit_open.return_value = True
    assert discoverer.is_search_circuit_open() is True

    discoverer._search.is_circuit_open.return_value = False
    assert discoverer.is_search_circuit_open() is False


def test_is_search_circuit_open_false_when_search_not_yet_constructed() -> None:
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    assert discoverer.is_search_circuit_open() is False


def test_retain_discovered_url_rejects_generic_attraction_landing_page_for_authoritative_direct_batch() -> None:
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._direct_batch_authoritative = True
    candidate = {
        "title": "The Narrows",
        "name": "The Narrows",
        "url": "https://www.nps.gov/zion/planyourvisit/things-to-do.htm",
    }
    url = "https://www.nps.gov/zion/planyourvisit/things-to-do.htm"

    result = discoverer._retain_discovered_url(
        url,
        "The Narrows",
        "Zion National Park",
        allow_alltrails=False,
        kind="attraction",
        candidate=candidate,
        allow_google_maps_search=True,
    )

    assert result == ""


def test_retain_discovered_url_rejects_generic_restaurant_landing_page_for_authoritative_direct_batch() -> None:
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._direct_batch_authoritative = True
    candidate = {
        "title": "Cafe Soleil",
        "name": "Cafe Soleil",
        "url": "https://www.tripadvisor.com/Restaurants-g60972-Zion_National_Park.html",
    }
    url = "https://www.tripadvisor.com/Restaurants-g60972-Zion_National_Park.html"

    result = discoverer._retain_discovered_url(
        url,
        "Cafe Soleil",
        "Zion National Park",
        allow_alltrails=False,
        kind="restaurant",
        candidate=candidate,
        allow_google_maps_search=True,
    )

    assert result == ""


def test_audit_demotes_direct_batch_authoritative_trail_when_over_miles_threshold():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._max_trail_miles = 3.0
    discoverer._direct_batch_authoritative = True
    discoverer._remember_direct_batch_authoritative_url(
        "https://www.alltrails.com/trail/us/utah/observation-point-trail",
        "Observation Point Trail",
    )

    trip = {
        "destinations": [
            {
                "id": "zion",
                "name": "Zion National Park",
                "ai_content": {
                    "top_attractions": [
                        {
                            "name": "Observation Point Trail",
                            "type": "hike",
                            "description": "A 5-mile hike with chain-assisted sections and exposure.",
                            "url": "https://www.alltrails.com/trail/us/utah/observation-point-trail",
                        }
                    ],
                    "getting_here": {"en_route_stops": []},
                    "dinner_recommendations": [],
                },
                "scenic_drives": [],
                "cultural_events": {"events": []},
            }
        ]
    }

    with patch.object(discoverer, "_prewarm_url_validation_cache", return_value=None):
        discoverer.audit_discovered_urls(trip)

    # Policy (2026-08-17): a non-seed item demoted below the trail-miles
    # threshold has no verified link left after demotion -- it must be
    # removed from the itinerary entirely, not kept with an empty url.
    attractions = trip["destinations"][0]["ai_content"]["top_attractions"]
    assert attractions == []


def test_audit_demotes_long_trail_when_over_miles_threshold() -> None:
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._direct_batch_authoritative = True
    discoverer._max_trail_miles = 3.0
    discoverer._remember_direct_batch_authoritative_url(
        "https://www.alltrails.com/trail/us/utah/angels-landing-trail",
        "Angels Landing",
    )

    trip = {
        "destinations": [
            {
                "id": "zion",
                "name": "Zion National Park",
                "ai_content": {
                    "top_attractions": [
                        {
                            "name": "Angels Landing",
                            "type": "hike",
                            "description": "A strenuous 5.4-mile roundtrip hike with major elevation gain.",
                            "url": "https://www.alltrails.com/trail/us/utah/angels-landing-trail",
                        }
                    ],
                    "getting_here": {"en_route_stops": []},
                    "dinner_recommendations": [],
                },
                "scenic_drives": [],
                "cultural_events": {"events": []},
            }
        ]
    }

    with patch.object(discoverer, "_prewarm_url_validation_cache", return_value=None):
        discoverer.audit_discovered_urls(trip)

    # Policy (2026-08-17): non-seed, over-threshold demoted trail has no
    # verified link -- removed from the itinerary entirely.
    attractions = trip["destinations"][0]["ai_content"]["top_attractions"]
    assert attractions == []


def test_audit_discovered_urls_rejects_wrong_topic_link_for_scenic_drive_overlooks() -> None:
    """End-to-end regression for the real Bryce Canyon eval bug: the
    "Scenic Drive Overlooks" attraction linked to nps.gov's hoodoo-geology
    page instead of a page about the drive itself. Confirms
    audit_discovered_urls actually threads the item's own description
    through to the relevance gate (item_description=attr.get("description")),
    not just that _is_relevant_result behaves correctly when called directly."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._url_validator = MagicMock()
    discoverer._url_validator.verify_url.return_value = (True, 200)

    trip = {
        "destinations": [
            {
                "id": "bryce",
                "name": "Bryce Canyon National Park",
                "ai_content": {
                    "top_attractions": [
                        {
                            "name": "Scenic Drive Overlooks",
                            "description": "18-mile auto tour with multiple pullouts for hoodoo viewing.",
                            "url": "https://www.nps.gov/brca/learn/nature/hoodoos.htm",
                        }
                    ],
                    "getting_here": {"en_route_stops": []},
                    "dinner_recommendations": [],
                },
                "scenic_drives": [],
                "cultural_events": {"events": []},
            }
        ]
    }

    page_text = (
        "Hoodoos - Bryce Canyon National Park. The formation of Bryce Canyon "
        "and its hoodoos requires 3 steps: deposition of rocks, uplift of the "
        "land, and weathering and erosion."
    )
    with patch.object(discoverer, "_prewarm_url_validation_cache", return_value=None):
        with patch.object(discoverer, "_fetch_page_text", return_value=(True, 200, page_text)):
            discoverer.audit_discovered_urls(trip)

    # Non-seed attraction with no verified link left after rejection is
    # pruned entirely, matching the established verified-link-or-seed policy.
    assert trip["destinations"][0]["ai_content"]["top_attractions"] == []


def test_audit_demotion_strips_hike_badge_fields_not_just_type_and_url() -> None:
    """Regression for dipstick58: "Peek-a-boo Loop" and "Fairyland Loop" (Bryce
    Canyon, real run data) were correctly demoted -- url stripped, type flipped
    to "attraction", threshold note attached -- yet still rendered with a
    badge-hike-strenuous "Strenuous" badge in html_assembler, because that
    badge is driven purely by the item's "difficulty" field (see
    html_assembler.py's diff_class = difficulty_colors.get(diff, "")), which
    is independent of "type" and was never cleared during demotion. A demoted
    trail must present as a genuinely plain attraction, not a hike whose link
    happened to be removed.

    Both items are non-seed, so under the verified-link-or-seed policy
    (2026-08-17) they are additionally pruned from top_attractions entirely
    after demotion (asserted below). The field-stripping itself is still
    verified by holding direct references to the original dicts, which are
    mutated in place before being dropped from the list.
    """
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._max_trail_miles = 3.0

    trip = {
        "destinations": [
            {
                "id": "bryce",
                "name": "Bryce Canyon National Park",
                "ai_content": {
                    "top_attractions": [
                        {
                            "name": "Peek-a-boo Loop",
                            "type": "hike",
                            "difficulty": "Strenuous",
                            "duration": "3-4 hrs round-trip",
                            "elevation_gain_feet": 1500,
                            "description": (
                                "This 5.5-mile trail winds through the heart of the "
                                "hoodoos, offering unique perspectives. Expect steep "
                                "sections and views."
                            ),
                            "practical_note": "Best to start early to avoid midday heat.",
                            "rating": 4.9,
                        },
                        {
                            "name": "Fairyland Loop",
                            "type": "hike",
                            "difficulty": "Strenuous",
                            "duration": "4-5 hrs round-trip",
                            "elevation_gain_feet": 1700,
                            "description": (
                                "A 8-mile loop that showcases less-visited hoodoos and "
                                "rock formations. The trail offers views and fewer crowds."
                            ),
                            "practical_note": "Bring plenty of water as there are no water sources along the trail.",
                            "rating": 4.5,
                        },
                    ],
                    "getting_here": {"en_route_stops": []},
                    "dinner_recommendations": [],
                },
                "scenic_drives": [],
                "cultural_events": {"events": []},
            }
        ]
    }

    original_attrs = list(trip["destinations"][0]["ai_content"]["top_attractions"])

    with patch.object(discoverer, "_prewarm_url_validation_cache", return_value=None):
        discoverer.audit_discovered_urls(trip)

    for attr in original_attrs:
        assert attr.get("type") == "attraction"
        assert str(attr.get("url", "")) == ""
        assert "threshold" in str(attr.get("practical_note", "") or "").lower()
        # The real bug: these hike-specific fields survived demotion and kept
        # rendering badge-hike-strenuous/badge-elevation despite the item no
        # longer being presented as a hike.
        assert "difficulty" not in attr
        assert "elevation_gain_feet" not in attr

    # Policy (2026-08-17): both are non-seed and, after demotion, have no
    # verified url -- removed from top_attractions entirely.
    attractions = trip["destinations"][0]["ai_content"]["top_attractions"]
    assert attractions == []


def test_audit_demotes_alltrails_linked_attraction_when_description_lacks_trail_keywords() -> None:
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._max_trail_miles = 3.0

    trip = {
        "destinations": [
            {
                "id": "zion",
                "name": "Zion National Park",
                "ai_content": {
                    "top_attractions": [
                        {
                            "name": "Angels Landing",
                            "type": "attraction",
                            "description": "Iconic summit route with major exposure.",
                            "url": "https://www.alltrails.com/trail/us/utah/angels-landing-trail",
                        }
                    ],
                    "getting_here": {"en_route_stops": []},
                    "dinner_recommendations": [],
                },
                "scenic_drives": [],
                "cultural_events": {"events": []},
            }
        ]
    }

    with patch.object(discoverer, "_prewarm_url_validation_cache", return_value=None):
        with patch.object(
            discoverer,
            "_fetch_page_text",
            return_value=(True, 200, "Angels Landing Trail is a 5.4-mile out-and-back route in Zion."),
        ):
            discoverer.audit_discovered_urls(trip)

    # Policy (2026-08-17): non-seed, demoted with no verified link -- removed.
    attractions = trip["destinations"][0]["ai_content"]["top_attractions"]
    assert attractions == []


def test_audit_demotes_trail_over_threshold_keeps_primary_maps_search_url() -> None:
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._direct_batch_authoritative = True
    discoverer._max_trail_miles = 3.0
    discoverer._remember_direct_batch_authoritative_url(
        "https://www.alltrails.com/trail/us/utah/the-narrows-top-down",
        "The Narrows",
    )

    trip = {
        "destinations": [
            {
                "id": "zion",
                "name": "Zion National Park",
                "ai_content": {
                    "top_attractions": [
                        {
                            "name": "The Narrows",
                            "type": "hike",
                            "description": "Long canyon route, typically 9 miles or more depending on turnaround.",
                            "url": "https://www.alltrails.com/trail/us/utah/the-narrows-top-down",
                        }
                    ],
                    "getting_here": {"en_route_stops": []},
                    "dinner_recommendations": [],
                },
                "scenic_drives": [],
                "cultural_events": {"events": []},
            }
        ]
    }

    with patch.object(discoverer, "_prewarm_url_validation_cache", return_value=None):
        discoverer.audit_discovered_urls(trip)

    # Policy (2026-08-17): non-seed, demoted with no verified link -- removed.
    attractions = trip["destinations"][0]["ai_content"]["top_attractions"]
    assert attractions == []


def test_summarizes_restaurant_dispositions_by_item_and_source() -> None:
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    disposition_threads = {
        "zion-1": [
            {
                "kind": "restaurant",
                "item": "Zion Pizza & Noodle Co",
                "reason": "direct_batch_accepted",
                "source": "direct_batch",
                "message": "restaurant link (direct-link batch)",
                "url": "https://example.com/zion-pizza",
            }
        ],
        "zion-2": [
            {
                "kind": "restaurant",
                "item": "Cafe Soleil",
                "reason": "maps_fallback_only",
                "source": "search",
                "message": "restaurant link omitted; no canonical URL found",
                "url": "",
            }
        ],
    }

    summary = discoverer._summarize_entity_dispositions(
        kind="restaurant",
        disposition_threads=disposition_threads,
    )

    assert summary["total"] == 2
    assert summary["disposition_counts"]["accepted"] == 1
    assert summary["disposition_counts"]["rejected"] == 1
    assert summary["source_counts"]["direct_batch"] == 1
    assert summary["source_counts"]["search"] == 1

    pizza_summary = next(item for item in summary["items"] if item["name"] == "Zion Pizza & Noodle Co")
    assert pizza_summary["final_outcome"] == "accepted"
    assert pizza_summary["source"] == "direct_batch"
    assert pizza_summary["reasons"] == ["direct_batch_accepted"]

    cafe_summary = next(item for item in summary["items"] if item["name"] == "Cafe Soleil")
    assert cafe_summary["final_outcome"] == "rejected"
    assert cafe_summary["source"] == "search"
    assert cafe_summary["reasons"] == ["maps_fallback_only"]


def test_classify_disposition_outcome_canonical_states() -> None:
    classify = URLDiscoverer._classify_disposition_outcome
    assert classify("direct_batch_accepted", "https://example.com") == "accepted"
    assert classify("direct_batch_existing_url_preserved", "https://example.com") == "accepted"
    assert classify("seed_ai_candidate_recovered", "https://example.com") == "accepted"
    assert classify("direct_batch_source_locked_no_match", "") == "rejected"
    assert classify("maps_fallback_only", "") == "rejected"
    assert classify("direct_batch_no_accepted_candidates", "") == "rejected"
    assert classify("url_rejected", "") == "rejected"
    assert classify("interest_filter_skipped", "") == "filtered"
    assert classify("interest_filter_removed", "") == "filtered"
    assert classify("entity_removed", "") == "filtered"
    assert classify("trail_links_disabled", "") == "filtered"
    assert classify("threshold_demoted_to_attraction", "") == "demoted"
    assert classify("seed_threshold_override", "https://example.com") == "skipped"
    # URL-presence fallback: unknown reason with URL → accepted
    assert classify("some_novel_reason", "https://example.com") == "accepted"
    # URL-presence fallback: unknown reason without URL → rejected
    assert classify("some_novel_reason", "") == "rejected"


def test_summarize_attraction_dispositions() -> None:
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    threads = {
        "a1": [{"kind": "attraction", "item": "Angels Landing", "reason": "alltrails_accepted",
                "source": "alltrails", "url": "https://www.alltrails.com/trail/us/utah/angels-landing"}],
        "a2": [{"kind": "attraction", "item": "Canyon Overlook", "reason": "nps_accepted",
                "source": "search", "url": "https://www.nps.gov/zion/overlook"}],
        "a3": [{"kind": "attraction", "item": "Zion Narrows", "reason": "direct_batch_source_locked_no_match",
                "source": "direct_batch", "url": ""}],
    }
    summary = discoverer._summarize_entity_dispositions(kind="attraction", disposition_threads=threads)
    assert summary["total"] == 3
    assert summary["disposition_counts"]["accepted"] == 2
    assert summary["disposition_counts"]["rejected"] == 1
    landing = next(i for i in summary["items"] if i["name"] == "Angels Landing")
    assert landing["final_outcome"] == "accepted"
    assert landing["source"] == "alltrails"
    narrows = next(i for i in summary["items"] if i["name"] == "Zion Narrows")
    assert narrows["final_outcome"] == "rejected"


def test_summarize_trail_dispositions_uses_attraction_kind_events() -> None:
    """Trails are logged as kind='attraction'; trail summary must include them."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    threads = {
        "t1": [{"kind": "attraction", "item": "Observation Point", "reason": "alltrails_accepted",
                "source": "alltrails", "url": "https://www.alltrails.com/trail/us/utah/observation-point"}],
        "t2": [{"kind": "attraction", "item": "West Rim Trail", "reason": "threshold_demoted_to_attraction",
                "source": "direct_batch", "url": ""}],
        "t3": [{"kind": "attraction", "item": "Subway", "reason": "trail_links_disabled",
                "source": "other", "url": ""}],
    }
    summary = discoverer._summarize_entity_dispositions(kind="trail", disposition_threads=threads)
    assert summary["total"] == 3
    assert summary["disposition_counts"]["accepted"] == 1
    assert summary["disposition_counts"]["demoted"] == 1
    assert summary["disposition_counts"]["filtered"] == 1
    west_rim = next(i for i in summary["items"] if i["name"] == "West Rim Trail")
    assert west_rim["final_outcome"] == "demoted"
    subway = next(i for i in summary["items"] if i["name"] == "Subway")
    assert subway["final_outcome"] == "filtered"


def test_summarize_en_route_stop_dispositions() -> None:
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    threads = {
        "e1": [{"kind": "en_route_stop", "item": "Kolob Canyons", "reason": "direct_batch_accepted",
                "source": "direct_batch", "url": "https://www.nps.gov/zion/kolob"}],
        "e2": [{"kind": "en_route_stop", "item": "Cedar Breaks", "reason": "direct_batch_no_match",
                "source": "direct_batch", "url": ""}],
    }
    summary = discoverer._summarize_entity_dispositions(kind="en_route_stop", disposition_threads=threads)
    assert summary["total"] == 2
    assert summary["disposition_counts"]["accepted"] == 1
    assert summary["disposition_counts"]["rejected"] == 1
    kolob = next(i for i in summary["items"] if i["name"] == "Kolob Canyons")
    assert kolob["final_outcome"] == "accepted"


def test_disposition_outcome_priority_accepted_beats_rejected() -> None:
    """When a single item gets both a rejected and accepted event, accepted wins."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    threads = {
        "x1": [
            {"kind": "restaurant", "item": "Spotted Dog", "reason": "direct_batch_candidate_rejected",
             "source": "direct_batch", "url": ""},
            {"kind": "restaurant", "item": "Spotted Dog", "reason": "ai_candidate_accepted",
             "source": "ai_candidate", "url": "https://www.spotteddog.com"},
        ],
    }
    summary = discoverer._summarize_entity_dispositions(kind="restaurant", disposition_threads=threads)
    assert summary["total"] == 1
    assert summary["items"][0]["final_outcome"] == "accepted"
    assert summary["items"][0]["source"] == "ai_candidate"


def test_scenic_drive_search_name_strips_ai_added_day_trip_suffix() -> None:
    assert URLDiscoverer._scenic_drive_search_name("Notom-Bullfrog Road Day Trip") == "Notom-Bullfrog Road"
    assert URLDiscoverer._scenic_drive_search_name("Zion Canyon Scenic Drive") == "Zion Canyon Scenic Drive"
    assert URLDiscoverer._scenic_drive_search_name("") == ""


def test_discover_scenic_drives_search_query_omits_day_trip_suffix() -> None:
    """Quoted exact-phrase search variants must target the road's real name,
    not an AI-added activity-type descriptor no real source uses verbatim."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    dest = {
        "name": "Capitol Reef National Park",
        "scenic_drives": [{"title": "Notom-Bullfrog Road Day Trip"}],
    }

    with patch.object(discoverer, "_fetch_page_text", return_value=(True, 200, "unrelated park page")):
        with patch.object(discoverer, "_search_first", return_value=None) as mock_search:
            discoverer._discover_scenic_drives(dest, "Capitol Reef National Park", nps_code="care")

    searched_query_variants = mock_search.call_args.args[0]
    assert any("Day Trip" not in v for v in searched_query_variants)
    assert not any("Day Trip" in v for v in searched_query_variants)


def test_discover_scenic_drives_rejects_deterministic_url_for_unrelated_named_drive() -> None:
    """Real reported bug: a park can have several distinctly named scenic
    drives (Capitol Reef's paved 'Scenic Drive' vs. the separate,
    backcountry 'Notom-Bullfrog Road'). Blindly trusting the deterministic
    NPS URL whenever it returns HTTP 200 gave every drive the same generic
    park page, pointing at the wrong starting point. The page content must
    actually be about the named drive, or fall through to a real search."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    dest = {
        "name": "Capitol Reef National Park",
        "scenic_drives": [{"title": "Notom-Bullfrog Road Day Trip"}],
    }

    with patch.object(
        discoverer,
        "_fetch_page_text",
        return_value=(True, 200, "The Capitol Reef Scenic Drive is an 8-mile paved road through the park."),
    ):
        with patch.object(
            discoverer, "_search_first", return_value="https://www.nps.gov/care/planyourvisit/notom-bullfrog-road.htm"
        ) as mock_search:
            discoverer._discover_scenic_drives(dest, "Capitol Reef National Park", nps_code="care")

    mock_search.assert_called_once()
    assert dest["scenic_drives"][0]["url"] == "https://www.nps.gov/care/planyourvisit/notom-bullfrog-road.htm"


def test_discover_scenic_drives_uses_nps_deterministic_url_for_nps_park() -> None:
    """For NPS parks the deterministic scenic-drive page should be preferred over a search."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    dest = {
        "name": "Zion National Park",
        "scenic_drives": [{"title": "Zion Canyon Scenic Drive"}],
    }

    with patch.object(
        discoverer,
        "_fetch_page_text",
        return_value=(True, 200, "The Zion Canyon Scenic Drive is the park's main route through the canyon."),
    ):
        with patch.object(discoverer, "_search_first") as mock_search:
            discoverer._discover_scenic_drives(dest, "Zion National Park", nps_code="zion")

    mock_search.assert_not_called()
    assert dest["scenic_drives"][0]["url"] == "https://www.nps.gov/zion/planyourvisit/scenic-drive.htm"


def test_discover_scenic_drives_falls_back_to_search_when_nps_page_absent() -> None:
    """When the NPS deterministic page is unreachable, discovery falls back to search."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    dest = {
        "name": "Zion National Park",
        "scenic_drives": [{"title": "Zion Canyon Scenic Drive"}],
    }

    with patch.object(discoverer, "_fetch_page_text", return_value=(False, 404, "")):
        with patch.object(discoverer, "_search_first", return_value="https://www.visitutah.com/zion-canyon-scenic-drive") as mock_search:
            discoverer._discover_scenic_drives(dest, "Zion National Park", nps_code="zion")

    mock_search.assert_called_once()
    assert "visitutah.com" in dest["scenic_drives"][0]["url"]


def test_discover_scenic_drives_no_nps_code_uses_search() -> None:
    """Without an NPS code, discovery uses search only (no deterministic attempt)."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    dest = {
        "name": "Route 66",
        "scenic_drives": [{"title": "Historic Route 66"}],
    }

    with patch.object(discoverer, "_fetch_page_text") as mock_fetch:
        with patch.object(discoverer, "_search_first", return_value="https://www.historic66.com") as mock_search:
            discoverer._discover_scenic_drives(dest, "Route 66", nps_code=None)

    mock_fetch.assert_not_called()
    mock_search.assert_called_once()


def test_audit_emits_audit_rejection_event_for_scenic_drive_non_route_url() -> None:
    """Audit stripping a scenic drive URL should emit an audit_url_rejected broker event."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._decision_threads_by_destination = {}
    discoverer._decision_stats_by_destination = {}
    discoverer._decision_source_stats_by_destination = {}
    discoverer._decision_event_sequence = 0
    discoverer._request_cache_lock = __import__("threading").Lock()
    discoverer._direct_batch_authoritative = False
    discoverer._direct_batch_authoritative_urls = set()

    trip = {
        "destinations": [
            {
                "id": "zion",
                "name": "Zion National Park",
                "ai_content": {
                    "top_attractions": [],
                    "getting_here": {"en_route_stops": []},
                    "dinner_recommendations": [],
                },
                "scenic_drives": [
                    {
                        # Generic NPS section index — not route-specific, should be stripped
                        "title": "Zion Canyon Scenic Drive",
                        "url": "https://www.nps.gov/zion/planyourvisit/index.htm",
                    }
                ],
                "cultural_events": {"events": []},
            }
        ]
    }

    with patch.object(discoverer, "_prewarm_url_validation_cache", return_value=None):
        discoverer.audit_discovered_urls(trip)

    threads = discoverer._decision_threads_by_destination.get("Zion National Park", {})
    all_events = [ev for evs in threads.values() for ev in evs if isinstance(ev, dict)]
    audit_events = [ev for ev in all_events if "audit_url_rejected" in str(ev.get("reason", ""))]
    assert len(audit_events) >= 1, f"Expected audit_url_rejected; got: {all_events}"
    assert audit_events[0]["item"] == "Zion Canyon Scenic Drive"


def test_broker_output_includes_scenic_drive_dispositions() -> None:
    """scenic_drive_dispositions must appear in the broker output block from discover_all."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    threads = {
        "sd-1": [{
            "kind": "scenic_drive",
            "item": "Zion Canyon Scenic Drive",
            "reason": "nps_deterministic_accepted",
            "source": "nps",
            "url": "https://www.nps.gov/zion/planyourvisit/scenic-drive.htm",
        }],
        "sd-2": [{
            "kind": "scenic_drive",
            "item": "Pa'rus Trail Loop",
            "reason": "no_match",
            "source": "search",
            "url": "",
        }],
    }
    summary = discoverer._summarize_entity_dispositions(kind="scenic_drive", disposition_threads=threads)
    assert summary["total"] == 2
    assert summary["disposition_counts"]["accepted"] == 1
    assert summary["disposition_counts"]["rejected"] == 1
    zion = next(i for i in summary["items"] if i["name"] == "Zion Canyon Scenic Drive")
    assert zion["final_outcome"] == "accepted"
    assert zion["source"] == "nps"


def test_is_definitively_dead_status_recognizes_dns_and_connection_failures() -> None:
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    assert discoverer._is_definitively_dead_status(404) is True
    assert discoverer._is_definitively_dead_status(410) is True
    assert discoverer._is_definitively_dead_status(403) is False
    assert discoverer._is_definitively_dead_status(500) is False
    assert discoverer._is_definitively_dead_status("timeout") is False
    dns_error = (
        "HTTPSConnectionPool(host='flanigansinn.com', port=443): Max retries exceeded "
        "with url: / (Caused by NameResolutionError(\"Failed to resolve 'flanigansinn.com' "
        "([Errno 11001] getaddrinfo failed)\"))"
    )
    assert discoverer._is_definitively_dead_status(dns_error) is True
    refused = "ConnectionError(MaxRetryError(\"Failed to establish a new connection: [Errno 111] Connection refused\"))"
    assert discoverer._is_definitively_dead_status(refused) is True


def test_is_bot_block_false_negative_dead_status_scoped_to_gov_connection_refused() -> None:
    """`_is_definitively_dead_status` alone can't tell a genuinely nonexistent
    host apart from a live .gov host that TCP-refused/reset an automated
    request (dipstick67: nps.gov pages for "Cliff Palace" at Mesa Verde and
    "Checkerboard Mesa" -- both live, famous, actively maintained -- were
    rejected as dead this way). The carve-out must fire only for the
    ambiguous "failed to establish a new connection" marker on a .gov host,
    never for an explicit 404/410, never for an unambiguous DNS-failure
    marker, and never for a non-.gov domain."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    refused = "ConnectionError(MaxRetryError(\"Failed to establish a new connection: [Errno 111] Connection refused\"))"

    assert discoverer._is_bot_block_false_negative_dead_status(
        "https://www.nps.gov/meve/planyourvisit/cliff_palace.htm", refused
    ) is True
    assert discoverer._is_bot_block_false_negative_dead_status(
        "https://www.flanigansinn.com/spotted-dog-cafe/", refused
    ) is False
    assert discoverer._is_bot_block_false_negative_dead_status(
        "https://www.nps.gov/meve/planyourvisit/cliff_palace.htm", 404
    ) is False
    dns_error_on_gov = (
        "HTTPSConnectionPool(host='www.nps.gov', port=443): Max retries exceeded "
        "with url: / (Caused by NameResolutionError(\"Failed to resolve 'www.nps.gov' "
        "([Errno 11001] getaddrinfo failed)\"))"
    )
    assert discoverer._is_bot_block_false_negative_dead_status(
        "https://www.nps.gov/meve/planyourvisit/cliff_palace.htm", dns_error_on_gov
    ) is False


def test_retain_url_preserves_nps_gov_en_route_stop_on_connection_refused_false_negative() -> None:
    """Regression for dipstick67: direct-batch harvest found the real,
    correct, official nps.gov page for the en-route stop "Cliff Palace
    (Mesa Verde)", but it was rejected outright as "dead" on a single fetch
    that failed with a connection-refused-style error -- the signature of
    nps.gov's bot-blocking, not of a page that no longer exists. The
    row-matched, non-DNS, .gov carve-out must let it through."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._direct_batch_authoritative = True
    candidate = {
        "name": "Cliff Palace",
        "title": "Cliff Palace",
        "url": "https://www.nps.gov/meve/planyourvisit/cliff_palace.htm",
        "snippet": "Cliff Palace at Mesa Verde National Park",
    }
    refused = "ConnectionError(MaxRetryError(\"Failed to establish a new connection: [Errno 111] Connection refused\"))"

    with patch.object(discoverer, "_fetch_page_text", return_value=(False, refused, "")):
        out = discoverer._retain_discovered_url(
            "https://www.nps.gov/meve/planyourvisit/cliff_palace.htm",
            "Cliff Palace (Mesa Verde)",
            "Pagosa Springs",
            allow_alltrails=False,
            kind="en-route stop",
            candidate=candidate,
        )

    assert out == "https://www.nps.gov/meve/planyourvisit/cliff_palace.htm"


def test_retain_url_preserves_multi_token_en_route_stop_row_matched_candidate() -> None:
    """Regression for dipstick67: the single-token preservation exemption only
    covered one-word item names, so real, row-matched, official multi-word
    en-route stops ("Corona Arch" -> blm.gov/visit/corona-arch-trail,
    2 tokens) still fell through to the deep relevance gate, which requires
    the *destination* name to appear on the stop's own page -- something an
    en-route stop (explicitly not part of the destination, per
    docs/design/url-discovery-and-audit.md) has no reason to do. Attractions
    keep the stricter single-token-only bar; only en-route stops get the
    wider exemption."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._direct_batch_authoritative = True
    candidate = {
        "name": "Corona Arch",
        "title": "Corona Arch Trailhead",
        "url": "https://www.blm.gov/visit/corona-arch-trail",
        "snippet": "Corona Arch is a popular hiking destination near Moab, Utah.",
    }

    with patch.object(discoverer, "_fetch_page_text", return_value=(True, 200, "")):
        out = discoverer._retain_discovered_url(
            "https://www.blm.gov/visit/corona-arch-trail",
            "Corona Arch",
            "Canyonlands National Park",
            allow_alltrails=False,
            kind="en-route stop",
            candidate=candidate,
        )

    assert out == "https://www.blm.gov/visit/corona-arch-trail"


def test_retain_url_rejects_redirect_to_generic_hub_page_for_en_route_stop() -> None:
    """Regression for dipstick69: the en-route stop "Poshuouinge Pueblo Ruins"
    (Santa Fe leg, via Pagosa Springs) was linked to
    fs.usda.gov/recarea/carson/recarea/?recid=44248 -- a URL whose distinguishing
    ?recid= query param made it look item-specific, so it passed
    _is_generic_section_landing_page's pure URL-string check and was then accepted
    via the direct-batch row-matched leniency path. Live-fetch (2026-08-18)
    confirmed this exact URL 301-redirects to fs.usda.gov/r03/carson/recreation, a
    generic Carson National Forest recreation hub with zero mentions of
    "Poshuouinge" anywhere on the page -- and that final path's own last segment
    ("recreation") isn't even in _is_generic_section_landing_page's generic_sections
    set, so the fix must fall back to checking whether the item's own name actually
    appears in the fetched page text, not just re-run the URL-string heuristic
    unchanged. The row-matched leniency must not treat a redirect target as
    automatically trustworthy just because the original URL string looked specific."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._direct_batch_authoritative = True
    discoverer._fetch_final_url_cache = {}
    original_url = "https://www.fs.usda.gov/recarea/carson/recarea/?recid=44248"
    final_url = "http://www.fs.usda.gov/r03/carson/recreation"
    candidate = {
        "name": "Poshuouinge Pueblo Ruins",
        "title": "Poshuouinge Pueblo Ruins",
        "url": original_url,
        "snippet": "Poshuouinge Pueblo Ruins ancestral pueblo site near Abiquiu.",
    }
    generic_hub_text = (
        "Carson National Forest Recreation. Explore trails, campgrounds, and "
        "scenic areas throughout the forest. Plan your visit today."
    )

    def fake_fetch_page_text(url, timeout=8):
        discoverer._fetch_final_url_cache[url] = final_url
        return True, 200, generic_hub_text

    with patch.object(discoverer, "_fetch_page_text", side_effect=fake_fetch_page_text):
        out = discoverer._retain_discovered_url(
            original_url,
            "Poshuouinge Pueblo Ruins",
            "Santa Fe",
            allow_alltrails=False,
            kind="en-route stop",
            candidate=candidate,
        )

    assert out == ""


def test_retain_url_preserves_row_matched_candidate_when_redirect_target_still_mentions_item() -> None:
    """Companion to the dipstick69 regression above: a redirect alone must not be
    treated as automatically disqualifying. If the page the URL actually resolves
    to still names the item, the row-matched leniency should still preserve it --
    the new check is about relevance at the final destination, not about punishing
    redirects in general."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._direct_batch_authoritative = True
    discoverer._fetch_final_url_cache = {}
    original_url = "https://www.blm.gov/visit/corona-arch-old-slug"
    final_url = "https://www.blm.gov/visit/corona-arch-trail"
    candidate = {
        "name": "Corona Arch",
        "title": "Corona Arch Trailhead",
        "url": original_url,
        "snippet": "Corona Arch is a popular hiking destination near Moab, Utah.",
    }
    item_specific_text = "Corona Arch Trail is a moderate 2.4 mile hike to the iconic Corona Arch near Moab."

    def fake_fetch_page_text(url, timeout=8):
        discoverer._fetch_final_url_cache[url] = final_url
        return True, 200, item_specific_text

    with patch.object(discoverer, "_fetch_page_text", side_effect=fake_fetch_page_text):
        out = discoverer._retain_discovered_url(
            original_url,
            "Corona Arch",
            "Canyonlands National Park",
            allow_alltrails=False,
            kind="en-route stop",
            candidate=candidate,
        )

    assert out == original_url


def test_retain_url_rejects_redirect_to_generic_hub_page_for_restaurant() -> None:
    """Same redirect-genericness gap as dipstick69, but exercised through the
    restaurant-specific 'item-matched authoritative direct-batch URL' leniency
    block, which is a separate code path from the attraction/en-route-stop/
    restaurant block above (it fetches and returns independently) and had the
    same gap: it fetched the page (for the dead-status check) but never asked
    whether the fetch's own final URL was still about this restaurant."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._direct_batch_authoritative = True
    discoverer._fetch_final_url_cache = {}
    original_url = "https://www.example-inn.com/spotted-dog-cafe/"
    final_url = "https://www.example-inn.com/dining/"
    candidate = {
        "name": "Spotted Dog Cafe",
        "title": "Spotted Dog Cafe",
        "url": original_url,
        "snippet": "Spotted Dog Cafe 4.6/5 $$$",
    }
    generic_dining_text = "Dining at Example Inn. Explore our restaurants and eateries. Make a reservation today."

    def fake_fetch_page_text(url, timeout=8):
        discoverer._fetch_final_url_cache[url] = final_url
        return True, 200, generic_dining_text

    with patch.object(discoverer, "_fetch_page_text", side_effect=fake_fetch_page_text):
        out = discoverer._retain_discovered_url(
            original_url,
            "Spotted Dog Cafe",
            "Zion National Park",
            allow_alltrails=False,
            kind="restaurant",
            candidate=candidate,
        )

    assert out == ""


def test_retain_url_rejects_matched_restaurant_row_with_unresolvable_domain() -> None:
    """A URL whose domain fails DNS resolution entirely is at least as dead as an
    explicit 404, and must not be published just because the fetch failure came
    back as a connection-error string rather than an HTTP status code."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._direct_batch_authoritative = True
    candidate = {
        "name": "Spotted Dog Cafe",
        "title": "Spotted Dog Cafe",
        "url": "https://www.flanigansinn.com/spotted-dog-cafe/",
        "snippet": "Spotted Dog Cafe 4.6/5 $$$",
    }
    dns_error = (
        "HTTPSConnectionPool(host='www.flanigansinn.com', port=443): Max retries exceeded "
        "with url: / (Caused by NameResolutionError(\"Failed to resolve 'www.flanigansinn.com' "
        "([Errno 11001] getaddrinfo failed)\"))"
    )

    with patch.object(discoverer, "_fetch_page_text", return_value=(False, dns_error, "")):
        out = discoverer._retain_discovered_url(
            "https://www.flanigansinn.com/spotted-dog-cafe/",
            "Spotted Dog Cafe",
            "Zion National Park",
            allow_alltrails=False,
            kind="restaurant",
            candidate=candidate,
        )

    assert out == ""


def test_audit_keeps_direct_batch_authoritative_restaurant_even_if_generic_landing_url():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._direct_batch_authoritative = True
    discoverer._remember_direct_batch_authoritative_url(
        "https://www.discovermoab.com/restaurants/",
        "Desert Bistro",
    )

    trip = {
        "destinations": [
            {
                "id": "moab",
                "name": "Moab",
                "ai_content": {
                    "top_attractions": [],
                    "getting_here": {"en_route_stops": []},
                    "dinner_recommendations": [
                        {
                            "name": "Desert Bistro",
                            "url": "https://www.discovermoab.com/restaurants/",
                        }
                    ],
                },
                "scenic_drives": [],
                "cultural_events": {"events": []},
            }
        ]
    }

    with patch.object(discoverer, "_prewarm_url_validation_cache", return_value=None):
        discoverer.audit_discovered_urls(trip)

    restaurants = trip["destinations"][0]["ai_content"]["dinner_recommendations"]
    assert len(restaurants) == 1
    assert restaurants[0]["url"] == "https://www.discovermoab.com/restaurants/"


def test_retain_url_keeps_authoritative_direct_batch_restaurant_when_candidate_matches_item():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._direct_batch_authoritative = True
    candidate = {
        "name": "Allred's Restaurant",
        "title": "Allred's Restaurant",
        "url": "https://www.telluride.com/dining/allreds",
        "snippet": "Allred's Restaurant in Telluride",
    }

    out = discoverer._retain_discovered_url(
        "https://www.telluride.com/dining/",
        "Allred's Restaurant",
        "Telluride",
        allow_alltrails=False,
        kind="restaurant",
        candidate=candidate,
    )

    assert out == "https://www.telluride.com/dining/"


def test_retain_url_keeps_remembered_authoritative_direct_batch_restaurant_without_candidate_row():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._direct_batch_authoritative = True
    discoverer._remember_direct_batch_authoritative_url(
        "https://www.telluride.com/dining/",
        "Allred's Restaurant",
    )

    out = discoverer._retain_discovered_url(
        "https://www.telluride.com/dining/",
        "Allred's Restaurant",
        "Telluride",
        allow_alltrails=False,
        kind="restaurant",
    )

    assert out == "https://www.telluride.com/dining/"


def test_retain_url_rejects_generic_attraction_listing_page_even_when_candidate_matches_item():
    """The item-matched authoritative direct-batch leniency block only screens for
    restaurant-shaped area listings (tripadvisor /restaurants-, /restaurants/,
    restaurants-near). For kind='attraction' it must not bypass the dedicated
    generic-section-landing-page gate, or a TripAdvisor 'things to do' listing page
    can be retained as if it were the specific attraction's canonical link."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._direct_batch_authoritative = True
    candidate = {
        "name": "Zion Human History Museum",
        "title": "Zion Human History Museum",
        "url": "https://www.tripadvisor.com/Attractions-g60999-Activities-Zion_National_Park_Utah.html",
        "snippet": "Zion Human History Museum things to do",
    }

    out = discoverer._retain_discovered_url(
        "https://www.tripadvisor.com/Attractions-g60999-Activities-Zion_National_Park_Utah.html",
        "Zion Human History Museum",
        "Zion National Park",
        allow_alltrails=False,
        kind="attraction",
        candidate=candidate,
    )

    assert out == ""


def test_retain_url_rejects_social_media_even_when_matched_restaurant_row() -> None:
    """The item-matched restaurant leniency block only screens for restaurant-shaped
    area listings; it must not bypass the URL-class policy blocklist. With the
    project's actual config (enforce mode, social_media blocked), a Facebook page
    for a matched restaurant row must still be rejected, not published as the
    canonical link."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._direct_batch_authoritative = True
    discoverer._url_policy_mode = "enforce"
    discoverer._url_policy_blocked_classes = {
        "google_search",
        "google_maps_search",
        "google_maps_dir",
        "social_media",
    }
    candidate = {
        "name": "Bit & Spur",
        "title": "Bit & Spur",
        "url": "https://www.facebook.com/BitAndSpurRestaurant",
        "snippet": "Bit & Spur Springdale Utah",
    }

    out = discoverer._retain_discovered_url(
        "https://www.facebook.com/BitAndSpurRestaurant",
        "Bit & Spur",
        "Zion National Park",
        allow_alltrails=False,
        kind="restaurant",
        candidate=candidate,
    )

    assert out == ""


def test_audit_marks_seed_attraction_and_seed_survives_render_without_url() -> None:
    """Full-pipeline proof of the 'no usable link should drop a card unless it is
    a seed' policy for a real documented seed example (requirements.md §3.4 uses
    'The Narrows' as its seed example). A seed attraction with no discovered link
    and only a thin description must still be marked as a seed by the audit and
    must still render as a text-only card, not silently vanish."""
    from generator.html_assembler import HTMLAssembler

    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._max_trail_miles = 0

    trip = {
        "destinations": [
            {
                "id": "zion",
                "name": "Zion National Park",
                "seeds": ["The Narrows"],
                "ai_content": {
                    "top_attractions": [
                        {
                            "name": "The Narrows",
                            "type": "hike",
                            "description": "Iconic slot canyon hike.",
                            "url": "",
                        }
                    ],
                    "getting_here": {"en_route_stops": []},
                    "dinner_recommendations": [],
                },
                "scenic_drives": [],
                "cultural_events": {"events": []},
            }
        ]
    }

    with patch.object(discoverer, "_prewarm_url_validation_cache", return_value=None):
        discoverer.audit_discovered_urls(trip)

    ai = trip["destinations"][0]["ai_content"]
    attractions = ai["top_attractions"]
    assert len(attractions) == 1
    assert attractions[0]["name"] == "The Narrows"
    assert attractions[0].get("is_seed") is True

    assembler = HTMLAssembler.__new__(HTMLAssembler)
    html = assembler._build_attractions(ai, drives=[], dest_name="Zion National Park")
    assert "The Narrows" in html


def test_audit_seed_vs_nonseed_with_identical_thin_content_diverge_in_render() -> None:
    """Differential proof that the seed override actually discriminates: a seed
    and a non-seed attraction with byte-identical (thin, linkless, no-url)
    content must diverge. Only the seed is a documented user-requested anchor
    (requirements.md §3.4, 'Dark Sky Stargazing' is a listed experience-anchor
    example); under the verified-link-or-seed policy (2026-08-17) the non-seed
    twin is removed from `top_attractions` entirely at audit time (not just
    hidden at render time) so the seed override isn't accidentally a blanket
    bypass."""
    from generator.html_assembler import HTMLAssembler

    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._max_trail_miles = 0

    trip = {
        "destinations": [
            {
                "id": "zion",
                "name": "Zion National Park",
                "seeds": ["Dark Sky Stargazing"],
                "ai_content": {
                    "top_attractions": [
                        {"name": "Dark Sky Stargazing", "type": "activity", "description": "Great views.", "url": ""},
                        {"name": "Random Overlook", "type": "activity", "description": "Great views.", "url": ""},
                    ],
                    "getting_here": {"en_route_stops": []},
                    "dinner_recommendations": [],
                },
                "scenic_drives": [],
                "cultural_events": {"events": []},
            }
        ]
    }

    with patch.object(discoverer, "_prewarm_url_validation_cache", return_value=None):
        discoverer.audit_discovered_urls(trip)

    ai = trip["destinations"][0]["ai_content"]
    attractions = {a["name"]: a for a in ai["top_attractions"]}
    assert attractions["Dark Sky Stargazing"].get("is_seed") is True
    # Random Overlook is non-seed and never got a verified url -- removed
    # from top_attractions entirely, not merely present-with-empty-url.
    assert "Random Overlook" not in attractions

    assembler = HTMLAssembler.__new__(HTMLAssembler)
    html = assembler._build_attractions(ai, drives=[], dest_name="Zion National Park")
    assert "Dark Sky Stargazing" in html
    assert "Random Overlook" not in html


def test_audit_demotes_trail_when_description_distance_exceeds_threshold_hyphenated():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._max_trail_miles = 4.0

    trip = {
        "destinations": [
            {
                "id": "zion",
                "name": "Zion National Park",
                "ai_content": {
                    "top_attractions": [
                        {
                            "name": "Angels Landing",
                            "type": "hike",
                            "description": "A 5-mile hike with chain-assisted sections and exposure.",
                            "url": "https://www.alltrails.com/trail/us/utah/angels-landing-trail",
                        }
                    ],
                    "getting_here": {"en_route_stops": []},
                    "dinner_recommendations": [],
                },
                "scenic_drives": [],
                "cultural_events": {"events": []},
            }
        ]
    }

    with patch.object(discoverer, "_prewarm_url_validation_cache", return_value=None):
        discoverer.audit_discovered_urls(trip)

    # Policy (2026-08-17): non-seed, demoted with no verified link -- removed.
    attractions = trip["destinations"][0]["ai_content"]["top_attractions"]
    assert attractions == []


def test_audit_demotes_trail_when_fetched_page_distance_exceeds_threshold():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._max_trail_miles = 4.0

    trip = {
        "destinations": [
            {
                "id": "telluride",
                "name": "Telluride",
                "ai_content": {
                    "top_attractions": [
                        {
                            "name": "San Miguel River Trail",
                            "type": "hike",
                            "description": "Riverside trail with mountain views.",
                            "url": "https://www.alltrails.com/trail/us/colorado/san-miguel-river-trail",
                        }
                    ],
                    "getting_here": {"en_route_stops": []},
                    "dinner_recommendations": [],
                },
                "scenic_drives": [],
                "cultural_events": {"events": []},
            }
        ]
    }

    with patch.object(discoverer, "_prewarm_url_validation_cache", return_value=None):
        with patch.object(discoverer, "_fetch_page_text", return_value=(True, 200, "Length: 9.0 mi. Elevation gain 700 ft.")):
            discoverer.audit_discovered_urls(trip)

    # Policy (2026-08-17): non-seed, demoted with no verified link -- removed.
    attractions = trip["destinations"][0]["ai_content"]["top_attractions"]
    assert attractions == []



def test_audit_keeps_seed_trail_link_even_when_over_max_trail_miles() -> None:
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._max_trail_miles = 4.0

    trip = {
        "destinations": [
            {
                "id": "zion",
                "name": "Zion National Park",
                "seeds": ["Angels Landing"],
                "ai_content": {
                    "top_attractions": [
                        {
                            "name": "Angels Landing",
                            "type": "hike",
                            "description": "Length: 9.0 mi. Strenuous route.",
                            "url": "https://www.alltrails.com/trail/us/utah/angels-landing-trail",
                        }
                    ],
                    "getting_here": {"en_route_stops": []},
                    "dinner_recommendations": [],
                },
                "scenic_drives": [],
                "cultural_events": {"events": []},
            }
        ]
    }

    with patch.object(discoverer, "_prewarm_url_validation_cache", return_value=None):
        with patch.object(discoverer, "_fetch_page_text", return_value=(True, 200, "Length: 9.0 mi. Elevation gain 1500 ft.")):
            with patch.object(discoverer, "_retain_discovered_url", side_effect=lambda url, *_a, **_k: url):
                discoverer.audit_discovered_urls(trip)

    attraction = trip["destinations"][0]["ai_content"]["top_attractions"][0]
    assert attraction.get("type") == "hike"
    assert str(attraction.get("url", "") or "").startswith("https://www.alltrails.com/trail/")


def test_audit_seed_trail_over_threshold_prefers_nps_page_when_available() -> None:
    """Real reported example: "The Narrows" is seeded for Zion National Park and
    resolves to an AllTrails page for the ~19-mile top-down wilderness-permit
    route, which exceeds max_trail_miles. Since it's a seed the link must never
    be dropped -- but confidently pointing at the single most strenuous variant
    is misleading. When the destination has an NPS park code and a live nps.gov
    page can be found for the item, it should be preferred over the
    over-threshold AllTrails link."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._max_trail_miles = 4.0

    alltrails_url = "https://www.alltrails.com/trail/us/utah/the-narrows-top-down"
    nps_url = "https://www.nps.gov/zion/planyourvisit/thenarrows.htm"

    trip = {
        "destinations": [
            {
                "id": "zion",
                "name": "Zion National Park",
                "seeds": ["The Narrows"],
                "nps_park_code": "zion",
                "ai_content": {
                    "top_attractions": [
                        {
                            "name": "The Narrows",
                            "type": "hike",
                            "description": "Length: 19.0 mi. Strenuous top-down wilderness route requiring a permit.",
                            "url": alltrails_url,
                        }
                    ],
                    "getting_here": {"en_route_stops": []},
                    "dinner_recommendations": [],
                },
                "scenic_drives": [],
                "cultural_events": {"events": []},
            }
        ]
    }

    with patch.object(discoverer, "_prewarm_url_validation_cache", return_value=None):
        with patch.object(discoverer, "_search_first", return_value=nps_url) as mock_search_first:
            discoverer.audit_discovered_urls(trip)

    attraction = trip["destinations"][0]["ai_content"]["top_attractions"][0]
    assert attraction.get("url") == nps_url
    mock_search_first.assert_called_once()
    _, kwargs = mock_search_first.call_args
    assert kwargs.get("site_filter") == "nps.gov"
    assert kwargs.get("site_hint") == "site:nps.gov/zion"


def test_audit_seed_trail_over_threshold_falls_back_to_alltrails_when_no_nps_match() -> None:
    """Same over-threshold seed scenario as above, but the NPS-site-filtered
    search finds nothing live -- the pipeline must fall back to today's
    existing safety net and keep the original over-threshold AllTrails link
    rather than end up with no link at all for a seed."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._max_trail_miles = 4.0

    alltrails_url = "https://www.alltrails.com/trail/us/utah/the-narrows-top-down"

    trip = {
        "destinations": [
            {
                "id": "zion",
                "name": "Zion National Park",
                "seeds": ["The Narrows"],
                "nps_park_code": "zion",
                "ai_content": {
                    "top_attractions": [
                        {
                            "name": "The Narrows",
                            "type": "hike",
                            "description": "Length: 19.0 mi. Strenuous top-down wilderness route requiring a permit.",
                            "url": alltrails_url,
                        }
                    ],
                    "getting_here": {"en_route_stops": []},
                    "dinner_recommendations": [],
                },
                "scenic_drives": [],
                "cultural_events": {"events": []},
            }
        ]
    }

    with patch.object(discoverer, "_prewarm_url_validation_cache", return_value=None):
        with patch.object(discoverer, "_search_first", return_value=None) as mock_search_first:
            with patch.object(discoverer, "_retain_discovered_url", side_effect=lambda url, *_a, **_k: url):
                discoverer.audit_discovered_urls(trip)

    attraction = trip["destinations"][0]["ai_content"]["top_attractions"][0]
    assert attraction.get("url") == alltrails_url
    mock_search_first.assert_called_once()


def test_audit_validates_authoritative_restaurant_maps_place_url():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._direct_batch_authoritative = True
    discoverer._remember_direct_batch_authoritative_url(
        "https://www.google.com/maps/place/Oscar's+Cafe/@37.1647,-112.9994,17z",
        "Oscar's Cafe",
    )

    trip = {
        "destinations": [
            {
                "id": "zion",
                "name": "Zion National Park",
                "ai_content": {
                    "top_attractions": [],
                    "getting_here": {"en_route_stops": []},
                    "dinner_recommendations": [
                        {
                            "name": "Oscar's Cafe",
                            "url": "https://www.google.com/maps/place/Oscar's+Cafe/@37.1647,-112.9994,17z",
                        }
                    ],
                },
                "scenic_drives": [],
                "cultural_events": {"events": []},
            }
        ]
    }

    with patch.object(discoverer, "_prewarm_url_validation_cache", return_value=None):
        discoverer.audit_discovered_urls(trip)

    # Policy (2026-08-17): restaurants have no seed concept -- a restaurant
    # rejected down to no url is removed from dinner_recommendations entirely.
    restaurants = trip["destinations"][0]["ai_content"]["dinner_recommendations"]
    assert restaurants == []


def test_audit_validates_authoritative_attraction_url():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._direct_batch_authoritative = True
    bad_url = "https://www.alltrails.com/trail/us/utah/rim-trail-sunset-point-to-sunrise-point"
    discoverer._remember_direct_batch_authoritative_url(bad_url, "Sunrise Point")

    trip = {
        "destinations": [
            {
                "id": "bryce",
                "name": "Bryce Canyon National Park",
                "ai_content": {
                    "top_attractions": [
                        {
                            "name": "Sunrise Point",
                            "type": "hike",
                            "description": "Viewpoint trail segment.",
                            "url": bad_url,
                        }
                    ],
                    "getting_here": {"en_route_stops": []},
                    "dinner_recommendations": [],
                },
                "scenic_drives": [],
                "cultural_events": {"events": []},
            }
        ]
    }

    with patch.object(discoverer, "_prewarm_url_validation_cache", return_value=None):
        with patch.object(discoverer, "_retain_discovered_url", return_value="") as mock_retain:
            discoverer.audit_discovered_urls(trip)

    # Policy (2026-08-17): non-seed attraction rejected down to no url is
    # removed from top_attractions entirely.
    assert mock_retain.called
    attractions = trip["destinations"][0]["ai_content"]["top_attractions"]
    assert attractions == []


def test_audit_validates_authoritative_en_route_stop_url():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._direct_batch_authoritative = True
    bad_url = "https://www.google.com/maps/place/Red+Canyon+Visitor+Center,+UT"
    discoverer._remember_direct_batch_authoritative_url(bad_url, "Red Canyon")

    trip = {
        "destinations": [
            {
                "id": "bryce",
                "name": "Bryce Canyon National Park",
                "ai_content": {
                    "top_attractions": [],
                    "getting_here": {
                        "en_route_stops": [
                            {
                                "name": "Red Canyon",
                                "description": "Quick en-route red rock stop.",
                                "url": bad_url,
                            }
                        ]
                    },
                    "dinner_recommendations": [],
                },
                "scenic_drives": [],
                "cultural_events": {"events": []},
            }
        ]
    }

    with patch.object(discoverer, "_prewarm_url_validation_cache", return_value=None):
        with patch.object(discoverer, "_retain_discovered_url", return_value="") as mock_retain:
            discoverer.audit_discovered_urls(trip)

    # Policy (2026-08-17): non-seed en-route stop rejected down to no url is
    # removed from en_route_stops entirely.
    assert mock_retain.called
    stops = trip["destinations"][0]["ai_content"]["getting_here"]["en_route_stops"]
    assert stops == []


def test_item_has_verified_route_geocode_requires_waypoint_eligible_and_coords():
    """Unit coverage for the helper backing the en-route-stop geocode bar.

    Only the exact combination `_prune_en_route_stops_by_geometry` produces
    -- `route_waypoint_eligible is True` together with numeric
    `geocode_lat`/`geocode_lng` -- counts. Anything short of that (missing
    flag, False flag, missing/non-numeric coordinates) must not count, since
    those states mean geocoding never resolved a route-plausible point at
    all (see `_prune_en_route_stops_by_geometry`'s early-return branches,
    which set `route_waypoint_eligible = False` without ever touching
    `geocode_lat`/`geocode_lng`).
    """
    verified = {
        "name": "Corona Arch",
        "route_waypoint_eligible": True,
        "geocode_lat": 38.6136,
        "geocode_lng": -109.6890,
    }
    assert URLDiscoverer._item_has_verified_route_geocode(verified) is True

    missing_flag = {"name": "Corona Arch", "geocode_lat": 38.6136, "geocode_lng": -109.6890}
    assert URLDiscoverer._item_has_verified_route_geocode(missing_flag) is False

    ineligible = {
        "name": "Corona Arch",
        "route_waypoint_eligible": False,
        "geocode_lat": 38.6136,
        "geocode_lng": -109.6890,
    }
    assert URLDiscoverer._item_has_verified_route_geocode(ineligible) is False

    no_coords = {"name": "Corona Arch", "route_waypoint_eligible": True}
    assert URLDiscoverer._item_has_verified_route_geocode(no_coords) is False

    non_numeric_coords = {
        "name": "Corona Arch",
        "route_waypoint_eligible": True,
        "geocode_lat": None,
        "geocode_lng": None,
    }
    assert URLDiscoverer._item_has_verified_route_geocode(non_numeric_coords) is False


def test_audit_keeps_en_route_stop_with_verified_route_geocode_despite_no_url():
    """Regression grounded in real SW2026-dipstick67 removals.

    Under the strict verified-link-or-seed policy, a production run removed
    68 of 77 en-route stops (~88%) trip-wide -- a wildly disproportionate
    rate next to attractions (~37%) and restaurants (~8%). Manual
    spot-checks of the removed names (this test uses "Corona Arch" and
    "Dead Horse Point State Park Overlook", both real dipstick67 Canyonlands
    en-route-stop removals) confirmed they are real, correctly located
    places -- Nominatim geocodes them cleanly, and the pipeline had already
    computed and vetted that same geocode via
    `_prune_en_route_stops_by_geometry` (`route_waypoint_eligible=True`,
    `geocode_lat`/`geocode_lng` set) before the stricter URL-only bar threw
    that evidence away and deleted the stop.

    A stop carrying that already-verified geocode must survive audit even
    with no source URL at all -- distinct from (and not weaker than) a
    generic maps-search fallback, since a real Nominatim resolution
    filtered through route-geometry plausibility checks is independent,
    externally checkable evidence the place exists at this specific
    location, not just a best-guess query string.
    """
    discoverer = URLDiscoverer.__new__(URLDiscoverer)

    trip = {
        "destinations": [
            {
                "id": "canyonlands",
                "name": "Canyonlands National Park",
                "ai_content": {
                    "top_attractions": [],
                    "getting_here": {
                        "en_route_stops": [
                            {
                                "name": "Corona Arch",
                                "description": "A dramatic freestanding sandstone arch just off the highway.",
                                "route_waypoint_eligible": True,
                                "geocode_lat": 38.6136,
                                "geocode_lng": -109.6890,
                            },
                            {
                                "name": "Dead Horse Point State Park Overlook",
                                "description": "Sweeping canyon-rim overlook near the highway turnoff.",
                                "route_waypoint_eligible": True,
                                "geocode_lat": 38.4838,
                                "geocode_lng": -109.7379,
                            },
                        ]
                    },
                    "dinner_recommendations": [],
                },
                "scenic_drives": [],
                "cultural_events": {"events": []},
            }
        ]
    }

    with patch.object(discoverer, "_prewarm_url_validation_cache", return_value=None):
        with patch.object(discoverer, "_retain_discovered_url", return_value=""):
            discoverer.audit_discovered_urls(trip)

    stops = trip["destinations"][0]["ai_content"]["getting_here"]["en_route_stops"]
    kept_names = {stop["name"] for stop in stops}
    assert kept_names == {"Corona Arch", "Dead Horse Point State Park Overlook"}
    # No removal should have been recorded in the registry for either stop.
    removed_names = {
        decision["display_name"]
        for decision in trip["destinations"][0].get("_registry_decisions", [])
        if decision.get("rejection_reasons") == ["no_verified_url_removed"]
    }
    assert removed_names == set()


def test_audit_still_removes_en_route_stop_without_geocode_or_url():
    """Companion to the geocode-keep regression above: a non-seed en-route
    stop with no source URL AND no verified route geocode (the ordinary
    "we truly have no evidence" case -- e.g. geocoding never ran, or
    genuinely found nothing plausible) must still be removed under the
    verified-link-or-seed policy. The new geocode bar is additive, not a
    blanket loosening of en-route-stop verification.
    """
    discoverer = URLDiscoverer.__new__(URLDiscoverer)

    trip = {
        "destinations": [
            {
                "id": "canyonlands",
                "name": "Canyonlands National Park",
                "ai_content": {
                    "top_attractions": [],
                    "getting_here": {
                        "en_route_stops": [
                            {
                                "name": "Some Unresolved Stop",
                                "description": "No geocode, no url.",
                            }
                        ]
                    },
                    "dinner_recommendations": [],
                },
                "scenic_drives": [],
                "cultural_events": {"events": []},
            }
        ]
    }

    with patch.object(discoverer, "_prewarm_url_validation_cache", return_value=None):
        with patch.object(discoverer, "_retain_discovered_url", return_value=""):
            discoverer.audit_discovered_urls(trip)

    stops = trip["destinations"][0]["ai_content"]["getting_here"]["en_route_stops"]
    assert stops == []


def test_audit_attraction_with_route_geocode_like_fields_still_removed_without_url():
    """The new geocode-based bar is en-route-stop-specific (passed via
    `extra_verified` only at the en-route-stops call site in
    `audit_discovered_urls`). An attraction is never given that override,
    so even if it happened to carry the same-shaped
    `route_waypoint_eligible`/`geocode_lat`/`geocode_lng` fields (which
    nothing in the attraction pipeline actually sets -- this is a defensive
    check, not a realistic data shape), it must still be removed when it
    has no seed status and no verified URL. Attractions keep the strict
    real-page-or-seed bar untouched.
    """
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._max_trail_miles = 0

    trip = {
        "destinations": [
            {
                "id": "canyonlands",
                "name": "Canyonlands National Park",
                "ai_content": {
                    "top_attractions": [
                        {
                            "name": "Mesa Arch",
                            "type": "attraction",
                            "description": "A short, easy walk to a famous arch overlook.",
                            "route_waypoint_eligible": True,
                            "geocode_lat": 38.3897,
                            "geocode_lng": -109.8683,
                        }
                    ],
                    "getting_here": {"en_route_stops": []},
                    "dinner_recommendations": [],
                },
                "scenic_drives": [],
                "cultural_events": {"events": []},
            }
        ]
    }

    with patch.object(discoverer, "_prewarm_url_validation_cache", return_value=None):
        discoverer.audit_discovered_urls(trip)

    assert trip["destinations"][0]["ai_content"]["top_attractions"] == []


def test_audit_restaurant_with_route_geocode_like_fields_still_removed_without_url():
    """Same isolation check as the attraction test above, for restaurants:
    restaurants have no seed concept and no `extra_verified` override at
    their `audit_discovered_urls` call site, so geocode-shaped fields (not
    something the restaurant pipeline actually sets) must not grant a pass.
    """
    discoverer = URLDiscoverer.__new__(URLDiscoverer)

    trip = {
        "destinations": [
            {
                "id": "moab",
                "name": "Moab",
                "ai_content": {
                    "top_attractions": [],
                    "getting_here": {"en_route_stops": []},
                    "dinner_recommendations": [
                        {
                            "name": "Some Cafe",
                            "description": "A local spot.",
                            "route_waypoint_eligible": True,
                            "geocode_lat": 38.5733,
                            "geocode_lng": -109.5498,
                        }
                    ],
                },
                "scenic_drives": [],
                "cultural_events": {"events": []},
            }
        ]
    }

    with patch.object(discoverer, "_prewarm_url_validation_cache", return_value=None):
        with patch.object(discoverer, "_is_restaurant_ineligible", return_value=False):
            discoverer.audit_discovered_urls(trip)

    assert trip["destinations"][0]["ai_content"]["dinner_recommendations"] == []


def test_retain_discovered_url_rejects_incomplete_google_maps_place_link():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)

    with patch.object(discoverer, "_is_relevant_result", return_value=True):
        out = discoverer._retain_discovered_url(
            "https://www.google.com/maps/place/Fremont+Indian+State+Park+Museum",
            "Fremont Indian State Park",
            "Capitol Reef National Park",
            allow_alltrails=False,
            kind="attraction",
        )

    assert out == ""


def test_retain_discovered_url_rejects_unverified_google_maps_place_link_for_attraction():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)

    with patch.object(discoverer, "_fetch_page_text", return_value=(False, 403, "")):
        out = discoverer._retain_discovered_url(
            "https://www.google.com/maps/place/Zion+Human+History+Museum/@37.2001,-112.9864,17z/data=!3m1!4b1!4m6!3m5!1s0x80ca50e0f1a2b3c4:0x1e2f3a4b5c6d7e8f!8m2!3d37.2001!4d-112.9864!16zL20vMGZqM3B6",
            "Zion Human History Museum",
            "Zion National Park",
            allow_alltrails=False,
            kind="attraction",
        )

    assert out == ""


def test_retain_discovered_url_allows_verified_google_maps_place_link_for_attraction():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)

    with patch.object(
        discoverer,
        "_fetch_page_text",
        return_value=(True, 200, "Zion Human History Museum exhibits and visitor information"),
    ):
        out = discoverer._retain_discovered_url(
            "https://www.google.com/maps/place/Zion+Human+History+Museum/@37.2001,-112.9864,17z/data=!3m1!4b1!4m6!3m5!1s0x80ca50e0f1a2b3c4:0x1e2f3a4b5c6d7e8f!8m2!3d37.2001!4d-112.9864!16zL20vMGZqM3B6",
            "Zion Human History Museum",
            "Zion National Park",
            allow_alltrails=False,
            kind="attraction",
        )

    assert out.startswith("https://www.google.com/maps/place/Zion+Human+History+Museum/")


def test_retain_discovered_url_preserves_seed_item_with_single_token_match() -> None:
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    candidate = {"name": "Sunrise Point", "title": "Sunrise Point", "description": "Viewpoint overlooking Bryce Canyon"}

    out = discoverer._retain_discovered_url(
        "https://www.alltrails.com/trail/us/utah/sunrise-point",
        "Sunrise Point",
        "Bryce Canyon National Park",
        allow_alltrails=True,
        kind="attraction",
        candidate=candidate,
        allow_google_maps_search=True,
    )

    assert out == "https://www.alltrails.com/trail/us/utah/sunrise-point"


def test_retain_discovered_url_rejects_deterministic_maps_place_with_only_generic_overlap() -> None:
    discoverer = URLDiscoverer.__new__(URLDiscoverer)

    with patch.object(
        discoverer,
        "_fetch_page_text",
        return_value=(True, 200, "Pagosa Springs Historic District visitor information and map"),
    ):
        out = discoverer._retain_discovered_url(
            "https://www.google.com/maps/place/Pagosa+Springs+Historic+District/@37.2694,-107.0098,15z",
            "Telluride Historic District",
            "Telluride",
            allow_alltrails=False,
            kind="attraction",
        )

    assert out == ""


def test_audit_removes_uninterested_attractions_from_top_list():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._uninterested_keywords = ("golf club", "bike trail")
    discoverer._seasonal_ski_keywords = (" ski",)
    discoverer._ski_in_season_months = (11, 12, 1, 2, 3, 4)

    trip = {
        "destinations": [
            {
                "id": "stgeorge",
                "name": "St. George, Utah",
                "dates": "October 18-20, 2026",
                "ai_content": {
                    "top_attractions": [
                        {
                            "name": "Dixie Red Hills Golf Club",
                            "type": "attraction",
                            "description": "Golf facility with scenic fairways.",
                            "url": "https://www.google.com/maps/search/?api=1&query=Dixie+Red+Hills+Golf+Club",
                        },
                        {
                            "name": "Bear Claw Poppy Trail",
                            "type": "attraction",
                            "description": "Popular bike trail with rolling desert terrain.",
                            "url": "https://www.google.com/maps/search/?api=1&query=Bear+Claw+Poppy+Trail",
                        },
                        {
                            "name": "Snow Canyon State Park",
                            "type": "attraction",
                            "description": "Red rock park with scenic overlooks.",
                            # A real, specific source url (not a maps-search
                            # fallback) so this survives the verified-link-or-
                            # seed policy independently of the interest filter
                            # this test is actually exercising.
                            "url": "https://stateparks.utah.gov/parks/snow-canyon/",
                        },
                    ],
                    "getting_here": {"en_route_stops": []},
                    "dinner_recommendations": [],
                },
                "scenic_drives": [],
                "cultural_events": {"events": []},
            }
        ]
    }

    with patch.object(discoverer, "_prewarm_url_validation_cache", return_value=None):
        discoverer.audit_discovered_urls(trip)

    attractions = trip["destinations"][0]["ai_content"]["top_attractions"]
    names = [str(row.get("name", "") or "") for row in attractions]
    assert "Dixie Red Hills Golf Club" not in names
    assert "Bear Claw Poppy Trail" not in names
    assert "Snow Canyon State Park" in names
    decisions = trip["destinations"][0].get("_registry_decisions", [])
    assert any("interest_filter_removed" in (d.get("rejection_reasons", []) or []) for d in decisions)


def test_search_strict_rejects_alltrails_soft_404_and_falls_back_to_none():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._search = MagicMock()
    discoverer._url_validator = MagicMock()
    discoverer._url_validator.session.get.return_value = MagicMock(
        status_code=200,
        text="404 We've reached the end of the trail. The page you're looking for either doesn't exist or has a new link.",
    )
    discoverer._search.search.return_value = [
        {"url": "https://www.alltrails.com/trail/us/utah/st-george-dinosaur-discovery-site"}
    ]

    result = discoverer._search_first_strict(
        query_variants=['"St. George Dinosaur Discovery Site" St. George Utah trail hiking'],
        site_filter="alltrails.com",
        site_hint=None,
        item_name="St. George Dinosaur Discovery Site",
        dest_name="St. George, Utah",
        allow_alltrails=True,
    )

    assert result is None


def test_alltrails_relevance_rejects_closed_trail_page_text():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._url_validator = MagicMock()

    with patch.object(
        discoverer,
        "_fetch_page_text",
        return_value=(True, 200, "Navajo Loop Trail is temporarily closed due to trail maintenance"),
    ):
        ok = discoverer._is_relevant_result(
            "https://www.alltrails.com/trail/us/utah/navajo-loop-trail",
            "Navajo Loop Trail",
            "Bryce Canyon National Park",
            candidate={
                "name": "Navajo Loop Trail",
                "snippet": "Popular Bryce trail",
            },
        )

    assert ok is False


def test_alltrails_relevance_rejects_closed_trail_candidate_snippet_when_fetch_blocked():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._allow_blocked_alltrails = True
    discoverer._url_validator = MagicMock()

    with patch.object(discoverer, "_fetch_page_text", return_value=(False, 403, "")):
        ok = discoverer._is_relevant_result(
            "https://www.alltrails.com/trail/us/utah/navajo-loop-trail",
            "Navajo Loop Trail",
            "Bryce Canyon National Park",
            candidate={
                "name": "Navajo Loop Trail",
                "snippet": "This trail is closed for restoration work",
            },
        )

    assert ok is False


def test_has_alltrails_closure_marker_ignores_html_comment_noise():
    """Regression for the same false-positive class as
    test_has_attraction_closure_marker_ignores_html_comment_noise: AllTrails
    trail pages are fetched via the identical raw-HTML path
    (_fetch_alltrails_text -> _fetch_page_text_uncached ->
    URLValidator.get_text -> unmodified resp.text), so a closure phrase left
    in a non-visible HTML comment (e.g. stale dev/CMS markup) must not be
    read as evidence the trail itself is closed.
    """
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    page_text = (
        "<html><head></head><body>"
        "<h1>Navajo Loop Trail</h1>"
        "<p>A popular 1.3 mile loop trail near Bryce Canyon City, Utah.</p>"
        "<!-- Note for editors: the connector spur to Tunnel View was "
        "temporarily closed due to rockfall last season, now reopened. -->"
        "<p>Rated moderate. Dogs not allowed.</p>"
        "</body></html>"
    )
    assert discoverer._has_alltrails_closure_marker(page_text) is False


def test_has_alltrails_closure_marker_ignores_script_json_noise():
    """Same false-positive class as the HTML-comment case above, but for
    <script> content: AllTrails is a React/Next.js site, so its raw
    (unrendered) HTML is unusually likely to carry inline JSON hydration
    state -- e.g. data for a "nearby trails" widget -- that can mention an
    unrelated trail's closure without the page's own trail being closed.
    """
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    page_text = (
        "<html><head>"
        "<script>window.__NEXT_DATA__ = {\"nearbyTrails\": ["
        "{\"name\": \"Peekaboo Loop Trail\", \"status\": \"trail is closed\"}"
        "]};</script>"
        "</head><body>"
        "<h1>Navajo Loop Trail</h1>"
        "<p>A popular 1.3 mile loop trail near Bryce Canyon City, Utah.</p>"
        "</body></html>"
    )
    assert discoverer._has_alltrails_closure_marker(page_text) is False


def test_has_alltrails_closure_marker_still_detects_real_full_closure():
    """Control for the comment/script-stripping fix: a real, visible closure
    notice with no comment/script noise involved must still be detected."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    assert discoverer._has_alltrails_closure_marker(
        "Navajo Loop Trail. This trail is closed due to rockfall damage."
    ) is True


def test_extract_alltrails_geo_from_html_parses_real_json_ld_shape():
    """Regression grounding: shape verified live against a real, currently-
    served AllTrails trail page (Hickman Bridge Trail, Capitol Reef,
    fetched via a real browser session on 2026-08-18). The page carries
    three ld+json blocks (WebPage, LocalBusiness, BreadcrumbList) -- only
    the LocalBusiness one has `geo` -- and AllTrails serializes
    latitude/longitude as JSON *strings*, not numbers, which this must
    float()-cast rather than reject."""
    page_html = """
    <html><head>
    <script type="application/ld+json">{"@context":"https://schema.org","@type":"WebPage","isAccessibleForFree":false}</script>
    <script type="application/ld+json">{"@context":"https://schema.org","@type":"LocalBusiness","@id":"/trail/us/utah/hickman-bridge-trail","address":{"@type":"PostalAddress","addressLocality":"Torrey, Utah, United States"},"geo":{"@type":"GeoCoordinates","latitude":"38.28876","longitude":"-111.22765"},"name":"Hickman Bridge Trail","aggregateRating":{"@type":"AggregateRating","ratingValue":4.7,"reviewCount":10913}}</script>
    <script type="application/ld+json">{"@context":"https://schema.org","@type":"BreadcrumbList","itemListElement":[{"@type":"ListItem","position":"1","name":"United States"}]}</script>
    </head><body><h1>Hickman Bridge Trail</h1></body></html>
    """
    coords = URLDiscoverer._extract_alltrails_geo_from_html(page_html)
    assert coords == (38.28876, -111.22765)


def test_extract_alltrails_geo_from_html_returns_none_without_geo_field():
    """No ld+json block on the page carries a `geo` field (e.g. a trail page
    whose LocalBusiness block was stripped or never rendered) -- must return
    None rather than fabricate a coordinate, and must not crash."""
    page_html = """
    <script type="application/ld+json">{"@context":"https://schema.org","@type":"WebPage"}</script>
    <script type="application/ld+json">{"@context":"https://schema.org","@type":"BreadcrumbList","itemListElement":[]}</script>
    """
    assert URLDiscoverer._extract_alltrails_geo_from_html(page_html) is None


def test_extract_alltrails_geo_from_html_returns_none_on_malformed_json():
    """A truncated/invalid JSON-LD blob (bot-block interstitial, partial
    fetch, hand-edited test fixture, etc.) must be skipped, not raise."""
    page_html = '<script type="application/ld+json">{"@type": "LocalBusiness", "geo": {not valid json</script>'
    assert URLDiscoverer._extract_alltrails_geo_from_html(page_html) is None


def test_extract_alltrails_geo_from_html_returns_none_for_out_of_range_coordinate():
    """Defensive range check: a malformed geo block that still happens to
    parse as numbers (e.g. corrupted upstream data) must not be trusted if
    the values are outside real lat/lng bounds or sit at null-island (0,0)."""
    page_html = (
        '<script type="application/ld+json">'
        '{"@type": "LocalBusiness", "geo": {"@type": "GeoCoordinates", '
        '"latitude": "0", "longitude": "0"}}'
        "</script>"
    )
    assert URLDiscoverer._extract_alltrails_geo_from_html(page_html) is None


def test_extract_alltrails_geo_from_html_returns_none_for_empty_html():
    assert URLDiscoverer._extract_alltrails_geo_from_html("") is None


def test_alltrails_geo_maps_url_builds_coordinate_link_from_fetched_page():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    page_html = (
        '<script type="application/ld+json">'
        '{"@type": "LocalBusiness", "geo": {"@type": "GeoCoordinates", '
        '"latitude": "38.28876", "longitude": "-111.22765"}}'
        "</script>"
    )
    with patch.object(discoverer, "_fetch_page_text", return_value=(True, 200, page_html)):
        result = discoverer._alltrails_geo_maps_url(
            "https://www.alltrails.com/trail/us/utah/hickman-bridge-trail"
        )
    assert result == "https://www.google.com/maps/search/?api=1&query=38.28876,-111.22765"


def test_alltrails_geo_maps_url_returns_none_when_fetch_and_wayback_both_fail():
    """Direct fetch blocked (e.g. DataDome 403) AND no Wayback snapshot
    available (or that lookup also fails) -- must fail closed, not crash."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    with patch.object(discoverer, "_fetch_page_text", return_value=(False, 403, "")), patch.object(
        discoverer, "_fetch_wayback_alltrails_text", return_value=(False, "no_snapshot", "")
    ):
        result = discoverer._alltrails_geo_maps_url(
            "https://www.alltrails.com/trail/us/utah/hickman-bridge-trail"
        )
    assert result is None


def test_alltrails_geo_maps_url_returns_none_for_non_alltrails_url():
    """This feature must never touch non-AllTrails URLs at all -- assert
    _fetch_page_text is never even called for one."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    with patch.object(discoverer, "_fetch_page_text") as mock_fetch:
        result = discoverer._alltrails_geo_maps_url("https://www.nps.gov/zion/index.htm")
    assert result is None
    mock_fetch.assert_not_called()


def test_alltrails_geo_maps_url_falls_back_to_wayback_when_direct_fetch_blocked():
    """The production-critical case (dipstick71: 0/19 fires with no
    fallback): a bot-blocked direct fetch (403, empty body) must not be the
    end of the line -- _alltrails_geo_maps_url must try the Wayback Machine
    fallback and, if that archived page carries the same JSON-LD geo block,
    still produce a real coordinate maps_url."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    archived_html = (
        '<script type="application/ld+json">'
        '{"@type": "LocalBusiness", "geo": {"@type": "GeoCoordinates", '
        '"latitude": "38.28876", "longitude": "-111.22765"}}'
        "</script>"
    )
    with patch.object(discoverer, "_fetch_page_text", return_value=(False, 403, "")) as mock_direct, patch.object(
        discoverer, "_fetch_wayback_alltrails_text", return_value=(True, 200, archived_html)
    ) as mock_wayback:
        result = discoverer._alltrails_geo_maps_url(
            "https://www.alltrails.com/trail/us/utah/hickman-bridge-trail"
        )
    assert result == "https://www.google.com/maps/search/?api=1&query=38.28876,-111.22765"
    mock_direct.assert_called_once()
    mock_wayback.assert_called_once()


def test_alltrails_geo_maps_url_does_not_call_wayback_when_direct_fetch_succeeds():
    """Avoid an unnecessary network call: when the direct fetch already
    works (rare in production today, but the primary path is kept for the
    cases it does), the Wayback fallback must never even be invoked."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    page_html = (
        '<script type="application/ld+json">'
        '{"@type": "LocalBusiness", "geo": {"@type": "GeoCoordinates", '
        '"latitude": "38.28876", "longitude": "-111.22765"}}'
        "</script>"
    )
    with patch.object(discoverer, "_fetch_page_text", return_value=(True, 200, page_html)), patch.object(
        discoverer, "_fetch_wayback_alltrails_text"
    ) as mock_wayback:
        result = discoverer._alltrails_geo_maps_url(
            "https://www.alltrails.com/trail/us/utah/hickman-bridge-trail"
        )
    assert result == "https://www.google.com/maps/search/?api=1&query=38.28876,-111.22765"
    mock_wayback.assert_not_called()


def test_fetch_wayback_alltrails_text_success_shape():
    """Grounded in the real Wayback CDX Server API response shape (verified
    live 2026-08-18 for alltrails.com/trail/us/utah/mesa-arch:
    `https://web.archive.org/cdx/search/cdx?url=...&output=json&filter=
    statuscode:200&limit=-5` returns a JSON array whose first row is the
    column header and whose LAST row (limit=-N returns oldest-first) is the
    most recent statuscode:200 snapshot) and a real recent (2026-01-08)
    archived snapshot's JSON-LD geo block (american-samoa/tutuila/
    lower-sauma-ridge-trail) -- mocks both the CDX call and the
    archived-snapshot HTML fetch.

    dipstick72 root cause (2026-08-18): this used to call the single-URL
    `archive.org/wayback/available` helper API, which was live-reproduced
    429ing under ordinary production request volume (all 20/20 real trail
    lookups from a real run got 429, and isolated retries kept 429ing for
    60+ seconds) while the CDX API used here, live-reproduced in the same
    window against the same real trail URLs, kept returning real 200s --
    a separate, more generous host/quota. See
    _fetch_wayback_alltrails_text's docstring for the full writeup."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    trail_url = "https://www.alltrails.com/trail/us/utah/hickman-bridge-trail"
    snapshot_url = (
        "https://web.archive.org/web/20260108000000/"
        "https://www.alltrails.com/trail/us/utah/hickman-bridge-trail"
    )
    cdx_body = json.dumps(
        [
            ["urlkey", "timestamp", "original", "mimetype", "statuscode", "digest", "length"],
            [
                "com,alltrails)/trail/us/utah/hickman-bridge-trail",
                "20230710204311",
                "https://www.alltrails.com/trail/us/utah/hickman-bridge-trail",
                "text/html",
                "200",
                "OLDDIGEST",
                "36694",
            ],
            [
                "com,alltrails)/trail/us/utah/hickman-bridge-trail",
                "20260108000000",
                "https://www.alltrails.com/trail/us/utah/hickman-bridge-trail",
                "text/html",
                "200",
                "NEWDIGEST",
                "152257",
            ],
        ]
    )
    archived_html = (
        '<script type="application/ld+json">'
        '{"@type": "LocalBusiness", "geo": {"@type": "GeoCoordinates", '
        '"latitude": "38.28876", "longitude": "-111.22765"}}'
        "</script>"
    )

    def fake_fetch_uncached(url, timeout=8):
        if "web.archive.org/cdx/search/cdx" in url:
            return (True, 200, cdx_body)
        if url == snapshot_url:
            return (True, 200, archived_html)
        raise AssertionError(f"unexpected URL fetched: {url}")

    with patch.object(discoverer, "_fetch_page_text_uncached", side_effect=fake_fetch_uncached):
        ok, status, text = discoverer._fetch_wayback_alltrails_text(trail_url, timeout=8)
    assert ok is True
    assert status == 200
    coords = URLDiscoverer._extract_alltrails_geo_from_html(text)
    assert coords == (38.28876, -111.22765)

    # Second call for the same URL must be a cache hit, not a second lookup.
    with patch.object(discoverer, "_fetch_page_text_uncached", side_effect=fake_fetch_uncached) as mock_fetch:
        discoverer._fetch_wayback_alltrails_text(trail_url, timeout=8)
    mock_fetch.assert_not_called()


def test_fetch_wayback_alltrails_text_no_snapshot_available():
    """A trail never archived by the Wayback Machine -- the real CDX API
    shape for "no snapshot" is a JSON array with only the header row (or an
    empty array). Must return a graceful empty result, not raise."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    trail_url = "https://www.alltrails.com/trail/us/nowhere/never-archived-trail"
    cdx_body = json.dumps([["urlkey", "timestamp", "original", "mimetype", "statuscode", "digest", "length"]])

    with patch.object(
        discoverer, "_fetch_page_text_uncached", return_value=(True, 200, cdx_body)
    ) as mock_fetch:
        ok, status, text = discoverer._fetch_wayback_alltrails_text(trail_url, timeout=8)
    assert ok is False
    assert text == ""
    # Only the CDX lookup should have fired -- no snapshot URL to fetch.
    mock_fetch.assert_called_once()


def test_fetch_wayback_alltrails_text_cdx_lookup_failure():
    """archive.org itself unreachable/erroring with a non-transient error --
    must fail closed, not crash, and not fabricate a snapshot URL."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    trail_url = "https://www.alltrails.com/trail/us/utah/hickman-bridge-trail"
    with patch.object(discoverer, "_fetch_page_text_uncached", return_value=(False, "invalid_url", "")):
        ok, status, text = discoverer._fetch_wayback_alltrails_text(trail_url, timeout=8)
    assert ok is False
    assert text == ""


def test_fetch_wayback_alltrails_text_retries_once_on_transient_429_then_succeeds():
    """dipstick72's real production failure mode, reproduced live 2026-08-18:
    archive.org's wayback endpoints intermittently 429 (or read-timeout) for
    a matter of seconds and then serve a normal 200 on the very next
    attempt. A single retry after a short pause turns that transient blip
    into a success instead of a silent, permanent miss for that item."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    trail_url = "https://www.alltrails.com/trail/us/utah/mesa-arch"
    snapshot_url = "https://web.archive.org/web/20251119122301/https://www.alltrails.com/trail/us/utah/mesa-arch"
    cdx_body = json.dumps(
        [
            ["urlkey", "timestamp", "original", "mimetype", "statuscode", "digest", "length"],
            [
                "com,alltrails)/trail/us/utah/mesa-arch",
                "20251119122301",
                "https://www.alltrails.com/trail/us/utah/mesa-arch",
                "text/html",
                "200",
                "DIGEST",
                "152149",
            ],
        ]
    )
    archived_html = (
        '<script type="application/ld+json">'
        '{"@type": "LocalBusiness", "geo": {"@type": "GeoCoordinates", '
        '"latitude": "38.38909", "longitude": "-109.86796"}}'
        "</script>"
    )

    call_count = {"cdx": 0}

    def fake_fetch_uncached(url, timeout=8):
        if "web.archive.org/cdx/search/cdx" in url:
            call_count["cdx"] += 1
            if call_count["cdx"] == 1:
                return (False, 429, "")
            return (True, 200, cdx_body)
        if url == snapshot_url:
            return (True, 200, archived_html)
        raise AssertionError(f"unexpected URL fetched: {url}")

    with patch.object(discoverer, "_fetch_page_text_uncached", side_effect=fake_fetch_uncached), patch(
        "generator.url_discovery.time.sleep"
    ):
        ok, status, text = discoverer._fetch_wayback_alltrails_text(trail_url, timeout=8)
    assert ok is True
    coords = URLDiscoverer._extract_alltrails_geo_from_html(text)
    assert coords == (38.38909, -109.86796)
    assert call_count["cdx"] == 2


def test_audit_attaches_alltrails_geo_coordinate_maps_url_for_accepted_trail() -> None:
    """End-to-end: an AllTrails trail URL that clears audit_discovered_urls'
    acceptance gates unchanged gets a real coordinate-based maps_url
    attached from its own page's JSON-LD geo field, so the existing
    _maps_corner_link_html render affordance (html_assembler.py) has a
    genuinely distinct link to show -- previously this maps_url was either
    absent or equal to the AllTrails url itself, so the map icon never
    rendered for a trail card at all."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._max_trail_miles = 0
    discoverer._direct_batch_authoritative = True
    discoverer._remember_direct_batch_authoritative_url(
        "https://www.alltrails.com/trail/us/utah/hickman-bridge-trail",
        "Hickman Bridge Trail",
    )

    alltrails_page_html = (
        '<script type="application/ld+json">{"@type":"WebPage"}</script>'
        '<script type="application/ld+json">'
        '{"@type": "LocalBusiness", "name": "Hickman Bridge Trail", '
        '"geo": {"@type": "GeoCoordinates", "latitude": "38.28876", "longitude": "-111.22765"}}'
        "</script>"
    )

    trip = {
        "destinations": [
            {
                "id": "capitol-reef",
                "name": "Capitol Reef National Park",
                "ai_content": {
                    "top_attractions": [
                        {
                            "name": "Hickman Bridge Trail",
                            "type": "hike",
                            "description": "A popular natural bridge hike.",
                            "url": "https://www.alltrails.com/trail/us/utah/hickman-bridge-trail",
                        }
                    ],
                    "getting_here": {"en_route_stops": []},
                    "dinner_recommendations": [],
                },
                "scenic_drives": [],
                "cultural_events": {"events": []},
            }
        ]
    }

    with patch.object(discoverer, "_prewarm_url_validation_cache", return_value=None):
        with patch.object(discoverer, "_fetch_page_text", return_value=(True, 200, alltrails_page_html)):
            discoverer.audit_discovered_urls(trip)

    attractions = trip["destinations"][0]["ai_content"]["top_attractions"]
    assert len(attractions) == 1
    attr = attractions[0]
    assert attr["url"] == "https://www.alltrails.com/trail/us/utah/hickman-bridge-trail"
    assert attr["maps_url"] == "https://www.google.com/maps/search/?api=1&query=38.28876,-111.22765"


def test_audit_leaves_maps_url_untouched_when_alltrails_geo_extraction_fails() -> None:
    """No JSON-LD geo on the fetched page (e.g. bot-blocked interstitial) --
    the trail URL is still accepted and kept, but maps_url must be left
    exactly as whatever the pre-existing behavior already produced (absent
    here), never a fabricated/estimated link."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._max_trail_miles = 0
    discoverer._direct_batch_authoritative = True
    discoverer._remember_direct_batch_authoritative_url(
        "https://www.alltrails.com/trail/us/utah/hickman-bridge-trail",
        "Hickman Bridge Trail",
    )

    trip = {
        "destinations": [
            {
                "id": "capitol-reef",
                "name": "Capitol Reef National Park",
                "ai_content": {
                    "top_attractions": [
                        {
                            "name": "Hickman Bridge Trail",
                            "type": "hike",
                            "description": "A popular natural bridge hike.",
                            "url": "https://www.alltrails.com/trail/us/utah/hickman-bridge-trail",
                        }
                    ],
                    "getting_here": {"en_route_stops": []},
                    "dinner_recommendations": [],
                },
                "scenic_drives": [],
                "cultural_events": {"events": []},
            }
        ]
    }

    with patch.object(discoverer, "_prewarm_url_validation_cache", return_value=None):
        with patch.object(discoverer, "_fetch_page_text", return_value=(False, 403, "")):
            discoverer.audit_discovered_urls(trip)

    attractions = trip["destinations"][0]["ai_content"]["top_attractions"]
    assert len(attractions) == 1
    attr = attractions[0]
    assert attr["url"] == "https://www.alltrails.com/trail/us/utah/hickman-bridge-trail"
    assert "maps_url" not in attr


def test_audit_does_not_apply_alltrails_geo_maps_url_to_non_alltrails_attraction() -> None:
    """This feature is AllTrails-specific: a non-trail, non-AllTrails
    attraction that already carries its own maps_url (e.g. a plain
    google-maps-search fallback from upstream discovery) must be completely
    unaffected. (Note: a *trail-like* item -- name/type implying a hike --
    with a non-AllTrails URL is rejected by an unrelated, pre-existing audit
    rule regardless of this feature, so this case deliberately uses a plain
    museum/visitor-center attraction to isolate the behavior under test.)"""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._max_trail_miles = 0
    discoverer._direct_batch_authoritative = True
    discoverer._remember_direct_batch_authoritative_url(
        "https://www.nps.gov/zion/planyourvisit/human-history-museum.htm",
        "Zion Human History Museum",
    )

    trip = {
        "destinations": [
            {
                "id": "zion",
                "name": "Zion National Park",
                "ai_content": {
                    "top_attractions": [
                        {
                            "name": "Zion Human History Museum",
                            "type": "attraction",
                            "description": "Exhibits on the human history of Zion Canyon.",
                            "url": "https://www.nps.gov/zion/planyourvisit/human-history-museum.htm",
                            "maps_url": "https://www.google.com/maps/search/?api=1&query=Zion+Human+History+Museum",
                        }
                    ],
                    "getting_here": {"en_route_stops": []},
                    "dinner_recommendations": [],
                },
                "scenic_drives": [],
                "cultural_events": {"events": []},
            }
        ]
    }

    with patch.object(discoverer, "_prewarm_url_validation_cache", return_value=None):
        with patch.object(discoverer, "_fetch_page_text", return_value=(False, "no_validator", "")):
            discoverer.audit_discovered_urls(trip)

    attractions = trip["destinations"][0]["ai_content"]["top_attractions"]
    assert len(attractions) == 1
    attr = attractions[0]
    assert attr["url"] == "https://www.nps.gov/zion/planyourvisit/human-history-museum.htm"
    assert attr["maps_url"] == "https://www.google.com/maps/search/?api=1&query=Zion+Human+History+Museum"


def test_audit_attaches_secondary_maps_link_for_attraction_with_distinct_primary_url() -> None:
    """Real gap confirmed on dipstick72 production output: 0 of 50 real
    attraction cards rendered the map-icon badge even though many had a
    real, distinct primary source URL (NPS pages, official sites, etc).
    Root cause: no code path in _discover_attractions/audit_discovered_urls
    ever attached a maps_url alongside an accepted real primary URL --
    every existing `attr["maps_url"] = ...` assignment only fires in the
    "no real source found, maps-search URL became the primary url itself"
    paths. This attraction (a plain museum, not trail-like, not AllTrails,
    with a real nps.gov page and no pre-existing maps_url at all) is the
    common case that previously got nothing; it must now get an additive,
    distinct name+destination Google Maps search link so the existing
    _maps_corner_link_html badge affordance has something to show."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._max_trail_miles = 0
    discoverer._direct_batch_authoritative = True
    discoverer._remember_direct_batch_authoritative_url(
        "https://www.nps.gov/zion/planyourvisit/human-history-museum.htm",
        "Zion Human History Museum",
    )

    trip = {
        "destinations": [
            {
                "id": "zion",
                "name": "Zion National Park",
                "ai_content": {
                    "top_attractions": [
                        {
                            "name": "Zion Human History Museum",
                            "type": "attraction",
                            "description": "Exhibits on the human history of Zion Canyon.",
                            "url": "https://www.nps.gov/zion/planyourvisit/human-history-museum.htm",
                        }
                    ],
                    "getting_here": {"en_route_stops": []},
                    "dinner_recommendations": [],
                },
                "scenic_drives": [],
                "cultural_events": {"events": []},
            }
        ]
    }

    with patch.object(discoverer, "_prewarm_url_validation_cache", return_value=None):
        with patch.object(discoverer, "_fetch_page_text", return_value=(False, "no_validator", "")):
            discoverer.audit_discovered_urls(trip)

    attractions = trip["destinations"][0]["ai_content"]["top_attractions"]
    assert len(attractions) == 1
    attr = attractions[0]
    assert attr["url"] == "https://www.nps.gov/zion/planyourvisit/human-history-museum.htm"
    assert attr["maps_url"] == (
        "https://www.google.com/maps/search/?api=1&query=Zion%20Human%20History%20Museum"
    )


def test_audit_attaches_secondary_maps_link_for_departure_route_option() -> None:
    """Real bug (published eval run): the "Turquoise Trail National Scenic
    Byway" departure route option had a real, distinct primary source URL
    (nsbfoundation.com) but no map-icon badge at all -- unlike en-route
    stops, attractions, and restaurants, no code path anywhere in this
    pipeline ever attached a maps_url to a route option. audit_discovered_
    urls' route_options loop now calls _attach_secondary_maps_link for each
    option after its primary url is settled, mirroring the attraction/
    restaurant fix above."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._direct_batch_authoritative = True
    discoverer._remember_direct_batch_authoritative_url(
        "https://nsbfoundation.com/nb/turquoise-trail-national-scenic-byway/",
        "Turquoise Trail National Scenic Byway",
    )

    trip = {
        "destinations": [
            {
                "id": "santa-fe",
                "name": "Santa Fe, New Mexico",
                "ai_content": {
                    "top_attractions": [],
                    "getting_here": {"en_route_stops": []},
                    "getting_there": {
                        "route_options": [
                            {
                                "title": "Turquoise Trail National Scenic Byway",
                                "url": "https://nsbfoundation.com/nb/turquoise-trail-national-scenic-byway/",
                                "distance_or_duration": "~50 mi total route",
                                "description": "This scenic byway connects Santa Fe and Albuquerque.",
                            }
                        ]
                    },
                    "dinner_recommendations": [],
                },
                "scenic_drives": [],
                "cultural_events": {"events": []},
            }
        ]
    }

    with patch.object(discoverer, "_prewarm_url_validation_cache", return_value=None):
        with patch.object(discoverer, "_fetch_page_text", return_value=(False, "no_validator", "")):
            discoverer.audit_discovered_urls(trip)

    options = trip["destinations"][0]["ai_content"]["getting_there"]["route_options"]
    assert len(options) == 1
    option = options[0]
    assert option["url"] == "https://nsbfoundation.com/nb/turquoise-trail-national-scenic-byway/"
    assert option["maps_url"] == (
        "https://www.google.com/maps/search/?api=1&query=Turquoise%20Trail%20National%20Scenic%20Byway%20Santa%20Fe%2C%20New%20Mexico"
    )


def test_audit_attaches_secondary_maps_link_for_restaurant_with_distinct_primary_url() -> None:
    """Restaurant mirror of the attraction case above: real dipstick72 data
    showed 0 of 61 restaurant cards ever rendered the map badge.
    _discover_restaurants actively does `rest.pop("maps_url", None)` in
    every branch that finds a real, distinct URL (Google Maps place,
    TripAdvisor, official site, direct-batch row) -- so by the time
    audit_discovered_urls runs, a restaurant with a genuinely useful
    primary link has no maps_url at all. This must now get one attached."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._direct_batch_authoritative = True
    discoverer._remember_direct_batch_authoritative_url(
        "https://www.thebitandspur.com/",
        "The Bit and Spur Restaurant",
    )

    trip = {
        "destinations": [
            {
                "id": "springdale",
                "name": "Springdale, Utah",
                "ai_content": {
                    "top_attractions": [],
                    "getting_here": {"en_route_stops": []},
                    "dinner_recommendations": [
                        {
                            "name": "The Bit and Spur Restaurant",
                            "url": "https://www.thebitandspur.com/",
                        }
                    ],
                },
                "scenic_drives": [],
                "cultural_events": {"events": []},
            }
        ]
    }

    with patch.object(discoverer, "_prewarm_url_validation_cache", return_value=None):
        with patch.object(discoverer, "_fetch_page_text", return_value=(False, "no_validator", "")):
            discoverer.audit_discovered_urls(trip)

    restaurants = trip["destinations"][0]["ai_content"]["dinner_recommendations"]
    assert len(restaurants) == 1
    rest = restaurants[0]
    assert rest["url"] == "https://www.thebitandspur.com/"
    assert rest["maps_url"] == (
        "https://www.google.com/maps/search/?api=1&query="
        "The%20Bit%20and%20Spur%20Restaurant%20Springdale%2C%20Utah"
    )


def test_audit_does_not_attach_secondary_maps_link_when_primary_url_is_maps_fallback() -> None:
    """The other half of the same fix: when no real source URL was ever
    found and a maps-search URL became the primary url itself (the
    pre-existing, already-correct behavior for both attractions and
    restaurants), the new secondary-maps-link logic must NOT fire --
    _maps_corner_link_html already suppresses the badge in this case
    (maps_url == primary_url would be redundant), and attaching a second,
    different maps-search link here would be actively wrong: it would
    make the corner-link badge point somewhere different from the card's
    own primary link for no reason."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._max_trail_miles = 0
    discoverer._direct_batch_authoritative = True

    maps_fallback_url = "https://www.google.com/maps/search/?api=1&query=Some+Obscure+Overlook+Zion+National+Park"
    trip = {
        "destinations": [
            {
                "id": "zion",
                "name": "Zion National Park",
                "seeds": ["Some Obscure Overlook"],
                "ai_content": {
                    "top_attractions": [
                        {
                            "name": "Some Obscure Overlook",
                            "type": "attraction",
                            "description": "A viewpoint with no dedicated page.",
                            "url": maps_fallback_url,
                            "maps_url": maps_fallback_url,
                        }
                    ],
                    "getting_here": {"en_route_stops": []},
                    "dinner_recommendations": [],
                },
                "scenic_drives": [],
                "cultural_events": {"events": []},
            }
        ]
    }

    with patch.object(discoverer, "_prewarm_url_validation_cache", return_value=None):
        with patch.object(discoverer, "_fetch_page_text", return_value=(False, "no_validator", "")):
            discoverer.audit_discovered_urls(trip)

    attractions = trip["destinations"][0]["ai_content"]["top_attractions"]
    assert len(attractions) == 1
    attr = attractions[0]
    assert attr["url"] == maps_fallback_url
    assert attr["maps_url"] == maps_fallback_url


def test_attach_secondary_maps_link_skips_alltrails_url_directly() -> None:
    """Unit-level guard on _attach_secondary_maps_link itself: AllTrails
    trail URLs are explicitly out of scope for this generic fallback --
    they have their own dedicated coordinate-based hook
    (_alltrails_geo_maps_url) with its own fail-closed/logging contract a
    few lines above this method's call site, and duplicating a text-query
    fallback on top of (or instead of) that would blur which mechanism is
    responsible for a trail card's map link."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    item = {
        "name": "Hickman Bridge Trail",
        "url": "https://www.alltrails.com/trail/us/utah/hickman-bridge-trail",
    }

    discoverer._attach_secondary_maps_link(
        item, "Hickman Bridge Trail", "Capitol Reef National Park", kind="attraction"
    )

    assert "maps_url" not in item


def test_alltrails_relevance_ignores_html_comment_closure_noise():
    """Full-path regression: _is_relevant_result must not reject a live,
    open trail page just because a closure phrase appears inside an HTML
    comment on that page (see test_has_alltrails_closure_marker_ignores_html_comment_noise
    for the underlying unit case)."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._url_validator = MagicMock()

    page_text = (
        "<html><body><h1>Navajo Loop Trail</h1>"
        "<p>A popular 1.3 mile loop trail near Bryce Canyon City, Utah.</p>"
        "<!-- temporarily closed due to rockfall last season, now reopened -->"
        "</body></html>"
    )

    with patch.object(discoverer, "_fetch_page_text", return_value=(True, 200, page_text)):
        ok = discoverer._is_relevant_result(
            "https://www.alltrails.com/trail/us/utah/navajo-loop-trail",
            "Navajo Loop Trail",
            "Bryce Canyon National Park",
            candidate={
                "name": "Navajo Loop Trail",
                "snippet": "Popular Bryce trail",
            },
        )

    assert ok is True


def test_alltrails_relevance_does_not_reject_generic_marketing_phrase_only():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._url_validator = MagicMock()
    discoverer._url_validator.session.get.return_value = MagicMock(
        status_code=200,
        text="Angels Landing Trail route details and reviews. Find your next trail nearby.",
    )

    url = "https://www.alltrails.com/trail/us/utah/angels-landing-trail"
    assert discoverer._is_relevant_result(url, "Angel's Landing", "Zion National Park")


def test_relevant_result_rejects_wrong_topic_page_for_generically_named_attraction():
    """Real bug (Bryce Canyon eval run): the attraction "Scenic Drive
    Overlooks" (description: "18-mile auto tour with multiple pullouts for
    hoodoo viewing") linked to nps.gov's hoodoo-GEOLOGY explainer page
    instead of a page about the drive/overlooks itself. Root cause: after
    _significant_tokens strips "scenic"/"drive" as generic route
    descriptors, the only token left is "overlook[s]" -- itself a member of
    GENERIC_VIEWPOINT_SUFFIX_TOKENS, not a real distinguishing identifier --
    so the single-token relevance bar (_required_general_token_matches(1) ==
    1) was trivially satisfied by any same-park page mentioning "overlook"
    once. This mocked page text mirrors the real hoodoos.htm content: it
    covers geology/formation, not the drive/auto-tour/pullouts the item's
    own description actually promises, so the new description-overlap
    requirement for weakly-named items must reject it."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    page_text = (
        "Hoodoos - Bryce Canyon National Park. The formation of Bryce Canyon "
        "and its hoodoos requires 3 steps: deposition of rocks, uplift of the "
        "land, and weathering and erosion. Many overlook the role of frost "
        "wedging in shaping these unique rock spires over thousands of years."
    )

    with patch.object(discoverer, "_fetch_page_text", return_value=(True, 200, page_text)):
        ok = discoverer._is_relevant_result(
            "https://www.nps.gov/brca/learn/nature/hoodoos.htm",
            "Scenic Drive Overlooks",
            "Bryce Canyon National Park",
            item_description="18-mile auto tour with multiple pullouts for hoodoo viewing.",
        )

    assert ok is False


def test_relevant_result_accepts_generically_named_attraction_with_matching_description():
    """Same weakly-named item as above, but this time the candidate page
    genuinely is about the drive/auto-tour/pullouts the description
    promises -- the new description-overlap check must not reject a real,
    on-topic match."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    page_text = (
        "Southern Scenic Drive Viewpoints - Bryce Canyon National Park. The "
        "18 mile main park road offers multiple pullouts and overlooks for "
        "a scenic auto tour, with viewing areas along the drive."
    )

    with patch.object(discoverer, "_fetch_page_text", return_value=(True, 200, page_text)):
        ok = discoverer._is_relevant_result(
            "https://www.nps.gov/brca/planyourvisit/southern-scenic-drive-viewpoints.htm",
            "Scenic Drive Overlooks",
            "Bryce Canyon National Park",
            item_description="18-mile auto tour with multiple pullouts for hoodoo viewing.",
        )

    assert ok is True


def test_relevant_result_weak_name_gate_is_noop_without_description():
    """When no item_description is available (most call sites today), the
    weak-name gate must not change behavior at all -- preserves today's
    exact single-token-match leniency for callers that don't pass one."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    page_text = "Hoodoos - Bryce Canyon National Park. Many overlook the geology here."

    with patch.object(discoverer, "_fetch_page_text", return_value=(True, 200, page_text)):
        ok = discoverer._is_relevant_result(
            "https://www.nps.gov/brca/learn/nature/hoodoos.htm",
            "Scenic Drive Overlooks",
            "Bryce Canyon National Park",
        )

    assert ok is True


def test_search_strict_rejects_wrong_but_live_alltrails_trail_page():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._search = MagicMock()
    discoverer._url_validator = MagicMock()
    discoverer._url_validator.session.get.return_value = MagicMock(
        status_code=200,
        text="Red Cliffs Recreation Area trail guide for hikers in St. George, Utah.",
    )
    discoverer._search.search.return_value = [
        {"url": "https://www.alltrails.com/trail/us/utah/red-cliffs-recreation-area-trail"}
    ]

    result = discoverer._search_first_strict(
        query_variants=['"Red Cliffs Desert Reserve" St. George Utah trail hiking'],
        site_filter="alltrails.com",
        site_hint=None,
        item_name="Red Cliffs Desert Reserve",
        dest_name="St. George, Utah",
        allow_alltrails=True,
    )

    assert result is None


def test_search_strict_accepts_alltrails_when_slug_uses_possessive_variant():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._search = MagicMock()
    discoverer._url_validator = MagicMock()
    discoverer._url_validator.session.get.return_value = MagicMock(
        status_code=200,
        text="Queen's Garden Trail Bryce Canyon hiking route details and reviews",
    )
    discoverer._search.search.return_value = [
        {"url": "https://www.alltrails.com/trail/us/utah/queen-s-garden-trail"}
    ]

    result = discoverer._search_first_strict(
        query_variants=['"Queens Garden Trail" Bryce Canyon National Park trail hiking'],
        site_filter="alltrails.com",
        site_hint=None,
        item_name="Queens Garden Trail",
        dest_name="Bryce Canyon National Park",
        allow_alltrails=True,
    )

    assert result == "https://www.alltrails.com/trail/us/utah/queen-s-garden-trail"


def test_search_strict_rejects_generic_404_landing_page():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._search = MagicMock()
    discoverer._url_validator = MagicMock()
    discoverer._url_validator.verify_url.return_value = (True, 200)
    discoverer._url_validator.session.get.return_value = MagicMock(
        status_code=200,
        text="Visit Utah 404 page",
    )
    discoverer._search.search.return_value = [
        {"url": "https://www.visitutah.com/404errorpage"}
    ]

    result = discoverer._search_first_strict(
        query_variants=['"Pioneer Park" St. George Utah trail hike attraction official site'],
        site_filter=None,
        site_hint=None,
        item_name="Pioneer Park",
        dest_name="St. George, Utah",
        allow_alltrails=False,
    )

    assert result is None


def test_search_strict_rejects_generic_nps_things2do_page():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._search = MagicMock()
    discoverer._url_validator = MagicMock()
    discoverer._url_validator.verify_url.return_value = (True, 200)
    discoverer._url_validator.session.get.return_value = MagicMock(
        status_code=200,
        text="Capitol Reef National Park things to do overview page",
    )
    discoverer._search.search.return_value = [
        {"url": "https://www.nps.gov/care/planyourvisit/things2do.htm"}
    ]

    result = discoverer._search_first_strict(
        query_variants=['"Red Canyon" Capitol Reef National Park attraction'],
        site_filter="nps.gov",
        site_hint="site:nps.gov/care",
        item_name="Red Canyon",
        dest_name="Capitol Reef National Park",
        allow_alltrails=False,
    )

    assert result is None


def test_is_relevant_result_rejects_campground_focused_page_for_noncamping_attraction():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)

    with patch.object(
        discoverer,
        "_fetch_page_text",
        return_value=(True, 200, "Red Canyon Campground reservations and campsite details near Bryce"),
    ):
        ok = discoverer._is_relevant_result(
            "https://www.brycecanyoncountry.com/places-to-go/red-canyon/campground/",
            "Red Canyon",
            "Bryce Canyon National Park",
            candidate={
                "name": "Red Canyon Campground",
                "snippet": "Campground reservations and campsites",
            },
        )

    assert ok is False


def test_is_relevant_result_allows_campground_page_for_camping_item_name():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)

    with patch.object(
        discoverer,
        "_fetch_page_text",
        return_value=(True, 200, "Red Canyon Campground reservations and campsite details near Bryce"),
    ):
        ok = discoverer._is_relevant_result(
            "https://www.brycecanyoncountry.com/places-to-go/red-canyon/campground/",
            "Red Canyon Campground",
            "Bryce Canyon National Park",
            candidate={
                "name": "Red Canyon Campground",
                "snippet": "Campground reservations and campsites",
            },
        )

    assert ok is True


def test_is_relevant_result_keeps_park_page_that_merely_mentions_its_campground():
    """A park's own page names the campground it contains, far down the body.

    Measured on live pages 2026-09-07: the first campground marker sat 54,207
    characters into Kodachrome Basin State Park's own page, 63,137 into Goblin
    Valley's, 212,003 into a Dixie National Forest visitor-centre page, and
    185,382 into a restaurant's site that mentions a nearby RV park. Scanning
    the whole body rejected all four -- the correct, official page for the
    place itself -- because every public-lands recreation page names a
    campground somewhere, having one.

    The distinction the filter wants is whether a page is ABOUT a campground,
    and such a page says so in its title and opening rather than in a footer.
    """
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    body = (
        "Kodachrome Basin State Park near Bryce Canyon is known for its "
        "sandstone chimneys and slot canyons. "
        + ("Trail descriptions and scenery notes for the park. " * 120)
        + " The park campground has 27 campsites and a group site."
    )
    assert body.index("campground") > 2000, "marker must sit past the scan window"

    with patch.object(discoverer, "_fetch_page_text", return_value=(True, 200, body)):
        ok = discoverer._is_relevant_result(
            "https://stateparks.utah.gov/parks/kodachrome-basin/",
            "Kodachrome Basin State Park",
            "Bryce Canyon National Park",
        )

    assert ok is True


def test_is_relevant_result_generic_branch_accepts_blocked_fetch_with_matching_candidate():
    """Regression: the generic (non-AllTrails) relevance branch used to treat
    ANY fetch failure as proof of a dead link, including a 403 from a
    bot-blocking site like TripAdvisor -- wrongly rejecting a perfectly live
    page. Mirrors the AllTrails branch's already-correct blocked-vs-dead
    handling: a non-dead-confirmed fetch failure with matching candidate
    metadata must be accepted, not rejected."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)

    with patch.object(discoverer, "_fetch_page_text", return_value=(False, 403, "")), patch.object(
        discoverer, "_verify_url_cached", return_value=(False, 403)
    ):
        ok = discoverer._is_relevant_result(
            "https://www.tripadvisor.com/Restaurant_Review-g60899-d123456-Reviews-Bit_Spur.html",
            "Bit & Spur",
            "Springdale",
            candidate={"name": "Bit & Spur Restaurant & Saloon", "snippet": "Southwestern dining in Springdale"},
        )

    assert ok is True


def test_is_relevant_result_generic_branch_accepts_blocked_fetch_with_no_candidate():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)

    with patch.object(discoverer, "_fetch_page_text", return_value=(False, 403, "")), patch.object(
        discoverer, "_verify_url_cached", return_value=(False, 403)
    ):
        ok = discoverer._is_relevant_result(
            "https://www.tripadvisor.com/Restaurant_Review-g60899-d123456-Reviews-Bit_Spur.html",
            "Bit & Spur",
            "Springdale",
        )

    assert ok is True


def test_is_relevant_result_generic_branch_rejects_definitively_dead_status():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)

    with patch.object(discoverer, "_fetch_page_text", return_value=(False, 404, "")):
        ok = discoverer._is_relevant_result(
            "https://www.example.com/closed-restaurant",
            "Some Restaurant",
            "Springdale",
            candidate={"name": "Some Restaurant", "snippet": "matches"},
        )

    assert ok is False


def test_is_relevant_result_generic_branch_rejects_when_secondary_probe_confirms_dead():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)

    with patch.object(discoverer, "_fetch_page_text", return_value=(False, 403, "")), patch.object(
        discoverer, "_verify_url_cached", return_value=(False, 404)
    ):
        ok = discoverer._is_relevant_result(
            "https://www.example.com/closed-restaurant",
            "Some Restaurant",
            "Springdale",
            candidate={"name": "Some Restaurant", "snippet": "matches"},
        )

    assert ok is False


def test_is_relevant_result_generic_branch_rejects_blocked_fetch_with_mismatched_candidate():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)

    with patch.object(discoverer, "_fetch_page_text", return_value=(False, 403, "")), patch.object(
        discoverer, "_verify_url_cached", return_value=(False, 403)
    ):
        ok = discoverer._is_relevant_result(
            "https://www.tripadvisor.com/Restaurant_Review-g60899-d999999-Reviews-Wrong_Place.html",
            "Bit & Spur",
            "Springdale",
            candidate={"name": "Completely Different Diner", "snippet": "unrelated cuisine"},
        )

    assert ok is False


def test_specific_result_url_accepts_nps_planyourvisit_detail_page():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    assert discoverer._is_specific_result_url(
        "https://www.nps.gov/zion/planyourvisit/kolob-canyons.htm",
        "Kolob Canyons",
        "Zion National Park",
    ) is True


def test_specific_result_url_rejects_nps_planyourvisit_landing_page():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    assert discoverer._is_specific_result_url(
        "https://www.nps.gov/zion/planyourvisit/",
        "Kolob Canyons",
        "Zion National Park",
    ) is False


def test_search_strict_accepts_blm_url_when_ssl_fallback_fetch_succeeds():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._search = MagicMock()
    discoverer._url_validator = MagicMock()
    discoverer._url_validator.verify_url.return_value = (True, 200)
    discoverer._url_validator.get_text.return_value = (True, 200, "Wilson Arch trailhead and visitor information near Moab Utah")
    discoverer._search.search.return_value = [
        {
            "url": "https://www.blm.gov/visit/wilson-arch",
            "name": "Wilson Arch",
            "snippet": "BLM information page",
        }
    ]

    result = discoverer._search_first_strict(
        query_variants=['"Wilson Arch" Moab Utah attraction'],
        site_filter=None,
        site_hint=None,
        item_name="Wilson Arch",
        dest_name="Moab, Utah",
        allow_alltrails=False,
    )

    assert result == "https://www.blm.gov/visit/wilson-arch"


def test_search_strict_rejects_npgallery_asset_detail_page():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._search = MagicMock()
    discoverer._url_validator = MagicMock()
    discoverer._url_validator.verify_url.return_value = (True, 200)
    discoverer._url_validator.session.get.return_value = MagicMock(
        status_code=200,
        text="National Register of Historic Places asset detail page",
    )
    discoverer._search.search.return_value = [
        {"url": "https://npgallery.nps.gov/NRHP/AssetDetail/7c8e5f3a-8b2d-4f1e-9c6a-2d5e8f7a9b0c"}
    ]

    result = discoverer._search_first_strict(
        query_variants=['"St. George Historic District" St. George Utah attraction landmark official site'],
        site_filter=None,
        site_hint=None,
        item_name="St. George Historic District",
        dest_name="St. George, Utah",
        allow_alltrails=False,
    )

    assert result is None


def test_search_strict_rejects_hallucinated_trail_with_partial_text_match():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._search = MagicMock()
    discoverer._url_validator = MagicMock()
    discoverer._url_validator.verify_url.return_value = (True, 200)
    discoverer._url_validator.session.get.return_value = MagicMock(
        status_code=200,
        text="Dixie trail in St. George, Utah.",
    )
    discoverer._search.search.return_value = [
        {"url": "https://www.dixie.edu/trails/dixie-trail"}
    ]

    result = discoverer._search_first_strict(
        query_variants=['"Dixie State University Trail" St. George Utah trail hike attraction official site'],
        site_filter=None,
        site_hint=None,
        item_name="Dixie State University Trail",
        dest_name="St. George, Utah",
        allow_alltrails=False,
    )

    assert result is None


def test_search_strict_rejects_numbered_suffix_alltrails_slug_variant():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._search = MagicMock()
    discoverer._url_validator = MagicMock()
    discoverer._search.search.return_value = [
        {
            "url": "https://www.alltrails.com/trail/us/utah/cassidy-arch-trail--2",
            "name": "Cassidy Arch Trail | AllTrails",
            "snippet": "2.7 mile out and back trail near Capitol Reef National Park.",
        }
    ]

    with patch.object(discoverer, "_fetch_page_text", return_value=(False, 403, "")):
        result = discoverer._search_first_strict(
            query_variants=['"Cassidy Arch" Capitol Reef National Park trail hiking'],
            site_filter="alltrails.com",
            site_hint=None,
            item_name="Cassidy Arch",
            dest_name="Capitol Reef National Park",
            allow_alltrails=True,
        )

    assert result is None


def test_normalize_restaurant_url_rejects_maps_directions_links():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    url = "https://www.google.com/maps/dir//Bit+%26+Spur+Restaurant+%26+Saloon,+1212+Zion+Park+Blvd,+Springdale,+UT+84767"
    assert discoverer._normalize_restaurant_url(url) == ""


def test_normalize_restaurant_url_rejects_maps_place_links():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    url = "https://www.google.com/maps/place/Zion+Pizza+%26+Noodle+Co./@37.1886,-112.9985,17z"
    assert discoverer._normalize_restaurant_url(url) == ""


def test_normalize_restaurant_url_accepts_deterministic_maps_place_links_with_data_segment():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._url_validator = MagicMock()
    discoverer._url_validator.session.get.return_value = MagicMock(
        url=(
            "https://www.google.com/maps/place/Zion+Pizza+%26+Noodle+Co./"
            "@37.1886,-112.9985,17z/data=!4m6!3m5!1s0x80cac2f9e17f7c3f:0x1234!8m2!3d37.1886!4d-112.9985"
        )
    )

    url = "https://www.google.com/maps/place/Zion+Pizza+%26+Noodle+Co./@37.1886,-112.9985,17z/data=!4m6"
    assert "/maps/place/" in discoverer._normalize_restaurant_url(url)


def test_normalize_restaurant_url_resolves_maps_short_link_to_place_url():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._url_validator = MagicMock()
    discoverer._maps_url_resolution_cache = {}
    discoverer._fetch_final_url_cache = {}
    discoverer._url_validator.session.get.return_value = MagicMock(
        url=(
            "https://www.google.com/maps/place/Oscar's+Cafe/"
            "@37.1882,-112.9983,17z/data=!4m6!3m5!1s0x80cac2f8:0x5678!8m2!3d37.1882!4d-112.9983"
        )
    )

    out = discoverer._normalize_restaurant_url("https://maps.app.goo.gl/rbaK8ZtvD67ZAjNa7")

    assert out.startswith("https://www.google.com/maps/place/Oscar's+Cafe/")


def test_normalize_restaurant_url_rejects_synthetic_google_maps_place_url():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._url_validator = MagicMock()
    discoverer._maps_url_resolution_cache = {}
    discoverer._fetch_final_url_cache = {}
    discoverer._url_validator.session.get.return_value = MagicMock(
        url=(
            "https://www.google.com/maps/place/Szechuan+Restaurant/"
            "data=!4m2!3m1!1s0x8746761e0a0a0a0a:0x1234567890abcdef"
        )
    )

    out = discoverer._normalize_restaurant_url("https://maps.app.goo.gl/example")

    assert out == ""


def test_normalize_restaurant_url_rejects_short_numeric_cid_maps_url():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._url_validator = MagicMock()
    discoverer._maps_url_resolution_cache = {}
    discoverer._fetch_final_url_cache = {}
    discoverer._url_validator.session.get.return_value = MagicMock(
        url="https://www.google.com/maps?cid=1234567890&q=The+Springs+Resort+Restaurant+Pagosa+Springs"
    )

    out = discoverer._normalize_restaurant_url("https://maps.app.goo.gl/example")

    assert out == ""


def test_restaurant_discovery_falls_back_when_maps_place_result_is_returned():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._restaurant_source = "search"

    ai = {
        "dinner_recommendations": [{"name": "Zion Pizza & Noodle Co."}],
        "top_attractions": [],
        "getting_here": {"en_route_stops": []},
    }

    def fake_search(variants, site_filter=None, **_kwargs):
        if site_filter == "google.com/maps":
            return "https://www.google.com/maps/place/Zion+Pizza+%26+Noodle+Co./@37.1886,-112.9985,17z"
        if site_filter == "tripadvisor.com":
            return None
        return None

    with patch.object(discoverer, "_search_first", side_effect=fake_search):
        with patch.object(discoverer, "_resolve_ai_candidate_url", return_value=None):
            discoverer._discover_restaurants(ai, dest_name="Zion National Park")

    out_url = ai["dinner_recommendations"][0]["url"]
    assert out_url == ""
    assert "maps_url" not in ai["dinner_recommendations"][0]


def test_normalize_restaurant_url_rejects_google_maps_search_query_links():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    assert (
        discoverer._normalize_restaurant_url(
            "https://www.google.com/maps/search/?api=1&query=Sushi+Yama+St.+George+Utah+restaurant"
        )
        == ""
    )
    assert (
        discoverer._normalize_restaurant_url(
            "https://maps.google.com/?q=Sushi+Yama+St.+George+UT"
        )
        == ""
    )


def test_restaurant_maps_query_text_keeps_destination_context_when_name_is_not_location_qualified():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    query = discoverer._restaurant_maps_query_text("Zion Pizza & Noodle Co.", "Zion National Park")
    assert "Zion National Park" in query
    assert "restaurant" in query.lower()


def test_restaurant_maps_query_text_does_not_append_destination_for_location_qualified_name():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    query = discoverer._restaurant_maps_query_text("Tropic Junction", "Bryce Canyon National Park")
    assert "Bryce Canyon National Park" not in query
    assert query == "Tropic Junction restaurant"


def test_non_hike_attractions_disallow_alltrails_results():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._search = MagicMock()
    discoverer._url_validator = MagicMock()

    discoverer._search.search.return_value = [
        {"url": "https://www.alltrails.com/trail/us/utah/st-george-dinosaur-discovery-site"},
        {"url": "https://utahdinosaurtracks.com/discovery-site"},
    ]
    discoverer._url_validator.verify_url.return_value = (True, None)
    discoverer._url_validator.session.get.return_value = MagicMock(
        status_code=200,
        text="St. George Dinosaur Discovery Site official information for St. George, Utah.",
    )

    result = discoverer._search_first_strict(
        query_variants=['"St. George Dinosaur Discovery Site" St. George Utah attraction'],
        site_filter=None,
        site_hint=None,
        item_name="St. George Dinosaur Discovery Site",
        dest_name="St. George, Utah",
        allow_alltrails=False,
    )

    assert result == "https://utahdinosaurtracks.com/discovery-site"


def test_search_strict_rejects_google_maps_place_restaurant_urls():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._search = MagicMock()
    discoverer._url_validator = MagicMock()

    discoverer._search.search.return_value = [
        {
            "url": "https://www.google.com/maps/place/Zion+Pizza+%26+Noodle+Co./@37.1885,-112.9995,17z",
            "name": "Zion Pizza & Noodle Co.",
            "snippet": "4.4 stars 1200 reviews",
        }
    ]

    with patch.object(discoverer, "_is_specific_result_url", return_value=True):
        result = discoverer._search_first_strict(
            query_variants=['"Zion Pizza & Noodle Co." "Zion National Park" restaurant'],
            site_filter="google.com/maps",
            site_hint=None,
            item_name="Zion Pizza & Noodle Co.",
            dest_name="Zion National Park",
            allow_alltrails=False,
        )

    assert result is None


def test_search_strict_accepts_google_maps_short_link_when_it_resolves_to_deterministic_place_url():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._search = MagicMock()
    discoverer._url_validator = MagicMock()
    discoverer._maps_url_resolution_cache = {}
    discoverer._fetch_final_url_cache = {}

    discoverer._search.search.return_value = [
        {
            "url": "https://maps.app.goo.gl/rbaK8ZtvD67ZAjNa7",
            "name": "Oscar's Cafe",
            "snippet": "4.6 stars 2,300 reviews",
        }
    ]
    discoverer._url_validator.session.get.return_value = MagicMock(
        url=(
            "https://www.google.com/maps/place/Oscar's+Cafe/"
            "@37.1882,-112.9983,17z/data=!4m6!3m5!1s0x80cac2f8:0x5678!8m2!3d37.1882!4d-112.9983"
        )
    )

    with patch.object(discoverer, "_is_specific_result_url", return_value=True):
        with patch.object(discoverer, "_is_relevant_result", return_value=True):
            result = discoverer._search_first_strict(
                query_variants=['"Oscar\'s Cafe" "Zion National Park" restaurant'],
                site_filter="google.com/maps",
                site_hint=None,
                item_name="Oscar's Cafe",
                dest_name="Zion National Park",
                allow_alltrails=False,
            )

    assert result.startswith("https://www.google.com/maps/place/Oscar's+Cafe/")


def test_discover_attractions_uses_google_maps_place_for_non_trail_when_web_search_misses():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._attraction_source = "search"

    ai = {
        "top_attractions": [
            {
                "name": "Gifford Homestead",
                "type": "attraction",
                "description": "Historic site and pie stop in Capitol Reef.",
            }
        ]
    }

    call_order = []

    def fake_search(_variants, site_filter=None, **_kwargs):
        call_order.append(site_filter or "")
        if site_filter == "nps.gov":
            return None
        if site_filter == "google.com/maps":
            return (
                "https://www.google.com/maps/place/Gifford+Homestead/"
                "@38.2912,-111.2475,17z/data=!4m6!3m5!1s0x8735ac7a:0x1234!8m2!3d38.2912!4d-111.2475"
            )
        return None

    with patch.object(discoverer, "_search_first", side_effect=fake_search):
        with patch.object(discoverer, "_resolve_ai_candidate_url", return_value=None):
            discoverer._discover_attractions(ai, "Capitol Reef National Park", "care")

    out = ai["top_attractions"][0]
    assert out["url"].startswith("https://www.google.com/maps/place/Gifford+Homestead/")
    assert out["maps_url"].startswith("https://www.google.com/maps/search/?api=1&query=")
    assert "google.com/maps" in call_order


def test_discover_attractions_keeps_alltrails_preference_before_google_maps():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)

    ai = {
        "top_attractions": [
            {
                "name": "The Narrows",
                "type": "attraction",
                "description": "Iconic Zion hike through the Virgin River canyon.",
            }
        ]
    }

    call_order = []

    def fake_search(_variants, site_filter=None, **_kwargs):
        call_order.append(site_filter or "")
        if site_filter == "alltrails.com":
            return "https://www.alltrails.com/trail/us/utah/the-narrows-top-down"
        if site_filter == "google.com/maps":
            return (
                "https://www.google.com/maps/place/The+Narrows/"
                "@37.2983,-112.9475,16z/data=!4m6!3m5!1s0x80cac2f8:0xabcd!8m2!3d37.2983!4d-112.9475"
            )
        return None

    with patch.object(discoverer, "_search_first", side_effect=fake_search):
        with patch.object(discoverer, "_resolve_ai_candidate_url", return_value=None):
            with patch.object(discoverer, "_meets_alltrails_publish_confidence", return_value=True):
                discoverer._discover_attractions(ai, "Zion National Park", "zion")

    out = ai["top_attractions"][0]
    assert out["url"].startswith("https://www.alltrails.com/trail/")
    assert "google.com/maps" not in call_order


def test_trail_like_attraction_prefers_alltrails_even_when_type_is_not_hike():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)

    call_order = []

    def fake_search(variants, site_filter=None, **kwargs):
        call_order.append(site_filter or "")
        if site_filter == "alltrails.com":
            return "https://www.alltrails.com/trail/us/utah/the-narrows-top-down"
        return None

    ai = {
        "top_attractions": [
            {
                "name": "The Narrows",
                "type": "attraction",
                "description": "Iconic Zion hike through the Virgin River canyon.",
            }
        ]
    }

    with patch.object(discoverer, "_search_first", side_effect=fake_search):
        with patch.object(discoverer, "_meets_alltrails_publish_confidence", return_value=True):
            discoverer._discover_attractions(ai, "Zion National Park", "zion")

    assert ai["top_attractions"][0]["url"].startswith("https://www.alltrails.com/trail/")
    assert call_order[0] == "alltrails.com"


def test_discover_attractions_keeps_remembered_direct_batch_trail_when_confidence_gate_fails():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._direct_batch_authoritative = True
    discoverer._alltrails_source = "direct_link_batch"
    direct_url = "https://www.alltrails.com/trail/us/utah/angels-landing-trail"
    discoverer._remember_direct_batch_authoritative_url(direct_url, "Angels Landing")

    ai = {
        "top_attractions": [
            {
                "name": "Angels Landing",
                "type": "hike",
                "description": "Iconic exposed route in Zion.",
            }
        ]
    }

    with patch.object(discoverer, "_resolve_ai_candidate_url", return_value=None):
        with patch.object(discoverer, "_search_alltrails_for_trail", return_value=direct_url):
            with patch.object(discoverer, "_meets_alltrails_publish_confidence", return_value=False):
                discoverer._discover_attractions(ai, "Zion National Park", "zion")

    out = ai["top_attractions"][0]
    assert out.get("url") == direct_url


def test_discovery_site_name_is_not_trail_like_even_if_type_is_hike():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)

    assert discoverer._is_trail_like_attraction(
        "St. George Dinosaur Discovery Site",
        "hike",
        "Hands-on museum exhibits with dinosaur trackway displays.",
    ) is False


def test_discovery_site_does_not_use_alltrails_first_when_type_is_hike():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._attraction_source = "search"

    call_order = []

    def fake_search(_variants, site_filter=None, **_kwargs):
        call_order.append(site_filter or "")
        if site_filter == "nps.gov":
            return None
        return "https://utahdinosaurtracks.com/discovery-site"

    ai = {
        "top_attractions": [
            {
                "name": "St. George Dinosaur Discovery Site",
                "type": "hike",
                "description": "Paleontology museum and in-situ tracks.",
            }
        ]
    }

    with patch.object(discoverer, "_search_first", side_effect=fake_search):
        discoverer._discover_attractions(ai, "St. George, Utah", None)

    assert call_order[0] != "alltrails.com"
    assert ai["top_attractions"][0]["url"] == "https://utahdinosaurtracks.com/discovery-site"


def test_trail_like_attraction_uses_ai_url_candidates_before_search():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._alltrails_source = "search"
    discoverer._direct_batch_authoritative = False

    ai = {
        "top_attractions": [
            {
                "name": "Canyon Overlook Trail",
                "type": "hike",
                "description": "Short trail with expansive canyon views.",
                "url_candidates": [
                    "https://www.alltrails.com/trail/us/utah/canyon-overlook-trail?u=i",
                ],
            }
        ]
    }

    with patch.object(discoverer, "_search_alltrails_for_trail", return_value=None) as mock_alltrails_search:
        with patch.object(discoverer, "_retain_discovered_url", side_effect=lambda url, *_args, **_kwargs: url):
            discoverer._discover_attractions(ai, "Zion National Park", "zion")

    assert ai["top_attractions"][0]["url"] == "https://www.alltrails.com/trail/us/utah/canyon-overlook-trail"
    mock_alltrails_search.assert_not_called()


def test_trail_like_attraction_prefers_alltrails_for_riverside_walk_name():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)

    seen_site_filters = []

    def fake_search(variants, site_filter=None, **kwargs):
        seen_site_filters.append(site_filter or "")
        if site_filter == "alltrails.com":
            return "https://www.alltrails.com/trail/us/utah/the-zion-narrows-riverside-walk"
        return None

    ai = {
        "top_attractions": [
            {
                "name": "The Zion Narrows Riverside Walk",
                "type": "attraction",
                "description": "Iconic canyon route with river views.",
            }
        ]
    }

    with patch.object(discoverer, "_search_first", side_effect=fake_search):
        discoverer._discover_attractions(ai, "Zion National Park", "zion")

    assert ai["top_attractions"][0]["url"] == "https://www.alltrails.com/trail/us/utah/the-zion-narrows-riverside-walk"
    assert seen_site_filters[0] == "alltrails.com"


def test_trail_like_attraction_uses_description_phrase_this_trail_for_alltrails_first():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)

    seen_site_filters = []

    def fake_search(variants, site_filter=None, **kwargs):
        seen_site_filters.append(site_filter or "")
        if site_filter == "alltrails.com":
            return "https://www.alltrails.com/trail/us/utah/angels-landing-trail"
        return None

    ai = {
        "top_attractions": [
            {
                "name": "Angels Landing",
                "type": "attraction",
                "description": "This trail climbs through steep switchbacks to panoramic views.",
            }
        ]
    }

    with patch.object(discoverer, "_search_first", side_effect=fake_search):
        discoverer._discover_attractions(ai, "Zion National Park", "zion")

    assert ai["top_attractions"][0]["url"] == "https://www.alltrails.com/trail/us/utah/angels-landing-trail"
    assert seen_site_filters[0] == "alltrails.com"


def test_trail_like_attraction_handles_apostrophe_name_variant():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._alltrails_min_confidence_for_publish = "low"
    discoverer._alltrails_source = "search"
    discoverer._direct_batch_authoritative = False

    seen_site_filters = []

    def fake_search(variants, site_filter=None, **kwargs):
        seen_site_filters.append(site_filter or "")
        if site_filter == "alltrails.com":
            return "https://www.alltrails.com/trail/us/utah/angels-landing-trail"
        return None

    ai = {
        "top_attractions": [
            {
                "name": "Angel's Landing",
                    "type": "hike",
                "description": "Iconic chain section and canyon views.",
            }
        ]
    }

    with patch.object(discoverer, "_search_first", side_effect=fake_search):
        discoverer._discover_attractions(ai, "Zion National Park", "zion")

    assert ai["top_attractions"][0]["url"] == "https://www.alltrails.com/trail/us/utah/angels-landing-trail"
    assert seen_site_filters[0] == "alltrails.com"


def test_place_level_attraction_not_forced_to_alltrails_from_generic_trail_wording():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._attraction_source = "search"

    seen_site_filters = []

    def fake_search(variants, site_filter=None, **kwargs):
        seen_site_filters.append(site_filter or "")
        if site_filter == "nps.gov":
            return "https://stateparks.utah.gov/parks/snow-canyon/"
        return None

    ai = {
        "top_attractions": [
            {
                "name": "Snow Canyon State Park",
                "type": "attraction",
                "description": "This trail-rich park has lava tubes and overlooks.",
            }
        ]
    }

    with patch.object(discoverer, "_search_first", side_effect=fake_search):
        discoverer._discover_attractions(ai, "St. George, Utah", None)

    assert ai["top_attractions"][0]["url"].startswith("https://www.google.com/maps/search/?api=1&query=")
    assert "alltrails.com" not in seen_site_filters


def test_place_level_snow_canyon_search_disallows_alltrails_candidates():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._attraction_source = "search"

    observed_allow_alltrails: list[bool] = []

    def fake_search(variants, site_filter=None, **kwargs):
        observed_allow_alltrails.append(bool(kwargs.get("allow_alltrails", True)))
        return None

    ai = {
        "top_attractions": [
            {
                "name": "Snow Canyon State Park",
                "type": "hike",
                "description": "Trail-rich desert park with overlooks.",
            }
        ]
    }

    with patch.object(discoverer, "_search_first", side_effect=fake_search):
        discoverer._discover_attractions(ai, "St. George, Utah", None)

    assert observed_allow_alltrails
    assert all(flag is False for flag in observed_allow_alltrails)


def test_plain_park_name_not_forced_to_alltrails():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)

    seen_site_filters = []

    def fake_search(variants, site_filter=None, **kwargs):
        seen_site_filters.append(site_filter or "")
        return None

    ai = {
        "top_attractions": [
            {
                "name": "Pioneer Park",
                "type": "hike",
                "description": "Local sandstone park with short connectors.",
            }
        ]
    }

    with patch.object(discoverer, "_search_first", side_effect=fake_search):
        discoverer._discover_attractions(ai, "St. George, Utah", None)

    assert "alltrails.com" not in seen_site_filters


def test_place_level_attraction_not_forced_to_alltrails_even_when_type_is_hike():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._attraction_source = "search"

    seen_site_filters = []

    def fake_search(variants, site_filter=None, **kwargs):
        seen_site_filters.append(site_filter or "")
        return None

    ai = {
        "top_attractions": [
            {
                "name": "Red Cliffs Desert Reserve",
                "type": "hike",
                "description": "Large protected landscape with multiple trailheads.",
            }
        ]
    }

    with patch.object(discoverer, "_search_first", side_effect=fake_search):
        discoverer._discover_attractions(ai, "St. George, Utah", None)

    assert ai["top_attractions"][0]["url"].startswith("https://www.google.com/maps/search/?api=1&query=")
    assert "alltrails.com" not in seen_site_filters


def test_petroglyph_place_level_attraction_not_forced_to_alltrails():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._attraction_source = "search"
    discoverer._search = MagicMock()
    discoverer._url_validator = MagicMock()

    seen_site_filters: list[str | None] = []

    def fake_search_first(variants, site_filter=None, **kwargs):
        seen_site_filters.append(site_filter)
        if site_filter == "nps.gov":
            return "https://www.nps.gov/care/learn/historyculture/fremont-culture.htm"
        return None

    with patch.object(discoverer, "_search_first", side_effect=fake_search_first):
        ai = {
            "top_attractions": [
                {
                    "name": "Fremont Petroglyphs",
                    "type": "attraction",
                    "description": "Rock art panels accessible from a short pullout stop.",
                }
            ],
            "dinner_recommendations": [],
            "getting_here": {"en_route_stops": []},
        }
        discoverer._discover_attractions(ai, "Capitol Reef National Park", "care", "October 11-13, 2026")

    assert "alltrails.com" not in [s for s in seen_site_filters if s]
    assert ai["top_attractions"][0]["url"].startswith("https://www.nps.gov/")


def test_viewpoint_place_level_attraction_not_forced_to_alltrails():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._search = MagicMock()
    discoverer._url_validator = MagicMock()

    seen_site_filters: list[str | None] = []

    def fake_search_first(variants, site_filter=None, **kwargs):
        seen_site_filters.append(site_filter)
        if site_filter == "nps.gov":
            return "https://www.nps.gov/care/planyourvisit/chimney-rock-trail.htm"
        return None

    with patch.object(discoverer, "_search_first", side_effect=fake_search_first):
        ai = {
            "top_attractions": [
                {
                    "name": "Capitol Reef Viewpoint",
                    "type": "attraction",
                    "description": "Roadside viewpoint with broad canyon panoramas.",
                }
            ],
            "dinner_recommendations": [],
            "getting_here": {"en_route_stops": []},
        }
        discoverer._discover_attractions(ai, "Capitol Reef National Park", "care", "October 11-13, 2026")

    assert "alltrails.com" not in [s for s in seen_site_filters if s]


def test_nps_category_activity_prefers_nps_activity_page_over_maps_fallback():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._attraction_source = "search"
    discoverer._search = MagicMock()
    discoverer._url_validator = MagicMock()

    observed_nps_variants: list[list[str]] = []

    def fake_search_first(variants, site_filter=None, **kwargs):
        if site_filter == "nps.gov":
            observed_nps_variants.append(list(variants))
            if any("night sky astronomy" in str(v).lower() for v in variants):
                return "https://www.nps.gov/brca/planyourvisit/night-skies.htm"
            return None
        return None

    with patch.object(discoverer, "_search_first", side_effect=fake_search_first):
        ai = {
            "top_attractions": [
                {
                    "name": "Dark Sky Stargazing",
                    "type": "attraction",
                    "description": "Night sky viewing program with telescope-friendly conditions.",
                }
            ],
            "dinner_recommendations": [],
            "getting_here": {"en_route_stops": []},
        }
        discoverer._discover_attractions(ai, "Bryce Canyon National Park", "brca", "October 11-13, 2026")

    assert ai["top_attractions"][0]["url"] == "https://www.nps.gov/brca/planyourvisit/night-skies.htm"
    assert len(observed_nps_variants) >= 2
    assert any("night sky astronomy" in " ".join(v).lower() for v in observed_nps_variants)


def test_nps_category_activity_fail_closes_when_nps_activity_page_unavailable():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._search = MagicMock()
    discoverer._url_validator = MagicMock()

    with patch.object(discoverer, "_search_first", return_value=None):
        ai = {
            "top_attractions": [
                {
                    "name": "Dark Sky Stargazing",
                    "type": "attraction",
                    "description": "Night sky viewing from pullouts and overlooks.",
                }
            ]
        }
        discoverer._discover_attractions(ai, "Bryce Canyon National Park", "brca", "October 11-13, 2026")

    assert ai["top_attractions"][0]["url"] == ""


def test_trail_like_attraction_fail_closes_when_no_validated_trail_url() -> None:
    """Unresolved trail-like items in authoritative mode (the default -- see
    DEFAULT_DIRECT_BATCH_AUTHORITATIVE) no longer stay completely unverified:
    since the trail-like-branch no-link-fallback fix they get the same safe
    maps-search fallback every other "no URL found" attraction gets. This is
    NOT a claim of a specific correct source page (that's what "generic maps
    fallbacks are not used" guarded against pre-fix, for the AI-candidate/
    live-search risk) -- it's a deterministic name+destination search link.
    """
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._search = MagicMock()
    discoverer._url_validator = MagicMock()

    with patch.object(discoverer, "_resolve_ai_candidate_url", return_value=None), patch.object(
        discoverer, "_search_alltrails_for_trail", return_value=None
    ), patch.object(discoverer, "_search_attraction_from_direct_batch", return_value=None):
        ai = {
            "top_attractions": [
                {
                    "name": "Jud Wiebe Trail",
                    "type": "hike",
                    "description": "Steep trail with views above Telluride.",
                }
            ]
        }
        discoverer._discover_attractions(ai, "Telluride", None, "July 10-12, 2026")

    attr = ai["top_attractions"][0]
    assert attr.get("url") == (
        "https://www.google.com/maps/search/?api=1&query=Jud%20Wiebe%20Trail%20Telluride"
    )
    assert attr.get("maps_url") == attr.get("url")


def test_trail_like_attraction_direct_batch_authoritative_does_not_accept_ai_candidate_url() -> None:
    """Fabrication-guard tripwire mirroring the non-trail-branch equivalent
    (test_discover_attractions_direct_batch_authoritative_recovers_seed_from_ai_candidate):
    authoritative mode must never resolve an AI-suggested url_candidate when
    the batch harvest found no match, regardless of whether the item
    afterward gets a maps-search fallback. The load-bearing assertion is
    `ai_candidate_mock.assert_not_called()`; the exact fallback URL only
    confirms the recovered link didn't come from the AI-candidate resolver.
    """
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._alltrails_source = "direct_link_batch"
    discoverer._direct_batch_authoritative = True

    ai = {
        "top_attractions": [
            {
                "name": "Jud Wiebe Trail",
                "type": "hike",
                "description": "Steep trail with views above Telluride.",
                "url_candidates": [
                    "https://www.alltrails.com/trail/us/colorado/jud-wiebe-trail"
                ],
            }
        ]
    }

    with patch.object(discoverer, "_resolve_ai_candidate_url") as ai_candidate_mock:
        with patch.object(discoverer, "_search_alltrails_for_trail", return_value=None):
            with patch.object(discoverer, "_search_attraction_from_direct_batch", return_value=None):
                discoverer._discover_attractions(ai, "Telluride", None, "July 10-12, 2026")

    attraction = ai["top_attractions"][0]
    assert attraction.get("url") == (
        "https://www.google.com/maps/search/?api=1&query=Jud%20Wiebe%20Trail%20Telluride"
    )
    assert attraction.get("maps_url") == attraction.get("url")
    ai_candidate_mock.assert_not_called()


def test_search_strict_nps_broad_pass_rejects_generic_index_page_without_item_signal():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._search = MagicMock()
    discoverer._url_validator = MagicMock()
    discoverer._url_validator.verify_url.return_value = (True, 200)
    discoverer._search.search.return_value = [
        {
            "url": "https://www.nps.gov/care/index.htm",
            "name": "Capitol Reef National Park",
            "snippet": "Official National Park Service site",
        }
    ]

    result = discoverer._search_first_strict(
        query_variants=['"Gifford Homestead" Capitol Reef National Park attraction'],
        site_filter="nps.gov",
        site_hint=None,
        item_name="Gifford Homestead",
        dest_name="Capitol Reef National Park",
        allow_alltrails=False,
    )

    assert result is None


def test_audit_keeps_alltrails_for_trail_like_non_hike_attraction():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._url_validator = MagicMock()
    discoverer._url_validator.session.get.return_value = MagicMock(
        status_code=200,
        text="The Narrows trail in Zion National Park hiking guide and route details.",
    )

    trip = {
        "destinations": [
            {
                "name": "Zion National Park",
                "ai_content": {
                    "top_attractions": [
                        {
                            "name": "The Narrows",
                            "type": "attraction",
                            "description": "Classic river hike.",
                            "url": "https://www.alltrails.com/trail/us/utah/the-narrows-top-down",
                        }
                    ],
                    "getting_here": {"en_route_stops": []},
                    "dinner_recommendations": [],
                },
                "scenic_drives": [],
                "cultural_events": {"has_events": False, "events": []},
            }
        ]
    }

    discoverer.audit_discovered_urls(trip)

    attraction = trip["destinations"][0]["ai_content"]["top_attractions"][0]
    assert attraction.get("url", "").startswith("https://www.alltrails.com/trail/")


def test_audit_keeps_alltrails_for_trail_like_when_text_fetch_fails_but_url_live():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._url_validator = MagicMock()
    discoverer._url_validator.verify_url.return_value = (True, 200)
    discoverer._url_validator.session.get.side_effect = Exception("timeout")

    trip = {
        "destinations": [
            {
                "name": "Bryce Canyon National Park",
                "ai_content": {
                    "top_attractions": [
                        {
                            "name": "Queens Garden Trail",
                            "type": "attraction",
                            "description": "This trail descends through hoodoos.",
                            "url": "https://www.alltrails.com/trail/us/utah/queens-garden-trail",
                        }
                    ],
                    "getting_here": {"en_route_stops": []},
                    "dinner_recommendations": [],
                },
                "scenic_drives": [],
                "cultural_events": {"has_events": False, "events": []},
            }
        ]
    }

    discoverer.audit_discovered_urls(trip)

    attraction = trip["destinations"][0]["ai_content"]["top_attractions"][0]
    assert attraction.get("url", "").startswith("https://www.alltrails.com/trail/")


def test_audit_keeps_alltrails_when_fetch_fails_even_if_liveness_probe_would_fail():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._url_validator = MagicMock()
    discoverer._url_validator.verify_url.return_value = (False, "blocked")
    discoverer._url_validator.session.get.side_effect = Exception("timeout")

    trip = {
        "destinations": [
            {
                "name": "St. George, Utah",
                "ai_content": {
                    "top_attractions": [
                        {
                            "name": "Angels Landing",
                            "type": "attraction",
                            "description": "Iconic canyon route.",
                            "practical_note": "This trail has chains and exposure.",
                            "url": "https://www.alltrails.com/trail/us/utah/angels-landing-via-west-rim-trail",
                        }
                    ],
                    "getting_here": {"en_route_stops": []},
                    "dinner_recommendations": [],
                },
                "scenic_drives": [],
                "cultural_events": {"has_events": False, "events": []},
            }
        ]
    }

    discoverer.audit_discovered_urls(trip)

    attraction = trip["destinations"][0]["ai_content"]["top_attractions"][0]
    assert attraction.get("url", "").startswith("https://www.alltrails.com/trail/")


def test_audit_uses_same_trail_context_fields_as_discovery():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._url_validator = MagicMock()
    discoverer._url_validator.session.get.return_value = MagicMock(
        status_code=200,
        text="Angels Landing trail Zion hiking route details",
    )

    trip = {
        "destinations": [
            {
                "name": "Zion National Park",
                "ai_content": {
                    "top_attractions": [
                        {
                            "name": "Angels Landing",
                            "type": "attraction",
                            "description": "Iconic canyon viewpoint.",
                            "practical_note": "This trail requires a permit.",
                            "url": "https://www.alltrails.com/trail/us/utah/zion-national-park-angels-landing",
                        }
                    ],
                    "getting_here": {"en_route_stops": []},
                    "dinner_recommendations": [],
                },
                "scenic_drives": [],
                "cultural_events": {"has_events": False, "events": []},
            }
        ]
    }

    discoverer.audit_discovered_urls(trip)

    attraction = trip["destinations"][0]["ai_content"]["top_attractions"][0]
    assert attraction.get("url", "").startswith("https://www.alltrails.com/trail/")


def test_audit_keeps_alltrails_when_slug_matches_but_page_text_is_sparse():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._url_validator = MagicMock()
    discoverer._url_validator.session.get.return_value = MagicMock(
        status_code=200,
        text="<html><body>AllTrails</body></html>",
    )

    trip = {
        "destinations": [
            {
                "name": "Bryce Canyon National Park",
                "ai_content": {
                    "top_attractions": [
                        {
                            "name": "Navajo Loop Trail",
                            "type": "hike",
                            "description": "Classic hoodoo descent.",
                            "url": "https://www.alltrails.com/trail/us/utah/navajo-loop-trail",
                        }
                    ],
                    "getting_here": {"en_route_stops": []},
                    "dinner_recommendations": [],
                },
                "scenic_drives": [],
                "cultural_events": {"has_events": False, "events": []},
            }
        ]
    }

    discoverer.audit_discovered_urls(trip)

    attraction = trip["destinations"][0]["ai_content"]["top_attractions"][0]
    assert attraction.get("url", "").startswith("https://www.alltrails.com/trail/")


def test_trail_like_attraction_uses_extended_alltrails_sweep_before_fallback():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)

    seen_calls = []

    def fake_search(variants, site_filter=None, **kwargs):
        seen_calls.append((site_filter, list(variants)))
        if site_filter == "alltrails.com":
            # Simulate only a broad trailing variant finding a match.
            if any(v.strip().lower() == "canyon overlook trail" for v in variants):
                return "https://www.alltrails.com/trail/us/utah/canyon-overlook-trail"
            return None
        return "https://www.nps.gov/zion/planyourvisit/canyon-overlook-trail.htm"

    ai = {
        "top_attractions": [
            {
                "name": "Canyon Overlook Trail",
                "type": "attraction",
                "description": "Short trail with great views.",
            }
        ]
    }

    with patch.object(discoverer, "_search_first", side_effect=fake_search):
        discoverer._discover_attractions(ai, "Zion National Park", "zion")

    attr_url = ai["top_attractions"][0]["url"]
    assert attr_url == "https://www.alltrails.com/trail/us/utah/canyon-overlook-trail"
    assert seen_calls[0][0] == "alltrails.com"
    assert any(v.strip().lower() == "canyon overlook trail" for v in seen_calls[0][1])


def test_search_alltrails_for_trail_includes_explicit_alltrails_variants():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)

    captured = {}

    def fake_search_first(variants, **kwargs):
        captured["variants"] = list(variants)
        return None

    with patch.object(discoverer, "_search_first", side_effect=fake_search_first):
        discoverer._search_alltrails_for_trail("Angels Landing", "Zion National Park")

    variants = [v.lower() for v in captured["variants"]]
    assert any("alltrails" in v for v in variants)
    assert any(v.strip() == '"angels landing" alltrails' for v in variants)


def test_trail_like_attraction_omits_link_when_no_validated_trail_url_exists() -> None:
    """Authoritative mode is the default (DEFAULT_DIRECT_BATCH_AUTHORITATIVE),
    so an unresolved trail-like item here now gets the same safe maps-search
    fallback every other "no URL found" attraction gets, instead of being
    left completely unverified (the trail-like-branch no-link-fallback fix).

    This must hold when EVERY real search avenue -- including the one-more
    general-search recovery attempt authoritative mode now tries before
    giving up (see authoritative_no_match_recovered_via_general_search) --
    comes up empty, not just the AllTrails-specific ones. Returning None
    unconditionally here (instead of only for site_filter == "alltrails.com")
    is what actually exercises the "no validated trail url exists anywhere"
    scenario this test is named for.
    """
    discoverer = URLDiscoverer.__new__(URLDiscoverer)

    def fake_search(variants, site_filter=None, **kwargs):
        return None

    ai = {
        "top_attractions": [
            {
                "name": "Grand Wash Trail",
                "type": "hike",
                "description": "Canyon walk.",
            }
        ]
    }

    with patch.object(discoverer, "_search_first", side_effect=fake_search):
        with patch.object(discoverer, "_search_attraction_from_item_query_fanout", return_value=(None, "no_match")):
            with patch.object(discoverer, "_search_attraction_from_direct_batch", return_value=None):
                discoverer._discover_attractions(ai, "Bryce Canyon National Park", "blca")

    attr = ai["top_attractions"][0]
    assert attr.get("url") == (
        "https://www.google.com/maps/search/?api=1&query=Grand%20Wash%20Trail%20Bryce%20Canyon%20National%20Park"
    )
    assert attr.get("maps_url") == attr.get("url")


def _make_nominatim_response(lat: str, lon: str):
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = [{"lat": lat, "lon": lon}]
    return resp


def _make_nominatim_response_named(lat: str, lon: str, *, name: str = "", display_name: str = ""):
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = [{"lat": lat, "lon": lon, "name": name, "display_name": display_name}]
    return resp


def _make_nominatim_empty_response():
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = []
    return resp


def _make_nominatim_response_full(
    lat: str,
    lon: str,
    *,
    name: str = "",
    display_name: str = "",
    osm_class: str = "",
    osm_type: str = "",
    boundingbox: list[str] | None = None,
):
    """Like _make_nominatim_response_named but also carries class/type/
    boundingbox -- the fields the oversized-waterway check needs and real
    Nominatim responses always include, but which the earlier helpers above
    predate and omit (deliberately -- see _geocode_result_is_oversized_
    waterway's docstring on why absence must fail open, not reject)."""
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    row = {"lat": lat, "lon": lon, "name": name, "display_name": display_name}
    if osm_class:
        row["class"] = osm_class
    if osm_type:
        row["type"] = osm_type
    if boundingbox is not None:
        row["boundingbox"] = boundingbox
    resp.json.return_value = [row]
    return resp


def test_geocode_en_route_stop_uses_viewbox_to_disambiguate_common_place_name() -> None:
    """Full-pipeline regression for a real reported bug: Zion->Bryce en-route
    stops were rendered in AI-harvest order, not route order, forcing the
    driving-directions link to backtrack. Root cause: the geocoder's 'near X'
    query phrasing always returns zero results from Nominatim, and the
    unbiased fallback resolves common names like 'Red Canyon' to an unrelated
    same-named place elsewhere in the state. A viewbox biased to the route's
    own origin/destination must be used and must win over an unbiased match."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._url_validator = MagicMock()

    # Zion (Springdale) -> Bryce Canyon, approximate real coordinates.
    origin = (37.1889, -112.9986)
    dest = (37.5930, -112.1871)
    # "Red Canyon" near the actual route vs. a same-named place far away (San Juan County, UT).
    near_route = ("37.72", "-112.31")
    far_away = ("37.64", "-110.38")

    def fake_get(_url, params=None, **_kwargs):
        if params and params.get("bounded") == 1:
            return _make_nominatim_response(*near_route)
        return _make_nominatim_response(*far_away)

    discoverer._url_validator.session.get.side_effect = fake_get

    with patch("generator.url_discovery.time.sleep"):
        coords = discoverer._geocode_en_route_stop_for_route(
            "Red Canyon",
            origin_name="Zion National Park",
            dest_name="Bryce Canyon National Park",
            origin=origin,
            dest=dest,
        )

    assert coords == (37.72, -112.31)


def test_geocode_en_route_stop_rejects_unrestricted_match_outside_sanity_radius() -> None:
    """When nothing is found within the route's viewbox, the unrestricted
    fallback must not blindly accept a same-named place clear across the
    country (e.g. a real reported case: 'Glendale Town Park' resolved to
    Illinois for a Utah route) -- it must be rejected as no match."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._url_validator = MagicMock()

    origin = (37.1889, -112.9986)
    dest = (37.5930, -112.1871)
    chicago_area = ("41.798644", "-87.7111642")

    def fake_get(_url, params=None, **_kwargs):
        if params and params.get("bounded") == 1:
            return _make_nominatim_empty_response()
        return _make_nominatim_response(*chicago_area)

    discoverer._url_validator.session.get.side_effect = fake_get

    with patch("generator.url_discovery.time.sleep"):
        coords = discoverer._geocode_en_route_stop_for_route(
            "Glendale Town Park",
            origin_name="Zion National Park",
            dest_name="Bryce Canyon National Park",
            origin=origin,
            dest=dest,
        )

    assert coords is None


def test_geocode_en_route_stop_accepts_unrestricted_match_within_sanity_radius() -> None:
    """A same-region unrestricted match (viewbox search found nothing, but the
    fallback match is still plausibly near the route) should still be usable --
    the sanity check should not be so strict it rejects everything."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._url_validator = MagicMock()

    origin = (37.1889, -112.9986)
    dest = (37.5930, -112.1871)
    nearby_but_outside_viewbox = ("37.9", "-111.8")

    def fake_get(_url, params=None, **_kwargs):
        if params and params.get("bounded") == 1:
            return _make_nominatim_empty_response()
        return _make_nominatim_response(*nearby_but_outside_viewbox)

    discoverer._url_validator.session.get.side_effect = fake_get

    with patch("generator.url_discovery.time.sleep"):
        coords = discoverer._geocode_en_route_stop_for_route(
            "Some Trailhead",
            origin_name="Zion National Park",
            dest_name="Bryce Canyon National Park",
            origin=origin,
            dest=dest,
        )

    assert coords == (37.9, -111.8)


def test_geocode_en_route_stop_marks_out_of_region_rejection_distinctly_from_no_data() -> None:
    """Regression for dipstick55 Theme A: when the sanity-radius check rejects
    a real named-place hit (not merely an absence of data), that must be
    recorded so the caller can tell 'we found this name, but only far away'
    apart from 'we have no information either way'."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._url_validator = MagicMock()

    origin = (37.1889, -112.9986)
    dest = (37.5930, -112.1871)
    snoqualmie_wa = ("47.5301", "-121.8253")

    def fake_get(_url, params=None, **_kwargs):
        if params and params.get("bounded") == 1:
            return _make_nominatim_empty_response()
        return _make_nominatim_response(*snoqualmie_wa)

    discoverer._url_validator.session.get.side_effect = fake_get

    with patch("generator.url_discovery.time.sleep"):
        coords = discoverer._geocode_en_route_stop_for_route(
            "Stan's Overlook Trail",
            origin_name="Zion National Park",
            dest_name="Bryce Canyon National Park",
            origin=origin,
            dest=dest,
        )

    assert coords is None
    assert discoverer._en_route_stop_geocode_was_rejected_out_of_region(
        "Stan's Overlook Trail",
        origin_name="Zion National Park",
        dest_name="Bryce Canyon National Park",
    ) is True
    # A totally different, never-attempted name must not be flagged.
    assert discoverer._en_route_stop_geocode_was_rejected_out_of_region(
        "Some Other Stop",
        origin_name="Zion National Park",
        dest_name="Bryce Canyon National Park",
    ) is False


def test_geocode_result_name_plausible_rejects_different_named_place() -> None:
    """Unit-level check on the name-plausibility helper itself, grounded in
    live-captured Nominatim data (2026-08-18) for the real dipstick69 bug:
    querying 'Rockville Historic District' with a St. George->Zion route
    viewbox returns a real OSM entry named 'Grafton Historic DIstrict' at
    (37.166804, -113.0864502) -- Grafton Ghost Town's actual location, a
    separate en-route stop ~3 road miles from Rockville. A naive
    "shares no token" check would wrongly pass this because both names
    share 'historic'/'district'; the real identifying words ('rockville'
    vs 'grafton') never overlap and must be what's compared."""
    grafton_result = {
        "name": "Grafton Historic DIstrict",
        "display_name": "Grafton Historic DIstrict, Rockville, Washington County, Utah, United States",
    }
    assert URLDiscoverer._geocode_result_name_plausible(
        "Rockville Historic District", grafton_result
    ) is False


def test_geocode_result_name_plausible_accepts_superset_variant() -> None:
    """A result name that's a reasonable superset/variant of the query (not a
    clearly different place) must still pass -- e.g. 'Sunrise Point'
    legitimately resolving to a result named 'Sunrise Point Overlook'."""
    result = {"name": "Sunrise Point Overlook", "display_name": "Sunrise Point Overlook, Bryce Canyon, UT"}
    assert URLDiscoverer._geocode_result_name_plausible("Sunrise Point", result) is True


def test_geocode_result_name_plausible_rejects_oversized_waterway_truncation_match() -> None:
    """Real dipstick75 bug (St. George -> Zion leg): en-route stop "Virgin
    River Petroglyph Site" has no Nominatim entry under its full name.
    _geocode_en_route_stop_for_route's progressive-truncation fallback
    retries with "Virgin River Petroglyph" (also no match, live-verified)
    and then "Virgin River" -- which resolves to the Virgin River itself
    (OSM relation 10605393, live Nominatim response 2026-08-18): a real
    place whose displayed reverse-geocoded address is "Veterans Memorial
    Highway, Mohave County, Arizona" -- ~12 mi from St. George and nowhere
    near the real BLM petroglyph site the stop refers to.

    The anchor-overlap check alone passes this: "Virgin River" is, by
    construction of the truncation that produced it, always a literal token
    subset of "Virgin River Petroglyph Site", so "virgin"/"river" trivially
    overlap no matter how much real identity ("Petroglyph Site") was
    dropped to reach the match -- a structurally different gap from
    Rockville/Grafton (zero token overlap there). Only the oversized-
    waterway check (real bounding box, ~114 mi diagonal) catches this."""
    virgin_river_result = {
        "name": "Virgin River",
        "display_name": "Virgin River, Arizona, United States",
        "class": "waterway",
        "type": "river",
        "boundingbox": ["36.1457610", "37.2935220", "-114.4173887", "-112.9404080"],
    }
    assert URLDiscoverer._geocode_result_name_plausible(
        "Virgin River Petroglyph Site", virgin_river_result
    ) is False


def test_geocode_result_name_plausible_accepts_small_waterway_truncation_match() -> None:
    """No-regression counterpart to the oversized-waterway rejection above:
    the module's own pre-existing, intentional truncation-recovery case for
    "Willis Creek Slot Canyon Trailhead" -> "Willis Creek" also resolves to
    a `class: waterway` Nominatim result (live-verified) -- a bare
    "reject waterway class" rule would silently break this real, documented
    recovery (see test_geocode_en_route_stop_recovers_real_landmark_behind_
    descriptive_suffix). The real distinguishing signal is bounding-box
    size: Willis Creek's real bounding box is ~10.8 mi diagonal (live
    Nominatim data, Bryce Canyon area), far under the Virgin River case's
    ~114 mi -- so this legitimate match must still pass."""
    willis_creek_result = {
        "name": "Willis Creek",
        "display_name": "Willis Creek, Kane County, Utah, United States",
        "class": "waterway",
        "type": "stream",
        "boundingbox": ["37.4732287", "37.5535466", "-112.2339599", "-112.0653402"],
    }
    assert URLDiscoverer._geocode_result_name_plausible(
        "Willis Creek Slot Canyon Trailhead", willis_creek_result
    ) is True


def test_geocode_result_name_plausible_accepts_confluence_park_same_name_collision() -> None:
    """Documents a real, investigated, but NOT code-fixable sibling bug from
    the same dipstick75 report: en-route stop "Confluence Park" (AI
    description: "riverfront trails and historic site at Virgin River",
    referring to the well-known St. George park at the Santa Clara/Virgin
    River confluence, ~37.07 N -113.62 W) geocoded instead to a real but
    different, same-named "Confluence Park" -- a small leisure=park way in
    the Bloomington Hills neighborhood (37.0745819, -113.5829350, live
    Nominatim data), ~2 mi from the intended location and reverse-geocoding
    to a private residence's address there.

    Live investigation (2026-08-18) confirmed this is a genuine OSM/
    Nominatim data limitation, not a code bug: querying "Confluence Park,
    St. George, Utah" (even at limit=10) returns exactly one result --
    the Bloomington Hills park -- and neither a broad "park"/"confluence"
    search restricted to a tight viewbox around the real confluence
    location, nor a reverse-geocode of that location, turns up any
    alternate OSM entity distinctly named "Confluence Park" there. There is
    no second, better candidate for a plausibility/disambiguation check to
    prefer, and the name match itself is exact and unambiguous, so no
    name-token heuristic can or should reject it -- this is pinned here as
    a documented, confirmed limitation of the underlying geocoding data
    source, not left as an unexplained gap."""
    confluence_park_result = {
        "name": "Confluence Park",
        "display_name": "Confluence Park, Bloomington Hills, St. George, Washington County, Utah, United States",
        "class": "leisure",
        "type": "park",
        "boundingbox": ["37.0740480", "37.0751066", "-113.5839812", "-113.5816287"],
    }
    assert URLDiscoverer._geocode_result_name_plausible(
        "Confluence Park", confluence_park_result
    ) is True


def test_geocode_en_route_stop_rejects_name_mismatched_viewbox_match() -> None:
    """Full-pipeline regression for the real dipstick69 bug: en-route stop
    'Rockville Historic District' (a real Rockville, UT designation with no
    distinctly-tagged OSM entry) geocoded -- via the route-viewbox-restricted
    Nominatim search -- to (37.166804, -113.0864502), which is actually the
    separate, neighbouring 'Grafton Historic DIstrict' entry (~3 road miles
    away, a different en-route stop on the same St. George -> Zion leg). The
    rendered Google Maps directions URL then listed 'Grafton Ghost Town' as a
    waypoint twice (once via its own name-string fallback, once via
    Rockville's mis-geocoded coordinates). The existing distance-from-route-
    midpoint sanity check does not catch this -- Grafton is well within the
    route viewbox -- only a name-plausibility check does."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._url_validator = MagicMock()

    origin = (37.0965, -113.5684)  # St. George, UT
    dest = (37.2982, -113.0263)  # Zion National Park

    def fake_get(_url, params=None, **_kwargs):
        # Every attempt (viewbox-biased and unrestricted) resolves to the
        # same wrong, name-mismatched Grafton entry -- mirrors the real
        # captured Nominatim behavior for this exact query/route.
        return _make_nominatim_response_named(
            "37.1668040",
            "-113.0864502",
            name="Grafton Historic DIstrict",
            display_name="Grafton Historic DIstrict, Rockville, Washington County, Utah, United States",
        )

    discoverer._url_validator.session.get.side_effect = fake_get

    with patch("generator.url_discovery.time.sleep"):
        coords = discoverer._geocode_en_route_stop_for_route(
            "Rockville Historic District",
            origin_name="St. George",
            dest_name="Zion National Park",
            origin=origin,
            dest=dest,
        )

    assert coords is None


def test_geocode_en_route_stop_rejects_virgin_river_oversized_waterway_truncation_match() -> None:
    """Full-pipeline regression for the real dipstick75 bug (St. George ->
    Zion leg): en-route stop "Virgin River Petroglyph Site" published with
    its badge-map coordinate as the raw pair (36.9670932, -113.7249242) --
    reverse-geocoding live to "Veterans Memorial Highway, Mohave County,
    Arizona", a different state, nowhere near the real BLM petroglyph site
    (~37.05 N -113.53 W). Root cause traced live: Nominatim has no entry for
    the stop's full name or "Virgin River Petroglyph" (both verified empty),
    but the final truncation "Virgin River" resolves to the Virgin River
    itself (`class: waterway`, real ~114 mi-diagonal bounding box) --
    exactly reproduced below with the real captured response shape. Before
    the oversized-waterway fix this mock would have returned that
    coordinate; now the truncation match is rejected and geocoding
    correctly gives up (no other real candidate exists for this name)."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._url_validator = MagicMock()

    origin = (37.0965, -113.5684)  # St. George, UT
    dest = (37.2982, -113.0263)  # Zion National Park (Springdale)

    def fake_get(_url, params=None, **_kwargs):
        q = str((params or {}).get("q", "")).strip().lower()
        if q == "virgin river":
            return _make_nominatim_response_full(
                "36.9670932",
                "-113.7249242",
                name="Virgin River",
                display_name="Virgin River, Arizona, United States",
                osm_class="waterway",
                osm_type="river",
                boundingbox=["36.1457610", "37.2935220", "-114.4173887", "-112.9404080"],
            )
        # The stop's full name and the first (less-truncated) fallback both
        # have no real Nominatim entry -- live-verified.
        return _make_nominatim_empty_response()

    discoverer._url_validator.session.get.side_effect = fake_get

    with patch("generator.url_discovery.time.sleep"):
        coords = discoverer._geocode_en_route_stop_for_route(
            "Virgin River Petroglyph Site",
            origin_name="St. George",
            dest_name="Zion National Park",
            origin=origin,
            dest=dest,
        )

    assert coords is None


def test_prune_en_route_stops_drops_stop_confirmed_out_of_region() -> None:
    """End-to-end regression for the exact dipstick55 Theme A report: 'Stan's
    Overlook Trail' resolving to Snoqualmie, WA must be removed from the
    en-route stop list for a Zion -> Bryce Canyon leg, not merely excluded
    from waypoint ordering while still rendering its own stop card/link."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._decision_threads_by_destination = {}
    discoverer._decision_stats_by_destination = {}
    discoverer._decision_source_stats_by_destination = {}
    discoverer._decision_event_sequence = 0
    discoverer._request_cache_lock = Lock()

    origin = (37.1889, -112.9986)
    dest = (37.5930, -112.1871)
    snoqualmie_wa = ("47.5301", "-121.8253")
    mesquite_on_route = ("36.8055", "-114.0672")

    def fake_get(_url, params=None, **_kwargs):
        q = str((params or {}).get("q", ""))
        if "Mesquite" in q:
            if params.get("bounded") == 1:
                return _make_nominatim_response(*mesquite_on_route)
            return _make_nominatim_empty_response()
        # "Stan's Overlook Trail": nothing found in-region (viewbox), only
        # an unrestricted match clear across the country.
        if params.get("bounded") == 1:
            return _make_nominatim_empty_response()
        return _make_nominatim_response(*snoqualmie_wa)

    discoverer._url_validator = MagicMock()
    discoverer._url_validator.session.get.side_effect = fake_get

    stops = [
        {"name": "Stan's Overlook Trail"},
        {"name": "Mesquite"},
    ]

    with patch("generator.url_discovery.time.sleep"):
        with patch.object(
            discoverer,
            "_geocode_en_route_stop_for_route",
            wraps=discoverer._geocode_en_route_stop_for_route,
        ):
            with patch.object(discoverer, "_route_progress_ratio", side_effect=lambda **_k: 0.4):
                result = discoverer._prune_en_route_stops_by_geometry(
                    stops=stops,
                    origin_name="Zion National Park",
                    dest_name="Bryce Canyon National Park",
                    origin_lat=origin[0],
                    origin_lng=origin[1],
                    dest_lat=dest[0],
                    dest_lng=dest[1],
                )

    names = [str(stop.get("name", "") or "") for stop in result]
    assert "Stan's Overlook Trail" not in names
    assert "Mesquite" in names


def test_prune_en_route_stops_by_geometry_logs_far_off_corridor_stops_without_dropping_them() -> None:
    """dipstick62: on a single real Torrey->Moab leg, "Lake Powell / Hite
    Crossing" (37.9 mi off the straight origin->destination line) and
    "Sego Canyon" (progress ~0.98, essentially at Moab itself) both
    rendered as waypoints on the same leg -- Google Maps' resulting
    directions came out to 706 miles / 13h50m for what should be roughly
    140 miles / 2.5-3 hours.

    A hard perpendicular-distance cutoff was tried and reverted: the real,
    commonly-used I-70 route through Green River to Moab (an established-
    legitimate stop -- see test_discover_en_route_stops_uses_geocoded_
    coordinates_for_maps_url, "Swasey's Beach", dipstick55 Theme E) sits
    35.4 mi off the same straight line -- only 2.5 mi closer than Lake
    Powell. Real highways bend that far around terrain; no straight-line
    threshold can safely separate the two without actual road-network
    routing data. So: Lake Powell stays kept (an outlier this codebase
    can't yet safely auto-reject) but is logged for visibility. Sego
    Canyon is dropped by the *pre-existing* at-destination check (progress
    ~0.98, ~98% of the leg's own distance from origin) -- a different,
    unrelated mechanism, not the new diagnostic logging."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._decision_threads_by_destination = {}
    discoverer._decision_stats_by_destination = {}
    discoverer._decision_source_stats_by_destination = {}
    discoverer._decision_event_sequence = 0
    discoverer._request_cache_lock = Lock()

    origin = (38.3021, -111.4188)  # Torrey (Capitol Reef)
    dest = (38.5733, -109.5498)  # Moab

    stops = [
        {"name": "Lake Powell / Hite Crossing"},
        {"name": "Sego Canyon"},
        {"name": "Close Highway Overlook"},
    ]
    geocodes = {
        "Lake Powell / Hite Crossing": (37.8721, -110.3822),
        "Sego Canyon": (38.9800, -109.6800),
        "Close Highway Overlook": (38.35, -111.20),
    }

    def fake_geocode(stop_name, **_kwargs):
        return geocodes.get(stop_name)

    with patch.object(discoverer, "_geocode_en_route_stop_for_route", side_effect=fake_geocode):
        result = discoverer._prune_en_route_stops_by_geometry(
            stops=stops,
            origin_name="Capitol Reef National Park",
            dest_name="Moab",
            origin_lat=origin[0],
            origin_lng=origin[1],
            dest_lat=dest[0],
            dest_lng=dest[1],
        )

    names = [str(stop.get("name", "") or "") for stop in result]
    assert "Lake Powell / Hite Crossing" in names
    assert "Sego Canyon" not in names
    assert "Close Highway Overlook" in names


def test_prune_en_route_stops_by_geometry_logs_diagnostic_for_far_off_corridor_stop() -> None:
    """Direct unit check that the far-off-corridor diagnostic logging call
    actually fires with the expected reason code, without depending on the
    internal disposition-thread storage structure."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)

    origin = (38.3021, -111.4188)
    dest = (38.5733, -109.5498)
    stops = [{"name": "Lake Powell / Hite Crossing"}]

    with patch.object(
        discoverer, "_geocode_en_route_stop_for_route", return_value=(37.8721, -110.3822)
    ):
        with patch.object(discoverer, "_log_decision") as mock_log:
            discoverer._prune_en_route_stops_by_geometry(
                stops=stops,
                origin_name="Capitol Reef National Park",
                dest_name="Moab",
                origin_lat=origin[0],
                origin_lng=origin[1],
                dest_lat=dest[0],
                dest_lng=dest[1],
            )

    reasons = [call.kwargs.get("reason") for call in mock_log.call_args_list]
    assert "en_route_far_off_straight_line_kept" in reasons


def test_route_perpendicular_distance_miles_matches_expected_offsets() -> None:
    """Direct unit test of the new lateral-distance helper against the same
    real coordinates as the pruning test above, pinning the approximate
    mile values so a future change to the scaling can't silently regress
    without a visible assertion failure."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    origin = (38.3021, -111.4188)
    dest = (38.5733, -109.5498)

    lake_powell = discoverer._route_perpendicular_distance_miles(
        origin=origin, dest=dest, point=(37.8721, -110.3822)
    )
    sego = discoverer._route_perpendicular_distance_miles(
        origin=origin, dest=dest, point=(38.9800, -109.6800)
    )
    close = discoverer._route_perpendicular_distance_miles(
        origin=origin, dest=dest, point=(38.35, -111.20)
    )

    assert 35.0 < lake_powell < 45.0
    assert 25.0 < sego < 33.0
    assert close < 3.0


def test_prune_en_route_stops_by_geometry_drops_stop_coincident_with_destination_coordinates() -> None:
    """Defense-in-depth regression for the Bryce -> Capitol Reef 'Capitol
    Reef National Park appears twice, right before the real destination'
    bug from the project owner's Google Maps screenshot: even when the
    progress-ratio math doesn't flag a stop as 'at destination' (e.g. a
    non-straight-line route bends the origin->destination projection), a
    stop that geocodes to essentially the same point as the destination
    itself is never a genuine en-route detour and must be dropped outright,
    not merely deprioritized in waypoint ordering."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)

    origin = (37.5707948, -112.1855939)  # Bryce Canyon National Park
    dest = (38.0670286, -111.1552562)  # Capitol Reef National Park
    stop_coords = (38.0665, -111.1560)  # effectively the same point as dest

    stops = [{"name": "Red Canyon"}]

    with patch.object(discoverer, "_geocode_en_route_stop_for_route", return_value=stop_coords):
        with patch.object(discoverer, "_route_progress_ratio", return_value=0.5):
            result = discoverer._prune_en_route_stops_by_geometry(
                stops=stops,
                origin_name="Bryce Canyon National Park",
                dest_name="Capitol Reef National Park",
                origin_lat=origin[0],
                origin_lng=origin[1],
                dest_lat=dest[0],
                dest_lng=dest[1],
            )

    assert result == []


def test_en_route_stop_name_truncations_drops_one_word_at_a_time() -> None:
    """Unit coverage for the progressive-truncation helper itself: it must
    drop exactly one trailing word per step, cap at max_variants, and never
    shrink below min_words."""
    assert URLDiscoverer._en_route_stop_name_truncations("Willis Creek Slot Canyon Trailhead") == [
        "Willis Creek Slot Canyon",
        "Willis Creek Slot",
        "Willis Creek",
    ]
    assert URLDiscoverer._en_route_stop_name_truncations("Coral Pink Sand Dunes State Park Boardwalk") == [
        "Coral Pink Sand Dunes State Park",
        "Coral Pink Sand Dunes State",
        "Coral Pink Sand Dunes",
    ]
    # Already at (or below) min_words: nothing to drop.
    assert URLDiscoverer._en_route_stop_name_truncations("Red Canyon") == []
    assert URLDiscoverer._en_route_stop_name_truncations("Solo") == []


def test_geocode_en_route_stop_recovers_real_landmark_behind_descriptive_suffix() -> None:
    """Regression for dipstick59 Bug 1 (real Zion -> Bryce Canyon leg).

    Real screenshot from the project owner: Google's driving route for this
    leg zigzagged between two geographic clusters (Cedar City area vs. Kanab
    area) three times instead of visiting each once. Investigation traced
    this to _geocode_en_route_stop_for_route silently failing for 3 of the 5
    real en-route stops -- "Cedar Breaks National Monument Rim View",
    "Coral Pink Sand Dunes State Park Boardwalk", "Willis Creek Slot Canyon
    Trailhead" -- because Nominatim's free-text search requires
    (approximately) every significant word to match, and none of those exact
    compound strings are in its index (verified live against Nominatim
    during investigation). The real landmark underneath each ("Cedar Breaks
    National Monument", "Coral Pink Sand Dunes State Park", "Willis Creek")
    geocodes cleanly. Without a resolved coordinate, these 3 stops got no
    route_progress_ratio at all and piled up at the end of the waypoint
    list in AI-harvest order instead of their real geographic position --
    reproducing the exact zigzag from the screenshot. The fix retries with
    progressively word-truncated queries before giving up.
    """
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._url_validator = MagicMock()

    origin = (37.3221673, -113.0047934)  # Zion Canyon Visitor Center
    dest = (37.5707948, -112.1855939)  # Bryce Canyon City

    # Real, live-verified Nominatim results: the exact AI-generated name
    # (with its descriptive suffix) returns nothing; the truncated landmark
    # name underneath it resolves correctly.
    resolves = {
        "cedar breaks national monument": ("37.6387738", "-112.8447452"),
        "willis creek": ("37.5047232", "-112.1575941"),
    }

    def fake_get(_url, params=None, **_kwargs):
        q = str((params or {}).get("q", "")).strip().lower()
        if params and params.get("bounded") == 1 and q in resolves:
            return _make_nominatim_response(*resolves[q])
        return _make_nominatim_empty_response()

    discoverer._url_validator.session.get.side_effect = fake_get

    with patch("generator.url_discovery.time.sleep"):
        cedar_breaks = discoverer._geocode_en_route_stop_for_route(
            "Cedar Breaks National Monument Rim View",
            origin_name="Zion National Park",
            dest_name="Bryce Canyon National Park",
            origin=origin,
            dest=dest,
        )
        willis_creek = discoverer._geocode_en_route_stop_for_route(
            "Willis Creek Slot Canyon Trailhead",
            origin_name="Zion National Park",
            dest_name="Bryce Canyon National Park",
            origin=origin,
            dest=dest,
        )

    assert cedar_breaks == (37.6387738, -112.8447452)
    assert willis_creek == (37.5047232, -112.1575941)


def test_prune_en_route_stops_resolves_ratio_for_real_dipstick59_stops() -> None:
    """End-to-end regression for dipstick59 Bug 1: with the progressive-
    truncation geocoding fallback, the real Zion -> Bryce en-route stops
    whose AI-generated name previously failed to geocode ("Cedar Breaks
    National Monument Rim View", "Coral Pink Sand Dunes State Park
    Boardwalk") now resolve real coordinates and a real route_progress_ratio,
    instead of silently falling back to the buggy 'sorts last' behavior that
    produced the reported zigzag (Parowan Gap -> Moqui Cave -> Cedar Breaks
    -> Coral Pink -> Willis Creek, crossing between the Cedar City and Kanab
    clusters three times).

    "Willis Creek Slot Canyon Trailhead" also now resolves real coordinates
    (verified live against Nominatim: "Willis Creek", Kane County, UT) --
    but those real coordinates place it only ~4.8 miles from Bryce Canyon
    City, 48.17 of the leg's 48.11 route-miles from the origin (progress
    ratio ~0.996). That trips the pre-existing "at/past the destination"
    geometry filter a few lines below in _prune_en_route_stops_by_geometry
    (same filter dipstick58's fix relies on for stops literally inside the
    next destination) and the stop is dropped from this leg's en-route list
    entirely -- consistent, intentional behavior for a stop this close to
    the destination, not a side effect specific to this fix.
    """
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._decision_threads_by_destination = {}
    discoverer._decision_stats_by_destination = {}
    discoverer._decision_source_stats_by_destination = {}
    discoverer._decision_event_sequence = 0
    discoverer._request_cache_lock = Lock()

    origin = (37.3221673, -113.0047934)
    dest = (37.5707948, -112.1855939)

    resolves = {
        "parowan gap petroglyphs": ("37.9094137", "-112.9848435"),
        "moqui cave": ("37.1207779", "-112.5638010"),
        "cedar breaks national monument": ("37.6387738", "-112.8447452"),
        "coral pink sand dunes state park": ("37.0405873", "-112.7131482"),
        "willis creek": ("37.5047232", "-112.1575941"),
    }

    def fake_get(_url, params=None, **_kwargs):
        q = str((params or {}).get("q", "")).strip().lower()
        if params and params.get("bounded") == 1 and q in resolves:
            return _make_nominatim_response(*resolves[q])
        return _make_nominatim_empty_response()

    discoverer._url_validator = MagicMock()
    discoverer._url_validator.session.get.side_effect = fake_get

    stops = [
        {"name": "Parowan Gap Petroglyphs"},
        {"name": "Moqui Cave"},
        {"name": "Cedar Breaks National Monument Rim View"},
        {"name": "Coral Pink Sand Dunes State Park Boardwalk"},
        {"name": "Willis Creek Slot Canyon Trailhead"},
    ]

    with patch("generator.url_discovery.time.sleep"):
        result = discoverer._prune_en_route_stops_by_geometry(
            stops=stops,
            origin_name="Zion National Park",
            dest_name="Bryce Canyon National Park",
            origin_lat=origin[0],
            origin_lng=origin[1],
            dest_lat=dest[0],
            dest_lng=dest[1],
        )

    by_name = {str(stop.get("name", "")): stop for stop in result}
    for stop_name in [
        "Parowan Gap Petroglyphs",
        "Moqui Cave",
        "Cedar Breaks National Monument Rim View",
        "Coral Pink Sand Dunes State Park Boardwalk",
    ]:
        stop = by_name[stop_name]
        assert stop.get("route_waypoint_eligible") is True, stop_name
        assert isinstance(stop.get("route_progress_ratio"), float), (
            f"{stop_name} must have a resolved route_progress_ratio now that its "
            "underlying landmark name geocodes via the truncation fallback"
        )

    # See the docstring above: this one is correctly pruned as at/past the
    # destination once its real coordinates are known, not silently dropped.
    assert "Willis Creek Slot Canyon Trailhead" not in by_name


def test_alltrails_confidence_boosted_to_high_when_corroborating_search_agrees() -> None:
    """Corroboration piece: a blocked-fetch 'medium' confidence trail must be
    promoted to 'high' when an independent secondary lookup (opt-in via the
    existing filtered-selection flag) points at the exact same canonical page --
    this reuses the existing veto's plumbing as a positive signal instead of
    only a negative one."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._enable_filtered_alltrails_selection = True
    url = "https://www.alltrails.com/trail/us/utah/angels-landing-trail"

    with patch.object(discoverer, "_alltrails_slug_matches_item", return_value=True):
        with patch.object(discoverer, "_alltrails_slug_has_numbered_suffix", return_value=False):
            with patch.object(discoverer, "_alltrails_slug_extra_term_count", return_value=0):
                with patch.object(discoverer, "_fetch_page_text", return_value=(False, 403, "")):
                    with patch.object(discoverer, "_get_filtered_alltrails_selection", return_value=url):
                        confidence = discoverer._alltrails_confidence_level(url, "Angels Landing", "Zion National Park")

    assert confidence == "high"


def test_alltrails_confidence_promoted_to_high_for_extra_term_slug_when_corroborated() -> None:
    """Regression for dipstick58: Zion "The Narrows" resolved to the real,
    correct, slug-matched candidate https://www.alltrails.com/trail/us/utah/
    the-narrows-top-down via seed-relaxed search -- but it was silently
    dropped (no disposition-log entry; rejection only visible via
    _log_rejected_url's plain logger.warning) because AllTrails' bot-blocked
    fetch could not confirm it and the slug's "top down" qualifier counts as
    one extra term beyond item tokens {"narrow"}, which previously routed
    straight to "low" with no chance to corroborate at all. An independent,
    differently-queried search landing on the exact same URL must still be
    able to rescue it.
    """
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._enable_filtered_alltrails_selection = True
    url = "https://www.alltrails.com/trail/us/utah/the-narrows-top-down"

    with patch.object(discoverer, "_alltrails_slug_matches_item", return_value=True):
        with patch.object(discoverer, "_alltrails_slug_has_numbered_suffix", return_value=False):
            with patch.object(discoverer, "_fetch_page_text", return_value=(False, 403, "")):
                with patch.object(discoverer, "_get_filtered_alltrails_selection", return_value=url):
                    confidence = discoverer._alltrails_confidence_level(url, "The Narrows", "Zion National Park")

    assert confidence == "high"


def test_alltrails_confidence_stays_low_for_extra_term_slug_without_corroboration_match() -> None:
    """The extra-term relief must not become a blanket default: if the
    independent corroboration search disagrees (or finds nothing), a
    multi-extra-term slug stays "low" and gets rejected -- this is what keeps
    the fix from reopening the Theme B wrong-trail-link bug (dipstick55).
    """
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._enable_filtered_alltrails_selection = True
    url = "https://www.alltrails.com/trail/us/utah/the-narrows-top-down"

    with patch.object(discoverer, "_alltrails_slug_matches_item", return_value=True):
        with patch.object(discoverer, "_alltrails_slug_has_numbered_suffix", return_value=False):
            with patch.object(discoverer, "_fetch_page_text", return_value=(False, 403, "")):
                with patch.object(discoverer, "_get_filtered_alltrails_selection", return_value=None):
                    confidence = discoverer._alltrails_confidence_level(url, "The Narrows", "Zion National Park")

    assert confidence == "low"


def test_alltrails_confidence_stays_medium_without_corroboration_opt_in() -> None:
    """The boost must be opt-in (same flag as the existing filtered-selection
    search) since it costs one extra search call per borderline candidate --
    it must not fire by default."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._enable_filtered_alltrails_selection = False
    url = "https://www.alltrails.com/trail/us/utah/angels-landing-trail"

    with patch.object(discoverer, "_alltrails_slug_matches_item", return_value=True):
        with patch.object(discoverer, "_alltrails_slug_has_numbered_suffix", return_value=False):
            with patch.object(discoverer, "_alltrails_slug_extra_term_count", return_value=0):
                with patch.object(discoverer, "_fetch_page_text", return_value=(False, 403, "")):
                    with patch.object(
                        discoverer,
                        "_get_filtered_alltrails_selection",
                        side_effect=AssertionError("must not be called when corroboration is disabled"),
                    ):
                        confidence = discoverer._alltrails_confidence_level(url, "Angels Landing", "Zion National Park")

    assert confidence == "medium"


def test_alltrails_confidence_demoted_to_low_when_corroboration_finds_no_match() -> None:
    """Regression for dipstick60: Capitol Reef "Water Tanks via Capitol Gorge"
    resolved to https://www.alltrails.com/trail/us/utah/water-tanks-via-capitol-gorge-
    and-tanks-trail -- a slug the direct-batch harvest LLM invented outright (it
    never appeared in any real AllTrails search result) -- and it still reached
    "medium" confidence and got published as a live link (it 404s) because
    AllTrails' bot-blocking makes the 403'd primary liveness check unable to ever
    affirmatively confirm *or* deny a candidate, and the old fail-open default left
    unconfirmed slug-matched candidates at "medium" even when the independent
    corroboration search (opt-in, on by default in config.yaml) came back empty.
    With corroboration opt-in enabled but returning no match, confidence must now
    fall through to "low" -- below the "medium" publish threshold -- instead of
    silently staying at "medium".
    """
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._enable_filtered_alltrails_selection = True
    url = "https://www.alltrails.com/trail/us/utah/water-tanks-via-capitol-gorge-and-tanks-trail"

    with patch.object(discoverer, "_alltrails_slug_matches_item", return_value=True):
        with patch.object(discoverer, "_alltrails_slug_has_numbered_suffix", return_value=False):
            with patch.object(discoverer, "_alltrails_slug_extra_term_count", return_value=0):
                with patch.object(discoverer, "_fetch_page_text", return_value=(False, 403, "")):
                    with patch.object(discoverer, "_verify_url_cached", return_value=(False, 403)):
                        with patch.object(discoverer, "_get_filtered_alltrails_selection", return_value=None):
                            confidence = discoverer._alltrails_confidence_level(
                                url, "Water Tanks via Capitol Gorge", "Capitol Reef National Park"
                            )

    assert confidence == "low"


def test_alltrails_confidence_demoted_to_low_when_corroboration_raises() -> None:
    """A corroboration-search failure (network/API error) must fail closed the
    same way a clean no-match result does -- an exception is not evidence the
    candidate is correct, so it must not leave confidence fail-open at
    "medium"."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._enable_filtered_alltrails_selection = True
    url = "https://www.alltrails.com/trail/us/utah/angels-landing-trail"

    with patch.object(discoverer, "_alltrails_slug_matches_item", return_value=True):
        with patch.object(discoverer, "_alltrails_slug_has_numbered_suffix", return_value=False):
            with patch.object(discoverer, "_alltrails_slug_extra_term_count", return_value=0):
                with patch.object(discoverer, "_fetch_page_text", return_value=(False, 403, "")):
                    with patch.object(discoverer, "_verify_url_cached", return_value=(False, 403)):
                        with patch.object(
                            discoverer,
                            "_get_filtered_alltrails_selection",
                            side_effect=RuntimeError("search backend unavailable"),
                        ):
                            confidence = discoverer._alltrails_confidence_level(
                                url, "Angels Landing", "Zion National Park"
                            )

    assert confidence == "low"


def test_alltrails_confidence_reaches_medium_via_broad_search_when_filtered_search_has_no_match() -> None:
    """Tier-2 of the blocked-exact-slug-match gate: the narrow, metadata-
    filtered corroboration search (_get_filtered_alltrails_selection)
    requires a full rating/reviews/difficulty/mileage snippet and
    family-hike-policy compliance, so it routinely returns no match even for
    genuinely correct trails (e.g. ones outside the difficulty/mileage/
    review-count policy, or with a snippet missing a metric). This must not
    be treated the same as "no corroboration at all" -- when the broader,
    unfiltered site:alltrails.com search (the same mechanism
    _search_alltrails_for_trail() itself treats as authoritative discovery in
    non-direct-batch mode) independently resolves to this exact URL, that is
    still real corroboration and must reach "medium" (publish-eligible), not
    fall all the way to "low". This is the counterpart to the
    fabricated-slug regression above: proof the fix doesn't just flip the bug
    to the opposite failure mode of losing genuinely correct AllTrails links
    that AllTrails itself also routinely bot-blocks.
    """
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._enable_filtered_alltrails_selection = True
    url = "https://www.alltrails.com/trail/us/utah/navajo-loop-trail"

    with patch.object(discoverer, "_alltrails_slug_matches_item", return_value=True):
        with patch.object(discoverer, "_alltrails_slug_has_numbered_suffix", return_value=False):
            with patch.object(discoverer, "_alltrails_slug_extra_term_count", return_value=0):
                with patch.object(discoverer, "_fetch_page_text", return_value=(False, 403, "")):
                    with patch.object(discoverer, "_get_filtered_alltrails_selection", return_value=None):
                        with patch.object(discoverer, "_search_first", return_value=url):
                            confidence = discoverer._alltrails_confidence_level(
                                url, "Navajo Loop Trail", "Bryce Canyon National Park"
                            )

    assert confidence == "medium"


def test_trail_like_attraction_gets_maps_fallback_when_alltrails_confidence_below_threshold() -> None:
    """Regression using a real affected name from the SW2026-dipstick64 run:
    Zion's "Canyon Overlook Trail" rendered with no url and no maps_url
    because authoritative mode's trail-like branch (default -- see
    DEFAULT_DIRECT_BATCH_AUTHORITATIVE) unconditionally cleared both fields
    with `attr["url"] = ""; attr.pop("maps_url", None); continue` when it
    ran out of AllTrails/NPS-fanout/batch-recovery options, instead of
    falling through to the same safe maps-search fallback the
    "not authoritative" case in this same branch already used
    (`trail_maps_fallback_assigned`) and the non-trail branch got in the
    earlier `fe0024a` fix. It must still not use the below-confidence-
    threshold AllTrails URL itself.
    """
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._alltrails_min_confidence_for_publish = "high"

    ai = {
        "top_attractions": [
            {
                "name": "Canyon Overlook Trail",
                "type": "hike",
                "description": "Short trail with views.",
            }
        ]
    }

    with patch.object(
        discoverer,
        "_search_alltrails_for_trail",
        return_value="https://www.alltrails.com/trail/us/utah/canyon-overlook-trail",
    ):
        with patch.object(discoverer, "_fetch_page_text", return_value=(False, 403, "")):
            with patch.object(discoverer, "_search_attraction_from_direct_batch", return_value=None):
                discoverer._discover_attractions(ai, "Zion National Park", "zion")

    attr = ai["top_attractions"][0]
    assert attr.get("url") == (
        "https://www.google.com/maps/search/?api=1&query=Canyon%20Overlook%20Trail%20Zion%20National%20Park"
    )
    assert attr.get("maps_url") == attr.get("url")
    assert "alltrails.com" not in attr.get("url", "")
    stats = getattr(discoverer, "_decision_stats_by_destination", {}).get("Zion National Park", {})
    assert stats.get("maps_fallback_assigned", 0) == 1


def test_retain_discovered_url_rejects_low_confidence_alltrails_for_trails():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._alltrails_min_confidence_for_publish = "high"

    with patch.object(discoverer, "_fetch_page_text", return_value=(False, 403, "")):
        retained = discoverer._retain_discovered_url(
            "https://www.alltrails.com/trail/us/utah/canyon-overlook-trail",
            "Canyon Overlook Trail",
            "Zion National Park",
            allow_alltrails=True,
        )

    assert retained == ""


def test_retain_discovered_url_preserves_seed_alltrails_trail_via_relaxed_standard():
    """Regression test for the dipstick60 "Why didn't NPS resolve?" investigation
    into Zion's "The Narrows".

    A seed trail is attached via the relaxed seed-only AllTrails fallback
    (_search_alltrails_for_seed_relaxed), which deliberately accepts long/
    strenuous trails that fail the strict, non-seed-aware length/gain/
    difficulty confidence gate (_meets_alltrails_publish_confidence). Before
    this fix, the later audit_discovered_urls() safety pass called
    _retain_discovered_url() again on every attraction without threading
    is_seed through, so it silently re-rejected the exact URL it had just
    accepted -- AllTrails was bot-blocked (403) and the slug carries an extra
    qualifier ("top-down") that requires corroboration search (which was
    unavailable/inconclusive here) to reach anything above "low" confidence.
    is_seed must exempt the retain-time check the same way it already exempts
    the trail-miles demotion check earlier in the same audit loop.
    """
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._alltrails_min_confidence_for_publish = "medium"
    discoverer._enable_filtered_alltrails_selection = True

    url = "https://www.alltrails.com/trail/us/utah/the-narrows-top-down"

    with patch.object(discoverer, "_fetch_page_text", return_value=(False, 403, "")), \
         patch.object(discoverer, "_verify_url_cached", return_value=(True, 200)), \
         patch.object(discoverer, "_get_filtered_alltrails_selection", return_value=None):
        retained = discoverer._retain_discovered_url(
            url,
            "The Narrows",
            "Zion National Park",
            allow_alltrails=True,
            kind="attraction",
            is_seed=True,
        )

    assert retained == url


def test_retain_discovered_url_rejects_same_alltrails_url_when_not_seed():
    """Contrast case for the seed-relaxed regression above: without is_seed,
    the exact same bot-blocked, extra-slug-term AllTrails URL still hits the
    strict confidence gate and is rejected -- the fix only exempts seeds, it
    does not weaken the general AllTrails confidence policy."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._alltrails_min_confidence_for_publish = "medium"
    discoverer._enable_filtered_alltrails_selection = True

    url = "https://www.alltrails.com/trail/us/utah/the-narrows-top-down"

    with patch.object(discoverer, "_fetch_page_text", return_value=(False, 403, "")), \
         patch.object(discoverer, "_verify_url_cached", return_value=(True, 200)), \
         patch.object(discoverer, "_get_filtered_alltrails_selection", return_value=None):
        retained = discoverer._retain_discovered_url(
            url,
            "The Narrows",
            "Zion National Park",
            allow_alltrails=True,
            kind="attraction",
            is_seed=False,
        )

    assert retained == ""


def test_audit_discovered_urls_preserves_seed_trail_attraction_link() -> None:
    """End-to-end regression through audit_discovered_urls(): a destination's
    seeds list marks "The Narrows" as a seed, so the final audit pass must
    preserve its already-attached AllTrails URL via the relaxed seed standard
    instead of discarding it through the strict confidence gate."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._alltrails_min_confidence_for_publish = "medium"
    discoverer._enable_filtered_alltrails_selection = True
    discoverer._max_trail_miles = 3.0

    url = "https://www.alltrails.com/trail/us/utah/the-narrows-top-down"

    trip = {
        "destinations": [
            {
                "id": "zion",
                "name": "Zion National Park",
                "seeds": ["The Narrows"],
                "ai_content": {
                    "top_attractions": [
                        {
                            "name": "The Narrows",
                            "type": "hike",
                            "description": "An iconic river hike through a slot canyon.",
                            "url": url,
                        }
                    ],
                    "getting_here": {"en_route_stops": []},
                    "dinner_recommendations": [],
                },
                "scenic_drives": [],
                "cultural_events": {"events": []},
            }
        ]
    }

    with patch.object(discoverer, "_prewarm_url_validation_cache", return_value=None), \
         patch.object(discoverer, "_fetch_page_text", return_value=(False, 403, "")), \
         patch.object(discoverer, "_verify_url_cached", return_value=(True, 200)), \
         patch.object(discoverer, "_get_filtered_alltrails_selection", return_value=None):
        discoverer.audit_discovered_urls(trip)

    attractions = trip["destinations"][0]["ai_content"]["top_attractions"]
    assert len(attractions) == 1
    assert attractions[0].get("url") == url


def test_fetch_page_text_caches_alltrails_fetches():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._url_validator = MagicMock()
    discoverer._url_validator.session.get.return_value = MagicMock(
        status_code=200,
        text="Angels Landing trail details",
    )
    discoverer._alltrails_request_delay_seconds = 0
    discoverer._alltrails_block_cooldown_seconds = 0

    url = "https://www.alltrails.com/trail/us/utah/angels-landing-trail"
    first = discoverer._fetch_page_text(url, timeout=8)
    second = discoverer._fetch_page_text(url, timeout=8)

    assert first[0] is True
    assert second[0] is True
    assert discoverer._url_validator.session.get.call_count == 1


def test_verify_url_cached_avoids_duplicate_liveness_calls():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._url_validator = MagicMock()
    discoverer._url_validator.verify_url.return_value = (True, 200)

    url = "https://example.com/entity"
    first = discoverer._verify_url_cached(url)
    second = discoverer._verify_url_cached(url)

    assert first == (True, 200)
    assert second == (True, 200)
    assert discoverer._url_validator.verify_url.call_count == 1


def test_fetch_page_text_caches_non_alltrails_fetches():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._url_validator = MagicMock()
    discoverer._url_validator.get_text.return_value = (True, 200, "entity detail")

    url = "https://example.com/entity"
    first = discoverer._fetch_page_text(url, timeout=8)
    second = discoverer._fetch_page_text(url, timeout=8)

    assert first == (True, 200, "entity detail")
    assert second == (True, 200, "entity detail")
    assert discoverer._url_validator.get_text.call_count == 1


def test_search_cached_avoids_duplicate_grok_query_calls():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._search = MagicMock()
    discoverer._search.search.return_value = [
        {"url": "https://example.com/entity", "name": "Entity", "snippet": "snippet"}
    ]

    query = 'site:nps.gov "Canyon Overlook" Zion National Park'
    first = discoverer._search_cached(query, count=10)
    second = discoverer._search_cached(query, count=10)

    assert len(first) == 1
    assert len(second) == 1
    assert discoverer._search.search.call_count == 1


def test_search_cached_short_circuits_empty_result_while_in_failure_cooldown():
    """Regression: an empty search result used to be cached permanently for
    the run, with no distinction between 'genuinely no results' and 'the
    request failed' (GrokSearch.search() swallows exceptions and returns []
    either way). A single transient failure would poison that query for the
    rest of the run. Within the cooldown window, a repeat call must
    short-circuit without a second network call; the search client itself is
    only ever hit once."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._search_failure_cooldown_seconds = 180.0
    discoverer._search = MagicMock()
    discoverer._search.search.return_value = []

    query = "some obscure query that returns nothing"
    first = discoverer._search_cached(query, count=10)
    second = discoverer._search_cached(query, count=10)

    assert first == []
    assert second == []
    assert discoverer._search.search.call_count == 1


def test_search_cached_retries_after_failure_cooldown_expires():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._search_failure_cooldown_seconds = 0.0
    discoverer._search = MagicMock()
    discoverer._search.search.side_effect = [
        [],
        [{"url": "https://example.com/entity", "name": "Entity", "snippet": "snippet"}],
    ]

    query = "some query"
    first = discoverer._search_cached(query, count=10)
    second = discoverer._search_cached(query, count=10)

    assert first == []
    assert len(second) == 1
    assert discoverer._search.search.call_count == 2


def test_collect_discovered_urls_returns_unique_urls_across_sections():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    trip = {
        "destinations": [
            {
                "ai_content": {
                    "top_attractions": [
                        {"url": "https://example.com/a"},
                    ],
                    "getting_here": {
                        "en_route_stops": [
                            {"url": "https://example.com/b"},
                        ]
                    },
                    "dinner_recommendations": [
                        {"url": "https://example.com/c"},
                    ],
                },
                "scenic_drives": [
                    {"url": "https://example.com/a"},
                ],
                "cultural_events": {
                    "events": [
                        {"url": "https://example.com/d"},
                    ]
                },
            }
        ]
    }

    urls = discoverer._collect_discovered_urls(trip)

    assert urls == {
        "https://example.com/a",
        "https://example.com/b",
        "https://example.com/c",
        "https://example.com/d",
    }


def test_prewarm_url_validation_cache_fetches_unique_non_alltrails_urls_once():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    seen_calls = []

    def _fake_fetch(url, timeout=8):
        seen_calls.append((url, timeout))
        return True, 200, "ok"

    with patch.object(discoverer, "_fetch_page_text", side_effect=_fake_fetch):
        with patch.object(discoverer, "_is_obviously_generic_url", return_value=False):
            trip = {
                "destinations": [
                    {
                        "ai_content": {
                            "top_attractions": [
                                {"url": "https://example.com/x"},
                                {"url": "https://example.com/x"},
                            ],
                            "getting_here": {"en_route_stops": []},
                            "dinner_recommendations": [
                                {"url": "https://www.google.com/maps/search/?api=1&query=test"},
                            ],
                        },
                        "scenic_drives": [
                            {"url": "https://www.alltrails.com/trail/us/utah/canyon-overlook-trail"},
                            {"url": "https://example.com/y"},
                        ],
                        "cultural_events": {"events": []},
                    }
                ]
            }
            discoverer._prewarm_url_validation_cache(trip)

    fetched_urls = sorted(url for (url, _timeout) in seen_calls)
    assert fetched_urls == [
        "https://example.com/x",
        "https://example.com/y",
    ]


def test_prewarm_url_validation_cache_skips_gov_domains():
    """Regression: the audit pass's bulk prewarm used to force a full-content
    fetch over every discovered URL regardless of how much confidence
    discovery already established. An official .gov page doesn't need that
    proactive re-check -- skipping the prewarm doesn't skip verification
    entirely, it just avoids paying for a fetch that's rarely actually
    needed downstream for a source this trustworthy."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._fetch_page_text = MagicMock(side_effect=AssertionError("must not prewarm .gov URLs"))

    with patch.object(discoverer, "_is_obviously_generic_url", return_value=False):
        trip = {
            "destinations": [
                {
                    "ai_content": {
                        "top_attractions": [
                            {"url": "https://www.nps.gov/zion/planyourvisit/kolob-canyons.htm"},
                        ],
                        "getting_here": {"en_route_stops": []},
                        "dinner_recommendations": [],
                    },
                    "scenic_drives": [],
                    "cultural_events": {"events": []},
                }
            ]
        }
        discoverer._prewarm_url_validation_cache(trip)

    discoverer._fetch_page_text.assert_not_called()


def test_prewarm_url_validation_cache_skips_remembered_authoritative_urls():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._direct_batch_authoritative_urls = {"https://www.zionlodge.com/dining/restaurant"}
    discoverer._fetch_page_text = MagicMock(side_effect=AssertionError("must not prewarm authoritative URLs"))

    with patch.object(discoverer, "_is_obviously_generic_url", return_value=False):
        trip = {
            "destinations": [
                {
                    "ai_content": {
                        "top_attractions": [],
                        "getting_here": {"en_route_stops": []},
                        "dinner_recommendations": [
                            {"url": "https://www.zionlodge.com/dining/restaurant"},
                        ],
                    },
                    "scenic_drives": [],
                    "cultural_events": {"events": []},
                }
            ]
        }
        discoverer._prewarm_url_validation_cache(trip)

    discoverer._fetch_page_text.assert_not_called()


def test_search_first_strict_short_circuits_after_high_confidence_match():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._search = MagicMock()

    discoverer._search.search.return_value = [
        {
            "url": "https://www.nps.gov/zion/learn/nature/canyon-overlook-trail.htm",
            "name": "Canyon Overlook Trail - Zion National Park",
            "snippet": "Canyon Overlook Trail in Zion National Park Utah.",
        }
    ]

    with patch.object(discoverer, "_is_specific_result_url", return_value=True):
        with patch.object(discoverer, "_is_relevant_result", return_value=True):
            with patch.object(discoverer, "_verify_url_cached", return_value=(True, 200)):
                with patch.object(discoverer, "_score_candidate_result", return_value=30):
                    result = discoverer._search_first_strict(
                        query_variants=[
                            '"Canyon Overlook Trail" "Zion National Park" trail',
                            '"Canyon Overlook" Zion park hike',
                        ],
                        site_filter="nps.gov",
                        site_hint=None,
                        item_name="Canyon Overlook Trail",
                        dest_name="Zion National Park",
                        allow_alltrails=False,
                    )

    assert result == "https://www.nps.gov/zion/learn/nature/canyon-overlook-trail.htm"
    assert discoverer._search.search.call_count == 1


def test_filtered_alltrails_strategy_prefers_highest_rated_candidate_with_constraints():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._search = MagicMock()
    discoverer._enable_filtered_alltrails_selection = True
    discoverer._max_alltrails_query_attempts = 5
    discoverer._alltrails_filter_max_miles = 3.0
    discoverer._alltrails_filter_max_gain_feet = 300
    discoverer._alltrails_filter_min_reviews = 5
    discoverer._alltrails_filter_allowed_difficulties = ("easy", "moderate", "moderately challenging")

    discoverer._search.search.return_value = [
        {
            "url": "https://www.alltrails.com/trail/us/utah/canyon-overlook-trail",
            "name": "Canyon Overlook Trail",
            "snippet": "moderate 1.0 mi 213 ft elevation gain 4.8 stars 16200 reviews",
        },
        {
            "url": "https://www.alltrails.com/trail/us/utah/lower-emerald-pool-trail",
            "name": "Lower Emerald Pool Trail",
            "snippet": "moderately challenging 1.3 mi 120 ft elevation gain 4.6 stars 3924 reviews",
        },
    ]

    with patch.object(discoverer, "_prefer_canonical_alltrails_url", side_effect=lambda url, _name: url):
        url = discoverer._search_alltrails_for_trail("Canyon Overlook Trail", "Zion National Park")

    assert url == "https://www.alltrails.com/trail/us/utah/canyon-overlook-trail"


def test_filtered_alltrails_strategy_rejects_candidates_outside_constraints():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._search = MagicMock()
    discoverer._enable_filtered_alltrails_selection = True
    discoverer._max_alltrails_query_attempts = 5
    discoverer._alltrails_filter_max_miles = 3.0
    discoverer._alltrails_filter_max_gain_feet = 300
    discoverer._alltrails_filter_min_reviews = 5
    discoverer._alltrails_filter_allowed_difficulties = ("easy", "moderate", "moderately challenging")

    discoverer._search.search.return_value = [
        {
            "url": "https://www.alltrails.com/trail/us/utah/sand-bench-trail",
            "name": "Sand Bench Trail",
            "snippet": "hard 5.7 mi 1000 ft elevation gain 4.7 stars 1200 reviews",
        },
        {
            "url": "https://www.alltrails.com/trail/us/utah/parus-trail",
            "name": "Pa'rus Trail",
            "snippet": "easy 3.3 mi 80 ft elevation gain 4.6 stars 4000 reviews",
        },
    ]

    with patch.object(discoverer, "_search_first", return_value=None):
        url = discoverer._search_alltrails_for_trail("Canyon Overlook Trail", "Zion National Park")

    assert url is None


def test_filtered_alltrails_does_not_pad_with_weak_matches_when_only_one_candidate_passes():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._enable_filtered_alltrails_selection = True
    discoverer._alltrails_filtered_selection_cache = {}
    discoverer._uninterested_keywords = ()
    discoverer._seasonal_ski_keywords = ()
    discoverer._ski_in_season_months = ()

    ai = {
        "top_attractions": [
            {
                "name": "Canyon Overlook Trail",
                "type": "hike",
                "description": "Short canyon overlook hike.",
            },
            {
                "name": "Sand Bench Trail",
                "type": "hike",
                "description": "Longer strenuous desert trail.",
            },
        ]
    }

    def fake_filtered(*, item_name, dest_name, query_variants):
        _ = dest_name, query_variants
        if item_name == "Canyon Overlook Trail":
            return "https://www.alltrails.com/trail/us/utah/canyon-overlook-trail"
        return None

    with patch.object(discoverer, "_search_alltrails_for_trail_filtered", side_effect=fake_filtered):
        with patch.object(discoverer, "_resolve_ai_candidate_url", return_value=None):
            with patch.object(discoverer, "_meets_alltrails_publish_confidence", return_value=True):
                with patch.object(discoverer, "_search_attraction_from_direct_batch", return_value=None):
                    discoverer._discover_attractions(ai, "Zion National Park", "zion")

    first_url = ai["top_attractions"][0]["url"]
    second_url = ai["top_attractions"][1].get("url", "")

    assert first_url == "https://www.alltrails.com/trail/us/utah/canyon-overlook-trail"
    # "Sand Bench Trail" must not get padded with a weak/unrelated AllTrails
    # match -- but since the trail-like-branch no-link-fallback fix it does
    # get the same safe maps-search fallback every other unresolved
    # attraction gets, instead of no link at all.
    assert second_url == "https://www.google.com/maps/search/?api=1&query=Sand%20Bench%20Trail%20Zion%20National%20Park"


def test_load_interest_filters_applies_multi_site_base_owned_categories_default(tmp_path):
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        """
multi_site_grouping:
  base_owned_categories: ["restaurant", "scenic_drive"]
""".strip(),
        encoding="utf-8",
    )

    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._multi_site_base_owned_categories = frozenset({"restaurant"})

    discoverer._load_interest_filters(str(config_file))

    assert discoverer._multi_site_base_owned_categories == frozenset({"restaurant", "scenic_drive"})


def test_load_interest_filters_defaults_multi_site_base_owned_categories_when_key_absent(tmp_path):
    config_file = tmp_path / "config.yaml"
    config_file.write_text("url_discovery:\n  restaurant_source: search\n", encoding="utf-8")

    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._multi_site_base_owned_categories = frozenset({"restaurant"})

    discoverer._load_interest_filters(str(config_file))

    assert discoverer._multi_site_base_owned_categories == frozenset({"restaurant"})


def test_load_interest_filters_applies_rating_threshold_and_boost_controls(tmp_path):
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        """
url_discovery:
  alltrails_rating_min: 4.9
  alltrails_rating_min_votes: 1000
  alltrails_rating_boost: 25
  restaurant_rating_min: 4.8
  restaurant_rating_min_votes: 500
  restaurant_rating_boost: 20
""".strip(),
        encoding="utf-8",
    )

    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._alltrails_rating_min = 4.5
    discoverer._alltrails_rating_min_votes = 200
    discoverer._alltrails_rating_boost = 8
    discoverer._restaurant_rating_min = 4.4
    discoverer._restaurant_rating_min_votes = 100
    discoverer._restaurant_rating_boost = 6

    discoverer._load_interest_filters(str(config_file))

    assert discoverer._alltrails_rating_min == 4.9
    assert discoverer._alltrails_rating_min_votes == 1000
    assert discoverer._alltrails_rating_boost == 25
    assert discoverer._restaurant_rating_min == 4.8
    assert discoverer._restaurant_rating_min_votes == 500
    assert discoverer._restaurant_rating_boost == 20

    item = {
        "url": "https://www.alltrails.com/trail/us/utah/example-trail",
        "name": "Example Trail",
        "snippet": "4.9 stars 1200 reviews scenic trail",
    }
    strict_score = discoverer._score_candidate_result(
        item,
        "Example Trail",
        "Zion National Park",
        specific=True,
        site_filter="alltrails.com",
    )

    discoverer._alltrails_rating_min = 5.0
    discoverer._alltrails_rating_min_votes = 5000
    discoverer._alltrails_rating_boost = 25
    tighter_score = discoverer._score_candidate_result(
        item,
        "Example Trail",
        "Zion National Park",
        specific=True,
        site_filter="alltrails.com",
    )

    assert strict_score > tighter_score


def test_audit_fail_closed_removes_named_entity_url_when_policy_blocks_only_candidate():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._url_policy_mode = "enforce"
    discoverer._url_policy_blocked_classes = {"google_maps_search"}
    discoverer._url_policy_allowlisted_urls = set()
    discoverer._url_domain_denylist = frozenset()
    discoverer._alltrails_slug_denylist = frozenset()
    discoverer._max_trail_miles = 10.0
    discoverer._allow_blocked_alltrails = True
    discoverer._alltrails_min_confidence_for_publish = "low"

    trip = {
        "destinations": [
            {
                "name": "Capitol Reef National Park",
                "ai_content": {
                    "top_attractions": [
                        {
                            "name": "Capitol Reef Cafe",
                            "type": "attraction",
                            "description": "Popular stop.",
                            "url": "https://www.google.com/maps/search/?api=1&query=Capitol+Reef+Cafe",
                        }
                    ],
                    "getting_here": {"en_route_stops": []},
                    "dinner_recommendations": [],
                },
                "scenic_drives": [],
                "cultural_events": {"has_events": False, "events": []},
            }
        ]
    }

    discoverer.audit_discovered_urls(trip)

    # Policy (2026-08-17): the only candidate URL is a google_maps_search
    # link blocked by enforce-mode policy, leaving a non-seed attraction
    # with no verified url and no maps fallback to render -- removed from
    # top_attractions entirely rather than kept as a maps-only fallback card.
    attractions = trip["destinations"][0]["ai_content"]["top_attractions"]
    assert attractions == []


def test_load_url_policy_allowlist_merges_manual_and_output_urls(tmp_path):
    discoverer = URLDiscoverer.__new__(URLDiscoverer)

    allowlist_file = tmp_path / "allowlist.txt"
    allowlist_file.write_text(
        "https://manual.example.com/kept\n# comment\n",
        encoding="utf-8",
    )

    output_file = tmp_path / "index.html"
    output_file.write_text(
        '<a href="https://output.example.com/from-baseline">One</a>'
        '<a href="/local/path">Local</a>',
        encoding="utf-8",
    )

    discoverer._url_policy_allowlist_path = str(allowlist_file)
    discoverer._url_policy_auto_allow_from_output = True
    discoverer._url_policy_output_path = str(output_file)

    discoverer._load_url_policy_allowlist()

    assert "https://manual.example.com/kept" in discoverer._url_policy_allowlisted_urls
    assert "https://output.example.com/from-baseline" in discoverer._url_policy_allowlisted_urls
    assert "/local/path" not in discoverer._url_policy_allowlisted_urls


def test_load_url_policy_allowlist_can_disable_output_auto_seed(tmp_path):
    discoverer = URLDiscoverer.__new__(URLDiscoverer)

    allowlist_file = tmp_path / "allowlist.txt"
    allowlist_file.write_text("", encoding="utf-8")

    output_file = tmp_path / "index.html"
    output_file.write_text(
        '<a href="https://output.example.com/from-baseline">One</a>',
        encoding="utf-8",
    )

    discoverer._url_policy_allowlist_path = str(allowlist_file)
    discoverer._url_policy_auto_allow_from_output = False
    discoverer._url_policy_output_path = str(output_file)

    discoverer._load_url_policy_allowlist()

    assert "https://output.example.com/from-baseline" not in discoverer._url_policy_allowlisted_urls


def test_retain_url_rejects_wikipedia_wrong_entity():
    """PR-009: Wikipedia link to wrong entity is rejected via URL-path token check."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    result = discoverer._retain_discovered_url(
        "https://en.wikipedia.org/wiki/Bryce_Canyon_National_Park",
        "Mammoth Cave",
        "Bryce Canyon National Park",
        allow_alltrails=False,
    )
    assert result == ""


def test_retain_url_rejects_generic_restaurant_landing_page_for_named_entity():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    result = discoverer._retain_discovered_url(
        "https://www.visitpagosasprings.com/restaurants/",
        "Cafe Colorado",
        "Pagosa Springs",
        allow_alltrails=False,
        kind="restaurant",
    )
    assert result == ""


def test_retain_url_rejects_unescaped_whitespace_in_url():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    result = discoverer._retain_discovered_url(
        "https://www.visitpagosasprings.com/listing/pagosa-springs-center-for-the-arts/ wh",
        "Pagosa Springs Center for the Arts",
        "Pagosa Springs",
        allow_alltrails=False,
        kind="attraction",
    )
    assert result == ""


def test_retain_url_rejects_wikipedia_listings_page_for_specific_historic_district():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    result = discoverer._retain_discovered_url(
        "https://en.wikipedia.org/wiki/National_Register_of_Historic_Places_listings_in_Washington_County,_Utah",
        "St. George Historic District",
        "St. George, Utah",
        allow_alltrails=False,
        kind="attraction",
    )
    assert result == ""


def test_retain_url_rejects_domain_in_denylist():
    """PR-020/021/025: Known-untrusted domains are rejected before relevance checks."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._url_domain_denylist = frozenset({"visitpagosasprings.com", "pagosabrewing.com"})
    for url in [
        "https://visitpagosasprings.com/lizard-head-pass-area",
        "https://www.visitpagosasprings.com/listing/pagosa-springs-center-for-the-arts/204/",
        "https://www.pagosabrewing.com",
    ]:
        result = discoverer._retain_discovered_url(
            url,
            "Test Item",
            "Pagosa Springs",
            allow_alltrails=False,
        )
        assert result == "", f"Expected denylist rejection for {url}"


def test_retain_url_rejects_google_maps_search_for_token_strong_attraction_in_enforce_mode():
    """Maps-search URL is rejected for attractions in enforce mode to avoid multi-result links."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._url_policy_mode = "enforce"
    discoverer._url_policy_blocked_classes = {"google_maps_search"}
    discoverer._url_policy_allowlisted_urls = set()
    discoverer._url_domain_denylist = frozenset()

    result = discoverer._retain_discovered_url(
        "https://www.google.com/maps/search/?api=1&query=San+Juan+River+Fly+Fishing+Pagosa+Springs",
        "San Juan River Fly Fishing",
        "Pagosa Springs",
        allow_alltrails=False,
        kind="attraction",
    )
    assert result == ""


def test_retain_url_rejects_google_maps_search_when_attraction_tokens_are_weak_in_enforce_mode():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._url_policy_mode = "enforce"
    discoverer._url_policy_blocked_classes = {"google_maps_search"}
    discoverer._url_policy_allowlisted_urls = set()
    discoverer._url_domain_denylist = frozenset()

    result = discoverer._retain_discovered_url(
        "https://www.google.com/maps/search/?api=1&query=Things+to+do+near+St+George",
        "Snow Canyon State Park",
        "St. George, Utah",
        allow_alltrails=False,
        kind="attraction",
    )
    assert result == ""


def test_retain_url_rejects_google_maps_search_for_location_qualified_attraction_without_dest_tokens() -> None:
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._url_policy_mode = "enforce"
    discoverer._url_policy_blocked_classes = {"google_maps_search"}
    discoverer._url_policy_allowlisted_urls = set()
    discoverer._url_domain_denylist = frozenset()

    result = discoverer._retain_discovered_url(
        "https://www.google.com/maps/search/?api=1&query=Snow+Canyon+State+Park",
        "Snow Canyon State Park",
        "St. George, Utah",
        allow_alltrails=False,
        kind="attraction",
    )

    assert result == ""


def test_retain_url_rejects_synthetic_maps_place_placeholder_ids() -> None:
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._url_policy_mode = "monitor"
    discoverer._url_policy_allowlisted_urls = set()
    discoverer._url_domain_denylist = frozenset()

    with patch.object(
        discoverer,
        "_fetch_page_text",
        return_value=(True, 200, "Zion Human History Museum visitor information"),
    ):
        result = discoverer._retain_discovered_url(
            "https://www.google.com/maps/place/Zion+Human+History+Museum/@37.200975,-112.9875,17z/data=!3m1!4b1!4m6!3m5!1s0x80cacee0f5e5e5e5:0x5e5e5e5e5e5e5e5e!8m2!3d37.200975!4d-112.9875!16s%2Fg%2F1tc_xyz",
            "Zion Human History Museum",
            "Zion National Park",
            allow_alltrails=False,
            kind="attraction",
        )

    assert result == ""


def test_passes_alltrails_post_search_filters_rejects_404_even_when_filtered_selection_disabled() -> None:
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._enable_filtered_alltrails_selection = False

    with patch.object(discoverer, "_verify_url_cached", return_value=(False, 404)):
        ok = discoverer._passes_alltrails_post_search_filters(
            "https://www.alltrails.com/trail/us/colorado/san-juan-river-walk-trail",
            "San Juan River Walk",
            "Pagosa Springs",
        )

    assert ok is False


def test_retain_url_rejects_google_maps_search_for_non_location_qualified_attraction_without_dest_tokens() -> None:
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._url_policy_mode = "enforce"
    discoverer._url_policy_blocked_classes = {"google_maps_search"}
    discoverer._url_policy_allowlisted_urls = set()
    discoverer._url_domain_denylist = frozenset()

    result = discoverer._retain_discovered_url(
        "https://www.google.com/maps/search/?api=1&query=Cliffs+View",
        "Cliffs View",
        "St. George, Utah",
        allow_alltrails=False,
        kind="attraction",
    )

    assert result == ""


def test_retain_url_rejects_google_maps_search_for_named_restaurant_in_enforce_mode():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._url_policy_mode = "enforce"
    discoverer._url_policy_blocked_classes = {"google_maps_search"}
    discoverer._url_policy_allowlisted_urls = set()
    discoverer._url_domain_denylist = frozenset()

    result = discoverer._retain_discovered_url(
        "https://www.google.com/maps/search/?api=1&query=Capitol+Reef+Cafe",
        "Capitol Reef Cafe",
        "Capitol Reef National Park",
        allow_alltrails=False,
        kind="restaurant",
    )
    assert result == ""


def test_retain_url_rejects_google_maps_search_for_named_waypoint_in_enforce_mode():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._url_policy_mode = "enforce"
    discoverer._url_policy_blocked_classes = {"google_maps_search"}
    discoverer._url_policy_allowlisted_urls = set()
    discoverer._url_domain_denylist = frozenset()

    result = discoverer._retain_discovered_url(
        "https://www.google.com/maps/search/?api=1&query=Wilson+Arch+Moab",
        "Wilson Arch",
        "Moab",
        allow_alltrails=False,
        kind="en-route stop",
    )
    assert result == ""


def test_retain_url_allows_google_maps_search_for_direct_batch_harvest_only():
    """The policy exception still accepts a direct-batch-harvested
    google_maps_search URL -- but see
    test_retain_url_rebuilds_ai_authored_google_maps_search_query_dropping_
    state_suffix below: the query text itself is now rebuilt via the
    controlled _maps_fallback_query_text builder (item_name + dest_name,
    %20-quoted) rather than trusted verbatim, so this fixture's already-
    item+dest-ordered, no-extra-suffix query round-trips to the same text
    modulo quoting style."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._url_policy_mode = "enforce"
    discoverer._url_policy_blocked_classes = {"google_maps_search"}
    discoverer._url_policy_allowlisted_urls = set()
    discoverer._url_domain_denylist = frozenset()

    url = "https://www.google.com/maps/search/?api=1&query=Canyon+Overlook+Trail+Zion+National+Park"
    result = discoverer._retain_discovered_url(
        url,
        "Canyon Overlook Trail",
        "Zion National Park",
        allow_alltrails=False,
        kind="attraction",
        allow_google_maps_search=True,
    )
    assert result == (
        "https://www.google.com/maps/search/?api=1&query=Canyon%20Overlook%20Trail%20Zion%20National%20Park"
    )


def test_retain_url_rebuilds_ai_authored_google_maps_search_query_dropping_state_suffix() -> None:
    """Real dipstick67 bug: the direct-batch harvest's own AI-authored Google
    Maps search link for "Sunrise Point" (Bryce Canyon National Park) was
    "https://www.google.com/maps/search/?api=1&query=Sunrise+Point+Bryce+
    Canyon+National+Park+UT" -- accepted verbatim through the
    google_maps_search policy exception. Reproduced live in a real browser:
    that exact query (with the trailing bare "UT") returns a multi-result
    disambiguation list matching the user's complaint ("Sunrise point is
    opening to a broad range of options") -- Sunrise Point tied against the
    unrelated, similarly-named "Sunset Point" and an out-of-state "Sunrise
    Point" cliff. Dropping just the trailing state code (same item+destination
    text otherwise) resolves straight to the single correct place. The fix
    rebuilds the query via the same controlled _maps_fallback_query_text
    builder used everywhere else, instead of trusting the AI-harvested query
    text verbatim."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._url_policy_mode = "enforce"
    discoverer._url_policy_blocked_classes = {"google_maps_search"}
    discoverer._url_policy_allowlisted_urls = set()
    discoverer._url_domain_denylist = frozenset()

    url = "https://www.google.com/maps/search/?api=1&query=Sunrise+Point+Bryce+Canyon+National+Park+UT"
    result = discoverer._retain_discovered_url(
        url,
        "Sunrise Point",
        "Bryce Canyon National Park",
        allow_alltrails=False,
        kind="attraction",
        allow_google_maps_search=True,
    )
    assert result == (
        "https://www.google.com/maps/search/?api=1&query=Sunrise%20Point%20Bryce%20Canyon%20National%20Park"
    )
    assert "UT" not in result


def test_retain_discovered_url_rejects_alltrails_trail_when_post_constraints_fail() -> None:
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._enable_filtered_alltrails_selection = True
    discoverer._alltrails_filter_allowed_difficulties = ("easy", "moderate", "moderately challenging")
    discoverer._alltrails_filter_max_miles = 4.0
    discoverer._alltrails_filter_max_gain_feet = 1000
    discoverer._alltrails_filter_min_reviews = 5
    discoverer._max_trail_miles = 4.0
    discoverer._alltrails_min_confidence_for_publish = "low"

    with patch.object(
        discoverer,
        "_fetch_page_text",
        return_value=(True, 200, "Hard. 9.4 mi. Elevation gain 1,620 ft. 4.8 stars, 785 reviews."),
    ):
        result = discoverer._retain_discovered_url(
            "https://www.alltrails.com/trail/us/utah/navajo-knobs-trail",
            "Navajo Knobs",
            "Capitol Reef National Park",
            allow_alltrails=True,
            kind="attraction",
        )

    assert result == ""


def test_retain_discovered_url_allows_alltrails_trail_when_post_constraints_metadata_unavailable() -> None:
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._enable_filtered_alltrails_selection = True
    discoverer._alltrails_filter_allowed_difficulties = ("easy", "moderate", "moderately challenging")
    discoverer._alltrails_filter_max_miles = 4.0
    discoverer._alltrails_filter_max_gain_feet = 1000
    discoverer._alltrails_filter_min_reviews = 5
    discoverer._max_trail_miles = 4.0
    discoverer._alltrails_min_confidence_for_publish = "low"

    with patch.object(discoverer, "_fetch_page_text", return_value=(False, 403, "")):
        result = discoverer._retain_discovered_url(
            "https://www.alltrails.com/trail/us/utah/canyon-overlook-trail",
            "Canyon Overlook Trail",
            "Zion National Park",
            allow_alltrails=True,
            kind="attraction",
        )

    assert result == "https://www.alltrails.com/trail/us/utah/canyon-overlook-trail"


def test_retain_url_rejects_google_maps_dir_in_enforce_mode():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._url_policy_mode = "enforce"
    discoverer._url_policy_blocked_classes = {"google_maps_dir"}
    discoverer._url_policy_allowlisted_urls = set()
    discoverer._url_domain_denylist = frozenset()

    result = discoverer._retain_discovered_url(
        "https://www.google.com/maps/dir/Capitol+Reef/Capitol+Reef+Cafe",
        "Capitol Reef Cafe",
        "Capitol Reef National Park",
        allow_alltrails=False,
        kind="restaurant",
    )
    assert result == ""


def test_retain_url_rejects_google_search_in_enforce_mode():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._url_policy_mode = "enforce"
    discoverer._url_policy_blocked_classes = {"google_search"}
    discoverer._url_policy_allowlisted_urls = set()
    discoverer._url_domain_denylist = frozenset()

    result = discoverer._retain_discovered_url(
        "https://www.google.com/search?q=Capitol+Reef+Cafe",
        "Capitol Reef Cafe",
        "Capitol Reef National Park",
        allow_alltrails=False,
        kind="restaurant",
    )
    assert result == ""


def test_retain_url_rejects_social_media_in_enforce_mode():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._url_policy_mode = "enforce"
    discoverer._url_policy_blocked_classes = {"social_media"}
    discoverer._url_policy_allowlisted_urls = set()
    discoverer._url_domain_denylist = frozenset()
    discoverer._is_relevant_result = lambda *_args, **_kwargs: True

    result = discoverer._retain_discovered_url(
        "https://www.facebook.com/pagosacenterforthearts",
        "Pagosa Springs Center for the Arts",
        "Pagosa Springs",
        allow_alltrails=False,
        kind="attraction",
    )
    assert result == ""


def test_retain_url_keeps_blocked_class_in_monitor_mode():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._url_policy_mode = "monitor"
    discoverer._url_policy_blocked_classes = {"social_media"}
    discoverer._url_policy_allowlisted_urls = set()
    discoverer._url_domain_denylist = frozenset()
    discoverer._is_relevant_result = lambda *_args, **_kwargs: True

    url = "https://www.instagram.com/example-trail/"
    result = discoverer._retain_discovered_url(
        url,
        "Example Trail",
        "Telluride",
        allow_alltrails=False,
        kind="attraction",
    )
    assert result == url


def test_retain_url_keeps_wikipedia_matching_entity():
    """Wikipedia link whose slug contains item tokens passes entity check and proceeds to relevance."""
    from unittest.mock import MagicMock
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._url_validator = MagicMock()
    discoverer._url_validator.session.get.return_value = MagicMock(
        status_code=200,
        text="Bryce Canyon National Park is a national park in Utah.",
        url="https://en.wikipedia.org/wiki/Bryce_Canyon_National_Park",
    )
    result = discoverer._retain_discovered_url(
        "https://en.wikipedia.org/wiki/Bryce_Canyon_National_Park",
        "Bryce Canyon National Park",
        "Bryce Canyon National Park",
        allow_alltrails=False,
    )
    assert result == "https://en.wikipedia.org/wiki/Bryce_Canyon_National_Park"


def test_retain_url_rejects_alltrails_slug_in_denylist():
    """PR-017/019: Slug in denylist is rejected even before any network fetch."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._alltrails_slug_denylist = frozenset({
        "ajax-peak-trail",
        "jud-wiebe-trail",
        "jud-wiebe-memorial-trail",
        "bear-creek-trail",
    })
    for slug, item in [
        ("ajax-peak-trail", "Ajax Peak"),
        ("jud-wiebe-trail", "Jud Wiebe Trail"),
        ("jud-wiebe-memorial-trail", "Jud Wiebe Trail"),
        ("bear-creek-trail", "Bear Creek Trail"),
    ]:
        result = discoverer._retain_discovered_url(
            f"https://www.alltrails.com/trail/us/colorado/{slug}",
            item,
            "Telluride",
            allow_alltrails=True,
        )
        assert result == "", f"Expected denylist rejection for {slug}"


def test_retain_url_rejects_generic_geography_url_for_category_activity():
    """PR-022 hardening: category activities must not link to generic geography pages."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._url_domain_denylist = frozenset()
    discoverer._url_policy_allowlisted_urls = set()
    discoverer._url_policy_mode = "enforce"
    discoverer._url_policy_blocked_classes = set()

    result = discoverer._retain_discovered_url(
        "https://en.wikipedia.org/wiki/San_Juan_River_(Colorado_River_tributary)",
        "San Juan River Fly Fishing",
        "Pagosa Springs",
        allow_alltrails=False,
        kind="attraction",
    )
    assert result == ""


def test_retain_url_rejects_offer_listing_url_for_category_activity():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._url_domain_denylist = frozenset()
    discoverer._url_policy_allowlisted_urls = set()
    discoverer._url_policy_mode = "enforce"
    discoverer._url_policy_blocked_classes = set()

    result = discoverer._retain_discovered_url(
        "https://www.visitpagosasprings.com/listing/fishing-guides/123/",
        "San Juan River Fly Fishing",
        "Pagosa Springs",
        allow_alltrails=False,
        kind="attraction",
    )
    assert result == ""


def test_retain_url_accepts_item_specific_restaurant_homepage_when_content_matches() -> None:
    from unittest.mock import MagicMock

    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._url_validator = MagicMock()
    discoverer._url_validator.session.get.return_value = MagicMock(
        status_code=200,
        text="Wood Ash Rye in St. George Utah serves modern American dinner and cocktails.",
        url="https://www.woodashrye.com/",
    )
    discoverer._url_domain_denylist = frozenset()
    discoverer._url_policy_allowlisted_urls = set()
    discoverer._url_policy_mode = "enforce"
    discoverer._url_policy_blocked_classes = set()

    result = discoverer._retain_discovered_url(
        "https://www.woodashrye.com/",
        "Wood Ash Rye",
        "St. George, Utah",
        allow_alltrails=False,
        kind="restaurant",
    )

    assert result == "https://www.woodashrye.com/"


def test_retain_url_accepts_restaurant_homepage_when_page_text_matches_item_name() -> None:
    from unittest.mock import MagicMock

    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._url_validator = MagicMock()
    discoverer._url_validator.session.get.return_value = MagicMock(
        status_code=200,
        text="Cafe Diablo serves breakfast and dinner in Torrey, Utah.",
        url="https://www.cafediablo.com/",
    )
    discoverer._url_domain_denylist = frozenset()
    discoverer._url_policy_allowlisted_urls = set()
    discoverer._url_policy_mode = "enforce"
    discoverer._url_policy_blocked_classes = set()

    result = discoverer._retain_discovered_url(
        "https://www.cafediablo.com/",
        "Cafe Diablo",
        "Capitol Reef National Park",
        allow_alltrails=False,
        kind="restaurant",
    )

    assert result == "https://www.cafediablo.com/"


def test_is_relevant_result_rejects_alltrails_slug_in_denylist():
    """Slug denylist is also applied in the relevance gate during discovery."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._alltrails_slug_denylist = frozenset({"ajax-peak-trail"})
    result = discoverer._is_relevant_result(
        "https://www.alltrails.com/trail/us/colorado/ajax-peak-trail",
        "Ajax Peak",
        "Telluride",
    )
    assert result is False


def test_is_relevant_result_rejects_alltrails_redirect_to_different_entity():
    """PR-018: When AllTrails redirects to a different trail slug, it is rejected."""
    from unittest.mock import MagicMock

    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._alltrails_slug_denylist = frozenset()
    discoverer._alltrails_min_confidence_for_publish = "medium"
    discoverer._url_validator = MagicMock()
    discoverer._url_validator.session.get.return_value = MagicMock(
        status_code=200,
        text="Penrose Trail Colorado hiking details and reviews.",
        url="https://www.alltrails.com/trail/us/colorado/penrose-trail",
    )
    discoverer._alltrails_request_delay_seconds = 0
    discoverer._alltrails_block_cooldown_seconds = 0
    discoverer._alltrails_fetch_cache = {}
    discoverer._alltrails_fetch_lock = __import__("threading").Lock()
    discoverer._alltrails_last_request_ts = 0.0
    discoverer._alltrails_blocked_until_ts = 0.0
    discoverer._fetch_final_url_cache = {}
    discoverer._allow_blocked_alltrails = True
    discoverer._max_trail_miles = 10.0

    result = discoverer._is_relevant_result(
        "https://www.alltrails.com/trail/us/colorado/bear-creek-trail",
        "Bear Creek Trail",
        "Telluride",
    )
    assert result is False


def test_is_relevant_result_rejects_alltrails_redirect_mismatch_when_blocked_fetch():
    """PR-018 hardening: redirect mismatch is rejected even when AllTrails fetch is blocked (403)."""
    from unittest.mock import MagicMock

    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._alltrails_slug_denylist = frozenset()
    discoverer._alltrails_min_confidence_for_publish = "medium"
    discoverer._url_validator = MagicMock()

    def _fake_get_text(url, timeout=8):
        discoverer._url_validator._last_final_url = "https://www.alltrails.com/trail/us/colorado/penrose-trail"
        return False, 403, ""

    discoverer._url_validator.get_text.side_effect = _fake_get_text
    discoverer._alltrails_request_delay_seconds = 0
    discoverer._alltrails_block_cooldown_seconds = 0
    discoverer._alltrails_fetch_cache = {}
    discoverer._alltrails_fetch_lock = __import__("threading").Lock()
    discoverer._alltrails_last_request_ts = 0.0
    discoverer._alltrails_blocked_until_ts = 0.0
    discoverer._fetch_final_url_cache = {}
    discoverer._allow_blocked_alltrails = True
    discoverer._max_trail_miles = 10.0

    result = discoverer._is_relevant_result(
        "https://www.alltrails.com/trail/us/colorado/bear-creek-trail",
        "Bear Creek Trail",
        "Telluride",
    )
    assert result is False


def test_fetch_alltrails_text_short_circuits_while_blocked_without_sleeping_or_refetching():
    """Regression: AllTrails' DataDome block tends to be sustained, not a
    simple time-window rate limit -- the old behavior slept out the cooldown
    and then re-attempted the network call anyway, almost always just failing
    again. While blocked, this must return a synthetic result immediately: no
    sleep, no network call, and the result must not be cached (so a later
    call after the cooldown naturally expires still gets a real attempt)."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._alltrails_fetch_cache = {}
    discoverer._alltrails_fetch_lock = Lock()
    discoverer._alltrails_last_request_ts = 0.0
    discoverer._alltrails_blocked_until_ts = time.monotonic() + 30.0
    discoverer._alltrails_request_delay_seconds = 0.0
    discoverer._alltrails_block_cooldown_seconds = 8.0
    discoverer._fetch_page_text_uncached = MagicMock(side_effect=AssertionError("must not fetch while blocked"))

    with patch("generator.url_discovery.time.sleep", side_effect=AssertionError("must not sleep while blocked")):
        result = discoverer._fetch_alltrails_text("https://www.alltrails.com/trail/us/utah/angels-landing-trail")

    assert result == (False, 403, "")
    discoverer._fetch_page_text_uncached.assert_not_called()
    assert "https://www.alltrails.com/trail/us/utah/angels-landing-trail" not in discoverer._alltrails_fetch_cache


def test_fetch_page_text_short_circuits_generic_domain_while_blocked():
    """Regression: only AllTrails had a block-cooldown; any other domain that
    returns 401/403 (TripAdvisor, etc.) had no memory of that block, so a
    different URL on the same domain moments later paid a full network
    timeout for a call very unlikely to succeed. This generalizes the
    AllTrails cooldown to any domain."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._page_text_cache = {}
    discoverer._request_cache_lock = Lock()
    discoverer._domain_blocked_until_ts = {"www.tripadvisor.com": time.monotonic() + 30.0}
    discoverer._domain_block_cooldown_seconds = 8.0
    discoverer._fetch_page_text_uncached = MagicMock(side_effect=AssertionError("must not fetch while domain blocked"))

    result = discoverer._fetch_page_text(
        "https://www.tripadvisor.com/Restaurant_Review-g60899-d999999-Reviews-Other_Place.html"
    )

    assert result == (False, 403, "")
    discoverer._fetch_page_text_uncached.assert_not_called()
    assert (
        "https://www.tripadvisor.com/Restaurant_Review-g60899-d999999-Reviews-Other_Place.html"
        not in discoverer._page_text_cache
    )


def test_fetch_page_text_records_domain_block_on_403_and_still_returns_result():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._page_text_cache = {}
    discoverer._request_cache_lock = Lock()
    discoverer._domain_blocked_until_ts = {}
    discoverer._domain_block_cooldown_seconds = 8.0
    discoverer._fetch_page_text_uncached = MagicMock(return_value=(False, 403, ""))

    result = discoverer._fetch_page_text("https://www.tripadvisor.com/Restaurant_Review-g60899-d1-Reviews-Place.html")

    assert result == (False, 403, "")
    assert "www.tripadvisor.com" in discoverer._domain_blocked_until_ts
    assert discoverer._domain_blocked_until_ts["www.tripadvisor.com"] > time.monotonic()


def test_fetch_page_text_does_not_block_domain_on_success():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._page_text_cache = {}
    discoverer._request_cache_lock = Lock()
    discoverer._domain_blocked_until_ts = {}
    discoverer._domain_block_cooldown_seconds = 8.0
    discoverer._fetch_page_text_uncached = MagicMock(return_value=(True, 200, "hello"))

    result = discoverer._fetch_page_text("https://www.example.com/some-restaurant")

    assert result == (True, 200, "hello")
    assert discoverer._domain_blocked_until_ts == {}


# ── Epic 4: Restaurant freshness gate ────────────────────────────────────────

def test_is_restaurant_ineligible_via_name_denylist():
    """PR-025/026/030: Restaurant name in denylist is immediately ineligible."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._restaurant_name_denylist = frozenset({"nello's bistro", "la casa sena", "pagosa brewing"})
    assert discoverer._is_restaurant_ineligible({"name": "Nello's Bistro"}, "Pagosa Springs")
    assert discoverer._is_restaurant_ineligible({"name": "La Casa Sena"}, "Santa Fe")
    assert discoverer._is_restaurant_ineligible({"name": "Pagosa Brewing"}, "Pagosa Springs")


def test_is_restaurant_ineligible_via_closure_page_text():
    """Restaurant with closure marker in page text is ineligible."""
    from unittest.mock import MagicMock
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._restaurant_name_denylist = frozenset()
    discoverer._url_validator = MagicMock()
    discoverer._url_validator.session.get.return_value = MagicMock(
        status_code=200,
        text="<html><body>Nello's Bistro — this business is permanently closed.</body></html>",
        url="https://www.tripadvisor.com/Restaurant_Review-123",
    )
    rest = {"name": "Nello's Bistro", "url": "https://www.tripadvisor.com/Restaurant_Review-123"}
    assert discoverer._is_restaurant_ineligible(rest, "Pagosa Springs")


def test_is_restaurant_ineligible_via_pre_opening_page_text():
    """Restaurant with pre-opening marker in page text is ineligible."""
    from unittest.mock import MagicMock
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._restaurant_name_denylist = frozenset()
    discoverer._url_validator = MagicMock()
    discoverer._url_validator.session.get.return_value = MagicMock(
        status_code=200,
        text="Welcome to La Casa Sena. Opening soon — stay tuned for our grand opening!",
        url="https://www.lacasasena.com",
    )
    rest = {"name": "La Casa Sena", "url": "https://www.lacasasena.com"}
    assert discoverer._is_restaurant_ineligible(rest, "Santa Fe")


def test_is_restaurant_ineligible_skips_fallback_urls():
    """Restaurants with only a fallback maps URL skip page-text check (returns False)."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._restaurant_name_denylist = frozenset()
    rest = {
        "name": "Some Restaurant",
        "url": "https://www.google.com/maps/search/?api=1&query=Some+Restaurant+Pagosa",
    }
    assert not discoverer._is_restaurant_ineligible(rest, "Pagosa Springs")


def test_audit_removes_ineligible_restaurant_from_destination():
    """Full audit pass removes restaurant matching denylist from dinner_recommendations."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._restaurant_name_denylist = frozenset({"nello's bistro", "pagosa brewing"})
    discoverer._url_policy_mode = "off"
    discoverer._url_policy_blocked_classes = set()
    discoverer._url_policy_allowlisted_urls = set()
    discoverer._alltrails_slug_denylist = frozenset()
    discoverer._max_trail_miles = 10.0
    discoverer._allow_blocked_alltrails = True
    discoverer._alltrails_min_confidence_for_publish = "low"

    trip = {
        "destinations": [
            {
                "name": "Pagosa Springs",
                "ai_content": {
                    "top_attractions": [],
                    "getting_here": {"en_route_stops": []},
                    "dinner_recommendations": [
                        {"name": "Nello's Bistro", "url": ""},
                        {"name": "Pagosa Brewing", "url": ""},
                    ],
                },
                "scenic_drives": [],
                "cultural_events": {"has_events": False, "events": []},
            }
        ]
    }
    discoverer.audit_discovered_urls(trip)
    names = [r["name"] for r in trip["destinations"][0]["ai_content"]["dinner_recommendations"]]
    assert "Nello's Bistro" not in names
    assert "Pagosa Brewing" not in names
    decisions = trip["destinations"][0]["_registry_decisions"]
    rejected_names = [entry["display_name"] for entry in decisions]
    assert "Nello's Bistro" in rejected_names
    assert "Pagosa Brewing" in rejected_names
    assert all(entry["validation_status"] == "rejected" for entry in decisions)
    assert all("entity_removed" in entry["rejection_reasons"] for entry in decisions)


# ── Epic 3: Content deduplication ────────────────────────────────────────────

def test_retain_url_rejects_destination_homepage_for_compound_named_attraction():
    """A city homepage is rejected for a specific landmark; relevance gate catches it via page text."""
    from unittest.mock import patch
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    with patch.object(
        discoverer, "_fetch_page_text",
        return_value=(True, 200, "Welcome to the City of Santa Fe. City services and information."),
    ):
        result = discoverer._retain_discovered_url(
            "https://www.santafenm.gov",
            "Santa Fe Plaza & Palace of the Governors",
            "Santa Fe",
            allow_alltrails=False,
            kind="attraction",
        )
    assert result == ""


def test_retain_url_accepts_specific_page_for_compound_named_attraction():
    """A specific historical-landmark URL is accepted even when the entity name contains ' & '."""
    from unittest.mock import MagicMock
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._url_validator = MagicMock()
    discoverer._url_validator.session.get.return_value = MagicMock(
        status_code=200,
        text="Palace of the Governors historic building on the Santa Fe Plaza.",
        url="https://www.newmexicohistory.org/palace-of-the-governors/",
    )
    result = discoverer._retain_discovered_url(
        "https://www.newmexicohistory.org/palace-of-the-governors/",
        "Santa Fe Plaza & Palace of the Governors",
        "Santa Fe",
        allow_alltrails=False,
        kind="attraction",
    )
    assert result == "https://www.newmexicohistory.org/palace-of-the-governors/"


def test_retain_url_keeps_non_compound_entity():
    """Single-entity name without ' & ' is not rejected by compound check."""
    from unittest.mock import MagicMock
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._url_validator = MagicMock()
    discoverer._url_validator.session.get.return_value = MagicMock(
        status_code=200,
        text="Santa Fe Plaza historic square in Santa Fe New Mexico.",
        url="https://www.santafenm.gov/plaza",
    )
    result = discoverer._retain_discovered_url(
        "https://www.santafenm.gov/plaza",
        "Santa Fe Plaza",
        "Santa Fe",
        allow_alltrails=False,
    )
    assert result == "https://www.santafenm.gov/plaza"


def test_deduplicate_within_destination_removes_drive_matching_attraction():
    """PR-013/023: Scenic drive whose title overlaps an attraction is removed."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    dest = {
        "name": "Moab, UT",
        "ai_content": {
            "top_attractions": [
                {"name": "Dead Horse Point State Park", "type": "attraction"},
            ]
        },
        "scenic_drives": [
            {"title": "Dead Horse Point State Park", "category": "viewpoint"},
            {"title": "Colorado River Scenic Byway", "category": "drive"},
        ],
    }
    discoverer._deduplicate_within_destination(dest)
    titles = [d["title"] for d in dest["scenic_drives"]]
    assert "Dead Horse Point State Park" not in titles
    assert "Colorado River Scenic Byway" in titles


def test_deduplicate_within_destination_removes_partial_drive_overlap():
    """PR-023: 'Wolf Creek Pass Scenic Drive' removed when 'Wolf Creek Pass' is an attraction."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    dest = {
        "name": "Pagosa Springs",
        "ai_content": {
            "top_attractions": [
                {"name": "Wolf Creek Pass", "type": "viewpoint"},
            ]
        },
        "scenic_drives": [
            {"title": "Wolf Creek Pass Scenic Drive", "category": "drive"},
            {"title": "Treasure Falls", "category": "viewpoint"},
        ],
    }
    discoverer._deduplicate_within_destination(dest)
    titles = [d["title"] for d in dest["scenic_drives"]]
    assert "Wolf Creek Pass Scenic Drive" not in titles
    assert "Treasure Falls" in titles


def test_deduplicate_within_destination_keeps_unrelated_drives():
    """Unrelated scenic drives are not affected by within-destination dedup."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    dest = {
        "name": "Zion National Park",
        "ai_content": {
            "top_attractions": [
                {"name": "Angels Landing", "type": "hike"},
            ]
        },
        "scenic_drives": [
            {"title": "Zion Canyon Scenic Drive", "category": "drive"},
        ],
    }
    discoverer._deduplicate_within_destination(dest)
    assert len(dest["scenic_drives"]) == 1


def test_deduplicate_within_destination_removes_attraction_matching_en_route_stop():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    dest = {
        "name": "Pagosa Springs",
        "ai_content": {
            "top_attractions": [
                {"name": "Wolf Creek Pass Scenic Drive", "type": "attraction"},
                {"name": "Treasure Falls", "type": "hike"},
            ],
            "getting_here": {
                "en_route_stops": [
                    {"name": "Wolf Creek Pass", "description": "Mountain pass detour."}
                ]
            },
        },
        "scenic_drives": [],
    }

    discoverer._deduplicate_within_destination(dest)
    kept_names = [str(a.get("name", "") or "") for a in dest["ai_content"]["top_attractions"]]
    assert "Wolf Creek Pass Scenic Drive" not in kept_names
    assert "Treasure Falls" in kept_names


def test_deduplicate_within_destination_merges_attractions_sharing_exact_url():
    """Regression for the exact dipstick55 Theme F report: 'Telluride
    Mountain Village' and 'Telluride Mountain Village Gondola' both
    resolved to https://www.telluride.com/discover/the-gondola/ and
    rendered as two separate cards. An exact-URL match within the same
    destination's top_attractions is merged, keeping whichever entry has
    richer metadata (description/rating) rather than the bare duplicate."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    dest = {
        "name": "Telluride",
        "ai_content": {
            "top_attractions": [
                {
                    "name": "Mountain Village",
                    "type": "attraction",
                    "url": "https://www.telluride.com/discover/the-gondola/",
                    "description": "Mountain Village offers a modern contrast to Telluride with shopping and dining.",
                    "rating": 4.9,
                },
                {
                    "name": "Telluride Mountain Village Gondola",
                    "type": "attraction",
                    "url": "https://www.telluride.com/discover/the-gondola/",
                },
            ],
            "getting_here": {"en_route_stops": []},
        },
        "scenic_drives": [],
    }

    discoverer._deduplicate_within_destination(dest)

    attractions = dest["ai_content"]["top_attractions"]
    assert len(attractions) == 1
    assert attractions[0]["name"] == "Mountain Village"


def test_deduplicate_within_destination_merges_bryce_inspiration_point_duplicates_but_keeps_distinct_trail():
    """Regression for the second real dipstick55 Theme F case: Bryce
    Canyon's 'Inspiration Point' and 'Sunset and Inspiration Points via Rim
    Trail and Bryce Canyon Path' both resolved to the same AllTrails page and
    must merge, but the genuinely distinct 'Lower, Mid, and Upper
    Inspiration Points' (a different AllTrails URL) must survive -- exact-
    URL matching must not over-merge just because names share a word."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    dest = {
        "name": "Bryce Canyon National Park",
        "ai_content": {
            "top_attractions": [
                {
                    "name": "Inspiration Point",
                    "type": "attraction",
                    "url": "https://www.alltrails.com/trail/us/utah/sunset-and-inspiration-points-via-rim-trail-and-bryce-canyon-path",
                    "description": "Provides one of the best panoramic views of the Bryce Canyon amphitheater.",
                    "rating": 4.8,
                },
                {
                    "name": "Sunset and Inspiration Points via Rim Trail and Bryce Canyon Path",
                    "type": "hike",
                    "url": "https://www.alltrails.com/trail/us/utah/sunset-and-inspiration-points-via-rim-trail-and-bryce-canyon-path",
                    "rating": 4.8,
                },
                {
                    "name": "Lower, Mid, and Upper Inspiration Points",
                    "type": "hike",
                    "url": "https://www.alltrails.com/trail/us/utah/lower-mid-and-upper-inspiration-points",
                    "rating": 4.8,
                },
            ],
            "getting_here": {"en_route_stops": []},
        },
        "scenic_drives": [],
    }

    discoverer._deduplicate_within_destination(dest)

    names = [a["name"] for a in dest["ai_content"]["top_attractions"]]
    assert names == ["Inspiration Point", "Lower, Mid, and Upper Inspiration Points"]


def test_deduplicate_within_destination_records_attraction_url_merge_for_registry():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    dest = {
        "name": "Telluride",
        "ai_content": {
            "top_attractions": [
                {"name": "Mountain Village", "type": "attraction", "url": "https://example.com/gondola"},
                {"name": "Telluride Mountain Village Gondola", "type": "attraction", "url": "https://example.com/gondola"},
            ],
            "getting_here": {"en_route_stops": []},
        },
        "scenic_drives": [],
    }
    discoverer._deduplicate_within_destination(dest)

    decisions = dest.get("_registry_decisions", [])
    assert len(decisions) == 1
    assert decisions[0]["display_name"] == "Telluride Mountain Village Gondola"
    assert decisions[0]["section_target"] == "top_attractions"
    assert decisions[0]["validation_status"] == "rejected"


def test_deduplicate_within_destination_records_scenic_drive_removal_for_registry():
    """Regression: this removal used to be invisible to the entity registry
    (and, transitively, schedule reconciliation), so a schedule could keep
    referencing a scenic drive that silently vanished here."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    dest = {
        "name": "Moab, UT",
        "ai_content": {
            "top_attractions": [
                {"name": "Dead Horse Point State Park", "type": "attraction"},
            ]
        },
        "scenic_drives": [
            {"title": "Dead Horse Point State Park", "category": "viewpoint"},
        ],
    }
    discoverer._deduplicate_within_destination(dest)

    decisions = dest.get("_registry_decisions", [])
    assert len(decisions) == 1
    assert decisions[0]["display_name"] == "Dead Horse Point State Park"
    assert decisions[0]["section_target"] == "scenic_drives"
    assert decisions[0]["validation_status"] == "rejected"


def test_deduplicate_within_destination_records_attraction_removal_for_registry():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    dest = {
        "name": "Pagosa Springs",
        "ai_content": {
            "top_attractions": [
                {"name": "Wolf Creek Pass Scenic Drive", "type": "attraction"},
            ],
            "getting_here": {
                "en_route_stops": [
                    {"name": "Wolf Creek Pass", "description": "Mountain pass detour."}
                ]
            },
        },
        "scenic_drives": [],
    }
    discoverer._deduplicate_within_destination(dest)

    decisions = dest.get("_registry_decisions", [])
    assert len(decisions) == 1
    assert decisions[0]["display_name"] == "Wolf Creek Pass Scenic Drive"
    assert decisions[0]["section_target"] == "top_attractions"
    assert decisions[0]["validation_status"] == "rejected"


def test_retain_discovered_url_rejects_yelp_search_pages():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    out = discoverer._retain_discovered_url(
        "https://www.yelp.com/search?cflt=localservices&find_loc=Pagosa+Springs%2C+CO",
        "Chimney Rock National Monument",
        "Pagosa Springs",
        allow_alltrails=False,
        kind="attraction",
    )
    assert out == ""


def test_retain_discovered_url_rejects_tripadvisor_attractions_listing_pages():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    out = discoverer._retain_discovered_url(
        "https://www.tripadvisor.com/Attractions-g60958-Activities-Santa_Fe_New_Mexico.html",
        "Georgia O'Keeffe Museum",
        "Santa Fe",
        allow_alltrails=False,
        kind="attraction",
    )
    assert out == ""


def test_deduplicate_cross_destination_drives_removes_overlap_with_other_destination_attraction():
    """PR-008: scenic drive is removed when it duplicates another destination's attraction concept."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    trip = {
        "destinations": [
            {
                "name": "St. George",
                "ai_content": {"top_attractions": []},
                "scenic_drives": [{"title": "Kolob Canyons Road", "category": "drive"}],
            },
            {
                "name": "Zion National Park",
                "ai_content": {"top_attractions": [{"name": "Kolob Canyons", "type": "attraction"}]},
                "scenic_drives": [{"title": "Zion Canyon Scenic Drive", "category": "drive"}],
            },
        ]
    }

    discoverer._deduplicate_cross_destination_drives(trip)

    st_george_titles = [d["title"] for d in trip["destinations"][0]["scenic_drives"]]
    zion_titles = [d["title"] for d in trip["destinations"][1]["scenic_drives"]]
    assert "Kolob Canyons Road" not in st_george_titles
    assert "Zion Canyon Scenic Drive" in zion_titles


def test_deduplicate_cross_destination_drives_keeps_unrelated_concepts():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    trip = {
        "destinations": [
            {
                "name": "A",
                "ai_content": {"top_attractions": [{"name": "Angels Landing", "type": "hike"}]},
                "scenic_drives": [{"title": "Kolob Terrace Road", "category": "drive"}],
            },
            {
                "name": "B",
                "ai_content": {"top_attractions": [{"name": "Bryce Amphitheater", "type": "viewpoint"}]},
                "scenic_drives": [{"title": "Scenic Byway 12", "category": "drive"}],
            },
        ]
    }

    discoverer._deduplicate_cross_destination_drives(trip)

    assert len(trip["destinations"][0]["scenic_drives"]) == 1
    assert len(trip["destinations"][1]["scenic_drives"]) == 1


def test_deduplicate_tripwide_removes_attraction_matching_other_destination_en_route_stop():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    trip = {
        "destinations": [
            {
                "name": "Telluride",
                "ai_content": {
                    "top_attractions": [
                        {"name": "Lizard Head Pass", "type": "attraction"},
                        {"name": "Bear Creek Falls", "type": "hike"},
                    ],
                    "getting_here": {"en_route_stops": []},
                },
                "scenic_drives": [],
            },
            {
                "name": "Pagosa Springs",
                "ai_content": {
                    "top_attractions": [
                        {"name": "Treasure Falls", "type": "hike"},
                    ],
                    "getting_here": {
                        "en_route_stops": [
                            {"name": "Lizard Head Pass", "description": "Scenic pass on the transfer leg."}
                        ]
                    },
                },
                "scenic_drives": [],
            },
        ]
    }

    discoverer._deduplicate_attractions_against_en_route_stops_tripwide(trip)

    telluride_attractions = [
        str(item.get("name", "") or "")
        for item in trip["destinations"][0]["ai_content"]["top_attractions"]
    ]
    pagosa_attractions = [
        str(item.get("name", "") or "")
        for item in trip["destinations"][1]["ai_content"]["top_attractions"]
    ]

    assert "Lizard Head Pass" not in telluride_attractions
    assert "Bear Creek Falls" in telluride_attractions
    assert "Treasure Falls" in pagosa_attractions


def test_trail_ai_candidate_rejected_when_filtered_constraints_fail_then_gets_maps_fallback():
    """Authoritative mode is the default (DEFAULT_DIRECT_BATCH_AUTHORITATIVE),
    so once every AllTrails/relaxed-seed path is exhausted for "The Narrows"
    this now gets the same safe maps-search fallback every other
    unresolved attraction gets, instead of being left with no link at all
    (the trail-like-branch no-link-fallback fix). The load-bearing
    assertion remains that the rejected url_candidate itself is never used.
    """
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._enable_filtered_alltrails_selection = True
    discoverer._alltrails_filtered_selection_cache = {}

    ai = {
        "top_attractions": [
            {
                "name": "The Narrows",
                "type": "hike",
                "description": "River hike through canyon walls.",
                "url_candidates": [
                    "https://www.alltrails.com/trail/us/utah/the-narrows",
                ],
            }
        ]
    }

    with patch.object(discoverer, "_search_alltrails_for_trail_filtered", return_value=None):
        with patch.object(discoverer, "_search_alltrails_for_trail", return_value=None):
            with patch.object(discoverer, "_search_alltrails_for_seed_relaxed", return_value=None):
                with patch.object(discoverer, "_search_attraction_from_direct_batch", return_value=None):
                    discoverer._discover_attractions(ai, "Zion National Park", "zion")

    attr = ai["top_attractions"][0]
    assert attr.get("url") == "https://www.google.com/maps/search/?api=1&query=The%20Narrows%20Zion%20National%20Park"
    assert attr.get("maps_url") == attr.get("url")
    assert attr.get("url") != "https://www.alltrails.com/trail/us/utah/the-narrows"


def test_angels_landing_seed_fails_filtered_constraints_gets_maps_fallback_in_authoritative_mode():
    """Since the trail-like-branch no-link-fallback fix, an authoritative-mode
    seed trail that exhausts every AllTrails path (filtered, standard, and
    relaxed-seed) gets the same safe maps-search fallback every other
    unresolved attraction gets, rather than no link at all. The
    fabrication-risk url_candidate itself must still never be used.
    """
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._enable_filtered_alltrails_selection = True
    discoverer._alltrails_filtered_selection_cache = {}
    discoverer._alltrails_source = "direct_link_batch"
    discoverer._direct_batch_authoritative = True

    ai = {
        "top_attractions": [
            {
                "name": "Angels Landing",
                "type": "hike",
                "description": "Iconic but strenuous route with significant elevation gain.",
                "url_candidates": [
                    "https://www.alltrails.com/trail/us/utah/angels-landing",
                ],
            }
        ]
    }

    with patch.object(discoverer, "_search_alltrails_for_trail_filtered", return_value=None):
        with patch.object(discoverer, "_search_alltrails_for_trail", return_value=None):
            with patch.object(discoverer, "_search_alltrails_for_seed_relaxed", return_value=None):
                with patch.object(discoverer, "_search_attraction_from_direct_batch", return_value=None):
                    discoverer._discover_attractions(ai, "Zion National Park", "zion")

    attr = ai["top_attractions"][0]
    assert attr.get("url") == "https://www.google.com/maps/search/?api=1&query=Angels%20Landing%20Zion%20National%20Park"
    assert attr.get("maps_url") == attr.get("url")
    assert attr.get("url") != "https://www.alltrails.com/trail/us/utah/angels-landing"


def test_narrows_seed_fails_filtered_constraints_does_not_override_authoritative_direct_batch() -> None:
    """The url_candidate fabrication guard must hold even for a non-seed
    "The Narrows" item (no seed_names passed, so the relaxed-seed recovery
    path is never attempted): authoritative direct-batch mode must not
    resolve the AI-suggested url_candidate when AllTrails search finds no
    match. Since the trail-like-branch no-link-fallback fix, it gets the
    safe maps-search fallback instead of no link at all.
    """
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._enable_filtered_alltrails_selection = True
    discoverer._alltrails_filtered_selection_cache = {}
    discoverer._alltrails_source = "direct_link_batch"
    discoverer._direct_batch_authoritative = True

    ai = {
        "top_attractions": [
            {
                "name": "The Narrows",
                "type": "hike",
                "description": "Iconic Zion canyon route.",
                "url_candidates": [
                    "https://www.alltrails.com/trail/us/utah/the-narrows-top-down",
                ],
            }
        ]
    }

    with patch.object(discoverer, "_search_alltrails_for_trail_filtered", return_value=None):
        with patch.object(discoverer, "_search_alltrails_for_trail", return_value=None):
            with patch.object(discoverer, "_search_attraction_from_direct_batch", return_value=None):
                discoverer._discover_attractions(ai, "Zion National Park", "zion")

    attr = ai["top_attractions"][0]
    assert attr.get("url") == "https://www.google.com/maps/search/?api=1&query=The%20Narrows%20Zion%20National%20Park"
    assert attr.get("maps_url") == attr.get("url")
    assert attr.get("url") != "https://www.alltrails.com/trail/us/utah/the-narrows-top-down"


def test_seed_trail_uses_relaxed_alltrails_recovery_before_maps_fallback() -> None:
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._enable_filtered_alltrails_selection = True
    discoverer._alltrails_filtered_selection_cache = {}
    discoverer._alltrails_source = "direct_link_batch"
    discoverer._direct_batch_authoritative = True

    ai = {
        "top_attractions": [
            {
                "name": "The Narrows",
                "type": "hike",
                "description": "Iconic Zion canyon route.",
                "url_candidates": [
                    "https://www.alltrails.com/trail/us/utah/the-narrows-top-down",
                ],
            }
        ]
    }

    with patch.object(discoverer, "_search_alltrails_for_trail", return_value=None):
        with patch.object(
            discoverer,
            "_search_alltrails_for_seed_relaxed",
            return_value="https://www.alltrails.com/trail/us/utah/the-narrows-trail",
        ):
            discoverer._discover_attractions(ai, "Zion National Park", "zion", seed_names=["The Narrows"])

    attr = ai["top_attractions"][0]
    assert attr.get("url") == "https://www.alltrails.com/trail/us/utah/the-narrows-trail"
    assert "maps_url" not in attr


def test_human_history_museum_is_not_classified_as_trail_like_when_type_is_hike() -> None:
    discoverer = URLDiscoverer.__new__(URLDiscoverer)

    assert discoverer._is_trail_like_attraction(
        "Human History Museum",
        "hike",
        "Exhibits on regional cultural history and early inhabitants.",
    ) is False


def test_seeded_alltrails_candidate_with_404_marker_gets_maps_fallback_not_the_dead_link():
    """The 404-marker fail-closed policy still rejects the dead url_candidate
    (that's the load-bearing behavior this test protects). Since the
    trail-like-branch no-link-fallback fix, the item then gets the same safe
    maps-search fallback every other unresolved attraction gets, instead of
    being left with no link and no maps fallback at all.
    """
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._enable_filtered_alltrails_selection = False

    ai = {
        "top_attractions": [
            {
                "name": "Angels Landing",
                "type": "hike",
                "description": "Iconic route in Zion.",
                "url_candidates": [
                    "https://www.alltrails.com/trail/us/utah/angels-landing",
                ],
            }
        ]
    }

    with patch.object(discoverer, "_fetch_page_text", return_value=(False, 404, "")):
        with patch.object(discoverer, "_search_alltrails_for_trail", return_value=None):
            with patch.object(discoverer, "_search_alltrails_for_seed_relaxed", return_value=None):
                with patch.object(discoverer, "_search_attraction_from_direct_batch", return_value=None):
                    discoverer._discover_attractions(ai, "Zion National Park", "zion")

    attraction = ai["top_attractions"][0]
    assert attraction.get("url") == "https://www.google.com/maps/search/?api=1&query=Angels%20Landing%20Zion%20National%20Park"
    assert attraction.get("maps_url") == attraction.get("url")
    assert attraction.get("url") != "https://www.alltrails.com/trail/us/utah/angels-landing"


def test_seeded_alltrails_candidate_blocked_fetch_with_verify_404_gets_maps_fallback_not_the_dead_link():
    """The blocked-fetch-plus-verify-404 fail-closed policy still rejects the
    dead url_candidate (that's the load-bearing behavior this test
    protects). Since the trail-like-branch no-link-fallback fix, the item
    then gets the same safe maps-search fallback every other unresolved
    attraction gets, instead of being left with no link and no maps
    fallback at all.
    """
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._enable_filtered_alltrails_selection = False
    discoverer._alltrails_slug_denylist = frozenset()

    ai = {
        "top_attractions": [
            {
                "name": "Inspiration Point",
                "type": "hike",
                "description": "Popular viewpoint trail.",
                "url_candidates": [
                    "https://www.alltrails.com/trail/us/utah/inspiration-point-trail",
                ],
            }
        ]
    }

    with patch.object(discoverer, "_fetch_page_text", return_value=(False, 403, "")):
        with patch.object(discoverer, "_verify_url_cached", return_value=(False, 404)):
            with patch.object(discoverer, "_search_alltrails_for_trail", return_value=None):
                with patch.object(discoverer, "_search_alltrails_for_seed_relaxed", return_value=None):
                    with patch.object(discoverer, "_search_attraction_from_direct_batch", return_value=None):
                        discoverer._discover_attractions(ai, "Zion National Park", "zion")

    attraction = ai["top_attractions"][0]
    assert attraction.get("url") == "https://www.google.com/maps/search/?api=1&query=Inspiration%20Point%20Zion%20National%20Park"
    assert attraction.get("maps_url") == attraction.get("url")
    assert attraction.get("url") != "https://www.alltrails.com/trail/us/utah/inspiration-point-trail"


def test_non_strict_trail_ai_candidate_can_pass_when_filtered_metadata_missing():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._enable_filtered_alltrails_selection = True
    discoverer._alltrails_filtered_selection_cache = {}
    discoverer._alltrails_min_confidence_for_publish = "low"
    discoverer._alltrails_source = "search"
    discoverer._direct_batch_authoritative = False

    ai = {
        "top_attractions": [
            {
                "name": "Canyon Overlook Trail",
                "type": "hike",
                "description": "Short canyon overlook hike.",
                "url_candidates": [
                    "https://www.alltrails.com/trail/us/utah/canyon-overlook-trail",
                ],
            }
        ]
    }

    with patch.object(discoverer, "_search_alltrails_for_trail_filtered", return_value=None):
        with patch.object(discoverer, "_retain_discovered_url", side_effect=lambda url, *_args, **_kwargs: url):
            discoverer._discover_attractions(ai, "Zion National Park", "zion")

    assert ai["top_attractions"][0]["url"] == "https://www.alltrails.com/trail/us/utah/canyon-overlook-trail"


def test_en_route_discovery_disallows_alltrails_results_upfront():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    captured = {}

    def fake_search(variants, **kwargs):
        captured.update(kwargs)
        return None

    ai = {
        "getting_here": {
            "en_route_stops": [{"name": "Wilson Arch"}],
        }
    }

    with patch.object(discoverer, "_search_first", side_effect=fake_search):
        discoverer._discover_en_route_stops(ai, "Moab")

    assert captured.get("allow_alltrails") is False


def test_discover_en_route_stops_can_use_direct_batch_source() -> None:
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._en_route_source = "direct_link_batch"

    ai = {
        "getting_here": {
            "en_route_stops": [{"name": "Wilson Arch", "detour_time_minutes": 10}],
        }
    }

    with patch.object(discoverer, "_get_en_route_direct_batch_rows_for_destination", return_value=[{"name": "Wilson Arch", "detour_time_minutes": 10}]):
        with patch.object(discoverer, "_search_en_route_stop_from_direct_batch", return_value="https://www.blm.gov/visit/wilson-arch"):
            with patch.object(discoverer, "_search_first") as fallback_search:
                discoverer._discover_en_route_stops(ai, "Moab")

    assert ai["getting_here"]["en_route_stops"][0]["url"] == "https://www.blm.gov/visit/wilson-arch"
    fallback_search.assert_not_called()


def test_discover_en_route_stops_assigns_maps_fallback_url_when_unresolved() -> None:
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._en_route_source = "search"

    ai = {
        "getting_here": {
            "en_route_stops": [{"name": "Wilson Arch"}],
        }
    }

    with patch.object(discoverer, "_search_first", return_value=None):
        discoverer._discover_en_route_stops(ai, "Moab")

    stop = ai["getting_here"]["en_route_stops"][0]
    assert stop["url"].startswith("https://www.google.com/maps/search/?api=1&query=")
    assert stop["maps_url"].startswith("https://www.google.com/maps/search/?api=1&query=")


def test_discover_en_route_stops_removes_stop_when_no_canonical_or_fallback_url() -> None:
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._en_route_source = "search"

    ai = {
        "getting_here": {
            "en_route_stops": [{"name": ""}],
        }
    }

    with patch.object(discoverer, "_search_first", return_value=None):
        discoverer._discover_en_route_stops(ai, "", origin_name="")

    assert ai["getting_here"]["en_route_stops"] == []


def test_en_route_maps_fallback_query_adds_route_context_for_ambiguous_stop_name() -> None:
    discoverer = URLDiscoverer.__new__(URLDiscoverer)

    query = discoverer._en_route_maps_fallback_query_text(
        "Leeds Historic District",
        "Las Vegas, Nevada",
        "St. George, Utah",
    )

    assert "st. george" in query.lower() or "st george" in query.lower()
    assert "route from" in query.lower()


def test_looks_location_qualified_recognizes_saint_and_st_variants() -> None:
    discoverer = URLDiscoverer.__new__(URLDiscoverer)

    assert discoverer._looks_location_qualified("Saint George")
    assert discoverer._looks_location_qualified("St George")
    assert not discoverer._looks_location_qualified("Temple View")


def test_discover_en_route_stops_prunes_waypoints_beyond_destination_leg() -> None:
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._en_route_source = "search"

    ai = {
        "getting_here": {
            "en_route_stops": [
                {"name": "Mesquite"},
                {"name": "Cedar City"},
            ],
        }
    }

    geocodes = {
        "Mesquite": (36.8055, -114.0672),
        "Cedar City": (37.6775, -113.0619),
    }

    with patch.object(discoverer, "_search_first", return_value=None):
        with patch.object(
            discoverer,
            "_geocode_en_route_stop_for_route",
            side_effect=lambda stop_name, **_kwargs: geocodes.get(stop_name),
        ):
            discoverer._discover_en_route_stops(
                ai,
                "St. George, Utah",
                origin_name="Las Vegas, Nevada",
                origin_lat=36.1699,
                origin_lng=-115.1398,
                dest_lat=37.0965,
                dest_lng=-113.5684,
            )

    names = [str(stop.get("name", "") or "") for stop in ai["getting_here"]["en_route_stops"]]
    assert names == ["Mesquite"]


def test_discover_en_route_stops_removes_stop_that_duplicates_destination_name() -> None:
    """Regression for the project owner's Google Maps screenshot of the
    Bryce -> Capitol Reef leg: 'Capitol Reef National Park' appeared as its
    own waypoint entry immediately before the real destination pin --
    '... -> Lower Calf Creek Falls -> Capitol Reef National Park -> Capitol
    Reef National Park -> 2600 UT-24, Torrey UT'. An en-route stop whose
    name IS the destination itself is never a real detour and must be
    dropped entirely (not just excluded from waypoint ordering), so it also
    stops showing up as a redundant 'can't-miss enroute' card."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._en_route_source = "search"

    ai = {
        "getting_here": {
            "en_route_stops": [
                {"name": "Mossy Cave"},
                {"name": "Capitol Reef National Park"},
            ],
        }
    }

    with patch.object(discoverer, "_search_first", return_value="https://example.com/mossy-cave"):
        with patch.object(discoverer, "_geocode_en_route_stop_for_route", return_value=None):
            discoverer._discover_en_route_stops(
                ai,
                "Capitol Reef National Park",
                origin_name="Bryce Canyon National Park",
                origin_lat=37.5707948,
                origin_lng=-112.1855939,
                dest_lat=38.0670286,
                dest_lng=-111.1552562,
            )

    names = [str(stop.get("name", "") or "") for stop in ai["getting_here"]["en_route_stops"]]
    assert "Capitol Reef National Park" not in names
    assert "Mossy Cave" in names


def test_discover_en_route_stops_uses_geocoded_coordinates_for_maps_url() -> None:
    """Regression for dipstick55 Theme E: 'Swasey's Beach' is a real,
    correctly in-region BLM beach that a free-text Google Maps search doesn't
    reliably resolve to a single point ('the address isn't specific enough
    for a usable link'). When _prune_en_route_stops_by_geometry already
    verified precise coordinates for a stop (a real, free Nominatim lookup,
    confirmed live to resolve "Swasey's Beach, USA" to Grand County, UT),
    the final maps_url must use those coordinates -- which always resolve to
    exactly one point -- instead of an ambiguous free-text query."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._en_route_source = "search"

    ai = {
        "getting_here": {
            "en_route_stops": [
                {
                    "name": "Swasey's Beach",
                    "maps_url": "https://www.google.com/maps/search/?api=1&query=Swasey%27s+Beach+Campground+Green+River+UT",
                },
            ],
        }
    }

    swasey_coords = (39.1154401, -110.1096439)

    with patch.object(discoverer, "_search_first", return_value=None):
        with patch.object(discoverer, "_geocode_en_route_stop_for_route", return_value=swasey_coords):
            discoverer._discover_en_route_stops(
                ai,
                "Moab",
                origin_name="Capitol Reef National Park",
                origin_lat=38.2872,
                origin_lng=-111.2615,
                dest_lat=38.5733,
                dest_lng=-109.5498,
            )

    stop = ai["getting_here"]["en_route_stops"][0]
    expected = f"https://www.google.com/maps/search/?api=1&query={swasey_coords[0]},{swasey_coords[1]}"
    assert stop.get("maps_url") == expected
    # The clickable card link ("url", not just the maps icon) must also use
    # the precise coordinate query, not the ambiguous free-text one -- the
    # real dipstick55 output rendered the exact same free-text query as both
    # fields' href.
    assert stop.get("url") == expected


def test_discover_en_route_stops_marks_waypoint_ineligible_when_geocode_missing() -> None:
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._en_route_source = "search"

    ai = {
        "getting_here": {
            "en_route_stops": [
                {"name": "Mystery Stop"},
            ],
        }
    }

    with patch.object(discoverer, "_search_first", return_value=None):
        with patch.object(discoverer, "_geocode_en_route_stop_for_route", return_value=None):
            discoverer._discover_en_route_stops(
                ai,
                "St. George, Utah",
                origin_name="Las Vegas, Nevada",
                origin_lat=36.1699,
                origin_lng=-115.1398,
                dest_lat=37.0965,
                dest_lng=-113.5684,
            )

    stop = ai["getting_here"]["en_route_stops"][0]
    assert stop.get("route_waypoint_eligible") is False


def test_discover_en_route_stops_keeps_waypoint_eligible_when_geocode_missing_but_detour_metadata_is_good() -> None:
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._en_route_source = "search"
    discoverer._en_route_detour_max_minutes = 20
    discoverer._en_route_detour_max_miles = 0.0
    discoverer._en_route_require_detour_metadata = True

    ai = {
        "getting_here": {
            "en_route_stops": [
                {
                    "name": "Virgin River Gorge Overlook",
                    "detour_time_minutes": 10,
                },
            ],
        }
    }

    with patch.object(discoverer, "_search_first", return_value=None):
        with patch.object(discoverer, "_geocode_en_route_stop_for_route", return_value=None):
            discoverer._discover_en_route_stops(
                ai,
                "St. George, Utah",
                origin_name="Las Vegas, Nevada",
                origin_lat=36.1699,
                origin_lng=-115.1398,
                dest_lat=37.0965,
                dest_lng=-113.5684,
            )

    stop = ai["getting_here"]["en_route_stops"][0]
    assert stop.get("route_waypoint_eligible") is True


def test_discover_en_route_stops_keeps_generic_waypoint_ineligible_when_geocode_missing_even_with_detour_metadata() -> None:
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._en_route_source = "search"
    discoverer._en_route_detour_max_minutes = 20
    discoverer._en_route_detour_max_miles = 0.0
    discoverer._en_route_require_detour_metadata = True

    ai = {
        "getting_here": {
            "en_route_stops": [
                {
                    "name": "Scenic Stops on the Drive from Las Vegas to St. George",
                    "detour_time_minutes": 10,
                },
            ],
        }
    }

    with patch.object(discoverer, "_search_first", return_value=None):
        with patch.object(discoverer, "_geocode_en_route_stop_for_route", return_value=None):
            discoverer._discover_en_route_stops(
                ai,
                "St. George, Utah",
                origin_name="Las Vegas, Nevada",
                origin_lat=36.1699,
                origin_lng=-115.1398,
                dest_lat=37.0965,
                dest_lng=-113.5684,
            )

    stop = ai["getting_here"]["en_route_stops"][0]
    assert stop.get("route_waypoint_eligible") is False


def test_discover_en_route_stops_seeds_from_direct_batch_when_missing() -> None:
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._en_route_source = "direct_link_batch"

    ai = {"getting_here": {"en_route_stops": []}}
    rows = [
        {
            "name": "Lizard Head Pass",
            "url": "https://www.google.com/maps/search/?api=1&query=Lizard+Head+Pass+Telluride+CO",
            "detour_time_minutes": 12,
        },
        {
            "name": "Rico Historic District",
            "url": "https://www.colorado.com/articles/why-rico-colorado-worth-stop",
            "detour_time_minutes": 18,
        },
    ]

    with patch.object(discoverer, "_get_en_route_direct_batch_rows_for_destination", return_value=rows):
        with patch.object(
            discoverer,
            "_search_en_route_stop_from_direct_batch",
            side_effect=[rows[0]["url"], rows[1]["url"]],
        ):
            discoverer._discover_en_route_stops(ai, "Telluride")

    names = [str(s.get("name", "") or "") for s in ai["getting_here"]["en_route_stops"]]
    assert "Lizard Head Pass" in names
    assert "Rico Historic District" in names


def test_discover_en_route_stops_keeps_generic_html_title_when_it_has_real_url() -> None:
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._en_route_source = "direct_link_batch"

    ai = {"getting_here": {"en_route_stops": []}}
    rows = [
        {
            "name": "Best stops along the route",
            "url": "https://www.blm.gov/visit/wilson-arch",
            "detour_time_minutes": 10,
        }
    ]

    with patch.object(discoverer, "_get_en_route_direct_batch_rows_for_destination", return_value=rows):
        with patch.object(discoverer, "_search_en_route_stop_from_direct_batch", return_value=rows[0]["url"]):
            with patch.object(discoverer, "_search_first") as fallback_search:
                discoverer._discover_en_route_stops(ai, "Moab")

    stops = ai["getting_here"]["en_route_stops"]
    assert [str(item.get("name", "") or "") for item in stops] == ["Best stops along the route"]
    assert stops[0]["url"] == "https://www.blm.gov/visit/wilson-arch"
    fallback_search.assert_not_called()


def test_discover_en_route_stops_mines_named_stops_from_generic_item_description() -> None:
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._en_route_source = "direct_link_batch"

    ai = {
        "getting_here": {
            "en_route_stops": [
                {
                    "name": "Scenic Stops on the Drive from Zion to Bryce Canyon",
                    "description": (
                        "Quick cultural and scenic detours \u226420 min: "
                        "Coral Pink Sand Dunes State Park overlook (short spur, 4.6 stars/600 reviews), "
                        "Google Maps pin. Mt. Carmel Junction historic spots. "
                        "Avoids gas/rest areas; all 4+ rated."
                    ),
                }
            ]
        }
    }

    with patch.object(discoverer, "_get_en_route_direct_batch_rows_for_destination", return_value=[]):
        with patch.object(discoverer, "_search_en_route_stop_from_direct_batch", return_value=None):
            with patch.object(discoverer, "_search_first", return_value=None):
                discoverer._discover_en_route_stops(ai, "Bryce Canyon National Park", origin_name="Zion National Park")

    stop_names = [str(s.get("name", "") or "") for s in ai["getting_here"]["en_route_stops"]]
    assert "Scenic Stops on the Drive from Zion to Bryce Canyon" not in stop_names
    assert any("Coral Pink Sand Dunes" in n for n in stop_names), stop_names
    assert any("Mt. Carmel Junction" in n or "Mt Carmel Junction" in n for n in stop_names), stop_names


def test_extract_named_stops_from_description_returns_proper_noun_fragments() -> None:
    from generator.url_discovery import URLDiscoverer as UD
    desc = (
        "Quick detours \u226420 min: Coral Pink Sand Dunes State Park overlook "
        "(short spur, 4.6 stars/600 reviews), Google Maps pin. "
        "Mt. Carmel Junction historic spots. Avoids gas/rest areas; all 4+ rated."
    )
    result = UD._extract_named_stops_from_description(desc)
    assert any("Coral Pink Sand Dunes" in n for n in result), result
    assert any("Mt. Carmel Junction" in n or "Mt Carmel Junction" in n for n in result), result
    assert not any("Avoids" in n for n in result), result
    assert not any("Google" in n for n in result), result


def test_discover_en_route_stops_preserves_mined_stop_metadata_from_generic_description() -> None:
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._en_route_source = "direct_link_batch"

    ai = {
        "getting_here": {
            "en_route_stops": [
                {
                    "name": "Scenic Stops on the Drive from Zion to Bryce Canyon",
                    "description": (
                        "Quick cultural and scenic detours ≤20 min: "
                        "Coral Pink Sand Dunes State Park overlook (short spur, 4.6 stars/600 reviews), "
                        "Google Maps pin. Mt. Carmel Junction historic spots."
                    ),
                }
            ]
        }
    }

    with patch.object(discoverer, "_get_en_route_direct_batch_rows_for_destination", return_value=[]):
        with patch.object(discoverer, "_search_en_route_stop_from_direct_batch", return_value=None):
            with patch.object(discoverer, "_search_first", return_value=None):
                discoverer._discover_en_route_stops(ai, "Bryce Canyon National Park", origin_name="Zion National Park")

    stops = ai["getting_here"]["en_route_stops"]
    assert any(str(stop.get("name", "") or "") == "Coral Pink Sand Dunes State Park overlook" for stop in stops), stops
    assert any(str(stop.get("detour_time_minutes", "") or "") == "20" for stop in stops), stops
    assert all(str(stop.get("detour_distance_miles", "") or "") != "None" for stop in stops), stops


# ── Bug 1 (dipstick63): en-route stop vs. destination's own scenic-drive/  ──
# attraction list cross-check dedup (Kolob Canyons Scenic Drive vs. Kolob
# Canyons Road), and intra-leg same-place dedup (Moab Museum bare-address
# vs. named entry).

def test_en_route_stop_duplicates_destination_own_list_matches_scenic_drive() -> None:
    """Direct unit test for the Kolob Canyons cross-list matcher: an
    en-route stop and a destination's own scenic-drives entry that reduce to
    the same significant-token set are recognized as the same real place."""
    dest = {
        "scenic_drives": [
            {
                "title": "Kolob Canyons Road",
                "distance_or_duration": "5 miles",
                "description": "A lesser-known but beautiful drive that leads to the Kolob Canyons section of Zion.",
            },
        ],
    }
    match = URLDiscoverer._en_route_stop_duplicates_destination_own_list("Kolob Canyons Scenic Drive", dest)
    assert match == "Kolob Canyons Road"


def test_en_route_stop_duplicates_destination_own_list_matches_top_attraction() -> None:
    dest = {
        "ai_content": {
            "top_attractions": [{"name": "Kolob Canyons Trail"}],
        },
    }
    match = URLDiscoverer._en_route_stop_duplicates_destination_own_list("Kolob Canyons Scenic Drive", dest)
    assert match == "Kolob Canyons Trail"


def test_en_route_stop_duplicates_destination_own_list_no_false_positive() -> None:
    dest = {"scenic_drives": [{"title": "Zion Canyon Scenic Drive"}]}
    assert URLDiscoverer._en_route_stop_duplicates_destination_own_list("Kolob Canyons Scenic Drive", dest) is None


def test_discover_en_route_stops_drops_stop_duplicating_destination_scenic_drive() -> None:
    """Regression for dipstick63 Bug 1: Zion's 'Getting Here' en-route stops
    included 'Kolob Canyons Scenic Drive' (linked to zionnationalpark.com)
    while Zion's own scenic_drives list -- populated by ai_content.py's
    destination-content generation, which always runs before URL discovery
    -- independently included 'Kolob Canyons Road' for the exact same real
    place. The en-route-stop duplicate must be dropped in favor of the
    destination's own (fuller, more authoritative) entry."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._en_route_source = "search"

    ai = {
        "getting_here": {
            "en_route_stops": [
                {
                    "name": "Kolob Canyons Scenic Drive",
                    "url": "https://zionnationalpark.com/getting-around/",
                    "description": (
                        "Kolob Canyons Scenic Drive runs for 5 miles from the Kolob Canyons "
                        "Visitor Center along a ridge up to Kolob Canyons Viewpoint."
                    ),
                    "detour_distance_miles": 10,
                    "detour_time_minutes": 15,
                },
            ],
        }
    }
    dest = {
        "name": "Zion National Park",
        "scenic_drives": [
            {
                "title": "Kolob Canyons Road",
                "distance_or_duration": "5 miles",
                "description": "A lesser-known but beautiful drive that leads to the Kolob Canyons section of Zion.",
            }
        ],
    }

    with patch.object(discoverer, "_search_first", return_value=None):
        discoverer._discover_en_route_stops(
            ai,
            "Zion National Park",
            origin_name="St. George, Utah",
            dest=dest,
        )

    assert ai["getting_here"]["en_route_stops"] == []


def test_en_route_stop_address_key_matches_bare_address_and_named_variant() -> None:
    """Direct unit test for the Moab Museum address-key matcher: a bare
    street address and a venue name plus the same address reduce to the
    same normalized key."""
    bare = URLDiscoverer._en_route_stop_address_key("118 E Center St, Moab, UT 84532")
    named = URLDiscoverer._en_route_stop_address_key("Moab Museum, 118 E Center St, Moab, UT")
    assert bare and named
    assert bare == named


def test_en_route_stop_name_is_bare_street_address() -> None:
    assert URLDiscoverer._en_route_stop_name_is_bare_street_address("118 E Center St, Moab, UT 84532") is True
    assert URLDiscoverer._en_route_stop_name_is_bare_street_address("Moab Museum, 118 E Center St, Moab, UT") is False


def test_discover_en_route_stops_dedupes_bare_address_against_named_entry_same_leg() -> None:
    """Regression for a real Moab -> Arches leg Google Maps waypoint
    screenshot: the harvested en_route_stops list for a single leg included
    both a bare geocoded address ('118 E Center St, Moab, UT 84532') and a
    named entry for the exact same address ('Moab Museum, 118 E Center St,
    Moab, UT') as two separate candidates -- the same real place forced onto
    the route as two waypoints, contributing to the observed route bouncing
    between Moab town and inside Arches. The named entry (more informative)
    must survive; the bare-address duplicate must be dropped."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._en_route_source = "search"

    ai = {
        "getting_here": {
            "en_route_stops": [
                {"name": "118 E Center St, Moab, UT 84532"},
                {
                    "name": "Moab Museum, 118 E Center St, Moab, UT",
                    "description": "Local history museum in downtown Moab.",
                },
            ],
        }
    }

    with patch.object(discoverer, "_search_first", return_value=None):
        discoverer._discover_en_route_stops(
            ai,
            "Arches National Park",
            origin_name="Moab, Utah",
        )

    names = [str(s.get("name", "") or "") for s in ai["getting_here"]["en_route_stops"]]
    assert names == ["Moab Museum, 118 E Center St, Moab, UT"]


def test_dedupe_en_route_stops_same_leg_by_geocode_proximity_keeps_named_entry() -> None:
    """Same-place collapse for two stops that don't share a parseable street
    address but geocode to essentially the same point -- the geocode-based
    fallback for the address-key pass above."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    stops = [
        {"name": "Moab Museum of Film and Western Heritage", "geocode_lat": 38.5733, "geocode_lng": -109.5498,
         "description": "Museum near downtown Moab."},
        {"name": "Downtown Moab Pin", "geocode_lat": 38.57335, "geocode_lng": -109.54985},
    ]
    result = discoverer._dedupe_en_route_stops_same_leg_by_geocode_proximity(stops, "Arches National Park")
    names = [s["name"] for s in result]
    assert names == ["Moab Museum of Film and Western Heritage"]


# ── Bug 2 (dipstick63): en-route detour distance/time corrected against  ──
# real geocoded coordinates rather than trusted verbatim from AI/text-mined
# prose (Kolob Canyons Scenic Drive rendering "(10 mi detour | 15 min)" for
# a real ~18-20 mi one-way detour off I-15).

def test_en_route_stop_geometry_grounded_detour_floor_rejects_bogus_short_value() -> None:
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    min_miles, min_minutes = discoverer._en_route_stop_geometry_grounded_detour_floor(20.8)
    # A round trip can never be shorter than 2x the one-way perpendicular
    # offset -- the real Kolob Canyons "10 mi detour" figure is well below
    # this floor and is therefore geometrically impossible.
    assert min_miles == pytest.approx(41.6)
    assert 10.0 < min_miles
    assert min_minutes > 15  # the bogus "15 min" figure is also impossible


def test_en_route_stop_geometry_grounded_detour_estimate_exceeds_floor() -> None:
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    min_miles, _min_minutes = discoverer._en_route_stop_geometry_grounded_detour_floor(20.8)
    est_miles, est_minutes = discoverer._en_route_stop_geometry_grounded_detour_estimate(20.8)
    assert est_miles > min_miles
    assert est_minutes > 0


def test_resolve_en_route_stop_detour_metrics_rejects_implausible_speed_pair() -> None:
    """Real example (Kodachrome Basin State Park): '22 mi detour | 15 min'
    implies an 88 mph average -- unrealistic for any detour road. Each
    number individually clears its own geometric floor (the stop sits only
    ~5 mi perpendicular off the route, so the per-dimension floor is small:
    10 mi / 9 min), so the existing independent-per-dimension checks don't
    catch it. The combined-pair check must still override both numbers to
    the geometry-grounded estimate."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    stop = {
        "name": "Kodachrome Basin State Park",
        "detour_distance_miles": 22,
        "detour_time_minutes": 15,
        "geocode_lat": 37.5,
        "geocode_lng": -111.9,
    }
    with patch.object(discoverer, "_route_perpendicular_distance_miles", return_value=5.0):
        final_miles, final_minutes, overridden = discoverer._resolve_en_route_stop_detour_metrics_against_geometry(
            stop, origin=(37.0, -112.0), dest=(38.0, -111.0)
        )

    assert overridden is True
    implied_mph = final_miles / (final_minutes / 60.0)
    assert implied_mph <= 70.0, (final_miles, final_minutes, implied_mph)
    # Confirms it's the geometry-grounded *estimate*, not a value that just
    # barely squeaks under the speed ceiling.
    est_miles, est_minutes = discoverer._en_route_stop_geometry_grounded_detour_estimate(5.0)
    assert final_miles == est_miles
    assert final_minutes == est_minutes


def test_resolve_en_route_stop_detour_metrics_keeps_plausible_pair_unmodified() -> None:
    """Control: a text-mined pair that clears both individual floors AND
    implies a realistic speed must not be touched."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    stop = {
        "name": "Realistic Overlook",
        "detour_distance_miles": 12,
        "detour_time_minutes": 15,
        "geocode_lat": 37.5,
        "geocode_lng": -111.9,
    }
    with patch.object(discoverer, "_route_perpendicular_distance_miles", return_value=5.0):
        final_miles, final_minutes, overridden = discoverer._resolve_en_route_stop_detour_metrics_against_geometry(
            stop, origin=(37.0, -112.0), dest=(38.0, -111.0)
        )

    assert overridden is False
    assert final_miles == 12
    assert final_minutes == 15


def test_discover_en_route_stops_corrects_geometrically_impossible_detour_distance() -> None:
    """Regression for dipstick63 Bug 2: 'Kolob Canyons Scenic Drive' rendered
    '(10 mi detour | 15 min)' on the real St. George -> Springdale (Zion)
    leg. The stop's real coordinates (Kolob Canyons Visitor Center, off I-15
    exit 40) sit ~21.8 mi perpendicular-offset from the direct St. George ->
    Springdale route line -- a round-trip detour to a point that far off the
    route can never be as short as 10 miles / 15 minutes, so the
    AI-provided/text-mined figures must be replaced with a geometry-grounded
    value once the stop's own geocode is available."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._en_route_source = "search"

    kolob_coords = (37.4530, -113.3564)
    ai = {
        "getting_here": {
            "en_route_stops": [
                {
                    "name": "Kolob Canyons Scenic Drive",
                    "detour_distance_miles": 10,
                    "detour_time_minutes": 15,
                },
            ],
        }
    }

    with patch.object(discoverer, "_search_first", return_value=None):
        with patch.object(discoverer, "_geocode_en_route_stop_for_route", return_value=kolob_coords):
            discoverer._discover_en_route_stops(
                ai,
                "Zion National Park",
                origin_name="St. George, Utah",
                origin_lat=37.0965,
                origin_lng=-113.5684,
                dest_lat=37.1889,
                dest_lng=-112.9975,
            )

    stop = ai["getting_here"]["en_route_stops"][0]
    # The real round-trip detour is on the order of 40+ miles / 35-45+ min
    # each way -- nowhere close to the bogus "10 mi | 15 min" the AI/text
    # mining originally produced.
    assert stop["detour_distance_miles"] > 30, stop
    assert stop["detour_time_minutes"] > 30, stop


def test_infer_destination_day_count_from_date_ranges() -> None:
    assert URLDiscoverer._infer_destination_day_count("October 17, 2026") == 1
    assert URLDiscoverer._infer_destination_day_count("October 19-21, 2026") == 3
    assert URLDiscoverer._infer_destination_day_count("2026-10-19 / 2026-10-22") == 4
    assert URLDiscoverer._infer_destination_day_count("") == 1


def test_prioritize_direct_batch_attractions_never_evicts_seed() -> None:
    """A seed attraction that doesn't match any harvested row must still survive
    the merge -- attractions must never be evicted the way en-route stops are."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    existing = [{"name": "The Narrows", "type": "hike"}]
    rows = [
        {"name": "St. George Tabernacle", "url": "https://www.stgeorgetabernacle.com/"},
        {"name": "Rosenbruch Wildlife Museum", "url": "https://www.rosenbruch.org/"},
    ]

    with patch.object(discoverer, "_get_attraction_direct_batch_rows_for_destination", return_value=rows):
        out = discoverer._prioritize_direct_batch_attractions(
            existing, "St. George, Utah", "October 17, 2026", seed_names=["The Narrows"]
        )

    names = [item["name"] for item in out]
    assert "The Narrows" in names


def test_prioritize_direct_batch_attractions_picks_highest_rated_first() -> None:
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._attraction_direct_batch_items_per_day = 2
    existing: list[dict] = []
    rows = [
        {"name": "St. George Tabernacle", "url": "https://a.example/", "rating": 4.4},
        {"name": "Rosenbruch Wildlife Museum", "url": "https://b.example/", "rating": 4.8},
        {"name": "St. George Art Museum", "url": "https://c.example/", "rating": 4.6},
        {"name": "Dinosaur Discovery Site", "url": "https://d.example/", "rating": 4.3},
    ]

    with patch.object(discoverer, "_get_attraction_direct_batch_rows_for_destination", return_value=rows):
        out = discoverer._prioritize_direct_batch_attractions(
            existing, "St. George, Utah", "October 17, 2026"
        )

    # 1 day * 2 items/day = 2 slots; must be the two highest-rated rows.
    names = [item["name"] for item in out]
    assert names == ["Rosenbruch Wildlife Museum", "St. George Art Museum"]


def test_prioritize_direct_batch_attractions_scales_target_count_per_day() -> None:
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._attraction_direct_batch_items_per_day = 2
    existing: list[dict] = []
    rows = [
        {"name": f"Attraction {i}", "url": f"https://example.com/{i}", "rating": 4.5 + (i * 0.01)}
        for i in range(8)
    ]

    with patch.object(discoverer, "_get_attraction_direct_batch_rows_for_destination", return_value=rows):
        out_one_day = discoverer._prioritize_direct_batch_attractions(
            list(existing), "Bryce Canyon National Park", "October 19, 2026"
        )
        out_three_day = discoverer._prioritize_direct_batch_attractions(
            list(existing), "Bryce Canyon National Park", "October 19-21, 2026"
        )

    assert len(out_one_day) == 2
    assert len(out_three_day) == 6


def test_prioritize_direct_batch_attractions_excludes_trail_like_rows() -> None:
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    existing: list[dict] = []
    rows = [
        {"name": "Angels Landing Trail", "url": "https://a.example/", "rating": 4.9},
        {"name": "Zion Human History Museum", "url": "https://b.example/", "rating": 4.5},
    ]

    with patch.object(discoverer, "_get_attraction_direct_batch_rows_for_destination", return_value=rows):
        out = discoverer._prioritize_direct_batch_attractions(
            existing, "Zion National Park", "October 18, 2026"
        )

    names = [item["name"] for item in out]
    assert "Angels Landing Trail" not in names
    assert "Zion Human History Museum" in names


def test_prioritize_direct_batch_trails_never_evicts_existing_and_injects_new() -> None:
    """Full-pipeline regression for a real reported bug: St. George's AllTrails
    batch harvested 20 candidates but only 1 trail (whatever the AI happened to
    generate) ever got a chance -- there was no injection mechanism for trails,
    mirroring the gap attractions had before being fixed."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._trail_direct_batch_items_per_day = 2
    discoverer._max_trail_miles = 3.0
    existing = [{"name": "Jenny's Canyon Trail", "type": "hike"}]
    rows = [
        {"name": "Jenny's Canyon Trail", "url": "https://www.alltrails.com/trail/us/utah/jennys-canyon-trail"},
        {"name": "Petrified Dunes Trail", "url": "https://www.alltrails.com/trail/us/utah/petrified-dunes-trail"},
        {"name": "Red Cliffs Trail", "url": "https://www.alltrails.com/trail/us/utah/red-cliffs-trail"},
    ]

    with patch.object(discoverer, "_get_alltrails_direct_batch_rows_for_destination", return_value=rows):
        out = discoverer._prioritize_direct_batch_trails(
            existing, "St. George, Utah", "October 17, 2026"
        )

    names = [item["name"] for item in out]
    assert "Jenny's Canyon Trail" in names
    # 1 day * 2 items/day = 2 slots; already have 1, so exactly 1 new trail added.
    assert len(names) == 2


def test_prioritize_direct_batch_trails_copies_description_into_injected_items() -> None:
    """Regression for a real bug (dipstick58, 2026-08-16): unlike
    _prioritize_direct_batch_attractions, this trail-injection path built new
    items with only name/type/url and silently dropped the harvested row's
    description/practical_note -- every trail injected this way rendered
    with a permanently empty teaser downstream (HTMLValidator's teaser-
    completeness check), even when the direct-batch HTML captured a real
    descriptive note for that trail."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._trail_direct_batch_items_per_day = 2
    discoverer._max_trail_miles = 3.0
    existing: list[dict] = []
    rows = [
        {
            "name": "Sun Mountain Trail",
            "url": "https://www.alltrails.com/trail/us/new-mexico/sun-mountain-trail",
            "rating": 4.6,
            "description": "Short steep ascent to panoramic mountain and valley views.",
        },
    ]

    with patch.object(discoverer, "_get_alltrails_direct_batch_rows_for_destination", return_value=rows):
        out = discoverer._prioritize_direct_batch_trails(existing, "Santa Fe", "October 27-29, 2026")

    assert len(out) == 1
    assert out[0]["name"] == "Sun Mountain Trail"
    assert out[0]["description"] == "Short steep ascent to panoramic mountain and valley views."


def test_prioritize_direct_batch_trails_prefers_highest_rated_and_respects_mileage_threshold() -> None:
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._trail_direct_batch_items_per_day = 1
    discoverer._max_trail_miles = 3.0
    existing: list[dict] = []
    rows = [
        {
            "name": "Long Ridge Trail",
            "url": "https://www.alltrails.com/trail/us/utah/long-ridge-trail",
            "rating": 4.9,
            "description": "8.5 mi hike",
        },
        {
            "name": "Short Loop Trail",
            "url": "https://www.alltrails.com/trail/us/utah/short-loop-trail",
            "rating": 4.4,
            "description": "1.2 mi hike",
        },
    ]

    with patch.object(discoverer, "_get_alltrails_direct_batch_rows_for_destination", return_value=rows):
        out = discoverer._prioritize_direct_batch_trails(
            existing, "Zion National Park", "October 18, 2026"
        )

    names = [item["name"] for item in out]
    # Long Ridge Trail is rated higher but exceeds the 3.0-mile threshold, so the
    # lower-rated but in-threshold trail must be the one selected.
    assert names == ["Short Loop Trail"]


def test_prioritize_direct_batch_en_route_stops_dedupes_same_url_and_prefers_specific_title() -> None:
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    rows = [
        {
            "name": "Best stops along the route",
            "url": "https://www.google.com/maps/search/?api=1&query=Lizard+Head+Pass+Telluride+CO",
            "detour_time_minutes": 10,
        },
        {
            "name": "Lizard Head Pass",
            "url": "https://www.google.com/maps/search/?api=1&query=Lizard+Head+Pass+Telluride+CO",
            "detour_time_minutes": 10,
        },
    ]

    with patch.object(discoverer, "_get_en_route_direct_batch_rows_for_destination", return_value=rows):
        out = discoverer._prioritize_direct_batch_en_route_stops(
            [],
            "Telluride",
            "October 18, 2026",
            "Moab",
        )

    assert len(out) == 1
    assert out[0]["name"] == "Lizard Head Pass"


def test_discover_en_route_stops_direct_batch_replaces_ai_list_even_when_nonempty() -> None:
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._en_route_source = "direct_link_batch"

    ai = {
        "getting_here": {
            "en_route_stops": [{"name": "Fuel Stop Plaza", "detour_time_minutes": 8}],
        }
    }
    rows = [
        {"name": "Wilson Arch", "url": "https://www.blm.gov/visit/wilson-arch", "detour_time_minutes": 10},
    ]

    with patch.object(discoverer, "_get_en_route_direct_batch_rows_for_destination", return_value=rows):
        with patch.object(discoverer, "_search_en_route_stop_from_direct_batch", return_value=rows[0]["url"]):
            with patch.object(discoverer, "_search_first") as fallback_search:
                discoverer._discover_en_route_stops(ai, "Moab")

    names = [str(item.get("name", "") or "") for item in ai["getting_here"]["en_route_stops"]]
    assert names == ["Wilson Arch"]
    fallback_search.assert_not_called()


def test_discover_en_route_stops_direct_batch_preserves_existing_url_without_rematch() -> None:
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._en_route_source = "direct_link_batch"

    ai = {
        "getting_here": {
            "en_route_stops": [
                {
                    "name": "Wilson Arch",
                    "url": "https://www.blm.gov/visit/wilson-arch",
                    "detour_time_minutes": 10,
                }
            ]
        }
    }

    with patch.object(discoverer, "_retain_discovered_url", side_effect=lambda url, *_a, **_k: url):
        with patch.object(discoverer, "_search_en_route_stop_from_direct_batch") as batch_search:
            with patch.object(discoverer, "_search_first") as fallback_search:
                discoverer._discover_en_route_stops(ai, "Moab")

    assert ai["getting_here"]["en_route_stops"][0]["url"] == "https://www.blm.gov/visit/wilson-arch"
    batch_search.assert_not_called()
    fallback_search.assert_not_called()


def test_discover_en_route_stops_preserves_existing_maps_url_instead_of_overwriting_with_fallback() -> None:
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._en_route_source = "direct_link_batch"

    ai = {
        "getting_here": {
            "en_route_stops": [
                {
                    "name": "Leeds Historic District",
                    "url": "https://www.blm.gov/visit/wilson-arch",
                    "maps_url": "https://www.google.com/maps/place/Leeds,+UT",
                    "detour_time_minutes": 10,
                }
            ]
        }
    }

    with patch.object(discoverer, "_retain_discovered_url", side_effect=lambda url, *_a, **_k: url):
        with patch.object(discoverer, "_search_en_route_stop_from_direct_batch") as batch_search:
            with patch.object(discoverer, "_search_first") as fallback_search:
                discoverer._discover_en_route_stops(ai, "St. George, Utah", origin_name="Las Vegas")

    stop = ai["getting_here"]["en_route_stops"][0]
    assert stop["maps_url"] == "https://www.google.com/maps/place/Leeds,+UT"
    batch_search.assert_not_called()
    fallback_search.assert_not_called()


def test_discover_en_route_stops_also_discovers_departure_route_option_urls() -> None:
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._en_route_source = "search"

    ai = {
        "getting_here": {"en_route_stops": []},
        "getting_there": {
            "route_options": [
                {"title": "Turquoise Trail Scenic Byway", "description": "Historic route option."}
            ]
        },
    }

    with patch.object(
        discoverer,
        "_search_first",
        return_value="https://www.newmexico.org/scenic-byways/turquoise-trail-national-scenic-byway/",
    ):
        discoverer._discover_en_route_stops(ai, "Santa Fe")

    opt = ai["getting_there"]["route_options"][0]
    assert "newmexico.org/scenic-byways" in str(opt.get("url", "") or "")


def test_scenic_drive_discovery_disallows_alltrails_results_upfront():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    captured = {}

    def fake_search(variants, **kwargs):
        captured.update(kwargs)
        return None

    dest = {
        "scenic_drives": [{"title": "Kolob Terrace Road"}],
    }

    with patch.object(discoverer, "_search_first", side_effect=fake_search):
        discoverer._discover_scenic_drives(dest, "St. George, Utah")

    assert captured.get("allow_alltrails") is False


def test_attraction_fallback_maps_avoids_contradictory_destination_append():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._attraction_source = "search"

    with patch.object(discoverer, "_search_first", return_value=None):
        ai = {
            "top_attractions": [
                {
                    "name": "Historic Downtown St. George",
                    "type": "attraction",
                    "description": "Historic district walk.",
                }
            ]
        }
        discoverer._discover_attractions(ai, "Zion National Park", "zion")

    url = ai["top_attractions"][0]["url"]
    assert "Historic%20Downtown%20St.%20George" in url
    assert "Zion%20National%20Park" not in url


def test_discover_attractions_skips_blacklisted_interest_keywords():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._uninterested_keywords = ("golf course",)
    discoverer._seasonal_ski_keywords = (" ski",)
    discoverer._ski_in_season_months = (11, 12, 1, 2, 3, 4)

    ai = {
        "top_attractions": [
            {
                "name": "Telluride Golf Course",
                "type": "attraction",
                "description": "Championship greens and club house.",
            }
        ]
    }

    with patch.object(discoverer, "_search_first", return_value="https://example.com/should-not-be-used"):
        discoverer._discover_attractions(ai, "Telluride, Colorado", None, "July 10-12, 2026")

    assert ai["top_attractions"][0]["url"] == ""


def test_discover_attractions_skips_bike_trail_interest_keywords():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._uninterested_keywords = ("bike trail",)
    discoverer._seasonal_ski_keywords = (" ski",)
    discoverer._ski_in_season_months = (11, 12, 1, 2, 3, 4)

    ai = {
        "top_attractions": [
            {
                "name": "Bear Claw Poppy Trail",
                "type": "attraction",
                "description": "A popular bike trail through the desert foothills.",
            }
        ]
    }

    with patch.object(discoverer, "_search_first", return_value="https://example.com/should-not-be-used"):
        discoverer._discover_attractions(ai, "St. George, Utah", None, "October 18-20, 2026")

    assert ai["top_attractions"][0]["url"] == ""


def test_discover_attractions_skips_ski_out_of_season():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._uninterested_keywords = ()
    discoverer._seasonal_ski_keywords = ("ski resort", "snowboarding")
    discoverer._ski_in_season_months = (11, 12, 1, 2, 3, 4)

    ai = {
        "top_attractions": [
            {
                "name": "Telluride Ski Resort",
                "type": "attraction",
                "description": "Alpine ski terrain.",
            }
        ]
    }

    with patch.object(discoverer, "_search_first", return_value="https://example.com/should-not-be-used"):
        discoverer._discover_attractions(ai, "Telluride, Colorado", None, "July 10-12, 2026")

    assert ai["top_attractions"][0]["url"] == ""


def test_discover_attractions_omits_maps_fallback_for_ambiguous_geographic_feature_name():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)

    ai = {
        "top_attractions": [
            {
                "name": "Dolores River Canyon",
                "type": "attraction",
                "description": "Canyon and river landscape views.",
            }
        ]
    }

    with patch.object(discoverer, "_search_first", return_value=None):
        discoverer._discover_attractions(ai, "Telluride, Colorado", None, "July 10-12, 2026")

    assert ai["top_attractions"][0]["url"] == ""


def test_discover_attractions_assigns_maps_fallback_for_red_cliffs_desert_reserve():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._attraction_source = "search"

    ai = {
        "top_attractions": [
            {
                "name": "Red Cliffs Desert Reserve",
                "type": "attraction",
                "description": "Desert conservation landscape near St. George.",
            }
        ]
    }

    with patch.object(discoverer, "_search_first", return_value=None):
        discoverer._discover_attractions(ai, "Zion National Park", "zion")

    attraction = ai["top_attractions"][0]
    assert attraction["url"].startswith("https://www.google.com/maps/search/")
    assert "maps_url" in attraction


def test_discover_attractions_omits_maps_fallback_when_policy_enforce_blocks_maps_search() -> None:
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._url_policy_mode = "enforce"
    discoverer._url_policy_blocked_classes = {"google_maps_search"}

    ai = {
        "top_attractions": [
            {
                "name": "Red Cliffs Desert Reserve",
                "type": "attraction",
                "description": "Desert conservation landscape near St. George.",
            }
        ]
    }

    with patch.object(discoverer, "_search_first", return_value=None):
        discoverer._discover_attractions(ai, "Zion National Park", "zion")

    attraction = ai["top_attractions"][0]
    assert str(attraction.get("url", "") or "") == ""
    assert "maps_url" not in attraction


def test_discover_attractions_allows_ski_in_season():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._attraction_source = "search"
    discoverer._uninterested_keywords = ()
    discoverer._seasonal_ski_keywords = ("ski resort",)
    discoverer._ski_in_season_months = (11, 12, 1, 2, 3, 4)

    ai = {
        "top_attractions": [
            {
                "name": "Telluride Ski Resort",
                "type": "attraction",
                "description": "Alpine ski terrain.",
            }
        ]
    }

    with patch.object(discoverer, "_search_first", return_value="https://www.tellurideskiresort.com"):
        discoverer._discover_attractions(ai, "Telluride, Colorado", None, "December 10-12, 2026")

    assert ai["top_attractions"][0]["url"] == "https://www.tellurideskiresort.com"


def test_discover_scenic_drives_leaves_url_empty_when_no_match():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    dest = {
        "scenic_drives": [
            {
                "title": "Scenic Byway 12",
                "category": "drive",
            }
        ]
    }

    with patch.object(discoverer, "_search_first", return_value=None):
        discoverer._discover_scenic_drives(dest, "Bryce Canyon National Park")

    assert dest["scenic_drives"][0]["url"] == ""


def test_audit_retains_verified_scenic_drive_url():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._url_validator = MagicMock()
    discoverer._url_validator.session.get.return_value = MagicMock(
        status_code=200,
        text="Scenic Byway 12 details near Bryce Canyon route viewpoints",
    )

    trip = {
        "destinations": [
            {
                "name": "Bryce Canyon National Park",
                "ai_content": {
                    "top_attractions": [],
                    "getting_here": {"en_route_stops": []},
                    "dinner_recommendations": [],
                },
                "scenic_drives": [
                    {
                        "title": "Scenic Byway 12",
                        "url": "https://www.visitutah.com/places-to-go/scenic-drives/scenic-byway-12",
                    }
                ],
                "cultural_events": {"has_events": False, "events": []},
            }
        ]
    }

    discoverer.audit_discovered_urls(trip)

    assert trip["destinations"][0]["scenic_drives"][0]["url"].startswith("https://www.visitutah.com/")


def test_audit_preserves_restaurant_homepage_url_for_specific_site():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._url_validator = MagicMock()
    discoverer._url_validator.verify_url.return_value = (True, 200)
    discoverer._url_validator.session.get.return_value = MagicMock(
        status_code=200,
        text="Bit & Spur Restaurant & Saloon official site.",
    )

    trip = {
        "destinations": [
            {
                "name": "Zion National Park",
                "ai_content": {
                    "top_attractions": [],
                    "getting_here": {"en_route_stops": []},
                    "dinner_recommendations": [
                        {
                            "name": "Bit & Spur Restaurant & Saloon",
                            "url": "https://www.bitandspur.com",
                        }
                    ],
                },
                "scenic_drives": [],
                "cultural_events": {"has_events": False, "events": []},
            }
        ]
    }

    discoverer.audit_discovered_urls(trip)

    assert trip["destinations"][0]["ai_content"]["dinner_recommendations"][0]["url"] == "https://www.bitandspur.com"


def test_audit_preserves_en_route_homepage_url_for_specific_site():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._url_validator = MagicMock()
    discoverer._url_validator.verify_url.return_value = (True, 200)
    discoverer._url_validator.session.get.return_value = MagicMock(
        status_code=200,
        text="Hurricane Heritage Center official visitor site.",
    )

    trip = {
        "destinations": [
            {
                "name": "Zion National Park",
                "ai_content": {
                    "top_attractions": [],
                    "getting_here": {
                        "en_route_stops": [
                            {
                                "name": "Hurricane Heritage Center",
                                "url": "https://www.hurricaneheritagecenter.org",
                            }
                        ]
                    },
                    "dinner_recommendations": [],
                },
                "scenic_drives": [],
                "cultural_events": {"has_events": False, "events": []},
            }
        ]
    }

    discoverer.audit_discovered_urls(trip)

    assert trip["destinations"][0]["ai_content"]["getting_here"]["en_route_stops"][0]["url"] == "https://www.hurricaneheritagecenter.org"


def test_audit_rejects_scenic_drive_place_page_url_without_route_intent():
    """PR-004: scenic-drive URLs should be route-specific, not generic place pages."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._url_validator = MagicMock()
    discoverer._url_validator.session.get.return_value = MagicMock(
        status_code=200,
        text="Snow Canyon State Park official visitor information.",
    )

    trip = {
        "destinations": [
            {
                "name": "St. George, Utah",
                "ai_content": {
                    "top_attractions": [],
                    "getting_here": {"en_route_stops": []},
                    "dinner_recommendations": [],
                },
                "scenic_drives": [
                    {
                        "title": "Snow Canyon Scenic Drive",
                        "url": "https://stateparks.utah.gov/parks/snow-canyon/",
                    }
                ],
                "cultural_events": {"has_events": False, "events": []},
            }
        ]
    }

    discoverer.audit_discovered_urls(trip)

    assert trip["destinations"][0]["scenic_drives"][0].get("url", "") == ""


def test_audit_strips_non_alltrails_url_for_trail_like_attraction_but_assigns_maps_fallback():
    """This audit pass still strips a non-AllTrails, non-maps URL from a
    trail-like attraction (it can't tell a specifically-matched authoritative
    recovery -- e.g. real dipstick64 "Bryce Point", a misclassified viewpoint
    whose correct nps.gov page was recovered via the attraction direct-batch
    fallback -- from an arbitrary low-confidence web hit). Since the audit-pass
    no-link-fallback fix, when there's no pre-existing maps_url to fall back on
    either, it still internally assigns the same safe Google-Maps-search
    fallback every other "no URL found" attraction gets (asserted below via
    the registry decision) -- but under the verified-link-or-seed policy
    (2026-08-17) a maps-search fallback is explicitly NOT "verified", so this
    non-seed attraction is then removed from top_attractions entirely rather
    than rendered with the fallback as its link.
    """
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._url_validator = MagicMock()
    discoverer._url_validator.verify_url.return_value = (True, 200)
    discoverer._url_validator.session.get.return_value = MagicMock(
        status_code=200,
        text="Generic scenic drive page",
    )

    trip = {
        "destinations": [
            {
                "name": "Bryce Canyon National Park",
                "ai_content": {
                    "top_attractions": [
                        {
                            "name": "Grand Wash Trail",
                            "type": "hike",
                            "description": "Canyon walk.",
                            "url": "https://www.nps.gov/care/planyourvisit/scenicdrive.htm",
                        }
                    ],
                    "getting_here": {"en_route_stops": []},
                    "dinner_recommendations": [],
                },
                "scenic_drives": [],
                "cultural_events": {"has_events": False, "events": []},
            }
        ]
    }

    # Keep a direct reference to the original dict -- it's mutated in place
    # with the internally-assigned maps fallback even though the policy
    # prunes it out of top_attractions afterward.
    attraction = trip["destinations"][0]["ai_content"]["top_attractions"][0]

    discoverer.audit_discovered_urls(trip)

    assert attraction.get("url") == (
        "https://www.google.com/maps/search/?api=1&query=Grand%20Wash%20Trail%20Bryce%20Canyon%20National%20Park"
    )
    assert attraction.get("maps_url") == attraction.get("url")
    assert attraction["_registry"]["validation_status"] == "accepted"
    assert attraction["_registry"]["rendered_url"] == attraction["url"]
    assert "url_rejected" not in attraction["_registry"].get("rejection_reasons", [])
    # Policy (2026-08-17): a maps-search fallback is not "verified" -- this
    # non-seed attraction is removed from top_attractions entirely despite
    # the fallback having been internally assigned.
    assert trip["destinations"][0]["ai_content"]["top_attractions"] == []


def test_audit_real_bryce_point_misclassified_viewpoint_keeps_its_nps_page() -> None:
    """Regression using the real affected name from the SW2026-dipstick64 run:
    Bryce Canyon's "Bryce Point" is a viewpoint that _is_trail_like_attraction
    misclassifies as trail-like purely because its description mentions "a
    short walk". _discover_attractions correctly recovered its real nps.gov
    page via the attraction direct-batch fallback (reason=trail_like_
    misclassified_attraction_batch_recovered), but this later audit_discovered_
    urls safety pass re-derives trail_like the same way, sees a non-AllTrails
    URL, and strips it, internally assigning the same safe maps-search
    fallback every other "no URL found" attraction gets instead of leaving no
    link at all.

    That used to end with the item removed: the maps-search fallback is not
    "verified" under the verified-link-or-seed policy (2026-08-17), so a real
    nps.gov page existed and the card was dropped anyway. This test asserted
    that outcome and called it a deliberate trade-off.

    Owner rule (2026-08-29) changes it: when a trail-type target has both
    available, prefer AllTrails if the title contains "trail", otherwise
    prefer NPS. "Bryce Point" does not claim to be a trail, so its official
    page now survives the audit. AllTrails-first still governs anything that
    does -- it exists because non-AllTrails trail URLs were generated badly.

    Same defect, same shape, found again on 2026-08-29 as Balanced Rock and
    Upheaval Dome: a formation and a crater, both entity_class=trail, both
    stripped of correct nps.gov pages.
    """
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._url_validator = MagicMock()
    discoverer._url_validator.verify_url.return_value = (True, 200)
    discoverer._url_validator.session.get.return_value = MagicMock(
        status_code=200,
        text="Bryce Point overlook information",
    )

    trip = {
        "destinations": [
            {
                "name": "Bryce Canyon National Park",
                "ai_content": {
                    "top_attractions": [
                        {
                            "name": "Bryce Point",
                            "type": "viewpoint",
                            "description": (
                                "Known for its panoramic views of the canyon, Bryce "
                                "Point is accessible via a short drive and a brief walk."
                            ),
                            "url": "https://www.nps.gov/brca/planyourvisit/brycepoint.htm",
                        }
                    ],
                    "getting_here": {"en_route_stops": []},
                    "dinner_recommendations": [],
                },
                "scenic_drives": [],
                "cultural_events": {"has_events": False, "events": []},
            }
        ]
    }

    attraction = trip["destinations"][0]["ai_content"]["top_attractions"][0]

    discoverer.audit_discovered_urls(trip)

    # "Bryce Point" already shares a token ("Bryce") with the destination name,
    # so _maps_fallback_query_text uses the item name alone rather than
    # appending the destination again.
    assert attraction.get("url") == "https://www.nps.gov/brca/planyourvisit/brycepoint.htm"
    assert not attraction.get("url", "").startswith("https://www.google.com/maps/search/")
    # It keeps a real verified URL now, so verified-link-or-seed keeps the
    # card. Previously this asserted [] -- the item was dropped despite its
    # official page having been found, because the audit had already replaced
    # that page with a maps-search fallback that cannot satisfy the policy.
    kept = trip["destinations"][0]["ai_content"]["top_attractions"]
    assert [a["name"] for a in kept] == ["Bryce Point"]


def test_semantic_scoring_prefers_cultural_domain_over_preserve_domain():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._search = MagicMock()
    discoverer._url_validator = MagicMock()
    discoverer._url_validator.verify_url.return_value = (True, 200)
    discoverer._url_validator.session.get.return_value = MagicMock(
        status_code=200,
        text="Canyon Road district arts galleries and culture in Santa Fe",
    )
    discoverer._search.search.return_value = [
        {
            "url": "https://example-canyon-preserve.org/visit",
            "name": "Canyon Preserve",
            "snippet": "Nature preserve and wildlife habitat",
        },
        {
            "url": "https://santafe.org/visit/arts/canyon-road",
            "name": "Canyon Road Arts District | Visit Santa Fe",
            "snippet": "Explore galleries and culture",
        },
    ]

    result = discoverer._search_first_strict(
        query_variants=['"Canyon Road" Santa Fe attraction'],
        site_filter=None,
        site_hint=None,
        item_name="Canyon Road",
        dest_name="Santa Fe",
        allow_alltrails=False,
    )

    assert result == "https://santafe.org/visit/arts/canyon-road"


def test_candidate_scoring_applies_path_and_domain_hints():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    item = {
        "url": "https://visit-santafe.example/visit/culture/gallery/canyon-road",
        "name": "Visit Santa Fe - Canyon Road Arts",
        "snippet": "Culture and galleries district",
    }
    score = discoverer._score_candidate_result(
        item,
        item_name="Canyon Road",
        dest_name="Santa Fe",
        specific=True,
    )
    bad_item = {
        "url": "https://canyon-preserve.example/wildlife",
        "name": "Canyon Preserve",
        "snippet": "nature preserve and conservation",
    }
    bad_score = discoverer._score_candidate_result(
        bad_item,
        item_name="Canyon Road",
        dest_name="Santa Fe",
        specific=True,
    )
    assert score > bad_score


def test_candidate_scoring_prefers_destination_country_tld_for_international_destinations():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    local_item = {
        "url": "https://visitdubrovnik.hr/visit/old-town",
        "name": "Visit Dubrovnik",
        "snippet": "Explore the old town district",
    }
    foreign_item = {
        "url": "https://visitdubrovnik.com/visit/old-town",
        "name": "Visit Dubrovnik",
        "snippet": "Explore the old town district",
    }

    local_score = discoverer._score_candidate_result(
        local_item,
        item_name="Old Town Dubrovnik",
        dest_name="Dubrovnik, Croatia",
        specific=True,
    )
    foreign_score = discoverer._score_candidate_result(
        foreign_item,
        item_name="Old Town Dubrovnik",
        dest_name="Dubrovnik, Croatia",
        specific=True,
    )

    assert local_score > foreign_score


def test_restaurant_rating_priority_requires_sufficient_votes():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._restaurant_rating_min = 4.4
    discoverer._restaurant_rating_min_votes = 100
    discoverer._restaurant_rating_boost = 10

    low_votes_item = {
        "url": "https://www.google.com/maps/place/Test+Cafe/",
        "name": "Test Cafe",
        "snippet": "Rated 4.9 stars with 18 reviews",
    }
    enough_votes_item = {
        "url": "https://www.google.com/maps/place/Test+Cafe/",
        "name": "Test Cafe",
        "snippet": "Rated 4.6 stars with 320 reviews",
    }

    low_votes_score = discoverer._score_candidate_result(
        low_votes_item,
        item_name="Test Cafe",
        dest_name="Moab",
        specific=True,
        site_filter="google.com/maps",
    )
    enough_votes_score = discoverer._score_candidate_result(
        enough_votes_item,
        item_name="Test Cafe",
        dest_name="Moab",
        specific=True,
        site_filter="google.com/maps",
    )

    assert enough_votes_score > low_votes_score


def test_alltrails_rating_priority_requires_sufficient_votes():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._alltrails_rating_min = 4.5
    discoverer._alltrails_rating_min_votes = 200
    discoverer._alltrails_rating_boost = 12

    low_votes_item = {
        "url": "https://www.alltrails.com/trail/us/utah/canyon-overlook-trail",
        "name": "Canyon Overlook Trail",
        "snippet": "4.9 stars 26 reviews",
    }
    enough_votes_item = {
        "url": "https://www.alltrails.com/trail/us/utah/canyon-overlook-trail",
        "name": "Canyon Overlook Trail",
        "snippet": "4.7 stars 1,420 reviews",
    }

    low_votes_score = discoverer._score_candidate_result(
        low_votes_item,
        item_name="Canyon Overlook Trail",
        dest_name="Zion National Park",
        specific=True,
        site_filter="alltrails.com",
    )
    enough_votes_score = discoverer._score_candidate_result(
        enough_votes_item,
        item_name="Canyon Overlook Trail",
        dest_name="Zion National Park",
        specific=True,
        site_filter="alltrails.com",
    )

    assert enough_votes_score > low_votes_score


def test_place_interest_threshold_passes_when_rating_and_votes_meet_minimums():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._place_interest_min_rating = 4.0
    discoverer._place_interest_min_votes = 10
    discoverer._place_interest_require_metadata = True

    candidate = {
        "name": "Snow Canyon State Park",
        "snippet": "4.7 stars with 632 reviews",
        "url": "https://stateparks.utah.gov/parks/snow-canyon/",
    }

    assert discoverer._meets_place_interest_threshold(candidate, site_filter=None)


def test_place_interest_threshold_fails_when_votes_below_minimum():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._place_interest_min_rating = 4.0
    discoverer._place_interest_min_votes = 10
    discoverer._place_interest_require_metadata = True

    candidate = {
        "name": "Small Viewpoint",
        "snippet": "4.8 stars with 6 reviews",
        "url": "https://example.com/viewpoint",
    }

    assert not discoverer._meets_place_interest_threshold(candidate, site_filter=None)


def test_place_interest_threshold_allows_missing_metadata_when_not_required():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._place_interest_min_rating = 4.0
    discoverer._place_interest_min_votes = 10
    discoverer._place_interest_require_metadata = False

    candidate = {
        "name": "Historic Site",
        "snippet": "Official tourism overview page",
        "url": "https://example.com/historic-site",
    }

    assert discoverer._meets_place_interest_threshold(candidate, site_filter=None)


def test_place_interest_threshold_skips_alltrails_site_filter():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._place_interest_min_rating = 5.0
    discoverer._place_interest_min_votes = 10000
    discoverer._place_interest_require_metadata = True

    candidate = {
        "name": "Any Trail",
        "snippet": "No rating text present",
        "url": "https://www.alltrails.com/trail/us/utah/any-trail",
    }

    assert discoverer._meets_place_interest_threshold(candidate, site_filter="alltrails.com")


def test_audit_discovered_urls_strips_weak_hallucinated_links():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._url_validator = MagicMock()
    discoverer._url_validator.verify_url.return_value = (True, 200)
    discoverer._url_validator.session.get.return_value = MagicMock(
        status_code=200,
        text="Dixie trail in St. George, Utah.",
    )

    trip = {
        "destinations": [
            {
                "name": "St. George, Utah",
                "ai_content": {
                    "top_attractions": [
                        {
                            "name": "Dixie State University Trail",
                            "type": "hike",
                            "url": "https://www.dixie.edu/trails/dixie-trail",
                        }
                    ],
                    "getting_here": {"en_route_stops": []},
                    "dinner_recommendations": [],
                },
                "scenic_drives": [
                    {
                        "title": "Kolob Canyons Drive",
                        "url": "https://example.com/should-be-removed",
                    }
                ],
                "cultural_events": {
                    "has_events": True,
                    "events": [
                        {
                            "name": "Bad Event",
                            "url": "https://example.com/bad-event",
                        }
                    ],
                },
            }
        ]
    }

    attraction = trip["destinations"][0]["ai_content"]["top_attractions"][0]

    discoverer.audit_discovered_urls(trip)

    # The weak/hallucinated dixie.edu URL is still stripped (unchanged). Since
    # the audit-pass no-link-fallback fix, the trail-like attraction then gets
    # the same safe maps-search fallback every other unresolved attraction
    # gets, instead of no link at all -- scenic drives and cultural events are
    # untouched by that fix and still end up with no url.
    assert attraction.get("url") == (
        "https://www.google.com/maps/search/?api=1&query=Dixie%20State%20University%20Trail%20St.%20George%2C%20Utah"
    )
    assert attraction.get("url") != "https://www.dixie.edu/trails/dixie-trail"
    # Policy (2026-08-17): a maps-search fallback is not "verified" -- this
    # non-seed attraction is removed from top_attractions entirely despite
    # the fallback having been internally assigned.
    assert trip["destinations"][0]["ai_content"]["top_attractions"] == []
    assert "url" not in trip["destinations"][0]["scenic_drives"][0]
    assert "url" not in trip["destinations"][0]["cultural_events"]["events"][0]


def test_audit_discovered_urls_preserves_event_maps_fallback_as_maps_url():
    """Real bug (St. George eval run, dipstick73/74): "I-15 Country Rock
    Music Festival" and "Odyssey Dance Theatre's Thriller 2026" rendered with
    NO link at all. cultural_events.py's _verify_event_urls deliberately
    assigns a Google-Maps-search fallback into the event's "url" field when
    no real URL survives (mirroring every other content type's maps
    fallback). But this audit pass re-validates that same url through the
    strict retention gate, and config.yaml's url_policy_mode: "enforce"
    blocks the "google_maps_search" policy class unless the caller opts in
    via allow_google_maps_search -- which this call site never did, so the
    fallback got silently stripped with nothing left to render. Fix: extract
    a pre-existing google_maps_search/dir url into a separate maps_url field
    before the retain call, exactly like restaurants/attractions/en-route
    stops already do, so it survives even when the primary url is rejected."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._url_validator = MagicMock()
    discoverer._url_validator.verify_url.return_value = (True, 200)
    discoverer._url_policy_mode = "enforce"

    fallback_url = (
        "https://www.google.com/maps/search/?api=1&query="
        "I-15+Country+Rock+Music+Festival+Mesquite+Regional+Sports+and+Event+Complex"
    )
    trip = {
        "destinations": [
            {
                "name": "St. George, Utah",
                "ai_content": {
                    "top_attractions": [],
                    "getting_here": {"en_route_stops": []},
                    "dinner_recommendations": [],
                },
                "scenic_drives": [],
                "cultural_events": {
                    "has_events": True,
                    "events": [
                        {
                            "name": "I-15 Country Rock Music Festival",
                            "url": fallback_url,
                        }
                    ],
                },
            }
        ]
    }

    discoverer.audit_discovered_urls(trip)

    event = trip["destinations"][0]["cultural_events"]["events"][0]
    # The policy-blocked google_maps_search class still can't survive as the
    # primary "url" in enforce mode ...
    assert "url" not in event
    # ... but it must survive as maps_url so html_assembler._build_events can
    # still render a real, clickable link instead of plain unlinked text.
    assert event.get("maps_url") == fallback_url


def test_update_route_distance_skips_live_fetch_when_disabled():
    """Route distance already has a solid Haversine fallback that costs zero
    network calls -- the live Google Maps directions HTML scrape is a pure
    accuracy enhancement on top of it, not a correctness gate. When disabled,
    it must not be attempted at all."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._route_distance_live_fetch_enabled = False
    discoverer._parse_route_info_from_maps_html = MagicMock(
        side_effect=AssertionError("must not live-fetch route info when disabled")
    )

    ai: dict = {}
    getting_here: dict = {}
    discoverer._update_route_distance_and_time(
        ai=ai,
        getting_here=getting_here,
        origin_name="Zion National Park",
        dest_name="Bryce Canyon National Park",
        origin_lat=37.2982,
        origin_lng=-113.0263,
        dest_lat=37.5930,
        dest_lng=-112.1871,
    )

    discoverer._parse_route_info_from_maps_html.assert_not_called()
    assert getting_here.get("distance_miles")
    assert getting_here.get("travel_time")


def test_update_route_distance_uses_live_fetch_when_enabled():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._route_distance_live_fetch_enabled = True
    discoverer._parse_route_info_from_maps_html = MagicMock(return_value=(84.0, "1 hr 45 min"))

    ai: dict = {}
    getting_here: dict = {}
    discoverer._update_route_distance_and_time(
        ai=ai,
        getting_here=getting_here,
        origin_name="Zion National Park",
        dest_name="Bryce Canyon National Park",
        origin_lat=37.2982,
        origin_lng=-113.0263,
        dest_lat=37.5930,
        dest_lng=-112.1871,
    )

    discoverer._parse_route_info_from_maps_html.assert_called_once()
    assert getting_here.get("distance_miles") == "84"
    assert getting_here.get("travel_time") == "1 hr 45 min"


# --- dipstick55 Theme B/C regression: remembered-authoritative-URL cache must
# be scoped per item, not just per URL. Before this fix, _remember_direct_batch_
# authoritative_url stored a flat set() of URL strings; once a URL was validated
# for ANY item during a run, _is_remembered_direct_batch_authoritative_url (and
# the bypass in _retain_discovered_url that consults it) would vouch for that
# same URL string being reused for a completely different, unrelated item --
# e.g. a row-parsing hiccup attaching Arches' "double-arch-trail" AllTrails URL
# to both "Double Arch" and "Delicate Arch". These tests cover the four
# concretely reported mismatches plus the underlying cache API.


def test_remembered_authoritative_url_scoped_to_the_item_it_was_validated_for():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    double_arch_url = "https://www.alltrails.com/trail/us/utah/double-arch-trail"
    discoverer._remember_direct_batch_authoritative_url(double_arch_url, "Double Arch")

    assert discoverer._is_remembered_direct_batch_authoritative_url(double_arch_url, "Double Arch") is True
    assert discoverer._is_remembered_direct_batch_authoritative_url(double_arch_url, "Delicate Arch") is False


def test_remembered_authoritative_url_scoping_covers_all_four_reported_mismatches():
    """Reproduces the exact four wrong-attribution cases from the dipstick55
    triage doc's Theme B: a URL remembered for one trail must not also
    validate for the differently-named trail it got wrongly attached to."""
    cases = [
        ("https://www.alltrails.com/trail/us/utah/double-arch-trail", "Double Arch", "Delicate Arch"),
        ("https://www.alltrails.com/trail/us/utah/mesa-arch", "Mesa Arch", "Landscape Arch"),
        (
            "https://www.alltrails.com/trail/us/colorado/cornet-creek-falls-hike",
            "Cornet Creek Falls",
            "Bridal Veil Falls",
        ),
        (
            "https://www.alltrails.com/trail/us/utah/canyon-overlook-trail",
            "Canyon Overlook Trail",
            "The Narrows, Emerald Pools trail",
        ),
    ]
    for url, remembered_for, wrong_item in cases:
        discoverer = URLDiscoverer.__new__(URLDiscoverer)
        discoverer._remember_direct_batch_authoritative_url(url, remembered_for)
        assert discoverer._is_remembered_direct_batch_authoritative_url(url, remembered_for) is True
        assert discoverer._is_remembered_direct_batch_authoritative_url(url, wrong_item) is False, (
            f"{url} remembered for {remembered_for!r} must not validate for {wrong_item!r}"
        )


def test_remembered_authoritative_url_with_no_item_name_checks_any_item():
    """The item-agnostic overload (item_name=None) is used by the bulk
    prewarm-cache eligibility check (_is_high_confidence_provenance_url),
    which only needs to know a URL was vetted for *some* item this run, not
    attribute it to one. This must keep working after the per-item scoping."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    url = "https://www.alltrails.com/trail/us/utah/double-arch-trail"
    discoverer._remember_direct_batch_authoritative_url(url, "Double Arch")

    assert discoverer._is_remembered_direct_batch_authoritative_url(url) is True
    assert discoverer._is_remembered_direct_batch_authoritative_url("https://example.com/nope") is False


def test_retain_discovered_url_rejects_wrong_item_even_when_url_remembered_for_different_item():
    """End-to-end through _retain_discovered_url: the 'remembered authoritative
    direct-batch URL' bypass must not short-circuit normal rejection when the
    URL was remembered for a different item than the one currently being
    checked (the actual Theme B bug path)."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._direct_batch_authoritative = True
    double_arch_url = "https://www.alltrails.com/trail/us/utah/double-arch-trail"
    discoverer._remember_direct_batch_authoritative_url(double_arch_url, "Double Arch")

    with patch.object(discoverer, "_meets_alltrails_publish_confidence", return_value=False):
        out = discoverer._retain_discovered_url(
            double_arch_url,
            "Delicate Arch",
            "Arches National Park",
            allow_alltrails=True,
            kind="attraction",
        )

    assert out == ""


def test_retain_discovered_url_keeps_remembered_url_for_the_same_item():
    """Sanity check that the legitimate fast-path is preserved: a URL
    remembered as authoritative for the SAME item still bypasses the
    (expensive) confidence re-check."""
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._direct_batch_authoritative = True
    double_arch_url = "https://www.alltrails.com/trail/us/utah/double-arch-trail"
    discoverer._remember_direct_batch_authoritative_url(double_arch_url, "Double Arch")

    with patch.object(discoverer, "_meets_alltrails_publish_confidence", return_value=False):
        out = discoverer._retain_discovered_url(
            double_arch_url,
            "Double Arch",
            "Arches National Park",
            allow_alltrails=True,
            kind="attraction",
        )

    assert out == double_arch_url


def test_persistent_cache_preserves_entry_age_across_resave(tmp_path) -> None:
    """Re-saving must write each entry's ORIGINAL birth timestamp, not "now".

    Regression: every save stamped ts=now, so an entry's age reset on each
    build. With builds happening regularly no TTL here could ever expire
    anything -- a dead URL stayed served from cache indefinitely, and the
    `now if now >= cutoff else cutoff` guards were dead code (cutoff is
    now - ttl, so the condition is always true)."""
    cache_path = tmp_path / "persistent_cache.json"
    old_ts = time.time() - (100 * 3600)
    cache_path.write_text(
        json.dumps(
            {
                "version": 1,
                "updated_at": old_ts,
                "search_results": {"q1": {"ts": old_ts, "results": [{"url": "https://a"}]}},
            }
        ),
        encoding="utf-8",
    )

    disc = URLDiscoverer.__new__(URLDiscoverer)
    disc._persistent_cache_enabled = True
    disc._persistent_cache_path = str(cache_path)
    disc._search_results_cache = {}
    disc._load_persistent_caches()
    disc._persistent_cache_dirty = True
    disc._save_persistent_caches()

    resaved = json.loads(cache_path.read_text(encoding="utf-8"))
    assert resaved["search_results"]["q1"]["ts"] == pytest.approx(old_ts, abs=1.0)


def test_persistent_cache_stamps_now_for_entries_first_seen_this_run(tmp_path) -> None:
    """The age-preserving fix must not freeze new entries at someone else's
    timestamp -- anything not loaded from disk is genuinely new."""
    cache_path = tmp_path / "persistent_cache.json"

    disc = URLDiscoverer.__new__(URLDiscoverer)
    disc._persistent_cache_enabled = True
    disc._persistent_cache_path = str(cache_path)
    disc._search_results_cache = {"fresh": [{"url": "https://b"}]}
    disc._persistent_cache_dirty = True
    disc._save_persistent_caches()

    saved = json.loads(cache_path.read_text(encoding="utf-8"))
    assert saved["search_results"]["fresh"]["ts"] == pytest.approx(time.time(), abs=5.0)


def test_expired_entry_is_dropped_on_the_load_after_a_resave(tmp_path) -> None:
    """End-to-end consequence of preserving age: an entry past its TTL must
    still expire even after intervening saves have rewritten the file."""
    cache_path = tmp_path / "persistent_cache.json"
    old_ts = time.time() - (200 * 3600)  # past the 168h search TTL
    cache_path.write_text(
        json.dumps(
            {
                "version": 1,
                "updated_at": old_ts,
                "search_results": {"q1": {"ts": old_ts, "results": [{"url": "https://a"}]}},
            }
        ),
        encoding="utf-8",
    )

    # A run that loads (dropping the stale entry) and then rewrites the file.
    first = URLDiscoverer.__new__(URLDiscoverer)
    first._persistent_cache_enabled = True
    first._persistent_cache_path = str(cache_path)
    first._search_results_cache = {}
    first._load_persistent_caches()
    assert "q1" not in first._search_results_cache
    first._search_results_cache["q2"] = [{"url": "https://b"}]
    first._persistent_cache_dirty = True
    first._save_persistent_caches()

    second = URLDiscoverer.__new__(URLDiscoverer)
    second._persistent_cache_enabled = True
    second._persistent_cache_path = str(cache_path)
    second._search_results_cache = {}
    second._load_persistent_caches()

    assert "q1" not in second._search_results_cache
    assert "q2" in second._search_results_cache


def test_conftest_isolates_default_cache_path_from_the_repo(tmp_path) -> None:
    """The autouse fixture in tests/conftest.py must keep the default cache
    path out of the repo working tree.

    Regression: the default path is relative, so a pytest run started at the
    repo root shared one file with real generator runs. Any test that built a
    real URLDiscoverer and marked the cache dirty rewrote that file from its
    own near-empty state, discarding the search results and harvest rows the
    last real build had paid xAI for."""
    from pathlib import Path as _Path

    from generator import url_discovery as mod

    default_path = _Path(mod.DEFAULT_PERSISTENT_CACHE_PATH)

    assert default_path.is_absolute()
    assert _Path.cwd() not in default_path.parents


def test_harvest_cache_ttl_is_one_week() -> None:
    """Harvest rows are the highest-volume cached bucket; a 24h TTL only ever
    helped same-day rebuilds and re-paid the search fee every morning."""
    from generator import url_discovery as mod

    assert mod.DEFAULT_PERSISTENT_HARVEST_CACHE_TTL_HOURS == 168
    assert (
        mod.DEFAULT_PERSISTENT_HARVEST_CACHE_TTL_HOURS
        == mod.DEFAULT_PERSISTENT_SEARCH_CACHE_TTL_HOURS
    ), "harvest and search answer the same class of question and should age alike"


def test_liveness_ttls_stay_shorter_than_content_ttls() -> None:
    """The safety argument for week-long content TTLs.

    Caching "which candidates does this query yield, and where do they live"
    for a week is safe ONLY because whether a page is still live, and whether a
    place has closed, are re-checked on much shorter independent TTLs. Raising
    those would genuinely let a closure or dead link go unnoticed for longer.
    This test fails if someone raises a liveness TTL to match a content one.
    """
    from generator import url_discovery as mod

    content_ttls = {
        "search": mod.DEFAULT_PERSISTENT_SEARCH_CACHE_TTL_HOURS,
        "harvest": mod.DEFAULT_PERSISTENT_HARVEST_CACHE_TTL_HOURS,
    }
    liveness_ttls = {
        "verify": mod.DEFAULT_PERSISTENT_VERIFY_CACHE_TTL_HOURS,
        "page_text": mod.DEFAULT_PERSISTENT_PAGE_TEXT_CACHE_TTL_HOURS,
        "alltrails": mod.DEFAULT_PERSISTENT_ALLTRAILS_CACHE_TTL_HOURS,
    }

    for live_name, live_ttl in liveness_ttls.items():
        for content_name, content_ttl in content_ttls.items():
            assert live_ttl < content_ttl, (
                f"{live_name} ({live_ttl}h) is a liveness/closure check and must expire "
                f"sooner than {content_name} content ({content_ttl}h)"
            )


class TestCachedAllTrailsBatchUrlForItem:
    """Cross-category reuse of an AllTrails URL a different batch already bought.

    Each category reads its own direct batch, so an item classified into one
    category never sees a URL another category harvested for it. Measured on
    the 2026-08-21 cold-start run: 11 items appeared in both the trail batch
    and another category's batch, and 4 published the weaker link -- two of
    them the bare park homepage `nps.gov/brca/`.
    """

    @staticmethod
    def _discoverer():
        mock_llm = type("MockLLM", (), {"provider": "grok", "model": "grok-4.5", "usage_tracker": None})()
        with patch("generator.search_provider.GrokSearch"), patch("generator.search_provider.ClaudeSearch"):
            return URLDiscoverer(config_path="config.yaml", llm_client=mock_llm)

    def _seed_trail_cache(self, disc, dest, dates, rows):
        disc._alltrails_direct_batch_cache = {
            disc._batch_cache_key(dest, f"{dates}|html|trail"): rows
        }

    def test_returns_url_harvested_by_the_trail_batch(self):
        disc = self._discoverer()
        self._seed_trail_cache(disc, "Zion National Park", "October 18, 2026", [
            {"name": "Canyon Overlook Trail",
             "url": "https://www.alltrails.com/trail/us/utah/canyon-overlook-trail"},
        ])
        found = disc._cached_alltrails_batch_url_for_item(
            "Zion National Park", "October 18, 2026", "Canyon Overlook Trail"
        )
        assert found == "https://www.alltrails.com/trail/us/utah/canyon-overlook-trail"

    def test_never_fetches_when_the_trail_batch_has_not_run(self):
        """The whole point of reading cache-only: this must not add a paid call.

        A destination whose trail batch has not run yields nothing rather than
        triggering one.
        """
        disc = self._discoverer()
        disc._alltrails_direct_batch_cache = {}
        with patch.object(disc, "_get_alltrails_direct_batch_rows_for_destination") as fetch:
            found = disc._cached_alltrails_batch_url_for_item("Zion National Park", "October 18, 2026", "Whatever Trail")
        assert found == ""
        fetch.assert_not_called()

    def test_ignores_rows_for_a_different_item(self):
        disc = self._discoverer()
        self._seed_trail_cache(disc, "Zion National Park", "October 18, 2026", [
            {"name": "Emerald Pools Trail",
             "url": "https://www.alltrails.com/trail/us/utah/emerald-pools-trail"},
        ])
        assert disc._cached_alltrails_batch_url_for_item(
            "Zion National Park", "October 18, 2026", "Canyon Overlook Trail"
        ) == ""

    def test_ignores_non_alltrails_rows(self):
        """A trail batch row that came back as an nps.gov page is not a
        substitute -- this preference exists specifically to promote
        trail-specific AllTrails links."""
        disc = self._discoverer()
        self._seed_trail_cache(disc, "Zion National Park", "October 18, 2026", [
            {"name": "Canyon Overlook Trail",
             "url": "https://www.nps.gov/thingstodo/hike-canyon-overlook.htm"},
        ])
        assert disc._cached_alltrails_batch_url_for_item(
            "Zion National Park", "October 18, 2026", "Canyon Overlook Trail"
        ) == ""


class TestEnRouteStopsMayCarryAllTrailsWhenTrailLike:
    """En-route stops used to hard-code allow_alltrails=False.

    That is why Canyon Overlook Trail, harvested from Zion's trail batch with
    its AllTrails URL, published a generic nps.gov page instead: reclassifying
    it as an en-route stop moved it behind a gate that forbids AllTrails
    outright. The gate now mirrors the attraction path's allow_alltrails=
    trail_like.
    """

    @staticmethod
    def _discoverer():
        mock_llm = type("MockLLM", (), {"provider": "grok", "model": "grok-4.5", "usage_tracker": None})()
        with patch("generator.search_provider.GrokSearch"), patch("generator.search_provider.ClaudeSearch"):
            return URLDiscoverer(config_path="config.yaml", llm_client=mock_llm)

    def test_trail_like_seed_stop_keeps_an_alltrails_url(self):
        """Canyon Overlook Trail is a manifest seed, so it takes the relaxed
        seed standard rather than the length/gain/difficulty confidence gate.
        Before the allow_alltrails change this returned "" no matter what."""
        disc = self._discoverer()
        kept = disc._retain_discovered_url(
            "https://www.alltrails.com/trail/us/utah/canyon-overlook-trail",
            "Canyon Overlook Trail",
            "Zion National Park",
            allow_alltrails=True,
            kind="en-route stop",
            is_seed=True,
        )
        assert "alltrails.com" in kept

    def test_non_seed_trail_stop_still_faces_the_publish_confidence_gate(self):
        """Opening allow_alltrails does NOT bypass the trail-quality gate.

        A non-seed trail still has to clear _meets_alltrails_publish_confidence
        and _passes_alltrails_post_search_filters, which need real trail stats.
        This is why the change helps seeded trails most.
        """
        disc = self._discoverer()
        with patch.object(disc, "_meets_alltrails_publish_confidence", return_value=False):
            kept = disc._retain_discovered_url(
                "https://www.alltrails.com/trail/us/utah/some-unverified-trail",
                "Some Unverified Trail",
                "Zion National Park",
                allow_alltrails=True,
                kind="en-route stop",
            )
        assert kept == ""

    def test_non_trail_stop_still_refuses_alltrails(self):
        """The gate is narrowed, not removed."""
        disc = self._discoverer()
        kept = disc._retain_discovered_url(
            "https://www.alltrails.com/trail/us/utah/canyon-overlook-trail",
            "Some Roadside Diner",
            "Zion National Park",
            allow_alltrails=False,
            kind="en-route stop",
        )
        assert kept == ""


class TestBackfillAttractionsFromTrailBatch:
    """Refill attraction slots emptied by URL removal with trails already bought.

    Measured on the 2026-08-21 cold-start run: the trail batch harvested 84
    rows across 10 destinations, all carrying AllTrails URLs, and only 24 were
    published -- 60 discarded (71%). The same run removed 23 attractions for
    having no verified URL. Both ends of the same problem.
    """

    @staticmethod
    def _discoverer():
        mock_llm = type("MockLLM", (), {"provider": "grok", "model": "grok-4.5", "usage_tracker": None})()
        with patch("generator.search_provider.GrokSearch"), patch("generator.search_provider.ClaudeSearch"):
            return URLDiscoverer(config_path="config.yaml", llm_client=mock_llm)

    def _seed(self, disc, rows, dest="Bryce Canyon National Park", dates="October 19-21, 2026"):
        disc._alltrails_direct_batch_cache = {
            disc._batch_cache_key(dest, f"{dates}|html|trail"): rows
        }

    @staticmethod
    def _rows(*names):
        return [
            {"name": n, "url": f"https://www.alltrails.com/trail/us/utah/{n.lower().replace(chr(32), '-')}",
             "description": f"{n} desc", "practical_note": "note", "rating": 4.7}
            for n in names
        ]

    def _call(self, disc, eligible, original_count, ai=None, dest=None, **kw):
        return disc._backfill_attractions_from_trail_batch(
            dest=dest if dest is not None else {},
            ai=ai if ai is not None else {},
            dest_name=kw.get("dest_name", "Bryce Canyon National Park"),
            dest_dates=kw.get("dest_dates", "October 19-21, 2026"),
            eligible=eligible,
            original_count=original_count,
        )

    def test_backfills_only_up_to_the_emptied_slots(self):
        """ai_content sized the list to attractions_per_day * day_count; this
        refills what removal emptied and must not exceed it."""
        disc = self._discoverer()
        self._seed(disc, self._rows("Queens Garden", "Bristlecone Loop", "Rim Trail", "Whale Rock"))
        eligible = [{"name": "Sunrise Point", "url": "https://x/1"}]
        with patch.object(disc, "_retain_discovered_url", side_effect=lambda u, *a, **k: u):
            added = self._call(disc, eligible, original_count=3)
        assert added == 2
        assert len(eligible) == 3

    def test_no_backfill_when_nothing_was_removed(self):
        disc = self._discoverer()
        self._seed(disc, self._rows("Queens Garden"))
        eligible = [{"name": "A", "url": "https://x/1"}, {"name": "B", "url": "https://x/2"}]
        with patch.object(disc, "_retain_discovered_url", side_effect=lambda u, *a, **k: u):
            added = self._call(disc, eligible, original_count=2)
        assert added == 0
        assert len(eligible) == 2

    def test_never_fetches_when_no_trail_batch_ran(self):
        disc = self._discoverer()
        disc._alltrails_direct_batch_cache = {}
        with patch.object(disc, "_get_alltrails_direct_batch_rows_for_destination") as fetch:
            added = self._call(disc, [], original_count=4)
        assert added == 0
        fetch.assert_not_called()

    def test_skips_a_trail_the_itinerary_already_carries(self):
        """Including one already published as an en-route stop or scenic drive
        -- promoting it would render the same place twice."""
        disc = self._discoverer()
        self._seed(disc, self._rows("Mossy Cave", "Bristlecone Loop"))
        eligible = []
        ai = {"getting_here": {"en_route_stops": [{"name": "Mossy Cave"}]}}
        dest = {"scenic_drives": [{"title": "Bristlecone Loop"}]}
        with patch.object(disc, "_retain_discovered_url", side_effect=lambda u, *a, **k: u):
            added = self._call(disc, eligible, original_count=4, ai=ai, dest=dest)
        assert added == 0

    def test_strips_markdown_artifacts_from_names(self):
        """The batch emitted '**Balanced Rock Loop**' style names on the
        2026-08-21 run; the asterisks would become part of the rendered name."""
        disc = self._discoverer()
        self._seed(disc, [{"name": "**Balanced Rock Loop**",
                           "url": "https://www.alltrails.com/trail/us/utah/balanced-rock-loop"}])
        eligible = []
        with patch.object(disc, "_retain_discovered_url", side_effect=lambda u, *a, **k: u):
            self._call(disc, eligible, original_count=2)
        assert eligible[0]["name"] == "Balanced Rock Loop"

    def test_promoted_trails_must_clear_the_quality_gate(self):
        """Promotions are not seeds -- an unvetted trail cannot enter the
        itinerary through this path."""
        disc = self._discoverer()
        self._seed(disc, self._rows("Sketchy Unverified Trail"))
        eligible = []
        with patch.object(disc, "_retain_discovered_url", return_value=""):
            added = self._call(disc, eligible, original_count=4)
        assert added == 0
        assert eligible == []

    def test_ignores_rows_without_an_alltrails_trail_url(self):
        disc = self._discoverer()
        self._seed(disc, [
            {"name": "Browse Page", "url": "https://www.alltrails.com/us/colorado/pagosa-springs/easy"},
            {"name": "Park Page", "url": "https://www.nps.gov/brca/"},
        ])
        eligible = []
        with patch.object(disc, "_retain_discovered_url", side_effect=lambda u, *a, **k: u):
            added = self._call(disc, eligible, original_count=4)
        assert added == 0

    def test_does_not_reuse_a_url_already_published(self):
        """Two batch rows sharing one URL was observed on the 2026-08-21 run
        ('Chimney Rock Trail' and 'Great Kiva Trail' both resolved to
        chimney-rock-trail)."""
        disc = self._discoverer()
        dup = "https://www.alltrails.com/trail/us/colorado/chimney-rock-trail"
        self._seed(disc, [
            {"name": "Chimney Rock Trail", "url": dup},
            {"name": "Great Kiva Trail", "url": dup},
        ])
        eligible = []
        with patch.object(disc, "_retain_discovered_url", side_effect=lambda u, *a, **k: u):
            added = self._call(disc, eligible, original_count=4)
        assert added == 1

    def test_promoted_entry_is_marked_and_carries_its_batch_content(self):
        disc = self._discoverer()
        self._seed(disc, self._rows("Queens Garden"))
        eligible = []
        with patch.object(disc, "_retain_discovered_url", side_effect=lambda u, *a, **k: u):
            self._call(disc, eligible, original_count=2)
        entry = eligible[0]
        assert entry["promoted_from_trail_batch"] is True
        assert entry["type"] == "trail"
        assert entry["is_seed"] is False
        assert entry["description"] == "Queens Garden desc"


class TestAttractionBatchPrewarmWithItineraryItems:
    """Ask the batch for the items the itinerary actually contains.

    Stage 3 invents the attraction list and the Stage 5b batch independently
    invents its own; every non-overlapping name costs a per-item fallback
    search. Measured 2026-08-21: 81 of 82 direct_batch_no_match events were
    attractions, and fallbacks are 42% of all token spend.
    """

    @staticmethod
    def _discoverer():
        mock_llm = type("MockLLM", (), {"provider": "grok", "model": "grok-4.5", "usage_tracker": None})()
        with patch("generator.search_provider.GrokSearch"), patch("generator.search_provider.ClaudeSearch"):
            return URLDiscoverer(config_path="config.yaml", llm_client=mock_llm)

    def test_itinerary_item_names_are_passed_to_the_batch(self):
        disc = self._discoverer()
        ai = {"top_attractions": [{"name": "Sunrise Point"}, {"name": "Bryce Point"}]}
        with patch.object(disc, "_get_attraction_direct_batch_rows_for_destination") as fetch:
            disc._prewarm_attraction_batch_with_itinerary_items(
                ai=ai, dest_name="Bryce Canyon National Park", dest_dates="October 19-21, 2026", seed_names=None
            )
        assert fetch.call_args.kwargs["seed_names"] == ["Sunrise Point", "Bryce Point"]

    def test_seeds_come_first_so_the_cap_cannot_crowd_them_out(self):
        """Seeds are the traveller's explicit asks; generated names must not
        displace them when the hint list is truncated."""
        disc = self._discoverer()
        ai = {"top_attractions": [{"name": f"Generated {i}"} for i in range(40)]}
        with patch.object(disc, "_get_attraction_direct_batch_rows_for_destination") as fetch:
            disc._prewarm_attraction_batch_with_itinerary_items(
                ai=ai, dest_name="Bryce Canyon National Park", dest_dates="", seed_names=["Mossy Cave"]
            )
        hints = fetch.call_args.kwargs["seed_names"]
        assert hints[0] == "Mossy Cave"
        assert len(hints) == disc._MAX_BATCH_ITEM_HINTS

    def test_duplicates_between_seeds_and_generated_names_collapse(self):
        disc = self._discoverer()
        ai = {"top_attractions": [{"name": "Sunrise  Point"}, {"name": "Bryce Point"}]}
        with patch.object(disc, "_get_attraction_direct_batch_rows_for_destination") as fetch:
            disc._prewarm_attraction_batch_with_itinerary_items(
                ai=ai, dest_name="Bryce Canyon National Park", dest_dates="", seed_names=["Sunrise Point"]
            )
        assert fetch.call_args.kwargs["seed_names"] == ["Sunrise Point", "Bryce Point"]

    def test_markdown_artifacts_are_stripped_from_hints(self):
        """"Balanced Rock Loop" is trail-like, so it routes to the trail
        batch -- which is also the point of the routing test below."""
        disc = self._discoverer()
        ai = {"top_attractions": [{"name": "**Balanced Rock Loop**"}]}
        with patch.object(disc, "_get_alltrails_direct_batch_rows_for_destination") as trail_fetch:
            disc._prewarm_attraction_batch_with_itinerary_items(
                ai=ai, dest_name="Arches National Park", dest_dates="", seed_names=None
            )
        assert trail_fetch.call_args.kwargs["seed_names"] == ["Balanced Rock Loop"]

    def test_hikes_go_to_the_trail_batch_not_the_attraction_batch(self):
        """The attraction prompt says "excluding hikes" in its own text.

        Regression for the 2026-08-22 baseline run: the first version of this
        prewarm fed every itinerary item to the attraction batch, hikes
        included. At Zion that meant five names asked for, four of them
        hikes; the model correctly honoured its own exclusion and returned
        one, while the hint list displaced slots that would have held real
        attractions. direct_batch_no_match went UP, 82 -> 108.
        """
        disc = self._discoverer()
        ai = {"top_attractions": [
            {"name": "Emerald Pools Trail", "type": "trail"},
            {"name": "Angels Landing", "description": "a strenuous hike to a summit"},
            {"name": "Zion Human History Museum", "type": "attraction"},
        ]}
        with patch.object(disc, "_get_attraction_direct_batch_rows_for_destination") as attr_fetch,              patch.object(disc, "_get_alltrails_direct_batch_rows_for_destination") as trail_fetch:
            disc._prewarm_attraction_batch_with_itinerary_items(
                ai=ai, dest_name="Zion National Park", dest_dates="October 18, 2026", seed_names=None
            )
        attraction_hints = attr_fetch.call_args.kwargs["seed_names"]
        trail_hints = trail_fetch.call_args.kwargs["seed_names"]
        assert "Zion Human History Museum" in attraction_hints
        assert "Emerald Pools Trail" in trail_hints
        assert "Angels Landing" in trail_hints
        # the whole point: no hike is asked of the batch that excludes hikes
        assert "Emerald Pools Trail" not in attraction_hints
        assert "Angels Landing" not in attraction_hints

    def test_a_destination_with_only_attractions_never_calls_the_trail_batch(self):
        disc = self._discoverer()
        ai = {"top_attractions": [{"name": "Santa Fe Plaza", "type": "attraction"}]}
        with patch.object(disc, "_get_attraction_direct_batch_rows_for_destination") as attr_fetch,              patch.object(disc, "_get_alltrails_direct_batch_rows_for_destination") as trail_fetch:
            disc._prewarm_attraction_batch_with_itinerary_items(
                ai=ai, dest_name="Santa Fe", dest_dates="", seed_names=None
            )
        assert attr_fetch.called
        trail_fetch.assert_not_called()

    def test_no_items_means_no_prewarm_call(self):
        disc = self._discoverer()
        with patch.object(disc, "_get_attraction_direct_batch_rows_for_destination") as fetch:
            disc._prewarm_attraction_batch_with_itinerary_items(
                ai={"top_attractions": []}, dest_name="Moab", dest_dates="", seed_names=None
            )
        fetch.assert_not_called()

    def test_prewarm_failure_does_not_break_discovery(self):
        """A prewarm is an optimisation; the ordinary lazy fetch still runs."""
        disc = self._discoverer()
        ai = {"top_attractions": [{"name": "Delicate Arch"}]}
        with patch.object(disc, "_get_attraction_direct_batch_rows_for_destination", side_effect=RuntimeError("boom")):
            disc._prewarm_attraction_batch_with_itinerary_items(
                ai=ai, dest_name="Arches National Park", dest_dates="", seed_names=None
            )  # must not raise


class TestEnRouteStopsResolveToMaps:
    """En-route stops resolve to a Google Maps link, not a website.

    They are waypoints on a drive: the traveller needs to find the pullout,
    not read about it. Measured on the 2026-08-21 cold-start run, chasing
    websites for them produced 253 of 301 batch candidate rejections -- about
    four rejected candidates per stop -- because the candidates were
    land-agency landing pages (blm.gov 55, nps.gov 48, fs.usda.gov 24)
    offered for a specific roadside stop. That page granularity does not
    exist, so the searches could not be tuned into succeeding.
    """

    @staticmethod
    def _discoverer():
        mock_llm = type("MockLLM", (), {"provider": "grok", "model": "grok-4.5", "usage_tracker": None})()
        with patch("generator.search_provider.GrokSearch"), patch("generator.search_provider.ClaudeSearch"):
            return URLDiscoverer(config_path="config.yaml", llm_client=mock_llm)

    def test_verified_geocode_produces_a_coordinate_link(self):
        """Coordinates beat a name search: they resolve to the exact spot,
        and _prune_en_route_stops_by_geometry has already checked that
        coordinate against the real route."""
        disc = self._discoverer()
        # lat/lng must be numeric: _item_has_verified_route_geocode requires
        # int/float, so a stringified coordinate is deliberately NOT trusted.
        stop = {"name": "Canyon Overlook", "route_waypoint_eligible": True,
                "geocode_lat": 37.2128153, "geocode_lng": -112.9445374}
        url = disc._en_route_maps_url(stop, "Canyon Overlook", "Zion National Park")
        assert "google.com/maps/search/" in url
        assert "37.2128153%2C-112.9445374" in url or "37.2128153,-112.9445374" in url

    def test_without_a_geocode_it_falls_back_to_a_name_query(self):
        disc = self._discoverer()
        url = disc._en_route_maps_url({"name": "Shafer Canyon Overlook"}, "Shafer Canyon Overlook", "Canyonlands National Park")
        assert "google.com/maps/search/" in url
        assert "Shafer" in url

    def test_a_free_geocode_is_tried_before_settling_for_a_name_query(self):
        """An en-route stop gets the same free geocode an attraction gets.

        Without this the New England ride shipped Mount Agamenticus, Wickford
        Village and Stony Creek as bare text queries -- the unverifiable form
        -- while every stop beside them carried a coordinate, purely because
        nothing on this path asked for one.
        """
        disc = self._discoverer()
        with patch.object(disc, "_geocode_en_route_stop_for_route", return_value=(43.2214, -70.6939)):
            url = disc._en_route_maps_url(
                {"name": "Mount Agamenticus"},
                "Mount Agamenticus",
                "Portsmouth, New Hampshire",
                (43.0718, -70.7626),
            )
        assert URLDiscoverer._is_coordinate_maps_query_url(url)
        assert "43.2214" in url
        assert "Agamenticus" not in url

    def test_no_bias_box_means_no_geocode_at_all(self):
        """An unbiased geocode is wrong, not merely vaguer.

        "Canyon Overlook" near Zion resolves to 34.26,-84.54 -- Georgia --
        and a coordinate link renders that as a precise pin with nothing
        admitting the guess. Without the destination to bias against, the
        name query is the honest answer.
        """
        disc = self._discoverer()
        with patch.object(disc, "_geocode_en_route_stop_for_route") as geo:
            url = disc._en_route_maps_url(
                {"name": "Canyon Overlook"}, "Canyon Overlook", "Zion National Park"
            )
        geo.assert_not_called()
        assert "Canyon" in url

    def test_the_name_query_is_reached_only_after_the_geocode_fails(self):
        """The text query stays as the last resort, not the second step."""
        disc = self._discoverer()
        with patch.object(disc, "_geocode_en_route_stop_for_route", return_value=None):
            url = disc._en_route_maps_url(
                {"name": "Stony Creek"}, "Stony Creek", "New Haven, Connecticut"
            )
        assert not URLDiscoverer._is_coordinate_maps_query_url(url)
        assert "Stony" in url

    def test_a_route_verified_geocode_still_outranks_the_free_one(self):
        """Ordering, not just presence: the coordinate the route vouched for
        must win, and no geocode call should be made at all."""
        disc = self._discoverer()
        stop = {"name": "Canyon Overlook", "route_waypoint_eligible": True,
                "geocode_lat": 37.2128153, "geocode_lng": -112.9445374}
        with patch.object(disc, "_geocode_en_route_stop_for_route") as geo:
            url = disc._en_route_maps_url(stop, "Canyon Overlook", "Zion National Park", (37.3, -113.0))
        geo.assert_not_called()
        assert "37.2128153" in url

    def test_maps_mode_is_an_accepted_en_route_source(self):
        disc = self._discoverer()
        assert getattr(disc, "_en_route_source", "") == "maps"

    def test_stringified_coordinates_are_not_trusted_and_fall_back_to_name(self):
        """_item_has_verified_route_geocode requires numeric lat/lng. A stop
        carrying strings has not been through the geometry check, so it must
        not be presented as an exact pin."""
        disc = self._discoverer()
        stop = {"name": "Canyon Overlook", "route_waypoint_eligible": True,
                "geocode_lat": "37.2128153", "geocode_lng": "-112.9445374"}
        url = disc._en_route_maps_url(stop, "Canyon Overlook", "Zion National Park")
        assert "37.2128153" not in url
        assert "Canyon" in url

    def test_a_nameless_stop_yields_no_link_rather_than_a_bare_destination_pin(self):
        disc = self._discoverer()
        assert disc._en_route_maps_url({}, "", "") == ""


class TestCoordinateMapsUrlSurvivesTheQueryRebuild:
    """A coordinate Maps link must not be rewritten into a name search.

    _retain_discovered_url rebuilds google_maps_search queries to sanitize
    AI-authored query text (the dipstick67 "Sunrise Point ... UT" case). A
    bare lat,lng query is not AI-authored -- this code built it from a
    geocode _prune_en_route_stops_by_geometry already resolved and checked
    against the route, so rebuilding it into a name query is a downgrade.

    Caught on the 2026-08-22 baseline run: every coordinate link produced by
    en_route_source "maps" was being silently rewritten. The mode still
    emitted a Maps link, so it looked correct while losing the precision it
    exists for.
    """

    def test_detects_a_bare_coordinate_query(self):
        f = URLDiscoverer._is_coordinate_maps_query_url
        assert f("https://www.google.com/maps/search/?api=1&query=38.5142197%2C-109.7387738")
        assert f("https://www.google.com/maps/search/?api=1&query=37.2128153,-112.9445374")

    def test_a_place_name_query_is_not_a_coordinate(self):
        """These MUST still be rebuilt -- that path is load-bearing."""
        f = URLDiscoverer._is_coordinate_maps_query_url
        assert not f("https://www.google.com/maps/search/?api=1&query=Dead%20Horse%20Point%20near%20Moab")
        assert not f("https://www.google.com/maps/search/?api=1&query=Route%2066")

    def test_a_non_maps_url_is_not_a_coordinate(self):
        assert not URLDiscoverer._is_coordinate_maps_query_url("https://moabmuseum.org/")
        assert not URLDiscoverer._is_coordinate_maps_query_url("")

    def test_coordinate_link_is_retained_unchanged_for_an_en_route_stop(self):
        mock_llm = type("MockLLM", (), {"provider": "grok", "model": "grok-4.5", "usage_tracker": None})()
        with patch("generator.search_provider.GrokSearch"), patch("generator.search_provider.ClaudeSearch"):
            disc = URLDiscoverer(config_path="config.yaml", llm_client=mock_llm)
        coord = "https://www.google.com/maps/search/?api=1&query=38.5142197%2C-109.7387738"
        kept = disc._retain_discovered_url(
            coord,
            "Dead Horse Point State Park",
            "Canyonlands National Park",
            allow_alltrails=False,
            kind="en-route stop",
            allow_google_maps_search=True,
        )
        assert kept == coord, "coordinate was rewritten into a name query"


class TestTheAuditWritesBackWhatItResolved:
    """A resolved URL must reach the stop, not just the log.

    `audit_discovered_urls` wrote `stop["url"]` only when retention CHANGED
    the URL it was handed. That silently discarded every value produced by
    the two blocks above it -- the AllTrails-batch preference and maps mode
    -- because those reassign the local `url`, so retention accepting the new
    value unchanged looked identical to "nothing to do".

    Measured on the East Coast Greenway run: `en_route_resolved_to_maps`
    announced a coordinate for Wickford Village Historic District and Stony
    Creek, `en_route_geocode_verified_kept` accepted it, and the page still
    rendered the text query the direct batch had left in `stop["url"]`. The
    decision log said the fix worked, which is why it survived a rerun.
    """

    @staticmethod
    def _discoverer():
        mock_llm = type("MockLLM", (), {"provider": "grok", "model": "grok-4.5", "usage_tracker": None})()
        with patch("generator.search_provider.GrokSearch"), patch("generator.search_provider.ClaudeSearch"):
            disc = URLDiscoverer(config_path="config.yaml", llm_client=mock_llm)
        disc._en_route_source = "maps"
        return disc

    @staticmethod
    def _trip(stop, name="Narragansett, Rhode Island", lat=41.4501, lng=-71.4495):
        return {
            "destinations": [
                {
                    "name": name,
                    "lat": lat,
                    "lng": lng,
                    "ai_content": {"getting_here": {"en_route_stops": [stop]}},
                }
            ]
        }

    def test_a_coordinate_replaces_the_text_query_the_batch_left_behind(self):
        disc = self._discoverer()
        stop = {
            "name": "Wickford Village Historic District",
            "url": "https://www.google.com/maps/search/?api=1&query=Wickford%20Village%20Historic%20District",
            "route_waypoint_eligible": True,
            "geocode_lat": 41.5711941,
            "geocode_lng": -71.4524181,
        }
        disc.audit_discovered_urls(self._trip(stop))

        assert URLDiscoverer._is_coordinate_maps_query_url(stop.get("url", "")), (
            f"stop kept {stop.get('url')!r} instead of the coordinate it resolved to"
        )
        assert "41.5711941" in stop["url"]

    def test_a_real_page_already_harvested_is_not_replaced_by_a_pin(self):
        """Maps mode saves the hunt, it does not refuse a page in hand.

        Widening the write-back exposed this: with the assignment finally
        landing, maps mode overwrote every official page the direct batch had
        found -- thetrustees.org, ipswichmuseum.org, historicbeverly.net --
        with a bare coordinate. 34 real pages became pins in one run. The
        AllTrails exemption already stated the rule ("free, specific, and
        strictly more useful than a pin"); nothing about it was specific to
        AllTrails.
        """
        disc = self._discoverer()
        page = "https://thetrustees.org/place/appleton-farms/"
        stop = {
            "name": "Appleton Farms",
            "url": page,
            "route_waypoint_eligible": True,
            "geocode_lat": 42.6478864,
            "geocode_lng": -70.8542545,
        }
        # Retention is held to a passthrough so this asserts the maps-mode
        # guard alone. Its real verdict depends on harvested candidate rows
        # this bare harness has none of, which is a separate question from
        # whether maps mode should have displaced the page before it.
        with patch.object(disc, "_retain_discovered_url", side_effect=lambda u, *a, **k: u):
            disc.audit_discovered_urls(
                self._trip(stop, "Boston, Massachusetts", 42.3601, -71.0589)
            )
        assert stop.get("url") == page

    def test_a_stop_already_holding_the_right_value_is_left_alone(self):
        """The write-back must be idempotent, not merely present.

        A stop that already carries the coordinate maps mode would resolve
        for it must come out the other side carrying that same coordinate --
        the condition widened here governs whether an assignment happens, and
        widening it must not turn into rewriting what was already correct.
        """
        disc = self._discoverer()
        coord = "https://www.google.com/maps/search/?api=1&query=41.5711941%2C-71.4524181"
        stop = {
            "name": "Wickford Village Historic District",
            "url": coord,
            "route_waypoint_eligible": True,
            "geocode_lat": 41.5711941,
            "geocode_lng": -71.4524181,
        }
        disc.audit_discovered_urls(self._trip(stop))
        assert stop.get("url") == coord


class TestMapsModeStillRunsTheEnRouteHarvest:
    """"maps" changes URL resolution, not whether the harvest runs.

    The en-route direct batch does two jobs: it supplies candidate en-route
    STOPS as well as their URLs. The first version of maps mode conflated
    them and skipped the batch entirely. The 2026-08-22 run measured the
    cost: en-route stop cards fell 50 -> 12 (a 76% content loss) and the
    geometry pass that assigns route-verified geocodes collapsed with them
    (35 -> 9). The apparent "-83% batch candidate rejections" that made the
    mode look successful was mostly stops ceasing to exist.
    """

    def test_maps_mode_is_batch_backed(self):
        """Both modes must take the harvest branch; only resolution differs."""
        import inspect
        from generator import url_discovery as ud

        src = inspect.getsource(ud.URLDiscoverer._discover_en_route_stops)
        # the harvest gate must admit "maps", not only "direct_link_batch"
        assert 'source_mode in {"direct_link_batch", "maps"}' in src
        assert 'source_mode == "direct_link_batch"' not in src, (
            "a bare direct_link_batch gate would skip the harvest in maps mode "
            "and delete most en-route stops"
        )


class TestGeocodeMapsFallback:
    """Replace the paid per-item fallback with a free geocode.

    That path was 66% of a cold run on 2026-08-22 -- $3.86 of $5.85, 218
    calls, 306 billed web_search invocations -- and fires only for items the
    direct batch already failed to resolve. After spending it, the
    verified-link-or-seed policy deleted 29 attractions anyway.
    """

    @staticmethod
    def _discoverer(mode="geocode_maps"):
        mock_llm = type("MockLLM", (), {"provider": "grok", "model": "grok-4.5", "usage_tracker": None})()
        with patch("generator.search_provider.GrokSearch"), patch("generator.search_provider.ClaudeSearch"):
            d = URLDiscoverer(config_path="config.yaml", llm_client=mock_llm)
        d._fallback_mode = mode
        return d

    def test_a_coordinate_maps_link_counts_as_verified(self):
        """Owner decision 2026-08-22. The 2026-08-17 rule bars best-guess TEXT
        queries; a lat,lng names one point and is resolved by a geocoder."""
        item = {"url": "https://www.google.com/maps/search/?api=1&query=37.2982%2C-113.0263"}
        assert URLDiscoverer._item_has_verified_url(item)

    def test_a_name_query_maps_link_still_does_not(self):
        """The original rule is intact -- this is an extension, not a repeal."""
        item = {"url": "https://www.google.com/maps/search/?api=1&query=Angels%20Landing%20Zion"}
        assert not URLDiscoverer._item_has_verified_url(item)

    def test_geocode_maps_mode_does_not_buy_a_search(self):
        disc = self._discoverer("geocode_maps")
        with patch.object(disc, "_search_first_strict") as paid:
            result = disc._search_first(["angels landing"], item_name="Angels Landing", dest_name="Zion")
        assert result is None
        paid.assert_not_called()

    def test_search_mode_still_buys_one(self):
        """The lever must be reversible -- config back to "search" restores it."""
        disc = self._discoverer("search")
        with patch.object(disc, "_search_first_strict", return_value="https://nps.gov/zion") as paid:
            result = disc._search_first(["angels landing"], item_name="Angels Landing 2", dest_name="Zion")
        assert result == "https://nps.gov/zion"
        paid.assert_called_once()

    def test_geocode_builds_a_coordinate_link(self):
        disc = self._discoverer()
        with patch.object(disc, "_geocode_en_route_stop_for_route", return_value=(37.2982, -113.0263)):
            url = disc._geocode_maps_url_for_item("Angels Landing", "Zion National Park", (37.3, -113.0))
        assert URLDiscoverer._is_coordinate_maps_query_url(url)
        assert "37.2982" in url

    def test_a_failed_geocode_leaves_the_item_untouched(self):
        """No coordinate is better than a wrong one -- the item then falls to
        the normal retention policy exactly as before."""
        disc = self._discoverer()
        with patch.object(disc, "_geocode_en_route_stop_for_route", return_value=None):
            assert disc._geocode_maps_url_for_item("Nowhere", "Zion National Park") == ""


class TestCategorySwitchesActuallyStopSpending:
    """A switch that hides output while still buying it is worse than no switch.

    Measured 2026-08-23 with trails DISABLED: alltrails_trail_filtered still
    made 98 paid fallback calls. _search_alltrails_for_trail and
    _search_alltrails_for_seed_relaxed had always honoured _disable_trails;
    the filtered variant -- the highest-volume of the three -- did not.
    """

    @staticmethod
    def _discoverer(**kw):
        mock_llm = type("M", (), {"provider": "grok", "model": "grok-4.5", "usage_tracker": None})()
        with patch("generator.search_provider.GrokSearch"), patch("generator.search_provider.ClaudeSearch"):
            return URLDiscoverer(config_path="config.yaml", llm_client=mock_llm, **kw)

    def test_filtered_trail_search_is_skipped_when_trails_disabled(self):
        disc = self._discoverer(disable_trails=True)
        with patch.object(disc, "_search_cached") as paid:
            result = disc._search_alltrails_for_trail_filtered(
                item_name="Angels Landing", dest_name="Zion National Park",
                query_variants=["angels landing alltrails"],
            )
        assert result is None
        paid.assert_not_called()

    def test_all_three_alltrails_entry_points_honour_the_switch(self):
        """The gap was one of three paths, so assert all three together."""
        disc = self._discoverer(disable_trails=True)
        assert disc._search_alltrails_for_trail("Angels Landing", "Zion National Park") is None
        assert disc._search_alltrails_for_seed_relaxed("Angels Landing", "Zion National Park") is None
        assert disc._search_alltrails_for_trail_filtered(
            item_name="Angels Landing", dest_name="Zion National Park", query_variants=["x"],
        ) is None


class TestRestaurantsSwitch:
    """Restaurants are switchable but default ON -- dining is arguably core
    itinerary content, unlike trails, en-route stops and cultural events.

    The gate sits above every purchase path (direct batch, its prioritisation
    pass, per-item fallbacks) so that disabling stops the SPENDING, not just
    the rendering. That distinction is the lesson from the trails switch,
    which hid its output while still buying 98 calls per run.
    """

    @staticmethod
    def _discoverer(**kw):
        mock_llm = type("M", (), {"provider": "grok", "model": "grok-4.5", "usage_tracker": None})()
        with patch("generator.search_provider.GrokSearch"), patch("generator.search_provider.ClaudeSearch"):
            return URLDiscoverer(config_path="config.yaml", llm_client=mock_llm, **kw)

    def test_disabled_empties_the_section_and_buys_nothing(self):
        disc = self._discoverer(disable_restaurants=True)
        ai = {"dinner_recommendations": [{"name": "Some Bistro"}, {"name": "Another"}]}
        with patch.object(disc, "_get_restaurant_direct_batch_rows_for_destination") as batch, \
             patch.object(disc, "_search_restaurant_from_direct_batch") as per_item:
            disc._discover_restaurants(ai, "Moab", "October 22-24, 2026")
        assert ai["dinner_recommendations"] == []
        batch.assert_not_called()
        per_item.assert_not_called()

    def test_shipped_config_leaves_restaurants_on(self):
        from generator.main import _category_enabled
        assert _category_enabled("config.yaml", "restaurants", default=True) is True

    def test_explicit_false_turns_them_off(self, tmp_path):
        from generator.main import _category_enabled
        cfg = tmp_path / "c.yaml"
        cfg.write_text("restaurants:\n  enabled: false\n", encoding="utf-8")
        assert _category_enabled(str(cfg), "restaurants", default=True) is False


class TestTrailsSwitchClosesEveryEntryPoint:
    """Four AllTrails entry points, guarded one leak at a time.

    2026-08-23 history, because the shape matters more than the fix: the
    switch first guarded two of three search paths (the third bought 98 calls
    while its output was hidden); that was fixed; the next run then showed
    the fallback correctly at 0 calls while the trail direct BATCH ran for
    all ten destinations and put 8 AllTrails links in the output.

    Each partial fix made the leak smaller and none closed it, which is why
    this asserts every entry point together rather than the newest one.
    """

    @staticmethod
    def _disabled():
        mock_llm = type("M", (), {"provider": "grok", "model": "grok-4.5", "usage_tracker": None})()
        with patch("generator.search_provider.GrokSearch"), patch("generator.search_provider.ClaudeSearch"):
            return URLDiscoverer(config_path="config.yaml", llm_client=mock_llm, disable_trails=True)

    def test_no_search_path_runs(self):
        d = self._disabled()
        assert d._search_alltrails_for_trail("Angels Landing", "Zion National Park") is None
        assert d._search_alltrails_for_seed_relaxed("Angels Landing", "Zion National Park") is None
        assert d._search_alltrails_for_trail_filtered(
            item_name="Angels Landing", dest_name="Zion National Park", query_variants=["x"]
        ) is None

    def test_the_trail_direct_batch_does_not_run(self):
        """The leak the previous two fixes left open."""
        d = self._disabled()
        ai = {"top_attractions": [{"name": "Angels Landing", "type": "trail"}]}
        with patch.object(d, "_prioritize_direct_batch_trails") as batch, \
             patch.object(d, "_get_alltrails_direct_batch_rows_for_destination") as rows, \
             patch.object(d, "_prewarm_attraction_batch_with_itinerary_items"), \
             patch.object(d, "_prioritize_direct_batch_attractions", side_effect=lambda a, *x, **k: a):
            d._discover_attractions(ai, "Zion National Park", None, "October 18, 2026", [])
        batch.assert_not_called()
        rows.assert_not_called()


class TestTrailsSwitchEnforcedAtTheChokepoint:
    """No AllTrails URL survives retention when trails are disabled.

    Guarding call sites individually failed four times: three search entry
    points, then the trail direct batch, and the 2026-08-23 Core run still
    resolved 29 AllTrails URLs -- 56% of everything the paid fallback found
    -- because the general per-item hunt passes allow_alltrails=True.

    _retain_discovered_url is the single point every candidate passes
    through, so the switch is enforced there regardless of which path
    proposed the URL.
    """

    @staticmethod
    def _disc(disable):
        mock_llm = type("M", (), {"provider": "grok", "model": "grok-4.5", "usage_tracker": None})()
        with patch("generator.search_provider.GrokSearch"), patch("generator.search_provider.ClaudeSearch"):
            return URLDiscoverer(config_path="config.yaml", llm_client=mock_llm, disable_trails=disable)

    def test_alltrails_rejected_even_when_the_caller_allows_it(self):
        """The exact hole: allow_alltrails=True from a general search path."""
        disc = self._disc(True)
        kept = disc._retain_discovered_url(
            "https://www.alltrails.com/trail/us/utah/angels-landing-trail",
            "Angels Landing", "Zion National Park",
            allow_alltrails=True, kind="attraction", is_seed=True,
        )
        assert kept == ""

    def test_non_alltrails_urls_are_unaffected(self):
        """The switch must not become a general URL filter.

        Asserted as an equivalence rather than an absolute: whatever the rest
        of the retention pipeline decides about a non-AllTrails URL, the
        trails switch must not change it.
        """
        url = "https://www.nps.gov/zion/planyourvisit/index.htm"
        args = ("Zion Visitor Center", "Zion National Park")
        kw = dict(allow_alltrails=True, kind="attraction", is_seed=True)
        off = self._disc(True)._retain_discovered_url(url, *args, **kw)
        on = self._disc(False)._retain_discovered_url(url, *args, **kw)
        assert off == on

    def test_with_trails_enabled_alltrails_still_passes(self):
        disc = self._disc(False)
        kept = disc._retain_discovered_url(
            "https://www.alltrails.com/trail/us/utah/angels-landing-trail",
            "Angels Landing", "Zion National Park",
            allow_alltrails=True, kind="attraction", is_seed=True,
        )
        assert "alltrails.com" in kept


class TestTrailsSwitchClosesThePrewarm:
    """The prewarm must not start a trail batch when trails are disabled.

    Fifth leak in this one switch, introduced by the hint-routing change:
    routing trail-like names to the trail batch prewarms it. On the
    2026-08-24 Core run that ran the trail batch for all ten destinations
    with trails.enabled false -- 9 paid calls, ~$0.57 of a $1.77 run, for a
    category that was switched off.
    """

    @staticmethod
    def _disc(disable):
        mock_llm = type("M", (), {"provider": "grok", "model": "grok-4.3", "usage_tracker": None})()
        with patch("generator.search_provider.GrokSearch"), patch("generator.search_provider.ClaudeSearch"):
            return URLDiscoverer(config_path="config.yaml", llm_client=mock_llm, disable_trails=disable)

    def test_disabled_trails_prewarm_no_trail_batch(self):
        disc = self._disc(True)
        ai = {"top_attractions": [
            {"name": "Angels Landing", "type": "trail"},
            {"name": "Zion Human History Museum", "type": "attraction"},
        ]}
        with patch.object(disc, "_get_alltrails_direct_batch_rows_for_destination") as trail, \
             patch.object(disc, "_get_attraction_direct_batch_rows_for_destination") as attr:
            disc._prewarm_attraction_batch_with_itinerary_items(
                ai=ai, dest_name="Zion National Park", dest_dates="October 18, 2026", seed_names=None)
        trail.assert_not_called()
        assert attr.called, "non-trail hints must still prewarm the attraction batch"

    def test_enabled_trails_still_prewarm_it(self):
        disc = self._disc(False)
        ai = {"top_attractions": [{"name": "Angels Landing", "type": "trail"}]}
        with patch.object(disc, "_get_alltrails_direct_batch_rows_for_destination") as trail, \
             patch.object(disc, "_get_attraction_direct_batch_rows_for_destination"):
            disc._prewarm_attraction_batch_with_itinerary_items(
                ai=ai, dest_name="Zion National Park", dest_dates="", seed_names=None)
        trail.assert_called_once()


# ── GH #2 Phase 1: the stage-3-to-stage-5b overwrite hazard ───────────────

def test_route_distance_update_returns_early_on_a_transit_leg() -> None:
    """multimodal-routing.md 4.1. `best_time` here is a scraped Google
    DRIVING duration or a 60 mph Haversine estimate, and the overwrite fires
    when `distance_miles` is empty -- which is exactly the state a transit leg
    is supposed to be in. Without the guard a real rail duration set in stage
    3 is silently replaced by a car estimate in stage 5b."""
    from generator.url_discovery import URLDiscoverer

    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    getting_here = {"travel_time": "3 hr 15 min", "distance_miles": ""}
    ai = {"getting_here": getting_here}

    discoverer._update_route_distance_and_time(
        ai=ai,
        getting_here=getting_here,
        origin_name="Bryce Canyon",
        dest_name="Capitol Reef",
        origin_lat=37.6, origin_lng=-112.1,
        dest_lat=38.2, dest_lng=-111.2,
        dest={"name": "Capitol Reef", "_transport_mode": "transit"},
    )

    assert getting_here["travel_time"] == "3 hr 15 min"
    assert getting_here["distance_miles"] == ""


def test_route_distance_update_still_runs_on_a_driving_leg() -> None:
    """The guard must not disarm the correction it was added beside."""
    from generator.url_discovery import URLDiscoverer

    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._route_distance_live_fetch_enabled = False
    getting_here = {"travel_time": "", "distance_miles": ""}
    ai = {"getting_here": getting_here}

    discoverer._update_route_distance_and_time(
        ai=ai,
        getting_here=getting_here,
        origin_name="Bryce Canyon",
        dest_name="Capitol Reef",
        origin_lat=37.6, origin_lng=-112.1,
        dest_lat=38.2, dest_lng=-111.2,
        dest={"name": "Capitol Reef"},
    )

    assert getting_here["travel_time"]
    assert getting_here["distance_miles"]


def test_en_route_discovery_skipped_on_a_transport_mode_transit_leg() -> None:
    """There is no roadside to stop at on a train, and this path harvests its
    own stops independently of ai_content's -- two sources, one rule."""
    from generator.url_discovery import URLDiscoverer

    assert URLDiscoverer._arrival_is_not_self_driven({"_transport_mode": "transit"}) is True
    # `mixed` keeps them: the drive is still on the table there.
    assert URLDiscoverer._arrival_is_not_self_driven({"_transport_mode": "mixed"}) is False
    assert URLDiscoverer._arrival_is_not_self_driven({}) is False


# ── GH #2: the leg's own trail, not the day loops near the town ───────────

class TestLegTrailLink:
    def _discoverer(self, found_url, disable_trails=True, disable_en_route=True):
        from generator.url_discovery import URLDiscoverer

        d = URLDiscoverer.__new__(URLDiscoverer)
        d._disable_trails = disable_trails
        d._disable_en_route = disable_en_route
        d.calls = []

        def _search(item_name, dest_name, dates="", *, allow_when_disabled=False,
                    force_search=False):
            d.calls.append((item_name, allow_when_disabled, force_search))
            return found_url if allow_when_disabled or not d._disable_trails else None

        d._search_alltrails_for_trail = _search
        d._log_decision = lambda **kw: None
        return d

    def test_it_asks_for_the_section_the_manifest_named(self):
        d = self._discoverer("https://www.alltrails.com/trail/us/washington/pct-h")
        ai = {"getting_here": {}}
        dest = {"name": "Trout Lake", "_transport_mode": "hike",
                "_trail_name": "Pacific Crest Trail",
                "trail_section": "PCT: Section H - Bridge of the Gods - White Pass"}

        d._discover_leg_trail_link(ai, dest, "Cascade Locks", "Trout Lake")

        assert d.calls[0][0] == "PCT: Section H - Bridge of the Gods - White Pass"
        assert ai["getting_here"]["trail_url"].endswith("pct-h")
        assert ai["getting_here"]["trail_label"] == (
            "PCT: Section H - Bridge of the Gods - White Pass"
        )

    def test_a_leg_with_no_section_name_asks_nothing(self):
        """The composed "<trail> <stop> to <stop>" query was tried and removed.
        It failed both ways: nothing matched it across a full PCT run, and on
        the East Coast Greenway it matched the WRONG thing -- "East Coast
        Greenway Newburyport to Boston" resolved to the East Boston Greenway,
        a three-mile neighbourhood path, and rendered under a label naming the
        43-mile leg. A name the generator invented is not evidence about a
        catalogue's contents."""
        d = self._discoverer("https://www.alltrails.com/trail/us/massachusetts/east-boston-greenway")
        ai = {"getting_here": {}}
        dest = {"name": "Boston", "_transport_mode": "bike",
                "_trail_name": "East Coast Greenway"}

        d._discover_leg_trail_link(ai, dest, "Newburyport", "Boston")

        assert d.calls == []
        assert "trail_url" not in ai["getting_here"]

    def test_it_runs_with_trail_links_switched_off(self):
        """--trails governs trail links for attractions AT a destination --
        on a thru-hike, day loops near the resupply town. The leg's own trail
        is the opposite thing and is asked for by naming it in the manifest,
        so tying them together meant the wanted one dragged in the unwanted."""
        d = self._discoverer("https://www.alltrails.com/trail/x", disable_trails=True)
        ai = {"getting_here": {}}
        dest = {"name": "B", "_transport_mode": "hike", "_trail_name": "Pacific Crest Trail",
                "trail_section": "PCT: Section B"}

        d._discover_leg_trail_link(ai, dest, "A", "B")

        assert d.calls[0][1] is True
        # And past the direct-link batch, which is a per-destination harvest a
        # leg's trail cannot be in by construction.
        assert d.calls[0][2] is True
        assert ai["getting_here"]["trail_url"]

    def test_a_manifest_supplied_url_is_used_and_costs_no_search(self):
        """The catalogue's own title and slug disagree -- AllTrails names one
        page "Section B - Callahan's-Ashland to Fish Lake" and slugs it
        "pct-or-section-b-highway-5-to-highway-140-fish-lake" -- so strict
        matching rejects it under either phrasing. An authored URL ends that,
        and design.md 1.4 bars the MODEL from producing a URL, not the human."""
        d = self._discoverer("https://www.alltrails.com/trail/should-not-be-used")
        ai = {"getting_here": {}}
        dest = {
            "name": "Fish Lake Resort", "_transport_mode": "hike",
            "_trail_name": "Pacific Crest Trail",
            "trail_section": "Pacific Crest Trail (PCT): Section B",
            "trail_url": "https://www.alltrails.com/trail/us/oregon/pct-or-section-b",
        }

        d._discover_leg_trail_link(ai, dest, "Callahan's Lodge", "Fish Lake Resort")

        assert ai["getting_here"]["trail_url"].endswith("pct-or-section-b")
        assert ai["getting_here"]["trail_label"] == "Pacific Crest Trail (PCT): Section B"
        assert d.calls == []

    def test_an_authored_section_name_is_searched_when_no_url_is_given(self):
        d = self._discoverer("https://www.alltrails.com/trail/x")
        ai = {"getting_here": {}}
        dest = {"name": "B", "_transport_mode": "hike", "_trail_name": "PCT",
                "trail_section": "PCT: Section B"}

        d._discover_leg_trail_link(ai, dest, "A", "B")

        assert d.calls[0][0] == "PCT: Section B"

    def test_the_first_leg_still_gets_its_authored_link(self):
        """The first destination has no origin_name in the discovery pass even
        when trip.departure gives it a real leg. That is only needed to
        compose a section name, so requiring it dropped 1 of 15 links on a
        manifest that named every section outright."""
        d = self._discoverer(None)
        ai = {"getting_here": {}}
        dest = {"name": "Callahan's Lodge", "_transport_mode": "hike",
                "_trail_name": "Pacific Crest Trail",
                "trail_section": "PCT: Section R/A - Seiad Valley to Interstate 5",
                "trail_url": "https://www.alltrails.com/trail/us/california/pct-section-r-a"}

        d._discover_leg_trail_link(ai, dest, "", "Callahan's Lodge")

        assert ai["getting_here"]["trail_url"].endswith("pct-section-r-a")

    def test_no_trail_name_means_no_query(self):
        """"A to B" alone matches whatever AllTrails has near either end."""
        d = self._discoverer("https://www.alltrails.com/trail/x")
        ai = {"getting_here": {}}

        d._discover_leg_trail_link(ai, {"name": "B", "_transport_mode": "hike"}, "A", "B")

        assert d.calls == []
        assert "trail_url" not in ai["getting_here"]

    def test_nothing_found_leaves_the_card_alone(self):
        d = self._discoverer(None)
        ai = {"getting_here": {}}
        dest = {"name": "B", "_transport_mode": "hike", "_trail_name": "PCT"}

        d._discover_leg_trail_link(ai, dest, "A", "B")

        assert "trail_url" not in ai["getting_here"]


def test_leg_trail_discovery_is_its_own_job_not_a_tail_of_en_route():
    """It was a tail of _discover_en_route_stops for one release and never
    ran once: en-route stops are off by default and that early return sits
    above where the lookup had been appended."""
    import inspect

    from generator.url_discovery import URLDiscoverer

    en_route_src = inspect.getsource(URLDiscoverer._discover_en_route_stops)
    assert "_discover_leg_trail_link" not in en_route_src

    dispatch = inspect.getsource(URLDiscoverer.discover_all)
    assert "_discover_leg_trail_link" in dispatch


def test_concurrent_page_fetches_do_not_swap_redirect_targets():
    """The redirect gate (`_redirect_target_lacks_item_relevance`) rejects a
    candidate URL on the strength of `_fetch_final_url_cache`, and page
    fetches run on a thread pool. If the final URL travels back from the
    validator through one attribute on the shared validator instance,
    another thread can overwrite it in the window between this thread's call
    returning and this thread reading it -- so this URL is filed against
    that URL's redirect, and a good link is rejected (or a bad one accepted)
    for a reason nothing in the record explains.

    The interleaving is forced, not raced: thread A's fetch completes, then
    parks in that exact window until thread B's fetch has completed, and
    only then does A read the final URL back.
    """
    from concurrent.futures import ThreadPoolExecutor

    from generator.url_validator import URLValidator

    url_a = "https://www.opentable.com/r/some-restaurant"
    url_b = "https://www.fws.gov/refuge/some-refuge"
    final_a = "https://www.opentable.com/r/some-restaurant-renamed"
    final_b = "https://www.fws.gov/refuge/some-refuge/visit-us"
    finals = {url_a: final_a, url_b: final_b}

    a_fetched = threading.Event()
    b_fetched = threading.Event()

    class SequencedValidator(URLValidator):
        def get_text(self, url, timeout=None):
            out = super().get_text(url, timeout=timeout)
            if url == url_a:
                a_fetched.set()
                assert b_fetched.wait(timeout=10), "thread B never fetched"
            else:
                assert a_fetched.wait(timeout=10), "thread A never fetched"
                b_fetched.set()
            return out

    def fake_get(url, **kwargs):
        resp = MagicMock()
        resp.status_code = 200
        resp.text = "page body"
        resp.url = finals[url]
        return resp

    validator = SequencedValidator(timeout=1)
    validator.session.get = MagicMock(side_effect=fake_get)

    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._url_validator = validator
    discoverer._fetch_final_url_cache = {}

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(discoverer._fetch_page_text_uncached, url_a),
            pool.submit(discoverer._fetch_page_text_uncached, url_b),
        ]
        for future in futures:
            assert future.result(timeout=15)[0] is True

    assert discoverer._fetch_final_url_cache[url_a] == final_a
    assert discoverer._fetch_final_url_cache[url_b] == final_b


# ----------------------------------------------------------------------
# Redirect ledger: absent must mean "never looked", not "did not redirect".
# ----------------------------------------------------------------------


def _redirect_ledger_discoverer():
    discoverer = URLDiscoverer.__new__(URLDiscoverer)
    discoverer._fetch_final_url_cache = {}
    return discoverer


def test_redirect_state_distinguishes_never_looked_from_no_redirect():
    """The three states the record has to be able to hold. Before this, a
    URL that was fetched and did not redirect was written nowhere, so it was
    indistinguishable from one nothing ever fetched."""
    from generator.url_discovery import (
        LINK_REDIRECT_FOLLOWED,
        LINK_REDIRECT_NONE,
        LINK_REDIRECT_UNCHECKED,
    )

    discoverer = _redirect_ledger_discoverer()

    never = "https://www.fs.usda.gov/recarea/carson/recarea/?recid=44248"
    stayed = "https://www.nps.gov/zion/planyourvisit/kolob-canyons.htm"
    moved = "https://www.fs.usda.gov/recarea/carson/recarea/?recid=99999"
    moved_to = "https://www.fs.usda.gov/r03/carson/recreation"

    discoverer._record_link_redirect(stayed, stayed)
    discoverer._record_link_redirect(moved, moved_to)

    assert discoverer.link_redirect_state(never) == LINK_REDIRECT_UNCHECKED
    assert discoverer.link_redirect_state(stayed) == LINK_REDIRECT_NONE
    assert discoverer.link_redirect_state(moved) == LINK_REDIRECT_FOLLOWED


def test_a_fetch_that_does_not_redirect_records_that_it_did_not():
    """The write side of the same fact, through the real fetch path: a
    response whose final URL equals the requested one is an observation, and
    the old `final_url != url` write guard threw it away."""
    from generator.url_discovery import LINK_REDIRECT_NONE
    from generator.url_validator import URLValidator

    url = "https://www.nps.gov/zion/planyourvisit/kolob-canyons.htm"

    def fake_get(requested, **kwargs):
        resp = MagicMock()
        resp.status_code = 200
        resp.text = "Kolob Canyons Viewpoint"
        resp.url = requested
        return resp

    validator = URLValidator(timeout=1)
    validator.session.get = MagicMock(side_effect=fake_get)

    discoverer = _redirect_ledger_discoverer()
    discoverer._url_validator = validator

    ok, status, _text = discoverer._fetch_page_text_uncached(url)

    assert (ok, status) == (True, 200)
    assert discoverer.link_redirect_state(url) == LINK_REDIRECT_NONE
    assert discoverer._fetch_final_url_cache[url] == url


def test_redirect_state_is_unknown_when_the_fetch_reported_no_final_url():
    """A fetch can succeed and still observe no destination -- the validator's
    per-thread final-URL slot is cleared at the top of every call, so a path
    that never sets it leaves it empty. Empty is `unchecked`; recording it as
    `not_redirected` would be the same unmeasured claim, made by the fix."""
    from generator.url_discovery import LINK_REDIRECT_UNCHECKED

    url = "https://www.opentable.com/r/some-restaurant"

    class SilentValidator:
        _last_final_url = ""

        def get_text(self, requested, timeout=None):
            return True, 200, "page body"

    discoverer = _redirect_ledger_discoverer()
    discoverer._url_validator = SilentValidator()

    ok, status, _text = discoverer._fetch_page_text_uncached(url)

    assert (ok, status) == (True, 200)
    assert discoverer.link_redirect_state(url) == LINK_REDIRECT_UNCHECKED
    assert url not in discoverer._fetch_final_url_cache


def test_persistent_cache_entry_without_a_final_url_loads_as_unknown(tmp_path) -> None:
    """Backward compatibility, and the crux of the change. An entry written
    before the redirect ledger existed carries `final_url: ""` for BOTH "it
    did not redirect" and "nothing observed one" -- the writer below is the
    real one, driven with an empty ledger, so the payload is exactly the
    shape already on disk. It must load as `unchecked`: reading it as
    `not_redirected` would assert something nobody measured."""
    from generator.url_discovery import LINK_REDIRECT_UNCHECKED

    url = "https://www.fs.usda.gov/recarea/carson/recarea/?recid=44248"
    cache_path = tmp_path / "persistent_cache.json"

    writer = URLDiscoverer.__new__(URLDiscoverer)
    writer._persistent_cache_enabled = True
    writer._persistent_cache_path = str(cache_path)
    writer._persistent_cache_dirty = True
    writer._request_cache_lock = Lock()
    writer._page_text_cache = {url: (True, 200, "Carson National Forest recreation")}
    writer._fetch_final_url_cache = {}
    writer._save_persistent_caches()

    payload = json.loads(cache_path.read_text(encoding="utf-8"))
    assert payload["page_text_results"][url]["final_url"] == ""

    reader = URLDiscoverer.__new__(URLDiscoverer)
    reader._persistent_cache_enabled = True
    reader._persistent_cache_path = str(cache_path)
    reader._page_text_cache = {}
    reader._fetch_final_url_cache = {}
    reader._load_persistent_caches()

    assert reader._page_text_cache[url][1] == 200
    assert url not in reader._fetch_final_url_cache
    assert reader.link_redirect_state(url) == LINK_REDIRECT_UNCHECKED


def test_persistent_cache_round_trips_a_measured_non_redirect(tmp_path) -> None:
    """A measured non-redirect must survive a save/load round trip, and must
    be persisted as the URL itself rather than as "" -- which is both what
    makes it distinguishable from an old entry on the next load, and what
    keeps an older reader of the same file sane: its `final_url ==
    original_url` branch already fails open on exactly this value."""
    from generator.url_discovery import LINK_REDIRECT_FOLLOWED, LINK_REDIRECT_NONE

    stayed = "https://www.nps.gov/zion/planyourvisit/kolob-canyons.htm"
    moved = "https://www.fs.usda.gov/recarea/carson/recarea/?recid=44248"
    moved_to = "https://www.fs.usda.gov/r03/carson/recreation"
    cache_path = tmp_path / "persistent_cache.json"

    writer = URLDiscoverer.__new__(URLDiscoverer)
    writer._persistent_cache_enabled = True
    writer._persistent_cache_path = str(cache_path)
    writer._persistent_cache_dirty = True
    writer._request_cache_lock = Lock()
    writer._page_text_cache = {
        stayed: (True, 200, "Kolob Canyons Viewpoint"),
        moved: (True, 200, "Carson National Forest recreation"),
    }
    writer._fetch_final_url_cache = {}
    writer._record_link_redirect(stayed, stayed)
    writer._record_link_redirect(moved, moved_to)
    writer._save_persistent_caches()

    payload = json.loads(cache_path.read_text(encoding="utf-8"))
    assert payload["page_text_results"][stayed]["final_url"] == stayed
    assert payload["page_text_results"][moved]["final_url"] == moved_to

    reader = URLDiscoverer.__new__(URLDiscoverer)
    reader._persistent_cache_enabled = True
    reader._persistent_cache_path = str(cache_path)
    reader._page_text_cache = {}
    reader._fetch_final_url_cache = {}
    reader._load_persistent_caches()

    assert reader.link_redirect_state(stayed) == LINK_REDIRECT_NONE
    assert reader.link_redirect_state(moved) == LINK_REDIRECT_FOLLOWED
    assert reader._fetch_final_url_cache[moved] == moved_to


def test_redirect_gate_verdict_is_unchanged_for_every_redirect_state():
    """The whole point of making the third state representable is that it
    changes no verdict. `unchecked` and `not_redirected` both clear the gate
    -- for different reasons, but with the same outcome as before -- and only
    a recorded redirect to a page that has lost the item can reject.

    The `not_redirected` row is the one that bites: the page text does not
    mention the item, so a gate that judged a non-redirect as though it were
    a redirect would reject a URL that resolved to itself."""
    item = "Poshuouinge Pueblo Ruins"
    dest = "Espanola"
    original = "https://www.fs.usda.gov/recarea/carson/recarea/?recid=44248"
    hub = "https://www.fs.usda.gov/r03/carson/recreation"
    hub_text = "Carson National Forest recreation opportunities and seasonal closures."

    discoverer = _redirect_ledger_discoverer()
    tokens = discoverer._significant_tokens(item)

    # Nothing ever looked: no evidence, so no rejection.
    assert discoverer._redirect_target_lacks_item_relevance(
        original, item, dest, tokens, "attraction", hub_text
    ) == ""

    # Fetched, and it resolved to itself: nothing to judge, so no rejection,
    # even though the page text never names the item.
    discoverer._record_link_redirect(original, original)
    assert discoverer._redirect_target_lacks_item_relevance(
        original, item, dest, tokens, "attraction", hub_text
    ) == ""

    # Fetched, and it went somewhere that has lost the item: rejected, exactly
    # as before.
    discoverer._record_link_redirect(original, hub)
    assert discoverer._redirect_target_lacks_item_relevance(
        original, item, dest, tokens, "attraction", hub_text
    ) == hub

    # Fetched, went elsewhere, and the destination still names the item: kept.
    moved_text = "Poshuouinge Pueblo Ruins trailhead, Carson National Forest."
    assert discoverer._redirect_target_lacks_item_relevance(
        original, item, dest, tokens, "attraction", moved_text
    ) == ""


class TestTheDestinationsOwnPageIsNotAnItemsLink:
    """GH #59: "Red Canyon" linked to a generic Capitol Reef listing page.

    Closed as completed 2026-08-14 with no comments and no linked commit, and
    still reproducing on v3 2026-09-08: one live page
    (utah.com/destinations/national-parks/capitol-reef-national-park/) was
    accepted as the link for four different attractions.

    **Why the rule reads the URL and not the page.** A destination's overview
    page legitimately talks about the things inside it -- fetched live, that
    Capitol Reef page contains "hickman bridge", "cathedral valley" and
    "sulphur creek". Any gate reading the prose is right that the page is about
    the item and still wrong to accept it. The page is *about* the destination
    and mentions the item the way a contents page mentions a chapter.
    """

    P = staticmethod(URLDiscoverer._is_the_destinations_own_page)
    DEST = "Capitol Reef National Park"
    GENERIC = "https://www.utah.com/destinations/national-parks/capitol-reef-national-park/"

    def test_the_destinations_page_is_not_a_specific_attractions_link(self):
        for item in ("Red Canyon", "Hickman Bridge Trail", "Cathedral Valley", "Sulphur Creek"):
            assert self.P(self.GENERIC, item, self.DEST), item

    def test_a_real_page_for_the_item_is_not_caught(self):
        """The second condition is what keeps this rule from eating the pages
        it exists to prefer -- the item's own words are in the URL."""
        assert not self.P(
            "https://www.utah.com/destinations/national-parks/capitol-reef-national-park/hickman-bridge/",
            "Hickman Bridge Trail", self.DEST,
        )

    def test_an_item_named_for_its_park_is_judged_on_what_it_adds(self):
        """"Capitol Reef Visitor Center" is asked for "visitor" or "center",
        not for "capitol" -- otherwise the park's own name would satisfy the
        rule for everything inside it."""
        assert self.P(self.GENERIC, "Capitol Reef Visitor Center", self.DEST)

    def test_an_item_that_IS_the_destination_keeps_the_page(self):
        """No word of its own to look for, so the second condition would hold
        vacuously. For an attraction called simply "Capitol Reef" the park's
        page is the right answer, not the wrong one."""
        assert not self.P(self.GENERIC, "Capitol Reef", self.DEST)

    def test_a_url_that_does_not_name_the_destination_is_untouched(self):
        """Both conditions are required. Without the first, any URL lacking the
        item's words would be rejected -- including opaque but correct ones.

        This test used nps.gov/zion/index.htm as its second example. It is no
        longer untouched: nps.gov scopes pages by park code, the code now
        stands in for the destination's name, and a park's home page is not a
        link for a single thing inside it -- this park or another. See
        test_an_nps_park_code_stands_in_for_the_destinations_name.
        """
        assert not self.P("https://example.com/attractions/12345", "Red Canyon", self.DEST)
        assert not self.P("https://www.recreation.gov/camping/campgrounds/234059", "Red Canyon", self.DEST)

    def test_the_rejection_is_a_labelled_retention_exit(self):
        from generator.url_discovery import _RETENTION_EXIT_LABELS
        assert 31 in _RETENTION_EXIT_LABELS

    # ── Beyond Utah ──────────────────────────────────────────────────────────
    #
    # Everything above used DEST = "Capitol Reef National Park". Real
    # manifests say "Capitol Reef National Park, Utah" -- and the rule only
    # worked on that because `utah` is in _significant_tokens' stoplist. For
    # any state outside it the state token was required in the URL path,
    # where generic pages never put it.

    def test_the_manifest_shaped_name_still_catches_59(self):
        for item in ("Red Canyon", "Hickman Bridge Trail", "Cathedral Valley", "Sulphur Creek"):
            assert self.P(self.GENERIC, item, "Capitol Reef National Park, Utah"), item

    @pytest.mark.parametrize(
        "dest, item, generic",
        [
            # Each URL returned HTTP 200 on 2026-09-12, found by searching for
            # a visitor guide to the park. #120's rule missed all of them.
            (
                "Mount Rainier National Park, Washington",
                "Skyline Trail",
                "https://wheatlesswanderlust.com/things-to-do-mount-rainier-national-park/",
            ),
            (
                "Mount Rainier National Park, Washington",
                "Skyline Trail",
                "https://parktrust.org/blog/mount-rainier-things-to-do-history-visitor-guide/",
            ),
            (
                "Crater Lake National Park, Oregon",
                "Rim Village",
                "https://www.travelonthereg.com/crater-lake-national-park-itinerary/",
            ),
            (
                "Yosemite National Park, California",
                "Mist Trail",
                "https://www.yosemite.com/official-guide-for-yosemite-first-timers/",
            ),
            (
                "Yosemite National Park, California",
                "Mist Trail",
                "https://theazhikeaholics.com/yosemite-national-park/",
            ),
        ],
    )
    def test_a_state_outside_the_stoplist_does_not_hide_the_destinations_page(self, dest, item, generic):
        """Live guides to the whole park. The path had to contain `washington`,
        `oregon` or `california`, and no guide puts its state there."""
        assert self.P(generic, item, dest)

    def test_the_real_page_is_still_kept_outside_utah(self):
        """URL shape only -- the item's own words in the path."""
        assert not self.P(
            "https://www.nps.gov/mora/planyourvisit/skyline-trail.htm",
            "Skyline Trail", "Mount Rainier National Park, Washington",
        )

    def test_an_item_is_never_judged_on_its_state(self):
        """URL shapes, not live pages: this pins the token arithmetic.

        The item's words are still reduced by the WHOLE destination string.
        "Oregon Vortex" is asked for `vortex`; if `oregon` stayed an item word,
        every URL containing it -- half the state's -- would count as naming
        the item, and the rule would never fire."""
        dest = "Gold Hill, Oregon"
        assert self.P("https://www.traveloregon.com/places-to-go/cities/gold-hill/", "Oregon Vortex", dest)
        assert not self.P("https://www.oregonvortex.com/gold-hill/visit", "Oregon Vortex", dest)

    # ── nps.gov ──────────────────────────────────────────────────────────────

    @pytest.mark.parametrize(
        "listing",
        [
            "https://www.nps.gov/care/planyourvisit/trailguide.htm",
            "https://www.nps.gov/care/planyourvisit/hiking.htm",
            "https://www.nps.gov/care/planyourvisit/thingstodo.htm",
            "https://www.nps.gov/care/planyourvisit/day-hikes.htm",
        ],
    )
    def test_an_nps_park_code_stands_in_for_the_destinations_name(self, listing):
        """NPS never puts the park's name in a path -- /care/ is Capitol Reef.
        These park-wide listings passed both this rule and
        _is_obviously_generic_url; trailguide.htm was confirmed live, listing
        every trail in the park, and KEPT for Hickman Bridge Trail."""
        assert self.P(listing, "Hickman Bridge Trail", "Capitol Reef National Park, Utah")

    def test_a_park_home_is_not_an_items_link_in_any_park(self):
        assert self.P("https://www.nps.gov/zion/index.htm", "Red Canyon", "Capitol Reef National Park, Utah")

    @pytest.mark.parametrize(
        "item, specific",
        [
            ("Sulphur Creek", "https://www.nps.gov/care/planyourvisit/sulphur-creek.htm"),
            ("Hickman Bridge Trail", "https://www.nps.gov/care/planyourvisit/hickmanbridge.htm"),
            ("Cathedral Valley", "https://www.nps.gov/thingstodo/hike-cathedral-valley.htm"),
            ("Sulphur Creek", "https://www.nps.gov/places/sulphur-creek-trailhead.htm"),
        ],
    )
    def test_nps_pages_about_the_item_are_kept(self, item, specific):
        """Under a park code, (2) still reads the URL for the item's words, and
        matches inside unhyphenated slugs. /thingstodo/ and /places/ are
        service-wide sections, never park-scoped -- that is where NPS keeps
        its pages about single things."""
        assert not self.P(specific, item, "Capitol Reef National Park, Utah")

    def test_a_four_letter_nps_section_is_not_mistaken_for_a_park(self):
        assert not URLDiscoverer._is_nps_park_scoped_path("www.nps.gov", "/news/release.htm")
        assert not URLDiscoverer._is_nps_park_scoped_path("www.nps.gov", "/thingstodo/hike.htm")
        assert URLDiscoverer._is_nps_park_scoped_path("www.nps.gov", "/care/planyourvisit/hiking.htm")

    def test_the_items_own_site_is_not_the_destinations_page(self):
        """Live on the published Pacific Crest Trail guide, and the one false
        positive reading the place name produced: the brewery's own site.
        `dru` and `bru` are under the four-letter token minimum; `brewery` is
        not in the URL. The name survives, run together, in the domain."""
        assert not self.P(
            "https://www.drubru.com/snoqualmie-pass/", "Dru Bru Brewery", "Snoqualmie Pass, Washington"
        )

    def test_a_host_match_needs_two_joined_words(self):
        """One word in the host is already condition (2)'s job; the joined-run
        check exists only for names whose words are too short to be tokens."""
        assert URLDiscoverer._host_carries_the_items_name("www.drubru.com", "Dru Bru Brewery")
        assert not URLDiscoverer._host_carries_the_items_name("www.cascadeloop.com", "Espresso Chalet")
        assert not URLDiscoverer._host_carries_the_items_name("www.abcd.com", "A B C D")

    def test_park_codes_are_only_read_on_nps(self):
        assert not URLDiscoverer._is_nps_park_scoped_path("example.com", "/care/planyourvisit/hiking.htm")
        assert not URLDiscoverer._is_nps_park_scoped_path("notnps.gov", "/care/hiking.htm")

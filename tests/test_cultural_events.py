from generator.cultural_events import CulturalEventsDiscoverer


def _discoverer() -> CulturalEventsDiscoverer:
    # Use helper methods without initializing network clients.
    return CulturalEventsDiscoverer.__new__(CulturalEventsDiscoverer)


def test_local_tip_removed_when_weekday_outside_itinerary() -> None:
    d = _discoverer()
    result = {
        "has_events": False,
        "honest_assessment": "Quiet scene with visitor center programs.",
        "local_tip": "Saturday artisan market on Main Street.",
    }
    sanitized = d._sanitize_local_tip_by_itinerary_days(result, "October 7-9, 2026")
    assert "local_tip" not in sanitized


def test_local_tip_kept_when_weekday_inside_itinerary() -> None:
    d = _discoverer()
    result = {
        "has_events": False,
        "honest_assessment": "Quiet scene with visitor center programs.",
        "local_tip": "Friday live music at the town hall.",
    }
    sanitized = d._sanitize_local_tip_by_itinerary_days(result, "October 7-9, 2026")
    assert sanitized.get("local_tip") == "Friday live music at the town hall."


def test_local_tip_removed_when_dates_unparseable_and_weekday_specific() -> None:
    d = _discoverer()
    result = {
        "has_events": False,
        "honest_assessment": "Quiet scene with visitor center programs.",
        "local_tip": "Sunday market near the visitor center.",
    }
    sanitized = d._sanitize_local_tip_by_itinerary_days(result, "Early October")
    assert "local_tip" not in sanitized


def test_local_tip_kept_when_not_weekday_specific() -> None:
    d = _discoverer()
    result = {
        "has_events": False,
        "honest_assessment": "Quiet scene with visitor center programs.",
        "local_tip": "Check ranger talks posted at the visitor center desk.",
    }
    sanitized = d._sanitize_local_tip_by_itinerary_days(result, "Early October")
    assert sanitized.get("local_tip") == "Check ranger talks posted at the visitor center desk."

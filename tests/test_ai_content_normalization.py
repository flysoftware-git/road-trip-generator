from generator.ai_content import AIContentGenerator


def _gen() -> AIContentGenerator:
    return AIContentGenerator.__new__(AIContentGenerator)


def test_dedupes_similar_attraction_names() -> None:
    g = _gen()
    items = [
        {"name": "Kolob Canyons Road", "description": "Scenic canyon drive.", "must_see": False},
        {"name": "Kolb Canyons", "description": "Overview of Kolob district.", "must_see": True},
    ]

    deduped = g._normalize_attractions(items)

    assert len(deduped) == 1
    assert "kol" in deduped[0]["name"].lower()


def test_schedule_injects_arrival_and_departure_context() -> None:
    g = _gen()
    schedule = [
        {
            "day_label": "Day 1",
            "periods": [
                {"period": "Morning", "summary": "Start at sunrise viewpoints."},
                {"period": "Afternoon", "summary": "Explore canyon trails."},
            ],
        },
        {
            "day_label": "Day 2",
            "periods": [
                {"period": "Morning", "summary": "Hit key overlooks."},
                {"period": "Evening", "summary": "Dinner and sunset."},
            ],
        },
    ]

    updated = g._inject_travel_realism(
        schedule,
        {"drive_time": "2 hrs 15 min", "route_summary": "US-89 to UT-12"},
        "Zion National Park",
        "Capitol Reef National Park",
    )

    first_text = updated[0]["periods"][0]["summary"].lower()
    last_text = updated[-1]["periods"][-1]["summary"].lower()

    assert "arrival day" in first_text
    assert "2 hrs 15 min" in first_text
    assert "onward drive to capitol reef national park" in last_text


def test_infer_day_count_single_day_date() -> None:
    g = _gen()
    assert g._infer_day_count("October 17, 2026") == 1


def test_expand_days_truncates_to_single_day() -> None:
    g = _gen()
    days = [
        {"day_label": "Day 1", "periods": [{"period": "Morning", "summary": "A"}]},
        {"day_label": "Day 2", "periods": [{"period": "Afternoon", "summary": "B"}]},
        {"day_label": "Day 3", "periods": [{"period": "Evening", "summary": "C"}]},
    ]

    trimmed = g._expand_days(days, 1)
    assert len(trimmed) == 1
    assert trimmed[0]["day_label"] == "Day 1"


def test_inject_travel_realism_no_leading_colon_when_arrival_already_present() -> None:
    g = _gen()
    days = [{
        "day_label": "Day 1",
        "periods": [{"period": "Morning", "summary": "Arrive at Bryce Canyon and settle in."}],
    }, {
        "day_label": "Day 2",
        "periods": [{"period": "Evening", "summary": "Dinner and sunset."}],
    }]

    updated = g._inject_travel_realism(
        days,
        {"drive_time": "1 hr 45 min"},
        "Zion National Park",
        "Capitol Reef National Park",
    )
    first_summary = updated[0]["periods"][0]["summary"]
    assert not first_summary.lstrip().startswith(":")
    assert "allow about 1 hr 45 min of inbound driving" in first_summary.lower()


def test_remove_enroute_stops_from_attractions() -> None:
    g = _gen()
    attractions = [
        {"name": "Goblin Valley State Park", "type": "attraction"},
        {"name": "Capitol Reef Scenic Drive", "type": "scenic"},
    ]
    getting_here = {
        "en_route_stops": [
            {"name": "Goblin Valley"},
            {"name": "Hanksville"},
        ]
    }

    filtered = g._remove_enroute_stops_from_attractions(attractions, getting_here)
    names = [a["name"] for a in filtered]

    assert "Goblin Valley State Park" not in names
    assert "Capitol Reef Scenic Drive" in names

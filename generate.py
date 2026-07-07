#!/usr/bin/env python3
"""
Road Trip Itinerary Generator — main entry point.

Usage:
    python generate.py                    # interactive mode
    python generate.py --output my.html   # specify output file
    python generate.py --help

Requires:
    OPENAI_API_KEY set in environment (or in a .env file).
"""
import argparse
import os
import sys
from datetime import datetime
from pathlib import Path

# Load .env if python-dotenv is available
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from generator.cli import collect_itinerary, print_progress, print_success, print_error
from generator.ai_client import (
    generate_destination_content,
    generate_drive_card,
    generate_trip_title,
)
from generator.html_builder import build_html
from generator.models import (
    Attraction, Restaurant, ScheduleItem, DaySchedule, DriveCard
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate a styled road-trip itinerary HTML file using AI."
    )
    parser.add_argument(
        "--output", "-o",
        type=str,
        default="",
        help="Output HTML filename (default: auto-generated from title)",
    )
    parser.add_argument(
        "--model",
        type=str,
        default="gpt-4o",
        help="OpenAI model to use (default: gpt-4o)",
    )
    return parser.parse_args()


def destination_context(destinations) -> str:
    names = [d.name for d in destinations]
    if len(names) <= 1:
        return ""
    return f"This is stop #{names.index(names[0]) + 1} on a road trip visiting: {', '.join(names)}."


def build_destination_from_ai(dest, all_destinations, model: str):
    """Call AI and populate a Destination object with full content."""
    context = destination_context(all_destinations)
    print_progress(f"Generating content for {dest.name}…")

    data = generate_destination_content(dest.name, dest.nights, context)

    # Location subtitle
    dest.location = data.get("location_subtitle", f"{dest.name} · {dest.nights} nights")

    # Scenic drive descriptions lookup
    scenic_descs = data.get("scenic_drive_descriptions", {})

    # Attractions
    dest.attractions = []
    for a in data.get("attractions", []):
        attr = Attraction(
            name=a.get("name", ""),
            emoji=a.get("emoji", "🏔️"),
            badge_type=a.get("badge_type", "activity"),
            description=a.get("description", ""),
            link=a.get("link", "#"),
            is_scenic_drive=bool(a.get("is_scenic_drive", False)),
        )
        # Attach scenic description as extra attribute for collection later
        attr._scenic_desc = scenic_descs.get(attr.name, "")
        dest.attractions.append(attr)

    # Restaurants
    dest.restaurants = []
    for r in data.get("restaurants", []):
        dest.restaurants.append(Restaurant(
            name=r.get("name", ""),
            cuisine=r.get("cuisine", ""),
            price=r.get("price", "$$"),
            description=r.get("description", ""),
            maps_link=r.get("maps_link", "#"),
            reserve=bool(r.get("reserve", False)),
        ))

    # Daily schedule
    dest.schedule = []
    for day_data in data.get("schedule", []):
        items = [
            ScheduleItem(
                time_of_day=i.get("time_of_day", "morning"),
                description=i.get("description", ""),
            )
            for i in day_data.get("items", [])
        ]
        dest.schedule.append(DaySchedule(
            day_number=day_data.get("day_number", 1),
            items=items,
        ))

    return scenic_descs


def build_drive_card_from_ai(origin_name: str, dest) -> None:
    """Call AI to generate drive card and attach it to dest."""
    print_progress(f"Generating drive info: {origin_name} → {dest.name}…")
    try:
        data = generate_drive_card(origin_name, dest.name)
        dest.drive_card = DriveCard(
            origin=origin_name,
            destination=dest.name,
            miles=int(data.get("miles", 0)),
            drive_hours=data.get("drive_hours", ""),
            maps_url=data.get("maps_url", "#"),
            enroute_stops=data.get("enroute_stops", []),
        )
    except Exception as e:
        print_error(f"Could not generate drive card ({e}); skipping.")
        dest.drive_card = None


def make_output_path(title: str, override: str) -> Path:
    if override:
        return Path(override)
    slug = title.lower().replace(" ", "-").replace("/", "-")
    slug = "".join(c for c in slug if c.isalnum() or c == "-")
    timestamp = datetime.now().strftime("%Y%m%d-%H%M")
    return Path("output") / f"{slug}-{timestamp}.html"


def main():
    args = parse_args()

    # Check API key early
    if not os.getenv("OPENAI_API_KEY"):
        print_error("OPENAI_API_KEY is not set.")
        print("  Copy .env.example to .env and add your OpenAI API key, then re-run.")
        sys.exit(1)

    # Collect destinations from user
    itinerary = collect_itinerary()

    all_destinations = itinerary.destinations
    all_scenic_descs: dict = {}

    # Generate content for each destination
    for i, dest in enumerate(all_destinations):
        scenic = build_destination_from_ai(dest, all_destinations, args.model)
        all_scenic_descs.update(scenic)
        print_success(f"{dest.name} content ready.")

        # Generate drive card (from previous destination to this one)
        if i > 0:
            origin = all_destinations[i - 1].name
            build_drive_card_from_ai(origin, dest)
            print_success(f"Drive card: {origin} → {dest.name} ready.")

    # Auto-generate trip title if user left it as default
    if itinerary.title == "My Road Trip Itinerary" and len(all_destinations) > 1:
        print_progress("Generating trip title…")
        try:
            itinerary.title = generate_trip_title([d.name for d in all_destinations])
        except Exception:
            pass   # Keep the default

    # Build HTML
    print_progress("Building HTML file…")
    html_content = build_html(itinerary, all_scenic_descs)

    # Write output
    output_path = make_output_path(itinerary.title, args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html_content, encoding="utf-8")

    print_success(f"Itinerary saved to: {output_path}")
    print()
    print(f"  Open in your browser:  file://{output_path.resolve()}")
    print(f"  To print:              open the file, use Ctrl+P / Cmd+P")
    print()


if __name__ == "__main__":
    main()

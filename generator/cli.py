from generator.models import Trip, DayPlan
from generator.ai_client import get_recommendations
from generator.ai_client import maps_link_for

from datetime import datetime, timedelta

def prompt_user_for_trip():
    print("\n=== Road Trip Generator ===\n")

    name = input("Trip name: ").strip()

    destinations = []
    print("\nEnter destinations (one per line). Leave blank to finish.")
    while True:
        d = input("Destination: ").strip()
        if not d:
            break
        destinations.append(d)

    start_date = input("\nStart date (YYYY-MM-DD): ").strip()
    end_date = input("End date (YYYY-MM-DD): ").strip()

    planning_links = []
    print("\nEnter planning links (one per line). Leave blank to finish.")
    while True:
        link = input("Planning link: ").strip()
        if not link:
            break
        planning_links.append(link)

    start = datetime.fromisoformat(start_date)
    end = datetime.fromisoformat(end_date)
    days_count = (end - start).days + 1

    days = []
    for i in range(days_count):
        date = start + timedelta(days=i)
        destination = destinations[i % len(destinations)]

        recs = get_recommendations(destination)

        days.append(
            DayPlan(
                date=date.strftime("%A, %B %d"),
                destination=destination,
                attractions=recs["top_attractions"],
                restaurants=recs["restaurants"],
                maps_link=maps_link_for(destination)
            )
        )

    return Trip(
        name=name,
        start_date=start_date,
        end_date=end_date,
        destinations=destinations,
        planning_links=planning_links,
        days=days
    )

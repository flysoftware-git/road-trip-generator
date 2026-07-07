"""
Data models for the road trip itinerary generator.
"""
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Attraction:
    name: str
    badge_type: str          # hike-easy | hike-moderate | hike-strenuous | scenic | cultural | mustsee | activity
    description: str
    link: str                # AllTrails URL for hikes, Wikipedia/Google Maps for others
    emoji: str = "🏔️"
    is_scenic_drive: bool = False   # True → rendered as modal button, not anchor


@dataclass
class Restaurant:
    name: str
    cuisine: str
    price: str               # $ | $$ | $$$ | $$$$
    description: str
    maps_link: str
    reserve: bool = False


@dataclass
class ScheduleItem:
    time_of_day: str         # morning | afternoon | evening
    description: str


@dataclass
class DaySchedule:
    day_number: int
    items: list[ScheduleItem] = field(default_factory=list)


@dataclass
class DriveCard:
    origin: str
    destination: str
    miles: int
    drive_hours: str         # e.g. "~2.5 hrs"
    maps_url: str
    enroute_stops: list[dict] = field(default_factory=list)   # [{emoji, name, description}]


@dataclass
class Destination:
    name: str                         # Display name, e.g. "Zion National Park"
    location: str                     # e.g. "Utah · 2 nights"
    nights: int
    notion_link: Optional[str] = None
    attractions: list[Attraction] = field(default_factory=list)
    restaurants: list[Restaurant] = field(default_factory=list)
    schedule: list[DaySchedule] = field(default_factory=list)
    drive_card: Optional[DriveCard] = None   # Drive FROM previous destination TO this one


@dataclass
class Itinerary:
    title: str
    destinations: list[Destination] = field(default_factory=list)

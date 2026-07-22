"""
html_assembler.py — Assemble final index.html from frozen v2.5 template.

Steps:
  1. Verify template SHA-256 checksum (hard fail on mismatch)
  2. Build per-destination section HTML strings
  3. Replace template placeholders with generated content
  4. Inject attribution block at page bottom

IMPORTANT: Uses Python string assembly — no Jinja2, no DOM parsing.
Template placeholders use the pattern <!--PLACEHOLDER_NAME-->.
"""
from __future__ import annotations
import html as html_escape
import hashlib, json, logging
from datetime import datetime
from pathlib import Path
import re
from typing import Any
from urllib.parse import quote

logger = logging.getLogger(__name__)
TEMPLATE_PATH = Path(__file__).parent.parent / "templates" / "v2.5_template.html"
CHECKSUM_PATH = Path(__file__).parent.parent / "templates" / "checksums.txt"

def _path_to_file_url(path_str: str) -> str:
    """Convert a local filesystem path to a file:// URL.
    
    Handles both absolute and relative paths.
    Example: 'output\\images\\xyz.jpg' or '/full/path/to/xyz.jpg' → 'file:///C:/...' or 'file://localhost/C:/...'
    """
    p = Path(path_str).resolve()  # Resolve to absolute path
    # Convert to file:// URL
    url = p.as_uri()
    return url

def sanitize_dest_id(name: str) -> str:
    """
    Convert destination names into validator-friendly IDs.
    Examples:
      'Zion National Park' → 'zion'
      'Bryce Canyon National Park' → 'bryce'
      'Capitol Reef National Park' → 'capitolreef'
      'Pagosa Springs' → 'pagosa'
      'Santa Fe' → 'santafe'
    """
    name = name.lower()
    for remove in ["national park", "state park", "park", ","]:
        name = name.replace(remove, "")
    name = name.replace("-", " ")
    name = "".join(ch for ch in name if ch.isalnum() or ch == " ")
    return "".join(name.split())
    

def sanitize_drive_key(title: str) -> str:
    """
    Convert scenic drive titles into validator-friendly keys.
    Examples:
      'Zion Canyon Scenic Drive' → 'zion_canyon_scenic_drive'
      'Free Gondola to Mountain Village' → 'free_gondola_to_mountain_village'
    """
    title = title.lower()
    title = title.replace("/", " ")
    title = "".join(ch for ch in title if ch.isalnum() or ch == " ")
    return "_".join(title.split())


def _verify_checksum(template_text: str) -> None:
    """Hard fail if template SHA-256 doesn't match stored value."""
    if not CHECKSUM_PATH.exists():
        raise FileNotFoundError(f"Checksum file not found: {CHECKSUM_PATH}")
    stored = CHECKSUM_PATH.read_text(encoding="utf-8").strip().split()[0]
    actual = hashlib.sha256(template_text.encode("utf-8")).hexdigest()
    if actual != stored:
        raise RuntimeError(
            f"Template checksum mismatch!\n"
            f"  Expected: {stored}\n"
            f"  Actual:   {actual}\n"
            "The frozen template has been modified. Restore it from git."
        )


class HTMLAssembler:
    def sanitize_dest_id(self, name: str) -> str:
        """
        Convert destination names into validator-friendly IDs.
        Examples:
          'Zion National Park' → 'zion'
          'Bryce Canyon National Park' → 'bryce'
          'Capitol Reef National Park' → 'capitolreef'
          'Pagosa Springs' → 'pagosa'
          'Santa Fe' → 'santafe'
        """
        name = name.lower()
        for remove in ["national park", "state park", "park", ","]:
            name = name.replace(remove, "")
        name = name.replace("-", " ")
        name = "".join(ch for ch in name if ch.isalnum() or ch == " ")
        return "".join(name.split())

    def sanitize_drive_key(self, title: str) -> str:
        """
        Convert scenic drive titles into validator-friendly keys.
        Examples:
          'Zion Canyon Scenic Drive' → 'zion_canyon_scenic_drive'
          'Free Gondola to Mountain Village' → 'free_gondola_to_mountain_village'
        """
        title = title.lower()
        title = title.replace("/", " ")
        title = "".join(ch for ch in title if ch.isalnum() or ch == " ")
        return "_".join(title.split())

    def __init__(self, config_path: Path | str = "config.yaml") -> None:
        import yaml
        with Path(config_path).open() as f:
            self._config = yaml.safe_load(f)

    def assemble(self, trip: dict[str, Any], attribution_block: str = "") -> str:
        template_text = TEMPLATE_PATH.read_text(encoding="utf-8")
        _verify_checksum(template_text)
        logger.info("Template checksum verified ✓")

        html = template_text
        meta = trip.get("_meta", {})
        stamp = (
            f"<!-- generator_version={meta.get('generator_version', '')}; "
            f"template_version={meta.get('template_version', '')}; "
            f"provider={meta.get('llm', {}).get('provider', '')}; "
            f"model={meta.get('llm', {}).get('model', '')}; "
            f"generated_at={meta.get('generated_at_utc', '')} -->\n"
        )
        html = stamp + html

        # ── Trip-level substitutions ─────────────────────────────────────────
        meta = trip["trip"]
        html = html.replace("<!--TRIP_TITLE-->", meta["title"])
        html = html.replace("<!--THEME_COLOR-->", meta.get("theme_color", "#C0623E"))

        # ── Google Maps overview link ────────────────────────────────────────
        gmaps_url = self._build_google_maps_url(trip["destinations"], meta)
        html = html.replace("<!--GOOGLE_MAPS_URL-->", gmaps_url)

        # ── Map markers JSON ─────────────────────────────────────────────────
        markers = self._build_map_markers(trip["destinations"], meta)
        html = html.replace("'<!--MAP_MARKERS_JSON-->'", json.dumps(markers))

        # ── Nav tabs ────────────────────────────────────────────────────────
        html = html.replace("<!--NAV_TABS-->", self._build_nav_tabs(trip["destinations"], meta))

        # ── Per-destination sections ─────────────────────────────────────────
        sections_html = ""
        destinations = trip.get("destinations", [])
        departure_name = meta.get("departure", "")
        for index, dest in enumerate(destinations):
            previous_name = destinations[index - 1]["name"] if index > 0 else departure_name
            sections_html += self._build_single_section(dest, meta, previous_name)
        sections_html += self._build_packing_summary(destinations)
        html = html.replace("<!--DESTINATION_SECTIONS-->", sections_html)

        # ── var DRIVE_DESCRIPTIONS (keyed by raw title, matches template JS) ──
        drive_descriptions = self._build_drive_descriptions(trip["destinations"])
        drive_json = json.dumps(drive_descriptions, indent=2)
        html = html.replace(
            "var DRIVE_DESCRIPTIONS = {};",
            f"var DRIVE_DESCRIPTIONS = {drive_json};",
        )

        # ── Footer credit ───────────────────────────────────────────────────
        html = html.replace("<!--GENERATOR_FOOTER-->", self._build_generator_footer(trip))

        # ── Attribution block (before </body>) ──────────────────────────────
        if attribution_block:
            html = html.replace("</body>", attribution_block + "\n</body>")

        return html

    # ── Private helpers ──────────────────────────────────────────────────────

    def _build_google_maps_url(self, destinations: list[dict[str, Any]], trip_meta: dict[str, Any] | None = None) -> str:
        if not destinations:
            return ""

        trip_meta = trip_meta or {}
        departure = str(trip_meta.get("departure", "") or "").strip()
        ret = str(trip_meta.get("return", "") or "").strip()

        origin = departure or destinations[0].get("name", "")
        destination = ret or destinations[-1].get("name", "")
        if not origin or not destination:
            return ""

        stops = [d.get("name", "") for d in destinations if d.get("name")]
        if departure:
            waypoints = stops
        else:
            waypoints = stops[1:]
        if not ret and waypoints:
            waypoints = waypoints[:-1]

        params = [
            "api=1",
            f"origin={quote(origin)}",
            f"destination={quote(destination)}",
            "travelmode=driving",
        ]
        if waypoints:
            params.append("waypoints=" + quote("|".join(waypoints), safe="|"))
        return "https://www.google.com/maps/dir/?" + "&".join(params)

    def _build_map_markers(self, destinations: list[dict[str, Any]], trip_meta: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """Build Leaflet stops array in {c:[lat,lng], mo, dy, name} format."""
        import re
        trip_meta = trip_meta or {}
        result = []

        if trip_meta.get("departure") and trip_meta.get("departure_lat") and trip_meta.get("departure_lng"):
            result.append({
                "c": [trip_meta.get("departure_lat"), trip_meta.get("departure_lng")],
                "mo": "DEP",
                "dy": "",
                "name": str(trip_meta.get("departure"))[:24],
            })

        for d in destinations:
            dates = d.get("dates", "")
            # Extract month abbrev and start day from e.g. "October 7-9, 2026"
            mo_match = re.match(r'([A-Za-z]+)\s+(\d+)', dates)
            mo = mo_match.group(1)[:3] if mo_match else ""
            dy = mo_match.group(2) if mo_match else ""
            short = d["name"].replace(" National Park", "").replace(" State Park", "")
            result.append({"c": [d.get("lat", 0), d.get("lng", 0)], "mo": mo, "dy": dy, "name": short})

        if trip_meta.get("return") and trip_meta.get("return_lat") and trip_meta.get("return_lng"):
            result.append({
                "c": [trip_meta.get("return_lat"), trip_meta.get("return_lng")],
                "mo": "RET",
                "dy": "",
                "name": str(trip_meta.get("return"))[:24],
            })

        return result

    def _build_nav_tabs(self, destinations: list[dict[str, Any]], trip_meta: dict[str, Any] | None = None) -> str:
        """Build tab-btn buttons (data-tab=section-{id}) + Google Maps link."""
        gmaps_url = self._build_google_maps_url(destinations, trip_meta)
        tabs = []
        for i, dest in enumerate(destinations):
            active = ' active' if i == 0 else ''
            dest_id = dest["id"]  # use manifest id directly
            short = dest["name"].replace(" National Park", "").replace(" State Park", "").split(",")[0].strip()
            label = f"{i + 1} · {short}"
            tabs.append(f'<button class="tab-btn{active}" data-tab="section-{dest_id}">{label}</button>')
        tabs.append(
            f'<a href="{gmaps_url}" target="_blank" rel="noopener" class="map-tab-btn">'
            f'\U0001f5fa\ufe0f Full Route Map</a>'
        )
        return "\n          ".join(tabs)

    def _build_single_section(self, dest: dict[str, Any], trip_meta: dict[str, Any], previous_name: str = "") -> str:
        import logging
        logger = logging.getLogger(__name__)
        ai = dest.get("ai_content", {})
        images = dest.get("images", [])
        events = dest.get("cultural_events", {})
        drives = dest.get("scenic_drives", [])
        logger.debug(f"_build_single_section for {dest['name']}: scenic_drives={len(drives)}")

        section_id = dest["id"]  # use manifest id directly
        section = f'<section id="section-{section_id}" class="dest-section">\n'

        # Header
        section += self._build_header(
            dest,
            images,
            dest.get("planning_links", []),
            dest.get("nps_park_code"),
        )

        # Intro note or cultural event summary belongs directly under hero
        section += self._build_intro_note(dest, events)

        # Image gallery
        section += self._build_image_gallery(images, dest["name"])

        # Expected environment
        section += self._build_environment_card(ai, dest)

        # Getting here + en-route stops
        section += self._build_getting_here(ai, dest, previous_name)

        # Attractions + scenic drives/viewpoints
        section += self._build_attractions(ai, drives)

        # Daily schedule
        section += self._build_schedule(ai, drives, dest["name"])

        # Cultural events
        section += self._build_events(events, dest["name"])

        # Dinner recommendations
        section += self._build_restaurants(ai, dest["name"])

        # Collapsible debug block (opt-in only)
        if self._config.get("render", {}).get("show_debug_block", False):
            section += self._build_debug_block(dest, trip_meta)

        section += "</section>\n"
        return section

    # ── Section builders ─────────────────────────────────────────────────────

    def _build_header(
        self,
        dest: dict[str, Any],
        images: list[dict[str, Any]],
        planning_links: list[dict[str, Any]],
        nps_code: str | None,
    ) -> str:
        hero_img = images[0]["local_path"] if images else ""
        # Convert to file:// URL for browser
        if hero_img:
            hero_img = _path_to_file_url(hero_img)
        credit = self._build_image_caption(images[0]) if images else ""
        header_links = self._build_header_links(planning_links, nps_code, dest)
        return (
            f'<div class="dest-header" style="background-image:url(\'{hero_img}\')">\n'
            f'  <div class="dest-header-actions">{header_links}</div>\n'
            f'  <h2>{dest["name"]}</h2>\n'
            f'  <p class="dates">{dest["dates"]}</p>\n'
            f'  <p class="img-credit">{credit}</p>\n'
            f'</div>\n'
        )

    def _build_header_links(
        self,
        links: list[dict[str, Any]],
        nps_code: str | None,
        dest: dict[str, Any],
    ) -> str:
        pills: list[str] = []
        weather_url = self._build_weather_url(dest)
        if weather_url:
            pills.append(
                f'<a href="{weather_url}" target="_blank" rel="noopener" class="notion-header-btn">Current Weather</a>'
            )
        if nps_code:
            pills.append(
                f'<a href="https://www.nps.gov/{nps_code}/" target="_blank" rel="noopener" class="notion-header-btn">NPS</a>'
            )
        for link in links:
            url = self._normalize_external_url(link.get("url", ""))
            if not url:
                continue
            label = html_escape.escape(link.get("label", "Plans"))
            pills.append(
                f'<a href="{self._safe_href(url)}" target="_blank" rel="noopener" class="notion-header-btn">{label}</a>'
            )
        return "".join(pills)

    def _build_intro_note(self, dest: dict[str, Any], events: dict[str, Any]) -> str:
        if not events or events.get("has_events"):
            return ""
        title = html_escape.escape(dest.get("name", ""))
        honest = html_escape.escape(events.get("honest_assessment", "").strip())
        if not honest:
            honest = (
                "Seasonal programming varies here. Check the park, town, or visitor-center calendar before you go."
            )
        local_tip = html_escape.escape(events.get("local_tip", "").strip())
        html = '<div class="card intro-note-card">\n'
        html += f'  <h3>What to Know About {title}</h3>\n'
        html += f'  <p class="intro-note-text">{honest}</p>\n'
        if local_tip:
            tip_query = quote(f"{events.get('local_tip', '')} {dest.get('name', '')}")
            tip_url = f"https://www.google.com/search?q={tip_query}"
            html += (
                f'  <p class="local-tip"><strong>Local tip:</strong> {local_tip} '
                f'<a href="{self._safe_href(tip_url)}" target="_blank" rel="noopener" class="event-link">More info</a></p>\n'
            )
        html += '</div>\n'
        return html

    def _build_weather_url(self, dest: dict[str, Any]) -> str:
        lat = dest.get("lat")
        lng = dest.get("lng")
        if not lat or not lng:
            return ""
        return f"https://forecast.weather.gov/MapClick.php?lat={lat:.4f}&lon={lng:.4f}"

    def _build_environment_card(self, ai: dict, dest: dict[str, Any]) -> str:
        env = ai.get("expected_environment", "")
        if not env:
            return ""
        # Handle dict structure: {"summary": "...", "temperature_high_f": 72, ...}
        if isinstance(env, dict):
            summary = env.get("summary", "")
            html = '<div class="card env-card">\n'
            html += '<div class="env-subcard">\n'
            html += f'<h3>🧥 What to Expect</h3>\n'
            html += f'  <p class="env-summary">{summary}</p>\n'
            weather_url = self._build_weather_url(dest)
            if weather_url:
                html += f'  <a href="{weather_url}" target="_blank" rel="noopener" class="weather-link">Current Weather</a>\n'
            html += '</div>\n'
            html += '</div>\n'
            return html
        # Fallback for string
        return f'<div class="card env-card"><p>{env}</p></div>\n'

    def _build_image_gallery(self, images: list, dest_name: str) -> str:
        """Build image gallery from discovered images."""
        if not images or len(images) <= 1:
            return ""

        gallery_images = [img for img in images[1:] if img.get("local_path")]
        if not gallery_images:
            return ""

        gallery_class = "photo-gallery photo-gallery-single" if len(gallery_images) == 1 else "photo-gallery"
        html = f'<div class="{gallery_class}">\n'
        for img in gallery_images:
            local_path = img.get("local_path", "")
            caption = self._build_image_caption(img)

            file_url = _path_to_file_url(local_path)
            dest_escaped = html_escape.escape(dest_name)
            
            html += '  <div class="photo-item">\n'
            html += f'    <a href="{file_url}" target="_blank" rel="noopener">\n'
            html += f'      <img src="{file_url}" alt="{dest_escaped}" />\n'
            html += '    </a>\n'
            
            if caption:
                html += f'    <p class="photo-caption">{caption}</p>\n'
            html += '  </div>\n'
        
        html += '</div>\n'
        return html

    def _build_image_caption(self, image: dict[str, Any]) -> str:
        credit = str(image.get("credit", "") or "").strip()
        source = str(image.get("source", "") or "").strip()
        title = str(image.get("title", "") or "").strip()
        if credit:
            return html_escape.escape(credit)
        if source and title:
            return html_escape.escape(f"{source.title()} — {title}")
        if source:
            return html_escape.escape(source.title())
        return html_escape.escape(title)

    def _build_route_gmaps_url(self, previous_name: str, dest: dict, stops: list) -> str:
        """Build Google Maps URL with names and named waypoints."""
        destination = dest.get("name", "")
        if not destination:
            return ""

        params = [f"destination={quote(destination)}", "travelmode=driving", "api=1"]
        if previous_name:
            params.append(f"origin={quote(previous_name)}")

        waypoint_names = [stop.get("name", "") for stop in stops[:8] if stop.get("name")]
        if waypoint_names:
            params.append("waypoints=" + quote("|".join(waypoint_names), safe="|"))

        return "https://www.google.com/maps/dir/?" + "&".join(params)

    def _build_getting_here(self, ai: dict, dest: dict, previous_name: str) -> str:
        gh = ai.get("getting_here", {})
        if not gh:
            return ""
        route_summary = gh.get("route_summary", "")
        distance = gh.get("distance_miles", "")
        drive_time = gh.get("drive_time", "")
        stops = gh.get("en_route_stops", [])
        route_label = ""
        if previous_name:
            route_label = f'{self._short_place_name(previous_name)} → {self._short_place_name(dest.get("name", ""))}'

        # Build Google Maps URL with named waypoints
        gmaps_url = self._build_route_gmaps_url(previous_name, dest, stops)
        
        # Icon map for stop types
        stop_icons = {
            "viewpoint": "🏜️",
            "attraction": "🏛️",
            "town": "🏘️",
            "food": "🍔",
            "scenic": "🌄",
            "natural": "🏞️",
            "historic": "🏛️",
            "hike": "🥾",
            "waterfall": "💧",
            "museum": "🏛️",
            "market": "🛍️",
        }
        
        html = '<div class="card getting-here-card getting-here-subcard">\n'
        html += '  <div class="getting-here-header">\n'
        html += '    <h3>🚗 Getting Here</h3>\n'
        if gmaps_url:
            html += f'    <a href="{gmaps_url}" target="_blank" rel="noopener" class="gmaps-link">Open in Google Maps →</a>\n'
        html += '  </div>\n'

        # Route summary with distance and time badges
        if distance and drive_time:
            html += '  <div class="route-headline-row">\n'
            if route_label:
                html += f'    <div class="route-headline">{html_escape.escape(route_label)}</div>\n'
            html += '    <div class="route-badges route-badges-inline">\n'
            html += f'      <span class="badge badge-distance">{distance} mi</span>\n'
            html += f'      <span class="badge badge-time">{drive_time}</span>\n'
            html += '    </div>\n'
            html += '  </div>\n'
        elif route_label:
            html += f'  <div class="route-headline">{html_escape.escape(route_label)}</div>\n'

        if route_summary:
            html += f'  <p class="route-summary">{route_summary}</p>\n'
        
        if stops:
            html += '  <div class="can-miss-header">🧭 CAN\'T-MISS ENROUTE</div>\n'
            html += '  <div class="en-route-stops">\n'
            for stop in stops:
                url = self._normalize_external_url(stop.get("url", ""))
                if not url:
                    query = quote(f"{stop.get('name', '')} {dest.get('name', '')}")
                    url = f"https://www.google.com/maps/search/?api=1&query={query}"
                stop_type = self._infer_stop_type(stop).lower()
                icon = stop_icons.get(stop_type, "📍")
                name_html = (
                    f'<a href="{self._safe_href(url)}" target="_blank" rel="noopener">{html_escape.escape(stop.get("name", ""))}</a>'
                    if url else stop["name"]
                )
                detour_parts: list[str] = []
                detour_miles = stop.get("detour_distance_miles")
                detour_minutes = stop.get("detour_time_minutes")
                if detour_miles not in (None, ""):
                    detour_parts.append(f"{detour_miles} mi detour")
                if detour_minutes not in (None, ""):
                    detour_parts.append(f"{detour_minutes} min")
                detour_html = ""
                if detour_parts:
                    detour_html = f' <span class="stop-detour">({html_escape.escape(" | ".join(detour_parts))})</span>'
                description = html_escape.escape(str(stop.get("description", "") or "").strip())
                html += (
                    f'    <div class="stop-card">'
                    f'<span class="stop-icon">{icon}</span>'
                    f'<div class="stop-body"><strong>{name_html}</strong>{detour_html}'
                    f'<div class="stop-desc">{description}</div></div>'
                    f'</div>\n'
                )
            html += '  </div>\n'
        html += '</div>\n'
        return html

    def _short_place_name(self, name: str) -> str:
        short = name.replace("National Park", "NP").replace("State Park", "SP")
        return " ".join(short.split())

    def _build_attractions(self, ai: dict, drives: list[dict[str, Any]]) -> str:
        attrs = ai.get("top_attractions", [])
        if not attrs and not drives:
            return ""
        
        # Icon and badge color map by type
        type_icons = {
            "hike": "🥾",
            "attraction": "🏛️",
            "viewpoint": "📍",
            "activity": "🎯",
            "landmark": "🗻",
            "nature": "🌲",
            "scenic": "🌄",
        }
        
        difficulty_colors = {
            "Easy": "badge-hike-easy",
            "Moderate": "badge-hike-moderate",
            "Strenuous": "badge-hike-strenuous",
        }
        
        scenic_badges = {
            "drive": "Scenic Drive",
            "viewpoint": "Viewpoint",
            "aerial": "Aerial",
            "day_trip": "Day Trip",
            "historic": "Historic Route",
        }

        html = '<div class="card attractions-card">\n<h3>🏔️ Top Attractions</h3>\n<div class="attraction-list">\n'
        for attr in attrs:
            url = self._normalize_external_url(attr.get("url", ""))
            if not url:
                query = quote(f"{attr.get('name', '')} {dest_name}")
                url = f"https://www.google.com/maps/search/?api=1&query={query}"
            attr_type = attr.get("type", "attraction").lower()
            icon = type_icons.get(attr_type, "📍")
            
            name_html = (
                f'<a href="{self._safe_href(url)}" target="_blank" rel="noopener" class="attr-link">{html_escape.escape(attr.get("name", ""))}</a>'
                if url else html_escape.escape(attr.get("name", ""))
            )
            
            diff = attr.get("difficulty", "")
            dur = attr.get("duration", "")
            must = attr.get("must_see", False)
            note = attr.get("practical_note", "")
            
            diff_class = difficulty_colors.get(diff, "")
            diff_html = f'<span class="badge {diff_class}">{diff}</span>' if diff and diff_class else ""
            dur_html = f'<span class="badge badge-duration">{dur}</span>' if dur else ""
            must_html = '<span class="badge badge-mustsee">Must-See</span>' if must else ""
            note_html = f'<span class="practical-note">📌 {note}</span>' if note else ""
            
            html += (
                f'  <div class="attr-item">'
                f'<div class="attr-header attr-header-inline">'
                f'<span class="attr-icon">{icon}</span>'
                f'<span class="attr-name">{name_html}</span>'
                f'<div class="attr-badges attr-badges-inline">'
                f'{must_html}'
                f'{diff_html}'
                f'{dur_html}'
                f'</div>'
                f'</div>'
                f'<span class="attr-desc">{html_escape.escape(str(attr.get("description", "") or ""))}</span>'
                f'{note_html}'
                f'</div>\n'
            )

        for drive in drives:
            title = drive.get("title", "")
            if not title:
                continue
            safe = title.replace('"', '&quot;').replace("'", "&#39;")
            category = scenic_badges.get(drive.get("category", "drive"), "Scenic Drive")
            duration = drive.get("distance_or_duration", "")
            description = drive.get("description", "")
            duration_html = (
                f'<span class="badge badge-duration">{html_escape.escape(duration)}</span>'
                if duration else ""
            )
            link_html = (
                f'<a href="#" class="attr-link drive-link" data-drive-title="{safe}">'
                f'{html_escape.escape(title)}</a>'
            )

            html += (
                '  <div class="attr-item attr-drive-item">'
                '<div class="attr-header attr-header-inline">'
                '<span class="attr-icon">🚗</span>'
                f'<span class="attr-name">{link_html}</span>'
                '<div class="attr-badges attr-badges-inline">'
                f'<span class="badge badge-scenic">{category}</span>'
                f'{duration_html}'
                '</div>'
                '</div>'
                f'<span class="attr-desc">{html_escape.escape(str(description or ""))}</span>'
                '</div>\n'
            )
        html += '</div>\n</div>\n'
        return html

    def _build_schedule(self, ai: dict, drives: list, dest_name: str) -> str:
        schedule = ai.get("possible_daily_schedule", [])
        
        # If schedule is empty or too short, synthesize from available data
        if not schedule or (isinstance(schedule, list) and len(schedule) < 2):
            attrs = ai.get("top_attractions", [])
            restaurants = ai.get("dinner_recommendations", [])
            
            if attrs and len(attrs) > 0:
                dinner_name = restaurants[0].get("name", "a listed restaurant") if restaurants else "a listed restaurant"
                schedule = [{
                    "day_label": "Day 1",
                    "periods": [
                        {"period": "Morning", "summary": f"Start with {attrs[0]['name']}; plan for 2–3 hours."},
                        {"period": "Afternoon", "summary": f"Continue with {attrs[1]['name'] if len(attrs) > 1 else 'exploration'} and nearby viewpoints."},
                        {"period": "Evening", "summary": f"Dinner at {dinner_name}, then sunset viewing if conditions are clear."},
                    ],
                }]
            else:
                return ""
        
        html = '<div class="card schedule-card">\n<h3>⏰ Possible Daily Schedule</h3>\n'
        period_icons = {"morning": "🌅", "afternoon": "☀️", "evening": "🌙", "plan": "🗺️"}

        if isinstance(schedule, list) and schedule and isinstance(schedule[0], dict):
            day_count = sum(1 for day in schedule if day.get("periods"))
            grid_class = " schedule-days-two-col" if day_count > 1 else ""
            html += f'<div class="schedule-days{grid_class}">\n'
            for day in schedule:
                periods = day.get("periods", [])
                if not periods:
                    continue
                html += f'  <div class="schedule-day">\n'
                html += f'    <div class="schedule-day-title">{html_escape.escape(day.get("day_label", "Day"))}</div>\n'
                for period in periods:
                    label = str(period.get("period", "Plan")).title()
                    content = str(period.get("summary", "")).strip()
                    if not content:
                        continue
                    html += f'    <div class="schedule-period">\n'
                    icon = period_icons.get(label.lower(), "🗺️")
                    html += f'      <div class="schedule-line"><span class="schedule-time"><span class="schedule-icon">{icon}</span> {html_escape.escape(label)}</span><span class="schedule-summary">{html_escape.escape(content)}</span></div>\n'
                    html += f'    </div>\n'
                html += '  </div>\n'
            html += '</div>\n'
        elif isinstance(schedule, dict):
            html += '<div class="schedule-day">\n'
            html += '  <div class="schedule-day-title">Day 1</div>\n'
            for period in ["morning", "afternoon", "evening"]:
                content = str(schedule.get(period, "")).strip()
                if not content:
                    continue
                html += f'  <div class="schedule-period">\n'
                icon = period_icons.get(period, "🗺️")
                html += f'    <div class="schedule-line"><span class="schedule-time"><span class="schedule-icon">{icon}</span> {period.title()}</span><span class="schedule-summary">{html_escape.escape(content)}</span></div>\n'
                html += f'  </div>\n'
            html += '</div>\n'
        else:
            html += '<div class="schedule-day">\n'
            html += '  <div class="schedule-day-title">Day 1</div>\n'
            html += '<ol class="schedule-list">\n'
            for i, item in enumerate(schedule):
                period = ["Morning", "Afternoon", "Evening"][i % 3]
                icon = period_icons.get(period.lower(), "🗺️")
                html += f'  <li class="schedule-item"><span class="schedule-time-inline"><span class="schedule-icon">{icon}</span> {period}</span> {html_escape.escape(str(item))}</li>\n'
            html += '</ol>\n'
            html += '</div>\n'

        html += '</div>\n'
        return html

    def _build_drive_buttons(self, drives: list) -> str:
        import logging
        logger = logging.getLogger(__name__)
        logger.debug(f"_build_drive_buttons called with {len(drives)} drives")
        if not drives:
            return ""
        html = '<div class="card drives-card">\n<h3>Scenic Drives &amp; Viewpoints</h3>\n<div class="drive-buttons">\n'
        for drive in drives:
            title = drive.get("title", "")
            safe = title.replace('"', '&quot;').replace("'", "&#39;")
            html += f'  <button class="drive-link" data-drive-title="{safe}">{title}</button>\n'
        html += '</div>\n</div>\n'
        logger.debug(f"  Generated {len(drives)} drive buttons")
        return html

    def _build_events(self, events: dict, dest_name: str) -> str:
        import logging
        logger = logging.getLogger(__name__)
        
        if not events:
            return ""
        
        html = '<div class="card events-card">\n'
        if events.get("has_events"):
            html += '<h3>🎭 Cultural Events &amp; Entertainment</h3>\n'
            events_intro = events.get("ambient_scene", "") or events.get("intro", "")
            if events_intro:
                html += f'<p class="events-intro">{events_intro}</p>\n'
            html += '<div class="events-list">\n'
            for ev in events.get("events", []):
                url = self._normalize_external_url(ev.get("url", ""))
                if not url:
                    query_text = " ".join(
                        p for p in [
                            str(ev.get("name", "") or "").strip(),
                            str(ev.get("venue", "") or "").strip(),
                            dest_name,
                        ]
                        if p
                    )
                    if query_text:
                        url = f"https://www.google.com/search?q={quote(query_text)}"
                name_html = (
                    f'<a href="{self._safe_href(url)}" target="_blank" rel="noopener" class="event-link">{html_escape.escape(str(ev.get("name", "") or ""))}</a>'
                    if url else ev.get("name", "")
                )
                date_str = ev.get("dates_in_range", "") or ev.get("date", "")
                venue_str = ev.get("venue", "")
                admission_str = ev.get("admission", "")
                
                html += (
                    f'  <div class="event-item">\n'
                    f'    <div class="events-subcard">\n'
                    f'      <strong>{name_html}</strong><br/>\n'
                    f'      <span class="events-date-range">{date_str}</span><br/>\n'
                    f'      📍 {venue_str}<br/>\n'
                    f'      💵 {admission_str}<br/>\n'
                    f'    </div>\n'
                )
                html += '  </div>\n'
            html += '</div>\n'
        else:
            # Fallback: no confirmed ticketed events
            html += '<h3>🎭 Cultural Events</h3>\n'
            honest = events.get("honest_assessment", "")
            if not honest:
                logger.warning("No honest_assessment for '%s' (events=%s)", dest_name, events)
                honest = "No ticketed events were confidently verified for these dates. Check visitor center and local calendars close to travel dates."
            html += f'<p>{honest}</p>\n'
            tip = events.get("local_tip", "")
            if tip:
                tip_query = quote(f"{tip} {dest_name}")
                tip_url = f"https://www.google.com/search?q={tip_query}"
                html += (
                    f'<p class="local-tip"><strong>Local tip:</strong> {html_escape.escape(str(tip))} '
                    f'<a href="{self._safe_href(tip_url)}" target="_blank" rel="noopener" class="event-link">More info</a></p>\n'
                )
        html += '</div>\n'
        return html

    def _build_restaurants(self, ai: dict, dest_name: str) -> str:
        rests = ai.get("dinner_recommendations", [])
        if not rests:
            return ""
        html = '<div class="card restaurants-card">\n<h3>🍽️ Dinner Recommendations</h3>\n<div class="restaurant-list">\n'
        for rest in rests:
            url = self._normalize_external_url(rest.get("maps_url", "") or rest.get("url", ""))
            if "google.com/maps" not in url:
                query = quote(f"{rest.get('name', '')} {dest_name}")
                url = f"https://www.google.com/maps/search/?api=1&query={query}"
            name_html = (
                f'<a href="{self._safe_href(url)}" target="_blank" rel="noopener">{rest["name"]}</a>'
                if url else rest["name"]
            )
            cuisine = rest.get("cuisine", "")
            price = str(rest.get("price_range", "") or rest.get("price", "") or "").strip()
            desc = rest.get("description", "")
            reserve = rest.get("reserve_recommended", False)
            
            # Cuisine badge
            cuisine_badge = f'<span class="badge cuisine-badge">{cuisine}</span>' if cuisine else ""
            
            # Reserve recommendation badge
            reserve_badge = '<span class="badge badge-reserve">Reservations Recommended</span>' if reserve else ""
            price_badge = f'<span class="badge badge-price">{html_escape.escape(price)}</span>' if price else ""
            
            html += (
                f'  <div class="rest-item">\n'
                f'    <div class="rest-header rest-header-inline">\n'
                f'      <span class="rest-name"><span class="rest-icon">🍽️</span> {name_html}</span>\n'
                f'      <div class="rest-badges">\n'
                f'        {cuisine_badge}\n'
                f'        {price_badge}\n'
                f'        {reserve_badge}\n'
                f'      </div>\n'
                f'    </div>\n'
                f'    <span class="rest-desc">{desc}</span>\n'
                f'  </div>\n'
            )
        html += '</div>\n</div>\n'
        return html

    def _build_packing_summary(self, destinations: list[dict[str, Any]]) -> str:
        by_item: dict[str, set[str]] = {}
        for dest in destinations:
            env = dest.get("ai_content", {}).get("expected_environment", {})
            if not isinstance(env, dict):
                continue
            for raw_item in env.get("what_to_pack", []) or []:
                item = str(raw_item).strip()
                if not item:
                    continue
                by_item.setdefault(item, set()).add(dest.get("name", ""))

        if not by_item:
            return ""

        html = '<section class="dest-section pack-summary-section">\n'
        html += '  <div class="card pack-summary-card">\n'
        html += '    <h3>🎒 Packing Summary (Trip-Wide)</h3>\n'
        html += '    <p class="pack-summary-intro">Here\'s what to bring and where it\'s needed:</p>\n'
        html += '    <ul class="pack-summary-list">\n'
        for item in sorted(by_item.keys(), key=str.lower):
            places = ", ".join(sorted(p for p in by_item[item] if p))
            html += f'      <li><strong>{html_escape.escape(item)}</strong>'
            if places:
                html += f' <span class="pack-summary-places">({html_escape.escape(places)})</span>'
            html += '</li>\n'
        html += '    </ul>\n'
        html += '  </div>\n'
        html += '</section>\n'
        return html

    def _build_generator_footer(self, trip: dict[str, Any]) -> str:
        meta = trip.get("_meta", {})
        version = meta.get("generator_version", "")
        timestamp = meta.get("generated_at_utc")
        if timestamp:
            try:
                dt = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
                shown_time = dt.strftime("%Y-%m-%d %H:%M UTC")
            except ValueError:
                shown_time = str(timestamp)
        else:
            shown_time = "unknown"
        return (
            'Generated by '
            '<a href="https://github.com/flysoftware-git/road-trip-generator" '
            'target="_blank" rel="noopener">Road Trip Itinerary Generator</a>'
            f' v{html_escape.escape(str(version))} · {html_escape.escape(shown_time)}'
        )

    def _build_drive_descriptions(self, destinations: list[dict]) -> dict[str, Any]:
        """Build DRIVE_DESCRIPTIONS keyed by raw title string (matches template JS lookup)."""
        result: dict[str, Any] = {}
        for dest in destinations:
            for drive in dest.get("scenic_drives", []):
                key = drive.get("title", "")
                result[key] = {
                    "title": drive.get("title", ""),
                    "category": drive.get("category", "scenic_drive"),
                    "distance_or_duration": drive.get("distance_or_duration", ""),
                    "best_time": drive.get("best_time", ""),
                    "description": self._clean_drive_description(drive.get("description", "")),
                    "vehicle_requirement": drive.get("vehicle_requirement", ""),
                    "url": drive.get("url", ""),
                }
        return result

    def _clean_drive_description(self, description: Any) -> str:
        text = str(description or "").strip()
        if not text:
            return ""
        text = re.sub(r"<!--.*?-->", " ", text, flags=re.DOTALL)
        text = re.sub(r"\b(?:template_version|generator_version|generated_at|provider|model)\s*=\s*[^;\n]+", " ", text, flags=re.IGNORECASE)
        text = re.sub(r"\bGenerated by\b[^.\n]*", " ", text, flags=re.IGNORECASE)
        text = re.sub(r"\bVersion\b\s*[:=]?\s*[^.\n]*", " ", text, flags=re.IGNORECASE)
        text = re.sub(r"\bUpdated\b\s*[:=]?\s*[^.\n]*", " ", text, flags=re.IGNORECASE)
        text = re.sub(r"\b(?:Attribution|Credit|License)\b\s*[:=]?\s*[^.\n]*", " ", text, flags=re.IGNORECASE)
        lines = [ln.strip() for ln in re.split(r"\n+", text) if ln.strip()]
        keep: list[str] = []
        for line in lines:
            lower = line.lower()
            if any(k in lower for k in ["attribution", "credit:", "photo by", "license:", "version", "updated", "generator"]):
                continue
            keep.append(line)
        cleaned = " ".join(keep).strip()
        cleaned = re.sub(r"\s{2,}", " ", cleaned)
        return cleaned

    def _safe_href(self, url: str) -> str:
        return html_escape.escape(self._normalize_external_url(url), quote=True)

    def _normalize_external_url(self, url: Any) -> str:
        raw = str(url or "").strip()
        if not raw:
            return ""
        low = raw.lower()
        if low.startswith(("javascript:", "data:")):
            return ""
        if low.startswith("//"):
            return "https:" + raw
        if low.startswith(("http://", "https://", "mailto:")):
            return raw
        if re.match(r"^[a-z][a-z0-9+.-]*:", low):
            return ""
        return "https://" + raw

    def _infer_stop_type(self, stop: dict[str, Any]) -> str:
        raw = str(stop.get("type", "") or "").strip().lower()
        if raw:
            return raw
        text = f"{stop.get('name', '')} {stop.get('description', '')}".lower()
        if any(k in text for k in ["trail", "hike", "loop", "summit"]):
            return "hike"
        if any(k in text for k in ["overlook", "viewpoint", "vista"]):
            return "viewpoint"
        if any(k in text for k in ["museum", "center", "historic"]):
            return "museum"
        if any(k in text for k in ["market", "shop", "gallery"]):
            return "market"
        if any(k in text for k in ["falls", "waterfall", "river", "lake"]):
            return "waterfall"
        if any(k in text for k in ["food", "cafe", "restaurant", "bakery"]):
            return "food"
        return "attraction"

    def _build_debug_block(self, dest: dict[str, Any], trip_meta: dict[str, Any]) -> str:
        debug_payload = {
            "destination_id": dest.get("id", ""),
            "destination_name": dest.get("name", ""),
            "coordinates": {"lat": dest.get("lat"), "lng": dest.get("lng")},
            "nps_park_code": dest.get("nps_park_code"),
            "counts": {
                "images": len(dest.get("images", [])),
                "attractions": len(dest.get("ai_content", {}).get("top_attractions", [])),
                "restaurants": len(dest.get("ai_content", {}).get("dinner_recommendations", [])),
                "drives": len(dest.get("scenic_drives", [])),
                "events": len(dest.get("cultural_events", {}).get("events", [])),
            },
            "llm": {
                "provider": trip_meta.get("llm", {}).get("provider", ""),
                "model": trip_meta.get("llm", {}).get("model", ""),
            },
        }
        payload = html_escape.escape(json.dumps(debug_payload, indent=2))
        return (
            '<details class="debug-block" style="margin-top:1rem;background:#f8fafc;border:1px solid #d7dee7;padding:0.75rem;border-radius:8px;">\n'
            '  <summary style="cursor:pointer;font-weight:600;">Debug</summary>\n'
            f'  <pre style="margin-top:0.75rem;overflow:auto;white-space:pre-wrap;">{payload}</pre>\n'
            '</details>\n'
        )

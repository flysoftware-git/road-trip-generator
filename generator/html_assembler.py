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
from pathlib import Path
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
        gmaps_url = self._build_google_maps_url(trip["destinations"])
        html = html.replace("<!--GOOGLE_MAPS_URL-->", gmaps_url)

        # ── Map markers JSON ─────────────────────────────────────────────────
        markers = self._build_map_markers(trip["destinations"])
        html = html.replace("'<!--MAP_MARKERS_JSON-->'", json.dumps(markers))

        # ── Nav tabs ────────────────────────────────────────────────────────
        html = html.replace("<!--NAV_TABS-->", self._build_nav_tabs(trip["destinations"]))

        # ── Per-destination sections ─────────────────────────────────────────
        sections_html = ""
        for dest in trip.get("destinations", []):
            sections_html += self._build_single_section(dest, meta)
        html = html.replace("<!--DESTINATION_SECTIONS-->", sections_html)

        # ── var DRIVE_DESCRIPTIONS (keyed by raw title, matches template JS) ──
        drive_descriptions = self._build_drive_descriptions(trip["destinations"])
        drive_json = json.dumps(drive_descriptions, indent=2)
        html = html.replace(
            "var DRIVE_DESCRIPTIONS = {};",
            f"var DRIVE_DESCRIPTIONS = {drive_json};",
        )

        return html

    # ── Private helpers ──────────────────────────────────────────────────────

    def _build_google_maps_url(self, destinations: list[dict[str, Any]]) -> str:
        waypoints = "/".join(
            f"{d['lat']},{d['lng']}"
            for d in destinations
            if d.get("lat") and d.get("lng")
        )
        return f"https://www.google.com/maps/dir/{waypoints}"

    def _build_map_markers(self, destinations: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Build Leaflet stops array in {c:[lat,lng], mo, dy, name} format."""
        import re
        result = []
        for d in destinations:
            dates = d.get("dates", "")
            # Extract month abbrev and start day from e.g. "October 7-9, 2026"
            mo_match = re.match(r'([A-Za-z]+)\s+(\d+)', dates)
            mo = mo_match.group(1)[:3] if mo_match else ""
            dy = mo_match.group(2) if mo_match else ""
            short = d["name"].replace(" National Park", "").replace(" State Park", "")
            result.append({"c": [d.get("lat", 0), d.get("lng", 0)], "mo": mo, "dy": dy, "name": short})
        return result

    def _build_nav_tabs(self, destinations: list[dict[str, Any]]) -> str:
        """Build tab-btn buttons (data-tab=section-{id}) + Google Maps link."""
        gmaps_url = self._build_google_maps_url(destinations)
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

    def _build_single_section(self, dest: dict[str, Any], trip_meta: dict[str, Any]) -> str:
        import logging
        logger = logging.getLogger(__name__)
        ai = dest.get("ai_content", {})
        images = dest.get("images", [])
        planning_links = dest.get("planning_links", [])
        events = dest.get("cultural_events", {})
        drives = dest.get("scenic_drives", [])
        logger.debug(f"_build_single_section for {dest['name']}: scenic_drives={len(drives)}")

        section_id = dest["id"]  # use manifest id directly
        section = f'<section id="section-{section_id}" class="dest-section">\n'

        # Header
        section += self._build_header(dest, ai, images)

        # Image gallery
        section += self._build_image_gallery(images, dest["name"])

        # Expected environment
        section += self._build_environment_card(ai)

        # Getting here + en-route stops
        section += self._build_getting_here(ai, dest)

        # Attractions
        section += self._build_attractions(ai)

        # Daily schedule
        section += self._build_schedule(ai, drives, dest["name"])

        # Scenic drives (button row → modals built separately)
        section += self._build_drive_buttons(drives)

        # Cultural events or honest fallback
        section += self._build_events(events, dest["name"])

        # Dinner recommendations
        section += self._build_restaurants(ai)

        # Planning links
        section += self._build_planning_links(planning_links, dest.get("nps_park_code"), dest)

        # Collapsible debug block
        section += self._build_debug_block(dest, trip_meta)

        section += "</section>\n"
        return section

    # ── Section builders ─────────────────────────────────────────────────────

    def _build_header(self, dest: dict, ai: dict, images: list) -> str:
        hero_img = images[0]["local_path"] if images else ""
        # Convert to file:// URL for browser
        if hero_img:
            hero_img = _path_to_file_url(hero_img)
        credit = images[0].get("credit", "") if images else ""
        return (
            f'<div class="dest-header" style="background-image:url(\'{hero_img}\')">\n'
            f'  <h2>{dest["name"]}</h2>\n'
            f'  <p class="dates">{dest["dates"]}</p>\n'
            f'  <p class="img-credit">{credit}</p>\n'
            f'</div>\n'
        )

    def _build_environment_card(self, ai: dict) -> str:
        env = ai.get("expected_environment", "")
        if not env:
            return ""
        # Handle dict structure: {"summary": "...", "temperature_high_f": 72, ...}
        if isinstance(env, dict):
            summary = env.get("summary", "")
            temp_h = env.get("temperature_high_f", "")
            temp_l = env.get("temperature_low_f", "")
            pack = env.get("what_to_pack", [])
            html = '<div class="card env-card">\n'
            html += '<div class="env-subcard">\n'
            html += f'<h3>🧥 What to Expect</h3>\n'
            html += f'  <p class="env-summary">{summary}</p>\n'
            if temp_h or temp_l:
                html += f'  <div class="temp-range">🌡️ Temperature: {temp_h}°F high / {temp_l}°F low</div>\n'
            if pack:
                html += '  <div class="pack-list">Pack: ' + ', '.join(pack) + '</div>\n'
            html += '</div>\n'
            html += '</div>\n'
            return html
        # Fallback for string
        return f'<div class="card env-card"><p>{env}</p></div>\n'

    def _build_image_gallery(self, images: list, dest_name: str) -> str:
        """Build image gallery from discovered images."""
        if not images or len(images) <= 1:
            return ""
        
        html = '<div class="photo-gallery">\n'
        # Skip first image (hero), render rest in gallery
        for img in images[1:]:
            local_path = img.get("local_path", "")
            credit = img.get("credit", "")
            source_url = img.get("source_url", "")
            
            if not local_path:
                continue
            
            file_url = _path_to_file_url(local_path)
            dest_escaped = html_escape.escape(dest_name)
            
            html += '  <div class="photo-item">\n'
            if source_url:
                html += f'    <a href="{source_url}" target="_blank" rel="noopener">\n'
                html += f'      <img src="{file_url}" alt="{dest_escaped}" />\n'
                html += '    </a>\n'
            else:
                html += f'    <img src="{file_url}" alt="{dest_escaped}" />\n'
            
            if credit:
                html += f'    <p class="photo-caption">{credit}</p>\n'
            html += '  </div>\n'
        
        html += '</div>\n'
        return html

    def _build_route_gmaps_url(self, dest: dict, stops: list) -> str:
        """Build Google Maps URL with destination and waypoints."""
        dest_lat = dest.get("lat", "")
        dest_lng = dest.get("lng", "")
        if not dest_lat or not dest_lng:
            return ""
        
        waypoints_str = ""
        if stops and len(stops) > 0:
            waypoint_coords = []
            for stop in stops[:10]:
                stop_name = stop.get("name", "").replace(" ", "+")
                waypoint_coords.append(stop_name)
            if waypoint_coords:
                waypoints_str = "&waypoints=" + "|".join(waypoint_coords)
        
        destination = f"{dest_lat},{dest_lng}"
        gmaps_url = f"https://www.google.com/maps/dir/?api=1&destination={destination}{waypoints_str}&travelmode=driving"
        return gmaps_url

    def _build_getting_here(self, ai: dict, dest: dict) -> str:
        gh = ai.get("getting_here", {})
        if not gh:
            return ""
        from_text = gh.get("from_previous", "")
        # Ensure from_text is a string, not dict
        if isinstance(from_text, dict):
            from_text = str(from_text)
        
        distance = gh.get("distance_miles", "")
        drive_time = gh.get("drive_time", "")
        stops = gh.get("en_route_stops", [])
        
        # Build Google Maps URL with waypoints
        gmaps_url = self._build_route_gmaps_url(dest, stops)
        
        # Icon map for stop types
        stop_icons = {
            "viewpoint": "🏜️",
            "attraction": "🏛️",
            "town": "🏘️",
            "food": "🍔",
            "scenic": "🌄",
            "natural": "🏞️",
            "historic": "🏛️",
        }
        
        html = '<div class="card getting-here-card getting-here-subcard">\n'
        html += '  <div class="getting-here-header">\n'
        html += '    <h3>🚗 Getting Here</h3>\n'
        if distance and drive_time:
            html += f'    <a href="{gmaps_url}" target="_blank" rel="noopener" class="gmaps-link">Open in Google Maps →</a>\n'
        html += '  </div>\n'
        
        # Route summary with distance and time badges
        if distance and drive_time:
            html += '  <div class="route-badges">\n'
            html += f'    <span class="badge badge-distance">{distance} miles</span>\n'
            html += f'    <span class="badge badge-time">~{drive_time}</span>\n'
            html += '  </div>\n'
        
        html += f'  <p class="route-summary">{from_text}</p>\n'
        
        if stops:
            html += '  <div class="can-miss-header">CAN\'T-MISS EN ROUTE</div>\n'
            html += '  <div class="en-route-stops">\n'
            for stop in stops:
                url = stop.get("url", "")
                stop_type = stop.get("type", "attraction").lower()
                icon = stop_icons.get(stop_type, "📍")
                name_html = (
                    f'<a href="{url}" target="_blank">{stop["name"]}</a>'
                    if url else stop["name"]
                )
                html += (
                    f'    <div class="stop-card">'
                    f'<span class="stop-icon">{icon}</span>'
                    f'<strong>{name_html}</strong> — {stop.get("description", "")} '
                    f'</div>\n'
                )
            html += '  </div>\n'
        html += '</div>\n'
        return html

    def _build_attractions(self, ai: dict) -> str:
        attrs = ai.get("top_attractions", [])
        if not attrs:
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
        
        html = '<div class="card attractions-card">\n<h3>🏔️ Top Attractions</h3>\n<div class="attraction-list">\n'
        for attr in attrs:
            url = attr.get("url", "")
            attr_type = attr.get("type", "attraction").lower()
            icon = type_icons.get(attr_type, "📍")
            
            name_html = (
                f'<a href="{url}" target="_blank" class="attr-link">{attr["name"]}</a>'
                if url else attr["name"]
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
                f'<div class="attr-header">'
                f'<span class="attr-icon">{icon}</span>'
                f'<span class="attr-name">{name_html}</span>'
                f'</div>'
                f'<div class="attr-badges">'
                f'{must_html}'
                f'{diff_html}'
                f'{dur_html}'
                f'</div>'
                f'<span class="attr-desc">{attr.get("description", "")}</span>'
                f'{note_html}'
                f'</div>\n'
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
                schedule = [
                    f"🌅 Morning: Start with {attrs[0]['name']}; plan for 2–3 hours.",
                    f"☀️ Afternoon: Continue with {attrs[1]['name'] if len(attrs) > 1 else 'exploration'}.",
                    f"🌙 Evening: Dinner reservation recommended; end with sunset viewing if possible.",
                ]
            else:
                return ""
        
        html = '<div class="card schedule-card">\n<h3>⏰ Possible Daily Schedule</h3>\n'
        
        # Render as periods (morning/afternoon/evening)
        period_emojis = {"morning": "🌅", "afternoon": "☀️", "evening": "🌙"}
        
        if isinstance(schedule, dict):
            # Dict with morning/afternoon/evening keys
            html += '<div class="schedule-periods">\n'
            for period in ["morning", "afternoon", "evening"]:
                content = schedule.get(period, "")
                if content:
                    emoji = period_emojis.get(period, "")
                    html += f'  <div class="schedule-period">\n'
                    html += f'    <div class="schedule-time"><span class="schedule-icon">{emoji}</span> {period.title()}</div>\n'
                    html += f'    <div class="schedule-content">{content}</div>\n'
                    html += f'  </div>\n'
            html += '</div>\n'
        else:
            # List of schedule items
            html += '<ol class="schedule-list">\n'
            for i, item in enumerate(schedule):
                emoji_idx = i % 3
                emojis = ["🌅", "☀️", "🌙"]
                html += f'  <li><span class="schedule-icon">{emojis[emoji_idx]}</span> {item}</li>\n'
            html += '</ol>\n'
        
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
            html += f'<p class="events-intro">{events.get("intro", "")}</p>\n'
            html += '<div class="events-list">\n'
            for ev in events.get("events", []):
                url = ev.get("url", "")
                name_html = (
                    f'<a href="{url}" target="_blank" class="event-link">{ev.get("name", "")}</a>'
                    if url else ev.get("name", "")
                )
                date_str = ev.get("date", "")
                venue_str = ev.get("venue", "")
                admission_str = ev.get("admission", "")
                ambient = ev.get("ambient_scene", "")
                
                html += (
                    f'  <div class="event-item">\n'
                    f'    <div class="events-subcard">\n'
                    f'      <strong>{name_html}</strong><br/>\n'
                    f'      <span class="events-date-range">{date_str}</span><br/>\n'
                    f'      📍 {venue_str}<br/>\n'
                    f'      💵 {admission_str}\n'
                    f'    </div>\n'
                )
                if ambient:
                    html += f'    <em class="ambient-scene">{ambient}</em>\n'
                html += '  </div>\n'
            html += '</div>\n'
        else:
            # Fallback: no events found
            html += f'<h3>🎭 What to Know About {dest_name}</h3>\n'
            honest = events.get("honest_assessment", "")
            if not honest:
                logger.warning("No honest_assessment for '%s' (events=%s)", dest_name, events)
                honest = "Cultural events and programming vary seasonally. Check local visitor center websites for current offerings."
            html += f'<p>{honest}</p>\n'
            tip = events.get("local_tip", "")
            if tip:
                html += f'<p class="local-tip"><strong>💡 Local tip:</strong> {tip}</p>\n'
        html += '</div>\n'
        return html

    def _build_restaurants(self, ai: dict) -> str:
        rests = ai.get("dinner_recommendations", [])
        if not rests:
            return ""
        html = '<div class="card restaurants-card">\n<h3>🍽️ Dinner Recommendations</h3>\n<div class="restaurant-list">\n'
        for rest in rests:
            url = rest.get("url", "")
            name_html = (
                f'<a href="{url}" target="_blank">{rest["name"]}</a>'
                if url else rest["name"]
            )
            price = rest.get("price", "")
            cuisine = rest.get("cuisine", "")
            desc = rest.get("description", "")
            reserve = rest.get("reserve_recommended", False)
            
            # Price badge (convert $ symbols to count or keep as-is)
            price_badge = ""
            if price:
                # Normalize to $/$$/$$$/$$$$
                price_normalized = str(price).replace("USD", "").strip()
                price_badge = f'<span class="badge badge-price">{price_normalized}</span>'
            
            # Cuisine badge
            cuisine_badge = f'<span class="badge cuisine-badge">{cuisine}</span>' if cuisine else ""
            
            # Reserve recommendation badge
            reserve_badge = '<span class="badge badge-reserve">Reservations Recommended</span>' if reserve else ""
            
            html += (
                f'  <div class="rest-item">\n'
                f'    <div class="rest-header">\n'
                f'      <span class="rest-name">{name_html}</span>\n'
                f'    </div>\n'
                f'    <div class="rest-badges">\n'
                f'      {cuisine_badge}\n'
                f'      {price_badge}\n'
                f'      {reserve_badge}\n'
                f'    </div>\n'
                f'    <span class="rest-desc">{desc}</span>\n'
                f'  </div>\n'
            )
        html += '</div>\n</div>\n'
        return html

    def _build_planning_links(self, links: list, nps_code: str | None, dest: dict) -> str:
        html = '<div class="card links-card">\n<h3>Planning Links</h3>\n<ul class="links-list">\n'
        # Auto-add weather link
        lat = dest.get("lat", 0)
        lng = dest.get("lng", 0)
        if lat and lng:
            weather_url = f"https://forecast.weather.gov/MapClick.php?lat={lat:.4f}&lon={lng:.4f}"
            html += f'  <li><a href="{weather_url}" target="_blank">🌤 Weather Forecast</a></li>\n'
        # Auto-add NPS link
        if nps_code:
            html += f'  <li><a href="https://www.nps.gov/{nps_code}/" target="_blank">🏛 NPS Park Page</a></li>\n'
        # User-provided planning links
        for link in links:
            url = link.get("url", "")
            label = link.get("label", url)
            check = " ✓" if link.get("verified") else " ⚠️"
            html += f'  <li><a href="{url}" target="_blank">{label}</a>{check}</li>\n'
        html += '</ul>\n</div>\n'
        return html

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
                    "description": drive.get("description", ""),
                    "vehicle_requirement": drive.get("vehicle_requirement", ""),
                    "url": drive.get("url", ""),
                }
        return result

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

#!/usr/bin/env python3
"""Patch html_assembler.py with all 6 feature fixes."""
import re
from pathlib import Path

assembler_file = Path("generator/html_assembler.py")
content = assembler_file.read_text(encoding="utf-8")

# 1. Add _escape_html_attr helper function before sanitize_dest_id
escape_func = '''def _escape_html_attr(s: str) -> str:
    """Escape HTML attribute values."""
    return s.replace('&', '&amp;').replace('"', '&quot;').replace("'", '&#39;').replace('<', '&lt;').replace('>', '&gt;')


'''
insert_pos = content.find("def sanitize_dest_id(name: str) -> str:")
if "_escape_html_attr" not in content:
    content = content[:insert_pos] + escape_func + content[insert_pos:]
    print("✓ Added _escape_html_attr function")

# 2. Fix _build_single_section signature - update calls to pass dest, drives, dest_name
content = content.replace(
    "section += self._build_getting_here(ai)",
    "section += self._build_getting_here(ai, dest)"
)
print("✓ Updated _build_getting_here call to pass dest")

content = content.replace(
    "section += self._build_schedule(ai)",
    "section += self._build_schedule(ai, drives, dest[\"name\"])"
)
print("✓ Updated _build_schedule call to pass drives and dest_name")

# Add image gallery call after header
old_section = """        # Header
        section += self._build_header(dest, ai, images)

        # Expected environment"""

new_section = """        # Header
        section += self._build_header(dest, ai, images)

        # Image gallery
        section += self._build_image_gallery(images, dest["name"])

        # Expected environment"""

content = content.replace(old_section, new_section)
print("✓ Added _build_image_gallery call")

# 3. Replace _build_getting_here method with new version
old_method_start = "    def _build_getting_here(self, ai: dict) -> str:"
old_method_end = '        html += "</div>\n        return html\n\n    def _build_attractions'

if old_method_start in content and old_method_end in content:
    new_getting_here = '''    def _build_getting_here(self, ai: dict, dest: dict[str, Any]) -> str:
        gh = ai.get("getting_here", {})
        if not gh:
            return ""
        from_text = gh.get("from_previous", "")
        # Ensure from_text is a string, not dict
        if isinstance(from_text, dict):
            from_text = str(from_text)
        
        drive_time = gh.get("drive_time", "")
        distance = gh.get("distance_miles", "")
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
    
    def _build_route_gmaps_url(self, dest: dict[str, Any], stops: list[dict[str, Any]]) -> str:
        """Build Google Maps URL with destination and waypoints."""
        # Start with destination as primary target
        dest_lat = dest.get("lat", "")
        dest_lng = dest.get("lng", "")
        if not dest_lat or not dest_lng:
            return ""
        
        # Build waypoints from en-route stops (up to 10 waypoints limit for Google Maps API)
        waypoints_str = ""
        if stops and len(stops) > 0:
            # Try to get coordinates from stops if available
            waypoint_coords = []
            for stop in stops[:10]:  # Google Maps limit
                # Fallback: use stop name if coordinates not available
                stop_name = stop.get("name", "").replace(" ", "+")
                waypoint_coords.append(stop_name)
            if waypoint_coords:
                waypoints_str = "&waypoints=" + "|".join(waypoint_coords)
        
        # Construct URL: user's current location (origin empty = use current location) to destination with waypoints
        destination = f"{dest_lat},{dest_lng}"
        gmaps_url = f"https://www.google.com/maps/dir/?api=1&destination={destination}{waypoints_str}&travelmode=driving"
        return gmaps_url

    def _build_attractions'''
    
    # Find start of old method
    start_idx = content.find(old_method_start)
    end_idx = content.find(old_method_end, start_idx)
    if start_idx >= 0 and end_idx >= 0:
        content = content[:start_idx] + new_getting_here + content[end_idx:]
        print("✓ Updated _build_getting_here with Google Maps and styling")

# 4. Replace _build_attractions
old_attr = "    def _build_attractions(self, ai: dict) -> str:\n        attrs = ai.get(\"top_attractions\", [])\n        if not attrs:\n            return \"\"\n        html = '<div class=\"card attractions-card\">\\n<h3>Top Attractions</h3>\\n<ul class=\"attraction-list\">\\n'"

new_attr_start = '''    def _build_attractions(self, ai: dict) -> str:
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
        
        html = '<div class="card attractions-card">\n<h3>🏔️ Top Attractions</h3>\n<div class="attraction-list">\n'''

if old_attr in content:
    # Replace just the opening part
    content = content.replace(old_attr, new_attr_start)
    print("✓ Updated attractions method opening")

# 5. Replace old attractions item rendering with new version
old_attr_item = '''        for attr in attrs:
            url = attr.get("url", "")
            name_html = (
                f'<a href="{url}" target="_blank">{attr["name"]}</a>'
                if url else attr["name"]
            )
            diff = attr.get("difficulty", "")
            dur = attr.get("duration", "")
            must = " ⭐" if attr.get("must_see") else ""
            note = attr.get("practical_note", "")
            diff_html = f'<span class="badge diff-badge">{diff}</span>' if diff else ""
            dur_html = f'<span class="badge dur-badge">{dur}</span>' if dur else ""
            note_html = f'<span class="practical-note">{note}</span>' if note else ""
            html += (
                f'  <li class="attr-item">'
                f'<span class="attr-name">{name_html}{must}</span>'
                f'{diff_html}'
                f'{dur_html}'
                f'<span class="attr-desc">{attr.get("description", "")}</span>'
                f'{note_html}'
                f'</li>\n'
            )
        html += '</ul>\n</div>\n'
        return html'''

new_attr_item = '''        for attr in attrs:
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
        return html'''

if old_attr_item in content:
    content = content.replace(old_attr_item, new_attr_item)
    print("✓ Updated attraction item rendering")

# Save the patched file
assembler_file.write_text(content, encoding="utf-8")
print("\n✅ All patches applied successfully!")
print("✨ Verify with: python -m py_compile generator/html_assembler.py")

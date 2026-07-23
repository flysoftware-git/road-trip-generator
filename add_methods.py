#!/usr/bin/env python3
"""Add the new methods to html_assembler.py for all 6 feature fixes."""
from pathlib import Path

# Read the current file
assembler_path = Path("generator/html_assembler.py")
lines = assembler_path.read_text().split('\n')

# Find the line number where _build_header ends and _build_environment_card begins
insert_after_line = None
for i, line in enumerate(lines):
    if "def _build_environment_card" in line:
        insert_after_line = i
        break

if insert_after_line is None:
    print("ERROR: Could not find insertion point")
    exit(1)

# New methods to insert
new_methods = '''
    def _build_image_gallery(self, images: list, dest_name: str) -> str:
        """Build image gallery from discovered images."""
        if not images or len(images) <= 1:
            return ""
        
        html = '<div class="photo-gallery">\\n'
        # Skip the first image (used as hero header)
        for img in images[1:]:
            local_path = img.get("local_path", "")
            credit = img.get("credit", "")
            source_url = img.get("source_url", "")
            
            if not local_path:
                continue
            
            # Convert to file:// URL
            file_url = _path_to_file_url(local_path)
            
            html += '  <div class="photo-item">\\n'
            if source_url:
                html += f'    <a href="{source_url}" target="_blank" rel="noopener">\\n'
                html += f'      <img src="{file_url}" alt="{html_escape.escape(dest_name)}" />\\n'
                html += '    </a>\\n'
            else:
                html += f'    <img src="{file_url}" alt="{html_escape.escape(dest_name)}" />\\n'
            
            if credit:
                html += f'    <p class="photo-caption">{credit}</p>\\n'
            html += '  </div>\\n'
        
        html += '</div>\\n'
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
            for stop in stops[:10]:  # Google Maps limit
                stop_name = stop.get("name", "").replace(" ", "+")
                waypoint_coords.append(stop_name)
            if waypoint_coords:
                waypoints_str = "&waypoints=" + "|".join(waypoint_coords)
        
        destination = f"{dest_lat},{dest_lng}"
        gmaps_url = f"https://www.google.com/maps/dir/?api=1&destination={destination}{waypoints_str}&travelmode=driving"
        return gmaps_url
'''

# Insert the new methods
lines.insert(insert_after_line, new_methods)

# Write back
assembler_path.write_text('\n'.join(lines))
print(f"✅ Added new methods at line {insert_after_line}")
print("🔍 Now updating method signatures and calls...")

# Now update the actual methods - read again since we've modified the file
content = assembler_path.read_text()

# Update _build_single_section
content = content.replace(
    'section += self._build_header(dest, ai, images)\n\n        # Expected environment',
    'section += self._build_header(dest, ai, images)\n\n        # Image gallery\n        section += self._build_image_gallery(images, dest["name"])\n\n        # Expected environment'
)
print("✓ Added image gallery call")

content = content.replace(
    'section += self._build_getting_here(ai)',
    'section += self._build_getting_here(ai, dest)'
)
print("✓ Updated getting_here call")

content = content.replace(
    'section += self._build_schedule(ai)',
    'section += self._build_schedule(ai, drives, dest["name"])'
)
print("✓ Updated schedule call")

# Write the updated content
assembler_path.write_text(content)
print("\\n✅ All updates applied!")

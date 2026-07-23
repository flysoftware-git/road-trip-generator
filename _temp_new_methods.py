# Temporary file with new methods to be added to html_assembler.py

def _build_image_gallery(self, images: list, dest_name: str) -> str:
    """Build image gallery from discovered images."""
    if not images or len(images) <= 1:
        return ""
    
    html = '<div class="photo-gallery">\n'
    # Skip the first image (used as hero header)
    for img in images[1:]:
        local_path = img.get("local_path", "")
        credit = img.get("credit", "")
        source_url = img.get("source_url", "")
        
        if not local_path:
            continue
        
        # Convert to file:// URL
        file_url = _path_to_file_url(local_path)
        
        html += '  <div class="photo-item">\n'
        if source_url:
            html += f'    <a href="{source_url}" target="_blank" rel="noopener">\n'
            html += f'      <img src="{file_url}" alt="{_escape_html_attr(dest_name)}" />\n'
            html += '    </a>\n'
        else:
            html += f'    <img src="{file_url}" alt="{_escape_html_attr(dest_name)}" />\n'
        
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
    
    # Build waypoints from en-route stops (up to 10 waypoints limit)
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


def _build_getting_here_new(self, ai: dict, dest: dict) -> str:
    """Build 'Getting Here' section with Google Maps link, badges, and icons."""
    gh = ai.get("getting_here", {})
    if not gh:
        return ""
    from_text = gh.get("from_previous", "")
    if isinstance(from_text, dict):
        from_text = str(from_text)
    
    drive_time = gh.get("drive_time", "")
    distance = gh.get("distance_miles", "")
    stops = gh.get("en_route_stops", [])
    
    gmaps_url = self._build_route_gmaps_url(dest, stops)
    
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
            name_html = f'<a href="{url}" target="_blank">{stop["name"]}</a>' if url else stop["name"]
            html += f'    <div class="stop-card"><span class="stop-icon">{icon}</span><strong>{name_html}</strong> — {stop.get("description", "")} </div>\n'
        html += '  </div>\n'
    html += '</div>\n'
    return html


def _build_schedule_new(self, ai: dict, drives: list, dest_name: str) -> str:
    """Build daily schedule, synthesizing from attractions, restaurants."""
    schedule_data = ai.get("possible_daily_schedule", {})
    
    if not schedule_data or (isinstance(schedule_data, (list, dict)) and len(str(schedule_data)) < 50):
        attrs = ai.get("top_attractions", [])
        rests = ai.get("dinner_recommendations", [])
        
        html = '<div class="card schedule-card">\n<h3>📅 Possible Daily Schedule</h3>\n'
        html += '<div class="schedule-note">Sample itinerary for active adults—adjust pace and attractions to match your interests.</div>\n'
        html += '<div class="schedule-day"><h4 class="schedule-day-title">Day 1</h4>'
        
        html += '<div class="schedule-period"><span class="schedule-icon">🌅</span><div class="schedule-content">'
        html += '<strong class="schedule-time">MORNING</strong>'
        if attrs:
            attr_name = attrs[0].get("name", "nearby attraction")
            html += f'<p>Start with {attr_name}; plan for 2–3 hours to avoid peak crowds.</p>'
        html += '</div></div>'
        
        html += '<div class="schedule-period"><span class="schedule-icon">☀️</span><div class="schedule-content">'
        html += '<strong class="schedule-time">AFTERNOON</strong>'
        if len(attrs) > 1:
            attr_name = attrs[1].get("name", "scenic location")
            html += f'<p>Lunch, then visit {attr_name}. Allow 2–3 hours with travel time.</p>'
        elif drives:
            drive_name = drives[0].get("title", "scenic drive")
            html += f'<p>Lunch break, then take the {drive_name} for panoramic views.</p>'
        else:
            html += f'<p>Lunch and rest period. Explore additional local highlights or relax in town.</p>'
        html += '</div></div>'
        
        html += '<div class="schedule-period"><span class="schedule-icon">🌙</span><div class="schedule-content">'
        html += '<strong class="schedule-time">EVENING</strong>'
        if rests:
            rest_name = rests[0].get("name", "recommended restaurant")
            html += f'<p>Dinner at {rest_name}. Check reservation availability ahead.</p>'
        else:
            html += f'<p>Dinner in {dest_name}. Sunset viewing from a lookout is ideal if weather permits.</p>'
        html += '</div></div>'
        html += '</div></div>\n'
        return html
    
    if isinstance(schedule_data, dict):
        html = '<div class="card schedule-card">\n<h3>📅 Possible Daily Schedule</h3>\n'
        html += '<div class="schedule-day">'
        
        morning = schedule_data.get("morning", "")
        if morning:
            html += '<div class="schedule-period"><span class="schedule-icon">🌅</span><div class="schedule-content">'
            html += '<strong class="schedule-time">MORNING</strong>'
            html += f'<p>{morning}</p>'
            html += '</div></div>'
        
        afternoon = schedule_data.get("afternoon", "")
        if afternoon:
            html += '<div class="schedule-period"><span class="schedule-icon">☀️</span><div class="schedule-content">'
            html += '<strong class="schedule-time">AFTERNOON</strong>'
            html += f'<p>{afternoon}</p>'
            html += '</div></div>'
        
        evening = schedule_data.get("evening", "")
        if evening:
            html += '<div class="schedule-period"><span class="schedule-icon">🌙</span><div class="schedule-content">'
            html += '<strong class="schedule-time">EVENING</strong>'
            html += f'<p>{evening}</p>'
            html += '</div></div>'
        
        html += '</div></div>\n'
        return html
    
    html = '<div class="card schedule-card">\n<h3>📅 Possible Daily Schedule</h3>\n<div class="schedule-list">\n'
    if isinstance(schedule_data, list):
        for item in schedule_data:
            html += f'  <div class="schedule-item">{item}</div>\n'
    html += '</div>\n</div>\n'
    return html

"""
make_template.py — One-time script to convert the v2.5 reference HTML into a
true generator template with injection placeholders.

Run from the repo root:
    python scripts/make_template.py

What it does:
  1. Replaces the hardcoded trip title with <!--TRIP_TITLE-->
  2. Replaces the hardcoded nav tabs+map link with <!--NAV_TABS-->
  3. Replaces all destination sections inside <main> with <!--DESTINATION_SECTIONS-->
  4. Replaces the hardcoded Leaflet stops array with '<!--MAP_MARKERS_JSON-->'
  5. Empties DRIVE_DESCRIPTIONS to {} (assembler re-populates at runtime)
  6. Recomputes SHA-256 and updates templates/checksums.txt
"""
import hashlib
import re
import sys
from pathlib import Path

TEMPLATE = Path("templates/v2.5_template.html")
CHECKSUMS = Path("templates/checksums.txt")

html = TEMPLATE.read_text(encoding="utf-8")
original_len = len(html)

# ── 1. Trip title ─────────────────────────────────────────────────────────────
html = html.replace(
    ">Southwest Road Trip</h1>",
    "><!--TRIP_TITLE--></h1>",
    1,
)

# ── 2. Nav tabs: replace only the buttons+map-link inside the flex container ──
# The outer wrapper div remains; only the hardcoded children are replaced.
nav_old = """\
          <button data-id="19" class="tab-btn active" data-tab="section-zion">1 · Zion</button>
          <button data-id="20" class="tab-btn" data-tab="section-bryce">2 · Bryce</button>
          <button data-id="21" class="tab-btn" data-tab="section-capitolreef">3 · Capitol Reef</button>
          <button data-id="22" class="tab-btn" data-tab="section-moab">4 · Moab</button>
          <button data-id="23" class="tab-btn" data-tab="section-telluride">5 · Telluride</button>
          <button data-id="24" class="tab-btn" data-tab="section-pagosa">6 · Pagosa</button>
          <button data-id="25" class="tab-btn" data-tab="section-santafe">7 · Santa Fe</button>
          <a data-id="26" href="https://www.google.com/maps/dir/Zion+National+Park,+UT/Bryce+Canyon+National+Park,+UT/Capitol+Reef+National+Park,+UT/Moab,+UT/Telluride,+CO/Pagosa+Springs,+CO/Santa+Fe,+NM" target="_blank" rel="noopener" class="map-tab-btn">
            🗺️ Full Route Map
          </a>"""
nav_new = "          <!--NAV_TABS-->"

if nav_old not in html:
    print("ERROR: Could not find nav tabs block", file=sys.stderr)
    sys.exit(1)
html = html.replace(nav_old, nav_new, 1)

# ── 3. Destination sections: replace everything inside <main> ─────────────────
# Match from the first comment after <main> to just before </main>
main_inner_pat = re.compile(
    r'(<main[^>]*>\s*)<!-- ═+.*?(\s*</main>)',
    re.DOTALL,
)
m = main_inner_pat.search(html)
if not m:
    print("ERROR: Could not locate <main> section block", file=sys.stderr)
    sys.exit(1)
html = html[:m.start()] + m.group(1) + "<!--DESTINATION_SECTIONS-->" + m.group(2) + html[m.end():]

# ── 4. Leaflet map stops array ────────────────────────────────────────────────
# Replace the hardcoded stops literal with a JSON placeholder.
stops_pat = re.compile(r'var stops=\[.*?\];', re.DOTALL)
stops_m = stops_pat.search(html)
if not stops_m:
    print("ERROR: Could not locate Leaflet stops array", file=sys.stderr)
    sys.exit(1)
html = html[:stops_m.start()] + "var stops='<!--MAP_MARKERS_JSON-->';" + html[stops_m.end():]

# ── 5. Empty DRIVE_DESCRIPTIONS ───────────────────────────────────────────────
# Brace-scan to find the full dict literal and replace with {}
start_marker = "var DRIVE_DESCRIPTIONS = {"
start_idx = html.find(start_marker)
if start_idx == -1:
    print("ERROR: Could not find DRIVE_DESCRIPTIONS", file=sys.stderr)
    sys.exit(1)

end_idx = start_idx + len(start_marker)
depth = 1
in_str = False
escape = False
while end_idx < len(html) and depth > 0:
    ch = html[end_idx]
    if escape:
        escape = False
    elif ch == "\\":
        escape = True
    elif ch == '"':
        in_str = not in_str
    elif not in_str:
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
    end_idx += 1

# end_idx now points just after the closing }
# Consume optional whitespace and ;
semicolon_idx = end_idx
while semicolon_idx < len(html) and html[semicolon_idx] in (" ", "\t"):
    semicolon_idx += 1
if semicolon_idx < len(html) and html[semicolon_idx] == ";":
    semicolon_idx += 1

html = html[:start_idx] + "var DRIVE_DESCRIPTIONS = {};" + html[semicolon_idx:]

# ── 6. Recompute checksum and write ──────────────────────────────────────────
new_hash = hashlib.sha256(html.encode("utf-8")).hexdigest()
TEMPLATE.write_text(html, encoding="utf-8")
CHECKSUMS.write_text(f"{new_hash}  templates/v2.5_template.html\n", encoding="utf-8")

print(f"Template converted:  {original_len:,} → {len(html):,} bytes")
print(f"New checksum:        {new_hash}")
print("Written: templates/v2.5_template.html")
print("Written: templates/checksums.txt")

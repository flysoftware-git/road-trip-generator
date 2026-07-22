"""Tests for generator.html_validator"""
import json
import pytest
from pathlib import Path
from generator.html_validator import HTMLValidator


SAMPLE_TRIP = {
    "destinations": [
        {
            "id": "zion",
            "name": "Zion National Park",
            "images": [
                {"local_path": "output/images/abc.jpg"},
                {"local_path": "output/images/def.jpg"},
            ],
            "scenic_drives": [
                {"title": "Zion Canyon Scenic Drive"}
            ],
        }
    ]
}

DRIVE_KEY = "Zion Canyon Scenic Drive"

VALID_HTML = f"""<!DOCTYPE html>
<html>
<head><title>Test</title></head>
<body>
<section id="section-zion" class="destination-section">
  <div class="dest-header">
    <div class="inner"></div>
  </div>
</section>
<script>
var DRIVE_DESCRIPTIONS = {json.dumps({DRIVE_KEY: {"title": "Zion Canyon Scenic Drive"}})};
</script>
<button class="drive-link" data-drive-title="{DRIVE_KEY}"></button>
</body>
</html>"""


def _write_html(tmp_path, html):
    p = tmp_path / "index.html"
    p.write_text(html, encoding="utf-8")
    return p


def _make_validator(tmp_path):
    # Build a minimal config.yaml for the validator
    cfg = tmp_path / "config.yaml"
    cfg.write_text("images:\n  min_per_destination: 2\n  max_per_destination: 4\n")
    return HTMLValidator(config_path=str(cfg))


def test_valid_html_passes(tmp_path):
    p = _write_html(tmp_path, VALID_HTML)
    v = _make_validator(tmp_path)
    report = v.validate(p, SAMPLE_TRIP)
    assert report["valid"] is True
    assert report["error_count"] == 0


def test_const_drive_descriptions_flagged(tmp_path):
    bad_html = VALID_HTML.replace("var DRIVE_DESCRIPTIONS", "const DRIVE_DESCRIPTIONS")
    p = _write_html(tmp_path, bad_html)
    v = _make_validator(tmp_path)
    report = v.validate(p, SAMPLE_TRIP)
    assert any("const" in e for e in report["errors"])


def test_missing_drive_descriptions_flagged(tmp_path):
    bad_html = VALID_HTML.replace("var DRIVE_DESCRIPTIONS", "// removed")
    p = _write_html(tmp_path, bad_html)
    v = _make_validator(tmp_path)
    report = v.validate(p, SAMPLE_TRIP)
    assert any("DRIVE_DESCRIPTIONS" in e for e in report["errors"])


def test_image_count_below_min_flagged(tmp_path):
    trip_with_one_image = {
        "destinations": [
            {
                "id": "zion",
                "name": "Zion National Park",
                "images": [{"local_path": "output/images/abc.jpg"}],
                "scenic_drives": [{"title": "Zion Canyon Scenic Drive"}],
            }
        ]
    }
    p = _write_html(tmp_path, VALID_HTML)
    v = _make_validator(tmp_path)
    report = v.validate(p, trip_with_one_image)
    assert any("image" in e.lower() or "minimum" in e.lower() for e in report["errors"])


def test_div_imbalance_flagged(tmp_path):
    bad_html = VALID_HTML.replace(
        '<section id="section-zion" class="destination-section">\n  <div class="dest-header">\n    <div class="inner"></div>\n  </div>\n</section>',
        '<section id="section-zion" class="destination-section">\n  <div class="dest-header">\n    <div class="inner">\n  </div>\n</section>'
    )
    p = _write_html(tmp_path, bad_html)
    v = _make_validator(tmp_path)
    report = v.validate(p, SAMPLE_TRIP)
    assert any("zion" in e.lower() or "div" in e.lower() for e in report["errors"])


def test_orphan_script_in_section_warns(tmp_path):
    bad_html = VALID_HTML.replace(
        '<section id="section-zion" class="destination-section">',
        '<section id="section-zion" class="destination-section"><script>alert(1)</script>'
    )
    p = _write_html(tmp_path, bad_html)
    v = _make_validator(tmp_path)
    report = v.validate(p, SAMPLE_TRIP)
    assert any("script" in w.lower() for w in report["warnings"])


def test_nested_drive_descriptions_json_parses(tmp_path):
    nested = {
        DRIVE_KEY: {
            "title": "Zion Canyon Scenic Drive",
            "description": "Contains nested JSON-like blocks",
            "meta": {"season": "fall", "difficulty": {"level": "easy"}},
        }
    }
    html = VALID_HTML.replace(
        json.dumps({DRIVE_KEY: {"title": "Zion Canyon Scenic Drive"}}),
        json.dumps(nested),
    )
    p = _write_html(tmp_path, html)
    v = _make_validator(tmp_path)
    report = v.validate(p, SAMPLE_TRIP)
    assert report["valid"] is True

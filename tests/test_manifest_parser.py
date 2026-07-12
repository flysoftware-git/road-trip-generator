"""Tests for generator.manifest_parser"""
import pytest
from pathlib import Path
from generator.manifest_parser import ManifestParser

FIXTURES = Path(__file__).parent / "fixtures"


def test_load_valid_manifest():
    parser = ManifestParser()
    trip = parser.load(str(FIXTURES / "sample_manifest.yaml"))
    assert trip["trip"]["title"] == "Test Road Trip"
    assert len(trip["destinations"]) == 2
    assert trip["destinations"][0]["id"] == "zion"


def test_destinations_have_required_fields():
    parser = ManifestParser()
    trip = parser.load(str(FIXTURES / "sample_manifest.yaml"))
    for dest in trip["destinations"]:
        assert "id" in dest
        assert "name" in dest
        assert "dates" in dest
        assert "planning_links" in dest


def test_seed_urls_rejected(tmp_path):
    manifest_content = """
trip:
  title: "Test"
  subtitle: "Test"
  theme_color: "#123456"
destinations:
  - id: test
    name: "Test Destination"
    dates: "Jan 1–3, 2026"
    planning_links: []
    seeds:
      - "https://alltrails.com/trail/test"
"""
    f = tmp_path / "bad_manifest.yaml"
    f.write_text(manifest_content)
    parser = ManifestParser()
    with pytest.raises(ValueError, match="URL"):
        parser.load(str(f))


def test_duplicate_ids_rejected(tmp_path):
    manifest_content = """
trip:
  title: "Test"
  subtitle: "Test"
  theme_color: "#123456"
destinations:
  - id: dup
    name: "Destination A"
    dates: "Jan 1–2, 2026"
    planning_links: []
  - id: dup
    name: "Destination B"
    dates: "Jan 3–4, 2026"
    planning_links: []
"""
    f = tmp_path / "dup_manifest.yaml"
    f.write_text(manifest_content)
    parser = ManifestParser()
    with pytest.raises(ValueError, match="duplicate"):
        parser.load(str(f))


def test_missing_required_trip_field(tmp_path):
    manifest_content = """
trip:
  subtitle: "Missing title"
  theme_color: "#000"
destinations: []
"""
    f = tmp_path / "missing_field.yaml"
    f.write_text(manifest_content)
    parser = ManifestParser()
    with pytest.raises(Exception):
        parser.load(str(f))

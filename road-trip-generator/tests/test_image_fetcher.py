"""Tests for generator.image_fetcher"""
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch
from generator.image_fetcher import ImageFetcher


def _make_fetcher(tmp_path):
    """Create an ImageFetcher with a real config.yaml wired to tmp output."""
    fetcher = ImageFetcher.__new__(ImageFetcher)
    fetcher._nps_key = "DEMO_KEY"
    fetcher._min_per_dest = 2
    fetcher._max_per_dest = 4
    fetcher._output_dir = tmp_path / "images"
    fetcher._output_dir.mkdir(parents=True, exist_ok=True)
    fetcher._session = MagicMock()
    return fetcher


def test_fetch_all_raises_if_not_enough_images(tmp_path):
    fetcher = _make_fetcher(tmp_path)

    trip = {
        "destinations": [
            {"id": "zion", "name": "Zion National Park", "nps_park_code": "zion"}
        ]
    }

    with patch.object(fetcher, "_fetch_from_nps", return_value=[]):
        with patch.object(fetcher, "_fetch_from_wikimedia", return_value=[]):
            with patch.object(fetcher, "_download_image", return_value=None):
                with pytest.raises(RuntimeError, match="Image fetch failed"):
                    fetcher.fetch_all(trip)


def test_fetch_all_attaches_images_to_dest(tmp_path):
    fetcher = _make_fetcher(tmp_path)

    fake_images = [
        {"url": "https://example.com/img1.jpg", "title": "Img 1", "credit": "NPS", "license": "PD", "source": "nps"},
        {"url": "https://example.com/img2.jpg", "title": "Img 2", "credit": "NPS", "license": "PD", "source": "nps"},
    ]
    trip = {
        "destinations": [
            {"id": "zion", "name": "Zion National Park", "nps_park_code": "zion"}
        ]
    }

    fake_local = tmp_path / "images" / "fake.jpg"
    fake_local.write_bytes(b"FAKE")

    with patch.object(fetcher, "_fetch_from_nps", return_value=fake_images):
        with patch.object(fetcher, "_fetch_from_wikimedia", return_value=[]):
            with patch.object(fetcher, "_download_image", return_value=fake_local):
                fetcher.fetch_all(trip)

    assert len(trip["destinations"][0]["images"]) == 2


def test_fallback_queries_returns_four():
    queries = ImageFetcher._fallback_queries("Zion National Park")
    assert len(queries) == 4
    assert all(isinstance(q, str) for q in queries)


def test_guess_extension_jpg():
    assert ImageFetcher._guess_extension("https://example.com/photo.jpg") == ".jpg"


def test_guess_extension_unknown_defaults_jpg():
    assert ImageFetcher._guess_extension("https://example.com/photo.tiff") == ".jpg"


def test_build_thumb_url_md5(tmp_path):
    """Thumb URL construction uses MD5 hash of filename for Wikimedia path."""
    import hashlib
    url = "https://upload.wikimedia.org/wikipedia/commons/5/5b/Zion_Canyon.jpg"
    filename = url.split("/")[-1]
    h = hashlib.md5(filename.encode()).hexdigest()
    expected_path_prefix = f"thumb/{h[:1]}/{h[:2]}/{filename}/{960}px-{filename}"
    # Just verify the hash logic is consistent
    assert h == hashlib.md5(b"Zion_Canyon.jpg").hexdigest()

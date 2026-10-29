"""Unit tests for Phase 5 image research and visual analysis.

All network and model calls are mocked; the suite is hermetic and requires
no external services, credentials, or uncontrolled Internet access.
"""

from __future__ import annotations

import json
import os
import struct
import unittest
from unittest import mock

from agletras_mcp import images
from agletras_mcp.config import load_settings
from agletras_mcp.errors import ToolError

from tests.support import StrictHostTestsMixin, TempHomeCase


def make_png(width: int = 800, height: int = 600) -> bytes:
    """Build a minimal, spec-valid-enough PNG (signature + IHDR only)."""
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = b"IHDR" + struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    chunk = struct.pack(">I", len(ihdr)) + ihdr + b"\x00\x00\x00\x00"
    return sig + chunk


WIKIMIDIA_SEARCH_JSON = json.dumps(
    {
        "query": {
            "search": [
                {"title": "File:Widget product.jpg", "snippet": "A widget product photo"},
                {"title": "File:Widget product 2.png", "snippet": "Another angle"},
            ]
        }
    }
)

WIKIMIDIA_IMAGEINFO_JSON = json.dumps(
    {
        "query": {
            "pages": {
                "1": {
                    "title": "File:Widget product.jpg",
                    "imageinfo": [
                        {
                            "url": "https://upload.wikimedia.org/wikipedia/commons/thumb/w/Widget_product.jpg/800px-Widget_product.jpg",
                            "width": 800,
                            "height": 600,
                            "mime": "image/jpeg",
                            "size": 45000,
                            "extmetadata": {
                                "Artist": {"value": "Jane Photographer"},
                                "Credit": {"value": "Wikimedia Commons"},
                                "LicenseShortName": {"value": "CC BY-SA 4.0"},
                                "LicenseUrl": {
                                    "value": "https://creativecommons.org/licenses/by-sa/4.0/"
                                },
                                "ImageDescription": {"value": "A widget product on a white background"},
                            },
                        }
                    ],
                },
                "2": {
                    "title": "File:Widget product 2.png",
                    "imageinfo": [
                        {
                            "url": "https://upload.wikimedia.org/wikipedia/commons/thumb/w/Widget_product_2.png/600px-Widget_product_2.png",
                            "width": 600,
                            "height": 400,
                            "mime": "image/png",
                            "size": 20000,
                            "extmetadata": {},
                        }
                    ],
                },
            }
        }
    }
)


def fake_fetch_bytes(search_body: str, info_body: str):
    """Build a webfetch.fetch_bytes replacement keyed by request URL."""

    def _fake(url, settings, *, max_bytes=None, data=None):
        if "list=search" in url:
            return url, "application/json", search_body.encode("utf-8")
        if "prop=imageinfo" in url:
            return url, "application/json", info_body.encode("utf-8")
        raise AssertionError(f"unexpected Wikimedia fetch URL: {url!r}")

    return _fake


class ImageSearchTests(TempHomeCase):
    """search_images: discovery, normalization, dedupe, missing metadata."""

    def setUp(self) -> None:
        super().setUp()
        self.settings = load_settings()

    def test_search_returns_source_linked_metadata(self) -> None:
        with mock.patch(
            "agletras_mcp.images.webfetch.fetch_bytes",
            fake_fetch_bytes(WIKIMIDIA_SEARCH_JSON, WIKIMIDIA_IMAGEINFO_JSON),
        ):
            result = images.search_images("widget product", settings=self.settings)

        self.assertTrue(result["ok"])
        self.assertEqual(result["query"], "widget product")
        self.assertEqual(result["provider"], "wikimedia_commons")
        self.assertEqual(result["count"], 2)
        self.assertIsNone(result["provider_limitation"])

        first = result["results"][0]
        self.assertEqual(first["result_id"], "img:" + images._sha256_hex(first["image_url"].encode())[:16])
        self.assertTrue(first["image_url"].startswith("https://upload.wikimedia.org/"))
        self.assertTrue(first["source_page_url"].startswith("https://commons.wikimedia.org/wiki/"))
        self.assertEqual(first["title"], "File:Widget product.jpg")
        self.assertEqual(first["creator"], "Jane Photographer")
        self.assertEqual(first["width"], 800)
        self.assertEqual(first["height"], 600)
        self.assertEqual(first["mime_type"], "image/jpeg")
        self.assertEqual(first["license_name"], "CC BY-SA 4.0")
        self.assertEqual(first["license_url"], "https://creativecommons.org/licenses/by-sa/4.0/")
        self.assertIsNotNone(first["attribution"])
        self.assertIn("retrieved_at", first)
        self.assertEqual(first["provider"], "wikimedia_commons")
        # All required fields present, none invented.
        for key in (
            "result_id", "image_url", "source_page_url", "title", "creator",
            "retrieved_at", "width", "height", "mime_type", "license_name",
            "license_url", "attribution", "provider", "missing_metadata",
        ):
            self.assertIn(key, first)

    def test_search_deduplicates_by_image_url(self) -> None:
        # Two search hits that resolve to the same image URL collapse to one.
        info_dup = json.dumps(
            {
                "query": {
                    "pages": {
                        "1": {
                            "title": "File:Widget product.jpg",
                            "imageinfo": [
                                {
                                    "url": "https://upload.wikimedia.org/wikipedia/commons/thumb/w/Widget_product.jpg/800px-Widget_product.jpg",
                                    "width": 800,
                                    "height": 600,
                                    "mime": "image/jpeg",
                                    "size": 45000,
                                    "extmetadata": {"Artist": {"value": "Jane Photographer"}},
                                }
                            ],
                        }
                    }
                }
            }
        )
        with mock.patch(
            "agletras_mcp.images.webfetch.fetch_bytes",
            fake_fetch_bytes(WIKIMIDIA_SEARCH_JSON, info_dup),
        ):
            result = images.search_images("widget product", settings=self.settings)
        self.assertEqual(result["count"], 1)
        urls = [r["image_url"] for r in result["results"]]
        self.assertEqual(len(urls), len(set(urls)))

    def test_search_missing_license_reported_honestly(self) -> None:
        # Second hit has empty extmetadata -> license/creator unknown, missing_metadata lists them.
        with mock.patch(
            "agletras_mcp.images.webfetch.fetch_bytes",
            fake_fetch_bytes(WIKIMIDIA_SEARCH_JSON, WIKIMIDIA_IMAGEINFO_JSON),
        ):
            result = images.search_images("widget product", settings=self.settings)
        second = result["results"][1]
        self.assertIsNone(second["license_name"])
        self.assertIsNone(second["creator"])
        self.assertIn("license_name", second["missing_metadata"])
        self.assertIn("creator", second["missing_metadata"])
        # Attribution may fall back to the title; nothing is fabricated.
        self.assertIsNotNone(second["attribution"])

    def test_search_empty_results(self) -> None:
        empty_search = json.dumps({"query": {"search": []}})
        empty_info = json.dumps({"query": {"pages": {}}})
        with mock.patch(
            "agletras_mcp.images.webfetch.fetch_bytes",
            fake_fetch_bytes(empty_search, empty_info),
        ):
            result = images.search_images("nothing here", settings=self.settings)
        self.assertTrue(result["ok"])
        self.assertEqual(result["count"], 0)
        self.assertEqual(result["results"], [])

    def test_search_provider_failure_reported(self) -> None:
        def _fake(url, settings, *, max_bytes=None, data=None):
            # The search API call itself fails -> provider limitation reported.
            raise ToolError("network_error", "DNS failure")

        with mock.patch("agletras_mcp.images.webfetch.fetch_bytes", _fake):
            result = images.search_images("widget product", settings=self.settings)
        self.assertEqual(result["count"], 0)
        self.assertIsNotNone(result["provider_limitation"])
        self.assertEqual(result["results"], [])

    def test_unknown_provider_rejected(self) -> None:
        with self.assertRaises(ToolError) as ctx:
            images.search_images("widget", provider="bogus", settings=self.settings)
        self.assertEqual(ctx.exception.code, "invalid_argument")

    def test_stable_image_id_deterministic(self) -> None:
        url = "https://example.com/img.jpg"
        self.assertEqual(images._stable_image_id(url), images._stable_image_id(url))
        self.assertNotEqual(images._stable_image_id(url), images._stable_image_id("https://example.com/other.jpg"))


class ImageMetadataTests(TempHomeCase):
    """Dimension parsing, MIME detection, and missing-metadata indicators."""

    def setUp(self) -> None:
        super().setUp()
        self.settings = load_settings()

    def test_png_dimensions_parsed(self) -> None:
        body = make_png(1024, 768)
        width, height = images.parse_image_dimensions(body)
        self.assertEqual((width, height), (1024, 768))

    def test_gif_dimensions_parsed(self) -> None:
        body = b"GIF89a" + struct.pack("<HH", 200, 150) + b"\x00" * 10
        width, height = images.parse_image_dimensions(body)
        self.assertEqual((width, height), (200, 150))

    def test_bmp_dimensions_parsed(self) -> None:
        # BMP: 2-byte sig + 4 size + 4 reserved + 4 offset + 4 dib-headersize
        # (18 bytes total), then width(4) at offset 18 and height(4) at 22.
        body = (
            b"BM"
            + b"\x00" * 4
            + b"\x00" * 4
            + b"\x00" * 4
            + b"\x00" * 4
            + struct.pack("<II", 400, 300)
        )
        width, height = images.parse_image_dimensions(body)
        self.assertEqual((width, height), (400, 300))

    def test_unknown_image_returns_none_dimensions(self) -> None:
        self.assertEqual(images.parse_image_dimensions(b"not an image"), (None, None))
        self.assertEqual(images.parse_image_dimensions(b""), (None, None))

    def test_mime_from_magic_bytes(self) -> None:
        self.assertEqual(images.detect_mime_type(make_png()), "image/png")
        self.assertEqual(images.detect_mime_type(b"\xff\xd8\xff\xe0"), "image/jpeg")
        self.assertEqual(images.detect_mime_type(b"GIF87a\x00\x00"), "image/gif")
        self.assertIsNone(images.detect_mime_type(b"random bytes"))

    def test_metadata_missing_indicators(self) -> None:
        meta = images.ImageMetadata(
            image_url="https://example.com/x.png",
            source_page_url=None,
            title=None,
            creator=None,
            retrieved_at="2026-01-01T00:00:00+00:00",
            width=None,
            height=None,
            mime_type=None,
            license_name=None,
            license_url=None,
            attribution=None,
            provider="wikimedia_commons",
            missing_metadata=["mime_type", "width", "height", "title", "creator", "license_name"],
        )
        self.assertIn("mime_type", meta.missing_metadata)
        self.assertIn("creator", meta.missing_metadata)
        self.assertIsNone(meta.width)
        self.assertIsNone(meta.license_name)


class ImageAnalysisTests(TempHomeCase):
    """analyze_image: visual analysis with vision disabled (default)."""

    def setUp(self) -> None:
        super().setUp()
        self.settings = load_settings()

    def test_analyze_single_image_vision_unavailable(self) -> None:
        png = make_png(800, 600)
        with mock.patch(
            "agletras_mcp.images._fetch_image_bytes",
            return_value=("https://example.com/widget.png", "image/png", png),
        ):
            result = images.analyze_image(
                image_url="https://example.com/widget.png", settings=self.settings
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["capability"], "vision_unavailable")
        self.assertFalse(result["vision_available"])
        self.assertEqual(result["vision_model"], "nemotron-3.5-super-vl-free")
        self.assertEqual(result["image_id"], "img:" + images._sha256_hex(b"https://example.com/widget.png")[:16])
        self.assertEqual(result["image_url"], "https://example.com/widget.png")

        # Structural observations present (facts about the file).
        obs_texts = [o["text"] for o in result["observations"]]
        self.assertIn("MIME type: image/png", obs_texts)
        self.assertIn("dimensions: 800×600 pixels", obs_texts)

        # Vision not performed: limitations say so, no visual claims.
        self.assertTrue(any("NOT inspected" in lim for lim in result["limitations"]))
        self.assertTrue(any("requires vision analysis" in uc for uc in result["unverified_claims"]))
        # inferences are empty when vision is disabled.
        self.assertEqual(result["inferences"], [])

    def test_analyze_local_file_path(self) -> None:
        target = self.home / "widget.png"
        target.write_bytes(make_png(300, 200))
        result = images.analyze_image(local_path=str(target), settings=self.settings)
        self.assertTrue(result["ok"])
        self.assertEqual(result["capability"], "vision_unavailable")
        obs_texts = [o["text"] for o in result["observations"]]
        self.assertIn("dimensions: 300×200 pixels", obs_texts)

    def test_analyze_missing_input_rejected(self) -> None:
        with self.assertRaises(ToolError) as ctx:
            images.analyze_image(settings=self.settings)
        self.assertEqual(ctx.exception.code, "invalid_argument")

    def test_analyze_too_many_images_rejected(self) -> None:
        too_many = ["https://example.com/%d.png" % i for i in range(11)]
        with self.assertRaises(ToolError) as ctx:
            images.analyze_image(
                image_urls=json.dumps(too_many), settings=self.settings
            )
        self.assertEqual(ctx.exception.code, "invalid_argument")


class ImageComparisonTests(TempHomeCase):
    """analyze_image: comparing two images reports per-image similarities/differences."""

    def setUp(self) -> None:
        super().setUp()
        self.settings = load_settings()

    def test_compare_two_images_structural(self) -> None:
        png_a = make_png(800, 600)
        png_b = make_png(600, 400)
        fetch_map = {
            "https://example.com/a.png": ("https://example.com/a.png", "image/png", png_a),
            "https://example.com/b.png": ("https://example.com/b.png", "image/png", png_b),
        }

        def _fake(url, settings, *, max_bytes=None, data=None):
            return fetch_map[url]

        with mock.patch("agletras_mcp.images._fetch_image_bytes", _fake):
            result = images.analyze_image(
                image_urls=json.dumps(["https://example.com/a.png", "https://example.com/b.png"]),
                settings=self.settings,
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["image_count"], 2)
        comparison = result["comparison"]
        # Same MIME type -> similarity; different dimensions -> difference.
        self.assertTrue(any("share MIME type" in s for s in comparison["similarities"]))
        self.assertTrue(any("800×600" in d and "600×400" in d for d in comparison["differences"]))
        # Each image has per-image metadata.
        self.assertEqual(len(result["images"]), 2)
        self.assertEqual(result["images"][0]["image_url"], "https://example.com/a.png")
        self.assertEqual(result["images"][1]["image_url"], "https://example.com/b.png")


class ImageVisionTests(TempHomeCase):
    """analyze_image: vision model enabled via explicit endpoint."""

    def setUp(self) -> None:
        super().setUp()
        os.environ["AGLETRAS_VISION_MODEL_ENDPOINT"] = "https://vision.example.local/analyze"
        os.environ["AGLETRAS_VISION_MODEL"] = "nemotron-3.5-super-vl-free"
        self.settings = load_settings()

    def tearDown(self) -> None:
        os.environ.pop("AGLETRAS_VISION_MODEL_ENDPOINT", None)
        os.environ.pop("AGLETRAS_VISION_MODEL", None)
        super().tearDown()

    def test_vision_configured_reports_available(self) -> None:
        caps = images.vision_capability(self.settings)
        self.assertTrue(caps["vision_available"])
        self.assertEqual(caps["vision_model"], "nemotron-3.5-super-vl-free")

    def test_analyze_calls_vision_endpoint_when_configured(self) -> None:
        png = make_png(400, 300)
        vision_response = json.dumps(
            {
                "observations": ["A red rectangular object centered in the frame", "white background"],
                "inferences": ["likely a product box"],
                "description": "A product on white background.",
            }
        )

        class _Resp:
            """Context-manager-capable mock HTTP response."""

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self, n=-1):
                return vision_response.encode("utf-8")

        with mock.patch(
            "agletras_mcp.images._fetch_image_bytes",
            return_value=("https://example.com/prod.png", "image/png", png),
        ), mock.patch("agletras_mcp.images.urllib.request.urlopen", return_value=_Resp()):
            result = images.analyze_image(
                image_url="https://example.com/prod.png", settings=self.settings
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["capability"], "vision_available")
        obs_texts = [o["text"] for o in result["observations"]]
        self.assertIn("A red rectangular object centered in the frame", obs_texts)
        inf_texts = [i["text"] for i in result["inferences"]]
        self.assertIn("likely a product box", inf_texts)


class ImageSafetyTests(StrictHostTestsMixin, TempHomeCase):
    """SSRF and local-file access safety."""

    def setUp(self) -> None:
        super().setUp()
        self.settings = load_settings()

    def test_rejects_localhost_url(self) -> None:
        with self.assertRaises(ToolError) as ctx:
            images.analyze_image(image_url="http://localhost:1/widget.png", settings=self.settings)
        self.assertEqual(ctx.exception.code, "blocked_host")

    def test_rejects_private_ip_url(self) -> None:
        with self.assertRaises(ToolError) as ctx:
            images.analyze_image(image_url="http://10.0.0.7/widget.png", settings=self.settings)
        self.assertEqual(ctx.exception.code, "blocked_host")

    def test_rejects_metadata_url(self) -> None:
        with self.assertRaises(ToolError) as ctx:
            images.analyze_image(image_url="http://169.254.169.254/latest/meta-data/", settings=self.settings)
        self.assertEqual(ctx.exception.code, "blocked_host")

    def test_rejects_unsupported_scheme(self) -> None:
        with self.assertRaises(ToolError) as ctx:
            images.analyze_image(image_url="ftp://example.com/widget.png", settings=self.settings)
        self.assertEqual(ctx.exception.code, "invalid_argument")

    def test_rejects_local_path_outside_roots(self) -> None:
        with self.assertRaises(ToolError) as ctx:
            images.analyze_image(local_path="/etc/hostname", settings=self.settings)
        self.assertEqual(ctx.exception.code, "access_denied")


if __name__ == "__main__":
    unittest.main()

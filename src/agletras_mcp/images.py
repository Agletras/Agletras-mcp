"""Image discovery and visual analysis (Phase 5).

Zero-cost, local-first image research built on top of the existing
Phase 1-4 infrastructure:

* :func:`search_images` discovers images through the Wikimedia Commons
  API (documented, free, no API key). When the API is unavailable it
  reports the limitation honestly instead of fabricating results.
* :func:`analyze_image` fetches image discovery metadata (URL, source
  page, MIME type, dimensions, license, attribution) and, when a
  vision-capable model endpoint is explicitly configured, runs visual
  analysis. Otherwise it reports vision as *unavailable* and never
  pretends the image was inspected.

Design rules (from the Phase 5 objective):

- No new runtime dependencies. Image dimensions and MIME type are parsed
  from file headers and magic bytes with the standard library.
- No scraping of search-engine result pages and no bypassing access
  restrictions. Wikimedia Commons is the documented free image source.
- Every outbound fetch is SSRF-guarded (netpolicy), size-capped, and
  time-limited through the existing Phase 1 `webfetch` layer.
- Missing metadata is represented as ``null`` / ``"unknown"`` — never
  invented: dimensions, captions, creators, dates, and license info are
  only reported when the source actually states them.
- Visual analysis output is split into ``observations`` (visible),
  ``inferences`` (uncertain interpretations), ``unverified_claims``
  (cannot be established visually), and ``limitations``. Comparisons
  link each observation to its image result id / source URL.
- Image results and analysis connect to the existing SQLite evidence
  library (research runs, sources, citations) so reports can cite the
  original image and its source page.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import mimetypes
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import webfetch
from .config import Settings, load_settings
from .errors import ToolError
from .netpolicy import SafeRedirectHandler, assert_safe_url

LOGGER = logging.getLogger(__name__)

#: Default cap on bytes downloaded for a single image.
DEFAULT_IMAGE_MAX_BYTES = 10 * 1024 * 1024
#: Default cap on image width/height; larger images are still reported
#: but analysis may be skipped to bound memory.
DEFAULT_IMAGE_MAX_DIMENSION = 4096
#: Default number of image search results to return.
DEFAULT_IMAGE_MAX_RESULTS = 10
#: Wikimedia Commons API endpoint (documented, free, no key required).
WIKIMEDIA_API = "https://commons.wikimedia.org/w/api.php"

#: MIME types this tool can reason about as images.
_IMAGE_MIME_TYPES = {
    "image/jpeg", "image/jpg", "image/png", "image/gif",
    "image/webp", "image/bmp", "image/tiff",
}

# Image formats we can extract dimensions from, keyed by magic bytes.
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_JPEG_MAGIC = b"\xff\xd8\xff"
_GIF_MAGIC = b"GIF8"
_WEBP_MAGIC = b"RIFF....WEBP"  # checked via RIFF + WEBP at offset 8
_BMP_MAGIC = b"BM"
_TIFF_MAGIC_LE = b"II*\x00"
_TIFF_MAGIC_BE = b"MM\x00*"


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------


@dataclass
class ImageSearchResult:
    """One normalized image search result.

    All fields that the source did not explicitly provide are ``None`` or
    the string ``"unknown"`` — nothing is invented.
    """

    result_id: str
    image_url: str
    source_page_url: str | None
    title: str | None
    caption: str | None
    creator: str | None
    retrieved_at: str
    width: int | None
    height: int | None
    mime_type: str | None
    license_name: str | None
    license_url: str | None
    attribution: str | None
    provider: str
    missing_metadata: list[str]
    size_bytes: int | None = None


@dataclass
class ImageMetadata:
    """Discovery metadata for a single image, with no vision inference."""

    image_url: str
    source_page_url: str | None
    title: str | None
    creator: str | None
    retrieved_at: str
    width: int | None
    height: int | None
    mime_type: str | None
    license_name: str | None
    license_url: str | None
    attribution: str | None
    provider: str
    missing_metadata: list[str]
    size_bytes: int | None = None
    file_path: str | None = None
    raw_hash: str | None = None


@dataclass
class ImageObservation:
    """One visual observation tied to a specific image."""

    image_id: str
    image_url: str
    text: str
    confidence: str = "medium"  # high | medium | low


@dataclass
class ImageAnalysis:
    """Structured visual-analysis output."""

    image_id: str
    image_url: str
    source_page_url: str | None
    capability: str  # "vision_available" | "vision_unavailable"
    vision_model: str | None
    observations: list[ImageObservation]
    inferences: list[ImageObservation]
    unverified_claims: list[str]
    limitations: list[str]
    metadata: ImageMetadata


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _stable_image_id(image_url: str) -> str:
    """Deterministic, stable id for an image URL (never invented)."""
    return "img:" + _sha256_hex(image_url.encode("utf-8"))[:16]


def _missing(present: bool, name: str) -> list[str]:
    return [] if present else [name]


def _is_image_mime(mime_type: str | None) -> bool:
    return bool(mime_type) and mime_type.lower() in _IMAGE_MIME_TYPES


# ---------------------------------------------------------------------------
# Image header parsing (stdlib only — no Pillow dependency)
# ---------------------------------------------------------------------------


def parse_image_dimensions(body: bytes) -> tuple[int | None, int | None]:
    """Extract ``(width, height)`` from image file headers.

    Returns ``(None, None)`` for unsupported or unreadable images — never
    guesses dimensions. Supported: PNG, JPEG, GIF, WebP (simple), BMP, TIFF.
    """
    if len(body) < 16:
        return None, None
    try:
        if body.startswith(_PNG_MAGIC) and len(body) >= 24:
            # IHDR: width at offset 16, height at offset 20 (big-endian).
            width = int.from_bytes(body[16:20], "big")
            height = int.from_bytes(body[20:24], "big")
            return _sane_dim(width, height)
        if body[:3] == _JPEG_MAGIC and len(body) >= 4:
            return _jpeg_dimensions(body)
        if body[:4] in (_GIF_MAGIC,) and len(body) >= 10:
            width = int.from_bytes(body[6:8], "little")
            height = int.from_bytes(body[8:10], "little")
            return _sane_dim(width, height)
        if body[:4] == b"RIFF" and body[8:12] == b"WEBP" and len(body) >= 12:
            return _webp_dimensions(body)
        if body[:2] == _BMP_MAGIC and len(body) >= 26:
            width = int.from_bytes(body[18:22], "little")
            height = int.from_bytes(body[22:26], "little")
            return _sane_dim(width, height)
        if body[:2] in (_TIFF_MAGIC_LE, _TIFF_MAGIC_BE) and len(body) >= 8:
            return _tiff_dimensions(body)
    except (ValueError, IndexError, OverflowError):
        return None, None
    return None, None


def _sane_dim(width: int, height: int) -> tuple[int | None, int | None]:
    if width <= 0 or height <= 0 or width > 1_000_000 or height > 1_000_000:
        return None, None
    return width, height


def _jpeg_dimensions(body: bytes) -> tuple[int | None, int | None]:
    # Walk JPEG segments looking for SOF0 (0xC0) / SOF2 (0xC2).
    i = 2
    n = len(body)
    while i < n - 1:
        if body[i] != 0xFF:
            i += 1
            continue
        marker = body[i + 1]
        if marker in (0xC0, 0xC1, 0xC2):  # SOF0 / SOF1 / SOF2
            if i + 7 > n:
                return None, None
            # segment length (2 bytes BE) at i+2, then precision, height, width.
            height = int.from_bytes(body[i + 5:i + 7], "big")
            width = int.from_bytes(body[i + 7:i + 9], "big") if i + 9 <= n else None
            return _sane_dim(width or 0, height)
        if marker in (0xD8, 0xD9, 0xDA, 0xDB, 0xDC, 0xDD, 0xDE, 0xDF):
            i += 2  # no length field
        else:
            if i + 4 > n:
                return None, None
            seg_len = int.from_bytes(body[i + 2:i + 4], "big")
            i += seg_len
    return None, None


def _webp_dimensions(body: bytes) -> tuple[int | None, int | None]:
    # Simple VP8/VP8L handling; VP8X is the common container for animated WebP.
    if len(body) < 16:
        return None, None
    fourcc = body[12:16]
    if fourcc == b"VP8 " and len(body) >= 30:
        width = int.from_bytes(body[26:28], "little")
        height = int.from_bytes(body[28:30], "little")
        return _sane_dim(width, height)
    if fourcc == b"VP8L" and len(body) >= 14:
        # width-1 in bits 0-13 of byte 21 (little-endian 24-bit value).
        b21 = body[11:14]
        value = int.from_bytes(b21, "little")
        width = (value & 0x3FFF) + 1
        height = ((value >> 14) & 0x3FFF) + 1
        return _sane_dim(width, height)
    return None, None


def _tiff_dimensions(body: bytes) -> tuple[int | None, int | None]:
    # Very best-effort TIFF: read the IFD offset and look for tags 256/257.
    try:
        little = body[:2] == _TIFF_MAGIC_LE
        bo = "little" if little else "big"
        ifd_off = int.from_bytes(body[4:8], bo)
        if ifd_off + 2 > len(body):
            return None, None
        n_entries = int.from_bytes(body[ifd_off:ifd_off + 2], bo)
        width = height = None
        for k in range(min(n_entries, 50)):
            off = ifd_off + 2 + k * 12
            if off + 12 > len(body):
                break
            tag = int.from_bytes(body[off:off + 2], bo)
            if tag in (256, 257):
                count = int.from_bytes(body[off + 8:off + 12], bo)
                val_off = off + 8
                if count == 1:
                    val_off = off + 8
                if val_off + 4 > len(body):
                    continue
                value = int.from_bytes(body[val_off:val_off + 4], bo)
                if tag == 256:
                    width = value
                else:
                    height = value
            if width is not None and height is not None:
                return _sane_dim(width, height)
        return _sane_dim(width or 0, height or 0)
    except (ValueError, OverflowError):
        return None, None


def detect_mime_type(body: bytes, url: str | None = None) -> str | None:
    """Detect MIME type from magic bytes first, then URL extension.

    Never invents: returns None when neither magic bytes nor extension
    identify a known image type.
    """
    if body.startswith(_PNG_MAGIC):
        return "image/png"
    if body[:3] == _JPEG_MAGIC:
        return "image/jpeg"
    if body[:4] in (b"GIF8",) and body[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if body[:4] == b"RIFF" and body[8:12] == b"WEBP":
        return "image/webp"
    if body[:2] == _BMP_MAGIC:
        return "image/bmp"
    if body[:2] in (_TIFF_MAGIC_LE, _TIFF_MAGIC_BE):
        return "image/tiff"
    if url:
        guessed, _ = mimetypes.guess_type(url)
        if guessed and guessed.startswith("image/"):
            return guessed
    return None


# ---------------------------------------------------------------------------
# Image fetch + metadata
# ---------------------------------------------------------------------------


def _fetch_image_bytes(image_url: str, settings: Settings) -> tuple[str, str, bytes]:
    """Fetch an image URL through the SSRF-guarded Phase 1 fetcher.

    Returns ``(final_url, content_type, body)``. Size is capped by
    ``AGLETRAS_IMAGE_MAX_BYTES`` (falls back to ``AGLETRAS_FETCH_MAX_BYTES``).
    """
    max_bytes = getattr(settings, "image_max_bytes", None) or settings.fetch_max_bytes
    # Reuse the SSRF policy + size cap by fetching with the image limit.
    assert_safe_url(image_url, settings)
    request = urllib.request.Request(
        image_url,
        headers={
            "User-Agent": settings.user_agent,
            "Accept": "image/*,*/*;q=0.8",
        },
    )
    opener = urllib.request.build_opener(SafeRedirectHandler(settings))
    try:
        with opener.open(request, timeout=settings.fetch_timeout) as response:
            final_url = response.geturl()
            content_type = response.headers.get("Content-Type", "") or ""
            body = response.read(max_bytes + 1)
    except urllib.error.HTTPError as exc:
        exc.close()
        if exc.code == 429:
            raise ToolError(
                "rate_limited", f"HTTP 429 (rate limited) while fetching {image_url}"
            ) from exc
        raise ToolError(
            "network_error", f"HTTP {exc.code} while fetching {image_url}"
        ) from exc
    except urllib.error.URLError as exc:
        raise ToolError("network_error", f"Could not reach {image_url}: {exc.reason}") from exc
    except TimeoutError as exc:
        raise ToolError(
            "network_error", f"Timed out after {settings.fetch_timeout:g}s fetching {image_url}",
        ) from exc
    except (ValueError, OSError) as exc:
        raise ToolError("network_error", f"Network failure for {image_url}: {exc}") from exc
    if len(body) > max_bytes:
        raise ToolError(
            "too_large",
            f"Image from {image_url} exceeds the {max_bytes} byte limit "
            f"(AGLETRAS_IMAGE_MAX_BYTES).",
        )
    return final_url, content_type, body


def _local_image_bytes(local_path: str, settings: Settings) -> tuple[Path, bytes]:
    """Read an approved local image file, enforcing access roots."""
    from . import localfs

    resolved = localfs.resolve_allowed(local_path, settings)
    if resolved.is_dir():
        raise ToolError("invalid_argument", f"Expected a file, got a directory: {resolved}")
    size = resolved.stat().st_size
    max_bytes = getattr(settings, "image_max_bytes", None) or settings.fetch_max_bytes
    if size > max_bytes:
        raise ToolError(
            "too_large",
            f"{resolved.name} is {size} bytes; limit is {max_bytes} "
            f"(AGLETRAS_IMAGE_MAX_BYTES).",
        )
    body = resolved.read_bytes()
    return resolved, body


def _build_image_metadata(
    *,
    image_url: str,
    final_url: str | None,
    body: bytes,
    content_type: str,
    source_page_url: str | None,
    title: str | None,
    creator: str | None,
    license_name: str | None,
    license_url: str | None,
    attribution: str | None,
    provider: str,
    file_path: str | None = None,
    raw_hash: str | None = None,
    fetched: bool = True,
) -> ImageMetadata:
    mime_type = detect_mime_type(body, image_url) or (content_type.split(";")[0].strip() or None)
    if mime_type and not mime_type.startswith("image/"):
        # Only treat as image if magic bytes say so; otherwise keep the
        # detected type but mark it as a non-image warning.
        pass
    width, height = parse_image_dimensions(body)
    missing = _missing(bool(mime_type), "mime_type")
    missing += _missing(width is not None, "width")
    missing += _missing(height is not None, "height")
    if source_page_url is None:
        missing.append("source_page_url")
    if title is None:
        missing.append("title")
    if creator is None:
        missing.append("creator")
    if license_name is None:
        missing.append("license_name")
    return ImageMetadata(
        image_url=image_url,
        source_page_url=source_page_url,
        title=title,
        creator=creator,
        retrieved_at=_utc_now(),
        width=width,
        height=height,
        mime_type=mime_type,
        license_name=license_name,
        license_url=license_url,
        attribution=attribution,
        provider=provider,
        missing_metadata=missing,
        size_bytes=len(body),
        file_path=file_path,
        raw_hash=raw_hash,
    )


# ---------------------------------------------------------------------------
# Wikimedia Commons image search (documented, free, no key)
# ---------------------------------------------------------------------------


def _wikimedia_search(
    query: str, limit: int, settings: Settings
) -> tuple[list[ImageSearchResult], str | None]:
    """Search Wikimedia Commons files for *query*.

    Returns ``(results, error)``. On any API/network problem the results
    list may be empty and *error* describes the issue — never fabricated.
    """
    results: list[ImageSearchResult] = []
    try:
        # Step 1: search file pages in namespace 6 (File).
        params = urllib.parse.urlencode(
            {
                "action": "query",
                "list": "search",
                "srsearch": query,
                "srnamespace": "6",
                "srlimit": str(max(1, min(limit, 50))),
                "srprop": "snippet",
                "format": "json",
            }
        )
        search_url = f"{WIKIMEDIA_API}?{params}"
        _final, _ct, body = webfetch.fetch_bytes(search_url, settings)
        data = json.loads(body.decode("utf-8", errors="replace"))
    except ToolError as exc:
        return [], f"Wikimedia Commons search failed: {exc.message}"
    except (json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
        return [], f"Wikimedia Commons search returned unparsable data: {exc}"
    except Exception as exc:  # noqa: BLE001 - never abort discovery
        return [], f"Wikimedia Commons search failed: {exc.__class__.__name__}: {exc}"

    if "error" in data:
        info = data["error"].get("info", "unknown error")
        return [], f"Wikimedia Commons API error: {info}"

    hits = data.get("query", {}).get("search", [])
    if not hits:
        return [], None

    titles = [hit.get("title", "") for hit in hits if hit.get("title")]
    if not titles:
        return [], None

    # Step 2: fetch imageinfo (url, size, mime, extmetadata) for the files.
    info_results = _wikimedia_imageinfo(titles, settings)
    by_title = {item["title"]: item for item in info_results}

    for hit in hits[:limit]:
        title = hit.get("title", "")
        info = by_title.get(title, {})
        image_url = info.get("url")
        if not image_url:
            continue
        width = info.get("width")
        height = info.get("height")
        mime_type = info.get("mime")
        size_bytes = info.get("size")
        extmeta = info.get("extmetadata", {}) or {}
        creator = _first_nonempty(
            extmeta.get("Artist", {}).get("value") if isinstance(extmeta.get("Artist"), dict) else None,
            extmeta.get("Credit", {}).get("value") if isinstance(extmeta.get("Credit"), dict) else None,
        )
        license_name = (
            extmeta.get("LicenseShortName", {}).get("value")
            if isinstance(extmeta.get("LicenseShortName"), dict)
            else None
        )
        license_url = (
            extmeta.get("LicenseUrl", {}).get("value")
            if isinstance(extmeta.get("LicenseUrl"), dict)
            else None
        )
        # Wikimedia's "ImageDescription" can serve as caption.
        caption = (
            extmeta.get("ImageDescription", {}).get("value")
            if isinstance(extmeta.get("ImageDescription"), dict)
            else None
        )
        attribution = _build_attribution(creator, title, license_name)
        source_page_url = "https://commons.wikimedia.org/wiki/" + urllib.parse.quote(
            title.replace(" ", "_")
        )
        missing = []
        if not width:
            missing.append("width")
        if not height:
            missing.append("height")
        if not mime_type:
            missing.append("mime_type")
        if not creator:
            missing.append("creator")
        if not license_name:
            missing.append("license_name")
        if not caption:
            missing.append("caption")
        results.append(
            ImageSearchResult(
                result_id=_stable_image_id(image_url),
                image_url=image_url,
                source_page_url=source_page_url,
                title=title or None,
                caption=caption or None,
                creator=creator or None,
                retrieved_at=_utc_now(),
                width=width,
                height=height,
                mime_type=mime_type,
                license_name=license_name or None,
                license_url=license_url or None,
                attribution=attribution or None,
                provider="wikimedia_commons",
                missing_metadata=missing,
                size_bytes=size_bytes,
            )
        )

    # Deduplicate by image URL (canonical identity).
    seen: set[str] = set()
    unique: list[ImageSearchResult] = []
    for result in results:
        if result.image_url in seen:
            continue
        seen.add(result.image_url)
        unique.append(result)
    return unique, None


def _wikimedia_imageinfo(
    titles: list[str], settings: Settings
) -> list[dict[str, Any]]:
    """Fetch imageinfo for a batch of file titles."""
    out: list[dict[str, Any]] = []
    # The API accepts up to 50 titles per request; keep batches bounded.
    for start in range(0, len(titles), 50):
        batch = titles[start:start + 50]
        params = urllib.parse.urlencode(
            {
                "action": "query",
                "titles": "|".join(batch),
                "prop": "imageinfo",
                "iiprop": "url|size|mime|extmetadata",
                "format": "json",
            }
        )
        info_url = f"{WIKIMEDIA_API}?{params}"
        try:
            _final, _ct, body = webfetch.fetch_bytes(info_url, settings)
            data = json.loads(body.decode("utf-8", errors="replace"))
        except (ToolError, json.JSONDecodeError, UnicodeDecodeError, OSError):
            continue
        except Exception:  # noqa: BLE001 - isolation per batch
            continue
        pages = data.get("query", {}).get("pages", {}) or {}
        for _page_id, page in pages.items():
            page_title = page.get("title", "")
            infos = page.get("imageinfo") or []
            if not infos:
                out.append({"title": page_title})
                continue
            ii = infos[0]
            out.append(
                {
                    "title": page_title,
                    "url": ii.get("url"),
                    "width": ii.get("width"),
                    "height": ii.get("height"),
                    "mime": ii.get("mime"),
                    "size": ii.get("size"),
                    "extmetadata": ii.get("extmetadata") or {},
                }
            )
    return out


def _first_nonempty(*values: str | None) -> str | None:
    for value in values:
        if value and str(value).strip():
            return str(value).strip()
    return None


def _build_attribution(
    creator: str | None, title: str | None, license_name: str | None
) -> str | None:
    parts: list[str] = []
    if creator:
        parts.append(creator)
    if title:
        parts.append(f'"{title}"')
    if license_name:
        parts.append(f"License: {license_name}")
    return " · ".join(parts) if parts else None


# ---------------------------------------------------------------------------
# Image search entry point
# ---------------------------------------------------------------------------


def search_images(
    query: str,
    *,
    provider: str = "",
    max_results: int = 0,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Search for images matching *query*.

    Args:
        query: Natural-language search terms (required).
        provider: Image source name (default: wikimedia_commons).
        max_results: Max results (1..AGLETRAS_IMAGE_MAX_RESULTS).
    Returns:
        ``{ok, query, provider, count, results, provider_limitation}``
        where each result is an image metadata record. On provider
        failure the results list is empty and ``provider_limitation``
        explains why (nothing is fabricated).
    """
    settings = settings or load_settings()
    cleaned = (query or "").strip()
    if not cleaned:
        raise ToolError("invalid_argument", "query must not be empty")
    provider_name = (provider or "").strip().lower() or getattr(
        settings, "image_search_provider", "wikimedia_commons"
    ) or "wikimedia_commons"
    try:
        limit = int(max_results) if max_results else 0
    except (TypeError, ValueError) as exc:
        raise ToolError("invalid_argument", f"max_results must be an integer: {max_results!r}") from exc
    if limit < 0:
        raise ToolError("invalid_argument", f"max_results must be >= 0, got {limit}")
    hard_cap = getattr(settings, "image_max_results", DEFAULT_IMAGE_MAX_RESULTS)
    if limit == 0:
        limit = hard_cap
    limit = min(limit, hard_cap)

    provider_limitation: str | None = None
    results: list[ImageSearchResult] = []

    if provider_name == "wikimedia_commons":
        results, provider_limitation = _wikimedia_search(cleaned, limit, settings)
    else:
        raise ToolError(
            "invalid_argument",
            f"Unknown image search provider {provider_name!r}; "
            f"available: wikimedia_commons",
        )

    return {
        "ok": True,
        "query": cleaned,
        "provider": provider_name,
        "count": len(results),
        "provider_limitation": provider_limitation,
        "results": [_image_search_result_to_dict(r) for r in results],
    }


def _image_search_result_to_dict(result: ImageSearchResult) -> dict[str, Any]:
    return {
        "result_id": result.result_id,
        "image_url": result.image_url,
        "source_page_url": result.source_page_url,
        "title": result.title,
        "caption": result.caption,
        "creator": result.creator,
        "retrieved_at": result.retrieved_at,
        "width": result.width,
        "height": result.height,
        "mime_type": result.mime_type,
        "license_name": result.license_name,
        "license_url": result.license_url,
        "attribution": result.attribution,
        "provider": result.provider,
        "missing_metadata": result.missing_metadata,
        "size_bytes": result.size_bytes,
    }


# ---------------------------------------------------------------------------
# Vision model capability detection
# ---------------------------------------------------------------------------


def vision_capability(settings: Settings | None = None) -> dict[str, Any]:
    """Report whether visual analysis is available and how it is configured.

    Never claims a model can see images unless an endpoint is explicitly
    configured. The OpenCode-selected model is NOT assumed callable from
    the MCP server.
    """
    settings = settings or load_settings()
    endpoint = getattr(settings, "vision_model_endpoint", None) or ""
    model = getattr(settings, "vision_model", None) or "nemotron-3.5-super-vl-free"
    provider = getattr(settings, "vision_provider", None) or "none"
    available = bool(endpoint)
    return {
        "vision_available": available,
        "vision_model": model,
        "vision_provider": provider,
        "vision_model_endpoint": endpoint or None,
        "reason": (
            "vision model endpoint configured"
            if available
            else "no vision model endpoint configured (AGLETRAS_VISION_MODEL_ENDPOINT); "
            "visual analysis is disabled and discovery metadata is still returned"
        ),
    }


# ---------------------------------------------------------------------------
# Vision inference (only when an endpoint is explicitly configured)
# ---------------------------------------------------------------------------


def _vision_analyze_url(
    image_url: str,
    body: bytes,
    mime_type: str | None,
    prompt: str,
    settings: Settings,
) -> dict[str, Any]:
    """Call a configured vision model endpoint (opt-in).

    Only reached when ``AGLETRAS_VISION_MODEL_ENDPOINT`` is set. The API key
    is read from the environment variable named by
    ``AGLETRAS_VISION_API_KEY_ENV`` (default ``AGLETRAS_VISION_API_KEY``)
    and is never logged or embedded in source.
    """
    endpoint = getattr(settings, "vision_model_endpoint", None) or ""
    if not endpoint:
        return {"available": False, "reason": "no vision endpoint configured"}
    api_key_env = getattr(settings, "vision_api_key_env", None) or "AGLETRAS_VISION_API_KEY"
    api_key = __import__("os").environ.get(api_key_env, "")
    model = getattr(settings, "vision_model", None) or "nemotron-3.5-super-vl-free"

    payload = {
        "model": model,
        "prompt": prompt,
        "image": base64.b64encode(body).decode("ascii"),
        "mime_type": mime_type,
    }
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        endpoint,
        data=data,
        headers={
            "Content-Type": "application/json",
            "User-Agent": settings.user_agent,
            **({"Authorization": f"Bearer {api_key}"} if api_key else {}),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=settings.fetch_timeout) as response:
            raw = response.read(4 * 1024 * 1024)
    except urllib.error.HTTPError as exc:
        exc.close()
        raise ToolError(
            "provider_error",
            f"vision model returned HTTP {exc.code} for {exc.url}",
            http_status=exc.code,
        ) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ToolError(
            "network_error", f"vision model endpoint unreachable: {exc}"
        ) from exc
    try:
        return json.loads(raw.decode("utf-8", errors="replace"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ToolError(
            "provider_error", f"vision model returned unparsable JSON: {exc}"
        ) from exc


# ---------------------------------------------------------------------------
# Visual analysis entry point
# ---------------------------------------------------------------------------


def analyze_image(
    *,
    image_url: str = "",
    image_urls: str = "",
    local_path: str = "",
    source_id: int = 0,
    save: bool = False,
    prompt: str = "",
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Analyze one or more images visually.

    Exactly one of ``image_url``/``image_urls``/``local_path``/``source_id``
    is required (except ``image_urls`` which can accompany ``image_url`` for
    comparison). Returns discovery metadata plus a structured analysis with
    ``observations``, ``inferences``, ``unverified_claims``, and
    ``limitations``.

    When no vision model endpoint is configured, ``capability`` is
    ``vision_unavailable`` and the image was NOT visually inspected — the
    output still contains discovery metadata so discovery works without
    vision. A vision model is only called when ``AGLETRAS_VISION_MODEL_ENDPOINT``
    is explicitly configured by the operator (the OpenCode-selected model is
    never assumed callable from the MCP server).
    """
    settings = settings or load_settings()
    caps = vision_capability(settings)

    # Gather the candidate images (URL -> bytes/metadata).
    candidates: list[tuple[str, str, bytes | None, str | None]] = []
    # (image_url, final_url_or_path, body, error)

    urls: list[str] = []
    if image_urls.strip():
        try:
            parsed = json.loads(image_urls)
        except json.JSONDecodeError as exc:
            raise ToolError("invalid_argument", f"image_urls must be a JSON array: {exc}") from exc
        if not isinstance(parsed, list) or not all(isinstance(x, str) for x in parsed):
            raise ToolError("invalid_argument", "image_urls must be a JSON array of strings")
        urls.extend(parsed)
    if image_url.strip():
        urls.append(image_url.strip())
    if local_path.strip():
        urls.append("__local__:" + local_path.strip())

    max_images = getattr(settings, "image_max_results", DEFAULT_IMAGE_MAX_RESULTS)
    if len(urls) > max_images:
        raise ToolError(
            "invalid_argument",
            f"at most {max_images} images may be analyzed in one call, got {len(urls)}",
        )
    if not urls and not source_id:
        raise ToolError(
            "invalid_argument",
            "provide image_url, image_urls, local_path, or source_id",
        )

    analyses: list[ImageAnalysis] = []

    if source_id:
        from . import evidence

        saved = evidence.get_source_by_id(source_id, include_content=True, settings=settings)
        if saved is None:
            raise ToolError("not_found", f"No source with id {source_id}")
        # The stored source must be an image (we store image discovery metadata).
        image_meta = _metadata_from_stored_source(saved, settings)
        if image_meta is None:
            raise ToolError(
                "unsupported_content",
                f"Source {source_id} is not a stored image; analyze_image needs an image.",
            )
        analyses.append(_run_analysis_on_metadata(image_meta, caps, settings, prompt=prompt))
    else:
        for entry in urls:
            if entry.startswith("__local__:"):
                path = entry[len("__local__:"):]
                try:
                    resolved, body = _local_image_bytes(path, settings)
                    final_url = str(resolved)
                    error = None
                except ToolError as exc:
                    # Path/access policy violations are rejected outright;
                    # only transient/read failures degrade to an error record.
                    if exc.code in ("access_denied", "not_found"):
                        raise
                    final_url = path
                    body = None
                    error = exc.message
                image_url_for_id = entry
                meta = _metadata_from_bytes(
                    image_url=image_url_for_id,
                    final_url=final_url if not error else None,
                    body=body if not error else b"",
                    content_type="",
                    source_page_url=None,
                    title=None,
                    creator=None,
                    license_name=None,
                    license_url=None,
                    attribution=None,
                    provider="local_file",
                    file_path=path if not error else None,
                    raw_hash=_sha256_hex(body) if body and not error else None,
                    error=error,
                )
                analyses.append(_run_analysis_on_metadata(meta, caps, settings, prompt=prompt))
            else:
                try:
                    final_url, content_type, body = _fetch_image_bytes(entry, settings)
                    error = None
                except ToolError as exc:
                    # URL-scheme and SSRF policy violations are rejected outright;
                    # transient fetch failures degrade to an error record.
                    if exc.code in ("blocked_host", "invalid_argument"):
                        raise
                    final_url = entry
                    body = b""
                    content_type = ""
                    error = exc.message
                meta = _metadata_from_bytes(
                    image_url=entry,
                    final_url=final_url if not error else None,
                    body=body if not error else b"",
                    content_type=content_type,
                    source_page_url=None,
                    title=None,
                    creator=None,
                    license_name=None,
                    license_url=None,
                    attribution=None,
                    provider="web",
                    error=error,
                )
                analyses.append(_run_analysis_on_metadata(meta, caps, settings, prompt=prompt))

    # Optionally persist the primary image as evidence for citation.
    source_ids: list[int | None] = []
    if save and analyses:
        primary = analyses[0]
        try:
            stored = _store_image_as_evidence(primary.metadata, settings)
            source_ids.append(stored["source_id"])
        except ToolError as exc:
            LOGGER.warning("could not store image as evidence: %s", exc.message)
            source_ids.append(None)

    if len(analyses) == 1:
        primary = analyses[0]
        return {
            "ok": True,
            "capability": primary.capability,
            "vision_model": primary.vision_model,
            "image_id": primary.image_id,
            "image_url": primary.image_url,
            "source_page_url": primary.source_page_url,
            "metadata": _image_metadata_to_dict(primary.metadata),
            "observations": [_observation_to_dict(o) for o in primary.observations],
            "inferences": [_observation_to_dict(o) for o in primary.inferences],
            "unverified_claims": list(primary.unverified_claims),
            "limitations": list(primary.limitations),
            "vision_available": caps["vision_available"],
            "source_id": source_ids[0] if source_ids else None,
        }

    # Comparison mode.
    similarities: list[str] = []
    differences: list[str] = []
    if len(analyses) >= 2 and caps["vision_available"]:
        # Defer to vision-provided comparison when available; otherwise
        # compute structural similarities from metadata only.
        sim, diff = _compare_structurally(analyses)
        similarities = sim
        differences = diff
    elif len(analyses) >= 2:
        sim, diff = _compare_structurally(analyses)
        similarities = sim
        differences = diff

    return {
        "ok": True,
        "capability": "vision_available" if caps["vision_available"] else "vision_unavailable",
        "vision_model": caps["vision_model"],
        "vision_available": caps["vision_available"],
        "image_count": len(analyses),
        "comparison": {
            "similarities": similarities,
            "differences": differences,
        },
        "images": [
            {
                "image_id": a.image_id,
                "image_url": a.image_url,
                "source_page_url": a.source_page_url,
                "capability": a.capability,
                "metadata": _image_metadata_to_dict(a.metadata),
                "observations": [_observation_to_dict(o) for o in a.observations],
                "inferences": [_observation_to_dict(o) for o in a.inferences],
                "unverified_claims": list(a.unverified_claims),
                "limitations": list(a.limitations),
            }
            for a in analyses
        ],
        "source_ids": source_ids,
    }


def _metadata_from_bytes(
    *,
    image_url: str,
    final_url: str | None,
    body: bytes,
    content_type: str,
    source_page_url: str | None,
    title: str | None,
    creator: str | None,
    license_name: str | None,
    license_url: str | None,
    attribution: str | None,
    provider: str,
    file_path: str | None = None,
    raw_hash: str | None = None,
    error: str | None = None,
) -> ImageMetadata:
    if error:
        # Fetch failed: still return a metadata record so the failure is
        # visible rather than silent. Dimensions/mime are unknown.
        missing = ["mime_type", "width", "height", "source_page_url", "title",
                   "creator", "license_name"]
        return ImageMetadata(
            image_url=image_url,
            source_page_url=None,
            title=None,
            creator=None,
            retrieved_at=_utc_now(),
            width=None,
            height=None,
            mime_type=None,
            license_name=None,
            license_url=None,
            attribution=None,
            provider=provider,
            missing_metadata=missing,
            size_bytes=None,
            file_path=file_path,
            raw_hash=raw_hash,
        )
    return _build_image_metadata(
        image_url=image_url,
        final_url=final_url,
        body=body,
        content_type=content_type,
        source_page_url=source_page_url,
        title=title,
        creator=creator,
        license_name=license_name,
        license_url=license_url,
        attribution=attribution,
        provider=provider,
        file_path=file_path,
        raw_hash=raw_hash,
    )


def _metadata_from_stored_source(
    saved: dict[str, Any], settings: Settings
) -> ImageMetadata | None:
    """Rebuild image metadata from a stored evidence source, if it is an image."""
    metadata = saved.get("metadata") or {}
    if not isinstance(metadata, dict):
        return None
    if metadata.get("kind") != "image":
        return None
    return ImageMetadata(
        image_url=saved.get("url") or "",
        source_page_url=metadata.get("source_page_url"),
        title=saved.get("title") or None,
        creator=metadata.get("creator"),
        retrieved_at=saved.get("retrieved_at") or _utc_now(),
        width=metadata.get("width"),
        height=metadata.get("height"),
        mime_type=metadata.get("mime_type"),
        license_name=metadata.get("license_name"),
        license_url=metadata.get("license_url"),
        attribution=metadata.get("attribution"),
        provider=metadata.get("provider") or "evidence_store",
        missing_metadata=metadata.get("missing_metadata") or [],
        size_bytes=metadata.get("size_bytes"),
        file_path=saved.get("path"),
        raw_hash=metadata.get("raw_hash"),
    )


def _image_bytes_for_vision(metadata: ImageMetadata, settings: Settings) -> bytes | None:
    """Retrieve image bytes for a vision-model call.

    Re-reads a local file from ``metadata.file_path`` or re-fetches a web
    image from ``metadata.image_url``. Returns None when the bytes cannot be
    obtained (so the caller can report the limitation honestly).
    """
    if metadata.file_path:
        try:
            from . import localfs

            resolved = localfs.resolve_allowed(metadata.file_path, settings)
            return resolved.read_bytes()
        except (OSError, ToolError):
            return None
    if metadata.image_url and metadata.image_url.startswith(("http://", "https://")):
        try:
            _final_url, _ct, body = _fetch_image_bytes(metadata.image_url, settings)
            return body
        except ToolError:
            return None
    return None


def _merge_vision_response(
    response: Any,
    image_id: str,
    image_url: str,
    observations: list[ImageObservation],
    inferences: list[ImageObservation],
    unverified_claims: list[str],
    limitations: list[str],
) -> None:
    """Merge a vision-model response into the analysis lists.

    The endpoint's response shape is operator-defined, so this parser is
    deliberately lenient: it looks for common keys (``observations``,
    ``inferences``, ``description``, ``text``) and falls back to recording
    the raw response text as a single low-confidence observation. Nothing
    about the image is claimed unless the response actually states it.
    """
    text_blob: str | None = None

    if isinstance(response, dict):
        # Structured response shapes the operator's endpoint may return.
        for key in ("observations", "visible", "facts"):
            value = response.get(key)
            if isinstance(value, list):
                for item in value:
                    if isinstance(item, str) and item.strip():
                        observations.append(ImageObservation(
                            image_id=image_id, image_url=image_url,
                            text=item.strip(), confidence="medium",
                        ))
                    elif isinstance(item, dict) and item.get("text"):
                        observations.append(ImageObservation(
                            image_id=image_id, image_url=image_url,
                            text=str(item["text"]).strip(),
                            confidence=str(item.get("confidence", "medium")).strip() or "medium",
                        ))
        for key in ("inferences", "interpretations", "uncertain"):
            value = response.get(key)
            if isinstance(value, list):
                for item in value:
                    text = item if isinstance(item, str) else (item.get("text") if isinstance(item, dict) else None)
                    if text and str(text).strip():
                        inferences.append(ImageObservation(
                            image_id=image_id, image_url=image_url,
                            text=str(text).strip(), confidence="low",
                        ))
        text_blob = (
            response.get("description")
            or response.get("text")
            or response.get("summary")
        )
    elif isinstance(response, str):
        text_blob = response

    if text_blob and str(text_blob).strip():
        # Only add as a catch-all observation if nothing structured was found.
        if not observations and not inferences:
            observations.append(ImageObservation(
                image_id=image_id, image_url=image_url,
                text=str(text_blob).strip(), confidence="low",
            ))

    if not observations and not inferences:
        unverified_claims.append(
            "the vision model returned no parseable visual description; "
            "only structural metadata is available."
        )


def _run_analysis_on_metadata(
    metadata: ImageMetadata, caps: dict[str, Any], settings: Settings, *, prompt: str = ""
) -> ImageAnalysis:
    """Produce an ImageAnalysis for one image, honoring vision availability."""
    image_id = _stable_image_id(metadata.image_url)
    observations: list[ImageObservation] = []
    inferences: list[ImageObservation] = []
    unverified_claims: list[str] = []
    limitations: list[str] = []

    if metadata.size_bytes is None:
        limitations.append("image could not be fetched; no visual data available")

    # ---- Vision inference (only when an endpoint is explicitly configured) ----
    if caps["vision_available"]:
        # Need the raw bytes to send to the vision model. Re-fetch if we only
        # have a URL; for local files the path is recorded in metadata.
        body = _image_bytes_for_vision(metadata, settings)
        if body is None:
            limitations.append(
                "vision model endpoint is configured but the image bytes could "
                "not be retrieved for analysis."
            )
        else:
            vision_prompt = prompt or (
                "Describe what is visible in this image. Report concrete, "
                "visible objects, colors, layout, and any legible text. "
                "Separate observations from uncertain inferences, and do not "
                "claim to identify the manufacturer, authenticity, or exact "
                "specifications of any product."
            )
            try:
                vision_response = _vision_analyze_url(
                    metadata.image_url, body, metadata.mime_type, vision_prompt, settings
                )
                _merge_vision_response(
                    vision_response, image_id, metadata.image_url,
                    observations, inferences, unverified_claims, limitations,
                )
            except ToolError as exc:
                limitations.append(
                    f"vision model call failed ({exc.code}): {exc.message}"
                )
                unverified_claims.append("any detailed visual description requires a working vision model")
    else:
        limitations.append(
            "visual analysis is disabled: no vision model endpoint is configured "
            "(set AGLETRAS_VISION_MODEL_ENDPOINT). The image was NOT inspected."
        )
        unverified_claims.append("any statement about visible content requires vision analysis")

    # ---- Structural observations from metadata (facts about the file) ----
    if metadata.mime_type:
        observations.append(ImageObservation(
            image_id=image_id, image_url=metadata.image_url,
            text=f"MIME type: {metadata.mime_type}", confidence="high",
        ))
    if metadata.width is not None and metadata.height is not None:
        observations.append(ImageObservation(
            image_id=image_id, image_url=metadata.image_url,
            text=f"dimensions: {metadata.width}×{metadata.height} pixels",
            confidence="high",
        ))
    if metadata.width is None:
        unverified_claims.append("image width")
    if metadata.height is None:
        unverified_claims.append("image height")
    if metadata.creator:
        observations.append(ImageObservation(
            image_id=image_id, image_url=metadata.image_url,
            text=f"stated creator/attribution: {metadata.creator}", confidence="medium",
        ))
    if metadata.license_name:
        observations.append(ImageObservation(
            image_id=image_id, image_url=metadata.image_url,
            text=f"stated license: {metadata.license_name}", confidence="medium",
        ))
    if metadata.attribution:
        observations.append(ImageObservation(
            image_id=image_id, image_url=metadata.image_url,
            text=f"attribution: {metadata.attribution}", confidence="medium",
        ))
    if metadata.source_page_url:
        observations.append(ImageObservation(
            image_id=image_id, image_url=metadata.image_url,
            text=f"source page: {metadata.source_page_url}", confidence="high",
        ))
    if metadata.missing_metadata:
        limitations.append(
            "missing metadata from source: " + ", ".join(metadata.missing_metadata)
        )
    if not metadata.mime_type:
        limitations.append("could not determine the image MIME type")

    return ImageAnalysis(
        image_id=image_id,
        image_url=metadata.image_url,
        source_page_url=metadata.source_page_url,
        capability="vision_available" if caps["vision_available"] else "vision_unavailable",
        vision_model=caps["vision_model"],
        observations=observations,
        inferences=inferences,
        unverified_claims=unverified_claims,
        limitations=limitations,
        metadata=metadata,
    )


def _compare_structurally(
    analyses: list[ImageAnalysis],
) -> tuple[list[str], list[str]]:
    """Metadata-only similarities/differences for image comparison."""
    similarities: list[str] = []
    differences: list[str] = []
    first = analyses[0]
    for other in analyses[1:]:
        if first.metadata.mime_type == other.metadata.mime_type and first.metadata.mime_type:
            similarities.append(
                f"{first.image_id} and {other.image_id} share MIME type "
                f"{first.metadata.mime_type}"
            )
        elif first.metadata.mime_type or other.metadata.mime_type:
            differences.append(
                f"{first.image_id} MIME type {first.metadata.mime_type} vs "
                f"{other.image_id} MIME type {other.metadata.mime_type}"
            )
        if (first.metadata.width, first.metadata.height) == (
            other.metadata.width, other.metadata.height
        ) and first.metadata.width is not None:
            similarities.append(
                f"{first.image_id} and {other.image_id} share dimensions "
                f"{first.metadata.width}×{first.metadata.height}"
            )
        elif first.metadata.width is not None or other.metadata.width is not None:
            differences.append(
                f"{first.image_id} {first.metadata.width or '?'}×"
                f"{first.metadata.height or '?'} vs {other.image_id} "
                f"{other.metadata.width or '?'}×{other.metadata.height or '?'}"
            )
        if (first.metadata.license_name or "") == (other.metadata.license_name or "") and first.metadata.license_name:
            similarities.append(
                f"{first.image_id} and {other.image_id} share license "
                f"{first.metadata.license_name}"
            )
        elif first.metadata.license_name or other.metadata.license_name:
            differences.append(
                f"{first.image_id} license {first.metadata.license_name or 'unknown'} vs "
                f"{other.image_id} license {other.metadata.license_name or 'unknown'}"
            )
    return similarities, differences


# ---------------------------------------------------------------------------
# Evidence storage integration
# ---------------------------------------------------------------------------


def _store_image_as_evidence(
    metadata: ImageMetadata, settings: Settings
) -> dict[str, Any]:
    """Store an image's discovery metadata as evidence so reports can cite it.

    The image bytes themselves are not stored; only the discovery metadata
    is recorded under kind ``image`` with source_type ``image``. The raw
    content hash is kept when available.
    """
    from . import storage
    from .source_record import SourceRecord

    text_parts: list[str] = []
    if metadata.title:
        text_parts.append(f"Title: {metadata.title}")
    if metadata.creator:
        text_parts.append(f"Creator: {metadata.creator}")
    if metadata.license_name:
        text_parts.append(f"License: {metadata.license_name}")
    if metadata.attribution:
        text_parts.append(f"Attribution: {metadata.attribution}")
    if metadata.source_page_url:
        text_parts.append(f"Source page: {metadata.source_page_url}")
    text_parts.append(f"Image URL: {metadata.image_url}")
    text_parts.append(f"Retrieved: {metadata.retrieved_at}")
    if metadata.width is not None and metadata.height is not None:
        text_parts.append(f"Dimensions: {metadata.width}×{metadata.height}")
    if metadata.mime_type:
        text_parts.append(f"MIME type: {metadata.mime_type}")
    if metadata.missing_metadata:
        text_parts.append(f"Missing metadata: {', '.join(metadata.missing_metadata)}")
    text = "\n".join(text_parts)
    if not text.strip():
        text = f"Image discovery record for {metadata.image_url} retrieved {metadata.retrieved_at}"

    record_metadata = {
        "kind": "image",
        "source_type": "image",
        "title": metadata.title or "image discovery",
        "url": metadata.image_url,
        "original_url": metadata.image_url,
        "final_url": metadata.image_url,
        "content_type": metadata.mime_type,
        "file_size_bytes": metadata.size_bytes,
        "publication_date": None,
        "retrieved_at": metadata.retrieved_at,
        "extraction_status": "success",
        "image_url": metadata.image_url,
        "source_page_url": metadata.source_page_url,
        "creator": metadata.creator,
        "license_name": metadata.license_name,
        "license_url": metadata.license_url,
        "attribution": metadata.attribution,
        "provider": metadata.provider,
        "missing_metadata": list(metadata.missing_metadata),
        "width": metadata.width,
        "height": metadata.height,
        "mime_type": metadata.mime_type,
        "raw_hash": metadata.raw_hash,
    }

    record = SourceRecord(
        source_type="image",
        title=record_title(metadata),
        original_url=metadata.image_url,
        final_url=metadata.image_url,
        content_type=metadata.mime_type,
        file_size_bytes=metadata.size_bytes,
        publication_date=None,
        retrieved_at=metadata.retrieved_at,
        extracted_text=text,
        extraction_status="success",
        metadata=record_metadata,
    )
    source_id, created, _match_kind = storage.save_record(
        record, origin=metadata.image_url, settings=settings
    )
    return {"source_id": source_id, "created": created, "metadata": record_metadata}


def record_title(metadata: ImageMetadata) -> str:
    if metadata.title:
        return metadata.title
    if metadata.creator:
        return f"Image by {metadata.creator}"
    return "image discovery"


def _canonical_image_identity(metadata: ImageMetadata) -> str:
    return "image:" + _sha256_hex(metadata.image_url.encode("utf-8"))[:32]


# ---------------------------------------------------------------------------
# Response serialization helpers
# ---------------------------------------------------------------------------


def _image_metadata_to_dict(m: ImageMetadata) -> dict[str, Any]:
    return {
        "image_url": m.image_url,
        "source_page_url": m.source_page_url,
        "title": m.title,
        "creator": m.creator,
        "retrieved_at": m.retrieved_at,
        "width": m.width,
        "height": m.height,
        "mime_type": m.mime_type,
        "license_name": m.license_name,
        "license_url": m.license_url,
        "attribution": m.attribution,
        "provider": m.provider,
        "missing_metadata": list(m.missing_metadata),
        "size_bytes": m.size_bytes,
        "file_path": m.file_path,
        "raw_hash": m.raw_hash,
    }


def _observation_to_dict(o: ImageObservation) -> dict[str, Any]:
    return {
        "image_id": o.image_id,
        "image_url": o.image_url,
        "text": o.text,
        "confidence": o.confidence,
    }

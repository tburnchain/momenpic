"""Hardened fetch of Coupang product images (§4 M5 시각 소스 (2) 쿠팡 제공 상품 이미지, §10 보안).

Only HTTPS URLs on Coupang hosts are fetched (``*.coupangcdn.com`` / ``*.coupang.com``), redirects are
followed manually so every hop is re-validated, the body is streamed with a hard size cap, and the bytes
must decode as a real image. The image itself is never edited: JPEGs are stored byte-for-byte; other formats
are only transcoded to JPEG (same pixels) so the ledger key ``assets/{sha}.jpg`` is uniform.
"""

from __future__ import annotations

import io
from urllib.parse import urljoin, urlparse

import httpx
from PIL import Image, UnidentifiedImageError

from acvm.core.errors import PolicyViolation, UpstreamError, ValidationFailed

ALLOWED_HOST_SUFFIXES = ("coupangcdn.com", "coupang.com")
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_PIXELS = 40_000_000
TIMEOUT_SECONDS = 15.0
MAX_REDIRECTS = 3


def host_allowed(url: str) -> bool:
    """HTTPS + host equal to or a subdomain of an allowed Coupang domain (no userinfo, default port)."""
    u = urlparse(url)
    if u.scheme != "https" or not u.hostname or u.username or u.password:
        return False
    if u.port not in (None, 443):
        return False
    host = u.hostname.lower().rstrip(".")
    return any(host == s or host.endswith("." + s) for s in ALLOWED_HOST_SUFFIXES)


def _validate_image(data: bytes) -> tuple[bytes, str]:
    """Return (bytes_to_store, original_format). Rejects non-images and decompression bombs."""
    try:
        with Image.open(io.BytesIO(data)) as probe:
            fmt = (probe.format or "").upper()
            w, h = probe.size
            if w * h > MAX_PIXELS:
                raise ValidationFailed("product image too large", width=w, height=h)
            probe.verify()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise ValidationFailed("downloaded product image is not a valid image") from exc
    if fmt == "JPEG":
        return data, fmt
    with Image.open(io.BytesIO(data)) as im:
        rgb = im.convert("RGB")
        buf = io.BytesIO()
        rgb.save(buf, format="JPEG", quality=95, subsampling=0, optimize=False)
    return buf.getvalue(), fmt


async def fetch_product_image(url: str, *, client: httpx.AsyncClient | None = None,
                              max_bytes: int = MAX_IMAGE_BYTES,
                              time_limit: float = TIMEOUT_SECONDS) -> tuple[bytes, str]:
    """Download and validate a product image. Returns (jpeg_bytes, original_format).

    Raises PolicyViolation for a disallowed host (never retried), UpstreamError for HTTP/network failures
    (5xx/timeouts retryable) and ValidationFailed for non-image or oversized bodies.
    """
    if not host_allowed(url):
        raise PolicyViolation("product image host not allowed (coupangcdn.com / coupang.com over HTTPS only)",
                              host=urlparse(url).hostname or "")
    own = client is None
    http = client or httpx.AsyncClient(timeout=httpx.Timeout(time_limit), follow_redirects=False,
                                       headers={"User-Agent": "ACVM-AssetEngine/1.0"})
    try:
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            try:
                async with http.stream("GET", current, timeout=time_limit) as resp:
                    if resp.status_code in (301, 302, 303, 307, 308):
                        nxt = urljoin(current, resp.headers.get("location", ""))
                        if not host_allowed(nxt):
                            raise PolicyViolation("product image redirect left allowed hosts",
                                                  host=urlparse(nxt).hostname or "")
                        current = nxt
                        continue
                    if resp.status_code >= 400:
                        raise UpstreamError(f"product image HTTP {resp.status_code}", status=resp.status_code,
                                            retryable=resp.status_code >= 500 or resp.status_code == 429)
                    ctype = resp.headers.get("content-type", "").split(";")[0].strip().lower()
                    if ctype and not ctype.startswith("image/"):
                        raise ValidationFailed("product image URL did not return an image", content_type=ctype)
                    declared = int(resp.headers.get("content-length") or 0)
                    if declared > max_bytes:
                        raise ValidationFailed("product image exceeds size limit", bytes=declared)
                    buf = bytearray()
                    async for chunk in resp.aiter_bytes():
                        buf.extend(chunk)
                        if len(buf) > max_bytes:
                            raise ValidationFailed("product image exceeds size limit", bytes=len(buf))
                    return _validate_image(bytes(buf))
            except httpx.TimeoutException as exc:
                raise UpstreamError("product image download timed out", retryable=True) from exc
            except httpx.TransportError as exc:
                raise UpstreamError(f"product image download failed: {type(exc).__name__}", retryable=True) from exc
        raise UpstreamError("too many redirects fetching product image", retryable=False)
    finally:
        if own:
            await http.aclose()

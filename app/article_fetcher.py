import ipaddress
import logging
import socket

import httpx
import trafilatura

logger = logging.getLogger(__name__)

MAX_BYTES = 2_000_000
# Shorter extractions are usually paywall teasers or cookie banners, not the article.
MIN_TEXT_CHARS = 500


def require_public_host(request: httpx.Request) -> None:
    # The URL comes from the browser, so refuse anything that resolves to a private or internal
    # address (SSRF). httpx runs this hook for every redirect hop too.
    for info in socket.getaddrinfo(request.url.host, None):
        if not ipaddress.ip_address(info[4][0]).is_global:
            raise ValueError(f"refusing non-public host {request.url.host}")


def make_client(transport: httpx.BaseTransport | None = None) -> httpx.Client:
    return httpx.Client(
        transport=transport,
        timeout=8,
        follow_redirects=True,
        max_redirects=3,
        headers={"User-Agent": "NewsInsight/1.0"},
        event_hooks={"request": [require_public_host]},
    )


_default_client = make_client()


def fetch_full_text(url: str, http: httpx.Client = _default_client) -> str | None:
    """Download an article and extract its main text. Returns None on any failure."""
    try:
        with http.stream("GET", url) as response:
            if response.status_code != 200 or "html" not in response.headers.get("content-type", ""):
                return None
            body = b""
            for chunk in response.iter_bytes():
                body += chunk
                if len(body) > MAX_BYTES:
                    return None
            html = body.decode(response.encoding or "utf-8", errors="replace")
        text = trafilatura.extract(html, include_comments=False)
    # Deliberately broad: whatever goes wrong, the caller falls back to the GNews preview.
    except Exception:
        logger.warning("Full-text fetch failed for %s", url, exc_info=True)
        return None
    return text if text and len(text) >= MIN_TEXT_CHARS else None

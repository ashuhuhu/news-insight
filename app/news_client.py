import logging
from time import monotonic

import httpx
from pydantic import ValidationError

from app.errors import ApiError
from app.schemas import Article

logger = logging.getLogger(__name__)

GNEWS_SEARCH_URL = "https://gnews.io/api/v4/search"
# The free GNews plan allows 100 requests a day, so repeat searches are served from memory.
CACHE_SECONDS = 600


class NewsClient:
    def __init__(self, api_key: str, http: httpx.Client):
        self.api_key = api_key
        self.http = http
        self.cache: dict[tuple[str, int], tuple[float, list[Article]]] = {}

    def search(self, query: str, max_results: int) -> list[Article]:
        key = (query.lower(), max_results)
        cached = self.cache.get(key)
        if cached and monotonic() - cached[0] < CACHE_SECONDS:
            return cached[1]

        params = {"q": query, "lang": "en", "max": max_results, "sortby": "publishedAt", "apikey": self.api_key}
        try:
            response = self.http.get(GNEWS_SEARCH_URL, params=params, timeout=10)
        except httpx.TimeoutException:
            raise ApiError(504, "news_timeout", "The news service took too long to respond. Try again.")
        except httpx.HTTPError:
            raise ApiError(502, "news_unavailable", "The news service is unavailable right now.")

        if response.status_code == 400:
            raise ApiError(400, "invalid_query", "The news service couldn't understand that search. Try simpler keywords.")
        if response.status_code in (403, 429):
            raise ApiError(429, "news_quota_exceeded", "The daily news search limit has been reached. Try again later.")
        if response.status_code != 200:
            logger.error("GNews returned %s: %s", response.status_code, response.text[:200])
            raise ApiError(502, "news_unavailable", "The news service is unavailable right now.")

        try:
            items = response.json()["articles"]
        except (ValueError, KeyError, TypeError):
            items = None
        if not isinstance(items, list):
            raise ApiError(502, "news_unavailable", "The news service sent an unexpected response.")

        articles: list[Article] = []
        seen: set[str] = set()
        for item in items:
            try:
                article = Article(
                    url=item.get("url"),
                    title=item.get("title"),
                    description=item.get("description"),
                    content=item.get("content"),
                    source_name=(item.get("source") or {}).get("name"),
                    image_url=item.get("image"),
                    published_at=item.get("publishedAt"),
                )
            except (ValidationError, AttributeError):
                continue
            if article.url not in seen:
                seen.add(article.url)
                articles.append(article)

        self.cache[key] = (monotonic(), articles)
        return articles

import httpx
import pytest

from app import news_client
from app.errors import ApiError
from app.news_client import NewsClient
from tests.helpers import gnews_item


def client_returning(*responses, requests=None):
    """NewsClient whose HTTP calls return the given responses in order."""
    queue = list(responses)

    def handler(request: httpx.Request):
        if requests is not None:
            requests.append(request)
        response = queue.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    return NewsClient("test-key", httpx.Client(transport=httpx.MockTransport(handler)))


def test_maps_and_cleans_gnews_articles():
    items = [
        gnews_item(url="https://a.example.com/1", publishedAt="not a date", image=None, source=None),
        gnews_item(url="https://a.example.com/1", title="Duplicate URL"),
        gnews_item(url=None),
        gnews_item(title="  "),
        gnews_item(url="javascript:alert(1)"),
        "not an object",
        gnews_item(url="https://b.example.com/2"),
    ]
    articles = client_returning(httpx.Response(200, json={"articles": items})).search("solar", 10)

    assert [a.url for a in articles] == ["https://a.example.com/1", "https://b.example.com/2"]
    first, second = articles
    assert first.published_at is None and first.image_url is None and first.source_name is None
    assert second.source_name == "Example News"
    assert second.image_url == "https://news.example.com/solar.jpg"
    assert second.published_at.isoformat() == "2026-11-08T09:00:00+00:00"


def test_sends_expected_query_parameters():
    requests = []
    client_returning(httpx.Response(200, json={"articles": []}), requests=requests).search("solar power", 5)

    params = requests[0].url.params
    assert requests[0].url.host == "gnews.io"
    assert dict(params) == {"q": "solar power", "lang": "en", "max": "5", "sortby": "publishedAt", "apikey": "test-key"}


@pytest.mark.parametrize(
    "status, expected_status, expected_code",
    [
        (400, 400, "invalid_query"),
        (401, 502, "news_unavailable"),
        (403, 429, "news_quota_exceeded"),
        (429, 429, "news_quota_exceeded"),
        (500, 502, "news_unavailable"),
    ],
)
def test_maps_gnews_error_statuses(status, expected_status, expected_code):
    with pytest.raises(ApiError) as error:
        client_returning(httpx.Response(status, json={"errors": ["x"]})).search("solar", 10)
    assert (error.value.status_code, error.value.code) == (expected_status, expected_code)


@pytest.mark.parametrize(
    "response, expected_status",
    [
        (httpx.ReadTimeout("slow"), 504),
        (httpx.ConnectError("down"), 502),
        (httpx.Response(200, text="<html>not json</html>"), 502),
        (httpx.Response(200, json={"totalArticles": 0}), 502),
        (httpx.Response(200, json={"articles": None}), 502),
    ],
)
def test_transport_failures_and_bad_bodies(response, expected_status):
    with pytest.raises(ApiError) as error:
        client_returning(response).search("solar", 10)
    assert error.value.status_code == expected_status


def test_caches_successful_searches_for_ten_minutes(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(news_client, "monotonic", lambda: now[0])
    requests = []
    ok = httpx.Response(200, json={"articles": [gnews_item()]})
    client = client_returning(ok, ok, ok, requests=requests)

    client.search("Solar", 10)
    client.search("solar", 10)  # same search, different case: cached
    assert len(requests) == 1

    client.search("solar", 5)  # different size: new request
    assert len(requests) == 2

    now[0] += 601
    client.search("solar", 10)  # cache expired
    assert len(requests) == 3


def test_does_not_cache_failures():
    requests = []
    client = client_returning(
        httpx.Response(500), httpx.Response(200, json={"articles": [gnews_item()]}), requests=requests
    )
    with pytest.raises(ApiError):
        client.search("solar", 10)
    assert len(client.search("solar", 10)) == 1
    assert len(requests) == 2

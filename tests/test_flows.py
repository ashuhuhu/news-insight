"""Full-flow tests: the real app code wired together, with only the outside network faked."""
import logging
from functools import partial
from types import SimpleNamespace

import httpx
import openai
import pytest
from fastapi.testclient import TestClient

from app import article_fetcher, config
from app.analyzer import Analyzer
from app.article_fetcher import fetch_full_text, make_client
from app.main import app, get_analyzer, get_fetcher, get_news_client
from app.news_client import NewsClient
from tests.helpers import FakeOpenAI, article_html, fake_dns, gnews_item

SOLAR = "https://news.example.com/solar"
CLIMATE = "https://blocked.example.com/climate-crisis"
OPENAI_TIMEOUT = openai.APITimeoutError(request=httpx.Request("POST", "https://api.openai.com/v1/responses"))


class Network:
    """Fake GNews and news websites."""

    def __init__(self):
        self.gnews_items: list = []
        self.gnews_status = 200
        self.gnews_error: Exception | None = None
        self.gnews_calls = 0
        self.pages: dict[str, str] = {}
        self.site_requests: list[str] = []

    def gnews(self, request):
        self.gnews_calls += 1
        if self.gnews_error:
            raise self.gnews_error
        return httpx.Response(self.gnews_status, json={"articles": self.gnews_items})

    def sites(self, request):
        self.site_requests.append(str(request.url))
        html = self.pages.get(str(request.url))
        if html is None:
            return httpx.Response(403, text="Forbidden")
        return httpx.Response(200, headers={"content-type": "text/html"}, text=html)


@pytest.fixture
def flow(override_session, monkeypatch):
    monkeypatch.setattr(config, "APP_PASSWORD", "pw")
    monkeypatch.setattr(article_fetcher.socket, "getaddrinfo", fake_dns(**{"169.254.169.254": "169.254.169.254"}))
    net, fake_openai = Network(), FakeOpenAI()
    news = NewsClient("test-key", httpx.Client(transport=httpx.MockTransport(net.gnews)))
    fetch = partial(fetch_full_text, http=make_client(httpx.MockTransport(net.sites)))
    analyzer = Analyzer(fake_openai, "gpt-6-luna")
    app.dependency_overrides[get_news_client] = lambda: news
    app.dependency_overrides[get_analyzer] = lambda: analyzer
    app.dependency_overrides[get_fetcher] = lambda: fetch
    client = TestClient(app)
    client.auth = ("reviewer", "pw")
    return SimpleNamespace(client=client, net=net, openai=fake_openai)


def search(flow, q="climate"):
    response = flow.client.get("/api/articles", params={"q": q})
    assert response.status_code == 200
    return response.json()["articles"]


def test_search_analyze_and_browse_library(flow):
    flow.net.gnews_items = [
        gnews_item(url=SOLAR, title="Solar power hits record high"),
        gnews_item(url=CLIMATE, title="Climate crisis deepens", content="Floods spread across the region ... [900 chars]"),
        gnews_item(url=SOLAR, title="Same story syndicated"),
    ]
    flow.net.pages = {SOLAR: article_html()}  # the climate site blocks us
    assert flow.client.get("/api/analyses").json()["total"] == 0

    results = search(flow)
    assert [r["url"] for r in results] == [SOLAR, CLIMATE]
    assert all(r["analysis"] is None for r in results)

    solar = flow.client.post("/api/analyses", json=results[0])
    climate = flow.client.post("/api/analyses", json=results[1])
    assert (solar.status_code, climate.status_code) == (201, 201)
    assert solar.json()["text_source"] == "full"
    assert solar.json()["summary"] == "Summary based on the full article."
    assert "Paragraph 3" in flow.openai.prompts[0]
    assert climate.json()["text_source"] == "preview"
    assert climate.json()["sentiment"] == "negative"
    assert "Floods spread across the region\n" in flow.openai.prompts[1]  # GNews marker stripped

    again = search(flow, q="Climate")
    assert flow.net.gnews_calls == 1  # served from cache
    assert [r["analysis"]["id"] for r in again] == [solar.json()["id"], climate.json()["id"]]

    library = flow.client.get("/api/analyses").json()
    assert [item["url"] for item in library["items"]] == [CLIMATE, SOLAR]
    assert library["counts"] == {"positive": 1, "neutral": 0, "negative": 1}
    negative = flow.client.get("/api/analyses", params={"sentiment": "negative"}).json()
    assert [item["url"] for item in negative["items"]] == [CLIMATE]
    assert flow.client.get(f"/api/analyses/{solar.json()['id']}").json() == solar.json()


def test_analyze_all_where_one_article_fails(flow):
    flow.net.gnews_items = [gnews_item(url=f"https://news.example.com/{i}", title=f"Story {i}") for i in range(5)]
    flow.openai.error, flow.openai.fail_when = OPENAI_TIMEOUT, "Story 2"
    results = search(flow)

    statuses = [flow.client.post("/api/analyses", json=article).status_code for article in results]
    assert statuses == [201, 201, 504, 201, 201]
    assert flow.client.get("/api/analyses").json()["total"] == 4

    flow.openai.error = None
    assert flow.client.post("/api/analyses", json=results[2]).status_code == 201
    assert all(r["analysis"] for r in search(flow))


def test_reanalyze_upgrades_preview_and_keeps_result_on_failure(flow):
    flow.net.gnews_items = [gnews_item(url=CLIMATE, title="Climate crisis deepens")]
    first = flow.client.post("/api/analyses", json=search(flow)[0]).json()
    assert first["text_source"] == "preview"

    flow.net.pages[CLIMATE] = article_html()  # the site is reachable now
    upgraded = flow.client.post(f"/api/analyses/{first['id']}/reanalyze").json()
    assert upgraded["id"] == first["id"]
    assert upgraded["text_source"] == "full"
    assert upgraded["summary"] == "Summary based on the full article."
    assert upgraded["analyzed_at"] > first["analyzed_at"]
    assert upgraded["created_at"] == first["created_at"]

    flow.openai.error = OPENAI_TIMEOUT
    assert flow.client.post(f"/api/analyses/{first['id']}/reanalyze").status_code == 504
    assert flow.client.get(f"/api/analyses/{first['id']}").json() == upgraded


def test_delete_then_analyze_again(flow):
    flow.net.gnews_items = [gnews_item(url=SOLAR)]
    first = flow.client.post("/api/analyses", json=search(flow)[0]).json()

    assert flow.client.delete(f"/api/analyses/{first['id']}").status_code == 204
    assert flow.client.get("/api/analyses").json()["total"] == 0
    article = search(flow)[0]
    assert article["analysis"] is None

    second = flow.client.post("/api/analyses", json=article)
    assert second.status_code == 201
    assert len(flow.openai.prompts) == 2  # analysed afresh, not served from the deleted row


def test_api_key_never_appears_in_logs(flow, caplog):
    flow.net.gnews_items = [gnews_item()]
    with caplog.at_level(logging.DEBUG):
        search(flow)
    assert "test-key" not in caplog.text


def test_outside_services_failing(flow):
    flow.net.gnews_status = 403
    response = flow.client.get("/api/articles", params={"q": "solar"})
    assert response.status_code == 429
    assert response.json()["error"]["code"] == "news_quota_exceeded"

    flow.net.gnews_status, flow.net.gnews_error = 200, httpx.ReadTimeout("slow")
    assert flow.client.get("/api/articles", params={"q": "solar"}).status_code == 504

    flow.openai.error = OPENAI_TIMEOUT
    response = flow.client.post("/api/analyses", json={"url": SOLAR, "title": "Solar"})
    assert response.status_code == 504
    assert response.json()["error"]["code"] == "analysis_timeout"

    library = flow.client.get("/api/analyses")
    assert library.status_code == 200 and library.json()["total"] == 0


def test_internal_urls_are_never_fetched_and_html_is_stored_as_text(flow):
    response = flow.client.post(
        "/api/analyses",
        json={"url": "http://169.254.169.254/latest/meta-data", "title": "<script>alert(1)</script>", "content": "Preview"},
    )
    assert response.status_code == 201
    assert response.json()["text_source"] == "preview"
    assert response.json()["title"] == "<script>alert(1)</script>"  # escaping is the page's job
    assert flow.net.site_requests == []

    assert flow.client.post("/api/analyses", json={"url": "javascript:alert(1)", "title": "x"}).status_code == 422


@pytest.mark.parametrize(
    "method, path",
    [
        ("get", "/"),
        ("get", "/api/articles?q=solar"),
        ("get", "/api/analyses"),
        ("get", "/api/analyses/1"),
        ("post", "/api/analyses"),
        ("post", "/api/analyses/1/reanalyze"),
        ("delete", "/api/analyses/1"),
    ],
)
def test_every_route_except_health_needs_the_password(flow, method, path):
    anonymous = TestClient(app)
    assert getattr(anonymous, method)(path).status_code == 401
    assert anonymous.get("/api/health").status_code == 200

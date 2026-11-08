from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from app import config
from app.db import Analysis, get_session, utcnow
from app.errors import ApiError
from app.main import app
from tests.helpers import make_article, make_result, seed


# ---------- health ----------

def test_health(api):
    assert api.client.get("/api/health").json() == {"status": "ok"}


def test_health_reports_database_down(api):
    class BrokenSession:
        def execute(self, *args):
            raise OperationalError("SELECT 1", {}, Exception("connection refused"))

    app.dependency_overrides[get_session] = lambda: BrokenSession()
    response = api.client.get("/api/health")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "db_unavailable"


# ---------- search ----------

def test_search_returns_articles_with_stored_analysis_attached(api):
    stored = seed(api.session_factory, url="https://news.example.com/already-done")
    api.news.articles = [make_article(), make_article(url="https://news.example.com/already-done")]

    response = api.client.get("/api/articles", params={"q": "  solar  "})

    assert response.status_code == 200
    fresh, done = response.json()["articles"]
    assert fresh["title"] == "Solar power hits record high" and fresh["analysis"] is None
    assert done["analysis"]["id"] == stored.id
    assert api.news.calls == [("solar", 10)]  # query trimmed, default size


@pytest.mark.parametrize(
    "params",
    [{}, {"q": ""}, {"q": "   "}, {"q": "x" * 201}, {"q": "solar", "max": 0}, {"q": "solar", "max": 11}],
    ids=["missing", "empty", "blank", "too-long", "max-0", "max-11"],
)
def test_search_rejects_invalid_parameters(api, params):
    response = api.client.get("/api/articles", params=params)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"
    assert api.news.calls == []


@pytest.mark.parametrize("status, code", [(429, "news_quota_exceeded"), (502, "news_unavailable"), (504, "news_timeout")])
def test_search_passes_news_errors_through(api, status, code):
    api.news.error = ApiError(status, code, "Readable message")
    response = api.client.get("/api/articles", params={"q": "solar"})
    assert response.status_code == status
    assert response.json() == {"error": {"code": code, "message": "Readable message"}}


def test_search_with_no_results(api):
    assert api.client.get("/api/articles", params={"q": "zzzz"}).json() == {"articles": []}


# ---------- create analysis ----------

def test_create_analysis_with_full_text(api):
    api.fetcher.text = "Full article text"
    response = api.client.post("/api/analyses", json=make_article().model_dump(mode="json"))

    assert response.status_code == 201
    body = response.json()
    assert response.headers["location"] == f"/api/analyses/{body['id']}"
    assert body["summary"] == make_result().summary
    assert body["text_source"] == "full" and body["model"] == "test-model"
    assert api.analyzer.calls[0][1] == "Full article text"
    with api.session_factory() as session:
        assert session.get(Analysis, body["id"]).url == make_article().url


def test_create_analysis_falls_back_to_preview(api):
    response = api.client.post("/api/analyses", json=make_article().model_dump(mode="json"))
    assert response.json()["text_source"] == "preview"
    assert api.analyzer.calls[0][1] is None


def test_same_article_twice_is_not_analyzed_again(api):
    payload = make_article().model_dump(mode="json")
    first = api.client.post("/api/analyses", json=payload)
    second = api.client.post("/api/analyses", json=payload | {"analysis": {"ignored": True}})

    assert (first.status_code, second.status_code) == (201, 200)
    assert first.json()["id"] == second.json()["id"]
    assert len(api.analyzer.calls) == 1 and len(api.fetcher.calls) == 1


@pytest.mark.parametrize(
    "payload",
    [
        {"url": "https://news.example.com/a"},
        {"url": "https://news.example.com/a", "title": "   "},
        {"url": "javascript:alert(1)", "title": "Bad link"},
        ["not", "an", "object"],
    ],
    ids=["missing-title", "blank-title", "javascript-url", "not-an-object"],
)
def test_create_rejects_invalid_articles(api, payload):
    response = api.client.post("/api/analyses", json=payload)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"
    assert api.analyzer.calls == []


def test_title_only_article_can_be_analyzed(api):
    response = api.client.post("/api/analyses", json={"url": "https://news.example.com/t", "title": "Headline only"})
    assert response.status_code == 201


@pytest.mark.parametrize("status", [502, 504])
def test_failed_analysis_stores_nothing(api, status):
    api.analyzer.error = ApiError(status, "analysis_failed", "AI failed")
    response = api.client.post("/api/analyses", json=make_article().model_dump(mode="json"))
    assert response.status_code == status
    assert api.client.get("/api/analyses").json()["total"] == 0


def test_concurrent_duplicate_returns_existing_row(api):
    # Simulates another request saving the same URL while this one waits for the AI.
    api.analyzer.before = lambda: seed(api.session_factory, summary="Saved by the other request")
    response = api.client.post("/api/analyses", json=make_article().model_dump(mode="json"))

    assert response.status_code == 200
    assert response.json()["summary"] == "Saved by the other request"
    assert api.client.get("/api/analyses").json()["total"] == 1


# ---------- re-analyze ----------

def test_reanalyze_overwrites_the_stored_analysis(api):
    old = seed(api.session_factory, text_source="preview", analyzed_at=utcnow() - timedelta(hours=1))
    api.fetcher.text = "Full article text"
    api.analyzer.result = make_result(summary="Better summary", sentiment="neutral", sentiment_score=0.0)

    body = api.client.post(f"/api/analyses/{old.id}/reanalyze").json()

    assert body["id"] == old.id
    assert (body["summary"], body["sentiment"], body["text_source"]) == ("Better summary", "neutral", "full")
    with api.session_factory() as session:
        row = session.get(Analysis, old.id)
        assert row.analyzed_at > old.analyzed_at.replace(tzinfo=None)
        assert row.created_at == old.created_at.replace(tzinfo=None)


def test_failed_reanalysis_keeps_the_old_result(api):
    old = seed(api.session_factory)
    before = api.client.get(f"/api/analyses/{old.id}").json()
    api.analyzer.error = ApiError(502, "analysis_failed", "AI failed")

    assert api.client.post(f"/api/analyses/{old.id}/reanalyze").status_code == 502
    assert api.client.get(f"/api/analyses/{old.id}").json() == before


# ---------- library ----------

def test_empty_library(api):
    assert api.client.get("/api/analyses").json() == {
        "items": [], "total": 0, "counts": {"positive": 0, "neutral": 0, "negative": 0}
    }


def test_library_order_filter_counts_and_paging(api):
    start = utcnow()
    for i in range(25):
        seed(
            api.session_factory,
            url=f"https://news.example.com/{i}",
            sentiment="negative" if i % 5 == 0 else "positive",
            created_at=start + timedelta(minutes=i),
        )

    page1 = api.client.get("/api/analyses").json()
    page2 = api.client.get("/api/analyses", params={"offset": 20}).json()
    assert [item["url"] for item in page1["items"][:2]] == ["https://news.example.com/24", "https://news.example.com/23"]
    assert (len(page1["items"]), len(page2["items"]), page1["total"]) == (20, 5, 25)
    assert page1["counts"] == {"positive": 20, "neutral": 0, "negative": 5}

    negative = api.client.get("/api/analyses", params={"sentiment": "negative"}).json()
    assert {item["sentiment"] for item in negative["items"]} == {"negative"}
    assert negative["total"] == 5
    assert negative["counts"] == page1["counts"]  # counts ignore the filter so every chip shows its number


@pytest.mark.parametrize("params", [{"sentiment": "mixed"}, {"limit": 0}, {"limit": 101}, {"offset": -1}])
def test_library_rejects_invalid_parameters(api, params):
    assert api.client.get("/api/analyses", params=params).status_code == 422


def test_get_and_delete(api):
    row = seed(api.session_factory)
    assert api.client.get(f"/api/analyses/{row.id}").json()["url"] == row.url
    assert api.client.delete(f"/api/analyses/{row.id}").status_code == 204
    assert api.client.get(f"/api/analyses/{row.id}").status_code == 404
    assert api.client.delete(f"/api/analyses/{row.id}").status_code == 404


@pytest.mark.parametrize("method, path", [("get", "/api/analyses/999"), ("post", "/api/analyses/999/reanalyze")])
def test_unknown_id_is_404(api, method, path):
    response = getattr(api.client, method)(path)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_non_integer_id_is_422(api):
    assert api.client.get("/api/analyses/abc").status_code == 422


def test_unexpected_error_returns_500_without_details(api):
    api.analyzer.error = RuntimeError("secret internal detail")
    client = TestClient(app, raise_server_exceptions=False)
    response = client.post("/api/analyses", json=make_article().model_dump(mode="json"))
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"
    assert "secret internal detail" not in response.text


# ---------- password ----------

@pytest.fixture
def locked(api, monkeypatch):
    monkeypatch.setattr(config, "APP_PASSWORD", "s3cret-pässword")
    return api.client


@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": "Bearer s3cret"}, {"Authorization": "Basic !!!"}, {"Authorization": "Basic bm9jb2xvbg=="}],
    ids=["no-header", "bearer", "not-base64", "no-colon"],
)
def test_password_required(locked, headers):
    response = locked.get("/api/analyses", headers=headers)
    assert response.status_code == 401
    assert response.headers["www-authenticate"].startswith("Basic")
    assert response.json()["error"]["code"] == "unauthorized"


def test_wrong_password_rejected(locked):
    assert locked.get("/api/analyses", auth=("anyone", "wrong")).status_code == 401


def test_correct_password_opens_api_and_page(locked):
    assert locked.get("/api/analyses", auth=("anyone", "s3cret-pässword")).status_code == 200
    assert locked.get("/", auth=("reviewer", "s3cret-pässword")).status_code == 200


def test_health_needs_no_password(locked):
    assert locked.get("/api/health").status_code == 200


def test_openapi_lists_every_route(api):
    paths = api.client.get("/openapi.json").json()["paths"]
    assert set(paths) == {
        "/api/health", "/api/articles", "/api/analyses",
        "/api/analyses/{analysis_id}", "/api/analyses/{analysis_id}/reanalyze",
    }

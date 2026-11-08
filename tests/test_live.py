"""Checks a running app end to end with the real GNews and OpenAI APIs.

Run with `pytest -m live` while the app is running. Uses 1 GNews search and at most 1 OpenAI call.
LIVE_URL defaults to the local server; point it at the deployed app to check that instead.
"""
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from dotenv import dotenv_values

from app.analyzer import SCORE_RANGES

pytestmark = pytest.mark.live

LIVE_URL = os.getenv("LIVE_URL", "http://127.0.0.1:8000")
PASSWORD = os.getenv("APP_PASSWORD") or dotenv_values(Path(__file__).parent.parent / ".env").get("APP_PASSWORD", "")


def test_health_is_open_but_everything_else_needs_the_password():
    assert httpx.get(f"{LIVE_URL}/api/health", timeout=90).status_code == 200
    assert httpx.get(f"{LIVE_URL}/", timeout=90).status_code == 401
    assert httpx.get(f"{LIVE_URL}/api/analyses", timeout=90).status_code == 401


def test_search_analyze_store_and_delete():
    client = httpx.Client(base_url=LIVE_URL, auth=("live-test", PASSWORD), timeout=90)

    search = client.get("/api/articles", params={"q": "renewable energy"})
    assert search.status_code == 200, search.text
    articles = search.json()["articles"]
    assert articles and all(a["url"] and a["title"] and a["source_name"] for a in articles)
    dates = [datetime.fromisoformat(a["published_at"]) for a in articles if a["published_at"]]
    assert dates == sorted(dates, reverse=True)
    assert dates[0] > datetime.now(timezone.utc) - timedelta(days=7)

    article = next((a for a in articles if a["analysis"] is None), None)
    if article is None:
        pytest.skip("every search result is already analysed")

    created = client.post("/api/analyses", json=article)
    assert created.status_code == 201, created.text
    analysis = created.json()
    try:
        low, high = SCORE_RANGES[analysis["sentiment"]]
        assert low <= analysis["sentiment_score"] <= high
        assert analysis["summary"] and analysis["sentiment_reason"]
        assert client.get(created.headers["location"]).json()["summary"] == analysis["summary"]

        again = client.post("/api/analyses", json=article)
        assert (again.status_code, again.json()["id"]) == (200, analysis["id"])

        library = client.get("/api/analyses", params={"limit": 100}).json()
        assert analysis["id"] in {item["id"] for item in library["items"]}
    finally:
        # Only ever delete the analysis this test created, never existing ones.
        assert client.delete(f"/api/analyses/{analysis['id']}").status_code == 204
    assert client.get(f"/api/analyses/{analysis['id']}").status_code == 404

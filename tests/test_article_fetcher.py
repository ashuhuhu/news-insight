import httpx
import pytest

from app import article_fetcher
from app.article_fetcher import fetch_full_text, make_client
from tests.helpers import article_html, fake_dns

HTML = {"content-type": "text/html; charset=utf-8"}


@pytest.fixture(autouse=True)
def public_dns(monkeypatch):
    monkeypatch.setattr(article_fetcher.socket, "getaddrinfo", fake_dns())


def fetch_with(handler, url="https://news.example.com/story"):
    requests = []

    def recording_handler(request):
        requests.append(request)
        return handler(request)

    return fetch_full_text(url, http=make_client(httpx.MockTransport(recording_handler))), requests


def test_extracts_article_text():
    text, _ = fetch_with(lambda r: httpx.Response(200, headers=HTML, text=article_html()))
    assert text is not None and len(text) >= 500
    assert "Paragraph 3" in text
    assert "Home | World" not in text  # navigation is stripped


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(403, headers=HTML, text=article_html()),
        httpx.Response(404, headers=HTML, text="Not found"),
        httpx.Response(200, headers={"content-type": "application/pdf"}, content=b"%PDF-1.7"),
        httpx.Response(200, headers=HTML, text=article_html(paragraphs=1)),  # paywall-style teaser
        httpx.Response(200, headers=HTML, text="<html><body></body></html>"),
        httpx.Response(200, headers=HTML, content=b"<p>" + b"x" * 3_000_000 + b"</p>"),
    ],
    ids=["403", "404", "pdf", "too-short", "empty-page", "too-large"],
)
def test_returns_none_when_page_is_unusable(response):
    text, _ = fetch_with(lambda r: response)
    assert text is None


@pytest.mark.parametrize("error", [httpx.ReadTimeout("slow"), httpx.ConnectError("refused")])
def test_returns_none_on_network_errors(error):
    def handler(request):
        raise error

    text, _ = fetch_with(handler)
    assert text is None


@pytest.mark.parametrize("ip", ["127.0.0.1", "10.0.0.5", "169.254.169.254", "192.168.1.1"])
def test_blocks_private_addresses(monkeypatch, ip):
    monkeypatch.setattr(article_fetcher.socket, "getaddrinfo", fake_dns(default_ip=ip))
    text, requests = fetch_with(lambda r: httpx.Response(200, headers=HTML, text=article_html()))
    assert text is None
    assert requests == []


def test_blocks_redirect_to_private_address(monkeypatch):
    monkeypatch.setattr(article_fetcher.socket, "getaddrinfo", fake_dns(**{"internal.example.com": "10.0.0.1"}))

    def handler(request):
        if request.url.host == "news.example.com":
            return httpx.Response(302, headers={"location": "http://internal.example.com/secret"})
        return httpx.Response(200, headers=HTML, text=article_html())

    text, requests = fetch_with(handler)
    assert text is None
    assert [r.url.host for r in requests] == ["news.example.com"]

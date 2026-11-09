import socket
from types import SimpleNamespace

from app.db import Analysis
from app.schemas import AnalysisResult, Article


def make_article(**overrides) -> Article:
    data = {
        "url": "https://news.example.com/solar-record",
        "title": "Solar power hits record high",
        "description": "Output from solar farms beat expectations.",
        "content": "Solar farms produced more power than ever this autumn ... [1200 chars]",
        "source_name": "Example News",
        "image_url": "https://news.example.com/solar.jpg",
        "published_at": "2026-11-08T09:00:00Z",
    }
    return Article(**(data | overrides))


def make_result(**overrides) -> AnalysisResult:
    data = {
        "summary": "Solar output reached a record this autumn.",
        "sentiment": "positive",
        "sentiment_score": 0.7,
        "sentiment_reason": "Growth in clean energy is good news.",
    }
    return AnalysisResult(**(data | overrides))


def seed(session_factory, **overrides) -> Analysis:
    data = make_article().model_dump() | make_result().model_dump() | {"text_source": "preview", "model": "test-model"}
    with session_factory() as session:
        row = Analysis(**(data | overrides))
        session.add(row)
        session.commit()
        return row


def gnews_item(**overrides) -> dict:
    item = {
        "title": "Solar power hits record high",
        "description": "Output from solar farms beat expectations.",
        "content": "Solar farms produced more power than ever ... [1200 chars]",
        "url": "https://news.example.com/solar-record",
        "image": "https://news.example.com/solar.jpg",
        "publishedAt": "2026-11-08T09:00:00Z",
        "source": {"name": "Example News", "url": "https://news.example.com"},
    }
    return item | overrides


def article_html(paragraphs: int = 6) -> str:
    body = "".join(
        f"<p>Paragraph {i}: researchers said the new solar farm produced far more electricity than "
        "expected during the autumn months, according to figures released by the operator on Monday.</p>"
        for i in range(paragraphs)
    )
    return (
        "<html><head><title>Solar record</title></head><body><nav>Home | World | Business</nav>"
        f"<article><h1>Solar power hits record high</h1>{body}</article><footer>Copyright</footer></body></html>"
    )


def fake_dns(default_ip: str = "93.184.216.34", **ips_by_host):
    # So tests never do real DNS lookups.
    def getaddrinfo(host, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ips_by_host.get(host, default_ip), 0))]

    return getaddrinfo


class FakeNews:
    def __init__(self):
        self.articles: list[Article] = []
        self.error: Exception | None = None
        self.calls: list[tuple[str, int]] = []

    def search(self, query: str, max_results: int) -> list[Article]:
        self.calls.append((query, max_results))
        if self.error:
            raise self.error
        return self.articles


class FakeAnalyzer:
    model = "test-model"

    def __init__(self):
        self.result = make_result()
        self.error: Exception | None = None
        self.before = None
        self.calls: list[tuple[Article, str | None]] = []

    def analyze(self, article: Article, full_text: str | None) -> AnalysisResult:
        self.calls.append((article, full_text))
        if self.before:
            self.before()
        if self.error:
            raise self.error
        return self.result


class FakeFetcher:
    def __init__(self):
        self.text: str | None = None
        self.calls: list[str] = []

    def __call__(self, url: str) -> str | None:
        self.calls.append(url)
        return self.text


class FakeOpenAI:
    def __init__(self):
        self.responses = self
        self.prompts: list[str] = []
        self.error: Exception | None = None
        self.fail_when: str | None = None

    def parse(self, *, model, instructions, input, text_format):
        self.prompts.append(input)
        if self.error and (self.fail_when is None or self.fail_when in input):
            raise self.error
        source = "full article" if "Paragraph 3" in input else "preview"
        negative = "crisis" in input.lower()
        result = text_format(
            summary=f"Summary based on the {source}.",
            sentiment="negative" if negative else "positive",
            sentiment_score=-0.6 if negative else 0.6,
            sentiment_reason="Test reason.",
        )
        return SimpleNamespace(output_parsed=result)

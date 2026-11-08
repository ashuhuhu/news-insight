from datetime import datetime, timezone
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator

Sentiment = Literal["positive", "neutral", "negative"]
TextSource = Literal["full", "preview"]


def is_http_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def as_utc(value):
    # Every timestamp in the app is UTC, but SQLite drops the time zone when storing it.
    if isinstance(value, datetime) and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


class Article(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    # Plain str rather than HttpUrl: HttpUrl normalises URLs (e.g. adds a trailing slash),
    # which would break matching against URLs already stored in the database.
    url: str = Field(max_length=2000)
    title: str = Field(min_length=1, max_length=500)
    # GNews sends a few hundred characters; the limits stop clients posting huge text to store and prompt with.
    description: str | None = Field(default=None, max_length=5000)
    content: str | None = Field(default=None, max_length=5000)
    source_name: str | None = Field(default=None, max_length=200)
    image_url: str | None = None
    published_at: datetime | None = None

    @field_validator("url")
    @classmethod
    def require_http_url(cls, value: str) -> str:
        if not is_http_url(value):
            raise ValueError("must be an http(s) URL")
        return value

    @field_validator("image_url", mode="before")
    @classmethod
    def drop_invalid_image(cls, value):
        # A bad image link shouldn't reject the whole article.
        return value if isinstance(value, str) and is_http_url(value.strip()) else None

    @field_validator("published_at", mode="before")
    @classmethod
    def drop_invalid_date(cls, value):
        if isinstance(value, str):
            try:
                value = datetime.fromisoformat(value)
            except ValueError:
                return None
        return as_utc(value)


class AnalysisResult(BaseModel):
    """What the AI returns. Also used as the OpenAI structured-output schema."""

    summary: str
    sentiment: Sentiment
    sentiment_score: float
    sentiment_reason: str


class AnalysisOut(Article):
    model_config = ConfigDict(from_attributes=True)

    id: int
    summary: str
    sentiment: Sentiment
    sentiment_score: float
    sentiment_reason: str
    text_source: TextSource
    model: str
    created_at: datetime
    analyzed_at: datetime

    @field_validator("created_at", "analyzed_at", mode="before")
    @classmethod
    def stored_in_utc(cls, value):
        return as_utc(value)


class ArticleOut(Article):
    analysis: AnalysisOut | None = None


class ArticleList(BaseModel):
    articles: list[ArticleOut]


class AnalysisList(BaseModel):
    items: list[AnalysisOut]
    total: int
    counts: dict[str, int]

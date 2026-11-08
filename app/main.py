import base64
import binascii
import logging
import secrets
from contextlib import asynccontextmanager
from functools import lru_cache
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from openai import OpenAI
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app import config
from app.analyzer import Analyzer
from app.article_fetcher import fetch_full_text
from app.db import Analysis, get_session, init_db, utcnow
from app.errors import ApiError
from app.news_client import NewsClient
from app.schemas import AnalysisList, AnalysisOut, Article, ArticleList, ArticleOut, Sentiment

logging.basicConfig(level=logging.INFO)
# httpx logs every request URL at INFO, and GNews takes its API key as a URL parameter.
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="News Insight", lifespan=lifespan)


# Dependencies. Tests replace these through app.dependency_overrides.
@lru_cache
def get_news_client() -> NewsClient:
    # One shared instance so the search cache survives between requests.
    return NewsClient(config.GNEWS_API_KEY, httpx.Client())


@lru_cache
def get_analyzer() -> Analyzer:
    return Analyzer(OpenAI(api_key=config.OPENAI_API_KEY, timeout=30, max_retries=1), config.OPENAI_MODEL)


def get_fetcher():
    return fetch_full_text


def error_response(status_code: int, code: str, message: str, headers: dict | None = None) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status_code, headers=headers)


@app.middleware("http")
async def require_password(request: Request, call_next):
    # Health stays open so the host's health check works without credentials.
    if not config.APP_PASSWORD or request.url.path == "/api/health":
        return await call_next(request)
    scheme, _, encoded = request.headers.get("authorization", "").partition(" ")
    try:
        password = base64.b64decode(encoded, validate=True).decode().partition(":")[2]
    except (binascii.Error, UnicodeDecodeError):
        password = ""
    if scheme.lower() == "basic" and secrets.compare_digest(password.encode(), config.APP_PASSWORD.encode()):
        return await call_next(request)
    return error_response(
        401, "unauthorized", "Password required.", headers={"WWW-Authenticate": 'Basic realm="News Insight"'}
    )


@app.exception_handler(ApiError)
async def handle_api_error(request: Request, exc: ApiError):
    return error_response(exc.status_code, exc.code, exc.message)


@app.exception_handler(RequestValidationError)
async def handle_validation_error(request: Request, exc: RequestValidationError):
    first = exc.errors()[0]
    field = ".".join(str(part) for part in first["loc"][1:]) or "request"
    return error_response(422, "invalid_request", f"{field}: {first['msg']}")


@app.exception_handler(Exception)
async def handle_unexpected_error(request: Request, exc: Exception):
    logger.exception("Unhandled error on %s", request.url.path)
    return error_response(500, "internal_error", "Something went wrong. Try again.")


def get_or_404(session: Session, analysis_id: int) -> Analysis:
    row = session.get(Analysis, analysis_id)
    if row is None:
        raise ApiError(404, "not_found", "Analysis not found.")
    return row


@app.get("/api/health")
def health(session: Session = Depends(get_session)):
    try:
        session.execute(text("SELECT 1"))
    except SQLAlchemyError:
        raise ApiError(503, "db_unavailable", "The database is unavailable.")
    return {"status": "ok"}


@app.get("/api/articles", response_model=ArticleList)
def search_articles(
    q: str = Query(min_length=1, max_length=200),
    max_results: int = Query(10, alias="max", ge=1, le=10),
    session: Session = Depends(get_session),
    news: NewsClient = Depends(get_news_client),
):
    query = q.strip()
    if not query:
        raise ApiError(422, "invalid_request", "q: Enter a search term.")
    articles = news.search(query, max_results)
    stored = session.scalars(select(Analysis).where(Analysis.url.in_([a.url for a in articles])))
    by_url = {row.url: row for row in stored}
    return ArticleList(
        articles=[
            ArticleOut(**a.model_dump(), analysis=AnalysisOut.model_validate(by_url[a.url]) if a.url in by_url else None)
            for a in articles
        ]
    )


@app.post("/api/analyses", response_model=AnalysisOut, status_code=201)
def create_analysis(
    article: Article,
    response: Response,
    session: Session = Depends(get_session),
    analyzer: Analyzer = Depends(get_analyzer),
    fetch=Depends(get_fetcher),
):
    existing = session.scalar(select(Analysis).where(Analysis.url == article.url))
    if existing:
        response.status_code = 200
        return existing

    full_text = fetch(article.url)
    result = analyzer.analyze(article, full_text)
    row = Analysis(
        **article.model_dump(),
        **result.model_dump(),
        text_source="full" if full_text else "preview",
        model=analyzer.model,
    )
    session.add(row)
    try:
        session.commit()
    except IntegrityError:
        # Another request stored this URL while we were waiting for the AI.
        session.rollback()
        response.status_code = 200
        return session.scalar(select(Analysis).where(Analysis.url == article.url))
    response.headers["Location"] = f"/api/analyses/{row.id}"
    return row


@app.post("/api/analyses/{analysis_id}/reanalyze", response_model=AnalysisOut)
def reanalyze(
    analysis_id: int,
    session: Session = Depends(get_session),
    analyzer: Analyzer = Depends(get_analyzer),
    fetch=Depends(get_fetcher),
):
    row = get_or_404(session, analysis_id)
    article = Article.model_validate(row, from_attributes=True)
    full_text = fetch(row.url)
    # analyze() raises before the row is touched, so a failed re-analysis keeps the old result.
    result = analyzer.analyze(article, full_text)
    for field, value in result.model_dump().items():
        setattr(row, field, value)
    row.text_source = "full" if full_text else "preview"
    row.model = analyzer.model
    row.analyzed_at = utcnow()
    session.commit()
    return row


@app.get("/api/analyses", response_model=AnalysisList)
def list_analyses(
    sentiment: Sentiment | None = None,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    session: Session = Depends(get_session),
):
    query = select(Analysis)
    if sentiment:
        query = query.where(Analysis.sentiment == sentiment)
    rows = session.scalars(
        query.order_by(Analysis.created_at.desc(), Analysis.id.desc()).limit(limit).offset(offset)
    ).all()
    total = session.scalar(select(func.count()).select_from(query.subquery()))
    counts = {"positive": 0, "neutral": 0, "negative": 0}
    counts.update(session.execute(select(Analysis.sentiment, func.count()).group_by(Analysis.sentiment)).all())
    return AnalysisList(items=[AnalysisOut.model_validate(r) for r in rows], total=total, counts=counts)


@app.get("/api/analyses/{analysis_id}", response_model=AnalysisOut)
def get_analysis(analysis_id: int, session: Session = Depends(get_session)):
    return get_or_404(session, analysis_id)


@app.delete("/api/analyses/{analysis_id}", status_code=204)
def delete_analysis(analysis_id: int, session: Session = Depends(get_session)):
    session.delete(get_or_404(session, analysis_id))
    session.commit()
    return Response(status_code=204)


# Mounted last so the /api routes above take priority.
app.mount("/", StaticFiles(directory=Path(__file__).parent / "static", html=True), name="static")

from datetime import datetime, timezone

from sqlalchemy import CheckConstraint, DateTime, String, Text, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from app import config

# pre_ping because Neon closes idle connections; without it the first request after a quiet spell fails.
engine = create_engine(config.DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Analysis(Base):
    __tablename__ = "analyses"
    __table_args__ = (
        CheckConstraint("sentiment IN ('positive', 'neutral', 'negative')", name="valid_sentiment"),
        CheckConstraint("text_source IN ('full', 'preview')", name="valid_text_source"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    url: Mapped[str] = mapped_column(Text, unique=True)
    title: Mapped[str] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    content: Mapped[str | None] = mapped_column(Text)
    source_name: Mapped[str | None] = mapped_column(Text)
    image_url: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    summary: Mapped[str] = mapped_column(Text)
    sentiment: Mapped[str] = mapped_column(String(8))
    sentiment_score: Mapped[float]
    sentiment_reason: Mapped[str] = mapped_column(Text)
    text_source: Mapped[str] = mapped_column(String(8))
    model: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    analyzed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


def get_session():
    with SessionLocal() as session:
        yield session


def init_db() -> None:
    Base.metadata.create_all(engine)

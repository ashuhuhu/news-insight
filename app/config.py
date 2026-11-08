import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")


def normalize_database_url(url: str) -> str:
    # Hosts hand out postgres:// URLs, but SQLAlchemy needs the psycopg driver named explicitly.
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


GNEWS_API_KEY = os.getenv("GNEWS_API_KEY", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-6-luna")
APP_PASSWORD = os.getenv("APP_PASSWORD", "")
DATABASE_URL = normalize_database_url(os.getenv("DATABASE_URL") or "sqlite:///./dev.db")

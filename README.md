# News Insight

Search recent news, pick the articles you care about, and get an AI summary and sentiment for each one. Every analysis is saved and listed in a library.

**Live app:** https://news-insight-yyhs.onrender.com. Any username works; the password is in the handover email. The free host sleeps when idle, so the first load can take about a minute.

## What it does

1. **Search:** fetches the 10 newest English articles for a topic from GNews. Articles you've already analysed show their saved result.
2. **Analyze:** click **Analyze** on an article, or **Analyze all** to analyse every new result.
3. **Read the article:** the server downloads the article page and extracts the text. If the page can't be read, it uses the GNews preview, and the card says which was used.
4. **AI:** `gpt-6-luna` returns a 2–3 sentence summary, a sentiment label (positive / neutral / negative), a score from −1 to +1, and a one-line reason.
5. **Store and show:** results are saved in PostgreSQL. The **Library** tab lists them newest first, with sentiment filters, re-analyze and delete.

## Assumptions

- Search covers English-language news only.
- An article is identified by its URL. Analysing it again returns the saved result. Re-analyze replaces the saved result only if the new attempt succeeds.
- Articles are analysed only when the user asks, because each analysis is a paid AI call.
- Sentiment means the overall tone of the events reported (good, bad or mixed for those involved), not the tone towards the search term.
- One shared password is enough; there are no user accounts.
- The app runs as a single server process, so the 10-minute search cache can live in memory.
- The server trusts the article details the browser sends, because GNews can't look an article up by URL.

## Dependencies

- Python 3.12
- A [GNews](https://gnews.io) API key and an OpenAI API key
- PostgreSQL in production. Without `DATABASE_URL`, the app uses a local SQLite file.
- Python packages from `requirements.txt`:

  | Package | Used for |
  |---|---|
  | FastAPI and Uvicorn | Web server |
  | SQLAlchemy and psycopg | Database |
  | httpx | GNews and article downloads |
  | openai | AI analysis |
  | trafilatura | Extracting article text from web pages |
  | python-dotenv | Loading `.env` |

## Run locally

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
cp .env.example .env               # fill in the values below
.venv/bin/uvicorn app.main:app --reload
.venv/bin/python -m pytest         # run the tests
```

Then open http://127.0.0.1:8000.

| Variable | Purpose |
|---|---|
| `OPENAI_API_KEY` | OpenAI key |
| `OPENAI_MODEL` | Model name; defaults to `gpt-6-luna` |
| `GNEWS_API_KEY` | GNews key |
| `APP_PASSWORD` | Shared login password; leave empty for no login |
| `DATABASE_URL` | PostgreSQL connection string |

## API

Every endpoint except `/api/health` needs the password (HTTP Basic auth). Interactive docs are at `/docs`.

| Method | Path | Does |
|---|---|---|
| GET | `/api/health` | Checks the database is reachable |
| GET | `/api/articles?q=&max=` | Searches news; each article includes its saved `analysis`, or `null` if it hasn't been analysed |
| POST | `/api/analyses` | Analyses an article (body: an article from the search). Returns 201, or 200 if it was already analysed |
| POST | `/api/analyses/{id}/reanalyze` | Runs the analysis again |
| GET | `/api/analyses?sentiment=&limit=&offset=` | The library, with a count for each sentiment |
| GET | `/api/analyses/{id}` | One analysis |
| DELETE | `/api/analyses/{id}` | Deletes an analysis |

Errors are returned as `{"error": {"code": "...", "message": "..."}}`.

## Code layout

```
app/
  main.py             routes, password check, error handling
  news_client.py      GNews search with a 10-minute cache
  article_fetcher.py  downloads article pages and extracts the text
  analyzer.py         AI prompt and output checks
  db.py               database table and connection
  schemas.py          request and response models
  config.py           settings from environment variables
  static/             the web page (index.html, app.js, styles.css)
tests/
```

## Deploy

`render.yaml` sets up a free Render web service. In Render, choose **New → Blueprint**, select this repo, and enter the secret values for `OPENAI_API_KEY`, `GNEWS_API_KEY`, `DATABASE_URL` and `APP_PASSWORD`. The database table is created on first start.

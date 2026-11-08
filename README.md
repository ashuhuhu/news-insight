# News Insight

Search recent news, pick the stories you care about, and get an AI summary and sentiment for each one. Every analysis is saved, so the Library builds up a sentiment history across all your searches.

**Live app:** _added after deploy_. Log in with any username; the password is in the handover email. The free host sleeps when idle, so the first load can take about a minute.

## How it works

1. **Search.** Type a topic (or click an example). The server fetches the 10 newest English articles from GNews and attaches any analyses you already have for them.
2. **Analyze.** Click **Analyze** on a card, or **Analyze all** to work through every new result (3 at a time). Nothing is analysed unless you ask, because each analysis is an AI call.
3. **Read the article.** The server downloads the article page and extracts the main text, because GNews only gives a ~200-character preview. If the page can't be read (paywall, blocked, not HTML), it falls back to the preview, and the card says so.
4. **AI.** `gpt-6-luna` returns a 2–3 sentence summary, a label (positive / neutral / negative), a score from −1 to +1 that must fit the label, and a one-sentence reason. Output is schema-enforced and validated before saving.
5. **Store and show.** Results are saved in Postgres (one row per article URL) and shown on the card. A bar above the results shows the sentiment split for the search. The **Library** tab lists every analysis newest first, with sentiment filters, counts, re-analyze and delete.

## Requirements and how they're met

| Requirement (from the brief) | How |
|---|---|
| Search recent news through a public API | GNews search, newest first, results show title, source, time, description, image and link |
| User selects an article and triggers analysis | Per-article **Analyze**, plus **Analyze all** for the whole topic |
| Summary with the OpenAI API | `gpt-6-luna` via structured outputs, grounded in the full article text when it can be fetched |
| Sentiment (positive / neutral / negative) | Label, score in the label's range, and a reason; the card shows whether it used the full article or only the preview |
| Store results in a database | PostgreSQL (Neon), unique per URL |
| Display all results and their analysis | Analysis on each search card; Library tab with filters, counts and paging |
| Useful, not arbitrary | Already-analysed articles are flagged in search and never re-billed; re-analyze and delete |
| REST API design | Resource URLs, correct verbs and status codes (201 + `Location`, 200 on repeat, 204, 404, 422), one error format |
| Robust AI features | Out-of-range or contradictory scores, blank output and refusals are rejected; prompt injection in article text is fenced off |
| 100 requests/day news quota | Searches run on submit only and are cached for 10 minutes |

## API

Interactive docs at `/docs` (password required). All endpoints except health require HTTP Basic auth.

| Method | Path | Does | Success |
|---|---|---|---|
| GET | `/api/health` | Database check (no password, used by the host) | 200 |
| GET | `/api/articles?q=&max=` | Search news (`max` 1–10, default 10); each article includes its stored `analysis` or `null` | 200 |
| POST | `/api/analyses` | Analyse an article (body: an article from the search response) | 201 + `Location`, or 200 with the existing analysis |
| POST | `/api/analyses/{id}/reanalyze` | Re-run the analysis; the stored one is replaced only if the new one succeeds | 200 |
| GET | `/api/analyses?sentiment=&limit=&offset=` | Library, newest first, with `total` and per-sentiment `counts` | 200 |
| GET | `/api/analyses/{id}` | One analysis | 200 |
| DELETE | `/api/analyses/{id}` | Delete an analysis | 204 |

Errors always look like `{"error": {"code": "...", "message": "..."}}`. The message is written for the user.

| Status | Codes |
|---|---|
| 400 | `invalid_query` (GNews rejected the query) |
| 401 | `unauthorized` |
| 404 / 405 | `not_found`, `method_not_allowed` |
| 422 | `invalid_request` |
| 429 | `news_quota_exceeded` |
| 500 | `internal_error` (details are logged, never returned) |
| 502 | `news_unavailable`, `analysis_failed` |
| 503 | `db_unavailable` |
| 504 | `news_timeout`, `analysis_timeout` |

## Design decisions

- **Analyses are keyed by article URL.** Analysing the same article again returns the stored result (200) instead of paying for a second AI call. A unique constraint keeps this true even when "Analyze all" sends requests in parallel.
- **Re-analyze is `POST /{id}/reanalyze`, not `PUT`.** The client doesn't send a replacement; it asks the server to redo work. The old result is kept if the new attempt fails.
- **Full text over preview.** Summaries from a 200-character preview are thin, so the server fetches the page itself. Private and internal addresses are blocked, including through redirects, because the URL comes from the browser.
- **PostgreSQL.** The data is one fixed-shape table; Postgres gives a unique constraint for dedupe, check constraints for valid labels, and simple SQL for filters and counts. Neon's free tier doesn't expire, unlike Render's free Postgres.
- **Plain HTML and JavaScript, served by the same FastAPI app.** No build step and one deploy. All article and AI text is inserted as text, never HTML.
- **A single shared password (HTTP Basic auth)** protects the API quotas without building user accounts.

## Run locally

Needs Python 3.12.

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
cp .env.example .env        # add your OpenAI and GNews keys
.venv/bin/uvicorn app.main:app --reload
```

Open http://127.0.0.1:8000. Without `DATABASE_URL` the app uses a local SQLite file. With `APP_PASSWORD` empty, there is no login.

| Variable | Purpose |
|---|---|
| `OPENAI_API_KEY` | OpenAI key |
| `OPENAI_MODEL` | Defaults to `gpt-6-luna` |
| `GNEWS_API_KEY` | GNews key |
| `APP_PASSWORD` | Shared password; empty disables the login |
| `DATABASE_URL` | Postgres connection string (`postgres://` and `postgresql://` both work) |

## Tests

```bash
.venv/bin/python -m pytest              # offline: every component with fakes, plus full flows through the real app code
.venv/bin/python -m pytest --cov=app    # with coverage (about 98%)
.venv/bin/python -m pytest -m live      # against a running app with the real APIs; 1 GNews search, at most 1 OpenAI call
```

The offline suite never touches the network: GNews, article sites and OpenAI are replaced with fakes. `LIVE_URL` points the live check at the deployed app instead of `localhost`.

## Project layout

```
app/
  main.py             routes, password check, error handling
  news_client.py      GNews search and 10-minute cache
  article_fetcher.py  downloads article pages and extracts the text
  analyzer.py         prompt and OpenAI call, output validation
  db.py               database table and session
  schemas.py          request and response models
  config.py           environment variables
  static/             index.html, app.js, styles.css
tests/                component tests, full-flow tests, live check
```

## Deploy (Render)

`render.yaml` defines a free Render web service. In Render: **New → Blueprint**, pick this repo, and enter the four secret values (`OPENAI_API_KEY`, `GNEWS_API_KEY`, `DATABASE_URL`, `APP_PASSWORD`). The table is created on first start.

## Known limitations

- The server trusts the article details the browser sends, because GNews has no look-up-by-URL endpoint. A logged-in user could submit made-up text for a real URL.
- The same story from several outlets has different URLs, so it is analysed and listed separately.
- Paywalled pages fall back to the preview, and a long paywall teaser can occasionally pass as the article.
- The search cache lives in memory, so it resets when the server restarts.
- No migrations tool: the table is created at startup, and schema changes would need a manual `ALTER TABLE`.
- An analysis holds a database connection while the page downloads and the AI runs (up to about a minute). That's fine at this scale; at higher load I'd split it into two short transactions.
- The private-address check resolves DNS separately from the actual connection, so DNS rebinding isn't covered.

## Next steps

- Sentiment towards the searched topic, not just the article overall.
- Group the same story across outlets.
- Run "Analyze all" as a background job, so it continues when the tab is closed.
- Add Alembic migrations and user accounts.

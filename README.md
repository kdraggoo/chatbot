# draggoo.com chatbot

A self-hosted RAG (retrieval-augmented generation) chatbot that answers questions about Kevin Draggoo's resume and career. It runs at [www.draggoo.com/chatbot/](https://www.draggoo.com/chatbot/).

Everything runs locally on one server: no third-party LLM APIs.

- **FastAPI** (`app/main.py`) embeds the question, retrieves matching chunks and builds the prompt
- **Qdrant** stores the document chunks as vectors
- **Ollama** runs the embedding model (`bge-m3`) and the answer model (`llama3.2:3b` in production)

## How a question is answered

1. The browser (`web/app.js`) POSTs the question to `/chatbot/api/chat?stream=true`.
2. The API embeds it with Ollama and searches Qdrant.
3. Chunks scoring below `MIN_SIMILARITY_SCORE` are dropped, and at most `MAX_CONTEXT_CHUNKS` are kept.
4. The prompt, built from those chunks, goes to the answer model. Tokens stream back as server-sent events, followed by the sources.
5. The request is logged to SQLite for the dashboard.

## Layout

| Path | What it is |
|---|---|
| `app/main.py` | FastAPI service: chat, diagnostics, admin, dashboard stats |
| `app/rag/ingest.py` | Command-line document ingester (`.txt .md .rtf .doc .docx .odt .ott .pdf`) |
| `app/admin.html`, `admin.js` | Admin UI: add, list and delete documents |
| `app/dashboard.html`, `dashboard.js` | Dashboard: service health, usage, monitoring, knowledge base |
| `app/test_quality.py`, `test_kevin_query.py` | Manual scripts that call the live API (there is no test suite) |
| `web/` | Public chat UI, served by nginx as static files |
| `probe.sh` | Hourly monitoring probe |
| `backfill_nginx.py` | One-off import of past chat requests from nginx logs |
| `docker-compose.yml`, `Dockerfile.api` | The three services |

Not in the repo: `.env` (secrets), `data/` and `undata/` (source documents), and `stats/` (the chat log).

## Running it

```sh
cp .env.example .env        # then set ADMIN_API_KEY
docker compose up -d
docker compose exec ollama ollama pull bge-m3
docker compose exec ollama ollama pull llama3.2:3b
```

The API listens on `127.0.0.1:18000`. In production, a separate nginx container joins the `chatbot_chatbot-net` network and routes:

| Public path | Goes to |
|---|---|
| `/chatbot/` | `web/` (static) |
| `/chatbot/api/*` | `api:8000/*` (300s timeouts for slow generation) |
| `/chatbot/admin`, `/chatbot/dashboard` (and their `.js`) | `api:8000` |
| `/chatbot/readyz`, `/livez`, `/healthz` | `api:8000` |

nginx resolves `api` only at startup. After recreating the API container, run `nginx -s reload`, or requests return 502.

`./app` is bind-mounted into the container, so Python changes need only `docker compose restart api`. Changes to `requirements.txt` or the Dockerfile need `docker compose up -d --build api`. Changes to `.env` need `docker compose up -d api`.

## Configuration (`.env`)

| Variable | Default | Purpose |
|---|---|---|
| `GEN_MODEL` | `llama3.1:8b` | Ollama model that writes answers |
| `EMBED_MODEL` | `bge-m3` | Ollama embedding model (must match the ingested vectors) |
| `QDRANT_COLLECTION` | `docs` | Qdrant collection name |
| `MIN_SIMILARITY_SCORE` | `0.3` | Drop retrieved chunks below this score |
| `MAX_CONTEXT_CHUNKS` | `10` | Chunks included in the prompt |
| `MAX_QUERY_LENGTH` | `2000` | Longest accepted question |
| `RATE_LIMIT` | `10/minute` | Per-client limit on `/chat` |
| `CHUNK_SIZE`, `CHUNK_OVERLAP` | `900`, `150` | Chunking for `/admin/ingest` (the command-line ingester uses `--chunk-size`/`--chunk-overlap`, default 1200/200) |
| `ADMIN_API_KEY` | *(empty)* | Guards `/admin/*`. **If empty, admin endpoints are unprotected.** |
| `STATS_RETENTION_DAYS` | `90` | How long chat log rows are kept |

## Managing documents

Put source files in `data/` and ingest them:

```sh
docker compose exec api python -m rag.ingest /data --undata-dir /undata
```

To remove a document, move its file from `data/` to `undata/` and re-run the same command. Ingest deletes the vectors of every file it finds in `undata/`.

Documents can also be added, listed and deleted from `/chatbot/admin` (requires the admin key).

## API

| Endpoint | Auth | Purpose |
|---|---|---|
| `POST /chat` (`?stream=true` for SSE) | none, rate-limited | Ask a question: `{"query": "..."}` |
| `POST /diagnostic` | none | Show retrieval scores and tuning recommendations for a query |
| `GET /readyz` | none | Ready when Qdrant and Ollama are reachable and models are present |
| `GET /livez`, `/healthz` | none | Liveness |
| `POST /admin/ingest`, `/admin/delete`, `GET /admin/list` | admin key | Manage the knowledge base |
| `GET /admin/stats?days=N` | admin key | Data behind the dashboard |

Send the admin key as `X-API-Key: <key>` or `Authorization: Bearer <key>`.

## Dashboard and monitoring

`/chatbot/dashboard` asks for the admin key, then shows:

- **Services:** live health of the API, Qdrant and Ollama
- **Usage:** questions per day, response times, error rate, share of questions with no relevant context, recent and most-asked questions
- **Monitoring:** results from the hourly probe
- **Knowledge base:** ingested documents and chunk counts

Every `/chat` request is logged to `stats/chat.db` (SQLite), with the question text but no IP address. The `source` column separates the kinds of rows:

| `source` | Meaning |
|---|---|
| `chat` | A real visitor question |
| `probe` | A monitoring run, excluded from usage counts |
| `nginx` | Historical request imported by `backfill_nginx.py` (timestamp and status only) |

`probe.sh` runs hourly from a user systemd timer (`chatbot-probe.timer`). It sends one known question through the full pipeline with the admin key, which marks the request as `probe`. It exits non-zero if no answer comes back.

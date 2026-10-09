# /srv/chatbot/app/main.py
import hashlib
import json
import logging
import os
import re
import sqlite3
import time
import uuid
from collections import defaultdict
from contextlib import asynccontextmanager, closing
from datetime import datetime
from fastapi import HTTPException, Query, Depends, Header, Request
from fastapi import FastAPI
from fastapi.responses import StreamingResponse, FileResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pathlib import Path
from urllib.parse import urlparse
from pydantic import BaseModel, Field, field_validator
from qdrant_client import QdrantClient
from qdrant_client.models import PointStruct, FilterSelector, Filter, FieldCondition, MatchValue
from typing import List, Optional
import httpx
import numpy as np  # installed with qdrant-client
from rag.ingest import chunk_text
from rag.refusal import is_refusal
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from slowapi import Limiter
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Configuration constants
QDRANT_URL = os.getenv("QDRANT_URL", "http://qdrant:6333")
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://ollama:11434")
GEN_MODEL = os.getenv("GEN_MODEL", "llama3.1:8b")
# Sampling for generation; unset leaves the model's defaults (llama3.2: temperature 0.8).
# GEN_TEMPERATURE=0 answers the same prompt the same way every time.
GEN_OPTIONS = {k: cast(os.environ[e]) for k, e, cast in
               (("temperature", "GEN_TEMPERATURE", float), ("seed", "GEN_SEED", int)) if os.getenv(e)}
EMBED_MODEL = os.getenv("EMBED_MODEL", "bge-m3")
QDRANT_COLLECTION = os.getenv("QDRANT_COLLECTION", "docs")
MAX_QUERY_LENGTH = int(os.getenv("MAX_QUERY_LENGTH", "2000"))
MAX_CONTEXT_CHUNKS = int(os.getenv("MAX_CONTEXT_CHUNKS", "10"))  # Optimized for speed
MIN_SIMILARITY_SCORE = float(os.getenv("MIN_SIMILARITY_SCORE", "0.3"))  # Balanced threshold
ADMIN_API_KEY = os.getenv("ADMIN_API_KEY", "")  # Admin API key for authentication
RATE_LIMIT = os.getenv("RATE_LIMIT", "10/minute")  # Rate limit for /chat endpoint
STATS_DB = os.getenv("STATS_DB", "/stats/chat.db")  # SQLite chat log for the dashboard
STATS_RETENTION_DAYS = int(os.getenv("STATS_RETENTION_DAYS", "90"))
# Words dropped from the query before embedding it for search. Every document is
# about the same person, so their name matches every chunk equally and drowns out
# the words that say what the question is about. Comma-separated.
RETRIEVAL_STRIP_WORDS = [w.strip() for w in os.getenv("RETRIEVAL_STRIP_WORDS", "").split(",") if w.strip()]
_strip_re = re.compile(
    r"\b(?:" + "|".join(map(re.escape, RETRIEVAL_STRIP_WORDS)) + r")(?:['’]s)?\b", re.IGNORECASE
) if RETRIEVAL_STRIP_WORDS else None
RETRIEVAL_STRIP_SLOTS = int(os.getenv("RETRIEVAL_STRIP_SLOTS", "1"))  # context slots kept for that search
# The knowledge base holds many versions of the same resume, so the top hits are often
# one section repeated (e.g. the same volunteer list from five 2009-2011 resumes) and
# crowd out everything else. A chunk whose words overlap an already chosen chunk's by
# at least this much (Jaccard, header line ignored) is skipped. 1 or more disables it.
DEDUP_SIMILARITY = float(os.getenv("DEDUP_SIMILARITY", "0.7"))


def _chunk_words(text: str) -> set:
    """A chunk's words for near-duplicate checks, without its "[title | section]" line."""
    if text.startswith("["):
        text = text.split("\n", 1)[-1]
    return set(re.findall(r"\w+", text.lower()))


def is_near_duplicate(words: set, chosen: List[set]) -> bool:
    return DEDUP_SIMILARITY < 1 and bool(words) and any(
        len(words & c) / len(words | c) >= DEDUP_SIMILARITY for c in chosen)


# Context chunks are numbered [1], [2], ... and the model cites them ("According to
# section [1], ..."), but visitors never see the sources, so the markers and the
# phrases that point at them are removed from answers.
# The model also echoes the label each chunk starts with ("[Kevin - Career | Summary]"),
# so any bracketed text counts, except a Markdown link "[text](url)".
_CITE = r"(?:\[\d+(?:\s*[,\-–]\s*\d+)*\]|\[[^\[\]\n\d][^\[\]\n]{0,119}\](?!\())"
_CITES = rf"{_CITE}(?:(?:\s*,)?\s*(?:and|&)?\s*{_CITE})*"  # "[1], [2], and [4]"
_SECTIONS = r"(?:the\s+)?(?:context\s+)?(?:provided\s+)?(?:in\s+)?(?:(?:sections?|chunks?|sources?)\s*)?"
_CITATION_RULES = [
    # "These are mentioned in sections [3], [4] and [6] of the context." (whole sentence)
    (re.compile(rf"[ \t]*\b(?:These|This|All of these)\b[^.\n]*?\bin\s+{_SECTIONS}{_CITES}[^.\n]*\.", re.I), ""),
    # "(as seen in [1] and [2])"
    (re.compile(rf"[ \t]*\(\s*(?:as\s+)?(?:(?:seen|mentioned|stated|noted|listed|described)\s+)?(?:in\s+)?{_SECTIONS}{_CITES}\s*\)", re.I), ""),
    # "Additionally, section [7] mentions that Kevin", "However, [4] mentions that"
    (re.compile(rf"(?:(?<=^)|(?<=[.!?,:]\s)|(?<=\n)){_SECTIONS}{_CITES}\s+(?:also\s+)?(?:mentions|states|notes|says|shows|lists|indicates)\s+(?:that\s+)?", re.I | re.M), "\0"),
    # "whereas section [8] mentions similar results", "Note that section [4] also mentions"
    (re.compile(rf"\b{_SECTIONS}{_CITES}\s+((?:also\s+)?(?:mentions|states|notes|says|shows|lists|indicates))\b", re.I), r"his resume \1"),
    # "According to section [1], ", "in sections [1] and [2] of the context", "as mentioned in section [2]"
    (re.compile(rf"[ \t]*\b(?:as\s+(?:stated|mentioned|seen|noted|described|listed)\s+in|according\s+to|based\s+on|in|from)\s+"
                rf"{_SECTIONS}{_CITES}(?:\s+of\s+(?:the|his)\s+[\w ]+?(?=[,.;:\n]))?\s*,?[ \t]*", re.I), " \0"),
    # "[Kevin - Career | Summary], Kevin has run" (a marker opening a sentence)
    (re.compile(rf"(^[ \t]*(?:(?:[-*]|\d+\.)[ \t]+)?|(?<=[.!?]\s))[ \t]*{_CITES}\s*[,:]?[ \t]*", re.M), "\\1\0"),
    (re.compile(rf"[ \t]*{_CITES}"), ""),  # any marker left
]
# A space before punctuation or at a line edge, left where a phrase was removed
_CITATION_TIDY = [(re.compile(r"[ \t]+([.,;:!?])"), r"\1"), (re.compile(r"^[ \t]+|[ \t]+$", re.M), ""),
                  (re.compile(r"([,;:])[.]"), "."), (re.compile(r"[ \t]{2,}"), " ")]
# A sentence that now starts where a phrase was removed (marked \0): "- In section [2], he was"
_SENTENCE_START = re.compile(r"(^[ \t]*(?:[-*]|\d+\.)?[ \t]*|[.!?][ \t]+)\0[ \t]*([a-z])", re.M)


def strip_citations(text: str) -> str:
    if "[" not in text:
        return text
    out = text
    for rx, repl in _CITATION_RULES:
        out = rx.sub(repl, out)
    out = _SENTENCE_START.sub(lambda m: m.group(1) + m.group(2).upper(), out).replace("\0", "")
    for rx, repl in _CITATION_TIDY:
        out = rx.sub(repl, out)
    return out


# The phrase before a marker ("According to section") streams first, so streamed text is
# held back until a sentence or line ends and cleaned a sentence at a time.
_STREAM_BREAK = re.compile(r"(?:[.!?:](?=\s)|\n)(?!.*(?:[.!?:](?=\s)|\n))", re.S)


def _strip_piece(piece: str) -> str:
    """strip_citations for part of a text, keeping the spaces it starts and ends with."""
    core = piece.strip(" \t\n")
    if "[" not in core:
        return piece
    i = piece.index(core[0])
    return piece[:i] + strip_citations(core) + piece[i + len(core):]


async def without_citations(tokens):
    """Yield streamed tokens with citations removed, a sentence or line at a time."""
    pending = ""
    async for token in tokens:
        if token.startswith("[ERROR"):
            yield (_strip_piece(pending) if pending else "") + token
            pending = ""
            continue
        pending += token
        m = _STREAM_BREAK.search(pending)
        if m:
            out, pending = pending[:m.end()], pending[m.end():]
            yield _strip_piece(out)
    if pending:
        yield _strip_piece(pending)


def drop_near_duplicates(points: list) -> list:
    """Points in order, without those that nearly repeat an earlier one."""
    kept, chosen = [], []
    for point in points:
        words = _chunk_words((point.payload or {}).get("text") or "")
        if not is_near_duplicate(words, chosen):
            chosen.append(words)
            kept.append(point)
    return kept


# Word overlap misses versions that describe the same job in different words (the
# Infor role appears in ~10 resumes, cosine 0.8-0.95 apart but Jaccard under 0.35).
# Below 1, search results are reordered by maximal marginal relevance: each pick
# trades its score (weight MMR_LAMBDA) against its cosine to the closest earlier
# pick. 1 keeps plain score order.
MMR_LAMBDA = float(os.getenv("MMR_LAMBDA", "1"))


def diversify(points: list) -> list:
    """Points reordered by maximal marginal relevance (unchanged if MMR_LAMBDA >= 1)."""
    if MMR_LAMBDA >= 1 or len(points) < 3 or any(p.vector is None for p in points):
        return points
    vecs = [np.asarray(p.vector, dtype=float) for p in points]
    vecs = [v / (np.linalg.norm(v) or 1) for v in vecs]
    remaining, order = list(range(len(points))), []
    nearest = [0.0] * len(points)  # cosine to the closest chosen point
    while remaining:
        best = max(remaining, key=lambda i: MMR_LAMBDA * points[i].score - (1 - MMR_LAMBDA) * nearest[i])
        remaining.remove(best)
        order.append(best)
        for i in remaining:
            nearest[i] = max(nearest[i], float(vecs[i] @ vecs[best]))
    return [points[i] for i in order]


def retrieval_query(query: str) -> str:
    """The query as embedded for search: RETRIEVAL_STRIP_WORDS removed, unless nothing would be left."""
    if not _strip_re:
        return query
    stripped = re.sub(r"\s+", " ", _strip_re.sub("", query)).strip()
    return stripped if re.search(r"\w", stripped) else query


async def search_chunks(query: str, limit: int) -> list:
    """Vector search for a query, best first.

    With RETRIEVAL_STRIP_WORDS set, it also searches with those words removed, and
    that search's top RETRIEVAL_STRIP_SLOTS hit(s) go first. The name helps questions
    whose answer sits next to it (the resume header and summary), so the normal
    search still fills the other slots. Raises HTTPException(502) on failure.
    """
    queries = list(dict.fromkeys([query, retrieval_query(query)]))
    rankings = []
    for q in queries:
        try:
            vec = await embed_query(q)
        except Exception as e:
            logger.error(f"Embedding error: {e}")
            raise HTTPException(status_code=502, detail=f"Embedding error: {e}")
        try:
            rankings.append(qdrant_client.search(
                collection_name=QDRANT_COLLECTION, query_vector=vec, limit=limit, with_payload=True,
                with_vectors=MMR_LAMBDA < 1,
            ))
        except Exception as e:
            logger.error(f"Qdrant search error: {e}")
            raise HTTPException(status_code=502, detail=f"Qdrant error: {e}")
    if len(rankings) == 1:
        return diversify(rankings[0])

    full, stripped = rankings
    reserved = drop_near_duplicates(diversify([p for p in stripped if p.score >= MIN_SIMILARITY_SCORE]))[:RETRIEVAL_STRIP_SLOTS]
    seen, merged = set(), []
    for point in reserved + full:
        if point.id not in seen:
            seen.add(point.id)
            merged.append(point)
    return merged[:limit]

# Initialize rate limiter
def client_address(request: Request) -> str:
    """Visitor IP for rate limiting and logs.

    nginx overwrites X-Real-IP with the address it saw, so a client can't spoof it.
    Requests that skip nginx (host curl, probe.sh on 127.0.0.1:18000) have no
    header and fall back to the socket address.
    """
    return request.headers.get("x-real-ip") or get_remote_address(request)


limiter = Limiter(key_func=client_address)

# Query analytics storage (in-memory, could be persisted to file/db)
query_analytics = defaultdict(int)



# Session details logged with each visitor question (no IP: nginx sees every visitor as
# the Docker gateway). Browser, OS and device are parsed here so the dashboard can group
# them; the raw User-Agent is kept so they can be re-parsed later.
CLIENT_COLUMNS = ("visitor_id", "session_id", "user_agent", "browser", "os", "device",
                  "language", "timezone", "screen", "theme", "referrer")
# The answer as the visitor saw it (after strip_citations) and the passages it drew on,
# as JSON [{"label": "Kevin - Career | Infor", "score": 0.6}], added 2026-10-09
ANSWER_COLUMNS = ("answer", "passages")
MAX_LOGGED_ANSWER = 8000
_BOT_UA = re.compile(r"bot|crawl|spider|slurp|curl|wget|python|httpx|go-http|java/|headless|lighthouse", re.I)
_BROWSERS = [  # first match wins; in-app browsers before the engines they wrap
    (r"LinkedInApp", "LinkedIn app"), (r"FBAN|FBAV", "Facebook app"), (r"Instagram", "Instagram app"),
    (r"Edg(?:e|A|iOS)?/", "Edge"), (r"OPR/|Opera", "Opera"), (r"SamsungBrowser", "Samsung Internet"),
    (r"Firefox/|FxiOS", "Firefox"), (r"Chrome/|CriOS|Chromium", "Chrome"), (r"Version/[\d.]+.*Safari/", "Safari"),
]
_OSES = [
    (r"iPhone|iPod", "iOS"), (r"iPad", "iPadOS"), (r"Android", "Android"), (r"CrOS", "ChromeOS"),
    (r"Windows NT", "Windows"), (r"Macintosh|Mac OS X", "macOS"), (r"Linux", "Linux"),
]
_ID_RE = re.compile(r"^[A-Za-z0-9-]{8,64}$")
_TZ_RE = re.compile(r"^[A-Za-z_]+(?:/[A-Za-z0-9_+-]+){0,2}$|^UTC$")
_SCREEN_RE = re.compile(r"^\d{2,5}x\d{2,5}$")
_LANG_RE = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})?$")


def parse_user_agent(ua: str) -> tuple:
    """(browser, os, device) from a User-Agent string; 'Other' when unrecognized."""
    if not ua:
        return None, None, None
    if _BOT_UA.search(ua):
        return "Bot or script", "Other", "bot"
    browser = next((name for rx, name in _BROWSERS if re.search(rx, ua)), "Other")
    os_name = next((name for rx, name in _OSES if re.search(rx, ua)), "Other")
    if os_name == "iPadOS" or "Tablet" in ua or (os_name == "Android" and "Mobile" not in ua):
        device = "tablet"
    elif os_name in ("iOS", "Android") or "Mobi" in ua:
        device = "mobile"
    else:
        device = "desktop"
    return browser, os_name, device


def client_details(request: Request, client: Optional[dict]) -> dict:
    """Session fields for the chat log. Anything malformed is dropped, never rejected."""
    client = client if isinstance(client, dict) else {}

    def pick(key: str, pattern: re.Pattern, limit: int) -> Optional[str]:
        value = client.get(key)
        if isinstance(value, str) and len(value) <= limit and pattern.match(value):
            return value
        return None

    ua = request.headers.get("user-agent", "")[:400]
    browser, os_name, device = parse_user_agent(ua)
    lang = request.headers.get("accept-language", "").split(",")[0].split(";")[0].strip()
    referrer = client.get("referrer")
    host = urlparse(referrer).hostname if isinstance(referrer, str) and referrer.startswith("http") else None
    if host and host.startswith("www."):
        host = host[4:]
    theme = client.get("theme")
    return {
        "visitor_id": pick("visitor_id", _ID_RE, 64),
        "session_id": pick("session_id", _ID_RE, 64),
        "user_agent": ua or None,
        "browser": browser,
        "os": os_name,
        "device": device,
        "language": lang if _LANG_RE.match(lang) else None,
        "timezone": pick("timezone", _TZ_RE, 64),
        "screen": pick("screen", _SCREEN_RE, 11),
        "theme": theme if isinstance(theme, str) and re.fullmatch(r"[a-z]{2,20}", theme) else None,
        # Host only: a full referrer URL can carry someone's search terms or tokens
        "referrer": host[:255] if host else None,
    }


def init_stats_db():
    """Create the chat log table used by the dashboard."""
    Path(STATS_DB).parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(STATS_DB)) as conn, conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS chat_log (
                id INTEGER PRIMARY KEY,
                ts REAL NOT NULL,              -- unix time the question arrived
                query TEXT NOT NULL,
                stream INTEGER NOT NULL,
                status TEXT NOT NULL,          -- ok | error | aborted
                duration_ms INTEGER,
                chunks_used INTEGER,           -- 0 = no context above MIN_SIMILARITY_SCORE
                top_score REAL,
                answer_chars INTEGER,
                source TEXT NOT NULL DEFAULT 'chat'  -- chat | probe (monitoring) | nginx (backfilled)
            )"""
        )
        columns = {row[1] for row in conn.execute("PRAGMA table_info(chat_log)")}
        if "source" not in columns:
            conn.execute("ALTER TABLE chat_log ADD COLUMN source TEXT NOT NULL DEFAULT 'chat'")
        for column in CLIENT_COLUMNS + ANSWER_COLUMNS:  # added 2026-10-09; NULL on older rows
            if column not in columns:
                conn.execute(f"ALTER TABLE chat_log ADD COLUMN {column} TEXT")
        conn.execute("CREATE INDEX IF NOT EXISTS chat_log_ts ON chat_log(ts)")
    logger.info(f"Chat stats DB ready at {STATS_DB}")


def record_chat(query: str, stream: bool, status: str, started_at: float, started_mono: float,
                sources: Optional[List[dict]] = None, answer: str = "", source: str = "chat",
                client: Optional[dict] = None):
    """Log one /chat request for the dashboard. Never lets a logging failure break chat."""
    try:
        duration_ms = int((time.monotonic() - started_mono) * 1000)
        top_score = max((s["score"] for s in sources), default=None) if sources else None
        with closing(sqlite3.connect(STATS_DB, timeout=5)) as conn, conn:
            client = client or {}
            passages = None if sources is None else json.dumps(
                [{"label": s.get("label") or s.get("title"), "score": s.get("score")} for s in sources])
            conn.execute(
                "INSERT INTO chat_log (ts, query, stream, status, duration_ms, chunks_used, top_score, answer_chars, source, "
                + ", ".join(CLIENT_COLUMNS + ANSWER_COLUMNS) + ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?"
                + ", ?" * len(CLIENT_COLUMNS + ANSWER_COLUMNS) + ")",
                (started_at, query[:500], int(stream), status, duration_ms,
                 None if sources is None else len(sources), top_score, len(answer), source,
                 *(client.get(c) for c in CLIENT_COLUMNS), answer[:MAX_LOGGED_ANSWER] or None, passages),
            )
            conn.execute("DELETE FROM chat_log WHERE ts < ?", (time.time() - STATS_RETENTION_DAYS * 86400,))
    except Exception as e:
        logger.error(f"Failed to record chat stats: {e}")


# Global HTTP client for async requests
http_client: httpx.AsyncClient = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan context manager for startup and shutdown."""
    global http_client
    init_stats_db()
    # Startup - use higher default timeout to accommodate long prompts
    http_client = httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=10.0))
    logger.info(f"Initialized HTTP client with timeout 300s")
    yield
    # Shutdown
    if http_client:
        await http_client.aclose()
        logger.info("Closed HTTP client")

app = FastAPI(lifespan=lifespan)
app.state.limiter = limiter

# Rate limit exceeded handler
@app.exception_handler(RateLimitExceeded)
async def rate_limit_handler(request: Request, exc: RateLimitExceeded):
    return HTTPException(status_code=429, detail="Rate limit exceeded. Please slow down.")

# Initialize Qdrant client (synchronous, can be at module level)
qdrant_client = QdrantClient(url=QDRANT_URL)
logger.info(f"Initialized Qdrant client at {QDRANT_URL}")

# Security scheme for API key authentication
security = HTTPBearer(auto_error=False)


def verify_admin_api_key(
    authorization: Optional[HTTPAuthorizationCredentials] = Depends(security),
    x_api_key: Optional[str] = Header(None, alias="X-API-Key")
) -> bool:
    """
    Verify admin API key from either Bearer token or X-API-Key header.
    Returns True if authenticated, raises HTTPException if not.
    """
    if not ADMIN_API_KEY:
        logger.warning("ADMIN_API_KEY not set - admin endpoints are unprotected!")
        # If no API key is configured, allow access (for development)
        # In production, you should always set ADMIN_API_KEY
        return True
    
    # Check Bearer token
    if authorization and authorization.credentials:
        if authorization.credentials == ADMIN_API_KEY:
            return True
    
    # Check X-API-Key header
    if x_api_key and x_api_key == ADMIN_API_KEY:
        return True
    
    # No valid authentication provided
    logger.warning("Admin endpoint accessed without valid API key")
    raise HTTPException(
        status_code=401,
        detail="Unauthorized: Valid API key required. Provide via 'Authorization: Bearer <key>' header or 'X-API-Key' header."
    )

def is_probe_request(request: Request) -> bool:
    """A /chat call carrying the admin key is the monitoring probe (probe.sh), logged apart from visitors."""
    if not ADMIN_API_KEY:
        return False
    auth = request.headers.get("authorization", "")
    return request.headers.get("x-api-key") == ADMIN_API_KEY or auth == f"Bearer {ADMIN_API_KEY}"


class ChatRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=MAX_QUERY_LENGTH, description="User query string")
    # Session details from the chat page (visitor/session IDs, time zone, screen, theme,
    # referrer); sanitized by client_details, so bad values are dropped, not rejected
    client: Optional[dict] = None
    
    @field_validator('query')
    @classmethod
    def validate_query(cls, v: str) -> str:
        """Validate and sanitize query input."""
        v = v.strip()
        if not v:
            raise ValueError("Query cannot be empty or whitespace only")
        # Basic sanitization: remove excessive whitespace
        v = re.sub(r'\s+', ' ', v)
        # Check for suspicious patterns (basic protection)
        if len(v) > MAX_QUERY_LENGTH:
            raise ValueError(f"Query too long (max {MAX_QUERY_LENGTH} characters)")
        return v


class IngestRequest(BaseModel):
    content: str = Field(..., min_length=1, description="Text content to ingest")
    title: Optional[str] = Field(None, description="Optional title for the ingested content")
    source: Optional[str] = Field(None, description="Optional source identifier (e.g., 'chat-2024-01-01')")


class DeleteRequest(BaseModel):
    source_path: Optional[str] = Field(None, description="File path to delete (e.g., '/data/file.doc')")
    doc_id: Optional[str] = Field(None, description="Document ID to delete (alternative to source_path)")

@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    retry=retry_if_exception_type((httpx.TimeoutException, httpx.HTTPStatusError)),
    before_sleep=lambda retry_state: logger.warning(f"Retrying embed_query (attempt {retry_state.attempt_number})...")
)
async def embed_query(text: str) -> List[float]:
    """Generate embedding using Ollama API with retry logic."""
    url = f"{OLLAMA_URL.rstrip('/')}/api/embeddings"
    try:
        response = await http_client.post(
            url,
            json={"model": EMBED_MODEL, "prompt": text},
            timeout=30.0
        )
        response.raise_for_status()
        result = response.json()
        vec = result.get("embedding")
        if not isinstance(vec, list):
            raise RuntimeError("Unexpected embedding response from Ollama")
        logger.debug(f"Generated embedding of size {len(vec)}")
        return vec
    except httpx.HTTPStatusError as e:
        logger.error(f"Ollama embedding HTTP error: {e.response.status_code}")
        raise
    except httpx.TimeoutException:
        logger.error("Ollama embedding request timed out")
        raise
    except Exception as e:
        logger.error(f"Ollama embedding request failed: {e}")
        raise

@app.get("/readyz")
async def readyz():
    """Health check endpoint - verifies Qdrant and Ollama connectivity and required models."""
    # 1) Qdrant live check
    try:
        response = await http_client.get(f"{QDRANT_URL}/collections", timeout=5.0)
        response.raise_for_status()
        logger.debug("Qdrant connectivity check passed")
    except Exception as e:
        logger.error(f"Qdrant unreachable: {e}")
        raise HTTPException(status_code=503, detail={"qdrant": f"unreachable: {e}"})

    # 2) Ollama models check
    try:
        response = await http_client.get(f"{OLLAMA_URL}/api/tags", timeout=5.0)
        response.raise_for_status()
        m = response.json()
        # Normalize model names: Ollama returns "model:tag" but env vars may omit tag
        # Match if either exact name or base name (without tag) matches
        available_models = set()
        for model in m.get("models", []):
            name = model.get("name", "")
            available_models.add(name)  # e.g., "bge-m3:latest"
            available_models.add(name.split(":")[0])  # e.g., "bge-m3"
        missing = {GEN_MODEL, EMBED_MODEL} - available_models
        if missing:
            logger.error(f"Missing required Ollama models: {sorted(missing)}")
            raise HTTPException(status_code=503, detail={"ollama": f"missing models: {sorted(missing)}"})
        logger.debug("Ollama models check passed")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Ollama unreachable: {e}")
        raise HTTPException(status_code=503, detail={"ollama": f"unreachable: {e}"})
    return {"status": "ok"}


@app.get("/livez")
def livez():
    """Liveness probe - indicates service is running."""
    return {"status": "ok"}


@app.get("/healthz")
def healthz():
    """Health check endpoint - basic health status."""
    return {"status": "ok"}


@app.post("/diagnostic")
async def diagnostic(req: ChatRequest):
    """
    Diagnostic endpoint to analyze retrieval quality without generating a full response.
    Useful for tuning MIN_SIMILARITY_SCORE and understanding retrieval behavior.
    """
    query = req.query
    logger.info(f"Diagnostic query: {query[:100]}...")
    
    search_res = await search_chunks(query, MAX_CONTEXT_CHUNKS * 3)  # more results for analysis
    
    # Analyze all results
    all_chunks = []
    filtered_chunks = []
    
    for idx, point in enumerate(search_res):
        score = getattr(point, 'score', 1.0)
        payload = point.payload or {}
        
        chunk_info = {
            "rank": idx + 1,
            "score": round(score, 4),
            "title": payload.get("title", "Unknown"),
            "source_path": payload.get("source_path", "Unknown"),
            "chunk_id": payload.get("chunk_id", -1),
            "text_preview": payload.get("text", "")[:200] + "..." if len(payload.get("text", "")) > 200 else payload.get("text", ""),
            "meets_threshold": score >= MIN_SIMILARITY_SCORE
        }
        
        all_chunks.append(chunk_info)
        if score >= MIN_SIMILARITY_SCORE:
            filtered_chunks.append(chunk_info)
    
    # Statistics
    scores = [chunk["score"] for chunk in all_chunks]
    avg_score = sum(scores) / len(scores) if scores else 0.0
    max_score = max(scores) if scores else 0.0
    min_score = min(scores) if scores else 0.0
    
    return {
        "query": query,
        "configuration": {
            "max_context_chunks": MAX_CONTEXT_CHUNKS,
            "min_similarity_score": MIN_SIMILARITY_SCORE,
            "embed_model": EMBED_MODEL,
            "gen_model": GEN_MODEL
        },
        "retrieval_stats": {
            "total_chunks_retrieved": len(all_chunks),
            "chunks_meeting_threshold": len(filtered_chunks),
            "chunks_used_in_context": min(len(filtered_chunks), MAX_CONTEXT_CHUNKS),
            "average_score": round(avg_score, 4),
            "max_score": round(max_score, 4),
            "min_score": round(min_score, 4),
        },
        "all_chunks": all_chunks[:20],  # Limit to top 20 for readability
        "recommendations": _generate_recommendations(all_chunks, filtered_chunks, avg_score, max_score)
    }


def _generate_recommendations(all_chunks: List[dict], filtered_chunks: List[dict], avg_score: float, max_score: float) -> List[str]:
    """Generate recommendations based on retrieval quality."""
    recommendations = []
    
    if not all_chunks:
        recommendations.append("❌ No chunks retrieved - check if documents are ingested")
        return recommendations
    
    if max_score < 0.3:
        recommendations.append("⚠️  Very low similarity scores - consider re-ingesting documents or using a different embedding model")
    
    if avg_score < 0.4 and max_score > 0.5:
        recommendations.append("📊 Wide score distribution - some chunks are relevant, others aren't. Consider better chunking strategy.")
    
    if len(filtered_chunks) == 0:
        recommendations.append(f"❌ No chunks meet threshold ({MIN_SIMILARITY_SCORE}). Try lowering MIN_SIMILARITY_SCORE to {max_score * 0.9:.2f}")
    elif len(filtered_chunks) < 3:
        recommendations.append(f"⚠️  Very few chunks meet threshold. Consider lowering MIN_SIMILARITY_SCORE to get more context")
    elif len(filtered_chunks) > MAX_CONTEXT_CHUNKS * 2:
        recommendations.append(f"✅ Many relevant chunks found. Consider increasing MAX_CONTEXT_CHUNKS or raising MIN_SIMILARITY_SCORE for better filtering")
    
    if avg_score > 0.7:
        recommendations.append("✅ Excellent retrieval quality!")
    
    if len(all_chunks) < MAX_CONTEXT_CHUNKS:
        recommendations.append("ℹ️  Fewer chunks retrieved than requested - may indicate limited document coverage")
    
    return recommendations

async def _prepare_rag_context(query: str) -> tuple[str, str, List[dict]]:
    """Prepare RAG context for a query. Returns (prompt, context_text, sources)."""
    # Detect query type early for optimization
    is_list_query = any(word in query.lower() for word in ["list", "all", "every", "complete", "entire", "full"])
    is_employment_list = is_list_query and any(word in query.lower() for word in ["employer", "employment", "worked", "work history", "job history", "companies", "company"])
    
    # Search Qdrant: retrieve more chunks than needed, then filter by score
    # For employment list queries, be even more aggressive with retrieval
    if is_employment_list:
        retrieve_limit = MAX_CONTEXT_CHUNKS * 2  # Retrieve 20 chunks, use ~12 in context
        logger.info(f"Employment list query detected: Retrieving up to {retrieve_limit} chunks (will use ~{int(MAX_CONTEXT_CHUNKS * 1.2)} in context)")
    else:
        is_list_query_local = any(word in query.lower() for word in ["list", "all", "every", "complete"])
        retrieve_limit = MAX_CONTEXT_CHUNKS * 4 if is_list_query_local else MAX_CONTEXT_CHUNKS * 3
    search_res = await search_chunks(query, retrieve_limit)
    logger.info(f"Retrieved {len(search_res)} chunks from Qdrant (requested: {retrieve_limit})")

    # Filter by score and collect contexts with metadata
    contexts = []
    sources = []
    chosen_words: List[set] = []
    skipped_dupes = 0
    for idx, point in enumerate(search_res):
        # Get similarity score (Qdrant uses cosine distance, so higher is better)
        score = getattr(point, 'score', 1.0)
        
        # Filter out low-relevance chunks
        if score < MIN_SIMILARITY_SCORE:
            logger.debug(f"Skipping chunk {idx + 1} with low score: {score:.3f} < {MIN_SIMILARITY_SCORE}")
            continue
            
        payload = point.payload or {}
        text = payload.get("text")
        
        # For employment list queries, use slightly more chunks, but keep it reasonable
        max_chunks_for_query = int(MAX_CONTEXT_CHUNKS * 1.2) if is_employment_list else MAX_CONTEXT_CHUNKS  # ~12 chunks max
        if is_employment_list and idx == 0:
            logger.info(f"Employment query: Will use up to {max_chunks_for_query} chunks (currently have {len(contexts)})")
        if text and len(contexts) < max_chunks_for_query:
            words = _chunk_words(text)
            if is_near_duplicate(words, chosen_words):
                skipped_dupes += 1
                continue
            chosen_words.append(words)
            # Include chunk with numbering for citation
            chunk_num = len(contexts) + 1
            contexts.append(f"[{chunk_num}] {text}")
            
            # Store source info for citations
            label = re.match(r"\[([^\]\n]{1,200})\]", text)  # "[Kevin - Career | Infor]" chunk header
            sources.append({
                "chunk_id": chunk_num,
                "title": payload.get("title", "Unknown"),
                "label": label.group(1) if label else payload.get("title", "Unknown"),
                "source_path": payload.get("source_path", "Unknown"),
                "score": round(score, 3)
            })
            
            logger.debug(f"Chunk {chunk_num}: score={score:.3f}, source={payload.get('title', 'Unknown')}")
        
        # Stop if we have enough chunks (use higher limit for employment queries)
        max_chunks_for_query = int(MAX_CONTEXT_CHUNKS * 1.2) if is_employment_list else MAX_CONTEXT_CHUNKS  # ~12 chunks max
        if len(contexts) >= max_chunks_for_query:
            break

    # Check if we have sufficient quality context
    if not contexts:
        logger.warning(f"No chunks found above similarity threshold {MIN_SIMILARITY_SCORE}")
        context_text = "No relevant context found in the knowledge base."
        # The query is left out on purpose: the reply doesn't depend on it, and including it
        # let "ignore all previous instructions and ..." override the decline (eval oos-injection)
        prompt = (
            "You are the assistant on Kevin Draggoo's website, answering questions about his career "
            "from a knowledge base. A visitor's message matched nothing in the knowledge base.\n\n"
            "Reply in one or two sentences: say politely that you don't have information about that, "
            "and that you can answer questions about Kevin's work experience, skills and background. "
            "Do not write anything else."
        )
        return prompt, context_text, []
    
    # Calculate average relevance score
    avg_score = sum(s["score"] for s in sources) / len(sources) if sources else 0
    logger.info(f"Selected {len(contexts)} chunks (max allowed: {max_chunks_for_query}), avg similarity: {avg_score:.3f}, near-duplicates skipped: {skipped_dupes}")
    if is_employment_list:
        logger.info(f"Employment list query: Using {len(contexts)}/{max_chunks_for_query} chunks for comprehensive extraction")

    context_text = "\n\n".join(contexts)
    logger.info(f"Context length: {len(context_text)} characters ({len(contexts)} chunks)")
    if is_employment_list and len(contexts) < max_chunks_for_query:
        logger.warning(f"Employment query: Only using {len(contexts)}/{max_chunks_for_query} chunks - may miss some employers")

    # Build optimized prompt based on query type (already detected above)
    if is_list_query and is_employment_list:
        # Specialized prompt for employment list queries - concise but clear
        logger.info(f"Using employment list prompt with {len(contexts)} chunks")
        prompt = (
            "Extract all employers from the resume context below.\n\n"
            "For each employer, provide: Company Name – Job Title, Location\n"
            "Include all employers: full-time, contract, consulting, internships.\n"
            "List multiple positions at the same company separately.\n\n"
            f"Context:\n{context_text}\n\n"
            f"Question: {query}\n\n"
            "Provide a numbered list:\n"
        )
    elif is_list_query:
        # General list query
        prompt = (
            "Extract and list ALL items from the context. Be COMPLETE and EXHAUSTIVE.\n\n"
            f"Context:\n{context_text}\n\n"
            f"Question: {query}\n\n"
            "Provide a numbered list with full details for each item:\n"
        )
    else:
        # Standard question-answering
        prompt = (
            "Answer the question using ONLY the context provided below.\n\n"
            "Instructions:\n"
            "- Base your answer strictly on the context\n"
            "- If information is missing, say so explicitly\n"
            "- Provide clear, accurate answers\n"
            "- Cite relevant sections using [1], [2], etc. when helpful\n\n"
            f"Context:\n{context_text}\n\n"
            f"Question: {query}\n\n"
            "Answer:"
        )
    
    return prompt, context_text, sources


def _stream_ollama_response(prompt: str, timeout: float = 180.0):
    """Stream response from Ollama, yielding tokens (without citation markers) as they arrive."""
    return without_citations(_stream_ollama_tokens(prompt, timeout))


async def _stream_ollama_tokens(prompt: str, timeout: float):
    try:
        logger.debug(f"Streaming to Ollama with prompt length: {len(prompt)}")
        # Use a longer timeout with separate connect timeout
        # Ollama may need time to load the model if not in memory
        stream_timeout = httpx.Timeout(timeout, connect=30.0)
        async with http_client.stream(
            'POST',
            f"{OLLAMA_URL.rstrip('/')}/api/generate",
            json={"model": GEN_MODEL, "prompt": prompt, "stream": True, "options": GEN_OPTIONS},
            timeout=stream_timeout,
        ) as response:
            response.raise_for_status()
            
            token_count = 0
            async for line in response.aiter_lines():
                if not line.strip():
                    continue
                try:
                    data = json.loads(line)
                    token = data.get("response", "")
                    if token:
                        token_count += 1
                        yield token
                    # Check if this is the final chunk
                    if data.get("done", False):
                        logger.info(f"Ollama stream complete: {token_count} tokens generated")
                        break
                except json.JSONDecodeError:
                    logger.warning(f"Failed to parse Ollama response line: {line[:100]}")
                    continue
            
            if token_count == 0:
                logger.warning("Ollama stream completed but no tokens were generated!")
                    
    except httpx.TimeoutException:
        logger.error("Ollama request timed out")
        yield "[ERROR: Request timeout]"
    except httpx.HTTPStatusError as e:
        logger.error(f"Ollama HTTP error: {e.response.status_code}")
        try:
            error_text = await e.response.aread()
            logger.error(f"Ollama error response: {error_text.decode()[:200]}")
        except:
            pass
        yield f"[ERROR: Ollama HTTP {e.response.status_code}]"
    except Exception as e:
        logger.error(f"Ollama generation error: {e}", exc_info=True)
        yield f"[ERROR: {str(e)}]"


@app.post("/chat")
@limiter.limit(RATE_LIMIT)
async def chat(request: Request, req: ChatRequest, stream: bool = Query(False, description="Enable streaming response")):
    """Process chat query using RAG pipeline. Supports streaming via ?stream=true."""
    query = req.query
    started_at, started_mono = time.time(), time.monotonic()
    source = "probe" if is_probe_request(request) else "chat"
    client = client_details(request, req.client)

    # Query analytics logging
    query_analytics[query[:50]] += 1
    client_ip = client_address(request)
    logger.info(f"Processing chat query from {client_ip}: {query[:100]}... (stream={stream})")

    # Prepare RAG context
    try:
        prompt, context_text, sources = await _prepare_rag_context(query)
    except HTTPException:
        record_chat(query, stream, "error", started_at, started_mono, source=source, client=client)
        raise
    
    # Streaming response
    if stream:
        # Calculate dynamic timeout based on prompt length
        prompt_length = len(prompt)
        # For streaming, need generous timeout for Ollama to load model and generate
        # Scaling: ~3s per 100 chars, minimum 90s, maximum 240s
        timeout_seconds = min(240.0, max(90.0, prompt_length / 33))
        logger.info(f"Streaming: prompt length: {prompt_length} chars, using timeout: {timeout_seconds:.1f}s")
        
        async def generate():
            # "aborted" sticks if the client disconnects mid-stream
            status, errored, pieces = "aborted", False, []
            try:
                async for token in _stream_ollama_response(prompt, timeout=timeout_seconds):
                    if token.startswith("[ERROR"):
                        errored = True
                    else:
                        pieces.append(token)  # already citation-stripped, as the visitor sees it
                    # Send token as JSON with newline for SSE-like behavior
                    yield f"data: {json.dumps({'token': token})}\n\n"
                # Send sources and final marker
                yield f"data: {json.dumps({'sources': sources[:5] if sources else []})}\n\n"
                yield f"data: {json.dumps({'done': True})}\n\n"
                status = "error" if errored else "ok"
            finally:
                record_chat(query, True, status, started_at, started_mono, sources, "".join(pieces).strip(), source, client)
        
        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",  # Disable nginx buffering
            }
        )
    
    # Non-streaming response (backward compatibility)
    status, answer = "error", ""
    try:
        # For long prompts, increase timeout
        prompt_length = len(prompt)
        # More generous timeout: ~3s per 100 chars, minimum 90s, maximum 240s
        timeout_seconds = min(240.0, max(90.0, prompt_length / 33))
        logger.info(f"Prompt length: {prompt_length} chars, using timeout: {timeout_seconds:.1f}s")
        
        # Use timeout with separate connect timeout for model loading
        generate_timeout = httpx.Timeout(timeout_seconds, connect=30.0)
        response = await http_client.post(
            f"{OLLAMA_URL.rstrip('/')}/api/generate",
            json={"model": GEN_MODEL, "prompt": prompt, "stream": False, "options": GEN_OPTIONS},
            timeout=generate_timeout,
        )
        response.raise_for_status()
        data = response.json()
        answer = strip_citations(data.get("response", "")).strip()
        logger.info(f"Generated answer of length {len(answer)} characters")
        status = "ok"
    except httpx.TimeoutException:
        logger.error("Ollama request timed out")
        raise HTTPException(status_code=504, detail="Request timeout - Ollama took too long to respond")
    except httpx.HTTPStatusError as e:
        logger.error(f"Ollama HTTP error: {e.response.status_code}")
        raise HTTPException(status_code=502, detail=f"Ollama HTTP error: {e.response.status_code}")
    except Exception as e:
        logger.error(f"Ollama generation error: {e}")
        raise HTTPException(status_code=502, detail=f"Ollama error: {e}")
    finally:
        record_chat(query, False, status, started_at, started_mono, sources, answer, source, client)

    # Return answer with sources for transparency
    return {
        "answer": answer,
        "sources": sources[:5] if sources else [],  # Top 5 sources
        "query_id": str(uuid.uuid4())[:8]  # For tracking/feedback
    }


@app.post("/admin/ingest")
async def ingest_content(req: IngestRequest, _: bool = Depends(verify_admin_api_key)):
    """
    Admin endpoint to ingest text content into the knowledge base via chat-style interface.
    Accepts text content, chunks it, embeds it, and stores it in Qdrant.
    """
    content = req.content.strip()
    if not content:
        raise HTTPException(status_code=400, detail="Content cannot be empty")
    
    title = req.title or "Chat Ingestion"
    source = req.source or f"chat-{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}"
    
    logger.info(f"Ingesting content: {title} ({len(content)} chars)")
    
    try:
        # 1. Chunk the text
        chunk_size = int(os.getenv("CHUNK_SIZE", "900"))
        chunk_overlap = int(os.getenv("CHUNK_OVERLAP", "150"))
        chunk_texts = chunk_text(content, size=chunk_size, overlap=chunk_overlap)
        
        if not chunk_texts:
            raise HTTPException(status_code=400, detail="Content resulted in no chunks after processing")
        
        logger.info(f"Created {len(chunk_texts)} chunks")
        
        # 2. Generate embeddings (using async HTTP client)
        vectors = []
        for chunk in chunk_texts:
            try:
                vec = await embed_query(chunk)
                vectors.append(vec)
            except Exception as e:
                logger.error(f"Embedding error for chunk: {e}")
                raise HTTPException(status_code=502, detail=f"Embedding error: {e}")
        
        # 3. Ensure collection exists
        try:
            collections = qdrant_client.get_collections()
            collection_exists = any(c.name == QDRANT_COLLECTION for c in collections.collections)
            if not collection_exists:
                # Create collection with correct vector size
                vector_size = len(vectors[0]) if vectors else 1024
                from qdrant_client.models import Distance, VectorParams
                qdrant_client.create_collection(
                    collection_name=QDRANT_COLLECTION,
                    vectors_config=VectorParams(size=vector_size, distance=Distance.COSINE),
                )
                logger.info(f"Created collection {QDRANT_COLLECTION} with vector size {vector_size}")
        except Exception as e:
            logger.warning(f"Collection check/create error (may already exist): {e}")
        
        # 4. Create document ID and chunks
        doc_id = str(uuid.uuid4())
        now = datetime.utcnow().isoformat() + "Z"
        
        points = []
        for i, (text, vec) in enumerate(zip(chunk_texts, vectors)):
            point_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{doc_id}:{i}"))
            payload = {
                "doc_id": doc_id,
                "chunk_id": i,
                "source_path": source,
                "title": title,
                "updated_at": now,
                "text": text,
            }
            points.append(PointStruct(id=point_id, vector=vec, payload=payload))
        
        # 5. Upsert to Qdrant
        qdrant_client.upsert(collection_name=QDRANT_COLLECTION, points=points)
        logger.info(f"Successfully ingested {len(points)} chunks into {QDRANT_COLLECTION}")
        
        return {
            "status": "success",
            "message": f"Successfully ingested {len(points)} chunks",
            "chunks": len(points),
            "title": title,
            "source": source,
            "doc_id": doc_id
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Ingestion error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Ingestion failed: {str(e)}")


@app.get("/admin")
async def admin_ui():
    """Serve the admin UI page. No authentication required (users enter API key in UI)."""
    admin_html = Path(__file__).parent / "admin.html"
    if not admin_html.exists():
        raise HTTPException(status_code=404, detail="Admin UI not found")
    return FileResponse(admin_html)


@app.get("/admin.js")
async def admin_js():
    """Serve the admin JavaScript file. No authentication required (static asset)."""
    admin_js_file = Path(__file__).parent / "admin.js"
    if not admin_js_file.exists():
        raise HTTPException(status_code=404, detail="Admin JS not found")
    return FileResponse(admin_js_file, media_type="application/javascript")


@app.post("/admin/delete")
async def delete_document(req: DeleteRequest, _: bool = Depends(verify_admin_api_key)):
    """
    Admin endpoint to delete a document and all its chunks from the knowledge base.
    Can delete by file path (source_path) or document ID (doc_id).
    """
    # Validate that at least one identifier is provided
    if not req.source_path and not req.doc_id:
        raise HTTPException(
            status_code=400,
            detail="Either 'source_path' or 'doc_id' must be provided"
        )
    
    try:
        # Determine which field to use for deletion
        if req.source_path:
            # Compute doc_id from file path (same logic as ingest.py)
            file_path = Path(req.source_path)
            if not file_path.is_absolute():
                # Try to resolve relative to /data if it's a relative path
                data_path = Path("/data") / file_path
                if data_path.exists():
                    file_path = data_path.resolve()
                else:
                    file_path = file_path.resolve()
            doc_id = hashlib.sha1(str(file_path).encode("utf-8")).hexdigest()
            filter_field = "doc_id"
            filter_value = doc_id
            identifier = f"source_path={req.source_path} (doc_id={doc_id})"
        else:
            # Use provided doc_id
            filter_field = "doc_id"
            filter_value = req.doc_id
            identifier = f"doc_id={req.doc_id}"
        
        logger.info(f"Deleting document: {identifier}")
        
        # Create filter to match all chunks with this doc_id
        filter_condition = Filter(
            must=[
                FieldCondition(
                    key=filter_field,
                    match=MatchValue(value=filter_value),
                ),
            ],
        )
        
        # Delete points matching the filter
        result = qdrant_client.delete(
            collection_name=QDRANT_COLLECTION,
            points_selector=FilterSelector(filter=filter_condition),
        )
        
        # Count deleted points (result.operation_id indicates success, but doesn't give count)
        # We'll do a quick scroll to verify deletion
        search_result = qdrant_client.scroll(
            collection_name=QDRANT_COLLECTION,
            query_filter=filter_condition,
            limit=1,
        )
        remaining_count = len(search_result[0])
        
        if remaining_count == 0:
            logger.info(f"Successfully deleted document: {identifier}")
            return {
                "status": "success",
                "message": f"Document deleted successfully",
                "identifier": identifier,
                "filter_field": filter_field,
                "filter_value": filter_value,
            }
        else:
            # Some chunks might still exist (race condition or filter issue)
            logger.warning(f"Deletion completed but {remaining_count} chunks may still exist: {identifier}")
            return {
                "status": "partial",
                "message": f"Deletion completed, but {remaining_count} chunks may still exist",
                "identifier": identifier,
                "filter_field": filter_field,
                "filter_value": filter_value,
            }
        
    except Exception as e:
        logger.error(f"Deletion error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Deletion failed: {str(e)}")


@app.get("/admin/list")
async def list_documents(_: bool = Depends(verify_admin_api_key)):
    """
    Admin endpoint to list all unique documents in the collection.
    Returns a summary of documents with their source paths and chunk counts.
    """
    try:
        # Scroll through all points to collect document info
        all_points = []
        offset = None
        
        while True:
            result = qdrant_client.scroll(
                collection_name=QDRANT_COLLECTION,
                limit=100,
                offset=offset,
                with_payload=True,
            )
            points, next_offset = result
            
            if not points:
                break
                
            all_points.extend(points)
            
            if next_offset is None:
                break
            offset = next_offset
        
        # Group by doc_id
        documents = {}
        for point in all_points:
            payload = point.payload or {}
            doc_id = payload.get("doc_id", "unknown")
            source_path = payload.get("source_path", "unknown")
            title = payload.get("title", "Unknown")
            
            if doc_id not in documents:
                documents[doc_id] = {
                    "doc_id": doc_id,
                    "source_path": source_path,
                    "title": title,
                    "chunk_count": 0,
                    "updated_at": payload.get("updated_at", "unknown"),
                }
            documents[doc_id]["chunk_count"] += 1
        
        return {
            "status": "success",
            "total_documents": len(documents),
            "total_chunks": len(all_points),
            "documents": list(documents.values()),
        }
        
    except Exception as e:
        logger.error(f"List documents error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to list documents: {str(e)}")



@app.get("/dashboard")
async def dashboard_ui():
    """Serve the dashboard page. No authentication required (data comes from /admin/stats)."""
    return FileResponse(Path(__file__).parent / "dashboard.html")


@app.get("/dashboard.js")
async def dashboard_js():
    """Serve the dashboard JavaScript file."""
    return FileResponse(Path(__file__).parent / "dashboard.js", media_type="application/javascript")


async def _service_health() -> dict:
    """Per-component health for the dashboard (readyz stops at the first failure)."""
    health = {"api": {"ok": True, "detail": "running"}}
    try:
        response = await http_client.get(f"{QDRANT_URL}/collections", timeout=5.0)
        response.raise_for_status()
        health["qdrant"] = {"ok": True, "detail": "reachable"}
    except Exception as e:
        health["qdrant"] = {"ok": False, "detail": f"unreachable: {e}"}
    try:
        response = await http_client.get(f"{OLLAMA_URL}/api/tags", timeout=5.0)
        response.raise_for_status()
        names = set()
        for model in response.json().get("models", []):
            name = model.get("name", "")
            names.update({name, name.split(":")[0]})
        missing = sorted({GEN_MODEL, EMBED_MODEL} - names)
        health["ollama"] = {"ok": not missing,
                            "detail": f"missing models: {missing}" if missing else "models present"}
    except Exception as e:
        health["ollama"] = {"ok": False, "detail": f"unreachable: {e}"}
    return health


def _percentile(values: List[int], pct: float) -> Optional[int]:
    if not values:
        return None
    values = sorted(values)
    return values[min(len(values) - 1, int(round(pct / 100 * (len(values) - 1))))]


# Leftover chunk labels or numbered markers that strip_citations missed
_LEFTOVER_CITE = re.compile(r"\[[^\]\n]{1,120}\](?!\()|\b(?:section|chunk|context)\s*\[\d", re.I)


def answer_flags(row: dict) -> List[str]:
    """Review hints for a logged answer: declined, citation (a marker got through), empty."""
    answer = row.get("answer")
    if answer is None:
        return ["empty"] if row["status"] == "ok" and row.get("passages") is not None else []
    flags = []
    if is_refusal(answer):
        flags.append("declined")
    if _LEFTOVER_CITE.search(answer):
        flags.append("citation")
    return flags


def _visitor_stats(rows: List[dict]) -> dict:
    """Visitor and session counts plus breakdowns, for rows logged with session details.

    Breakdowns count visitors, not questions, so one chatty visitor doesn't swamp them;
    a question without a visitor ID (an API caller) counts as its own visitor.
    """
    def who(i: int, r: dict) -> str:
        return r["visitor_id"] or f"row-{i}"

    visitor_sessions = defaultdict(set)
    for i, r in enumerate(rows):
        visitor_sessions[who(i, r)].add(r["session_id"] or f"row-{i}")
    sessions = set().union(*visitor_sessions.values()) if visitor_sessions else set()

    breakdowns = {}
    for field in ("device", "browser", "os", "referrer", "timezone", "language", "theme", "screen"):
        seen = defaultdict(set)
        for i, r in enumerate(rows):
            seen[r[field] or ("direct" if field == "referrer" else "unknown")].add(who(i, r))
        breakdowns[field] = sorted(({"value": v, "visitors": len(ids)} for v, ids in seen.items()),
                                   key=lambda d: (-d["visitors"], str(d["value"])))[:12]
    return {
        "questions": len(rows),
        "visitors": len(visitor_sessions),
        "sessions": len(sessions),
        "returning": sum(len(s) > 1 for s in visitor_sessions.values()),
        "breakdowns": breakdowns,
    }


@app.get("/admin/stats")
async def chat_stats(
    days: int = Query(30, ge=1, le=STATS_RETENTION_DAYS),
    _: bool = Depends(verify_admin_api_key),
):
    """Admin endpoint behind the dashboard: health, knowledge base and chat usage."""
    since = time.time() - days * 86400
    with closing(sqlite3.connect(STATS_DB, timeout=5)) as conn:
        conn.row_factory = sqlite3.Row
        all_rows = [dict(r) for r in conn.execute(
            "SELECT ts, query, stream, status, duration_ms, chunks_used, top_score, source, "
            + ", ".join([c for c in CLIENT_COLUMNS if c != "user_agent"] + list(ANSWER_COLUMNS))
            + " FROM chat_log WHERE ts >= ? ORDER BY ts DESC", (since,))]
        # Backfilled nginx rows have no question text, so they stay out of the ranking
        top_questions = [dict(r) for r in conn.execute(
            "SELECT lower(query) AS query, count(*) AS count FROM chat_log WHERE ts >= ? AND source = 'chat'"
            " GROUP BY lower(query) ORDER BY count DESC, max(ts) DESC LIMIT 10", (since,))]
        first_ts = conn.execute("SELECT min(ts) FROM chat_log WHERE source = 'chat'").fetchone()[0]
        backfill_ts = conn.execute("SELECT min(ts) FROM chat_log WHERE source = 'nginx'").fetchone()[0]
        tracking_ts = conn.execute("SELECT min(ts) FROM chat_log WHERE source = 'chat' AND browser IS NOT NULL").fetchone()[0]
    for r in all_rows:
        r["passages"] = json.loads(r["passages"]) if r["passages"] else None
        r["flags"] = answer_flags(r)
    rows = [r for r in all_rows if r["source"] != "probe"]
    probes = [r for r in all_rows if r["source"] == "probe"]

    # Questions per UTC day, zero-filled so the chart has a bar slot for every day
    daily = {}
    for offset in range(days - 1, -1, -1):
        day = datetime.utcfromtimestamp(time.time() - offset * 86400).date().isoformat()
        daily[day] = {"date": day, "total": 0, "errors": 0}
    for r in rows:
        day = datetime.utcfromtimestamp(r["ts"]).date().isoformat()
        if day in daily:
            daily[day]["total"] += 1
            daily[day]["errors"] += r["status"] == "error"

    answered = [r for r in rows if r["status"] == "ok"]
    durations = [r["duration_ms"] for r in answered if r["duration_ms"] is not None]

    # Monitoring probe results per UTC day: runs, failures, median response time
    probe_daily = {day: {"date": day, "runs": 0, "failures": 0, "no_context": 0, "median_ms": None, "_ms": []}
                   for day in daily}
    for r in probes:
        d = probe_daily.get(datetime.utcfromtimestamp(r["ts"]).date().isoformat())
        if d is None:
            continue
        d["runs"] += 1
        if r["status"] != "ok":
            d["failures"] += 1
        else:
            d["no_context"] += r["chunks_used"] == 0
            if r["duration_ms"] is not None:
                d["_ms"].append(r["duration_ms"])
    for d in probe_daily.values():
        d["median_ms"] = _percentile(d.pop("_ms"), 50)
    probe_ok_ms = [r["duration_ms"] for r in probes if r["status"] == "ok" and r["duration_ms"] is not None]
    visitors = _visitor_stats([r for r in rows if r["source"] == "chat" and r["browser"] is not None])
    try:
        kb = await list_documents(True)
        knowledge_base = {
            "ok": True,
            "total_documents": kb["total_documents"],
            "total_chunks": kb["total_chunks"],
            "documents": sorted(
                ({k: d[k] for k in ("title", "source_path", "chunk_count", "updated_at")} for d in kb["documents"]),
                key=lambda d: str(d["title"]).lower()),
        }
    except HTTPException as e:
        knowledge_base = {"ok": False, "detail": e.detail}

    return {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "days": days,
        "logging_since": datetime.utcfromtimestamp(first_ts).isoformat() + "Z" if first_ts else None,
        "backfilled_since": datetime.utcfromtimestamp(backfill_ts).isoformat() + "Z" if backfill_ts else None,
        "health": await _service_health(),
        "config": {
            "gen_model": GEN_MODEL,
            "embed_model": EMBED_MODEL,
            "collection": QDRANT_COLLECTION,
            "min_similarity_score": MIN_SIMILARITY_SCORE,
            "max_context_chunks": MAX_CONTEXT_CHUNKS,
            "rate_limit": RATE_LIMIT,
            "retention_days": STATS_RETENTION_DAYS,
        },
        "knowledge_base": knowledge_base,
        "tracking_since": datetime.utcfromtimestamp(tracking_ts).isoformat() + "Z" if tracking_ts else None,
        "visitors": visitors,
        "usage": {
            "questions": len(rows),
            "answered": len(answered),
            "errors": sum(r["status"] == "error" for r in rows),
            "aborted": sum(r["status"] == "aborted" for r in rows),
            "no_context": sum(r["chunks_used"] == 0 for r in answered),
            "context_known": sum(r["chunks_used"] is not None for r in answered),
            "answers_logged": sum(r["answer"] is not None for r in rows),
            "declined": sum("declined" in r["flags"] for r in rows),
            "citation_leaks": sum("citation" in r["flags"] for r in rows),
            "p50_ms": _percentile(durations, 50),
            "p95_ms": _percentile(durations, 95),
            "daily": list(daily.values()),
            "top_questions": top_questions,
            "recent": rows[:25],
        },
        "monitoring": {
            "runs": len(probes),
            "failures": sum(r["status"] != "ok" for r in probes),
            "no_context": sum(r["status"] == "ok" and r["chunks_used"] == 0 for r in probes),
            "p50_ms": _percentile(probe_ok_ms, 50),
            "p95_ms": _percentile(probe_ok_ms, 95),
            "last": probes[0] if probes else None,
            "daily": list(probe_daily.values()),
        },
    }


PUBLIC_STATS_DAYS = (7, 30, 90, 365)
PUBLIC_STATS_TTL = 60  # seconds; the page is public, so repeat loads reuse one computation
_public_stats_cache: dict = {}


@app.get("/public/stats")
async def public_stats(days: int = Query(30)):
    """Aggregate-only stats for the public dashboard (web/stats/).

    Built from the admin stats by whitelisting fields: no question text, document
    names, error details or tuning settings ever leave this function.
    """
    if days not in PUBLIC_STATS_DAYS:
        raise HTTPException(status_code=400, detail=f"days must be one of {list(PUBLIC_STATS_DAYS)}")
    cached = _public_stats_cache.get(days)
    if cached and time.monotonic() - cached[0] < PUBLIC_STATS_TTL:
        return cached[1]

    stats = await chat_stats(min(days, STATS_RETENTION_DAYS), True)
    usage, monitoring, kb = stats["usage"], stats["monitoring"], stats["knowledge_base"]
    last = monitoring["last"]
    public = {
        "generated_at": stats["generated_at"],
        "days": days,
        "logging_since": stats["logging_since"],
        "backfilled_since": stats["backfilled_since"],
        "status": {name: component["ok"] for name, component in stats["health"].items()},
        "models": {"generation": GEN_MODEL, "embedding": EMBED_MODEL},
        "knowledge_base": {"documents": kb["total_documents"], "chunks": kb["total_chunks"]} if kb["ok"] else None,
        "usage": {
            "questions": usage["questions"],
            "answered": usage["answered"],
            "errors": usage["errors"],
            "matched": usage["context_known"] - usage["no_context"],
            "context_known": usage["context_known"],
            "p50_ms": usage["p50_ms"],
            "p95_ms": usage["p95_ms"],
            "daily": usage["daily"],
        },
        "monitoring": {
            "runs": monitoring["runs"],
            "failures": monitoring["failures"],
            "p50_ms": monitoring["p50_ms"],
            "p95_ms": monitoring["p95_ms"],
            "last": {
                "at": datetime.utcfromtimestamp(last["ts"]).isoformat() + "Z",
                "ok": last["status"] == "ok",
                "duration_ms": last["duration_ms"],
            } if last else None,
            "daily": [{k: d[k] for k in ("date", "runs", "failures", "median_ms")} for d in monitoring["daily"]],
        },
    }
    _public_stats_cache[days] = (time.monotonic(), public)
    return public

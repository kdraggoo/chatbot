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
from pydantic import BaseModel, Field, field_validator
from qdrant_client import QdrantClient
from qdrant_client.models import PointStruct, FilterSelector, Filter, FieldCondition, MatchValue
from typing import List, Optional
import httpx
from rag.ingest import chunk_text
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
EMBED_MODEL = os.getenv("EMBED_MODEL", "bge-m3")
QDRANT_COLLECTION = os.getenv("QDRANT_COLLECTION", "docs")
MAX_QUERY_LENGTH = int(os.getenv("MAX_QUERY_LENGTH", "2000"))
MAX_CONTEXT_CHUNKS = int(os.getenv("MAX_CONTEXT_CHUNKS", "10"))  # Optimized for speed
MIN_SIMILARITY_SCORE = float(os.getenv("MIN_SIMILARITY_SCORE", "0.3"))  # Balanced threshold
ADMIN_API_KEY = os.getenv("ADMIN_API_KEY", "")  # Admin API key for authentication
RATE_LIMIT = os.getenv("RATE_LIMIT", "10/minute")  # Rate limit for /chat endpoint
STATS_DB = os.getenv("STATS_DB", "/stats/chat.db")  # SQLite chat log for the dashboard
STATS_RETENTION_DAYS = int(os.getenv("STATS_RETENTION_DAYS", "90"))

# Initialize rate limiter
limiter = Limiter(key_func=get_remote_address)

# Query analytics storage (in-memory, could be persisted to file/db)
query_analytics = defaultdict(int)



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
        if "source" not in {row[1] for row in conn.execute("PRAGMA table_info(chat_log)")}:
            conn.execute("ALTER TABLE chat_log ADD COLUMN source TEXT NOT NULL DEFAULT 'chat'")
        conn.execute("CREATE INDEX IF NOT EXISTS chat_log_ts ON chat_log(ts)")
    logger.info(f"Chat stats DB ready at {STATS_DB}")


def record_chat(query: str, stream: bool, status: str, started_at: float, started_mono: float,
                sources: Optional[List[dict]] = None, answer_chars: int = 0, source: str = "chat"):
    """Log one /chat request for the dashboard. Never lets a logging failure break chat."""
    try:
        duration_ms = int((time.monotonic() - started_mono) * 1000)
        top_score = max((s["score"] for s in sources), default=None) if sources else None
        with closing(sqlite3.connect(STATS_DB, timeout=5)) as conn, conn:
            conn.execute(
                "INSERT INTO chat_log (ts, query, stream, status, duration_ms, chunks_used, top_score, answer_chars, source)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (started_at, query[:500], int(stream), status, duration_ms,
                 None if sources is None else len(sources), top_score, answer_chars, source),
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
    
    try:
        query_vec = await embed_query(query)
    except Exception as e:
        logger.error(f"Embedding error: {e}")
        raise HTTPException(status_code=502, detail=f"Embedding error: {e}")
    
    try:
        # Get more results for analysis
        search_res = qdrant_client.search(
            collection_name=QDRANT_COLLECTION,
            query_vector=query_vec,
            limit=MAX_CONTEXT_CHUNKS * 3,  # Get more for analysis
            with_payload=True,
        )
    except Exception as e:
        logger.error(f"Qdrant search error: {e}")
        raise HTTPException(status_code=502, detail=f"Qdrant error: {e}")
    
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
    
    # 1) Embed the query
    try:
        query_vec = await embed_query(query)
    except Exception as e:
        logger.error(f"Embedding error: {e}")
        raise HTTPException(status_code=502, detail=f"Embedding error: {e}")

    # 2) Search Qdrant for top-k chunks
    try:
        # Retrieve more chunks than needed, then filter by score
        # For employment list queries, be even more aggressive with retrieval
        if is_employment_list:
            retrieve_limit = MAX_CONTEXT_CHUNKS * 2  # Retrieve 20 chunks, use ~12 in context
            logger.info(f"Employment list query detected: Retrieving up to {retrieve_limit} chunks (will use ~{int(MAX_CONTEXT_CHUNKS * 1.2)} in context)")
        else:
            is_list_query_local = any(word in query.lower() for word in ["list", "all", "every", "complete"])
            retrieve_limit = MAX_CONTEXT_CHUNKS * 4 if is_list_query_local else MAX_CONTEXT_CHUNKS * 2
        search_res = qdrant_client.search(
            collection_name=QDRANT_COLLECTION,
            query_vector=query_vec,
            limit=retrieve_limit,
            with_payload=True,
        )
        logger.info(f"Retrieved {len(search_res)} chunks from Qdrant (requested: {retrieve_limit})")
    except Exception as e:
        logger.error(f"Qdrant search error: {e}")
        raise HTTPException(status_code=502, detail=f"Qdrant error: {e}")

    # Filter by score and collect contexts with metadata
    contexts = []
    sources = []
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
            # Include chunk with numbering for citation
            chunk_num = len(contexts) + 1
            contexts.append(f"[{chunk_num}] {text}")
            
            # Store source info for citations
            sources.append({
                "chunk_id": chunk_num,
                "title": payload.get("title", "Unknown"),
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
        prompt = (
            "You are a helpful assistant. The user asked a question, but no relevant information "
            "was found in the knowledge base.\n\n"
            f"Question: {query}\n\n"
            "Please respond politely that you don't have sufficient information to answer this question "
            "based on the available knowledge base. Do not make up information."
        )
        return prompt, context_text, []
    
    # Calculate average relevance score
    avg_score = sum(s["score"] for s in sources) / len(sources) if sources else 0
    logger.info(f"Selected {len(contexts)} chunks (max allowed: {max_chunks_for_query}), avg similarity: {avg_score:.3f}")
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


async def _stream_ollama_response(prompt: str, timeout: float = 180.0):
    """Stream response from Ollama, yielding tokens as they arrive."""
    try:
        logger.debug(f"Streaming to Ollama with prompt length: {len(prompt)}")
        # Use a longer timeout with separate connect timeout
        # Ollama may need time to load the model if not in memory
        stream_timeout = httpx.Timeout(timeout, connect=30.0)
        async with http_client.stream(
            'POST',
            f"{OLLAMA_URL.rstrip('/')}/api/generate",
            json={"model": GEN_MODEL, "prompt": prompt, "stream": True},
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

    # Query analytics logging
    query_analytics[query[:50]] += 1
    client_ip = get_remote_address(request)
    logger.info(f"Processing chat query from {client_ip}: {query[:100]}... (stream={stream})")

    # Prepare RAG context
    try:
        prompt, context_text, sources = await _prepare_rag_context(query)
    except HTTPException:
        record_chat(query, stream, "error", started_at, started_mono, source=source)
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
            status, errored, answer_chars = "aborted", False, 0
            try:
                async for token in _stream_ollama_response(prompt, timeout=timeout_seconds):
                    if token.startswith("[ERROR"):
                        errored = True
                    else:
                        answer_chars += len(token)
                    # Send token as JSON with newline for SSE-like behavior
                    yield f"data: {json.dumps({'token': token})}\n\n"
                # Send sources and final marker
                yield f"data: {json.dumps({'sources': sources[:5] if sources else []})}\n\n"
                yield f"data: {json.dumps({'done': True})}\n\n"
                status = "error" if errored else "ok"
            finally:
                record_chat(query, True, status, started_at, started_mono, sources, answer_chars, source)
        
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
            json={"model": GEN_MODEL, "prompt": prompt, "stream": False},
            timeout=generate_timeout,
        )
        response.raise_for_status()
        data = response.json()
        answer = data.get("response", "").strip()
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
        record_chat(query, False, status, started_at, started_mono, sources, len(answer), source)

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
            "SELECT ts, query, stream, status, duration_ms, chunks_used, top_score, source"
            " FROM chat_log WHERE ts >= ? ORDER BY ts DESC", (since,))]
        # Backfilled nginx rows have no question text, so they stay out of the ranking
        top_questions = [dict(r) for r in conn.execute(
            "SELECT lower(query) AS query, count(*) AS count FROM chat_log WHERE ts >= ? AND source = 'chat'"
            " GROUP BY lower(query) ORDER BY count DESC, max(ts) DESC LIMIT 10", (since,))]
        first_ts = conn.execute("SELECT min(ts) FROM chat_log WHERE source = 'chat'").fetchone()[0]
        backfill_ts = conn.execute("SELECT min(ts) FROM chat_log WHERE source = 'nginx'").fetchone()[0]
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
        "usage": {
            "questions": len(rows),
            "answered": len(answered),
            "errors": sum(r["status"] == "error" for r in rows),
            "aborted": sum(r["status"] == "aborted" for r in rows),
            "no_context": sum(r["chunks_used"] == 0 for r in answered),
            "context_known": sum(r["chunks_used"] is not None for r in answered),
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

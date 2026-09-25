# RAG Quality Improvements Guide

## Quick Start Testing

```bash
# Enter container and run test script
docker exec -it chatbot-api-1 bash
cd /app
python test_quality.py
```

## Key Improvements

1. **Similarity Score Filtering** - Filters irrelevant chunks (default: 0.3)
2. **Better Prompts** - Reduced hallucination, added citations
3. **More Context** - Increased from 5 to 10 chunks
4. **Source Citations** - Answers include source metadata
5. **Diagnostic Endpoint** - `/diagnostic` for tuning help

## Tuning Thresholds

Set in `.env` or `docker-compose.yml`:
- `MIN_SIMILARITY_SCORE=0.3` (lower = more chunks, higher = stricter)
- `MAX_CONTEXT_CHUNKS=10` (adjust based on document length)

## Testing

```bash
# Test query
curl -X POST http://localhost:18000/chat -H "Content-Type: application/json" -d '{"query":"your question"}'

# Diagnostics
curl -X POST http://localhost:18000/diagnostic -H "Content-Type: application/json" -d '{"query":"your question"}'
```

See logs for similarity scores: `docker logs -f chatbot-api-1`

#!/usr/bin/env python3
"""
Quality testing script for the RAG chatbot.

Usage:
    python test_quality.py [--url http://localhost:18000] [--query "your question"]

Or run interactively for multiple queries.
"""

import argparse
import asyncio
import json
import sys
from typing import Dict, List
import httpx


async def test_query(
    client: httpx.AsyncClient,
    base_url: str,
    query: str,
    stream: bool = False,
    show_sources: bool = True
) -> Dict:
    """Test a single query and return results with metadata."""
    url = f"{base_url}/chat"
    params = {"stream": stream} if stream else {}
    
    try:
        if stream:
            response = await client.get(url, params=params, json={"query": query}, timeout=60.0)
            # Handle streaming (simplified for testing)
            print("Streaming response (showing first 500 chars)...")
            return {"answer": "[Streaming - check logs]", "query": query}
        else:
            response = await client.post(url, params=params, json={"query": query}, timeout=60.0)
            response.raise_for_status()
            data = response.json()
            return {
                "query": query,
                "answer": data.get("answer", ""),
                "sources": data.get("sources", []),
                "num_sources": len(data.get("sources", [])),
                "avg_score": sum(s["score"] for s in data.get("sources", [])) / len(data.get("sources", [])) if data.get("sources") else 0.0
            }
    except httpx.HTTPStatusError as e:
        return {
            "query": query,
            "error": f"HTTP {e.response.status_code}: {e.response.text[:200]}",
        }
    except Exception as e:
        return {
            "query": query,
            "error": str(e),
        }


async def test_diagnostic(client: httpx.AsyncClient, base_url: str, query: str) -> Dict:
    """Get diagnostic information about a query."""
    url = f"{base_url}/diagnostic"
    try:
        response = await client.post(url, json={"query": query}, timeout=30.0)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        return {"error": str(e)}


def print_results(result: Dict, detailed: bool = True):
    """Pretty print test results."""
    print("\n" + "="*80)
    print(f"Query: {result.get('query', 'N/A')}")
    print("-"*80)
    
    if "error" in result:
        print(f"❌ ERROR: {result['error']}")
        return
    
    answer = result.get("answer", "")
    sources = result.get("sources", [])
    
    print(f"Answer ({len(answer)} chars):")
    print(answer[:500] + ("..." if len(answer) > 500 else ""))
    print()
    
    if sources:
        print(f"Sources ({len(sources)} chunks):")
        for src in sources:
            print(f"  [{src['chunk_id']}] Score: {src['score']:.3f} | {src['title']}")
            if detailed:
                print(f"      Path: {src['source_path']}")
        
        avg_score = result.get("avg_score", 0)
        print(f"\n📊 Average relevance score: {avg_score:.3f}")
        
        if avg_score < 0.3:
            print("⚠️  WARNING: Low average relevance. Consider:")
            print("   - Lowering MIN_SIMILARITY_SCORE threshold")
            print("   - Re-ingesting with better chunking")
            print("   - Checking embedding model quality")
        elif avg_score > 0.7:
            print("✅ Excellent relevance scores!")
    else:
        print("⚠️  No sources found - may indicate:")
        print("   - Query too dissimilar to documents")
        print("   - MIN_SIMILARITY_SCORE threshold too high")
        print("   - Documents not properly ingested")
    
    print("="*80)


async def interactive_mode(client: httpx.AsyncClient, base_url: str):
    """Run in interactive mode."""
    print("\n🧪 RAG Quality Tester - Interactive Mode")
    print("Enter queries to test (or 'diagnostic <query>' for detailed info, 'quit' to exit)")
    print("-"*80)
    
    while True:
        try:
            query = input("\n> ").strip()
            if not query:
                continue
            if query.lower() in ('quit', 'exit', 'q'):
                break
            
            if query.startswith("diagnostic "):
                query_text = query[11:].strip()
                print(f"\n🔍 Running diagnostic for: {query_text}")
                diag = await test_diagnostic(client, base_url, query_text)
                print(json.dumps(diag, indent=2))
                continue
            
            print(f"\n⏳ Testing query...")
            result = await test_query(client, base_url, query, stream=False)
            print_results(result, detailed=True)
            
        except KeyboardInterrupt:
            print("\n\nExiting...")
            break
        except EOFError:
            break


async def main():
    parser = argparse.ArgumentParser(description="Test RAG chatbot quality")
    parser.add_argument("--url", default="http://localhost:18000", help="API base URL")
    parser.add_argument("--query", help="Single query to test")
    parser.add_argument("--file", help="File with queries (one per line)")
    parser.add_argument("--diagnostic", help="Run diagnostic mode for a query")
    args = parser.parse_args()
    
    base_url = args.url.rstrip('/')
    
    async with httpx.AsyncClient() as client:
        # Check health
        try:
            health = await client.get(f"{base_url}/healthz", timeout=5.0)
            if health.status_code != 200:
                print(f"❌ Health check failed: {health.status_code}")
                sys.exit(1)
        except Exception as e:
            print(f"❌ Cannot connect to {base_url}: {e}")
            sys.exit(1)
        
        if args.diagnostic:
            diag = await test_diagnostic(client, base_url, args.diagnostic)
            print(json.dumps(diag, indent=2))
        elif args.query:
            result = await test_query(client, base_url, args.query, stream=False)
            print_results(result, detailed=True)
        elif args.file:
            with open(args.file, 'r') as f:
                queries = [line.strip() for line in f if line.strip()]
            print(f"\n🧪 Testing {len(queries)} queries...\n")
            for query in queries:
                result = await test_query(client, base_url, query, stream=False)
                print_results(result, detailed=False)
                await asyncio.sleep(0.5)  # Small delay between queries
        else:
            await interactive_mode(client, base_url)


if __name__ == "__main__":
    asyncio.run(main())

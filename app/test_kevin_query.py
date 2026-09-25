#!/usr/bin/env python3
"""
Quick test script for Kevin's employers query.
Run this from inside the API container: python test_kevin_query.py
"""

import asyncio
import json
import httpx
import os
import time

# When running inside container, use localhost; from host, use the mapped port
BASE_URL = os.getenv("API_URL", "http://localhost:8000")  # Default to container port
QUERY = "List Kevin's historical employers."


async def test_diagnostic():
    """Run diagnostic analysis first."""
    print("=" * 80)
    print("🔍 DIAGNOSTIC ANALYSIS")
    print("=" * 80)
    print(f"Query: {QUERY}\n")
    
    start_time = time.time()
    async with httpx.AsyncClient(timeout=60.0) as client:
        try:
            response = await client.post(
                f"{BASE_URL}/diagnostic",
                json={"query": QUERY},
                timeout=30.0
            )
            response.raise_for_status()
            data = response.json()
            
            print("Configuration:")
            config = data.get("configuration", {})
            print(f"  MIN_SIMILARITY_SCORE: {config.get('min_similarity_score')}")
            print(f"  MAX_CONTEXT_CHUNKS: {config.get('max_context_chunks')}")
            print(f"  Embed Model: {config.get('embed_model')}")
            print()
            
            stats = data.get("retrieval_stats", {})
            print("Retrieval Statistics:")
            print(f"  Total chunks retrieved: {stats.get('total_chunks_retrieved', 0)}")
            print(f"  Chunks meeting threshold: {stats.get('chunks_meeting_threshold', 0)}")
            print(f"  Chunks used in context: {stats.get('chunks_used_in_context', 0)}")
            print(f"  Average score: {stats.get('average_score', 0):.4f}")
            print(f"  Max score: {stats.get('max_score', 0):.4f}")
            print(f"  Min score: {stats.get('min_score', 0):.4f}")
            print()
            
            recommendations = data.get("recommendations", [])
            if recommendations:
                print("Recommendations:")
                for rec in recommendations:
                    print(f"  {rec}")
                print()
            
            print("Top Retrieved Chunks:")
            chunks = data.get("all_chunks", [])[:5]
            for chunk in chunks:
                meets = "✓" if chunk.get("meets_threshold") else "✗"
                print(f"  {meets} Rank {chunk.get('rank')}: Score {chunk.get('score'):.4f} | {chunk.get('title', 'Unknown')}")
                preview = chunk.get("text_preview", "")
                if preview:
                    print(f"      Preview: {preview[:150]}...")
            
            elapsed = time.time() - start_time
            print(f"\n⏱️  Diagnostic took {elapsed:.2f} seconds")
            
            return data
            
        except Exception as e:
            elapsed = time.time() - start_time
            print(f"❌ Diagnostic failed: {e}")
            print(f"⏱️  Failed after {elapsed:.2f} seconds")
            return None


async def test_chat():
    """Test the actual chat endpoint using streaming to avoid timeouts."""
    print("\n" + "=" * 80)
    print("💬 CHAT RESPONSE")
    print("=" * 80)
    print(f"Query: {QUERY}\n")
    
    start_time = time.time()
    async with httpx.AsyncClient(timeout=120.0) as client:
        try:
            # Use streaming to avoid gateway timeouts
            response = await client.post(
                f"{BASE_URL}/chat",
                json={"query": QUERY},
                params={"stream": True},
                timeout=120.0
            )
            response.raise_for_status()
            
            # Handle streaming response
            content_type = response.headers.get("content-type", "")
            answer_parts = []
            
            if "text/event-stream" in content_type:
                print("📡 Receiving streaming response...")
                line_count = 0
                async for line in response.aiter_lines():
                    if not line.strip():
                        continue
                    line_count += 1
                    if line.startswith("data: "):
                        try:
                            data = json.loads(line[6:])  # Remove "data: " prefix
                            if "token" in data:
                                answer_parts.append(data["token"])
                            # Sources removed from output
                            elif data.get("done"):
                                print(f"✅ Stream complete after {line_count} lines")
                                break
                            elif "error" in data:
                                print(f"❌ Error in stream: {data.get('error')}")
                        except json.JSONDecodeError as e:
                            print(f"⚠️  Failed to parse line: {line[:100]}... Error: {e}")
                            continue
                answer = "".join(answer_parts)
                print(f"📝 Received {len(answer_parts)} tokens, {len(answer)} total characters")
                if not answer:
                    print("⚠️  WARNING: No answer tokens received in stream!")
            else:
                # Non-streaming fallback
                print("📡 Receiving non-streaming response...")
                data = response.json()
                answer = data.get("answer", "")
            
            print("Answer:")
            print("-" * 80)
            print(answer)
            print("-" * 80)
            
            elapsed = time.time() - start_time
            print(f"\n⏱️  Chat response took {elapsed:.2f} seconds")
            
            return data
            
        except Exception as e:
            elapsed = time.time() - start_time
            print(f"❌ Chat request failed: {e}")
            print(f"⏱️  Failed after {elapsed:.2f} seconds")
            import traceback
            traceback.print_exc()
            return None


async def main():
    print("\n🧪 Testing Query: 'List Kevin's historical employers.'\n")
    
    total_start_time = time.time()
    
    # First run diagnostics
    diag_data = await test_diagnostic()
    
    # Then test the actual chat
    chat_data = await test_chat()
    
    total_elapsed = time.time() - total_start_time
    
    print("\n" + "=" * 80)
    print("✅ Testing complete!")
    print("=" * 80)
    print(f"⏱️  Total time: {total_elapsed:.2f} seconds ({total_elapsed/60:.2f} minutes)")
    
    # Summary
    if diag_data:
        stats = diag_data.get("retrieval_stats", {})
        if stats.get("chunks_meeting_threshold", 0) == 0:
            print("\n⚠️  ISSUE: No chunks meet the similarity threshold!")
            print(f"   Current threshold: {diag_data.get('configuration', {}).get('min_similarity_score')}")
            print(f"   Max score found: {stats.get('max_score', 0):.4f}")
            print("   Recommendation: Lower MIN_SIMILARITY_SCORE or check document ingestion")


if __name__ == "__main__":
    asyncio.run(main())

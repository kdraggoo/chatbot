#!/usr/bin/env python3
"""
ingest.py — Local RAG ingestion for Qdrant using Ollama embeddings

- Reads files from a directory (default: /data)
- Extracts text from: .txt .md .rtf .doc .docx .odt .ott
- Chunks text, embeds with Ollama (e.g., bge-m3), and upserts to Qdrant
- Automatically removes documents for files found in /undata directory

Requirements inside the api container (see requirements.txt):
- requests (for Ollama HTTP API)
- qdrant-client
- optional: striprtf (for .rtf)
- optional: pypandoc OR a system pandoc binary (for .doc .odt .ott as a robust fallback)
- optional: python-docx (for .docx if pandoc unavailable)
- optional: antiword or catdoc system binaries (for .doc files as fallback if pandoc fails)

Environment variables (with sensible defaults):
- QDRANT_URL (default: http://qdrant:6333)
- QDRANT_COLLECTION (default: docs)
- OLLAMA_URL (default: http://ollama:11434)
- EMBED_MODEL (default: bge-m3)

Usage:
  python -m rag.ingest /data --collection docs
  python -m rag.ingest /data --undata-dir /undata  # Remove files in /undata before ingesting

To un-ingest a file: Move it from /data to /undata, then run the ingest script.
"""

import argparse
import hashlib
import os
import re
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

import requests
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams, PointStruct, FilterSelector, Filter, FieldCondition, MatchValue

# Optional dependencies
try:
    from striprtf.striprtf import rtf_to_text  # for .rtf
except Exception:
    rtf_to_text = None

try:
    import pypandoc  # uses system pandoc under the hood
except Exception:
    pypandoc = None

try:
    import docx  # python-docx, for .docx if pandoc unavailable
except Exception:
    docx = None

try:
    import fitz  # pymupdf for PDF extraction
except Exception:
    fitz = None


# ----------------------------
# Text extraction
# ----------------------------

SUPPORTED_EXTS = {".txt", ".md", ".rtf", ".doc", ".docx", ".odt", ".ott", ".pdf"}


def read_text_file(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="ignore")


def read_rtf(path: Path) -> str:
    if rtf_to_text is None:
        raise RuntimeError("striprtf not available. Install 'striprtf' to parse .rtf")
    raw = path.read_text(encoding="utf-8", errors="ignore")
    return rtf_to_text(raw)


def read_docx(path: Path) -> str:
    if pypandoc is not None:
        return pypandoc.convert_file(str(path), "plain")
    if docx is None:
        raise RuntimeError("Neither pandoc nor python-docx available to parse .docx")
    d = docx.Document(str(path))
    return "\n".join(p.text for p in d.paragraphs)


def read_via_pandoc(path: Path) -> str:
    if pypandoc is None:
        raise RuntimeError("pypandoc/pandoc not available to convert this format")
    return pypandoc.convert_file(str(path), "plain")


def read_doc(path: Path) -> str:
    """
    Read .doc files using multiple fallback methods:
    1. Try antiword first (most reliable for .doc files)
    2. Try pypandoc (if available)
    3. Try catdoc (alternative .doc reader)
    """
    # Method 1: Try antiword first (most reliable for .doc files, lightweight)
    try:
        result = subprocess.run(
            ["antiword", str(path)],
            capture_output=True,
            text=True,
            timeout=30,
            check=True
        )
        output = result.stdout.strip()
        if output:  # Only return if we got meaningful output
            return output
    except FileNotFoundError:
        # antiword not installed, try next method
        pass
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        # antiword failed, try next method
        pass
    
    # Method 2: Try pypandoc (if available)
    if pypandoc is not None:
        try:
            # Ensure pypandoc can find system pandoc
            output = pypandoc.convert_file(str(path), "plain")
            if output and output.strip():
                return output.strip()
        except Exception as e:
            # If pypandoc fails, try next method
            pass
    
    # Method 3: Try catdoc (alternative .doc reader)
    try:
        result = subprocess.run(
            ["catdoc", str(path)],
            capture_output=True,
            text=True,
            timeout=30,
            check=True
        )
        output = result.stdout.strip()
        if output:
            return output
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        # catdoc not available or failed
        pass
    
    # All methods failed
    raise RuntimeError(
        "Unable to read .doc file. Install one of: "
        "pypandoc (with pandoc binary), antiword, or catdoc. "
        f"File: {path}"
    )


def read_pdf(path: Path) -> str:
    """
    Read PDF files using pymupdf (fitz).
    Extracts text from all pages.
    """
    if fitz is None:
        raise RuntimeError("pymupdf not available. Install 'pymupdf' to parse .pdf files")

    text_parts = []
    try:
        with fitz.open(str(path)) as doc:
            for page_num, page in enumerate(doc):
                page_text = page.get_text()
                if page_text.strip():
                    text_parts.append(page_text)
    except Exception as e:
        raise RuntimeError(f"Failed to read PDF file: {path}. Error: {e}")

    return "\n\n".join(text_parts)


def extract_text(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in {".txt", ".md"}:
        return read_text_file(path)
    if ext == ".rtf":
        return read_rtf(path)
    if ext == ".docx":
        return read_docx(path)
    if ext == ".doc":
        return read_doc(path)
    if ext == ".pdf":
        return read_pdf(path)
    if ext in {".odt", ".ott"}:
        return read_via_pandoc(path)
    raise ValueError(f"Unsupported extension: {ext}")


# ----------------------------
# Chunking
# ----------------------------

@dataclass
class Chunk:
    doc_id: str
    chunk_id: int
    text: str
    source_path: str
    title: str
    updated_at: str  # ISO8601


def chunk_text(text: str, size: int = 900, overlap: int = 150) -> List[str]:
    """
    Chunk text intelligently by respecting paragraph and sentence boundaries.
    
    Strategy:
    1. Split by double newlines (paragraphs)
    2. Within paragraphs, try to split at sentence boundaries
    3. Fall back to character-based chunking if needed
    4. Apply overlap between chunks
    """
    text = text.strip()
    if not text:
        return []
    
    # First, split by paragraphs (double newlines or single newline after a period)
    paragraphs = re.split(r'\n\s*\n', text)
    
    chunks: List[str] = []
    current_chunk = ""
    
    for para in paragraphs:
        para = para.strip()
        if not para:
            continue
        
        # If adding this paragraph would exceed size, finish current chunk
        if current_chunk and len(current_chunk) + len(para) + 2 > size:
            # Try to split current_chunk at sentence boundary
            chunk_parts = _split_at_sentences(current_chunk, size)
            if len(chunk_parts) > 1:
                # Add all but last to chunks
                chunks.extend(chunk_parts[:-1])
                current_chunk = chunk_parts[-1]
            else:
                # No good sentence boundary, just add it
                chunks.append(current_chunk)
                current_chunk = ""
        
        # Add paragraph to current chunk
        if current_chunk:
            current_chunk += "\n\n" + para
        else:
            current_chunk = para
        
        # If current chunk is already too large, split it
        while len(current_chunk) > size:
            chunk_parts = _split_at_sentences(current_chunk, size)
            if len(chunk_parts) > 1:
                chunks.append(chunk_parts[0])
                current_chunk = chunk_parts[-1]
            else:
                # Force split at character boundary if no sentences
                chunks.append(current_chunk[:size])
                current_chunk = current_chunk[size - overlap:]
    
    # Add remaining chunk
    if current_chunk:
        chunks.append(current_chunk)
    
    # Apply overlap between chunks
    if overlap > 0 and len(chunks) > 1:
        overlapped_chunks = [chunks[0]]
        for i in range(1, len(chunks)):
            prev_chunk = chunks[i - 1]
            curr_chunk = chunks[i]
            
            # Take last 'overlap' chars from previous chunk
            overlap_text = prev_chunk[-overlap:] if len(prev_chunk) >= overlap else prev_chunk
            
            # Try to start overlap at sentence boundary
            overlap_start = _find_sentence_start(overlap_text)
            overlap_text = overlap_text[overlap_start:]
            
            overlapped_chunks.append(overlap_text + " " + curr_chunk)
        chunks = overlapped_chunks
    
    # Filter out empty chunks and strip
    chunks = [chunk.strip() for chunk in chunks if chunk.strip()]
    return chunks


def _split_at_sentences(text: str, max_size: int) -> List[str]:
    """Split text at sentence boundaries, trying to keep chunks under max_size."""
    # Pattern to match sentence endings (period, exclamation, question mark followed by space)
    sentence_pattern = r'([.!?]+\s+)'
    
    sentences = re.split(sentence_pattern, text)
    # Recombine sentences with their punctuation
    combined_sentences = []
    for i in range(0, len(sentences) - 1, 2):
        if i + 1 < len(sentences):
            combined_sentences.append(sentences[i] + sentences[i + 1])
        else:
            combined_sentences.append(sentences[i])
    if len(sentences) % 2 == 1:
        combined_sentences.append(sentences[-1])
    
    chunks = []
    current = ""
    
    for sent in combined_sentences:
        if len(current) + len(sent) <= max_size:
            current += sent
        else:
            if current:
                chunks.append(current)
            # If single sentence is too long, split it
            if len(sent) > max_size:
                # Split long sentence at word boundaries
                words = sent.split()
                temp = ""
                for word in words:
                    if len(temp) + len(word) + 1 <= max_size:
                        temp += " " + word if temp else word
                    else:
                        if temp:
                            chunks.append(temp)
                        temp = word
                current = temp
            else:
                current = sent
    
    if current:
        chunks.append(current)
    
    return chunks if chunks else [text]


def _find_sentence_start(text: str) -> int:
    """Find the start of the last sentence in the text."""
    # Look for sentence ending patterns from the end
    match = re.search(r'[.!?]+\s+', text[::-1])
    if match:
        return len(text) - match.end()
    # Look for word boundaries as fallback
    match = re.search(r'\s+', text[::-1])
    if match:
        return len(text) - match.end()
    return 0


# ----------------------------
# Embeddings via Ollama
# ----------------------------

def embed_texts_ollama(texts: List[str], model: str, ollama_url: str) -> List[List[float]]:
    if not texts:
        return []
    url = f"{ollama_url.rstrip('/')}/api/embeddings"
    embeddings: List[List[float]] = []
    # Ollama supports batch via single prompt? Safer to loop to avoid context limits.
    for t in texts:
        resp = requests.post(url, json={"model": model, "prompt": t})
        resp.raise_for_status()
        data = resp.json()
        vec = data.get("embedding")
        if not isinstance(vec, list):
            raise RuntimeError("Unexpected embedding response from Ollama")
        embeddings.append(vec)
        # small throttle to be gentle
        time.sleep(0.01)
    return embeddings


# ----------------------------
# Qdrant helpers
# ----------------------------

def ensure_collection(client: QdrantClient, name: str, vector_size: int) -> None:
    existing = {c.name for c in client.get_collections().collections}
    if name in existing:
        return
    client.recreate_collection(
        collection_name=name,
        vectors_config=VectorParams(size=vector_size, distance=Distance.COSINE),
    )


def upsert_chunks(
    client: QdrantClient,
    collection: str,
    doc_id: str,
    chunks: List[Chunk],
    vectors: List[List[float]],
) -> None:
    points = []
    for ch, vec in zip(chunks, vectors):
        pid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{doc_id}:{ch.chunk_id}"))
        payload = {
            "doc_id": ch.doc_id,
            "chunk_id": ch.chunk_id,
            "source_path": ch.source_path,
            "title": ch.title,
            "updated_at": ch.updated_at,
            "text": ch.text,
        }
        points.append(PointStruct(id=pid, vector=vec, payload=payload))
    if points:
        client.upsert(collection_name=collection, points=points)


def delete_document(client: QdrantClient, collection: str, doc_id: str) -> int:
    """
    Delete all chunks for a document by doc_id.
    Returns the number of chunks deleted (or -1 if deletion failed).
    """
    try:
        # Create filter to match all chunks with this doc_id
        filter_condition = Filter(
            must=[
                FieldCondition(
                    key="doc_id",
                    match=MatchValue(value=doc_id),
                ),
            ],
        )
        
        # Delete points matching the filter
        client.delete(
            collection_name=collection,
            points_selector=FilterSelector(filter=filter_condition),
        )
        
        # Try to verify deletion by checking if any chunks remain
        # Note: Qdrant doesn't return count, so we'll estimate success
        return 1  # Indicate success
    except Exception as e:
        print(f"ERROR deleting document {doc_id}: {e}", file=sys.stderr)
        return -1


def remove_undata_files(
    client: QdrantClient,
    undata_dir: Path,
    collection: str,
) -> None:
    """
    Remove documents from Qdrant for files found in the undata directory.
    Files in /undata are considered 'un-ingested' and should be removed.
    """
    if not undata_dir.exists():
        return
    
    undata_files = list(iter_files(undata_dir))
    if not undata_files:
        return
    
    print(f"\nChecking {undata_dir} for files to remove...")
    removed_count = 0
    
    for file_path in undata_files:
        try:
            # Compute doc_id as if the file were in /data (original location)
            # This ensures we match the doc_id from when it was originally ingested
            # Most common case: file was in /data, got ingested, then moved to /undata
            file_path_str = str(file_path)
            undata_dir_str = str(undata_dir.resolve())
            if file_path_str.startswith(undata_dir_str):
                # Replace /undata with /data to get the original path
                original_path_str = file_path_str.replace(undata_dir_str, "/data", 1)
                original_path = Path(original_path_str)
            else:
                # File path doesn't contain undata_dir, use as-is
                original_path = file_path
            doc_id = stable_doc_id(original_path)
            
            # Delete all chunks for this document
            result = delete_document(client, collection, doc_id)
            
            if result > 0:
                print(f"Removed {file_path.name} (doc_id: {doc_id[:8]}...)")
                removed_count += 1
            else:
                # If removal failed, the file might have been ingested from a different path
                # Try with the current path as fallback
                doc_id_current = stable_doc_id(file_path)
                if doc_id_current != doc_id:
                    result = delete_document(client, collection, doc_id_current)
                    if result > 0:
                        print(f"Removed {file_path.name} (doc_id: {doc_id_current[:8]}...)")
                        removed_count += 1
                    else:
                        print(f"Failed to remove {file_path.name} (may not exist in collection)")
                else:
                    print(f"Failed to remove {file_path.name} (may not exist in collection)")
        except Exception as e:
            print(f"ERROR processing {file_path} for removal: {e}", file=sys.stderr)
    
    if removed_count > 0:
        print(f"Removed {removed_count} document(s) from collection")
    else:
        print(f"No documents found in {undata_dir} to remove")


# ----------------------------
# Main flow
# ----------------------------

def stable_doc_id(path: Path) -> str:
    # Stable ID from absolute path
    return hashlib.sha1(str(path.resolve()).encode("utf-8")).hexdigest()


def guess_title(path: Path, text: str) -> str:
    # Take filename, or first non-empty line as a fallback hint
    head = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    return path.stem if len(path.stem) >= 3 else (head[:80] or path.name)


def iter_files(root: Path) -> Iterable[Path]:
    for p in root.rglob("*"):
        if p.is_file() and p.suffix.lower() in SUPPORTED_EXTS:
            yield p


def run_ingest(
    data_dir: Path,
    qdrant_url: str,
    collection: str,
    ollama_url: str,
    embed_model: str,
    chunk_size: int,
    chunk_overlap: int,
    undata_dir: Optional[Path] = None,
) -> None:
    client = QdrantClient(url=qdrant_url)

    # Probe embedding size
    probe_vec = embed_texts_ollama(["probe"], model=embed_model, ollama_url=ollama_url)[0]
    ensure_collection(client, collection, vector_size=len(probe_vec))

    # First, remove documents for files in undata directory
    if undata_dir:
        remove_undata_files(client, undata_dir, collection)

    files = list(iter_files(data_dir))
    if not files:
        print(f"No files found under {data_dir}")
        return

    for f in files:
        try:
            text = extract_text(f)
            ch_texts = chunk_text(text, size=chunk_size, overlap=chunk_overlap)
            if not ch_texts:
                print(f"Skip empty after chunking: {f}")
                continue
            vectors = embed_texts_ollama(ch_texts, model=embed_model, ollama_url=ollama_url)
            doc_id = stable_doc_id(f)
            now = datetime.utcnow().isoformat() + "Z"
            chunks = [
                Chunk(
                    doc_id=doc_id,
                    chunk_id=i,
                    text=t,
                    source_path=str(f),
                    title=guess_title(f, text),
                    updated_at=now,
                )
                for i, t in enumerate(ch_texts)
            ]
            upsert_chunks(client, collection, doc_id, chunks, vectors)
            print(f"Ingested {f} -> {len(chunks)} chunks")
        except Exception as e:
            print(f"ERROR processing {f}: {e}", file=sys.stderr)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Ingest documents into Qdrant with Ollama embeddings")
    parser.add_argument("data_dir", nargs="?", default="/data", help="Directory of documents to ingest")
    parser.add_argument("--collection", default=os.getenv("QDRANT_COLLECTION", "docs"))
    parser.add_argument("--qdrant-url", default=os.getenv("QDRANT_URL", "http://qdrant:6333"))
    parser.add_argument("--ollama-url", default=os.getenv("OLLAMA_URL", "http://ollama:11434"))
    parser.add_argument("--embed-model", default=os.getenv("EMBED_MODEL", "bge-m3"))
    parser.add_argument("--chunk-size", type=int, default=1200)  # Increased for better context
    parser.add_argument("--chunk-overlap", type=int, default=200)  # Increased overlap for resumes
    parser.add_argument("--undata-dir", default="/undata", help="Directory containing files to remove from collection (default: /undata)")
    args = parser.parse_args(argv)

    undata_path = Path(args.undata_dir) if args.undata_dir else None

    run_ingest(
        data_dir=Path(args.data_dir),
        qdrant_url=args.qdrant_url,
        collection=args.collection,
        ollama_url=args.ollama_url,
        embed_model=args.embed_model,
        chunk_size=args.chunk_size,
        chunk_overlap=args.chunk_overlap,
        undata_dir=undata_path,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

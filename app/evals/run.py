#!/usr/bin/env python3
"""
Eval harness for the RAG chatbot: scores the golden set in cases.yaml.

It runs inside the API container and imports main.py, so it exercises the same
retrieval, prompt building and Ollama generation code as /chat, with the same
.env settings. It does not go through HTTP, so it is not rate-limited and does
not write to the dashboard's chat log.

Two scores per case:
  retrieval  were the expected facts in the context sent to the model?  (fast)
  answer     did the model's answer contain them (or decline, for refuse cases)?

Usage (from /srv/chatbot):
    docker compose exec api python -m evals.run                      # full run, ~40s/case
    docker compose exec api python -m evals.run --retrieval-only     # seconds, no generation
    docker compose exec api python -m evals.run --tag employment --case college
    docker compose exec -e MIN_SIMILARITY_SCORE=0.3 api python -m evals.run --retrieval-only
    docker compose exec api python -m evals.run --rescore evals/results/<run>-full.json

Override any main.py setting with -e (MIN_SIMILARITY_SCORE, MAX_CONTEXT_CHUNKS,
GEN_MODEL, ...) to try it without touching the live API.

Each run is saved to evals/results/ and compared with the previous run of the
same mode. Exit status is 1 if a case that passed in that run now fails.
Generation shares Ollama with live visitors, so cases run one at a time.
"""

import argparse
import asyncio
import json
import logging
import os
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
import yaml

EVALS_DIR = Path(__file__).resolve().parent
RESULTS_DIR = EVALS_DIR / "results"

# Phrases that mean the bot declined; matched case-insensitively against refuse cases
REFUSAL_MARKERS = [
    "don't have", "do not have", "doesn't have", "does not have",
    "no information", "not mention", "no mention", "not provided", "not specified",
    "not available", "not include", "not contain", "isn't mentioned", "is not mentioned",
    "not found", "no record", "not listed", "unable to", "cannot", "can't",
    "couldn't", "could not", "not able to", "does not provide", "doesn't provide",
    "insufficient", "not enough information", "no relevant",
]


def load_cases(path: Path, tags: list[str], ids: list[str]) -> list[dict]:
    cases = yaml.safe_load(path.read_text())
    seen = set()
    for c in cases:
        if c["id"] in seen:
            sys.exit(f"Duplicate case id: {c['id']}")
        seen.add(c["id"])
        # Normalize facts to lists of alternatives
        c["facts"] = [f if isinstance(f, list) else [f] for f in c.get("facts", [])]
        c["facts"] = [[str(a) for a in alts] for alts in c["facts"]]
        c.setdefault("min_facts", len(c["facts"]))
        c.setdefault("refuse", False)
        c.setdefault("forbid", [])
        c.setdefault("tags", [])
    if tags or ids:
        cases = [c for c in cases if c["id"] in ids or set(tags) & set(c["tags"])]
    return cases


def find_facts(text: str, facts: list[list[str]]) -> tuple[list[str], list[str]]:
    """Return (found, missing), each fact named by its first alternative."""
    low = text.lower()
    found, missing = [], []
    for alts in facts:
        (found if any(a.lower() in low for a in alts) else missing).append(alts[0])
    return found, missing


async def generate(main, prompt: str) -> tuple[str, bool]:
    """Generate an answer the way /chat?stream=true does. Returns (answer, errored)."""
    timeout = min(240.0, max(90.0, len(prompt) / 33))  # same formula as /chat
    parts, errored = [], False
    async for token in main._stream_ollama_response(prompt, timeout=timeout):
        if token.startswith("[ERROR"):
            errored = True
        parts.append(token)
    return "".join(parts).strip(), errored


async def run_case(main, case: dict, retrieval_only: bool) -> dict:
    r = {"id": case["id"], "query": case["query"], "tags": case["tags"]}
    t0 = time.monotonic()
    try:
        prompt, context, sources = await main._prepare_rag_context(case["query"])
    except Exception as e:
        return {**r, "error": f"retrieval: {getattr(e, 'detail', e)}", "retrieval_pass": False, "answer_pass": False}
    r["retrieval_ms"] = int((time.monotonic() - t0) * 1000)
    r["chunks"] = len(sources)
    r["top_score"] = max((s["score"] for s in sources), default=None)
    r["sources"] = sorted({s["title"] for s in sources})

    if case["refuse"]:
        r["retrieval_pass"] = None  # nothing to retrieve
    else:
        found, missing = find_facts(context if sources else "", case["facts"])
        r["context_found"], r["context_missing"] = found, missing
        r["context_recall"] = len(found) / len(case["facts"])
        r["retrieval_pass"] = len(found) >= case["min_facts"]

    if retrieval_only:
        return r

    t1 = time.monotonic()
    answer, errored = await generate(main, prompt)
    r["generation_ms"] = int((time.monotonic() - t1) * 1000)
    r["answer"] = answer
    if errored or not answer:
        r["error"] = "generation: " + (answer[:200] or "empty answer")
        r["answer_pass"] = False
        return r

    score_answer(r, case)
    return r


def score_answer(r: dict, case: dict):
    """Set the answer_* fields of result r from r["answer"]."""
    low = r["answer"].lower()
    r["forbidden_hits"] = [f for f in case["forbid"] if f.lower() in low]
    if case["refuse"]:
        r["refused"] = any(m in low for m in REFUSAL_MARKERS)
        r["answer_pass"] = r["refused"] and not r["forbidden_hits"]
    else:
        found, missing = find_facts(r["answer"], case["facts"])
        r["answer_found"], r["answer_missing"] = found, missing
        r["answer_recall"] = len(found) / len(case["facts"])
        r["answer_pass"] = len(found) >= case["min_facts"] and not r["forbidden_hits"]


def mark(v) -> str:
    return {True: "PASS", False: "FAIL", None: " -- "}[v]


def print_case(r: dict, retrieval_only: bool):
    line = f"{r['id']:<22} retrieval {mark(r.get('retrieval_pass'))}"
    if not retrieval_only:
        line += f"  answer {mark(r.get('answer_pass'))}"
    top = r.get("top_score")
    line += f"  chunks {r.get('chunks', 0):>2}  top {top:.3f}" if top is not None else f"  chunks {r.get('chunks', 0):>2}  top  --  "
    if "generation_ms" in r:
        line += f"  {r['generation_ms'] / 1000:5.1f}s"
    print(line, flush=True)
    details = []
    if r.get("error"):
        details.append(f"error: {r['error']}")
    if r.get("context_missing") and not r.get("retrieval_pass"):
        details.append(f"not in context: {', '.join(r['context_missing'])}")
    if r.get("answer_missing") and r.get("answer_pass") is False:
        details.append(f"not in answer: {', '.join(r['answer_missing'])}")
    if r.get("forbidden_hits"):
        details.append(f"forbidden: {', '.join(r['forbidden_hits'])}")
    if r.get("refused") is False:
        details.append("did not decline")
    if r.get("answer") and r.get("answer_pass") is False:
        details.append("answer: " + " ".join(r["answer"].split())[:240])
    for d in details:
        print(f"    {d}")


def summarize(results: list[dict], retrieval_only: bool) -> dict:
    def rate(key, rows):
        vals = [r[key] for r in rows if r.get(key) is not None]
        return (sum(vals), len(vals))

    s = {"cases": len(results), "retrieval": rate("retrieval_pass", results)}
    recalls = [r["context_recall"] for r in results if "context_recall" in r]
    s["context_recall_mean"] = round(statistics.mean(recalls), 3) if recalls else None
    if not retrieval_only:
        s["answer"] = rate("answer_pass", results)
        recalls = [r["answer_recall"] for r in results if "answer_recall" in r]
        s["answer_recall_mean"] = round(statistics.mean(recalls), 3) if recalls else None
        times = [r["generation_ms"] for r in results if "generation_ms" in r]
        s["generation_ms_median"] = int(statistics.median(times)) if times else None
    tags = sorted({t for r in results for t in r["tags"]})
    key = "retrieval_pass" if retrieval_only else "answer_pass"
    s["by_tag"] = {t: rate(key, [r for r in results if t in r["tags"]]) for t in tags}
    return s


def print_summary(s: dict, retrieval_only: bool):
    def pct(p):
        ok, n = p
        return f"{ok}/{n} ({100 * ok / n:.0f}%)" if n else "n/a"

    print("\n" + "=" * 60)
    print(f"Retrieval: {pct(s['retrieval'])}   mean context recall {s['context_recall_mean']}")
    if not retrieval_only:
        median = s["generation_ms_median"]
        print(f"Answers:   {pct(s['answer'])}   mean answer recall {s['answer_recall_mean']}"
              + (f"   median generation {median / 1000:.1f}s" if median else ""))
    print("By tag (" + ("retrieval" if retrieval_only else "answer") + "): "
          + ", ".join(f"{t} {ok}/{n}" for t, (ok, n) in s["by_tag"].items()))


def previous_run(mode: str, exclude: Path) -> Path | None:
    """Latest earlier run of the same mode over the whole set (--tag/--case runs are skipped)."""
    runs = sorted(p for p in RESULTS_DIR.glob(f"*-{mode}.json") if p != exclude)
    for p in reversed(runs):
        if not json.loads(p.read_text()).get("filtered"):
            return p
    return None


def compare(results: list[dict], baseline_path: Path, retrieval_only: bool) -> int:
    """Print what changed since the baseline run. Returns the number of regressions."""
    base = {r["id"]: r for r in json.loads(baseline_path.read_text())["results"]}
    keys = ["retrieval_pass"] + ([] if retrieval_only else ["answer_pass"])
    regressions, fixes = [], []
    for r in results:
        b = base.get(r["id"])
        if not b:
            continue
        for k in keys:
            if b.get(k) is True and r.get(k) is False:
                regressions.append(f"{r['id']} ({k.split('_')[0]})")
            elif b.get(k) is False and r.get(k) is True:
                fixes.append(f"{r['id']} ({k.split('_')[0]})")
    print(f"\nCompared with {baseline_path.name}:")
    print(f"  regressions: {', '.join(regressions) or 'none'}")
    print(f"  fixed:       {', '.join(fixes) or 'none'}")
    return len(regressions)


def rescore(path: Path, cases_path: Path) -> int:
    """Re-grade the answers saved in a full-run results file with the current cases.yaml.

    Use it after changing facts or REFUSAL_MARKERS, so old runs stay comparable
    without regenerating. Retrieval results are kept as they were (no context is saved).
    """
    data = json.loads(path.read_text())
    if data["mode"] != "full":
        sys.exit("Only full runs have answers to rescore")
    cases = {c["id"]: c for c in load_cases(cases_path, [], [])}
    changed = []
    for r in data["results"]:
        case = cases.get(r["id"])
        if not case or "answer" not in r or r.get("error"):
            continue
        before = r.get("answer_pass")
        score_answer(r, case)
        if r["answer_pass"] != before:
            changed.append(f"{r['id']} {mark(before)}->{mark(r['answer_pass'])}")
    data["summary"] = summarize(data["results"], False)
    data["rescored"] = datetime.now(timezone.utc).isoformat()
    path.write_text(json.dumps(data, indent=2))
    print(f"Rescored {path.name}: {', '.join(changed) or 'no changes'}")
    print_summary(data["summary"], False)
    return 0


async def main_async(args) -> int:
    logging.getLogger("httpx").setLevel(logging.WARNING)
    import main  # reads the env at import, so -e overrides apply

    logging.getLogger("main").setLevel(logging.ERROR)  # scores are printed below
    main.http_client = httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=10.0))

    cases = load_cases(Path(args.cases), args.tag, args.case)
    if not cases:
        sys.exit("No cases match the filters")

    config = {k: getattr(main, k) for k in
              ("GEN_MODEL", "EMBED_MODEL", "QDRANT_COLLECTION", "MIN_SIMILARITY_SCORE", "MAX_CONTEXT_CHUNKS", "RETRIEVAL_STRIP_WORDS", "RETRIEVAL_STRIP_SLOTS")}
    mode = "retrieval" if args.retrieval_only else "full"
    print(f"{len(cases)} cases, mode {mode}, " + ", ".join(f"{k}={v}" for k, v in config.items()) + "\n")

    results = []
    try:
        for case in cases:
            r = await run_case(main, case, args.retrieval_only)
            results.append(r)
            print_case(r, args.retrieval_only)
    finally:
        await main.http_client.aclose()

    summary = summarize(results, args.retrieval_only)
    print_summary(summary, args.retrieval_only)

    RESULTS_DIR.mkdir(exist_ok=True)
    out = RESULTS_DIR / f"{datetime.now(timezone.utc):%Y%m%d-%H%M%S}-{mode}.json"
    filtered = bool(args.tag or args.case)
    out.write_text(json.dumps({
        "started": datetime.now(timezone.utc).isoformat(), "mode": mode, "config": config,
        "filtered": filtered, "summary": summary, "results": results,
    }, indent=2))
    print(f"\nSaved {out.relative_to(EVALS_DIR.parent)}")

    baseline = Path(args.baseline) if args.baseline else previous_run(mode, out)
    return 1 if baseline and compare(results, baseline, args.retrieval_only) else 0


def main():
    p = argparse.ArgumentParser(description="Score the chatbot against the golden set")
    p.add_argument("--retrieval-only", action="store_true", help="skip generation (seconds instead of minutes)")
    p.add_argument("--tag", action="append", default=[], help="only cases with this tag (repeatable)")
    p.add_argument("--case", action="append", default=[], help="only this case id (repeatable)")
    p.add_argument("--cases", default=str(EVALS_DIR / "cases.yaml"), help="golden set file")
    p.add_argument("--baseline", help="results file to compare with (default: previous run of the same mode)")
    p.add_argument("--rescore", metavar="RESULTS", help="re-grade a saved full run with the current cases and markers, then exit")
    args = p.parse_args()
    if args.rescore:
        sys.exit(rescore(Path(args.rescore), Path(args.cases)))
    sys.exit(asyncio.run(main_async(args)))


if __name__ == "__main__":
    main()

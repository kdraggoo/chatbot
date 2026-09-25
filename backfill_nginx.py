#!/usr/bin/env python3
"""Backfill /chatbot/dashboard with chat requests found in the nginx access logs.

Covers the time before the API logged chats itself. nginx never saw the question
text (it's in the POST body) or the response time, so those columns stay empty;
rows are tagged source='nginx' and kept out of "Most asked".

Safe to re-run: it replaces earlier nginx rows and only imports requests older
than the first row the API logged itself.

    python3 /srv/chatbot/backfill_nginx.py [--dry-run]
"""
import argparse
import glob
import gzip
import re
import sqlite3
from contextlib import closing
from datetime import datetime

LOG_GLOB = "/srv/nginx/log/access*"
DB = "/srv/chatbot/stats/chat.db"
PLACEHOLDER = "(question not recorded; from nginx log)"

# nginx "main" format: addr - user [time] "request" status bytes "referer" "agent" "xff"
LINE = re.compile(r'\[([^\]]+)\] "POST /chatbot/api/chat(\?[^ "]*)? HTTP/[^"]*" (\d{3}) ')


def status_for(code: int) -> str:
    if code == 499:  # client closed the connection
        return "aborted"
    return "ok" if 200 <= code < 300 else "error"


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="print what would be imported")
    args = parser.parse_args()

    found, seen = [], set()  # dedupe on the whole line: two requests can share a second
    for path in sorted(glob.glob(LOG_GLOB)):
        opener = gzip.open if path.endswith(".gz") else open
        with opener(path, "rt", errors="replace") as f:
            for line in f:
                m = LINE.search(line)
                if m and line not in seen:
                    seen.add(line)
                    ts = datetime.strptime(m.group(1), "%d/%b/%Y:%H:%M:%S %z").timestamp()
                    found.append((ts, "stream=true" in (m.group(2) or ""), status_for(int(m.group(3)))))

    with closing(sqlite3.connect(DB, timeout=5)) as conn, conn:
        cutoff = conn.execute("SELECT min(ts) FROM chat_log WHERE source = 'chat'").fetchone()[0] or float("inf")
        rows = sorted(r for r in found if r[0] < cutoff)
        print(f"{len(found)} chat requests in nginx logs, {len(rows)} before API logging began")
        if rows:
            print(f"  {datetime.utcfromtimestamp(rows[0][0]):%Y-%m-%d} to {datetime.utcfromtimestamp(rows[-1][0]):%Y-%m-%d}; "
                  + ", ".join(f"{s}: {sum(r[2] == s for r in rows)}" for s in ("ok", "error", "aborted")))
        if args.dry_run:
            conn.rollback()
            return
        conn.execute("DELETE FROM chat_log WHERE source = 'nginx'")
        conn.executemany(
            "INSERT INTO chat_log (ts, query, stream, status, source) VALUES (?, ?, ?, ?, 'nginx')",
            [(ts, PLACEHOLDER, int(stream), status) for ts, stream, status in rows])
        print(f"Imported {len(rows)} rows into {DB}")


if __name__ == "__main__":
    main()

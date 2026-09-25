#!/bin/sh
# Monitoring probe for the chatbot: asks one known question through the full
# RAG pipeline (embed, Qdrant, Ollama) and fails if no answer comes back.
# The admin key tags the request as source='probe', so it shows under
# Monitoring on /chatbot/dashboard and stays out of the visitor usage counts.
# Run hourly by the user systemd unit chatbot-probe.timer.

set -eu
KEY=$(sed -n 's/^ADMIN_API_KEY=//p' /srv/chatbot/.env)

# Rotate through questions the knowledge base should answer
set -- \
    "How long has Kevin been in product management?" \
    "Does Kevin have direct management experience?" \
    "What is Kevin's skill level with AI?" \
    "Where has Kevin worked?"
shift $(( $(date +%H | sed 's/^0//;s/^$/0/') % $# ))
QUERY=$1

BODY=$(printf '{"query":"%s"}' "$QUERY")
OUT=$(curl -sS --max-time 300 -w '\n%{http_code} %{time_total}' \
    -H 'Content-Type: application/json' -H "X-API-Key: $KEY" \
    -d "$BODY" http://127.0.0.1:18000/chat)
STATUS=$(echo "$OUT" | tail -n1)
case "$STATUS" in
    200*) echo "$OUT" | head -n -1 | grep -q '"answer":"..' || { echo "FAIL empty answer ($STATUS): $QUERY"; exit 1; } ;;
    *) echo "FAIL HTTP $STATUS: $QUERY"; echo "$OUT" | head -n -1 | head -c 500; exit 1 ;;
esac
echo "OK $STATUS s: $QUERY"

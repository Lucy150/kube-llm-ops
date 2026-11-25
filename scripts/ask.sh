#!/usr/bin/env bash
# Send one prompt through the gateway and print the answer plus token stats.
set -euo pipefail

PROMPT="${1:?usage: ask.sh \"your prompt\"}"
PORT=18081

kubectl -n llm port-forward svc/llm-gateway "${PORT}:8080" >/dev/null 2>&1 &
PF=$!
trap 'kill $PF 2>/dev/null' EXIT
sleep 2

curl -sS "localhost:${PORT}/v1/generate" \
  -H 'content-type: application/json' \
  -d "$(python3 -c 'import json,sys; print(json.dumps({"prompt": sys.argv[1], "max_tokens": 120}))' "$PROMPT")" \
  | python3 -m json.tool

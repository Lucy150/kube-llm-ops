#!/usr/bin/env bash
# Fire N concurrent requests to see queueing, 429 load-shedding and latency on the dashboard.
set -euo pipefail

N="${1:-6}"
PORT=18082

kubectl -n llm port-forward svc/llm-gateway "${PORT}:8080" >/dev/null 2>&1 &
PF=$!
trap 'kill $PF 2>/dev/null' EXIT
sleep 2

for i in $(seq 1 "$N"); do
  curl -s -o /dev/null -w "request $i -> HTTP %{http_code} in %{time_total}s\n" \
    "localhost:${PORT}/v1/generate" -H 'content-type: application/json' \
    -d "{\"prompt\": \"Write a haiku-length note about Kubernetes request #$i\", \"max_tokens\": 60}" &
done
wait

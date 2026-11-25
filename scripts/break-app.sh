#!/usr/bin/env bash
# Deploy a deliberately broken workload into the "demo" namespace and watch the AIOps loop react.
set -euo pipefail

SCENARIO="${1:-crashloop}"
FILE="deploy/demo/${SCENARIO}.yaml"
[[ -f "$FILE" ]] || { echo "unknown scenario '$SCENARIO' (crashloop | bad-image | oom)"; exit 1; }

kubectl create namespace demo --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -f "$FILE"

cat <<EOF

Scenario '$SCENARIO' deployed. What happens next:
  1. kube-state-metrics exposes the failure       (~30s)
  2. Prometheus rule fires -> Alertmanager         (~1-2 min)
  3. Alertmanager -> alert-summarizer webhook
  4. summarizer pulls logs + events, asks the LLM  (~10-60s on CPU)

Watch it:
  kubectl -n aiops logs -f deploy/alert-summarizer
  make summaries
Clean up:
  make fix
EOF

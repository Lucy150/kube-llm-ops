CLUSTER   ?= llm-lab
MODEL     ?= qwen2.5:0.5b
KPS_VER   ?= 93.11.0       # pin a chart version, e.g. make up KPS_VER=<version>
APPS      := llm-gateway alert-summarizer

.DEFAULT_GOAL := help
.PHONY: help up down cluster images monitoring llm aiops rules break fix ask grafana prometheus alertmanager summaries test lint

help: ## Show targets
	@grep -E '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

up: cluster images mail monitoring llm aiops rules ## Create everything from scratch (first run: ~10 min)
	@printf "\nReady. Try: make ask | make break SCENARIO=crashloop | make grafana\n"

down: ## Delete the kind cluster
	kind delete cluster --name $(CLUSTER)

cluster: ## Create the kind cluster
	kind get clusters | grep -qx $(CLUSTER) || kind create cluster --config kind/cluster.yaml

images: ## Build app images and load them into kind (no registry needed)
	@for app in $(APPS); do \
		docker build -t $$app:dev apps/$$app && kind load docker-image $$app:dev --name $(CLUSTER); \
	done

mail: ## Deploy Mailpit (local SMTP server + web inbox for alert emails)
	kubectl apply -f deploy/mail/mailpit.yaml
	kubectl -n mail rollout status deploy/mailpit --timeout=3m

monitoring: ## Install kube-prometheus-stack (Prometheus, Alertmanager, Grafana)
	helm repo add prometheus-community https://prometheus-community.github.io/helm-charts >/dev/null 2>&1 || true
	helm repo update prometheus-community >/dev/null
	helm upgrade --install kps prometheus-community/kube-prometheus-stack $(if $(strip $(KPS_VER)),--version $(KPS_VER)) \
		-n monitoring --create-namespace -f deploy/monitoring/kps-values.yaml --wait --timeout 10m

llm: ## Deploy Ollama + gateway (waits for the model to be pulled)
	helm upgrade --install llm charts/llm-serving -n llm --create-namespace \
		--set ollama.model=$(MODEL) --wait --timeout 20m

aiops: ## Deploy the alert summarizer
	helm upgrade --install alert-summarizer charts/alert-summarizer -n aiops --create-namespace --wait

rules: ## Apply fast demo alert rules
	kubectl apply -f deploy/monitoring/demo-rules.yaml

break: ## Break something: make break SCENARIO=crashloop|bad-image|oom
	./scripts/break-app.sh $(or $(SCENARIO),crashloop)

fix: ## Remove all demo breakage
	kubectl delete namespace demo --ignore-not-found

ask: ## Send a test prompt through the gateway
	./scripts/ask.sh "Explain a Kubernetes readiness probe in two sentences."

summaries: ## Show the latest AI incident summaries
	@kubectl -n aiops port-forward svc/alert-summarizer 18080:8080 >/dev/null 2>&1 & pid=$$!; sleep 2; \
		curl -s localhost:18080/summaries | python3 -m json.tool; kill $$pid

grafana: ## Open Grafana on http://localhost:3000 (admin/admin)
	kubectl -n monitoring port-forward svc/kps-grafana 3000:80

mail-ui: ## Open the Mailpit inbox on http://localhost:8025
	kubectl -n mail port-forward svc/mailpit 8025:8025

prometheus: ## Open Prometheus on http://localhost:9090
	kubectl -n monitoring port-forward svc/kps-prometheus 9090:9090

alertmanager: ## Open Alertmanager on http://localhost:9093
	kubectl -n monitoring port-forward svc/kps-alertmanager 9093:9093

test: ## Run unit tests for both apps
	@for app in $(APPS); do (cd apps/$$app && python -m pytest -q) || exit 1; done

lint: ## Lint Python + Helm charts
	ruff check apps && ruff format --check apps
	helm lint charts/llm-serving charts/alert-summarizer

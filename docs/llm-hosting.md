# Hosting the LLM

The model is **self-hosted inside the kind cluster on your machine**. You don't need a cloud account, a GPU or an API key.

| What | Where |
|---|---|
| Model runtime | `ollama` Deployment in namespace `llm`, on the kind worker node (a Docker container on your laptop) |
| Model files | PVC `ollama-models` (5Gi, kind's `standard` local-path storage). Survives pod restarts and is deleted by `make down` |
| Default model | `qwen2.5:0.5b` (~400 MB, runs on CPU) |
| How consumers reach it | Only through `llm-gateway`: `http://llm-gateway.llm.svc.cluster.local:8080/v1/generate` |

## How the model gets there

The Ollama container starts `ollama serve`, waits for it to respond, runs `ollama pull $MODEL` into the PVC, and then keeps serving. Two probes keep traffic away until the model is usable:

- **startupProbe**: `ollama show $MODEL`, allowed up to 15 minutes for the first download.
- **readinessProbe**: the same check, so the Service only routes to a pod that actually has the model.

The gateway's `/readyz` also checks that the model is present, so `helm --wait` only returns once the whole path works.

## Prerequisites

| Tool | Notes |
|---|---|
| Docker Desktop | Use the WSL2 backend on Windows |
| kind, kubectl, Helm 3, make | Install inside WSL (Ubuntu) |
| RAM | 8 GB for Docker is comfortable. Ollama alone is limited to 3Gi |

**Windows: give WSL2 enough memory.** Create or edit `C:\Users\<you>\.wslconfig`:

```ini
[wsl2]
memory=8GB
processors=4
```

Then run `wsl --shutdown` from PowerShell and restart Docker Desktop.

Run everything from WSL, in the repo folder:

```bash
cd /mnt/c/Users/<you>/repos/kube-llm-ops
chmod +x scripts/*.sh        # Windows checkouts drop the execute bit
```

## Step by step

```bash
# 1. Cluster (1 control-plane + 1 worker)
make cluster

# 2. Build the gateway and summarizer images and load them into kind (no registry needed)
make images

# 3. Monitoring FIRST: it installs the ServiceMonitor / PrometheusRule CRDs the LLM chart uses
make monitoring

# 4. The LLM: Ollama + gateway. Waits until the model is pulled (2-10 min the first time)
make llm
```

Watch it in a second terminal:

```bash
kubectl -n llm get pods -w
kubectl -n llm logs -f deploy/ollama     # "pulling model qwen2.5:0.5b" + download progress
```

Test it:

```bash
make ask
```

Expected output (numbers vary by machine):

```json
{
  "model": "qwen2.5:0.5b",
  "response": "A readiness probe tells Kubernetes when a container can accept traffic...",
  "prompt_tokens": 38,
  "completion_tokens": 61,
  "tokens_per_second": 18.4,
  "latency_seconds": 4.21
}
```

Then deploy the rest of the stack:

```bash
make aiops rules
```

`make up` runs all of the above in the right order.

## Calling the gateway yourself

```bash
kubectl -n llm port-forward svc/llm-gateway 8080:8080
curl -s localhost:8080/v1/generate -H 'content-type: application/json' \
  -d '{"prompt": "What is a PodDisruptionBudget?", "max_tokens": 100}'
curl -s localhost:8080/metrics | grep ^llm_
```

## Changing the model

```bash
make llm MODEL=llama3.2:1b
```

| Model | Size | Suggested `ollama.resources.limits.memory` |
|---|---|---|
| `qwen2.5:0.5b` (default) | ~400 MB | 3Gi |
| `llama3.2:1b` | ~1.3 GB | 4Gi |
| `qwen2.5:1.5b` | ~1 GB | 4Gi |
| `llama3.2:3b` | ~2 GB | 6Gi (and give Docker 10 GB+) |

Set the limit in `charts/llm-serving/values.yaml`, or pass it with `--set ollama.resources.limits.memory=4Gi`. Old models stay on the PVC. To clear them, run `kubectl -n llm exec deploy/ollama -- ollama rm <model>`.

## Tuning knobs (`charts/llm-serving/values.yaml`)

| Value | Default | Effect |
|---|---|---|
| `gateway.maxConcurrency` | 2 | Requests allowed to hit the model at once |
| `gateway.queueTimeoutSeconds` | 30 | How long a request waits for a slot before getting a 429 |
| `gateway.requestTimeoutSeconds` | 180 | Upstream timeout (returns 504) |
| `gateway.defaultMaxTokens` | 256 | Completion length when the caller doesn't set it |
| `ollama.keepAlive` | 24h | How long the model stays loaded in memory (avoids cold starts) |
| `ollama.resources.limits.cpu` | 4 | More CPU means more tokens/sec |

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `ollama` pod **Pending** | Not enough memory on the node for the 1Gi request, or the PVC isn't bound | `kubectl -n llm describe pod -l app.kubernetes.io/name=ollama`, raise WSL memory, check `kubectl -n llm get pvc` |
| Pod **Running but 0/1 Ready** for a long time | Model still downloading | `kubectl -n llm logs -f deploy/ollama`. A slow network can take 10+ min |
| `make llm` times out | Download took longer than 20 min | Re-run `make llm`. The partial download on the PVC is reused |
| `pull model manifest: ... dial tcp` in logs | No internet or DNS from inside kind (VPN or corporate proxy) | Disconnect the VPN, or set `HTTPS_PROXY` on the ollama container |
| Pod restarts with **OOMKilled** | Model too big for the memory limit | Raise `ollama.resources.limits.memory` or use a smaller model |
| `llm-gateway` 0/1 Ready | `/readyz` can't see the model yet | Wait for Ollama to become Ready. Check `kubectl -n llm logs deploy/llm-gateway` |
| `make ask` returns **429** | All `maxConcurrency` slots busy (often from a running load test) | Wait, or raise `gateway.maxConcurrency` |
| `make ask` returns **504** | Generation slower than `requestTimeoutSeconds` (CPU starved) | Lower `max_tokens`, give Docker more CPUs, or raise the timeout |
| Very low tokens/sec (< 3) | CPU throttled by the limit, or the laptop is on battery saver | Check the "Ollama pod CPU" panel in Grafana. Raise `limits.cpu` |
| `ImagePullBackOff` on `llm-gateway` | Image not loaded into kind | `make images`. The chart uses `llm-gateway:dev` with `IfNotPresent` |
| Grafana dashboard missing | Sidecar didn't pick up the ConfigMap | Check the `grafana_dashboard: "1"` label and that `make monitoring` ran with `deploy/monitoring/kps-values.yaml` |

## Why not a hosted API?

Running the model yourself is the point of this project. It shows how to operate an LLM as a platform dependency: capacity limits, model-aware health checks, cold starts and serving metrics. It also costs nothing and keeps pod logs on your machine, which matters when the alert summarizer sends them to a model. Because the gateway is the only API consumers use, moving to vLLM or a hosted provider later would mean changing one service.

## Clean up

```bash
make down    # deletes the cluster, including the downloaded model
```

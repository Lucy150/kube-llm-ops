# Design decisions

Short ADR-style notes. Interviewers tend to ask about these.

### 1. Put a gateway in front of Ollama instead of calling it directly
Consumers shouldn't depend on one runtime's API. The gateway is where capacity limits, timeouts, metrics and, later, auth, quotas and model routing live. Moving to vLLM, or adding a hosted fallback, would mean changing one service instead of every consumer.

### 2. Concurrency limit with load shedding, not an unbounded queue
CPU inference handles roughly one request at a time. An unbounded queue turns a traffic spike into minutes of latency for everyone. A bounded wait followed by a 429 gives callers a clear signal they can retry. Ollama's own `OLLAMA_NUM_PARALLEL` is kept at 1, so the gateway is the single place where the limit is set.

### 3. Model-aware readiness
"Process is up" isn't the same as "can serve". Readiness runs `ollama show <model>`, so the Service only sends traffic once the model is on disk. The gateway's `/readyz` checks the same thing upstream, so `helm --wait` doesn't finish until the stack can actually answer.

### 4. Small model (qwen2.5:0.5b) by default
The project is about operating an LLM, not about how good the model is. A ~400 MB model runs on any laptop and in CI. It can be swapped with `make llm MODEL=llama3.2:1b`.

### 5. The summarizer acknowledges immediately and works in the background
Alertmanager webhooks have timeouts, and an LLM call on CPU can take a minute. The handler returns right away and summaries run one at a time, because the model is the bottleneck anyway.

### 6. Single replica plus in-memory dedupe for the summarizer
Alertmanager re-sends grouped alerts, so dedupe is needed. In-memory is enough for one replica. Running more replicas would need a shared store (Redis) or consistent hashing on the fingerprint. This is a known limitation, called out on purpose.

### 7. Degrade gracefully when the LLM is down
An AIOps tool that stays quiet while the platform is broken is worse than no tool. If the gateway call fails, the alert still goes out, with the raw warning events in place of a summary.

### 8. Fast demo alert rules
The built-in `KubePodCrashLooping` waits 15 minutes. The demo rules fire within 1–2 minutes so the whole loop can be shown live in an interview.

## Next steps (not built yet)
- Streaming responses plus a time-to-first-token metric
- HPA on in-flight requests through prometheus-adapter or KEDA
- A second model with routing by request `model` field
- ArgoCD app-of-apps to deploy the stack with GitOps
- Feedback buttons on summaries to measure usefulness

from prometheus_client import Counter, Gauge, Histogram

# CPU inference is slow: buckets go up to 2 minutes.
LATENCY_BUCKETS = (0.25, 0.5, 1, 2, 5, 10, 20, 30, 60, 120)
TPS_BUCKETS = (1, 2, 5, 10, 15, 20, 30, 50, 75, 100, 150)

REQUESTS = Counter("llm_requests_total", "Generate requests by outcome", ["model", "status"])
LATENCY = Histogram("llm_request_duration_seconds", "End-to-end generate latency", ["model"], buckets=LATENCY_BUCKETS)
TOKENS = Counter("llm_tokens_total", "Tokens processed", ["model", "type"])
TOKENS_PER_SECOND = Histogram(
    "llm_tokens_per_second", "Completion tokens per second (decode speed)", ["model"], buckets=TPS_BUCKETS
)
MODEL_LOAD = Histogram(
    "llm_model_load_seconds",
    "Time Ollama spent loading the model (cold starts)",
    ["model"],
    buckets=(0.01, 0.1, 0.5, 1, 2, 5, 10, 30, 60),
)
INFLIGHT = Gauge("llm_inflight_requests", "Requests currently being generated", ["model"])
REJECTED = Counter("llm_rejected_requests_total", "Requests shed with 429 due to concurrency limit", ["model"])

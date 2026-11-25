"""LLM gateway: a thin, observable platform layer in front of Ollama.

Responsibilities (the "platform concerns"):
  * one stable API for every internal consumer, whatever model runs behind it
  * concurrency limiting with back-pressure (429 instead of a melting node)
  * timeouts
  * Prometheus metrics: latency, tokens, tokens/sec, in-flight, rejections
  * readiness that is true only when the model is actually pulled
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, Field

from . import metrics

log = logging.getLogger("llm-gateway")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s %(message)s")


class Settings:
    def __init__(self) -> None:
        self.ollama_url = os.getenv("OLLAMA_URL", "http://ollama:11434").rstrip("/")
        self.model = os.getenv("MODEL", "qwen2.5:0.5b")
        self.max_concurrency = int(os.getenv("MAX_CONCURRENCY", "2"))
        self.queue_timeout = float(os.getenv("QUEUE_TIMEOUT_SECONDS", "30"))
        self.request_timeout = float(os.getenv("REQUEST_TIMEOUT_SECONDS", "180"))
        self.default_max_tokens = int(os.getenv("DEFAULT_MAX_TOKENS", "256"))


class GenerateRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=20_000)
    system: str | None = Field(default=None, max_length=4_000)
    max_tokens: int | None = Field(default=None, ge=1, le=2048)
    temperature: float = Field(default=0.2, ge=0.0, le=2.0)


class GenerateResponse(BaseModel):
    model: str
    response: str
    prompt_tokens: int
    completion_tokens: int
    tokens_per_second: float
    latency_seconds: float


def create_app(settings: Settings | None = None, transport: httpx.AsyncBaseTransport | None = None) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.client = httpx.AsyncClient(
            base_url=settings.ollama_url,
            timeout=httpx.Timeout(settings.request_timeout, connect=5.0),
            transport=transport,
        )
        app.state.semaphore = asyncio.Semaphore(settings.max_concurrency)
        log.info(
            "gateway started: model=%s ollama=%s max_concurrency=%d",
            settings.model,
            settings.ollama_url,
            settings.max_concurrency,
        )
        yield
        await app.state.client.aclose()

    app = FastAPI(title="llm-gateway", version="0.1.0", lifespan=lifespan)

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz() -> dict:
        """Ready only if Ollama is reachable AND the configured model is present."""
        try:
            r = await app.state.client.get("/api/tags", timeout=3.0)
            r.raise_for_status()
        except httpx.HTTPError as exc:
            raise HTTPException(503, f"ollama unreachable: {exc}") from exc
        names = {m.get("name") for m in r.json().get("models", [])}
        if settings.model not in names:
            raise HTTPException(503, f"model {settings.model} not loaded yet")
        return {"status": "ready", "model": settings.model}

    @app.get("/metrics")
    async def prom_metrics() -> Response:
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    @app.post("/v1/generate", response_model=GenerateResponse)
    async def generate(req: GenerateRequest) -> GenerateResponse:
        model = settings.model
        sem: asyncio.Semaphore = app.state.semaphore

        # Back-pressure: wait a bounded time for a slot, then shed load.
        try:
            await asyncio.wait_for(sem.acquire(), timeout=settings.queue_timeout)
        except TimeoutError:
            metrics.REJECTED.labels(model=model).inc()
            metrics.REQUESTS.labels(model=model, status="rejected").inc()
            raise HTTPException(429, "gateway at capacity, retry later") from None

        metrics.INFLIGHT.labels(model=model).inc()
        start = time.perf_counter()
        try:
            payload = {
                "model": model,
                "prompt": req.prompt,
                "stream": False,
                "options": {
                    "temperature": req.temperature,
                    "num_predict": req.max_tokens or settings.default_max_tokens,
                },
            }
            if req.system:
                payload["system"] = req.system
            try:
                r = await app.state.client.post("/api/generate", json=payload)
                r.raise_for_status()
            except httpx.TimeoutException as exc:
                metrics.REQUESTS.labels(model=model, status="timeout").inc()
                raise HTTPException(504, "model timed out") from exc
            except httpx.HTTPError as exc:
                metrics.REQUESTS.labels(model=model, status="error").inc()
                raise HTTPException(502, f"upstream error: {exc}") from exc

            body = r.json()
            latency = time.perf_counter() - start
            prompt_tokens = int(body.get("prompt_eval_count", 0))
            completion_tokens = int(body.get("eval_count", 0))
            eval_ns = int(body.get("eval_duration", 0))
            tps = completion_tokens / (eval_ns / 1e9) if eval_ns > 0 else 0.0
            load_s = int(body.get("load_duration", 0)) / 1e9

            metrics.REQUESTS.labels(model=model, status="ok").inc()
            metrics.LATENCY.labels(model=model).observe(latency)
            metrics.TOKENS.labels(model=model, type="prompt").inc(prompt_tokens)
            metrics.TOKENS.labels(model=model, type="completion").inc(completion_tokens)
            if tps > 0:
                metrics.TOKENS_PER_SECOND.labels(model=model).observe(tps)
            metrics.MODEL_LOAD.labels(model=model).observe(load_s)

            return GenerateResponse(
                model=model,
                response=body.get("response", ""),
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                tokens_per_second=round(tps, 2),
                latency_seconds=round(latency, 3),
            )
        finally:
            metrics.INFLIGHT.labels(model=model).dec()
            sem.release()

    return app


app = create_app()

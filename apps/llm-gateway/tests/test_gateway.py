import asyncio

import httpx
from fastapi.testclient import TestClient

from app.main import Settings, create_app

MODEL = "qwen2.5:0.5b"


def fake_ollama(models=(MODEL,), delay: float = 0.0, fail: bool = False):
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": m} for m in models]})
        if request.url.path == "/api/generate":
            if delay:
                await asyncio.sleep(delay)
            if fail:
                return httpx.Response(500, json={"error": "boom"})
            return httpx.Response(
                200,
                json={
                    "response": "hello",
                    "prompt_eval_count": 12,
                    "eval_count": 40,
                    "eval_duration": 2_000_000_000,  # 2s -> 20 tok/s
                    "load_duration": 100_000_000,
                },
            )
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def client(transport, **overrides) -> TestClient:
    s = Settings()
    s.model = MODEL
    for k, v in overrides.items():
        setattr(s, k, v)
    return TestClient(create_app(s, transport=transport))


def test_generate_returns_token_stats():
    with client(fake_ollama()) as c:
        r = c.post("/v1/generate", json={"prompt": "hi"})
    assert r.status_code == 200
    body = r.json()
    assert body["response"] == "hello"
    assert body["completion_tokens"] == 40
    assert body["tokens_per_second"] == 20.0


def test_metrics_exposed_after_request():
    with client(fake_ollama()) as c:
        c.post("/v1/generate", json={"prompt": "hi"})
        text = c.get("/metrics").text
    assert "llm_request_duration_seconds_bucket" in text
    assert 'llm_tokens_total{model="qwen2.5:0.5b",type="completion"}' in text


def test_readyz_false_until_model_pulled():
    with client(fake_ollama(models=())) as c:
        assert c.get("/readyz").status_code == 503
    with client(fake_ollama()) as c:
        assert c.get("/readyz").status_code == 200


def test_upstream_error_maps_to_502():
    with client(fake_ollama(fail=True)) as c:
        assert c.post("/v1/generate", json={"prompt": "hi"}).status_code == 502


def test_validation_rejects_empty_prompt():
    with client(fake_ollama()) as c:
        assert c.post("/v1/generate", json={"prompt": ""}).status_code == 422


async def test_sheds_load_with_429_when_saturated():
    s = Settings()
    s.model, s.max_concurrency, s.queue_timeout = MODEL, 1, 0.05
    app = create_app(s, transport=fake_ollama(delay=0.3))
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            results = await asyncio.gather(
                c.post("/v1/generate", json={"prompt": "a"}),
                c.post("/v1/generate", json={"prompt": "b"}),
            )
    assert sorted(r.status_code for r in results) == [200, 429]

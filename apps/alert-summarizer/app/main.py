"""Alertmanager webhook receiver that writes an LLM incident summary for each firing alert."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import deque
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import httpx
from fastapi import BackgroundTasks, FastAPI, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

from .context import ContextSource, KubeContextSource
from .notifier import SmtpConfig, notify
from .summarizer import summarize

log = logging.getLogger("alert-summarizer")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s %(message)s")

ALERTS = Counter("summarizer_alerts_received_total", "Alerts received from Alertmanager", ["status"])
SUMMARIES = Counter("summarizer_summaries_total", "Summaries produced", ["result"])
NOTIFY_FAILURES = Counter("summarizer_notify_failures_total", "Summaries that could not be delivered", ["kind"])
DURATION = Histogram(
    "summarizer_summary_duration_seconds", "Evidence + LLM + notify time", buckets=(1, 2, 5, 10, 20, 30, 60, 120, 240)
)


class Settings:
    def __init__(self) -> None:
        self.gateway_url = os.getenv("LLM_GATEWAY_URL", "http://llm-gateway.llm.svc.cluster.local:8080")
        self.notify_kind = os.getenv("NOTIFY_KIND", "log")  # log | slack | discord | email
        self.notify_url = os.getenv("NOTIFY_WEBHOOK_URL") or None
        self.dedupe_seconds = int(os.getenv("DEDUPE_SECONDS", "900"))
        self.llm_timeout = float(os.getenv("LLM_TIMEOUT_SECONDS", "240"))
        self.smtp = SmtpConfig.from_env()


def create_app(
    settings: Settings | None = None,
    context: ContextSource | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    settings = settings or Settings()
    seen: dict[str, float] = {}
    recent: deque[dict] = deque(maxlen=20)
    lock = asyncio.Lock()  # summarize one at a time: the CPU model is the bottleneck anyway

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.ctx = context or KubeContextSource()
        app.state.llm = httpx.AsyncClient(
            base_url=settings.gateway_url, timeout=settings.llm_timeout, transport=transport
        )
        app.state.http = httpx.AsyncClient(transport=transport)
        yield
        await app.state.llm.aclose()
        await app.state.http.aclose()

    app = FastAPI(title="alert-summarizer", version="0.1.0", lifespan=lifespan)

    def is_duplicate(alert: dict) -> bool:
        key = f"{alert.get('fingerprint')}:{alert.get('startsAt')}"
        now = time.time()
        for k, t in list(seen.items()):
            if now - t > settings.dedupe_seconds:
                del seen[k]
        if key in seen:
            return True
        seen[key] = now
        return False

    async def handle(alert: dict) -> None:
        labels = alert.get("labels", {})
        start = time.perf_counter()
        async with lock:
            try:
                ns, pod = labels.get("namespace"), labels.get("pod")
                ctx: ContextSource = app.state.ctx
                logs = await asyncio.to_thread(ctx.pod_logs, ns, pod, labels.get("container")) if ns and pod else ""
                events = await asyncio.to_thread(ctx.events, ns, pod) if ns else []
                try:
                    summary = await summarize(app.state.llm, alert, logs, events)
                    result = "ok"
                except httpx.HTTPError as exc:
                    # Degrade gracefully: still notify, with raw evidence instead of a summary.
                    log.warning("LLM unavailable, sending raw evidence: %s", exc)
                    summary = "LLM unavailable. Latest events:\n" + "\n".join(events[:3] or ["(none)"])
                    result = "fallback"
                try:
                    await notify(
                        app.state.http, settings.notify_kind, settings.notify_url, alert, summary, settings.smtp
                    )
                except Exception as exc:
                    # A broken mail server or webhook must not lose the summary: it is still kept below.
                    log.warning("delivering summary via %s failed: %s", settings.notify_kind, exc)
                    NOTIFY_FAILURES.labels(kind=settings.notify_kind).inc()
                recent.appendleft(
                    {
                        "time": datetime.now(UTC).isoformat(timespec="seconds"),
                        "alert": labels.get("alertname"),
                        "severity": labels.get("severity"),
                        "namespace": ns,
                        "pod": pod,
                        "summary": summary,
                        "result": result,
                        "seconds": round(time.perf_counter() - start, 1),
                    }
                )
                SUMMARIES.labels(result=result).inc()
            except Exception:
                log.exception("failed to handle alert %s", labels.get("alertname"))
                SUMMARIES.labels(result="error").inc()
            finally:
                DURATION.observe(time.perf_counter() - start)

    @app.post("/webhook/alertmanager")
    async def webhook(payload: dict, background: BackgroundTasks) -> dict:
        accepted = 0
        for alert in payload.get("alerts", []):
            status = alert.get("status", "firing")
            ALERTS.labels(status=status).inc()
            if status != "firing" or is_duplicate(alert):
                continue
            background.add_task(handle, alert)
            accepted += 1
        # Respond immediately; Alertmanager must not wait for a slow CPU model.
        return {"accepted": accepted}

    @app.get("/summaries")
    async def summaries() -> list[dict]:
        return list(recent)

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"status": "ok"}

    @app.get("/metrics")
    async def prom_metrics() -> Response:
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    return app


app = create_app()

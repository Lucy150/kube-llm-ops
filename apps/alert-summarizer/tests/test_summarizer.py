import json

import httpx
from fastapi.testclient import TestClient

from app.main import Settings, create_app
from app.summarizer import MAX_CONTEXT_CHARS, build_prompt

ALERT = {
    "status": "firing",
    "fingerprint": "abc123",
    "startsAt": "2026-11-21T10:00:00Z",
    "labels": {
        "alertname": "DemoPodCrashLooping",
        "severity": "warning",
        "namespace": "demo",
        "pod": "checkout-7d9f",
        "container": "app",
    },
    "annotations": {"description": "Pod demo/checkout-7d9f restarted 3 times in 3 minutes"},
}


class FakeContext:
    def __init__(self):
        self.calls = []

    def pod_logs(self, namespace, pod, container):
        self.calls.append(("logs", namespace, pod))
        return "connecting to postgres:5432\nERROR: connection refused"

    def events(self, namespace, pod):
        return ["BackOff (Pod/checkout-7d9f, x5): Back-off restarting failed container"]


def make_transport(sent: list, llm_status: int = 200):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/generate":
            sent.append(("llm", json.loads(request.content)))
            if llm_status != 200:
                return httpx.Response(llm_status)
            return httpx.Response(200, json={"response": "Probable cause: database unreachable"})
        sent.append(("notify", json.loads(request.content)))
        return httpx.Response(200)

    return httpx.MockTransport(handler)


def make_client(sent, llm_status=200, kind="slack"):
    s = Settings()
    s.gateway_url, s.notify_kind, s.notify_url = "http://gw", kind, "http://hooks.example/x"
    return TestClient(create_app(s, context=FakeContext(), transport=make_transport(sent, llm_status)))


def test_firing_alert_is_summarized_and_notified():
    sent = []
    with make_client(sent) as c:
        r = c.post("/webhook/alertmanager", json={"alerts": [ALERT]})
        assert r.json() == {"accepted": 1}
        summaries = c.get("/summaries").json()
    llm_call = next(body for kind, body in sent if kind == "llm")
    assert "connection refused" in llm_call["prompt"]
    notify_call = next(body for kind, body in sent if kind == "notify")
    assert "database unreachable" in notify_call["text"]
    assert summaries[0]["result"] == "ok"
    # fields the Grafana "AI incident notes" panel reads
    for field in ("time", "alert", "severity", "namespace", "pod", "summary", "seconds"):
        assert field in summaries[0]


def test_duplicate_and_resolved_alerts_are_skipped():
    sent = []
    resolved = {**ALERT, "status": "resolved", "fingerprint": "zzz"}
    with make_client(sent) as c:
        assert c.post("/webhook/alertmanager", json={"alerts": [ALERT]}).json()["accepted"] == 1
        assert c.post("/webhook/alertmanager", json={"alerts": [ALERT, resolved]}).json()["accepted"] == 0


def test_falls_back_to_raw_evidence_when_llm_down():
    sent = []
    with make_client(sent, llm_status=503) as c:
        c.post("/webhook/alertmanager", json={"alerts": [ALERT]})
        summaries = c.get("/summaries").json()
    assert summaries[0]["result"] == "fallback"
    assert "Back-off restarting" in summaries[0]["summary"]


def test_discord_payload_shape():
    sent = []
    with make_client(sent, kind="discord") as c:
        c.post("/webhook/alertmanager", json={"alerts": [ALERT]})
    notify_call = next(body for kind, body in sent if kind == "notify")
    assert "content" in notify_call


def test_prompt_is_truncated_keeping_newest_logs():
    logs = "old line\n" * 2000 + "NEWEST ERROR"
    prompt = build_prompt(ALERT, logs, [])
    assert len(prompt) <= MAX_CONTEXT_CHARS
    assert prompt.endswith("NEWEST ERROR")


class FakeSMTP:
    sent: list = []
    fail = False

    def __init__(self, host, port, timeout=None):
        self.host, self.port = host, port

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self):
        pass

    def login(self, user, password):
        pass

    def send_message(self, msg):
        if FakeSMTP.fail:
            raise OSError("connection refused")
        FakeSMTP.sent.append(msg)


def make_email_client(monkeypatch, fail=False):
    import app.notifier as notifier
    from app.notifier import SmtpConfig

    FakeSMTP.sent, FakeSMTP.fail = [], fail
    monkeypatch.setattr(notifier.smtplib, "SMTP", FakeSMTP)
    s = Settings()
    s.gateway_url, s.notify_kind = "http://gw", "email"
    s.smtp = SmtpConfig(host="mail.test", port=1025, recipients=["oncall@example.com"])
    return TestClient(create_app(s, context=FakeContext(), transport=make_transport([])))


def test_email_contains_ai_summary_and_alert_details(monkeypatch):
    with make_email_client(monkeypatch) as c:
        c.post("/webhook/alertmanager", json={"alerts": [ALERT]})
    assert len(FakeSMTP.sent) == 1
    msg = FakeSMTP.sent[0]
    assert msg["Subject"] == "AI summary: [WARNING] DemoPodCrashLooping on demo/checkout-7d9f"
    assert msg["To"] == "oncall@example.com"
    body = msg.get_content()
    assert "database unreachable" in body
    assert "Pod:         checkout-7d9f" in body


def test_email_failure_keeps_summary(monkeypatch):
    with make_email_client(monkeypatch, fail=True) as c:
        c.post("/webhook/alertmanager", json={"alerts": [ALERT]})
        summaries = c.get("/summaries").json()
    assert FakeSMTP.sent == []
    assert summaries[0]["result"] == "ok"
    assert "database unreachable" in summaries[0]["summary"]

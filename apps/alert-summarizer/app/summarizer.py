"""Turns one Alertmanager alert + cluster evidence into a short incident summary."""

from __future__ import annotations

import httpx

SYSTEM_PROMPT = (
    "You are an SRE assistant. Given a Kubernetes alert, recent container logs and warning events, "
    "write a concise incident note for an on-call engineer. Use exactly three lines:\n"
    "Probable cause: <one sentence>\n"
    "Evidence: <the specific log line or event that supports it>\n"
    "Next step: <one concrete kubectl command or action>\n"
    "Do not invent facts that are not in the input. If the evidence is unclear, say so."
)

MAX_CONTEXT_CHARS = 4000  # small CPU models: keep prompts short


def build_prompt(alert: dict, logs: str, events: list[str]) -> str:
    labels = alert.get("labels", {})
    ann = alert.get("annotations", {})
    parts = [
        f"Alert: {labels.get('alertname', 'unknown')} (severity={labels.get('severity', 'n/a')})",
        f"Namespace: {labels.get('namespace', 'n/a')}  Pod: {labels.get('pod', 'n/a')}  "
        f"Container: {labels.get('container', 'n/a')}",
        f"Description: {ann.get('description') or ann.get('summary') or 'n/a'}",
        "",
        "Warning events (newest first):",
        "\n".join(events) if events else "(none)",
        "",
        "Recent logs (tail):",
        logs.strip() if logs.strip() else "(no logs available)",
    ]
    prompt = "\n".join(parts)
    if len(prompt) > MAX_CONTEXT_CHARS:
        prompt = prompt[-MAX_CONTEXT_CHARS:]  # keep the tail: newest logs matter most
    return prompt


async def summarize(client: httpx.AsyncClient, alert: dict, logs: str, events: list[str]) -> str:
    r = await client.post(
        "/v1/generate",
        json={
            "prompt": build_prompt(alert, logs, events),
            "system": SYSTEM_PROMPT,
            "max_tokens": 160,
            "temperature": 0.1,
        },
    )
    r.raise_for_status()
    return r.json()["response"].strip()

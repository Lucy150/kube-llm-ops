"""Delivers summaries to the log (always), and optionally Slack, Discord or email."""

from __future__ import annotations

import asyncio
import logging
import os
import smtplib
from dataclasses import dataclass, field
from email.message import EmailMessage

import httpx

log = logging.getLogger("alert-summarizer.notifier")


@dataclass
class SmtpConfig:
    host: str = "mailpit.mail.svc.cluster.local"
    port: int = 1025
    sender: str = "alert-summarizer@kube-llm-ops.local"
    recipients: list[str] = field(default_factory=lambda: ["oncall@kube-llm-ops.local"])
    starttls: bool = False
    username: str | None = None
    password: str | None = None
    timeout: float = 15.0

    @classmethod
    def from_env(cls) -> SmtpConfig:
        return cls(
            host=os.getenv("SMTP_HOST", cls.host),
            port=int(os.getenv("SMTP_PORT", str(cls.port))),
            sender=os.getenv("SMTP_FROM", cls.sender),
            recipients=[r.strip() for r in os.getenv("SMTP_TO", "oncall@kube-llm-ops.local").split(",") if r.strip()],
            starttls=os.getenv("SMTP_STARTTLS", "false").lower() == "true",
            username=os.getenv("SMTP_USERNAME") or None,
            password=os.getenv("SMTP_PASSWORD") or None,
        )


def title(alert: dict) -> str:
    labels = alert.get("labels", {})
    head = f"[{labels.get('severity', 'alert').upper()}] {labels.get('alertname', 'Alert')}"
    return f"{head} on {labels.get('namespace', '?')}/{labels.get('pod', '-')}"


def format_message(alert: dict, summary: str) -> str:
    return f"{title(alert)}\n{summary}"


def build_email(cfg: SmtpConfig, alert: dict, summary: str) -> EmailMessage:
    labels = alert.get("labels", {})
    ann = alert.get("annotations", {})
    msg = EmailMessage()
    msg["Subject"] = f"AI summary: {title(alert)}"
    msg["From"] = cfg.sender
    msg["To"] = ", ".join(cfg.recipients)
    msg.set_content(
        f"{summary}\n\n"
        "---- alert ----\n"
        f"Alert:       {labels.get('alertname', 'n/a')}\n"
        f"Severity:    {labels.get('severity', 'n/a')}\n"
        f"Namespace:   {labels.get('namespace', 'n/a')}\n"
        f"Pod:         {labels.get('pod', 'n/a')}\n"
        f"Container:   {labels.get('container', 'n/a')}\n"
        f"Started at:  {alert.get('startsAt', 'n/a')}\n"
        f"Description: {ann.get('description') or ann.get('summary') or 'n/a'}\n\n"
        "Written by kube-llm-ops alert-summarizer from pod logs and Kubernetes events.\n"
    )
    return msg


def send_email(cfg: SmtpConfig, msg: EmailMessage) -> None:
    with smtplib.SMTP(cfg.host, cfg.port, timeout=cfg.timeout) as smtp:
        if cfg.starttls:
            smtp.starttls()
        if cfg.username and cfg.password:
            smtp.login(cfg.username, cfg.password)
        smtp.send_message(msg)


async def notify(
    client: httpx.AsyncClient,
    kind: str,
    url: str | None,
    alert: dict,
    summary: str,
    smtp: SmtpConfig | None = None,
) -> None:
    log.info("SUMMARY\n%s", format_message(alert, summary))  # always logged, whatever the channel
    if kind == "email":
        cfg = smtp or SmtpConfig.from_env()
        await asyncio.to_thread(send_email, cfg, build_email(cfg, alert, summary))
        return
    if kind in ("slack", "discord") and url:
        text = format_message(alert, summary)
        payload = {"content": text[:1900]} if kind == "discord" else {"text": text}
        r = await client.post(url, json=payload, timeout=10.0)
        r.raise_for_status()

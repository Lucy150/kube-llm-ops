"""Collects Kubernetes evidence for an alert: pod logs + warning events.

Kept behind a small interface so tests can swap in a fake without a cluster.
"""

from __future__ import annotations

import logging
from typing import Protocol

log = logging.getLogger("alert-summarizer.context")

MAX_LOG_LINES = 40
MAX_EVENTS = 10


class ContextSource(Protocol):
    def pod_logs(self, namespace: str, pod: str, container: str | None) -> str: ...
    def events(self, namespace: str, pod: str | None) -> list[str]: ...


class KubeContextSource:
    """Reads from the Kubernetes API using the pod's ServiceAccount (read-only RBAC)."""

    def __init__(self) -> None:
        from kubernetes import client, config

        try:
            config.load_incluster_config()
        except config.ConfigException:
            config.load_kube_config()  # local development
        self.core = client.CoreV1Api()

    def pod_logs(self, namespace: str, pod: str, container: str | None) -> str:
        # For crash loops the interesting output is in the *previous* container.
        for previous in (True, False):
            try:
                kwargs = {"tail_lines": MAX_LOG_LINES, "previous": previous}
                if container:
                    kwargs["container"] = container
                text = self.core.read_namespaced_pod_log(pod, namespace, **kwargs)
                if text and text.strip():
                    return text
            except Exception as exc:  # noqa: BLE001 - API errors are expected (no previous container, etc.)
                log.debug("logs previous=%s failed for %s/%s: %s", previous, namespace, pod, exc)
        return ""

    def events(self, namespace: str, pod: str | None) -> list[str]:
        selector = "type=Warning"
        if pod:
            selector += f",involvedObject.name={pod}"
        try:
            items = self.core.list_namespaced_event(namespace, field_selector=selector).items
        except Exception as exc:  # noqa: BLE001
            log.warning("listing events failed for %s: %s", namespace, exc)
            return []
        items.sort(key=lambda e: e.last_timestamp or e.event_time or e.metadata.creation_timestamp, reverse=True)
        return [
            f"{e.reason} ({e.involved_object.kind}/{e.involved_object.name}, x{e.count or 1}): {e.message}"
            for e in items[:MAX_EVENTS]
        ]

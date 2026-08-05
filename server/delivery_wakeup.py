from __future__ import annotations

import json
import urllib.error
import urllib.request


class DeliveryWakeClient:
    def __init__(self, url: str, token: str, *, timeout_seconds: float = 0.25):
        self.url = url.strip()
        self.token = token.strip()
        self.timeout_seconds = timeout_seconds

    def notify(self, agent_id: str, reason: str) -> bool:
        if not self.url or not self.token:
            return False
        body = json.dumps(
            {"agent_id": agent_id, "reason": reason},
            separators=(",", ":"),
        ).encode("utf-8")
        request = urllib.request.Request(
            self.url,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(
                request,
                timeout=self.timeout_seconds,
            ) as response:
                return 200 <= response.status < 300
        except (OSError, urllib.error.URLError, ValueError):
            return False

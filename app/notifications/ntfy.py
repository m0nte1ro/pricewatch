from urllib.parse import urlsplit

import httpx

from app.schemas.domain import Preferences


class NtfyProvider:
    def __init__(self, preferences: Preferences, *, transport=None):
        self.preferences, self.transport = preferences, transport

    async def send(self, title: str, message: str, urgent: bool = False) -> None:
        prefs = self.preferences
        parsed = urlsplit(prefs.ntfy_url)
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.hostname
            or parsed.username
            or parsed.password
        ):
            raise ValueError("Invalid ntfy server URL")
        headers = {"Authorization": f"Bearer {prefs.ntfy_token}"} if prefs.ntfy_token else {}
        # JSON supports accented product names without non-ASCII header errors.
        async with httpx.AsyncClient(
            timeout=10, transport=self.transport, trust_env=False
        ) as client:
            response = await client.post(
                prefs.ntfy_url.rstrip("/"),
                headers=headers,
                json={
                    "topic": prefs.ntfy_topic,
                    "title": title,
                    "message": message,
                    "priority": 5 if urgent else 3,
                },
            )
            response.raise_for_status()

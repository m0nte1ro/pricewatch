from typing import Protocol


class NotificationProvider(Protocol):
    async def send(
        self, title: str, message: str, urgent: bool = False, click: str | None = None
    ) -> None: ...

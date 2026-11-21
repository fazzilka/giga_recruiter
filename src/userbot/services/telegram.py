"""All Telegram I/O lives here, outside the interview state machine."""

from telethon import TelegramClient

from src.config.config import Config
from src.core.telemetry import Telemetry, observed
from src.services.alerts import AlertService


class TelegramGateway:
    def __init__(
        self,
        client: TelegramClient,
        config: Config,
        telemetry: Telemetry | None = None,
        alerts: AlertService | None = None,
    ):
        self.telemetry = telemetry
        self.client = client
        self.target = config.userbot.target
        self.alerts = alerts
        self.owner_id = None

    @observed("telegram.read")
    async def message(self, message_id: int):
        return await self.client.get_messages(self.target, ids=message_id)

    @observed("telegram.read")
    async def recent(self, limit: int):
        return await self.client.get_messages(self.target, limit=limit)

    @observed("telegram.read")
    async def pending(self, watermark: int):
        return await self.client.get_messages(self.target, limit=30, min_id=watermark)

    @observed("telegram.send")
    async def send(self, text: str):
        return await self.client.send_message(self.target, text, parse_mode=None)

    @observed("telegram.notify")
    async def notify(self, text: str, *, attention: bool = False):
        try:
            await self.client.send_message(
                "me", "Recruiter · " + text[:3900], parse_mode=None
            )
        finally:
            if attention and self.alerts is not None:
                await self.alerts.send(text, owner_id=self.owner_id)

    async def test_alert(self):
        if self.alerts is None:
            return False
        return await self.alerts.send(
            "Тестовое уведомление. Канал алертов подключён.", owner_id=self.owner_id
        )

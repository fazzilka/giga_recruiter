"""All Telegram I/O lives here, outside the interview state machine."""

from telethon import TelegramClient

from src.config.config import Config
from src.core.telemetry import Telemetry, observed


class TelegramGateway:
    def __init__(
        self, client: TelegramClient, config: Config, telemetry: Telemetry | None = None
    ):
        self.telemetry = telemetry
        self.client = client
        self.target = config.userbot.target

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
    async def notify(self, text: str):
        await self.client.send_message(
            "me", "Recruiter · " + text[:3900], parse_mode=None
        )

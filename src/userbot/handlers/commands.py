from telethon import TelegramClient, events

from src.userbot.services.commands import ChatCommandService
from src.userbot.services.telegram import TelegramGateway


class UserbotCommandHandlers:
    def __init__(self, commands: ChatCommandService, telegram: TelegramGateway):
        self.commands, self.telegram = commands, telegram

    async def handle(self, event) -> None:
        if not (event.raw_text or "").startswith("/"):
            return
        try:
            result = await self.commands.execute(event.raw_text)
            await self.telegram.notify(result)
        except Exception as exc:
            telemetry = self.commands.telemetry
            incident = (
                telemetry.error(
                    "command.failed",
                    exc,
                    phase=(self.commands.interview.current or {}).get("phase"),
                )
                if telemetry
                else None
            )
            self.commands.interview.pause()
            detail = (
                str(exc)
                if isinstance(exc, ValueError)
                else f"Ошибка {type(exc).__name__}. Проверь /status."
            )
            await self.telegram.notify(
                detail + (f" Ошибка {incident}. /diagnostics" if incident else "")
            )

    async def attach(self, client: TelegramClient, owner_id: int) -> None:
        client.add_event_handler(
            self.handle, events.NewMessage(outgoing=True, chats=owner_id)
        )

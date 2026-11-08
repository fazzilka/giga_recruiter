from telethon import TelegramClient, events

from src.runtime.interview import InterviewScheduler


class RecruiterHandlers:
    def __init__(self, scheduler: InterviewScheduler):
        self.scheduler = scheduler

    async def handle(self, event) -> None:
        self.scheduler.incoming()

    async def attach(self, client: TelegramClient, target) -> None:
        client.add_event_handler(
            self.handle, events.NewMessage(incoming=True, chats=target)
        )
        client.add_event_handler(
            self.handle, events.MessageEdited(incoming=True, chats=target)
        )

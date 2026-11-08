"""Event-driven scheduler: no Telegram polling every two seconds."""

import asyncio

from telethon.errors import FloodWaitError

from src.core.telemetry import Telemetry, observed
from src.services.interview import InterviewService


class InterviewScheduler:
    def __init__(self, service: InterviewService, telemetry: Telemetry | None = None):
        self.telemetry = telemetry
        self.service = service
        self._version = 0

    def incoming(self) -> None:
        self._version += 1
        self.service.wake()

    async def _debounce(self) -> None:
        while True:
            version = self._version
            await asyncio.sleep(self.service.config.debounce)
            if version == self._version:
                return

    @observed("interview.step")
    async def _tick(self):
        async with self.service.busy:
            await self.service.tick()

    async def step(self) -> None:
        service = self.service
        try:
            await self._tick()
        except FloodWaitError as exc:
            if self.telemetry:
                self.telemetry.error(
                    "interview.failed", exc, phase=(service.current or {}).get("phase")
                )
            service.pause()
            await service.telegram.notify(
                f"Telegram просит подождать {exc.seconds} с. Затем /resume."
            )
        except Exception as exc:
            incident = (
                self.telemetry.error(
                    "interview.failed", exc, phase=(service.current or {}).get("phase")
                )
                if self.telemetry
                else None
            )
            service.pause()
            detail = (
                str(exc)
                if isinstance(exc, ValueError)
                else "Проверь авторизацию и /status."
            )
            await service.telegram.notify(
                f"Остановлено: {type(exc).__name__}. {detail}"
                + (f" Ошибка {incident}. /diagnostics" if incident else "")
            )

    async def run(self) -> None:
        service = self.service
        while True:
            try:
                if service.active:
                    await asyncio.wait_for(
                        service.wakeup.wait(), service.config.reconciliation_interval
                    )
                else:
                    await service.wakeup.wait()
            except TimeoutError:
                pass  # Reconcile after a missed Telegram update or reconnect.
            service.wakeup.clear()
            if not service.active:
                continue
            await self._debounce()
            # Updates received during debounce are included in the next snapshot.
            # Only events arriving during step should schedule another step.
            service.wakeup.clear()
            await self.step()

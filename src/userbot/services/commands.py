"""Slash command parsing and orchestration, independent of Telegram events."""

import logging
import shlex

from src.core.telemetry import Telemetry, observed
from src.services.interview import InterviewService
from src.services.queue import QueueService
from src.userbot.texts.commands import HELP

log = logging.getLogger("recruiter.commands")
KNOWN_COMMANDS = {
    "/help",
    "/start",
    "/metrics",
    "/diagnostics",
    "/pause",
    "/skip",
    "/status",
    "/profile",
    "/mode",
    "/draft",
    "/queue",
    "/history",
    "/run",
    "/resume",
    "/retry",
    "/scan",
    "/next",
    "/add",
    "/addtext",
    "/handled",
    "/recover",
    "/answer",
    "/send",
}


class ChatCommandService:
    def __init__(
        self,
        interview: InterviewService,
        queue: QueueService,
        telemetry: Telemetry | None = None,
    ):
        self.telemetry = telemetry
        self.interview, self.queue = interview, queue

    @observed("command")
    async def execute(self, text: str) -> str:
        # Free-form answers may contain apostrophes and unmatched quotation marks.
        if text.startswith("/answer "):
            command, args = "/answer", [text.partition(" ")[2].strip()]
        else:
            parts = shlex.split(text)
            if not parts:
                return "Пустая команда. /help"
            command, *args = parts
        log.info(
            "Command received: %s", command if command in KNOWN_COMMANDS else "unknown"
        )
        interview = self.interview
        if command == "/metrics" and self.telemetry:
            return self.telemetry.summary()
        if command == "/diagnostics" and self.telemetry:
            path = self.telemetry.export()
            return f"Отчёт сохранён локально: {path}. Его можно приложить в чат для разбора."
        if command in {"/help", "/start"}:
            return HELP
        if command == "/pause":
            interview.pause()
            return "Пауза."
        if command == "/status":
            phase = interview.current["phase"] if interview.current else "idle"
            blocked = bool(
                interview.current and interview.current.get("blocked_reason")
            )
            return f"Цикл: {interview.active}; режим: {interview.mode}; фаза: {phase}; ожидает уточнения: {blocked}"
        if command == "/profile":
            material = await interview.materials.get()
            return f"Материалы доступны: {len(material)} символов. Содержимое в чат не копируется."
        if command == "/mode" and args in (["draft"], ["auto"]):
            interview.mode = args[0]
            return f"Режим: {interview.mode}."
        if command == "/draft":
            return (interview.current or {}).get("pending") or "Нет черновика."
        if command in {"/queue", "/history"}:
            statuses = (
                {"done", "skipped"} if command == "/history" else {"queued", "active"}
            )
            return "\n".join(
                f"{j['status']}: {j['message_id']} {j['button']}"
                for j in interview.store.jobs
                if j["status"] in statuses
            ) or ("История пуста." if command == "/history" else "Очередь пуста.")
        if command in {"/run", "/resume"}:
            await interview.resume()
            return "Цикл включён."
        if interview.busy.locked():
            return "Обрабатываю вопрос. Для остановки используй /pause; затем повтори команду."
        async with interview.busy:
            if command == "/recover" and len(args) == 2 and args[0] == "unfinished":
                await interview.reopen_unfinished(args[1])
                return "Ошибочное завершение отменено, история восстановлена. /resume — ответить на оставшийся вопрос."
            if command == "/next" and not args:
                if interview.current:
                    raise ValueError(
                        "Сначала заверши текущее интервью; перелистывание сейчас заблокировано."
                    )
                return await self.queue.next_page()
            if command == "/skip" and not args:
                await interview.skip_current()
                return "Текущая вакансия пропущена и убрана из очереди. /history — история; /run — следующая вакансия."
            if command == "/retry":
                await interview.retry_last_question()
                return f"Вопрос возвращён на генерацию. Режим: {interview.mode}."
            if command == "/scan":
                return await self.queue.scan()
            if command in {"/add", "/addtext"} and len(args) == 3:
                return await self.queue.add(*args, raw=command == "/addtext")
            if command == "/handled" and len(args) == 1:
                await interview.handled(int(args[0]))
                return "Отмечено. /resume"
            if command == "/recover" and len(args) == 1:
                await interview.recover(args[0])
                if args[0] == "not-started":
                    return "Неначавшийся диалог возвращён в очередь. Открой актуальный список у рекрутера, затем /scan и /run."
                return "Состояние восстановлено. /send для черновика либо /resume."
            if command == "/answer" and args and args[0]:
                if not await interview.answer_manually(args[0]):
                    return "Отправка отменена, черновик сохранён."
                return "Ответ отправлен. /resume — продолжить цикл."
            if command == "/send":
                if not await interview.send_pending():
                    return "Отправка отменена, черновик сохранён."
                interview.active = True
                interview.wake()
                return "Отправлено, цикл продолжен."
        return "Неизвестная команда или аргументы. /help"

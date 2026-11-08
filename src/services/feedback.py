"""Sber's explicit completion and star-rating protocol, outside model inference."""

import asyncio
import logging
import re
from datetime import UTC, datetime

from src.config.config import InterviewConfig
from src.core.db.repositories.workflow import WorkflowRepository
from src.userbot.services.telegram import TelegramGateway
from src.userbot.utils.messages import fingerprint, supported

log = logging.getLogger("recruiter.feedback")


def completed(text: str) -> bool:
    text = " ".join((text or "").lower().split())
    return bool(
        re.match(
            r"^(спасибо за интервью(?:[!.\s]|$)|(?:наше |ваше )?интервью (?:завершено|окончено|закончено)\b)",
            text,
        )
    )


def additional_questions(text: str) -> bool:
    text = " ".join((text or "").casefold().split())
    return bool(
        re.search(
            r"(?:остались|есть) ли у вас (?:(?:какие-либо|какие-нибудь|другие|дополнительные|ещё|еще) )?вопросы(?: ко мне)?\?$",
            text,
        )
    )


def five_star_button(messages):
    """Require a full star scale and an explicit nearby request to rate the recruiter."""
    incoming = sorted((m for m in messages if not m.out), key=lambda m: m.id)
    for index in range(len(incoming) - 1, -1, -1):
        message = incoming[index]
        context = " ".join(
            (m.raw_text or "") for m in incoming[max(0, index - 1) : index + 1]
        ).lower()
        context = " ".join(context.split())
        if not any(
            phrase in context
            for phrase in (
                "оцените мою работу",
                "оценить мою работу",
                "оцените работу рекрутера",
                "оцените работу бота",
                "оцените наше общение",
                "оцените общение с",
                "оцените качество интервью",
            )
        ):
            continue
        scale = {}
        options = 0
        for row in message.buttons or []:
            for button in row:
                label = re.sub(r"[\s\ufe0e\ufe0f]", "", getattr(button, "text", ""))
                if (
                    len(label) == 5
                    and set(label) <= {"★", "☆", "⭐", "🌟"}
                    and supported(button)
                ):
                    filled = sum(char in {"★", "⭐", "🌟"} for char in label)
                    scale[filled] = button
                    options += 1
        if set(scale) == {1, 2, 3, 4, 5} and options == 5:
            return message, scale[5]
    return None


class FeedbackService:
    def __init__(
        self,
        telegram: TelegramGateway,
        store: WorkflowRepository,
        config: InterviewConfig,
    ):
        self.telegram, self.store, self.config = telegram, store, config

    async def finish(self, messages, still_active) -> bool:
        current = self.store.current
        job = next(job for job in self.store.jobs if job["key"] == current["job"])
        if current["phase"] != "finishing":
            current["phase"] = "finishing"
            current["pending"] = None
            current["feedback_deadline"] = (
                datetime.now(UTC).timestamp() + self.config.feedback_wait
            )
            job["status"] = "done"
            job["answers"] = current["answers"]
            job["finished_at"] = datetime.now(UTC).isoformat()
            await self.store.save()
        rating = five_star_button(messages)
        if rating and not current.get("rating_done"):
            message, button = rating
            if not still_active():
                return False
            current["phase"] = "rating"
            current["rating_message_id"] = message.id
            # An uncertain click must never be repeated automatically.
            await self.store.save()
            if not still_active():
                current["phase"] = "finishing"
                await self.store.save()
                return False
            result = await button.click()
            confirmed = False
            if result is None:
                # The bot can edit the card but omit answerCallbackQuery; Telethon
                # then returns None on timeout. Read the same card, never click twice.
                for attempt in range(3):
                    if not still_active():
                        return False
                    updated = await self.telegram.message(message.id)
                    confirmed = (
                        getattr(updated, "id", None) == message.id
                        and not updated.out
                        and not updated.buttons
                        and " ".join(
                            (updated.raw_text or "").casefold().split()
                        ).rstrip("!.")
                        == "спасибо за обратную связь"
                    )
                    if confirmed:
                        break
                    if attempt < 2:
                        await asyncio.sleep(0.5)
            if (result is None and not confirmed) or getattr(
                result, "alert", False
            ) is True:
                raise ValueError(
                    "Результат оценки неизвестен. Проверь чат: /recover rated либо /recover not-rated."
                )
            current["rating_done"] = True
            current["phase"] = "finishing"
            job["rating"] = 5
            log.info("Recruiter rated five stars; message_id=%d", message.id)
            await self.store.save()
        elif (
            not current.get("rating_done")
            and datetime.now(UTC).timestamp() < current["feedback_deadline"]
        ):
            return False  # Let the following rating message arrive before selecting another job.
        for message in messages:
            if not message.out and fingerprint(message) not in current["processed"]:
                current["processed"].append(fingerprint(message))
                current["history"].append(
                    {"role": "recruiter", "text": message.raw_text}
                )
        job["history"] = current["history"]
        self.store.current = None
        await self.store.save()
        await self.telegram.notify(
            "Интервью завершено. Вакансия перенесена в /history."
            + (" Оценка: 5 звёзд." if job.get("rating") == 5 else "")
        )
        return True

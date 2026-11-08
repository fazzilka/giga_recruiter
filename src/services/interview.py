import asyncio
import logging
from datetime import UTC, datetime

from src.config.config import InterviewConfig
from src.core.db.repositories.workflow import WorkflowRepository
from src.core.vacancies import vacancy_identity
from src.integrations.codex import CodexClient
from src.services.cards import CardUnavailable, latest_menu, resolve_card
from src.services.feedback import (
    FeedbackService,
    additional_questions,
    completed,
    five_star_button,
)
from src.services.materials import MaterialService
from src.userbot.services.telegram import TelegramGateway
from src.userbot.utils.messages import find_button, fingerprint, supported

log = logging.getLogger("recruiter.interview")


class RecruiterResponseTimeout(ValueError):
    diagnostic_code = "recruiter_timeout"


class InterviewService:
    def __init__(
        self,
        telegram: TelegramGateway,
        store: WorkflowRepository,
        model: CodexClient,
        materials: MaterialService,
        config: InterviewConfig,
        feedback: FeedbackService | None = None,
    ):
        self.telegram, self.store, self.model = telegram, store, model
        self.materials, self.config = materials, config
        self.feedback = feedback or FeedbackService(telegram, store, config)
        self.active = False
        self.epoch = 0
        self.mode = "draft"
        self.busy = asyncio.Lock()
        self.wakeup = asyncio.Event()
        self._cancel_send = asyncio.Event()
        self._wait_logged_at = 0
        self._menu_wait = None
        self._visited_pages = set()

    def wake(self):
        self.wakeup.set()

    @property
    def current(self):
        return self.store.current

    def pause(self):
        self.active = False
        self.epoch += 1
        self._cancel_send.set()
        log.info("Interview paused")

    async def start_next(self):
        token = self.epoch
        await self.materials.get()
        if self._menu_wait:
            menu = latest_menu(await self.telegram.recent(30))
            previous, requested_at = self._menu_wait
            if menu is None or fingerprint(menu) == previous:
                if (
                    datetime.now(UTC).timestamp() - requested_at
                    >= self.config.response_timeout
                ):
                    raise RecruiterResponseTimeout(
                        "Рекрутер не обновил список вакансий. Проверь его чат и /resume."
                    )
                return
            self._menu_wait = None
        queued = [j for j in self.store.jobs if j["status"] == "queued"]
        if not queued:
            self.pause()
            await self.telegram.notify("Очередь завершена.")
            return
        unavailable = []
        for job in queued:
            if token != self.epoch or not self.active:
                return
            try:
                message, button = await resolve_card(self.telegram, self.store, job)
            except CardUnavailable:
                unavailable.append(job)
                log.info(
                    "Vacancy card unavailable; trying next queued job; message_id=%d",
                    job["message_id"],
                )
            else:
                break
        else:
            if token != self.epoch or not self.active:
                return
            if self.mode == "auto" and await self._advance_menu(token):
                return
            self.pause()
            first = queued[0]
            await self.telegram.notify(
                f"Нет доступных карточек для {len(queued)} вакансий. Очередь сохранена. "
                f"Первая: {first['button']}"
                + (f" ({first['url']})" if first.get("url") else "")
                + ". Открой страницу списка с нужной вакансией у рекрутера, затем /scan и /run."
            )
            return
        if unavailable:
            await self.telegram.notify(
                f"Отложено недоступных карточек: {len(unavailable)}. "
                "Они остаются в очереди; запускаю следующую доступную вакансию."
            )
        latest = await self.telegram.recent(1)
        if token != self.epoch or not self.active:
            return
        current = {
            "job": job["key"],
            # Include edits to the last existing message, but ignore its old content.
            "watermark": max(0, latest[0].id - 1) if latest else 0,
            "history": [],
            "processed": [fingerprint(latest[0])] if latest else [],
            "pending": None,
            "answers": 0,
            "phase": "clicking",
            "last_progress_at": datetime.now(UTC).timestamp(),
        }
        job["status"] = "active"
        self.store.current = current
        # Persist intent before any network effect; uncertain actions are not retried.
        await self.store.save()
        if token != self.epoch or not self.active:
            job["status"] = "queued"
            self.store.current = None
            await self.store.save()
            return
        await button.click()
        self._visited_pages.clear()
        current["phase"] = "interview"
        await self.store.save()
        await self.telegram.notify(f"Начато: {job['button']}. Режим {self.mode}.")

    async def _advance_menu(self, token):
        menu = latest_menu(await self.telegram.recent(30))
        if token != self.epoch or not self.active:
            return True
        if menu is not None:
            page = (
                menu.raw_text or "",
                tuple(b.text for row in menu.buttons for b in row if supported(b)),
            )
            if page in self._visited_pages or len(self._visited_pages) >= 20:
                return False
            self._visited_pages.add(page)
            if not any(
                b.text == "Далее" and supported(b) for row in menu.buttons for b in row
            ):
                return False
            button = find_button(menu, "Далее")
            self._menu_wait = (fingerprint(menu), datetime.now(UTC).timestamp())
            log.info("No queued vacancy on current page; requesting next menu page")
            await button.click()
        else:
            self._menu_wait = (None, datetime.now(UTC).timestamp())
            log.info("Requesting fresh vacancy menu before next interview")
            await self.telegram.send("/change_vacancy")
        self.wake()
        return True

    async def send_pending(self, automatic: bool = False) -> bool:
        token = self.epoch
        current = self.current
        if not current or not current["pending"]:
            raise ValueError("Нет подготовленного ответа.")
        if current["phase"] != "interview":
            raise ValueError(
                "Неопределённый результат предыдущего действия. Сначала проверь чат."
            )
        latest = await self.telegram.pending(current["watermark"])
        if token != self.epoch or (automatic and not self.active):
            return False
        if any(
            not m.out and fingerprint(m) not in current["processed"] for m in latest
        ):
            current["pending"] = None
            await self.store.save()
            raise ValueError(
                "Вопрос обновился: старый черновик удалён. /retry — подготовить новый ответ."
            )
        self._cancel_send.clear()
        if self.config.send_delay:
            log.info(
                "Answer ready; waiting %.1f seconds before sending",
                self.config.send_delay,
            )
            try:
                await asyncio.wait_for(
                    self._cancel_send.wait(), timeout=self.config.send_delay
                )
                log.info("Scheduled send cancelled; draft retained")
                return False
            except TimeoutError:
                pass
            if token != self.epoch or (automatic and not self.active):
                return False
            latest = await self.telegram.pending(current["watermark"])
            if token != self.epoch or (automatic and not self.active):
                return False
            if any(
                not m.out and fingerprint(m) not in current["processed"] for m in latest
            ):
                current["pending"] = None
                await self.store.save()
                raise ValueError(
                    "Вопрос обновился во время задержки: черновик удалён. /retry"
                )
        text = current["pending"]
        current["phase"] = "sending"
        await self.store.save()
        if token != self.epoch or (automatic and not self.active):
            current["phase"] = "interview"
            await self.store.save()
            return False
        await self.telegram.send(text)
        current["history"].append({"role": "candidate", "text": text})
        current["pending"] = None
        current["answers"] += 1
        current["last_progress_at"] = datetime.now(UTC).timestamp()
        current["phase"] = "interview"
        await self.store.save()
        log.info("Answer sent; interview answers=%d", current["answers"])
        return True

    async def tick(self):
        if not self.active:
            return
        if self.current is None:
            await self.start_next()
            return
        current = self.current
        if current["phase"] == "rating":
            raise ValueError(
                "Результат оценки неизвестен. Проверь чат: /recover rated либо /recover not-rated."
            )
        if current["phase"] not in {"interview", "finishing"}:
            self.pause()
            return
        messages = await self.telegram.pending(current["watermark"])
        incoming = [
            m
            for m in reversed(messages)
            if not m.out and fingerprint(m) not in current["processed"]
        ]
        terminal = any(completed(m.raw_text) for m in messages if not m.out)
        terminal = terminal or any(
            entry["role"] == "recruiter" and completed(entry["text"])
            for entry in current["history"]
        )
        if current["phase"] == "finishing" or terminal or five_star_button(messages):
            token = self.epoch
            await self.feedback.finish(
                messages, lambda: self.active and self.epoch == token
            )
            if self.active:
                self.wake()
            return
        if current["pending"]:
            self.pause()
            return
        if current["answers"] >= self.config.max_answers:
            self.pause()
            await self.telegram.notify("Достигнут MAX_ANSWERS. Проверь диалог.")
            return
        if not incoming:
            now = datetime.now(UTC).timestamp()
            if "last_progress_at" not in current:
                current["last_progress_at"] = now
                await self.store.save()
            idle = max(0, now - current["last_progress_at"])
            if idle >= self.config.response_timeout:
                if not current["answers"]:
                    raise RecruiterResponseTimeout(
                        f"Рекрутер не прислал новых сообщений за {int(idle)} с. "
                        "/resume — продолжить ожидание, /status — состояние."
                    )
                if (
                    current.get("silence_notice_progress")
                    != current["last_progress_at"]
                ):
                    current["silence_notice_progress"] = current["last_progress_at"]
                    await self.store.save()
                    log.warning(
                        "Recruiter response delayed; continuing to wait; idle=%.0fs",
                        idle,
                    )
                    await self.telegram.notify(
                        "Рекрутер задерживает ответ. Цикл продолжает ждать и ответит "
                        "на новое сообщение автоматически. Повторно ничего не отправляется."
                    )
            if now - self._wait_logged_at >= 30:
                log.info(
                    "Waiting for recruiter; idle=%.0fs timeout=%.0fs answers=%d",
                    idle,
                    self.config.response_timeout,
                    current["answers"],
                )
                self._wait_logged_at = now
            return
        if len(messages) >= 30:
            raise ValueError(
                "Слишком много непрочитанных сообщений. Нужна ручная проверка."
            )
        if any(m.buttons for m in incoming):
            self.pause()
            await self.telegram.notify(
                "Рекрутер прислал кнопки. Выбери ответ в его чате, затем "
                + ", ".join(f"/handled {m.id}" for m in incoming if m.buttons)
                + " и /resume."
            )
            return
        if not any(m.raw_text for m in incoming):
            self.pause()
            await self.telegram.notify(
                "Нет текстового вопроса. Проверь вложения в чате."
            )
            return
        job = next(j for j in self.store.jobs if j["key"] == current["job"])
        token = self.epoch
        log.info("Recruiter messages received: %d; preparing answer", len(incoming))
        texts = [m.raw_text for m in incoming]
        has_question = any((text or "").rstrip().endswith("?") for text in texts)
        answered_invitation = any(
            left["role"] == "recruiter"
            and additional_questions(left["text"])
            and right["role"] == "candidate"
            for left, right in zip(current["history"], current["history"][1:])
        )
        if (
            texts
            and additional_questions(texts[-1])
            and (self.mode == "auto" or answered_invitation)
        ):
            result = {
                "action": "reply",
                "text": "Пока других вопросов нет, спасибо.",
                "reason": "",
            }
            log.info("Closing candidate questions without starting another exchange")
        else:
            materials = await self.materials.get()
            for attempt in range(2):
                result = await self.model.answer(
                    materials, job, current["history"], texts
                )
                if token != self.epoch or not self.active:
                    return  # /pause during generation invalidates the result.
                if result["action"] != "wait" or not has_question or attempt:
                    break
                log.info("Model chose wait on an explicit question; regenerating once")
        if result["action"] == "done":
            # Completion is a bot protocol event, never just a model classification.
            if texts and additional_questions(texts[-1]):
                result = {
                    "action": "reply",
                    "text": "Пока других вопросов нет, спасибо.",
                    "reason": "",
                }
                log.warning("Rejected premature model completion on candidate question")
            else:
                result = {
                    "action": "needs_input",
                    "text": "",
                    "reason": "Рекрутер ещё не подтвердил завершение интервью. Проверь вопрос: /answer текст или /retry.",
                }
                log.warning("Rejected model completion without recruiter confirmation")
        if result["action"] == "wait" and has_question:
            result = {
                "action": "needs_input",
                "text": "",
                "reason": "Модель дважды выбрала ожидание на явный вопрос. Используй /retry или /answer текст.",
            }
        latest = await self.telegram.pending(current["watermark"])
        if {fingerprint(m) for m in latest if not m.out} != {
            fingerprint(m) for m in messages if not m.out
        }:
            self.wake()
            return  # New or edited recruiter message: regenerate on the next tick.
        current["processed"].extend(fingerprint(m) for m in incoming)
        current["history"].extend(
            {"role": "recruiter", "text": m.raw_text} for m in incoming
        )
        action = result["action"]
        current["last_action"] = action
        current["last_progress_at"] = datetime.now(UTC).timestamp()
        log.info("Model decision: %s", action)
        if action == "reply":
            current["pending"] = (
                result["text"].strip().translate(str.maketrans({"—": "-", "–": "-"}))
            )
            await self.store.save()
            if self.mode == "auto":
                await self.send_pending(automatic=True)
            else:
                self.pause()
                await self.telegram.notify(
                    "Черновик:\n" + current["pending"] + "\n/send или /answer текст"
                )
        elif action == "needs_input":
            current["blocked_reason"] = result["reason"]
            self.pause()
            await self.store.save()
            await self.telegram.notify(
                "Нужно уточнение: "
                + result["reason"]
                + "\n/answer текст, затем /resume"
            )
        else:
            await self.store.save()

    async def resume(self) -> None:
        await self.materials.get()
        if self.current is None:
            self._menu_wait = None
            self._visited_pages.clear()
        if (
            self.current
            and self.current["phase"] == "interview"
            and not self.current["answers"]
            and not self.current["history"]
        ):
            job = next(j for j in self.store.jobs if j["key"] == self.current["job"])
            if self.current["watermark"] == job["message_id"]:
                # Recover sessions started before edits at the boundary were included.
                self.current["watermark"] = max(0, job["message_id"] - 1)
                if job["source_hash"] not in self.current["processed"]:
                    self.current["processed"].append(job["source_hash"])
                await self.store.save()
        if self.current and self.current["phase"] not in {"interview", "finishing"}:
            raise ValueError(
                "Предыдущее действие могло выполниться. Проверь чат; автоматический повтор заблокирован."
            )
        if self.current and self.current["pending"]:
            raise ValueError("Ожидает черновик. /send либо /answer текст.")
        tail = self._unanswered_history()
        if (
            self.current
            and self.current["phase"] == "interview"
            and (
                self.current.get("blocked_reason")
                or (
                    not self.current.get("last_action")
                    and tail
                    and "?" in tail[-1]["text"]
                )
            )
        ):
            raise ValueError(
                "Сохранён вопрос без ответа. /retry — пересоздать ответ; /answer текст — ответить вручную."
            )
        if self.current:
            self.current["last_progress_at"] = datetime.now(UTC).timestamp()
            await self.store.save()
        self.active = True
        self.wake()

    def _unanswered_history(self):
        tail = []
        for entry in reversed((self.current or {}).get("history", [])):
            if entry["role"] != "recruiter":
                break
            tail.append(entry)
        return list(reversed(tail))

    async def retry_last_question(self) -> None:
        current = self.current
        if not current or current["phase"] != "interview":
            raise ValueError("Нет вопроса для повторной генерации. Проверь /status.")
        if current["pending"]:
            raise ValueError("Уже есть черновик: /draft и /send либо /answer текст.")
        tail = self._unanswered_history()
        if tail:
            messages = await self.telegram.pending(current["watermark"])
            candidates = sorted(
                (m for m in messages if not m.out), key=lambda m: m.id, reverse=True
            )
            replay = []
            upper = float("inf")
            for entry in reversed(tail):
                match = next(
                    (
                        m
                        for m in candidates
                        if m.id < upper and (m.raw_text or "") == entry["text"]
                    ),
                    None,
                )
                if match is None:
                    raise ValueError(
                        "Не удалось найти сохранённый вопрос в последних сообщениях. Используй /answer текст."
                    )
                replay.append(match)
                upper = match.id
            if any(m.out and m.id > max(item.id for item in replay) for m in messages):
                raise ValueError(
                    "После этого вопроса уже есть исходящее сообщение. Повторная генерация заблокирована; проверь чат."
                )
            replay_keys = {fingerprint(m) for m in replay}
            current["processed"] = [
                key for key in current["processed"] if key not in replay_keys
            ]
            del current["history"][-len(tail) :]
        current.pop("blocked_reason", None)
        current["last_action"] = "retry"
        await self.store.save()
        log.info(
            "Unanswered question scheduled for regeneration; recruiter_messages=%d",
            len(tail),
        )
        await self.resume()

    async def skip_current(self) -> None:
        current = self.current
        if not current:
            raise ValueError("Нет текущего интервью.")
        if current["phase"] != "interview":
            raise ValueError(
                "Сначала проверь результат предыдущего действия: /recover и /status."
            )
        self.pause()
        job = next(job for job in self.store.jobs if job["key"] == current["job"])
        job["status"] = "skipped"
        job["history"] = current["history"]
        job["answers"] = current["answers"]
        job["finished_at"] = datetime.now(UTC).isoformat()
        self.store.current = None
        await self.store.save()
        log.info("Current vacancy skipped; interview cleared")

    async def handled(self, message_id: int) -> None:
        if not self.current:
            raise ValueError("Нет текущего интервью.")
        message = await self.telegram.message(message_id)
        if not message or message.out or message.id <= self.current["watermark"]:
            raise ValueError("Сообщение не принадлежит текущему интервью.")
        key = fingerprint(message)
        if key not in self.current["processed"]:
            self.current["processed"].append(key)
            self.current["history"].append(
                {"role": "recruiter", "text": message.raw_text}
            )
            await self.store.save()

    async def reopen_unfinished(self, url: str) -> None:
        if self.current:
            raise ValueError("Сначала восстанови или заверши текущее интервью.")
        job = next(
            (
                j
                for j in self.store.jobs
                if j.get("url") and vacancy_identity(j["url"]) == vacancy_identity(url)
            ),
            None,
        )
        history = (job or {}).get("history", [])
        if (
            not job
            or job["status"] != "done"
            or job.get("rating")
            or not history
            or history[-1]["role"] != "recruiter"
            or not additional_questions(history[-1]["text"])
            or any(e["role"] == "recruiter" and completed(e["text"]) for e in history)
        ):
            raise ValueError(
                "Не найдено ошибочное завершение на вопросе рекрутера без итогового сообщения."
            )
        messages = await self.telegram.pending(max(0, job["message_id"] - 1))
        latest = max(
            (m for m in messages if not m.out), key=lambda m: m.id, default=None
        )
        if (
            latest is None
            or (latest.raw_text or "") != history[-1]["text"]
            or any(m.out and m.id > latest.id for m in messages)
        ):
            raise ValueError(
                "Диалог уже изменился или на вопрос есть ответ. Автоматическое восстановление заблокировано."
            )
        known = {e["text"] for e in history[:-1] if e["role"] == "recruiter"}
        self.pause()
        self.store.current = {
            "job": job["key"],
            "watermark": max(0, job["message_id"] - 1),
            "history": list(history[:-1]),
            "answers": job.get("answers", 0),
            "processed": [
                fingerprint(m)
                for m in messages
                if not m.out and m.id != latest.id and (m.raw_text or "") in known
            ],
            "pending": None,
            "phase": "interview",
            "last_action": "retry",
            "last_progress_at": datetime.now(UTC).timestamp(),
        }
        job["status"] = "active"
        job.pop("finished_at", None)
        await self.store.save()
        log.info("False completion restored; unanswered_message_id=%d", latest.id)

    async def recover(self, outcome: str) -> None:
        current = self.current
        if not current:
            raise ValueError("Нет текущего интервью.")
        if outcome == "not-started":
            if (
                current["phase"] not in {"clicking", "interview"}
                or current["answers"]
                or current["history"]
                or current["pending"]
            ):
                raise ValueError(
                    "Возврат разрешён только для ещё не начавшегося диалога без ответов."
                )
            self.pause()
            job = next(job for job in self.store.jobs if job["key"] == current["job"])
            job["status"] = "queued"
            self.store.current = None
            await self.store.save()
            return
        if current["phase"] == "rating" and outcome in {"rated", "not-rated"}:
            current["phase"] = "finishing"
            current["rating_done"] = outcome == "rated"
            if outcome == "rated":
                job = next(
                    job for job in self.store.jobs if job["key"] == current["job"]
                )
                job["rating"] = 5
            await self.store.save()
            return
        if current["phase"] == "sending" and outcome == "sent":
            current["history"].append({"role": "candidate", "text": current["pending"]})
            current["pending"] = None
            current["answers"] += 1
        elif not (
            (current["phase"] == "sending" and outcome == "not-sent")
            or (current["phase"] == "clicking" and outcome == "clicked")
        ):
            raise ValueError(
                "Неверная фаза/аргумент. clicked — только если интервью уже началось вручную."
            )
        current["phase"] = "interview"
        await self.store.save()

    async def answer_manually(self, text: str) -> bool:
        if not self.current or self.current["phase"] != "interview":
            raise ValueError(
                "Нет текущего интервью или результат отправки неопределён."
            )
        if not 1 <= len(text) <= 3500:
            raise ValueError("Ответ должен содержать от 1 до 3500 символов.")
        # A manual answer explicitly addresses every currently visible question.
        messages = await self.telegram.pending(self.current["watermark"])
        for message in reversed(messages):
            key = fingerprint(message)
            if not message.out and key not in self.current["processed"]:
                self.current["processed"].append(key)
                self.current["history"].append(
                    {"role": "recruiter", "text": message.raw_text}
                )
        self.current.pop("blocked_reason", None)
        self.current["last_action"] = "reply"
        self.current["pending"] = text
        await self.store.save()
        return await self.send_pending()

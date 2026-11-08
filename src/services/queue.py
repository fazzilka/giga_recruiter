import asyncio

import httpx

from src.core.db.repositories.workflow import WorkflowRepository
from src.core.vacancies import vacancy_identity
from src.services.cards import bind_card, latest_menu
from src.services.vacancies import VacancyService, allowed_url
from src.userbot.services.telegram import TelegramGateway
from src.userbot.utils.messages import (
    find_button,
    fingerprint,
    links,
    supported,
    vacancy_choices,
)


class QueueService:
    def __init__(
        self,
        telegram: TelegramGateway,
        store: WorkflowRepository,
        vacancies: VacancyService,
    ):
        self.telegram, self.store, self.vacancies = telegram, store, vacancies
        self.hosts = vacancies.hosts
        self._pending_page = None

    async def next_page(self):
        menu = latest_menu(await self.telegram.recent(30))
        if menu is None:
            raise ValueError(
                "Актуальный список вакансий не найден. Открой его у рекрутера."
            )
        key = fingerprint(menu)
        if key == self._pending_page:
            return "Обновление страницы ещё ожидается. После появления нового списка используй /scan."
        button = find_button(menu, "Далее")
        self._pending_page = key
        await button.click()
        return "Переход на следующую страницу запрошен. Когда список обновится, используй /scan и /run."

    async def add(self, message_id, button_text, source, raw=False):
        key = f"{int(message_id)}:{button_text}"
        if any(
            job["key"] == key
            or (
                not raw
                and job.get("url")
                and vacancy_identity(job["url"]) == vacancy_identity(source)
            )
            for job in self.store.jobs
        ):
            return "Уже в очереди."
        message = await self.telegram.message(int(message_id))
        if message is None or message.out:
            raise ValueError("Карточка рекрутера не найдена.")
        find_button(message, button_text)
        text = source if raw else await self.vacancies.text(source)
        if not 150 <= len(text) <= 30_000:
            raise ValueError("Нужен полный текст вакансии: от 150 до 30 000 символов.")
        added = await self.store.add(
            {
                "message_id": message.id,
                "button": button_text,
                "source_hash": fingerprint(message),
                "url": "" if raw else source,
                "text": text,
            }
        )
        return "Добавлено." if added else "Уже в очереди."

    async def scan(self):
        messages = await self.telegram.recent(30)
        cards = []
        candidates = []
        for message in reversed(messages):
            if message.out:
                continue
            buttons = [
                b.text for row in message.buttons or [] for b in row if supported(b)
            ]
            if not buttons:
                continue
            urls = [u for u in links(message) if allowed_url(u, self.hosts)]
            if not urls:
                continue
            cards.append(
                f"ID {message.id}: {', '.join(buttons)}; ссылки: {', '.join(urls)}"
            )
            for button, url in vacancy_choices(
                message, urls, lambda value: allowed_url(value, self.hosts)
            ):
                key = f"{message.id}:{button}"
                existing = next(
                    (
                        job
                        for job in self.store.jobs
                        if job["key"] == key
                        or (
                            job.get("url")
                            and vacancy_identity(job["url"]) == vacancy_identity(url)
                        )
                    ),
                    None,
                )
                if existing is not None:
                    if (
                        existing["status"] == "queued"
                        and existing.get("url")
                        and vacancy_identity(existing["url"]) == vacancy_identity(url)
                        and message.id >= existing["message_id"]
                    ):
                        bind_card(existing, message, button)
                        await self.store.save()
                        cards.append("Карточка в очереди обновлена.")
                    else:
                        cards.append("Уже в очереди или завершено.")
                else:
                    candidates.append((message, button, url))

        async def prepare(message, button, url):
            try:
                find_button(message, button)
                text = await self.vacancies.text(url)
                return {
                    "message_id": message.id,
                    "button": button,
                    "source_hash": fingerprint(message),
                    "url": url,
                    "text": text,
                }
            except (ValueError, httpx.HTTPError) as exc:
                return f"Не добавлено ID {message.id}: {type(exc).__name__}. Используй /addtext."

        # Only page downloads overlap. Persist results in the original card order.
        prepared = await asyncio.gather(
            *(prepare(*candidate) for candidate in candidates)
        )
        for result in prepared:
            if isinstance(result, str):
                cards.append(result)
            else:
                cards.append(
                    "Добавлено." if await self.store.add(result) else "Уже в очереди."
                )
        return (
            "\n".join(cards)
            or "Карточки не найдены. Открой список вакансий в боте и повтори /scan."
        )

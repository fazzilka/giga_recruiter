"""Refresh Telegram card references by vacancy identity, not button position."""

import logging
from urllib.parse import urlsplit

from src.core.vacancies import vacancy_identity
from src.services.vacancies import allowed_url
from src.userbot.utils.messages import find_button, fingerprint, links, vacancy_choices

log = logging.getLogger("recruiter.cards")


class CardUnavailable(ValueError):
    """No unambiguous card for this vacancy is currently available."""


def matched_button(job, message):
    if message is None or message.out or not job.get("url"):
        return None
    hosts = {urlsplit(job["url"]).hostname}
    urls = [url for url in links(message) if allowed_url(url, hosts)]
    choices = vacancy_choices(message, urls, lambda url: allowed_url(url, hosts))
    matches = [
        name
        for name, url in choices
        if vacancy_identity(url) == vacancy_identity(job["url"])
    ]
    if len(matches) != 1:
        return None
    return find_button(message, matches[0])


def bind_card(job, message, button_text):
    # Keep the database key, position, vacancy text and completion status unchanged.
    job.update(
        message_id=message.id, button=button_text, source_hash=fingerprint(message)
    )
    log.info("Queued vacancy card refreshed: message_id=%d", message.id)


def latest_menu(messages):
    message = max((m for m in messages if not m.out), key=lambda m: m.id, default=None)
    if message is None or not message.buttons:
        return None
    text = " ".join((message.raw_text or "").casefold().split())
    if "по какой из них" in text or "выберите вакансию" in text:
        return message
    if hasattr(message, "get_entities_text"):
        urls = links(message)
        if vacancy_choices(message, urls, lambda url: True):
            return message
    return None


async def resolve_card(telegram, store, job):
    original = await telegram.message(job["message_id"])
    recent = await telegram.recent(30)
    menu = latest_menu(recent)
    if menu is not None:
        original = menu
    elif not any(
        m.id == job["message_id"] and not m.out and m.buttons for m in recent[:1]
    ):
        original = None
    if (
        original is not None
        and not original.out
        and fingerprint(original) == job["source_hash"]
    ):
        return original, find_button(original, job["button"])
    if job.get("url"):
        button = matched_button(job, original)
        if button is not None:
            bind_card(job, original, button.text)
            await store.save()
            return original, button
    raise CardUnavailable(
        f"Исходная карточка изменилась или недоступна. Не найдена актуальная карточка вакансии «{job['button']}»"
        + (f" ({job['url']})" if job.get("url") else "")
        + ". Открой у рекрутера страницу списка именно с этой вакансией, затем /scan и /run. "
        "Сканирование других страниц не обновит эту карточку. Вакансия остаётся в очереди."
    )

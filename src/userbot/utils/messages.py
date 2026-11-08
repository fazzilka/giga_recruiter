import hashlib
import json

from telethon.tl.types import ButtonTypeDefault, InlineButtonTypeCallback


def supported(button):
    return isinstance(button.button.type, (ButtonTypeDefault, InlineButtonTypeCallback))


def find_button(message, text):
    matches = [
        b
        for row in message.buttons or []
        for b in row
        if b.text == text and supported(b)
    ]
    if len(matches) != 1:
        raise ValueError(
            "Не найдена одна однозначная текстовая/callback-кнопка. Нужна адаптация бота."
        )
    return matches[0]


def fingerprint(message):
    markup = getattr(message, "reply_markup", None)
    content = (message.raw_text or "") + (
        json.dumps(markup.to_dict(), default=str, sort_keys=True) if markup else ""
    )
    return f"{message.id}:" + hashlib.sha256(content.encode()).hexdigest()


def links(message):
    from telethon.tl.types import MessageEntityTextUrl, MessageEntityUrl

    urls = []
    for entity, text in message.get_entities_text():
        if isinstance(entity, MessageEntityTextUrl):
            urls.append(entity.url)
        elif isinstance(entity, MessageEntityUrl):
            urls.append(text)
    for row in message.buttons or []:
        for button in row:
            if getattr(button, "url", None):
                urls.append(button.url)
    return list(dict.fromkeys(urls))


def vacancy_choices(message, urls, is_allowed):
    import re

    buttons = [
        button for row in message.buttons or [] for button in row if supported(button)
    ]
    if len(buttons) == len(urls) == 1:
        return [(buttons[0].text, urls[0])]

    def normalize(value):
        return " ".join(re.sub(r"^\s*\d+[.)]\s*", "", value).casefold().split())

    counts = {}
    for button in buttons:
        name = normalize(button.text)
        counts[name] = counts.get(name, 0) + 1
    titles = {}
    for entity, title in getattr(message, "get_entities_text", lambda: [])():
        url = getattr(entity, "url", None)
        if url and is_allowed(url):
            titles.setdefault(normalize(title), set()).add(url)
    choices = []
    for button in buttons:
        matches = titles.get(normalize(button.text), set())
        if len(matches) == 1 and counts[normalize(button.text)] == 1:
            choices.append((button.text, next(iter(matches))))
    return choices

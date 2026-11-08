from types import SimpleNamespace

import pytest
from telethon.tl.types import (
    InlineButtonTypeCallback,
    KeyboardButton,
    MessageEntityTextUrl,
)

from src.core.vacancies import vacancy_identity
from src.services.feedback import completed, five_star_button
from src.userbot.utils.messages import vacancy_choices


def button(text):
    return SimpleNamespace(
        text=text, button=KeyboardButton(text, InlineButtonTypeCallback(b"1"))
    )


def test_multi_vacancy_menu_matches_titles_not_url_order():
    message = SimpleNamespace(
        buttons=[
            [button("1. Data Scientist")],
            [button("2. ML Engineer")],
            [button("Далее")],
        ],
        get_entities_text=lambda: [
            (
                MessageEntityTextUrl(0, 1, "https://rabota.sber.ru/search/2"),
                "ML Engineer",
            ),
            (
                MessageEntityTextUrl(0, 1, "https://rabota.sber.ru/search/1"),
                "Data Scientist",
            ),
        ],
    )
    result = vacancy_choices(
        message,
        ["https://rabota.sber.ru/search/2", "https://rabota.sber.ru/search/1"],
        lambda url: True,
    )
    assert result == [
        ("1. Data Scientist", "https://rabota.sber.ru/search/1"),
        ("2. ML Engineer", "https://rabota.sber.ru/search/2"),
    ]


def test_ambiguous_titles_are_not_guessed():
    message = SimpleNamespace(
        buttons=[[button("1. Analyst")], [button("2. Analyst")]],
        get_entities_text=lambda: [
            (MessageEntityTextUrl(0, 1, "https://rabota.sber.ru/search/1"), "Analyst")
        ],
    )
    assert vacancy_choices(message, ["url1", "url2"], lambda url: True) == []


@pytest.mark.parametrize(
    "text",
    [
        "Спасибо!",
        "Когда интервью завершено, что дальше?",
        "Почему вы решили, что интервью закончено?",
        "Оцените свои навыки SQL",
    ],
)
def test_intermediate_messages_are_not_completion(text):
    assert not completed(text)


def test_canonical_vacancy_id_ignores_title_and_slash():
    assert vacancy_identity(
        "https://rabota.sber.ru/search/4579379"
    ) == vacancy_identity("https://rabota.sber.ru/search/data-analyst-4579379/")


def test_unrelated_five_star_button_is_not_clicked():
    message = SimpleNamespace(
        id=1, raw_text="Выберите ответ", out=False, buttons=[[button("★★★★★")]]
    )
    assert five_star_button([message]) is None


def test_skill_self_rating_is_not_recruiter_feedback():
    buttons = [[button("★" * score + "☆" * (5 - score))] for score in range(1, 6)]
    message = SimpleNamespace(
        id=1, raw_text="Оцените свой опыт работы с SQL", out=False, buttons=buttons
    )
    assert five_star_button([message]) is None

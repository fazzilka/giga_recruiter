import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telethon.tl.types import InlineButtonTypeCallback, KeyboardButton

from src.config.config import CodexConfig, Config, InterviewConfig
from src.core.db.db import Database
from src.core.db.repositories.workflow import WorkflowRepository
from src.integrations.codex import validate_answer
from src.services.interview import InterviewService
from src.services.materials import load_materials
from src.services.vacancies import allowed_url
from src.userbot.services.commands import ChatCommandService
from src.userbot.services.telegram import TelegramGateway
from src.userbot.utils.messages import fingerprint


def commands(service):
    return ChatCommandService(service, SimpleNamespace())


async def reload_store(store):
    reloaded = WorkflowRepository(store.database)
    await reloaded.load()
    return reloaded


def message(mid=10, text="Почему вакансия интересна?", buttons=None):
    return SimpleNamespace(
        id=mid,
        raw_text=text,
        buttons=buttons,
        out=False,
        date=datetime.now(UTC) - timedelta(seconds=10),
        edit_date=None,
    )


@pytest.fixture
async def setup(tmp_path, monkeypatch):
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'test.sqlite3'}")
    await database.initialize()
    store = WorkflowRepository(database)
    await store.load()
    source = message(1, "Карточка")
    await store.add(
        {
            "message_id": 1,
            "button": "Data Analyst",
            "text": "SQL " * 100,
            "source_hash": fingerprint(source),
            "url": "",
        }
    )
    job = store.jobs[0]
    job["status"] = "active"
    store.current = {
        "job": job["key"],
        "watermark": 2,
        "history": [],
        "processed": [],
        "pending": None,
        "answers": 0,
        "phase": "interview",
    }
    client = SimpleNamespace(
        get_messages=AsyncMock(return_value=[message()]), send_message=AsyncMock()
    )
    model = SimpleNamespace(
        answer=AsyncMock(
            return_value={
                "action": "reply",
                "text": "Интересны задачи с SQL.",
                "reason": "",
            }
        )
    )
    config = Config(root=tmp_path)
    config.interview.send_delay = 0
    config.interview.feedback_wait = 0
    gateway = TelegramGateway(client, config)
    gateway.target = "target"
    materials = SimpleNamespace(get=AsyncMock(return_value="Подтвержденный опыт SQL"))
    app = InterviewService(
        gateway, store, model, materials, InterviewConfig(send_delay=0, feedback_wait=0)
    )
    app.active = True
    try:
        yield app, client, store
    finally:
        await database.close()


async def test_auto_reply_deduplicated_after_reload(setup):
    app, client, store = setup
    app.mode = "auto"
    await app.tick()
    reloaded = await reload_store(store)
    app.store = reloaded
    await app.tick()
    client.send_message.assert_awaited_once_with(
        "target", "Интересны задачи с SQL.", parse_mode=None
    )
    assert app.current["answers"] == 1
    assert app.model.answer.await_count == 1


async def test_draft_does_not_send_to_recruiter(setup):
    app, client, _ = setup
    await app.tick()
    assert app.current["pending"]
    assert not app.active
    assert all(c.args[0] == "me" for c in client.send_message.call_args_list)
    await commands(app).execute("/send")
    assert app.current["pending"] is None
    assert app.active


async def test_pause_during_generation_prevents_sending(setup):
    app, client, _ = setup
    app.mode = "auto"

    async def answer(*args):
        await commands(app).execute("/pause")
        return {"action": "reply", "text": "Ответ", "reason": ""}

    app.model.answer.side_effect = answer
    await app.tick()
    client.send_message.assert_not_awaited()
    assert not app.current["processed"]


async def test_failed_send_never_retried_automatically(setup):
    app, client, store = setup
    app.mode = "auto"
    client.send_message.side_effect = OSError("connection dropped")
    with pytest.raises(OSError):
        await app.tick()
    assert (await reload_store(store)).current["phase"] == "sending"
    client.send_message.reset_mock()
    await app.tick()
    client.send_message.assert_not_awaited()
    assert not app.active


async def test_missing_candidate_fact_pauses(setup):
    app, client, _ = setup
    app.mode = "auto"
    app.model.answer.return_value = {
        "action": "needs_input",
        "text": "",
        "reason": "Зарплата?",
    }
    await app.tick()
    assert not app.active
    assert all(c.args[0] == "me" for c in client.send_message.call_args_list)


async def test_model_done_cannot_finish_open_question(setup):
    app, client, store = setup
    app.model.answer.return_value = {
        "action": "done",
        "text": "",
        "reason": "Завершено",
    }
    await app.tick()
    assert app.current is not None
    assert app.current["last_action"] == "needs_input"
    assert not app.active
    assert store.jobs[0]["status"] == "active"


async def test_buttons_require_manual_handling(setup):
    app, client, _ = setup
    client.get_messages.return_value = [message(buttons=[[object()]])]
    await app.tick()
    assert not app.active
    app.model.answer.assert_not_awaited()
    assert "/handled 10" in client.send_message.call_args.args[1]


async def test_stale_source_does_not_click(setup):
    app, client, store = setup
    store.current = None
    store.jobs[0]["status"] = "queued"
    client.get_messages.side_effect = [message(1, "Карточка изменилась"), []]
    await app.start_next()
    assert app.current is None
    assert not app.active
    assert store.jobs[0]["status"] == "queued"


async def test_click_failure_recorded_before_network(setup):
    app, client, store = setup
    button = SimpleNamespace(
        text="Data Analyst",
        button=KeyboardButton("Data Analyst", InlineButtonTypeCallback(b"1")),
        click=AsyncMock(side_effect=OSError),
    )
    source = message(1, "Карточка", [[button]])
    store.current = None
    store.jobs[0]["status"] = "queued"
    client.get_messages.side_effect = [source, [source], [source]]
    with pytest.raises(OSError):
        await app.start_next()
    assert (await reload_store(store)).current["phase"] == "clicking"
    with pytest.raises(ValueError):
        await commands(app).execute("/resume")


async def test_store_deduplicates_same_card(setup):
    _, _, store = setup
    assert not await store.add(store.jobs[0])


@pytest.mark.parametrize(
    "url",
    [
        "http://rabota.sber.ru/job",
        "https://evil.test/job",
        "https://rabota.sber.ru.evil.test/job",
        "https://user@rabota.sber.ru/job",
        "https://rabota.sber.ru:444/job",
    ],
)
def test_url_allowlist(url):
    assert not allowed_url(url, {"rabota.sber.ru"})


def test_materials_empty_or_large_fails(tmp_path):
    with pytest.raises(ValueError):
        load_materials(tmp_path)
    (tmp_path / "resume.txt").write_text("SQL")
    (tmp_path / "experience.md").write_text("Проект аналитики")
    assert "SQL" in load_materials(tmp_path)
    assert "Проект аналитики" in load_materials(tmp_path)
    (tmp_path / "resume.txt").write_text("x" * 100_001)
    with pytest.raises(ValueError):
        load_materials(tmp_path)


@pytest.mark.parametrize(
    "result",
    [
        {},
        {"action": "reply", "text": "", "reason": ""},
        {"action": "execute", "text": "x", "reason": ""},
        {"action": "reply", "text": 1, "reason": ""},
    ],
)
def test_bad_model_output(result):
    with pytest.raises(ValueError):
        validate_answer(result)


async def test_new_question_during_generation_discards_answer(setup):
    app, client, _ = setup
    app.mode = "auto"
    old = message()
    client.get_messages.side_effect = [[old], [old, message(11, "Уточнение")]]
    await app.tick()
    client.send_message.assert_not_awaited()
    assert not app.current["processed"]


async def test_stale_draft_is_not_sent(setup):
    app, client, _ = setup
    await app.tick()
    client.get_messages.return_value = [message(), message(11, "Новый вопрос")]
    client.send_message.reset_mock()
    with pytest.raises(ValueError, match="обновился"):
        await app.send_pending()
    client.send_message.assert_not_awaited()
    assert app.current["pending"] is None


async def test_vacancy_parser_only_downloads_one_page(monkeypatch):
    import httpx

    from src.services.vacancies import VacancyService

    async def vacancy_text(url, hosts):
        async with httpx.AsyncClient() as client:
            return await VacancyService(client, hosts).text(url)

    calls = []

    def handle(request):
        calls.append(str(request.url))
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text="<nav>Лишнее</nav><main>" + "SQL аналитика " * 30 + "</main>",
        )

    original = httpx.AsyncClient
    monkeypatch.setattr(
        "src.services.vacancies.httpx.AsyncClient",
        lambda **kw: original(transport=httpx.MockTransport(handle), **kw),
    )
    text = await vacancy_text("https://rabota.sber.ru/job/1", {"rabota.sber.ru"})
    assert "Лишнее" not in text
    assert "SQL" in text
    assert calls == ["https://rabota.sber.ru/job/1"]


async def test_vacancy_external_redirect_is_not_followed(monkeypatch):
    import httpx

    from src.services.vacancies import VacancyService

    async def vacancy_text(url, hosts):
        async with httpx.AsyncClient() as client:
            return await VacancyService(client, hosts).text(url)

    original = httpx.AsyncClient
    transport = httpx.MockTransport(
        lambda req: httpx.Response(302, headers={"location": "https://evil.test"})
    )
    monkeypatch.setattr(
        "src.services.vacancies.httpx.AsyncClient",
        lambda **kw: original(transport=transport, **kw),
    )
    with pytest.raises(ValueError, match="запрещённый"):
        await vacancy_text("https://rabota.sber.ru/job/1", {"rabota.sber.ru"})


async def test_cli_boundary_without_real_model(tmp_path, monkeypatch):
    from src.integrations.codex import CodexClient

    fake = tmp_path / "fake-codex"
    fake.write_text("""#!/usr/bin/env python3
import json, pathlib, sys
prompt = sys.stdin.read()
assert "SQL" in prompt
assert "--sandbox" in sys.argv and "read-only" in sys.argv
assert "--ephemeral" in sys.argv
pathlib.Path(sys.argv[sys.argv.index("-o") + 1]).write_text(json.dumps({"action": "reply", "text": "Ответ", "reason": ""}))
""")
    fake.chmod(0o700)
    result = await CodexClient(CodexConfig(binary=str(fake))).answer(
        "SQL", {"text": "Вакансия"}, [], ["Вопрос"]
    )
    assert result["text"] == "Ответ"


async def test_pause_during_send_preflight_does_not_send(setup):
    app, client, _ = setup
    app.current["pending"] = "Ответ"

    async def messages(*args, **kwargs):
        app.pause()
        return []

    client.get_messages.side_effect = messages
    await app.send_pending(automatic=True)
    client.send_message.assert_not_awaited()
    assert app.current["phase"] == "interview"


async def test_pause_during_intent_commit_does_not_send(setup):
    app, client, store = setup
    app.current["pending"] = "Ответ"
    app.current["processed"] = [fingerprint(message())]
    original = store.save

    async def save():
        await original()
        app.pause()

    store.save = save
    await app.send_pending(automatic=True)
    client.send_message.assert_not_awaited()
    assert app.current["phase"] == "interview"


async def test_manual_answer_accepts_unbalanced_quotes_and_records_question(setup):
    app, client, _ = setup
    app.active = False
    text = 'Я работал над "проектом'
    await commands(app).execute("/answer " + text)
    client.send_message.assert_awaited_once_with("target", text, parse_mode=None)
    assert app.current["history"][0]["role"] == "recruiter"


async def test_recover_sent_does_not_resend(setup):
    app, client, _ = setup
    app.current["phase"] = "sending"
    app.current["pending"] = "Ответ"
    await app.recover("sent")
    assert app.current["pending"] is None
    assert app.current["answers"] == 1
    assert app.current["phase"] == "interview"
    client.send_message.assert_not_awaited()


async def test_cli_timeout_terminates_child(tmp_path):
    import os

    from src.integrations.codex import CodexClient

    fake = tmp_path / "slow-codex"
    pid_path = tmp_path / "pid"
    fake.write_text(
        "#!/usr/bin/env python3\nimport os, pathlib, time\n"
        + f"pathlib.Path({str(pid_path)!r}).write_text(str(os.getpid()))\n"
        + "time.sleep(60)\n"
    )
    fake.chmod(0o700)
    with pytest.raises(TimeoutError):
        await CodexClient(CodexConfig(binary=str(fake), timeout=0.2)).answer(
            "SQL", {"text": "SQL"}, [], ["Вопрос"]
        )
    if pid_path.exists():
        with pytest.raises(ProcessLookupError):
            os.kill(int(pid_path.read_text()), 0)


async def test_send_delay_waits_before_sending(setup):
    import time

    app, client, _ = setup
    app.config.send_delay = 0.03
    app.current["pending"] = "Ответ"
    app.current["processed"] = [fingerprint(message())]
    started = time.monotonic()
    assert await app.send_pending()
    assert time.monotonic() - started >= 0.03
    client.send_message.assert_awaited_once_with("target", "Ответ", parse_mode=None)


async def test_pause_cancels_thirty_second_wait_without_resuming(setup):
    import asyncio

    app, client, _ = setup
    app.config.send_delay = 30
    app.current["pending"] = "Ответ"
    app.current["processed"] = [fingerprint(message())]
    task = asyncio.create_task(commands(app).execute("/send"))
    await asyncio.sleep(0.01)
    await commands(app).execute("/pause")
    result = await asyncio.wait_for(task, 0.5)
    assert "отменена" in result
    assert not app.active
    assert app.current["pending"] == "Ответ"
    assert app.current["phase"] == "interview"
    client.send_message.assert_not_awaited()


async def test_new_question_during_delay_prevents_stale_answer(setup):
    import asyncio

    app, client, _ = setup
    app.config.send_delay = 0.03
    app.current["pending"] = "Старый ответ"
    app.current["processed"] = [fingerprint(message())]
    task = asyncio.create_task(app.send_pending(automatic=True))
    await asyncio.sleep(0.01)
    client.get_messages.return_value = [message(), message(11, "Новый вопрос")]
    with pytest.raises(ValueError, match="во время задержки"):
        await task
    assert app.current["pending"] is None
    client.send_message.assert_not_awaited()


async def test_generated_reply_removes_long_dashes_before_sending(setup):
    app, client, _ = setup
    app.mode = "auto"
    app.model.answer.return_value = {
        "action": "reply",
        "text": "SQL — основной инструмент, Python – для анализа.",
        "reason": "",
    }
    await app.tick()
    client.send_message.assert_awaited_once_with(
        "target", "SQL - основной инструмент, Python - для анализа.", parse_mode=None
    )


def rating_message(mid=12):
    buttons = []
    for score in range(1, 6):
        label = "★" * score + "☆" * (5 - score)
        buttons.append(
            [
                SimpleNamespace(
                    text=label,
                    button=KeyboardButton(
                        label, InlineButtonTypeCallback(bytes([score]))
                    ),
                    click=AsyncMock(return_value=SimpleNamespace(alert=False)),
                )
            ]
        )
    return message(mid, "Пожалуйста, оцените мою работу!", buttons)


async def test_completed_interview_rates_five_and_ignores_new_menu(setup):
    app, client, store = setup
    rating = rating_message()
    menu = message(13, "Выберите вакансию", [[object()]])
    client.get_messages.return_value = [
        menu,
        rating,
        message(11, "Спасибо за интервью! Я передам ваше резюме рекрутеру."),
    ]
    await app.tick()
    assert app.current is None
    assert store.jobs[0]["status"] == "done"
    assert store.jobs[0]["rating"] == 5
    rating.buttons[-1][0].click.assert_awaited_once()
    for row in rating.buttons[:-1]:
        row[0].click.assert_not_awaited()
    app.model.answer.assert_not_awaited()
    assert "Очередь пуста" in await commands(app).execute("/queue")
    assert "done:" in await commands(app).execute("/history")
    restored = await reload_store(store)
    assert restored.jobs[0]["rating"] == 5
    assert restored.current is None


async def test_rating_request_alone_finishes_interview(setup):
    app, client, store = setup
    rating = rating_message()
    client.get_messages.return_value = [rating]
    await app.tick()
    assert app.current is None
    assert store.jobs[0]["rating"] == 5
    app.model.answer.assert_not_awaited()


async def test_completion_clears_stale_draft(setup):
    app, client, store = setup
    app.current["pending"] = "Не отправлять"
    client.get_messages.return_value = [
        message(11, "Спасибо за интервью! Передам резюме.")
    ]
    await app.tick()
    assert app.current is None
    assert store.jobs[0]["status"] == "done"
    assert all(call.args[0] == "me" for call in client.send_message.call_args_list)


async def test_feedback_arriving_after_completion_is_rated(setup):
    app, client, _ = setup
    app.config.feedback_wait = 10
    terminal = message(11, "Спасибо за интервью! Передам резюме.")
    client.get_messages.return_value = [terminal]
    await app.tick()
    assert app.current["phase"] == "finishing"
    assert app.store.jobs[0]["status"] == "done"
    rating = rating_message()
    client.get_messages.return_value = [rating, terminal]
    await app.tick()
    assert app.current is None
    rating.buttons[-1][0].click.assert_awaited_once()


async def test_uncertain_rating_not_repeated_after_restart(setup):
    app, client, store = setup
    rating = rating_message()
    rating.buttons[-1][0].click.side_effect = OSError("connection lost")
    client.get_messages.return_value = [rating]
    with pytest.raises(OSError):
        await app.tick()
    assert store.current["phase"] == "rating"
    restored = await reload_store(store)
    assert restored.current["phase"] == "rating"
    with pytest.raises(ValueError, match="неизвестен"):
        await app.tick()
    rating.buttons[-1][0].click.assert_awaited_once()
    await app.recover("rated")
    await app.tick()
    assert app.current is None
    rating.buttons[-1][0].click.assert_awaited_once()


async def test_completed_message_in_saved_history_recovers_stuck_job(setup):
    app, client, store = setup
    app.current["history"].append(
        {"role": "recruiter", "text": "Спасибо за интервью! Я передам ваше резюме."}
    )
    client.get_messages.return_value = [message(15, "Выберите вакансию", [[object()]])]
    await app.tick()
    assert store.jobs[0]["status"] == "done"
    assert app.current is None
    app.model.answer.assert_not_awaited()


async def test_finished_vacancy_not_readded_from_new_menu(setup):
    _, _, store = setup
    store.jobs[0]["url"] = "https://rabota.sber.ru/search/4579379"
    store.jobs[0]["status"] = "done"
    await store.save()
    assert not await store.add(
        {
            "message_id": 993,
            "button": "4. Data Analyst",
            "url": "https://rabota.sber.ru/search/data-analyst-4579379/",
            "text": "SQL " * 100,
            "source_hash": "unused",
        }
    )
    assert len(store.jobs) == 1


async def test_rating_callback_timeout_keeps_uncertain_state(setup):
    app, client, _ = setup
    rating = rating_message()
    rating.buttons[-1][0].click.return_value = None
    client.get_messages.return_value = [rating]
    with pytest.raises(ValueError, match="неизвестен"):
        await app.tick()
    assert app.current["phase"] == "rating"
    assert app.store.jobs[0]["status"] == "done"


async def test_retry_regenerates_needs_input_question_after_database_reload(setup):
    app, client, store = setup
    app.model.answer.return_value = {
        "action": "needs_input",
        "text": "",
        "reason": "Нужны сведения об опыте.",
    }
    await app.tick()
    assert app.current["blocked_reason"]
    with pytest.raises(ValueError, match="/retry"):
        await app.resume()
    restored = await reload_store(store)
    restarted = InterviewService(
        app.telegram, restored, app.model, app.materials, app.config
    )
    restarted.mode = "auto"
    app.model.answer.return_value = {
        "action": "reply",
        "text": "Из близкого опыта у меня задачи на SQL.",
        "reason": "",
    }
    await commands(restarted).execute("/retry")
    await restarted.tick()
    assert restarted.current["answers"] == 1
    assert restarted.current["history"] == [
        {"role": "recruiter", "text": message().raw_text},
        {"role": "candidate", "text": "Из близкого опыта у меня задачи на SQL."},
    ]
    assert not restarted.current.get("blocked_reason")
    assert app.model.answer.await_count == 2


async def test_retry_recovers_legacy_processed_unanswered_question(setup):
    app, client, _ = setup
    question = message()
    app.current["processed"] = [fingerprint(question)]
    app.current["history"] = [
        {"role": "candidate", "text": "Предыдущий ответ"},
        {"role": "recruiter", "text": question.raw_text},
    ]
    with pytest.raises(ValueError, match="/retry"):
        await app.resume()
    await commands(app).execute("/retry")
    assert app.current["processed"] == []
    assert app.current["history"] == [{"role": "candidate", "text": "Предыдущий ответ"}]
    await app.tick()
    assert app.current["pending"]
    assert app.mode == "draft"
    app.model.answer.assert_awaited_once()


async def test_retry_does_not_resend_answered_messages(setup):
    app, client, _ = setup
    app.mode = "auto"
    await app.tick()
    await commands(app).execute("/retry")
    await app.tick()
    assert app.current["answers"] == 1
    app.model.answer.assert_awaited_once()


async def test_retry_rejects_question_answered_manually_in_telegram(setup):
    app, client, _ = setup
    question = message()
    app.current["processed"] = [fingerprint(question)]
    app.current["history"] = [{"role": "recruiter", "text": question.raw_text}]
    manual = message(11, "Мой ручной ответ")
    manual.out = True
    client.get_messages.return_value = [manual, question]
    with pytest.raises(ValueError, match="исходящее"):
        await commands(app).execute("/retry")
    assert app.current["processed"] == [fingerprint(question)]
    app.model.answer.assert_not_awaited()


async def test_retry_blocked_during_uncertain_send(setup):
    app, _, _ = setup
    app.current["phase"] = "sending"
    with pytest.raises(ValueError):
        await app.retry_last_question()
    app.model.answer.assert_not_awaited()


def linked_card(mid, title, vacancy_id, text="Обновлённый список вакансий"):
    from telethon.tl.types import MessageEntityTextUrl

    btn = SimpleNamespace(
        text=title,
        button=KeyboardButton(title, InlineButtonTypeCallback(b"new-callback")),
        click=AsyncMock(return_value=SimpleNamespace()),
    )
    card = message(mid, text, [[btn]])
    card.get_entities_text = lambda: [
        (
            MessageEntityTextUrl(0, 1, f"https://rabota.sber.ru/search/{vacancy_id}"),
            title.split(". ", 1)[-1],
        )
    ]
    return card, btn


async def test_start_refreshes_edited_card_for_same_vacancy(setup):
    app, client, store = setup
    store.current = None
    store.jobs[0].update(status="queued", url="https://rabota.sber.ru/search/123")
    card, btn = linked_card(1, "2. Data Analyst", 123)
    client.get_messages.side_effect = [card, [card], [card]]
    await app.start_next()
    assert store.current is not None
    assert store.jobs[0]["button"] == "2. Data Analyst"
    assert store.jobs[0]["status"] == "active"
    assert store.jobs[0]["key"] == "1:Data Analyst"
    btn.click.assert_awaited_once()
    restored = await reload_store(store)
    assert restored.jobs[0]["button"] == "2. Data Analyst"


async def test_start_finds_same_vacancy_in_new_menu(setup):
    app, client, store = setup
    store.current = None
    store.jobs[0].update(status="queued", url="https://rabota.sber.ru/search/123")
    old, old_btn = linked_card(1, "1. Other vacancy", 999)
    fresh, fresh_btn = linked_card(20, "4. Data Analyst", 123)
    client.get_messages.side_effect = [old, [fresh, old], [fresh]]
    await app.start_next()
    assert store.jobs[0]["message_id"] == 20
    assert store.jobs[0]["button"] == "4. Data Analyst"
    assert store.current["watermark"] == 19
    old_btn.click.assert_not_awaited()
    fresh_btn.click.assert_awaited_once()


async def test_changed_card_for_other_vacancy_is_not_clicked(setup):
    app, client, store = setup
    store.current = None
    store.jobs[0].update(status="queued", url="https://rabota.sber.ru/search/123")
    wrong, btn = linked_card(1, "Data Analyst", 999)
    client.get_messages.side_effect = [wrong, [wrong]]
    await app.start_next()
    notice = client.send_message.call_args.args[1]
    assert "Data Analyst" in notice
    assert "123" in notice
    assert "страницу списка" in notice
    assert store.current is None
    assert not app.active
    assert store.jobs[0]["status"] == "queued"
    btn.click.assert_not_awaited()


async def test_scan_refreshes_queued_card_without_readding_or_redownloading(setup):
    from src.services.queue import QueueService

    app, client, store = setup
    store.current = None
    store.jobs[0].update(status="queued", url="https://rabota.sber.ru/search/123")
    card, _ = linked_card(20, "4. Data Analyst", 123)
    client.get_messages.return_value = [card]
    vacancies = SimpleNamespace(hosts={"rabota.sber.ru"}, text=AsyncMock())
    queue = QueueService(app.telegram, store, vacancies)
    await queue.scan()
    assert len(store.jobs) == 1
    assert store.jobs[0]["message_id"] == 20
    assert store.jobs[0]["button"] == "4. Data Analyst"
    assert store.jobs[0]["key"] == "1:Data Analyst"
    vacancies.text.assert_not_awaited()


async def test_skip_archives_current_job_and_preserves_next_after_reload(setup):
    app, client, store = setup
    app.current["history"] = [{"role": "recruiter", "text": "Вопрос"}]
    app.current["blocked_reason"] = "Нужно уточнение"
    app.current["pending"] = "Не отправлять"
    await store.add(
        {
            "message_id": 20,
            "button": "Следующая",
            "url": "",
            "text": "SQL " * 100,
            "source_hash": "next",
        }
    )

    await commands(app).execute("/skip")

    reloaded = await reload_store(store)
    assert reloaded.current is None
    assert reloaded.jobs[0]["status"] == "skipped"
    assert reloaded.jobs[0]["history"] == [{"role": "recruiter", "text": "Вопрос"}]
    assert reloaded.jobs[1]["status"] == "queued"
    assert not app.active
    assert "Data Analyst" not in await commands(app).execute("/queue")
    assert "skipped: 1 Data Analyst" in await commands(app).execute("/history")
    assert not await reloaded.add(
        {
            "message_id": 1,
            "button": "Data Analyst",
            "url": "",
            "text": "SQL " * 100,
            "source_hash": "new",
        }
    )
    client.send_message.assert_not_awaited()


@pytest.mark.parametrize("phase", ["sending", "clicking", "rating"])
async def test_skip_rejects_uncertain_network_action(setup, phase):
    app, client, store = setup
    app.current["phase"] = phase
    with pytest.raises(ValueError, match="recover"):
        await commands(app).execute("/skip")
    assert store.current["phase"] == phase
    assert store.jobs[0]["status"] == "active"
    client.send_message.assert_not_awaited()


async def test_skip_without_current_does_not_remove_queued_job(setup):
    app, client, store = setup
    store.current = None
    store.jobs[0]["status"] = "queued"
    with pytest.raises(ValueError, match="Нет текущего интервью"):
        await commands(app).execute("/skip")
    assert store.jobs[0]["status"] == "queued"


async def test_rating_timeout_accepts_confirmation_on_original_card(setup):
    app, client, store = setup
    rating = rating_message()
    rating.buttons[-1][0].click.return_value = None
    confirmation = message(rating.id, "Спасибо за обратную связь!")
    client.get_messages.side_effect = [[rating], confirmation]
    await app.tick()
    restored = await reload_store(store)
    assert restored.current is None
    assert restored.jobs[0]["rating"] == 5
    rating.buttons[-1][0].click.assert_awaited_once()


async def test_rating_timeout_accepts_delayed_edit_without_clicking_twice(setup):
    app, client, store = setup
    rating = rating_message()
    rating.buttons[-1][0].click.return_value = None
    confirmation = message(rating.id, "Спасибо за обратную связь!")
    client.get_messages.side_effect = [[rating], rating, confirmation]
    await app.tick()
    assert (await reload_store(store)).current is None
    assert store.jobs[0]["rating"] == 5
    rating.buttons[-1][0].click.assert_awaited_once()


async def test_rating_timeout_does_not_accept_unrelated_confirmation(setup):
    app, client, _ = setup
    rating = rating_message()
    rating.buttons[-1][0].click.return_value = None
    client.get_messages.side_effect = [
        [rating],
        *[message(rating.id + 1, "Спасибо за обратную связь!")] * 3,
    ]
    with pytest.raises(ValueError, match="неизвестен"):
        await app.tick()
    assert app.current["phase"] == "rating"
    rating.buttons[-1][0].click.assert_awaited_once()


async def test_five_interviews_complete_in_one_queue_with_rating_timeouts(setup):
    app, client, store = setup
    store.current = None
    store.jobs[0]["status"] = "queued"
    stream = []
    cards = {}
    clicks = []
    sent_answers = []
    rated = []
    questions = [
        "Удобно сейчас ответить на вопросы?",
        "Какие задачи решали с Python?",
        "Есть ли вопросы по вакансии?",
    ]
    state = {"job": None, "question": 0, "sequence": 100}

    def incoming(text):
        state["sequence"] += 1
        item = message(state["sequence"], text)
        stream.append(item)
        return item

    async def read_messages(target, *, limit=None, min_id=0, ids=None):
        if ids is not None:
            return next((m for m in stream if m.id == ids), None)
        return sorted(
            [m for m in stream if m.id > min_id], key=lambda m: m.id, reverse=True
        )[:limit]

    async def send_message(target, text, **kwargs):
        if target == "me":
            return None
        state["sequence"] += 1
        outgoing = message(state["sequence"], text)
        outgoing.out = True
        stream.append(outgoing)
        sent_answers.append((state["job"], text))
        state["question"] += 1
        if state["question"] < len(questions):
            incoming(questions[state["question"]])
        else:
            incoming("Спасибо за интервью! Передам резюме рекрутеру.")
            state["sequence"] += 1
            rating = rating_message(state["sequence"])
            stream.append(rating)

            async def rate():
                rated.append(state["job"])
                # Actual Sber behaviour: edit succeeds, callback acknowledgement times out.
                rating.raw_text = "Спасибо за обратную связь!"
                rating.buttons = None
                next_index = state["job"] + 1
                if next_index in cards:
                    state["sequence"] += 1
                    old_card = cards[next_index]
                    new_menu, new_button = linked_card(
                        state["sequence"],
                        old_card.buttons[0][0].text,
                        5000 + next_index,
                    )
                    new_button.click.side_effect = old_card.buttons[0][
                        0
                    ].click.side_effect
                    stream.append(new_menu)
                return None

            rating.buttons[-1][0].click.side_effect = rate
        return outgoing

    client.get_messages.side_effect = read_messages
    client.send_message.side_effect = send_message
    app.mode = "auto"
    for index, title in enumerate(
        [
            "Data Analyst",
            "ML Engineer",
            "Python Backend",
            "LLM Engineer",
            "NLP Engineer",
        ],
        start=1,
    ):
        card, button = linked_card(index, f"{index}. {title}", 5000 + index)
        cards[index] = card
        if index == 1:
            stream.append(card)

        async def select(index=index):
            clicks.append(index)
            state.update(job=index, question=0)
            incoming(questions[0])
            return SimpleNamespace(alert=False)

        button.click.side_effect = select
        job = {
            "message_id": card.id,
            "button": button.text,
            "url": f"https://rabota.sber.ru/search/{5000 + index}",
            "text": "Python SQL " * 50,
            "source_hash": fingerprint(card),
        }
        if index == 1:
            store.jobs[0].update(job)
            await store.save()
        else:
            await store.add(job)

    for _ in range(30):
        await app.tick()
        if store.current is None and all(j["status"] == "done" for j in store.jobs):
            break
    restored = await reload_store(store)
    assert restored.current is None
    assert len(restored.jobs) == 5
    assert all(j["status"] == "done" and j["rating"] == 5 for j in restored.jobs)
    assert all(j["answers"] == 3 for j in restored.jobs)
    assert all(len(j["history"]) >= 6 for j in restored.jobs)
    assert clicks == [1, 2, 3, 4, 5]
    assert rated == [1, 2, 3, 4, 5]
    assert len(sent_answers) == 15
    assert "Очередь пуста" in await commands(app).execute("/queue")


async def test_start_handles_question_edited_into_latest_card(setup):
    app, client, store = setup
    store.current = None
    store.jobs[0].update(status="queued", url="https://rabota.sber.ru/search/123")
    card, button = linked_card(20, "1. Data Analyst", 123)
    store.jobs[0].update(
        message_id=20, button=button.text, source_hash=fingerprint(card)
    )

    async def read(target, *, ids=None, limit=None, min_id=0):
        if ids is not None:
            return card
        return [card] if card.id > min_id else []

    async def click():
        card.raw_text = "Будет удобно сейчас ответить на вопросы?"
        card.buttons = None

    client.get_messages.side_effect = read
    button.click.side_effect = click
    app.mode = "draft"
    await app.start_next()
    await app.tick()
    assert app.current["pending"] == "Интересны задачи с SQL."
    assert (
        app.current["history"][-1]["text"] == "Будет удобно сейчас ответить на вопросы?"
    )


async def test_resume_recovers_legacy_latest_card_edit_before_first_answer(setup):
    app, client, store = setup
    store.jobs[0]["message_id"] = 20
    app.current["watermark"] = 20
    edited = message(20, "Спасибо за выбор вакансии! Дайте мне несколько секунд")

    async def read(target, *, ids=None, limit=None, min_id=0):
        return [edited] if edited.id > min_id else []

    client.get_messages.side_effect = read
    app.model.answer.return_value = {"action": "wait", "text": "", "reason": ""}
    await app.resume()
    await app.tick()
    assert app.current["last_action"] == "wait"
    assert app.current["history"][-1]["text"] == edited.raw_text


async def test_wait_on_explicit_question_regenerates_before_marking_processed(setup):
    app, client, store = setup
    app.mode = "auto"
    client.get_messages.return_value = [
        message(text="С какими базами данных работали?")
    ]
    app.model.answer.side_effect = [
        {"action": "wait", "text": "", "reason": "Ошибочное ожидание"},
        {"action": "reply", "text": "Работал с PostgreSQL.", "reason": ""},
    ]
    await app.tick()
    assert app.current["answers"] == 1
    assert app.current["history"] == [
        {"role": "recruiter", "text": "С какими базами данных работали?"},
        {"role": "candidate", "text": "Работал с PostgreSQL."},
    ]
    client.send_message.assert_awaited_once_with(
        "target", "Работал с PostgreSQL.", parse_mode=None
    )


async def test_repeated_wait_on_question_pauses_instead_of_silent_stall(setup):
    app, client, store = setup
    app.mode = "auto"
    app.model.answer.return_value = {"action": "wait", "text": "", "reason": ""}
    await app.tick()
    assert app.current["last_action"] == "needs_input"
    assert app.current["blocked_reason"]
    assert not app.active
    assert app.model.answer.await_count == 2
    assert app.current["answers"] == 0
    assert all(call.args[0] == "me" for call in client.send_message.call_args_list)


async def test_wait_on_notification_does_not_regenerate(setup):
    app, client, store = setup
    client.get_messages.return_value = [
        message(text="Проверяю информацию, пожалуйста подождите.")
    ]
    app.model.answer.return_value = {"action": "wait", "text": "", "reason": ""}
    await app.tick()
    assert app.current["last_action"] == "wait"
    assert app.active
    assert app.model.answer.await_count == 1
    assert app.current["answers"] == 0


async def test_start_ignores_unchanged_latest_card(setup):
    app, client, store = setup
    store.current = None
    store.jobs[0].update(status="queued", url="https://rabota.sber.ru/search/123")
    card, button = linked_card(20, "1. Data Analyst", 123)
    store.jobs[0].update(
        message_id=20, button=button.text, source_hash=fingerprint(card)
    )

    async def read(target, *, ids=None, limit=None, min_id=0):
        if ids is not None:
            return card
        return [card] if card.id > min_id else []

    client.get_messages.side_effect = read
    await app.start_next()
    await app.tick()
    assert app.active
    assert app.current["history"] == []
    assert app.current["answers"] == 0
    app.model.answer.assert_not_awaited()


async def test_second_invitation_for_candidate_questions_closes_without_repeating(
    setup,
):
    app, client, store = setup
    app.mode = "auto"
    app.current["history"] = [
        {"role": "recruiter", "text": "Остались ли у Вас какие-либо вопросы ко мне?"},
        {"role": "candidate", "text": "Возможен ли гибридный формат?"},
    ]
    client.get_messages.return_value = [
        message(text="Доступен офисный формат. Остались ли у Вас другие вопросы?")
    ]
    app.model.answer.return_value = {
        "action": "reply",
        "text": "Возможен ли гибридный формат?",
        "reason": "",
    }
    await app.tick()
    assert app.current["history"][-1] == {
        "role": "candidate",
        "text": "Пока других вопросов нет, спасибо.",
    }
    assert app.current["answers"] == 1
    app.model.answer.assert_not_awaited()


@pytest.mark.parametrize("mode", ["draft", "auto"])
async def test_first_invitation_closes_in_auto_and_keeps_question_as_draft(setup, mode):
    app, client, store = setup
    app.mode = mode
    client.get_messages.return_value = [
        message(text="Остались ли у Вас какие-либо вопросы ко мне?")
    ]
    app.model.answer.return_value = {
        "action": "reply",
        "text": "Какие задачи будут в первые месяцы?",
        "reason": "",
    }
    await app.tick()
    if mode == "auto":
        assert (
            app.current["history"][-1]["text"] == "Пока других вопросов нет, спасибо."
        )
        assert app.current["answers"] == 1
        app.model.answer.assert_not_awaited()
    else:
        assert app.current["pending"] == "Какие задачи будут в первые месяцы?"
        assert app.current["answers"] == 0


async def test_missing_first_card_does_not_block_available_next_job(setup):
    app, client, store = setup
    store.current = None
    store.jobs[0].update(status="queued", url="https://rabota.sber.ru/search/123")
    missing, wrong_button = linked_card(1, "1. Other vacancy", 999)
    fresh, next_button = linked_card(20, "2. ML Engineer", 456)
    await store.add(
        {
            "message_id": 20,
            "button": next_button.text,
            "url": "https://rabota.sber.ru/search/456",
            "text": "Python " * 50,
            "source_hash": fingerprint(fresh),
        }
    )

    async def read(target, *, ids=None, limit=None, min_id=0):
        if ids is not None:
            return {1: missing, 20: fresh}.get(ids)
        return [fresh, missing][:limit]

    client.get_messages.side_effect = read
    await app.start_next()
    restored = await reload_store(store)
    assert restored.jobs[0]["status"] == "queued"
    assert restored.jobs[0]["key"] == "1:Data Analyst"
    assert restored.jobs[1]["status"] == "active"
    assert restored.current["job"] == "20:2. ML Engineer"
    wrong_button.click.assert_not_awaited()
    next_button.click.assert_awaited_once()
    assert app.active


@pytest.mark.parametrize("mode", ["draft", "auto"])
async def test_all_cards_unavailable_pauses_without_completing_queue(setup, mode):
    app, client, store = setup
    app.mode = mode
    store.current = None
    store.jobs[0].update(status="queued", url="https://rabota.sber.ru/search/123")
    missing, button = linked_card(1, "1. Other vacancy", 999)

    async def read(target, *, ids=None, limit=None, min_id=0):
        return missing if ids is not None else [missing]

    client.get_messages.side_effect = read
    await app.start_next()
    assert not app.active
    assert app.current is None
    assert store.jobs[0]["status"] == "queued"
    assert any(
        "Нет доступных карточек" in call.args[1]
        for call in client.send_message.call_args_list
    )
    assert all(
        "Очередь завершена" not in call.args[1]
        for call in client.send_message.call_args_list
    )
    button.click.assert_not_awaited()


async def test_auto_queue_opens_menu_once_then_pages_to_available_vacancy(setup):
    app, client, store = setup
    app.mode = "auto"
    store.current = None
    job = store.jobs[0]
    job.update(status="queued", url="https://rabota.sber.ru/search/123")
    old, _ = linked_card(1, "1. Other vacancy", 999)
    page, _ = linked_card(20, "1. Other vacancy", 999)
    more = SimpleNamespace(text="Далее", click=AsyncMock())
    more.button = KeyboardButton("Далее", InlineButtonTypeCallback(b"next"))
    page.buttons.append([more])
    fresh, choose = linked_card(30, "2. Data Analyst", 123)
    visible = [message(10, "Спасибо за обратную связь!")]

    async def read(target, *, ids=None, limit=None, min_id=0):
        return old if ids is not None else visible[:limit]

    client.get_messages.side_effect = read
    await app.start_next()
    await app.start_next()
    assert app.active and app.current is None
    assert (
        sum(
            c.args[:2] == ("target", "/change_vacancy")
            for c in client.send_message.call_args_list
        )
        == 1
    )
    visible[:] = [page]
    await app.start_next()
    await app.start_next()
    more.click.assert_awaited_once()
    assert app.current is None
    visible[:] = [fresh]
    await app.start_next()
    choose.click.assert_awaited_once()
    assert app.current["job"] == job["key"]
    assert job["status"] == "active"


async def test_auto_menu_wait_times_out_without_repeating_command(setup):
    app, client, store = setup
    app.mode = "auto"
    app.config.response_timeout = 0.001
    store.current = None
    store.jobs[0]["status"] = "queued"
    client.get_messages.return_value = [message(10, "Спасибо за обратную связь!")]
    await app.start_next()
    await asyncio.sleep(0.01)
    with pytest.raises(ValueError, match="список вакансий"):
        await app.start_next()
    assert (
        sum(
            c.args[:2] == ("target", "/change_vacancy")
            for c in client.send_message.call_args_list
        )
        == 1
    )
    assert store.jobs[0]["status"] == "queued"


async def test_auto_pagination_stops_on_repeated_page_content(setup):
    app, client, store = setup
    app.mode = "auto"
    store.current = None
    store.jobs[0].update(status="queued", url="https://rabota.sber.ru/search/123")
    page, _ = linked_card(20, "1. Other vacancy", 999)
    more = SimpleNamespace(
        text="Далее",
        button=KeyboardButton("Далее", InlineButtonTypeCallback(b"next")),
        click=AsyncMock(),
    )
    page.buttons.append([more])
    visible = [page]

    async def read(target, *, ids=None, limit=None, min_id=0):
        return page if ids is not None else visible[:limit]

    client.get_messages.side_effect = read
    await app.start_next()
    repeated = SimpleNamespace(**{**vars(page), "id": 21})
    visible[:] = [repeated]
    await app.start_next()
    more.click.assert_awaited_once()
    assert not app.active
    assert store.jobs[0]["status"] == "queued"


@pytest.mark.parametrize(
    "error", [OSError("network unavailable"), ValueError("invalid peer")]
)
async def test_read_failure_is_not_treated_as_unavailable_card(setup, error):
    app, client, store = setup
    store.current = None
    store.jobs[0]["status"] = "queued"
    client.get_messages.side_effect = error
    with pytest.raises(type(error)):
        await app.start_next()
    assert app.current is None
    assert store.jobs[0]["status"] == "queued"


async def test_unchanged_historical_card_is_not_clicked_when_new_menu_exists(setup):
    app, client, store = setup
    store.current = None
    store.jobs[0].update(status="queued", url="https://rabota.sber.ru/search/123")
    old, old_button = linked_card(1, "1. Data Analyst", 123)
    store.jobs[0].update(button=old_button.text, source_hash=fingerprint(old))
    current, _ = linked_card(30, "2. Another vacancy", 999)

    async def read(target, *, ids=None, limit=None, min_id=0):
        return old if ids is not None else [current, old][:limit]

    client.get_messages.side_effect = read
    await app.start_next()
    assert app.current is None
    assert store.jobs[0]["status"] == "queued"
    old_button.click.assert_not_awaited()


async def test_fresh_menu_replaces_unchanged_historical_card(setup):
    app, client, store = setup
    store.current = None
    store.jobs[0].update(status="queued", url="https://rabota.sber.ru/search/123")
    old, old_button = linked_card(1, "1. Data Analyst", 123)
    store.jobs[0].update(button=old_button.text, source_hash=fingerprint(old))
    fresh, fresh_button = linked_card(30, "3. Data Analyst", 123)

    async def read(target, *, ids=None, limit=None, min_id=0):
        return old if ids is not None else [fresh, old][:limit]

    client.get_messages.side_effect = read
    await app.start_next()
    assert store.current["job"] == store.jobs[0]["key"]
    assert store.jobs[0]["message_id"] == 30
    old_button.click.assert_not_awaited()
    fresh_button.click.assert_awaited_once()


async def test_recruiter_silence_emits_diagnostic_instead_of_waiting_forever(setup):
    app, client, store = setup
    app.current["last_progress_at"] = (
        datetime.now(UTC) - timedelta(minutes=10)
    ).timestamp()
    client.get_messages.return_value = []
    with pytest.raises(ValueError, match="не прислал"):
        await app.tick()
    assert app.current["answers"] == 0
    assert app.current["phase"] == "interview"
    app.model.answer.assert_not_awaited()
    client.send_message.assert_not_awaited()


async def test_started_interview_waits_for_late_reply_without_repeated_notices(setup):
    app, client, store = setup
    app.mode = "auto"
    app.current["answers"] = 1
    app.current["last_progress_at"] = (
        datetime.now(UTC) - timedelta(minutes=10)
    ).timestamp()
    client.get_messages.return_value = []
    await app.tick()
    await app.tick()
    assert app.active
    assert app.current["answers"] == 1
    client.send_message.assert_awaited_once()
    assert "продолжает ждать" in client.send_message.call_args.args[1]
    app.store = await reload_store(store)
    await app.tick()
    assert client.send_message.await_count == 1
    client.get_messages.return_value = [message(20)]
    await app.tick()
    assert app.active
    assert app.current["answers"] == 2
    assert client.send_message.call_args.args[:2] == (
        "target",
        "Интересны задачи с SQL.",
    )


async def test_resume_restarts_silence_timer_without_reclicking(setup):
    app, client, store = setup
    before = datetime.now(UTC).timestamp()
    app.current["last_progress_at"] = before - 10000
    client.get_messages.return_value = []
    await app.resume()
    await app.tick()
    assert app.active
    assert app.current["last_progress_at"] >= before
    assert app.current["answers"] == 0
    client.send_message.assert_not_awaited()


async def test_silence_timeout_is_reported_by_scheduler(setup, tmp_path):
    from src.core.telemetry import Telemetry
    from src.runtime.interview import InterviewScheduler

    app, client, store = setup
    app.current["last_progress_at"] = (
        datetime.now(UTC) - timedelta(minutes=10)
    ).timestamp()
    client.get_messages.return_value = []
    telemetry = Telemetry(tmp_path / "diagnostics")
    try:
        await InterviewScheduler(app, telemetry).step()
        assert not app.active
        assert telemetry.counters["errors.interview.failed"] == 1
        import json

        report = json.loads(telemetry.export().read_text())
        assert report["incidents"][-1]["code"] == "recruiter_timeout"
        assert "не прислал" in client.send_message.call_args.args[1]
        assert "/diagnostics" in client.send_message.call_args.args[1]
    finally:
        telemetry.close()


async def test_recover_not_started_requeues_without_marking_skipped(setup):
    app, client, store = setup
    await commands(app).execute("/recover not-started")
    restored = await reload_store(store)
    assert restored.current is None
    assert restored.jobs[0]["status"] == "queued"
    assert not app.active
    client.send_message.assert_not_awaited()


async def test_recover_not_started_rejects_dialog_with_answers(setup):
    app, client, store = setup
    app.current["answers"] = 1
    app.current["history"] = [{"role": "candidate", "text": "Ответ"}]
    with pytest.raises(ValueError):
        await commands(app).execute("/recover not-started")
    assert app.current["answers"] == 1
    assert store.jobs[0]["status"] == "active"


async def test_next_command_turns_current_menu_once_while_waiting_for_update(setup):
    from src.services.queue import QueueService

    app, client, store = setup
    store.current = None
    nxt = SimpleNamespace(
        text="Далее",
        button=KeyboardButton("Далее", InlineButtonTypeCallback(b"next")),
        click=AsyncMock(return_value=None),
    )
    menu = message(40, "По какой из них вы хотели бы продолжить диалог?", [[nxt]])
    client.get_messages.return_value = [menu]
    queue = QueueService(app.telegram, store, SimpleNamespace(hosts={"rabota.sber.ru"}))
    command = ChatCommandService(app, queue)
    first = await command.execute("/next")
    second = await command.execute("/next")
    assert "запрошен" in first
    assert "ожидается" in second
    nxt.click.assert_awaited_once()
    assert store.current is None


async def test_next_command_rejects_navigation_during_interview(setup):
    app, client, store = setup
    with pytest.raises(ValueError, match="интервью"):
        await commands(app).execute("/next")
    assert store.current is not None


@pytest.mark.parametrize("include_old_menu", [False, True])
async def test_historical_card_is_rejected_when_latest_bot_message_is_not_menu(
    setup, include_old_menu
):
    app, client, store = setup
    store.current = None
    store.jobs[0].update(status="queued", url="https://rabota.sber.ru/search/123")
    old, button = linked_card(1, "1. Data Analyst", 123)
    store.jobs[0].update(button=button.text, source_hash=fingerprint(old))
    current = message(50, "Остались ли у Вас вопросы?")

    async def read(target, *, ids=None, limit=None, min_id=0):
        if ids is not None:
            return old
        return [current, old] if include_old_menu else [current]

    client.get_messages.side_effect = read
    await app.start_next()
    assert store.current is None
    assert store.jobs[0]["status"] == "queued"
    button.click.assert_not_awaited()


async def test_model_done_on_closing_question_sends_answer_instead_of_finishing(setup):
    app, client, store = setup
    app.mode = "auto"
    client.get_messages.return_value = [
        message(text="Остались ли у Вас какие-либо вопросы ко мне?")
    ]
    app.model.answer.return_value = {
        "action": "done",
        "text": "",
        "reason": "Ошибочная классификация",
    }
    await app.tick()
    assert store.jobs[0]["status"] == "active"
    assert app.current is not None
    assert app.current["answers"] == 1
    assert app.current["history"][-1] == {
        "role": "candidate",
        "text": "Пока других вопросов нет, спасибо.",
    }


async def test_recover_unfinished_restores_false_completion_without_resending(setup):
    app, client, store = setup
    store.current = None
    job = store.jobs[0]
    job.update(
        status="done",
        url="https://rabota.sber.ru/search/123",
        answers=1,
        finished_at="2026-11-07T00:00:00+00:00",
        history=[
            {"role": "recruiter", "text": "Почему вакансия интересна?"},
            {"role": "candidate", "text": "Интересны задачи с SQL."},
            {
                "role": "recruiter",
                "text": "Остались ли у Вас какие-либо вопросы ко мне?",
            },
        ],
    )
    previous = message(10, "Почему вакансия интересна?")
    answer = message(11, "Интересны задачи с SQL.")
    answer.out = True
    question = message(12, "Остались ли у Вас какие-либо вопросы ко мне?")
    client.get_messages.return_value = [question, answer, previous]
    await commands(app).execute("/recover unfinished https://rabota.sber.ru/search/123")
    assert store.current["phase"] == "interview"
    assert store.current["answers"] == 1
    assert store.current["history"] == job["history"][:-1]
    assert fingerprint(question) not in store.current["processed"]
    assert fingerprint(previous) in store.current["processed"]
    assert job["status"] == "active"
    assert "finished_at" not in job
    client.send_message.assert_not_awaited()
    restored = await reload_store(store)
    assert restored.current["job"] == job["key"]


async def test_recover_unfinished_rejects_confirmed_completion(setup):
    app, client, store = setup
    store.current = None
    store.jobs[0].update(
        status="done",
        url="https://rabota.sber.ru/search/123",
        rating=5,
        history=[{"role": "recruiter", "text": "Спасибо за интервью!"}],
    )
    with pytest.raises(ValueError):
        await commands(app).execute(
            "/recover unfinished https://rabota.sber.ru/search/123"
        )
    assert store.current is None
    assert store.jobs[0]["status"] == "done"


async def test_recover_unfinished_rejects_question_already_answered_in_telegram(setup):
    app, client, store = setup
    store.current = None
    job = store.jobs[0]
    job.update(
        status="done",
        url="https://rabota.sber.ru/search/123",
        history=[{"role": "recruiter", "text": "Остались ли у Вас другие вопросы?"}],
    )
    q = message(12, "Остались ли у Вас другие вопросы?")
    a = message(13, "Нет, спасибо.")
    a.out = True
    client.get_messages.return_value = [a, q]
    with pytest.raises(ValueError, match="ответ"):
        await commands(app).execute(
            "/recover unfinished https://rabota.sber.ru/search/123"
        )
    assert store.current is None
    assert job["status"] == "done"

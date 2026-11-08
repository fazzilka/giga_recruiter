import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest
from sqlalchemy import select
from telethon import TelegramClient

from src.config.config import Config, InterviewConfig
from src.core.db.db import Database
from src.core.db.models.workflow import JobRecord
from src.core.db.repositories.workflow import WorkflowRepository
from src.runtime.bootstrap import process_lock
from src.runtime.containers import make_app_container
from src.runtime.interview import InterviewScheduler
from src.services.interview import InterviewService
from src.services.materials import MaterialService
from src.services.queue import QueueService
from src.services.vacancies import VacancyService
from src.userbot.handlers.commands import UserbotCommandHandlers
from src.userbot.handlers.recruiter import RecruiterHandlers
from src.userbot.services.commands import ChatCommandService


@pytest.fixture
async def database(tmp_path):
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'test.sqlite3'}")
    await db.initialize()
    try:
        yield db
    finally:
        await db.close()


async def test_di_container_builds_real_services_and_closes_resources(tmp_path):
    config = Config(root=tmp_path)
    config.interview.send_delay = 0
    config.interview.feedback_wait = 0
    config.state_dir.mkdir()
    (tmp_path / "materials").mkdir()
    container = make_app_container(config, Mock(spec=TelegramClient))
    service = await container.get(InterviewService)
    commands = await container.get(ChatCommandService)
    queue = await container.get(QueueService)
    scheduler = await container.get(InterviewScheduler)
    handler = await container.get(UserbotCommandHandlers)
    assert commands.interview is service
    assert queue.store is service.store
    assert scheduler.service is service
    assert handler.commands is commands
    assert await container.get(RecruiterHandlers)
    http = await container.get(httpx.AsyncClient)
    assert not http.is_closed
    assert not service.active
    assert service.mode == "draft"
    await container.close()
    assert http.is_closed


async def test_database_atomic_rollback(database):
    with pytest.raises(RuntimeError):
        async with database.uow() as uow:
            uow.session.add(
                JobRecord(key="one", position=0, payload={"status": "queued"})
            )
            await uow.session.flush()
            raise RuntimeError("rollback")
    async with database.uow() as uow:
        assert list(await uow.session.scalars(select(JobRecord))) == []


async def test_legacy_queue_import_is_idempotent_and_preserves_source(
    database, tmp_path
):
    original = {
        "jobs": [{"key": "1:Analyst", "status": "active"}],
        "current": {"phase": "sending", "pending": "Ответ"},
    }
    legacy = tmp_path / "queue.json"
    legacy.write_text(json.dumps(original))
    repository = WorkflowRepository(database)
    await repository.load(legacy)
    assert repository.current["phase"] == "sending"
    repository.jobs[0]["status"] = "done"
    repository.current = None
    await repository.save()
    again = WorkflowRepository(database)
    await again.load(legacy)
    assert again.jobs[0]["status"] == "done"
    assert again.current is None
    assert json.loads(legacy.read_text()) == original


def test_material_cache_reuses_extraction_and_invalidates_on_changes(tmp_path):
    file = tmp_path / "resume.txt"
    file.write_text("SQL")
    service = MaterialService(tmp_path)
    from src.services.materials import load_materials

    with patch(
        "src.services.materials.load_materials", wraps=load_materials
    ) as extract:
        assert "SQL" in service.read()
        for _ in range(20):
            service.read()
        assert extract.call_count == 1
        file.write_text("SQL и Python")
        assert "Python" in service.read()
        assert extract.call_count == 2
        file.unlink()
        with pytest.raises(ValueError):
            service.read()


async def test_vacancy_cache_uses_one_request_and_reuses_http_client():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text="<main>" + "SQL " * 100 + "</main>",
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        service = VacancyService(http, {"rabota.sber.ru"})
        first, second = await asyncio.gather(
            service.text("https://rabota.sber.ru/1"),
            service.text("https://rabota.sber.ru/1"),
        )
        assert first == second
        assert len(requests) == 1
        assert not http.is_closed


def scheduler_service(active=False, interval=10):
    return SimpleNamespace(
        active=active,
        wakeup=asyncio.Event(),
        busy=asyncio.Lock(),
        config=InterviewConfig(debounce=0, reconciliation_interval=interval),
        tick=AsyncMock(),
        pause=Mock(),
        telegram=SimpleNamespace(notify=AsyncMock()),
        wake=lambda: None,
    )


async def test_scheduler_paused_does_not_poll():
    service = scheduler_service()
    scheduler = InterviewScheduler(service)
    task = asyncio.create_task(scheduler.run())
    await asyncio.sleep(0.03)
    service.tick.assert_not_awaited()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def test_event_wakes_scheduler_without_waiting_for_reconciliation():
    service = scheduler_service(active=True)
    service.wake = service.wakeup.set
    scheduler = InterviewScheduler(service)
    completed = asyncio.Event()
    service.tick.side_effect = completed.set
    task = asyncio.create_task(scheduler.run())
    scheduler.incoming()
    await asyncio.wait_for(completed.wait(), 0.5)
    service.tick.assert_awaited_once()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def test_scheduler_fallback_reconciles_missed_updates():
    service = scheduler_service(active=True, interval=0.01)
    completed = asyncio.Event()
    service.tick.side_effect = completed.set
    task = asyncio.create_task(InterviewScheduler(service).run())
    await asyncio.wait_for(completed.wait(), 0.5)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def test_scheduler_coalesces_message_burst():
    service = scheduler_service(active=True)
    service.config.debounce = 0.03
    service.wake = service.wakeup.set
    scheduler = InterviewScheduler(service)
    completed = asyncio.Event()
    service.tick.side_effect = completed.set
    task = asyncio.create_task(scheduler.run())
    scheduler.incoming()
    await asyncio.sleep(0.01)
    scheduler.incoming()
    await asyncio.wait_for(completed.wait(), 0.5)
    service.tick.assert_awaited_once()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


def test_config_env_overrides_toml_and_redacts_secrets(tmp_path, monkeypatch):
    (tmp_path / "config.toml").write_text(
        "[codex]\ntimeout = 50\n[interview]\ndebounce = 0.2\n"
    )
    (tmp_path / ".env").write_text(
        "CODEX_TIMEOUT=100\nTELEGRAM_API_HASH=not-a-real-secret\n"
    )
    monkeypatch.setenv("CODEX_TIMEOUT", "150")
    config = Config.load(tmp_path)
    assert config.codex.timeout == 150
    assert config.interview.debounce == 0.2
    assert "not-a-real-secret" not in repr(config)


def test_single_process_lock(tmp_path):
    with process_lock(tmp_path):
        with pytest.raises(SystemExit, match="уже запущена"):
            with process_lock(tmp_path):
                pass


async def test_router_registers_only_owner_commands_and_target_events():
    client = Mock()
    command_handler = UserbotCommandHandlers(Mock(), Mock())
    recruiter = RecruiterHandlers(Mock())
    await command_handler.attach(client, owner_id=42)
    await recruiter.attach(client, target="Giga_recruiter_bot")
    assert client.add_event_handler.call_count == 3
    owner_filter = client.add_event_handler.call_args_list[0].args[1]
    assert owner_filter.chats == 42
    assert owner_filter.outgoing
    assert all(
        call.args[1].incoming for call in client.add_event_handler.call_args_list[1:]
    )


async def test_runtime_registers_handlers_and_shuts_down_cleanly(tmp_path):
    from src.runtime.app import serve

    config = Config(root=tmp_path)
    config.interview.send_delay = 0
    config.interview.feedback_wait = 0
    config.state_dir.mkdir()
    (tmp_path / "materials").mkdir()
    (tmp_path / "materials" / "resume.txt").write_text("SQL")

    async def disconnected():
        await asyncio.sleep(0.02)

    client = SimpleNamespace(
        get_entity=AsyncMock(return_value=SimpleNamespace(bot=True)),
        get_me=AsyncMock(return_value=SimpleNamespace(id=42)),
        send_message=AsyncMock(),
        add_event_handler=Mock(),
        run_until_disconnected=disconnected,
    )
    await serve(config, client)
    assert client.add_event_handler.call_count == 3
    client.send_message.assert_awaited_once()
    assert not [
        task
        for task in asyncio.all_tasks()
        if task.get_name() in {"interview-scheduler", "telegram-connection"}
    ]


async def test_full_two_vacancy_workflow_with_real_di_and_database(tmp_path):
    from telethon.tl.types import InlineButtonTypeCallback, KeyboardButton

    from src.integrations.codex import CodexClient
    from src.userbot.utils.messages import fingerprint

    config = Config(root=tmp_path)
    config.interview.send_delay = 0
    config.interview.feedback_wait = 0
    config.state_dir.mkdir()
    (tmp_path / "materials").mkdir()
    (tmp_path / "materials" / "resume.txt").write_text("Подтверждённый опыт SQL")
    cards = {}
    for mid in (1, 200):
        button = SimpleNamespace(
            text=f"Analyst {mid}",
            button=KeyboardButton(
                f"Analyst {mid}", InlineButtonTypeCallback(bytes([mid]))
            ),
            click=AsyncMock(),
        )
        cards[mid] = SimpleNamespace(
            id=mid, raw_text=f"Карточка {mid}", out=False, buttons=[[button]]
        )
    incoming = []

    async def get_messages(target, *, ids=None, limit=30, min_id=0):
        if ids is not None:
            return cards[ids]
        return [m for m in reversed(incoming) if m.id > min_id][:limit]

    client = SimpleNamespace(
        get_messages=AsyncMock(side_effect=get_messages), send_message=AsyncMock()
    )
    container = make_app_container(config, client)
    try:
        store = await container.get(WorkflowRepository)
        queue = await container.get(QueueService)
        commands = await container.get(ChatCommandService)
        service = await container.get(InterviewService)
        model = await container.get(CodexClient)
        model.answer = AsyncMock()
        for mid in (1, 200):
            await queue.add(mid, f"Analyst {mid}", "SQL аналитик " * 30, raw=True)
        await commands.execute("/mode auto")
        await commands.execute("/run")
        for mid in (1, 200):
            incoming.clear()
            incoming.append(cards[mid])
            await service.tick()  # Click exactly the saved vacancy button.
            cards[mid].buttons[0][0].click.assert_awaited_once()
            question = SimpleNamespace(
                id=mid + 100,
                raw_text="Чем интересна вакансия?",
                buttons=None,
                out=False,
            )
            incoming.append(question)
            model.answer.return_value = {
                "action": "reply",
                "text": "Интересны задачи с SQL.",
                "reason": "",
            }
            await service.tick()
            assert fingerprint(question) in service.current["processed"]
            incoming.append(
                SimpleNamespace(
                    id=mid + 101,
                    raw_text="Интервью завершено.",
                    buttons=None,
                    out=False,
                )
            )
            model.answer.return_value = {"action": "done", "text": "", "reason": ""}
            await service.tick()
            assert service.current is None
        await service.tick()
        assert not service.active
        assert all(job["status"] == "done" for job in store.jobs)
        replies = [
            call for call in client.send_message.call_args_list if call.args[0] != "me"
        ]
        assert len(replies) == 2
        restored = WorkflowRepository(store.database)
        await restored.load()
        assert all(job["status"] == "done" for job in restored.jobs)
        assert (
            model.answer.await_args_list[0].args[0].endswith("Подтверждённый опыт SQL")
        )
    finally:
        await container.close()


async def test_parallel_downloads_are_bounded_to_three():
    active = 0
    peak = 0

    async def respond(request):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text="<main>" + "SQL " * 100 + "</main>",
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        service = VacancyService(http, {"rabota.sber.ru"})
        await asyncio.gather(
            *(service.text(f"https://rabota.sber.ru/{index}") for index in range(8))
        )
    assert peak == 3


async def test_scan_preserves_order_and_does_not_refetch_cards(database, monkeypatch):
    from telethon.tl.types import InlineButtonTypeCallback, KeyboardButton

    store = WorkflowRepository(database)
    await store.load()
    cards = []
    for mid in (1, 2):
        button = SimpleNamespace(
            text=f"Analyst {mid}",
            button=KeyboardButton(
                f"Analyst {mid}", InlineButtonTypeCallback(bytes([mid]))
            ),
        )
        cards.append(
            SimpleNamespace(
                id=mid, raw_text=f"Карточка {mid}", buttons=[[button]], out=False
            )
        )
    telegram = SimpleNamespace(
        recent=AsyncMock(return_value=list(reversed(cards))), message=AsyncMock()
    )
    monkeypatch.setattr(
        "src.services.queue.links", lambda m: [f"https://rabota.sber.ru/{m.id}"]
    )

    async def text(url):
        # The second page finishes first; the first vacancy must remain first.
        await asyncio.sleep(0.02 if url.endswith("/1") else 0)
        return "SQL " * 100

    vacancies = SimpleNamespace(
        hosts={"rabota.sber.ru"}, text=AsyncMock(side_effect=text)
    )
    queue = QueueService(telegram, store, vacancies)
    await queue.scan()
    assert [job["message_id"] for job in store.jobs] == [1, 2]
    telegram.message.assert_not_awaited()
    await queue.scan()
    assert vacancies.text.await_count == 2

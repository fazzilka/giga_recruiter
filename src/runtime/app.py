"""Composition and lifecycle only; no commands or interview decisions."""

import argparse
import asyncio
import os
import shutil

from src.config import Config
from src.core.logger import setup_logging
from src.core.telemetry import Telemetry
from src.runtime.bootstrap import process_lock, run_async_entrypoint
from src.runtime.containers import make_app_container
from src.runtime.interview import InterviewScheduler
from src.services.materials import MaterialService
from src.userbot.client import create_userbot_client
from src.userbot.handlers.commands import UserbotCommandHandlers
from src.userbot.handlers.recruiter import RecruiterHandlers
from src.userbot.services.telegram import TelegramGateway


async def serve(config: Config, client, telemetry: Telemetry | None = None) -> None:
    target = await client.get_entity(config.userbot.target)
    if not target.bot:
        raise ValueError("TARGET_BOT должен быть ботом.")
    owner = await client.get_me()
    container = make_app_container(config, client, telemetry)
    try:
        telegram = await container.get(TelegramGateway)
        telegram.target = target
        await (await container.get(UserbotCommandHandlers)).attach(client, owner.id)
        await (await container.get(RecruiterHandlers)).attach(client, target)
        scheduler = await container.get(InterviewScheduler)
        await telegram.notify("Пульт запущен на паузе, режим draft. /help")
        worker = asyncio.create_task(scheduler.run(), name="interview-scheduler")
        connection = asyncio.create_task(
            client.run_until_disconnected(), name="telegram-connection"
        )
        try:
            done, _ = await asyncio.wait(
                {worker, connection}, return_when=asyncio.FIRST_COMPLETED
            )
            for task in done:
                task.result()
        finally:
            worker.cancel()
            connection.cancel()
            await asyncio.gather(worker, connection, return_exceptions=True)
    finally:
        await container.close()


async def _main(
    action: str, config: Config, telemetry: Telemetry | None = None
) -> None:
    if action == "doctor":
        print(
            "Codex CLI:", "найден" if shutil.which(config.codex.binary) else "не найден"
        )
        print(
            "Telegram credentials:",
            "заполнены"
            if config.userbot.api_id and config.userbot.api_hash.get_secret_value()
            else "не заполнены",
        )
        try:
            print(
                "Материалы:",
                len(await MaterialService(config.root / "materials").get()),
                "символов",
            )
        except ValueError as exc:
            print(exc)
        return
    if not config.userbot.api_id or not config.userbot.api_hash.get_secret_value():
        raise SystemExit(
            "Заполни TELEGRAM_API_ID и TELEGRAM_API_HASH в .env (см. README.md)."
        )
    with process_lock(config.state_dir):
        async with create_userbot_client(config) as client:
            os.chmod(config.state_dir / "telegram.session", 0o600)
            if action == "login":
                print("Telegram подключён. Теперь: uv run python -m src run")
            else:
                await serve(config, client, telemetry)


async def main(action: str, config: Config) -> None:
    setup_logging()
    if action == "doctor":
        return await _main(action, config)
    config.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    config.state_dir.chmod(0o700)
    telemetry = Telemetry(config.state_dir / "diagnostics")
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()

    def background_error(loop, context):
        telemetry.error(
            "runtime.background", context.get("exception") or RuntimeError()
        )

    loop.set_exception_handler(background_error)
    try:
        if action == "diagnostics":
            print(telemetry.export())
            return
        with telemetry.operation("runtime"):
            try:
                await _main(action, config, telemetry)
            except asyncio.CancelledError:
                raise
            except BaseException as exc:
                telemetry.error("runtime.failed", exc, phase="idle")
                raise
    finally:
        loop.set_exception_handler(previous_handler)
        try:
            telemetry.export()
        except OSError:
            pass
        telemetry.close()


def run() -> None:
    setup_logging()
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "action",
        nargs="?",
        default="run",
        choices=["login", "run", "doctor", "diagnostics"],
    )
    action = parser.parse_args().action
    config = Config.load()
    run_async_entrypoint(lambda: main(action, config))

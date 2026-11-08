from dishka import make_async_container
from telethon import TelegramClient

from src.config import Config
from src.core.di.providers import InfrastructureProvider, ServicesProvider
from src.core.telemetry import Telemetry


def make_app_container(
    config: Config, client: TelegramClient, telemetry: Telemetry | None = None
):
    config.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    config.state_dir.chmod(0o700)
    owned = telemetry is None
    telemetry = telemetry or Telemetry(config.state_dir / "diagnostics")
    return make_async_container(
        InfrastructureProvider(telemetry, owned),
        ServicesProvider(),
        context={Config: config, TelegramClient: client},
    )

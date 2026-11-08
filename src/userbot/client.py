from telethon import TelegramClient

from src.config.config import Config


def create_userbot_client(config: Config) -> TelegramClient:
    return TelegramClient(
        str(config.state_dir / "telegram"),
        config.userbot.api_id,
        config.userbot.api_hash.get_secret_value(),
        device_model="giga-recruiter",
        receive_updates=True,
        flood_sleep_threshold=0,
    )

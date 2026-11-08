"""Validated configuration; existing .env names remain supported."""

import os
import tomllib
from pathlib import Path

from dotenv import dotenv_values
from pydantic import BaseModel, Field, SecretStr

BASE_DIR = Path(__file__).resolve().parents[2]


class UserbotConfig(BaseModel):
    api_id: int = Field(default=0, ge=0)
    api_hash: SecretStr = SecretStr("")
    target: str = "Giga_recruiter_bot"


class CodexConfig(BaseModel):
    binary: str = "codex"
    model: str = ""
    timeout: float = Field(default=180, gt=0)


class InterviewConfig(BaseModel):
    max_answers: int = Field(default=30, ge=1)
    debounce: float = Field(default=0.6, ge=0)
    send_delay: float = Field(default=30, ge=0)
    feedback_wait: float = Field(default=2, ge=0)
    reconciliation_interval: float = Field(default=30, gt=0)
    response_timeout: float = Field(default=180, gt=0)


class Config(BaseModel):
    root: Path = BASE_DIR
    userbot: UserbotConfig = Field(default_factory=UserbotConfig)
    codex: CodexConfig = Field(default_factory=CodexConfig)
    interview: InterviewConfig = Field(default_factory=InterviewConfig)
    vacancy_hosts: set[str] = {"rabota.sber.ru"}

    @property
    def state_dir(self) -> Path:
        return self.root / "state"

    @property
    def database_url(self) -> str:
        return f"sqlite+aiosqlite:///{self.state_dir / 'recruiter.sqlite3'}"

    @classmethod
    def load(cls, root: Path = BASE_DIR) -> "Config":
        file = root / "config.toml"
        data = tomllib.loads(file.read_text()) if file.exists() else {}
        data["root"] = root
        env = {**dotenv_values(root / ".env"), **os.environ}
        mapping = {
            "TELEGRAM_API_ID": ("userbot", "api_id"),
            "TELEGRAM_API_HASH": ("userbot", "api_hash"),
            "TARGET_BOT": ("userbot", "target"),
            "CODEX_BIN": ("codex", "binary"),
            "CODEX_MODEL": ("codex", "model"),
            "CODEX_TIMEOUT": ("codex", "timeout"),
            "MAX_ANSWERS": ("interview", "max_answers"),
            "SEND_DELAY_SECONDS": ("interview", "send_delay"),
            "MESSAGE_DEBOUNCE": ("interview", "debounce"),
            "RECONCILIATION_INTERVAL": ("interview", "reconciliation_interval"),
            "RECRUITER_RESPONSE_TIMEOUT": ("interview", "response_timeout"),
        }
        for key, (section, field) in mapping.items():
            if env.get(key):
                data.setdefault(section, {})[field] = env[key]
        if env.get("VACANCY_HOSTS"):
            data["vacancy_hosts"] = {
                host.strip() for host in env["VACANCY_HOSTS"].split(",") if host.strip()
            }
        return cls.model_validate(data)

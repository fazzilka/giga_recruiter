from collections.abc import AsyncIterator

import httpx
from dishka import Provider, Scope, from_context, provide
from telethon import TelegramClient

from src.config.config import CodexConfig, Config, InterviewConfig
from src.core.db.db import Database
from src.core.db.repositories.workflow import WorkflowRepository
from src.core.telemetry import Telemetry
from src.integrations.codex import CodexClient
from src.runtime.interview import InterviewScheduler
from src.services.feedback import FeedbackService
from src.services.interview import InterviewService
from src.services.materials import MaterialService
from src.services.queue import QueueService
from src.services.vacancies import VacancyService
from src.userbot.handlers.commands import UserbotCommandHandlers
from src.userbot.handlers.recruiter import RecruiterHandlers
from src.userbot.services.commands import ChatCommandService
from src.userbot.services.telegram import TelegramGateway


class InfrastructureProvider(Provider):
    scope = Scope.APP
    config = from_context(provides=Config)
    client = from_context(provides=TelegramClient)

    def __init__(self, telemetry: Telemetry, owned: bool):
        super().__init__()
        self._telemetry, self._owns_telemetry = telemetry, owned

    @provide
    async def telemetry(self) -> AsyncIterator[Telemetry]:
        try:
            yield self._telemetry
        finally:
            if self._owns_telemetry:
                self._telemetry.close()

    @provide
    async def database(self, config: Config) -> AsyncIterator[Database]:
        database = Database(config.database_url)
        try:
            await database.initialize()
            yield database
        finally:
            await database.close()

    @provide
    async def workflow(self, database: Database, config: Config) -> WorkflowRepository:
        repository = WorkflowRepository(database)
        await repository.load(config.state_dir / "queue.json")
        return repository

    @provide
    async def http(self) -> AsyncIterator[httpx.AsyncClient]:
        async with httpx.AsyncClient(
            timeout=25,
            follow_redirects=False,
            trust_env=False,
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=4),
        ) as client:
            yield client

    @provide
    def materials(self, config: Config) -> MaterialService:
        return MaterialService(config.root / "materials")

    @provide
    def vacancies(
        self, client: httpx.AsyncClient, config: Config, telemetry: Telemetry
    ) -> VacancyService:
        return VacancyService(client, config.vacancy_hosts, telemetry)

    @provide
    def codex_config(self, config: Config) -> CodexConfig:
        return config.codex

    @provide
    def interview_config(self, config: Config) -> InterviewConfig:
        return config.interview


class ServicesProvider(Provider):
    scope = Scope.APP

    @provide
    def codex(self, config: CodexConfig, telemetry: Telemetry) -> CodexClient:
        return CodexClient(config, telemetry)

    @provide
    def telegram(
        self, client: TelegramClient, config: Config, telemetry: Telemetry
    ) -> TelegramGateway:
        return TelegramGateway(client, config, telemetry)

    feedback = provide(FeedbackService)

    @provide
    def interview(
        self,
        telegram: TelegramGateway,
        store: WorkflowRepository,
        model: CodexClient,
        materials: MaterialService,
        config: InterviewConfig,
        feedback: FeedbackService,
    ) -> InterviewService:
        return InterviewService(telegram, store, model, materials, config, feedback)

    queue = provide(QueueService)

    @provide
    def commands(
        self, interview: InterviewService, queue: QueueService, telemetry: Telemetry
    ) -> ChatCommandService:
        return ChatCommandService(interview, queue, telemetry)

    command_handlers = provide(UserbotCommandHandlers)
    recruiter_handlers = provide(RecruiterHandlers)

    @provide
    def scheduler(
        self, service: InterviewService, telemetry: Telemetry
    ) -> InterviewScheduler:
        return InterviewScheduler(service, telemetry)

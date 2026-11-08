"""Persistence boundary for the single-owner workflow aggregate."""

import copy
import json
from pathlib import Path

from src.core.db.db import Database
from src.core.vacancies import vacancy_identity


class WorkflowRepository:
    def __init__(self, database: Database):
        self.database = database
        self.jobs: list[dict] = []
        self.current: dict | None = None

    async def load(self, legacy_path: Path | None = None) -> None:
        async with self.database.uow() as uow:
            self.jobs = await uow.jobs.list()
            self.current = await uow.interviews.get()
        if (
            legacy_path
            and legacy_path.exists()
            and not self.jobs
            and self.current is None
        ):
            data = json.loads(legacy_path.read_text())
            if not isinstance(data.get("jobs"), list) or "current" not in data:
                raise ValueError("Некорректный старый queue.json; импорт остановлен.")
            self.jobs, self.current = data["jobs"], data["current"]
            await self.save()

    async def save(self) -> None:
        # Snapshot before awaiting: later mutations cannot change this transaction.
        jobs, current = copy.deepcopy((self.jobs, self.current))
        async with self.database.uow() as uow:
            await uow.jobs.save(jobs)
            await uow.interviews.save(current)
            await uow.commit()

    async def add(self, job: dict) -> bool:
        key = f"{job['message_id']}:{job['button']}"
        if any(
            item["key"] == key
            or (
                job.get("url")
                and item.get("url")
                and vacancy_identity(job["url"]) == vacancy_identity(item["url"])
            )
            for item in self.jobs
        ):
            return False
        self.jobs.append({**job, "key": key, "status": "queued"})
        await self.save()
        return True

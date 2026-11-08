from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.db.models.workflow import JobRecord


class JobRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def list(self) -> list[dict]:
        records = await self.session.scalars(
            select(JobRecord).order_by(JobRecord.position)
        )
        return [record.payload for record in records]

    async def save(self, jobs: list[dict]) -> None:
        records = await self.session.scalars(select(JobRecord))
        by_key = {record.key: record for record in records}
        for position, job in enumerate(jobs):
            existing = by_key.get(job["key"])
            if existing is None:
                self.session.add(
                    JobRecord(key=job["key"], position=position, payload=job)
                )
            elif existing.payload != job:
                existing.payload = job

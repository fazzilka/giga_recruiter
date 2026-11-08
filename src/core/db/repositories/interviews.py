from sqlalchemy.ext.asyncio import AsyncSession

from src.core.db.models.workflow import InterviewRecord


class InterviewRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def get(self) -> dict | None:
        record = await self.session.get(InterviewRecord, 1)
        return record.payload if record else None

    async def save(self, current: dict | None) -> None:
        record = await self.session.get(InterviewRecord, 1)
        if record is None:
            self.session.add(InterviewRecord(id=1, payload=current))
        elif record.payload != current:
            record.payload = current

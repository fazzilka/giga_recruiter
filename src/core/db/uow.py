from sqlalchemy.ext.asyncio import AsyncSession

from src.core.db.repositories.interviews import InterviewRepository
from src.core.db.repositories.jobs import JobRepository


class UnitOfWork:
    def __init__(self, session: AsyncSession):
        self.session = session
        self.jobs = JobRepository(session)
        self.interviews = InterviewRepository(session)

    async def __aenter__(self) -> "UnitOfWork":
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        try:
            if exc_type:
                await self.session.rollback()
        finally:
            await self.session.close()

    async def commit(self) -> None:
        await self.session.commit()

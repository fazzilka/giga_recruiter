from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.core.db.models.workflow import Base
from src.core.db.uow import UnitOfWork


class Database:
    def __init__(self, url: str):
        self.engine = create_async_engine(url)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    async def initialize(self) -> None:
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    def uow(self) -> UnitOfWork:
        return UnitOfWork(self.sessions())

    async def close(self) -> None:
        await self.engine.dispose()

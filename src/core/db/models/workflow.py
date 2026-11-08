from sqlalchemy import JSON, Integer, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class JobRecord(Base):
    __tablename__ = "jobs"
    key: Mapped[str] = mapped_column(String, primary_key=True)
    position: Mapped[int] = mapped_column(Integer, index=True)
    payload: Mapped[dict] = mapped_column(JSON)


class InterviewRecord(Base):
    __tablename__ = "interview"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)

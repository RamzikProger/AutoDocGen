import os
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

# Загружаем .env из корня проекта и из backend (если есть).
PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")
load_dotenv(Path(__file__).resolve().parent / ".env")


DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://postgres:postgres123@localhost:5432/autodoc_db",
)
SQLITE_FALLBACK_URL = "sqlite+aiosqlite:///./autodocgen.db"
ACTIVE_DATABASE_URL = DATABASE_URL


class Base(DeclarativeBase):
    pass


engine = create_async_engine(DATABASE_URL, future=True, echo=False, pool_pre_ping=True)
AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def get_db() -> AsyncSession:
    async with AsyncSessionLocal() as session:
        yield session


async def init_db() -> None:
    global engine, AsyncSessionLocal, ACTIVE_DATABASE_URL
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        return
    except Exception:
        # Быстрый fallback для локальной разработки, если Postgres недоступен.
        fallback_engine = create_async_engine(
            SQLITE_FALLBACK_URL,
            future=True,
            echo=False,
            pool_pre_ping=True,
        )
        try:
            async with fallback_engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
        except SQLAlchemyError as exc:
            raise RuntimeError(
                "Не удалось подключиться ни к PostgreSQL, ни к SQLite fallback."
            ) from exc

        await engine.dispose()
        engine = fallback_engine
        AsyncSessionLocal = async_sessionmaker(
            bind=engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )
        ACTIVE_DATABASE_URL = SQLITE_FALLBACK_URL

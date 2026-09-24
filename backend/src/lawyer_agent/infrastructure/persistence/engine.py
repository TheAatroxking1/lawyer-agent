import sys
from typing import Any

from sqlalchemy import event
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from lawyer_agent.config import Settings


def create_engine_from_url(
    database_url: str,
    *,
    pool_size: int = 5,
    max_overflow: int = 10,
    pool_timeout_seconds: float = 30.0,
) -> AsyncEngine:
    """Build an async engine whose connections run in UTC.

    Process composition roots (publisher, worker, maintenance) reuse this entry
    point so every backend connection applies the same session time zone.
    """
    url = make_url(database_url)
    # asyncmy's BufferedProtocol resizes an exported bytearray on Windows
    # Proactor reads larger than 256 KiB. Use the supported stream-based driver
    # without replacing the event loop (Windows subprocesses need Proactor).
    if sys.platform == "win32" and url.drivername == "mysql+asyncmy":
        url = url.set(drivername="mysql+aiomysql")
    engine = create_async_engine(
        url,
        pool_pre_ping=True,
        pool_size=pool_size,
        max_overflow=max_overflow,
        pool_timeout=pool_timeout_seconds,
    )

    @event.listens_for(engine.sync_engine, "connect")
    def configure_connection(dbapi_connection: Any, connection_record: Any) -> None:
        del connection_record
        if url.drivername == "mysql+aiomysql":
            # aiomysql 0.3.2 still calls PyMySQL's removed escape_bytes_prefixed.
            # Scope the compatibility adapter to this connection, keeping the
            # current PyMySQL security fixes and charset-independent hex bytes.
            driver_connection = dbapi_connection.driver_connection
            original_escape = driver_connection.escape

            def escape(value: object) -> str:
                if isinstance(value, bytes):
                    return "_binary X'" + value.hex() + "'"
                return str(original_escape(value))

            driver_connection.escape = escape
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("SET time_zone = '+00:00'")
        finally:
            cursor.close()

    return engine


def create_engine(settings: Settings) -> AsyncEngine:
    return create_engine_from_url(
        settings.database_url,
        pool_size=settings.database_pool_size,
        max_overflow=settings.database_max_overflow,
        pool_timeout_seconds=settings.database_pool_timeout_seconds,
    )


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)

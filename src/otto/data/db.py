"""
Async database singleton using the ``databases`` library.

One connection util for every application: ``database()`` connects the
shared pool for the duration of its block and yields it — an app lifespan
holds it open for the process lifetime, a CLI or Streamlit interaction
holds it per call. Code inside the block may also reach the pool via
``get_db()``.

Schema lives in ``models.py`` (SQLModel metadata) and is applied via
alembic migrations — the ``databases`` library is the runtime query layer.
"""

import contextlib
from collections.abc import AsyncIterator

import databases

from otto.data import _dsn
from otto.settings import settings


_db: databases.Database | None = None


def get_db() -> databases.Database:
    """
    Return the cached ``databases.Database`` singleton.

    :raises RuntimeError: if DATABASE_URL is not configured.
    """
    global _db  # noqa: PLW0603
    if _db is None:
        url = settings.database_url
        if not url:
            raise RuntimeError(
                "DATABASE_URL is not configured. Set the DATABASE_URL environment variable."
            )
        # The databases library expects libpq URLs, not the SQLAlchemy +asyncpg form.
        _db = databases.Database(_dsn.to_libpq(url))
    return _db


@contextlib.asynccontextmanager
async def database() -> AsyncIterator[databases.Database]:
    """
    Connect the shared pool for the duration of the block and yield it.

    On exit the pool is closed and the singleton reset, so the next block
    starts fresh — required by callers that open one block per event loop
    (each Streamlit interaction runs its own ``asyncio.run()``).

    :raises RuntimeError: if DATABASE_URL is not configured.
    """
    global _db  # noqa: PLW0603
    db = get_db()
    await db.connect()
    try:
        yield db
    finally:
        await db.disconnect()
        _db = None

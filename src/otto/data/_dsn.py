"""
DSN translation helpers for the data layer.

The application's ``DATABASE_URL`` is a SQLAlchemy-flavoured URL such as
``postgresql+asyncpg://...`` because alembic/SQLAlchemy choose the driver via
that suffix. The ``databases`` library speaks plain libpq URLs and rejects
the driver suffix. The strip lives here, in one place — call ``to_libpq()``
rather than re-implementing the replace inline.
"""

_ASYNCPG_DRIVER_SUFFIX = "+asyncpg"


def to_libpq(url: str) -> str:
    """
    Return the libpq form of a SQLAlchemy-flavoured Postgres URL.

    URLs that already lack the ``+asyncpg`` suffix pass through unchanged.
    """
    return url.replace(_ASYNCPG_DRIVER_SUFFIX, "")

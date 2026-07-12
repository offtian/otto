"""
Persistence layer. Models stay thin — no business logic.

- ``models.py`` — SQLModel table definitions (schema source of truth)
- ``db.py`` — async ``databases.Database`` singleton (runtime query layer)
- ``migrations/`` — alembic migrations (``just build-migration "message"``)
"""

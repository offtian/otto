"""
Environment configuration for Otto.

Values only — no objects. Anything that needs *construction* from these
values (clients, adapters, engines) belongs in ``config.py``.
"""

from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    debug: bool = False
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    # SQLAlchemy-flavoured URL; the databases lib gets the libpq form via
    # data/_dsn.py. Empty string = no database configured.
    database_url: str = "postgresql+asyncpg://localhost:5432/otto"


# Module-level singleton — the sanctioned direct-object import (the one
# exception to the import-modules-only rule):
#
#     from otto.settings import settings
settings = Settings()

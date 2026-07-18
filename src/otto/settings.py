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
    environment: str = "dev"

    # SQLAlchemy-flavoured URL; the databases lib gets the libpq form via
    # data/_dsn.py. Empty string = no database configured. The `postgres` role
    # matches the compose Postgres (`just infra`, trust auth) so migrations and
    # the durable store work zero-config; without a role asyncpg falls back to
    # the OS user, which the container has no role for.
    database_url: str = "postgresql+asyncpg://postgres@localhost:5432/otto"

    # LLM gateway (any OpenAI-compatible endpoint: LiteLLM, firm proxy, or
    # api.openai.com when base_url is empty).
    llm_base_url: str = "http://localhost:11434/v1"
    llm_api_key: str = "ollama"
    llm_model: str = "qwen3.6"

    # Slack app credentials + the channel where humans triage escalations
    # and approve sensitive actions.
    slack_bot_token: str = ""
    slack_signing_secret: str = ""
    slack_triage_channel: str = ""

    # Jira ticket channel (D9) — Jira Cloud REST + inbound webhook. An empty
    # base URL disables the ticket channel (stub parity with the MCP fields).
    jira_base_url: str = ""
    jira_user_email: str = ""
    jira_api_token: str = ""
    jira_webhook_secret: str = ""

    # Kill switch (FR8): false = events are acked but Otto never replies.
    otto_enabled: bool = True

    # Approver allowlists (D3) — comma-separated Slack user ids. A Postgres
    # roles table replaces these in Phase 2.
    support_user_ids: str = ""
    admin_user_ids: str = ""

    # Max messages of conversation history rebuilt per event (D2).
    thread_history_limit: int = 30

    # Per-user Slack request cap per rolling minute (3.5 runaway-spend/abuse
    # guard). 0 disables. In-process (single replica) — Redis before replicas > 1.
    slack_user_rate_limit_per_minute: int = 15

    # Approval maintenance sweep (2.5). The sweep expires stale pending
    # approvals, nudges the triage channel about the rest, and purges resolved
    # run state past its retention window. Interval 0 disables the sweep.
    approval_sweep_interval_minutes: int = 15
    approval_reminder_minutes: int = 60
    approval_expiry_minutes: int = 1440  # 24h
    approval_retention_days: int = 30  # A9 — run_state_json is PII at rest

    # Telemetry. Either, both, or neither sink may be enabled: Logfire when
    # a token is set, any OTLP collector when an endpoint is set.
    otel_service_name: str = "otto"
    otlp_endpoint: str = ""  # e.g. http://localhost:4318
    logfire_token: str = ""

    # Langfuse (LLM observability): all three set = agent traces also export
    # to its OTLP ingestion endpoint. Self-hosted via `--profile langfuse`
    # (dev keys pk-lf-otto-dev / sk-lf-otto-dev, see compose.yml) or Cloud.
    langfuse_host: str = ""  # e.g. http://localhost:3000
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""

    # MCP integrations (Streamable HTTP). An empty URL means the capability
    # runs on its local stub tool so the agent still works end-to-end in dev.
    confluence_mcp_url: str = ""
    confluence_mcp_token: str = ""
    sailpoint_mcp_url: str = ""
    sailpoint_mcp_token: str = ""

    # Directory of markdown runbooks the agent can walk users through.
    # Relative paths resolve against the process cwd (repo root for
    # `just run` and the compose app container alike).
    runbooks_dir: str = "runbooks"

    # YAML user directory: cross-channel identity (Slack id ↔ Jira account
    # id) + team per person. Missing file = empty directory; everything
    # degrades to raw channel ids.
    users_file: str = "users.yaml"

    @property
    def approver_ids(self) -> frozenset[str]:
        """
        Return the union of support and admin user ids (D3).
        """
        raw = f"{self.support_user_ids},{self.admin_user_ids}"
        return frozenset(part.strip() for part in raw.split(",") if part.strip())


# Module-level singleton — the sanctioned direct-object import (the one
# exception to the import-modules-only rule):
#
#     from otto.settings import settings
settings = Settings()

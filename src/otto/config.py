"""
Composition root for Otto.

``settings.py`` owns configuration *values*; this module owns the *objects*
built from them. Wire concrete adapters (HTTP clients, stores, vendor SDKs)
into ``Configuration`` here and access the wired instance from the
application/interfaces layers via ``get_config()``.

Layer rules (enforced by import-linter):

- ``config`` may import ``domain``, ``vendors``, ``utils`` and ``settings``
- ``domain`` and everything below it must never import ``config``
"""

import functools
import pathlib

import httpx
import pydantic
from agents import mcp as agents_mcp
from agents.models import interface as model_interface
from slack_sdk.web.async_client import AsyncWebClient

from otto.data import db as data_db
from otto.domain.identity import users as identity_users
from otto.domain.support import approvals
from otto.settings import Settings, settings
from otto.vendors import jira as jira_vendor
from otto.vendors import llm, mcp
from otto.vendors import slack as slack_vendor


class Configuration(pydantic.BaseModel):
    """
    Process-wide wiring of settings and adapters.

    A Pydantic model for consistency with ``settings`` and the interface
    schemas, but every field except ``settings`` holds a live adapter (an SDK
    client, MCP server, or model) — or, in tests, a double — that Pydantic
    cannot isinstance-validate, so those use ``SkipValidation``. ``frozen``
    keeps the wiring immutable; ``protected_namespaces=()`` allows the
    ``model`` field name.
    """

    model_config = pydantic.ConfigDict(
        frozen=True, protected_namespaces=(), arbitrary_types_allowed=True
    )

    settings: Settings
    slack: pydantic.SkipValidation[slack_vendor.SlackGateway]
    triage: pydantic.SkipValidation[slack_vendor.SlackTriageBackend]
    # None = ticket channel disabled (no jira_base_url configured).
    jira: pydantic.SkipValidation[jira_vendor.JiraGateway | None]
    # Cross-channel requester identity + team; empty when users_file is
    # absent — lookups just return None.
    directory: pydantic.SkipValidation[identity_users.Directory]
    approvals: pydantic.SkipValidation[approvals.ApprovalStore]
    model: pydantic.SkipValidation[model_interface.Model]
    # MCP servers are optional: None = capability runs on its local stub
    # tool, so the full loop works in dev with zero external dependencies.
    confluence_mcp: pydantic.SkipValidation[agents_mcp.MCPServerStreamableHttp | None]
    sailpoint_mcp: pydantic.SkipValidation[agents_mcp.MCPServerStreamableHttp | None]


@functools.cache
def get_config() -> Configuration:
    """
    Return the process-wide configuration, building it on first access.
    """
    gateway = slack_vendor.SlackGateway(client=AsyncWebClient(token=settings.slack_bot_token))
    return Configuration(
        settings=settings,
        slack=gateway,
        triage=slack_vendor.SlackTriageBackend(
            gateway=gateway,
            triage_channel=settings.slack_triage_channel,
        ),
        jira=(
            jira_vendor.JiraGateway(
                client=httpx.AsyncClient(
                    base_url=settings.jira_base_url,
                    auth=(settings.jira_user_email, settings.jira_api_token),
                    timeout=10.0,
                )
            )
            if settings.jira_base_url
            else None
        ),
        # DB-backed directory + live role lookup when a database is configured
        # (2.3); the yaml directory keeps the zero-dependency dev loop.
        directory=(
            identity_users.PostgresUserDirectory(database=data_db.get_db())
            if settings.database_url
            else identity_users.UserDirectory(
                users=identity_users.load_users(pathlib.Path(settings.users_file))
            )
        ),
        # Durable store when a database is configured (Phase 2); the in-memory
        # store keeps the zero-dependency dev loop when DATABASE_URL is empty.
        approvals=(
            approvals.PostgresApprovalStore(database=data_db.get_db())
            if settings.database_url
            else approvals.InMemoryApprovalStore()
        ),
        model=llm.build_model(
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key,
            model_name=settings.llm_model,
        ),
        confluence_mcp=(
            mcp.build_confluence(
                url=settings.confluence_mcp_url,
                token=settings.confluence_mcp_token,
            )
            if settings.confluence_mcp_url
            else None
        ),
        sailpoint_mcp=(
            mcp.build_sailpoint(
                url=settings.sailpoint_mcp_url,
                token=settings.sailpoint_mcp_token,
            )
            if settings.sailpoint_mcp_url
            else None
        ),
    )

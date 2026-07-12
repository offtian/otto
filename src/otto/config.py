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

import attrs
import httpx
from agents import mcp as agents_mcp
from agents.models import interface as model_interface
from slack_sdk.web.async_client import AsyncWebClient

from otto.domain.identity import users as identity_users
from otto.domain.support import approvals
from otto.settings import Settings, settings
from otto.vendors import jira as jira_vendor
from otto.vendors import llm, mcp
from otto.vendors import slack as slack_vendor


@attrs.frozen
class Configuration:
    """
    Process-wide wiring of settings and adapters.
    """

    settings: Settings
    slack: slack_vendor.SlackGateway
    triage: slack_vendor.SlackTriageBackend
    # None = ticket channel disabled (no jira_base_url configured).
    jira: jira_vendor.JiraGateway | None
    # Cross-channel requester identity + team; empty when users_file is
    # absent — lookups just return None.
    directory: identity_users.UserDirectory
    approvals: approvals.ApprovalStore
    model: model_interface.Model
    # MCP servers are optional: None = capability runs on its local stub
    # tool, so the full loop works in dev with zero external dependencies.
    confluence_mcp: agents_mcp.MCPServerStreamableHttp | None
    sailpoint_mcp: agents_mcp.MCPServerStreamableHttp | None


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
        directory=identity_users.UserDirectory(
            users=identity_users.load_users(pathlib.Path(settings.users_file))
        ),
        approvals=approvals.InMemoryApprovalStore(),
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

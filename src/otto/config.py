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
import typing

import agents
import httpx
import pydantic
from agents.models import interface as model_interface
from slack_sdk.web.async_client import AsyncWebClient

from otto.data import db
from otto.domain.identity import users as identity_users
from otto.domain.support import agent as support_agent
from otto.domain.support import approvals as support_approvals
from otto.domain.support import memory as support_memory
from otto.domain.support import policy as support_policy
from otto.domain.support import teams as support_teams
from otto.settings import Settings, settings
from otto.utils import logs
from otto.vendors import cognee as cognee_vendor
from otto.vendors import jira as jira_vendor
from otto.vendors import llm, mcp
from otto.vendors import slack as slack_vendor


if typing.TYPE_CHECKING:
    _Agent = agents.Agent[support_agent.SupportContext]
else:
    # agents.Agent is a dataclass with forward refs pydantic cannot resolve
    # (it recurses into dataclasses even under SkipValidation), so at runtime
    # the field is schema-less on purpose.
    _Agent = typing.Any


class Configuration(pydantic.BaseModel):
    """
    Process-wide wiring of settings and adapters.

    Constructed from ``settings`` alone; each ``load_*`` method wires one
    concern onto the instance and ``get_config()`` runs them all, so the
    object is fully loaded everywhere outside this module. Tests may instead
    pass doubles for every field directly. A Pydantic model for consistency
    with ``settings`` and the interface schemas, but every field except
    ``settings`` holds a live adapter (an SDK client, MCP server, or model)
    that Pydantic cannot isinstance-validate, so those use ``SkipValidation``.
    ``protected_namespaces=()`` allows the ``model`` field name.
    """

    model_config = pydantic.ConfigDict(protected_namespaces=(), arbitrary_types_allowed=True)

    settings: Settings
    # The None defaults exist only so Configuration(settings=...) can be
    # constructed before the load_* stages run; get_config() never hands out
    # a partially loaded instance, so the fields stay typed as always-present.
    slack: pydantic.SkipValidation[slack_vendor.SlackGateway] = None  # type: ignore[assignment]
    triage: pydantic.SkipValidation[slack_vendor.SlackTriageBackend] = None  # type: ignore[assignment]
    # None = ticket channel disabled (no jira_base_url configured).
    jira: pydantic.SkipValidation[jira_vendor.JiraGateway | None] = None
    # Cross-channel requester identity + team; empty when users_file is
    # absent — lookups just return None.
    directory: pydantic.SkipValidation[identity_users.Directory] = None  # type: ignore[assignment]
    approvals: pydantic.SkipValidation[support_approvals.ApprovalStore] = None  # type: ignore[assignment]
    model: pydantic.SkipValidation[model_interface.Model] = None  # type: ignore[assignment]
    # MCP mounts are optional: None = capability runs on its local stub
    # tool, so the full loop works in dev with zero external dependencies.
    confluence_mcp: pydantic.SkipValidation[mcp.MCPServerMount | None] = None
    sailpoint_mcp: pydantic.SkipValidation[mcp.MCPServerMount | None] = None
    # Wired by load_agents() at app startup, after the MCP servers connect —
    # function_tools() needs a live connection, so get_config() cannot do it.
    agent: pydantic.SkipValidation[_Agent] = None  # type: ignore[assignment]
    # The access-request flow node's agent; None routes access intents to the
    # general agent (which keeps the same gated tools).
    access_agent: pydantic.SkipValidation[_Agent | None] = None
    # Routes each inbound request to a flow-graph node; None = no routing,
    # every request runs the general agent, exactly the pre-graph behavior.
    intent_classifier: pydantic.SkipValidation[llm.LLMIntentClassifier | None] = None
    # Team-owned flows: the profiles registry (data, loaded at get_config),
    # the read-only specialist MCP mounts, and one owner agent per team
    # (built by load_agents once the MCP servers are connected). None/empty
    # = no team flows; requests fall back to the access/general split.
    team_registry: pydantic.SkipValidation[support_teams.TeamRegistry | None] = None
    specialist_mcps: pydantic.SkipValidation[dict[str, mcp.MCPServerMount] | None] = None
    team_agents: pydantic.SkipValidation[dict[str, _Agent] | None] = None
    # Long-term memory (Cognee) — None = disabled; the search_memory tool
    # and the resolution-ingest hook both degrade to no-ops.
    memory: pydantic.SkipValidation[support_memory.MemoryStore | None] = None

    def load_memory(self) -> None:
        """
        Wire the Cognee memory store when enabled — the one deliberately
        lazy wiring stage: building it imports the heavy cognee package.
        """
        if not self.settings.memory_enabled:
            return
        self.memory = cognee_vendor.build_memory_store(
            llm_base_url=self.settings.llm_base_url,
            llm_api_key=self.settings.llm_api_key,
            llm_model=self.settings.llm_model,
            embedding_endpoint=self.settings.memory_embedding_endpoint,
            embedding_model=self.settings.memory_embedding_model,
            embedding_dimensions=self.settings.memory_embedding_dimensions,
            dataset=self.settings.memory_dataset,
        )

    async def load_agents(self) -> None:
        """
        Wire the flow agents from the model and the mounted MCP capabilities.
        A mount whose server is absent or never connected degrades to the
        capability's local stub tool instead of erroring on every request.
        """
        confluence_tools = await _mounted_tools(self.confluence_mcp)
        sailpoint_tools = await _mounted_tools(self.sailpoint_mcp)
        self.agent = support_agent.build_agent(
            model=self.model,
            confluence_tools=confluence_tools,
            sailpoint_tools=sailpoint_tools,
        )
        self.access_agent = support_agent.build_access_agent(
            model=self.model,
            confluence_tools=confluence_tools,
            sailpoint_tools=sailpoint_tools,
        )
        specialist_tools = {
            name: (await _mounted_tools(mount)) or []
            for name, mount in (self.specialist_mcps or {}).items()
        }
        self.team_agents = support_teams.build_team_agents(
            registry=self.team_registry or support_teams.TeamRegistry(),
            model=self.model,
            specialist_mcp_tools=specialist_tools,
            confluence_tools=confluence_tools,
        )

    def load_model(self) -> None:
        """
        Wire the LLM the agent runs on.
        """
        self.model = llm.build_model(
            base_url=self.settings.llm_base_url,
            api_key=self.settings.llm_api_key,
            model_name=self.settings.llm_model,
        )

    def load_vendors(self) -> None:
        """
        Wire the channel gateways: Slack always, Jira only when configured.
        """
        self.slack = slack_vendor.SlackGateway(
            client=AsyncWebClient(token=self.settings.slack_bot_token)
        )
        self.triage = slack_vendor.SlackTriageBackend(
            gateway=self.slack,
            triage_channel=self.settings.slack_triage_channel,
            # Escalation team routing (D24): LLM stand-in until the firm
            # classification API replaces it behind the same seam. No teams
            # configured = no classifier = the single channel, as before.
            classifier=(
                llm.build_team_classifier(
                    base_url=self.settings.llm_base_url,
                    api_key=self.settings.llm_api_key,
                    model_name=self.settings.llm_model,
                    teams=self.settings.team_list,
                )
                if self.settings.support_teams
                else None
            ),
            team_channels=self.settings.team_channel_map,
        )
        # Intent routing for the flow graph: one zero-shot classification per
        # request; fail-open, so a classifier outage degrades to the general
        # agent, never to a blocked request.
        self.intent_classifier = llm.build_intent_classifier(
            base_url=self.settings.llm_base_url,
            api_key=self.settings.llm_api_key,
            model_name=self.settings.llm_model,
        )
        self.jira = (
            jira_vendor.JiraGateway(
                client=httpx.AsyncClient(
                    base_url=self.settings.jira_base_url,
                    auth=(self.settings.jira_user_email, self.settings.jira_api_token),
                    timeout=10.0,
                )
            )
            if self.settings.jira_base_url
            else None
        )

    def load_stores(self) -> None:
        """
        Wire the directory and approval stores: DB-backed when a database is
        configured (2.3/Phase 2); local yaml/in-memory keeps the
        zero-dependency dev loop when DATABASE_URL is empty.
        """
        if self.settings.database_url:
            self.directory = identity_users.PostgresUserDirectory(database=db.get_db())
            self.approvals = support_approvals.PostgresApprovalStore(database=db.get_db())
        else:
            self.directory = identity_users.UserDirectory(
                users=identity_users.load_users(pathlib.Path(self.settings.users_file))
            )
            self.approvals = support_approvals.InMemoryApprovalStore()

    def load_mcps(self) -> None:
        """
        Wire one mount per MCP integration, built from the spec table below.
        An unset URL yields None — the capability runs on its local stub tool.
        """
        specs: dict[str, mcp.MCPSpec | None] = {
            "confluence_mcp": (
                mcp.MCPSpec(
                    name="confluence",
                    url=self.settings.confluence_mcp_url,
                    token=self.settings.confluence_mcp_token,
                    allowed_tools=mcp.CONFLUENCE_READ_TOOLS,
                    # Reads only (A1) — every allowed tool runs without approval.
                    ungated_tools=frozenset(mcp.CONFLUENCE_READ_TOOLS),
                )
                if self.settings.confluence_mcp_url
                else None
            ),
            "sailpoint_mcp": (
                mcp.MCPSpec(
                    name="sailpoint",
                    url=self.settings.sailpoint_mcp_url,
                    token=self.settings.sailpoint_mcp_token,
                    # Default-deny (2.6): only policy-cleared reads skip approval.
                    ungated_tools=support_policy.SENSITIVITY_POLICY.ungated,
                )
                if self.settings.sailpoint_mcp_url
                else None
            ),
        }
        for field, spec in specs.items():
            setattr(self, field, mcp.build_mount(spec=spec) if spec is not None else None)
        # One read-only mount per specialist that declares both a server and
        # a tools allowlist. Every allowlisted tool mounts ungated on purpose:
        # an approval interruption inside a nested specialist run can never be
        # approved, so the allowlist IS the read-only contract (default-deny —
        # an unlisted tool never reaches the specialist).
        urls = self.settings.specialist_mcp_url_map
        tokens = self.settings.specialist_mcp_token_map
        self.specialist_mcps = {
            name: mcp.build_mount(
                spec=mcp.MCPSpec(
                    name=name,
                    url=urls[profile.mcp],
                    token=tokens.get(profile.mcp, ""),
                    allowed_tools=profile.tools,
                    ungated_tools=frozenset(profile.tools),
                )
            )
            for name, profile in (
                self.team_registry or support_teams.TeamRegistry()
            ).specialists.items()
            if profile.mcp and profile.mcp in urls and profile.tools
        }

    def load_teams(self) -> None:
        """
        Load the team-profile registry — data only; the owner agents build in
        ``load_agents()`` once the specialist MCP servers are connected.
        """
        self.team_registry = support_teams.load_registry(
            pathlib.Path(self.settings.team_profiles_file)
        )


async def _mounted_tools(mount: mcp.MCPServerMount | None) -> list[agents.Tool] | None:
    """
    Return the mount's wrapped tools, or None (→ the local stub tool) when
    the mount is absent or its server never connected.
    """
    if mount is None:
        return None
    try:
        return list(await mount.function_tools())
    except agents.exceptions.UserError as exc:
        logs.log_exception(exc, params={"mcp_tools": mount.spec.name})
        return None


@functools.cache
def get_config() -> Configuration:
    """
    Return the process-wide configuration, building it on first access.
    ``load_agents()`` is not run here — it is async and needs the MCP servers
    connected, so the app lifespan awaits it after connecting them.
    """
    config = Configuration(settings=settings)
    config.load_model()
    config.load_vendors()
    config.load_stores()
    config.load_teams()
    config.load_mcps()
    config.load_memory()
    return config

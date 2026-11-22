"""
The catalog: what ``otto.yaml`` says Otto may reach beyond its built-in
tools — the specialists it may consult and the repositories it may propose
changes to. Two flat lists, and one function that mounts them on the general
agent. Adding a specialist or a repository is a YAML edit, never an
orchestration change.
"""

import pathlib
from collections.abc import Mapping, Sequence

import agents
import attrs
import yaml
from agents.models import interface as model_interface

from otto.domain.support import agent as support_agent
from otto.domain.support import changes
from otto.domain.support import specialists as support_specialists


@attrs.frozen
class Catalog:
    """
    The loaded ``otto.yaml``. Empty by default: with nothing declared the
    general agent is exactly its built-in self.
    """

    # The module import is aliased because `x: ann = value` assigns the
    # value BEFORE evaluating the annotation — a field named like its
    # module shadows it.
    specialists: tuple[support_specialists.SpecialistProfile, ...] = ()
    repos: tuple[changes.RepoProfile, ...] = ()


def load_catalog(path: pathlib.Path) -> Catalog:
    """
    Return the catalog declared in the YAML file. A missing or empty file is
    an empty catalog — both lists are optional.

    :raises KeyError: if an entry has no ``name``.
    :raises ValueError: if two specialists share a name — a typo fails at
        startup, never per request.
    """
    if not path.is_file():
        return Catalog()
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    profiles = tuple(
        support_specialists.SpecialistProfile(
            name=str(entry["name"]),
            description=str(entry.get("description", "")),
            instructions=str(entry.get("instructions", "")),
            mcp=str(entry.get("mcp", "")),
            tools=tuple(str(tool) for tool in entry.get("tools") or ()),
        )
        for entry in data.get("specialists") or []
    )
    names = [profile.name for profile in profiles]
    if len(set(names)) != len(names):
        raise ValueError(f"otto.yaml declares a specialist twice: {sorted(names)}")
    repos = tuple(
        changes.RepoProfile(
            name=str(entry["name"]),
            description=str(entry.get("description", "")),
            base=str(entry.get("base", "main")),
            paths=tuple(str(pattern) for pattern in entry.get("paths") or ()),
        )
        for entry in data.get("repos") or []
    )
    return Catalog(specialists=profiles, repos=repos)


def mount(
    agent: agents.Agent[support_agent.SupportContext],
    *,
    catalog: Catalog,
    model: model_interface.Model,
    specialist_mcp_tools: Mapping[str, Sequence[agents.Tool]],
    repo_host: changes.RepoHost | None,
    signing_key: str = "",
) -> agents.Agent[support_agent.SupportContext]:
    """
    Return ``agent`` with what the catalog declares mounted on it: one
    consult tool per specialist, and the repository tools when a host is
    wired — each with the instructions for using it. An empty catalog
    returns an agent with the same tools and instructions.

    The agent must be a top-level one: the repository tools include the
    gated ``open_pull_request`` and ``update_pull_request``, which a nested
    agent could never get approved.

    :param signing_key: signs the return address on each pull request the
        agent opens; empty = they carry none, and the tools for revising a
        pull request are not mounted.
    :raises ValueError: if a specialist mounts an approval-gated tool.
    """
    tools = list(agent.tools)
    instructions = str(agent.instructions)
    if catalog.specialists:
        tools.extend(
            support_specialists.build_consult_tools(
                profiles=catalog.specialists, model=model, mcp_tools=specialist_mcp_tools
            )
        )
        instructions += support_specialists.GUIDANCE
    if catalog.repos and repo_host is not None:
        tools.extend(
            changes.build_change_tools(
                repos=catalog.repos, host=repo_host, signing_key=signing_key
            )
        )
        instructions += changes.guidance(catalog.repos, revisions=bool(signing_key))
    return agent.clone(tools=tools, instructions=instructions)

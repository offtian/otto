"""
The support flow graph: one classify node routes each inbound request to the
agent node that owns it — the access specialist, a team-owned owner agent
(resolved from the services the request mentions), or the general agent.
Agent nodes are the only ones that may suspend (HITL) — ``support`` persists
the suspended node name plus ``dump_state``'s durable subset on the
approval, and a human decision re-enters the graph exactly there.

State keys the nodes read: ``text`` (the raw request, for the classifier),
``input`` (the rebuilt conversation input), ``ctx`` (the ``SupportContext``),
``team`` (set by classify when an owning team resolved), and optionally
``resume`` (a paused run's serialized state plus the human decision).
"""

import json

import agents

from otto import config
from otto.application import graph
from otto.domain.support import agent as support_agent
from otto.utils import logs
from otto.vendors import llm


ACCESS = "access"
GENERAL = "general"
OWNER = "owner"


async def _classify(state: graph.State) -> graph.NodeResult:
    """
    Route the request to a flow node. Fail-open: no classifier, a classifier
    error, an unresolved team, or a plain question all land on the general
    agent — misrouting can only cost focus, never capability.
    """
    cfg = config.get_config()
    if cfg.intent_classifier is None or (cfg.access_agent is None and not cfg.team_agents):
        return graph.Goto(GENERAL)
    reading = await cfg.intent_classifier.classify(text=state["text"])
    node, team = _route(cfg=cfg, reading=reading)
    if team:
        state["team"] = team
    # Only the intent and resolved team are logged, never the extracted
    # services — those are derived from untrusted message text (NFR2).
    logs.log_event("intent_routed", params={"intent": reading.intent, "node": node, "team": team})
    return graph.Goto(node)


def _route(cfg: config.Configuration, *, reading: llm.IntentReading) -> tuple[str, str]:
    """
    Return (node, team) for one intent reading. Access requests beat team
    ownership — the gated submission flow is never diverted; every other
    intent is owned by whichever team owns the services it mentions.
    """
    if reading.intent == llm.INTENT_ACCESS and cfg.access_agent is not None:
        return ACCESS, ""
    team = (
        cfg.team_registry.resolve(services=reading.services)
        if cfg.team_registry is not None and reading.services
        else None
    )
    if team is not None and cfg.team_agents and team.name in cfg.team_agents:
        return OWNER, team.name
    return GENERAL, ""


async def _access(state: graph.State) -> graph.NodeResult:
    cfg = config.get_config()
    # A pause can outlive a deploy: if the access agent is gone on resume,
    # the general agent (same gated tools) still carries the decision out.
    return await _run_agent(cfg.access_agent or cfg.agent, state)


async def _owner(state: graph.State) -> graph.NodeResult:
    cfg = config.get_config()
    # A pause can outlive a profile change: a vanished team resumes on the
    # general agent rather than dropping the human's decision.
    agent = (cfg.team_agents or {}).get(state.get("team", ""))
    return await _run_agent(agent or cfg.agent, state)


async def _general(state: graph.State) -> graph.NodeResult:
    return await _run_agent(config.get_config().agent, state)


async def _run_agent(
    agent: agents.Agent[support_agent.SupportContext], state: graph.State
) -> graph.NodeResult:
    """
    Run one agent as a graph node: a fresh run on ``input``, or — when
    ``resume`` is present — a paused run continued with the human decision
    applied. Interruptions suspend the node with the ``RunResult`` as
    payload; anything else completes it.
    """
    resume = state.pop("resume", None)
    if resume is None:
        result = await agents.Runner.run(agent, state["input"], context=state["ctx"])
    else:
        # The context must be re-supplied via from_string, NOT via Runner.run:
        # a context passed to run() replaces the state's context wrapper, which
        # is where approve()/reject() decisions are recorded — the run would
        # re-interrupt forever.
        run_state = await agents.RunState.from_string(
            agent,
            resume["run_state_json"],
            context_override=agents.RunContextWrapper(context=state["ctx"]),
        )
        for interruption in run_state.get_interruptions():
            if resume["approved"]:
                run_state.approve(interruption)
            else:
                run_state.reject(interruption)
        result = await agents.Runner.run(agent, run_state)
    if result.interruptions:
        return graph.Suspend(payload=result)
    return graph.Done(output=result)


def dump_state(state: graph.State) -> str:
    """
    Serialize the durable subset of flow state for a paused run — only the
    primitives resume needs to rebuild the right agent, never SDK objects
    (the pinned SDK version already couples the RunState blob; this must
    not add to it). Empty string = nothing beyond the node name.
    """
    team = state.get("team", "")
    return json.dumps({"team": team}) if team else ""


def load_state(raw: str) -> graph.State:
    """
    Return a fresh flow state from a paused run's serialized subset.
    """
    if not raw:
        return {}
    data = json.loads(raw)
    return {"team": data["team"]} if data.get("team") else {}


SUPPORT = graph.Graph(
    name="support",
    start="classify",
    nodes={"classify": _classify, ACCESS: _access, OWNER: _owner, GENERAL: _general},
)

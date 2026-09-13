"""
Minimal graph engine for support flows: nodes are async callables over a
shared mutable state dict, and edges are the ``Goto`` results they return.
An agent node may return ``Suspend`` to pause for a human decision (HITL);
the caller persists the payload plus the suspended node's name and later
re-enters the graph at that node via ``run(entry=...)``.

Deliberately not a workflow runtime: no fanout/join primitive (fanout is
``asyncio.gather`` inside a node — only agent nodes may suspend, so branches
never pause), no per-node state persistence (non-agent nodes must be cheap
and re-runnable).
"""

import typing
from collections.abc import Awaitable, Callable

import attrs


State = dict[str, typing.Any]


@attrs.frozen
class Goto:
    """
    Continue the run at the named node.
    """

    node: str


@attrs.frozen
class Suspend:
    """
    Pause the run for a human decision. ``payload`` carries whatever the
    caller needs to persist and resume; ``node`` is stamped by the runner so
    the resume path knows where to re-enter.
    """

    payload: typing.Any
    node: str = ""


@attrs.frozen
class Done:
    """
    Terminal result of a run.
    """

    output: typing.Any = None


NodeResult = Goto | Suspend | Done
Node = Callable[[State], Awaitable[NodeResult]]


@attrs.frozen
class Graph:
    """
    A named set of nodes executed from ``start`` until one returns
    ``Suspend`` or ``Done``.
    """

    name: str
    start: str
    nodes: dict[str, Node]

    async def run(self, state: State, *, entry: str | None = None) -> Suspend | Done:
        """
        Run the graph over ``state`` and return the terminal result.

        :param state: shared mutable state the nodes read and write.
        :param entry: node to re-enter at (resume); None starts at ``start``.
        :raises KeyError: if a node names an edge that does not exist.
        """
        current = entry or self.start
        while True:
            match await self.nodes[current](state):
                case Goto(node=node):
                    current = node
                case Suspend(payload=payload):
                    return Suspend(payload=payload, node=current)
                case Done() as done:
                    return done

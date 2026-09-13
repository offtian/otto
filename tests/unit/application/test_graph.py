import pytest

from otto.application import graph


class TestGraphRun:
    async def test_follows_goto_edges_until_done(self):
        # Given a two-node graph whose nodes record the path taken
        visited: list[str] = []

        async def first(state: graph.State) -> graph.NodeResult:
            visited.append("first")
            return graph.Goto("second")

        async def second(state: graph.State) -> graph.NodeResult:
            visited.append("second")
            return graph.Done(output="answer")

        flow = graph.Graph(name="test", start="first", nodes={"first": first, "second": second})

        # When the graph runs from the start
        outcome = await flow.run({})

        # Then every edge was followed and the terminal output comes back
        assert visited == ["first", "second"]
        assert outcome == graph.Done(output="answer")

    async def test_stamps_the_suspending_node_on_the_returned_suspend(self):
        # Given a graph whose second node suspends for a human decision
        async def route(state: graph.State) -> graph.NodeResult:
            return graph.Goto("agent")

        async def agent(state: graph.State) -> graph.NodeResult:
            return graph.Suspend(payload="paused-run")

        flow = graph.Graph(name="test", start="route", nodes={"route": route, "agent": agent})

        # When the graph runs
        outcome = await flow.run({})

        # Then the suspend names the node that paused, so resume re-enters there
        assert outcome == graph.Suspend(payload="paused-run", node="agent")

    async def test_entry_reenters_at_the_named_node(self):
        # Given a graph with a start node that must not run on resume
        visited: list[str] = []

        async def start(state: graph.State) -> graph.NodeResult:
            visited.append("start")
            return graph.Goto("agent")

        async def agent(state: graph.State) -> graph.NodeResult:
            visited.append("agent")
            return graph.Done(output="resumed")

        flow = graph.Graph(name="test", start="start", nodes={"start": start, "agent": agent})

        # When the graph is re-entered at the suspended node
        outcome = await flow.run({}, entry="agent")

        # Then only the resumed node ran
        assert visited == ["agent"]
        assert outcome == graph.Done(output="resumed")

    async def test_state_is_shared_between_nodes(self):
        # Given a graph whose first node writes state the second one reads
        async def writer(state: graph.State) -> graph.NodeResult:
            state["finding"] = "vpn misconfigured"
            return graph.Goto("reader")

        async def reader(state: graph.State) -> graph.NodeResult:
            return graph.Done(output=state["finding"])

        flow = graph.Graph(name="test", start="writer", nodes={"writer": writer, "reader": reader})

        # When the graph runs
        outcome = await flow.run({})

        # Then the second node saw the first node's write
        assert outcome == graph.Done(output="vpn misconfigured")

    async def test_an_edge_to_an_unknown_node_raises_key_error(self):
        # Given a node that names an edge the graph does not have
        async def stray(state: graph.State) -> graph.NodeResult:
            return graph.Goto("nowhere")

        flow = graph.Graph(name="test", start="stray", nodes={"stray": stray})

        # When the graph runs
        # Then the broken edge surfaces as an error instead of a silent stop
        with pytest.raises(KeyError):
            await flow.run({})

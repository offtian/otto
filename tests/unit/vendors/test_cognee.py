import types
from unittest import mock

from otto.vendors import cognee as cognee_vendor


def _fake_cognee(results=()):
    """
    Stand-in for the cognee module — the store takes it by injection, so no
    test ever pays the real package's import cost.
    """
    return types.SimpleNamespace(
        add=mock.AsyncMock(),
        cognify=mock.AsyncMock(),
        search=mock.AsyncMock(return_value=list(results)),
        SearchType=types.SimpleNamespace(GRAPH_COMPLETION="GRAPH_COMPLETION"),
    )


class TestCogneeMemoryStore:
    async def test_ingest_adds_then_cognifies_the_same_dataset(self):
        # Given a store bound to the resolutions dataset
        fake = _fake_cognee()
        store = cognee_vendor.CogneeMemoryStore(cognee=fake, dataset="support_resolutions")

        # When a resolved conversation is ingested
        await store.ingest(text="Resolved: bump the base image")

        # Then it was added to the dataset and cognified into the graph
        fake.add.assert_awaited_once_with(
            "Resolved: bump the base image", dataset_name="support_resolutions"
        )
        fake.cognify.assert_awaited_once_with(datasets=["support_resolutions"])

    async def test_search_runs_a_graph_completion_over_the_dataset(self):
        # Given a store whose backend returns mixed-type results
        fake = _fake_cognee(results=["We bumped the image.", 42])
        store = cognee_vendor.CogneeMemoryStore(cognee=fake, dataset="support_resolutions")

        # When memory is searched
        passages = await store.search(query="template build fails")

        # Then every result comes back as a string, and the query ran a
        # graph completion scoped to the store's dataset
        assert passages == ["We bumped the image.", "42"]
        fake.search.assert_awaited_once_with(
            query_text="template build fails",
            query_type="GRAPH_COMPLETION",
            datasets=["support_resolutions"],
        )

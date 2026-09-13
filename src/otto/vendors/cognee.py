"""
Long-term memory over embedded Cognee (the ``MemoryStore`` implementation):
``ingest`` adds a resolved conversation and cognifies it into the knowledge
graph; ``search`` runs a graph-context completion over everything stored.

Model calls (entity extraction at ingest, completion at search) go through
the same OpenAI-compatible gateway as the agents; embeddings need their own
endpoint. Storage uses Cognee's configured backends — local files by
default in dev, Postgres/pgvector via Cognee's own env vars in prod.

The ``cognee`` package costs seconds to import, so this module never
imports it at the top: ``build_memory_store`` does (the sanctioned lazy
import), and the store holds the module as an injected attribute — tests
pass a fake namespace instead.
"""

import typing

import attrs


@attrs.frozen
class CogneeMemoryStore:
    """
    ``MemoryStore`` over the cognee module (injected, see module docstring).
    """

    cognee: typing.Any
    dataset: str

    async def ingest(self, *, text: str) -> None:
        """
        Store one resolved conversation and index it for retrieval.
        """
        await self.cognee.add(text, dataset_name=self.dataset)
        await self.cognee.cognify(datasets=[self.dataset])

    async def search(self, *, query: str) -> list[str]:
        """
        Return memory passages relevant to the query, best first.
        """
        results = await self.cognee.search(
            query_text=query,
            query_type=self.cognee.SearchType.GRAPH_COMPLETION,
            datasets=[self.dataset],
        )
        return [str(result) for result in results]


def build_memory_store(
    *,
    llm_base_url: str,
    llm_api_key: str,
    llm_model: str,
    embedding_endpoint: str,
    embedding_model: str,
    embedding_dimensions: int,
    dataset: str,
) -> CogneeMemoryStore:
    """
    Return the Cognee memory store wired to the configured gateway. Applies
    Cognee's process-wide LLM/embedding configuration once, at build time.
    """
    import cognee  # ~2s warm — the sanctioned lazy import; only paid when memory is enabled

    cognee.config.set_llm_provider("custom")
    cognee.config.set_llm_endpoint(llm_base_url)
    cognee.config.set_llm_api_key(llm_api_key or "unset")
    cognee.config.set_llm_model(f"openai/{llm_model}")
    if embedding_endpoint:
        cognee.config.set_embedding_provider("custom")
        cognee.config.set_embedding_endpoint(embedding_endpoint)
        cognee.config.set_embedding_api_key(llm_api_key or "unset")
    if embedding_model:
        cognee.config.set_embedding_model(f"openai/{embedding_model}")
    if embedding_dimensions:
        cognee.config.set_embedding_dimensions(embedding_dimensions)
    return CogneeMemoryStore(cognee=cognee, dataset=dataset)

from types import SimpleNamespace

import pytest

from homeshield.core.config import Provider, Settings
from homeshield.core.llm import OpenAICompatLLM


class Embeddings:
    def __init__(self, duplicate=False):
        self.sizes = []
        self.duplicate = duplicate

    async def create(self, *, model, input):
        self.sizes.append(len(input))
        data = [SimpleNamespace(index=i, embedding=[float(i)]) for i in range(len(input))]
        data.reverse()
        if self.duplicate:
            data[-1] = SimpleNamespace(index=data[0].index, embedding=data[0].embedding)
        return SimpleNamespace(data=data)


@pytest.mark.asyncio
async def test_embedding_response_is_reordered_and_batched_20_20_10():
    provider = Provider("P", "key", "", "model")
    settings = Settings(mode="llm", providers={"P": provider}, chat_provider_name="P",
                        embed_provider_name="P")
    llm = OpenAICompatLLM(settings)
    embeddings = Embeddings()
    llm._get_client_and_model = lambda _: (SimpleNamespace(embeddings=embeddings), "model")
    vectors = await llm.embed([str(i) for i in range(50)])
    assert embeddings.sizes == [20, 20, 10]
    assert vectors == [[float(i)] for i in range(20)] + [[float(i)] for i in range(20)] + [[float(i)] for i in range(10)]


@pytest.mark.asyncio
async def test_embedding_duplicate_index_is_rejected():
    provider = Provider("P", "key", "", "model")
    llm = OpenAICompatLLM(Settings(mode="llm", providers={"P": provider},
                                   chat_provider_name="P", embed_provider_name="P"))
    embeddings = Embeddings(duplicate=True)
    llm._get_client_and_model = lambda _: (SimpleNamespace(embeddings=embeddings), "model")
    with pytest.raises(ValueError, match="duplicate"):
        await llm.embed(["a", "b"])


@pytest.mark.asyncio
async def test_embed_dimensions_and_batch_size_are_applied():
    provider = Provider("P", "key", "", "model")
    settings = Settings(mode="llm", providers={"P": provider}, chat_provider_name="P",
                        embed_provider_name="P", embed_dimensions=256, embed_batch_size=10)
    llm = OpenAICompatLLM(settings)
    seen: list[dict] = []

    class Recording:
        async def create(self, **kwargs):
            seen.append(kwargs)
            return SimpleNamespace(data=[SimpleNamespace(index=i, embedding=[float(i)])
                                         for i in range(len(kwargs["input"]))])

    llm._get_client_and_model = lambda _: (SimpleNamespace(embeddings=Recording()), "model")
    vectors = await llm.embed([str(i) for i in range(25)])
    assert [len(kwargs["input"]) for kwargs in seen] == [10, 10, 5]
    assert all(kwargs["dimensions"] == 256 for kwargs in seen)
    assert len(vectors) == 25


def test_embed_dimensions_must_be_positive():
    provider = Provider("P", "key", "", "model")
    with pytest.raises(ValueError, match="EMBED_DIMENSIONS"):
        OpenAICompatLLM(Settings(mode="llm", providers={"P": provider}, chat_provider_name="P",
                                 embed_provider_name="P", embed_dimensions=0))

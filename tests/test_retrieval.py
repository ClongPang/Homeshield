import asyncio
import math

import pytest

from homeshield.core.features import FeatureSpec
from homeshield.core.models import Conversation, KbCase, Turn
from homeshield.core.retrieval import (
    DEFAULT_TOP_K, Retriever, _vector_text, build_retrieval_query,
)


def case(id="C1", name="安全账户", tactic="要求转账", markers=None):
    return KbCase(id=id, scam_type="fake_police", name=name, tactic=tactic,
                  markers=markers or ["立即转账"], advice="")


class FakeLLM:
    def __init__(self, vectors=None, fail_at=None):
        self.vectors = vectors
        self.fail_at = fail_at
        self.calls = []

    async def embed(self, texts):
        self.calls.append(texts)
        index = len(self.calls)
        if index == self.fail_at:
            raise RuntimeError("provider failure")
        if callable(self.vectors):
            return self.vectors(texts, index)
        return self.vectors if self.vectors is not None else [[1.0, 0.0] for _ in texts]


class TrackingLock:
    """Event-driven lock that exposes when the whole test wave is queued."""
    def __init__(self, waiters):
        self.lock = asyncio.Lock()
        self.waiters = waiters
        self.entered = 0
        self.all_entered = asyncio.Event()

    async def __aenter__(self):
        self.entered += 1
        if self.entered == self.waiters:
            self.all_entered.set()
        await self.lock.acquire()
        return self

    async def __aexit__(self, *exc):
        self.lock.release()


def test_default_and_constructor_validation():
    assert DEFAULT_TOP_K == 2
    for kwargs in ({"top_k": True}, {"top_k": 0}, {"keyword_weight": math.nan},
                   {"keyword_weight": 1.1}):
        with pytest.raises(ValueError):
            Retriever([case()], **kwargs)


@pytest.mark.asyncio
async def test_keyword_filters_zero_scores_and_preserves_ties():
    cases = [case("first", "甲乙", "丙丁", ["戊己"]), case("second", "甲乙", "丙丁", ["戊己"]),
             case("zero", "完全无关", "没有匹配", ["别的"])]
    got = await Retriever(cases, top_k=2).search("甲乙\n丙丁")
    assert [h.case.id for h in got.hits] == ["first", "second"]
    assert got.mode == "keyword"


@pytest.mark.asyncio
async def test_embedding_failure_falls_back_and_query_failure_keeps_case_cache():
    cases = [case("one"), case("two", "冒充客服", "要求转账", ["立即"])]
    baseline = await Retriever(cases).search("要求转账")
    llm = FakeLLM(fail_at=1)
    result = await Retriever(cases, llm).search("要求转账")
    assert [h.case.id for h in result.hits] == [h.case.id for h in baseline.hits]
    assert (result.mode, result.fallback_reason) == ("keyword_fallback", "case_embed")

    llm = FakeLLM(fail_at=2)
    retriever = Retriever(cases, llm)
    result = await retriever.search("要求转账")
    assert result.fallback_reason == "query_embed"
    assert retriever._case_vecs is not None
    llm.fail_at = None
    assert (await retriever.search("要求转账")).mode == "hybrid"
    assert len(llm.calls[0]) == len(cases)


@pytest.mark.asyncio
@pytest.mark.parametrize("vectors", [[], [[1.0]], [[0.0, 0.0]], [[float("nan"), 0]],
                                      [[float("inf"), 0]], [[1, 0], [1, 0, 1]]])
async def test_invalid_case_vectors_never_cache(vectors):
    retriever = Retriever([case(), case("two")], FakeLLM(vectors=vectors))
    result = await retriever.search("要求转账")
    assert result.mode == "keyword_fallback"
    assert result.fallback_reason == "invalid_case_vectors"
    assert retriever._case_vecs is None


@pytest.mark.asyncio
async def test_concurrent_initialization_is_singleflight():
    started, release = asyncio.Event(), asyncio.Event()

    class Blocking(FakeLLM):
        async def embed(self, texts):
            self.calls.append(texts)
            if len(self.calls) == 1:
                started.set()
                await release.wait()
                return [[1, 0] for _ in texts]
            return [[1, 0]]

    llm = Blocking()
    retriever = Retriever([case()], llm)
    tasks = [asyncio.create_task(retriever.search("要求转账")) for _ in range(6)]
    await started.wait()
    release.set()
    results = await asyncio.gather(*tasks)
    assert len(llm.calls) == 7  # 一次案例向量初始化 + 每次查询各一次
    assert all(result.mode == "hybrid" for result in results)


@pytest.mark.asyncio
async def test_concurrent_initialization_failure_is_shared_and_later_call_retries():
    started, release = asyncio.Event(), asyncio.Event()

    class FailsOnce(FakeLLM):
        async def embed(self, texts):
            self.calls.append(texts)
            if len(self.calls) == 1:
                started.set()
                await release.wait()
                raise RuntimeError("provider failure")
            return [[1, 0] for _ in texts]

    llm = FailsOnce()
    retriever = Retriever([case()], llm)
    barrier = TrackingLock(5)
    retriever._case_lock = barrier
    tasks = [asyncio.create_task(retriever.search("要求转账")) for _ in range(5)]
    await started.wait()
    await barrier.all_entered.wait()
    release.set()
    results = await asyncio.gather(*tasks)
    assert len(llm.calls) == 1
    assert all((r.mode, r.fallback_reason) == ("keyword_fallback", "case_embed") for r in results)
    assert (await retriever.search("要求转账")).mode == "hybrid"


@pytest.mark.asyncio
async def test_waiter_cancellation_does_not_cancel_initializer():
    started, release = asyncio.Event(), asyncio.Event()

    class Blocking(FakeLLM):
        async def embed(self, texts):
            self.calls.append(texts)
            if len(self.calls) == 1:
                started.set()
                await release.wait()
            return [[1, 0] for _ in texts]

    llm = Blocking()
    retriever = Retriever([case()], llm)
    barrier = TrackingLock(2)
    retriever._case_lock = barrier
    owner = asyncio.create_task(retriever.search("要求转账"))
    await started.wait()
    waiter = asyncio.create_task(retriever.search("要求转账"))
    await barrier.all_entered.wait()
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    release.set()
    assert (await owner).mode == "hybrid"
    assert len(llm.calls) == 2


@pytest.mark.asyncio
async def test_initializer_cancellation_releases_waiters_and_allows_retry():
    started, release = asyncio.Event(), asyncio.Event()

    class Blocking(FakeLLM):
        async def embed(self, texts):
            self.calls.append(texts)
            if len(self.calls) == 1:
                started.set()
                await release.wait()
            return [[1, 0] for _ in texts]

    llm = Blocking()
    retriever = Retriever([case()], llm)
    barrier = TrackingLock(3)
    retriever._case_lock = barrier
    owner = asyncio.create_task(retriever.search("要求转账"))
    await started.wait()
    waiters = [asyncio.create_task(retriever.search("要求转账")) for _ in range(2)]
    await barrier.all_entered.wait()
    owner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await owner
    results = await asyncio.gather(*waiters)
    assert all(r.fallback_reason == "case_embed" for r in results)
    assert retriever._case_vecs is None
    assert (await retriever.search("要求转账")).mode == "hybrid"
    assert len(llm.calls) == 3


@pytest.mark.asyncio
async def test_empty_skips_embed_and_semantic_only_can_rank():
    llm = FakeLLM()
    assert (await Retriever([case()], llm).search("  ")).mode == "empty"
    assert llm.calls == []
    retriever = Retriever([case()], FakeLLM(), keyword_weight=0)
    result = await retriever.search("借款先缴费")
    assert result.mode == "hybrid" and len(result.hits) == 1
    assert result.hits[0].keyword_score == 0


@pytest.mark.asyncio
async def test_l2_normalization_makes_positive_vector_scaling_rank_invariant():
    cases = [case("first", "甲乙", "一类话术"), case("second", "丙丁", "另一类话术")]

    async def rank(case_vectors, query_vector):
        def vectors(texts, call):
            return case_vectors if call == 1 else [query_vector]
        result = await Retriever(cases, FakeLLM(vectors=vectors)).search("随机输入")
        return [(hit.case.id, hit.score) for hit in result.hits]

    first = await rank([[1, 0], [0, 1]], [1, 0])
    scaled = await rank([[9, 0], [0, 3]], [7, 0])
    assert [case_id for case_id, _ in first] == [case_id for case_id, _ in scaled]
    assert [score for _, score in first] == pytest.approx([score for _, score in scaled])


@pytest.mark.asyncio
async def test_query_dimension_mismatch_falls_back_without_zip_truncation():
    llm = FakeLLM(vectors=lambda texts, call: [[1, 0] for _ in texts] if call == 1 else [[1, 0, 1]])
    result = await Retriever([case()], llm).search("要求转账")
    assert result.mode == "keyword_fallback"
    assert result.fallback_reason == "invalid_query_vector"


def test_case_fields_have_boundaries_and_name_is_in_vector_text():
    c = case(name=" 独有名称 ", tactic="甲乙", markers=["丙丁"])
    assert "名称：独有名称\n话术：甲乙\n识别点：丙丁" == _vector_text(c)
    retriever = Retriever([c])
    assert retriever._keyword_similarity_score("甲乙\n丙丁", c) == pytest.approx(1)
    assert retriever._keyword_similarity_score("甲乙丙丁", c) < 1


def test_query_uses_raw_turns_redacts_strong_values_and_obeys_budgets():
    text = "  先联系 https://example.com/" + "a" * 55 + "，再打给 13800138000，卡号 1234567890123456；扣费800元后转账。  "
    conv = Conversation(turns=[Turn(speaker="骗子", text=text), Turn(speaker="我", text="马上操作")])
    specs = [FeatureSpec(type="fee", value="手续费"), FeatureSpec(type="url", value="https://"),
             FeatureSpec(type="fee", value="手续费"), FeatureSpec(type="transfer", value="转账", source="llm")]
    specs += [FeatureSpec(type="account", value="银行账号"), FeatureSpec(type="amount", value="五万元"),
              FeatureSpec(type="escalation", value="递进升级")]
    before = conv.model_dump(), [s.model_dump() for s in specs]
    query = build_retrieval_query(conv, specs)
    assert "【第" not in query and "骗子" not in query and "example.com" not in query
    assert "13800138000" not in query and "1234567890123456" not in query
    assert "800元" in query and query.startswith("手续费")
    assert query.count("手续费") == 1
    assert all(value not in query for value in ("银行账号", "五万元", "递进升级"))
    assert len(query) <= 400
    assert before == (conv.model_dump(), [s.model_dump() for s in specs])


@pytest.mark.parametrize("length", [239, 240, 241])
def test_query_body_truncation_boundary(length):
    query = build_retrieval_query(Conversation.single("甲" * length), [])
    if length <= 240:
        assert query == "甲" * length
    else:
        assert len(query) == 240 and query == "甲" * 80 + "\n" + "甲" * 159


def test_query_feature_prefix_uses_whole_values_up_to_158_characters():
    specs = [FeatureSpec(type="fee", value="甲" * 80),
             FeatureSpec(type="transfer", value="乙" * 77),
             FeatureSpec(type="urgency", value="丙")]
    query = build_retrieval_query(Conversation.single(""), specs)
    assert query == "甲" * 80 + "\n" + "乙" * 77
    assert len(query) == 158

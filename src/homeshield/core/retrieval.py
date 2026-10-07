"""内存案例检索：分字段 bigram 与可选归一化向量加权排序。"""
import asyncio
import math
import logging
import re
from numbers import Real
from collections import Counter
from dataclasses import dataclass

from homeshield.core.features import FeatureSpec, redact_retrieval_text
from homeshield.core.llm import LLMPort
from homeshield.core.models import Conversation, KbCase

DEFAULT_TOP_K = 2
_QUERY_FEATURE_TYPES = {"isolation", "transfer", "urgency", "identity_claim", "fee"}
logger = logging.getLogger(__name__)


def _embedding_label(llm: LLMPort) -> str:
    """向量供应商标识;LLMPort 实现未暴露 embedding_label 时报 unknown。"""
    return getattr(llm, "embedding_label", "provider=unknown model=unknown")


def _field_parts(case: KbCase) -> list[str]:
    return [part for value in (case.name, case.tactic, *case.markers)
            if (part := value.strip())]


def _vector_text(case: KbCase) -> str:
    name = case.name.strip()
    tactic = case.tactic.strip()
    markers = "；".join(marker.strip() for marker in case.markers if marker.strip())
    return f"名称：{name}\n话术：{tactic}\n识别点：{markers}"


def _collapse(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip())


def build_retrieval_query(conversation: Conversation, rule_specs: list[FeatureSpec]) -> str:
    """从原始轮次生成有界检索文本，不修改对话和特征。"""
    body = "\n".join(_collapse(turn.text) for turn in conversation.turns)
    body = redact_retrieval_text(body)
    if len(body) > 240:
        body = body[:80] + "\n" + body[-159:]

    selected: list[str] = []
    seen: set[str] = set()
    size = 0
    for spec in rule_specs:
        value = spec.value.strip()
        if spec.source != "rule" or spec.type not in _QUERY_FEATURE_TYPES or not value or value in seen:
            continue
        addition = len(value) + (1 if selected else 0)
        if size + addition > 158:
            break
        selected.append(value)
        seen.add(value)
        size += addition
    prefix = "\n".join(selected)
    return f"{prefix}\n\n{body}" if prefix and body else prefix or body


@dataclass(frozen=True)
class RetrievalHit:
    case: KbCase
    keyword_score: float
    vector_score: float | None
    score: float


@dataclass(frozen=True)
class RetrievalResult:
    hits: list[RetrievalHit]
    mode: str
    fallback_reason: str | None = None


def _validated_vectors(vectors: list[list[float]], expected: int,
                       dimension: int | None = None) -> list[list[float]]:
    if not isinstance(vectors, list) or len(vectors) != expected:
        raise ValueError("embedding count mismatch")
    normalized: list[list[float]] = []
    found_dim = dimension
    for vector in vectors:
        if not isinstance(vector, (list, tuple)) or not vector:
            raise ValueError("empty embedding")
        if found_dim is None:
            found_dim = len(vector)
        if len(vector) != found_dim:
            raise ValueError("embedding dimension mismatch")
        try:
            if any(isinstance(x, bool) or not isinstance(x, Real) for x in vector):
                raise ValueError("non-numeric embedding")
            values = [float(x) for x in vector]
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("invalid embedding value") from exc
        if not all(math.isfinite(x) for x in values):
            raise ValueError("non-finite embedding")
        norm = math.sqrt(sum(x * x for x in values))
        if not math.isfinite(norm) or norm <= 0:
            raise ValueError("zero or invalid embedding norm")
        normalized.append([x / norm for x in values])
    return normalized


class Retriever:
    def __init__(self, cases: list[KbCase], llm: LLMPort | None = None,
                 top_k: int = DEFAULT_TOP_K, keyword_weight: float = 0.6):
        if isinstance(top_k, bool) or not isinstance(top_k, int) or top_k < 1:
            raise ValueError("top_k must be a positive integer")
        if (isinstance(keyword_weight, bool) or not isinstance(keyword_weight, (int, float))
                or not math.isfinite(keyword_weight) or not 0 <= keyword_weight <= 1):
            raise ValueError("keyword_weight must be finite and in [0, 1]")
        self.cases = [case.model_copy(deep=True) for case in cases]
        self.llm = llm
        self.top_k = top_k
        self.keyword_weight = keyword_weight
        self._case_vecs: list[list[float]] | None = None
        self._case_lock = asyncio.Lock()
        self._case_generation = 0
        self._case_last_failure_reason: str | None = None

    @staticmethod
    def _bigrams(text: str) -> Counter:
        text = "".join(ch for ch in text if not ch.isspace())
        return Counter(text[i:i + 2] for i in range(len(text) - 1))

    def _keyword_similarity_score(self, query: str, case: KbCase) -> float:
        q = Counter()
        for part in query.splitlines() or [query]:
            q.update(self._bigrams(part))
        if not q:
            return 0.0
        c = Counter()
        for part in _field_parts(case):
            c.update(self._bigrams(part))
        inter = sum((q & c).values())
        return inter / min(sum(q.values()), 80)

    async def _ensure_case_embeddings(self, generation: int) -> tuple[bool, str | None]:
        if self._case_vecs is not None:
            return True, None
        async with self._case_lock:
            if self._case_vecs is not None:
                return True, None
            if generation != self._case_generation:
                return False, self._case_last_failure_reason or "case_embed"
            case_texts = [_vector_text(case) for case in self.cases]
            try:
                raw = await self.llm.embed(case_texts)
            except asyncio.CancelledError:
                self._case_last_failure_reason = "case_embed"
                self._case_generation += 1
                raise
            except Exception as exc:
                self._case_last_failure_reason = "case_embed"
                self._case_generation += 1
                logger.warning("retrieval embedding failed stage=case_embed error=%s %s",
                               type(exc).__name__, _embedding_label(self.llm))
                return False, "case_embed"
            try:
                vectors = _validated_vectors(raw, len(self.cases))
            except Exception:
                self._case_last_failure_reason = "invalid_case_vectors"
                self._case_generation += 1
                logger.warning("retrieval embedding failed stage=case_embed error=invalid_response %s", _embedding_label(self.llm))
                return False, "invalid_case_vectors"
            self._case_vecs = vectors
            self._case_last_failure_reason = None
            self._case_generation += 1
            return True, None

    async def search(self, query: str) -> RetrievalResult:
        if not query.strip() or not self.cases:
            return RetrievalResult([], "empty")
        generation = self._case_generation
        keyword = [(self._keyword_similarity_score(query, case), idx)
                   for idx, case in enumerate(self.cases)]
        keyword = [(score, idx) for score, idx in keyword if score > 0]
        if self.llm is None:
            return RetrievalResult(self._rank(keyword, None), "keyword")

        ready, failure = await self._ensure_case_embeddings(generation)
        if not ready:
            return RetrievalResult(self._rank(keyword, None), "keyword_fallback", failure)
        try:
            raw_query = await self.llm.embed([query])
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("retrieval embedding failed stage=query_embed error=%s %s",
                           type(exc).__name__, _embedding_label(self.llm))
            return RetrievalResult(self._rank(keyword, None), "keyword_fallback", "query_embed")
        try:
            qv = _validated_vectors(raw_query, 1, len(self._case_vecs[0]))[0]
        except Exception:
            logger.warning("retrieval embedding failed stage=query_embed error=invalid_response %s", _embedding_label(self.llm))
            return RetrievalResult(self._rank(keyword, None), "keyword_fallback", "invalid_query_vector")

        scored: list[tuple[float, int, float, float]] = []
        for idx, case in enumerate(self.cases):
            k = self._keyword_similarity_score(query, case)
            v = sum(x * y for x, y in zip(qv, self._case_vecs[idx]))
            v = max(-1.0, min(1.0, v))
            score = self.keyword_weight * k + (1 - self.keyword_weight) * v
            scored.append((score, idx, k, v))
        scored.sort(key=lambda row: (-row[0], row[1]))
        return RetrievalResult([RetrievalHit(self.cases[i], k, v, s)
                                for s, i, k, v in scored[:self.top_k]], "hybrid")

    def _rank(self, keyword: list[tuple[float, int]], vector_scores: list[float] | None) -> list[RetrievalHit]:
        keyword.sort(key=lambda row: (-row[0], row[1]))
        return [RetrievalHit(self.cases[i], score, None, self.keyword_weight * score)
                for score, i in keyword[:self.top_k]]

    async def search_cases(self, query: str) -> list[KbCase]:
        return [hit.case for hit in (await self.search(query)).hits]

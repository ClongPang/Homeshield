"""混合检索:关键词(字符 bigram 重叠)+ 向量余弦,加权融合取 top-k。

向量侧依赖 LLMPort.embed;无向量能力时自动退化为纯关键词。
"""
from collections import Counter

from homeshield.core.llm import LLMPort
from homeshield.core.models import KbCase


class Retriever:
    def __init__(
        self,
        cases: list[KbCase],
        llm: LLMPort | None = None,
        top_k: int = 2,
        keyword_weight: float = 0.6,
    ):
        self.cases = cases
        self.llm = llm
        self.top_k = top_k
        self.keyword_weight = keyword_weight
        self._case_vecs: list[list[float]] | None = None

    @staticmethod
    def _bigrams(text: str) -> Counter:
        t = "".join(ch for ch in text if not ch.isspace())
        return Counter(t[i : i + 2] for i in range(len(t) - 1))

    def _kw_score(self, query: str, case: KbCase) -> float:
        q = self._bigrams(query)
        if not q:
            return 0.0
        c = self._bigrams(case.tactic + case.name + "".join(case.markers))
        inter = sum((q & c).values())
        return inter / min(sum(q.values()), 80)

    async def _ensure_vecs(self) -> None:
        if self._case_vecs is None and self.llm is not None:
            self._case_vecs = await self.llm.embed(
                [c.tactic + "".join(c.markers) for c in self.cases]
            )

    @staticmethod
    def _cos(a: list[float], b: list[float]) -> float:
        return sum(x * y for x, y in zip(a, b))  # 向量已归一化

    async def search(self, query: str) -> list[KbCase]:
        await self._ensure_vecs()
        qv = (
            (await self.llm.embed([query]))[0]
            if (self.llm is not None and self._case_vecs)
            else None
        )
        scored: list[tuple[float, int]] = []
        for i, case in enumerate(self.cases):
            s = self.keyword_weight * self._kw_score(query, case)
            if qv is not None:
                s += (1 - self.keyword_weight) * self._cos(qv, self._case_vecs[i])
            scored.append((s, i))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [self.cases[i] for _, i in scored[: self.top_k]]

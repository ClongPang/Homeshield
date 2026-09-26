"""判定管线编排。

run() 只做编排,四个阶段各司其职:
    _normalize_text  图片转写,失败降级"请粘贴文字"
    _extract         规则特征 + LLM 补抽与检索并行,补抽结果只喂 judge
    _judge           分级判定 + 引用校验/safe 置信门槛重试,耗尽降级"需人工判断"
    _deliver         规则下限、回复生成、落库、事件发布

阶段开关收敛在 PipelineConfig:产品默认与消融 C 相同,
A/B 是开关组合,评测与产品共用同一条代码路径。
"""
import asyncio
import logging
import time
from dataclasses import dataclass, field

from pydantic import BaseModel

from core.annotate import annotate_text
from core.errors import DegradeError
from core.events import EventBus, VerdictCompleted
from core.features import assign_ids, extract_rules, rule_floor, supplement_llm
from core.judge import Judge, judge_with_validation
from core.llm import LLMPort
from core.models import (
    Feature,
    KbCase,
    JudgeInput,
    JudgeOutput,
    Level,
    Message,
    Mode,
    max_level,
)
from core.reply import ReplyGenerator
from core.repo import Repos
from core.retrieval import Retriever

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PipelineConfig:
    """消融开关:A 全关,B 仅检索,C 全开,D=C+分级语义,E=D+机制内联标注。

    product_default = E(2026-09-26 证伪翻转):双侧集 55 条上 E 对 C
    Recall 持平(0.758)、FPR 0.045→0、FPR(strict) 持平,判据通过。
    消融 C 显式固定为旧默认,保持基线可比。见《判定模型_设计教训》§4。
    """

    llm_features: bool = True
    retrieval: bool = True
    constrain_citations: bool = True
    graded_semantics: bool = True  # 重构五(证伪通过)
    inline_annotation: bool = True  # 重构二(证伪通过)

    @classmethod
    def ablation_a(cls) -> "PipelineConfig":
        return cls(False, False, False, False, False)

    @classmethod
    def ablation_b(cls) -> "PipelineConfig":
        return cls(False, True, False, False, False)

    @classmethod
    def ablation_c(cls) -> "PipelineConfig":
        """旧默认(无分级语义/无内联标注),消融基线保持可比。"""
        return cls(True, True, True, False, False)

    @classmethod
    def ablation_d(cls) -> "PipelineConfig":
        """C + 分级语义('安全提醒≠识别')。"""
        return cls(True, True, True, True, False)

    @classmethod
    def ablation_e(cls) -> "PipelineConfig":
        """D + 机制内联标注(= 当前 product_default)。"""
        return cls(True, True, True, True, True)

    @classmethod
    def product_default(cls) -> "PipelineConfig":
        return cls()


@dataclass(frozen=True)
class Extraction:
    """阶段 2 产物:特征、检索案例、规则下限。"""

    features: list[Feature]
    cases: list[KbCase]
    rule_floor: Level


class PipelineResult(BaseModel):
    query_id: int
    verdict_id: int | None = None
    verdict: JudgeOutput | None = None
    reply: str
    latency_ms: int
    rule_floor_level: Level = Level.SAFE
    degraded: bool = False


@dataclass
class Pipeline:
    repos: Repos
    llm: LLMPort
    retriever: Retriever
    judge: Judge
    reply_gen: ReplyGenerator
    bus: EventBus
    judge_retries: int = 2
    safe_confidence_floor: int = 0  # safe 置信门槛(<=0 关);mock 置信分合成,装配层按模式传入
    config: PipelineConfig = field(default_factory=PipelineConfig.product_default)

    async def run(self, message: Message, query_id: int) -> PipelineResult:
        t0 = time.monotonic()
        try:
            text = await self._normalize_text(message)
            extraction = await self._extract(text)
            verdict = await self._judge(text, extraction)
        except DegradeError as de:
            return self._degraded(query_id, de.user_message, t0)
        return await self._deliver(message, query_id, text, extraction, verdict, t0)

    # ---- 阶段 1:归一化 ------------------------------------------------
    async def _normalize_text(self, message: Message) -> str:
        if message.content_type.value != "image":
            return message.content
        try:
            return await self.llm.transcribe_image(message.content, "转写图片中的文字")
        except DegradeError:
            raise
        except Exception as e:  # 网络/格式失败 → 降级
            logger.warning("image transcribe failed, degrade to text prompt", exc_info=True)
            raise DegradeError("图片看不清,请把内容打成文字发我", f"transcribe failed: {e}") from e

    # ---- 阶段 2:特征抽取 + 检索 ----------------------------------------
    async def _extract(self, text: str) -> Extraction:
        rule_specs = extract_rules(text)
        # 检索 query:特征值 + 原文片段——纯特征值在弱特征消息(如仅卡号)下失效
        retrieval_query = (" ".join(s.value for s in rule_specs) + " " + text[:80]).strip()
        sup_task = (
            asyncio.ensure_future(supplement_llm(self.llm, text))
            if self.config.llm_features
            else None
        )
        ret_task = (
            asyncio.ensure_future(self.retriever.search(retrieval_query))
            if self.config.retrieval
            else None
        )
        # 两个任务已在事件循环内并行调度,顺序 await 不改变并发性;
        # 检索/补抽失败不致命:退化为"仅规则特征、无案例"继续判定
        sup: list = []
        cases: list[KbCase] = []
        try:
            sup = await sup_task if sup_task else []
        except Exception:
            logger.warning("llm feature supplement failed", exc_info=True)
        try:
            cases = await ret_task if ret_task else []
        except Exception:
            logger.warning("retrieval failed, judge without cases", exc_info=True)
        return Extraction(
            features=assign_ids(rule_specs + sup),
            cases=cases,
            rule_floor=rule_floor(rule_specs),
        )

    # ---- 阶段 3:判定 ----------------------------------------------------
    async def _judge(self, text: str, extraction: Extraction) -> JudgeOutput:
        annotated = (
            annotate_text(text, extraction.features)
            if self.config.inline_annotation
            else None
        )
        inp = JudgeInput(
            text=text[:2000],
            features=extraction.features,
            cases=extraction.cases,
            annotated_text=annotated,
            graded_semantics=self.config.graded_semantics,
        )
        return await judge_with_validation(
            inp,
            self.judge,
            constrained=self.config.constrain_citations,
            retries=self.judge_retries,
            safe_confidence_floor=self.safe_confidence_floor,
        )

    # ---- 阶段 4:交付 ----------------------------------------------------
    async def _deliver(
        self,
        message: Message,
        query_id: int,
        text: str,
        extraction: Extraction,
        verdict: JudgeOutput,
        t0: float,
    ) -> PipelineResult:
        final_level = max_level(extraction.rule_floor, verdict.level)
        cited = list(verdict.cited_ids)
        if final_level is not verdict.level:
            for f in extraction.features:  # 下限抬升→强制引用驱动特征,保证依据可解释
                if f.type in ("isolation", "transfer") and f.id not in cited:
                    cited.append(f.id)
        verdict = verdict.model_copy(update={"level": final_level, "cited_ids": cited})

        reply = await self.reply_gen.generate(verdict, extraction.features, extraction.cases)
        latency_ms = int((time.monotonic() - t0) * 1000)
        verdict_id = self.repos.verdict.insert(
            query_id,
            verdict.level,
            cited,
            [f.model_dump() for f in extraction.features],
            verdict.reason,
            reply,
            latency_ms,
            self.judge.mode if isinstance(self.judge.mode, Mode) else Mode(self.judge.mode),
        )
        await self.bus.publish(
            VerdictCompleted(
                message=message, verdict=verdict, reply=reply,
                query_id=query_id, verdict_id=verdict_id,
            )
        )
        return PipelineResult(
            query_id=query_id,
            verdict_id=verdict_id,
            verdict=verdict,
            reply=reply,
            latency_ms=latency_ms,
            rule_floor_level=extraction.rule_floor,
        )

    def _degraded(self, query_id: int, reply: str, t0: float, floor: Level = Level.SAFE) -> PipelineResult:
        return PipelineResult(
            query_id=query_id,
            reply=reply,
            latency_ms=int((time.monotonic() - t0) * 1000),
            rule_floor_level=floor,
            degraded=True,
        )

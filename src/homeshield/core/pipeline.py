"""判定管线编排。

run() 只做编排,四个阶段各司其职:
    _extract_message_text                图片转写,失败降级"请粘贴文字"
    _extract_features_and_cases          规则特征 + LLM 补抽与检索并行,补抽结果只喂 judge
    _judge_conversation                  分级判定 + 引用校验/safe 置信门槛重试,耗尽降级"需人工判断"
    _persist_verdict_and_publish_event   规则下限、回复生成、落库、事件发布

阶段开关收敛在 PipelineConfig:产品默认与消融 C 相同,
A/B 是开关组合,评测与产品共用同一条代码路径。
"""
import asyncio
import json
import logging
import time
from dataclasses import dataclass, field

from pydantic import BaseModel

from homeshield.core.annotate import annotate_text
from homeshield.core.errors import DegradeError
from homeshield.core.events import EventBus, VerdictCompleted
from homeshield.core.features import (
    assign_feature_ids,
    detect_escalation_feature,
    extract_rule_features,
    extract_supply_features,
    get_rule_risk_floor,
    is_ask_feature,
    is_trust_feature,
    supplement_features_with_llm,
    FeatureSpec,
    extract_strong_values,
)
from homeshield.core.judge import Judge, judge_with_validation
from homeshield.core.llm import LLMPort
from homeshield.core.models import (
    Conversation,
    Feature,
    Turn,
    KbCase,
    JudgeInput,
    JudgeOutput,
    Level,
    Message,
    Mode,
    max_level,
)
from homeshield.core.reply import ReplyGenerator, add_delivery_notice
from homeshield.core.repo import Repos
from homeshield.core.retrieval import Retriever

logger = logging.getLogger(__name__)
_NON_MONEY_TRANSFER_VALUES = {"给我验证码"}  # 旧规则将其标为 transfer;跨条资金下限不得借此触发

# 判定行为语义版本:凡影响判定输出的变更(词表/提示词/分级语义/检索/模型默认)
# 必须递增;断点续跑与评测缓存据此失效,防止用旧引擎的分数冒充新引擎。
PIPELINE_VERSION = "1.2.0"


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
    supply_features: bool = False

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
    """阶段 2 产物:会话、特征、检索案例、规则下限。"""

    features: list[Feature]
    cases: list[KbCase]
    rule_floor: Level
    conversation: Conversation
    rule_specs: list[FeatureSpec] = field(default_factory=list)


@dataclass(frozen=True)
class SupplyItem:
    query_id: int
    created_at: int
    text: str
    specs: list[FeatureSpec]
    source: str
    matched_values: list[str]
    age_seconds: int


class PipelineResult(BaseModel):
    query_id: int
    verdict_id: int | None = None
    verdict: JudgeOutput | None = None
    reply: str
    latency_ms: int
    rule_floor_level: Level = Level.SAFE
    degraded: bool = False


_TRANSCRIBE_HINT = (
    "转写图片中的对话内容:每条消息单独一行,以发言人加冒号开头"
    "(如 '对方:' 或 '我:');通知/公告类图片输出为一行,以 '内容:' 开头。"
)
_SPEAKER_LINE_RE = __import__("re").compile(r"^(对方|我|内容)\s*[::]\s*(.+)$")


def _to_conversation(text: str, content_type: str) -> Conversation:
    """归一化文本 → 会话(重构四)。

    - 图片转写:按'发言人:'逐行分轮(≥2 行匹配才视为多轮,否则单轮兜底);
    - 文本:解析【第N轮】标记(评测样本注入会话结构的通道),无标记即单轮。
    """
    if content_type == "image":
        turns = [
            (m.group(1), m.group(2).strip())
            for line in text.splitlines()
            if (m := _SPEAKER_LINE_RE.match(line.strip()))
        ]
        if len(turns) >= 2:
            return Conversation(turns=[Turn(speaker=sp, text=t) for sp, t in turns])
        return Conversation.single(text)
    return Conversation.from_marked(text)


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
    incident_idle_seconds: int = 21600
    supply_window_seconds: int = 604800
    supply_max_items: int = 10

    async def run(self, message: Message, query_id: int) -> PipelineResult:
        t0 = time.monotonic()
        try:
            text = await self._extract_message_text(message)
            if message.content_type.value == "image":
                try:
                    self.repos.query.update_transcript(query_id, text)
                except Exception:
                    logger.warning("image transcript persistence failed", exc_info=True)
            conversation = _to_conversation(text, message.content_type.value)
            extraction = await self._extract_features_and_cases(conversation)
            supplied = self._supply(message, query_id, text) if self.config.supply_features else []
            verdict = await self._judge_conversation(conversation, extraction)
        except DegradeError as de:
            return self._build_degraded_result(query_id, de.user_message, t0)
        return await self._persist_verdict_and_publish_event(
            message, query_id, conversation.render(), extraction, verdict, t0, supplied
        )

    # ---- 阶段 1:归一化 ------------------------------------------------
    async def _extract_message_text(self, message: Message) -> str:
        if message.content_type.value != "image":
            return message.content
        try:
            return await self.llm.transcribe_image(message.content, _TRANSCRIBE_HINT)
        except DegradeError:
            raise
        except Exception as e:  # 网络/格式失败 → 降级
            logger.warning("image transcribe failed, degrade to text prompt", exc_info=True)
            raise DegradeError("图片看不清，请把内容打成文字发我", f"transcribe failed: {e}") from e

    # ---- 阶段 2:特征抽取 + 检索 ----------------------------------------
    async def _extract_features_and_cases(self, conversation: Conversation) -> Extraction:
        rendered = conversation.render()
        rule_specs: list[FeatureSpec] = []
        for idx, turn in enumerate(conversation.turns, 1):
            for spec in extract_rule_features(turn.text):
                spec.turn = idx  # 逐轮归属:升级检测与证据定位依赖轮次
                rule_specs.append(spec)
        escalation = detect_escalation_feature(rule_specs)
        if escalation is not None:
            rule_specs.append(escalation)
        # 检索 query:特征值 + 原文片段——纯特征值在弱特征消息(如仅卡号)下失效
        retrieval_query = (" ".join(s.value for s in rule_specs) + " " + rendered[:80]).strip()
        sup_task = (
            asyncio.ensure_future(supplement_features_with_llm(self.llm, rendered))
            if self.config.llm_features
            else None
        )
        ret_task = (
            asyncio.ensure_future(self.retriever.search_cases(retrieval_query))
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
            features=assign_feature_ids(rule_specs + sup),
            cases=cases,
            rule_floor=get_rule_risk_floor(rule_specs),
            conversation=conversation,
            rule_specs=rule_specs,
        )

    def _supply(self, message: Message, query_id: int, current_text: str) -> list[SupplyItem]:
        try:
            rows = self.repos.query.supply_context(
                query_id, message.user_id, self.supply_window_seconds
            )
            current_values = extract_strong_values(current_text)
            selected: list[tuple[dict, str, str, list[str]]] = []
            for row in reversed(rows):
                prior_text = (row["transcript"] or "") if row["content_type"] == "image" else row["content"]
                same_incident = (row["incident_id"] is not None and
                                 row["incident_id"] == row["current_incident_id"])
                matched = (sorted(current_values & extract_strong_values(prior_text))
                           if not same_incident else [])
                if not same_incident and not matched:
                    continue
                selected.append((row, prior_text, "same_incident" if same_incident else "feature_match", matched))
            selected = selected[-self.supply_max_items:] if self.supply_max_items > 0 else []
            return [SupplyItem(
                    query_id=int(row["id"]), created_at=int(row["created_at"]),
                    text=prior_text, specs=extract_supply_features(prior_text),
                    source=source, matched_values=matched,
                    age_seconds=max(0, int(row["current_created_at"]) - int(row["created_at"])),
                ) for row, prior_text, source, matched in selected]
        except Exception:
            logger.warning("supply failed, judge current message only", exc_info=True)
            return []

    @staticmethod
    def _age_label(seconds: int) -> str:
        if seconds < 3600:
            return f"{max(1, seconds // 60)}分钟"
        if seconds < 86400:
            return f"{seconds // 3600}小时"
        return f"{seconds // 86400}天"

    def _cross_message_features(
        self, supplied: list[SupplyItem], extraction: Extraction,
    ) -> tuple[list[FeatureSpec], Level]:
        if not supplied:
            return [], Level.SAFE
        prior_specs = [spec.model_copy(update={"turn": i})
                       for i, item in enumerate(supplied, 1) for spec in item.specs]
        current_specs = [spec.model_copy(update={"turn": len(supplied) + (spec.turn or 1)})
                         for spec in extraction.rule_specs]
        trust = [
            (item, spec) for item in supplied for spec in item.specs
            if is_trust_feature(spec)
        ]
        ask = [spec for spec in extraction.rule_specs if is_ask_feature(spec)]
        result: list[FeatureSpec] = []
        floor = Level.SAFE
        # 只用前情铺垫与本条索取判定 S1。前情若还含更早的索取，
        # detect_escalation_feature 的最早索取轮次不能遮蔽本条的升级。
        escalation_specs = ([s for s in prior_specs if is_trust_feature(s)] +
                            [s for s in current_specs if is_ask_feature(s)])
        if trust and ask and detect_escalation_feature(escalation_specs):
            result.append(FeatureSpec(
                type="escalation", source="rule", confidence=8, origin="prior",
                value=f"约{self._age_label(trust[0][0].age_seconds)}前的消息已建立铺垫(身份/利益/恐吓/情感),本条出现索取(跨消息渐进话术)",
                evidence_span="",
            ))
            floor = Level.SUSPICIOUS
        if (extraction.rule_floor is not Level.DANGEROUS
                and any(spec.type == "isolation" for spec in prior_specs)
                and any(spec.type == "transfer" and spec.value not in _NON_MONEY_TRANSFER_VALUES
                        for spec in extraction.rule_specs)):
            result.append(FeatureSpec(
                type="isolation", source="rule", confidence=5, origin="prior",
                value="前情有保密要求", evidence_span="",
            ))
            floor = Level.DANGEROUS
        return result[:2], floor

    # ---- 阶段 3:判定 ----------------------------------------------------
    async def _judge_conversation(self, conversation: Conversation, extraction: Extraction) -> JudgeOutput:
        rendered = conversation.render()
        cap = 4000 if conversation.is_multi_turn else 2000
        annotated = (
            annotate_text(rendered, extraction.features)
            if self.config.inline_annotation
            else None
        )
        inp = JudgeInput(
            text=rendered[:cap],
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
    async def _persist_verdict_and_publish_event(
        self,
        message: Message,
        query_id: int,
        text: str,
        extraction: Extraction,
        verdict: JudgeOutput,
        t0: float,
        supplied: list[SupplyItem] | None = None,
    ) -> PipelineResult:
        supplied = supplied or []
        synthetic = []
        cross_floor = Level.SAFE
        if supplied:
            try:
                synthetic_specs, cross_floor = self._cross_message_features(supplied, extraction)
                for spec in synthetic_specs:
                    feature = assign_feature_ids([spec])[0].model_copy(
                        update={"id": f"F{len(extraction.features) + len(synthetic) + 1:02d}"}
                    )
                    synthetic.append(feature)
            except Exception:
                logger.warning("supply synthesis failed, judge current message only", exc_info=True)
                supplied = []
                synthetic = []
                cross_floor = Level.SAFE
        baseline_level = max_level(extraction.rule_floor, verdict.level)
        final_level = max_level(baseline_level, cross_floor)
        cross_raised = final_level is not baseline_level
        cited = list(verdict.cited_ids)
        reason = verdict.reason
        if baseline_level is not verdict.level:
            for f in extraction.features:  # 下限抬升→强制引用驱动特征,保证依据可解释
                if f.type in ("isolation", "transfer") and f.id not in cited:
                    cited.append(f.id)
        basis_override = None
        if cross_raised:
            driver = next((f for f in synthetic if f.type == "isolation"), None)
            current_type = "transfer" if driver else None
            if driver is None:
                driver = next((f for f in synthetic if f.type == "escalation"), None)
                current_type = next((s.type for s in extraction.rule_specs if is_ask_feature(s)), None)
            current = next((f for f in extraction.features if f.type == current_type
                            and (current_type != "transfer" or f.value not in _NON_MONEY_TRANSFER_VALUES)), None)
            for f in (driver, current):
                if f is not None and f.id not in cited:
                    cited.append(f.id)
            reason = f"跨消息依据：{driver.id if driver else ''}、{current.id if current else ''}"
            basis_override = ("前情有保密要求，本条又要求转账（跨消息）" if cross_floor is Level.DANGEROUS
                              else "前情已有铺垫，本条出现索取（跨消息）")
        verdict = verdict.model_copy(update={"level": final_level, "cited_ids": cited, "reason": reason})

        if basis_override:
            reply = await self.reply_gen.generate(
                verdict, extraction.features, extraction.cases, basis_override=basis_override
            )
        else:
            reply = await self.reply_gen.generate(verdict, extraction.features, extraction.cases)
        latency_ms = int((time.monotonic() - t0) * 1000)
        snapshot = None
        if supplied:
            snapshot = json.dumps({
                "v": 1,
                "prior": [{"query_id": item.query_id, "age_seconds": item.age_seconds,
                           "excerpt": item.text[:200], "source": item.source,
                           "matched_values": item.matched_values} for item in supplied],
                "synthetic_ids": [f.id for f in synthetic], "floor_level": cross_floor.value,
            }, ensure_ascii=False)
        verdict_id = self.repos.verdict.insert(
            query_id,
            verdict.level,
            cited,
            [f.model_dump() for f in extraction.features + synthetic],
            verdict.reason,
            reply,
            latency_ms,
            self.judge.mode if isinstance(self.judge.mode, Mode) else Mode(self.judge.mode),
            context_snapshot=snapshot,
        )
        await self.bus.publish(
            event := VerdictCompleted(
                message=message, verdict=verdict, reply=reply,
                query_id=query_id, verdict_id=verdict_id,
            )
        )
        user_reply = add_delivery_notice(reply, event.queryer_notice)
        return PipelineResult(
            query_id=query_id,
            verdict_id=verdict_id,
            verdict=verdict,
            reply=user_reply,
            latency_ms=latency_ms,
            rule_floor_level=max_level(extraction.rule_floor, cross_floor),
        )

    def _build_degraded_result(self, query_id: int, reply: str, t0: float, floor: Level = Level.SAFE) -> PipelineResult:
        return PipelineResult(
            query_id=query_id,
            reply=reply,
            latency_ms=int((time.monotonic() - t0) * 1000),
            rule_floor_level=floor,
            degraded=True,
        )

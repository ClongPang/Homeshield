"""组合根(Composition Root):唯一知道具体实现的地方。"""
import json
import asyncio
from dataclasses import dataclass
from pathlib import Path

from psycopg_pool import AsyncConnectionPool

from homeshield.core.config import Settings
from homeshield.core.channels.wecom import WeComChannel
from homeshield.core.db import init_schema, make_pool
from homeshield.core.events import EventBus
from homeshield.core.relations import RelationService
from homeshield.core.judge import Judge, LLMJudge, MockJudge
from homeshield.core.llm import LLMPort, make_llm
from homeshield.core.models import KbCase
from homeshield.core.notifier import AlertBroker, AlertRouter, wire_alerts
from homeshield.core.pipeline import Pipeline, PipelineConfig
from homeshield.core.reply import LLMReply, ReplyGenerator, TemplateReply
from homeshield.core.repo import Repos, make_repos
from homeshield.core.retrieval import Retriever
from homeshield.core.verification import VerificationService

KB_PATH = Path(__file__).parent / "knowledge" / "cases.json"


def load_cases(path: Path = KB_PATH) -> list[KbCase]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return [KbCase(**case) for case in data]


@dataclass
class Deps:
    settings: Settings
    pool: AsyncConnectionPool
    repos: Repos
    bus: EventBus
    broker: AlertBroker
    alert_router: AlertRouter
    wecom: WeComChannel | None
    llm: LLMPort
    retriever: Retriever
    judge: Judge
    reply: ReplyGenerator
    pipeline: Pipeline
    verification: VerificationService
    relations: RelationService
    poll_signal: asyncio.Event


def build_deps(settings: Settings, pool: AsyncConnectionPool | None = None) -> Deps:
    """Build process-local services without opening the database pool."""
    pool = pool or make_pool(settings.database_url)
    repos = make_repos(pool)
    bus = EventBus()
    broker = AlertBroker()
    wecom = WeComChannel(settings) if settings.wecom_corpid else None
    llm = make_llm(settings)
    retriever = Retriever(
        load_cases(), llm if settings.llm_enabled and settings.get_embedding_provider() else None
    )
    judge: Judge = LLMJudge(llm) if settings.llm_enabled else MockJudge(settings.mock_judge_delay_seconds)
    reply: ReplyGenerator = LLMReply(llm) if settings.llm_enabled else TemplateReply()
    router = wire_alerts(bus, broker, repos, base_url=settings.public_base_url)
    if wecom is not None and wecom.api_ready and settings.wecom_agent_id:
        router.wecom = wecom
    pipeline = _assemble_pipeline(repos=repos, llm=llm, retriever=retriever, judge=judge,
                                  reply_gen=reply, bus=bus, settings=settings)
    verification = VerificationService(repos, pipeline)
    relations = RelationService(repos, settings.max_relations, settings.invite_code_ttl_days)
    return Deps(
        settings=settings,
        pool=pool,
        repos=repos,
        bus=bus,
        broker=broker,
        alert_router=router,
        wecom=wecom,
        llm=llm,
        retriever=retriever,
        judge=judge,
        reply=reply,
        pipeline=pipeline,
        verification=verification,
        relations=relations,
        poll_signal=asyncio.Event(),
    )


async def initialize_deps(deps: Deps) -> None:
    if deps.pool.closed:
        await deps.pool.open(wait=True)
    await init_schema(deps.pool)


def _assemble_pipeline(*, repos: Repos, llm: LLMPort, retriever: Retriever, judge: Judge,
                       reply_gen: ReplyGenerator, bus: EventBus, settings: Settings,
                       config: PipelineConfig | None = None) -> Pipeline:
    """build_deps 与 make_pipeline 共用的管线装配,防止两处参数漂移。"""
    return Pipeline(
        repos=repos,
        llm=llm,
        retriever=retriever,
        judge=judge,
        reply_gen=reply_gen,
        bus=bus,
        judge_retries=settings.judge_retries,
        safe_confidence_floor=settings.safe_confidence_floor if settings.llm_enabled else 0,
        config=config or PipelineConfig.product_default(),
        incident_idle_seconds=settings.incident_idle_seconds,
        supply_window_seconds=settings.supply_window_seconds,
        supply_max_items=settings.supply_max_items,
    )


def make_pipeline(deps: Deps, config: PipelineConfig | None = None) -> Pipeline:
    return _assemble_pipeline(repos=deps.repos, llm=deps.llm, retriever=deps.retriever,
                              judge=deps.judge, reply_gen=deps.reply, bus=deps.bus,
                              settings=deps.settings, config=config)

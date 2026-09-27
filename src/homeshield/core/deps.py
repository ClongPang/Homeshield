"""组合根(Composition Root):唯一知道具体实现的地方。

领域模块经 Protocol 依赖抽象;本模块按 Settings 把 Mock/OpenAI、SQLite、
事件总线装配成 Deps,server / cli / eval 都从这里取依赖。
"""
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from homeshield.core.config import Settings
from homeshield.core.binding import BindingService
from homeshield.core.channels.wechat import WeChatChannel
from homeshield.core.db import connect, init_schema
from homeshield.core.events import EventBus
from homeshield.core.groups import GroupService
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
    return [KbCase(**c) for c in data]


@dataclass
class Deps:
    settings: Settings
    conn: sqlite3.Connection
    repos: Repos
    bus: EventBus
    broker: AlertBroker
    alert_router: AlertRouter
    wechat: WeChatChannel | None
    llm: LLMPort
    retriever: Retriever
    judge: Judge
    reply: ReplyGenerator
    pipeline: Pipeline
    verification: VerificationService
    binding: BindingService
    groups: GroupService


def build_deps(settings: Settings) -> Deps:
    conn = connect(settings.db_path)
    init_schema(conn) # 幂等的操作
    repos = make_repos(conn) # 各种数据库的连接对象
    bus = EventBus() # 事件消息总线
    broker = AlertBroker()
    # 微信通道单例:回调签名 + 客服接口回复 + 模板消息共用一个实例(token 缓存随之生效)
    wechat = WeChatChannel(settings) if settings.wechat_token else None
    llm = make_llm(settings) # 模型对象实例
    # EMBED 供应商未配置时传 None,检索退化为纯关键词
    retriever = Retriever(
        load_cases(), llm if (settings.use_llm and settings.embed_endpoint()) else None
    )
    judge: Judge = LLMJudge(llm) if settings.use_llm else MockJudge()
    reply = (
        LLMReply(llm) if settings.use_llm else TemplateReply()
    )
    router = wire_alerts(
        bus,broker,repos,base_url=settings.public_base_url,
        template_id=settings.wechat_template_id,multi_template_id=settings.wechat_multi_template_id,
    )
    # 模板消息发送需要三件套(appid/secret 换 token,template_id 指模板);
    # 只配 wechat_token 时回调链路可用,告警模板保持关闭而非发送时失败
    if (wechat is not None and settings.wechat_appid and settings.wechat_secret
            and (settings.wechat_template_id or settings.wechat_multi_template_id)):
        router.wechat = wechat
    pipeline = Pipeline(
        repos=repos,
        llm=llm,
        retriever=retriever,
        judge=judge,
        reply_gen=reply,
        bus=bus,
        judge_retries=settings.judge_retries,
        # mock 的置信分是合成值,不参与 safe 门槛;门槛针对 LLM 校准不准
        safe_confidence_floor=settings.safe_confidence_floor if settings.use_llm else 0,
    )
    verification = VerificationService(repos, pipeline)
    binding = BindingService(
        repos,
        max_families=settings.max_families,
        max_members=settings.max_members,
        code_ttl_days=settings.bind_code_ttl_days,
        max_groups=settings.max_groups,
    )
    return Deps(
        settings=settings,
        conn=conn,
        repos=repos,
        bus=bus,
        broker=broker,
        alert_router=router,
        wechat=wechat,
        llm=llm,
        retriever=retriever,
        judge=judge,
        reply=reply,
        pipeline=pipeline,
        verification=verification,
        binding=binding,
        groups=GroupService(repos,settings.max_members),
    )


def make_pipeline(deps: Deps, config: PipelineConfig | None = None) -> Pipeline:
    return Pipeline(
        repos=deps.repos,
        llm=deps.llm,
        retriever=deps.retriever,
        judge=deps.judge,
        reply_gen=deps.reply,
        bus=deps.bus,
        judge_retries=deps.settings.judge_retries,
        safe_confidence_floor=deps.settings.safe_confidence_floor if deps.settings.use_llm else 0,
        config=config or PipelineConfig.product_default(),
    )

"""运行配置,从 .env 与环境变量读取,集中构造后经构造器注入;装配在 core/deps.py。

供应商解析:任何 <NAME>_API_KEY(可选搭配 <NAME>_BASE_URL / <NAME>_MODEL)
即注册一个供应商;任务分工由 CHAT_PROVIDER / TRANSCRIBE_PROVIDER /
EMBED_PROVIDER 指名。新增供应商只改 .env,不加代码。
"""
from dataclasses import dataclass, field
import os
import re

from dotenv import load_dotenv


@dataclass(frozen=True)
class Provider:
    """一个 OpenAI 兼容端点。"""

    name: str
    api_key: str
    base_url: str
    model: str


@dataclass(frozen=True)
class Settings:
    mode: str = "mock"  # mock | llm
    providers: dict[str, Provider] = field(default_factory=dict)
    chat_provider_name: str = ""  # 判定 / 特征补抽 / 回复
    transcribe_provider_name: str = ""  # 图片转写;留空回落 chat
    embed_provider_name: str = ""  # 向量检索;留空退化为纯关键词
    embed_dimensions: int | None = None  # 截断维度(MRL);None=模型默认。切换须重嵌案例库并升 PIPELINE_VERSION
    embed_batch_size: int = 20  # 单批条数;实测 v4/v3 端点仅收 10,qwen3.7 系收 20
    # 企业微信(微信客服):corpid/密钥用于 API 调用,token/aes_key 用于回调验签与加解密
    wecom_corpid: str = ""
    wecom_agent_id: str = ""
    wecom_app_secret: str = ""  # 自建应用密钥(需在控制台授权为微信客服可调用应用)
    wecom_kf_secret: str = ""  # 微信客服自身密钥(可选,优先级高于应用密钥)
    wecom_token: str = ""
    wecom_aes_key: str = ""
    wecom_contact_secret: str = ""  # 通讯录同步助手密钥(自助入录写通讯录用,可选)
    push_self_enroll: bool = False  # 手机号未命中时是否自助入录;通讯录写权限属企业级信任决策,默认关
    push_state_ttl: int = 600  # OAuth 绑定凭证有效期(秒),一次性防重放
    wecom_plugin_qr_path: str = "deploy/assets/wecom_plugin_qr.png"  # 微信插件邀请二维码资产,运营者可替换
    wecom_kf_qr_path: str = "deploy/assets/wecom_kf_qr.png"  # 微信客服入口二维码资产(/kf 落地页展示)
    public_base_url: str = ""
    max_relations: int = 10
    invite_code_ttl_days: int = 7
    correction_window_days: int = 7
    db_path: str = "data/kb_build.db"
    database_url: str = "postgresql://homeshield:homeshield@localhost:5432/homeshield"
    judge_retries: int = 2  # 引用校验失败重试上限
    safe_confidence_floor: int = 60  # safe 判定最低置信(llm 模式生效);低于则降级"拿不准"
    mock_judge_delay_seconds: float = 0  # 压测用 mock 延迟,正常运行保持 0
    incident_idle_seconds: int = 21600
    supply_window_seconds: int = 604800
    supply_max_items: int = 10
    recovery_stale_seconds: int = 300
    recovery_sweep_seconds: int = 60
    outbound_lease_seconds: int = 60
    outbound_max_attempts: int = 5

    @classmethod
    def load(cls, env_file: str | None = None) -> "Settings":
        # env_file 为 None 时 load_dotenv 自行向上查找 .env,与无参调用等价
        load_dotenv(env_file)
        providers: dict[str, Provider] = {}
        for key, value in os.environ.items():
            match = re.fullmatch(r"([A-Z][A-Z0-9_]*)_API_KEY", key)
            if not match or not value:
                continue
            name = match.group(1)
            providers[name] = Provider(
                name=name,
                api_key=value,
                base_url=os.getenv(f"{name}_BASE_URL", ""),
                model=os.getenv(f"{name}_MODEL", ""),
            )
        return cls(
            mode=os.getenv("MODE", "mock").strip().lower(),
            providers=providers,
            chat_provider_name=os.getenv("CHAT_PROVIDER", "").upper(),
            transcribe_provider_name=os.getenv("TRANSCRIBE_PROVIDER", "").upper(),
            embed_provider_name=os.getenv("EMBED_PROVIDER", "").upper(),
            embed_dimensions=(int(d) if (d := os.getenv("EMBED_DIMENSIONS", "").strip()) else None),
            embed_batch_size=max(1, int(os.getenv("EMBED_BATCH_SIZE", "20"))),
            wecom_corpid=os.getenv("WECOM_CORPID", ""),
            wecom_agent_id=os.getenv("WECOM_AGENT_ID", ""),
            wecom_app_secret=os.getenv("WECOM_APP_SECRET", ""),
            wecom_kf_secret=os.getenv("WECOM_KF_SECRET", ""),
            wecom_token=os.getenv("WECOM_TOKEN", ""),
            wecom_aes_key=os.getenv("WECOM_AES_KEY", ""),
            wecom_contact_secret=os.getenv("WECOM_CONTACT_SECRET", ""),
            push_self_enroll=os.getenv("PUSH_SELF_ENROLL", "false").strip().lower() in ("1", "true", "yes", "on"),
            push_state_ttl=int(os.getenv("PUSH_STATE_TTL", "600")),
            wecom_plugin_qr_path=os.getenv("WECOM_PLUGIN_QR_PATH", "deploy/assets/wecom_plugin_qr.png"),
            wecom_kf_qr_path=os.getenv("WECOM_KF_QR_PATH", "deploy/assets/wecom_kf_qr.png"),
            public_base_url=os.getenv("PUBLIC_BASE_URL", ""),
            max_relations=int(os.getenv("MAX_RELATIONS", "10")),
            invite_code_ttl_days=int(os.getenv("INVITE_CODE_TTL_DAYS", "7")),
            correction_window_days=int(os.getenv("CORRECTION_WINDOW_DAYS", "7")),
            db_path=os.getenv("DB_PATH", "data/kb_build.db"),
            database_url=os.getenv(
                "DATABASE_URL", "postgresql://homeshield:homeshield@localhost:5432/homeshield"
            ),
            safe_confidence_floor=int(os.getenv("SAFE_CONFIDENCE_FLOOR", "60")),
            mock_judge_delay_seconds=max(0.0, float(os.getenv("MOCK_JUDGE_DELAY_SECONDS", "0"))),
            incident_idle_seconds=int(os.getenv("INCIDENT_IDLE_SECONDS", "21600")),
            supply_window_seconds=int(os.getenv("SUPPLY_WINDOW_SECONDS", "604800")),
            supply_max_items=int(os.getenv("SUPPLY_MAX_ITEMS", "10")),
            recovery_stale_seconds=int(os.getenv("RECOVERY_STALE_SECONDS", "300")),
            recovery_sweep_seconds=int(os.getenv("RECOVERY_SWEEP_SECONDS", "60")),
            outbound_lease_seconds=int(os.getenv("OUTBOUND_LEASE_SECONDS", "60")),
            outbound_max_attempts=int(os.getenv("OUTBOUND_MAX_ATTEMPTS", "5")),
        )

    def validate_recovery(self) -> None:
        if min(self.recovery_stale_seconds, self.recovery_sweep_seconds,
               self.outbound_max_attempts) <= 0 or self.outbound_lease_seconds < 30:
            raise ValueError("recovery intervals and attempts must be positive; outbound lease >= 30 seconds")

    def provider(self, name: str) -> Provider | None:
        return self.providers.get(name.upper()) if name else None

    def get_chat_provider(self) -> Provider | None:
        """判定 / 特征补抽 / 回复使用的供应商;未指名时取已配置的第一个。"""
        if self.chat_provider_name:
            return self._required("CHAT_PROVIDER", self.chat_provider_name)
        return self._get_first_configured_provider()

    def get_transcription_provider(self) -> Provider | None:
        """图片转写供应商;未指名时回落到 chat。"""
        if self.transcribe_provider_name:
            return self._required("TRANSCRIBE_PROVIDER", self.transcribe_provider_name)
        return self.get_chat_provider()

    def get_embedding_provider(self) -> Provider | None:
        """向量检索供应商;未指名则检索退化为纯关键词。"""
        if not self.embed_provider_name:
            return None
        return self._required("EMBED_PROVIDER", self.embed_provider_name)

    def _required(self, env_name: str, name: str) -> Provider:
        found = self.provider(name)
        if found is None:
            raise ValueError(f"{env_name}={name} 未找到对应供应商(检查 <NAME>_API_KEY 拼写)")
        return found

    def _get_first_configured_provider(self) -> Provider | None:
        return min(self.providers.values(), key=lambda p: p.name) if self.providers else None

    @property
    def llm_enabled(self) -> bool:
        return self.mode == "llm"

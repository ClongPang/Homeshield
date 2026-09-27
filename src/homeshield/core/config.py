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
    wechat_token: str = ""
    wechat_appid: str = ""
    wechat_secret: str = ""
    wechat_template_id: str = ""
    wechat_multi_template_id: str = ""
    public_base_url: str = ""
    max_members: int = 10
    max_groups: int = 10
    bind_code_ttl_days: int = 7  # 绑定码有效期;超期/已用即失效
    db_path: str = "homeshield.db"
    judge_retries: int = 2  # 引用校验失败重试上限
    safe_confidence_floor: int = 60  # safe 判定最低置信(llm 模式生效);低于则降级"拿不准"
    incident_idle_seconds: int = 21600
    supply_window_seconds: int = 604800
    supply_max_items: int = 10

    @classmethod
    def load(cls, env_file: str | None = None) -> "Settings":
        if env_file:
            load_dotenv(env_file)
        else:
            load_dotenv()
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
            wechat_token=os.getenv("WECHAT_TOKEN", ""),
            wechat_appid=os.getenv("WECHAT_APPID", ""),
            wechat_secret=os.getenv("WECHAT_SECRET", ""),
            wechat_template_id=os.getenv("WECHAT_TEMPLATE_ID", ""),
            wechat_multi_template_id=os.getenv("WECHAT_MULTI_TEMPLATE_ID", ""),
            public_base_url=os.getenv("PUBLIC_BASE_URL", ""),
            max_members=int(os.getenv("MAX_MEMBERS", "10")),
            max_groups=int(os.getenv("MAX_GROUPS", "10")),
            bind_code_ttl_days=int(os.getenv("BIND_CODE_TTL_DAYS", "7")),
            db_path=os.getenv("DB_PATH", "homeshield.db"),
            safe_confidence_floor=int(os.getenv("SAFE_CONFIDENCE_FLOOR", "60")),
            incident_idle_seconds=int(os.getenv("INCIDENT_IDLE_SECONDS", "21600")),
            supply_window_seconds=int(os.getenv("SUPPLY_WINDOW_SECONDS", "604800")),
            supply_max_items=int(os.getenv("SUPPLY_MAX_ITEMS", "10")),
        )

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

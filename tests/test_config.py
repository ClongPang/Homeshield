"""供应商配置解析与任务路由。"""
from __future__ import annotations

import pytest

from homeshield.core.config import Provider, Settings


def _settings(**kw) -> Settings:
    providers = {
        "DEEPSEEK": Provider("DEEPSEEK", "k1", "https://a", "deepseek-chat"),
        "QWEN": Provider("QWEN", "k2", "https://b", "qwen-embed"),
    }
    return Settings(providers=providers, **kw)


def test_role_routing():
    s = _settings(chat_provider_name="DEEPSEEK", embed_provider_name="QWEN")
    assert s.get_chat_provider().model == "deepseek-chat"
    assert s.get_embedding_provider().model == "qwen-embed"
    assert s.get_transcription_provider().model == "deepseek-chat"  # 未配置转写,回落 chat


def test_unspecified_chat_falls_back_to_first_provider():
    s = _settings()
    assert s.get_chat_provider().name == "DEEPSEEK"
    assert s.get_embedding_provider() is None  # 未指名 embed → 检索退化为纯关键词


def test_unknown_provider_fails_fast():
    with pytest.raises(ValueError, match="CHAT_PROVIDER"):
        _settings(chat_provider_name="TYPPO").get_chat_provider()
    with pytest.raises(ValueError, match="EMBED_PROVIDER"):
        _settings(embed_provider_name="TYPPO").get_embedding_provider()


def test_load_scans_env(monkeypatch, tmp_path):
    # 与真实 .env 彻底隔离:清掉可能泄漏的变量,并指向一个空环境文件
    for var in ("DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL", "DEEPSEEK_MODEL",
                "QWEN_API_KEY", "CHAT_PROVIDER", "EMBED_PROVIDER"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "k")
    monkeypatch.setenv("CHAT_PROVIDER", "deepseek")
    env_file = tmp_path / "test.env"
    env_file.write_text("# empty\n", encoding="utf-8")
    s = Settings.load(env_file=str(env_file))
    assert set(s.providers) == {"DEEPSEEK"}
    assert s.get_chat_provider().name == "DEEPSEEK"
    assert s.get_embedding_provider() is None


def test_load_reads_group_capacity_limits_from_env_file(monkeypatch, tmp_path):
    names = ("MAX_GROUPS", "MAX_MEMBERS")
    for name in names:
        monkeypatch.delenv(name, raising=False)
    env_file = tmp_path / "limits.env"
    env_file.write_text(
        "MAX_GROUPS=4\nMAX_MEMBERS=7\n",
        encoding="utf-8",
    )

    settings = Settings.load(env_file=str(env_file))
    for name in names:
        monkeypatch.delenv(name, raising=False)

    assert settings.max_groups == 4
    assert settings.max_members == 7


def test_llm_routes_tasks_to_providers():
    from homeshield.core.llm import OpenAICompatLLM

    llm = OpenAICompatLLM(
        _settings(chat_provider_name="DEEPSEEK", embed_provider_name="QWEN")
    )
    assert llm.get_provider_for_task("judge").name == "DEEPSEEK"
    assert llm.get_provider_for_task("features").name == "DEEPSEEK"
    assert llm.get_provider_for_task("reply").name == "DEEPSEEK"
    assert llm.get_provider_for_task("transcribe").name == "DEEPSEEK"
    assert llm.get_provider_for_task("embed").name == "QWEN"

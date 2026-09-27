"""LLM 端口与适配器。

领域层只依赖 LLMPort 协议;openai 延迟导入,mock 模式零外部依赖。
task 参数承担双重职责:Mock 按用途返回确定性结果;OpenAICompatLLM 按
task 路由供应商——judge / features / reply 走 chat,transcribe_image 走
transcribe(未配置回落 chat),embed 走 embed。
"""
import hashlib
import json
import math
import re
from typing import Any, Protocol

from homeshield.core.config import Provider, Settings
from homeshield.core.errors import DegradeError

_EMBED_BATCH = 20  # 部分供应商(如 DashScope)单批上限 25,留余量


class LLMPort(Protocol):
    async def chat_json(self, task: str, system: str, user: str, schema: dict) -> dict: ...
    async def chat_text(self, task: str, system: str, user: str) -> str: ...
    async def embed(self, texts: list[str]) -> list[list[float]]: ...
    async def transcribe_image(self, image_b64: str, hint: str) -> str: ...


def _hash_vec(text: str, dim: int = 64) -> list[float]:
    v = [0.0] * dim
    for i in range(max(1, len(text) - 1)):
        h = int(hashlib.md5(text[i : i + 2].encode()).hexdigest(), 16)
        v[h % dim] += 1.0
    norm = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / norm for x in v]


def _loads_json(text: str) -> dict:
    """解析模型 JSON,容忍 ```json 围栏与前后杂文。"""
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", t)
    start, end = t.find("{"), t.rfind("}")
    if start != -1 and end != -1:
        t = t[start : end + 1]
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        return {}


class MockLLM:
    """确定性实现:不联网,行为可断言。"""

    async def chat_json(self, task: str, system: str, user: str, schema: dict) -> dict[str, Any]:
        if task == "features":
            feats = [
                {"mechanic": m, "value": kw, "evidence_span": kw, "confidence": c}
                for kw, m, c in (("退款", "bait", 5), ("保证金", "money", 9), ("安全账户", "identity", 8))
                if kw in user
            ]
            return {"features": feats}
        if task == "judge":
            # 计数打分,权重与 MockJudge 不同
            score = sum(user.count(k) for k in ("转账", "保证金", "别告诉", "安全账户", "立即"))
            level = "dangerous" if score >= 3 else "suspicious" if score >= 1 else "safe"
            return {
                "level": level,
                "confidence": min(90, 40 + score * 10),
                "cited_ids": [],
                "reason": "mock-llm 计分判定",
            }
        return {}

    async def chat_text(self, task: str, system: str, user: str) -> str:
        return "【结论】需要小心\n【依据】命中可疑特征\n【建议】先与家人商量再操作"

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [_hash_vec(t) for t in texts]

    async def transcribe_image(self, image_b64: str, hint: str) -> str:
        if "DEGRADE" in image_b64[:64] or "DEGRADE" in hint:
            raise DegradeError("图片看不清，请把内容打成文字发我", "mock transcribe degrade")
        return f"转写文本(mock):{hint or image_b64[:16]}"


class OpenAICompatLLM:
    """按任务路由到不同供应商的 OpenAI 兼容端点。"""

    def __init__(self, settings: Settings):
        self._chat = settings.chat_endpoint()
        if self._chat is None:
            raise ValueError("MODE=llm 需要至少一个供应商(<NAME>_API_KEY / <NAME>_BASE_URL)")
        self._transcribe = settings.transcribe_endpoint()
        self._embed = settings.embed_endpoint()
        self._clients: dict[str, Any] = {}

    def endpoint_for(self, task: str) -> Provider:
        if task == "embed":
            if self._embed is None:
                raise RuntimeError("未配置 EMBED_PROVIDER,无法做向量检索")
            return self._embed
        if task == "transcribe":
            return self._transcribe or self._chat
        return self._chat

    def _client(self, provider: Provider):
        if provider.name not in self._clients:
            from openai import AsyncOpenAI  # 延迟导入:mock 模式零外部依赖

            self._clients[provider.name] = AsyncOpenAI(
                api_key=provider.api_key,
                base_url=provider.base_url or None,
                timeout=60.0,  # 单请求上限:供应商侧挂起时快速失败走降级,不再默认等 10 分钟
                max_retries=1,
            )
        return self._clients[provider.name], provider.model

    async def chat_json(self, task: str, system: str, user: str, schema: dict) -> dict:
        client, model = self._client(self.endpoint_for(task))
        resp = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            response_format={"type": "json_object"},
        )
        return _loads_json(resp.choices[0].message.content or "{}")

    async def chat_text(self, task: str, system: str, user: str) -> str:
        client, model = self._client(self.endpoint_for(task))
        resp = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        return resp.choices[0].message.content or ""

    async def embed(self, texts: list[str]) -> list[list[float]]:
        client, model = self._client(self.endpoint_for("embed"))
        out: list[list[float]] = []
        for i in range(0, len(texts), _EMBED_BATCH):  # 部分供应商单批限 25,分批兜底
            batch = texts[i : i + _EMBED_BATCH] or texts
            resp = await client.embeddings.create(model=model, input=batch)
            out.extend(d.embedding for d in resp.data)
        return out

    async def transcribe_image(self, image_b64: str, hint: str) -> str:
        client, model = self._client(self.endpoint_for("transcribe"))
        resp = await client.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": hint or "转写图片中的所有文字,不要解释。"},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/png;base64,{image_b64}"},
                        },
                    ],
                }
            ],
        )
        return resp.choices[0].message.content or ""


def make_llm(settings: Settings) -> LLMPort:
    """按 MODE 切换双模式。"""
    return OpenAICompatLLM(settings) if settings.use_llm else MockLLM()

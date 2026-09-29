"""规则特征与隔离话术规则下限。"""

from homeshield.core.features import (
    FeatureSpec,
    assign_feature_ids,
    extract_rule_features,
    get_rule_risk_floor,
    supplement_features_with_llm,
)
from homeshield.core.models import Level


async def test_get_rule_risk_floor():
    iso = FeatureSpec(type="isolation", value="别告诉家人")
    tr = FeatureSpec(type="transfer", value="转账")
    assert get_rule_risk_floor([iso]) is Level.SUSPICIOUS
    assert get_rule_risk_floor([iso, tr]) is Level.DANGEROUS
    assert get_rule_risk_floor([FeatureSpec(type="urgency", value="立即")]) is Level.SAFE


async def test_extract_rule_features_hits():
    text = "别告诉家人,立即转账5万元到安全账户,手续费2000元"
    types = {s.type for s in extract_rule_features(text)}
    assert {"isolation", "transfer", "urgency", "identity_claim", "fee", "amount"} <= types


async def test_assign_feature_ids_sequential():
    feats = assign_feature_ids([FeatureSpec(type="url", value="x"), FeatureSpec(type="fee", value="y")])
    assert [f.id for f in feats] == ["F01", "F02"]


async def test_llm_supplement_is_mock_deterministic(deps):
    specs = await supplement_features_with_llm(deps.llm, "退款保证金")
    assert specs and all(s.source == "llm" for s in specs)
    assert await supplement_features_with_llm(deps.llm, "今天天气不错") == []


class _ScriptedLLM:
    """按脚本返回 supplement 结果的桩,覆盖解析与过滤分支。"""

    def __init__(self, payload):
        self.payload = payload

    async def chat_json(self, task, system, user, schema):
        return self.payload


async def test_supplement_filters_and_clamps():
    payload = {"features": [
        {"mechanic": "money", "value": "转账", "evidence_span": "转账", "confidence": 9},
        {"mechanic": "unknown_x", "value": "怪东西", "confidence": 9},       # 机制外丢弃
        {"mechanic": "urgency", "value": "", "confidence": 9},              # 空值丢弃
        {"mechanic": "urgency", "value": "马上", "confidence": "abc"},       # 坏分 → 5
        {"mechanic": "escape", "value": "链接", "confidence": 99},           # 越界 → 10
    ]}
    specs = await supplement_features_with_llm(_ScriptedLLM(payload), "文本")
    assert [(s.type, s.confidence) for s in specs] == [("money", 9), ("urgency", 5), ("escape", 10)]
    assert all(s.type in __import__("homeshield.core.knowledge.mechanics", fromlist=["REGISTRY"]).REGISTRY for s in specs)


async def test_supplement_mechanic_types_feed_annotate():
    specs = await supplement_features_with_llm(_ScriptedLLM(
        {"features": [{"mechanic": "isolation", "value": "别告诉家人",
                       "evidence_span": "别告诉家人", "confidence": 9}]}), "文本")
    feats = assign_feature_ids(specs)
    from homeshield.core.annotate import annotate_text
    assert "<隔离封口>别告诉家人</隔离封口>" in annotate_text("妈,别告诉家人,要转账", feats)

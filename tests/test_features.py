"""规则特征与隔离话术规则下限。"""
import asyncio

from core.features import (
    FeatureSpec,
    assign_ids,
    extract_rules,
    rule_floor,
    supplement_llm,
)
from core.models import Level


def test_rule_floor():
    iso = FeatureSpec(type="isolation", value="别告诉家人")
    tr = FeatureSpec(type="transfer", value="转账")
    assert rule_floor([iso]) is Level.SUSPICIOUS
    assert rule_floor([iso, tr]) is Level.DANGEROUS
    assert rule_floor([FeatureSpec(type="urgency", value="立即")]) is Level.SAFE


def test_extract_rules_hits():
    text = "别告诉家人,立即转账5万元到安全账户,手续费2000元"
    types = {s.type for s in extract_rules(text)}
    assert {"isolation", "transfer", "urgency", "identity_claim", "fee", "amount"} <= types


def test_assign_ids_sequential():
    feats = assign_ids([FeatureSpec(type="url", value="x"), FeatureSpec(type="fee", value="y")])
    assert [f.id for f in feats] == ["F01", "F02"]


def test_llm_supplement_is_mock_deterministic(deps):
    specs = asyncio.run(supplement_llm(deps.llm, "退款保证金"))
    assert specs and all(s.source == "llm" for s in specs)
    assert asyncio.run(supplement_llm(deps.llm, "今天天气不错")) == []

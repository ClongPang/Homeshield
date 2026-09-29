"""机制层与对照集:注册表完整性、与规则下限词表的一致性、覆盖检查器。"""
import json
from pathlib import Path

import pytest

from homeshield.core.features import FEE_WORDS, IDENTITY_WORDS, ISOLATION_WORDS, TRANSFER_WORDS, URGENCY_WORDS
from homeshield.core.knowledge.mechanics import (
    MECHANICS_VERSION,
    MECHANIC_LIST,
    Function,
    REGISTRY,
)
from homeshield.eval.contrast import infer_mechanics_from_markers, check_contrast_dataset_coverage, render_contrast_report


# ---- 注册表完整性 -------------------------------------------------------

async def test_registry_shape():
    assert len(MECHANIC_LIST) == 11
    assert {m.id for m in MECHANIC_LIST} == set(REGISTRY)
    for m in MECHANIC_LIST:
        assert m.function in Function
        assert m.definition and m.verification_path and m.markers


async def test_three_functions_populated():
    asks = {m.id for m in MECHANIC_LIST if m.function is Function.ASK}
    trusts = {m.id for m in MECHANIC_LIST if m.function is Function.TRUST_SUBSTITUTE}
    supps = {m.id for m in MECHANIC_LIST if m.function is Function.VERIFICATION_SUPPRESSION}
    assert asks == {"sensitive", "money", "control"}
    assert trusts == {"identity", "bait", "fear", "emotion"}
    assert supps == {"urgency", "isolation", "antiverify", "escape"}


async def test_no_duplicate_markers():
    seen: set[str] = set()
    for m in MECHANIC_LIST:
        dup = seen & set(m.markers)
        assert not dup, f"机制 {m.id} 词表重复: {dup}"
        seen |= set(m.markers)


async def test_version_stamped():
    assert MECHANICS_VERSION == "1.0"


# ---- 与运行时规则下限词表的一致性(机制层是词表的上级模型) -----------------

async def test_floor_lists_are_subsets_of_mechanics():
    assert set(ISOLATION_WORDS) <= set(REGISTRY["isolation"].markers)
    assert set(URGENCY_WORDS) <= set(REGISTRY["urgency"].markers)
    assert set(IDENTITY_WORDS) <= set(REGISTRY["identity"].markers)
    assert set(FEE_WORDS) <= set(REGISTRY["money"].markers)
    # features.py 把"给我验证码"归入 transfer(下限语义正确);机制层归 sensitive,已知差异
    assert set(TRANSFER_WORDS) - {"给我验证码"} <= set(REGISTRY["money"].markers)
    assert "给我验证码" in REGISTRY["sensitive"].markers


# ---- 覆盖检查器 ----------------------------------------------------------

@pytest.fixture()
def mini_contrast(tmp_path: Path) -> Path:
    rows = [
        {"id": "T1", "text": "转账到最后一天优惠", "label": "edge",
         "mechanics": ["money", "urgency"]},
        {"id": "T2", "text": "正常天气不错", "label": "benign", "mechanics": []},
        {"id": "T3", "text": "屏幕共享办理退款", "label": "edge"},  # 缺标注 → 自动推导
    ]
    p = tmp_path / "mini.jsonl"
    p.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8")
    return p


async def test_infer_mechanics_from_markers():
    assert "money" in infer_mechanics_from_markers("请立即转账到安全账户")
    assert infer_mechanics_from_markers("今天天气不错") == []


async def test_check_zones_and_auto_annotate(mini_contrast: Path):
    report = check_contrast_dataset_coverage(mini_contrast)
    assert report["rows"] == 3
    # T3 缺标注被自动推导:control+sensitive+money
    assert "control" in report["per_mechanic"]
    assert report["ask_zones"]["control"]["benign_rows"] == 1
    assert report["ask_zones"]["money"]["with_verification_suppression"] == 1
    assert set(report["gaps"]) == {"sensitive"}  # mini 集内 sensitive 零覆盖


async def test_render_mentions_gaps(mini_contrast: Path):
    text = render_contrast_report(check_contrast_dataset_coverage(mini_contrast))
    assert "对照缺口" in text


# ---- 真实对照集验收 ------------------------------------------------------

async def test_real_contrast_set_contract():
    path = Path("data/samples/benign_hard.jsonl")
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(rows) >= 12
    for r in rows:
        assert r["label"] in ("edge", "benign")
        assert r["source"].startswith("adapted:")  # 禁止 synthetic:llm(设计教训 §4)
        assert isinstance(r["mechanics"], list)
    hard = [r for r in rows if len(r["mechanics"]) >= 2]
    assert len(hard) >= 10  # ≥10 条多机制共现的"硬"样本


async def test_real_contrast_loads_via_eval_dataset():
    from homeshield.eval.dataset import load_dataset

    samples = load_dataset("data/samples/benign_hard.jsonl")
    assert len(samples) == 14  # mechanics 扩展字段被 Sample 忽略

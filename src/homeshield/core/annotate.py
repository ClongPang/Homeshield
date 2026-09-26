"""机制内联标注(重构二,设计依据《判定模型_设计教训》§3.1)。

把特征证据按机制原地标注进原文:<机制名>证据</机制名>。
FraudShield(WWW 2026)消融:证据表示形式本身是一级变量——内联标注
显著优于平行列表。标注是表示层:只标位置与机制名,不产生结论;
判定仍由 judge 在标注后原文上做出(防锚定约束见 judge.py 的提示词)。

特征→机制映射是 FeatureType(运行时现行特征体系)与机制层的桥;
LLM 补抽的 semantic 等无法映射的类型不标注,但仍进特征表供引用。
"""
from __future__ import annotations

from homeshield.core.knowledge.mechanics import REGISTRY
from homeshield.core.models import Feature

FEATURE_MECHANIC: dict[str, str] = {
    "isolation": "isolation",
    "transfer": "money",
    "fee": "money",
    "urgency": "urgency",
    "identity_claim": "identity",
    "url": "escape",
    "account": "sensitive",
    "amount": "money",
}


def annotate_text(text: str, features: list[Feature]) -> str:
    """按证据跨度原地标注;跨度重叠时同起点取更长,先到先得。

    无映射类型/空跨度/文本中不存在的证据不标注;无任何命中时原样返回。
    """
    hits: list[tuple[int, int, str]] = []
    for f in features:
        mechanic_id = FEATURE_MECHANIC.get(f.type) or (
            f.type if f.type in REGISTRY else None  # 重构三:LLM 补抽的机制 id 直标
        )
        span = f.evidence_span
        if not mechanic_id or not span:
            continue
        pos = 0
        while (idx := text.find(span, pos)) != -1:
            hits.append((idx, idx + len(span), mechanic_id))
            pos = idx + len(span)
    hits.sort(key=lambda h: (h[0], -(h[1] - h[0])))
    kept: list[tuple[int, int, str]] = []
    end = 0
    for s, e, mid in hits:
        if s >= end:
            kept.append((s, e, mid))
            end = e
    out: list[str] = []
    pos = 0
    for s, e, mid in kept:
        out.append(text[pos:s])
        out.append(f"<{REGISTRY[mid].name}>{text[s:e]}</{REGISTRY[mid].name}>")
        pos = e
    out.append(text[pos:])
    return "".join(out)

"""将特征证据以内联机制标签标注到原文。

输出保留原始文字，并用 <机制名>...</机制名> 包裹对应证据。
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
    """根据特征证据跨度生成机制标签，并嵌回原文。

    每个特征按类型确定机制，并查找证据在原文中的位置。重叠跨度按起点排序，
    同起点优先保留较长跨度；最终按原文顺序拼接标注片段。
    """
    hits: list[tuple[int, int, str]] = []
    for f in features:
        mechanic_id = FEATURE_MECHANIC.get(f.type) or (f.type if f.type in REGISTRY else None)
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

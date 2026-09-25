"""Fraud-R1 五大类 → 家中盾 12 类分类学映射。

原则:只做有依据的映射,对不上的返回 None 并计数——
phishing 三个子类(236 条)在 12 类里没有对应类目,是分类学的真实缺口,
宁可留在映射外报告,也不强行归入错误类目污染评测。
"""
from __future__ import annotations

import re

# 退款/理赔语义关键词(e-commerce 子类细分用)
_REFUND_CUES = ("退款", "理赔", "赔偿", "退货", "双倍")
# 刷单语义:先付费 + 任务酬劳组合(fake job posting 子类细分用)
_PAYFIRST_CUES = ("垫付", "先付", "预付", "押金")
_TASK_CUES = ("任务", "佣金", "点赞", "刷单", "做单", "接单")

# 分类学缺口:phishing 子类(中奖/钓鱼链接/垃圾推广)暂无对应类目
TAXONOMY_GAP = ("phishing",)


def map_scam_type(category: str, subcategory: str, text: str) -> str | None:
    cat, sub = category.strip().lower(), subcategory.strip().lower()
    if cat == "impersonation":
        if "public security" in sub or "government" in sub:
            return "impersonate_police"
        if "acquaintances" in sub or "friend" in sub:
            return "impersonate_relative"
        return "impersonate_boss"  # BEC/商业冒充兜底
    if cat == "fraudulent service":
        if "investment" in sub or "financial" in sub:
            return "fake_investment"
        if "e-commerce" in sub or "shopping" in sub or "logistics" in sub:
            return "fake_refund" if any(c in text for c in _REFUND_CUES) else "fake_shopping"
        if "loan" in sub:
            return "fake_loan"
        return None
    if cat == "fake job posting":
        if any(p in text for p in _PAYFIRST_CUES) and any(t in text for t in _TASK_CUES):
            return "task_scam"
        return "fake_parttime"
    if cat == "network friendship":
        return "romance_pigbutchering"
    return None  # phishing 及未知类目 → 分类学缺口

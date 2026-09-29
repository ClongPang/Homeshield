"""Fraud-R1 五大类 → 家中盾 16 类分类学映射。

原则:只做有依据的映射,对不上的返回 None 并计数——
宁可留在映射外报告,也不强行归入错误类目污染评测。
新增 4 类(中奖免费领/医保社保/退改签/保健品神药)后,phishing
按内容二次分流,残余仍计缺口。
"""

# 退款/理赔语义关键词(e-commerce 子类细分用)
_REFUND_CUES = ("退款", "理赔", "赔偿", "退货", "双倍")
# 刷单语义:先付费 + 任务酬劳组合(fake job posting 子类细分用)
_PAYFIRST_CUES = ("垫付", "先付", "预付", "押金")
_TASK_CUES = ("任务", "佣金", "点赞", "刷单", "做单", "接单")
# phishing 内容二次分流(新增类目承接后,残余仍计缺口)
_PRIZE_CUES = ("中奖", "奖品", "免费领", "抽奖", "礼包")
_SUBSIDY_CUES = ("补贴", "医保", "社保", "养老金", "资格认证")
_TICKET_CUES = ("航班", "退改", "机票", "火车票", "停运")
_HEALTH_CUES = ("保健", "秘方", "神药", "专家", "义诊", "疗程")
_PROMO_CUES = ("优惠", "低价", "促销", "折扣", "秒杀", "回馈")

# 分类学残余缺口:phishing 中无上述内容特征的通用钓鱼/垃圾邮件
TAXONOMY_GAP = ("phishing",)


def map_scam_type(category: str, subcategory: str, text: str) -> str | None:
    cat, sub = category.strip().lower(), subcategory.strip().lower()
    if cat == "impersonation":
        if "public security" in sub or "government" in sub:
            return "impersonate_police"
        if "acquaintances" in sub or "friend" in sub:
            # 内容修正:熟人子类下的商务邮件诈骗(BEC)实为冒充领导/合作方
            if any(c in text for c in ("主管", "采购", "框架协议", "付款处理", "项目合作",
                                       "总监", "联络函", "集团")):
                return "impersonate_boss"
            # 内容修正:熟人子类下的教培退费实为退费诈骗
            if any(c in text for c in ("退费", "学费", "课程")):
                return "fake_refund"
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
    if cat == "phishing":
        if any(c in text for c in _PRIZE_CUES):
            return "fake_prize"
        if any(c in text for c in _SUBSIDY_CUES):
            return "social_insurance"
        if any(c in text for c in _TICKET_CUES):
            return "ticket_refund"
        if any(c in text for c in _HEALTH_CUES):
            return "fake_health"
        if any(c in text for c in _PROMO_CUES):
            return "fake_shopping"
        return None  # 通用钓鱼/垃圾邮件,仍留缺口
    return None  # 未知类目

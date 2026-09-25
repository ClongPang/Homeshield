"""骗术分类学,共 12 类。

新增/调整类目只改 REGISTRY 数据。
markers 用于检索;advice 供回复的"建议"段使用。
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class ScamType:
    id: str
    name: str
    markers: tuple[str, ...]  # 识别点
    advice: str  # 应对建议


REGISTRY: dict[str, ScamType] = {
    t.id: t
    for t in [
        ScamType("task_scam", "刷单返利",
                 ("做任务", "返利", "佣金", "点赞赚钱", "垫付"),
                 "刷单本身违法,凡是垫资做任务返佣金的都是诈骗,立即停止并保留证据。"),
        ScamType("impersonate_police", "冒充公检法",
                 ("公检法", "公安局", "通缉", "洗钱", "安全账户", "清查资金"),
                 "公检法不会电话办案、更没有“安全账户”,不转账、不透露验证码,挂断后拨110核实。"),
        ScamType("fake_refund", "冒充客服退款",
                 ("退款", "理赔", "快递丢失", "质量问题", "双倍赔偿", "屏幕共享"),
                 "官方退款不需要你先转账或下载屏幕共享软件,直接在官方App里查订单。"),
        ScamType("fake_investment", "虚假投资理财",
                 ("稳赚不赔", "内幕", "跟单", "高收益", "导师", "荐股群"),
                 "承诺稳赚不赔的都是诈骗,投资只走持牌机构,别进陌生荐股群。"),
        ScamType("impersonate_relative", "冒充亲友借钱",
                 ("是我", "急用钱", "借我", "先转给我", "在住院"),
                 "凡是线上开口借钱的,先打电话当面核实本人,再谈钱。"),
        ScamType("fake_parttime", "虚假兼职",
                 ("兼职", "日结", "点赞员", "打字员", "在家可做", "先交押金"),
                 "先收费的兼职都是骗局,正规工作不会让你先交押金。"),
        ScamType("fake_shopping", "虚假购物",
                 ("低价", "折扣", "私下交易", "加微信购买", "渠道价"),
                 "超低价又要求脱离平台私下交易的,多半是假货或骗局。"),
        ScamType("impersonate_boss", "冒充领导老板",
                 ("我是你领导", "王总", "李总", "帮我垫付", "先转给我"),
                 "领导线上要你垫付转账的,务必电话或当面核实。"),
        ScamType("fake_loan", "虚假贷款",
                 ("无抵押", "放款快", "解冻金", "刷流水", "包装费"),
                 "放款前先收费的都是假贷款,只走正规银行和持牌机构。"),
        ScamType("romance_pigbutchering", "网络交友诱导投资",
                 ("网恋", "带你赚钱", "博彩", "内部渠道", "老师带单"),
                 "网上认识的人带你投资或赌博的,是“杀猪盘”,及时抽身并告知家人。"),
        ScamType("fake_fee", "收费名目异常",
                 ("保证金", "解冻费", "认证金", "手续费", "会员费", "激活费"),
                 "正规业务不会在各个环节反复收费,先收费的都值得怀疑。"),
        ScamType("express_insurance", "快递理赔/补贴",
                 ("快递理赔", "包裹丢失", "补贴", "领取资格", "点击链接填写"),
                 "理赔补贴直接在官方App办理,不点陌生链接、不填银行卡。"),
    ]
}


def all_types() -> list[ScamType]:
    return list(REGISTRY.values())


def get(type_id: str) -> ScamType | None:
    return REGISTRY.get(type_id)

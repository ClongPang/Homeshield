"""欺诈机制层(判定模型 v1.0,设计依据见《家中盾_判定模型_设计教训》§3.1)。

三功能×十一机制:识别靠机制,定级靠核实结构。机制是**表示层**
(抽取词表种子、内联标注标签、引用证据的组织方式),不是判定引擎——
分级语义的唯一确定性约束是 features.get_rule_risk_floor,其余分级由 LLM 在
机制标注后的原文上判断(重构二+五落地)。

字段约定:
- markers: 识别点种子词表(规则首筛与离线挖掘的种子;重构三前,
  features.py 各词表是运行时权威,本表与它的子集一致性由测试保证);
- verification_path: 核实路径,回复"行动建议"段的解药库;
- 机制卡内容(定义/话术/路径)经 git 评审修订;marker_candidate 审核队列
  留给语料挖掘出的候选(重构三),两者闸门不同。
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

MECHANICS_VERSION = "1.0"


class Function(StrEnum):
    """三功能:骗局是什么(索取),为什么信(信任替代),为什么不能核实(核实抑制)。"""

    ASK = "ask"
    TRUST_SUBSTITUTE = "trust_substitute"
    VERIFICATION_SUPPRESSION = "verification_suppression"


@dataclass(frozen=True)
class Mechanic:
    id: str
    name: str
    function: Function
    definition: str
    markers: tuple[str, ...]
    verification_path: str


REGISTRY: dict[str, Mechanic] = {
    m.id: m
    for m in [
        # ---- 索取:要什么 ------------------------------------------------
        Mechanic("sensitive", "敏感索求", Function.ASK,
                 "直接索取凭证类信息,拿到即可动用身份或资金。",
                 ("验证码", "支付密码", "登录密码", "人脸", "银行卡号", "卡号", "有效期", "给我验证码"),
                 "任何渠道索要验证码/密码/人脸的都是诈骗,直接拒绝;官方业务在官方App内完成,不需要你提供。"),
        Mechanic("money", "资金动作", Function.ASK,
                 "要求资金离开你的账户,或以名目先行收费。",
                 ("转账", "打款", "汇款", "转入", "先付", "垫付", "充值", "刷流水", "解冻金", "保证金",
                  "解冻费", "认证金", "手续费", "会员费", "激活费", "包装费"),
                 "资金往来只走原平台/官方渠道;凡是转到指定新账户、先交费后返款的,先在官方App查订单与账单。"),
        Mechanic("control", "控制权索取", Function.ASK,
                 "索取屏幕/设备的操作能力,绕过你本人直接操作。",
                 ("屏幕共享", "共享屏幕", "远程协助", "远程控制", "会议软件", "屏幕录制"),
                 "官方客服绝不会要求屏幕共享或远程控制;立即结束通话,不安装任何来源不明的软件。"),
        # ---- 信任替代:为什么信 -------------------------------------------
        Mechanic("identity", "身份冒充", Function.TRUST_SUBSTITUTE,
                 "自称可信身份,替代你对身份的核实。",
                 ("公检法", "公安局", "检察院", "法院", "银监会", "清查", "安全账户", "客服", "领导", "老师",
                  "官方客服", "我是你领导", "换号", "我是你"),
                 "挂断后通过官方公开渠道回拨核实(110/官方客服/当面确认),绝不使用对方提供的号码。"),
        Mechanic("bait", "利益诱饵", Function.TRUST_SUBSTITUTE,
                 "用高回报替代你对收益真实性的核实。",
                 ("稳赚不赔", "高收益", "内幕", "双倍赔偿", "中奖", "免费领", "返利", "佣金", "日结",
                  "低息", "免抵押", "渠道价", "原价"),
                 "回报明显高于市场水平即风险;到监管官网查资质,中奖类直接打官方电话核实。"),
        Mechanic("fear", "恐惧威胁", Function.TRUST_SUBSTITUTE,
                 "用恐惧替代你对后果真实性的核实。",
                 ("通缉", "逮捕", "洗钱", "涉案", "冻结", "起诉", "拉黑名单", "影响征信", "拘留", "停电", "停机"),
                 "法律与账户后果以官方文书和官方App为准,不接受电话口头告知;拨110或12348核实。"),
        Mechanic("emotion", "情感操纵", Function.TRUST_SUBSTITUTE,
                 "用情感联结替代你对请求动机的核实。",
                 ("相信我", "为了我们", "为了孩子", "我生病", "住院", "急用钱", "救命", "别让我失望", "亏欠"),
                 "涉及钱的情感请求,一律当面或视频电话确认本人;真挚的关系经得起核实。"),
        # ---- 核实抑制:为什么不能核实 --------------------------------------
        Mechanic("urgency", "紧迫施压", Function.VERIFICATION_SUPPRESSION,
                 "压缩思考与核实的时间窗口。",
                 ("立即", "马上", "立刻", "尽快", "最后一天", "限时", "逾期", "紧急", "倒计时", "名额有限"),
                 "任何'必须马上'的决定都缓半小时再办;真紧急的事不会因为核实半小时而失效。"),
        Mechanic("isolation", "隔离封口", Function.VERIFICATION_SUPPRESSION,
                 "切断来自家人的纠错渠道。正常事务几乎不需要对家人保密——本机制单独出现即值得警惕。",
                 ("别告诉家人", "别告诉子女", "不要告诉家人", "保密", "这是我们俩的事", "影响他工作", "偷偷"),
                 "凡是要求对家人保密的,第一时间告诉家人——真事情不怕家人知道(家人已知悉正是本产品的设计)。"),
        Mechanic("antiverify", "阻断核实", Function.VERIFICATION_SUPPRESSION,
                 "切断来自官方渠道的纠错,正常业务从不阻止核实。",
                 ("官方查不到", "不要打银行电话", "别报警", "报警没用", "别告诉银行", "内部渠道核实", "验证不了", "别联系平台"),
                 "对方越阻止你核实,越要核实;一律通过官方公开渠道独立验证。"),
        Mechanic("escape", "渠道逃逸", Function.VERIFICATION_SUPPRESSION,
                 "把你带离有保障与记录的受监管环境。",
                 ("加微信", "加QQ", "扫码", "下载App", "点击链接", "点击网址", "私下交易", "脱离平台", "场外"),
                 "交易与沟通留在原平台内;平台外的'专属客服/链接/软件'一律不看不点不装。"),
    ]
}

MECHANIC_LIST: list[Mechanic] = list(REGISTRY.values())


def list_mechanics_by_function(function: Function) -> list[Mechanic]:
    return [m for m in MECHANIC_LIST if m.function is function]


def get_mechanic(mechanic_id: str) -> Mechanic | None:
    return REGISTRY.get(mechanic_id)

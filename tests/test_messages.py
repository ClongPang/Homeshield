"""用户可见文案的免责口径:不担保、露官方兜底、知情、不泄内部配置。"""
from homeshield.core import messages


def test_welcome_no_guarantee_claim():
    """欢迎语不得担保判定结论("是不是骗局"是担保式表述,与 safe 口径矛盾)。"""
    assert "是不是骗局" not in messages.WELCOME
    assert "常见骗术" in messages.WELCOME  # 只声明覆盖范围:已知骗术对照


def test_welcome_discloses_privacy_and_official_fallback():
    """知情页承载(《产品定位》§1.3):查询私密范围 + 国家反诈专线兜底。"""
    assert "只回复你本人" in messages.WELCOME
    assert "96110" in messages.WELCOME


def test_open_no_url_leaks_no_internal_config():
    """故障文案不暴露内部配置名(环境变量不出用户面)。"""
    assert "PUBLIC_BASE_URL" not in messages.OPEN_NO_URL
    assert "请联系服务提供方" in messages.OPEN_NO_URL


def test_member_terminology_consistent():
    """绑定文案与控制台术语一致:群内称呼(不称"身份")。"""
    assert "身份" not in messages.BIND_SUCCESS.format(group="家", name="妈妈", link="")
    assert "群内称呼" in messages.BIND_SUCCESS.format(group="家", name="妈妈", link="")

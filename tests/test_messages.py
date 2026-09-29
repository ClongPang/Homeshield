"""Session copy states private results, relation direction and available help."""
from homeshield.core import messages


async def test_welcome_discloses_private_results_and_official_fallback():
    assert "是不是骗局" not in messages.WELCOME
    assert "只回复你本人" in messages.WELCOME
    assert "每次查证都会提醒联防你的人" in messages.WELCOME
    assert "96110" in messages.WELCOME
    assert "邀请 称呼" in messages.WELCOME and "绑定 邀请码" in messages.WELCOME
    assert "也可以直接转发可疑消息给我看" in messages.WELCOME
    assert "加入后" not in messages.WELCOME


async def test_binding_copy_warns_about_query_sharing():
    from homeshield.core.commands import OLD_COMMAND_HINT

    assert "邀请 称呼" in OLD_COMMAND_HINT and "我的联防" in OLD_COMMAND_HINT

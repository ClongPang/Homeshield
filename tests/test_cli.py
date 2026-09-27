"""CLI 的演示身份、信任护栏与运维解散入口。"""
import sys

import pytest

from homeshield import cli
from homeshield.core.errors import ValidationError


def test_demo_user_and_operator_lifecycle(deps, monkeypatch, capsys):
    group_id=deps.repos.group.create("CLI 家庭")
    monkeypatch.setattr(cli.Settings,"load",lambda:deps.settings)
    monkeypatch.setattr(cli,"build_deps",lambda settings:deps)
    monkeypatch.setattr(sys,"argv",["homeshield-cli","add-member","--group-id",str(group_id),
                                   "--name","演示成员","--trusted","--demo-user"])
    cli.main()
    member=deps.repos.member.list_members(group_id)[0]
    user=deps.repos.users.get(member.user_id)
    assert user.openid.startswith("demo:") and user.token

    monkeypatch.setattr(sys,"argv",["homeshield-cli","link","--member-id",str(member.id),
                                   "--base-url","https://shield.test"])
    cli.main()
    assert user.token in capsys.readouterr().out

    monkeypatch.setattr(sys,"argv",["homeshield-cli","set-trust","--member-id",str(member.id),"--trusted","0"])
    with pytest.raises(ValidationError,match="last trusted"):
        cli.main()

    monkeypatch.setattr(sys,"argv",["homeshield-cli","disband","--group-id",str(group_id)])
    cli.main()
    assert deps.repos.group.get(group_id)["disbanded_at"] is not None
    assert deps.repos.member.get(member.id).end_reason=="disbanded"

"""绑定域:自助开通、邀请码绑定、一次性/时限与护栏。"""
import sqlite3

import pytest

from homeshield.core.binding import CREATOR_NAME, OPEN_GROUP_NAME, BindingError, BindingService


@pytest.fixture()
def service(deps):
    s = deps.settings
    return BindingService(
        deps.repos,
        max_total_groups=s.max_total_groups,
        max_members=s.max_members,
        code_ttl_days=s.bind_code_ttl_days,
    )


def test_create_initial_group_creates_trusted_creator(service, deps):
    creator = service.create_initial_group("o_a")
    assert creator.trusted is True and creator.name == CREATOR_NAME
    identity = deps.repos.users.get(creator.user_id)
    assert identity.openid == "o_a" and identity.token
    group_row = deps.repos.group.get(creator.group_id)
    assert group_row["name"] == OPEN_GROUP_NAME


def test_identity_can_create_multiple_groups_atomically(deps):
    """同一 user 可创建多个群;无效 creator 不留下孤儿家庭。"""
    creator = deps.repos.users.get_or_create("o_z")
    deps.repos.group.create_with_creator("家A", "群主", creator.id)
    deps.repos.group.create_with_creator("家B", "群主", creator.id)
    import sqlite3

    with pytest.raises(sqlite3.IntegrityError):
        deps.repos.group.create_with_creator("坏群", "群主", 999999)
    assert deps.repos.group.count_active_groups() == 2
    assert len(deps.repos.member.list_for_user(creator.id)) == 2


def test_create_initial_group_twice_rejected(service):
    service.create_initial_group("o_a")
    with pytest.raises(BindingError) as e:
        service.create_initial_group("o_a")
    assert e.value.reason == "already_has_groups"


def test_create_initial_group_stops_at_max_total_groups(service, deps):
    for i in range(deps.settings.max_total_groups):
        deps.repos.group.create(f"f{i}")
    with pytest.raises(BindingError) as e:
        service.create_initial_group("o_a")
    assert e.value.reason == "limit"


def test_issue_and_bind_flow(service, deps, group):
    group_id, elder_id, adult_id = group
    other_group = deps.repos.group.create("另一群")
    slot_id = deps.repos.member.add(other_group, "妈妈")
    code = service.issue_bind_code(deps.repos.member.get(slot_id), created_by=adult_id)
    assert len(code["code"]) == 8

    bound = service.bind_member_with_invite_code("test:mom", code["code"])
    assert bound.id == slot_id
    assert deps.repos.users.get(bound.user_id).openid == "test:mom"
    assert deps.repos.users.get(bound.user_id).token == deps.repos.users.get(
        deps.repos.member.get(elder_id).user_id
    ).token
    assert len(deps.repos.member.list_for_user(bound.user_id)) == 2

    # 一次性:领取后再绑他人无效
    with pytest.raises(BindingError) as e:
        service.bind_member_with_invite_code("o_other", code["code"])
    assert e.value.reason == "invalid"


def test_bind_unknown_code_invalid(service):
    with pytest.raises(BindingError) as e:
        service.bind_member_with_invite_code("o_x", "NOPE0000")
    assert e.value.reason == "invalid"


def test_bind_expired_code_invalid(service, deps, group):
    group_id, _, _ = group
    slot_id = deps.repos.member.add(group_id, "新成员")
    row = deps.repos.bind_code.create(slot_id, None, ttl_days=-1)  # 已过期
    with pytest.raises(BindingError) as e:
        service.bind_member_with_invite_code("o_x", row["code"])
    assert e.value.reason == "invalid"


def test_reissue_invalidates_old_code(service, deps, group):
    group_id, _, adult_id = group
    slot_id = deps.repos.member.add(group_id, "新成员")
    elder = deps.repos.member.get(slot_id)
    first = service.issue_bind_code(elder, created_by=adult_id)
    second = service.issue_bind_code(elder, created_by=adult_id)
    assert first["code"] != second["code"]

    with pytest.raises(BindingError):
        service.bind_member_with_invite_code("o_x", first["code"])  # 重发即作废
    assert service.bind_member_with_invite_code("o_x", second["code"]).id == slot_id


def test_issue_bind_code_for_bound_member_rejected(service, deps, group):
    group_id, _, adult_id = group
    slot_id = deps.repos.member.add(group_id, "新成员")
    elder = deps.repos.member.get(slot_id)
    service.bind_member_with_invite_code("o_mom", service.issue_bind_code(elder, created_by=adult_id)["code"])
    with pytest.raises(BindingError) as e:
        service.issue_bind_code(deps.repos.member.get(slot_id), created_by=adult_id)
    assert e.value.reason == "member_bound"


def test_bind_to_bound_member_keeps_code_unconsumed(service, deps, group):
    """码有效但成员位已绑定:拒绝且不消耗码(先 peek 后 claim)。"""
    group_id, _, adult_id = group
    elder_id = deps.repos.member.add(group_id, "新成员")
    elder = deps.repos.member.get(elder_id)
    code = service.issue_bind_code(elder, created_by=adult_id)
    deps.repos.member.bind_member_to_user_by_openid(elder_id, "o_first")

    with pytest.raises(BindingError) as e:
        service.bind_member_with_invite_code("o_second", code["code"])
    assert e.value.reason == "member_bound"
    assert deps.repos.bind_code.get_valid_bind_code(code["code"]) is not None


def test_bind_openid_taken_maps_to_already_bound(service, deps, group):
    """已在本群的身份不能再绑定到第二个成员位。"""
    group_id, elder_id, adult_id = group
    target = deps.repos.member.add(group_id, "另一个邀请位")
    code = service.issue_bind_code(deps.repos.member.get(target), created_by=adult_id)
    with pytest.raises(BindingError) as e:
        service.bind_member_with_invite_code("test:mom", code["code"])
    assert e.value.reason == "already_in_group"
    assert deps.repos.member.get(target).user_id is None


def test_failed_binding_transaction_keeps_invitation_reusable(service, deps, group):
    group_id, _, trusted_id = group
    slot_id = deps.repos.member.add(group_id, "新成员")
    code = service.issue_bind_code(deps.repos.member.get(slot_id), created_by=trusted_id)
    deps.conn.execute(
        "CREATE TRIGGER fail_bind_claim BEFORE UPDATE OF used_at ON bind_code "
        "BEGIN SELECT RAISE(ABORT, 'simulated write failure'); END"
    )

    with pytest.raises(BindingError) as e:
        service.bind_member_with_invite_code("o_retry", code["code"])
    assert e.value.reason == "retry"
    assert deps.repos.member.get(slot_id).user_id is None
    assert deps.repos.bind_code.get_valid_bind_code(code["code"]) is not None

    deps.conn.execute("DROP TRIGGER fail_bind_claim")
    assert service.bind_member_with_invite_code("o_retry", code["code"]).id == slot_id


def test_bind_code_case_insensitive(service, deps, group):
    group_id, _, adult_id = group
    slot_id = deps.repos.member.add(group_id, "新成员")
    code = service.issue_bind_code(deps.repos.member.get(slot_id), created_by=adult_id)
    assert service.bind_member_with_invite_code("o_mom", code["code"].lower()).id == slot_id


def test_require_member_capacity(service, deps, group):
    group_id, _, _ = group
    for i in range(deps.settings.max_members):
        deps.repos.member.add(group_id, f"m{i}")
    with pytest.raises(BindingError) as e:
        service.require_member_capacity(group_id)
    assert e.value.reason == "limit"

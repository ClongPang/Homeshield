"""绑定域:自助开通、邀请码绑定、一次性/时限与护栏。"""
import pytest

from core.binding import ADMIN_NAME, OPEN_FAMILY_NAME, BindingError, BindingService
from core.models import Role


@pytest.fixture()
def service(deps):
    s = deps.settings
    return BindingService(
        deps.repos,
        max_families=s.max_families,
        max_members=s.max_members,
        code_ttl_days=s.bind_code_ttl_days,
    )


def test_open_family_creates_admin(service, deps):
    admin = service.open_family("o_a")
    assert admin.role is Role.ADULT and admin.name == ADMIN_NAME
    assert admin.openid == "o_a" and admin.token
    fam = deps.repos.family.get(admin.family_id)
    assert fam["name"] == OPEN_FAMILY_NAME


def test_create_with_admin_single_transaction(deps):
    """建家与管理员同一事务:成员 openid 唯一冲突时不留孤儿家庭。"""
    deps.repos.family.create_with_admin("家A", "管理员", "o_z")
    import sqlite3

    with pytest.raises(sqlite3.IntegrityError):
        deps.repos.family.create_with_admin("家B", "管理员", "o_z")  # openid 冲突
    assert deps.repos.family.count() == 1  # 家B 未落库


def test_open_family_twice_rejected(service):
    service.open_family("o_a")
    with pytest.raises(BindingError) as e:
        service.open_family("o_a")
    assert e.value.reason == "already_bound"


def test_open_family_stops_at_max_families(service, deps):
    for i in range(deps.settings.max_families):
        deps.repos.family.create(f"f{i}")
    with pytest.raises(BindingError) as e:
        service.open_family("o_a")
    assert e.value.reason == "limit"


def test_issue_and_bind_flow(service, deps, family):
    fid, elder_id, adult_id = family
    code = service.issue_code(deps.repos.member.get(elder_id), created_by=adult_id)
    assert len(code["code"]) == 8

    bound = service.bind("o_mom", code["code"])
    assert bound.id == elder_id and bound.openid == "o_mom"

    # 一次性:领取后再绑他人无效
    with pytest.raises(BindingError) as e:
        service.bind("o_other", code["code"])
    assert e.value.reason == "invalid"


def test_bind_unknown_code_invalid(service):
    with pytest.raises(BindingError) as e:
        service.bind("o_x", "NOPE0000")
    assert e.value.reason == "invalid"


def test_bind_expired_code_invalid(service, deps, family):
    fid, elder_id, _ = family
    row = deps.repos.bind_code.create(elder_id, None, ttl_days=-1)  # 已过期
    with pytest.raises(BindingError) as e:
        service.bind("o_x", row["code"])
    assert e.value.reason == "invalid"


def test_reissue_invalidates_old_code(service, deps, family):
    fid, elder_id, adult_id = family
    elder = deps.repos.member.get(elder_id)
    first = service.issue_code(elder, created_by=adult_id)
    second = service.issue_code(elder, created_by=adult_id)
    assert first["code"] != second["code"]

    with pytest.raises(BindingError):
        service.bind("o_x", first["code"])  # 重发即作废
    assert service.bind("o_x", second["code"]).id == elder_id


def test_issue_code_for_bound_member_rejected(service, deps, family):
    fid, elder_id, adult_id = family
    elder = deps.repos.member.get(elder_id)
    service.bind("o_mom", service.issue_code(elder, created_by=adult_id)["code"])
    with pytest.raises(BindingError) as e:
        service.issue_code(deps.repos.member.get(elder_id), created_by=adult_id)
    assert e.value.reason == "member_bound"


def test_bind_to_bound_member_keeps_code_unconsumed(service, deps, family):
    """码有效但成员位已绑定:拒绝且不消耗码(先 peek 后 claim)。"""
    fid, elder_id, adult_id = family
    elder = deps.repos.member.get(elder_id)
    code = service.issue_code(elder, created_by=adult_id)
    deps.repos.member.set_openid(elder_id, "o_first")

    with pytest.raises(BindingError) as e:
        service.bind("o_second", code["code"])
    assert e.value.reason == "member_bound"
    assert deps.repos.bind_code.peek(code["code"]) is not None


def test_bind_openid_taken_maps_to_already_bound(service, deps, family):
    """openid 已属别的成员:换绑被拒,映射为 already_bound。"""
    fid, elder_id, adult_id = family
    deps.repos.member.add(fid, "别人", Role.ELDER, openid="o_taken")
    code = service.issue_code(deps.repos.member.get(elder_id), created_by=adult_id)
    with pytest.raises(BindingError) as e:
        service.bind("o_taken", code["code"])
    assert e.value.reason == "already_bound"
    assert deps.repos.member.get(elder_id).openid is None


def test_bind_code_case_insensitive(service, deps, family):
    fid, elder_id, adult_id = family
    code = service.issue_code(deps.repos.member.get(elder_id), created_by=adult_id)
    assert service.bind("o_mom", code["code"].lower()).id == elder_id


def test_ensure_member_capacity(service, deps, family):
    fid, _, _ = family
    for i in range(deps.settings.max_members):
        deps.repos.member.add(fid, f"m{i}", Role.ELDER)
    with pytest.raises(BindingError) as e:
        service.ensure_member_capacity(fid)
    assert e.value.reason == "limit"

"""多群查询快照、去重推送和静音口径验收。"""
import asyncio

from homeshield.core.deps import make_pipeline
from homeshield.core.intake import ingest


class FakeTemplates:
    def __init__(self):
        self.sent = []

    async def send_template(self, openid, data, url=None, template_id=None):
        self.sent.append({"openid":openid,"data":data,"url":url,"template_id":template_id})


def test_alert_fanout_snapshot_user_dedupe_mute_and_template_choice(deps):
    repo=deps.repos
    first=repo.group.create("妈妈家")
    second=repo.group.create("岳父家")
    queryer_a=repo.member.get(repo.member.add(first,"查询者",openid="test:queryer"))
    queryer_b=repo.member.get(repo.member.add(second,"查询者",openid="test:queryer"))
    target_a=repo.member.get(repo.member.add(first,"女儿",openid="live:target"))
    target_b=repo.member.get(repo.member.add(second,"女儿",openid="live:target"))
    single=repo.member.get(repo.member.add(first,"叔叔",openid="live:single"))
    repo.member.set_mute(target_a.id,True)

    snapshot=repo.member.list_for_user(queryer_a.user_id)
    intake=ingest(repo,user_id=queryer_a.user_id,memberships=snapshot,
                  content="别告诉家人,立即转账")

    # 查询受理后才入第三群:这条旧查询不会广播到第三群。
    third=repo.group.create("新加入的群")
    target_c=repo.member.get(repo.member.add(third,"女儿",openid="live:target"))
    # 查询者在判定前退出 A 并加入 C。快照仍是 A/B，但退出者不再是 A 的接收者。
    deps.groups.leave_group(queryer_a.user_id,first)
    repo.member.add(third,"查询者",openid="test:queryer")

    fake=FakeTemplates()
    deps.alert_router.wechat=fake
    deps.alert_router.template_id="SINGLE"
    deps.alert_router.multi_template_id="MULTI"
    deps.alert_router.base_url="https://shield.test"
    stream=deps.broker.subscribe(target_a.user_id)

    result=asyncio.run(make_pipeline(deps).run(intake.message,intake.query_id))
    sent=fake.sent
    multi=[item for item in sent if item["openid"]=="live:target"]
    single_sent=[item for item in sent if item["openid"]=="live:single"]
    assert len(multi)==1 and multi[0]["template_id"]=="MULTI"
    assert multi[0]["data"]=={
        "thing1":{"value":"别告诉家人,立即转账"},
        "phrase1":{"value":"高危预警"},
        "thing2":{"value":"岳父家"},
    }
    assert len(single_sent)==1 and single_sent[0]["template_id"]=="SINGLE"
    assert single_sent[0]["data"]=={
        "thing1":{"value":"别告诉家人,立即转账"},
        "phrase1":{"value":"高危预警"},
    }

    payload=stream.get_nowait()
    assert payload["group_ids"]==[first,second]
    assert payload["group_names"]==["妈妈家","岳父家"]
    alert_groups={r[0] for r in repo.conn.execute(
        "SELECT DISTINCT m.group_id FROM alert a JOIN member m ON m.id=a.membership_id WHERE a.verdict_id=?",
        (result.verdict_id,),
    )}
    assert alert_groups=={first,second} and third not in alert_groups
    assert repo.alert.list_alerts_for_user_in_group(target_a.user_id,third)==[]
    assert repo.alert.list_alerts_for_user_in_group(queryer_a.user_id,first)==[]
    assert len(repo.alert.list_alerts_for_user_in_group(queryer_a.user_id,second))==1
    assert target_c.user_id==target_a.user_id and queryer_b.user_id==queryer_a.user_id

    # 缺少多群模板时跳过多群接收者,不把 thing2 塞进旧模板。
    deps.alert_router.multi_template_id=""
    next_intake=ingest(repo,user_id=queryer_a.user_id,memberships=repo.member.list_for_user(queryer_a.user_id),
                       content="别告诉家人,马上转账救急")
    asyncio.run(make_pipeline(deps).run(next_intake.message,next_intake.query_id))
    assert len([item for item in fake.sent if item["openid"]=="live:target"])==1
    missing_template_payload=stream.get_nowait()
    assert missing_template_payload["group_ids"]==[second,third]

    # 两群均静音时不发微信模板,但 alert 与 SSE 仍按相关群保留。
    repo.member.set_mute(target_b.id,True)
    muted_queryer=repo.member.get(repo.member.add(first,"查询者",openid="test:muted-queryer"))
    repo.member.add(second,"查询者",openid="test:muted-queryer")
    both_muted=ingest(repo,user_id=muted_queryer.user_id,memberships=repo.member.list_for_user(muted_queryer.user_id),
                      content="别告诉家人,马上转账救急")
    asyncio.run(make_pipeline(deps).run(both_muted.message,both_muted.query_id))
    assert len([item for item in fake.sent if item["openid"]=="live:target"])==1
    assert len(repo.alert.list_alerts_for_user_in_group(target_a.user_id,first))==2
    assert len(repo.alert.list_alerts_for_user_in_group(target_a.user_id,second))==3
    second_payload=stream.get_nowait()
    assert second_payload["group_ids"]==[first,second]


def test_disband_before_verdict_creates_no_alert_and_says_so(deps):
    owner=deps.binding.create_initial_group("test:owner","临时群")
    memberships=deps.repos.member.list_for_user(owner.user_id)
    intake=ingest(deps.repos,user_id=owner.user_id,memberships=memberships,
                  content="别告诉家人,立即转账")
    deps.groups.disband_group(owner.user_id,owner.group_id)
    result=asyncio.run(make_pipeline(deps).run(intake.message,intake.query_id))
    assert result.verdict.level.value=="dangerous"
    assert "本次未通知群成员" in result.reply
    assert deps.conn.execute("SELECT COUNT(*) FROM alert WHERE verdict_id=?",(result.verdict_id,)).fetchone()[0]==0


def test_demo_and_test_identities_never_receive_wechat_templates(deps):
    group=deps.repos.group.create("演示群")
    demo=deps.repos.member.get(deps.repos.member.add(group,"演示用户",openid="demo:sample"))
    queryer=deps.repos.member.get(deps.repos.member.add(group,"测试用户",openid="test:queryer"))
    fake=FakeTemplates()
    deps.alert_router.wechat=fake
    deps.alert_router.template_id="SINGLE"
    intake=ingest(deps.repos,user_id=queryer.user_id,
                  memberships=deps.repos.member.list_for_user(queryer.user_id),
                  content="别告诉家人,立即转账")
    asyncio.run(make_pipeline(deps).run(intake.message,intake.query_id))
    assert fake.sent==[]
    assert deps.repos.alert.list_alerts_for_user_in_group(demo.user_id,group)


def test_sse_subscribers_receive_independent_payloads(deps):
    user_id = deps.repos.users.get_or_create("test:stream-copy").id
    first = deps.broker.subscribe(user_id)
    second = deps.broker.subscribe(user_id)
    payload = {"group_ids": [10, 20], "group_names": ["A", "B"]}

    deps.broker.publish_alert(user_id, payload)
    first_payload = first.get_nowait()
    first_payload["group_ids"].remove(10)
    first_payload["group_names"].remove("A")
    second_payload = second.get_nowait()

    assert second_payload["group_ids"] == [10, 20]
    assert second_payload["group_names"] == ["A", "B"]
    assert first_payload is not second_payload

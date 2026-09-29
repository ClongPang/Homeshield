"""企业微信(微信客服)API 冒烟测试:token → 拉取消息 → 回复一条。

只依赖标准库;密钥只在本地读取,输出全部脱敏。
用法:python3 scripts/wecom_smoke.py [回复文本]
不传回复文本时只做只读检查(gettoken + sync_msg)。
"""
import json
import pathlib
import sys
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent


def load_env():
    env = {}
    for line in (ROOT / ".env").read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"')
    return env


def call(url, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def mask(s):
    return (s[:4] + "****") if s else "(empty)"


def main():
    env = load_env()
    corpid, agent_id = env.get("WECOM_CORPID"), env.get("WECOM_AGENT_ID")
    # 优先用微信客服自己的 secret(天然管理全部客服账号);否则用已授权的自建应用 secret
    secret = env.get("WECOM_KF_SECRET") or env.get("WECOM_APP_SECRET")
    which = "KF" if env.get("WECOM_KF_SECRET") else "APP"
    print(f"corpid={mask(corpid)} secret={mask(secret)}({which}) agent_id={mask(agent_id or '')}")

    tok = call(
        "https://qyapi.weixin.qq.com/cgi-bin/gettoken"
        f"?corpid={corpid}&corpsecret={secret}"
    )
    print(f"gettoken errcode={tok.get('errcode')} errmsg={tok.get('errmsg')}")
    if tok.get("errcode") != 0:
        return
    access_token = tok["access_token"]

    sm = call("https://qyapi.weixin.qq.com/cgi-bin/kf/sync_msg?access_token=" + access_token,
              {"cursor": "", "token": "", "limit": 1000})
    print(f"sync_msg errcode={sm.get('errcode')} errmsg={sm.get('errmsg')}")
    if sm.get("errcode") != 0:
        return
    msgs = sm.get("msg_list", [])
    print(f"messages={len(msgs)} has_more={sm.get('has_more')}")
    target = None
    for m in msgs:
        origin = m.get("origin")
        who = mask(m.get("external_userid", ""))
        if m.get("msgtype") == "text":
            content = m.get("text", {}).get("content", "")[:40]
        else:
            content = f"[{m.get('msgtype')}]"
        print(f"  origin={origin} from={who} open_kfid={mask(m.get('open_kfid',''))} {content}")
        # origin=3 为客户发来的消息,作为回复目标
        if origin == 3 and target is None:
            target = (m.get("external_userid"), m.get("open_kfid"))

    reply = sys.argv[1] if len(sys.argv) > 1 else None
    if reply is None:
        print("(只读模式,未发送)")
        return
    if target is None:
        print("没有可回复的客户消息:请先用微信打开小盾链接并发送一条消息,再重试。")
        return
    ext_uid, open_kfid = target
    snd = call(
        "https://qyapi.weixin.qq.com/cgi-bin/kf/send_msg?access_token=" + access_token,
        {"touser": ext_uid, "open_kfid": open_kfid, "msgtype": "text", "text": {"content": reply}},
    )
    print(f"send_msg errcode={snd.get('errcode')} errmsg={snd.get('errmsg')}")
    if snd.get("errcode") == 0:
        for item in snd.get("fail_list", []):
            print(f"  FAIL external_userid={mask(item.get('external_userid',''))} "
                  f"errcode={item.get('errcode')} ({item.get('errmsg')})")
        print("✅ 未验证主体发送成功——黄灯解除")
    else:
        print("❌ 发送失败,按 errcode 对照官方文档排查(8=主体未验证,60020=可信IP)")


if __name__ == "__main__":
    main()

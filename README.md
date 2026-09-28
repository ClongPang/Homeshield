# 家中盾（Homeshield）

家中盾为个人提供反诈查证，并允许用户自愿建立单向联防关系。当前实现以[关系模型实施规格 v1.2](docs/家中盾_关系模型_实施规格.md)为准；多群规格仅作历史依据。

## 开发

```bash
uv sync
uv run pytest
uv run homeshield-cli init-db
uv run uvicorn homeshield.server:app --reload
uv run homeshield-eval
```

复制 `.env.example` 到 `.env`。`MODE=mock` 无需外部 API；`MODE=llm` 时配置兼容 OpenAI 的供应商参数。

家人在微信客服会话(企微)中首次发送文字、URL 或图片时自动创建个人身份,并完整进入查证流程;首次进入会话由后端发送欢迎语。查询结果只回复查询者，不要求建立联防关系。用户可回复「邀请 称呼」发出单向邀请、「绑定 邀请码」接受邀请、「我的联防」查看关系、「解除 #关系ID」解除关系。

`PUBLIC_BASE_URL` 配置后，首次处理完成时通过客服消息单独发送个人控制台链接。邀请页不展示个人 token；网页查询和控制台均要求个人 token。

## 关键行为

- 查询受理时快照查询者当时的活跃 incoming 关系；判定为高危时，只为仍活跃的快照关系创建提醒。解除后无法查看该关系收到的历史提醒。
- 每条关系独立静音应用消息；提醒记录和 SSE 不受静音影响。高危提醒经企业微信应用消息送达已登记映射(`homeshield-cli wecom-link`)的联防者微信,未登记映射的会话联防者回落其客服会话。
- 查询者的纠正是普通反馈，不计入投票。危险判定由收到提醒的活跃联防者投票；低风险判定由查询者主动反馈后，查询时关系快照中的活跃联防者投票。超过固定投票人数一半的标签才会确认；零人或期限内无多数记为 `no_consensus`。
- `MAX_RELATIONS` 限制每个用户进出合计的活跃关系数；`INVITE_CODE_TTL_DAYS` 设置邀请码期限；`CORRECTION_WINDOW_DAYS` 设置投票期限。
- 关系模型替换群容器，不提供旧库迁移。开发库需重建；旧群成员不会自动转成关系。

## 目录

```text
src/homeshield/
  server.py                 FastAPI 组合根
  api/relations.py          个人关系、查询、提醒和投票 API
  api/wecom.py              企微客服回调、拉取轮询与消息派发
  core/commands.py          会话关系指令(通道无关)
  core/relations.py         单向关系和邀请规则
  core/repo.py              SQLite 仓储与事务
  core/db.py                当前数据库 schema
  core/{pipeline,features,judge,reply}.py  查证管线
  web/                      查询页、控制台、提醒页、邀请页
  eval/                     离线评测
  kbbuild/                  离线素材库工具
tests/                      判定回归、关系模型和 API 验收
docs/                       产品、架构和实施规格
```

微信模板字段、客服链接投递以及模板落地页仍需在部署前用真实测试号验证。

# 家中盾（Homeshield）

家中盾为个人提供反诈查证，并允许用户自愿建立单向联防关系。当前实现以[关系模型实施规格 v1.3](docs/家中盾_关系模型_实施规格.md)为准；多群规格仅作历史依据。

## 开发

```bash
cp .env.example .env  # 修改 POSTGRES_PASSWORD，并同步 DATABASE_URL 中的口令
uv sync
docker compose up -d --wait postgres
uv run homeshield-cli init-db
uv run uvicorn homeshield.server:app --reload
uv run homeshield-eval
```

评测数据集在 `data/`（构成、字段与再生成见 [data/README.md](data/README.md)）。

运行服务需要 Docker Postgres 16；默认只监听本机 `127.0.0.1:5432`，数据放在命名卷。运行数据经 `DATABASE_URL` 访问 Postgres；`DB_PATH` 配置离线 `kbbuild` SQLite 素材库（默认 `data/kb_build.db`，也可用 `--db` 覆盖）。`.env` 不得提交。`MODE=mock` 无需外部 API；`MODE=llm` 时配置兼容 OpenAI 的供应商参数。

测试使用独立的 `homeshield_test` 库（`TEST_DATABASE_URL`），不会清空 `DATABASE_URL` 指向的业务库：

```bash
./scripts/test.sh
```

`homeshield-eval` 同样只写隔离评测库：默认取 `TEST_DATABASE_URL`（未设置时派生 `<主库名>_test` 并自动建库），可用 `--database-url` 覆盖；目标与 `DATABASE_URL` 同名会拒绝运行。

单机开发可用 `--reload`；多 worker 部署使用 `uv run uvicorn homeshield.server:app --workers 2`。每个 worker 使用独立连接池，企微 poller 由 Postgres advisory lock 选主，客服回调经 Postgres `LISTEN/NOTIFY` 唤醒 leader 所在进程立即拉取，告警亦经 `LISTEN/NOTIFY` 广播到各 worker 的 SSE broker。

本服务作为多家庭共用入口运行；当前企微侧只有一个微信客服账号承载收发。代码按 `list_kf_accounts` 返回值逐账号维护游标，不限定未来账号数量。

Postgres 备份与空库恢复脚本：`scripts/backup_postgres.sh`、`scripts/restore_postgres.sh`。备份默认保留 14 天；生产切换与回滚步骤见 [`deploy/postgres-cutover.md`](deploy/postgres-cutover.md)。

家人在微信客服会话(企微)中首次发送文字、URL 或图片时自动创建个人身份,并完整进入查证流程;首次进入会话由后端发送欢迎语。查询结果只回复查询者，不要求建立联防关系。用户可回复「邀请 称呼」发出单向邀请、「绑定 邀请码」接受邀请、「我的联防」查看关系、「解除 #关系ID」解除关系。

`PUBLIC_BASE_URL` 配置后，首次处理完成时通过客服消息单独发送个人控制台链接。邀请页不展示个人 token；网页查询和控制台均要求个人 token。

## 关键行为

- 查询受理时快照查询者当时的活跃 incoming 关系；判定完成时（不论等级——查询本身就说明用户起了疑心），只为仍活跃的快照关系创建提醒。解除后无法查看该关系收到的历史提醒。
- 每条关系独立静音应用消息；提醒记录和 SSE 不受静音影响。查询提醒经企业微信应用消息送达已登记映射(`homeshield-cli wecom-link`)的联防者微信,未登记映射的会话联防者回落其客服会话；提醒措辞随判定等级变化（高危预警 / 留意核实 / 未发现典型骗术特征）。
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
  core/repo.py              Postgres async 仓储与事务
  core/db.py                Postgres schema 与连接池
  core/{pipeline,features,judge,reply}.py  查证管线
  web/                      查询页、控制台、提醒页、邀请页
  eval/                     离线评测
  kbbuild/                  离线素材库工具
tests/                      判定回归、关系模型和 API 验收
docs/                       产品、架构和实施规格
```

微信模板字段、客服链接投递以及模板落地页仍需在部署前用真实测试号验证。

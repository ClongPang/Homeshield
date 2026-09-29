# 验收追踪

35 个 EARS 场景逐项映射到自动化用例、开发机实测或部署后人工记录。开发机压测只作参考；标记“待部署”的项目仍是生产切换门禁。

## Storage

| 场景 | 验收证据 | 状态 |
|---|---|---|
| 依赖装配经连接池 | `tests/test_architecture.py::test_adapters_are_the_only_infra_users`、`tests/conftest.py` | 自动通过 |
| kbbuild 豁免 | `tests/test_architecture.py::test_sqlite_is_confined_to_offline_kbbuild`、`tests/test_kbbuild.py` | 自动通过 |
| 幂等建库 | `tests/test_db_schema.py::test_schema_init_is_idempotent_and_records_version` | 自动通过 |
| 部分唯一索引语义平移 | `tests/test_db_schema.py::test_partial_message_id_index_allows_null_but_rejects_duplicate`、`tests/test_pg_concurrency.py::test_concurrent_duplicate_message_maps_only_msg_id_constraint` | 自动通过 |
| ingest 原子性 | `tests/test_intake.py::test_query_and_relation_snapshot_roll_back_together` | 自动通过 |
| 管线失败降级语义保持 | `tests/test_pipeline.py::test_unexpected_pipeline_failure_keeps_ingested_query_and_returns_fallback`、`tests/test_pipeline.py::test_expected_pipeline_degradation_is_persisted_by_verification` | 自动通过 |
| 并发双插同一 msg_id | `tests/test_pg_concurrency.py::test_concurrent_duplicate_message_maps_only_msg_id_constraint` | 自动通过 |
| 企微重启重放 | `tests/test_wecom_flow.py::test_duplicate_msgid_not_reprocessed` | 自动通过；真机重启重放待部署 |
| 并发核销同一邀请码 | `tests/test_pg_concurrency.py::test_concurrent_invite_claim_returns_created_and_used`；`test_concurrent_distinct_invites_respect_relation_capacity` 验证不同邀请码争抢同一用户名额时仍受 `MAX_RELATIONS` 约束 | 自动通过 |
| 并发为首条判定建纠正案 | `tests/test_corrections.py::test_concurrent_case_open_and_decisive_votes_are_serialized` | 自动通过 |
| 并发开案挂靠 | `tests/test_incident.py::test_concurrent_first_queries_one_open_incident` | 自动通过 |
| 迁移后行为等价 | 本次隔离 SQLite→PG 演练：12 张源表计数一致；核对原 ID、时间戳、boolean、jsonb、msg_id 查询与 identity 续号；见 [`postgres-acceptance-2026-09-29.md`](../../../docs/reports/postgres-acceptance-2026-09-29.md) | 自动/隔离演练通过 |

## Runtime

| 场景 | 验收证据 | 状态 |
|---|---|---|
| 事件循环零 DB 阻塞 | 全量 async PG 测试；压力报告中的慢判定期间读请求 | 开发机通过；部署机待验 |
| 旧并发设施清零 | `rg` 检查生产源码无 `WRITE_LOCK`、`_serialize_repo_access`、`asyncio.to_thread`、`ThreadPoolExecutor`；`tests/test_architecture.py` 检查 SQLite 边界与 HTTP 路由 async 声明 | 自动/静态通过 |
| 双 worker 并存 | `scripts/multiworker_smoke.py`（强制 acceptance DB、清空企微配置、自动清理标记数据）；另有 `tests/test_multi_worker.py` | 开发机通过；部署机待验 |
| 单客服账号部署事实 | 代码按 `list_kf_accounts` 迭代；只读企微 API 确认当前账号数为 1；未发消息 | 部分通过；真机收发待部署 |
| 单主轮询与接管 | `tests/test_multi_worker.py::test_two_app_pollers_elect_one_and_handoff_with_durable_cursor` | 自动通过；真机双 Uvicorn 复核待部署 |
| 游标恢复 | 同上，断言接管方首轮使用数据库游标 | 自动通过；真机复核待部署 |
| 跨 worker 告警触达 | `scripts/multiworker_smoke.py` 与 `tests/test_multi_worker.py::test_two_app_instances_broadcast_sse_alert_once_on_postgres` | 开发机通过；部署机 SSE 复核待部署 |
| 双 worker 并发消费 | `scripts/multiworker_smoke.py` 与 app 集成测试；alert 恰一行，重复 MsgId 返回 409 且 query 恰一行 | 开发机通过；部署机并发复核待部署 |
| CLI 管理命令 | `tests/test_cli.py`、`tests/test_session_eval.py` | 自动通过 |
| 单条数据库操作开销 | 1000 次点查/插入；压力报告 P99 分别 2.307/2.311 ms | 开发机通过；部署机待验 |
| 读接口并发不排队 | 50 并发读 P99 279.129 ms、零 5xx | 开发机通过；部署机待验 |
| 读写互不阻塞 | 10 个慢判定期间 50 次读 P99 213.527 ms、10 个判定全部完成 | 开发机通过；部署机待验 |
| 持续混合负载 | 20 QPS × 60 秒、1200 请求、P99 41.876 ms、零 5xx | 开发机通过；部署机待验 |

## Deployment

| 场景 | 验收证据 | 状态 |
|---|---|---|
| 一键起库 | 当前 Compose Postgres 16 healthcheck healthy；`./scripts/test.sh` 自动等待健康 | 本机通过 |
| 端口默认与覆盖 | 当前绑定 `127.0.0.1:5432`；Compose 配置解析覆盖为 `127.0.0.1:15432` | 本机通过 |
| 数据持久化 | 独立 Compose 项目写入探针行，执行 `down`/`up` 后读取成功；临时卷已清理 | 隔离通过 |
| 测试间隔离 | `tests/conftest.py` 每测试 `TRUNCATE … RESTART IDENTITY CASCADE`；Postgres 全量测试通过 | 自动通过 |
| 里程碑门禁 | `UV_CACHE_DIR=/tmp/fraud-uv-cache ./scripts/test.sh` → 221 passed | 自动通过 |
| 备份可恢复 | 当前 schema 3 的合成 user/query/degraded_reply 经 pg_dump 与空库 pg_restore 恢复；保留主键、msg_id、降级回复与版本号；临时库/归档已清理 | 隔离通过 |
| 新环境按文档可复现 | 临时 `UV_PROJECT_ENVIRONMENT` 中执行锁定依赖 `uv sync`，独立 Compose Postgres 健康、`homeshield-cli init-db`、Uvicorn 首页及 mock 查询均通过；部署机仍需复核 | 本机新环境通过；部署机待验 |
| 无口令即拒绝启动 | `POSTGRES_PASSWORD='' docker compose up -d postgres` 在容器变更前以明确配置错误拒绝 | 本机通过 |
| 端口不对外暴露 | Compose 端口绑定及运行容器检查均为 `127.0.0.1:5432` | 本机通过 |
| 回滚可行性 | runbook 已写；切换前 SQLite 与旧版恢复尚未在部署机做分钟级演练 | 待部署 |

## 部署后剩余门禁

- 部署机重复运行压力基线并归档机器规格与 JSON 报告。
- 生产停写、SQLite 备份、迁移、数据核对与服务冒烟。
- 旧版 + SQLite 快照分钟级回滚演练。
- 两个真实 Uvicorn worker 的企微轮询接管、SSE 触达、消息收发与重启 MsgId 重放。
- 从干净部署环境按文档复现完整启动。

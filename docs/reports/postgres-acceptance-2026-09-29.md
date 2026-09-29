# Postgres 改造验收记录（开发环境）

- 日期：2026-09-29（Asia/Shanghai）
- 范围：M1/M2/M3 自动化与隔离验收；不代表生产切换已完成。
- PostgreSQL：16.15；开发机配置与性能结果见 [`postgres-dev-pressure-2026-09-29.json`](postgres-dev-pressure-2026-09-29.json)。

## 结果

- 完整门禁：`UV_CACHE_DIR=/tmp/fraud-uv-cache ./scripts/test.sh`，**221 passed in 9.80s**。
- 新增回归：Schema 重复及并发初始化与版本记录、部分唯一索引允许多个 NULL、query 与关系快照同事务回滚、正常/意外降级回复留存、`init-db` 不打印连接口令；不同邀请码并发争抢关系名额、HTTP 路由 async 声明及 `DB_PATH` 离线库配置。
- 迁移演练使用合成的旧版 SQLite schema 与独立临时 Postgres 数据库；12 张源表计数一致，原 query ID 73、Unix 时间戳、boolean `mute` 与 JSONB 值保留；msg_id 查询命中原行；新 user/query identity 为 43/75。临时数据库与 SQLite 文件已清理。
- Compose 隔离项目验证命名卷经 `down`/`up` 保留数据，随后清理临时卷。
- 配置验收：空 `POSTGRES_PASSWORD` 下 `docker compose up` 在容器变更前明确拒绝；端口覆盖解析为 `127.0.0.1:15432`；当前服务默认绑定 `127.0.0.1:5432`。
- README 新环境冒烟：临时 uv 虚拟环境执行锁定依赖同步，独立 Compose PG 健康，`init-db` 成功，Uvicorn `/` 返回 200，mock `/api/query` 返回 200 并落库 verdict；验收使用空企微配置，未触发外部消息。
- 进程级多 worker 冒烟：以 `scripts/multiworker_smoke.py` 在隔离库启动两个 Uvicorn 实例、每实例 2 workers；worker A 查询返回 200，worker B SSE 收到 verdict；同 MsgId 重放返回 409，query 与 alert 各一行。脚本要求数据库名含 `acceptance`、清空企微配置并自动清理合成数据。此项同时发现并修复多 worker 冷启动 schema DDL 死锁：`init_schema` 现在使用事务级 advisory lock，另有四路并发初始化回归测试。
- 当前 schema 3 的合成 user/query/degraded_reply 经 `pg_dump` 与空库 `pg_restore` 恢复，用户 ID、查询 ID、MsgId、降级回复及 schema 版本均保留；临时库和归档已清理。

## 性能参考（开发机）

开发压力报告五项门槛全部通过：点查 P99 2.307 ms、单行插入 P99 2.311 ms、50 并发读 P99 279.129 ms、10 个慢判定期间读 P99 213.527 ms、20 QPS × 60 秒混合请求 P99 41.876 ms，均为零 5xx。该报告来自两个 Uvicorn 实例、每实例 2 workers 的隔离验收库，仅作开发机参考。

## 尚未完成

部署机压力基线、生产数据切换、分钟级旧版回滚演练、真实企微双 worker 收发/SSE/重启幂等，以及部署机复核，仍需在部署环境执行。当前不将 M3/切换任务标记为完成。

## 验收脚本清理记录

一次进程级 smoke 的数据准备误读仓库 `.env`，在其配置的数据库创建了两个带 `worker-smoke:` 标记的用户、两个邀请码和一条关系。未创建查询/判定/告警，也未发送企微消息。核对无业务依赖后已删除这些精确标记行；身份序列未回退，因此可能留下 ID 空洞。修正后的脚本显式接收 scratch 数据库 URL，后续验收只写隔离库。

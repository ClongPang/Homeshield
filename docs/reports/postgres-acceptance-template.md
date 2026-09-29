# Postgres 切换验收记录

复制本模板作为每次部署验收记录，填入实际机器、命令和输出。开发机报告只作参考，不替代部署机基线与企微实号验证。

## 自动验收

| 项目 | 结果 | 证据 |
|---|---|---|
| `./scripts/test.sh` 全量测试 | 待执行 | |
| 两 Uvicorn 实例（各 2 workers）SSE 广播、alert 唯一行 | 待执行 | `scripts/multiworker_smoke.py`、`tests/test_multi_worker.py` |
| advisory lock 单主、停止后接管、游标恢复 | 待执行 | `tests/test_multi_worker.py` |
| SQLite→PG 行数、主键与 identity 续号 | 待执行 | `scripts/migrate_sqlite_to_pg.py` 演练输出 |
| `pg_dump` → 空库 `pg_restore` 与计数对照 | 待执行 | `scripts/backup_postgres.sh` / `scripts/restore_postgres.sh` |

## 部署机性能验收

- 日期/时区：
- 代码版本：
- 操作系统/CPU/内存：
- PostgreSQL 版本与部署形态：
- worker 数：
- `MOCK_JUDGE_DELAY_SECONDS`：
- 压测账号（只记录标识，不记录 token）：
- 命令与 JSON 报告路径：

| 门槛 | 目标 | 实测 | 通过 |
|---|---:|---:|---|
| 连接池点查 P99 | < 5 ms | | |
| 单行插入 P99 | < 5 ms | | |
| 50 并发读接口 P99 | < 500 ms、零 5xx | | |
| 10 个慢判定期间读接口 P99 | < 500 ms、查询完成 | | |
| 20 QPS × 60 秒混合流量 | P99 < 1 s、零 5xx | | |

## 双 worker 与真实渠道

- 两个 worker 启动/健康检查：
- 企微测试账号与消息收发：
- poller 当前 leader、接管与 `kf_cursor`：
- worker B 的 SSE 收到 worker A verdict：
- 重启后 MsgId 重放与重复回复：
- 错误日志/回复可达性：
- 切换备份位置、校验和、恢复耗时：
- 回滚演练耗时与结果：

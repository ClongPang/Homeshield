# Postgres 切换、回滚与验收 Runbook

适用于**当前单向关系模型 SQLite schema**。旧群模型库不属于可迁移源；本流程不删除旧版本、SQLite 原文件或备份。

## 切换前

1. 在维护窗口操作。记录当前代码版本、服务启动命令和 `.env` 副本位置；从旧版本配置读取 SQLite 文件路径并记为 `LEGACY_DB_PATH`。将新版配置的 `DB_PATH` 设为 `data/kb_build.db`；它供离线 `kbbuild` 使用，不得用作迁移源路径。
2. 确认 Postgres 16 已健康、5432 仅监听预期内网接口、`DATABASE_URL` 指向空目标库，且口令不在命令历史或版本库。
3. 先在目标版本运行 `./scripts/test.sh`。切换期间停止旧服务和所有写入者：

   ```bash
   systemctl stop homeshield
   # macOS 使用对应 launchd unload 命令
   ```

4. 停服后用 SQLite backup API 生成一致性副本，保留原文件：

   ```bash
   : "${LEGACY_DB_PATH:?Set the SQLite path from the previous version}"
   mkdir -p "$(dirname "$SQLITE_BACKUP")"
   sqlite3 "$LEGACY_DB_PATH" ".backup '$SQLITE_BACKUP'"
   test -s "$SQLITE_BACKUP"
   if command -v sha256sum >/dev/null; then sha256sum "$SQLITE_BACKUP"; else shasum -a 256 "$SQLITE_BACKUP"; fi
   ```

## 迁移与核对

1. 使用仅有迁移权限且指向空库的 `DATABASE_URL`，执行一次性迁移。迁移程序会在事务内校验每张源表和目标表行数；目标已有业务行会拒绝继续。

   ```bash
   uv run python scripts/migrate_sqlite_to_pg.py "$SQLITE_BACKUP"
   ```

   也可显式传 `--database-url "$DATABASE_URL"`。输出的逐表计数应保存到切换记录。
2. 独立复核关键数据（将 `SOURCE_SQLITE` 替换为备份路径）：

   ```bash
   sqlite3 "$SOURCE_SQLITE" "SELECT COUNT(*) FROM query WHERE msg_id IS NOT NULL;"
   psql "$DATABASE_URL" -Atc "SELECT COUNT(*) FROM query WHERE msg_id IS NOT NULL;"
   sqlite3 "$SOURCE_SQLITE" "SELECT COUNT(*) FROM alert; SELECT COUNT(*) FROM correction_case;"
   psql "$DATABASE_URL" -Atc "SELECT (SELECT COUNT(*) FROM alert), (SELECT COUNT(*) FROM correction_case);"
   ```

   抽取一个非空 `msg_id`，分别在源和目标查询并核对用户/内容；各表行数必须相同。`kf_cursor` 在首次迁移时为空，启动后由 poller 建立。
3. 执行 `uv run homeshield-cli init-db` 验证 schema 幂等。使用数据库名包含 `acceptance` 的隔离库，运行进程级双实例验收；脚本会启动两个 Uvicorn 实例、每实例 2 workers，显式清空企微配置，并在结束时清理合成数据：

   ```bash
   uv run python scripts/multiworker_smoke.py \
     --database-url "$ACCEPTANCE_DATABASE_URL" \
     --query-port 18080 --sse-port 18081 --workers 2
   ```

4. **保持生产写入关闭**。进程级脚本通过后，用专用验收账号做 HTTP 查询、查询详情、提醒、纠正队列及 SSE 冒烟。真实企微收发另用明确指定的测试账号与测试凭据执行，不能复用生产客服账号。检查两 worker 日志无 5xx/数据库错误、游标持续前进、同一 verdict 的 alert 恰一行。双 worker 自动化覆盖运行 `./scripts/test.sh`。
5. 冒烟通过后再恢复生产入口。压力测试使用专用账号与验收副本：两个验收服务指向同一验收库，一个 `MODE=mock, MOCK_JUDGE_DELAY_SECONDS=0`，另一个 `MODE=mock, MOCK_JUDGE_DELAY_SECONDS=1`。正常服务承载读基线和混合流量，延迟服务产生 10 个在途判定；这样混合流量不会被人工注入的一秒延迟改变。运行：

   ```bash
   # 分别在两个终端启动。即使仓库 .env 含真实企微配置，此验收服务也不会启动 poller。
   WECOM_CORPID= WECOM_AGENT_ID= WECOM_APP_SECRET= WECOM_KF_SECRET= WECOM_TOKEN= WECOM_AES_KEY= PUBLIC_BASE_URL= \
     DATABASE_URL="$ACCEPTANCE_DATABASE_URL" MODE=mock MOCK_JUDGE_DELAY_SECONDS=0 \
     uv run uvicorn homeshield.server:app --host 127.0.0.1 --port 8000 --workers 2
   WECOM_CORPID= WECOM_AGENT_ID= WECOM_APP_SECRET= WECOM_KF_SECRET= WECOM_TOKEN= WECOM_AES_KEY= PUBLIC_BASE_URL= \
     DATABASE_URL="$ACCEPTANCE_DATABASE_URL" MODE=mock MOCK_JUDGE_DELAY_SECONDS=1 \
     uv run uvicorn homeshield.server:app --host 127.0.0.1 --port 8001 --workers 2

   WECOM_CORPID= WECOM_AGENT_ID= WECOM_APP_SECRET= WECOM_KF_SECRET= WECOM_TOKEN= WECOM_AES_KEY= PUBLIC_BASE_URL= \
     DATABASE_URL="$ACCEPTANCE_DATABASE_URL" MODE=mock uv run python scripts/pressure.py \
     --base-url http://127.0.0.1:8000 \
     --slow-base-url http://127.0.0.1:8001 --token "$PRESSURE_USER_TOKEN" \
     > "$ACCEPTANCE_RECORD"
   ```

   脚本用客户端计时覆盖关系列表、我的查询/详情、提醒列表/详情、纠正队列；另测连接池点查/单行插入、10 个慢判定期间的 50 个并发读请求和正常服务上的 20 QPS × 60 秒混合流量。只对专用压测用户运行；压测会创建查询记录。检查 JSON `passed=true`，将报告、机器规格、版本、执行时间与企微测试号结果归档，并关闭验收副本。

## 快速回滚

若问题发生在生产入口重新开放前：停止新服务，将旧版本代码、旧启动配置和切换前 SQLite 副本恢复到原路径，然后启动旧服务。Postgres 库和切换前原 SQLite 文件都保留，以便调查；不要在回滚时覆盖或删除它们。用旧版查询 API 做冒烟并记录恢复耗时。

切换通过并恢复生产写入后，Postgres 成为唯一写入源。禁止直接切回切换前的 SQLite 快照（那会丢失切换后的写入）；此时先保留 Postgres 作为数据源，再按故障恢复决策处理。当前项目不提供 Postgres→SQLite 反向迁移。

## 备份与恢复演练

备份使用 `scripts/backup_postgres.sh`，默认归档在 `./backups/`、权限为当前用户可读写，保留 14 天；可通过 `BACKUP_DIR` 与 `BACKUP_RETENTION_DAYS` 调整。恢复必须使用隔离的空库：

```bash
RESTORE_DATABASE_URL='postgresql://.../homeshield_restore' \
  scripts/restore_postgres.sh ./backups/homeshield-YYYYmmddTHHMMSSZ.dump
```

恢复脚本拒绝已有业务对象的目标库，并以单事务 `pg_restore`。演练后比较源库和恢复库各业务表行数、抽样 `msg_id`、alert 与 correction_case 数量；记录 PostgreSQL 版本、归档校验和及恢复耗时。不要把真实 `.env`、访问 token 或备份归档提交到仓库。

# 部署

家人零维护,进程必须自愈:崩溃或宿主重启后自动拉起。按宿主系统二选一。

服务运行依赖 Postgres 16。运行数据使用 `DATABASE_URL`；`DB_PATH` 只供离线 `kbbuild` 使用（默认 `data/kb_build.db`，命令行 `--db` 可覆盖）。旧群模型数据库不迁移；符合当前关系模型 SQLite schema 的数据切换按 [`postgres-cutover.md`](postgres-cutover.md) 执行。

## Postgres

1. 复制 `.env.example` 为 `.env`，生成强随机 `POSTGRES_PASSWORD`，并将同一口令填入 `DATABASE_URL`；`.env` 不进入版本库。
2. `docker compose up -d --wait postgres`。默认宿主端口为 `127.0.0.1:5432`；端口冲突时设置 `POSTGRES_PORT`。
3. `uv sync && uv run homeshield-cli init-db`。
4. systemd / launchd 服务读取相同的 `.env`，启动 `uv run uvicorn homeshield.server:app --workers 2`。
5. 定期运行 `scripts/backup_postgres.sh`，并按 runbook 定期在空库上验证恢复。

## Linux(systemd)

1. 代码放在 `/opt/homeshield`,完成 `uv sync` 与 `.env` 配置
2. `cp deploy/systemd/homeshield.service /etc/systemd/system/`
3. `systemctl daemon-reload && systemctl enable --now homeshield`

## macOS(launchd)

1. 按需修改 `deploy/launchd/com.homeshield.plist` 内的代码目录与 uv 路径
2. `cp deploy/launchd/com.homeshield.plist ~/Library/LaunchAgents/`
3. `launchctl load ~/Library/LaunchAgents/com.homeshield.plist`

## 对外可达与家人入口

- 公网 VPS 直接绑定 `0.0.0.0`;家用宽带用内网穿透(frp / Tailscale Funnel 等)
- `.env` 的 `PUBLIC_BASE_URL` 填对外地址;配置企微回调与密钥后,消息经回调通知即时拉取判定,用户首次发来有效消息时会收到个人控制台链接,高危提醒可点击打开对应详情。
- 用户也可回复「我的联防」重新取得个人链接。运维需要代查时使用:`uv run homeshield-cli link --user-id <id> --base-url https://对外地址`。
- 个人链接包含访问凭证,请私下发给对应用户,不要发到群聊或公开位置。

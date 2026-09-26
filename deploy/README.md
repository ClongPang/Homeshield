# 部署

家人零维护,进程必须自愈:崩溃或宿主重启后自动拉起。按宿主系统二选一。

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
- `.env` 的 `PUBLIC_BASE_URL` 填对外地址,高危告警的模板消息可点击直达控制台
- 家人入口:`uv run homeshield-cli link --member-id <id> --base-url https://对外地址`,把打印的链接发到家庭群

# spec-delta：deployment（Docker 基座、测试库与备份）

## ADDED Requirements

### Requirement: 本机 Docker Postgres 基座
本机为**共用服务**的运行宿主，资源让位于本服务：其余项目的容器停用。开发与部署基座 SHALL 以 docker compose 提供 postgres:16：命名卷持久化、healthcheck、时区 UTC；宿主端口默认 5432，映射 SHALL 可经环境变量覆盖。连接串 SHALL 全部经 DATABASE_URL 注入，SHALL NOT 硬编码于代码。

#### Scenario: 一键起库
GIVEN 本机 Docker 已运行且其余项目容器已停用
WHEN docker compose up -d
THEN postgres 容器健康检查通过
AND 服务能以默认 DATABASE_URL（localhost:5432）连接并完成幂等建库

#### Scenario: 端口默认与覆盖
GIVEN 宿主机 5432 已清空
WHEN 本项目容器启动
THEN 默认占用 5432 对外提供服务
AND 若 5432 将来被占用，仅需改环境变量映射到其他端口，无需改代码

#### Scenario: 数据持久化
GIVEN 库中已有数据
WHEN docker compose down 后再次 up -d
THEN 数据经命名卷完整保留

### Requirement: 测试数据库与隔离
自动化测试 SHALL 运行于真实 Postgres：session 级建 schema，测试间以 TRUNCATE 全表 RESTART IDENTITY CASCADE 隔离。全部既有测试 SHALL 全绿，作为 M1（同步池化）与 M2（全异步）两个里程碑的验收门。

#### Scenario: 测试间隔离
GIVEN 上一测试遗留了 user/query/verdict 数据
WHEN 下一测试开始
THEN 全表已清空且序列已重置
AND 测试结果与执行顺序无关

#### Scenario: 里程碑门禁
GIVEN M1 或 M2 任一里程碑完成
WHEN 运行全部自动化测试
THEN 全部通过，无跳过、无为绕过 PG 而保留的 SQLite 测试路径

### Requirement: 备份与恢复
Postgres 数据 SHALL 具备例行备份：pg_dump 备份脚本与保留策略；备份产物 SHALL 经 pg_restore 在空库上验证可恢复。

#### Scenario: 备份可恢复
GIVEN 运行中的 Postgres 库含业务数据
WHEN 执行备份脚本
THEN 生成 pg_dump 归档
AND 在空库上 pg_restore 后各表行数与源库一致

### Requirement: 文档与运行说明同步
《技术选型》存储章节与 README 运行说明 SHALL 随切换更新：Docker 起库步骤、端口 5432、DATABASE_URL 配置、备份命令；《架构技术》SHALL 记录本次变更 changelog。

#### Scenario: 新环境按文档可复现
GIVEN 一台仅装 Docker 与 uv 的机器
WHEN 按 README 步骤操作（compose 起库、配置 DATABASE_URL、建库、启动服务）
THEN 服务可用且与既有部署行为一致

### Requirement: 生产安全基线
Postgres 凭据与端口 SHALL 遵循最小暴露：compose 的 POSTGRES_PASSWORD SHALL 仅经 `.env` 环境变量注入（compose 文件不含默认口令）；5432 SHALL 默认仅绑定 127.0.0.1（或内网地址）；DATABASE_URL 与全部凭据 SHALL NOT 提交入版本库（.gitignore 覆盖 `.env`）；生产部署后 SHALL 修改默认弱口令。

#### Scenario: 无口令即拒绝启动
GIVEN compose 文件中口令仅以环境变量引用
WHEN 未注入 POSTGRES_PASSWORD 即执行 docker compose up
THEN 容器拒绝启动并给出明确报错
AND 不存在以默认弱口令对外运行的路径

#### Scenario: 端口不对外暴露
GIVEN 默认配置启动
WHEN 检查端口绑定
THEN 5432 仅绑定 127.0.0.1
AND 同机之外的机器无法直连数据库端口

### Requirement: 切换 runbook 与回滚预案
生产切换 SHALL 按固化并预先演练过的 runbook 执行：停止写入 → 备份 SQLite 文件 → 执行迁移脚本 → 关键数据核对（各表行数一致、抽样 msg_id 命中、告警与纠正计数一致）→ 启动 PG 版服务 → 观察错误率与回复可达。切换前版本的启动方式与 SQLite 数据文件 SHALL 保留不删；切换失败 SHALL 以重启旧版本的方式分钟级回滚，SHALL NOT 依赖反向迁移。

#### Scenario: 回滚可行性
GIVEN 切换后服务出现异常
WHEN 执行回滚步骤（停新服务、以原 SQLite 文件重启旧版本）
THEN 旧服务以切换前数据恢复运行
AND 回滚在分钟级完成且无数据丢失

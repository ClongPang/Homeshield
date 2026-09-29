# spec-delta：storage（存储引擎与数据层）

## ADDED Requirements

### Requirement: 生产链路存储引擎为 Postgres
系统 SHALL 以 Postgres 为唯一生产存储引擎，经 psycopg3 连接池访问；生产代码（`src/homeshield` 除 `kbbuild` 外）SHALL NOT import sqlite3。`kbbuild` 为离线构建工具，SHALL 豁免保留 SQLite。

#### Scenario: 依赖装配经连接池
GIVEN 环境变量 DATABASE_URL 指向可用的 Postgres 实例
WHEN build_deps 装配依赖
THEN Repos 持有连接池而非单条连接
AND 所有 repo 方法经池借还连接
AND 生产代码中不存在 sqlite3 import

#### Scenario: kbbuild 豁免
GIVEN 仓库完成本提案全部任务
WHEN 审查 src/homeshield/kbbuild 的依赖
THEN kbbuild 仍可独立以 SQLite 完成知识库构建
AND 其产物（cases.json）不依赖任何数据库运行时

### Requirement: Schema 终态一次到位
DDL SHALL 以 Postgres 终态建库：主键为 BIGINT GENERATED ALWAYS AS IDENTITY；时间戳列为 timestamptz 且会话时区钉 UTC；`guard_relation.mute` 为 boolean；`features`/`cited_ids`/`context_snapshot` 为 jsonb；`query.degraded_reply` 为可空文本，用于留存无 verdict 的降级回复；现有 CHECK 约束与部分唯一索引（uq_query_msg_id、uq_open_incident、uq_active_relation）SHALL 全量平移。SHALL NOT 使用 ENUM 类型（保留 CHECK 约束表达取值域）。建库 SHALL 幂等并记录 schema_version。

#### Scenario: 幂等建库
GIVEN 已按当前 DDL 完成建库的 Postgres
WHEN 再次执行建库流程
THEN 无错误且 schema 不变
AND schema_version 表记录当前版本号

#### Scenario: 部分唯一索引语义平移
GIVEN 两条携带相同 msg_id 的并发插入
WHEN 第二条提交
THEN 数据库以 uq_query_msg_id 拒绝第二条
AND msg_id 为 NULL 的行不受该索引约束

### Requirement: 事务边界为每方法一事务
每个 repo 方法 SHALL 在单个事务内完成其全部写语句，方法内多语句原子段 SHALL 显式以 `conn.transaction()` 包裹。系统 SHALL NOT 引入请求级事务：管线失败时已入库的 query SHALL 保留，降级回复 SHALL 照常入库。

#### Scenario: ingest 原子性
GIVEN 一次新消息进入 ingest
WHEN query 行与 query_relation 关系快照写入
THEN 两条写语句同事务提交
AND 不存在有 query 无关系快照的中间可见状态

#### Scenario: 管线失败降级语义保持
GIVEN 消息已 ingest 并提交
WHEN 判定管线抛出意外异常
THEN 该 query 行保留在库
AND LOOK_FAILED 降级判定照常入库
AND 通道必有回复的语义与改造前完全一致

### Requirement: 消息幂等在 Postgres 下等价成立
重复消息识别 SHALL 三层成立：先查后插（msg_id 索引点查）→ msg_id 部分唯一索引 → 唯一冲突映射为 DuplicateMessage。冲突映射 SHALL 依据 SQLSTATE 23505 与 `diag.constraint_name == 'uq_query_msg_id'`，SHALL NOT 依赖错误消息文本匹配；其余约束违例 SHALL 原样抛出，不冒充重复。

#### Scenario: 并发双插同一 msg_id
GIVEN 两个并发执行流以同一 msg_id 同时调用 ingest
WHEN 两者提交
THEN query 表恰有一行
AND 恰一方正常入库，另一方得到重复结果且 query_id 指向同一行

#### Scenario: 企微重启重放
GIVEN 服务重启后企微游标清空触发全量重放
WHEN 已处理消息再次到达 ingest
THEN 返回 duplicate=true 且 query_id 为原行
AND 不产生第二条回复

### Requirement: check-then-act 序列以 PG 原生机制显式接棒
原先依赖进程级 WRITE_LOCK 事实原子的序列 SHALL 逐条以 PG 原生机制保护：`UserRepo.get_or_create` 用 ON CONFLICT(openid) DO NOTHING 加重查；`InviteRepo.claim` 保留条件 UPDATE（WHERE used_at IS NULL）加 rowcount 校验并事务包裹；`CorrectionRepo.submit` 建案用 ON CONFLICT(verdict_id) DO NOTHING 加重查；`IncidentRepo.attach_query_to_incident` 在事务内对打开中的 incident 行 SELECT FOR UPDATE 并以 uq_open_incident 兜底；`WecomMemberRepo.link` 撞 corp_userid 唯一约束时捕获 23505 重试一次。

#### Scenario: 并发核销同一邀请码
GIVEN 两个并发请求 claim 同一张有效邀请码
WHEN 两者提交
THEN 恰一方成功建立关系
AND 另一方得到 used 语义结果
AND invite_code.used_at 仅被设置一次

#### Scenario: 并发为首条判定建纠正案
GIVEN 同一判定的两个并发纠正提交且此前无案
WHEN 两者提交
THEN correction_case 恰建一案
AND 两方均拿到该案摘要，无一返回 500

#### Scenario: 并发开案挂靠
GIVEN 同一用户的两个并发查询同时触发 incident 挂靠且当前无打开案件
WHEN 两者提交
THEN 恰新建一个 incident（uq_open_incident 兜底或 FOR UPDATE 串行化）
AND 两条 query 均挂到该 incident，无孤儿 query

### Requirement: 数据迁移一次性完成
SQLite 到 Postgres 的数据迁移 SHALL 由一次性脚本完成：全表按原 id 灌入，时间戳经 to_timestamp 转换，mute 以 boolean 语义转换，JSON 文本转 jsonb；迁移后 SHALL 逐表 setval 将序列对齐至 max(id)+1。

#### Scenario: 迁移后行为等价
GIVEN 一个含 user/query/verdict/alert/correction 数据的 SQLite 生产库
WHEN 执行迁移脚本
THEN Postgres 各表行数与 SQLite 一致且 id 不变
AND 按任一 msg_id 查询命中原行
AND 迁移后的新插入不与迁移 id 冲突

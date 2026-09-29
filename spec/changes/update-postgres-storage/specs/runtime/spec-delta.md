# spec-delta：runtime（并发模型与多 worker）

## ADDED Requirements

### Requirement: 全异步并发模型
repo 层、服务层（verification/relations/feedback/intake/pipeline）、API 路由与企微通道 SHALL 全链路 async；事件循环 SHALL NOT 被同步 DB 调用或同步网络调用阻塞；进程级 WRITE_LOCK、asyncio.to_thread 包装与线程池借道 SHALL 全部退役。

#### Scenario: 事件循环零 DB 阻塞
GIVEN 服务以全 async 模式运行
WHEN 并发处理多个查询请求
THEN 全部 DB 等待经 await 在事件循环上完成
AND 任一请求的 LLM 等待期间不占用线程也不阻塞其他请求

#### Scenario: 旧并发设施清零
GIVEN M2 完成
WHEN 在生产代码（kbbuild 除外）中 grep WRITE_LOCK、_serialize_repo_access、asyncio.to_thread
THEN 零命中

### Requirement: 多 worker 部署解锁
服务 SHALL 支持多 worker（uvicorn --workers N）指向同一数据库；flock 单进程护栏 SHALL 移除；此前依赖单进程假设的内存状态 SHALL 全部落库或以 PG 机制协调。双 worker 场景的验收 SHALL 分两级：自动化集成测试（同一 PG 上启动两个应用实例断言行为）覆盖选主、游标恢复与 NOTIFY 幂等；部署后人工以真机双 uvicorn 操作脚本复核并留记录。

#### Scenario: 双 worker 并存
GIVEN 两个 worker 指向同一 DATABASE_URL
WHEN 同时启动并同时服务请求
THEN 两 worker 正常处理查询且数据一致
AND 无护栏拒绝启动

### Requirement: 企微轮询单主与游标落库
企微拉取游标 SHALL 落库（kf_cursor 表，per-kfid UPSERT）；实现 SHALL NOT 假设客服账号数量——保持按 `list_kf_accounts` 迭代与 per-kfid 游标的通用结构（当前部署仅一个客服账号，未来可增）。多 worker 下 poller SHALL 经 pg_try_advisory_lock 选主，同一时刻至多一个 worker 执行轮询；重启重放安全性 SHALL 继续由 msg_id 幂等去重保证。

#### Scenario: 单客服账号部署事实
GIVEN 当前部署仅有一个企微客服账号、全部家庭的用户消息经它收发
WHEN 服务运行轮询与回调链路
THEN 消息经该唯一账号正常收发
AND 代码路径按 list_kf_accounts 迭代、游标按 kfid 一行，无硬编码账号数量

#### Scenario: 单主轮询与接管
GIVEN 两个 worker 同时启动
WHEN poller 任务启动
THEN 恰一个 worker 持有 advisory lock 并执行轮询
AND 持主 worker 停止后另一 worker 可取得锁接管轮询

#### Scenario: 游标恢复
GIVEN 持主 worker 重启、另一 worker 接管
WHEN 接管方开始轮询
THEN 从 kf_cursor 表恢复各 kfid 游标
AND 游标之前的消息不重复拉取，重放部分由幂等去重兜底

### Requirement: SSE 跨 worker 经 LISTEN/NOTIFY
verdict 落库后 SHALL 发出 PG NOTIFY；每个 worker 的桥任务 SHALL LISTEN 并以与进程内 EventBus 相同的渲染路径分发告警事件；双 worker 同时消费同一事件 SHALL 无害（alert 落表以 ON CONFLICT 幂等）。NOTIFY SHALL 仅作为广播提示，SHALL NOT 承担任务分配语义。

#### Scenario: 跨 worker 告警触达
GIVEN 一个 SSE 客户端连接在 worker B
WHEN 判定管线在 worker A 完成落库
THEN worker B 的桥任务收到 NOTIFY
AND 该 SSE 客户端收到告警事件
AND alert 表不产生重复行

#### Scenario: 双 worker 并发消费
GIVEN 两个 worker 的桥任务几乎同时收到同一 verdict 的 NOTIFY
WHEN 两者执行告警渲染与落表
THEN alert 表每个 (verdict_id, relation_id) 恰一行
AND 双方均正常返回，无 500

### Requirement: CLI 与 eval 复用统一数据层
homeshield-cli 与 eval SHALL 经 asyncio.run 使用同一 async repo 层与同一 DATABASE_URL；SHALL NOT 保留独立的数据访问路径。

#### Scenario: CLI 管理命令
GIVEN Postgres 库已按终态 DDL 建成
WHEN 运行 homeshield-cli 的管理命令
THEN 行为与改造前一致
AND 数据访问全部复用 repo 层，无独立 SQL

### Requirement: 性能验收基线
系统 SHALL 提供可重复的压测脚本：LLM 以 mock 替代（压测对象为存储与并发层）；脚本 SHALL 枚举控制台实际依赖的读端点（关系列表、我的查询/详情、告警列表/详情、纠正队列）并逐一压测，P99 以客户端计时为准；压测 SHALL 在部署机上执行，基线数字随部署机校准，开发机结果仅作参考。验收时 SHALL 达成以下基线：走池的单条数据库操作（点查/单行插入）p99 < 5ms；50 并发读请求 P99 < 500ms 且零 5xx；存在进行中判定（慢 LLM 模拟）时读接口 P99 < 500ms；20 QPS 读写混合持续 60 秒零 5xx。基线为回归下限而非性能承诺，SHALL 可按首次实测结果修订并记录（含机器规格）。

#### Scenario: 单条数据库操作开销
GIVEN 服务经连接池连接本机 Docker Postgres
WHEN 压测脚本执行 1000 次点查与单行插入
THEN p99 单操作耗时小于 5ms
AND 全程无连接池耗尽或超时错误

#### Scenario: 读接口并发不排队
GIVEN 控制台、提醒列表、纠正队列等读接口
WHEN 50 个并发读请求同时发起
THEN 全部请求 P99 响应小于 500ms
AND 零 5xx

#### Scenario: 读写互不阻塞
GIVEN 10 个判定请求正在管线中处理（LLM 以 mock 模拟秒级耗时）
WHEN 同时发起读请求
THEN 读接口 P99 响应小于 500ms
AND 全部判定请求正常完成落库

#### Scenario: 持续混合负载
GIVEN mock 判定模式
WHEN 以 20 QPS 读写混合持续 60 秒
THEN 零 5xx
AND P99 响应小于 1 秒

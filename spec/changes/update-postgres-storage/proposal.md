# 提案：Postgres 完全化改造（全异步驱动 + SQLite 生产清零 + PG 原生能力一次到位 + 多 worker 解锁）

## Why

现状存储层是"单条 SQLite 连接 + 进程级 `WRITE_LOCK` + flock 单进程护栏"（`repo.py:12`、`server.py:29-55`）。这套纪律把三样东西钉死：

- **读并发**：所有 repo 方法（含纯读）在进程内串行，console/告警/队列等读接口与写互相排队；
- **写并发**：受 SQLite 单写者模型限制，无法利用多核与行级并发；
- **部署形态**：flock 强制单进程，企微游标（内存 dict）与 SSE EventBus（进程内）也依赖单进程假设，水平扩展被结构性禁止。

判定负载由 LLM 主导、DB 段非单请求瓶颈，但系统的并发上限、读吞吐与部署形态受制于存储架构。**决策：完全 Postgres 化**——不做"最小 diff"的中间态，而是把所有将来需要动 schema 或动并发模型的债一次还清。Postgres 运行于本机 Docker。

**产品定位（本提案的前提，实施与容量判断均以此为锚）**：本服务是**共用服务**——多家庭共享同一服务入口，而非单一家庭的私有工具；当前资源有限，微信侧仅一个企微客服账号。因此并发模型不能按"单家庭个位数并发"校准，存储与部署改造以共用服务的并发上限为目标；同时单客服账号意味着企微通道吞吐存在平台侧上限，本次改造解除的是自有侧（存储/进程模型）的瓶颈。机器资源让位于本服务：其余项目的 Docker 容器停用，5432 端口让给本项目 Postgres。

**背景**：
- 幂等三层防护本身近零成本（先查后插点查 + 部分唯一索引 + 异常兜底），性能问题出在全局锁串行化一切，而非幂等设计；
- LLM 客户端已是 `AsyncOpenAI`（`llm.py:107`），异步链路真实存在，repo 层是唯一的同步残留；
- 宿主机 5432 原被其他项目容器占用，已按部署决策停用让位，本项目 Postgres 默认使用 **5432**（映射可经环境变量覆盖）。

**当前状态**：sync repo（59 方法）+ 共享单连接 + `BEGIN IMMEDIATE` + `IntegrityError` 文本匹配；测试以 `DB_PATH=:memory:`/tmp 文件库运行。

**期望状态**：async repo 全链路 + psycopg3 `AsyncConnectionPool` + PG 原生约束与行锁接棒并发纪律 + 游标落库/advisory lock/NOTIFY 桥 + `uvicorn --workers N` 可用。

## What Changes

四个维度，M1→M2→M3 三个里程碑（每步测试全绿、可独立停下）：

- **M1 同步池化**：PG 终态 DDL（schema 只动这一次）+ psycopg3 sync 连接池 + 方言改造（`?`→`%s`、`RETURNING id`、`ON CONFLICT`、23505 按约束名捕获）+ 全局锁逐条接棒为 PG 原生机制 + conftest 换 PG + 全部测试绿。
- **M2 全异步**：repo/intake/服务层/API/企微通道全链路 async；`WRITE_LOCK`、flock、线程池借道全部退役；pytest-asyncio 迁移全部测试；CLI 经 `asyncio.run`。
- **M3 多 worker**：企微游标落库（`kf_cursor`）+ `pg_try_advisory_lock` 选主 + LISTEN/NOTIFY SSE 桥 + 摘 flock + 量化性能验收基线（可重复压测脚本，四项指标）与数据切换。

### 已定设计决策（实施阶段不再讨论）

| 决策点 | 选择 | 理由 |
|---|---|---|
| 驱动 | psycopg3（async 模式），不选 asyncpg | `%s` 占位符与现有 `?` 机械对应；`dict_row` 使 `row["col"]`/`dict(row)` 零改动；sync 版可给 CLI/脚本；SQL 与 M1 完全复用 |
| 事务边界 | 每方法一事务，**拒绝请求级事务** | 保住"管线失败时 query 已入库、降级回复照常入库"的现有语义；请求级事务会把幂等与降级语义搅乱 |
| Schema | `IDENTITY` / `timestamptz`（会话 UTC）/ `boolean` / `jsonb` 一次到位；**不用 ENUM**（保留 CHECK） | 避免"以后每个 PG 特性再来一遍 schema 变更"；ENUM 演进性差 |
| 跨进程事件 | LISTEN/NOTIFY 只是**广播提示不是任务分配**，双消费靠 `ON CONFLICT` 幂等无害 | notifier 触发路径不重设计 |
| 企微轮询 | 游标落库 + advisory lock 选主 | 正确性本有 msg_id 幂等兜底，治理的是 API 配额浪费 |
| kbbuild | **豁免**，保留 SQLite | 离线构建工具，产物是内存加载的 cases.json；迁 PG 只会让构建流程依赖活的 PG 实例而零收益 |

### 锁纪律接棒映射（全局锁退役后的显式保护）

| 现状（靠全局锁事实原子） | PG 接棒机制 |
|---|---|
| `ingest` 先查后插 | msg_id 部分唯一索引 + 23505（`constraint_name=uq_query_msg_id`）→ `DuplicateMessage`，不再做错误文本匹配 |
| `UserRepo.get_or_create` | `ON CONFLICT (openid) DO NOTHING` + 重查 |
| `invite.claim`（`BEGIN IMMEDIATE`） | 条件 UPDATE（`WHERE used_at IS NULL`）+ rowcount 校验本已原子，事务包裹即可 |
| `correction.submit` 无案建案 | `ON CONFLICT (verdict_id) DO NOTHING` + 重查 |
| `incident.attach` | 事务内 `SELECT … FOR UPDATE`（打开中的 incident 行）+ `uq_open_incident` 部分唯一索引兜底 |
| `wecom_member.link` DELETE+INSERT | 撞 `corp_userid` 唯一约束捕获 23505 重试一次 |
| `record_alerts_for_verdict` | `ON CONFLICT (verdict_id, relation_id) DO NOTHING` |

## 前置条件与交接约定

实施启动前，以下事项必须就位或明确归属；测试与开发团队以此为协作契约：

| # | 事项 | 说明 |
|---|---|---|
| 1 | 企微联调环境与凭据 | 测试用企微凭据由服务提供方注入 `.env`（不入仓）：`WECOM_CORPID`、`WECOM_AGENT_ID`、`WECOM_APP_SECRET`、`WECOM_KF_SECRET`、`WECOM_TOKEN`、`WECOM_AES_KEY`、`PUBLIC_BASE_URL`（回调须公网可达，本地联调用测试号+内网穿透）。缺任一项，任务 11/12 的企微相关场景只能做到 mock 级验证，真机验证顺延 |
| 2 | 里程碑分支纪律 | M1/M2/M3 各自测试全绿后才合入 main。**M1 合入起，main 分支运行即要求 Docker PG（SQLite 支持已移除）；生产部署切换在 M2 完成后进行，M1~M2 之间禁止照旧方式部署** |
| 3 | M2 排期特性 | 27 个测试文件的 async 化高度耦合（conftest 先行、逐文件迁移、迁一个跑一个），是单人连续完成的工作，不拆分多人并行 |
| 4 | 测试验收清单来源 | 三个 spec delta 共 35 个 EARS 场景即验收用例清单：每个场景至少映射一个自动化测试用例；涉及真机/企微实号的场景以操作脚本人工执行并留记录 |
| 5 | 压测环境 | 性能基线在**部署机**上测量（数字随部署机校准，记录机器规格）；开发机结果仅作参考 |

## Impact

### 受影响的规范
- 新建 `spec/specs/storage/spec.md`、`spec/specs/runtime/spec.md`、`spec/specs/deployment/spec.md` 三个能力基线（本项目首个 openspec 基线，归档时生成）。

### 受影响的代码
- `src/homeshield/core/db.py`、`repo.py`、`deps.py` — DDL 终态、连接池、59 方法方言与 async 改造；
- `src/homeshield/core/{intake,verification,relations,feedback,notifier}.py` — async 化与事务边界；
- `src/homeshield/api/{relations,wecom}.py`、`server.py` — 路由 async 化、flock 退役、NOTIFY 桥、游标落库；
- `src/homeshield/cli.py`、`eval/` — `asyncio.run` 包装；
- `tests/`（27 文件）— conftest 换 PG + pytest-asyncio；
- 新增 `docker-compose.yml`、迁移脚本、备份脚本。

### 用户影响
- 零变化：API 形状、回复文案、幂等语义、判定契约（features/judge/reply 三段式与会话模型）全部不动。

### API 变更
- 无破坏性变更，无新增端点。

### 需要迁移
- [x] 数据库迁移（一次性脚本：SQLite→PG 按原 id 灌入、`to_timestamp` 转换、逐表 `setval` 对齐序列）
- [ ] API 版本提升（不需要）
- [ ] 用户沟通（不需要，行为零变化）
- [x] 文档更新（《架构技术》changelog、《技术选型》存储一节）

## 时间线评估

大（单人，测试安全网 203 条）：M1 约两个工作日；M2 最重（测试 async 化面广）约两至三个工作日；M3 约一个工作日。每个里程碑独立可停。

## 风险

- **M2 测试大迁移枯燥且易漏** → 每迁完一个测试文件即跑，全绿后再动下一个；里程碑门禁是"全部测试绿"，不允许带病推进。
- **锁纪律接棒遗漏造成并发竞态（测试单连接跑不出）** → M1 强制为每条接棒补双线程/双协程并发回归测试，这是本次改造新增的测试类别。
- **timestamptz 改造波及窗口算术（incident/supply）** → 连接参数钉死会话时区 UTC；测试中 `UPDATE created_at=10000` 类写法统一改 `to_timestamp(10000)`。
- **生产切换停机** → 单机小数据量，停机窗口分钟级；迁移脚本先对 dev 库演练两次。
- **5432 端口依赖其它容器保持停用** → 若其他项目将来恢复运行，仅需改 `DATABASE_URL` 与 compose 映射端口，无代码变更；连接串全部经 `DATABASE_URL` 注入。

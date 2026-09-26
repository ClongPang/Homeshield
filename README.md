# 家中盾(Homeshield)—— 家庭反诈联防层

> 规格:《家中盾_产品定位.md》(做什么/为什么) + 《家中盾_架构技术.md》(怎么做)。
> 本 README 只讲**工程骨架**:怎么跑、怎么设计的、扩展点在哪。

## 快速开始(Python 3.12,uv 管理)

```bash
uv sync                       # 创建虚拟环境、锁依赖(uv.lock)
uv run pytest                 # 全部测试(MODE=mock,无需 API key,FR-10)
uv run python cli.py init-db  # 建库
uv run uvicorn server:app --reload          # MODE=mock 下即可完整演示
uv run python -m eval.run_eval              # 生成 eval/report.md(FR-9)
```

家人入家走公众号(见下文「家人入口」);本地无微信快速演示可沿用 CLI:

```bash
uv run python cli.py add-family --name 我的家庭
uv run python cli.py add-member --family-id 1 --name 妈妈 --role elder
uv run python cli.py add-member --family-id 1 --name 儿子 --role adult
uv run python cli.py link --member-id 2 --base-url http://localhost:8000
```

配置:复制 `.env.example` 为 `.env`,`MODE=llm` 时填 OpenAI 兼容接口与微信参数。

## 目录

```
core/            领域层(纯逻辑,依赖规则见 tests/test_architecture.py)
  models.py config.py errors.py events.py
  intake.py features.py retrieval.py judge.py reply.py notifier.py feedback.py binding.py pipeline.py
  db.py repo.py llm.py deps.py          ← 适配器/组合根
  knowledge/  taxonomy.py cases.json    ← 骗术分类学(12类)+ 案例种子(D1 扩到每类≥3)
  channels/   wechat.py                 ← 微信防腐层
eval/            评测:dataset/metrics/ablation/report/run_eval
web/             长辈聊天页 index.html / 子女控制台 console.html
tests/           契约测试、状态机测试、API 冒烟、架构守护
data/samples/    评测种子样例(D2 扩到 80-100 条,记 provenance)
server.py cli.py pyproject.toml .env.example
```

## 设计决策(模式 → 落点 → 解决什么)

| 模式 | 落点 | 解决什么 |
|---|---|---|
| 六边形架构(端口/适配器) | 领域模块只依赖 Protocol;`tests/test_architecture.py` 用 AST 守护依赖方向 | 防止业务逻辑长进框架里;LLM/SQLite/微信可整体替换 |
| 策略模式 | `Judge`(MockJudge/LLMJudge)、`LLMPort`(MockLLM/OpenAICompatLLM)、ReplyGenerator | §1.2 双模式:MODE 一键切换,CI 不依赖外部 API |
| 管道 + 配置即数据 | `Pipeline` + `PipelineConfig`(A/B/C 开关) | 消融矩阵与产品共用同一条代码路径(§6.3),评测不需要第二套实现 |
| 观察者/事件总线 | `EventBus`(sync/async 显式分派,含可调用对象)+ `VerdictCompleted` → `AlertRouter` | 判定与送达解耦;慢 I/O 由 handler 自行后台化,告警策略(dangerous 才推,§3.1)可整体替换 |
| 服务层(通道去重) | `core/verification.py` `VerificationService` | web/微信两条入口共用"幂等 ingest→管线"主链路,通道只做协议翻译;新增入口(如 iLink)不再复制主链路 |
| 仓储模式 | `repo.py`(SQL 只在此文件) | 领域层只见领域对象;SQLite 可换而不动业务 |
| 显式状态机 | `feedback.LEGAL_TRANSITIONS` | 纠正流转(§3.3)非法转移直接拒绝,防投毒规则可测 |
| 防腐层 | `channels/wechat.py` | 微信签名/XML/客服接口/模板消息不渗入领域 |
| 确定性兜底 | `TemplateReply` + `validate_reply`;`judge_with_validation` 重试→降级 | FR-4/FR-5 的格式正确性不赌 LLM |
| 组合根 | `deps.build_deps`(唯一 new 具体实现的地方) | server/cli/eval 共用装配;测试注入假实现 |
| 日志约定 | `core/logsetup.py` + 各模块 `logger.warning(exc_info=True)` | 降级与外部调用失败不静默:留排查痕迹,不打断主链路 |

## 关键工作区决策(规格未明说,骨架先行选定,欢迎推翻)

1. **schema 三处最小扩展**:`member.openid`(零注册映射)、`member.token`(个人链接凭证)、`query.msg_id`(幂等落库);时间戳统一 INTEGER unix 秒。
2. **多租户:公众号自助开通 + 绑定码入家**:陌生 openid 回复`开通`即建家庭并成为管理员(adult),家人由管理员在控制台生成 8 位一次性绑定码(默认 7 天),回复`绑定 <码>`入家;未绑定消息只引导不判定。护栏 `MAX_FAMILIES`×`MAX_MEMBERS`;旧的"自动绑 elder"单家庭捷径已删除(历史 family 1 成员不受影响)。
3. **`fpr_strict` 指标**:在规格 FPR(dangerous 级)之外补充用户感知口径(suspicious 亦计入),用于工作点选择;报告两者都出。
4. **降级语义**:转写失败/引用校验耗尽 → 不落 verdict、不触发告警,回复走人工兜底文案(FR-2/FR-4 的"需人工判断")。
5. **评测隔离**:`run_eval` 用 `:memory:` 库,不污染主库。
6. **超时用惰性触发,不建定时器**:`CorrectionService.decide()` 前先清算过期 pending——规则在唯一需要它的现场自执行(经评审否决了"server 内每日清扫"的过度设计);真实使用量起来后再评估是否升级为定时任务。
7. **触点模型(双方零技术背景)**:长辈只在微信里;子女是"被通知的人 + 一次点击的反馈者"——高危时模板消息送达并直达详情落地页(`/alert/<id>`,一键反馈),线下核实发生在系统之外;console 是兜底聚合页(告警历史/队列/周报),CLI 定位为开发者工具(模型详见《产品定位》§1.2)。
8. **异常即人话回复**:管线的意外故障在 VerificationService 收口为兜底文案(LOOK_FAILED),长辈与子女永远得到一句能懂的回应,而不是 500 或沉默;微信回执与全部提示文案集中在 core/messages.py。
9. **纠正数据以库为源**:confirmed 纠正留存于库即真实数据源,不维护并行的导出文件;需要回归验证时从库内读取,避免"导出文件过期、与库不一致"。
10. **供应商即配置,任务经路由分发**:任意 `<NAME>_API_KEY / BASE_URL / MODEL` 在 .env 声明一个供应商;判定/补抽/回复走 `CHAT_PROVIDER`,图片转写走 `TRANSCRIBE_PROVIDER`,向量检索走 `EMBED_PROVIDER`——换供应商只改 .env,不加代码;路由键就是贯穿管线的 task 参数。

## 评测纪律(防"虚假环境陷阱")

唯一的质量收敛标准是**真实世界使用**:真实查询日志、confirmed 纠正(库内留存)、真机/真通道延迟。
mock、种子样例、合成数据只有两个合法用途:**CI 管道回归**与**冷启动冒烟**。
对着自产数据调词表/提示词/阈值/检索权重,拟合的是想象,不是现实。

- `eval/report.md` 强制标注数据来源构成(adapted/synthetic/correction)与模式口径;
- mock 口径的数字一律表述为"管道验证",禁止表述为产品质量;
- 词表、提示词、阈值、检索权重的每次变更,须附真实数据复测(库内纠正重放 / 真机日志)才可合入;
- 冷启动集(§6.1 配比)只服务起步;真实使用启动后,数据分布与扩充由回流决定。

> 当前仓库 14 条种子样例跑出的任何数字(如 Recall 0.667、FPR 0)都是**管道验证值**;
> 9.4 秒的 localhost 延迟同样不代表真机/微信通道延迟。两者都不是质量结论。

## 与里程碑的对应

- **D1**:核心管线 + 引用校验 + 双模式 —— 骨架已可跑;待办:知识库每类 ≥3 案例、LLM 补抽提示词调优、FR-2 断言测试扩容。
- **D2**:样例集扩到 80-100 条(记 provenance)、消融/阈值扫描在 LLM 模式跑全量。
- **D3**:微信测试号闭环(回调/客服/模板已具备,待联调)、SSE 前端打磨、纠正队列视图。
- **D4**:README 生产化(认证服务号阶梯)、iLink 评估(《产品定位》§4.3)。

## 部署(deploy/)

家人零维护,进程必须自愈。按宿主二选一:

- Linux:`cp deploy/systemd/homeshield.service /etc/systemd/system/ && systemctl enable --now homeshield`
- macOS:`cp deploy/launchd/com.homeshield.plist ~/Library/LaunchAgents/ && launchctl load ~/Library/LaunchAgents/com.homeshield.plist`

对外可达:VPS 直接绑定 `0.0.0.0`,家用宽带用内网穿透(frp / Tailscale Funnel)。多租户部署必须配 `PUBLIC_BASE_URL`(开通/绑定成功后经客服消息把控制台/网页入口发给家人)。

家人入口(公众号,零注册):

1. 新家庭:家人回复`开通`→ 自动建家庭,ta 成为管理员,收到控制台链接;
2. 添加家人:管理员在控制台「添加家人」生成 8 位邀请码(或对未绑定成员重发),把`绑定 邀请码`发给对方;
3. 家人:关注公众号回复`绑定 邀请码`即入家,直接转发可疑消息即可使用;
4. 未绑定用户发其他消息只收到引导文案,不判定、不落库。

护栏:`MAX_FAMILIES`(全局家庭数,默认 100)、`MAX_MEMBERS`(每家成员数,默认 10)、`BIND_CODE_TTL_DAYS`(邀请码有效期,默认 7 天)。CLI 的 `add-family/add-member/link` 保留为运维调试工具。

## 安全状态

- 已完成:家人 API 以不可枚举 token 鉴权(链接即凭证,接口不回传 token);`/wechat/callback` 平台签名校验;多租户隔离——告警跨家庭 404,纠正提交/裁决跨家庭 400,成员管理仅 adult;绑定码一次性原子认领(防并发双花),过期/重发即失效。
- 待办:前端渲染统一转义(防 XSS);生产微信加密模式与 IP 白名单;按家庭的 LLM 用量配额(当前滥用边界 = MAX_FAMILIES × MAX_MEMBERS × 查询频次)。

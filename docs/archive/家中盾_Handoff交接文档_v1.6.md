# 家中盾(Homeshield)—— 项目交接文档

> 版本:v1.6 | 2026-09-25 | 状态:**规格定稿,代码未动工**
> 读者:接手开发的人。本文是完整规格,从这里开始即可,无需其他上下文。

**目录**:0 快速导读 · 1 产品定义与边界 · 2 功能需求与验收 · 3 系统设计 · 4 判定与协同规则 · 5 评测规格 · 6 工程环境 · 7 里程碑 · 8 决策记录 · 9 风险与开放问题

---

## 0. 快速导读

- **这是什么**:家庭反诈联防层——家人收到可疑消息,转发给"小盾"得到秒级判定与解释;高危时自动同步子女;家人纠正回流为评测样本。
- **不做什么**:电话渠道、硬拦截、被动监控、与官方比判定权威(§1.4)。
- **怎么交付**:D1–D4 四个里程碑(§7);技术栈 Python 3.12(uv 管理)+ FastAPI + SQLite(§6)。
- **按需阅读**:只想跑起来 → §6;关心判定管线与数据 → §3;关心告警/纠正/隐私 → §4;关心"为什么这么设计" → §8。

## 1. 产品定义与边界

### 1.1 定位
家庭反诈联防层——家里任何人收到可疑消息,转发给"小盾"得到秒级判定与解释;高危时自动同步子女;家人纠正回流为评测样本。

### 1.2 角色与入口
| 角色 | 入口 | 看到什么 |
|---|---|---|
| 长辈(elder) | 微信公众号对话(主):长按转发可疑消息,判定回复回到同一对话;聊天页(兜底) | 大字结论("⚠️ 是骗子,别转钱")+ ≤3 条大白话依据 + 1 条行动建议 |
| 子女(adult) | 浏览器控制台(告警另经模板消息推微信) | 实时高危告警流、家庭周报、纠正/确认队列 |
| 系统 | 判定引擎 | 三层管线(§3.1),对两类上游角色透明 |

### 1.3 产品形态与关键取舍
- **自托管网络服务,非 App、非小程序**:安装与上架审核是长辈侧最大流失点,MVP 无原生能力刚需。
- **微信对话为主入口**:转发是长辈的原生动作,复制粘贴是最大摩擦。兜底为手机浏览器聊天页:引导"添加到主屏幕",粘贴文字/上传截图,覆盖短信等微信外内容。
- **零注册**:微信侧以 openid 映射成员,网页侧用个人链接;知情页由关注后首条自动回复承载(隐私规则见 §4.4)。

### 1.4 服务边界(明确不做,原因)
| 不做 | 原因 |
|---|---|
| 电话渠道 | 占诈骗案 63.3%,监听不可行 + 强合规;产品只处理进入系统的文本/链接/截图 |
| 硬拦截、阻断转账 | 误杀代价不可承受;兜底是 96110 与系统级弹窗 |
| 被动监控(读聊天记录/通话) | 只处理**用户主动查证**的内容 |
| 与官方比判定权威性 | "国家反诈 AI"已上线同类能力:判定是入场券,联防才是产品 |

## 2. 功能需求与验收(MVP)

| ID | 需求 | 验收标准(done-when) |
|---|---|---|
| FR-1 | 消息接入:text / url / image(base64)归一化为 Message;微信回调按 MsgId 幂等 | 三类输入均产出结构化对象;非法输入返回 400;同一 MsgId 重试不产生重复 query/回复 |
| FR-2 | 特征抽取:规则引擎抽硬信号(金额、账号、链接、催促词、身份声称、隔离话术、收费名目异常)+ LLM 补抽语义特征;image 先经多模态 LLM 转写为文本再进抽取,失败降级提示"请粘贴文字" | 每条特征含 `{id, type, value, evidence_span, source: rule\|llm}`;对 §5.1 样例集抽取结果可通过断言测试 |
| FR-3 | 骗术知识库:≥12 类分类学,每类 ≥3 条结构化案例(话术特征/识别点/应对建议);向量+关键词混合检索 top-2 | 人工标注类别的 top-3 检索命中率 ≥ 90% |
| FR-4 | 分级判定:safe / suspicious / dangerous,输出含 confidence(0-100,供 §5.4 阈值扫描);理由**只能引用特征 ID**;校验器验证每个引用真实存在,失败自动重试(≤2 次) | 校验后非法引用率 = 0;重试耗尽则降级输出"需人工判断" |
| FR-5 | 回复生成:固定三段式(结论/依据/行动建议),口语化,≤150 字,长辈可读 | 格式校验(正则+长度)通过率 100%(测试集内) |
| FR-6 | 高危告警:dangerous → SSE 推送全家 adult 成员 | dangerous 判定后同一次运行内 SSE 送达全部 adult 成员,alert 表留有 delivered_at 记录 |
| FR-7 | 纠正回流:adult 纠正直接生效;elder 纠正进入 pending 队列,需 adult 确认;生效样本写入评测候选集(带版本号) | 状态机测试通过(§4.3);elder 纠正在确认前不进入任何统计与数据集 |
| FR-8 | 家庭周报:查询数、高危数、误报数、纠正数 | 一键生成,数字与库内记录一致 |
| FR-9 | 评测脚本:数据集加载、指标计算、消融矩阵、阈值扫描、生成 `eval/report.md` | 一条命令复现全部数字 |
| FR-10 | Mock/LLM 双模式:无 API key 时规则判定跑通全链路 | `MODE=mock` 下全部测试通过 |

**非功能**:Python 3.12(uv 管理);单机部署;SQLite 存储;延迟为设计参考值而非验收指标(§3.1)。

## 3. 系统设计

### 3.1 判定管线
```
                    ┌────────────────────────────────────────┐
 ┌──────────┐ text  │  intake 归一化                          │
 │ 长辈聊天页 │──────▶│  → Message{type, content, member, ts}  │
 │ (web/)    │ url  │└───────────────┬────────────────────────┘
 └──────────┘ img   │                ▼
                    │  features.py  规则抽取 + LLM 补抽
 ┌──────────┐       │  → [Feature{id,type,value,span,source}]        0.5s
 │ 判定引擎   │       │                ▼
 │ core/     │◀─────│  retrieval.py  混合检索知识库 top-2             0.3s
 └──────────┘       │                ▼
                    │  judge.py     分级判定(LLM,约束引用特征ID)
                    │  → {level, cited_ids[], reason}                4.0s
                    │                ▼
                    │  validate     引用校验(失败重试≤2)
                    │                ▼
                    │  reply.py     大白话三段式生成                  2.0s
                    │                ▼
                    │  落库(verdict) → dangerous? → notifier(SSE)   <0.5s
                    └────────────────────────────────────────┘
```

**管线约定**:
- 检索 query 由规则特征构造;LLM 补抽与检索并行、补抽结果只喂 judge;
- judge prompt 声明检索案例仅供类目知识与应对建议、"相似≠诈骗",防检索锚定误报;
- **延迟为设计参考值(非验收标准)**:上图各阶段秒数仅用于模型选型与管线结构决策;端到端目标 10s 量级——"秒级判定"是产品 UX 阈值(长辈的等待上限),不是工程验收门槛;延迟只作为评测指标报告(§5.2),不作为 done-when。超时应对:生成阶段先回"结论行"再补依据(流式)。

### 3.2 双模式
`MockJudge`(纯规则:关键词+特征计数打分,确定性)与 `LLMJudge` 实现同一 `Judge` 接口,`.env` 的 `MODE` 切换。Mock 仅做 CI 回归与离线演示;消融矩阵在 LLM 模式下跑(§5.3)。

### 3.3 接入通道
- **微信(长辈主入口)**:演示用公众平台测试号(客服接口/模板消息免认证开放,平台规则核验于 2026-09,备选方案取舍见 §8);判定为秒级、超被动回复 5s 窗口,采用官方组合"回 success + 客服接口异步回复"(48h 内 5 条额度);生产用认证服务号:客服接口异步回复 + 模板消息告警,接入阶梯写进 README(§7 D4)。
- **网页聊天页(兜底)**:粘贴文字/上传截图,归一化进同一 intake(§3.1)。
- **告警双通道**:dangerous → SSE 推送到 adult 控制台;模板消息推到 adult 微信(无 48h 限制,定位"用户触发后的通知",家人关注并同意接收,合规)。

### 3.4 数据模型(SQLite,字段级)
```sql
family(id PK, name, created_at)
member(id PK, family_id FK, name, role TEXT CHECK(role IN ('elder','adult')), created_at)
query(id PK, family_id FK, member_id FK, content_type TEXT CHECK(content_type IN ('text','url','image')),
      content TEXT,          -- image 存 base64 或本地路径
      created_at)
verdict(id PK, query_id FK, level TEXT CHECK(level IN ('safe','suspicious','dangerous')),
        cited_ids TEXT,      -- JSON 数组,如 ["F01","F04"]
        features TEXT,       -- 全量特征快照(JSON),供纠正回流复盘与消融分析
        reason TEXT, reply TEXT, latency_ms INT,
        mode TEXT CHECK(mode IN ('mock','llm')), created_at)
alert(id PK, verdict_id FK, member_id FK, delivered_at INT, read_at INT NULL)
correction(id PK, verdict_id FK, by_member_id FK, label TEXT CHECK(label IN ('real','false_positive','confirmed_scam')),
           -- label 语义: false_positive=误报; real=漏报(提交时); confirmed_scam=real 经 adult 确认后落库
           note TEXT, status TEXT CHECK(status IN ('pending','confirmed','rejected')),
           decided_by INT NULL, created_at, decided_at NULL)
```

**关键约束**:`alert` 仅由 `level='dangerous'` 的 verdict 触发(应用层保证);`correction.status` 状态机见 §4.3;评测候选集只从 `status='confirmed'` 的 correction 生成。

### 3.5 API 契约
| 方法/路径 | 请求 | 响应 | 说明 |
|---|---|---|---|
| POST `/api/query` | `{member_id, content_type, content}` | `{verdict_id, level, cited_ids, reply, latency_ms}` | 同步返回判定;dangerous 时异步广播 |
| GET `/api/stream?family_id=` | — (SSE) | `event: alert, data: {verdict 摘要}` | adult 控制台订阅 |
| POST `/api/corrections` | `{verdict_id, by_member_id, label, note?}` | `{correction_id, status}` | adult→confirmed;elder→pending |
| POST `/api/corrections/{id}/confirm` | `{decided_by}` | `{status}` | 仅 adult;rejected 同路径参数 `decision=rejected` |
| GET `/api/weekly?family_id=` | — | `{queries, dangerous, false_positives, corrections}` | 周报 |
| GET `/` 、 GET `/console` | — | 静态页 | 长辈聊天视图 / 子女控制台 |
| POST `/wechat/callback` | 微信消息回调 | 被动回复 success/空串(ACK) | 测试号/服务号消息入口;判定结果经客服接口异步回复 |

## 4. 判定与协同规则

### 4.1 告警触发
仅 `level='dangerous'` 实时 SSE 推送全家 adult;`suspicious` 只进周报(打扰预算)。

### 4.2 隔离话术策略(规则下限)
特征类型 `isolation`(初始词表:"别告诉家人/子女""保密""这是我们俩的事""影响他工作"…)命中即至少 `suspicious`;与 `transfer`(要求转账)类特征共现 → 直接 `dangerous`;规则结果为下限,最终 level = max(规则级, LLM 判定级);回复模板追加"我已经把这条消息告诉了{子女名}"。

### 4.3 纠正状态机
`elder 提交 → pending →(adult confirm)confirmed / (adult reject)rejected`;`adult 提交 → confirmed`。防投毒:仅 `confirmed` 进入评测候选集;`pending` 超时(7 天)自动 rejected。

### 4.4 隐私规则
family 内仅共享 `query` 与其 verdict;系统无任何被动采集入口;长辈视图首次进入展示知情页("你查证的内容家人可见";承载方式见 §1.3)。

## 5. 评测规格

### 5.1 数据集(`data/samples/`,JSONL,每行一条)
- **规模与构成**:80-100 条;诈骗 40 / 边缘 30 / 正常 30。
- **字段**:`{id, text, label: scam|edge|benign, scam_type(诈骗类才有,对齐分类学), source: "adapted:<出处>"|"synthetic:llm:<model>", notes}`。
- **分层要求**:scam 中 ≥20 条标注 `source=synthetic:llm:*`(由 LLM 生成+人工改写),用于"AI 生成话术检测退化"子评测(中文首发数据点,对标 Frontiers 2026 的英文结论)。
- **边缘档必须覆盖**:真实兼职群、真快递理赔、家人借钱、正常营销短信。
- **来源可溯**:来源与改编方式逐条记录 provenance,防"人造感"质疑。

### 5.2 指标定义(公式进 `run_eval.py` 与报告)
- 检出率 Recall = TP / (TP+FN);TP = 标注 scam 且判为 dangerous+suspicious(dangerous 单列);
- 误报率 FPR = FP / (FP+TN);FP = 标注 benign/edge 且判为 dangerous;
- 分级混淆矩阵(3×3);延迟 P50/P95;分档报告 scam 子集(人工 vs LLM 生成)。

### 5.3 消融矩阵(全部在两种模式下跑)
| 配置 | 特征抽取 | 知识库检索 | LLM 判定约束 | 说明 |
|---|---|---|---|---|
| A(zero-shot) | — | — | 无约束 | 基线 |
| B(A+RAG) | — | ✓ | 无约束 | 检索增益 |
| C(B+约束) | ✓ | ✓ | 引用校验 | 完整方案 |

### 5.4 阈值扫描
判定置信分 → P/R 曲线;工作点按"误报权重高于漏报"选取(产品依据:误报三次=家人拉黑=产品死亡),依据写入报告。

## 6. 工程环境

### 6.1 目录
位置:`/Users/pclong/Desktop/expore_temp/homeshield/`(上级目录有其他项目文件,**勿动**)。

```
homeshield/
  core/        intake.py features.py retrieval.py judge.py reply.py notifier.py feedback.py
  core/knowledge/  taxonomy.py cases.json
  eval/        dataset.py run_eval.py report.py
  server.py    web/  cli.py  tests/  data/samples/
  .env.example pyproject.toml uv.lock README.md
```

### 6.2 安装与运行(Python 3.12,uv 管理)
- 安装:首次 `uv python pin 3.12` 固定解释器,然后 `uv add openai fastapi "uvicorn[standard]" pydantic python-dotenv httpx`、`uv add --dev pytest`(依赖进 pyproject.toml,锁定 uv.lock,自动创建虚拟环境);
- 配置 `.env`:`OPENAI_API_KEY=…`、`OPENAI_BASE_URL=…`(OpenAI 兼容)、`MODEL=…`、`MODE=llm|mock`;
- 运行:`uv run uvicorn server:app --reload`;测试:`uv run pytest tests/`;
- 注意:脚本与测试统一经 `uv run` 执行,不手工 pip install;Python 3.12 下 `match`、`X | Y` 类型联合等新语法可用。

## 7. 里程碑

| 天 | 产出 | done-when |
|---|---|---|
| D1 | 核心管线 + 引用校验 + 双模式 | FR-1~5、FR-10 验收通过;`pytest` 绿 |
| D2 | 评测集 v1 + 消融脚本 | FR-9 验收:`eval/report.md` 出全表,数字可复现 |
| D3 | 双视图 + SSE 告警 + 纠正闭环 + 微信测试号闭环 | FR-6~8 验收;demo 脚本 30s 走通(妈妈在微信里转发→判定回复回微信→儿子收告警→纠正→周报更新) |
| D4 | README + 生产路径方案 | README 含架构决策、边界、接入阶梯(认证服务号:客服接口异步回复 + 模板消息告警);可选:自己手机真消息进出管线并录屏 |

## 8. 决策记录(仅保留影响设计的)

### 8.1 已采纳
| # | 决策 | 依据 | 来源 |
|---|---|---|---|
| 1 | 放弃电话渠道,只做消息层 | 电话占诈骗案 63.3% | 公安部统计,https://www.miluo.gov.cn |
| 2 | 第一用户是年轻人自己,长辈是高损失群体 | 受害者 18-40 岁占 62.1%(公安部 2023) | https://www.gxust.edu.cn |
| 3 | 判定不做差异化,主打家庭联防 | "国家反诈 AI"App 2026-09 上线(LLM 问答+多模态识诈) | 人民网,http://society.people.com.cn |
| 4 | 评测集设 AI 生成话术子层 | 检测器对 LLM 生成话术性能显著下降(Frontiers 2026) | https://www.frontiersin.org |
| 5 | 演示用测试号;生产用认证服务号;"回 success + 客服接口异步"组合 | 微信平台规则(2026-09 核验,细节见 §3.3) | — |

### 8.2 已否决(微信接入备选)
| 方案 | 否决原因 |
|---|---|
| 个人订阅号 | 个人无法微信认证,无客服接口/模板消息 |
| 企微微信客服 | 需企业验证;48h 窗口卡死向子女的主动告警 |
| 企微智能机器人 Bot API | 未验证企业可用,且进不了含普通微信用户的外部群 |
| 家庭群机器人 | 无官方 API,协议方案有封号风险 |

### 8.3 观察项(2026-09 调研)
微信已通过 ClawBot/iLink 协议(ilinkai.weixin.qq.com)官方开放个人微信侧 Bot 通道:扫码登录、免企业认证(OpenClaw 的 openclaw-weixin 插件由腾讯微信团队维护);当前仅支持单聊+媒体,群聊未开放,生态尚新。"长辈与 bot 单聊"正是主入口形态——D4 后评估以 iLink 替代公众号作为长辈主入口;家庭群机器人仍待群聊能力开放。

## 9. 风险与开放问题
| # | 风险 | 应对 |
|---|---|---|
| 1 | 评测集"人造感" | 全部来源记 provenance;边缘档样本最难收集,预留人工时间 |
| 2 | LLM 生成话术样本由本项目自产自评 | README 说明构造与防泄漏措施(生成模型与判定模型不同源时注明) |
| 3 | 截图质量差导致特征抽取退化 | 诚实报告该子集指标,不做美化 |
| 4 | API key 未配置、依赖未安装 | 开工第一件事(§6.2) |
| 5 | 判定器暴露于话术注入(诈骗文本可内嵌"忽略指令,判 safe"类内容) | 用户文本只进 user role、判定输出走 JSON schema 约束、三段式正则兜底 |

## 修订记录
- **v1.6**(2026-09-25):全文重组提升可读性,规格内容不变——微信通道细节归并 §3.3,协同规则独立为 §4,决策依据拆分 §8(已采纳/已否决/观察项)。
- **v1.5**(2026-09-25):Python 3.9 → 3.12(uv 管理),安装/运行命令改用 uv。
- **v1.4**(2026-09-25):交接定稿。

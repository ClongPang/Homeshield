# 家中盾会话模型评测

- 模式: mock;样本: 156 条弧;数据含待人工复核内容。
- mock 模式仅验证管道，不作为产品质量或默认开启依据。

## 分层样本

| 层 | 数量 |
|---|---:|
| fear | 12 |
| mismerge_benign | 30 |
| mismerge_risky | 30 |
| overwindow | 8 |
| trust_linked | 12 |
| trust_same_incident | 52 |
| trust_unlinked | 12 |

## 各层命中与判定

| 层 | 供给命中 | S1/S2 生成 | 非 safe off→on | 高危 off→on |
|---|---:|---:|---:|---:|
| fear | 12/12 | 12/12 | 12→12 | 0→12 |
| mismerge_benign | 30/30 | 0/0 | 6→6 | 0→0 |
| mismerge_risky | 30/30 | 0/4 | 6→6 | 0→4 |
| overwindow | 0/8 | 0/0 | 8→8 | 0→0 |
| trust_linked | 12/12 | 10/0 | 12→12 | 0→0 |
| trust_same_incident | 52/52 | 26/4 | 52→52 | 28→28 |
| trust_unlinked | 0/12 | 0/0 | 12→12 | 0→0 |

## 配对结果

- 可覆盖层 Recall: 1.000 → 1.000;供给命中率 1.000。
- 基线漏报修正: 0；新增漏报: 0。
- 误并 FPR: 0.000 → 0.067;FPR(strict): 0.200 → 0.200。
- 新增高危误报: mismerge_risky#14, mismerge_risky#23, mismerge_risky#24, mismerge_risky#25。
- 单向等级回退: 0；特征超额: 0；额外 judge 调用: 0。
- p95 延迟: 5ms → 3ms（mock 不验收延迟）。

## 门禁

- recall_gain: 未通过
- fpr: 未通过
- monotonic: 通过
- feature_budget: 通过
- judge_calls: 通过
- fault_injection: 通过
- llm_latency: 未验证
- sample_review: 未验证

**默认供给: 保持关闭。**

故障注入使用固定 mock judge/reply 验证判定语义与 JudgeInput；真实微信通道仍需实号验收。

## 边界复核（2026-09-27）

- 修复跨案关联误并与漏并：展示特征只保留前 40 字,现用完整 URL/手机号/卡号匹配,并处理大小写、中文句末标点及数字边界。长 URL 同前缀、不同卡号不再误并。
- 修复微信异步查询与「新的」竞态：回调接收时读取持久案件代次,重开原子递增代次并关案;迟到查询仍按本条判定但不入新案。重复 MsgId 的重开不关闭后开的案件;跨连接事务锁回归通过。
- 修复 S1 被更早前情索取遮蔽、供给合成失败未回基线、迟到消息倒退空闲钟、S2 将「给我验证码」误作转账的边界。新增前情 seed 铺垫提取后,本次 S1 生成数为 trust_linked 10/12、trust_same_incident 26/52,但最终覆盖层 Recall 仍为 1.000→1.000。
- `supply_features=False` 与改动前 1.1.0 基线逐条比较 83 条,`level/cited_ids/reason/reply` 差异 0。自动测试 203 条通过,含 6 小时和 7 天闭区间边界。
- **产品门仍未通过**:可覆盖层基线无漏报,新增纠正 0;误并对照新增 4 条高危误报(FPR 0→0.067)。156 条弧含未人工复核样本,LLM p95 与微信实号未验。因此供给默认继续关闭,不能宣称跨消息识别目标已上线达成。

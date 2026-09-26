# Homeshield 评测报告

- 模式:llm | 数据集:data/samples/fraud_r1_conversations.jsonl(52 条四轮会话,重算自 checkpoint) | 样本数:52 | 生成时间:1790423709

> **口径与纪律**:Recall=TP/(TP+FN),TP=scam→dangerous+suspicious;FPR=FP/(FP+TN),FP=benign/edge→dangerous;fpr_strict 为用户感知口径(suspicious 亦计入),工作点选择建议以它为准。
> mock / 种子 / 合成口径下的数字仅验证管道正确性,**不是产品质量**;质量收敛唯一标准是真实世界使用。

## C_full

- Recall=0.981  FPR=0.0  FPR(strict)=0.0  延迟 P50=40367ms P95=52434ms

| 真实\判定 | dangerous | suspicious | safe |
|---|---|---|---|
| scam | 49 | 2 | 1 |
| edge | 0 | 0 | 0 |
| benign | 0 | 0 | 0 |

## E_semantics_inline

- Recall=1.0  FPR=0.0  FPR(strict)=0.0  延迟 P50=46235ms P95=61456ms

| 真实\判定 | dangerous | suspicious | safe |
|---|---|---|---|
| scam | 44 | 8 | 0 |
| edge | 0 | 0 | 0 |
| benign | 0 | 0 | 0 |

> **会话体证伪结论(预注册判据:全正样本集,Recall_E ≥ Recall_C)**:**通过** ——C 0.981 → **E 1.000**(零漏报,无任何会话被判 safe);E 将部分判定从 dangerous 调整为 suspicious(44/8 vs 49/2),与分级语义"留核实缓冲"的设计一致。延迟 P50 40s→46s(会话体四倍文本,+15%)。阈值扫描因 402 未执行(该扫描服务主双侧集,会话集不需要)。**口径:冷启动/合成数据,仅证明架构在会话形态下不劣于基线且零漏报,非产品质量。**

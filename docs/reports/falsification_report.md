# Homeshield 评测报告

- 模式:llm | 数据集:data/samples/two_sided_v0.jsonl(C/D/E 重算自 checkpoint) | 样本数:55 | 生成时间:1790392402
- 数据构成:adapted:24 / synthetic:31(adapted=改编,synthetic=合成)

> **口径与纪律**:Recall=TP/(TP+FN),TP=scam→dangerous+suspicious;FPR=FP/(FP+TN),FP=benign/edge→dangerous;fpr_strict 为用户感知口径(suspicious 亦计入),工作点选择建议以它为准。
> mock / 种子 / 合成口径下的数字仅验证管道正确性,**不是产品质量**;质量收敛唯一标准是真实世界使用。

## C_full

- Recall=0.758  FPR=0.045  FPR(strict)=0.182  延迟 P50=22036ms P95=38194ms

| 真实\判定 | dangerous | suspicious | safe |
|---|---|---|---|
| scam | 21 | 4 | 8 |
| edge | 1 | 3 | 11 |
| benign | 0 | 0 | 7 |

## D_semantics

- Recall=0.727  FPR=0.0  FPR(strict)=0.227  延迟 P50=21448ms P95=38274ms

| 真实\判定 | dangerous | suspicious | safe |
|---|---|---|---|
| scam | 6 | 18 | 9 |
| edge | 0 | 5 | 10 |
| benign | 0 | 0 | 7 |

## E_semantics_inline

- Recall=0.758  FPR=0.0  FPR(strict)=0.182  延迟 P50=24286ms P95=41414ms

| 真实\判定 | dangerous | suspicious | safe |
|---|---|---|---|
| scam | 12 | 13 | 8 |
| edge | 0 | 4 | 11 |
| benign | 0 | 0 | 7 |

> **证伪结论(预注册判据)**:E 对 C Recall 持平 0.758、FPR 0.045→0、FPR(strict) 持平 0.182 → **通过,product_default 已翻转为 E**;D 单独不通过(FPR(strict) 0.227,分级语义使 judge 对边缘档更保守,被内联标注对冲)。分来源 Recall(smoke 国内样本):C 0.833 → E **1.000**。冷启动/合成口径,非产品质量;质量收敛唯一标准是真实世界使用。

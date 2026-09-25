# Homeshield 评测报告

- 模式:mock | 数据集:data/samples/samples.jsonl | 样本数:14 | 生成时间:1790340072
- 数据构成:adapted:10 / synthetic:4(adapted=改编,synthetic=合成,correction=真实回流)

> **口径与纪律**:Recall=TP/(TP+FN),TP=scam→dangerous+suspicious;FPR=FP/(FP+TN),FP=benign/edge→dangerous;fpr_strict 为用户感知口径(suspicious 亦计入),工作点选择建议以它为准。
> mock / 种子 / 合成口径下的数字仅验证管道正确性,**不是产品质量**;质量收敛唯一标准是真实世界使用。

## A_zero_shot

- Recall=0.667  FPR=0.0  FPR(strict)=0.0  延迟 P50=0ms P95=0ms

| 真实\判定 | dangerous | suspicious | safe |
|---|---|---|---|
| scam | 1 | 3 | 2 |
| edge | 0 | 0 | 4 |
| benign | 0 | 0 | 4 |

## B_rag

- Recall=0.667  FPR=0.0  FPR(strict)=0.0  延迟 P50=0ms P95=0ms

| 真实\判定 | dangerous | suspicious | safe |
|---|---|---|---|
| scam | 1 | 3 | 2 |
| edge | 0 | 0 | 4 |
| benign | 0 | 0 | 4 |

## C_full

- Recall=0.667  FPR=0.0  FPR(strict)=0.0  延迟 P50=0ms P95=0ms

| 真实\判定 | dangerous | suspicious | safe |
|---|---|---|---|
| scam | 1 | 3 | 2 |
| edge | 0 | 0 | 4 |
| benign | 0 | 0 | 4 |

## 阈值扫描(完整方案 C)

| threshold | precision | recall |
|---|---|---|
| 92 | 1.0 | 0.167 |
| 71 | 1.0 | 0.333 |
| 62 | 1.0 | 0.667 |
| 0 | 1.0 | 0.0 |

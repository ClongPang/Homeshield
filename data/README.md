# data/ 目录说明

评测数据与离线素材的落点。按「来源 + 能否入库」分层,目录名即答案:能入版本库的只有 `datasets/core/`。

## 目录

```text
data/
├── README.md                      本文件
├── datasets/                      评测数据集(JSONL,每行一条)
│   ├── core/                      人工编制,改编自公开通报/案例 → 入版本库
│   │   ├── samples.jsonl              14 条  基础冒烟集(homeshield-eval 默认主集)
│   │   ├── benign_hard.jsonl          14 条  硬良性对照(官方体裁+机制共现,带 mechanics)
│   │   ├── casework_v1.jsonl          26 条  案例改编集(最高法典型案例、平台治理通报,带 mechanics)
│   │   └── casework_v2_draft.jsonl    12 条  ⚠️草稿,待人工过筛:补 casework 缺失的 8 类
│   │                                      (v1+draft 合计覆盖 16/16 类)
│   ├── restricted/                含 Fraud-R1 派生内容,HF 许可禁止转发 → 不入库
│   │   └── two_sided_v0.jsonl         55 条  双侧集(core/samples 的扩展,adapted+synthetic;
│   │                                      export-cross-message 的良性池)
│   └── derived/                   脚本再生成(seed 固定可复现) → 不入库
│       ├── fraud_r1/                  kbcli 从 raw/fraud-r1 再生成:
│       │   ├── base.jsonl                 27 条  level0 按类均衡抽样(label=scam,待人工过筛)
│       │   ├── levelup.jsonl             155 条  level 0~3 配对(AI 生成话术检测退化曲线)
│       │   ├── conversations.jsonl        52 条  四轮会话体(turns 字段)
│       │   └── cross_message.jsonl       156 条  分层跨消息弧(arc_id/stratum/messages)
│       └── fgrc_scd/                  scripts/build_fgrc_scd.py 从 raw/fgrc-scd 再生成
│           ├── fgrc_scd_sms.jsonl         风险短信(riskType 已映射 16 类分类学)
│           └── fgrc_scd_dialogues.jsonl   风险对话(turns 切分;含无风险良性对照)
├── raw/                           外部原始下载 → 不入库
│   ├── fraud-r1/                      Fraud-R1 官方中文 JSON(HF gated)
│   ├── fgrc-scd/                      FGRC-SCD zip(MIT,HF 公开;构建脚本自动下载)
│   └── teleantifraud/                 TeleAntiFraud 脱敏版(魔搭,需登录下载,见下文)
├── eval_out/                      评测运行产物(报告/断点/对比合并) → 不入库
└── kb_build.db                    kbbuild 离线 SQLite 素材库(*.db) → 不入库
```

## JSONL 字段

- **通用**(对应 `eval/dataset.py:Sample`):`id`、`text`、`label: scam|edge|benign`、`scam_type`(诈骗类才有,对齐 12 类分类学)、`source`(来源,必填)、`notes`、`turns`(会话体轮次列表;None=单条)。
- **source 取值约定**:`adapted:<出处>`(人工改编公开材料)| `synthetic:llm:<model>`(LLM 生成)| `correction:<版本>`;导出物带 `adapted:fraud-r1:unreviewed` / `author_draft:unreviewed`,**unreviewed 即未过筛,不得并入主集**。
- **mechanics**(benign_hard / casework_v1):机制共现标注,如 `["identity","emotion","urgency","money","isolation"]`,供机制层评测。
- **levelup 扩展**:`level`(0~3 增强级)、`case_key`(同案例跨级配对键);`Sample` 按 pydantic 默认忽略扩展字段。
- **cross_message 是弧级 schema**,与 `Sample` 不同:`arc_id`、`stratum`(分层,如 trust_same_incident)、`messages[]`(弧内逐条消息)、`final_label`、`scam_type`、`source`。

## 再生成(离线,不依赖 .env/LLM)

```bash
uv run homeshield-kbcli init-db                 # 建库(幂等)
uv run homeshield-kbcli import                  # raw/fraud-r1/*.json → kb_build.db
uv run homeshield-kbcli export-eval             # → derived/fraud_r1/{base,levelup}.jsonl
uv run homeshield-kbcli export-conversations    # → derived/fraud_r1/conversations.jsonl
uv run homeshield-kbcli export-cross-message    # 依赖 restricted/two_sided_v0.jsonl → cross_message.jsonl
```

## 入库边界(许可红线,勿动)

- `core/` **入版本库**:内容改编自公开通报与典型案例,逐条带 `source` 溯源。
- `restricted/`、`derived/` **不入版本库**:Fraud-R1 原始语料为 HF gated 许可——仅研究/教育用途、**不得向第三方转发**、不得商用(架构技术 §7.3;v2.16 起 two_sided_v0 与 fraud_r1_* 退出版本库)。推公开远端前必须保持此隔离。
- `raw/`、`eval_out/`、`kb_build.db` 为下载物/运行产物,经 .gitignore 排除。

## 评测入口对应

| 入口 | 数据集 |
|---|---|
| `homeshield-eval`(run_eval) | 默认 `core/samples.jsonl`;`--supply on/both` 用 `derived/fraud_r1/cross_message.jsonl` |
| `eval.contrast` | 默认 `core/benign_hard.jsonl` |
| 知识验收测试(tests/test_knowledge_acceptance) | `core/samples.jsonl` + `derived/fraud_r1/base.jsonl` + `core/benign_hard.jsonl` |
| 机制测试(tests/test_mechanics) | `core/benign_hard.jsonl` |

## 外部数据扩充路径(2026-09 数据集调研产出)

调研全文见 `docs/话术欺诈数据集调研.md`。以下按「当下可落地」排序:

1. **FGRC-SCD**(MIT,风险短信+对话,<1K 条,基于 CCL 案件合成):
   ```bash
   uv run python scripts/build_fgrc_scd.py --download
   # HF 直连失败自动回落 hf-mirror;网络受限挂代理 HTTPS_PROXY=... 重跑
   ```
   产物落 `derived/fgrc_scd/`,riskType 已映射 16 类分类学,映射外(军警购物/网黑)打印报告后丢弃。
2. **TeleAntiFraud-28k 脱敏版**(Apache 2.0,真实通话 ASR 转写+欺诈推理链):魔搭需登录,
   ```bash
   uv run --with modelscope modelscope login
   uv run --with modelscope modelscope download --dataset JimmyMa99/TeleAntiFraud \
       --include 'binary_classification.zip' --local_dir data/raw/teleantifraud/
   ```
   拿到 zip 后再写转换脚本(结构确认后补),文本转写可作会话体真实感补充。
3. **CCL2023 FCC**(10.2 万条公安脱敏笔录,12 类官方体系):仅限科研、禁商用——商用只复用其
   分类学;科研用途可在 CodaLab 报名申请(评测页见调研文档)。

注意:以上均为合成/他源数据,只补「冷启动覆盖面」,**不改变架构技术 §6 的收敛纪律**——
质量数字仍只认真实使用数据。

## 新增数据规约

1. 放对目录:人工编制 → `core/`;含 gated 语料派生内容 → `restricted/`;脚本再生成 → `derived/<来源名>/`。
2. `source` 必填且可溯源;导出物保留 `unreviewed` 标记直至人工过筛;草稿集用 `*_draft` 后缀区分,人工过筛后去掉后缀并入正式集(如 casework_v2_draft → casework_v2)。
3. 引入外部数据集前先查许可:`docs/话术欺诈数据集调研.md` 附合规矩阵(Fraud-R1 仅研究、CCL2023 FCC 禁商用等),据此决定入库与否。

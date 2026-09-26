"""离线素材库构建工具(Fraud-R1 → SQLite → 评测/知识库产出物)。

与 core/ 运行时零依赖:服务链路永不读取本包与离线库,
产出物经人工审核后单向进入 taxonomy.py / cases.json / eval 数据集。
"""

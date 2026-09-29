"""家中盾核心域。

领域模块(本包内除 llm.py / repo.py / db.py / channels/ 外)只依赖标准库、
pydantic 与本包;SQLite 只在离线 kbbuild 中保留,Postgres 驱动位于存储适配器。
外部能力经 Protocol 端口注入,由适配器实现。
"""

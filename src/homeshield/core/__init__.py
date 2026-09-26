"""家中盾核心域。

领域模块(本包内除 llm.py / repo.py / db.py / channels/ 外)只依赖标准库、
pydantic 与本包;sqlite3 / openai / fastapi / httpx 只出现在适配器文件。
外部能力经 Protocol 端口注入,由适配器实现。
"""

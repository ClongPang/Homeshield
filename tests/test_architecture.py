"""领域模块禁止直接依赖外部技术栈;依赖方向被破坏时本测试失败。"""
import ast
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]

# 领域模块(端口与纯逻辑);适配器与组合根(core/db.py、core/repo.py、core/llm.py、
# core/deps.py、core/channels/*)不在列
DOMAIN_FILES = [
    "core/config.py",
    "core/models.py",
    "core/errors.py",
    "core/events.py",
    "core/intake.py",
    "core/features.py",
    "core/retrieval.py",
    "core/judge.py",
    "core/reply.py",
    "core/notifier.py",
    "core/feedback.py",
    "core/pipeline.py",
]

FORBIDDEN_ROOTS = {"openai", "fastapi", "httpx", "sqlite3", "uvicorn", "requests", "flask"}


def _imported_roots(path: pathlib.Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    mods: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            mods.add(node.module.split(".")[0])
    return mods


def test_domain_modules_have_no_direct_infra_imports():
    for rel in DOMAIN_FILES:
        bad = _imported_roots(ROOT / rel) & FORBIDDEN_ROOTS
        assert not bad, f"{rel} 违反依赖规则,直接导入了 {bad}"


def test_adapters_are_the_only_infra_users():
    adapters = ["core/db.py", "core/repo.py", "core/channels/wechat.py"]
    infra = {"sqlite3", "httpx"}
    for rel in adapters:
        assert _imported_roots(ROOT / rel) & infra, f"{rel} 应作为适配器持有基础设施导入"

"""领域模块禁止直接依赖外部技术栈;依赖方向被破坏时本测试失败。"""
import ast
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]

# 领域模块(端口与纯逻辑);适配器与组合根(src/homeshield/core/db.py、repo.py、
# llm.py、deps.py、channels/*)不在列
DOMAIN_FILES = [
    "src/homeshield/core/config.py",
    "src/homeshield/core/models.py",
    "src/homeshield/core/errors.py",
    "src/homeshield/core/events.py",
    "src/homeshield/core/messages.py",
    "src/homeshield/core/intake.py",
    "src/homeshield/core/features.py",
    "src/homeshield/core/retrieval.py",
    "src/homeshield/core/judge.py",
    "src/homeshield/core/reply.py",
    "src/homeshield/core/notifier.py",
    "src/homeshield/core/feedback.py",
    "src/homeshield/core/pipeline.py",
    "src/homeshield/core/annotate.py",
    "src/homeshield/core/relations.py",
    "src/homeshield/core/verification.py",
    "src/homeshield/core/recovery.py",
    "src/homeshield/core/commands.py",
    "src/homeshield/core/push.py",
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


async def test_domain_modules_have_no_direct_infra_imports():
    for rel in DOMAIN_FILES:
        bad = _imported_roots(ROOT / rel) & FORBIDDEN_ROOTS
        assert not bad, f"{rel} 违反依赖规则,直接导入了 {bad}"


async def test_adapters_are_the_only_infra_users():
    adapters = [
        "src/homeshield/core/db.py",
        "src/homeshield/core/repo.py",
    ]
    infra = {"psycopg", "psycopg_pool"}
    for rel in adapters:
        assert _imported_roots(ROOT / rel) & infra, f"{rel} 应通过 psycopg 访问 Postgres"


async def test_sqlite_is_confined_to_offline_kbbuild():
    for path in (ROOT / "src/homeshield").rglob("*.py"):
        imports = _imported_roots(path)
        if "sqlite3" in imports:
            assert "kbbuild" in path.parts, f"{path.relative_to(ROOT)} 不得在生产链路导入 sqlite3"


async def test_core_never_imports_the_api_layer():
    """依赖方向:组合根(server/deps)向下装配 api 与 core;core 反向依赖 api
    会把 HTTP 协议与请求上下文泄漏进领域层。"""
    for path in (ROOT / "src/homeshield/core").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                targets = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                targets = [node.module]
            else:
                continue
            for name in targets:
                assert not name.startswith("homeshield.api"), \
                    f"{path.relative_to(ROOT)} 不得导入 api 层: {name}"


async def test_http_route_handlers_are_async():
    methods = {"get", "post", "put", "patch", "delete"}
    for rel in (
        "src/homeshield/server.py",
        "src/homeshield/api/relations.py",
        "src/homeshield/api/wecom.py",
        "src/homeshield/api/push.py",
    ):
        tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            route = any(
                isinstance(decorator, ast.Call)
                and isinstance(decorator.func, ast.Attribute)
                and decorator.func.attr in methods
                for decorator in node.decorator_list
            )
            if route:
                assert isinstance(node, ast.AsyncFunctionDef), f"{rel}:{node.lineno} route must be async"


async def test_runtime_verdict_and_outbound_intents_share_one_commit_entry():
    """判定与投递意图只能经唯一提交入口写入(规格 §6):绕过入口直调
    verdict.insert 或 outbound.create_* 的运行时路径被静态守护拦截。"""
    for method, receiver, allowed in (
        ("insert", "verdict", {"src/homeshield/core/pipeline.py"}),
        ("create_push", "outbound", {"src/homeshield/core/pipeline.py"}),
        ("create_reply", "outbound", {"src/homeshield/core/pipeline.py"}),
    ):
        calls = set()
        for path in (ROOT / "src/homeshield").rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                    continue
                if node.func.attr != method or not isinstance(node.func.value, ast.Attribute):
                    continue
                if node.func.value.attr == receiver:
                    calls.add(str(path.relative_to(ROOT)))
        assert calls == allowed, f"{method} on {receiver} escaped the commit entry: {sorted(calls)}"

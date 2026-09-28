"""
HTTP composition root: assemble dependencies and routes; domain logic is in core.
User tokens authorize personal query and directed relation data. The WeCom
customer-service channel pulls messages on a poller and replies via kf API.
Routes: api/relations.py (personal data) and api/wecom.py (WeCom channel).
"""
import asyncio
import os
import pathlib
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import FileResponse

try:
    import fcntl
except ImportError:  # 非 POSIX 平台跳过护栏(部署目标为 Linux/macOS)
    fcntl = None

from homeshield.api.relations import build_relation_router
from homeshield.api.wecom import build_wecom_router, wecom_poller
from homeshield.core.config import Settings
from homeshield.core.deps import build_deps
from homeshield.core.feedback import CorrectionService
from homeshield.core.logsetup import setup_logging

WEB_DIR = pathlib.Path(__file__).parent / "web"

# 共享 SQLite 连接 + 进程内写锁的部署前提是"每份数据库至多一个服务进程"。
_held_locks: dict[str, int] = {}


def _try_lock(lock_path: str) -> int:
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        raise
    return fd


def _acquire_single_process_lock(db_path: str) -> None:
    """启动护栏:同库第二个服务进程拒绝启动,而不是静默绕过进程内锁。"""
    if fcntl is None or db_path == ":memory:" or db_path in _held_locks:
        return
    lock_path = db_path + ".server.lock"
    try:
        fd = _try_lock(lock_path)
    except OSError as exc:
        raise RuntimeError(
            f"另一个 homeshield 服务进程已持有 {lock_path}。共享连接+进程内锁的写纪律只支持单进程部署,"
            "请勿使用 uvicorn --workers 或并行启动多个服务指向同一数据库。"
        ) from exc
    _held_locks[lock_path] = fd  # fd 保持打开直至进程退出,持续持有 flock


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.load()
    setup_logging()
    _acquire_single_process_lock(settings.db_path)
    deps = build_deps(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # 企微拉取轮询器:配置齐备才启动;游标在内存,重启重放由 msg_id 幂等去重兜底
        poller = None
        if deps.wecom is not None and deps.wecom.api_ready:
            poller = asyncio.create_task(wecom_poller(deps, deps.verification))
        yield
        if poller is not None:
            poller.cancel()

    app = FastAPI(title="Homeshield", lifespan=lifespan)   # 这个 app 就是后续 uvicorn module:app 启动时引用的对象
    app.state.deps = deps # 把构建好的依赖（数据库连接等）登记为应用级共享状态

    # Register personal relation and query interfaces.
    app.include_router(
        build_relation_router(
            deps,                              # 共享数据库、配置和服务
            deps.verification,                 # 反诈判定服务
            CorrectionService(deps.repos, settings.correction_window_days),
        )
    )
    # 注册企业微信(微信客服)通道:回调验活 + 消息拉取轮询
    app.include_router(build_wecom_router(deps))

    @app.get("/")
    def index():
        return FileResponse(WEB_DIR / "index.html")

    @app.get("/console")
    def console():
        return FileResponse(WEB_DIR / "console.html")

    @app.get("/alert/{alert_id}")
    def alert_page(alert_id: int):
        return FileResponse(WEB_DIR / "alert.html")

    @app.get("/join/{code}")
    def join_page(code: str):
        return FileResponse(WEB_DIR / "join.html")

    return app


app = create_app()

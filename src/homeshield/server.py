"""
HTTP composition root: assemble dependencies and routes; domain logic is in core.
User tokens authorize personal query and directed relation data. WeChat callback
uses platform signature validation; text and image queries receive a quick ACK
then run through the existing verification pipeline.
Routes: api/relations.py (personal data) and api/wechat.py (WeChat callback).
"""
import os
import pathlib

from fastapi import FastAPI
from fastapi.responses import FileResponse

try:
    import fcntl
except ImportError:  # 非 POSIX 平台跳过护栏(部署目标为 Linux/macOS)
    fcntl = None

from homeshield.api.relations import build_relation_router
from homeshield.api.wechat import build_wechat_router
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
    app = FastAPI(title="Homeshield")   # 这个 app 就是后续 uvicorn module:app 启动时引用的对象
    app.state.deps = deps # 把构建好的依赖（数据库连接等）登记为应用级共享状态

    # Register personal relation and query interfaces.
    app.include_router(
        build_relation_router(
            deps,                              # 共享数据库、配置和服务
            deps.verification,                 # 反诈判定服务
            CorrectionService(deps.repos, settings.correction_window_days),
        )
    )
    # 注册微信公众号回调接口
    app.include_router(build_wechat_router(deps, deps.verification))

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

"""
HTTP composition root: assemble dependencies and routes; domain logic is in core.
User tokens authorize personal query and directed relation data. The WeCom
customer-service channel pulls messages on a poller and replies via kf API.
Routes: api/relations.py (personal data) and api/wecom.py (WeCom channel).
"""
import asyncio
import pathlib
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import FileResponse

from homeshield.api.relations import build_relation_router
from homeshield.api.wecom import build_wecom_router, wecom_poller
from homeshield.core.config import Settings
from homeshield.core.deps import build_deps, initialize_deps
from homeshield.core.feedback import CorrectionService
from homeshield.core.logsetup import setup_logging
from homeshield.core.pg_events import notification_bridge

WEB_DIR = pathlib.Path(__file__).parent / "web"

def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.load()
    setup_logging()
    deps = build_deps(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await initialize_deps(deps)
        bridge = asyncio.create_task(notification_bridge(deps))
        poller = None
        if deps.wecom is not None and deps.wecom.api_ready:
            poller = asyncio.create_task(wecom_poller(deps, deps.verification))
        app.state.notification_task = bridge
        app.state.poller_task = poller
        try:
            yield
        finally:
            tasks = [task for task in (poller, bridge) if task is not None]
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            await deps.pool.close()

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
    async def index():
        return FileResponse(WEB_DIR / "index.html")

    @app.get("/console")
    async def console():
        return FileResponse(WEB_DIR / "console.html")

    @app.get("/alert/{alert_id}")
    async def alert_page(alert_id: int):
        return FileResponse(WEB_DIR / "alert.html")

    @app.get("/join/{code}")
    async def join_page(code: str):
        return FileResponse(WEB_DIR / "join.html")

    return app


app = create_app()

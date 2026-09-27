"""
HTTP 组合根(FastAPI):装配依赖与路由,业务在 core,协议在 api/
家人凭证 = 不可枚举 token:所有家人 API 以 token 定位 user,无/错 token 一律
401;接口不返回 token。user 承载微信身份,member 表示群内成员关系,
群数据按 group_id 隔离。多群开通/绑定见 core/binding。
微信回调以平台签名校验;开通/绑定命令同步回复,其余 5s 窗口内先回执再异步判定
路由分块:api/groups.py(防护群 API)· api/wechat.py(公众号回调)· 本文件仅装配 + 静态页
"""
import pathlib

from fastapi import FastAPI
from fastapi.responses import FileResponse

from homeshield.api.groups import build_group_router
from homeshield.api.wechat import build_wechat_router
from homeshield.core.config import Settings
from homeshield.core.deps import build_deps
from homeshield.core.feedback import CorrectionService
from homeshield.core.logsetup import setup_logging

WEB_DIR = pathlib.Path(__file__).parent / "web"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.load()
    setup_logging()
    deps = build_deps(settings)
    app = FastAPI(title="Homeshield")   # 这个 app 就是后续 uvicorn module:app 启动时引用的对象
    app.state.deps = deps # 把构建好的依赖（数据库连接等）登记为应用级共享状态

    # 注册网页控制台和防护群接口
    app.include_router(
        build_group_router(
            deps,                              # 共享数据库、配置和服务
            deps.verification,                 # 反诈判定服务
            CorrectionService(deps.repos),     # 纠正提交与审核服务
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

    @app.get("/alert/{verdict_id}")
    def alert_page(verdict_id: int):
        return FileResponse(WEB_DIR / "alert.html")

    @app.get("/join/{code}")
    def join_page(code: str):
        return FileResponse(WEB_DIR / "join.html")

    return app


app = create_app()

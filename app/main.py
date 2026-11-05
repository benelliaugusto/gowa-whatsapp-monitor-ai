"""Ponto de entrada do monitor-ia (FastAPI)."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import api, dashboard, db, webhook
from .config import settings
from .gowa import GowaError, get_gowa_client, get_sync_service
from .timeutil import utc_now_iso

log = logging.getLogger(__name__)
BASE_DIR = Path(__file__).resolve().parent


def _configure_logging() -> None:
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s :: %(message)s",
    )


class SyncScheduler:
    """Sincronização periódica em background (não bloqueia o boot)."""

    def __init__(self, interval_minutes: int, on_startup: bool):
        self.interval = max(1, int(interval_minutes)) * 60
        self.on_startup = on_startup
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="sync-scheduler")

    async def _loop(self) -> None:
        if self.on_startup:
            await asyncio.sleep(5)
            await self._run("startup")
        while True:
            await asyncio.sleep(self.interval)
            await self._run("auto")

    async def _run(self, trigger: str) -> None:
        try:
            resultado = await get_sync_service().sync(trigger=trigger)
            log.info(
                "sync[%s]: %s chats, %s mensagens novas",
                trigger,
                resultado.get("chats_scanned"),
                resultado.get("messages_inserted"),
            )
        except Exception:  # noqa: BLE001 - o scheduler nunca deve morrer
            log.exception("sync[%s] falhou", trigger)

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    _configure_logging()
    db.init_db()

    scheduler: SyncScheduler | None = None
    if settings.sync_enabled and settings.sync_interval_minutes > 0:
        scheduler = SyncScheduler(settings.sync_interval_minutes, settings.sync_on_startup)
        scheduler.start()
        log.info("sincronizacao automatica a cada %s min", settings.sync_interval_minutes)

    if settings.require_api_key and not settings.api_keys:
        log.warning("API_KEYS vazio: chamadas a /api/v1 serao recusadas ate configurar as chaves")
    if not settings.dashboard_password:
        log.warning("DASHBOARD_PASSWORD vazio: o painel esta acessivel sem senha")

    yield

    if scheduler is not None:
        await scheduler.stop()
    await get_gowa_client().close()


def create_app() -> FastAPI:
    app = FastAPI(
        title="monitor-ia",
        description=(
            "Painel de conversas do WhatsApp + API de leitura/escrita para agentes de IA. "
            "Leia /docs para o contrato completo (Swagger)."
        ),
        version=settings.app_version,
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url=None,
        openapi_url="/openapi.json",
    )

    app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
    app.include_router(api.router)
    app.include_router(webhook.router)
    app.include_router(dashboard.router)

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict:
        return {"status": "ok", "version": settings.app_version, "time": utc_now_iso()}

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon() -> Response:
        return Response(status_code=204)

    @app.exception_handler(GowaError)
    async def gowa_error_handler(request: Request, exc: GowaError) -> JSONResponse:
        return JSONResponse(
            status_code=502,
            content={"error": "gowa_unavailable", "message": str(exc)},
        )

    @app.middleware("http")
    async def audit_api_calls(request: Request, call_next):
        response = await call_next(request)
        caminho = request.url.path
        if caminho.startswith("/api/v1") and request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            db.log_api_call(
                request.method,
                caminho,
                getattr(request.state, "api_key_name", ""),
                response.status_code,
                request.client.host if request.client else "",
            )
        return response

    return app


app = create_app()


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=8000)